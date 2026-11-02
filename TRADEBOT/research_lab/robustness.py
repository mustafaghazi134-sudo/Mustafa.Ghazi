"""Robustness checks for the strategies that came out of research_lab/strategy_audit.py looking profitable.

The audit compared 20 family x horizon combinations, so one passing its 95% test is roughly what chance alone
produces. This script re-creates the exact out-of-sample (OOS) trades of the audit (same data, walk-forward
plans, costs and delays) and asks the questions a sceptical reviewer would:

  1. multiple testing: bootstrap interval at the Bonferroni level for 20 tests (99.75% two-sided);
  2. concentration: expectancy after removing the best 5 trades and the best 10% of trades;
  3. stability in time and across assets: expectancy by calendar year of entry and by asset;
  4. execution: one, two and three bars of extra delay, and the break-even cost per side;
  5. benchmark: an equal-weight three-asset portfolio (1/3 of capital per asset, mark-to-market every bar)
     against buying and holding the same three assets over the same OOS windows: return, max drawdown,
     time in market, return/|max DD|.

Everything here is a backtest of historical prices (no observed trades).

usage (from TRADEBOT/): PYTHONPATH=$PWD python -m research_lab.robustness --root <data_1m dir> --out research_lab/results/robustness.json
"""
from __future__ import annotations

import argparse
import glob
import json

import numpy as np
import pandas as pd

from .intraday import load_1m, resample
from .strategy_audit import BASE, COSTS, FAMILIES, HORIZONS, STRESS, oos_trades, run, walk_forward, warmup_for

CANDIDATES = [("trend_ema", "4h"), ("tsmom_ema_deployed", "4h"), ("tsmom_ema_deployed", "1d"), ("breakout", "4h")]
N_TESTS = 20


def boot_ci(x: np.ndarray, rng, lo_pct: float, hi_pct: float) -> list[float]:
    b = rng.choice(x, size=(20_000, len(x)), replace=True).mean(axis=1)
    return [round(float(np.percentile(b, lo_pct)) * 1e4, 1), round(float(np.percentile(b, hi_pct)) * 1e4, 1)]   # bps


def portfolio(fn, frames, plans, bar_min, cost, delay) -> dict:
    """Mark-to-market equity of 1/3 capital per asset, each asset traded by its own OOS plan; vs equal-weight hold."""
    curves, holds, expo, per_asset = [], [], [], {}
    for sym, df in frames.items():
        plan = plans[sym]
        w = warmup_for(fn, bar_min)
        parts = []
        for t0, t1, p in plan:
            pos = df.index.searchsorted(t0)
            seg = df.iloc[max(0, pos - w): df.index.searchsorted(t1)]
            if len(seg) <= min(w, pos) + 2:
                continue
            r = run(fn, seg, p, cost, delay, bar_min, min(w, pos))
            eq = r.equity[r.equity.index >= t0]
            parts.append(eq / eq.iloc[0])           # each window restarts flat (no position carried across windows)
        g = pd.concat(parts)
        g = g[~g.index.duplicated()]
        # chain window returns into one curve
        rets = g.pct_change().fillna(0.0)
        starts = [p.index[0] for p in parts]
        rets.loc[rets.index.isin(starts)] = 0.0
        curve = (1 + rets).cumprod()
        curves.append(curve.rename(sym))
        px = df["close"].reindex(curve.index)
        holds.append((px / px.iloc[0]).rename(sym))
        expo.append(float((rets != 0).mean()))
        h = px / px.iloc[0]
        per_asset[sym] = {"window": [str(curve.index[0])[:10], str(curve.index[-1])[:10]],
                          "strategy_return_pct": round(float((curve.iloc[-1] - 1) * 100), 1),
                          "strategy_max_dd_pct": round(float((curve / curve.cummax() - 1).min() * 100), 1),
                          "hold_return_pct": round(float((h.iloc[-1] - 1) * 100), 1),
                          "hold_max_dd_pct": round(float((h / h.cummax() - 1).min() * 100), 1)}
    common = curves[0].index
    for c in curves[1:]:
        common = common.intersection(c.index)
    start = max(c.index[0] for c in curves)
    strat = sum(c.reindex(common).loc[start:] / c.reindex(common).loc[start:].iloc[0] for c in curves) / len(curves)
    hold = sum(h.reindex(common).loc[start:] / h.reindex(common).loc[start:].iloc[0] for h in holds) / len(holds)

    def dd(s):
        return float((s / s.cummax() - 1).min() * 100)

    sr, hr = float((strat.iloc[-1] - 1) * 100), float((hold.iloc[-1] - 1) * 100)
    return {"window": [str(strat.index[0])[:10], str(strat.index[-1])[:10]],
            "strategy_return_pct": round(sr, 1), "strategy_max_dd_pct": round(dd(strat), 1),
            "hold_return_pct": round(hr, 1), "hold_max_dd_pct": round(dd(hold), 1),
            "strategy_return_over_dd": round(sr / abs(dd(strat)), 2) if dd(strat) else None,
            "hold_return_over_dd": round(hr / abs(dd(hold)), 2) if dd(hold) else None,
            "per_asset_full_oos": per_asset,
            "time_in_market_pct_by_asset": {s: round(e * 100, 1) for s, e in zip(frames, expo)},
            "strategy_by_year_pct": {str(y): round(float((g.iloc[-1] / g.iloc[0] - 1) * 100), 1) for y, g in strat.groupby(strat.index.year)},
            "hold_by_year_pct": {str(y): round(float((g.iloc[-1] / g.iloc[0] - 1) * 100), 1) for y, g in hold.groupby(hold.index.year)}}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", default="research_lab/results/robustness.json")
    a = ap.parse_args()
    rng = np.random.default_rng(20261102)
    data = {s: load_1m(glob.glob(f"{a.root}/parshant231234-eng_{s}-Historical-Data/1m/*/*/*.csv")) for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT")}
    out = {"note": "historical backtest; not observed trading", "bonferroni_tests": N_TESTS, "candidates": {}}
    for fam, hz in CANDIDATES:
        fn, grid, _ = FAMILIES[fam]
        rule, bar_min = HORIZONS[hz]
        frames = {s: resample(d, rule) for s, d in data.items()}
        plans = {s: walk_forward(fn, grid, df, bar_min, df.index[0] + pd.DateOffset(months=9), df.index[-1]) for s, df in frames.items()}
        tr = {d: [] for d in (1, 2, 3)}
        gross = []
        for s, df in frames.items():
            for d in (1, 2, 3):
                tr[d] += [(s, t) for t in oos_trades(fn, plans[s], df, bar_min, COSTS[BASE], d)]
            gross += [t.ret_pct for t in oos_trades(fn, plans[s], df, bar_min, 0.0, 1)]
        x = np.array([t.ret_pct for _, t in tr[1]]) / 100        # fraction per trade
        xs = np.sort(x)[::-1]
        k10 = max(1, int(round(len(x) * 0.10)))
        lo_b = 100 * (0.05 / N_TESTS) / 2
        res = {"n": len(x), "expectancy_bps": round(float(x.mean()) * 1e4, 1),
               "ci95_bps": boot_ci(x, rng, 2.5, 97.5), f"ci_bonferroni_{N_TESTS}_bps": boot_ci(x, rng, lo_b, 100 - lo_b),
               "share_of_net_from_top5_pct": round(float(xs[:5].sum() / x.sum() * 100), 1) if x.sum() > 0 else None,
               "expectancy_without_top5_bps": round(float(xs[5:].mean()) * 1e4, 1),
               "ci95_without_top5_bps": boot_ci(xs[5:], rng, 2.5, 97.5),
               "expectancy_without_top10pct_bps": round(float(xs[k10:].mean()) * 1e4, 1),
               "by_asset_bps": {s: round(float(np.mean([t.ret_pct for q, t in tr[1] if q == s])) * 100, 1) for s in frames},
               "by_year": {}, "delay_bars_expectancy_bps": {}, "break_even_cost_bps_per_side": None}
        for y in sorted({t.entry_ts.year for _, t in tr[1]}):
            v = np.array([t.ret_pct for _, t in tr[1] if t.entry_ts.year == y]) / 100
            res["by_year"][str(y)] = {"n": len(v), "expectancy_bps": round(float(v.mean()) * 1e4, 1), "sum_pct": round(float(v.sum()) * 100, 1)}
        for d in (1, 2, 3):
            res["delay_bars_expectancy_bps"][str(d)] = round(float(np.mean([t.ret_pct for _, t in tr[d]])) * 100, 1)
        g = float(np.mean(gross)) * 100                           # gross bps per round trip at delay 1
        res["gross_expectancy_bps_delay1"] = round(g, 1)
        res["break_even_cost_bps_per_side"] = round(g / 2, 1)
        res["portfolio_vs_hold_base_cost"] = portfolio(fn, frames, plans, bar_min, COSTS[BASE], 1)
        res["portfolio_vs_hold_stress_cost"] = portfolio(fn, frames, plans, bar_min, COSTS[STRESS], 1)
        out["candidates"][f"{hz} {fam}"] = res
        print(f"{hz} {fam}: n={res['n']} exp={res['expectancy_bps']} CI95={res['ci95_bps']} CI-Bonf={res[f'ci_bonferroni_{N_TESTS}_bps']} "
              f"no-top5={res['expectancy_without_top5_bps']} {res['ci95_without_top5_bps']} no-top10%={res['expectancy_without_top10pct_bps']} "
              f"delays={res['delay_bars_expectancy_bps']} BE/side={res['break_even_cost_bps_per_side']}", flush=True)
        print("   by year:", res["by_year"], flush=True)
        print("   portfolio vs hold (base):", json.dumps(res["portfolio_vs_hold_base_cost"]), flush=True)
    with open(a.out, "w") as f:
        json.dump(out, f, indent=1, default=str)


if __name__ == "__main__":
    main()
