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
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
BOLLINGER_PERIOD = 20
BOLLINGER_STD = 2

# --- Duplicate-trade prevention ---
TRADE_COOLDOWN_MINUTES = 30

# --- Fixed watchlist scanned for pure technical setups even with no news ---
# Kept to large, liquid, well-known names so indicator data is reliable.
WATCHLIST = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
    "JPM", "V", "MA", "WMT", "JNJ", "PG", "UNH", "HD",
    "DIS", "BAC", "XOM", "KO", "NFLX",
]
