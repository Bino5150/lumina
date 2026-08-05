"""ui/settings.py ToolsTab -- S51 Part B: SUBAGENTS_ENABLED/
BACKGROUND_TASKS_ENABLED toggles + MAX_SUBAGENT_DEPTH spinbox.

Genuinely PySide6-dependent (constructs a real QWidget), unlike the pure
core/chat_render.py logic the earlier CI fix (S51) extracted -- there's no
equivalent extraction available here since this IS the widget wiring under
test. skipped, not failed, in CI (which deliberately never installs
PySide6, see tests.yml's own header) via the importorskip guard below --
same category of gap flagged, not fixed, during that CI investigation.

Uses a real tools.registry.ToolRegistry (not a full LuminaAgent -- no DB
init, no 72-tool load) so register_subagent_tools()/register_task_tools()
run against genuine register()/enable()/disable()/all_tool_names() behavior,
proving the toggle actually changes what those functions see, not just that
a checkbox's visual state changed.
"""
import os
import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace
import config
from core import persistence
from tools.registry import ToolRegistry


@pytest.fixture
def isolated_prefs(tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(config, "SUBAGENTS_ENABLED", False)
    monkeypatch.setattr(config, "BACKGROUND_TASKS_ENABLED", False)
    monkeypatch.setattr(config, "MAX_SUBAGENT_DEPTH", 2)


@pytest.fixture
def tab(isolated_prefs):
    from PySide6.QtWidgets import QApplication
    from ui.main_window import COLORS
    from ui.settings import ToolsTab

    QApplication.instance() or QApplication([])
    fake_agent = SimpleNamespace(registry=ToolRegistry(), _subagent_depth=0,
                                  _background_task_ids=set())
    return ToolsTab(fake_agent, COLORS)


def test_initial_state_reflects_config_defaults(tab):
    assert tab.subagents_enabled_cb.isChecked() is False
    assert tab.background_tasks_enabled_cb.isChecked() is False
    assert tab.subagent_depth_spin.value() == 2
    assert "spawn_subagent" not in tab.agent.registry.all_tool_names()


def test_toggling_subagents_on_registers_and_enables_the_tool(tab):
    tab.subagents_enabled_cb.setChecked(True)

    assert config.SUBAGENTS_ENABLED is True
    assert "spawn_subagent" in tab.agent.registry.all_tool_names()
    assert tab.agent.registry.is_enabled("spawn_subagent")
    assert persistence.load()["subagents_enabled"] is True


def test_toggling_subagents_off_disables_without_unregistering(tab):
    tab.subagents_enabled_cb.setChecked(True)
    tab.subagents_enabled_cb.setChecked(False)

    assert config.SUBAGENTS_ENABLED is False
    # Registration is one-time and additive (ToolRegistry has no unregister) --
    # the tool stays present but must no longer be enabled/dispatchable.
    assert "spawn_subagent" in tab.agent.registry.all_tool_names()
    assert not tab.agent.registry.is_enabled("spawn_subagent")
    assert persistence.load()["subagents_enabled"] is False


def test_toggling_on_off_on_does_not_duplicate_registration(tab):
    """The live-apply handlers call register_subagent_tools()/
    register_task_tools() again on every re-enable, on the assumption that
    re-registering an already-registered name is a harmless overwrite, not
    a duplicate entry. Verified directly here, not just inferred from
    ToolRegistry.register()'s dict-keyed-by-name implementation -- toggle
    on, off, on again, and confirm the tool count and callability are
    unaffected by having gone through registration twice."""
    tab.subagents_enabled_cb.setChecked(True)
    count_after_first_on = len(tab.agent.registry.all_tool_names())

    tab.subagents_enabled_cb.setChecked(False)
    tab.subagents_enabled_cb.setChecked(True)

    assert len(tab.agent.registry.all_tool_names()) == count_after_first_on
    assert tab.agent.registry.is_enabled("spawn_subagent")

    # Same check for the three background-task tools.
    tab.background_tasks_enabled_cb.setChecked(True)
    count_after_bg_on = len(tab.agent.registry.all_tool_names())
    tab.background_tasks_enabled_cb.setChecked(False)
    tab.background_tasks_enabled_cb.setChecked(True)
    assert len(tab.agent.registry.all_tool_names()) == count_after_bg_on
    for name in ("run_background_subagent", "check_background_task", "schedule_background_subagent"):
        assert tab.agent.registry.is_enabled(name)


def test_toggling_background_tasks_on_registers_all_three_tools(tab):
    tab.background_tasks_enabled_cb.setChecked(True)

    assert config.BACKGROUND_TASKS_ENABLED is True
    for name in ("run_background_subagent", "check_background_task", "schedule_background_subagent"):
        assert name in tab.agent.registry.all_tool_names()
        assert tab.agent.registry.is_enabled(name)
    assert persistence.load()["background_tasks_enabled"] is True


def test_toggles_are_independent_of_each_other(tab):
    """SUBAGENTS_ENABLED and BACKGROUND_TASKS_ENABLED gate separate tool
    families -- flipping one must not touch the other's registration."""
    tab.background_tasks_enabled_cb.setChecked(True)

    assert "spawn_subagent" not in tab.agent.registry.all_tool_names()
    assert config.SUBAGENTS_ENABLED is False


def test_depth_spinbox_persists_and_updates_config(tab):
    tab.subagent_depth_spin.setValue(4)
    tab._on_subagent_depth_changed()  # editingFinished doesn't fire reliably offscreen

    assert config.MAX_SUBAGENT_DEPTH == 4
    assert persistence.load()["max_subagent_depth"] == 4


def test_ui_reflects_prefs_stored_override_not_just_shipped_default(isolated_prefs, monkeypatch):
    """MB-24-class gotcha check (per the task's own instruction): confirm
    the widget shows whatever's actually active -- a prefs-stored override --
    not just config.py's bare default, on load."""
    monkeypatch.setattr(config, "SUBAGENTS_ENABLED", True)
    monkeypatch.setattr(config, "MAX_SUBAGENT_DEPTH", 5)

    from PySide6.QtWidgets import QApplication
    from ui.main_window import COLORS
    from ui.settings import ToolsTab

    QApplication.instance() or QApplication([])
    fake_agent = SimpleNamespace(registry=ToolRegistry(), _subagent_depth=0,
                                  _background_task_ids=set())
    tab = ToolsTab(fake_agent, COLORS)

    assert tab.subagents_enabled_cb.isChecked() is True
    assert tab.subagent_depth_spin.value() == 5
