"""Performance report from the lab ledger: net results after all modelled costs, risk, rejection funnel, failure rates,
breakdowns, a naive benchmark and the shadow AI comparison. Every figure is a PAPER SIMULATION."""
from __future__ import annotations

import json
import math
import random
import statistics
from collections import Counter, defaultdict

from .config import LabConfig
from .costs import buy_fill, sell_fill
from .ledger import LabLedger


def bootstrap_ci(xs: list[float], n_boot: int = 4000, alpha: float = 0.05, seed: int = 0) -> list[float] | None:
    if len(xs) < 5:
        return None
    rng = random.Random(seed)
    means = sorted(sum(rng.choice(xs) for _ in xs) / len(xs) for _ in range(n_boot))
    return [means[int(n_boot * alpha / 2)], means[int(n_boot * (1 - alpha / 2)) - 1]]


def trade_stats(pnl_pct: list[float], pnl_usd: list[float], alpha: float = 0.05) -> dict:
    n = len(pnl_pct)
    if n == 0:
        return {"n": 0}
    wins = [x for x in pnl_usd if x > 0]
    losses = [x for x in pnl_usd if x <= 0]
    bps = [x * 1e4 for x in pnl_pct]
    sd = statistics.pstdev(bps) if n > 1 else 0.0
    return {"n": n, "net_usd": round(sum(pnl_usd), 2), "win_rate": round(len(wins) / n, 3), "loss_rate": round(len(losses) / n, 3),
            "profit_factor": round(sum(wins) / -sum(losses), 2) if sum(losses) < 0 else None,
            "expectancy_usd": round(sum(pnl_usd) / n, 3), "expectancy_bps": round(sum(bps) / n, 1),
            "expectancy_ci_bps": [round(v, 1) for v in ci] if (ci := bootstrap_ci(bps, alpha=alpha)) else None,
            "per_trade_sharpe": round(sum(bps) / n / sd, 3) if sd else None,
            "avg_win_usd": round(sum(wins) / len(wins), 3) if wins else None,
            "avg_loss_usd": round(sum(losses) / len(losses), 3) if losses else None}


def max_drawdown(equity: list[float]) -> float:
    peak, mdd = -math.inf, 0.0
    for e in equity:
        peak = max(peak, e)
        mdd = max(mdd, peak - e)
    return round(mdd, 2)


def naive_benchmark(cfg: LabConfig, led: LabLedger) -> dict:
    """Buy EVERY security-approved opportunity at the next observation, sell at the first observation after
    max_hold (or the last one), same cost model, same size. Tells us whether strategy selection adds anything."""
    ex = cfg.execution
    rows = led.x("SELECT DISTINCT a.mint, o.ts FROM assessments a JOIN opportunities o ON o.id=a.opportunity_id "
                 "WHERE a.approved=1 ORDER BY o.ts").fetchall()
    seen, res = set(), []
    for r in rows:
        if r["mint"] in seen:
            continue
        seen.add(r["mint"])
        snaps = led.x("SELECT payload FROM snapshots WHERE mint=? AND observed_ts>? ORDER BY observed_ts", (r["mint"], r["ts"])).fetchall()
        snaps = [json.loads(s["payload"]) for s in snaps]
        if len(snaps) < 2 or not snaps[0].get("price_usd"):
            continue
        entry = snaps[0]
        exit_ = next((s for s in snaps if s["observed_ts"] - entry["observed_ts"] >= cfg.exits.max_hold_min * 60), snaps[-1])
        if not exit_.get("price_usd"):
            continue
        size = cfg.risk.position_usd
        qty, _, _ = buy_fill(size, entry["price_usd"], entry.get("liquidity_usd"), entry.get("dex_id", ""), ex)
        proceeds, _ = sell_fill(qty, exit_["price_usd"], exit_.get("liquidity_usd"), exit_.get("dex_id", ""), ex)
        res.append((proceeds - size) / size)
    return trade_stats(res, [x * cfg.risk.position_usd for x in res]) | {"description": "buy every approved opportunity, hold max_hold"}


def ai_comparison(trades) -> dict:
    out = {}
    for name in ("claude", "grok"):
        scored = []
        for t in trades:
            sc = (json.loads(t["payload"] or "{}").get("ai_scores") or {}).get(name)
            if isinstance(sc, dict) and "score" in sc:
                scored.append((sc["score"], t["pnl_pct"], t["pnl_usd"]))
        if not scored:
            continue
        liked = [(p, u) for s, p, u in scored if s >= 60]
        out[name] = {"scored_trades": len(scored), "all": trade_stats([p for _, p, _ in scored], [u for _, _, u in scored]),
                     "ai_score_ge_60": trade_stats([p for p, _ in liked], [u for _, u in liked]),
                     "note": "shadow: the AI never changed a decision; this is the subset it would have kept"}
    return out


def build_report(cfg: LabConfig, led: LabLedger, ground_truth: dict | None = None) -> dict:
    trades = led.trades()
    pct, usd = [t["pnl_pct"] for t in trades], [t["pnl_usd"] for t in trades]
    eq = [r["equity"] for r in led.x("SELECT equity FROM equity ORDER BY ts").fetchall()]
    rep = {"evidence_class": "PAPER SIMULATION (not real-money results)", "overall": trade_stats(pct, usd),
           "max_drawdown_usd": max_drawdown(eq) if eq else 0.0,
           "final_equity": round(eq[-1], 2) if eq else cfg.risk.paper_capital_usd}
    n_opp = led.count("opportunities")
    n_elig = led.count("opportunities", "eligible=1")
    n_ass = led.count("assessments")
    n_appr = led.count("assessments", "approved=1")
    n_prop = led.count("proposals")
    n_acc = led.count("proposals", "accept=1")
    n_ent = led.count("intents", "side='buy'")
    n_fill = led.count("intents", "side='buy' AND state='filled'")
    n_fail = led.count("intents", "side='buy' AND state='failed'")
    rep["funnel"] = {"opportunities": n_opp, "eligible": n_elig, "assessed": n_ass, "security_approved": n_appr,
                     "proposals": n_prop, "accepted": n_acc, "entries": n_ent, "filled": n_fill, "failed_entries": n_fail,
                     "rejection_rate": round(1 - n_fill / n_opp, 4) if n_opp else None,
                     "failed_entry_rate": round(n_fail / n_ent, 3) if n_ent else None}
    flags = Counter()
    for r in led.x("SELECT flags FROM assessments WHERE approved=0").fetchall():
        for f in json.loads(r["flags"]):
            if f["severity"] in ("critical", "high"):
                flags[f["code"]] += 1
    rep["security_rejections_by_flag"] = dict(flags.most_common())
    basis = Counter()
    for r in led.x("SELECT flags FROM assessments").fetchall():
        b = next((f["detail"] for f in json.loads(r["flags"]) if f.get("code") == "decision_basis"), "not_recorded")
        basis[b] += 1
    rep["security_decision_basis"] = dict(basis)   # evidence | missing_data_only | approved | not_recorded (older rows)
    reasons = Counter()
    for r in led.x("SELECT payload FROM proposals WHERE accept=0").fetchall():
        for x in json.loads(r["payload"]).get("reasons", []):
            reasons[x.split(":")[0].split(" ")[0]] += 1
    rep["analyst_rejections"] = dict(reasons.most_common())
    rep["exit_reasons"] = dict(Counter(t["exit_reason"] for t in trades))
    liq = [t for t in trades if t["exit_reason"] in ("liquidity_deterioration", "unsellable_writeoff")]
    rep["liquidity_related"] = {"trades": len(liq), "pnl_usd": round(sum(t["pnl_usd"] for t in liq), 2),
                                "writeoffs": sum(1 for t in trades if t["exit_reason"] == "unsellable_writeoff")}
    by = defaultdict(lambda: defaultdict(list))
    for t in trades:
        meta = json.loads(t["payload"] or "{}")
        liq_b = "<100k" if (meta.get("exit_liquidity") or 0) < 1e5 else ("100k-1M" if (meta.get("exit_liquidity") or 0) < 1e6 else ">1M")
        for dim, key in (("strategy", t["strategy"]), ("exit_reason", t["exit_reason"]), ("liquidity_bucket", liq_b)):
            by[dim][key].append((t["pnl_pct"], t["pnl_usd"]))
        if ground_truth:
            by["synthetic_archetype"][ground_truth.get(t["mint"], "?")].append((t["pnl_pct"], t["pnl_usd"]))
    rep["breakdown"] = {d: {k: trade_stats([a for a, _ in v], [b for _, b in v]) for k, v in g.items()} for d, g in by.items()}
    rep["benchmark_naive_basket"] = naive_benchmark(cfg, led)
    rep["benchmark_cash"] = {"net_usd": 0.0, "note": "holding USDC; no SOL/BTC benchmark series is tracked by the lab yet"}
    rep["ai_shadow"] = ai_comparison(trades)
    return rep


def format_report(rep: dict) -> str:
    o, f = rep["overall"], rep["funnel"]
    lines = [f"{rep['evidence_class']}",
             f"trades {o.get('n', 0)}  net ${o.get('net_usd', 0)}  expectancy {o.get('expectancy_bps')} bps "
             f"CI95 {o.get('expectancy_ci_bps')}  win {o.get('win_rate')}  PF {o.get('profit_factor')}  "
             f"maxDD ${rep['max_drawdown_usd']}  equity ${rep['final_equity']}",
             f"funnel: {f}",
             f"security rejections: {rep['security_rejections_by_flag']}",
             f"security decision basis: {rep.get('security_decision_basis')}",
             f"analyst rejections: {rep['analyst_rejections']}",
             f"exits: {rep['exit_reasons']}  liquidity-related: {rep['liquidity_related']}",
             f"naive basket benchmark: n={rep['benchmark_naive_basket'].get('n')} expectancy "
             f"{rep['benchmark_naive_basket'].get('expectancy_bps')} bps CI {rep['benchmark_naive_basket'].get('expectancy_ci_bps')}"]
    for dim, g in rep["breakdown"].items():
        lines.append(f"by {dim}: " + "; ".join(f"{k}: n={v['n']} {v.get('expectancy_bps')} bps" for k, v in g.items()))
    if rep["ai_shadow"]:
        lines.append(f"AI shadow: {json.dumps(rep['ai_shadow'])}")
    return "\n".join(lines)
