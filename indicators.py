"""
indicators.py

Pure-Python technical indicator calculations for the trading bot -- no
external dependencies (no numpy/pandas required). All functions take plain
lists of floats and return either a float, a dict of floats, a string
classification, or None when there isn't enough data to compute a result.

Every function is safe to call with short histories: they return None
(or a neutral default, documented per-function) rather than raising an
exception, so a single ticker with limited history can't crash a run.
"""

from typing import List, Optional, Dict

__all__ = [
    "compute_sma",
    "compute_ema",
    "compute_ema_series",
    "compute_rsi",
    "compute_atr",
    "compute_adx",
    "compute_volume_trend",
    "compute_relative_volume",
    "classify_trend",
    "compute_macd",
    "compute_bollinger_bands",
    "compute_stochastic",
    "compute_momentum",
    "compute_volatility",
]


# ============================================================
# Internal helpers
# ============================================================

def _rolling_mean(values: List[float], period: int) -> Optional[float]:
    """Simple moving average of the last `period` values, or None if not enough data."""
    if len(values) < period or period <= 0:
        return None
    return sum(values[-period:]) / period


def _rolling_std(values: List[float], period: int) -> Optional[float]:
    """Population standard deviation of the last `period` values, or None if not enough data."""
    if len(values) < period or period <= 0:
        return None
    window = values[-period:]
    mean = sum(window) / period
    variance = sum((v - mean) ** 2 for v in window) / period
    return variance ** 0.5


def _wilder_smooth(values: List[float], period: int) -> Optional[List[float]]:
    """
    Wilder's smoothing (used by RSI, ATR, ADX). Returns the full smoothed
    series (same convention as an EMA with alpha = 1/period), or None if
    there isn't enough data to seed the first value.
    """
    if len(values) < period:
        return None
    smoothed = [sum(values[:period]) / period]
    for v in values[period:]:
        smoothed.append((smoothed[-1] * (period - 1) + v) / period)
    return smoothed


# ============================================================
# Moving averages
# ============================================================

def compute_sma(closes: List[float], period: int) -> Optional[float]:
    """
    Simple moving average of closing prices.

    Args:
        closes: List of closing prices, oldest first.
        period: Number of most recent bars to average.

    Returns:
        The SMA as a float, or None if there isn't enough history.
    """
    return _rolling_mean(closes, period)


def compute_ema_series(closes: List[float], period: int) -> Optional[List[float]]:
    """
    Full exponential moving average series (seeded with a simple average
    of the first `period` values, then smoothed forward). Useful internally
    for indicators like MACD that need the whole EMA history, not just the
    latest value.

    Args:
        closes: List of closing prices, oldest first.
        period: EMA period.

    Returns:
        A list of EMA values aligned to `closes[period - 1:]`, or None if
        there isn't enough history.
    """
    if len(closes) < period or period <= 0:
        return None
    multiplier = 2 / (period + 1)
    ema = [sum(closes[:period]) / period]
    for price in closes[period:]:
        ema.append((price - ema[-1]) * multiplier + ema[-1])
    return ema


def compute_ema(closes: List[float], period: int) -> Optional[float]:
    """
    Latest exponential moving average value.

    Args:
        closes: List of closing prices, oldest first.
        period: EMA period.

    Returns:
        The most recent EMA value as a float, or None if there isn't
        enough history.
    """
    series = compute_ema_series(closes, period)
    return series[-1] if series else None


# ============================================================
# RSI
# ============================================================

def compute_rsi(closes: List[float], period: int = 14) -> Optional[float]:
    """
    Relative Strength Index using Wilder's smoothing, the standard
    convention (matches TradingView/most brokers' default RSI).

    Args:
        closes: List of closing prices, oldest first.
        period: Lookback period (default 14).

    Returns:
        RSI as a float between 0.0 and 100.0, or None if there isn't
        enough history (need at least `period + 1` closes).
    """
    if len(closes) < period + 1:
        return None

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


# ============================================================
# ATR / ADX (require high, low, close)
# ============================================================

def _true_ranges(highs: List[float], lows: List[float], closes: List[float]) -> List[float]:
    trs = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    return trs


def compute_atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> Optional[float]:
    """
    Average True Range -- a volatility measure in the instrument's own
    price units (not a percentage), using Wilder's smoothing.

    Args:
        highs: List of period highs, oldest first.
        lows: List of period lows, oldest first.
        closes: List of closing prices, oldest first. Must be the same
            length as `highs` and `lows`.
        period: Lookback period (default 14).

    Returns:
        ATR as a float, or None if there isn't enough history.
    """
    if len(closes) < period + 1 or len(highs) != len(closes) or len(lows) != len(closes):
        return None
    trs = _true_ranges(highs, lows, closes)
    smoothed = _wilder_smooth(trs, period)
    return round(smoothed[-1], 4) if smoothed else None


def compute_adx(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> Optional[float]:
    """
    Average Directional Index -- measures trend STRENGTH (not direction).
    Values above ~25 generally indicate a strong trend (in either
    direction); values below ~20 generally indicate a weak or absent trend.
    Pairs naturally with ATR since both are Wilder-smoothed from the same
    high/low/close inputs.

    Args:
        highs: List of period highs, oldest first.
        lows: List of period lows, oldest first.
        closes: List of closing prices, oldest first.
        period: Lookback period (default 14).

    Returns:
        ADX as a float between 0.0 and 100.0, or None if there isn't
        enough history (need at least roughly 2 * period bars).
    """
    n = len(closes)
    if n < period * 2 or len(highs) != n or len(lows) != n:
        return None

    plus_dm = [0.0]
    minus_dm = [0.0]
    for i in range(1, n):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)

    trs = _true_ranges(highs, lows, closes)

    smoothed_tr = _wilder_smooth(trs, period)
    smoothed_plus_dm = _wilder_smooth(plus_dm, period)
    smoothed_minus_dm = _wilder_smooth(minus_dm, period)
    if not smoothed_tr or not smoothed_plus_dm or not smoothed_minus_dm:
        return None

    length = min(len(smoothed_tr), len(smoothed_plus_dm), len(smoothed_minus_dm))
    dx_values = []
    for i in range(length):
        tr = smoothed_tr[i]
        if tr == 0:
            continue
        plus_di = 100.0 * (smoothed_plus_dm[i] / tr)
        minus_di = 100.0 * (smoothed_minus_dm[i] / tr)
        di_sum = plus_di + minus_di
        if di_sum == 0:
            dx_values.append(0.0)
        else:
            dx_values.append(100.0 * abs(plus_di - minus_di) / di_sum)

    if len(dx_values) < period:
        return None

    adx_series = _wilder_smooth(dx_values, period)
    return round(adx_series[-1], 2) if adx_series else None


# ============================================================
# Volume
# ============================================================

def compute_volume_trend(volumes: List[float], period: int = 10) -> Optional[str]:
    """
    Simple classification of whether volume has been rising or falling
    over the recent window.

    Args:
        volumes: List of period volumes, oldest first.
        period: Number of bars to split into "earlier" vs "recent" halves.

    Returns:
        "increasing", "decreasing", "flat", or None if there isn't enough
        history (need at least `period` bars).
    """
    if len(volumes) < period:
        return None
    window = volumes[-period:]
    half = period // 2
    earlier_avg = sum(window[:half]) / half
    recent_avg = sum(window[half:]) / (period - half)
    if earlier_avg == 0:
        return "flat"
    pct_change = (recent_avg - earlier_avg) / earlier_avg
    if pct_change > 0.10:
        return "increasing"
    elif pct_change < -0.10:
        return "decreasing"
    return "flat"


def compute_relative_volume(volumes: List[float], period: int = 20) -> Optional[float]:
    """
    Today's (most recent bar's) volume relative to its own trailing
    average, expressed as a percentage difference. E.g. 150.0 means
    today's volume is 150% of the average (2.5x); -30.0 means 30% below
    average.

    Args:
        volumes: List of period volumes, oldest first. The last element
            is treated as "today."
        period: Number of prior bars to average (excludes today itself).

    Returns:
        Percentage difference as a float, or None if there isn't enough
        history, or if the trailing average is zero.
    """
    if len(volumes) < period + 1:
        return None
    today = volumes[-1]
    trailing_avg = sum(volumes[-(period + 1):-1]) / period
    if trailing_avg == 0:
        return None
    return round(((today - trailing_avg) / trailing_avg) * 100.0, 1)


# ============================================================
# Trend classification
# ============================================================

def classify_trend(closes: List[float], short_period: int = 20, long_period: int = 50) -> Optional[str]:
    """
    Classifies the overall trend by comparing price to two SMAs.

    Args:
        closes: List of closing prices, oldest first.
        short_period: Shorter SMA period (default 20).
        long_period: Longer SMA period (default 50).

    Returns:
        "uptrend" (price above both SMAs, short above long),
        "downtrend" (price below both SMAs, short below long),
        "sideways" (mixed signal), or None if there isn't enough history.
    """
    sma_short = compute_sma(closes, short_period)
    sma_long = compute_sma(closes, long_period)
    if sma_short is None or sma_long is None:
        return None

    price = closes[-1]
    if price > sma_short > sma_long:
        return "uptrend"
    elif price < sma_short < sma_long:
        return "downtrend"

    return "sideways"


# ============================================================
# MACD
# ============================================================

def compute_macd_crossover(
    closes: List[float],
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> Optional[str]:
    """
    Direction of the most recent MACD line / signal line crossover:
    "bullish" (macd crossed above signal on the last bar), "bearish"
    (crossed below), "none" (no crossover at the last bar), or None if
    there isn't enough history.
    """
    fast_series = compute_ema_series(closes, fast_period)
    slow_series = compute_ema_series(closes, slow_period)
    if not fast_series or not slow_series:
        return None
    offset = len(fast_series) - len(slow_series)
    if offset < 0:
        return None
    macd_line = [fast_series[i + offset] - slow_series[i] for i in range(len(slow_series))]
    if len(macd_line) < signal_period + 1:
        return None
    signal_series = compute_ema_series(macd_line, signal_period)
    if not signal_series:
        return None
    above_now = macd_line[-1] > signal_series[-1]
    above_prev = macd_line[-2] > signal_series[-2]
    if above_now and not above_prev:
        return "bullish"
    if not above_now and above_prev:
        return "bearish"
    return "none"


def compute_macd(
    closes: List[float],
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> Optional[Dict[str, float]]:
    """
    Moving Average Convergence Divergence.

    Args:
        closes: List of closing prices, oldest first.
        fast_period: Fast EMA period (default 12).
        slow_period: Slow EMA period (default 26).
        signal_period: Signal line EMA period, applied to the MACD line
            itself (default 9).

    Returns:
        A dict with keys "macd", "signal", and "histogram" (all floats),
        or None if there isn't enough history.
    """
    fast_series = compute_ema_series(closes, fast_period)
    slow_series = compute_ema_series(closes, slow_period)
    if not fast_series or not slow_series:
        return None

    # Align the two series to the same ending point (slow EMA starts later)
    offset = len(fast_series) - len(slow_series)
    if offset < 0:
        return None
    macd_line = [fast_series[i + offset] - slow_series[i] for i in range(len(slow_series))]

    if len(macd_line) < signal_period:
        return None

    signal_series = compute_ema_series(macd_line, signal_period)
    if not signal_series:
        return None

    macd_value = macd_line[-1]
    signal_value = signal_series[-1]
    return {
        "macd": round(macd_value, 4),
        "signal": round(signal_value, 4),
        "histogram": round(macd_value - signal_value, 4),
    }


# ============================================================
# Bollinger Bands
# ============================================================

def compute_bollinger_bands(
    closes: List[float], period: int = 20, num_std: float = 2.0
) -> Optional[Dict[str, float]]:
    """
    Bollinger Bands: a moving average with upper/lower bands at
    `num_std` standard deviations away, plus %B (where price sits within
    the bands) and bandwidth (how wide the bands are, as a fraction of
    the middle band).

    Args:
        closes: List of closing prices, oldest first.
        period: SMA / std-dev period (default 20).
        num_std: Number of standard deviations for the bands (default 2.0).

    Returns:
        A dict with keys "upper", "middle", "lower", "percent_b", and
        "bandwidth" (all floats), or None if there isn't enough history.
        `percent_b` defaults to 0.5 (neutral/mid-band) in the edge case
        where the bands have zero width, rather than dividing by zero.
    """
    middle = compute_sma(closes, period)
    std = _rolling_std(closes, period)
    if middle is None or std is None:
        return None

    upper = middle + (num_std * std)
    lower = middle - (num_std * std)
    width = upper - lower
    price = closes[-1]

    if abs(width) < 1e-12:
        percent_b = 0.5
    else:
        percent_b = (price - lower) / width

    bandwidth = (width / middle) if middle != 0 else 0.0

    return {
        "upper": round(upper, 4),
        "middle": round(middle, 4),
        "lower": round(lower, 4),
        "percent_b": round(percent_b, 4),
        "bandwidth": round(bandwidth, 4),
    }


# ============================================================
# Stochastic Oscillator
# ============================================================

def compute_stochastic(
    highs: List[float], lows: List[float], closes: List[float],
    period: int = 14, smooth_period: int = 3,
) -> Optional[Dict[str, float]]:
    """
    Stochastic Oscillator (%K and %D).

    Args:
        highs: List of period highs, oldest first.
        lows: List of period lows, oldest first.
        closes: List of closing prices, oldest first. Must be the same
            length as `highs` and `lows`.
        period: Lookback period for %K (default 14).
        smooth_period: Smoothing period for %D, the SMA of %K (default 3).

    Returns:
        A dict with keys "percent_k" and "percent_d" (both floats, 0-100),
        or None if there isn't enough history.
    """
    n = len(closes)
    if n < period + smooth_period or len(highs) != n or len(lows) != n:
        return None

    percent_k_values = []
    for i in range(period - 1, n):
        window_high = max(highs[i - period + 1: i + 1])
        window_low = min(lows[i - period + 1: i + 1])
        if window_high == window_low:
            percent_k_values.append(50.0)
        else:
            percent_k_values.append(100.0 * (closes[i] - window_low) / (window_high - window_low))

    if len(percent_k_values) < smooth_period:
        return None

    percent_d = sum(percent_k_values[-smooth_period:]) / smooth_period

    return {
        "percent_k": round(percent_k_values[-1], 2),
        "percent_d": round(percent_d, 2),
    }


# ============================================================
# Momentum / Volatility
# ============================================================

def compute_momentum(closes: List[float], period: int = 10) -> Optional[float]:
    """
    Percentage price change over the previous `period` bars (rate of change).

    Args:
        closes: List of closing prices, oldest first.
        period: Lookback period (default 10).

    Returns:
        Percentage change as a float (e.g. 3.5 means +3.5%), or None if
        there isn't enough history or the starting price is zero.
    """
    if len(closes) < period + 1:
        return None
    start = closes[-(period + 1)]
    end = closes[-1]
    if start == 0:
        return None
    return round(((end - start) / start) * 100.0, 2)


def compute_volatility(closes: List[float], period: int = 20) -> Optional[float]:
    """
    Historical volatility: the standard deviation of daily percentage
    returns over the period, expressed as a percentage.

    Args:
        closes: List of closing prices, oldest first.
        period: Lookback period for returns (default 20; needs
            `period + 1` closes to compute `period` returns).

    Returns:
        Volatility as a float percentage (e.g. 2.1 means a ~2.1%
        typical daily move), or None if there isn't enough history.
    """
    if len(closes) < period + 1:
        return None
    returns = [
        (closes[i] - closes[i - 1]) / closes[i - 1]
        for i in range(len(closes) - period, len(closes))
        if closes[i - 1] != 0
    ]
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / len(returns)
    return round((variance ** 0.5) * 100.0, 2)
