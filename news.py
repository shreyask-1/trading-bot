"""
Pulls recent market news from Finnhub, matches headlines against S&P 500 tickers,
and dedups recently seen articles.
"""

import re
import os
import json
import hashlib
import html as html_lib
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
import requests

from config import (
    FINNHUB_API_KEY,
    MAX_NEWS_ITEMS,
    NEWS_DEDUP_MAX_AGE_HOURS,
    NEWS_MIN_SCORE_TO_CONSIDER,
    ENABLE_RSS_NEWS,
    RSS_FETCH_TIMEOUT_SECONDS,
)
from sp500_data import SP500

SEEN_NEWS_FILE = os.path.join(os.path.dirname(__file__), "logs", "seen_news.json")
# Per-ticker worst sentiment from the last news fetch; the risk engine uses it
# for negative-news exits without making one API call per holding.
NEWS_SENTIMENT_CACHE_FILE = os.path.join(os.path.dirname(__file__), "logs", "news_sentiment_cache.json")
MIN_TICKER_LEN_FOR_DIRECT_MATCH = 2

# --- Article importance scoring (0-10) ---
# Not every mention is a tradeable event. "Apple announces AI partnership"
# scores ~9; "Apple CEO interview" scores ~3; "Apple opens new store" ~1.
# Only high-scoring articles survive the filter in get_news_candidates.
_HIGH_IMPACT_POS = {
    "beats", "surpasses", "record", "partnership", "partners", "merger",
    "acquisition", "acquire", "breakthrough", "fda", "approval", "approved",
    "upgraded", "upgrade", "price target", "outperform", "buy rating",
    "soars", "surges", "all-time high", "raises guidance", "raises forecast",
}
_MEDIUM_IMPACT_POS = {
    "launch", "launches", "expansion", "expands", "profit", "profits",
    "strong", "stronger", "contract", "wins", "deal", "growth", "raise",
    "raises", "boost", "boosts", "beats expectations", "new product",
}
_LOW_IMPACT_POS = {
    "interview", "announces", "announcement", "appoints",
    "appointment", "ceo", "comments", "speech", "conference",
    "hires", "named", "joins", "speaks",
}
_HIGH_IMPACT_NEG = {
    "miss", "misses", "plunge", "plunges", "collapse", "fraud", "lawsuit",
    "investigation", "recall", "bankruptcy", "bankrupt", "downgraded",
    "downgrade", "criminal", "probe", "warns", "warning", "halted", "restated",
}
_MEDIUM_IMPACT_NEG = {
    "cuts", "cut", "below", "weak", "weaker", "decline",
    "declines", "drop", "drops", "fall", "falls", "loss", "losses",
    "layoff", "layoffs", "underperform", "sell rating", "misses estimates",
}
_LOW_IMPACT_NEG = {"delay", "delays", "halt", "negative", "uncertainty", "probe"}


def score_article(article):
    """
    Importance score 0-10 for a single article (headline + summary). Deterministic
    keyword weighting: real catalysts (earnings beats, partnerships, approvals,
    analyst actions) score high; filler (interviews, store openings) scores low.
    """
    text = f"{article.get('headline', '')} {article.get('summary', '')}".lower()
    if not text.strip():
        return 0.0
    score = 2.0  # baseline: ordinary news
    score += 4.0 * sum(1 for w in _HIGH_IMPACT_POS if w in text)
    score += 2.0 * sum(1 for w in _MEDIUM_IMPACT_POS if w in text)
    score += 0.5 * sum(1 for w in _LOW_IMPACT_POS if w in text)
    score -= 4.0 * sum(1 for w in _HIGH_IMPACT_NEG if w in text)
    score -= 2.0 * sum(1 for w in _MEDIUM_IMPACT_NEG if w in text)
    score -= 0.5 * sum(1 for w in _LOW_IMPACT_NEG if w in text)
    return round(max(0.0, min(10.0, score)), 1)

def fetch_market_news(timeout=15.0):
    url = "https://finnhub.io/api/v1/news"
    params = {"category": "general", "token": FINNHUB_API_KEY}
    resp = requests.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    articles = resp.json()
    # Bulletproof against a 200 with a non-list body (an empty dict or a
    # rate-limit page instead of a JSON array): slicing a dict raises
    # TypeError and kills the whole news step. Empty list = no news this
    # run; the watchlist scan still covers the universe.
    if not isinstance(articles, list):
        return []
    return articles[: MAX_NEWS_ITEMS * 4]

def _clean_company_name(name):
    name = re.sub(
        r"\b(Inc\.?|Corporation|Corp\.?|Company|Co\.?|plc|Holdings?|Group|The)\b",
        "",
        name,
        flags=re.IGNORECASE,
    )
    name = re.sub(r"[,\(\)\.]", "", name)
    return re.sub(r"\s+", " ", name).strip()

def _article_id(article):
    key = article.get("url") or f"{article.get('headline', '')}|{article.get('datetime', '')}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()

def _load_seen_news():
    if not os.path.exists(SEEN_NEWS_FILE):
        return {}
    try:
        with open(SEEN_NEWS_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

def _save_seen_news(seen):
    os.makedirs(os.path.dirname(SEEN_NEWS_FILE), exist_ok=True)
    with open(SEEN_NEWS_FILE, "w") as f:
        json.dump(seen, f)

def _prune_old_entries(seen):
    cutoff = datetime.now() - timedelta(hours=NEWS_DEDUP_MAX_AGE_HOURS)
    out = {}
    for aid, ts in seen.items():
        try:
            if datetime.fromisoformat(ts) > cutoff:
                out[aid] = ts
        except (TypeError, ValueError):
            # A malformed timestamp (corrupt file / partial write) must never
            # crash the whole news step -- drop the entry and move on.
            continue
    return out

# Lightweight lexicon for headline sentiment (-1 .. +1). This is a crude
# directional proxy, not a model: it only needs to nudge the quant score for
# news candidates, and it is deterministic (no LLM calls, free).
_POSITIVE_WORDS = {
    "beat", "beats", "surge", "surges", "soar", "soars", "rally", "rallies",
    "gain", "gains", "jump", "jumps", "climb", "climbs", "record", "outperform",
    "upgrade", "upgraded", "buy", "bullish", "growth", "profit", "profits",
    "strong", "stronger", "raised", "raise", "launch", "launches", "wins",
    "win", "partnership", "expansion", "boost", "boosts", "soaring",
    "positive", "record-high", "all-time high", "upgraded", "outlook",
}
_NEGATIVE_WORDS = {
    "miss", "misses", "plunge", "plunges", "drop", "drops", "fall", "falls",
    "decline", "declines", "downgrade", "downgraded", "sell", "bearish",
    "loss", "losses", "weak", "weaker", "cut", "cuts", "lawsuit", "probe",
    "investigation", "fraud", "recall", "recalls", "warning", "warns",
    "layoff", "layoffs", "bankrupt", "bankruptcy", "guidance", "below",
    "underperform", "negative", "halt", "halted", "delay", "delays",
}


def headline_sentiment(article):
    """
    Crude -1..+1 sentiment for a single article's headline+summary.
    0.0 means neutral or no signal. Deterministic keyword match only.
    """
    text = f"{article.get('headline', '')} {article.get('summary', '')}".lower()
    if not text.strip():
        return 0.0
    pos = sum(1 for w in _POSITIVE_WORDS if w in text)
    neg = sum(1 for w in _NEGATIVE_WORDS if w in text)
    if pos == 0 and neg == 0:
        return 0.0
    raw = (pos - neg) / (pos + neg)
    # Scale hits so a single strong word is ~+/-0.5, multiple corroborating
    # words push toward +/-1.0.
    return max(-1.0, min(1.0, raw * 0.5 + (0.5 if pos > neg else (-0.5 if neg > pos else 0.0))))


def find_mentioned_tickers(articles, seen_news):
    mentions = {}
    sentiment_by_ticker = {}
    newly_seen = []
    cleaned = [(ticker, name, _clean_company_name(name)) for ticker, name in SP500]

    for article in articles:
        aid = _article_id(article)
        if aid in seen_news:
            continue
        newly_seen.append(aid)

        text = f"{article.get('headline', '')} {article.get('summary', '')}"
        text_lower = text.lower()
        sent = headline_sentiment(article)

        for ticker, full_name, short_name in cleaned:
            name_hit = bool(short_name) and len(short_name) > 3 and short_name.lower() in text_lower
            ticker_hit = False
            if len(ticker) >= MIN_TICKER_LEN_FOR_DIRECT_MATCH:
                ticker_hit = bool(re.search(rf"\b{re.escape(ticker)}\b", text, flags=re.IGNORECASE))

            if name_hit or ticker_hit:
                mentions.setdefault(ticker, []).append({
                    "headline": article.get("headline", ""),
                    "summary": article.get("summary", "")[:300],
                    "source": article.get("source", ""),
                    "url": article.get("url", ""),
                    "sentiment": round(sent, 2),
                    "score": score_article(article),
                })
                sentiment_by_ticker[ticker] = sentiment_by_ticker.get(ticker, 0.0) + sent

    # Average sentiment across the ticker's matched articles.
    for t in sentiment_by_ticker:
        sentiment_by_ticker[t] = round(
            sentiment_by_ticker[t] / len(mentions.get(t, [])), 2
        )
    return mentions, newly_seen, sentiment_by_ticker


def fetch_rss_news(timeout=8.0):
    """
    Free RSS headline feeds (no API key, no Finnhub quota): Google News US
    top stories + Yahoo Finance index headlines. Parsed with the stdlib
    ElementTree; each item is normalized to the same article shape the
    Finnhub pipeline expects ({headline, summary, source, url, datetime}) so
    the ticker-matcher and importance scorer treat them identically.
    Fail-soft: returns [] on any error -- Finnhub coverage still runs.
    """
    feeds = [
        "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en",
        "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%5EGSPC%2C%5EIXIC%2C%5EDJI&region=US&lang=en-US",
    ]
    articles = []
    for url in feeds:
        try:
            resp = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
        except Exception as e:
            print(f"RSS feed {url} failed (continuing): {e}")
            continue
        for item in root.iter("item"):
            title = ""
            desc = ""
            link = ""
            pub = ""
            for child in item:
                tag = child.tag.split("}")[-1]  # strip namespace
                if tag == "title":
                    title = child.text or ""
                elif tag == "description":
                    desc = child.text or ""
                elif tag == "link":
                    link = child.text or ""
                elif tag == "pubDate":
                    pub = child.text or ""
            title = html_lib.unescape(re.sub(r"<[^>]+>", "", title)).strip()
            desc = html_lib.unescape(re.sub(r"<[^>]+>", "", desc)).strip()
            if not title:
                continue
            articles.append({
                "headline": title[:300],
                "summary": desc[:300],
                "source": "rss",
                "url": link,
                "datetime": pub,
            })
    return articles


def get_news_candidates(time_budget=None):
    """
    Returns (mentions, stats, sentiment_by_ticker). time_budget (wall-clock
    seconds) is an optional cap from main.py's run deadline: if the budget is
    already spent the fetch is skipped entirely, and the Finnhub HTTP call
    itself is capped to fit whatever time remains, so a slow news API can
    never push a run past the 1-minute cap.
    """
    if time_budget is not None and time_budget <= 0:
        return {}, {"articles_fetched": 0, "articles_new_after_dedup": 0, "tickers_matched": 0}, {}
    seen = _prune_old_entries(_load_seen_news())
    # Cap the single HTTP call to the remaining budget (min 2s so a healthy
    # fetch still has a real chance) instead of always allowing the full 15s.
    fetch_timeout = 15.0
    if time_budget is not None:
        fetch_timeout = max(2.0, min(15.0, time_budget))
    articles = fetch_market_news(timeout=fetch_timeout)
    # Free RSS feeds broaden coverage without spending Finnhub quota; any RSS
    # item that survives the same dedup/score pipeline just adds a candidate.
    if ENABLE_RSS_NEWS:
        rss_timeout = min(RSS_FETCH_TIMEOUT_SECONDS, fetch_timeout)
        try:
            rss_articles = fetch_rss_news(timeout=rss_timeout)
            if rss_articles and isinstance(rss_articles, list):
                articles = articles + rss_articles
        except Exception as e:
            print(f"RSS news fetch failed (continuing with Finnhub only): {e}")
    mentions, newly_seen, sentiment_by_ticker = find_mentioned_tickers(articles, seen)

    # Cache per-ticker WORST sentiment (from ALL matched articles, even ones
    # too low-score to trade) so the risk engine can exit on negative news.
    try:
        os.makedirs(os.path.dirname(NEWS_SENTIMENT_CACHE_FILE), exist_ok=True)
        worst = {}
        for t, arts in mentions.items():
            sents = [a.get("sentiment", 0.0) for a in arts]
            worst[t] = min(sents) if sents else 0.0
        with open(NEWS_SENTIMENT_CACHE_FILE, "w") as f:
            json.dump(worst, f)
    except Exception as e:
        print(f"Could not write news sentiment cache: {e}")

    # Better news filtering: keep only high-importance articles.
    if NEWS_MIN_SCORE_TO_CONSIDER > 0:
        filtered = {}
        for t, arts in mentions.items():
            kept = [a for a in arts if a.get("score", 0) >= NEWS_MIN_SCORE_TO_CONSIDER]
            if kept:
                filtered[t] = kept
        mentions = filtered

    now_iso = datetime.now().isoformat()
    for aid in newly_seen:
        seen[aid] = now_iso
    _save_seen_news(seen)

    stats = {
        "articles_fetched": len(articles),
        "articles_new_after_dedup": len(newly_seen),
        "tickers_matched": len(mentions),
    }
    return mentions, stats, sentiment_by_ticker
