"""core/suspension_runtime.py -- this process's participation in runaway
suspension (AGENT-RUNAWAY-SUSPEND-01, Gatling Slice C1b).

core/suspension.py is the durable store and its laws. This module is the
one place a running Lumina process holds that store open: which data root
it is bound to, its registered runtime id and heartbeat, and the admission
helpers the agent, the tool registry and every handoff point call.

Participation is FAIL-CLOSED. A process that never called start(), or
whose store is missing, uninitialized or faulted, admits nothing: no new
turn, no tool dispatch, no provider request. Only the desktop may
initialize or recover a store (an explicit owner choice -- see
ui/suspension_dialog.py); the standalone Telegram and Discord bridges, the
eval harness and every other participant only open an existing one.

Independent of the Emergency Interlock (core/emergency_stop.py) and the
Telegram remote-access switch: callers check those gates too, and nothing
here reads or clears either of them.

Refusal text names an incident by time and trigger only. The resume
selector never leaves the desktop operator display.
"""
from __future__ import annotations

import contextlib
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from core import suspension as S


HEARTBEAT_INTERVAL_S = 30.0

# What a public (non-owner) channel sees when admission is refused: nothing
# about incident times, triggers or store state.
PUBLIC_REFUSAL = "[Lumina is paused right now and can't take requests here. Please try again later.]"

ROLES = ("desktop", "cli", "telegram_bridge", "discord_bridge", "eval", "driver", "test")

_TRIGGER_LABELS = {
    S.Trigger.BREAKER_TRIPPED.value: "the no-progress breaker tripped",
    S.Trigger.SECOND_BATCH_REFUSED.value: "a second oversized tool batch was refused",
    S.RECOVERY_TRIGGER: "store recovery could not confirm this scope was clear",
}


class State(Enum):
    NOT_STARTED = "not_started"
    ACTIVE = "active"
    UNINITIALIZED = "uninitialized"
    FAULT = "fault"


@dataclass(frozen=True)
class RuntimeStatus:
    state: State
    role: Optional[str]
    data_dir: Optional[str]
    probe_status: Optional[S.StoreStatus]
    fault: Optional[S.Fault]
    traces: tuple
    local_stops: tuple
    active_incidents: int


class SuspensionRefused(Exception):
    """Admission refused. `text` is fixed machine text safe for any channel
    (time and trigger only, never a selector)."""

    def __init__(self, refusal: str, text: str, *, cause: Optional[Exception] = None):
        super().__init__(text)
        self.refusal = refusal
        self.text = text
        self.cause = cause


_lock = threading.RLock()
_state = State.NOT_STARTED
_role: Optional[str] = None
_data_dir: Optional[str] = None
_store: Optional[S.SuspensionStore] = None
_runtime_id: Optional[str] = None
_probe: Optional[S.StoreProbe] = None
_heartbeat_stop: Optional[threading.Event] = None
_heartbeat_thread: Optional[threading.Thread] = None
_heartbeat_enabled = False
_listeners: list = []


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def start(data_dir, *, role: str, heartbeat: bool = True) -> RuntimeStatus:
    """Bind this process to `data_dir`'s suspension store. Never creates or
    repairs one: an absent or faulted store leaves the process refusing
    all admission until the desktop owner resolves it."""
    if role not in ROLES:
        raise ValueError(f"unknown suspension runtime role {role!r}")
    with _lock:
        _close_locked()
        global _role, _data_dir, _heartbeat_enabled
        _role, _data_dir, _heartbeat_enabled = role, str(data_dir), bool(heartbeat)
        _open_locked(heartbeat=heartbeat)
    status_now = status()
    _notify("state", status_now)
    return status_now


def stop() -> None:
    with _lock:
        _close_locked()
        global _state, _role, _data_dir, _probe
        _state, _role, _data_dir, _probe = State.NOT_STARTED, None, None, None


def _close_locked() -> None:
    global _store, _runtime_id, _heartbeat_stop, _heartbeat_thread
    if _heartbeat_stop is not None:
        _heartbeat_stop.set()
    _heartbeat_stop, _heartbeat_thread = None, None
    if _store is not None:
        with contextlib.suppress(Exception):
            _store.close()
    _store, _runtime_id = None, None


def _open_locked(*, heartbeat: bool) -> None:
    global _state, _store, _runtime_id, _probe, _heartbeat_stop, _heartbeat_thread
    _probe = S.probe(_data_dir)
    if _probe.status is S.StoreStatus.HEALTHY:
        try:
            store = S.open_store(_data_dir)
            runtime_id = store.register_runtime()
        except (S.StoreFault, S.StoreUnavailable) as exc:
            fault = exc.fault if isinstance(exc, S.StoreFault) else None
            _probe = S.StoreProbe(S.StoreStatus.FAULT, fault, _probe.traces)
            _state = State.FAULT
            return
        _store, _runtime_id, _state = store, runtime_id, State.ACTIVE
        if heartbeat:
            _heartbeat_stop = threading.Event()
            _heartbeat_thread = threading.Thread(target=_heartbeat_loop,
                                                 args=(store, runtime_id, _heartbeat_stop),
                                                 name="suspension-heartbeat", daemon=True)
            _heartbeat_thread.start()
    elif _probe.status is S.StoreStatus.FAULT:
        _state = State.FAULT
    else:
        _state = State.UNINITIALIZED


def _heartbeat_loop(store, runtime_id, stop_event) -> None:
    while not stop_event.wait(HEARTBEAT_INTERVAL_S):
        try:
            store.heartbeat(runtime_id)
        except Exception:
            # A heartbeat failure is a store problem: the next admission on
            # this handle sees the same fault and refuses. Nothing to do here.
            pass


def _reopen_after_owner_action() -> RuntimeStatus:
    with _lock:
        _close_locked()
        _open_locked(heartbeat=_heartbeat_enabled)
    status_now = status()
    _notify("state", status_now)
    return status_now


def _status_locked() -> RuntimeStatus:
    """Module state only. Store I/O never runs under _lock (a slow or stuck
    store must not block every admission check in the process)."""
    store = _store
    fault = store.fault if store is not None else (_probe.fault if _probe is not None else None)
    state = _state
    if state is State.ACTIVE and store is not None and store.fault is not None:
        state = State.FAULT
    return RuntimeStatus(state, _role, _data_dir,
                         _probe.status if _probe is not None else None,
                         fault if state is not State.ACTIVE else None,
                         _probe.traces if _probe is not None else (),
                         (), 0)


def status() -> RuntimeStatus:
    with _lock:
        snapshot = _status_locked()
        store = _store
    if store is None:
        return snapshot
    local_stops = store.local_stops()
    active = 0
    if snapshot.state is State.ACTIVE:
        with contextlib.suppress(Exception):
            active = len(store.operator_view())
    return RuntimeStatus(snapshot.state, snapshot.role, snapshot.data_dir, snapshot.probe_status,
                         snapshot.fault, snapshot.traces, local_stops, active)


def runtime_id() -> Optional[str]:
    with _lock:
        return _runtime_id


# ---------------------------------------------------------------------------
# Listeners (the desktop marshals these onto its own thread)
# ---------------------------------------------------------------------------

def add_listener(callback: Callable[[str, object], None]) -> None:
    with _lock:
        if callback not in _listeners:
            _listeners.append(callback)


def remove_listener(callback) -> None:
    with _lock:
        if callback in _listeners:
            _listeners.remove(callback)


def _notify(kind: str, payload) -> None:
    with _lock:
        listeners = list(_listeners)
    for callback in listeners:
        try:
            callback(kind, payload)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Refusal text (fixed, machine-authored, selector-free)
# ---------------------------------------------------------------------------

def _incident_phrase(scope: str) -> str:
    try:
        views = [v for v in _store.operator_view() if v.scope == scope]
    except Exception:
        views = []
    if not views:
        return ""
    view = views[0]
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(view.created_at))
    trigger = _TRIGGER_LABELS.get(view.trigger, "a runaway trigger fired")
    return f" since {when} ({trigger})"


def _refusal_for(exc: Exception, scope: Optional[str]) -> SuspensionRefused:
    if isinstance(exc, S.AdmissionRefused):
        refusal = exc.refusal
        if refusal is S.Refusal.SUSPENDED or refusal is S.Refusal.STALE_TOKEN:
            phrase = _incident_phrase(scope) if scope else ""
            if refusal is S.Refusal.SUSPENDED:
                text = (f"[Lumina: runaway suspension is active{phrase}. Nothing was run. "
                        "The owner can review and resume it from the desktop "
                        "(Suspension controls).]")
            else:
                text = ("[Lumina: this work began before a runaway suspension and can no longer "
                        "run. Nothing further was done.]")
            return SuspensionRefused(refusal.value, text, cause=exc)
        if refusal is S.Refusal.LOCAL_STOP:
            return SuspensionRefused(refusal.value, (
                "[Lumina: a runaway suspension could not be recorded, so this scope is stopped "
                "in this process (global suspension unconfirmed). Nothing was run.]"), cause=exc)
        if refusal is S.Refusal.MISSING_TOKEN:
            return SuspensionRefused(refusal.value, (
                "[Lumina: this work has no suspension lineage, so it was not run.]"), cause=exc)
        if refusal in (S.Refusal.STORE_FAULT, S.Refusal.STORE_BUSY):
            return SuspensionRefused(refusal.value, (
                "[Lumina: the runaway-suspension store is unavailable or failed its identity "
                "checks, so nothing was run. The owner can repair or recover it from the "
                "desktop (Suspension controls).]"), cause=exc)
        return SuspensionRefused(refusal.value, (
            f"[Lumina: suspension admission refused ({refusal.value}). Nothing was run.]"), cause=exc)
    return _not_ready_refusal()


def _not_ready_refusal() -> SuspensionRefused:
    with _lock:
        state = _status_locked().state
    if state is State.UNINITIALIZED:
        return SuspensionRefused("uninitialized", (
            "[Lumina: runaway suspension is not set up for this data folder yet, so nothing was "
            "run. The owner initializes or recovers it from the desktop (Suspension controls).]"))
    if state is State.FAULT:
        return SuspensionRefused("store_fault", (
            "[Lumina: the runaway-suspension store failed its identity checks, so nothing was "
            "run. The owner can repair or recover it from the desktop (Suspension controls).]"))
    return SuspensionRefused("not_started", (
        "[Lumina: this process is not participating in runaway suspension, so nothing was run.]"))


def _active_store() -> S.SuspensionStore:
    with _lock:
        if _state is State.ACTIVE and _store is not None:
            return _store
    raise _not_ready_refusal()


# ---------------------------------------------------------------------------
# Capture, inheritance, admission
# ---------------------------------------------------------------------------

def capture(scope: str) -> S.ExecutionToken:
    """Mint the token for NEW root work in `scope`. Call it where the work
    is created, before any asynchronous handoff, and carry the result."""
    store = _active_store()
    try:
        return store.capture(scope)
    except S.AdmissionRefused as exc:
        raise _refusal_for(exc, scope) from None


def inherited() -> Optional[S.ExecutionToken]:
    """The token bound on this call stack, or None (never current state)."""
    try:
        return S.inherited_token()
    except S.AdmissionRefused:
        return None


def missing_lineage_refusal() -> SuspensionRefused:
    """The refusal for work that arrived without the token it must carry
    (e.g. a relayed turn with no intake token). Never consults this call
    stack -- whatever happens to be bound here is not that work's lineage."""
    return _refusal_for(S.AdmissionRefused(S.Refusal.MISSING_TOKEN), None)


def resolve(explicit: Optional[S.ExecutionToken] = None) -> S.ExecutionToken:
    """explicit > inherited. Missing lineage is refused, never defaulted."""
    if explicit is not None:
        if type(explicit) is not S.ExecutionToken:
            raise _refusal_for(S.AdmissionRefused(S.Refusal.MISSING_TOKEN), None)
        return explicit
    token = inherited()
    if token is None:
        raise _refusal_for(S.AdmissionRefused(S.Refusal.MISSING_TOKEN), None)
    return token


carrying = S.carrying


def carry_into(fn):
    """Wrap a thread target so it runs under the token bound on THIS call
    stack -- an explicit carry for a thread that continues the current
    work (it never mints or refreshes a token). With no lineage here, the
    wrapped call is refused like any other work without lineage."""
    token = inherited()

    def run(*args, **kwargs):
        if token is None:
            raise _refusal_for(S.AdmissionRefused(S.Refusal.MISSING_TOKEN), None)
        with carrying(token):
            return fn(*args, **kwargs)

    return run


def verify(token: S.ExecutionToken) -> None:
    store = _active_store()
    try:
        store.verify(token)
    except S.AdmissionRefused as exc:
        raise _refusal_for(exc, getattr(token, "scope", None)) from None


class Lease:
    """One admitted dispatch. release() is idempotent and MUST be called when
    the work is over; there is deliberately no finalizer, so a forgotten
    release leaves the lease in flight (which blocks a resume) rather than
    depending on garbage collection."""

    def __init__(self, store, lease_id: str, runtime_id: str):
        self.lease_id = lease_id
        self._store, self._runtime_id, self._released = store, runtime_id, False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._store.finish(self.lease_id, self._runtime_id)


def admit(kind: S.LeaseKind, name: str, token: Optional[S.ExecutionToken] = None) -> Lease:
    """Admit and lease one dispatch / provider request / consequential write
    in the store's own transaction. Raises SuspensionRefused if refused."""
    token = resolve(token)
    store = _active_store()
    with _lock:
        rid = _runtime_id
    try:
        lease_id = store.admit(token, kind=kind, name=name, runtime_id=rid)
    except S.AdmissionRefused as exc:
        raise _refusal_for(exc, token.scope) from None
    return Lease(store, lease_id, rid)


@contextlib.contextmanager
def admitted(kind: S.LeaseKind, name: str, token: Optional[S.ExecutionToken] = None):
    """admit() for the duration of a block."""
    lease = admit(kind, name, token)
    try:
        yield lease.lease_id
    finally:
        lease.release()


def trip(trigger: S.Trigger, evidence: dict = None,
         token: Optional[S.ExecutionToken] = None) -> Optional[S.TripOutcome]:
    """Latch the token's scope (compare-and-swap). Returns None if this
    process has no usable store or the work has no lineage -- in which
    case nothing could be admitted for it anyway."""
    try:
        token = resolve(token)
    except SuspensionRefused:
        return None
    with _lock:
        store = _store
    if store is None:
        return None
    outcome = store.trip(token, trigger=trigger, evidence=evidence)
    _notify("trip", outcome)
    return outcome


# ---------------------------------------------------------------------------
# Applying staged items (C1 stopgap -- C2 replaces it)
# ---------------------------------------------------------------------------

def staging_scope() -> Optional[str]:
    """The scope of the work on this call stack, for an item it stages, or
    None when there is no lineage here."""
    token = inherited()
    return token.scope if token is not None else None


_SUSPENDED_REFUSALS = frozenset({"staged_application_suspended", S.Refusal.SUSPENDED.value,
                                 S.Refusal.STALE_TOKEN.value, S.Refusal.LOCAL_STOP.value})


def _application_refusal() -> SuspensionRefused:
    return SuspensionRefused("staged_application_suspended", (
        "[Lumina: a runaway suspension is active, so staged items can't be applied right now. "
        "Nothing was changed and the item is still pending. Review and resume the suspension "
        "from the desktop (Suspension controls) first.]"))


def _any_scope_suspended(store) -> bool:
    try:
        return (bool(store.local_stops()) or bool(store.operator_view())
                or store.unknown_scopes_presumed_suspended())
    except Exception as exc:
        raise _refusal_for(S.AdmissionRefused(S.Refusal.STORE_FAULT, str(exc)), None) from None


@contextlib.contextmanager
def owner_application(name: str, *, scope: Optional[str] = None):
    """Wrap the statement that applies a staged item on the owner's say-so
    (a Settings pending action or Palace promotion, an image-draft
    approval): raises SuspensionRefused before it runs if the affected
    scope is suspended or can't be verified. Owner ruling Q3, 2026-10-20.

    `scope` is the scope that staged the item, when the runtime itself
    recorded it. With no trustworthy record (the pending-actions queue is a
    plain JSON file any write_file call can forge) the affected scope is
    unknown, so every scope counts: refused while any incident is active,
    while unknown scopes are presumed suspended after a lossy recovery, or
    while this process holds a local stop. Either way a fresh root token is
    captured and a WRITE lease is held across the block, so a trip
    committed first refuses it and a resume can't complete while it runs.

    This is C1's stopgap, not C2's deferred-authority enforcement: it does
    not cancel approvals recorded before a trip or re-validate them after a
    resume. The local suspension controls never pass through here."""
    try:
        store = _active_store()
        if scope is None and _any_scope_suspended(store):
            raise _application_refusal()
        token = capture(S.OWNER_SCOPE if scope is None else scope)
        lease = admit(S.LeaseKind.WRITE, name, token)
    except SuspensionRefused as refused:
        if refused.refusal in _SUSPENDED_REFUSALS:
            raise _application_refusal() from None
        raise SuspensionRefused(refused.refusal, refused.text[:-1] + " The staged item is still pending.]",
                                cause=refused.cause) from None
    try:
        # A trip in another scope that committed between the check and the
        # lease would otherwise slip through.
        if scope is None and _any_scope_suspended(store):
            raise _application_refusal()
        yield
    finally:
        lease.release()


def admit_provider(name: str) -> Lease:
    """admit() a provider request (spend) against this call stack's token."""
    return admit(S.LeaseKind.PROVIDER, name)


# ---------------------------------------------------------------------------
# Owner-local controls (desktop only; each needs a local envelope)
# ---------------------------------------------------------------------------

def _require_desktop() -> None:
    with _lock:
        role = _role
    if role not in ("desktop", "test"):
        raise S.NotAuthorized("suspension controls are desktop-only")


def initialize(*, ingress) -> RuntimeStatus:
    _require_desktop()
    S.initialize(_data_dir, ingress=ingress).close()
    return _reopen_after_owner_action()


def repair_identity(*, ingress) -> RuntimeStatus:
    _require_desktop()
    S.repair_identity(_data_dir, ingress=ingress).close()
    return _reopen_after_owner_action()


def recover(*, ingress, acknowledged_runtimes=frozenset(), acknowledge_unreadable_runtimes=False,
            acknowledge_unknown_scopes=False, acknowledge_unknown_prior_state=False) -> S.RecoveryOutcome:
    _require_desktop()
    with _lock:
        _close_locked()
    outcome = S.recover_destructively(
        _data_dir, ingress=ingress, acknowledged_runtimes=acknowledged_runtimes,
        acknowledge_unreadable_runtimes=acknowledge_unreadable_runtimes,
        acknowledge_unknown_scopes=acknowledge_unknown_scopes,
        acknowledge_unknown_prior_state=acknowledge_unknown_prior_state)
    _reopen_after_owner_action()
    return outcome


def operator_view() -> tuple:
    """Desktop operator display only (carries selectors)."""
    _require_desktop()
    return _active_store().operator_view()


def lease_report(scope: str) -> S.LeaseReport:
    return _active_store().lease_report(scope)


def resume(view: S.IncidentView, *, ingress, ack_unknown: bool = False) -> S.ResumeOutcome:
    """Resume exactly the incident on screen: one freshly minted, bound
    desktop event, consumed in one transaction."""
    _require_desktop()
    expectation = view.expectation()
    event = S.desktop_recovery_event(ingress, expectation, ack_unknown=ack_unknown)
    outcome = _active_store().resume(event, expectation)
    _notify("resume", outcome)
    return outcome


def acknowledge_unknown_scopes(*, ingress) -> None:
    _require_desktop()
    _active_store().acknowledge_unknown_scopes(ingress=ingress)
    _notify("state", status())
