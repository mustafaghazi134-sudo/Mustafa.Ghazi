"""Restart recovery and ledger self-checks for the fast engine.

reconcile_intents: settle order intents a previous process (or a timeout) left unresolved, on evidence only, the same
rules as the 4h engine: confirmed -> booked exactly once (atomic, skipped if a fill for that order already exists);
failed -> failed; not found AND blockhash expired -> expired; anything else stays pending, blocks its symbol, and is
re-checked every poll. For the paper broker after a restart, a paper fill that was never booked does not exist
anywhere, so its intent resolves to `expired` by the same rule.

verify_balances: recompute cash and every position from the fill history alone and compare with the book in memory,
the cash stored in the ledger, and the stored positions. Any difference is a bookkeeping fault.
"""
from __future__ import annotations

import logging
from typing import Callable

from ..models import Fill, Side
from ..portfolio.ledger import Ledger

log = logging.getLogger("tradebot.fast.recovery")


def reconcile_intents(ledger: Ledger, book, broker, clock: Callable[[], float],
                      on_unknown: Callable[[dict], None] | None = None, unknown_alert_sec: float = 300.0) -> dict[str, int]:
    counts = {"confirmed": 0, "failed": 0, "expired": 0, "abandoned": 0, "pending": 0}
    for row in ledger.pending_intents():
        age = clock() - row["ts"]
        if row["state"] == "created" or not row["tx_signature"]:
            ledger.update_intent(row["id"], state="abandoned", result="never signed")
            counts["abandoned"] += 1
            continue
        try:
            ev = broker.transaction_status(row["tx_signature"], row["signed_tx"])
        except Exception as e:  # noqa: BLE001 - unknown stays unknown
            log.error("reconcile: status lookup failed for %s: %s (stays UNKNOWN)", row["tx_signature"], e)
            ev = {"status": "lookup_failed", "blockhash_valid": None}
        status, valid = ev.get("status"), ev.get("blockhash_valid")
        if status == "confirmed" and ledger.fill_for_order(row["order_id"]) is not None:
            ledger.update_intent(row["id"], state="confirmed", result="already booked; confirmed by reconciliation")
            counts["confirmed"] += 1
        elif status == "confirmed":
            side = Side(row["side"])
            qty, notional = float(row["qty"]), float(row["notional_usd"])
            fill = Fill(order_id=row["order_id"], symbol=row["symbol"], side=side, qty=qty, price=notional / qty if qty else 0.0,
                        notional_usd=notional, fee_usd=0.0, ts=clock(), tx_signature=row["tx_signature"])
            try:
                with ledger.atomic():
                    book.apply_fill(fill, exit_reason="reconciled" if side == Side.SELL else "")
                    ledger.update_intent(row["id"], state="confirmed", result={"reconciled_from_intent": True, "fill_id": fill.id})
                counts["confirmed"] += 1
            except Exception as e:  # noqa: BLE001
                book.reload()
                log.critical("reconcile: booking %s failed (%s); stays pending", row["id"], e)
                counts["pending"] += 1
        elif status == "failed":
            ledger.update_intent(row["id"], state="failed", result="failed per broker evidence")
            counts["failed"] += 1
        elif status == "not_found" and valid is False:
            ledger.update_intent(row["id"], state="expired", result=f"not found and can no longer land (age {age:.0f}s)")
            counts["expired"] += 1
        else:
            ledger.update_intent(row["id"], result=f"UNKNOWN: status={status} blockhash_valid={valid} age={age:.0f}s")
            counts["pending"] += 1
            if on_unknown and age >= unknown_alert_sec:
                on_unknown({"intent": row["id"], "symbol": row["symbol"], "side": row["side"], "age_sec": round(age)})
    return counts


def expected_balances(ledger: Ledger, initial_cash: float) -> tuple[float, dict[str, float]]:
    """Cash and positions implied by the fill history alone (same arithmetic as PortfolioBook.apply_fill)."""
    cash, pos = float(initial_cash), {}
    for f in ledger._exec("SELECT side, symbol, qty, notional_usd, fee_usd FROM fills WHERE mode=? ORDER BY rowid", (ledger.mode,)):
        if f["side"] == "buy":
            cash -= f["notional_usd"]
            pos[f["symbol"]] = pos.get(f["symbol"], 0.0) + f["qty"]
        else:
            cash += f["notional_usd"] - f["fee_usd"]
            held = pos.get(f["symbol"], 0.0)
            pos[f["symbol"]] = held - min(f["qty"], held)
    return cash, {s: q for s, q in pos.items() if q > 1e-12}


def verify_balances(ledger: Ledger, book, rel_tol: float = 1e-9, abs_tol: float = 1e-6) -> list[str]:
    """[] when book, stored ledger state and fill history agree; otherwise one line per difference."""
    exp_cash, exp_pos = expected_balances(ledger, book.initial_capital)
    problems = []

    def close(a: float, b: float) -> bool:
        return abs(a - b) <= max(abs_tol, rel_tol * max(abs(a), abs(b)))

    stored_cash = ledger.get("cash", book.initial_capital)
    if not close(book.cash, exp_cash):
        problems.append(f"cash: book {book.cash:.6f} vs fills {exp_cash:.6f}")
    if not close(float(stored_cash), exp_cash):
        problems.append(f"cash: ledger {float(stored_cash):.6f} vs fills {exp_cash:.6f}")
    stored = {s: p.qty for s, p in ledger.load_positions().items()}
    mem = {s: p.qty for s, p in book.positions.items()}
    for sym in set(exp_pos) | set(stored) | set(mem):
        e, s_, m = exp_pos.get(sym, 0.0), stored.get(sym, 0.0), mem.get(sym, 0.0)
        if not close(m, e):
            problems.append(f"{sym}: book qty {m:.10g} vs fills {e:.10g}")
        if not close(s_, e):
            problems.append(f"{sym}: ledger qty {s_:.10g} vs fills {e:.10g}")
    return problems
