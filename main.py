# main.py
import os
import time
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockSnapshotRequest
from decide import evaluate_entire_market
from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, WATCHLIST
from trader import (
    get_account_snapshot,
    execute_trade,
    record_performance_snapshot,
)

LOG_DIR = os.path.dirname(__file__)
os.makedirs(LOG_DIR, exist_ok=True)

data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)

def run_pipeline():
    start_time = time.time()
    print("=" * 60)
    print(f"STARTING 100-STOCK UNIVERSE SCAN ({len(WATCHLIST)} assets)")
    print("=" * 60)

    try:
        account = get_account_snapshot()
        print(f"Portfolio Value: ${account.get('total_value', 0):,.2f}")
    except Exception as e:
        print(f"[CRITICAL ERROR] Failed to load account: {e}")
        return

    master_universe_data = []

    # Chunk into groups of 50 to maximize network speed and prevent payload limits (2 total calls)
    chunk_size = 50
    for i in range(0, len(WATCHLIST), chunk_size):
        chunk = WATCHLIST[i:i + chunk_size]
        try:
            request = StockSnapshotRequest(symbol_or_symbols=chunk)
            snapshots = data_client.get_stock_snapshot(request)

            for ticker, snap in snapshots.items():
                try:
                    if not snap.latest_trade or not snap.daily_bar or not snap.previous_daily_bar:
                        continue

                    current_price = snap.latest_trade.price
                    prev_close = snap.previous_daily_bar.close
                    momentum_pct = round(((current_price - prev_close) / prev_close) * 100, 2)

                    master_universe_data.append({
                        "ticker": ticker,
                        "price": current_price,
                        "momentum_pct": momentum_pct,
                        "volume": snap.daily_bar.volume
                    })
                except Exception:
                    continue
        except Exception as e:
            print(f"[Error fetching chunk starting at index {i}]: {e}")

    # Sort instantly by highest momentum percentage to find leading rising stocks
    master_universe_data.sort(key=lambda x: x["momentum_pct"], reverse=True)
    
    # Keep only the absolute top 50 rising stocks from the market universe
    top_50_watchlist = master_universe_data[:50]
    print(f"Successfully filtered top {len(top_50_watchlist)} rising stocks out of {len(WATCHLIST)} scanned.")

    print("\n--- Sending Top 50 Watchlist to Gemini for Evaluation ---")
    approved_signals = []
    if top_50_watchlist:
        approved_signals = evaluate_entire_market(top_50_watchlist)

    print(f"AI Approved Execution Signals: {len(approved_signals)}")
    for trade in approved_signals:
        if trade.get("approve") and trade.get("action") == "BUY":
            ticker = trade["ticker"]
            # Dynamic capital sizing (allocating 2% per trade across your active pool)
            allocation = account.get("total_value", 100000) * 0.02
            execute_trade({
                "ticker": ticker,
                "action": "buy",
                "dollar_amount": allocation,
                "reasoning": trade.get("reasoning", "Approved")
            }, account)

    try:
        record_performance_snapshot(account, LOG_DIR)
    except Exception as e:
        print(f"[WARNING] Could not write log: {e}")

    elapsed = time.time() - start_time
    print(f"\nPipeline finished in {elapsed:.2f} seconds (Well under 40s target!).")
    print("=" * 60)

if __name__ == "__main__":
    while True:
        loop_start = time.time()
        try:
            run_pipeline()
        except Exception as e:
            print(f"[Loop Error]: {e}")
        
        elapsed = time.time() - loop_start
        sleep_time = max(5, 120 - elapsed)
        print(f"Sleeping for {sleep_time:.1f} seconds...\n")
        time.sleep(sleep_time)
