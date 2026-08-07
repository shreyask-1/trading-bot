"""
Talks to Alpaca PAPER TRADING account.
Handles position sizing, chart-based stops/targets, risk limits, and order submission.
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
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed

from config import (
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY,
    MAX_POSITION_PCT,
    ATR_STOP_MULTIPLIER,
    ATR_TAKE_PROFIT_MULTIPLIER,
    ATR_PERIOD,
    SWING_LOOKBACK_DAYS,
    MIN_STOP_DISTANCE_ATR_MULT,
    MAX_STOP_DISTANCE_ATR_MULT,
    MIN_TAKE_PROFIT_DISTANCE_ATR_MULT,
    MAX_TAKE_PROFIT_DISTANCE_ATR_MULT,
    ALLOW_GEMINI_CUSTOM_EXITS,
    ENABLE_INTRADAY_ANALYSIS,
    INTRADAY_BAR_MINUTES,
    INTRADAY_LOOKBACK_DAYS,
    PRICE_HISTORY_DAYS,
    TRADE_COOLDOWN_MINUTES,
    MARKET_HIGH_VOLATILITY_THRESHOLD,
    ALPACA_DATA_FEED,
    MIN_CASH_RESERVE_PCT,
    MIN_TRADE_DOLLAR_AMOUNT,
    MAX_OPEN_POSITIONS,
    EXCEPTIONAL_CONVICTION_THRESHOLD,
    EXCEPTIONAL_TRADE_RESERVE_ACCESS_PCT,
    CONSOLIDATION_SCORE_THRESHOLD,
)
import indicators as ind
from market_regime import evaluate_market_regime
from signal_score import calculate_signal_score

trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)

COOLDOWN_FILE = os.path.join(os.path.dirname(__file__), "logs", "cooldowns.json")
CUSTOM_EXITS_FILE = os.path.join(os.path.dirname(__file__), "logs", "custom_exits.json")

_FEED_MAP = {"iex": DataFeed.IEX, "sip": DataFeed.SIP, "otc": DataFeed.OTC}
DATA_FEED = _FEED_MAP.get(ALPACA_DATA_FEED.lower(), DataFeed.IEX)

_EASTERN = pytz.timezone("America/New_York")

PERFORMANCE_CSV_HEADER = [
    "timestamp", "total_value", "cash", "num_holdings", "market_regime",
    "size_multiplier", "candidates_considered", "candidates_passed_prescreen",
    "trades_proposed", "trades_executed", "trades_skipped", "trades_failed",
    "risk_exits", "consolidation_exits",
]

def get_eastern_time_str():
    now_utc = datetime.now(pytz.utc)
    now_et = now_utc.astimezone(_EASTERN)
    return now_et.strftime("%Y-%m-%d %I:%M %p %Z")

def is_market_open():
    try:
        return bool(trading_client.get_clock().is_open)
    except Exception as e:
        print(f"Could not fetch market clock, assuming closed: {e}")
        return False

def get_market_regime():
    history = get_price_history("SPY")
    if history is None:
        print("Could not fetch SPY history for market regime check, defaulting to NEUTRAL.")
        return "NEUTRAL"
    try:
        return evaluate_market_regime(
            history["closes"],
            high_vol_threshold=MARKET_HIGH_VOLATILITY_THRESHOLD,
        )
    except Exception as e:
        print(f"Market regime evaluation failed, defaulting to NEUTRAL: {e}")
        return "NEUTRAL"

def get_price(ticker):
    try:
        request = StockLatestTradeRequest(symbol_or_symbols=ticker, feed=DATA_FEED)
        trade = data_client.get_stock_latest_trade(request)
        return float(trade[ticker].price)
    except Exception as e:
        print(f"Could not get price for {ticker}: {e}")
        return None

def get_price_history(ticker, days=PRICE_HISTORY_DAYS):
    try:
        end = datetime.now()
        start = end - timedelta(days=days + 10)
        request = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            feed=DATA_FEED,
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

def get_intraday_indicators(ticker):
    if not ENABLE_INTRADAY_ANALYSIS:
        return None
    try:
        end = datetime.now()
        start = end - timedelta(days=INTRADAY_LOOKBACK_DAYS)
        request = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame(INTRADAY_BAR_MINUTES, TimeFrameUnit.Minute),
            start=start,
            end=end,
            feed=DATA_FEED,
        )
        bars = list(data_client.get_stock_bars(request)[ticker])
        if len(bars) < 20:
            return None

        closes = [b.close for b in bars]
        highs = [b.high for b in bars]
        lows = [b.low for b in bars]
        volumes = [b.volume for b in bars]

        typical_prices = [(h + l + c) / 3 for h, l, c in zip(highs, lows, closes)]
        total_volume = sum(volumes) or 1
        vwap = sum(tp * v for tp, v in zip(typical_prices, volumes)) / total_volume

        current_price = closes[-1]
        session_open = closes[0]
        intraday_momentum_pct = (
            round(((current_price - session_open) / session_open) * 100, 2)
            if session_open else None
        )
        vwap_deviation_pct = (
            round(((current_price - vwap) / vwap) * 100, 2) if vwap else None
        )

        intraday_rsi = ind.compute_rsi(closes)
        short_sma = ind.compute_sma(closes, 10)
        long_sma = ind.compute_sma(closes, 30) if len(closes) >= 30 else None
        if short_sma is not None and long_sma is not None:
            if short_sma > long_sma:
                intraday_trend = "uptrend"
            elif short_sma < long_sma:
                intraday_trend = "downtrend"
            else:
                intraday_trend = "sideways"
        else:
            intraday_trend = None

        return {
            "intraday_rsi": intraday_rsi,
            "intraday_momentum_pct": intraday_momentum_pct,
            "intraday_trend": intraday_trend,
            "vwap": round(vwap, 2),
            "vwap_deviation_pct": vwap_deviation_pct,
        }
    except Exception as e:
        print(f"Could not get intraday indicators for {ticker}: {e}")
        return None

def get_full_indicators(ticker):
    history = get_price_history(ticker)
    if history is None:
        return None

    closes, highs, lows, volumes = history["closes"], history["highs"], history["lows"], history["volumes"]
    macd = ind.compute_macd(closes)
    bb = ind.compute_bollinger_bands(closes)
    stoch = ind.compute_stochastic(highs, lows, closes)

    swing_window = min(SWING_LOOKBACK_DAYS, len(highs))
    recent_swing_low = min(lows[-swing_window:])
    recent_swing_high = max(highs[-swing_window:])

    result = {
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
        f"recent_swing_low_{SWING_LOOKBACK_DAYS}d": recent_swing_low,
        f"recent_swing_high_{SWING_LOOKBACK_DAYS}d": recent_swing_high,
    }

    intraday = get_intraday_indicators(ticker)
    if intraday:
        result.update(intraday)
    else:
        result.update({
            "intraday_rsi": None, "intraday_momentum_pct": None,
            "intraday_trend": None, "vwap": None, "vwap_deviation_pct": None,
        })
    return result

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
    cooldowns = _load_cooldowns()
    cutoff = datetime.now() - timedelta(minutes=TRADE_COOLDOWN_MINUTES)
    return {t for t, ts in cooldowns.items() if datetime.fromisoformat(ts) > cutoff}

def _record_cooldown(ticker):
    cooldowns = _load_cooldowns()
    cooldowns[ticker] = datetime.now().isoformat()
    _save_cooldowns(cooldowns)

def _load_custom_exits():
    if not os.path.exists(CUSTOM_EXITS_FILE):
        return {}
    try:
        with open(CUSTOM_EXITS_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

def _save_custom_exits(exits):
    os.makedirs(os.path.dirname(CUSTOM_EXITS_FILE), exist_ok=True)
    with open(CUSTOM_EXITS_FILE, "w") as f:
        json.dump(exits, f)

def _prune_custom_exits(holdings):
    exits = _load_custom_exits()
    pruned = {t: v for t, v in exits.items() if t in holdings}
    if pruned != exits:
        _save_custom_exits(pruned)

def _clamp_stop_loss(entry_price, atr, candidate_stop):
    if candidate_stop is None or atr is None or atr <= 0:
        return None
    distance = entry_price - candidate_stop
    min_dist = MIN_STOP_DISTANCE_ATR_MULT * atr
    max_dist = MAX_STOP_DISTANCE_ATR_MULT * atr
    distance = max(min_dist, min(distance, max_dist))
    return round(entry_price - distance, 2)

def _clamp_take_profit(entry_price, atr, candidate_tp):
    if candidate_tp is None or atr is None or atr <= 0:
        return None
    distance = candidate_tp - entry_price
    min_dist = MIN_TAKE_PROFIT_DISTANCE_ATR_MULT * atr
    max_dist = MAX_TAKE_PROFIT_DISTANCE_ATR_MULT * atr
    distance = max(min_dist, min(distance, max_dist))
    return round(entry_price + distance, 2)

def _record_custom_exit(ticker, trade, entry_price):
    atr = None
    swing_low = swing_high = None
    try:
        history = get_price_history(ticker, days=30)
        if history:
            atr = ind.compute_atr(history["highs"], history["lows"], history["closes"], period=ATR_PERIOD)
            window = min(SWING_LOOKBACK_DAYS, len(history["lows"]))
            swing_low = min(history["lows"][-window:])
            swing_high = max(history["highs"][-window:])
    except Exception as e:
        print(f"Could not compute chart-based exit levels for {ticker}: {e}")

    gemini_stop = trade.get("stop_loss") if ALLOW_GEMINI_CUSTOM_EXITS else None
    gemini_tp = trade.get("take_profit") if ALLOW_GEMINI_CUSTOM_EXITS else None

    candidate_stop = gemini_stop if gemini_stop is not None else swing_low
    candidate_tp = gemini_tp if gemini_tp is not None else swing_high

    stop_loss = _clamp_stop_loss(entry_price, atr, candidate_stop)
    take_profit = _clamp_take_profit(entry_price, atr, candidate_tp)

    if stop_loss is None and atr:
        stop_loss = round(entry_price - ATR_STOP_MULTIPLIER * atr, 2)
    if take_profit is None and atr:
        take_profit = round(entry_price + ATR_TAKE_PROFIT_MULTIPLIER * atr, 2)

    if stop_loss is None or take_profit is None:
        return

    exits = _load_custom_exits()
    exits[ticker] = {
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "entry_price": entry_price,
        "set_at": datetime.now().isoformat(),
    }
    _save_custom_exits(exits)

def check_atr_stop_take_profit(account_snapshot):
    results = []
    open_order_tickers = get_tickers_with_open_orders()
    custom_exits = _load_custom_exits()
    holdings = account_snapshot["holdings"]

    for ticker, pos in holdings.items():
        if ticker in open_order_tickers:
            continue
        entry = pos["avg_entry_price"]
        current = pos["current_price"]

        custom = custom_exits.get(ticker)
        if custom and custom.get("stop_loss") is not None and custom.get("take_profit") is not None:
            stop_level = custom["stop_loss"]
            target_level = custom["take_profit"]
        else:
            indicators_data = get_full_indicators(ticker)
            atr = indicators_data["atr_14"] if indicators_data else None
            if atr is None:
                continue
            stop_level = entry - (ATR_STOP_MULTIPLIER * atr)
            target_level = entry + (ATR_TAKE_PROFIT_MULTIPLIER * atr)

        reason = None
        if current <= stop_level:
            reason = f"Stop-loss hit: price {current} <= stop {round(stop_level, 2)}"
        elif current >= target_level:
            reason = f"Take-profit hit: price {current} >= target {round(target_level, 2)}"

        if reason:
            trade = {"ticker": ticker, "action": "sell", "dollar_amount": 0, "reasoning": reason, "conviction": 10}
            result = execute_trade(trade, account_snapshot)
            result["trigger"] = "risk_management"
            results.append(result)

    _prune_custom_exits(holdings)
    return results

def enforce_portfolio_consolidation(account_snapshot):
    results = []
    holdings = account_snapshot.get("holdings", {})
    if len(holdings) <= MAX_OPEN_POSITIONS:
        return results

    open_order_tickers = get_tickers_with_open_orders()
    excess_count = len(holdings) - MAX_OPEN_POSITIONS

    scored_holdings = []
    for ticker in holdings.keys():
        if ticker in open_order_tickers:
            continue
        indicators_data = get_full_indicators(ticker)
        score = calculate_signal_score(indicators_data)
        scored_holdings.append((score, ticker))

    scored_holdings.sort(key=lambda x: x[0])
    candidates = scored_holdings[:excess_count]

    for score, ticker in candidates:
        if score < CONSOLIDATION_SCORE_THRESHOLD:
            reason = f"Consolidation exit: score {round(score, 1)} < threshold ({CONSOLIDATION_SCORE_THRESHOLD})"
            trade = {"ticker": ticker, "action": "sell", "dollar_amount": 0, "reasoning": reason, "conviction": 10}
            result = execute_trade(trade, account_snapshot)
            result["trigger"] = "portfolio_consolidation"
            results.append(result)

    return results

def execute_trade(trade, account_snapshot=None, size_multiplier=1.0):
    ticker = trade["ticker"]
    action = trade["action"].lower()
    requested_amount = float(trade.get("dollar_amount") or 0)
    conviction = max(1, min(10, int(trade.get("conviction", 5))))

    if account_snapshot is None:
        account_snapshot = get_account_snapshot()

    price = get_price(ticker)
    if price is None or price <= 0:
        return {"ticker": ticker, "status": "failed", "reason": "no valid price data"}

    total_value = account_snapshot["total_value"]
    current_holding = account_snapshot["holdings"].get(ticker)
    current_position_value = (current_holding["qty"] * price) if current_holding else 0.0

    if action == "buy":
        is_new_position = current_holding is None
        if is_new_position and len(account_snapshot["holdings"]) >= MAX_OPEN_POSITIONS:
            return {
                "ticker": ticker,
                "status": "skipped",
                "reason": f"max open positions reached ({MAX_OPEN_POSITIONS})",
            }

        max_allowed = total_value * MAX_POSITION_PCT * (conviction / 10.0) * size_multiplier
        target_room = max(0.0, max_allowed - current_position_value)

        # Fix: If requested_amount is 0/unspecified, default to remaining target position room
        if requested_amount <= 0:
            buy_target = target_room
        else:
            buy_target = min(requested_amount, target_room)

        base_reserve = total_value * MIN_CASH_RESERVE_PCT
        is_exceptional = conviction >= EXCEPTIONAL_CONVICTION_THRESHOLD
        reserve_kept = base_reserve * (1.0 - EXCEPTIONAL_TRADE_RESERVE_ACCESS_PCT) if is_exceptional else base_reserve
        available_cash = max(0.0, account_snapshot["cash"] - reserve_kept)

        amount = min(buy_target, available_cash)

        if amount < MIN_TRADE_DOLLAR_AMOUNT:
            return {
                "ticker": ticker,
                "status": "skipped",
                "reason": f"below minimum trade size (${MIN_TRADE_DOLLAR_AMOUNT}) after position cap (${max_allowed:,.2f}), room (${target_room:,.2f}), regime multiplier ({size_multiplier:.0%}), and cash reserve (${reserve_kept:,.2f} kept of ${account_snapshot['cash']:,.2f} cash)",
            }

        qty = round(amount / price, 4)
        if qty <= 0:
            return {"ticker": ticker, "status": "skipped", "reason": "calculated quantity too small"}
        side = OrderSide.BUY

    elif action == "sell":
        shares_owned = current_holding["qty"] if current_holding else 0
        if shares_owned <= 0:
            return {"ticker": ticker, "status": "skipped", "reason": "no shares owned"}
        qty = round(min(shares_owned, requested_amount / price) if requested_amount > 0 else shares_owned, 4)
        if qty <= 0:
            return {"ticker": ticker, "status": "skipped", "reason": "calculated quantity too small"}
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
        _record_cooldown(ticker)

        if side == OrderSide.BUY:
            _record_custom_exit(ticker, trade, price)

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

def record_performance_snapshot(account_snapshot, log_dir, **stats):
    path = os.path.join(log_dir, "performance.csv")
    file_exists = os.path.exists(path)

    if file_exists:
        with open(path) as f:
            first_line = f.readline().strip()
            existing_header = first_line.split(",") if first_line else []
        if existing_header != PERFORMANCE_CSV_HEADER:
            backup_path = os.path.join(
                log_dir,
                f"performance_legacy_{datetime.now().strftime('%Y%m%d%H%M%S')}.csv",
            )
            os.rename(path, backup_path)
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
        "consolidation_exits": stats.get("consolidation_exits", 0),
    }

    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=PERFORMANCE_CSV_HEADER)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)
