"""Event-driven reference prices: Binance spot WebSocket (`<pair>@trade`), no key, free.

Why only the reference leg: Jupiter exposes no public price WebSocket (Price v3 is request/response; Helius streams
are chain events, not prices), so the venue price is still polled. The WebSocket removes the 5-second staleness on
the Binance side and gives a measured exchange->local latency (trade event time `T` vs local receive time).

Reliability:
  - heartbeat: protocol pings every `ping_interval` s; a missing pong within `ping_timeout` s closes the connection;
  - stall detection: no message for `stall_sec` s (a silent half-open socket) forces a reconnect;
  - reconnect: exponential backoff with jitter, reset after the first message on a new connection;
  - freshness: `fresh_price(pair, max_age)` returns None for anything older than max_age, and the wrapper falls back
    to REST, so a dead stream can never feed a stale price into a decision.

Requires the `websockets` package (`pip install "tradebot[fast]"`). If it is missing or the connection fails, the
engine falls back to REST polling and says so once.
"""
from __future__ import annotations

import json
import logging
import random
import threading
import time

log = logging.getLogger("tradebot.fast.ws")


class BinanceTradeWS:
    def __init__(self, pairs: list[str], url: str = "wss://stream.binance.com:9443/stream", reconnect_sec: float = 1.0,
                 max_reconnect_sec: float = 60.0, ping_interval: float = 20.0, ping_timeout: float = 10.0,
                 stall_sec: float = 30.0, proxy: object = True, rng: random.Random | None = None):
        self.pairs = [p.lower() for p in pairs]
        self.url = url
        self.reconnect_sec = reconnect_sec
        self.max_reconnect_sec = max_reconnect_sec
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.stall_sec = stall_sec
        self.proxy = proxy
        self.rng = rng or random.Random()
        self.prices: dict[str, float] = {}             # PAIR -> last trade price
        self.last_ts: dict[str, float] = {}            # PAIR -> local receive time
        self.latency_ms: list[float] = []              # exchange event time -> local receive, last 1000
        self.events = 0
        self.errors = 0
        self.stalls = 0
        self.connects = 0
        self.reconnect_delays: list[float] = []
        self.last_message_ts: float | None = None
        self.connected = False
        self._failures = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def available() -> bool:
        try:
            import websockets  # noqa: F401
            return True
        except ImportError:
            return False

    def start(self) -> bool:
        if not self.available():
            log.warning("websockets package missing; reference prices stay on REST polling (pip install \"tradebot[fast]\")")
            return False
        self._thread = threading.Thread(target=self._run, name="binance-ws", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def handle(self, raw: str, recv_ts: float | None = None) -> None:
        """Parse one combined-stream frame. Public so tests can feed frames without a socket."""
        recv_ts = recv_ts or time.time()
        msg = json.loads(raw)
        d = msg.get("data", msg)
        self.last_message_ts = recv_ts
        if d.get("e") != "trade":
            return
        pair = str(d["s"]).upper()
        self.prices[pair] = float(d["p"])
        self.last_ts[pair] = recv_ts
        self.events += 1
        if "T" in d:
            self.latency_ms.append(recv_ts * 1000 - float(d["T"]))
            if len(self.latency_ms) > 1000:
                del self.latency_ms[:-1000]

    def fresh_price(self, pair: str, max_age_sec: float, now: float | None = None) -> float | None:
        now = now or time.time()
        pair = pair.upper()
        if pair in self.prices and now - self.last_ts.get(pair, 0.0) <= max_age_sec:
            return self.prices[pair]
        return None

    def next_delay(self) -> float:
        """Exponential backoff with jitter for the next reconnect attempt."""
        self._failures += 1
        d = min(self.max_reconnect_sec, self.reconnect_sec * 2 ** (self._failures - 1)) * (0.5 + self.rng.random())
        self.reconnect_delays.append(round(d, 3))
        del self.reconnect_delays[:-50]
        return d

    def _run(self) -> None:
        from websockets.sync.client import connect
        streams = "/".join(f"{p}@trade" for p in self.pairs)
        url = f"{self.url}?streams={streams}"
        while not self._stop.is_set():
            try:
                with connect(url, open_timeout=10, close_timeout=2, ping_interval=self.ping_interval,
                             ping_timeout=self.ping_timeout, proxy=self.proxy) as ws:
                    self.connected = True
                    self.connects += 1
                    log.info("binance ws connected (%d): %s", self.connects, streams)
                    first = True
                    while not self._stop.is_set():
                        try:
                            raw = ws.recv(timeout=self.stall_sec)
                        except TimeoutError:
                            self.stalls += 1
                            log.warning("binance ws: no message for %.1fs; reconnecting", self.stall_sec)
                            break
                        self.handle(raw)
                        if first:
                            self._failures = 0          # healthy again: reset backoff
                            first = False
            except Exception as e:  # noqa: BLE001 - any failure leads to a backed-off reconnect
                self.errors += 1
                log.warning("binance ws error: %s", e)
            self.connected = False
            if not self._stop.is_set():
                self._stop.wait(self.next_delay())
        self.connected = False

    def stats(self, now: float | None = None) -> dict:
        now = now or time.time()
        lat = sorted(self.latency_ms)
        return {"connected": self.connected, "connects": self.connects, "events": self.events, "errors": self.errors,
                "stalls": self.stalls, "last_reconnect_delays": self.reconnect_delays[-5:],
                "latency_p50_ms": round(lat[len(lat) // 2], 1) if lat else None,
                "latency_p95_ms": round(lat[int(len(lat) * 0.95) - 1], 1) if len(lat) > 1 else None,
                "age_sec": {p: round(now - t, 1) for p, t in self.last_ts.items()}}


class WSReferenceFeed:
    """Wraps a scanner Feed: reference prices come from the WebSocket when fresh, else from the wrapped REST feed."""

    def __init__(self, inner, ws: BinanceTradeWS, max_age_sec: float = 5.0):
        self.inner = inner
        self.ws = ws
        self.max_age_sec = max_age_sec
        self.jup = getattr(inner, "jup", None)

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def reference_prices(self, pairs: list[str]):
        from ..scanner.scanner import FeedResult
        now = time.time()
        fresh = {p: self.ws.prices[p] for p in pairs if p in self.ws.prices and now - self.ws.last_ts.get(p, 0) <= self.max_age_sec}
        if len(fresh) == len(pairs):
            return FeedResult(fresh, 0.0, 200, True, 0)
        res = self.inner.reference_prices(pairs)
        if res.ok and isinstance(res.data, dict):
            res.data.update(fresh)
        return res
