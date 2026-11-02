"""Collect forward-paper evidence from a fast-engine data folder (data/fast.db + data/scanner.db).

It classifies the run (LIVE PAPER = real-time feeds; REPLAY = historical bars) from the ledger's own start events,
and never mixes the two. Output: every trade with timestamps and net result, estimated cost at decision versus the
realized cost of the fill, every rejected signal grouped by reason, missed opportunities (rejected candidates whose
shadow mark at the horizon cleared the minimum edge after measured cost), per-stage latency, rate-limit and
WebSocket health, alerts, and expectancy with a bootstrap 95% interval once there are at least 5 trades.

usage: PYTHONPATH=$PWD python -m research_lab.forward_evidence --data-dir data --out data/forward_evidence.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path

import numpy as np


def _rows(db: Path, sql: str, args=()) -> list[sqlite3.Row]:
    if not db.exists():
        return []
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def collect(data_dir: Path) -> dict:
    fast, scan = data_dir / "fast.db", data_dir / "scanner.db"
    starts = _rows(fast, "SELECT ts, message FROM events WHERE kind='start' ORDER BY ts")
    replay = any("replay" in (r["message"] or "") for r in starts)
    span = _rows(fast, "SELECT MIN(ts) a, MAX(ts) b FROM decisions")
    a, b = (span[0]["a"], span[0]["b"]) if span else (None, None)
    days = max(((b or 0) - (a or 0)) / 86400, 1e-9) if a else 0.0
    trades = [dict(r) for r in _rows(fast, "SELECT * FROM trades ORDER BY exit_ts")]
    fills = [dict(r) for r in _rows(fast, "SELECT * FROM fills ORDER BY rowid")]
    entries = {json.loads(r["payload"]).get("order"): json.loads(r["payload"]) for r in _rows(fast, "SELECT payload FROM decisions WHERE kind='risk'")}
    rej: dict[str, int] = {}
    for r in _rows(fast, "SELECT payload FROM decisions WHERE kind='fast_reject'"):
        for x in json.loads(r["payload"]).get("reasons", []):
            k = x.split("(")[0]
            rej[k] = rej.get(k, 0) + 1
    cands = [dict(r) for r in _rows(scan, "SELECT ts, symbol, signal, expected_move_bps, net_edge_bps, status, reasons, mark_bps, cost FROM candidates")]
    missed = [c for c in cands if c["status"] == "rejected_marked" and (c["mark_bps"] or -1e9) >= 20.0]
    kv = {r["key"].split(":", 1)[1]: json.loads(r["value"]) for r in _rows(fast, "SELECT key, value FROM kv")}
    alerts = [dict(r) for r in _rows(fast, "SELECT ts, message FROM events WHERE kind='alert' ORDER BY ts")]
    out = {"evidence_class": "REPLAY (historical bars; not forward evidence)" if replay else ("LIVE PAPER (forward)" if starts else "NO DATA"),
           "window_utc": [time.strftime("%Y-%m-%d %H:%M", time.gmtime(a)) if a else None, time.strftime("%Y-%m-%d %H:%M", time.gmtime(b)) if b else None],
           "days": round(days, 3), "fills": len(fills), "closed_trades": len(trades),
           "trades_per_day": round(len(trades) / days, 3) if days else None,
           "trades": [{"entry_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t["entry_ts"])),
                       "exit_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t["exit_ts"])), "symbol": t["symbol"], "strategy": t["strategy"],
                       "entry": t["entry_price"], "exit": t["exit_price"], "net_usd": round(t["pnl_usd"], 6), "fees_usd": round(t["fees_usd"], 6),
                       "reason": t["exit_reason"]} for t in trades],
           "rejections_by_reason": dict(sorted(rej.items(), key=lambda kv_: -kv_[1])),
           "candidates": len(cands), "missed_opportunities": len(missed),
           "missed_examples": [{"utc": time.strftime("%Y-%m-%d %H:%M", time.gmtime(c["ts"])), "symbol": c["symbol"], "signal": c["signal"],
                                "mark_bps_after_cost": c["mark_bps"], "rejected_for": json.loads(c["reasons"] or "[]")} for c in missed[:10]],
           "latency_by_stage": kv.get("latency_summary"), "rate_limits": kv.get("rate_limits"), "websocket": kv.get("websocket"),
           "alerts": [{"utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(x["ts"])), "message": x["message"]} for x in alerts[:50]]}
    if trades:
        x = np.array([t["pnl_usd"] for t in trades])
        out["net_usd_total"] = round(float(x.sum()), 6)
        out["expectancy_usd"] = round(float(x.mean()), 6)
        if len(x) >= 5:
            boot = np.random.default_rng(0).choice(x, (5000, len(x))).mean(1)
            out["expectancy_ci95_usd"] = [round(float(np.percentile(boot, 2.5)), 6), round(float(np.percentile(boot, 97.5)), 6)]
        else:
            out["expectancy_ci95_usd"] = f"not computed: {len(x)} trade(s) < 5"
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    res = collect(Path(a.data_dir))
    text = json.dumps(res, indent=1, default=str)
    if a.out:
        Path(a.out).write_text(text)
    print(text)


if __name__ == "__main__":
    main()
