import csv
import os
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
    MACD_FAST,
    MACD_SLOW,
    MACD_SIGNAL,
    BOLLINGER_PERIOD,
    BOLLINGER_STD,
    TRADE_COOLDOWN_MINUTES,
)
from indicators import (
    compute_sma,
    compute_rsi,
    compute_volume_trend,
    classify_trend,
    compute_macd,
    compute_bollinger_bands,
)

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
    try:
        end = datetime.now(timezone.utc)
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
        macd = compute_macd(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
        bollinger = compute_bollinger_bands(closes, BOLLINGER_PERIOD, BOLLINGER_STD)

        return {
            "price": current_price,
            "momentum_pct": momentum,
            "sma20": sma20,
            "sma50": sma50,
            "rsi": rsi,
            "volume_trend_pct": volume_trend,
            "trend": trend,
            "macd": macd,
            "bollinger": bollinger,
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
    try:
        request = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        open_orders = trading_client.get_orders(request)
        return {o.symbol for o in open_orders}
    except Exception as e:
        print(f"Could not fetch open orders: {e}")
        return set()


def check_cooldown_period(ticker, log_dir):
    """
    Checks if a ticker was traded recently within TRADE_COOLDOWN_MINUTES.
    Reads from the local trade history log.
    """
    trade_log_path = os.path.join(log_dir, "trades.csv")
    if not os.path.exists(trade_log_path):
        return False

    try:
        now = datetime.now(timezone.utc)
        with open(trade_log_path, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("ticker") == ticker:
                    t_str = row.get("timestamp")
                    if t_str:
                        trade_time = datetime.fromisoformat(t_str)
                        if trade_time.tzinfo is None:
                            trade_time = trade_time.replace(tzinfo=timezone.utc)
                        
                        age_minutes = (now - trade_time).total_seconds() / 60.0
                        if age_minutes < TRADE_COOLDOWN_MINUTES:
                            return True  # Still in cooldown
    except Exception as e:
        print(f"Error checking cooldown for {ticker}: {e}")

    return False


def log_trade_action(trade_record, log_dir):
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, "trades.csv")
    file_exists = os.path.exists(path)
    with open(path, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp", "ticker", "action", "qty", "status", "reasoning"])
        writer.writerow([
            trade_record.get("timestamp", datetime.now(timezone.utc).isoformat()),
            trade_record.get("ticker"),
            trade_record.get("action"),
            trade_record.get("qty"),
            trade_record.get("status"),
            trade_record.get("reasoning"),
        ])


def check_stop_loss_take_profit(account_snapshot):
    results = []
    open_order_tickers = get_tickers_with_open_orders()

    for ticker, pos in account_snapshot["holdings"].items():
        if ticker in open_order_tickers:
            continue

        plpc = pos["unrealized_plpc"]
        reason = None
        if plpc <= (STOP_LOSS_PCT * 100):
            reason = f"stop-loss triggered ({plpc}% <= {STOP_LOSS_PCT * 100}%)"
        elif plpc >= (TAKE_PROFIT_PCT * 100):
            reason = f"take-profit triggered ({plpc}% >= {TAKE_PROFIT_PCT * 100}%)"

        if reason:
            trade = {"ticker": ticker, "action": "sell", "dollar_amount": 0, "reasoning": reason}
            result = execute_trade(trade, account_snapshot)
            result["trigger"] = "risk_management"
            results.append(result)

    return results


def check_position_caps(account_snapshot):
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


def execute_trade(trade, account_snapshot=None, log_dir="data"):
    ticker = trade["ticker"]
    action = trade["action"].lower()
    dollar_amount = trade.get("dollar_amount", 0)

    if action in ("trim", "reduce", "exit", "close"):
        action = "sell"

    # Enforce cooldown on BUY actions
    if action == "buy" and check_cooldown_period(ticker, log_dir):
        return {"ticker": ticker, "status": "skipped", "reason": f"cooldown active ({TRADE_COOLDOWN_MINUTES} mins)"}

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
        
        if dollar_amount <= 0:
            qty = shares_owned
        else:
            shares_to_sell = dollar_amount / price
            qty = round(min(shares_owned, shares_to_sell), 4)
            
        side = OrderSide.SELL

    else:
        return {"ticker": ticker, "status": "failed", "reason": f"unknown action '{action}'"}

    try:
        order_request = MarketOrderRequest(
            symbol=ticker, qty=qty, side=side, time_in_force=TimeInForce.DAY
        )
        order = trading_client.submit_order(order_request)
        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "ticker": ticker,
            "action": action,
            "qty": qty,
            "order_id": str(order.id),
            "order_status": str(order.status),
            "time_horizon": trade.get("time_horizon", "unspecified"),
            "reasoning": trade.get("reasoning", ""),
            "status": "submitted",
        }
        log_trade_action(result, log_dir)
        return result
    except Exception as e:
        return {"ticker": ticker, "status": "failed", "reason": str(e)}


def record_performance_snapshot(account_snapshot, log_dir):
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, "performance.csv")
    file_exists = os.path.exists(path)
    with open(path, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp", "total_value", "cash", "num_holdings"])
        writer.writerow([
            datetime.now(timezone.utc).isoformat(),
            round(account_snapshot["total_value"], 2),
            round(account_snapshot["cash"], 2),
            len(account_snapshot["holdings"]),
        ])
