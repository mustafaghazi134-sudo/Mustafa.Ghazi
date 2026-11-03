"""The supervised execution test (tradebot exec-test): one buy, one sell straight back, chain reconciliation.

Mocked Jupiter API, in-memory chain that applies balance changes, in-process signer with an EPHEMERAL key.
No network, no real wallet, no real key. The build switch is patched ON only inside the tests that need it.
"""
from __future__ import annotations

import base64
import json

import httpx
import pytest
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from jupiter_fixtures import FakeChain, build_tx, order_response
from tradebot.config import Settings
from tradebot.engine.bot import TradingBot
from tradebot.execution import exec_test as X
from tradebot.execution import submission
from tradebot.execution import tx_verify as V
from tradebot.execution.jupiter_adapter import JupiterExecutionAdapter
from tradebot.execution.live_controls import LiveControls
from tradebot.execution.live_gate import ENV_LIVE, ENV_LIVE_VALUE, LiveGateError
from tradebot.models import Ticker
from tradebot.portfolio.accounting import PortfolioBook
from tradebot.portfolio.ledger import Ledger
from tradebot.research.claude_analyst import ClaudeAnalyst
from tradebot.risk.engine import RiskEngine
from tradebot.risk.kill_switch import KillSwitch
from tradebot_signer.core import SignerCore
from tradebot_signer.policy import SignerPolicy, create_activation

PRICE = 160.0
T0 = 1_800_000_000.0
SECRET = bytes(range(32))


class Prov:
    is_replay = False

    def __init__(self, s, clock):
        self.mints, self.decimals, self.clock = dict(s.universe.mints), dict(s.universe.decimals), clock

    def get_tickers(self, symbols):
        return {x: Ticker(x, PRICE, self.clock(), 1e9, 1e9) for x in symbols}


class Signer:
    def __init__(self, core):
        self.core, self.down, self.sign_calls = core, False, 0

    def health(self):
        if self.down:
            from tradebot.execution.signer_protocol import SignerUnavailable
            raise SignerUnavailable("down")
        return self.core.health()

    def sign(self, coid, tx, claims):
        self.sign_calls += 1
        return self.core.sign(coid, tx, claims)

    def lookup(self, coid):
        return self.core.lookup(coid)


class Chain(FakeChain):
    """FakeChain that also applies landed swaps to the wallet's balances."""

    def land(self, signature, *, err=None, in_mint, in_raw, out_mint, out_raw, fee=45_000):
        super().land(signature, err=err, in_mint=in_mint, in_raw=in_raw, out_mint=out_mint, out_raw=out_raw, fee=fee)
        if err:
            return
        self.lamports[self.wallet] -= fee
        for mint, d in ((in_mint, -in_raw), (out_mint, out_raw)):
            if mint == V.WSOL_MINT:
                self.lamports[self.wallet] += d
            else:
                ata = V.ata_address(self.wallet, mint)
                m, amt = self.tokens[ata]
                self.tokens[ata] = (m, amt + d)


class Jup:
    """Swap v2 mock for both directions; the price impact + fee is `haircut` per leg."""

    def __init__(self, chain, wallet, haircut=0.0012):
        self.chain, self.wallet, self.haircut = chain, wallet, haircut
        self.pending: dict[str, tuple] = {}
        self.exec_calls = 0

    def handler(self, request):
        if request.url.path.endswith("/order"):
            q = dict(request.url.params)
            ain, aout, raw = q["inputMint"], q["outputMint"], int(q["amount"])
            if ain == V.USDC_MINT:
                out = int(raw / 1e6 / PRICE * 1e9 * (1 - self.haircut))
                tx = build_tx(self.wallet, out_mint=V.WSOL_MINT)
            else:
                out = int(raw / 1e9 * PRICE * 1e6 * (1 - self.haircut))
                tx = build_tx(self.wallet, sol_in_lamports=raw, out_mint=V.USDC_MINT)
            if "taker" not in q:
                return httpx.Response(200, json=order_response(self.wallet, input_mint=ain, output_mint=aout, in_amount=raw,
                                                               out_amount=out, transaction=None, taker=None))
            o = order_response(self.wallet, input_mint=ain, output_mint=aout, in_amount=raw, out_amount=out,
                               min_out=int(out * 0.995), lvbh=self.chain.block_height + 150, tx=tx)
            self.chain.swap_effect = {ain: -raw, aout: out}
            self.pending[o["requestId"]] = (ain, raw, aout, out)
            return httpx.Response(200, json=o)
        if request.url.path.endswith("/execute"):
            self.exec_calls += 1
            body = json.loads(request.content)
            sig = str(VersionedTransaction.from_bytes(base64.b64decode(body["signedTransaction"])).signatures[0])
            ain, raw, aout, out = self.pending[body["requestId"]]
            self.chain.land(sig, in_mint=ain, in_raw=raw, out_mint=aout, out_raw=out)
            return httpx.Response(200, json={"status": "Success", "signature": sig, "code": 0})
        return httpx.Response(404)


@pytest.fixture
def env(tmp_path, monkeypatch):
    now = [T0]
    clock = lambda: now[0]  # noqa: E731
    kp = Keypair()
    w = str(kp.pubkey())
    s = Settings(mode="paper", data_dir=str(tmp_path / "data"), initial_capital_usd=1000.0)
    s.claude.enabled = False
    s.data.jupiter_api_key = "test-key"
    s.universe.symbols = ["SOL"]
    s.live.wallet_pubkey = w
    s.data_path.mkdir(parents=True, exist_ok=True)
    chain = Chain(w, [V.USDC_MINT, V.WSOL_MINT])
    chain.lamports[w] = 50_000_000                       # 0.05 SOL
    chain.set_token(V.USDC_MINT, 12_000_000)             # 12 USDC
    jup = Jup(chain, w)
    pol = SignerPolicy(wallet=w)
    home = tmp_path / "signer"; home.mkdir()
    core = SignerCore(home, pol, SECRET, keypair=kp, rpc=chain.rpc, clock=clock)
    (home / "ACTIVATION.json").write_text(json.dumps(create_activation(SECRET, pol, 1, now=T0)))
    signer = Signer(core)
    monkeypatch.setenv(ENV_LIVE, ENV_LIVE_VALUE)
    ledger = Ledger(s.data_path / "exec_test.sqlite", mode="live")
    kill = KillSwitch(s.kill_file)
    controls = LiveControls(s.live, ledger, kill, s.live_halt_file, clock=clock)
    prov = Prov(s, clock)

    def build(usd=10.0):
        risk = RiskEngine(X.exec_test_risk_limits(s, usd, 12.0), kill, clock=clock)
        gate = X.ExecTestGate(s, clock)
        adapter = JupiterExecutionAdapter(risk, s, prov, gate=gate, controls=controls, signer=signer, dry_run=False,
                                          http=httpx.Client(transport=httpx.MockTransport(jup.handler)), rpc=chain.rpc,
                                          clock=clock, sleep=lambda x: now.__setitem__(0, now[0] + x))
        bot = TradingBot(s, prov, ledger, risk, kill, adapter, PortfolioBook(ledger, 12.0, clock), [], ClaudeAnalyst(s.claude), clock)
        return adapter, bot

    def approve(usd=10.0, loss=1.0, bps=60.0, hours=1.0):
        return X.write_approval(s, "SOL", usd, loss, bps, hours, X.approval_phrase("SOL", usd, loss), clock=clock)

    def run(exit_only=False):
        adapter, bot = build()
        return X.run_exec_test(s, adapter, bot, controls, clock=clock, sleep=lambda x: now.__setitem__(0, now[0] + x), exit_only=exit_only)

    return {"s": s, "w": w, "chain": chain, "jup": jup, "signer": signer, "ledger": ledger, "controls": controls,
            "now": now, "approve": approve, "run": run, "home": home}


@pytest.fixture
def switch_on(monkeypatch):
    monkeypatch.setattr(submission, "MAINNET_SUBMISSION_ENABLED", True)


# ---------------------------------------------------------------------------------------------------- approval
def test_approval_requires_the_exact_phrase_and_limits_within_live_caps(env):
    s = env["s"]
    with pytest.raises(LiveGateError, match="confirmation must be exactly"):
        X.write_approval(s, "SOL", 10.0, 1.0, 60, 1.0, "yes")
    with pytest.raises(LiveGateError, match="amount must be"):
        X.write_approval(s, "SOL", 50.0, 1.0, 60, 1.0, X.approval_phrase("SOL", 50.0, 1.0))
    with pytest.raises(LiveGateError, match="max loss"):
        X.write_approval(s, "SOL", 10.0, 9.0, 60, 1.0, X.approval_phrase("SOL", 10.0, 9.0))
    with pytest.raises(LiveGateError, match="validity"):
        X.write_approval(s, "SOL", 10.0, 1.0, 60, 5.0, X.approval_phrase("SOL", 10.0, 1.0))
    assert not X.approval_path(s).exists()


def test_refused_without_approval_and_when_engine_is_armed_or_expired(env, switch_on):
    assert env["run"]()["result"] == "REFUSED"
    env["approve"]()
    env["s"].armed_file.write_text("{}")
    r = env["run"]()
    assert r["result"] == "REFUSED" and any("engine is armed" in p for p in r["problems"])
    env["s"].armed_file.unlink()
    env["now"][0] += 2 * 3600
    assert any("expired" in p for p in env["run"]()["problems"])
    assert env["signer"].sign_calls == 0 and env["jup"].exec_calls == 0


# ---------------------------------------------------------------------------------------------------- shipped build
def test_shipped_build_switch_blocks_the_test_before_signing(env):
    assert submission.MAINNET_SUBMISSION_ENABLED is False
    env["approve"]()
    r = env["run"]()
    assert r["result"] == "BUY_NOT_FILLED" and "disabled in this build" in (r["legs"][0]["result"] or "")
    assert env["signer"].sign_calls == 0 and env["jup"].exec_calls == 0
    assert X.load_approval(env["s"])["state"] == "done_no_fill"          # single use even when nothing filled
    assert env["run"]()["result"] == "REFUSED"


# ---------------------------------------------------------------------------------------------------- full round trip
def test_round_trip_buys_sells_back_reconciles_and_consumes_the_approval(env, switch_on):
    env["approve"]()
    usdc0, lam0 = env["chain"].tokens[V.ata_address(env["w"], V.USDC_MINT)][1], env["chain"].lamports[env["w"]]
    r = env["run"]()
    assert r["result"] == "COMPLETED", r["problems"]
    assert [leg["leg"] for leg in r["legs"]] == ["buy", "sell (attempt 1)"]
    assert all(leg["intent_state"] == "confirmed" and leg["tx_signature"] for leg in r["legs"])
    rec = r["reconciliation"]
    assert rec["usdc_match"] and rec["token_match"]
    assert -0.05 < rec["net_result_usd"] < 0                          # costs only: ~2 x 12 bps of $10
    assert env["signer"].sign_calls == 2 and env["jup"].exec_calls == 2
    assert env["chain"].tokens[V.ata_address(env["w"], V.USDC_MINT)][1] < usdc0
    assert abs(env["chain"].lamports[env["w"]] - lam0) < 200_000          # only network fees left the SOL balance
    assert not X.approval_path(env["s"]).exists()                         # consumed (renamed EXEC_TEST_USED_*)
    assert not env["s"].armed_file.exists()                               # the engine was never armed
    assert env["run"]()["result"] == "REFUSED"                            # single use


def test_cost_above_the_approved_limit_refuses_before_anything_is_signed(env, switch_on):
    env["jup"].haircut = 0.004                                            # 40 bps per leg -> ~80 bps round trip
    env["approve"](bps=60.0)
    r = env["run"]()
    assert r["result"] == "REFUSED" and any("exceeds the approved" in p for p in r["problems"])
    assert env["signer"].sign_calls == 0 and X.load_approval(env["s"])["state"] == "approved"


def test_failed_sell_leaves_a_recoverable_state_and_exit_only_completes(env, switch_on):
    env["approve"]()
    orig = env["signer"].sign

    def sign_once(coid, tx, claims):                                      # buy signs; sell attempts are refused
        if env["signer"].sign_calls >= 1:
            env["signer"].sign_calls += 1
            from tradebot.execution.signer_protocol import SignRefused
            raise SignRefused("test: refused")
        return orig(coid, tx, claims)
    env["signer"].sign = sign_once
    r = env["run"]()
    assert r["result"] == "POSITION_STILL_HELD" and X.load_approval(env["s"])["state"] == "bought"
    env["signer"].sign = orig
    r2 = env["run"](exit_only=True)
    assert r2["result"] in ("COMPLETED", "COMPLETED_WITH_MISMATCH") and r2["legs"][-1]["intent_state"] == "confirmed"
    assert not X.approval_path(env["s"]).exists()


def test_unverifiable_sell_route_refuses_before_buying(env, switch_on, monkeypatch):
    from tradebot.execution import tx_verify as V2
    env["approve"]()
    real = V2.verify_all

    def refuse_sol_input(order, exp, *a, **k):
        rep = real(order, exp, *a, **k)
        if exp.input_mint == V.WSOL_MINT:
            raise V2.VerificationError(["ix3: SyncNative on an account other than the wallet's wSOL account"])
        return rep
    monkeypatch.setattr("tradebot.execution.jupiter_adapter.V.verify_all", refuse_sol_input)
    r = env["run"]()
    assert r["result"] == "REFUSED" and any("sell route would be refused" in p for p in r["problems"])
    assert env["signer"].sign_calls == 0 and env["jup"].exec_calls == 0
    assert X.load_approval(env["s"])["state"] == "approved"          # nothing was bought; the approval is still unused
