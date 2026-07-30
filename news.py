"""
Pulls recent market news from Finnhub, isolates S&P 500 company mentions, 
and ranks high-impact news catalysts while filtering out market noise.
"""

import re
import requests
from config import FINNHUB_API_KEY, MAX_NEWS_ITEMS
from sp500_data import SP500

# High-impact catalyst categories and their relative weightings
CATALYST_PATTERNS = {
    "EARNINGS_GUIDANCE": (r"\b(earnings|revenue|eps|guidance|quarterly results|profit margin)\b", 1.5),
    "FDA_REGULATORY": (r"\b(fda|approval|phase 3|clinical trial|clearance|patent)\b", 1.4),
    "MA_CORPORATE": (r"\b(merger|acquisition|buyout|takeover|spin-off|divestiture)\b", 1.3),
    "ANALYST_RATING": (r"\b(upgraded|downgraded|price target|outperform|strong buy)\b", 1.2),
    "EXECUTIVE_LEADERSHIP": (r"\b(ceo|cfo|resigns|steps down|appointed|named CEO)\b", 1.1),
}

# Generic noise keywords to drop low-conviction articles
NOISE_PATTERNS = r"\b(why it matters|3 reasons to buy|top stocks for today|what to watch|market recap)\b"


def fetch_market_news():
    """Get general market news from Finnhub. Returns a list of dicts."""
    url = "https://finnhub.io/api/v1/news"
    params = {"category": "general", "token": FINNHUB_API_KEY}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    articles = resp.json()
    return articles[:MAX_NEWS_ITEMS * 4]  # grab extra since we'll filter down


def _clean_company_name(name):
    """Strip corporate suffixes so 'Apple Inc.' matches text saying just 'Apple'."""
    name = re.sub(
        r"\b(Inc\.?|Corporation|Corp\.?|Company|Co\.?|plc|Holdings?|Group|The)\b",
        "",
        name,
        flags=re.IGNORECASE,
    )
    name = re.sub(r"[,\(\)]", "", name).strip()
    return name


def classify_and_score_catalyst(text):
    """
    Evaluates news text against catalyst patterns and noise filters.
    Returns (catalyst_type, impact_multiplier).
    """
    if re.search(NOISE_PATTERNS, text, flags=re.IGNORECASE):
        return "NOISE", 0.5

    best_type = "GENERAL_NEWS"
    best_weight = 1.0

    for catalyst_type, (pattern, weight) in CATALYST_PATTERNS.items():
        if re.search(pattern, text, flags=re.IGNORECASE):
            if weight > best_weight:
                best_type = catalyst_type
                best_weight = weight

    return best_type, best_weight


def find_mentioned_tickers(articles):
    """
    Scan article headlines + summaries for S&P 500 tickers or company names,
    ranking items by catalyst score.
    Returns a dict: {ticker: [matching article dicts sorted by score]}
    """
    mentions = {}

    # Precompute cleaned names once
    cleaned = [(ticker, name, _clean_company_name(name)) for ticker, name in SP500]

    for article in articles:
        headline = article.get("headline", "")
        summary = article.get("summary", "")[:300]
        text = f"{headline} {summary}"
        text_lower = text.lower()

        catalyst_type, catalyst_weight = classify_and_score_catalyst(text)

        for ticker, full_name, short_name in cleaned:
            if not short_name:
                continue
            # Match either the cleaned company name or a standalone ticker mention
            name_hit = short_name.lower() in text_lower and len(short_name) > 3
            ticker_hit = bool(re.search(rf"\b{re.escape(ticker)}\b", text))

            if name_hit or ticker_hit:
                mentions.setdefault(ticker, []).append({
                    "headline": headline,
                    "summary": summary,
                    "source": article.get("source", ""),
                    "url": article.get("url", ""),
                    "catalyst_type": catalyst_type,
                    "catalyst_weight": catalyst_weight
                })

    # Sort each ticker's articles by highest catalyst weight first
    for ticker in mentions:
        mentions[ticker].sort(key=lambda x: x["catalyst_weight"], reverse=True)

    return mentions


def get_news_candidates():
    """
    Main entry point: fetch news, return a dict of
    {ticker: [article info]} for every S&P 500 company mentioned recently.
    """
    articles = fetch_market_news()
    mentions = find_mentioned_tickers(articles)
    return mentions


if __name__ == "__main__":
    # Quick manual test: run `python3 news.py` to see what it finds right now
    candidates = get_news_candidates()
    print(f"Found {len(candidates)} companies mentioned in today's news:\n")
    for ticker, items in list(candidates.items())[:10]:
        first = items[0]
        print(f"{ticker} [{first['catalyst_type']}]: {first['headline']}")
