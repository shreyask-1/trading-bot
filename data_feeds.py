"""
Phase 2 data feeds: everything beyond price/news that a pro daytrader watches.

  - Economic calendar (CPI, FOMC, NFP, GDP, PCE ...)   -- Finnhub /calendar/economic
  - Analyst upgrades / downgrades                       -- Finnhub /stock/upgrade-downgrade
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
from datetime import datetime, timedelta

import requests

from config import FINNHUB_API_KEY

BASE_DIR = os.path.dirname(__file__)
LOG_DIR = os.path.join(BASE_DIR, "logs")

_HIGH_IMPACT_EVENTS = (
    "cpi", "consumer price index", "fomc", "fed funds", "nonfarm", "non-farm",
    "payroll", "employment situation", "gdp", "pce", "core pce", "jobs report",
    "unemployment", "interest rate decision",
)
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
    """
    cached = _load_cache("eco_calendar.json", 6)
    if cached:
        return cached
    events = []
    try:
        today = datetime.now().date().isoformat()
        end = (datetime.now().date() + timedelta(days=14)).isoformat()
        resp = requests.get(
            "https://finnhub.io/api/v1/calendar/economic",
            params={"from": today, "to": end, "token": FINNHUB_API_KEY},
            timeout=15,
        )
        resp.raise_for_status()
        events = resp.json().get("economicCalendar", []) or []
    except Exception as e:
        print(f"Economic calendar fetch failed (trading continues without it): {e}")
        return {"events": [], "high_impact_today": None}

    high_impact_today = None
    today_str = datetime.now().date().isoformat()
    for ev in events:
        # Only flag events that are still UPCOMING (no actual released yet) --
        # once CPI/NFP actually prints, the uncertainty is gone.
        if ev.get("actual"):
            continue
        date = (ev.get("date") or "")[:10]
        event = ev.get("event", "")
        impact = (ev.get("impact") or "").lower()
        is_high = impact in ("high", "market") or any(k in event.lower() for k in _HIGH_IMPACT_EVENTS)
        if date == today_str and is_high:
            high_impact_today = f"{event} ({impact} impact) forecast {ev.get('forecast', '')}".strip()
            break
    result = {"events": events, "high_impact_today": high_impact_today}
    _save_cache("eco_calendar.json", result)
    return result


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
    """
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
def fetch_insider_activity(tickers):
    """
    For each ticker, the last ~90 days of insider transactions, summarized as
    {ticker: {"net_buy_value": $, "buy_count": n, "sell_count": n}}.
    Only fetches tickers not already in the daily cache. Best-effort.
    """
    cached = _load_cache("insider_activity.json", 24)
    store = dict(cached.get("by_ticker", {})) if cached else {}
    need = [t for t in tickers if t and t not in store]
    for t in need[:20]:
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


def get_insider_activity(tickers):
    """Cache-or-fetch wrapper used by the scoring engine (never raises)."""
    try:
        return fetch_insider_activity(tickers)
    except Exception as e:
        print(f"Insider activity unavailable: {e}")
        return {}


# ============================================================
# SEC filings (per-ticker, cached daily)
# ============================================================
def fetch_sec_filings(tickers):
    """
    Recent SEC filings per ticker: {ticker: [{"form", "date", "title"}...]},
    keeping only forms that actually matter for a trade decision
    (8-K current events, 10-Q/10-K earnings, Form 4 insider trades).
    """
    cached = _load_cache("sec_filings.json", 24)
    store = dict(cached.get("by_ticker", {})) if cached else {}
    relevant = ("8-K", "10-Q", "10-K", "4")
    need = [t for t in tickers if t and t not in store]
    for t in need[:20]:
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


def get_sec_filings(tickers):
    """Cache-or-fetch wrapper (never raises)."""
    try:
        return fetch_sec_filings(tickers)
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
# Assembled blocks for prompts / scoring
# ============================================================
def get_fundamental_signals(tickers):
    """
    Everything Phase 2 knows about the given tickers right now, as a compact
    dict: {ticker: {"analyst": "upgrade"/"downgrade"/None, "insider_net": $,
    "reddit_sentiment": -1..+1, "recent_filings": ["8-K", ...],
    "days_until_earnings": int/None}}. Uses caches only -- never blocks on
    network inside the hot scoring path.
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
        signals[t] = {
            "analyst": by_analyst.get(t),
            "insider_net": (insider.get("by_ticker", {}).get(t) or {}).get("net_buy_value", 0.0),
            "reddit_sentiment": reddit.get("by_ticker", {}).get(t),
            "recent_filings": [f.get("form") for f in sec.get("by_ticker", {}).get(t, [])],
            "days_until_earnings": days,
        }
    return signals


def get_context_block(tickers, include_econ=True):
    """
    Human-readable Phase 2 context for the Gemini prompt:
    high-impact economic events, analyst actions, insider buys, reddit
    sentiment, and SEC filings for the given tickers. Pure cache reads.
    """
    lines = []

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
