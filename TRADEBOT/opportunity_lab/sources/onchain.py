"""Security facts and route checks from Solana RPC and Jupiter, read-only.

RPC (any provider; Helius when HELIUS_API_KEY is set): getAccountInfo(jsonParsed) for the mint's program,
authorities and Token-2022 extensions; getTokenLargestAccounts for concentration.
Jupiter: Tokens v2 search (audit.isSus, organicScore, holderCount) and Swap v2 `/order` WITHOUT `taker`, which returns
a price quote and no transaction: buy `size_usd` of USDC -> token, then quote selling exactly what that buy returns.
"""
from __future__ import annotations

import os
import time

from tradebot.execution.tx_verify import TOKEN_2022_PROGRAM, TOKEN_PROGRAM, USDC_MINT

from ..models import MintFacts, RouteCheck
from .base import Http, fnum

JUP_KEYED = "https://api.jup.ag"
JUP_KEYLESS = "https://api.jup.ag"            # keyless access is rate-limited (about 0.5 req/s)


class OnchainFacts:
    def __init__(self, http: Http, rpc_url: str, use_jupiter: bool = True, clock=time.time):
        self.http, self.clock, self.use_jupiter = http, clock, use_jupiter
        key = os.environ.get("HELIUS_API_KEY", "").strip()
        self.rpc_url = f"https://mainnet.helius-rpc.com/?api-key={key}" if key else rpc_url
        jk = os.environ.get("JUPITER_API_KEY", "").strip()
        self.jup_base, self.jup_headers = (JUP_KEYED, {"x-api-key": jk}) if jk else (JUP_KEYLESS, {})

    def _rpc(self, method: str, params: list):
        data = self.http.post("solana_rpc", self.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if not isinstance(data, dict) or "result" not in data:
            return None
        return data["result"]

    def mint_facts(self, mint: str) -> MintFacts | None:
        now = self.clock()
        f = MintFacts(mint=mint, observed_ts=now, mint_authority="?", freeze_authority="?")
        acc = self._rpc("getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        val = (acc or {}).get("value") if isinstance(acc, dict) else None
        if val:
            owner = val.get("owner")
            f.token_program = "spl-token-2022" if owner == TOKEN_2022_PROGRAM else ("spl-token" if owner == TOKEN_PROGRAM else owner)
            info = ((val.get("data") or {}).get("parsed") or {}).get("info") or {}
            f.decimals = info.get("decimals")
            f.mint_authority = info.get("mintAuthority")          # None when revoked
            f.freeze_authority = info.get("freezeAuthority")
            exts = info.get("extensions")
            f.extensions = [e.get("extension") for e in exts] if isinstance(exts, list) else ([] if f.token_program == "spl-token" else None)
            for e in exts or []:
                if e.get("extension") == "transferFeeConfig":
                    st = e.get("state") or {}
                    fee = (st.get("newerTransferFee") or {}).get("transferFeeBasisPoints")
                    f.transfer_fee_bps = fnum(fee)
            f.provenance["authorities"] = "rpc:getAccountInfo"
            supply = fnum(info.get("supply"))
            big = self._rpc("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
            amounts = sorted((fnum(a.get("amount")) or 0.0 for a in ((big or {}).get("value") or [])), reverse=True)
            if supply and amounts:
                # The largest account is usually the AMM pool vault; it is excluded from the holder metrics (recorded).
                holders = amounts[1:11]
                f.top10_pct = sum(holders) / supply * 100
                f.top1_pct = holders[0] / supply * 100 if holders else 0.0
                f.provenance["holders"] = "rpc:getTokenLargestAccounts (largest account excluded as presumed pool vault)"
        if self.use_jupiter:
            data = self.http.get("jupiter_tokens", f"{self.jup_base}/tokens/v2/search", params={"query": mint}, headers=self.jup_headers)
            t = next((x for x in data if x.get("id") == mint), None) if isinstance(data, list) else None
            if t:
                audit = t.get("audit") or {}
                f.jupiter_sus = audit.get("isSus") is True
                f.organic_score = fnum(t.get("organicScore"))
                f.holder_count = t.get("holderCount")
                f.provenance["indexer"] = "jupiter:tokens/v2"
        return f

    def route_check(self, mint: str, size_usd: float) -> RouteCheck:
        params = {"inputMint": USDC_MINT, "outputMint": mint, "amount": str(int(size_usd * 1e6)), "swapMode": "ExactIn"}
        buy = self.http.get("jupiter_swap", f"{self.jup_base}/swap/v2/order", params=params, headers=self.jup_headers)
        if not isinstance(buy, dict) or not buy.get("outAmount"):
            return RouteCheck(ok=False, source="jupiter", error=f"no buy route ({self.http.last_error.get('jupiter_swap', buy and buy.get('error'))})")
        out_raw = int(buy["outAmount"])
        sell = self.http.get("jupiter_swap", f"{self.jup_base}/swap/v2/order", headers=self.jup_headers,
                             params={"inputMint": mint, "outputMint": USDC_MINT, "amount": str(out_raw), "swapMode": "ExactIn"})
        if not isinstance(sell, dict) or not sell.get("outAmount"):
            return RouteCheck(ok=False, buy_out_raw=out_raw, source="jupiter", error="no sell route")
        back = int(sell["outAmount"]) / 1e6
        impact = max(abs(fnum(buy.get("priceImpact")) or 0.0), abs(fnum(sell.get("priceImpact")) or 0.0)) * 100
        return RouteCheck(ok=True, buy_out_raw=out_raw, sell_back_usd=back, round_trip_loss_bps=(1 - back / size_usd) * 1e4,
                          price_impact_bps=impact, fee_bps=fnum(buy.get("feeBps")), router=buy.get("router"), source="jupiter")
