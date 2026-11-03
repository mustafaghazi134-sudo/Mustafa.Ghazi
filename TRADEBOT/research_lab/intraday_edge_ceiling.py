"""Intraday edge-ceiling test, pre-registered in docs/INTRADAY_EDGE_CEILING_PREREG.md. Run once.

For 7 fixed features and 4 horizons: decile thresholds and the momentum/reversal sign are chosen on train
(to 2024-12-31); on holdout (2025-01-01 ..) non-overlapping extreme-decile events are traded in the predicted direction
at the next 1-minute open and closed `h` minutes later. Net of 11 bps (perp taker round trip) or 3 bps (maker bound).

usage (from TRADEBOT/): PYTHONPATH=$PWD python -m research_lab.intraday_edge_ceiling --root-1m <data_1m> \
    --funding-dir research_lab/data_ext --cache <scratch>/edge_cache.pkl --out research_lab/results/intraday_edge_ceiling.json
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import pickle
import time

import numpy as np
import pandas as pd

SYMS = {"BTCUSDT": "BTC", "ETHUSDT": "ETH", "SOLUSDT": "SOL"}
TRAIN_END = pd.Timestamp("2025-01-01", tz="UTC")
HOLD_END = pd.Timestamp("2026-10-25", tz="UTC")
HALF = pd.Timestamp("2026-01-01", tz="UTC")
HORIZONS = (5, 15, 60, 240)
COST_TAKER, COST_MAKER = 11.0, 3.0
N_TESTS = 28
ALPHA = 0.05 / N_TESTS
SEED = 20261104


def load(root: str, sym: str) -> pd.DataFrame:
    frames = []
    for f in sorted(glob.glob(f"{root}/parshant231234-eng_{sym}-Historical-Data/1m/*/*/*.csv")):
        d = pd.read_csv(f, usecols=["timestamp", "open", "close", "volume", "taker_buy_volume"])
        frames.append(d)
    d = pd.concat(frames)
    d.index = pd.to_datetime(d.pop("timestamp"), unit="ms", utc=True)
    d = d[~d.index.duplicated(keep="last")].sort_index()
    return d[d.index < HOLD_END].astype(float)


def features(d: pd.DataFrame, funding: pd.Series | None) -> pd.DataFrame:
    c, v, tb = d["close"], d["volume"], d["taker_buy_volume"]
    f = pd.DataFrame(index=d.index)
    for n, name in ((5, "F1_ofi_5m"), (60, "F2_ofi_60m")):
        vs = v.rolling(n).sum()
        f[name] = (2 * tb.rolling(n).sum() - vs) / vs.replace(0, np.nan)
    r5, r60 = c / c.shift(5) - 1, c / c.shift(60) - 1
    f["F3_ret_5m"], f["F4_ret_60m"] = r5, r60
    f["F5_vol_burst"] = r5 / r5.rolling(1440).std()
    if funding is not None and len(funding):
        fr = funding.reindex(d.index, method="ffill")          # last completed hourly funding at or before t
        f["F6_funding"] = fr.where(fr.index >= funding.index[0])
    else:
        f["F6_funding"] = np.nan
    f["F7_ret60_z"] = r60 / r60.rolling(43200, min_periods=10000).std()
    return f


def forward(d: pd.DataFrame, h: int) -> np.ndarray:
    """Return from open of t+1 to open of t+1+h; NaN where the minute grid has gaps."""
    o = d["open"].values
    t = d.index.values.astype("datetime64[m]").astype(np.int64)
    n = len(o)
    out = np.full(n, np.nan)
    a, b = np.arange(n) + 1, np.arange(n) + 1 + h
    ok = b < n
    a, b = a[ok], b[ok]
    good = (t[b] - t[a]) == h
    idx = np.nonzero(ok)[0][good]
    out[idx] = o[b[good]] / o[a[good]] - 1
    return out


def non_overlapping(pos: np.ndarray, h: int) -> np.ndarray:
    keep, nxt = [], -1
    for p in pos:
        if p >= nxt:
            keep.append(p); nxt = p + 1 + h
    return np.array(keep, dtype=np.int64)


def boot_p(net_bps: np.ndarray, days: np.ndarray, rng, n_boot: int = 5000) -> tuple[float, list]:
    if len(net_bps) < 30:
        return float("nan"), [None, None]
    df = pd.DataFrame({"x": net_bps, "d": days})
    g = df.groupby("d")["x"].agg(["sum", "count"])
    s, c = g["sum"].values, g["count"].values
    obs = s.sum() / c.sum()
    k = len(s)
    ix = rng.integers(0, k, (n_boot, k))
    means = s[ix].sum(axis=1) / c[ix].sum(axis=1)
    p = float(((means - obs) >= obs).mean())          # centred: P(mean* - obs >= obs) under H0 mean 0
    return p, [round(float(np.percentile(means, 2.5)), 2), round(float(np.percentile(means, 97.5)), 2)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root-1m", required=True)
    ap.add_argument("--funding-dir", default="research_lab/data_ext")
    ap.add_argument("--cache")
    ap.add_argument("--out", default="research_lab/results/intraday_edge_ceiling.json")
    a = ap.parse_args()
    rng = np.random.default_rng(SEED)
    t0 = time.time()
    if a.cache and os.path.exists(a.cache):
        data = pickle.load(open(a.cache, "rb"))
    else:
        data = {s: load(a.root_1m, s) for s in SYMS}
        if a.cache:
            pickle.dump(data, open(a.cache, "wb"))
    events: dict = {}        # (feature, h) -> list of dicts per asset
    meta = {}
    for sym, coin in SYMS.items():
        d = data[sym]
        fp = os.path.join(a.funding_dir, f"funding_hyperliquid_{coin}.csv")
        fund = None
        if os.path.exists(fp):
            fr = pd.read_csv(fp)
            fund = pd.Series(fr["rate_per_hour"].values, index=pd.DatetimeIndex(pd.to_datetime(fr["time_ms"], unit="ms", utc=True)).floor("min"))
            fund = fund[~fund.index.duplicated(keep="last")].sort_index()
        F = features(d, fund)
        train = F.index < TRAIN_END
        hold = (F.index >= TRAIN_END) & (F.index < HOLD_END)
        meta[sym] = {"bars": int(len(d)), "first": str(d.index[0]), "last": str(d.index[-1]),
                     "train_bars": int(train.sum()), "holdout_bars": int(hold.sum())}
        day = F.index.floor("D").values
        for h in HORIZONS:
            fwd = forward(d, h)
            for col in F.columns:
                x = F[col].values
                tr = train & ~np.isnan(x) & ~np.isnan(fwd)
                if tr.sum() < 1000:
                    continue
                lo, hi = np.nanpercentile(x[tr], 10), np.nanpercentile(x[tr], 90)
                top_tr, bot_tr = tr & (x >= hi), tr & (x <= lo)
                spread = np.nanmean(fwd[top_tr]) - np.nanmean(fwd[bot_tr])
                sign = 1 if spread > 0 else -1
                ho = hold & ~np.isnan(x) & ~np.isnan(fwd)
                side = np.where(x >= hi, 1, np.where(x <= lo, -1, 0))
                pos = np.nonzero(ho & (side != 0))[0]
                pos = non_overlapping(pos, h)
                gross_bps = sign * side[pos] * fwd[pos] * 1e4
                events.setdefault((col, h), []).append({
                    "sym": sym, "sign": "momentum" if sign > 0 else "reversal", "train_spread_bps": round(float(spread) * 1e4, 2),
                    "train_events": int(top_tr.sum() + bot_tr.sum()), "gross": gross_bps, "days": day[pos],
                    "ts": F.index.values[pos]})
        print(f"{sym} done ({time.time() - t0:.0f}s)", flush=True)
    rows = []
    for (col, h), per in sorted(events.items()):
        g = np.concatenate([e["gross"] for e in per])
        dd = np.concatenate([e["days"] for e in per])
        ts = np.concatenate([e["ts"] for e in per])
        net = g - COST_TAKER
        p, ci = boot_p(net, dd, rng)
        h1 = ts < np.datetime64(HALF.tz_localize(None))
        per_asset = {e["sym"]: {"n": int(len(e["gross"])), "gross_bps": round(float(e["gross"].mean()), 2) if len(e["gross"]) else None,
                                "net_taker_bps": round(float(e["gross"].mean() - COST_TAKER), 2) if len(e["gross"]) else None,
                                "sign": e["sign"], "train_spread_bps": e["train_spread_bps"]} for e in per}
        crit = {
            "1_net_pos_bonferroni": bool(net.mean() > 0 and p < ALPHA),
            "2_two_of_three_assets": sum(1 for v in per_asset.values() if (v["net_taker_bps"] or -1) > 0) >= 2,
            "3_both_halves": bool(len(net[h1]) and len(net[~h1]) and net[h1].mean() > 0 and net[~h1].mean() > 0),
            "4_min_200_events": bool(len(net) >= 200),
        }
        rows.append({"feature": col, "horizon_min": h, "n": int(len(g)), "gross_bps": round(float(g.mean()), 2),
                     "net_taker_bps": round(float(net.mean()), 2), "net_taker_ci95_bps": ci, "p_one_sided": round(p, 5),
                     "net_maker_bound_bps": round(float(g.mean() - COST_MAKER), 2),
                     "net_taker_2025": round(float(net[h1].mean()), 2) if h1.any() else None,
                     "net_taker_2026": round(float(net[~h1].mean()), 2) if (~h1).any() else None,
                     "events_per_day": round(len(g) / max(1, len(np.unique(dd))), 2),
                     "per_asset": per_asset, "criteria": crit, "PASS": all(crit.values())})
    out = {"preregistration": "docs/INTRADAY_EDGE_CEILING_PREREG.md", "seed": SEED, "costs_bps_round_trip": {"taker": COST_TAKER, "maker_bound": COST_MAKER},
           "bonferroni_alpha": ALPHA, "data": meta, "rows": rows, "passes": [f"{r['feature']}@{r['horizon_min']}m" for r in rows if r["PASS"]],
           "runtime_sec": round(time.time() - t0, 1)}
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    json.dump(out, open(a.out, "w"), indent=1, default=str)
    for r in rows:
        print(f"{r['feature']:14s} {r['horizon_min']:>4}m n {r['n']:>6} gross {r['gross_bps']:>7} net {r['net_taker_bps']:>7} "
              f"ci {r['net_taker_ci95_bps']} p {r['p_one_sided']} 25/26 {r['net_taker_2025']}/{r['net_taker_2026']} "
              f"maker {r['net_maker_bound_bps']} {'PASS' if r['PASS'] else ''}")
    print("passes:", out["passes"])


if __name__ == "__main__":
    main()
