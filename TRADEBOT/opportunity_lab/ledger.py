"""The lab's own SQLite ledger (data/lab/lab.sqlite). Nothing here touches the main bot's ledger.

Every observation, opportunity, assessment, proposal and rejection is kept, not only trades, so that results can be
audited and re-tested later. Paper orders go through intents with a unique key (one entry per proposal, one exit
per position attempt), so a crash or a retry can never create a duplicate simulated order.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from .models import Opportunity, PaperPosition, RiskAssessment, TokenSnapshot, TradeProposal, new_id

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS snapshots (observed_ts REAL, mint TEXT, source TEXT, digest TEXT, payload TEXT,
    UNIQUE(mint, source, observed_ts));
CREATE INDEX IF NOT EXISTS idx_snap_ts ON snapshots(observed_ts);
CREATE TABLE IF NOT EXISTS opportunities (id TEXT PRIMARY KEY, ts REAL, mint TEXT, eligible INTEGER, reason TEXT,
    signals TEXT, snapshot TEXT, provenance TEXT);
CREATE TABLE IF NOT EXISTS assessments (ts REAL, mint TEXT, opportunity_id TEXT, approved INTEGER, score REAL,
    flags TEXT, data_gaps TEXT, route TEXT, facts TEXT);
CREATE TABLE IF NOT EXISTS proposals (id TEXT PRIMARY KEY, ts REAL, opportunity_id TEXT, mint TEXT, strategy TEXT,
    accept INTEGER, payload TEXT);
CREATE TABLE IF NOT EXISTS intents (id TEXT PRIMARY KEY, key TEXT UNIQUE, ts REAL, updated_ts REAL, mint TEXT,
    side TEXT, state TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS positions (id TEXT PRIMARY KEY, proposal_id TEXT UNIQUE, mint TEXT, state TEXT, payload TEXT,
    opened_ts REAL, closed_ts REAL);
CREATE TABLE IF NOT EXISTS trades (position_id TEXT PRIMARY KEY, opportunity_id TEXT, proposal_id TEXT, mint TEXT,
    symbol TEXT, strategy TEXT, entry_ts REAL, exit_ts REAL, entry_price REAL, exit_price REAL, qty REAL,
    cost_usd REAL, proceeds_usd REAL, pnl_usd REAL, pnl_pct REAL, fees_usd REAL, exit_reason TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS events (ts REAL, kind TEXT, message TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS equity (ts REAL, cash REAL, exposure REAL, equity REAL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""

INTENT_STATES = {"created": {"filled", "failed", "abandoned"}, "filled": set(), "failed": set(), "abandoned": set()}


class DuplicateIntent(RuntimeError):
    pass


class LabLedger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)
        self._lock = threading.RLock()
        self._depth = 0

    def close(self) -> None:
        self.con.close()

    @contextmanager
    def atomic(self):
        with self._lock:
            outer = self._depth == 0
            if outer:
                self.con.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if outer:
                    self.con.execute("ROLLBACK")
                raise
            self._depth -= 1
            if outer:
                self.con.execute("COMMIT")

    def x(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.con.execute(sql, args)

    # ---------------------------------------------------------------- kv / events
    def get(self, key: str, default=None):
        r = self.x("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default

    def set(self, key: str, value) -> None:
        self.x("INSERT INTO kv VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value, default=str)))

    def event(self, kind: str, message: str, payload: dict | None = None, ts: float | None = None) -> None:
        self.x("INSERT INTO events VALUES(?,?,?,?)", (ts if ts is not None else time.time(), kind, message[:500],
                                                    json.dumps(payload or {}, default=str)))

    # ---------------------------------------------------------------- research records
    def add_snapshot(self, s: TokenSnapshot) -> None:
        self.x("INSERT OR IGNORE INTO snapshots VALUES(?,?,?,?,?)", (s.observed_ts, s.mint, s.source, s.digest, s.to_json()))

    def snapshots(self, start: float = 0.0, end: float = 1e18) -> list[TokenSnapshot]:
        rows = self.x("SELECT payload FROM snapshots WHERE observed_ts>=? AND observed_ts<? ORDER BY observed_ts", (start, end)).fetchall()
        return [TokenSnapshot.from_dict(json.loads(r["payload"])) for r in rows]

    def add_opportunity(self, o: Opportunity) -> None:
        self.x("INSERT OR IGNORE INTO opportunities VALUES(?,?,?,?,?,?,?,?)",
               (o.id, o.detected_ts, o.mint, int(o.eligible), o.ineligible_reason, json.dumps(o.signals, default=str),
                o.snapshot.to_json(), json.dumps(o.provenance)))

    def add_assessment(self, a: RiskAssessment, opportunity_id: str) -> None:
        self.x("INSERT INTO assessments VALUES(?,?,?,?,?,?,?,?,?)",
               (a.ts, a.mint, opportunity_id, int(a.approved), a.score, json.dumps(a.flags), json.dumps(a.data_gaps),
                json.dumps(asdict(a.route) if a.route else None), json.dumps(asdict(a.facts) if a.facts else None, default=str)))

    def add_proposal(self, p: TradeProposal) -> None:
        self.x("INSERT OR REPLACE INTO proposals VALUES(?,?,?,?,?,?,?)",
               (p.id, p.ts, p.opportunity_id, p.mint, p.strategy, int(p.accept), json.dumps(asdict(p), default=str)))

    # ---------------------------------------------------------------- intents (paper orders)
    def create_intent(self, key: str, mint: str, side: str, detail: dict, ts: float) -> str:
        iid = new_id("int")
        try:
            self.x("INSERT INTO intents VALUES(?,?,?,?,?,?,?,?)", (iid, key, ts, ts, mint, side, "created", json.dumps(detail, default=str)))
        except sqlite3.IntegrityError as e:
            raise DuplicateIntent(f"intent key {key} already exists") from e
        return iid

    def resolve_intent(self, iid: str, state: str, detail: dict | None = None, ts: float | None = None) -> None:
        r = self.x("SELECT state, detail FROM intents WHERE id=?", (iid,)).fetchone()
        if r is None:
            raise KeyError(iid)
        if state not in INTENT_STATES[r["state"]]:
            raise ValueError(f"intent {iid}: illegal {r['state']} -> {state}")
        d = json.loads(r["detail"] or "{}")
        d.update(detail or {})
        self.x("UPDATE intents SET state=?, detail=?, updated_ts=? WHERE id=?", (state, json.dumps(d, default=str),
                                                                          ts if ts is not None else time.time(), iid))

    def pending_intents(self) -> list[sqlite3.Row]:
        return self.x("SELECT * FROM intents WHERE state='created' ORDER BY ts").fetchall()

    def intent_exists(self, key: str) -> bool:
        return self.x("SELECT 1 FROM intents WHERE key=?", (key,)).fetchone() is not None

    # ---------------------------------------------------------------- positions and trades
    def save_position(self, p: PaperPosition) -> None:
        self.x("INSERT INTO positions VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state, "
               "payload=excluded.payload, closed_ts=excluded.closed_ts",
               (p.id, p.proposal_id, p.mint, p.state, json.dumps(asdict(p)), p.entry_ts, None if p.state == "open" else time.time()))

    def open_positions(self) -> list[PaperPosition]:
        rows = self.x("SELECT payload FROM positions WHERE state='open'").fetchall()
        return [PaperPosition(**json.loads(r["payload"])) for r in rows]

    def add_trade(self, row: dict) -> None:
        cols = ["position_id", "opportunity_id", "proposal_id", "mint", "symbol", "strategy", "entry_ts", "exit_ts", "entry_price",
                "exit_price", "qty", "cost_usd", "proceeds_usd", "pnl_usd", "pnl_pct", "fees_usd", "exit_reason", "payload"]
        self.x(f"INSERT INTO trades VALUES({','.join('?' * len(cols))})", tuple(row.get(c) for c in cols))

    def trades(self, since: float = 0.0) -> list[sqlite3.Row]:
        return self.x("SELECT * FROM trades WHERE exit_ts>=? ORDER BY exit_ts", (since,)).fetchall()

    def add_equity(self, ts: float, cash: float, exposure: float) -> None:
        self.x("INSERT INTO equity VALUES(?,?,?,?)", (ts, cash, exposure, cash + exposure))

    def count(self, table: str, where: str = "1=1", args: tuple = ()) -> int:
        return int(self.x(f"SELECT COUNT(*) c FROM {table} WHERE {where}", args).fetchone()["c"])
