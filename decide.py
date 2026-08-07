"""
Decision Engine: Handles Gemini LLM reviews with rotating model fallback and rate limit tracking.
Includes pure-technical decision engine fallback when LLM calls are unavailable.
"""

import json
import os
import re
from datetime import datetime, timedelta
import pytz

from google import genai
from google.genai import types

from config import (
    GEMINI_API_KEY,
    GEMINI_MODEL,
    GEMINI_MODEL_FALLBACKS,
    GEMINI_MODEL_LIMITS,
    GEMINI_QUOTA_RESET_TIMEZONE,
    MAX_POSITION_PCT,
    MIN_CONVICTION_TO_TRADE,
    WATCHLIST,
    SWING_LOOKBACK_DAYS,
    MIN_SIGNAL_SCORE_TO_CONSIDER,
    REGIME_POSITION_MULTIPLIERS,
    EXCEPTIONAL_CONVICTION_THRESHOLD,
    CONSOLIDATION_SCORE_THRESHOLD,
    MAX_OPEN_POSITIONS,
    TECHNICAL_MIN_CONVICTION,
    TECHNICAL_CONVICTION_AGGRESSIVENESS,
)
from trader import (
    get_full_indicators,
    get_tickers_with_open_orders,
    get_tickers_on_cooldown,
)
from signal_score import calculate_signal_score

_client = genai.Client(api_key=GEMINI_API_KEY)

_CALL_TRACKER_FILE = os.path.join(
    os.path.dirname(__file__), "data", "gemini_call_tracker.json"
)
_QUOTA_TZ = pytz.timezone(GEMINI_QUOTA_RESET_TIMEZONE)

_SWING_LOW_KEY = f"recent_swing_low_{SWING_LOOKBACK_DAYS}d"
_SWING_HIGH_KEY = f"recent_swing_high_{SWING_LOOKBACK_DAYS}d"

_RESPONSE_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "trades": types.Schema(
            type=types.Type.ARRAY,
            items=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "ticker": types.Schema(type=types.Type.STRING),
                    "action": types.Schema(
                        type=types.Type.STRING, enum=["buy", "sell"]
                    ),
                    "dollar_amount": types.Schema(type=types.Type.NUMBER),
                    "conviction": types.Schema(type=types.Type.INTEGER),
                    "reasoning": types.Schema(type=types.Type.STRING),
                    "stop_loss": types.Schema(type=types.Type.NUMBER),
                    "take_profit": types.Schema(type=types.Type.NUMBER),
                },
                required=["ticker", "action", "conviction"],
            ),
        )
    },
    required=["trades"],
)

PROMPT_TEMPLATE = """You are a moderately aggressive DAY-TRADING analyst for a SIMULATED paper-trading portfolio (no real money). You have access to BOTH daily-chart context (trend, moving averages, multi-week momentum) AND short-term intraday context (5-minute-bar RSI, intraday momentum since today's open, VWAP deviation) for every ticker below -- use both: the daily context tells you the broader setup, the intraday context tells you whether NOW is a good moment to act on it.

Every trade idea you propose MUST include a "conviction" score from 1-10, reflecting how strongly the signals agree with each other. Ideas below conviction {min_conviction} will be discarded automatically.

Current portfolio: - Cash available: ${cash:,.2f} - Total portfolio value: ${total_value:,.2f}
Existing holdings: {holdings_block}
News-driven candidates: {news_block}
Watchlist candidates: {watchlist_block}

Respond with ONLY valid JSON:
{{"trades": [{{"ticker": "AAPL", "action": "buy", "dollar_amount": 5000, "conviction": 8, "reasoning": "short reason"}}]}}"""

def _quota_day_now():
    return datetime.now(pytz.utc).astimezone(_QUOTA_TZ).strftime("%Y-%m-%d")

def _default_model_state():
    return {"count": 0, "recent_calls": [], "exhausted": False}

def _default_tracker():
    return {
        "date": _quota_day_now(),
        "last_call": None,
        "models": {name: _default_model_state() for name in GEMINI_MODEL_FALLBACKS},
        "confirmed_limits": {},
    }

def _load_tracker():
    if not os.path.exists(_CALL_TRACKER_FILE):
        return _default_tracker()
    try:
        with open(_CALL_TRACKER_FILE) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return _default_tracker()

    if data.get("date") != _quota_day_now():
        fresh = _default_tracker()
        fresh["confirmed_limits"] = data.get("confirmed_limits", {})
        return fresh

    for name in GEMINI_MODEL_FALLBACKS:
        data.setdefault("models", {}).setdefault(name, _default_model_state())
    return data

def _save_tracker(data):
    os.makedirs(os.path.dirname(_CALL_TRACKER_FILE), exist_ok=True)
    with open(_CALL_TRACKER_FILE, "w") as f:
        json.dump(data, f)

def _effective_limits(tracker, model_name):
    configured = GEMINI_MODEL_LIMITS.get(model_name, {"rpd": 1500, "rpm": 15})
    learned = tracker.get("confirmed_limits", {}).get(model_name, {})
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
    state = tracker["models"].get(model_name, _default_model_state())
    if state.get("exhausted"):
        return 0
    limit = _effective_limits(tracker, model_name)["rpd"]
    return max(0, limit - state.get("count", 0))

def _has_rpm_room(tracker, model_name):
    state = tracker["models"].get(model_name, _default_model_state())
    recent = _prune_recent_calls(state.get("recent_calls", []))
    state["recent_calls"] = recent
    limit = _effective_limits(tracker, model_name)["rpm"]
    return len(recent) < limit

def _total_remaining(tracker):
    return sum(_remaining_rpd(tracker, name) for name in GEMINI_MODEL_FALLBACKS)

def _should_attempt_call(tracker):
    total_remaining = _total_remaining(tracker)
    if total_remaining <= 0:
        return False, "all configured models have exhausted their daily free-tier quota"
    if tracker.get("last_call") is None:
        return True, None
    try:
        last_call = datetime.fromisoformat(tracker["last_call"])
        if last_call.tzinfo is None:
            last_call = pytz.utc.localize(last_call)
    except (ValueError, TypeError):
        return True, None

    elapsed = (datetime.now(pytz.utc) - last_call).total_seconds()
    if elapsed < 30.0:  # 30-second RPM safety interval
        return False, f"spacing calls ({elapsed:.0f}s since last call, need 30s)"
    return True, None

def _generate_with_rotation(prompt, tracker):
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
            model_state = tracker["models"][model_name]
            model_state["count"] += 1
            model_state["recent_calls"].append(now_iso)
            tracker["last_call"] = now_iso
            tracker["date"] = _quota_day_now()
            return response
        except Exception as e:
            error_str = str(e)
            last_error = e
            if "PerDay" in error_str or "429" in error_str:
                tracker["models"][model_name]["exhausted"] = True
                print(f"Model '{model_name}' exhausted daily quota.")
            else:
                print(f"Model '{model_name}' call failed: {e}")
            continue

    raise last_error if last_error else RuntimeError("No Gemini models had remaining quota.")

def _technical_conviction_from_score(quant_score):
    if quant_score < MIN_SIGNAL_SCORE_TO_CONSIDER:
        return 0
    base = 5 + ((quant_score - MIN_SIGNAL_SCORE_TO_CONSIDER) / (100 - MIN_SIGNAL_SCORE_TO_CONSIDER)) * 4.5
    scaled = base * TECHNICAL_CONVICTION_AGGRESSIVENESS
    return min(9, max(1, round(scaled, 1)))

def get_technical_trade_decisions(scored_holdings, scored_watchlist, account_snapshot):
    holdings = account_snapshot.get("holdings", {})
    open_orders = get_tickers_with_open_orders()
    cooldowns = get_tickers_on_cooldown()
    unavailable = open_orders | cooldowns

    trades = []
    total_val = account_snapshot.get("total_value", 100000)

    for ticker, pos in holdings.items():
        if ticker in unavailable:
            continue
        info = scored_holdings.get(ticker, {})
        score = info.get("score", 0.0)

        if score < MIN_SIGNAL_SCORE_TO_CONSIDER:
            trades.append({
                "ticker": ticker,
                "action": "sell",
                "dollar_amount": 0,
                "conviction": max(1, int(10 - (score / 10))),
                "reasoning": f"technical score {score:.0f}/100 below threshold",
            })
        elif score >= 80:
            # Check if position is already near/over max allowed ceiling before proposing buy
            cur_val = pos["qty"] * pos["current_price"]
            max_allowed = total_val * MAX_POSITION_PCT
            if cur_val < max_allowed * 0.9:
                conviction = _technical_conviction_from_score(score)
                if conviction >= TECHNICAL_MIN_CONVICTION:
                    data = info.get("indicators") or {}
                    trades.append({
                        "ticker": ticker,
                        "action": "buy",
                        "dollar_amount": 0,
                        "conviction": int(conviction),
                        "reasoning": f"technical score {score:.0f}/100, adding to position",
                        "stop_loss": data.get(_SWING_LOW_KEY),
                        "take_profit": data.get(_SWING_HIGH_KEY),
                    })

    for ticker, info in scored_watchlist.items():
        if ticker in unavailable or ticker in holdings:
            continue
        score = info.get("score", 0.0)
        conviction = _technical_conviction_from_score(score)
        if conviction >= TECHNICAL_MIN_CONVICTION:
            data = info.get("indicators") or {}
            trades.append({
                "ticker": ticker,
                "action": "buy",
                "dollar_amount": 0,
                "conviction": int(conviction),
                "reasoning": f"technical score {score:.0f}/100",
                "stop_loss": data.get(_SWING_LOW_KEY),
                "take_profit": data.get(_SWING_HIGH_KEY),
            })

    meta = {
        "candidates_considered": len(scored_watchlist),
        "candidates_passed_prescreen": sum(1 for info in scored_watchlist.values() if info["score"] >= MIN_SIGNAL_SCORE_TO_CONSIDER),
        "throttled": False,
        "technical_fallback": True,
        "gemini_calls_today": 0,
    }
    return trades, meta

def _score_candidates(tickers):
    scored = {}
    for t in tickers:
        data = get_full_indicators(t)
        scored[t] = {"indicators": data, "score": calculate_signal_score(data)}
    return scored

def get_trade_decisions(candidates, account_snapshot, regime="NEUTRAL"):
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
    calls_today = sum(tracker["models"][m]["count"] for m in GEMINI_MODEL_FALLBACKS if m in tracker["models"])

    meta = {
        "candidates_considered": len(scored_news) + len(scored_watchlist),
        "candidates_passed_prescreen": sum(1 for info in list(scored_news.values()) + list(scored_watchlist.values()) if info["score"] >= MIN_SIGNAL_SCORE_TO_CONSIDER),
        "throttled": False,
        "technical_fallback": False,
        "gemini_calls_today": calls_today,
    }

    should_call, reason = _should_attempt_call(tracker)
    if not should_call:
        print(f"Skipping Gemini call: {reason}. Falling back to technical engine.")
        meta["throttled"] = True
        combined = {**scored_news, **scored_watchlist}
        trades, tech_meta = get_technical_trade_decisions(scored_holdings, combined, account_snapshot)
        meta.update(tech_meta)
        return trades, meta

    prompt = PROMPT_TEMPLATE.format(
        cash=cash,
        total_value=total_value,
        min_conviction=MIN_CONVICTION_TO_TRADE,
        holdings_block=str(list(holdings.keys())),
        news_block=str(list(scored_news.keys())),
        watchlist_block=str(list(scored_watchlist.keys())),
    )

    try:
        response = _generate_with_rotation(prompt, tracker)
        _save_tracker(tracker)
    except Exception as e:
        _save_tracker(tracker)
        print(f"Gemini call failed on all models: {e}. Falling back to technical engine.")
        combined = {**scored_news, **scored_watchlist}
        trades, tech_meta = get_technical_trade_decisions(scored_holdings, combined, account_snapshot)
        meta.update(tech_meta)
        return trades, meta

    raw_text = (response.text or "").strip()
    raw_text = re.sub(r"^```json\s*|\s*```$", "", raw_text)

    try:
        trades = json.loads(raw_text).get("trades", [])
    except json.JSONDecodeError:
        print("Warning: could not parse Gemini response as JSON:", raw_text)
        return [], meta

    filtered = []
    for t in trades:
        if t.get("ticker") in unavailable:
            continue
        if t.get("conviction", 0) < MIN_CONVICTION_TO_TRADE:
            continue
        filtered.append(t)
    return filtered, meta
