"""Focused responsive-layout regression for Settings > Tools
(TOOLS-SETTINGS-LAYOUT-01).

Real-Qt geometry test, same convention as test_settings_general_layout.py.
Before this fix, ToolsTab laid out five stacked sections (profile bar,
Subagents & Background Tasks, Pending Tools, Pending Actions, tool table)
directly in one QVBoxLayout with no QScrollArea anywhere in the tab. Two
of those sections each contained a pair of setFixedHeight(110) widgets,
so the whole tab's layout-minimum was ~880px tall -- at the app's declared
minimum window size (960x660, see ui/main_window.py's
setMinimumSize(960, 660)) the only flexible widget, the tool table (the
lone stretch=1 item), absorbed the entire deficit and was squeezed to a
~1-row sliver.

The fix wraps everything above the tool table in the same _scroll_wrap()
QScrollArea pattern GeneralTab/TTSTab/CommunicationsTab/UserProfileTab
already use, bounded between a floor (setMinimumHeight) and a ceiling
(setMaximumHeight) so it can neither collapse the tool table nor soak up
window growth that should go to the table instead -- both bounds were
picked from an actual measured run of this exact widget tree (see the
class docstring's measurements in tools_tab.py's _build()), not guessed.
"""

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QScrollArea

from ui.main_window import COLORS
from ui.settings.tools_tab import ToolsTab
from tools.registry import ToolRegistry


# Matches ui/main_window.py's _setup_window(): setMinimumSize(960, 660),
# minus the 50px header (_build_header) and the QTabWidget's own tab bar
# (~40px from its stylesheet's padding/font -- not pixel-exact, just the
# same rough chrome budget test_settings_general_layout.py's 700px uses).
MIN_SUPPORTED_HEIGHT = 570
DEFAULT_HEIGHT = 670
TALL_HEIGHT = 910
ROW_HEIGHT_PX = 30  # QTableWidget default row height at this stylesheet's font size


@pytest.fixture
def tab(tmp_path, monkeypatch):
    from core import persistence

    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))

    app = QApplication.instance() or QApplication([])
    registry = ToolRegistry()
    for i in range(15):
        registry.register(
            name=f"tool_{i}", fn=lambda: None,
            description=f"Test tool number {i} does a thing.",
            parameters={"type": "object", "properties": {}, "required": []},
        )
    fake_agent = SimpleNamespace(registry=registry, _subagent_depth=0,
                                  _background_task_ids=set())
    t = ToolsTab(fake_agent, COLORS)
    t.show()
    app.processEvents()
    yield t
    t.close()


def test_tool_table_has_a_usable_viewport_at_minimum_window_size(tab):
    """Acceptance criterion 1: at the project's minimum supported window
    size, the tool list must show multiple rows, not a sliver."""
    app = QApplication.instance()
    tab.resize(960, MIN_SUPPORTED_HEIGHT)
    app.processEvents()

    visible_rows = tab.table.viewport().height() // ROW_HEIGHT_PX
    assert visible_rows >= 3, (
        f"only {visible_rows} tool rows visible "
        f"(viewport height {tab.table.viewport().height()}px) at the minimum "
        "supported window size -- the tool list is back to being a sliver"
    )


def test_growing_window_expands_table_not_upper_sections(tab):
    """Acceptance criterion 2: additional window height must go to the
    tool table, not sit as extra room in the upper (profile/subagents/
    pending) sections above it."""
    app = QApplication.instance()
    upper_scroll = tab.findChild(QScrollArea)

    tab.resize(960, MIN_SUPPORTED_HEIGHT)
    app.processEvents()
    small_table_h = tab.table.height()
    small_upper_h = upper_scroll.height()

    tab.resize(960, TALL_HEIGHT)
    app.processEvents()
    tall_table_h = tab.table.height()
    tall_upper_h = upper_scroll.height()

    grown = TALL_HEIGHT - MIN_SUPPORTED_HEIGHT
    table_growth = tall_table_h - small_table_h
    upper_growth = tall_upper_h - small_upper_h

    assert table_growth >= grown - 5, (
        f"table only grew {table_growth}px of the {grown}px added to the "
        f"window (upper sections grew {upper_growth}px instead)"
    )
    assert upper_growth <= 5, (
        f"upper sections grew {upper_growth}px when the window grew -- "
        "they should stay pinned to their capped height"
    )


def test_upper_sections_stay_reachable_and_do_not_steal_stretch(tab):
    """Acceptance criterion 3 + "no dead space in upper panels": the
    scroll-wrapped upper region (profile bar, Subagents & Background
    Tasks, Pending Tools, Pending Actions) must never exceed a bounded
    ceiling, and Pending Tools / Pending Actions must still be real,
    visible descendants of it -- reachable by scrolling, not removed."""
    app = QApplication.instance()
    upper_scroll = tab.findChild(QScrollArea)

    for height in (MIN_SUPPORTED_HEIGHT, DEFAULT_HEIGHT, TALL_HEIGHT):
        tab.resize(960, height)
        app.processEvents()
        assert upper_scroll.height() <= 280, (
            f"upper section is {upper_scroll.height()}px tall at window "
            f"height {height} -- it is stealing stretch from the table"
        )

    assert upper_scroll.isAncestorOf(tab.pending_list)
    assert upper_scroll.isAncestorOf(tab.pending_actions_list)
    assert upper_scroll.isAncestorOf(tab.profile_combo)


def test_constrained_height_degrades_through_scrolling_not_table_collapse(tab):
    """Below the app's declared minimum window size (a defensive check --
    ui/main_window.py's setMinimumSize(960, 660) should prevent the app
    from ever reaching this in practice), the upper region must expose an
    active scrollbar instead of the deficit landing back on the table."""
    app = QApplication.instance()
    tab.resize(960, 380)
    app.processEvents()

    upper_scroll = tab.findChild(QScrollArea)
    assert upper_scroll.verticalScrollBar().maximum() > 0, (
        "upper region has no scroll room left to absorb a constrained "
        "window -- the deficit has nowhere to go but back onto the table"
    )
    assert tab.table.height() >= 175, (
        f"table collapsed to {tab.table.height()}px instead of the upper "
        "region's scrollbar absorbing the constrained window"
    )


def test_existing_tool_toggle_and_count_behavior_unchanged(tab):
    """The reparenting into upper_layout/upper_scroll must not change any
    existing widget identity, persistence, or behavior -- same object,
    same signal wiring, just a different container."""
    from tools.registry import ToolRegistry

    assert isinstance(tab.agent.registry, ToolRegistry)
    total = len(tab.agent.registry.list_tools())
    enabled = len(tab.agent.registry.list_enabled())
    assert total == 15
    assert enabled == 15  # all registered enabled by default
    assert f"({enabled}/{total} enabled" in tab.count_lbl.text()

    tab._disable_all()
    assert len(tab.agent.registry.list_enabled()) == 0
    tab._enable_all()
    assert len(tab.agent.registry.list_enabled()) == 15
