"""Turn results.json into RESULTS.md: a comparison table with verdicts and verification status."""
from __future__ import annotations

import argparse
import json
import math


def _f(x, nd=1, suffix=""):
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "inf" if isinstance(x, float) and math.isinf(x) else "n/a"
    return f"{x:,.{nd}f}{suffix}"


def verdict(name: str, entry: dict, bh: dict | None, flags) -> str:
    oos = entry.get("walk_forward_oos") or {}
    la = entry.get("lookahead", {})
    cs = entry.get("cost_sensitivity", {})
    de = entry.get("delayed_entry", {})
    reasons = []
    if not la.get("passed", True):
        reasons.append("LOOKAHEAD")
    if "no_stop_loss" in flags:
        reasons.append("no stop loss")
    if not oos:
        return "benchmark"
    if oos.get("trades", 0) < 30:
        reasons.append(f"only {oos.get('trades')} OOS trades")
    if oos.get("net_return_pct", 0) <= 0:
        reasons.append("loses money OOS after costs")
    elif oos.get("profit_factor", 0) < 1.2:
        reasons.append("thin edge (PF<1.2)")
    if cs and cs.get("cost_x2", {}).get("net_return_pct", 0) <= 0 < cs.get("cost_x1", {}).get("net_return_pct", 0):
        reasons.append("dies at 2x costs")
    if de.get("retained_fraction") is not None and de["retained_fraction"] < 0.3 and (entry.get("in_sample_default_params", {}).get("net_return_pct", 0) > 0):
        reasons.append("collapses if filled 1 bar late")
    if bh and oos.get("net_return_pct", 0) > 0:
        # compare risk-adjusted: calmar vs buy & hold calmar over the same OOS span is not available; use sharpe + dd
        if oos.get("sharpe", 0) < bh.get("sharpe", 0) and abs(oos.get("max_drawdown_pct", 0)) >= abs(bh.get("max_drawdown_pct", 0)):
            reasons.append("worse than buy&hold on both Sharpe and drawdown")
    if not reasons:
        return "PASS"
    return "REJECT: " + "; ".join(reasons)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    R = json.load(open(args.results))
    L = []
    L.append("# Strategy research results (independently reproduced)\n")
    L.append(f"Costs modelled per side: {R['costs']['fee_bps_per_side']} bps fee + {R['costs']['slippage_bps_per_side']} bps slippage "
             f"(~{(R['costs']['fee_bps_per_side'] + R['costs']['slippage_bps_per_side']) * 2 / 100:.1f}% round trip). Long-only spot, one position per asset, "
             "entries at next bar open, stop checked on bar low before take-profit (pessimistic). Walk-forward: 12-month train, 3-month test, rolling; "
             "parameters chosen on train only. All numbers below were produced by `research_lab` in this repository on the data described; "
             "any performance claim quoted from a strategy's source is marked UNVERIFIED.\n")
    L.append("## Data\n")
    L.append("| Symbol | Bars | Span | Monthly files / sources | Gaps | Cross-source max close dev | Sanity drops |")
    L.append("|---|---|---|---|---|---|---|")
    for s, d in R["data"].items():
        L.append(f"| {s} | {d['bars']} | {d['start'][:10]} .. {d['end'][:10]} | {d['months']} / {d['sources']} | {len(d['gaps'])} | {d['max_overlap_close_dev_pct']}% over {d['overlap_months_checked']} months | {d['sanity_errors']} |")
    L.append("")
    symbols = sorted({sym for st in R["strategies"].values() for sym in st["per_symbol"]})
    bh = {sym: R["strategies"].get("buy_hold", {}).get("per_symbol", {}).get(sym, {}).get("in_sample_default_params") for sym in symbols}
    for sym in symbols:
        L.append(f"## {sym}\n")
        L.append("| Strategy | Source | Trades (OOS) | Win rate (OOS) | Net return OOS | Profit factor OOS | Max DD OOS | Avg win / avg loss (OOS) | Sharpe OOS | IS return (default params) | Lookahead | 2x costs | Verdict |")
        L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        rows = []
        for name, st in R["strategies"].items():
            e = st["per_symbol"].get(sym)
            if not e:
                continue
            oos = e.get("walk_forward_oos") or {}
            is_ = e.get("in_sample_default_params", {})
            cs = e.get("cost_sensitivity", {}).get("cost_x2", {})
            v = verdict(name, e, bh.get(sym), st.get("flags", []))
            if name == "buy_hold":
                rows.append((-1e9, f"| **{name}** | {st['source']} | 1 | n/a | {_f(is_.get('net_return_pct'), 1, '%')} (full span) | n/a | {_f(is_.get('max_drawdown_pct'), 1, '%')} | n/a | {_f(is_.get('sharpe'), 2)} | {_f(is_.get('net_return_pct'), 1, '%')} | ok | n/a | benchmark |"))
                continue
            rows.append((-(oos.get("sharpe") or -99), f"| {name} | {st['source'][:40]} | {oos.get('trades', 'n/a')} | {_f(oos.get('win_rate_pct'), 1, '%')} | {_f(oos.get('net_return_pct'), 1, '%')} | {_f(oos.get('profit_factor'), 2)} | {_f(oos.get('max_drawdown_pct'), 1, '%')} | {_f(oos.get('avg_win_pct'), 2, '%')} / {_f(oos.get('avg_loss_pct'), 2, '%')} | {_f(oos.get('sharpe'), 2)} | {_f(is_.get('net_return_pct'), 1, '%')} ({is_.get('trades')} tr) | {'ok' if e.get('lookahead', {}).get('passed') else 'FAIL'} | {_f(cs.get('net_return_pct'), 1, '%')} | {v} |"))
        for _, r in sorted(rows, key=lambda t: t[0]):
            L.append(r)
        L.append("")
        dca = R.get("dca_martingale_demo", {}).get(sym)
        if dca:
            L.append(f"**Martingale/DCA demonstration ({sym})**: win rate {dca['win_rate_pct']}% over {dca['closed_trades']} closed trades, "
                     f"net return {dca['net_return_pct']}%, max drawdown {dca['max_drawdown_pct']}%, open position unrealised {dca['open_position_unrealised_pct']}%, "
                     f"liquidated={dca['liquidated']}. {dca['verdict']}\n")
    L.append("## Notes on each candidate\n")
    for name, st in R["strategies"].items():
        L.append(f"- **{name}** ({st['source']}): {st['note']}" + (f" Flags: {', '.join(st['flags'])}." if st.get("flags") else ""))
    text = "\n".join(L)
    if args.out:
        open(args.out, "w").write(text)
    print(text)


if __name__ == "__main__":
    main()
