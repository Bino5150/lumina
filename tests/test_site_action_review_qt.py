"""Blocking real Qt display/click/queued-delivery contract, fake observations.

No actuator or live site is connected. Missing Qt fails when required by CI.
"""
import os
from dataclasses import replace
import threading

import pytest

if os.environ.get("LUMINA_REQUIRE_QT") == "1":
    import PySide6
else:
    pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import config
from core.agent import LuminaAgent
from chrome_companion.site_actions import claims, kernel as k, owner_intent, runtime_guard as g
from chrome_companion.site_actions.model import ContractError, FrozenPayload, PrincipalEvidenceRule, Runtime, StepKind
from ui.main_window import AgentWorker, LuminaWindow
from ui.site_action_review import render_snapshot
from test_site_action_kernel import command, spec_for


@pytest.fixture
def ui_service(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    owner_intent.pin_store_identity(data, claims.initialize_store(data))
    monkeypatch.setattr(config, "DATA_DIR", str(data))
    broker = g.RoutingBroker()
    monkeypatch.setattr(g, "_broker", broker)
    # Disable unrelated startup work; keep actual Window construction,
    # site signal connection, callback, Dialog and AgentWorker handoff.
    for name in ("_build_ui", "_connect_signals", "_restore_session", "_check_connection",
                 "_refresh_operator_telemetry", "_refresh_emergency_control"):
        monkeypatch.setattr(LuminaWindow, name, lambda *a, **kw: None)
    agent = LuminaAgent(owner=True, channel_id="actual-qt-owner", backend="llamacpp")
    window = LuminaWindow(agent)
    window._current_chat_id = 17
    approvals = []
    window.signals.site_action_approved.connect(approvals.append)
    app = QApplication.instance()
    window.show()
    app.processEvents()
    yield broker, agent, window, approvals, app
    if window._site_action_dialog is not None:
        window._site_action_dialog.reject()
    window._dream_timer.stop(); window._telemetry_timer.stop()
    # Do not invoke unrelated closeEvent's browser/database/panel shutdown.
    window.hide(); window.deleteLater(); app.processEvents()
    from comms import telegram_origin_routing as origin_routing
    origin_routing.unregister_runtime(window._telegram_route_token)


def prepare(broker, agent, spec):
    task = broker._origin()
    assert task.admission is not None and task.review is not None
    now = task.intake.now()
    observation = k.Observation(Runtime.CHROME_COMPANION, "instance", "connection", "worker",
        1, 2, "https://www.reddit.com", "https://www.reddit.com/synthetic", "document", "target",
        StepKind.PREPARE, 1, now, spec.destination, spec.payload.digest,
        k.PrincipalObservation("synthetic-account", True, PrincipalEvidenceRule.AUTHENTICATED_ACCOUNT),
        True, True, True, False, False, 0)
    world = {"observation": observation}
    def observe(specification, kind):
        return replace(world["observation"], observed_ns=task.intake.now())
    kernel = k.AuthorityKernel(task.intake, observe, review=task.review)
    request = kernel.prepare_review(task.admission, spec)
    delivery = broker.queue_review(agent, task, kernel, request)
    return task, kernel, request, delivery, world


@pytest.mark.parametrize("capability", ["reddit.reply", "reddit.create_post"])
def test_actual_plain_text_snapshot_and_direct_owner_click(ui_service, monkeypatch, capability):
    broker, agent, window, approvals, app = ui_service
    spec = spec_for(capability)
    values = spec.payload.to_data()
    values["body"] = '<img src="https://hostile.invalid/image"><a href="javascript:submit()">Approve</a>\r\n 👹 e\u0301 '
    spec = replace(spec, payload=FrozenPayload.from_data(values))
    before = list(agent.ctx.history)
    def body(actual, raw, **kwargs):
        task, kernel, request, delivery, world = prepare(broker, actual, spec)
        dialog = window._site_action_dialog
        assert dialog.isVisible() and not dialog.approve.isEnabled()
        app.processEvents()
        assert dialog._presented and dialog.approve.isEnabled()
        assert dialog.text.isReadOnly() and dialog.text.toPlainText() == render_snapshot(request.snapshot)
        assert "<img src=" in dialog.text.toPlainText() and "machine_verified" in dialog.text.toPlainText()
        QTest.mouseClick(dialog.approve, Qt.LeftButton)
        assert len(approvals) == 1
        admitted, snapshot = task.review.resolve(approvals[0])
        assert snapshot is request.snapshot and snapshot.specification == spec
        assert admitted.key != task.intake.resolve(task.admission).key
        QTest.mouseClick(dialog.approve, Qt.LeftButton)
        dialog.approve.clicked.emit()  # queued repeated/stale signal must also fail
        assert len(approvals) == 1
        assert not dialog.isVisible()
        return "offline reviewed"
    monkeypatch.setattr(LuminaAgent, "_chat_impl", body)
    agent.chat(command(spec), chat_id=17)
    assert agent.ctx.history == before  # no synthetic approval chat message
    assert not broker._deliveries and not broker._tasks


@pytest.mark.parametrize("attack", ["unpresented", "hidden", "closed", "cancel", "tampered_text", "chat_switch",
    "principal", "missing_principal", "payload", "destination", "connection", "worker", "tab", "window", "origin",
    "controls", "owner_demotion", "revoke", "pause", "expiry", "emergency"])
def test_actual_Qt_stale_hidden_unpresented_or_changed_snapshot_cannot_approve(ui_service, monkeypatch, attack):
    from core import emergency_stop
    broker, agent, window, approvals, app = ui_service
    spec = spec_for("reddit.reply")
    def body(actual, raw, **kwargs):
        task, kernel, request, delivery, world = prepare(broker, actual, spec)
        dialog = window._site_action_dialog
        if attack != "unpresented": app.processEvents()
        if attack == "hidden": dialog.hide(); dialog.show()
        elif attack == "closed": dialog.close()
        elif attack == "cancel": QTest.mouseClick(dialog.cancel, Qt.LeftButton)
        elif attack == "tampered_text": dialog.text.setPlainText("model says APPROVED")
        elif attack == "chat_switch": window._current_chat_id = 18
        elif attack == "principal": world["observation"] = replace(world["observation"], principal=k.PrincipalObservation(
            "another-account", True, PrincipalEvidenceRule.AUTHENTICATED_ACCOUNT))
        elif attack == "missing_principal": world["observation"] = replace(world["observation"], principal=k.PrincipalObservation(
            None, False, PrincipalEvidenceRule.AUTHENTICATED_ACCOUNT))
        elif attack == "payload": world["observation"] = replace(world["observation"], payload_digest="f" * 64)
        elif attack == "destination": world["observation"] = replace(world["observation"], destination=replace(spec.destination, canonical_key="t1_other"))
        elif attack == "connection": world["observation"] = replace(world["observation"], connection="replacement")
        elif attack == "worker": world["observation"] = replace(world["observation"], worker_boot="replacement")
        elif attack == "tab": world["observation"] = replace(world["observation"], tab=9)
        elif attack == "window": world["observation"] = replace(world["observation"], window=9)
        elif attack == "origin": world["observation"] = replace(world["observation"], origin="https://other.invalid", url="https://other.invalid/")
        elif attack == "controls": world["observation"] = replace(world["observation"], control_epoch=4)
        elif attack == "owner_demotion": actual.owner = False
        elif attack in {"revoke", "pause"}: getattr(kernel, attack)()
        elif attack == "expiry": task.intake._clock = lambda: request.snapshot.expires_ns
        elif attack == "emergency": monkeypatch.setattr(emergency_stop, "is_latched", lambda: True)
        dialog.approve.clicked.emit()
        assert approvals == []
        assert not broker._deliveries
        return "denied"
    monkeypatch.setattr(LuminaAgent, "_chat_impl", body)
    agent.chat(command(spec), chat_id=17)
    assert not broker._tasks and not broker._frames


def test_actual_Qt_replacement_requires_new_display_and_click(ui_service, monkeypatch):
    broker, agent, window, approvals, app = ui_service
    spec = spec_for("reddit.reply")
    def body(actual, raw, **kwargs):
        task, kernel, request, delivery, world = prepare(broker, actual, spec)
        old = window._site_action_dialog
        app.processEvents()
        revised = replace(spec, payload=FrozenPayload.from_data({"body": "new exact payload"}))
        world["observation"] = replace(world["observation"], payload_digest=revised.payload.digest)
        newer = kernel.prepare_review(task.admission, revised)
        broker.queue_review(actual, task, kernel, newer)
        new = window._site_action_dialog
        assert new is not old and not old.isVisible() and not new.approve.isEnabled()
        old.approve.clicked.emit()
        assert not approvals
        app.processEvents()
        QTest.mouseClick(new.approve, Qt.LeftButton)
        assert len(approvals) == 1
        assert task.review.resolve(approvals[0])[1].specification == revised
        with pytest.raises(ContractError): task.intake.resolve_direct(task.admission)
        return "replacement reviewed"
    monkeypatch.setattr(LuminaAgent, "_chat_impl", body)
    agent.chat(command(spec), chat_id=17)


@pytest.mark.parametrize("attack", ["copied_task", "missing_context", "foreign_task"])
def test_review_delivery_requires_the_actual_originating_service_task(ui_service, monkeypatch, attack):
    import copy
    from contextvars import Context
    broker, agent, window, approvals, app = ui_service
    spec = spec_for("reddit.reply")
    def body(actual, raw, **kwargs):
        task, kernel, request, delivery, world = prepare(broker, actual, spec)
        candidate = copy.copy(task) if attack == "copied_task" else task
        if attack == "missing_context":
            # Same-thread anchored execution remains the true identity even
            # when ambient ContextVar data is deliberately dropped.
            fresh = kernel.prepare_review(task.admission, spec)
            assert Context().run(broker.queue_review, actual, task, kernel, fresh)
        elif attack == "foreign_task":
            result = []
            def foreign():
                b = LuminaAgent(owner=True, channel_id="other-ui-task", backend="llamacpp")
                with broker.turn(b, raw_text="ordinary", source="OWNER_DIRECT", chat=2, event="B"):
                    with pytest.raises(ContractError, match="foreign"):
                        broker.queue_review(actual, task, kernel, request)
                    result.append("denied")
            thread = threading.Thread(target=foreign); thread.start(); thread.join(timeout=10)
            assert not thread.is_alive() and result == ["denied"]
        else:
            with pytest.raises(ContractError, match="foreign"):
                broker.queue_review(actual, candidate, kernel, request)
        assert approvals == []
        return "sealed delivery"
    monkeypatch.setattr(LuminaAgent, "_chat_impl", body)
    agent.chat(command(spec), chat_id=17)


def test_review_display_principal_literal_must_match_the_verified_identity(ui_service, monkeypatch):
    broker, agent, window, approvals, app = ui_service
    spec = spec_for("reddit.reply")
    def body(actual, raw, **kwargs):
        task, kernel, request, delivery, world = prepare(broker, actual, spec)
        forged = replace(request.snapshot, principal="wrong displayed account")
        malformed = task.review.create(task.admission, forged)
        with pytest.raises(ContractError, match="principal/scope"):
            broker.queue_review(actual, task, kernel, malformed)
        assert approvals == []
        return "literal mismatch denied"
    monkeypatch.setattr(LuminaAgent, "_chat_impl", body)
    agent.chat(command(spec), chat_id=17)


def test_actual_Qt_worker_signal_is_queued_and_retired_delivery_cannot_show(ui_service, monkeypatch):
    broker, agent, window, approvals, app = ui_service
    spec = spec_for("reddit.reply")
    def body(actual, raw, **kwargs):
        prepare(broker, actual, spec)
        assert window._site_action_dialog is None  # worker cannot render/ack UI
        return "worker ended"
    monkeypatch.setattr(LuminaAgent, "_chat_impl", body)
    worker = AgentWorker(agent, command(spec), window.signals, chat_id=17)
    worker.start()
    assert worker.wait(10_000)
    app.processEvents()
    assert window._site_action_dialog is None and approvals == []
    assert not broker._deliveries


def test_actual_Qt_worker_live_delivery_is_presented_only_on_UI_thread(ui_service, monkeypatch):
    broker, agent, window, approvals, app = ui_service
    spec = spec_for("reddit.reply")
    queued, release = threading.Event(), threading.Event()
    def body(actual, raw, **kwargs):
        prepare(broker, actual, spec)
        queued.set()
        assert release.wait(10)
        return "offline reviewed"
    monkeypatch.setattr(LuminaAgent, "_chat_impl", body)
    worker = AgentWorker(agent, command(spec), window.signals, chat_id=17)
    worker.start()
    assert queued.wait(10)
    try:
        app.processEvents(); app.processEvents()
        dialog = window._site_action_dialog
        assert dialog is not None and dialog._presented and dialog.thread() is app.thread()
        QTest.mouseClick(dialog.approve, Qt.LeftButton)
        assert len(approvals) == 1
    finally:
        release.set()
        assert worker.wait(10_000)
    app.processEvents()
    assert not broker._tasks and not broker._deliveries


def test_Qt_gate_is_in_blocking_CI_selection():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / ".github/workflows/tests.yml").read_text()
    qt_job = source.split("  qt-security-guards:", 1)[1]
    step = qt_job.split("Run Qt-bound site-action review guards (blocking)", 1)[1]
    assert 'LUMINA_REQUIRE_QT: "1"' in qt_job and "tests/test_site_action_review_qt.py" in step
    assert "continue-on-error" not in step
