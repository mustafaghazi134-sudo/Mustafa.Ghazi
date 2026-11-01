"""Paper broker: simulated fills that hold no real funds.

Fill model (most conservative available):
  1. If the data provider is the live Jupiter provider and an API key is configured, request a real Swap v2
     `/order` quote WITHOUT `taker` for the exact size and direction (nothing is created or signed), and fill at
     `otherAmountThreshold` (the minimum the route guarantees after the configured slippage), so the fill already
     includes Jupiter's fee, route price impact and the slippage tolerance. A fixed Solana network fee is charged too.
  2. Otherwise (replay / synthetic data, or no key) fall back to the parametric model: mid price plus a random
     slippage in [0.3, 1.0] x paper_slippage_bps against the trade, plus paper_fee_bps.
Either way the effective cost versus the mid price is recorded on the Fill as slippage_bps.
"""
from __future__ import annotations

import logging
import random

import httpx

from ..data.base import MarketDataProvider
from ..models import Fill, OrderRequest, Side
from ..risk.engine import RiskEngine
from .base import Broker, ExecutionError, Quote

log = logging.getLogger(__name__)

SOLANA_TX_FEE_USD = 0.01   # base fee + typical priority fee, charged per paper fill when using live quotes


class PaperBroker(Broker):
    def __init__(self, risk: RiskEngine, provider: MarketDataProvider, fee_bps: float = 30.0,
                 slippage_bps: float = 15.0, seed: int = 7, clock=None, quote_timeout_sec: float = 5.0,
                 live_slippage_bps: int = 50):
        super().__init__(risk)
        self.provider = provider
        self.fee_bps = fee_bps
        self.slippage_bps = slippage_bps
        self.rng = random.Random(seed)
        self.clock = clock
        self.quote_timeout_sec = quote_timeout_sec
        self.live_slippage_bps = live_slippage_bps
        self._confirmed: set[str] = set()
        self.quote_source = "parametric"
        jup = getattr(provider, "jup", None)
        self._jup = jup if (jup is not None and getattr(jup, "has_key", False)) else None

    # ------------------------------------------------------------------ pricing
    def _mid(self, symbol: str) -> float:
        t = self.provider.get_tickers([symbol]).get(symbol)
        if not t or t.price <= 0:
            raise ExecutionError(f"no price for {symbol}")
        return t.price

    def _parametric_quote(self, symbol: str, side: Side, amount: float, mid: float) -> Quote:
        slip_bps = float(self.slippage_bps * self.rng.uniform(0.3, 1.0))
        px = mid * (1 + slip_bps / 1e4) if side == Side.BUY else mid * (1 - slip_bps / 1e4)
        if side == Side.BUY:
            fee = amount * self.fee_bps / 1e4
            return Quote(symbol, side, px, slip_bps, fee, amount, (amount - fee) / px)
        gross = amount * px
        fee = gross * self.fee_bps / 1e4
        return Quote(symbol, side, px, slip_bps, fee, amount, gross - fee)

    def _jupiter_quote(self, symbol: str, side: Side, amount: float, mid: float) -> Quote | None:
        """Size-specific read-only quote. Returns None (fallback) if the mint is unknown or the request fails."""
        mints, decs = self.provider.mints, self.provider.decimals          # LiveProvider attributes
        mint, dec, usdc = mints.get(symbol), decs.get(symbol, 6), mints.get("USDC")
        if not mint or not usdc:
            return None
        if side == Side.BUY:
            params = {"inputMint": usdc, "outputMint": mint, "amount": str(int(round(amount * 1e6))), "slippageBps": self.live_slippage_bps}
        else:
            params = {"inputMint": mint, "outputMint": usdc, "amount": str(int(round(amount * 10 ** dec))), "slippageBps": self.live_slippage_bps}
        try:
            r = httpx.get(f"{self._jup.swap}/order", params=params, headers=self._jup.headers, timeout=self.quote_timeout_sec)
            r.raise_for_status()
            q = r.json()
            if q.get("error") or "outAmount" not in q:
                raise ExecutionError(q.get("error") or "no outAmount")
        except (httpx.HTTPError, ExecutionError, ValueError) as e:
            log.warning("paper: Jupiter quote unavailable for %s %s (%s); using parametric fill", side.value, symbol, e)
            return None
        min_out_raw = int(q.get("otherAmountThreshold") or q["outAmount"])   # conservative: the guaranteed minimum
        if side == Side.BUY:
            qty = min_out_raw / 10 ** dec
            px = (amount - SOLANA_TX_FEE_USD) / qty if qty else float("inf")
            eff_bps = (px / mid - 1) * 1e4
            return Quote(symbol, side, px, eff_bps, SOLANA_TX_FEE_USD, amount, qty, raw={"router": q.get("router"), "feeBps": q.get("feeBps")})
        out_usd = min_out_raw / 1e6 - SOLANA_TX_FEE_USD
        px = out_usd / amount if amount else 0.0
        eff_bps = (1 - px / mid) * 1e4
        return Quote(symbol, side, px, eff_bps, SOLANA_TX_FEE_USD, amount, out_usd, raw={"router": q.get("router"), "feeBps": q.get("feeBps")})

    def quote(self, symbol: str, side: Side, amount: float) -> Quote:
        mid = self._mid(symbol)
        if self._jup is not None:
            q = self._jupiter_quote(symbol, side, amount, mid)
            if q is not None:
                self.quote_source = "jupiter"
                return q
        self.quote_source = "parametric"
        return self._parametric_quote(symbol, side, amount, mid)

    # ------------------------------------------------------------------ reconciliation support
    def lookup_signature(self, tx_signature: str) -> str:
        return "confirmed" if tx_signature in self._confirmed else "not_found"

    def transaction_status(self, tx_signature: str, signed_tx_b64: str | None) -> dict:
        st = self.lookup_signature(tx_signature)
        return {"status": st, "blockhash_valid": False if st == "not_found" else None}

    # ------------------------------------------------------------------ fills
    def _execute(self, order: OrderRequest, qty: float, q: Quote) -> Fill:
        ts = self.clock() if self.clock else None
        sig = f"paper-{order.id}"
        if self.presubmit_hook:
            self.presubmit_hook({"request_id": f"paper-req-{order.id}", "tx_signature": sig, "signed_tx": ""})
        self._confirmed.add(sig)
        if order.side == Side.BUY:
            fill = Fill(order_id=order.id, symbol=order.symbol, side=order.side, qty=q.out_amount, price=q.price,
                        notional_usd=q.in_amount, fee_usd=q.fee_usd, slippage_bps=q.expected_slippage_bps, tx_signature=sig)
        else:
            fill = Fill(order_id=order.id, symbol=order.symbol, side=order.side, qty=qty, price=q.price,
                        notional_usd=qty * q.price, fee_usd=q.fee_usd, slippage_bps=q.expected_slippage_bps, tx_signature=sig)
        if ts is not None:
            fill.ts = ts
        log.info("PAPER FILL (%s quote) %s %s qty=%.6g px=%.6g notional=$%.2f cost=%.1fbps", self.quote_source,
                 fill.side.value.upper(), fill.symbol, fill.qty, fill.price, fill.notional_usd, fill.slippage_bps)
        return fill
