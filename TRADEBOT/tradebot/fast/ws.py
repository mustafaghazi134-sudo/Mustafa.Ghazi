"""Optional event-driven reference prices: Binance spot WebSocket (`<pair>@trade`), no key, free.

Why only the reference leg: Jupiter exposes no public price WebSocket (Price v3 is request/response; Helius streams
are chain events, not prices), so the venue price is still polled. The WebSocket removes the 5-second staleness on
the Binance side and gives a measured exchange->local latency (trade event time `T` vs local receive time).

Requires the `websockets` package (`pip install "tradebot[fast]"`). If it is missing or the connection fails, the
engine falls back to REST polling and says so once.
"""
from __future__ import annotations

import json
import logging
import threading
import time

log = logging.getLogger("tradebot.fast.ws")


class BinanceTradeWS:
    def __init__(self, pairs: list[str], url: str = "wss://stream.binance.com:9443/stream", reconnect_sec: float = 5.0):
        self.pairs = [p.lower() for p in pairs]
        self.url = url
        self.reconnect_sec = reconnect_sec
        self.prices: dict[str, float] = {}             # PAIR -> last trade price
        self.last_ts: dict[str, float] = {}            # PAIR -> local receive time
        self.latency_ms: list[float] = []              # exchange event time -> local receive, last 1000
        self.events = 0
        self.errors = 0
        self.connected = False
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

    def handle(self, raw: str, recv_ts: float | None = None) -> None:
        """Parse one combined-stream frame. Public so tests can feed frames without a socket."""
        recv_ts = recv_ts or time.time()
        msg = json.loads(raw)
        d = msg.get("data", msg)
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

    def _run(self) -> None:
        from websockets.sync.client import connect
        streams = "/".join(f"{p}@trade" for p in self.pairs)
        url = f"{self.url}?streams={streams}"
        while not self._stop.is_set():
            try:
                with connect(url, open_timeout=10, close_timeout=2) as ws:
                    self.connected = True
                    log.info("binance ws connected: %s", streams)
                    while not self._stop.is_set():
                        self.handle(ws.recv(timeout=30))
            except Exception as e:  # noqa: BLE001
                self.errors += 1
                self.connected = False
                log.warning("binance ws error (%s); reconnecting in %.0fs", e, self.reconnect_sec)
                self._stop.wait(self.reconnect_sec)
        self.connected = False

    def stats(self) -> dict:
        lat = sorted(self.latency_ms)
        return {"connected": self.connected, "events": self.events, "errors": self.errors,
                "latency_p50_ms": round(lat[len(lat) // 2], 1) if lat else None,
                "latency_p95_ms": round(lat[int(len(lat) * 0.95) - 1], 1) if len(lat) > 1 else None,
                "age_sec": {p: round(time.time() - t, 1) for p, t in self.last_ts.items()}}


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
