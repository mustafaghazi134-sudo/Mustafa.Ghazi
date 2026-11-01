"""Run the full research pipeline and write RESULTS.md + results.json.

usage: python -m research_lab.run_all --data-dirs <dir> [<dir> ...] [--symbols BTCUSDT,ETHUSDT,SOLUSDT]
"""
from __future__ import annotations

import argparse
import json
import os
import time

import pandas as pd

from .checks import cost_sensitivity, dca_martingale_demo, delayed_entry_check, lookahead_check
from .data import discover, format_report, load_symbol, resample, split_on_gaps
from .strategies import REGISTRY
from .walkforward import run_strategy, walk_forward

FEE_BPS, SLIP_BPS, WARMUP = 10.0, 15.0, 800   # per side; ~0.5% round trip, realistic for liquid Jupiter routes


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dirs", nargs="+", required=True)
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT")
    ap.add_argument("--out", default="research_lab/results")
    ap.add_argument("--strategies", default=None, help="comma list; default all")
    ap.add_argument("--train-months", type=int, default=12)
    ap.add_argument("--test-months", type=int, default=3)
    ap.add_argument("--timeframe", default="1h", choices=["1h", "4h", "1D"], help="resample hourly data before testing")
    args = ap.parse_args()
    bar_minutes = {"1h": 60, "4h": 240, "1D": 1440}[args.timeframe]
    warmup = {"1h": WARMUP, "4h": 300, "1D": 120}[args.timeframe]
    os.makedirs(args.out, exist_ok=True)
    files = discover(args.data_dirs)
    symbols = args.symbols.split(",")
    names = args.strategies.split(",") if args.strategies else list(REGISTRY)
    results: dict = {"costs": {"fee_bps_per_side": FEE_BPS, "slippage_bps_per_side": SLIP_BPS}, "timeframe": args.timeframe, "data": {}, "strategies": {}}
    data = {}
    for s in symbols:
        if s not in files:
            print(f"!! no data for {s}"); continue
        df, rep = load_symbol(s, files[s], prefer=["changhaowuwu_LSTM-live-cripto-forecast", "spurs7216_Quant_Research", "7huahua_RedditDataset"])
        results["data"][s] = rep.__dict__
        print(format_report(rep))
        blocks = split_on_gaps(df)
        if args.timeframe != "1h":
            blocks = [resample(b, args.timeframe) for b in blocks]
        for b in blocks:
            key = s if len(blocks) == 1 else f"{s}[{str(b.index[0])[:7]}..{str(b.index[-1])[:7]}]"
            data[key] = b
            if len(blocks) > 1:
                print(f"  block {key}: {len(b)} bars")
    t_start = time.time()
    for name in names:
        results["strategies"][name] = {"source": REGISTRY[name]["source"], "note": REGISTRY[name]["note"], "flags": REGISTRY[name]["flags"], "per_symbol": {}}
        for s, df in data.items():
            t0 = time.time()
            full = run_strategy(name, df, {}, FEE_BPS, SLIP_BPS, bar_minutes, warmup=warmup)
            la = lookahead_check(name, df)
            entry: dict = {"in_sample_default_params": full.metrics, "lookahead": la}
            if name != "buy_hold":
                wf = walk_forward(name, df, FEE_BPS, SLIP_BPS, args.train_months, args.test_months, warmup, bar_minutes)
                entry["walk_forward_oos"] = wf.oos_metrics
                entry["wf_segments"] = wf.segments
                entry["wf_param_stability"] = wf.param_stability
                entry["cost_sensitivity"] = cost_sensitivity(name, df, {}, warmup)
                entry["delayed_entry"] = delayed_entry_check(name, df, {}, warmup, FEE_BPS, SLIP_BPS)
                if wf.oos_equity is not None:
                    wf.oos_equity.to_csv(f"{args.out}/oos_equity_{name}_{s}.csv")
            results["strategies"][name]["per_symbol"][s] = entry
            print(f"{name:<26} {s:<8} IS ret {full.metrics['net_return_pct']:>8.1f}%  trades {full.metrics['trades']:>4}  "
                  f"OOS ret {entry.get('walk_forward_oos', {}).get('net_return_pct', float('nan')):>8.1f}%  "
                  f"OOS PF {entry.get('walk_forward_oos', {}).get('profit_factor', float('nan')):>5}  lookahead={'ok' if la['passed'] else 'FAIL'}  ({time.time()-t0:.0f}s)")
    results["dca_martingale_demo"] = {s: dca_martingale_demo(df) for s, df in data.items()}
    with open(f"{args.out}/results.json", "w") as f:
        json.dump(results, f, indent=1, default=str)
    print(f"done in {time.time()-t_start:.0f}s -> {args.out}/results.json")


if __name__ == "__main__":
    main()
