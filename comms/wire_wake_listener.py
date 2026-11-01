"""WIRE-WAKE-01 W1 — Lumina's native Wire wake listener (R2).

Connects the deployed W0 wake infrastructure (production Wire daemon,
release 364c0e9) to the Lumina GUI: registers one ``lumina-gui`` session,
holds a session-bound authenticated watch, keeps the lease alive with an
independent heartbeat thread, reconnects with an honest backoff budget,
catches up through byte-bounded ``wake_list`` pagination on (re)connect
only, and marshals every event onto the Qt GUI thread via
:class:`WireSignals` (the established cross-thread pattern: telegram
bridge, SuspensionSignals).

R2 repairs (Sonnet review of 75d2b98):

- **B2 framing**: the client's watch() now uses a persistent buffered
  frame decoder (fragmented + coalesced frames safe); the reconnect
  backoff budget resets only after a valid post-subscribe frame, so a
  relay that accepts subscriptions and then fails cannot pin the listener
  in a ~0.6s reconnect storm.
- **H1 identity**: only a server-side protocol rejection of the session
  reference invalidates session identity. Transient failures (relay
  unreachable, frame errors) keep the identity and retry. Registration
  first retires stale ``lumina-gui`` sessions whose ``platform_pid`` is
  no longer alive, so crash/restart cycles cannot accumulate live
  sessions or make seat-level wakes resolve ambiguous.
- **H2 shutdown**: ``shutdown()`` closes the live watch socket (client
  ``_watch_socket`` hook) to unblock ``recv`` immediately; thread joins
  are hard-capped and the final unregister is fire-and-forget, so GUI
  close never waits on Wire traffic.
- **H3 scale**: catch-up runs on (re)connect only — never on the routine
  watch-timeout resubscribe — and only for live states
  (pending/delivered/claimed), bounded pages, truncation reported
  honestly instead of raised.
- **B3 default-off** is enforced in ``ui/main_window.py`` (prefs key
  defaults to False; env canary override); this module is inert unless
  constructed and started.

HONESTY CONTRACT (mirrors the W0 state machine, never decorated):

- A wake event means "the Wire has a durable intent addressed to this
  seat/session". It is NOT a model turn, NOT a resume, NOT work.
- ``ack`` is an adapter receipt: "this GUI received the intent". Nothing more.
- ``claim`` is a coordination claim on the Wire's task table, exactly-once
  with server-side lease/task fencing. It is NOT work execution. It is
  reachable ONLY from the owner's GUI click (see :meth:`claim_wake`); this
  module deliberately imports nothing from ``core.agent``,
  ``core.turn_recovery``, the tool registry, or any execution path, so no
  model-facing route to claim can exist.
- ``release`` is the owner's explicit unclaim: the W0 protocol's only
  release mechanism is ``complete_task(outcome="failed")`` with an
  explicit released-without-execution note, which the confirmation dialog
  states verbatim before the owner confirms.
- Wire content is untrusted coordination data. This module logs event
  types and identifiers only — never message bodies, notes, or tokens —
  and log lines are sanitized to single-line bounded text.

Zero model inference while merely listening: the threads here speak the Wire
protocol and emit Qt signals. That is all they can do.
"""

from __future__ import annotations

import os
import re
import socket
import threading
import time
import uuid
from typing import Any

from PySide6.QtCore import QObject, Signal

from tools.aivengers_wire import (
    AivengersWireClient,
    WireError,
    WireProtocolError,
    WireUnavailableError,
)

__all__ = ["WireSignals", "WireWakeListener"]

#: Lease requested at registration (R2/H1: short lease so a crashed GUI's
#: orphan session stops resolving live within ~1.5 minutes). The heartbeat
#: renews it well inside this window.
DEFAULT_LEASE_SECONDS = 90

#: Wire lease heartbeat period. Deliberately named to avoid confusion with
#: PULSE's turn-recovery heartbeat: this is a socket keepalive that renews
#: the session lease, nothing cognitive.
DEFAULT_HEARTBEAT_SECONDS = 30.0

#: Watch subscription timeout. On each server ``timeout`` event the watch
#: generator ends cleanly and the loop re-subscribes with the SAME session
#: (the relay replays unacked pending wakes on every subscription). No
#: catch-up walk on this path — that runs on (re)connect only (R2/H3).
DEFAULT_WATCH_TIMEOUT_MS = 15_000

#: Reconnect backoff bounds. The budget resets ONLY after a valid
#: post-subscribe frame (R2/B2), so repeated subscribe-then-fail cycles
#: back off exponentially instead of looping at ~0.6s.
RECONNECT_MIN_SECONDS = 0.5
RECONNECT_MAX_SECONDS = 30.0

#: Registration lease must always outlive several heartbeat periods; refuse
#: misconfiguration that could let a healthy listener expire silently.
_MIN_LEASE_TO_HEARTBEAT_RATIO = 2.0

#: R2/H3: catch-up walks only live states, bounded pages per state.
_CATCHUP_STATES = ("pending", "delivered", "claimed")
_CATCHUP_MAX_PAGES_PER_STATE = 5  # 5 x 100 = 500 wakes per state, then truncated

#: R2: log lines are single-line and bounded (log-injection surface).
_LOG_SANITIZE_RE = re.compile(r"[\r\n\t\x00-\x1f\x7f]")
_LOG_MAX_CHARS = 200


def _sanitize_log(text: Any) -> str:
    cleaned = _LOG_SANITIZE_RE.sub(" ", str(text))
    if len(cleaned) > _LOG_MAX_CHARS:
        cleaned = cleaned[:_LOG_MAX_CHARS] + "…"
    return cleaned


def _pid_alive(pid: Any) -> bool:
    """Best-effort liveness check for a recorded platform_pid."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


class WireSignals(QObject):
    """Cross-thread marshal for Wire listener events.

    Emitted from the listener/heartbeat threads; delivered on the GUI thread
    via Qt queued connections (same shape as SuspensionSignals and the
    telegram bridge's origin dispatch).
    """

    #: A wake/task-relevant event or catch-up snapshot row arrived.
    wake_event = Signal(object)
    #: Connection lifecycle: "connecting" | "listening" | "disconnected" | "offline".
    connection_state = Signal(str)
    #: Current session record (dict) or None when unregistered/expired.
    session_state = Signal(object)
    #: Result of an owner-initiated ack/claim/release action:
    #: {wake_id, task_ref, action, ok, wake?, task?, error?}.
    wake_action_result = Signal(object)


class WireWakeListener:
    """Owns Lumina's Wire session, watch, heartbeat and catch-up.

    Threading model: one watch thread (blocking generator loop with bounded
    re-subscribe), one heartbeat thread (periodic one-shot lease renewal —
    a quiet Wire must never silently expire the session), plus short-lived
    action threads for owner-initiated ack/claim/release so the GUI never
    blocks on a socket call. All cross-thread communication to the UI goes
    through :class:`WireSignals` queued connections.

    Structural no-inference guarantee: this module imports nothing from
    ``core.agent``, ``core.turn_recovery``, ``core.suspension`` execution
    paths, or the tool registry. Its only outputs are Qt signals and Wire
    RPCs (register/heartbeat/unregister/ack/claim/complete/wake_list/
    sessions).
    """

    def __init__(
        self,
        *,
        client: AivengersWireClient | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
        watch_timeout_ms: int = DEFAULT_WATCH_TIMEOUT_MS,
        adapter_name: str = "lumina-gui",
    ) -> None:
        if lease_seconds < _MIN_LEASE_TO_HEARTBEAT_RATIO * heartbeat_seconds:
            raise ValueError(
                "lease_seconds must exceed "
                f"{_MIN_LEASE_TO_HEARTBEAT_RATIO}x heartbeat_seconds so a healthy "
                "listener can never silently expire"
            )
        self._client = client if client is not None else AivengersWireClient()
        self._lease_seconds = lease_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._watch_timeout_ms = watch_timeout_ms
        self._adapter_name = adapter_name
        self.signals = WireSignals()

        self._stop = threading.Event()
        self._watch_thread: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._session_id: str | None = None
        self._session_lock = threading.Lock()
        self._started = False
        self._action_threads: list[threading.Thread] = []
        self._action_threads_lock = threading.Lock()
        #: R2/H3: catch-up runs on (re)connect only — set when a fresh
        #: session is registered or after a connection loss, cleared once
        #: walked. Routine watch-timeout resubscribes do NOT re-walk.
        self._catchup_needed = threading.Event()
        #: R2: wake_ids this listener has surfaced in a non-terminal state.
        #: Refreshed via wake_status on each resubscribe cycle so terminal
        #: transitions that happen between catch-ups (operator cancellation,
        #: expiry) surface honestly without unbounded re-walks.
        self._tracked_wakes: dict[str, str] = {}
        self._tracked_lock = threading.Lock()

    # ── lifecycle ─────────────────────────────────────────────────────────

    @property
    def session_id(self) -> str | None:
        with self._session_lock:
            return self._session_id

    @property
    def is_running(self) -> bool:
        return self._started and not self._stop.is_set()

    def start(self) -> None:
        """Start the listener exactly once per instance.

        A second ``start()`` on a running (or previously started) listener
        raises: duplicate registration is a defect, not a fallback.
        """
        if self._started:
            raise RuntimeError("WireWakeListener already started")
        self._started = True
        self._stop.clear()
        self._catchup_needed.set()
        self.signals.connection_state.emit("connecting")
        self._watch_thread = threading.Thread(
            target=self._watch_loop, name="wire-wake-watch", daemon=True
        )
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, name="wire-wake-lease", daemon=True
        )
        self._watch_thread.start()
        self._heartbeat_thread.start()

    def shutdown(self, join_timeout: float = 2.0) -> None:
        """Stop cleanly, bounded: never waits on Wire traffic (R2/H2).

        The live watch socket is closed immediately, which unblocks the
        watch thread's ``recv``; thread joins are hard-capped; the final
        unregister runs fire-and-forget (its failure is covered honestly by
        the lease lapse — the daemon marks the session offline either way).
        """
        if not self._started:
            return
        self._stop.set()
        # Unblock the watch recv NOW instead of waiting for the server
        # timeout event (up to watch_timeout_ms away).
        watch_sock = getattr(self._client, "_watch_socket", None)
        if watch_sock is not None:
            try:
                watch_sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                watch_sock.close()
            except OSError:
                pass
        if self._watch_thread is not None:
            self._watch_thread.join(timeout=join_timeout)
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=1.0)
        session_id = self.session_id
        if session_id is not None:
            with self._session_lock:
                self._session_id = None
            # Fire-and-forget: bounded GUI shutdown beats a synchronous RPC.
            # If the process exits before this lands, the lease lapse marks
            # the session offline — the honest outcome either way.
            threading.Thread(
                target=self._unregister_best_effort,
                args=(session_id,),
                name="wire-wake-unregister",
                daemon=True,
            ).start()
        self.signals.connection_state.emit("offline")

    def _unregister_best_effort(self, session_id: str) -> None:
        try:
            self._client.unregister_session(session_id)
        except WireError:
            pass

    # ── registration ──────────────────────────────────────────────────────

    def _register_session(self) -> dict[str, Any]:
        """Register a fresh session; retire dead leftovers first (R2/H1).

        Stale ``lumina-gui`` sessions of this seat whose ``platform_pid`` is
        no longer alive are unregistered before the new registration, so a
        crash/restart cycle cannot leave two live sessions (which would make
        seat-level wakes resolve ambiguous). The previous session id of THIS
        listener is also retired best-effort.
        """
        try:
            page = self._client.sessions_page(limit=100)
            for stale in page["sessions"]:
                if stale.get("adapter") != self._adapter_name:
                    continue
                if stale.get("session_id") == self.session_id:
                    continue
                if not _pid_alive(stale.get("platform_pid")):
                    try:
                        self._client.unregister_session(stale["session_id"])
                    except WireError:
                        pass
        except WireError as exc:
            print(
                f"[WIRE] stale-session sweep skipped: "
                f"{_sanitize_log(type(exc).__name__ + ': ' + str(exc))}",
                flush=True,
            )
        old = self.session_id
        if old is not None:
            try:
                self._client.unregister_session(old)
            except WireError:
                pass  # best-effort; the lease lapse covers it regardless
        session = self._client.register_session(
            adapter=self._adapter_name,
            capabilities={"notify": True, "wake_ack": True, "claim": True},
            lease_seconds=self._lease_seconds,
            platform_pid=os.getpid(),
        )
        with self._session_lock:
            self._session_id = session["session_id"]
        self._catchup_needed.set()
        self.signals.session_state.emit(session)
        return session

    def _ensure_session(self) -> dict[str, Any]:
        """Return the live session; re-register ONLY on server-side rejection.

        R2/H1: a liveness heartbeat that cannot reach the relay
        (:class:`WireUnavailableError`) or that fails on the transport
        (frame errors) does NOT invalidate the session identity — the id is
        kept and the watch loop's reconnect path retries. Only an explicit
        server-side protocol rejection of the session reference (unknown
        session, session offline/expired/revoked, seat mismatch) proves the
        identity dead and triggers re-registration.
        """
        session_id = self.session_id
        if session_id is not None:
            try:
                session = self._client.heartbeat_session(session_id)
                self.signals.session_state.emit(session)
                return session
            except WireProtocolError as exc:
                print(
                    f"[WIRE] session rejected by relay; re-registering: "
                    f"{_sanitize_log(str(exc))}",
                    flush=True,
                )
                with self._session_lock:
                    self._session_id = None
            except WireError:
                # Transient (unreachable relay, frame trouble): keep the
                # identity; the caller's reconnect path retries with it.
                raise
        return self._register_session()

    # ── watch loop ────────────────────────────────────────────────────────

    def _watch_loop(self) -> None:
        """Subscribe → consume → resubscribe, with an honest backoff budget.

        R2/B2: the backoff budget resets ONLY after a valid post-subscribe
        frame has been consumed — a relay that accepts subscriptions and
        then fails cannot pin the listener into a tight reconnect storm.
        Each subscription is session-bound to the STABLE session id; the
        server's ``timeout`` event ends the generator cleanly and the loop
        re-subscribes with the same session (server replay covers pending
        wakes). Catch-up walks run on (re)connect only (R2/H3).
        """
        backoff = RECONNECT_MIN_SECONDS
        while not self._stop.is_set():
            try:
                session = self._ensure_session()
                self.signals.connection_state.emit("listening")
                if self._catchup_needed.is_set():
                    self._catchup_needed.clear()
                    self._emit_catchup(session["session_id"])
                got_frame = False
                for event in self._client.watch(
                    session_id=session["session_id"],
                    timeout_ms=self._watch_timeout_ms,
                ):
                    if self._stop.is_set():
                        return
                    if not got_frame:
                        got_frame = True
                        backoff = RECONNECT_MIN_SECONDS
                    kind = event.get("event")
                    if kind == "wake":
                        wake = event.get("wake")
                        if isinstance(wake, dict):
                            self._track_wake(wake)
                            self.signals.wake_event.emit(
                                {"source": "live", "wake": wake}
                            )
                    elif kind in ("timeout", "overflow"):
                        break  # clean re-subscribe; server replay covers the gap
                if self._stop.is_set():
                    return
                # R2: refresh tracked non-terminal wakes — terminal
                # transitions that happened between catch-ups (operator
                # cancellation, TTL expiry) surface here. Bounded by the
                # live backlog the listener actually surfaced.
                self._refresh_tracked_wakes()
                # Clean end (timeout/overflow): brief pause, then resubscribe
                # with the SAME session (no churn, no catch-up re-walk).
                self._sleep_or_stop(0.05)
            except WireError as exc:
                if self._stop.is_set():
                    return
                self.signals.connection_state.emit("disconnected")
                print(
                    f"[WIRE] listener: {_sanitize_log(type(exc).__name__ + ': ' + str(exc))}",
                    flush=True,
                )
                self._sleep_or_stop(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX_SECONDS)
            except Exception as exc:  # pragma: no cover - defensive
                if self._stop.is_set():
                    return
                self.signals.connection_state.emit("disconnected")
                print(
                    f"[WIRE] listener: unexpected "
                    f"{_sanitize_log(type(exc).__name__ + ': ' + str(exc))}",
                    flush=True,
                )
                self._sleep_or_stop(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX_SECONDS)

    def _track_wake(self, wake: dict[str, Any]) -> None:
        wake_id = wake.get("wake_id")
        if isinstance(wake_id, str) and wake_id:
            with self._tracked_lock:
                self._tracked_wakes[wake_id] = str(wake.get("state", "pending"))

    def _refresh_tracked_wakes(self) -> None:
        """Re-query tracked non-terminal wakes (bounded, one RPC each).

        A wake whose task was cancelled while pending is marked failed by the
        relay's delivery path — an event the watch stream never carries. The
        catch-up walk covers reconnects; this covers the routine resubscribe
        cycle. Bounded: only non-terminal tracked wakes, capped per cycle.
        """
        with self._tracked_lock:
            pending_ids = [
                wid for wid, state in self._tracked_wakes.items()
                if state not in ("completed", "failed", "expired", "cancelled",
                                 "ambiguous")
            ][:50]
        for wake_id in pending_ids:
            if self._stop.is_set():
                return
            try:
                wake = self._client.wake_status(wake_id)
            except WireError:
                continue  # relay hiccup; next cycle retries
            with self._tracked_lock:
                self._tracked_wakes[wake_id] = str(wake.get("state", "pending"))
            self.signals.wake_event.emit({"source": "refresh", "wake": wake})

    def _emit_catchup(self, session_id: str) -> None:
        """Walk the bounded live-state backlog on (re)connect (R2/H3).

        Only ``pending``/``delivered``/``claimed`` wakes are surfaced — the
        full terminal history is never re-walked (the relay retains wake
        rows forever, so an unbounded walk eventually wedges). Each state
        walk is bounded; overflow is reported as an honest truncation
        notice instead of an exception. Uses ``wake_list`` pagination ONLY
        (byte-bounded by the relay's R3/R4 frame budgets); the message
        ``read`` op is never called — it bounds count but not bytes.
        """
        for state in _CATCHUP_STATES:
            cursor: dict[str, str] | None = None
            truncated = False
            for _ in range(_CATCHUP_MAX_PAGES_PER_STATE):
                page = self._client.wake_list_page(
                    state=state,
                    after_created_at=cursor["created_at"] if cursor else None,
                    after_wake_id=cursor["wake_id"] if cursor else None,
                )
                for wake in page["wakes"]:
                    self.signals.wake_event.emit({"source": "catchup", "wake": wake})
                if page["has_more"] and page["next_after"]:
                    cursor = page["next_after"]
                else:
                    cursor = None
                    break
            else:
                truncated = bool(page["has_more"] and page["next_after"])
            if truncated:
                self.signals.wake_event.emit(
                    {
                        "source": "catchup",
                        "truncated": True,
                        "state": state,
                        "note": (
                            f"more than "
                            f"{_CATCHUP_MAX_PAGES_PER_STATE * 100} {state} wakes; "
                            "showing the newest bounded window"
                        ),
                    }
                )

    # ── heartbeat ─────────────────────────────────────────────────────────

    def _heartbeat_loop(self) -> None:
        """Renew the session lease forever while running, independent of watch
        traffic. A quiet Wire must not silently expire Lumina's session: this
        thread fires on a clock, not on events."""
        while not self._stop.is_set():
            if self._stop.wait(timeout=self._heartbeat_seconds):
                return
            session_id = self.session_id
            if session_id is None:
                continue  # watch loop is (re)registering; next tick covers it
            try:
                session = self._client.heartbeat_session(session_id)
                self.signals.session_state.emit(session)
            except WireUnavailableError:
                continue  # relay down; watch loop owns reconnection
            except WireError as exc:
                print(
                    f"[WIRE] heartbeat: {_sanitize_log(type(exc).__name__ + ': ' + str(exc))}",
                    flush=True,
                )

    # ── owner-initiated actions (panel only; never model-facing) ──────────

    def ack_wake(self, wake_id: str) -> None:
        """Owner GUI action: record the adapter receipt for one wake.

        Runs on a short-lived thread so the GUI never blocks on a socket;
        the result arrives via :attr:`signals.wake_action_result`. Ack is an
        adapter receipt only — it never implies a model resumed or work ran.
        """
        self._run_action("ack", wake_id, self._ack_sync)

    def claim_wake(self, wake_id: str) -> None:
        """Owner GUI action: coordination-claim a wake's task, exactly once.

        The panel confirms intent before this is called. The relay
        re-validates lease, task state and exactly-once at execution time
        (server-side fencing), so a stale or racing click receives an honest
        refusal. A claim does NOT execute work; actual work happens only
        through the owner's existing channels with the full admission stack
        (suspension, E-stop, budget).
        """
        self._run_action("claim", wake_id, self._claim_sync)

    def release_task(self, wake_id: str, task_ref: str) -> None:
        """Owner GUI action: release a claimed task (R2/H4 lifecycle).

        The W0 protocol's only unclaim mechanism is a terminal completion:
        this records ``outcome='failed'`` with the explicit note
        ``released by owner without execution``. The panel's confirmation
        dialog states that verbatim before this runs — the task becomes
        permanently terminal and cannot be reclaimed.
        """
        self._run_action(
            "release",
            wake_id,
            lambda _wid: self._client.complete_task(
                task_ref,
                self.session_id or "",
                outcome="failed",
                note="released by owner without execution",
            ),
        )

    def _ack_sync(self, wake_id: str) -> dict[str, Any]:
        session_id = self.session_id
        if session_id is None:
            raise WireError("listener has no live session")
        return self._client.wake_ack(wake_id, session_id)

    def _claim_sync(self, wake_id: str) -> dict[str, Any]:
        session_id = self.session_id
        if session_id is None:
            raise WireError("listener has no live session")
        return self._client.claim_wake(wake_id, session_id)

    def _run_action(self, action: str, wake_id: str, fn) -> None:
        wake_id = str(wake_id)
        if not wake_id:
            return
        if not self.is_running:
            self.signals.wake_action_result.emit(
                {"wake_id": wake_id, "action": action, "ok": False,
                 "error": "listener is not running"}
            )
            return

        def runner() -> None:
            try:
                result = fn(wake_id)
                payload: dict[str, Any] = {
                    "wake_id": wake_id,
                    "action": action,
                    "ok": True,
                }
                if isinstance(result, dict):
                    if "wake" in result:
                        # Wrapper response (claim: {wake, task, fencing}).
                        payload["wake"] = result["wake"]
                        if "task" in result:
                            payload["task"] = result["task"]
                    elif "wake_id" in result:
                        # Bare wake record (ack returns the wake directly).
                        payload["wake"] = result
                    elif action == "release":
                        payload["task"] = result
                self.signals.wake_action_result.emit(payload)
            except WireError as exc:
                self.signals.wake_action_result.emit(
                    {"wake_id": wake_id, "action": action, "ok": False,
                     "error": _sanitize_log(str(exc))}
                )
            finally:
                with self._action_threads_lock:
                    try:
                        self._action_threads.remove(threading.current_thread())
                    except ValueError:
                        pass

        thread = threading.Thread(
            target=runner, name=f"wire-action-{action}-{uuid.uuid4().hex[:8]}",
            daemon=True,
        )
        with self._action_threads_lock:
            # R2 housekeeping: drop finished threads so the list stays bounded.
            self._action_threads[:] = [
                t for t in self._action_threads if t.is_alive()
            ]
            self._action_threads.append(thread)
        thread.start()

    # ── helpers ───────────────────────────────────────────────────────────

    def _sleep_or_stop(self, seconds: float) -> None:
        """Sleep in small slices so shutdown stays bounded even mid-backoff."""
        deadline = time.monotonic() + seconds
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._stop.wait(timeout=min(remaining, 0.25))
