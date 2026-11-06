"""How well do the executable Solana assets track the Binance instruments the signals are computed on?
Reads research_lab/data_ext/ (fetched by .github/workflows/research-data-fetch.yml) and prints/returns:
  - DefiLlama daily: Solana cbBTC / Wormhole WETH / SOL vs coingecko BTC / ETH / SOL
  - GeckoTerminal daily and hourly pool closes vs Binance closes for the deepest pools
  - pool liquidity (reserve) and 24h volume
usage: PYTHONPATH=$PWD python -m research_lab.wrapped_tracking --dir research_lab/data_ext --out research_lab/results/wrapped_tracking.json
"""
from __future__ import annotations

import argparse
import glob
import json
import os

import numpy as np
import pandas as pd

REF = {"cbBTC": "BTCUSDT", "WETH_wormhole": "ETHUSDT"}


def dev_stats(dev_bps: pd.Series) -> dict:
    d = dev_bps.dropna()
    if not len(d):
        return {"n": 0}
    a = d.abs()
    return {"n": int(len(d)), "first": str(d.index[0])[:16], "last": str(d.index[-1])[:16],
            "mean_bps": round(float(d.mean()), 1), "median_abs_bps": round(float(a.median()), 1),
            "p95_abs_bps": round(float(a.quantile(0.95)), 1), "p99_abs_bps": round(float(a.quantile(0.99)), 1),
            "max_abs_bps": round(float(a.max()), 1), "max_at": str(a.idxmax())[:16],
            "share_over_50bps_pct": round(float((a > 50).mean()) * 100, 2), "share_over_100bps_pct": round(float((a > 100).mean()) * 100, 2)}


def binance(dirp: str, pair: str, itv: str) -> pd.Series:
    p = os.path.join(dirp, f"binance_{pair}_{itv}.csv")
    if not os.path.exists(p):
        return pd.Series(dtype=float)
    b = pd.read_csv(p)
    r = pd.Series(b["close"].values, index=pd.to_datetime(b["open_ms"], unit="ms", utc=True))
    return r[~r.index.duplicated(keep="last")]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="research_lab/data_ext")
    ap.add_argument("--out", default="research_lab/results/wrapped_tracking.json")
    a = ap.parse_args()
    out: dict = {"llama_daily": {}, "geckoterminal": {}, "pools": {}}
    for name in ("cbBTC", "WETH_wormhole", "SOL"):
        p = os.path.join(a.dir, f"wrapped_llama_{name}.csv")
        if os.path.exists(p):
            d = pd.read_csv(p)
            d.index = pd.to_datetime(d["day_ts"], unit="s", utc=True)
            out["llama_daily"][name] = dev_stats((d["solana_token_usd"] / d["reference_usd"] - 1) * 1e4)
    for name, pair in REF.items():
        pj = os.path.join(a.dir, f"wrapped_gt_{name}_pools.json")
        if os.path.exists(pj):
            out["pools"][name] = json.load(open(pj))
        res = {}
        p = os.path.join(a.dir, f"wrapped_gt_{name}_1d.csv")
        if os.path.exists(p):
            g = pd.read_csv(p)
            ref = binance(a.dir, pair, "1d")
            for pool, gg in g.groupby("pool"):
                s = pd.Series(gg["close"].values, index=pd.to_datetime(gg["ts"], unit="s", utc=True)).sort_index()
                s = s[~s.index.duplicated(keep="last")]
                j = pd.concat([s, ref], axis=1, keys=["pool", "ref"]).dropna()
                st = dev_stats((j["pool"] / j["ref"] - 1) * 1e4)
                st["median_daily_volume_usd"] = round(float(gg["volume_usd"].median()), 0)
                res[f"{pool[:8]}_1d"] = st
        for p in glob.glob(os.path.join(a.dir, f"wrapped_gt_{name}_*_1h.csv")):
            g = pd.read_csv(p)
            ref = binance(a.dir, pair, "1h")
            s = pd.Series(g["close"].values, index=pd.to_datetime(g["ts"], unit="s", utc=True)).sort_index()
            s = s[~s.index.duplicated(keep="last")]
            j = pd.concat([s, ref], axis=1, keys=["pool", "ref"]).dropna()
            st = dev_stats((j["pool"] / j["ref"] - 1) * 1e4)
            st["median_hourly_volume_usd"] = round(float(g["volume_usd"].median()), 0)
            st["hours_with_zero_volume_pct"] = round(float((g["volume_usd"] <= 0).mean()) * 100, 1)
            res[os.path.basename(p)[len(f"wrapped_gt_{name}_"):-7] + "_1h"] = st
        out["geckoterminal"][name] = res
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w") as f:
        json.dump(out, f, indent=1)
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
