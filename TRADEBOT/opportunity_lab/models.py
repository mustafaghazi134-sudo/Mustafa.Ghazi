"""Records exchanged between the four agents. Every market observation carries `observed_ts`: the time the lab
received it (information availability), which is the only time any decision may use it."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


@dataclass
class TokenSnapshot:
    mint: str
    observed_ts: float                      # when the lab received this observation
    source: str                             # e.g. "dexscreener", "geckoterminal", "synthetic", "replay:<file>"
    symbol: str = ""
    pair_address: str = ""
    dex_id: str = ""
    quote_symbol: str = ""
    price_usd: float | None = None
    liquidity_usd: float | None = None
    volume_m5: float | None = None
    volume_h1: float | None = None
    volume_h24: float | None = None
    buys_m5: int | None = None
    sells_m5: int | None = None
    buys_h1: int | None = None
    sells_h1: int | None = None
    price_change_m5: float | None = None    # percent
    price_change_h1: float | None = None
    fdv_usd: float | None = None
    pair_created_ts: float | None = None
    source_ts: float | None = None          # timestamp the source attaches, if any (never used for decisions)

    def age_min(self, now: float | None = None) -> float | None:
        if not self.pair_created_ts:
            return None
        return ((now if now is not None else self.observed_ts) - self.pair_created_ts) / 60.0

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.to_json().encode()).hexdigest()[:16]

    @classmethod
    def from_dict(cls, d: dict) -> "TokenSnapshot":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})


@dataclass
class MintFacts:
    """On-chain and indexer facts about a mint, each with where it came from. None means unknown."""
    mint: str
    observed_ts: float
    decimals: int | None = None
    token_program: str | None = None        # "spl-token" | "spl-token-2022"
    mint_authority: str | None = None       # None = revoked; "?" = unknown
    freeze_authority: str | None = None
    extensions: list[str] | None = None     # Token-2022 extension names; [] = none; None = unknown
    transfer_fee_bps: float | None = None
    top10_pct: float | None = None
    top1_pct: float | None = None
    holder_count: int | None = None
    jupiter_sus: bool | None = None
    organic_score: float | None = None
    provenance: dict = field(default_factory=dict)
    # per data item ("authorities", "holders", "indexer"): {"source", "ts", "http", "outcome", "state"} where state is
    # confirmed | missing (provider answered, no data) | failed (no answer: 429, throttled, network) | stale
    data_status: dict = field(default_factory=dict)
    indexer_audit: dict | None = None        # Jupiter tokens/v2 `audit` object, RECORDED ONLY (not used in decisions)

    @property
    def holders_confirmed(self) -> bool:
        return (self.data_status.get("holders") or {}).get("state") == "confirmed"

    @property
    def known(self) -> bool:
        return self.mint_authority != "?" and self.freeze_authority != "?" and self.extensions is not None


@dataclass
class RouteCheck:
    """Read-only quotes (no taker): can we buy `size_usd` and sell what we would receive?"""
    ok: bool
    buy_out_raw: int | None = None
    sell_back_usd: float | None = None
    round_trip_loss_bps: float | None = None
    price_impact_bps: float | None = None
    fee_bps: float | None = None
    router: str | None = None
    source: str = ""
    error: str = ""
    unknown: bool = False                    # no data either way (e.g. replay without a recorded check)
    retry_pending: bool = False              # unknown because a request got no answer (429, throttle, network): retry
    status: dict = field(default_factory=dict)   # {"buy": CallResult.record(...), "sell": ...}


@dataclass
class Opportunity:
    id: str
    mint: str
    detected_ts: float
    signals: dict
    snapshot: TokenSnapshot
    provenance: list[str]
    eligible: bool = True
    ineligible_reason: str = ""


@dataclass
class RiskAssessment:
    mint: str
    ts: float
    approved: bool
    score: float                             # 0 (worst) .. 100; informational only; `approved` is what counts
    flags: list[dict]                        # {"code", "severity": critical|high|medium|info, "detail"}
    route: RouteCheck | None
    facts: MintFacts | None
    data_gaps: list[str]
    # evidence: at least one blocking flag rests on observed data | missing_data_only: every blocking flag is a data
    # gap (safety UNKNOWN, neither safe nor dangerous) | approved: no blocking flag
    decision_basis: str = ""

    @property
    def reasons(self) -> list[str]:
        return [f"{f['code']}: {f['detail']}" for f in self.flags if f["severity"] in ("critical", "high")]


@dataclass
class TradeProposal:
    id: str
    opportunity_id: str
    mint: str
    symbol: str
    strategy: str
    entry_condition: str
    decision_price: float
    edge_pre_cost_bps: float | None          # calibrated estimate; None = uncalibrated
    edge_status: str                         # "calibrated" | "uncalibrated" | "negative"
    est_cost_bps: float
    stop_pct: float
    target_pct: float
    reward_risk: float
    size_usd: float
    accept: bool
    reasons: list[str]
    confidence_notes: list[str]
    ts: float = field(default_factory=time.time)
    ai_scores: dict = field(default_factory=dict)   # shadow only


@dataclass
class PaperPosition:
    id: str
    proposal_id: str
    mint: str
    symbol: str
    strategy: str
    entry_ts: float
    entry_price: float                       # effective, after entry costs
    qty: float
    cost_usd: float                          # cash paid including all entry costs
    entry_liquidity_usd: float
    high_water: float
    stop_price: float
    target_price: float
    vol_stop_price: float | None = None
    no_route_since: float | None = None
    state: str = "open"
