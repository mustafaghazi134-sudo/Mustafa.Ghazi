"""The 2026-08-22 SOL trade: fill-assumption audit, historical analogs, and a Time-Machine-style bootstrap.

Three different kinds of evidence, kept apart:
  A. RECONSTRUCTION of the one replayed trade under alternative, explicit fill assumptions (what the price data permits).
  B. HISTORICAL ANALOGS: every past minute on BTC/ETH/SOL (2023-01 -> 2026-10) where the same rule would have fired
     with a dip large enough to clear the cost gate, traded with the engine's exit rules, after costs. These are
     backtested outcomes of real price paths, not observed trades.
  C. SCENARIO SIMULATION (Time-Machine style): 10,000 synthetic continuations of the Aug-22 setup, each minute drawn
     from the minutes that followed historical analog dips at the same lag (+-0). Probabilities of target-first,
     stop-first, neither. A simulation of history, not a prediction.

usage (from TRADEBOT/): PYTHONPATH=$PWD python -m research_lab.analogs --root <data_1m dir> --out research_lab/results/analogs.json
"""
from __future__ import annotations

import argparse
import glob
import json

import numpy as np
import pandas as pd

from .intraday import load_1m

SIZE_USD = 50.0
NET_FEE_USD = 0.01          # per transaction
STOP_PCT = 1.0
HOLD_BARS = 6               # signal horizon 180 s x max_hold_mult 2
MIN_EDGE_BPS = 20.0
ADVERSE_BPS = 10.0


def pnl(entry: float, exit_: float, cost_bps_leg: float, size=SIZE_USD) -> float:
    qty = size * (1 - cost_bps_leg / 1e4) / entry
    return qty * exit_ * (1 - cost_bps_leg / 1e4) - size - 2 * NET_FEE_USD


def part_a(sol: pd.DataFrame) -> dict:
    w = sol.loc["2026-08-22 05:09":"2026-08-22 05:15"]
    bar = {t.strftime("%H:%M"): {k: float(v) for k, v in r.items()} for t, r in w.iterrows()}
    tp = 93.5888                                   # the replay's take-profit level
    entries = {"replayed fill: 05:11 close (known 05:12:00)": 90.49, "next open 05:12": float(w.loc["2026-08-22 05:12", "open"]),
               "worst inside 05:11 bar (signal at 05:11:00)": float(w.loc["2026-08-22 05:11", "high"]),
               "best inside 05:11 bar": float(w.loc["2026-08-22 05:11", "low"])}
    exits = {"replayed: 05:13 close": 93.73, "take-profit level touched in 05:13 bar": tp,
             "next open 05:14": float(w.loc["2026-08-22 05:14", "open"]), "worst inside 05:13 bar": float(w.loc["2026-08-22 05:13", "low"])}
    grid = []
    for en_name, en in entries.items():
        for ex_name, ex in exits.items():
            row = {"entry": en_name, "entry_px": en, "exit": ex_name, "exit_px": ex}
            for c in (15, 25, 50, 100, 200):
                row[f"net_usd_cost{c}"] = round(pnl(en, ex, c), 3)
            grid.append(row)
    return {"bars": bar, "grid": grid,
            "engine_runs": {"old engine (parametric fill, 30 bps fee + 6-11 bps slippage)": 1.39,
                            "new engine (min-out at 50 bps slippage tolerance + 15 bps/leg, $0.01/tx)": 1.10}}


def analog_trades(df: pd.DataFrame, cost_bps_leg: float, delay: int = 1) -> list[dict]:
    """Engine rule on 1m bars: z<-2.5 dip whose expected move (half the drop) clears worst cost + adverse + min edge."""
    c = df["close"]
    r = c.pct_change()
    z = (r - r.rolling(20).mean().shift(1)) / r.rolling(20).std().shift(1)
    exp_move_bps = (r.abs() * 1e4 * 0.5)
    gate = 2 * cost_bps_leg + 2 * NET_FEE_USD / SIZE_USD * 1e4 + ADVERSE_BPS + MIN_EDGE_BPS
    sig = (z < -2.5) & (r < 0) & (exp_move_bps > gate)
    o, h, l, cl = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    idx = np.where(sig.to_numpy())[0]
    out, busy_until = [], -1
    for i in idx:
        e = i + delay
        if e >= len(df) - HOLD_BARS - 1 or e <= busy_until:
            continue
        entry = o[e]
        tp = entry * (1 + max(exp_move_bps.iloc[i], gate - ADVERSE_BPS - MIN_EDGE_BPS + 10) / 1e4)
        stop = entry * (1 - STOP_PCT / 100)
        exit_px, how, j = None, "max_hold", e
        for j in range(e, e + HOLD_BARS):
            if l[j] <= stop:                       # stop checked first inside a bar (conservative)
                exit_px, how = stop, "stop"
                break
            if h[j] >= tp:
                exit_px, how = tp, "take_profit"
                break
        if exit_px is None:
            exit_px = cl[e + HOLD_BARS - 1]
            j = e + HOLD_BARS - 1
        busy_until = j
        out.append({"time": str(df.index[i])[:16], "drop_pct": round(float(r.iloc[i]) * 100, 2), "entry": float(entry), "exit": float(exit_px),
                    "how": how, "net_usd": pnl(entry, exit_px, cost_bps_leg), "ret_bps": (pnl(entry, exit_px, cost_bps_leg) / SIZE_USD) * 1e4})
    return out


def summarise(tr: list[dict], rng) -> dict:
    if not tr:
        return {"n": 0}
    x = np.array([t["ret_bps"] for t in tr])
    boot = rng.choice(x, size=(5000, len(x))).mean(axis=1)
    w, ls = x[x > 0], x[x <= 0]
    return {"n": len(x), "win_rate_pct": round(float((x > 0).mean()) * 100, 1), "mean_bps": round(float(x.mean()), 1),
            "median_bps": round(float(np.median(x)), 1), "ci95_mean_bps": [round(float(np.percentile(boot, 2.5)), 1), round(float(np.percentile(boot, 97.5)), 1)],
            "profit_factor": round(float(w.sum() / -ls.sum()), 2) if ls.sum() < 0 else None, "worst_bps": round(float(x.min()), 1),
            "best_bps": round(float(x.max()), 1), "exits": {k: sum(1 for t in tr if t["how"] == k) for k in ("take_profit", "stop", "max_hold")}}


def part_c(data: dict[str, pd.DataFrame], sol_bar_ret: float, rng, n_paths: int = 10_000) -> dict:
    """Bootstrap the 6 minutes after entry from post-dip minutes of every historical dip at least as deep as 3%."""
    pools = [[] for _ in range(HOLD_BARS)]       # pools[k] = (high, low, close) returns vs prior close at lag k
    for df in data.values():
        r = df["close"].pct_change().to_numpy()
        o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
        for i in np.where(r < -0.03)[0]:
            e = i + 1
            if e + HOLD_BARS >= len(df):
                continue
            for k in range(HOLD_BARS):
                prev = o[e] if k == 0 else c[e + k - 1]
                pools[k].append((h[e + k] / prev - 1, l[e + k] / prev - 1, c[e + k] / prev - 1))
    pools = [np.array(p) for p in pools]
    entry, tp, stop = 90.50, 93.5888, 90.50 * (1 - STOP_PCT / 100)
    out = {"target": 0, "stop": 0, "neither": 0}
    final = []
    for _ in range(n_paths):
        px = entry
        res = "neither"
        for k in range(HOLD_BARS):
            hi, lo, cl = pools[k][rng.integers(len(pools[k]))]
            if px * (1 + lo) <= stop:
                res = "stop"; break
            if px * (1 + hi) >= tp:
                res = "target"; break
            px *= (1 + cl)
        out[res] += 1
        final.append(px)
    tot = sum(out.values())
    return {"pool_sizes": [len(p) for p in pools], "probabilities_pct": {k: round(v / tot * 100, 1) for k, v in out.items()},
            "entry": entry, "take_profit": tp, "stop": round(stop, 4), "breakeven_target_share_pct": round(100 * 1.0 / (1 + (tp - entry) / (entry - stop)), 1)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", default="research_lab/results/analogs.json")
    a = ap.parse_args()
    rng = np.random.default_rng(822)
    data = {s: load_1m(glob.glob(f"{a.root}/parshant231234-eng_{s}-Historical-Data/1m/*/*/*.csv")) for s in ("SOLUSDT", "BTCUSDT", "ETHUSDT")}
    res = {"A_reconstruction": part_a(data["SOLUSDT"]), "B_analogs": {}, "C_bootstrap": None}
    for cost in (15, 25, 50):
        for delay in (0, 1, 2):
            allt = []
            per = {}
            for s, df in data.items():
                t = analog_trades(df, cost, delay)
                per[s] = summarise(t, rng)
                allt += [dict(x, symbol=s) for x in t]
            res["B_analogs"][f"cost{cost}|delay{delay}"] = {"pooled": summarise(allt, rng), "per_asset": per,
                                                             "by_year": {y: summarise([t for t in allt if t["time"].startswith(y)], rng)
                                                                         for y in ("2023", "2024", "2025", "2026")},
                                                             "trades": allt if (cost == 25 and delay == 1) else None}
    res["C_bootstrap"] = part_c(data, -0.0551, rng)
    with open(a.out, "w") as f:
        json.dump(res, f, indent=1, default=str)
    A = res["A_reconstruction"]
    print("A. fill grid ($50, net USD at 15/25/50/100/200 bps per leg):")
    for g in A["grid"]:
        print(f"   {g['entry'][:44]:<44} {g['entry_px']:>7.2f} -> {g['exit'][:38]:<38} {g['exit_px']:>7.2f}: "
              + " ".join(f"{g[f'net_usd_cost{c}']:+.2f}" for c in (15, 25, 50, 100, 200)))
    print("B. analogs (engine rule, real paths):")
    for k, v in res["B_analogs"].items():
        p = v["pooled"]
        print(f"   {k:<16} n={p.get('n')} win={p.get('win_rate_pct')}% mean={p.get('mean_bps')}bps CI={p.get('ci95_mean_bps')} PF={p.get('profit_factor')} "
              f"exits={p.get('exits')} per-asset n={ {s: x.get('n') for s, x in v['per_asset'].items()} }")
    print("C. bootstrap:", json.dumps(res["C_bootstrap"]))


if __name__ == "__main__":
    main()
