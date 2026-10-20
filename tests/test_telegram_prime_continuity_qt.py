"""Telegram Prime continuity -- the owner's remote-control route, end to end.

Owner Telegram is Bino's authenticated remote-control channel into the SAME
Lumina Prime desktop session, not a separate Telegram agent. This drives the
real route with only the network faked:

  real telegram_bridge.handle_message (bridge thread, its own event loop)
    -> real comms.telegram_origin_routing (exact swipe-reply / sole recent)
    -> real LuminaWindow._relay_telegram_origin -> Qt signal -> origin queue
    -> real AgentWorker (QThread) -> Prime's agent.chat inside origin_scope
    -> Prime's own ToolRegistry -> real tools.telegram_send (requests faked)
    -> real _on_finished -> _complete_telegram_origin -> bridge reply

Prime's model turn itself is replaced by a recording stand-in that performs
the tool work a source-vet turn ends with (sending the report and a message
to the phone through Prime's registry). Everything around it is production.

Ordinary remote owner instructions keep full Prime authority; only text the
desktop would read as an operator slash command is refused
(OPERATOR-INGRESS-PROVENANCE-01). The slash-command case imports C0's module
lazily so this file also runs unchanged against the pre-C0 tree as a
baseline.

Missing Qt fails (never skips) when LUMINA_REQUIRE_QT=1.
"""
import asyncio
import os
import threading
import time
import types

import pytest

# TELEGRAM-MASTER-SWITCH-01: these tests model the owner having Telegram
# remote access ON (it defaults to OFF); the switch has its own tests.
pytestmark = pytest.mark.usefixtures("telegram_remote_access_on")

if os.environ.get("LUMINA_REQUIRE_QT") == "1":
    import PySide6  # noqa: F401
else:
    pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import comms.telegram_bridge as telegram_bridge
import config as config_module
import core.persistence as persistence
import tools.telegram_send as telegram_send
from comms import telegram_origin_routing as routing
from core import emergency_stop

OWNER = "42"
SOURCE_VET = ("Lumina, source-vet the latest changes to the Browser Companion "
              "and send me the findings.")
PRIME_FINAL = "Source-vet complete: 2 findings. Report sent to your phone."


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _Phone:
    """Fake Telegram Bot API: every outbound POST, with message ids."""

    def __init__(self):
        self.posts = []
        self.next_id = 7001

    def post(self, url, data=None, files=None, timeout=None):
        message_id = self.next_id
        self.next_id += 1
        self.posts.append({"method": url.rsplit("/", 1)[-1], "data": dict(data or {}),
                           "file": getattr((files or {}).get("document"), "name", None),
                           "message_id": message_id})
        phone = self

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"ok": True, "result": {"message_id": message_id}}

        return _Resp()


@pytest.fixture
def prime(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(config_module, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config_module, "DB_PATH", str(tmp_path / "data" / "memory" / "lumina.db"))
    monkeypatch.setattr(config_module, "LLM_BACKEND", "llamacpp")
    monkeypatch.setattr(config_module, "LLM_BACKEND_URL", "http://127.0.0.1:8080/v1")
    import core.secrets as secrets_module
    monkeypatch.setattr(secrets_module, "SECRETS_PATH", str(tmp_path / "credentials.json"))
    import ui.main_window as mw
    monkeypatch.setattr(mw.browser_manager, "close", lambda: None)
    emergency_stop._reset_for_tests()
    routing._reset_for_tests()

    phone = _Phone()
    monkeypatch.setattr(telegram_send.requests, "post", phone.post)
    monkeypatch.setattr(telegram_send, "get_secret", lambda key: "fake-token")
    monkeypatch.setattr(telegram_send, "_owner_chat_id", lambda: OWNER)
    monkeypatch.setattr(telegram_send, "check", lambda request_id: None)
    monkeypatch.setattr(telegram_send, "record", lambda request_id, result: None)
    monkeypatch.setattr(telegram_bridge, "is_running", lambda: True)
    monkeypatch.setattr(telegram_bridge, "_owner_chat_id", lambda: OWNER)
    monkeypatch.setattr(telegram_bridge, "run_headless_turn",
                        lambda **kw: pytest.fail("a routed owner message must reach Prime, not headless"))

    from core.agent import LuminaAgent
    from tools.memory import create_chat, init_chat_db
    init_chat_db()
    chat_id = create_chat("remote control")
    win = mw.LuminaWindow(LuminaAgent(owner=True, channel_id="prime-desktop"))
    win._load_chat(chat_id, persist_as_last=False)
    monkeypatch.setattr(mw, "list_chats", lambda: [{"id": chat_id}])

    report = tmp_path / "browser_companion_source_vet.md"
    report.write_text("# Findings\n1. ...\n2. ...\n")
    turns = []

    def prime_turn(user_input, **kwargs):
        """Stand-in for Prime's model turn: record what Prime received and
        where it ran, then do the tool work through Prime's own registry."""
        turns.append({"input": user_input, "kwargs": kwargs,
                      "origin": routing._origin.get(), "thread": threading.current_thread()})
        # AGENT-RUNAWAY-SUSPEND-01: like the real chat(), run the turn under
        # the token Telegram intake captured and the desktop carried here.
        from core import suspension, suspension_runtime
        token = kwargs["suspension_token"]
        assert type(token) is suspension.ExecutionToken and token.scope == suspension.OWNER_SCOPE
        with suspension_runtime.carrying(token):
            if "source-vet" in str(user_input):
                win.agent.registry.call("send_telegram_file", {"path": str(report), "caption": "Findings"})
                win.agent.registry.call("send_telegram_message", {"text": "Findings sent."})
        return PRIME_FINAL

    win.agent.chat = prime_turn
    win._phone, win._turns, win._chat, win._report = phone, turns, chat_id, report
    yield win
    win.close()
    routing._reset_for_tests()
    emergency_stop._reset_for_tests()


def _owner_sends(win, text, *, chat_id=OWNER, reply_to=None, message_id=9001, timeout=20.0):
    """Deliver one inbound Telegram update through the real bridge on its own
    thread and event loop (as python-telegram-bot does) while the Qt main
    thread keeps running; return the replies sent back to the phone."""
    replies = []

    async def _reply(value):
        replies.append(value)

    message = types.SimpleNamespace(
        text=text, message_id=message_id, reply_text=_reply,
        reply_to_message=types.SimpleNamespace(message_id=reply_to) if reply_to is not None else None,
    )
    update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=int(chat_id)), message=message)
    errors = []

    def _run():
        try:
            asyncio.run(telegram_bridge.handle_message(update, context=None))
        except BaseException as exc:            # surfaced below, never swallowed
            errors.append(exc)

    bridge = threading.Thread(target=_run)
    bridge.start()
    deadline = time.monotonic() + timeout
    while bridge.is_alive() and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.01)
    bridge.join(timeout=1)
    assert not bridge.is_alive(), "bridge never received Prime's reply"
    worker = win.worker
    if worker is not None:
        worker.wait(5000)
    QApplication.processEvents()
    assert not errors, errors
    return replies


def _prime_messaged_phone(win, text="Ready when you are."):
    """Prime sends something to the phone during a desktop turn in this
    conversation -- exactly how a swipe-reply route is created."""
    with routing.origin_scope(win._telegram_route_token, win._chat):
        telegram_send.send_telegram_message(text)
    return win._phone.posts[-1]["message_id"]


def test_swipe_reply_source_vet_runs_in_prime_and_returns_reply_and_report_to_the_phone(prime):
    anchor = _prime_messaged_phone(prime)

    replies = _owner_sends(prime, SOURCE_VET, reply_to=anchor)

    # Prime (the desktop owner agent) handled it, in this conversation.
    assert len(prime._turns) == 1
    turn = prime._turns[0]
    assert turn["input"] == SOURCE_VET
    assert turn["kwargs"]["chat_id"] == prime._chat
    assert turn["kwargs"]["approval_event_id"] == f"telegram:{OWNER}:9001"
    assert turn["origin"] == (prime._telegram_route_token, prime._chat)
    assert turn["thread"] is not threading.main_thread()          # the real AgentWorker thread
    assert prime.agent.owner is True
    # It is in Prime's desktop conversation record.
    from tools.memory import load_chat_messages
    messages = [(m["role"], m["content"]) for m in load_chat_messages(prime._chat)]
    assert ("user", SOURCE_VET) in messages and ("assistant", PRIME_FINAL) in messages
    # The phone got Prime's final answer, the report file and the message.
    assert replies == [PRIME_FINAL]
    sent = [(p["method"], p["data"].get("chat_id"), p["file"]) for p in prime._phone.posts[1:]]
    assert sent == [("sendDocument", OWNER, str(prime._report)), ("sendMessage", OWNER, None)]


def test_swipe_reply_to_the_returned_report_continues_the_same_prime_conversation(prime):
    anchor = _prime_messaged_phone(prime)
    _owner_sends(prime, SOURCE_VET, reply_to=anchor)
    report_message = next(p["message_id"] for p in prime._phone.posts if p["method"] == "sendDocument")

    replies = _owner_sends(prime, "Thanks. Now check the popup code too.", reply_to=report_message,
                           message_id=9002)

    assert replies == [PRIME_FINAL]
    assert [t["kwargs"]["chat_id"] for t in prime._turns] == [prime._chat, prime._chat]
    assert prime._turns[1]["input"] == "Thanks. Now check the popup code too."


def test_plain_follow_up_reaches_the_sole_recent_prime_conversation(prime):
    _prime_messaged_phone(prime)

    replies = _owner_sends(prime, "status of that check?")

    assert replies == [PRIME_FINAL]
    assert prime._turns[0]["kwargs"]["chat_id"] == prime._chat


def test_messages_from_any_other_account_are_rejected_silently(prime):
    anchor = _prime_messaged_phone(prime)

    replies = _owner_sends(prime, SOURCE_VET, chat_id="999", reply_to=anchor)

    assert replies == [] and prime._turns == []


def test_cold_owner_message_uses_the_existing_headless_owner_channel(prime, monkeypatch):
    """No live route (Prime never messaged this chat in this runtime, or the
    route expired): the pre-existing contract is the owner headless channel,
    with owner authority. Pinned here so the report is exact."""
    calls = []
    monkeypatch.setattr(telegram_bridge, "run_headless_turn",
                        lambda **kw: calls.append(kw) or {"success": True, "response": "headless answer"})

    replies = _owner_sends(prime, "hello, anyone home?")

    assert replies == ["headless answer"]
    assert prime._turns == []
    # AGENT-RUNAWAY-SUSPEND-01: intake captured the owner token and carried it.
    from core import suspension
    for call in calls:
        token = call.pop("suspension_token")
        assert type(token) is suspension.ExecutionToken and token.scope == suspension.OWNER_SCOPE
    assert calls == [{"task": "hello, anyone home?", "channel_id": "telegram-owner", "owner": True,
                      "approval_event_id": f"telegram:{OWNER}:9001"}]


@pytest.mark.parametrize("text", [" /stop all", " /btw test", " /compact"])
def test_relayed_slash_text_cannot_invoke_desktop_commands(prime, text):
    from core.operator_ingress import RELAYED_COMMAND_REFUSAL   # C0 module (lazy: baseline runs)
    anchor = _prime_messaged_phone(prime)

    replies = _owner_sends(prime, text, reply_to=anchor)

    assert replies == [RELAYED_COMMAND_REFUSAL]
    assert prime._turns == []
    assert emergency_stop.is_latched() is False                 # " /stop all" did not latch E-stop
    assert prime._btw_task_ids == {}                             # " /btw" started no sidequest
