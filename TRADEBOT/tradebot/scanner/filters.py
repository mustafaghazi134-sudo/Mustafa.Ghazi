"""Deterministic rejection rules for scanner candidates. Every rejection carries a machine-readable reason so the
report can count how often each rule fires and how many rejected candidates would have been profitable (missed)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TokenFacts:
    """What Jupiter Tokens v2 tells us about a mint (field names per the official skill)."""
    mint: str
    symbol: str = ""
    decimals: int | None = None
    is_verified: bool = False
    is_sus: bool = False
    organic_score: float = 0.0
    liquidity_usd: float = 0.0
    volume_24h_usd: float = 0.0
    mint_authority_disabled: bool | None = None
    freeze_authority_disabled: bool | None = None
    top_holders_pct: float | None = None

    @classmethod
    def from_tokens_v2(cls, t: dict) -> "TokenFacts":
        st = t.get("stats24h") or {}
        audit = t.get("audit") or {}
        return cls(mint=t.get("id", ""), symbol=t.get("symbol", ""), decimals=t.get("decimals"),
                   is_verified=bool(t.get("isVerified")), is_sus=audit.get("isSus") is True,
                   organic_score=float(t.get("organicScore") or 0.0), liquidity_usd=float(t.get("liquidity") or 0.0),
                   volume_24h_usd=float(st.get("buyVolume") or 0.0) + float(st.get("sellVolume") or 0.0),
                   mint_authority_disabled=audit.get("mintAuthorityDisabled"),
                   freeze_authority_disabled=audit.get("freezeAuthorityDisabled"),
                   top_holders_pct=audit.get("topHoldersPercentage"))


@dataclass
class FilterLimits:
    min_liquidity_usd: float = 250_000.0
    min_volume_24h_usd: float = 500_000.0
    min_organic_score: float = 50.0
    require_verified: bool = True
    max_top_holders_pct: float = 60.0
    max_quote_age_sec: float = 10.0          # price older than this is stale
    max_price_slots_behind: int = 25         # ~10 s of Solana slots between price blockId and current slot
    max_round_trip_bps: float = 150.0        # beyond this the market is too thin for the size
    min_net_edge_bps: float = 20.0           # minimum expected net return after worst-case costs
    max_single_quote_impact_bps: float = 100.0
    stables: tuple[str, ...] = ("USDC", "USDT", "USDG", "PYUSD", "USDS", "DAI", "FDUSD", "USD1", "EURC", "USDe")
    allow_flags: tuple[str, ...] = field(default_factory=tuple)


def token_reasons(f: TokenFacts, lim: FilterLimits) -> list[str]:
    r: list[str] = []
    if f.symbol.upper() in lim.stables:
        r.append("stablecoin")
    if f.is_sus:
        r.append("audit.isSus")
    if lim.require_verified and not f.is_verified:
        r.append("not_verified")
    if f.organic_score < lim.min_organic_score:
        r.append(f"organic_score<{lim.min_organic_score:g}")
    if f.liquidity_usd < lim.min_liquidity_usd:
        r.append(f"liquidity<{lim.min_liquidity_usd:g}")
    if f.volume_24h_usd < lim.min_volume_24h_usd:
        r.append(f"volume24h<{lim.min_volume_24h_usd:g}")
    if f.mint_authority_disabled is False:
        r.append("mint_authority_active")
    if f.freeze_authority_disabled is False:
        r.append("freeze_authority_active")
    if f.top_holders_pct is not None and f.top_holders_pct > lim.max_top_holders_pct:
        r.append(f"top_holders>{lim.max_top_holders_pct:g}%")
    return r


def staleness_reasons(price_age_sec: float | None, slots_behind: int | None, lim: FilterLimits) -> list[str]:
    r: list[str] = []
    if price_age_sec is not None and price_age_sec > lim.max_quote_age_sec:
        r.append(f"price_age>{lim.max_quote_age_sec:g}s")
    if slots_behind is not None and slots_behind > lim.max_price_slots_behind:
        r.append(f"price_slots_behind>{lim.max_price_slots_behind}")
    return r


def cost_reasons(worst_case_bps: float, buy_impact_bps: float, sell_impact_bps: float, net_edge_bps: float,
                 lim: FilterLimits) -> list[str]:
    r: list[str] = []
    if worst_case_bps > lim.max_round_trip_bps:
        r.append(f"round_trip>{lim.max_round_trip_bps:g}bps")
    if max(buy_impact_bps, sell_impact_bps) > lim.max_single_quote_impact_bps:
        r.append(f"impact>{lim.max_single_quote_impact_bps:g}bps")
    if net_edge_bps < lim.min_net_edge_bps:
        r.append(f"net_edge<{lim.min_net_edge_bps:g}bps")
    return r
