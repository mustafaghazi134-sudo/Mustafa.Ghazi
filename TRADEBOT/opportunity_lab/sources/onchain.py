"""Security facts and route checks from Solana RPC and Jupiter, read-only.

RPC (any provider; Helius when HELIUS_API_KEY is set): getAccountInfo(jsonParsed) for the mint's program,
authorities and Token-2022 extensions; getTokenLargestAccounts for concentration.
Jupiter: Tokens v2 search (audit.isSus, organicScore, holderCount) and Swap v2 `/order` WITHOUT `taker`, which returns
a price quote and no transaction: buy `size_usd` of USDC -> token, then quote selling exactly what that buy returns.
"""
from __future__ import annotations

import json
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
        res = self._rpc_call(method, params)
        return res.data["result"] if res.outcome == "ok" and isinstance(res.data, dict) and "result" in res.data else None

    def _rpc_call(self, method: str, params: list):
        """One RPC method, in its own limiter bucket (a 429 on one method does not back off the others)."""
        res = self.http.call("solana_rpc", "POST", self.rpc_url, bucket=f"solana_rpc:{method}", retries=1,
                             json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if res.outcome == "ok" and isinstance(res.data, dict) and "error" in res.data:
            err = res.data.get("error") or {}
            code = err.get("code") if isinstance(err, dict) else None
            res.outcome, res.error = ("rate_limited" if code == 429 else "http_error"), f"rpc error {code}: {str(err)[:120]}"
        return res

    @staticmethod
    def _state(res, has_data: bool) -> str:
        if res.outcome == "ok":
            return "confirmed" if has_data else "missing"
        return "missing" if res.outcome == "http_error" and (res.status or 0) < 500 else "failed"

    def mint_facts(self, mint: str) -> MintFacts | None:
        now = self.clock()
        f = MintFacts(mint=mint, observed_ts=now, mint_authority="?", freeze_authority="?")
        provider = "helius" if "helius" in self.rpc_url else "solana-public-rpc"
        res = self._rpc_call("getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        acc = res.data.get("result") if res.outcome == "ok" and isinstance(res.data, dict) else None
        val = (acc or {}).get("value") if isinstance(acc, dict) else None
        f.data_status["authorities"] = {**res.record(f"{provider}:getAccountInfo"), "state": self._state(res, bool(val))}
        f.data_status["holders"] = {"source": f"{provider}:getTokenLargestAccounts", "state": "failed", "outcome": "not_requested",
                                    "error": "mint account unavailable"}
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
            hres = self._rpc_call("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
            big = hres.data.get("result") if hres.outcome == "ok" and isinstance(hres.data, dict) else None
            amounts = sorted((fnum(a.get("amount")) or 0.0 for a in ((big or {}).get("value") or [])), reverse=True)
            got = bool(supply and amounts)
            f.data_status["holders"] = {**hres.record(f"{provider}:getTokenLargestAccounts"), "state": self._state(hres, got)}
            if got:
                # The largest account is usually the AMM pool vault; it is excluded from the holder metrics (recorded).
                holders = amounts[1:11]
                f.top10_pct = sum(holders) / supply * 100
                f.top1_pct = holders[0] / supply * 100 if holders else 0.0
                f.provenance["holders"] = "rpc:getTokenLargestAccounts (largest account excluded as presumed pool vault)"
        if self.use_jupiter:
            jres = self.http.call("jupiter_tokens", "GET", f"{self.jup_base}/tokens/v2/search", params={"query": mint},
                                  headers=self.jup_headers, retries=1)
            data = jres.data if jres.outcome == "ok" else None
            t = next((x for x in data if x.get("id") == mint), None) if isinstance(data, list) else None
            f.data_status["indexer"] = {**jres.record("jupiter:tokens/v2"), "state": self._state(jres, t is not None)}
            if t:
                audit = t.get("audit") or {}
                f.jupiter_sus = audit.get("isSus") is True
                f.organic_score = fnum(t.get("organicScore"))
                f.holder_count = t.get("holderCount")
                f.indexer_audit = {k: audit.get(k) for k in ("mintAuthorityDisabled", "freezeAuthorityDisabled",
                                                             "topHoldersPercentage", "devBalancePercentage", "isSus")}
                f.provenance["indexer"] = "jupiter:tokens/v2"
        return f

    NO_ROUTE_HINTS = ("route", "could_not_find", "no_routes", "not tradable", "not_tradable", "insufficient liquidity")

    def _quote(self, params: dict):
        """-> (CallResult, payload, confirmed_no_route). A 'no route' is CONFIRMED only when the provider answered and
        said so; a 429, local throttle, timeout or 5xx is no answer at all."""
        res = self.http.call("jupiter_swap", "GET", f"{self.jup_base}/swap/v2/order", params=params, headers=self.jup_headers,
                             retries=2, max_wait_sec=6.0)
        payload = res.data if res.outcome == "ok" else res.body
        if isinstance(payload, dict) and payload.get("outAmount"):
            return res, payload, False
        answered = res.outcome == "ok" or (res.outcome == "http_error" and (res.status or 0) < 500)
        text = json.dumps(payload).lower() if isinstance(payload, (dict, list)) else ""
        return res, payload, bool(answered and any(h in text for h in self.NO_ROUTE_HINTS))

    def route_check(self, mint: str, size_usd: float) -> RouteCheck:
        params = {"inputMint": USDC_MINT, "outputMint": mint, "amount": str(int(size_usd * 1e6)), "swapMode": "ExactIn"}
        bres, buy, buy_none = self._quote(params)
        status = {"buy": {**bres.record("jupiter:swap/v2/order"), "confirmed_no_route": buy_none}}
        if not (isinstance(buy, dict) and buy.get("outAmount")):
            if buy_none:
                return RouteCheck(ok=False, source="jupiter", error="no buy route (confirmed by Jupiter)", status=status)
            return RouteCheck(ok=False, unknown=True, retry_pending=True, source="jupiter", status=status,
                              error=f"buy quote unavailable ({bres.error or bres.outcome}); retry pending")
        out_raw = int(buy["outAmount"])
        sres, sell, sell_none = self._quote({"inputMint": mint, "outputMint": USDC_MINT, "amount": str(out_raw), "swapMode": "ExactIn"})
        status["sell"] = {**sres.record("jupiter:swap/v2/order"), "confirmed_no_route": sell_none}
        if not (isinstance(sell, dict) and sell.get("outAmount")):
            if sell_none:
                return RouteCheck(ok=False, buy_out_raw=out_raw, source="jupiter", error="no sell route (confirmed by Jupiter)", status=status)
            return RouteCheck(ok=False, unknown=True, retry_pending=True, buy_out_raw=out_raw, source="jupiter", status=status,
                              error=f"sell quote unavailable ({sres.error or sres.outcome}); retry pending")
        back = int(sell["outAmount"]) / 1e6
        impact = max(abs(fnum(buy.get("priceImpact")) or 0.0), abs(fnum(sell.get("priceImpact")) or 0.0)) * 100
        return RouteCheck(ok=True, buy_out_raw=out_raw, sell_back_usd=back, round_trip_loss_bps=(1 - back / size_usd) * 1e4,
                          price_impact_bps=impact, fee_bps=fnum(buy.get("feeBps")), router=buy.get("router"), source="jupiter",
                          status=status)
