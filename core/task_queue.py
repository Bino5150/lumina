"""
core/task_queue.py — background job queue. Two trigger modes on one job
abstraction: immediate dispatch (background tasks -- user present, agent
doesn't want to block) and scheduled dispatch (run_at timestamp, fires with
nobody watching). Pure infra, no Lumina-specific logic -- tools/tasks.py is
the Lumina-facing layer on top of this.

Module-level state + module-level functions, same pattern as core/headless.py,
not a class-based singleton -- keeps this trivially monkeypatchable in tests
and matches the one-shared-resource philosophy already established there.
"""
import heapq
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from core import emergency_stop

MAX_CONCURRENT_TASKS = 2
RESULT_TTL_SECONDS = 30 * 60       # matches core/headless.py's IDLE_TIMEOUT_SECONDS convention
SCHEDULER_POLL_SECONDS = 5

_executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_TASKS)
_results: dict = {}                # task_id -> {"status", "result", "completed_at"}
_results_lock = threading.Lock()
_scheduled: list = []              # heap of (run_at, task_id, task_epoch, fn, args, kwargs)
_scheduled_lock = threading.Lock()
_scheduler_thread = None
_scheduler_thread_lock = threading.Lock()  # guards _scheduler_thread creation only
_scheduler_wake = threading.Event()        # lets schedule_task() interrupt a long idle sleep


def _dispatch_with_epoch(task_id: str, task_epoch: int, fn, args, kwargs):
    """Shared executor-dispatch core for submit_task() (epoch captured
    fresh at call time) and the scheduler (epoch captured back at
    schedule_task() time, retained verbatim even across a later E-stop +
    local re-arm -- see core/emergency_stop.py's execution_scope()).

    Runs fn() inside an emergency execution_scope pinned to task_epoch:
      - if task_epoch is already stale/latched when this actually reaches
        the executor, execution_scope() rejects immediately and fn() is
        never called -- result recorded as "cancelled";
      - if a latch happens WHILE fn() is running, execution_scope()'s lease
        keeps re-arm blocked for the duration, but doesn't interrupt fn();
        once fn() returns, if the process epoch has since moved on from
        task_epoch, the real result is discarded and the task is still
        recorded as "cancelled" rather than "success"/"error" -- a
        pre-stop background result must never be reinjected as if it
        completed cleanly after re-arm.
    """
    def _run():
        try:
            with emergency_stop.execution_scope(
                kind="background_task", label=task_id, expected_epoch=task_epoch,
            ):
                try:
                    result = fn(*args, **kwargs)
                    status = "success"
                except Exception as e:
                    result = {"error": str(e)}
                    status = "error"
        except emergency_stop.EmergencyStopError:
            with _results_lock:
                _results[task_id] = {"status": "cancelled", "result": None, "completed_at": time.time()}
            return

        if emergency_stop.current_epoch() != task_epoch:
            status = "cancelled"
            result = None

        with _results_lock:
            _results[task_id] = {"status": status, "result": result, "completed_at": time.time()}

    _executor.submit(_run)


def submit_task(fn, *args, task_id: str = None, **kwargs) -> str:
    """Immediate dispatch -- the background-task pattern. Returns task_id
    right away; fn runs on the shared executor. Never blocks the caller."""
    task_id = task_id or str(uuid.uuid4())
    try:
        task_epoch = emergency_stop.capture_epoch_for_new_work()
    except emergency_stop.EmergencyStopError:
        # Latched, or this call itself came from an already-stale execution
        # -- never run fn(), but still hand back a real task_id so callers
        # don't need a separate "was it even accepted" code path.
        with _results_lock:
            _results[task_id] = {"status": "cancelled", "result": None, "completed_at": time.time()}
        return task_id

    with _results_lock:
        _results[task_id] = {"status": "running", "result": None, "completed_at": None}
    _dispatch_with_epoch(task_id, task_epoch, fn, args, kwargs)
    return task_id


def schedule_task(run_at: float, fn, *args, task_id: str = None, **kwargs) -> str:
    """Scheduled dispatch -- fires at run_at (unix timestamp) with nobody
    watching. Returns task_id immediately; actual dispatch happens later via
    the scheduler loop, which retains the epoch captured here rather than
    ever recapturing a fresh one at fire time."""
    task_id = task_id or str(uuid.uuid4())
    try:
        task_epoch = emergency_stop.capture_epoch_for_new_work()
    except emergency_stop.EmergencyStopError:
        with _results_lock:
            _results[task_id] = {"status": "cancelled", "result": None, "completed_at": time.time()}
        return task_id

    with _scheduled_lock:
        heapq.heappush(_scheduled, (run_at, task_id, task_epoch, fn, args, kwargs))
    with _results_lock:
        _results[task_id] = {"status": "scheduled", "result": None, "completed_at": None}
    _ensure_scheduler_running()
    # The scheduler is a persistent, module-level singleton thread (same
    # pattern as core/headless.py) -- it can already be mid-sleep, computed
    # from a heap state that didn't include this item, when this call lands.
    # Without waking it explicitly, a task scheduled well before the current
    # sleep's deadline wouldn't be noticed until that sleep expires.
    _scheduler_wake.set()
    return task_id


def get_task_result(task_id: str):
    """Returns {"status", "result", "completed_at"} or None if unknown/expired.
    status is one of: "scheduled", "running", "success", "error", "cancelled"."""
    with _results_lock:
        entry = _results.get(task_id)
        if entry is None:
            return None
        if entry["completed_at"] and time.time() - entry["completed_at"] > RESULT_TTL_SECONDS:
            del _results[task_id]
            return None
        return dict(entry)


def list_active_tasks() -> list:
    """task_ids currently running or scheduled (not yet fired)."""
    with _results_lock:
        return [tid for tid, e in _results.items() if e["status"] in ("running", "scheduled")]


def list_all_tasks() -> list:
    """task_ids in any status still tracked -- scheduled, running, or a
    completed success/error result not yet past RESULT_TTL_SECONDS. Used
    by the Scheduled Tasks UI tab (S51 Part C) to show a full recent-tasks
    view, not just what's still active like list_active_tasks() above."""
    with _results_lock:
        return list(_results.keys())


def cancel_task(task_id: str) -> bool:
    """Cancel a task that hasn't started running yet -- i.e. still sitting
    in the scheduled heap, never dispatched to the executor. Returns True if
    cancelled, False if task_id isn't in the scheduled heap (already
    running, already completed, or never existed). Once dispatched to
    ThreadPoolExecutor there's no way to interrupt a thread already
    executing arbitrary work -- deliberately no "cancel a running task"
    path, only "not yet started" as scoped."""
    with _scheduled_lock:
        for i, item in enumerate(_scheduled):
            if item[1] == task_id:
                del _scheduled[i]
                heapq.heapify(_scheduled)
                break
        else:
            return False
    with _results_lock:
        _results[task_id] = {"status": "cancelled", "result": None, "completed_at": time.time()}
    return True


def _ensure_scheduler_running():
    global _scheduler_thread
    with _scheduler_thread_lock:
        if _scheduler_thread is None or not _scheduler_thread.is_alive():
            _scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True)
            _scheduler_thread.start()


def _scheduler_loop():
    """Polls the scheduled-task heap, dispatching anything due via
    submit_task(). Waits until the next scheduled item's run_at or
    SCHEDULER_POLL_SECONDS, whichever is sooner -- a fixed sleep(POLL) every
    iteration would mean a task scheduled 1 second out could wait up to a
    full poll interval to actually fire, regardless of how soon it's due.

    Waits on _scheduler_wake rather than time.sleep() so schedule_task() can
    interrupt an in-progress wait. This is a persistent, module-level
    singleton thread (same pattern as core/headless.py) -- once it's asleep,
    computed from whatever the heap looked like at that moment, a plain
    sleep() would have no way to notice a newly scheduled near-term item
    until that sleep's fixed duration ran out regardless of how soon the new
    item is actually due.

    Daemon thread -- dies with the process, no explicit shutdown path needed."""
    while True:
        now = time.time()
        due = []
        with _scheduled_lock:
            while _scheduled and _scheduled[0][0] <= now:
                due.append(heapq.heappop(_scheduled))
            next_run_at = _scheduled[0][0] if _scheduled else None
        for run_at, task_id, task_epoch, fn, args, kwargs in due:
            # Deliberately NOT submit_task() -- that would call
            # capture_epoch_for_new_work() again and stamp the job with
            # whatever epoch is current *now*, defeating the whole point of
            # a scheduled job becoming permanently non-executable once its
            # original epoch goes stale. Retain the epoch captured back at
            # schedule_task() time instead.
            with _results_lock:
                _results[task_id] = {"status": "running", "result": None, "completed_at": None}
            _dispatch_with_epoch(task_id, task_epoch, fn, args, kwargs)
        if next_run_at is not None:
            wait_for = max(0.05, min(SCHEDULER_POLL_SECONDS, next_run_at - time.time()))
        else:
            wait_for = SCHEDULER_POLL_SECONDS
        _scheduler_wake.wait(timeout=wait_for)
        _scheduler_wake.clear()
