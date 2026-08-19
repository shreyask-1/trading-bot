"""Small, deterministic sector fallback for free-tier metadata outages.

This is intentionally conservative and only contains symbols the bot has
recently held or considered. It is used only when the live profile endpoint
returns no sector; live metadata still wins whenever available.
"""

SECTOR_FALLBACK = {
    # Current/recent portfolio names
    "AMCR": "Materials", "AMT": "Real Estate", "COO": "Health Care",
    "EOG": "Energy", "EQT": "Energy", "ERIE": "Financials",
    "GEHC": "Health Care", "ICE": "Financials", "INTU": "Technology",
    "IVZ": "Financials", "KKR": "Financials", "MS": "Financials",
    "MSI": "Technology", "NWS": "Communication Services",
    "T": "Communication Services", "TRMB": "Technology",
    "TSCO": "Consumer Discretionary", "TRGP": "Energy", "NOW": "Technology",
    "TAP": "Consumer Staples", "CEG": "Utilities", "CHD": "Consumer Staples",
    "CVX": "Energy", "HAS": "Consumer Discretionary", "LMT": "Industrials",
    "NDAQ": "Financials", "NUE": "Materials", "NVDA": "Technology",
    "STLD": "Materials", "WSM": "Consumer Discretionary",
    # Recent queue/watchlist names
    "GPN": "Financials", "GILD": "Health Care", "GDDY": "Communication Services",
    "NWSA": "Communication Services", "MRK": "Health Care", "MNST": "Consumer Staples",
    "RSG": "Industrials", "STZ": "Consumer Staples", "WDC": "Technology",
    "TYL": "Technology", "TTWO": "Communication Services", "WFC": "Financials",
    "HUM": "Health Care", "TDY": "Industrials", "BAC": "Financials",
    "HBAN": "Financials", "CPRT": "Industrials", "IVZ": "Financials",
    "NEM": "Materials", "DASH": "Consumer Discretionary", "DAL": "Industrials",
    "MRNA": "Health Care", "COHR": "Technology", "SNDK": "Technology",
    "ANET": "Technology", "HPE": "Technology", "MSFT": "Technology",
    "AAPL": "Technology", "GOOGL": "Communication Services", "GOOG": "Communication Services",
    "META": "Communication Services", "AMZN": "Consumer Discretionary",
    "TSLA": "Consumer Discretionary", "COST": "Consumer Staples",
    "DIS": "Communication Services", "HD": "Consumer Discretionary",
    "MA": "Financials", "COP": "Energy", "LMT": "Industrials",
    "GPC": "Consumer Discretionary", "MDLZ": "Consumer Staples",
    "SHW": "Materials", "NWS": "Communication Services", "NWSA": "Communication Services",
    "NTAP": "Technology", "RJF": "Financials", "RL": "Consumer Discretionary",
    "RMD": "Health Care", "ROP": "Industrials", "ROST": "Consumer Discretionary",
    "RTX": "Industrials", "RVTY": "Health Care", "SBAC": "Real Estate",
    "SBUX": "Consumer Discretionary", "SCHW": "Financials", "SJM": "Consumer Staples",
    "SLB": "Energy", "SMCI": "Technology", "SNPS": "Technology",
    "SOLV": "Health Care", "SPGI": "Financials", "STE": "Health Care",
    "STT": "Financials", "STX": "Technology", "SW": "Materials",
    "SWK": "Industrials", "SWKS": "Technology", "SYF": "Financials",
    "SYK": "Health Care", "TGT": "Consumer Discretionary", "TEL": "Technology",
    "TER": "Technology", "TFC": "Financials", "TGT": "Consumer Discretionary",
    "TMO": "Health Care", "TKO": "Communication Services", "TMUS": "Communication Services",
    "TPR": "Consumer Discretionary", "TT": "Industrials", "WDAY": "Technology",
}
