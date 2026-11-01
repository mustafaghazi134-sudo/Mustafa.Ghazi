"""Signing key handling for live mode.

The bot needs its own key because Phantom only signs when a human clicks. Create a *dedicated*
account in Phantom, fund it with risk capital only, export its private key (Settings -> Manage
Accounts -> account -> Show Private Key) and provide it via SOLANA_PRIVATE_KEY (base58) or a
path in SOLANA_KEYPAIR_FILE (Solana CLI JSON array). The key is never logged or persisted by the bot.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import base58
from solders.keypair import Keypair

ENV_KEY = "SOLANA_PRIVATE_KEY"
ENV_FILE = "SOLANA_KEYPAIR_FILE"


def load_keypair() -> Keypair:
    raw = os.environ.get(ENV_KEY, "").strip()
    if raw:
        if raw.startswith("["):
            return Keypair.from_bytes(bytes(json.loads(raw)))
        return Keypair.from_bytes(base58.b58decode(raw))
    path = os.environ.get(ENV_FILE, "").strip()
    if path:
        return Keypair.from_bytes(bytes(json.loads(Path(path).read_text())))
    raise RuntimeError(f"no signing key: set {ENV_KEY} (Phantom export) or {ENV_FILE}")


def redact(pubkey: str) -> str:
    return f"{pubkey[:4]}…{pubkey[-4:]}"
