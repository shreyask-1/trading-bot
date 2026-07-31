"""
Talks to your real Alpaca PAPER TRADING account (fake money, real broker
infrastructure, real order execution logic). No live/real money is ever
touched as long as paper=True stays set below.

Also computes the full technical indicator set (via indicators.py) from a
single price-history fetch per ticker, evaluates the broad market regime
(via market_regime.py, using SPY as a proxy), and enforces ATR-based
stop-loss/take-profit, a per-ticker trade cooldown, a minimum cash
reserve, a minimum trade size, and a cap on total open positions.
"""

import os
import json
import csv
from datetime import datetime, timedelta

import pytz
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

from config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, MAX_POSITION_PCT,
    ATR_STOP_MULTIPLIER, ATR_TAKE_PROFIT_MULTIPLIER, ATR_PERIOD,
    PRICE_HISTORY_DAYS, TRADE_COOLDOWN_MINUTES, MARKET_HIGH_VOLATILITY_THRESHOLD,
    ALPACA_DATA_FEED, MIN_CASH_RESERVE_PCT, MIN_TRADE_DOLLAR_AMOUNT, MAX_OPEN_POSITIONS,
)
import indicators as ind
from market_regime import evaluate_market_regime

trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)

COOLDOWN_FILE = os.path.join(os.path.dirname(__file__), "logs", "cooldowns.json")

_FEED_MAP = {"iex": DataFeed.IEX, "sip": DataFeed.SIP, "otc": DataFeed.OTC}
DATA_FEED = _FEED_MAP.get(ALPACA_DATA_FEED.lower(), DataFeed.IEX)

_EASTERN = pytz.timezone("America/New_York")

# Column order for logs/performance.csv. If a pre-existing file has a
# different header (e.g. from an older version of the bot), it's archived
# rather than appended to under a mismatched schema -- see
# record_performance_snapshot().
PERFORMANCE_CSV_HEADER = [
    "timestamp", "total_value", "cash", "num_holdings",
    "market_regime", "size_multiplier",
    "candidates_considered", "candidates_passed_prescreen",
    "trades_proposed", "trades_executed", "trades_skipped", "trades_failed",
    "risk_exits",
]


# ============================================================
# Time / market clock / regime
# ============================================================

def get_eastern_time_str():
    """
    Explicit US-Eastern-time string, computed from timezone-AWARE UTC (not
    from datetime.now(), which depends on whatever timezone the host
    machine happens to be set to). Used purely for unambiguous logging --
    NYSE hours are defined in Eastern time, and a naive local timestamp
    printed elsewhere in the logs can otherwise look confusing (e.g. a
    log labeled "06:28" is meaningless without knowing which timezone
    produced it).
    """
    now_utc = datetime.now(pytz.utc)
    now_et = now_utc.astimezone(_EASTERN)
    return now_et.strftime("%Y-%m-%d %I:%M %p %Z")


def is_market_open():
    """
    True if the market is open for regular trading right now, per Alpaca's
    own authoritative clock (not derived from local system time in any
    way). Fails closed: if the clock call errors, returns False. This is
    purely informational for logging now -- it does not gate whether the
    bot runs or trades; orders submitted while closed simply queue at
    Alpaca for the next open.
    """
    try:
        return bool(trading_client.get_clock().is_open)
    except Exception as e:
        print(f"Could not fetch market clock, assuming closed: {e}")
        return False


def get_market_regime():
    """
    Evaluates the broad market regime from SPY's own price history (see
    market_regime.py). Used to scale down (or fully block) new position
    sizing when the broad market is unfavorable or unusually volatile --
    independent of what Gemini decides about any individual ticker.

    Fails safe to "NEUTRAL" (a moderate, not maximal, sizing multiplier)
    if SPY's history can't be fetched or evaluated, rather than either
    fully blocking or fully allowing new trades based on missing data.
    """
    history = get_price_history("SPY")
    if history is None:
        print("Could not fetch SPY history for market regime check, defaulting to NEUTRAL.")
        return "NEUTRAL"
    try:
        return evaluate_market_regime(history["closes"], high_vol_threshold=MARKET_HIGH_VOLATILITY_THRESHOLD)
    except Exception as e:
        print(f"Market regime evaluation failed, defaulting to NEUTRAL: {e}")
        return "NEUTRAL"


# ============================================================
# Price data
# ============================================================

def get_price(ticker):
    """Current real market price for a ticker."""
    try:
        request = StockLatestTradeRequest(symbol_or_symbols=ticker, feed=DATA_FEED)
        trade = data_client.get_stock_latest_trade(request)
        return float(trade[ticker].price)
    except Exception as e:
        print(f"Could not get price for {ticker}: {e}")
        return None


def get_price_history(ticker, days=PRICE_HISTORY_DAYS):
    """
    Fetches daily OHLCV history ONCE per ticker and returns plain lists,
    oldest-first, ready to feed into any indicators.py function. Returns
    None if there isn't enough data.
    """
    try:
        end = datetime.now()
        start = end - timedelta(days=days + 10)  # pad for weekends/holidays
        request = StockBarsRequest(
            symbol_or_symbols=ticker, timeframe=TimeFrame.Day,
            start=start, end=end, feed=DATA_FEED,
        )
        bars = list(data_client.get_stock_bars(request)[ticker])
        if len(bars) < 55:
            return None
        return {
            "closes": [b.close for b in bars],
            "highs": [b.high for b in bars],
            "lows": [b.low for b in bars],
            "volumes": [b.volume for b in bars],
        }
    except Exception as e:
        print(f"Could not get price history for {ticker}: {e}")
        return None


def get_full_indicators(ticker):
    """
    Computes the complete indicator set for a ticker from a single history
    fetch. Returns a dict; any indicator that couldn't be computed (not
    enough history) is None rather than missing.
    """
    history = get_price_history(ticker)
    if history is None:
        return None

    closes, highs, lows, volumes = history["closes"], history["highs"], history["lows"], history["volumes"]

    macd = ind.compute_macd(closes)
    bb = ind.compute_bollinger_bands(closes)
    stoch = ind.compute_stochastic(highs, lows, closes)

    return {
        "price": closes[-1],
        "sma_20": ind.compute_sma(closes, 20),
        "sma_50": ind.compute_sma(closes, 50),
        "rsi_14": ind.compute_rsi(closes),
        "atr_14": ind.compute_atr(highs, lows, closes, period=ATR_PERIOD),
        "adx_14": ind.compute_adx(highs, lows, closes),
        "macd": macd,
        "bollinger": bb,
        "stochastic": stoch,
        "momentum_10d": ind.compute_momentum(closes, period=10),
        "volatility_20d": ind.compute_volatility(closes, period=20),
        "volume_trend": ind.compute_volume_trend(volumes),
        "relative_volume_pct": ind.compute_relative_volume(volumes),
        "trend": ind.classify_trend(closes),
    }


# ============================================================
# Account state
# ============================================================

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
        return {o.symbol for o in trading_client.get_orders(request)}
    except Exception as e:
        print(f"Could not fetch open orders: {e}")
        return set()


# ============================================================
# Cooldown tracking
# ============================================================

def _load_cooldowns():
    if not os.path.exists(COOLDOWN_FILE):
        return {}
    try:
        with open(COOLDOWN_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cooldowns(cooldowns):
    os.makedirs(os.path.dirname(COOLDOWN_FILE), exist_ok=True)
    with open(COOLDOWN_FILE, "w") as f:
        json.dump(cooldowns, f)


def get_tickers_on_cooldown():
    """
    Returns the set of tickers traded within the last TRADE_COOLDOWN_MINUTES.
    Used to stop Gemini from churning the same ticker repeatedly -- NOT used
    to gate the ATR stop-loss/take-profit check, which must always be able
    to force an exit regardless of cooldown.
    """
    cooldowns = _load_cooldowns()
    cutoff = datetime.now() - timedelta(minutes=TRADE_COOLDOWN_MINUTES)
    return {t for t, ts in cooldowns.items() if datetime.fromisoformat(ts) > cutoff}


def _record_cooldown(ticker):
    cooldowns = _load_cooldowns()
    cooldowns[ticker] = datetime.now().isoformat()
    _save_cooldowns(cooldowns)


# ============================================================
# ATR-based risk management
# ============================================================

def check_atr_stop_take_profit(account_snapshot):
    """
    For every holding, computes an ATR-based stop-loss and take-profit
    level from its average entry price and current ATR, and force-sells
    if either is breached. Independent of what Gemini decides that run,
    and independent of market regime (an exit is always allowed).

    Deliberately ignores the trade cooldown: a position opened moments ago
    is exactly the one most in need of its stop-loss staying active. Only
    skips tickers with an already-open order, to avoid duplicate exits.
    """
    results = []
    open_order_tickers = get_tickers_with_open_orders()

    for ticker, pos in account_snapshot["holdings"].items():
        if ticker in open_order_tickers:
            continue

        indicators_data = get_full_indicators(ticker)
        atr = indicators_data["atr_14"] if indicators_data else None
        if atr is None:
            continue  # can't compute a data-driven stop without ATR; skip rather than guess

        entry = pos["avg_entry_price"]
        current = pos["current_price"]
        stop_level = entry - (ATR_STOP_MULTIPLIER * atr)
        target_level = entry + (ATR_TAKE_PROFIT_MULTIPLIER * atr)

        reason = None
        if current <= stop_level:
            reason = f"ATR stop-loss hit (price {current} <= stop {round(stop_level, 2)}, ATR {atr})"
        elif current >= target_level:
            reason = f"ATR take-profit hit (price {current} >= target {round(target_level, 2)}, ATR {atr})"

        if reason:
            trade = {"ticker": ticker, "action": "sell", "dollar_amount": 0, "reasoning": reason, "conviction": 10}
            result = execute_trade(trade, account_snapshot)
            result["trigger"] = "risk_management"
            results.append(result)

    return results


# ============================================================
# Order execution (conviction-scaled, regime-scaled, cash- and
# sprawl-aware sizing)
# ============================================================

def execute_trade(trade, account_snapshot=None, size_multiplier=1.0):
    """
    trade: {"ticker", "action", "dollar_amount", "reasoning", "conviction" (1-10)}

    Buy sizing, in order of constraints applied:
      1. conviction/10 and the market-regime size_multiplier scale the
         MAX_POSITION_PCT cap (0.0 regime multiplier blocks all buys).
      2. MIN_CASH_RESERVE_PCT of total portfolio value is never spendable.
      3. MAX_OPEN_POSITIONS blocks opening a BRAND NEW ticker (adds to an
         existing holding are unaffected) once the cap is reached.
      4. MIN_TRADE_DOLLAR_AMOUNT -- anything smaller than this is skipped
         rather than executed as a dust trade.
    None of these apply to sells: an exit is always allowed regardless of
    size, cash reserve, or position count, so the bot can always clean up
    or de-risk.
    """
    ticker = trade["ticker"]
    action = trade["action"].lower()
    requested_amount = trade.get("dollar_amount", 0)
    conviction = max(1, min(10, trade.get("conviction", 5)))

    if account_snapshot is None:
        account_snapshot = get_account_snapshot()

    price = get_price(ticker)
    if price is None:
        return {"ticker": ticker, "status": "failed", "reason": "no price data"}

    total_value = account_snapshot["total_value"]
    current_holding = account_snapshot["holdings"].get(ticker)
    current_position_value = (current_holding["qty"] * price) if current_holding else 0

    if action == "buy":
        is_new_position = current_holding is None
        if is_new_position and len(account_snapshot["holdings"]) >= MAX_OPEN_POSITIONS:
            return {
                "ticker": ticker, "status": "skipped",
                "reason": f"max open positions reached ({MAX_OPEN_POSITIONS} held); "
                          f"only adds to existing holdings or sells are allowed until it consolidates",
            }

        max_allowed = total_value * MAX_POSITION_PCT * (conviction / 10) * size_multiplier
        cash_reserve = total_value * MIN_CASH_RESERVE_PCT
        available_cash = max(0.0, account_snapshot["cash"] - cash_reserve)

        amount = min(requested_amount, max_allowed - current_position_value, available_cash)
        if amount < MIN_TRADE_DOLLAR_AMOUNT:
            return {
                "ticker": ticker, "status": "skipped",
                "reason": f"below minimum trade size (${MIN_TRADE_DOLLAR_AMOUNT}) after position cap, "
                          f"regime multiplier, and/or cash reserve (${cash_reserve:,.2f} kept uninvested, "
                          f"${account_snapshot['cash']:,.2f} cash on hand)",
            }
        qty = round(amount / price, 4)
        if qty <= 0:
            return {"ticker": ticker, "status": "skipped", "reason": "calculated quantity too small"}
        side = OrderSide.BUY

    elif action == "sell":
        shares_owned = current_holding["qty"] if current_holding else 0
        if shares_owned <= 0:
            return {"ticker": ticker, "status": "skipped", "reason": "no shares owned"}
        qty = round(min(shares_owned, requested_amount / price) if requested_amount else shares_owned, 4)
        if qty <= 0:
            return {"ticker": ticker, "status": "skipped", "reason": "calculated quantity too small"}
        side = OrderSide.SELL

    else:
        return {"ticker": ticker, "status": "failed", "reason": f"unknown action '{action}'"}

    try:
        order_request = MarketOrderRequest(symbol=ticker, qty=qty, side=side, time_in_force=TimeInForce.DAY)
        order = trading_client.submit_order(order_request)
        _record_cooldown(ticker)

        return {
            "timestamp": datetime.now().isoformat(),
            "ticker": ticker,
            "action": action,
            "qty": qty,
            "conviction": conviction,
            "size_multiplier": size_multiplier,
            "order_id": str(order.id),
            "order_status": str(order.status),
            "reasoning": trade.get("reasoning", ""),
            "status": "submitted",
        }
    except Exception as e:
        return {"ticker": ticker, "status": "failed", "reason": str(e)}


# ============================================================
# Performance logging
# ============================================================

def record_performance_snapshot(account_snapshot, log_dir, **stats):
    """
    Appends one row to logs/performance.csv per run. Extra keyword args
    (all optional) populate the regime/pre-screen/trade-outcome columns:
        market_regime, size_multiplier, candidates_considered,
        candidates_passed_prescreen, trades_proposed, trades_executed,
        trades_skipped, trades_failed, risk_exits

    If an existing performance.csv has an outdated header (e.g. from
    before these columns existed), it's archived to a timestamped
    "_legacy" file rather than appended to under a mismatched schema --
    silently misaligned columns would be worse than a clearly separate file.
    """
    path = os.path.join(log_dir, "performance.csv")
    file_exists = os.path.exists(path)

    if file_exists:
        with open(path) as f:
            first_line = f.readline().strip()
        existing_header = first_line.split(",") if first_line else []
        if existing_header != PERFORMANCE_CSV_HEADER:
            backup_path = os.path.join(
                log_dir, f"performance_legacy_{datetime.now().strftime('%Y%m%d%H%M%S')}.csv"
            )
            os.rename(path, backup_path)
            print(f"performance.csv had an outdated schema; archived old file to {backup_path}")
            file_exists = False

    row = {
        "timestamp": datetime.now().isoformat(),
        "total_value": round(account_snapshot["total_value"], 2),
        "cash": round(account_snapshot["cash"], 2),
        "num_holdings": len(account_snapshot["holdings"]),
        "market_regime": stats.get("market_regime", "UNKNOWN"),
        "size_multiplier": stats.get("size_multiplier", 1.0),
        "candidates_considered": stats.get("candidates_considered", 0),
        "candidates_passed_prescreen": stats.get("candidates_passed_prescreen", 0),
        "trades_proposed": stats.get("trades_proposed", 0),
        "trades_executed": stats.get("trades_executed", 0),
        "trades_skipped": stats.get("trades_skipped", 0),
        "trades_failed": stats.get("trades_failed", 0),
        "risk_exits": stats.get("risk_exits", 0),
    }

    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=PERFORMANCE_CSV_HEADER)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)
