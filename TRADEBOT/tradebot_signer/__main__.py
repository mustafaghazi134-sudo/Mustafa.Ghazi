"""Owner-only command line for the signer service.

    python -m tradebot_signer init --home DIR --keypair PATH   # create secrets + default policy (reads only the PUBLIC key)
    python -m tradebot_signer status --home DIR
    python -m tradebot_signer activate --home DIR --hours 2     # interactive, typed confirmation, max 24 h
    python -m tradebot_signer deactivate --home DIR
    python -m tradebot_signer halt --home DIR [--reason ...]
    python -m tradebot_signer resume --home DIR
    python -m tradebot_signer serve --home DIR [--port 8787]

Run every command as the signer's own OS account. `activate` refuses when stdin is not an interactive terminal.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import sys
import time
from pathlib import Path

from .keys import load_keypair_file, redact
from .policy import MAX_ACTIVATION_HOURS, SignerPolicy, create_activation, load_policy, verify_activation


def _write_private(path: Path, text: str) -> None:
    path.write_text(text)
    if os.name == "posix":
        os.chmod(path, 0o600)


def _secret(home: Path, name: str) -> bytes:
    return bytes.fromhex((home / name).read_text().strip())


def cmd_init(a) -> int:
    home = Path(a.home)
    home.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(home, 0o700)
    if (home / "policy.json").exists():
        print(f"{home} is already initialised; edit policy.json by hand (this deactivates the signer)")
        return 1
    kp = load_keypair_file(a.keypair)
    pol = SignerPolicy(wallet=str(kp.pubkey()), keypair_file=str(Path(a.keypair).resolve()), rpc_url=a.rpc_url)
    _write_private(home / "policy.json", pol.model_dump_json(indent=2))
    _write_private(home / "activation_secret", secrets.token_hex(32))
    _write_private(home / "client_secret", secrets.token_hex(32))
    print(f"initialised {home} for wallet {pol.wallet}")
    print(f"copy {home / 'client_secret'} to the trading account (its [live] signer_secret_file); "
          "never copy activation_secret or the key file")
    return 0


def cmd_status(a) -> int:
    home = Path(a.home)
    pol = load_policy(home)
    f = home / "ACTIVATION.json"
    ok, why = verify_activation(json.loads(f.read_text()) if f.exists() else None, _secret(home, "activation_secret"), pol)
    print(f"wallet:      {pol.wallet}")
    print(f"policy hash: {pol.policy_hash()[:16]}")
    print(f"activation:  {'ACTIVE' if ok else 'inactive'} ({why})")
    print(f"halted:      {(home / 'HALT').exists()}")
    from tradebot.execution.submission import submission_enabled
    print(f"build allows mainnet submission: {submission_enabled()}")
    return 0


def cmd_activate(a) -> int:
    home = Path(a.home)
    if not sys.stdin.isatty():
        print("REFUSED: activation must be typed by the owner in an interactive terminal")
        return 2
    pol = load_policy(home)
    if not 0 < a.hours <= MAX_ACTIVATION_HOURS:
        print(f"REFUSED: --hours must be in (0, {MAX_ACTIVATION_HOURS:g}]")
        return 2
    print(pol.model_dump_json(indent=2))
    phrase = f"ACTIVATE {redact(pol.wallet)} FOR {a.hours:g} HOURS"
    typed = input(f"\nThis lets the signer sign real transactions within the policy above.\nType exactly: {phrase}\n> ")
    if typed.strip() != phrase:
        print("REFUSED: confirmation did not match")
        return 2
    doc = create_activation(_secret(home, "activation_secret"), pol, a.hours, note=a.note)
    _write_private(home / "ACTIVATION.json", json.dumps(doc, indent=2))
    print(f"activated until {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(doc['record']['expires_ts']))}")
    return 0


def cmd_deactivate(a) -> int:
    f = Path(a.home) / "ACTIVATION.json"
    if f.exists():
        f.unlink()
    print("deactivated")
    return 0


def cmd_halt(a) -> int:
    _write_private(Path(a.home) / "HALT", json.dumps({"ts": time.time(), "reason": a.reason}))
    print("signer halted: every signing request is refused until `resume`")
    return 0


def cmd_resume(a) -> int:
    f = Path(a.home) / "HALT"
    if f.exists():
        f.unlink()
    print("signer halt removed (activation, policy and the build switch still apply)")
    return 0


def cmd_serve(a) -> int:
    from .core import SignerCore
    from .server import serve
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    home = Path(a.home)
    core = SignerCore(home, load_policy(home), _secret(home, "activation_secret"))
    srv = serve(core, _secret(home, "client_secret"), port=a.port)
    print(f"signer listening on 127.0.0.1:{a.port} for wallet {redact(core.policy.wallet)}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tradebot_signer")
    sub = ap.add_subparsers(required=True)
    p = sub.add_parser("init"); p.add_argument("--home", required=True); p.add_argument("--keypair", required=True)
    p.add_argument("--rpc-url", default="https://api.mainnet-beta.solana.com"); p.set_defaults(fn=cmd_init)
    p = sub.add_parser("status"); p.add_argument("--home", required=True); p.set_defaults(fn=cmd_status)
    p = sub.add_parser("activate"); p.add_argument("--home", required=True); p.add_argument("--hours", type=float, required=True)
    p.add_argument("--note", default=""); p.set_defaults(fn=cmd_activate)
    p = sub.add_parser("deactivate"); p.add_argument("--home", required=True); p.set_defaults(fn=cmd_deactivate)
    p = sub.add_parser("halt"); p.add_argument("--home", required=True); p.add_argument("--reason", default="manual"); p.set_defaults(fn=cmd_halt)
    p = sub.add_parser("resume"); p.add_argument("--home", required=True); p.set_defaults(fn=cmd_resume)
    p = sub.add_parser("serve"); p.add_argument("--home", required=True); p.add_argument("--port", type=int, default=8787); p.set_defaults(fn=cmd_serve)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
