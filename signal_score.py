"""
Quantitative Candidate Scoring Engine.

Ranks candidate tickers on a 0-100 scale using ONLY deterministic
technical indicators (no news, no LLM) before they're ever shown to
Gemini. In decide.py, new candidates (news-driven or watchlist) scoring
below MIN_SIGNAL_SCORE_TO_CONSIDER (config.py) are filtered out and never
reach the prompt -- this is the quantitative pre-scoring stage described
in the README. Existing holdings are never filtered this way.

Expects the exact dict shape produced by trader.get_full_indicators().
"""

from typing import Optional, Dict


def calculate_signal_score(indicators: Optional[Dict]) -> float:
    """
    Args:
        indicators: The dict returned by trader.get_full_indicators(),
            or None if indicators couldn't be computed for that ticker
            (returns 0.0 -- can't score what we can't measure).

    Returns:
        A composite score from 0.0 to 100.0. 50.0 is neutral/baseline.
    """
    if not indicators:
        return 0.0

    score = 50.0  # baseline

    trend = indicators.get("trend")                     # "uptrend" / "downtrend" / "sideways" / None
    rsi = indicators.get("rsi_14")
    adx = indicators.get("adx_14")
    macd = indicators.get("macd")                        # dict with "histogram", or None
    rel_volume = indicators.get("relative_volume_pct")    # e.g. +42.0 means 42% above average

    # --- Trend alignment (+/-15) ---
    if trend == "uptrend":
        score += 15
    elif trend == "downtrend":
        score -= 15

    # --- Trend strength confirmation via ADX (+/-5) ---
    # A trend backed by ADX > 25 is more trustworthy than one with no
    # measurable strength behind it; weak ADX undercuts an otherwise
    # promising trend signal rather than confirming it.
    if trend in ("uptrend", "downtrend") and adx is not None:
        if adx > 25:
            score += 5
        elif adx < 20:
            score -= 5

    # --- RSI (+15 / +10 / -15) ---
    if rsi is not None:
        if rsi < 35:
            score += 15   # potential oversold bounce
        elif 40 <= rsi <= 60:
            score += 10   # healthy, non-extreme momentum
        elif rsi > 70:
            score -= 15   # overbought penalty

    # --- Volume confirmation (+10 / -5) ---
    if rel_volume is not None:
        if rel_volume > 20:
            score += 10   # meaningfully above-average interest behind the move
        elif rel_volume < -30:
            score -= 5    # unusually quiet -- low conviction behind any signal

    # --- MACD momentum (+/-10) ---
    if macd is not None:
        histogram = macd.get("histogram", 0) or 0
        if histogram > 0:
            score += 10
        elif histogram < 0:
            score -= 10

    return max(0.0, min(100.0, score))
