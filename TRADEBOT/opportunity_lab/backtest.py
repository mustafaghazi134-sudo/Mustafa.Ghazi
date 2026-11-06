"""Validation harness. Runs the SAME four agents and paper executor over simulated time:

  replay       recorded observations (the lab's own ledger or JSONL), point-in-time, failed tokens included
  synthetic    Monte Carlo over many synthetic markets; `null` markets have no edge by construction

Walk-forward protocol for replay data (pre-registered in docs/OPPORTUNITY_LAB_RESULTS.md):
  * the recorded time range is split chronologically: development 50%, validation 25%, holdout 25%;
  * every configuration variant in the grid runs on development; the best by expectancy with at least
    `min_trades` trades is chosen; it is then run once on validation and once on holdout;
  * each holdout run is appended to data/lab/holdout_looks.jsonl (a holdout looked at twice is no longer a holdout);
  * confidence intervals use a Bonferroni-corrected alpha = 0.05 / number of variants;
  * every run is repeated at 1x, 2x and 3x costs, and with a fraction of tokens that cannot be sold.
Acceptance (a strategy is only "promising", never "proven"): validation AND holdout expectancy CI lower bound > 0 at
2x costs, profit factor >= 1.3, at least 30 trades in each window, and better than the naive basket.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import time
from pathlib import Path

from .config import LabConfig
from .engine import LabEngine
from .ledger import LabLedger
from .models import RouteCheck
from .report import bootstrap_ci, build_report, trade_stats
from .sources.replay import ReplayMarket
from .sources.synthetic import SimClock, SyntheticMarket

DEFAULT_GRID = [
    {"name": "base", "exits": {}},
    {"name": "tight", "exits": {"stop_loss_pct": 8.0, "take_profit_pct": 20.0, "max_hold_min": 120.0}},
    {"name": "wide", "exits": {"stop_loss_pct": 20.0, "take_profit_pct": 50.0, "max_hold_min": 480.0}},
    {"name": "trail_only", "exits": {"take_profit_pct": 1000.0, "trailing_stop_pct": 8.0, "trailing_activation_pct": 5.0}},
]


class UnsellableInjector:
    """Route wrapper: a deterministic fraction of tokens lose their SELL route once held (rug / freeze / honeypot
    switched on after entry). Buys and the entry-time security check still pass, which is the realistic failure."""

    def __init__(self, inner, rate: float, seed: int = 0):
        self.inner, self.rate, self.seed = inner, rate, seed
        self.calls: dict[str, int] = {}

    def _doomed(self, mint: str) -> bool:
        h = int(hashlib.sha256(f"{self.seed}:{mint}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        return h < self.rate

    def route_check(self, mint: str, size_usd: float) -> RouteCheck:
        n = self.calls.get(mint, 0)
        self.calls[mint] = n + 1
        if n >= 2 and self._doomed(mint):          # passes the security check and the entry fill, then disappears
            return RouteCheck(ok=False, buy_out_raw=1, source="injected", error="no sell route (injected)")
        return self.inner.route_check(mint, size_usd)


def _cfg_variant(base: LabConfig, variant: dict, data_dir: Path, cost_mult: float) -> LabConfig:
    d = json.loads(base.model_dump_json())
    d["data_dir"] = str(data_dir)
    d["main_kill_file"] = str(data_dir / "NO_MAIN_KILL_IN_BACKTEST")
    for sec, vals in variant.items():
        if sec != "name":
            d[sec].update(vals)
    d["execution"]["cost_multiplier"] = cost_mult
    cfg = LabConfig.model_validate(d)
    cfg.data_path.mkdir(parents=True, exist_ok=True)
    return cfg


def run_window(base: LabConfig, variant: dict, market_factory, start: float, end: float, cost_mult: float = 1.0,
               unsellable_rate: float = 0.0, seed: int = 0, keep_dir: Path | None = None) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="labbt_")) if keep_dir is None else keep_dir
    try:
        cfg = _cfg_variant(base, variant, tmp, cost_mult)
        clock = SimClock(start)
        market, facts, routes, truth = market_factory(clock)
        if unsellable_rate:
            routes = UnsellableInjector(routes, unsellable_rate, seed)
        eng = LabEngine(cfg, [market], facts, routes, clock=clock, lock=False)
        eng.run(minutes=(end - start) / 60, advance=clock.advance)
        eng.finish()
        rep = build_report(cfg, eng.ledger, truth)
        trades = eng.ledger.trades()
        rep["_pnl"] = [(t["pnl_pct"], t["pnl_usd"]) for t in trades]
        eng.close()
        return rep
    finally:
        if keep_dir is None:
            shutil.rmtree(tmp, ignore_errors=True)


def _stats_alpha(pnl: list, alpha: float) -> dict:
    s = trade_stats([p for p, _ in pnl], [u for _, u in pnl], alpha=alpha)
    return s


def walk_forward_replay(base: LabConfig, ledger_path: str, grid: list[dict] | None = None, min_trades: int = 30,
                        holdout_log: Path | None = None) -> dict:
    grid = grid or DEFAULT_GRID
    led = LabLedger(ledger_path)
    r = led.x("SELECT MIN(observed_ts) a, MAX(observed_ts) b FROM snapshots").fetchone()
    led.close()
    if r["a"] is None:
        return {"error": "no recorded snapshots in " + ledger_path}
    a, b = r["a"], r["b"]
    cuts = [a, a + 0.5 * (b - a), a + 0.75 * (b - a), b]
    names = ["development", "validation", "holdout"]
    alpha = 0.05 / len(grid)

    def factory_for(s, e):
        def f(clock):
            m = ReplayMarket.from_ledger(clock, ledger_path, s - 6 * 3600, e)
            return m, m, m, None
        return f

    out = {"range": [a, b], "windows": dict(zip(names, zip(cuts[:-1], cuts[1:]))), "alpha": alpha, "dev": {}, "selected": None}
    for v in grid:
        rep = run_window(base, v, factory_for(cuts[0], cuts[1]), cuts[0], cuts[1])
        out["dev"][v["name"]] = _stats_alpha(rep["_pnl"], alpha)
    eligible = {k: s for k, s in out["dev"].items() if s.get("n", 0) >= min_trades}
    if not eligible:
        out["selected"] = None
        out["verdict"] = f"NO CANDIDATE: no variant reached {min_trades} development trades"
        return out
    best = max(eligible, key=lambda k: eligible[k]["expectancy_bps"])
    out["selected"] = best
    variant = next(v for v in grid if v["name"] == best)
    for wname, (s, e) in (("validation", (cuts[1], cuts[2])), ("holdout", (cuts[2], cuts[3]))):
        res = {}
        for mult in (1.0, 2.0, 3.0):
            rep = run_window(base, variant, factory_for(s, e), s, e, cost_mult=mult)
            res[f"cost_x{mult:g}"] = _stats_alpha(rep["_pnl"], alpha) | {"naive": rep["benchmark_naive_basket"]}
        rep = run_window(base, variant, factory_for(s, e), s, e, unsellable_rate=0.05)
        res["unsellable_5pct"] = _stats_alpha(rep["_pnl"], alpha)
        out[wname] = res
        if wname == "holdout" and holdout_log:
            holdout_log.parent.mkdir(parents=True, exist_ok=True)
            with open(holdout_log, "a") as f:
                f.write(json.dumps({"ts": time.time(), "ledger": ledger_path, "variant": best, "window": [s, e]}) + "\n")
    ok = all(out[w]["cost_x2"].get("n", 0) >= min_trades and (out[w]["cost_x2"].get("expectancy_ci_bps") or [-1])[0] > 0
             and (out[w]["cost_x2"].get("profit_factor") or 0) >= 1.3 for w in ("validation", "holdout"))
    out["verdict"] = "PROMISING (forward paper test next)" if ok else "REJECTED by the pre-registered criteria"
    return out


def synthetic_monte_carlo(base: LabConfig, seeds: list[int], null_edge: bool, hours: float = 24.0, n_tokens: int = 80,
                          cost_mult: float = 1.0, unsellable_rate: float = 0.0) -> dict:
    per_seed, pooled = [], []
    trap_trades = 0
    for sd in seeds:
        def factory(clock, sd=sd):
            m = SyntheticMarket(clock, seed=sd, n_tokens=n_tokens, hours=hours, null_edge=null_edge)
            return m, m, m, m.ground_truth
        rep = run_window(base, {"name": "base"}, factory, 1_800_000_000.0, 1_800_000_000.0 + hours * 3600, cost_mult=cost_mult,
                         unsellable_rate=unsellable_rate, seed=sd)
        pooled += rep["_pnl"]
        arch = rep["breakdown"].get("synthetic_archetype", {})
        trap_trades += sum(v["n"] for k, v in arch.items() if k in ("honeypot", "mint_live", "freeze_live", "fee_trap"))
        per_seed.append({"seed": sd, "n": rep["overall"].get("n", 0), "net_usd": rep["overall"].get("net_usd", 0.0),
                         "funnel": rep["funnel"], "archetypes": {k: v["n"] for k, v in arch.items()}})
    bps = [p * 1e4 for p, _ in pooled]
    return {"null_edge": null_edge, "seeds": len(seeds), "cost_multiplier": cost_mult, "unsellable_rate": unsellable_rate,
            "pooled": trade_stats([p for p, _ in pooled], [u for _, u in pooled]),
            "seed_net_usd_ci": bootstrap_ci([s["net_usd"] for s in per_seed]) if len(per_seed) >= 5 else None,
            "trades_on_trap_tokens": trap_trades, "per_seed": per_seed,
            "mean_bps": round(sum(bps) / len(bps), 1) if bps else None,
            "note": "synthetic: tests mechanics and bias controls only; not evidence about real markets"}
