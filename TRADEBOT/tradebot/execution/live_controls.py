"""Live-only controls applied before any order is prepared for signing. They add to, and never relax, the shared
RiskEngine approval (which every order must already carry) and the signer's own caps.

  * emergency halt: `data/LIVE_HALT` (written by `tradebot live-halt`, or automatically) blocks every live order,
    buys and sells; the shared `data/KILL` blocks buys (sells stay possible so the engine can flatten);
  * strict serial execution: any unresolved intent blocks every new live order until reconciliation settles it;
  * per-order notional cap; daily order-count and notional caps;
  * wallet exposure cap: non-USDC holdings (marked to market) + this buy <= max_wallet_exposure_usd;
  * daily loss: realised losses in the ledger, or a fall in wallet value since the first snapshot of the UTC day,
    beyond max_daily_loss_usd halts buys for the rest of the day (and writes the halt file);
  * SOL reserve: the wallet keeps at least min_sol_reserve_lamports for fees (SOL sales cannot dip below it).
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config import LiveExecutionConfig
from ..models import Side
from ..portfolio.ledger import Ledger
from ..risk.kill_switch import KillSwitch

log = logging.getLogger(__name__)


@dataclass
class WalletSnapshot:
    """Native SOL is `lamports`; `holdings` are SPL token balances (UI units) and must not repeat native SOL."""
    lamports: int
    holdings: dict[str, float] = field(default_factory=dict)      # symbol -> token amount (UI units)
    prices: dict[str, float] = field(default_factory=dict)        # symbol -> USD
    quote_symbol: str = "USDC"

    def value(self, symbol: str) -> float:
        if symbol == self.quote_symbol:
            return self.holdings.get(symbol, 0.0)
        return self.holdings.get(symbol, 0.0) * self.prices.get(symbol, 0.0)

    @property
    def exposure_usd(self) -> float:
        """Everything that is not the quote stablecoin, native SOL included, at current prices."""
        tokens = sum(self.value(s) for s in self.holdings if s not in (self.quote_symbol, "SOL"))
        return tokens + self.lamports / 1e9 * self.prices.get("SOL", 0.0)

    @property
    def total_usd(self) -> float:
        return self.exposure_usd + self.holdings.get(self.quote_symbol, 0.0)


def _day_start(ts: float) -> float:
    return ts - (ts % 86400)


class LiveControls:
    def __init__(self, cfg: LiveExecutionConfig, ledger: Ledger, kill: KillSwitch, halt_file: Path, clock=time.time):
        self.cfg = cfg
        self.ledger = ledger
        self.kill = kill
        self.halt_file = Path(halt_file)
        self.clock = clock

    # ------------------------------------------------------------------ halt
    def halted(self) -> str | None:
        if self.halt_file.exists():
            try:
                return json.loads(self.halt_file.read_text()).get("reason", "halted")
            except (ValueError, OSError):
                return "halted"
        return None

    def halt(self, reason: str, source: str = "auto") -> None:
        self.halt_file.parent.mkdir(parents=True, exist_ok=True)
        self.halt_file.write_text(json.dumps({"ts": self.clock(), "reason": reason, "source": source}))
        self.ledger.log_event("live_halt", f"{source}: {reason}", ts=self.clock())
        log.critical("LIVE HALT (%s): %s", source, reason)

    def resume(self) -> None:
        if self.halt_file.exists():
            self.halt_file.unlink()
        self.ledger.log_event("live_resume", "live halt removed", ts=self.clock())

    # ------------------------------------------------------------------ counters
    def today_orders(self) -> tuple[int, float]:
        """(signed orders today, their notional) from the intent log: anything that reached a signature counts."""
        since = _day_start(self.clock())
        n, notional = 0, 0.0
        for r in self.ledger.intents(500):
            if r["ts"] >= since and r["tx_signature"]:
                n += 1
                notional += float(r["notional_usd"] or 0.0)
        return n, notional

    def start_of_day_value(self, snap: WalletSnapshot) -> float:
        key = f"live_sod_value:{time.strftime('%Y-%m-%d', time.gmtime(self.clock()))}"
        v = self.ledger.get(key)
        if v is None:
            v = snap.total_usd
            self.ledger.set(key, v)
        return float(v)

    # ------------------------------------------------------------------ the check
    def check(self, side: Side, symbol: str, notional_usd: float, snap: WalletSnapshot, sell_qty: float = 0.0,
              order_id: str | None = None) -> list[str]:
        p: list[str] = []
        h = self.halted()
        if h:
            return [f"live halt: {h}"]
        if side == Side.BUY and self.kill.is_tripped():
            p.append("kill switch tripped (buys blocked)")
        if symbol not in self.cfg.allowed_symbols:
            p.append(f"{symbol} is not in [live] allowed_symbols")
        pend = [r for r in self.ledger.pending_intents() if r["order_id"] != order_id]   # the order being placed has its own intent
        if pend:
            p.append(f"{len(pend)} unresolved intent(s); live orders are serial until reconciliation settles them")
        if side == Side.BUY and notional_usd > self.cfg.max_order_usd:
            p.append(f"order ${notional_usd:.2f} exceeds max_order_usd ${self.cfg.max_order_usd:.2f}")
        n, notional = self.today_orders()
        if n >= self.cfg.max_orders_per_day:
            p.append(f"{n} orders today (max {self.cfg.max_orders_per_day})")
        if notional + notional_usd > self.cfg.max_daily_notional_usd:
            p.append(f"daily notional ${notional + notional_usd:.2f} would exceed ${self.cfg.max_daily_notional_usd:.2f}")
        if side == Side.BUY and snap.exposure_usd + notional_usd > self.cfg.max_wallet_exposure_usd:
            p.append(f"wallet exposure ${snap.exposure_usd:.2f} + ${notional_usd:.2f} exceeds ${self.cfg.max_wallet_exposure_usd:.2f}")
        reserve = self.cfg.min_sol_reserve_lamports
        remaining = snap.lamports - (int(sell_qty * 1e9) if (side == Side.SELL and symbol == "SOL") else 0)
        if remaining < reserve:
            p.append(f"SOL reserve: {remaining} lamports would remain (min {reserve})")
        realised = self.ledger.realized_pnl_since(_day_start(self.clock()))
        drop = self.start_of_day_value(snap) - snap.total_usd
        if side == Side.BUY and (-realised >= self.cfg.max_daily_loss_usd or drop >= self.cfg.max_daily_loss_usd):
            reason = f"daily loss limit: realised ${-realised:.2f}, wallet value down ${drop:.2f} (max ${self.cfg.max_daily_loss_usd:.2f})"
            self.halt(reason)
            p.append(reason)
        return p
