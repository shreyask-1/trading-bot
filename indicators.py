"""
Plain-Python technical indicator calculations (no external TA library
needed). Standard, widely-used formulas from technical analysis --
established concepts, but heuristics, not guarantees of future performance.
"""


def compute_sma(closes, period):
    if len(closes) < period:
        return None
    return round(sum(closes[-period:]) / period, 2)


def compute_ema_series(closes, period):
    """Returns the full EMA series (list), not just the final value."""
    if len(closes) < period:
        return []
    multiplier = 2 / (period + 1)
    ema_values = [sum(closes[:period]) / period]
    for price in closes[period:]:
        ema_values.append((price - ema_values[-1]) * multiplier + ema_values[-1])
    return ema_values


def compute_rsi(closes, period=14):
    """Wilder's RSI, 0-100. Traditionally >70 = overbought, <30 = oversold."""
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


def compute_macd(closes, fast=12, slow=26, signal=9):
    """
    Returns dict with macd line, signal line, and histogram, or None if
    not enough data. Histogram crossing from negative to positive is a
    classic bullish signal; positive to negative is bearish.
    """
    if len(closes) < slow + signal:
        return None

    ema_fast_series = compute_ema_series(closes, fast)
    ema_slow_series = compute_ema_series(closes, slow)

    offset = len(ema_fast_series) - len(ema_slow_series)
    macd_line_series = [
        ema_fast_series[i + offset] - ema_slow_series[i]
        for i in range(len(ema_slow_series))
    ]

    if len(macd_line_series) < signal:
        return None

    signal_series = compute_ema_series(macd_line_series, signal)
    if not signal_series:
        return None

    macd_value = round(macd_line_series[-1], 3)
    signal_value = round(signal_series[-1], 3)
    histogram = round(macd_value - signal_value, 3)

    return {"macd": macd_value, "signal": signal_value, "histogram": histogram}


def compute_bollinger_bands(closes, period=20, num_std=2):
    """
    Returns upper/lower bands and %B (where price sits within the bands:
    0 = at lower band, 1 = at upper band, >1 or <0 = outside the bands).
    """
    if len(closes) < period:
        return None

    window = closes[-period:]
    sma = sum(window) / period
    variance = sum((c - sma) ** 2 for c in window) / period
    std = variance ** 0.5

    upper = sma + num_std * std
    lower = sma - num_std * std
    current = closes[-1]

    if upper == lower:
        percent_b = 0.5
    else:
        percent_b = (current - lower) / (upper - lower)

    return {"upper": round(upper, 2), "lower": round(lower, 2), "percent_b": round(percent_b, 2)}


def compute_volume_trend(volumes, period=20):
    if len(volumes) < period + 1:
        return None
    recent = volumes[-1]
    baseline = sum(volumes[-period - 1:-1]) / period
    if baseline == 0:
        return None
    return round(((recent - baseline) / baseline) * 100, 2)


def classify_trend(price, sma20, sma50):
    if sma20 is None or sma50 is None:
        return "unknown"
    if price > sma20 > sma50:
        return "uptrend"
    if price < sma20 < sma50:
        return "downtrend"
    return "mixed/sideways"
