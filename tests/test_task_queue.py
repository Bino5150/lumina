import threading
import time

import pytest

from core import emergency_stop
from core import task_queue


@pytest.fixture(autouse=True)
def _clean_emergency_state():
    """A few tests below exercise cancel_all_scheduled() under a real
    latch. Reset unconditionally before and after every test in this file
    so a mid-test assertion failure can never leak a stuck latch into the
    rest of this file or the wider suite (matches tests/test_emergency_stop.py's
    own isolation fixture)."""
    emergency_stop._reset_for_tests()
    yield
    emergency_stop._reset_for_tests()


def test_submit_task_returns_result():
    tid = task_queue.submit_task(lambda x: x * 2, 21)
    for _ in range(50):
        r = task_queue.get_task_result(tid)
        if r["status"] != "running":
            break
        time.sleep(0.05)
    assert r["status"] == "success"
    assert r["result"] == 42


def test_submit_task_captures_exceptions_as_error_status():
    def _boom():
        raise ValueError("nope")
    tid = task_queue.submit_task(_boom)
    for _ in range(50):
        r = task_queue.get_task_result(tid)
        if r["status"] != "running":
            break
        time.sleep(0.05)
    assert r["status"] == "error"
    assert "nope" in r["result"]["error"]


def test_get_task_result_unknown_id_returns_none():
    assert task_queue.get_task_result("does-not-exist") is None


def test_schedule_task_fires_at_run_at():
    run_at = time.time() + 0.2
    tid = task_queue.schedule_task(run_at, lambda: "done")
    # Immediately after scheduling, should not have fired yet
    r = task_queue.get_task_result(tid)
    assert r["status"] == "scheduled"
    time.sleep(0.5)
    r = task_queue.get_task_result(tid)
    assert r["status"] == "success"
    assert r["result"] == "done"


def test_list_active_tasks_excludes_completed():
    tid = task_queue.submit_task(lambda: "quick")
    time.sleep(0.3)
    assert tid not in task_queue.list_active_tasks()


def test_list_all_tasks_includes_completed_unlike_list_active_tasks():
    """S51 Part C — the Scheduled Tasks tab needs to show completed tasks
    too, not just still-active ones."""
    tid = task_queue.submit_task(lambda: "quick")
    for _ in range(50):
        r = task_queue.get_task_result(tid)
        if r["status"] != "running":
            break
        time.sleep(0.05)
    assert tid not in task_queue.list_active_tasks()
    assert tid in task_queue.list_all_tasks()


def test_cancel_task_removes_a_not_yet_fired_scheduled_task():
    run_at = time.time() + 60  # far enough out it can't fire during the test
    tid = task_queue.schedule_task(run_at, lambda: "should never run")

    assert task_queue.cancel_task(tid) is True
    r = task_queue.get_task_result(tid)
    assert r["status"] == "cancelled"

    # And it genuinely won't fire later -- not just marked cancelled while
    # still sitting in the heap.
    time.sleep(0.2)
    assert task_queue.get_task_result(tid)["status"] == "cancelled"


def test_cancel_task_returns_false_for_unknown_or_already_running_task():
    assert task_queue.cancel_task("does-not-exist") is False

    tid = task_queue.submit_task(lambda: (time.sleep(0.3), "done")[1])
    # Already dispatched to the executor, not sitting in the scheduled heap.
    assert task_queue.cancel_task(tid) is False


# ---------------------------------------------------------------------------
# 3A.2 Part B -- cancel_all_scheduled()
# ---------------------------------------------------------------------------

def test_cancel_all_scheduled_removes_and_cancels_multiple_jobs():
    # The scheduled heap is genuine process-wide shared state (by design --
    # that's what lets an emergency stop sweep ALL scheduled work with no
    # notion of "whose" job it is). Some other test file in the same
    # session (tests/test_settings_scheduled_tasks_tab.py) schedules
    # far-future jobs without cleaning them up -- harmless before this
    # function existed, but this function has no way to know they aren't
    # its own. Drain first for a clean baseline rather than asserting an
    # empty-heap assumption this module doesn't own.
    task_queue.cancel_all_scheduled()

    far = time.time() + 60
    ran = threading.Event()
    tids = [task_queue.schedule_task(far, lambda: ran.set()) for _ in range(3)]

    cancelled = task_queue.cancel_all_scheduled()

    assert set(cancelled) == set(tids)
    for tid in tids:
        r = task_queue.get_task_result(tid)
        assert r["status"] == "cancelled"
        assert r["completed_at"] is not None
    time.sleep(0.2)
    assert not ran.is_set()


def test_cancel_all_scheduled_leaves_completed_and_running_jobs_untouched():
    done_tid = task_queue.submit_task(lambda: "quick")
    for _ in range(50):
        r = task_queue.get_task_result(done_tid)
        if r["status"] != "running":
            break
        time.sleep(0.05)
    assert r["status"] == "success"

    entered = threading.Event()
    release = threading.Event()

    def _slow():
        entered.set()
        release.wait(timeout=5)
        return "finished"

    running_tid = task_queue.submit_task(_slow)
    assert entered.wait(timeout=5)

    cancelled = task_queue.cancel_all_scheduled()
    assert cancelled == []  # nothing in the heap -- neither job was ever scheduled

    assert task_queue.get_task_result(done_tid)["status"] == "success"
    assert task_queue.get_task_result(running_tid)["status"] == "running"

    release.set()
    for _ in range(50):
        r = task_queue.get_task_result(running_tid)
        if r["status"] != "running":
            break
        time.sleep(0.05)
    assert r["status"] == "success"
    assert r["result"] == "finished"


def test_cancel_all_scheduled_pop_race_job_still_cannot_execute(monkeypatch):
    """A job the scheduler already popped off the heap (submitted to the
    executor, about to enter its execution_scope() admission check) a
    moment before cancel_all_scheduled() runs isn't reported here -- it's
    not in the heap any more by the time this reads it -- but it still
    can't execute its function, because Patch 3A.1's own epoch enforcement
    in _dispatch_with_epoch() is the real backstop for that race,
    independent of this function. Gate execution_scope() itself (rather
    than relying on timing) to deterministically land in that exact
    window: popped-and-dispatched, but not yet admitted."""
    ran = threading.Event()
    popped = threading.Event()
    release = threading.Event()

    real_execution_scope = emergency_stop.execution_scope

    def _gated_execution_scope(*args, **kwargs):
        popped.set()
        assert release.wait(timeout=5)
        return real_execution_scope(*args, **kwargs)

    monkeypatch.setattr(task_queue.emergency_stop, "execution_scope", _gated_execution_scope)

    run_at = time.time() + 0.05
    tid = task_queue.schedule_task(run_at, lambda: ran.set())
    assert popped.wait(timeout=5)  # popped off the heap, dispatched, held right before admission

    emergency_stop.latch(source="test", reason="pop-race")
    cancelled = task_queue.cancel_all_scheduled()
    assert tid not in cancelled  # already off the heap, not falsely reported here

    release.set()

    r = None
    for _ in range(100):
        r = task_queue.get_task_result(tid)
        if r["status"] != "running":
            break
        time.sleep(0.02)
    assert r["status"] == "cancelled"
    assert not ran.is_set()  # admission itself was rejected -- fn() never ran

    emergency_stop.rearm_local()


def test_cancel_all_scheduled_during_latched_state_is_safe_and_idempotent():
    # Schedule while NOT latched so the jobs actually land in the heap --
    # scheduling while already latched is 3A.1's own concern (schedule_task
    # rejects at creation time), not this function's.
    far = time.time() + 60
    tids = [task_queue.schedule_task(far, lambda: None) for _ in range(2)]

    emergency_stop.latch(source="test", reason="cancel-all-while-latched")

    first = task_queue.cancel_all_scheduled()
    assert set(first) == set(tids)

    second = task_queue.cancel_all_scheduled()
    assert second == []  # idempotent -- nothing left to cancel

    for tid in tids:
        assert task_queue.get_task_result(tid)["status"] == "cancelled"

    emergency_stop.rearm_local()
