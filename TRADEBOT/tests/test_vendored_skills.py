"""Third-party Claude Code skills stay pinned, read-only and separate from the trading engine.

- every vendored file matches the SHA-256 recorded in .claude/skills.lock.json (the reviewed upstream commit);
- no vendored skill contains an order path, a credential read, a process launch or dynamic code;
- nothing in the `tradebot` package imports or shells out to a vendored skill;
- the project settings deny brokerage connectors and grant nothing.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / ".claude" / "skills.lock.json"
SKILLS = ROOT / ".claude" / "skills"


def _lock() -> dict:
    return json.loads(LOCK.read_text())


def test_vendored_files_match_reviewed_hashes():
    lock = _lock()
    assert lock["skills"], "lock file lists no skills"
    for name, entry in lock["skills"].items():
        assert re.fullmatch(r"[0-9a-f]{40}", entry["commit"]), "pin must be a full commit SHA"
        folder = SKILLS / name
        on_disk = sorted(p.name for p in folder.iterdir() if p.is_file())
        assert on_disk == sorted(entry["files"]), f"{name}: unexpected or missing files {on_disk}"
        for fname, digest in entry["files"].items():
            assert hashlib.sha256((folder / fname).read_bytes()).hexdigest() == digest, f"{name}/{fname} drifted from the reviewed version"
        assert entry["can_place_orders"] is False and entry["credentials"] == "none"


def test_only_reviewed_skills_are_installed():
    installed = sorted(p.name for p in SKILLS.iterdir() if p.is_dir())
    assert installed == sorted(_lock()["skills"]), f"skill folder not covered by the lock file: {installed}"
    for rejected in _lock()["rejected"]:
        assert not (SKILLS / rejected).exists(), f"rejected skill {rejected} is installed"


def _code_tokens(src: str) -> str:
    """Python source with comments and docstrings removed, so prose mentioning brokers does not count."""
    out = []
    for t in tokenize.generate_tokens(io.StringIO(src).readline):
        if t.type in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE):
            continue
        if t.type == tokenize.STRING and len(t.string) > 200:     # docstrings / long help text
            continue
        out.append(t.string)
    return " ".join(out)


def test_vendored_code_has_no_order_credential_or_exec_path():
    for py in SKILLS.rglob("*.py"):
        code = _code_tokens(py.read_text())
        for bad in ("subprocess", "os.system", "os.popen", "eval (", "exec (", "pickle", "ctypes", "socket",
                    "os.environ", "getenv", "place_stock_order", "place_crypto_order", "alpaca", "ALPACA",
                    "solders", "Keypair", "SOLANA_PRIVATE_KEY", "JUPITER_API_KEY", "swap/v2"):
            assert bad not in code, f"{py.name}: '{bad}' found in code"
        assert "data =" not in code.replace("data = json", ""), f"{py.name}: request bodies are not expected"
        # hosts named in code (comments excluded); github.com appears only inside the User-Agent string
        hosts = set(re.findall(r"https?://([a-zA-Z0-9.-]+)", code))
        assert hosts <= {"query1.finance.yahoo.com", "query2.finance.yahoo.com", "github.com"}, hosts


def test_tradebot_never_imports_or_runs_vendored_skills():
    for py in (ROOT / "tradebot").rglob("*.py"):
        src = py.read_text()
        for name in _lock()["skills"]:
            assert name not in src and name.replace("trader-", "") + ".py" not in src, f"{py} references {name}"
        assert ".claude/skills" not in src and ".claude\\\\skills" not in src


def test_project_settings_deny_brokers_and_allow_nothing():
    s = json.loads((ROOT / ".claude" / "settings.json").read_text())
    perms = s["permissions"]
    assert "allow" not in perms and "defaultMode" not in perms
    assert {"mcp__alpaca", "mcp__ibkr"} <= set(perms["deny"])
    assert "mcpServers" not in s and "enabledMcpjsonServers" not in s and "hooks" not in s
