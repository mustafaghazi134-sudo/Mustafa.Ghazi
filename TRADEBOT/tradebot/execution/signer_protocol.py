"""Wire protocol between the trading process and the signer service, and the trading process's client.

Requests are authenticated with HMAC-SHA256 over method, path, timestamp, a single-use nonce and the body hash,
using a shared client secret. The secret authorises *asking*; it does not authorise signing: the signer applies
its own activation, policy, caps and simulation to every request. The client never holds a private key.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

import httpx

ENV_SIGNER_SECRET = "TRADEBOT_SIGNER_SECRET"
MAX_BODY = 64 * 1024
MAX_SKEW_SEC = 30.0


class SignerAuthError(PermissionError):
    pass


class SignerUnavailable(RuntimeError):
    """The signer could not be reached or did not answer. The request may or may not have been processed."""


class SignRefused(PermissionError):
    """The signer answered and refused. Nothing was signed for this request."""


def _canonical(method: str, path: str, ts: str, nonce: str, body: bytes) -> bytes:
    return "\n".join([method.upper(), path, ts, nonce, hashlib.sha256(body).hexdigest()]).encode()


def auth_headers(secret: bytes, method: str, path: str, body: bytes, ts: float | None = None, nonce: str | None = None) -> dict:
    ts_s = f"{ts if ts is not None else time.time():.3f}"
    nonce = nonce or secrets.token_hex(16)
    mac = hmac.new(secret, _canonical(method, path, ts_s, nonce, body), hashlib.sha256).hexdigest()
    return {"X-TB-Ts": ts_s, "X-TB-Nonce": nonce, "X-TB-Sig": mac}


class RequestVerifier:
    """Signer side: timestamp window, single-use nonces, constant-time MAC comparison."""

    def __init__(self, secret: bytes, max_skew: float = MAX_SKEW_SEC, clock=time.time):
        self._secret = secret
        self.max_skew = max_skew
        self.clock = clock
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def verify(self, method: str, path: str, headers: dict, body: bytes) -> None:
        h = {k.lower(): v for k, v in headers.items()}
        ts, nonce, sig = h.get("x-tb-ts"), h.get("x-tb-nonce"), h.get("x-tb-sig")
        if not (ts and nonce and sig):
            raise SignerAuthError("missing authentication headers")
        try:
            t = float(ts)
        except ValueError as e:
            raise SignerAuthError("bad timestamp") from e
        now = self.clock()
        if abs(now - t) > self.max_skew:
            raise SignerAuthError("timestamp outside the allowed window")
        want = hmac.new(self._secret, _canonical(method, path, ts, nonce, body), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(want, sig):
            raise SignerAuthError("bad signature")
        with self._lock:
            for k, exp in list(self._seen.items()):
                if exp < now:
                    del self._seen[k]
            if nonce in self._seen:
                raise SignerAuthError("replayed nonce")
            self._seen[nonce] = now + 2 * self.max_skew


def load_client_secret(path: str = "") -> bytes:
    """From the file the owner copied from the signer home (`client_secret`), or env TRADEBOT_SIGNER_SECRET (hex)."""
    raw = ""
    if path:
        raw = Path(path).read_text().strip()
    if not raw:
        raw = os.environ.get(ENV_SIGNER_SECRET, "").strip()
    if not raw:
        raise SignerAuthError("no signer client secret configured ([live] signer_secret_file or env TRADEBOT_SIGNER_SECRET)")
    try:
        secret = bytes.fromhex(raw)
    except ValueError as e:
        raise SignerAuthError("signer client secret is not hex") from e
    if len(secret) < 32:
        raise SignerAuthError("signer client secret is shorter than 32 bytes")
    return secret


class SignerClient:
    """Trading-process side. Holds the signer URL and the client secret, never a key."""

    def __init__(self, url: str, secret: bytes, timeout: float = 10.0, transport: httpx.BaseTransport | None = None):
        if not (url.startswith("http://127.0.0.1") or url.startswith("http://localhost") or url.startswith("http://[::1]")):
            raise ValueError("the signer must be reached on the loopback interface only")
        self.url = url.rstrip("/")
        self._secret = secret
        self.http = httpx.Client(timeout=timeout, transport=transport)

    def __repr__(self) -> str:
        return f"SignerClient({self.url})"

    def _call(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = json.dumps(payload, sort_keys=True).encode() if payload is not None else b""
        headers = {"Content-Type": "application/json", **auth_headers(self._secret, method, path, body)}
        try:
            r = self.http.request(method, self.url + path, content=body, headers=headers)
        except httpx.HTTPError as e:
            raise SignerUnavailable(f"signer unreachable: {type(e).__name__}") from e
        try:
            data = r.json()
        except ValueError as e:
            raise SignerUnavailable(f"signer returned HTTP {r.status_code} without JSON") from e
        if r.status_code == 403 and data.get("refused"):
            raise SignRefused(data.get("reason", "refused"))
        if r.status_code == 401:
            raise SignerAuthError(data.get("reason", "unauthorised"))
        if r.status_code != 200:
            raise SignerUnavailable(f"signer HTTP {r.status_code}: {data.get('reason', '')}")
        return data

    def health(self) -> dict:
        return self._call("GET", "/v1/health")

    def sign(self, client_order_id: str, tx_b64: str, claims: dict) -> dict:
        """-> {"signed_tx", "signature"}. Raises SignRefused (nothing signed) or SignerUnavailable (unknown)."""
        return self._call("POST", "/v1/sign", {"client_order_id": client_order_id, "transaction": tx_b64, "claims": claims})

    def lookup(self, client_order_id: str) -> dict | None:
        """What the signer signed for this client order id, if anything: {"signature", "signed_tx", "ts"}."""
        res = self._call("POST", "/v1/lookup", {"client_order_id": client_order_id})
        return res.get("record")
