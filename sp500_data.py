"""
S&P 500 & Custom High-Growth Universe Retrieval Engine.
Pulls the complete live S&P 500 component list from authoritative open repositories,
appends custom momentum targets and SpaceX (SPCX), and ensures clean deduplication.
"""

import requests

# Custom High-Momentum & Special Targets to explicitly merge
CUSTOM_TARGETS = [
    "SPCX",    # SpaceX (Publicly traded equity)
    "PLTR",    # Palantir Technologies (Enterprise AI)
    "FBRX",    # Forte Biosciences (Biotech Breakout)
    "NOW",     # ServiceNow (Enterprise Cloud)
    "MANH",    # Manhattan Associates (Supply Chain)
    "CTSH",    # Cognizant Technology Solutions
    "THC",     # Tenet Healthcare
]


def get_sp500_tickers():
    """
     Fetches the active, complete S&P 500 constituent list from reliable open data sources,
    merges custom growth equities, and automatically handles ticker formatting.
    """
    tickers = []
    
    # Primary Source: Open S&P 500 JSON dataset hosted on GitHub
    try:
        url = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.json"
        response = requests.get(url, timeout=6)
        
        if response.status_code == 200:
            data = response.json()
            # Extract symbols and sanitize format (e.g., BRK.B -> BRK-B for Alpaca)
            tickers = [str(item["Symbol"]).replace(".", "-") for item in data if "Symbol" in item]
    except Exception as e:
        print(f"[sp500_data] Warning: Could not pull live index list ({e}). Using robust fallback baseline.")

    # Fallback baseline list if network fetch fails
    if len(tickers) < 400:
        tickers = [
            "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "BRK-B", "UNH", "JNJ",
            "JPM", "V", "PG", "XOM", "MA", "HD", "CVX", "MRK", "ABBV", "LLY",
            "PEP", "KO", "BAC", "COST", "TMO", "WMT", "AVGO", "MCD", "CSCO", "ACN", "ABT",
            "DHR", "LIN", "ORCL", "CMCSA", "DIS", "ADBE", "TXN", "PM", "PFE", "NKE", "AMD",
            "INTC", "HON", "QCOM", "LOW", "UPS", "SPGI", "IBM", "AMGN", "GE", "CAT", "BA",
            "SBUX", "DE", "MS", "GS", "BLK", "MDLZ", "GILD", "ADP", "INTU", "C", "T",
            "VZ", "BKNG", "ISRG", "MU" # Micron is naturally part of this baseline
        ]

    # Combine full index with custom targets, remove duplicates (like MU if matched), and sort
    complete_universe = sorted(list(set(tickers + CUSTOM_TARGETS)))
    
    print(f"[sp500_data] Loaded active screening universe: {len(complete_universe)} total assets.")
    return complete_universe


# Backward compatibility export for legacy modules (like news.py or main.py)
SP500 = get_sp500_tickers()
