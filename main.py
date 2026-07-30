"""
Master Pipeline Orchestrator (Bulletproof Multi-Stock Execution).
Runs market regime analysis, candidate scoring, dual news validation via Gemini,
and trade execution via Alpaca with full isolated exception handling.
"""

import os
import sys
import traceback
from sp500_data import get_sp500_tickers
from market_regime import evaluate_market_regime
from signal_score import calculate_signal_score
from news import get_ticker_news
from decide import evaluate_trade_candidate
from trader import (
    get_account_snapshot,
    get_indicator_snapshot,
    execute_trade,
    check_stop_loss_take_profit,
    check_position_caps,
    record_performance_snapshot,
)

LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOG_DIR, exist_ok=True)


def safe_get_snapshot(ticker):
    """Safely retrieves indicators without failing the whole pipeline."""
    try:
        return get_indicator_snapshot(ticker)
    except Exception as e:
        # Silently skip tickers that fail data fetching (e.g., restricted/delisted)
        return None


def run_pipeline():
    print("=" * 60)
    print("STARTING TRADING BOT PIPELINE EXECUTION")
    print("=" * 60)

    # Step 1: Account Snapshot
    try:
        account = get_account_snapshot()
        print(f"Portfolio Value: ${account.get('total_value', 0):,.2f} | Cash: ${account.get('cash', 0):,.2f}")
    except Exception as e:
        print(f"[CRITICAL ERROR] Failed to load Alpaca account snapshot: {e}")
        return

    # Step 2: Risk Management (Stop-loss / Take-profit / Caps)
    print("\n--- [Stage 1] Running Risk & Position Cap Checks ---")
    try:
        risk_trades = check_stop_loss_take_profit(account)
        cap_trades = check_position_caps(account)
        executed_risk_trades = (risk_trades or []) + (cap_trades or [])
        if executed_risk_trades:
            print(f"Executed {len(executed_risk_trades)} risk/cap trades.")
            account = get_account_snapshot()
        else:
            print("No risk or position cap triggers met.")
    except Exception as e:
        print(f"[WARNING] Risk management check encountered an error: {e}")

    # Step 3: Fetch Universe & Evaluate Market Regime
    print("\n--- [Stage 2] Universe Screening & Macro Regime Check ---")
    try:
        tickers = get_sp500_tickers()
        print(f"Total Tickers In Universe: {len(tickers)}")
    except Exception as e:
        print(f"[ERROR] Failed to fetch ticker list: {e}")
        tickers = ["AAPL", "MSFT", "NVDA", "SPCX", "MU", "PLTR"]

    regime = "NEUTRAL"
    try:
        spy_snapshot = safe_get_snapshot("SPY")
        if spy_snapshot and spy_snapshot.get("trend") == "bullish":
            regime = "BULLISH"
    except Exception:
        pass
    print(f"Current Market Regime: {regime}")

    # Step 4: Technical & Quantitative Candidate Scoring
    print("\n--- [Stage 3] Quantitative Signal Scoring ---")
    scored_candidates = []

    for ticker in tickers:
        snapshot = safe_get_snapshot(ticker)
        if not snapshot:
            continue

        try:
            score = calculate_signal_score(snapshot)
            if score >= 50.0:  # Threshold for qualifying candidates
                scored_candidates.append({
                    "ticker": ticker,
                    "score": score,
                    "snapshot": snapshot
                })
        except Exception as e:
            continue

    # Sort candidates by quantitative signal score
    scored_candidates.sort(key=lambda x: x["score"], reverse=True)
    top_candidates = scored_candidates[:10]
    print(f"Top {len(top_candidates)} Quant Candidates Selected For Gemini Veto.")

    # Step 5: Gemini News Validation & Execution
    print("\n--- [Stage 4] Gemini News Catalyst & Trade Veto Agent ---")
    for candidate in top_candidates:
        ticker = candidate["ticker"]
        snapshot = candidate["snapshot"]

        try:
            news = get_ticker_news(ticker)
            decision = evaluate_trade_candidate(ticker, news, snapshot, account)

            print(f"\nCandidate: {ticker} | Signal Score: {candidate['score']}")
            print(f"Gemini Decision: Approve={decision.get('approve')} | Action={decision.get('action')}")
            print(f"Reasoning: {decision.get('reasoning')}")

            if decision.get("approve") and decision.get("action") == "BUY":
                allocation = account.get("total_value", 100000) * 0.05  # 5% position
                trade_payload = {
                    "ticker": ticker,
                    "action": "buy",
                    "dollar_amount": allocation,
                    "reasoning": decision.get("reasoning", "Approved by Gemini Veto Agent"),
                }
                res = execute_trade(trade_payload, account)
                print(f"Trade Execution Result ({ticker}): {res.get('status')}")

        except Exception as e:
            print(f"[ERROR] Failed evaluation/execution for {ticker}: {e}")
            continue

    # Step 6: Log Performance State
    try:
        record_performance_snapshot(account, LOG_DIR)
    except Exception as e:
        print(f"[WARNING] Could not write performance log: {e}")

    print("\n=" * 60)
    print("PIPELINE EXECUTION COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    run_pipeline()
