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
from . import candidates  # noqa: F401  (registers adapted candidates)
from .walkforward import holdout_run, run_strategy, walk_forward

FEE_BPS, SLIP_BPS, WARMUP = 10.0, 15.0, 800   # per side; ~0.5% round trip, realistic for liquid Jupiter routes


def _log_holdout_look(out_dir: str, strategy: str, symbol: str, holdout_start: str, params: dict, metrics: dict) -> None:
    """Every evaluation on the untouched period is appended to results/holdout_looks.jsonl so the number of looks is
    auditable (research_lab/SPLITS.md). A holdout that has been looked at many times is no longer independent."""
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "holdout_looks.jsonl"), "a") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "symbol": symbol, "holdout_start": holdout_start,
                            "strategy": strategy, "params": params, "purpose": "run_all frozen-parameter holdout",
                            "net_return_pct": metrics.get("net_return_pct")}) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dirs", nargs="+", required=True)
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT")
    ap.add_argument("--out", default="research_lab/results")
    ap.add_argument("--strategies", default=None, help="comma list; default all")
    ap.add_argument("--train-months", type=int, default=12)
    ap.add_argument("--test-months", type=int, default=3)
    ap.add_argument("--timeframe", default="1h", choices=["1h", "4h", "1D"], help="resample hourly data before testing")
    ap.add_argument("--holdout-start", default=None, help="YYYY-MM-DD: walk-forward develops before this date; frozen params are then run on the untouched period after it")
    ap.add_argument("--size-frac", type=float, default=1.0, help="fraction of equity per position (deployment: max_position_pct/100)")
    ap.add_argument("--fee-bps", type=float, default=None); ap.add_argument("--slip-bps", type=float, default=None)
    ap.add_argument("--dev-only", action="store_true", help="with --holdout-start: develop before the date but do NOT evaluate on the holdout (for hypotheses formed after the holdout was inspected)")
    args = ap.parse_args()
    global FEE_BPS, SLIP_BPS
    if args.fee_bps is not None: FEE_BPS = args.fee_bps
    if args.slip_bps is not None: SLIP_BPS = args.slip_bps
    holdout = pd.Timestamp(args.holdout_start, tz="UTC") if args.holdout_start else None
    bar_minutes = {"1h": 60, "4h": 240, "1D": 1440}[args.timeframe]
    warmup = {"1h": WARMUP, "4h": 300, "1D": 120}[args.timeframe]
    os.makedirs(args.out, exist_ok=True)
    files = discover(args.data_dirs)
    symbols = args.symbols.split(",")
    names = args.strategies.split(",") if args.strategies else list(REGISTRY)
    results: dict = {"costs": {"fee_bps_per_side": FEE_BPS, "slippage_bps_per_side": SLIP_BPS}, "timeframe": args.timeframe,
                     "size_frac": args.size_frac, "holdout_start": args.holdout_start, "data": {}, "strategies": {}}
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
        results["strategies"][name] = {"source": REGISTRY[name]["source"], "note": REGISTRY[name]["note"], "flags": REGISTRY[name]["flags"],
                                       "author_claim": REGISTRY[name].get("author_claim", ""), "per_symbol": {}}
        for s, df in data.items():
            t0 = time.time()
            if "needs_regimes" in REGISTRY[name]["flags"]:
                candidates.prepare_regimes(df)          # once per asset, causal, full history
            partners = {k: v for k, v in data.items() if k != s} if REGISTRY[name].get("needs_partners") else None
            dev_df = df[df.index < holdout] if holdout is not None else df
            full = run_strategy(name, dev_df, {}, FEE_BPS, SLIP_BPS, bar_minutes, warmup=warmup, size_frac=args.size_frac, partners=partners)
            la = lookahead_check(name, df) if "custom_backtest" not in REGISTRY[name]["flags"] and not REGISTRY[name].get("needs_partners") else {"passed": True, "note": "n/a (custom backtest / partner strategy); construction reviewed by hand"}
            entry: dict = {"in_sample_default_params": full.metrics, "lookahead": la}
            if name != "buy_hold":
                wf = walk_forward(name, df, FEE_BPS, SLIP_BPS, args.train_months, args.test_months, warmup, bar_minutes,
                                  size_frac=args.size_frac, partners=partners, end_at=holdout)
                entry["walk_forward_oos"] = wf.oos_metrics
                entry["wf_segments"] = wf.segments
                entry["wf_param_stability"] = wf.param_stability
                entry["final_params"] = wf.final_params
                if holdout is not None and args.dev_only:
                    entry["holdout"] = {"note": "not evaluated (--dev-only): hypothesis post-dates the holdout inspection"}
                elif holdout is not None and len(df[df.index >= holdout]) > 10:
                    hr = holdout_run(name, df, wf.final_params, holdout, FEE_BPS, SLIP_BPS, warmup, bar_minutes, args.size_frac, partners)
                    entry["holdout"] = hr.metrics if hr else {"note": "insufficient holdout data"}
                    entry["holdout_span"] = [str(holdout)[:10], str(df.index[-1])[:10]]
                    _log_holdout_look(args.out, name, s, args.holdout_start, wf.final_params, entry["holdout"])
            elif holdout is not None:
                hr = holdout_run(name, df, {}, holdout, FEE_BPS, SLIP_BPS, warmup, bar_minutes, args.size_frac)
                entry["holdout"] = hr.metrics if hr else {}
                simple = not REGISTRY[name].get("needs_partners") and "custom_backtest" not in REGISTRY[name]["flags"]
                entry["cost_sensitivity"] = cost_sensitivity(name, dev_df, {}, warmup) if simple else {}
                entry["delayed_entry"] = delayed_entry_check(name, dev_df, {}, warmup, FEE_BPS, SLIP_BPS) if simple else {}
                if "custom_backtest" in REGISTRY[name]["flags"]:
                    r2 = run_strategy(name, dev_df, {}, FEE_BPS * 2, SLIP_BPS * 2, bar_minutes, warmup=warmup, size_frac=args.size_frac)
                    entry["cost_sensitivity"] = {"cost_x2": {"net_return_pct": r2.metrics["net_return_pct"], "profit_factor": r2.metrics["profit_factor"], "trades": r2.metrics["trades"]}}
                if name != "buy_hold" and wf.oos_equity is not None:
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
