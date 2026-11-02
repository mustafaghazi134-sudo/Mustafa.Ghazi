"""Jupiter Swap API v2 live-execution adapter (Meta-Aggregator: GET /order -> sign -> POST /execute).

Implements the same `Broker` interface as `PaperBroker`, so the engine, the RiskEngine approval and the intent
ledger are unchanged. Two modes:

  dry_run=True  (default): quote, build with `taker`, verify, simulate. Never asks for a signature, never calls
                /execute, never returns a fill. Used by `tradebot live-dry-run` and preflight.
  dry_run=False: additionally, behind every gate below, obtains a signature from the separate signer service and
                submits through /execute. It cannot run in this build: `submission.MAINNET_SUBMISSION_ENABLED` is
                False, which is checked first.

Order of gates for a live order (each one fails closed, all before anything is signed):
  1. RiskEngine approval (single-use HMAC) and kill/daily-halt re-check (Broker.execute);
  2. build switch (submission.py), LiveGate (mode=live + env + armed file whose hash covers [risk] and [live]);
  3. signer health: same wallet, activated by the owner, not halted, build switch on;
  4. LiveControls: live halt, serial execution, per-order/daily caps, wallet exposure, daily loss, SOL reserve;
  5. fresh /order with taker, `maxSupportedTransactionVersion=0`, unverifiable routers excluded;
  6. tx_verify: order fields (mints, amounts, our own minimum output from the reference price, fees, impact, router,
     expiry) and every top-level instruction (programs, signers, transfers, approvals, account creation);
  7. simulation on our RPC: the wallet's real balance changes are within the order;
  8. the signer repeats 3 and 5-7 on its own, with its own caps, and signs at most once per order id.

After signing, the signature and signed bytes are persisted (presubmit hook) BEFORE /execute. Every outcome after
that point that is not a chain-confirmed fill raises SubmittedUnknownError: reconciliation decides from chain
evidence (confirmed / failed / not found with an expired blockhash), never from silence or elapsed time, and no
second transaction is ever signed for the same order. Resubmission re-sends the identical signed bytes.
"""
from __future__ import annotations

import base64
import json
import logging
import random
import time
from dataclasses import dataclass, field

import httpx
from solders.message import to_bytes_versioned
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from ..config import Settings, jupiter_endpoints
from ..models import Fill, OrderRequest, Side
from ..risk.engine import RiskEngine
from . import tx_verify as V
from .base import Broker, ExecutionError, Quote, SubmittedUnknownError
from .live_controls import LiveControls, WalletSnapshot
from .live_gate import LiveGate
from .signer_protocol import SignerClient, SignerUnavailable, SignRefused
from .simulation import SimResult, alt_resolver_from_rpc, check_deltas, simulate_deltas
from .submission import DISABLED_REASON, submission_enabled

log = logging.getLogger(__name__)

ALL_ROUTERS = ("metis", "jupiterz", "dflow", "okx")


@dataclass
class Prepared:
    order: dict
    expectation: V.OrderExpectation
    report: V.TxReport
    sim: SimResult | None
    block_height: int
    reference_price: float
    in_decimals: int
    out_decimals: int
    timings_ms: dict = field(default_factory=dict)

    def summary(self) -> dict:
        o, lvbh = self.order, V._int(self.order.get("lastValidBlockHeight"))
        return {"router": o.get("router"), "mode": o.get("mode"), "transactionVersion": o.get("transactionVersion"),
                "requestId": o.get("requestId"), "inAmount": o.get("inAmount"), "outAmount": o.get("outAmount"),
                "otherAmountThreshold": o.get("otherAmountThreshold"), "our_min_out": self.expectation.min_out_raw,
                "feeBps": o.get("feeBps"), "feeMint": o.get("feeMint"), "priceImpact_bps": V.price_impact_bps(o),
                "gasless": o.get("gasless"), "lastValidBlockHeight": lvbh,
                "blocks_remaining": (lvbh - self.block_height) if lvbh else None, "expireAt": o.get("expireAt"),
                "signers": self.report.signers, "programs": sorted(set(self.report.programs)),
                "wallet_sol_cost_lamports": self.report.wallet_sol_cost_lamports,
                "priority_fee_lamports": self.report.priority_fee_lamports,
                "simulation": None if self.sim is None else {"err": self.sim.err, "units": self.sim.units,
                                                             "native_delta_lamports": self.sim.native_delta_lamports,
                                                             "token_deltas": self.sim.token_deltas},
                "reference_price": self.reference_price, "timings_ms": self.timings_ms}


class JupiterExecutionAdapter(Broker):
    def __init__(self, risk: RiskEngine, settings: Settings, provider, *, gate: LiveGate | None = None,
                 controls: LiveControls | None = None, signer: SignerClient | None = None, dry_run: bool = True,
                 http: httpx.Client | None = None, rpc=None, clock=time.time, sleep=time.sleep):
        super().__init__(risk)
        self.s, self.cfg = settings, settings.live
        self.provider = provider
        self.gate, self.controls, self.signer = gate, controls, signer
        self.dry_run = dry_run
        self.kind = "dry_run" if dry_run else "live"
        self.clock, self.sleep = clock, sleep
        try:
            Pubkey.from_string(self.cfg.wallet_pubkey)
        except Exception as e:  # noqa: BLE001
            raise ExecutionError("[live] wallet_pubkey must be the bot wallet's public key") from e
        self.wallet = self.cfg.wallet_pubkey
        self.jup = jupiter_endpoints(settings.data, settings.execution)
        if not self.jup.has_key and http is None:
            raise ExecutionError("JUPITER_API_KEY is required for the Swap API (developers.jup.ag)")
        self.http = http or httpx.Client(timeout=settings.data.jupiter_quote_timeout_sec, headers=self.jup.headers)
        self.exec_http = http or httpx.Client(timeout=settings.data.jupiter_execute_timeout_sec, headers=self.jup.headers)
        self._rpc_http = httpx.Client(timeout=settings.data.request_timeout_sec) if rpc is None else None
        self.rpc = rpc or self._rpc
        self.alts = alt_resolver_from_rpc(self.rpc)
        mints = getattr(provider, "mints", None) or settings.universe.mints
        self.mints: dict[str, str] = dict(mints)
        self.decimals: dict[str, int] = dict(getattr(provider, "decimals", None) or settings.universe.decimals)
        self.usdc = self.mints[settings.universe.quote_symbol]
        self.allowed_mints = frozenset({self.usdc, *(self.mints[s] for s in self.cfg.allowed_symbols if s in self.mints)})
        self.order_policy = V.OrderPolicy(allowed_routers=frozenset(self.cfg.allowed_routers) & V.VERIFIABLE_ROUTERS,
                                          max_fee_bps=self.cfg.max_fee_bps, max_price_impact_bps=self.cfg.max_price_impact_bps,
                                          max_slippage_bps=self.cfg.max_slippage_bps,
                                          max_priority_fee_lamports=self.cfg.max_priority_fee_lamports,
                                          max_rent_lamports=self.cfg.max_rent_lamports, min_blocks_remaining=self.cfg.min_blocks_remaining,
                                          min_rfq_seconds_remaining=self.cfg.min_rfq_seconds_remaining,
                                          max_quote_age_sec=self.cfg.max_quote_age_sec)
        self.tx_policy = V.TxPolicy(allowed_mints=self.allowed_mints, max_priority_fee_lamports=self.cfg.max_priority_fee_lamports,
                                    max_external_sol_outflow_lamports=self.cfg.max_external_sol_outflow_lamports,
                                    max_rent_lamports=self.cfg.max_rent_lamports)

    # ------------------------------------------------------------------ plumbing
    def _rpc(self, method: str, params: list):
        r = self._rpc_http.post(self.s.execution.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise ExecutionError(f"rpc {method}: {data['error']}")
        return data["result"]

    def _get_order(self, params: dict) -> dict:
        last = None
        for attempt in range(3):
            try:
                r = self.http.get(f"{self.jup.swap}/order", params=params)
            except httpx.HTTPError as e:
                last = e
            else:
                if r.status_code == 429 or r.status_code >= 500:
                    last = ExecutionError(f"/order HTTP {r.status_code}")
                    ra = float(r.headers.get("Retry-After") or 0)
                    self.sleep(min(max(ra, 2 ** attempt) * random.uniform(0.5, 1.5), 10.0))
                    continue
                if r.status_code >= 400:
                    raise ExecutionError(f"/order HTTP {r.status_code}: {r.text[:200]}")
                return r.json()
            self.sleep(min(2 ** attempt * random.uniform(0.5, 1.5), 10.0))
        raise ExecutionError(f"/order failed: {last}")

    def _pair(self, symbol: str, side: Side, amount: float) -> tuple[str, str, int, int, int]:
        """(input mint, output mint, raw input, input decimals, output decimals)."""
        mint, dec = self.mints.get(symbol), self.decimals.get(symbol)
        if not mint or dec is None or mint not in self.allowed_mints:
            raise ExecutionError(f"{symbol} is not an allowed live symbol")
        if side == Side.BUY:
            return self.usdc, mint, int(round(amount * 1e6)), 6, dec
        return mint, self.usdc, int(round(amount * 10 ** dec)), dec, 6

    def order_params(self, in_mint: str, out_mint: str, raw_in: int, taker: str | None) -> dict:
        excluded = sorted(set(ALL_ROUTERS) - set(self.order_policy.allowed_routers))
        p = {"inputMint": in_mint, "outputMint": out_mint, "amount": str(raw_in), "swapMode": "ExactIn",
             "slippageBps": self.cfg.max_slippage_bps, "maxSupportedTransactionVersion": "0"}
        if excluded:
            p["excludeRouters"] = ",".join(excluded)
        if taker:
            p["taker"] = taker
        return p

    def reference_price(self, symbol: str) -> float:
        try:
            t = self.provider.get_tickers([symbol]).get(symbol)
        except Exception as e:  # noqa: BLE001 - no reference, no order
            raise ExecutionError(f"reference price for {symbol} unavailable: {e}") from e
        if not t or t.price <= 0:
            raise ExecutionError(f"no reference price for {symbol}")
        if self.clock() - t.ts > self.cfg.max_reference_age_sec:
            raise ExecutionError(f"reference price for {symbol} is {self.clock() - t.ts:.0f}s old")
        return t.price

    def min_out_raw(self, side: Side, amount: float, ref: float, out_dec: int) -> int:
        keep = 1 - self.cfg.max_total_cost_bps / 1e4
        value = amount / ref if side == Side.BUY else amount * ref
        return int(value * keep * 10 ** out_dec)

    # ------------------------------------------------------------------ Broker: quote (read-only, no taker)
    def quote(self, symbol: str, side: Side, amount: float) -> Quote:
        in_mint, out_mint, raw_in, _, out_dec = self._pair(symbol, side, amount)
        ref = self.reference_price(symbol)
        o = self._get_order(self.order_params(in_mint, out_mint, raw_in, None))
        out = V._int(o.get("outAmount"))
        if not out:
            raise ExecutionError(f"no quote: {o.get('error') or o.get('errorMessage')}")
        out_ui = out / 10 ** out_dec
        if side == Side.BUY:
            price = amount / out_ui
            cost_bps = (price / ref - 1) * 1e4
        else:
            price = out_ui / amount
            cost_bps = (1 - price / ref) * 1e4
        fee_usd = (amount if side == Side.BUY else out_ui) * float(o.get("feeBps") or 0) / 1e4
        return Quote(symbol, side, price, cost_bps, fee_usd, amount, out_ui, raw={"router": o.get("router")})

    # ------------------------------------------------------------------ prepare (shared by dry-run and live)
    def prepare(self, symbol: str, side: Side, amount: float) -> Prepared:
        in_mint, out_mint, raw_in, in_dec, out_dec = self._pair(symbol, side, amount)
        ref = self.reference_price(symbol)
        t0 = self.clock()
        order = self._get_order(self.order_params(in_mint, out_mint, raw_in, self.wallet))
        received = self.clock()
        exp = V.OrderExpectation(self.wallet, in_mint, out_mint, raw_in, self.min_out_raw(side, amount, ref, out_dec), received)
        try:
            bh = int(self.rpc("getBlockHeight", [{"commitment": "confirmed"}]))
        except Exception as e:  # noqa: BLE001
            raise ExecutionError(f"block height unavailable: {e}") from e
        rep = V.verify_all(order, exp, self.order_policy, self.tx_policy, bh, self.alts, now=self.clock())
        sim = None
        if self.cfg.require_simulation:
            try:
                sim = simulate_deltas(self.rpc, order["transaction"], self.wallet, sorted(self.allowed_mints))
            except Exception as e:  # noqa: BLE001
                raise V.VerificationError([f"simulation unavailable: {type(e).__name__}: {e}"]) from e
            sp = check_deltas(sim, in_mint, out_mint, raw_in, exp.min_out_raw, self.cfg.max_sol_cost_lamports)
            if sp:
                raise V.VerificationError(sp)
        return Prepared(order, exp, rep, sim, bh, ref, in_dec, out_dec,
                        {"order": round((received - t0) * 1000, 1), "verify_simulate": round((self.clock() - received) * 1000, 1)})

    def dry_run_order(self, symbol: str, side: Side, amount: float) -> dict:
        """Everything up to (not including) signing. Never signs, never submits, never fills."""
        out = {"symbol": symbol, "side": side.value, "amount": amount, "wallet": self.wallet, "signed": False, "submitted": False}
        try:
            prep = self.prepare(symbol, side, amount)
        except V.VerificationError as e:
            out.update(ok=False, problems=e.problems)
            return out
        except ExecutionError as e:
            out.update(ok=False, problems=[str(e)])
            return out
        out.update(ok=True, problems=[], **prep.summary())
        if self.controls is not None:
            try:
                out["live_controls_would_block"] = self.controls.check(side, symbol, amount if side == Side.BUY else amount * prep.reference_price,
                                                                       self.wallet_snapshot(), sell_qty=amount if side == Side.SELL else 0.0)
            except Exception as e:  # noqa: BLE001
                out["live_controls_would_block"] = [f"could not evaluate: {e}"]
        out["submission_enabled_in_build"] = submission_enabled()
        return out

    # ------------------------------------------------------------------ gates
    def assert_submission_allowed(self, order: OrderRequest, notional_usd: float, sell_qty: float) -> None:
        if not submission_enabled():
            raise ExecutionError(DISABLED_REASON)
        if self.dry_run:
            raise ExecutionError("dry-run adapter: execution is disabled")
        if self.gate is None or self.controls is None or self.signer is None:
            raise ExecutionError("live adapter is missing its gate, controls or signer")
        self.gate.assert_armed()
        try:
            h = self.signer.health()
        except Exception as e:  # noqa: BLE001
            raise ExecutionError(f"signer unavailable: {e}") from e
        if h.get("wallet") != self.wallet:
            raise ExecutionError("signer serves a different wallet")
        if not h.get("active") or h.get("halted") or not h.get("submission_enabled_in_build"):
            raise ExecutionError(f"signer not ready: active={h.get('active')} ({h.get('activation')}) halted={h.get('halted')}")
        problems = self.controls.check(order.side, order.symbol, notional_usd, self.wallet_snapshot(), sell_qty=sell_qty,
                                       order_id=order.id)
        if problems:
            raise ExecutionError("live controls: " + "; ".join(problems))

    # ------------------------------------------------------------------ Broker: execute
    def _execute(self, order_req: OrderRequest, qty: float, q: Quote) -> Fill:
        amount = q.in_amount
        notional = amount if order_req.side == Side.BUY else qty * q.price
        self.assert_submission_allowed(order_req, notional, qty if order_req.side == Side.SELL else 0.0)
        try:
            prep = self.prepare(order_req.symbol, order_req.side, amount)
        except V.VerificationError as e:
            raise ExecutionError("verification failed before signing: " + "; ".join(e.problems)) from e
        o, exp = prep.order, prep.expectation
        claims = {"input_mint": exp.input_mint, "output_mint": exp.output_mint, "in_amount_raw": exp.in_amount_raw,
                  "min_out_raw": exp.min_out_raw, "request_id": o["requestId"], "router": o.get("router"),
                  "maker": o.get("maker"), "gasless": bool(o.get("gasless")), "last_valid_block_height": o.get("lastValidBlockHeight")}
        try:
            signed = self.signer.sign(order_req.id, o["transaction"], claims)
        except SignRefused as e:
            raise ExecutionError(f"signer refused: {e}") from e
        except SignerUnavailable as e:
            # Possibly signed but certainly not submitted (only this process submits, and it has nothing to send).
            raise ExecutionError(f"signer did not answer; nothing was submitted: {e}") from e
        signed_tx, sig = signed["signed_tx"], signed["signature"]
        self._check_signed(o["transaction"], signed_tx, sig, prep.report.wallet_signer_index)
        if self.presubmit_hook:      # durable BEFORE the network: a crash after this line is reconciled from chain evidence
            self.presubmit_hook({"request_id": o["requestId"], "tx_signature": sig, "signed_tx": signed_tx,
                                 "quote": json.dumps({"lastValidBlockHeight": o.get("lastValidBlockHeight"), "router": o.get("router"),
                                                      "input_mint": exp.input_mint, "output_mint": exp.output_mint,
                                                      "in_amount_raw": exp.in_amount_raw, "min_out_raw": exp.min_out_raw,
                                                      "otherAmountThreshold": o.get("otherAmountThreshold"),
                                                      "reference_price": prep.reference_price})})
        try:
            res = self._submit(signed_tx, sig, o["requestId"], V._int(o.get("lastValidBlockHeight")))
            if res.get("status") != "Success":
                raise SubmittedUnknownError(f"/execute status={res.get('status')} code={res.get('code')} {res.get('error') or ''}".strip(),
                                            sig, o["requestId"])
            if res.get("signature") and res["signature"] != sig:
                self.controls.halt(f"/execute reported signature {res['signature']} for our {sig}")
                raise SubmittedUnknownError("signature mismatch in /execute response", sig, o["requestId"])
            status = self._await_confirmation(sig)
            if status != "confirmed":
                raise SubmittedUnknownError(f"/execute Success but chain status is {status}", sig, o["requestId"])
            fill = self.fill_from_chain(order_req.id, order_req.symbol, order_req.side, sig, reference_price=prep.reference_price)
        except SubmittedUnknownError:
            raise
        except Exception as e:  # noqa: BLE001 - after submission nothing is ever reported as a plain failure
            raise SubmittedUnknownError(f"outcome unknown after submission: {type(e).__name__}: {e}", sig, o["requestId"]) from e
        floor = V._int(o.get("otherAmountThreshold")) or 0
        got_raw = round((fill.qty if order_req.side == Side.BUY else fill.notional_usd) * 10 ** prep.out_decimals)
        if got_raw + 1 < floor:
            self.controls.halt(f"{order_req.id}: chain output {got_raw} below the order minimum {floor}")
        log.warning("LIVE FILL %s %s qty=%.8g px=%.6g notional=$%.2f fee=$%.4f router=%s sig=%s", fill.side.value.upper(), fill.symbol,
                    fill.qty, fill.price, fill.notional_usd, fill.fee_usd, o.get("router"), sig)
        return fill

    def _check_signed(self, unsigned_b64: str, signed_b64: str, sig: str, idx: int | None) -> None:
        a = VersionedTransaction.from_bytes(base64.b64decode(unsigned_b64))
        b = VersionedTransaction.from_bytes(base64.b64decode(signed_b64))
        if to_bytes_versioned(a.message) != to_bytes_versioned(b.message):
            raise ExecutionError("signer returned a different message than the one verified; not submitted")
        if idx is None or str(b.signatures[idx]) != sig or not b.verify_with_results()[idx]:
            raise ExecutionError("signer returned an invalid signature; not submitted")

    def _submit(self, signed_tx: str, sig: str, request_id: str, lvbh: int | None) -> dict:
        """POST /execute, re-sending the IDENTICAL signed bytes on transport errors, 429 and 5xx until the timeout
        or the blockhash's last valid height. Never builds or signs anything new."""
        body = {"signedTransaction": signed_tx, "requestId": request_id}
        if lvbh:
            body["lastValidBlockHeight"] = str(lvbh)
        start, attempt, last = self.clock(), 0, "no attempt"
        while self.clock() - start < self.cfg.execute_timeout_sec:
            try:
                r = self.exec_http.post(f"{self.jup.swap}/execute", json=body)
                if r.status_code == 200:
                    return r.json()
                if r.status_code != 429 and r.status_code < 500:
                    try:
                        data = r.json()
                    except ValueError:
                        data = {}
                    return {"status": "Failed", "code": data.get("code"), "error": f"HTTP {r.status_code} {data.get('error') or r.text[:120]}"}
                last = f"HTTP {r.status_code}"
            except httpx.HTTPError as e:
                last = type(e).__name__
            attempt += 1
            if lvbh:
                try:
                    if int(self.rpc("getBlockHeight", [{"commitment": "confirmed"}])) > lvbh:
                        break
                except Exception:  # noqa: BLE001
                    pass
            self.sleep(min(0.5 * 2 ** attempt * random.uniform(0.5, 1.5), 5.0))
        raise SubmittedUnknownError(f"/execute gave no answer ({last}) after {attempt} attempt(s)", sig, request_id)

    def _await_confirmation(self, sig: str) -> str:
        start = self.clock()
        while True:
            st = self.lookup_signature(sig)
            if st in ("confirmed", "failed"):
                return st
            if self.clock() - start > self.cfg.confirm_timeout_sec:
                return st
            self.sleep(1.0)

    # ------------------------------------------------------------------ chain evidence
    def lookup_signature(self, tx_signature: str) -> str:
        res = self.rpc("getSignatureStatuses", [[tx_signature], {"searchTransactionHistory": True}])
        st = (res.get("value") or [None])[0]
        if not st:
            return "not_found"
        if st.get("err"):
            return "failed"
        return "confirmed" if st.get("confirmationStatus") in ("confirmed", "finalized") else "processing"

    def transaction_status(self, tx_signature: str, signed_tx_b64: str | None) -> dict:
        """The blockhash is checked at FINALIZED commitment first and the signature looked up afterwards, so a
        transaction that landed before its blockhash expired is visible by the time we would call it expired.
        RPC errors propagate: reconciliation then leaves the intent pending."""
        valid = None
        if signed_tx_b64:
            tx = VersionedTransaction.from_bytes(base64.b64decode(signed_tx_b64))
            res = self.rpc("isBlockhashValid", [str(tx.message.recent_blockhash), {"commitment": "finalized"}])
            valid = bool(res.get("value")) if isinstance(res, dict) else bool(res)
        status = self.lookup_signature(tx_signature)
        return {"status": status, "blockhash_valid": valid if status == "not_found" else None}

    def fill_from_chain(self, order_id: str, symbol: str, side: Side, sig: str, reference_price: float | None = None) -> Fill:
        tx = self.rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}])
        if not tx or not tx.get("meta"):
            raise ExecutionError(f"transaction {sig} not available from RPC yet")
        meta = tx["meta"]
        if meta.get("err"):
            raise ExecutionError(f"transaction {sig} failed on chain: {meta['err']}")
        keys = [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]
        widx = keys.index(self.wallet) if self.wallet in keys else None
        deltas: dict[str, int] = {}
        for sign, rows in ((-1, meta.get("preTokenBalances") or []), (1, meta.get("postTokenBalances") or [])):
            for b in rows:
                if b.get("owner") == self.wallet:
                    deltas[b["mint"]] = deltas.get(b["mint"], 0) + sign * int(b["uiTokenAmount"]["amount"])
        fee = int(meta.get("fee") or 0) if widx == 0 else 0
        lamport_delta = (meta["postBalances"][widx] - meta["preBalances"][widx]) if widx is not None else 0
        native = lamport_delta + deltas.pop(V.WSOL_MINT, 0)
        mint, dec = self.mints[symbol], self.decimals[symbol]
        sol_px = reference_price if symbol == "SOL" and reference_price else self._sol_price()
        if mint == V.WSOL_MINT:
            token_delta = native + fee          # the swap's SOL leg, network fee excluded
            sol_cost = fee
        else:
            token_delta = deltas.get(mint, 0)
            sol_cost = -lamport_delta           # fee + any rent deposited for new token accounts
        usdc_delta = deltas.get(self.usdc, 0)
        if side == Side.BUY:
            qty, usd = token_delta / 10 ** dec, -usdc_delta / 1e6
        else:
            qty, usd = -token_delta / 10 ** dec, usdc_delta / 1e6
        if qty <= 0 or usd <= 0:
            raise ExecutionError(f"could not derive a fill for {sig} from wallet balance changes")
        price = usd / qty
        slip = 0.0
        if reference_price:
            slip = ((price / reference_price - 1) if side == Side.BUY else (1 - price / reference_price)) * 1e4
        return Fill(order_id=order_id, symbol=symbol, side=side, qty=qty, price=price, notional_usd=usd,
                    fee_usd=max(sol_cost, 0) / 1e9 * sol_px, ts=self.clock(), tx_signature=sig, slippage_bps=slip)

    def _sol_price(self) -> float:
        try:
            return self.reference_price("SOL")
        except Exception:  # noqa: BLE001
            return 0.0

    def chain_fill(self, row) -> Fill | None:
        """Used by reconciliation for a confirmed intent: the fill as the chain recorded it."""
        return self.fill_from_chain(row["order_id"], row["symbol"], Side(row["side"]), row["tx_signature"])

    def signature_for_order(self, order_id: str) -> dict | None:
        """What the signer signed for this order id (for intents that crashed before the signature was persisted)."""
        if self.signer is None:
            return None
        return self.signer.lookup(order_id)

    # ------------------------------------------------------------------ balances
    def get_balances(self) -> dict[str, float]:
        by_mint = {m: s for s, m in self.mints.items()}
        out = {"SOL": self.rpc("getBalance", [self.wallet, {"commitment": "confirmed"}])["value"] / 1e9}
        for prog in (V.TOKEN_PROGRAM, V.TOKEN_2022_PROGRAM):
            res = self.rpc("getTokenAccountsByOwner", [self.wallet, {"programId": prog}, {"encoding": "jsonParsed", "commitment": "confirmed"}])
            for acc in res.get("value", []):
                info = acc["account"]["data"]["parsed"]["info"]
                sym = by_mint.get(info["mint"])
                if sym and sym != "SOL":
                    out[sym] = out.get(sym, 0.0) + int(info["tokenAmount"]["amount"]) / 10 ** self.decimals.get(sym, 6)
        return out

    def wallet_snapshot(self) -> WalletSnapshot:
        bal = self.get_balances()
        lamports = int(round(bal.pop("SOL", 0.0) * 1e9))
        prices = {}
        for sym in set(bal) | {"SOL"}:
            if sym == self.s.universe.quote_symbol:
                continue
            try:
                prices[sym] = self.reference_price(sym)
            except ExecutionError:
                prices[sym] = 0.0
                if bal.get(sym, 0.0) > 0 or (sym == "SOL" and lamports > 0):
                    raise
        return WalletSnapshot(lamports=lamports, holdings=bal, prices=prices, quote_symbol=self.s.universe.quote_symbol)
