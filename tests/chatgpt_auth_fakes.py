"""
SUBSCRIPTION-PLAN-BACKENDS-01C test double: a local, deterministic stand-in
for OpenAI's Sign in with ChatGPT auth service, served over real HTTP on
127.0.0.1 so the product code's requests/JWKS/callback paths run unmodified.

Contract it enforces (developers.openai.com/siwc, vetted 2026-10-01):
  * discovery document with issuer/authorize/token/revoke/jwks endpoints;
  * RS256 ID tokens signed by a test-only RSA key published in its JWKS;
  * authorization codes bound to client_id + redirect_uri + PKCE S256
    challenge + nonce, single use;
  * first registration only via client_id=dynamic_agent_client, which mints
    a fresh issued client ID (never accepted at the token endpoint);
  * refresh tokens rotate on every refresh; re-presenting a retired one is
    `refresh_token_reused` and kills the whole token family (the behavior
    that makes unserialized refreshes destructive);
  * revocation: HTTP 200 for any token (including already-invalid ones).

Every token/code it mints starts with "CANARY-" (or is a JWT whose payload
carries "canary") so tests can scan persistence/log surfaces for leaks.
Nothing here is, or resembles, a production credential.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import itertools
import json
import socketserver
import threading
import time
import urllib.parse
import uuid

import jwt
import requests
from cryptography.hazmat.primitives.asymmetric import rsa

PLAN_SCOPE = "chatgpt.tokens.use.direct"
FULL_SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
NO_PLAN_SCOPES = "openid profile email offline_access resource.invoke"
RESOURCE = "https://api.openai.com/v1"


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _int_b64u(value: int) -> str:
    return _b64u(value.to_bytes((value.bit_length() + 7) // 8, "big"))


class _ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class FakeOpenAIAuth:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "test-kid-1"
        self.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.lock = threading.RLock()
        self.codes = {}
        self.families = {}        # family_id -> dict(client_id, subject, current, alive)
        self.refresh_index = {}   # refresh token -> family_id
        self.clients = {}         # issued client_id -> subject (bound at registration)
        self.revoked = []         # every token presented to revoke, in order
        self.grants = []          # (grant_type, client_id) per token request
        self.paths = []           # every request path seen (for routing assertions)
        self._client_seq = itertools.count(1)
        # fault knobs
        self.refresh_delay = 0.0
        self.refresh_gate = None          # threading.Event the refresh handler waits on
        self.refresh_entered = threading.Event()
        self.refresh_respond_gate = None  # rotate first, then hold the RESPONSE on this Event
        self.refresh_rotated = threading.Event()
        self.refresh_faults = []          # queue of (status, body) returned instead of success
        self.include_id_token_on_refresh = True
        self.refresh_id_token_overrides = {}
        self.code_id_token_overrides = {}
        self.code_scope_override = None
        self.code_id_token_key = None
        self.code_response_overrides = {}
        self.refresh_response_overrides = {}
        self.revoke_cascades = True       # False: revoking a retired token leaves its successor alive
        self.launched_urls = []           # exactly what Lumina handed to the "system browser"
        self.revoke_faults = []           # queue of statuses returned instead of 200
        self.jwks_status = 200
        self.discovery_hits = 0
        self.jwks_hits = 0
        self.server = _ThreadingServer(("127.0.0.1", 0), self._handler())
        self.port = self.server.server_address[1]
        self.issuer = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    # -- tokens --------------------------------------------------------------

    def id_token(self, *, client_id, subject, email=None, nonce=None, key=None, kid=None,
                 now=None, **overrides):
        now = int(self.clock() if now is None else now)
        claims = {"iss": self.issuer, "aud": client_id, "sub": subject, "iat": now,
                  "exp": now + 3600, "canary": "CANARY-IDT"}
        if email:
            claims["email"] = email
        if nonce is not None:
            claims["nonce"] = nonce
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not _DROP}
        return jwt.encode(claims, key or self.key, algorithm="RS256",
                          headers={"kid": kid or self.kid})

    def jwks(self):
        pub = self.key.public_key().public_numbers()
        return {"keys": [{"kty": "RSA", "kid": self.kid, "use": "sig", "alg": "RS256",
                          "n": _int_b64u(pub.n), "e": _int_b64u(pub.e)}]}

    def _new_pair(self):
        return f"CANARY-AT-{uuid.uuid4().hex}", f"CANARY-RT-{uuid.uuid4().hex}"

    # -- browser simulation --------------------------------------------------

    def browser(self, *, subject="user-sub-A", email="owner@example.test", consent=True,
                scopes=FULL_SCOPES, mutate_callback=None, deliver=True, record=None):
        """A replacement for the system browser: validates the authorize URL
        like OpenAI would, signs the user in as `subject`, and immediately
        delivers the loopback callback (the listener is already bound)."""
        def open_browser(url):
            self.launched_urls.append(url)
            start = urllib.parse.urlsplit(url)
            if start.path.startswith("/auth/start/"):
                s0 = requests.Session()
                s0.trust_env = False
                hop = s0.get(url, allow_redirects=False, timeout=10)
                assert hop.status_code == 302, hop.status_code
                url = hop.headers["Location"]
            parts = urllib.parse.urlsplit(url)
            q = dict(urllib.parse.parse_qsl(parts.query))
            if record is not None:
                record.append(q)
            assert parts.path == "/api/accounts/authorize"
            assert q["response_type"] == "code"
            assert q["code_challenge_method"] == "S256"
            assert q["resource"] == RESOURCE
            redirect = q["redirect_uri"]
            cb = {"state": q["state"]}
            if not consent:
                cb["error"] = "access_denied"
            else:
                with self.lock:
                    if q["client_id"] == "dynamic_agent_client":
                        client_id = f"oaiapp_test{next(self._client_seq)}"
                        self.clients[client_id] = subject
                        returned_client = client_id
                    else:
                        client_id = q["client_id"]
                        assert client_id in self.clients, "unknown issued client"
                        returned_client = None   # reauthorization may omit it
                    code = f"CANARY-CODE-{uuid.uuid4().hex}"
                    self.codes[code] = {
                        "client_id": client_id, "redirect_uri": redirect,
                        "challenge": q["code_challenge"], "nonce": q["nonce"],
                        "subject": subject, "email": email, "scopes": scopes, "used": False,
                    }
                cb["code"] = code
                cb["scope"] = scopes
                if returned_client:
                    cb["client_id"] = returned_client
            if mutate_callback is not None:
                cb = mutate_callback(cb)
            if deliver:
                target = redirect + "?" + urllib.parse.urlencode(cb)
                s = requests.Session()
                s.trust_env = False
                s.get(target, timeout=10)
            return True
        return open_browser

    # -- HTTP handler --------------------------------------------------------

    def _handler(self):
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                fake.paths.append(self.path)
                if self.path == "/.well-known/openid-configuration":
                    fake.discovery_hits += 1
                    return self._json(200, {
                        "issuer": fake.issuer,
                        "authorization_endpoint": fake.issuer + "/api/accounts/authorize",
                        "token_endpoint": fake.issuer + "/api/accounts/oauth/token",
                        "revocation_endpoint": fake.issuer + "/api/accounts/oauth/revoke",
                        "jwks_uri": fake.issuer + "/.well-known/jwks.json",
                        "id_token_signing_alg_values_supported": ["RS256"],
                        "code_challenge_methods_supported": ["S256"],
                    })
                if self.path == "/.well-known/jwks.json":
                    fake.jwks_hits += 1
                    if fake.jwks_status != 200:
                        return self._json(fake.jwks_status, {"error": "unavailable"})
                    return self._json(200, fake.jwks())
                return self._json(404, {"error": "not_found"})

            def do_POST(self):
                fake.paths.append(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                form = dict(urllib.parse.parse_qsl(self.rfile.read(length).decode()))
                if self.path == "/api/accounts/oauth/token":
                    return self._token(form)
                if self.path == "/api/accounts/oauth/revoke":
                    return self._revoke(form)
                return self._json(404, {"error": "not_found"})

            def _token(self, form):
                grant = form.get("grant_type")
                with fake.lock:
                    fake.grants.append((grant, form.get("client_id")))
                if form.get("resource") != RESOURCE:
                    return self._json(400, {"error": "invalid_request"})
                if form.get("client_id") == "dynamic_agent_client" or form.get("client_id") not in fake.clients:
                    return self._json(400, {"error": "invalid_client"})
                if grant == "authorization_code":
                    return self._code(form)
                if grant == "refresh_token":
                    return self._refresh(form)
                return self._json(400, {"error": "unsupported_grant_type"})

            def _code(self, form):
                with fake.lock:
                    entry = fake.codes.get(form.get("code"))
                    if entry is None or entry["used"]:
                        return self._json(400, {"error": "invalid_grant"})
                    entry["used"] = True
                    verifier = form.get("code_verifier", "")
                    challenge = _b64u(hashlib.sha256(verifier.encode()).digest())
                    if (challenge != entry["challenge"] or form.get("client_id") != entry["client_id"]
                            or form.get("redirect_uri") != entry["redirect_uri"]):
                        return self._json(400, {"error": "invalid_grant"})
                    at, rt = fake._new_pair()
                    family = uuid.uuid4().hex
                    fake.families[family] = {"client_id": entry["client_id"], "subject": entry["subject"],
                                             "current": rt, "alive": True, "email": entry["email"]}
                    fake.refresh_index[rt] = family
                    scopes = fake.code_scope_override or entry["scopes"]
                    claims = {"client_id": entry["client_id"], "subject": entry["subject"],
                              "email": entry["email"], "nonce": entry["nonce"]}
                    claims.update(fake.code_id_token_overrides)
                    idt = fake.id_token(key=fake.code_id_token_key, **claims)
                body = {"access_token": at, "refresh_token": rt, "id_token": idt,
                        "token_type": "Bearer", "expires_in": 3600, "scope": scopes}
                for k, v in fake.code_response_overrides.items():
                    if v is _DROP:
                        body.pop(k, None)
                    else:
                        body[k] = v
                return self._json(200, body)

            def _refresh(self, form):
                fake.refresh_entered.set()
                if fake.refresh_gate is not None:
                    fake.refresh_gate.wait(30)
                if fake.refresh_delay:
                    time.sleep(fake.refresh_delay)
                with fake.lock:
                    if fake.refresh_faults:
                        status, body = fake.refresh_faults.pop(0)
                        return self._json(status, body)
                    rt = form.get("refresh_token")
                    family_id = fake.refresh_index.get(rt)
                    if family_id is None:
                        return self._json(400, {"error": "invalid_grant"})
                    family = fake.families[family_id]
                    if family["client_id"] != form.get("client_id"):
                        return self._json(400, {"error": "invalid_grant"})
                    if not family["alive"]:
                        return self._json(400, {"error": "invalid_grant"})
                    if family["current"] != rt:
                        family["alive"] = False        # reuse detection kills the family
                        return self._json(400, {"error": "refresh_token_reused"})
                    at, new_rt = fake._new_pair()
                    family["current"] = new_rt
                    fake.refresh_index[new_rt] = family_id
                    body = {"access_token": at, "refresh_token": new_rt, "token_type": "Bearer",
                            "expires_in": 3600}
                    if fake.include_id_token_on_refresh:
                        claims = {"client_id": family["client_id"], "subject": family["subject"],
                                  "email": family["email"]}
                        claims.update(fake.refresh_id_token_overrides)
                        body["id_token"] = fake.id_token(**claims)
                    for k, v in fake.refresh_response_overrides.items():
                        if v is _DROP:
                            body.pop(k, None)
                        else:
                            body[k] = v
                fake.refresh_rotated.set()
                if fake.refresh_respond_gate is not None:
                    fake.refresh_respond_gate.wait(30)
                return self._json(200, body)

            def _revoke(self, form):
                with fake.lock:
                    fake.revoked.append(form.get("token"))
                    if fake.revoke_faults:
                        status = fake.revoke_faults.pop(0)
                        return self._json(status, {"error": "server_error"})
                    family_id = fake.refresh_index.get(form.get("token"))
                    if family_id is not None and (
                            fake.revoke_cascades or fake.families[family_id]["current"] == form.get("token")):
                        fake.families[family_id]["alive"] = False
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

        return H

    # -- assertions helpers --------------------------------------------------

    def refresh_count(self):
        return sum(1 for g, _ in self.grants if g == "refresh_token")

    def family_alive_for(self, refresh_token):
        family_id = self.refresh_index.get(refresh_token)
        return family_id is not None and self.families[family_id]["alive"]


class _Drop:
    def __repr__(self):
        return "DROP"


_DROP = _Drop()
DROP = _DROP


def provider_session():
    s = requests.Session()
    s.trust_env = False
    return s


class FakeClock:
    def __init__(self, start=None):
        self.now = time.time() if start is None else start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
