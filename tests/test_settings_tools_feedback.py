"""Patch 3A.3B Part E / R13 -- proves ToolsTab's profile-bar "💾 Save"
button gives truthful semantic feedback, that the existing "No Profile"
warning is untouched, and that a write failure surfaces visibly instead of
silently doing nothing.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox
from types import SimpleNamespace

import core.tool_profiles as tool_profiles_module
from tools.registry import ToolRegistry


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _pump_until(condition, timeout=2.0, interval=0.005):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if condition():
            return True
        time.sleep(interval)
    QApplication.processEvents()
    return condition()


@pytest.fixture
def tab(qapp, tmp_path, monkeypatch):
    from ui.main_window import COLORS
    from ui.settings._widgets import ButtonFeedback
    from ui.settings.tools_tab import ToolsTab
    from core import persistence

    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(tool_profiles_module, "PROFILES_DIR", str(tmp_path / "tool_profiles"))
    monkeypatch.setattr(ButtonFeedback, "HOLD_MS", 30)

    registry = ToolRegistry()
    registry.register(name="dummy_tool", fn=lambda: None, description="test",
                       parameters={"type": "object", "properties": {}, "required": []})
    agent = SimpleNamespace(registry=registry, _subagent_depth=0, _background_task_ids=set())
    t = ToolsTab(agent, COLORS)
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    return t


def _make_profile_file(tab, name="Research"):
    from core.tool_profiles import save_profile, fname_from_name, PROFILES_DIR
    path = os.path.join(PROFILES_DIR, fname_from_name(name))
    save_profile(path, {"name": name, "description": "", "enabled": []})
    tab._load_profiles()
    # setCurrentIndex(0) is a no-op (no currentIndexChanged signal, so
    # _on_profile_selected never runs) when index 0 -- the sole profile
    # here -- is already the combo's default post-reload selection. Set
    # the path directly; profile *selection* UI is not what's under test.
    tab._current_profile_path = path
    return path


def test_r13_successful_write_shows_saved(tab):
    path = _make_profile_file(tab)

    tab._save_profile()

    assert tab.save_profile_btn.text() == "✓ Saved"
    with open(path) as f:
        data = json.load(f)
    assert "dummy_tool" in data["enabled"]
    assert _pump_until(lambda: tab.save_profile_btn.text() == "💾 Save")


def test_r13_no_profile_warning_preserved(tab, monkeypatch):
    calls = []
    monkeypatch.setattr(QMessageBox, "warning",
                         staticmethod(lambda *a, **k: calls.append(a) or None))
    tab._current_profile_path = None

    tab._save_profile()

    assert len(calls) == 1, "existing 'No Profile' warning must still fire"
    # Button feedback must not fire on the no-profile precondition bail-out.
    assert tab.save_profile_btn.text() == "💾 Save"


def test_r13_write_failure_is_visible(tab, monkeypatch):
    _make_profile_file(tab)
    monkeypatch.setattr(
        tool_profiles_module, "save_profile",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )

    tab._save_profile()

    assert tab.save_profile_btn.text() == "✗ Failed"
    assert "disk full" in tab.save_profile_btn.toolTip()
    assert not _pump_until(lambda: tab.save_profile_btn.text() != "✗ Failed", timeout=0.15)


def test_pending_tools_and_toggles_semantics_unaffected(tab):
    """Regression guard: 3A.3B must not touch Pending Tools/Actions,
    Enable/Disable All, or per-tool toggle behavior in this tab."""
    tab.agent.registry.disable("dummy_tool")
    assert not tab.agent.registry.is_enabled("dummy_tool")
    tab._enable_all()
    assert tab.agent.registry.is_enabled("dummy_tool")
    tab._disable_all()
    assert not tab.agent.registry.is_enabled("dummy_tool")
