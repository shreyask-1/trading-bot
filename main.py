import os
import time
from sp500_data import get_sp500_tickers
from signal_score import calculate_signal_score
from decide import evaluate_entire_market
from trader import (
    get_account_snapshot,
    get_indicator_snapshot,
    execute_trade,
    check_stop_loss_take_profit,
    check_position_caps,
    record_performance_snapshot,
)

LOG_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(LOG_DIR, exist_ok=True)


def safe_get_snapshot(ticker):
    try:
        return get_indicator_snapshot(ticker)
    except Exception:
        return None


def run_pipeline():
    start_time = time.time()
    print("=" * 60)
    print("STARTING FAST MACRO-SCAN TRADING PIPELINE")
    print("=" * 60)

    try:
        account = get_account_snapshot()
        print(f"Portfolio Value: ${account.get('total_value', 0):,.2f} | Cash: ${account.get('cash', 0):,.2f}")
    except Exception as e:
        print(f"[CRITICAL ERROR] Failed to load Alpaca account snapshot: {e}")
        return

    print("\n--- [Stage 1] Running Risk & Position Cap Checks ---")
    try:
        risk_trades = check_stop_loss_take_profit(account)
        cap_trades = check_position_caps(account)
        if (risk_trades or []) + (cap_trades or []):
            account = get_account_snapshot()
    except Exception as e:
        print(f"[WARNING] Risk management check error: {e}")

    print("\n--- [Stage 2 & 3] Building Master Universe Data ---")
    try:
        tickers = get_sp500_tickers()
    except Exception:
        tickers = ["AAPL", "MSFT", "NVDA", "SPY", "MU", "PLTR", "TSLA", "AMZN"]

    master_universe_data = []
    for ticker in tickers:
        snapshot = safe_get_snapshot(ticker)
        if not snapshot:
            continue
        try:
            score = calculate_signal_score(snapshot)
            # Filter pre-screen candidates to keep payload relevant and clean
            if score >= 40.0:
                snapshot["ticker"] = ticker
                snapshot["signal_score"] = score
                master_universe_data.append(snapshot)
        except Exception:
            continue

    print(f"Collected indicators for {len(master_universe_data)} viable candidates.")

    print("\n--- [Stage 4] Single-Prompt Macro Gemini Sweep ---")
    approved_signals = []
    if master_universe_data:
        approved_signals = evaluate_entire_market(master_universe_data)

    print(f"Gemini Approved Trades: {len(approved_signals)}")

    for trade in approved_signals:
        if trade.get("approve") and trade.get("action") == "BUY":
            ticker = trade["ticker"]
            allocation = account.get("total_value", 100000) * 0.05
            trade_payload = {
                "ticker": ticker,
                "action": "buy",
                "dollar_amount": allocation,
                "reasoning": trade.get("reasoning", "Approved by Gemini Macro Scan"),
            }
            res = execute_trade(trade_payload, account)
            print(f"Trade Execution Result ({ticker}): {res.get('status')}")

    try:
        record_performance_snapshot(account, LOG_DIR)
    except Exception as e:
        print(f"[WARNING] Could not write performance log: {e}")

    elapsed = time.time() - start_time
    print(f"\nPipeline finished in {elapsed:.2f} seconds.")
    print("=" * 60)


if __name__ == "__main__":
    while True:
        loop_start = time.time()
        try:
            run_pipeline()
        except Exception as e:
            print(f"[Loop Error]: {e}")
        
        # Keep pace with your 2-minute cron rhythm safely
        elapsed = time.time() - loop_start
        sleep_time = max(5, 120 - elapsed)
        print(f"Sleeping for {sleep_time:.1f} seconds until next scan...\n")
        time.sleep(sleep_time)
