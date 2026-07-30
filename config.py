"""
Global Configuration & Environment Settings Engine.
Contains all required keys, risk parameters, and indicator windows to 
satisfy all imported references across the entire bot codebase.
"""

import os

# =====================================================================
# 1. API KEYS & CREDENTIALS
# =====================================================================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")

# =====================================================================
# 2. RISK MANAGEMENT & POSITION SIZING (All Aliases Included)
# =====================================================================
MAX_POSITION_SIZE_PCT = 0.05
MAX_POSITION_PCT = 0.05            # Alias for trader.py imports
MAX_PORTFOLIO_ALLOCATION_PCT = 0.80

STOP_LOSS_PCT = 0.03
TAKE_PROFIT_PCT = 0.08

# =====================================================================
# 3. TECHNICAL ANALYSIS & INDICATOR SETTINGS (All Aliases Included)
# =====================================================================
PRICE_HISTORY_DAYS = 30
RSI_PERIOD = 14
SMA_SHORT_PERIOD = 9
SMA_LONG_PERIOD = 21

# Additional legacy/module compatibility aliases
RSI_WINDOW = 14
SMA_FAST = 9
SMA_SLOW = 21

# =====================================================================
# 4. SIGNAL SCORING & EXECUTION SETTINGS
# =====================================================================
MIN_SIGNAL_SCORE = 50.0
MAX_EVALUATION_CANDIDATES = 10

MAX_NEWS_ARTICLES = 5
NEWS_LOOKBACK_DAYS = 3

LOG_LEVEL = "INFO"
PERFORMANCE_LOG_FILE = os.path.join(os.path.dirname(__file__), "logs", "performance.csv")
