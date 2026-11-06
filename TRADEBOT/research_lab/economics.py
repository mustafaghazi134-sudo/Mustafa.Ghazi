"""Is 20 / 50 / 100 profitable trades a day economically plausible at $5 / $50 / $100 / $1,000?

Deterministic arithmetic, no forecasts. For every (capital, trades/day) cell it reports the per-trade cost floor, the
gross edge per trade needed to clear it, and how that edge compares with the typical short-horizon move of the asset.

Cost inputs, in order of preference:
  1. measured worst-case round-trip probes from the scanner database (data/scanner.db, `tradebot scan`), per size;
  2. otherwise the documented assumptions below (flagged ASSUMED in the output).
Move inputs: realised volatility from the 1h research data, scaled to 1-minute and 5-minute horizons by sqrt(time).
That scaling is an approximation (intraday vol clusters and has intraday seasonality); when 1-minute data is present
(research_lab/intraday.py), the measured 1m/5m absolute moves replace it.

usage (from TRADEBOT/): PYTHONPATH=$PWD python -m research_lab.economics --data-dirs <dirs> [--scanner-db data/scanner.db]
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3

import pandas as pd

from .data import discover, load_symbol

CAPITALS = [5.0, 50.0, 100.0, 1000.0]
TRADES_PER_DAY = [20, 50, 100]
NETWORK_FEE_USD_PER_TX = 0.01
# ASSUMED when no probes exist: variable round-trip cost on liquid Solana pairs at small size (spread + impact +
# Jupiter fee, both legs). Majors (SOL/USDC) at <= $1k are typically single-digit bps per leg; this is deliberately
# not optimistic for a taker crossing the spread twice.
ASSUMED_VARIABLE_RT_BPS = {5.0: 20.0, 50.0: 20.0, 100.0: 20.0, 1000.0: 30.0}
ADVERSE_SELECTION_BPS = 10.0       # the fill you get is the one nobody else wanted; applied per round trip
POSITION_FRACTION = 1.0            # intraday: whole capital per trade (10% sizing makes the fixed fee 10x worse)


def measured_costs(db_path: str | None) -> dict[float, dict]:
    if not db_path:
        return {}
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT size_usd, AVG(worst_case_bps), AVG(expected_bps), COUNT(*) FROM cost_probes GROUP BY size_usd").fetchall()
    except sqlite3.Error:
        return {}
    return {float(r[0]): {"worst_bps": r[1], "expected_bps": r[2], "n": r[3]} for r in rows}


def horizon_moves(df: pd.DataFrame) -> dict:
    """Typical absolute move over 1 and 5 minutes implied by hourly realised vol (sqrt scaling, approximation)."""
    r = df["close"].pct_change().dropna()
    sd_1h = float(r.std())
    return {"sd_1h_bps": round(sd_1h * 1e4, 1), "sd_1m_bps_approx": round(sd_1h / math.sqrt(60) * 1e4, 1),
            "sd_5m_bps_approx": round(sd_1h / math.sqrt(12) * 1e4, 1), "mean_abs_1h_bps": round(float(r.abs().mean()) * 1e4, 1)}


def cell(capital: float, n_per_day: int, variable_rt_bps: float, measured: bool) -> dict:
    size = capital * POSITION_FRACTION
    fixed_bps = 2 * NETWORK_FEE_USD_PER_TX / size * 1e4
    floor = variable_rt_bps + fixed_bps + ADVERSE_SELECTION_BPS
    daily_cost_pct = floor * n_per_day / 1e4 * 100
    return {"capital_usd": capital, "trades_per_day": n_per_day, "size_usd": size, "fixed_fee_bps": round(fixed_bps, 1),
            "variable_rt_bps": round(variable_rt_bps, 1), "cost_floor_bps_per_trade": round(floor, 1),
            "daily_cost_pct_of_capital": round(daily_cost_pct, 1),
            "gross_edge_needed_for_1pct_day_bps": round(floor + 100.0 / n_per_day, 1), "cost_source": "measured" if measured else "ASSUMED"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dirs", nargs="+", default=None)
    ap.add_argument("--symbol", default="SOLUSDT")
    ap.add_argument("--scanner-db", default=None)
    ap.add_argument("--out", default="research_lab/results/economics.json")
    args = ap.parse_args()
    probes = measured_costs(args.scanner_db)
    moves = None
    if args.data_dirs:
        files = discover(args.data_dirs)
        if args.symbol in files:
            df, _ = load_symbol(args.symbol, files[args.symbol])
            moves = horizon_moves(df[df.index >= df.index[-1] - pd.DateOffset(months=12)])
    cells = []
    for cap in CAPITALS:
        p = probes.get(cap)
        var = p["worst_bps"] if p else ASSUMED_VARIABLE_RT_BPS[cap]
        for n in TRADES_PER_DAY:
            cells.append(cell(cap, n, var, bool(p)))
    out = {"symbol_for_moves": args.symbol, "moves": moves, "probes": probes, "assumptions": {
        "network_fee_usd_per_tx": NETWORK_FEE_USD_PER_TX, "assumed_variable_rt_bps": ASSUMED_VARIABLE_RT_BPS,
        "adverse_selection_bps": ADVERSE_SELECTION_BPS, "position_fraction": POSITION_FRACTION}, "cells": cells}
    with open(args.out, "w") as f:
        json.dump(out, f, indent=1)
    # markdown table
    print(f"moves ({args.symbol}, last 12 months of 1h data): {moves}")
    print("\n| Capital | Trades/day | Fixed fee bps | Variable RT bps | Cost floor bps/trade | Daily cost % of capital | Gross edge per trade for +1%/day | Cost source |")
    print("|---|---|---|---|---|---|---|---|")
    for c in cells:
        print(f"| ${c['capital_usd']:g} | {c['trades_per_day']} | {c['fixed_fee_bps']} | {c['variable_rt_bps']} | {c['cost_floor_bps_per_trade']} | "
              f"{c['daily_cost_pct_of_capital']}% | {c['gross_edge_needed_for_1pct_day_bps']} bps | {c['cost_source']} |")
    if moves:
        print(f"\nTypical 1-minute move ≈ {moves['sd_1m_bps_approx']} bps, 5-minute ≈ {moves['sd_5m_bps_approx']} bps (1 sd, sqrt-scaled from 1h vol). "
              f"A strategy must capture more than the cost floor PER TRADE on average, net of losers, from moves of this size.")


if __name__ == "__main__":
    main()
