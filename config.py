"""
Global Configuration & Environment Settings Engine.
Loads API keys, environment endpoints, risk controls, and system parameters.
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
# 2. QUANTITATIVE & RISK MANAGEMENT PARAMETERS
# =====================================================================
# Position Sizing & Allocation Bounds
MAX_POSITION_SIZE_PCT = 0.05
MAX_POSITION_PCT = 0.05            # Backward compatibility alias
MAX_PORTFOLIO_ALLOCATION_PCT = 0.80

# Risk Limits per Trade
STOP_LOSS_PCT = 0.03
TAKE_PROFIT_PCT = 0.08

# Technical Analysis & Indicator Settings
PRICE_HISTORY_DAYS = 30            # Number of historical days/bars required
RSI_PERIOD = 14                    # Relative Strength Index lookback window
SMA_SHORT_PERIOD = 9               # Short-term Simple Moving Average
SMA_LONG_PERIOD = 21               # Long-term Simple Moving Average

# Signal Scoring Thresholds
MIN_SIGNAL_SCORE = 50.0
MAX_EVALUATION_CANDIDATES = 10

# News Ingestion Settings
MAX_NEWS_ARTICLES = 5
NEWS_LOOKBACK_DAYS = 3

# System & Data Logging
LOG_LEVEL = "INFO"
PERFORMANCE_LOG_FILE = os.path.join(os.path.dirname(__file__), "logs", "performance.csv")
