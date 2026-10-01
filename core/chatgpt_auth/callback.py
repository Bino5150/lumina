"""
SUBSCRIPTION-PLAN-BACKENDS-01C -- single-attempt loopback callback listener.

Shape fixed by the vendor contract: HTTP on 127.0.0.1 (never localhost,
never 0.0.0.0/LAN), path exactly /auth/callback, port chosen per attempt
(only the port may vary). One listener per authorization attempt; it binds
before the browser opens and is closed when the attempt reaches any
terminal outcome, is cancelled, or expires.

Validation order (an unrelated or forged loopback request must never
consume the pending attempt):
  1. method GET, Host header exactly 127.0.0.1:<port>, path exactly
     /auth/callback, attempt not already settled  -- else 404, not consumed;
  2. exactly one `state`, constant-time equal to this attempt's state
                                                 -- else 400, not consumed;
  3. the result is settled exactly once: an OAuth `error`, or exactly one
     `code` with the issued `client_id` rules applied. A second callback
     after settlement gets 404.

The result is only "the browser came back with a code for THIS attempt".
It never makes a session READY: the code still has to be exchanged with
this attempt's PKCE verifier and the returned ID token verified.

The stdlib handler's request logging is disabled: the request line carries
the authorization code and state.

The browser is never launched with the authorize URL itself (it carries the
state, nonce, PKCE challenge and host ID, and a launcher's process arguments
are readable by other local users). It is launched with a one-shot
/auth/start/<random> URL on this listener, which answers exactly once with a
302 to the authorize URL and 404 afterwards. Requests are served on worker
threads so one stalled local connection cannot block the real callback.
"""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import http.server
import secrets
import socketserver
import threading
import urllib.parse
from typing import Optional

from core.chatgpt_auth.oauth import CALLBACK_ERROR_CODES, CALLBACK_PATH
from core.chatgpt_auth.store import BOOTSTRAP_CLIENT_ID, _client_id_ok

LOOPBACK_HOST = "127.0.0.1"
_REQUEST_TIMEOUT_SECONDS = 10

_HISTORY_SCRIPT = 'history.replaceState(null, "", "/auth/complete");'
_SCRIPT_HASH = base64.b64encode(hashlib.sha256(_HISTORY_SCRIPT.encode()).digest()).decode()
_DONE_PAGE = (
    "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><title>Return to Lumina</title>"
    "<style>body{font:16px system-ui;max-width:32rem;margin:18vh auto;padding:24px}</style>"
    "<h1>Return to Lumina</h1><p>Lumina will finish checking your ChatGPT connection. "
    "You can close this tab.</p>"
    f"<script>{_HISTORY_SCRIPT}</script></html>"
).encode("utf-8")


@dataclasses.dataclass
class CallbackResult:
    """Exactly one of (code, error) is set."""
    code: Optional[str] = dataclasses.field(default=None, repr=False)
    client_id: Optional[str] = dataclasses.field(default=None, repr=False)
    error: Optional[str] = None          # categorical: OAuth error code or a local rejection class


class CallbackTimeout(Exception):
    pass


class CallbackCancelled(Exception):
    pass


_MAX_CONCURRENT_REQUESTS = 8


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = False
    daemon_threads = True
    block_on_close = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._slots = threading.BoundedSemaphore(_MAX_CONCURRENT_REQUESTS)

    def process_request(self, request, client_address):
        # A local flood holds at most a few threads; excess connections are dropped.
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class LoopbackCallback:
    """One listener for one attempt. `expected_client_id` is the issued
    client ID for a reauthorization (None for a first registration)."""

    def __init__(self, *, state: str, expected_client_id: Optional[str], port: int = 0):
        self._state = state.encode("utf-8")
        self._expected_client_id = expected_client_id
        self._settled = threading.Event()
        self._closed = threading.Event()
        self._result: Optional[CallbackResult] = None
        self._lock = threading.Lock()
        self._start_path: Optional[str] = None
        self._spent_start_path: Optional[str] = None
        self._authorize_url: Optional[str] = None
        # Set when the one-shot start URL is requested a second time: someone
        # other than the browser fetched it first and learned this attempt's
        # state. Such an attempt must not commit anything.
        self.compromised = False
        self._server = _Server((LOOPBACK_HOST, port), self._make_handler())
        self._server.timeout = 0.5
        self.port = self._server.server_address[1]
        self.redirect_uri = f"http://{LOOPBACK_HOST}:{self.port}{CALLBACK_PATH}"
        self._thread = threading.Thread(target=self._serve, name="chatgpt-auth-callback", daemon=True)
        self._thread.start()

    def arm_start(self, authorize_url: str) -> str:
        """Returns the one-shot local URL to hand to the browser."""
        with self._lock:
            self._start_path = f"/auth/start/{secrets.token_urlsafe(24)}"
            self._authorize_url = authorize_url
            return f"http://{LOOPBACK_HOST}:{self.port}{self._start_path}"

    def _take_start(self, method: str, host: Optional[str], target: str) -> Optional[str]:
        """The authorize URL, exactly once, for the armed start path."""
        path = urllib.parse.urlsplit(target).path
        with self._lock:
            if self._spent_start_path is not None and path == self._spent_start_path:
                self.compromised = True
                return None
            if (method != "GET" or host != f"{LOOPBACK_HOST}:{self.port}" or self._settled.is_set()
                    or self._start_path is None or path != self._start_path):
                return None
            url, self._authorize_url = self._authorize_url, None
            self._spent_start_path, self._start_path = self._start_path, None
            return url

    # -- lifecycle -----------------------------------------------------------

    def _serve(self) -> None:
        try:
            self._server.serve_forever(poll_interval=0.1)
        finally:
            self._server.server_close()

    def close(self) -> None:
        """Stop serving and release the port. Idempotent; safe from any thread."""
        if self._closed.is_set():
            return
        self._closed.set()
        with self._lock:
            self._authorize_url = self._start_path = None
        self._settle(CallbackResult(error="cancelled"))
        self._server.shutdown()
        self._thread.join(timeout=5)

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def wait(self, timeout: float) -> CallbackResult:
        if not self._settled.wait(timeout):
            raise CallbackTimeout()
        result = self._result
        if result is not None and result.error == "cancelled" and self._closed.is_set():
            raise CallbackCancelled()
        return result

    def _settle(self, result: CallbackResult) -> bool:
        with self._lock:
            if self._settled.is_set():
                return False
            self._result = result
            self._settled.set()
            return True

    # -- request handling ----------------------------------------------------

    def _evaluate(self, method: str, host: Optional[str], target: str):
        """Returns (http_status, result_or_None). result is None when the
        request must not consume the attempt."""
        if self._settled.is_set():
            return 404, None
        if method != "GET" or host != f"{LOOPBACK_HOST}:{self.port}":
            return 404, None
        parts = urllib.parse.urlsplit(target)
        if parts.path != CALLBACK_PATH:
            return 404, None
        query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
        states = query.get("state", [])
        if len(states) != 1 or not hmac.compare_digest(states[0].encode("utf-8"), self._state):
            return 400, None

        errors = query.get("error", [])
        if errors:
            code = errors[0] if len(errors) == 1 and errors[0] in CALLBACK_ERROR_CODES else "callback_error"
            return 200, CallbackResult(error=code)

        codes = query.get("code", [])
        returned = query.get("client_id", [])
        if len(codes) != 1 or not codes[0] or len(returned) > 1:
            return 200, CallbackResult(error="callback_malformed")
        returned_id = returned[0] if returned else None
        expected = self._expected_client_id
        if expected is None:
            # First registration: the issued client ID must come back here.
            if returned_id is None:
                return 200, CallbackResult(error="registration_incomplete")
            client_id = returned_id
        else:
            # Reauthorization may omit it; a different one is never accepted.
            if returned_id is not None and returned_id != expected:
                return 200, CallbackResult(error="client_id_mismatch")
            client_id = expected
        if client_id == BOOTSTRAP_CLIENT_ID or not _client_id_ok(client_id):
            return 200, CallbackResult(error="registration_incomplete")
        return 200, CallbackResult(code=codes[0], client_id=client_id)

    def _make_handler(self):
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            timeout = _REQUEST_TIMEOUT_SECONDS
            server_version = "Lumina"
            sys_version = ""

            def log_message(self, format, *args):  # noqa: A002 -- stdlib signature
                return  # request lines carry the code and state: never log them

            def _respond(self, status: int, body: bytes = b"") -> None:
                self.send_response(status)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header(
                    "Content-Security-Policy",
                    f"default-src 'none'; script-src 'sha256-{_SCRIPT_HASH}'; "
                    "style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'",
                )
                self.send_header("Content-Type", "text/html; charset=utf-8" if body else "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _handle(self, method: str) -> None:
                location = owner._take_start(method, self.headers.get("Host"), self.path)
                if location is not None:
                    self.send_response(302)
                    self.send_header("Location", location)
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status, result = owner._evaluate(method, self.headers.get("Host"), self.path)
                if result is None:
                    self._respond(status, b"")
                    return
                if not owner._settle(result):
                    self._respond(404, b"")
                    return
                self._respond(200, _DONE_PAGE)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_HEAD(self):
                self._handle("HEAD")

        return Handler
