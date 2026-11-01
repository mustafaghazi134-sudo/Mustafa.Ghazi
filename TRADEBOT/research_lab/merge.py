"""Merge several results.json files (same data, different strategy batches) into one."""
from __future__ import annotations

import json
import sys


def merge(paths: list[str]) -> dict:
    out = None
    for p in paths:
        r = json.load(open(p))
        if out is None:
            out = r
            continue
        out["strategies"].update(r["strategies"])
        out.setdefault("dca_martingale_demo", {}).update(r.get("dca_martingale_demo", {}))
    return out


if __name__ == "__main__":
    json.dump(merge(sys.argv[2:]), open(sys.argv[1], "w"), indent=1, default=str)
    print("merged ->", sys.argv[1])
