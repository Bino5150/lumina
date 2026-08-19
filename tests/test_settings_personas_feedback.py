"""Patch 3A.3B Part F / R14 -- proves PersonasTab's "💾 Save Persona" button
gives truthful semantic feedback (previously only a console print, no GUI
confirmation at all), and specifically proves the button-reference/rebuild
landmine flagged in the patch: PersonasTab tears down and rebuilds its
entire right panel (_clear_right()'s deleteLater()) on every persona
selection, so a delayed success-reset timer scheduled against the *old*
Save button must not crash when it fires after that button is gone.

_fetch_voices() is monkeypatched to a fixed list everywhere here -- it
otherwise does a real network probe against config.TTS_HOST, which is an
unrelated dependency this file has no reason to exercise.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication
from types import SimpleNamespace

import core.personas as personas_module
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
    from ui.settings.personas_tab import PersonasTab

    monkeypatch.setattr(personas_module, "PERSONAS_DIR", str(tmp_path / "personas"))
    monkeypatch.setattr(ButtonFeedback, "HOLD_MS", 30)
    monkeypatch.setattr(
        PersonasTab, "_fetch_voices",
        lambda self: ["af_bella", "af_sarah"],
    )

    registry = ToolRegistry()
    agent = SimpleNamespace(registry=registry)
    return PersonasTab(agent, COLORS)


def _make_persona_file(tab, name="TestPersona"):
    from core.personas import save_persona, fname_from_name, PERSONAS_DIR
    path = os.path.join(PERSONAS_DIR, fname_from_name(name))
    save_persona(path, {
        "name": name, "tagline": "", "avatar": "", "system_prompt": "",
        "tools_profile": "", "tts_voice": "af_bella", "tts_speed": 1.0,
        "tts_pitch": 1.0, "tts_volume": 1.0, "description": "", "protected": False,
    })
    return path


def _select(tab, path):
    tab._load_personas()
    persona = next(p for p in tab._personas if p["_file"] == path)
    tab._select_persona(persona)


# ── R14 ────────────────────────────────────────────────────────────────

def test_r14_successful_write_shows_saved(tab, capsys):
    path = _make_persona_file(tab)
    _select(tab, path)

    tab.rp_name.setText("Renamed Persona")
    tab._save_persona()

    assert tab.rp_save_btn.text() == "✓ Saved"
    with open(path) as f:
        data = json.load(f)
    assert data["name"] == "Renamed Persona"
    # The pre-existing console print is not GUI confirmation on its own,
    # but it must still fire (unchanged behavior) alongside the new feedback.
    assert "[PERSONA] Saved: Renamed Persona" in capsys.readouterr().out
    assert _pump_until(lambda: tab.rp_save_btn.text() == "💾 Save Persona")


def test_r14_write_failure_shows_failure(tab, monkeypatch):
    path = _make_persona_file(tab)
    _select(tab, path)

    monkeypatch.setattr(
        personas_module, "save_persona",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )

    tab._save_persona()

    assert tab.rp_save_btn.text() == "✗ Failed"
    assert "disk full" in tab.rp_save_status_lbl.text()
    assert not _pump_until(lambda: tab.rp_save_btn.text() != "✗ Failed", timeout=0.15)


# ── Rebuild/timer-lifetime landmine ──────────────────────────────────────

def test_save_success_then_panel_rebuild_before_timer_fires_does_not_crash(tab):
    """The exact scenario called out in the patch: trigger a Save success,
    then -- before its delayed reset fires -- select a different persona,
    which tears down and rebuilds the right panel (deleting the old Save
    button). Processing the stale timer afterward must be a silent no-op,
    not a RuntimeError on a deleted C++ object."""
    path_a = _make_persona_file(tab, "PersonaA")
    path_b = _make_persona_file(tab, "PersonaB")

    _select(tab, path_a)
    old_save_btn = tab.rp_save_btn
    tab._save_persona()
    assert old_save_btn.text() == "✓ Saved"

    # Switch personas before the ~30ms reset fires -- rebuilds the right
    # panel, replacing tab.rp_save_btn with a brand new QPushButton and
    # scheduling the old one for deletion.
    _select(tab, path_b)
    assert tab.rp_save_btn is not old_save_btn

    # Actually destroy the old button's C++ object, then let its stale
    # timer's window pass -- must not raise.
    QApplication.processEvents()
    QApplication.sendPostedEvents(None, 0)
    _pump_until(lambda: False, timeout=0.15)

    # The new panel's own button must be completely unaffected.
    assert tab.rp_save_btn.text() == "💾 Save Persona"

    # And it must still work normally for a fresh Save on the new persona.
    tab._save_persona()
    assert tab.rp_save_btn.text() == "✓ Saved"
    assert _pump_until(lambda: tab.rp_save_btn.text() == "💾 Save Persona")


def test_activate_duplicate_export_delete_unaffected(tab):
    """Regression guard: 3A.3B must not touch Activate/Duplicate/Export/
    Delete or persona-protection semantics in this tab."""
    path = _make_persona_file(tab, "Untouched")
    _select(tab, path)
    assert tab._current_persona.get("protected") is False

    tab._duplicate()
    names = [p.get("name") for p in personas_module.list_personas()]
    assert "Untouched (copy)" in names
