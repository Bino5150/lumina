"""WIRE-WAKE-01 W1 — Lumina's native Wire wake listener.

Connects the deployed W0 wake infrastructure (production Wire daemon,
release 364c0e9) to the Lumina GUI: registers one ``lumina-gui`` session,
holds a session-bound authenticated watch, keeps the lease alive with an
independent heartbeat thread, reconnects with backoff, catches up through
byte-bounded ``wake_list`` pagination, and marshals every event onto the Qt
GUI thread via :class:`WireSignals` (the established cross-thread pattern:
telegram bridge, SuspensionSignals).

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
- Wire content is untrusted coordination data. This module logs event types
  and identifiers only — never message bodies, notes, or tokens — and never
  acts on content.

Zero model inference while merely listening: the threads here speak the Wire
protocol and emit Qt signals. That is all they can do.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from typing import Any

from PySide6.QtCore import QObject, Signal

from tools.aivengers_wire import (
    AivengersWireClient,
    WireError,
    WireUnavailableError,
)

__all__ = ["WireSignals", "WireWakeListener"]

#: Lease requested at registration. The heartbeat renews it well inside this
#: window; a crashed GUI lets it lapse and the daemon marks the session
#: honestly offline (no fake liveness).
DEFAULT_LEASE_SECONDS = 300

#: Wire lease heartbeat period. Deliberately named to avoid confusion with
#: PULSE's turn-recovery heartbeat: this is a socket keepalive that renews
#: the session lease, nothing cognitive.
DEFAULT_HEARTBEAT_SECONDS = 60.0

#: Watch subscription timeout. On each server ``timeout`` event the watch
#: generator ends cleanly and the loop re-subscribes — which also replays
#: unacked pending wakes (W0 R2 semantics) and gives the panel a periodic
#: full-state refresh. Bounded shutdown latency derives from this value.
DEFAULT_WATCH_TIMEOUT_MS = 15_000

#: Reconnect backoff bounds.
RECONNECT_MIN_SECONDS = 0.5
RECONNECT_MAX_SECONDS = 30.0

#: Registration lease must always outlive several heartbeat periods; refuse
#: misconfiguration that could let a healthy listener expire silently.
_MIN_LEASE_TO_HEARTBEAT_RATIO = 2.0


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
    #: Result of an owner-initiated ack/claim action: {wake_id, action, ok, wake|error}.
    wake_action_result = Signal(object)


class WireWakeListener:
    """Owns Lumina's Wire session, watch, heartbeat and catch-up.

    Threading model: one watch thread (blocking generator loop with bounded
    re-subscribe), one heartbeat thread (periodic one-shot lease renewal —
    a quiet Wire must never silently expire the session), plus short-lived
    action threads for owner-initiated ack/claim so the GUI never blocks on
    a socket call. All cross-thread communication to the UI goes through
    :class:`WireSignals` queued connections.

    Structural no-inference guarantee: this module imports nothing from
    ``core.agent``, ``core.turn_recovery``, ``core.suspension`` execution
    paths, or the tool registry. Its only outputs are Qt signals and Wire
    RPCs (register/heartbeat/unregister/ack/claim/wake_list).
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
        self.signals.connection_state.emit("connecting")
        self._watch_thread = threading.Thread(
            target=self._watch_loop, name="wire-wake-watch", daemon=True
        )
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, name="wire-wake-lease", daemon=True
        )
        self._watch_thread.start()
        self._heartbeat_thread.start()

    def shutdown(self, join_timeout: float | None = None) -> None:
        """Stop cleanly: bounded thread termination, best-effort unregister.

        The watch generator ends on its next server timeout event (at most
        ``watch_timeout_ms`` away), so termination latency is bounded by
        configuration, not by traffic. ``join_timeout`` defaults to enough
        headroom for that plus the heartbeat period.
        """
        if not self._started:
            return
        self._stop.set()
        if join_timeout is None:
            join_timeout = self._watch_timeout_ms / 1000.0 + self._heartbeat_seconds + 5.0
        if self._watch_thread is not None:
            self._watch_thread.join(timeout=join_timeout)
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=self._heartbeat_seconds + 5.0)
        for thread in self._drain_action_threads(timeout=10.0):
            pass
        session_id = self.session_id
        if session_id is not None:
            try:
                self._client.unregister_session(session_id)
                self._set_session(None)
            except WireError:
                # Relay unreachable at shutdown: the lease lapses and the
                # daemon marks the session offline. Honest, not silent-fake.
                pass
        self.signals.connection_state.emit("offline")

    # ── registration ──────────────────────────────────────────────────────

    def _register_session(self) -> dict[str, Any]:
        """Register a fresh session; best-effort retire the previous one.

        Called only when the current session is known-dead (liveness probe
        failed) or none exists. After a relay restart the previous session
        row still exists with a lapsed lease; it is unregistered best-effort
        so the seat's session list stays truthful.
        """
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
        self.signals.session_state.emit(session)
        return session

    def _ensure_session(self) -> dict[str, Any]:
        """Return the live session, re-registering only when it is truly dead.

        The liveness probe IS a heartbeat (cheap, renews the lease), so a
        healthy session is never churned: watch timeouts re-subscribe with
        the SAME session id, and only a dead/expired session triggers a new
        registration. This keeps the session identity stable for wake
        targeting and keeps the relay's session table honest.
        """
        session_id = self.session_id
        if session_id is not None:
            try:
                session = self._client.heartbeat_session(session_id)
                self.signals.session_state.emit(session)
                return session
            except WireError as exc:
                print(
                    f"[WIRE] session liveness probe failed; re-registering: "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )
                with self._session_lock:
                    self._session_id = None
        return self._register_session()

    def _set_session(self, session: dict[str, Any] | None) -> None:
        with self._session_lock:
            if session is None:
                self._session_id = None
            self._last_session_view = session
        self.signals.session_state.emit(session)

    _last_session_view: dict[str, Any] | None = None

    # ── watch loop ────────────────────────────────────────────────────────

    def _watch_loop(self) -> None:
        """Subscribe → consume → resubscribe, with backoff on failures.

        Each subscription is session-bound to the STABLE session id: the
        server's ``timeout`` event ends the generator cleanly and the loop
        re-subscribes with the same session (which also replays unacked
        pending wakes, W0 R2). Re-registration happens only when the
        liveness probe in :meth:`_ensure_session` proves the session dead —
        never as a side effect of watch timeouts.
        """
        backoff = RECONNECT_MIN_SECONDS
        while not self._stop.is_set():
            try:
                session = self._ensure_session()
                self.signals.connection_state.emit("listening")
                backoff = RECONNECT_MIN_SECONDS
                self._emit_catchup(session["session_id"])
                for event in self._client.watch(
                    session_id=session["session_id"],
                    timeout_ms=self._watch_timeout_ms,
                ):
                    if self._stop.is_set():
                        return
                    kind = event.get("event")
                    if kind == "wake":
                        wake = event.get("wake")
                        if isinstance(wake, dict):
                            self.signals.wake_event.emit(
                                {"source": "live", "wake": wake}
                            )
                    elif kind == "timeout":
                        break  # clean re-subscribe; replay covers the gap
                    elif kind == "overflow":
                        break  # treat like a drop: reconnect below
                if self._stop.is_set():
                    return
                # Clean end (timeout/overflow): brief pause, then resubscribe.
                self._sleep_or_stop(0.05)
            except WireError as exc:
                if self._stop.is_set():
                    return
                self.signals.connection_state.emit("disconnected")
                print(f"[WIRE] listener: {type(exc).__name__}: {exc}", flush=True)
                self._sleep_or_stop(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX_SECONDS)
            except Exception as exc:  # pragma: no cover - defensive
                if self._stop.is_set():
                    return
                self.signals.connection_state.emit("disconnected")
                print(f"[WIRE] listener: unexpected {type(exc).__name__}: {exc}", flush=True)
                self._sleep_or_stop(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX_SECONDS)

    def _emit_catchup(self, session_id: str) -> None:
        """Walk the bounded wake backlog and emit every row as catch-up.

        Uses ``wake_list`` pagination ONLY (byte-bounded by the relay's R3/R4
        frame budgets); the message ``read`` op is never called — it bounds
        count but not serialized bytes.
        """
        seen: set[str] = set()
        for page in self._client.wake_list_iter():
            for wake in page["wakes"]:
                wake_id = wake.get("wake_id")
                if wake_id in seen:
                    continue
                seen.add(wake_id)
                self.signals.wake_event.emit({"source": "catchup", "wake": wake})

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
                print(f"[WIRE] heartbeat: {type(exc).__name__}: {exc}", flush=True)

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

        The relay re-validates lease, task state and exactly-once at
        execution time (server-side fencing), so a stale or racing click
        receives an honest refusal. A claim does NOT execute work; actual
        work happens only through the owner's existing channels with the
        full admission stack (suspension, E-stop, budget).
        """
        self._run_action("claim", wake_id, self._claim_sync)

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
                self.signals.wake_action_result.emit(
                    {"wake_id": wake_id, "action": action, "ok": True,
                     "wake": result.get("wake", result)}
                )
            except WireError as exc:
                self.signals.wake_action_result.emit(
                    {"wake_id": wake_id, "action": action, "ok": False,
                     "error": str(exc)}
                )

        thread = threading.Thread(
            target=runner, name=f"wire-action-{action}-{uuid.uuid4().hex[:8]}",
            daemon=True,
        )
        with self._action_threads_lock:
            self._action_threads.append(thread)
        thread.start()

    def _drain_action_threads(self, timeout: float) -> list[threading.Thread]:
        with self._action_threads_lock:
            threads = list(self._action_threads)
            self._action_threads.clear()
        for thread in threads:
            thread.join(timeout=timeout)
        return threads

    # ── helpers ───────────────────────────────────────────────────────────

    def _sleep_or_stop(self, seconds: float) -> None:
        """Sleep in small slices so shutdown stays bounded even mid-backoff."""
        deadline = time.monotonic() + seconds
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._stop.wait(timeout=min(remaining, 0.25))
