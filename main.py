import os
import time
from datetime import datetime, timedelta, timezone
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed
from sp500_data import get_sp500_tickers
from signal_score import calculate_signal_score
from decide import evaluate_entire_market
from config import ALPACA_API_KEY, ALPACA_SECRET_KEY
from trader import (
    get_account_snapshot,
    execute_trade,
    record_performance_snapshot,
)

LOG_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(LOG_DIR, exist_ok=True)

data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)

def run_pipeline():
    start_time = time.time()
    print("=" * 60)
    print("STARTING BULK 500-STOCK MARKET SCAN")
    print("=" * 60)

    try:
        account = get_account_snapshot()
        print(f"Portfolio Value: ${account.get('total_value', 0):,.2f}")
    except Exception as e:
        print(f"[CRITICAL ERROR] Failed to load account: {e}")
        return

    print("\n--- Fetching All 500+ Tickers in Bulk ---")
    try:
        tickers = get_sp500_tickers()
    except Exception:
        tickers = ["AAPL", "MSFT", "NVDA", "SPY", "TSLA", "AMZN"]

    master_universe_data = []
    
    # BULK FETCH: Query all symbols at once instead of looping individually
    try:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=40)
        
        # Alpaca allows bulk multi-symbol requests
        request = StockBarsRequest(
            symbol_or_symbols=tickers,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            feed=DataFeed.IEX,
        )
        all_bars = data_client.get_stock_bars(request)
        
        for ticker in tickers:
            try:
                symbol_bars = all_bars[ticker]
                if not symbol_bars or len(symbol_bars) < 10:
                    continue
                
                closes = [b.close for b in symbol_bars]
                current_price = closes[-1]
                
                # Fast indicator stub for the bulk payload
                snapshot = {
                    "ticker": ticker,
                    "price": current_price,
                    "momentum_pct": round(((current_price - closes[0]) / closes[0]) * 100, 2),
                    "volume": symbol_bars[-1].volume
                }
                
                master_universe_data.append(snapshot)
            except Exception:
                continue
                
    except Exception as e:
        print(f"[Error fetching bulk data]: {e}")

    print(f"Successfully processed bulk data for {len(master_universe_data)} stocks.")

    print("\n--- Sending Full Universe to Gemini ---")
    approved_signals = []
    if master_universe_data:
        approved_signals = evaluate_entire_market(master_universe_data)

    print(f"Gemini Approved Trades: {len(approved_signals)}")
    for trade in approved_signals:
        if trade.get("approve") and trade.get("action") == "BUY":
            ticker = trade["ticker"]
            allocation = account.get("total_value", 100000) * 0.05
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
    print(f"\nPipeline finished in {elapsed:.2f} seconds.")
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
