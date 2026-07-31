"""
Pulls recent market news from Finnhub, matches it to S&P 500 tickers, and
filters out articles already seen in a recent prior run (duplicate-news
detection) so the bot doesn't repeatedly react to the same headline every
few minutes while it's still the top story.
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


def fetch_market_news():
    """Get general market news from Finnhub. Returns a list of dicts."""
    url = "https://finnhub.io/api/v1/news"
    params = {"category": "general", "token": FINNHUB_API_KEY}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    articles = resp.json()
    return articles[:MAX_NEWS_ITEMS * 4]  # grab extra since we filter down


def _clean_company_name(name):
    """Strip corporate suffixes so 'Apple Inc.' matches text saying just 'Apple'."""
    name = re.sub(
        r"\b(Inc\.?|Corporation|Corp\.?|Company|Co\.?|plc|Holdings?|Group|The)\b",
        "", name, flags=re.IGNORECASE,
    )
    name = re.sub(r"[,\(\)\.]", "", name)   # drop stray commas, parens, and leftover periods
    return re.sub(r"\s+", " ", name).strip()  # collapse any resulting double spaces


def _article_id(article):
    """A stable ID for an article, used for duplicate detection."""
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


def find_mentioned_tickers(articles, seen_news):
    """
    Scans article headlines + summaries for S&P 500 tickers or company
    names, skipping articles already in `seen_news`.
    Returns (mentions dict, list of newly-seen article IDs to record).
    """
    mentions = {}
    newly_seen = []
    cleaned = [(ticker, name, _clean_company_name(name)) for ticker, name in SP500]

    for article in articles:
        aid = _article_id(article)
        if aid in seen_news:
            continue  # already reacted to this exact article in a recent run
        newly_seen.append(aid)

        text = f"{article.get('headline', '')} {article.get('summary', '')}"
        text_lower = text.lower()

        for ticker, full_name, short_name in cleaned:
            if not short_name:
                continue
            name_hit = short_name.lower() in text_lower and len(short_name) > 3
            ticker_hit = bool(re.search(rf"\b{re.escape(ticker)}\b", text))
            if name_hit or ticker_hit:
                mentions.setdefault(ticker, []).append({
                    "headline": article.get("headline", ""),
                    "summary": article.get("summary", "")[:300],
                    "source": article.get("source", ""),
                    "url": article.get("url", ""),
                })

    return mentions, newly_seen


def get_news_candidates():
    """
    Main entry point: fetch news, filter out already-seen articles, return
    {ticker: [article info]} for every newly-mentioned S&P 500 company.
    Also persists the updated "seen" list to disk.
    """
    seen = _prune_old_entries(_load_seen_news())
    articles = fetch_market_news()
    mentions, newly_seen = find_mentioned_tickers(articles, seen)

    now_iso = datetime.now().isoformat()
    for aid in newly_seen:
        seen[aid] = now_iso
    _save_seen_news(seen)

    return mentions


if __name__ == "__main__":
    candidates = get_news_candidates()
    print(f"Found {len(candidates)} newly-mentioned companies:\n")
    for ticker, items in list(candidates.items())[:10]:
        print(f"{ticker}: {items[0]['headline']}")
