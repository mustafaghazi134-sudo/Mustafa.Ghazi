"""Failure-injection tests for execution crash protection: persisted intents, reconciliation, duplicate prevention."""
from __future__ import annotations

import base64

import httpx
import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import Message
from solders.signature import Signature
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from tradebot.config import Settings
from tradebot.data.synthetic import SyntheticProvider
from tradebot.engine.bot import TradingBot, build_bot
from tradebot.execution.base import Broker, ExecutionError, Quote, SubmittedUnknownError
from tradebot.execution.jupiter_live import JupiterLiveBroker
from tradebot.execution.live_gate import LiveGate
from tradebot.execution.paper import PaperBroker
from tradebot.models import Fill, OrderRequest, Side
from tradebot.portfolio.accounting import PortfolioBook
from tradebot.portfolio.ledger import Ledger
from tradebot.research.claude_analyst import ClaudeAnalyst
from tradebot.risk.engine import MarketState, RiskEngine
from tradebot.risk.kill_switch import KillSwitch
from tradebot.strategies.registry import build_strategy


class CrashingBroker(Broker):
    """Programmable broker: can 'crash' after signing (unknown outcome) and answer signature lookups."""

    def __init__(self, risk, provider, mode="crash_after_sign", lookup="not_found"):
        super().__init__(risk)
        self.provider = provider
        self.mode = mode
        self.lookup = lookup
        self.executions = 0

    def quote(self, symbol, side, amount):
        px = self.provider.get_tickers([symbol])[symbol].price
        return Quote(symbol, side, px, 1.0, 0.01, amount, amount / px if side == Side.BUY else amount * px)

    def _execute(self, order, qty, q):
        self.executions += 1
        if self.mode == "fail_before_sign":           # fails before anything is signed or persisted
            raise ExecutionError("order error: no route")
        if self.presubmit_hook:
            self.presubmit_hook({"request_id": f"req-{order.id}", "tx_signature": f"SIG-{order.id}", "signed_tx": "AAAA"})
        if self.mode == "crash_after_sign":
            raise SubmittedUnknownError("timeout after broadcast", f"SIG-{order.id}", f"req-{order.id}")
        if self.mode == "plain_error_after_sign":     # a bug or unexpected error after the signature was persisted
            raise KeyError("unexpected response field")
        return Fill(order_id=order.id, symbol=order.symbol, side=order.side, qty=q.out_amount, price=q.price,
                    notional_usd=q.in_amount, fee_usd=q.fee_usd, tx_signature=f"SIG-{order.id}")

    def lookup_signature(self, tx_signature):
        return self.lookup

    def transaction_status(self, tx_signature, signed_tx_b64):
        return {"status": self.lookup, "blockhash_valid": getattr(self, "blockhash_valid", None)}

    def get_balances(self):
        return getattr(self, "balances", {})


def _settings(tmp_path):
    s = Settings(mode="paper", data_dir=str(tmp_path), initial_capital_usd=1000.0)
    s.claude.enabled = False
    s.data.provider = "synthetic"
    s.risk.min_liquidity_usd = 1000; s.risk.min_volume_24h_usd = 1000
    s.universe.symbols = ["SOL"]
    return s


def _bot(tmp_path, broker_factory, clock_start=1_700_000_000.0):
    s = _settings(tmp_path)
    prov = SyntheticProvider(["SOL"], seed=1, history=100, total_bars=300)
    clock = lambda: prov.now_ts  # noqa: E731
    ledger = Ledger(s.db_path, mode="paper")
    kill = KillSwitch(s.kill_file)
    risk = RiskEngine(s.risk, kill, clock=clock)
    broker = broker_factory(risk, prov)
    book = PortfolioBook(ledger, 1000.0, clock)
    bot = TradingBot(s, prov, ledger, risk, kill, broker, book, [build_strategy("momentum")], ClaudeAnalyst(s.claude), clock)
    return bot, ledger, broker, prov


def _decide(bot, prov, notional=50.0):
    t = prov.get_tickers(["SOL"])["SOL"]
    order = OrderRequest(symbol="SOL", side=Side.BUY, notional_usd=notional, strategy="t")
    order.ts = bot.clock()
    return order, bot.risk.evaluate(order, bot.book.risk_state({"SOL": t.price}), MarketState(t.price, t.liquidity_usd, t.volume_24h_usd, t.ts))


def _buy(bot, prov, notional=50.0):
    order, decision = _decide(bot, prov, notional)
    assert decision.approved, decision.reasons
    return bot._execute_with_intent(order, decision.adjusted_qty, decision), order


def test_crash_after_sign_then_confirmed_on_restart_books_exactly_once(tmp_path):
    bot, ledger, broker, prov = _bot(tmp_path, lambda r, p: CrashingBroker(r, p, "crash_after_sign"))
    ok, order = _buy(bot, prov)
    assert ok is False and not bot.book.positions
    row = ledger.pending_intents("SOL")[0]
    assert row["state"] == "submitted" and row["tx_signature"] == f"SIG-{order.id}" and row["request_id"] == f"req-{order.id}"
    assert ledger._exec("SELECT status FROM orders WHERE id=?", (order.id,)).fetchone()["status"] == "unknown"
    # restart: a new process finds the row, the chain says confirmed -> position booked once from the persisted intent
    bot2, ledger2, broker2, prov2 = _bot(tmp_path, lambda r, p: CrashingBroker(r, p, "ok", lookup="confirmed"))
    assert ledger2.pending_intents() == []
    assert "SOL" in bot2.book.positions and bot2.book.positions["SOL"].qty == pytest.approx(row["qty"])
    assert ledger2.get_intent(row["id"])["state"] == "confirmed"
    assert bot2.book.cash == pytest.approx(1000.0 - row["notional_usd"])
    bot2.reconcile_pending()                                   # idempotent: nothing booked twice
    assert bot2.book.positions["SOL"].qty == pytest.approx(row["qty"])


def test_not_found_stays_unknown_until_blockhash_expiry_is_proven(tmp_path):
    bot, ledger, broker, prov = _bot(tmp_path, lambda r, p: CrashingBroker(r, p, "crash_after_sign"))
    _buy(bot, prov)
    bot2, ledger2, broker2, prov2 = _bot(tmp_path, lambda r, p: CrashingBroker(r, p, "ok", lookup="not_found"))
    # evidence incomplete (blockhash validity unknown): stays UNKNOWN no matter how much time passes
    for _ in range(30):
        prov2.cursor += 1                                               # +30h of replay clock
    counts = bot2.reconcile_pending()
    assert counts["pending"] == 1 and counts["expired"] == 0 and ledger2.pending_intents() != [] and not bot2.book.positions
    assert "UNKNOWN" in ledger2.pending_intents()[0]["result"]
    broker2.blockhash_valid = True                                       # blockhash still valid: could still land -> UNKNOWN
    assert bot2.reconcile_pending()["pending"] == 1
    broker2.blockhash_valid = False                                      # proven: can never land -> expired, no position, cash intact
    counts = bot2.reconcile_pending()
    assert counts["expired"] == 1 and ledger2.pending_intents() == [] and not bot2.book.positions
    assert bot2.book.cash == pytest.approx(1000.0)


def test_unknown_intent_blocks_symbol_and_manual_resolution_is_logged(tmp_path):
    bot, ledger, broker, prov = _bot(tmp_path, lambda r, p: CrashingBroker(r, p, "crash_after_sign"))
    _buy(bot, prov)
    broker.mode = "ok"
    _, d2 = _decide(bot, prov)
    assert not d2.approved and "in flight" in d2.reasons[0]              # risk engine refuses while UNKNOWN
    iid = ledger.pending_intents("SOL")[0]["id"]
    with pytest.raises(ValueError):
        ledger.resolve_intent_manually(iid, "cancelled", "bad state")
    row = ledger.resolve_intent_manually(iid, "expired", "checked explorer: no such tx")
    assert row["state"] == "expired" and ledger.pending_intents() == []
    assert any(e["kind"] == "intent_manual_resolution" for e in ledger.events(5))
    ok, _ = _buy(bot, prov)
    assert ok is True                                                    # symbol unblocked after resolution


def test_pending_intent_blocks_duplicate_order_for_symbol(tmp_path):
    bot, ledger, broker, prov = _bot(tmp_path, lambda r, p: CrashingBroker(r, p, "crash_after_sign"))
    order2, early = _decide(bot, prov)            # approval minted BEFORE the first order is in flight
    _buy(bot, prov)
    broker.mode = "ok"
    # layer 1: the risk engine now refuses a second buy while the first is unresolved
    _, d = _decide(bot, prov)
    assert not d.approved and "in flight" in d.reasons[0]
    # layer 2: an approval minted earlier is still stopped by the intent guard before reaching the broker
    ok = bot._execute_with_intent(order2, early.adjusted_qty, early)
    assert ok is False and broker.executions == 1
    assert ledger._exec("SELECT status, detail FROM orders WHERE id=?", (order2.id,)).fetchone()["detail"].startswith("pending intent")


def test_failure_before_signing_is_abandoned_not_booked(tmp_path):
    bot, ledger, broker, prov = _bot(tmp_path, lambda r, p: CrashingBroker(r, p, "fail_before_sign"))
    ok, order = _buy(bot, prov)
    assert ok is False
    row = ledger.get_intent(ledger.intents(1)[0]["id"])
    assert row["state"] == "failed" and not bot.book.positions
    # a 'created' row with no signature (crash before signing) is abandoned on restart, never booked
    fresh = OrderRequest(symbol=order.symbol, side=order.side, notional_usd=order.notional_usd, strategy="t")  # new id: one intent per order id
    iid = ledger.create_intent(fresh, 1.0, 50.0, ts=bot.clock())
    bot.reconcile_pending()
    assert ledger.get_intent(iid)["state"] == "abandoned"


def test_balance_mismatch_at_startup_trips_kill_switch(tmp_path):
    bot, ledger, broker, prov = _bot(tmp_path, lambda r, p: CrashingBroker(r, p, "ok"))
    ok, _ = _buy(bot, prov)
    assert ok and "SOL" in bot.book.positions
    broker.balances = {"SOL": 0.0, "USDC": bot.book.cash}             # wallet shows no SOL
    problems = bot.reconcile_balances()
    assert problems and bot.kill.is_tripped() and "mismatch" in bot.kill.reason()
    bot.kill.reset()
    broker.balances = {"SOL": bot.book.positions["SOL"].qty, "USDC": bot.book.cash}
    assert bot.reconcile_balances() == [] and not bot.kill.is_tripped()


def test_balance_reconciliation_never_clears_a_manual_or_drawdown_kill(tmp_path):
    bot, ledger, broker, prov = _bot(tmp_path, lambda r, p: CrashingBroker(r, p, "ok"))
    _buy(bot, prov)
    broker.balances = {"SOL": bot.book.positions["SOL"].qty, "USDC": bot.book.cash}   # balances are perfect
    bot.kill.trip("manual kill", source="cli")
    assert bot.reconcile_balances() == [] and bot.kill.is_tripped() and bot.kill.reason() == "manual kill"
    bot.kill.reset()
    bot.kill.trip("drawdown 12% >= limit 10%", source="risk_engine")
    bot.reconcile_pending(); bot.reconcile_balances()
    assert bot.kill.is_tripped() and "drawdown" in bot.kill.reason()
    assert not bot._execute_with_intent.__self__.risk.evaluate(
        OrderRequest(symbol="SOL", side=Side.BUY, notional_usd=10), bot.book.risk_state({"SOL": 1.0}), MarketState(1.0, 1e9, 1e9, bot.clock())).approved


def test_paper_broker_intents_go_created_signed_confirmed(tmp_path):
    s = _settings(tmp_path)
    bot = build_bot(s)
    bot.run_forever(max_cycles=400)
    rows = bot.ledger.intents(1000)
    assert rows, "expected paper orders"
    assert {r["state"] for r in rows} == {"confirmed"} and all(r["tx_signature"].startswith("paper-") for r in rows)
    assert bot.ledger.pending_intents() == []


# ---------------- live broker specifics (mocked network) ----------------
def _unsigned_tx_b64(payer: Keypair) -> str:
    ix = transfer(TransferParams(from_pubkey=payer.pubkey(), to_pubkey=Keypair().pubkey(), lamports=1))
    msg = Message.new_with_blockhash([ix], payer.pubkey(), Hash.default())
    tx = VersionedTransaction.populate(msg, [Signature.default()])   # what Jupiter returns: unsigned
    return base64.b64encode(bytes(tx)).decode()


def _live(tmp_path, monkeypatch):
    s = Settings(mode="live", data_dir=str(tmp_path)); s.data.jupiter_api_key = "k"
    monkeypatch.setenv("TRADEBOT_LIVE_TRADING", "I_ACCEPT_THE_RISK")
    gate = LiveGate(s); gate.arm(s.engine.live_confirm_phrase, force=True)
    from tradebot.config import DataConfig, ExecutionConfig, UniverseConfig
    from tradebot.data.live import LiveProvider
    kp = Keypair()
    b = JupiterLiveBroker(RiskEngine(s.risk, KillSwitch(tmp_path / "KILL")), s, LiveProvider(DataConfig(jupiter_api_key="k"), UniverseConfig(), ExecutionConfig()), kp, gate)
    return s, b, kp


def test_live_sign_derives_signature_before_broadcast(tmp_path, monkeypatch):
    s, b, kp = _live(tmp_path, monkeypatch)
    signed_b64, sig = b._sign(_unsigned_tx_b64(kp))
    tx = VersionedTransaction.from_bytes(base64.b64decode(signed_b64))
    assert str(tx.signatures[0]) == sig and sig != str(Signature.default())


def test_live_timeout_resends_same_payload_and_reports_unknown_not_new_order(tmp_path, monkeypatch):
    s, b, kp = _live(tmp_path, monkeypatch)
    order = {"transaction": _unsigned_tx_b64(kp), "requestId": "req-9", "outAmount": "554000000", "priceImpactPct": "0.0001", "feeBps": 2}
    sent, hooks = [], []
    b.presubmit_hook = lambda info: hooks.append(info)

    class FakeClient:
        def request(self, method, url, **kw):
            sent.append(kw["json"]); raise httpx.ReadTimeout("timeout")

    b.exec_http = FakeClient()
    b._order = lambda *a, **k: pytest.fail("must not create a new order after a timeout")
    monkeypatch.setattr("tradebot.execution.jupiter_live.time.sleep", lambda *_: None)
    q = Quote("SOL", Side.BUY, 180.0, 1.0, 0.02, 100.0, 0.554, raw=order)
    with pytest.raises(SubmittedUnknownError) as ei:
        b._execute(OrderRequest(symbol="SOL", side=Side.BUY, notional_usd=100), 0.554, q)
    assert len(sent) == 3 and len({s_["signedTransaction"] for s_ in sent}) == 1 and all(s_["requestId"] == "req-9" for s_ in sent)
    assert hooks and hooks[0]["request_id"] == "req-9" and hooks[0]["tx_signature"] == ei.value.tx_signature
    assert sent[0]["signedTransaction"] == hooks[0]["signed_tx"]           # persisted payload == submitted payload


def test_unexpected_error_after_signing_is_never_recorded_as_failed(tmp_path):
    """A signature was persisted, so the transaction may be on chain: a plain exception is not evidence of failure."""
    bot, ledger, broker, prov = _bot(tmp_path, lambda r, p: CrashingBroker(r, p, "plain_error_after_sign", lookup="not_found"))
    ok, order = _buy(bot, prov)
    assert ok is False
    row = ledger.get_intent(ledger.intents(1)[0]["id"])
    assert row["state"] == "submitted" and row["tx_signature"] == f"SIG-{order.id}"
    broker.blockhash_valid = None                       # unknown: must stay pending
    bot.reconcile_pending()
    assert ledger.get_intent(row["id"])["state"] == "submitted"
    broker.lookup = "confirmed"                         # chain evidence decides
    bot.reconcile_pending()
    assert ledger.get_intent(row["id"])["state"] == "confirmed" and "SOL" in bot.book.positions


def test_unknown_outcome_without_signature_never_erases_the_persisted_one(tmp_path):
    class NoSigUnknown(CrashingBroker):
        def _execute(self, order, qty, q):
            self.presubmit_hook({"request_id": f"req-{order.id}", "tx_signature": f"SIG-{order.id}", "signed_tx": "AAAA"})
            raise SubmittedUnknownError("no answer", None, None)
    bot, ledger, broker, prov = _bot(tmp_path, lambda r, p: NoSigUnknown(r, p))
    ok, order = _buy(bot, prov)
    row = ledger.get_intent(ledger.intents(1)[0]["id"])
    assert row["state"] == "submitted" and row["tx_signature"] == f"SIG-{order.id}" and row["request_id"] == f"req-{order.id}"
