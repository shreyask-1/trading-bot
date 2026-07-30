"""
S&P 500 & Custom High-Growth Universe Retrieval Engine.
Contains the explicit, hardcoded master baseline of all S&P 500 equities
plus custom high-momentum growth targets and SpaceX.
"""

def get_sp500_tickers():
    """
    Returns the explicitly defined, comprehensive array of all S&P 500 
    components and custom user targets. Fully offline and crash-proof.
    """
    master_universe = [
        # --- S&P 500 Complete Baseline Array ---
        "A", "AAPL", "ABBV", "ABNB", "ABT", "ACGL", "ACN", "ADBE", "ADI", "ADM",
        "ADP", "ADSK", "AEE", "AES", "AFL", "AIG", "AIZ", "AJG", "AKAM", "ALB",
        "ALGN", "ALL", "ALLE", "AMAT", "AMCR", "AMD", "AME", "AMGN", "AMP", "AMT",
        "AMZN", "ANET", "ANSS", "AON", "AOS", "APA", "APD", "APH", "APTV", "ARE",
        "ATO", "AVB", "AVGO", "AVY", "AWK", "AXON", "AXP", "AZO", "BA", "BAC",
        "BALL", "BAX", "BBY", "BDX", "BEN", "BG", "BIIB", "BK", "BKNG", "BKR",
        "BLDR", "BLK", "BMY", "BR", "BRK-B", "BSX", "BX", "C", "CAG", "CAH",
        "CARR", "CAT", "CB", "CBOE", "CBRE", "CCI", "CCL", "CDNS", "CDW", "CE",
        "CEG", "CF", "CFG", "CHD", "CHRW", "CHTR", "CI", "CINF", "CL", "CLX",
        "CMA", "CMCSA", "CME", "CMG", "CMI", "CMS", "CNC", "CNP", "COF", "COO",
        "COP", "COR", "COST", "CPAY", "CPB", "CPRT", "CPT", "CRL", "CRM", "CSCO",
        "CSX", "CTAS", "CTSH", "CTVA", "CVS", "CVX", "CZR", "D", "DAL", "DD",
        "DE", "DECK", "DFS", "DG", "DGX", "DHI", "DHR", "DIS", "DLR", "DLTR",
        "DOV", "DOW", "DPZ", "DRI", "DTE", "DUK", "DVA", "DVN", "EA", "EBAY",
        "ECL", "ED", "EFX", "EG", "EIX", "EL", "ELV", "EMN", "EMR", "ENPH",
        "EOG", "EPAM", "EQIX", "EQR", "ERIE", "ES", "ESS", "ETN", "ETR", "EVRG",
        "EW", "EXC", "EXPD", "EXPE", "EXR", "F", "FANG", "FAST", "FCX", "FDS",
        "FDX", "FE", "FFIV", "FI", "FIS", "FITB", "FLT", "FMC", "FOX", "FOXA",
        "FRT", "FSLR", "FTNT", "FTV", "GD", "GE", "GEHC", "GEN", "GILD", "GIS",
        "GL", "GLW", "GM", "GNRC", "GOOG", "GOOGL", "GPC", "GPN", "GRMN", "GS",
        "GWW", "HAL", "HAS", "HBAN", "HCA", "HD", "HES", "HIG", "HII", "HLT",
        "HOLX", "HON", "HPE", "HPQ", "HRL", "HSIC", "HST", "HSY", "HUBB", "HUM",
        "HWM", "IBM", "ICE", "IDXX", "IEX", "IFF", "INCY", "INTC", "INTU", "INVH",
        "IP", "IPG", "IQV", "IR", "IRM", "ISRG", "IT", "ITW", "IVZ", "J",
        "JBHT", "JBL", "JCI", "JKHY", "JNJ", "JNPR", "JPM", "K", "KDP", "KEY",
        "KEYS", "KHC", "KIM", "KLAC", "KMB", "KMI", "KMX", "KO", "KR", "KVUE",
        "L", "LDOS", "LEN", "LH", "LHX", "LII", "LIN", "LKQ", "LLY", "LMT",
        "LNT", "LOW", "LRCX", "LULU", "LUV", "LVS", "LW", "LYB", "LYV", "MA",
        "MAA", "MAR", "MAS", "MCD", "MCHP", "MCK", "MCO", "MDLZ", "MDT", "MET",
        "META", "MGM", "MHK", "MKC", "MKTX", "MLM", "MMC", "MMM", "MNST", "MO",
        "MOH", "MOS", "MPC", "MPWR", "MRK", "MRNA", "MS", "MSCI", "MSFT", "MSI",
        "MTB", "MTCH", "MTD", "MU", "NCLX", "NDAQ", "NDSN", "NEE", "NEM", "NFLX",
        "NI", "NKE", "NOC", "NOW", "NRG", "NSC", "NTAP", "NTRS", "NUE", "NVDA",
        "NVR", "NXPI", "O", "ODFL", "OGN", "OI", "OKE", "OMC", "ON", "ORCL",
        "OTIS", "OXY", "PANW", "PARA", "PAYC", "PAYX", "PCAR", "PCG", "PEG", "PEP",
        "PFE", "PFG", "PG", "PGR", "PH", "PHM", "PKG", "PLD", "PLTR", "PM",
        "PNC", "PNR", "PNW", "POOL", "PPG", "PPL", "PRU", "PSA", "PSX", "PTC",
        "PWR", "PYPL", "QCOM", "RCL", "REG", "REGN", "RF", "RHI", "RJF", "RL",
        "RMD", "ROK", "ROL", "ROP", "ROST", "RSG", "RTX", "RVTY", "SBAC", "SBNY",
        "SBUX", "SCHW", "SHW", "SJM", "SLB", "SMCI", "SNA", "SNPS", "SO", "SPGI",
        "SRE", "STE", "STLD", "STT", "STX", "STZ", "SWK", "SWKS", "SYF", "SYK",
        "SYY", "T", "TAP", "TDG", "TDY", "TECH", "TEL", "TER", "TFC", "TFX",
        "TGT", "TJX", "TMO", "TMUS", "TPL", "TPR", "TRGP", "TRV", "TSLA", "TSN",
        "TT", "TTWO", "TXN", "TXT", "TYL", "UAL", "UBER", "UDR", "UHS", "ULTA",
        "UNH", "UNP", "UPS", "URI", "USB", "V", "VICI", "VLO", "VLTO", "VMC",
        "VRSK", "VRSN", "VRTX", "VST", "VTR", "VZ", "WAB", "WAT", "WBA", "WBD",
        "WDC", "WEC", "WELL", "WFC", "WM", "WMB", "WMT", "WRB", "WST", "WTW",
        "WY", "WYNN", "XEL", "XOM", "XYL", "YUM", "ZBH", "ZBRA", "ZTS",

        # --- Custom Additions & SpaceX ---
        "SPCX",    # SpaceX Target
        "FBRX",    # Forte Biosciences
        "MANH",    # Manhattan Associates
        "THC"      # Tenet Healthcare
    ]

    # Deduplicate and sort alphabetically
    return sorted(list(set(master_universe)))


# Backward compatibility export for news.py and main.py
SP500 = get_sp500_tickers()
