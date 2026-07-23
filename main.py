"""
Real-time News Trading Bot - Listens to Finnhub WebSocket and reacts instantly.
"""

import json
import os
import time
import websocket
from datetime import datetime, timedelta
from collections import defaultdict

from config import FINNHUB_API_KEY, TRADE_COOLDOWN_MINUTES
from trader import (
    get_account_snapshot,
    check_stop_loss_take_profit,
    check_position_caps,
    execute_trade,
    record_performance_snapshot,
    get_recently_traded_tickers,
)
from news import get_news_candidates
from decide import get_trade_decisions

LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

last_trade_time = defaultdict(lambda: datetime.min)


def should_process_ticker(ticker):
    """Prevent spamming the same ticker too frequently."""
    cutoff = datetime.now() - timedelta(minutes=TRADE_COOLDOWN_MINUTES)
    return last_trade_time[ticker] < cutoff


def on_message(ws, message):
    try:
        data = json.loads(message)
        if data.get("type") != "news":
            return

        print(f"\n[NEWS RECEIVED] {datetime.now().strftime('%H:%M:%S')}")

        run_full_cycle()

    except Exception as e:
        print(f"Error in WebSocket handler: {e}")


def run_full_cycle():
    global last_trade_time
    timestamp = datetime.now()
    log_lines = [f"=== Real-time Run at {timestamp.isoformat()} ==="]

    account = get_account_snapshot()
    log_lines.append(f"Account Value: ${account['total_value']:,.2f} | Cash: ${account['cash']:,.2f}")

    # Hard risk management
    risk_sells = check_stop_loss_take_profit(account)
    if risk_sells:
        log_lines.append(f"→ Risk Management: {len(risk_sells)} sells triggered")
        account = get_account_snapshot()

    cap_trims = check_position_caps(account)
    if cap_trims:
        log_lines.append(f"→ Position Cap Enforcement: {len(cap_trims)} trims")
        account = get_account_snapshot()

    # Get fresh news
    candidates = get_news_candidates()
    log_lines.append(f"Found {len(candidates)} mentioned companies.")

    trades = get_trade_decisions(candidates, account)
    log_lines.append(f"Gemini recommended {len(trades)} trade(s).")

    for trade in trades:
        ticker = trade.get("ticker")
        if ticker and should_process_ticker(ticker):
            result = execute_trade(trade, account)
            log_lines.append(f"  → {json.dumps(result)}")
            if result.get("status") == "submitted":
                last_trade_time[ticker] = datetime.now()
        else:
            log_lines.append(f"  → Skipped {ticker} (cooldown active)")

    final_account = get_account_snapshot()
    record_performance_snapshot(final_account, LOG_DIR)
    log_lines.append(f"Ending Value: ${final_account['total_value']:,.2f}")

    print("\n".join(log_lines))

    # Save to log file
    with open(os.path.join(LOG_DIR, f"{timestamp.strftime('%Y-%m-%d')}.log"), "a") as f:
        f.write("\n".join(log_lines) + "\n\n")


def on_error(ws, error):
    print(f"WebSocket Error: {error}")


def on_close(ws, close_status_code, close_msg):
    print("WebSocket closed. Reconnecting in 10 seconds...")
    time.sleep(10)
    start_websocket()


def on_open(ws):
    print("✅ Connected to Finnhub Real-time News Stream")
    ws.send('{"type":"subscribe","symbol":"*"}')


def start_websocket():
    ws = websocket.WebSocketApp(
        f"wss://ws.finnhub.io?token={FINNHUB_API_KEY}",
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )
    ws.run_forever()


if __name__ == "__main__":
    print("🚀 Starting Real-Time News Trading Bot...")
    print("Listening for news 24/7. Trades will trigger immediately on fresh signals.")
    start_websocket()
