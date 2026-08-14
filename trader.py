"""
Talks to Alpaca PAPER TRADING account.
Handles position sizing, chart-based stops/targets, risk limits, and order submission.
"""

import os
import json
import csv
from datetime import datetime, timedelta
import pytz
import requests

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest, GetOrdersRequest, CreateWatchlistRequest, UpdateWatchlistRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed

from config import (
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY,
    ALLOW_EXTENDED_HOURS,
    FLAT_SIZING,
    FLAT_TRADE_SIZE_PCT,
    MAX_POSITION_PCT,
    ATR_STOP_MULTIPLIER,
    ATR_TAKE_PROFIT_MULTIPLIER,
    ATR_PERIOD,
    SWING_LOOKBACK_DAYS,
    MIN_STOP_DISTANCE_ATR_MULT,
    MAX_STOP_DISTANCE_ATR_MULT,
    MIN_TAKE_PROFIT_DISTANCE_ATR_MULT,
    MAX_TAKE_PROFIT_DISTANCE_ATR_MULT,
    MIN_REWARD_RISK_RATIO,
    ENABLE_SCALE_OUT,
    SCALE_OUT_AT_RR_FRAC,
    SCALE_OUT_FRAC,
    ENABLE_QUALITY_TRIM,
    QUALITY_TRIM_SCORE_THRESHOLD,
    QUALITY_TRIM_MAX_PER_RUN,
    QUALITY_TRIM_LOSS_GUARD_PCT,
    QUALITY_TRIM_PROFIT_TAKE_PCT,
    WALKFORWARD_LIVE_LEARNING,
    WALKFORWARD_MIN_SAMPLES,
    WALKFORWARD_PROVEN_MULT,
    MAX_PENDING_TRADES,
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
    MAX_TOTAL_EXPOSURE_PCT,
    MAX_POSITION_LOSS_PCT,
    DELEVERAGE_TARGET_CASH_PCT,
    DAILY_LOSS_HALT_PCT,
    MAX_DRAWDOWN_DELEVERAGE_PCT,
    MAX_DRAWDOWN_FLATTEN_PCT,
    DELEVERAGE_SIZE_MULTIPLIER,
    RESET_EQUITY_PEAK_ON_START,
    DISCORD_WEBHOOK_URL,
    ENABLE_FOREIGN_ACTIVITY_DETECTION,
    DAYTRADE_MODE,
    END_OF_DAY_FLATTEN,
    END_OF_DAY_FLATTEN_TIME,
    OPENING_RANGE_BARS,
    TRADE_START_MINUTES_AFTER_OPEN,
    STOP_NEW_BUYS_AFTER,
    MAX_BUY_EXTENSION_ABOVE_VWAP_PCT,
    MAX_INTRADAY_MOVE_PCT,
    CHASE_HARD_SKIP_MULT,
    TRAILING_STOP_ACTIVATE_MULT,
    TRAILING_STOP_DISTANCE_MULT,
    MAX_RISK_PER_TRADE_PCT,
    RISK_PARITY_SIZING,
    MAX_SECTOR_EXPOSURE_PCT,
    TIME_OF_DAY_MULTIPLIERS,
    CONFIDENCE_SIZING,
    CONFIDENCE_MIN_TO_TRADE,
    ENABLE_MA_BREAKDOWN_EXIT,
    ENABLE_RSI_EXHAUSTION_EXIT,
    RSI_EXHAUSTION_LEVEL,
    ENABLE_NEGATIVE_NEWS_EXIT,
    NEGATIVE_NEWS_SENTIMENT_THRESHOLD,
    MARKET_VIX_STRESS_LEVEL,
    MARKET_VIX_SEVERE_LEVEL,
    ENABLE_ECONOMIC_CALENDAR,
    HIGH_IMPACT_EVENT_SIZE_MULT,
    EARNINGS_PROXIMITY_DAYS,
    EARNINGS_PROXIMITY_SIZE_MULT,
    SELF_LEARNING_ENABLED,
    SETUP_MULT_MIN,
    SETUP_MULT_MAX,
    SELF_LEARNING_MIN_SAMPLES,
    SELF_LEARNING_EDGE_MIN,
)
import indicators as ind
from market_regime import evaluate_market_regime
from signal_score import calculate_signal_score

trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)

COOLDOWN_FILE = os.path.join(os.path.dirname(__file__), "logs", "cooldowns.json")
CUSTOM_EXITS_FILE = os.path.join(os.path.dirname(__file__), "logs", "custom_exits.json")
RISK_STATE_FILE = os.path.join(os.path.dirname(__file__), "logs", "risk_state.json")
ORDER_LEDGER_FILE = os.path.join(os.path.dirname(__file__), "logs", "bot_order_ledger.json")
RECON_STATE_FILE = os.path.join(os.path.dirname(__file__), "logs", "reconciliation_state.json")
SETUP_GATE_FILE = os.path.join(os.path.dirname(__file__), "data", "setup_gate.json")
TRADES_JOURNAL_FILE = os.path.join(os.path.dirname(__file__), "logs", "trades_journal.csv")
TRADE_RESULTS_FILE = os.path.join(os.path.dirname(__file__), "logs", "trade_results.csv")
OPEN_TRADES_FILE = os.path.join(os.path.dirname(__file__), "logs", "open_trades.json")
NEWS_SENTIMENT_CACHE_FILE = os.path.join(os.path.dirname(__file__), "logs", "news_sentiment_cache.json")
EARNINGS_CAL_FILE = os.path.join(os.path.dirname(__file__), "logs", "earnings_calendar.json")
# Overnight queue: trade ideas proposed while no session could fill, re-verified
# by Gemini at the next live-session run before anything is placed.
PENDING_TRADES_FILE = os.path.join(os.path.dirname(__file__), "data", "pending_trades.json")
# Dashboard watchlist: the Alpaca dashboard renders news/headlines from your
# watchlist, so even a flat cash account shows content for the names the bot
# is actively watching. Kept in sync by sync_dashboard_watchlist() each run.
DASHBOARD_WATCHLIST_NAME = "Bot Universe"
WATCHLIST_MAX_SYMBOLS = 60

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

def is_extended_session(now_et=None):
    """
    True during Alpaca's extended sessions: 4:00-9:30 AM and 4:00-8:00 PM ET
    on weekdays. Pure local-time check (no API call); callers combine it with
    the market clock for holiday/weekend awareness. 'now_et' is injectable
    for tests.
    """
    if now_et is None:
        now_et = datetime.now(pytz.utc).astimezone(_EASTERN)
    if now_et.weekday() >= 5:
        return False
    t = now_et.time()
    pre_open = datetime.strptime("04:00", "%H:%M").time()
    open_t = datetime.strptime("09:30", "%H:%M").time()
    close_t = datetime.strptime("16:00", "%H:%M").time()
    after_close = datetime.strptime("20:00", "%H:%M").time()
    return (pre_open <= t < open_t) or (close_t <= t < after_close)


def is_trading_session():
    """
    True when the bot may propose/execute NEW trades right now: the regular
    session (9:30-16:00 ET) always counts; when ALLOW_EXTENDED_HOURS is on,
    the extended sessions (4:00-9:30 and 16:00-20:00 ET) count too. Guards
    against weekend/holiday drift: when the clock says the market is closed
    and the next regular open is more than 16h away, no session is live.
    """
    try:
        clock = trading_client.get_clock()
        if clock.is_open:
            return True
        if not ALLOW_EXTENDED_HOURS:
            return False
        try:
            next_open = getattr(clock, "next_open", None)
            if next_open is not None:
                if getattr(next_open, "tzinfo", None) is None:
                    next_open = _EASTERN.localize(next_open)
                if (next_open.astimezone(pytz.utc) - datetime.now(pytz.utc)) > timedelta(hours=16):
                    return False  # weekend or holiday gap -- no session
        except Exception:
            pass
        return is_extended_session()
    except Exception:
        return is_extended_session()

def get_market_regime():
    """
    Broad market regime from SPY + QQQ trend/volatility plus a best-effort
    VIX check (CBOE "VIX" index when the data feed provides it). Returns
    the most defensive regime across the inputs so the bot never buys
    aggressively into a bear tape. Falls back to NEUTRAL if no data is
    available.

    NOTE: Alpaca's stock feeds (iex/sip) do NOT carry the CBOE VIX index,
    so that fetch normally fails -- expected, not an error. When VIX is
    unavailable, the elevated-volatility signal comes from SPY/QQQ realized
    volatility inside evaluate_market_regime(), which is the correct
    fallback. (Previously this compared UVXY's raw price -- a 2x short-vol
    ETN whose level has nothing to do with the VIX index -- against VIX
    index thresholds, which could flag a fake stress regime.)
    """
    order = ["BEARISH", "HIGH_VOLATILITY", "NEUTRAL", "BULLISH"]
    regimes = []
    for ticker in ("SPY", "QQQ"):
        history = get_price_history(ticker)
        if history is None:
            continue
        try:
            regimes.append(
                evaluate_market_regime(
                    history["closes"],
                    high_vol_threshold=MARKET_HIGH_VOLATILITY_THRESHOLD,
                )
            )
        except Exception as e:
            print(f"Market regime evaluation failed for {ticker}, skipping: {e}")
    if not regimes:
        print("Could not fetch SPY/QQQ history for market regime check, defaulting to NEUTRAL.")
        return "NEUTRAL"

    vix = None
    try:
        request = StockLatestTradeRequest(symbol_or_symbols="VIX", feed=DATA_FEED)
        trade = data_client.get_stock_latest_trade(request)
        vix = float(trade["VIX"].price)
    except Exception:
        vix = None
    if vix and vix > 0:
        if vix > MARKET_VIX_SEVERE_LEVEL:
            print(f"VIX {vix:.1f} > severe {MARKET_VIX_SEVERE_LEVEL:.0f} -> BEARISH (defensive).")
            return "BEARISH"
        if vix > MARKET_VIX_STRESS_LEVEL:
            print(f"VIX {vix:.1f} > stress {MARKET_VIX_STRESS_LEVEL:.0f} -> HIGH_VOLATILITY (defensive).")
            return "HIGH_VOLATILITY"
    else:
        # Expected on Alpaca: VIX is a CBOE index, not a tradeable stock.
        # SPY/QQQ realized volatility already covers the elevated-vol case.
        print("VIX index unavailable on this data feed (expected); using SPY/QQQ volatility.")

    defensive = min(regimes, key=lambda r: order.index(r) if r in order else 2)
    return defensive

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

        # Today's Eastern session bars only -- session open / VWAP / momentum
        # must be measured against TODAY's open, not a 2-day-old close. (This
        # was a bug: intraday_momentum_pct was % vs a 2-day-old anchor, which
        # made the VWAP/momentum numbers in the prompt and filters misleading.)
        today_et = datetime.now(pytz.utc).astimezone(_EASTERN).strftime("%Y-%m-%d")
        session_bars = []
        for b in bars:
            ts = b.timestamp
            if ts.tzinfo is None:
                ts = pytz.utc.localize(ts)
            if ts.astimezone(_EASTERN).strftime("%Y-%m-%d") == today_et:
                session_bars.append(b)

        if session_bars:
            session_open = session_bars[0].open
            sp = [((b.high + b.low + b.close) / 3) * b.volume for b in session_bars]
            sv = sum(b.volume for b in session_bars)
            vwap = (sum(sp) / sv) if sv else None
        else:
            # Pre-open fallback: session not started yet -- anchor to the last
            # close so the fields are still populated (bot won't buy pre-open
            # anyway due to the market-hours gate).
            session_open = closes[-1]
            typical_prices = [(h + l + c) / 3 for h, l, c in zip(highs, lows, closes)]
            total_volume = sum(volumes) or 1
            vwap = sum(tp * v for tp, v in zip(typical_prices, volumes)) / total_volume

        current_price = closes[-1]
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
            "session_open": round(session_open, 2),
        }
    except Exception as e:
        print(f"Could not get intraday indicators for {ticker}: {e}")
        return None

def get_opening_range_breakout(ticker):
    """
    Daytrading signal: the first OPENING_RANGE_BARS 5-minute bars of the
    current session define an opening range; price above it is a bullish
    breakout, below it a bearish breakdown. Returns None if not enough
    intraday data (e.g. pre-open), else a dict with high/low/status.
    """
    try:
        now = datetime.now()
        start = now - timedelta(days=2)
        request = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame(INTRADAY_BAR_MINUTES, TimeFrameUnit.Minute),
            start=start,
            end=now,
            feed=DATA_FEED,
        )
        bars = list(data_client.get_stock_bars(request)[ticker])
    except Exception as e:
        print(f"Could not get opening-range bars for {ticker}: {e}")
        return None
    if not bars:
        return None

    # Keep only bars from today's Eastern session (9:30 ET open).
    today_et = datetime.now(pytz.utc).astimezone(_EASTERN).strftime("%Y-%m-%d")
    session_bars = []
    for b in bars:
        ts = b.timestamp
        if ts.tzinfo is None:
            ts = pytz.utc.localize(ts)
        ts_et = ts.astimezone(_EASTERN)
        if ts_et.strftime("%Y-%m-%d") != today_et:
            continue
        if ts_et.time() < datetime.strptime("09:30", "%H:%M").time():
            continue
        session_bars.append(b)
    if len(session_bars) < OPENING_RANGE_BARS + 1:
        return None

    open_bars = session_bars[:OPENING_RANGE_BARS]
    range_high = max(b.high for b in open_bars)
    range_low = min(b.low for b in open_bars)
    current = session_bars[-1].close

    if current > range_high:
        status = "above"
    elif current < range_low:
        status = "below"
    else:
        status = "inside"
    return {
        "opening_range_high": round(range_high, 2),
        "opening_range_low": round(range_low, 2),
        "opening_range_status": status,
        "opening_range_bars": OPENING_RANGE_BARS,
    }


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
        "support": recent_swing_low,
        "resistance": recent_swing_high,
        "macd_cross": ind.compute_macd_crossover(closes),
        "high_52w": max(highs) if highs else None,
        "low_52w": min(lows) if lows else None,
    }

    intraday = get_intraday_indicators(ticker)
    if intraday:
        result.update(intraday)
    else:
        result.update({
            "intraday_rsi": None, "intraday_momentum_pct": None,
            "intraday_trend": None, "vwap": None, "vwap_deviation_pct": None,
            "session_open": None,
        })

    # Gap % vs the prior session close (needs today's session open).
    price_now = result.get("price")
    if intraday and intraday.get("session_open") and len(closes) >= 2 and closes[-2]:
        result["gap_pct"] = round((intraday["session_open"] - closes[-2]) / closes[-2] * 100.0, 2)
    else:
        result["gap_pct"] = None
    # Distance from the 52-week high / low (the history fetch is 400 days).
    high_52w, low_52w = result.get("high_52w"), result.get("low_52w")
    if price_now and high_52w and high_52w > 0:
        result["dist_from_52w_high_pct"] = round((high_52w - price_now) / high_52w * 100.0, 2)
    else:
        result["dist_from_52w_high_pct"] = None
    if price_now and low_52w and low_52w > 0:
        result["dist_from_52w_low_pct"] = round((price_now - low_52w) / low_52w * 100.0, 2)
    else:
        result["dist_from_52w_low_pct"] = None
    # Earnings proximity from the cached earnings calendar (refreshed by
    # morning_prep.py). Best-effort; None when unavailable.
    try:
        cal = _load_json_file(EARNINGS_CAL_FILE, {})
        edate = cal.get(ticker)
        if edate:
            days = (datetime.strptime(edate, "%Y-%m-%d").date() - datetime.now().date()).days
            result["days_until_earnings"] = days
        else:
            result["days_until_earnings"] = None
    except Exception:
        result["days_until_earnings"] = None

    if DAYTRADE_MODE:
        or_data = get_opening_range_breakout(ticker)
        if or_data:
            result.update(or_data)
        else:
            result.update({
                "opening_range_high": None, "opening_range_low": None,
                "opening_range_status": None, "opening_range_bars": OPENING_RANGE_BARS,
            })
    return result

# Unfilled extended-hours GTC limit orders older than this many hours are
# considered stale and get canceled automatically each run (see
# cancel_stale_extended_orders). GTC lets evening orders survive overnight to
# fill in pre-market, but an order that has not filled for days carries a stale
# price and stale conviction -- it would also permanently block that ticker
# from being re-proposed (the open-order dedup). 96h covers long holiday
# weekends while still clearing genuinely dead orders.
STALE_GTC_MAX_AGE_HOURS = 96


def cancel_stale_extended_orders():
    """
    Best-effort: cancels open GTC orders older than STALE_GTC_MAX_AGE_HOURS.
    Only extended-hours limit orders are ever submitted as GTC, so any open
    GTC order is by construction an overnight/after-hours entry waiting to
    fill. Returns the number canceled. Never raises.
    """
    try:
        request = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        orders = trading_client.get_orders(request)
    except Exception as e:
        print(f"Could not fetch open orders for stale cleanup: {e}")
        return 0
    now = datetime.now(pytz.utc)
    canceled = 0
    for o in orders:
        tif = _status_val(getattr(o, "time_in_force", ""))
        if tif != "gtc":
            continue
        try:
            submitted = o.submitted_at
            if submitted.tzinfo is None:
                submitted = submitted.replace(tzinfo=pytz.utc)
            age_hours = (now - submitted).total_seconds() / 3600.0
        except Exception:
            continue
        if age_hours > STALE_GTC_MAX_AGE_HOURS:
            try:
                trading_client.cancel_order_by_id(o.id)
                canceled += 1
            except Exception as e:
                print(f"Could not cancel stale order {o.id}: {e}")
    if canceled:
        print(f"Canceled {canceled} stale extended-hours order(s) older than {STALE_GTC_MAX_AGE_HOURS}h.")
    return canceled


def get_account_snapshot():
    # Clear stale unfilled GTC orders first so tickers become eligible for
    # fresh re-verification instead of being blocked by an old order. Never
    # lets cleanup break the run.
    try:
        cancel_stale_extended_orders()
    except Exception:
        pass
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
        # Account identity for the second-trader baseline: every Alpaca account
        # (live / paper / each new paper key set) has a unique id, so a switch
        # to a new account is detected and the stale baseline / risk state is
        # reset instead of being misread as a second trader.
        "account_id": str(getattr(account, "id", "") or ""),
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

def get_open_orders_with_side():
    """
    Returns open orders as [{'symbol', 'qty', 'side'}] (side in {'buy','sell'}).
    Used to reserve cash for pending BUY orders -- the missing piece that let
    the bot stack overnight orders that all filled at once on 2026-08-07.
    """
    try:
        request = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        orders = trading_client.get_orders(request)
    except Exception as e:
        print(f"Could not fetch open order details: {e}")
        return []
    out = []
    for o in orders:
        side = _status_val(getattr(o, "side", ""))
        if side not in ("buy", "sell"):
            continue
        try:
            qty = float(o.qty) if o.qty else 0.0
        except (TypeError, ValueError):
            qty = 0.0
        if qty <= 0:
            continue
        out.append({"symbol": o.symbol, "qty": qty, "side": side})
    return out

def pending_order_notional(open_orders=None):
    """
    Estimated dollars already committed to the market by open orders:
    returns (buy_notional, sell_notional). Buys consume cash when they fill;
    sells free cash. A missing price simply skips that order (conservative
    enough -- the hard no-margin and exposure rules still apply).
    """
    if open_orders is None:
        open_orders = get_open_orders_with_side()
    symbols = {o["symbol"] for o in open_orders}
    prices = {}
    for s in symbols:
        p = get_price(s)
        if p and p > 0:
            prices[s] = p
    buy_notional = 0.0
    sell_notional = 0.0
    for o in open_orders:
        p = prices.get(o["symbol"])
        if not p:
            continue
        notional = o["qty"] * p
        if o["side"] == "buy":
            buy_notional += notional
        else:
            sell_notional += notional
    return buy_notional, sell_notional

def get_gross_exposure(account_snapshot):
    """Dollar value of all held long positions (short exposure unsupported)."""
    holdings = account_snapshot.get("holdings", {})
    return sum(p["qty"] * p["current_price"] for p in holdings.values())

# ============================================================
# Dashboard watchlist sync (news/headlines for the Alpaca app)
# ============================================================
def sync_dashboard_watchlist(tickers):
    """
    Keep the Alpaca dashboard watchlist ("Bot Universe") in sync with the
    tickers the bot cares about: holdings, open orders, and today's scan
    candidates. The Alpaca dashboard's news/headlines feed follows your
    watchlist, so a brand-new all-cash account otherwise renders "no data"
    for headlines/news even while the bot is working.

    One update call replaces the whole list (no per-symbol churn), capped at
    WATCHLIST_MAX_SYMBOLS. Best-effort: any failure is swallowed (returns
    None) so a dashboard cosmetic can never break a trading run. Returns the
    number of symbols synced, or None when there was nothing to sync / it
    failed.
    """
    try:
        desired = [t for t in dict.fromkeys(str(t).strip().upper() for t in tickers) if t]
        desired = desired[:WATCHLIST_MAX_SYMBOLS]
        if not desired:
            return None
        watchlists = trading_client.get_watchlists()
        ours = None
        for wl in watchlists:
            if str(getattr(wl, "name", "") or "").strip() == DASHBOARD_WATCHLIST_NAME:
                ours = wl
                break
        if ours is None:
            trading_client.create_watchlist(
                CreateWatchlistRequest(name=DASHBOARD_WATCHLIST_NAME, symbols=desired)
            )
            print(f"Dashboard watchlist '{DASHBOARD_WATCHLIST_NAME}' created ({len(desired)} symbols).")
            return len(desired)
        wl_id = str(getattr(ours, "id", "") or "")
        if wl_id:
            # update_watchlist_by_id replaces the full symbol list in one call.
            trading_client.update_watchlist_by_id(
                wl_id, UpdateWatchlistRequest(symbols=desired)
            )
            return len(desired)
        return None
    except Exception as e:
        print(f"Dashboard watchlist sync failed (continuing without it): {e}")
        return None

# ============================================================
# Overnight trade queue (24/7 flow: queue at night, verify at the open)
# ============================================================
def load_pending_trades():
    """Overnight queue: trade ideas proposed while no session could fill."""
    return _load_json_file(PENDING_TRADES_FILE, [])


def save_pending_trades(trades):
    """
    Merge `trades` into the overnight queue (dedup by ticker+action; newer
    entries win, so a later run's fresher analysis replaces an older idea for
    the same name). Keeps only the top MAX_PENDING_TRADES by conviction then
    confidence, so the morning verification prompt stays tight instead of
    bloating across a long night (the queue refreshes fresh every night).
    Returns the total number queued.
    """
    existing = load_pending_trades()
    merged = {}
    for t in existing + list(trades):
        key = (t.get("ticker"), str(t.get("action", "buy")).lower())
        merged[key] = t
    out = list(merged.values())
    if MAX_PENDING_TRADES > 0 and len(out) > MAX_PENDING_TRADES:
        out.sort(
            key=lambda t: (
                float(t.get("conviction", 0) or 0),
                float(t.get("confidence", 0) or 0),
            ),
            reverse=True,
        )
        out = out[:MAX_PENDING_TRADES]
    _save_json_file(PENDING_TRADES_FILE, out)
    return len(out)


def clear_pending_trades():
    """Drop the overnight queue (called after Gemini re-verified it)."""
    _save_json_file(PENDING_TRADES_FILE, [])

# ============================================================
# Equity-level circuit breakers
# ============================================================
def _default_risk_state():
    return {
        "peak_equity": None,
        "day": None,
        "day_start_equity": None,
        "halted": False,
        "halt_reason": "",
        "halt_date": None,
        "deleveraged": False,
        "deleverage_date": None,
    }

def load_risk_state():
    state = _load_json_file(RISK_STATE_FILE, _default_risk_state())
    for k, v in _default_risk_state().items():
        state.setdefault(k, v)
    return state

def save_risk_state(state):
    _save_json_file(RISK_STATE_FILE, state)

def _load_json_file(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default

def _save_json_file(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)

def evaluate_circuit_breakers(account_snapshot):
    """
    Equity-level risk guardrails that run every cycle, before anything else.

    Returns (halted, halt_reason, size_multiplier, drawdown_pct, daily_pl_pct,
    peak_equity, messages). 'halted' means NO new buys for the rest of the day
    (stops and forced sells still run). size_multiplier is 1.0 normally and
    drops to DELEVERAGE_SIZE_MULTIPLIER once drawdown passes the deleverage
    threshold.
    """
    equity = float(account_snapshot.get("total_value", 0.0))
    state = load_risk_state()
    messages = []

    # New account / new API keys: drop the old account's peak / daily anchor /
    # halt flags so the new account isn't measured against the old one (the
    # stale day-anchor is what printed a bogus 'today +12%' on a fresh account
    # after the 2026-08-12 switch).
    global _ACCOUNT_CHANGED_THIS_RUN
    account_id = account_snapshot.get("account_id", "")
    if _ACCOUNT_CHANGED_THIS_RUN or _account_changed(account_id, state):
        print("ACCOUNT CHANGED: resetting risk state (equity peak / daily anchor) for the new account.")
        state = _default_risk_state()
        _ACCOUNT_CHANGED_THIS_RUN = False
    state["account_id"] = account_id

    # Running equity peak (used for drawdown). Default: start at current value
    # on first run so an already-damaged account isn't surprised on deploy.
    peak = state.get("peak_equity")
    if RESET_EQUITY_PEAK_ON_START and peak is None:
        peak = equity
    if peak is None or equity > peak:
        peak = equity
    state["peak_equity"] = peak

    # Daily anchor: equity at the start of each Eastern day.
    now_utc = datetime.now(pytz.utc)
    day = now_utc.astimezone(_EASTERN).strftime("%Y-%m-%d")
    if state.get("day") != day:
        state["day"] = day
        state["day_start_equity"] = equity
        state["halted"] = False
        state["halt_reason"] = ""

    # Both "stop the day" breakers are opt-in (config defaults to 0 = off).
    # If both are disabled, never let a stale 'halted' flag from a previous
    # config/day keep the bot from trading.
    if DAILY_LOSS_HALT_PCT <= 0 and MAX_DRAWDOWN_FLATTEN_PCT <= 0:
        state["halted"] = False
        state["halt_reason"] = ""

    day_start = state.get("day_start_equity")
    daily_pl_pct = 0.0
    if day_start:
        daily_pl_pct = (equity - day_start) / day_start * 100.0
        if DAILY_LOSS_HALT_PCT > 0 and daily_pl_pct <= -DAILY_LOSS_HALT_PCT:
            state["halted"] = True
            state["halt_reason"] = (
                f"daily loss {daily_pl_pct:.1f}% >= limit {DAILY_LOSS_HALT_PCT}%"
            )
            state["halt_date"] = day

    drawdown_pct = 0.0
    if peak:
        drawdown_pct = (peak - equity) / peak * 100.0

    if MAX_DRAWDOWN_FLATTEN_PCT > 0 and drawdown_pct >= MAX_DRAWDOWN_FLATTEN_PCT:
        state["halted"] = True
        state["halt_reason"] = (
            f"drawdown {drawdown_pct:.1f}% >= flatten threshold "
            f"{MAX_DRAWDOWN_FLATTEN_PCT}%"
        )
        state["halt_date"] = day

    size_multiplier = 1.0
    if drawdown_pct >= MAX_DRAWDOWN_DELEVERAGE_PCT:
        size_multiplier = DELEVERAGE_SIZE_MULTIPLIER
        if (not state.get("deleveraged")) or state.get("deleverage_date") != day:
            messages.append(
                f"Drawdown {drawdown_pct:.1f}% >= {MAX_DRAWDOWN_DELEVERAGE_PCT}%: "
                f"position sizing cut to {DELEVERAGE_SIZE_MULTIPLIER:.0%}."
            )
        state["deleveraged"] = True
        state["deleverage_date"] = day

    halted = bool(state.get("halted"))
    if halted:
        messages.append(f"Trading halted: {state.get('halt_reason', 'unknown')}.")

    save_risk_state(state)
    return halted, state.get("halt_reason", ""), size_multiplier, drawdown_pct, daily_pl_pct, peak, messages

def enforce_deleveraging(account_snapshot):
    """
    Margin healing: if cash is below DELEVERAGE_TARGET_CASH_PCT of equity
    (almost always negative), sell the weakest-scored holdings until projected
    cash is back above target. This is what would have dug the account out of
    its -$4.7k hole on 2026-08-07 instead of leaving it negative for 4 days.
    """
    results = []
    total_value = float(account_snapshot.get("total_value", 0.0))
    cash = float(account_snapshot.get("cash", 0.0))
    target_cash = total_value * DELEVERAGE_TARGET_CASH_PCT
    if cash >= target_cash:
        return results

    open_order_tickers = get_tickers_with_open_orders()
    holdings = account_snapshot.get("holdings", {})

    # Sells already queued in the market will free cash when they fill at the
    # next open; count them toward the target so overnight runs don't stack
    # MORE sells on top of positions that are already being liquidated. (Each
    # run used to re-enter the loop with the same negative cash and sell the
    # next-weakest holding, queuing sells on more positions than needed.)
    try:
        _, pending_sell_notional = pending_order_notional()
    except Exception:
        pending_sell_notional = 0.0

    scored = []
    for ticker in holdings.keys():
        if ticker in open_order_tickers:
            continue
        indicators_data = get_full_indicators(ticker)
        score = calculate_signal_score(indicators_data)
        scored.append((score, ticker))
    scored.sort(key=lambda x: x[0])  # weakest first

    projected_cash = cash + pending_sell_notional
    for score, ticker in scored:
        if projected_cash >= target_cash:
            break
        pos = holdings[ticker]
        notional = pos["qty"] * pos["current_price"]
        if notional <= 0:
            # Can't price it -> can't know how much cash selling it frees, so
            # selling it is blind. Skip rather than queue a blind liquidation
            # (a zero price here is what could let de-leveraging "sell
            # everything" when the snapshot is missing prices).
            continue
        reason = (
            f"De-leveraging: cash ${projected_cash:,.2f} below target "
            f"${target_cash:,.2f}; selling weakest holding "
            f"(score {score:.0f})."
        )
        trade = {
            "ticker": ticker,
            "action": "sell",
            "dollar_amount": 0,
            "reasoning": reason,
            "conviction": 10,
        }
        result = execute_trade(trade, account_snapshot, trigger="deleveraging")
        result["trigger"] = "deleveraging"
        if result.get("status") == "submitted":
            projected_cash += notional
        results.append(result)
    return results

def notify(message):
    """
    Optional alerting. Currently posts to a Discord webhook if configured;
    extend with email/Telegram in the same spot. Always logged to console.
    """
    print(f"[ALERT] {message}")
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": message[:1900]}, timeout=10)
    except Exception as e:
        print(f"Could not send alert: {e}")

# ============================================================
# Trade journal & results (win rate by setup)
# ============================================================
JOURNAL_HEADER = [
    "timestamp", "ticker", "action", "qty", "price",
    "stop_loss", "take_profit", "conviction", "confidence", "trigger", "reasoning",
]
RESULTS_HEADER = [
    "closed_at", "opened_at", "ticker", "entry_price", "exit_price",
    "qty", "pnl_pct", "pnl_dollars", "setup", "exit_reason",
]


def _append_csv(path, header, row):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        if new:
            writer.writeheader()
        writer.writerow(row)


def _record_trade_journal(ticker, action, qty, price, stop_loss, take_profit, conviction, confidence, trigger, reasoning):
    """Append one row per fill to logs/trades_journal.csv."""
    try:
        _append_csv(TRADES_JOURNAL_FILE, JOURNAL_HEADER, {
            "timestamp": datetime.now().isoformat(),
            "ticker": ticker,
            "action": action,
            "qty": qty,
            "price": price,
            "stop_loss": stop_loss if stop_loss is not None else "",
            "take_profit": take_profit if take_profit is not None else "",
            "conviction": conviction,
            "confidence": confidence if confidence is not None else "",
            "trigger": trigger,
            "reasoning": (reasoning or "")[:200],
        })
    except Exception as e:
        print(f"Could not write trade journal: {e}")


def _track_open_close(ticker, action, qty, price, stop_loss, take_profit, trigger, reasoning):
    """Pair buys with sells to build closed-trade results (win rate by setup)."""
    try:
        open_trades = _load_json_file(OPEN_TRADES_FILE, {})
        now_iso = datetime.now().isoformat()
        if action == "buy":
            prev = open_trades.get(ticker)
            total_qty = qty + (prev["qty"] if prev else 0.0)
            if prev and prev["qty"] > 0:
                entry = (prev["entry"] * prev["qty"] + price * qty) / total_qty
            else:
                entry = price
            open_trades[ticker] = {
                "qty": total_qty,
                "entry": entry,
                "opened_at": prev["opened_at"] if prev else now_iso,
                "setup": prev["setup"] if prev else (reasoning or ""),
                "stop": prev["stop"] if prev else stop_loss,
            }
        elif action == "sell":
            prev = open_trades.get(ticker)
            if prev and prev["qty"] > 0:
                remaining = prev["qty"] - qty
                pnl_pct = (price - prev["entry"]) / prev["entry"] * 100.0 if prev["entry"] else 0.0
                pnl_dollars = (price - prev["entry"]) * qty
                if remaining <= 0.01:
                    _append_csv(TRADE_RESULTS_FILE, RESULTS_HEADER, {
                        "closed_at": now_iso,
                        "opened_at": prev["opened_at"],
                        "ticker": ticker,
                        "entry_price": round(prev["entry"], 4),
                        "exit_price": price,
                        "qty": prev["qty"],
                        "pnl_pct": round(pnl_pct, 2),
                        "pnl_dollars": round(pnl_dollars, 2),
                        "setup": (prev.get("setup") or "")[:200],
                        "exit_reason": trigger,
                    })
                    open_trades.pop(ticker, None)
                else:
                    prev["qty"] = remaining
        _save_json_file(OPEN_TRADES_FILE, open_trades)
    except Exception as e:
        print(f"Could not track open/close positions: {e}")


def summarize_trade_results():
    """Read closed-trade results and summarize win rate by setup type."""
    if not os.path.exists(TRADE_RESULTS_FILE):
        return "no closed trades yet"
    try:
        with open(TRADE_RESULTS_FILE) as f:
            rows = list(csv.DictReader(f))
    except (OSError, csv.Error):
        return "could not read trade_results.csv"
    if not rows:
        return "no closed trades yet"

    by_setup = {}
    for r in rows:
        setup = (r.get("setup") or "").lower()
        if "news" in setup:
            cat = "news"
        elif "opening-range" in setup or "breakout" in setup:
            cat = "breakout"
        elif "technical" in setup or "score" in setup:
            cat = "technical"
        else:
            cat = "other"
        by_setup.setdefault(cat, []).append(float(r.get("pnl_pct", 0) or 0))
    total = [float(r.get("pnl_pct", 0) or 0) for r in rows]

    def _fmt(name, plist):
        wins = sum(1 for p in plist if p > 0)
        avg = sum(plist) / max(1, len(plist))
        return f"{name}: {len(plist)} trades, {wins / max(1, len(plist)) * 100:.0f}% win rate, avg {avg:+.2f}%"

    parts = [_fmt("all", total)]
    for cat in sorted(by_setup):
        parts.append(_fmt(cat, by_setup[cat]))
    return " | ".join(parts)

# ============================================================
# Phase 3: self-learning statistics -- weight toward what works
# ============================================================
def _setup_category(reasoning):
    """Map a trade's reasoning/trigger to a coarse setup category."""
    text = (reasoning or "").lower()
    if "news" in text or "headline" in text:
        return "news"
    if "opening-range" in text or "breakout" in text:
        return "breakout"
    if "earnings" in text:
        return "earnings"
    if "technical" in text or "score" in text or "rsi" in text or "trend" in text or "macd" in text:
        return "technical"
    return "other"


def _setup_stats():
    """
    Read closed-trade results and return {category: {"n", "wins", "win_rate",
    "avg_pnl_pct"}} plus the overall totals. Empty file -> {}.
    """
    if not os.path.exists(TRADE_RESULTS_FILE):
        return {}
    try:
        with open(TRADE_RESULTS_FILE) as f:
            rows = list(csv.DictReader(f))
    except (OSError, csv.Error):
        return {}
    stats = {}
    for r in rows:
        try:
            pnl = float(r.get("pnl_pct", 0) or 0)
        except (TypeError, ValueError):
            continue
        cat = _setup_category(r.get("setup"))
        s = stats.setdefault(cat, {"n": 0, "wins": 0, "sum": 0.0})
        s["n"] += 1
        s["sum"] += pnl
        if pnl > 0:
            s["wins"] += 1
    out = {}
    for cat, s in stats.items():
        out[cat] = {
            "n": s["n"],
            "wins": s["wins"],
            "win_rate": s["wins"] / s["n"],
            "avg_pnl_pct": s["sum"] / s["n"],
        }
    return out


def get_setup_multiplier(setup_category):
    """
    Phase 3: size entries of a setup by its DEMONSTRATED edge, learned from
    the closed-trade journal. A setup that is winning gets sized up (toward
    SETUP_MULT_MAX); a setup that is losing gets sized down (toward
    SETUP_MULT_MIN); setups with too few samples get exactly 1.0 (no opinion).

    The mapping: win_rate and avg pnl both count. A setup with win_rate >= 55%
    AND positive avg pnl is "proven" and gets up to MAX; one with win_rate
    below 45% OR negative expectancy gets cut toward MIN.
    """
    if not SELF_LEARNING_ENABLED:
        return 1.0
    stats = _setup_stats()
    s = stats.get(setup_category)
    if not s or s["n"] < SELF_LEARNING_MIN_SAMPLES:
        return 1.0  # not enough data -- no opinion yet

    wr = s["win_rate"]
    avg = s["avg_pnl_pct"]
    if wr >= 0.55 and avg > SELF_LEARNING_EDGE_MIN:
        # Proven edge: scale up proportionally to (win_rate, avg) quality.
        quality = min(1.0, (wr - 0.55) / 0.25 + min(0.5, avg / 2.0))
        return round(SETUP_MULT_MIN + (SETUP_MULT_MAX - SETUP_MULT_MIN) * quality, 3)
    if wr <= 0.45 or avg < 0:
        # Demonstrated drag: scale down proportionally to how bad it is.
        badness = min(1.0, (0.45 - wr) / 0.2 + max(0.0, -avg / 2.0))
        return round(SETUP_MULT_MAX - (SETUP_MULT_MAX - SETUP_MULT_MIN) * badness, 3)
    return 1.0


def _live_setup_string(ind_data):
    """
    Build the walk-forward-style indicator-regime setup string from a live
    get_full_indicators() dict. MUST match backtest.py's build_setup_string()
    exactly (same fields, same thresholds) -- this is what lets the live bot
    consult the same gate the backtest learned. Returns None when the data is
    unusable.
    """
    if not ind_data:
        return None
    trend = ind_data.get("trend") or "sideways"
    rsi = ind_data.get("rsi_14")
    if rsi is None:
        rsi_zone = "n/a"
    elif rsi < 30:
        rsi_zone = "oversold"
    elif rsi > 70:
        rsi_zone = "overbought"
    else:
        rsi_zone = "neutral"
    macd = ind_data.get("macd_cross") or "none"
    mom = ind_data.get("momentum_10d")
    mom_zone = "pos" if (mom is not None and mom > 0) else ("neg" if (mom is not None and mom < 0) else "flat")
    vol = ind_data.get("volatility_20d")
    vol_zone = "hi" if (vol is not None and vol >= 2.0) else "lo"
    return f"{trend}|{rsi_zone}|{macd}|{mom_zone}|{vol_zone}"


def _walkforward_gate():
    """
    Load data/setup_gate.json (written by backtest.py --walkforward). Returns
    (gate_set, stats): gate_set is the set of setups proven in the final train
    window, stats is {setup: {n, wins, win_rate, avg_pnl_pct}} from ALL
    closed walk-forward trades. ({}, {}) when absent or unreadable.
    """
    try:
        with open(SETUP_GATE_FILE) as f:
            data = json.load(f) or {}
        return set(data.get("gate") or []), data.get("stats") or {}
    except Exception:
        return set(), {}


def get_walkforward_multiplier(ind_data):
    """
    Phase 3b: live application of the walk-forward learned gate. Computes the
    same indicator-regime setup string the backtest learns on, then sizes the
    entry by that setup's demonstrated edge: a setup that won gets sized toward
    SETUP_MULT_MAX, a proven drag toward SETUP_MULT_MIN, unknown stays 1.0.
    Only kicks in when the gate file exists and has enough samples; never
    raises. The live closed-trade journal (get_setup_multiplier) takes
    precedence once IT has samples -- this fills the gap before then.
    """
    if not WALKFORWARD_LIVE_LEARNING:
        return 1.0
    try:
        gate, stats = _walkforward_gate()
        if not gate and not stats:
            return 1.0
        setup = _live_setup_string(ind_data)
        if not setup:
            return 1.0
        s = stats.get(setup)
        if not s or s.get("n", 0) < WALKFORWARD_MIN_SAMPLES:
            # No reliable stats for this exact setup -- but if the final gate
            # proved it with >= MIN samples in the train window, give it a
            # mild boost rather than no opinion.
            if setup in gate:
                return WALKFORWARD_PROVEN_MULT
            return 1.0
        wr = float(s.get("win_rate", 0.0))
        avg = float(s.get("avg_pnl_pct", 0.0))
        if wr >= 0.55 and avg > 0:
            quality = min(1.0, (wr - 0.55) / 0.25 + min(0.5, avg / 2.0))
            return round(SETUP_MULT_MIN + (SETUP_MULT_MAX - SETUP_MULT_MIN) * quality, 3)
        if wr <= 0.45 or avg < 0:
            badness = min(1.0, (0.45 - wr) / 0.2 + max(0.0, -avg / 2.0))
            return round(SETUP_MULT_MAX - (SETUP_MULT_MAX - SETUP_MULT_MIN) * badness, 3)
        return 1.0
    except Exception:
        return 1.0


def build_performance_brief():
    """
    "What's working" block for the Gemini prompt (Phase 3): the win rate and
    average return per setup category, so the LLM favors setups the bot has
    actually made money on and avoids the ones it keeps losing on.
    """
    if not SELF_LEARNING_ENABLED:
        return "self-learning disabled"
    stats = _setup_stats()
    if not stats:
        return "no closed trades yet -- all setups unproven"
    parts = []
    for cat in sorted(stats):
        s = stats[cat]
        verdict = "WORKING" if (s["win_rate"] >= 0.55 and s["avg_pnl_pct"] > 0) else ("LOSING" if s["avg_pnl_pct"] < 0 else "neutral")
        parts.append(
            f"{cat}: {s['n']} trades, {s['win_rate'] * 100:.0f}% win rate, "
            f"avg {s['avg_pnl_pct']:+.2f}% ({verdict})"
        )
    return " | ".join(parts)


def get_economic_event_multiplier():
    """
    On a day with an upcoming high-impact economic event (CPI / FOMC / NFP /
    GDP / PCE), new buys are sized down to HIGH_IMPACT_EVENT_SIZE_MULT.
    Pure cache read; returns 1.0 on any failure or when disabled.
    """
    if not ENABLE_ECONOMIC_CALENDAR:
        return 1.0
    try:
        from data_feeds import high_impact_event_today
        hit, _desc = high_impact_event_today()
        return HIGH_IMPACT_EVENT_SIZE_MULT if hit else 1.0
    except Exception as e:
        print(f"Economic event check unavailable (continuing at full size): {e}")
        return 1.0


# ============================================================
# Second-trader / foreign-activity detection
# ============================================================
# Set when this process detects the Alpaca account changed (new API keys /
# new paper account), so later steps in the same run (circuit breakers) can
# reset their own cached state without re-deriving the detection.
_ACCOUNT_CHANGED_THIS_RUN = False


def _account_changed(account_id, state):
    """True when `state` was recorded under a different Alpaca account id."""
    if not account_id:
        return False  # no identity to compare against -- treat as unchanged
    recorded = state.get("account_id")
    return recorded is not None and recorded != account_id


def _detect_account_change(account_snapshot):
    """
    Detect that the Alpaca account this bot is now talking to is not the one
    its cached state was built against (new paper account / keys rotated to a
    different account).

    Two signals, either of which counts:
      1. The recorded account_id (stored by current code) differs from the
         live account id.
      2. Legacy state written before account_id existed: the account holds
         NONE of the baseline tickers AND this bot has no FILLED orders on
         record. That combination means the baseline belongs to a different
         account -- NOT a second trader (a real liquidation on the same
         account would still show the bot's own filled sells in the ledger).
    """
    account_id = account_snapshot.get("account_id", "")
    recon = _load_json_file(RECON_STATE_FILE, {})
    if _account_changed(account_id, recon):
        return True
    baseline = recon.get("baseline") or {}
    if not baseline:
        return False
    holdings = account_snapshot.get("holdings", {})
    if any(t in holdings for t in baseline):
        return False  # still holds baseline names -> same account
    ledger = _load_json_file(ORDER_LEDGER_FILE, [])
    if any(_status_val(o.get("order_status", "")) == "filled" for o in ledger):
        return False  # bot has filled orders on this account -> same account
    return True


def _append_order_to_ledger(entry):
    """Record every order this bot submits so holdings can be reconciled."""
    try:
        ledger = _load_json_file(ORDER_LEDGER_FILE, [])
        ledger.append(entry)
        _save_json_file(ORDER_LEDGER_FILE, ledger)
    except Exception as e:
        print(f"Could not write order ledger: {e}")

def _status_val(v):
    """
    Return the plain lowercase value of an Alpaca status/side field.

    alpaca-py returns ENUM objects (OrderStatus.FILLED), and str() of those is
    'OrderStatus.FILLED' -- while every comparison in this module uses plain
    strings like 'filled'. Storing str(order.status) in the ledger and then
    comparing against 'filled' silently NEVER matched (the 2026-08-13 CVX
    false-alarm root cause: the heal FOUND the filled order and rejected it
    because 'orderstatus.filled' != 'filled'). This normalizes all three
    shapes the field can arrive in:

        OrderStatus.FILLED  (enum)     -> 'filled'
        'OrderStatus.FILLED' (string)  -> 'filled'   (old ledger entries)
        'filled'            (string)   -> 'filled'
    """
    if hasattr(v, "value"):
        v = v.value
    s = str(v or "").lower()
    if "." in s:
        s = s.split(".")[-1]
    return s


_LEDGER_IN_FLIGHT_STATUSES = {
    "accepted", "pending_new", "pending_replace", "new",
    "submitted", "held", "partially_filled", "pending_cancel",
}


def _refresh_ledger_statuses():
    """
    Sync the order ledger with reality before reconciling.

    Ledger entries are written with the order's status at SUBMISSION time
    (e.g. 'accepted' for an after-hours DAY order that Alpaca holds until the
    open). If that status is never updated, a sell that filled at the open
    still looks 'pending' forever -- so get_expected_holdings() ignores it and
    the reconciliation accuses the bot's OWN fills of being a second trader:

        FOREIGN ACTIVITY: AAPL holds 0.0000 sh but this bot should own 12.8 sh

    This queries Alpaca once for recent orders (open + closed) and rewrites
    any in-flight ledger entry to its real final status (filled / canceled /
    expired / rejected). One API call per run; no-op when nothing is in
    flight. Orders we can't find (purged from Alpaca's history) are left as-is
    rather than guessed at.
    """
    try:
        ledger = _load_json_file(ORDER_LEDGER_FILE, [])
        if not ledger:
            return 0
        needs_refresh = [
            e for e in ledger
            if _status_val(e.get("order_status", "")) in _LEDGER_IN_FLIGHT_STATUSES
        ]
        if not needs_refresh:
            return 0
        orders = trading_client.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL))
        by_id = {}
        for o in orders:
            oid = str(getattr(o, "id", "") or "").strip()
            status = _status_val(getattr(o, "status", ""))
            if oid:
                by_id[oid] = status
        changed = 0
        for entry in ledger:
            oid = str(entry.get("order_id", "") or "").strip()
            status = _status_val(entry.get("order_status", ""))
            if status not in _LEDGER_IN_FLIGHT_STATUSES or not oid or oid == "x":
                continue
            resolved = by_id.get(oid)
            if resolved and resolved != status:
                entry["order_status"] = resolved
                changed += 1
        if changed:
            _save_json_file(ORDER_LEDGER_FILE, ledger)
            print(f"Order ledger refreshed: {changed} order(s) updated to their final status.")
        return changed
    except Exception as e:
        print(f"Could not refresh order ledger statuses: {e}")
        return 0


def get_expected_holdings():
    """
    Recompute the quantities this bot should own from its order ledger:
    returns {ticker: qty} summing buys minus sells. Empty ledger -> {}.

    Only orders that actually FILLED count. Pending / canceled / rejected
    orders are excluded so a still-working order doesn't create a false
    'foreign activity' flag on the next reconciliation run. (Ledger statuses
    are kept current by _refresh_ledger_statuses(), called before
    reconciliation, so a queued order that filled at the open is counted.)
    """
    ledger = _load_json_file(ORDER_LEDGER_FILE, [])
    expected = {}
    for o in ledger:
        t = o.get("ticker")
        side = str(o.get("action", "")).lower()
        status = _status_val(o.get("order_status", ""))
        if status and status != "filled":
            # Pending (PENDING_NEW / new / submitted), canceled, expired,
            # rejected orders haven't changed holdings (yet).
            continue
        try:
            qty = float(o.get("qty", 0.0))
        except (TypeError, ValueError):
            continue
        if side == "buy":
            expected[t] = expected.get(t, 0.0) + qty
        elif side == "sell":
            expected[t] = expected.get(t, 0.0) - qty
    return {t: q for t, q in expected.items() if abs(q) > 0.0001}

def _heal_ledger_from_orders(holdings, expected):
    """
    Recover bot orders whose ledger entries were lost.

    The order ledger is the source of truth for "what this bot owns", but its
    persistence depends on the per-run git commit succeeding. If that step
    fails (e.g. a merge conflict in the shared state files), a filled order
    submitted that run never reaches GitHub -- and the next run's
    reconciliation accuses the bot of its OWN fill:

        FOREIGN ACTIVITY: CVX position (24.88 sh) was never created by this bot.

    Before declaring a position foreign, cross-check Alpaca's own order
    history: a filled buy on this account was placed by this bot (it is the
    only automated trader on these keys). Re-add it to the ledger so the
    reconciliation matches reality. Only positions with NO ledger/baseline
    explanation are healed -- partial quantity mismatches still flag normally.
    Returns the number of orders recovered.
    """
    missing = {}
    for t, p in holdings.items():
        held = p.get("qty", 0.0) if isinstance(p, dict) else 0.0
        if held > 0.0001 and abs(expected.get(t, 0.0)) <= 0.0001:
            missing[t] = held
    if not missing:
        return 0
    try:
        # Scan ALL recent orders (no symbol filter). A symbols-scoped query is
        # tempting but has proven unreliable for recovery -- the order for a
        # position sitting in the account can fail to come back under
        # status=all+symbols while the same order IS returned unfiltered (the
        # exact query _refresh_ledger_statuses uses successfully). limit=500
        # also beats the 50-order default so the lost entry can't hide.
        orders = trading_client.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.ALL,
            limit=500,
        ))
    except Exception as e:
        print(f"Ledger self-heal skipped (order history unavailable): {e}")
        return 0
    ledger = _load_json_file(ORDER_LEDGER_FILE, [])
    existing_ids = {str(e.get("order_id", "") or "").strip() for e in ledger}
    added = 0
    seen = {t: [] for t in missing}
    for o in orders:
        symbol = str(getattr(o, "symbol", "") or "").upper()
        side = _status_val(getattr(o, "side", ""))
        status = _status_val(getattr(o, "status", ""))
        if symbol in seen:
            seen[symbol].append(f"{side}/{status}")
        if status not in ("filled", "partially_filled") or side != "buy" or symbol not in missing:
            continue
        oid = str(getattr(o, "id", "") or "").strip()
        if not oid or oid == "x" or oid in existing_ids:
            continue
        try:
            qty = float(getattr(o, "filled_qty", None) or getattr(o, "qty", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        ledger.append({
            "timestamp": str(getattr(o, "submitted_at", "") or datetime.now().isoformat()),
            "ticker": symbol,
            "action": "buy",
            "qty": qty,
            "order_id": oid,
            "order_status": "filled",
            "healed_from_order_history": True,
        })
        existing_ids.add(oid)
        added += 1
    if added:
        _save_json_file(ORDER_LEDGER_FILE, ledger)
        print(f"Order ledger self-healed: {added} filled order(s) recovered from Alpaca order history (this bot's own fills whose ledger entries were lost).")
    else:
        detail = "; ".join(
            f"{t}: {', '.join(v) if v else 'NO ORDERS AT ALL'}" for t, v in seen.items()
        )
        print(f"Ledger self-heal: no buy fill found in Alpaca order history for {sorted(missing)} [{detail}] -- if these positions are not yours, that is REAL foreign activity.")
    return added


def reconcile_foreign_activity(account_snapshot):
    """
    Compare actual account holdings against what the bot's own order ledger
    says it should own. Returns (flags, baseline_created):

      flags: list of strings describing positions the bot did not create or
             quantities that changed without the bot's orders. Empty means
             the account matches the bot exactly.
      baseline_created: True on the very first run after deploy, when there
             is no ledger history yet (the account's pre-existing positions
             can't be attributed to this bot -- flag them once so you can
             verify, then treat them as the baseline going forward).

    Catches a second bot / local cron / manual trades on the same Alpaca keys.
    """
    if not ENABLE_FOREIGN_ACTIVITY_DETECTION:
        return [], False
    # Bring the ledger up to date first: otherwise a sell that filled at the
    # open still reads as 'accepted' and this bot accuses its OWN fills of
    # being a second trader (the 2026-08-12 false-alarm storm).
    try:
        _refresh_ledger_statuses()
    except Exception as e:
        print(f"Ledger refresh failed (reconciling anyway): {e}")
    account_id = account_snapshot.get("account_id", "")

    # New account / new API keys: the cached baseline, order ledger, and open
    # trade state were built against a DIFFERENT Alpaca account and must not
    # be reconciled (that produced the 2026-08-12 'SECOND TRADER' storm after
    # the account switch). Reset them so the new account starts clean.
    if _detect_account_change(account_snapshot):
        global _ACCOUNT_CHANGED_THIS_RUN
        _ACCOUNT_CHANGED_THIS_RUN = True
        print("ACCOUNT CHANGED (new API keys / new Alpaca account detected): resetting reconciliation baseline, order ledger, and open-trade state for the new account.")
        _save_json_file(RECON_STATE_FILE, {"account_id": account_id})
        _save_json_file(ORDER_LEDGER_FILE, [])
        _save_json_file(OPEN_TRADES_FILE, {})

    holdings = account_snapshot.get("holdings", {})
    recon = _load_json_file(RECON_STATE_FILE, {})
    flags = []

    # Key-presence check (not dict truthiness): a NEW account with zero
    # positions legitimately has an EMPTY baseline -- that must not be treated
    # as 'no baseline' on the next run, or the bot would re-baseline over its
    # own first buys and double-count them as foreign activity.
    if "baseline" not in recon:
        # First run: no ledger history for the pre-existing account. Record
        # current quantities as the baseline and surface them once so the
        # user can verify them in Alpaca's Activity log.
        baseline = {t: p["qty"] for t, p in holdings.items()}
        recon["account_id"] = account_id
        recon["baseline"] = baseline
        _save_json_file(RECON_STATE_FILE, recon)
        if holdings:
            flags.append(
                "FIRST RUN BASELINE: these holdings existed before this deploy "
                "(no order history on record): " + ", ".join(
                    f"{t} x{baseline[t]:.4f}" for t in sorted(baseline)
                ) + ". Verify them in the Alpaca dashboard Activity log."
            )
        return flags, True

    baseline = recon["baseline"]
    expected = dict(baseline)
    for t, q in get_expected_holdings().items():
        expected[t] = expected.get(t, 0.0) + q

    # Self-heal: a holding entirely unexplained by the ledger + baseline is
    # usually this bot's own fill whose ledger entry was lost (failed git
    # commit). Recover it from Alpaca's order history before flagging it.
    if _heal_ledger_from_orders(holdings, expected):
        expected = dict(baseline)
        for t, q in get_expected_holdings().items():
            expected[t] = expected.get(t, 0.0) + q

    for t, q in expected.items():
        if abs(q) <= 0.0001:
            continue
        actual = holdings.get(t, {}).get("qty", 0.0)
        if abs(actual - q) > 0.01:
            flags.append(
                f"FOREIGN ACTIVITY: {t} holds {actual:.4f} sh but this bot "
                f"should own {q:.4f} sh (diff {actual - q:+.4f}). Someone/something "
                "else is trading this account."
            )
    for t in holdings:
        if abs(expected.get(t, 0.0)) <= 0.0001:
            flags.append(
                f"FOREIGN ACTIVITY: {t} position ({holdings[t]['qty']:.4f} sh) "
                "was never created by this bot."
            )
    return flags, False

def flatten_portfolio(account_snapshot, reason="Circuit breaker: flattening portfolio on deep drawdown.", trigger="circuit_breaker_flatten"):
    """
    Sell every held position (skipping tickers that already have open orders)
    to take the portfolio to cash. Used by the deep-drawdown circuit breaker
    and the daytrading end-of-day flatten.
    """
    results = []
    open_orders = get_tickers_with_open_orders()
    for ticker, pos in account_snapshot.get("holdings", {}).items():
        if ticker in open_orders:
            continue
        if pos.get("qty", 0) <= 0:
            continue
        trade = {
            "ticker": ticker,
            "action": "sell",
            "dollar_amount": 0,
            "reasoning": reason,
            "conviction": 10,
        }
        result = execute_trade(trade, account_snapshot, trigger=trigger)
        result["trigger"] = trigger
        results.append(result)
    return results

def should_end_of_day_flatten():
    """
    Daytrading discipline: after END_OF_DAY_FLATTEN_TIME ET, sell everything
    so the account never carries overnight risk (the 2026-08-11 liquidation
    hit an overnight position at 3:30 AM ET). Only active when DAYTRADE_MODE
    and END_OF_DAY_FLATTEN are enabled.
    """
    if not (DAYTRADE_MODE and END_OF_DAY_FLATTEN):
        return False
    now_et = datetime.now(pytz.utc).astimezone(_EASTERN)
    cutoff = datetime.strptime(END_OF_DAY_FLATTEN_TIME, "%H:%M").time()
    return now_et.time() >= cutoff

def is_within_trade_window():
    """
    Daytrading entry window: no new buys in the first N minutes after the
    open (auction chop) and none after STOP_NEW_BUYS_AFTER ET. Sells are
    never restricted by this. Inactive when DAYTRADE_MODE is off.
    """
    if not DAYTRADE_MODE:
        return True
    try:
        now_et = datetime.now(pytz.utc).astimezone(_EASTERN)
        t = now_et.time()
        open_t = datetime.strptime("09:30", "%H:%M").time()
        start_t = (datetime.combine(datetime.now().date(), open_t) + timedelta(minutes=TRADE_START_MINUTES_AFTER_OPEN)).time()
        stop_t = datetime.strptime(STOP_NEW_BUYS_AFTER, "%H:%M").time()
        # The extended session is always in-window when extended hours are
        # allowed (after-hours entries are wanted; fills use limit orders).
        if ALLOW_EXTENDED_HOURS and is_extended_session(now_et):
            return True
        if t < open_t:
            return False
        if t < start_t:
            return False
        if t > stop_t:
            return False
        # Outside the regular session and not in the extended session (the
        # dead zone, e.g. 9-10 PM): no entries.
        close_t = datetime.strptime("16:00", "%H:%M").time()
        if t >= close_t:
            return False
        return True
    except Exception:
        return True

def get_time_of_day_multiplier(now_et=None):
    """
    Daytrading edge windows: full size in the open power hour and the closing
    push, reduced size through the lunch lull. Sells are never affected.
    Returns 1.0 when DAYTRADE_MODE is off. 'now_et' is injectable for tests.
    """
    if not DAYTRADE_MODE:
        return 1.0
    try:
        if now_et is None:
            now_et = datetime.now(pytz.utc).astimezone(_EASTERN)
        minutes = now_et.hour * 60 + now_et.minute
        for (sh, sm, eh, em), mult in TIME_OF_DAY_MULTIPLIERS.items():
            if sh * 60 + sm <= minutes < eh * 60 + em:
                return mult
        return 0.8
    except Exception:
        return 1.0


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

def _compute_exit_levels(ticker, trade, entry_price, ind=None):
    """
    Compute this trade's stop-loss / take-profit from ITS OWN setup: recent
    swing high/low clamped to a sane ATR multiple, or Gemini's explicit
    stop_loss/take_profit when provided (and allowed). Returns
    (stop_loss, take_profit) -- either may be None if no ATR/history exists.

    'ind' is an optional precomputed get_full_indicators() dict (avoids a
    duplicate price-history fetch when the caller already has it).
    """
    atr = None
    swing_low = swing_high = None
    if ind is not None:
        atr = ind.get("atr_14")
        swing_low = ind.get(f"recent_swing_low_{SWING_LOOKBACK_DAYS}d")
        swing_high = ind.get(f"recent_swing_high_{SWING_LOOKBACK_DAYS}d")
    if atr is None or swing_low is None or swing_high is None:
        try:
            # FIX (real): the `ind` parameter here is a DICT (the
            # get_full_indicators() result) -- it shadows the `indicators as
            # ind` MODULE import at the top of this file, so the old
            # `ind.compute_atr(...)` raised 'dict' object has no attribute
            # 'compute_atr' and chart-based exit levels were silently NEVER
            # computed (Gemini's stop_loss/take_profit stayed dead code for
            # any ticker whose dict lacked ATR/swings). Use the module under
            # an unambiguous name. Also use the full default lookback so
            # get_price_history() actually returns bars (a 30-day fetch
            # returns None -- Alpaca needs 55+ bars).
            history = get_price_history(ticker)
            if history:
                import indicators as _ind_mod
                atr = _ind_mod.compute_atr(history["highs"], history["lows"], history["closes"], period=ATR_PERIOD)
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

    # Sell-side risk parity: a tight target on a wide stop is a losing
    # proposition -- the winner can't pay for the losers. If both levels exist
    # and the take-profit is closer than MIN_REWARD_RISK_RATIO x the stop
    # distance, push the target out to that ratio (still capped at the max
    # ATR take-profit distance so the level stays sane).
    if (
        MIN_REWARD_RISK_RATIO > 0
        and stop_loss is not None
        and take_profit is not None
        and atr
        and atr > 0
    ):
        risk = entry_price - stop_loss
        if risk > 0:
            current_rr = (take_profit - entry_price) / risk
            if current_rr < MIN_REWARD_RISK_RATIO:
                tp = entry_price + MIN_REWARD_RISK_RATIO * risk
                tp = min(tp, entry_price + MAX_TAKE_PROFIT_DISTANCE_ATR_MULT * atr)
                if tp > entry_price:
                    take_profit = round(tp, 2)
    return stop_loss, take_profit


def _record_custom_exit(ticker, trade, entry_price, levels=None):
    """Persist precomputed (or freshly computed) exit levels for a position."""
    if levels is None:
        levels = _compute_exit_levels(ticker, trade, entry_price)
    stop_loss, take_profit = levels
    if stop_loss is None or take_profit is None:
        return None

    exits = _load_custom_exits()
    exits[ticker] = {
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "entry_price": entry_price,
        "set_at": datetime.now().isoformat(),
    }
    _save_custom_exits(exits)
    return exits[ticker]

def check_atr_stop_take_profit(account_snapshot):
    results = []
    open_order_tickers = get_tickers_with_open_orders()
    custom_exits = _load_custom_exits()
    holdings = account_snapshot["holdings"]
    news_sentiment_cache = _load_json_file(NEWS_SENTIMENT_CACHE_FILE, {})
    exits_modified = False

    for ticker, pos in holdings.items():
        if ticker in open_order_tickers:
            continue
        entry = pos["avg_entry_price"]
        current = pos["current_price"]

        indicators_data = get_full_indicators(ticker)
        atr = indicators_data["atr_14"] if indicators_data else None

        custom = custom_exits.get(ticker)
        if custom and custom.get("stop_loss") is not None and custom.get("take_profit") is not None:
            stop_level = custom["stop_loss"]
            target_level = custom["take_profit"]
        else:
            if atr is None:
                # Never let a data hiccup leave a position unprotected: fall
                # back to a hard %-of-entry loss cap instead of skipping.
                stop_level = entry * (1.0 - MAX_POSITION_LOSS_PCT / 100.0)
                target_level = None
            else:
                stop_level = entry - (ATR_STOP_MULTIPLIER * atr)
                target_level = entry + (ATR_TAKE_PROFIT_MULTIPLIER * atr)

        # Trailing stop: once the position is up TRAILING_STOP_ACTIVATE_MULT x
        # ATR from entry, ratchet the stop up to (best price -
        # TRAILING_STOP_DISTANCE_MULT x ATR). The stop only ever moves up, so
        # winners are banked instead of given back. Persisted in custom_exits.
        if atr and atr > 0 and current >= entry + TRAILING_STOP_ACTIVATE_MULT * atr:
            best_price = max(float(custom.get("best_price", entry)) if custom else entry, current)
            trail_stop = round(max(stop_level, best_price - TRAILING_STOP_DISTANCE_MULT * atr), 2)
            if trail_stop > stop_level:
                if custom is None:
                    custom = {
                        "stop_loss": stop_level,
                        "take_profit": target_level,
                        "entry_price": entry,
                    }
                    custom_exits[ticker] = custom
                custom["stop_loss"] = trail_stop
                custom["best_price"] = best_price
                stop_level = trail_stop
                exits_modified = True

        # Scale-out (partial profit taking): when a winner reaches a fraction
        # of the way to its take-profit, bank a slice of the position -- locked
        # gains can't be given back, and the trailing stop keeps ratcheting on
        # the remainder (which exits at the target or on stop). One-shot per
        # position, flagged in custom_exits so it never double-fires. Only
        # when a target exists and the position is big enough to split.
        if (
            ENABLE_SCALE_OUT
            and target_level is not None
            and target_level > entry
            and current >= entry + SCALE_OUT_AT_RR_FRAC * (target_level - entry)
            and not (custom and custom.get("scaled_out_1"))
        ):
            qty_owned = float(pos.get("qty", 0.0))
            sell_qty = round(qty_owned * SCALE_OUT_FRAC, 4)
            if sell_qty >= 0.01:
                if custom is None:
                    custom = {
                        "stop_loss": stop_level,
                        "take_profit": target_level,
                        "entry_price": entry,
                    }
                    custom_exits[ticker] = custom
                custom["scaled_out_1"] = True
                exits_modified = True
                trade = {
                    "ticker": ticker,
                    "action": "sell",
                    # dollar_amount = qty * price so execute_trade sells EXACTLY
                    # this many shares (its sell path divides by price).
                    "dollar_amount": sell_qty * current,
                    "reasoning": (
                        f"Scale-out: banked {SCALE_OUT_FRAC:.0%} at {round(current, 2)} "
                        f"({SCALE_OUT_AT_RR_FRAC:.0%} of the way to target {round(target_level, 2)})"
                    ),
                    "conviction": 10,
                }
                result = execute_trade(trade, account_snapshot, trigger="scale_out")
                result["trigger"] = "scale_out"
                results.append(result)

        # Hard per-position loss cap, always enforced regardless of ATR/indicators.
        hard_stop = entry * (1.0 - MAX_POSITION_LOSS_PCT / 100.0)

        reason = None
        if current <= stop_level:
            reason = f"Stop-loss hit: price {current} <= stop {round(stop_level, 2)}"
        elif target_level is not None and current >= target_level:
            reason = f"Take-profit hit: price {current} >= target {round(target_level, 2)}"
        elif current <= hard_stop:
            reason = f"Hard loss cap hit: price {current} <= {round(hard_stop, 2)} (-{MAX_POSITION_LOSS_PCT:.0f}% from entry {entry})"

        # Smarter exits, using the indicator data already fetched above.
        if reason is None and ENABLE_MA_BREAKDOWN_EXIT and indicators_data:
            sma20 = indicators_data.get("sma_20")
            if sma20 and current < sma20:
                reason = f"Moving-average breakdown: price {current} < SMA-20 {round(sma20, 2)}"
        if reason is None and ENABLE_RSI_EXHAUSTION_EXIT and indicators_data:
            rsi = indicators_data.get("rsi_14")
            if rsi is not None and rsi > RSI_EXHAUSTION_LEVEL:
                reason = f"RSI exhaustion: RSI {rsi:.1f} > {RSI_EXHAUSTION_LEVEL:.0f}"
        if reason is None and ENABLE_NEGATIVE_NEWS_EXIT:
            worst_sent = news_sentiment_cache.get(ticker)
            if worst_sent is not None and worst_sent <= NEGATIVE_NEWS_SENTIMENT_THRESHOLD:
                reason = f"Negative news: worst sentiment {worst_sent:+.2f}"

        if reason:
            trade = {"ticker": ticker, "action": "sell", "dollar_amount": 0, "reasoning": reason, "conviction": 10}
            if "Take-profit" in reason:
                exit_trigger = "take_profit"
            elif "Hard loss cap" in reason:
                exit_trigger = "hard_loss_cap"
            elif "Moving-average" in reason:
                exit_trigger = "ma_breakdown"
            elif "RSI exhaustion" in reason:
                exit_trigger = "rsi_exhaustion"
            elif "Negative news" in reason:
                exit_trigger = "negative_news"
            else:
                exit_trigger = "stop_loss"
            result = execute_trade(trade, account_snapshot, trigger=exit_trigger)
            result["trigger"] = exit_trigger
            results.append(result)

    if exits_modified:
        _save_custom_exits(custom_exits)
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
            result = execute_trade(trade, account_snapshot, trigger="portfolio_consolidation")
            result["trigger"] = "portfolio_consolidation"
            results.append(result)

    return results

def enforce_quality_trim(account_snapshot):
    """
    Conservative quality-trim of LEGACY holdings -- the pre-baseline positions
    recorded in reconciliation_state.json's baseline that predate this bot and
    were never screened by this strategy (the ~$89K drag). Two passes, sharing
    the QUALITY_TRIM_MAX_PER_RUN cap:
      1. Profit-take: any legacy position up >= QUALITY_TRIM_PROFIT_TAKE_PCT
         since its avg entry price is sold to bank the gain (score ignored).
      2. Score-based: legacy positions that FAIL the current technical screens
         (signal score below QUALITY_TRIM_SCORE_THRESHOLD), worst first, but
         ONLY when the position is not down overall (current >= entry) -- a
         legacy loser is held long until it recovers to the +5% profit-take
         mark instead of being sold into weakness (never sell into the hole).
    Skips tickers with open orders. Non-legacy positions are never touched.
    Returns the executed trade results (empty when nothing qualifies).
    """
    if not ENABLE_QUALITY_TRIM:
        print("Quality trim: disabled via ENABLE_QUALITY_TRIM=false -- skipping.")
        return []
    recon = _load_json_file(RECON_STATE_FILE, {})
    baseline = recon.get("baseline") or {}
    if not baseline:
        print("Quality trim: no legacy baseline recorded yet (reconciliation_state.json empty) -- nothing to trim.")
        return []  # no legacy positions recorded on this account
    holdings = account_snapshot.get("holdings", {})
    open_orders = get_tickers_with_open_orders()
    candidates = [t for t in holdings if t in baseline and t not in open_orders]
    if not candidates:
        print(f"Quality trim: {len(holdings)} holdings, none are legacy baseline names (or all have open orders) -- nothing to trim.")
        return []
    scored = []
    for t in candidates:
        try:
            ind_data = get_full_indicators(t)
            score = calculate_signal_score(ind_data)
        except Exception as e:
            print(f"Quality trim: could not score {t} (skipping it): {e}")
            continue
        scored.append((score, t))
    scored.sort(key=lambda x: x[0])  # worst first

    results = []
    slots_left = QUALITY_TRIM_MAX_PER_RUN
    loss_guard_skips = 0

    def _sell_legacy(t, reason):
        nonlocal slots_left
        trade = {"ticker": t, "action": "sell", "dollar_amount": 0, "reasoning": reason, "conviction": 10}
        result = execute_trade(trade, account_snapshot, trigger="quality_trim")
        result["trigger"] = "quality_trim"
        results.append(result)
        slots_left -= 1

    # Pass 1: profit-take on legacy winners -- any legacy position up
    # QUALITY_TRIM_PROFIT_TAKE_PCT or more since its avg entry price is sold
    # to bank the gain. Score is ignored here (a winner is a winner).
    profit_takers = []
    for t in candidates:
        pos = holdings.get(t, {})
        entry = float(pos.get("avg_entry_price", 0) or 0)
        current = float(pos.get("current_price", 0) or 0)
        if entry > 0 and current > 0 and (current / entry - 1.0) * 100.0 >= QUALITY_TRIM_PROFIT_TAKE_PCT:
            profit_takers.append(t)
    for t in sorted(profit_takers):
        if slots_left <= 0:
            break
        pos = holdings.get(t, {})
        entry = float(pos.get("avg_entry_price", 0) or 0)
        current = float(pos.get("current_price", 0) or 0)
        gain_pct = (current / entry - 1.0) * 100.0 if entry else 0.0
        reason = (
            f"Quality trim profit-take: legacy pre-baseline holding {t} is up "
            f"{round(gain_pct, 1)}% since entry >= {QUALITY_TRIM_PROFIT_TAKE_PCT}%; "
            "banking the gain"
        )
        _sell_legacy(t, reason)

    # Pass 2: score-based trim of legacy positions failing the technical
    # screens (signal score below QUALITY_TRIM_SCORE_THRESHOLD), worst first,
    # but ONLY for positions that are not down overall (current >= entry): a
    # legacy loser is held long until it recovers to the +5% profit-take
    # mark instead of being sold into weakness. Never sells into the hole.
    # Uses whatever per-run slots remain.
    for score, t in scored:
        if slots_left <= 0:
            break
        if score >= QUALITY_TRIM_SCORE_THRESHOLD:
            break  # sorted ascending -- the rest are even better
        if t in profit_takers:
            continue  # already handled (or already sold) in pass 1
        pos = holdings.get(t, {})
        entry = float(pos.get("avg_entry_price", 0) or 0)
        current = float(pos.get("current_price", 0) or 0)
        if entry > 0 and current > 0 and current < entry:
            loss_guard_skips += 1
            continue  # legacy loser -- hold long until it recovers to +5%
        if entry > 0 and current > 0 and current <= entry * (1.0 - QUALITY_TRIM_LOSS_GUARD_PCT / 100.0):
            loss_guard_skips += 1
            continue  # already near the loss guard -- don't sell into the hole
        reason = (
            f"Quality trim: legacy pre-baseline holding {t} fails technical screens "
            f"(signal score {round(score, 1)} < {QUALITY_TRIM_SCORE_THRESHOLD}); freeing "
            "capital for higher-conviction entries"
        )
        _sell_legacy(t, reason)

    if not results and scored:
        worst_score = scored[0][0]
        if worst_score >= QUALITY_TRIM_SCORE_THRESHOLD:
            why = f"worst legacy score {round(worst_score, 1)} >= threshold {QUALITY_TRIM_SCORE_THRESHOLD}"
        else:
            why = (f"worst legacy score {round(worst_score, 1)} < threshold {QUALITY_TRIM_SCORE_THRESHOLD} "
                   f"but {loss_guard_skips} held back (down overall or near entry)")
        print(f"Quality trim: checked {len(scored)} legacy position(s); {why} -- nothing sold this run.")
    return results

def confidence_to_size_pct(confidence):
    """
    Map Gemini confidence (0-100) to a target position size as a fraction of
    portfolio equity: 90+ -> 8%, 80+ -> 5%, 70+ -> 3%, 60+ -> 2%, below 60
    -> 0 (not tradeable). Falls back to 0 for missing/invalid input.
    """
    try:
        c = float(confidence)
    except (TypeError, ValueError):
        return 0.0
    for threshold, pct in CONFIDENCE_SIZING:
        if c >= threshold:
            return pct
    return 0.0


def _sector_room_for(account_snapshot, ticker, total_value):
    """
    How many MORE dollars of `ticker`'s GICS sector the account can still buy
    before MAX_SECTOR_EXPOSURE_PCT, ignoring this ticker's own pending amount.
    Returns (room, sector_name): room >= 0; sector_name None when the sector
    is unknown (no cap applies). Cache read (free Finnhub profile2, 24h).
    """
    from data_feeds import get_sector_profiles

    holdings = account_snapshot.get("holdings", {}) or {}
    sectors = get_sector_profiles(list(holdings.keys()) + [ticker])
    target_sector = (sectors.get(ticker) or {}).get("sector")
    if not target_sector:
        return float("inf"), None

    sector_value = 0.0
    for t, pos in holdings.items():
        if t == ticker:
            continue
        sec = (sectors.get(t) or {}).get("sector")
        if sec == target_sector:
            sector_value += float(pos.get("qty", 0.0)) * float(pos.get("current_price", 0.0))
    room = total_value * MAX_SECTOR_EXPOSURE_PCT - sector_value
    return max(0.0, room), target_sector


def _chase_size_multiplier(extension_pct, limit_pct):
    """
    Soft chase filter: how much of a full position an extended name earns.

    extension <= limit                -> 1.0  (full size)
    up to 2x the limit                -> 0.60
    up to 3x the limit                -> 0.35
    beyond, up to CHASE_HARD_SKIP_MULT x -> 0.15
    beyond CHASE_HARD_SKIP_MULT x     -> 0.0  (hard skip: truly priced in)

    With CHASE_HARD_SKIP_MULT <= 0 the hard skip is disabled and the worst
    case is 0.15 -- a very extended name is still tradeable, just small.
    """
    if limit_pct <= 0 or extension_pct <= limit_pct:
        return 1.0
    if CHASE_HARD_SKIP_MULT and CHASE_HARD_SKIP_MULT > 0 and extension_pct > limit_pct * CHASE_HARD_SKIP_MULT:
        return 0.0
    ratio = extension_pct / limit_pct
    if ratio <= 2.0:
        return 0.60
    if ratio <= 3.0:
        return 0.35
    return 0.15


def execute_trade(trade, account_snapshot=None, size_multiplier=1.0, trigger=None):
    ticker = trade["ticker"]
    action = trade["action"].lower()
    requested_amount = float(trade.get("dollar_amount") or 0)
    conviction = max(1, min(10, int(trade.get("conviction", 5))))
    if trigger is None:
        trigger = str(trade.get("trigger", "decision"))

    if account_snapshot is None:
        account_snapshot = get_account_snapshot()

    price = get_price(ticker)
    if price is None or price <= 0:
        return {"ticker": ticker, "status": "failed", "reason": "no valid price data"}

    total_value = account_snapshot["total_value"]
    current_holding = account_snapshot["holdings"].get(ticker)
    current_position_value = (current_holding["qty"] * price) if current_holding else 0.0

    if action == "buy":
        # Hard no-margin rule: never buy into a negative-cash account. This is
        # the direct consequence of the 2026-08-07 overnight order stacking.
        if account_snapshot.get("cash", 0.0) < 0:
            return {
                "ticker": ticker,
                "status": "skipped",
                "reason": f"no-margin rule: cash is negative (${account_snapshot['cash']:,.2f}); de-leveraging first",
            }

        # Confidence: a GATE in flat mode (skip below the bar, size unchanged)
        # and a SIZER in tiered mode (converts the 0-100 score to % of equity).
        if FLAT_SIZING:
            if trade.get("confidence") is not None:
                try:
                    if float(trade["confidence"]) < CONFIDENCE_MIN_TO_TRADE:
                        return {
                            "ticker": ticker,
                            "status": "skipped",
                            "reason": f"confidence {trade['confidence']} below tradeable bar ({CONFIDENCE_MIN_TO_TRADE})",
                        }
                except (TypeError, ValueError):
                    pass
        elif trade.get("confidence") is not None:
            size_pct = confidence_to_size_pct(trade.get("confidence"))
            if size_pct <= 0:
                return {
                    "ticker": ticker,
                    "status": "skipped",
                    "reason": f"confidence {trade.get('confidence')} below tradeable bar ({CONFIDENCE_MIN_TO_TRADE})",
                }
            requested_amount = total_value * size_pct

        is_new_position = current_holding is None
        if is_new_position and len(account_snapshot["holdings"]) >= MAX_OPEN_POSITIONS:
            return {
                "ticker": ticker,
                "status": "skipped",
                "reason": f"max open positions reached ({MAX_OPEN_POSITIONS})",
            }

        # Daytrading entry window (only for NEW buys, never sells, never
        # existing positions being topped up): skip the first
        # TRADE_START_MINUTES_AFTER_OPEN minutes after the 9:30 open
        # (auction chop) and skip new entries after STOP_NEW_BUYS_AFTER ET.
        # Config defaults (0 / "23:59") keep this open 24/7 -- the knobs
        # exist for a stricter daytrading schedule, and they must actually
        # gate (this was dead code: is_within_trade_window() was never
        # called, so those documented config values silently did nothing).
        if is_new_position and not is_within_trade_window():
            return {
                "ticker": ticker,
                "status": "skipped",
                "reason": "outside the daytrading entry window (open-auction chop or after STOP_NEW_BUYS_AFTER)",
            }

        # Chase filters -> SOFT filters: buying into a name already extended
        # on the day is where momentum profits get given back, but momentum IS
        # the trade. So instead of skipping, the position is scaled DOWN as the
        # extension grows (full size near the limit, 60% / 35% / 15% further
        # out) and refused only past CHASE_HARD_SKIP_MULT x the limit (a truly
        # priced-in move). Config value <= 0 disables each filter entirely;
        # CHASE_HARD_SKIP_MULT <= 0 removes the hard skip. Sells never scaled.
        chase_multiplier = 1.0
        chase_note = None
        try:
            ind_data = get_full_indicators(ticker)
        except Exception as e:
            print(f"Could not fetch indicators for chase filters on {ticker} (continuing without them): {e}")
            ind_data = None
        if ind_data:
            if MAX_BUY_EXTENSION_ABOVE_VWAP_PCT > 0:
                vwap = ind_data.get("vwap")
                if vwap and vwap > 0 and price > vwap * (1 + MAX_BUY_EXTENSION_ABOVE_VWAP_PCT / 100.0):
                    pct_above = (price / vwap - 1) * 100.0
                    mult = _chase_size_multiplier(pct_above, MAX_BUY_EXTENSION_ABOVE_VWAP_PCT)
                    if mult < chase_multiplier:
                        chase_multiplier = mult
                        chase_note = f"price {pct_above:.1f}% above VWAP (limit {MAX_BUY_EXTENSION_ABOVE_VWAP_PCT}%)"
            if MAX_INTRADAY_MOVE_PCT > 0:
                intraday_move = ind_data.get("intraday_momentum_pct")
                if intraday_move is not None and intraday_move > MAX_INTRADAY_MOVE_PCT:
                    mult = _chase_size_multiplier(intraday_move, MAX_INTRADAY_MOVE_PCT)
                    if mult < chase_multiplier:
                        chase_multiplier = mult
                        chase_note = f"already up {intraday_move:.1f}% on the session (limit {MAX_INTRADAY_MOVE_PCT}%)"
        if chase_multiplier <= 0:
            return {
                "ticker": ticker,
                "status": "skipped",
                "reason": f"chase filter: {chase_note or 'name extended'} -- hard skip beyond {CHASE_HARD_SKIP_MULT:.1f}x the limit",
            }
        if chase_multiplier < 1.0:
            print(f"chase filter: {ticker} {chase_note} -> position scaled to {chase_multiplier:.0%}")

        # FLAT sizing (default): every trade gets the SAME FLAT_TRADE_SIZE_PCT
        # of equity, capped only by the per-position ceiling and the
        # regime/breaker multiplier. Confidence/conviction/time-of-day/
        # setup-learning/economic-event/earnings multipliers are skipped in
        # flat mode -- size is uniform by design. Stops/TPs are still computed
        # per trade (protection) but never resize the position.
        exit_levels = None
        if FLAT_SIZING:
            max_allowed = total_value * MAX_POSITION_PCT
            flat_target = total_value * FLAT_TRADE_SIZE_PCT * size_multiplier
            flat_target = min(flat_target, max_allowed)
            target_room = max(0.0, flat_target - current_position_value)
            buy_target = target_room
            exit_levels = _compute_exit_levels(ticker, trade, price, ind=ind_data)
        else:
            max_allowed = total_value * MAX_POSITION_PCT * (conviction / 10.0) * size_multiplier
            target_room = max(0.0, max_allowed - current_position_value)

            # Fix: If requested_amount is 0/unspecified, default to remaining target position room
            if requested_amount <= 0:
                buy_target = target_room
            else:
                buy_target = min(requested_amount, target_room)

            # Time-of-day sizing: full size in the high-edge windows, smaller
            # through the lunch lull. Sells are never affected by this.
            buy_target *= get_time_of_day_multiplier()

            # Phase 3 self-learning: size this setup by its DEMONSTRATED edge from
            # the closed-trade journal (winning setups bigger, losing setups
            # smaller). Never affects sells.
            buy_target *= get_setup_multiplier(_setup_category(trade.get("reasoning")))

            # Phase 3b: walk-forward learned gate (backtest.py --walkforward)
            # fills the gap before the live journal has samples -- size by the
            # indicator-regime setup's demonstrated edge from history. Same
            # guardrails as the other multipliers: never affects sells, and
            # FLAT_SIZING keeps its uniform size by design.
            if ind_data:
                buy_target *= get_walkforward_multiplier(ind_data)

            # Phase 2: on high-impact economic event days (CPI/FOMC/NFP...), size
            # new buys down -- the market reprices hard around those prints.
            buy_target *= get_economic_event_multiplier()

            # Earnings proximity: buying into a print means carrying overnight gap
            # risk; shrink the entry when earnings are within the window.
            if ind_data and ind_data.get("days_until_earnings") is not None:
                days = ind_data.get("days_until_earnings")
                if 0 <= days <= EARNINGS_PROXIMITY_DAYS:
                    buy_target *= EARNINGS_PROXIMITY_SIZE_MULT

        base_reserve = total_value * MIN_CASH_RESERVE_PCT
        is_exceptional = conviction >= EXCEPTIONAL_CONVICTION_THRESHOLD
        reserve_kept = base_reserve * (1.0 - EXCEPTIONAL_TRADE_RESERVE_ACCESS_PCT) if is_exceptional else base_reserve

        # Reserve cash for orders already sitting open in the market (the bug
        # that let ~10 queued overnight buys all fill at once). Sells that are
        # open will free cash when they fill, so they're credited back.
        try:
            buy_pending, sell_pending = pending_order_notional()
        except Exception as e:
            print(f"Could not compute pending order notional (continuing without it): {e}")
            buy_pending, sell_pending = 0.0, 0.0
        available_cash = max(0.0, account_snapshot["cash"] - reserve_kept - buy_pending + sell_pending)

        # Total gross exposure cap: holdings + pending buys may not exceed
        # MAX_TOTAL_EXPOSURE_PCT of the portfolio, so the account can never
        # lever itself into margin even with many positions.
        current_exposure = get_gross_exposure(account_snapshot) + buy_pending
        exposure_room = max(0.0, total_value * MAX_TOTAL_EXPOSURE_PCT - current_exposure)

        # Soft chase filter: extended names get smaller positions (momentum is
        # tradeable, but the snapback risk grows with the extension). Applies
        # to both flat and confidence-sized paths.
        buy_target *= chase_multiplier

        amount = min(buy_target, available_cash, exposure_room)

        # Sector concentration cap: never let one GICS sector exceed
        # MAX_SECTOR_EXPOSURE_PCT of the portfolio (14+ positions can quietly
        # become 5 names in Energy, and a sector shock then hits them all at
        # once). Instead of skipping the buy outright, the position is capped
        # at the ROOM left in that sector -- capital stuck at the exposure
        # ceiling still deploys into the under-exposed slice, and a genuinely
        # full sector leaves nothing to buy (the min-size check below skips
        # it). Existing positions are never force-sold. Data comes from the
        # cached sector profiles (free Finnhub, 24h cache) so this is nearly
        # free; when the sector lookup fails the buy is allowed through rather
        # than blocked on a data hiccup.
        sector_room = None
        sector_was_binding = False
        if MAX_SECTOR_EXPOSURE_PCT > 0:
            try:
                sector_room, _sector_name = _sector_room_for(
                    account_snapshot, ticker, total_value
                )
                if sector_room < amount:
                    sector_was_binding = True
                    amount = sector_room
            except Exception as e:
                print(f"Sector cap check failed on {ticker} (continuing without it): {e}")

        # Risk-based sizing: cap the position so a stop-out costs at most
        # MAX_RISK_PER_TRADE_PCT of equity, using THIS trade's real stop
        # distance (tight stop = bigger size, wide stop = smaller size).
        # Applies in tiered mode AND -- with RISK_PARITY_SIZING (default on) --
        # in flat mode too, so EVERY trade risks the same dollar amount
        # regardless of the stock's volatility (volatility-scaled sizing).
        if MAX_RISK_PER_TRADE_PCT > 0 and (not FLAT_SIZING or RISK_PARITY_SIZING):
            exit_levels = _compute_exit_levels(ticker, trade, price, ind=ind_data)
            stop_for_risk = exit_levels[0] if exit_levels else None
            risk_budget = total_value * MAX_RISK_PER_TRADE_PCT / 100.0
            if stop_for_risk is not None and price > stop_for_risk:
                stop_frac = (price - stop_for_risk) / price
            else:
                stop_frac = MAX_POSITION_LOSS_PCT / 100.0  # assume hard-cap distance
            if stop_frac > 0:
                amount = min(amount, risk_budget / stop_frac)

        if amount < MIN_TRADE_DOLLAR_AMOUNT:
            reason = (
                f"below minimum trade size (${MIN_TRADE_DOLLAR_AMOUNT}) after position cap "
                f"(${max_allowed:,.2f}), room (${target_room:,.2f}), regime multiplier "
                f"({size_multiplier:.0%}), and cash reserve "
                f"(${reserve_kept:,.2f} kept of ${account_snapshot['cash']:,.2f} cash)"
            )
            if sector_was_binding and sector_room is not None:
                reason += f", and sector room (${sector_room:,.2f})"
            return {
                "ticker": ticker,
                "status": "skipped",
                "reason": reason,
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
        # Extended-hours trading: Alpaca's extended session (4:00-9:30 AM and
        # 4:00-8:00 PM ET) REJECTS market orders -- only LIMIT orders with
        # extended_hours=True are accepted. So whenever the regular session is
        # closed (and ALLOW_EXTENDED_HOURS is on) we submit a limit order at
        # the last traded price. Outside any session Alpaca holds it and
        # enters it at the next eligible session (4 AM) instead of queueing a
        # market order to the 9:30 open (the 2026-08-07 failure mode).
        #
        # These extended-hours limit orders use GTC (good-til-canceled), NOT
        # DAY: a DAY order placed in the 4-8 PM extended session expires at
        # 8 PM without ever seeing the overnight/pre-market liquidity, which
        # is exactly the 'orders expired' behavior the user hit. GTC keeps
        # the order live across sessions so it can fill during pre-market
        # (4 AM onward) or the regular session, and is explicitly supported
        # by Alpaca for extended-hours limit orders (docs: 'Only limit
        # orders with time_in_force set to day or gtc orders are accepted as
        # extended hours eligible'). The bot's open-order dedup prevents
        # duplicates, and open orders are cleared by the account-switch reset.
        use_extended = False
        try:
            use_extended = bool(ALLOW_EXTENDED_HOURS) and not bool(trading_client.get_clock().is_open)
        except Exception:
            use_extended = False
        if use_extended:
            order_request = LimitOrderRequest(
                symbol=ticker,
                qty=qty,
                side=side,
                time_in_force=TimeInForce.GTC,
                limit_price=price,
                extended_hours=True,
            )
        else:
            order_request = MarketOrderRequest(
                symbol=ticker,
                qty=qty,
                side=side,
                time_in_force=TimeInForce.DAY,
            )
        order = trading_client.submit_order(order_request)
        _record_cooldown(ticker)

        # Per-trade custom stop-loss / take-profit: computed from THIS trade's
        # own setup (swing levels clamped by ATR, or Gemini's levels), saved to
        # custom_exits.json and enforced by check_atr_stop_take_profit. Reuses
        # the levels already computed for risk-based sizing (no second fetch).
        stop_loss = take_profit = None
        if side == OrderSide.BUY:
            custom = _record_custom_exit(ticker, trade, price, levels=exit_levels)
            if custom:
                stop_loss = custom.get("stop_loss")
                take_profit = custom.get("take_profit")

        # Second-trader detection ledger: every order this bot submits.
        # NOTE: _status_val() -- storing str(order.status) would write
        # 'OrderStatus.PENDING_NEW', which no plain-string comparison in the
        # reconciliation pipeline could ever match (the root cause of the
        # 2026-08-13 CVX false alarm). Store the plain value.
        _append_order_to_ledger({
            "timestamp": datetime.now().isoformat(),
            "ticker": ticker,
            "action": action,
            "qty": qty,
            "order_id": str(order.id),
            "order_status": _status_val(order.status),
        })

        # Trade journal: record every fill, then pair buys/sells into
        # closed-trade results so win rate by setup can be measured.
        _record_trade_journal(
            ticker=ticker, action=action, qty=qty, price=price,
            stop_loss=stop_loss, take_profit=take_profit,
            conviction=conviction, confidence=trade.get("confidence"),
            trigger=trigger,
            reasoning=trade.get("reasoning", ""),
        )
        _track_open_close(ticker, action, qty, price, stop_loss, take_profit, trigger, trade.get("reasoning", ""))

        return {
            "timestamp": datetime.now().isoformat(),
            "ticker": ticker,
            "action": action,
            "qty": qty,
            "conviction": conviction,
            "size_multiplier": size_multiplier,
            "order_id": str(order.id),
            "order_status": str(order.status),
            "stop_loss": stop_loss,
            "take_profit": take_profit,
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


def summarize_performance(account_snapshot=None):
    """
    Portfolio stats for the run log, computed from the existing logs:
    total return, max drawdown, daily Sharpe (from performance.csv) and win
    rate / avg winner / avg loser (from trade_results.csv).
    """
    lines = []
    values = []
    daily = {}
    perf_path = os.path.join(os.path.dirname(__file__), "logs", "performance.csv")
    if os.path.exists(perf_path):
        try:
            with open(perf_path) as f:
                rows = list(csv.DictReader(f))
            for r in rows:
                try:
                    values.append(float(r["total_value"]))
                    daily.setdefault(r["timestamp"][:10], []).append(float(r["total_value"]))
                except (KeyError, ValueError):
                    continue
        except (OSError, csv.Error):
            pass

    if values:
        total_ret = (values[-1] / values[0] - 1.0) * 100.0 if values[0] else 0.0
        peak = -1e18
        max_dd = 0.0
        for v in values:
            peak = max(peak, v)
            if peak > 0:
                max_dd = max(max_dd, (peak - v) / peak * 100.0)
        day_rets = []
        for day in sorted(daily):
            ds = daily[day]
            if len(ds) >= 2 and ds[0]:
                day_rets.append((ds[-1] / ds[0] - 1.0) * 100.0)
        sharpe = None
        if len(day_rets) >= 2:
            mean = sum(day_rets) / len(day_rets)
            std = (sum((d - mean) ** 2 for d in day_rets) / len(day_rets)) ** 0.5
            sharpe = (mean / std * (252 ** 0.5)) if std > 0 else 0.0
        lines.append(f"Total return {total_ret:+.2f}% | Max drawdown {max_dd:.2f}% | Daily Sharpe {sharpe:.2f}" if sharpe is not None
                     else f"Total return {total_ret:+.2f}% | Max drawdown {max_dd:.2f}%")

    if os.path.exists(TRADE_RESULTS_FILE):
        try:
            with open(TRADE_RESULTS_FILE) as f:
                rows = list(csv.DictReader(f))
            pcts = []
            for r in rows:
                try:
                    pcts.append(float(r["pnl_pct"]))
                except (ValueError, KeyError):
                    continue
            if pcts:
                wins = [p for p in pcts if p > 0]
                losses = [p for p in pcts if p <= 0]
                wr = len(wins) / len(pcts) * 100.0
                avg_w = sum(wins) / len(wins) if wins else 0.0
                avg_l = sum(losses) / len(losses) if losses else 0.0
                lines.append(
                    f"{len(pcts)} closed trades | Win rate {wr:.0f}% | "
                    f"Avg winner {avg_w:+.2f}% | Avg loser {avg_l:+.2f}%"
                )
        except (OSError, csv.Error):
            pass

    if account_snapshot is not None:
        lines.append(
            f"Open positions: {len(account_snapshot.get('holdings', {}))} | "
            f"Equity ${account_snapshot.get('total_value', 0):,.2f}"
        )
    return " | ".join(lines) if lines else "no performance history yet"
