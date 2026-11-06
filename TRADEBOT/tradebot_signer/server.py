"""Loopback-only HTTP front end for SignerCore. Every request is HMAC-authenticated (signer_protocol)."""
from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tradebot.execution.signer_protocol import MAX_BODY, RequestVerifier, SignerAuthError, SignRefused

from .core import SignerCore

log = logging.getLogger("tradebot_signer")
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def make_handler(core: SignerCore, verifier: RequestVerifier):
    class Handler(BaseHTTPRequestHandler):
        server_version = "tradebot-signer"

        def log_message(self, fmt, *args):  # route through logging; bodies are never logged
            log.info("%s %s", self.address_string(), fmt % args)

        def _send(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> bytes | None:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                self._send(413, {"reason": "body too large"})
                return None
            return self.rfile.read(n) if n else b""

        def _auth(self, body: bytes) -> bool:
            try:
                verifier.verify(self.command, self.path, dict(self.headers.items()), body)
                return True
            except SignerAuthError as e:
                log.warning("rejected unauthenticated %s %s: %s", self.command, self.path, e)
                self._send(401, {"reason": str(e)})
                return False

        def do_GET(self):  # noqa: N802
            body = self._body()
            if body is None or not self._auth(body):
                return
            if self.path == "/v1/health":
                self._send(200, core.health())
            else:
                self._send(404, {"reason": "not found"})

        def do_POST(self):  # noqa: N802
            body = self._body()
            if body is None or not self._auth(body):
                return
            try:
                req = json.loads(body or b"{}")
            except ValueError:
                self._send(400, {"reason": "bad json"})
                return
            if self.path == "/v1/sign":
                try:
                    self._send(200, core.sign(req.get("client_order_id"), req.get("transaction"), req.get("claims")))
                except SignRefused as e:
                    self._send(403, {"refused": True, "reason": str(e)})
                except Exception as e:  # noqa: BLE001
                    log.exception("signer internal error")
                    self._send(500, {"reason": f"internal error: {type(e).__name__}"})
            elif self.path == "/v1/lookup":
                self._send(200, {"record": core.lookup(str(req.get("client_order_id") or ""))})
            else:
                self._send(404, {"reason": "not found"})
    return Handler


def serve(core: SignerCore, client_secret: bytes, host: str = "127.0.0.1", port: int = 8787) -> ThreadingHTTPServer:
    if host not in LOOPBACK:
        raise ValueError("the signer listens on the loopback interface only")
    srv = ThreadingHTTPServer((host, port), make_handler(core, RequestVerifier(client_secret)))
    srv.daemon_threads = True
    return srv
