"""Adversarial integration tests for the Jupiter live-execution adapter, run through the real bot order path
(_execute_with_intent -> RiskEngine approval -> adapter -> signer -> /execute -> chain) with a mocked Jupiter API,
an in-memory chain and an in-process signer holding an EPHEMERAL key. No network, no real wallet, no real key.

Most tests patch the build switch ON inside the test process only, to exercise the gated code; the first test pins
that the shipped build has it OFF.
"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import httpx
import pytest
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from jupiter_fixtures import FakeChain, build_tx, order_response, sys_transfer, swap_ix, cu_limit
from gate_helpers import arm_for_test
from tradebot.config import Settings
from tradebot.engine.bot import TradingBot
from tradebot.execution import submission
from tradebot.execution import tx_verify as V
from tradebot.execution.base import ExecutionAdapter
from tradebot.execution.jupiter_adapter import JupiterExecutionAdapter
from tradebot.execution.live_controls import LiveControls, WalletSnapshot
from tradebot.execution.live_gate import ENV_LIVE, ENV_LIVE_VALUE, LiveGate
from tradebot.execution.paper import PaperBroker
from tradebot.execution.signer_protocol import SignerUnavailable
from tradebot.models import OrderRequest, Side, Ticker
from tradebot.portfolio.accounting import PortfolioBook
from tradebot.portfolio.ledger import Ledger
from tradebot.research.claude_analyst import ClaudeAnalyst
from tradebot.risk.engine import MarketState, RiskEngine
from tradebot.risk.kill_switch import KillSwitch
from tradebot.strategies.registry import build_strategy
from tradebot_signer.core import SignerCore
from tradebot_signer.policy import SignerPolicy, create_activation

PRICE = 160.0
T0 = 1_800_000_000.0
SECRET = bytes(range(32))


class Prov:
    is_replay = False

    def __init__(self, s: Settings, clock):
        self.mints, self.decimals, self.clock = dict(s.universe.mints), dict(s.universe.decimals), clock

    def get_tickers(self, symbols):
        return {x: Ticker(x, PRICE if x == "SOL" else 100_000.0, self.clock(), 1e9, 1e9) for x in symbols}


class LocalSigner:
    """SignerClient stand-in that calls a real SignerCore in-process; can be made unreachable or malicious."""

    def __init__(self, core):
        self.core, self.down, self.tamper, self.sign_calls = core, False, None, 0

    def health(self):
        if self.down:
            raise SignerUnavailable("down")
        return self.core.health()

    def sign(self, coid, tx, claims):
        if self.down:
            raise SignerUnavailable("down")
        self.sign_calls += 1
        res = self.core.sign(coid, tx, claims)
        return self.tamper(res) if self.tamper else res

    def lookup(self, coid):
        if self.down:
            raise SignerUnavailable("down")
        return self.core.lookup(coid)


class Jupiter:
    """Mock Swap v2 API. `execute` is a list of behaviours consumed per POST /execute call."""

    def __init__(self, chain: FakeChain, wallet: str):
        self.chain, self.wallet = chain, wallet
        self.order_over: dict = {}
        self.tx_builder = lambda: build_tx(wallet, out_mint=V.WSOL_MINT)
        self.execute: list[str] = ["success"]
        self.exec_bodies: list[dict] = []
        self.taker_orders = 0
        self.on_execute = None
        self.landed_out = 31_150_000

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/order"):
            q = dict(request.url.params)
            assert q.get("maxSupportedTransactionVersion") == "0" and "dflow" in q.get("excludeRouters", "")
            out = int(int(q["amount"]) / 1e6 / PRICE * 1e9 * 0.9984)           # 5 USDC -> 31,200,000 lamports
            if "taker" in q:
                self.taker_orders += 1
                o = order_response(self.wallet, in_amount=int(q["amount"]), out_amount=out, min_out=int(out * 0.995),
                                   lvbh=self.chain.block_height + 150, tx=self.tx_builder())
            else:
                o = order_response(self.wallet, in_amount=int(q["amount"]), out_amount=out, transaction=None, taker=None)
            o.update(self.order_over)
            return httpx.Response(200, json=o)
        if request.url.path.endswith("/execute"):
            body = json.loads(request.content)
            self.exec_bodies.append(body)
            if self.on_execute:
                self.on_execute(body)
            beh = self.execute.pop(0) if len(self.execute) > 1 else self.execute[0]
            sig = str(VersionedTransaction.from_bytes(base64.b64decode(body["signedTransaction"])).signatures[0])
            if beh == "timeout":
                raise httpx.ReadTimeout("timed out")
            if beh == "500":
                return httpx.Response(503, json={"error": "unavailable"})
            land = beh in ("success", "failed_but_landed", "success_low")
            if land or beh == "success_chain_err":
                out = 30_000_000 if beh == "success_low" else self.landed_out
                self.chain.land(sig, in_mint=V.USDC_MINT, in_raw=5_000_000, out_mint=V.WSOL_MINT, out_raw=out,
                                err={"InstructionError": [3, {"Custom": 1}]} if beh == "success_chain_err" else None)
            if beh.startswith("success"):
                return httpx.Response(200, json={"status": "Success", "signature": sig, "code": 0})
            return httpx.Response(200, json={"status": "Failed", "signature": sig, "code": -1005, "error": "expired before landing"})
        return httpx.Response(404)


@pytest.fixture
def live(tmp_path, monkeypatch):
    now = [T0]
    clock = lambda: now[0]  # noqa: E731
    kp = Keypair()
    w = str(kp.pubkey())
    s = Settings(mode="live", data_dir=str(tmp_path / "data"), initial_capital_usd=1000.0)
    s.claude.enabled = False
    s.data.jupiter_api_key = "test-key"
    s.risk.min_liquidity_usd = 1000; s.risk.min_volume_24h_usd = 1000
    s.universe.symbols = ["SOL"]
    s.live.wallet_pubkey = w
    s.data_path.mkdir(parents=True, exist_ok=True)
    chain = FakeChain(w, [V.USDC_MINT, V.WSOL_MINT])
    chain.lamports[w] = 50_000_000                       # 0.05 SOL for fees ($8 of exposure)
    chain.set_token(V.USDC_MINT, 100_000_000)
    chain.swap_effect = {V.USDC_MINT: -5_000_000, V.WSOL_MINT: 31_155_000}
    jup = Jupiter(chain, w)
    pol = SignerPolicy(wallet=w)
    home = tmp_path / "signer"; home.mkdir()
    core = SignerCore(home, pol, SECRET, keypair=kp, rpc=chain.rpc, clock=clock)
    (home / "ACTIVATION.json").write_text(json.dumps(create_activation(SECRET, pol, 1, now=T0)))
    signer = LocalSigner(core)
    ledger = Ledger(s.db_path, mode="live")
    kill = KillSwitch(s.kill_file)
    risk = RiskEngine(s.risk, kill, clock=clock)
    monkeypatch.setenv(ENV_LIVE, ENV_LIVE_VALUE)
    gate = arm_for_test(s, ledger)
    controls = LiveControls(s.live, ledger, kill, s.live_halt_file, clock=clock)
    prov = Prov(s, clock)
    adapter = JupiterExecutionAdapter(risk, s, prov, gate=gate, controls=controls, signer=signer, dry_run=False,
                                      http=httpx.Client(transport=httpx.MockTransport(jup.handler)), rpc=chain.rpc,
                                      clock=clock, sleep=lambda x: now.__setitem__(0, now[0] + x))
    book = PortfolioBook(ledger, 1000.0, clock)
    bot = TradingBot(s, prov, ledger, risk, kill, adapter, book, [build_strategy("momentum")], ClaudeAnalyst(s.claude), clock)
    return {"s": s, "w": w, "kp": kp, "chain": chain, "jup": jup, "core": core, "signer": signer, "ledger": ledger, "bot": bot,
            "adapter": adapter, "controls": controls, "now": now, "home": home, "gate": gate}


@pytest.fixture
def switch_on(monkeypatch):
    monkeypatch.setattr(submission, "MAINNET_SUBMISSION_ENABLED", True)


def buy(env, notional=5.0):
    bot = env["bot"]
    t = bot.provider.get_tickers(["SOL"])["SOL"]
    order = OrderRequest(symbol="SOL", side=Side.BUY, notional_usd=notional, strategy="t")
    order.ts = bot.clock()
    d = bot.risk.evaluate(order, bot.book.risk_state({"SOL": t.price}), MarketState(t.price, t.liquidity_usd, t.volume_24h_usd, t.ts))
    assert d.approved, d.reasons
    ok = bot._execute_with_intent(order, d.adjusted_qty, d)
    return ok, order, env["ledger"].get_intent(env["ledger"].intents(1)[0]["id"])


def signed_count(env) -> int:
    return env["core"].db.execute("SELECT COUNT(*) c FROM signatures").fetchone()["c"]


# ====================================================================== the shipped build
def test_submission_is_disabled_in_this_build(live):
    """Pinned. Changing this requires the owner's explicit written authorisation (docs/LIVE_EXECUTION_ADAPTER.md)."""
    assert submission.MAINNET_SUBMISSION_ENABLED is False
    ok, order, row = buy(live)                          # everything else armed, activated and funded
    assert ok is False and row["state"] == "failed" and "disabled in this build" in row["result"]
    assert live["jup"].taker_orders == 0 and live["jup"].exec_bodies == [] and live["signer"].sign_calls == 0 and signed_count(live) == 0
    assert not live["bot"].book.positions


def test_interface_is_shared_by_paper_and_live(live):
    paper = PaperBroker(live["bot"].risk, live["bot"].provider)
    assert isinstance(paper, ExecutionAdapter) and isinstance(live["adapter"], ExecutionAdapter)
    assert (paper.kind, live["adapter"].kind) == ("paper", "live")


def test_dry_run_builds_verifies_and_simulates_but_never_signs_or_submits(live):
    a = live["adapter"]
    a.dry_run = True
    rep = a.dry_run_order("SOL", Side.BUY, 5.0)
    assert rep["ok"] is True and rep["signed"] is False and rep["submitted"] is False and rep["transactionVersion"] == 0
    assert rep["simulation"]["token_deltas"][V.USDC_MINT] == -5_000_000 and rep["live_controls_would_block"] == []
    live["jup"].tx_builder = lambda: build_tx(live["w"], instructions=[cu_limit(200_000), swap_ix(live["w"]),
                                                                       sys_transfer(live["w"], str(Keypair().pubkey()), 10**8)])
    bad = a.dry_run_order("SOL", Side.BUY, 5.0)
    assert bad["ok"] is False and any("third parties" in p for p in bad["problems"])
    assert live["jup"].exec_bodies == [] and live["signer"].sign_calls == 0
    with pytest.raises(Exception, match="disabled"):
        a._execute(OrderRequest(symbol="SOL", side=Side.BUY, notional_usd=5.0), 0.03, a.quote("SOL", Side.BUY, 5.0))


# ====================================================================== gates (switch patched on)
@pytest.mark.parametrize("breaker, needle", [
    (lambda e: e["gate"].disarm(), "not armed"),
    (lambda e: (e["home"] / "ACTIVATION.json").unlink(), "signer not ready"),
    (lambda e: (e["home"] / "HALT").write_text("{}"), "signer not ready"),
    (lambda e: setattr(e["signer"], "down", True), "signer unavailable"),
    (lambda e: e["controls"].halt("test", source="test"), "live halt"),
    (lambda e: e["chain"].lamports.__setitem__(e["w"], 200_000_000), "wallet exposure"),
    (lambda e: e["chain"].lamports.__setitem__(e["w"], 1_000_000), "SOL reserve"),
    # The activation is bound to the configuration: editing it AFTER arming blocks the next order, with no re-arm in between.
    (lambda e: setattr(e["s"].risk, "max_position_pct", 11.0), "configuration changed"),
    (lambda e: setattr(e["s"].live, "max_order_usd", 11.0), "configuration changed"),
    (lambda e: setattr(e["s"].execution, "execute_max_retries", 9), "configuration changed"),
    (lambda e: e["s"].strategies[0].params.update(lookback_days=31), "configuration changed"),
    (lambda e: setattr(e["s"].universe, "symbols", e["s"].universe.symbols + ["JUP"]), "configuration changed"),
    (lambda e: setattr(e["s"].engine, "min_paper_trades_before_live", 1), "configuration changed"),
    (lambda e: setattr(e["s"].engine, "paper_gap_tolerance_intervals", 20.0), "configuration changed"),
    (lambda e: setattr(e["s"].engine, "max_single_outage_hours", 24.0), "configuration changed"),
    (lambda e: setattr(e["gate"], "clock", lambda: time.time() + 13 * 3600), "expired"),
])
def test_every_gate_blocks_before_anything_is_signed(live, switch_on, breaker, needle):
    breaker(live)
    ok, _, row = buy(live)
    assert ok is False and row["state"] == "failed" and needle in row["result"]
    assert live["signer"].sign_calls == 0 and live["jup"].exec_bodies == [] and signed_count(live) == 0


def test_per_order_cap_and_serial_execution(live, switch_on):
    ok, _, row = buy(live, notional=12.0)
    assert ok is False and "max_order_usd" in row["result"]
    other = OrderRequest(symbol="SOL", side=Side.BUY, notional_usd=5.0)
    live["ledger"].create_intent(other, 0.03, 5.0, ts=T0)          # an unresolved intent anywhere blocks every live order
    live["bot"].ledger.update_intent(live["ledger"].intents(1)[0]["id"], state="submitted", tx_signature="X" * 64)
    from tradebot.execution.live_controls import LiveControls as LC
    snap = WalletSnapshot(lamports=50_000_000, holdings={"USDC": 100.0}, prices={"SOL": PRICE})
    assert any("unresolved intent" in p for p in LC(live["s"].live, live["ledger"], KillSwitch(live["s"].kill_file), live["s"].live_halt_file,
                                                     clock=lambda: T0).check(Side.SELL, "SOL", 5.0, snap))


# ====================================================================== outcomes
def test_happy_path_persists_before_submitting_and_books_chain_amounts(live, switch_on):
    seen = []
    live["jup"].on_execute = lambda body: seen.append(live["ledger"].pending_intents()[0])
    ok, order, row = buy(live)
    assert ok is True and row["state"] == "confirmed"
    assert seen and seen[0]["state"] == "signed" and seen[0]["tx_signature"] and seen[0]["signed_tx"]   # durable BEFORE /execute
    pos = live["bot"].book.positions["SOL"]
    assert pos.qty == pytest.approx(0.03115)                         # what the chain recorded, not the quote
    fill = live["ledger"].fill_for_order(order.id)
    assert fill["notional_usd"] == pytest.approx(5.0) and fill["tx_signature"] == row["tx_signature"]
    assert signed_count(live) == 1 and len(live["jup"].exec_bodies) == 1
    assert "lastValidBlockHeight" in json.loads(row["quote"])


def test_timeouts_resend_identical_bytes_then_chain_decides_and_nothing_is_resigned(live, switch_on):
    live["jup"].execute = ["timeout"]
    ok, order, row = buy(live)
    assert ok is False and row["state"] == "submitted" and row["tx_signature"]
    bodies = live["jup"].exec_bodies
    assert len(bodies) >= 2 and all(b == bodies[0] for b in bodies)     # same signed transaction + requestId each time
    assert signed_count(live) == 1 and live["signer"].sign_calls == 1
    bot = live["bot"]
    assert bot.reconcile_pending()["pending"] == 1                       # not found, blockhash still valid: UNKNOWN
    live["chain"].down.add("isBlockhashValid")
    assert bot.reconcile_pending()["pending"] == 1                       # RPC trouble: still UNKNOWN, never "failed"
    live["chain"].down.clear()
    live["chain"].blockhash_valid = False
    assert bot.reconcile_pending()["expired"] == 1                       # not found AND blockhash expired: can never land
    assert not bot.book.positions and signed_count(live) == 1
    ok2, _, row2 = buy(live)                                             # a NEW order may now proceed
    assert row2["id"] != row["id"]


def test_failed_response_but_landed_is_booked_once_from_chain(live, switch_on):
    live["jup"].execute = ["failed_but_landed"]
    ok, order, row = buy(live)
    assert ok is False and row["state"] == "submitted" and "-1005" in row["result"]
    bot = live["bot"]
    assert bot.reconcile_pending()["confirmed"] == 1
    assert bot.book.positions["SOL"].qty == pytest.approx(0.03115)
    assert bot.reconcile_pending() == {"confirmed": 0, "failed": 0, "expired": 0, "abandoned": 0, "pending": 0}
    assert live["ledger"]._exec("SELECT COUNT(*) c FROM fills").fetchone()["c"] == 1


def test_success_response_but_failed_on_chain_books_nothing(live, switch_on):
    live["jup"].execute = ["success_chain_err"]
    ok, _, row = buy(live)
    assert ok is False and row["state"] == "submitted"
    assert live["bot"].reconcile_pending()["failed"] == 1 and not live["bot"].book.positions


def test_server_errors_are_retried_with_the_same_bytes(live, switch_on):
    live["jup"].execute = ["500", "500", "success"]
    ok, _, row = buy(live)
    assert ok is True and len(live["jup"].exec_bodies) == 3 and len({json.dumps(b) for b in live["jup"].exec_bodies}) == 1
    assert signed_count(live) == 1


def test_fill_below_the_order_minimum_is_booked_from_chain_and_halts(live, switch_on):
    live["jup"].execute = ["success_low"]
    ok, _, _ = buy(live)
    assert ok is True and live["bot"].book.positions["SOL"].qty == pytest.approx(0.03)
    assert live["controls"].halted() and "below the order minimum" in live["controls"].halted()


def test_tampered_signer_response_is_never_submitted(live, switch_on):
    def swap_message(res):
        other = build_tx(live["w"], out_mint=V.WSOL_MINT)
        return dict(res, signed_tx=other)
    live["signer"].tamper = swap_message
    ok, _, row = buy(live)
    assert ok is False and row["state"] == "failed" and "different message" in row["result"]
    assert live["jup"].exec_bodies == []


def test_order_attack_at_execution_time_is_refused_before_signing(live, switch_on):
    live["jup"].order_over = {"otherAmountThreshold": "1000"}                # a route that would accept any price
    ok, _, row = buy(live)
    assert ok is False and row["state"] == "failed" and "below our floor" in row["result"]
    assert live["signer"].sign_calls == 0


# ====================================================================== crash recovery
def test_crash_after_signer_signed_but_before_persisting_is_recovered_from_the_signer_log(live, switch_on):
    led, bot = live["ledger"], live["bot"]
    order = OrderRequest(symbol="SOL", side=Side.BUY, notional_usd=5.0)
    iid = led.create_intent(order, 0.03, 5.0, ts=T0)                     # the process died right after the signer answered
    tx = build_tx(live["w"], out_mint=V.WSOL_MINT)
    claims = {"input_mint": V.USDC_MINT, "output_mint": V.WSOL_MINT, "in_amount_raw": 5_000_000, "min_out_raw": 30_000_000}
    res = live["core"].sign(order.id, tx, claims)
    live["signer"].down = True
    assert bot.reconcile_pending()["pending"] == 1 and led.get_intent(iid)["state"] == "created"   # signer unreachable: not abandoned
    live["signer"].down = False
    assert bot.reconcile_pending()["pending"] == 1                       # recovered signature; not on chain yet, blockhash valid
    row = led.get_intent(iid)
    assert row["state"] == "signed" and row["tx_signature"] == res["signature"]
    live["chain"].land(res["signature"], in_mint=V.USDC_MINT, in_raw=5_000_000, out_mint=V.WSOL_MINT, out_raw=31_150_000)
    assert bot.reconcile_pending()["confirmed"] == 1 and bot.book.positions["SOL"].qty == pytest.approx(0.03115)


def test_created_intent_with_nothing_signed_is_abandoned(live):
    led = live["ledger"]
    iid = led.create_intent(OrderRequest(symbol="SOL", side=Side.BUY, notional_usd=5.0), 0.03, 5.0, ts=T0)
    assert live["bot"].reconcile_pending()["abandoned"] == 1 and led.get_intent(iid)["state"] == "abandoned"


def test_confirmed_but_unreadable_fill_stays_pending(live, switch_on):
    live["jup"].execute = ["timeout"]
    ok, _, row = buy(live)
    live["chain"].statuses[row["tx_signature"]] = {"confirmationStatus": "confirmed", "err": None}   # landed, tx body unavailable
    assert live["bot"].reconcile_pending()["pending"] == 1 and not live["bot"].book.positions


# ====================================================================== controls
def test_kill_switch_blocks_live_buys_but_not_reduce_only_sells(live):
    Path(live["s"].kill_file).write_text("{}")
    snap = WalletSnapshot(lamports=50_000_000, holdings={"USDC": 100.0}, prices={"SOL": PRICE})
    assert any("kill switch" in p for p in live["controls"].check(Side.BUY, "SOL", 5.0, snap))
    assert live["controls"].check(Side.SELL, "SOL", 1.0, snap, sell_qty=0.001) == []


def test_daily_loss_halts_buys_and_writes_the_halt_file(live):
    c = live["controls"]
    snap = WalletSnapshot(lamports=50_000_000, holdings={"USDC": 100.0}, prices={"SOL": PRICE})
    assert c.check(Side.BUY, "SOL", 5.0, snap) == []
    worse = WalletSnapshot(lamports=50_000_000, holdings={"USDC": 94.0}, prices={"SOL": PRICE})   # -$6 since the day's first snapshot
    assert any("daily loss" in p for p in c.check(Side.BUY, "SOL", 5.0, worse))
    assert c.halted() and c.check(Side.SELL, "SOL", 1.0, worse)[0].startswith("live halt")


def test_daily_order_and_notional_caps(live):
    c, led = live["controls"], live["ledger"]
    for i in range(10):
        o = OrderRequest(symbol="SOL", side=Side.BUY, notional_usd=5.0)
        iid = led.create_intent(o, 0.03, 5.0, ts=T0)
        led.update_intent(iid, state="signed", tx_signature=f"S{i}")
        led.update_intent(iid, state="confirmed")
    snap = WalletSnapshot(lamports=50_000_000, holdings={"USDC": 100.0}, prices={"SOL": PRICE})
    p = " ".join(c.check(Side.BUY, "SOL", 5.0, snap))
    assert "orders today" in p and "daily notional" in p


def test_sol_sale_cannot_breach_the_fee_reserve(live):
    snap = WalletSnapshot(lamports=50_000_000, holdings={"USDC": 100.0}, prices={"SOL": PRICE})
    assert any("SOL reserve" in p for p in live["controls"].check(Side.SELL, "SOL", 5.0, snap, sell_qty=0.04))


# ====================================================================== static guarantees
def test_trading_process_never_loads_a_key_and_has_no_llm_in_the_execution_path():
    root = Path(__file__).resolve().parents[1]
    bot = (root / "tradebot/engine/bot.py").read_text()
    assert "load_keypair" not in bot and "JupiterLiveBroker" not in bot
    for f in ["tradebot/execution/jupiter_adapter.py", "tradebot/execution/tx_verify.py", "tradebot/execution/simulation.py",
              "tradebot/execution/live_controls.py", "tradebot/execution/signer_protocol.py", *map(str, (root / "tradebot_signer").glob("*.py"))]:
        text = (root / f).read_text() if not f.startswith("/") else Path(f).read_text()
        assert "anthropic" not in text.lower() and "claude_analyst" not in text
        if "tradebot_signer" not in f:
            assert "load_keypair" not in text and "Keypair(" not in text and "SOLANA_PRIVATE_KEY" not in text
