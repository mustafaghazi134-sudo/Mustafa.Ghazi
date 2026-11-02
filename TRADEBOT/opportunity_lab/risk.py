"""The lab's deterministic risk gateway. Pure function of the ledger, the open book and the clock. No AI input.
Every entry needs `check_entry() == []`. Exits are never blocked."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import LabConfig
from .ledger import LabLedger
from .models import PaperPosition


def _day_start(ts: float) -> float:
    return ts - ts % 86400


@dataclass
class Book:
    cash: float
    positions: dict[str, PaperPosition] = field(default_factory=dict)      # by position id
    marks: dict[str, float] = field(default_factory=dict)                  # mint -> last price
    reserved: float = 0.0                                                   # pending paper entries (USD)
    pending_mints: set = field(default_factory=set)

    def exposure(self) -> float:
        return sum(p.qty * self.marks.get(p.mint, p.entry_price) for p in self.positions.values()) + self.reserved

    def equity(self) -> float:
        return self.cash + self.exposure()


class LabRiskGateway:
    def __init__(self, cfg: LabConfig, ledger: LabLedger, clock=time.time):
        self.cfg, self.r, self.ledger, self.clock = cfg, cfg.risk, ledger, clock

    def halted(self) -> str | None:
        if self.cfg.halt_file.exists():
            try:
                return "lab halt: " + json.loads(self.cfg.halt_file.read_text()).get("reason", "")
            except (ValueError, OSError):
                return "lab halt"
        if Path(self.cfg.main_kill_file).exists():
            return "main bot kill switch (data/KILL) present"
        return None

    def halt(self, reason: str, source: str = "auto") -> None:
        self.cfg.halt_file.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.halt_file.write_text(json.dumps({"ts": self.clock(), "reason": reason, "source": source}))
        self.ledger.event("halt", f"{source}: {reason}", ts=self.clock())

    def realized_today(self) -> float:
        r = self.ledger.x("SELECT COALESCE(SUM(pnl_usd),0) s FROM trades WHERE exit_ts>=?", (_day_start(self.clock()),)).fetchone()
        return float(r["s"])

    def loss_streak(self) -> tuple[int, float | None]:
        rows = self.ledger.x("SELECT pnl_usd, exit_ts FROM trades ORDER BY exit_ts DESC LIMIT 50").fetchall()
        n = 0
        for r in rows:
            if r["pnl_usd"] < 0:
                n += 1
            else:
                break
        return n, (rows[0]["exit_ts"] if rows else None)

    def check_entry(self, mint: str, size_usd: float, book: Book) -> list[str]:
        p: list[str] = []
        now = self.clock()
        h = self.halted()
        if h:
            return [h]
        if size_usd > self.r.position_usd + 1e-9:
            p.append(f"size ${size_usd:.2f} > position limit ${self.r.position_usd:.2f}")
        if size_usd > book.cash:
            p.append("insufficient paper cash")
        n_open = len(book.positions) + len(book.pending_mints)
        if n_open >= self.r.max_open_positions:
            p.append(f"{n_open} open or pending positions (max {self.r.max_open_positions})")
        if mint in book.pending_mints:
            p.append("an entry for this token is already pending")
        if any(pos.mint == mint for pos in book.positions.values()):
            p.append("already holding this token")
        if book.exposure() + size_usd > self.r.max_exposure_usd + 1e-9:
            p.append(f"exposure ${book.exposure():.2f} + ${size_usd:.2f} > ${self.r.max_exposure_usd:.2f}")
        if self.ledger.count("intents", "side='buy' AND ts>=?", (_day_start(now),)) >= self.r.max_entries_per_day:
            p.append(f"daily entry cap {self.r.max_entries_per_day}")
        last = self.ledger.x("SELECT MAX(ts) t FROM intents WHERE side='buy' AND mint=?", (mint,)).fetchone()["t"]
        if last and now - last < self.r.token_cooldown_min * 60:
            p.append(f"token cooldown ({(now - last) / 60:.0f} of {self.r.token_cooldown_min:.0f} min)")
        if self.realized_today() <= -self.r.max_daily_loss_usd:
            p.append(f"daily loss limit ${self.r.max_daily_loss_usd:.0f} reached")
        eq = book.cash + book.exposure()          # cash here excludes reservations, exposure includes them
        peak = max(self.ledger.get("peak_equity", self.r.paper_capital_usd), eq)
        self.ledger.set("peak_equity", peak)
        if peak - eq >= self.r.max_drawdown_usd:
            reason = f"drawdown ${peak - eq:.2f} >= ${self.r.max_drawdown_usd:.2f}"
            self.halt(reason)
            p.append(reason)
        n, last_loss = self.loss_streak()
        if n >= self.r.loss_streak_pause and last_loss and now - last_loss < self.r.loss_streak_pause_min * 60:
            p.append(f"{n} losses in a row: paused")
        return p
