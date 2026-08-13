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
# More headlines per run (still one Finnhub call; the fetch returns up to
# MAX_NEWS_ITEMS * 4 and we keep the top scored). The 60s run cap gives the
# matcher room to chew through more articles for better coverage.
MAX_NEWS_ITEMS = 40  # headlines considered per run

# The technical universe is now the ENTIRE S&P 500. Every name gets scanned
# over the course of the day via a rotating slice (UNIVERSE_SCAN_PER_RUN per
# run) so we get full coverage without hammering the data API in one shot.
# News-matched tickers are ALWAYS scored regardless of the slice.
WATCHLIST = [ticker for ticker, _ in SP500]
# How many non-news universe tickers are scanned per run (rotating window).
# 60 gives Gemini a bigger slice of the S&P 500 every call while staying
# inside the 60s run budget (the rotating slice is cached per hour -- see
# decide.py -- so only the first run of each hour pays the fresh-scan cost,
# and that is capped by SCAN_REFRESH_BUDGET_SECONDS).
UNIVERSE_SCAN_PER_RUN = int(os.environ.get("UNIVERSE_SCAN_PER_RUN", 60))

# --- Data feed ---
ALPACA_DATA_FEED = os.environ.get("ALPACA_DATA_FEED", "iex")

# --- Position sizing ---
# FLAT sizing (ON by default): EVERY trade is sized the same -- FLAT_TRADE_SIZE_PCT
# of portfolio equity, capped only by the per-position ceiling and the
# regime/breaker multiplier. Confidence/conviction/time-of-day/setup-learning/
# economic-event/earnings multipliers do NOT change size in flat mode;
# confidence still GATES (below CONFIDENCE_MIN_TO_TRADE the trade is skipped,
# it just doesn't resize). Set FLAT_SIZING=false to restore per-confidence
# tiered sizing.
FLAT_SIZING = os.environ.get("FLAT_SIZING", "true").lower() == "true"
FLAT_TRADE_SIZE_PCT = float(os.environ.get("FLAT_TRADE_SIZE_PCT", 0.10))
MAX_POSITION_PCT = 0.15  # Hard ceiling: no single position over 15% of portfolio
MIN_CONVICTION_TO_TRADE = 6  # Conviction 1-10; below this, skip trade

# --- Cash discipline & portfolio sprawl control ---
MIN_CASH_RESERVE_PCT = 0.05  # Normally-untouchable cash fraction (5%)
MIN_TRADE_DOLLAR_AMOUNT = 25  # Buys sized below $25 skipped outright
MAX_OPEN_POSITIONS = 20  # Hard cap on distinct held tickers
CONSOLIDATION_SCORE_THRESHOLD = 70  # Force-sell excess positions scoring below this

EXCEPTIONAL_CONVICTION_THRESHOLD = 9
EXCEPTIONAL_TRADE_RESERVE_ACCESS_PCT = 0.5

# --- Trading sessions: 24/7 with an overnight queue ---
# The bot runs 24/7 and never idles:
#   * Regular session (9:30-16:00 ET): trades execute immediately.
#   * Extended session (4:00-9:30 AM / 4:00-8:00 PM ET): trades execute
#     immediately as LIMIT orders at the last traded price with
#     extended_hours=True (Alpaca rejects market orders there).
#   * Overnight dead zone (8:00 PM - 4:00 AM ET, weekends, holidays): no
#     order can fill, so instead of submitting blind orders (the 2026-08-07
#     failure mode, where queued DAY market orders ALL filled at once at the
#     open) the bot QUEUES its trade ideas to data/pending_trades.json. The
#     first live-session run hands the queue to Gemini for re-verification
#     against fresh data, and only the re-confirmed trades are placed.
# TRADE_ONLY_DURING_MARKET_HOURS=true restores the old strict gate (regular
# session only, everything else queues); default false = 24/7 behavior.
TRADE_ONLY_DURING_MARKET_HOURS = (
    os.environ.get("TRADE_ONLY_DURING_MARKET_HOURS", "false").lower() == "true"
)
ALLOW_EXTENDED_HOURS = (
    os.environ.get("ALLOW_EXTENDED_HOURS", "true").lower() == "true"
)
# Master switch for the overnight queue + next-morning Gemini verification.
OVERNIGHT_QUEUE_ENABLED = (
    os.environ.get("OVERNIGHT_QUEUE_ENABLED", "true").lower() == "true"
)

# --- Hard no-margin / exposure discipline ---
# Hard ceiling on TOTAL invested (holdings + pending buys) as a fraction of
# portfolio value. 15% per position x 20 positions mathematically allows ~3x
# leverage; this caps gross exposure so the account can never sit in margin.
# 0.95 lets the bot deploy the usable cash beyond the 5% MIN_CASH_RESERVE_PCT
# (at 0.90 the account gets stuck 'full' with ~$5k cash idle and every buy
# skipped as below minimum size -- exactly the 2026-08-13 log); the reserve
# still guarantees cash is never fully spent.
MAX_TOTAL_EXPOSURE_PCT = float(os.environ.get("MAX_TOTAL_EXPOSURE_PCT", 0.95))
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
# When True the bot acts as a news+technical trader: news-driven candidates
# get a sentiment boost to their signal score, entries prefer opening-range
# breakouts and VWAP-aligned intraday momentum, and the same opening-range /
# intraday context is fed to Gemini. DAYTRADE_MODE does NOT force an
# end-of-day exit by itself.
DAYTRADE_MODE = os.environ.get("DAYTRADE_MODE", "true").lower() == "true"
# Overnight holds (short-term AND long-term / swing positions): default OFF
# so winners may be held past the close and managed over days. The per-trade
# stop-loss / trailing stop / hard loss cap / negative-news exits still
# protect every position around the clock. Set END_OF_DAY_FLATTEN=true to
# force-flatten everything back to cash at END_OF_DAY_FLATTEN_TIME ET (the
# old daytrading-only discipline; the 2026-08-11 liquidation hit an
# overnight position, which is why this used to be on).
END_OF_DAY_FLATTEN = (
    os.environ.get("END_OF_DAY_FLATTEN", "false").lower() == "true"
)
END_OF_DAY_FLATTEN_TIME = os.environ.get("END_OF_DAY_FLATTEN_TIME", "15:50")
# First OPENING_RANGE_BARS 5-minute bars after 9:30 ET form the opening range;
# price breaking above/below it is a classic daytrading entry signal.
OPENING_RANGE_BARS = int(os.environ.get("OPENING_RANGE_BARS", 3))
# Skip NEW buys in the first N minutes of the session (open auction chop).
# Default 0 = no opening restriction (24/7 trading; the overnight queue
# already handles everything pre-open).
TRADE_START_MINUTES_AFTER_OPEN = int(os.environ.get("TRADE_START_MINUTES_AFTER_OPEN", 0))
# Skip NEW buys after this ET time (no late-session entries). Default 23:59
# = disabled: with ALLOW_EXTENDED_HOURS on, after-hours entries are wanted,
# so the only regular-session restriction left is
# TRADE_START_MINUTES_AFTER_OPEN (the open-auction chop).
STOP_NEW_BUYS_AFTER = os.environ.get("STOP_NEW_BUYS_AFTER", "23:59")
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
# Volatility-scaled (risk-parity) sizing: ON by default. Even in FLAT sizing
# mode, each buy is additionally capped so a stop-out costs at most
# MAX_RISK_PER_TRADE_PCT of equity -- a tight-stop name gets a bigger
# position, a wide-stop name gets a smaller one, so EVERY trade risks the
# SAME dollar amount regardless of the stock's volatility. Set false to
# revert to pure uniform dollar sizing.
RISK_PARITY_SIZING = os.environ.get("RISK_PARITY_SIZING", "true").lower() == "true"

# --- Sector concentration caps (loss protection) ---
# The bot never holds more than this fraction of portfolio equity in any ONE
# GICS sector. 14+ positions can quietly become 5 names in Energy and a
# sector shock then hits them all at once; this caps that correlated risk.
# New buys into an already-heavy sector are skipped (existing positions are
# left alone -- no forced selling of winners). Set 0 to disable.
MAX_SECTOR_EXPOSURE_PCT = float(os.environ.get("MAX_SECTOR_EXPOSURE_PCT", 0.25))

# --- Free RSS news feeds (profit lever) ---
# Beyond Finnhub's general market wire, the bot pulls headline RSS feeds that
# cost nothing: Google News US top stories + Yahoo Finance index headlines.
# They are parsed with the stdlib (no new dependency) and piped through the
# same ticker-matching + importance-scoring pipeline, so more catalysts reach
# Gemini without spending a single Finnhub quota call.
ENABLE_RSS_NEWS = os.environ.get("ENABLE_RSS_NEWS", "true").lower() == "true"
RSS_FETCH_TIMEOUT_SECONDS = 8.0

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
# Every trade is sized the same in normal conditions (NEUTRAL and BULLISH are
# both 1.0 -- no per-trade or per-regime size variation). BEARISH still blocks
# ALL new buys (confirmed bear tape = no entries) and HIGH_VOLATILITY trades
# defensively at half size. This is a market-level gate, not a per-trade size
# difference.
REGIME_POSITION_MULTIPLIERS = {
    "BULLISH": 1.0,
    "NEUTRAL": 1.0,
    "BEARISH": 0.0,
    "HIGH_VOLATILITY": 0.5,
}

# --- Quantitative pre-screen ---
MIN_SIGNAL_SCORE_TO_CONSIDER = 55

# --- Better news filtering ---
# Every article is scored 0-10 (news.score_article: earnings beats and
# partnerships score high, interviews and store openings score low). Articles
# below this threshold never reach Gemini. 0 = no filtering.
NEWS_MIN_SCORE_TO_CONSIDER = float(os.environ.get("NEWS_MIN_SCORE_TO_CONSIDER", 5.0))

# --- Confidence-based position sizing (used only when FLAT_SIZING=false) ---
# Gemini returns a confidence score 0-100 (instead of a raw dollar amount) and
# the code converts it to a target position size as a % of portfolio equity.
# (threshold, size % of equity), checked top-down. Below the lowest threshold
# the trade is skipped outright. With FLAT_SIZING=true (default), confidence
# only gates (min to trade) and every trade gets FLAT_TRADE_SIZE_PCT instead.
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
# VIX stress levels (best-effort: only used when the data feed actually
# provides the CBOE VIX index; otherwise SPY/QQQ realized volatility is the
# elevated-volatility signal). Above the stress level the bot goes
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
# Analyst upgrades/downgrades need a PAID Finnhub plan (free keys get HTTP
# 403) -- off by default so the bot is fully free-plan accustomed with zero
# error spam. Everything else (news, earnings, insider transactions, SEC
# filings, Reddit best-effort) is free-tier.
ENABLE_ANALYST_ACTIONS = os.environ.get("ENABLE_ANALYST_ACTIONS", "false").lower() == "true"
ENABLE_INSIDER_ACTIVITY = os.environ.get("ENABLE_INSIDER_ACTIVITY", "true").lower() == "true"
ENABLE_SEC_FILINGS = os.environ.get("ENABLE_SEC_FILINGS", "true").lower() == "true"
ENABLE_REDDIT_SENTIMENT = os.environ.get("ENABLE_REDDIT_SENTIMENT", "true").lower() == "true"
# Per-ticker feeds (insider/SEC) are limited so a run never makes dozens of
# API calls: only the top N candidates get the deep look, cached for 24h.
# Per-run cap on how many tickers get fresh per-ticker fundamental fetches
# (insider transactions / SEC filings). 12 + the FEED_REFRESH_BUDGET_SECONDS
# wall-clock cap keeps more candidates covered now that the run has a 60s
# budget, while still guaranteeing feeds never dominate runtime.
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
