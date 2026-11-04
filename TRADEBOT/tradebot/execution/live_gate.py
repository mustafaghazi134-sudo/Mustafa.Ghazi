"""The live gate: independent conditions that must all hold before any real-money order is sent.

1. config.mode == "live"
2. env TRADEBOT_LIVE_TRADING == "I_ACCEPT_THE_RISK"
3. a valid, unexpired activation record in data/LIVE_ARMED written by an interactive `tradebot arm-live`:
   * bound to the risk, live, execution, strategy, universe and readiness settings (editing any of them disarms);
   * life of at most 24 h (default 12 h); an expired record is "not armed";
   * validated fail-closed: any malformed, edited, future-dated, flagged or unknown record is "not armed".
Plus, outside this module and unchanged: the build switch (execution/submission.py, MAINNET_SUBMISSION_ENABLED = False)
and the separate signer service. There is no override flag: the readiness requirements cannot be skipped.

See docs/LIVE_GATE_HARDENING.md for the threat model and for what a record can and cannot protect against.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from typing import Callable

from ..config import Settings
from ..portfolio.ledger import Ledger
from . import attestation as A
from .audit_log import AuditError, AuditSink, HashChainedAuditLog
from .evidence_import import EvidenceVerdict
from .readiness import PaperEvidence, ReadinessResult, evaluate, evidence_from_ledger

log = logging.getLogger(__name__)

ENV_LIVE = "TRADEBOT_LIVE_TRADING"
ENV_LIVE_VALUE = "I_ACCEPT_THE_RISK"


class LiveGateError(PermissionError):
    pass


class LiveGate:
    def __init__(self, settings: Settings, ledger: Ledger | None = None, *, clock: Callable[[], float] = time.time,
                 attestor: A.Attestor | None = None, audit_sinks: list[AuditSink] | None = None):
        self.s = settings
        self.ledger = ledger
        self.clock = clock
        self.attestor = attestor
        self.audit = HashChainedAuditLog(settings.audit_log_file, audit_sinks)

    # ---- binding -------------------------------------------------------------------------------------------------
    def limits_hash(self) -> str:
        """Legacy [risk]+[live] hash (kept for tools that print it). The activation is bound to `config_digest()`."""
        payload = json.dumps({"risk": self.s.risk.model_dump(), "live": self.s.live.model_dump()}, sort_keys=True).encode()
        return hashlib.sha256(payload).hexdigest()

    def config_digest(self) -> str:
        return A.config_digest(self.s)

    # ---- state ---------------------------------------------------------------------------------------------------
    def _read_record(self) -> tuple[dict | None, list[str]]:
        f = self.s.armed_file
        if not f.exists():
            return None, ["not armed (run: tradebot arm-live)"]
        try:
            data = json.loads(f.read_text())
        except (ValueError, OSError):
            return None, ["armed file is unreadable or not valid JSON"]
        return data, A.validate_record(data, now=self.clock(), expected_digest=self.config_digest(),
                                       require_external=self.s.engine.require_external_attestation, attestor=self.attestor)

    def status(self) -> dict:
        armed_file = self.s.armed_file
        data, problems = self._read_record()
        valid = data is not None and not problems
        d = data if isinstance(data, dict) else {}
        exp = d.get("expires_at") if A._num(d.get("expires_at")) else None
        info = {"mode": self.s.mode, "env_ok": os.environ.get(ENV_LIVE) == ENV_LIVE_VALUE, "armed_file": armed_file.exists(),
                "record_valid": valid, "problems": problems,
                # `limits_match` keeps its old name for existing callers; it now means "the whole binding still matches".
                "limits_match": valid or (data is not None and d.get("config_digest") == self.config_digest()),
                "armed_by": "interactive challenge" if valid else None, "armed_ts": d.get("issued_at") if A._num(d.get("issued_at")) else None,
                "expires_at": exp, "seconds_remaining": max(0.0, exp - self.clock()) if exp is not None and valid else None,
                "authenticated": bool(valid and (d.get("attestation") or {}).get("scheme") not in (None, "none")),
                "evidence_source": (d.get("readiness") or {}).get("source") if valid and isinstance(d.get("readiness"), dict) else None}
        info["armed"] = bool(self.s.is_live and info["env_ok"] and info["armed_file"] and valid)
        return info

    def is_armed(self) -> bool:
        try:
            self.assert_armed()
        except LiveGateError:
            return False
        return True

    def assert_armed(self) -> None:
        st = self.status()
        if not st["armed"]:
            missing = []
            if not self.s.is_live:
                missing.append('config mode != "live"')
            if not st["env_ok"]:
                missing.append(f"env {ENV_LIVE} != {ENV_LIVE_VALUE}")
            if not st["armed_file"]:
                missing.append("not armed (run: tradebot arm-live)")
            elif not st["record_valid"]:
                missing.extend(st["problems"])
            raise LiveGateError("live trading blocked: " + "; ".join(missing))
        # Order-time re-validation: if the record rests on this machine's own paper ledger, that ledger must still meet
        # the requirements (an edited or truncated ledger after arming un-arms). Imported evidence cannot be re-fetched
        # here; its staleness is bounded by the record's expiry.
        if st["evidence_source"] == "local_ledger" and self.ledger is not None:
            res = self.evaluate_readiness()
            if not res.ok:
                raise LiveGateError("live trading blocked: paper readiness no longer met: " + "; ".join(res.reasons))

    # ---- readiness -----------------------------------------------------------------------------------------------
    def evaluate_readiness(self, evidence: PaperEvidence | None = None) -> ReadinessResult:
        if evidence is None:
            if self.ledger is None:
                evidence = PaperEvidence("local_ledger", (), (), ())
            else:
                evidence = evidence_from_ledger(self.ledger)
        return evaluate(self.s, evidence, now=self.clock())

    def paper_record(self) -> tuple[int, float]:
        """(closed paper trades, days spanned by paper equity snapshots)."""
        r = self.evaluate_readiness()
        return r.trades, r.days

    def paper_coverage(self) -> float:
        """Share of the paper period covered by gap-free equity snapshots, excluding kill-switch time (0..1)."""
        return self.evaluate_readiness().coverage

    # ---- arming: interactive challenge, no override --------------------------------------------------------------
    def _audit(self, kind: str, payload: dict, *, required: bool) -> None:
        try:
            self.audit.append(kind, payload, ts=self.clock())
        except (AuditError, OSError) as e:
            if required:
                raise LiveGateError(f"audit log unavailable ({type(e).__name__}); refusing to change live state without a record") from e
            log.error("audit log write failed for %s: %s", kind, e)

    def begin_arm(self, ttl_hours: float | None = None, verdict: EvidenceVerdict | None = None) -> A.Challenge:
        """Check readiness, then return a one-time challenge for a human to type back. Refuses with no side effects
        other than an audit entry. Imported evidence is used only if its verdict says `accepted`."""
        try:
            ttl = A.validate_ttl(self.s.engine.live_arm_ttl_hours if ttl_hours is None else ttl_hours)
        except ValueError as e:
            raise LiveGateError(f"refused: {e}") from e
        evidence = None
        if verdict is not None:
            if not (self.s.engine.accept_imported_paper_evidence and verdict.accepted and verdict.evidence is not None):
                why = verdict.failures or ["imported evidence is disabled ([engine] accept_imported_paper_evidence = false)"]
                self._audit("arm_refused", {"reason": "imported evidence not accepted", "detail": why[:6]}, required=False)
                raise LiveGateError("refused: imported paper evidence not accepted: " + "; ".join(why))
            evidence = verdict.evidence
        res = self.evaluate_readiness(evidence)
        if not res.ok:
            self._audit("arm_refused", {"reasons": res.reasons, "readiness": res.summary()}, required=False)
            raise LiveGateError("refused: paper record insufficient: " + "; ".join(res.reasons))
        ch = A.new_challenge(self.config_digest(), ttl, self.clock(), evidence=evidence)
        ch.context = {"readiness": res.summary()}
        self._audit("arm_challenge_issued", {"ttl_hours": ttl, "config_digest": ch.digest, "source": res.source}, required=True)
        return ch

    def complete_arm(self, challenge: A.Challenge, typed: str) -> dict:
        now = self.clock()
        if challenge.used:
            raise LiveGateError("refused: this challenge was already used; start again")
        challenge.used = True                                   # one attempt per challenge, right or wrong
        if now - challenge.issued_at > A.CHALLENGE_TTL_SEC or now < challenge.issued_at - A.CLOCK_SKEW_SEC:
            self._audit("arm_refused", {"reason": "challenge expired"}, required=False)
            raise LiveGateError("refused: challenge expired")
        if not isinstance(typed, str) or not hmac.compare_digest(typed.strip().encode(), challenge.code.encode()):
            self._audit("arm_refused", {"reason": "challenge mismatch"}, required=False)
            raise LiveGateError("refused: challenge text did not match")
        if challenge.digest != self.config_digest():
            self._audit("arm_refused", {"reason": "configuration changed during arming"}, required=False)
            raise LiveGateError("refused: configuration changed while arming; start again")
        res = self.evaluate_readiness(challenge.evidence)
        if not res.ok:
            self._audit("arm_refused", {"reasons": res.reasons}, required=False)
            raise LiveGateError("refused: paper record insufficient: " + "; ".join(res.reasons))
        rec = A.build_record(digest=challenge.digest, readiness=res.summary(), ttl_hours=challenge.ttl_hours, now=now,
                             attestor=self.attestor)
        self._audit("armed", {"expires_at": rec["expires_at"], "config_digest": rec["config_digest"], "nonce": rec["nonce"],
                              "record_hash": rec["record_hash"], "source": res.source, "authenticated": self.attestor is not None},
                    required=True)
        self._write_record(rec)
        log.warning("LIVE TRADING ARMED until %s UTC (%s paper trades, %.1f days, %.0f%% coverage)",
                    time.strftime("%Y-%m-%d %H:%M", time.gmtime(rec["expires_at"])), res.trades, res.days, res.coverage * 100)
        return rec

    def _write_record(self, rec: dict) -> None:
        """The only writer of the armed file. Atomic replace, owner-only permissions where the OS supports them."""
        f = self.s.armed_file
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f.name + ".tmp")
        tmp.write_text(json.dumps(rec, indent=2, sort_keys=True))
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, f)

    def disarm(self, reason: str = "") -> None:
        existed = self.s.armed_file.exists()
        if existed:
            self.s.armed_file.unlink()
        self._audit("disarmed", {"reason": reason or "operator", "was_armed": existed}, required=False)
        log.warning("live trading disarmed")
