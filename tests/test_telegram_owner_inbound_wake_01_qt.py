"""TELEGRAM-OWNER-INBOUND-WAKE-01 -- blocking real-Qt guards.

The real LuminaWindow origin queue and closeEvent, driven by the real bridge
handler on its own thread and event loop, and the real Settings
Communications switch (real mouse clicks). Only the network and Prime's
model turn are faked. Missing Qt fails (never skips) when
LUMINA_REQUIRE_QT=1, as in CI.

  * C-1: a routed reply queued behind a busy Prime is re-checked at final
    admission; OFF, an E-stop (even re-armed) or OFF->ON in between means it
    never runs.
  * C-4: closing the window asks the listener to stop before the origin
    runtime goes away, and an in-flight routed reply gets the closing
    refusal, never a headless turn.
  * D-A: flipping the switch ON starts exactly one listener; the status line
    is truthful.
"""
import asyncio
import json
import os
import threading
import time
import types

import pytest

if os.environ.get("LUMINA_REQUIRE_QT") == "1":
    import PySide6  # noqa: F401
else:
    pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import comms.telegram_access as access
import comms.telegram_bridge as telegram_bridge
import config as config_module
import core.persistence as persistence
import tools.telegram_send as telegram_send
from comms import telegram_origin_routing as routing
from core import emergency_stop, idempotency

OWNER = "42"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(idempotency, "LEDGER_PATH", str(tmp_path / "ledger.db"))
    monkeypatch.setenv("LUMINA_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(config_module, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config_module, "DB_PATH", str(tmp_path / "data" / "memory" / "lumina.db"))
    monkeypatch.setattr(config_module, "LLM_BACKEND", "llamacpp")
    monkeypatch.setattr(config_module, "LLM_BACKEND_URL", "http://127.0.0.1:8080/v1")
    monkeypatch.setattr(config_module, "TELEGRAM_OWNER_CHAT_ID", None, raising=False)
    import core.secrets as secrets_module
    monkeypatch.setattr(secrets_module, "SECRETS_PATH", str(tmp_path / "credentials.json"))
    access._reset_for_tests()
    emergency_stop._reset_for_tests()
    routing._reset_for_tests()
    getattr(telegram_bridge, "_reset_for_tests", lambda: None)()


def _reset_all():
    access._reset_for_tests()
    emergency_stop._reset_for_tests()
    routing._reset_for_tests()
    getattr(telegram_bridge, "_reset_for_tests", lambda: None)()


class _Phone:
    def __init__(self):
        self.posts = []
        self.next_id = 7001

    def post(self, url, data=None, files=None, timeout=None):
        message_id = self.next_id
        self.next_id += 1
        self.posts.append({"method": url.rsplit("/", 1)[-1], "message_id": message_id})

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"ok": True, "result": {"message_id": message_id}}

        return _Resp()


class _Busy:
    def isRunning(self):
        return True


@pytest.fixture
def prime(qapp, tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    assert access.turn_on_by_owner() is True
    import ui.main_window as mw
    monkeypatch.setattr(mw.browser_manager, "close", lambda: None)
    phone = _Phone()
    monkeypatch.setattr(telegram_send.requests, "post", phone.post)
    monkeypatch.setattr(telegram_send, "get_secret", lambda key: "unused")
    monkeypatch.setattr(telegram_send, "_owner_chat_id", lambda: OWNER)
    monkeypatch.setattr(telegram_send, "check", lambda request_id: None)
    monkeypatch.setattr(telegram_send, "record", lambda request_id, result: None)
    # A route is recorded only while a listener runs (TELEGRAM-MASTER-SWITCH-01).
    monkeypatch.setattr(telegram_send, "_listener_running", lambda: True)
    monkeypatch.setattr(telegram_bridge, "_owner_chat_id", lambda: OWNER)
    headless = []
    monkeypatch.setattr(telegram_bridge, "run_headless_turn",
                        lambda **kw: headless.append(kw) or {"success": True, "response": "headless"})

    from core.agent import LuminaAgent
    from tools.memory import create_chat, init_chat_db
    init_chat_db()
    chat_id = create_chat("remote control")
    win = mw.LuminaWindow(LuminaAgent(owner=True, channel_id="prime-desktop"))
    win._load_chat(chat_id, persist_as_last=False)
    monkeypatch.setattr(mw, "list_chats", lambda: [{"id": chat_id}])
    turns = []
    win.agent.chat = lambda user_input, **kw: turns.append(user_input) or "prime answer"
    win._phone, win._turns, win._chat, win._headless = phone, turns, chat_id, headless
    yield win
    win.worker = None
    win.close()
    parked = telegram_bridge._thread
    if parked is not None and parked.is_alive():
        telegram_bridge.request_stop_bridge()
        parked.join(timeout=5)
    _reset_all()


def _prime_messaged_phone(win):
    with routing.origin_scope(win._telegram_route_token, win._chat):
        telegram_send.send_telegram_message("Printer 2 failed. Reply here.")
    return win._phone.posts[-1]["message_id"]


def _send_in_background(text, *, reply_to=None, message_id=9001):
    replies = []

    async def _reply(value):
        replies.append(value)
        return types.SimpleNamespace(message_id=6001)

    message = types.SimpleNamespace(
        text=text, message_id=message_id, reply_text=_reply,
        date=None,
        reply_to_message=types.SimpleNamespace(message_id=reply_to) if reply_to is not None else None,
    )
    update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=int(OWNER)),
                                   message=message, edited_message=None)
    errors = []

    def _run():
        try:
            asyncio.run(telegram_bridge.handle_message(update, context=None))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=_run)
    thread.start()
    return thread, replies, errors


def _pump_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _finish(thread, errors, timeout=10.0):
    assert _pump_until(lambda: not thread.is_alive(), timeout), "bridge handler never finished"
    thread.join(timeout=1)
    assert not errors, errors


def _revoke(how):
    if how == "off":
        access.turn_off("owner")
    elif how == "estop_latched_then_rearmed":
        emergency_stop.latch(source="test", reason="test")
        assert emergency_stop.rearm_local() is True
    elif how == "off_then_on":
        access.turn_off("owner")
        time.sleep(0.01)
        assert access.turn_on_by_owner() is True


# ── C-1: final admission at the real origin queue ──────────────────────────

def test_a_queued_routed_reply_runs_when_authority_is_unchanged(prime):
    anchor = _prime_messaged_phone(prime)
    prime.worker = _Busy()
    thread, replies, errors = _send_in_background("cancel the print", reply_to=anchor)
    assert _pump_until(lambda: len(prime._telegram_origin_queue) == 1)
    prime.worker = None
    prime._drain_telegram_origin_queue()
    _finish(thread, errors)
    assert prime._turns == ["cancel the print"]
    assert replies == ["prime answer"] and prime._headless == []


@pytest.mark.parametrize("how", ["off", "estop_latched_then_rearmed", "off_then_on"])
def test_a_queued_routed_reply_is_refused_at_final_admission_after_a_revocation(prime, how):
    anchor = _prime_messaged_phone(prime)
    prime.worker = _Busy()
    thread, replies, errors = _send_in_background("cancel the print", reply_to=anchor)
    assert _pump_until(lambda: len(prime._telegram_origin_queue) == 1)

    _revoke(how)
    prime.worker = None
    prime._drain_telegram_origin_queue()
    _finish(thread, errors)

    assert prime._turns == []                  # never reached Prime's agent
    assert prime._headless == []               # and never fell back to headless
    assert replies == []                       # revocation is silent
    assert prime._telegram_active_dispatch is None


# ── C-4: closeEvent ordering and no headless after close ───────────────────

@pytest.fixture
def live_listener(monkeypatch):
    """A real listener thread (parked), so begin_shutdown has something to close."""
    monkeypatch.setattr(telegram_bridge, "get_secret", lambda key: "12345678" + ":" + "Q" * 35)

    def _park(stop_event, token):
        stop_event.wait(10)
        return True

    monkeypatch.setattr(telegram_bridge, "_run_bridge", _park)
    assert telegram_bridge.start_bridge(reason="startup")[0] is True


def test_close_asks_the_listener_to_stop_before_the_origin_runtime_goes_away(prime, live_listener, monkeypatch):
    order = []
    real_begin, real_unregister = telegram_bridge.begin_shutdown, routing.unregister_runtime
    monkeypatch.setattr(telegram_bridge, "begin_shutdown", lambda: order.append("begin_shutdown") or real_begin())
    monkeypatch.setattr(routing, "unregister_runtime",
                        lambda token: order.append("unregister_runtime") or real_unregister(token))
    prime.close()
    assert order[:2] == ["begin_shutdown", "unregister_runtime"]
    assert telegram_bridge.is_closing() is True


def test_an_in_flight_routed_reply_gets_the_closing_refusal_and_never_runs_headless(prime, live_listener):
    from comms import telegram_ingress_policy as policy
    anchor = _prime_messaged_phone(prime)
    prime.worker = _Busy()
    thread, replies, errors = _send_in_background("cancel the print", reply_to=anchor)
    assert _pump_until(lambda: len(prime._telegram_origin_queue) == 1)

    prime.close()
    _finish(thread, errors)

    assert replies == [policy.CLOSING_REFUSAL]
    assert prime._turns == [] and prime._headless == []


def test_a_plain_message_after_close_never_runs_headless(prime, live_listener):
    from comms import telegram_ingress_policy as policy
    _prime_messaged_phone(prime)
    prime.close()
    thread, replies, errors = _send_in_background("are you still there?", message_id=9100)
    _finish(thread, errors)
    assert replies == [policy.CLOSING_REFUSAL]
    assert prime._headless == []


# ── D-A: the Settings switch starts and stops the listener ─────────────────

@pytest.fixture
def tab(qapp, tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from ui.main_window import COLORS
    from ui.settings.communications_tab import CommunicationsTab
    calls = types.SimpleNamespace(starts=[], stops=[], running=False)
    monkeypatch.setattr(telegram_bridge, "start_bridge",
                        lambda reason="manual": calls.starts.append(reason) or (True, "Bridge started."))
    monkeypatch.setattr(telegram_bridge, "stop_bridge", lambda: calls.stops.append(1) or (True, "Bridge stopped."))
    monkeypatch.setattr(telegram_bridge, "is_running", lambda: calls.running)
    widget = CommunicationsTab(types.SimpleNamespace(), COLORS)
    widget.show()
    widget._calls = calls
    yield widget
    widget.close()
    _reset_all()


def _click(widget):
    QTest.mouseClick(widget, Qt.LeftButton)
    QApplication.processEvents()


def test_flipping_on_starts_exactly_one_listener_and_flipping_off_stops_it(tab):
    _click(tab.tg_bridge_toggle)
    assert access.remote_access_enabled() is True
    assert tab._calls.starts == ["switch_on"]

    tab._calls.running = True
    _click(tab.tg_bridge_toggle)
    assert access.remote_access_enabled() is False
    assert tab._calls.stops == [1]
    assert tab._calls.starts == ["switch_on"]


def test_a_failed_on_write_starts_nothing(tab, monkeypatch):
    monkeypatch.setattr(persistence, "update", lambda mapping: False)
    _click(tab.tg_bridge_toggle)
    assert tab._calls.starts == []


@pytest.mark.parametrize("state, running, expected", [
    ("listening", True, "Listening"),
    ("starting", True, "Starting"),
    ("retrying", True, "no network, retrying"),
    ("token_rejected", False, "Bot token rejected"),
    ("token_malformed", False, "malformed"),
    ("conflict", False, "Another Lumina is using this bot"),
    ("lock_held", False, "Another Lumina is using this bot"),
    ("not_configured", False, "Not configured"),
    ("error", False, "Stopped after an error"),
])
def test_the_status_line_is_truthful(tab, monkeypatch, state, running, expected):
    assert access.turn_on_by_owner() is True
    tab._calls.running = running
    monkeypatch.setattr(telegram_bridge, "health", lambda: {"state": state})
    tab._refresh_telegram_bridge_status()
    status = tab.tg_bridge_status.text()
    assert status.startswith("Remote access: ON") and expected in status
    assert "arrives with the next update" not in status


def test_saving_the_configuration_while_on_starts_a_listener(tab):
    assert access.turn_on_by_owner() is True
    tab.tg_chat_id.setText(OWNER)
    tab._save_telegram()
    assert tab._calls.starts == ["config_saved"]


def test_saving_the_configuration_while_off_starts_nothing(tab):
    tab.tg_chat_id.setText(OWNER)
    tab._save_telegram()
    assert tab._calls.starts == []


def test_the_status_line_follows_the_listener_without_a_click(tab, monkeypatch):
    assert access.turn_on_by_owner() is True
    state = {"state": "starting"}
    tab._calls.running = True
    monkeypatch.setattr(telegram_bridge, "health", lambda: dict(state))
    tab._refresh_telegram_bridge_status()
    assert "Starting" in tab.tg_bridge_status.text()

    state["state"] = "listening"                     # the listener came up on its own thread
    assert _pump_until(lambda: "Listening" in tab.tg_bridge_status.text(), timeout=5.0)

    state["state"] = "token_rejected"                # ...and later stopped on a fault
    tab._calls.running = False
    assert _pump_until(lambda: "Bot token rejected" in tab.tg_bridge_status.text(), timeout=5.0)
