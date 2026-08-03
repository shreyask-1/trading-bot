"""
Talks to your real Alpaca PAPER TRADING account (fake money, real broker
infrastructure, real order execution logic). No live/real money is ever
touched as long as paper=True stays set below.

Also computes the full technical indicator set (via indicators.py) from a
single daily price-history fetch per ticker PLUS a short-term intraday read
(RSI, momentum, trend, VWAP deviation) from recent 5-minute bars, evaluates
the broad market regime (via market_regime.py, using SPY as a proxy), and
enforces a per-position, CHART-BASED stop-loss/take-profit (computed from
recent swing lows/highs, with ATR-based sanity bounds and fallback), a
per-ticker trade cooldown, a minimum cash reserve (with a narrow exception
for exceptional-conviction ideas), a minimum trade size, a cap on total
open positions, and automatic portfolio consolidation when over position
limits.
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
    ALPACA_API_KEY, ALPACA_SECRET_KEY, MAX_POSITION_PCT,
    ATR_STOP_MULTIPLIER, ATR_TAKE_PROFIT_MULTIPLIER, ATR_PERIOD,
    SWING_LOOKBACK_DAYS, MIN_STOP_DISTANCE_ATR_MULT, MAX_STOP_DISTANCE_ATR_MULT,
    MIN_TAKE_PROFIT_DISTANCE_ATR_MULT, MAX_TAKE_PROFIT_DISTANCE_ATR_MULT,
    ALLOW_GEMINI_CUSTOM_EXITS,
    ENABLE_INTRADAY_ANALYSIS, INTRADAY_BAR_MINUTES, INTRADAY_LOOKBACK_DAYS,
    PRICE_HISTORY_DAYS, TRADE_COOLDOWN_MINUTES, MARKET_HIGH_VOLATILITY_THRESHOLD,
    ALPACA_DATA_FEED, MIN_CASH_RESERVE_PCT, MIN_TRADE_DOLLAR_AMOUNT, MAX_OPEN_POSITIONS,
    EXCEPTIONAL_CONVICTION_THRESHOLD, EXCEPTIONAL_TRADE_RESERVE_ACCESS_PCT,
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

# Column order for logs/performance.csv. If a pre-existing file has a
# different header (e.g. from an older version of the bot), it's archived
# rather than appended to under a mismatched schema -- see
# record_performance_snapshot().
PERFORMANCE_CSV_HEADER = [
    "timestamp", "total_value", "cash", "num_holdings",
    "market_regime", "size_multiplier",
    "candidates_considered", "candidates_passed_prescreen",
    "trades_proposed", "trades_executed", "trades_skipped", "trades_failed",
    "risk_exits", "consolidation_exits",
]


# ============================================================
# Time / market clock / regime
# ============================================================

def get_eastern_time_str():
    """
    Explicit US-Eastern-time string, computed from timezone-AWARE UTC.
    Used purely for unambiguous logging.
    """
    now_utc = datetime.now(pytz.utc)
    now_et = now_utc.astimezone(_EASTERN)
    return now_et.strftime("%Y-%m-%d %I:%M %p %Z")


def is_market_open():
    """
    True if the market is open for regular trading right now, per Alpaca's
    own authoritative clock. Fails closed: if the clock call errors, returns False.
    This is purely informational for logging -- it does not gate whether the
    bot runs or trades; orders submitted while closed simply queue at
    Alpaca for the next open (this is exactly how after-hours order queuing
    works for this bot).
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

    Fails safe to "NEUTRAL" if SPY's history can't be fetched or evaluated.
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
# Price data (daily)
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


# ============================================================
# Price data (intraday) -- for day-trading-relevant short-term context
# ============================================================

def get_intraday_indicators(ticker):
    """
    Fetches recent short-interval (default 5-minute) bars and computes a
    short-term technical read: intraday RSI, intraday momentum (vs this
    session's opening bar), a crude VWAP (volume-weighted average price)
    and the current price's deviation from it, and a short-term trend
    read (10-bar vs 30-bar SMA of intraday closes).

    Returns None if intraday analysis is disabled, or if there isn't
    enough recent bar data to compute anything meaningful (e.g. a newly
    listed ticker, or a data hiccup) -- callers should treat None fields
    as "unknown", not as a signal of any kind.
    """
    if not ENABLE_INTRADAY_ANALYSIS:
        return None
    try:
        end = datetime.now()
        start = end - timedelta(days=INTRADAY_LOOKBACK_DAYS)
        request = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame(INTRADAY_BAR_MINUTES, TimeFrameUnit.Minute),
            start=start, end=end, feed=DATA_FEED,
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
            round(((current_price - session_open) / session_open) * 100, 2) if session_open else None
        )
        vwap_deviation_pct = round(((current_price - vwap) / vwap) * 100, 2) if vwap else None

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
        print(f"Could not get intraday indicators for {ticker} (non-fatal, treating as unknown): {e}")
        return None


def get_full_indicators(ticker):
    """
    Computes the complete indicator set for a ticker: the existing daily
    technical set, PLUS a 10-day swing low/high (used for chart-based
    stop-loss/take-profit -- see compute_chart_based_exits() below), PLUS
    a short-term intraday read (see get_intraday_indicators() above).

    Returns a dict; any indicator that couldn't be computed (not enough
    history, or intraday disabled/unavailable) is None rather than missing.
    """
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
            "intraday_rsi": None,
            "intraday_momentum_pct": None,
            "intraday_trend": None,
            "vwap": None,
            "vwap_deviation_pct": None,
        })

    return result


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
    to gate the stop-loss/take-profit check, which must always be able
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
# Chart-based custom exit levels (stop-loss / take-profit)
# ============================================================

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
    """Drops stored exit levels for tickers no longer actually held."""
    exits = _load_custom_exits()
    pruned = {t: v for t, v in exits.items() if t in holdings}
    if pruned != exits:
        _save_custom_exits(pruned)


def _clamp_stop_loss(entry_price, atr, candidate_stop):
    """
    Ensures a stop-loss (whether chart-derived or Gemini-proposed) sits
    between MIN_STOP_DISTANCE_ATR_MULT and MAX_STOP_DISTANCE_ATR_MULT away
    from entry, in ATR terms -- prevents both noise-triggered stopouts
    (too tight) and unbounded risk (too wide/missing).
    """
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
    """
    Called after every successful BUY fill. Computes and stores a
    chart-based stop-loss/take-profit for this position:

      1. If the trade dict includes explicit stop_loss/take_profit prices
         (i.e. Gemini proposed specific levels) AND ALLOW_GEMINI_CUSTOM_EXITS
         is on, those are used as the starting candidates.
      2. Otherwise (or for whichever of the two Gemini didn't specify),
         falls back to the recent SWING_LOOKBACK_DAYS-day swing low/high.
      3. Either way, both levels are clamped to sit within a sane ATR-based
         distance from entry (see _clamp_stop_loss/_clamp_take_profit) so
         neither an overly-tight chart level nor a bad Gemini suggestion
         can produce a nonsensical stop.
      4. If ATR itself can't be computed, falls back further to the
         original flat ATR_STOP_MULTIPLIER/ATR_TAKE_PROFIT_MULTIPLIER
         defaults applied directly (old behavior), as a last resort.

    NOTE: this OVERWRITES any existing stored exit for the ticker,
    including on an "add" to an existing position -- each new fill
    re-anchors the stop/take-profit to the latest chart action. This is
    intentional (a day-trading-style bot should let stops trail with
    fresh price action, not stay pinned to a stale first-entry level).
    """
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
        print(f"Could not compute chart-based exit levels for {ticker}, will use ATR default if possible: {e}")

    gemini_stop = trade.get("stop_loss") if ALLOW_GEMINI_CUSTOM_EXITS else None
    gemini_tp = trade.get("take_profit") if ALLOW_GEMINI_CUSTOM_EXITS else None

    candidate_stop = gemini_stop if gemini_stop is not None else swing_low
    candidate_tp = gemini_tp if gemini_tp is not None else swing_high

    if gemini_stop is not None and gemini_tp is not None:
        source = "gemini"
    elif gemini_stop is not None or gemini_tp is not None:
        source = "gemini_partial_swing_default"
    elif swing_low is not None or swing_high is not None:
        source = "swing_default"
    else:
        source = "atr_default"

    stop_loss = _clamp_stop_loss(entry_price, atr, candidate_stop)
    take_profit = _clamp_take_profit(entry_price, atr, candidate_tp)

    # Last-resort fallback: no chart/Gemini data at all, but we do have ATR.
    if stop_loss is None and atr:
        stop_loss = round(entry_price - ATR_STOP_MULTIPLIER * atr, 2)
    if take_profit is None and atr:
        take_profit = round(entry_price + ATR_TAKE_PROFIT_MULTIPLIER * atr, 2)

    if stop_loss is None or take_profit is None:
        print(f"Warning: could not compute any exit levels for {ticker} (no ATR, no chart data) -- "
              f"position will have no automated stop-loss/take-profit until data recovers.")
        return

    exits = _load_custom_exits()
    exits[ticker] = {
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "entry_price": entry_price,
        "set_at": datetime.now().isoformat(),
        "source": source,
    }
    _save_custom_exits(exits)
    print(f"Set exit levels for {ticker}: stop ${stop_loss} / target ${take_profit} (source: {source})")


# ============================================================
# ATR/chart-based risk management & Portfolio Consolidation
# ============================================================

def check_atr_stop_take_profit(account_snapshot):
    """
    For every holding, checks its stored chart-based stop-loss/take-profit
    (set at entry time by _record_custom_exit -- see above) and force-sells
    if either is breached. Falls back to a fresh flat-ATR-based level for
    any holding that somehow has no stored custom exit (e.g. positions that
    existed before this feature was added). Independent of what Gemini
    decides that run, and independent of market regime (an exit is always
    allowed).

    Deliberately ignores the trade cooldown: a position opened moments ago
    is exactly the one most in need of its stop-loss staying active. Only
    skips tickers with an already-open order, to avoid duplicate exits.
    """
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
            level_source = custom.get("source", "custom")
        else:
            # No stored custom exit (e.g. a pre-existing position from
            # before this feature existed) -- compute a fresh flat-ATR
            # default on the fly, same as the old behavior.
            indicators_data = get_full_indicators(ticker)
            atr = indicators_data["atr_14"] if indicators_data else None
            if atr is None:
                continue  # can't compute a data-driven stop without ATR; skip rather than guess
            stop_level = entry - (ATR_STOP_MULTIPLIER * atr)
            target_level = entry + (ATR_TAKE_PROFIT_MULTIPLIER * atr)
            level_source = "atr_default_no_stored_exit"

        reason = None
        if current <= stop_level:
            reason = f"Stop-loss hit ({level_source}): price {current} <= stop {round(stop_level, 2)}"
        elif current >= target_level:
            reason = f"Take-profit hit ({level_source}): price {current} >= target {round(target_level, 2)}"

        if reason:
            trade = {"ticker": ticker, "action": "sell", "dollar_amount": 0, "reasoning": reason, "conviction": 10}
            result = execute_trade(trade, account_snapshot)
            result["trigger"] = "risk_management"
            results.append(result)

    _prune_custom_exits(holdings)
    return results


def enforce_portfolio_consolidation(account_snapshot):
    """
    Enforces portfolio sprawl control if current holdings exceed MAX_OPEN_POSITIONS.

    1. Computes the deterministic quant score (0-100) for all holdings.
    2. Identifies the excess count (N).
    3. Sorts all holdings by score ascending.
    4. Takes the worst N holdings.
    5. Force-sells any of those worst N that score below CONSOLIDATION_SCORE_THRESHOLD.
       If any score above the threshold, they are kept until they drop below.
    """
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

    # Sort ascending (lowest score first, which isolates worst assets)
    scored_holdings.sort(key=lambda x: x[0])

    # Slice the worst N candidates (where N is the excess amount)
    candidates = scored_holdings[:excess_count]

    for score, ticker in candidates:
        if score < CONSOLIDATION_SCORE_THRESHOLD:
            reason = (
                f"Consolidation exit: Ticker score {round(score, 1)} is "
                f"below threshold ({CONSOLIDATION_SCORE_THRESHOLD}) while holding "
                f"{len(holdings)} positions (limit {MAX_OPEN_POSITIONS})."
            )
            trade = {
                "ticker": ticker,
                "action": "sell",
                "dollar_amount": 0,
                "reasoning": reason,
                "conviction": 10
            }
            result = execute_trade(trade, account_snapshot)
            result["trigger"] = "portfolio_consolidation"
            results.append(result)

    return results


# ============================================================
# Order execution (conviction-scaled, regime-scaled, cash- and
# sprawl-aware sizing, with a narrow exception for exceptional
# conviction ideas to access part of the cash reserve)
# ============================================================

def execute_trade(trade, account_snapshot=None, size_multiplier=1.0):
    """
    trade: {"ticker", "action", "dollar_amount", "reasoning", "conviction" (1-10),
            "stop_loss" (optional, BUY only), "take_profit" (optional, BUY only)}

    Buy sizing, in order of constraints applied:
      1. conviction/10 and the market-regime size_multiplier scale the
         MAX_POSITION_PCT cap (0.0 regime multiplier blocks all buys).
      2. MIN_CASH_RESERVE_PCT of total portfolio value is normally kept
         uninvested -- EXCEPT for a trade at or above
         EXCEPTIONAL_CONVICTION_THRESHOLD conviction, which may draw down
         up to EXCEPTIONAL_TRADE_RESERVE_ACCESS_PCT of that reserve. Every
         other constraint below still applies to exceptional trades too --
         this only changes how much cash counts as "available."
      3. MAX_OPEN_POSITIONS blocks opening a BRAND NEW ticker (adds to an
         existing holding are unaffected) once the cap is reached -- no
         exception, regardless of conviction.
      4. MIN_TRADE_DOLLAR_AMOUNT -- anything smaller than this is skipped
         rather than executed as a dust trade.
    None of these apply to sells: an exit is always allowed regardless of
    size, cash reserve, or position count, so the bot can always clean up
    or de-risk.

    On a successful BUY fill, a chart-based stop-loss/take-profit is
    computed and stored for this position (see _record_custom_exit) --
    using trade["stop_loss"]/trade["take_profit"] if provided and allowed,
    otherwise a fresh swing-high/low read of the chart.
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
                "reason": f"max open positions reached ({MAX_OPEN_POSITIONS} held, no exception for "
                          f"conviction); only adds to existing holdings or sells are allowed until "
                          f"it consolidates",
            }

        max_allowed = total_value * MAX_POSITION_PCT * (conviction / 10) * size_multiplier

        base_reserve = total_value * MIN_CASH_RESERVE_PCT
        is_exceptional = conviction >= EXCEPTIONAL_CONVICTION_THRESHOLD
        if is_exceptional:
            reserve_kept = base_reserve * (1 - EXCEPTIONAL_TRADE_RESERVE_ACCESS_PCT)
        else:
            reserve_kept = base_reserve
        available_cash = max(0.0, account_snapshot["cash"] - reserve_kept)

        amount = min(requested_amount, max_allowed - current_position_value, available_cash)
        if amount < MIN_TRADE_DOLLAR_AMOUNT:
            note = " (exceptional conviction already granted partial reserve access)" if is_exceptional else ""
            return {
                "ticker": ticker, "status": "skipped",
                "reason": f"below minimum trade size (${MIN_TRADE_DOLLAR_AMOUNT}) after position cap, "
                          f"regime multiplier, and/or cash reserve (${reserve_kept:,.2f} kept uninvested "
                          f"of ${base_reserve:,.2f} normal reserve, ${account_snapshot['cash']:,.2f} cash "
                          f"on hand){note}",
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


# ============================================================
# Performance logging
# ============================================================

def record_performance_snapshot(account_snapshot, log_dir, **stats):
    """
    Appends one row to logs/performance.csv per run. Extra keyword args
    (all optional) populate the columns.

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
        "consolidation_exits": stats.get("consolidation_exits", 0),
    }

    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=PERFORMANCE_CSV_HEADER)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)
