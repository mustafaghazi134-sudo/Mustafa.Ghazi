"""The separate signer: activation, independent checks, idempotency, authenticated loopback API, key hygiene.
Ephemeral keys only; RPC is the in-memory FakeChain. Nothing reaches a network or a real wallet."""
from __future__ import annotations

import json
import logging
import os
import threading
import time

import base58
import httpx
import pytest
from solders.keypair import Keypair

from jupiter_fixtures import FakeChain, build_tx
from tradebot.execution import submission
from tradebot.execution import tx_verify as V
from tradebot.execution.signer_protocol import (RequestVerifier, SignerAuthError, SignerClient, SignRefused, auth_headers,
                                                load_client_secret)
from tradebot_signer.core import SignerCore
from tradebot_signer.keys import KeyFileError, load_keypair_file
from tradebot_signer.policy import SignerPolicy, create_activation, verify_activation
from tradebot_signer.server import serve

SECRET = bytes(range(32))
CLAIMS = {"input_mint": V.USDC_MINT, "output_mint": V.WSOL_MINT, "in_amount_raw": 5_000_000, "min_out_raw": 30_000_000}


@pytest.fixture
def env(tmp_path):
    kp = Keypair()
    w = str(kp.pubkey())
    pol = SignerPolicy(wallet=w)
    chain = FakeChain(w, pol.allowed_mints)
    chain.set_token(V.USDC_MINT, 100_000_000)
    chain.swap_effect = {V.USDC_MINT: -5_000_000, V.WSOL_MINT: 31_000_000 - 60_000}
    core = SignerCore(tmp_path, pol, SECRET, keypair=kp, rpc=chain.rpc)
    return {"kp": kp, "w": w, "pol": pol, "chain": chain, "core": core, "home": tmp_path}


def activate(env, hours=1.0):
    (env["home"] / "ACTIVATION.json").write_text(json.dumps(create_activation(SECRET, env["pol"], hours)))


def test_refuses_while_the_build_switch_is_off_even_when_activated(env):
    assert submission.MAINNET_SUBMISSION_ENABLED is False
    activate(env)
    with pytest.raises(SignRefused, match="disabled in this build"):
        env["core"].sign("o1", build_tx(env["w"], out_mint=V.WSOL_MINT), CLAIMS)
    assert env["core"].lookup("o1") is None


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(submission, "MAINNET_SUBMISSION_ENABLED", True)


def test_activation_is_required_and_bounded(env, enabled, tmp_path):
    with pytest.raises(SignRefused, match="not activated"):
        env["core"].sign("o1", build_tx(env["w"], out_mint=V.WSOL_MINT), CLAIMS)
    with pytest.raises(ValueError):
        create_activation(SECRET, env["pol"], 25)
    doc = create_activation(SECRET, env["pol"], 1)
    assert verify_activation(doc, SECRET, env["pol"])[0]
    assert "authentication" in verify_activation(doc, b"x" * 32, env["pol"])[1]
    forged = json.loads(json.dumps(doc)); forged["record"]["expires_ts"] += 86400 * 30
    assert "authentication" in verify_activation(forged, SECRET, env["pol"])[1]
    assert "expired" in verify_activation(doc, SECRET, env["pol"], now=time.time() + 7200)[1]
    changed = env["pol"].model_copy(update={"max_signatures_per_day": 99})
    assert "policy changed" in verify_activation(doc, SECRET, changed)[1]
    other = env["pol"].model_copy(update={"wallet": str(Keypair().pubkey())})
    assert "different wallet" in verify_activation(doc, SECRET, other)[1]


def test_signs_once_per_order_and_is_idempotent(env, enabled):
    activate(env)
    tx = build_tx(env["w"], out_mint=V.WSOL_MINT)
    r1 = env["core"].sign("o1", tx, CLAIMS)
    r2 = env["core"].sign("o1", tx, CLAIMS)
    assert r1["signature"] == r2["signature"] and r2["idempotent_replay"] is True
    with pytest.raises(SignRefused, match="different transaction"):
        env["core"].sign("o1", build_tx(env["w"], out_mint=V.WSOL_MINT), CLAIMS)
    assert env["core"].lookup("o1")["signature"] == r1["signature"]


def test_partial_signature_fills_only_the_wallet_slot(env, enabled):
    activate(env)
    maker = str(Keypair().pubkey())
    tx = build_tx(env["w"], program=V.JUPITERZ_ORDER_ENGINE, extra_signer=maker, out_mint=V.WSOL_MINT)
    from solders.transaction import VersionedTransaction
    import base64
    signed = VersionedTransaction.from_bytes(base64.b64decode(env["core"].sign("rfq", tx, CLAIMS)["signed_tx"]))
    ok = signed.verify_with_results()
    assert ok[0] is True and ok[1] is False          # the maker co-signs later at /execute


@pytest.mark.parametrize("mutate, needle", [
    (lambda e: e["home"].joinpath("HALT").write_text("{}"), "halted"),
    (lambda e: e["chain"].swap_effect.update({V.USDC_MINT: -9_000_000}), "more than the order"),
    (lambda e: setattr(e["chain"], "blockhash_valid", False), "blockhash"),
    (lambda e: e["chain"].down.add("simulateTransaction"), "could not verify"),
])
def test_independent_checks_refuse(env, enabled, mutate, needle):
    activate(env)
    mutate(env)
    with pytest.raises(SignRefused, match=needle):
        env["core"].sign("o1", build_tx(env["w"], out_mint=V.WSOL_MINT), CLAIMS)
    assert env["core"].lookup("o1") is None


def test_caps_and_claims(env, enabled):
    activate(env)
    with pytest.raises(SignRefused, match="per-transaction cap"):
        env["core"].sign("big", build_tx(env["w"], out_mint=V.WSOL_MINT), dict(CLAIMS, in_amount_raw=50_000_000))
    with pytest.raises(SignRefused, match="not allowed"):
        env["core"].sign("bonk", build_tx(env["w"]), dict(CLAIMS, output_mint="DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"))
    with pytest.raises(SignRefused, match="claims must include"):
        env["core"].sign("bad", build_tx(env["w"]), {})
    bad = build_tx(env["w"], instructions=None, fee_payer=None)
    from jupiter_fixtures import sys_transfer, swap_ix, cu_limit
    drain = build_tx(env["w"], instructions=[cu_limit(200_000), swap_ix(env["w"]), sys_transfer(env["w"], str(Keypair().pubkey()), 10**9)])
    with pytest.raises(SignRefused, match="third parties"):
        env["core"].sign("drain", drain, CLAIMS)
    env["chain"].swap_effect = {V.USDC_MINT: -1_000_000, V.WSOL_MINT: 6_000_000}
    small = dict(CLAIMS, in_amount_raw=1_000_000, min_out_raw=5_000_000)
    for i in range(10):                              # daily signature cap (policy default 10)
        env["core"].sign(f"d{i}", build_tx(env["w"], out_mint=V.WSOL_MINT), small)
    with pytest.raises(SignRefused, match="daily signature cap"):
        env["core"].sign("d10", build_tx(env["w"], out_mint=V.WSOL_MINT), small)
    assert bad


def test_key_never_appears_in_logs_errors_or_repr(env, enabled, caplog, tmp_path):
    caplog.set_level(logging.DEBUG)
    activate(env)
    env["core"].sign("o1", build_tx(env["w"], out_mint=V.WSOL_MINT), CLAIMS)
    with pytest.raises(SignRefused):
        env["core"].sign("o1", build_tx(env["w"], out_mint=V.WSOL_MINT), CLAIMS)
    secret_b58 = base58.b58encode(bytes(env["kp"])).decode()
    secret_json = json.dumps(list(bytes(env["kp"])))
    text = caplog.text + repr(env["core"]) + json.dumps(env["core"].health())
    db = (env["home"] / "signing_log.sqlite").read_bytes()
    assert secret_b58 not in text and secret_json not in text and bytes(env["kp"])[:32] not in db
    f = tmp_path / "kp.json"
    f.write_text(secret_json)
    os.chmod(f, 0o644)
    with pytest.raises(KeyFileError, match="group/others") as ei:
        load_keypair_file(f)
    os.chmod(f, 0o600)
    assert str(load_keypair_file(f).pubkey()) == env["w"]
    f.write_text("[1, 2, 3, \"" + secret_b58 + "\"]")
    with pytest.raises(KeyFileError) as ei:
        load_keypair_file(f)
    assert secret_b58 not in str(ei.value) and ei.value.__cause__ is None


def test_request_authentication():
    v = RequestVerifier(SECRET, clock=lambda: 1000.0)
    h = auth_headers(SECRET, "POST", "/v1/sign", b"{}", ts=1000.0, nonce="n1")
    v.verify("POST", "/v1/sign", h, b"{}")
    for headers, body, path, needle in [
        (h, b"{}", "/v1/sign", "replayed"),
        (auth_headers(b"y" * 32, "POST", "/v1/sign", b"{}", ts=1000.0), b"{}", "/v1/sign", "bad signature"),
        (auth_headers(SECRET, "POST", "/v1/sign", b"{}", ts=900.0), b"{}", "/v1/sign", "window"),
        (auth_headers(SECRET, "POST", "/v1/sign", b"{}", ts=1000.0), b'{"x":1}', "/v1/sign", "bad signature"),
        (auth_headers(SECRET, "POST", "/v1/lookup", b"{}", ts=1000.0), b"{}", "/v1/sign", "bad signature"),
        ({}, b"{}", "/v1/sign", "missing"),
    ]:
        with pytest.raises(SignerAuthError, match=needle):
            v.verify("POST", path, headers, body)


def test_http_api_end_to_end_on_loopback(env, enabled):
    activate(env)
    srv = serve(env["core"], SECRET, port=0)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        client = SignerClient(f"http://127.0.0.1:{port}", SECRET)
        assert client.health()["wallet"] == env["w"] and client.health()["active"] is True
        res = client.sign("o1", build_tx(env["w"], out_mint=V.WSOL_MINT), CLAIMS)
        assert client.lookup("o1")["signature"] == res["signature"]
        assert client.lookup("nope") is None
        with pytest.raises(SignRefused):
            client.sign("o2", build_tx(env["w"], out_mint=V.WSOL_MINT), dict(CLAIMS, in_amount_raw=10**12))
        with pytest.raises(SignerAuthError):
            SignerClient(f"http://127.0.0.1:{port}", b"z" * 32).health()
        r = httpx.post(f"http://127.0.0.1:{port}/v1/sign", json={"client_order_id": "x"})
        assert r.status_code == 401
    finally:
        srv.shutdown()
    with pytest.raises(ValueError):
        serve(env["core"], SECRET, host="0.0.0.0", port=0)
    with pytest.raises(ValueError):
        SignerClient("http://10.0.0.5:8787", SECRET)


def test_client_secret_loading(tmp_path, monkeypatch):
    monkeypatch.delenv("TRADEBOT_SIGNER_SECRET", raising=False)
    with pytest.raises(SignerAuthError):
        load_client_secret("")
    f = tmp_path / "client_secret"
    f.write_text("ab" * 8)
    with pytest.raises(SignerAuthError, match="shorter"):
        load_client_secret(str(f))
    f.write_text("ab" * 32)
    assert len(load_client_secret(str(f))) == 32


def test_wrong_key_for_policy_is_refused_at_startup(tmp_path):
    with pytest.raises(RuntimeError, match="does not belong"):
        SignerCore(tmp_path, SignerPolicy(wallet=str(Keypair().pubkey())), SECRET, keypair=Keypair(), rpc=lambda m, p: {})


def test_activation_cli_refuses_non_interactive(tmp_path, monkeypatch):
    from tradebot_signer.__main__ import main
    kp = Keypair()
    f = tmp_path / "kp.json"
    f.write_text(json.dumps(list(bytes(kp))))
    os.chmod(f, 0o600)
    home = tmp_path / "signer"
    assert main(["init", "--home", str(home), "--keypair", str(f)]) == 0
    assert json.loads((home / "policy.json").read_text())["wallet"] == str(kp.pubkey())
    assert str(bytes(kp)) not in (home / "policy.json").read_text()
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert main(["activate", "--home", str(home), "--hours", "1"]) == 2
    assert not (home / "ACTIVATION.json").exists()
