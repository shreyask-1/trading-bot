"""
S&P 500 & Custom High-Momentum Universe Retrieval Engine.
Uses a fast, reliable static base array with GitHub raw JSON fallback.
"""

import requests

# High-priority custom momentum & universe targets
CUSTOM_TARGETS = [
    "MU",      # Micron Technology
    "SPCX",    # Target Space / SpaceX
    "PLTR",    # Palantir Technologies
    "FBRX",    # Forte Biosciences
    "NOW",     # ServiceNow
    "MANH",    # Manhattan Associates
    "CTSH",    # Cognizant Technology Solutions
    "THC",     # Tenet Healthcare
]

# Core S&P 500 Baseline Universe (Fast, offline, zero scraping required)
STATIC_SP500 = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "BRK-B", "UNH", "JNJ",
    "JPM", "V", "PG", "XOM", "MA", "HD", "CVX", "MRK", "ABBV", "LLY",
    "PEP", "KO", "BAC", "COST", "TMO", "WMT", "AVGO", "MCD", "CSCO", "ACN", "ABT",
    "DHR", "LIN", "ORCL", "CMCSA", "DIS", "ADBE", "TXN", "PM", "PFE", "NKE", "AMD",
    "INTC", "HON", "QCOM", "LOW", "UPS", "SPGI", "IBM", "AMGN", "GE", "CAT", "BA",
    "SBUX", "DE", "MS", "GS", "BLK", "MDLZ", "GILD", "ADP", "INTU", "C", "T",
    "VZ", "BKNG", "ISRG", "NOW", "PLTR", "MU", "THC", "MANH", "CTSH"
]


def fetch_github_sp500():
    """Fetches clean S&P 500 JSON dataset from GitHub open datasets."""
    try:
        url = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.json"
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            data = response.json()
            return [item["Symbol"].replace(".", "-") for item in data if "Symbol" in item]
    except Exception as e:
        print(f"[sp500_data] GitHub remote fetch skipped: {e}")
    return []


def get_sp500_tickers():
    """
    Returns a unified, deduplicated list of tickers.
    Combines GitHub remote data (if available) or static base list with custom targets.
    """
    remote_tickers = fetch_github_sp500()
    base_universe = remote_tickers if len(remote_tickers) > 400 else STATIC_SP500
    
    # Merge, deduplicate, and sort
    all_tickers = sorted(list(set(base_universe + CUSTOM_TARGETS)))
    return all_tickers


# Backward compatibility export
SP500 = get_sp500_tickers()
