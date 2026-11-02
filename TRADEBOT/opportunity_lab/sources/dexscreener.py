"""DexScreener public API (keyless). Discovery: latest token profiles and boosts (boosts are PAID promotions: they
are recorded with `promoted=True` and are a discovery input only, never a signal). Snapshots: all Solana pairs of a
token, keeping the most liquid one.

Endpoints (docs.dexscreener.com/api/reference): /token-profiles/latest/v1 and /token-boosts/latest/v1 (60 req/min),
/tokens/v1/solana/{addresses, up to 30} (300 req/min).
"""
from __future__ import annotations

import time

from ..models import TokenSnapshot
from .base import Http, fnum, inum

BASE = "https://api.dexscreener.com"


def pair_to_snapshot(p: dict, observed_ts: float) -> TokenSnapshot | None:
    base = p.get("baseToken") or {}
    mint = base.get("address")
    if not mint or p.get("chainId") != "solana":
        return None
    tx, vol, chg = p.get("txns") or {}, p.get("volume") or {}, p.get("priceChange") or {}
    created = fnum(p.get("pairCreatedAt"))
    return TokenSnapshot(mint=mint, observed_ts=observed_ts, source="dexscreener", symbol=base.get("symbol", ""),
                         pair_address=p.get("pairAddress", ""), dex_id=(p.get("dexId") or "").lower(),
                         quote_symbol=(p.get("quoteToken") or {}).get("symbol", ""), price_usd=fnum(p.get("priceUsd")),
                         liquidity_usd=fnum((p.get("liquidity") or {}).get("usd")), volume_m5=fnum(vol.get("m5")),
                         volume_h1=fnum(vol.get("h1")), volume_h24=fnum(vol.get("h24")),
                         buys_m5=inum((tx.get("m5") or {}).get("buys")), sells_m5=inum((tx.get("m5") or {}).get("sells")),
                         buys_h1=inum((tx.get("h1") or {}).get("buys")), sells_h1=inum((tx.get("h1") or {}).get("sells")),
                         price_change_m5=fnum(chg.get("m5")), price_change_h1=fnum(chg.get("h1")), fdv_usd=fnum(p.get("fdv")),
                         pair_created_ts=created / 1000 if created else None)


class DexScreenerSource:
    name = "dexscreener"

    def __init__(self, http: Http, clock=time.time):
        self.http, self.clock = http, clock
        self.promoted: set[str] = set()

    def _candidates(self) -> list[str]:
        mints: list[str] = []
        for path, promoted in (("/token-profiles/latest/v1", False), ("/token-boosts/latest/v1", True)):
            data = self.http.get("dexscreener_slow", BASE + path) or []
            for t in data if isinstance(data, list) else []:
                if t.get("chainId") == "solana" and t.get("tokenAddress"):
                    mints.append(t["tokenAddress"])
                    if promoted:
                        self.promoted.add(t["tokenAddress"])
        return list(dict.fromkeys(mints))

    def discover(self) -> list[TokenSnapshot]:
        return list(self.refresh(self._candidates()).values())

    def refresh(self, mints: list[str]) -> dict[str, TokenSnapshot]:
        out: dict[str, TokenSnapshot] = {}
        for i in range(0, len(mints), 30):
            chunk = mints[i:i + 30]
            data = self.http.get("dexscreener_pairs", f"{BASE}/tokens/v1/solana/{','.join(chunk)}")
            now = self.clock()
            for p in data if isinstance(data, list) else []:
                s = pair_to_snapshot(p, now)
                if s and s.mint in chunk and (s.mint not in out or (s.liquidity_usd or 0) > (out[s.mint].liquidity_usd or 0)):
                    out[s.mint] = s
        return out
