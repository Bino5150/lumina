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

MAX_CONCURRENT_TASKS = 2
RESULT_TTL_SECONDS = 30 * 60       # matches core/headless.py's IDLE_TIMEOUT_SECONDS convention
SCHEDULER_POLL_SECONDS = 5

_executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_TASKS)
_results: dict = {}                # task_id -> {"status", "result", "completed_at"}
_results_lock = threading.Lock()
_scheduled: list = []              # heap of (run_at, task_id, fn, args, kwargs)
_scheduled_lock = threading.Lock()
_scheduler_thread = None
_scheduler_thread_lock = threading.Lock()  # guards _scheduler_thread creation only
_scheduler_wake = threading.Event()        # lets schedule_task() interrupt a long idle sleep


def submit_task(fn, *args, task_id: str = None, **kwargs) -> str:
    """Immediate dispatch -- the background-task pattern. Returns task_id
    right away; fn runs on the shared executor. Never blocks the caller."""
    task_id = task_id or str(uuid.uuid4())
    with _results_lock:
        _results[task_id] = {"status": "running", "result": None, "completed_at": None}

    def _run():
        try:
            result = fn(*args, **kwargs)
            status = "success"
        except Exception as e:
            result = {"error": str(e)}
            status = "error"
        with _results_lock:
            _results[task_id] = {"status": status, "result": result, "completed_at": time.time()}

    _executor.submit(_run)
    return task_id


def schedule_task(run_at: float, fn, *args, task_id: str = None, **kwargs) -> str:
    """Scheduled dispatch -- fires at run_at (unix timestamp) with nobody
    watching. Returns task_id immediately; actual dispatch happens later via
    the scheduler loop, which reuses submit_task() when a job comes due."""
    task_id = task_id or str(uuid.uuid4())
    with _scheduled_lock:
        heapq.heappush(_scheduled, (run_at, task_id, fn, args, kwargs))
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
    status is one of: "scheduled", "running", "success", "error"."""
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
        for run_at, task_id, fn, args, kwargs in due:
            submit_task(fn, *args, task_id=task_id, **kwargs)
        if next_run_at is not None:
            wait_for = max(0.05, min(SCHEDULER_POLL_SECONDS, next_run_at - time.time()))
        else:
            wait_for = SCHEDULER_POLL_SECONDS
        _scheduler_wake.wait(timeout=wait_for)
        _scheduler_wake.clear()
