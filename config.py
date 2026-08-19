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
# FLAT sizing (ON by default): trades start from the same
# FLAT_TRADE_SIZE_PCT of equity. Confidence/conviction/time-of-day/setup/
# event multipliers do not deliberately resize normal flat trades, but risk,
# exposure, sector, cash, chase, and stop-distance caps may reduce a trade.
# Fallback trades have their own smaller cap. Set FLAT_SIZING=false to restore
# confidence-tiered sizing.
FLAT_SIZING = os.environ.get("FLAT_SIZING", "true").lower() == "true"
FLAT_TRADE_SIZE_PCT = float(os.environ.get("FLAT_TRADE_SIZE_PCT", 0.10))
MAX_POSITION_PCT = 0.15  # Hard ceiling: no single position over 15% of portfolio
MIN_CONVICTION_TO_TRADE = 6  # Conviction 1-10; below this, skip trade

# --- Cash discipline & portfolio sprawl control ---
# Keep a larger cash buffer so the bot can react to new setups and absorb
# slippage instead of running permanently at the exposure ceiling.
MIN_CASH_RESERVE_PCT = float(os.environ.get("MIN_CASH_RESERVE_PCT", 0.10))  # 10% reserve

# Do not let exceptional confidence bypass the reserve. A sell can still fund
# a replacement through the high-conviction swap path, but buys cannot spend
# the reserve by themselves.
EXCEPTIONAL_TRADE_RESERVE_ACCESS_PCT = float(os.environ.get("EXCEPTIONAL_TRADE_RESERVE_ACCESS_PCT", 0.0))
MIN_TRADE_DOLLAR_AMOUNT = 25  # Buys sized below $25 skipped outright
MAX_OPEN_POSITIONS = 20  # Hard cap on distinct held tickers
CONSOLIDATION_SCORE_THRESHOLD = 70  # Force-sell excess positions scoring below this

EXCEPTIONAL_CONVICTION_THRESHOLD = 9
# Defined above next to the cash-reserve setting; kept here only as a comment
# marker so older configuration readers can find the related control.

# --- High-conviction swaps ---
# When a genuinely outstanding new idea (Gemini confidence >= SWAP_MIN_CONFIDENCE
# OR conviction >= SWAP_MIN_CONVICTION) can't fund because the account is fully
# deployed (cash/room below the min trade size), the bot may SELL its smallest
# existing winner -- a holding already up at least SWAP_MIN_WINNER_PCT -- to
# free capital for the better setup. Deterministic and conservative: the
# SMALLEST qualifying winner is sold first (banks a modest gain, keeps the big
# winners compounding), the sale never targets a position that is down, and a
# per-run cap stops churn. If the freed capital is still below the min trade
# size, the new idea is skipped with the swap noted in the reason.
ENABLE_HIGH_CONVICTION_SWAPS = os.environ.get("ENABLE_HIGH_CONVICTION_SWAPS", "true").lower() == "true"
SWAP_MIN_CONFIDENCE = float(os.environ.get("SWAP_MIN_CONFIDENCE", 90))
SWAP_MIN_CONVICTION = int(os.environ.get("SWAP_MIN_CONVICTION", 9))
SWAP_MIN_WINNER_PCT = float(os.environ.get("SWAP_MIN_WINNER_PCT", 3.0))
SWAP_MAX_PER_RUN = int(os.environ.get("SWAP_MAX_PER_RUN", 2))

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
# 0.90 leaves room for new setups and spread/slippage while the separate
# cash-reserve rule prevents buys from consuming the reserve.
MAX_TOTAL_EXPOSURE_PCT = float(os.environ.get("MAX_TOTAL_EXPOSURE_PCT", 0.90))
# Hard per-position loss cap (%): if a position is ever down this much from
# its average entry, it is force-sold even if ATR/indicator data is unavailable.
MAX_POSITION_LOSS_PCT = float(os.environ.get("MAX_POSITION_LOSS_PCT", 8.0))
# De-leveraging heals the account by selling weakest holdings until projected
# cash is back to this fraction of portfolio value (default 2% = positive cash).
DELEVERAGE_TARGET_CASH_PCT = float(os.environ.get("DELEVERAGE_TARGET_CASH_PCT", 0.02))

# --- Equity-level circuit breakers ---
# A daily loss halt protects against repeated churn: it blocks NEW buys after
# the configured daily loss while stops, sells, and de-leveraging continue.
# The prior overnight-margin failure is separately prevented by the hard
# no-margin rule, pending-order-aware cash, exposure cap, and de-leveraging.
# Halt ALL new buys for the rest of the day if equity is down this much (%)
# from the start of the day. Stops and sells always run. Set to 0 to disable.
# A 3% daily loss blocks NEW buys until the next Eastern day. Risk exits,
# de-leveraging, and sells still run. This prevents churn from compounding
# during a bad session while preserving the account-protection path.
DAILY_LOSS_HALT_PCT = float(os.environ.get("DAILY_LOSS_HALT_PCT", 3.0))
# Drawdown sizing cut (does NOT stop trading -- it only shrinks NEW buy size to
# DELEVERAGE_SIZE_MULTIPLIER while drawdown from peak exceeds this %). Set <= 0
# to disable even this.
MAX_DRAWDOWN_DELEVERAGE_PCT = float(os.environ.get("MAX_DRAWDOWN_DELEVERAGE_PCT", 5.0))
# Flatten every position + halt the day at this drawdown from peak.
# 0 = disabled; the daily-loss halt above is the less-destructive default.
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

# --- Data quality, liquidity, execution, and operations ---
# New buys require a recent quote/candle and sufficient liquidity. Protective
# sells remain allowed when data is stale so the account can still reduce risk.
ENABLE_MARKET_DATA_GUARDS = os.environ.get("ENABLE_MARKET_DATA_GUARDS", "true").lower() == "true"
ALLOW_STALE_DATA_FOR_EXITS = os.environ.get("ALLOW_STALE_DATA_FOR_EXITS", "true").lower() == "true"
MAX_QUOTE_AGE_SECONDS = float(os.environ.get("MAX_QUOTE_AGE_SECONDS", 180.0))
MAX_CANDLE_AGE_MINUTES = float(os.environ.get("MAX_CANDLE_AGE_MINUTES", 30.0))
MAX_BID_ASK_SPREAD_PCT = float(os.environ.get("MAX_BID_ASK_SPREAD_PCT", 0.75))
MIN_AVG_DAILY_VOLUME = float(os.environ.get("MIN_AVG_DAILY_VOLUME", 500000))

# Optional paper-only dry-run. When enabled, buys are evaluated and written to
# logs/shadow_trades.jsonl but never submitted. Protective sells still submit.
SHADOW_MODE = os.environ.get("SHADOW_MODE", "false").lower() == "true"
# Manual emergency switch: blocks NEW buys only; stops, profit-taking,
# de-leveraging, and all other protective sells continue to operate.
MANUAL_BUY_KILL_SWITCH = os.environ.get("MANUAL_BUY_KILL_SWITCH", "false").lower() == "true"

# Fill accounting and cost model. Alpaca paper trading has no commission, but
# spread/slippage still affect realized results. Journal/P&L is finalized from
# actual filled quantity and average fill price, not submission estimates.
RECORD_ONLY_FILLED_ORDERS = os.environ.get("RECORD_ONLY_FILLED_ORDERS", "true").lower() == "true"
COMMISSION_PER_SHARE = float(os.environ.get("COMMISSION_PER_SHARE", "0.0"))
ESTIMATED_SLIPPAGE_BPS = float(os.environ.get("ESTIMATED_SLIPPAGE_BPS", "5.0"))

# A position that has not progressed is capital being held without evidence.
# Every position with an open-trade record is eligible, regardless of when it
# entered the account. Ownership history is used for attribution only.
MAX_HOLDING_HOURS = float(os.environ.get("MAX_HOLDING_HOURS", "120"))
STAGNATION_MAX_HOURS = float(os.environ.get("STAGNATION_MAX_HOURS", "24"))
STAGNATION_MIN_PROGRESS_PCT = float(os.environ.get("STAGNATION_MIN_PROGRESS_PCT", "0.5"))
# Limit non-protective stagnation/time exits so a stale-data or bad-cache
# episode cannot liquidate an entire book in one run. Hard stops, negative-news
# exits, and de-leveraging are not subject to this cap.
STAGNATION_MAX_EXITS_PER_RUN = int(os.environ.get("STAGNATION_MAX_EXITS_PER_RUN", "2"))

# Daily/weekly reports are written locally and appended to the normal run log.
ENABLE_PERFORMANCE_REPORTS = os.environ.get("ENABLE_PERFORMANCE_REPORTS", "true").lower() == "true"

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

# --- Sell-side risk parity (minimum reward:risk) ---
# Every trade's take-profit must be at least this many times its stop
# distance, so a wide stop + tight target pair (R:R < 1) can never ship --
# otherwise the winners can't pay for the losers. Applied in
# _compute_exit_levels whenever both levels exist; the target is pushed out
# to this ratio (still capped by MAX_TAKE_PROFIT_DISTANCE_ATR_MULT). Set 0 to
# disable and let each level clamp independently.
MIN_REWARD_RISK_RATIO = float(os.environ.get("MIN_REWARD_RISK_RATIO", 1.5))

# --- Scale-out (partial profit taking) ---
# When a winner reaches this fraction of the way to its take-profit, bank
# SCALE_OUT_FRAC of the position (lock the gains, leave the runner). The
# trailing stop keeps ratcheting on the remainder, and the rest exits at the
# target or on stop. One-shot per position. Set ENABLE_SCALE_OUT=false to
# exit everything at the target as before.
ENABLE_SCALE_OUT = os.environ.get("ENABLE_SCALE_OUT", "true").lower() == "true"
SCALE_OUT_AT_RR_FRAC = float(os.environ.get("SCALE_OUT_AT_RR_FRAC", 0.6))
SCALE_OUT_FRAC = float(os.environ.get("SCALE_OUT_FRAC", 0.33))

# --- Uniform portfolio quality review ---
# The quality review applies to EVERY holding, not just positions that existed
# before deployment. It can bank winners at the configured profit threshold and
# replace weak, non-losing positions; positions already down are not churned
# into weakness. Ownership history is never used to grant a stock more value.
ENABLE_QUALITY_TRIM = os.environ.get("ENABLE_QUALITY_TRIM", "true").lower() == "true"
QUALITY_TRIM_SCORE_THRESHOLD = float(os.environ.get("QUALITY_TRIM_SCORE_THRESHOLD", 55.0))
QUALITY_TRIM_MAX_PER_RUN = int(os.environ.get("QUALITY_TRIM_MAX_PER_RUN", 2))
QUALITY_TRIM_LOSS_GUARD_PCT = float(os.environ.get("QUALITY_TRIM_LOSS_GUARD_PCT", 3.0))
# Profit-take on any winner: a position up QUALITY_TRIM_PROFIT_TAKE_PCT or
# more since its average entry is sold to bank the gain (score ignored).
# Profit-takers are sold before score-failures and share the same cap.
QUALITY_TRIM_PROFIT_TAKE_PCT = float(os.environ.get("QUALITY_TRIM_PROFIT_TAKE_PCT", 5.0))

# --- Momentum pre-filter on the universe scan (concentrate on movers) ---
# Instead of scanning a blind rotating slice of the S&P 500, each run first
# includes today's top movers (reused from the scan cache -- zero extra API
# cost) so Gemini's attention lands on names already showing relative
# strength; the rest of the quota backfills with rotation so the full
# universe is still covered over the day. Set MOMENTUM_PREFILTER=false for
# pure rotation.
MOMENTUM_PREFILTER = os.environ.get("MOMENTUM_PREFILTER", "true").lower() == "true"
MOMENTUM_PREFILTER_MAX = int(os.environ.get("MOMENTUM_PREFILTER_MAX", 5))

# --- Walk-forward learning into live sizing (Phase 3b) ---
# backtest.py --walkforward writes data/setup_gate.json: the indicator-regime
# setups that demonstrably won in the prior train window. When that file
# exists, the live bot computes the SAME setup string from its own indicators
# at entry time and sizes by that setup's demonstrated edge (proven setups
# toward SETUP_MULT_MAX, proven drags toward SETUP_MULT_MIN, unknown = 1.0).
# The live closed-trade journal still takes precedence once it has samples.
WALKFORWARD_LIVE_LEARNING = os.environ.get("WALKFORWARD_LIVE_LEARNING", "true").lower() == "true"
WALKFORWARD_MIN_SAMPLES = int(os.environ.get("WALKFORWARD_MIN_SAMPLES", 8))
WALKFORWARD_PROVEN_MULT = float(os.environ.get("WALKFORWARD_PROVEN_MULT", 1.1))

# --- Overnight queue cap ---
# Overnight runs queue trade ideas every cycle; without a cap the queue grows
# unbounded (40+ ideas) and the morning verification prompt bloats while most
# get skipped anyway at execution (cash/exposure caps). Keep only the top
# MAX_PENDING_TRADES by conviction/confidence when saving; the queue refreshes
# fresh every night, so dropping the tail costs nothing. Set 0 for no cap.
MAX_PENDING_TRADES = int(os.environ.get("MAX_PENDING_TRADES", 12))
# A queued idea is a snapshot, not a permanent order. Expire it if it has
# remained unverified too long or has been handed back for verification too
# many times; the next overnight scan can create a fresh idea instead.
PENDING_TRADE_MAX_AGE_HOURS = float(os.environ.get("PENDING_TRADE_MAX_AGE_HOURS", 24.0))
PENDING_TRADE_MAX_ATTEMPTS = int(os.environ.get("PENDING_TRADE_MAX_ATTEMPTS", 6))
# Do not spend a Gemini verification call every cron cycle when an extended-
# hours quote is known to be stale. The idea remains queued and is retried
# after this cooldown or as soon as a fresh quote is available.
STALE_QUEUE_RETRY_COOLDOWN_MINUTES = float(os.environ.get("STALE_QUEUE_RETRY_COOLDOWN_MINUTES", 30.0))

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
# Chase filters are SOFT: an extended name is still tradeable, but its
# position is scaled down as the extension grows (60% up to 2x the limit,
# 35% up to 3x, 15% beyond) instead of being skipped -- momentum IS the
# trade, it just earns a smaller size. Only past CHASE_HARD_SKIP_MULT x the
# limit (a truly priced-in move) is the buy refused outright. Set to 0 to
# remove the hard skip entirely (everything stays tradeable, just smaller).
CHASE_HARD_SKIP_MULT = float(os.environ.get("CHASE_HARD_SKIP_MULT", 5.0))
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

# --- Sector and correlation concentration caps (loss protection) ---
# The bot never holds more than this fraction of portfolio equity in one GICS
# sector. A second cap catches different tickers moving as the same factor:
# candidate/holding pairs with rolling daily-return correlation above the
# threshold share one exposure bucket. Missing history never blocks a trade;
# it simply leaves the correlation cap unapplied for that candidate.
MAX_SECTOR_EXPOSURE_PCT = float(os.environ.get("MAX_SECTOR_EXPOSURE_PCT", 0.25))
ENABLE_CORRELATION_CAP = os.environ.get("ENABLE_CORRELATION_CAP", "true").lower() == "true"
CORRELATION_THRESHOLD = float(os.environ.get("CORRELATION_THRESHOLD", 0.75))
MAX_CORRELATED_EXPOSURE_PCT = float(os.environ.get("MAX_CORRELATED_EXPOSURE_PCT", 0.35))
CORRELATION_LOOKBACK_DAYS = int(os.environ.get("CORRELATION_LOOKBACK_DAYS", 60))
CORRELATION_MAX_HOLDINGS_CHECKED = int(os.environ.get("CORRELATION_MAX_HOLDINGS_CHECKED", 12))

# --- Turnover / friction budget ---
# Limit submitted buy and non-protective-sell notional per Eastern day. This
# prevents a noisy signal from repeatedly paying spread/slippage. Protective
# exits bypass the cap. 0 disables the budget.
MAX_DAILY_TURNOVER_PCT = float(os.environ.get("MAX_DAILY_TURNOVER_PCT", 0.50))
MAX_DAILY_TURNOVER_DOLLARS = float(os.environ.get("MAX_DAILY_TURNOVER_DOLLARS", 0.0))
TURNOVER_PROTECTIVE_SELLS_BYPASS = os.environ.get("TURNOVER_PROTECTIVE_SELLS_BYPASS", "true").lower() == "true"

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
# Multi-timeframe context is collected only for the top candidates after the
# cheap daily/5-minute screen, keeping the run bounded while giving Gemini
# 1-minute, 5-minute, and 1-hour trend context.
ENABLE_MULTI_TIMEFRAME = os.environ.get("ENABLE_MULTI_TIMEFRAME", "true").lower() == "true"
MULTI_TIMEFRAME_MAX_TICKERS = int(os.environ.get("MULTI_TIMEFRAME_MAX_TICKERS", 6))

# --- Cooldown & dedup ---
TRADE_COOLDOWN_MINUTES = 30
NEWS_DEDUP_MAX_AGE_HOURS = 48

# --- Engine-quality gate ---
# Do not let a fallback engine with a demonstrated negative edge keep trading
# indefinitely. The gate is inactive until enough clean, engine-attributed
# closed trades exist; historical unattributed rows do not count.
ENGINE_QUALITY_GATE_ENABLED = os.environ.get("ENGINE_QUALITY_GATE_ENABLED", "true").lower() == "true"
ENGINE_QUALITY_GATE_MIN_SAMPLES = int(os.environ.get("ENGINE_QUALITY_GATE_MIN_SAMPLES", 30))
ENGINE_QUALITY_GATE_MIN_WIN_RATE_PCT = float(os.environ.get("ENGINE_QUALITY_GATE_MIN_WIN_RATE_PCT", 45.0))
ENGINE_QUALITY_GATE_MAX_AVG_PNL_PCT = float(os.environ.get("ENGINE_QUALITY_GATE_MAX_AVG_PNL_PCT", -0.25))

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

# Daily Gemini budget pacing: cap total Gemini ATTEMPTS per day and spread
# them across the day's runs instead of front-loading them. The bot runs
# around the clock, so without pacing it can burn the free tier's daily
# quota by midday (and a slow-Google night with model-rotation timeouts
# inflates the counter even faster). At any run you may call only the
# budget's share of the day's runs elapsed so far -- so calls stay available
# all day and the quota never dies early.
#
# 24/7 operation: with a 2-minute cadence (~720 runs/day) and a 1000-call
# budget, EVERY run gets its Gemini call (720 used < 1000 budget) and the
# quota never runs out mid-day -- the pacing only steps in when a run burns
# multiple attempts (timeouts / model rotation) so a slow-Google period can
# never stack up and empty the day early. The morning queue re-verification
# is exempted from pacing (decide._should_attempt_call allow_despite_pacing)
# so the open is always Gemini-verified. Set to 0 to disable pacing and let
# Google's own limit stop the day.
GEMINI_DAILY_BUDGET = int(os.environ.get("GEMINI_DAILY_BUDGET", 1000))
# Expected runs per day, used to compute the per-run share of the daily
# budget (every-2-min cadence = 720). The pacing adapts automatically: the
# more runs that have elapsed, the larger the share of the budget you may
# have spent -- so a burst of retries thins out the following runs.
GEMINI_RUNS_PER_DAY = int(os.environ.get("GEMINI_RUNS_PER_DAY", 720))

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

# Entry/exit alignment: when daily data is available, new long entries must be
# above SMA-20 because the MA-breakdown exit treats a close below SMA-20 as a
# failure. This removes the buy-then-immediate-MA-sell whipsaw path.
BUY_REQUIRE_SMA20_ALIGNMENT = os.environ.get("BUY_REQUIRE_SMA20_ALIGNMENT", "true").lower() == "true"
MA_BREAKDOWN_REQUIRE_DOWNTREND = os.environ.get("MA_BREAKDOWN_REQUIRE_DOWNTREND", "true").lower() == "true"

# Gemini-unavailable fallback is intentionally stricter than the normal
# candidate screen. A 55/100 technical score is a research candidate, not a
# sufficiently strong fallback trade. Fallback entries also use a smaller
# per-position cap than Gemini-reviewed trades.
TECHNICAL_FALLBACK_MIN_SCORE = float(os.environ.get("TECHNICAL_FALLBACK_MIN_SCORE", 65.0))
TECHNICAL_FALLBACK_REQUIRE_UPTREND = os.environ.get("TECHNICAL_FALLBACK_REQUIRE_UPTREND", "true").lower() == "true"
TECHNICAL_FALLBACK_MAX_POSITION_PCT = float(os.environ.get("TECHNICAL_FALLBACK_MAX_POSITION_PCT", 0.05))
# A fallback run is a degraded mode, not permission to submit dozens of
# low-information orders. Keep only the strongest few ideas so stale-data
# checks and protective reporting still finish inside the run budget.
TECHNICAL_FALLBACK_MAX_TRADES_PER_RUN = int(os.environ.get("TECHNICAL_FALLBACK_MAX_TRADES_PER_RUN", 8))

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
# A single ambiguous headline should not liquidate a position. The news cache
# records the number of negative articles in the current fetch; a very severe
# sentiment still exits on its own as an emergency override.
NEGATIVE_NEWS_MIN_ARTICLES = int(os.environ.get("NEGATIVE_NEWS_MIN_ARTICLES", 2))
NEGATIVE_NEWS_EMERGENCY_THRESHOLD = float(os.environ.get("NEGATIVE_NEWS_EMERGENCY_THRESHOLD", -0.8))

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
