"""
Pulls recent market news from Finnhub, matches headlines against S&P 500 tickers,
and dedups recently seen articles.
"""

import re
import os
import json
import hashlib
from datetime import datetime, timedelta
import requests

from config import FINNHUB_API_KEY, MAX_NEWS_ITEMS, NEWS_DEDUP_MAX_AGE_HOURS
from sp500_data import SP500

SEEN_NEWS_FILE = os.path.join(os.path.dirname(__file__), "logs", "seen_news.json")
MIN_TICKER_LEN_FOR_DIRECT_MATCH = 2

def fetch_market_news():
    url = "https://finnhub.io/api/v1/news"
    params = {"category": "general", "token": FINNHUB_API_KEY}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    articles = resp.json()
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
    return {aid: ts for aid, ts in seen.items() if datetime.fromisoformat(ts) > cutoff}

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
                })
                sentiment_by_ticker[ticker] = sentiment_by_ticker.get(ticker, 0.0) + sent

    # Average sentiment across the ticker's matched articles.
    for t in sentiment_by_ticker:
        sentiment_by_ticker[t] = round(
            sentiment_by_ticker[t] / len(mentions.get(t, [])), 2
        )
    return mentions, newly_seen, sentiment_by_ticker


def get_news_candidates():
    seen = _prune_old_entries(_load_seen_news())
    articles = fetch_market_news()
    mentions, newly_seen, sentiment_by_ticker = find_mentioned_tickers(articles, seen)

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
