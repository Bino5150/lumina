"""ui/settings.py ToolsTab pending-tools panel -- MB-28 regression test.

Genuinely PySide6-dependent (constructs the real ToolsTab), same
importorskip guard as test_settings_subagent_toggles.py (S51 Part B) and
test_settings_scheduled_tasks_tab.py (S51 Part C) for the same reason --
skips cleanly in CI (which deliberately never installs PySide6) rather
than failing collection.

Covers the exact bug class Part C covered for ScheduledTasksTab, in the
Pending Tools panel: _approve_pending()/_reject_pending() set
pending_status_lbl to a confirmation message, then immediately called
_load_pending_tools(), which unconditionally overwrote the label with its
own empty-state text -- the confirmation was never actually visible.

The logic-level tests in tests/test_toolmaker.py cannot see this bug: they
drive tools/toolmaker.py with fake registries and never touch a QLabel.
The clobber happens entirely in the Qt widget refresh layer, so this file
constructs the real panel, drives the real approve/reject handlers (dialog
auto-confirmed), and asserts the label text survives the follow-up refresh
-- the only way to prove this exact bug class is actually gone.
"""
import os
import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace
from core import persistence
from tools.registry import ToolRegistry


VALID_TOOL_CODE = """
def hello_world():
    return "hi from custom tool"

def register_hello_world_tool(registry):
    registry.register(name="hello_world", fn=hello_world, description="says hi",
                       parameters={"type": "object", "properties": {}, "required": []})
"""


def _stage_pending_tool(toolmaker, name="hello_world"):
    os.makedirs(toolmaker.PENDING_DIR, exist_ok=True)
    path = os.path.join(toolmaker.PENDING_DIR, f"{name}.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(VALID_TOOL_CODE)
    return path


@pytest.fixture
def tab(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication, QMessageBox
    from ui.main_window import COLORS
    from ui.settings import ToolsTab
    from tools import toolmaker

    QApplication.instance() or QApplication([])

    # Hermetic file ops: prefs, the _pending/ staging dir, the hotload dir,
    # and the audit log all land in tmp_path -- nothing touches the real ones.
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(toolmaker, "PENDING_DIR", str(tmp_path / "_pending"))
    monkeypatch.setattr(toolmaker, "CUSTOM_TOOLS_DIR", str(tmp_path / "custom"))
    monkeypatch.setattr(toolmaker, "AUDIT_LOG_PATH", str(tmp_path / "tool_audit.log"))
    # approve_pending_tool() os.replace()'s into CUSTOM_TOOLS_DIR -- the real
    # app never hits this because tools/ always exists, but the monkeypatched
    # path above points at a directory that doesn't exist yet until created.
    os.makedirs(toolmaker.CUSTOM_TOOLS_DIR, exist_ok=True)

    fake_agent = SimpleNamespace(registry=ToolRegistry(), _subagent_depth=0,
                                  _background_task_ids=set())
    t = ToolsTab(fake_agent, COLORS)

    # The QMessageBox.question confirm dialogs aren't under test -- auto-confirm.
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.Yes))
    return t


def test_reject_pending_preserves_confirmation_message(tab):
    """MB-28: _reject_pending() set pending_status_lbl to "Discarded 'x'.",
    then _load_pending_tools() overwrote it before the event loop ever
    painted. The confirmation must survive the refresh."""
    from tools import toolmaker
    _stage_pending_tool(toolmaker)
    tab._load_pending_tools()
    tab.pending_list.selectRow(0)

    tab._reject_pending()

    assert "Discarded" in tab.pending_status_lbl.text()
    # The pending dir is now empty, so an un-guarded refresh would have
    # replaced the confirmation with exactly this -- the bug's fingerprint.
    assert tab.pending_status_lbl.text() != "Nothing pending review."
    assert not os.path.exists(os.path.join(toolmaker.PENDING_DIR, "hello_world.py"))


def test_reject_error_message_survives_refresh(tab, monkeypatch):
    """Same bug, error path: the "Error rejecting" message set in the
    except branch was also clobbered by the unconditional refresh."""
    from tools import toolmaker
    _stage_pending_tool(toolmaker)
    tab._load_pending_tools()
    tab.pending_list.selectRow(0)

    monkeypatch.setattr(toolmaker, "_append_audit_log",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    tab._reject_pending()

    assert "Error rejecting" in tab.pending_status_lbl.text()


def test_approve_pending_preserves_confirmation_message(tab):
    """MB-28 bonus catch: _approve_pending() had the same clobber -- the
    approve_pending_tool() result string was written to the label, then
    wiped by _load_pending_tools() in the same call chain."""
    from tools import toolmaker
    _stage_pending_tool(toolmaker)
    tab._load_pending_tools()
    tab.pending_list.selectRow(0)

    tab._approve_pending()

    text = tab.pending_status_lbl.text()
    assert text != ""  # something actually got written
    assert text != "Nothing pending review."  # ...and it wasn't clobbered
    assert "approved" in text.lower() or "live" in text.lower()
    # The tool really went live in the real registry, not just the label.
    assert "hello_world" in tab.agent.registry.all_tool_names()
    assert tab.agent.registry.is_enabled("hello_world")


def test_default_load_pending_tools_still_overwrites_status(tab):
    """The guard must not change the default refresh path: a bare
    _load_pending_tools() call (init, manual refresh) still resets the
    label to the empty state -- preserve_status is opt-in only."""
    tab.pending_status_lbl.setText("stale message from a previous action")

    tab._load_pending_tools()

    assert tab.pending_status_lbl.text() == "Nothing pending review."
