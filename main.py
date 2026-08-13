"""
Ties everything together. This is the file cron / GitHub Actions / your
external scheduler runs.

Flow (in order):
1. Pull current account state from Alpaca
2. Evaluate equity-level circuit breakers (daily-loss halt, drawdown sizing
   cut, flatten threshold) against the running equity peak
3. Enforce ATR-based stop-loss / take-profit (independent of Gemini)
4. Enforce portfolio consolidation if over MAX_OPEN_POSITIONS limits
5. De-leverage: sell weakest holdings until cash is back above target (heals
   negative-cash / margin states automatically)
6. Flatten every position if the deep-drawdown circuit breaker fired
7. Refresh account state after any forced sells
8. Evaluate the broad market regime (SPY-based)
9. 24/7 news -> decisions -> execution flow:
   - Live session (regular, or extended when ALLOW_EXTENDED_HOURS is on):
     decisions execute immediately -- but any overnight-queued ideas are first
     handed to Gemini for re-verification against fresh data, so a setup that
     broke overnight is dropped instead of placed blindly.
   - Overnight dead zone (8 PM - 4 AM ET, weekends): no order can fill, so the
     bot runs its analysis and QUEUES the trade ideas to
     data/pending_trades.json for the next morning's verification. No blind
     order submission while no session is running (the 2026-08-07 failure
     mode is structurally prevented).
10. Execute with pending-order-aware cash, hard no-margin rule, total-exposure
    cap, and flat (same-size) position sizing.
11. Record a full performance snapshot and log everything
"""

import json
import os
import time
from datetime import datetime

from config import (
    REGIME_POSITION_MULTIPLIERS,
    TRADE_ONLY_DURING_MARKET_HOURS,
    MAX_DRAWDOWN_FLATTEN_PCT,
    OVERNIGHT_QUEUE_ENABLED,
)
from trader import (
    get_account_snapshot,
    execute_trade,
    check_atr_stop_take_profit,
    enforce_portfolio_consolidation,
    enforce_deleveraging,
    flatten_portfolio,
    evaluate_circuit_breakers,
    record_performance_snapshot,
    is_market_open,
    is_trading_session,
    get_market_regime,
    get_eastern_time_str,
    notify,
    reconcile_foreign_activity,
    get_open_orders_with_side,
    should_end_of_day_flatten,
    load_pending_trades,
    save_pending_trades,
    clear_pending_trades,
    sync_dashboard_watchlist,
    summarize_trade_results,
    summarize_performance,
    build_performance_brief,
)
from news import get_news_candidates
from decide import get_trade_decisions

LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")

# Hard wall-clock budget for the ENTIRE run() (override with the
# RUN_BUDGET_SECONDS env var). The deadline is set once at the top of run()
# and threaded into every expensive step -- news fetch, the decision engine
# (feeds / scans / Gemini), watchlist sync, the performance snapshot -- each
# of which consumes at most the remaining time and skips cleanly when the
# deadline is spent. This is what guarantees a run can never blow past the
# 60s shell cap (`timeout 60` in run-bot.yml): 55 leaves ~5s of headroom for
# Python startup / SDK imports, so the whole python process finishes inside
# the 60s window while still spending nearly all of it on real work.
RUN_BUDGET_SECONDS = float(os.environ.get("RUN_BUDGET_SECONDS", 55))

# Keep the committed daily log bounded. Every 2-min run appends its full
# output, so without this the log grows without limit and the per-run
# git commit/push of a multi-MB file gets slower run after run. Once a day's
# log exceeds LOG_MAX_BYTES it is truncated to its most recent
# LOG_KEEP_BYTES (pure tail-keep; the newest entries are the ones that
# matter for debugging).
LOG_MAX_BYTES = int(os.environ.get("LOG_MAX_BYTES", 1_500_000))
LOG_KEEP_BYTES = int(os.environ.get("LOG_KEEP_BYTES", 500_000))

# Per-section run timing: _mark() records the wall-clock seconds since the
# previous mark, and run() prints a "Timings:" line at the end so it's obvious
# at a glance where each run spends its time (and what to look at if a run
# ever stalls). Production runs call run() once per process, so the marks are
# clean per run.
_marks = {}


def _mark(section):
    now = time.monotonic()
    _t0 = _marks.pop("_t0", None)
    _marks[section] = (now - _t0) if _t0 is not None else 0.0
    _marks["_t0"] = now


def run():
    os.makedirs(LOG_DIR, exist_ok=True)
    timestamp = datetime.now()
    log_lines = [f"=== Run at {timestamp.isoformat()} ==="]

    # One deadline for the whole run -- see RUN_BUDGET_SECONDS above. Every
    # budgeted step receives the remaining time; cosmetic steps are skipped
    # once it's spent.
    _run_deadline = time.monotonic() + RUN_BUDGET_SECONDS

    def _remaining_run():
        return max(0.0, _run_deadline - time.monotonic())

    market_open = is_market_open()
    # A live "trading session" is the regular session, plus the Alpaca extended
    # session (4:00-9:30 AM / 4:00-8:00 PM ET) when ALLOW_EXTENDED_HOURS is on
    # and the strict TRADE_ONLY_DURING_MARKET_HOURS kill-switch is off. Outside
    # a live session the bot QUEUES trade ideas instead of submitting (below).
    trading_session_active = market_open or (not TRADE_ONLY_DURING_MARKET_HOURS and is_trading_session())
    if market_open:
        session_label = "REGULAR"
    elif trading_session_active:
        session_label = "EXTENDED"
    else:
        session_label = "CLOSED"
    log_lines.append(f"Current time: {get_eastern_time_str()} | Market open (per Alpaca): {market_open} | Session: {session_label}")

    try:
        account = get_account_snapshot()
    except Exception as e:
        log_lines.append(f"FATAL: could not fetch account snapshot, aborting run. Error: {e}")
        _write_log(log_lines, timestamp)
        return

    log_lines.append(f"Account value: ${account['total_value']:,.2f}")
    holdings_summary = {t: f"{p['qty']} sh ({p['unrealized_plpc']:+.2f}%)" for t, p in account["holdings"].items()}
    log_lines.append(f"Cash: ${account['cash']:,.2f} | Holdings ({len(account['holdings'])}): {holdings_summary or 'none'}")
    _mark("account")

    # Open-order visibility: queued/pending orders (e.g. de-leveraging sells
    # placed after the close) are why the account may look unchanged at night
    # -- they fill at the next open. Log them so a night run never looks like
    # it "did nothing".
    open_orders = []
    try:
        open_orders = get_open_orders_with_side()
        if open_orders:
            brief = ", ".join(f"{o['symbol']} {o['side']} {o['qty']:g}" for o in open_orders[:12])
            log_lines.append(f"Open orders ({len(open_orders)}): {brief}")
    except Exception as e:
        log_lines.append(f"Could not fetch open orders: {e}")

    # Step -1: second-trader detection -- reconcile the actual account against
    # this bot's own order ledger. Any holding or quantity this bot never
    # created is flagged loudly (and notified), so a second bot / local cron /
    # manual trades on the same Alpaca keys can't silently run again.
    try:
        recon_flags, baseline_created = reconcile_foreign_activity(account)
        for f in recon_flags:
            log_lines.append(f"  !! {f}")
            notify(f"SECOND TRADER DETECTION: {f}")
        if baseline_created:
            log_lines.append(
                "  -> Order ledger started fresh. Holdings are now the baseline; "
                "future deviations from the ledger are flagged."
            )
    except Exception as e:
        log_lines.append(f"Reconciliation step failed (continuing): {e}")

    # Step 0: equity-level circuit breakers (daily loss, drawdown sizing cut,
    # flatten threshold). Runs before anything else so a damaged account never
    # keeps trading at full size.
    halted = False
    halt_reason = ""
    breaker_multiplier = 1.0
    drawdown_pct = 0.0
    daily_pl_pct = 0.0
    peak_equity = 0.0
    try:
        halted, halt_reason, breaker_multiplier, drawdown_pct, daily_pl_pct, peak_equity, breaker_msgs = evaluate_circuit_breakers(account)
        for m in breaker_msgs:
            log_lines.append(f"  -> {m}")
            notify(f"{m}")
        log_lines.append(
            f"Risk state: peak ${peak_equity:,.2f} | "
            f"drawdown {drawdown_pct:.2f}% | today {daily_pl_pct:+.2f}% | halted: {halted}"
        )
    except Exception as e:
        log_lines.append(f"Circuit breaker step failed (continuing): {e}")

    if account["cash"] < 0:
        notify(
            f"MARGIN WARNING: paper account cash is negative (${account['cash']:,.2f}). "
            "De-leveraging will run before any new buys."
        )

    # Step 1: hard ATR-based risk management (always allowed, any regime)
    risk_exits = 0
    try:
        risk_sells = check_atr_stop_take_profit(account)
        risk_exits = len(risk_sells)
        if risk_sells:
            log_lines.append(f"Risk management triggered {risk_exits} forced sell(s):")
            for r in risk_sells:
                log_lines.append(f"  -> {json.dumps(r)}")
            account = get_account_snapshot()
    except Exception as e:
        log_lines.append(f"Risk management step failed (continuing anyway): {e}")

    # Step 1b: portfolio consolidation (purges worst excess holdings below threshold)
    consolidation_exits = 0
    try:
        consolidation_sells = enforce_portfolio_consolidation(account)
        consolidation_exits = len(consolidation_sells)
        if consolidation_sells:
            log_lines.append(f"Consolidation engine triggered {consolidation_exits} forced cleanup sell(s):")
            for c in consolidation_sells:
                log_lines.append(f"  -> {json.dumps(c)}")
            account = get_account_snapshot()
    except Exception as e:
        log_lines.append(f"Consolidation step failed (continuing anyway): {e}")

    # Step 1c: de-leveraging -- sell weakest holdings until cash is back above
    # target. Runs every cycle; this is what heals a negative-cash account
    # instead of leaving it in margin for days.
    deleverage_exits = 0
    try:
        deleverage_sells = enforce_deleveraging(account)
        deleverage_exits = len(deleverage_sells)
        if deleverage_sells:
            log_lines.append(f"De-leveraging triggered {deleverage_exits} sell(s) to restore cash:")
            for d in deleverage_sells:
                log_lines.append(f"  -> {json.dumps(d)}")
            notify(f"De-leveraging: sold {deleverage_exits} position(s) to restore positive cash.")
            account = get_account_snapshot()
    except Exception as e:
        log_lines.append(f"De-leveraging step failed (continuing anyway): {e}")

    # Step 1c.5: daytrading discipline -- flatten everything back to cash at
    # END_OF_DAY_FLATTEN_TIME ET so no position ever survives overnight (the
    # 2026-08-11 3:30 AM ET liquidation hit an overnight position).
    eod_flatten_exits = 0
    if should_end_of_day_flatten():
        try:
            eod_sells = flatten_portfolio(account, reason="End-of-day flatten: daytrading discipline.", trigger="end_of_day_flatten")
            eod_flatten_exits = len(eod_sells)
            if eod_sells:
                log_lines.append(f"End-of-day flatten: closed {eod_flatten_exits} position(s):")
                for e_ in eod_sells:
                    log_lines.append(f"  -> {json.dumps(e_)}")
                account = get_account_snapshot()
        except Exception as e:
            log_lines.append(f"End-of-day flatten step failed (continuing anyway): {e}")

    # Step 1d: flatten everything if the deep-drawdown circuit breaker fired.
    # (Opt-in: MAX_DRAWDOWN_FLATTEN_PCT defaults to 0 = disabled.)
    flatten_exits = 0
    if MAX_DRAWDOWN_FLATTEN_PCT > 0 and drawdown_pct >= MAX_DRAWDOWN_FLATTEN_PCT:
        try:
            flatten_sells = flatten_portfolio(account)
            flatten_exits = len(flatten_sells)
            if flatten_sells:
                log_lines.append(f"CIRCUIT BREAKER: flattening {flatten_exits} position(s) (drawdown {drawdown_pct:.1f}%):")
                for f_ in flatten_sells:
                    log_lines.append(f"  -> {json.dumps(f_)}")
                notify(f"CIRCUIT BREAKER FLATTEN: portfolio flattened ({flatten_exits} positions) on {drawdown_pct:.1f}% drawdown.")
                account = get_account_snapshot()
        except Exception as e:
            log_lines.append(f"Flatten step failed (continuing anyway): {e}")

    _mark("risk")
    # Step 2: market regime -> position-sizing multiplier
    try:
        regime = get_market_regime()
    except Exception as e:
        log_lines.append(f"Market regime check failed, defaulting to NEUTRAL: {e}")
        regime = "NEUTRAL"
    size_multiplier = REGIME_POSITION_MULTIPLIERS.get(regime, 0.6) * breaker_multiplier
    log_lines.append(f"Market regime: {regime} (position-size multiplier: {size_multiplier:.0%} incl. circuit breaker {breaker_multiplier:.0%})")

    # Steps 3-5 (news -> decisions -> execution): 24/7 flow.
    #   * Live session (regular, or extended when ALLOW_EXTENDED_HOURS):
    #     decisions execute immediately, but any overnight-queued ideas are
    #     first re-verified by Gemini against fresh data -- a setup that broke
    #     overnight is dropped, not placed blindly.
    #   * Overnight dead zone (8 PM - 4 AM ET, weekends, holidays): no order
    #     can fill, so instead of submitting blind orders (the 2026-08-07
    #     failure mode) the bot runs its analysis and QUEUES the ideas to
    #     data/pending_trades.json for the next morning's verification.
    # Risk management / de-leveraging above always run, regardless of session.
    trades = []
    candidates = {}
    decision_meta = {
        "candidates_considered": 0,
        "candidates_passed_prescreen": 0,
        "technical_fallback": False,
        "gemini_calls_today": 0,
    }
    executed = skipped = failed = 0

    if halted:
        log_lines.append(f"Trading halted ({halt_reason}) -- no new trades this run.")
    elif trading_session_active:
        # Morning verification: hand any overnight-queued ideas to Gemini so it
        # re-checks them against fresh data BEFORE anything executes. Whatever
        # Gemini re-approves comes back in `trades` and is executed below;
        # everything it omits is dropped when the queue is cleared.
        pending = load_pending_trades() if OVERNIGHT_QUEUE_ENABLED else []
        if pending:
            log_lines.append(
                f"Morning verification: {len(pending)} overnight-queued trade(s) "
                "handed to Gemini for re-verification against fresh data."
            )

        # Step 3: news
        try:
            candidates, news_stats, news_sentiment = get_news_candidates(time_budget=_remaining_run())
            # news_sentiment ({ticker: -1..+1}) is folded into the quant
            # scores inside decide.py; surfaced here for the log only.
            log_lines.append(
                f"News: fetched {news_stats['articles_fetched']}, "
                f"{news_stats['articles_new_after_dedup']} new after dedup, "
                f"{news_stats['tickers_matched']} ticker(s) matched."
            )
        except Exception as e:
            log_lines.append(f"News fetch failed, proceeding with watchlist only: {e}")
            candidates = {}

        # Step 4: decisions (quant pre-screen inside get_trade_decisions)
        try:
            trades, decision_meta = get_trade_decisions(candidates, account, regime, pending_trades=pending, time_budget=_remaining_run())
            log_lines.append(
                f"Quant pre-screen: {decision_meta['candidates_passed_prescreen']}/"
                f"{decision_meta['candidates_considered']} candidates passed."
            )
            # Reflect what actually produced the trades, and surface today's
            # Gemini call count so quota usage is visible at a glance.
            engine_label = "Technical fallback (Gemini throttled/unavailable)" if decision_meta.get("technical_fallback") else "Gemini"
            log_lines.append(f"{engine_label} proposed {len(trades)} trade(s) meeting the conviction bar.")
            log_lines.append(f"Gemini calls used today (all models combined): {decision_meta.get('gemini_calls_today', 0)}")
        except Exception as e:
            log_lines.append(f"Decision step failed, no trades this run: {e}")
            trades = []

        # Step 5: execute
        for trade in trades:
            try:
                result = execute_trade(trade, account, size_multiplier=size_multiplier)
                log_lines.append(f"  -> {json.dumps(result)}")
                status = result.get("status")
                if status == "submitted":
                    executed += 1
                    account = get_account_snapshot()
                elif status == "skipped":
                    skipped += 1
                else:
                    failed += 1
            except Exception as e:
                failed += 1
                log_lines.append(f"  -> FAILED to execute {trade.get('ticker')}: {e}")

        # Gemini re-verified the queue this round: whatever it re-approved is
        # in `trades` (executed above); everything it dropped is discarded.
        if pending:
            clear_pending_trades()
            log_lines.append("Overnight queue cleared after verification.")
    else:
        # Overnight dead zone: analyze and QUEUE ideas for the morning. Nothing
        # is submitted now -- the first live-session run re-verifies them with
        # Gemini before placing anything.
        try:
            candidates, news_stats, news_sentiment = get_news_candidates(time_budget=_remaining_run())
            log_lines.append(
                f"News: fetched {news_stats['articles_fetched']}, "
                f"{news_stats['articles_new_after_dedup']} new after dedup, "
                f"{news_stats['tickers_matched']} ticker(s) matched."
            )
        except Exception as e:
            log_lines.append(f"News fetch failed, proceeding with watchlist only: {e}")
            candidates = {}

        try:
            trades, decision_meta = get_trade_decisions(candidates, account, regime, time_budget=_remaining_run())
            log_lines.append(
                f"Quant pre-screen: {decision_meta['candidates_passed_prescreen']}/"
                f"{decision_meta['candidates_considered']} candidates passed."
            )
            engine_label = "Technical fallback (Gemini throttled/unavailable)" if decision_meta.get("technical_fallback") else "Gemini"
            log_lines.append(f"{engine_label} proposed {len(trades)} trade(s) meeting the conviction bar.")
            log_lines.append(f"Gemini calls used today (all models combined): {decision_meta.get('gemini_calls_today', 0)}")
        except Exception as e:
            log_lines.append(f"Decision step failed, no trades this run: {e}")
            trades = []

        if OVERNIGHT_QUEUE_ENABLED and trades:
            queued = save_pending_trades(trades)
            log_lines.append(
                f"Market closed (overnight): queued {queued} trade idea(s) for "
                "next-morning verification -- nothing submitted yet."
            )
            for trade in trades:
                log_lines.append(
                    f"  -> queued {trade.get('ticker')} {trade.get('action')} "
                    f"(conf {trade.get('confidence')}, stop {trade.get('stop_loss')}, "
                    f"tp {trade.get('take_profit')})"
                )
        else:
            log_lines.append("Market closed (overnight): no trade ideas queued this run.")

    _mark("decide")
    # Step 5b: keep the Alpaca dashboard watchlist in sync so the app shows
    # news/headlines for the bot's active names even while the account is
    # flat. Best-effort -- a cosmetic failure never stops the run. Skipped
    # entirely if the run deadline is already spent (one more API call would
    # push the run past the cap for zero trading value).
    if _remaining_run() <= 0:
        log_lines.append("Run budget spent -- skipping dashboard watchlist sync.")
    else:
        try:
            watchlist_tickers = set(account["holdings"].keys())
            watchlist_tickers.update(o["symbol"] for o in open_orders)
            watchlist_tickers.update(candidates.keys())
            watchlist_tickers.update(t["ticker"] for t in trades)
            synced = sync_dashboard_watchlist(watchlist_tickers)
            if synced:
                log_lines.append(f"Dashboard watchlist synced ({synced} symbols) for news/headlines.")
        except Exception as e:
            log_lines.append(f"Dashboard watchlist sync skipped: {e}")

    _mark("watchlist")
    # Step 6: performance tracking. Skipped if the run deadline is spent:
    # the snapshot needs one more Alpaca call, and trading correctness (the
    # steps above) matters more than this cosmetic record.
    if _remaining_run() <= 0:
        log_lines.append("Run budget spent -- skipping performance snapshot.")
        final_account = account
    else:
        try:
            final_account = get_account_snapshot()
            record_performance_snapshot(
            final_account,
            LOG_DIR,
            market_regime=regime,
            size_multiplier=size_multiplier,
            candidates_considered=decision_meta.get("candidates_considered", 0),
            candidates_passed_prescreen=decision_meta.get("candidates_passed_prescreen", 0),
            trades_proposed=len(trades),
            trades_executed=executed,
            trades_skipped=skipped,
            trades_failed=failed,
            risk_exits=risk_exits,
            consolidation_exits=consolidation_exits,
        )
            log_lines.append(f"Ending portfolio value: ${final_account['total_value']:,.2f}")
        except Exception as e:
            log_lines.append(f"Could not record performance snapshot: {e}")

    # Step 6b: win rate by setup, from the closed-trade journal.
    try:
        summary = summarize_trade_results()
        log_lines.append(f"Trade journal: {summary}")
    except Exception as e:
        log_lines.append(f"Could not summarize trade results: {e}")

    # Step 6b2: Phase 3 self-learning -- what setups are being weighted up/down.
    try:
        brief = build_performance_brief()
        if brief != "no closed trades yet -- all setups unproven":
            log_lines.append(f"Self-learning: {brief}")
    except Exception as e:
        log_lines.append(f"Could not build self-learning brief: {e}")

    # Step 6c: portfolio stats -- total/daily return, Sharpe, max drawdown,
    # win rate, avg winner/loser, open positions.
    try:
        perf = summarize_performance(account)
        log_lines.append(f"Performance: {perf}")
    except Exception as e:
        log_lines.append(f"Could not summarize performance: {e}")

    _mark("perf")
    timings = " | ".join(f"{k}: {v:.1f}s" for k, v in _marks.items() if k != "_t0")
    log_lines.append(f"Timings: {timings}")
    log_lines.append("")
    _write_log(log_lines, timestamp)
    print("\n".join(log_lines))


def _write_log(log_lines, timestamp):
    log_path = os.path.join(LOG_DIR, f"{timestamp.strftime('%Y-%m-%d')}.log")
    block = "\n".join(log_lines) + "\n"
    with open(log_path, "a") as f:
        f.write(block)
    # Rotate: once today's log exceeds LOG_MAX_BYTES, keep only its tail so
    # the per-run git commit/push of logs/ stays fast forever (an unbounded
    # log was dragging every run's commit step slower and slower).
    try:
        if os.path.getsize(log_path) > LOG_MAX_BYTES:
            with open(log_path) as f:
                content = f.read()
            with open(log_path, "w") as f:
                f.write(content[-LOG_KEEP_BYTES:])
    except OSError:
        pass


if __name__ == "__main__":
    run()
