"""
Evaluates news candidates and portfolio state using Gemini as a structured
quantitative Veto Agent. Enforces confidence scores, objective justifications,
and self-consistency validations while rejecting weak trades automatically.
"""

import json
import re
import yfinance as yf
from google import genai
from google.genai import types

from config import (
    GEMINI_API_KEY, 
    GEMINI_MODEL, 
    MAX_POSITION_PCT, 
    PRICE_HISTORY_DAYS,
    MIN_GEMINI_CONFIDENCE
)
from trader import get_indicator_snapshot, get_tickers_with_open_orders, get_recently_traded_tickers

client = genai.Client(api_key=GEMINI_API_KEY)

# --- Structured Schema Definition for Gemini Validation Gate ---
VALIDATION_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "trades": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "ticker": {"type": "STRING"},
                    "action": {"type": "STRING", "enum": ["buy", "sell", "hold"]},
                    "dollar_amount": {"type": "NUMBER"},
                    "time_horizon": {"type": "STRING", "enum": ["short", "long"]},
                    "confidence_score": {"type": "INTEGER"}, # Scale 0 to 100
                    "objective_evidence": {
                        "type": "ARRAY",
                        "items": {"type": "STRING"}
                    },
                    "risk_factors": {
                        "type": "ARRAY",
                        "items": {"type": "STRING"}
                    },
                    "consistency_check_passed": {"type": "BOOLEAN"},
                    "rejection_reason": {"type": "STRING"}
                },
                "required": [
                    "ticker",
                    "action",
                    "confidence_score",
                    "objective_evidence",
                    "risk_factors",
                    "consistency_check_passed"
                ]
            }
        }
    },
    "required": ["trades"]
}

SYSTEM_INSTRUCTION = """You are a quantitative risk management Veto Agent for a paper-trading portfolio.
Your sole duty is to validate or reject proposed trade setups based on rigorous data alignment.

Rules:
1. Require at least two distinct, data-backed justification points in 'objective_evidence'.
2. Assign an explicit confidence score from 0 to 100 based strictly on technical/news alignment.
3. Reject trades where high volatility chaos or market trend directly contradicts the trade direction.
4. If internal reasoning contains contradictions (e.g., calling a breakdown bullish), set 'consistency_check_passed': false.
5. Do not invent trades. Recommend zero trades if no setups meet strict criteria.
"""

PROMPT_TEMPLATE = """Evaluate the following candidates and current portfolio holdings.

Current Portfolio State:
- Cash available: ${cash:,.2f}
- Total portfolio value: ${total_value:,.2f}

Existing Holdings:
{holdings_block}

Candidate Tickers & Technical/News Signals:
{news_block}

Rules & Constraints:
- Position cap: Max {max_pct}% of portfolio value per ticker.
- Tickers marked '(order pending)' or '(cooldown active)' must be skipped for NEW buys.
- Diversify across sectors.
- Only output trades with high quantitative conviction and clear data justifications.
"""


def _safe_sector(ticker: str) -> str:
    """
    Fetch sector via yfinance with rate limit safety fallback.
    """
    try:
        info = yf.Ticker(ticker).info or {}
        sector = info.get("sector")
        return sector if sector else "unknown"
    except Exception:
        return "unknown"


def _format_indicators(snap):
    if snap is None:
        return "indicators unavailable"
    parts = [f"price ${snap['price']:.2f}"]
    if snap.get("momentum_pct") is not None:
        parts.append(f"{PRICE_HISTORY_DAYS}d momentum {snap['momentum_pct']:+.2f}%")
    if snap.get("rsi") is not None:
        parts.append(f"RSI {snap['rsi']}")
    if snap.get("rsi_zscore") is not None:
        parts.append(f"RSI Z-Score {snap['rsi_zscore']:+.2f}")
    parts.append(f"trend: {snap.get('trend', 'unknown')}")
    if snap.get("volume_trend_pct") is not None:
        parts.append(f"volume {snap['volume_trend_pct']:+.2f}% vs avg")

    if snap.get("macd"):
        m = snap["macd"]
        parts.append(f"MACD hist {m['histogram']:+.3f}")

    if snap.get("bollinger"):
        b = snap["bollinger"]
        parts.append(f"Bollinger %B {b['percent_b']:.2f}")

    return ", ".join(parts)


def build_news_block(candidates, recently_traded, sector_cache):
    lines = []
    for ticker, articles in candidates.items():
        snap = get_indicator_snapshot(ticker)
        sector = sector_cache.get(ticker, "unknown")
        cooldown_flag = " (cooldown active)" if ticker in recently_traded else ""

        if articles:
            headlines = "; ".join(a.get("headline", "") for a in articles[:3])
            lines.append(
                f"- {ticker} (Sector: {sector}): {_format_indicators(snap)} | filtered news: {headlines}{cooldown_flag}"
            )
        else:
            lines.append(
                f"- {ticker} (Sector: {sector}): {_format_indicators(snap)} | pure technical setup{cooldown_flag}"
            )

    return "\n".join(lines) if lines else "(no notable candidates today)"


def build_holdings_block(holdings, candidates, open_order_tickers, recently_traded, sector_cache):
    if not holdings:
        return "(no current holdings)"

    lines = []
    for ticker, pos in holdings.items():
        snap = get_indicator_snapshot(ticker)
        sector = sector_cache.get(ticker, "unknown")

        related_news = ""
        if ticker in candidates and candidates[ticker]:
            headlines = "; ".join(a.get("headline", "") for a in candidates[ticker][:2])
            related_news = f" | news: {headlines}"

        flags = ""
        if ticker in open_order_tickers:
            flags += " (order pending)"
        elif ticker in recently_traded:
            flags += " (cooldown active)"

        lines.append(
            f"- {ticker} (Sector: {sector}): {pos['qty']} shares, P/L {pos['unrealized_plpc']:+.2f}%, "
            f"{_format_indicators(snap)}{related_news}{flags}"
        )

    return "\n".join(lines)


def get_trade_decisions(candidates, account_snapshot):
    holdings = account_snapshot.get("holdings", {})
    cash = account_snapshot.get("cash", 0)
    total_value = account_snapshot.get("total_value", cash)

    open_order_tickers = get_tickers_with_open_orders()
    recently_traded = get_recently_traded_tickers()

    # Sector caching to prevent repetitive lookup overhead
    all_tickers = set(holdings.keys()) | set(candidates.keys())
    sector_cache = {t: _safe_sector(t) for t in all_tickers}

    prompt = PROMPT_TEMPLATE.format(
        cash=cash,
        total_value=total_value,
        max_pct=int(MAX_POSITION_PCT * 100),
        holdings_block=build_holdings_block(
            holdings, candidates, open_order_tickers, recently_traded, sector_cache
        ),
        news_block=build_news_block(candidates, recently_traded, sector_cache),
    )

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=VALIDATION_SCHEMA,
                system_instruction=SYSTEM_INSTRUCTION,
                temperature=0.1
            )
        )

        raw_text = response.text.strip()
        parsed = json.loads(raw_text)
        trades = parsed.get("trades", [])

    except Exception as e:
        print(f"Error during Gemini trade decision validation: {e}")
        return []

    validated_trades = []
    for t in trades:
        ticker = t.get("ticker")
        action = t.get("action", "").lower()
        confidence = t.get("confidence_score", 0)
        consistency_passed = t.get("consistency_check_passed", False)

        # 1. Skip pending orders or buy cooldowns
        if ticker in open_order_tickers:
            print(f"Skipping {ticker}: Order already pending.")
            continue
        if action == "buy" and ticker in recently_traded:
            print(f"Skipping {ticker}: Cooldown active.")
            continue

        # 2. Threshold Veto Gate: Reject low confidence or failed consistency checks
        if confidence < MIN_GEMINI_CONFIDENCE or not consistency_passed:
            reason = t.get("rejection_reason", "Failed minimum confidence score or self-consistency check.")
            print(f"❌ [VETO REJECTED] {ticker} (Action: {action}): Confidence {confidence}/{MIN_GEMINI_CONFIDENCE} | Reason: {reason}")
            continue

        print(f"✅ [VETO APPROVED] {ticker} (Action: {action}): Confidence {confidence}/100 | Evidence: {t.get('objective_evidence')}")
        validated_trades.append(t)

    return validated_trades
