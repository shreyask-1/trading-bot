"""
Master Pipeline Orchestrator.
Coordinates market regime analysis, candidate scoring, news validation via Gemini,
and trade execution via Alpaca.
"""

import os
import time
from sp500_data import get_sp500_tickers
from indicators import compute_sma
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


def run_pipeline():
    print("=" * 60)
    print("STARTING TRADING BOT PIPELINE EXECUTION")
    print("=" * 60)

    # Step 1: Account Snapshot
    account = get_account_snapshot()
    print(f"Portfolio Value: ${account['total_value']:,.2f} | Cash: ${account['cash']:,.2f}")

    # Step 2: Risk Management (Stop-loss / Take-profit / Caps)
    print("\n--- [Stage 1] Running Risk & Position Cap Checks ---")
    risk_trades = check_stop_loss_take_profit(account)
    cap_trades = check_position_caps(account)
    executed_risk_trades = risk_trades + cap_trades

    if executed_risk_trades:
        print(f"Executed {len(executed_risk_trades)} risk/cap trades.")
        # Refresh account after risk trades
        account = get_account_snapshot()
    else:
        print("No risk or position cap triggers met.")

    # Step 3: Fetch Universe & Evaluate Market Regime
    print("\n--- [Stage 2] Universe Screening & Macro Regime Check ---")
    tickers = get_sp500_tickers()
    print(f"Total Tickers In Universe: {len(tickers)}")

    spy_snapshot = get_indicator_snapshot("SPY")
    # Simple fallback structure for regime evaluation
    regime = "NEUTRAL"
    if spy_snapshot:
        # Generate dummy bars array from price for regime check if required
        regime = "BULLISH" if spy_snapshot["trend"] == "bullish" else "NEUTRAL"
    print(f"Current Market Regime: {regime}")

    # Step 4: Technical & Quantitative Candidate Scoring
    print("\n--- [Stage 3] Quantitative Signal Scoring ---")
    scored_candidates = []

    for ticker in tickers:
        snapshot = get_indicator_snapshot(ticker)
        if not snapshot:
            continue

        score = calculate_signal_score(snapshot)
        if score >= 50.0:  # Candidate score threshold
            scored_candidates.append({
                "ticker": ticker,
                "score": score,
                "snapshot": snapshot
            })

    # Sort candidates by quantitative signal score
    scored_candidates.sort(key=lambda x: x["score"], reverse=True)
    top_candidates = scored_candidates[:10]
    print(f"Top {len(top_candidates)} Quant Candidates Evaluated.")

    # Step 5: Gemini Validation & Veto
    print("\n--- [Stage 4] Gemini News Catalyst & Trade Veto Agent ---")
    for candidate in top_candidates:
        ticker = candidate["ticker"]
        snapshot = candidate["snapshot"]

        news = get_ticker_news(ticker)
        decision = evaluate_trade_candidate(ticker, news, snapshot, account)

        print(f"\nCandidate: {ticker} | Signal Score: {candidate['score']}")
        print(f"Gemini Decision: Approve={decision.get('approve')} | Action={decision.get('action')}")
        print(f"Reasoning: {decision.get('reasoning')}")

        if decision.get("approve") and decision.get("action") == "BUY":
            # Target cash allocation based on score
            allocation = account["total_value"] * 0.05  # 5% target size
            trade_payload = {
                "ticker": ticker,
                "action": "buy",
                "dollar_amount": allocation,
                "reasoning": decision.get("reasoning", "Approved by Gemini Veto Agent"),
            }
            res = execute_trade(trade_payload, account)
            print(f"Trade Execution Result ({ticker}): {res.get('status')}")

    # Step 6: Log Performance State
    record_performance_snapshot(account, LOG_DIR)
    print("\n=" * 60)
    print("PIPELINE EXECUTION COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    run_pipeline()
