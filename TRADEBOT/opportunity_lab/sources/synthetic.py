"""A synthetic Solana new-token market for testing the lab's MECHANICS (detection, rejection, fills, exits, crash
recovery). It is not market data and nothing measured on it is evidence of profitability.

Each token follows one archetype with known ground truth, so tests can check that the security agent rejects what it
must reject (rugs, honeypots, live mint/freeze authority, Token-2022 fee traps) and that the executor handles gaps,
unsellable tokens and liquidity loss. Archetypes deliberately include traps that look like opportunities.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from ..models import MintFacts, RouteCheck, TokenSnapshot

ARCHETYPES = {             # weight, per-step drift, per-step sigma
    "noise": (0.30, 0.0, 0.012),
    "organic": (0.10, 0.0012, 0.010),
    "slow_bleed": (0.15, -0.0010, 0.010),
    "pump_dump": (0.15, 0.0, 0.012),
    "rug": (0.12, 0.0004, 0.012),
    "honeypot": (0.06, 0.0030, 0.008),
    "mint_live": (0.05, 0.0005, 0.012),
    "fee_trap": (0.04, 0.0008, 0.010),
    "freeze_live": (0.03, 0.0008, 0.010),
}


class SimClock:
    def __init__(self, start: float):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, sec: float) -> None:
        self.t += sec


@dataclass
class _Tok:
    mint: str
    symbol: str
    archetype: str
    launch_ts: float
    dex_id: str
    price: list[float] = field(default_factory=list)
    liq: list[float] = field(default_factory=list)
    vol: list[float] = field(default_factory=list)
    buy_share: list[float] = field(default_factory=list)


def _b58(rng: random.Random, n: int = 44) -> str:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    return "".join(rng.choice(alphabet) for _ in range(n))


class SyntheticMarket:
    """Market data, facts and route checks for synthetic tokens. All three share one simulated clock."""
    name = "synthetic"

    def __init__(self, clock: SimClock, seed: int = 7, n_tokens: int = 60, hours: float = 24.0, step_sec: float = 30.0,
                 mix: dict[str, float] | None = None, null_edge: bool = False):
        """null_edge=True: every price path is a driftless martingale (pumps keep their volume and buy pressure, but
        their price increments have zero mean). Nothing is predictable, so a correct harness must lose about its costs:
        a profit on a null market means look-ahead or a fill bug."""
        self.clock, self.step, self.rng, self.null_edge = clock, step_sec, random.Random(seed), null_edge
        self.start = clock()
        self.n_steps = int(hours * 3600 / step_sec) + 1
        names = list(ARCHETYPES)
        weights = [(mix or {}).get(a, ARCHETYPES[a][0]) for a in names]
        self.tokens: dict[str, _Tok] = {}
        for i in range(n_tokens):
            arch = self.rng.choices(names, weights)[0]
            launch = self.start + self.rng.uniform(-6, hours * 0.8) * 3600
            t = _Tok(_b58(self.rng), f"SYN{i}", arch, launch, self.rng.choice(["pumpswap", "raydium", "meteora"]))
            self._simulate(t)
            self.tokens[t.mint] = t
        self.ground_truth = {m: t.archetype for m, t in self.tokens.items()}

    # ------------------------------------------------------------------ path generation
    def _simulate(self, t: _Tok) -> None:
        w, mu, sig = ARCHETYPES[t.archetype]
        rng = self.rng
        p, liq = rng.uniform(0.0005, 0.05), rng.uniform(40_000, 600_000)
        steps_since_launch0 = int((self.start - t.launch_ts) / self.step)
        total = self.n_steps + max(steps_since_launch0, 0)
        pump_at = rng.randint(40, 240) if t.archetype == "pump_dump" else -1
        rug_at = rng.randint(60, 600) if t.archetype == "rug" else -1
        for k in range(total):
            d, s, vol_mult, bs = mu, sig, 1.0, 0.5 + rng.uniform(-0.05, 0.05)
            if t.archetype == "pump_dump" and pump_at <= k < pump_at + 40:
                d, vol_mult, bs = 0.015, 8.0, 0.78
            elif t.archetype == "pump_dump" and pump_at + 40 <= k < pump_at + 52:
                d, vol_mult, bs = -0.05, 10.0, 0.2
            if t.archetype == "honeypot":
                bs = 0.85                                   # nobody can sell
            if k == rug_at:
                p *= 0.04
                liq *= 0.02
                vol_mult, bs = 15.0, 0.1
            if self.null_edge:
                d = 0.0
                s = max(s, 0.03) if (t.archetype == "pump_dump" and pump_at <= k < pump_at + 52) else s
            p *= math.exp(d - s * s / 2 + s * rng.gauss(0, 1))
            liq *= math.exp(0.6 * (d - s * s / 2 + s * rng.gauss(0, 1))) if k != rug_at else 1.0
            t.price.append(p)
            t.liq.append(max(liq, 10.0))
            t.vol.append(max(liq * 0.002 * vol_mult * rng.uniform(0.5, 1.5), 0.0))
            t.buy_share.append(min(max(bs, 0.0), 1.0))

    def _idx(self, t: _Tok, ts: float) -> int | None:
        k = int((ts - t.launch_ts) / self.step)
        if k < 0 or k >= len(t.price):
            return None
        return k

    def _snap(self, t: _Tok, ts: float) -> TokenSnapshot | None:
        k = self._idx(t, ts)
        if k is None:
            return None
        n5 = max(1, int(300 / self.step))
        v5 = sum(t.vol[max(0, k - n5 + 1):k + 1])
        v1h = sum(t.vol[max(0, k - 12 * n5 + 1):k + 1])
        txns = int(v5 / 40) + 1
        buys = int(txns * t.buy_share[k])
        p5 = t.price[max(0, k - n5)]
        p1h = t.price[max(0, k - 12 * n5)]
        return TokenSnapshot(mint=t.mint, observed_ts=ts, source="synthetic", symbol=t.symbol, pair_address="pool_" + t.mint[:8],
                             dex_id=t.dex_id, quote_symbol="SOL", price_usd=t.price[k], liquidity_usd=t.liq[k], volume_m5=v5,
                             volume_h1=v1h, volume_h24=v1h * 12, buys_m5=buys, sells_m5=txns - buys, buys_h1=buys * 12,
                             sells_h1=(txns - buys) * 12, price_change_m5=(t.price[k] / p5 - 1) * 100,
                             price_change_h1=(t.price[k] / p1h - 1) * 100, fdv_usd=t.price[k] * 1e9, pair_created_ts=t.launch_ts)

    # ------------------------------------------------------------------ MarketData
    def discover(self) -> list[TokenSnapshot]:
        now = self.clock()
        out = [self._snap(t, now) for t in self.tokens.values() if 0 <= now - t.launch_ts <= 12 * 3600]
        return [s for s in out if s]

    def refresh(self, mints: list[str]) -> dict[str, TokenSnapshot]:
        now = self.clock()
        out = {}
        for m in mints:
            t = self.tokens.get(m)
            s = self._snap(t, now) if t else None
            if s:
                out[m] = s
        return out

    # ------------------------------------------------------------------ FactsSource / RouteSource
    def mint_facts(self, mint: str) -> MintFacts | None:
        t = self.tokens.get(mint)
        if not t:
            return None
        f = MintFacts(mint=mint, observed_ts=self.clock(), decimals=6, token_program="spl-token", mint_authority=None,
                      freeze_authority=None, extensions=[], transfer_fee_bps=0.0, top10_pct=self.rng.uniform(8, 28),
                      top1_pct=self.rng.uniform(1, 6), holder_count=self.rng.randint(300, 5000), jupiter_sus=False,
                      organic_score=self.rng.uniform(20, 90), provenance={"all": "synthetic"})
        if t.archetype == "mint_live":
            f.mint_authority = "Auth" + mint[:8]
        if t.archetype == "freeze_live":
            f.freeze_authority = "Frz" + mint[:8]
        if t.archetype == "fee_trap":
            f.token_program, f.extensions, f.transfer_fee_bps = "spl-token-2022", ["transferFeeConfig"], 1000.0
        if t.archetype == "rug" and self.rng.random() < 0.5:
            f.top10_pct, f.top1_pct = self.rng.uniform(40, 80), self.rng.uniform(15, 40)   # half of rugs are visibly concentrated
        return f

    def route_check(self, mint: str, size_usd: float) -> RouteCheck:
        t = self.tokens.get(mint)
        s = self._snap(t, self.clock()) if t else None
        if not s:
            return RouteCheck(ok=False, source="synthetic", error="no pool")
        if t.archetype == "honeypot":
            return RouteCheck(ok=False, buy_out_raw=1, source="synthetic", error="no sell route")
        r = s.liquidity_usd / 2
        impact = size_usd / (r + size_usd)
        fee = 0.0025
        back = size_usd * (1 - impact - fee) * (1 - impact - fee)
        if t.archetype == "fee_trap":
            back *= 0.81
        return RouteCheck(ok=True, buy_out_raw=int(size_usd / s.price_usd * 1e6), sell_back_usd=back,
                          round_trip_loss_bps=(1 - back / size_usd) * 1e4, price_impact_bps=impact * 1e4, fee_bps=25,
                          router="synthetic", source="synthetic")
