import time
from core import task_queue


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
