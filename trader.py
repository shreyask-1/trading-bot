"""
Talks to your real Alpaca PAPER TRADING account (fake money, real broker
infrastructure, real order execution logic). No live/real money is ever
touched as long as ALPACA_BASE_URL stays pointed at the paper endpoint.
"""

from datetime import datetime
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest

from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, MAX_POSITION_PCT

# paper=True routes every request to Alpaca's paper trading environment --
# this is the setting that guarantees no real money is ever involved.
trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)


def get_price(ticker):
    """Get the current real market price for a ticker via Alpaca's data API."""
    try:
        request = StockLatestTradeRequest(symbol_or_symbols=ticker)
        trade = data_client.get_stock_latest_trade(request)
        return float(trade[ticker].price)
    except Exception as e:
        print(f"Could not get price for {ticker}: {e}")
        return None


def get_account_snapshot():
    """
    Pull your real (paper) account state from Alpaca: cash, total portfolio
    value, and current holdings. This replaces the old portfolio.json file --
    Alpaca itself is now the source of truth for your balance and positions.
    """
    account = trading_client.get_account()
    positions = trading_client.get_all_positions()

    holdings = {p.symbol: float(p.qty) for p in positions}

    return {
        "cash": float(account.cash),
        "total_value": float(account.portfolio_value),
        "holdings": holdings,
    }


def execute_trade(trade, account_snapshot=None):
    """
    trade: {"ticker": "AAPL", "action": "buy"/"sell", "dollar_amount": 5000, "reasoning": "..."}
    Submits a real market order to your Alpaca PAPER account.
    Returns a log entry dict describing what was submitted.
    """
    ticker = trade["ticker"]
    action = trade["action"].lower()
    dollar_amount = trade.get("dollar_amount", 0)

    if account_snapshot is None:
        account_snapshot = get_account_snapshot()

    price = get_price(ticker)
    if price is None:
        return {"ticker": ticker, "status": "failed", "reason": "no price data"}

    total_value = account_snapshot["total_value"]
    max_allowed = total_value * MAX_POSITION_PCT
    current_position_value = account_snapshot["holdings"].get(ticker, 0) * price

    if action == "buy":
        amount = min(dollar_amount, max_allowed - current_position_value, account_snapshot["cash"])
        if amount <= 0:
            return {"ticker": ticker, "status": "skipped", "reason": "position cap or insufficient cash"}

        qty = round(amount / price, 4)
        if qty <= 0:
            return {"ticker": ticker, "status": "skipped", "reason": "calculated quantity too small"}

        side = OrderSide.BUY

    elif action == "sell":
        shares_owned = account_snapshot["holdings"].get(ticker, 0)
        if shares_owned <= 0:
            return {"ticker": ticker, "status": "skipped", "reason": "no shares owned"}

        qty = round(min(shares_owned, dollar_amount / price) if dollar_amount else shares_owned, 4)
        side = OrderSide.SELL

    else:
        return {"ticker": ticker, "status": "failed", "reason": f"unknown action '{action}'"}

    try:
        order_request = MarketOrderRequest(
            symbol=ticker,
            qty=qty,
            side=side,
            time_in_force=TimeInForce.DAY,
        )
        order = trading_client.submit_order(order_request)

        return {
            "timestamp": datetime.now().isoformat(),
            "ticker": ticker,
            "action": action,
            "qty": qty,
            "order_id": str(order.id),
            "order_status": str(order.status),
            "reasoning": trade.get("reasoning", ""),
            "status": "submitted",
        }
    except Exception as e:
        return {"ticker": ticker, "status": "failed", "reason": str(e)}
