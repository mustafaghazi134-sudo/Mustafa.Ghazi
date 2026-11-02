"""Loading the wallet key inside the signer process only. The key is never printed, logged, returned or persisted
by this package; errors name the file, never its content."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from solders.keypair import Keypair


class KeyFileError(RuntimeError):
    pass


def load_keypair_file(path: str | Path, check_permissions: bool = True) -> Keypair:
    """A Solana CLI keypair file (JSON array of 64 integers). On POSIX the file must not be readable by group or
    others. On Windows, restrict it with NTFS permissions to the signer's own account (docs: icacls)."""
    p = Path(path)
    if not p.is_file():
        raise KeyFileError(f"key file not found: {p}")
    if check_permissions and os.name == "posix":
        mode = p.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise KeyFileError(f"key file {p} is accessible by group/others (chmod 600 it)")
    try:
        data = json.loads(p.read_text())
        if not (isinstance(data, list) and len(data) == 64 and all(isinstance(x, int) and 0 <= x < 256 for x in data)):
            raise ValueError("expected a JSON array of 64 bytes")
        return Keypair.from_bytes(bytes(data))
    except Exception as e:  # noqa: BLE001 - never echo the content
        raise KeyFileError(f"key file {p} is not a valid Solana keypair file ({type(e).__name__})") from None


def redact(pubkey: str) -> str:
    return f"{pubkey[:4]}…{pubkey[-4:]}" if pubkey else "?"
