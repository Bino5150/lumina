"""
core/chrome_companion_hub.py -- Lumina-side Chrome Companion hub.

The trusted end of the Chrome Companion bridge. Listens on a private Unix
socket (0600, inside an owner-only 0700 runtime dir; no TCP, no localhost
HTTP/WebSocket) for the one native-host process Chrome launches per
extension port, authenticates it, and issues bounded reads and the two
BC-01B-A navigation requests on behalf of tools/chrome_companion.py.

Handshake (every connection, re-read from disk every time):
    peer UID (SO_PEERCRED) == this process's UID      else drop, no reply
    host hello well-formed, protocol v1               else reject bad_hello
    Chrome-supplied origin == installed origin         else reject wrong_origin
    a pairing exists                                   else reject unpaired
    paired instance_id + origin == hello's             else reject wrong_instance

Lifecycle law: A DISCONNECT CREATES FRESH LIFECYCLE STATE. Every accepted
connection is a brand-new _Connection with a new random connection_id and an
empty in-flight map. On any disconnect (host exit, extension reload/PAUSE,
Chrome restart, protocol violation, hub stop) every in-flight request fails
explicitly and the object is discarded -- it is never reused, and a response
carrying an old connection_id is dropped.

Terminal-transition law (BROWSER-COMPANION-01A-R1): every request reaches
exactly ONE terminal outcome, decided under its connection's lock -- either
the requester claims its response ("delivered") or the connection is retired
first ("cancelled"), never both. A response the reader has staged but the
requester has not yet claimed is NOT a success: retiring the connection
cancels it. A connection stops being current and is retired in the same hub
critical section, so no request can claim success from a connection that has
already been superseded, paused, stopped, or lost. There is no executor pool at all:
one daemon reader thread per connection, created fresh each time, so the
BrowserManager "cannot schedule new futures after shutdown" class cannot
occur here. stop() followed by start() builds a new listener and thread.

Welcome-before-work law (BROWSER-COMPANION-01A-R2 / N1): a connection becomes
current -- request-addressable -- only after its welcome has been written,
and only if no newer connection has been published meanwhile and the hub is
still the one that accepted it. The previous connection stays current (and
valid) until that same critical section retires it.

Reject-state law (R2 / N2): a rejection's reason is recorded before the
reject frame is sent, so no observer can see the rejection without it.

Requests are numbered per connection in wire order (R2 / P1; see
protocol.sequenced_request_id), assigned under the connection's send lock.

Nothing here falls back to Playwright (tools/browser.py) and nothing here
knows Firefox exists.

Every observation is external content. The hub re-applies the restricted-
surface policy (chrome_companion/policy.py) and drops incognito tabs even
though the extension already does, so a single-side regression cannot leak
a restricted surface. Telemetry (flight recorder) is metadata only --
operation, tab id, hashed origin, sizes, durations, result codes -- never
page text, titles, full URLs, or instance IDs; a recorder failure never
breaks a request.
"""
from __future__ import annotations

import atexit
import errno
import hashlib
import os
import secrets
import socket
import stat
import struct
import threading
import time
from pathlib import Path

from chrome_companion import policy, protocol, state

HELLO_TIMEOUT_S = 5.0
ACCEPT_POLL_S = 0.25
REJECT_TELEMETRY_INTERVAL_S = 60.0


class CompanionError(Exception):
    """A Chrome Companion call did not produce a confirmed result.

    ``code`` is stable and content-free. For an action, ``executed`` means
    dispatch may have reached Chrome; it never asserts that an effect occurred.
    ``operation_id`` identifies that one attempt for reconciliation."""

    def __init__(self, code: str, message: str, *, executed: bool = False,
                 operation_id: str | None = None):
        self.code = code
        self.message = message
        self.executed = executed
        self.operation_id = operation_id
        super().__init__(f"{code}: {message}")


def origin_hash(origin) -> str | None:
    if not isinstance(origin, str) or not origin:
        return None
    return hashlib.sha256(origin.encode("utf-8")).hexdigest()[:12]


def _default_peer_uid(sock: socket.socket) -> int:
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", raw)
    return uid


def _default_recorder():
    from core import flight_recorder
    return flight_recorder.get_recorder()


_DISCONNECT_MESSAGES = {
    "paused": "Chrome companion is PAUSED by the owner in Chrome (PAUSE LUMINA). "
              "Nothing was read. Only the owner can resume it from the extension popup.",
    "superseded": "the Chrome connection was replaced by a newer connection",
    "hub_stopped": "Lumina's Chrome companion hub stopped",
    "host_closed": "Chrome closed the companion connection (extension reloaded, "
                   "Chrome restarted, or the tab/browser went away)",
    "protocol_violation": "the companion connection was dropped after a malformed frame",
    "unpaired": "the owner removed the Chrome Companion pairing",
    "uninstalled": "the owner uninstalled Chrome Companion",
}

ACTION_OPS = frozenset({"open_owner_url", "switch_tab"})


class _Waiter:
    """One request's rendezvous. ``outcome`` is the single authority: None
    while pending, then exactly one of "delivered", "cancelled", "timeout",
    "abandoned" -- written only under the owning connection's lock, and never
    rewritten. A staged ``response`` is inert unless the outcome becomes
    "delivered"."""
    __slots__ = ("event", "response", "size", "error", "outcome", "op", "tab_id", "started")

    def __init__(self, op: str, tab_id):
        self.event = threading.Event()
        self.response = None
        self.size = 0
        self.error = None
        self.outcome = None
        self.op = op
        self.tab_id = tab_id
        self.started = time.monotonic()


class _Connection:
    """One authenticated host connection. Single-use: once closed it is dead
    and is never revived -- a reconnect is always a new _Connection."""

    def __init__(self, sock: socket.socket, *, connection_id: str, seq: int,
                 instance_fp: str, extension_version: str,
                 instance_id: str | None = None, origin: str | None = None):
        self.sock = sock
        self.connection_id = connection_id
        self.seq = seq
        self.instance_fp = instance_fp
        self.instance_id = instance_id
        self.origin = origin
        self.extension_version = extension_version
        self.opened_at = time.time()
        self.closed = False
        self.close_reason = None
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._in_flight: dict[str, _Waiter] = {}
        self._request_seq = 0  # last sequence number put on the wire

    def send(self, message: dict) -> None:
        frame = protocol.encode_frame(message, protocol.MAX_TO_EXTENSION_BYTES)
        with self._send_lock:
            self.sock.sendall(frame)

    def send_request(self, *, op: str, tab_id, args: dict, deadline_ms: int) -> tuple[str, _Waiter]:
        """Number, register and send ONE request, atomically in wire order
        (R2 / P1): the sequence number is taken under the send lock, so the
        extension -- which refuses any number not higher than the last one it
        accepted -- sees every request of this connection in strictly
        increasing order. A request that cannot be built or registered takes
        no number and leaves nothing registered."""
        with self._send_lock:
            seq = self._request_seq + 1
            request_id = protocol.sequenced_request_id(seq)
            message = protocol.build_request(connection_id=self.connection_id, request_id=request_id, op=op,
                                             tab_id=tab_id, deadline_ms=deadline_ms, args=args)
            frame = protocol.encode_frame(message, protocol.MAX_TO_EXTENSION_BYTES)
            waiter = self.register(request_id, op, tab_id)
            self._request_seq = seq
            try:
                self.sock.sendall(frame)
            except OSError:
                self.unregister(request_id)
                raise
        return request_id, waiter

    def register(self, request_id: str, op: str, tab_id) -> _Waiter:
        with self._lock:
            if self.closed:
                raise CompanionError(self.close_reason or "disconnected",
                                     _DISCONNECT_MESSAGES.get(self.close_reason, "not connected"))
            if request_id in self._in_flight:
                raise CompanionError("duplicate_request", "request id already in flight")
            if len(self._in_flight) >= protocol.MAX_IN_FLIGHT:
                raise CompanionError("busy", "too many Chrome requests in flight")
            waiter = _Waiter(op, tab_id)
            self._in_flight[request_id] = waiter
            return waiter

    def unregister(self, request_id: str) -> None:
        """The requester gave up before claiming (e.g. the send failed): any
        response that still arrives is dropped as unsolicited."""
        with self._lock:
            waiter = self._in_flight.pop(request_id, None)
            if waiter is not None and waiter.outcome is None:
                waiter.outcome = "abandoned"

    def resolve(self, message: dict, size: int) -> str:
        """STAGE a validated response on its waiter and wake the requester.
        Staging is not success: the waiter stays in flight, so retiring the
        connection before the requester claims it still cancels it. Anything
        not matching THIS connection's identity and a pending in-flight id is
        dropped."""
        if message["connection_id"] != self.connection_id:
            return "wrong_connection"
        with self._lock:
            if self.closed:
                return "connection_closed"
            waiter = self._in_flight.get(message["request_id"])
            if waiter is None or waiter.outcome is not None or waiter.response is not None:
                return "unsolicited"
            waiter.response = message
            waiter.size = size
        waiter.event.set()
        return "ok"

    def claim(self, request_id: str, waiter: _Waiter, *, answered: bool, timeout_s: float) -> dict:
        """The requester's terminal transition, mutually exclusive with
        retire(): a staged response becomes a delivered success only if this
        connection has not been retired first. This -- not the wake-up -- is
        the linearization point of a successful request."""
        with self._lock:
            self._in_flight.pop(request_id, None)
            if waiter.outcome is None:
                waiter.outcome = "delivered" if answered and waiter.response is not None else "timeout"
        if waiter.outcome == "delivered":
            return waiter.response
        if waiter.outcome == "cancelled":
            raise waiter.error
        raise CompanionError("timeout", f"Chrome did not answer within {timeout_s:g}s")

    def retire(self, reason: str) -> list[_Waiter]:
        """End this connection's validity: the ONE state transition that makes
        it dead. Under the lock, every waiter without a terminal outcome --
        including one whose response is staged but not yet claimed -- is
        cancelled. Returns the cancelled waiters; the caller wakes them
        outside every lock. Idempotent."""
        error_code = reason if reason in _DISCONNECT_MESSAGES else "disconnected"
        message = _DISCONNECT_MESSAGES.get(reason, "the Chrome companion disconnected mid-request")
        with self._lock:
            if self.closed:
                return []
            self.closed = True
            self.close_reason = reason
            cancelled = []
            for waiter in self._in_flight.values():
                if waiter.outcome is None:
                    waiter.outcome = "cancelled"
                    waiter.error = CompanionError(error_code, message)
                    cancelled.append(waiter)
            self._in_flight.clear()
            return cancelled

    def close(self, reason: str) -> None:
        """retire() + wake the cancelled requesters + drop the socket."""
        self.release(self.retire(reason))

    def release(self, cancelled: list[_Waiter]) -> None:
        for waiter in cancelled:
            waiter.event.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class ChromeCompanionHub:
    def __init__(self, data_dir, *, recorder_fn=None, peer_uid_fn=None):
        self.data_dir = str(data_dir)
        self._recorder_fn = recorder_fn or _default_recorder
        self._peer_uid = peer_uid_fn or _default_peer_uid
        self._lock = threading.Lock()
        self._listener: socket.socket | None = None
        self._socket_path: str | None = None
        self._socket_ino = None
        self._stop_event: threading.Event | None = None
        self._accept_thread: threading.Thread | None = None
        self._current: _Connection | None = None
        self._conn_seq = 0
        self._published_seq = 0  # highest seq ever made current (R2 / N1)
        self._last_disconnect = None
        self._last_reject = None
        self._last_reject_recorded = 0.0
        self.start_error: str | None = None

    # -- telemetry -------------------------------------------------------

    def _record(self, event_type: str, severity: str = "info", **fields) -> None:
        try:
            self._recorder_fn().record_machine_event(event_type, severity=severity, fields=fields)
        except Exception:
            pass  # a recorder failure must never break a browser command

    # -- lifecycle -------------------------------------------------------

    @property
    def listening(self) -> bool:
        return self._listener is not None

    def start(self) -> None:
        with self._lock:
            if self._listener is not None:
                return
            install = state.load_install(self.data_dir)
            if install is None:
                raise CompanionError("not_installed", "Chrome companion is not installed")
            path = install.socket_path
            state.ensure_private_dir(Path(path).parent)
            self._prepare_socket_path(path)
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            old_umask = os.umask(0o177)  # socket file is born 0600, no chmod window
            try:
                listener.bind(path)
            except OSError:
                listener.close()
                raise
            finally:
                os.umask(old_umask)
            os.chmod(path, 0o600)
            listener.listen(4)
            listener.settimeout(ACCEPT_POLL_S)
            stop_event = threading.Event()
            self._listener = listener
            self._socket_path = path
            self._socket_ino = os.lstat(path).st_ino
            self._stop_event = stop_event
            self._accept_thread = threading.Thread(
                target=self._accept_loop, args=(listener, stop_event), daemon=True,
                name="chrome-companion-accept")
            self._accept_thread.start()
            self.start_error = None
        self._record("chrome_companion.hub", state="listening")

    def stop(self) -> None:
        with self._lock:
            listener, stop_event = self._listener, self._stop_event
            thread, conn = self._accept_thread, self._current
            path, ino = self._socket_path, self._socket_ino
            self._listener = self._stop_event = self._accept_thread = None
            retired = self._retire_locked(conn, "hub_stopped") if conn is not None else None
        if retired is not None:
            self._finish_retirement(conn, "hub_stopped", retired)
        if listener is None:
            return
        stop_event.set()
        try:
            listener.close()
        except OSError:
            pass
        try:
            info = os.lstat(path)
            if stat.S_ISSOCK(info.st_mode) and info.st_ino == ino:
                os.unlink(path)
        except FileNotFoundError:
            pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._record("chrome_companion.hub", state="stopped")

    @staticmethod
    def _prepare_socket_path(path: str) -> None:
        try:
            mode = os.lstat(path).st_mode
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(mode):
            raise CompanionError("hub_unavailable", f"refusing to replace non-socket path {path}")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.25)
            probe.connect(path)
        except OSError as exc:
            if exc.errno not in {errno.ECONNREFUSED, errno.ENOENT}:
                raise CompanionError("hub_unavailable", f"cannot inspect existing socket: {exc}") from exc
        else:
            raise CompanionError("hub_unavailable",
                                 "another Lumina process already owns the Chrome companion socket")
        finally:
            probe.close()
        os.unlink(path)

    def _accept_loop(self, listener: socket.socket, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                sock, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._serve, args=(sock, stop_event), daemon=True,
                             name="chrome-companion-conn").start()

    # -- handshake -------------------------------------------------------

    def _reject(self, sock: socket.socket, reason: str, *, reply: bool = True,
                instance_id: str | None = None) -> None:
        # REJECT STATE BEFORE THE REJECTION IS OBSERVABLE (R2 / N2): the
        # reason is recorded first, so once the peer can see the reject, every
        # request already reports it. Socket I/O and telemetry stay outside
        # the lock.
        fp = state.instance_fingerprint(instance_id) if instance_id else None
        now = time.time()
        with self._lock:
            previous = self._last_reject
            self._last_reject = {"reason": reason, "at": now, "instance_fp": fp}
            throttle = (previous is not None and previous["reason"] == reason
                        and now - self._last_reject_recorded < REJECT_TELEMETRY_INTERVAL_S)
            if not throttle:
                self._last_reject_recorded = now
        if reply:
            try:
                sock.sendall(protocol.encode_frame(
                    {"v": protocol.PROTOCOL_VERSION, "type": "reject", "reason": reason},
                    protocol.MAX_TO_EXTENSION_BYTES))
            except (OSError, protocol.ProtocolError):
                pass
        try:
            sock.close()
        except OSError:
            pass
        if not throttle:
            self._record("chrome_companion.connection", severity="warning",
                         state="rejected", reason=reason, instance_fp=fp)

    def _authorize(self, hello: dict) -> str | None:
        try:
            install = state.load_install(self.data_dir)
        except state.StateError:
            return "not_installed"
        if install is None:
            return "not_installed"
        if hello["origin"] != install.extension_origin:
            return "wrong_origin"
        try:
            pairing = state.load_pairing(self.data_dir)
        except state.StateError:
            return "unpaired"
        if pairing is None:
            return "unpaired"
        if pairing.extension_origin != hello["origin"] or pairing.instance_id != hello["instance_id"]:
            return "wrong_instance"
        return None

    def _serve(self, sock: socket.socket, stop_event: threading.Event) -> None:
        try:
            sock.settimeout(HELLO_TIMEOUT_S)
            if self._peer_uid(sock) != os.getuid():
                self._reject(sock, "wrong_peer", reply=False)
                return
            hello = protocol.read_frame(sock.recv, protocol.MAX_FROM_EXTENSION_BYTES)
            if hello is None:
                sock.close()
                return
            if hello.get("type") == "owner_revoke":
                self._serve_owner_revoke(sock, hello)
                return
            protocol.validate_host_hello(hello)
        except protocol.ProtocolError as exc:
            self._reject(sock, "unsupported_version" if exc.code == "unsupported_version" else "bad_hello")
            return
        except OSError:
            try:
                sock.close()
            except OSError:
                pass
            return
        verdict = self._authorize(hello)
        if verdict is not None:
            self._reject(sock, verdict, instance_id=hello["instance_id"])
            return

        with self._lock:
            if stop_event.is_set() or self._stop_event is not stop_event:
                conn = None
            else:
                self._conn_seq += 1
                conn = _Connection(sock, connection_id=secrets.token_hex(16), seq=self._conn_seq,
                                   instance_fp=state.instance_fingerprint(hello["instance_id"]),
                                   extension_version=hello["extension_version"],
                                   instance_id=hello["instance_id"], origin=hello["origin"])
        if conn is None:
            self._reject(sock, "shutting_down")
            return
        # WELCOME BEFORE WORK (R2 / N1): nothing can address this connection
        # until its welcome is on the wire, so no request can overtake it.
        try:
            conn.send({"v": protocol.PROTOCOL_VERSION, "type": "welcome",
                       "connection_id": conn.connection_id,
                       "limits": {"max_text_chars": protocol.MAX_TEXT_CHARS,
                                  "max_links": protocol.MAX_LINKS, "max_tabs": protocol.MAX_TABS}})
        except (OSError, protocol.ProtocolError):
            self._abandon(conn, "host_closed")
            return
        with self._lock:
            if stop_event.is_set() or self._stop_event is not stop_event:
                refused = "hub_stopped"
            elif conn.seq <= self._published_seq:
                refused = "superseded"  # a newer connection was published first
            else:
                refused = None
                self._published_seq = conn.seq
                previous, self._current = self._current, conn
                # The superseded connection dies in the SAME critical section
                # that replaces it: there is no instant at which it is no
                # longer current yet could still deliver a success.
                superseded = self._retire_locked(previous, "superseded") if previous is not None else None
        if refused is not None:
            self._abandon(conn, refused)
            return
        if superseded is not None:
            self._finish_retirement(previous, "superseded", superseded)
        reason = "host_closed"
        try:
            sock.settimeout(None)
            self._record("chrome_companion.connection", state="ready", connection_seq=conn.seq,
                         instance_fp=conn.instance_fp, extension_version=conn.extension_version)
            reason = self._read_loop(conn)
        except (OSError, protocol.ProtocolError):
            reason = "host_closed" if not conn.closed else (conn.close_reason or "host_closed")
        finally:
            with self._lock:
                retired = self._retire_locked(conn, reason)
            self._finish_retirement(conn, reason, retired)

    def _abandon(self, conn: _Connection, reason: str) -> None:
        """A connection that never became current: retire it (under the hub
        lock, like every retirement) without touching the current
        connection's disconnect note -- it was never "the" connection."""
        with self._lock:
            cancelled = conn.retire(reason)
        conn.release(cancelled)
        self._record("chrome_companion.connection", severity="warning", state="not_published",
                     reason=reason, connection_seq=conn.seq)

    def _serve_owner_revoke(self, sock: socket.socket, message: dict) -> None:
        """Retire the connection when same-UID owner CLI removed its authority."""
        try:
            if message.get("v") != protocol.PROTOCOL_VERSION or set(message) != {"v", "type", "reason"} \
                    or message["reason"] not in {"unpaired", "uninstalled"}:
                return
            install = state.load_install(self.data_dir)
            pairing = state.load_pairing(self.data_dir)
            if install is not None and pairing is not None:
                return  # never let a notification alone remove live authority
            with self._lock:
                conn = self._current
                retired = self._retire_locked(conn, message["reason"]) if conn is not None else None
            if retired is not None:
                self._finish_retirement(conn, message["reason"], retired)
            sock.sendall(protocol.encode_frame({"v": protocol.PROTOCOL_VERSION,
                "type": "owner_revoke_ack", "ok": True}, protocol.MAX_TO_EXTENSION_BYTES))
        except (OSError, state.StateError, protocol.ProtocolError):
            pass
        finally:
            sock.close()

    def _read_loop(self, conn: _Connection) -> str:
        while True:
            try:
                message, size = protocol.read_frame_sized(conn.sock.recv, protocol.MAX_FROM_EXTENSION_BYTES)
            except protocol.ProtocolError:
                return "protocol_violation"
            except OSError:
                return conn.close_reason or "host_closed"
            if message is None:
                return conn.close_reason or "host_closed"
            kind = message.get("type")
            try:
                if kind == "response":
                    protocol.validate_response(message)
                elif kind == "bye":
                    protocol.validate_bye(message)
                else:
                    return "protocol_violation"
            except protocol.ProtocolError:
                return "protocol_violation"
            if kind == "bye":
                if message["connection_id"] == conn.connection_id:
                    return message["reason"]
                continue
            outcome = conn.resolve(message, size)
            if outcome != "ok":
                self._record("chrome_companion.response_dropped", severity="warning",
                             reason=outcome, connection_seq=conn.seq)

    def _retire_locked(self, conn: _Connection, reason: str) -> tuple[list[_Waiter], bool]:
        """Caller holds self._lock. Stop ``conn`` being current, record why,
        and retire it (cancelling every unclaimed waiter) as ONE transition,
        so nothing can observe the connection gone without its reason (e.g.
        the owner's PAUSE) or claim a success from it once it is gone.
        Returns (cancelled waiters, whether this note is new)."""
        if self._current is conn:
            self._current = None
        # Each connection's disconnect is noted once; a late note from an
        # older (superseded) connection never overwrites a newer one.
        noted = self._last_disconnect is None or self._last_disconnect.get("seq", 0) < conn.seq
        if noted:
            self._last_disconnect = {"reason": reason, "at": time.time(), "seq": conn.seq}
        return conn.retire(reason), noted

    def _finish_retirement(self, conn: _Connection, reason: str, retired: tuple[list[_Waiter], bool]) -> None:
        """Outside every lock: wake the cancelled requesters, drop the socket,
        and emit the (once-per-connection) disconnect telemetry."""
        cancelled, noted = retired
        conn.release(cancelled)
        if noted:
            self._record("chrome_companion.connection", severity="info" if reason == "paused" else "warning",
                         state="disconnected", reason=reason, connection_seq=conn.seq,
                         duration_s=round(time.time() - conn.opened_at, 3))

    # -- requests --------------------------------------------------------

    def _unavailable(self, conn: _Connection | None = None) -> CompanionError:
        if not self.listening:
            return CompanionError("hub_unavailable", self.start_error or
                                  "Lumina's Chrome companion hub is not running")
        last = self._last_disconnect
        # A connection that just died carries its own reason even before the
        # serving thread has recorded it.
        reason = conn.close_reason if conn is not None and conn.closed else (last or {}).get("reason")
        if reason == "paused":
            return CompanionError("paused", _DISCONNECT_MESSAGES["paused"])
        reject = self._last_reject
        if reject is not None and reject["reason"] in {"unpaired", "wrong_instance"}:
            return CompanionError("not_paired",
                                  "the Chrome extension instance trying to connect is not the paired one; "
                                  "the owner must pair it (scripts/chrome_companion_setup.py pair <id>)")
        return CompanionError("not_connected",
                              "Lumina's Chrome is not connected (Chrome closed, extension not loaded, "
                              "or reconnect pending)")

    def current_connection_id(self) -> str | None:
        conn = self._current
        return conn.connection_id if conn is not None and not conn.closed else None

    def request(self, op: str, *, tab_id=None, args: dict | None = None, timeout_s: float = 10.0) -> dict:
        """Issue one bounded request and return a validated response.

        Action failures after possible dispatch retain an operation ID and
        ambiguity marker. Nothing is retried or routed to another browser.
        """
        with self._lock:
            conn = self._current
        if conn is None or conn.closed:
            error = self._unavailable(conn)
            self._record("chrome_companion.request", severity="warning", op=op, tab_id=tab_id,
                         result=error.code)
            raise error
        if op in ACTION_OPS:
            if self._authorize({"origin": conn.origin, "instance_id": conn.instance_id}) is not None:
                with self._lock:
                    retired = self._retire_locked(conn, "unpaired")
                self._finish_retirement(conn, "unpaired", retired)
                raise CompanionError("unpaired", "Chrome Companion pairing is no longer active")
            if conn.extension_version != "0.2.0":
                raise CompanionError("unsupported_version", "reload the BC-01B-A Chrome extension")
        started = time.monotonic()
        outcome, observed_origin, truncated = "error", None, None
        request_id = waiter = None
        sent = False
        worker_rejected = False
        try:
            try:
                request_id, waiter = conn.send_request(
                    op=op, tab_id=tab_id, args=dict(args or {}),
                    deadline_ms=int((time.time() + timeout_s) * 1000))
                sent = True
            except OSError as exc:
                raise CompanionError("disconnected", "could not reach Chrome",
                                     executed=op in ACTION_OPS) from exc
            answered = waiter.event.wait(timeout_s)
            # Being woken is not success: claim() is the single terminal
            # transition, and fails if the connection was retired first.
            response = conn.claim(request_id, waiter, answered=answered, timeout_s=timeout_s)
            if response.get("observed"):
                observed_origin = response["observed"].get("origin")
            truncated = response["truncated"]
            if not response["ok"]:
                error = response["error"]
                worker_rejected = True  # worker action errors occur before its dispatch boundary
                raise CompanionError(error["code"], error["message"])
            if tab_id is not None and response["tab_id"] != tab_id:
                raise CompanionError("tab_mismatch", "Chrome answered for a different tab")
            try:
                response["result"] = protocol.validate_result(op, response["result"])
            except protocol.ProtocolError as exc:
                raise CompanionError("malformed_response", f"invalid {op} result ({exc.code})") from exc
            if op in ACTION_OPS and response["result"]["operation_id"] != f"{conn.connection_id}:{request_id}":
                raise CompanionError("malformed_response", "Chrome returned the wrong operation identity")
            self._apply_policy(op, response)
            outcome = "ok"
            return response
        except CompanionError as exc:
            if op in ACTION_OPS and sent and not worker_rejected:
                exc.executed = True  # possible dispatch, never proof of an effect
            if op in ACTION_OPS and request_id is not None:
                exc.operation_id = f"{conn.connection_id}:{request_id}"
            outcome = exc.code
            raise
        finally:
            if request_id is not None:
                conn.unregister(request_id)
            self._record("chrome_companion.request",
                         severity="info" if outcome == "ok" else "warning",
                         op=op, tab_id=tab_id, result=outcome, connection_seq=conn.seq,
                         origin_hash=origin_hash(observed_origin), truncated=truncated,
                         response_bytes=waiter.size if waiter is not None else 0,
                         duration_ms=round((time.monotonic() - started) * 1000, 1))

    @staticmethod
    def _filter_tab(tab):
        """Hub-side defense in depth over the extension's own filtering."""
        if tab is None or tab["incognito"]:
            return None
        verdict = policy.classify_url(tab["url"])
        if tab["restricted"] or not verdict.readable:
            reason = tab["restriction"] or verdict.reason
            return dict(tab, restricted=True, restriction=reason, url=None, title=None,
                        site_access="restricted")
        return tab

    def _apply_policy(self, op: str, response: dict) -> None:
        result = response["result"]
        if op == "list_tabs":
            tabs = [self._filter_tab(tab) for tab in result["tabs"]]
            result["tabs"] = [tab for tab in tabs if tab is not None]
        elif op in {"get_tab", "get_active_tab"}:
            filtered = self._filter_tab(result)
            if op == "get_tab" and filtered is None:
                raise CompanionError("tab_not_found", "no readable tab with that id")
            response["result"] = filtered
        elif op in {"extract_text", "get_links"}:
            observed = response["observed"]
            verdict = policy.classify_url(observed["url"] if observed else None)
            if observed is None or not verdict.readable or observed["origin"] != verdict.origin \
                    or response["tab_id"] is None:
                raise CompanionError("restricted_surface",
                                     "Chrome returned content for a restricted or unverifiable surface; discarded")
        elif op in ACTION_OPS:
            url = result["observed_url"]
            if url is not None and not policy.classify_url(url).readable:
                raise CompanionError("restricted_surface", "Chrome returned a restricted action surface")
            if op == "switch_tab" and result["status"] == "browser_local_effect_observed" \
                    and (response["tab_id"] != result["tab_id"] or result["tab_id"] is None):
                raise CompanionError("tab_mismatch", "Chrome switched a different tab")

    # -- status ----------------------------------------------------------

    def status(self) -> dict:
        """Metadata-only snapshot for chrome_status."""
        now = time.time()
        try:
            install = state.load_install(self.data_dir)
            pairing = state.load_pairing(self.data_dir)
            state_error = None
        except state.StateError as exc:
            install = pairing = None
            state_error = str(exc)
        with self._lock:
            conn = self._current
            last_disconnect, last_reject = self._last_disconnect, self._last_reject
        return {
            "installed": install is not None,
            "state_error": state_error,
            "paired_instance": state.instance_fingerprint(pairing.instance_id) if pairing else None,
            "hub": "listening" if self.listening else "stopped",
            "hub_error": self.start_error,
            "connection": None if conn is None or conn.closed else {
                "state": "ready", "connection_seq": conn.seq, "instance": conn.instance_fp,
                "extension_version": conn.extension_version,
                "connected_for_s": round(now - conn.opened_at, 1),
            },
            "last_disconnect": None if last_disconnect is None else {
                "reason": last_disconnect["reason"],
                "seconds_ago": round(now - last_disconnect["at"], 1),
            },
            "last_rejected_connection": None if last_reject is None else {
                "reason": last_reject["reason"], "instance": last_reject["instance_fp"],
                "seconds_ago": round(now - last_reject["at"], 1),
            },
        }


# ---------------------------------------------------------------------------
# Process-wide singleton (one hub per Lumina process / data dir)
# ---------------------------------------------------------------------------

_hub: ChromeCompanionHub | None = None
_hub_lock = threading.Lock()


def get_hub(data_dir=None) -> ChromeCompanionHub:
    global _hub
    with _hub_lock:
        if _hub is None:
            if data_dir is None:
                import config
                data_dir = config.DATA_DIR
            _hub = ChromeCompanionHub(data_dir)
            atexit.register(_hub.stop)
        return _hub


def ensure_hub_started(data_dir=None) -> ChromeCompanionHub:
    """Start the process hub if needed. Start failures are recorded on the
    hub (surfaced by chrome_status and every tool error), never raised into
    agent construction."""
    hub = get_hub(data_dir)
    if not hub.listening:
        try:
            hub.start()
        except (CompanionError, state.StateError, OSError) as exc:
            hub.start_error = str(exc)
            hub._record("chrome_companion.hub", severity="warning", state="start_failed",
                        reason=getattr(exc, "code", type(exc).__name__))
    return hub
