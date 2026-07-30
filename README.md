# News-Driven Paper Trading Bot (Real Alpaca Paper Account)

Trades the S&P 500 based on real news and technical indicators, through your real Alpaca **paper trading** account — real broker infrastructure, real order execution mechanics, fake money. Free the whole way through.

Now powered by an **institutional multi-stage validation pipeline** featuring Google Gemini as a structured quantitative Veto Agent.

---

## 🏗️ How it works (Multi-Stage Pipeline)

Instead of passing raw news straight into AI, every potential trade passes through a strict 6-stage quantitative filter:

```text
  [ Market Data & News Feeds ]
               │
               ▼
   1. Technical Engine (Normalization & Trend/Volatility)
               │
               ▼
   2. Quantitative Pre-Scoring (signal_score.py)
               │
               ▼
   3. Market Regime Filter (market_regime.py)
               │
               ▼
   4. Catalyst Ranking & Noise Reduction (news_engine.py)
               │
               ▼
   5. Gemini Structured Veto Agent (gemini_validator.py)
               │
               ▼
   6. Dynamic Risk Engine (risk_engine.py)
               │
               ▼
   [ Order Execution via Alpaca ] ──► trade_memory.py & performance.py
