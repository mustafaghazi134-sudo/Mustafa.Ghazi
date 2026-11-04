"""Bringing GitHub PAPER evidence into the local readiness assessment: read-only, and distrusted by default.

The paper ledger that matters may live on a GitHub runner, not on the machine that runs `arm-live`. A file copied from
there is only a claim. This module turns it into usable evidence only if EVERY check below passes, and it never writes to
the local ledger.

  1. structure     schema, mode == "paper", finite and ordered timestamps, bounded sizes, nothing from the future
  2. integrity     the bundle's content hash recomputes AND equals a hash the operator obtained out-of-band
                   (e.g. printed in the Actions run log). A hash inside the file alone proves nothing.
  3. correspondence the bundle was produced under the same strategies, universe and risk limits as the config being armed
  4. provenance    source repository is on the allow-list, required run metadata is present, and an independent
                   `ProvenanceVerifier` (e.g. GitHub artifact attestation / Sigstore) confirms that this exact hash was
                   produced by that workflow run. No verifier ships in this change, so imported evidence is reported as
                   NOT authenticated and is not accepted for arming.

`accepted` requires all four. A bundle that passes 1-3 but not 4 is shown to the operator as "self-consistent, unproven".
Nothing here is produced by the running workflows: generating a bundle on the runner needs a workflow change that has not
been made (it needs owner approval).
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from .attestation import canonical, evidence_digest, sha256_hex
from .readiness import HALT_KINDS, PaperEvidence

SCHEMA = "tradebot.paper_evidence.v1"
MAX_EQUITY, MAX_TRADES, MAX_EVENTS = 1_000_000, 200_000, 100_000
REQUIRED_PROVENANCE = ("source", "repository", "workflow", "run_id", "commit_sha")
CLOCK_SKEW_SEC = 300.0


class ProvenanceVerifier(Protocol):
    """Independent confirmation that `content_sha256` was produced by the run named in `provenance`."""

    def verify(self, content_sha256: str, provenance: dict) -> bool: ...


def _finite(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def content_hash(bundle: dict) -> str:
    return sha256_hex(canonical({k: v for k, v in bundle.items() if k != "content_sha256"}))


def export_bundle(ledger, settings, provenance: dict, *, now: float | None = None) -> dict:
    """Read-only export of the paper ledger. Run it where the paper engine ran."""
    now = time.time() if now is None else now
    bundle = {
        "schema": SCHEMA, "mode": "paper", "as_of": now, "provenance": dict(provenance),
        "binding": {"evidence_digest": evidence_digest(settings)},
        "equity": [[ts, v] for ts, v in ledger.equity_points(mode="paper")],
        "trades": [{"entry_ts": t.entry_ts, "exit_ts": t.exit_ts, "pnl_usd": t.pnl_usd, "fees_usd": t.fees_usd,
                    "symbol": t.symbol, "strategy": t.strategy} for t in ledger.trades(mode="paper")],
        "events": [[ts, k] for ts, k in ledger.events_of_kinds(HALT_KINDS, mode="paper")],
    }
    bundle["content_sha256"] = content_hash(bundle)
    return bundle


@dataclass
class EvidenceVerdict:
    accepted: bool
    authenticated: bool
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    evidence: PaperEvidence | None = None
    content_sha256: str | None = None

    @property
    def failures(self) -> list[str]:
        return [f"{n}: {d}" for n, ok, d in self.checks if not ok]


def verify_bundle(bundle: Any, settings, *, expected_sha256: str | None, now: float | None = None,
                  verifier: ProvenanceVerifier | None = None) -> EvidenceVerdict:
    now = time.time() if now is None else now
    checks: list[tuple[str, bool, str]] = []

    def chk(name: str, ok: bool, detail: str = "") -> bool:
        checks.append((name, bool(ok), detail))
        return bool(ok)

    try:
        return _verify(bundle, settings, expected_sha256, now, verifier, checks, chk)
    except Exception as e:  # noqa: BLE001 - malformed input is a rejection
        chk("parse", False, f"bundle could not be processed ({type(e).__name__})")
        return EvidenceVerdict(False, False, checks)


def _verify(bundle, settings, expected_sha256, now, verifier, checks, chk) -> EvidenceVerdict:
    if not chk("structure.object", isinstance(bundle, dict), "bundle is not a JSON object"):
        return EvidenceVerdict(False, False, checks)
    ok = chk("structure.schema", bundle.get("schema") == SCHEMA and bundle.get("mode") == "paper",
             f"schema/mode must be {SCHEMA}/paper")
    eq, trades, events = bundle.get("equity"), bundle.get("trades"), bundle.get("events")
    ok &= chk("structure.lists", isinstance(eq, list) and isinstance(trades, list) and isinstance(events, list)
              and len(eq) <= MAX_EQUITY and len(trades) <= MAX_TRADES and len(events) <= MAX_EVENTS, "missing or oversized lists")
    if not ok:
        return EvidenceVerdict(False, False, checks)
    as_of = bundle.get("as_of")
    ok &= chk("structure.as_of", _finite(as_of) and as_of <= now + CLOCK_SKEW_SEC, "as_of missing or in the future")
    good_eq = all(isinstance(r, list) and len(r) == 2 and _finite(r[0]) and _finite(r[1]) for r in eq)
    good_ev = all(isinstance(r, list) and len(r) == 2 and _finite(r[0]) and r[1] in HALT_KINDS for r in events)
    good_tr = all(isinstance(t, dict) and all(_finite(t.get(k)) for k in ("entry_ts", "exit_ts", "pnl_usd")) for t in trades)
    ok &= chk("structure.values", good_eq and good_ev and good_tr, "non-finite or malformed values")
    if ok and _finite(as_of):
        stamps = [r[0] for r in eq] + [r[0] for r in events] + [t["exit_ts"] for t in trades]
        ok &= chk("structure.no_future", all(ts <= as_of + CLOCK_SKEW_SEC for ts in stamps), "records dated after as_of")
        ok &= chk("structure.trade_order", all(t["entry_ts"] <= t["exit_ts"] for t in trades), "trade exits before it enters")
    # integrity
    digest = content_hash(bundle)
    ok &= chk("integrity.self_hash", bundle.get("content_sha256") == digest, "content hash does not match the content")
    pinned = isinstance(expected_sha256, str) and len(expected_sha256) == 64 and expected_sha256.lower() == digest
    ok &= chk("integrity.pinned_hash", pinned, "no out-of-band expected hash supplied, or it differs" if not pinned else "")
    # correspondence
    bound = (bundle.get("binding") or {}).get("evidence_digest") if isinstance(bundle.get("binding"), dict) else None
    ok &= chk("correspondence.config", bound == evidence_digest(settings),
              "produced under different strategies / universe / risk limits than the configuration being armed")
    # provenance
    prov = bundle.get("provenance") if isinstance(bundle.get("provenance"), dict) else {}
    ok &= chk("provenance.fields", all(isinstance(prov.get(k), str) and prov.get(k) for k in REQUIRED_PROVENANCE),
              f"required: {', '.join(REQUIRED_PROVENANCE)}")
    allowed = settings.engine.evidence_allowed_repositories
    ok &= chk("provenance.repository", prov.get("repository") in allowed if allowed else False,
              "repository not on [engine] evidence_allowed_repositories" if allowed else "allow-list is empty")
    authenticated = False
    if verifier is None:
        chk("provenance.authenticated", False, "no independent provenance verifier is configured: origin is unproven")
    else:
        authenticated = bool(verifier.verify(digest, prov))
        chk("provenance.authenticated", authenticated, "independent verifier rejected this hash/provenance")
    accepted = bool(ok and authenticated)
    ev = None
    if all(c[1] for c in checks if c[0].startswith(("structure", "integrity", "correspondence"))):
        ev = PaperEvidence("imported_bundle", tuple((float(a), float(b)) for a, b in eq),
                           tuple({k: t.get(k) for k in ("entry_ts", "exit_ts", "pnl_usd", "fees_usd", "symbol", "strategy")} for t in trades),
                           tuple((float(a), str(k)) for a, k in events), dict(prov))
    return EvidenceVerdict(accepted, authenticated, checks, ev, digest)
