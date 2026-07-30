import os

# --- API Keys & Base URLs ---
APEX_API_KEY = os.getenv("APEX_API_KEY", "")
APEX_API_SECRET = os.getenv("APEX_API_SECRET", "")

# Alpaca Credentials (pulled automatically from GitHub Secrets in CI/CD)
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_PAPER = os.getenv("ALPACA_PAPER", "True").lower() in ("true", "1", "t")

# Finnhub API Key (pulled from GitHub Secrets)
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")

# Google Gemini API Key & Model Configuration
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# --- Trading Strategy & Indicator Parameters ---
MAX_POSITION_PCT = 0.20
STOP_LOSS_PCT = -0.05
TAKE_PROFIT_PCT = 0.10
PRICE_HISTORY_DAYS = 14
RSI_PERIOD = 14
SMA_SHORT = 20
SMA_LONG = 50
VOLUME_LOOKBACK = 20
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
BOLLINGER_PERIOD = 20
BOLLINGER_STD = 2.0
TRADE_COOLDOWN_MINUTES = 60
