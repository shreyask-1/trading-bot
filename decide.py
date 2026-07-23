"""
Sends the day's news candidates + current portfolio state to Gemini and
asks it to come back with a structured list of trade decisions.
"""

import json
import re
import google.generativeai as genai
from config import GEMINI_API_KEY, GEMINI_MODEL, MAX_POSITION_PCT

genai.configure(api_key=GEMINI_API_KEY)


PROMPT_TEMPLATE = """You are a cautious, balanced-risk trading analyst for a SIMULATED \
paper-trading portfolio (no real money). Analyze the news below and decide on \
zero or more trades.

Current portfolio:
- Cash available: ${cash:,.2f}
- Current holdings: {holdings}
- Total portfolio value: ${total_value:,.2f}

Rules you must follow:
- Never recommend spending more than {max_pct}% of total portfolio value on a single stock.
- Only choose tickers from the candidates list below -- do not invent tickers.
- Prefer diversification over concentration.
- If the news doesn't give a clear, reasonably strong signal, it's fine to recommend nothing (empty list).
- Be balanced: not overly cautious, not reckless. Moderate conviction trades only.

News candidates (ticker: recent headlines):
{news_block}

Respond with ONLY valid JSON (no markdown fences, no commentary) in this exact shape:
{{
  "trades": [
    {{"ticker": "AAPL", "action": "buy", "dollar_amount": 5000, "reasoning": "short reason"}}
  ]
}}
If you recommend no trades, return {{"trades": []}}.
"""


def build_news_block(candidates):
    lines = []
    for ticker, articles in candidates.items():
        headlines = "; ".join(a["headline"] for a in articles[:3])
        lines.append(f"- {ticker}: {headlines}")
    return "\n".join(lines) if lines else "(no notable news today)"


def get_trade_decisions(candidates, account_snapshot):
    """
    account_snapshot: {"cash": ..., "total_value": ..., "holdings": {ticker: qty}}
    -- this comes from trader.get_account_snapshot(), i.e. your real Alpaca
    paper account, not a local simulated file.
    """
    holdings_str = ", ".join(
        f"{t}: {q} shares" for t, q in account_snapshot.get("holdings", {}).items()
    ) or "none"

    cash = account_snapshot.get("cash", 0)
    total_value = account_snapshot.get("total_value", cash)

    prompt = PROMPT_TEMPLATE.format(
        cash=cash,
        holdings=holdings_str,
        total_value=total_value,
        max_pct=int(MAX_POSITION_PCT * 100),
        news_block=build_news_block(candidates),
    )

    model = genai.GenerativeModel(GEMINI_MODEL)
    response = model.generate_content(prompt)
    raw_text = response.text.strip()

    # Gemini sometimes wraps JSON in ```json fences despite instructions -- strip them
    raw_text = re.sub(r"^```json\s*|\s*```$", "", raw_text.strip())

    try:
        parsed = json.loads(raw_text)
        return parsed.get("trades", [])
    except json.JSONDecodeError:
        print("Warning: could not parse Gemini response as JSON:")
        print(raw_text)
        return []
