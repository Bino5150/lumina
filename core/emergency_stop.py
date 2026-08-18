"""
core/emergency_stop.py — process-wide OH SHIT / STOP ALL interlock kernel.

Runtime-only, in-memory. No prefs/SQLite/Palace/chat-history involvement —
this state must never survive a restart, and must never be reachable from
anything model-facing. Once latched, no new model-controlled tool dispatch
or new execution generation may start; work started before the latch can
finish (already-blocked provider/tool calls are never killed), but its
epoch becomes permanently stale and it can never regain authority again,
even after local re-arm.

Normal /stop (core/agent.py's per-worker cancellation Event) is a separate,
narrower mechanism. This module is the process-wide trust boundary those
per-turn cancellations now also automatically respect (see
_cancel_requested() in core/agent.py) — but this module itself knows
nothing about turns, workers, or UI.

See LUMINA_PATCH_3A1_EMERGENCY_INTERLOCK_KERNEL_SPEC.md for the full
invariant this implements.
"""
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar


class EmergencyStopError(Exception):
    """Base class for emergency-interlock rejections."""


class EmergencyStopActive(EmergencyStopError):
    """Raised when new work is attempted while the interlock is latched."""


class StaleExecution(EmergencyStopError):
    """Raised when work belongs to an epoch that is no longer current —
    i.e. it was created/started before a latch that has since re-armed."""


class RearmBlocked(EmergencyStopError):
    """Raised when rearm_local() is called from an unsafe thread/state."""


_lock = threading.RLock()

_latched = False
_epoch = 0
_incident: dict = None          # snapshot of the current/last incident, or None
_executions: dict = {}          # lease_id -> lease dict
_tool_dispatches: dict = {}     # lease_id -> lease dict

# Same-thread propagation only, by design — background/scheduled work
# crosses real OS threads and deliberately does NOT rely on ContextVar
# inheritance there; it threads an explicit expected_epoch through instead
# (see core/task_queue.py). These vars only carry epoch/execution identity
# down a single synchronous call stack (e.g. chat() -> tool loop -> a tool
# function calling submit_task()).
_current_epoch_var: ContextVar = ContextVar("lumina_emergency_epoch", default=None)
_current_execution_id_var: ContextVar = ContextVar("lumina_emergency_execution_id", default=None)


def is_latched() -> bool:
    with _lock:
        return _latched


def current_epoch() -> int:
    with _lock:
        return _epoch


def execution_permitted(epoch: int = None) -> bool:
    """Read-side safe-boundary check — NOT the atomic tool-dispatch gate
    (that's begin_tool_dispatch()). False if latched, or if an explicit or
    current-ContextVar epoch exists and no longer matches the process
    epoch. True with no active execution context and no latch."""
    with _lock:
        if _latched:
            return False
        check_epoch = epoch if epoch is not None else _current_epoch_var.get()
        if check_epoch is not None and check_epoch != _epoch:
            return False
        return True


def capture_epoch_for_new_work() -> int:
    """Called when background/scheduled work is created, to stamp it with
    the epoch it's allowed to run under. Raises EmergencyStopActive if
    latched, or StaleExecution if the calling execution is itself already
    stale. Inherits the enclosing execution's epoch when called from a
    valid nested execution (same-thread only); otherwise returns the
    current process epoch."""
    with _lock:
        if _latched:
            raise EmergencyStopActive("emergency stop is latched; cannot create new work")
        ctx_epoch = _current_epoch_var.get()
        if ctx_epoch is None:
            return _epoch
        if ctx_epoch != _epoch:
            raise StaleExecution(
                f"calling execution's epoch {ctx_epoch} is stale (current epoch {_epoch})"
            )
        return ctx_epoch


@contextmanager
def execution_scope(kind: str, label: str = "", expected_epoch: int = None, metadata: dict = None):
    """Context manager marking one unit of "real" execution (a foreground
    turn, a background task, a scheduled job) as active. Registers a lease
    that keeps re-arm blocked for its whole lifetime, and sets the current
    epoch for anything nested inside it on the same thread.

    Nested scopes inherit the same epoch unless expected_epoch is given
    explicitly (used by task_queue to pin a background/scheduled job to the
    epoch it was created under, independent of whatever the process epoch
    is by the time it actually runs).

    The global lock is only held for the entry/exit bookkeeping, never
    across the yielded body.
    """
    with _lock:
        if _latched:
            raise EmergencyStopActive(f"emergency stop is latched; cannot start {kind}")
        if expected_epoch is not None:
            epoch = expected_epoch
        else:
            ctx_epoch = _current_epoch_var.get()
            epoch = ctx_epoch if ctx_epoch is not None else _epoch
        if epoch != _epoch:
            raise StaleExecution(f"{kind} epoch {epoch} is stale (current epoch {_epoch})")
        lease_id = str(uuid.uuid4())
        _executions[lease_id] = {
            "id": lease_id,
            "kind": kind,
            "label": label,
            "epoch": epoch,
            "started_at": time.time(),
            "metadata": dict(metadata) if metadata else {},
        }

    epoch_token = _current_epoch_var.set(epoch)
    exec_id_token = _current_execution_id_var.set(lease_id)
    try:
        yield lease_id
    finally:
        _current_epoch_var.reset(epoch_token)
        _current_execution_id_var.reset(exec_id_token)
        with _lock:
            _executions.pop(lease_id, None)


def begin_tool_dispatch(name: str) -> str:
    """Atomic lowest-level execution gate — the universal blast door
    immediately before a tool function actually runs. Raises
    EmergencyStopActive/StaleExecution if dispatch isn't currently
    permitted; otherwise registers and returns a lease id. If this wins the
    lock a moment before a concurrent latch(), the tool is already
    considered in-flight: the latch will still see and snapshot this lease,
    and re-arm stays blocked until end_tool_dispatch() closes it — the
    latch does not retroactively un-admit a dispatch that already won the
    lock."""
    with _lock:
        if _latched:
            raise EmergencyStopActive(f"emergency stop is latched; cannot dispatch tool '{name}'")
        ctx_epoch = _current_epoch_var.get()
        epoch = ctx_epoch if ctx_epoch is not None else _epoch
        if epoch != _epoch:
            raise StaleExecution(f"tool dispatch epoch {epoch} is stale (current epoch {_epoch})")
        lease_id = str(uuid.uuid4())
        _tool_dispatches[lease_id] = {
            "id": lease_id,
            "name": name,
            "epoch": epoch,
            "started_at": time.time(),
        }
        return lease_id


def end_tool_dispatch(lease_id: str):
    """Always call in a finally — releases the lease regardless of the
    tool's outcome (normal result, TypeError, or exception)."""
    with _lock:
        _tool_dispatches.pop(lease_id, None)


def latch(source: str = "local_control_plane", reason: str = "operator emergency stop") -> dict:
    """Idempotent: a repeated latch while already latched does not
    increment the epoch again and does not overwrite the original incident.
    On the first latch, atomically flips _latched, increments the epoch
    exactly once, and snapshots whatever executions/tool-dispatches were
    active at that instant. Returns a copy of the (possibly pre-existing)
    incident."""
    global _latched, _epoch, _incident
    with _lock:
        if _latched:
            return dict(_incident)
        old_epoch = _epoch
        _epoch += 1
        _latched = True
        _incident = {
            "incident_id": str(uuid.uuid4()),
            "latched_at": time.time(),
            "source": source,
            "reason": reason,
            "old_epoch": old_epoch,
            "new_epoch": _epoch,
            "executions_at_latch": [
                {"id": l["id"], "kind": l["kind"], "label": l["label"],
                 "epoch": l["epoch"], "metadata": dict(l["metadata"])}
                for l in _executions.values()
            ],
            "tool_dispatches_at_latch": [
                {"id": l["id"], "name": l["name"], "epoch": l["epoch"]}
                for l in _tool_dispatches.values()
            ],
        }
        return dict(_incident)


def can_rearm() -> bool:
    with _lock:
        return _latched and not _executions and not _tool_dispatches


def rearm_local() -> bool:
    """Defense-in-depth: only the main thread may re-arm, and only once
    every execution/tool-dispatch lease has closed. Never decrements or
    resets the epoch — that increment is permanent for the process
    lifetime, which is what keeps pre-stop zombie work stale forever even
    after re-arm. No model-facing tool may call this."""
    global _latched
    if threading.current_thread() is not threading.main_thread():
        raise RearmBlocked("rearm_local() may only be called from the main thread")
    with _lock:
        if not _latched:
            raise RearmBlocked("emergency stop is not latched")
        if _executions or _tool_dispatches:
            raise RearmBlocked("cannot re-arm while executions or tool dispatches are still active")
        _latched = False
        return True


def snapshot() -> dict:
    """JSON-serializable, operator-telemetry-safe read-only copy. No
    function objects, secrets, environment values, tool results, or raw
    prompts — only whatever small metadata execution_scope() callers chose
    to attach."""
    with _lock:
        return {
            "latched": _latched,
            "epoch": _epoch,
            "incident": dict(_incident) if _incident else None,
            "active_execution_count": len(_executions),
            "active_executions": [
                {"id": l["id"], "kind": l["kind"], "label": l["label"],
                 "epoch": l["epoch"], "started_at": l["started_at"],
                 "metadata": dict(l["metadata"])}
                for l in _executions.values()
            ],
            "active_tool_dispatch_count": len(_tool_dispatches),
            "active_tool_dispatches": [
                {"id": l["id"], "name": l["name"], "epoch": l["epoch"], "started_at": l["started_at"]}
                for l in _tool_dispatches.values()
            ],
            "can_rearm": _latched and not _executions and not _tool_dispatches,
        }


def _reset_for_tests():
    """Test-only. Never call from product code. Clears latch/incident/lease
    state between tests; deliberately leaves the epoch untouched — it's
    monotonic for the life of the process by design, and tests should only
    ever assert relative epoch relationships, never an absolute value."""
    global _latched, _incident
    with _lock:
        _latched = False
        _incident = None
        _executions.clear()
        _tool_dispatches.clear()
