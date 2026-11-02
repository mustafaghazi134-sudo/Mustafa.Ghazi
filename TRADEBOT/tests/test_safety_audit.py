"""Production-readiness audit fixes (2026-11-02). Each test reproduces a failure mode first found in the code and
asserts the fix: single-use approvals, kill switch re-checked at execution, atomic booking + idempotent
reconciliation, conservative handling of ambiguous Jupiter execute codes, cash/slots reserved for in-flight orders,
one engine per ledger, and credentials kept out of git and out of Claude Code's reach."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_crash_protection import CrashingBroker, _bot, _buy, _decide  # noqa: E402
from test_jupiter_api import ORDER, _broker  # noqa: E402

from tradebot.data.synthetic import SyntheticProvider
from tradebot.execution.base import ExecutionError, Quote, SubmittedUnknownError
from tradebot.execution.paper import PaperBroker
from tradebot.instance_lock import AlreadyRunningError, InstanceLock, lock_for
from tradebot.models import OrderRequest, Position, Side
from tradebot.risk.engine import MarketState, PortfolioState

ROOT = Path(__file__).resolve().parent.parent


def _paper(risk, clock):
    prov = SyntheticProvider(["SOL"], seed=1, history=100, total_bars=200)
    clock.t = prov.now_ts
    t = prov.get_tickers(["SOL"])["SOL"]
    return PaperBroker(risk, prov, clock=clock), t, MarketState(t.price, t.liquidity_usd, t.volume_24h_usd, clock())


def _pf(**kw):
    base = dict(equity_usd=1000, cash_usd=1000, positions={}, trades_today=0, realized_pnl_today_usd=0, consecutive_losses=0,
                last_loss_ts=None, peak_equity_usd=1000, day_start_equity_usd=1000)
    base.update(kw)
    return PortfolioState(**base)


# 1. single-use approvals ---------------------------------------------------------------------------------------
def test_approval_cannot_execute_the_same_order_twice(risk, clock):
    broker, t, mkt = _paper(risk, clock)
    o = OrderRequest(symbol="SOL", side=Side.BUY, notional_usd=100)
    d = risk.evaluate(o, _pf(), mkt)
    broker.execute(o, d.adjusted_qty, d.approval)
    with pytest.raises(PermissionError, match="already used"):
        broker.execute(o, d.adjusted_qty, d.approval)          # same order, same valid approval, inside the TTL


# 2. kill switch / daily halt re-checked between approval and execution ---------------------------------------------
def test_kill_switch_tripped_after_approval_blocks_entry_but_not_exit(risk, kill, clock):
    broker, t, mkt = _paper(risk, clock)
    buy = OrderRequest(symbol="SOL", side=Side.BUY, notional_usd=100)
    d = risk.evaluate(buy, _pf(), mkt)
    assert d.approved
    kill.trip("tripped by another process after approval", source="test")
    with pytest.raises(PermissionError, match="kill switch"):
        broker.execute(buy, d.adjusted_qty, d.approval)
    pos = Position(symbol="SOL", qty=0.5, avg_cost=t.price, opened_ts=clock())
    sell = OrderRequest(symbol="SOL", side=Side.SELL, notional_usd=0.5 * t.price, qty=0.5, reduce_only=True)
    ds = risk.evaluate(sell, _pf(positions={"SOL": pos}), mkt)
    assert broker.execute(sell, ds.adjusted_qty, ds.approval).side == Side.SELL     # exits still allowed


def test_daily_halt_after_approval_blocks_entry(risk, clock):
    broker, t, mkt = _paper(risk, clock)
    buy = OrderRequest(symbol="SOL", side=Side.BUY, notional_usd=100)
    d = risk.evaluate(buy, _pf(), mkt)
    risk.on_equity(_pf(equity_usd=950))                       # 5% day loss > 3% limit -> halt for the UTC day
    assert risk.is_daily_halted()
    with pytest.raises(PermissionError, match="daily-loss halt"):
        broker.execute(buy, d.adjusted_qty, d.approval)


# 3. atomic booking + idempotent reconciliation -----------------------------------------------------------------------
def test_booking_failure_keeps_nothing_and_reconcile_books_exactly_once(tmp_path):
    bot, ledger, broker, prov = _bot(tmp_path, lambda r, p: CrashingBroker(r, p, "ok"))
    real_update = ledger.update_intent

    def failing_update(iid, state=None, **kw):                # simulate the process dying mid-booking
        if state == "confirmed":
            raise RuntimeError("power cut")
        return real_update(iid, state, **kw)

    ledger.update_intent = failing_update
    ok, order = _buy(bot, prov)
    assert ok is False
    assert ledger.fill_for_order(order.id) is None and not bot.book.positions and bot.book.cash == pytest.approx(1000.0)
    assert ledger.load_positions() == {} and ledger.get("cash") in (None, 1000.0)
    pending = ledger.pending_intents("SOL")
    assert len(pending) == 1 and pending[0]["tx_signature"]                # evidence kept for reconciliation
    ledger.update_intent = real_update
    broker.lookup = "confirmed"
    assert bot.reconcile_pending()["confirmed"] == 1
    assert len(ledger._exec("SELECT * FROM fills WHERE order_id=?", (order.id,)).fetchall()) == 1
    qty = bot.book.positions["SOL"].qty
    bot.reconcile_pending()                                                 # second pass: nothing pending, nothing added
    assert bot.book.positions["SOL"].qty == qty and len(ledger.fills_since(0)) == 1


def test_reconcile_never_books_an_order_that_already_has_a_fill(tmp_path):
    bot, ledger, broker, prov = _bot(tmp_path, lambda r, p: CrashingBroker(r, p, "ok"))
    ok, order = _buy(bot, prov)
    assert ok
    iid = ledger.intents(1)[0]["id"]
    ledger._exec("UPDATE intents SET state='signed' WHERE id=?", (iid,))   # as if the confirm write were lost (raw: the lifecycle forbids it)
    broker.lookup = "confirmed"
    before = (bot.book.positions["SOL"].qty, bot.book.cash)
    assert bot.reconcile_pending()["confirmed"] == 1
    assert (bot.book.positions["SOL"].qty, bot.book.cash) == before and len(ledger.fills_since(0)) == 1


# 4. ambiguous Jupiter execute codes never lead to a second signed transaction ---------------------------------------
@pytest.mark.parametrize("code", [-1000, -1001, -1004, -2000, -2001, -2004, 4242])
def test_ambiguous_execute_codes_are_unknown_and_never_resigned(tmp_path, monkeypatch, code):
    s, b = _broker(tmp_path, monkeypatch=monkeypatch)
    s.execution.execute_max_retries = 3
    calls, signs = [], []
    b._request = lambda client, method, url, **kw: calls.append(url.rsplit("/", 1)[-1]) or (
        ORDER if url.endswith("/order") else {"status": "Failed", "code": code, "error": "x"})
    b._sign = lambda tx: signs.append(tx) or ("signed", f"SIG{len(signs)}")
    with pytest.raises(SubmittedUnknownError) as ei:
        b._execute(OrderRequest(symbol="SOL", side=Side.BUY, notional_usd=100), 0.554, Quote("SOL", Side.BUY, 180.0, 1.0, 0.02, 100.0, 0.554, raw=ORDER))
    assert calls == ["execute"] and len(signs) == 1 and ei.value.tx_signature == "SIG1" and ei.value.request_id == "req-1"


@pytest.mark.parametrize("code", [-2, -3, -1002, -1003, -2002])
def test_rejected_execute_codes_fail_without_retry(tmp_path, monkeypatch, code):
    s, b = _broker(tmp_path, monkeypatch=monkeypatch)
    s.execution.execute_max_retries = 3
    calls = []
    b._request = lambda client, method, url, **kw: calls.append(url.rsplit("/", 1)[-1]) or {"status": "Failed", "code": code, "error": "x"}
    b._sign = lambda tx: ("signed", "SIG")
    with pytest.raises(ExecutionError) as ei:
        b._execute(OrderRequest(symbol="SOL", side=Side.BUY, notional_usd=100), 0.554, Quote("SOL", Side.BUY, 180.0, 1.0, 0.02, 100.0, 0.554, raw=ORDER))
    assert not isinstance(ei.value, SubmittedUnknownError) and calls == ["execute"]


# 5. in-flight orders reserve cash, exposure and a position slot -------------------------------------------------------
def test_in_flight_buy_reserves_cash_slot_and_symbol(tmp_path):
    bot, ledger, broker, prov = _bot(tmp_path, lambda r, p: CrashingBroker(r, p, "crash_after_sign"))
    _buy(bot, prov, notional=90.0)                            # stays UNKNOWN (signed, outcome not known)
    st = bot.book.risk_state({"SOL": prov.get_tickers(["SOL"])["SOL"].price})
    assert st.pending_buy_symbols == frozenset({"SOL"}) and st.pending_buy_usd == pytest.approx(90.0, rel=0.05)
    assert st.cash_usd == pytest.approx(bot.book.cash - st.pending_buy_usd)
    t = prov.get_tickers(["SOL"])["SOL"]
    mkt = MarketState(t.price, t.liquidity_usd, t.volume_24h_usd, t.ts)
    other = OrderRequest(symbol="JUP", side=Side.BUY, notional_usd=1000.0)
    bot.risk.limits.max_open_positions = 1
    d = bot.risk.evaluate(other, st, mkt)
    assert not d.approved and "in flight" in d.reasons[0]     # the in-flight order occupies the only slot


def test_in_flight_buy_counts_toward_total_exposure(risk, clock):
    broker, t, mkt = _paper(risk, clock)
    risk.limits.max_position_pct = 50
    st = _pf(cash_usd=1000 - 400, pending_buy_usd=400.0, pending_buy_symbols=frozenset({"ETH"}))
    d = risk.evaluate(OrderRequest(symbol="SOL", side=Side.BUY, notional_usd=400), st, mkt)
    assert d.approved and d.adjusted_notional_usd == pytest.approx(100.0)   # 50% total exposure cap: 500 - 400 in flight


# 6b. a failed emergency flatten is retried until the book is flat ------------------------------------------------------
def test_kill_switch_flatten_is_retried_after_a_failed_exit(tmp_path):
    bot, ledger, broker, prov = _bot(tmp_path, lambda r, p: CrashingBroker(r, p, "ok"))
    ok, _ = _buy(bot, prov)
    assert ok and "SOL" in bot.book.positions
    bot.kill.trip("operator", source="test")
    broker.mode = "fail_before_sign"                          # the first exit attempt fails (no route, RPC down...)
    bot.cycle()
    assert "SOL" in bot.book.positions
    broker.mode = "ok"
    bot.cycle()                                               # before the fix: never retried while the switch stayed on
    assert bot.book.positions == {} and ledger.trades()[-1].exit_reason.startswith("flatten")


def test_fast_engine_kill_flatten_is_retried(tmp_path):
    from test_fast_engine import _clock, _engine
    from test_scanner import BAD, SOL as SOL_MINT, FakeFeed, _facts
    now, advance = _clock()
    feed = FakeFeed({SOL_MINT: 98.0, BAD: 1.0}, {"SOLUSDT": 100.0}, _facts())
    eng, fl, store, kill = _engine(tmp_path, feed, clock=now)
    eng.poll()
    assert "SOL" in eng.book.positions
    kill.trip("operator", source="test")
    real = eng.broker._execute
    eng.broker._execute = lambda *a, **k: (_ for _ in ()).throw(ExecutionError("no route"))
    advance(5); eng.poll()
    assert "SOL" in eng.book.positions
    eng.broker._execute = real
    advance(5); eng.poll()
    assert eng.book.positions == {}


# 6. one engine per ledger -----------------------------------------------------------------------------------------------
def test_second_engine_on_the_same_ledger_is_refused(tmp_path):
    a = lock_for(tmp_path / "tradebot.db").acquire()
    with pytest.raises(AlreadyRunningError):
        lock_for(tmp_path / "tradebot.db").acquire()
    InstanceLock(tmp_path / "fast.db.lock").acquire().release()           # a different ledger is independent
    a.release()
    lock_for(tmp_path / "tradebot.db").acquire().release()                # free again once released


def test_cli_run_refuses_to_start_while_another_engine_holds_the_ledger(tmp_path, capsys):
    from tradebot.cli import main
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'mode = "paper"\ndata_dir = "{tmp_path.as_posix()}/data"\n[claude]\nenabled = false\n[data]\nprovider = "synthetic"\n')
    from tradebot.config import load_settings
    held = lock_for(load_settings(cfg).db_path).acquire()      # data/ledger.sqlite.lock, as the CLI derives it
    try:
        assert main(["--config", str(cfg), "run", "--paper", "--cycles", "1"]) == 3
        assert "refusing to start" in capsys.readouterr().out
        assert main(["--config", str(cfg), "fast", "--minutes", "0"]) in (2, 3)  # fast: own lock, then no key -> 2
    finally:
        held.release()


# 7. credentials stay out of git and out of Claude Code's reach --------------------------------------------------------
@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_secret_backups_are_git_ignored():
    names = ["backups/.env.20261102-101500", "backups/config.toml.20261102-101500", "config.toml.bak-20261102-101500",
             ".env", ".env.local", "config.toml", "my-keypair.json", "data/ledger.sqlite.lock"]
    for n in names:
        r = subprocess.run(["git", "check-ignore", "-q", n], cwd=ROOT)
        assert r.returncode == 0, f"{n} would be committed"
    assert subprocess.run(["git", "check-ignore", "-q", ".env.example"], cwd=ROOT).returncode == 1


def test_claude_code_cannot_read_secrets_or_arm_live():
    deny = set(json.loads((ROOT / ".claude" / "settings.json").read_text())["permissions"]["deny"])
    assert {"Read(./.env)", "Read(**/.env)", "Read(./backups/**)", "Read(**/*keypair*.json)", "Bash(*arm-live*)"} <= deny
