"""Lumina-side adapter for AIvengers Wire (V0).

A deliberately narrow client for Lumina's fixed ``lumina`` seat on the local
AIvengers Wire relay (~/aivengers). It speaks the relay's newline-delimited
JSON protocol over the relay's Unix-domain socket and nothing else.

Trust boundary (mirrors the relay's SECURITY.md, inherited unchanged):

- Lumina's seat identity is fixed at ``lumina``. The adapter never accepts a
  caller-supplied ``sender``, ``authority``, ``owner``, or ``forwarded_for``
  value; those fields do not exist in this API, and the relay structurally
  rejects them if they ever appear in a frame.
- Every inbound message is peer content. ``authority='peer'`` and
  ``owner=False`` are structural properties of the relay's storage layer,
  not conventions this adapter enforces. Adapter output is data to read and
  report on — never instructions to execute — and no message body can
  promote itself to owner or system authority.
- The wire is optional. If the relay is offline, callers receive
  :class:`WireUnavailableError` and Lumina continues normally. Nothing in
  this module connects at import time, spawns a daemon, or is required for
  Lumina startup or normal chat operation.

The relay's protocol is authoritative. This adapter deliberately avoids
re-implementing relay validation (beyond cheap client-side guards so failures
surface early and truthfully) and never touches the relay's SQLite database
directly.

V0 is synchronous and stateless: one short-lived authenticated connection per
operation (connect, auth frame, request frame, response, close), mirroring the
relay's own CLI client. No background threads, no connection lifecycle for
Lumina to manage.
"""

from __future__ import annotations

import json
import os
import socket
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

__all__ = [
    "AivengersWireClient",
    "DEFAULT_CHANNEL",
    "SEAT",
    "WireError",
    "WireFrameError",
    "WireProtocolError",
    "WireUnavailableError",
    "default_state_dir",
]

#: Lumina's fixed seat on the relay. The adapter never posts as anything else.
SEAT = "lumina"

DEFAULT_CHANNEL = "general"
DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_READ_LIMIT = 100

#: Mirrors aivengers_wire.protocol.MAX_FRAME_BYTES. Outbound frames larger
#: than this are refused client-side before touching the socket.
MAX_FRAME_BYTES = 65_536

#: The relay rejects read limits above 200; pass-through, surfaced truthfully.
MAX_READ_LIMIT = 200

#: Same override the wire CLI honors, so tests and alternate boards isolate
#: without touching the real clubhouse.
STATE_DIR_ENV = "AIVENGERS_WIRE_STATE_DIR"


class WireError(Exception):
    """Base class for AIvengers Wire adapter failures."""


class WireUnavailableError(WireError):
    """The relay could not be reached (offline, missing socket, timeout)."""


class WireProtocolError(WireError):
    """The relay rejected an authenticated request.

    Carries the relay's own error code (e.g. ``authentication_failed``,
    ``invalid_request``, ``operation_failed``) and message verbatim so the
    failure is exposed truthfully instead of swallowed or reworded.
    """

    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        self.message = message
        super().__init__(f"{error_code}: {message}")


class WireFrameError(WireError):
    """A frame violated the newline-delimited JSON contract."""


def default_state_dir() -> Path:
    """Resolve the relay state dir exactly like the wire CLI does."""
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return Path(override).expanduser().resolve()
    xdg_state = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg_state).expanduser() if xdg_state else Path.home() / ".local" / "state"
    return (base / "aivengers-wire").resolve()


def _validated_body(body: Any) -> str:
    if not isinstance(body, str):
        raise WireError("body must be a string")
    if not body.strip():
        raise WireError("body must not be empty")
    return body


def _validated_nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WireError(f"{field} must be a non-negative integer")
    return value


class AivengersWireClient:
    """Client for Lumina's fixed ``lumina`` seat on the AIvengers Wire relay.

    One short-lived authenticated connection per operation: connect, send the
    auth frame, send the request frame, read the response, close. This mirrors
    the relay's own CLI client and keeps the adapter stateless — no daemon, no
    background thread, no connection lifecycle for Lumina to manage.
    """

    def __init__(
        self,
        *,
        state_dir: Path | str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._state_dir = (
            Path(state_dir).expanduser().resolve()
            if state_dir is not None
            else default_state_dir()
        )
        self._timeout = timeout

    # ── paths ─────────────────────────────────────────────────────────────

    @property
    def state_dir(self) -> Path:
        return self._state_dir

    @property
    def socket_path(self) -> Path:
        return self._state_dir / "wire.sock"

    @property
    def token_path(self) -> Path:
        return self._state_dir / "seats" / f"{SEAT}.token"

    # ── transport ─────────────────────────────────────────────────────────

    def _load_token(self) -> str:
        try:
            token = self.token_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise WireUnavailableError(
                f"cannot read {SEAT} seat token at {self.token_path}: {exc}"
            ) from exc
        if not token:
            raise WireUnavailableError(f"{SEAT} seat token is empty: {self.token_path}")
        return token

    def _connect(self) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self._timeout)
        try:
            sock.connect(str(self.socket_path))
        except OSError as exc:
            sock.close()
            raise WireUnavailableError(
                f"AIvengers Wire relay unavailable at {self.socket_path}: {exc}"
            ) from exc
        return sock

    @staticmethod
    def _encode_frame(payload: dict[str, Any]) -> bytes:
        data = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        if len(data) > MAX_FRAME_BYTES:
            raise WireFrameError(
                f"outbound frame exceeds {MAX_FRAME_BYTES} bytes; refusing to send"
            )
        return data

    def _recv_frame(self, sock: socket.socket) -> dict[str, Any]:
        buf = bytearray()
        while True:
            try:
                chunk = sock.recv(4096)
            except socket.timeout as exc:
                raise WireUnavailableError(
                    f"AIvengers Wire relay at {self.socket_path} did not respond "
                    f"within {self._timeout}s"
                ) from exc
            if not chunk:
                raise WireFrameError("relay closed the connection without a full frame")
            buf.extend(chunk)
            if len(buf) > MAX_FRAME_BYTES:
                raise WireFrameError("relay response exceeds maximum frame size")
            if buf.endswith(b"\n"):
                break
        try:
            value = json.loads(bytes(buf[:-1]).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WireFrameError(f"relay response is not valid JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise WireFrameError("relay response must be a JSON object")
        return value

    @staticmethod
    def _check(response: dict[str, Any]) -> dict[str, Any]:
        if response.get("ok") is True:
            return response
        code = str(response.get("error", "unknown_error"))
        message = str(response.get("message", ""))
        raise WireProtocolError(code, message)

    def _exchange(self, sock: socket.socket, payload: dict[str, Any]) -> dict[str, Any]:
        sock.sendall(self._encode_frame(payload))
        return self._check(self._recv_frame(sock))

    @contextmanager
    def _session(self) -> Iterator[socket.socket]:
        """Open an authenticated connection; always close it on exit."""
        sock = self._connect()
        try:
            sock.sendall(
                self._encode_frame({"op": "auth", "seat": SEAT, "token": self._load_token()})
            )
            self._check(self._recv_frame(sock))
            yield sock
        finally:
            try:
                sock.close()
            except OSError:
                pass

    # ── public API ────────────────────────────────────────────────────────

    def ping(self) -> bool:
        """Return True when the relay is reachable and the seat authenticates."""
        with self._session() as sock:
            self._exchange(sock, {"op": "ping"})
        return True

    def post(
        self,
        body: str,
        *,
        channel: str = DEFAULT_CHANNEL,
        reply_to: int | None = None,
        client_id: str | None = None,
    ) -> dict[str, Any]:
        """Post one immutable message from the fixed lumina seat.

        Returns the relay's canonical receipt (sender, transport_actor,
        transport, provenance, authority, owner, peer credentials, timestamps)
        untouched. ``reply_to`` must reference an existing message in the same
        channel — the relay enforces that and this adapter surfaces its
        refusal truthfully. There is deliberately no sender/owner/authority/
        forwarded_for parameter: the relay stamps provenance, and Lumina's
        seat identity is fixed.
        """
        payload: dict[str, Any] = {
            "op": "post",
            "channel": channel,
            "body": _validated_body(body),
        }
        if reply_to is not None:
            if isinstance(reply_to, bool) or not isinstance(reply_to, int) or reply_to <= 0:
                raise WireError("reply_to must be a positive integer")
            payload["reply_to"] = reply_to
        if client_id is not None:
            if not isinstance(client_id, str) or not client_id:
                raise WireError("client_id must be a non-empty string")
            payload["client_id"] = client_id
        with self._session() as sock:
            response = self._exchange(sock, payload)
        return response["message"]

    def reply(
        self,
        message_id: int,
        body: str,
        *,
        channel: str = DEFAULT_CHANNEL,
        client_id: str | None = None,
    ) -> dict[str, Any]:
        """Post a threaded reply to an existing message in ``channel``."""
        return self.post(body, channel=channel, reply_to=message_id, client_id=client_id)

    def read_since(
        self,
        after: int,
        *,
        channel: str = DEFAULT_CHANNEL,
        limit: int = DEFAULT_READ_LIMIT,
    ) -> list[dict[str, Any]]:
        """Read messages with canonical id > ``after``, without cursor use."""
        _validated_nonnegative_int(after, "after")
        with self._session() as sock:
            response = self._exchange(
                sock, {"op": "read", "channel": channel, "after": after, "limit": limit}
            )
        return response["messages"]

    def read_unseen(
        self,
        *,
        channel: str = DEFAULT_CHANNEL,
        limit: int = DEFAULT_READ_LIMIT,
        advance: bool = True,
    ) -> dict[str, Any]:
        """Read this seat's unseen messages in canonical id order.

        Mirrors the wire CLI's ``read --advance`` contract: fetch the seat's
        saved cursor, read everything after it, then (when ``advance``) move
        the cursor to the last message actually returned. Messages arriving
        between the read and the advance remain unseen. Pass ``advance=False``
        to peek without consuming.
        """
        with self._session() as sock:
            cursor_response = self._exchange(sock, {"op": "cursor", "channel": channel})
            after = cursor_response["last_message_id"]
            read_response = self._exchange(
                sock, {"op": "read", "channel": channel, "after": after, "limit": limit}
            )
            messages = read_response["messages"]
            cursor = after
            advanced = False
            if advance and messages:
                advance_response = self._exchange(
                    sock,
                    {
                        "op": "advance",
                        "channel": channel,
                        "last_message_id": messages[-1]["id"],
                    },
                )
                cursor = advance_response["last_message_id"]
                advanced = True
        return {"messages": messages, "cursor": cursor, "advanced": advanced}

    def cursor(self, *, channel: str = DEFAULT_CHANNEL) -> int:
        """Return this seat's saved cursor for ``channel`` (0 if never set)."""
        with self._session() as sock:
            response = self._exchange(sock, {"op": "cursor", "channel": channel})
        return response["last_message_id"]

    def advance(self, *, channel: str = DEFAULT_CHANNEL, last_message_id: int) -> int:
        """Monotonically advance this seat's cursor; the relay never regresses it."""
        _validated_nonnegative_int(last_message_id, "last_message_id")
        with self._session() as sock:
            response = self._exchange(
                sock,
                {"op": "advance", "channel": channel, "last_message_id": last_message_id},
            )
        return response["last_message_id"]

    def channels(self) -> list[dict[str, Any]]:
        """List channels with message counts and latest canonical ids."""
        with self._session() as sock:
            response = self._exchange(sock, {"op": "channels"})
        return response["channels"]