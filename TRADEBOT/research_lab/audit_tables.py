"""Render the strategy-audit results as Markdown tables (the tables in docs/STRATEGY_AUDIT_GO_NOGO.md).

usage (from TRADEBOT/): PYTHONPATH=$PWD python -m research_lab.audit_tables [--in research_lab/results/strategy_audit.json]
"""
from __future__ import annotations

import argparse
import json

ORDER = ["1m", "5m", "15m", "1h", "4h", "1d"]


def _ci(x):
    return f"[{x[0]:+.1f}, {x[1]:+.1f}]" if isinstance(x, list) else "-"


def main_table(rows) -> str:
    out = ["| Horizon | Family | OOS trades | Trades/day | Gross bps | Net bps (base) | 95% CI (base) | PF | Max DD % | Accepted | Failed checks |",
           "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        b, g = r["scenarios"]["jup_base|d1"], r["scenarios"]["zero|d0"]
        failed = ", ".join(k for k, v in r["acceptance_checks"].items() if not v) or "none"
        out.append(f"| {r['horizon']} | {r['family']} | {b.get('n', 0)} | {b.get('trades_per_day', '-')} | {g.get('expectancy_bps', '-')} | "
                   f"{b.get('expectancy_bps', '-')} | {_ci(b.get('expectancy_ci95_bps'))} | {b.get('profit_factor', '-')} | "
                   f"{b.get('max_drawdown_pct', '-')} | {'YES' if r['accepted'] else 'no'} | {failed} |")
    return "\n".join(out)


def cost_table(rows) -> str:
    keys = ["zero|d0", "cex_taker|d1", "jup_optimistic|d1", "jup_base|d0", "jup_base|d1", "jup_stress|d1"]
    out = ["| Horizon | Family | " + " | ".join(keys) + " |", "|---|---|" + "---|" * len(keys)]
    for r in rows:
        cells = []
        for k in keys:
            s = r["scenarios"].get(k, {})
            cells.append(f"{s.get('expectancy_bps', '-')}" if s.get("n") else "-")
        out.append(f"| {r['horizon']} | {r['family']} | " + " | ".join(cells) + " |")
    return "\n".join(out)


def mc_table(rows) -> str:
    out = ["| Horizon | Family | Per-asset net bps (base) | MC total return % p5/p50/p95 | MC max DD % p50/p95 | P(loss) % |", "|---|---|---|---|---|---|"]
    for r in rows:
        b = r["scenarios"]["jup_base|d1"]
        if not b.get("n"):
            continue
        pa = ", ".join(f"{k[:3]} {v}" for k, v in b.get("per_asset_expectancy_bps", {}).items())
        out.append(f"| {r['horizon']} | {r['family']} | {pa} | {b.get('mc_total_return_p5_p50_p95')} | {b.get('mc_max_drawdown_p50_p95')} | {b.get('mc_prob_loss_pct')} |")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="research_lab/results/strategy_audit.json")
    a = ap.parse_args()
    res = json.load(open(a.inp))
    rows = sorted(res["rows"], key=lambda r: (ORDER.index(r["horizon"]), r["family"]))
    print("Acceptance (jup_base = 26 bps/side incl. network fee, one bar extra delay):", res["acceptance"], "\n")
    print(main_table(rows), "\n")
    print("Net expectancy (bps per trade) by cost scenario and delay:\n")
    print(cost_table(rows), "\n")
    print("Scenario simulation (trade-sequence Monte Carlo of the OOS trades; not observed results):\n")
    print(mc_table(rows))


if __name__ == "__main__":
    main()
