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
import threading
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

#: Incoming responses AND watch events, including their newline terminator.
MAX_RESPONSE_FRAME_BYTES = 131_072

#: Bounded channel enumeration: refuse incomplete inventories at this bound.
MAX_CHANNELS_LIMIT = 500
MAX_CHANNEL_PAGES = 100

#: The relay rejects read limits above 200; pass-through, surfaced truthfully.
MAX_READ_LIMIT = 200

#: W1: wake/session listing page bound (mirrors the relay's own cap; the
#: relay byte-bounds every page against the transport frame limit and
#: returns truthful continuation cursors).
MAX_WAKE_LIST_LIMIT = 100

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
        # R3/O4 (Sonnet P7): registry of every live client socket so a
        # shutdown hook can interrupt ALL blocked recvs (watch + any RPC in
        # flight — heartbeat/ack/claim on their own short-lived sockets),
        # not just the watch socket. A relay that accepts and never answers
        # must not hold a listener thread past the shutdown join cap.
        self._live_sockets: set[socket.socket] = set()
        self._live_sockets_lock = threading.Lock()

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

    @staticmethod
    def _iter_frames(
        sock: socket.socket, *, max_frame: int = MAX_RESPONSE_FRAME_BYTES, idle_timeout: float
    ) -> Iterator[bytes]:
        """Yield complete newline-delimited frames from a streaming socket.

        Handles both failure modes a bare ``recv()`` reader has:

        - **fragmented** — one frame split across several ``recv`` returns;
        - **coalesced** — several frames arriving in one ``recv`` (exactly
          what the relay emits when it replays multiple pending wakes
          back-to-back with no await between writes).

        A persistent buffer is carried across reads and split on ``\\n``;
        complete frames are yielded one at a time and the remainder stays
        buffered for the next frame. R2/B2.
        """
        buf = bytearray()
        while True:
            nl = buf.find(b"\n")
            if nl != -1:
                if nl + 1 > max_frame:
                    raise WireFrameError(f"relay frame exceeds {max_frame} bytes")
                frame = bytes(buf[:nl])
                del buf[: nl + 1]
                yield frame
                continue
            if len(buf) >= max_frame:
                raise WireFrameError(
                    f"frame exceeds {max_frame} bytes without a newline terminator"
                )
            try:
                chunk = sock.recv(65_536)
            except socket.timeout as exc:
                raise WireUnavailableError(
                    f"AIvengers Wire relay at {sock} did not deliver a frame "
                    f"within the {idle_timeout:g}s receive timeout"
                ) from exc
            if not chunk:
                if buf:
                    raise WireFrameError(
                        "relay closed the connection mid-frame (incomplete frame buffered)"
                    )
                return
            buf.extend(chunk)

    def _recv_frame(self, sock: socket.socket) -> dict[str, Any]:
        frames = self._iter_frames(sock, idle_timeout=self._timeout)
        try:
            raw = next(frames)
        except StopIteration:
            raise WireFrameError("relay closed the connection without a full frame") from None
        return self._decode_frame(raw)

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

    def _track_socket(self, sock: socket.socket) -> None:
        with self._live_sockets_lock:
            self._live_sockets.add(sock)

    def _untrack_socket(self, sock: socket.socket) -> None:
        with self._live_sockets_lock:
            self._live_sockets.discard(sock)

    def interrupt_sockets(self) -> None:
        """Close every live client socket (R3/O4 shutdown hook).

        Best-effort and idempotent: a blocked ``recv`` on any of them
        returns/raises immediately, which unblocks whichever listener thread
        was waiting. Safe to call from another thread while RPCs run.
        """
        with self._live_sockets_lock:
            sockets = list(self._live_sockets)
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    @contextmanager
    def _session(self) -> Iterator[socket.socket]:
        """Open an authenticated connection; always close it on exit."""
        sock = self._connect()
        self._track_socket(sock)
        try:
            sock.sendall(
                self._encode_frame({"op": "auth", "seat": SEAT, "token": self._load_token()})
            )
            self._check(self._recv_frame(sock))
            yield sock
        finally:
            self._untrack_socket(sock)
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
        """Read up to ``limit`` messages after ``after``, without cursor use.

        Follow byte-trimmed pages until the requested count or end of history.
        Older relays without continuation metadata return their one complete
        count-limited response. Malformed/incomplete reads raise WireError;
        partial lists are never presented as successful reads.
        """
        _validated_nonnegative_int(after, "after")
        with self._session() as sock:
            return self._read_messages(sock, after=after, channel=channel, limit=limit)

    @staticmethod
    def _listing_page(
        response: dict[str, Any], *, rows_key: str, key: str,
        after: int | str | None, limit: int, allow_legacy: bool,
    ) -> tuple[list[dict[str, Any]], bool | None, int | str | None]:
        """Validate keyset order and continuation before using any page.

        A continuation must name exactly the last included row, never an
        unseen row. Global message id gaps are legal; only order matters.
        None for has_more means an older, metadata-free protocol response.
        """
        rows = response.get(rows_key)
        if not isinstance(rows, list) or len(rows) > limit:
            raise WireFrameError(f"invalid {rows_key} page")
        previous = after
        for row in rows:
            value = row.get(key) if isinstance(row, dict) else None
            if key == "id":
                valid = type(value) is int and value > 0
            else:
                valid = (
                    isinstance(value, str) and 1 <= len(value) <= 64
                    and all(c in "abcdefghijklmnopqrstuvwxyz0123456789._-" for c in value)
                    and value[0].isalnum() and value[-1].isalnum()
                )
            if not valid or (previous is not None and value <= previous):
                raise WireFrameError(f"invalid or non-increasing {rows_key} key")
            previous = value
        if "has_more" not in response and "next_after" not in response and allow_legacy:
            return rows, None, None
        more = response.get("has_more")
        cursor = response.get("next_after")
        if type(more) is not bool or "next_after" not in response:
            raise WireFrameError(f"invalid {rows_key} continuation metadata")
        if more:
            # Exact type also excludes bool cursors masquerading as id 1.
            if not rows or type(cursor) is not type(previous) or cursor != previous:
                raise WireFrameError(f"invalid or stalled {rows_key} continuation cursor")
        elif cursor is not None:
            raise WireFrameError(f"unexpected terminal {rows_key} cursor")
        return rows, more, cursor

    def _read_messages(
        self, sock: socket.socket, *, after: int, channel: str, limit: int,
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for page_index in range(MAX_READ_LIMIT):
            # Keep invalid caller limits relay-validated, as in the public V0 API.
            remaining = limit if not messages else limit - len(messages)
            response = self._exchange(
                sock, {"op": "read", "channel": channel, "after": after, "limit": remaining}
            )
            if type(limit) is not int or not 1 <= limit <= MAX_READ_LIMIT:
                raise WireFrameError("relay accepted an invalid read limit")
            rows, more, cursor = self._listing_page(
                response, rows_key="messages", key="id", after=after,
                limit=remaining, allow_legacy=page_index == 0,
            )
            messages.extend(rows)
            if more is not True or len(messages) == limit:
                return messages
            after = cursor
        raise WireFrameError("message pagination exceeded its page bound")

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
            after = cursor_response.get("last_message_id")
            if type(after) is not int or after < 0:
                raise WireFrameError("invalid durable message cursor")
            messages = self._read_messages(sock, after=after, channel=channel, limit=limit)
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
        """List all channels, following at most 100 pages of up to 500 rows.

        Old relays rejecting ``limit`` get one legacy request on a fresh
        connection. Any failure or page-bound exhaustion raises WireError
        rather than returning an apparently complete partial inventory.
        """
        return self._channels_paged()

    def _channels_legacy(self) -> list[dict[str, Any]]:
        # A protocol refusal closes the old relay connection. Never reuse it.
        with self._session() as sock:
            response = self._exchange(sock, {"op": "channels"})
        rows, more, _ = self._listing_page(
            response, rows_key="channels", key="channel", after=None,
            limit=MAX_RESPONSE_FRAME_BYTES, allow_legacy=True,
        )
        if more is True:
            raise WireFrameError("legacy channel response is incomplete")
        return rows

    def _channels_paged(self) -> list[dict[str, Any]]:
        channels: list[dict[str, Any]] = []
        after = None
        with self._session() as sock:
            for page_index in range(MAX_CHANNEL_PAGES):
                payload: dict[str, Any] = {"op": "channels", "limit": MAX_CHANNELS_LIMIT}
                if after is not None:
                    payload["after"] = after
                try:
                    response = self._exchange(sock, payload)
                except WireProtocolError as exc:
                    if (
                        page_index == 0 and exc.error_code == "invalid_request"
                        and exc.message == "unknown fields: limit"
                    ):
                        return self._channels_legacy()
                    raise
                rows, more, cursor = self._listing_page(
                    response, rows_key="channels", key="channel", after=after,
                    limit=MAX_CHANNELS_LIMIT, allow_legacy=False,
                )
                channels.extend(rows)
                if more is False:
                    return channels
                after = cursor
        raise WireFrameError("channel pagination exceeded its page bound")

    # ── W1: wake-adapter session operations ───────────────────────────────

    def register_session(
        self,
        *,
        adapter: str,
        capabilities: dict[str, bool] | None = None,
        lease_seconds: int | None = None,
        model_claim: str | None = None,
        platform_pid: int | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Register one adapter session owned by the fixed ``lumina`` seat.

        A session is a specific adapter instance (this GUI runtime), never a
        model claim and never an OS process id. The relay assigns the
        session id and generation; the lease must be kept alive by periodic
        :meth:`heartbeat_session` calls or the session honestly expires.
        """
        if not isinstance(adapter, str) or not adapter.strip():
            raise WireError("adapter must be a non-empty string")
        payload: dict[str, Any] = {"op": "register", "adapter": adapter.strip()}
        if capabilities is not None:
            if not isinstance(capabilities, dict):
                raise WireError("capabilities must be an object")
            payload["capabilities"] = capabilities
        if lease_seconds is not None:
            if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds <= 0:
                raise WireError("lease_seconds must be a positive integer")
            payload["lease_seconds"] = lease_seconds
        if model_claim is not None:
            if not isinstance(model_claim, str) or not model_claim.strip():
                raise WireError("model_claim must be a non-empty string when present")
            payload["model_claim"] = model_claim.strip()
        if platform_pid is not None:
            if isinstance(platform_pid, bool) or not isinstance(platform_pid, int) or platform_pid <= 0:
                raise WireError("platform_pid must be a positive integer when present")
            payload["platform_pid"] = platform_pid
        if meta is not None:
            if not isinstance(meta, dict):
                raise WireError("meta must be an object")
            payload["meta"] = meta
        with self._session() as sock:
            response = self._exchange(sock, payload)
        return response["session"]

    def heartbeat_session(
        self, session_id: str, *, state: str | None = None
    ) -> dict[str, Any]:
        """Renew a session's lease (and optionally update ready/busy state)."""
        if not isinstance(session_id, str) or not session_id:
            raise WireError("session_id must be a non-empty string")
        payload: dict[str, Any] = {"op": "heartbeat", "session_id": session_id}
        if state is not None:
            if state not in ("ready", "busy", "draining"):
                raise WireError("state must be one of ready, busy, draining")
            payload["state"] = state
        with self._session() as sock:
            response = self._exchange(sock, payload)
        return response["session"]

    def unregister_session(self, session_id: str) -> dict[str, Any]:
        """Mark a session offline; its lease and live state end immediately."""
        if not isinstance(session_id, str) or not session_id:
            raise WireError("session_id must be a non-empty string")
        with self._session() as sock:
            response = self._exchange(
                sock, {"op": "unregister", "session_id": session_id}
            )
        return response["session"]

    def sessions_page(
        self,
        *,
        limit: int = MAX_WAKE_LIST_LIMIT,
        after_registered_at: str | None = None,
        after_session_id: str | None = None,
    ) -> dict[str, Any]:
        """One bounded session-listing page with truthful continuation."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise WireError("limit must be a positive integer")
        if limit > MAX_WAKE_LIST_LIMIT:
            raise WireError(f"limit must not exceed {MAX_WAKE_LIST_LIMIT}")
        payload: dict[str, Any] = {"op": "sessions", "limit": limit}
        if (after_registered_at is None) != (after_session_id is None):
            raise WireError(
                "after_registered_at and after_session_id must be given together"
            )
        if after_registered_at is not None:
            payload["after_registered_at"] = after_registered_at
            payload["after_session_id"] = after_session_id
        with self._session() as sock:
            response = self._exchange(sock, payload)
        return response

    def wake_list_page(
        self,
        *,
        state: str | None = None,
        limit: int = MAX_WAKE_LIST_LIMIT,
        after_created_at: str | None = None,
        after_wake_id: str | None = None,
    ) -> dict[str, Any]:
        """One bounded wake-listing page with truthful continuation cursors.

        The relay byte-bounds every page against the transport frame limit;
        dropped rows stay reachable through ``next_after``. This is the
        adapter's ONLY catch-up path: the message ``read`` op is deliberately
        unused because it bounds count but not serialized bytes.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise WireError("limit must be a positive integer")
        if limit > MAX_WAKE_LIST_LIMIT:
            raise WireError(f"limit must not exceed {MAX_WAKE_LIST_LIMIT}")
        payload: dict[str, Any] = {"op": "wake_list", "limit": limit}
        if state is not None:
            payload["state"] = state
        if (after_created_at is None) != (after_wake_id is None):
            raise WireError("after_created_at and after_wake_id must be given together")
        if after_created_at is not None:
            payload["after_created_at"] = after_created_at
            payload["after_wake_id"] = after_wake_id
        with self._session() as sock:
            response = self._exchange(sock, payload)
        return response

    def wake_list_iter(
        self,
        *,
        state: str | None = None,
        limit: int = MAX_WAKE_LIST_LIMIT,
        max_pages: int = 100,
    ) -> Iterator[dict[str, Any]]:
        """Walk the entire wake backlog page by page via truthful cursors.

        ``max_pages`` is a runaway guard, not a data bound: a healthy relay
        terminates on ``has_more=false`` long before it.
        """
        cursor: dict[str, str] | None = None
        for _ in range(max_pages):
            page = self.wake_list_page(
                state=state,
                limit=limit,
                after_created_at=cursor["created_at"] if cursor else None,
                after_wake_id=cursor["wake_id"] if cursor else None,
            )
            yield page
            if not page["has_more"] or not page["next_after"]:
                return
            cursor = page["next_after"]
        raise WireError(
            f"wake_list_iter exceeded {max_pages} pages; cursor did not terminate"
        )

    def wake_status(self, wake_id: str) -> dict[str, Any]:
        """Fetch one wake's exact pipeline position (target/requester only)."""
        if not isinstance(wake_id, str) or not wake_id:
            raise WireError("wake_id must be a non-empty string")
        with self._session() as sock:
            response = self._exchange(sock, {"op": "wake_status", "wake_id": wake_id})
        return response["wake"]

    def wake_ack(self, wake_id: str, session_id: str) -> dict[str, Any]:
        """Adapter receipt of a wake intent. NOT a resume, NOT execution.

        The relay enforces target/lease/capability fencing server-side and
        returns the updated wake record truthfully.
        """
        if not isinstance(wake_id, str) or not wake_id:
            raise WireError("wake_id must be a non-empty string")
        if not isinstance(session_id, str) or not session_id:
            raise WireError("session_id must be a non-empty string")
        with self._session() as sock:
            response = self._exchange(
                sock,
                {"op": "wake_ack", "wake_id": wake_id, "session_id": session_id},
            )
        return response["wake"]

    def claim_wake(self, wake_id: str, session_id: str) -> dict[str, Any]:
        """Coordination claim of a wake's task for this session, exactly once.

        The relay enforces lease validity, task-open state and exactly-once
        semantics server-side; losers receive an honest refusal, never a
        duplicate claim. A claim is a coordination fact on the Wire's task
        table — it does NOT execute work and must never be reachable from a
        model-facing tool path.
        """
        if not isinstance(wake_id, str) or not wake_id:
            raise WireError("wake_id must be a non-empty string")
        if not isinstance(session_id, str) or not session_id:
            raise WireError("session_id must be a non-empty string")
        with self._session() as sock:
            response = self._exchange(
                sock,
                {"op": "claim", "wake_id": wake_id, "session_id": session_id},
            )
        return response

    def complete_task(
        self,
        task_ref: str,
        session_id: str,
        *,
        outcome: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Record a terminal outcome for a claimed task (owner GUI action).

        The relay enforces claimant identity and task state server-side. W1
        uses this ONLY for the owner's explicit Release control (outcome
        ``failed`` with an explicit released-without-execution note) — never
        for autonomous completion of work.
        """
        if outcome not in ("completed", "failed"):
            raise WireError("outcome must be 'completed' or 'failed'")
        if not isinstance(task_ref, str) or not task_ref:
            raise WireError("task_ref must be a non-empty string")
        if not isinstance(session_id, str) or not session_id:
            raise WireError("session_id must be a non-empty string")
        payload: dict[str, Any] = {
            "op": "complete",
            "task_ref": task_ref,
            "session_id": session_id,
            "outcome": outcome,
        }
        if note is not None:
            if not isinstance(note, str):
                raise WireError("note must be a string")
            payload["note"] = note
        with self._session() as sock:
            response = self._exchange(sock, payload)
        return response

    def watch(
        self,
        *,
        channels: list[str] | None = None,
        timeout_ms: int | None = None,
        idle_timeout_seconds: float | None = None,
        session_id: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Subscribe to relay events; yields frames until the subscription ends.

        Mirrors the relay CLI client's contract exactly: yields the
        ``subscribed`` ack, then every ``message`` / ``wake`` / ``timeout`` /
        ``overflow`` event frame. The server's ``timeout`` event and an
        ``overflow``-followed-by-close both end the iteration cleanly (the
        generator returns without raising); any other server-side close
        raises :class:`WireError`. ``session_id`` binds the subscription to
        one registered session: session-targeted wakes are delivered only to
        that session's watcher, and queued seat wakes replay immediately
        after the ack as ``wake`` frames. Events never advance cursors.
        """
        if channels is not None:
            if not channels:
                raise WireError("channels must be a non-empty list when filtering")
            for value in channels:
                if not isinstance(value, str) or not value.strip():
                    raise WireError("channel names must be non-empty strings")
        payload: dict[str, Any] = {"op": "watch"}
        if channels is not None:
            payload["channels"] = list(channels)
        if timeout_ms is not None:
            if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms <= 0:
                raise WireError("timeout_ms must be a positive integer")
            payload["timeout_ms"] = timeout_ms
        if session_id is not None:
            if not isinstance(session_id, str) or not session_id:
                raise WireError("session_id must be a non-empty string when present")
            payload["session_id"] = session_id
        token = self._load_token()
        if idle_timeout_seconds is None:
            idle_timeout_seconds = 30.0 + (timeout_ms / 1000.0 if timeout_ms else 0.0)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(idle_timeout_seconds)
        terminated = False
        # R2/H2 shutdown hook: the listener closes this socket to unblock a
        # blocking recv immediately (bounded GUI shutdown). One watch at a
        # time per client instance; cleared when the generator exits.
        self._watch_socket = sock
        self._track_socket(sock)
        try:
            sock.connect(str(self.socket_path))
            sock.sendall(
                self._encode_frame({"op": "auth", "seat": SEAT, "token": token})
            )
            auth = self._recv_frame(sock)
            self._check(auth)
            sock.sendall(self._encode_frame(payload))
            # R2/B2: ONE frame-decoder instance for the whole subscription —
            # its buffer carries coalesced/fragmented remainders across
            # frames. (Calling _iter_frames inside the loop would create a
            # fresh empty buffer per frame and silently drop everything
            # after the first coalesced batch — the exact bug the framing
            # test caught.)
            frames_iter = self._iter_frames(sock, idle_timeout=idle_timeout_seconds)
            while True:
                try:
                    raw = next(frames_iter)
                except StopIteration:
                    # R3/R2-3 (Sonnet): a clean EOF with an empty buffer used
                    # to escape watch() as ``RuntimeError: generator raised
                    # StopIteration`` (PEP 479 — StopIteration raised inside
                    # this generator). Surface the honest frame error instead,
                    # matching the one-shot reader's message.
                    raise WireFrameError(
                        "relay closed the connection without a full frame"
                    ) from None
                frame = self._decode_frame(raw)
                if frame.get("ok") is not True:
                    raise WireProtocolError(
                        str(frame.get("error", "operation_failed")),
                        str(frame.get("message", "")),
                    )
                event = frame.get("event")
                yield frame
                if event == "timeout" or event == "overflow":
                    terminated = True
                    return
        finally:
            self._watch_socket = None
            self._untrack_socket(sock)
            try:
                sock.close()
            except OSError:
                pass
            if not terminated:
                # Non-clean exits (exceptions, generator.close()) simply close
                # the socket; the relay treats the drop as a disconnect and
                # replays unacked wakes on the next subscription.
                pass

    #: R2/H2: the live watch connection while a watch() generator is active,
    #: else None. The listener's shutdown closes it to unblock recv() — this
    #: is a deliberate shutdown hook, not general-purpose API.
    _watch_socket: socket.socket | None = None

    @staticmethod
    def _decode_frame(raw: bytes) -> dict[str, Any]:
        def reject_constant(value: str) -> Any:
            raise ValueError(f"non-finite JSON number: {value}")

        try:
            value = json.loads(raw.decode("utf-8"), parse_constant=reject_constant)
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise WireFrameError(f"relay frame is not valid JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise WireFrameError("relay frame must be a JSON object")
        return value
