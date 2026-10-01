"""
SUBSCRIPTION-PLAN-BACKENDS-01C -- the Settings "ChatGPT Plan — sign-in"
card, under real Qt.

Pins: the card is mounted in the General page but outside its backend
selector and Save; a READY session never adds the plan lane to the
selector; the card renders verified identity and the app-generated start
code, but no token or authorization URL, and claims nothing 01C has not earned (no model, no chat
readiness); every session call runs off the Qt main thread; app quit
cancels a pending browser sign-in.

tests.yml runs this file with LUMINA_REQUIRE_QT=1 (fail, never skip).
"""
import os
import threading
import time

import pytest

if os.environ.get("LUMINA_REQUIRE_QT") == "1":
    import PySide6  # noqa: F401 -- the Qt guard job must fail, never skip
else:
    pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from core.chatgpt_auth.session import (
    DisconnectOutcome, PendingSignIn, PlanPermission, ProfileView, ReauthRequired, SessionState,
    SessionStatus,
)

PLAN = "openai_chatgpt_plan"
from ui.main_window import COLORS  # noqa: E402 -- the real palette every tab is built with


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class StubManager:
    """Stands in for ChatGPTSessionManager: sanitized DTOs only."""

    def __init__(self, state=SessionState.DISCONNECTED, plan=PlanPermission.UNKNOWN):
        self.state = state
        self.plan = plan
        self.calls = []
        self.threads = []
        self.gate = None
        self.fail_with = None

    def _note(self, name):
        self.calls.append(name)
        self.threads.append(threading.current_thread() is threading.main_thread())

    def get_session_state(self, profile_id=None):
        self._note("get_session_state")
        pid = None if self.state is SessionState.DISCONNECTED else "p" * 32
        return SessionStatus(self.state, self.plan, profile_id=pid,
                             profile_label="ChatGPT account 1" if pid else None,
                             account_display="owner@example.test" if pid else None)

    def list_profiles(self):
        self._note("list_profiles")
        if self.state is SessionState.DISCONNECTED:
            return []
        return [ProfileView("p" * 32, "ChatGPT account 1", "owner@example.test", self.state, self.plan, True)]

    def begin_sign_in(self, **kw):
        self._note("begin_sign_in")
        self.state = SessionState.AUTHORIZING
        return PendingSignIn("a" * 32, "new_registration")

    def complete_sign_in(self, pending, timeout=None):
        self._note("complete_sign_in")
        if self.gate is not None:
            assert self.gate.wait(10)
        if self.fail_with is not None:
            self.state = SessionState.DISCONNECTED
            raise self.fail_with
        self.state, self.plan = SessionState.READY, PlanPermission.GRANTED
        return self.get_session_state()

    def cancel_sign_in(self):
        self.calls.append("cancel_sign_in")

    def disconnect(self, profile_id=None):
        self._note("disconnect")
        self.state = SessionState.DISCONNECTED
        return DisconnectOutcome(SessionState.DISCONNECTED, True)

    def select_profile(self, pid):
        self._note("select_profile")


def pump(qapp, card, until, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        qapp.processEvents()
        if until():
            return True
        time.sleep(0.01)
    return False


def visible_text(card):
    texts = [w.text() for w in card.findChildren(QLabel)]
    texts += [b.text() for b in card.findChildren(QPushButton) if not b.isHidden()]
    return "\n".join(texts)


def make_card(qapp, manager):
    from ui.settings.chatgpt_plan_card import ChatGPTPlanCard
    card = ChatGPTPlanCard(None, COLORS, manager=manager)
    card.show()
    assert pump(qapp, card, lambda: not card._busy)
    return card


def test_ready_session_renders_only_truthful_sanitized_text(qapp):
    manager = StubManager(SessionState.READY, PlanPermission.GRANTED)
    card = make_card(qapp, manager)
    text = visible_text(card)
    assert "Session: Connected to ChatGPT" in text
    assert "Plan permission: Enabled" in text
    assert "Connected account: owner@example.test" in text
    lowered = text.lower()
    for claim in ("model available", "ready to chat", "gpt ready", "unlimited", "free", "tools supported",
                  "vision", "reforge"):
        assert claim not in lowered, claim
    assert "not available in this version" in lowered       # says what 01C has NOT earned
    assert not card.disconnect_btn.isHidden()
    assert card.continue_btn.isHidden()
    assert all(not on_main for on_main in manager.threads)   # never on the Qt main thread


def test_sign_in_runs_off_the_main_thread_and_ui_stays_responsive(qapp):
    manager = StubManager()
    card = make_card(qapp, manager)
    assert not card.continue_btn.isHidden()
    manager.gate = threading.Event()
    card.continue_btn.click()
    assert pump(qapp, card, lambda: not card.cancel_btn.isHidden())   # AUTHORIZING shown while blocked
    assert "browser" in card.session_label.text().lower()
    manager.gate.set()
    assert pump(qapp, card, lambda: "Plan permission enabled" in card.message_label.text())
    assert all(not on_main for on_main in manager.threads)


def test_first_registration_requires_visible_owner_confirmation(qapp):
    class StagedManager(StubManager):
        def __init__(self):
            super().__init__()
            self.candidate = False

        def get_session_state(self, profile_id=None):
            self._note("get_session_state")
            return SessionStatus(self.state, self.plan,
                                 account_display="owner@example.test" if self.candidate else None)

        def begin_sign_in(self, **kw):
            self._note("begin_sign_in")
            self.state = SessionState.AUTHORIZING
            return PendingSignIn("a" * 32, "new_registration", "CODETEST42")

        def complete_sign_in(self, pending, timeout=None):
            self._note("complete_sign_in")
            assert self.gate.wait(10)
            self.candidate = True
            return self.get_session_state()

        def confirm_sign_in(self, pending):
            self._note("confirm_sign_in")
            self.state = SessionState.READY
            self.plan = PlanPermission.GRANTED
            return self.get_session_state()

    manager = StagedManager()
    manager.gate = threading.Event()
    card = make_card(qapp, manager)
    card.continue_btn.click()
    assert pump(qapp, card, lambda: "CODETEST42" in card.code_label.text())
    assert card.confirm_btn.isHidden()
    manager.gate.set()
    assert pump(qapp, card, lambda: not card.confirm_btn.isHidden())
    assert "Account to confirm: owner@example.test" in visible_text(card)
    card.confirm_btn.click()
    assert pump(qapp, card, lambda: card._status.state is SessionState.READY)
    assert "confirm_sign_in" in manager.calls


def test_sign_in_error_shows_only_the_category_message(qapp):
    manager = StubManager()
    manager.fail_with = ReauthRequired("refresh_token_reused")
    card = make_card(qapp, manager)
    card.continue_btn.click()
    assert pump(qapp, card, lambda: card.message_label.text() != "")
    assert card.message_label.text() == "Connection expired — reconnect required."
    assert "refresh_token_reused" not in visible_text(card)


def test_disconnect_and_quit_cancel(qapp):
    manager = StubManager(SessionState.READY, PlanPermission.GRANTED)
    card = make_card(qapp, manager)
    card.disconnect_btn.click()
    assert pump(qapp, card, lambda: card.message_label.text() == "Disconnected.")
    qapp.aboutToQuit.emit()
    assert "cancel_sign_in" in manager.calls


def test_card_is_mounted_in_general_outside_selector_and_save(qapp, monkeypatch, tmp_path):
    import config
    from core import persistence
    import core.secrets as secrets_module
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(secrets_module, "SECRETS_PATH", str(tmp_path / "credentials.json"))
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "data" / "memory" / "lumina.db"))
    monkeypatch.setattr(config, "LLM_BACKEND", "openai")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-" + "h" * 24)
    import ui.settings.chatgpt_plan_card as card_mod
    import ui.settings.panel as panel_module
    manager = StubManager(SessionState.READY, PlanPermission.GRANTED)
    monkeypatch.setattr(card_mod.ChatGPTPlanCard, "_mgr", lambda self: manager)

    from PySide6.QtWidgets import QWidget

    class _PlainTab(QWidget):
        def __init__(self, *args, **kwargs):
            super().__init__()

        def refresh_voices(self):
            pass
    from PySide6.QtCore import Signal

    class _SignalTab(_PlainTab):
        backend_changed = Signal(str)
    for name in ("UserProfileTab", "MemoryTab", "KnowledgeTab", "SkillsTab", "ToolsTab",
                 "CommunicationsTab", "PersonasTab", "ScheduledTasksTab", "AboutTab", "ComingSoonTab"):
        monkeypatch.setattr(panel_module, name, _PlainTab)
    monkeypatch.setattr(panel_module, "MultimodalTab", _SignalTab)
    from core.agent import LuminaAgent
    agent = LuminaAgent(owner=True, channel_id="01c-settings", backend="openai")
    panel = panel_module.SettingsPanel(agent, COLORS)
    general = panel.general_tab
    card = panel.chatgpt_plan_card
    assert general.isAncestorOf(card)
    assert pump(qapp, card, lambda: "Connected to ChatGPT" in card.session_label.text())
    items = [general.backend_combo.itemText(i) for i in range(general.backend_combo.count())]
    assert PLAN not in items and not any("chatgpt" in i.lower() for i in items)
    # General's Save never drives the session manager.
    before = list(manager.calls)
    general._save()
    assert [c for c in manager.calls[len(before):] if c not in ("get_session_state", "list_profiles")] == []
