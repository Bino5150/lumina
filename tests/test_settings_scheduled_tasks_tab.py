"""ui/settings.py ScheduledTasksTab -- S51 Part C.

Genuinely PySide6-dependent, same importorskip guard as
test_settings_subagent_toggles.py (S51 Part B) for the same reason -- skips
cleanly in CI (which deliberately never installs PySide6) rather than
failing collection.

Uses the real core.task_queue module directly (submit_task/schedule_task/
cancel_task) -- task_queue's own logic is already covered by
tests/test_task_queue.py, this covers the tab's read path (does it render
that real state correctly) and the cancel action, per the task's own scope.
"""
import os
import time
import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace
from core import task_queue


def _wait_for_terminal(tid, timeout=5):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = task_queue.get_task_result(tid)
        if r["status"] not in ("running", "scheduled"):
            return r
        time.sleep(0.02)
    raise TimeoutError(f"{tid} never left running/scheduled")


@pytest.fixture
def tab():
    from PySide6.QtWidgets import QApplication
    from ui.main_window import COLORS
    from ui.settings import ScheduledTasksTab

    QApplication.instance() or QApplication([])
    fake_agent = SimpleNamespace()
    return ScheduledTasksTab(fake_agent, COLORS)


def test_read_path_renders_completed_and_scheduled_tasks(tab):
    success_tid = task_queue.submit_task(lambda: "the answer")
    _wait_for_terminal(success_tid)
    scheduled_tid = task_queue.schedule_task(time.time() + 120, lambda: "later")

    tab._load_tasks()

    assert success_tid in tab._task_ids
    assert scheduled_tid in tab._task_ids
    rows = {tab.table.item(r, 0).text(): tab.table.item(r, 1).text()
            for r in range(tab.table.rowCount())}
    assert rows[success_tid] == "success"
    assert rows[scheduled_tid] == "scheduled"


def test_selecting_a_row_shows_its_result_in_the_preview(tab):
    tid = task_queue.submit_task(lambda: "a specific distinctive result")
    _wait_for_terminal(tid)
    tab._load_tasks()

    row = tab._task_ids.index(tid)
    tab.table.selectRow(row)

    assert "a specific distinctive result" in tab.preview.toPlainText()


def test_cancel_button_enabled_only_for_scheduled_not_completed(tab):
    success_tid = task_queue.submit_task(lambda: "done")
    _wait_for_terminal(success_tid)
    scheduled_tid = task_queue.schedule_task(time.time() + 120, lambda: "later")
    tab._load_tasks()

    tab.table.selectRow(tab._task_ids.index(success_tid))
    assert tab.cancel_btn.isEnabled() is False

    tab.table.selectRow(tab._task_ids.index(scheduled_tid))
    assert tab.cancel_btn.isEnabled() is True


def test_cancel_action_cancels_and_shows_confirmation_message(tab):
    """Regression test for a bug caught live while building this tab:
    _cancel_selected() set status_lbl to a confirmation message, then
    immediately called _load_tasks(), which unconditionally overwrote
    status_lbl with its own empty-state text -- the confirmation was never
    actually visible. Fixed via _load_tasks(preserve_status=True)."""
    scheduled_tid = task_queue.schedule_task(time.time() + 120, lambda: "later")
    tab._load_tasks()
    tab.table.selectRow(tab._task_ids.index(scheduled_tid))

    tab._cancel_selected()

    assert task_queue.get_task_result(scheduled_tid)["status"] == "cancelled"
    assert scheduled_tid in tab.status_lbl.text()
    assert "cancel" in tab.status_lbl.text().lower()


def test_cancel_on_already_completed_task_reports_failure_cleanly(tab):
    tid = task_queue.submit_task(lambda: "already done")
    _wait_for_terminal(tid)
    tab._load_tasks()
    # Manually select even though the button would normally be disabled --
    # confirms _cancel_selected() itself is safe, not just the UI gating.
    tab._task_ids = [tid]
    tab.table.selectRow(0)

    tab._cancel_selected()

    assert "could not cancel" in tab.status_lbl.text().lower()
    assert task_queue.get_task_result(tid)["status"] == "success"  # untouched
