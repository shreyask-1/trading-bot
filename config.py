"""
API keys and bot settings, all in one place.
Keys are read from environment variables -- never hardcoded, never committed.
"""

import os

from sp500_data import SP500

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
        + ". Set them as GitHub Actions secrets, or export them locally."
    )

FINNHUB_API_KEY = os.environ["FINNHUB_API_KEY"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
ALPACA_API_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]

# --- Universe ---
MAX_NEWS_ITEMS = 15  # headlines considered per run

# The technical universe is now the ENTIRE S&P 500. Every name gets scanned
# over the course of the day via a rotating slice (UNIVERSE_SCAN_PER_RUN per
# run) so we get full coverage without hammering the data API in one shot.
# News-matched tickers are ALWAYS scored regardless of the slice.
WATCHLIST = [ticker for ticker, _ in SP500]
# How many non-news universe tickers are scanned per run (rotating window).
UNIVERSE_SCAN_PER_RUN = int(os.environ.get("UNIVERSE_SCAN_PER_RUN", 40))

# --- Data feed ---
ALPACA_DATA_FEED = os.environ.get("ALPACA_DATA_FEED", "iex")

# --- Position sizing ---
MAX_POSITION_PCT = 0.15  # Hard ceiling: no single position over 15% of portfolio
MIN_CONVICTION_TO_TRADE = 6  # Conviction 1-10; below this, skip trade

# --- Cash discipline & portfolio sprawl control ---
MIN_CASH_RESERVE_PCT = 0.05  # Normally-untouchable cash fraction (5%)
MIN_TRADE_DOLLAR_AMOUNT = 25  # Buys sized below $25 skipped outright
MAX_OPEN_POSITIONS = 20  # Hard cap on distinct held tickers
CONSOLIDATION_SCORE_THRESHOLD = 70  # Force-sell excess positions scoring below this

EXCEPTIONAL_CONVICTION_THRESHOLD = 9
EXCEPTIONAL_TRADE_RESERVE_ACCESS_PCT = 0.5

# --- Market-hours gating ---
# THE big one: this bot historically submitted orders 24/7. DAY orders placed
# while the market is closed sit queued in Alpaca and ALL fill at the next
# 9:30 AM ET open at once -- which is exactly what blew through cash into
# negative margin on 2026-08-07 (cash went +$33k -> -$19k in minutes).
# When enabled, the bot never proposes or executes NEW trades unless the
# market is open. Risk management (stops) still runs so exits are never blocked.
TRADE_ONLY_DURING_MARKET_HOURS = (
    os.environ.get("TRADE_ONLY_DURING_MARKET_HOURS", "true").lower() == "true"
)

# --- Hard no-margin / exposure discipline ---
# Hard ceiling on TOTAL invested (holdings + pending buys) as a fraction of
# portfolio value. 15% per position x 20 positions mathematically allows ~3x
# leverage; this caps gross exposure so the account can never sit in margin.
MAX_TOTAL_EXPOSURE_PCT = float(os.environ.get("MAX_TOTAL_EXPOSURE_PCT", 0.90))
# Hard per-position loss cap (%): if a position is ever down this much from
# its average entry, it is force-sold even if ATR/indicator data is unavailable.
MAX_POSITION_LOSS_PCT = float(os.environ.get("MAX_POSITION_LOSS_PCT", 8.0))
# De-leveraging heals the account by selling weakest holdings until projected
# cash is back to this fraction of portfolio value (default 2% = positive cash).
DELEVERAGE_TARGET_CASH_PCT = float(os.environ.get("DELEVERAGE_TARGET_CASH_PCT", 0.02))

# --- Equity-level circuit breakers (OPT-IN; OFF by default) ---
# As requested: the bot NEVER stops buying/selling/trading for the day on its
# own. Both "stop the day" breakers default to 0.0 = disabled. The 2026-08-11
# loss was NOT a bad trading day -- it was margin caused by overnight order
# stacking. THAT failure mode is prevented by the market-hours gate, the hard
# no-margin rule, pending-order-aware cash, de-leveraging, and per-position
# stops, all of which remain ON. Re-enable either breaker by setting it to a
# positive value (e.g. DAILY_LOSS_HALT_PCT=3.0).
# Halt ALL new buys for the rest of the day if equity is down this much (%)
# from the start of the day. 0 = disabled (default). Stops and sells always run.
DAILY_LOSS_HALT_PCT = float(os.environ.get("DAILY_LOSS_HALT_PCT", 0.0))
# Drawdown sizing cut (does NOT stop trading -- it only shrinks NEW buy size to
# DELEVERAGE_SIZE_MULTIPLIER while drawdown from peak exceeds this %). Set <= 0
# to disable even this.
MAX_DRAWDOWN_DELEVERAGE_PCT = float(os.environ.get("MAX_DRAWDOWN_DELEVERAGE_PCT", 5.0))
# Flatten every position + halt the day at this drawdown from peak.
# 0 = disabled (default).
MAX_DRAWDOWN_FLATTEN_PCT = float(os.environ.get("MAX_DRAWDOWN_FLATTEN_PCT", 0.0))
DELEVERAGE_SIZE_MULTIPLIER = float(os.environ.get("DELEVERAGE_SIZE_MULTIPLIER", 0.25))
# When True (default), the equity peak is initialized to the account value on
# the first run after deploy, so an already-damaged account isn't instantly
# flattened. Set to false to enforce hard drawdown limits from day one.
RESET_EQUITY_PEAK_ON_START = (
    os.environ.get("RESET_EQUITY_PEAK_ON_START", "true").lower() == "true"
)

# --- Alerting (optional but strongly recommended) ---
# Set DISCORD_WEBHOOK_URL (or extend notify() for email/Telegram) to get pinged
# on margin, halts, de-leveraging, and forced liquidations instead of finding
# out by looking at the dashboard.
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# --- Second-trader / foreign-activity detection ---
# The bot records every order it submits to logs/bot_order_ledger.json and,
# each run, reconciles actual account holdings against what the ledger says
# it should own. Any position the bot did not create (e.g. a second bot,
# manual trading, or another cron using the same Alpaca keys) is flagged and
# alerted. If you did not put money into this account manually, you should
# never see "FOREIGN ACTIVITY" lines.
ENABLE_FOREIGN_ACTIVITY_DETECTION = (
    os.environ.get("ENABLE_FOREIGN_ACTIVITY_DETECTION", "true").lower() == "true"
)

# --- Per-trade stop-loss / take-profit ---
# On every buy the bot computes a stop-loss and take-profit from the specific
# trade's own setup: swing high/low over SWING_LOOKBACK_DAYS, clamped to a
# sane multiple of ATR (MIN/MAX_STOP_DISTANCE_ATR_MULT etc.), or Gemini's
# custom levels when it provides them. These are saved to
# logs/custom_exits.json and enforced every run by check_atr_stop_take_profit.

# --- Daytrading mode (dual focus: news catalysts + chart/technicals) ---
# When True the bot acts as a news+technical day trader: news-driven
# candidates get a sentiment boost to their signal score, entries prefer
# opening-range breakouts and VWAP-aligned intraday momentum, and all
# positions are flattened back to cash at END_OF_DAY_FLATTEN_TIME ET so the
# account never carries overnight risk (the 2026-08-11 liquidation happened
# at 3:30 AM ET on an overnight position).
DAYTRADE_MODE = os.environ.get("DAYTRADE_MODE", "true").lower() == "true"
END_OF_DAY_FLATTEN = (
    os.environ.get("END_OF_DAY_FLATTEN", "true").lower() == "true"
)
END_OF_DAY_FLATTEN_TIME = os.environ.get("END_OF_DAY_FLATTEN_TIME", "15:50")
# First OPENING_RANGE_BARS 5-minute bars after 9:30 ET form the opening range;
# price breaking above/below it is a classic daytrading entry signal.
OPENING_RANGE_BARS = int(os.environ.get("OPENING_RANGE_BARS", 3))
# Skip NEW buys in the first N minutes of the session (open auction chop).
TRADE_START_MINUTES_AFTER_OPEN = int(os.environ.get("TRADE_START_MINUTES_AFTER_OPEN", 15))
# Skip NEW buys after this ET time (no late-session entries).
STOP_NEW_BUYS_AFTER = os.environ.get("STOP_NEW_BUYS_AFTER", "15:30")
# Signal-score points added per unit of headline sentiment (-1..+1) for
# news-driven candidates. E.g. weight 10 = up to +/-10 points on the 0-100
# scale, enough to push a 50-60 candidate over the 55 pre-screen bar.
NEWS_SENTIMENT_WEIGHT = float(os.environ.get("NEWS_SENTIMENT_WEIGHT", 10.0))

# --- Risk management ---
ATR_PERIOD = 14
ATR_STOP_MULTIPLIER = 2.5
ATR_TAKE_PROFIT_MULTIPLIER = 4.0

SWING_LOOKBACK_DAYS = 10
MIN_STOP_DISTANCE_ATR_MULT = 1.0
MAX_STOP_DISTANCE_ATR_MULT = 5.0
MIN_TAKE_PROFIT_DISTANCE_ATR_MULT = 1.5
MAX_TAKE_PROFIT_DISTANCE_ATR_MULT = 8.0

ALLOW_GEMINI_CUSTOM_EXITS = (
    os.environ.get("ALLOW_GEMINI_CUSTOM_EXITS", "true").lower() == "true"
)

# --- Profit-maximizing entry filters & trailing stops ---
# Chase filters (set <= 0 to disable): never buy into a name already extended
# on the day -- chasing is the #1 way daytraders give back gains. Sells are
# never filtered by these.
# Skip buys where price is more than this % above VWAP.
MAX_BUY_EXTENSION_ABOVE_VWAP_PCT = float(os.environ.get("MAX_BUY_EXTENSION_ABOVE_VWAP_PCT", 2.5))
# Skip buys where the name is already up more than this % on the session
# (gap + run = reversion risk).
MAX_INTRADAY_MOVE_PCT = float(os.environ.get("MAX_INTRADAY_MOVE_PCT", 4.0))
# Trailing stop: once a position is up TRAILING_STOP_ACTIVATE_MULT x ATR from
# entry, the stop ratchets up to (best price - TRAILING_STOP_DISTANCE_MULT x
# ATR). The stop only ever moves up, so winners are banked instead of given
# back. Persisted in logs/custom_exits.json.
TRAILING_STOP_ACTIVATE_MULT = float(os.environ.get("TRAILING_STOP_ACTIVATE_MULT", 1.5))
TRAILING_STOP_DISTANCE_MULT = float(os.environ.get("TRAILING_STOP_DISTANCE_MULT", 2.0))

# --- Risk-based position sizing (profit lever) ---
# Size each buy so a stop-out costs at most MAX_RISK_PER_TRADE_PCT of
# portfolio equity, computed from the trade's ACTUAL stop distance (tighter
# stop = bigger position, wider stop = smaller). Uniform per-trade pain is
# what lets compounding work. If no stop is known, the hard
# MAX_POSITION_LOSS_PCT distance is assumed. 0 disables.
MAX_RISK_PER_TRADE_PCT = float(os.environ.get("MAX_RISK_PER_TRADE_PCT", 0.75))

# --- Time-of-day size multipliers (daytrading edge windows) ---
# New buys are multiplied by the factor for the current ET window; the lunch
# lull trades smaller, the open/closing windows trade full size. Sells are
# never affected. Format: (h_start, m_start, h_end, m_end): multiplier
TIME_OF_DAY_MULTIPLIERS = {
    (9, 30, 11, 0): 1.0,    # open power hour
    (11, 0, 11, 30): 0.7,
    (11, 30, 13, 30): 0.5,  # lunch lull
    (13, 30, 15, 0): 0.8,
    (15, 0, 16, 0): 1.0,    # closing push
}

# --- News + technical confluence (profit lever) ---
# A news-driven candidate only reaches Gemini if its PURE technical score
# (before the sentiment boost) is at least this value -- headline alone is
# not a setup; the chart must agree at least somewhat. 0 disables.
NEWS_CONFLUENCE_MIN_TECH_SCORE = float(os.environ.get("NEWS_CONFLUENCE_MIN_TECH_SCORE", 50.0))

# --- Intraday analysis ---
ENABLE_INTRADAY_ANALYSIS = (
    os.environ.get("ENABLE_INTRADAY_ANALYSIS", "true").lower() == "true"
)
INTRADAY_BAR_MINUTES = int(os.environ.get("INTRADAY_BAR_MINUTES", 5))
INTRADAY_LOOKBACK_DAYS = int(os.environ.get("INTRADAY_LOOKBACK_DAYS", 2))

# --- Cooldown & dedup ---
TRADE_COOLDOWN_MINUTES = 30
NEWS_DEDUP_MAX_AGE_HOURS = 48

# --- Model & quota ---
# NOTE: decide.py now discovers live, currently-valid model IDs from
# Google's own ListModels endpoint at runtime and caches them for the day --
# this static list below is ONLY used as a last-resort fallback if that
# discovery call itself fails (e.g. no network). Don't rely on these names
# staying accurate forever; Google renames/retires models over time (this
# is exactly what caused the gemini-1.5-flash / gemini-1.5-flash-8b 404s).
GEMINI_MODEL_FALLBACKS = [
    "gemini-flash-latest",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", GEMINI_MODEL_FALLBACKS[0])

GEMINI_MODEL_LIMITS = {
    "gemini-flash-latest": {"rpd": int(os.environ.get("GEMINI_RPD_FLASH_LATEST", 1500)), "rpm": int(os.environ.get("GEMINI_RPM_FLASH_LATEST", 15))},
    "gemini-2.0-flash": {"rpd": int(os.environ.get("GEMINI_RPD_20_FLASH", 1500)), "rpm": int(os.environ.get("GEMINI_RPM_20_FLASH", 15))},
    "gemini-2.0-flash-lite": {"rpd": int(os.environ.get("GEMINI_RPD_20_FLASH_LITE", 1500)), "rpm": int(os.environ.get("GEMINI_RPM_20_FLASH_LITE", 15))},
}

GEMINI_QUOTA_RESET_TIMEZONE = "America/Los_Angeles"
# ~52 weeks of daily bars so 52-week high/low distances are available in the
# same fetch as all other indicators (same API cost).
PRICE_HISTORY_DAYS = 400

# --- Market regime filter ---
MARKET_HIGH_VOLATILITY_THRESHOLD = 2.5
REGIME_POSITION_MULTIPLIERS = {
    "BULLISH": 1.0,
    "NEUTRAL": 0.6,
    "BEARISH": 0.0,
    "HIGH_VOLATILITY": 0.3,
}

# --- Quantitative pre-screen ---
MIN_SIGNAL_SCORE_TO_CONSIDER = 55

# --- Better news filtering ---
# Every article is scored 0-10 (news.score_article: earnings beats and
# partnerships score high, interviews and store openings score low). Articles
# below this threshold never reach Gemini. 0 = no filtering.
NEWS_MIN_SCORE_TO_CONSIDER = float(os.environ.get("NEWS_MIN_SCORE_TO_CONSIDER", 5.0))

# --- Confidence-based position sizing ---
# Gemini returns a confidence score 0-100 (instead of a raw dollar amount) and
# the code converts it to a target position size as a % of portfolio equity.
# (threshold, size % of equity), checked top-down. Below the lowest threshold
# the trade is skipped outright.
CONFIDENCE_SIZING = [
    (90, 0.08),  # 90+  -> 8%
    (80, 0.05),  # 80+  -> 5%
    (70, 0.03),  # 70+  -> 3%
    (60, 0.02),  # 60+  -> 2%
]
CONFIDENCE_MIN_TO_TRADE = 60  # below this, no position

# --- Smarter exits ---
# Moving-average breakdown: exit a long when price closes below SMA-20.
ENABLE_MA_BREAKDOWN_EXIT = os.environ.get("ENABLE_MA_BREAKDOWN_EXIT", "true").lower() == "true"
# RSI exhaustion: exit a long when RSI-14 pushes above this (overbought
# exhaustion after a run).
ENABLE_RSI_EXHAUSTION_EXIT = os.environ.get("ENABLE_RSI_EXHAUSTION_EXIT", "true").lower() == "true"
RSI_EXHAUSTION_LEVEL = float(os.environ.get("RSI_EXHAUSTION_LEVEL", 75.0))
# Negative-news exit: exit a position when the most recent news for the
# ticker carries strong negative sentiment (uses the last news fetch).
ENABLE_NEGATIVE_NEWS_EXIT = os.environ.get("ENABLE_NEGATIVE_NEWS_EXIT", "true").lower() == "true"
NEGATIVE_NEWS_SENTIMENT_THRESHOLD = float(os.environ.get("NEGATIVE_NEWS_SENTIMENT_THRESHOLD", -0.4))

# --- Market regime filter (SPY + QQQ + VIX) ---
# VIX stress levels (best-effort: tries "VIX", then "UVXY" as a proxy, then
# falls back to SPY realized volatility). Above the stress level the bot goes
# HIGH_VOLATILITY/defensive; above severe it goes fully defensive (BEARISH).
MARKET_VIX_STRESS_LEVEL = float(os.environ.get("MARKET_VIX_STRESS_LEVEL", 30.0))
MARKET_VIX_SEVERE_LEVEL = float(os.environ.get("MARKET_VIX_SEVERE_LEVEL", 40.0))

# --- Pure-technical fallback ---
TECHNICAL_MIN_CONVICTION = int(os.environ.get("TECHNICAL_MIN_CONVICTION", 5))
TECHNICAL_CONVICTION_AGGRESSIVENESS = float(
    os.environ.get("TECHNICAL_CONVICTION_AGGRESSIVENESS", 0.8)
)

# --- Phase 2: data feeds (economic calendar, analyst, insider, SEC, Reddit) ---
# All feeds are fail-soft: a missing key, network error, or blocked IP never
# breaks a run -- the bot just trades without that feed.
ENABLE_ECONOMIC_CALENDAR = os.environ.get("ENABLE_ECONOMIC_CALENDAR", "true").lower() == "true"
ENABLE_ANALYST_ACTIONS = os.environ.get("ENABLE_ANALYST_ACTIONS", "true").lower() == "true"
ENABLE_INSIDER_ACTIVITY = os.environ.get("ENABLE_INSIDER_ACTIVITY", "true").lower() == "true"
ENABLE_SEC_FILINGS = os.environ.get("ENABLE_SEC_FILINGS", "true").lower() == "true"
ENABLE_REDDIT_SENTIMENT = os.environ.get("ENABLE_REDDIT_SENTIMENT", "true").lower() == "true"
# Per-ticker feeds (insider/SEC) are limited so a run never makes dozens of
# API calls: only the top N candidates get the deep look, cached for 24h.
MAX_FUNDAMENTAL_TICKERS = int(os.environ.get("MAX_FUNDAMENTAL_TICKERS", 12))
# On a day with a high-impact economic event (CPI/FOMC/NFP/GDP...), new buys
# are sized down to this fraction of normal (event uncertainty is real).
HIGH_IMPACT_EVENT_SIZE_MULT = float(os.environ.get("HIGH_IMPACT_EVENT_SIZE_MULT", 0.5))
# New buys into a name reporting earnings within this many days are sized down
# (gap risk on the print is not something a daytrader should carry).
EARNINGS_PROXIMITY_DAYS = int(os.environ.get("EARNINGS_PROXIMITY_DAYS", 3))
EARNINGS_PROXIMITY_SIZE_MULT = float(os.environ.get("EARNINGS_PROXIMITY_SIZE_MULT", 0.5))
# Boosts applied by signal_score.py from the Phase 2 feeds (0 disables each).
ANALYST_UPGRADE_BOOST = float(os.environ.get("ANALYST_UPGRADE_BOOST", 8.0))
ANALYST_DOWNGRADE_PENALTY = float(os.environ.get("ANALYST_DOWNGRADE_PENALTY", 8.0))
INSIDER_BUY_BOOST = float(os.environ.get("INSIDER_BUY_BOOST", 6.0))
INSIDER_SELL_PENALTY = float(os.environ.get("INSIDER_SELL_PENALTY", 5.0))
REDDIT_SENTIMENT_WEIGHT = float(os.environ.get("REDDIT_SENTIMENT_WEIGHT", 8.0))
HIGH_IMPACT_EVENT_PENALTY = float(os.environ.get("HIGH_IMPACT_EVENT_PENALTY", 5.0))

# --- Phase 3: self-learning statistics ---
# The bot tracks every trade in logs/trades_journal.csv (reason, confidence,
# stop, outcome) and pairs buys/sells into logs/trade_results.csv. From that
# it learns which SETUPS actually win, and sizes future entries of a setup by
# its demonstrated expectancy (this is the "increase weighting toward
# successful patterns" loop). Multiplier is clamped to [MIN, MAX] and only
# kicks in once a setup has >= MIN_SAMPLES closed trades.
SELF_LEARNING_ENABLED = os.environ.get("SELF_LEARNING_ENABLED", "true").lower() == "true"
SETUP_MULT_MIN = float(os.environ.get("SETUP_MULT_MIN", 0.5))
SETUP_MULT_MAX = float(os.environ.get("SETUP_MULT_MAX", 1.5))
SELF_LEARNING_MIN_SAMPLES = int(os.environ.get("SELF_LEARNING_MIN_SAMPLES", 5))
SELF_LEARNING_EDGE_MIN = float(os.environ.get("SELF_LEARNING_EDGE_MIN", 0.0))  # min avg win % for a positive multiplier
