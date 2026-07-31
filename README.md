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
  0. Market-hours check
       Skip the entire run if the market is closed (config.MARKET_HOURS_ONLY).
       Prevents market orders from queuing overnight and filling on stale
       decisions at the next open.
               │
               ▼
  1. Hard risk management (trader.check_atr_stop_take_profit)
       Every existing holding is checked against an ATR-based stop-loss and
       take-profit level. This runs BEFORE anything else and is completely
       independent of what the LLM decides that run, and independent of
       market regime -- an exit is always allowed.
               │
               ▼
  2. Market regime check (market_regime.py, using SPY as a proxy)
       Classifies the broad market as BULLISH / NEUTRAL / BEARISH /
       HIGH_VOLATILITY based on SPY's trend (20 vs 50 SMA) and 20-day
       realized volatility. Produces a position-size multiplier that is
       enforced IN CODE on every subsequent buy order (trader.execute_trade),
       not just suggested in the prompt. BEARISH sets the multiplier to 0.0,
       which blocks all new buys outright; sells are never restricted by
       this, so the bot can still exit or trim in any regime.
               │
               ▼
  3. News fetch (news.py)
       Pulls recent headlines from Finnhub, matches them against the S&P
       500 ticker/company list (sp500_data.py), and skips any article
       already reacted to in a recent prior run (deduped for
       config.NEWS_DEDUP_MAX_AGE_HOURS).
               │
               ▼
  4. Quantitative pre-screen (signal_score.py)
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
  5. LLM review (decide.py, via Gemini)
       Everything that survived the pre-screen, plus all current holdings
       (unfiltered) and full technical indicators for each, is assembled
       into a prompt. Gemini returns a conviction score (1-10) per trade
       idea; ideas below config.MIN_CONVICTION_TO_TRADE are discarded.
       The prompt also states the current market regime and its sizing
       consequence, but that consequence is enforced by code regardless of
       what the model outputs -- the LLM cannot override it.
               │
               ▼
  6. Execution (trader.execute_trade)
       Each surviving trade is sized by conviction (as a fraction of
       config.MAX_POSITION_PCT) AND by the regime multiplier from step 2,
       then submitted to Alpaca as a market order. Account state is
       re-fetched after each fill so a run with multiple trades doesn't
       size every trade against the same stale cash figure. Tickers with
       an open order or on cooldown (config.TRADE_COOLDOWN_MINUTES) are
       skipped.
               │
               ▼
  7. Logging (trader.record_performance_snapshot)
       Appends one row to logs/performance.csv per run: portfolio value,
       cash, regime, size multiplier, pre-screen stats, and trade outcome
       counts. Full run details are also written to logs/YYYY-MM-DD.log.
