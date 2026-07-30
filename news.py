"""
News Catalyst Data Engine.
Retrieves recent headlines and context for candidate tickers.
"""

import os
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import NewsRequest
from config import ALPACA_API_KEY, ALPACA_SECRET_KEY

# Initialize Alpaca Data Client for news retrieval
data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY) if ALPACA_API_KEY else None


def get_ticker_news(ticker, limit=5):
    """
    Fetches recent news items for a specific ticker symbol.
    Returns a list of news summary dictionaries.
    """
    if not data_client:
        return [{"headline": f"No Alpaca API key configured for {ticker}", "summary": ""}]

    try:
        request = NewsRequest(symbols=ticker, limit=limit)
        news_set = data_client.get_news(request)
        
        # news_set returns a dictionary or object with .news attribute
        articles = news_set.news if hasattr(news_set, 'news') else news_set.get(ticker, [])
        
        results = []
        for article in articles:
            results.append({
                "headline": getattr(article, "headline", ""),
                "summary": getattr(article, "summary", ""),
                "source": getattr(article, "source", "Alpaca News"),
                "created_at": str(getattr(article, "created_at", ""))
            })
            
        return results if results else [{"headline": f"No recent headlines found for {ticker}", "summary": ""}]

    except Exception as e:
        print(f"[News Engine] Error fetching news for {ticker}: {e}")
        return [{"headline": f"Market news scan pending for {ticker}", "summary": f"Context error: {str(e)}"}]


# Alias for alternative import styles
get_news = get_ticker_news
fetch_news_for_ticker = get_ticker_news
