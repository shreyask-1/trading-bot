"""
API keys and bot settings, all in one place.

Fill in the 4 keys below with your real values from:
  - Finnhub:   https://finnhub.io/dashboard
  - Gemini:    https://aistudio.google.com/apikey
  - Alpaca:    https://app.alpaca.markets (Paper Trading view -> API Keys)

Treat these like passwords -- never commit real values to a public repo.
"""

FINNHUB_API_KEY = "PASTE_YOUR_FINNHUB_KEY_HERE"
GEMINI_API_KEY = "PASTE_YOUR_GEMINI_KEY_HERE"
ALPACA_API_KEY = "PASTE_YOUR_ALPACA_PAPER_API_KEY_HERE"
ALPACA_SECRET_KEY = "PASTE_YOUR_ALPACA_PAPER_SECRET_KEY_HERE"

# --- Universe ---
MAX_NEWS_ITEMS = 15          # headlines considered per run
# Liquid, well-known tickers always evaluated on technicals, even with zero
# news that cycle -- so the bot isn't 100% dependent on a headline existing.
WATCHLIST = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "AVGO",
    "JPM", "V", "UNH", "XOM", "WMT", "MA", "HD", "PG", "COST", "NFLX",
    "BAC", "DIS",
]

# --- Position sizing ---
MAX_POSITION_PCT = 0.15          # hard ceiling: no single position over 15% of portfolio
MIN_CONVICTION_TO_TRADE = 6      # Gemini rates each idea 1-10; below this, skip it
# Position size scales with conviction: a conviction-10 idea can use the full
# MAX_POSITION_PCT; a conviction-6 idea gets scaled down proportionally.
# This makes size reflect how strong the setup actually is, not a flat amount.

# --- Risk management: ATR-based, not fixed percentages ---
# ATR (Average True Range) measures how much a stock typically moves per day,
# in its own price terms. Using it for stops means the stop distance adapts
# to each stock's real volatility, instead of one fixed % for every ticker.
ATR_STOP_MULTIPLIER = 2.5        # stop-loss = entry price - (2.5 x ATR)
ATR_TAKE_PROFIT_MULTIPLIER = 4.0 # take-profit = entry price + (4.0 x ATR)
ATR_PERIOD = 14

# --- Cooldown & dedup ---
TRADE_COOLDOWN_MINUTES = 30      # don't re-trade the same ticker within this window
NEWS_DEDUP_MAX_AGE_HOURS = 48    # forget "already seen" articles older than this

# --- Model ---
GEMINI_MODEL = "gemini-flash-latest"
PRICE_HISTORY_DAYS = 60          # bars of history fetched per ticker (needed for ADX/SMA50/etc.)
