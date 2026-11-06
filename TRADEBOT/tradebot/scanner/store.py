"""SQLite store for the scanner. Separate file from the trading ledger so the two processes never contend."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (ts REAL, source TEXT, ok INTEGER, status INTEGER, latency_ms REAL, bytes INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS ticks (ts REAL, symbol TEXT, price REAL, block_id INTEGER, slot INTEGER, reference REAL, source_latency_ms REAL);
CREATE TABLE IF NOT EXISTS candidates (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, symbol TEXT, signal TEXT, expected_move_bps REAL,
    horizon_sec INTEGER, size_usd REAL, detail TEXT, cost TEXT, net_edge_bps REAL, status TEXT, reasons TEXT,
    entry_price_usd REAL, token_qty REAL, exit_ts REAL, exit_quote TEXT, realized_bps REAL, mark_bps REAL);
CREATE TABLE IF NOT EXISTS cost_probes (ts REAL, symbol TEXT, size_usd REAL, expected_bps REAL, worst_case_bps REAL, buy_impact_bps REAL,
    sell_impact_bps REAL, jupiter_fee_bps REAL, router TEXT, route TEXT, latency_ms REAL);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE INDEX IF NOT EXISTS ix_cand_status ON candidates(status);
CREATE INDEX IF NOT EXISTS ix_ticks_sym ON ticks(symbol, ts);
"""

# candidate lifecycle: rejected (never opened, shadow-tracked) | open (hypothetical position) | closed (realised)
#                      missed = rejected AND mark_bps >= min edge at horizon;  false_positive = closed AND realized_bps <= 0


class ScannerStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    # ---- writes --------------------------------------------------------------------------------
    def log_source(self, source: str, ok: bool, status: int, latency_ms: float, nbytes: int = 0, note: str = "", ts: float | None = None) -> None:
        self.conn.execute("INSERT INTO sources VALUES (?,?,?,?,?,?,?)", (ts or time.time(), source, int(ok), status, latency_ms, nbytes, note[:200]))

    def log_tick(self, ts: float, symbol: str, price: float, block_id: int | None, slot: int | None, reference: float | None, latency_ms: float) -> None:
        self.conn.execute("INSERT INTO ticks VALUES (?,?,?,?,?,?,?)", (ts, symbol, price, block_id, slot, reference, latency_ms))

    def add_candidate(self, ts: float, symbol: str, signal: str, expected_move_bps: float, horizon_sec: int, size_usd: float, detail: dict,
                      cost: dict | None, net_edge_bps: float | None, status: str, reasons: list[str],
                      entry_price_usd: float | None = None, token_qty: float | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO candidates (ts,symbol,signal,expected_move_bps,horizon_sec,size_usd,detail,cost,net_edge_bps,status,reasons,entry_price_usd,token_qty) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, symbol, signal, expected_move_bps, horizon_sec, size_usd, json.dumps(detail, default=str), json.dumps(cost, default=str) if cost else None,
             net_edge_bps, status, json.dumps(reasons), entry_price_usd, token_qty))
        return int(cur.lastrowid)

    def close_candidate(self, cid: int, exit_ts: float, exit_quote: dict | None, realized_bps: float | None, mark_bps: float | None, status: str) -> None:
        self.conn.execute("UPDATE candidates SET exit_ts=?, exit_quote=?, realized_bps=?, mark_bps=?, status=? WHERE id=?",
                          (exit_ts, json.dumps(exit_quote, default=str) if exit_quote else None, realized_bps, mark_bps, status, cid))

    def log_cost_probe(self, ts: float, symbol: str, c: dict, latency_ms: float) -> None:
        self.conn.execute("INSERT INTO cost_probes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (ts, symbol, c["size_usd"], c["expected_bps"], c["worst_case_bps"], c["buy_impact_bps"], c["sell_impact_bps"],
                           c.get("jupiter_fee_bps"), c.get("router", ""), c.get("route", ""), latency_ms))

    def set(self, k: str, v) -> None:
        self.conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, json.dumps(v, default=str)))

    def get(self, k: str, default=None):
        r = self.conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r["v"]) if r else default

    # ---- reads ---------------------------------------------------------------------------------
    def pending(self, status: str = "open") -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM candidates WHERE status=? ORDER BY ts", (status,)).fetchall()

    def source_stats(self, since: float = 0.0) -> list[dict]:
        rows = self.conn.execute(
            "SELECT source, COUNT(*) n, SUM(ok) ok, AVG(latency_ms) avg_ms, MAX(latency_ms) max_ms, "
            "SUM(CASE WHEN status=429 THEN 1 ELSE 0 END) rate_limited, MIN(ts) first_ts, MAX(ts) last_ts "
            "FROM sources WHERE ts>=? GROUP BY source", (since,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            span = (d["last_ts"] or 0) - (d["first_ts"] or 0)
            d["requests_per_min"] = round(d["n"] / span * 60, 2) if span > 0 else None
            d["error_rate_pct"] = round((1 - (d["ok"] or 0) / d["n"]) * 100, 2) if d["n"] else None
            # p95 latency
            lat = [x[0] for x in self.conn.execute("SELECT latency_ms FROM sources WHERE source=? AND ts>=? ORDER BY latency_ms", (d["source"], since)).fetchall()]
            d["p95_ms"] = round(lat[int(len(lat) * 0.95) - 1], 1) if len(lat) >= 2 else (round(lat[0], 1) if lat else None)
            d["avg_ms"] = round(d["avg_ms"], 1) if d["avg_ms"] is not None else None
            out.append(d)
        return out

    def refresh_stats(self, since: float = 0.0) -> dict:
        """Achieved price refresh: median gap between consecutive ticks per symbol, and how often the price changed."""
        out = {}
        for (sym,) in self.conn.execute("SELECT DISTINCT symbol FROM ticks WHERE ts>=?", (since,)).fetchall():
            ts_px = self.conn.execute("SELECT ts, price, block_id FROM ticks WHERE symbol=? AND ts>=? ORDER BY ts", (sym, since)).fetchall()
            if len(ts_px) < 3:
                continue
            gaps = sorted(ts_px[i][0] - ts_px[i - 1][0] for i in range(1, len(ts_px)))
            changed = sum(1 for i in range(1, len(ts_px)) if ts_px[i][1] != ts_px[i - 1][1])
            blocks = [r[2] for r in ts_px if r[2] is not None]
            new_blocks = sum(1 for i in range(1, len(blocks)) if blocks[i] != blocks[i - 1])
            out[sym] = {"ticks": len(ts_px), "median_gap_sec": round(gaps[len(gaps) // 2], 2), "price_changed_pct": round(changed / (len(ts_px) - 1) * 100, 1),
                        "block_advanced_pct": round(new_blocks / max(len(blocks) - 1, 1) * 100, 1) if len(blocks) > 1 else None}
        return out

    def candidate_stats(self, since: float = 0.0, min_edge_bps: float = 0.0) -> dict:
        rows = self.conn.execute("SELECT * FROM candidates WHERE ts>=?", (since,)).fetchall()
        by_sig: dict[str, dict] = {}
        reasons: dict[str, int] = {}
        for r in rows:
            d = by_sig.setdefault(r["signal"], {"candidates": 0, "opened": 0, "closed": 0, "wins": 0, "false_positives": 0, "rejected": 0, "missed": 0,
                                                "realized_bps_sum": 0.0, "open": 0})
            d["candidates"] += 1
            if r["status"] in ("open", "closed", "closed_win", "closed_loss"):
                d["opened"] += 1
            if r["status"] == "open":
                d["open"] += 1
            if r["status"].startswith("closed"):
                d["closed"] += 1
                if r["realized_bps"] is not None:
                    d["realized_bps_sum"] += r["realized_bps"]
                    if r["realized_bps"] > 0:
                        d["wins"] += 1
                    else:
                        d["false_positives"] += 1
            if r["status"].startswith("rejected"):
                d["rejected"] += 1
                for reason in json.loads(r["reasons"] or "[]"):
                    reasons[reason] = reasons.get(reason, 0) + 1
                if r["mark_bps"] is not None and r["mark_bps"] >= max(min_edge_bps, 0.0) and r["mark_bps"] > 0:
                    d["missed"] += 1
        for d in by_sig.values():
            d["avg_realized_bps"] = round(d["realized_bps_sum"] / d["closed"], 1) if d["closed"] else None
            d["win_rate_pct"] = round(d["wins"] / d["closed"] * 100, 1) if d["closed"] else None
            del d["realized_bps_sum"]
        return {"by_signal": by_sig, "rejection_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1]))}

    def cost_probe_stats(self, since: float = 0.0) -> list[dict]:
        rows = self.conn.execute(
            "SELECT symbol, size_usd, COUNT(*) n, AVG(expected_bps) exp_bps, AVG(worst_case_bps) worst_bps, MAX(worst_case_bps) worst_max, "
            "AVG(jupiter_fee_bps) fee_bps, AVG(latency_ms) latency_ms FROM cost_probes WHERE ts>=? GROUP BY symbol, size_usd ORDER BY symbol, size_usd", (since,)).fetchall()
        return [{k: (round(v, 1) if isinstance(v, float) else v) for k, v in dict(r).items()} for r in rows]
