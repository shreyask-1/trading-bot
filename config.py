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
