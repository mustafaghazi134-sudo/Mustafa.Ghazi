"""Export the merged research series as tradebot CSV replay files (<TAG>_<interval>.csv) for whole-bot replays.
usage: PYTHONPATH=. python -m research_lab.export_csv <data_raw_dir> <out_dir>"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

from .data import discover, load_symbol, resample, split_on_gaps

PREFER = ["changhaowuwu_LSTM-live-cripto-forecast", "spurs7216_Quant_Research", "7huahua_RedditDataset"]


def main() -> None:
    raw, out = Path(sys.argv[1]), Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    files = discover([str(p) for p in raw.iterdir() if p.is_dir()])
    epoch = pd.Timestamp(0, tz="UTC")
    for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        df, _ = load_symbol(sym, files[sym], prefer=PREFER)
        blocks = split_on_gaps(df)
        for k, b in enumerate(blocks):
            tag = sym.replace("USDT", "") + ("" if len(blocks) == 1 else f"B{k+1}")
            for iv, rule in (("1h", None), ("4h", "4h"), ("1d", "1D")):
                o = (b if rule is None else resample(b, rule)).reset_index()
                o["ts"] = ((o["ts"] - epoch) // pd.Timedelta(seconds=1)).astype("int64")
                o[["ts", "open", "high", "low", "close", "volume"]].to_csv(out / f"{tag}_{iv}.csv", index=False)
            print(tag, len(b), str(b.index[0])[:10], str(b.index[-1])[:10])


if __name__ == "__main__":
    main()
