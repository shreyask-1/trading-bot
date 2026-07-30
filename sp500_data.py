"""
S&P 500 & Custom High-Momentum Universe Retrieval Engine.
Fetches S&P 500 components and appends target high-growth/breakout candidates.
"""

import pandas as pd

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


def get_sp500_tickers():
    """
    Scrapes the S&P 500 list from Wikipedia and merges custom targets.
    Returns a clean, deduplicated list of uppercase ticker symbols.
    """
    try:
        url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        tables = pd.read_html(url)
        df = tables[0]
        tickers = df["Symbol"].tolist()

        # Sanitize tickers (e.g., convert BRK.B to BRK-B for Alpaca compliance)
        tickers = [str(t).replace(".", "-") for t in tickers]

    except Exception as e:
        print(f"[sp500_data] Error fetching S&P 500 list from Wikipedia: {e}")
        # Fallback baseline list
        tickers = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA"]

    # Deduplicate and append custom momentum tickers
    all_tickers = sorted(list(set(tickers + CUSTOM_TARGETS)))
    return all_tickers


# Backward compatibility export for news.py and other legacy modules
SP500 = get_sp500_tickers()
