"""Integrity audit of the 1-minute Binance datasets used by the fast-strategy research.

Per dataset: coverage, duplicates, gaps, impossible candles (OHLC ordering, non-positive prices), zero-volume share,
extreme 1-minute moves, spike-and-revert patterns typical of bad ticks, and extreme wicks. Across datasets: exact
agreement of overlapping minutes (an independent copy of the same exchange data must match byte for byte).

usage (from TRADEBOT/): PYTHONPATH=$PWD python -m research_lab.data_audit --root <data_1m dir> --out research_lab/results/data_audit.json
"""
from __future__ import annotations

import argparse
import glob
import json
import os

import numpy as np
import pandas as pd

COLS12 = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "trades", "tbb", "tbq", "ignore"]


def read_raw(files: list[str]) -> pd.DataFrame:
    """No de-duplication or sorting: the audit must see the data as stored."""
    frames = []
    for f in sorted(files):
        head = pd.read_csv(f, nrows=1, header=None)
        has_header = not str(head.iloc[0, 0]).replace(".", "").isdigit()
        df = pd.read_csv(f, header=0 if has_header else None)
        if not has_header:
            df.columns = COLS12[: df.shape[1]]
        df.columns = [str(c).strip().lower() for c in df.columns]
        tcol = "open_time" if "open_time" in df.columns else "timestamp"
        t = df[tcol].astype("int64")
        t = np.where(t > 10**15, t // 1000, t)              # microseconds (official files from 2025) -> ms
        out = pd.DataFrame({"ts": t, "open": df["open"].astype(float), "high": df["high"].astype(float), "low": df["low"].astype(float),
                            "close": df["close"].astype(float), "volume": df["volume"].astype(float)})
        out["file"] = os.path.basename(f)
        frames.append(out)
    return pd.concat(frames, ignore_index=True)


def audit(df: pd.DataFrame, name: str) -> dict:
    r: dict = {"dataset": name, "rows": int(len(df))}
    r["duplicates"] = int(df["ts"].duplicated().sum())
    r["non_monotonic"] = int((df["ts"].diff().fillna(60_000) <= 0).sum())
    d = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    idx = pd.to_datetime(d["ts"], unit="ms", utc=True)
    r["first"], r["last"] = str(idx.iloc[0])[:16], str(idx.iloc[-1])[:16]
    r["misaligned_to_minute"] = int((d["ts"] % 60_000 != 0).sum())
    step = d["ts"].diff().dropna() / 60_000
    gaps = step[step > 1]
    r["gaps"] = int(len(gaps))
    r["missing_minutes"] = int((gaps - 1).sum())
    if len(gaps):
        top = gaps.sort_values(ascending=False).head(3)
        r["largest_gaps"] = [f"{idx.iloc[i - 1]:%Y-%m-%d %H:%M} -> {idx.iloc[i]:%H:%M} ({int(v) - 1} min)" for i, v in top.items()]
    o, h, l, c, v = (d[k].to_numpy() for k in ("open", "high", "low", "close", "volume"))
    r["nonpositive_prices"] = int(((o <= 0) | (h <= 0) | (l <= 0) | (c <= 0)).sum())
    r["ohlc_violations"] = int(((l > np.minimum(o, c) + 1e-12) | (h < np.maximum(o, c) - 1e-12) | (l > h)).sum())
    r["zero_volume_pct"] = round(float((v == 0).mean()) * 100, 3)
    ret = np.diff(c) / c[:-1]
    r["abs_1m_return_p99_bps"] = round(float(np.percentile(np.abs(ret), 99)) * 1e4, 1)
    big = np.where(np.abs(ret) > 0.05)[0]
    r["moves_gt_5pct"] = int(len(big))
    r["largest_moves"] = [f"{idx.iloc[i + 1]:%Y-%m-%d %H:%M} {ret[i] * 100:+.2f}%" for i in np.argsort(-np.abs(ret))[:3]]
    # spike-and-revert: >=2% one way then >=80% of it back the next minute (typical of a bad tick)
    rev = [i for i in range(len(ret) - 1) if abs(ret[i]) >= 0.02 and np.sign(ret[i + 1]) == -np.sign(ret[i]) and abs(ret[i + 1]) >= 0.8 * abs(ret[i])]
    r["spike_and_revert"] = len(rev)
    r["spike_and_revert_examples"] = [f"{idx.iloc[i + 1]:%Y-%m-%d %H:%M} {ret[i] * 100:+.2f}% then {ret[i + 1] * 100:+.2f}%" for i in rev[:3]]
    wick = (h - l) / np.where(c > 0, c, np.nan)
    r["wicks_gt_5pct"] = int((wick > 0.05).sum())
    return r


def compare(a: pd.DataFrame, b: pd.DataFrame, name: str) -> dict:
    a = a.drop_duplicates("ts").set_index("ts")
    b = b.drop_duplicates("ts").set_index("ts")
    j = a.join(b, how="inner", lsuffix="_a", rsuffix="_b")
    out = {"pair": name, "overlap_minutes": int(len(j))}
    if not len(j):
        return out
    for k in ("open", "high", "low", "close", "volume"):
        diff = (j[f"{k}_a"] - j[f"{k}_b"]).abs()
        out[f"{k}_mismatches"] = int((diff > 1e-9 * j[f"{k}_b"].abs().clip(lower=1)).sum())
    out["max_close_rel_diff"] = float(((j["close_a"] - j["close_b"]).abs() / j["close_b"]).max())
    only_a, only_b = len(a.index.difference(b.index)), len(b.index.difference(a.index))
    out["minutes_only_in_first"], out["minutes_only_in_second"] = int(only_a), int(only_b)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", default="research_lab/results/data_audit.json")
    a = ap.parse_args()
    R = a.root
    sets = {}
    for sym in ("SOLUSDT", "BTCUSDT", "ETHUSDT"):
        fs = glob.glob(f"{R}/parshant231234-eng_{sym}-Historical-Data/1m/*/*/*.csv")
        if fs:
            sets[f"{sym} parshant (primary)"] = read_raw(fs)
        fs = glob.glob(f"{R}/JienWeng_crypto-execution-timing/data/raw/{sym}-1m-*.zip")
        if fs:
            sets[f"{sym} official zips (JienWeng)"] = read_raw(fs)
    fs = glob.glob(f"{R}/metatim89-a11y_solpred/data/historical/raw/zip/SOLUSDT-1m-*.zip")
    if fs:
        sets["SOLUSDT official zips (metatim)"] = read_raw(fs)
    audits = [audit(df, n) for n, df in sets.items()]
    comps = []
    for sym in ("SOLUSDT", "BTCUSDT", "ETHUSDT"):
        prim = sets.get(f"{sym} parshant (primary)")
        for other in (f"{sym} official zips (JienWeng)", f"{sym} official zips (metatim)"):
            if prim is not None and other in sets:
                comps.append(compare(prim, sets[other], f"{sym}: parshant vs {other.split(' ', 1)[1]}"))
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w") as f:
        json.dump({"audits": audits, "comparisons": comps}, f, indent=1)
    for r in audits:
        print(json.dumps(r))
    for c in comps:
        print(json.dumps(c))


if __name__ == "__main__":
    main()
