"""
Builds the full picture for Gemini every run: existing holdings (with P/L,
full technical indicators, quant score, and any related news), news-driven
candidates, and fixed-watchlist tickers -- both candidate pools first pass
through a quantitative pre-screen (signal_score.py) before they're shown
to Gemini at all. Also informs Gemini of the current market regime, though
regime-based position-size enforcement happens in code (trader.py), not
via prompt instruction alone. Asks for a conviction score (1-10) on every
surviving trade idea, which trader.py uses to scale position size (and,
for exceptional conviction, how much of the cash reserve it may touch).

IMPORTANT -- quota management: each model in GEMINI_MODEL_FALLBACKS
(config.py) has its OWN independent daily (RPD) and per-minute (RPM) free
tier quota -- confirmed directly from the account's own rate-limit
dashboard, not guessed. This file tracks usage separately per model,
rotates through them in the configured priority order, and spreads calls
evenly across the day (or across remaining market hours, if
GEMINI_ONLY_DURING_MARKET_HOURS is on) to use as much of the combined
daily budget as possible without tripping any single model's real limit.
If Google's live response ever reports a different real quotaValue than
what's configured, that number is parsed out and adopted automatically
for the rest of the day, so the bot self-corrects if the confirmed
numbers ever drift.
"""

import json
import os
import re
from datetime import datetime, timedelta

import pytz
from google import genai
from google.genai import types

from config import (
    GEMINI_API_KEY, GEMINI_MODEL, GEMINI_MODEL_FALLBACKS, GEMINI_MODEL_LIMITS,
    GEMINI_QUOTA_RESET_TIMEZONE, GEMINI_ONLY_DURING_MARKET_HOURS,
    MAX_POSITION_PCT, MIN_CONVICTION_TO_TRADE,
    WATCHLIST, ATR_STOP_MULTIPLIER, ATR_TAKE_PROFIT_MULTIPLIER,
    MIN_SIGNAL_SCORE_TO_CONSIDER, REGIME_POSITION_MULTIPLIERS,
    EXCEPTIONAL_CONVICTION_THRESHOLD, CONSOLIDATION_SCORE_THRESHOLD,
    MAX_OPEN_POSITIONS,
)
from trader import (
    get_full_indicators, get_tickers_with_open_orders, get_tickers_on_cooldown,
    is_market_open,
)
from signal_score import calculate_signal_score

_client = genai.Client(api_key=GEMINI_API_KEY)

_CALL_TRACKER_FILE = os.path.join(os.path.dirname(__file__), "data", "gemini_call_tracker.json")
_QUOTA_TZ = pytz.timezone(GEMINI_QUOTA_RESET_TIMEZONE)

# Forces Gemini's output into this exact shape -- removes the need to hope
# it didn't wrap the JSON in markdown fences or add commentary.
_RESPONSE_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "trades": types.Schema(
            type=types.Type.ARRAY,
            items=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "ticker": types.Schema(type=types.Type.STRING),
                    "action": types.Schema(type=types.Type.STRING, enum=["buy", "sell"]),
                    "dollar_amount": types.Schema(type=types.Type.NUMBER),
                    "conviction": types.Schema(type=types.Type.INTEGER),
                    "reasoning": types.Schema(type=types.Type.STRING),
                },
                required=["ticker", "action", "conviction"],
            ),
        )
    },
    required=["trades"],
)

PROMPT_TEMPLATE = """You are a moderately aggressive trading analyst for a SIMULATED \
paper-trading portfolio (no real money). Every trade idea you propose MUST include a \
"conviction" score from 1-10, reflecting how strongly the signals (news, trend, \
momentum, RSI, MACD, volume) agree with each other. Ideas below conviction {min_conviction} \
will be discarded automatically, so only include ideas you'd actually rate that high. \
Reserve conviction {exceptional_conviction}+ for truly exceptional setups only -- those get \
access to extra cash reserve room in code, so treat that rating as rare, not routine.

{regime_block}

Current portfolio:
- Cash available: ${cash:,.2f}
- Total portfolio value: ${total_value:,.2f}

Existing holdings (review each: hold, add, trim, or exit -- quant score shown for
context only, it does NOT gate whether you can discuss a holding):
{holdings_block}

News-driven candidates (mentioned in fresh news, not currently held -- this list has
ALREADY been filtered to only tickers that cleared a quantitative pre-screen; weak
setups were removed before you ever saw this list):
{news_block}

Watchlist candidates (liquid large-caps, evaluated on technicals only -- also
pre-screened; may have no fresh news):
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
- Quant score: a 0-100 composite of the above (trend, ADX, RSI, volume, MACD), computed
  independently of you. Not a replacement for your judgment -- a useful cross-check.

Rules:
- Never let any single position exceed {max_pct}% of total portfolio value.
- Only use tickers that appear in the lists above -- do not invent tickers.
- Tickers marked "(unavailable: pending order or cooldown)" must be skipped entirely.
- Prefer setups where multiple signals agree (e.g. news + uptrend + rising volume + MACD bullish)
  over single-signal ideas -- these deserve higher conviction scores.
- It's completely fine to return zero trades if nothing meets the bar.
- Hard ATR-based stop-loss ({stop_mult}x ATR below entry) and take-profit ({tp_mult}x ATR above entry)
  are already enforced separately in code -- focus your reasoning on the setup itself, not exit levels.
- Market-regime-based position-size scaling (described above) is enforced automatically in code,
  regardless of what you propose -- you don't need to and cannot override it.
- A hard cap on total number of open positions ({max_positions} held) is also enforced in code. If the portfolio is at \
or over that cap, the consolidation engine will automatically purge the lowest-scoring excess positions \
that score below {consolidation_threshold}. Focus your work on trimming existing weaker holdings to clear space.

Respond with ONLY valid JSON (no markdown fences, no commentary):
{{
  "trades": [
    {{"ticker": "AAPL", "action": "buy", "dollar_amount": 5000, "conviction": 8, "reasoning": "short reason"}}
  ]
}}
"""


def _regime_block(regime):
    multiplier = REGIME_POSITION_MULTIPLIERS.get(regime, 0.6)
    notes = {
        "BULLISH": "Broad market trend (SPY) is favorable. Normal position sizing applies.",
        "NEUTRAL": f"Broad market trend (SPY) is mixed/sideways. The system is automatically "
                   f"scaling all new position sizes to {multiplier:.0%} of normal as a precaution.",
        "BEARISH": "Broad market trend (SPY) is unfavorable. The system will automatically block "
                   "ALL new buy orders this run (both opens and adds), regardless of what you "
                   "propose below -- focus your reasoning on hold/trim/exit decisions for existing "
                   "holdings.",
        "HIGH_VOLATILITY": f"Market-wide volatility (SPY) is elevated. The system is automatically "
                           f"scaling all new position sizes to {multiplier:.0%} of normal as a "
                           f"precaution, regardless of trend direction.",
    }
    note = notes.get(regime, "Regime unrecognized; treating as neutral/cautious.")
    return f"Market regime: {regime}. {note}"


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


def _score_candidates(tickers):
    """
    Fetches indicators once per ticker and computes its quant score.
    Returns {ticker: {"indicators": dict_or_None, "score": float}}.
    """
    scored = {}
    for t in tickers:
        data = get_full_indicators(t)
        scored[t] = {"indicators": data, "score": calculate_signal_score(data)}
    return scored


def build_holdings_block(holdings, candidates, unavailable, scored):
    if not holdings:
        return "(no current holdings)"
    lines = []
    for ticker, pos in holdings.items():
        flag = " (unavailable: pending order or cooldown)" if ticker in unavailable else ""
        news = ""
        if ticker in candidates:
            news = " | news: " + "; ".join(a["headline"] for a in candidates[ticker][:2])
        info = scored.get(ticker, {})
        data = info.get("indicators")
        score = info.get("score", 0.0)
        lines.append(
            f"- {ticker}: {pos['qty']} shares, unrealized P/L {pos['unrealized_plpc']:+.2f}%, "
            f"quant score {score:.0f}/100, {_indicators_str(data)}{news}{flag}"
        )
    return "\n".join(lines)


def build_news_block(candidates, scored, unavailable, min_score):
    if not candidates:
        return "(no new candidates from news this run)"
    lines = []
    for ticker, articles in candidates.items():
        info = scored.get(ticker, {})
        score = info.get("score", 0.0)
        if score < min_score:
            continue
        flag = " (unavailable: pending order or cooldown)" if ticker in unavailable else ""
        headlines = "; ".join(a["headline"] for a in articles[:3])
        data = info.get("indicators")
        lines.append(f"- {ticker}: quant score {score:.0f}/100, {_indicators_str(data)} | news: {headlines}{flag}")
    return "\n".join(lines) if lines else "(no news candidates cleared the quantitative pre-screen this run)"


def build_watchlist_block(scored, unavailable, min_score):
    lines = []
    for ticker, info in scored.items():
        score = info.get("score", 0.0)
        if score < min_score:
            continue
        flag = " (unavailable: pending order or cooldown)" if ticker in unavailable else ""
        data = info.get("indicators")
        lines.append(f"- {ticker}: quant score {score:.0f}/100, {_indicators_str(data)} | no fresh news{flag}")
    return "\n".join(lines) if lines else "(no watchlist tickers cleared the quantitative pre-screen this run)"


# ============================================================
# Per-model quota tracking (RPD + RPM, confirmed from live dashboard)
# ============================================================

def _quota_day_now():
    """Current date string in the quota-reset timezone (Pacific by default)."""
    return datetime.now(pytz.utc).astimezone(_QUOTA_TZ).strftime("%Y-%m-%d")


def _seconds_until_next_quota_day():
    now_local = datetime.now(pytz.utc).astimezone(_QUOTA_TZ)
    next_midnight = (now_local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max((next_midnight - now_local).total_seconds(), 1)


def _default_model_state():
    return {"count": 0, "recent_calls": [], "exhausted": False}


def _default_tracker():
    return {
        "date": _quota_day_now(),
        "last_call": None,  # ISO timestamp (UTC) of the last successful call, any model
        "models": {name: _default_model_state() for name in GEMINI_MODEL_FALLBACKS},
        # Real limits learned from an actual 429 response override the
        # config.py defaults for the rest of the day. Persists across the
        # day-rollover below since it's a fact about the account, not
        # something that resets daily.
        "confirmed_limits": {},  # e.g. {"gemini-2.5-flash": {"rpd": 20, "rpm": 5}}
    }


def _load_tracker():
    if not os.path.exists(_CALL_TRACKER_FILE):
        return _default_tracker()
    try:
        with open(_CALL_TRACKER_FILE) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return _default_tracker()

    confirmed = data.get("confirmed_limits", {})
    if data.get("date") != _quota_day_now():
        fresh = _default_tracker()
        fresh["confirmed_limits"] = confirmed  # keep learned facts across day rollover
        return fresh

    data.setdefault("last_call", None)
    data.setdefault("models", {})
    data.setdefault("confirmed_limits", {})
    for name in GEMINI_MODEL_FALLBACKS:
        data["models"].setdefault(name, _default_model_state())
    return data


def _save_tracker(data):
    os.makedirs(os.path.dirname(_CALL_TRACKER_FILE), exist_ok=True)
    with open(_CALL_TRACKER_FILE, "w") as f:
        json.dump(data, f)


def _effective_limits(tracker, model_name):
    """Real learned limits (from an actual 429) override the config.py defaults."""
    learned = tracker["confirmed_limits"].get(model_name, {})
    configured = GEMINI_MODEL_LIMITS.get(model_name, {"rpd": 20, "rpm": 5})
    return {
        "rpd": learned.get("rpd", configured["rpd"]),
        "rpm": learned.get("rpm", configured["rpm"]),
    }


def _prune_recent_calls(recent_calls):
    now = datetime.now(pytz.utc)
    cutoff = now - timedelta(seconds=60)
    kept = []
    for ts in recent_calls:
        try:
            if datetime.fromisoformat(ts) > cutoff:
                kept.append(ts)
        except ValueError:
            continue
    return kept


def _remaining_rpd(tracker, model_name):
    state = tracker["models"][model_name]
    if state["exhausted"]:
        return 0
    limit = _effective_limits(tracker, model_name)["rpd"]
    return max(0, limit - state["count"])


def _has_rpm_room(tracker, model_name):
    state = tracker["models"][model_name]
    state["recent_calls"] = _prune_recent_calls(state["recent_calls"])
    limit = _effective_limits(tracker, model_name)["rpm"]
    return len(state["recent_calls"]) < limit


def _total_remaining(tracker):
    return sum(_remaining_rpd(tracker, name) for name in GEMINI_MODEL_FALLBACKS)


def _extract_quota_value(error_str, marker):
    """
    Parses the REAL quota number straight out of Google's own 429 error
    body for the given marker (e.g. "PerDay" or "PerMinute"). Best-effort
    text parsing since the SDK only exposes this as a stringified error.
    """
    pattern = rf"'quotaId':\s*'[^']*{marker}[^']*'.*?'quotaValue':\s*'(\d+)'"
    match = re.search(pattern, error_str, re.DOTALL)
    return int(match.group(1)) if match else None


def _is_daily_quota_error(error_str):
    return "PerDay" in error_str or "GenerateRequestsPerDayPerProjectPerModel" in error_str


def _should_attempt_call(tracker):
    """
    Even-spacing gate across the COMBINED remaining budget of every
    non-exhausted model, so the bot uses as much of the real ~60/day total
    as possible without front-loading it all into the first hour.
    """
    total_remaining = _total_remaining(tracker)
    if total_remaining <= 0:
        return False, "all configured models have exhausted their daily free-tier quota"

    if tracker["last_call"] is None:
        return True, None

    try:
        last_call = datetime.fromisoformat(tracker["last_call"])
    except (ValueError, TypeError):
        return True, None

    seconds_left = _seconds_until_next_quota_day()
    min_gap_seconds = seconds_left / total_remaining
    elapsed = (datetime.now(pytz.utc) - last_call).total_seconds()

    if elapsed < min_gap_seconds:
        return False, (
            f"spacing calls evenly across remaining budget: {elapsed:.0f}s since last call, "
            f"need {min_gap_seconds:.0f}s ({total_remaining} call(s) left today across all models)"
        )
    return True, None


def _generate_with_rotation(prompt, tracker):
    """
    Tries each model in GEMINI_MODEL_FALLBACKS order that still has RPD
    remaining AND has RPM room in the last 60s. On a genuine daily-quota
    429, marks that model exhausted and moves to the next. Also parses
    real quotaValue numbers out of any 429 and adopts them going forward.
    """
    last_error = None
    for model_name in GEMINI_MODEL_FALLBACKS:
        if _remaining_rpd(tracker, model_name) <= 0:
            continue
        if not _has_rpm_room(tracker, model_name):
            continue

        try:
            response = _client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.3,
                    response_mime_type="application/json",
                    response_schema=_RESPONSE_SCHEMA,
                ),
            )
            now_iso = datetime.now(pytz.utc).isoformat()
            tracker["models"][model_name]["count"] += 1
            tracker["models"][model_name]["recent_calls"].append(now_iso)
            tracker["last_call"] = now_iso
            tracker["date"] = _quota_day_now()
            if model_name != GEMINI_MODEL:
                print(f"NOTE: used '{model_name}' this run (rotation/fallback), not the default '{GEMINI_MODEL}'.")
            return response
        except Exception as e:
            error_str = str(e)
            last_error = e

            real_rpd = _extract_quota_value(error_str, "PerDay")
            real_rpm = _extract_quota_value(error_str, "PerMinute")
            if real_rpd is not None or real_rpm is not None:
                learned = tracker["confirmed_limits"].setdefault(model_name, {})
                if real_rpd is not None:
                    learned["rpd"] = real_rpd
                if real_rpm is not None:
                    learned["rpm"] = real_rpm
                print(f"Learned real quota for '{model_name}' from Google's response: {learned}. Adopting it.")

            if _is_daily_quota_error(error_str):
                tracker["models"][model_name]["exhausted"] = True
                print(f"Model '{model_name}' hit its daily free-tier quota -- marking exhausted until quota reset.")
            else:
                print(f"Gemini model '{model_name}' failed (not a daily-quota error, will retry it next run): {e}")
            continue

    raise last_error if last_error else RuntimeError("No Gemini models had remaining quota.")


def get_trade_decisions(candidates, account_snapshot, regime="NEUTRAL"):
    """
    Returns (trades, meta) where trades is the filtered list of trade
    ideas and meta is a stats dict for logging to performance.csv:
        {"candidates_considered": int, "candidates_passed_prescreen": int,
         "throttled": bool, "gemini_calls_today": int}
    """
    holdings = account_snapshot.get("holdings", {})
    cash = account_snapshot.get("cash", 0)
    total_value = account_snapshot.get("total_value", cash)

    open_orders = get_tickers_with_open_orders()
    cooldowns = get_tickers_on_cooldown()
    unavailable = open_orders | cooldowns

    new_candidates = {t: a for t, a in candidates.items() if t not in holdings}
    watchlist_tickers = [t for t in WATCHLIST if t not in holdings and t not in new_candidates]

    scored_holdings = _score_candidates(list(holdings.keys()))
    scored_news = _score_candidates(list(new_candidates.keys()))
    scored_watchlist = _score_candidates(watchlist_tickers)

    tracker = _load_tracker()
    calls_today = sum(tracker["models"][m]["count"] for m in GEMINI_MODEL_FALLBACKS)

    meta = {
        "candidates_considered": len(scored_news) + len(scored_watchlist),
        "candidates_passed_prescreen": sum(
            1 for info in list(scored_news.values()) + list(scored_watchlist.values())
            if info["score"] >= MIN_SIGNAL_SCORE_TO_CONSIDER
        ),
        "throttled": False,
        "gemini_calls_today": calls_today,
    }

    if GEMINI_ONLY_DURING_MARKET_HOURS and not is_market_open():
        print("Skipping Gemini call this run: market is closed and GEMINI_ONLY_DURING_MARKET_HOURS is enabled.")
        meta["throttled"] = True
        return [], meta

    should_call, reason = _should_attempt_call(tracker)
    if not should_call:
        print(f"Skipping Gemini call this run: {reason}")
        meta["throttled"] = True
        return [], meta

    prompt = PROMPT_TEMPLATE.format(
        cash=cash,
        total_value=total_value,
        max_pct=int(MAX_POSITION_PCT * 100),
        min_conviction=MIN_CONVICTION_TO_TRADE,
        exceptional_conviction=EXCEPTIONAL_CONVICTION_THRESHOLD,
        consolidation_threshold=CONSOLIDATION_SCORE_THRESHOLD,
        max_positions=MAX_OPEN_POSITIONS,
        stop_mult=ATR_STOP_MULTIPLIER,
        tp_mult=ATR_TAKE_PROFIT_MULTIPLIER,
        regime_block=_regime_block(regime),
        holdings_block=build_holdings_block(holdings, candidates, unavailable, scored_holdings),
        news_block=build_news_block(new_candidates, scored_news, unavailable, MIN_SIGNAL_SCORE_TO_CONSIDER),
        watchlist_block=build_watchlist_block(scored_watchlist, unavailable, MIN_SIGNAL_SCORE_TO_CONSIDER),
    )

    try:
        response = _generate_with_rotation(prompt, tracker)
        _save_tracker(tracker)
        meta["gemini_calls_today"] = sum(tracker["models"][m]["count"] for m in GEMINI_MODEL_FALLBACKS)
    except Exception as e:
        _save_tracker(tracker)  # persist any exhaustion/learned-limit flags either way
        print(f"Gemini call failed on all models with remaining quota: {e}")
        meta["throttled"] = True
        return [], meta

    raw_text = (response.text or "").strip()
    raw_text = re.sub(r"^```json\s*|\s*```$", "", raw_text)  # belt-and-suspenders

    try:
        trades = json.loads(raw_text).get("trades", [])
    except json.JSONDecodeError:
        print("Warning: could not parse Gemini response as JSON:")
        print(raw_text)
        return [], meta

    # Enforce rules in code too, not just via prompt instructions
    filtered = []
    for t in trades:
        if t.get("ticker") in unavailable:
            continue
        if t.get("conviction", 0) < MIN_CONVICTION_TO_TRADE:
            continue
        filtered.append(t)
    return filtered, meta
