"""GeckoTerminal public API (keyless, ~30 calls/min): new and trending Solana pools.
Endpoints: /api/v2/networks/solana/new_pools, /trending_pools, /tokens/multi/{addresses} (apiguide.geckoterminal.com)."""
from __future__ import annotations

import time
from datetime import datetime

from ..models import TokenSnapshot
from .base import Http, fnum, inum

BASE = "https://api.geckoterminal.com/api/v2/networks/solana"
HEADERS = {"Accept": "application/json;version=20230302"}


def _ts(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def pool_to_snapshot(pool: dict, observed_ts: float) -> TokenSnapshot | None:
    a = pool.get("attributes") or {}
    rel = pool.get("relationships") or {}
    base_id = ((rel.get("base_token") or {}).get("data") or {}).get("id", "")
    mint = base_id.split("_", 1)[1] if base_id.startswith("solana_") else None
    if not mint:
        return None
    tx, vol, chg = a.get("transactions") or {}, a.get("volume_usd") or {}, a.get("price_change_percentage") or {}
    dex = ((rel.get("dex") or {}).get("data") or {}).get("id", "")
    name = a.get("name") or ""
    return TokenSnapshot(mint=mint, observed_ts=observed_ts, source="geckoterminal", symbol=name.split(" / ")[0][:20],
                         pair_address=a.get("address", ""), dex_id=dex.lower().replace("-solana", ""),
                         quote_symbol=name.split(" / ")[1] if " / " in name else "", price_usd=fnum(a.get("base_token_price_usd")),
                         liquidity_usd=fnum(a.get("reserve_in_usd")), volume_m5=fnum(vol.get("m5")), volume_h1=fnum(vol.get("h1")),
                         volume_h24=fnum(vol.get("h24")), buys_m5=inum((tx.get("m5") or {}).get("buys")),
                         sells_m5=inum((tx.get("m5") or {}).get("sells")), buys_h1=inum((tx.get("h1") or {}).get("buys")),
                         sells_h1=inum((tx.get("h1") or {}).get("sells")), price_change_m5=fnum(chg.get("m5")),
                         price_change_h1=fnum(chg.get("h1")), fdv_usd=fnum(a.get("fdv_usd")), pair_created_ts=_ts(a.get("pool_created_at")))


class GeckoTerminalSource:
    name = "geckoterminal"

    def __init__(self, http: Http, clock=time.time):
        self.http, self.clock = http, clock

    def discover(self) -> list[TokenSnapshot]:
        out: dict[str, TokenSnapshot] = {}
        for path in ("/new_pools", "/trending_pools"):
            data = self.http.get("geckoterminal", BASE + path, headers=HEADERS) or {}
            now = self.clock()
            for pool in data.get("data", []) if isinstance(data, dict) else []:
                s = pool_to_snapshot(pool, now)
                if s and (s.mint not in out or (s.liquidity_usd or 0) > (out[s.mint].liquidity_usd or 0)):
                    out[s.mint] = s
        return list(out.values())

    def refresh(self, mints: list[str]) -> dict[str, TokenSnapshot]:
        return {}     # DexScreener refreshes known tokens; GeckoTerminal's budget is kept for discovery
