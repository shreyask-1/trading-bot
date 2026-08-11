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
