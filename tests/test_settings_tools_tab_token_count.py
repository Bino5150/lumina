"""ui/settings ToolsTab -- MB-10 follow-up: live schema_token_estimate()
readout next to the enabled/total count.

Context: MB-10 (dynamic per-turn tool relevance filtering) was scoped down
after measuring the real registry live (68 tools, ~6.4k schema tokens --
already lean per-tool, close to TOOL_BUDGET_TOKENS, not blowing past it once
tool profiles and context compaction are accounted for). The one gap that
survives that scope-down: Toolmaker lets Lumina register her own tools at
runtime, so the schema-token total can creep upward over a long-running
install with nothing surfacing it except a console-only [TOOLS] print in
core/agent.py that a desktop user is unlikely to ever see. This puts the
same number in Settings, next to the tool count that already lived there,
styled red past config.TOOL_BUDGET_TOKENS -- same threshold the console
warning already uses.

Uses a real tools.registry.ToolRegistry with a small number of
hand-registered synthetic tools of known schema size (not a full
LuminaAgent's 68-tool load) so the token math in each assertion is exact and
doesn't drift as real tools get added/removed elsewhere in the codebase.

Genuinely PySide6-dependent (constructs a real QWidget) -- same
importorskip guard as every other tools_tab test in this suite, skipped
(not failed) in CI, which deliberately never installs PySide6.
"""
import os
import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace
import config
from core import persistence
from tools.registry import ToolRegistry


def _register_n_tools(registry: ToolRegistry, n: int, param_count: int = 0):
    """Register n tools with identical, deterministic-size schemas so the
    resulting schema_token_estimate() is exact and reproducible."""
    props = {f"p{i}": {"type": "string", "description": "x"} for i in range(param_count)}
    for i in range(n):
        registry.register(
            name=f"tool_{i}",
            fn=lambda: "ok",
            description="A test tool.",
            parameters={"type": "object", "properties": props, "required": []},
        )


@pytest.fixture
def isolated_prefs(tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(config, "TOOL_BUDGET_TOKENS", 500)


@pytest.fixture
def tab(isolated_prefs):
    from PySide6.QtWidgets import QApplication
    from ui.main_window import COLORS
    from ui.settings import ToolsTab

    QApplication.instance() or QApplication([])

    registry = ToolRegistry()
    _register_n_tools(registry, 30, param_count=3)  # comfortably over the 500-token test budget

    fake_agent = SimpleNamespace(registry=registry, _subagent_depth=0,
                                  _background_task_ids=set())
    return ToolsTab(fake_agent, COLORS)


def test_label_shows_enabled_total_and_token_count(tab):
    text = tab.count_lbl.text()
    assert "30/30 enabled" in text
    assert "schema tokens" in text
    # The number in the label should match the registry's own estimate exactly.
    tokens = tab.agent.registry.schema_token_estimate()
    assert f"{tokens:,}" in text


def test_over_budget_styles_danger_color_with_tooltip(tab):
    assert tab.agent.registry.schema_token_estimate() > config.TOOL_BUDGET_TOKENS
    assert tab.c["danger"] in tab.count_lbl.styleSheet()
    assert "font-weight:bold" in tab.count_lbl.styleSheet()
    assert str(config.TOOL_BUDGET_TOKENS) in tab.count_lbl.toolTip()


def test_disabling_tools_drops_below_budget_and_clears_danger_style(tab):
    # Disable all but 2 tools -- well under the 500-token test budget.
    names = tab.agent.registry.all_tool_names()
    for name in names[2:]:
        tab.agent.registry.disable(name)
    tab._update_count()

    assert tab.agent.registry.schema_token_estimate() <= config.TOOL_BUDGET_TOKENS
    assert "2/30 enabled" in tab.count_lbl.text()
    assert tab.c["danger"] not in tab.count_lbl.styleSheet()
    assert tab.count_lbl.toolTip() == ""


def test_individual_checkbox_toggle_refreshes_token_count(tab):
    """_toggle() (the per-row checkbox handler) must refresh the label too --
    not just _load_tools()/profile switches. Regression guard against only
    wiring the readout into one of the two update paths."""
    names = tab.agent.registry.all_tool_names()
    before = tab.count_lbl.text()

    tab._toggle(names[0], 0)  # Qt.Unchecked == 0 -- disable via the same path a real click uses

    after = tab.count_lbl.text()
    assert after != before
    assert "29/30 enabled" in after


def test_enable_all_disable_all_refresh_token_count(tab):
    tab._disable_all()
    assert "0/30 enabled" in tab.count_lbl.text()
    assert "~0 schema tokens" in tab.count_lbl.text()
    assert tab.c["danger"] not in tab.count_lbl.styleSheet()

    tab._enable_all()
    assert "30/30 enabled" in tab.count_lbl.text()
    assert tab.c["danger"] in tab.count_lbl.styleSheet()
