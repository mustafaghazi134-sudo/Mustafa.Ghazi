"""The legacy JupiterLiveBroker must not sign or submit anything while MAINNET_SUBMISSION_ENABLED is False.

It holds a private key in-process and POSTs to /execute, so the only thing between a forged activation and a real swap
was the (forgeable) engine gate. These tests prove the central build lock now sits in front of every signing and
submitting entry point, before any key use, and that nothing in production code can reach the class.

No test here uses a real credential, a funded wallet or the network: the Keypair is generated in memory, every HTTP
client is replaced by a tripwire, and the build switch is only ever turned ON by the separate `legacy_switch_on` fixture
in tests that are NOT about the lock.
"""
import ast
import base64
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from gate_helpers import arm_for_test
from solders.keypair import Keypair

from tradebot.config import DataConfig, ExecutionConfig, Settings, UniverseConfig
from tradebot.data.live import LiveProvider
from tradebot.execution import jupiter_live
from tradebot.execution.base import ExecutionError, Quote
from tradebot.execution.jupiter_live import JupiterLiveBroker
from tradebot.execution.live_gate import ENV_LIVE, ENV_LIVE_VALUE, LiveGate
from tradebot.execution.submission import DISABLED_REASON, MAINNET_SUBMISSION_ENABLED, submission_enabled
from tradebot.models import OrderRequest, Side
from tradebot.risk.engine import RiskEngine
from tradebot.risk.kill_switch import KillSwitch

ROOT = Path(__file__).resolve().parents[1]
ORDER = {"transaction": base64.b64encode(b"\x01\x00" * 40).decode(), "requestId": "req-q", "outAmount": "554000000",
         "priceImpactPct": "0.0001", "feeBps": 2}


class ExplodingKey:
    """Stands in for the Keypair: ANY touch of the key fails the test."""

    def __getattr__(self, name):
        raise AssertionError(f"private key touched ({name}) while the build switch is off")


class Tripwire:
    def __init__(self):
        self.calls = []

    def __call__(self, *a, **k):
        self.calls.append((a, k))
        raise AssertionError(f"execution-related call attempted while the build switch is off: {a[:2]}")


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """A legacy broker in the WORST case: live mode, env set, activation forged (no verifier), real in-memory key."""
    assert submission_enabled() is False                         # the lock is closed for every test in this file
    monkeypatch.setenv(ENV_LIVE, ENV_LIVE_VALUE)
    s = Settings(mode="live", data_dir=str(tmp_path))
    s.data.jupiter_api_key = "k"
    gate = LiveGate(s)
    arm_for_test(s, gate=gate)                                   # unsigned, unaudited, readiness never checked = a forgery
    assert gate.is_armed()                                       # the forged activation DOES satisfy the engine gate
    broker = JupiterLiveBroker(RiskEngine(s.risk, KillSwitch(tmp_path / "KILL")), s,
                               LiveProvider(DataConfig(jupiter_api_key="k"), UniverseConfig(), ExecutionConfig()), Keypair(), gate)
    trip = Tripwire()
    monkeypatch.setattr(httpx.Client, "request", trip)
    monkeypatch.setattr(httpx.Client, "post", trip)
    monkeypatch.setattr(httpx.Client, "get", trip)
    monkeypatch.setattr(httpx.Client, "send", trip)
    hooks = []
    broker.presubmit_hook = hooks.append
    broker.kp = ExplodingKey()
    broker._order = Tripwire()
    monkeypatch.setattr(jupiter_live, "VersionedTransaction", Tripwire())    # signing / parsing a transaction would trip this
    return broker, s, trip, hooks


def _order_args():
    q = Quote("SOL", Side.BUY, 180.0, 1.0, 0.02, 100.0, 0.554, raw=dict(ORDER))
    return OrderRequest(symbol="SOL", side=Side.BUY, notional_usd=100), 0.554, q


def _assert_refused(exc, trip, hooks):
    assert isinstance(exc.value, ExecutionError) and str(exc.value) == DISABLED_REASON
    assert trip.calls == [] and hooks == []


# ---- the lock itself ------------------------------------------------------------------------------------------------------

def test_the_build_switch_is_still_off():
    assert MAINNET_SUBMISSION_ENABLED is False and submission_enabled() is False
    assert "MAINNET_SUBMISSION_ENABLED = False" in (ROOT / "tradebot" / "execution" / "submission.py").read_text()


def test_the_broker_reuses_the_central_guard_and_adds_no_second_switch():
    src = (ROOT / "tradebot" / "execution" / "jupiter_live.py").read_text()
    assert "from .submission import DISABLED_REASON, submission_enabled" in src
    calls = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "submission_enabled"]
    assert len(calls) == 1                                        # one call site: the shared guard method
    for forbidden in ("os.environ", "getenv", "ENABLE", "BYPASS", "allow_legacy", "unsafe"):
        assert forbidden not in src, forbidden
    assert not any(n in JupiterLiveBroker.__init__.__code__.co_varnames for n in ("force", "enabled", "override", "allow"))


# ---- switch off: no signing, no /execute, key never used --------------------------------------------------------------------

def test_execute_is_refused_before_key_signing_or_any_request(rig):
    broker, s, trip, hooks = rig
    with pytest.raises(ExecutionError) as e:
        broker._execute(*_order_args())
    _assert_refused(e, trip, hooks)


def test_sign_is_refused_before_the_transaction_is_parsed_or_the_key_touched(rig):
    broker, s, trip, hooks = rig
    with pytest.raises(ExecutionError) as e:
        broker._sign(ORDER["transaction"])
    _assert_refused(e, trip, hooks)


def test_post_to_execute_is_refused_at_the_request_layer(rig):
    broker, s, trip, hooks = rig
    for url in (f"{broker.jup.swap}/execute", "https://example.invalid/anything"):
        for method in ("POST", "post", "PUT", "DELETE", "PATCH"):
            with pytest.raises(ExecutionError) as e:
                broker._request(broker.exec_http, method, url, json={"signedTransaction": "x", "requestId": "r"})
            _assert_refused(e, trip, hooks)


def test_send_rpc_methods_are_refused_but_read_methods_are_not_blocked_by_the_lock(rig, monkeypatch):
    broker, s, trip, hooks = rig
    for m in ("sendTransaction", "sendRawTransaction", "SENDTRANSACTION"):
        with pytest.raises(ExecutionError) as e:
            broker._rpc(m, ["x"])
        _assert_refused(e, trip, hooks)
    # a read method passes the lock and reaches the (tripwired) network layer, proving the lock is not a blanket block
    with pytest.raises(AssertionError, match="execution-related call attempted"):
        broker._rpc("getBalance", [broker.pubkey])


def test_direct_python_api_execute_is_refused_before_the_risk_approval_is_consumed(rig):
    broker, s, trip, hooks = rig

    class SpyRisk:
        limits = broker.risk.limits

        def verify(self, *a, **k):
            raise AssertionError("risk approval consumed before the build lock was checked")

        guard_execution = verify

    broker.risk = SpyRisk()
    order, qty, _ = _order_args()
    with pytest.raises(ExecutionError) as e:
        broker.execute(order, qty, approval=object())
    _assert_refused(e, trip, hooks)


@pytest.mark.parametrize("entry", ["_execute", "_sign", "execute", "_request_post", "_rpc_send"])
@pytest.mark.parametrize("state", ["armed_forged", "unarmed", "env_missing", "paper_mode"])
def test_every_entry_point_is_refused_in_every_gate_state(rig, monkeypatch, entry, state):
    """The lock comes first, so the answer is the same whatever the engine gate says."""
    broker, s, trip, hooks = rig
    if state == "unarmed":
        broker.s.armed_file.unlink()
    elif state == "env_missing":
        monkeypatch.delenv(ENV_LIVE)
    elif state == "paper_mode":
        broker.s.mode = "paper"
    order, qty, q = _order_args()
    calls = {"_execute": lambda: broker._execute(order, qty, q), "_sign": lambda: broker._sign(ORDER["transaction"]),
             "execute": lambda: broker.execute(order, qty, object()),
             "_request_post": lambda: broker._request(broker.exec_http, "POST", f"{broker.jup.swap}/execute", json={}),
             "_rpc_send": lambda: broker._rpc("sendTransaction", [])}
    with pytest.raises(ExecutionError) as e:
        calls[entry]()
    _assert_refused(e, trip, hooks)


def test_forged_and_even_properly_signed_activations_do_not_matter(rig):
    """A forged record satisfies the engine gate (that is the known limitation); a valid signature would too.
    Neither can enable submission: only the build constant can."""
    import hashlib
    import hmac

    from tradebot.execution import attestation as A

    broker, s, trip, hooks = rig

    class Att:
        scheme = "test-hmac"

        def sign(self, payload):
            return hmac.new(b"k" * 32, payload, hashlib.sha256).hexdigest()

        def verify(self, payload, signature):
            return hmac.compare_digest(self.sign(payload), signature)

    gate = LiveGate(s, attestor=Att())
    s.armed_file.write_text(__import__("json").dumps(A.build_record(digest=gate.config_digest(), readiness={}, ttl_hours=12,
                                                                    now=gate.clock(), attestor=Att())))
    broker.gate = gate
    assert gate.is_armed() and gate.status()["authenticated"] is True
    with pytest.raises(ExecutionError) as e:
        broker._execute(*_order_args())
    _assert_refused(e, trip, hooks)


# ---- read-only behaviour is preserved --------------------------------------------------------------------------------------

def test_get_requests_pass_the_lock(rig):
    broker, s, trip, hooks = rig

    class Resp:
        status_code = 200
        headers = {}

        def json(self):
            return {"ok": True}

    class Client:
        def request(self, method, url, **kw):
            return Resp()

    assert broker._request(Client(), "GET", "https://example.invalid/read") == {"ok": True}
    assert broker._request(Client(), "get", "https://example.invalid/read") == {"ok": True}


# ---- with the (test-only) switch on, the existing gate is unchanged -----------------------------------------------------------

def test_lock_is_additive_the_engine_gate_still_applies_when_the_switch_is_on(rig, legacy_switch_on):
    broker, s, trip, hooks = rig
    broker.s.armed_file.unlink()
    from tradebot.execution.live_gate import LiveGateError
    with pytest.raises(LiveGateError):
        broker._execute(*_order_args())
    assert trip.calls == [] and hooks == []


# ---- paper engines and the Lab are unaffected -------------------------------------------------------------------------------

def test_paper_selftest_is_unaffected():
    from tradebot.selftest import run_selftest
    checks = run_selftest(cycles=120)
    bad = [c for c in checks if not c.ok]
    assert not bad, bad


@pytest.mark.parametrize("modules", [["tradebot.cli", "tradebot.engine.bot", "tradebot.selftest", "tradebot.execution.paper"],
                                     ["tradebot.fast.engine", "tradebot.fast.broker", "tradebot.scanner.scanner"],
                                     ["opportunity_lab.engine", "opportunity_lab.cli"]])
def test_paper_engines_and_lab_never_import_the_legacy_broker_or_a_wallet(modules):
    code = ("import sys;" + "".join(f"import {m};" for m in modules) +
            "bad=[m for m in ('tradebot.execution.jupiter_live','tradebot.execution.wallet','tradebot_signer') if m in sys.modules];"
            "print('LOADED:'+','.join(bad) if bad else 'CLEAN')")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert out.returncode == 0, out.stderr[-500:]
    assert out.stdout.strip().endswith("CLEAN"), out.stdout


# ---- static audit ---------------------------------------------------------------------------------------------------------

PRODUCTION_DIRS = ("tradebot", "tradebot_signer", "opportunity_lab", "research_lab", "deploy")
LEGACY = "jupiter_live"


def _production_files():
    for d in PRODUCTION_DIRS:
        for p in sorted((ROOT / d).rglob("*.py")):
            if p.name != "jupiter_live.py" and "__pycache__" not in p.parts:
                yield p


def _legacy_references(tree: ast.AST) -> list[str]:
    """Every way a module can import, name or dynamically load the legacy broker."""
    found = []
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""] + [a.name for a in node.names]
        if any(LEGACY in n for n in names):
            found.append(f"line {node.lineno}: imports {names}")
        if isinstance(node, ast.Name) and node.id == "JupiterLiveBroker":
            found.append(f"line {node.lineno}: references JupiterLiveBroker")
        if isinstance(node, ast.Attribute) and node.attr in ("JupiterLiveBroker", LEGACY):
            found.append(f"line {node.lineno}: attribute {node.attr}")
        if isinstance(node, ast.Call):
            f = node.func
            dyn = (isinstance(f, ast.Name) and f.id == "__import__") or (isinstance(f, ast.Attribute) and f.attr == "import_module")
            if dyn and any(isinstance(a, ast.Constant) and isinstance(a.value, str) and LEGACY in a.value for a in node.args):
                found.append(f"line {node.lineno}: dynamic import of the legacy module")
    return found


def test_no_production_file_imports_or_constructs_the_legacy_broker():
    offenders = [f"{p.relative_to(ROOT)}: {hit}" for p in _production_files() for hit in _legacy_references(ast.parse(p.read_text()))]
    assert offenders == [], "\n".join(offenders)


@pytest.mark.parametrize("planted", [
    "from tradebot.execution.jupiter_live import JupiterLiveBroker", "import tradebot.execution.jupiter_live",
    "from tradebot.execution import jupiter_live", "b = JupiterLiveBroker(1, 2, 3, 4, 5)",
    "import importlib\nimportlib.import_module('tradebot.execution.jupiter_live')", "__import__('tradebot.execution.jupiter_live')",
    "import tradebot.execution as e\ne.jupiter_live.JupiterLiveBroker"])
def test_the_audit_detects_every_planted_way_to_reach_the_legacy_broker(planted):
    assert _legacy_references(ast.parse(planted)), planted


def test_the_audit_ignores_innocent_code():
    assert _legacy_references(ast.parse("from tradebot.execution.jupiter_adapter import JupiterExecutionAdapter")) == []


def test_every_signing_or_submitting_method_starts_with_the_central_guard():
    tree = ast.parse((ROOT / "tradebot" / "execution" / "jupiter_live.py").read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "JupiterLiveBroker")
    methods = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}

    def first_stmt(fn):
        body = fn.body
        if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
            body = body[1:]                                   # skip the docstring
        return body[0]

    def calls_guard(node):
        return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "_require_submission_enabled"
                   for n in ast.walk(node))

    for name in ("execute", "_execute", "_sign", "_request", "_rpc"):
        assert name in methods, name
        assert calls_guard(first_stmt(methods[name])), f"{name}: first statement must be (or contain) the central guard"
    # the guard method itself is the only place that reads the switch
    g = methods["_require_submission_enabled"]
    assert "submission_enabled" in ast.dump(g) and "DISABLED_REASON" in ast.dump(g)


def test_no_other_method_can_post_sign_or_send():
    """Any method that posts, signs or sends must be one of the guarded ones (or the guarded `_request`/`_rpc`)."""
    tree = ast.parse((ROOT / "tradebot" / "execution" / "jupiter_live.py").read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "JupiterLiveBroker")
    risky = {}
    for fn in (n for n in cls.body if isinstance(n, ast.FunctionDef)):
        dump = ast.dump(fn)
        hits = [t for t in ("'POST'", "'post'", "VersionedTransaction(", "sendTransaction", "sign_message", "'/execute'")
                if t in dump or (t == "VersionedTransaction(" and "VersionedTransaction" in dump and "from_bytes" not in dump)]
        if "attr='post'" in dump:
            hits.append("rpc post")
        if hits:
            risky[fn.name] = hits
    allowed = {"_request", "_rpc", "_execute", "_sign", "transaction_status"}     # transaction_status only parses a tx (read-only)
    assert set(risky) <= allowed, risky


def test_no_production_code_loads_a_private_key_through_the_legacy_wallet_helper():
    offenders = []
    for p in _production_files():
        if p.name == "wallet.py":
            continue
        text = p.read_text()
        if "load_keypair(" in text and "tradebot_signer" not in p.parts:
            offenders.append(str(p.relative_to(ROOT)))
    assert offenders == []


def test_no_transaction_sending_rpc_exists_anywhere_in_production_code():
    for d in ("tradebot", "tradebot_signer"):
        for p in (ROOT / d).rglob("*.py"):
            assert not any(t in p.read_text() for t in ("sendTransaction", "sendRawTransaction", "send_raw_transaction")) \
                or p.name == "jupiter_live.py", p
