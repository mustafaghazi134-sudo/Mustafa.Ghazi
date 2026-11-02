"""The signer's own policy and the owner's activation record.

The policy lives in the signer home (`policy.json`) and is edited only by the owner. Any change to it changes its
hash, which invalidates the current activation. An activation is a time-limited (at most 24 h) record bound to the
wallet and the policy hash, authenticated with a secret that exists only in the signer home.
"""
from __future__ import annotations

import getpass
import hashlib
import hmac
import json
import time
from pathlib import Path

from pydantic import BaseModel, Field

from tradebot.execution.tx_verify import DEFAULT_ALLOWED_PROGRAMS, USDC_MINT, WSOL_MINT

MAX_ACTIVATION_HOURS = 24.0
CBBTC_MINT = "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij"
WETH_MINT = "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs"


class SignerPolicy(BaseModel):
    wallet: str = ""
    keypair_file: str = ""
    rpc_url: str = "https://api.mainnet-beta.solana.com"
    allowed_mints: list[str] = Field(default_factory=lambda: [USDC_MINT, WSOL_MINT, CBBTC_MINT, WETH_MINT])
    allowed_programs: list[str] = Field(default_factory=lambda: sorted(DEFAULT_ALLOWED_PROGRAMS))
    # Per-transaction maximum input, raw units per mint. Micro-live defaults: 10 USDC; 0.08 SOL, 0.00012 cbBTC and
    # 0.004 WETH (about $10-15 at SOL $150, BTC $100k, ETH $3k). The owner must re-check them against current prices
    # before activating; the trading process cannot change them.
    max_in_raw: dict[str, int] = Field(default_factory=lambda: {USDC_MINT: 10_000_000, WSOL_MINT: 80_000_000,
                                                                  CBBTC_MINT: 12_000, WETH_MINT: 400_000})
    daily_max_in_raw: dict[str, int] = Field(default_factory=lambda: {USDC_MINT: 50_000_000, WSOL_MINT: 400_000_000,
                                                                        CBBTC_MINT: 60_000, WETH_MINT: 2_000_000})
    max_signatures_per_day: int = 10
    max_sol_cost_lamports: int = 5_000_000          # fees + tips + rent per transaction
    max_priority_fee_lamports: int = 500_000
    max_external_sol_outflow_lamports: int = 500_000
    require_simulation: bool = True

    def policy_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(), sort_keys=True).encode()).hexdigest()


def load_policy(home: Path) -> SignerPolicy:
    return SignerPolicy.model_validate_json((home / "policy.json").read_text())


def _mac(secret: bytes, record: dict) -> str:
    return hmac.new(secret, json.dumps(record, sort_keys=True).encode(), hashlib.sha256).hexdigest()


def create_activation(secret: bytes, policy: SignerPolicy, hours: float, note: str = "", now: float | None = None) -> dict:
    if not 0 < hours <= MAX_ACTIVATION_HOURS:
        raise ValueError(f"activation must last between 0 and {MAX_ACTIVATION_HOURS:g} hours")
    now = time.time() if now is None else now
    try:
        who = getpass.getuser()
    except Exception:  # noqa: BLE001
        who = "unknown"
    rec = {"v": 1, "wallet": policy.wallet, "policy_hash": policy.policy_hash(), "issued_ts": now,
           "expires_ts": now + hours * 3600, "issued_by": who, "note": note[:200]}
    return {"record": rec, "mac": _mac(secret, rec)}


def verify_activation(doc: dict | None, secret: bytes, policy: SignerPolicy, now: float | None = None) -> tuple[bool, str]:
    now = time.time() if now is None else now
    if not doc:
        return False, "not activated"
    rec, mac = doc.get("record") or {}, doc.get("mac") or ""
    if not hmac.compare_digest(_mac(secret, rec), mac):
        return False, "activation record failed authentication"
    if rec.get("wallet") != policy.wallet:
        return False, "activation is for a different wallet"
    if rec.get("policy_hash") != policy.policy_hash():
        return False, "policy changed since activation (re-activate)"
    if rec.get("expires_ts", 0) - rec.get("issued_ts", 0) > MAX_ACTIVATION_HOURS * 3600 + 1:
        return False, "activation longer than the maximum"
    if now >= rec.get("expires_ts", 0):
        return False, "activation expired"
    if now < rec.get("issued_ts", 0) - 60:
        return False, "activation issued in the future"
    return True, f"active until {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(rec['expires_ts']))}"
