"""One supervised EXECUTION TEST with real funds: buy a fixed small USD amount of one symbol, sell exactly what was
bought straight back, then reconcile both legs and the wallet against the chain and write a report.

This proves the execution path (quote -> verify -> simulate -> separate signer -> submit -> confirm -> chain fill ->
ledger -> wallet reconciliation) on real money. It is NOT a strategy and says nothing about profitability.

Isolation from unattended trading:
  - It needs its own single-use approval file, written by the owner with `tradebot approve-exec-test`, which typed the
    exact symbol, amount and maximum loss. The file expires within 2 hours and is consumed by the run.
  - It uses ExecTestGate instead of the engine's LiveGate. The engine's arming (paper-record requirement, armed file,
    mode = "live") is untouched. This command REFUSES to run if the engine is armed, and it never arms anything.
  - Every execution control still applies: the build switch, the separate signer with owner activation and its own
    caps, LiveControls (order, exposure, daily loss, daily orders, SOL reserve, live halt), RiskEngine single-use
    approvals, pre-sign transaction verification, simulation, intents persisted before submission, chain-evidence
    reconciliation of unknown outcomes.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from ..config import Settings
from ..models import OrderRequest, Side
from .base import ExecutionError
from .live_gate import ENV_LIVE, ENV_LIVE_VALUE, LiveGate, LiveGateError

log = logging.getLogger(__name__)

APPROVAL_FILE = "EXEC_TEST_APPROVAL.json"
MAX_TEST_USD = 25.0
MAX_TTL_HOURS = 2.0
EXPLORER = "https://solscan.io/tx/"
SELL_PROBE_QTY = {"SOL": 0.01}           # verify + simulate a sell of this size before buying (no probe for other symbols yet)


def approval_path(settings: Settings) -> Path:
    return settings.data_path / APPROVAL_FILE


def approval_phrase(symbol: str, usd: float, max_loss_usd: float) -> str:
    return f"I APPROVE ONE {symbol.upper()} ROUND TRIP OF {usd:.2f} USD WITH MAX LOSS {max_loss_usd:.2f} USD"


def write_approval(settings: Settings, symbol: str, usd: float, max_loss_usd: float, max_cost_bps: float, hours: float,
                   confirm: str, clock=time.time) -> dict:
    """Owner-only. Writes the single-use approval for exactly one round trip with these limits."""
    symbol = symbol.upper()
    problems = []
    if symbol not in settings.live.allowed_symbols:
        problems.append(f"{symbol} is not in [live] allowed_symbols {settings.live.allowed_symbols}")
    if not 0 < usd <= min(MAX_TEST_USD, settings.live.max_order_usd):
        problems.append(f"amount must be > 0 and <= {min(MAX_TEST_USD, settings.live.max_order_usd):.2f} USD ([live] max_order_usd)")
    if not 0 < max_loss_usd <= min(usd, settings.live.max_daily_loss_usd):
        problems.append(f"max loss must be > 0 and <= min(amount, [live] max_daily_loss_usd = {settings.live.max_daily_loss_usd:.2f})")
    if not 0 < max_cost_bps <= settings.live.max_total_cost_bps:
        problems.append(f"max cost must be > 0 and <= [live] max_total_cost_bps = {settings.live.max_total_cost_bps:.0f}")
    if not 0 < hours <= MAX_TTL_HOURS:
        problems.append(f"validity must be > 0 and <= {MAX_TTL_HOURS} hours")
    if not settings.live.wallet_pubkey:
        problems.append("[live] wallet_pubkey is not set")
    phrase = approval_phrase(symbol, usd, max_loss_usd)
    if confirm.strip() != phrase:
        problems.append(f"confirmation must be exactly: {phrase}")
    if problems:
        raise LiveGateError("; ".join(problems))
    now = clock()
    data = {"symbol": symbol, "usd": round(usd, 2), "max_loss_usd": round(max_loss_usd, 2), "max_cost_bps": max_cost_bps,
            "wallet": settings.live.wallet_pubkey, "created_ts": now, "expires_ts": now + hours * 3600, "state": "approved",
            "confirm": phrase, "history": [{"ts": now, "state": "approved"}]}
    p = approval_path(settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=1))
    return data


def load_approval(settings: Settings) -> dict | None:
    p = approval_path(settings)
    if not p.exists():
        return None
    return json.loads(p.read_text())


def _set_state(settings: Settings, appr: dict, state: str, clock, **extra) -> None:
    appr["state"] = state
    appr.setdefault("history", []).append({"ts": clock(), "state": state, **extra})
    approval_path(settings).write_text(json.dumps(appr, indent=1))


class ExecTestGate:
    """Stands in for LiveGate for this one command. Never arms the engine; refuses if the engine is armed."""

    def __init__(self, settings: Settings, clock=time.time, allowed_states=("approved", "buying", "bought", "selling")):
        self.s, self.clock, self.allowed = settings, clock, allowed_states

    def problems(self) -> list[str]:
        out = []
        appr = load_approval(self.s)
        if appr is None:
            out.append("no execution-test approval (owner runs: tradebot approve-exec-test ...)")
        else:
            if appr.get("state") not in self.allowed:
                out.append(f"approval state is {appr.get('state')!r}; approvals are single use")
            if self.clock() > appr.get("expires_ts", 0):
                out.append("approval expired")
            if appr.get("wallet") != self.s.live.wallet_pubkey:
                out.append("approval is for a different wallet")
        if os.environ.get(ENV_LIVE) != ENV_LIVE_VALUE:
            out.append(f"env {ENV_LIVE} != {ENV_LIVE_VALUE}")
        if self.s.armed_file.exists():
            out.append("the trading engine is armed (tradebot disarm-live first): the test must not share an armed engine")
        return out

    def assert_armed(self) -> None:
        p = self.problems()
        if p:
            raise LiveGateError("execution test blocked: " + "; ".join(p))

    def is_armed(self) -> bool:
        return not self.problems()


def exec_test_risk_limits(settings: Settings, usd: float, equity_usd: float):
    """RiskEngine limits for the test: the approved amount and nothing more (tighter than or equal to [live])."""
    r = settings.risk.model_copy()
    r.max_position_pct = 100.0
    r.max_total_exposure_pct = 100.0
    r.max_open_positions = 1
    r.max_order_notional_usd = round(usd * 1.10, 2)        # the sell leg is valued at the live price
    r.min_order_notional_usd = min(r.min_order_notional_usd, max(0.5, usd * 0.5))
    r.max_trades_per_day = 2
    r.loss_cooldown_sec = 0.0
    r.max_daily_loss_pct = 100.0                            # the cap that matters is LiveControls.max_daily_loss_usd
    r.max_drawdown_pct = 100.0
    return r


def _wait_reconciled(bot, sym: str, sleep, tries: int = 24, every: float = 5.0) -> bool:
    for _ in range(tries):
        bot.reconcile_pending()
        if not bot.ledger.pending_intents(sym):
            return True
        sleep(every)
    return not bot.ledger.pending_intents(sym)


def run_exec_test(settings: Settings, adapter, bot, controls, clock=time.time, sleep=time.sleep, exit_only: bool = False) -> dict:
    """adapter: JupiterExecutionAdapter wired with ExecTestGate, LiveControls and the signer client.
    bot: TradingBot over the execution-test ledger with that adapter as broker. Returns the report (also written)."""
    appr = load_approval(settings)
    gate = ExecTestGate(settings, clock)
    rep: dict = {"kind": "EXECUTION TEST (not a strategy; says nothing about profitability)", "started_ts": clock(),
                 "approval": {k: appr.get(k) for k in ("symbol", "usd", "max_loss_usd", "max_cost_bps", "wallet", "expires_ts", "state")} if appr else None,
                 "legs": [], "problems": []}

    def finish(state: str) -> dict:
        rep["finished_ts"] = clock()
        rep["result"] = state
        path = settings.data_path / f"exec_test_report_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(clock()))}.json"
        path.write_text(json.dumps(rep, indent=1, default=str))
        rep["report_file"] = str(path)
        return rep

    gp = gate.problems()
    if gp:
        rep["problems"] += gp
        return finish("REFUSED")
    sym, usd = appr["symbol"], float(appr["usd"])
    if exit_only and appr["state"] not in ("bought", "selling"):
        rep["problems"].append(f"--exit-only needs an approval in state bought/selling, not {appr['state']}")
        return finish("REFUSED")
    if not exit_only and appr["state"] != "approved":
        rep["problems"].append(f"approval state is {appr['state']!r}; use --exit-only to sell back a held position")
        return finish("REFUSED")
    if bot.ledger.pending_intents(sym):
        if not _wait_reconciled(bot, sym, sleep):
            rep["problems"].append("an earlier order is still unresolved; see tradebot live-status")
            return finish("REFUSED")

    bal0 = adapter.get_balances()
    rep["balances_before"] = bal0
    if not exit_only:
        # cost check on a real, verified and simulated route BEFORE anything is signed
        dry = adapter.dry_run_order(sym, Side.BUY, usd)
        rep["pre_trade_dry_run"] = {k: dry.get(k) for k in ("ok", "problems", "router", "inAmount", "outAmount", "otherAmountThreshold",
                                                              "feeBps", "priceImpact_bps", "reference_price", "live_controls_would_block")}
        if not dry.get("ok") or dry.get("live_controls_would_block"):
            rep["problems"].append("pre-trade dry run refused: " + "; ".join((dry.get("problems") or []) + (dry.get("live_controls_would_block") or [])))
            return finish("REFUSED")
        # the SELL route must also pass verification and simulation BEFORE buying, or the test could strand a
        # position: probe it with a small quantity the wallet already holds (its SOL reserve, for SOL)
        probe_qty = SELL_PROBE_QTY.get(sym)
        if probe_qty is None or bal0.get(sym, 0.0) < probe_qty * 1.5:
            rep["problems"].append(f"cannot pre-verify the {sym} sell route: the wallet needs at least {(probe_qty or 0) * 1.5:g} {sym} "
                                   "to build a probe sell")
            return finish("REFUSED")
        sp = adapter.dry_run_order(sym, Side.SELL, probe_qty)
        rep["pre_trade_sell_route_probe"] = {k: sp.get(k) for k in ("ok", "problems", "router", "programs", "live_controls_would_block")}
        rep["pre_trade_sell_route_probe"]["qty"] = probe_qty
        if not sp.get("ok"):
            rep["problems"].append("the sell route would be refused, so nothing is bought: " + "; ".join(sp.get("problems") or []))
            return finish("REFUSED")
        out_ui = int(dry["outAmount"]) / 10 ** adapter.decimals[sym]
        buy_cost_bps = (usd / out_ui / dry["reference_price"] - 1) * 1e4
        sq = adapter.quote(sym, Side.SELL, out_ui)
        rt_cost_bps = buy_cost_bps + sq.expected_slippage_bps      # quote(): cost vs reference, bps
        rep["estimated_round_trip_cost_bps"] = round(rt_cost_bps, 1)
        rep["estimated_round_trip_cost_usd"] = round(usd * rt_cost_bps / 1e4, 4)
        if rt_cost_bps > appr["max_cost_bps"] or usd * rt_cost_bps / 1e4 > appr["max_loss_usd"]:
            rep["problems"].append(f"estimated round-trip cost {rt_cost_bps:.1f} bps (${usd * rt_cost_bps / 1e4:.3f}) exceeds the approved "
                                   f"{appr['max_cost_bps']} bps / ${appr['max_loss_usd']:.2f}")
            return finish("REFUSED")
        # ---- BUY (single use: the approval is marked before anything can be signed)
        _set_state(settings, appr, "buying", clock)
        t = bot.provider.get_tickers([sym])[sym]
        from ..risk.engine import MarketState
        order = OrderRequest(symbol=sym, side=Side.BUY, notional_usd=usd, strategy="exec_test", reason="execution test buy")
        order.ts = clock()
        d = bot.risk.evaluate(order, bot.book.risk_state({sym: t.price}), MarketState(t.price, t.liquidity_usd, t.volume_24h_usd, t.ts))
        if not d.approved or d.approval is None:
            rep["problems"].append("risk engine rejected the buy: " + "; ".join(d.reasons))
            _set_state(settings, appr, "refused", clock)
            return finish("REFUSED")
        ok = bot._execute_with_intent(order, d.adjusted_qty or 0.0, d)
        if not ok and bot.ledger.pending_intents(sym):
            _wait_reconciled(bot, sym, sleep)
        rep["legs"].append(_leg(bot, order.id, "buy"))
        if sym not in bot.book.positions:
            _set_state(settings, appr, "done_no_fill", clock)
            rep["problems"].append("buy did not fill (nothing held); see the leg for the reason")
            rep["balances_after"] = adapter.get_balances()
            return finish("BUY_NOT_FILLED")
        _set_state(settings, appr, "bought", clock)

    # ---- SELL exactly the held quantity straight back
    _set_state(settings, appr, "selling", clock)
    sold = False
    for attempt in range(3):
        pos = bot.book.positions.get(sym)
        if pos is None:
            sold = True
            break
        t = bot.provider.get_tickers([sym])[sym]
        before = {r["id"] for r in bot.ledger.intents(50)}
        bot._close_position(sym, "exec_test_exit", t)
        new = [r for r in bot.ledger.intents(50) if r["id"] not in before]
        if bot.ledger.pending_intents(sym):
            _wait_reconciled(bot, sym, sleep)
        if new:
            rep["legs"].append(_leg(bot, new[0]["order_id"], f"sell (attempt {attempt + 1})"))
        if sym not in bot.book.positions:
            sold = True
            break
        sleep(5.0)
    rep["balances_after"] = adapter.get_balances()
    rep["reconciliation"] = _reconcile(rep, sym)
    if not sold:
        _set_state(settings, appr, "bought", clock)
        rep["problems"].append(f"{sym} is still held. Re-run with --exit-only within the approval window, or sell it in your wallet app.")
        return finish("POSITION_STILL_HELD")
    _set_state(settings, appr, "done", clock)
    used = approval_path(settings)
    used.rename(used.with_name(f"EXEC_TEST_USED_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(clock()))}.json"))
    rec_ok = rep["reconciliation"]["usdc_match"] and rep["reconciliation"]["token_match"]
    if not rec_ok:
        controls.halt("execution test: chain balances do not match the booked fills", source="exec_test")
    return finish("COMPLETED" if rec_ok else "COMPLETED_WITH_MISMATCH")


def _leg(bot, order_id: str, label: str) -> dict:
    row = next((r for r in bot.ledger.intents(50) if r["order_id"] == order_id), None)
    fill = bot.ledger._exec("SELECT * FROM fills WHERE order_id=? ORDER BY ts DESC LIMIT 1", (order_id,)).fetchone()
    leg = {"leg": label, "order_id": order_id, "intent_state": row["state"] if row else None,
           "result": row["result"] if row else None, "tx_signature": row["tx_signature"] if row else None}
    if leg["tx_signature"]:
        leg["explorer"] = EXPLORER + leg["tx_signature"]
    if fill is not None:
        leg.update(qty=fill["qty"], price=fill["price"], notional_usd=fill["notional_usd"], fee_usd=fill["fee_usd"])
    return leg


def _reconcile(rep: dict, sym: str) -> dict:
    """Chain balance changes against the fills the ledger booked (both read from the chain, independently)."""
    b0, b1 = rep.get("balances_before") or {}, rep.get("balances_after") or {}
    buys = [x for x in rep["legs"] if x["leg"] == "buy" and "qty" in x]
    sells = [x for x in rep["legs"] if x["leg"].startswith("sell") and "qty" in x]
    booked_usdc = sum(x["notional_usd"] for x in sells) - sum(x["notional_usd"] for x in buys)
    booked_qty = sum(x["qty"] for x in buys) - sum(x["qty"] for x in sells)
    chain_usdc = b1.get("USDC", 0.0) - b0.get("USDC", 0.0)
    out = {"chain_usdc_change": round(chain_usdc, 6), "booked_usdc_change": round(booked_usdc, 6),
           "usdc_match": abs(chain_usdc - booked_usdc) <= 0.011, "booked_token_change": round(booked_qty, 9)}
    if sym == "SOL":
        # native SOL also pays network fees and any rent; compare after removing them (reported separately)
        chain_sol = b1.get("SOL", 0.0) - b0.get("SOL", 0.0)
        out["chain_sol_change"] = round(chain_sol, 9)
        out["implied_network_cost_sol"] = round(booked_qty - chain_sol, 9)
        out["token_match"] = -0.0002 <= booked_qty - chain_sol <= 0.003     # fees + priority (+ refundable rent) bound
    else:
        chain_tok = b1.get(sym, 0.0) - b0.get(sym, 0.0)
        out["chain_token_change"] = round(chain_tok, 9)
        out["token_match"] = abs(chain_tok - booked_qty) <= max(1e-9, abs(booked_qty) * 1e-6)
    out["net_result_usd"] = round(booked_usdc, 4)
    return out
