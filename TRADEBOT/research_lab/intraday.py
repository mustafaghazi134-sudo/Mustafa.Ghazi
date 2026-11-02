"""1-minute / 5-minute candidate strategies vs the deployed 4h baseline on the SAME unseen period, after realistic costs.

Input: Binance-format 1m kline CSVs (official 12-column monthly files, header optional) or any CSV with
open_time/open/high/low/close/volume columns. Everything is resampled from 1m, so the 4h baseline, the 5m candidates
and the 1m candidates see identical price history.

Costs: fee_bps + slip_bps per side (default 10 + 15 as in the rest of research_lab) PLUS a fixed network fee per side
in USD (default $0.01), which matters at small sizes and is the reason the capital level is a parameter here.

Walk-forward: parameters are chosen on `train_days` and applied to the following `test_days`, rolling; the report is
out-of-sample only. Lookahead check: shifting signals by one bar must not improve results.

usage (from TRADEBOT/):
  PYTHONPATH=$PWD python -m research_lab.intraday --files <1m csvs...> --symbol SOLUSDT --capital 100 --out research_lab/results/intraday_SOLUSDT.json
"""
from __future__ import annotations

import argparse
import glob
import json
import os

import numpy as np
import pandas as pd

from . import indicators as I
from .engine import ExitSpec, compute_metrics, run_backtest
from .strategies import REGISTRY

COLS12 = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore"]
NETWORK_FEE_USD_PER_TX = 0.01


def load_1m(files: list[str]) -> pd.DataFrame:
    frames = []
    for f in files:
        head = pd.read_csv(f, nrows=1, header=None)
        has_header = not str(head.iloc[0, 0]).replace(".", "").isdigit()
        df = pd.read_csv(f, header=0 if has_header else None)
        if not has_header and df.shape[1] >= 6:
            df.columns = COLS12[: df.shape[1]]
        df.columns = [str(c).strip().lower() for c in df.columns]
        tcol = next(c for c in df.columns if c in ("open_time", "timestamp", "time", "date", "datetime", "ts"))
        t = df[tcol]
        if np.issubdtype(t.dtype, np.number):
            unit = "us" if t.iloc[0] > 1e15 else ("ms" if t.iloc[0] > 1e11 else "s")
            idx = pd.to_datetime(t, unit=unit, utc=True)
        else:
            idx = pd.to_datetime(t, utc=True)
        out = df[["open", "high", "low", "close", "volume"]].astype(float)
        out.index = idx
        frames.append(out)
    df = pd.concat(frames).sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return df


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    return df.resample(rule, label="left", closed="left").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()


# ---- candidates (1m / 5m), pure functions returning (entries, exits, ExitSpec) ---------------------------------
def c_breakout_5m(df, p):
    c = df["close"]; hi = df["high"].rolling(p["lookback"]).max().shift(1)
    entries = (c > hi * (1 + p["confirm_bps"] / 1e4)) & (c > c.shift(1))
    exits = c < df["low"].rolling(p["exit_lookback"]).min().shift(1)
    return entries, exits, ExitSpec(stop_loss_pct=p["stop_pct"], max_hold_bars=p["max_bars"])


def c_dip_reversion_1m(df, p):
    r = df["close"].pct_change()
    z = (r - r.rolling(p["lookback"]).mean()) / r.rolling(p["lookback"]).std()
    entries = z < -p["z"]
    exits = df["close"] > df["close"].rolling(p["lookback"]).mean()
    return entries, exits, ExitSpec(stop_loss_pct=p["stop_pct"], max_hold_bars=p["max_bars"])


def c_ema_cross_5m(df, p):
    c = df["close"]; f = I.ema(c, p["fast"]); s = I.ema(c, p["slow"])
    return (f > s) & (f.shift(1) <= s.shift(1)), (f < s), ExitSpec(stop_loss_pct=p["stop_pct"])


def c_vwap_pullback_5m(df, p):
    """Trend by EMA, buy a pullback to the rolling VWAP, exit above VWAP + k x ATR-like range or on trend loss."""
    c = df["close"]; v = df["volume"].replace(0, np.nan)
    vwap = (c * v).rolling(p["vwap_bars"]).sum() / v.rolling(p["vwap_bars"]).sum()
    trend = I.ema(c, p["trend"])
    entries = (c > trend) & (c < vwap) & (c.shift(1) >= vwap.shift(1))
    exits = (c < trend) | (c > vwap * (1 + p["target_bps"] / 1e4))
    return entries, exits, ExitSpec(stop_loss_pct=p["stop_pct"], max_hold_bars=p["max_bars"])


CANDIDATES = {
    "breakout_5m": ("5min", c_breakout_5m, {"lookback": [12, 24], "confirm_bps": [5, 15], "exit_lookback": [6], "stop_pct": [1.0, 2.0], "max_bars": [12]}),
    "dip_reversion_1m": ("1min", c_dip_reversion_1m, {"lookback": [20, 60], "z": [2.5, 3.5], "stop_pct": [1.0], "max_bars": [15]}),
    "ema_cross_5m": ("5min", c_ema_cross_5m, {"fast": [5, 9], "slow": [21, 34], "stop_pct": [1.5, 3.0]}),
    "vwap_pullback_5m": ("5min", c_vwap_pullback_5m, {"vwap_bars": [48, 96], "trend": [100], "target_bps": [30, 60], "stop_pct": [1.0, 2.0], "max_bars": [24]}),
}


def _grid(g: dict) -> list[dict]:
    out = [{}]
    for k, vs in g.items():
        out = [{**o, k: v} for o in out for v in vs]
    return out


def _run(fn, df, p, fee_bps, slip_bps, bar_minutes, capital, warmup):
    entries, exits, spec = fn(df, p)
    entries = entries.copy(); entries.iloc[:warmup] = False
    # fixed network fees expressed as extra bps of the position size
    extra = NETWORK_FEE_USD_PER_TX / capital * 1e4
    return run_backtest(df, entries, exits, spec, fee_bps=fee_bps + extra, slippage_bps=slip_bps, initial=capital, size_frac=1.0, bar_minutes=bar_minutes)


def walk_forward(name, df, fee, slip, capital, train_days, test_days, warmup=200):
    rule, fn, grid = CANDIDATES[name]
    bar_minutes = 1 if rule == "1min" else 5
    start = df.index[0] + pd.Timedelta(days=train_days)
    eq_parts, trades, segs = [], [], []
    while start + pd.Timedelta(days=test_days) <= df.index[-1]:
        train = df[(df.index >= start - pd.Timedelta(days=train_days)) & (df.index < start)]
        test = df[(df.index >= start - pd.Timedelta(minutes=bar_minutes * warmup)) & (df.index < start + pd.Timedelta(days=test_days))]
        best, best_score = None, -1e9
        for p in _grid(grid):
            r = _run(fn, train, p, fee, slip, bar_minutes, capital, warmup)
            m = compute_metrics(r, capital, bar_minutes)
            if m["trades"] < 10:
                continue
            score = m["net_return_pct"] - 0.5 * abs(m["max_drawdown_pct"])
            if score > best_score:
                best, best_score = p, score
        if best is not None:
            r = _run(fn, test, best, fee, slip, bar_minutes, capital, warmup)
            r.equity = r.equity.iloc[warmup:]; r.trades = [t for t in r.trades if t.entry_i >= warmup]
            eq_parts.append(r.equity / float(r.equity.iloc[0]) if len(r.equity) else r.equity)
            trades += r.trades
            segs.append({"test_start": str(start)[:10], "params": best, "trades": len(r.trades)})
        start += pd.Timedelta(days=test_days)
    if not eq_parts:
        return {"note": "no window produced >= 10 training trades"}
    chained = []
    level = capital
    for e in eq_parts:
        chained.append(e * level); level = float(chained[-1].iloc[-1])
    eq = pd.concat(chained)
    from .engine import Result
    res = Result(trades=trades, equity=eq)
    m = compute_metrics(res, capital, bar_minutes)
    days = max((eq.index[-1] - eq.index[0]).days, 1)
    m["trades_per_day"] = round(len(trades) / days, 2)
    m["segments"] = segs
    return m


def lookahead_check(name, df, fee=None, slip=None, capital=None, warmup=200):
    """Signals at bar t must be identical whether or not bars after t exist (same test as research_lab/checks.py)."""
    rule, fn, grid = CANDIDATES[name]
    p = {k: v[0] for k, v in grid.items()}
    cut = int(len(df) * 0.7)
    e_full, x_full, _ = fn(df, p)
    e_cut, x_cut, _ = fn(df.iloc[:cut], p)
    e_full, x_full = e_full.fillna(False).astype(bool), x_full.fillna(False).astype(bool)
    e_cut, x_cut = e_cut.fillna(False).astype(bool), x_cut.fillna(False).astype(bool)
    diff = int((e_full.iloc[:cut].to_numpy() != e_cut.to_numpy()).sum() + (x_full.iloc[:cut].to_numpy() != x_cut.to_numpy()).sum())
    return {"differing_signal_bars": diff, "passed": diff == 0}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", nargs="+", required=True, help="1m CSV files or globs")
    ap.add_argument("--symbol", default="SOLUSDT")
    ap.add_argument("--capital", type=float, default=100.0)
    ap.add_argument("--fee-bps", type=float, default=10.0); ap.add_argument("--slip-bps", type=float, default=15.0)
    ap.add_argument("--train-days", type=int, default=30); ap.add_argument("--test-days", type=int, default=7)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    files = sorted(f for g in args.files for f in glob.glob(g))
    m1 = load_1m(files)
    frames = {"1min": m1, "5min": resample(m1, "5min"), "4h": resample(m1, "4h")}
    span = (str(m1.index[0])[:16], str(m1.index[-1])[:16])
    print(f"{args.symbol}: {len(m1)} 1m bars {span[0]} .. {span[1]}; 5m {len(frames['5min'])}; 4h {len(frames['4h'])}; capital ${args.capital:g}")
    out = {"symbol": args.symbol, "span": span, "capital": args.capital, "costs": {"fee_bps": args.fee_bps, "slip_bps": args.slip_bps, "network_fee_usd": NETWORK_FEE_USD_PER_TX},
           "candidates": {}, "baseline": {}}
    for name, (rule, _fn, _g) in CANDIDATES.items():
        wf = walk_forward(name, frames[rule], args.fee_bps, args.slip_bps, args.capital, args.train_days, args.test_days)
        la = lookahead_check(name, frames[rule], args.fee_bps, args.slip_bps, args.capital)
        out["candidates"][name] = {"timeframe": rule, "walk_forward_oos": wf, "lookahead": la}
        if "trades" in wf:
            print(f"  {name:<18} {rule:<5} OOS trades {wf['trades']:>5} ({wf['trades_per_day']}/day)  win {wf['win_rate_pct']}%  PF {wf['profit_factor']}  "
                  f"net {wf['net_return_pct']:>7.1f}%  DD {wf['max_drawdown_pct']}%  lookahead={'ok' if la['passed'] else 'FAIL'}")
        else:
            print(f"  {name:<18} {rule:<5} {wf['note']}")
    # baseline: deployed 4h TSMOM+EMA on the same period (fixed deployed parameters; data is too short for walk-forward)
    df4 = frames["4h"]
    if len(df4) > 650:
        fn = REGISTRY["tsmom_ema_filter"]["fn"]
        entries, exits, spec = fn(df4, {"lookback_days": 30, "ema_days": 100, "vol_stop_pct": 25.0})
        entries = entries.copy(); entries.iloc[:600] = False
        r = run_backtest(df4, entries, exits, spec, fee_bps=args.fee_bps + NETWORK_FEE_USD_PER_TX / args.capital * 1e4, slippage_bps=args.slip_bps, initial=args.capital, size_frac=1.0, bar_minutes=240)
        out["baseline"] = compute_metrics(r, args.capital, 240)
        print(f"  baseline tsmom_ema_filter 4h (deployed params, 100% sizing for comparability): trades {out['baseline']['trades']}  net {out['baseline']['net_return_pct']}%  DD {out['baseline']['max_drawdown_pct']}%")
    else:
        out["baseline"] = {"note": f"only {len(df4)} 4h bars: the 4h baseline needs ~650 for its 100-day EMA warm-up; period too short for a fair comparison"}
        print("  baseline: period too short for the 4h strategy's warm-up (needs ~110 days)")
    bh = (float(m1['close'].iloc[-1]) / float(m1['close'].iloc[0]) - 1) * 100
    out["buy_hold_pct"] = round(bh, 2); print(f"  buy & hold over the period: {bh:+.1f}%")
    if args.out:
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        with open(args.out, "w") as f:
            json.dump(out, f, indent=1, default=str)


if __name__ == "__main__":
    main()
