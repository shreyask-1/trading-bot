"""
API keys and bot settings, all in one place.

Keys are read from environment variables -- never hardcoded, never committed.

Set them as GitHub Actions secrets (Settings -> Secrets and variables ->
Actions) for the scheduled runs, and export them in your shell for local
testing:

    export FINNHUB_API_KEY="..."       # https://finnhub.io/dashboard
    export GEMINI_API_KEY="..."        # https://aistudio.google.com/apikey
    export ALPACA_API_KEY="..."        # https://app.alpaca.markets (Paper Trading -> API Keys)
    export ALPACA_SECRET_KEY="..."
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
        + ". Set them as GitHub Actions secrets, or export them locally. "
        "See the docstring at the top of config.py."
    )

FINNHUB_API_KEY = os.environ["FINNHUB_API_KEY"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
ALPACA_API_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]

# --- Universe ---
MAX_NEWS_ITEMS = 15          # headlines considered per run
# Liquid, well-known tickers always evaluated on technicals, even with zero
# news that cycle -- so the bot isn't 100% dependent on a headline existing.
WATCHLIST = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "AVGO",
    "JPM", "V", "UNH", "XOM", "WMT", "MA", "HD", "PG", "COST", "NFLX",
    "BAC", "DIS",
]

# --- Trading window ---
# Skip the entire run when the market is closed. Without this, market orders
# placed overnight queue until the next open and fill on prices that have
# nothing to do with the data the decision was made on. Fractional-share
# orders are also rejected outside regular trading hours.
MARKET_HOURS_ONLY = True

# --- Position sizing ---
MAX_POSITION_PCT = 0.15          # hard ceiling: no single position over 15% of portfolio
MIN_CONVICTION_TO_TRADE = 6      # Gemini rates each idea 1-10; below this, skip it
# Position size scales with conviction: a conviction-10 idea can use the full
# MAX_POSITION_PCT; a conviction-6 idea gets scaled down proportionally.
# This makes size reflect how strong the setup actually is, not a flat amount.

# --- Risk management: ATR-based, not fixed percentages ---
# ATR (Average True Range) measures how much a stock typically moves per day,
# in its own price terms. Using it for stops means the stop distance adapts
# to each stock's real volatility, instead of one fixed % for every ticker.
ATR_STOP_MULTIPLIER = 2.5        # stop-loss = entry price - (2.5 x ATR)
ATR_TAKE_PROFIT_MULTIPLIER = 4.0 # take-profit = entry price + (4.0 x ATR)
ATR_PERIOD = 14

# --- Cooldown & dedup ---
TRADE_COOLDOWN_MINUTES = 30      # don't re-trade the same ticker within this window
# NOTE: this cooldown applies to Gemini-proposed trades only. Forced risk-management
# exits (ATR stop-loss / take-profit) deliberately ignore it -- see
# check_atr_stop_take_profit() in trader.py.
NEWS_DEDUP_MAX_AGE_HOURS = 48    # forget "already seen" articles older than this

# --- Model ---
# Pinned deliberately: a floating "-latest" alias would let Google swap the
# model underneath an unattended trading bot with no code change and no log entry.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

# 150 calendar days ~= 103 trading days. Must comfortably exceed 50 so SMA-50
# and classify_trend() actually resolve, plus warm-up room for ADX-14.
PRICE_HISTORY_DAYS = 150
