"""
Ties everything together. This is the file cron / GitHub Actions / your
external scheduler runs.

Flow (in order):
  0. Skip entirely if the market is closed (MARKET_HOURS_ONLY in config.py)
  1. Pull current account state from Alpaca
  2. Enforce ATR-based stop-loss / take-profit (independent of Gemini)
  3. Refresh account state after any forced sells
  4. Fetch news (deduped against recently-seen articles)
  5. Ask Gemini to review holdings + news candidates + fixed watchlist,
     each idea scored with a conviction rating
  6. Execute trades (conviction-scaled size, cooldown/open-order aware,
     re-checking account state between trades so multi-trade runs don't
     all size against the same stale cash figure)
  7. Record a performance snapshot and log everything
"""

import json
import os
from datetime import datetime

from config import MARKET_HOURS_ONLY
from trader import (
    get_account_snapshot, execute_trade, check_atr_stop_take_profit,
    record_performance_snapshot, is_market_open,
)
from news import get_news_candidates
from decide import get_trade_decisions

LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")


def run():
    os.makedirs(LOG_DIR, exist_ok=True)
    timestamp = datetime.now()
    log_lines = [f"=== Run at {timestamp.isoformat()} ==="]

    if MARKET_HOURS_ONLY and not is_market_open():
        log_lines.append("Market closed -- skipping run.")
        _write_log(log_lines, timestamp)
        print("\n".join(log_lines))
        return

    try:
        account = get_account_snapshot()
    except Exception as e:
        log_lines.append(f"FATAL: could not fetch account snapshot, aborting run. Error: {e}")
        _write_log(log_lines, timestamp)
        return

    log_lines.append(f"Account value: ${account['total_value']:,.2f}")
    holdings_summary = {t: f"{p['qty']} sh ({p['unrealized_plpc']:+.2f}%)" for t, p in account["holdings"].items()}
    log_lines.append(f"Cash: ${account['cash']:,.2f} | Holdings: {holdings_summary or 'none'}")

    # Step 1: hard ATR-based risk management
    try:
        risk_sells = check_atr_stop_take_profit(account)
        if risk_sells:
            log_lines.append(f"Risk management triggered {len(risk_sells)} forced sell(s):")
            for r in risk_sells:
                log_lines.append(f"  -> {json.dumps(r)}")
            account = get_account_snapshot()
    except Exception as e:
        log_lines.append(f"Risk management step failed (continuing anyway): {e}")

    # Step 2: news
    try:
        candidates = get_news_candidates()
        log_lines.append(f"Found {len(candidates)} newly-mentioned companies in news.")
    except Exception as e:
        log_lines.append(f"News fetch failed, proceeding with watchlist only: {e}")
        candidates = {}

    # Step 3: decisions
    try:
        trades = get_trade_decisions(candidates, account)
        log_lines.append(f"Gemini proposed {len(trades)} trade(s) meeting the conviction bar.")
    except Exception as e:
        log_lines.append(f"Decision step failed, no trades this run: {e}")
        trades = []

    # Step 4: execute
    # Re-fetch account state after each fill so a run with multiple trades
    # doesn't size every trade against the same stale cash/holdings figures.
    for trade in trades:
        try:
            result = execute_trade(trade, account)
            log_lines.append(f"  -> {json.dumps(result)}")
            if result.get("status") == "submitted":
                account = get_account_snapshot()
        except Exception as e:
            log_lines.append(f"  -> FAILED to execute {trade.get('ticker')}: {e}")

    # Step 5: performance tracking
    try:
        final_account = get_account_snapshot()
        record_performance_snapshot(final_account, LOG_DIR)
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
