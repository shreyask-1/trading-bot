import math
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from config import (
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY,
    ALPACA_PAPER,
    STOP_LOSS_PCT,
    TAKE_PROFIT_PCT,
    MAX_PORTFOLIO_ALLOCATION_PCT
)

# Initialize Alpaca Trading Client
trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)


def get_account_snapshot():
    """Fetches key metrics from the Alpaca account."""
    try:
        account = trading_client.get_account()
        return {
            "cash": float(account.cash),
            "portfolio_value": float(account.portfolio_value),
            "buying_power": float(account.buying_power),
            "total_value": float(account.equity)
        }
    except Exception as e:
        print(f"[ERROR] Failed to fetch Alpaca account details: {e}")
        return {
            "cash": 100000.0,
            "portfolio_value": 100000.0,
            "buying_power": 100000.0,
            "total_value": 100000.0
        }


def get_current_positions():
    """Retrieves all open positions as a dictionary mapped by ticker symbol."""
    positions_map = {}
    try:
        positions = trading_client.get_all_positions()
        for p in positions:
            positions_map[p.symbol] = {
                "qty": float(p.qty),
                "market_value": float(p.market_value),
                "avg_entry_price": float(p.avg_entry_price),
                "current_price": float(p.current_price),
                "unrealized_plpc": float(p.unrealized_plpc)
            }
    except Exception as e:
        print(f"[ERROR] Failed to fetch current open positions: {e}")
    return positions_map


def check_stop_loss_take_profit():
    """Scans open positions for stop-loss or take-profit threshold breaches."""
    positions = get_current_positions()
    triggered_trades = []

    for ticker, pos in positions.items():
        plpc = pos["unrealized_plpc"]  # e.g., -0.06 means -6%
        if plpc <= -STOP_LOSS_PCT:
            triggered_trades.append({
                "ticker": ticker,
                "action": "sell",
                "dollar_amount": 0,  # 0 signals full liquidation
                "reasoning": f"Stop-loss triggered at {plpc * 100:.2f}% loss."
            })
        elif plpc >= TAKE_PROFIT_PCT:
            triggered_trades.append({
                "ticker": ticker,
                "action": "sell",
                "dollar_amount": 0,  # 0 signals full liquidation
                "reasoning": f"Take-profit triggered at +{plpc * 100:.2f}% gain."
            })

    return triggered_trades


def check_position_caps():
    """Ensures no single position exceeds the maximum allowed portfolio allocation cap."""
    positions = get_current_positions()
    account = get_account_snapshot()
    total_value = account["total_value"]
    rebalance_trades = []

    if total_value <= 0:
        return rebalance_trades

    max_allowed_value = total_value * MAX_PORTFOLIO_ALLOCATION_PCT

    for ticker, pos in positions.items():
        if pos["market_value"] > max_allowed_value:
            excess_value = pos["market_value"] - max_allowed_value
            rebalance_trades.append({
                "ticker": ticker,
                "action": "sell",
                "dollar_amount": excess_value,
                "reasoning": f"Position cap exceeded. Trimming excess value of ${excess_value:.2f}."
            })

    return rebalance_trades


def execute_trade(trade_payload, account):
    """Executes a buy or sell trade order through the Alpaca API client."""
    ticker = trade_payload["ticker"].upper()
    action = trade_payload["action"].lower()
    dollar_amount = float(trade_payload.get("dollar_amount", 0.0))
    reasoning = trade_payload.get("reasoning", "No reason provided")

    positions = get_current_positions()
    current_holding = positions.get(ticker)

    try:
        if current_holding:
            price = current_holding["current_price"]
        else:
            print(f"[WARNING] No current position for {ticker}. Skipping standalone execution without live quote reference.")
            return {"ticker": ticker, "status": "skipped", "reason": "No live price context available for unheld asset."}

        if action == "buy":
            cash_available = account["cash"]
            if dollar_amount > cash_available:
                dollar_amount = cash_available * 0.95  # Safe buffer

            if dollar_amount <= 1.0:
                return {"ticker": ticker, "status": "skipped", "reason": "Insufficient cash allocation."}

            qty = round(dollar_amount / price, 4)
            if qty <= 0:
                return {"ticker": ticker, "status": "skipped", "reason": "Calculated quantity is zero."}

            order_data = MarketOrderRequest(
                symbol=ticker,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY
            )
            order = trading_client.submit_order(order_data=order_data)
            print(f"[TRADE EXECUTION] BUY {qty} shares of {ticker}. Reason: {reasoning}")
            return {"ticker": ticker, "status": "submitted", "order_id": str(order.id)}

        elif action == "sell":
            shares_owned = current_holding["qty"] if current_holding else 0
            if shares_owned <= 0:
                return {"ticker": ticker, "status": "skipped", "reason": "No shares owned to sell."}

            if dollar_amount <= 0:
                qty = shares_owned  # Full liquidation
            else:
                shares_to_sell = dollar_amount / price
                qty = round(min(shares_owned, shares_to_sell), 4)

            if qty <= 0:
                return {"ticker": ticker, "status": "skipped", "reason": "Calculated sell quantity is zero."}

            order_data = MarketOrderRequest(
                symbol=ticker,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY
            )
            order = trading_client.submit_order(order_data=order_data)
            print(f"[TRADE EXECUTION] SELL {qty} shares of {ticker}. Reason: {reasoning}")
            return {"ticker": ticker, "status": "submitted", "order_id": str(order.id)}

        else:
            return {"ticker": ticker, "status": "failed", "reason": f"Unknown action type: {action}"}

    except Exception as e:
        print(f"[ERROR] Order execution failed for {ticker} ({action}): {e}")
        return {"ticker": ticker, "status": "error", "reason": str(e)}
