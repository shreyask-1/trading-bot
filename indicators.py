"""
Technical Indicator Computation Engine.
Calculates core indicators (SMA, RSI, Volume Trend, MACD, Bollinger Bands)
and standardizes trend classification.
"""

import math


def compute_sma(data, period):
    if not data or len(data) < period:
        return None
    return round(sum(data[-period:]) / period, 2)


def compute_rsi(data, period=14):
    if not data or len(data) <= period:
        return None

    deltas = [data[i] - data[i - 1] for i in range(1, len(data))]
    gains = [max(d, 0) for d in deltas[:period]]
    losses = [max(-d, 0) for d in deltas[:period]]

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    for i in range(period, len(deltas)):
        gain = max(deltas[i], 0)
        loss = max(-deltas[i], 0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return round(rsi, 2)


def compute_volume_trend(volumes, lookback=5):
    if not volumes or len(volumes) < lookback * 2:
        return None

    recent_avg = sum(volumes[-lookback:]) / lookback
    prior_avg = sum(volumes[-(lookback * 2) : -lookback]) / lookback

    if prior_avg == 0:
        return 0.0

    return round(((recent_avg - prior_avg) / prior_avg) * 100, 2)


def compute_ema(data, period):
    if not data or len(data) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(data[:period]) / period
    for price in data[period:]:
        ema = (price * k) + (ema * (1 - k))
    return ema


def compute_macd(data, fast=12, slow=26, signal=9):
    if not data or len(data) < slow + signal:
        return None

    macd_line = []
    # Calculate MACD series
    k_fast = 2 / (fast + 1)
    k_slow = 2 / (slow + 1)
    
    ema_fast = sum(data[:fast]) / fast
    ema_slow = sum(data[:slow]) / slow

    for i in range(len(data)):
        if i >= fast:
            ema_fast = (data[i] * k_fast) + (ema_fast * (1 - k_fast))
        if i >= slow:
            ema_slow = (data[i] * k_slow) + (ema_slow * (1 - k_slow))
            macd_line.append(ema_fast - ema_slow)

    if len(macd_line) < signal:
        return None

    # Calculate Signal series
    signal_line = compute_ema(macd_line, signal)
    current_macd = macd_line[-1]
    
    if current_macd is None or signal_line is None:
        return None

    histogram = current_macd - signal_line

    return {
        "macd": round(current_macd, 4),
        "signal": round(signal_line, 4),
        "histogram": round(histogram, 4),
    }


def compute_bollinger_bands(data, period=20, std_dev_mult=2.0):
    if not data or len(data) < period:
        return None

    subset = data[-period:]
    mean = sum(subset) / period
    variance = sum((x - mean) ** 2 for x in subset) / period
    std_dev = math.sqrt(variance)

    return {
        "upper": round(mean + (std_dev_mult * std_dev), 2),
        "middle": round(mean, 2),
        "lower": round(mean - (std_dev_mult * std_dev), 2),
    }


def classify_trend(price, sma20, sma50):
    if not price or not sma20 or not sma50:
        return "neutral"

    if price > sma20 > sma50:
        return "bullish"
    elif price < sma20 < sma50:
        return "bearish"
    else:
        return "neutral"
