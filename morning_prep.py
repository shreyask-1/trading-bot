"""
Morning prep (run overnight / before the open, scheduled via GitHub Actions).

Instead of idling at night, this gathers everything for the next trading day:
  1. scored news (0-10 importance per article, sentiment -1..+1)
  2. chart setups across the S&P 500 universe (top news names + a rotating slice)
  3. market regime (SPY + QQQ + VIX, defensive when ugly)
  4. the earnings calendar (next ~3 weeks, Finnhub)

...then makes ONE Gemini call to decide what's important and saves:
  logs/morning_brief_YYYY-MM-DD.md   (readable briefing)
  data/morning_candidates.json       (machine-readable picks; the trading loop
                                      consumes this at the open)

Reuses the same model-discovery / quota tracker as decide.py, so this call
counts against the same daily quota and is properly spaced.
"""

import json
import os
from datetime import datetime, timedelta

import requests
from google.genai import types

from config import FINNHUB_API_KEY
from news import get_news_candidates
from trader import get_full_indicators, get_market_regime, EARNINGS_CAL_FILE
from signal_score import calculate_signal_score
from decide import (
    _load_tracker,
    _get_effective_model_list,
    _generate_with_rotation,
    _save_tracker,
)

BASE_DIR = os.path.dirname(__file__)
LOG_DIR = os.path.join(BASE_DIR, "logs")
DATA_DIR = os.path.join(BASE_DIR, "data")

# How many candidates to fully analyze per prep run (news names always count).
MAX_NEWS_CANDIDATES = 15
MAX_UNIVERSE_CANDIDATES = 25
MAX_PRIORITY_PICKS = 12

_PREP_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "briefing": types.Schema(type=types.Type.STRING),
        "priority_tickers": types.Schema(
            type=types.Type.ARRAY,
            items=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "ticker": types.Schema(type=types.Type.STRING),
                    "priority": types.Schema(type=types.Type.NUMBER),
                    "stance": types.Schema(type=types.Type.STRING, enum=["buy", "watch", "avoid", "sell"]),
                    "notes": types.Schema(type=types.Type.STRING),
                    "key_levels": types.Schema(type=types.Type.STRING),
                },
                required=["ticker", "priority"],
            ),
        ),
    },
    required=["briefing"],
)

_PREP_PROMPT = """You are a pre-market research analyst preparing a SIMULATED paper-trading daytrading portfolio (no real money). It is {now_et}. Market regime: {regime}. {vix_note}

Your job: decide what actually matters TODAY from the gathered data, and rank the highest-probability opportunities. Quality over quantity.

Top news (article importance 0-10, sentiment -1..+1):
{news_block}

Top chart setups (quant score 0-100, then key fields):
{chart_block}

Earnings in the next 3 weeks (ticker: date):
{earnings_block}

Phase 2 context (economic calendar, analyst actions, insider activity, Reddit sentiment, SEC filings):
{fundamental_block}

Respond with ONLY valid JSON:
{{"briefing": "a 3-6 sentence summary of today's market context and the highest-conviction themes", "priority_tickers": [{{"ticker": "NVDA", "priority": 9, "stance": "buy", "notes": "why it matters", "key_levels": "support/resistance or watch levels"}}]}}

Rules:
- "buy" stance = a concrete entry setup worth trading at the open; "watch" = waiting for confirmation; "avoid" = skip; "sell" = exit/trim pressure.
- Priority 1-10, 10 = highest. Only {max_picks} picks max. No filler."""


def refresh_earnings_calendar():
    """Fetch the next ~3 weeks of earnings dates from Finnhub (best-effort)."""
    try:
        today = datetime.now().date()
        to = (today + timedelta(days=21)).isoformat()
        url = "https://finnhub.io/api/v1/calendar/earnings"
        resp = requests.get(url, params={"from": today.isoformat(), "to": to, "token": FINNHUB_API_KEY}, timeout=15)
        resp.raise_for_status()
        data = resp.json().get("earningsCalendar", [])
        cal = {}
        for e in data:
            s = e.get("symbol")
            d = e.get("date")
            if s and d:
                cal.setdefault(s, d)
        os.makedirs(LOG_DIR, exist_ok=True)
        tmp = EARNINGS_CAL_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cal, f)
        os.replace(tmp, EARNINGS_CAL_FILE)
        print(f"Earnings calendar refreshed: {len(cal)} tickers over the next 3 weeks.")
        return cal
    except Exception as e:
        print(f"Could not fetch earnings calendar (continuing without it): {e}")
        return {}


def _score_candidates(tickers):
    scored = {}
    for t in tickers:
        data = get_full_indicators(t)
        score = calculate_signal_score(data) if data else 0.0
        scored[t] = {"indicators": data, "score": score}
    return scored


def _fmt_setup(d):
    d = d or {}
    parts = [f"trend {d.get('trend')}"]
    if d.get("rsi_14") is not None:
        parts.append(f"RSI {d['rsi_14']:.0f}")
    if d.get("atr_14") is not None:
        parts.append(f"ATR {d['atr_14']:.2f}")
    if d.get("adx_14") is not None:
        parts.append(f"ADX {d['adx_14']:.0f}")
    if d.get("macd_cross"):
        parts.append(f"MACD {d['macd_cross']}")
    if d.get("gap_pct") is not None:
        parts.append(f"gap {d['gap_pct']:+.1f}%")
    if d.get("dist_from_52w_high_pct") is not None:
        parts.append(f"{d['dist_from_52w_high_pct']:.0f}% off 52wH")
    if d.get("dist_from_52w_low_pct") is not None:
        parts.append(f"{d['dist_from_52w_low_pct']:.0f}% off 52wL")
    if d.get("days_until_earnings") is not None:
        parts.append(f"earnings in {d['days_until_earnings']}d")
    if d.get("support") is not None:
        parts.append(f"sup {d['support']:.2f}")
    if d.get("resistance") is not None:
        parts.append(f"res {d['resistance']:.2f}")
    return " | ".join(parts)


def _fmt_news(mentions):
    if not mentions:
        return "none"
    lines = []
    for t, arts in sorted(mentions.items(), key=lambda kv: -max((a.get("score", 0) for a in kv[1]), default=0))[:MAX_NEWS_CANDIDATES]:
        best = max((a for a in arts if a.get("score") is not None), key=lambda a: a.get("score", 0), default=arts[0])
        lines.append(
            f"- {t} (news {best.get('score', 0)}/10, sent {best.get('sentiment', 0):+.2f}): "
            f"{(best.get('headline') or '')[:140]}"
        )
    return "\n".join(lines)


def _fmt_charts(scored):
    if not scored:
        return "none"
    lines = []
    for t, info in sorted(scored.items(), key=lambda kv: -kv[1]["score"])[:MAX_UNIVERSE_CANDIDATES]:
        lines.append(f"- {t} (quant {info['score']:.0f}/100 | {_fmt_setup(info.get('indicators'))})")
    return "\n".join(lines)


def run():
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)

    print("=== Morning prep start ===")
    mentions, stats, _ = get_news_candidates()
    print(f"News: {stats['tickers_matched']} ticker(s) matched after filtering.")
    cal = refresh_earnings_calendar()

    # Exactly ONE Gemini call per calendar day. The workflow fires at two UTC
    # times to cover DST; the second trigger refreshes news/calendar but skips
    # the expensive call (the trading loop still gets the same briefing).
    date_str = datetime.now().strftime("%Y-%m-%d")
    brief_path = os.path.join(LOG_DIR, f"morning_brief_{date_str}.md")
    if os.path.exists(brief_path):
        print(f"Morning brief for {date_str} already exists -- skipping today's Gemini call.")
        return

    regime = get_market_regime()

    vix_note = ""
    if regime in ("BEARISH", "HIGH_VOLATILITY"):
        vix_note = f"Regime is {regime}: be DEFENSIVE -- prefer watch/avoid stances, no aggressive buying."

    # Analyze the top news names + a slice of the universe.
    news_tickers = list(mentions.keys())[:MAX_NEWS_CANDIDATES]
    scored_news = _score_candidates(news_tickers)
    scored_charts = _score_candidates([])  # populated below with a universe slice
    # Universe slice: everything else worth a look beyond news names.
    from sp500_data import SP500
    universe = [t for t, _ in SP500 if t not in set(news_tickers)]
    slice_size = min(MAX_UNIVERSE_CANDIDATES, len(universe))
    seed = int(datetime.now().strftime("%Y%m%d%H"))
    start = seed % len(universe) if universe else 0
    universe_slice = (universe[start:] + universe[:start])[:slice_size]
    scored_charts = _score_candidates(universe_slice)
    combined = {**scored_news, **scored_charts}

    # Phase 2: refresh the cached feeds (economic calendar, analyst actions,
    # Reddit, insider, SEC) so the morning call sees the same context as the
    # intraday loop. Fail-soft -- the brief still builds without them.
    fundamental_block = "none"
    try:
        from data_feeds import (
            fetch_economic_calendar,
            fetch_analyst_actions,
            get_insider_activity,
            get_sec_filings,
            get_reddit_sentiment,
            get_context_block,
        )
        analyze_tickers = list(dict.fromkeys(list(news_tickers) + list(universe_slice)))
        fetch_economic_calendar()
        fetch_analyst_actions()
        get_reddit_sentiment()
        get_insider_activity(analyze_tickers)
        get_sec_filings(analyze_tickers)
        fundamental_block = get_context_block(analyze_tickers)
    except Exception as e:
        print(f"Phase 2 context unavailable for morning prep (continuing): {e}")

    earnings_block = "; ".join(
        f"{t} {d}" for t, d in sorted(cal.items(), key=lambda kv: kv[1])[:20]
    ) or "none"

    now_et = datetime.now().astimezone().strftime("%Y-%m-%d %I:%M %p %Z")
    prompt = _PREP_PROMPT.format(
        now_et=now_et,
        regime=regime,
        vix_note=vix_note,
        news_block=_fmt_news(mentions),
        chart_block=_fmt_charts(combined),
        earnings_block=earnings_block,
        fundamental_block=fundamental_block,
        max_picks=MAX_PRIORITY_PICKS,
    )

    tracker = _load_tracker()
    model_list = _get_effective_model_list(tracker)
    try:
        response = _generate_with_rotation(prompt, tracker, model_list, schema=_PREP_SCHEMA)
        _save_tracker(tracker)
    except Exception as e:
        _save_tracker(tracker)
        print(f"Morning prep Gemini call failed: {e}")
        print("Skipping briefing generation; trading loop will run on live data only.")
        return

    raw = (response.text or "").strip()
    import re as _re
    raw = _re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=_re.MULTILINE)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print("Could not parse morning prep response as JSON:")
        print(raw[:2000])
        return

    briefing = data.get("briefing", "")
    picks = data.get("priority_tickers", [])[:MAX_PRIORITY_PICKS]

    date_str = datetime.now().strftime("%Y-%m-%d")
    brief_path = os.path.join(LOG_DIR, f"morning_brief_{date_str}.md")
    with open(brief_path, "w") as f:
        f.write(f"# Morning brief {date_str}\n\n")
        f.write(f"**Regime:** {regime}\n\n")
        f.write(f"**{briefing}**\n\n")
        f.write("## Priority tickers\n\n")
        if not picks:
            f.write("_None — stay in cash / defensive._\n")
        for p in picks:
            f.write(f"- **{p.get('ticker')}** (priority {p.get('priority')}, stance {p.get('stance')}): {p.get('notes') or ''}\n")
            if p.get("key_levels"):
                f.write(f"  - levels: {p['key_levels']}\n")
    print(f"Briefing saved to {brief_path}")

    picks_path = os.path.join(DATA_DIR, "morning_candidates.json")
    tmp = picks_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(picks, f)
    os.replace(tmp, picks_path)
    print(f"Candidates saved to {picks_path} ({len(picks)} picks)")

    print("=== Morning prep done ===")
    print(briefing)


if __name__ == "__main__":
    run()
