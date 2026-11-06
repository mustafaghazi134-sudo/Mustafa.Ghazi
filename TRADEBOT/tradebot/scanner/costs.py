"""Realistic round-trip cost of a hypothetical spot trade on Jupiter, from two size-specific read-only quotes.

All inputs are raw integer amounts as returned by `GET /swap/v2/order` (no taker). Nothing here talks to the network.

Cost components (per round trip, in basis points of the USD size):
  spread + impact   : implicit in (buy outAmount, sell outAmount) relative to the reference mid
  jupiter fee       : implicit in outAmount (feeBps is reported for information)
  network fees      : two transactions (base + typical priority fee), fixed USD
  adverse execution : assume each leg fills at its minimum acceptable amount (otherAmountThreshold), not the quote
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

NETWORK_FEE_USD_PER_TX = 0.01     # same assumption as the paper broker (SOLANA_TX_FEE_USD)


def _int_or_none(v) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


@dataclass
class Quote:
    in_amount: int
    out_amount: int
    min_out_amount: int           # otherAmountThreshold at the configured slippage
    price_impact_pct: float = 0.0
    fee_bps: float | None = None
    router: str = ""
    route: str = ""
    latency_ms: float = 0.0
    ts: float = 0.0
    signature_fee_lamports: int | None = None      # Swap v2 /order fee fields, when the response carries them
    priority_fee_lamports: int | None = None
    rent_fee_lamports: int | None = None

    @classmethod
    def from_order(cls, payload: dict, latency_ms: float = 0.0, ts: float = 0.0) -> "Quote":
        if payload.get("error"):
            raise ValueError(f"order error: {payload['error']}")
        out = int(payload["outAmount"])
        return cls(in_amount=int(payload["inAmount"]), out_amount=out,
                   min_out_amount=int(payload.get("otherAmountThreshold") or out),
                   price_impact_pct=float(payload.get("priceImpactPct") or 0.0), fee_bps=payload.get("feeBps"),
                   router=str(payload.get("router") or ""),
                   route="+".join(h.get("swapInfo", {}).get("label", "?") for h in payload.get("routePlan", []))[:80],
                   latency_ms=latency_ms, ts=ts,
                   signature_fee_lamports=_int_or_none(payload.get("signatureFeeLamports")),
                   priority_fee_lamports=_int_or_none(payload.get("prioritizationFeeLamports")),
                   rent_fee_lamports=_int_or_none(payload.get("rentFeeLamports")))


@dataclass
class RoundTripCost:
    size_usd: float
    expected_bps: float            # quoted outAmount both legs, plus network fees
    worst_case_bps: float          # otherAmountThreshold both legs, plus network fees
    buy_impact_bps: float
    sell_impact_bps: float
    jupiter_fee_bps: float | None
    network_fee_usd: float
    buy_price_usd: float           # effective USD per token on the buy leg (expected)
    buy_price_worst_usd: float     # effective USD per token if the buy filled at minimum out
    token_qty: float               # tokens received on the buy (expected)

    def row(self) -> dict:
        return asdict(self)


def round_trip_cost(buy: Quote, sell: Quote, size_usd: float, token_decimals: int, usdc_decimals: int = 6,
                    network_fee_usd: float = NETWORK_FEE_USD_PER_TX) -> RoundTripCost:
    """`buy` is USDC->token for size_usd, `sell` is token->USDC for the tokens `buy` would deliver."""
    if size_usd <= 0:
        raise ValueError("size must be positive")
    qty = buy.out_amount / 10 ** token_decimals
    qty_min = buy.min_out_amount / 10 ** token_decimals
    back = sell.out_amount / 10 ** usdc_decimals
    back_worst = sell.min_out_amount / 10 ** usdc_decimals
    fees = 2 * network_fee_usd
    # if the buy only delivered its minimum, the sell leg returns proportionally less
    scale = (qty_min / qty) if qty > 0 else 1.0
    expected = (1 - (back - fees) / size_usd) * 1e4
    worst = (1 - (back_worst * scale - fees) / size_usd) * 1e4
    return RoundTripCost(size_usd=size_usd, expected_bps=round(expected, 1), worst_case_bps=round(worst, 1),
                         buy_impact_bps=round(buy.price_impact_pct * 1e4, 1), sell_impact_bps=round(sell.price_impact_pct * 1e4, 1),
                         jupiter_fee_bps=buy.fee_bps, network_fee_usd=fees,
                         buy_price_usd=(size_usd / qty) if qty > 0 else float("inf"),
                         buy_price_worst_usd=(size_usd / qty_min) if qty_min > 0 else float("inf"), token_qty=qty)


def net_edge_bps(expected_move_bps: float, cost: RoundTripCost, adverse_buffer_bps: float = 0.0,
                 conservative: bool = True) -> float:
    """Expected net return of acting on a signal: expected favourable move minus the round-trip cost (worst case by
    default) minus an extra buffer for adverse selection (the quote you act on is the one nobody else wanted)."""
    c = cost.worst_case_bps if conservative else cost.expected_bps
    return round(expected_move_bps - c - adverse_buffer_bps, 1)


def realized_bps(entry_price_usd: float, exit_quote: Quote, token_qty: float, usdc_decimals: int = 6,
                 network_fee_usd: float = NETWORK_FEE_USD_PER_TX, size_usd: float | None = None) -> float:
    """Realised net return of a hypothetical position bought at entry_price_usd (already worst-case) and sold via
    `exit_quote` at its minimum-out amount, both network fees included."""
    size = size_usd if size_usd is not None else entry_price_usd * token_qty
    back = exit_quote.min_out_amount / 10 ** usdc_decimals
    return round(((back - 2 * network_fee_usd) / size - 1) * 1e4, 1)
