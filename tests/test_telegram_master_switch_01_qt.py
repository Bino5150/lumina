"""TELEGRAM-MASTER-SWITCH-01 -- blocking real-Qt guards.

Real LuminaWindow emergency stop and re-arm, the real Settings
Communications switch (real mouse clicks), and app launch with the switch
ON. Missing Qt fails (never skips) when LUMINA_REQUIRE_QT=1, as in CI.
"""
import json
import os

import pytest

if os.environ.get("LUMINA_REQUIRE_QT") == "1":
    import PySide6  # noqa: F401
else:
    pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

import comms.telegram_access as access
import comms.telegram_bridge as telegram_bridge
import config as config_module
import core.persistence as persistence
from core import emergency_stop


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def hermetic(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(config_module, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config_module, "DB_PATH", str(tmp_path / "data" / "memory" / "lumina.db"))
    monkeypatch.setattr(config_module, "LLM_BACKEND", "llamacpp")
    monkeypatch.setattr(config_module, "LLM_BACKEND_URL", "http://127.0.0.1:8080/v1")
    monkeypatch.setattr(config_module, "TELEGRAM_OWNER_CHAT_ID", None, raising=False)
    import core.secrets as secrets_module
    monkeypatch.setattr(secrets_module, "SECRETS_PATH", str(tmp_path / "credentials.json"))
    access._reset_for_tests()
    emergency_stop._reset_for_tests()
    starts = []
    monkeypatch.setattr(telegram_bridge, "start_bridge", lambda *a, **k: starts.append(1) or (True, "started"))
    yield starts
    access._reset_for_tests()
    emergency_stop._reset_for_tests()


def _prefs():
    try:
        return json.load(open(persistence.PREFS_PATH))
    except FileNotFoundError:
        return {}


@pytest.fixture
def window(hermetic, monkeypatch):
    import ui.main_window as mw
    monkeypatch.setattr(mw.browser_manager, "close", lambda: None)
    from core.agent import LuminaAgent
    win = mw.LuminaWindow(LuminaAgent(owner=True, channel_id="master-switch-qt"))
    notices = []
    original = win.chat_widget.add_operator_message
    win.chat_widget.add_operator_message = lambda text: (notices.append(text), original(text))
    win._notices = notices
    yield win
    win.close()


# ── Emergency stop forces OFF; re-arm never turns it on ──────────────────

def test_emergency_stop_turns_remote_access_off_after_the_latch(window, monkeypatch):
    access.turn_on_by_owner()
    persistence.update({"some_other_setting": "written after startup"})
    order = []
    original = access.turn_off
    monkeypatch.setattr(access, "turn_off",
                        lambda reason: (order.append(("turn_off", emergency_stop.is_latched(), reason)),
                                        original(reason))[1])

    window._trigger_emergency_stop(source="gui_button")

    assert order == [("turn_off", True, "emergency_stop")]       # the latch came first
    prefs = _prefs()
    assert prefs[access.PREF_KEY] is False
    assert prefs[access.REASON_KEY] == "emergency_stop"
    assert prefs["some_other_setting"] == "written after startup"  # owned keys only
    assert access.remote_access_enabled() is False


def test_emergency_stop_with_a_failed_write_holds_off_for_the_session_and_says_so(window, monkeypatch):
    access.turn_on_by_owner()
    monkeypatch.setattr(persistence, "update", lambda mapping: False)

    window._trigger_emergency_stop(source="gui_button")

    assert access.remote_access_enabled() is False
    assert any("this session only" in n for n in window._notices)


def test_rearm_never_turns_remote_access_back_on(window, monkeypatch, hermetic):
    access.turn_on_by_owner()
    window._trigger_emergency_stop(source="gui_button")
    monkeypatch.setattr(window, "_emergency_rearm_ready", lambda: True)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)

    window._confirm_and_rearm()

    assert emergency_stop.is_latched() is False
    assert access.remote_access_enabled() is False
    assert access.off_reason() == "emergency_stop"
    assert hermetic == []                                          # nothing started listening
    assert any("Telegram remains off" in n for n in window._notices)


def test_launch_with_the_switch_on_starts_nothing_until_automatic_listening_exists(hermetic, monkeypatch):
    access.turn_on_by_owner()
    import ui.main_window as mw
    monkeypatch.setattr(mw.browser_manager, "close", lambda: None)
    from core.agent import LuminaAgent
    win = mw.LuminaWindow(LuminaAgent(owner=True, channel_id="master-switch-launch"))
    try:
        assert hermetic == []
    finally:
        win.close()


# ── The Settings switch (real clicks) ────────────────────────────────────

@pytest.fixture
def tab(hermetic, monkeypatch):
    from types import SimpleNamespace
    from ui.main_window import COLORS
    from ui.settings.communications_tab import CommunicationsTab
    stops = []
    monkeypatch.setattr(telegram_bridge, "is_running", lambda: False)
    monkeypatch.setattr(telegram_bridge, "stop_bridge", lambda: stops.append(1) or (True, "Bridge stopped."))
    widget = CommunicationsTab(SimpleNamespace(), COLORS)
    widget.show()
    widget._stops = stops
    return widget


def _click(widget):
    QTest.mouseClick(widget, Qt.LeftButton)
    QApplication.processEvents()


def test_flipping_the_switch_on_persists_and_starts_the_listener(tab, hermetic):
    """Was "...and starts nothing" (the accepted blackout). The blackout
    ended with TELEGRAM-OWNER-INBOUND-WAKE-01 (D-A): flipping ON starts
    exactly one listener now; every start gate lives in start_bridge()."""
    assert "OFF" in tab.tg_bridge_status.text()

    _click(tab.tg_bridge_toggle)

    assert _prefs()[access.PREF_KEY] is True and access.on_since() is not None
    assert hermetic == [1]                                         # one start request
    status = tab.tg_bridge_status.text()
    assert status.startswith("Remote access: ON") and "arrives with the next update" not in status


def test_flipping_it_off_persists_the_owner_reason_and_stops_a_running_listener(tab, monkeypatch):
    access.turn_on_by_owner()
    monkeypatch.setattr(telegram_bridge, "is_running", lambda: True)
    tab._refresh_telegram_bridge_status()

    _click(tab.tg_bridge_toggle)

    assert access.remote_access_enabled() is False and access.off_reason() == "owner"
    assert tab._stops == [1]


def test_the_switch_shows_when_the_emergency_stop_turned_it_off(tab):
    access.turn_on_by_owner()
    access.turn_off("emergency_stop")
    tab._refresh_telegram_bridge_status()
    assert "emergency stop" in tab.tg_bridge_status.text().lower()


def test_the_switch_state_survives_a_relaunch_of_the_tab(tab, hermetic, monkeypatch):
    _click(tab.tg_bridge_toggle)                                   # ON
    access._reset_for_tests()                                      # relaunch
    from types import SimpleNamespace
    from ui.main_window import COLORS
    from ui.settings.communications_tab import CommunicationsTab
    again = CommunicationsTab(SimpleNamespace(), COLORS)
    assert "ON" in again.tg_bridge_status.text()


def test_ci_runs_this_file_as_a_blocking_qt_guard():
    workflow = open(os.path.join(os.path.dirname(__file__), "..", ".github", "workflows", "tests.yml")).read()
    qt_job = workflow[workflow.index("qt-security-guards:"):]
    assert "tests/test_telegram_master_switch_01_qt.py" in qt_job
