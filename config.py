import os
from dotenv import load_dotenv

# Load environment variables from a local .env file if present
load_dotenv()

# --- API Keys & Base URLs ---
APEX_API_KEY = os.getenv("APEX_API_KEY", "your_apex_api_key_here")
APEX_API_SECRET = os.getenv("APEX_API_SECRET", "your_apex_api_secret_here")

# Alpaca Credentials
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "your_alpaca_api_key_here")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "your_alpaca_secret_key_here")
ALPACA_PAPER = os.getenv("ALPACA_PAPER", "True").lower() in ("true", "1", "t")

# Google Gemini API Key
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "your_gemini_api_key_here")

# --- Trading Strategy Configuration ---
MAX_PORTFOLIO_ALLOCATION_PCT = 0.20  # Max 20% allocation per individual ticker
STOP_LOSS_PCT = 0.05                 # 5% stop loss threshold
TAKE_PROFIT_PCT = 0.10               # 10% take profit threshold
PAPER_TRADING_DEFAULT_CASH = 100000.0
