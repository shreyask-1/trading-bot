import os

# --- API Keys ---
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")

if not all([FINNHUB_API_KEY, GEMINI_API_KEY, ALPACA_API_KEY, ALPACA_SECRET_KEY]):
    raise RuntimeError(
        "Missing one or more required API keys. On GitHub Actions, set them "
        "as repo Secrets (Settings > Secrets and variables > Actions)."
    )

# --- Bot Behavior & Portfolio Settings ---
STARTING_CASH = 100_000.00
MAX_POSITION_PCT = 0.08         # Cap single position to max 8% of portfolio
MAX_RISK_PER_TRADE_PCT = 0.01   # Risk max 1% of total account equity per trade
MAX_NEWS_ITEMS = 15
GEMINI_MODEL = "gemini-flash-latest"

# --- Quantitative Pipeline Gate Thresholds ---
MIN_SIGNAL_SCORE = 60.0         # Minimum pre-scoring gate required (0-100)
MIN_GEMINI_CONFIDENCE = 75      # Minimum Gemini confidence score required (0-100)

# --- Risk Management & Volatility Dynamic Stops ---
STOP_LOSS_PCT = -6.0            # Static fallback stop loss percentage
TAKE_PROFIT_PCT = 15.0          # Static fallback take profit percentage
ATR_STOP_MULTIPLIER = 1.5       # Dynamic ATR multiplier for volatility-based stops
RISK_REWARD_RATIO = 2.0         # Target risk-reward ratio for dynamic profit targets

# --- Technical Indicator & Normalization Parameters ---
PRICE_HISTORY_DAYS = 5
FEATURE_LOOKBACK = 20           # Rolling lookback for Z-score feature normalization
RSI_PERIOD = 14
SMA_SHORT = 20
SMA_LONG = 50
VOLUME_LOOKBACK = 20
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
BOLLINGER_PERIOD = 20
BOLLINGER_STD = 2

# --- Duplicate-Trade Prevention & Throttling ---
TRADE_COOLDOWN_MINUTES = 30
GEMINI_CALL_INTERVAL_MINUTES = 25
GEMINI_TIMESTAMP_FILE = "logs/last_gemini_call.txt"

# --- Fixed Watchlist Scanned for Technical Setups ---
WATCHLIST = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
    "JPM", "V", "MA", "WMT", "JNJ", "PG", "UNH", "HD",
    "DIS", "BAC", "XOM", "KO", "NFLX",
]
