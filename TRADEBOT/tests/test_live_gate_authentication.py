"""`authenticated` can only ever come from an independent verifier that validated a signature.

Regression for a defect found in review of 12c5dea: a record that merely NAMED an attestation scheme was accepted when no
verifier was configured, and `LiveGate.status()` reported `authenticated=True` for it.
"""
import argparse
import hashlib
import hmac
import json
import re
from pathlib import Path

import pytest
from gate_helpers import arm_for_test, full_arm, paper_history

from tradebot import cli
from tradebot.config import Settings
from tradebot.execution import attestation as A
from tradebot.execution.live_gate import ENV_LIVE, ENV_LIVE_VALUE, LiveGate, LiveGateError
from tradebot.execution.submission import MAINNET_SUBMISSION_ENABLED, submission_enabled

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_800_000_000.0


def live(tmp_path) -> Settings:
    return Settings(mode="live", data_dir=str(tmp_path))


def rehash(rec: dict) -> dict:
    rec = {k: v for k, v in rec.items() if k != "record_hash"}
    rec["record_hash"] = A.sha256_hex(A.canonical(rec))
    return rec


def unsigned(s: Settings, now: float = NOW) -> dict:
    return A.build_record(digest=A.config_digest(s), readiness={"source": "test_fixture"}, ttl_hours=12, now=now)


class HmacAttestor:
    scheme = "test-hmac"

    def __init__(self, key=b"k" * 32):
        self.key = key

    def sign(self, payload):
        return hmac.new(self.key, payload, hashlib.sha256).hexdigest()

    def verify(self, payload, signature):
        return hmac.compare_digest(self.sign(payload), signature)


def signed(s, attestor=None, now=NOW):
    return A.build_record(digest=A.config_digest(s), readiness={"source": "test_fixture"}, ttl_hours=12, now=now,
                          attestor=attestor or HmacAttestor())


def check(rec, s, attestor=None, require_external=False, now=NOW + 60):
    return A.check_record(rec, now=now, expected_digest=A.config_digest(s), require_external=require_external, attestor=attestor)


# ---- 1. a record cannot grant itself authentication --------------------------------------------------------------------

@pytest.mark.parametrize("claimed", ["ed25519", "hmac-sha256", "totally-real-signature", "NONE", "None", " none", "none ", "",
                                     "   ", "sigstore", "github-attestation"])
def test_naming_a_scheme_without_a_configured_verifier_is_rejected(tmp_path, monkeypatch, claimed):
    monkeypatch.setenv(ENV_LIVE, ENV_LIVE_VALUE)
    s = live(tmp_path)
    gate = LiveGate(s)
    rec = unsigned(s, gate.clock())
    rec["attestation"] = {"scheme": claimed}
    s.armed_file.write_text(json.dumps(rehash(rec)))               # hash recomputed: "record hash matches"
    st = gate.status()
    assert st["authenticated"] is False and st["record_valid"] is False and st["armed"] is False
    assert not gate.is_armed()
    with pytest.raises(LiveGateError):
        gate.assert_armed()


@pytest.mark.parametrize("claimed", [None, 5, 1.5, True, ["ed25519"], {"x": 1}])
def test_non_string_schemes_are_rejected(tmp_path, claimed):
    s = live(tmp_path)
    rec = unsigned(s)
    rec["attestation"] = {"scheme": claimed}
    c = check(rehash(rec), s)
    assert c.problems and c.authenticated is False


def test_a_signature_field_on_an_unsigned_record_is_rejected(tmp_path):
    s = live(tmp_path)
    rec = unsigned(s)
    rec["signature"] = "ab" * 32
    c = check(rehash(rec), s)
    assert c.problems and c.authenticated is False


@pytest.mark.parametrize("extra", [{"authenticated": True}, {"verified": True}, {"signature_valid": True}])
def test_self_declared_authentication_fields_are_ignored(tmp_path, extra):
    s = live(tmp_path)
    rec = unsigned(s)
    rec.update(extra)
    rec["attestation"] = {"scheme": "none", **extra}
    c = check(rehash(rec), s)
    assert c.problems == [] and c.authenticated is False            # valid as an unsigned record, never authenticated


# ---- 2. a matching record hash is not authentication ----------------------------------------------------------------------

def test_a_valid_unsigned_record_is_valid_but_not_authenticated(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_LIVE, ENV_LIVE_VALUE)
    s = live(tmp_path)
    gate = arm_for_test(s)
    st = gate.status()
    assert st["record_valid"] and st["armed"] and st["authenticated"] is False
    assert st["auth_scheme"] == "none" and st["attestor_configured"] is False


def test_forging_the_hash_after_editing_never_yields_authentication(tmp_path):
    s = live(tmp_path)
    rec = unsigned(s)
    rec["readiness"] = {"source": "forged", "closed_trades": 10_000}
    c = check(rehash(rec), s)
    assert c.problems == [] and c.authenticated is False            # hash matches by construction; still not authenticated


def test_with_a_verifier_configured_a_hash_match_alone_is_rejected(tmp_path):
    s = live(tmp_path)
    c = check(rehash(unsigned(s)), s, attestor=HmacAttestor())
    assert c.authenticated is False and any("scheme" in p or "signature" in p for p in c.problems)


# ---- 3. only a verifier that returns True authenticates --------------------------------------------------------------------

def test_a_valid_signature_under_the_configured_scheme_authenticates(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_LIVE, ENV_LIVE_VALUE)
    s = live(tmp_path)
    att = HmacAttestor()
    gate = LiveGate(s, attestor=att)
    s.armed_file.write_text(json.dumps(signed(s, att, gate.clock())))
    st = gate.status()
    assert st["authenticated"] is True and st["armed"] is True and st["auth_scheme"] == "test-hmac" and st["attestor_configured"]


def test_edited_signed_record_rehashed_is_rejected_and_unauthenticated(tmp_path):
    s = live(tmp_path)
    rec = signed(s)
    rec["expires_at"] += 3600
    c = check(rehash(rec), s, attestor=HmacAttestor())
    assert c.authenticated is False and any("signature" in p for p in c.problems)


def test_wrong_key_is_rejected(tmp_path):
    s = live(tmp_path)
    c = check(signed(s, HmacAttestor(b"a" * 32)), s, attestor=HmacAttestor(b"b" * 32))
    assert c.authenticated is False and c.problems


def test_scheme_different_from_the_configured_verifiers_is_rejected(tmp_path):
    s = live(tmp_path)

    class Other(HmacAttestor):
        scheme = "other-scheme"

    c = check(signed(s, Other()), s, attestor=HmacAttestor())
    assert c.authenticated is False and any("not the configured verifier" in p for p in c.problems)


@pytest.mark.parametrize("returned", [1, "yes", "ok", object(), [1], None, 0, "", {}])
def test_only_a_literal_true_from_the_verifier_counts(tmp_path, returned):
    s = live(tmp_path)

    class Sloppy(HmacAttestor):
        def verify(self, payload, signature):
            return returned

    c = check(signed(s, Sloppy()), s, attestor=Sloppy())
    assert c.authenticated is False and c.problems


def test_a_verifier_that_raises_means_invalid_not_authenticated(tmp_path):
    s = live(tmp_path)

    class Boom(HmacAttestor):
        def verify(self, payload, signature):
            raise RuntimeError("verifier offline")

    c = check(signed(s, HmacAttestor()), s, attestor=Boom())
    assert c.authenticated is False and c.problems


@pytest.mark.parametrize("bad_scheme", ["none", "", "  ", None])
def test_an_attestor_without_a_real_scheme_can_neither_sign_nor_authenticate(tmp_path, bad_scheme):
    s = live(tmp_path)

    class Always:
        scheme = bad_scheme

        def sign(self, payload):
            return "sig"

        def verify(self, payload, signature):
            return True

    with pytest.raises(ValueError):
        A.build_record(digest=A.config_digest(s), readiness={}, ttl_hours=1, now=NOW, attestor=Always())
    c = check(unsigned(s), s, attestor=Always())                    # an always-true verifier must not bless an unsigned record
    assert c.authenticated is False and c.problems


def test_an_empty_signature_from_the_attestor_is_refused_at_build_time(tmp_path):
    s = live(tmp_path)

    class Empty(HmacAttestor):
        def sign(self, payload):
            return ""

    with pytest.raises(ValueError):
        A.build_record(digest="d", readiness={}, ttl_hours=1, now=NOW, attestor=Empty())


def test_unsigned_record_is_rejected_when_a_verifier_is_configured(tmp_path):
    s = live(tmp_path)
    c = check(unsigned(s), s, attestor=HmacAttestor())
    assert c.authenticated is False and c.problems


def test_other_defects_always_win_over_a_good_signature(tmp_path):
    s = live(tmp_path)
    att = HmacAttestor()
    expired = check(signed(s, att, NOW - 30 * 3600), s, attestor=att)
    assert expired.authenticated is False and any("expired" in p for p in expired.problems)
    changed = A.check_record(signed(s, att), now=NOW + 60, expected_digest="0" * 64, attestor=att)
    assert changed.authenticated is False and any("configuration changed" in p for p in changed.problems)


def test_require_external_attestation(tmp_path):
    s = live(tmp_path)
    assert check(unsigned(s), s, require_external=True).problems                    # no attestor: nothing validates
    att = HmacAttestor()
    ok = check(signed(s, att), s, attestor=att, require_external=True)
    assert ok.problems == [] and ok.authenticated is True


@pytest.mark.parametrize("junk", [None, [], "x", 7, {}, {"attestation": {"scheme": "ed25519"}}])
def test_non_records_are_never_authenticated(tmp_path, junk):
    s = live(tmp_path)
    c = check(junk, s, attestor=HmacAttestor())
    assert c.authenticated is False and c.problems


# ---- 4. production wiring and the real arming flow --------------------------------------------------------------------------

def test_real_arming_flow_without_an_attestor_is_unauthenticated_in_record_status_and_audit(tmp_path):
    s = live(tmp_path)
    gate = LiveGate(s, paper_history(s, 61, 0.97, 6))
    rec = full_arm(gate)
    assert rec["attestation"] == {"scheme": "none"} and "signature" not in rec
    assert gate.status()["authenticated"] is False
    armed_entry = [r for r in gate.audit.records() if r["kind"] == "armed"][0]
    assert armed_entry["payload"]["authenticated"] is False


def test_real_arming_flow_with_an_attestor_is_authenticated_until_the_file_is_edited(tmp_path):
    s = live(tmp_path)
    att = HmacAttestor()
    gate = LiveGate(s, paper_history(s, 61, 0.97, 6), attestor=att)
    rec = full_arm(gate)
    assert rec["attestation"] == {"scheme": "test-hmac"} and rec["signature"]
    assert gate.status()["authenticated"] is True
    assert [r for r in gate.audit.records() if r["kind"] == "armed"][0]["payload"]["authenticated"] is True
    rec["ttl_hours"] = 6.0
    s.armed_file.write_text(json.dumps(rehash(rec)))
    st = gate.status()
    assert st["authenticated"] is False and st["record_valid"] is False


def test_production_code_never_constructs_an_attestor_or_sets_authenticated():
    src = {p.name: p.read_text() for p in (ROOT / "tradebot").rglob("*.py")}
    assert src["attestation.py"].count("authenticated = True") == 1          # the single place, after verify(...) is True
    for name, text in src.items():
        if name != "attestation.py":
            assert "authenticated = True" not in text and '"authenticated": True' not in text, name
        if name not in ("live_gate.py", "attestation.py"):
            assert "attestor=" not in text, name
    assert "LiveGate(settings, ledger)" in src["bot.py"]                     # production passes no attestor
    # the only producer of the status field reads the checker's result
    assert 'check.authenticated' in src["live_gate.py"]


def test_cli_states_that_the_activation_is_unauthenticated_and_readiness_is_not_profitability(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TRADEBOT_DATA_DIR", str(tmp_path))
    paper_history(Settings(data_dir=str(tmp_path)), 61, 0.97, 6)
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": re.search(r"ARM-[0-9A-F]{8}-[0-9A-F]{8}-\d+H", prompt).group(0))
    ns = argparse.Namespace(config=str(tmp_path / "none.toml"), ttl_hours=None, evidence=None, evidence_sha256=None)
    assert cli.cmd_arm_live(ns) == 0
    out = capsys.readouterr().out
    assert "authenticated by an independent attestor: False" in out and "could forge it" in out
    assert "NOT evidence of profitability" in out and "strategy-evidence gate" in out
    assert cli.cmd_status(argparse.Namespace(config=str(tmp_path / "none.toml"), mode=None)) == 0
    assert "authenticated=False" in capsys.readouterr().out


# ---- 5. unchanged guarantees ------------------------------------------------------------------------------------------------

def test_thresholds_and_submission_switch_are_unchanged():
    e = Settings().engine
    assert (e.min_paper_trades_before_live, e.min_paper_days_before_live, e.min_paper_coverage_before_live) == (5, 60.0, 0.8)
    assert e.paper_gap_tolerance_intervals == 3.0 and e.live_arm_ttl_hours == 12.0 and e.max_single_outage_hours is None
    assert MAINNET_SUBMISSION_ENABLED is False and submission_enabled() is False


def test_a_fully_authenticated_activation_still_does_not_enable_submission(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_LIVE, ENV_LIVE_VALUE)
    s = live(tmp_path)
    att = HmacAttestor()
    gate = LiveGate(s, attestor=att)
    s.armed_file.write_text(json.dumps(signed(s, att, gate.clock())))
    gate.assert_armed()
    assert gate.status()["authenticated"] is True and submission_enabled() is False


def test_documentation_states_the_profitability_limit():
    for name in ("LIVE_GATE_HARDENING.md", "LIVE_GATE_REVIEW_2.md"):
        text = (ROOT / "docs" / name).read_text().lower()
        assert "does not establish profitability" in text or "not evidence of profitability" in text, name
        assert "strategy-evidence gate" in text or "strategy-validation gate" in text, name
