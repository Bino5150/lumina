import os
import threading
import types

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import ui.main_window as main_window
from core.agent import TurnCancelled
from ui.main_window import AgentWorker, LuminaWindow, StreamSignals


class _Widget:
    def __init__(self):
        self.notices = []
        self.turn_states = []

    def add_operator_message(self, text):
        self.notices.append(text)

    def set_turn_running(self, running):
        self.turn_states.append(running)


class _Label:
    def __init__(self):
        self.text = ""

    def setText(self, text):
        self.text = text


class _Worker:
    def __init__(self):
        self.requested = False

    def isRunning(self):
        return True

    def cancel_requested(self):
        return self.requested

    def request_cancel(self):
        if self.requested:
            return False
        self.requested = True
        return True


def _window_fake(worker=None):
    return types.SimpleNamespace(
        worker=worker,
        chat_widget=_Widget(),
        status_lbl=_Label(),
        _manual_compaction_thread=None,
        _manual_compaction_cancel=None,
        _btw_task_ids={"sidequest": {"question": "leave me alone"}},
    )


def test_stop_requests_only_foreground_worker_and_leaves_btw_tracking_untouched():
    worker = _Worker()
    fake = _window_fake(worker)

    LuminaWindow._command_stop(fake, "")

    assert worker.requested is True
    assert fake._btw_task_ids == {"sidequest": {"question": "leave me alone"}}
    assert fake.status_lbl.text == "stop requested..."
    assert "background tasks untouched" in fake.chat_widget.notices[-1]


def test_repeated_stop_is_idempotent():
    worker = _Worker()
    worker.requested = True
    fake = _window_fake(worker)

    LuminaWindow._command_stop(fake, "")

    assert "already requested" in fake.chat_widget.notices[-1]


class _AliveThread:
    def is_alive(self):
        return True


class _DeadThread:
    def is_alive(self):
        return False


def test_stop_can_cancel_manual_compaction_at_its_existing_safe_boundary():
    fake = _window_fake(worker=None)
    fake._manual_compaction_thread = _AliveThread()
    fake._manual_compaction_cancel = threading.Event()

    LuminaWindow._command_stop(fake, "")

    assert fake._manual_compaction_cancel.is_set()
    assert "requested for /compact" in fake.chat_widget.notices[-1]


def test_stop_does_not_claim_a_finalizing_compaction_was_cancelled():
    fake = _window_fake(worker=None)
    fake._manual_compaction_thread = _DeadThread()
    fake._manual_compaction_cancel = threading.Event()

    LuminaWindow._command_stop(fake, "")

    assert not fake._manual_compaction_cancel.is_set()
    assert "already finalizing" in fake.chat_widget.notices[-1]


def test_stop_with_no_current_operation_is_a_noop_notice():
    fake = _window_fake(worker=None)

    LuminaWindow._command_stop(fake, "")

    assert "no foreground turn or manual compaction" in fake.chat_widget.notices[-1]
    assert fake._btw_task_ids


class _Bubble:
    def __init__(self):
        self.finalized = False
        self.detached = False
        self.deleted = False

    def finalize(self):
        self.finalized = True

    def setParent(self, parent):
        self.detached = parent is None

    def deleteLater(self):
        self.deleted = True


def _cancel_completion_fake(partial=True):
    bubble = _Bubble()
    widget = _Widget()
    return types.SimpleNamespace(
        _live_bubble=bubble,
        chat_widget=widget,
        _operator_turn_started_at=123.0,
        _operator_phase="responding",
        _operator_current_tool=None,
        status_lbl=_Label(),
        _current_chat_id=7,
        _refresh_chat_list=lambda: None,
        _refresh_operator_telemetry=lambda refresh_context=False: None,
    ), bubble


def test_cancelled_partial_response_is_persisted_with_cancel_metadata(monkeypatch):
    writes = []
    monkeypatch.setattr(
        main_window, "save_chat_message",
        lambda chat_id, role, content, metadata=None: writes.append(
            (chat_id, role, content, metadata)
        ),
    )
    fake, bubble = _cancel_completion_fake()

    LuminaWindow._on_cancelled(fake, "partial answer")

    assert bubble.finalized is True
    assert writes == [(7, "assistant", "partial answer", {"cancelled": True})]
    assert fake.chat_widget.turn_states[-1] is False
    assert "partial response kept" in fake.chat_widget.notices[-1]


def test_cancelled_before_response_removes_empty_live_bubble_and_writes_nothing(monkeypatch):
    writes = []
    monkeypatch.setattr(
        main_window, "save_chat_message",
        lambda *args, **kwargs: writes.append((args, kwargs)),
    )
    fake, bubble = _cancel_completion_fake()

    LuminaWindow._on_cancelled(fake, "")

    assert bubble.detached is True and bubble.deleted is True
    assert writes == []
    assert "no assistant response was committed" in fake.chat_widget.notices[-1]


def test_agent_worker_routes_turn_cancelled_to_cancelled_signal_not_error():
    class _Agent:
        def chat(self, user_input, chat_id=None, cancel_event=None):
            raise TurnCancelled("partial")

    signals = StreamSignals()
    cancelled = []
    errors = []
    signals.cancelled.connect(cancelled.append)
    signals.error.connect(errors.append)
    worker = AgentWorker(_Agent(), "question", signals, chat_id=4)

    worker.run()

    assert cancelled == ["partial"]
    assert errors == []


def test_agent_worker_keeps_legacy_test_stub_compatibility_without_cancel_kwarg():
    calls = []

    class _Agent:
        on_tool_call = None
        on_tool_result = None
        on_think_start = None
        on_think_token = None
        on_think_end = None
        on_response_token = None

        def chat(self, user_input, chat_id=None):
            calls.append((user_input, chat_id))
            return "done"

    signals = StreamSignals()
    finished = []
    signals.finished.connect(finished.append)
    worker = AgentWorker(_Agent(), "question", signals, chat_id=9)

    worker.run()

    assert calls == [("question", 9)]
    assert finished == ["done"]
