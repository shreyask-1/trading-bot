"""
API keys and bot settings, all in one place.
Keys are read from environment variables -- never hardcoded, never committed.
"""

import os

_REQUIRED_KEYS = [
    "FINNHUB_API_KEY",
    "GEMINI_API_KEY",
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
]

_missing = [k for k in _REQUIRED_KEYS if not os.environ.get(k)]
if _missing:
    raise RuntimeError(
        "Missing required environment variable(s): "
        + ", ".join(_missing)
        + ". Set them as GitHub Actions secrets, or export them locally."
    )

FINNHUB_API_KEY = os.environ["FINNHUB_API_KEY"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
ALPACA_API_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]

# --- Universe ---
MAX_NEWS_ITEMS = 15  # headlines considered per run

WATCHLIST = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA",
    "META", "TSLA", "AVGO", "JPM", "V",
    "UNH", "XOM", "WMT", "MA", "HD",
    "PG", "COST", "NFLX", "BAC", "DIS",
]

# --- Data feed ---
ALPACA_DATA_FEED = os.environ.get("ALPACA_DATA_FEED", "iex")

# --- Position sizing ---
MAX_POSITION_PCT = 0.15  # Hard ceiling: no single position over 15% of portfolio
MIN_CONVICTION_TO_TRADE = 6  # Conviction 1-10; below this, skip trade

# --- Cash discipline & portfolio sprawl control ---
MIN_CASH_RESERVE_PCT = 0.05  # Normally-untouchable cash fraction (5%)
MIN_TRADE_DOLLAR_AMOUNT = 25  # Buys sized below $25 skipped outright
MAX_OPEN_POSITIONS = 20  # Hard cap on distinct held tickers
CONSOLIDATION_SCORE_THRESHOLD = 70  # Force-sell excess positions scoring below this

EXCEPTIONAL_CONVICTION_THRESHOLD = 9
EXCEPTIONAL_TRADE_RESERVE_ACCESS_PCT = 0.5

# --- Market-hours gating ---
# THE big one: this bot historically submitted orders 24/7. DAY orders placed
# while the market is closed sit queued in Alpaca and ALL fill at the next
# 9:30 AM ET open at once -- which is exactly what blew through cash into
# negative margin on 2026-08-07 (cash went +$33k -> -$19k in minutes).
# When enabled, the bot never proposes or executes NEW trades unless the
# market is open. Risk management (stops) still runs so exits are never blocked.
TRADE_ONLY_DURING_MARKET_HOURS = (
    os.environ.get("TRADE_ONLY_DURING_MARKET_HOURS", "true").lower() == "true"
)

# --- Hard no-margin / exposure discipline ---
# Hard ceiling on TOTAL invested (holdings + pending buys) as a fraction of
# portfolio value. 15% per position x 20 positions mathematically allows ~3x
# leverage; this caps gross exposure so the account can never sit in margin.
MAX_TOTAL_EXPOSURE_PCT = float(os.environ.get("MAX_TOTAL_EXPOSURE_PCT", 0.90))
# Hard per-position loss cap (%): if a position is ever down this much from
# its average entry, it is force-sold even if ATR/indicator data is unavailable.
MAX_POSITION_LOSS_PCT = float(os.environ.get("MAX_POSITION_LOSS_PCT", 8.0))
# De-leveraging heals the account by selling weakest holdings until projected
# cash is back to this fraction of portfolio value (default 2% = positive cash).
DELEVERAGE_TARGET_CASH_PCT = float(os.environ.get("DELEVERAGE_TARGET_CASH_PCT", 0.02))

# --- Equity-level circuit breakers (the "never again" guards) ---
# Halt ALL new buys for the rest of the day if equity is down this much (%) from
# the start of the day. Stops and sells are still allowed.
DAILY_LOSS_HALT_PCT = float(os.environ.get("DAILY_LOSS_HALT_PCT", 3.0))
# If drawdown from the running equity peak reaches this (%), cut new position
# sizing to DELEVERAGE_SIZE_MULTIPLIER.
MAX_DRAWDOWN_DELEVERAGE_PCT = float(os.environ.get("MAX_DRAWDOWN_DELEVERAGE_PCT", 5.0))
# If drawdown from peak reaches this (%), flatten every position and halt new
# trading until the user reviews. 12% is the blast radius from 2026-08-11.
MAX_DRAWDOWN_FLATTEN_PCT = float(os.environ.get("MAX_DRAWDOWN_FLATTEN_PCT", 8.0))
DELEVERAGE_SIZE_MULTIPLIER = float(os.environ.get("DELEVERAGE_SIZE_MULTIPLIER", 0.25))
# When True (default), the equity peak is initialized to the account value on
# the first run after deploy, so an already-damaged account isn't instantly
# flattened. Set to false to enforce hard drawdown limits from day one.
RESET_EQUITY_PEAK_ON_START = (
    os.environ.get("RESET_EQUITY_PEAK_ON_START", "true").lower() == "true"
)

# --- Alerting (optional but strongly recommended) ---
# Set DISCORD_WEBHOOK_URL (or extend notify() for email/Telegram) to get pinged
# on margin, halts, de-leveraging, and forced liquidations instead of finding
# out by looking at the dashboard.
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# --- Second-trader / foreign-activity detection ---
# The bot records every order it submits to logs/bot_order_ledger.json and,
# each run, reconciles actual account holdings against what the ledger says
# it should own. Any position the bot did not create (e.g. a second bot,
# manual trading, or another cron using the same Alpaca keys) is flagged and
# alerted. If you did not put money into this account manually, you should
# never see "FOREIGN ACTIVITY" lines.
ENABLE_FOREIGN_ACTIVITY_DETECTION = (
    os.environ.get("ENABLE_FOREIGN_ACTIVITY_DETECTION", "true").lower() == "true"
)

# --- Per-trade stop-loss / take-profit ---
# On every buy the bot computes a stop-loss and take-profit from the specific
# trade's own setup: swing high/low over SWING_LOOKBACK_DAYS, clamped to a
# sane multiple of ATR (MIN/MAX_STOP_DISTANCE_ATR_MULT etc.), or Gemini's
# custom levels when it provides them. These are saved to
# logs/custom_exits.json and enforced every run by check_atr_stop_take_profit.

# --- Daytrading mode (dual focus: news catalysts + chart/technicals) ---
# When True the bot acts as a news+technical day trader: news-driven
# candidates get a sentiment boost to their signal score, entries prefer
# opening-range breakouts and VWAP-aligned intraday momentum, and all
# positions are flattened back to cash at END_OF_DAY_FLATTEN_TIME ET so the
# account never carries overnight risk (the 2026-08-11 liquidation happened
# at 3:30 AM ET on an overnight position).
DAYTRADE_MODE = os.environ.get("DAYTRADE_MODE", "true").lower() == "true"
END_OF_DAY_FLATTEN = (
    os.environ.get("END_OF_DAY_FLATTEN", "true").lower() == "true"
)
END_OF_DAY_FLATTEN_TIME = os.environ.get("END_OF_DAY_FLATTEN_TIME", "15:50")
# First OPENING_RANGE_BARS 5-minute bars after 9:30 ET form the opening range;
# price breaking above/below it is a classic daytrading entry signal.
OPENING_RANGE_BARS = int(os.environ.get("OPENING_RANGE_BARS", 3))
# Skip NEW buys in the first N minutes of the session (open auction chop).
TRADE_START_MINUTES_AFTER_OPEN = int(os.environ.get("TRADE_START_MINUTES_AFTER_OPEN", 15))
# Skip NEW buys after this ET time (no late-session entries).
STOP_NEW_BUYS_AFTER = os.environ.get("STOP_NEW_BUYS_AFTER", "15:30")
# Signal-score points added per unit of headline sentiment (-1..+1) for
# news-driven candidates. E.g. weight 10 = up to +/-10 points on the 0-100
# scale, enough to push a 50-60 candidate over the 55 pre-screen bar.
NEWS_SENTIMENT_WEIGHT = float(os.environ.get("NEWS_SENTIMENT_WEIGHT", 10.0))

# --- Risk management ---
ATR_PERIOD = 14
ATR_STOP_MULTIPLIER = 2.5
ATR_TAKE_PROFIT_MULTIPLIER = 4.0

SWING_LOOKBACK_DAYS = 10
MIN_STOP_DISTANCE_ATR_MULT = 1.0
MAX_STOP_DISTANCE_ATR_MULT = 5.0
MIN_TAKE_PROFIT_DISTANCE_ATR_MULT = 1.5
MAX_TAKE_PROFIT_DISTANCE_ATR_MULT = 8.0

ALLOW_GEMINI_CUSTOM_EXITS = (
    os.environ.get("ALLOW_GEMINI_CUSTOM_EXITS", "true").lower() == "true"
)

# --- Intraday analysis ---
ENABLE_INTRADAY_ANALYSIS = (
    os.environ.get("ENABLE_INTRADAY_ANALYSIS", "true").lower() == "true"
)
INTRADAY_BAR_MINUTES = int(os.environ.get("INTRADAY_BAR_MINUTES", 5))
INTRADAY_LOOKBACK_DAYS = int(os.environ.get("INTRADAY_LOOKBACK_DAYS", 2))

# --- Cooldown & dedup ---
TRADE_COOLDOWN_MINUTES = 30
NEWS_DEDUP_MAX_AGE_HOURS = 48

# --- Model & quota ---
# NOTE: decide.py now discovers live, currently-valid model IDs from
# Google's own ListModels endpoint at runtime and caches them for the day --
# this static list below is ONLY used as a last-resort fallback if that
# discovery call itself fails (e.g. no network). Don't rely on these names
# staying accurate forever; Google renames/retires models over time (this
# is exactly what caused the gemini-1.5-flash / gemini-1.5-flash-8b 404s).
GEMINI_MODEL_FALLBACKS = [
    "gemini-flash-latest",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", GEMINI_MODEL_FALLBACKS[0])

GEMINI_MODEL_LIMITS = {
    "gemini-flash-latest": {"rpd": int(os.environ.get("GEMINI_RPD_FLASH_LATEST", 1500)), "rpm": int(os.environ.get("GEMINI_RPM_FLASH_LATEST", 15))},
    "gemini-2.0-flash": {"rpd": int(os.environ.get("GEMINI_RPD_20_FLASH", 1500)), "rpm": int(os.environ.get("GEMINI_RPM_20_FLASH", 15))},
    "gemini-2.0-flash-lite": {"rpd": int(os.environ.get("GEMINI_RPD_20_FLASH_LITE", 1500)), "rpm": int(os.environ.get("GEMINI_RPM_20_FLASH_LITE", 15))},
}

GEMINI_QUOTA_RESET_TIMEZONE = "America/Los_Angeles"
PRICE_HISTORY_DAYS = 150

# --- Market regime filter ---
MARKET_HIGH_VOLATILITY_THRESHOLD = 2.5
REGIME_POSITION_MULTIPLIERS = {
    "BULLISH": 1.0,
    "NEUTRAL": 0.6,
    "BEARISH": 0.0,
    "HIGH_VOLATILITY": 0.3,
}

# --- Quantitative pre-screen ---
MIN_SIGNAL_SCORE_TO_CONSIDER = 55

# --- Pure-technical fallback ---
TECHNICAL_MIN_CONVICTION = int(os.environ.get("TECHNICAL_MIN_CONVICTION", 5))
TECHNICAL_CONVICTION_AGGRESSIVENESS = float(
    os.environ.get("TECHNICAL_CONVICTION_AGGRESSIVENESS", 0.8)
)
