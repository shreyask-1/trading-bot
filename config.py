"""
API keys and settings. Keys are read from environment variables only.
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
MAX_POSITION_PCT = 0.08
MAX_NEWS_ITEMS = 15
GEMINI_MODEL = "gemini-flash-latest"

# --- Risk management (enforced in code, independent of Gemini) ---
STOP_LOSS_PCT = -6.0
TAKE_PROFIT_PCT = 15.0

# --- Price/indicator context ---
PRICE_HISTORY_DAYS = 5
RSI_PERIOD = 14
SMA_SHORT = 20
SMA_LONG = 50
VOLUME_LOOKBACK = 20

# --- Duplicate-trade prevention ---
# Keep this well above the 5-min run interval so a ticker can't get bought
# again just because the same headline is still showing up in the next run.
TRADE_COOLDOWN_MINUTES = 30
