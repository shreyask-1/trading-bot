"""
Talks to your real Alpaca PAPER TRADING account (fake money, real broker
infrastructure). No live/real money is touched as long as paper=True stays set.
"""

from datetime import datetime, timedelta, timezone
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

from config import (
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY,
    MAX_POSITION_PCT,
    STOP_LOSS_PCT,
    TAKE_PROFIT_PCT,
    PRICE_HISTORY_DAYS,
    RSI_PERIOD,
    SMA_SHORT,
    SMA_LONG,
    VOLUME_LOOKBACK,
    TRADE_COOLDOWN_MINUTES,
)
from indicators import compute_sma, compute_rsi, compute_volume_trend, classify_trend

trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)


def get_price(ticker):
    try:
        request = StockLatestTradeRequest(symbol_or_symbols=ticker, feed=DataFeed.IEX)
        trade = data_client.get_stock_latest_trade(request)
        return float(trade[ticker].price)
    except Exception as e:
        print(f"Could not get price for {ticker}: {e}")
        return None


def get_indicator_snapshot(ticker):
    """
    Fetches ~120 calendar days of daily bars and computes momentum, RSI,
    SMA20, SMA50, volume trend, and a basic trend label. Uses the IEX feed
    since Alpaca's free plan doesn't include SIP data. Returns None if
    there isn't enough data.
    """
    try:
        end = datetime.now()
        start = end - timedelta(days=120)
        request = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            feed=DataFeed.IEX,
        )
        bars = data_client.get_stock_bars(request)
        symbol_bars = bars[ticker]
        if len(symbol_bars) < 10:
            return None

        closes = [b.close for b in symbol_bars]
        volumes = [b.volume for b in symbol_bars]
        current_price = closes[-1]

        momentum = None
        if len(closes) > PRICE_HISTORY_DAYS:
            past = closes[-(PRICE_HISTORY_DAYS + 1)]
            momentum = round(((current_price - past) / past) * 100, 2)

        sma20 = compute_sma(closes, SMA_SHORT)
        sma50 = compute_sma(closes, SMA_LONG)
        rsi = compute_rsi(closes, RSI_PERIOD)
        volume_trend = compute_volume_trend(volumes, VOLUME_LOOKBACK)
        trend = classify_trend(current_price, sma20, sma50)

        return {
            "price": current_price,
            "momentum_pct": momentum,
            "sma20": sma20,
            "sma50": sma50,
            "rsi": rsi,
            "volume_trend_pct": volume_trend,
            "trend": trend,
        }
    except Exception as e:
        print(f"Could not compute indicators for {ticker}: {e}")
        return None


def get_account_snapshot():
    account = trading_client.get_account()
    positions = trading_client.get_all_positions()

    holdings = {}
    for p in positions:
        holdings[p.symbol] = {
            "qty": float(p.qty),
            "avg_entry_price": float(p.avg_entry_price),
            "current_price": float(p.current_price),
            "unrealized_pl": float(p.unrealized_pl),
            "unrealized_plpc": round(float(p.unrealized_plpc) * 100, 2),
        }

    return {
        "cash": float(account.cash),
        "total_value": float(account.portfolio_value),
        "holdings": holdings,
    }


def get_tickers_with_open_orders():
    """Tickers with a currently pending (unfilled) order."""
    try:
        request = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        open_orders = trading_client.get_orders(request)
        return {o.symbol for o in open_orders}
    except Exception as e:
        print(f"Could not fetch open orders: {e}")
        return set()


def get_recently_traded_tickers(minutes=None):
    """
    Tickers with ANY order (filled or not) submitted within the last
    `minutes` -- the core double-trading fix. Checks Alpaca's own order
    history directly, so it works correctly even across separate GitHub
    Actions runs (which don't share local memory between runs).
    """
    if minutes is None:
        minutes = TRADE_COOLDOWN_MINUTES
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        request = GetOrdersRequest(status=QueryOrderStatus.ALL, after=cutoff, limit=200)
        orders = trading_client.get_orders(request)
        return {o.symbol for o in orders}
    except Exception as e:
        print(f"Could not fetch recent orders: {e}")
        return set()


def check_stop_loss_take_profit(account_snapshot):
    """
    Hard-coded risk management, independent of Gemini. Sells are always
    allowed regardless of cooldown -- protecting capital takes priority
    over the duplicate-trade guard.
    """
    results = []
    open_order_tickers = get_tickers_with_open_orders()

    for ticker, pos in account_snapshot["holdings"].items():
        if ticker in open_order_tickers:
            continue

        plpc = pos["unrealized_plpc"]
        reason = None
        if plpc <= STOP_LOSS_PCT:
            reason = f"stop-loss triggered ({plpc}% <= {STOP_LOSS_PCT}%)"
        elif plpc >= TAKE_PROFIT_PCT:
            reason = f"take-profit triggered ({plpc}% >= {TAKE_PROFIT_PCT}%)"

        if reason:
            trade = {"ticker": ticker, "action": "sell", "dollar_amount": 0, "reasoning": reason}
            result = execute_trade(trade, account_snapshot)
            result["trigger"] = "risk_management"
            results.append(result)

    return results


def check_position_caps(account_snapshot):
    """
    Hard-coded enforcement of MAX_POSITION_PCT, independent of Gemini.
    Previously this only existed as a prompt instruction Gemini could
    choose to follow or ignore -- this makes it automatic, the same way
    stop-loss/take-profit already are. Trims any position whose current
    value exceeds the cap down to exactly the cap, regardless of whether
    Gemini notices or mentions it this run.
    """
    results = []
    open_order_tickers = get_tickers_with_open_orders()
    total_value = account_snapshot["total_value"]
    max_allowed_value = total_value * MAX_POSITION_PCT

    for ticker, pos in account_snapshot["holdings"].items():
        if ticker in open_order_tickers:
            continue

        current_value = pos["qty"] * pos["current_price"]
        if current_value > max_allowed_value:
            excess_value = current_value - max_allowed_value
            trade = {
                "ticker": ticker,
                "action": "sell",
                "dollar_amount": excess_value,
                "reasoning": (
                    f"auto-trim: position (${current_value:,.2f}) exceeded "
                    f"{MAX_POSITION_PCT * 100:.0f}% cap (${max_allowed_value:,.2f})"
                ),
            }
            result = execute_trade(trade, account_snapshot)
            result["trigger"] = "position_cap"
            results.append(result)

    return results


def execute_trade(trade, account_snapshot=None):
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
    current_holding = account_snapshot["holdings"].get(ticker)
    current_position_value = (current_holding["qty"] * price) if current_holding else 0

    if action == "buy":
        amount = min(dollar_amount, max_allowed - current_position_value, account_snapshot["cash"])
        if amount <= 0:
            return {"ticker": ticker, "status": "skipped", "reason": "position cap or insufficient cash"}
        qty = round(amount / price, 4)
        if qty <= 0:
            return {"ticker": ticker, "status": "skipped", "reason": "calculated quantity too small"}
        side = OrderSide.BUY

    elif action == "sell":
        shares_owned = current_holding["qty"] if current_holding else 0
        if shares_owned <= 0:
            return {"ticker": ticker, "status": "skipped", "reason": "no shares owned"}
        qty = round(min(shares_owned, dollar_amount / price) if dollar_amount else shares_owned, 4)
        side = OrderSide.SELL

    else:
        return {"ticker": ticker, "status": "failed", "reason": f"unknown action '{action}'"}

    try:
        order_request = MarketOrderRequest(
            symbol=ticker, qty=qty, side=side, time_in_force=TimeInForce.DAY,
        )
        order = trading_client.submit_order(order_request)
        return {
            "timestamp": datetime.now().isoformat(),
            "ticker": ticker,
            "action": action,
            "qty": qty,
            "order_id": str(order.id),
            "order_status": str(order.status),
            "time_horizon": trade.get("time_horizon", "unspecified"),
            "reasoning": trade.get("reasoning", ""),
            "status": "submitted",
        }
    except Exception as e:
        return {"ticker": ticker, "status": "failed", "reason": str(e)}


def record_performance_snapshot(account_snapshot, log_dir):
    import csv, os
    path = os.path.join(log_dir, "performance.csv")
    file_exists = os.path.exists(path)
    with open(path, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp", "total_value", "cash", "num_holdings"])
        writer.writerow([
            datetime.now().isoformat(),
            round(account_snapshot["total_value"], 2),
            round(account_snapshot["cash"], 2),
            len(account_snapshot["holdings"]),
        ])
