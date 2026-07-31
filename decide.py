"""
Builds the full picture for Gemini every run: existing holdings (with P/L,
full technical indicators, and any related news), news-driven candidates,
and fixed-watchlist tickers being evaluated on technicals alone. Asks for
a conviction score (1-10) on every trade idea, which trader.py uses to
scale position size.
"""

import json
import re
import google.generativeai as genai

from config import (
    GEMINI_API_KEY, GEMINI_MODEL, MAX_POSITION_PCT, MIN_CONVICTION_TO_TRADE,
    WATCHLIST, ATR_STOP_MULTIPLIER, ATR_TAKE_PROFIT_MULTIPLIER,
)
from trader import get_full_indicators, get_tickers_with_open_orders, get_tickers_on_cooldown

genai.configure(api_key=GEMINI_API_KEY)


PROMPT_TEMPLATE = """You are a moderately aggressive trading analyst for a SIMULATED \
paper-trading portfolio (no real money). Every trade idea you propose MUST include a \
"conviction" score from 1-10, reflecting how strongly the signals (news, trend, \
momentum, RSI, MACD, volume) agree with each other. Ideas below conviction {min_conviction} \
will be discarded automatically, so only include ideas you'd actually rate that high.

Current portfolio:
- Cash available: ${cash:,.2f}
- Total portfolio value: ${total_value:,.2f}

Existing holdings (review each: hold, add, trim, or exit):
{holdings_block}

News-driven candidates (mentioned in fresh news, not currently held):
{news_block}

Watchlist candidates (liquid large-caps, evaluated on technicals only -- may have no fresh news):
{watchlist_block}

How to read the indicators:
- RSI (14): <30 oversold, >70 overbought.
- ADX (14): >25 = strong trend (either direction), <20 = weak/no trend -- use this to judge whether
  a momentum or trend-following idea is actually supported.
- MACD: histogram above zero and rising = bullish momentum building; below zero and falling = bearish.
- Bollinger %B: near 1.0 = price near upper band (extended), near 0.0 = near lower band (extended).
- Stochastic %K/%D: >80 overbought, <20 oversold.
- ATR: the stock's typical daily range in price terms -- larger ATR means a given % move happens on
  smaller price swings, useful for judging if a move is significant relative to normal noise.
- Relative volume: how today's volume compares to its recent average -- large positive spikes mean
  a move has real conviction behind it; near 0% means average, uneventful volume.
- Trend: uptrend / downtrend / sideways, from the 20 vs 50 SMA relationship.

Rules:
- Never let any single position exceed {max_pct}% of total portfolio value.
- Only use tickers that appear in the lists above -- do not invent tickers.
- Tickers marked "(unavailable: pending order or cooldown)" must be skipped entirely.
- Prefer setups where multiple signals agree (e.g. news + uptrend + rising volume + MACD bullish)
  over single-signal ideas -- these deserve higher conviction scores.
- It's completely fine to return zero trades if nothing meets the bar.
- Hard ATR-based stop-loss ({stop_mult}x ATR below entry) and take-profit ({tp_mult}x ATR above entry)
  are already enforced separately in code -- focus your reasoning on the setup itself, not exit levels.

Respond with ONLY valid JSON (no markdown fences, no commentary):
{{
  "trades": [
    {{"ticker": "AAPL", "action": "buy", "dollar_amount": 5000, "conviction": 8, "reasoning": "short reason"}}
  ]
}}
"""


def _indicators_str(data):
    if data is None:
        return "no data available"
    macd = data["macd"]
    bb = data["bollinger"]
    stoch = data["stochastic"]
    parts = [
        f"price ${data['price']}",
        f"trend {data['trend'] or 'unknown'}",
        f"RSI {data['rsi_14']}" if data['rsi_14'] is not None else "RSI unknown",
        f"ADX {data['adx_14']}" if data['adx_14'] is not None else "ADX unknown",
        f"ATR {data['atr_14']}" if data['atr_14'] is not None else "ATR unknown",
        f"MACD hist {macd['histogram']}" if macd else "MACD unknown",
        f"BB %B {bb['percent_b']}" if bb else "BB unknown",
        f"Stoch %K {stoch['percent_k']}" if stoch else "Stoch unknown",
        f"momentum10d {data['momentum_10d']}%" if data['momentum_10d'] is not None else "momentum unknown",
        f"rel.volume {data['relative_volume_pct']:+.1f}%" if data['relative_volume_pct'] is not None else "rel.volume unknown",
        f"vol trend {data['volume_trend']}" if data['volume_trend'] else "vol trend unknown",
    ]
    return ", ".join(parts)


def build_holdings_block(holdings, candidates, unavailable):
    if not holdings:
        return "(no current holdings)"
    lines = []
    for ticker, pos in holdings.items():
        flag = " (unavailable: pending order or cooldown)" if ticker in unavailable else ""
        news = ""
        if ticker in candidates:
            news = " | news: " + "; ".join(a["headline"] for a in candidates[ticker][:2])
        data = get_full_indicators(ticker)
        lines.append(
            f"- {ticker}: {pos['qty']} shares, unrealized P/L {pos['unrealized_plpc']:+.2f}%, "
            f"{_indicators_str(data)}{news}{flag}"
        )
    return "\n".join(lines)


def build_news_block(candidates, unavailable):
    if not candidates:
        return "(no new candidates from news this run)"
    lines = []
    for ticker, articles in candidates.items():
        flag = " (unavailable: pending order or cooldown)" if ticker in unavailable else ""
        headlines = "; ".join(a["headline"] for a in articles[:3])
        data = get_full_indicators(ticker)
        lines.append(f"- {ticker}: {_indicators_str(data)} | news: {headlines}{flag}")
    return "\n".join(lines)


def build_watchlist_block(holdings, candidates, unavailable):
    lines = []
    for ticker in WATCHLIST:
        if ticker in holdings or ticker in candidates:
            continue  # already covered above, don't repeat
        flag = " (unavailable: pending order or cooldown)" if ticker in unavailable else ""
        data = get_full_indicators(ticker)
        lines.append(f"- {ticker}: {_indicators_str(data)} | no fresh news{flag}")
    return "\n".join(lines) if lines else "(all watchlist tickers already covered above)"


def get_trade_decisions(candidates, account_snapshot):
    holdings = account_snapshot.get("holdings", {})
    cash = account_snapshot.get("cash", 0)
    total_value = account_snapshot.get("total_value", cash)

    open_orders = get_tickers_with_open_orders()
    cooldowns = get_tickers_on_cooldown()
    unavailable = open_orders | cooldowns

    new_candidates = {t: a for t, a in candidates.items() if t not in holdings}

    prompt = PROMPT_TEMPLATE.format(
        cash=cash,
        total_value=total_value,
        max_pct=int(MAX_POSITION_PCT * 100),
        min_conviction=MIN_CONVICTION_TO_TRADE,
        stop_mult=ATR_STOP_MULTIPLIER,
        tp_mult=ATR_TAKE_PROFIT_MULTIPLIER,
        holdings_block=build_holdings_block(holdings, candidates, unavailable),
        news_block=build_news_block(new_candidates, unavailable),
        watchlist_block=build_watchlist_block(holdings, new_candidates, unavailable),
    )

    model = genai.GenerativeModel(GEMINI_MODEL)
    response = model.generate_content(prompt)
    raw_text = re.sub(r"^```json\s*|\s*```$", "", response.text.strip())

    try:
        trades = json.loads(raw_text).get("trades", [])
    except json.JSONDecodeError:
        print("Warning: could not parse Gemini response as JSON:")
        print(raw_text)
        return []

    # Enforce rules in code too, not just via prompt instructions
    filtered = []
    for t in trades:
        if t.get("ticker") in unavailable:
            continue
        if t.get("conviction", 0) < MIN_CONVICTION_TO_TRADE:
            continue
        filtered.append(t)
    return filtered
