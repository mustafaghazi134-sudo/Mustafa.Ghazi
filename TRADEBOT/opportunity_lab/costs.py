"""All-in cost model for paper fills. Conservative by construction; `cost_multiplier` scales every variable cost
for 2x / 3x stress tests.

Per leg, without a Jupiter quote: DEX fee (by dexId) + constant-product price impact
    impact = s / (R + s), R = liquidity_usd / 2 (the pool's quote-side reserve), s = trade size in USD
+ Token-2022 transfer fee (if any) + a fixed network fee (base + priority) per transaction + rent for a new token account
on the first buy. With a read-only Jupiter round-trip quote, the quoted round-trip loss replaces fee + impact.
An adverse-selection allowance (the price moving against a signal-driven entry) is added to every estimate.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import ExecutionModelConfig
from .models import RouteCheck


@dataclass
class LegCost:
    fee_bps: float
    impact_bps: float
    transfer_fee_bps: float
    network_usd: float

    @property
    def variable_bps(self) -> float:
        return self.fee_bps + self.impact_bps + self.transfer_fee_bps


def dex_fee_bps(dex_id: str, cfg: ExecutionModelConfig) -> float:
    return cfg.dex_fee_bps.get((dex_id or "").lower(), cfg.default_dex_fee_bps)


def cpmm_impact_bps(size_usd: float, liquidity_usd: float | None) -> float:
    if not liquidity_usd or liquidity_usd <= 0:
        return 10_000.0
    r = liquidity_usd / 2.0
    return size_usd / (r + size_usd) * 1e4


def leg_cost(size_usd: float, liquidity_usd: float | None, dex_id: str, cfg: ExecutionModelConfig,
             transfer_fee_bps: float = 0.0) -> LegCost:
    m = cfg.cost_multiplier
    return LegCost(fee_bps=dex_fee_bps(dex_id, cfg) * m, impact_bps=cpmm_impact_bps(size_usd, liquidity_usd) * m,
                   transfer_fee_bps=transfer_fee_bps, network_usd=cfg.network_fee_usd * m)


@dataclass
class RoundTripEstimate:
    total_bps: float
    parts: dict

    def __str__(self) -> str:
        return f"{self.total_bps:.0f} bps " + " ".join(f"{k}={v:.0f}" for k, v in self.parts.items())


def round_trip_bps(size_usd: float, liquidity_usd: float | None, dex_id: str, cfg: ExecutionModelConfig,
                   adverse_bps: float, route: RouteCheck | None = None, transfer_fee_bps: float = 0.0) -> RoundTripEstimate:
    """Total expected cost of buying `size_usd` and selling it back immediately, in bps of the size."""
    m = cfg.cost_multiplier
    if route is not None and route.ok and route.round_trip_loss_bps is not None:
        swap = max(route.round_trip_loss_bps, 0.0) * m
        src = "jupiter_quote"
    else:
        leg = leg_cost(size_usd, liquidity_usd, dex_id, cfg)
        swap = 2 * (leg.fee_bps + leg.impact_bps)
        src = "cpmm_model"
    fixed = (2 * cfg.network_fee_usd * m + cfg.ata_rent_usd) / size_usd * 1e4
    tf = 2 * transfer_fee_bps
    adverse = adverse_bps * m
    return RoundTripEstimate(swap + fixed + tf + adverse, {"swap_" + src: swap, "network_rent": fixed, "transfer_fee": tf,
                                                           "adverse": adverse})


def buy_fill(size_usd: float, mid: float, liquidity_usd: float | None, dex_id: str, cfg: ExecutionModelConfig,
             transfer_fee_bps: float = 0.0, first_buy: bool = True) -> tuple[float, float, float]:
    """-> (token qty received, effective price incl. all costs, fees_usd). Cash spent is exactly `size_usd`."""
    leg = leg_cost(size_usd, liquidity_usd, dex_id, cfg, transfer_fee_bps)
    fixed = leg.network_usd + (cfg.ata_rent_usd if first_buy else 0.0)
    swap_usd = max(size_usd - fixed, 0.0)
    qty = swap_usd / mid * (1 - (leg.fee_bps + leg.impact_bps) / 1e4) * (1 - transfer_fee_bps / 1e4)
    fees = size_usd - qty * mid
    return qty, (size_usd / qty if qty > 0 else float("inf")), fees


def sell_fill(qty: float, mid: float, liquidity_usd: float | None, dex_id: str, cfg: ExecutionModelConfig,
              transfer_fee_bps: float = 0.0) -> tuple[float, float]:
    """-> (USD proceeds after all costs, fees_usd)."""
    gross = qty * mid
    leg = leg_cost(gross, liquidity_usd, dex_id, cfg, transfer_fee_bps)
    proceeds = gross * (1 - transfer_fee_bps / 1e4) * (1 - (leg.fee_bps + leg.impact_bps) / 1e4) - leg.network_usd
    proceeds = max(proceeds, 0.0)
    return proceeds, gross - proceeds
