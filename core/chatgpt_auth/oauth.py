"""
SUBSCRIPTION-PLAN-BACKENDS-01C -- the documented Sign in with ChatGPT
protocol for open-source local apps (vetted 2026-10-01 against
developers.openai.com/siwc/token-sharing-open-source/*):

  authorization code + PKCE S256 + OIDC nonce, system browser, loopback
  callback on 127.0.0.1, first registration via the bootstrap client ID
  `dynamic_agent_client` (never saved, never used to exchange), issued
  client ID per registration, ID token verified against OpenAI's JWKS,
  rotating refresh tokens, refresh-token revocation on sign-out.

This module performs authentication HTTP only: discovery, JWKS, token,
revocation. It has no inference surface -- no Responses request, no model
listing -- and never reads an API key.

Nothing here logs or prints. Exceptions carry a categorical `error_class`
and an HTTP status, never a response body, token, code, URL or identity.
"""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import math
import secrets
import threading
import time
import urllib.parse
from datetime import datetime
from typing import Callable, Optional

import jwt
import requests

from core.chatgpt_auth.store import BOOTSTRAP_CLIENT_ID, Credentials, _client_id_ok

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
SCOPES = ("openid", "profile", "email", "offline_access", "resource.invoke",
          "chatgpt.tokens.use.direct")
PLAN_SCOPE = "chatgpt.tokens.use.direct"
CALLBACK_PATH = "/auth/callback"
AGENT_NAME_HINT = "Lumina"

ID_TOKEN_ALGORITHMS = ["RS256"]          # discovery: id_token_signing_alg_values_supported
ID_TOKEN_LEEWAY_SECONDS = 5              # vendor example's clockTolerance
HTTP_CONNECT_TIMEOUT = 10.0
HTTP_READ_TIMEOUT = 30.0
MAX_EXPIRES_IN_SECONDS = 24 * 3600       # vendor documents 3600; anything far beyond is malformed

# Vendor-documented terminal refresh codes (errors-and-recovery, "Refresh errors").
TERMINAL_REFRESH_CODES = frozenset({
    "invalid_grant", "invalid_refresh_token", "token_expired",
    "refresh_token_expired", "refresh_token_invalidated", "refresh_token_reused",
})
INVALID_CLIENT_CODE = "invalid_client"
# Codes we are willing to carry as an `error_class`; anything else the
# provider says is collapsed to "unrecognized_error" (never echoed).
_KNOWN_PROVIDER_CODES = TERMINAL_REFRESH_CODES | {
    INVALID_CLIENT_CODE, "invalid_request", "unauthorized_client", "access_denied",
    "invalid_scope", "server_error", "temporarily_unavailable", "unsupported_grant_type",
}
CALLBACK_ERROR_CODES = frozenset({
    "access_denied", "invalid_request", "unauthorized_client", "invalid_scope",
    "server_error", "temporarily_unavailable", "consent_required", "login_required",
    "interaction_required",
})


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ProtocolError(Exception):
    """Base. `error_class` is categorical and safe to record."""

    retryable = False

    def __init__(self, error_class: str, status: Optional[int] = None):
        super().__init__(error_class)
        self.error_class = error_class
        self.status = status


class ProviderUnavailable(ProtocolError):
    """Network failure, timeout, 429 or 5xx. Never grounds for erasing a
    session (vendor: "Do not erase credentials solely because of a temporary
    network or infrastructure failure")."""
    retryable = True


class ProviderRejected(ProtocolError):
    """The provider answered with a definite OAuth error."""

    @property
    def terminal_for_refresh(self) -> bool:
        return self.error_class in TERMINAL_REFRESH_CODES or self.error_class == INVALID_CLIENT_CODE


class InvalidTokenResponse(ProtocolError):
    """A 2xx token response that does not meet the documented contract.
    `salvage_refresh_token` carries a syntactically present refresh token
    from such a body (refresh only) so the caller can revoke it -- it is
    never stored or used."""
    salvage_refresh_token: Optional[str] = None


class IdentityInvalid(ProtocolError):
    """The ID token failed verification (signature, issuer, audience,
    expiry, nonce, subject, azp). Never retried."""


class IdentityVerificationUnavailable(ProtocolError):
    """Discovery/JWKS could not be fetched. Says nothing about the token's
    validity; the caller keeps whatever it holds and retries later."""
    retryable = True


# ---------------------------------------------------------------------------
# Randomness / PKCE  (CSPRNG: `secrets`)
# ---------------------------------------------------------------------------

def new_state() -> str:
    return secrets.token_urlsafe(32)


def new_nonce() -> str:
    return secrets.token_urlsafe(32)


def new_pkce_verifier() -> str:
    # 64 chars from the RFC 7636 unreserved alphabet (43..128 allowed).
    return secrets.token_urlsafe(48)


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


# ---------------------------------------------------------------------------
# Authorization attempt (in memory only -- never persisted)
# ---------------------------------------------------------------------------

MODE_NEW_REGISTRATION = "new_registration"
MODE_REAUTHORIZE = "reauthorize"


@dataclasses.dataclass
class AuthorizationAttempt:
    """Everything one browser authorization is bound to. Lives only in this
    process's memory for one attempt; scrubbed when the attempt ends.
    Pending temporary auth material is never written to prefs, the session
    store, logs, the Flight Recorder or a backup."""
    attempt_id: str
    created_at: float
    expires_at: float
    redirect_uri: str
    state: str = dataclasses.field(repr=False)
    nonce: str = dataclasses.field(repr=False)
    verifier: str = dataclasses.field(repr=False)
    mode: str
    host_id: str = dataclasses.field(repr=False)
    client_id: Optional[str] = dataclasses.field(repr=False)        # None => bootstrap registration
    expected_profile_id: Optional[str]
    expected_subject: Optional[str] = dataclasses.field(repr=False)
    pending_registration_id: Optional[str]
    request_consent: bool = False
    finished: bool = False

    def scrub(self) -> None:
        self.state = self.nonce = self.verifier = ""
        self.finished = True


def build_authorization_url(authorization_endpoint: str, attempt: AuthorizationAttempt) -> str:
    """The URL contains state/nonce/challenge and the host ID: it is never
    logged, recorded or shown. Account hints are deliberately omitted -- an
    `id_token_hint` would put an ID token (and `login_hint` an email) into
    the browser launcher's process arguments, which other local users can
    read; the documented flow works without them and the returned identity
    is verified either way."""
    params = {
        "client_id": attempt.client_id or BOOTSTRAP_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": attempt.redirect_uri,
        "scope": " ".join(SCOPES),
        "resource": RESOURCE,
        "state": attempt.state,
        "nonce": attempt.nonce,
        "code_challenge_method": "S256",
        "code_challenge": pkce_challenge(attempt.verifier),
        "ext_agent_host_id": attempt.host_id,
    }
    if attempt.client_id is None:
        params["agent_name_hint"] = AGENT_NAME_HINT
    if attempt.request_consent:
        # Documented re-enable path (errors-and-recovery): OAuth prompt=consent,
        # only on an explicit owner request -- never on ordinary sign-in.
        params["prompt"] = "consent"
    return authorization_endpoint + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)


# ---------------------------------------------------------------------------
# Token responses
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class TokenResult:
    credentials: Credentials
    id_token: Optional[str] = dataclasses.field(repr=False)
    scopes: tuple


def _parse_earliest_refresh_at(value) -> Optional[float]:
    """The token reference lists `earliest_refresh_at` without a type. Accept
    epoch seconds or an ISO-8601 timestamp; anything else is ignored (the
    expiry still governs)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
        return float(value)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                return None
            return parsed.timestamp()
        except (ValueError, OverflowError, OSError):
            return None
    return None


def parse_token_response(data, *, now: float, previous_scopes: Optional[tuple] = None,
                         require_id_token: bool) -> TokenResult:
    """Validates a 2xx token-endpoint body against the documented contract.
    Refresh responses may omit `scope` (then the previous grant stands) and
    may omit `id_token`; a refresh token is always required because every
    successful refresh rotates it."""
    if not isinstance(data, dict):
        raise InvalidTokenResponse("token_response_shape")
    scope = data.get("scope")
    if scope is None and previous_scopes is not None:
        scopes = tuple(previous_scopes)
    elif isinstance(scope, str):
        scopes = tuple(s for s in scope.split() if s)
    else:
        raise InvalidTokenResponse("token_response_scope")
    access = data.get("access_token")
    refresh = data.get("refresh_token")
    token_type = data.get("token_type")
    expires_in = data.get("expires_in")
    if not isinstance(access, str) or not access:
        raise InvalidTokenResponse("token_response_access")
    if not isinstance(token_type, str) or token_type.lower() != "bearer":
        raise InvalidTokenResponse("token_response_type")
    if (isinstance(expires_in, bool) or not isinstance(expires_in, (int, float))
            or not math.isfinite(expires_in) or expires_in <= 0 or expires_in > MAX_EXPIRES_IN_SECONDS):
        raise InvalidTokenResponse("token_response_expiry")
    if not isinstance(refresh, str) or not refresh:
        raise InvalidTokenResponse("token_response_refresh")
    id_token = data.get("id_token")
    if id_token is not None and (not isinstance(id_token, str) or not id_token):
        raise InvalidTokenResponse("token_response_id_token")
    if require_id_token and id_token is None:
        raise InvalidTokenResponse("token_response_id_token")
    creds = Credentials(
        access_token=access,
        refresh_token=refresh,
        id_token=id_token,
        token_type="Bearer",
        expires_at=now + float(expires_in),
        earliest_refresh_at=_parse_earliest_refresh_at(data.get("earliest_refresh_at")),
        scopes=scopes,
        received_at=now,
    )
    return TokenResult(credentials=creds, id_token=id_token, scopes=scopes)


# ---------------------------------------------------------------------------
# Verified identity
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class VerifiedIdentity:
    issuer: str
    subject: str = dataclasses.field(repr=False)
    email: Optional[str] = dataclasses.field(repr=False)
    name: Optional[str] = dataclasses.field(repr=False)


# ---------------------------------------------------------------------------
# Provider client
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class ProviderEndpoints:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    revocation_endpoint: str
    jwks_uri: str


def _origin(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


class ProviderClient:
    """HTTP to OpenAI's auth service only. The issuer is fixed to
    https://auth.openai.com in production; tests construct one against a
    local fake provider (`allow_loopback_http=True` is the only way to use
    a non-HTTPS issuer, and only a 127.0.0.1 one)."""

    def __init__(self, issuer: str = ISSUER, *, session: Optional[requests.Session] = None,
                 allow_loopback_http: bool = False,
                 sleep: Callable[[float], None] = time.sleep):
        parts = urllib.parse.urlsplit(issuer)
        loopback_http = (allow_loopback_http and parts.scheme == "http"
                         and parts.hostname == "127.0.0.1")
        if parts.scheme != "https" and not loopback_http:
            raise ValueError("ChatGPT issuer must use HTTPS")
        self.issuer = issuer.rstrip("/")
        self._http = session or requests.Session()
        self._sleep = sleep
        self._endpoints: Optional[ProviderEndpoints] = None
        self._jwks: dict = {}
        self._lock = threading.Lock()

    # -- transport -----------------------------------------------------------

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        try:
            return self._http.request(method, url, timeout=(HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT),
                                      allow_redirects=False, **kwargs)
        except requests.Timeout:
            raise ProviderUnavailable("network_timeout") from None
        except requests.RequestException:
            raise ProviderUnavailable("network_error") from None

    @staticmethod
    def _json_or_none(response: requests.Response):
        try:
            return response.json()
        except ValueError:
            return None

    @staticmethod
    def _error_class(body) -> str:
        code = None
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, str):
                code = err
            elif isinstance(err, dict) and isinstance(err.get("code"), str):
                code = err["code"]
        return code if code in _KNOWN_PROVIDER_CODES else "unrecognized_error"

    def _raise_for_status(self, response: requests.Response) -> None:
        status = response.status_code
        if status == 429 or status >= 500:
            raise ProviderUnavailable(f"http_{status}", status)
        if status >= 400:
            raise ProviderRejected(self._error_class(self._json_or_none(response)), status)
        if status >= 300:
            raise ProviderRejected("unexpected_redirect", status)

    # -- discovery / JWKS ----------------------------------------------------

    def endpoints(self) -> ProviderEndpoints:
        with self._lock:
            if self._endpoints is not None:
                return self._endpoints
        response = self._request("GET", self.issuer + "/.well-known/openid-configuration",
                                 headers={"Accept": "application/json"})
        if response.status_code != 200:
            raise ProviderUnavailable("discovery_unavailable", response.status_code)
        data = self._json_or_none(response)
        if not isinstance(data, dict) or data.get("issuer") != self.issuer:
            raise ProviderUnavailable("discovery_invalid")
        values = {}
        for key in ("authorization_endpoint", "token_endpoint", "revocation_endpoint", "jwks_uri"):
            value = data.get(key)
            if not isinstance(value, str) or _origin(value) != _origin(self.issuer):
                raise ProviderUnavailable("discovery_invalid")
            values[key] = value
        endpoints = ProviderEndpoints(issuer=self.issuer, **values)
        with self._lock:
            self._endpoints = endpoints
        return endpoints

    def _fetch_jwks(self) -> dict:
        try:
            endpoints = self.endpoints()
            response = self._request("GET", endpoints.jwks_uri, headers={"Accept": "application/json"})
        except ProviderUnavailable:
            raise IdentityVerificationUnavailable("jwks_unavailable") from None
        data = self._json_or_none(response)
        if response.status_code != 200 or not isinstance(data, dict) or not isinstance(data.get("keys"), list):
            raise IdentityVerificationUnavailable("jwks_unavailable", response.status_code)
        keys = {}
        for entry in data["keys"]:
            if not isinstance(entry, dict) or entry.get("kty") != "RSA" or not isinstance(entry.get("kid"), str):
                continue
            if entry.get("use") not in (None, "sig") or entry.get("alg") not in (None, "RS256"):
                continue
            try:
                keys[entry["kid"]] = jwt.PyJWK(entry, algorithm="RS256").key
            except Exception:
                continue
        with self._lock:
            self._jwks = keys
        return keys

    def _signing_key(self, kid) -> object:
        if not isinstance(kid, str) or not kid:
            raise IdentityInvalid("id_token_kid")
        with self._lock:
            key = self._jwks.get(kid)
        if key is None:
            # Unknown kid: refresh the key set once (rotation), then decide.
            key = self._fetch_jwks().get(kid)
        if key is None:
            # Still unknown after one refetch: a key-set propagation delay or
            # an empty/unusable set says nothing about the token itself.
            raise IdentityVerificationUnavailable("id_token_unknown_key")
        return key

    def verify_id_token(self, id_token: str, *, client_id: str, nonce: Optional[str],
                        now: float) -> VerifiedIdentity:
        """Signature (RS256 against the issuer's JWKS, by PyJWT), issuer,
        audience == the issued client ID, azp, expiry/iat/nbf (against the
        injected clock, with the vendor's 5 s tolerance), nonce (when this
        token answers an authorization request), and a non-empty subject.
        Email is returned for display only; it is never identity."""
        if not isinstance(id_token, str) or not id_token:
            raise IdentityInvalid("id_token_missing")
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError:
            raise IdentityInvalid("id_token_malformed") from None
        if header.get("alg") not in ID_TOKEN_ALGORITHMS:
            raise IdentityInvalid("id_token_algorithm")
        key = self._signing_key(header.get("kid"))
        try:
            claims = jwt.decode(
                id_token, key=key, algorithms=ID_TOKEN_ALGORITHMS,
                audience=client_id, issuer=self.issuer,
                # Time claims are checked below against the injected clock
                # (PyJWT cannot take one); signature/aud/iss stay PyJWT's.
                options={"require": ["iss", "aud", "exp", "iat", "sub"],
                         "verify_exp": False, "verify_iat": False, "verify_nbf": False},
            )
        except jwt.PyJWTError:
            raise IdentityInvalid("id_token_rejected") from None
        exp, iat, nbf = claims.get("exp"), claims.get("iat"), claims.get("nbf")
        for value in (exp, iat):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise IdentityInvalid("id_token_time_claims")
        if now > exp + ID_TOKEN_LEEWAY_SECONDS:
            raise IdentityInvalid("id_token_expired")
        if iat > now + ID_TOKEN_LEEWAY_SECONDS:
            raise IdentityInvalid("id_token_issued_in_future")
        if nbf is not None and (isinstance(nbf, bool) or not isinstance(nbf, (int, float))
                                or nbf > now + ID_TOKEN_LEEWAY_SECONDS):
            raise IdentityInvalid("id_token_not_yet_valid")
        aud = claims.get("aud")
        azp = claims.get("azp")
        if azp is not None and azp != client_id:
            raise IdentityInvalid("id_token_azp")
        if isinstance(aud, list) and len(aud) > 1 and azp != client_id:
            raise IdentityInvalid("id_token_azp")
        if nonce is not None and claims.get("nonce") != nonce:
            raise IdentityInvalid("id_token_nonce")
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise IdentityInvalid("id_token_subject")
        email = claims.get("email") if isinstance(claims.get("email"), str) else None
        name = claims.get("name") if isinstance(claims.get("name"), str) else None
        return VerifiedIdentity(issuer=self.issuer, subject=subject,
                                email=(email[:320] if email else None),
                                name=(name[:200] if name else None))

    # -- token endpoint ------------------------------------------------------

    def _token_request(self, form: dict) -> dict:
        endpoints = self.endpoints()
        response = self._request("POST", endpoints.token_endpoint, data=form,
                                 headers={"Accept": "application/json"})
        self._raise_for_status(response)
        body = self._json_or_none(response)
        if not isinstance(body, dict):
            raise InvalidTokenResponse("token_response_shape")
        return body

    def exchange_code(self, *, client_id: str, code: str, verifier: str, redirect_uri: str,
                      now: float) -> TokenResult:
        if not _client_id_ok(client_id):
            raise ProtocolError("issued_client_id_invalid")
        body = self._token_request({
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "resource": RESOURCE,
        })
        return parse_token_response(body, now=now, require_id_token=True)

    def refresh(self, *, client_id: str, refresh_token: str, previous_scopes: tuple,
                now: float) -> TokenResult:
        if not _client_id_ok(client_id):
            raise ProtocolError("issued_client_id_invalid")
        body = self._token_request({
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
            "resource": RESOURCE,
        })
        try:
            return parse_token_response(body, now=now, previous_scopes=previous_scopes,
                                        require_id_token=False)
        except InvalidTokenResponse as exc:
            salvage = body.get("refresh_token")
            if isinstance(salvage, str) and salvage and salvage != refresh_token:
                exc.salvage_refresh_token = salvage
            raise

    def revoke(self, *, client_id: str, refresh_token: str, attempts: int = 3,
               backoff_seconds: float = 0.5) -> bool:
        """True only on the documented success response (HTTP 200, which
        also covers an already-invalid token). Network/5xx failures are
        retried with backoff, bounded; any other answer is 'not confirmed'.
        Never raises for a revocation outcome."""
        try:
            endpoints = self.endpoints()
        except ProviderUnavailable:
            return False
        for attempt in range(max(1, attempts)):
            retryable = False
            try:
                response = self._request("POST", endpoints.revocation_endpoint, data={
                    "token": refresh_token,
                    "token_type_hint": "refresh_token",
                    "client_id": client_id,
                })
                if response.status_code == 200:
                    return True
                retryable = response.status_code >= 500 or response.status_code == 429
            except ProviderUnavailable:
                retryable = True
            if not retryable or attempt == attempts - 1:
                return False
            self._sleep(backoff_seconds * (2 ** attempt))
        return False
