"""
Decision Engine: Handles Gemini LLM reviews with dynamic model discovery,
rotating fallback, and rate limit tracking. Includes pure-technical decision
engine fallback when LLM calls are unavailable.

IMPORTANT: Google renames/deprecates model IDs over time (confirmed by your
own 404 errors on gemini-1.5-flash / gemini-1.5-flash-8b). Rather than trust
a hardcoded list forever, this file queries the live ListModels endpoint once
per day, caches whatever text-capable models actually exist on YOUR account
right now, and uses that -- falling back to the static config list only if
the discovery call itself fails (e.g. no network).
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
    GEMINI_MODEL_FALLBACKS,
    GEMINI_MODEL_LIMITS,
    GEMINI_QUOTA_RESET_TIMEZONE,
    MIN_CONVICTION_TO_TRADE,
    WATCHLIST,
    SWING_LOOKBACK_DAYS,
    MIN_SIGNAL_SCORE_TO_CONSIDER,
    MAX_POSITION_PCT,
    TECHNICAL_MIN_CONVICTION,
    TECHNICAL_CONVICTION_AGGRESSIVENESS,
)
from trader import (
    get_full_indicators,
    get_tickers_with_open_orders,
    get_tickers_on_cooldown,
)
from signal_score import calculate_signal_score
from news import headline_sentiment

_client = genai.Client(api_key=GEMINI_API_KEY)

_CALL_TRACKER_FILE = os.path.join(
    os.path.dirname(__file__), "data", "gemini_call_tracker.json"
)
_QUOTA_TZ = pytz.timezone(GEMINI_QUOTA_RESET_TIMEZONE)

_SWING_LOW_KEY = f"recent_swing_low_{SWING_LOOKBACK_DAYS}d"
_SWING_HIGH_KEY = f"recent_swing_high_{SWING_LOOKBACK_DAYS}d"

_DEFAULT_LIMITS = {"rpd": 1500, "rpm": 15}

# Keywords that mean "not a text chat model we can use here"
_EXCLUDE_KEYWORDS = [
    "embed", "aqa", "vision", "image", "imagen", "tts", "audio",
    "video", "veo", "live", "ocr", "vision", "gemma",
]

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
                    "stop_loss": types.Schema(type=types.Type.NUMBER),
                    "take_profit": types.Schema(type=types.Type.NUMBER),
                },
                required=["ticker", "action", "conviction"],
            ),
        )
    },
    required=["trades"],
)

PROMPT_TEMPLATE = """You are a disciplined DAY TRADER with a DUAL focus for a SIMULATED paper-trading portfolio (no real money): you trade NEWS CATALYSTS (headlines with sentiment scores) AND CHART/TECHNICAL setups (trend, momentum, VWAP, opening-range breakouts). It is now {now_et} Eastern time, market open. Every trade idea MUST include a "conviction" score from 1-10. Ideas below conviction {min_conviction} will be discarded.

HARD CONSTRAINTS (enforced by code, but respect them anyway):
- CASH-ONLY: you may only recommend buys that fit within available cash. NEVER recommend margin purchases. The total of all buy dollar_amounts must stay well under the cash available.
- NEVER recommend buying when cash is negative or near zero.
- Prefer small, sized entries over large bets; a single position must stay a modest fraction of the portfolio.
- Every BUY should carry a stop_loss and take_profit price from the setup (opening-range high/low, VWAP, swing levels); if you have no opinion, omit them and the code will derive them from ATR/swings.
- Respect the daytrading window: no late-session entries, no chasing after big moves.

Current portfolio: - Cash available: ${cash:,.2f} - Total portfolio value: ${total_value:,.2f}
Existing holdings: {holdings_block}
News-driven candidates (score = quant technical score INCLUDING headline sentiment; news sentiment -1..+1; opening-range = above/below/inside today's first 15-min range):
{news_block}
Watchlist candidates (score 0-100; intraday = % move vs session open; opening-range = above/below/inside):
{watchlist_block}

Respond with ONLY valid JSON:
{{"trades": [{{"ticker": "AAPL", "action": "buy", "dollar_amount": 5000, "conviction": 8, "stop_loss": 210.5, "take_profit": 224.0, "reasoning": "short reason"}}]}}"""


# ============================================================
# Dynamic model discovery -- self-heals when Google renames models
# ============================================================
def _rank_model_name(name):
    n = name.lower()
    if "lite" in n:
        return 0
    if "flash" in n and "pro" not in n:
        return 1
    if "pro" in n:
        return 3
    return 2


def _discover_live_models():
    """
    Queries Google's own ListModels endpoint to find whatever text-capable
    models actually exist on this account RIGHT NOW. Returns a ranked list
    of short model names (e.g. "gemini-2.0-flash"), or None if the call
    itself failed (e.g. no network) -- callers should fall back to the
    static config list in that case only.
    """
    try:
        models_iter = _client.models.list()
    except Exception as e:
        print(f"Could not list live Gemini models (using static fallback list): {e}")
        return None

    candidates = []
    try:
        for m in models_iter:
            name = getattr(m, "name", None) or ""
            short = name.split("/")[-1] if "/" in name else name
            if not short:
                continue
            lname = short.lower()
            if any(bad in lname for bad in _EXCLUDE_KEYWORDS):
                continue
            # Only interested in gemini text models
            if "gemini" not in lname:
                continue
            candidates.append(short)
    except Exception as e:
        print(f"Error while iterating live Gemini model list (using static fallback list): {e}")
        return None

    if not candidates:
        return None

    candidates = sorted(set(candidates), key=_rank_model_name)
    return candidates


def _get_effective_model_list(tracker):
    """
    Returns the fallback list to actually use this run: live-discovered
    models (refreshed once per calendar day and cached in the tracker file)
    minus any models already proven invalid today, or the static config
    list if discovery has never succeeded.
    """
    today = _quota_day_now()
    if tracker.get("discovered_date") != today or not tracker.get("discovered_models"):
        discovered = _discover_live_models()
        tracker["discovered_date"] = today
        tracker["discovered_models"] = discovered  # may be None

    invalid = set(tracker.get("invalid_models", []))
    live_list = tracker.get("discovered_models")

    if live_list:
        effective = [m for m in live_list if m not in invalid]
        if effective:
            return effective
    # Fall back to static config list (also filtered for known-invalid today)
    return [m for m in GEMINI_MODEL_FALLBACKS if m not in invalid]


# ============================================================
# Per-model quota tracking
# ============================================================
def _quota_day_now():
    return datetime.now(pytz.utc).astimezone(_QUOTA_TZ).strftime("%Y-%m-%d")


def _default_model_state():
    return {"count": 0, "recent_calls": [], "exhausted": False}


def _default_tracker():
    return {
        "date": _quota_day_now(),
        "last_call": None,
        "models": {},
        "confirmed_limits": {},
        "discovered_date": None,
        "discovered_models": None,
        "invalid_models": [],
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
        # discovered_models/invalid_models intentionally reset daily --
        # yesterday's "invalid" model might be valid again after Google's
        # own daily refresh, and yesterday's discovery is stale anyway.
        return fresh

    data.setdefault("models", {})
    data.setdefault("confirmed_limits", {})
    data.setdefault("discovered_date", None)
    data.setdefault("discovered_models", None)
    data.setdefault("invalid_models", [])
    return data


def _save_tracker(data):
    os.makedirs(os.path.dirname(_CALL_TRACKER_FILE), exist_ok=True)
    tmp = _CALL_TRACKER_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, _CALL_TRACKER_FILE)


def _effective_limits(tracker, model_name):
    configured = GEMINI_MODEL_LIMITS.get(model_name, _DEFAULT_LIMITS)
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


def _get_model_state(tracker, model_name):
    return tracker["models"].setdefault(model_name, _default_model_state())


def _remaining_rpd(tracker, model_name):
    state = _get_model_state(tracker, model_name)
    if state.get("exhausted"):
        return 0
    limit = _effective_limits(tracker, model_name)["rpd"]
    return max(0, limit - state.get("count", 0))


def _has_rpm_room(tracker, model_name):
    state = _get_model_state(tracker, model_name)
    state["recent_calls"] = _prune_recent_calls(state.get("recent_calls", []))
    limit = _effective_limits(tracker, model_name)["rpm"]
    return len(state["recent_calls"]) < limit


def _total_remaining(tracker, model_list):
    return sum(_remaining_rpd(tracker, name) for name in model_list)


def _is_daily_quota_error(error_str):
    return "PerDay" in error_str or "GenerateRequestsPerDayPerProjectPerModel" in error_str or "RESOURCE_EXHAUSTED" in error_str


def _is_model_not_found_error(error_str):
    return "404" in error_str or "NOT_FOUND" in error_str


def _extract_quota_value(error_str, marker):
    pattern = rf'["\']quotaId["\']\s*:\s*["\'][^"\']*{marker}[^"\']*["\'].*?["\']quotaValue["\']\s*:\s*["\'](\d+)["\']'
    m = re.search(pattern, error_str, re.DOTALL)
    return int(m.group(1)) if m else None


def _should_attempt_call(tracker, model_list):
    total_remaining = _total_remaining(tracker, model_list)
    if total_remaining <= 0:
        return False, "all available models have exhausted their daily free-tier quota"
    if tracker.get("last_call") is None:
        return True, None
    try:
        last_call = datetime.fromisoformat(tracker["last_call"])
        if last_call.tzinfo is None:
            last_call = pytz.utc.localize(last_call)
    except (ValueError, TypeError):
        return True, None

    elapsed = (datetime.now(pytz.utc) - last_call).total_seconds()
    if elapsed < 20.0:  # simple RPM safety floor
        return False, f"spacing calls ({elapsed:.0f}s since last call, need 20s minimum)"
    return True, None


def _generate_with_rotation(prompt, tracker, model_list):
    last_error = None
    for model_name in model_list:
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
            state = _get_model_state(tracker, model_name)
            state["count"] += 1
            state["recent_calls"].append(now_iso)
            tracker["last_call"] = now_iso
            tracker["date"] = _quota_day_now()
            return response
        except Exception as e:
            error_str = str(e)
            last_error = e

            if _is_model_not_found_error(error_str):
                # Not a quota problem -- this model ID simply doesn't exist
                # (renamed/deprecated by Google). Remove it from today's
                # rotation entirely so we don't waste time retrying it.
                tracker.setdefault("invalid_models", [])
                if model_name not in tracker["invalid_models"]:
                    tracker["invalid_models"].append(model_name)
                print(f"Model '{model_name}' not found/unsupported for this API version -- "
                      f"removing from today's rotation.")
                continue

            real_rpd = _extract_quota_value(error_str, "PerDay")
            real_rpm = _extract_quota_value(error_str, "PerMinute")
            if real_rpd is not None or real_rpm is not None:
                learned = tracker["confirmed_limits"].setdefault(model_name, {})
                if real_rpd is not None:
                    learned["rpd"] = real_rpd
                if real_rpm is not None:
                    learned["rpm"] = real_rpm

            if _is_daily_quota_error(error_str):
                _get_model_state(tracker, model_name)["exhausted"] = True
                print(f"Model '{model_name}' exhausted its daily quota.")
            else:
                print(f"Model '{model_name}' call failed (will retry next run): {e}")
            continue

    raise last_error if last_error else RuntimeError("No Gemini models had remaining quota.")


# ============================================================
# Pure-technical fallback (when Gemini is throttled/unavailable)
# ============================================================
def _technical_conviction_from_score(quant_score):
    """
    Maps quant signal_score (0-100) to a conviction level for pure-technical
    trading. FIXED: the pass-threshold score (MIN_SIGNAL_SCORE_TO_CONSIDER)
    now ALWAYS maps to at least TECHNICAL_MIN_CONVICTION -- previously the
    aggressiveness multiplier was applied to the whole value including its
    floor, which could push a genuinely-qualifying candidate's conviction
    below the very gate it needed to clear, silently producing zero trades.
    Aggressiveness now only scales the BONUS above that guaranteed floor.
    """
    if quant_score < MIN_SIGNAL_SCORE_TO_CONSIDER:
        return 0
    span_score = max(1.0, 100 - MIN_SIGNAL_SCORE_TO_CONSIDER)
    span_conviction = max(0.0, 9 - TECHNICAL_MIN_CONVICTION)
    bonus = ((quant_score - MIN_SIGNAL_SCORE_TO_CONSIDER) / span_score) * span_conviction
    bonus *= TECHNICAL_CONVICTION_AGGRESSIVENESS
    conviction = TECHNICAL_MIN_CONVICTION + bonus
    return round(min(9, max(TECHNICAL_MIN_CONVICTION, conviction)), 1)


def get_technical_trade_decisions(scored_holdings, scored_watchlist, account_snapshot):
    holdings = account_snapshot.get("holdings", {})
    open_orders = get_tickers_with_open_orders()
    cooldowns = get_tickers_on_cooldown()
    unavailable = open_orders | cooldowns
    total_val = account_snapshot.get("total_value", 100000)

    trades = []

    for ticker, pos in holdings.items():
        if ticker in unavailable:
            continue
        info = scored_holdings.get(ticker, {})
        score = info.get("score", 0.0)

        if score < MIN_SIGNAL_SCORE_TO_CONSIDER:
            conviction = max(1, int(10 - (score / 10)))
            trades.append({
                "ticker": ticker,
                "action": "sell",
                "dollar_amount": 0,
                "conviction": conviction,
                "reasoning": f"technical score {score:.0f}/100 below threshold ({MIN_SIGNAL_SCORE_TO_CONSIDER}), exiting to consolidate.",
            })
        elif score >= 80:
            current_val = pos["qty"] * pos["current_price"]
            max_allowed = total_val * MAX_POSITION_PCT
            if current_val < max_allowed * 0.9:
                conviction = _technical_conviction_from_score(score)
                if conviction >= TECHNICAL_MIN_CONVICTION:
                    data = info.get("indicators") or {}
                    or_status = data.get("opening_range_status")
                    conviction = min(9, conviction + (1 if or_status == "above" else 0))
                    trades.append({
                        "ticker": ticker,
                        "action": "buy",
                        "dollar_amount": 0,
                        "conviction": int(conviction),
                        "reasoning": f"technical score {score:.0f}/100, strong setup, adding to position"
                                     + (f" on opening-range breakout ({or_status})." if or_status else "."),
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
            or_status = data.get("opening_range_status")
            conviction = min(9, conviction + (1 if or_status == "above" else 0))
            trades.append({
                "ticker": ticker,
                "action": "buy",
                "dollar_amount": 0,
                "conviction": int(conviction),
                "reasoning": f"technical score {score:.0f}/100, strong signal on technicals alone"
                             + (f" with opening-range breakout ({or_status})." if or_status else "."),
                "stop_loss": data.get(_SWING_LOW_KEY),
                "take_profit": data.get(_SWING_HIGH_KEY),
            })

    meta = {
        "candidates_considered": len(scored_watchlist),
        "candidates_passed_prescreen": sum(
            1 for info in scored_watchlist.values() if info["score"] >= MIN_SIGNAL_SCORE_TO_CONSIDER
        ),
        "throttled": False,
        "technical_fallback": True,
        "gemini_calls_today": 0,
    }
    return trades, meta


def _score_candidates(tickers, sentiment=None):
    """
    Scores tickers on technicals PLUS an optional news-sentiment boost (the
    dual-focus half of the bot). sentiment: {ticker: -1..+1}.
    """
    scored = {}
    for t in tickers:
        data = get_full_indicators(t)
        score = calculate_signal_score(data, news_sentiment=(sentiment or {}).get(t, 0.0))
        scored[t] = {"indicators": data, "score": score}
    return scored


def _fmt_holdings_block(scored_holdings):
    parts = []
    for t, info in scored_holdings.items():
        d = info.get("indicators") or {}
        parts.append(
            f"{t} (score {info['score']:.0f}/100, trend {d.get('trend')}, "
            f"intraday {d.get('intraday_momentum_pct')}% vs open, "
            f"opening-range {d.get('opening_range_status')})"
        )
    return "; ".join(parts) or "none"


def _fmt_news_block(candidates, scored_news, sentiment):
    parts = []
    for t, info in scored_news.items():
        articles = candidates.get(t, [])
        headline = (articles[0].get("headline", "") if articles else "")[:140]
        d = info.get("indicators") or {}
        parts.append(
            f"{t} (score {info['score']:.0f}/100, news sentiment {sentiment.get(t, 0.0):+.2f}, "
            f"opening-range {d.get('opening_range_status')}): \"{headline}\""
        )
    return "; ".join(parts) or "none"


def _fmt_watchlist_block(scored_watchlist):
    parts = []
    for t, info in scored_watchlist.items():
        d = info.get("indicators") or {}
        parts.append(
            f"{t} (score {info['score']:.0f}/100, trend {d.get('trend')}, "
            f"intraday {d.get('intraday_momentum_pct')}% vs open, "
            f"opening-range {d.get('opening_range_status')})"
        )
    return "; ".join(parts) or "none"


def get_trade_decisions(candidates, account_snapshot, regime="NEUTRAL"):
    holdings = account_snapshot.get("holdings", {})
    cash = account_snapshot.get("cash", 0)
    total_value = account_snapshot.get("total_value", cash)

    open_orders = get_tickers_with_open_orders()
    cooldowns = get_tickers_on_cooldown()
    unavailable = open_orders | cooldowns

    new_candidates = {t: a for t, a in candidates.items() if t not in holdings}
    watchlist_tickers = [t for t in WATCHLIST if t not in holdings and t not in new_candidates]

    # Dual focus: average headline sentiment per news candidate feeds the
    # quant score so a real catalyst moves the needle alongside the chart.
    news_sentiment = {}
    for t, articles in new_candidates.items():
        if articles:
            news_sentiment[t] = sum(headline_sentiment(a) for a in articles) / len(articles)

    scored_holdings = _score_candidates(list(holdings.keys()))
    scored_news = _score_candidates(list(new_candidates.keys()), sentiment=news_sentiment)
    scored_watchlist = _score_candidates(watchlist_tickers)

    tracker = _load_tracker()
    model_list = _get_effective_model_list(tracker)
    calls_today = sum(tracker["models"].get(m, {}).get("count", 0) for m in model_list)

    meta = {
        "candidates_considered": len(scored_news) + len(scored_watchlist),
        "candidates_passed_prescreen": sum(
            1 for info in list(scored_news.values()) + list(scored_watchlist.values())
            if info["score"] >= MIN_SIGNAL_SCORE_TO_CONSIDER
        ),
        "throttled": False,
        "technical_fallback": False,
        "gemini_calls_today": calls_today,
    }

    should_call, reason = _should_attempt_call(tracker, model_list)
    if not should_call:
        print(f"Skipping Gemini call this run: {reason}")
        print("Falling back to pure-technical decision engine instead.")
        _save_tracker(tracker)
        meta["throttled"] = True
        combined = {**scored_news, **scored_watchlist}
        trades, tech_meta = get_technical_trade_decisions(scored_holdings, combined, account_snapshot)
        meta.update(tech_meta)
        meta["gemini_calls_today"] = calls_today  # preserve real count
        return trades, meta

    now_et = datetime.now(pytz.timezone("America/New_York")).strftime("%Y-%m-%d %I:%M %p %Z")
    prompt = PROMPT_TEMPLATE.format(
        cash=cash,
        total_value=total_value,
        min_conviction=MIN_CONVICTION_TO_TRADE,
        now_et=now_et,
        holdings_block=_fmt_holdings_block(scored_holdings),
        news_block=_fmt_news_block(new_candidates, scored_news, news_sentiment),
        watchlist_block=_fmt_watchlist_block(scored_watchlist),
    )

    try:
        response = _generate_with_rotation(prompt, tracker, model_list)
        _save_tracker(tracker)
        meta["gemini_calls_today"] = sum(tracker["models"].get(m, {}).get("count", 0) for m in model_list)
    except Exception as e:
        _save_tracker(tracker)
        print(f"Gemini call failed on all models with remaining quota: {e}")
        print("Falling back to pure-technical decision engine instead.")
        combined = {**scored_news, **scored_watchlist}
        trades, tech_meta = get_technical_trade_decisions(scored_holdings, combined, account_snapshot)
        meta.update(tech_meta)
        meta["gemini_calls_today"] = calls_today
        return trades, meta

    raw_text = (response.text or "").strip()
    raw_text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text, flags=re.MULTILINE)

    try:
        trades = json.loads(raw_text).get("trades", [])
    except json.JSONDecodeError:
        print("Warning: could not parse Gemini response as JSON:")
        print(raw_text)
        return [], meta

    filtered = []
    for t in trades:
        if t.get("ticker") in unavailable:
            continue
        if t.get("conviction", 0) < MIN_CONVICTION_TO_TRADE:
            continue
        filtered.append(t)
    return filtered, meta
