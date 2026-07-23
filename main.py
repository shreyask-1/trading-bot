"""
Ties everything together. This is the file cron will run every hour
during market hours.

Flow:
  1. Pull current account state from your real Alpaca PAPER account
  2. Fetch news, find which S&P 500 companies are mentioned
  3. Ask Gemini for trade decisions
  4. Submit real (paper) orders to Alpaca
  5. Log everything to logs/
"""

import json
import os
from datetime import datetime

from trader import get_account_snapshot, execute_trade
from news import get_news_candidates
from decide import get_trade_decisions

LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")


def run():
    os.makedirs(LOG_DIR, exist_ok=True)
    timestamp = datetime.now()
    log_lines = [f"=== Run at {timestamp.isoformat()} ==="]

    account = get_account_snapshot()
    log_lines.append(f"Alpaca paper account value: ${account['total_value']:,.2f}")
    log_lines.append(f"Cash: ${account['cash']:,.2f} | Holdings: {account['holdings'] or 'none'}")

    # Step 1: news
    candidates = get_news_candidates()
    log_lines.append(f"Found {len(candidates)} companies mentioned in current news.")

    if not candidates:
        log_lines.append("No relevant news found this run. No trades considered.")
    else:
        # Step 2: ask Gemini for decisions
        trades = get_trade_decisions(candidates, account)
        log_lines.append(f"Gemini recommended {len(trades)} trade(s).")

        # Step 3: submit real (paper) orders
        for trade in trades:
            result = execute_trade(trade, account)
            log_lines.append(f"  -> {json.dumps(result)}")

    log_lines.append("")

    log_path = os.path.join(LOG_DIR, f"{timestamp.strftime('%Y-%m-%d')}.log")
    with open(log_path, "a") as f:
        f.write("\n".join(log_lines) + "\n")

    print("\n".join(log_lines))


if __name__ == "__main__":
    run()
