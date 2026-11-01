"""Live execution via the Jupiter Swap API v2 (official `integrating-jupiter` / `jupiter-swap-migration` skills).

Flow per order: LiveGate.assert_armed -> RiskEngine.verify -> GET /swap/v2/order?taker=<wallet> -> sign the
returned VersionedTransaction locally -> POST /swap/v2/execute {signedTransaction, requestId}. Jupiter submits
and confirms the transaction; the response carries the actual on-chain amounts (inputAmountResult /
outputAmountResult), which are used for the fill. Retryable execute codes trigger a fresh order (orders
expire after ~2 minutes). The RPC is used only for balance reconciliation.
"""
from __future__ import annotations

import base64
import logging
import random
import time

import httpx
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from ..config import Settings, jupiter_endpoints
from ..data.live import LiveProvider
from ..models import Fill, OrderRequest, Side
from ..risk.engine import RiskEngine
from .base import Broker, ExecutionError, Quote, SubmittedUnknownError
from .live_gate import LiveGate
from .wallet import redact

log = logging.getLogger(__name__)

RETRYABLE_EXECUTE_CODES = {-1, -1000, -1001, -1004, -2000, -2001, -2003, -2004}


class JupiterLiveBroker(Broker):
    def __init__(self, risk: RiskEngine, settings: Settings, provider: LiveProvider, keypair: Keypair, gate: LiveGate):
        super().__init__(risk)
        self.s = settings
        self.provider = provider
        self.kp = keypair
        self.gate = gate
        self.pubkey = str(keypair.pubkey())
        self.jup = jupiter_endpoints(settings.data, settings.execution)
        if not self.jup.has_key:
            raise ExecutionError("JUPITER_API_KEY is required for the Jupiter Swap API (developers.jup.ag)")  # skill: fail fast
        self.http = httpx.Client(timeout=settings.data.jupiter_quote_timeout_sec, headers=self.jup.headers)
        self.exec_http = httpx.Client(timeout=settings.data.jupiter_execute_timeout_sec, headers=self.jup.headers)
        self.rpc = httpx.Client(timeout=settings.data.request_timeout_sec)
        self.usdc_mint = settings.universe.mints["USDC"]
        log.warning("live broker (Swap API v2) initialised for wallet %s via %s", redact(self.pubkey), self.jup.swap)

    # ------------------------------------------------------------------ helpers
    def _mint_dec(self, symbol: str) -> tuple[str, int]:
        mint = self.provider.mint_for(symbol)
        if not mint:
            raise ExecutionError(f"unknown mint for {symbol}")
        return mint, self.provider.decimals.get(symbol, 6)

    def _pair(self, symbol: str, side: Side, amount: float) -> tuple[str, str, int, int]:
        """(input mint, output mint, raw input amount, output decimals). Amounts are raw integers (skill)."""
        mint, dec = self._mint_dec(symbol)
        if side == Side.BUY:
            return self.usdc_mint, mint, int(round(amount * 10 ** 6)), dec
        return mint, self.usdc_mint, int(round(amount * 10 ** dec)), 6

    def _request(self, client: httpx.Client, method: str, url: str, **kw) -> dict:
        """GET/POST with 429 backoff + jitter and typed errors, per the skill's cross-cutting pattern."""
        last = None
        for attempt in range(3):
            try:
                r = client.request(method, url, **kw)
            except httpx.HTTPError as e:
                last = e
                time.sleep(min(2 ** attempt + random.random() * 0.5, 10.0))
                continue
            if r.status_code == 429:
                retry_after = float(r.headers.get("Retry-After") or 0)
                time.sleep(min(max(retry_after, 2 ** attempt) + random.random() * 0.5, 10.0))
                last = ExecutionError("rate limited (429)")
                continue
            if r.status_code >= 400:
                raise ExecutionError(f"{method} {url} -> HTTP {r.status_code}: {r.text[:200]}")
            return r.json()
        raise ExecutionError(f"{method} {url} failed after retries: {last}")

    def _rpc(self, method: str, params: list) -> dict:
        r = self.rpc.post(self.s.execution.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise ExecutionError(f"rpc {method}: {data['error']}")
        return data["result"]

    def _sign(self, tx_b64: str) -> tuple[str, str]:
        """Returns (signed tx base64, transaction signature). The signature is known before broadcast."""
        tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
        signed = VersionedTransaction(tx.message, [self.kp])
        return base64.b64encode(bytes(signed)).decode(), str(signed.signatures[0])

    def transaction_status(self, tx_signature: str, signed_tx_b64: str | None) -> dict:
        """Signature status plus whether the transaction's blockhash is still valid. A not-found transaction whose
        blockhash has expired cannot land any more; one whose blockhash is still valid (or unknown) stays UNKNOWN."""
        status = self.lookup_signature(tx_signature)
        valid = None
        if status == "not_found" and signed_tx_b64:
            try:
                tx = VersionedTransaction.from_bytes(base64.b64decode(signed_tx_b64))
                bh = str(tx.message.recent_blockhash)
                res = self._rpc("isBlockhashValid", [bh, {"commitment": "confirmed"}])
                valid = bool(res.get("value")) if isinstance(res, dict) else bool(res)
            except Exception as e:  # noqa: BLE001 - unknown stays unknown
                log.warning("blockhash validity check failed for %s: %s", tx_signature, e)
                valid = None
        return {"status": status, "blockhash_valid": valid}

    def lookup_signature(self, tx_signature: str) -> str:
        res = self._rpc("getSignatureStatuses", [[tx_signature], {"searchTransactionHistory": True}])
        st = (res.get("value") or [None])[0]
        if not st:
            return "not_found"
        if st.get("err"):
            return "failed"
        return "confirmed" if st.get("confirmationStatus") in ("confirmed", "finalized") else "not_found"

    def order_params(self, in_mint: str, out_mint: str, raw_in: int, taker: str | None) -> dict:
        p = {"inputMint": in_mint, "outputMint": out_mint, "amount": str(raw_in), "slippageBps": self.s.execution.live_slippage_bps}
        if taker:
            p["taker"] = taker
        return p

    def _order(self, symbol: str, side: Side, amount: float) -> tuple[dict, int]:
        in_mint, out_mint, raw_in, out_dec = self._pair(symbol, side, amount)
        order = self._request(self.http, "GET", f"{self.jup.swap}/order", params=self.order_params(in_mint, out_mint, raw_in, self.pubkey))
        if order.get("error") or not order.get("transaction"):
            raise ExecutionError(f"order error: {order.get('error') or 'no transaction returned'}")
        return order, out_dec

    def _to_quote(self, symbol: str, side: Side, amount: float, order: dict, out_dec: int) -> Quote:
        out_raw = int(order["outAmount"])
        impact_bps = float(order.get("priceImpactPct") or 0.0) * 1e4
        fee_bps = float(order.get("feeBps") or 0.0)
        if side == Side.BUY:
            out_qty = out_raw / 10 ** out_dec
            price = amount / out_qty if out_qty else float("inf")
            return Quote(symbol, side, price, impact_bps, fee_usd=amount * fee_bps / 1e4, in_amount=amount, out_amount=out_qty, raw=order)
        out_usd = out_raw / 10 ** 6
        return Quote(symbol, side, out_usd / amount if amount else 0.0, impact_bps, fee_usd=out_usd * fee_bps / 1e4,
                     in_amount=amount, out_amount=out_usd, raw=order)

    # ------------------------------------------------------------------ Broker API
    def quote(self, symbol: str, side: Side, amount: float) -> Quote:
        order, out_dec = self._order(symbol, side, amount)
        return self._to_quote(symbol, side, amount, order, out_dec)

    def _execute(self, order_req: OrderRequest, qty: float, q: Quote) -> Fill:
        self.gate.assert_armed()  # re-checked on every single order, before anything is signed
        if q.raw is None:
            raise ExecutionError("quote has no order")
        order = q.raw
        res = None
        for attempt in range(self.s.execution.execute_max_retries + 1):
            signed_tx, local_sig = self._sign(order["transaction"])
            if self.presubmit_hook:                      # durable record BEFORE anything can reach the network
                self.presubmit_hook({"request_id": order["requestId"], "tx_signature": local_sig, "signed_tx": signed_tx})
            try:
                # _request resends the SAME signedTransaction+requestId on network errors (idempotent for ~2 min per the
                # skill); it never builds a new order on a timeout.
                res = self._request(self.exec_http, "POST", f"{self.jup.swap}/execute",
                                    json={"signedTransaction": signed_tx, "requestId": order["requestId"]})
            except ExecutionError as e:
                raise SubmittedUnknownError(f"execute outcome unknown: {e}", local_sig, order["requestId"]) from e
            if res.get("status") == "Success":
                break
            code = int(res.get("code") or 0)
            log.error("execute failed (code %s): %s [requestId=%s]", code, res.get("error"), order.get("requestId"))
            if code not in RETRYABLE_EXECUTE_CODES or attempt == self.s.execution.execute_max_retries:
                raise ExecutionError(f"swap execute failed: code={code} {res.get('error')}")
            time.sleep(min(2 ** attempt + random.random() * 0.5, 10.0))
            order, out_dec = self._order(order_req.symbol, order_req.side, q.in_amount)   # re-quote: orders expire (~2 min)
            q = self._to_quote(order_req.symbol, order_req.side, q.in_amount, order, out_dec)
            if q.expected_slippage_bps > self.risk.limits.max_slippage_bps:
                raise ExecutionError(f"re-quote slippage {q.expected_slippage_bps:.0f}bps exceeds limit")
        sig = res.get("signature", "")
        # actual on-chain amounts (skill: reconcile against the quote)
        _, out_dec = self._pair(order_req.symbol, order_req.side, q.in_amount)[3], self._mint_dec(order_req.symbol)[1]
        in_res, out_res = res.get("inputAmountResult"), res.get("outputAmountResult")
        if order_req.side == Side.BUY:
            got = int(out_res) / 10 ** out_dec if out_res else q.out_amount
            paid = int(in_res) / 1e6 if in_res else q.in_amount
            fill = Fill(order_id=order_req.id, symbol=order_req.symbol, side=order_req.side, qty=got, price=paid / got if got else q.price,
                        notional_usd=paid, fee_usd=q.fee_usd, slippage_bps=q.expected_slippage_bps, tx_signature=sig)
        else:
            got_usd = int(out_res) / 1e6 if out_res else q.out_amount
            sold = int(in_res) / 10 ** out_dec if in_res else qty
            fill = Fill(order_id=order_req.id, symbol=order_req.symbol, side=order_req.side, qty=sold, price=got_usd / sold if sold else q.price,
                        notional_usd=got_usd, fee_usd=q.fee_usd, slippage_bps=q.expected_slippage_bps, tx_signature=sig)
        log.warning("LIVE FILL %s %s qty=%.6g px=%.6g notional=$%.2f router=%s sig=%s", fill.side.value.upper(), fill.symbol,
                    fill.qty, fill.price, fill.notional_usd, order.get("router"), sig)
        return fill

    # ------------------------------------------------------------------ balances (RPC; Ultra balances endpoint is deprecated)
    def get_balances(self) -> dict[str, float]:
        by_mint = {m: s for s, m in self.provider.mints.items()}
        out = {"SOL": self._rpc("getBalance", [self.pubkey])["value"] / 1e9}
        res = self._rpc("getTokenAccountsByOwner", [self.pubkey, {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                                                     {"encoding": "jsonParsed"}])
        for acc in res.get("value", []):
            info = acc["account"]["data"]["parsed"]["info"]
            sym = by_mint.get(info["mint"])
            if sym:
                out[sym] = out.get(sym, 0.0) + float(info["tokenAmount"]["uiAmount"] or 0.0)
        return out
