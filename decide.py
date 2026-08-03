"""
Builds the full picture for Gemini every run: existing holdings (with P/L,
full technical indicators -- daily AND intraday, chart-based swing levels,
and quant score -- plus any related news), news-driven candidates, and
fixed-watchlist tickers -- both candidate pools first pass through a
quantitative pre-screen (signal_score.py) before they're shown to Gemini
at all. Also informs Gemini of the current market regime, though
regime-based position-size enforcement happens in code (trader.py), not
via prompt instruction alone. Asks for a conviction score (1-10) on every
surviving trade idea, which trader.py uses to scale position size (and,
for exceptional conviction, how much of the cash reserve it may touch).
Gemini may also optionally propose specific stop_loss/take_profit price
levels per BUY idea, based on the chart data shown -- trader.py sanity-
clamps these against ATR bounds before honoring them.

IMPORTANT -- quota management: each model in GEMINI_MODEL_FALLBACKS
(config.py) has its OWN independent daily (RPD) and per-minute (RPM) free
tier quota -- confirmed directly from the account's own rate-limit
dashboard, not guessed. This file tracks usage separately per model,
rotates through them in priority order, and spreads calls evenly across
the day (24/7, no market-hours gating) to use as much of the combined
daily budget as possible without tripping any single model's real limit.
If Google's live response ever reports a different real quotaValue than
configured, that number is parsed out and adopted automatically for the
rest of the day.

FALLBACK -- pure-technical trading: when Gemini call spacing gates block
a call, a fallback decision engine fires instead. It evaluates all
holdings + watchlist tickers on pure technical indicators (daily, intraday,
and chart-based swing levels) and quant score, and generates conviction-
scaled trade ideas -- including chart-based stop_loss/take_profit -- fully
autonomously, without needing Gemini's judgment or news context. This
keeps the bot actively trading every run (e.g. every 2 minutes) while
staying under Gemini's daily/per-minute quota.
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
    GEMINI_QUOTA_RESET_TIMEZONE,
    MAX_POSITION_PCT, MIN_CONVICTION_TO_TRADE,
    WATCHLIST, SWING_LOOKBACK_DAYS,
    MIN_SIGNAL_SCORE_TO_CONSIDER, REGIME_POSITION_MULTIPLIERS,
    EXCEPTIONAL_CONVICTION_THRESHOLD, CONSOLIDATION_SCORE_THRESHOLD,
    MAX_OPEN_POSITIONS,
    TECHNICAL_MIN_CONVICTION, TECHNICAL_CONVICTION_AGGRESSIVENESS,
)
from trader import (
    get_full_indicators, get_tickers_with_open_orders, get_tickers_on_cooldown,
)
from signal_score import calculate_signal_score

_client = genai.Client(api_key=GEMINI_API_KEY)

_CALL_TRACKER_FILE = os.path.join(os.path.dirname(__file__), "data", "gemini_call_tracker.json")
_QUOTA_TZ = pytz.timezone(GEMINI_QUOTA_RESET_TIMEZONE)

_SWING_LOW_KEY = f"recent_swing_low_{SWING_LOOKBACK_DAYS}d"
_SWING_HIGH_KEY = f"recent_swing_high_{SWING_LOOKBACK_DAYS}d"

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
                    "stop_loss": types.Schema(type=types.Type.NUMBER),
                    "take_profit": types.Schema(type=types.Type.NUMBER),
                },
                required=["ticker", "action", "conviction"],
            ),
        )
    },
    required=["trades"],
)

PROMPT_TEMPLATE = """You are a moderately aggressive DAY-TRADING analyst for a SIMULATED \
paper-trading portfolio (no real money). You have access to BOTH daily-chart context \
(trend, moving averages, multi-week momentum) AND short-term intraday context (5-minute-bar \
RSI, intraday momentum since today's open, VWAP deviation) for every ticker below -- use both: \
the daily context tells you the broader setup, the intraday context tells you whether NOW is a \
good moment to act on it.

Every trade idea you propose MUST include a "conviction" score from 1-10, reflecting how \
strongly the signals (news, daily trend, intraday momentum, RSI, MACD, volume) agree with each \
other. Ideas below conviction {min_conviction} will be discarded automatically, so only include \
ideas you'd actually rate that high. Reserve conviction {exceptional_conviction}+ for truly \
exceptional setups only -- those get access to extra cash reserve room in code, so treat that \
rating as rare, not routine.

For BUY ideas, you MAY optionally include "stop_loss" and "take_profit" as specific price levels \
based on the actual chart structure shown below (e.g. the recent_swing_low/recent_swing_high \
figures), rather than a generic percentage. If you omit either, the system automatically computes \
a sensible one from the same swing-low/swing-high data, with sanity bounds based on the ticker's \
own ATR so a stop can never end up absurdly tight or absurdly wide. Only propose custom levels \
when you see a genuinely meaningful support/resistance point in the data -- otherwise, omitting \
them and letting the system's default apply is completely fine.

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
- RSI (14, daily): <30 oversold, >70 overbought.
- Intraday RSI (5-min bars): same read as above but for TODAY's short-term momentum specifically.
- ADX (14): >25 = strong trend (either direction), <20 = weak/no trend -- use this to judge whether
  a momentum or trend-following idea is actually supported.
- MACD: histogram above zero and rising = bullish momentum building; below zero and falling = bearish.
- Bollinger %B: near 1.0 = price near upper band (extended), near 0.0 = near lower band (extended).
- Stochastic %K/%D: >80 overbought, <20 oversold.
- ATR: the stock's typical daily range in price terms -- larger ATR means a given % move happens on
  smaller price swings, useful for judging if a move is significant relative to normal noise. Also
  used as the unit for sanity-bounding any stop_loss/take_profit you propose.
- Swing low/high ({swing_days}d): the lowest low and highest high over the recent chart window --
  natural support/resistance reference points for setting a chart-based stop_loss or take_profit.
- Intraday momentum: % change from today's opening 5-min bar to now -- positive and rising alongside
  a bullish daily setup is a strong "the move is happening right now" confirmation.
- VWAP deviation: how far the current price sits from today's volume-weighted average price --
  large positive deviation on high relative volume suggests a strong intraday move already underway
  (could mean momentum, or could mean it's already extended -- weigh against RSI/Stochastic).
- Relative volume: how today's volume compares to its recent average -- large positive spikes mean
  a move has real conviction behind it; near 0% means average, uneventful volume.
- Trend: uptrend / downtrend / sideways, from the 20 vs 50 SMA relationship (daily).
- Quant score: a 0-100 composite of the above (trend, ADX, RSI, volume, MACD), computed
  independently of you. Not a replacement for your judgment -- a useful cross-check.

Rules:
- Never let any single position exceed {max_pct}% of total portfolio value.
- Only use tickers that appear in the lists above -- do not invent tickers.
- Tickers marked "(unavailable: pending order or cooldown)" must be skipped entirely.
- Prefer setups where multiple signals agree across BOTH timeframes (e.g. daily uptrend + news +
  intraday RSI turning up + rising relative volume) over single-signal or single-timeframe ideas --
  these deserve higher conviction scores.
- It's completely fine to return zero trades if nothing meets the bar.
- If the market is currently closed, orders you propose will queue and execute at the next market
  open (standard day-order behavior) -- still fine to propose ideas based on the data shown.
- Market-regime-based position-size scaling (described above) is enforced automatically in code,
  regardless of what you propose -- you don't need to and cannot override it.
- A hard cap on total number of open positions ({max_positions} held) is also enforced in code. If the portfolio is at \
or over that cap, the consolidation engine will automatically purge the lowest-scoring excess positions \
that score below {consolidation_threshold}. Focus your work on trimming existing weaker holdings to clear space.

Respond with ONLY valid JSON (no markdown fences, no commentary):
{{
  "trades": [
    {{"ticker": "AAPL", "action": "buy", "dollar_amount": 5000, "conviction": 8, "reasoning": "short reason", "stop_loss": 227.50, "take_profit": 241.00}}
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
        f"swing low({SWING_LOOKBACK_DAYS}d) ${data.get(_SWING_LOW_KEY)}" if data.get(_SWING_LOW_KEY) is not None else "swing low unknown",
        f"swing high({SWING_LOOKBACK_DAYS}d) ${data.get(_SWING_HIGH_KEY)}" if data.get(_SWING_HIGH_KEY) is not None else "swing high unknown",
        f"intraday RSI {data.get('intraday_rsi')}" if data.get('intraday_rsi') is not None else "intraday RSI unknown",
        f"intraday momentum {data.get('intraday_momentum_pct')}%" if data.get('intraday_momentum_pct') is not None else "intraday momentum unknown",
        f"intraday trend {data.get('intraday_trend')}" if data.get('intraday_trend') else "intraday trend unknown",
        f"VWAP dev {data.get('vwap_deviation_pct')}%" if data.get('vwap_deviation_pct') is not None else "VWAP dev unknown",
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

    confirmed = data.get("confirmed_limits", {})
    if data.get("date") != _quota_day_now():
        fresh = _default_tracker()
        fresh["confirmed_limits"] = confirmed
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
    pattern = rf"'quotaId':\s*'[^']*{marker}[^']*'.*?'quotaValue':\s*'(\d+)'"
    match = re.search(pattern, error_str, re.DOTALL)
    return int(match.group(1)) if match else None


def _is_daily_quota_error(error_str):
    return "PerDay" in error_str or "GenerateRequestsPerDayPerProjectPerModel" in error_str


def _should_attempt_call(tracker):
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


# ============================================================
# Pure-technical fallback (when Gemini call is throttled)
# ============================================================

def _technical_conviction_from_score(quant_score):
    """
    Maps the quant signal_score (0-100) to a conviction level (1-9) for
    pure-technical trading, scaled by TECHNICAL_CONVICTION_AGGRESSIVENESS.
    Score 55 -> conviction 5, score 100 -> conviction ~9.5 (capped at 9).
    Below MIN_SIGNAL_SCORE_TO_CONSIDER, returns 0 (not traded).
    """
    if quant_score < MIN_SIGNAL_SCORE_TO_CONSIDER:
        return 0
    base_conviction = 5 + ((quant_score - MIN_SIGNAL_SCORE_TO_CONSIDER) / (100 - MIN_SIGNAL_SCORE_TO_CONSIDER)) * 4.5
    scaled = base_conviction * TECHNICAL_CONVICTION_AGGRESSIVENESS
    return min(9, max(1, round(scaled, 1)))


def get_technical_trade_decisions(scored_holdings, scored_watchlist, account_snapshot):
    """
    Pure-technical fallback: generates trade ideas based purely on quant
    signals (indicators + signal_score, daily AND intraday, plus chart-based
    swing levels for stop_loss/take_profit) -- no Gemini, no news context.
    Used when the Gemini quota-spacing gate blocks a call, so the bot stays
    actively trading every run without burning Gemini quota.
    """
    holdings = account_snapshot.get("holdings", {})
    open_orders = get_tickers_with_open_orders()
    cooldowns = get_tickers_on_cooldown()
    unavailable = open_orders | cooldowns

    trades = []

    # --- Holdings: trim/exit weak ones, add to strong ones
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
                "reasoning": f"technical score {score:.0f}/100 below threshold ({MIN_SIGNAL_SCORE_TO_CONSIDER}), "
                            f"weak setup, exiting to consolidate.",
            })
        elif score >= 80:
            conviction = _technical_conviction_from_score(score)
            if conviction >= TECHNICAL_MIN_CONVICTION:
                data = info.get("indicators") or {}
                trades.append({
                    "ticker": ticker,
                    "action": "buy",
                    "dollar_amount": 0,
                    "conviction": int(conviction),
                    "reasoning": f"technical score {score:.0f}/100, strong technical setup, adding to strong position.",
                    "stop_loss": data.get(_SWING_LOW_KEY),
                    "take_profit": data.get(_SWING_HIGH_KEY),
                })

    # --- Watchlist + new candidates: find strong entries
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
                "reasoning": f"technical score {score:.0f}/100, strong signal on technicals alone, "
                            f"no recent news but pattern is compelling.",
                "stop_loss": data.get(_SWING_LOW_KEY),
                "take_profit": data.get(_SWING_HIGH_KEY),
            })

    meta = {
        "candidates_considered": len(scored_watchlist),
        "candidates_passed_prescreen": sum(
            1 for info in scored_watchlist.values()
            if info["score"] >= MIN_SIGNAL_SCORE_TO_CONSIDER
        ),
        "throttled": False,
        "technical_fallback": True,
        "gemini_calls_today": 0,
    }
    return trades, meta


def get_trade_decisions(candidates, account_snapshot, regime="NEUTRAL"):
    """
    Returns (trades, meta) where trades is the filtered list of trade
    ideas and meta is a stats dict for logging to performance.csv:
        {"candidates_considered": int, "candidates_passed_prescreen": int,
         "throttled": bool, "technical_fallback": bool, "gemini_calls_today": int}

    If Gemini call is allowed by spacing, tries to call Gemini. If blocked,
    falls back to the pure-technical decision engine instead.
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
        "technical_fallback": False,
        "gemini_calls_today": calls_today,
    }

    should_call, reason = _should_attempt_call(tracker)
    if not should_call:
        print(f"Skipping Gemini call this run: {reason}")
        print("Falling back to pure-technical decision engine instead.")
        meta["throttled"] = True
        combined_watchlist = {**scored_news, **scored_watchlist}
        trades, tech_meta = get_technical_trade_decisions(scored_holdings, combined_watchlist, account_snapshot)
        meta.update(tech_meta)
        return trades, meta

    prompt = PROMPT_TEMPLATE.format(
        cash=cash,
        total_value=total_value,
        max_pct=int(MAX_POSITION_PCT * 100),
        min_conviction=MIN_CONVICTION_TO_TRADE,
        exceptional_conviction=EXCEPTIONAL_CONVICTION_THRESHOLD,
        consolidation_threshold=CONSOLIDATION_SCORE_THRESHOLD,
        max_positions=MAX_OPEN_POSITIONS,
        swing_days=SWING_LOOKBACK_DAYS,
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
        _save_tracker(tracker)
        print(f"Gemini call failed on all models with remaining quota: {e}")
        print("Falling back to pure-technical decision engine instead.")
        combined_watchlist = {**scored_news, **scored_watchlist}
        trades, tech_meta = get_technical_trade_decisions(scored_holdings, combined_watchlist, account_snapshot)
        meta.update(tech_meta)
        return trades, meta

    raw_text = (response.text or "").strip()
    raw_text = re.sub(r"^```json\s*|\s*```$", "", raw_text)

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
