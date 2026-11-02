"""The scanner loop and its data feed. READ-ONLY, PAPER ONLY: no wallet, no signing, no `taker`, no POST to /execute."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

import httpx

from ..config import Settings, jupiter_endpoints
from .costs import Quote, net_edge_bps, realized_bps, round_trip_cost
from .filters import FilterLimits, TokenFacts, cost_reasons, staleness_reasons, token_reasons
from .signals import BarBuilder, Candidate, evaluate_all
from .store import ScannerStore

log = logging.getLogger("tradebot.scanner")
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOLANA_SLOT_SEC = 0.4


@dataclass
class FeedResult:
    data: Any
    latency_ms: float
    status: int
    ok: bool
    nbytes: int = 0


class Feed(Protocol):
    def prices(self, mints: list[str]) -> FeedResult: ...                 # -> {mint: {"usdPrice":..,"blockId":..}}
    def reference_prices(self, pairs: list[str]) -> FeedResult: ...       # -> {pair: price}
    def slot(self) -> FeedResult: ...                                     # -> int
    def token_facts(self, mints: list[str]) -> FeedResult: ...            # -> [tokens v2 dicts]
    def discover(self, category: str, interval: str) -> FeedResult: ...  # -> [tokens v2 dicts]
    def order(self, in_mint: str, out_mint: str, amount_raw: int, slippage_bps: int) -> FeedResult: ...  # -> order payload


class LiveFeed:
    """httpx against api.jup.ag, Binance and a Solana RPC. Every call is timed; nothing is ever signed or sent."""

    def __init__(self, settings: Settings):
        self.s = settings
        self.jup = jupiter_endpoints(settings.data, settings.execution)
        if not self.jup.has_key:
            raise RuntimeError("JUPITER_API_KEY missing: the scanner needs a (free) key from developers.jup.ag")
        self.client = httpx.Client(timeout=settings.data.jupiter_quote_timeout_sec)

    def _get(self, url: str, params: dict | None = None, headers: dict | None = None) -> FeedResult:
        t0 = time.perf_counter()
        try:
            r = self.client.get(url, params=params, headers=headers)
            lat = (time.perf_counter() - t0) * 1000
            ok = r.status_code == 200
            return FeedResult(r.json() if ok else None, lat, r.status_code, ok, len(r.content))
        except (httpx.HTTPError, ValueError) as e:
            return FeedResult(None, (time.perf_counter() - t0) * 1000, 0, False, 0) if not isinstance(e, ValueError) else \
                FeedResult(None, (time.perf_counter() - t0) * 1000, 200, False, 0)

    def _post_rpc(self, method: str, params: list) -> FeedResult:
        t0 = time.perf_counter()
        try:
            r = self.client.post(self.s.execution.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            lat = (time.perf_counter() - t0) * 1000
            body = r.json() if r.status_code == 200 else {}
            return FeedResult(body.get("result"), lat, r.status_code, r.status_code == 200 and "result" in body, len(r.content))
        except (httpx.HTTPError, ValueError):
            return FeedResult(None, (time.perf_counter() - t0) * 1000, 0, False, 0)

    def prices(self, mints: list[str]) -> FeedResult:
        return self._get(self.jup.price, params={"ids": ",".join(mints[:50])}, headers=self.jup.headers)

    def reference_prices(self, pairs: list[str]) -> FeedResult:
        res = self._get(f"{self.s.data.binance_base_url}/api/v3/ticker/price",
                        params={"symbols": json.dumps(sorted(set(pairs)), separators=(",", ":"))})
        if res.ok and isinstance(res.data, list):
            res.data = {d["symbol"]: float(d["price"]) for d in res.data if "symbol" in d}
        return res

    def slot(self) -> FeedResult:
        return self._post_rpc("getSlot", [{"commitment": "processed"}])

    def token_facts(self, mints: list[str]) -> FeedResult:
        return self._get(f"{self.jup.tokens}/search", params={"query": ",".join(mints[:100])}, headers=self.jup.headers)

    def discover(self, category: str, interval: str) -> FeedResult:
        return self._get(f"{self.jup.tokens}/{category}/{interval}", headers=self.jup.headers)

    def order(self, in_mint: str, out_mint: str, amount_raw: int, slippage_bps: int) -> FeedResult:
        # no `taker` => Jupiter returns a quote with transaction null; nothing can be executed from this response
        return self._get(f"{self.jup.swap}/order", headers=self.jup.headers,
                         params={"inputMint": in_mint, "outputMint": out_mint, "amount": str(amount_raw), "slippageBps": slippage_bps})


class OpportunityScanner:
    def __init__(self, settings: Settings, store: ScannerStore, feed: Feed, clock: Callable[[], float] = time.time):
        self.s = settings
        self.sc = settings.scanner
        self.store = store
        self.feed = feed
        self.clock = clock
        self.limits = FilterLimits(min_liquidity_usd=settings.risk.min_liquidity_usd, min_volume_24h_usd=settings.risk.min_volume_24h_usd,
                                   min_organic_score=self.sc.min_organic_score, require_verified=self.sc.require_verified,
                                   max_quote_age_sec=self.sc.max_price_age_sec, max_price_slots_behind=self.sc.max_price_slots_behind,
                                   max_round_trip_bps=self.sc.max_round_trip_bps, min_net_edge_bps=self.sc.min_net_edge_bps,
                                   max_single_quote_impact_bps=settings.risk.max_slippage_bps)
        self.bars = BarBuilder()
        self.symbols: dict[str, str] = {s: m for s, m in settings.universe.mints.items() if s in settings.universe.symbols}
        self.symbols.update({f"X{i}": m for i, m in enumerate(self.sc.extra_mints)})
        self.decimals: dict[str, int] = dict(settings.universe.decimals)
        self.facts: dict[str, TokenFacts] = {}
        self.last_facts_ts = 0.0
        self.last_probe_ts = 0.0
        self.last_discover_ts = 0.0
        self.last_slot: int | None = None
        self.last_slot_ts = 0.0
        self.req_log: list[float] = []
        self.cooldown: dict[tuple[str, str], float] = {}
        self.polls = 0

    # ---- helpers ------------------------------------------------------------------------------
    def _record(self, name: str, res: FeedResult) -> None:
        self.req_log.append(self.clock())
        self.store.log_source(name, res.ok, res.status, res.latency_ms, res.nbytes, ts=self.clock())

    def _requests_last_minute(self) -> int:
        now = self.clock()
        self.req_log = [t for t in self.req_log if now - t < 60]
        return len(self.req_log)

    def _mint(self, sym: str) -> str:
        return self.symbols[sym]

    def _sym_for(self, mint: str) -> str | None:
        return next((s for s, m in self.symbols.items() if m == mint), None)

    def refresh_facts(self) -> None:
        res = self.feed.token_facts(list(self.symbols.values()))
        self._record("jupiter_tokens_v2", res)
        if not res.ok or not isinstance(res.data, list):
            return
        for t in res.data:
            f = TokenFacts.from_tokens_v2(t)
            sym = self._sym_for(f.mint)
            if sym:
                self.facts[sym] = f
                if f.decimals is not None:
                    self.decimals[sym] = int(f.decimals)
                if sym.startswith("X") and f.symbol:          # name discovered mints by their ticker
                    self.symbols[f.symbol.upper()] = self.symbols.pop(sym); self.facts[f.symbol.upper()] = self.facts.pop(sym)
                    if sym in self.decimals:
                        self.decimals[f.symbol.upper()] = self.decimals.pop(sym)
        self.last_facts_ts = self.clock()

    def discover_universe(self) -> None:
        """Optionally widen the universe with Jupiter's most-traded verified tokens (still subject to every filter)."""
        if not self.sc.discover_category:
            return
        res = self.feed.discover(self.sc.discover_category, self.sc.discover_interval)
        self._record("jupiter_tokens_v2_discover", res)
        if not res.ok or not isinstance(res.data, list):
            return
        added = 0
        for t in res.data:
            f = TokenFacts.from_tokens_v2(t)
            if not f.mint or f.mint in self.symbols.values() or f.mint == USDC:
                continue
            if token_reasons(f, self.limits):
                continue
            if len(self.symbols) >= self.sc.max_universe:
                break
            self.symbols[f.symbol.upper() or f.mint[:6]] = f.mint
            self.facts[f.symbol.upper() or f.mint[:6]] = f
            if f.decimals is not None:
                self.decimals[f.symbol.upper() or f.mint[:6]] = int(f.decimals)
            added += 1
        self.last_discover_ts = self.clock()
        if added:
            log.info("discover: +%d tokens, universe now %d", added, len(self.symbols))

    def quotes_for(self, sym: str, size_usd: float) -> tuple[Quote, Quote, float] | None:
        mint, dec = self._mint(sym), self.decimals.get(sym)
        if dec is None:
            return None
        slip = self.s.execution.live_slippage_bps
        rb = self.feed.order(USDC, mint, int(round(size_usd * 1e6)), slip); self._record("jupiter_order_quote", rb)
        if not rb.ok or not rb.data or rb.data.get("error"):
            return None
        qb = Quote.from_order(rb.data, rb.latency_ms, self.clock())
        rs = self.feed.order(mint, USDC, qb.out_amount, slip); self._record("jupiter_order_quote", rs)
        if not rs.ok or not rs.data or rs.data.get("error"):
            return None
        qs = Quote.from_order(rs.data, rs.latency_ms, self.clock())
        return qb, qs, rb.latency_ms + rs.latency_ms

    def probe_costs(self) -> None:
        for sym in self.sc.probe_symbols or list(self.symbols)[:3]:
            if sym not in self.symbols:
                continue
            for size in self.sc.probe_sizes_usd:
                q = self.quotes_for(sym, size)
                if q is None:
                    continue
                qb, qs, lat = q
                c = round_trip_cost(qb, qs, size, self.decimals[sym]).row()
                c["router"], c["route"] = qb.router, qb.route
                self.store.log_cost_probe(self.clock(), sym, c, lat)
        self.last_probe_ts = self.clock()

    # ---- one poll -----------------------------------------------------------------------------
    def poll(self) -> dict:
        now = self.clock()
        self.polls += 1
        if now - self.last_facts_ts >= self.sc.facts_interval_sec:
            self.refresh_facts()
        if self.sc.discover_category and now - self.last_discover_ts >= self.sc.discover_interval_sec:
            self.discover_universe()
        mints = list(self.symbols.values())
        pres = self.feed.prices(mints); self._record("jupiter_price_v3", pres)
        pairs = {s: self.s.universe.candle_symbols[s] for s in self.symbols if s in self.s.universe.candle_symbols}
        rres = self.feed.reference_prices(list(pairs.values())) if pairs else None
        if rres is not None:
            self._record("binance_ticker_price", rres)
        slot = None
        if self.sc.check_slot and now - self.last_slot_ts >= self.sc.slot_interval_sec:
            sres = self.feed.slot(); self._record("solana_rpc_getSlot", sres)
            if sres.ok and isinstance(sres.data, int):
                self.last_slot, self.last_slot_ts = sres.data, now
        if self.last_slot is not None:
            slot = self.last_slot + int((now - self.last_slot_ts) / SOLANA_SLOT_SEC)
        if not pres.ok or not isinstance(pres.data, dict):
            return {"ok": False, "reason": f"price feed status {pres.status}"}
        refs = rres.data if (rres is not None and rres.ok and isinstance(rres.data, dict)) else {}
        opened = rejected = 0
        for sym, mint in list(self.symbols.items()):
            p = pres.data.get(mint)
            if not p or not p.get("usdPrice"):
                continue                                       # unpriced => fail closed (skill: missing mint = no reliable price)
            price = float(p["usdPrice"]); block = p.get("blockId")
            ref = refs.get(pairs.get(sym, "")) if pairs.get(sym) else None
            self.store.log_tick(now, sym, price, block, slot, ref, pres.latency_ms)
            self.bars.add(sym, now, price)
            for cand in evaluate_all(sym, self.bars.bars(sym), price, ref):
                key = (sym, cand.signal)
                if self.cooldown.get(key, 0) > now:
                    continue
                self.cooldown[key] = now + cand.horizon_sec
                status = self._handle_candidate(cand, price, block, slot, now)
                opened += status == "open"; rejected += status != "open"
        self._settle(now)
        if now - self.last_probe_ts >= self.sc.probe_interval_sec and self._requests_last_minute() < self.sc.max_requests_per_min - 10:
            self.probe_costs()
        self.store.set("last_poll", {"ts": now, "polls": self.polls, "universe": len(self.symbols), "req_last_min": self._requests_last_minute()})
        return {"ok": True, "opened": opened, "rejected": rejected, "universe": len(self.symbols), "req_last_min": self._requests_last_minute()}

    def _handle_candidate(self, cand: Candidate, price: float, block: int | None, slot: int | None, now: float) -> str:
        sym = cand.symbol
        facts = self.facts.get(sym)
        reasons = token_reasons(facts, self.limits) if facts else ["no_token_facts"]
        slots_behind = (slot - block) if (slot is not None and block is not None) else None
        reasons += staleness_reasons(None, slots_behind, self.limits)
        detail = {**cand.detail, "venue_price": price, "block_id": block, "slot": slot}
        cost = None; edge = None; entry_px = None; qty = None
        if not reasons:
            if self._requests_last_minute() > self.sc.max_requests_per_min - 2:
                reasons.append("request_budget")
            else:
                q = self.quotes_for(sym, self.sc.size_usd)
                if q is None:
                    reasons.append("quote_unavailable")
                else:
                    qb, qs, _ = q
                    rtc = round_trip_cost(qb, qs, self.sc.size_usd, self.decimals[sym])
                    cost = rtc.row(); cost["router"], cost["route"] = qb.router, qb.route
                    edge = net_edge_bps(cand.expected_move_bps, rtc, self.sc.adverse_buffer_bps)
                    reasons += cost_reasons(rtc.worst_case_bps, rtc.buy_impact_bps, rtc.sell_impact_bps, edge, self.limits)
                    entry_px, qty = rtc.buy_price_worst_usd, rtc.token_qty * (rtc.buy_price_usd / rtc.buy_price_worst_usd)
        status = "open" if not reasons else "rejected"
        self.store.add_candidate(now, sym, cand.signal, cand.expected_move_bps, cand.horizon_sec, self.sc.size_usd, detail, cost, edge, status, reasons,
                                 entry_px if status == "open" else None, qty if status == "open" else None)
        if status == "open":
            log.info("OPEN  %s %s exp %+.0f bps cost(worst) %.0f bps edge %+.0f bps @ %.6g", sym, cand.signal, cand.expected_move_bps, cost["worst_case_bps"], edge, entry_px)
        else:
            log.debug("REJECT %s %s: %s", sym, cand.signal, ",".join(reasons))
        return status

    def _settle(self, now: float) -> None:
        for r in self.store.pending("open"):
            if now < r["ts"] + r["horizon_sec"]:
                continue
            sym = r["symbol"]
            dec = self.decimals.get(sym)
            if dec is None or sym not in self.symbols:
                self.store.close_candidate(r["id"], now, None, None, None, "closed_unpriced"); continue
            res = self.feed.order(self._mint(sym), USDC, int(round(r["token_qty"] * 10 ** dec)), self.s.execution.live_slippage_bps)
            self._record("jupiter_order_quote", res)
            if not res.ok or not res.data or res.data.get("error"):
                if now > r["ts"] + r["horizon_sec"] + 600:
                    self.store.close_candidate(r["id"], now, None, None, None, "closed_unpriced")
                continue
            q = Quote.from_order(res.data, res.latency_ms, now)
            rb = realized_bps(r["entry_price_usd"], q, r["token_qty"], size_usd=r["size_usd"])
            self.store.close_candidate(r["id"], now, {"out": q.out_amount, "min_out": q.min_out_amount, "impact_pct": q.price_impact_pct, "router": q.router},
                                       rb, None, "closed_win" if rb > 0 else "closed_loss")
            log.info("CLOSE %s %s realized %+.0f bps", sym, r["signal"], rb)
        # shadow-mark rejected candidates at horizon so 'missed' can be counted (mark uses the venue price move minus the
        # measured worst-case cost when quotes existed, else the latest probe for that symbol, else a default)
        for r in self.store.pending("rejected"):
            if now < r["ts"] + r["horizon_sec"]:
                continue
            sym = r["symbol"]
            px_now = self.bars.last_price(sym)
            d = json.loads(r["detail"] or "{}")
            px_then = d.get("venue_price")
            if not px_now or not px_then:
                self.store.close_candidate(r["id"], now, None, None, None, "rejected_unmarked"); continue
            cost_bps = None
            if r["cost"]:
                cost_bps = json.loads(r["cost"]).get("worst_case_bps")
            if cost_bps is None:
                probe = self.store.conn.execute("SELECT worst_case_bps FROM cost_probes WHERE symbol=? AND size_usd=? ORDER BY ts DESC LIMIT 1",
                                                (sym, r["size_usd"])).fetchone()
                cost_bps = probe[0] if probe else self.sc.default_round_trip_bps
            mark = round((px_now / px_then - 1) * 1e4 - cost_bps, 1)
            self.store.close_candidate(r["id"], now, None, None, mark, "rejected_marked")

    # ---- run ----------------------------------------------------------------------------------
    def run(self, minutes: float | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        start = self.clock()
        log.info("scanner start: %d symbols, poll %.0fs, size $%.0f, min edge %.0f bps, PAPER/read-only", len(self.symbols), self.sc.poll_sec,
                 self.sc.size_usd, self.sc.min_net_edge_bps)
        while True:
            t0 = self.clock()
            try:
                out = self.poll()
                if self.polls % 12 == 0:
                    log.info("poll %d: %s", self.polls, out)
            except Exception:  # noqa: BLE001
                log.exception("poll failed")
            if minutes is not None and self.clock() - start >= minutes * 60:
                break
            sleep(max(0.0, self.sc.poll_sec - (self.clock() - t0)))


# ---- report -------------------------------------------------------------------------------------
def scan_report(store: ScannerStore, since_hours: float = 24.0, min_edge_bps: float = 20.0) -> str:
    since = time.time() - since_hours * 3600
    L = [f"=== scanner report (last {since_hours:g} h, read-only paper scanner) ==="]
    last = store.get("last_poll")
    L.append(f"last poll: {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(last['ts'])) if last else 'never'} UTC  polls={last['polls'] if last else 0}  "
             f"universe={last['universe'] if last else 0}  requests/min={last['req_last_min'] if last else 0}")
    L.append("-- data sources: latency / reliability / achieved rate")
    for d in store.source_stats(since):
        L.append(f"  {d['source']:<28} n={d['n']:<6} ok={100-(d['error_rate_pct'] or 0):5.1f}%  429s={d['rate_limited']:<4} avg {d['avg_ms']} ms  p95 {d['p95_ms']} ms  max {round(d['max_ms'] or 0)} ms  {d['requests_per_min']} req/min")
    L.append("-- price refresh actually achieved")
    for sym, d in store.refresh_stats(since).items():
        L.append(f"  {sym:<8} ticks={d['ticks']:<6} median gap {d['median_gap_sec']} s  price changed {d['price_changed_pct']}% of polls  block advanced {d['block_advanced_pct']}%")
    L.append("-- round-trip cost probes (USDC->token->USDC, two read-only quotes, incl. 2 network fees)")
    for d in store.cost_probe_stats(since):
        L.append(f"  {d['symbol']:<8} ${d['size_usd']:<8} n={d['n']:<4} expected {d['exp_bps']} bps  worst-case avg {d['worst_bps']} / max {d['worst_max']} bps  jup fee {d['fee_bps']} bps  quotes {d['latency_ms']} ms")
    cs = store.candidate_stats(since, min_edge_bps)
    L.append("-- candidates (hypothetical; nothing executed)")
    for sig, d in cs["by_signal"].items():
        L.append(f"  {sig:<18} candidates={d['candidates']:<5} opened={d['opened']:<4} closed={d['closed']:<4} win rate {d['win_rate_pct']}%  avg realized {d['avg_realized_bps']} bps  "
                 f"false positives={d['false_positives']}  rejected={d['rejected']}  missed(profitable after cost)={d['missed']}")
    if cs["rejection_reasons"]:
        L.append("  rejection reasons: " + ", ".join(f"{k}={v}" for k, v in list(cs["rejection_reasons"].items())[:12]))
    return "\n".join(L)
