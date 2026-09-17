"""
core/higgsfield_transport.py -- MULTIMODAL-M4-HIGGSFIELD-ADAPTER-01

The small, injectable HTTP transport layer for core/higgsfield_adapter.py.
Exists as its own seam, separate from the adapter's Higgsfield-domain
logic, so tests can inject FakeHiggsfieldTransport (see the test file) and
exercise the adapter's real request-building/response-parsing code with
zero sockets -- never a network call, never real credentials.

Wraps `requests`, already an established project dependency (see
core/backends/openai_backend.py and siblings) -- no new third-party
dependency was needed or added.

Auth per MULTIMODAL_M4_HIGGSFIELD_API_VET_2026-09-17.md Sec 3, re-verified
live (byte-identical diff against the same-day cache) immediately before
this module was written: `Authorization: Key {key_id}:{key_secret}`, never
Bearer. Credentials are supplied at construction time by the caller (the
adapter), sourced from core.secrets -- see core/higgsfield_adapter.py's
module docstring for why that's the credential boundary this campaign
reuses rather than redesigns. This module never reads config.py or
core.secrets itself; it only ever receives already-resolved key material
as constructor arguments, and never logs, reprs, or embeds it in any
exception message -- see _redacted_repr() and every error-construction
path below.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional, Protocol

import requests

BASE_URL = "https://api.higgsfield.ai"
# Lightweight metadata calls (submit/poll/cancel/estimate/upload-url) --
# NOT the generation itself, which runs async server-side and is observed
# via polling, not held open on one request. Deliberately much shorter
# than config.TOOL_CALL_TIMEOUT (600s, tuned for a different kind of call
# -- LLM tool-use turns -- not applicable here).
DEFAULT_TIMEOUT = 30


class HiggsfieldTransportError(Exception):
    """Base class for every transport-level failure (network/connection
    issues -- never an HTTP error status, which is a normal, successfully
    round-tripped TransportResponse the adapter layer interprets)."""


class HiggsfieldConnectionError(HiggsfieldTransportError):
    """The provider was not reachable at all."""


class HiggsfieldTimeoutError(HiggsfieldTransportError):
    """The request exceeded its timeout before any response arrived."""


@dataclass(frozen=True)
class TransportResponse:
    """A small, explicit value type -- deliberately NOT `requests.Response`
    itself, so a fake transport in tests can construct one trivially with
    no real HTTP machinery, and so the adapter layer depends on a narrow,
    stable shape rather than requests' full surface."""

    status_code: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""

    def json(self):
        import json as _json
        return _json.loads(self.body.decode("utf-8"))

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class HiggsfieldTransport(Protocol):
    """The seam core/higgsfield_adapter.py depends on. Real
    (RequestsHiggsfieldTransport, below) and fake (test-only) transports
    both satisfy this structurally -- no shared base class, matching
    core/generation_adapter.py's own typing.Protocol convention."""

    def post(self, path: str, *, json: Optional[dict] = None,
              params: Optional[Mapping[str, str]] = None) -> TransportResponse:
        """POST to `path` (relative to the API base URL) with the
        Authorization header already applied. Raises HiggsfieldConnectionError
        / HiggsfieldTimeoutError on a network-level failure; any HTTP
        status (including 4xx/5xx) is returned as an ordinary
        TransportResponse for the caller to interpret -- this method never
        raises merely because the provider returned an error status."""
        ...

    def get(self, path: str, *,
            params: Optional[Mapping[str, str]] = None) -> TransportResponse:
        ...

    def put_bytes(self, url: str, *, data: bytes,
                   headers: Mapping[str, str]) -> TransportResponse:
        """PUT raw bytes to an arbitrary (already-presigned) URL, with
        caller-supplied headers ONLY -- deliberately does NOT apply the
        Higgsfield Authorization header, matching the source-vet finding
        that Higgsfield credentials must never be sent to the presigned
        storage URL (file-uploads.md: "Do not send Higgsfield API
        credentials to the presigned storage URL")."""
        ...

    def get_raw(self, url: str, *,
                headers: Optional[Mapping[str, str]] = None) -> TransportResponse:
        """GET an arbitrary absolute URL (e.g. a completed-output CDN URL)
        with ONLY caller-supplied headers -- deliberately does NOT apply
        the Higgsfield Authorization header. Output URLs are typically
        hosted off api.higgsfield.ai entirely (every documented example is
        `https://cdn.example.com/...`); sending Higgsfield credentials
        there would leak them to an unrelated host."""
        ...


class RequestsHiggsfieldTransport:
    """The real transport. Constructed with already-resolved credentials
    (never reads them itself) and never exposes them again: __repr__ is
    overridden so accidentally printing/logging an instance (e.g. in a
    traceback's local-variable dump) cannot leak the secret."""

    def __init__(self, key_id: str, key_secret: str, *,
                 base_url: str = BASE_URL, timeout: int = DEFAULT_TIMEOUT):
        if not isinstance(key_id, str) or not key_id:
            raise HiggsfieldTransportError("key_id must be a non-empty string")
        if not isinstance(key_secret, str) or not key_secret:
            raise HiggsfieldTransportError("key_secret must be a non-empty string")
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._auth_header = f"Key {key_id}:{key_secret}"

    def __repr__(self) -> str:
        return f"RequestsHiggsfieldTransport(base_url={self._base_url!r}, key=[REDACTED])"

    def _headers(self, *, json_body: bool) -> dict:
        headers = {"Authorization": self._auth_header}
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _url(self, path: str) -> str:
        path = path.lstrip("/")
        return f"{self._base_url}/{path}"

    def _to_transport_response(self, resp: "requests.Response") -> TransportResponse:
        return TransportResponse(
            status_code=resp.status_code,
            headers=dict(resp.headers),
            body=resp.content or b"",
        )

    def post(self, path: str, *, json: Optional[dict] = None,
              params: Optional[Mapping[str, str]] = None) -> TransportResponse:
        try:
            resp = requests.post(
                self._url(path), headers=self._headers(json_body=True),
                json=json, params=dict(params) if params else None,
                timeout=self._timeout,
            )
        except requests.exceptions.ConnectionError as exc:
            raise HiggsfieldConnectionError(f"Higgsfield not reachable at {self._base_url}") from exc
        except requests.exceptions.Timeout as exc:
            raise HiggsfieldTimeoutError(f"Higgsfield request to {path} timed out") from exc
        return self._to_transport_response(resp)

    def get(self, path: str, *,
            params: Optional[Mapping[str, str]] = None) -> TransportResponse:
        try:
            resp = requests.get(
                self._url(path), headers=self._headers(json_body=False),
                params=dict(params) if params else None,
                timeout=self._timeout,
            )
        except requests.exceptions.ConnectionError as exc:
            raise HiggsfieldConnectionError(f"Higgsfield not reachable at {self._base_url}") from exc
        except requests.exceptions.Timeout as exc:
            raise HiggsfieldTimeoutError(f"Higgsfield request to {path} timed out") from exc
        return self._to_transport_response(resp)

    def put_bytes(self, url: str, *, data: bytes,
                   headers: Mapping[str, str]) -> TransportResponse:
        try:
            resp = requests.put(url, data=data, headers=dict(headers), timeout=self._timeout)
        except requests.exceptions.ConnectionError as exc:
            raise HiggsfieldConnectionError(f"upload target not reachable") from exc
        except requests.exceptions.Timeout as exc:
            raise HiggsfieldTimeoutError(f"upload to presigned URL timed out") from exc
        return self._to_transport_response(resp)

    def get_raw(self, url: str, *,
                headers: Optional[Mapping[str, str]] = None) -> TransportResponse:
        try:
            resp = requests.get(url, headers=dict(headers) if headers else None, timeout=self._timeout)
        except requests.exceptions.ConnectionError as exc:
            raise HiggsfieldConnectionError(f"output URL not reachable") from exc
        except requests.exceptions.Timeout as exc:
            raise HiggsfieldTimeoutError(f"fetching output URL timed out") from exc
        return self._to_transport_response(resp)
