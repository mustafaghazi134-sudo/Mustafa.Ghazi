"""SQLite ledger: the single source of truth for orders, fills, positions, trades, equity and decisions."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..models import ClosedTrade, EquityPoint, Fill, OrderRequest, Position, Side

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
CREATE INDEX IF NOT EXISTS idx_fills_ts ON fills(ts);
CREATE INDEX IF NOT EXISTS idx_trades_exit ON trades(exit_ts);
CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity(ts);
"""


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

    def close(self) -> None:
        self.conn.close()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

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
