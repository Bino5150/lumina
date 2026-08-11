"""ui/settings/general_tab.py -- MB-11 Step 6: CONTEXT_COMPACTION_ENABLED
checkbox + CONTEXT_COMPACTION_BATCH_TOKENS spinbox in the CONTEXT WINDOW
section.

Genuinely PySide6-dependent (constructs a real QWidget) -- skipped, not
failed, in CI the same way as the other ui/*_smoke.py and
test_settings_subagent_toggles.py tests (PySide6 deliberately never
installed there).

Unlike ToolsTab's subagent toggles (which live-apply on every click),
GeneralTab's CONTEXT WINDOW section follows a "Save All Settings" button
model -- same as Max Context Tokens / Max Tool Iterations / Response
Tokens / Dream Sweep Enabled in that same section. These tests follow that
save-button-driven flow rather than asserting on bare toggle clicks.
"""
import os
import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import config
from core import persistence


@pytest.fixture
def isolated_prefs(tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(config, "CONTEXT_COMPACTION_ENABLED", False)
    monkeypatch.setattr(config, "CONTEXT_COMPACTION_BATCH_TOKENS", 1400)


def _make_tab():
    from PySide6.QtWidgets import QApplication
    from core.agent import LuminaAgent
    from ui.main_window import COLORS
    from ui.settings.general_tab import GeneralTab

    QApplication.instance() or QApplication([])
    agent = LuminaAgent(owner=True, channel_id="settings-compaction-toggle-test")
    return GeneralTab(agent, COLORS)


@pytest.fixture
def tab(isolated_prefs):
    return _make_tab()


def test_initial_state_reflects_config_defaults(tab):
    assert tab.compaction_enabled_cb.isChecked() is False
    assert tab.compaction_batch_spin.value() == 1400
    # Spinbox starts disabled -- checkbox is unchecked at construction.
    assert tab.compaction_batch_spin.isEnabled() is False


def test_checking_the_box_enables_the_batch_spinbox(tab):
    tab.compaction_enabled_cb.setChecked(True)

    assert tab.compaction_batch_spin.isEnabled() is True


def test_unchecking_disables_the_batch_spinbox_again(tab):
    tab.compaction_enabled_cb.setChecked(True)
    tab.compaction_enabled_cb.setChecked(False)

    assert tab.compaction_batch_spin.isEnabled() is False


def test_toggle_alone_does_not_persist_without_save(tab):
    """Matches the established CONTEXT WINDOW / Dream Sweep pattern in this
    tab: flipping the checkbox only updates widget state (the enabled/
    disabled spinbox) until 'Save All Settings' is actually clicked --
    unlike ToolsTab's subagent toggles, which live-apply immediately."""
    tab.compaction_enabled_cb.setChecked(True)

    assert config.CONTEXT_COMPACTION_ENABLED is False


def test_save_persists_enabled_flag_and_batch_tokens(tab):
    tab.compaction_enabled_cb.setChecked(True)
    tab.compaction_batch_spin.setValue(2500)

    tab._save()

    assert config.CONTEXT_COMPACTION_ENABLED is True
    assert config.CONTEXT_COMPACTION_BATCH_TOKENS == 2500
    saved = persistence.load()
    assert saved["context_compaction_enabled"] is True
    assert saved["context_compaction_batch_tokens"] == 2500


def test_save_persists_disabled_state_too(tab):
    tab.compaction_enabled_cb.setChecked(False)
    tab.compaction_batch_spin.setValue(900)

    tab._save()

    assert config.CONTEXT_COMPACTION_ENABLED is False
    saved = persistence.load()
    assert saved["context_compaction_enabled"] is False
    assert saved["context_compaction_batch_tokens"] == 900


def test_other_context_window_fields_unaffected_by_save(tab):
    """Regression guard: adding the compaction fields to _save() must not
    have disturbed the existing Max Context Tokens / Max Tool Iterations /
    Response Tokens writes in the same method."""
    tab.iter_spin.setValue(33)
    tab.resp_spin.setValue(2048)

    tab._save()

    assert config.MAX_TOOL_ITERATIONS == 33
    assert config.RESPONSE_RESERVE_TOKENS == 2048
    saved = persistence.load()
    assert saved["max_tool_iterations"] == 33
    assert saved["response_reserve_tokens"] == 2048


def test_ui_reflects_prefs_stored_override_not_just_shipped_default(isolated_prefs, monkeypatch):
    """MB-24-class gotcha check (same as test_settings_subagent_toggles.py):
    confirm the widget shows whatever's actually active -- a prefs-stored
    override -- not just config.py's bare default, on load."""
    monkeypatch.setattr(config, "CONTEXT_COMPACTION_ENABLED", True)
    monkeypatch.setattr(config, "CONTEXT_COMPACTION_BATCH_TOKENS", 3300)

    tab = _make_tab()

    assert tab.compaction_enabled_cb.isChecked() is True
    assert tab.compaction_batch_spin.value() == 3300
    assert tab.compaction_batch_spin.isEnabled() is True
