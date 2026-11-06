"""Calibration added AFTER the pre-registration (it changes no candidate, parameter or criterion): how often do the
pre-registered acceptance tiers pass on markets with NO edge by construction? Runs the same study code on
random-walk prices (fat-tailed hourly returns, zero drift, volatility similar to BTC/ETH/SOL, same calendar spans).

usage: PYTHONPATH=$PWD python -m research_lab.longshort_null_calibration --seeds 20 --out research_lab/results/longshort_null_calibration.json
"""
from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

from . import longshort_study as L

SPANS = {"BTCUSDT": ("2019-01-01", 0.006), "ETHUSDT": ("2019-09-01", 0.008), "SOLUSDT": ("2022-01-01", 0.011)}


def synth(seed: int) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    out = {}
    for s, (start, vol) in SPANS.items():
        idx = pd.date_range(pd.Timestamp(start, tz="UTC"), pd.Timestamp("2026-10-24 23:00", tz="UTC"), freq="1h")
        r = rng.standard_t(3, len(idx)) * vol / np.sqrt(3.0)
        p = 100 * np.exp(np.cumsum(r - r.mean()))
        out[s] = pd.DataFrame({"open": p, "high": p * (1 + vol / 2), "low": p * (1 - vol / 2), "close": p, "volume": 1.0}, index=idx)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=20)
    ap.add_argument("--funding-dir", default="research_lab/data_ext")
    ap.add_argument("--out", default="research_lab/results/longshort_null_calibration.json")
    a = ap.parse_args()
    funding = {s: L.load_funding(a.funding_dir, s)[0] for s in L.SYMS}
    tally: dict = {}
    for seed in range(a.seeds):
        data = synth(1000 + seed)
        rng = np.random.default_rng(seed)
        for name, (fn, rule) in L.CANDIDATES.items():
            res = L.run_candidate(name, fn, rule, data, funding, rng)
            for side, cfg in res["configs"].items():
                t = tally.setdefault(f"{name}|{side}", {"TIER_A": 0, "TIER_B_forward_paper_only": 0, "REJECTED": 0, "E_ann_ret_pos": 0})
                t[cfg["acceptance"]["verdict"]] += 1
                t["E_ann_ret_pos"] += int(cfg["E"].get("ann_return_pct", -1) > 0)
        print(f"seed {seed} done", flush=True)
    any_b = sum(v["TIER_B_forward_paper_only"] + v["TIER_A"] for v in tally.values())
    out = {"seeds": a.seeds, "per_config": tally, "total_tier_A": sum(v["TIER_A"] for v in tally.values()),
           "total_tier_A_or_B": any_b, "configs_x_seeds": a.seeds * len(tally)}
    with open(a.out, "w") as f:
        json.dump(out, f, indent=1)
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
