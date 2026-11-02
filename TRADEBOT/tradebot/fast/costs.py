"""All-in transaction cost estimates for every contemplated fast-engine entry and exit.

Components (bps of the USD size unless stated):
  spread_impact     what the quoted outAmount loses versus the reference: the round trip for an entry (buy then sell
                    the tokens the buy would deliver), or the sale versus mark value for an exit. Includes Jupiter's
                    platform fee (feeBps is already inside outAmount; it is reported separately for information).
  slippage_allow    extra loss if every leg fills at its minimum acceptable amount (otherAmountThreshold).
  network           signature + priority fees per transaction, from the quote's lamport fields when Jupiter sends them
                    (valued at the current SOL price), else an assumed $0.01 per transaction.
  rent              token-account rent the quote says would be paid (refundable, but cash out now), when present.
  adverse           the configured adverse-selection buffer (the quote you can act on is the one nobody else took).

total_expected = spread_impact + network + rent + adverse;  total_worst = total_expected + slippage_allow.
An entry is only worth taking if its expected move exceeds total_worst plus the minimum edge.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from ..scanner.costs import Quote

ASSUMED_TX_FEE_USD = 0.01
LAMPORTS_PER_SOL = 1e9


@dataclass
class CostEstimate:
    kind: str                      # "entry" (round trip) | "exit"
    symbol: str
    size_usd: float
    spread_impact_bps: float
    slippage_allow_bps: float
    jupiter_fee_bps: float | None
    network_fee_usd: float
    rent_usd: float
    adverse_bps: float
    total_expected_bps: float
    total_worst_bps: float
    total_worst_usd: float
    quote_age_sec: float
    fee_source: str                # "quote_lamports" | "assumed"

    def row(self) -> dict:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in asdict(self).items()}


def _leg_fee_usd(q: Quote, sol_usd: float | None) -> tuple[float, str]:
    if sol_usd and (q.signature_fee_lamports is not None or q.priority_fee_lamports is not None):
        lam = (q.signature_fee_lamports or 0) + (q.priority_fee_lamports or 0)
        return lam / LAMPORTS_PER_SOL * sol_usd, "quote_lamports"
    return ASSUMED_TX_FEE_USD, "assumed"


def _rent_usd(q: Quote, sol_usd: float | None) -> float:
    if sol_usd and q.rent_fee_lamports:
        return q.rent_fee_lamports / LAMPORTS_PER_SOL * sol_usd
    return 0.0


def estimate_entry(symbol: str, buy: Quote, sell: Quote, size_usd: float, token_decimals: int, sol_usd: float | None,
                   adverse_bps: float, now: float, usdc_decimals: int = 6) -> CostEstimate:
    """Round-trip cost of entering now: USDC -> token (buy) and the token straight back (sell)."""
    if size_usd <= 0 or buy.out_amount <= 0:
        raise ValueError("cannot estimate an empty entry")
    qty = buy.out_amount / 10 ** token_decimals
    qty_min = buy.min_out_amount / 10 ** token_decimals
    back = sell.out_amount / 10 ** usdc_decimals
    back_worst = sell.min_out_amount / 10 ** usdc_decimals * (qty_min / qty)
    f1, src1 = _leg_fee_usd(buy, sol_usd)
    f2, src2 = _leg_fee_usd(sell, sol_usd)
    net = f1 + f2
    rent = _rent_usd(buy, sol_usd)
    spread = (1 - back / size_usd) * 1e4
    slip = (back - back_worst) / size_usd * 1e4
    fixed = (net + rent) / size_usd * 1e4
    exp_bps = spread + fixed + adverse_bps
    worst = exp_bps + slip
    return CostEstimate("entry", symbol, size_usd, spread, slip, buy.fee_bps, net, rent, adverse_bps, exp_bps, worst,
                        worst / 1e4 * size_usd, max(0.0, now - min(buy.ts, sell.ts)),
                        "quote_lamports" if src1 == src2 == "quote_lamports" else "assumed")


def estimate_exit(symbol: str, sell: Quote, qty: float, mark_usd: float, sol_usd: float | None, now: float,
                  adverse_bps: float = 0.0, usdc_decimals: int = 6) -> CostEstimate:
    """Cost of exiting `qty` now versus its mark value (qty x reference price)."""
    value = qty * mark_usd
    if value <= 0:
        raise ValueError("cannot estimate an empty exit")
    proceeds = sell.out_amount / 10 ** usdc_decimals
    worst = sell.min_out_amount / 10 ** usdc_decimals
    fee, src = _leg_fee_usd(sell, sol_usd)
    spread = (1 - proceeds / value) * 1e4
    slip = (proceeds - worst) / value * 1e4
    exp_bps = spread + fee / value * 1e4 + adverse_bps
    total_worst = exp_bps + slip
    return CostEstimate("exit", symbol, value, spread, slip, sell.fee_bps, fee, 0.0, adverse_bps, exp_bps, total_worst,
                        total_worst / 1e4 * value, max(0.0, now - sell.ts), src)
