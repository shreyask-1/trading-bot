from alpaca.data.historical import NewsClient
from config import ALPACA_API_KEY, ALPACA_SECRET_KEY

news_client = NewsClient(api_key=ALPACA_API_KEY, secret_key=ALPACA_SECRET_KEY)

def get_ticker_news(ticker):
    try:
        news = news_client.get_news(ticker=ticker, limit=3)
        return [n.headline for n in news]
    except Exception as e:
        print(f"[News Engine] Alpaca fetch error for {ticker}: {e}")
        return []
