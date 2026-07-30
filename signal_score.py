"""
Quantitative Candidate Scoring Engine.
Ranks universe candidates prior to Gemini validation.
"""

def calculate_signal_score(indicators):
    """
    Computes a composite score (0-100) based on normalized indicators.
    """
    if not indicators:
        return 0.0

    score = 50.0  # Baseline

    # Trend Alignment (+15 / -15)
    trend = indicators.get("trend")
    if trend == "bullish":
        score += 15
    elif trend == "bearish":
        score -= 15

    # RSI Factor (+15 for oversold bounce, -15 for overbought)
    rsi = indicators.get("rsi")
    if rsi:
        if 40 <= rsi <= 60:
            score += 10
        elif rsi < 35:
            score += 15  # Potential oversold dip
        elif rsi > 70:
            score -= 15  # Overbought penalty

    # Volume Confirmation (+10)
    vol_trend = indicators.get("volume_trend_pct")
    if vol_trend and vol_trend > 10:
        score += 10

    # MACD Momentum (+10)
    macd = indicators.get("macd")
    if macd and macd.get("histogram", 0) > 0:
        score += 10

    return max(0.0, min(100.0, score))
