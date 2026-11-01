"""Load Binance monthly kline CSV dumps (official data.binance.vision format) from one or more source
directories, merge them into one continuous hourly series per symbol, and validate.

Validation: monotonic timestamps, no duplicates, gap report, OHLC sanity (low <= open/close <= high,
positive prices), and cross-source agreement on overlapping months (max abs close deviation).
"""
from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "count",
        "taker_buy_volume", "taker_buy_quote_volume", "ignore"]
_FNAME = re.compile(r"([A-Z]+USDT)-1h-(\d{4}-\d{2})\.csv$")


@dataclass
class LoadReport:
    symbol: str
    sources: dict[str, int] = field(default_factory=dict)   # source -> months used
    months: int = 0
    bars: int = 0
    start: str = ""
    end: str = ""
    gaps: list[tuple[str, str, int]] = field(default_factory=list)   # (from, to, missing bars)
    max_overlap_close_dev_pct: float = 0.0
    overlap_months_checked: int = 0
    sanity_errors: int = 0


def _read_month(path: str) -> pd.DataFrame:
    with open(path) as f:
        first = f.readline()
    header = 0 if first.lower().startswith("open_time") else None
    df = pd.read_csv(path, header=header, names=None if header == 0 else COLS, usecols=range(6) if header is None else None)
    df = df.iloc[:, :6]
    df.columns = ["open_time", "open", "high", "low", "close", "volume"]
    ot = df["open_time"].astype("int64")
    if ot.iloc[0] > 1e14:      # microseconds (2025+ dumps)
        ot = ot // 1000
    df["ts"] = pd.to_datetime(ot, unit="ms", utc=True)
    df = df.drop(columns=["open_time"]).set_index("ts").astype(float)
    return df


def discover(source_dirs: list[str]) -> dict[str, dict[str, dict[str, str]]]:
    """symbol -> month -> {source_name: path}"""
    out: dict[str, dict[str, dict[str, str]]] = {}
    for src in source_dirs:
        name = os.path.basename(src.rstrip("/"))
        for p in glob.glob(os.path.join(src, "**", "*USDT-1h-*.csv"), recursive=True):
            m = _FNAME.search(p)
            if not m:
                continue
            out.setdefault(m.group(1), {}).setdefault(m.group(2), {})[name] = p
    return out


def load_symbol(symbol: str, files: dict[str, dict[str, str]], prefer: list[str] | None = None,
                start: str | None = None, end: str | None = None) -> tuple[pd.DataFrame, LoadReport]:
    rep = LoadReport(symbol)
    frames = []
    devs = []
    for month in sorted(files):
        if (start and month < start[:7]) or (end and month > end[:7]):
            continue
        srcs = files[month]
        order = [s for s in (prefer or []) if s in srcs] + [s for s in srcs if s not in (prefer or [])]
        primary = order[0]
        df = _read_month(srcs[primary])
        rep.sources[primary] = rep.sources.get(primary, 0) + 1
        for other in order[1:]:
            try:
                o = _read_month(srcs[other])
                j = df[["close"]].join(o[["close"]], how="inner", rsuffix="_o")
                if len(j) > 24:
                    devs.append(float(((j["close"] - j["close_o"]).abs() / j["close"]).max() * 100))
                    rep.overlap_months_checked += 1
            except Exception:  # noqa: BLE001 - a bad secondary file must not block the primary
                pass
        frames.append(df)
    if not frames:
        raise FileNotFoundError(f"no data for {symbol}")
    full = pd.concat(frames).sort_index()
    full = full[~full.index.duplicated(keep="first")]
    bad = (full["low"] > full[["open", "close"]].min(axis=1)) | (full["high"] < full[["open", "close"]].max(axis=1)) | (full["close"] <= 0)
    rep.sanity_errors = int(bad.sum())
    full = full[~bad]
    # gaps
    diffs = full.index.to_series().diff().dropna()
    for ts, d in diffs[diffs > pd.Timedelta(hours=1)].items():
        missing = int(d / pd.Timedelta(hours=1)) - 1
        rep.gaps.append((str(full.index[full.index.get_loc(ts) - 1])[:16], str(ts)[:16], missing))
    rep.months, rep.bars = len(frames), len(full)
    rep.start, rep.end = str(full.index[0])[:16], str(full.index[-1])[:16]
    rep.max_overlap_close_dev_pct = round(max(devs), 4) if devs else 0.0
    full.index.name = "ts"
    return full, rep


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample an hourly OHLCV frame to e.g. '4h' or '1D' (right-closed bars labelled by open time)."""
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    out = df.resample(rule, label="left", closed="left").agg(agg).dropna()
    return out


def format_report(rep: LoadReport) -> str:
    gaps = ", ".join(f"{a}->{b} ({n}h)" for a, b, n in rep.gaps[:6]) + (" ..." if len(rep.gaps) > 6 else "")
    return (f"{rep.symbol}: {rep.bars} bars {rep.start} .. {rep.end} from {rep.months} monthly files "
            f"{rep.sources}; gaps={len(rep.gaps)} [{gaps}]; cross-source max close dev {rep.max_overlap_close_dev_pct}% "
            f"over {rep.overlap_months_checked} overlapping months; OHLC sanity errors dropped={rep.sanity_errors}")
