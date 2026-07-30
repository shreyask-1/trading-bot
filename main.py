"""
Ties the entire multi-stage quantitative pipeline together. Runs every 2 minutes 
(via cron-job.org or local cron), executing risk management and position-cap checks on 
every run.

Only calls Gemini for trade validations once every GEMINI_CALL_INTERVAL_MINUTES
to remain strictly within free-tier API limits. Timestamping occurs BEFORE the call 
to ensure failure/quota cooldown compliance.
"""

import json
import os
from datetime import datetime, timedelta

from trader import (
    get_account_snapshot,
    execute_trade,
    check_stop_loss_take_profit,
    check_position_caps,
    record_performance_snapshot,
)
from news import get_news_candidates
from decide import get_trade_decisions
from signal_score import compute_signal_score
from market_regime import get_market_regime
from config import WATCHLIST, GEMINI_CALL_INTERVAL_MINUTES, GEMINI_TIMESTAMP_FILE, MIN_SIGNAL_SCORE

LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")


def _should_call_gemini():
    if not os.path.exists(GEMINI_TIMESTAMP_FILE):
        return True
    try:
        with open(GEMINI_TIMESTAMP_FILE, "r") as f:
            last_call = datetime.fromisoformat(f.read().strip())
        return datetime.now() - last_call >= timedelta(minutes=GEMINI_CALL_INTERVAL_MINUTES)
    except Exception:
        return True


def _mark_gemini_called():
    os.makedirs(os.path.dirname(GEMINI_TIMESTAMP_FILE), exist_ok=True)
    with open(GEMINI_TIMESTAMP_FILE, "w") as f:
        f.write(datetime.now().isoformat())


def run():
    os.makedirs(LOG_DIR, exist_ok=True)
    timestamp = datetime.now()
    log_lines = [f"=== Quantitative Pipeline Run at {timestamp.isoformat()} ==="]

    # --- Step 1: Account Snapshot ---
    try:
        account = get_account_snapshot()
    except Exception as e:
        log_lines.append(f"FATAL: Could not fetch account snapshot, aborting run. Error: {e}")
        _write_log(log_lines, timestamp)
        print("\n".join(log_lines))
        return

    log_lines.append(f"Alpaca Paper Account Value: ${account['total_value']:,.2f}")
    holdings_summary = {t: f"{p['qty']} shares ({p['unrealized_plpc']:+.2f}%)" for t, p in account["holdings"].items()}
    log_lines.append(f"Cash: ${account['cash']:,.2f} | Holdings: {holdings_summary or 'none'}")

    # --- Step 2: Immediate Risk Controls (Stop-Loss / Take-Profit) ---
    try:
        risk_sells = check_stop_loss_take_profit(account)
        if risk_sells:
            log_lines.append(f"Risk Engine triggered {len(risk_sells)} forced sell(s):")
            for result in risk_sells:
                log_lines.append(f"  -> {json.dumps(result)}")
            account = get_account_snapshot()
    except Exception as e:
        log_lines.append(f"WARNING: Stop-loss/take-profit check failed this run: {e}")

    # --- Step 3: Position Cap Enforcement ---
    try:
        cap_trims = check_position_caps(account)
        if cap_trims:
            log_lines.append(f"Position-Cap Engine triggered {len(cap_trims)} trim(s):")
            for result in cap_trims:
                log_lines.append(f"  -> {json.dumps(result)}")
            account = get_account_snapshot()
    except Exception as e:
        log_lines.append(f"WARNING: Position-cap check failed this run: {e}")

    # --- Step 4: Multi-Stage Trade Pipeline (Gated by Gemini Interval) ---
    if _should_call_gemini():
        log_lines.append("Gemini call interval reached -- evaluating new trade candidates.")
        _mark_gemini_called()

        # 4a. Market Regime Filter
        try:
            regime = get_market_regime()
            log_lines.append(f"Market Regime Detected: {regime['regime'].upper()} | Volatility: {regime['volatility']}")
        except Exception as e:
            log_lines.append(f"WARNING: Regime detection failed, defaulting to neutral context: {e}")
            regime = {"regime": "neutral", "volatility": "normal"}

        # 4b. News Candidate Aggregation
        try:
            candidates = get_news_candidates()
            log_lines.append(f"Found {len(candidates)} news-driven candidate(s).")
        except Exception as e:
            log_lines.append(f"WARNING: News fetch failed, falling back to technical watchlist: {e}")
            candidates = {}

        # 4c. Technical Watchlist Merge
        added_technical = 0
        for ticker in WATCHLIST:
            if ticker not in candidates and ticker not in account["holdings"]:
                candidates[ticker] = []
                added_technical += 1
        log_lines.append(f"Added {added_technical} watchlist ticker(s) for technical analysis.")

        # 4d. Quantitative Pre-Scoring Gate
        scored_candidates = {}
        for ticker, news in candidates.items():
            try:
                score = compute_signal_score(ticker, news, regime)
                if score >= MIN_SIGNAL_SCORE:
                    scored_candidates[ticker] = news
                    log_lines.append(f"  -> {ticker} PASSED pre-score gate ({score:.1f}/{MIN_SIGNAL_SCORE})")
                else:
                    log_lines.append(f"  -> {ticker} REJECTED by pre-score gate ({score:.1f}/{MIN_SIGNAL_SCORE})")
            except Exception as e:
                log_lines.append(f"WARNING: Pre-scoring error for {ticker}: {e}")

        # 4e. Structured Gemini Veto Agent Validation
        try:
            trades = get_trade_decisions(scored_candidates, account)
            log_lines.append(f"Gemini Veto Agent approved {len(trades)} trade(s).")
        except Exception as e:
            log_lines.append(f"WARNING: Gemini decision validation failed this run: {e}")
            trades = []

        # 4f. Trade Execution via Alpaca Broker Engine
        for trade in trades:
            try:
                result = execute_trade(trade, account)
                log_lines.append(f"  -> EXECUTED: {json.dumps(result)}")
            except Exception as e:
                log_lines.append(f"  -> FAILED to execute trade for {trade.get('ticker')}: {e}")
    else:
        log_lines.append("Skipping Gemini evaluation (cooldown active) -- risk management checks complete.")

    # --- Step 5: Final Performance Snapshot Recording ---
    try:
        final_account = get_account_snapshot()
        record_performance_snapshot(final_account, LOG_DIR)
        log_lines.append(f"Ending Portfolio Value: ${final_account['total_value']:,.2f}")
    except Exception as e:
        log_lines.append(f"WARNING: Could not record final performance snapshot: {e}")

    _write_log(log_lines, timestamp)
    print("\n".join(log_lines))


def _write_log(log_lines, timestamp):
    log_lines.append("")
    log_path = os.path.join(LOG_DIR, f"{timestamp.strftime('%Y-%m-%d')}.log")
    with open(log_path, "a") as f:
        f.write("\n".join(log_lines) + "\n")


if __name__ == "__main__":
    run()
