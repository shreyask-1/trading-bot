"""
Ties everything together. This is the file cron / GitHub Actions / your
external scheduler runs.

Flow (in order):
  1. Pull current account state from Alpaca
  2. Enforce ATR-based stop-loss / take-profit (independent of Gemini
     and of market regime)
  3. Refresh account state after any forced sells
  4. Evaluate the broad market regime (SPY-based) -- determines a
     position-sizing multiplier enforced in code
  5. Fetch news (deduped against recently-seen articles)
  6. Ask Gemini to review holdings + quant-prescreened news candidates +
     quant-prescreened watchlist
  7. Execute trades (conviction- and regime-scaled size, cooldown/open-order
     aware, re-checking account state between trades -- orders submitted
     outside market hours will queue for the next open)
  8. Record a full performance snapshot and log everything
"""

import json
import os
from datetime import datetime

from config import MARKET_HOURS_ONLY, REGIME_POSITION_MULTIPLIERS
from trader import (
    get_account_snapshot, execute_trade, check_atr_stop_take_profit,
    record_performance_snapshot, is_market_open, get_market_regime,
)
from news import get_news_candidates
from decide import get_trade_decisions

LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")


def run():
    os.makedirs(LOG_DIR, exist_ok=True)
    timestamp = datetime.now()
    log_lines = [f"=== Run at {timestamp.isoformat()} ==="]

    # Note: if the market is closed, orders submitted here will queue at
    # Alpaca for the next open (DAY orders). We do NOT abort the run --
    # the bot continues to evaluate news, regime, holdings, and submit
    # trades regardless of the clock.
    market_open = is_market_open() if MARKET_HOURS_ONLY else True
    log_lines.append(f"Market open: {market_open}")

    try:
        account = get_account_snapshot()
    except Exception as e:
        log_lines.append(f"FATAL: could not fetch account snapshot, aborting run. Error: {e}")
        _write_log(log_lines, timestamp)
        return

    log_lines.append(f"Account value: ${account['total_value']:,.2f}")
    holdings_summary = {t: f"{p['qty']} sh ({p['unrealized_plpc']:+.2f}%)" for t, p in account["holdings"].items()}
    log_lines.append(f"Cash: ${account['cash']:,.2f} | Holdings: {holdings_summary or 'none'}")

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

    # Step 2: market regime -> position-sizing multiplier
    try:
        regime = get_market_regime()
    except Exception as e:
        log_lines.append(f"Market regime check failed, defaulting to NEUTRAL: {e}")
        regime = "NEUTRAL"
    size_multiplier = REGIME_POSITION_MULTIPLIERS.get(regime, 0.6)
    log_lines.append(f"Market regime: {regime} (position-size multiplier: {size_multiplier:.0%})")

    # Step 3: news
    try:
        candidates = get_news_candidates()
        log_lines.append(f"Found {len(candidates)} newly-mentioned companies in news.")
    except Exception as e:
        log_lines.append(f"News fetch failed, proceeding with watchlist only: {e}")
        candidates = {}

    # Step 4: decisions (quant pre-screen inside get_trade_decisions)
    decision_meta = {"candidates_considered": 0, "candidates_passed_prescreen": 0}
    try:
        trades, decision_meta = get_trade_decisions(candidates, account, regime)
        log_lines.append(
            f"Quant pre-screen: {decision_meta['candidates_passed_prescreen']}/"
            f"{decision_meta['candidates_considered']} candidates passed."
        )
        log_lines.append(f"Gemini proposed {len(trades)} trade(s) meeting the conviction bar.")
    except Exception as e:
        log_lines.append(f"Decision step failed, no trades this run: {e}")
        trades = []

    # Step 5: execute
    # Re-fetch account state after each fill. Orders submitted when the
    # market is closed are queued by Alpaca and execute at the open.
    executed = skipped = failed = 0
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
            final_account, LOG_DIR,
            market_regime=regime,
            size_multiplier=size_multiplier,
            candidates_considered=decision_meta.get("candidates_considered", 0),
            candidates_passed_prescreen=decision_meta.get("candidates_passed_prescreen", 0),
            trades_proposed=len(trades),
            trades_executed=executed,
            trades_skipped=skipped,
            trades_failed=failed,
            risk_exits=risk_exits,
        )
        log_lines.append(f"Ending portfolio value: ${final_account['total_value']:,.2f}")
    except Exception as e:
        log_lines.append(f"Could not record performance snapshot: {e}")

    log_lines.append("")
    _write_log(log_lines, timestamp)
    print("\n".join(log_lines))


def _write_log(log_lines, timestamp):
    log_path = os.path.join(LOG_DIR, f"{timestamp.strftime('%Y-%m-%d')}.log")
    with open(log_path, "a") as f:
        f.write("\n".join(log_lines) + "\n")


if __name__ == "__main__":
    run()
