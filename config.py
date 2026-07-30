"""
Global Configuration & Environment Settings Engine.
Loads API keys, environment endpoints, risk controls, and system parameters.
"""

import os

# =====================================================================
# 1. API KEYS & CREDENTIALS
# =====================================================================
# Google GenAI / Gemini API Settings
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# Alpaca Trading API Credentials
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
# Default to Alpaca Paper Trading Endpoint
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

# Finnhub Financial News API Credentials
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")

# =====================================================================
# 2. QUANTITATIVE & RISK MANAGEMENT PARAMETERS
# =====================================================================
# Position Sizing & Allocation Bounds
MAX_POSITION_SIZE_PCT = 0.05       # Max 5% of portfolio total value per position
MAX_PORTFOLIO_ALLOCATION_PCT = 0.80 # Maintain at least 20% total cash reserve

# Risk Limits per Trade
STOP_LOSS_PCT = 0.03               # Stop-loss trigger at -3% drawdown
TAKE_PROFIT_PCT = 0.08             # Take-profit trigger at +8% gain

# Signal Scoring Thresholds
MIN_SIGNAL_SCORE = 50.0            # Minimum quant composite score required to qualify
MAX_EVALUATION_CANDIDATES = 10     # Top N screened candidates sent to Gemini Veto Agent

# News Ingestion Settings
MAX_NEWS_ARTICLES = 5              # Max articles to pull per candidate symbol
NEWS_LOOKBACK_DAYS = 3             # Lookback window for news catalyst detection

# System & Data Logging
LOG_LEVEL = "INFO"
PERFORMANCE_LOG_FILE = os.path.join(os.path.dirname(__file__), "logs", "performance.csv")
