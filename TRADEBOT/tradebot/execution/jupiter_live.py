"""Live execution on Solana via Jupiter, signing locally with the bot's keypair.

Two engines (config [execution].engine):
  swap  - Swap API v1: GET /swap/v1/quote -> POST /swap/v1/swap -> sign -> sendTransaction via rpc_url -> confirm
  ultra - Ultra API:   GET /ultra/v1/order?taker=<pubkey> -> sign -> POST /ultra/v1/execute (Jupiter broadcasts & confirms)

Every order: LiveGate.assert_armed -> RiskEngine.verify -> quote -> price-impact check -> sign -> send -> Fill.
Fill amounts come from the quote/order (slippage is enforced on-chain; a worse route reverts).
"""
from __future__ import annotations

import base64
import logging
import time

import httpx
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from ..config import Settings, jupiter_endpoints
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
        self.jup = jupiter_endpoints(settings.data, settings.execution)
        self.http = httpx.Client(timeout=settings.data.request_timeout_sec)
        self.rpc = httpx.Client(timeout=settings.data.request_timeout_sec)
        self.usdc_mint = settings.universe.mints["USDC"]
        log.warning("live broker (%s engine) initialised for wallet %s via %s", settings.execution.engine, redact(self.pubkey), self.jup.base)

    # ------------------------------------------------------------------ helpers
    def _mint_dec(self, symbol: str) -> tuple[str, int]:
        mint = self.provider.mint_for(symbol)
        if not mint:
            raise ExecutionError(f"unknown mint for {symbol}")
        return mint, self.provider.decimals.get(symbol, 6)

    def _pair(self, symbol: str, side: Side, amount: float) -> tuple[str, str, int, int]:
        """(input mint, output mint, raw input amount, output decimals)."""
        mint, dec = self._mint_dec(symbol)
        if side == Side.BUY:
            return self.usdc_mint, mint, int(round(amount * 10 ** 6)), dec
        return mint, self.usdc_mint, int(round(amount * 10 ** dec)), 6

    def _rpc(self, method: str, params: list) -> dict:
        r = self.rpc.post(self.s.execution.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise ExecutionError(f"rpc {method}: {data['error']}")
        return data["result"]

    def _get(self, url: str, params: dict) -> dict:
        r = self.http.get(url, params=params, headers=self.jup.headers)
        r.raise_for_status()
        return r.json()

    def _post(self, url: str, body: dict) -> dict:
        r = self.http.post(url, json=body, headers=self.jup.headers)
        r.raise_for_status()
        return r.json()

    def _sign(self, tx_b64: str) -> str:
        tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
        signed = VersionedTransaction(tx.message, [self.kp])
        return base64.b64encode(bytes(signed)).decode()

    def _to_quote(self, symbol: str, side: Side, amount: float, q: dict, out_dec: int, extra_fee_bps: float = 0.0) -> Quote:
        out_raw = int(q["outAmount"])
        impact_bps = float(q.get("priceImpactPct") or 0.0) * 1e4
        slip_half = self.s.execution.live_slippage_bps * 0.5
        if side == Side.BUY:
            out_qty = out_raw / 10 ** out_dec
            price = amount / out_qty if out_qty else float("inf")
            return Quote(symbol, side, price, impact_bps + slip_half, fee_usd=amount * (5 + extra_fee_bps) / 1e4,
                         in_amount=amount, out_amount=out_qty, raw=q)
        out_usd = out_raw / 10 ** 6
        price = out_usd / amount if amount else 0.0
        return Quote(symbol, side, price, impact_bps + slip_half, fee_usd=out_usd * (5 + extra_fee_bps) / 1e4,
                     in_amount=amount, out_amount=out_usd, raw=q)

    # ------------------------------------------------------------------ Broker API
    def quote(self, symbol: str, side: Side, amount: float) -> Quote:
        in_mint, out_mint, raw_in, out_dec = self._pair(symbol, side, amount)
        if self.s.execution.engine == "ultra":
            order = self._get(f"{self.jup.ultra}/order", {"inputMint": in_mint, "outputMint": out_mint, "amount": str(raw_in),
                                                           "taker": self.pubkey, "slippageBps": self.s.execution.live_slippage_bps})
            if not order.get("transaction"):
                raise ExecutionError(f"ultra order has no transaction: {order.get('errorMessage') or order}")
            return self._to_quote(symbol, side, amount, order, out_dec, extra_fee_bps=float(order.get("feeBps") or 0.0))
        q = self._get(f"{self.jup.swap}/quote", {"inputMint": in_mint, "outputMint": out_mint, "amount": str(raw_in),
                                                  "slippageBps": self.s.execution.live_slippage_bps, "restrictIntermediateTokens": "true"})
        return self._to_quote(symbol, side, amount, q, out_dec)

    def _execute(self, order: OrderRequest, qty: float, q: Quote) -> Fill:
        self.gate.assert_armed()  # re-checked on every single order, before anything is signed
        if q.raw is None:
            raise ExecutionError("quote has no route")
        sig = self._execute_ultra(q.raw) if self.s.execution.engine == "ultra" else self._execute_swap(q.raw)
        if order.side == Side.BUY:
            fill = Fill(order_id=order.id, symbol=order.symbol, side=order.side, qty=q.out_amount, price=q.price,
                        notional_usd=q.in_amount, fee_usd=q.fee_usd, slippage_bps=q.expected_slippage_bps, tx_signature=sig)
        else:
            fill = Fill(order_id=order.id, symbol=order.symbol, side=order.side, qty=qty, price=q.price,
                        notional_usd=q.out_amount, fee_usd=q.fee_usd, slippage_bps=q.expected_slippage_bps, tx_signature=sig)
        log.warning("LIVE FILL %s %s qty=%.6g px=%.6g notional=$%.2f sig=%s", fill.side.value.upper(), fill.symbol,
                    fill.qty, fill.price, fill.notional_usd, sig)
        return fill

    # ------------------------------------------------------------------ Swap API v1 path
    def swap_body(self, quote_response: dict) -> dict:
        return {
            "quoteResponse": quote_response,
            "userPublicKey": self.pubkey,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "dynamicSlippage": False,
            "prioritizationFeeLamports": {"priorityLevelWithMaxLamports": {
                "maxLamports": self.s.execution.priority_fee_lamports, "priorityLevel": self.s.execution.priority_level}},
        }

    def _execute_swap(self, quote_response: dict) -> str:
        swap = self._post(f"{self.jup.swap}/swap", self.swap_body(quote_response))
        if swap.get("simulationError"):
            raise ExecutionError(f"swap simulation failed: {swap['simulationError']}")
        signed = self._sign(swap["swapTransaction"])
        sig = self._rpc("sendTransaction", [signed, {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}])
        self._confirm(sig, swap.get("lastValidBlockHeight"))
        return sig

    def _confirm(self, sig: str, last_valid_block_height: int | None = None) -> None:
        deadline = time.time() + self.s.execution.confirm_timeout_sec
        while time.time() < deadline:
            res = self._rpc("getSignatureStatuses", [[sig], {"searchTransactionHistory": True}])
            st = (res.get("value") or [None])[0]
            if st:
                if st.get("err"):
                    raise ExecutionError(f"transaction {sig} failed on-chain: {st['err']}")
                if st.get("confirmationStatus") in ("confirmed", "finalized"):
                    return
            if last_valid_block_height is not None:
                height = self._rpc("getBlockHeight", [])
                if isinstance(height, int) and height > last_valid_block_height:
                    raise ExecutionError(f"transaction {sig} expired (block height {height} > {last_valid_block_height})")
            time.sleep(1.5)
        raise ExecutionError(f"transaction {sig} not confirmed within {self.s.execution.confirm_timeout_sec}s")

    # ------------------------------------------------------------------ Ultra path
    def _execute_ultra(self, order: dict) -> str:
        signed = self._sign(order["transaction"])
        res = self._post(f"{self.jup.ultra}/execute", {"signedTransaction": signed, "requestId": order["requestId"]})
        if res.get("status") != "Success":
            raise ExecutionError(f"ultra execute failed: code={res.get('code')} {res.get('error') or res}")
        return res.get("signature", "")

    # ------------------------------------------------------------------ balances
    def get_balances(self) -> dict[str, float]:
        """Ultra balances endpoint (RPC-free); falls back to RPC."""
        by_mint = {m: s for s, m in self.provider.mints.items()}
        try:
            data = self._get(f"{self.jup.ultra}/balances/{self.pubkey}", {})
            out: dict[str, float] = {}
            for key, v in data.items():
                sym = "SOL" if key == "SOL" else by_mint.get(key)
                if sym:
                    out[sym] = out.get(sym, 0.0) + float(v.get("uiAmount") or 0.0)
            return out
        except (httpx.HTTPError, ValueError, KeyError) as e:
            log.info("ultra balances unavailable (%s); using RPC", e)
        out = {"SOL": self._rpc("getBalance", [self.pubkey])["value"] / 1e9}
        res = self._rpc("getTokenAccountsByOwner", [self.pubkey, {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                                                     {"encoding": "jsonParsed"}])
        for acc in res.get("value", []):
            info = acc["account"]["data"]["parsed"]["info"]
            sym = by_mint.get(info["mint"])
            if sym:
                out[sym] = out.get(sym, 0.0) + float(info["tokenAmount"]["uiAmount"] or 0.0)
        return out
