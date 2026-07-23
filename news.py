"""
Pulls recent market news from Finnhub, then figures out which S&P 500
companies are actually mentioned in that news (this is how the bot
"dynamically picks" stocks instead of using a fixed watchlist).
"""

import re
import requests
from config import FINNHUB_API_KEY, MAX_NEWS_ITEMS
from sp500_data import SP500


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


def find_mentioned_tickers(articles):
    """
    Scan article headlines + summaries for S&P 500 tickers or company names.
    Returns a dict: {ticker: [matching article dicts]}
    """
    mentions = {}

    # Precompute cleaned names once
    cleaned = [(ticker, name, _clean_company_name(name)) for ticker, name in SP500]

    for article in articles:
        text = f"{article.get('headline', '')} {article.get('summary', '')}"
        text_lower = text.lower()

        for ticker, full_name, short_name in cleaned:
            if not short_name:
                continue
            # Match either the cleaned company name or a standalone ticker mention
            name_hit = short_name.lower() in text_lower and len(short_name) > 3
            ticker_hit = bool(re.search(rf"\b{re.escape(ticker)}\b", text))

            if name_hit or ticker_hit:
                mentions.setdefault(ticker, []).append({
                    "headline": article.get("headline", ""),
                    "summary": article.get("summary", "")[:300],
                    "source": article.get("source", ""),
                    "url": article.get("url", ""),
                })

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
        print(f"{ticker}: {items[0]['headline']}")
