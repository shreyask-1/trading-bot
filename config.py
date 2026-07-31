# config.py
import os

# --- API Keys & Base URLs ---
APEX_API_KEY = os.getenv("APEX_API_KEY", "")
APEX_API_SECRET = os.getenv("APEX_API_SECRET", "")

# Alpaca Credentials
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_PAPER = os.getenv("ALPACA_PAPER", "True").lower() in ("true", "1", "t")

# Finnhub API Key
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")

# Google Gemini API Key & Model Configuration
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

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

# --- 100-Stock Master Market Universe Watchlist ---
WATCHLIST = [
    # --- Mega-Cap & AI Infrastructure Core ---
    "NVDA", "MSFT", "GOOGL", "AMZN", "META", "AAPL", "TSLA", "AVGO", "PLTR", "AMD",
    "NFLX", "ORCL", "CRM", "TSM", "QCOM", "IBM", "NOW", "ADBE", "AMAT", "LRCX",
    
    # --- High-Growth Tech, Software & Cybersecurity ---
    "SNOW", "CRWD", "PANW", "DDOG", "NET", "PATH", "ZS", "MDB", "HUBS", "SHOP",
    "U", "TTD", "ANET", "MU", "INTC", "TXN", "ADI", "MRVL", "CDNS", "SNPS",
    
    # --- Financials & Fintech Innovators ---
    "JPM", "V", "MA", "BAC", "GS", "MS", "AXP", "BLK", "SCHW", "PYPL",
    "SQ", "COIN", "HOOD", "FI", "PGR", "TRV", "ICE", "CME", "SPGI", "CB",
    
    # --- Healthcare & Biotech Disruptors ---
    "UNH", "JNJ", "LLY", "NVO", "ABBV", "MRK", "TMO", "DHR", "ISRG", "VRTX",
    "REGN", "AMGN", "PFE", "BMY", "ELV", "CI", "CVS", "ZTS", "BSX", "GILD",
    
    # --- Industrials, Aerospace & Energy Transition ---
    "GE", "CAT", "ETN", "HON", "UNP", "UPS", "RTX", "LMT", "BA", "DE",
    "LIN", "SHW", "FCX", "NEM", "XOM", "CVX", "COP", "SLB", "HAL", "NEE",
    
    # --- Consumer Discretionary & High-Momentum Staples ---
    "WMT", "COST", "HD", "MCD", "NKE", "SBUX", "DIS", "ABNB", "UBER", "BKNG",
    "TJX", "LOW", "PM", "KO", "PEP", "MO", "MDLZ", "CL", "TGT", "CMCSA"
]
