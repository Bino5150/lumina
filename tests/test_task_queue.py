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
