"""Independent audit of strategy families across horizons on identical data and identical cost assumptions.

Data: Binance spot 1-minute klines (BTCUSDT, ETHUSDT, SOLUSDT), verified minute-for-minute against official
data.binance.vision files (research_lab/results/data_audit.json), resampled to 1m / 5m / 15m / 1h / 4h / 1d.

Method (identical for every strategy and horizon):
  - rolling walk-forward: 9-month train, 3-month test, step 3 months; parameters chosen on train ONLY, at the
    base cost with one bar of extra execution delay; test windows are stitched into one out-of-sample record;
  - fills at the next bar's open (signal at bar close), plus `delay` extra bars; stops checked intrabar before
    take-profit; whole capital per trade (per-trade metrics do not depend on sizing);
  - costs per side (fee + spread + price impact + slippage), plus the Solana network fee on Jupiter scenarios
    ($0.01 per transaction on a $100 position = 1 bp):
        zero 0 (diagnostic only: gross edge) | cex_taker 12 | jup_optimistic 10+1 | jup_base 25+1 | jup_stress 50+1
  - bootstrap (5,000 resamples of OOS trades) for 95% confidence intervals of expectancy, and a trade-sequence
    Monte Carlo for the distribution of total return and drawdown. These are SCENARIO SIMULATIONS of historical
    trades, not observed trading results.

PRE-REGISTERED ACCEPTANCE (written before the results; docs/STRATEGY_AUDIT_GO_NOGO.md):
  at jup_base with delay 1, pooled over the three assets:  OOS trades >= 30, expectancy 95% CI lower bound > 0,
  profit factor >= 1.2, positive expectancy in >= 2 of 3 assets, and expectancy still > 0 at jup_stress.

usage (from TRADEBOT/): PYTHONPATH=$PWD python -m research_lab.strategy_audit --root <data_1m dir> --out research_lab/results/strategy_audit.json
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import time

import numpy as np
import pandas as pd

from . import indicators as I
from .engine import ExitSpec, compute_metrics, run_backtest
from .intraday import load_1m, resample

HORIZONS = {"1m": ("1min", 1), "5m": ("5min", 5), "15m": ("15min", 15), "1h": ("1h", 60), "4h": ("4h", 240), "1d": ("1D", 1440)}
COSTS = {"zero": 0.0, "cex_taker": 12.0, "jup_optimistic": 11.0, "jup_base": 26.0, "jup_stress": 51.0}
BASE, STRESS = "jup_base", "jup_stress"
WARMUP_BARS = 250


# --------------------------------------------------------------------------- strategy families (bar-based, horizon-agnostic)
def breakout(df, p):
    c = df["close"]
    hi = df["high"].rolling(p["n"]).max().shift(1)
    lo = df["low"].rolling(max(2, p["n"] // 2)).min().shift(1)
    return c > hi, c < lo, ExitSpec(atr_stop_mult=3.0)


def trend_ema(df, p):
    c = df["close"]
    f, s, t = I.ema(c, p["fast"]), I.ema(c, p["slow"]), I.ema(c, 200)
    return (f > s) & (f.shift(1) <= s.shift(1)) & (c > t), (f < s), ExitSpec(atr_stop_mult=3.0)


def dip_reversion(df, p):
    c = df["close"]
    r = c.pct_change()
    z = (r - r.rolling(60).mean()) / r.rolling(60).std()
    return z < -p["z"], c > c.rolling(20).mean(), ExitSpec(atr_stop_mult=3.0, max_hold_bars=p["hold"])


def tsmom_ema(df, p):
    """The deployed 4h rule: daily evaluation, 30-day momentum > 0 and close above the 100-day EMA."""
    bar_min = (df.index[1] - df.index[0]).total_seconds() / 60
    bpd = max(1, int(round(1440 / bar_min)))
    c = df["close"]
    mom = c / c.shift(30 * bpd) - 1
    e = I.ema(c, 100 * bpd)
    daily = pd.Series(df.index.hour == 0, index=df.index) if bpd > 1 else pd.Series(True, index=df.index)
    return (mom > 0) & (c > e) & daily, ((mom < 0) | (c < e)) & daily, ExitSpec(stop_loss_pct=25.0)


FAMILIES = {
    "breakout": (breakout, [{"n": 20}, {"n": 55}], list(HORIZONS)),
    "trend_ema": (trend_ema, [{"fast": 10, "slow": 30}, {"fast": 20, "slow": 60}], list(HORIZONS)),
    "dip_reversion": (dip_reversion, [{"z": 2.5, "hold": 10}, {"z": 3.5, "hold": 10}], list(HORIZONS)),
    "tsmom_ema_deployed": (tsmom_ema, [{}], ["4h", "1d"]),
}


# --------------------------------------------------------------------------- engine wrappers
def run(fn, df, p, cost, delay, bar_min, warmup):
    entries, exits, spec = fn(df, p)
    entries = entries.fillna(False).astype(bool)
    exits = exits.fillna(False).astype(bool)
    if delay:
        entries = entries.shift(delay).fillna(False).astype(bool)
        exits = exits.shift(delay).fillna(False).astype(bool)
    entries.iloc[:warmup] = False
    return run_backtest(df, entries, exits, spec, fee_bps=cost, slippage_bps=0.0, initial=10_000.0, size_frac=1.0, bar_minutes=bar_min)


def warmup_for(fn, bar_min: int) -> int:
    """Bars of history each test window needs before its first trade (indicator warm-up, never traded)."""
    if fn is tsmom_ema:
        return int(110 * max(1, round(1440 / bar_min)))        # 100-day EMA + margin
    return WARMUP_BARS


def walk_forward(fn, grid, df, bar_min, start_oos, end, delay_sel=1):
    """-> list of (test_start, test_end, chosen params)"""
    plan, t = [], pd.Timestamp(start_oos)
    while t < end:
        t_end = min(t + pd.DateOffset(months=3), end)
        train = df[(df.index >= t - pd.DateOffset(months=9)) & (df.index < t)]
        best, best_s = grid[0], -1e18
        if len(grid) > 1 and len(train) > WARMUP_BARS * 2:
            for p in grid:
                r = run(fn, train, p, COSTS[BASE], delay_sel, bar_min, warmup_for(fn, bar_min))
                m = compute_metrics(r, 10_000.0, bar_min)
                if m["trades"] < 10:
                    continue
                s = m["net_return_pct"] - 0.5 * abs(m["max_drawdown_pct"])
                if s > best_s:
                    best, best_s = p, s
        plan.append((t, t_end, best))
        t = t_end
    return plan


def oos_trades(fn, plan, df, bar_min, cost, delay):
    out = []
    w = warmup_for(fn, bar_min)
    for t0, t1, p in plan:
        pos = df.index.searchsorted(t0)
        seg = df.iloc[max(0, pos - w): df.index.searchsorted(t1)]
        if len(seg) <= min(w, pos) + 2:
            continue
        r = run(fn, seg, p, cost, delay, bar_min, min(w, pos))
        out += [t for t in r.trades if seg.index[t.entry_i] >= t0]
    return out


# --------------------------------------------------------------------------- statistics
def stats(rets: np.ndarray, days: float, rng: np.random.Generator) -> dict:
    n = len(rets)
    if n == 0:
        return {"n": 0}
    wins, losses = rets[rets > 0], rets[rets <= 0]
    pf = wins.sum() / -losses.sum() if losses.sum() < 0 else float("inf")
    boot = rng.choice(rets, size=(5000, n), replace=True).mean(axis=1) if n > 1 else np.array([rets[0]])
    eq = np.cumprod(1 + rets / 100)
    dd = float((eq / np.maximum.accumulate(eq) - 1).min() * 100)
    # trade-sequence Monte Carlo: reshuffled/resampled order -> distribution of total return and drawdown
    sims = rng.choice(rets, size=(2000, n), replace=True) / 100
    eqs = np.cumprod(1 + sims, axis=1)
    sim_ret = (eqs[:, -1] - 1) * 100
    sim_dd = ((eqs / np.maximum.accumulate(eqs, axis=1)) - 1).min(axis=1) * 100
    return {"n": n, "trades_per_day": round(n / days, 3), "win_rate_pct": round(len(wins) / n * 100, 1),
            "expectancy_bps": round(float(rets.mean()) * 100, 1),
            "expectancy_ci95_bps": [round(float(np.percentile(boot, 2.5)) * 100, 1), round(float(np.percentile(boot, 97.5)) * 100, 1)],
            "profit_factor": round(float(pf), 2) if math.isfinite(pf) else None, "avg_win_bps": round(float(wins.mean()) * 100, 1) if len(wins) else None,
            "avg_loss_bps": round(float(losses.mean()) * 100, 1) if len(losses) else None,
            "total_return_pct": round(float((eq[-1] - 1) * 100), 1), "max_drawdown_pct": round(dd, 1),
            "mc_total_return_p5_p50_p95": [round(float(np.percentile(sim_ret, q)), 1) for q in (5, 50, 95)],
            "mc_max_drawdown_p50_p95": [round(float(np.percentile(sim_dd, 50)), 1), round(float(np.percentile(sim_dd, 5)), 1)],
            "mc_prob_loss_pct": round(float((sim_ret < 0).mean()) * 100, 1)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", default="research_lab/results/strategy_audit.json")
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT")
    ap.add_argument("--horizons", default=",".join(HORIZONS))
    a = ap.parse_args()
    rng = np.random.default_rng(20261102)
    data: dict[str, pd.DataFrame] = {}
    for sym in a.symbols.split(","):
        files = glob.glob(f"{a.root}/parshant231234-eng_{sym}-Historical-Data/1m/*/*/*.csv")
        data[sym] = load_1m(files)
        print(f"{sym}: {len(data[sym])} 1m bars {str(data[sym].index[0])[:10]} .. {str(data[sym].index[-1])[:10]}", flush=True)
    results: dict = {"costs_bps_per_side": COSTS, "acceptance": "jup_base, delay 1: n>=30, CI low>0, PF>=1.2, >=2/3 assets positive, jup_stress expectancy>0",
                     "rows": []}
    for hz in a.horizons.split(","):
        rule, bar_min = HORIZONS[hz]
        frames = {s: (resample(d, rule) if rule != "1min" else d) for s, d in data.items()}
        for fam, (fn, grid, horizons) in FAMILIES.items():
            if hz not in horizons:
                continue
            t0 = time.time()
            per_sym: dict = {}
            for sym, df in frames.items():
                start_oos = df.index[0] + pd.DateOffset(months=9)
                plan = walk_forward(fn, grid, df, bar_min, start_oos, df.index[-1])
                days = (df.index[-1] - start_oos).days
                per_sym[sym] = {"days": days, "plan": [(str(x)[:10], str(y)[:10], p) for x, y, p in plan], "trades": {}}
                for cost_name, cost in COSTS.items():
                    for delay in (0, 1):
                        tr = oos_trades(fn, plan, df, bar_min, cost, delay)
                        per_sym[sym]["trades"][f"{cost_name}|d{delay}"] = np.array([t.ret_pct for t in tr])
            row = {"family": fam, "horizon": hz, "scenarios": {}}
            total_days = sum(v["days"] for v in per_sym.values()) / len(per_sym)
            for key in per_sym[next(iter(per_sym))]["trades"]:
                pooled = np.concatenate([per_sym[s]["trades"][key] for s in per_sym])
                sc = stats(pooled, total_days, rng)
                sc["per_asset_expectancy_bps"] = {s: (round(float(per_sym[s]["trades"][key].mean()) * 100, 1) if len(per_sym[s]["trades"][key]) else None) for s in per_sym}
                row["scenarios"][key] = sc
            b = row["scenarios"][f"{BASE}|d1"]
            st = row["scenarios"][f"{STRESS}|d1"]
            pos_assets = sum(1 for v in b.get("per_asset_expectancy_bps", {}).values() if v is not None and v > 0)
            checks = {"n>=30": b.get("n", 0) >= 30, "ci_low>0": b.get("n", 0) > 1 and b["expectancy_ci95_bps"][0] > 0,
                      "pf>=1.2": (b.get("profit_factor") or 0) >= 1.2, ">=2/3 assets": pos_assets >= 2,
                      "stress>0": st.get("n", 0) > 0 and st["expectancy_bps"] > 0}
            row["acceptance_checks"] = checks
            row["accepted"] = all(checks.values())
            row["params_by_window"] = {s: v["plan"] for s, v in per_sym.items()}
            results["rows"].append(row)
            g = row["scenarios"]["zero|d0"]
            print(f"{hz:>3} {fam:<20} base|d1: n={b.get('n')} exp={b.get('expectancy_bps')}bps CI={b.get('expectancy_ci95_bps')} PF={b.get('profit_factor')} "
                  f"| gross exp={g.get('expectancy_bps')}bps | {'ACCEPT' if row['accepted'] else 'reject'} ({time.time() - t0:.0f}s)", flush=True)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w") as f:
        json.dump(results, f, indent=1, default=str)


if __name__ == "__main__":
    main()
