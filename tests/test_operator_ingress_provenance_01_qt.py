"""OPERATOR-INGRESS-PROVENANCE-01 -- blocking real-Qt desktop boundary.

Drives the real LuminaWindow: the real SmartInput Enter key for local
authority, and the real Telegram origin queue (_on_telegram_origin_dispatch
-> _drain_telegram_origin_queue -> _on_user_message) for relayed text. Every
operator-command executor is replaced by a spy so a dispatch is observable
and nothing real runs; AgentWorker is a recording stub so an admitted turn
is observable without a model.

Missing Qt fails (never skips) when LUMINA_REQUIRE_QT=1, as in CI's
qt-security-guards job.
"""
import concurrent.futures
import os

import pytest

if os.environ.get("LUMINA_REQUIRE_QT") == "1":
    import PySide6  # noqa: F401
else:
    pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import config as config_module
import core.operator_ingress as ingress
import core.persistence as persistence
from core import emergency_stop
from core.operator_commands import parse_operator_command

EXECUTORS = ("_command_status", "_command_btw", "_command_compact", "_command_stop", "_command_context")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _Worker:
    created = []

    def __init__(self, agent, user_input, signals, chat_id=None, attachments=None, approval_event_id=None,
                 suspension_token=None):
        self.user_input = user_input
        self.approval_event_id = approval_event_id
        self.suspension_token = suspension_token
        self.started = False
        _Worker.created.append(self)

    def start(self):
        self.started = True

    def isRunning(self):
        return False

    def cancel_requested(self):
        return False


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(config_module, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config_module, "DB_PATH", str(tmp_path / "data" / "memory" / "lumina.db"))
    monkeypatch.setattr(config_module, "LLM_BACKEND", "llamacpp")
    monkeypatch.setattr(config_module, "LLM_BACKEND_URL", "http://127.0.0.1:8080/v1")
    import core.secrets as secrets_module
    monkeypatch.setattr(secrets_module, "SECRETS_PATH", str(tmp_path / "credentials.json"))
    import ui.main_window as mw
    monkeypatch.setattr(mw.browser_manager, "close", lambda: None)
    monkeypatch.setattr(mw, "AgentWorker", _Worker)
    _Worker.created = []
    emergency_stop._reset_for_tests()

    from core.agent import LuminaAgent
    from tools.memory import create_chat, init_chat_db
    init_chat_db()
    chat_id = create_chat("ingress")
    win = mw.LuminaWindow(LuminaAgent(owner=True, channel_id="operator-ingress-qt"))
    win._load_chat(chat_id, persist_as_last=False)
    monkeypatch.setattr(mw, "list_chats", lambda: [{"id": chat_id}])

    calls = []
    for name in EXECUTORS:
        setattr(win, name, (lambda n: (lambda argument="": calls.append((n, argument))))(name))
    notices = []
    original_notice = win.chat_widget.add_operator_message
    win.chat_widget.add_operator_message = lambda text: (notices.append(text), original_notice(text))
    win._calls, win._notices, win._chat = calls, notices, chat_id
    yield win
    win.worker = None
    win.close()
    emergency_stop._reset_for_tests()


def _type(win, text):
    win.chat_widget.input.setPlainText(text)
    QTest.keyClick(win.chat_widget.input, Qt.Key_Return)
    QApplication.processEvents()


def _relay(win, text):
    from comms import telegram_origin_routing as routing
    route = routing.OriginRoute("42", 7001, win._chat, win._telegram_route_token, 1.0, 1e12)
    # AGENT-RUNAWAY-SUSPEND-01: Telegram intake captures the owner token
    # before the request waits for Prime; the relay carries it.
    from core import suspension, suspension_runtime
    request = routing.OriginDispatch(route=route, text=text, approval_event_id="telegram:42:9001",
                                     suspension_token=suspension_runtime.capture(suspension.OWNER_SCOPE))
    win._on_telegram_origin_dispatch(request)
    QApplication.processEvents()
    return request


# ── Local authority is unchanged ─────────────────────────────────────────

@pytest.mark.parametrize("text, expected", [
    ("/status", ("_command_status", "")),
    ("/btw what is up", ("_command_btw", "what is up")),
    ("/stop all", ("_command_stop", "all")),
    ("/context reforge", ("_command_context", "reforge")),
])
def test_owner_typing_a_command_in_the_desktop_chat_still_dispatches_it(window, text, expected):
    _type(window, text)
    assert window._calls == [expected]
    assert _Worker.created == []


def test_the_input_signal_is_wired_to_the_local_submission_path(window):
    window.chat_widget.message_submitted.emit("/status")
    QApplication.processEvents()
    assert window._calls == [("_command_status", "")]


# ── Relayed text never dispatches, whatever its shape ────────────────────

@pytest.mark.parametrize("text", [
    " /btw test", " /stop all", " /compact", "\t/context reforge", "/status", "/stop", "/unknown x",
])
def test_relayed_command_shaped_text_is_refused_with_no_command_and_no_turn(window, text):
    request = _relay(window, text)

    assert window._calls == []
    assert _Worker.created == []
    assert request.future.done() and request.future.result() == ingress.RELAYED_COMMAND_REFUSAL
    assert window._telegram_active_dispatch is None
    assert any("refused" in n.lower() for n in window._notices)


def test_a_refused_relay_never_becomes_a_headless_fallback(window):
    request = _relay(window, " /btw test")
    # The bridge falls back to a cold headless turn only on an exception
    # (OriginUnavailable); a completed refusal must not raise.
    assert request.future.exception() is None


def test_relayed_ordinary_text_is_still_an_ordinary_owner_turn(window):
    request = _relay(window, "hello from my phone")

    assert window._calls == []
    assert len(_Worker.created) == 1 and _Worker.created[0].started
    assert _Worker.created[0].approval_event_id == "telegram:42:9001"
    assert _Worker.created[0].suspension_token is request.suspension_token   # carried, not re-minted
    assert window._telegram_active_dispatch is request


# ── Paths with no local envelope fail closed ─────────────────────────────

def test_a_call_with_no_ingress_cannot_dispatch_a_command(window):
    window._on_user_message("/stop all")
    assert window._calls == []
    assert any("refused" in n.lower() for n in window._notices)


def test_a_call_with_no_ingress_still_admits_ordinary_text(window):
    window._on_user_message("plain text")
    assert len(_Worker.created) == 1


def test_the_dispatcher_itself_requires_a_local_envelope(window):
    command = parse_operator_command("/status")
    with pytest.raises(TypeError):
        window._dispatch_operator_command(command)
    window._dispatch_operator_command(command, ingress=ingress.telegram_relayed_text("telegram:42:1"))
    window._dispatch_operator_command(command, ingress=ingress.local_runtime_action("x"))
    assert window._calls == []
    window._dispatch_operator_command(command, ingress=ingress.local_text_submission())
    assert window._calls == [("_command_status", "")]


def test_the_approval_button_turn_is_a_runtime_action_never_typed_text_authority(window, monkeypatch):
    import core.image_generation_draft as drafts
    monkeypatch.setattr(drafts, "approve_draft", lambda *a, **k: True)
    seen = []
    original = window._on_user_message
    window._on_user_message = lambda text, **kw: (seen.append(kw.get("ingress")), original(text, **kw))

    window._on_approve_draft_clicked("draft-1")

    assert len(seen) == 1
    assert seen[0].kind is ingress.IngressKind.LOCAL_RUNTIME_ACTION
    assert not ingress.may_dispatch_operator_command(seen[0])
    assert len(_Worker.created) == 1                            # the follow-up turn still runs


def test_ci_runs_this_file_as_a_blocking_qt_guard():
    workflow = open(os.path.join(os.path.dirname(__file__), "..", ".github", "workflows", "tests.yml")).read()
    qt_job = workflow[workflow.index("qt-security-guards:"):]
    assert 'LUMINA_REQUIRE_QT: "1"' in qt_job
    assert "tests/test_operator_ingress_provenance_01_qt.py" in qt_job
    assert "tests/test_telegram_prime_continuity_qt.py" in qt_job
