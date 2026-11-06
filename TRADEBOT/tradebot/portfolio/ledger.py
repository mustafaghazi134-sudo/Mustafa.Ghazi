"""SQLite ledger: the single source of truth for orders, fills, positions, trades, equity and decisions."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..models import ClosedTrade, EquityPoint, Fill, OrderRequest, Position, Side, new_id

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, ts REAL, mode TEXT, symbol TEXT, side TEXT, notional_usd REAL,
    qty REAL, strategy TEXT, reason TEXT, reduce_only INTEGER, status TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS fills (id TEXT PRIMARY KEY, order_id TEXT, ts REAL, mode TEXT, symbol TEXT, side TEXT, qty REAL,
    price REAL, notional_usd REAL, fee_usd REAL, slippage_bps REAL, tx_signature TEXT);
CREATE TABLE IF NOT EXISTS positions (symbol TEXT PRIMARY KEY, mode TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS trades (id TEXT PRIMARY KEY, mode TEXT, symbol TEXT, strategy TEXT, qty REAL, entry_price REAL,
    exit_price REAL, entry_ts REAL, exit_ts REAL, fees_usd REAL, pnl_usd REAL, exit_reason TEXT);
CREATE TABLE IF NOT EXISTS equity (ts REAL, mode TEXT, equity_usd REAL, cash_usd REAL, exposure_usd REAL,
    realized_pnl_usd REAL, unrealized_pnl_usd REAL);
CREATE TABLE IF NOT EXISTS decisions (ts REAL, mode TEXT, kind TEXT, symbol TEXT, strategy TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS events (ts REAL, mode TEXT, kind TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS api_costs (ts REAL, mode TEXT, model TEXT, purpose TEXT, input_tokens INTEGER,
    output_tokens INTEGER, cost_usd REAL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS intents (id TEXT PRIMARY KEY, mode TEXT, ts REAL, updated_ts REAL, order_id TEXT, symbol TEXT, side TEXT,
    qty REAL, notional_usd REAL, state TEXT, request_id TEXT, tx_signature TEXT, signed_tx TEXT, quote TEXT, result TEXT);
CREATE INDEX IF NOT EXISTS idx_intents_state ON intents(mode, state);
CREATE TABLE IF NOT EXISTS intent_events (ts REAL, intent_id TEXT, order_id TEXT, from_state TEXT, to_state TEXT, note TEXT);
CREATE INDEX IF NOT EXISTS idx_intent_events ON intent_events(intent_id);
CREATE INDEX IF NOT EXISTS idx_fills_ts ON fills(ts);
CREATE INDEX IF NOT EXISTS idx_trades_exit ON trades(exit_ts);
CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity(ts);
"""


class DuplicateOrderError(RuntimeError):
    """A second submission attempt for an order id that already has an intent."""


class IntentTransitionError(ValueError):
    """An order-intent state change that the lifecycle does not allow."""


class Ledger:
    def __init__(self, path: str | Path, mode: str = "paper"):
        self.path = Path(path)
        self.mode = mode
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            try:   # one intent per order id; existing ledgers with historic duplicates keep working without the index
                self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_intents_order ON intents(order_id)")
                self.unique_order_ids = True
            except sqlite3.IntegrityError:
                self.unique_order_ids = False

    def close(self) -> None:
        self.conn.close()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    @contextmanager
    def atomic(self):
        """All writes inside commit together or not at all (a crash or exception mid-way rolls them back).
        Used to book a fill and mark its intent confirmed as one unit, so a restart can never see one without the
        other. Re-entrant: a nested block joins the outer transaction."""
        with self._lock:
            if self.conn.in_transaction:
                yield
                return
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    # ---- kv -----------------------------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        row = self._exec("SELECT value FROM kv WHERE key=?", (f"{self.mode}:{key}",)).fetchone()
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value: Any) -> None:
        self._exec("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", (f"{self.mode}:{key}", json.dumps(value)))

    # ---- orders / fills -----------------------------------------------------------------------
    def record_order(self, o: OrderRequest, status: str, detail: str = "") -> None:
        self._exec("INSERT OR REPLACE INTO orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   (o.id, o.ts, self.mode, o.symbol, o.side.value, o.notional_usd, o.qty, o.strategy, o.reason,
                    int(o.reduce_only), status, detail))

    def record_fill(self, f: Fill) -> None:
        self._exec("INSERT OR REPLACE INTO fills VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   (f.id, f.order_id, f.ts, self.mode, f.symbol, f.side.value, f.qty, f.price, f.notional_usd, f.fee_usd,
                    f.slippage_bps, f.tx_signature))

    def fill_for_order(self, order_id: str) -> sqlite3.Row | None:
        return self._exec("SELECT * FROM fills WHERE mode=? AND order_id=? LIMIT 1", (self.mode, order_id)).fetchone()

    def fills_since(self, ts: float) -> list[sqlite3.Row]:
        return self._exec("SELECT * FROM fills WHERE mode=? AND ts>=? ORDER BY ts", (self.mode, ts)).fetchall()

    def count_buy_fills_since(self, ts: float) -> int:
        return self._exec("SELECT COUNT(*) c FROM fills WHERE mode=? AND ts>=? AND side='buy'", (self.mode, ts)).fetchone()["c"]

    # ---- positions ----------------------------------------------------------------------------
    def upsert_position(self, p: Position) -> None:
        self._exec("INSERT OR REPLACE INTO positions(symbol,mode,data) VALUES(?,?,?)",
                   (f"{self.mode}:{p.symbol}", self.mode, json.dumps(asdict(p))))

    def delete_position(self, symbol: str) -> None:
        self._exec("DELETE FROM positions WHERE symbol=?", (f"{self.mode}:{symbol}",))

    def load_positions(self) -> dict[str, Position]:
        rows = self._exec("SELECT data FROM positions WHERE mode=?", (self.mode,)).fetchall()
        out = {}
        for r in rows:
            p = Position(**json.loads(r["data"]))
            out[p.symbol] = p
        return out

    # ---- trades -------------------------------------------------------------------------------
    def record_trade(self, t: ClosedTrade) -> None:
        self._exec("INSERT OR REPLACE INTO trades VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   (t.id, self.mode, t.symbol, t.strategy, t.qty, t.entry_price, t.exit_price, t.entry_ts, t.exit_ts,
                    t.fees_usd, t.pnl_usd, t.exit_reason))

    def trades(self, since: float = 0.0, mode: str | None = None) -> list[ClosedTrade]:
        rows = self._exec("SELECT * FROM trades WHERE mode=? AND exit_ts>=? ORDER BY exit_ts", (mode or self.mode, since)).fetchall()
        return [ClosedTrade(symbol=r["symbol"], strategy=r["strategy"], qty=r["qty"], entry_price=r["entry_price"],
                            exit_price=r["exit_price"], entry_ts=r["entry_ts"], exit_ts=r["exit_ts"], fees_usd=r["fees_usd"],
                            pnl_usd=r["pnl_usd"], exit_reason=r["exit_reason"], id=r["id"]) for r in rows]

    def count_trades(self, mode: str | None = None) -> int:
        return self._exec("SELECT COUNT(*) c FROM trades WHERE mode=?", (mode or self.mode,)).fetchone()["c"]

    def realized_pnl_since(self, ts: float) -> float:
        r = self._exec("SELECT COALESCE(SUM(pnl_usd),0) s FROM trades WHERE mode=? AND exit_ts>=?", (self.mode, ts)).fetchone()
        return float(r["s"])

    def consecutive_losses(self) -> tuple[int, float | None]:
        rows = self._exec("SELECT pnl_usd, exit_ts FROM trades WHERE mode=? ORDER BY exit_ts DESC LIMIT 50", (self.mode,)).fetchall()
        n = 0
        last_loss_ts = None
        for r in rows:
            if r["pnl_usd"] < 0:
                n += 1
                if last_loss_ts is None:
                    last_loss_ts = r["exit_ts"]
            else:
                break
        return n, last_loss_ts

    # ---- equity -------------------------------------------------------------------------------
    def add_equity(self, e: EquityPoint) -> None:
        self._exec("INSERT INTO equity VALUES(?,?,?,?,?,?,?)",
                   (e.ts, self.mode, e.equity_usd, e.cash_usd, e.exposure_usd, e.realized_pnl_usd, e.unrealized_pnl_usd))

    def count_equity(self, mode: str | None = None) -> int:
        return self._exec("SELECT COUNT(*) c FROM equity WHERE mode=?", (mode or self.mode,)).fetchone()["c"]

    def equity_curve(self, since: float = 0.0) -> list[EquityPoint]:
        rows = self._exec("SELECT * FROM equity WHERE mode=? AND ts>=? ORDER BY ts", (self.mode, since)).fetchall()
        return [EquityPoint(r["ts"], r["equity_usd"], r["cash_usd"], r["exposure_usd"], r["realized_pnl_usd"], r["unrealized_pnl_usd"])
                for r in rows]

    # ---- decisions / events / costs -----------------------------------------------------------
    def log_decision(self, kind: str, payload: dict, symbol: str = "", strategy: str = "", ts: float | None = None) -> None:
        self._exec("INSERT INTO decisions VALUES(?,?,?,?,?,?)",
                   (ts if ts is not None else time.time(), self.mode, kind, symbol, strategy, json.dumps(payload, default=str)))

    def decisions(self, kind: str | None = None, limit: int = 100) -> list[sqlite3.Row]:
        if kind:
            return self._exec("SELECT * FROM decisions WHERE mode=? AND kind=? ORDER BY ts DESC LIMIT ?", (self.mode, kind, limit)).fetchall()
        return self._exec("SELECT * FROM decisions WHERE mode=? ORDER BY ts DESC LIMIT ?", (self.mode, limit)).fetchall()

    def log_event(self, kind: str, message: str, ts: float | None = None) -> None:
        self._exec("INSERT INTO events VALUES(?,?,?,?)", (ts if ts is not None else time.time(), self.mode, kind, message))

    def events(self, limit: int = 50) -> list[sqlite3.Row]:
        return self._exec("SELECT * FROM events WHERE mode=? ORDER BY ts DESC LIMIT ?", (self.mode, limit)).fetchall()

    def first_event_ts(self, mode: str | None = None) -> float | None:
        r = self._exec("SELECT MIN(ts) t FROM events WHERE mode=?", (mode or self.mode,)).fetchone()
        return r["t"]

    def add_api_cost(self, model: str, purpose: str, input_tokens: int, output_tokens: int, cost_usd: float) -> None:
        self._exec("INSERT INTO api_costs VALUES(?,?,?,?,?,?,?)",
                   (time.time(), self.mode, model, purpose, input_tokens, output_tokens, cost_usd))

    def total_api_cost(self, since: float = 0.0) -> float:
        r = self._exec("SELECT COALESCE(SUM(cost_usd),0) s FROM api_costs WHERE mode=? AND ts>=?", (self.mode, since)).fetchone()
        return float(r["s"])

    # ---- order intents (crash protection) ---------------------------------------------------------
    # Lifecycle: created -> signed (requestId + signature + signed tx persisted BEFORE submission) -> submitted
    # (sent, outcome unknown) -> confirmed | failed | expired | abandoned. Anything not terminal is "pending".
    PENDING_STATES = ("created", "signed", "submitted")
    TERMINAL_STATES = ("confirmed", "failed", "expired", "abandoned")
    TRANSITIONS = {
        "created": frozenset({"signed", "submitted", "failed", "abandoned"}),
        "signed": frozenset({"signed", "submitted", "confirmed", "failed", "expired", "abandoned"}),
        "submitted": frozenset({"submitted", "confirmed", "failed", "expired", "abandoned"}),
        "confirmed": frozenset(), "failed": frozenset(), "expired": frozenset(), "abandoned": frozenset(),
    }

    def create_intent(self, order: OrderRequest, qty: float, notional_usd: float, quote: dict | None = None,
                      ts: float | None = None) -> str:
        """ts should be the engine clock (replay-safe); defaults to wall time."""
        iid = new_id("int")
        now = ts if ts is not None else time.time()
        with self._lock:
            if self._exec("SELECT 1 FROM intents WHERE order_id=? LIMIT 1", (order.id,)).fetchone():
                raise DuplicateOrderError(f"order id {order.id} already has an intent; refusing a duplicate submission")
            try:
                self._exec("INSERT INTO intents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (iid, self.mode, now, now, order.id, order.symbol, order.side.value, qty, notional_usd, "created",
                            None, None, None, json.dumps(quote or {}, default=str), None))
            except sqlite3.IntegrityError as e:
                raise DuplicateOrderError(f"order id {order.id} already has an intent") from e
            self._exec("INSERT INTO intent_events VALUES(?,?,?,?,?,?)", (now, iid, order.id, None, "created", order.symbol + " " + order.side.value))
        return iid

    def update_intent(self, iid: str, state: str | None = None, **fields: Any) -> None:
        """Update fields and, optionally, move the intent along its lifecycle. Illegal moves (out of a terminal
        state, or skipping backwards) raise IntentTransitionError so a bug can never resurrect or rewrite an order."""
        cols = {k: v for k, v in fields.items() if k in ("request_id", "tx_signature", "signed_tx", "result", "quote")}
        prev = None
        if state:
            row = self.get_intent(iid)
            if row is None:
                raise KeyError(iid)
            prev = row["state"]
            if state not in self.TRANSITIONS.get(prev, frozenset()):
                raise IntentTransitionError(f"intent {iid}: illegal transition {prev} -> {state}")
            cols["state"] = state
        if "result" in cols and not isinstance(cols["result"], str):
            cols["result"] = json.dumps(cols["result"], default=str)
        cols["updated_ts"] = time.time()
        sets = ", ".join(f"{k}=?" for k in cols)
        self._exec(f"UPDATE intents SET {sets} WHERE id=?", (*cols.values(), iid))
        if state and state != prev:
            note = cols.get("result") if isinstance(cols.get("result"), str) else ""
            self._exec("INSERT INTO intent_events VALUES(?,?,?,?,?,?)",
                       (cols["updated_ts"], iid, None, prev, state, (note or "")[:200]))

    def intent_history(self, iid: str) -> list[sqlite3.Row]:
        return self._exec("SELECT * FROM intent_events WHERE intent_id=? ORDER BY rowid", (iid,)).fetchall()

    def get_intent(self, iid: str) -> sqlite3.Row | None:
        return self._exec("SELECT * FROM intents WHERE id=?", (iid,)).fetchone()

    def pending_intents(self, symbol: str | None = None) -> list[sqlite3.Row]:
        q = f"SELECT * FROM intents WHERE mode=? AND state IN ({','.join('?' * len(self.PENDING_STATES))})"
        args: list = [self.mode, *self.PENDING_STATES]
        if symbol:
            q += " AND symbol=?"; args.append(symbol)
        return self._exec(q + " ORDER BY ts", tuple(args)).fetchall()

    def intents(self, limit: int = 50) -> list[sqlite3.Row]:
        return self._exec("SELECT * FROM intents WHERE mode=? ORDER BY ts DESC LIMIT ?", (self.mode, limit)).fetchall()

    def resolve_intent_manually(self, iid: str, state: str, note: str) -> sqlite3.Row:
        if state not in ("expired", "failed", "confirmed"):
            raise ValueError("state must be expired, failed or confirmed")
        row = self.get_intent(iid)
        if row is None:
            raise KeyError(iid)
        if row["state"] not in self.PENDING_STATES:
            raise ValueError(f"intent {iid} is already {row['state']}")
        self.update_intent(iid, state=state, result=f"MANUAL: {note}")
        self.log_event("intent_manual_resolution", f"{iid} {row['symbol']} {row['side']} -> {state}: {note}")
        return self.get_intent(iid)
