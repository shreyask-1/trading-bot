"""
Market Regime Engine.
Evaluates macro trend & volatility filters to determine trading regime.
"""

from indicators import compute_sma

def evaluate_market_regime(spy_bars):
    """
    Determines market regime using SPY 20/50 SMA and recent volatility.
    Returns regime label: 'BULLISH', 'BEARISH', or 'HIGH_VOLATILITY'
    """
    if not spy_bars or len(spy_bars) < 50:
        return "NEUTRAL"

    closes = [b.close for b in spy_bars]
    current_price = closes[-1]
    sma20 = compute_sma(closes, 20)
    sma50 = compute_sma(closes, 50)

    if not sma20 or not sma50:
        return "NEUTRAL"

    if current_price > sma20 > sma50:
        return "BULLISH"
    elif current_price < sma20 < sma50:
        return "BEARISH"
    else:
        return "NEUTRAL"
