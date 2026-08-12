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
9. NEW TRADES ARE ONLY PROPOSED/EXECUTED WHILE THE MARKET IS OPEN and the
   circuit breakers haven't halted the day -- no more overnight order stacking
10. Fetch news -> decisions -> execute (pending-order-aware cash, hard
    no-margin rule, total-exposure cap)
11. Record a full performance snapshot and log everything
"""

import json
import os
from datetime import datetime

from config import (
    REGIME_POSITION_MULTIPLIERS,
    TRADE_ONLY_DURING_MARKET_HOURS,
    MAX_DRAWDOWN_FLATTEN_PCT,
    DAYTRADE_MODE,
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
    get_market_regime,
    get_eastern_time_str,
    notify,
    reconcile_foreign_activity,
    get_open_orders_with_side,
    should_end_of_day_flatten,
    is_within_trade_window,
    summarize_trade_results,
    summarize_performance,
    build_performance_brief,
)
from news import get_news_candidates
from decide import get_trade_decisions

LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")


def run():
    os.makedirs(LOG_DIR, exist_ok=True)
    timestamp = datetime.now()
    log_lines = [f"=== Run at {timestamp.isoformat()} ==="]

    market_open = is_market_open()
    log_lines.append(f"Current time: {get_eastern_time_str()} | Market open (per Alpaca): {market_open}")

    try:
        account = get_account_snapshot()
    except Exception as e:
        log_lines.append(f"FATAL: could not fetch account snapshot, aborting run. Error: {e}")
        _write_log(log_lines, timestamp)
        return

    log_lines.append(f"Account value: ${account['total_value']:,.2f}")
    holdings_summary = {t: f"{p['qty']} sh ({p['unrealized_plpc']:+.2f}%)" for t, p in account["holdings"].items()}
    log_lines.append(f"Cash: ${account['cash']:,.2f} | Holdings ({len(account['holdings'])}): {holdings_summary or 'none'}")

    # Open-order visibility: queued/pending orders (e.g. de-leveraging sells
    # placed after the close) are why the account may look unchanged at night
    # -- they fill at the next open. Log them so a night run never looks like
    # it "did nothing".
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

    # Step 2: market regime -> position-sizing multiplier
    try:
        regime = get_market_regime()
    except Exception as e:
        log_lines.append(f"Market regime check failed, defaulting to NEUTRAL: {e}")
        regime = "NEUTRAL"
    size_multiplier = REGIME_POSITION_MULTIPLIERS.get(regime, 0.6) * breaker_multiplier
    log_lines.append(f"Market regime: {regime} (position-size multiplier: {size_multiplier:.0%} incl. circuit breaker {breaker_multiplier:.0%})")

    # Steps 3-5 (news -> decisions -> execution) are GATED: never propose new
    # trades while the market is closed (the overnight order-stacking bug) or
    # while the day is halted by a circuit breaker. Risk management and
    # de-leveraging above still run in all cases.
    trades = []
    decision_meta = {
        "candidates_considered": 0,
        "candidates_passed_prescreen": 0,
        "technical_fallback": False,
        "gemini_calls_today": 0,
    }
    executed = skipped = failed = 0

    daytrade_window = is_within_trade_window()
    if halted:
        log_lines.append(f"Trading halted ({halt_reason}) -- no new trades this run.")
    elif not market_open and TRADE_ONLY_DURING_MARKET_HOURS:
        log_lines.append(
            "Market closed and TRADE_ONLY_DURING_MARKET_HOURS is enabled -- "
            "skipping news, decisions, and execution this run. Risk management "
            "and de-leveraging still ran above."
        )
    elif DAYTRADE_MODE and not daytrade_window:
        log_lines.append(
            "Outside the daytrading entry window (first 15 min chop / after "
            "15:30 ET) -- no NEW buys this run. Risk management, stops, and "
            "de-leveraging still ran above."
        )
    else:
        # Step 3: news
        try:
            candidates, news_stats, news_sentiment = get_news_candidates()
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
            trades, decision_meta = get_trade_decisions(candidates, account, regime)
            log_lines.append(
                f"Quant pre-screen: {decision_meta['candidates_passed_prescreen']}/"
                f"{decision_meta['candidates_considered']} candidates passed."
            )
            # FIX: this used to unconditionally say "Gemini proposed...", even on
            # runs where Gemini's quota was exhausted and the pure-technical
            # fallback engine generated the ideas instead. Now it reflects what
            # actually produced the trades, and surfaces today's Gemini call
            # count so quota usage is visible at a glance.
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

    # Step 6: performance tracking
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

    log_lines.append("")
    _write_log(log_lines, timestamp)
    print("\n".join(log_lines))


def _write_log(log_lines, timestamp):
    log_path = os.path.join(LOG_DIR, f"{timestamp.strftime('%Y-%m-%d')}.log")
    with open(log_path, "a") as f:
        f.write("\n".join(log_lines) + "\n")


if __name__ == "__main__":
    run()
