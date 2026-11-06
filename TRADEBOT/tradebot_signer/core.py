"""The signer's decision logic: refuse unless every independent condition holds, then sign exactly once per order.

Conditions, all checked on every request, in this order:
  1. no HALT file in the signer home;
  2. the build allows mainnet submission (tradebot/execution/submission.py);
  3. a valid owner activation (authenticated, unexpired, same wallet and policy hash);
  4. idempotency: a client order id is signed at most once; the same id with the same message returns the stored
     signature, a different message for the same id is refused;
  5. static transaction checks (tx_verify.inspect_transaction) with the signer's own allow-lists and caps;
  6. the transaction's blockhash is still valid (own RPC);
  7. simulation on the signer's own RPC: the wallet loses at most the per-transaction cap of the input mint, gains at
     least the claimed minimum output, pays at most the SOL cost cap, and nothing else it holds decreases;
  8. daily caps per input mint and on the number of signatures.
Only then is the transaction partially signed (the wallet's slot only; an RFQ maker adds its own later).
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable

import httpx
from solders.message import to_bytes_versioned
from solders.transaction import VersionedTransaction

from tradebot.execution import tx_verify as V
from tradebot.execution.signer_protocol import SignRefused
from tradebot.execution.simulation import alt_resolver_from_rpc, check_deltas, simulate_deltas
from tradebot.execution.submission import DISABLED_REASON, submission_enabled

from .keys import load_keypair_file, redact
from .policy import SignerPolicy, verify_activation

log = logging.getLogger("tradebot_signer")

SCHEMA = """
CREATE TABLE IF NOT EXISTS signatures (client_order_id TEXT PRIMARY KEY, ts REAL, day TEXT, message_hash TEXT,
    signature TEXT, signed_tx TEXT, input_mint TEXT, in_raw INTEGER, output_mint TEXT, min_out_raw INTEGER, request_id TEXT);
CREATE TABLE IF NOT EXISTS refusals (ts REAL, client_order_id TEXT, reason TEXT);
"""


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


class SignerCore:
    def __init__(self, home: Path, policy: SignerPolicy, activation_secret: bytes, keypair=None,
                 rpc: Callable[[str, list], dict] | None = None, clock=time.time):
        self.home = Path(home)
        self.policy = policy
        self._activation_secret = activation_secret
        self._kp = keypair if keypair is not None else load_keypair_file(policy.keypair_file)
        if str(self._kp.pubkey()) != policy.wallet:
            raise RuntimeError("the key file does not belong to the policy wallet")
        self.rpc = rpc or self._default_rpc()
        self.clock = clock
        self._lock = threading.Lock()
        self.db = sqlite3.connect(str(self.home / "signing_log.sqlite"), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        log.warning("signer ready for wallet %s", redact(policy.wallet))

    def __repr__(self) -> str:  # never expose key material
        return f"SignerCore(wallet={redact(self.policy.wallet)})"

    def _default_rpc(self):
        http = httpx.Client(timeout=10.0)

        def rpc(method: str, params: list) -> dict:
            r = http.post(self.policy.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                raise RuntimeError(f"rpc {method}: {data['error']}")
            return data["result"]
        return rpc

    # ------------------------------------------------------------------ state
    @property
    def halt_file(self) -> Path:
        return self.home / "HALT"

    def activation_status(self) -> tuple[bool, str]:
        f = self.home / "ACTIVATION.json"
        doc = json.loads(f.read_text()) if f.exists() else None
        return verify_activation(doc, self._activation_secret, self.policy, self.clock())

    def health(self) -> dict:
        active, why = self.activation_status()
        day = _day(self.clock())
        n = self.db.execute("SELECT COUNT(*) c FROM signatures WHERE day=?", (day,)).fetchone()["c"]
        return {"wallet": self.policy.wallet, "policy_hash": self.policy.policy_hash(), "active": active, "activation": why,
                "halted": self.halt_file.exists(), "submission_enabled_in_build": submission_enabled(),
                "signatures_today": n, "max_signatures_per_day": self.policy.max_signatures_per_day}

    def lookup(self, client_order_id: str) -> dict | None:
        r = self.db.execute("SELECT signature, signed_tx, ts, request_id FROM signatures WHERE client_order_id=?",
                            (client_order_id,)).fetchone()
        return dict(r) if r else None

    def _refuse(self, coid: str, reason: str) -> SignRefused:
        self.db.execute("INSERT INTO refusals VALUES(?,?,?)", (self.clock(), coid, reason[:500]))
        self.db.commit()
        log.warning("REFUSED %s: %s", coid, reason)
        return SignRefused(reason)

    # ------------------------------------------------------------------ signing
    def sign(self, client_order_id: str, tx_b64: str, claims: dict) -> dict:
        with self._lock:
            return self._sign_locked(str(client_order_id or ""), tx_b64, claims or {})

    def _sign_locked(self, coid: str, tx_b64: str, claims: dict) -> dict:
        if not coid or len(coid) > 128:
            raise self._refuse(coid, "invalid client order id")
        if self.halt_file.exists():
            raise self._refuse(coid, "signer halted (HALT file present)")
        if not submission_enabled():
            raise self._refuse(coid, DISABLED_REASON)
        active, why = self.activation_status()
        if not active:
            raise self._refuse(coid, f"not activated: {why}")
        try:
            tx = V.decode_transaction(tx_b64)
        except V.VerificationError as e:
            raise self._refuse(coid, "; ".join(e.problems)) from None
        msg_hash = hashlib.sha256(to_bytes_versioned(tx.message)).hexdigest()
        prior = self.db.execute("SELECT * FROM signatures WHERE client_order_id=?", (coid,)).fetchone()
        if prior is not None:
            if prior["message_hash"] == msg_hash:
                return {"signed_tx": prior["signed_tx"], "signature": prior["signature"], "idempotent_replay": True}
            raise self._refuse(coid, "this client order id was already signed for a different transaction")
        try:
            in_mint, out_mint = str(claims["input_mint"]), str(claims["output_mint"])
            in_raw, min_out = int(claims["in_amount_raw"]), int(claims["min_out_raw"])
        except (KeyError, TypeError, ValueError):
            raise self._refuse(coid, "claims must include input_mint, output_mint, in_amount_raw, min_out_raw") from None
        allowed = set(self.policy.allowed_mints)
        if in_mint not in allowed or out_mint not in allowed or in_mint == out_mint:
            raise self._refuse(coid, f"mint pair {in_mint}->{out_mint} not allowed")
        cap = self.policy.max_in_raw.get(in_mint)
        if cap is None or in_raw > cap:
            raise self._refuse(coid, f"input {in_raw} exceeds the per-transaction cap {cap} for {in_mint}")
        day = _day(self.clock())
        n_today = self.db.execute("SELECT COUNT(*) c FROM signatures WHERE day=?", (day,)).fetchone()["c"]
        if n_today >= self.policy.max_signatures_per_day:
            raise self._refuse(coid, f"daily signature cap {self.policy.max_signatures_per_day} reached")
        spent = self.db.execute("SELECT COALESCE(SUM(in_raw),0) s FROM signatures WHERE day=? AND input_mint=?", (day, in_mint)).fetchone()["s"]
        dcap = self.policy.daily_max_in_raw.get(in_mint, 0)
        if spent + in_raw > dcap:
            raise self._refuse(coid, f"daily cap for {in_mint}: {spent} + {in_raw} > {dcap}")
        tx_policy = V.TxPolicy(allowed_programs=frozenset(self.policy.allowed_programs), allowed_mints=frozenset(allowed),
                               max_priority_fee_lamports=self.policy.max_priority_fee_lamports,
                               max_external_sol_outflow_lamports=self.policy.max_external_sol_outflow_lamports,
                               max_rent_lamports=self.policy.max_sol_cost_lamports)
        try:
            rep = V.inspect_transaction(tx, self.policy.wallet, tx_policy, alt_resolver_from_rpc(self.rpc))
        except V.VerificationError as e:
            raise self._refuse(coid, "; ".join(e.problems)) from None
        problems = list(rep.problems)
        others = [s for s in rep.signers if s != self.policy.wallet]
        if len(others) > 1:
            problems.append("more than one co-signer")
        if rep.wallet_sol_cost_lamports > self.policy.max_sol_cost_lamports:
            problems.append(f"SOL cost {rep.wallet_sol_cost_lamports} exceeds {self.policy.max_sol_cost_lamports}")
        if problems:
            raise self._refuse(coid, "; ".join(problems))
        try:
            ok = self.rpc("isBlockhashValid", [rep.recent_blockhash, {"commitment": "confirmed"}])
            if not (ok.get("value") if isinstance(ok, dict) else ok):
                raise self._refuse(coid, "blockhash no longer valid")
            if self.policy.require_simulation:
                sim = simulate_deltas(self.rpc, tx_b64, self.policy.wallet, sorted(allowed))
                sp = check_deltas(sim, in_mint, out_mint, in_raw, min_out, self.policy.max_sol_cost_lamports)
                if sp:
                    raise self._refuse(coid, "; ".join(sp))
        except SignRefused:
            raise
        except Exception as e:  # noqa: BLE001 - no evidence, no signature
            raise self._refuse(coid, f"could not verify on chain: {type(e).__name__}: {e}") from None
        # Partial signature: fill only the wallet's slot (RFQ makers co-sign at /execute).
        sigs = list(tx.signatures)
        sigs[rep.wallet_signer_index] = self._kp.sign_message(to_bytes_versioned(tx.message))
        signed = VersionedTransaction.populate(tx.message, sigs)
        if not signed.verify_with_results()[rep.wallet_signer_index]:
            raise self._refuse(coid, "internal: produced signature does not verify")
        signed_b64 = base64.b64encode(bytes(signed)).decode()
        signature = str(sigs[rep.wallet_signer_index])
        self.db.execute("INSERT INTO signatures VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (coid, self.clock(), day, msg_hash, signature, signed_b64, in_mint, in_raw, out_mint, min_out,
                         str(claims.get("request_id", ""))[:128]))
        self.db.commit()
        log.warning("SIGNED %s sig=%s %s %d -> %s min %d", coid, signature, in_mint[:4], in_raw, out_mint[:4], min_out)
        return {"signed_tx": signed_b64, "signature": signature, "idempotent_replay": False}
