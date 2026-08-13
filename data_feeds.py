"""
Phase 2 data feeds: everything beyond price/news that a pro daytrader watches.

  - Economic calendar (CPI, FOMC, NFP, GDP, PCE ...)   -- BUILT-IN table, free and
    offline (Finnhub's /calendar/economic endpoint needs a PAID plan, so it is
    never called -- there is no fallback, this IS the calendar)
  - Analyst upgrades / downgrades                       -- Finnhub /stock/upgrade-downgrade
    (PAID tier; disabled by default so free keys never 403)
  - Insider buying / selling                            -- Finnhub /stock/insider-transactions (per ticker)
  - SEC filings (8-K, 10-Q, 10-K, Form 4)               -- Finnhub /stock/filings (per ticker)
  - Reddit sentiment (best-effort, r/wallstreetbets + r/stocks + r/investing)

Every fetch is cached to logs/ with a TTL so the market-hours loop doesn't
hammer the APIs, and EVERY call is fail-soft: a missing key, a network error,
or a blocked IP (Reddit blocks datacenter IPs -- including GitHub Actions)
never breaks the trading run. The bot just trades without that feed.

Cache files (all under logs/):
  eco_calendar.json        -- economic events, refreshed every N hours
  analyst_actions.json     -- recent upgrades/downgrades, refreshed every N hours
  insider_activity.json    -- per-ticker insider buys/sells, refreshed daily
  sec_filings.json         -- per-ticker recent filings, refreshed daily
  reddit_sentiment.json    -- per-ticker sentiment from Reddit, refreshed every N hours
"""

import os
import json
import re
import time
from datetime import datetime, timedelta

import requests

from config import FINNHUB_API_KEY, ENABLE_ANALYST_ACTIONS

BASE_DIR = os.path.dirname(__file__)
LOG_DIR = os.path.join(BASE_DIR, "logs")

_HIGH_IMPACT_EVENTS = (
    "cpi", "consumer price index", "fomc", "fed funds", "nonfarm", "non-farm",
    "payroll", "employment situation", "gdp", "pce", "core pce", "jobs report",
    "unemployment", "interest rate decision",
)

# Built-in calendar of the big market-moving releases -- the PRIMARY source.
# Finnhub's /calendar/economic endpoint requires a PAID plan (free keys get
# HTTP 403), so this table IS the economic calendar on the free plan: no live
# fetch, no fallback. Dates are the OFFICIAL scheduled dates:
#   - FOMC decision days: federalreserve.gov 2026 meeting calendar
#   - CPI + Employment Situation (NFP): bls.gov 2026 release schedule
_FALLBACK_EVENTS_2026 = {
    # FOMC rate decisions (2nd day of each meeting)
    "2026-01-28": ["FOMC rate decision"],
    "2026-03-18": ["FOMC rate decision"],
    "2026-04-29": ["FOMC rate decision"],
    "2026-06-17": ["FOMC rate decision"],
    "2026-07-29": ["FOMC rate decision"],
    "2026-09-16": ["FOMC rate decision"],
    "2026-10-28": ["FOMC rate decision"],
    "2026-12-09": ["FOMC rate decision"],
    # Consumer Price Index (8:30 AM ET)
    "2026-01-13": ["CPI release"],
    "2026-02-13": ["CPI release"],
    "2026-03-11": ["CPI release"],
    "2026-04-10": ["CPI release"],
    "2026-05-12": ["CPI release"],
    "2026-06-10": ["CPI release"],
    "2026-07-14": ["CPI release"],
    "2026-08-12": ["CPI release"],
    "2026-09-11": ["CPI release"],
    "2026-10-14": ["CPI release"],
    "2026-11-10": ["CPI release"],
    "2026-12-10": ["CPI release"],
    # Employment Situation / Non-Farm Payrolls (8:30 AM ET)
    "2026-01-09": ["Employment Situation (NFP)"],
    "2026-02-11": ["Employment Situation (NFP)"],
    "2026-03-06": ["Employment Situation (NFP)"],
    "2026-04-03": ["Employment Situation (NFP)"],
    "2026-05-08": ["Employment Situation (NFP)"],
    "2026-06-05": ["Employment Situation (NFP)"],
    "2026-07-02": ["Employment Situation (NFP)"],
    "2026-08-07": ["Employment Situation (NFP)"],
    "2026-09-04": ["Employment Situation (NFP)"],
    "2026-10-02": ["Employment Situation (NFP)"],
    "2026-11-06": ["Employment Situation (NFP)"],
    "2026-12-04": ["Employment Situation (NFP)"],
}
_REDDIT_SUBS = ["wallstreetbets", "stocks", "investing"]
_REDDIT_UA = {"User-Agent": "freebuff-paper-trader/1.0 (research)"}
_UA_TIMEOUT = 10


def _cache_path(name):
    return os.path.join(LOG_DIR, name)


def _load_cache(name, ttl_hours):
    path = _cache_path(name)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        ts = data.get("_fetched_at")
        if not ts:
            return None
        age = datetime.now() - datetime.fromisoformat(ts)
        if age > timedelta(hours=ttl_hours):
            return None
        return data
    except (json.JSONDecodeError, OSError, ValueError):
        return None


def _save_cache(name, payload):
    os.makedirs(LOG_DIR, exist_ok=True)
    payload = dict(payload)
    payload["_fetched_at"] = datetime.now().isoformat()
    path = _cache_path(name)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except OSError as e:
        print(f"Could not write cache {name}: {e}")


# ============================================================
# Economic calendar (market-wide, cheap, high value)
# ============================================================
def fetch_economic_calendar():
    """
    Returns {"events": [{"date", "event", "impact", "actual", "forecast"}...]}
    for the next ~14 days, or a cache hit. Never raises.

    Free-plan friendly: Finnhub's /calendar/economic endpoint requires a PAID
    plan (free keys get HTTP 403), so this feed is a pure local table of the
    official FOMC / CPI / Employment-Situation (NFP) release dates. There is
    no live fetch and no fallback -- the built-in table IS the calendar.
    """
    cached = _load_cache("eco_calendar.json", 6)
    if cached:
        return cached
    result = _builtin_economic_calendar()
    _save_cache("eco_calendar.json", result)
    return result


def _builtin_economic_calendar():
    """
    Built-in high-impact event calendar (official 2026 FOMC decision days,
    CPI, and Employment Situation/NFP -- from federalreserve.gov and bls.gov
    schedules) covering the next 14 days. No API, no fallback, never raises.
    """
    today = datetime.now().date()
    events = []
    for day in range(15):
        d = (today + timedelta(days=day)).isoformat()
        for name in _FALLBACK_EVENTS_2026.get(d, []):
            events.append({
                "date": d, "event": name, "impact": "high",
                "actual": None, "forecast": "n/a",
            })
    high_impact_today = None
    today_names = _FALLBACK_EVENTS_2026.get(today.isoformat())
    if today_names:
        high_impact_today = f"{today_names[0]} (high impact) forecast n/a"
    return {"events": events, "high_impact_today": high_impact_today, "_source": "builtin"}


def high_impact_event_today():
    """
    Returns (bool, description_or_None) from the CACHE ONLY. The cache is
    populated at the start of each trading run (decide.py), so the hot
    position-sizing path never makes a network call.
    """
    cal = _load_cache("eco_calendar.json", 6)
    if cal is None:
        return (False, None)
    desc = cal.get("high_impact_today")
    return (desc is not None, desc)


# ============================================================
# Analyst upgrades / downgrades (market-wide)
# ============================================================
def fetch_analyst_actions():
    """
    Returns {"actions": [{"symbol", "grade_time", "action", "from_grade",
    "to_grade", "company"}...]} from the last ~7 days of analyst actions.

    Finnhub's /stock/upgrade-downgrade endpoint requires a PAID plan (free
    keys get HTTP 403), so this feed is DISABLED by default
    (ENABLE_ANALYST_ACTIONS=false) and returns empty without any API call --
    the bot is free-plan accustomed with zero error spam. Re-enable only
    with a paid Finnhub key.
    """
    if not ENABLE_ANALYST_ACTIONS:
        return {"actions": []}
    cached = _load_cache("analyst_actions.json", 6)
    if cached:
        return cached
    actions = []
    try:
        resp = requests.get(
            "https://finnhub.io/api/v1/stock/upgrade-downgrade",
            params={"token": FINNHUB_API_KEY},
            timeout=15,
        )
        resp.raise_for_status()
        raw = resp.json() or []
        now = datetime.now()
        for a in raw:
            try:
                grade_time = datetime.fromtimestamp(int(a.get("gradeTime", 0)))
            except (TypeError, ValueError, OSError):
                continue
            if (now - grade_time).days > 7:
                continue
            actions.append({
                "symbol": a.get("symbol"),
                "grade_time": grade_time.strftime("%Y-%m-%d %H:%M"),
                "action": (a.get("upgradeDowngrade") or "").lower(),
                "from_grade": a.get("fromGrade"),
                "to_grade": a.get("toGrade"),
                "company": a.get("company"),
            })
    except Exception as e:
        print(f"Analyst actions fetch failed (trading continues without it): {e}")
        return {"actions": []}
    result = {"actions": actions}
    _save_cache("analyst_actions.json", result)
    return result


# ============================================================
# Insider buying / selling (per-ticker, cached daily)
# ============================================================
def fetch_insider_activity(tickers, time_budget=None):
    """
    For each ticker, the last ~90 days of insider transactions, summarized as
    {ticker: {"net_buy_value": $, "buy_count": n, "sell_count": n}}.
    Only fetches tickers not already in the daily cache. Best-effort.
    time_budget: optional wall-clock seconds cap for the WHOLE loop, so a
    cold cache can't stall a run (decide.py passes its feed-refresh budget).
    """
    cached = _load_cache("insider_activity.json", 24)
    store = dict(cached.get("by_ticker", {})) if cached else {}
    need = [t for t in tickers if t and t not in store]
    _loop_start = time.monotonic()
    for t in need[:20]:
        if time_budget is not None and time.monotonic() - _loop_start >= time_budget:
            break
        try:
            resp = requests.get(
                "https://finnhub.io/api/v1/stock/insider-transactions",
                params={"symbol": t, "token": FINNHUB_API_KEY},
                timeout=12,
            )
            resp.raise_for_status()
            rows = (resp.json().get("data") or [])
            buys = sells = 0
            net = 0.0
            for r in rows:
                code = (r.get("transactionCode") or "").upper()
                try:
                    value = float(r.get("change") or 0) * float(r.get("transactionPrice") or 0)
                except (TypeError, ValueError):
                    value = 0.0
                if code in ("P", "PU", "A"):  # purchase / purchase + plan / award
                    buys += 1
                    net += value
                elif code in ("S", "SE"):
                    sells += 1
                    net -= value
            store[t] = {
                "net_buy_value": round(net, 2),
                "buy_count": buys,
                "sell_count": sells,
            }
        except Exception as e:
            print(f"Insider fetch failed for {t} (continuing): {e}")
            store.setdefault(t, {"net_buy_value": 0.0, "buy_count": 0, "sell_count": 0})
    _save_cache("insider_activity.json", {"by_ticker": store})
    return store


def get_insider_activity(tickers, time_budget=None):
    """Cache-or-fetch wrapper used by the scoring engine (never raises).
    time_budget is forwarded so decide.py's feed-refresh budget actually
    applies (without it the wrapper dropped the budget entirely)."""
    try:
        return fetch_insider_activity(tickers, time_budget=time_budget)
    except Exception as e:
        print(f"Insider activity unavailable: {e}")
        return {}


# ============================================================
# SEC filings (per-ticker, cached daily)
# ============================================================
def fetch_sec_filings(tickers, time_budget=None):
    """
    Recent SEC filings per ticker: {ticker: [{"form", "date", "title"}...]},
    keeping only forms that actually matter for a trade decision
    (8-K current events, 10-Q/10-K earnings, Form 4 insider trades).
    time_budget: optional wall-clock seconds cap for the WHOLE loop, so a
    cold cache can't stall a run (decide.py passes its feed-refresh budget).
    """
    cached = _load_cache("sec_filings.json", 24)
    store = dict(cached.get("by_ticker", {})) if cached else {}
    relevant = ("8-K", "10-Q", "10-K", "4")
    need = [t for t in tickers if t and t not in store]
    _loop_start = time.monotonic()
    for t in need[:20]:
        if time_budget is not None and time.monotonic() - _loop_start >= time_budget:
            break
        try:
            resp = requests.get(
                "https://finnhub.io/api/v1/stock/filings",
                params={"symbol": t, "token": FINNHUB_API_KEY},
                timeout=12,
            )
            resp.raise_for_status()
            rows = []
            for f in (resp.json() or []):
                form = f.get("form") or ""
                if any(form.startswith(r) for r in relevant):
                    rows.append({
                        "form": form,
                        "date": f.get("filedDate", ""),
                        "title": (f.get("title") or "")[:120],
                    })
            store[t] = rows[:5]
        except Exception as e:
            print(f"SEC filings fetch failed for {t} (continuing): {e}")
            store.setdefault(t, [])
    _save_cache("sec_filings.json", {"by_ticker": store})
    return store


def get_sec_filings(tickers, time_budget=None):
    """Cache-or-fetch wrapper (never raises).
    time_budget is forwarded so decide.py's feed-refresh budget actually
    applies (without it the wrapper dropped the budget entirely)."""
    try:
        return fetch_sec_filings(tickers, time_budget=time_budget)
    except Exception as e:
        print(f"SEC filings unavailable: {e}")
        return {}


# ============================================================
# Reddit sentiment (best-effort; datacenter IPs are often blocked)
# ============================================================
def fetch_reddit_sentiment():
    """
    Best-effort -1..+1 sentiment per ticker from Reddit's public JSON.
    Only r/wallstreetbets, r/stocks, r/investing 'hot' listings, so the
    surface is small but real. Returns {} on ANY failure -- a blocked IP or
    rate limit must never break a run.
    """
    cached = _load_cache("reddit_sentiment.json", 6)
    if cached:
        return cached.get("by_ticker", {})
    from news import headline_sentiment  # reuse the same deterministic lexicon

    by_ticker = {}
    try:
        for sub in _REDDIT_SUBS:
            resp = requests.get(
                f"https://www.reddit.com/r/{sub}/hot.json",
                params={"limit": 25},
                headers=_REDDIT_UA,
                timeout=_UA_TIMEOUT,
            )
            if resp.status_code != 200:
                continue
            children = resp.json().get("data", {}).get("children", [])
            for child in children:
                post = child.get("data", {})
                text = f"{post.get('title', '')} {post.get('selftext', '')}"[:2000]
                sent = headline_sentiment({"headline": text, "summary": ""})
                if abs(sent) < 0.01:
                    continue
                for ticker, _name in _get_sp500():
                    if len(ticker) >= 2 and re.search(rf"\b{ticker}\b", text):
                        by_ticker.setdefault(ticker, []).append(sent)
    except Exception as e:
        print(f"Reddit sentiment unavailable (continuing without it): {e}")
        return {}

    result = {}
    for t, sents in by_ticker.items():
        result[t] = round(sum(sents) / len(sents), 2)
    _save_cache("reddit_sentiment.json", {"by_ticker": result})
    return result


def get_reddit_sentiment():
    try:
        return fetch_reddit_sentiment()
    except Exception:
        return {}


# ============================================================
# Market pulse: indexes + sector rotation + scan movers (Gemini context)
# ============================================================
# 11 sector SPDR ETFs, used for today's sector-rotation read.
_SECTOR_ETFS = {
    "XLK": "Technology", "XLF": "Financials", "XLE": "Energy",
    "XLV": "Health Care", "XLY": "Consumer Discretionary", "XLP": "Consumer Staples",
    "XLI": "Industrials", "XLU": "Utilities", "XLB": "Materials",
    "XLRE": "Real Estate", "XLC": "Communication",
}

# Macro ETFs: cheap, tradeable proxies for the big-picture backdrop every
# daytrader checks first -- rates (TLT moves INVERSELY to yields, so a falling
# TLT = rising rates), the dollar (UUP), and gold (GLD, the fear trade).
_MACRO_ETFS = {
    "TLT": "rates (TLT, down=rising yields)",
    "UUP": "dollar",
    "GLD": "gold",
}


def get_market_pulse():
    """
    Market backdrop for the Gemini prompt, cached for 1 hour:
      - <SYM>_today  : today's % change for SPY/QQQ/IWM/DIA, the 11 sector
                       ETFs, and the 3 macro ETFs (TLT/UUP/GLD)
      - <SYM>_mom5   : 5-trading-day momentum for the 4 indexes
      - <SYM>_vol20  : 20-day realized (annualized) volatility for SPY/QQQ
    At most 18 price-history calls ONCE per hour, then pure cache reads.
    Fail-soft: returns {} on any error so the run continues without it.
    """
    cached = _load_cache("market_pulse2.json", 1)
    if cached:
        return cached.get("pulse", {})
    from trader import get_price_history  # local import avoids a cycle
    symbols = (
        ["SPY", "QQQ", "IWM", "DIA"]
        + list(_SECTOR_ETFS.keys())
        + list(_MACRO_ETFS.keys())
    )
    # Hard wall-clock budget so a slow Alpaca minute can never stall a run:
    # at most 12s total, then we keep whatever we already have (partial pulse
    # still beats a hung run; the rest fills in next hour's first run).
    pulse = {}
    _deadline = time.monotonic() + 12.0
    for s in symbols:
        if time.monotonic() >= _deadline:
            break
        try:
            bars = get_price_history(s, days=90)  # needs >=55 bars to return
            if bars is None:
                continue
            closes = [float(c) for c in bars.get("closes", [])]
            if len(closes) < 2:
                continue
            prev = closes[-2]
            if prev:
                pulse[f"{s}_today"] = round((closes[-1] / prev - 1) * 100, 2)
            if len(closes) >= 6 and closes[-6]:
                pulse[f"{s}_mom5"] = round((closes[-1] / closes[-6] - 1) * 100, 2)
            if len(closes) >= 22:
                rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))][-20:]
                mean = sum(rets) / len(rets)
                var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
                pulse[f"{s}_vol20"] = round((var ** 0.5) * (252 ** 0.5) * 100, 1)
        except Exception:
            continue
    if pulse:
        _save_cache("market_pulse2.json", {"pulse": pulse})
    return pulse


def _scan_movers():
    """
    Top upside/downside movers among TODAY's scanned candidates, read from
    decide.py's hourly scan cache (data/scan_cache.json). Zero API cost -- it
    reuses work the scan already did. Returns (up_list, down_list, up_count,
    down_count): up/down lists are (ticker, pct) pairs sorted by |move|.
    """
    try:
        path = os.path.join(BASE_DIR, "data", "scan_cache.json")
        with open(path) as f:
            cache = json.load(f)
    except Exception:
        return [], [], 0, 0
    today = datetime.now().strftime("%Y%m%d")
    rows = []
    for key, info in cache.items():
        try:
            hour = str(key).split("|")[-1]
        except Exception:
            continue
        if not hour.startswith(today):
            continue
        ind = (info or {}).get("indicators") or {}
        pct = ind.get("intraday_momentum_pct")
        if pct is None:
            pct = ind.get("gap_pct")
        if pct is None:
            continue
        try:
            rows.append((str(key).split("|")[0], float(pct)))
        except (TypeError, ValueError):
            continue
    if not rows:
        return [], [], 0, 0
    rows.sort(key=lambda x: x[1], reverse=True)
    ups = [r for r in rows if r[1] > 0]
    downs = [r for r in rows if r[1] < 0]
    return rows[:5], (rows[-5:][::-1] if rows else []), len(ups), len(downs)


# Per-ticker sector + market cap from Finnhub /stock/profile2 (free tier),
# cached 24h. Gives Gemini the crucial "this ticker lives in the strongest /
# weakest sector today" link. Budgeted + capped so a cold cache can't stall
# a run: at most 12 NEW tickers per call within a 12s wall-clock budget (the
# 60s run cap now affords deeper per-run coverage).
SECTOR_PROFILE_BUDGET_SECONDS = 12.0
SECTOR_PROFILE_MAX_FRESH = 12


# Per-ticker analyst consensus + price targets from Finnhub /stock/metrics
# (free tier -- recommendationBuy/Hold/Sell counts and the street's mean/high/
# low price targets), cached 24h. This is the "what does Wall Street think"
# number Gemini can't get from a chart. Same budgeted+capped lazy pattern as
# sector profiles so a cold cache can never stall a run: at most 12 NEW
# tickers per call within a 12s wall-clock budget.
ANALYST_CONSENSUS_BUDGET_SECONDS = 12.0
ANALYST_CONSENSUS_MAX_FRESH = 12


def _consensus_from_recommendation_trends(t):
    """
    Fallback for get_analyst_consensus: Finnhub /stock/recommendation-trends
    (documented FREE endpoint) returns the raw buy/hold/sell analyst counts.
    Returns (buy, hold, sell) or (None, None, None) on any failure.
    """
    try:
        resp = requests.get(
            "https://finnhub.io/api/v1/stock/recommendation-trends",
            params={"symbol": t, "token": FINNHUB_API_KEY},
            timeout=10,
        )
        resp.raise_for_status()
        rows = resp.json() or []
        if not rows:
            return None, None, None
        r = rows[0]
        return r.get("strongBuy") + (r.get("buy") or 0), r.get("hold"), r.get("sell") + (r.get("strongSell") or 0)
    except Exception:
        return None, None, None


def _consensus_from_price_target(t):
    """
    Fallback for get_analyst_consensus: Finnhub /stock/price-target
    (documented FREE endpoint) returns the street's mean/high/low targets.
    Returns (mean, high, low) or (None, None, None) on any failure.
    """
    try:
        resp = requests.get(
            "https://finnhub.io/api/v1/stock/price-target",
            params={"symbol": t, "token": FINNHUB_API_KEY},
            timeout=10,
        )
        resp.raise_for_status()
        d = resp.json() or {}
        return d.get("targetMean"), d.get("targetHigh"), d.get("targetLow")
    except Exception:
        return None, None, None


def get_analyst_consensus(tickers):
    """
    {ticker: {"buy": int, "hold": int, "sell": int, "target_mean": float,
    "target_high": float, "target_low": float}} -- primary source Finnhub
    /stock/metrics (free tier), with automatic fallback to the two endpoints
    that are DOCUMENTED free (/stock/recommendation-trends +
    /stock/price-target) when the primary returns an empty/invalid body (as
    some free keys do). Cached 24h, only uncached tickers fetched.
    Fail-soft: returns {} on any error -- the run continues without it.
    """
    cached = _load_cache("analyst_consensus.json", 24)
    store = dict(cached.get("by_ticker", {})) if cached else {}
    need = [t for t in tickers if t and t not in store]
    _loop_start = time.monotonic()
    for t in need[:ANALYST_CONSENSUS_MAX_FRESH]:
        if time.monotonic() - _loop_start >= ANALYST_CONSENSUS_BUDGET_SECONDS:
            break
        entry = {}
        try:
            resp = requests.get(
                "https://finnhub.io/api/v1/stock/metrics",
                params={"symbol": t, "metric": "all", "token": FINNHUB_API_KEY},
                timeout=10,
            )
            resp.raise_for_status()
            data = (resp.json() or {}).get("metric") or {}
            entry = {
                "buy": data.get("recommendationBuy"),
                "hold": data.get("recommendationHold"),
                "sell": data.get("recommendationSell"),
                "target_mean": data.get("targetMeanPrice"),
                "target_high": data.get("targetHighPrice"),
                "target_low": data.get("targetLowPrice"),
            }
        except Exception as e:
            # Primary endpoint failed (often an empty body on free keys).
            # Fall back to the documented-free endpoints so the feature
            # still works instead of silently producing nothing.
            print(f"Analyst metrics failed for {t} (falling back to free endpoints): {e}")
            b, h, s = _consensus_from_recommendation_trends(t)
            m, hi, lo = _consensus_from_price_target(t)
            entry = {
                "buy": b, "hold": h, "sell": s,
                "target_mean": m, "target_high": hi, "target_low": lo,
            }
        if not any(v is not None for v in entry.values()):
            entry = {}
        store[t] = entry
    _save_cache("analyst_consensus.json", {"by_ticker": store})
    return store


def get_sector_profiles(tickers):
    """
    {ticker: {"sector": str, "market_cap": float}} from Finnhub
    /stock/profile2 (free tier), cached 24h. Only tickers not already cached
    are fetched. Fail-soft: returns {} on any error -- the run continues
    without the sector link.
    """
    cached = _load_cache("sector_profiles.json", 24)
    store = dict(cached.get("by_ticker", {})) if cached else {}
    need = [t for t in tickers if t and t not in store]
    _loop_start = time.monotonic()
    for t in need[:SECTOR_PROFILE_MAX_FRESH]:
        if time.monotonic() - _loop_start >= SECTOR_PROFILE_BUDGET_SECONDS:
            break
        try:
            resp = requests.get(
                "https://finnhub.io/api/v1/stock/profile2",
                params={"symbol": t, "token": FINNHUB_API_KEY},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json() or {}
            sector = (data.get("sector") or "").strip()
            store[t] = {
                "sector": sector or None,
                "market_cap": data.get("marketCapitalization"),
            }
        except Exception as e:
            print(f"Sector profile fetch failed for {t} (continuing): {e}")
            store.setdefault(t, {"sector": None, "market_cap": None})
    _save_cache("sector_profiles.json", {"by_ticker": store})
    return store


# ============================================================
# Assembled blocks for prompts / scoring
# ============================================================
def get_fundamental_signals(tickers):
    """
    Everything Phase 2 knows about the given tickers right now, as a compact
    dict: {ticker: {"analyst": "upgrade"/"downgrade"/None, "insider_net": $,
    "reddit_sentiment": -1..+1, "recent_filings": ["8-K", ...],
    "days_until_earnings": int/None, "sector": str/None, "market_cap":
    float/None, "analyst_consensus": {buy/hold/sell/target_*}}}. Uses caches
    only -- never blocks on network inside the hot scoring path (the two
    per-ticker lazy refreshes, sector profiles + analyst consensus, are
    budgeted to ~8s each on cold caches).
    """
    from trader import EARNINGS_CAL_FILE  # local import avoids a cycle
    earnings = {}
    try:
        cal_path = EARNINGS_CAL_FILE
        if os.path.exists(cal_path):
            with open(cal_path) as f:
                earnings = json.load(f)
    except (OSError, json.JSONDecodeError):
        earnings = {}

    analyst = _load_cache("analyst_actions.json", 6) or {"actions": []}
    insider = _load_cache("insider_activity.json", 24) or {"by_ticker": {}}
    sec = _load_cache("sec_filings.json", 24) or {"by_ticker": {}}
    reddit = _load_cache("reddit_sentiment.json", 6) or {"by_ticker": {}}
    sectors = get_sector_profiles(tickers)
    consensus = get_analyst_consensus(tickers)

    by_analyst = {}
    for a in analyst.get("actions", []):
        sym = a.get("symbol")
        if sym and a.get("action") in ("upgrade", "downgrade"):
            by_analyst[sym] = a["action"]

    signals = {}
    today = datetime.now().date()
    for t in tickers:
        edate = earnings.get(t)
        days = None
        if edate:
            try:
                days = (datetime.strptime(edate, "%Y-%m-%d").date() - today).days
            except ValueError:
                days = None
        sp = sectors.get(t, {}) or {}
        signals[t] = {
            "analyst": by_analyst.get(t),
            "insider_net": (insider.get("by_ticker", {}).get(t) or {}).get("net_buy_value", 0.0),
            "reddit_sentiment": reddit.get("by_ticker", {}).get(t),
            "recent_filings": [f.get("form") for f in sec.get("by_ticker", {}).get(t, [])],
            "days_until_earnings": days,
            "sector": sp.get("sector"),
            "market_cap": sp.get("market_cap"),
            "analyst_consensus": consensus.get(t, {}),
        }
    return signals


def get_context_block(tickers, include_econ=True):
    """
    Human-readable Phase 2 context for the Gemini prompt:
    market pulse (indexes/sectors/movers), high-impact economic events,
    analyst actions, insider buys, reddit sentiment, and SEC filings for the
    given tickers. Pure cache reads except the hourly market-pulse refresh.
    """
    lines = []

    # Market pulse: indexes + sector rotation (cached hourly, ~15 calls once
    # per hour) plus movers/breadth from today's scan cache (zero API cost).
    pulse = get_market_pulse()
    if pulse:
        idx = [f"{k} {pulse[f'{k}_today']:+.2f}%" for k in ("SPY", "QQQ", "IWM", "DIA") if pulse.get(f"{k}_today") is not None]
        if idx:
            lines.append("Market pulse - indexes today: " + ", ".join(idx))
        mom = [f"{k} {pulse[f'{k}_mom5']:+.1f}%" for k in ("SPY", "QQQ", "IWM", "DIA") if pulse.get(f"{k}_mom5") is not None]
        if mom:
            lines.append("Index momentum (5d): " + ", ".join(mom))
        macro = [f"{label} {pulse[f'{s}_today']:+.2f}%" for s, label in _MACRO_ETFS.items() if pulse.get(f"{s}_today") is not None]
        if macro:
            lines.append("Macro: " + ", ".join(macro))
        vol_parts = [f"{k} {pulse[f'{k}_vol20']:.0f}% 20d vol" for k in ("SPY", "QQQ") if pulse.get(f"{k}_vol20") is not None]
        if vol_parts:
            lines.append("Market volatility: " + ", ".join(vol_parts))
        sectors = [(name, pulse[f"{s}_today"]) for s, name in _SECTOR_ETFS.items() if pulse.get(f"{s}_today") is not None]
        sectors.sort(key=lambda x: x[1], reverse=True)
        if sectors:
            lines.append("Sector rotation: strong " + ", ".join(f"{n} {p:+.1f}%" for n, p in sectors[:3]))
            lines.append("Sector rotation: weak " + ", ".join(f"{n} {p:+.1f}%" for n, p in sectors[-3:]))
    up, down, up_count, down_count = _scan_movers()
    if up:
        lines.append("Scan movers UP today: " + ", ".join(f"{t} {p:+.1f}%" for t, p in up))
    if down:
        lines.append("Scan movers DOWN today: " + ", ".join(f"{t} {p:+.1f}%" for t, p in down))
    if up_count or down_count:
        lines.append(f"Scan breadth: {up_count} up / {down_count} down ({up_count + down_count} scanned)")

    if include_econ:
        cal = _load_cache("eco_calendar.json", 6)
        if cal:
            events = cal.get("events", [])
            upcoming = []
            for ev in events[:10]:
                impact = (ev.get("impact") or "").lower()
                if impact in ("high", "market"):
                    upcoming.append(
                        f"{ev.get('date', '')[:10]} {ev.get('event')} (impact {impact}, "
                        f"forecast {ev.get('forecast', '')})"
                    )
            if upcoming:
                lines.append("Economic calendar (high-impact): " + "; ".join(upcoming[:5]))
            hi = cal.get("high_impact_today")
            if hi:
                lines.append(f"!! HIGH-IMPACT EVENT TODAY: {hi} -- be defensive, size down.")

    analyst = _load_cache("analyst_actions.json", 6) or {"actions": []}
    acts = [a for a in analyst.get("actions", []) if a.get("symbol") in tickers]
    if acts:
        parts = [f"{a['symbol']} {a['action']} ({a.get('to_grade')})" for a in acts[:8]]
        lines.append("Analyst actions: " + ", ".join(parts))

    insider = _load_cache("insider_activity.json", 24) or {"by_ticker": {}}
    ins_parts = []
    for t in tickers:
        info = insider.get("by_ticker", {}).get(t)
        if info and info.get("net_buy_value", 0) > 0:
            ins_parts.append(f"{t} insider net buy ${info['net_buy_value']:,.0f}")
        elif info and info.get("net_buy_value", 0) < 0:
            ins_parts.append(f"{t} insider net sell ${-info['net_buy_value']:,.0f}")
    if ins_parts:
        lines.append("Insider activity: " + ", ".join(ins_parts[:8]))

    reddit = _load_cache("reddit_sentiment.json", 6) or {"by_ticker": {}}
    red_parts = []
    for t in tickers:
        s = reddit.get("by_ticker", {}).get(t)
        if s is not None and abs(s) >= 0.15:
            red_parts.append(f"{t} reddit {s:+.2f}")
    if red_parts:
        lines.append("Reddit sentiment: " + ", ".join(red_parts[:8]))

    sec = _load_cache("sec_filings.json", 24) or {"by_ticker": {}}
    sec_parts = []
    for t in tickers:
        forms = [f.get("form") for f in sec.get("by_ticker", {}).get(t, [])]
        if forms:
            sec_parts.append(f"{t} filed {','.join(forms)}")
    if sec_parts:
        lines.append("Recent SEC filings: " + "; ".join(sec_parts[:8]))

    # Per-ticker sector + market cap, so Gemini can tie each candidate to the
    # sector rotation read above. Pure cache read (refresh happens lazily in
    # get_sector_profiles, budgeted at 8s / 8 tickers, 24h cache).
    sp = _load_cache("sector_profiles.json", 24) or {"by_ticker": {}}
    sp_parts = []
    for t in tickers:
        info = sp.get("by_ticker", {}).get(t)
        if info and info.get("sector"):
            mc = info.get("market_cap")
            mc_s = ""
            if isinstance(mc, (int, float)) and mc:
                if mc >= 1_000_000:
                    mc_s = f" ${mc / 1_000_000:.2f}T"
                elif mc >= 1_000:
                    mc_s = f" ${mc / 1_000:.0f}B"
                else:
                    mc_s = f" ${mc:.0f}M"
            sp_parts.append(f"{t}: {info['sector']}{mc_s}")
    if sp_parts:
        lines.append("Sector/market cap: " + ", ".join(sp_parts[:10]))

    # Street consensus + price targets per candidate (Finnhub /stock/metrics,
    # free tier, cached 24h, budgeted) -- the "what does Wall Street think"
    # read. Target vs the candidate's current price is computed by Gemini,
    # which sees both in the prompt.
    cons = _load_cache("analyst_consensus.json", 24) or {"by_ticker": {}}
    cons_parts = []
    for t in tickers:
        c = cons.get("by_ticker", {}).get(t) or {}
        if c.get("buy") is None:
            continue
        cons_parts.append(
            f"{t} {c['buy']}B/{c.get('hold') or 0}H/{c.get('sell') or 0}S"
            + (f" PT ${c['target_mean']:.2f}" if c.get("target_mean") else "")
            + (f" (hi ${c['target_high']:.2f}/lo ${c['target_low']:.2f})" if c.get("target_high") and c.get("target_low") else "")
        )
    if cons_parts:
        lines.append("Street consensus/targets: " + ", ".join(cons_parts[:10]))

    if not lines:
        return "none"
    return "\n".join(lines)


# Lazily imported SP500 for the reddit matcher (avoids a hard import cycle).
_SP500_NAMES = None


def _get_sp500():
    global _SP500_NAMES
    if _SP500_NAMES is None:
        from sp500_data import SP500
        _SP500_NAMES = SP500
    return _SP500_NAMES
