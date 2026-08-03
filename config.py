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

# --- Risk management: chart-based custom exits, with ATR as a fallback/floor ---
# Every filled BUY gets its OWN stop-loss and take-profit computed from
# actual recent chart structure (10-day swing low/high), not a single
# flat multiplier applied identically to every ticker. Gemini may also
# propose its own specific stop_loss/take_profit price per trade (see
# decide.py's prompt/schema) if it identifies a better level -- those are
# sanity-clamped against the multipliers below so a bad Gemini suggestion
# can't set an absurdly tight or absurdly wide stop.
#
# ATR (Average True Range) is still used as the sanity-bound unit AND as
# the fallback if swing-based levels can't be computed (e.g. insufficient
# history) -- it measures how much a stock typically moves per day, in
# its own price terms, so the bound scales per-ticker automatically.
ATR_PERIOD = 14
ATR_STOP_MULTIPLIER = 2.5          # fallback stop-loss = entry price - (2.5 x ATR), if no swing data available
ATR_TAKE_PROFIT_MULTIPLIER = 4.0   # fallback take-profit = entry price + (4.0 x ATR), if no swing data available

SWING_LOOKBACK_DAYS = 10           # how many recent daily bars define "the chart's" swing low/high
MIN_STOP_DISTANCE_ATR_MULT = 1.0   # a stop can never be tighter than this many ATRs from entry (avoids noise stopouts)
MAX_STOP_DISTANCE_ATR_MULT = 5.0   # a stop can never be further than this many ATRs from entry (avoids runaway risk)
MIN_TAKE_PROFIT_DISTANCE_ATR_MULT = 1.5
MAX_TAKE_PROFIT_DISTANCE_ATR_MULT = 8.0

# Whether Gemini-proposed custom stop_loss/take_profit prices (see
# decide.py) are honored at all. If False, every position always gets the
# system-computed swing/ATR-based default regardless of what Gemini asks
# for -- useful if you ever want to fully remove Gemini's influence over
# exit levels while still letting it pick entries.
ALLOW_GEMINI_CUSTOM_EXITS = os.environ.get("ALLOW_GEMINI_CUSTOM_EXITS", "true").lower() == "true"

# --- Intraday analysis ---
# In addition to the existing daily-bar analysis, each ticker also gets a
# short-term intraday read (RSI, momentum, trend, VWAP deviation) computed
# from recent 5-minute bars. This gives the bot day-trading-relevant
# context (e.g. "up on the daily chart AND showing intraday momentum right
# now") instead of relying on daily bars alone, which can be a day stale
# on timing.
#
# CAVEAT: this roughly DOUBLES the number of Alpaca market-data API calls
# per run (one extra fetch per ticker). If you notice run slowdowns or
# GitHub Actions workflow timeouts after enabling this, either disable it
# here, shrink WATCHLIST, or increase `timeout-minutes` in
# .github/workflows/run-bot.yml.
ENABLE_INTRADAY_ANALYSIS = os.environ.get("ENABLE_INTRADAY_ANALYSIS", "true").lower() == "true"
INTRADAY_BAR_MINUTES = int(os.environ.get("INTRADAY_BAR_MINUTES", 5))
INTRADAY_LOOKBACK_DAYS = int(os.environ.get("INTRADAY_LOOKBACK_DAYS", 2))

# --- Cooldown & dedup ---
TRADE_COOLDOWN_MINUTES = 30      # don't re-trade the same ticker within this window
# NOTE: this cooldown applies to Gemini-proposed trades only. Forced risk-management
# exits (stop-loss / take-profit) deliberately ignore it -- see
# check_atr_stop_take_profit() in trader.py.
NEWS_DEDUP_MAX_AGE_HOURS = 48    # forget "already seen" articles older than this

# --- Model & quota (CONFIRMED live from https://ai.dev/rate-limit) ---
# Each model below has its OWN independent free-tier daily quota (RPD) --
# they do NOT share a pool. Using all three in rotation therefore gives a
# combined effective daily budget of ~60 calls/day instead of ~20/day for
# a single model. Order matters: earlier entries are preferred when
# multiple still have quota remaining this run.
#
# CONFIRMED limits as of your last dashboard check (https://ai.dev/rate-limit):
#   gemini-2.5-flash-lite : 10 RPM, 20 RPD
#   gemini-2.5-flash      :  5 RPM, 20 RPD
#   gemini-flash-latest   :  5 RPM, 20 RPD  (currently resolves to gemini-3.6-flash)
#
# decide.py also self-corrects: if Google's actual 429 response ever
# reports a different real quotaValue than what's configured here, that
# real number is parsed out and adopted automatically for the rest of the
# day, so these defaults staying slightly stale over time isn't fatal.
GEMINI_MODEL_FALLBACKS = [
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-flash-latest",
]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", GEMINI_MODEL_FALLBACKS[0])

GEMINI_MODEL_LIMITS = {
    "gemini-2.5-flash-lite": {
        "rpd": int(os.environ.get("GEMINI_RPD_25_FLASH_LITE", 20)),
        "rpm": int(os.environ.get("GEMINI_RPM_25_FLASH_LITE", 10)),
    },
    "gemini-2.5-flash": {
        "rpd": int(os.environ.get("GEMINI_RPD_25_FLASH", 20)),
        "rpm": int(os.environ.get("GEMINI_RPM_25_FLASH", 5)),
    },
    "gemini-flash-latest": {
        "rpd": int(os.environ.get("GEMINI_RPD_FLASH_LATEST", 20)),
        "rpm": int(os.environ.get("GEMINI_RPM_FLASH_LATEST", 5)),
    },
}

# Google resets daily API quotas at midnight Pacific Time (not UTC, not
# your local time) -- used so each model's daily counter rolls over in
# sync with the actual quota refill.
GEMINI_QUOTA_RESET_TIMEZONE = "America/Los_Angeles"

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

# --- Pure-technical fallback (when a Gemini call is throttled) ---
# When the Gemini quota-spacing gate blocks a call, a pure-technical
# decision engine generates trade ideas based on indicators + quant score
# alone (including the chart-based swing levels and intraday context
# above), so the bot keeps trading every run even between Gemini calls.
TECHNICAL_MIN_CONVICTION = int(os.environ.get("TECHNICAL_MIN_CONVICTION", 5))
TECHNICAL_CONVICTION_AGGRESSIVENESS = float(os.environ.get("TECHNICAL_CONVICTION_AGGRESSIVENESS", 0.8))
