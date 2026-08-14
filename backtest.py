"""
backtest.py

Historical simulation of the QUANTITATIVE layer only: signal_score.py's
pre-screen, market_regime.py's regime filter, and trader.py's ATR-based
stop-loss/take-profit sizing rules -- run against real historical daily
bars from Alpaca.

WHAT THIS DOES NOT DO, AND CANNOT DO:
This does NOT backtest Gemini's decisions. There is no honest way to
replay what an LLM would have said on a past date: its outputs are
non-deterministic, and a model queried today may already "know" what
happened after that historical date from its training data, which would
silently invalidate any results. What CAN be tested honestly is the
deterministic part of the pipeline -- the signal score formula, the
regime filter, and the ATR stop/target math -- which is what this
simulates. It substitutes signal_score (0-100) for the "conviction"
value the live bot gets from Gemini.

Treat the output as a sanity check on the risk/sizing rules, NOT as a
projection of what the live LLM-driven bot will return. The two systems
only partially overlap.

Usage:
    python backtest.py --start 2023-01-01 --end 2025-01-01 --capital 10000
    python backtest.py --start 2022-01-01 --end 2025-01-01 --tickers AAPL MSFT NVDA
"""

import argparse
import csv
import os
from datetime import datetime, timedelta

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, WATCHLIST, MAX_POSITION_PCT,
    ATR_STOP_MULTIPLIER, ATR_TAKE_PROFIT_MULTIPLIER, ATR_PERIOD,
    MIN_SIGNAL_SCORE_TO_CONSIDER, REGIME_POSITION_MULTIPLIERS,
    MARKET_HIGH_VOLATILITY_THRESHOLD,
)
import indicators as ind
from market_regime import evaluate_market_regime
from signal_score import calculate_signal_score

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "backtests")
WARMUP_BARS = 60  # min bars of history required before a ticker is evaluated

data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)


def _fetch_history(ticker, start, end):
    request = StockBarsRequest(symbol_or_symbols=ticker, timeframe=TimeFrame.Day, start=start, end=end)
    return list(data_client.get_stock_bars(request)[ticker])


def _compute_indicator_snapshot(closes, highs, lows, volumes):
    """Mirrors trader.get_full_indicators()'s dict shape, but from local
    slices instead of a live API call -- lets us reuse signal_score.py
    unmodified."""
    macd = ind.compute_macd(closes)
    bb = ind.compute_bollinger_bands(closes)
    stoch = ind.compute_stochastic(highs, lows, closes)
    return {
        "price": closes[-1],
        "sma_20": ind.compute_sma(closes, 20),
        "sma_50": ind.compute_sma(closes, 50),
        "rsi_14": ind.compute_rsi(closes),
        "atr_14": ind.compute_atr(highs, lows, closes, period=ATR_PERIOD),
        "adx_14": ind.compute_adx(highs, lows, closes),
        "macd": macd,
        "bollinger": bb,
        "stochastic": stoch,
        "momentum_10d": ind.compute_momentum(closes, period=10),
        "volatility_20d": ind.compute_volatility(closes, period=20),
        "volume_trend": ind.compute_volume_trend(volumes),
        "relative_volume_pct": ind.compute_relative_volume(volumes),
        "trend": ind.classify_trend(closes),
    }


def run_backtest(start, end, capital, max_positions, tickers):
    tradeable = [t for t in tickers if t != "SPY"]
    fetch_list = list(dict.fromkeys(tradeable + ["SPY"]))  # dedupe, keep order, ensure SPY present

    print(f"Fetching history for {len(fetch_list)} tickers from {start.date()} to {end.date()}...")
    bars = {}
    for t in fetch_list:
        try:
            bars[t] = _fetch_history(t, start, end)
        except Exception as e:
            if t == "SPY":
                raise RuntimeError(f"Could not fetch SPY history (required for regime filter): {e}")
            print(f"  Skipping {t}: {e}")
    if not bars.get("SPY"):
        raise RuntimeError("No SPY data returned -- cannot run regime-aware backtest.")

    tradeable = [t for t in tradeable if t in bars and bars[t]]
    fetch_list = [t for t in fetch_list if t in bars and bars[t]]
    spy_dates = sorted({b.timestamp.date() for b in bars["SPY"]})

    ptr = {t: 0 for t in fetch_list}
    running = {t: {"closes": [], "highs": [], "lows": [], "volumes": []} for t in fetch_list}

    cash = capital
    open_positions = {}   # ticker -> position dict
    closed_trades = []
    equity_curve = []     # (date_iso, equity, regime)

    for d in spy_dates:
        today_bar = {}
        for t in fetch_list:
            tbars = bars[t]
            p = ptr[t]
            while p < len(tbars) and tbars[p].timestamp.date() <= d:
                b = tbars[p]
                if b.timestamp.date() == d:
                    today_bar[t] = b
                running[t]["closes"].append(b.close)
                running[t]["highs"].append(b.high)
                running[t]["lows"].append(b.low)
                running[t]["volumes"].append(b.volume)
                p += 1
            ptr[t] = p

        spy_closes_so_far = running["SPY"]["closes"]
        if len(spy_closes_so_far) < WARMUP_BARS:
            continue  # not enough SPY history yet to evaluate regime meaningfully

        regime = evaluate_market_regime(spy_closes_so_far, high_vol_threshold=MARKET_HIGH_VOLATILITY_THRESHOLD)
        multiplier = REGIME_POSITION_MULTIPLIERS.get(regime, 0.6)

        # 1. Manage open positions: check stop/target against today's high/low
        for ticker in list(open_positions.keys()):
            bar = today_bar.get(ticker)
            if bar is None:
                continue
            pos = open_positions[ticker]
            exit_price, exit_reason = None, None
            if bar.low <= pos["stop_level"]:
                exit_price, exit_reason = pos["stop_level"], "stop_loss"
            elif bar.high >= pos["target_level"]:
                exit_price, exit_reason = pos["target_level"], "take_profit"
            if exit_price is not None:
                proceeds = exit_price * pos["shares"]
                pnl = proceeds - pos["dollar_invested"]
                cash += proceeds
                closed_trades.append({
                    "ticker": ticker, "entry_date": pos["entry_date"], "exit_date": d.isoformat(),
                    "entry_price": round(pos["entry_price"], 2), "exit_price": round(exit_price, 2),
                    "shares": round(pos["shares"], 4), "pnl": round(pnl, 2),
                    "pnl_pct": round((pnl / pos["dollar_invested"]) * 100, 2) if pos["dollar_invested"] else 0,
                    "exit_reason": exit_reason, "score_at_entry": pos["score_at_entry"],
                    "regime_at_entry": pos["regime_at_entry"],
                })
                del open_positions[ticker]

        # 2. Mark to market
        equity = cash
        for ticker, pos in open_positions.items():
            bar = today_bar.get(ticker)
            if bar:
                pos["last_known_price"] = bar.close
            equity += pos["last_known_price"] * pos["shares"]
        equity_curve.append((d.isoformat(), round(equity, 2), regime))

        # 3. Consider new entries
        if multiplier > 0 and len(open_positions) < max_positions:
            candidates = []
            for ticker in tradeable:
                if ticker in open_positions:
                    continue
                bar = today_bar.get(ticker)
                if bar is None:
                    continue
                closes, highs, lows, volumes = (
                    running[ticker]["closes"], running[ticker]["highs"],
                    running[ticker]["lows"], running[ticker]["volumes"],
                )
                if len(closes) < WARMUP_BARS:
                    continue
                snap = _compute_indicator_snapshot(closes, highs, lows, volumes)
                if snap["atr_14"] is None:
                    continue
                score = calculate_signal_score(snap)
                if score >= MIN_SIGNAL_SCORE_TO_CONSIDER:
                    candidates.append((score, ticker, bar.close, snap["atr_14"]))

            candidates.sort(reverse=True)
            for score, ticker, price, atr in candidates:
                if len(open_positions) >= max_positions:
                    break
                position_dollar = min(cash, equity * MAX_POSITION_PCT * multiplier * (score / 100.0))
                if position_dollar < 100:
                    continue
                shares = position_dollar / price
                cash -= position_dollar
                open_positions[ticker] = {
                    "entry_date": d.isoformat(), "entry_price": price, "shares": shares,
                    "dollar_invested": position_dollar,
                    "stop_level": price - ATR_STOP_MULTIPLIER * atr,
                    "target_level": price + ATR_TAKE_PROFIT_MULTIPLIER * atr,
                    "score_at_entry": round(score, 1), "regime_at_entry": regime,
                    "last_known_price": price,
                }

    # Close anything still open at the end of the backtest window
    for ticker, pos in open_positions.items():
        proceeds = pos["last_known_price"] * pos["shares"]
        pnl = proceeds - pos["dollar_invested"]
        cash += proceeds
        closed_trades.append({
            "ticker": ticker, "entry_date": pos["entry_date"], "exit_date": spy_dates[-1].isoformat(),
            "entry_price": round(pos["entry_price"], 2), "exit_price": round(pos["last_known_price"], 2),
            "shares": round(pos["shares"], 4), "pnl": round(pnl, 2),
            "pnl_pct": round((pnl / pos["dollar_invested"]) * 100, 2) if pos["dollar_invested"] else 0,
            "exit_reason": "end_of_backtest", "score_at_entry": pos["score_at_entry"],
            "regime_at_entry": pos["regime_at_entry"],
        })
    final_equity = cash

    return equity_curve, closed_trades, final_equity, bars["SPY"]


def build_setup_string(snap):
    """
    Indicator-regime setup string from an indicator snapshot. MUST match
    trader.py's _live_setup_string() exactly (same fields, same thresholds)
    -- this is the shared taxonomy that lets the live bot consult the gate
    the walk-forward learns. Deterministic (no lookahead): built purely from
    the snapshot available on the entry day.
    """
    trend = snap.get("trend") or "sideways"
    rsi = snap.get("rsi_14")
    if rsi is None:
        rsi_zone = "n/a"
    elif rsi < 30:
        rsi_zone = "oversold"
    elif rsi > 70:
        rsi_zone = "overbought"
    else:
        rsi_zone = "neutral"
    macd = snap.get("macd_cross") or "none"
    mom = snap.get("momentum_10d")
    mom_zone = "pos" if (mom is not None and mom > 0) else ("neg" if (mom is not None and mom < 0) else "flat")
    vol = snap.get("volatility_20d")
    vol_zone = "hi" if (vol is not None and vol >= 2.0) else "lo"
    return f"{trend}|{rsi_zone}|{macd}|{mom_zone}|{vol_zone}"


def run_walk_forward(start, end, capital, max_positions, train_days, test_days, tickers):
    """
    Walk-forward backtest: the setup gate is learned ONLY from trades closed
    in the preceding TRAIN window and applied to the next TEST window, then
    rolled forward -- so every test trade is gated by setups that
    demonstrably won BEFORE it, with no lookahead. This is the honest way to
    learn "which setups actually work" from historical data.

    Returns (equity_curve, closed_trades, final_equity, spy_bars, gate_log)
    where gate_log is [{date, gate_ticker_setups}] so you can see which
    setups were allowed in each window.
    """
    tradeable = [t for t in tickers if t != "SPY"]
    fetch_list = list(dict.fromkeys(tradeable + ["SPY"]))
    print(f"Walk-forward: fetching history for {len(fetch_list)} tickers "
          f"({start.date()} to {end.date()}), train {train_days}d / test {test_days}d...")
    bars = {}
    for t in fetch_list:
        try:
            bars[t] = _fetch_history(t, start, end)
        except Exception as e:
            if t == "SPY":
                raise RuntimeError(f"Could not fetch SPY history: {e}")
            print(f"  Skipping {t}: {e}")
    if not bars.get("SPY"):
        raise RuntimeError("No SPY data returned -- cannot run walk-forward.")
    tradeable = [t for t in tradeable if t in bars and bars[t]]
    fetch_list = [t for t in fetch_list if t in bars and bars[t]]
    spy_dates = sorted({b.timestamp.date() for b in bars["SPY"]})

    ptr = {t: 0 for t in fetch_list}
    running = {t: {"closes": [], "highs": [], "lows": [], "volumes": []} for t in fetch_list}
    cash = capital
    open_positions = {}
    closed_trades = []
    equity_curve = []
    gate_log = []

    # Rolling setup memory: every closed trade contributes (exit_date, setup,
    # pnl_pct). The gate for a test window is built from trades closed in the
    # prior `train_days` -- never including the test window itself.
    outcome_history = []  # (exit_date, setup, pnl_pct)
    setup_gate = None  # None = no gate yet (cold start allows all setups)
    MIN_GATE_SAMPLES = 5  # need this many closes of a setup before trusting it
    EDGE_MIN = 0.0  # setup must have positive average return to be tradable

    for d in spy_dates:
        today_bar = {}
        for t in fetch_list:
            tbars = bars[t]
            p = ptr[t]
            while p < len(tbars) and tbars[p].timestamp.date() <= d:
                b = tbars[p]
                if b.timestamp.date() == d:
                    today_bar[t] = b
                running[t]["closes"].append(b.close)
                running[t]["highs"].append(b.high)
                running[t]["lows"].append(b.low)
                running[t]["volumes"].append(b.volume)
                p += 1
            ptr[t] = p

        spy_closes_so_far = running["SPY"]["closes"]
        if len(spy_closes_so_far) < WARMUP_BARS:
            continue

        regime = evaluate_market_regime(spy_closes_so_far, high_vol_threshold=MARKET_HIGH_VOLATILITY_THRESHOLD)
        multiplier = REGIME_POSITION_MULTIPLIERS.get(regime, 0.6)

        # Manage open positions (stops/targets)
        for ticker in list(open_positions.keys()):
            bar = today_bar.get(ticker)
            if bar is None:
                continue
            pos = open_positions[ticker]
            exit_price, exit_reason = None, None
            if bar.low <= pos["stop_level"]:
                exit_price, exit_reason = pos["stop_level"], "stop_loss"
            elif bar.high >= pos["target_level"]:
                exit_price, exit_reason = pos["target_level"], "take_profit"
            if exit_price is not None:
                proceeds = exit_price * pos["shares"]
                pnl = proceeds - pos["dollar_invested"]
                cash += proceeds
                pnl_pct = (pnl / pos["dollar_invested"]) * 100 if pos["dollar_invested"] else 0.0
                outcome_history.append((d, pos["setup"], pnl_pct))
                closed_trades.append({
                    "ticker": ticker, "entry_date": pos["entry_date"], "exit_date": d.isoformat(),
                    "entry_price": round(pos["entry_price"], 2), "exit_price": round(exit_price, 2),
                    "shares": round(pos["shares"], 4), "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2),
                    "exit_reason": exit_reason, "score_at_entry": pos["score_at_entry"],
                    "regime_at_entry": pos["regime_at_entry"], "setup": pos["setup"],
                })
                del open_positions[ticker]

        # Rebuild the gate at the start of each new TEST window: look only at
        # outcomes closed within the preceding train_days (strictly before the
        # first day of this window is implicit -- outcomes are historical).
        # CRITICAL: until there is at least SOME outcome history the gate stays
        # None (exploration mode, all setups tradable). Building an empty gate
        # before any trade ever closed would filter out every setup and lock
        # the walk-forward into zero trades forever.
        if setup_gate is None or (d - last_gate_date).days >= test_days:
            cutoff = d - timedelta(days=train_days)
            recent = [(s, p) for (ed, s, p) in outcome_history if ed >= cutoff]
            if outcome_history:
                by_setup = {}
                for s, p in recent:
                    by_setup.setdefault(s, []).append(p)
                setup_gate = set()
                for s, samples in by_setup.items():
                    if len(samples) >= MIN_GATE_SAMPLES and sum(samples) / len(samples) > EDGE_MIN:
                        setup_gate.add(s)
                if not recent:
                    # No outcomes in the train window (e.g. every name was in
                    # a stop on entry-day regimes) -- fall back to exploration
                    # rather than locking everything out.
                    setup_gate = None
            last_gate_date = d
            gate_log.append({"date": d.isoformat(), "setups": sorted(setup_gate or [])[:12], "n_outcomes": len(recent)})

        # Mark to market
        equity = cash
        for ticker, pos in open_positions.items():
            bar = today_bar.get(ticker)
            if bar:
                pos["last_known_price"] = bar.close
            equity += pos["last_known_price"] * pos["shares"]
        equity_curve.append((d.isoformat(), round(equity, 2), regime))

        # Consider new entries -- only setups that won in the train window
        if multiplier > 0 and len(open_positions) < max_positions:
            candidates = []
            for ticker in tradeable:
                if ticker in open_positions:
                    continue
                bar = today_bar.get(ticker)
                if bar is None:
                    continue
                closes, highs, lows, volumes = (
                    running[ticker]["closes"], running[ticker]["highs"],
                    running[ticker]["lows"], running[ticker]["volumes"],
                )
                if len(closes) < WARMUP_BARS:
                    continue
                snap = _compute_indicator_snapshot(closes, highs, lows, volumes)
                if snap["atr_14"] is None:
                    continue
                score = calculate_signal_score(snap)
                if score < MIN_SIGNAL_SCORE_TO_CONSIDER:
                    continue
                setup = build_setup_string(snap)
                if setup_gate is not None and setup not in setup_gate:
                    continue  # setup did not prove itself in the train window
                candidates.append((score, ticker, bar.close, snap["atr_14"], setup))

            candidates.sort(reverse=True)
            for score, ticker, price, atr, setup in candidates:
                if len(open_positions) >= max_positions:
                    break
                position_dollar = min(cash, equity * MAX_POSITION_PCT * multiplier * (score / 100.0))
                if position_dollar < 100:
                    continue
                shares = position_dollar / price
                cash -= position_dollar
                open_positions[ticker] = {
                    "entry_date": d.isoformat(), "entry_price": price, "shares": shares,
                    "dollar_invested": position_dollar,
                    "stop_level": price - ATR_STOP_MULTIPLIER * atr,
                    "target_level": price + ATR_TAKE_PROFIT_MULTIPLIER * atr,
                    "score_at_entry": round(score, 1), "regime_at_entry": regime,
                    "last_known_price": price, "setup": setup,
                }

    # Close anything still open at the end
    for ticker, pos in open_positions.items():
        proceeds = pos["last_known_price"] * pos["shares"]
        pnl = proceeds - pos["dollar_invested"]
        cash += proceeds
        closed_trades.append({
            "ticker": ticker, "entry_date": pos["entry_date"], "exit_date": spy_dates[-1].isoformat(),
            "entry_price": round(pos["entry_price"], 2), "exit_price": round(pos["last_known_price"], 2),
            "shares": round(pos["shares"], 4), "pnl": round(pnl, 2),
            "pnl_pct": round((pnl / pos["dollar_invested"]) * 100, 2) if pos["dollar_invested"] else 0,
            "exit_reason": "end_of_backtest", "score_at_entry": pos["score_at_entry"],
            "regime_at_entry": pos["regime_at_entry"], "setup": pos.get("setup"),
        })

    # Persist the learned gate for the LIVE bot (Phase 3b): data/setup_gate.json
    # carries the final proven-setup gate plus win stats per setup from ALL
    # closed walk-forward trades. trader.get_walkforward_multiplier() computes
    # the same setup string from live indicators and sizes by this edge until
    # the live journal has its own samples.
    try:
        import json as _json
        stats = {}
        for s, p in outcome_history:
            st = stats.setdefault(s, {"n": 0, "wins": 0, "sum": 0.0})
            st["n"] += 1
            st["sum"] += p
            if p > 0:
                st["wins"] += 1
        stats_out = {}
        for s, st in stats.items():
            stats_out[s] = {
                "n": st["n"],
                "wins": st["wins"],
                "win_rate": round(st["wins"] / st["n"], 4) if st["n"] else 0.0,
                "avg_pnl_pct": round(st["sum"] / st["n"], 4) if st["n"] else 0.0,
            }
        gate_path = os.path.join(os.path.dirname(__file__), "data", "setup_gate.json")
        os.makedirs(os.path.dirname(gate_path), exist_ok=True)
        with open(gate_path, "w") as f:
            _json.dump({
                "generated_at": datetime.now().isoformat(),
                "train_days": train_days,
                "test_days": test_days,
                "n_closed_trades": len(closed_trades),
                "gate": sorted(setup_gate or []),
                "stats": stats_out,
            }, f, indent=2)
        print(f"Learned setup gate saved to {gate_path} ({len(setup_gate or [])} proven setups) -- the live bot will size entries by it (Phase 3b).")
    except Exception as e:
        print(f"Could not persist setup gate for live use: {e}")

    return equity_curve, closed_trades, cash, bars["SPY"], gate_log


def _summarize_and_save(equity_curve, closed_trades, capital, final_equity, spy_bars, start, end):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    tag = f"{start.date()}_{end.date()}"

    eq_path = os.path.join(OUTPUT_DIR, f"equity_{tag}.csv")
    with open(eq_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "equity", "regime"])
        w.writerows(equity_curve)

    trades_path = os.path.join(OUTPUT_DIR, f"trades_{tag}.csv")
    with open(trades_path, "w", newline="") as f:
        fieldnames = ["ticker", "entry_date", "exit_date", "entry_price", "exit_price",
                      "shares", "pnl", "pnl_pct", "exit_reason", "score_at_entry", "regime_at_entry"]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(closed_trades)

    total_return_pct = ((final_equity / capital) - 1) * 100 if capital else 0
    peak, max_dd = float("-inf"), 0.0
    for _, eq, _ in equity_curve:
        peak = max(peak, eq)
        if peak > 0:
            max_dd = min(max_dd, (eq - peak) / peak)
    max_drawdown_pct = max_dd * 100

    wins = [t for t in closed_trades if t["pnl"] > 0]
    win_rate = (len(wins) / len(closed_trades) * 100) if closed_trades else 0.0

    spy_start, spy_end = spy_bars[0].close, spy_bars[-1].close
    spy_return_pct = ((spy_end / spy_start) - 1) * 100 if spy_start else 0

    print("=" * 64)
    print(f"Backtest: {start.date()} to {end.date()}")
    print(f"Starting capital: ${capital:,.2f}")
    print(f"Ending equity:    ${final_equity:,.2f}")
    print(f"Total return:     {total_return_pct:+.2f}%")
    print(f"Max drawdown:     {max_drawdown_pct:.2f}%")
    print(f"Closed trades:    {len(closed_trades)}")
    print(f"Win rate:         {win_rate:.1f}%")
    print("-" * 64)
    print(f"Benchmark -- buy & hold SPY over same period: {spy_return_pct:+.2f}%")
    print("=" * 64)
    print(f"Equity curve saved to: {eq_path}")
    print(f"Trade log saved to:    {trades_path}")
    print("=" * 64)
    print("IMPORTANT: this backtests the deterministic signal-score + regime")
    print("+ ATR sizing rules ONLY. It does NOT and cannot backtest what")
    print("Gemini would have said on any historical date. Treat this as a")
    print("sanity check on the risk/sizing rules, not a promise about the")
    print("live LLM-driven bot's future returns.")
    print("=" * 64)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--capital", type=float, default=10000.0)
    parser.add_argument("--max-positions", type=int, default=6)
    parser.add_argument("--tickers", nargs="*", default=None, help="Override WATCHLIST for this run")
    parser.add_argument("--walkforward", action="store_true", help="Walk-forward mode: gate each test window on setups that won in the prior train window (no lookahead)")
    parser.add_argument("--train-days", type=int, default=120, help="Walk-forward train window (days of closed-trade history used to build the setup gate)")
    parser.add_argument("--test-days", type=int, default=30, help="Walk-forward test window length (days per rolling test period)")
    args = parser.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d")
    end = datetime.strptime(args.end, "%Y-%m-%d")
    tickers = args.tickers if args.tickers else list(WATCHLIST)

    if args.walkforward:
        equity_curve, closed_trades, final_equity, spy_bars, gate_log = run_walk_forward(
            start, end, args.capital, args.max_positions, args.train_days, args.test_days, tickers
        )
        _summarize_and_save(equity_curve, closed_trades, args.capital, final_equity, spy_bars, start, end)
        print("Walk-forward setup gates per window (which setups were tradable):")
        for g in gate_log:
            print(f"  {g['date']}: {len(g['setups'])} proven setups ({g['n_outcomes']} train outcomes)")
    else:
        equity_curve, closed_trades, final_equity, spy_bars = run_backtest(
            start, end, args.capital, args.max_positions, tickers
        )
        _summarize_and_save(equity_curve, closed_trades, args.capital, final_equity, spy_bars, start, end)


if __name__ == "__main__":
    main()
