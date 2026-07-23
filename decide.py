"""
Sends news candidates + watchlist technical candidates + portfolio state
to Gemini and asks for structured trade decisions, each tagged with an
explicit short-term or long-term intent. Candidates with no news are
still evaluated purely on technical indicators.
"""

import json
import re
from google import genai
from config import GEMINI_API_KEY, GEMINI_MODEL, MAX_POSITION_PCT, PRICE_HISTORY_DAYS
from trader import get_indicator_snapshot, get_tickers_with_open_orders, get_recently_traded_tickers

client = genai.Client(api_key=GEMINI_API_KEY)


PROMPT_TEMPLATE = """You are a disciplined trading analyst for a SIMULATED paper-trading \
portfolio (no real money). Use these well-established technical concepts to inform your \
reasoning -- they are heuristics used broadly in technical analysis, not guarantees:
- Trend: price above both SMA20 and SMA50 = uptrend; below both = downtrend
- RSI above 70 = potentially overbought; below 30 = potentially oversold
- MACD histogram turning positive = bullish momentum shift; turning negative = bearish
- Bollinger %B near 1.0 = price near upper band (possible overextension); near 0.0 = near lower band
- Volume trend above baseline = stronger conviction behind a price move

Some candidates below have real news attached; others have NO recent news and are being \
evaluated purely on technical setup from a fixed watchlist scan. Both are valid reasons to trade.

You have two jobs each run:
1. Review EXISTING holdings and decide hold, add, trim, or fully exit.
2. Consider NEW candidate tickers (news-driven or technical-only) for potential new positions.

For every trade, classify it as "short" (days, momentum/news-driven) or "long" \
(weeks+, trend-driven) intent -- pick whichever horizon actually fits your reasoning.

Current portfolio:
- Cash available: ${cash:,.2f}
- Total portfolio value: ${total_value:,.2f}

Existing holdings:
{holdings_block}

New candidate tickers (news and/or technical watchlist):
{news_block}

Rules:
- Never let any single position exceed {max_pct}% of total portfolio value.
- Only use tickers listed above -- do not invent tickers.
- Tickers marked "(order pending)" or "(cooldown active)" must be skipped entirely for NEW buys.
  Cooldown tickers CAN still be sold if you have a genuine reason to exit.
- Prefer diversification over concentration. Moderate conviction trades only.
- It's fine to recommend zero trades if nothing meets the bar -- a technical-only setup should
  meet a HIGHER bar than a news-confirmed one, since there's no external catalyst.
- Stop-loss/take-profit and position caps are enforced separately in code -- focus your
  reasoning on trend/momentum/news, not raw P/L.

Respond with ONLY valid JSON, no markdown fences:
{{
  "trades": [
    {{"ticker": "AAPL", "action": "buy", "dollar_amount": 5000, "time_horizon": "short", "reasoning": "short reason"}}
  ]
}}
If no trades: {{"trades": []}}
"""


def _format_indicators(snap):
    if snap is None:
        return "indicators unavailable"
    parts = [f"price ${snap['price']:.2f}"]
    if snap["momentum_pct"] is not None:
        parts.append(f"{PRICE_HISTORY_DAYS}d momentum {snap['momentum_pct']:+.2f}%")
    if snap["rsi"] is not None:
        parts.append(f"RSI {snap['rsi']}")
    parts.append(f"trend: {snap['trend']}")
    if snap["volume_trend_pct"] is not None:
        parts.append(f"volume {snap['volume_trend_pct']:+.2f}% vs avg")
    if snap.get("macd"):
        m = snap["macd"]
        parts.append(f"MACD hist {m['histogram']:+.3f}")
    if snap.get("bollinger"):
        b = snap["bollinger"]
        parts.append(f"Bollinger %B {b['percent_b']:.2f}")
    return ", ".join(parts)


def build_news_block(candidates, recently_traded):
    lines = []
    for ticker, articles in candidates.items():
        snap = get_indicator_snapshot(ticker)
        cooldown_flag = " (cooldown active)" if ticker in recently_traded else ""
        if articles:
            headlines = "; ".join(a["headline"] for a in articles[:3])
            lines.append(f"- {ticker}: {_format_indicators(snap)} | news: {headlines}{cooldown_flag}")
        else:
            lines.append(f"- {ticker}: {_format_indicators(snap)} | no recent news, technical setup only{cooldown_flag}")
    return "\n".join(lines) if lines else "(no notable candidates today)"


def build_holdings_block(holdings, candidates, open_order_tickers, recently_traded):
    if not holdings:
        return "(no current holdings)"
    lines = []
    for ticker, pos in holdings.items():
        snap = get_indicator_snapshot(ticker)
        related_news = ""
        if ticker in candidates and candidates[ticker]:
            headlines = "; ".join(a["headline"] for a in candidates[ticker][:2])
            related_news = f" | news: {headlines}"
        flags = ""
        if ticker in open_order_tickers:
            flags += " (order pending)"
        elif ticker in recently_traded:
            flags += " (cooldown active)"
        lines.append(
            f"- {ticker}: {pos['qty']} shares, P/L {pos['unrealized_plpc']:+.2f}%, "
            f"{_format_indicators(snap)}{related_news}{flags}"
        )
    return "\n".join(lines)


def get_trade_decisions(candidates, account_snapshot):
    holdings = account_snapshot.get("holdings", {})
    cash = account_snapshot.get("cash", 0)
    total_value = account_snapshot.get("total_value", cash)
    open_order_tickers = get_tickers_with_open_orders()
    recently_traded = get_recently_traded_tickers()

    new_candidates = {t: a for t, a in candidates.items() if t not in holdings}

    prompt = PROMPT_TEMPLATE.format(
        cash=cash,
        total_value=total_value,
        max_pct=int(MAX_POSITION_PCT * 100),
        holdings_block=build_holdings_block(holdings, candidates, open_order_tickers, recently_traded),
        news_block=build_news_block(new_candidates, recently_traded),
    )

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
    )
    raw_text = response.text.strip()
    raw_text = re.sub(r"^```json\s*|\s*```$", "", raw_text.strip())

    try:
        parsed = json.loads(raw_text)
        trades = parsed.get("trades", [])
    except json.JSONDecodeError:
        print("Warning: could not parse Gemini response as JSON:")
        print(raw_text)
        return []

    final = []
    for t in trades:
        ticker = t.get("ticker")
        action = t.get("action", "").lower()
        if ticker in open_order_tickers:
            continue
        if action == "buy" and ticker in recently_traded:
            continue
        final.append(t)
    return final
