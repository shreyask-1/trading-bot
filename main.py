"""
Ties everything together. This is the file GitHub Actions runs on a
schedule.

Flow (in order):
  1. Pull current account state from your real Alpaca PAPER account
  2. Enforce hard stop-loss / take-profit rules (independent of Gemini)
  3. Enforce hard position-size caps (independent of Gemini)
  4. Refresh account state after any forced sells
  5. Fetch news, find which S&P 500 companies are mentioned
  6. Add fixed watchlist tickers as technical-only candidates (no news needed)
  7. Ask Gemini to review existing holdings AND consider new candidates
  8. Submit real (paper) orders to Alpaca, skipping cooldown/pending tickers
  9. Record a performance snapshot and log everything to logs/

If Alpaca, Finnhub, or Gemini have a transient failure (e.g. a 500 error
on their end), this run logs the error and exits cleanly instead of
crashing -- the next scheduled run a few minutes later will just try again.
"""

import json
import os
from datetime import datetime

from trader import (
    get_account_snapshot,
    execute_trade,
    check_stop_loss_take_profit,
    check_position_caps,
    record_performance_snapshot,
)
from news import get_news_candidates
from decide import get_trade_decisions
from config import WATCHLIST

LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")


def run():
    os.makedirs(LOG_DIR, exist_ok=True)
    timestamp = datetime.now()
    log_lines = [f"=== Run at {timestamp.isoformat()} ==="]

    try:
        account = get_account_snapshot()
    except Exception as e:
        log_lines.append(f"FATAL: could not fetch account snapshot, aborting this run. Error: {e}")
        _write_log(log_lines, timestamp)
        print("\n".join(log_lines))
        return  # exit cleanly -- next scheduled run will retry

    log_lines.append(f"Alpaca paper account value: ${account['total_value']:,.2f}")
    holdings_summary = {t: f"{p['qty']} shares ({p['unrealized_plpc']:+.2f}%)" for t, p in account["holdings"].items()}
    log_lines.append(f"Cash: ${account['cash']:,.2f} | Holdings: {holdings_summary or 'none'}")

    try:
        risk_sells = check_stop_loss_take_profit(account)
        if risk_sells:
            log_lines.append(f"Risk management triggered {len(risk_sells)} forced sell(s):")
            for result in risk_sells:
                log_lines.append(f"  -> {json.dumps(result)}")
            account = get_account_snapshot()
    except Exception as e:
        log_lines.append(f"WARNING: stop-loss/take-profit check failed this run: {e}")

    try:
        cap_trims = check_position_caps(account)
        if cap_trims:
            log_lines.append(f"Position-cap enforcement triggered {len(cap_trims)} trim(s):")
            for result in cap_trims:
                log_lines.append(f"  -> {json.dumps(result)}")
            account = get_account_snapshot()
    except Exception as e:
        log_lines.append(f"WARNING: position-cap check failed this run: {e}")

    try:
        candidates = get_news_candidates()
        log_lines.append(f"Found {len(candidates)} companies mentioned in current news.")
    except Exception as e:
        log_lines.append(f"WARNING: news fetch failed this run, continuing with watchlist only: {e}")
        candidates = {}

    added_technical = 0
    for ticker in WATCHLIST:
        if ticker not in candidates and ticker not in account["holdings"]:
            candidates[ticker] = []
            added_technical += 1
    log_lines.append(f"Added {added_technical} watchlist ticker(s) for technical-only evaluation.")

    try:
        trades = get_trade_decisions(candidates, account)
        log_lines.append(f"Gemini recommended {len(trades)} trade(s).")
    except Exception as e:
        log_lines.append(f"WARNING: Gemini decision step failed this run, no new trades: {e}")
        trades = []

    for trade in trades:
        try:
            result = execute_trade(trade, account)
            log_lines.append(f"  -> {json.dumps(result)}")
        except Exception as e:
            log_lines.append(f"  -> FAILED to execute trade for {trade.get('ticker')}: {e}")

    try:
        final_account = get_account_snapshot()
        record_performance_snapshot(final_account, LOG_DIR)
        log_lines.append(f"Ending portfolio value: ${final_account['total_value']:,.2f}")
    except Exception as e:
        log_lines.append(f"WARNING: could not record final performance snapshot: {e}")

    _write_log(log_lines, timestamp)
    print("\n".join(log_lines))


def _write_log(log_lines, timestamp):
    log_lines.append("")
    log_path = os.path.join(LOG_DIR, f"{timestamp.strftime('%Y-%m-%d')}.log")
    with open(log_path, "a") as f:
        f.write("\n".join(log_lines) + "\n")


if __name__ == "__main__":
    run()
