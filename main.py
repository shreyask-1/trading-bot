import time
from trader import (
    get_account_snapshot,
    check_stop_loss_take_profit,
    check_position_caps,
    execute_trade
)

def mock_scan_top_candidates():
    """Mock scanner returning potential ticker candidates with sample snapshots."""
    return [
        {
            "ticker": "AAPL",
            "score": 88,
            "snapshot": {"recent_trend": "bullish", "volatility": "low"}
        },
        {
            "ticker": "MSFT",
            "score": 82,
            "snapshot": {"recent_trend": "stable", "volatility": "low"}
        }
    ]

def get_ticker_news(ticker):
    """Mock news retrieval function for pipeline validation."""
    return f"Recent institutional reports highlight strong product adoption and positive revenue metrics for {ticker}."

def evaluate_trade_candidate(ticker, news, snapshot, account):
    """Mock Gemini validation agent evaluation output."""
    # In production, replace this with direct Google GenAI calls analyzing the news and snapshot parameters
    return {
        "approve": True,
        "action": "BUY",
        "reasoning": f"Positive catalyst metrics confirmed via news feed for {ticker}. Market setup looks favorable."
    }

def run_trading_pipeline():
    print("==================================================")
    print(" Starting Automated Trading Pipeline Cycle")
    print("==================================================")

    # Fetch initial system account snapshot
    account = get_account_snapshot()
    print(f"Account Balance -- Total Value: ${account['total_value']:,.2f} | Cash: ${account['cash']:,.2f}")

    # --- [Stage 1] Risk Management & Position Cap Enforcement ---
    print("\n--- [Stage 1] Risk Management Checks ---")
    risk_trades = check_stop_loss_take_profit()
    cap_trades = check_position_caps()
    
    all_risk_trades = risk_trades + cap_trades
    executed_risk_trades = []

    if all_risk_trades:
        print(f"Triggered {len(all_risk_trades)} risk-mitigation / position adjustment actions.")
        for trade in all_risk_trades:
            res = execute_trade(trade, account)
            executed_risk_trades.append(res)
        
        # Refresh account info post-risk trades
        account = get_account_snapshot()
    else:
        print("All active positions are within healthy parameters. No risk actions triggered.")

    # --- [Stage 2 & 3] Candidate Sourcing & Filtering ---
    print("\n--- [Stage 2 & 3] Market Candidate Sourcing ---")
    top_candidates = mock_scan_top_candidates()
    print(f"Identified {len(top_candidates)} top candidate assets for pipeline analysis.")

    # --- [Stage 4] Gemini News Validation & Execution ---
    print("\n--- [Stage 4] Gemini News Catalyst & Trade Veto Agent ---")
    for candidate in top_candidates:
        ticker = candidate["ticker"]
        snapshot = candidate["snapshot"]

        try:
            # Dynamically refresh account snapshot per iteration to protect cash availability
            account = get_account_snapshot()
            
            news = get_ticker_news(ticker)
            decision = evaluate_trade_candidate(ticker, news, snapshot, account)

            print(f"\nCandidate: {ticker} | Signal Score: {candidate['score']}")
            print(f"Gemini Decision: Approve={decision.get('approve')} | Action={decision.get('action')}")
            print(f"Reasoning: {decision.get('reasoning')}")

            if decision.get("approve") and decision.get("action") == "BUY":
                # Allocate 5% of total portfolio value per approved trade
                allocation = account.get("total_value", 100000.0) * 0.05
                trade_payload = {
                    "ticker": ticker,
                    "action": "buy",
                    "dollar_amount": allocation,
                    "reasoning": decision.get("reasoning", "Approved by Gemini validation framework"),
                }
                res = execute_trade(trade_payload, account)
                print(f"Trade Execution Result ({ticker}): {res.get('status')}")

        except Exception as e:
            print(f"[ERROR] Failed evaluation loop or execution for {ticker}: {e}")
            continue

    print("\n==================================================")
    print(" Pipeline Cycle Completed Successfully")
    print("==================================================")

if __name__ == "__main__":
    run_trading_pipeline()
