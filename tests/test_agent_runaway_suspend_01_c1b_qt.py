"""AGENT-RUNAWAY-SUSPEND-01 Slice C1b -- the desktop side, in a real LuminaWindow.

  * the owner's submit captures the owner token on the Qt thread and hands
    exactly it to the worker; a suspended scope refuses before any worker
    exists, and the typed text is restored;
  * a Telegram-relayed turn runs only under the token intake captured: a
    stale one is refused, a missing one is refused, nothing is re-minted;
  * /btw captures and carries its token into the task queue;
  * trip receipts, /status, the startup readiness check and /suspension;
  * the Suspension controls dialog: explicit Initialize, Resume (incl. drain
    and outcome-unknown acknowledgement), Repair identity, Recover, and the
    selector shown only there.

Missing Qt fails (never skips) when LUMINA_REQUIRE_QT=1, as in CI's
blocking qt-security-guards job. Every store is disposable.
"""
import os
import sqlite3
import time

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
from core import emergency_stop, operator_ingress as oi
from core import suspension as S, suspension_runtime as R


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _Worker:
    created = []

    def __init__(self, agent, user_input, signals, chat_id=None, attachments=None, approval_event_id=None,
                 suspension_token=None):
        self.user_input = user_input
        self.suspension_token = suspension_token
        self.started = False
        _Worker.created.append(self)

    def start(self):
        self.started = True

    def isRunning(self):
        return False

    def cancel_requested(self):
        return False


def _make_window(tmp_path, monkeypatch):
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
    chat_id = create_chat("suspension")
    win = mw.LuminaWindow(LuminaAgent(owner=True, channel_id="suspension-qt"))
    win._load_chat(chat_id, persist_as_last=False)
    monkeypatch.setattr(mw, "list_chats", lambda: [{"id": chat_id}])
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
    if win._suspension_dialog is not None:
        win._suspension_dialog.close()
    win.close()
    emergency_stop._reset_for_tests()


def _type(win, text):
    win.chat_widget.input.setPlainText(text)
    QTest.keyClick(win.chat_widget.input, Qt.Key_Return)
    QApplication.processEvents()


def _trip():
    outcome = R.trip(S.Trigger.BREAKER_TRIPPED, {"tool": "read_file", "calls": 20})
    assert outcome.result is S.TripResult.TRIPPED
    return outcome


def _relay(win, token):
    from comms import telegram_origin_routing as routing
    route = routing.OriginRoute("42", 7001, win._chat, win._telegram_route_token, 1.0, 1e12)
    request = routing.OriginDispatch(route=route, text="hello from my phone",
                                     approval_event_id="telegram:42:9001", suspension_token=token)
    win._on_telegram_origin_dispatch(request)
    QApplication.processEvents()
    return request


# ── Desktop submit ───────────────────────────────────────────────────────

def test_a_typed_turn_captures_the_owner_token_and_hands_exactly_it_to_the_worker(window):
    _type(window, "hello")
    assert len(_Worker.created) == 1 and _Worker.created[0].started
    token = _Worker.created[0].suspension_token
    assert type(token) is S.ExecutionToken and token.scope == S.OWNER_SCOPE
    R.verify(token)


def test_a_suspended_scope_refuses_before_any_worker_and_restores_the_text(window):
    _trip()
    _type(window, "please continue")
    assert _Worker.created == []
    assert any("runaway suspension is active since" in n and "not sent" in n for n in window._notices)
    QApplication.processEvents()
    assert window.chat_widget.input.toPlainText() == "please continue"


def test_a_relayed_turn_runs_under_exactly_the_intake_token(window):
    token = R.capture(S.OWNER_SCOPE)
    request = _relay(window, token)
    assert len(_Worker.created) == 1 and _Worker.created[0].suspension_token is token
    assert window._telegram_active_dispatch is request


def test_a_relayed_turn_whose_token_went_stale_while_waiting_is_refused(window):
    token = R.capture(S.OWNER_SCOPE)
    _trip()
    request = _relay(window, token)
    assert _Worker.created == []
    assert request.future.done() and isinstance(request.future.exception(), Exception)
    assert window.chat_widget.input.toPlainText() == ""                       # never put in the input


def test_a_relayed_turn_without_a_token_is_refused_never_minted(window):
    request = _relay(window, None)
    assert _Worker.created == [] and request.future.done()
    assert any("no suspension lineage" in n for n in window._notices)


# ── /btw, /status, /suspension ───────────────────────────────────────────

def test_btw_captures_its_token_and_carries_it_into_the_task_queue(window, monkeypatch):
    import tools.tasks as tasks
    seen = []
    monkeypatch.setattr(config_module, "BACKGROUND_TASKS_ENABLED", True)
    monkeypatch.setattr(config_module, "SUBAGENTS_ENABLED", True)
    monkeypatch.setattr(tasks, "run_background_subagent",
                        lambda question, persona=None: seen.append(R.inherited()) or {"task_id": "t" * 32})
    reset = S._token_var.set(None)                       # the Qt thread itself has no lineage
    try:
        _type(window, "/btw what time is it")
    finally:
        S._token_var.reset(reset)
    assert len(seen) == 1 and seen[0].scope == S.OWNER_SCOPE


def test_btw_is_refused_while_suspended(window, monkeypatch):
    import tools.tasks as tasks
    monkeypatch.setattr(config_module, "BACKGROUND_TASKS_ENABLED", True)
    monkeypatch.setattr(config_module, "SUBAGENTS_ENABLED", True)
    monkeypatch.setattr(tasks, "run_background_subagent", lambda *a, **k: pytest.fail("must not start"))
    _trip()
    _type(window, "/btw what time is it")
    assert any(n.startswith("/btw not started.") for n in window._notices)


def test_status_reports_runaway_suspension(window):
    _type(window, "/status")
    assert any("Runaway suspension: active, nothing suspended" in n for n in window._notices)
    _trip()
    _type(window, "/status")
    assert any("Runaway suspension: 1 suspended scope(s)" in n for n in window._notices)


def test_the_suspension_command_opens_the_controls(window):
    _type(window, "/suspension")
    assert window._suspension_dialog is not None and window._suspension_dialog.isVisible()


# ── Receipts ─────────────────────────────────────────────────────────────

def test_a_trip_posts_a_machine_receipt_without_the_selector(window):
    outcome = _trip()
    QApplication.processEvents()
    receipt = [n for n in window._notices if n.startswith("⛔ Runaway suspension: owner is suspended")]
    assert len(receipt) == 1 and "Suspension controls (/suspension)" in receipt[0]
    assert R.operator_view()[0].selector not in receipt[0]
    assert outcome.suspension_id not in receipt[0]


def test_an_unrecorded_trip_says_global_suspension_unconfirmed(window):
    window._on_suspension_event("trip", S.TripOutcome(S.TripResult.GLOBAL_UNCONFIRMED, S.OWNER_SCOPE))
    assert any("global suspension unconfirmed" in n for n in window._notices)


def test_startup_offers_initialize_or_recover_when_the_store_is_not_ready(qapp, tmp_path, monkeypatch):
    root = tmp_path / "fresh-root"
    (root / S.MEMORY_DIRNAME).mkdir(parents=True, mode=0o700)
    R.start(root, role="desktop", heartbeat=False)
    win = _make_window(tmp_path, monkeypatch)
    try:
        QApplication.processEvents()
        dialog = win._suspension_dialog                                        # opened by the startup check
        assert dialog is not None and dialog.isVisible()
        assert "No runaway-suspension store exists" in dialog.state_label.text()
        win._check_suspension_ready()
        assert any("isn't ready for this data folder" in n for n in win._notices)
        assert os.listdir(root / S.MEMORY_DIRNAME) == []                    # nothing created on its own
    finally:
        win._suspension_dialog.close()
        win.close()


# ── The Suspension controls dialog ───────────────────────────────────────

@pytest.fixture
def dialog(qapp):
    from ui.suspension_dialog import SuspensionDialog
    made = []

    def make():
        d = SuspensionDialog()
        made.append(d)
        return d

    yield make
    for d in made:
        d.close()


def test_initialize_needs_the_explicit_first_launch_confirmation(qapp, tmp_path, dialog):
    root = tmp_path / "init-root"
    (root / S.MEMORY_DIRNAME).mkdir(parents=True, mode=0o700)
    R.start(root, role="desktop", heartbeat=False)
    d = dialog()
    assert d.initialize_btn.isVisible() is False or not d.isVisible()
    assert d.initialize_btn.isEnabled() is False
    d._initialize()                                               # without the checkbox: nothing happens
    assert os.listdir(root / S.MEMORY_DIRNAME) == []
    d.first_run_confirm.setChecked(True)
    assert d.initialize_btn.isEnabled()
    d.initialize_btn.click()
    assert R.status().state is R.State.ACTIVE and "Initialized" in d.result_label.text()


def test_resume_clears_exactly_the_selected_incident_and_shows_its_selector(qapp, dialog, monkeypatch):
    _trip()
    view = R.operator_view()[0]
    seen = []
    real = R.resume
    monkeypatch.setattr(R, "resume", lambda v, *, ingress, ack_unknown=False:
                        seen.append(ingress) or real(v, ingress=ingress, ack_unknown=ack_unknown))
    d = dialog()
    assert d.incidents.count() == 1 and view.selector in d.detail_label.text()
    d.resume_btn.click()
    assert "Resumed owner" in d.result_label.text() and d.incidents.count() == 0
    assert seen[0].kind is oi.IngressKind.LOCAL_SUSPENSION_CONTROL           # minted at the click
    R.capture(S.OWNER_SCOPE)


def test_resume_waits_while_earlier_work_is_still_running(qapp, dialog):
    token = R.inherited()
    store = R._store
    lease = store.admit(token, kind=S.LeaseKind.TOOL, name="slow_tool", runtime_id=R.runtime_id())
    _trip()
    d = dialog()
    assert "Still running: tool:slow_tool" in d.detail_label.text()
    d.resume_btn.click()
    assert d.result_label.text().startswith("Not yet: 1 earlier dispatch")
    store.finish(lease, R.runtime_id())
    d.refresh()
    d.resume_btn.click()
    assert "Resumed owner" in d.result_label.text()


def test_work_whose_process_died_needs_the_acknowledgement(qapp, dialog):
    token = R.inherited()
    db = os.path.join(R.status().data_dir, S.MEMORY_DIRNAME, S.STORE_NAME)
    conn = sqlite3.connect(db)
    with conn:
        conn.execute("INSERT INTO runtimes VALUES ('dead', 999999999, 'gone-boot', ?, 1, 1, 'x', 0, 0)",
                     (S.socket.gethostname(),))
        conn.execute("INSERT INTO leases VALUES ('orphan', ?, 'owner', ?, 'dead', 'tool', 'lost_tool', ?, "
                     "'in_flight', NULL)", (token.store_incarnation, token.execution_epoch, time.time()))
    conn.close()
    _trip()
    d = dialog()
    assert "Outcome unknown (process stopped): tool:lost_tool" in d.detail_label.text()
    d.resume_btn.click()
    assert "outcome is unknown" in d.result_label.text()
    d.ack_unknown.setChecked(True)
    d.resume_btn.click()
    assert "Resumed owner" in d.result_label.text()


def test_repair_identity_keeps_every_suspension(qapp, tmp_path, dialog):
    root = tmp_path / "repair-root"
    (root / S.MEMORY_DIRNAME).mkdir(parents=True, mode=0o700)
    S.initialize(root, ingress=oi.local_suspension_control("t")).close()
    R.start(root, role="desktop", heartbeat=False)
    with R.carrying(R.capture(S.OWNER_SCOPE)):
        _trip()
    R.stop()
    (root / S.MEMORY_DIRNAME / S.IDENTITY_NAME).unlink()
    R.start(root, role="desktop", heartbeat=False)
    d = dialog()
    assert d.repair_btn.isVisibleTo(d) and R.status().state is R.State.FAULT
    d.repair_btn.click()
    assert R.status().state is R.State.ACTIVE and d.incidents.count() == 1


def test_recover_after_a_lost_store_starts_with_the_owner_suspended(qapp, tmp_path, dialog):
    root = tmp_path / "recover-root"
    (root / S.MEMORY_DIRNAME).mkdir(parents=True, mode=0o700)
    S.initialize(root, ingress=oi.local_suspension_control("t")).close()
    for name in (S.STORE_NAME, S.STORE_NAME + "-wal", S.STORE_NAME + "-shm"):
        (root / S.MEMORY_DIRNAME / name).unlink(missing_ok=True)
    R.start(root, role="desktop", heartbeat=False)
    d = dialog()
    assert R.status().state is R.State.UNINITIALIZED and d.recover_btn.isVisibleTo(d)
    assert not d.initialize_btn.isVisibleTo(d)                               # traces: never initialize
    d.recover_btn.click()                                    # registry unreadable: needs the ack
    assert "Recovery waiting" in d.result_label.text()
    d.ack_runtimes.setChecked(True)
    d.recover_btn.click()
    assert "Recovered" in d.result_label.text() and R.status().state is R.State.ACTIVE
    assert [v.scope for v in R.operator_view()] == [S.OWNER_SCOPE]


def test_recover_with_no_trace_needs_its_own_acknowledgement_and_restores_nothing(qapp, tmp_path, dialog):
    # LUMINA-C1-UPGRADE-SAFETY-02 Slice A: the screen recommends Recover when
    # unsure, so Recover must work when no trace survives.
    root = tmp_path / "no-trace-root"
    (root / S.MEMORY_DIRNAME).mkdir(parents=True, mode=0o700)
    R.start(root, role="desktop", heartbeat=False)
    d = dialog()
    assert d.recover_btn.isVisibleTo(d) and d.unknown_prior_state.isVisibleTo(d)
    assert not d.recover_btn.isEnabled()
    assert not d.ack_runtimes.isVisibleTo(d) and not d.unknown_scopes_clear.isVisibleTo(d)
    d._recover()                                                # without the acknowledgement: nothing
    assert os.listdir(root / S.MEMORY_DIRNAME) == []
    d.first_run_confirm.setChecked(True)
    d.unknown_prior_state.setChecked(True)                    # the two answers exclude each other
    assert not d.first_run_confirm.isChecked() and not d.initialize_btn.isEnabled()
    assert d.recover_btn.isEnabled()
    d.recover_btn.click()
    text = d.result_label.text()
    assert "nothing was restored" in text and "Recovered." not in text
    assert R.status().state is R.State.ACTIVE
    views = R.operator_view()
    assert [v.scope for v in views] == [S.OWNER_SCOPE] and views[0].requires_ack
    assert not d.initialize_btn.isVisibleTo(d) and not d.unknown_prior_state.isVisibleTo(d)
    d.resume_btn.click()                                       # outcome unknown: needs the ack
    assert "outcome is unknown" in d.result_label.text()
    d.ack_unknown.setChecked(True)
    d.resume_btn.click()
    assert "Resumed owner" in d.result_label.text()


def test_initialize_and_no_trace_recovery_exclude_each_other(qapp, tmp_path, dialog):
    root = tmp_path / "exclusive-root"
    (root / S.MEMORY_DIRNAME).mkdir(parents=True, mode=0o700)
    R.start(root, role="desktop", heartbeat=False)
    d = dialog()
    d.unknown_prior_state.setChecked(True)
    d.first_run_confirm.setChecked(True)
    assert not d.unknown_prior_state.isChecked() and not d.recover_btn.isEnabled()
    d.initialize_btn.click()
    assert "Initialized" in d.result_label.text()
    assert R.operator_view() == ()


def test_ci_runs_this_file_as_a_blocking_qt_guard():
    workflow = open(os.path.join(os.path.dirname(__file__), "..", ".github", "workflows", "tests.yml")).read()
    qt_job = workflow[workflow.index("qt-security-guards:"):]
    assert 'LUMINA_REQUIRE_QT: "1"' in qt_job
    assert "tests/test_agent_runaway_suspend_01_c1b_qt.py" in qt_job


# ── Applying staged items while suspended (C1 stopgap, owner ruling Q3) ──

import json
import sys
from types import SimpleNamespace

from PySide6.QtWidgets import QMessageBox

import core.image_generation_draft as draft_store
from core import palace_promotion as pp
from tools import palace, pending_actions, toolmaker
from tools.registry import ToolRegistry


class _Ctx:
    def __init__(self):
        self.prompts = []

    def update_system_prompt(self, text):
        self.prompts.append(text)

    def clear(self):
        self.prompts.append("<cleared>")


@pytest.fixture
def tools_tab(qapp, tmp_path, monkeypatch):
    from ui.main_window import COLORS
    from ui.settings import ToolsTab
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(config_module, "DB_PATH", str(tmp_path / "ui.db"))
    monkeypatch.setattr(pending_actions, "QUEUE_PATH", str(tmp_path / "pending_actions.json"))
    monkeypatch.setattr(pending_actions, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    monkeypatch.setattr(toolmaker, "PENDING_DIR", str(tmp_path / "_pending"))
    monkeypatch.setattr(toolmaker, "CUSTOM_TOOLS_DIR", str(tmp_path / "custom"))
    monkeypatch.setattr(toolmaker, "AUDIT_LOG_PATH", str(tmp_path / "tool_audit.log"))
    os.makedirs(toolmaker.PENDING_DIR), os.makedirs(toolmaker.CUSTOM_TOOLS_DIR)
    pp._LIVE.clear()
    palace.init_palace_db()
    agent = SimpleNamespace(registry=ToolRegistry(), _subagent_depth=0, _background_task_ids=set(), ctx=_Ctx())
    tab = ToolsTab(agent, COLORS)
    yield tab
    tab.deleteLater()
    pp._LIVE.clear()
    sys.modules.pop("q3_hello", None)


def _audit_events():
    with open(pending_actions.AUDIT_LOG_PATH) as f:
        return [json.loads(line)["event"] for line in f if line.strip()]


def test_q3_a_trip_between_the_settings_preview_and_yes_refuses_the_pending_action(tools_tab, monkeypatch):
    pending_actions.stage_action("edit_prompt", {"new_prompt": "be someone else"})
    tools_tab._load_pending_actions()
    tools_tab.pending_actions_list.selectRow(0)
    QApplication.processEvents()
    assert "be someone else" in tools_tab.pending_actions_preview.toPlainText()  # previewed while clear
    assert tools_tab.pending_actions_approve_btn.isEnabled()

    def trip_then_yes(*args, **kwargs):
        _trip()
        return QMessageBox.Yes

    monkeypatch.setattr(QMessageBox, "question", staticmethod(trip_then_yes))
    QTest.mouseClick(tools_tab.pending_actions_approve_btn, Qt.LeftButton)
    QApplication.processEvents()
    label = tools_tab.pending_actions_status_lbl.text()
    assert "runaway suspension is active" in label and "still pending" in label
    assert tools_tab.agent.ctx.prompts == []
    assert tools_tab.pending_actions_list.rowCount() == 1 and len(pending_actions._load_queue()) == 1
    assert _audit_events() == ["staged", "refused"]


def test_q3_the_pending_tools_approve_button_is_refused_while_suspended(tools_tab, monkeypatch):
    with open(os.path.join(toolmaker.PENDING_DIR, "q3_hello.py"), "w") as f:
        f.write("def register_q3_hello_tool(registry):\n"
                "    registry.register('q3_hello', lambda: 'hi', 'hi', {'type': 'object', 'properties': {}})\n")
    tools_tab._load_pending_tools()
    tools_tab.pending_list.selectRow(0)
    QApplication.processEvents()
    _trip()
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    QTest.mouseClick(tools_tab.pending_approve_btn, Qt.LeftButton)
    QApplication.processEvents()
    assert "runaway suspension is active" in tools_tab.pending_status_lbl.text()
    assert os.listdir(toolmaker.PENDING_DIR) == ["q3_hello.py"] and os.listdir(toolmaker.CUSTOM_TOOLS_DIR) == []
    assert "q3_hello" not in tools_tab.agent.registry.list_tools()


def test_q3_the_palace_review_approve_click_is_refused_while_suspended(tools_tab, monkeypatch):
    from ui.palace_promotion_review import PalacePromotionReviewDialog
    palace._palace_remember_tool("crown me", "identity", "self", 1)
    tools_tab._load_pending_actions()
    [(aid, entry)] = pending_actions._load_queue().items()
    tools_tab.pending_actions_list.selectRow(0)
    QApplication.processEvents()

    def fake_exec(self):
        self.show()
        QApplication.processEvents()
        _trip()
        QTest.mouseClick(self.approve_btn, Qt.LeftButton)
        QApplication.processEvents()
        return self.result()

    monkeypatch.setattr(PalacePromotionReviewDialog, "exec", fake_exec)
    tools_tab._approve_pending_action()
    label = tools_tab.pending_actions_status_lbl.text()
    assert "NOT applied" in label and "runaway suspension is active" in label
    assert aid in pending_actions._load_queue()
    did = entry["payload"]["target_id"]
    conn = sqlite3.connect(config_module.DB_PATH)
    layer = conn.execute("SELECT c.layer FROM palace_closets c JOIN palace_drawers d ON d.closet_id=c.id "
                         "WHERE d.id=?", (did,)).fetchone()[0]
    receipts = conn.execute("SELECT COUNT(*) FROM palace_promotion_receipts").fetchone()[0]
    conn.close()
    assert layer == 2 and receipts == 0


@pytest.fixture
def presented_draft(window):
    draft_store._drafts.clear(), draft_store._approvals.clear(), draft_store._presented.clear()
    draft = draft_store.stage_draft(
        specialist="higgsfield", model="higgsfield-ai/soul/standard", settings={"prompt": "q3"},
        cost_estimate=0.05, cost_unit="usd", manifest_provider="higgsfield",
        channel_id=window.agent.channel_id, chat_id=window._current_chat_id, staged_at_turn_seq=0)
    assert draft_store.mark_draft_presented(draft.draft_id, channel_id=window.agent.channel_id,
                                            chat_id=window._current_chat_id)
    yield draft
    draft_store._drafts.clear(), draft_store._approvals.clear(), draft_store._presented.clear()


def test_q3_the_draft_approve_button_is_refused_while_suspended_and_sends_no_follow_up(window, presented_draft,
                                                                                       monkeypatch):
    _trip()
    follow_ups = []
    real_submit = window._on_user_message
    monkeypatch.setattr(window, "_on_user_message", lambda *a, **k: follow_ups.append(a))
    window._on_approve_draft_clicked(presented_draft.draft_id)
    QApplication.processEvents()
    assert any("staged items can't be applied right now" in n and "still pending" in n for n in window._notices)
    assert not draft_store.is_approved(presented_draft.draft_id)
    assert follow_ups == [] and _Worker.created == []  # no synthetic "[Owner approved via button]" turn
    monkeypatch.setattr(window, "_on_user_message", real_submit)
    for view in R.operator_view():
        R.resume(view, ingress=oi.local_suspension_control("q3"))
    assert _Worker.created == [] and not draft_store.is_approved(presented_draft.draft_id)  # nothing replays
    window._on_approve_draft_clicked(presented_draft.draft_id)
    QApplication.processEvents()
    assert draft_store.is_approved(presented_draft.draft_id)
    assert len(_Worker.created) == 1 and _Worker.created[0].user_input == "[Owner approved via button]"


def test_q3_the_suspension_dialog_resumes_with_staged_items_waiting(qapp, dialog, tools_tab):
    pending_actions.stage_action("reset_chat", {})
    _trip()
    d = dialog()
    d.resume_btn.click()
    assert "Resumed owner" in d.result_label.text()
    assert len(pending_actions._load_queue()) == 1 and tools_tab.agent.ctx.prompts == []
