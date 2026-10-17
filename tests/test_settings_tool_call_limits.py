"""GATLING-TRIGGER-GUARD-01 -- Settings -> General: "Max Agent Rounds" (the
renamed round count) and the three tool execution budgets.

Reuses test_settings_compaction_toggle.py's hermetic fixtures (isolated prefs
and secrets, every config global _save() can mutate snapshotted for teardown).
"""
import pytest

pytest.importorskip("PySide6")

import config
from core import persistence
from test_settings_compaction_toggle import isolated_prefs, tab  # noqa: F401  (fixtures)


def _label_texts(widget):
    from PySide6.QtWidgets import QLabel
    return {label.text() for label in widget.findChildren(QLabel)}


def test_round_count_is_labelled_as_rounds_not_a_tool_call_limit(tab):
    labels = _label_texts(tab)
    assert "Max Agent Rounds" in labels
    assert "Max Tool Iterations" not in labels
    assert "Not a tool-call limit" in tab.iter_spin.toolTip()


def test_budget_fields_show_the_active_values(tab):
    labels = _label_texts(tab)
    for text in ("Max Tool Calls per Response", "Max Tool Calls per Turn", "Max Turn Minutes"):
        assert text in labels
    assert tab.calls_per_response_spin.value() == config.MAX_TOOL_CALLS_PER_RESPONSE
    assert tab.calls_per_turn_spin.value() == config.MAX_TOOL_CALLS_PER_TURN
    assert tab.turn_minutes_spin.value() == config.MAX_TURN_MINUTES


def test_save_applies_and_persists_every_budget(tab):
    tab.calls_per_response_spin.setValue(64)
    tab.calls_per_turn_spin.setValue(300)
    tab.turn_minutes_spin.setValue(45)
    tab.iter_spin.setValue(40)

    tab._save()

    assert (config.MAX_TOOL_CALLS_PER_RESPONSE, config.MAX_TOOL_CALLS_PER_TURN,
            config.MAX_TURN_MINUTES, config.MAX_TOOL_ITERATIONS) == (64, 300, 45, 40)
    saved = persistence.load()
    assert saved["max_tool_calls_per_response"] == 64
    assert saved["max_tool_calls_per_turn"] == 300
    assert saved["max_turn_minutes"] == 45
    assert saved["max_tool_iterations"] == 40


def test_budget_fields_cannot_be_set_to_zero(tab):
    for spin in (tab.calls_per_response_spin, tab.calls_per_turn_spin, tab.turn_minutes_spin):
        spin.setValue(0)
        assert spin.value() == 1
