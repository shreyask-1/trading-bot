"""
API keys and bot settings, all in one place.

Keys are read from environment variables -- never hardcoded, never committed.

Set them as GitHub Actions secrets (Settings -> Secrets and variables ->
Actions) for the scheduled runs, and export them in your shell for local
testing:

    export FINNHUB_API_KEY="..."       # https://finnhub.io/dashboard
    export GEMINI_API_KEY="..."        # https://aistudio.google.com/apikey
    export ALPACA_API_KEY="..."        # https://app.alpaca.markets (Paper Trading -> API Keys)
    export ALPACA_SECRET_KEY="..."
"""

import os

_REQUIRED_KEYS = [
    "FINNHUB_API_KEY",
    "GEMINI_API_KEY",
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
]

_missing = [k for k in _REQUIRED_KEYS if not os.environ.get(k)]
if _missing:
    raise RuntimeError(
        "Missing required environment variable(s): "
        + ", ".join(_missing)
        + ". Set them as GitHub Actions secrets, or export them locally. "
        "See the docstring at the top of config.py."
    )

FINNHUB_API_KEY = os.environ["FINNHUB_API_KEY"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
ALPACA_API_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]

# --- Universe ---
MAX_NEWS_ITEMS = 15          # headlines considered per run
# Liquid, well-known tickers always evaluated on technicals, even with zero
# news that cycle -- so the bot isn't 100% dependent on a headline existing.
WATCHLIST = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "AVGO",
    "JPM", "V", "UNH", "XOM", "WMT", "MA", "HD", "PG", "COST", "NFLX",
    "BAC", "DIS",
]

# --- Data feed ---
# Free/paper Alpaca accounts only have access to the IEX feed, not the
# full SIP consolidated tape -- requesting SIP data without a paid market
# data subscription fails outright with "subscription does not permit
# querying recent SIP data". IEX is a real, tradable exchange's data, just
# not a cross-exchange consolidated view. Set to "sip" only if you've
# actually subscribed to Alpaca market data that includes it.
ALPACA_DATA_FEED = os.environ.get("ALPACA_DATA_FEED", "iex")

# --- Position sizing ---
MAX_POSITION_PCT = 0.15          # hard ceiling: no single position over 15% of portfolio
MIN_CONVICTION_TO_TRADE = 6      # Gemini rates each idea 1-10; below this, skip it
# Position size scales with conviction: a conviction-10 idea can use the full
# MAX_POSITION_PCT; a conviction-6 idea gets scaled down proportionally.
# This makes size reflect how strong the setup actually is, not a flat amount.

# --- Cash discipline & portfolio sprawl control ---
# Without these, the bot will happily open new tiny positions every run
# until cash runs out and every idea -- however good -- gets sized against
# whatever crumbs are left. These exist specifically to prevent that,
# while still leaving room for a genuinely exceptional idea to act.
MIN_CASH_RESERVE_PCT = 0.05    # normally-untouchable fraction of total portfolio value.
MIN_TRADE_DOLLAR_AMOUNT = 25   # buys sized below this are skipped outright, not executed as dust.
                                # Does NOT apply to sells -- cleanup exits of small holdings still work.
MAX_OPEN_POSITIONS = 20        # HARD cap -- blocks opening a brand new position once this many distinct
                                # tickers are held. Adds to existing holdings and sells are never blocked.
                                
CONSOLIDATION_SCORE_THRESHOLD = 70  # When total positions are over MAX_OPEN_POSITIONS, the consolidation
                                    # engine will rank all holdings by technical signal score (0-100) and
                                    # automatically force-sell any of the worst-scoring "excess" positions
                                    # that fall below this threshold.

# An idea at or above this conviction is allowed to dip into part of the
# cash reserve above -- the reserve stays the default for every other
# trade. This does NOT bypass MIN_TRADE_DOLLAR_AMOUNT, MAX_POSITION_PCT,
# or MAX_OPEN_POSITIONS -- it only affects how much of the cash reserve
# counts as "available" for sizing purposes.
EXCEPTIONAL_CONVICTION_THRESHOLD = 9
EXCEPTIONAL_TRADE_RESERVE_ACCESS_PCT = 0.5   # fraction of the reserve an exceptional trade may use

# --- Risk management: ATR-based, not fixed percentages ---
# ATR (Average True Range) measures how much a stock typically moves per day,
# in its own price terms. Using it for stops means the stop distance adapts
# to each stock's real volatility, instead of one fixed % for every ticker.
ATR_STOP_MULTIPLIER = 2.5        # stop-loss = entry price - (2.5 x ATR)
ATR_TAKE_PROFIT_MULTIPLIER = 4.0 # take-profit = entry price + (4.0 x ATR)
ATR_PERIOD = 14

# --- Cooldown & dedup ---
TRADE_COOLDOWN_MINUTES = 30      # don't re-trade the same ticker within this window
# NOTE: this cooldown applies to Gemini-proposed trades only. Forced risk-management
# exits (ATR stop-loss / take-profit) deliberately ignore it -- see
# check_atr_stop_take_profit() in trader.py.
NEWS_DEDUP_MAX_AGE_HOURS = 48    # forget "already seen" articles older than this

# --- Model ---
# Google periodically deprecates/restricts specific model IDs, sometimes
# without much warning (e.g. "no longer available to new users"). A strict
# pin is the right default -- it keeps behavior stable and detects drift --
# but a pin that 404s stops the bot dead. decide.py tries GEMINI_MODEL
# first, then falls through GEMINI_MODEL_FALLBACKS in order, and logs
# loudly if it had to use a fallback. If you see that log line, update
# GEMINI_MODEL to whatever it fell back to.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
GEMINI_MODEL_FALLBACKS = [
    GEMINI_MODEL,
    "gemini-flash-latest",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
]

# 150 calendar days ~= 103 trading days. Must comfortably exceed 50 so SMA-50
# and classify_trend() actually resolve, plus warm-up room for ADX-14.
PRICE_HISTORY_DAYS = 150

# --- Market regime filter ---
# Broad-market (SPY) trend/volatility check, evaluated independently of any
# individual ticker's setup. Multiplies (not replaces) the position-sizing
# cap above -- see get_market_regime() in trader.py and evaluate_market_regime()
# in market_regime.py. A multiplier of 0.0 hard-blocks all new buys (adds and
# opens) via the existing MAX_POSITION_PCT cap math; it never affects sells,
# so the bot can still exit/trim positions during a bearish or volatile regime.
MARKET_HIGH_VOLATILITY_THRESHOLD = 2.5  # 20-day realized volatility (%) considered "elevated"
REGIME_POSITION_MULTIPLIERS = {
    "BULLISH": 1.0,
    "NEUTRAL": 0.6,
    "BEARISH": 0.0,
    "HIGH_VOLATILITY": 0.3,
}

# --- Quantitative pre-screen ---
# New candidates (news-driven or watchlist) below this signal_score.py score
# are filtered out in decide.py and never shown to Gemini at all. Existing
# holdings are never filtered this way -- a bad score on something you own
# is a reason to consider exiting, not a reason to hide it from review.
MIN_SIGNAL_SCORE_TO_CONSIDER = 55
