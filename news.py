"""
News Catalyst Data Engine (Dual-Source: Alpaca + Finnhub).
Retrieves live headlines and market catalysts from both APIs concurrently.
"""

import os
import requests
from datetime import datetime, timedelta
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import NewsRequest
from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, FINNHUB_API_KEY

# Initialize Alpaca Data Client
alpaca_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY) if ALPACA_API_KEY else None


def fetch_alpaca_news(ticker, limit=5):
    """Fetches recent news from Alpaca Market News API."""
    if not alpaca_client:
        return []
    try:
        request = NewsRequest(symbols=ticker, limit=limit)
        news_set = alpaca_client.get_news(request)
        articles = news_set.news if hasattr(news_set, 'news') else news_set.get(ticker, [])
        
        results = []
        for article in articles:
            results.append({
                "headline": getattr(article, "headline", ""),
                "summary": getattr(article, "summary", ""),
                "source": f"Alpaca ({getattr(article, 'source', 'News')})",
                "created_at": str(getattr(article, "created_at", ""))
            })
        return results
    except Exception as e:
        print(f"[News Engine] Alpaca fetch error for {ticker}: {e}")
        return []


def fetch_finnhub_news(ticker, days_back=3):
    """Fetches company news from Finnhub API."""
    if not FINNHUB_API_KEY:
        return []
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        from_date = (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%d')
        
        url = f"https://finnhub.io/api/v1/company-news?symbol={ticker}&from={from_date}&to={today}&token={FINNHUB_API_KEY}"
        response = requests.get(url, timeout=5)
        
        if response.status_code != 200:
            return []
            
        articles = response.json()
        results = []
        for article in articles[:5]:
            results.append({
                "headline": article.get("headline", ""),
                "summary": article.get("summary", ""),
                "source": f"Finnhub ({article.get('source', 'News')})",
                "created_at": datetime.fromtimestamp(article.get("datetime", 0)).strftime('%Y-%m-%d %H:%M:%S')
            })
        return results
    except Exception as e:
        print(f"[News Engine] Finnhub fetch error for {ticker}: {e}")
        return []


def get_ticker_news(ticker, limit=5):
    """
    Runs both Alpaca and Finnhub in parallel, combines headlines,
    deduplicates identical stories, and returns up to `limit` items.
    """
    alpaca_items = fetch_alpaca_news(ticker, limit=limit)
    finnhub_items = fetch_finnhub_news(ticker, days_back=3)
    
    # Merge both news feeds
    combined = alpaca_items + finnhub_items
    
    # Deduplicate based on headline similarity
    seen_headlines = set()
    unique_news = []
    
    for item in combined:
        clean_headline = item["headline"].strip().lower()
        if clean_headline and clean_headline not in seen_headlines:
            seen_headlines.add(clean_headline)
            unique_news.append(item)
            
    if unique_news:
        return unique_news[:limit]
    
    # Fallback if both return no headlines
    return [{
        "headline": f"No recent headlines found for {ticker}",
        "summary": "No active catalyst news reported from Alpaca or Finnhub.",
        "source": "System",
        "created_at": datetime.now().strftime('%Y-%m-%d')
    }]


# Compatibility aliases for legacy calls
get_news = get_ticker_news
fetch_news_for_ticker = get_ticker_news
