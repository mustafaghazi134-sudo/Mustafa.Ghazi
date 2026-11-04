"""Live activation records: expiring, bound to the configuration, validated fail-closed.

A record says "the owner armed live trading at time T, for at most H hours, for exactly this configuration, on this
readiness evidence". `validate_record` returns the list of problems; an empty list means valid, anything else (a missing
field, a bad type, a hash mismatch, an expiry, an unknown flag) means NOT armed.

What this does and does not protect (full threat model: docs/LIVE_GATE_HARDENING.md section 2):

* `record_hash` detects accidental corruption and careless hand edits. It is NOT authentication: a process running as the
  trading user can compute it, exactly as it could forge any file the trading user can write.
* Real protection needs a signature checked by something the trading user cannot control. That is the `Attestor`
  interface below. No implementation is shipped, no key is generated here, and the signer service is untouched. Until one
  is approved, records carry `attestation.scheme == "none"` and `LiveGate.status()` reports `authenticated: False`.
  Setting `[engine] require_external_attestation = true` makes the gate refuse every record without a valid signature.
"""
from __future__ import annotations

import hashlib
import json
import math
import secrets
from dataclasses import dataclass, field
from typing import Any, Protocol

SCHEMA = 2
KIND = "live_arm"
MAX_TTL_HOURS = 24.0          # hard cap, deliberately not configurable
CLOCK_SKEW_SEC = 120.0        # tolerated clock error before a record is called "issued in the future"
CHALLENGE_TTL_SEC = 120.0     # how long a typed-back challenge stays valid
_FORBIDDEN_KEYS = ("forced", "force")


class Attestor(Protocol):
    """Independent protection for activation records. Implementations must keep their key where the trading user
    cannot read it (a separate OS account, an HSM or hardware token, a remote service). See the design document."""

    scheme: str

    def sign(self, payload: bytes) -> str: ...

    def verify(self, payload: bytes, signature: str) -> bool: ...


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False).encode()


def sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# ---- what an activation is bound to ---------------------------------------------------------------------------------

def config_binding(settings) -> dict:
    """Everything that, if edited, must disarm: risk, live and execution settings, the strategy set and parameters, the
    universe, starting capital and the readiness settings themselves. Paths, log level, data-provider URLs and the
    Claude analyst are deliberately excluded (they do not change what an order can be)."""
    e = settings.engine
    return {
        "v": SCHEMA,
        "risk": settings.risk.model_dump(),
        "live": settings.live.model_dump(),
        "execution": settings.execution.model_dump(),
        "strategies": [c.model_dump() for c in settings.strategies],
        "universe": settings.universe.model_dump(),
        "initial_capital_usd": settings.initial_capital_usd,
        "readiness": {
            "min_paper_trades_before_live": e.min_paper_trades_before_live,
            "min_paper_days_before_live": e.min_paper_days_before_live,
            "min_paper_coverage_before_live": e.min_paper_coverage_before_live,
            "equity_snapshot_interval_sec": e.equity_snapshot_interval_sec,
            "paper_gap_tolerance_intervals": e.paper_gap_tolerance_intervals,
            "require_external_attestation": e.require_external_attestation,
            "accept_imported_paper_evidence": e.accept_imported_paper_evidence,
            "evidence_allowed_repositories": sorted(e.evidence_allowed_repositories),
        },
    }


def config_digest(settings) -> str:
    return sha256_hex(canonical(config_binding(settings)))


def evidence_digest(settings) -> str:
    """What paper evidence must correspond to: the strategies, the universe and the risk limits that produced it."""
    return sha256_hex(canonical({"v": SCHEMA, "strategies": [c.model_dump() for c in settings.strategies],
                                 "universe": settings.universe.model_dump(), "risk": settings.risk.model_dump()}))


# ---- one-time challenge ---------------------------------------------------------------------------------------------

@dataclass
class Challenge:
    """Typed back by a human at a terminal. Lives only in memory (never written to disk) and works once."""
    code: str
    issued_at: float
    digest: str
    ttl_hours: float
    evidence: Any = None
    used: bool = False
    context: dict = field(default_factory=dict)


def validate_ttl(ttl_hours: float) -> float:
    if isinstance(ttl_hours, bool) or not isinstance(ttl_hours, (int, float)) or not math.isfinite(ttl_hours):
        raise ValueError("ttl must be a finite number of hours")
    if ttl_hours <= 0:
        raise ValueError("ttl must be positive")
    if ttl_hours > MAX_TTL_HOURS:
        raise ValueError(f"ttl {ttl_hours:g} h exceeds the {MAX_TTL_HOURS:g} h maximum")
    return float(ttl_hours)


def new_challenge(digest: str, ttl_hours: float, now: float, evidence: Any = None) -> Challenge:
    ttl = validate_ttl(ttl_hours)
    code = f"ARM-{digest[:8].upper()}-{secrets.token_hex(4).upper()}-{ttl:g}H"
    return Challenge(code=code, issued_at=now, digest=digest, ttl_hours=ttl, evidence=evidence)


# ---- the record -----------------------------------------------------------------------------------------------------

def build_record(*, digest: str, readiness: dict, ttl_hours: float, now: float, attestor: Attestor | None = None,
                 nonce: str | None = None) -> dict:
    ttl = validate_ttl(ttl_hours)
    rec: dict[str, Any] = {
        "schema": SCHEMA, "kind": KIND, "issued_at": float(now), "expires_at": float(now) + ttl * 3600.0,
        "ttl_hours": ttl, "nonce": nonce or secrets.token_hex(16), "config_digest": digest, "readiness": readiness,
        "attestation": {"scheme": attestor.scheme if attestor else "none"},
    }
    if attestor:
        rec["signature"] = attestor.sign(canonical({k: v for k, v in rec.items() if k != "signature"}))
    rec["record_hash"] = sha256_hex(canonical({k: v for k, v in rec.items() if k != "record_hash"}))
    return rec


def _num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def validate_record(data: Any, *, now: float, expected_digest: str, require_external: bool = False,
                    attestor: Attestor | None = None) -> list[str]:
    """Problems that make a record invalid; empty list = valid. Never raises on malformed input."""
    try:
        return _validate(data, now, expected_digest, require_external, attestor)
    except Exception as e:  # noqa: BLE001 - anything unexpected is a rejection, never an accept
        return [f"record could not be validated ({type(e).__name__})"]


def _validate(data: Any, now: float, expected_digest: str, require_external: bool, attestor: Attestor | None) -> list[str]:
    if not isinstance(data, dict):
        return ["record is not a JSON object"]
    p: list[str] = []
    bad = [k for k in _FORBIDDEN_KEYS if k in data]
    if bad:
        p.append(f"record carries a forbidden override flag ({', '.join(bad)})")
    if data.get("schema") != SCHEMA or data.get("kind") != KIND:
        p.append(f"unsupported record (schema {data.get('schema')!r}, kind {data.get('kind')!r}); re-run arm-live")
        return p
    iat, exp, ttl = data.get("issued_at"), data.get("expires_at"), data.get("ttl_hours")
    if not (_num(iat) and _num(exp) and _num(ttl)):
        return p + ["timestamps are missing or not finite numbers"]
    if exp <= iat:
        p.append("expiry is not after issue time")
    if (exp - iat) > MAX_TTL_HOURS * 3600.0 + 1.0 or ttl > MAX_TTL_HOURS or ttl <= 0:
        p.append(f"lifetime exceeds the {MAX_TTL_HOURS:g} h maximum")
    if abs((exp - iat) - ttl * 3600.0) > 1.0:
        p.append("ttl does not match issue/expiry times")
    if iat > now + CLOCK_SKEW_SEC:
        p.append("record was issued in the future (clock error or forged record)")
    if now >= exp:
        p.append("activation has expired; re-run arm-live")
    nonce = data.get("nonce")
    if not isinstance(nonce, str) or len(nonce) < 16:
        p.append("nonce missing or too short")
    if data.get("config_digest") != expected_digest:
        p.append("configuration changed since arming (risk / live / execution / strategies / universe / readiness); re-run arm-live")
    if not isinstance(data.get("readiness"), dict):
        p.append("readiness snapshot missing")
    claimed = data.get("record_hash")
    if not isinstance(claimed, str) or claimed != sha256_hex(canonical({k: v for k, v in data.items() if k != "record_hash"})):
        p.append("record hash mismatch (corrupted or edited)")
    att = data.get("attestation")
    scheme = att.get("scheme") if isinstance(att, dict) else None
    if scheme is None:
        p.append("attestation section missing")
    sig = data.get("signature")
    if attestor is not None:
        if scheme != attestor.scheme or not isinstance(sig, str) or not attestor.verify(
                canonical({k: v for k, v in data.items() if k not in ("signature", "record_hash")}), sig):
            p.append("signature missing or invalid")
    elif require_external:
        p.append("external attestation is required but no attestor is configured (nothing can be armed)")
    return p
