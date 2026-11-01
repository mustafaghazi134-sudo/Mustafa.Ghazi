"""Offline operational self-test: runs a short replay session on synthetic data in a temporary ledger and
checks that the execution path behaves (every order risk-approved, limits held, fills priced with fees and
slippage, P&L consistent, kill switch flattens). No network, no Claude, no real funds."""
from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass

from .config import Settings
from .engine.bot import build_bot


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def run_selftest(cycles: int = 700) -> list[Check]:
    checks: list[Check] = []
    with tempfile.TemporaryDirectory() as tmp:
        s = Settings(mode="paper", data_dir=tmp, initial_capital_usd=1000.0)
        s.claude.enabled = False
        s.data.provider = "synthetic"
        s.data.candle_history = 300
        s.universe.symbols = ["SOL", "JUP", "RAY", "BONK", "WIF"]
        s.risk.min_liquidity_usd = 1000
        s.risk.min_volume_24h_usd = 1000
        s.risk.max_daily_loss_pct = 50
        s.risk.max_drawdown_pct = 60
        s.claude.research_interval_hours = 1000
        bot = build_bot(s)
        bot.run_forever(max_cycles=cycles)
        L = bot.ledger
        orders = L._exec("SELECT id, status, side FROM orders").fetchall()
        fills = L._exec("SELECT * FROM fills").fetchall()
        decided = {json.loads(r["payload"]).get("order") for r in L.decisions(kind="risk", limit=10**6)}
        trades = L.trades()
        curve = L.equity_curve()

        checks.append(Check("replay session ran", bot.cycles == cycles, f"{bot.cycles} cycles, ledger mode={L.mode}"))
        checks.append(Check("paper orders filled", sum(1 for o in orders if o["status"] == "filled") > 0,
                            f"{sum(1 for o in orders if o['status']=='filled')} filled / {sum(1 for o in orders if o['status']=='rejected')} rejected"))
        missing = [o["id"] for o in orders if o["id"] not in decided]
        checks.append(Check("every order has a risk-engine decision", not missing, f"{len(orders)} orders, {len(missing)} without decision"))
        bad_fee = [f for f in fills if f["fee_usd"] <= 0 or f["slippage_bps"] < 0 or f["slippage_bps"] > s.execution.paper_slippage_bps]
        checks.append(Check("fills carry fee and bounded slippage", not bad_fee, f"{len(fills)} fills, {len(bad_fee)} out of bounds"))
        cap = s.risk.max_total_exposure_pct / 100
        worst = max(((e.exposure_usd / e.equity_usd) for e in curve if e.equity_usd > 0), default=0.0)
        checks.append(Check("total exposure cap held at entry", worst <= cap * 1.25, f"peak exposure/equity {worst:.3f} (cap {cap:.2f} + price drift)"))
        checks.append(Check("open positions within limit", len(L.load_positions()) <= s.risk.max_open_positions,
                            f"{len(L.load_positions())} open (limit {s.risk.max_open_positions})"))
        pnl_sum = sum(t.pnl_usd for t in trades)
        cash = L.get("cash")
        exposure_cost = sum(p.cost_basis_usd + p.fees_usd for p in L.load_positions().values())
        recon = abs((cash + exposure_cost) - (1000.0 + pnl_sum))
        checks.append(Check("cash + cost basis reconciles with initial capital + realized P&L", recon < 0.05,
                            f"difference ${recon:.4f} over {len(trades)} closed trades"))
        checks.append(Check("equity curve and daily reports written", len(curve) > 0 and any(e["kind"] == "daily_report" for e in L.events(10**6)),
                            f"{len(curve)} equity points"))
        # kill switch: trip, run a few cycles, expect flatten + no buys afterwards
        buys_before = L.count_buy_fills_since(0)
        bot.kill.trip("selftest", source="selftest")
        bot.run_forever(max_cycles=3)
        flattened = L.load_positions() == {}
        checks.append(Check("kill switch flattens and blocks new entries", flattened and L.count_buy_fills_since(0) == buys_before,
                            f"positions after trip: {len(L.load_positions())}, new buys: {L.count_buy_fills_since(0) - buys_before}"))
        checks.append(Check("live gate disarmed", not bot.s.armed_file.exists() and bot.s.mode == "paper", "paper mode, no armed file"))
        L.close()
    return checks


def format_checks(checks: list[Check]) -> str:
    lines = ["=== tradebot self-test (offline, synthetic data) ==="]
    for c in checks:
        lines.append(f"{'PASS' if c.ok else 'FAIL'}  {c.name:<60} {c.detail}")
    lines.append(f"result: {'OPERATIONAL' if all(c.ok for c in checks) else 'FAILED'} ({sum(c.ok for c in checks)}/{len(checks)} checks)")
    return "\n".join(lines)
