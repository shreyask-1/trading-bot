# News-Driven Paper Trading Bot (Real Alpaca Paper Account)

Trades a fixed watchlist plus news-driven candidates from the S&P 500,
through a real Alpaca **paper trading** account — real broker
infrastructure and order execution mechanics, fake money. Runs on a
schedule via GitHub Actions. Free the whole way through.

**This is an experiment, not a proven strategy.** Nothing here has a
demonstrated edge over just holding an index fund. See
[Honest limitations](#honest-limitations) before you read too much into
any of it.

---

## How it works

Every run follows this sequence (see `main.py`):

```text
  0. Equity circuit breakers (trader.evaluate_circuit_breakers)
       Tracks a running equity peak and the day's starting equity. Halts all
       new buys for the day on a daily loss >= DAILY_LOSS_HALT_PCT, cuts new
       position sizing to DELEVERAGE_SIZE_MULTIPLIER at drawdowns >=
       MAX_DRAWDOWN_DELEVERAGE_PCT, and flattens everything at drawdowns >=
       MAX_DRAWDOWN_FLATTEN_PCT. Peak/drawdown state persists in
       logs/risk_state.json and is logged every run.
               │
               ▼
  1. Hard risk management (trader.check_atr_stop_take_profit)
       Every existing holding is checked against an ATR-based stop-loss and
       take-profit level, plus a hard per-position loss cap
       (MAX_POSITION_LOSS_PCT) that fires even if indicator data is
       unavailable. Runs BEFORE anything else and is completely independent
       of what the LLM decides that run, and independent of market regime --
       an exit is always allowed.
               │
               ▼
  1b. Consolidation + de-leveraging
       Holdings above MAX_OPEN_POSITIONS are purged, then, if cash is below
       DELEVERAGE_TARGET_CASH_PCT (e.g. negative), the weakest-scored
       holdings are sold until cash is restored. This heals margin states
       automatically instead of leaving the account negative for days.
       If the flatten circuit breaker fired, every position is sold.
               │
               ▼
  2. Market-hours gate
       NEW trades (news -> decisions -> execution) only run while the market
       is open (TRADE_ONLY_DURING_MARKET_HOURS) and the day isn't halted.
       This prevents DAY orders from queuing overnight and all filling at the
       next 9:30 AM ET open -- the bug that blew cash from +$33k to -$19k
       on 2026-08-07. Risk management and de-leveraging still run anytime.
               │
               ▼
  3. Market regime check (market_regime.py, using SPY as a proxy)
       Classifies the broad market as BULLISH / NEUTRAL / BEARISH /
       HIGH_VOLATILITY based on SPY's trend (20 vs 50 SMA) and 20-day
       realized volatility. Produces a position-size multiplier that is
       enforced IN CODE on every subsequent buy order (trader.execute_trade),
       not just suggested in the prompt. BEARISH sets the multiplier to 0.0,
       which blocks all new buys outright; sells are never restricted by
       this, so the bot can still exit or trim in any regime.
               │
               ▼
  4. News fetch (news.py)
       Pulls recent headlines from Finnhub, matches them against the S&P
       500 ticker/company list (sp500_data.py), and skips any article
       already reacted to in a recent prior run (deduped for
       config.NEWS_DEDUP_MAX_AGE_HOURS).
               │
               ▼
  5. Quantitative pre-screen (signal_score.py)
       Every news-driven candidate and every watchlist ticker not already
       held is scored 0-100 using ONLY deterministic technical indicators
       (trend, ADX, RSI, relative volume, MACD histogram) -- no LLM
       involved. Candidates scoring below config.MIN_SIGNAL_SCORE_TO_CONSIDER
       are filtered out and never reach the LLM prompt at all. This filter
       does NOT apply to existing holdings -- a bad score on something
       already owned is a reason to consider exiting, not a reason to hide
       it from review.
               │
               ▼
  6. LLM review (decide.py, via Gemini)
       Everything that survived the pre-screen, plus all current holdings
       (unfiltered) and full technical indicators for each, is assembled
       into a prompt. Gemini returns a conviction score (1-10) per trade
       idea; ideas below config.MIN_CONVICTION_TO_TRADE are discarded. The
       prompt explicitly requires cash-only, no-margin suggestions, and the
       code enforces it regardless of what the model outputs.
               │
               ▼
  7. Execution (trader.execute_trade)
       Each surviving trade is sized by conviction (as a fraction of
       config.MAX_POSITION_PCT) AND by the regime/circuit-breaker multiplier,
       then submitted to Alpaca as a market order. Buys are rejected if cash
       is negative (hard no-margin rule), if pending open orders already
       consume the cash (pending-order-aware sizing), or if gross exposure
       (holdings + pending buys) would exceed MAX_TOTAL_EXPOSURE_PCT.
       Account state is re-fetched after each fill. Tickers with an open
       order or on cooldown (config.TRADE_COOLDOWN_MINUTES) are skipped.
               │
               ▼
  8. Logging (trader.record_performance_snapshot)
       Appends one row to logs/performance.csv per run: portfolio value,
       cash, regime, size multiplier, pre-screen stats, and trade outcome
       counts. Risk state (peak, drawdown, daily P/L, halted) is logged every
       run and persisted to logs/risk_state.json. Full run details are also
       written to logs/YYYY-MM-DD.log.
```

## Risk controls (the "never lose 12k overnight again" layer)

Everything below is enforced in code, independent of what Gemini says:

- **Market-hours gating** (`TRADE_ONLY_DURING_MARKET_HOURS`): no new trades
  while the market is closed. Orders can no longer queue overnight and all
  fill at the open.
- **No margin, ever** (`execute_trade`): buys are rejected when cash is
  negative, when open buy orders already consume the available cash, or when
  total exposure would exceed `MAX_TOTAL_EXPOSURE_PCT` (default 90%).
- **Automatic de-leveraging** (`enforce_deleveraging`): if cash is below
  target, the weakest holdings are sold until cash is restored.
- **The bot never stops trading for the day** (as requested):
  `DAILY_LOSS_HALT_PCT` and `MAX_DRAWDOWN_FLATTEN_PCT` default to `0` =
  disabled. The bot buys, sells, and trades all day regardless of how the
  day is going. Both are still available as opt-in guards -- set a positive
  value (e.g. `DAILY_LOSS_HALT_PCT=3.0`) to re-enable. Note the 2026-08-11
  loss was NOT a bad trading day; it was margin from overnight order
  stacking, which the market-hours gate, no-margin rule, and de-leveraging
  below prevent regardless of the breakers.
- **Drawdown sizing cut** (`MAX_DRAWDOWN_DELEVERAGE_PCT`, default 5%): while
  drawdown from peak exceeds 5%, new buys are sized at 25%. This only
  shrinks new entries -- it does not stop trading. Set to `0` to disable.
- **Hard per-position loss cap** (`MAX_POSITION_LOSS_PCT`, default 8%):
  force-sells any position down 8% from entry, even if indicator data is
  unavailable.
- **Alerting**: set `DISCORD_WEBHOOK_URL` as a GitHub Actions secret and the
  bot pings you on margin warnings, halts, de-leveraging, and flattens
  instead of you finding out by checking the dashboard. Without it, alerts
  still appear in the run logs.

All knobs live at the top of `config.py` and can be overridden with
environment variables (set them as repo secrets / Actions env).

## Daytrading mode (dual focus: news + charts) & per-trade exits

- **Dual focus** (`DAYTRADE_MODE`, default on): every news headline is
  scored -1..+1 with a deterministic lexicon (`news.headline_sentiment`),
  and that sentiment is folded into the quant signal score
  (`NEWS_SENTIMENT_WEIGHT`) so a real catalyst matters alongside the chart.
  Gemini now also sees intraday momentum vs session open, VWAP deviation,
  and the opening-range status (above/below/inside the first 15 minutes)
  for every candidate.
- **Opening-range breakout** (`get_opening_range_breakout`): price breaking
  above the first `OPENING_RANGE_BARS` 5-minute bars is scored as a bullish
  breakout and boosts the signal; breaking below is penalized. The technical
  fallback engine adds conviction for breakouts.
- **Entry window discipline**: no new buys in the first
  `TRADE_START_MINUTES_AFTER_OPEN` minutes (auction chop) and none after
  `STOP_NEW_BUYS_AFTER` ET. Sells are never restricted.
- **End-of-day flatten** (`END_OF_DAY_FLATTEN`, default on): everything is
  sold back to cash at `END_OF_DAY_FLATTEN_TIME` (15:50 ET) so no position
  survives overnight -- the 2026-08-11 liquidation hit an overnight position
  at 3:30 AM ET.
- **Per-trade stop-loss / take-profit** (`_record_custom_exit`): every buy
  gets a stop and target computed from THAT trade's setup -- recent swing
  high/low clamped to a sane multiple of ATR, or Gemini's explicit
  `stop_loss`/`take_profit` values when it provides them (all configurable
  via `ALLOW_GEMINI_CUSTOM_EXITS` and the ATR clamp constants). Saved to
  `logs/custom_exits.json` and enforced every run by
  `check_atr_stop_take_profit`, with the hard 8% loss cap as backstop.

## Profit levers (implemented)

- **Chase filters** (`MAX_BUY_EXTENSION_ABOVE_VWAP_PCT`, default 2.5%, and
  `MAX_INTRADAY_MOVE_PCT`, default 4%): the bot refuses to buy a name already
  extended above VWAP or already up big on the session. Chasing is the #1
  way daytraders give back gains. Set either to `0` to disable.
- **Trailing stop** (`TRAILING_STOP_ACTIVATE_MULT`, default 1.5x ATR, and
  `TRAILING_STOP_DISTANCE_MULT`, default 2x ATR): once a position is up, the
  stop ratchets up to (best price - 2 ATR) and only ever moves up, so
  winners are banked instead of given back. Persisted in
  `logs/custom_exits.json`.
- **Risk-based sizing** (`MAX_RISK_PER_TRADE_PCT`, default 0.75%): each
  buy is sized so a stop-out costs at most 0.75% of equity, computed from
  the trade's actual stop distance (tight stop = bigger size, wide stop =
  smaller). Uniform per-trade pain is what lets compounding work. Set `0`
  to disable.
- **Time-of-day sizing** (`TIME_OF_DAY_MULTIPLIERS`): new buys trade full
  size in the open power hour (9:30-11:00) and closing push (15:00-16:00),
  are cut to 50% through the lunch lull (11:30-13:30 ET), and 70-80% in
  between. Sells are never affected.
- **News + technical confluence** (`NEWS_CONFLUENCE_MIN_TECH_SCORE`, default
  50): a news-driven candidate only reaches Gemini if its PURE technical
  score (before the sentiment boost) clears the bar -- headline alone is not
  a setup, the chart must agree. Set `0` to disable.
- **Trade journal** (`logs/trades_journal.csv` + `logs/trade_results.csv`):
  every fill is recorded with its confidence and exit reason, buys are paired
  with sells, and each run prints a win-rate summary by setup type (news /
  breakout / technical / other) so you can measure what actually works.
- **Better news filtering** (`NEWS_MIN_SCORE_TO_CONSIDER`, default 5): every
  article is scored 0-10; only the important ones (earnings beats,
  partnerships, upgrades) reach Gemini. Interviews and filler don't.
- **Intraday indicator fix**: intraday momentum / VWAP were measured against
  a 2-day-old anchor (a bug); they now measure today's session open and
  today's session VWAP, so the filters and the Gemini prompt see real
  intraday numbers.
- **Run cadence (free, do this)**: daytrading signals are 5-minute
  phenomena. The Actions schedule below is an hourly fallback; for real
  daytrading cadence, point cron-job.org (or similar) at your
  `workflow_dispatch` endpoint every 2-5 minutes during market hours
  (9:30-16:00 ET, weekdays). Concurrency guard in the workflow prevents
  overlapping runs.

## Morning prep (no more idle nights)

Instead of doing nothing overnight, `morning_prep.py` (scheduled ~1-2h before
the open in `morning-prep.yml`) gathers everything for the next day and makes
**exactly one Gemini call** to decide what matters:

- **Scored news** — every article is rated 0-10 by importance
  (`news.score_article`): earnings beats, partnerships, FDA/approvals and
  analyst upgrades score high; interviews and store openings score low. Only
  high-scoring news survives into the briefing.
- **Chart setups** across the S&P 500 — top news names plus a rotating slice
  of the universe, with the full indicator set below.
- **Market regime** — SPY + QQQ trend/volatility plus a best-effort CBOE
  VIX check (Alpaca stock feeds don't carry VIX, so SPY/QQQ realized
  volatility is the elevated-vol signal); the bot turns defensive when the tape is
  ugly.
- **Earnings calendar** — the next ~3 weeks (Finnhub) so `days_until_earnings`
  is available per ticker all day.

Output: `logs/morning_brief_YYYY-MM-DD.md` (readable) and
`data/morning_candidates.json` (machine-readable), which the trading loop
consumes at the open so the first trades act on the prepared context.

## Full S&P 500 universe

`WATCHLIST` is now every S&P 500 ticker. Each run technically scans
`UNIVERSE_SCAN_PER_RUN` (default 60) non-news names on a deterministic
hourly rotation, so the whole index gets scanned every day without hammering
the data API in one shot. News-matched tickers are always scanned regardless
of the slice.

## What Gemini sees & how size is set

- **Rich indicator context** per candidate: trend, RSI, ATR, ADX, Stochastic,
  MACD crossover direction, support/resistance (10-day swings), gap %, % off
  the 52-week high/low, relative volume, days until earnings, VWAP, and
  opening-range status.
- **Confidence-based sizing** — Gemini returns a `confidence` 0-100 and the
  code converts it: 90+ → 8% of equity, 80+ → 5%, 70+ → 3%, 60+ → 2%, below
  60 → skipped. Raw dollar amounts are ignored when confidence is present.

## Smarter exits (in addition to per-trade stops)

- **Trailing stop** — ratchets up once the position is profitable.
- **Moving-average breakdown** — exit a long that closes below SMA-20
  (`ENABLE_MA_BREAKDOWN_EXIT`).
- **RSI exhaustion** — exit a long with RSI-14 above 75 (selling into
  strength, `ENABLE_RSI_EXHAUSTION_EXIT`).
- **Negative news** — exit when the last news fetch shows strong negative
  sentiment for the ticker (`ENABLE_NEGATIVE_NEWS_EXIT`).

## Better logging

Every run now logs: portfolio value, total return, max drawdown, daily
Sharpe ratio, closed-trade win rate, average winner/loser, open positions,
and each trade's reason, confidence, and trigger — via
`trader.summarize_performance` + the trade journal
(`logs/trades_journal.csv`, `logs/trade_results.csv`).

## Phase 2: data feeds (economic calendar, analyst, insider, SEC, Reddit)

`data_feeds.py` adds the fundamental layer a pro daytrader watches, on top
of price + news. Everything is TTL-cached under `logs/` and **fail-soft** —
a missing key, a network error, or a blocked IP (Reddit blocks datacenter
IPs, including GitHub Actions) never breaks a run; the bot just trades
without that feed.

- **Economic calendar** (Finnhub `/calendar/economic`) — CPI, FOMC, NFP,
  GDP, PCE and other high-impact events for the next 14 days. On a day with
  an upcoming high-impact event, new buys are sized down to
  `HIGH_IMPACT_EVENT_SIZE_MULT` (0.5x) and the event is surfaced to Gemini.
  **Note:** Finnhub's economic-calendar endpoint requires a paid plan (free
  keys get HTTP 403). When the live feed is unavailable, `data_feeds.py`
  automatically falls back to a built-in table of the official 2026
  FOMC/CPI/NFP release dates, so defensive sizing still works on a free key.
  A paid Finnhub key makes live data take over automatically.
- **Analyst upgrades / downgrades** (Finnhub `/stock/upgrade-downgrade`) —
  the last 7 days of actions; an upgrade adds `ANALYST_UPGRADE_BOOST` to the
  quant score, a downgrade subtracts `ANALYST_DOWNGRADE_PENALTY`.
- **Insider activity** (Finnhub `/stock/insider-transactions`) — net insider
  buys/sells per candidate (cached daily, limited to
  `MAX_FUNDAMENTAL_TICKERS`); insider buying adds `INSIDER_BUY_BOOST`.
- **SEC filings** (Finnhub `/stock/filings`) — recent 8-K / 10-Q / 10-K /
  Form-4 per candidate; a fresh 8-K adds a small confirmation boost.
- **Reddit sentiment** (best-effort public JSON from r/wallstreetbets,
  r/stocks, r/investing) — crowd sentiment feeds the score via
  `REDDIT_SENTIMENT_WEIGHT` and is surfaced to Gemini. May be empty on
  hosted runners.

Each feed is gated by an `ENABLE_*` config flag (all default on). The
scoring boosts live in `signal_score.calculate_signal_score` and are all
config-tunable (0 disables each).

## Phase 3: self-learning statistics (weight toward what works)

Every trade is already journaled (`logs/trades_journal.csv`) with its
reasoning, confidence, stop, and outcome, and buys pair with sells into
`logs/trade_results.csv`. Phase 3 closes the loop:

- `trader._setup_stats()` buckets closed trades by setup category (news /
  breakout / earnings / technical / other).
- `trader.get_setup_multiplier(setup)` converts a setup's *demonstrated*
  win rate + average return into a sizing multiplier clamped to
  `SETUP_MULT_MIN`..`SETUP_MULT_MAX` (0.5x..1.5x by default). Winning setups
  get sized up; losing setups get sized down; setups with fewer than
  `SELF_LEARNING_MIN_SAMPLES` (5) closed trades get exactly 1.0x — no
  opinion until there's data. Applied to every new buy, never to sells.
- `trader.build_performance_brief()` writes a "what actually WORKS" block
  into the Gemini prompt, so the LLM favors setups with proven edge and
  avoids the ones it keeps losing on.
- The multiplier and per-setup stats are logged every run
  (`Self-learning: news: 12 trades, 67% win rate, avg +1.2% (WORKING) | ...`).

Disable with `SELF_LEARNING_ENABLED=false`.

## Second-trader detection

Every order this bot submits is recorded in `logs/bot_order_ledger.json`.
Each run, `reconcile_foreign_activity` compares the actual Alpaca holdings
against what the ledger says the bot should own. Anything the bot didn't
create -- a second bot, a local cron, or manual trades on the same Alpaca
keys -- is flagged as `FOREIGN ACTIVITY` in the log and sent to your alert
webhook. The first run after deploy records the account's pre-existing
positions as a baseline and lists them once for you to verify. If you did
not trade this account manually, you should never see these flags.

## Honest limitations

- The LLM layer (Gemini) is not backtestable: its outputs are
  non-deterministic and a model queried today may already "know" the past.
  `backtest.py` therefore only simulates the deterministic layer (signal
  scoring, regime filter, ATR stops) and is a sanity check, not a
  projection.
- Even the deterministic edge here is unproven. If the strategy had a real
  edge, it would show up as consistent positive drift over many trades and
  regimes -- verify it in the logs before believing it.
- This bot traded on margin and got force-liquidated by Alpaca's paper
  engine on 2026-08-11 (-$12k). The risk controls above prevent the
  mechanism that caused it, but paper trading is not a rehearsal for live
  trading risk: fees, slippage, liquidity, and real margin calls are not
  faithfully simulated.

## Testing

`python test_risk_controls.py` runs mocked unit tests for the risk layer,
the daytrading helpers, the ledger/reconciliation, the news scorer,
confidence sizing, the smarter exits, Phase 2 feeds, and Phase 3
self-learning (no network calls). 86 checks.
