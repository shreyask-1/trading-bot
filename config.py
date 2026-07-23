"""
API keys and settings. Keys are read from environment variables only --
GitHub Actions injects these from your repo's encrypted Secrets. There is
no fallback value here on purpose: if a key is missing, the bot should
fail loudly instead of silently running with something wrong.
"""

import os

FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")

if not all([FINNHUB_API_KEY, GEMINI_API_KEY, ALPACA_API_KEY, ALPACA_SECRET_KEY]):
    raise RuntimeError(
        "Missing one or more required API keys. On GitHub Actions, set them "
        "as repo Secrets (Settings > Secrets and variables > Actions)."
    )

# --- Bot behavior settings ---
STARTING_CASH = 100_000.00
MAX_POSITION_PCT = 0.08          # max 8% of portfolio in any single stock
MAX_NEWS_ITEMS = 15
GEMINI_MODEL = "gemini-flash-latest"

# --- Risk management (enforced in code, independent of Gemini) ---
STOP_LOSS_PCT = -6.0
TAKE_PROFIT_PCT = 15.0

# --- Price/indicator context ---
PRICE_HISTORY_DAYS = 5     # momentum window
RSI_PERIOD = 14
SMA_SHORT = 20
SMA_LONG = 50
VOLUME_LOOKBACK = 20

# --- Duplicate-trade prevention ---
# Don't let a ticker be bought again within this many minutes of its last
# order, even across separate runs -- this is the main double-trading fix.
TRADE_COOLDOWN_MINUTES = 25
