"""TURN-RECOVERY-UI-01 -- the desktop side, in a real LuminaWindow with a real
LuminaAgent whose backend drops the connection.

  * a local owner turn's transport failure shows one "Continue interrupted
    turn" control; one click starts exactly one continuation worker carrying
    a fenced Claim, writes no user row, and retires the control;
  * double click, a duplicate signal and a re-entrant handler call still
    start one worker;
  * stale generation, chat switch, new chat, a newer message, a durable
    transcript change, E-stop (latched or re-armed), a suspension trip and a
    running turn all refuse with nothing started;
  * a Telegram-relayed turn is never offered;
  * an image-draft approval or the Telegram switch turned on after the
    failure refuses the click with nothing started (the agent's own fence,
    not the button state); the real Approve button retires the offer;
  * the continuation's reply is a new assistant row tagged with its root turn;
  * a click whose authority read fails, or whose claim raises outright,
    refuses visibly and truthfully: no uncaught slot exception, no worker,
    the offer dropped, and the next failed turn is offered again.

Missing Qt fails (never skips) when LUMINA_REQUIRE_QT=1, as in CI's
blocking qt-security-guards job. Every store is disposable.
"""
import os

import pytest

if os.environ.get("LUMINA_REQUIRE_QT") == "1":
    import PySide6  # noqa: F401
else:
    pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import config as config_module
import core.persistence as persistence
from core import emergency_stop, turn_recovery as TR
from core import suspension as S, suspension_runtime as R


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _Worker:
    created = []

    def __init__(self, agent, user_input, signals, chat_id=None, attachments=None, approval_event_id=None,
                 suspension_token=None, offer_recovery=False, recovery=None):
        self.user_input, self.chat_id = user_input, chat_id
        self.offer_recovery, self.recovery = offer_recovery, recovery
        self.suspension_token = suspension_token
        self.started = False
        self.running = False
        _Worker.created.append(self)

    def start(self):
        self.started = True

    def isRunning(self):
        return self.running

    def cancel_requested(self):
        return False


class _DroppingLLM:
    """Every request dies in transport, like the chat #166 TLS storms."""
    name = "fake"
    display_name = "fake"

    def configured_model(self):
        return "model-a"

    def get_model(self):
        return "model-a"

    def chat(self, **kwargs):
        raise ConnectionError("OpenRouter not reachable (SSLError)")


def _make_window(tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(config_module, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config_module, "DB_PATH", str(tmp_path / "data" / "memory" / "lumina.db"))
    monkeypatch.setattr(config_module, "LLM_BACKEND", "llamacpp")
    monkeypatch.setattr(config_module, "LLM_BACKEND_URL", "http://127.0.0.1:8080/v1")
    import core.secrets as secrets_module
    monkeypatch.setattr(secrets_module, "SECRETS_PATH", str(tmp_path / "credentials.json"))
    import core.agent as agent_module
    monkeypatch.setattr(agent_module, "build_skills_block", lambda q: ("", ""))
    import ui.main_window as mw
    monkeypatch.setattr(mw.browser_manager, "close", lambda: None)
    monkeypatch.setattr(mw, "AgentWorker", _Worker)
    _Worker.created = []
    emergency_stop._reset_for_tests()
    from core.agent import LuminaAgent
    from tools.memory import create_chat, init_chat_db
    init_chat_db()
    chat_id = create_chat("pulse")
    agent = LuminaAgent(owner=True, channel_id="pulse-qt")
    agent.llm = _DroppingLLM()
    win = mw.LuminaWindow(agent)
    win._load_chat(chat_id, persist_as_last=False)
    monkeypatch.setattr(mw, "list_chats", lambda: [{"id": chat_id, "name": "pulse"}])
    notices = []
    original = win.chat_widget.add_operator_message
    win.chat_widget.add_operator_message = lambda text: (notices.append(text), original(text))
    win._notices, win._chat = notices, chat_id
    return win


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    win = _make_window(tmp_path, monkeypatch)
    QApplication.processEvents()
    yield win
    win.worker = None
    win.close()
    emergency_stop._reset_for_tests()


def _rows(win):
    from tools.memory import load_chat_messages
    return load_chat_messages(win._chat)


def _fail_a_local_turn(win, text="build it"):
    """Submit through the real path, then run the worker's agent call
    synchronously (the stubbed worker never runs) and deliver its result."""
    win.chat_widget.input.setPlainText(text)
    QTest.keyClick(win.chat_widget.input, Qt.Key_Return)
    QApplication.processEvents()
    worker = _Worker.created[-1]
    assert worker.offer_recovery is True
    result = win.agent.chat(worker.user_input, chat_id=worker.chat_id,
                            suspension_token=worker.suspension_token, offer_recovery=True)
    win._on_finished(result)
    QApplication.processEvents()
    return result


def _control(win):
    control = win._turn_recovery_control
    assert control is not None and control.button.isEnabled()
    return control


def _click(win):
    QTest.mouseClick(_control(win).button, Qt.LeftButton)
    QApplication.processEvents()


def _continuations():
    return [w for w in _Worker.created if w.recovery is not None]


def test_one_click_starts_exactly_one_fenced_continuation(window):
    _fail_a_local_turn(window)
    rows_before = _rows(window)
    assert [r["role"] for r in rows_before] == ["user", "assistant"]
    control = _control(window)
    _click(window)
    (worker,) = _continuations()
    assert isinstance(worker.recovery, TR.Claim) and worker.started and worker.user_input == ""
    assert _rows(window) == rows_before                       # no user row written
    assert control.button.isHidden() and not control.button.isEnabled()
    assert window.agent.interrupted_turn(window._chat) is None


def test_double_click_duplicate_signal_and_reentry_start_one(window):
    _fail_a_local_turn(window)
    control = _control(window)
    QTest.mouseClick(control.button, Qt.LeftButton)
    QTest.mouseClick(control.button, Qt.LeftButton)
    control.continue_requested.emit()
    window._on_continue_interrupted_turn()
    QApplication.processEvents()
    assert len(_continuations()) == 1


def test_the_control_alone_is_one_shot(qapp):
    from ui.chat_widget import TurnRecoveryControl
    from ui.main_window import COLORS
    control = TurnRecoveryControl(COLORS, "lost")
    fired = []
    control.continue_requested.connect(lambda: fired.append(1))
    control.button.click()
    control._on_clicked()
    assert fired == [1] and not control.button.isEnabled()


def test_the_control_stylesheet_is_well_formed(qapp):
    from ui.chat_widget import TurnRecoveryControl
    from ui.main_window import COLORS
    sheet = TurnRecoveryControl(COLORS, "lost").styleSheet()
    assert sheet.startswith("QFrame{") and sheet.endswith("}") and sheet.count("{") == sheet.count("}") == 1


def test_the_handler_alone_consumes_the_offer_before_claiming(window, monkeypatch):
    # Independent of the agent's own one-shot claim: a re-entrant handler
    # call must not even reach claim_interrupted_turn() a second time.
    _fail_a_local_turn(window)
    claims = []
    real = window.agent.claim_interrupted_turn
    monkeypatch.setattr(window.agent, "claim_interrupted_turn",
                        lambda key, **kw: (claims.append(key), real(key, **kw))[1])
    window._on_continue_interrupted_turn()
    window._on_continue_interrupted_turn()
    assert len(claims) == 1 and len(_continuations()) == 1
    assert not any(n.startswith("Can't continue") for n in window._notices)  # second call was silent


@pytest.mark.parametrize("staleness", [
    lambda w: w._context_generation.bump(),
    lambda w: __import__("tools.memory", fromlist=["x"]).save_chat_message(w._chat, "assistant", "late row"),
    lambda w: emergency_stop.latch(source="test", reason="fence"),
    lambda w: (emergency_stop.latch(source="test", reason="fence"), emergency_stop.rearm_local()),
    lambda w: R.trip(S.Trigger.BREAKER_TRIPPED, {"tool": "read_file", "calls": 20}),
    lambda w: setattr(w.agent.llm, "configured_model", lambda: "model-b"),
])
def test_stale_offers_refuse_with_nothing_started(window, staleness):
    _fail_a_local_turn(window)
    staleness(window)
    _click(window)
    assert _continuations() == []
    assert window.agent.interrupted_turn(window._chat) is None
    assert window._turn_recovery_control is None


@pytest.fixture
def staged_draft(window):
    from core import image_generation_draft as drafts
    draft = drafts.stage_draft(specialist="s", model="m", settings={}, cost_estimate=1.0, cost_unit="u",
                               manifest_provider="p", channel_id=window.agent.channel_id,
                               chat_id=window._chat, staged_at_turn_seq=0)
    assert drafts.mark_draft_presented(draft.draft_id, channel_id=window.agent.channel_id,
                                       chat_id=window._chat)
    yield draft.draft_id
    drafts.discard_draft(draft.draft_id)


@pytest.mark.parametrize("grant", ["draft_approval", "telegram_switch"])
def test_authority_granted_after_the_failure_refuses_the_click(window, staged_draft, grant):
    from comms import telegram_access
    from core import image_generation_draft as drafts
    telegram_access._reset_for_tests()
    _fail_a_local_turn(window)
    control = _control(window)
    if grant == "draft_approval":
        assert drafts.approve_draft(staged_draft, channel_id=window.agent.channel_id, chat_id=window._chat)
    else:
        assert telegram_access.turn_on_by_owner()      # what the Settings switch calls
    _click(window)
    assert _continuations() == []
    assert window.agent.interrupted_turn(window._chat) is None
    assert TR.REASON_TEXT[TR.AUTHORITY_WIDENED] in control.label.text()
    telegram_access._reset_for_tests()


def test_the_real_approve_button_retires_the_offer(window, staged_draft):
    _fail_a_local_turn(window)
    window._on_approve_draft_clicked(staged_draft)
    QApplication.processEvents()
    assert window.agent.interrupted_turn(window._chat) is None
    assert window._turn_recovery_control is None and _continuations() == []


def test_running_turn_refuses(window):
    _fail_a_local_turn(window)
    busy = _Worker(None, "x", None)
    busy.running = True
    window.worker = busy
    _click(window)
    assert _continuations() == []


@pytest.mark.parametrize("navigate", ["_new_chat", "_clear_chat"])
def test_navigation_retires_the_offer(window, navigate):
    _fail_a_local_turn(window)
    getattr(window, navigate)()
    QApplication.processEvents()
    assert window._turn_recovery_control is None and window._turn_recovery_offer is None
    assert window.agent.interrupted_turn(window._chat) is None
    window._on_continue_interrupted_turn()
    assert _continuations() == []


def test_switching_away_and_back_leaves_nothing_to_continue(window):
    _fail_a_local_turn(window)
    from tools.memory import create_chat
    other = create_chat("other")
    window._load_chat(other, persist_as_last=False)
    window._load_chat(window._chat, persist_as_last=False)
    QApplication.processEvents()
    assert window._turn_recovery_control is None
    assert window.agent.interrupted_turn(window._chat) is None


def test_a_newer_message_supersedes_the_offer(window):
    _fail_a_local_turn(window)
    control = _control(window)
    window.chat_widget.input.setPlainText("never mind, do something else")
    QTest.keyClick(window.chat_widget.input, Qt.Key_Return)
    QApplication.processEvents()
    assert control.button.isHidden() and window._turn_recovery_offer is None
    assert window.agent.interrupted_turn(window._chat) is None


def test_relayed_telegram_turn_is_never_offered(window):
    from comms import telegram_origin_routing as routing
    token = R.capture(S.OWNER_SCOPE)
    route = routing.OriginRoute("42", 7001, window._chat, window._telegram_route_token, 1.0, 1e12)
    request = routing.OriginDispatch(route=route, text="hello from my phone",
                                     approval_event_id="telegram:42:9001", suspension_token=token)
    window._on_telegram_origin_dispatch(request)
    QApplication.processEvents()
    assert _Worker.created and _Worker.created[-1].offer_recovery is False


def test_continuation_reply_is_a_new_tagged_row(window):
    _fail_a_local_turn(window)
    _click(window)
    (worker,) = _continuations()
    window.worker = worker
    window._on_finished("here is the finished answer")
    rows = _rows(window)
    assert [r["role"] for r in rows] == ["user", "assistant", "assistant"]
    assert rows[1]["content"].startswith("[Lumina error:")                  # untouched
    assert rows[2]["metadata"] == {"turn_recovery": {
        "root_turn_id": worker.recovery.record.root_turn_id, "attempt": 1}}



# ── R5 / Sonnet N-R4-2: the click never dies silently ──

@pytest.fixture
def slot_errors(monkeypatch):
    """PySide reports an exception escaping a slot through sys.excepthook
    and carries on; record it so a silently dead button fails the test."""
    import sys
    seen = []
    monkeypatch.setattr(sys, "excepthook", lambda *exc_info: seen.append(exc_info[1]))
    return seen


def test_an_unreadable_tool_list_refuses_the_click_truthfully(window, slot_errors, monkeypatch):
    _fail_a_local_turn(window)
    control = _control(window)

    def unreadable():
        raise RuntimeError("tool registry unavailable")
    monkeypatch.setattr(window.agent.registry, "get_schemas", unreadable)
    _click(window)
    assert slot_errors == [] and _continuations() == []
    assert window.agent.interrupted_turn(window._chat) is None and window._turn_recovery_control is None
    assert TR.REASON_TEXT[TR.AUTHORITY_UNREADABLE] in control.label.text()
    assert "widened" not in control.label.text() and "granted" not in control.label.text()


def test_a_claim_that_raises_refuses_visibly_and_the_next_failure_is_offered(window, slot_errors, monkeypatch):
    _fail_a_local_turn(window)
    control = _control(window)
    real = window.agent.claim_interrupted_turn

    def broken(key, **kw):
        raise RuntimeError("backend identity unreadable")
    monkeypatch.setattr(window.agent, "claim_interrupted_turn", broken)
    _click(window)
    assert slot_errors == [] and _continuations() == []
    assert window.agent.interrupted_turn(window._chat) is None and window._turn_recovery_control is None
    assert "could not be checked" in control.label.text()
    assert control.button.isHidden() and not control.button.isEnabled()
    monkeypatch.setattr(window.agent, "claim_interrupted_turn", real)
    _fail_a_local_turn(window, text="try again")                    # retry is a new turn, offered afresh
    _click(window)
    (worker,) = _continuations()
    assert isinstance(worker.recovery, TR.Claim)
