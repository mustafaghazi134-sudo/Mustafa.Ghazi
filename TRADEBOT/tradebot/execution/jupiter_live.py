"""Live execution on Solana via the Jupiter swap API, signing locally with the bot's keypair.

Flow per order: LiveGate.assert_armed -> RiskEngine.verify -> GET quote -> price-impact check ->
POST swap -> sign VersionedTransaction -> sendTransaction -> poll confirmation -> Fill.
Fill amounts come from the quote (Jupiter enforces slippage on-chain; a worse route reverts).
"""
from __future__ import annotations

import base64
import logging
import time

import httpx
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from ..config import Settings
from ..data.live import LiveProvider
from ..models import Fill, OrderRequest, Side
from ..risk.engine import RiskEngine
from .base import Broker, ExecutionError, Quote
from .live_gate import LiveGate
from .wallet import redact

log = logging.getLogger(__name__)


class JupiterLiveBroker(Broker):
    def __init__(self, risk: RiskEngine, settings: Settings, provider: LiveProvider, keypair: Keypair, gate: LiveGate):
        super().__init__(risk)
        self.s = settings
        self.provider = provider
        self.kp = keypair
        self.gate = gate
        self.pubkey = str(keypair.pubkey())
        self.http = httpx.Client(timeout=settings.data.request_timeout_sec)
        self.rpc = httpx.Client(timeout=settings.data.request_timeout_sec)
        self.usdc_mint = settings.universe.mints["USDC"]
        log.warning("live broker initialised for wallet %s", redact(self.pubkey))

    # ------------------------------------------------------------------ helpers
    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self.s.data.jupiter_api_key} if self.s.data.jupiter_api_key else {}

    def _mint_dec(self, symbol: str) -> tuple[str, int]:
        mint = self.provider.mint_for(symbol)
        if not mint:
            raise ExecutionError(f"unknown mint for {symbol}")
        return mint, self.provider.decimals.get(symbol, 6)

    def _rpc(self, method: str, params: list) -> dict:
        r = self.rpc.post(self.s.execution.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise ExecutionError(f"rpc {method}: {data['error']}")
        return data["result"]

    def _jup_quote(self, in_mint: str, out_mint: str, amount_raw: int) -> dict:
        r = self.http.get(f"{self.s.execution.jupiter_swap_base_url}/quote", headers=self._headers(),
                          params={"inputMint": in_mint, "outputMint": out_mint, "amount": str(amount_raw),
                                  "slippageBps": self.s.execution.live_slippage_bps, "restrictIntermediateTokens": "true"})
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------------ Broker API
    def quote(self, symbol: str, side: Side, amount: float) -> Quote:
        mint, dec = self._mint_dec(symbol)
        if side == Side.BUY:
            raw_in = int(round(amount * 10 ** 6))                    # USDC has 6 decimals
            q = self._jup_quote(self.usdc_mint, mint, raw_in)
            out_qty = int(q["outAmount"]) / 10 ** dec
            price = amount / out_qty if out_qty else float("inf")
            impact_bps = float(q.get("priceImpactPct") or 0.0) * 1e4
            return Quote(symbol, side, price, impact_bps + self.s.execution.live_slippage_bps * 0.5,
                         fee_usd=amount * 0.0005, in_amount=amount, out_amount=out_qty, raw=q)
        raw_in = int(round(amount * 10 ** dec))
        q = self._jup_quote(mint, self.usdc_mint, raw_in)
        out_usd = int(q["outAmount"]) / 10 ** 6
        price = out_usd / amount if amount else 0.0
        impact_bps = float(q.get("priceImpactPct") or 0.0) * 1e4
        return Quote(symbol, side, price, impact_bps + self.s.execution.live_slippage_bps * 0.5,
                     fee_usd=out_usd * 0.0005, in_amount=amount, out_amount=out_usd, raw=q)

    def _execute(self, order: OrderRequest, qty: float, q: Quote) -> Fill:
        self.gate.assert_armed()  # re-checked on every single order
        if q.raw is None:
            raise ExecutionError("quote has no route")
        r = self.http.post(f"{self.s.execution.jupiter_swap_base_url}/swap", headers=self._headers(), json={
            "quoteResponse": q.raw, "userPublicKey": self.pubkey, "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True, "dynamicSlippage": False,
            "prioritizationFeeLamports": self.s.execution.priority_fee_lamports,
        })
        r.raise_for_status()
        swap_tx_b64 = r.json()["swapTransaction"]
        tx = VersionedTransaction.from_bytes(base64.b64decode(swap_tx_b64))
        signed = VersionedTransaction(tx.message, [self.kp])
        sig = self._rpc("sendTransaction", [base64.b64encode(bytes(signed)).decode(),
                                            {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}])
        self._confirm(sig)
        if order.side == Side.BUY:
            fill = Fill(order_id=order.id, symbol=order.symbol, side=order.side, qty=q.out_amount, price=q.price,
                        notional_usd=q.in_amount, fee_usd=q.fee_usd, slippage_bps=q.expected_slippage_bps, tx_signature=sig)
        else:
            fill = Fill(order_id=order.id, symbol=order.symbol, side=order.side, qty=qty, price=q.price,
                        notional_usd=q.out_amount, fee_usd=q.fee_usd, slippage_bps=q.expected_slippage_bps, tx_signature=sig)
        log.warning("LIVE FILL %s %s qty=%.6g px=%.6g notional=$%.2f sig=%s", fill.side.value.upper(), fill.symbol,
                    fill.qty, fill.price, fill.notional_usd, sig)
        return fill

    def _confirm(self, sig: str) -> None:
        deadline = time.time() + self.s.execution.confirm_timeout_sec
        while time.time() < deadline:
            res = self._rpc("getSignatureStatuses", [[sig], {"searchTransactionHistory": True}])
            st = (res.get("value") or [None])[0]
            if st:
                if st.get("err"):
                    raise ExecutionError(f"transaction {sig} failed on-chain: {st['err']}")
                if st.get("confirmationStatus") in ("confirmed", "finalized"):
                    return
            time.sleep(1.5)
        raise ExecutionError(f"transaction {sig} not confirmed within {self.s.execution.confirm_timeout_sec}s")

    # ------------------------------------------------------------------ balances
    def get_balances(self) -> dict[str, float]:
        out: dict[str, float] = {}
        lamports = self._rpc("getBalance", [self.pubkey])["value"]
        out["SOL"] = lamports / 1e9
        res = self._rpc("getTokenAccountsByOwner", [self.pubkey, {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                                                     {"encoding": "jsonParsed"}])
        by_mint = {m: s for s, m in self.provider.mints.items()}
        for acc in res.get("value", []):
            info = acc["account"]["data"]["parsed"]["info"]
            sym = by_mint.get(info["mint"])
            if sym:
                out[sym] = out.get(sym, 0.0) + float(info["tokenAmount"]["uiAmount"] or 0.0)
        return out
