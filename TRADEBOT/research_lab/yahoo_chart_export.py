"""Export Binance 1-minute klines as a Yahoo v8 chart JSON of 5-minute bars, for the vendored Time Machine skill.

The Time Machine (`.claude/skills/trader-timemachine`) reads Yahoo chart JSON with `--offline FILE`. This converts the
verified Binance data TRADEBOT already uses, so the skill runs without network access and on the same prices as the
research lab. Read-only: it writes one .json.gz file and nothing else.

Crypto caveats that the output does NOT fix (see research_lab/DAY_TRADER_SKILL_REVIEW.md):
  - the skill only replays 9:30-16:00 New York time and skips weekends and NYSE holidays (24/7 market);
  - its "average R" includes no trading costs; subtract the round trip in R yourself:
        cost_in_R = round_trip_bps / 1e4 * entry / (entry - stop)

usage (from TRADEBOT/):
  python -m research_lab.yahoo_chart_export --symbol SOL-USD --days 60 --end 2026-10-21 \
      --out trader/SOL-USD-60d5m.json.gz "<path>/SOLUSDT_1m_2026-08.csv" "<path>/SOLUSDT_1m_2026-09.csv" ...
  python .claude/skills/trader-timemachine/timemachine.py run --offline trader/SOL-USD-60d5m.json.gz \
      --symbol SOL-USD --date 2026-10-20 --entry 104.76 --stop 104.28 --target 105.24 --entry-time "9:50 AM" --now "2026-10-20 09:50"
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import glob
import gzip
import json
import os


def read_1m(paths: list[str]) -> dict[int, list[float]]:
    """-> {5-minute bucket start (unix s): [open, high, low, close, volume]} from Binance 1m CSVs.
    Accepts the official 12-column files (no header; ms or us timestamps) and header CSVs whose first column is a
    millisecond timestamp followed by a datetime column (open in column 3)."""
    bars: dict[int, list[float]] = {}
    for path in sorted(p for g in paths for p in glob.glob(g)):
        with open(path, newline="") as fh:
            for row in csv.reader(fh):
                if not row or not row[0].strip().isdigit():
                    continue
                t = int(row[0])
                t = t // 1_000_000 if t > 10**15 else (t // 1000 if t > 10**11 else t)
                off = 2 if not row[1].replace(".", "").isdigit() else 1        # header CSV has a datetime column
                o, h, l, c, v = (float(x) for x in row[off:off + 5])
                b = t - t % 300
                x = bars.get(b)
                bars[b] = [o, h, l, c, v] if x is None else [x[0], max(x[1], h), min(x[2], l), c, x[4] + v]
    return bars


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--symbol", required=True, help="name shown by the skill, e.g. SOL-USD")
    ap.add_argument("--days", type=int, default=60, help="calendar days before --end to keep (the skill expects ~60)")
    ap.add_argument("--end", default=None, help="YYYY-MM-DD, exclusive; default: after the last bar")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    bars = read_1m(a.files)
    if not bars:
        raise SystemExit("no bars read")
    end = int(dt.datetime.fromisoformat(a.end).replace(tzinfo=dt.UTC).timestamp()) if a.end else max(bars) + 300
    start = end - a.days * 86400
    ts = [t for t in sorted(bars) if start <= t < end]
    res = {"meta": {"symbol": a.symbol, "dataGranularity": "5m", "range": f"{a.days}d", "exchangeTimezoneName": "America/New_York",
                    "regularMarketTime": ts[-1], "instrumentType": "CRYPTOCURRENCY", "currency": "USD", "source": "Binance spot 1m klines (TRADEBOT research data)"},
           "timestamp": ts,
           "indicators": {"quote": [{k: [bars[t][i] for t in ts] for i, k in enumerate(("open", "high", "low", "close", "volume"))}]}}
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with gzip.open(a.out, "wt") as fh:
        json.dump({"chart": {"result": [res], "error": None}}, fh)
    print(f"{a.symbol}: {len(ts)} five-minute bars {dt.datetime.fromtimestamp(ts[0], dt.UTC):%Y-%m-%d %H:%M} .. "
          f"{dt.datetime.fromtimestamp(ts[-1], dt.UTC):%Y-%m-%d %H:%M} UTC -> {a.out}")


if __name__ == "__main__":
    main()
