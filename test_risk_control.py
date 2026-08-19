"""
Smoke tests for the new risk-control machinery (no network calls).

Run with:  python test_risk_controls.py
Requires the project venv or installed requirements (alpaca-py, google-genai).

Covers:
1. execute_trade hard no-margin rule (buys blocked when cash < 0)
2. execute_trade pending-order-aware cash + exposure cap
3. pending_order_notional math
4. circuit breakers: daily-loss halt, drawdown sizing cut, flatten threshold
5. enforce_deleveraging heals a negative-cash account
6. get_gross_exposure
"""

import os
import sys
import json
import csv
import shutil
import tempfile
from types import SimpleNamespace

# Dummy keys so config.py doesn't raise at import time.
os.environ.setdefault("FINNHUB_API_KEY", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("ALPACA_API_KEY", "dummy")
os.environ.setdefault("ALPACA_SECRET_KEY", "dummy")
# The sizing tests below assert tiered-mode quantities (conviction/confidence/
# risk-based caps). Keep them deterministic by defaulting to tiered sizing
# here; the flat-sizing behavior is covered by its own dedicated test.
os.environ.setdefault("FLAT_SIZING", "false")
# Keep legacy sizing assertions deterministic; production defaults use a
# larger 10% reserve, while these tests intentionally model the older 5%
# reserve to exercise order math without changing the account state.
os.environ.setdefault("MIN_CASH_RESERVE_PCT", "0.05")
# Unit tests stub get_price/get_full_indicators; production enables strict
# quote, candle, spread, and liquidity guards by default.
os.environ.setdefault("ENABLE_MARKET_DATA_GUARDS", "false")
# Keep the broad smoke suite independent of the production turnover budget;
# the turnover guard has its own focused test override.
os.environ.setdefault("MAX_DAILY_TURNOVER_PCT", "0")

sys.path.insert(0, os.path.dirname(__file__))

import trader
import config

# Captured before any test patches it, so time-dependent tests can still call
# the real implementation.
_REAL_GET_TOD = trader.get_time_of_day_multiplier
# The cached indicator wrappers are also patched over by earlier tests; keep
# the real ones so the cache test can restore them.
_REAL_GET_PRICE_HISTORY = trader.get_price_history
_REAL_GET_FULL_INDICATORS = trader.get_full_indicators

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"FAIL  {name}  {detail}")


class FakeOrder:
    def __init__(self, symbol, qty, side):
        self.symbol = symbol
        self.qty = qty
        self.side = side  # already lowercase str like "buy"


# --- 1. no-margin rule -----------------------------------------------------
def test_no_margin_rule():
    print("\n[1] Hard no-margin rule: buys blocked when cash < 0")
    account = {
        "cash": -4761.65,
        "total_value": 88444.0,
        "holdings": {},
    }
    # Patch the network surface: no open orders, no prices needed for the buy path
    # (it should bail before fetching anything beyond the snapshot).
    trader.trading_client = type("FakeClient", (), {"get_orders": lambda *a, **k: []})()
    trader.get_price = lambda s: 100.0

    result = trader.execute_trade(
        {"ticker": "NVDA", "action": "buy", "dollar_amount": 5000, "conviction": 9}, account
    )
    check("buy skipped with no-margin reason", result["status"] == "skipped" and "no-margin" in result["reason"], json.dumps(result))

    # Sells are still allowed with negative cash (that's how we heal).
    account["holdings"] = {"NVDA": {"qty": 10, "current_price": 100.0}}
    submitted = {}
    def fake_submit(order_request):
        submitted["symbol"] = order_request.symbol
        return type("O", (), {"id": "x", "status": "accepted"})()
    trader.trading_client.submit_order = fake_submit
    result = trader.execute_trade(
        {"ticker": "NVDA", "action": "sell", "dollar_amount": 0, "conviction": 10}, account
    )
    check("sell allowed while cash negative", result["status"] == "submitted", json.dumps(result))


# --- 2. pending-order-aware cash -------------------------------------------
def test_pending_order_aware_cash():
    print("\n[2] Pending orders reserve cash; exposure cap limits size")
    account = {
        "cash": 20000.0,
        "total_value": 100000.0,
        "holdings": {},
    }
    # One open BUY order for 50 shares of AAPL at $200 = $10,000 committed.
    trader.trading_client = type("FakeClient", (), {
        "get_orders": lambda *a, **k: [FakeOrder("AAPL", 50, "buy")],
        "submit_order": lambda self, req: type("O", (), {"id": "x", "status": "accepted"})(),
    })()
    trader.get_price = lambda s: 200.0
    trader.get_price_history = lambda *a, **k: None  # silence custom-exit network noise
    # Deterministic sizing: neutralize time-of-day multiplier and give a tight
    # ATR so the risk-based cap (0.75% of equity) doesn't bind at this size.
    trader.get_time_of_day_multiplier = lambda *a, **k: 1.0
    trader.get_full_indicators = lambda s: {"atr_14": 1.0, "vwap": 199.0, "intraday_momentum_pct": 0.5}

    result = trader.execute_trade(
        {"ticker": "MSFT", "action": "buy", "dollar_amount": 20000, "conviction": 8}, account
    )
    # conviction 8 (non-exceptional): reserve kept = 5k.
    # max_allowed = 100k*0.15*0.8 = 12k; buy_target = min(20k, 12k) = 12k
    # available_cash = 20k - 5k(reserve) - 10k(pending AAPL buy) = 5k -> qty 25 @ $200
    if result["status"] == "submitted":
        check("buy scaled by pending notional (qty 25)", abs(result["qty"] - 25.0) < 0.01, json.dumps(result))
    else:
        check("buy scaled by pending notional (qty 25)", False, json.dumps(result))


def test_exposure_cap():
    print("\n[3] Total exposure cap blocks over-extension")
    # This test isolates the total-exposure calculation; sector concentration
    # has its own dedicated production guard and test.
    old_sector_cap = trader.MAX_SECTOR_EXPOSURE_PCT
    trader.MAX_SECTOR_EXPOSURE_PCT = 0.0
    account = {
        "cash": 50000.0,
        "total_value": 100000.0,
        "holdings": {
            "AAPL": {"qty": 100, "current_price": 800.0},  # 80k exposure = 80%
        },
    }
    trader.trading_client = type("FakeClient", (), {
        "get_orders": lambda *a, **k: [],
        "submit_order": lambda self, req: type("O", (), {"id": "x", "status": "accepted"})(),
    })()
    trader.get_price = lambda s: 100.0
    trader.get_price_history = lambda *a, **k: None
    # Deterministic sizing: neutralize time-of-day multiplier; tight ATR so the
    # risk-based cap (stop 2.5% away -> cap $30k) doesn't bind at $10k.
    trader.get_time_of_day_multiplier = lambda *a, **k: 1.0
    trader.get_full_indicators = lambda s: {"atr_14": 1.0, "vwap": 99.5, "intraday_momentum_pct": 0.5}
    result = trader.execute_trade(
        {"ticker": "MSFT", "action": "buy", "dollar_amount": 10000, "conviction": 10}, account
    )
    # exposure 80k + 0 pending; room = 90k - 80k = 10k; amount = min(10k, cash room, 10k)
    # amount 10k -> qty 100 at $100
    if result["status"] == "submitted":
        check("exposure cap allows exactly the room (qty 100)", abs(result["qty"] - 100.0) < 0.01, json.dumps(result))
    else:
        check("exposure cap allows exactly the room (qty 100)", False, json.dumps(result))

    # At 89.9% exposure with the default 90% cap, room = 90k - 89.9k = $100
    # -> the buy is capped to the remaining room (qty 1 @ $100).
    account["holdings"] = {"AAPL": {"qty": 100, "current_price": 899.0}}
    result = trader.execute_trade(
        {"ticker": "MSFT", "action": "buy", "dollar_amount": 10000, "conviction": 10}, account
    )
    if result["status"] == "submitted":
        check("exposure cap trims to remaining room (qty 1)", abs(result["qty"] - 1.0) < 0.01, json.dumps(result))
    else:
        check("exposure cap trims to remaining room (qty 51)", False, json.dumps(result))
    trader.MAX_SECTOR_EXPOSURE_PCT = old_sector_cap


# --- 4. circuit breakers ----------------------------------------------------
def test_circuit_breakers():
    print("\n[4] Circuit breakers (opt-in; disabled by default)")
    state_file = trader.RISK_STATE_FILE
    if os.path.exists(state_file):
        os.remove(state_file)

    old_daily = trader.DAILY_LOSS_HALT_PCT
    old_flatten = trader.MAX_DRAWDOWN_FLATTEN_PCT

    # Default config: both halts disabled -> the bot NEVER stops trading for
    # the day, even at deep drawdown. The sizing cut still applies (it only
    # shrinks new buys; it doesn't stop trading).
    trader.DAILY_LOSS_HALT_PCT = 0.0
    trader.MAX_DRAWDOWN_FLATTEN_PCT = 0.0

    halted, reason, mult, dd, daily, peak, msgs = trader.evaluate_circuit_breakers({"total_value": 100000.0})
    check("peak initializes on first run", peak == 100000.0, str(peak))

    halted, reason, mult, dd, daily, peak, msgs = trader.evaluate_circuit_breakers({"total_value": 100500.0})
    check("peak rises to 100500", peak == 100500.0, str(peak))
    check("no halt on small up day", halted is False, str(halted))

    state = trader.load_risk_state()
    state["day"] = None  # force new day anchor at current (lower) equity
    trader.save_risk_state(state)
    halted, reason, mult, dd, daily, peak, msgs = trader.evaluate_circuit_breakers({"total_value": 94500.0})
    check("6% drawdown -> sizing cut to 0.25", mult == config.DELEVERAGE_SIZE_MULTIPLIER, f"mult={mult}")
    check("6% drawdown -> NOT halted (halts disabled)", halted is False, str(halted))

    state = trader.load_risk_state()
    state["day"] = None
    trader.save_risk_state(state)
    halted, reason, mult, dd, daily, peak, msgs = trader.evaluate_circuit_breakers({"total_value": 91000.0})
    check("9% drawdown -> still NOT halted when disabled", halted is False, str(halted))
    check("9% drawdown -> sizing still cut", mult == config.DELEVERAGE_SIZE_MULTIPLIER, f"mult={mult}")

    # A stale 'halted' flag from a previous config/day must not lock the bot
    # out when the breakers are disabled.
    state = trader.load_risk_state()
    state["halted"] = True
    state["halt_reason"] = "stale from old config"
    trader.save_risk_state(state)
    halted, reason, mult, dd, daily, peak, msgs = trader.evaluate_circuit_breakers({"total_value": 91000.0})
    check("stale halted flag cleared when breakers disabled", halted is False, str(reason))

    # Re-enabled: daily-loss halt at 3% and flatten at 8% behave as before.
    trader.DAILY_LOSS_HALT_PCT = 3.0
    trader.MAX_DRAWDOWN_FLATTEN_PCT = 8.0
    state = trader.load_risk_state()
    state["day"] = None
    trader.save_risk_state(state)
    halted, reason, mult, dd, daily, peak, msgs = trader.evaluate_circuit_breakers({"total_value": 91000.0})
    check("9% drawdown -> halted when flatten enabled", halted is True, str(reason))
    check("flatten reason mentions drawdown", "drawdown" in reason.lower(), reason)

    import pytz as _pytz
    now_et = _pytz.utc.localize(__import__("datetime").datetime.utcnow()).astimezone(_pytz.timezone("America/New_York"))
    state = trader.load_risk_state()
    state["peak_equity"] = 100000.0
    state["day"] = now_et.strftime("%Y-%m-%d")
    state["day_start_equity"] = 100000.0
    state["halted"] = False
    trader.save_risk_state(state)
    halted, reason, mult, dd, daily, peak, msgs = trader.evaluate_circuit_breakers({"total_value": 96000.0})
    check("daily loss >= 3% -> halted", halted is True and "daily loss" in reason.lower(), str(reason))

    trader.DAILY_LOSS_HALT_PCT = old_daily
    trader.MAX_DRAWDOWN_FLATTEN_PCT = old_flatten
    if os.path.exists(state_file):
        os.remove(state_file)


# --- 5. de-leveraging --------------------------------------------------------
def test_deleveraging():
    print("\n[5] De-leveraging heals negative cash")
    account = {
        "cash": -5000.0,
        "total_value": 100000.0,
        "holdings": {
            "STRONG": {"qty": 50, "current_price": 100.0},   # 5000
            "WEAK1": {"qty": 40, "current_price": 100.0},    # 4000
            "WEAK2": {"qty": 30, "current_price": 100.0},    # 3000
        },
    }
    trader.trading_client = type("FakeClient", (), {"get_orders": lambda *a, **k: []})()
    scores = {"WEAK1": 20.0, "WEAK2": 40.0, "STRONG": 90.0}
    trader.get_full_indicators = lambda t: {"score": scores[t]}
    trader.calculate_signal_score = lambda d: d["score"] if d else 0.0

    sold = []
    def fake_submit(order_request):
        sold.append(order_request.symbol)
        return type("O", (), {"id": "x", "status": "accepted"})()
    trader.trading_client.submit_order = fake_submit

    results = trader.enforce_deleveraging(account)
    submitted = [r for r in results if r.get("status") == "submitted"]
    # target cash = 100k * 2% = 2k. Start -5k. WEAK1 (4k) -> -1k, WEAK2 (3k) -> +2k done.
    check("sold weakest first (WEAK1, WEAK2)", submitted and [r["ticker"] for r in submitted] == ["WEAK1", "WEAK2"], str([r["ticker"] for r in submitted]))
    check("did not sell the strong holding", "STRONG" not in [r["ticker"] for r in submitted])


# --- 5b. de-leveraging counts queued sells ----------------------------------
def test_deleveraging_counts_pending_sells():
    print("\n[5b] De-leveraging counts queued sell orders as future cash")
    account = {
        "cash": -5000.0,
        "total_value": 100000.0,
        "holdings": {
            "WEAK1": {"qty": 40, "current_price": 100.0},   # 4000
            "WEAK2": {"qty": 30, "current_price": 100.0},   # 3000
            "STRONG": {"qty": 50, "current_price": 100.0},  # 5000
        },
    }
    trader.trading_client = type("FakeClient", (), {"get_orders": lambda *a, **k: []})()
    scores = {"WEAK1": 20.0, "WEAK2": 40.0, "STRONG": 90.0}
    trader.get_full_indicators = lambda t: {"score": scores[t]}
    trader.calculate_signal_score = lambda d: d["score"] if d else 0.0

    sold = []
    def fake_submit(order_request):
        sold.append(order_request.symbol)
        return type("O", (), {"id": "x", "status": "accepted"})()
    trader.trading_client.submit_order = fake_submit

    # WEAK1 already has a queued SELL for $4,000 -> projected cash -1000,
    # so only WEAK2 ($3,000) is needed to reach the $2,000 target. Without
    # the queued-sell credit, the loop would ALSO sell STRONG.
    trader.get_tickers_with_open_orders = lambda: {"WEAK1"}
    trader.pending_order_notional = lambda: (0.0, 4000.0)

    results = trader.enforce_deleveraging(account)
    submitted = [r for r in results if r.get("status") == "submitted"]
    check("queued sells count toward target (only WEAK2 sold)", [r["ticker"] for r in submitted] == ["WEAK2"], str([r["ticker"] for r in submitted]))


# --- 6. gross exposure -------------------------------------------------------
def test_gross_exposure():
    print("\n[6] Gross exposure")
    account = {"holdings": {"A": {"qty": 10, "current_price": 100.0}, "B": {"qty": 5, "current_price": 200.0}}}
    check("exposure sums holdings", trader.get_gross_exposure(account) == 2000.0, str(trader.get_gross_exposure(account)))


# --- 7. order ledger + second-trader detection -------------------------------
def test_order_ledger_and_reconciliation():
    print("\n[7] Order ledger + second-trader detection")
    ledger_file = trader.ORDER_LEDGER_FILE
    recon_file = trader.RECON_STATE_FILE
    for f in (ledger_file, recon_file):
        if os.path.exists(f):
            os.remove(f)

    # First run: no ledger, no recon baseline -> baseline created from the
    # account's pre-existing state (realistic fresh-deploy scenario).
    account = {"holdings": {"AAPL": {"qty": 6.0}, "MSFT": {"qty": 5.0}, "COST": {"qty": 7.0}}}
    flags, baseline_created = trader.reconcile_foreign_activity(account)
    check("first run creates baseline", baseline_created is True, str(flags))
    check("first run flags pre-existing holdings for review", len(flags) == 1 and "FIRST RUN BASELINE" in flags[0], str(flags))

    # Second run with an unchanged account -> expected == baseline -> clean.
    flags, baseline_created = trader.reconcile_foreign_activity(account)
    check("clean account -> no foreign flags", flags == [], str(flags))

    # Ledger math: filled buys minus sells feeds expected holdings.
    trader._append_order_to_ledger({"ticker": "AAPL", "action": "buy", "qty": 10.0, "order_status": "filled", "ts": "t1"})
    trader._append_order_to_ledger({"ticker": "AAPL", "action": "sell", "qty": 4.0, "order_status": "filled", "ts": "t2"})
    expected = trader.get_expected_holdings()
    check("ledger nets buys minus sells", abs(expected["AAPL"] - 6.0) < 1e-6, str(expected))

    # The bot's own filled buys DID change the account: AAPL 6 -> 12. With the
    # account updated, reconciliation is clean again.
    account["holdings"]["AAPL"] = {"qty": 12.0}
    flags, baseline_created = trader.reconcile_foreign_activity(account)
    check("bot's own fills reconcile clean", flags == [], str(flags))

    # A PENDING order must NOT create a false foreign flag (and is excluded
    # from expected holdings entirely).
    trader._append_order_to_ledger({"ticker": "MSFT", "action": "buy", "qty": 5.0, "order_status": "pending_new", "ts": "t3"})
    expected = trader.get_expected_holdings()
    check("pending orders excluded from expected", "MSFT" not in expected, str(expected))
    flags, baseline_created = trader.reconcile_foreign_activity(account)
    check("pending order does not cause a flag", flags == [], str(flags))

    # A foreign bot adds GOOGL and changes AAPL to 20 -> must be flagged.
    account["holdings"]["GOOGL"] = {"qty": 9.0}
    account["holdings"]["AAPL"] = {"qty": 20.0}
    flags, baseline_created = trader.reconcile_foreign_activity(account)
    check("foreign GOOGL position flagged", any("GOOGL" in f and "never created" in f for f in flags), str(flags))
    check("foreign AAPL qty change flagged", any("AAPL" in f and "diff" in f for f in flags), str(flags))

    for f in (ledger_file, recon_file):
        if os.path.exists(f):
            os.remove(f)


# --- 7b. ledger status refresh: own fills must not look like a second trader --
def test_ledger_status_refresh_stops_false_second_trader():
    print("\n[7b] Ledger status refresh: own fills are not 'second trader'")
    ledger_file = trader.ORDER_LEDGER_FILE
    recon_file = trader.RECON_STATE_FILE
    for f in (ledger_file, recon_file):
        if os.path.exists(f):
            os.remove(f)

    # Account starts with 10 AAPL (pre-existing baseline), and the bot then
    # QUEUES a sell of all 10 AAPL overnight. The sell fills at the open.
    # Ledger entry is written at submission time -> status 'accepted'.
    account = {"holdings": {"AAPL": {"qty": 10.0}}}
    trader.reconcile_foreign_activity(account)  # first run -> baseline created

    trader._append_order_to_ledger({
        "ticker": "AAPL", "action": "sell", "qty": 10.0,
        "order_id": "real-order-1", "order_status": "accepted",
    })

    # Real Alpaca now reports the order as FILLED (no longer open).
    fake_order = SimpleNamespace(id="real-order-1", status="filled")
    trader.trading_client = type("FakeClient", (), {
        "get_orders": lambda *a, **k: [fake_order],
    })()

    # The account is now all cash (AAPL sold at the open).
    account = {"holdings": {}}
    flags, baseline_created = trader.reconcile_foreign_activity(account)
    check("filled sell is not flagged as foreign activity", flags == [], str(flags))

    # The refresh also rewrote the ledger status so the math is durable.
    ledger = json.load(open(ledger_file))
    check("ledger entry refreshed to filled", ledger[-1]["order_status"] == "filled", str(ledger[-1]))

    # A genuinely foreign position (never in baseline, never in ledger) is
    # STILL caught after the refresh -- detection is not disabled.
    account = {"holdings": {"GOOGL": {"qty": 9.0}}}
    flags, baseline_created = trader.reconcile_foreign_activity(account)
    check("real foreign position still flagged", any("GOOGL" in f and "never created" in f for f in flags), str(flags))

    for f in (ledger_file, recon_file):
        if os.path.exists(f):
            os.remove(f)


# --- 7c. bot-created reductions are drift, not foreign activity -----------
def test_bot_reduction_is_adopted_once():
    print("\\n[7c] Bot-created position reduction is adopted without foreign spam")
    ledger_file = trader.ORDER_LEDGER_FILE
    recon_file = trader.RECON_STATE_FILE
    old_client = trader.trading_client
    old_open_orders = trader.get_tickers_with_open_orders
    try:
        trader._save_json_file(recon_file, {"account_id": "paper-1", "baseline": {}})
        trader._save_json_file(ledger_file, [{
            "ticker": "LLY", "action": "buy", "qty": 1.241,
            "order_id": "lly-buy", "order_status": "filled",
        }])
        trader.trading_client = type("FakeClient", (), {
            "get_orders": lambda *a, **k: [],
        })()
        trader.get_tickers_with_open_orders = lambda: set()
        account = {"account_id": "paper-1", "holdings": {}}
        flags, baseline_created = trader.reconcile_foreign_activity(account)
        check("reduction is reported as account drift", any("ACCOUNT DRIFT" in f for f in flags), str(flags))
        check("reduction is not called foreign activity", not any("FOREIGN ACTIVITY" in f for f in flags), str(flags))
        flags, baseline_created = trader.reconcile_foreign_activity(account)
        check("same reduction is quiet on next run", flags == [], str(flags))
    finally:
        trader.trading_client = old_client
        trader.get_tickers_with_open_orders = old_open_orders


# --- 7d. account switch (new keys / new paper account) resets stale state ----
def test_account_change_resets_stale_state():
    print("\n[7c] Account switch: stale baseline/risk state reset, no false 'second trader'")
    ledger_file = trader.ORDER_LEDGER_FILE
    recon_file = trader.RECON_STATE_FILE
    risk_file = trader.RISK_STATE_FILE
    open_trades_file = trader.OPEN_TRADES_FILE
    for f in (ledger_file, recon_file, risk_file, open_trades_file):
        if os.path.exists(f):
            os.remove(f)

    # State written by the OLD account before the switch: a baseline with the
    # old holdings and NO recorded account_id (old code), an order ledger full
    # of old-account orders (status 'accepted' -- the new account's key cannot
    # see those order ids so the refresh leaves them in-flight), and a risk
    # state with the old peak / day anchor.
    trader._save_json_file(recon_file, {"baseline": {"AAPL": 10.0, "MSFT": 5.0}})
    trader._append_order_to_ledger({"ticker": "AAPL", "action": "sell", "qty": 10.0, "order_id": "old-order-1", "order_status": "accepted"})
    trader._save_json_file(risk_file, {"peak_equity": 88461.60, "day": "2026-08-11", "day_start_equity": 88424.18, "halted": False, "halt_reason": "", "halt_date": None, "deleveraged": True, "deleverage_date": "2026-08-11"})
    trader._save_json_file(open_trades_file, {"AAPL": {"qty": 10.0, "entry": 180.0, "opened_at": "2026-08-11", "setup": "old", "stop": 170.0}})

    # New account: new id, $100k cash, zero positions. Old order ids are not
    # found by the new account's client, so the refresh leaves them 'accepted'.
    trader.trading_client = type("FakeClient", (), {
        "get_orders": lambda *a, **k: [],
    })()

    new_account = {"account_id": "new-paper-account-123", "total_value": 100000.0, "cash": 100000.0, "holdings": {}}

    flags, baseline_created = trader.reconcile_foreign_activity(new_account)
    check("account switch produces no foreign flags", flags == [], str(flags))
    check("account switch re-baselines the new account", baseline_created is True, str(baseline_created))
    recon = json.load(open(recon_file))
    check("new baseline recorded under new account id", recon.get("account_id") == "new-paper-account-123", str(recon))
    check("new baseline is empty (new account has no positions)", recon.get("baseline") == {}, str(recon.get("baseline")))
    ledger = json.load(open(ledger_file))
    check("old-account ledger cleared", ledger == [], str(ledger))
    open_trades = json.load(open(open_trades_file))
    check("old-account open trades cleared", open_trades == {}, str(open_trades))

    # Risk state must also reset for the new account (the stale day-anchor
    # would otherwise print a bogus 'today +12%' on a fresh account).
    trader.evaluate_circuit_breakers(new_account)
    state = trader.load_risk_state()
    check("risk peak reset to new equity", abs(state["peak_equity"] - 100000.0) < 1e-6, str(state.get("peak_equity")))
    check("risk day anchor reset", abs(state["day_start_equity"] - 100000.0) < 1e-6, str(state.get("day_start_equity")))
    check("old deleverage flag cleared", state.get("deleveraged") is False, str(state.get("deleveraged")))
    check("risk state recorded under new account id", state.get("account_id") == "new-paper-account-123", str(state.get("account_id")))

    # Same account on the next run -> no re-reset, and the empty baseline is
    # NOT recreated over the bot's own future fills.
    flags, baseline_created = trader.reconcile_foreign_activity(new_account)
    check("no false re-reset on same account", flags == [] and baseline_created is False, str(flags))
    check("empty baseline survives without re-baselining", recon.get("baseline") == {}, str(recon))

    trader._ACCOUNT_CHANGED_THIS_RUN = False
    for f in (ledger_file, recon_file, risk_file, open_trades_file):
        if os.path.exists(f):
            os.remove(f)


# --- 7d. self-heal: a lost ledger entry (failed git commit) is not 'foreign' --
def test_ledger_self_heal_recovers_lost_entries():
    print("\n[7d] Self-heal: lost ledger entries recovered from order history")
    ledger_file = trader.ORDER_LEDGER_FILE
    recon_file = trader.RECON_STATE_FILE
    for f in (ledger_file, recon_file):
        if os.path.exists(f):
            os.remove(f)

    # Baseline account with one pre-existing position.
    account = {"holdings": {"AAPL": {"qty": 6.0}}}
    trader.reconcile_foreign_activity(account)  # first run -> baseline

    # The bot bought CVX (order filled, in Alpaca's history) but the ledger
    # entry was lost before the per-run git commit reached GitHub.
    # CRITICAL: alpaca-py returns ENUM objects (OrderStatus.FILLED), and
    # str() of those is 'OrderStatus.FILLED' -- not 'filled'. The heal MUST
    # normalize that or it finds the order and still rejects it (the real
    # 2026-08-13 production bug: 'orderstatus.filled' != 'filled').
    from alpaca.trading.enums import OrderStatus, OrderSide
    fake_orders = [
        SimpleNamespace(id="cvx-order-1", symbol="CVX", side=OrderSide.BUY, status=OrderStatus.FILLED,
                        filled_qty=24.8836, qty=24.8836, submitted_at="2026-08-13T21:55:55"),
    ]
    trader.trading_client = type("FakeClient", (), {
        "get_orders": lambda *a, **k: fake_orders,
    })()

    account["holdings"]["CVX"] = {"qty": 24.8836}
    flags, baseline_created = trader.reconcile_foreign_activity(account)
    check("recovered own fill is not flagged as foreign", flags == [], str(flags))

    ledger = json.load(open(ledger_file))
    check("healed entry added to ledger",
          any(e.get("order_id") == "cvx-order-1" and e.get("order_status") == "filled" for e in ledger),
          str(ledger))

    # A genuinely foreign position (never in baseline, never in ledger, no
    # matching order in history) is STILL caught after healing.
    account["holdings"]["GOOGL"] = {"qty": 9.0}
    flags, baseline_created = trader.reconcile_foreign_activity(account)
    check("real foreign position still flagged after heal",
          any("GOOGL" in f and "never created" in f for f in flags), str(flags))

    # Also verify the ledger stores the PLAIN status value, not the enum repr
    # ('filled', not 'OrderStatus.FILLED') -- old entries are still read fine
    # by _status_val, but new writes must be clean.
    trader._append_order_to_ledger({
        "ticker": "MSFT", "action": "buy", "qty": 5.0,
        "order_id": "msft-1", "order_status": OrderStatus.FILLED,
    })
    ledger = json.load(open(ledger_file))
    msft = [e for e in ledger if e.get("ticker") == "MSFT"][-1]
    check("ledger stores plain status value", msft["order_status"] == "filled", str(msft))
    check("_status_val handles enum repr string", trader._status_val("OrderStatus.PENDING_NEW") == "pending_new", trader._status_val("OrderStatus.PENDING_NEW"))

    for f in (ledger_file, recon_file):
        if os.path.exists(f):
            os.remove(f)


# --- 7e. technical fallback must not dump down holdings ----------------------
def test_technical_fallback_holds_losers():
    print("\n[7e] Technical fallback never sells a position that is DOWN overall")
    import decide
    old_win = trader.is_within_trade_window
    trader.is_within_trade_window = lambda: True
    try:
        scored_holdings = {
            "DOWN1": {"indicators": {"price": 95.0}, "score": 30.0},   # down, weak -> HELD
            "UP1": {"indicators": {"price": 105.0}, "score": 40.0},    # up, weak -> sellable
            "STRONG": {"indicators": {"price": 110.0}, "score": 85.0},  # strong -> buy more
        }
        account = {"cash": 50000.0, "total_value": 100000.0, "holdings": {
            "DOWN1": {"qty": 10, "avg_entry_price": 100.0, "current_price": 95.0},
            "UP1": {"qty": 10, "avg_entry_price": 100.0, "current_price": 105.0},
            "STRONG": {"qty": 5, "avg_entry_price": 100.0, "current_price": 110.0},
        }}
        old_unavail = decide.get_tickers_with_open_orders
        decide.get_tickers_with_open_orders = lambda: set()
        old_cd = decide.get_tickers_on_cooldown
        decide.get_tickers_on_cooldown = lambda: set()
        try:
            trades, meta = decide.get_technical_trade_decisions(scored_holdings, {}, account)
            sells = {t["ticker"] for t in trades if t["action"] == "sell"}
            buys = {t["ticker"] for t in trades if t["action"] == "buy"}
            check("fallback holds the DOWN loser (DOWN1)", "DOWN1" not in sells, str(sells))
            check("fallback still exits the UP weak name (UP1)", "UP1" in sells, str(sells))
            check("fallback still adds to STRONG", "STRONG" in buys, str(buys))
            check("fallback flagged", meta.get("technical_fallback") is True, str(meta))
        finally:
            decide.get_tickers_with_open_orders = old_unavail
            decide.get_tickers_on_cooldown = old_cd
    finally:
        trader.is_within_trade_window = old_win


# --- 8. daytrading window helpers --------------------------------------------
def test_daytrade_window_flags():
    print("\n[8] Daytrading helpers (config flags, off-switch)")
    # These are time-dependent, so test the deterministic parts: the mode
    # switches and that the helpers do not crash and return booleans.
    check("should_end_of_day_flatten returns bool", isinstance(trader.should_end_of_day_flatten(), bool))
    check("is_within_trade_window returns bool", isinstance(trader.is_within_trade_window(), bool))

    old_mode = config.DAYTRADE_MODE
    config.DAYTRADE_MODE = False
    trader.DAYTRADE_MODE = False
    check("window is open when daytrade mode off", trader.is_within_trade_window() is True)
    check("no EOD flatten when mode off", trader.should_end_of_day_flatten() is False)
    config.DAYTRADE_MODE = old_mode
    trader.DAYTRADE_MODE = old_mode

    # The buy gate must actually fire: a NEW buy outside the entry window is
    # skipped with the window reason (dead code before this fix -- the gate
    # was never called, so the documented TRADE_START_MINUTES_AFTER_OPEN /
    # STOP_NEW_BUYS_AFTER knobs silently did nothing).
    old_win = trader.is_within_trade_window
    trader.is_within_trade_window = lambda: False
    try:
        account = {"cash": 50000.0, "total_value": 100000.0, "holdings": {}}
        trader.get_price = lambda s: 100.0
        trader.trading_client = type("FakeClient", (), {
            "get_orders": lambda *a, **k: [],
            "submit_order": lambda self, req: type("O", (), {"id": "x", "status": "accepted"})(),
        })()
        r = trader.execute_trade({"ticker": "AAA", "action": "buy", "dollar_amount": 5000.0, "conviction": 8}, account)
        check("buy skipped outside entry window", r.get("status") == "skipped" and "entry window" in str(r.get("reason")), str(r))
        # Sells are never restricted by the window.
        account["holdings"] = {"AAA": {"qty": 10, "avg_entry_price": 100.0, "current_price": 100.0}}
        r = trader.execute_trade({"ticker": "AAA", "action": "sell", "dollar_amount": 0, "conviction": 10}, account)
        check("sell not blocked by entry window", r.get("status") == "submitted", str(r))
    finally:
        trader.is_within_trade_window = old_win


# --- 9. chase filters ---------------------------------------------------------
def test_chase_filters():
    print("\n[9] Chase filters scale extended buys (soft), hard-skip extremes")
    account = {"cash": 50000.0, "total_value": 100000.0, "holdings": {}}
    trader.trading_client = type("FakeClient", (), {
        "get_orders": lambda *a, **k: [],
        "submit_order": lambda self, req: type("O", (), {"id": "x", "status": "accepted"})(),
    })()
    trader.get_price = lambda s: 110.0
    trader.get_price_history = lambda *a, **k: None  # silence custom-exit noise
    trader.get_time_of_day_multiplier = lambda *a, **k: 1.0

    old_vwap = trader.MAX_BUY_EXTENSION_ABOVE_VWAP_PCT
    old_move = trader.MAX_INTRADAY_MOVE_PCT
    old_hard = trader.CHASE_HARD_SKIP_MULT
    trader.MAX_BUY_EXTENSION_ABOVE_VWAP_PCT = 2.0
    trader.MAX_INTRADAY_MOVE_PCT = 4.0
    trader.CHASE_HARD_SKIP_MULT = 5.0

    # 22% above VWAP = 11x the 2% limit -> beyond the 5x hard skip -> refused.
    trader.get_full_indicators = lambda s: {"vwap": 90.0, "intraday_momentum_pct": 1.0}
    result = trader.execute_trade({"ticker": "NVDA", "action": "buy", "dollar_amount": 10000, "conviction": 8}, account)
    check("extreme VWAP extension hard-skipped", result["status"] == "skipped" and "VWAP" in result["reason"], json.dumps(result))

    # Already up 17% on the session (4.25x the 4% limit) -> TRADED, scaled down.
    trader.get_full_indicators = lambda s: {"vwap": 108.0, "intraday_momentum_pct": 17.0}
    result = trader.execute_trade({"ticker": "NVDA", "action": "buy", "dollar_amount": 10000, "conviction": 8}, account)
    check("momentum name traded, not skipped", result["status"] == "submitted", json.dumps(result))

    # Near VWAP with a modest move -> allowed.
    trader.get_full_indicators = lambda s: {"vwap": 109.0, "intraday_momentum_pct": 1.5}
    result = trader.execute_trade({"ticker": "NVDA", "action": "buy", "dollar_amount": 10000, "conviction": 8}, account)
    check("buy near VWAP allowed", result["status"] == "submitted", json.dumps(result))

    # Filters disabled (<= 0) -> extended buy allowed.
    trader.MAX_BUY_EXTENSION_ABOVE_VWAP_PCT = 0.0
    trader.MAX_INTRADAY_MOVE_PCT = 0.0
    trader.get_full_indicators = lambda s: {"vwap": 100.0, "intraday_momentum_pct": 9.0}
    result = trader.execute_trade({"ticker": "NVDA", "action": "buy", "dollar_amount": 10000, "conviction": 8}, account)
    check("filters disabled -> buy allowed", result["status"] == "submitted", json.dumps(result))

    trader.MAX_BUY_EXTENSION_ABOVE_VWAP_PCT = old_vwap
    trader.MAX_INTRADAY_MOVE_PCT = old_move
    trader.CHASE_HARD_SKIP_MULT = old_hard


# --- 10. trailing stop --------------------------------------------------------
def test_trailing_stop():
    print("\n[10] Trailing stop ratchets up and persists")
    exits_file = trader.CUSTOM_EXITS_FILE
    if os.path.exists(exits_file):
        os.remove(exits_file)

    old_activate = trader.TRAILING_STOP_ACTIVATE_MULT
    old_dist = trader.TRAILING_STOP_DISTANCE_MULT
    old_scale = trader.ENABLE_SCALE_OUT
    trader.TRAILING_STOP_ACTIVATE_MULT = 1.5
    trader.TRAILING_STOP_DISTANCE_MULT = 2.0
    # Isolate the trailing-stop behavior: this scenario (105 vs target 108)
    # sits past the scale-out trigger, so disable partial profit-taking here.
    trader.ENABLE_SCALE_OUT = False

    trader.trading_client = type("FakeClient", (), {
        "get_orders": lambda *a, **k: [],
        "submit_order": lambda self, req: type("O", (), {"id": "x", "status": "accepted"})(),
    })()
    # atr = 2.0, entry 100 -> trailing activates at 103, trail distance 4.0.
    trader.get_full_indicators = lambda s: {"atr_14": 2.0}
    trader.get_price_history = lambda *a, **k: None

    # Position up 5 (105) -> trail stop = max(entry stop 95, 105 - 4) = 101.
    account = {"cash": 50000.0, "total_value": 100000.0,
               "holdings": {"NVDA": {"qty": 10, "avg_entry_price": 100.0, "current_price": 105.0}}}
    results = trader.check_atr_stop_take_profit(account)
    check("no sell yet (105 > trailed stop 101)", results == [], str(results))
    exits = trader._load_custom_exits()
    check("trail stop persisted at 101", "NVDA" in exits and abs(exits["NVDA"]["stop_loss"] - 101.0) < 0.01, json.dumps(exits.get("NVDA")))

    # Price drops to 100.5 -> below the trailed stop 101 -> sell.
    account["holdings"]["NVDA"]["current_price"] = 100.5
    trader.get_price = lambda s: 100.5
    results = trader.check_atr_stop_take_profit(account)
    check("trailing stop triggers sell", len(results) == 1 and results[0]["status"] == "submitted", str(results))

    trader.TRAILING_STOP_ACTIVATE_MULT = old_activate
    trader.TRAILING_STOP_DISTANCE_MULT = old_dist
    trader.ENABLE_SCALE_OUT = old_scale
    if os.path.exists(exits_file):
        os.remove(exits_file)


# --- 10b. scale-out (partial profit taking) -----------------------------------
def test_scale_out():
    print("\n[10b] Scale-out banks a slice of the winner, once")
    exits_file = trader.CUSTOM_EXITS_FILE
    if os.path.exists(exits_file):
        os.remove(exits_file)

    old_enable = trader.ENABLE_SCALE_OUT
    trader.ENABLE_SCALE_OUT = True

    calls = {"sells": []}
    trader.trading_client = type("FakeClient", (), {
        "get_orders": lambda *a, **k: [],
        "submit_order": lambda self, req: calls["sells"].append(req) or type("O", (), {"id": "x", "status": "accepted"})(),
    })()
    trader.get_full_indicators = lambda s: {"atr_14": 2.0}
    trader.get_price_history = lambda *a, **k: None
    trader.get_price = lambda s: 112.0

    # Pre-set exit levels: entry 100, target 120 -> scale-out trigger at 112
    # (60% of the way there). Position is 10 sh -> banks 3.33 sh.
    trader._save_custom_exits({
        "NVDA": {"stop_loss": 96.0, "take_profit": 120.0, "entry_price": 100.0, "set_at": "x"},
    })
    account = {"cash": 50000.0, "total_value": 100000.0,
               "holdings": {"NVDA": {"qty": 10, "avg_entry_price": 100.0, "current_price": 112.0}}}

    results = trader.check_atr_stop_take_profit(account)
    check("scale-out fires at 60% of the way to target",
          len(results) == 1 and results[0].get("trigger") == "scale_out" and results[0]["status"] == "submitted",
          str(results))
    check("sells exactly 33% of the position",
          len(calls["sells"]) == 1 and abs(float(calls["sells"][0].qty) - 3.3) < 0.01,
          str(calls["sells"]))
    exits = trader._load_custom_exits()
    check("one-shot flag persisted", exits.get("NVDA", {}).get("scaled_out_1") is True, json.dumps(exits.get("NVDA")))

    # Same price again -> the flag blocks a second scale-out (no double sell).
    calls["sells"] = []
    results = trader.check_atr_stop_take_profit(account)
    check("no double scale-out on the next run", results == [], str(results))

    trader.ENABLE_SCALE_OUT = old_enable


# --- 10c. uniform portfolio quality review -------------------------------
def test_quality_trim():
    print("\n[10c] Uniform portfolio quality review")
    import tempfile
    recon_tmp = tempfile.mkdtemp()
    old_recon = trader.RECON_STATE_FILE
    old_trim = trader.ENABLE_QUALITY_TRIM
    trader.ENABLE_QUALITY_TRIM = True
    trader.RECON_STATE_FILE = os.path.join(recon_tmp, "recon.json")
    with open(trader.RECON_STATE_FILE, "w") as f:
        json.dump({"account_id": "a", "baseline": {"AAA": 10, "BBB": 20, "CCC": 5, "DDD": 7, "GGG": 3}}, f)

    scores = {"AAA": 30.0, "BBB": 80.0, "CCC": 40.0, "DDD": 25.0, "GGG": 20.0}
    trader.get_full_indicators = lambda s: {"_ticker": s, "rsi_14": 50.0}
    trader.calculate_signal_score = lambda ind: scores.get(ind.get("_ticker", ""), 60.0)
    trader.get_price = lambda s: {"AAA": 98.0, "BBB": 52.0, "CCC": 195.0, "DDD": 11.0, "EEE": 9.0, "GGG": 50.0}[s]
    trader.trading_client = type("FakeClient", (), {
        "get_orders": lambda *a, **k: [],
        "submit_order": lambda self, req: type("O", (), {"id": "x", "status": "accepted"})(),
    })()

    # Legacy candidates and their prices: GGG(20, flat at entry), DDD(25, UP
    # 10% -> profit-take), AAA(30, DOWN 2% -> held long despite bad score),
    # CCC(40, DOWN 2.5% -> held), BBB(80, up 4% -> winner, never touched).
    # EEE is not legacy (never considered).
    account = {"cash": 50000.0, "total_value": 100000.0, "holdings": {
        "AAA": {"qty": 10, "avg_entry_price": 100.0, "current_price": 98.0},
        "BBB": {"qty": 20, "avg_entry_price": 50.0, "current_price": 52.0},
        "CCC": {"qty": 5, "avg_entry_price": 200.0, "current_price": 195.0},
        "DDD": {"qty": 7, "avg_entry_price": 10.0, "current_price": 11.0},
        "EEE": {"qty": 1, "avg_entry_price": 10.0, "current_price": 9.0},
        "GGG": {"qty": 3, "avg_entry_price": 50.0, "current_price": 50.0},
    }}
    results = trader.enforce_quality_trim(account)
    sold = {r.get("ticker") for r in results if r.get("status") == "submitted"}
    check("profit-take banks DDD (+10%) and score-trim sells flat GGG (score 20)", sold == {"GGG", "DDD"}, str(sold))
    check("all trims tagged quality_trim", all(r.get("trigger") == "quality_trim" for r in results), str(results))
    check("holds legacy loser AAA (down 2%, score 30) long", "AAA" not in sold, str(sold))
    check("holds legacy loser CCC (down 2.5%, score 40) long", "CCC" not in sold, str(sold))
    check("never sells winners (BBB)", "BBB" not in sold, str(sold))
    check("non-baseline loser is also protected from churn", "EEE" not in sold, str(sold))

    # A non-baseline position with the same weak score and no loss is eligible
    # under exactly the same rule as a baseline position.
    scores["EEE"] = 20.0
    account["holdings"]["EEE"]["current_price"] = 10.0
    trader.get_price = lambda s: {"AAA": 98.0, "BBB": 52.0, "CCC": 195.0, "DDD": 11.0, "EEE": 10.0, "GGG": 50.0}[s]
    results = trader.enforce_quality_trim(account)
    sold = {r.get("ticker") for r in results if r.get("status") == "submitted"}
    check("non-baseline position receives the same quality rule", "EEE" in sold, str(sold))

    # Same run with GGG in the hole (47 vs entry 50 = -6%): the guard skips it
    # even though it is the WORST-scoring candidate. DDD (entry 10 -> 11) is up
    # 10%, so the profit-take pass banks it first; AAA (score 30, not in the
    # hole) fills the second slot. GGG is still never sold.
    account["holdings"]["GGG"]["current_price"] = 47.0
    trader.get_price = lambda s: {"AAA": 98.0, "BBB": 52.0, "CCC": 195.0, "DDD": 11.0, "EEE": 9.0, "GGG": 47.0}[s]
    results = trader.enforce_quality_trim(account)
    sold = {r.get("ticker") for r in results if r.get("status") == "submitted"}
    check("never sells into the hole even when worst", "GGG" not in sold, str(sold))
    check("profit-take banks the legacy winner (DDD +10%)", "DDD" in sold, str(sold))

    # New: profit-take pass alone -- a legacy winner up >= 5% sells even when
    # its signal score is excellent (a winner is a winner, score ignored).
    account["holdings"]["BBB"]["current_price"] = 55.0  # entry 50 -> +10%
    trader.get_price = lambda s: {"AAA": 98.0, "BBB": 55.0, "CCC": 195.0, "DDD": 11.0, "EEE": 9.0, "GGG": 47.0}[s]
    results = trader.enforce_quality_trim(account)
    sold = {r.get("ticker") for r in results if r.get("status") == "submitted"}
    check("profit-take ignores score (BBB score 80 still sold)", "BBB" in sold, str(sold))

    trader.RECON_STATE_FILE = old_recon
    trader.ENABLE_QUALITY_TRIM = old_trim


# --- 10d. walk-forward gate into live sizing ----------------------------------
def test_walkforward_live_learning():
    print("\n[10d] Walk-forward gate sizes proven setups up, drags down")
    import tempfile
    tmp = tempfile.mkdtemp()
    old_file = trader.SETUP_GATE_FILE
    old_enable = trader.WALKFORWARD_LIVE_LEARNING
    trader.WALKFORWARD_LIVE_LEARNING = True
    trader.SETUP_GATE_FILE = os.path.join(tmp, "setup_gate.json")
    with open(trader.SETUP_GATE_FILE, "w") as f:
        json.dump({
            "gate": ["uptrend|neutral|bullish|pos|lo"],
            "stats": {
                "uptrend|neutral|bullish|pos|lo": {"n": 12, "wins": 9, "win_rate": 0.75, "avg_pnl_pct": 1.3},
                "downtrend|neutral|bearish|neg|lo": {"n": 10, "wins": 2, "win_rate": 0.20, "avg_pnl_pct": -1.1},
            },
        }, f)
    proven = trader.get_walkforward_multiplier({"trend": "uptrend", "rsi_14": 55.0, "macd_cross": "bullish", "momentum_10d": 1.5, "volatility_20d": 1.2})
    drag = trader.get_walkforward_multiplier({"trend": "downtrend", "rsi_14": 50.0, "macd_cross": "bearish", "momentum_10d": -1.5, "volatility_20d": 1.2})
    unknown = trader.get_walkforward_multiplier({"trend": "sideways", "rsi_14": 50.0, "macd_cross": "none", "momentum_10d": 0.0, "volatility_20d": 1.2})
    check("proven setup sized up", proven > 1.0, str(proven))
    check("proven drag sized down", drag < 1.0, str(drag))
    check("unknown setup neutral", unknown == 1.0, str(unknown))
    trader.SETUP_GATE_FILE = os.path.join(tmp, "missing.json")
    check("no gate file -> neutral", trader.get_walkforward_multiplier({"trend": "uptrend", "rsi_14": 55.0}) == 1.0, "should be 1.0")
    trader.SETUP_GATE_FILE = old_file
    trader.WALKFORWARD_LIVE_LEARNING = old_enable


# --- 10e. momentum pre-filter on the universe slice ----------------------------
def test_universe_slice_momentum_prefilter():
    print("\n[10e] Momentum pre-filter prioritizes today's movers")
    import decide
    eligible = ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "GOOGL", "META", "JPM", "XOM", "WMT"]
    sliced = decide._pick_universe_slice(eligible, ["NVDA", "TSLA"], 5)
    check("movers scanned first", sliced[:2] == ["NVDA", "TSLA"] and len(sliced) == 5, str(sliced))
    check("quota not binding -> full list", len(decide._pick_universe_slice(eligible[:3], ["NVDA"], 5)) == 3, "full list")


# --- 11. risk-based sizing ----------------------------------------------------
def test_risk_based_sizing():
    print("\n[11] Risk-based sizing caps by stop distance")
    account = {"cash": 50000.0, "total_value": 100000.0, "holdings": {}}
    trader.trading_client = type("FakeClient", (), {
        "get_orders": lambda *a, **k: [],
        "submit_order": lambda self, req: type("O", (), {"id": "x", "status": "accepted"})(),
    })()
    trader.get_price = lambda s: 100.0
    trader.get_price_history = lambda *a, **k: None
    trader.get_time_of_day_multiplier = lambda *a, **k: 1.0

    old_risk = trader.MAX_RISK_PER_TRADE_PCT
    old_tod = trader.DAYTRADE_MODE
    trader.MAX_RISK_PER_TRADE_PCT = 0.75
    trader.DAYTRADE_MODE = False
    config.DAYTRADE_MODE = False

    # atr 4 -> stop 90 (10% away) -> risk cap = 750 / 0.10 = 7500 -> qty 75.
    trader.get_full_indicators = lambda s: {"atr_14": 4.0, "vwap": 99.0, "intraday_momentum_pct": 0.5}
    result = trader.execute_trade({"ticker": "NVDA", "action": "buy", "dollar_amount": 15000, "conviction": 10}, account)
    check("risk cap sizes to 75 shares", result["status"] == "submitted" and abs(result["qty"] - 75.0) < 0.01, json.dumps(result))

    # Tighter stop (atr 1 -> stop 97.5, 2.5% away) -> cap 30k, doesn't bind.
    trader.get_full_indicators = lambda s: {"atr_14": 1.0, "vwap": 99.0, "intraday_momentum_pct": 0.5}
    result = trader.execute_trade({"ticker": "NVDA", "action": "buy", "dollar_amount": 15000, "conviction": 10}, account)
    check("tight stop -> full requested size", result["status"] == "submitted" and abs(result["qty"] - 150.0) < 0.01, json.dumps(result))

    trader.MAX_RISK_PER_TRADE_PCT = old_risk
    trader.DAYTRADE_MODE = old_tod
    config.DAYTRADE_MODE = old_tod


# --- 12. time-of-day multipliers ----------------------------------------------
def test_time_of_day_multiplier():
    print("\n[12] Time-of-day sizing windows")
    old_tod = trader.DAYTRADE_MODE
    trader.DAYTRADE_MODE = True
    import pytz as _pytz
    et = _pytz.timezone("America/New_York")
    noon = et.localize(__import__("datetime").datetime(2026, 8, 11, 12, 0))
    ten = et.localize(__import__("datetime").datetime(2026, 8, 11, 10, 0))
    two = et.localize(__import__("datetime").datetime(2026, 8, 11, 14, 0))
    three_forty = et.localize(__import__("datetime").datetime(2026, 8, 11, 15, 40))
    check("lunch lull (12:00) -> 0.5", _REAL_GET_TOD(noon) == 0.5, str(_REAL_GET_TOD(noon)))
    check("power hour (10:00) -> 1.0", _REAL_GET_TOD(ten) == 1.0, str(_REAL_GET_TOD(ten)))
    check("mid-afternoon (14:00) -> 0.8", _REAL_GET_TOD(two) == 0.8, str(_REAL_GET_TOD(two)))
    check("closing push (15:40) -> 1.0", _REAL_GET_TOD(three_forty) == 1.0, str(_REAL_GET_TOD(three_forty)))
    trader.DAYTRADE_MODE = False
    check("off when daytrade mode disabled", _REAL_GET_TOD(noon) == 1.0)
    trader.DAYTRADE_MODE = old_tod


# --- 13. news confluence -------------------------------------------------------
def test_news_confluence():
    print("\n[13] News+technical confluence filter")
    import decide
    old = decide.NEWS_CONFLUENCE_MIN_TECH_SCORE
    decide.NEWS_CONFLUENCE_MIN_TECH_SCORE = 50.0
    strong = {"trend": "uptrend", "rsi_14": 50.0, "adx_14": 26.0, "macd": {"histogram": 1.0}, "relative_volume_pct": 25.0, "opening_range_status": "above"}
    weak = {"trend": "downtrend", "rsi_14": 45.0, "adx_14": 15.0, "macd": {"histogram": -1.0}, "relative_volume_pct": -40.0, "opening_range_status": "below"}
    candidates = {"NVDA": [{"headline": "x"}], "TSLA": [{"headline": "y"}]}
    scored = {"NVDA": {"indicators": strong, "score": 80.0}, "TSLA": {"indicators": weak, "score": 55.0}}
    sentiment = {"NVDA": 0.8, "TSLA": -0.6}
    c2, s2, sen2 = decide._apply_news_confluence(candidates, scored, sentiment)
    check("strong-tech news candidate kept, weak dropped", "NVDA" in c2 and "TSLA" not in c2, str(list(c2.keys())))
    decide.NEWS_CONFLUENCE_MIN_TECH_SCORE = 0.0
    c3, s3, sen3 = decide._apply_news_confluence(candidates, scored, sentiment)
    check("confluence disabled keeps all", set(c3.keys()) == {"NVDA", "TSLA"}, str(list(c3.keys())))
    decide.NEWS_CONFLUENCE_MIN_TECH_SCORE = old


# --- 14. trade journal ---------------------------------------------------------
def test_trade_journal():
    print("\n[14] Trade journal pairs buys and sells")
    for f in (trader.TRADES_JOURNAL_FILE, trader.TRADE_RESULTS_FILE, trader.OPEN_TRADES_FILE):
        if os.path.exists(f):
            os.remove(f)
    import csv as _csv
    trader._track_open_close("NVDA", "buy", 10, 100.0, 95.0, 110.0, "decision", "news-driven")
    trader._track_open_close("NVDA", "sell", 10, 105.0, None, None, "stop_loss", "")
    rows = []
    with open(trader.TRADE_RESULTS_FILE) as f:
        rows = list(_csv.DictReader(f))
    check("closed trade recorded", len(rows) == 1 and rows[0]["ticker"] == "NVDA", str(rows))
    check("pnl_pct includes estimated transaction costs", len(rows) == 1 and 4.8 < float(rows[0]["pnl_pct"]) < 5.0 and float(rows[0]["transaction_costs_dollars"]) > 0, str(rows))
    check("exit_reason = stop_loss", len(rows) == 1 and rows[0]["exit_reason"] == "stop_loss", str(rows))
    check("setup preserved from buy", len(rows) == 1 and "news" in rows[0]["setup"], str(rows))
    # Partial sell keeps the position open (no new closed row).
    trader._track_open_close("AAPL", "buy", 20, 50.0, 48.0, 55.0, "decision", "opening-range breakout")
    trader._track_open_close("AAPL", "sell", 8, 52.0, None, None, "take_profit", "")
    with open(trader.TRADE_RESULTS_FILE) as f:
        line_count = sum(1 for _ in f)
    check("partial sell does not close", line_count == 2, f"{line_count} lines (header + 1 closed row) expected")
    summary = trader.summarize_trade_results()
    check("summary includes news setup", "news" in summary, summary)
    for f in (trader.TRADES_JOURNAL_FILE, trader.TRADE_RESULTS_FILE, trader.OPEN_TRADES_FILE):
        if os.path.exists(f):
            os.remove(f)


# --- 14b. engine-level P&L report ---------------------------------------------
def test_engine_performance_report():
    print("\n[14b] Engine-level realized and unrealized P&L report")
    import csv as _csv
    import tempfile

    tmp = tempfile.mkdtemp()
    old_results = trader.TRADE_RESULTS_FILE
    old_open = trader.OPEN_TRADES_FILE
    old_engine_file = trader.ENGINE_PERFORMANCE_FILE
    old_book_file = trader.BOOK_PERFORMANCE_FILE
    trader.BOOK_PERFORMANCE_FILE = os.path.join(tmp, "book_performance.csv")
    trader.TRADE_RESULTS_FILE = os.path.join(tmp, "trade_results.csv")
    trader.OPEN_TRADES_FILE = os.path.join(tmp, "open_trades.json")
    trader.ENGINE_PERFORMANCE_FILE = os.path.join(tmp, "engine_performance.csv")
    try:
        trader._append_csv(trader.TRADE_RESULTS_FILE, trader.RESULTS_HEADER, {
            "closed_at": "t", "opened_at": "t", "ticker": "GEM",
            "entry_price": 100, "exit_price": 106, "qty": 10,
            "pnl_pct": 6.0, "pnl_dollars": 60.0, "setup": "news catalyst",
            "exit_reason": "take_profit", "engine": "gemini",
        })
        trader._append_csv(trader.TRADE_RESULTS_FILE, trader.RESULTS_HEADER, {
            "closed_at": "t", "opened_at": "t", "ticker": "TECH",
            "entry_price": 100, "exit_price": 97, "qty": 10,
            "pnl_pct": -3.0, "pnl_dollars": -30.0, "setup": "technical score",
            "exit_reason": "stop_loss", "engine": "technical_fallback",
        })
        trader._save_json_file(trader.OPEN_TRADES_FILE, {
            "GEM": {"qty": 10, "entry": 100, "setup": "news catalyst", "engine": "gemini"},
            "TECH": {"qty": 10, "entry": 100, "setup": "technical score", "engine": "technical_fallback"},
        })
        account = {"holdings": {
            "GEM": {"qty": 10, "current_price": 105, "unrealized_pl": 50},
            "TECH": {"qty": 10, "current_price": 98, "unrealized_pl": -20},
            "OLD": {"qty": 4, "current_price": 20, "unrealized_pl": -12},
        }}
        report = trader.record_engine_performance_snapshot(account, tmp)
        check("report separates Gemini", "gemini: realized $+60.00" in report and "unrealized $+50.00" in report, report)
        check("report separates fallback", "technical_fallback: realized $-30.00" in report and "unrealized $-20.00" in report, report)
        check("report labels unattributed holdings", "legacy/unknown" in report and "unrealized $-12.00" in report, report)
        check("report includes separate books", "Books:" in report and "legacy" in report, report)
        with open(trader.ENGINE_PERFORMANCE_FILE) as f:
            rows = list(_csv.DictReader(f))
        with open(trader.BOOK_PERFORMANCE_FILE) as f:
            book_rows = list(_csv.DictReader(f))
        check("dashboard CSV has one row per engine", len(rows) == 3, str(rows))
        check("book CSV has separate book rows", len(book_rows) >= 2, str(book_rows))
    finally:
        trader.TRADE_RESULTS_FILE = old_results
        trader.OPEN_TRADES_FILE = old_open
        trader.ENGINE_PERFORMANCE_FILE = old_engine_file
        trader.BOOK_PERFORMANCE_FILE = old_book_file
        for name in ("trade_results.csv", "open_trades.json", "engine_performance.csv", "book_performance.csv"):
            path = os.path.join(tmp, name)
            if os.path.exists(path):
                os.remove(path)
        os.rmdir(tmp)


# --- 15. confidence-based sizing ------------------------------------------------
def test_confidence_sizing():
    print("\n[15] Confidence-based sizing converts 0-100 to % of equity")
    account = {"cash": 50000.0, "total_value": 100000.0, "holdings": {}}
    trader.trading_client = type("FakeClient", (), {
        "get_orders": lambda *a, **k: [],
        "submit_order": lambda self, req: type("O", (), {"id": "x", "status": "accepted"})(),
    })()
    trader.get_price = lambda s: 100.0
    trader.get_price_history = lambda *a, **k: None
    trader.get_time_of_day_multiplier = lambda *a, **k: 1.0
    trader.get_full_indicators = lambda s: {"atr_14": 1.0, "vwap": 99.0, "intraday_momentum_pct": 0.5}

    # 95 -> 8% of 100k = 8k -> qty 80 @ 100. Dollar amount is ignored.
    r = trader.execute_trade({"ticker": "NVDA", "action": "buy", "dollar_amount": 50000, "conviction": 8, "confidence": 95}, account)
    check("confidence 95 -> 8% of equity (qty 80)", r["status"] == "submitted" and abs(r["qty"] - 80.0) < 0.01, json.dumps(r))

    # 60 -> 2% = 2k -> qty 20.
    r = trader.execute_trade({"ticker": "NVDA", "action": "buy", "dollar_amount": 50000, "conviction": 8, "confidence": 60}, account)
    check("confidence 60 -> 2% of equity (qty 20)", r["status"] == "submitted" and abs(r["qty"] - 20.0) < 0.01, json.dumps(r))

    # 40 -> below tradeable bar -> skip.
    r = trader.execute_trade({"ticker": "NVDA", "action": "buy", "dollar_amount": 50000, "conviction": 8, "confidence": 40}, account)
    check("confidence 40 -> skipped", r["status"] == "skipped" and "below tradeable" in r["reason"], json.dumps(r))

    # 0 / missing -> not tradeable.
    check("confidence 0 -> size 0", trader.confidence_to_size_pct(0) == 0.0)
    check("missing confidence -> size 0", trader.confidence_to_size_pct(None) == 0.0)


# --- 16. news article scoring ---------------------------------------------------
def test_news_scoring():
    print("\n[16] News importance scoring ranks catalysts above filler")
    import news
    big = news.score_article({"headline": "Apple announces AI partnership with OpenAI", "summary": ""})
    beat = news.score_article({"headline": "NVDA beats earnings, raises guidance", "summary": ""})
    interview = news.score_article({"headline": "Apple CEO Tim Cook interview at conference", "summary": ""})
    store = news.score_article({"headline": "Apple opens new store", "summary": ""})
    recall = news.score_article({"headline": "Tesla recalls cars amid investigation", "summary": ""})
    check("partnership scores high (>=6)", big >= 6.0, str(big))
    check("earnings beat scores high (>=6)", beat >= 6.0, str(beat))
    check("interview scores low (<5, filtered)", interview < 5.0, str(interview))
    check("store opening scores low (<5, filtered)", store < 5.0, str(store))
    check("recall+investigation scores 0", recall == 0.0, str(recall))
    check("catalysts rank above filler", big > interview > store, f"{big} > {interview} > {store}")
    sent = news.headline_sentiment({"headline": "NVDA beats earnings and surges", "summary": ""})
    check("sentiment positive for good news", sent > 0, str(sent))
    sent2 = news.headline_sentiment({"headline": "Company misses estimates, plunges", "summary": ""})
    check("sentiment negative for bad news", sent2 < 0, str(sent2))


# --- 17. smarter exits (MA breakdown, RSI exhaustion, negative news) ---------------
def test_smarter_exits():
    print("\n[17] Smarter exits: MA breakdown / RSI exhaustion / negative news")
    exits_file = trader.CUSTOM_EXITS_FILE
    sent_file = trader.NEWS_SENTIMENT_CACHE_FILE
    for f in (exits_file, sent_file):
        if os.path.exists(f):
            os.remove(f)

    trader.trading_client = type("FakeClient", (), {
        "get_orders": lambda *a, **k: [],
        "submit_order": lambda self, req: type("O", (), {"id": "x", "status": "accepted"})(),
    })()
    trader.get_price = lambda s: 95.0
    trader.get_price_history = lambda *a, **k: None

    old_ma, old_rsi, old_news = trader.ENABLE_MA_BREAKDOWN_EXIT, trader.ENABLE_RSI_EXHAUSTION_EXIT, trader.ENABLE_NEGATIVE_NEWS_EXIT
    trader.ENABLE_MA_BREAKDOWN_EXIT = True
    trader.ENABLE_RSI_EXHAUSTION_EXIT = True
    trader.ENABLE_NEGATIVE_NEWS_EXIT = True

    account = {"cash": 50000.0, "total_value": 100000.0,
               "holdings": {"NVDA": {"qty": 10, "avg_entry_price": 100.0, "current_price": 95.0}}}
    # MA breakdown: price 95 < SMA-20 96, above hard stop (92) -> sell on MA.
    trader.get_full_indicators = lambda s: {"atr_14": None, "sma_20": 96.0, "rsi_14": 40.0, "trend": "downtrend"}
    results = trader.check_atr_stop_take_profit(account)
    check("MA breakdown triggers exit", len(results) == 1 and results[0].get("trigger") == "ma_breakdown", str(results))

    # RSI exhaustion: RSI 80 > 75, price at entry, no MA breakdown -> sell.
    account["holdings"]["NVDA"]["current_price"] = 100.0
    trader.get_full_indicators = lambda s: {"atr_14": None, "sma_20": 99.0, "rsi_14": 80.0}
    results = trader.check_atr_stop_take_profit(account)
    check("RSI exhaustion triggers exit", len(results) == 1 and results[0].get("trigger") == "rsi_exhaustion", str(results))

    # Negative news: two corroborating articles at -0.6 -> sell (MA/RSI quiet).
    with open(sent_file, "w") as f:
        json.dump({"NVDA": {"worst_sentiment": -0.6, "negative_article_count": 2}}, f)
    trader.get_full_indicators = lambda s: {"atr_14": None, "sma_20": 99.0, "rsi_14": 50.0}
    results = trader.check_atr_stop_take_profit(account)
    check("negative news triggers exit", len(results) == 1 and results[0].get("trigger") == "negative_news", str(results))

    # All quiet -> no exit.
    trader.get_full_indicators = lambda s: {"atr_14": None, "sma_20": 99.0, "rsi_14": 50.0}
    with open(sent_file, "w") as f:
        json.dump({"NVDA": 0.2}, f)
    results = trader.check_atr_stop_take_profit(account)
    check("no exit when all quiet", results == [], str(results))

    trader.ENABLE_MA_BREAKDOWN_EXIT, trader.ENABLE_RSI_EXHAUSTION_EXIT, trader.ENABLE_NEGATIVE_NEWS_EXIT = old_ma, old_rsi, old_news
    for f in (exits_file, sent_file):
        if os.path.exists(f):
            os.remove(f)


# --- 18. Phase 2: economic-event sizing -----------------------------------------
def test_economic_event_sizing():
    print("\n[18] High-impact economic event sizes down new buys")
    eco_file = os.path.join(os.path.dirname(__file__), "logs", "eco_calendar.json")
    if os.path.exists(eco_file):
        os.remove(eco_file)

    from datetime import datetime as _dt
    # No cache -> no opinion -> full size.
    check("no cache -> multiplier 1.0", trader.get_economic_event_multiplier() == 1.0)

    # High-impact event today -> size down to 0.5.
    os.makedirs(os.path.dirname(eco_file), exist_ok=True)
    with open(eco_file, "w") as f:
        json.dump({"_fetched_at": _dt.now().isoformat(), "high_impact_today": "CPI (high impact) forecast 3.1%"}, f)
    check("event today -> 0.5x", trader.get_economic_event_multiplier() == 0.5, str(trader.get_economic_event_multiplier()))

    # Disabled -> full size even with an event cached.
    old = trader.ENABLE_ECONOMIC_CALENDAR
    trader.ENABLE_ECONOMIC_CALENDAR = False
    check("disabled -> multiplier 1.0", trader.get_economic_event_multiplier() == 1.0)
    trader.ENABLE_ECONOMIC_CALENDAR = old

    if os.path.exists(eco_file):
        os.remove(eco_file)


# --- 19. Phase 3: self-learning setup multiplier ----------------------------------
def test_setup_multiplier():
    print("\n[19] Self-learning weights setups by demonstrated edge")
    for f in (trader.TRADE_RESULTS_FILE, trader.OPEN_TRADES_FILE):
        if os.path.exists(f):
            os.remove(f)

    old_en, old_min, old_max, old_samples = (
        trader.SELF_LEARNING_ENABLED, trader.SETUP_MULT_MIN,
        trader.SETUP_MULT_MAX, trader.SELF_LEARNING_MIN_SAMPLES,
    )
    trader.SELF_LEARNING_ENABLED = True
    trader.SETUP_MULT_MIN = 0.5
    trader.SETUP_MULT_MAX = 1.5
    trader.SELF_LEARNING_MIN_SAMPLES = 5

    # No data -> no opinion -> 1.0.
    check("no data -> 1.0", trader.get_setup_multiplier("news") == 1.0)

    # 6 winning news trades -> sized UP.
    for _ in range(6):
        trader._append_csv(trader.TRADE_RESULTS_FILE, trader.RESULTS_HEADER, {
            "closed_at": "t", "opened_at": "t", "ticker": "NVDA",
            "entry_price": 100, "exit_price": 106, "qty": 10,
            "pnl_pct": 6.0, "pnl_dollars": 60, "setup": "news-driven buy", "exit_reason": "stop_loss",
        })
    mult = trader.get_setup_multiplier("news")
    check("winning setup sized up", 1.0 < mult <= 1.5, str(mult))
    check("unrelated setup stays 1.0", trader.get_setup_multiplier("breakout") == 1.0, str(trader.get_setup_multiplier("breakout")))

    # 6 losing breakout trades -> sized DOWN.
    for _ in range(6):
        trader._append_csv(trader.TRADE_RESULTS_FILE, trader.RESULTS_HEADER, {
            "closed_at": "t", "opened_at": "t", "ticker": "TSLA",
            "entry_price": 100, "exit_price": 93, "qty": 10,
            "pnl_pct": -7.0, "pnl_dollars": -70, "setup": "opening-range breakout", "exit_reason": "stop_loss",
        })
    mult = trader.get_setup_multiplier("breakout")
    check("losing setup sized down", 0.5 <= mult < 1.0, str(mult))

    # Disabled -> always 1.0.
    trader.SELF_LEARNING_ENABLED = False
    check("disabled -> 1.0", trader.get_setup_multiplier("news") == 1.0)
    trader.SELF_LEARNING_ENABLED = old_en
    trader.SETUP_MULT_MIN, trader.SETUP_MULT_MAX, trader.SELF_LEARNING_MIN_SAMPLES = old_min, old_max, old_samples

    brief = trader.build_performance_brief()
    check("performance brief mentions setups", "news" in brief and "breakout" in brief, brief)
    for f in (trader.TRADE_RESULTS_FILE, trader.OPEN_TRADES_FILE):
        if os.path.exists(f):
            os.remove(f)


# --- 20. Phase 2: fundamental extras in the quant score ----------------------------
def test_fundamental_extras_scoring():
    print("\n[20] Analyst/insider/reddit/earnings signals nudge the score")
    import signal_score
    # Neutral chart (base ~60) so positive boosts have headroom below the 100 cap.
    ind = {"trend": "sideways", "rsi_14": 55.0, "adx_14": 20.0, "macd": {"histogram": 0.0}, "relative_volume_pct": 0.0}
    base = signal_score.calculate_signal_score(ind)
    up = signal_score.calculate_signal_score(ind, extras={"analyst": "upgrade"})
    down = signal_score.calculate_signal_score(ind, extras={"analyst": "downgrade"})
    insider = signal_score.calculate_signal_score(ind, extras={"insider_net": 20000})
    reddit = signal_score.calculate_signal_score(ind, extras={"reddit_sentiment": 0.7})
    near_earn = signal_score.calculate_signal_score(ind, extras={"days_until_earnings": 2})
    check("upgrade boosts score", up > base, f"{up} vs {base}")
    check("downgrade penalizes score", down < base, f"{down} vs {base}")
    check("insider buying boosts score", insider > base, f"{insider} vs {base}")
    check("reddit sentiment boosts score", reddit > base, f"{reddit} vs {base}")
    check("earnings proximity penalizes score", near_earn < base, f"{near_earn} vs {base}")
    check("no extras -> unchanged", signal_score.calculate_signal_score(ind, extras={}) == base)


def test_economic_calendar_builtin():
    print("\n[21] Built-in economic calendar (free plan; no paid API, no fallback)")
    import data_feeds
    from datetime import date as _date

    eco_file = os.path.join(os.path.dirname(__file__), "logs", "eco_calendar.json")
    if os.path.exists(eco_file):
        os.remove(eco_file)

    today = _date.today()
    old_table = data_feeds._FALLBACK_EVENTS_2026
    try:
        # Pin "today" to an event day deterministically (the real 2026 table
        # may or may not have an event within 14 days of the test run).
        if today.isoformat() in old_table:
            fake_table = old_table
        else:
            fake_table = dict(old_table)
            fake_table[today.isoformat()] = ["CPI release"]
        data_feeds._FALLBACK_EVENTS_2026 = fake_table

        result = data_feeds.fetch_economic_calendar()
        check("builtin flags today's event", result["high_impact_today"] is not None, str(result["high_impact_today"]))
        check("builtin produces events", len(result["events"]) >= 1, str(len(result["events"])))
        check("builtin events are high impact", all(e["impact"] == "high" for e in result["events"]))
        check(
            "builtin events only within 14 days",
            all((_date.fromisoformat(e["date"]) - today).days <= 14 for e in result["events"]),
        )
        check("builtin is the primary source", result["_source"] == "builtin")

        # The cache-only reader (used in the hot sizing path) sees it too.
        hi, desc = data_feeds.high_impact_event_today()
        check("high_impact_event_today reads builtin", hi is True, str(desc))
    finally:
        data_feeds._FALLBACK_EVENTS_2026 = old_table
        if os.path.exists(eco_file):
            os.remove(eco_file)


# --- 22. extended-hours session detection ------------------------------------
def test_extended_hours_session():
    print("\n[22] Extended-hours session detection")
    from datetime import datetime as _dt
    import pytz as _pytz
    et = _pytz.timezone("America/New_York")
    cases = [
        (_dt(2026, 8, 12, 9, 0, tzinfo=et), True),    # Wed pre-market
        (_dt(2026, 8, 12, 10, 0, tzinfo=et), False),  # Wed regular (clock handles)
        (_dt(2026, 8, 12, 17, 30, tzinfo=et), True),  # Wed after-hours
        (_dt(2026, 8, 12, 22, 0, tzinfo=et), False),  # dead zone
        (_dt(2026, 8, 15, 17, 0, tzinfo=et), False),  # Saturday
    ]
    for dt, want in cases:
        got = trader.is_extended_session(dt)
        check(f"extended session @ {dt:%a %H:%M} -> {want}", got is want, str(got))


# --- 23. flat sizing: every trade the same size ---------------------------------
def test_flat_sizing_uniform():
    print("\n[23] Flat sizing: every trade the same size")
    old_flat = trader.FLAT_SIZING
    trader.FLAT_SIZING = True
    trader.FLAT_TRADE_SIZE_PCT = 0.10
    try:
        account = {"cash": 90000.0, "total_value": 100000.0, "holdings": {}}
        trader.trading_client = type("FakeClient", (), {
            "get_orders": lambda *a, **k: [],
            "submit_order": lambda self, req: type("O", (), {"id": "x", "status": "accepted"})(),
        })()
        trader.get_price = lambda s: 100.0
        trader.get_price_history = lambda *a, **k: None
        trader.get_full_indicators = lambda s: {"atr_14": 1.0, "vwap": 99.0, "intraday_momentum_pct": 0.5}

        # Two trades with very different confidence/conviction must size IDENTICALLY
        # (10% of 100k = $10k = qty 100 @ $100): flat sizing ignores them.
        hi = trader.execute_trade({"ticker": "MSFT", "action": "buy", "confidence": 95, "conviction": 10}, account)
        lo = trader.execute_trade({"ticker": "GOOG", "action": "buy", "confidence": 62, "conviction": 6}, account)
        check("high-confidence trade sized flat (qty 100)", hi.get("status") == "submitted" and abs(hi.get("qty", 0) - 100.0) < 0.01, json.dumps(hi))
        check("low-confidence trade sized the same (qty 100)", lo.get("status") == "submitted" and abs(lo.get("qty", 0) - 100.0) < 0.01, json.dumps(lo))

        # Confidence still gates: below the tradeable bar the trade is skipped
        # even though flat sizing would otherwise allow it.
        below = trader.execute_trade({"ticker": "NVDA", "action": "buy", "confidence": 50, "conviction": 6}, account)
        check("confidence below bar skipped in flat mode", below.get("status") == "skipped", json.dumps(below))
    finally:
        trader.FLAT_SIZING = old_flat


# --- 24. overnight trade queue ----------------------------------------------------
def test_pending_trade_queue():
    print("\n[24] Overnight queue: merge / load / clear")
    path = trader.PENDING_TRADES_FILE
    if os.path.exists(path):
        os.remove(path)
    try:
        n = trader.save_pending_trades([
            {"ticker": "GILD", "action": "buy", "confidence": 90},
            {"ticker": "COO", "action": "buy", "confidence": 85},
        ])
        check("first queue has 2 entries", n == 2, str(n))

        n = trader.save_pending_trades([
            {"ticker": "GILD", "action": "buy", "confidence": 93},  # newer wins
            {"ticker": "GPN", "action": "buy", "confidence": 88},
        ])
        q = trader.load_pending_trades()
        check("merge dedups by ticker+action", len(q) == 3, str(q))
        gild = [t for t in q if t["ticker"] == "GILD"][0]
        check("newest entry wins", gild["confidence"] == 93, str(gild))

        # Queue cap: 20 ideas saved -> only the top MAX_PENDING_TRADES by
        # conviction (then confidence) survive, so a long night can never
        # bloat the morning verification prompt.
        old_cap = trader.MAX_PENDING_TRADES
        trader.MAX_PENDING_TRADES = 4
        many = [{"ticker": f"T{i:02d}", "action": "buy", "conviction": 5, "confidence": 60.0 + i} for i in range(20)]
        # Two of them have top conviction: T15 (10/100) and T03 (9/95).
        many[15]["conviction"] = 10
        many[15]["confidence"] = 100.0
        many[3]["conviction"] = 9
        many[3]["confidence"] = 95.0
        trader.save_pending_trades(many)
        q = trader.load_pending_trades()
        check("queue capped at MAX_PENDING_TRADES", len(q) == 4, str(len(q)))
        check("cap keeps the highest-conviction ideas", {"T15", "T03"}.issubset({t["ticker"] for t in q}) and len(q) == 4, str([t["ticker"] for t in q]))
        trader.MAX_PENDING_TRADES = old_cap

        trader.clear_pending_trades()
        check("clear empties the queue", trader.load_pending_trades() == [], str(trader.load_pending_trades()))
    finally:
        trader.MAX_PENDING_TRADES = old_cap if "old_cap" in dir() else trader.MAX_PENDING_TRADES
        if os.path.exists(path):
            os.remove(path)


# --- 25. overnight queue expiration ---------------------------------------------
def test_pending_trade_expiration():
    print("\n[25] Overnight queue expiration and retry limits")
    from datetime import datetime as _dt, timedelta as _td
    old_path = trader.PENDING_TRADES_FILE
    old_state_path = trader.STALE_QUEUE_STATE_FILE
    old_age = trader.PENDING_TRADE_MAX_AGE_HOURS
    old_attempts = trader.PENDING_TRADE_MAX_ATTEMPTS
    old_cooldown = trader.STALE_QUEUE_RETRY_COOLDOWN_MINUTES
    temp = tempfile.mkdtemp()
    trader.PENDING_TRADES_FILE = os.path.join(temp, "pending.json")
    trader.STALE_QUEUE_STATE_FILE = os.path.join(temp, "cooldowns.json")
    trader.PENDING_TRADE_MAX_AGE_HOURS = 24.0
    trader.PENDING_TRADE_MAX_ATTEMPTS = 3
    trader.STALE_QUEUE_RETRY_COOLDOWN_MINUTES = 30.0
    now = _dt.utcnow()
    try:
        trader._save_json_file(trader.PENDING_TRADES_FILE, [
            {"ticker": "OLD", "action": "buy", "queued_at": (now - _td(hours=25)).isoformat(), "verification_attempts": 0},
            {"ticker": "RETRY", "action": "buy", "queued_at": now.isoformat(), "verification_attempts": 2},
            {"ticker": "FRESH", "action": "buy", "queued_at": now.isoformat(), "verification_attempts": 0},
        ])
        fresh, expired = trader.prune_pending_trades()
        check("expired queue entries are removed", expired == 1 and {x["ticker"] for x in fresh} == {"RETRY", "FRESH"}, str((fresh, expired)))
        marked = trader.mark_pending_verification_attempts(fresh)
        check("verification attempts increment without resetting age", marked[0].get("verification_attempts") == 3 and marked[0].get("queued_at"), str(marked))
        remaining, expired = trader.prune_pending_trades()
        check("retry limit removes exhausted ideas", expired == 1 and [x["ticker"] for x in remaining] == ["FRESH"], str((remaining, expired)))
        recent_stale = {"last_stale_at": _dt.utcnow().isoformat()}
        old_stale = {"last_stale_at": (_dt.utcnow() - _td(minutes=31)).isoformat()}
        check("recent stale item is deferred", trader.pending_trade_in_stale_cooldown(recent_stale) is True, str(recent_stale))
        check("stale cooldown eventually permits retry", trader.pending_trade_in_stale_cooldown(old_stale) is False, str(old_stale))
        trader.mark_pending_stale([{"ticker": "TRGP", "action": "buy"}])
        check("dedicated cooldown state survives queue metadata changes", trader.pending_trade_in_stale_cooldown({"ticker": "TRGP", "action": "buy"}) is True, str(trader._load_json_file(trader.STALE_QUEUE_STATE_FILE, {})))
    finally:
        trader.PENDING_TRADES_FILE = old_path
        trader.STALE_QUEUE_STATE_FILE = old_state_path
        trader.PENDING_TRADE_MAX_AGE_HOURS = old_age
        trader.PENDING_TRADE_MAX_ATTEMPTS = old_attempts
        trader.STALE_QUEUE_RETRY_COOLDOWN_MINUTES = old_cooldown
        shutil.rmtree(temp, ignore_errors=True)


# --- 26. sector metadata fallback ----------------------------------------------
def test_sector_metadata_fallback():
    print("\n[26] Sector metadata fallback during free-endpoint outage")
    import data_feeds
    old_load = data_feeds._load_cache
    old_save = data_feeds._save_cache
    old_get = data_feeds.requests.get
    calls = {"n": 0}
    try:
        data_feeds._load_cache = lambda *args, **kwargs: None
        data_feeds._save_cache = lambda *args, **kwargs: None
        def unavailable(*args, **kwargs):
            calls["n"] += 1
            raise RuntimeError("simulated free endpoint outage")
        data_feeds.requests.get = unavailable
        result = data_feeds.get_sector_profiles(["AMCR", "TRGP", "ZZZ"])
        check("known holdings receive local sector fallback", result["AMCR"]["sector"] == "Materials" and result["AMCR"]["source"] == "local_fallback", str(result))
        check("known candidate avoids extra profile request", result["TRGP"]["sector"] == "Energy" and calls["n"] == 1, str((result, calls)))
        check("unknown symbol remains explicitly unavailable", result["ZZZ"]["sector"] is None and result["ZZZ"]["source"] == "unavailable", str(result))
    finally:
        data_feeds._load_cache = old_load
        data_feeds._save_cache = old_save
        data_feeds.requests.get = old_get


# --- 27. dashboard watchlist sync -----------------------------------------------
def test_dashboard_watchlist_sync():
    print("\n[25] Dashboard watchlist sync: create when missing, update in place")
    calls = {"created": None, "updated": None}

    class FakeWL:
        def __init__(self, wid, name):
            self.id = wid
            self.name = name
            self.assets = []

    def make_client(existing):
        return type("FakeClient", (), {
            "get_watchlists": lambda *a, **k: list(existing),
            "create_watchlist": lambda self, req: calls.__setitem__("created", req),
            "update_watchlist_by_id": lambda self, wid, req: calls.__setitem__("updated", (wid, req)),
        })()

    trader.trading_client = make_client([])
    n = trader.sync_dashboard_watchlist(["aapl", "MSFT", "aapl", "GOOGL"])
    check("creates watchlist when missing", calls["created"] is not None, str(calls["created"]))
    check("dedups and uppercases symbols", calls["created"] and calls["created"].symbols == ["AAPL", "MSFT", "GOOGL"], str(calls["created"]))
    check("returns symbol count", n == 3, str(n))

    calls["created"] = None
    existing = [FakeWL("wl-1", trader.DASHBOARD_WATCHLIST_NAME)]
    trader.trading_client = make_client(existing)
    n = trader.sync_dashboard_watchlist(["NVDA", "AMD"])
    check("updates existing watchlist in place", calls["updated"] is not None and calls["updated"][0] == "wl-1", str(calls["updated"]))
    check("one update call replaces the full list", calls["updated"] and calls["updated"][1].symbols == ["NVDA", "AMD"], str(calls["updated"]))

    calls["updated"] = None
    trader.trading_client = make_client(existing)
    n = trader.sync_dashboard_watchlist([])
    check("empty tickers -> no-op (no API call)", calls["updated"] is None and n is None, str(n))


# --- 26. garbage exit-level guards (Gemini 1.4e-12 take-profit fix) -------------
def test_sane_price_rejects_garbage_levels():
    print("\n[26] Gemini exit-level sanitizer: garbage prices become None")
    import decide
    sane = decide._sane_price
    check("sane price passes through", sane(224.0) == 224.0, str(sane(224.0)))
    check("None stays None", sane(None) is None, str(sane(None)))
    # A lone tiny positive float passes the finiteness/positivity contract --
    # the PAIR check (tp <= stop, test 26b) and the entry-side guard in
    # check_atr_stop_take_profit (test 26c) are what reject it downstream.
    check("tiny positive float passes sanitizer (rejected by pair/entry guards)", sane(1.4450508544921876e-12) == 1.4450508544921876e-12, str(sane(1.4450508544921876e-12)))
    check("negative rejected", sane(-5.0) is None, str(sane(-5.0)))
    check("zero rejected", sane(0.0) is None, str(sane(0.0)))
    check("NaN rejected", sane(float("nan")) is None, str(sane(float("nan"))))
    check("inf rejected", sane(float("inf")) is None, str(sane(float("inf"))))
    check("string garbage rejected", sane("abc") is None, str(sane("abc")))


def test_inverted_pair_dropped_and_exit_refuses_garbage():
    print("\n[26b] Inverted tp<=stop pair dropped; _record_custom_exit refuses garbage")
    import decide
    # A Gemini pair with take_profit below the stop must be dropped entirely
    # (both levels -> None so the code derives sane ATR/swing levels instead).
    stop = decide._sane_price(129.5)
    tp = decide._sane_price(1.4450508544921876e-12)
    if stop is not None and tp is not None and tp <= stop:
        stop = tp = None
    check("inverted pair fully dropped", stop is None and tp is None, f"{stop} / {tp}")

    # _record_custom_exit must refuse to persist an inverted pair.
    old_file = trader.CUSTOM_EXITS_FILE
    import tempfile
    trader.CUSTOM_EXITS_FILE = os.path.join(tempfile.mkdtemp(), "exits.json")
    try:
        rec = trader._record_custom_exit("ZZZ", {"ticker": "ZZZ", "action": "buy"}, 124.0, levels=(130.0, 125.0))
        check("inverted levels not persisted", rec is None, str(rec))
        rec2 = trader._record_custom_exit("ZZZ", {"ticker": "ZZZ", "action": "buy"}, 124.0, levels=(122.0, 126.0))
        check("sane levels persisted", rec2 is not None and rec2["take_profit"] == 126.0, str(rec2))
    finally:
        trader.CUSTOM_EXITS_FILE = old_file


def test_garbage_recorded_tp_ignored_by_exit_engine():
    print("\n[26c] Exit engine ignores a garbage recorded take-profit (no instant sell)")
    old_fi = trader.get_full_indicators
    old_oo = trader.get_tickers_with_open_orders
    old_tod = trader.get_time_of_day_multiplier
    exits_file = trader.CUSTOM_EXITS_FILE
    if os.path.exists(exits_file):
        os.remove(exits_file)
    trader.get_full_indicators = lambda s: {"atr_14": 2.0}
    trader.get_price_history = lambda *a, **k: None
    trader.get_tickers_with_open_orders = lambda: []
    trader.get_time_of_day_multiplier = lambda *a, **k: 1.0
    try:
        # Recorded garbage: tp ~ 0 for a 100-entry position. The exit engine
        # must NOT fire an instant "take-profit hit" on a fresh 101 fill.
        trader._save_custom_exits({
            "NVDA": {"stop_loss": 96.0, "take_profit": 1.4450508544921876e-12, "entry_price": 100.0},
        })
        account = {"cash": 50000.0, "total_value": 100000.0,
                   "holdings": {"NVDA": {"qty": 10, "avg_entry_price": 100.0, "current_price": 101.0}}}
        results = trader.check_atr_stop_take_profit(account)
        check("garbage tp does not fire an instant sell", results == [], str(results))
    finally:
        trader.get_full_indicators = old_fi
        trader.get_tickers_with_open_orders = old_oo
        trader.get_time_of_day_multiplier = old_tod
        if os.path.exists(exits_file):
            os.remove(exits_file)


# --- 27. daily Gemini budget pacing ----------------------------------------------
def test_gemini_daily_budget_pacing():
    print("\n[27] Gemini budget pacing: calls spread across the whole day")
    import decide
    old_budget = decide.GEMINI_DAILY_BUDGET
    old_runs = decide.GEMINI_RUNS_PER_DAY
    decide.GEMINI_DAILY_BUDGET = 1000
    decide.GEMINI_RUNS_PER_DAY = 720
    model_list = ["gemini-flash-latest"]
    try:
        # Healthy day, 1 attempt per run: every run may call, budget never
        # exhausted, spread evenly (one per run -> ~30/hr).
        t = decide._default_tracker()
        calls = 0
        for run in range(1, 721):
            t["runs_today"] = run
            ok, _ = decide._should_attempt_call(t, model_list)
            if ok:
                st = decide._get_model_state(t, "gemini-flash-latest")
                st["count"] += 1
                t["last_call"] = "2026-08-14T00:00:00+00:00"
                calls += 1
        check("healthy day: every run gets a call", calls == 720, str(calls))
        check("healthy day: budget not exhausted", decide._total_used(t) <= 1000, str(decide._total_used(t)))

        # Slow-Google day (3 attempts per call): the EXACT failure mode that
        # hit 452 by 1 AM. Pacing must keep usage near the day's share.
        t2 = decide._default_tracker()
        hourly = {}
        for run in range(1, 721):
            t2["runs_today"] = run
            ok, _ = decide._should_attempt_call(t2, model_list)
            if ok:
                st = decide._get_model_state(t2, "gemini-flash-latest")
                st["count"] += 3
                t2["last_call"] = "2026-08-14T00:00:00+00:00"
            hour = (run - 1) // 30
            hourly.setdefault(hour, 0)
            if ok:
                hourly[hour] += 3
        used_1am = sum(hourly.get(h, 0) for h in range(1))
        check("1 AM usage capped near the day share (~41, was 452)", used_1am <= 60, str(used_1am))
        check("spread across all 24 hours", len([h for h in range(24) if hourly.get(h, 0) > 0]) == 24, str(hourly))
        check("no hour front-loads", max(hourly.values()) <= 60, str(max(hourly.values())))
        check("end of day stays at the budget", decide._total_used(t2) <= 1010, str(decide._total_used(t2)))

        # Disabled (0) -> unlimited attempts, previous behavior.
        decide.GEMINI_DAILY_BUDGET = 0
        ok, _ = decide._should_attempt_call(decide._default_tracker(), model_list)
        check("budget=0 disables pacing", ok, str(ok))

        # Morning-verification override: when the pacing curve says skip but
        # there are queued trades to verify, allow_despite_pacing lets the
        # call through (Google's own quota still gates it).
        decide.GEMINI_DAILY_BUDGET = 1000
        t4 = decide._default_tracker()
        st = decide._get_model_state(t4, "gemini-flash-latest")
        st["count"] = 456  # pre-existing usage far above the early share
        t4["runs_today"] = 8
        ok, reason = decide._should_attempt_call(t4, model_list)
        check("pacing blocks without override", not ok and "pacing" in (reason or ""), reason or "")
        ok2, _ = decide._should_attempt_call(t4, model_list, allow_despite_pacing=True)
        check("pacing bypassed for queue verification", ok2, str(ok2))
        # But if Google itself is exhausted, even the override must not call.
        t5 = decide._default_tracker()
        st5 = decide._get_model_state(t5, "gemini-flash-latest")
        st5["exhausted"] = True
        ok3, reason3 = decide._should_attempt_call(t5, model_list, allow_despite_pacing=True)
        check("override still respects Google's own quota", not ok3, reason3 or "")
    finally:
        decide.GEMINI_DAILY_BUDGET = old_budget
        decide.GEMINI_RUNS_PER_DAY = old_runs


# --- 28. indicator cache + run-budget guards (exit-124 fix) ---------------------
def test_indicator_cache_dedupes_fetches():
    print("\n[26] Indicator cache: repeated fetches hit the cache (run-time fix)")
    old_ttl = trader.FULL_INDICATOR_CACHE_TTL
    trader.FULL_INDICATOR_CACHE_TTL = 600  # keep entries alive for the test
    trader._INDICATOR_CACHE.clear()
    # Restore the REAL wrappers (earlier tests patched over them with lambdas).
    trader.get_price_history = _REAL_GET_PRICE_HISTORY
    trader.get_full_indicators = _REAL_GET_FULL_INDICATORS
    try:
        calls = {"n": 0}
        def fake_fetch(ticker, days=trader.PRICE_HISTORY_DAYS):
            calls["n"] += 1
            return {"closes": [1.0] * 60, "highs": [2.0] * 60, "lows": [0.5] * 60, "volumes": [1000] * 60}
        trader._fetch_price_history = fake_fetch
        trader.get_price_history("ZZZ")
        trader.get_price_history("ZZZ")
        trader.get_price_history("ZZZ")
        check("3 calls -> 1 underlying fetch", calls["n"] == 1, str(calls["n"]))
        # Different ticker must fetch fresh (no cross-ticker leakage).
        trader.get_price_history("YYY")
        check("different ticker fetches fresh", calls["n"] == 2, str(calls["n"]))
        # Full-indicators wrapper shares the same dedupe.
        fi_calls = {"n": 0}
        orig = trader._compute_full_indicators
        def fake_full(t):
            fi_calls["n"] += 1
            return orig(t)
        trader._compute_full_indicators = fake_full
        trader.get_full_indicators("ZZZ")
        trader.get_full_indicators("ZZZ")
        check("full-indicators wrapper caches too", fi_calls["n"] == 1, str(fi_calls["n"]))
        trader._compute_full_indicators = orig
    finally:
        trader.FULL_INDICATOR_CACHE_TTL = old_ttl
        trader._INDICATOR_CACHE.clear()


def test_high_conviction_swap():
    print("\n[28] High-conviction swap: 90+ idea sells smallest winner to fund")
    old_client = trader.trading_client
    old_price = trader.get_price
    old_ph = trader.get_price_history
    old_tod = trader.get_time_of_day_multiplier
    old_fi = trader.get_full_indicators
    old_sector_cap = trader.MAX_SECTOR_EXPOSURE_PCT
    trader.MAX_SECTOR_EXPOSURE_PCT = 0.0
    # Earlier tests replace trader.pending_order_notional with a stub and
    # never restore it -- so this test provides its own, mirroring the real
    # logic (sum open orders at $100) against the fake client's order list.
    old_pending = trader.pending_order_notional
    placed = []

    def _fake_pending():
        buy_n = sell_n = 0.0
        for o in placed:
            q = float(getattr(o, "qty", 0) or 0)
            if str(getattr(o, "side", "")) == "OrderSide.SELL":
                sell_n += q * 100.0
            else:
                buy_n += q * 100.0
        return buy_n, sell_n

    trader.pending_order_notional = _fake_pending

    class FakeClient:
        def get_clock(self):
            return type("C", (), {"is_open": True})()

        def get_orders(self, *a, **k):
            return placed  # the just-placed swap sell reads back as open

        def submit_order(self, req):
            placed.append(req)
            return type("O", (), {"id": "x", "status": "accepted"})()

    trader.trading_client = FakeClient()
    trader.get_price = lambda s: 100.0
    trader.get_price_history = lambda *a, **k: None
    trader.get_time_of_day_multiplier = lambda *a, **k: 1.0
    trader.get_full_indicators = lambda s: {"atr_14": 1.0, "vwap": 99.0, "intraday_momentum_pct": 0.5}
    try:
        # Two winners (GEHC $10k and NVDA $40k) but only $500 cash -- the
        # smallest winner must be sold to fund a 95-confidence idea.
        account = {
            "cash": 500.0, "total_value": 100000.0,
            "holdings": {
                "GEHC": {"qty": 100, "current_price": 100.0, "avg_entry_price": 95.0, "unrealized_plpc": 0.0526},
                "NVDA": {"qty": 400, "current_price": 100.0, "avg_entry_price": 95.0, "unrealized_plpc": 0.0526},
            },
        }
        trader._SWAPS_THIS_RUN = 0
        r = trader.execute_trade(
            {"ticker": "MSFT", "action": "buy", "confidence": 95, "conviction": 9, "dollar_amount": 0},
            account,
        )
        check("90+ idea funds via swap (sell + buy placed)",
              r["status"] == "submitted" and len(placed) == 2, json.dumps(r) + " | " + str(placed))
        check("swap sells the SMALLEST winner first",
              bool(placed) and placed[0].symbol == "GEHC",
              str([getattr(o, "symbol", None) for o in placed]))

        # A marginal idea (confidence 70, conviction 7) must NEVER swap.
        placed.clear()
        trader._SWAPS_THIS_RUN = 0
        r2 = trader.execute_trade(
            {"ticker": "AAPL", "action": "buy", "confidence": 70, "conviction": 7, "dollar_amount": 0},
            account,
        )
        check("low-conviction idea skips without swapping",
              r2["status"] == "skipped" and placed == [], json.dumps(r2) + " | " + str(placed))
    finally:
        trader.MAX_SECTOR_EXPOSURE_PCT = old_sector_cap
        trader.trading_client = old_client
        trader.get_price = old_price
        trader.get_price_history = old_ph
        trader.get_time_of_day_multiplier = old_tod
        trader.get_full_indicators = old_fi
        trader.pending_order_notional = old_pending
        trader._SWAPS_THIS_RUN = 0


def test_concentration_turnover_and_engine_gate():
    print("\\n[30] Correlation cap, turnover budget, and engine-quality gate")
    old_history = trader.get_price_history
    old_corr = trader.ENABLE_CORRELATION_CAP
    old_corr_threshold = trader.CORRELATION_THRESHOLD
    old_corr_cap = trader.MAX_CORRELATED_EXPOSURE_PCT
    old_gate = (trader.ENGINE_QUALITY_GATE_ENABLED, trader.ENGINE_QUALITY_GATE_MIN_SAMPLES,
                trader.ENGINE_QUALITY_GATE_MIN_WIN_RATE_PCT, trader.ENGINE_QUALITY_GATE_MAX_AVG_PNL_PCT)
    old_results = trader.TRADE_RESULTS_FILE
    old_turnover = trader.MAX_DAILY_TURNOVER_PCT
    temp = tempfile.mkdtemp()
    trader.TRADE_RESULTS_FILE = os.path.join(temp, "results.csv")
    try:
        closes = [100 + i * 0.5 for i in range(70)]
        trader.get_price_history = lambda _ticker, days=60: {"closes": closes}
        trader.ENABLE_CORRELATION_CAP = True
        trader.CORRELATION_THRESHOLD = 0.75
        trader.MAX_CORRELATED_EXPOSURE_PCT = 0.35
        room, peers = trader._correlation_room_for(
            {"total_value": 10000.0, "holdings": {"PEER": {"qty": 10, "current_price": 100}}},
            "CANDIDATE", 10000.0,
        )
        check("correlation cap finds highly correlated peer", room == 2500.0 and peers, str((room, peers)))

        with open(trader.TRADE_RESULTS_FILE, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["pnl_pct", "pnl_dollars", "engine", "setup", "exit_reason"])
            writer.writeheader()
            for _ in range(3):
                writer.writerow({"pnl_pct": -1.0, "pnl_dollars": -100, "engine": "technical_fallback", "setup": "technical", "exit_reason": "stop_loss"})
            for _ in range(3):
                writer.writerow({"pnl_pct": -1.0, "pnl_dollars": -100, "engine": "legacy/unknown", "setup": "old", "exit_reason": "stop_loss"})
        trader.ENGINE_QUALITY_GATE_ENABLED = True
        trader.ENGINE_QUALITY_GATE_MIN_SAMPLES = 3
        trader.ENGINE_QUALITY_GATE_MIN_WIN_RATE_PCT = 45.0
        trader.ENGINE_QUALITY_GATE_MAX_AVG_PNL_PCT = -0.25
        allowed, reason = trader.engine_quality_gate("technical_fallback")
        check("negative fallback expectancy is gated", allowed is False and "blocked" in reason, reason)
        allowed, reason = trader.engine_quality_gate("gemini")
        check("unattributed rows do not gate Gemini", allowed is True, reason)

        trader.MAX_DAILY_TURNOVER_PCT = 0.01
        state = trader._turnover_state()
        state["submitted_notional"] = 900.0
        trader._save_json_file(trader.TURNOVER_STATE_FILE, state)
        reason = trader._turnover_guard_reason(200.0, 100000.0, "buy", "decision")
        check("turnover budget blocks churn", reason is not None and "turnover budget" in reason, str(reason))
        reason = trader._turnover_guard_reason(200.0, 100000.0, "sell", "stop_loss")
        check("protective sell bypasses turnover budget", reason is None, str(reason))
    finally:
        trader.get_price_history = old_history
        trader.ENABLE_CORRELATION_CAP = old_corr
        trader.CORRELATION_THRESHOLD = old_corr_threshold
        trader.MAX_CORRELATED_EXPOSURE_PCT = old_corr_cap
        (trader.ENGINE_QUALITY_GATE_ENABLED,
         trader.ENGINE_QUALITY_GATE_MIN_SAMPLES,
         trader.ENGINE_QUALITY_GATE_MIN_WIN_RATE_PCT,
         trader.ENGINE_QUALITY_GATE_MAX_AVG_PNL_PCT) = old_gate
        trader.TRADE_RESULTS_FILE = old_results
        trader.MAX_DAILY_TURNOVER_PCT = old_turnover
        shutil.rmtree(temp, ignore_errors=True)


def test_operational_safety_controls():
    print("\n[29] Data guards, kill switch, shadow mode, holding limits, and fill accounting")
    import tempfile
    from datetime import datetime as _dt, timedelta as _td

    old_guards = trader.ENABLE_MARKET_DATA_GUARDS
    old_kill = trader.MANUAL_BUY_KILL_SWITCH
    old_shadow = trader.SHADOW_MODE
    old_max_hold = trader.MAX_HOLDING_HOURS
    old_stag = trader.STAGNATION_MAX_HOURS
    old_fi = trader.get_full_indicators
    old_price = trader.get_price
    old_orders = trader.get_tickers_with_open_orders
    old_client = trader.trading_client
    temp = tempfile.mkdtemp()
    old_paths = (trader.ORDER_LEDGER_FILE, trader.TRADE_RESULTS_FILE, trader.TRADES_JOURNAL_FILE, trader.OPEN_TRADES_FILE)
    trader.ORDER_LEDGER_FILE = os.path.join(temp, "ledger.json")
    trader.TRADE_RESULTS_FILE = os.path.join(temp, "results.csv")
    trader.TRADES_JOURNAL_FILE = os.path.join(temp, "journal.csv")
    trader.OPEN_TRADES_FILE = os.path.join(temp, "open.json")
    trader.get_price = lambda _s: 100.0
    trader.get_tickers_with_open_orders = lambda: set()
    trader.trading_client = type("FakeClient", (), {
        "get_orders": lambda *a, **k: [],
        "submit_order": lambda self, req: type("O", (), {"id": "new", "status": "accepted"})(),
    })()
    account = {"cash": 50000.0, "total_value": 100000.0, "holdings": {}}
    fresh = {
        "price": 100.0, "sma_20": 95.0, "vwap": 99.0,
        "intraday_momentum_pct": 0.5, "atr_14": 1.0,
        "quote_available": True, "quote_age_seconds": 5.0,
        "last_intraday_timestamp": _dt.utcnow().isoformat() + "+00:00",
        "avg_volume_20": 2_000_000.0, "spread_pct": 0.10,
    }
    try:
        trader.ENABLE_MARKET_DATA_GUARDS = True
        trader.MANUAL_BUY_KILL_SWITCH = False
        trader.SHADOW_MODE = False
        trader.get_full_indicators = lambda _s: dict(fresh)
        stale = dict(fresh)
        stale["quote_age_seconds"] = 999.0
        trader.get_full_indicators = lambda _s: stale
        r = trader.execute_trade({"ticker": "AAA", "action": "buy", "conviction": 8}, account)
        check("stale quote rejected", r.get("status") == "skipped" and "stale quote" in r.get("reason", ""), str(r))
        check("stale-data rejection is not mislabeled shadow mode", r.get("shadow") is False and r.get("data_guard") is True, str(r))

        trader.MANUAL_BUY_KILL_SWITCH = True
        r = trader.execute_trade({"ticker": "AAA", "action": "buy", "conviction": 8}, account)
        check("manual kill switch blocks buys", r.get("status") == "skipped" and "kill switch" in r.get("reason", ""), str(r))

        trader.MANUAL_BUY_KILL_SWITCH = False
        trader.SHADOW_MODE = True
        trader.get_full_indicators = lambda _s: dict(fresh)
        r = trader.execute_trade({"ticker": "AAA", "action": "buy", "conviction": 8}, account)
        check("shadow mode does not submit", r.get("status") == "shadow", str(r))

        trader.SHADOW_MODE = False
        trader.MAX_HOLDING_HOURS = 1.0
        trader.STAGNATION_MAX_HOURS = 0.0
        trader._save_json_file(trader.OPEN_TRADES_FILE, {
            "AAA": {"qty": 10, "entry": 100.0, "opened_at": (_dt.now() - _td(hours=2)).isoformat(), "engine": "gemini", "setup": "test"}
        })
        held_account = {"cash": 50000.0, "total_value": 100000.0, "holdings": {
            "AAA": {"qty": 10, "avg_entry_price": 100.0, "current_price": 100.0}
        }}
        r = trader.enforce_stagnant_positions(held_account)
        check("maximum holding-time exit fires", len(r) == 1 and r[0].get("trigger") == "max_holding_time", str(r))

        trader._save_json_file(trader.OPEN_TRADES_FILE, {
            "LEGACY": {"qty": 10, "entry": 100.0, "opened_at": (_dt.now() - _td(hours=2)).isoformat(), "engine": "legacy/unknown", "book": "legacy"}
        })
        legacy_account = {"cash": 50000.0, "total_value": 100000.0, "holdings": {
            "LEGACY": {"qty": 10, "avg_entry_price": 100.0, "current_price": 100.0}
        }}
        r = trader.enforce_stagnant_positions(legacy_account)
        check("baseline position receives the same stagnation exit", len(r) == 1 and r[0].get("trigger") == "max_holding_time", str(r))

        fake_filled = SimpleNamespace(id="fill-1", status="filled", filled_qty=10.0, filled_avg_price=101.0)
        trader._save_json_file(trader.OPEN_TRADES_FILE, {})
        trader._save_json_file(trader.ORDER_LEDGER_FILE, [{
            "order_id": "fill-1", "ticker": "AAA", "action": "buy", "qty": 10,
            "order_status": "accepted", "engine": "gemini", "reasoning": "news catalyst",
            "trigger": "decision", "accounted_filled_qty": 0.0,
        }])
        trader.trading_client = type("FilledClient", (), {"get_orders": lambda *a, **k: [fake_filled]})()
        counts = trader.reconcile_filled_orders()
        check("actual fill is reconciled", counts["filled"] == 1 and os.path.exists(trader.TRADE_RESULTS_FILE) is False, str(counts))
        # A buy does not close a result, but it must create the open-trade book.
        open_book = trader._load_json_file(trader.OPEN_TRADES_FILE, {})
        check("fill creates open trade record", open_book.get("AAA", {}).get("qty") == 10.0, str(open_book))
    finally:
        trader.ENABLE_MARKET_DATA_GUARDS = old_guards
        trader.MANUAL_BUY_KILL_SWITCH = old_kill
        trader.SHADOW_MODE = old_shadow
        trader.MAX_HOLDING_HOURS = old_max_hold
        trader.STAGNATION_MAX_HOURS = old_stag
        trader.get_full_indicators = old_fi
        trader.get_price = old_price
        trader.get_tickers_with_open_orders = old_orders
        trader.trading_client = old_client
        for path in old_paths:
            pass
        for name in os.listdir(temp):
            os.remove(os.path.join(temp, name))
        os.rmdir(temp)
        trader.ORDER_LEDGER_FILE, trader.TRADE_RESULTS_FILE, trader.TRADES_JOURNAL_FILE, trader.OPEN_TRADES_FILE = old_paths


def test_run_budget_guard_keeps_hard_stop():
    print("\n[27] Run-budget guard: spent budget skips fetches but hard stop still fires")
    old_fi = trader.get_full_indicators
    old_oo = trader.get_tickers_with_open_orders
    fetched = {"n": 0}
    def fake_full(s):
        fetched["n"] += 1
        return None
    trader.get_full_indicators = fake_full
    trader.get_tickers_with_open_orders = lambda: []
    try:
        snap = {"holdings": {"AAA": {"qty": 10, "avg_entry_price": 100.0, "current_price": 85.0}},
                "total_value": 10000.0, "cash": 5000.0}
        # Budget already spent: no indicator fetch, but the snapshot-price
        # hard stop (entry * (1 - MAX_POSITION_LOSS_PCT%)) must still fire.
        sells = trader.check_atr_stop_take_profit(snap, time_budget=0.0)
        check("no fetch with spent budget", fetched["n"] == 0, str(fetched["n"]))
        check("hard stop still triggered", len(sells) == 1 and sells[0].get("trigger") == "stop_loss", str(sells))
        # With budget remaining, the indicator fetch happens.
        fetched["n"] = 0
        trader.check_atr_stop_take_profit(snap, time_budget=10.0)
        check("fetch happens when budget remains", fetched["n"] == 1, str(fetched["n"]))
    finally:
        trader.get_full_indicators = old_fi
        trader.get_tickers_with_open_orders = old_oo


if __name__ == "__main__":
    # The daytrading entry window is wall-clock dependent: buy-path tests
    # call execute_trade() directly at whatever real time they run (often
    # outside the 9:30-16:00 + extended-session window). Stub the window
    # open for all buy-execution tests; the real window logic is verified
    # in test_daytrade_window_flags() below, which saves/restores the real
    # function around its own assertions.
    # Keep the smoke suite from writing fake orders, reports, or risk state
    # into the live logs/data directory. This matters because the production
    # bot reads those files on its next run.
    _test_state_dir = tempfile.mkdtemp(prefix="risk-test-state-")
    _state_bindings = {
        "COOLDOWN_FILE": "cooldowns.json",
        "CUSTOM_EXITS_FILE": "custom_exits.json",
        "RISK_STATE_FILE": "risk_state.json",
        "ORDER_LEDGER_FILE": "bot_order_ledger.json",
        "RECON_STATE_FILE": "reconciliation_state.json",
        "SETUP_GATE_FILE": "setup_gate.json",
        "TRADES_JOURNAL_FILE": "trades_journal.csv",
        "TRADE_RESULTS_FILE": "trade_results.csv",
        "OPEN_TRADES_FILE": "open_trades.json",
        "NEWS_SENTIMENT_CACHE_FILE": "news_sentiment_cache.json",
        "EARNINGS_CAL_FILE": "earnings_calendar.json",
        "PENDING_TRADES_FILE": "pending_trades.json",
        "STALE_QUEUE_STATE_FILE": "stale_queue_cooldowns.json",
        "ENGINE_PERFORMANCE_FILE": "engine_performance.csv",
        "SHADOW_TRADES_FILE": "shadow_trades.jsonl",
        "OPERATIONS_STATE_FILE": "operations_state.json",
        "DAILY_REPORT_FILE": "daily_report.csv",
        "WEEKLY_REPORT_FILE": "weekly_report.csv",
        "BOOK_PERFORMANCE_FILE": "book_performance.csv",
        "TURNOVER_STATE_FILE": "turnover_state.json",
        "SHADOW_REPORT_FILE": "shadow_report.csv",
    }
    _original_state_paths = {}
    for _name, _filename in _state_bindings.items():
        if hasattr(trader, _name):
            _original_state_paths[_name] = getattr(trader, _name)
            setattr(trader, _name, os.path.join(_test_state_dir, _filename))
    _REAL_WINDOW = trader.is_within_trade_window
    trader.is_within_trade_window = lambda: True
    test_no_margin_rule()
    test_pending_order_aware_cash()
    test_exposure_cap()
    test_circuit_breakers()
    test_deleveraging()
    test_deleveraging_counts_pending_sells()
    test_gross_exposure()
    test_order_ledger_and_reconciliation()
    test_ledger_status_refresh_stops_false_second_trader()
    test_bot_reduction_is_adopted_once()
    test_account_change_resets_stale_state()
    test_ledger_self_heal_recovers_lost_entries()
    test_technical_fallback_holds_losers()
    test_daytrade_window_flags()
    test_chase_filters()
    test_trailing_stop()
    test_scale_out()
    test_quality_trim()
    test_walkforward_live_learning()
    test_universe_slice_momentum_prefilter()
    test_risk_based_sizing()
    test_time_of_day_multiplier()
    test_news_confluence()
    test_trade_journal()
    test_engine_performance_report()
    test_confidence_sizing()
    test_news_scoring()
    test_smarter_exits()
    test_economic_event_sizing()
    test_setup_multiplier()
    test_fundamental_extras_scoring()
    test_economic_calendar_builtin()
    test_extended_hours_session()
    test_flat_sizing_uniform()
    test_pending_trade_queue()
    test_pending_trade_expiration()
    test_sector_metadata_fallback()
    test_dashboard_watchlist_sync()
    test_sane_price_rejects_garbage_levels()
    test_inverted_pair_dropped_and_exit_refuses_garbage()
    test_garbage_recorded_tp_ignored_by_exit_engine()
    test_gemini_daily_budget_pacing()
    test_indicator_cache_dedupes_fetches()
    test_run_budget_guard_keeps_hard_stop()
    test_high_conviction_swap()
    test_concentration_turnover_and_engine_gate()
    test_operational_safety_controls()
    trader.is_within_trade_window = _REAL_WINDOW
    for _name, _path in _original_state_paths.items():
        setattr(trader, _name, _path)
    shutil.rmtree(_test_state_dir, ignore_errors=True)
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
