"""Why did the deployed strategy make ~0% on the 2025-01 -> 2026-09 BTC holdout?

Evidence-based decomposition, not a market-conditions story. Every number here is computed from the same monthly
Binance kline mirrors as the comparison run (DATA_SOURCES.md), resampled to 4h, 10% sizing, 10 bps fee + 15 bps
slippage per side, next-open fills.

What it measures
  1. Trade ledger for the holdout: gross P&L, costs, exit reasons, holding times, quarter attribution.
  2. Signal quality: forward return of the asset while the strategy is in-signal vs out-of-signal, development vs holdout,
     with standard errors. If the signal no longer separates up-bars from down-bars, that is strategy decay.
  3. Path statistics that a trend follower depends on: 30-day momentum sign flips per year, efficiency ratio,
     realised vol, and the number/size of >15% swings. These are measured on both periods so "sideways" is a number,
     not an adjective.
  4. Historical context: the distribution of rolling 21-month returns of the SAME frozen configuration over the
     development history. Where does the holdout result sit in that distribution?
  5. Parameter sensitivity on the holdout for the two configurations that were already fixed before the holdout was
     opened (deployed 30/100 and walk-forward-final 30/50). No other parameter set is evaluated on the holdout, and
     every evaluation is appended to results/holdout_looks.jsonl so later readers can count the looks.

usage (from TRADEBOT/):
  PYTHONPATH=$PWD python -m research_lab.holdout_diagnosis --data-dirs <dirs> --symbol BTCUSDT --holdout-start 2025-01-01
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time

import numpy as np
import pandas as pd

from .data import discover, load_symbol, resample, split_on_gaps
from .engine import compute_metrics
from .walkforward import holdout_run, run_strategy

FEE_BPS, SLIP_BPS = 10.0, 15.0
BAR_MIN, WARMUP = 240, 300
DEPLOYED = {"lookback_days": 30, "ema_days": 100, "vol_stop_pct": 25.0}       # tradebot config.toml [[strategies]] tsmom
WF_FINAL = {"lookback_days": 30, "ema_days": 50, "vol_stop_pct": 25.0}        # chosen by the last development window


def _bpd(df: pd.DataFrame) -> int:
    return int(round(1440 / BAR_MIN))


def signal_series(df: pd.DataFrame, p: dict) -> pd.Series:
    """In-signal state (long) per bar for the TSMOM+EMA rule, evaluated at 00:00 UTC bars, held until exit signal."""
    bpd = _bpd(df)
    c = df["close"]
    mom = c / c.shift(p["lookback_days"] * bpd) - 1
    ema = c.ewm(span=p["ema_days"] * bpd, adjust=False).mean()
    daily = pd.Series(df.index.hour == 0, index=df.index)
    enter = (mom > 0) & (c > ema) & daily
    exit_ = ((mom < 0) | (c < ema)) & daily
    state = np.zeros(len(df), dtype=bool)
    on = False
    e, x = enter.to_numpy(), exit_.to_numpy()
    for i in range(len(df)):
        if on and x[i]:
            on = False
        elif not on and e[i]:
            on = True
        state[i] = on
    # the position is held from the NEXT bar after the signal bar (next-open fill)
    return pd.Series(state, index=df.index).shift(1).fillna(False).astype(bool)


def forward_return_split(df: pd.DataFrame, state: pd.Series, horizon_bars: int) -> dict:
    """Mean forward return over `horizon_bars` when in-signal vs out-of-signal, with standard errors (non-overlapping)."""
    c = df["close"]
    fwd = c.shift(-horizon_bars) / c - 1
    rows = {}
    for label, mask in (("in_signal", state), ("out_of_signal", ~state)):
        s = fwd[mask].iloc[::horizon_bars].dropna()          # non-overlapping samples
        if len(s) < 5:
            rows[label] = {"n": int(len(s))}
            continue
        rows[label] = {"n": int(len(s)), "mean_pct": round(float(s.mean()) * 100, 3), "se_pct": round(float(s.std(ddof=1) / math.sqrt(len(s))) * 100, 3),
                       "hit_rate_pct": round(float((s > 0).mean()) * 100, 1)}
    a, b = rows.get("in_signal", {}), rows.get("out_of_signal", {})
    if "mean_pct" in a and "mean_pct" in b:
        diff = a["mean_pct"] - b["mean_pct"]
        se = math.sqrt(a["se_pct"] ** 2 + b["se_pct"] ** 2)
        rows["in_minus_out_pct"] = round(diff, 3)
        rows["t_stat"] = round(diff / se, 2) if se > 0 else None
    return rows


def path_stats(df: pd.DataFrame, lookback_days: int) -> dict:
    bpd = _bpd(df)
    c = df["close"]
    daily = c[df.index.hour == 0]
    years = max((daily.index[-1] - daily.index[0]).days / 365.25, 1e-9)
    mom_sign = np.sign((daily / daily.shift(lookback_days) - 1).dropna())
    flips = int((mom_sign != mom_sign.shift(1)).sum() - 1)
    # Kaufman efficiency ratio on 90-day windows of daily closes: |net move| / sum|daily moves|
    er = []
    for i in range(90, len(daily), 30):
        seg = daily.iloc[i - 90:i]
        denom = float(seg.diff().abs().sum())
        er.append(abs(float(seg.iloc[-1] - seg.iloc[0])) / denom if denom else 0.0)
    dr = daily.pct_change().dropna()
    # swings: count of peak-to-trough or trough-to-peak moves > 15% on daily closes (zigzag)
    swings, direction, ext = 0, 0, float(daily.iloc[0])
    hi = lo = ext
    for px in daily.to_numpy(dtype=float):
        if direction == 0:                      # establish the first leg without counting it
            hi, lo = max(hi, px), min(lo, px)
            if px < hi * 0.85:
                direction, ext = -1, px
            elif px > lo * 1.15:
                direction, ext = 1, px
        elif direction > 0:
            if px > ext:
                ext = px
            elif px < ext * 0.85:
                swings += 1; direction = -1; ext = px
        else:
            if px < ext:
                ext = px
            elif px > ext * 1.15:
                swings += 1; direction = 1; ext = px
    return {"days": int(len(daily)), "net_change_pct": round((float(daily.iloc[-1]) / float(daily.iloc[0]) - 1) * 100, 1),
            "mom_sign_flips_per_year": round(flips / years, 1), "efficiency_ratio_90d_median": round(float(np.median(er)), 3) if er else None,
            "realised_vol_ann_pct": round(float(dr.std() * math.sqrt(365)) * 100, 1), "swings_gt_15pct_per_year": round(swings / years, 1),
            "best_30d_pct": round(float((daily / daily.shift(30) - 1).max()) * 100, 1), "worst_30d_pct": round(float((daily / daily.shift(30) - 1).min()) * 100, 1)}


def trade_ledger(res, size_frac: float) -> tuple[list[dict], dict]:
    rows, gross, cost = [], 0.0, 0.0
    per_side = (FEE_BPS + SLIP_BPS) / 1e4
    for t in res.trades:
        notional = t.qty * t.entry_price
        c = notional * per_side + t.qty * t.exit_price * per_side
        g = t.pnl + c
        gross += g; cost += c
        rows.append({"entry": str(t.entry_ts)[:16], "exit": str(t.exit_ts)[:16], "days": round(t.bars_held * BAR_MIN / 1440, 1),
                     "ret_pct": round(t.ret_pct, 2), "gross_usd": round(g, 2), "cost_usd": round(c, 2), "net_usd": round(t.pnl, 2), "reason": t.reason})
    by_reason: dict[str, dict] = {}
    for r in rows:
        d = by_reason.setdefault(r["reason"], {"n": 0, "net_usd": 0.0})
        d["n"] += 1; d["net_usd"] = round(d["net_usd"] + r["net_usd"], 2)
    by_q: dict[str, float] = {}
    for r in rows:
        q = pd.Timestamp(r["exit"]).to_period("Q").strftime("%YQ%q")
        by_q[q] = round(by_q.get(q, 0.0) + r["net_usd"], 2)
    short = [r for r in rows if r["days"] <= 7]
    return rows, {"trades": len(rows), "gross_usd": round(gross, 2), "cost_usd": round(cost, 2), "net_usd": round(gross - cost, 2),
                  "cost_share_of_gross_pct": round(cost / abs(gross) * 100, 1) if gross else None, "by_exit_reason": by_reason,
                  "by_quarter": by_q, "trades_held_le_7d": len(short), "net_of_trades_held_le_7d": round(sum(r["net_usd"] for r in short), 2)}


def rolling_windows(df: pd.DataFrame, p: dict, size_frac: float, months: int, step_months: int = 1) -> list[dict]:
    """Frozen-parameter return over every `months`-long window in the development history (warm-up from prior bars)."""
    out = []
    start = df.index[0] + pd.DateOffset(months=18)
    while start + pd.DateOffset(months=months) <= df.index[-1]:
        end = start + pd.DateOffset(months=months)
        seg = df[df.index < end]
        r = holdout_run("tsmom_ema_filter", seg, p, start, FEE_BPS, SLIP_BPS, WARMUP, BAR_MIN, size_frac)
        if r is not None:
            bh = float(seg["close"].iloc[-1] / seg["close"][seg.index >= start].iloc[0] - 1) * 100
            out.append({"start": str(start)[:10], "net_return_pct": r.metrics["net_return_pct"], "trades": r.metrics["trades"],
                        "max_drawdown_pct": r.metrics["max_drawdown_pct"], "buy_hold_pct": round(bh, 1)})
        start += pd.DateOffset(months=step_months)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dirs", nargs="+", required=True)
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--holdout-start", default="2025-01-01")
    ap.add_argument("--size-frac", type=float, default=0.10)
    ap.add_argument("--out", default="research_lab/results")
    args = ap.parse_args()
    hold = pd.Timestamp(args.holdout_start, tz="UTC")
    files = discover(args.data_dirs)
    df, rep = load_symbol(args.symbol, files[args.symbol], prefer=["changhaowuwu_LSTM-live-cripto-forecast", "spurs7216_Quant_Research", "7huahua_RedditDataset"])
    blocks = [resample(b, "4h") for b in split_on_gaps(df)]
    df = max(blocks, key=len)
    dev, ho = df[df.index < hold], df[df.index >= hold]
    report: dict = {"symbol": args.symbol, "bars": len(df), "span": [str(df.index[0])[:10], str(df.index[-1])[:10]], "holdout_start": args.holdout_start,
                    "costs_bps_per_side": FEE_BPS + SLIP_BPS, "size_frac": args.size_frac, "configs": {}}
    looks = []
    for label, p in (("deployed_30_100", DEPLOYED), ("wf_final_30_50", WF_FINAL)):
        r = holdout_run("tsmom_ema_filter", df, p, hold, FEE_BPS, SLIP_BPS, WARMUP, BAR_MIN, args.size_frac)
        rows, decomp = trade_ledger(r, args.size_frac)
        st_full = signal_series(df, p)
        st_dev, st_ho = st_full[df.index < hold], st_full[df.index >= hold]
        bpd = _bpd(df)
        entry = {"params": p, "holdout_metrics": r.metrics, "decomposition": decomp, "trades": rows,
                 "time_in_market_pct": {"dev": round(float(st_dev.mean()) * 100, 1), "holdout": round(float(st_ho.mean()) * 100, 1)},
                 "signal_quality_5d": {"dev": forward_return_split(dev, st_dev, 5 * bpd), "holdout": forward_return_split(ho, st_ho, 5 * bpd)},
                 "signal_quality_20d": {"dev": forward_return_split(dev, st_dev, 20 * bpd), "holdout": forward_return_split(ho, st_ho, 20 * bpd)}}
        # asset return captured: product of bar returns while in-signal vs the asset's total move
        br = df["close"].pct_change().fillna(0.0)
        cap_ho = float(np.prod(1 + br[df.index >= hold][st_ho]) - 1) * 100
        cap_dev = float(np.prod(1 + br[df.index < hold][st_dev]) - 1) * 100
        entry["asset_return_while_in_signal_pct"] = {"dev": round(cap_dev, 1), "holdout": round(cap_ho, 1)}
        entry["asset_return_total_pct"] = {"dev": round((float(dev["close"].iloc[-1]) / float(dev["close"].iloc[WARMUP]) - 1) * 100, 1),
                                           "holdout": round((float(ho["close"].iloc[-1]) / float(ho["close"].iloc[0]) - 1) * 100, 1)}
        report["configs"][label] = entry
        looks.append({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "symbol": args.symbol, "holdout_start": args.holdout_start,
                      "strategy": "tsmom_ema_filter", "params": p, "purpose": "diagnosis of a configuration fixed before the holdout was opened",
                      "net_return_pct": r.metrics["net_return_pct"]})
    report["path_stats"] = {"dev_by_year": {str(y): path_stats(dev[dev.index.year == y], 30) for y in sorted(set(dev.index.year)) if (dev.index.year == y).sum() > 300},
                            "dev_all": path_stats(dev.iloc[WARMUP:], 30), "holdout": path_stats(ho, 30)}
    months = int(round((ho.index[-1] - ho.index[0]).days / 30.44))
    win = rolling_windows(dev, DEPLOYED, args.size_frac, months)
    rets = np.array([w["net_return_pct"] for w in win])
    hres = report["configs"]["deployed_30_100"]["holdout_metrics"]["net_return_pct"]
    report["rolling_context"] = {"window_months": months, "windows": len(win), "min_pct": round(float(rets.min()), 1), "p10_pct": round(float(np.percentile(rets, 10)), 1),
                                 "median_pct": round(float(np.median(rets)), 1), "p90_pct": round(float(np.percentile(rets, 90)), 1), "max_pct": round(float(rets.max()), 1),
                                 "share_of_windows_below_1pct": round(float((rets < 1.0).mean()) * 100, 1),
                                 "holdout_percentile": round(float((rets < hres).mean()) * 100, 1), "sample": win[::3]}
    os.makedirs(args.out, exist_ok=True)
    with open(f"{args.out}/holdout_diagnosis_{args.symbol}.json", "w") as f:
        json.dump(report, f, indent=1, default=str)
    with open(f"{args.out}/holdout_looks.jsonl", "a") as f:
        for lk in looks:
            f.write(json.dumps(lk) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "configs"}, indent=1, default=str))
    for label, e in report["configs"].items():
        print(f"\n== {label} {e['params']}")
        print(" holdout:", {k: e["holdout_metrics"][k] for k in ("trades", "win_rate_pct", "profit_factor", "net_return_pct", "max_drawdown_pct", "avg_bars_held")})
        print(" decomposition:", {k: v for k, v in e["decomposition"].items() if k != "trades"})
        print(" time in market:", e["time_in_market_pct"], " asset while in-signal:", e["asset_return_while_in_signal_pct"], " asset total:", e["asset_return_total_pct"])
        print(" signal 5d:", e["signal_quality_5d"]); print(" signal 20d:", e["signal_quality_20d"])
        for t in e["trades"]:
            print("  ", t)


if __name__ == "__main__":
    main()
