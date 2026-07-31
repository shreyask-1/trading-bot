"""
Market Regime Engine.

Evaluates the broad market's trend and volatility (using SPY as a proxy)
to classify the current regime. Used in trader.py as an independent,
code-enforced throttle on new position sizing -- separate from, and in
addition to, whatever any individual ticker's own setup looks like.

Regimes:
    BULLISH         -- SPY trending up (price > 20-SMA > 50-SMA).
    BEARISH         -- SPY trending down (price < 20-SMA < 50-SMA).
    NEUTRAL         -- mixed/sideways, or not enough history yet.
    HIGH_VOLATILITY -- SPY's 20-day realized volatility is elevated,
                       regardless of trend direction. Takes precedence
                       over BULLISH/BEARISH, since a market moving
                       violently is risky in both directions.
"""

from typing import List

from indicators import compute_sma, compute_volatility


def evaluate_market_regime(spy_closes: List[float], high_vol_threshold: float = 2.5) -> str:
    """
    Args:
        spy_closes: SPY closing prices, oldest first. Must reflect only
            data available as of the date being evaluated -- never pass
            in future bars (matters for backtest.py's use of this).
        high_vol_threshold: 20-day realized volatility (%, as returned by
            compute_volatility) at or above which the regime is forced to
            HIGH_VOLATILITY regardless of trend.

    Returns:
        One of "BULLISH", "BEARISH", "NEUTRAL", "HIGH_VOLATILITY".
    """
    if not spy_closes or len(spy_closes) < 50:
        return "NEUTRAL"

    sma20 = compute_sma(spy_closes, 20)
    sma50 = compute_sma(spy_closes, 50)
    if sma20 is None or sma50 is None:
        return "NEUTRAL"

    volatility = compute_volatility(spy_closes, period=20)
    if volatility is not None and volatility >= high_vol_threshold:
        return "HIGH_VOLATILITY"

    current_price = spy_closes[-1]
    if current_price > sma20 > sma50:
        return "BULLISH"
    elif current_price < sma20 < sma50:
        return "BEARISH"
    return "NEUTRAL"
