"""
S&P 500 & Custom High-Momentum Universe Retrieval Engine.
Fetches S&P 500 components and appends target high-growth/breakout candidates.
"""

import pandas as pd
import requests

# Hardcoded custom targets to explicitly include alongside standard universe
CUSTOM_TARGETS = [
    "MU",      # Micron Technology (Semiconductors / Memory)
    "SPCX",    # SpaceX / Target Space Tech
    "PLTR",    # Palantir Technologies (Enterprise AI)
    "FBRX",    # Forte Biosciences (Biotech Breakout)
    "NOW",     # ServiceNow (Enterprise Cloud)
    "MANH",    # Manhattan Associates (Supply Chain)
    "CTSH",    # Cognizant Technology Solutions
    "THC",     # Tenet Healthcare
]

DEFAULT_FALLBACK = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
    "MU", "PLTR", "NOW", "MANH", "CTSH", "THC", "SPCX"
]


def get_sp500_tickers():
    """
    Scrapes the S&P 500 list from Wikipedia and merges custom targets.
    Returns a clean, deduplicated list of uppercase ticker symbols.
    """
    tickers = []
    try:
        url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        response = requests.get(url, headers=headers, timeout=10)
        
        if response.status_code == 200:
            tables = pd.read_html(response.text)
            df = tables[0]
            raw_tickers = df["Symbol"].tolist()
            tickers = [str(t).replace(".", "-") for t in raw_tickers]
        else:
            tickers = DEFAULT_FALLBACK

    except Exception as e:
        print(f"[sp500_data] Error fetching S&P 500 list from Wikipedia: {e}")
        tickers = DEFAULT_FALLBACK

    # Deduplicate and append custom momentum tickers
    all_tickers = sorted(list(set(tickers + CUSTOM_TARGETS)))
    return all_tickers


# Backward compatibility export for legacy scripts
SP500 = get_sp500_tickers()
