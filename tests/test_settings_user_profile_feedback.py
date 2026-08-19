"""Patch 3A.3B Part D / R12 -- proves UserProfileTab's Save Profile button
gives truthful semantic feedback instead of silently discarding
persistence.save()'s return value, and that a persistence failure's
message doesn't imply the already-mutated live USER_NAME was rolled back.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication
from types import SimpleNamespace

import config
from core import persistence


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def isolated_prefs(tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(config, "USER_NAME", "Bino", raising=False)


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
def tab(qapp, isolated_prefs, monkeypatch):
    from ui.main_window import COLORS
    from ui.settings._widgets import ButtonFeedback
    from ui.settings.user_profile_tab import UserProfileTab

    monkeypatch.setattr(ButtonFeedback, "HOLD_MS", 30)
    agent = SimpleNamespace()
    return UserProfileTab(agent, COLORS)


def test_r12_successful_persistence_shows_saved(tab):
    tab.user_name.setText("Jason")
    tab.human_bio.setPlainText("Some bio text.")

    tab._save()

    assert tab.save_btn.text() == "✓ Saved"
    assert config.USER_NAME == "Jason"
    saved = persistence.load()
    assert saved["user_name"] == "Jason"
    assert saved["human_bio"] == "Some bio text."
    assert _pump_until(lambda: tab.save_btn.text() == "Save Profile")


def test_r12_failed_persistence_shows_truthful_failure(tab, monkeypatch):
    monkeypatch.setattr(persistence, "save", lambda prefs: False)
    tab.user_name.setText("Renamed")
    prior = config.USER_NAME

    tab._save()

    # USER_NAME really was mutated live before the failed write -- the
    # message must not imply a rollback that never happened.
    assert config.USER_NAME == "Renamed"
    assert config.USER_NAME != prior
    assert tab.save_btn.text() == "✗ Failed"
    msg = tab.save_status_lbl.text()
    assert "not saved" in msg.lower()
    assert "rolled back" not in msg.lower() and "reverted" not in msg.lower()
    # Failure persists -- does not auto-revert like a clean success.
    assert not _pump_until(lambda: tab.save_btn.text() != "✗ Failed", timeout=0.15)


def test_autosave_bio_handlers_unaffected_by_save_feedback(tab):
    """Regression guard: the autosave textChanged handlers must stay silent
    -- 3A.3B explicitly does not add feedback animations to them."""
    tab.human_bio.setPlainText("triggers autosave")

    assert persistence.load()["human_bio"] == "triggers autosave"
    assert tab.save_btn.text() == "Save Profile"
