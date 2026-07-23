"""
Plain-Python technical indicator calculations (no external TA library
needed). These are standard, widely-used formulas from technical analysis
-- well-established as concepts, but heuristics, not guarantees of
future performance.
"""


def compute_sma(closes, period):
    if len(closes) < period:
        return None
    return round(sum(closes[-period:]) / period, 2)


def compute_rsi(closes, period=14):
    """
    Wilder's RSI, 0-100. Traditionally >70 = overbought, <30 = oversold.
    """
    if len(closes) < period + 1:
        return None

    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def compute_volume_trend(volumes, period=20):
    """% difference between latest volume and the prior `period`-day average."""
    if len(volumes) < period + 1:
        return None
    recent = volumes[-1]
    baseline = sum(volumes[-period - 1:-1]) / period
    if baseline == 0:
        return None
    return round(((recent - baseline) / baseline) * 100, 2)


def classify_trend(price, sma20, sma50):
    """Basic moving-average trend classification."""
    if sma20 is None or sma50 is None:
        return "unknown"
    if price > sma20 > sma50:
        return "uptrend"
    if price < sma20 < sma50:
        return "downtrend"
    return "mixed/sideways"
