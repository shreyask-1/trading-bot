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


def calculate_signal_score(indicators: Optional[Dict], news_sentiment: float = 0.0, extras: Optional[Dict] = None) -> float:
    """
    Args:
        indicators: The dict returned by trader.get_full_indicators(),
            or None if indicators couldn't be computed for that ticker
            (returns 0.0 -- can't score what we can't measure).
        news_sentiment: Optional -1..+1 headline sentiment for news-driven
            candidates, from news.headline_sentiment(). Added to the score as
            news_sentiment * NEWS_SENTIMENT_WEIGHT (config), so positive news
            can push a borderline candidate over the pre-screen bar.
        extras: Optional Phase 2 fundamental signals from
            data_feeds.get_fundamental_signals() -- {"analyst", "insider_net",
            "reddit_sentiment", "recent_filings", "days_until_earnings"}.
            Analyst upgrades/insider buying/positive Reddit nudge the score
            up; downgrades/insider selling/negative Reddit pull it down; an
            8-K or 10-Q filing adds mild confirmation. Pure cache reads.

    Returns:
        A composite score from 0.0 to 100.0. 50.0 is neutral/baseline.
    """
    if not indicators:
        return 0.0

    from config import (
        NEWS_SENTIMENT_WEIGHT,
        ANALYST_UPGRADE_BOOST,
        ANALYST_DOWNGRADE_PENALTY,
        INSIDER_BUY_BOOST,
        INSIDER_SELL_PENALTY,
        REDDIT_SENTIMENT_WEIGHT,
        EARNINGS_PROXIMITY_DAYS,
    )
    extras = extras or {}

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

    # --- News catalyst (+/- NEWS_SENTIMENT_WEIGHT) ---
    # The "dual focus" half of the bot: a headline moving the story matters
    # as much as the chart. Only applies to news-driven candidates (0.0 for
    # watchlist/holdings by default).
    if news_sentiment:
        score += news_sentiment * NEWS_SENTIMENT_WEIGHT

    # --- Opening-range breakout (+/-5) ---
    # Daytrading confirmation: breaking above/below the first 15-minute range
    # of the session is a classic momentum entry/exit trigger.
    or_status = indicators.get("opening_range_status")
    if or_status == "above":
        score += 5
    elif or_status == "below":
        score -= 5

    # --- Phase 2: analyst actions ---
    # A fresh upgrade is a real catalyst (price targets move); a downgrade is
    # an equal and opposite warning. 0 disables each.
    analyst = extras.get("analyst")
    if analyst == "upgrade":
        score += ANALYST_UPGRADE_BOOST
    elif analyst == "downgrade":
        score -= ANALYST_DOWNGRADE_PENALTY

    # --- Phase 2: insider activity ---
    # Insiders buying their own stock with cash is one of the strongest
    # fundamental tells that exists; heavy selling is a caution flag.
    insider_net = extras.get("insider_net", 0.0) or 0.0
    if insider_net > 5000:
        score += INSIDER_BUY_BOOST
    elif insider_net < -5000:
        score -= INSIDER_SELL_PENALTY

    # --- Phase 2: Reddit sentiment ---
    # Crowd sentiment as a mild confirmation factor (best-effort feed).
    reddit = extras.get("reddit_sentiment")
    if reddit is not None:
        score += reddit * REDDIT_SENTIMENT_WEIGHT

    # --- Phase 2: earnings proximity ---
    # Buying into an earnings print means carrying overnight gap risk -- a
    # daytrader's worst trade. Names reporting within the window get a
    # penalty so they rarely pass the pre-screen (the position-sizing layer
    # also shrinks them when they do slip through).
    days = extras.get("days_until_earnings")
    if days is not None and 0 <= days <= EARNINGS_PROXIMITY_DAYS:
        score -= 8.0

    # --- Phase 2: SEC filings ---
    # A fresh 8-K means something material just happened; a 10-Q/10-K is the
    # quarterly scorecard. Mild confirmation for the story the chart tells.
    filings = extras.get("recent_filings") or []
    if any(f.startswith("8-K") for f in filings):
        score += 3.0
    elif any(f.startswith(("10-Q", "10-K")) for f in filings):
        score += 2.0

    return max(0.0, min(100.0, score))
