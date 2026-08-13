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
import time
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
    NEWS_CONFLUENCE_MIN_TECH_SCORE,
    CONFIDENCE_MIN_TO_TRADE,
    UNIVERSE_SCAN_PER_RUN,
    ENABLE_ECONOMIC_CALENDAR,
    ENABLE_ANALYST_ACTIONS,
    ENABLE_INSIDER_ACTIVITY,
    ENABLE_SEC_FILINGS,
    ENABLE_REDDIT_SENTIMENT,
    MAX_FUNDAMENTAL_TICKERS,
)
from trader import (
    get_full_indicators,
    get_tickers_with_open_orders,
    get_tickers_on_cooldown,
    build_performance_brief,
)
from signal_score import calculate_signal_score
from news import headline_sentiment

# Bound each Gemini attempt with an explicit HTTP timeout so a slow/hung
# Google response can't stall a run indefinitely. CRITICAL: the genai SDK's
# HttpOptions.timeout is in MILLISECONDS (verified in types.py -- "Timeout
# for the request in milliseconds"), so 20_000 = 20 seconds. The previous
# value (60) was interpreted as 60ms = 0.06s, which made EVERY Gemini call
# fail instantly with 'Read timed out. (read timeout=0.06)'. 20s gives the
# now-larger prompt (more candidates + more fields) a comfortable per-attempt
# window, and _generate_with_rotation additionally stops the whole fallback
# rotation once the run's time budget is spent -- so the worst case is the
# rotation budget (~45s), never 20s x every model.
_client = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options=types.HttpOptions(timeout=20_000),
)

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
                    "confidence": types.Schema(type=types.Type.NUMBER),
                    "conviction": types.Schema(type=types.Type.INTEGER),
                    "reasoning": types.Schema(type=types.Type.STRING),
                    "stop_loss": types.Schema(type=types.Type.NUMBER),
                    "take_profit": types.Schema(type=types.Type.NUMBER),
                },
                required=["ticker", "action"],
            ),
        )
    },
    required=["trades"],
)

PROMPT_TEMPLATE = """You are a disciplined news+technical trader with a DUAL focus for a SIMULATED paper-trading portfolio (no real money): you trade NEWS CATALYSTS (headlines scored 0-10 with sentiment -1..+1) AND CHART/TECHNICAL setups (trend, RSI, ATR, ADX, MACD crossover, support/resistance, gap %, 52-week position, VWAP, opening-range breakouts). You trade short-term AND longer-term: daytrades and multi-day holds are both fine. It is now {now_et} Eastern time, session: {session}.

Every trade idea MUST include a "confidence" score from 0-100 -- your honest probability-weighted conviction in this setup. The code converts it to size: 90+ -> 8% of equity, 80+ -> 5%, 70+ -> 3%, 60+ -> 2%, below {conf_min} -> skipped. A raw "dollar_amount" is optional and IGNORED whenever confidence is present. Also include a "conviction" 1-10 as a secondary gate.

HARD CONSTRAINTS (enforced by code, but respect them anyway):
- CASH-ONLY: never recommend buys that need margin; total size stays well under available cash.
- NEVER recommend buying when cash is negative or near zero.
- Be selective: only the best 2-4 ideas per run. No filler trades just because a name is on screen.
- Every BUY should carry a stop_loss and take_profit price from the setup (opening-range high/low, VWAP, swing levels); if you have no opinion, omit them and the code will derive them from ATR/swings.
- Respect the session: in the regular session avoid chasing big moves and late-session entries. In the EXTENDED session (thin liquidity, wide spreads) only take high-confidence setups and expect fills near the last price. Positions may be HELD OVERNIGHT if the setup is still intact -- your stop_loss/take_profit (plus the code's trailing stop and hard loss cap) protect them around the clock, so longer-term winners are encouraged.

Current portfolio: - Cash available: ${cash:,.2f} - Total portfolio value: ${total_value:,.2f}
Existing holdings: {holdings_block}
News-driven candidates (score = quant technical score INCLUDING headline sentiment; article score 0-10 = importance; sentiment -1..+1):
{news_block}
Watchlist candidates (score 0-100; RSI; ATR; MACD cross; gap %; 52w position; S/R; opening-range):
{watchlist_block}

Overnight-queued trades (proposed while the market was closed; RE-VERIFY each against the FRESH data above -- re-emit only the ones still valid, with updated stop_loss/take_profit; omit anything whose setup broke, gapped against you, or you no longer want -- it will be dropped):
{pending_block}

Phase 2 fundamental context (analyst actions, insider activity, Reddit sentiment, SEC filings, economic calendar):
{fundamental_block}

What actually WORKS so far (learned from the closed-trade journal -- favor the WORKING setups, avoid LOSING ones):
{performance_brief}

Respond with ONLY valid JSON:
{{"trades": [{{"ticker": "AAPL", "action": "buy", "confidence": 82, "conviction": 8, "stop_loss": 210.5, "take_profit": 224.0, "reasoning": "short reason"}}]}}"""


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


def _generate_with_rotation(prompt, tracker, model_list, schema=None, time_budget=None):
    """
    Try models in order until one responds or all are exhausted. When
    time_budget (wall-clock seconds) is given, the whole rotation stops as
    soon as it is spent -- a slow/hung Google response can therefore never
    push a run past its deadline; the caller falls back to the technical
    engine. (morning_prep.py calls without a budget -- it runs once a day
    and is allowed more time.)
    """
    last_error = None
    deadline = (time.monotonic() + time_budget) if time_budget is not None else None
    for model_name in model_list:
        if deadline is not None and time.monotonic() >= deadline:
            break  # run budget spent -- stop trying, raise last_error below
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
                    response_schema=schema or _RESPONSE_SCHEMA,
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

    if last_error is None:
        last_error = RuntimeError("No Gemini models had remaining quota or the time budget was exhausted.")
    raise last_error


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


def _score_candidates(tickers, sentiment=None, extras=None, time_budget=None):
    """
    Scores tickers on technicals PLUS an optional news-sentiment boost (the
    dual-focus half of the bot) PLUS Phase 2 fundamental signals (analyst
    actions, insider activity, Reddit sentiment, earnings proximity, SEC
    filings). sentiment: {ticker: -1..+1}; extras: {ticker: signal-dict}
    from data_feeds.get_fundamental_signals(). time_budget (wall-clock
    seconds) stops the loop once spent -- each get_full_indicators call can
    take multiple seconds, so without this the holdings/news scoring could
    blow a run's deadline on its own.
    """
    scored = {}
    deadline = (time.monotonic() + time_budget) if time_budget is not None else None
    for t in tickers:
        if deadline is not None and time.monotonic() >= deadline:
            break
        data = get_full_indicators(t)
        score = calculate_signal_score(
            data,
            news_sentiment=(sentiment or {}).get(t, 0.0),
            extras=(extras or {}).get(t),
        )
        scored[t] = {"indicators": data, "score": score}
    return scored


_SCAN_CACHE_FILE = os.path.join(os.path.dirname(__file__), "data", "scan_cache.json")

# Bounds for the rotating-scan cache miss. The FIRST run of each hour re-scans
# from scratch (cache is keyed by hour); without these caps, a slow Alpaca
# minute -- each data call can take up to the SDK's ~5s -- multiplied by 20+
# tickers is exactly the intermittent 2+ minute run. Now at most
# MAX_FRESH_SCANS_PER_RUN tickers and SCAN_REFRESH_BUDGET_SECONDS of wall-clock
# time are spent per run; anything not scored this run gets scored by the next
# run within the same hour, so the cache still fills in a few runs.
SCAN_REFRESH_BUDGET_SECONDS = float(os.environ.get("SCAN_REFRESH_BUDGET_SECONDS", 40))
MAX_FRESH_SCANS_PER_RUN = int(os.environ.get("MAX_FRESH_SCANS_PER_RUN", 12))


def _load_scan_cache():
    try:
        with open(_SCAN_CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_scan_cache(cache):
    try:
        tmp = _SCAN_CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f)
        os.replace(tmp, _SCAN_CACHE_FILE)
    except Exception:
        pass


def _score_candidates_cached(tickers, time_budget=None, **kwargs):
    """
    Hour-scoped cache wrapper around _score_candidates for the ROTATING
    universe scan. The scan slice is deterministic per calendar hour, so the
    same ~20-40 tickers were being FULLY re-scored (price history fetch +
    indicator math + intraday momentum, ~2-3 API calls each) on every 2-min
    run -- ~30 redundant full scans per hour, which is what pushed runs from
    20-50s to 2+ minutes. Each ticker is now computed at most once per hour;
    every other run reuses the cached score (pure disk read). News-matched
    candidates and holdings are still scored fresh -- they are small sets
    and time-sensitive. Returns {ticker: {"indicators", "score"}}.
    """
    if not tickers:
        return {}
    cache = _load_scan_cache()
    hour = datetime.now(pytz.timezone("America/New_York")).strftime("%Y%m%d%H")
    today = hour[:8]
    # drop entries from previous days so the cache stays bounded
    cache = {k: v for k, v in cache.items() if k.split("|")[-1].startswith(today)}
    merged = {}
    need = []
    for t in tickers:
        entry = cache.get(f"{t}|{hour}")
        if entry is not None and isinstance(entry, dict):
            merged[t] = entry
        else:
            need.append(t)
    if need:
        fresh = {}
        budget = SCAN_REFRESH_BUDGET_SECONDS
        if time_budget is not None:
            budget = min(budget, max(0.0, time_budget))
        deadline = time.monotonic() + budget
        for t in need:
            if len(fresh) >= MAX_FRESH_SCANS_PER_RUN or time.monotonic() >= deadline:
                break
            try:
                data = get_full_indicators(t)
                score = calculate_signal_score(
                    data,
                    news_sentiment=(kwargs.get("sentiment") or {}).get(t, 0.0),
                    extras=(kwargs.get("extras") or {}).get(t),
                )
                fresh[t] = {"indicators": data, "score": score}
            except Exception:
                continue
        for t, info in fresh.items():
            cache[f"{t}|{hour}"] = info
        if fresh:
            _save_scan_cache(cache)
        merged.update(fresh)
    return merged


# Hard wall-clock budget for the per-run data-feeds refresh. On a warm cache
# this is nearly free; on a cold cache (first run after a fresh checkout) the
# per-ticker insider/SEC loops could otherwise burn 40+ sequential HTTP calls
# (~2-4 min). The budget guarantees feeds can never dominate a run: whatever
# didn't get refreshed this run simply falls back to the TTL cache from the
# last successful refresh (all feeds are fail-soft by design).
FEED_REFRESH_BUDGET_SECONDS = float(
    os.environ.get("FEED_REFRESH_BUDGET_SECONDS", 30)
)


def _refresh_data_feeds(candidate_tickers, time_budget=None):
    """
    Phase 2: populate the cached market-wide feeds (economic calendar,
    analyst actions, Reddit) and per-ticker feeds (insider, SEC) for the
    current candidate set. Everything is TTL-cached and fail-soft, so this
    is cheap on the hot loop and never breaks a run. Config flags gate each
    feed (each defaults to enabled). A hard wall-clock budget bounds the
    whole refresh so a cold cache can never stall a run; time_budget (from
    get_trade_decisions) further shrinks that budget to whatever the run
    has left.
    """
    from data_feeds import (
        fetch_economic_calendar,
        fetch_analyst_actions,
        get_insider_activity,
        get_sec_filings,
        get_reddit_sentiment,
    )
    budget = FEED_REFRESH_BUDGET_SECONDS
    if time_budget is not None:
        budget = min(budget, max(0.0, time_budget))
    deadline = time.monotonic() + budget
    if ENABLE_ECONOMIC_CALENDAR and time.monotonic() < deadline:
        try:
            fetch_economic_calendar()
        except Exception as e:
            print(f"Economic calendar refresh failed: {e}")
    if ENABLE_ANALYST_ACTIONS and time.monotonic() < deadline:
        try:
            fetch_analyst_actions()
        except Exception as e:
            print(f"Analyst actions refresh failed: {e}")
    if ENABLE_REDDIT_SENTIMENT and time.monotonic() < deadline:
        try:
            get_reddit_sentiment()
        except Exception as e:
            print(f"Reddit sentiment refresh failed: {e}")
    if ENABLE_INSIDER_ACTIVITY and time.monotonic() < deadline:
        try:
            get_insider_activity(
                candidate_tickers[:MAX_FUNDAMENTAL_TICKERS],
                time_budget=max(0.0, deadline - time.monotonic()),
            )
        except Exception as e:
            print(f"Insider activity refresh failed: {e}")
    if ENABLE_SEC_FILINGS and time.monotonic() < deadline:
        try:
            get_sec_filings(
                candidate_tickers[:MAX_FUNDAMENTAL_TICKERS],
                time_budget=max(0.0, deadline - time.monotonic()),
            )
        except Exception as e:
            print(f"SEC filings refresh failed: {e}")


def _build_fundamental_block(tickers):
    """Phase 2 context block for the Gemini prompt (pure cache reads)."""
    from data_feeds import get_context_block
    try:
        return get_context_block(tickers)
    except Exception as e:
        print(f"Could not build fundamental context: {e}")
        return "none"


def _fmt_setup(d):
    """
    Compact, information-dense formatting of a ticker's key setup fields.
    Everything shown here is ALREADY computed by get_full_indicators -- this
    just surfaces it to Gemini (free, zero extra API calls).
    """
    d = d or {}
    parts = []
    parts.append(f"trend {d.get('trend')}")
    if d.get("price") is not None:
        parts.append(f"${d['price']:.2f}")
    if d.get("rsi_14") is not None:
        parts.append(f"RSI {d['rsi_14']:.0f}")
    stoch = d.get("stochastic") or {}
    if stoch.get("percent_k") is not None:
        parts.append(f"stochK {stoch['percent_k']:.0f}")
    if stoch.get("percent_d") is not None:
        parts.append(f"stochD {stoch['percent_d']:.0f}")
    if d.get("atr_14") is not None:
        parts.append(f"ATR {d['atr_14']:.2f}")
    if d.get("adx_14") is not None:
        parts.append(f"ADX {d['adx_14']:.0f}")
    if d.get("sma_20") and d.get("price"):
        parts.append(f"vs20 {((d['price'] / d['sma_20']) - 1) * 100:+.1f}%")
    if d.get("sma_50") and d.get("price"):
        parts.append(f"vs50 {((d['price'] / d['sma_50']) - 1) * 100:+.1f}%")
    if d.get("macd_cross"):
        parts.append(f"MACD {d['macd_cross']}")
    macd = d.get("macd") or {}
    if macd.get("macd") is not None:
        parts.append(f"macd {macd['macd']:.3f}")
    if macd.get("signal") is not None:
        parts.append(f"macdSig {macd['signal']:.3f}")
    if macd.get("histogram") is not None:
        parts.append(f"macd_hist {macd['histogram']:.3f}")
    bb = d.get("bollinger") or {}
    if bb.get("percent_b") is not None:
        parts.append(f"%B {bb['percent_b']:.2f}")
    if bb.get("bandwidth") is not None:
        parts.append(f"BBwidth {bb['bandwidth']:.3f}")
    if d.get("momentum_10d") is not None:
        parts.append(f"mom10 {d['momentum_10d']:+.1f}%")
    if d.get("volatility_20d") is not None:
        parts.append(f"vol20 {d['volatility_20d']:.0f}%")
    if d.get("relative_volume_pct") is not None:
        parts.append(f"relVol {d['relative_volume_pct']:+.0f}%")
    if d.get("volume_trend"):
        parts.append(f"volTrend {d['volume_trend']}")
    if d.get("gap_pct") is not None:
        parts.append(f"gap {d['gap_pct']:+.1f}%")
    if d.get("vwap") is not None:
        parts.append(f"VWAP ${d['vwap']:.2f}")
    if d.get("vwap_deviation_pct") is not None:
        parts.append(f"vsVWAP {d['vwap_deviation_pct']:+.1f}%")
    if d.get("session_open") is not None:
        parts.append(f"open ${d['session_open']:.2f}")
    if d.get("intraday_rsi") is not None:
        parts.append(f"iRSI {d['intraday_rsi']:.0f}")
    if d.get("intraday_trend"):
        parts.append(f"iTrend {d['intraday_trend']}")
    if d.get("intraday_momentum_pct") is not None:
        parts.append(f"{d['intraday_momentum_pct']:+.1f}% vs open")
    if d.get("opening_range_status"):
        parts.append(f"OR {d['opening_range_status']}")
    if d.get("opening_range_high") is not None:
        parts.append(f"ORH ${d['opening_range_high']:.2f}")
    if d.get("opening_range_low") is not None:
        parts.append(f"ORL ${d['opening_range_low']:.2f}")
    if d.get("dist_from_52w_high_pct") is not None:
        parts.append(f"{d['dist_from_52w_high_pct']:.0f}% off 52wH")
    if d.get("dist_from_52w_low_pct") is not None:
        parts.append(f"{d['dist_from_52w_low_pct']:.0f}% off 52wL")
    if d.get("high_52w") is not None:
        parts.append(f"52wH ${d['high_52w']:.2f}")
    if d.get("low_52w") is not None:
        parts.append(f"52wL ${d['low_52w']:.2f}")
    if d.get("days_until_earnings") is not None:
        parts.append(f"earnings in {d['days_until_earnings']}d")
    if d.get("support") is not None:
        parts.append(f"sup {d['support']:.2f}")
    if d.get("resistance") is not None:
        parts.append(f"res {d['resistance']:.2f}")
    return " | ".join(parts)


def _apply_news_confluence(new_candidates, scored_news, news_sentiment):
    """
    Headline alone is not a setup: drop news candidates whose PURE technical
    score (before the sentiment boost) is below NEWS_CONFLUENCE_MIN_TECH_SCORE,
    so the chart has to agree with the story at least somewhat. Returns
    filtered copies of (new_candidates, scored_news, news_sentiment).
    Disabled when the config value is <= 0.
    """
    if NEWS_CONFLUENCE_MIN_TECH_SCORE <= 0:
        return new_candidates, scored_news, news_sentiment
    keep = []
    for t, info in scored_news.items():
        tech_only = calculate_signal_score(info.get("indicators"), news_sentiment=0.0)
        if tech_only >= NEWS_CONFLUENCE_MIN_TECH_SCORE:
            keep.append(t)
    return (
        {t: new_candidates[t] for t in keep},
        {t: scored_news[t] for t in keep},
        {t: news_sentiment[t] for t in keep if t in news_sentiment},
    )


def _fmt_fundamental(t, fundamental):
    """
    Compact per-ticker Phase 2 tags for the Gemini prompt (street consensus,
    analyst actions, insider flow, reddit sentiment, sector, earnings, SEC
    filings). Pure cache reads -- zero network cost. Empty string when the
    ticker has no fundamental data.
    """
    f = (fundamental or {}).get(t) or {}
    bits = []
    ac = f.get("analyst_consensus") or {}
    if ac.get("buy") is not None:
        bits.append(f"street {ac['buy']}B/{ac.get('hold') or 0}H/{ac.get('sell') or 0}S")
        if ac.get("target_mean"):
            bits.append(f"PT ${ac['target_mean']:.2f}")
    if f.get("analyst"):
        bits.append(f"analyst {f['analyst']}")
    if f.get("insider_net"):
        bits.append(f"insider ${f['insider_net']:,.0f}")
    rs = f.get("reddit_sentiment")
    if rs is not None and abs(rs) >= 0.15:
        bits.append(f"reddit {rs:+.2f}")
    if f.get("sector"):
        bits.append(f["sector"])
    de = f.get("days_until_earnings")
    if de is not None and 0 <= de <= 14:
        bits.append(f"earnings {de}d")
    if f.get("recent_filings"):
        bits.append("filed " + ",".join(str(x) for x in f["recent_filings"][:4]))
    return (" | " + " | ".join(bits)) if bits else ""


def _fmt_holdings_block(scored_holdings, positions=None, fundamental=None):
    """
    Holdings with their full setup AND unrealized P&L, so Gemini knows which
    positions are winners/losers and how much is at risk. positions is the
    raw account holdings dict (qty/avg_entry/current/unrealized_plpc).
    """
    parts = []
    for t, info in scored_holdings.items():
        pos = (positions or {}).get(t, {})
        pl = pos.get("unrealized_plpc")
        pl_str = f" P&L {pl:+.2f}%" if pl is not None else ""
        parts.append(f"{t} (score {info['score']:.0f}/100 | {_fmt_setup(info.get('indicators'))} |{pl_str}{_fmt_fundamental(t, fundamental)})")
    return "; ".join(parts) or "none"


def _fmt_news_block(candidates, scored_news, sentiment, fundamental=None):
    """
    News candidates with the TOP 2 headlines (not just one) per ticker, so
    Gemini sees the catalyst's full story. Articles are already fetched --
    zero extra cost.
    """
    parts = []
    for t, info in scored_news.items():
        articles = [a for a in candidates.get(t, []) if isinstance(a, dict) and a.get("score") is not None]
        articles.sort(key=lambda a: a.get("score", 0), reverse=True)
        heads = " / ".join((a.get("headline", "") or "")[:100] for a in articles[:3]) or "none"
        ascore = articles[0].get("score") if articles else None
        parts.append(
            f"{t} (quant {info['score']:.0f}/100 | {_fmt_setup(info.get('indicators'))} | "
            f"news {ascore}/10 sent {sentiment.get(t, 0.0):+.2f}{_fmt_fundamental(t, fundamental)}): \"{heads}\""
        )
    return "; ".join(parts) or "none"


def _fmt_watchlist_block(scored_watchlist, fundamental=None):
    parts = []
    for t, info in scored_watchlist.items():
        parts.append(f"{t} (score {info['score']:.0f}/100 | {_fmt_setup(info.get('indicators'))}{_fmt_fundamental(t, fundamental)})")
    return "; ".join(parts) or "none"


def _fmt_pending_block(pending):
    """Compact one-line-per-trade view of the overnight queue for re-verification."""
    if not pending:
        return "none"
    parts = []
    for t in pending:
        if not isinstance(t, dict):  # never crash on a malformed queue entry
            continue
        parts.append(
            f"{t.get('ticker')} {t.get('action', 'buy')} (conf {t.get('confidence')}, "
            f"conv {t.get('conviction')}, stop {t.get('stop_loss')}, "
            f"tp {t.get('take_profit')}): {(t.get('reasoning') or '')[:80]}"
        )
    return "; ".join(parts) or "none"


def get_trade_decisions(candidates, account_snapshot, regime="NEUTRAL", pending_trades=None, time_budget=None):
    holdings = account_snapshot.get("holdings", {})
    # Hard wall-clock budget for the whole decision step (threaded from
    # main.py's run deadline). Feeds, scans and Gemini each consume at most
    # the remaining budget, so the decision step -- and therefore the whole
    # run -- can never blow past ~1 minute. Whatever isn't finished falls
    # back to cache or the technical engine.
    _decide_deadline = time.monotonic() + (time_budget if time_budget is not None else 40.0)

    def _remaining():
        return max(0.0, _decide_deadline - time.monotonic())
    cash = account_snapshot.get("cash", 0)
    total_value = account_snapshot.get("total_value", cash)

    open_orders = get_tickers_with_open_orders()
    cooldowns = get_tickers_on_cooldown()
    unavailable = open_orders | cooldowns

    new_candidates = {t: a for t, a in candidates.items() if t not in holdings}

    # Morning prep briefing (morning_prep.py): pre-analyzed names from the
    # overnight/early-morning Gemini call are injected as candidates so the
    # open trades on the prepared context, not just whatever news just hit.
    try:
        prep_path = os.path.join(os.path.dirname(__file__), "data", "morning_candidates.json")
        if os.path.exists(prep_path):
            with open(prep_path) as f:
                prep = json.load(f)
            # Bulletproof: morning_prep saves a list of dicts, but Gemini's
            # JSON can come back as an object {ticker: {...}} or even a list
            # of strings -- handle every shape so a format hiccup can never
            # crash the run ('str' object has no attribute 'get').
            items = []
            if isinstance(prep, dict):
                for t, v in prep.items():
                    if isinstance(v, dict):
                        items.append({"ticker": t, **v})
            elif isinstance(prep, list):
                items = [i for i in prep if isinstance(i, dict)]
            for item in items:
                t = item.get("ticker")
                if not t or t in holdings or t in new_candidates:
                    continue
                new_candidates.setdefault(t, []).append({
                    "headline": item.get("notes") or item.get("stance") or "morning prep pick",
                    "summary": "",
                    "source": "morning-brief",
                    "url": "",
                    "sentiment": 0.0,
                    "score": float(item.get("priority", 5) or 5),
                })
    except Exception as e:
        print(f"Could not load morning candidates: {e}")

    # Rotate through the FULL S&P 500 universe: every name gets scanned over
    # the day, but each run only scores UNIVERSE_SCAN_PER_RUN non-news names
    # (deterministic per hour, so the same run-hour reuses the same slice).
    watchlist_tickers = [t for t in WATCHLIST if t not in holdings and t not in new_candidates]
    if UNIVERSE_SCAN_PER_RUN > 0 and len(watchlist_tickers) > UNIVERSE_SCAN_PER_RUN:
        watchlist_tickers = sorted(watchlist_tickers)
        seed = int(datetime.now(pytz.timezone("America/New_York")).strftime("%Y%m%d%H"))
        start = seed % len(watchlist_tickers)
        watchlist_tickers = (watchlist_tickers[start:] + watchlist_tickers[:start])[:UNIVERSE_SCAN_PER_RUN]

    # Dual focus: average headline sentiment per news candidate feeds the
    # quant score so a real catalyst moves the needle alongside the chart.
    news_sentiment = {}
    for t, articles in new_candidates.items():
        # Bulletproof against a malformed candidates value (e.g. a float or
        # string instead of a list of article dicts) so one bad article can
        # never crash the run.
        if not isinstance(articles, list) or not articles:
            continue
        valid = [a for a in articles if isinstance(a, dict)]
        if valid:
            news_sentiment[t] = sum(headline_sentiment(a) for a in valid) / len(valid)

    # Phase 2: refresh the cached market-wide + per-ticker feeds, then build
    # cache-only fundamental signals (analyst / insider / reddit / earnings /
    # SEC) for every candidate so the quant score and the LLM see them.
    all_candidate_tickers = list(dict.fromkeys(
        list(new_candidates.keys()) + list(watchlist_tickers)
    ))
    _refresh_data_feeds(all_candidate_tickers, time_budget=_remaining())
    fundamental = {}
    try:
        from data_feeds import get_fundamental_signals
        fundamental = get_fundamental_signals(all_candidate_tickers)
    except Exception as e:
        print(f"Fundamental signals unavailable (continuing without them): {e}")

    scored_holdings = _score_candidates(list(holdings.keys()), time_budget=_remaining())
    scored_news = _score_candidates(
        list(new_candidates.keys()),
        sentiment=news_sentiment,
        extras=fundamental,
        time_budget=_remaining(),
    )
    # The rotating universe slice is cached per hour -- see
    # _score_candidates_cached (this was the dominant per-run cost).
    scored_watchlist = _score_candidates_cached(watchlist_tickers, extras=fundamental, time_budget=_remaining())

    # News + technical confluence: a news candidate only reaches the LLM if
    # its pure technical score (without the sentiment boost) clears the bar.
    new_candidates, scored_news, news_sentiment = _apply_news_confluence(
        new_candidates, scored_news, news_sentiment
    )

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

    now_et_dt = datetime.now(pytz.timezone("America/New_York"))
    now_et = now_et_dt.strftime("%Y-%m-%d %I:%M %p %Z")
    _mins = now_et_dt.hour * 60 + now_et_dt.minute
    _wd = now_et_dt.weekday()
    if _wd < 5 and 570 <= _mins < 960:  # 9:30-16:00
        session = "regular session, market open"
    elif _wd < 5 and ((240 <= _mins < 570) or (960 <= _mins < 1200)):  # 4:00-9:30 / 16:00-20:00
        session = "extended session (after-hours / pre-market)"
    else:
        session = "market closed (orders queue to the next session)"
    prompt = PROMPT_TEMPLATE.format(
        cash=cash,
        total_value=total_value,
        min_conviction=MIN_CONVICTION_TO_TRADE,
        conf_min=CONFIDENCE_MIN_TO_TRADE,
        now_et=now_et,
        session=session,
        holdings_block=_fmt_holdings_block(scored_holdings, holdings, fundamental),
        news_block=_fmt_news_block(new_candidates, scored_news, news_sentiment, fundamental),
        watchlist_block=_fmt_watchlist_block(scored_watchlist, fundamental),
        pending_block=_fmt_pending_block(pending_trades),
        fundamental_block=_build_fundamental_block(all_candidate_tickers),
        performance_brief=build_performance_brief(),
    )

    try:
        response = _generate_with_rotation(prompt, tracker, model_list, time_budget=_remaining())
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
        confidence = t.get("confidence")
        if confidence is not None:
            try:
                confidence = float(confidence)
            except (TypeError, ValueError):
                confidence = None
        conviction = int(t.get("conviction") or 0)
        if confidence is not None:
            if confidence < CONFIDENCE_MIN_TO_TRADE:
                continue
            conviction = max(conviction, round(confidence / 10.0))
        if conviction < MIN_CONVICTION_TO_TRADE:
            continue
        t["conviction"] = conviction
        filtered.append(t)
    return filtered, meta
