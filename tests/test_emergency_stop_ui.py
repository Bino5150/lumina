"""
tests/test_emergency_stop_ui.py -- Patch 3A.2 Parts C/D/E/F/I: the local
OH SHIT control surface's UI-logic layer.

Same convention as tests/test_operator_stop_ui.py: every LuminaWindow
method under test is called UNBOUND against a minimal
types.SimpleNamespace stand-in, never against a real booted window --
these are logic/orchestration tests, not rendering tests. A full
offscreen-Qt smoke run (real window, real widgets, real button clicks,
real QMessageBox) is a separate script, not this file.
"""
import os
import threading
import time
import types

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import ui.main_window as main_window
from core import emergency_stop
from ui.main_window import LuminaWindow


@pytest.fixture(autouse=True)
def _clean_emergency_state():
    emergency_stop._reset_for_tests()
    yield
    emergency_stop._reset_for_tests()


class _Widget:
    def __init__(self):
        self.notices = []

    def add_operator_message(self, text):
        self.notices.append(text)


class _StatusBar:
    def __init__(self):
        self.calls = []  # (latched, rearm_ready, tooltip)

    def set_emergency_state(self, latched, rearm_ready, tooltip=""):
        self.calls.append((latched, rearm_ready, tooltip))


class _Worker:
    def __init__(self, running=True):
        self._running = running
        self.cancel_requested_calls = 0

    def isRunning(self):
        return self._running

    def request_cancel(self):
        self.cancel_requested_calls += 1
        return True


class _AliveThread:
    def is_alive(self):
        return True


def _window_fake(**overrides):
    base = dict(
        worker=None,
        chat_widget=_Widget(),
        status_bar=_StatusBar(),
        _manual_compaction_thread=None,
        _manual_compaction_cancel=None,
        _btw_task_ids={},
        _tracked_operator_task_ids=lambda: set(),
        agent=types.SimpleNamespace(
            _background_task_ids=set(),
            get_context_usage=lambda chat_id=None: {"used_tokens": 0, "max_tokens": 1, "percent": 0.0},
        ),
        _current_chat_id=None,
        _operator_turn_started_at=None,
        _operator_last_progress_at=None,
        _operator_phase="idle",
        _operator_current_tool=None,
        _manual_compaction_started_at=None,
    )
    base.update(overrides)
    fake = types.SimpleNamespace(**base)
    # These are pure functions of attributes _window_fake() already
    # provides (status_bar, worker, _manual_compaction_thread) -- wire the
    # real implementations rather than re-stubbing their logic, so tests
    # that call _trigger_emergency_stop()/_confirm_and_rearm()/
    # _command_status() exercise the real orchestration end to end.
    fake._emergency_rearm_ready = lambda: LuminaWindow._emergency_rearm_ready(fake)
    fake._emergency_snapshot_summary = lambda: LuminaWindow._emergency_snapshot_summary(fake)
    fake._emergency_status_lines = lambda: LuminaWindow._emergency_status_lines(fake)
    fake._emergency_tooltip = lambda: LuminaWindow._emergency_tooltip(fake)
    fake._refresh_emergency_control = lambda: LuminaWindow._refresh_emergency_control(fake)
    return fake


# ---------------------------------------------------------------------------
# R1 -- latch is the first security-relevant state change
# ---------------------------------------------------------------------------

def test_trigger_emergency_stop_latches_before_anything_else(monkeypatch):
    order = []

    real_latch = emergency_stop.latch
    monkeypatch.setattr(emergency_stop, "latch", lambda **kw: (order.append("latch"), real_latch(**kw))[1])

    worker = _Worker(running=True)
    real_request_cancel = worker.request_cancel
    worker.request_cancel = lambda: (order.append("worker_cancel"), real_request_cancel())[1]

    compaction_cancel = threading.Event()
    real_set = compaction_cancel.set
    compaction_cancel.set = lambda: (order.append("compaction_cancel"), real_set())[1]

    # cancel_all_scheduled is imported lazily inside the method (`from
    # core.task_queue import cancel_all_scheduled`) -- patch the real
    # source so that import picks up the recorder.
    import core.task_queue as task_queue_module
    monkeypatch.setattr(task_queue_module, "cancel_all_scheduled", lambda: (order.append("cancel_scheduled"), [])[1])

    import comms.telegram_bridge as bridge_module
    monkeypatch.setattr(bridge_module, "request_stop_bridge", lambda: (order.append("telegram_stop"), False)[1])

    fake = _window_fake(worker=worker, _manual_compaction_cancel=compaction_cancel)

    LuminaWindow._trigger_emergency_stop(fake, source="test")

    assert order[0] == "latch"
    assert order.index("latch") < order.index("worker_cancel")
    assert order.index("latch") < order.index("compaction_cancel")
    assert order.index("latch") < order.index("cancel_scheduled")
    assert order.index("latch") < order.index("telegram_stop")

    emergency_stop.rearm_local()


# ---------------------------------------------------------------------------
# _trigger_emergency_stop: fresh latch vs. idempotent repeat
# ---------------------------------------------------------------------------

def test_trigger_emergency_stop_fresh_latch_messaging_and_side_effects(monkeypatch):
    import core.task_queue as task_queue_module
    cancelled_calls = []
    monkeypatch.setattr(task_queue_module, "cancel_all_scheduled", lambda: cancelled_calls.append(1) or [])

    import comms.telegram_bridge as bridge_module
    stop_calls = []
    monkeypatch.setattr(bridge_module, "request_stop_bridge", lambda: stop_calls.append(1) or False)

    worker = _Worker(running=True)
    compaction_cancel = threading.Event()
    fake = _window_fake(worker=worker, _manual_compaction_cancel=compaction_cancel)

    LuminaWindow._trigger_emergency_stop(fake, source="test")

    assert worker.cancel_requested_calls == 1
    assert compaction_cancel.is_set()
    assert cancelled_calls == [1]
    assert stop_calls == [1]
    assert emergency_stop.is_latched() is True
    assert "EMERGENCY STOP ACTIVE" in fake.chat_widget.notices[-1]
    assert len(fake.status_bar.calls) >= 2  # repainted at least twice (step 2 and step 7)

    emergency_stop.rearm_local()


def test_trigger_emergency_stop_repeated_call_is_idempotent_and_says_already_latched(monkeypatch):
    import core.task_queue as task_queue_module
    monkeypatch.setattr(task_queue_module, "cancel_all_scheduled", lambda: [])
    import comms.telegram_bridge as bridge_module
    monkeypatch.setattr(bridge_module, "request_stop_bridge", lambda: False)
    monkeypatch.setattr(bridge_module, "is_running", lambda: False)

    fake = _window_fake()
    LuminaWindow._trigger_emergency_stop(fake, source="test")
    epoch_after_first = emergency_stop.current_epoch()

    LuminaWindow._trigger_emergency_stop(fake, source="test")

    assert emergency_stop.current_epoch() == epoch_after_first  # no second increment
    assert "already latched" in fake.chat_widget.notices[-1].lower()

    emergency_stop.rearm_local()


# ---------------------------------------------------------------------------
# Part F -- _emergency_rearm_ready()
# ---------------------------------------------------------------------------

def test_rearm_ready_false_when_kernel_not_latched_or_active():
    fake = _window_fake()
    # Not latched at all -- kernel can_rearm() is False (nothing to rearm)
    assert LuminaWindow._emergency_rearm_ready(fake) is False


def test_rearm_ready_false_while_worker_running(monkeypatch):
    import comms.telegram_bridge as bridge_module
    monkeypatch.setattr(bridge_module, "is_running", lambda: False)
    emergency_stop.latch(source="test", reason="rearm-ready-worker")

    fake = _window_fake(worker=_Worker(running=True))
    assert LuminaWindow._emergency_rearm_ready(fake) is False

    emergency_stop.rearm_local()


def test_rearm_ready_false_while_manual_compaction_thread_present(monkeypatch):
    import comms.telegram_bridge as bridge_module
    monkeypatch.setattr(bridge_module, "is_running", lambda: False)
    emergency_stop.latch(source="test", reason="rearm-ready-compaction")

    fake = _window_fake(_manual_compaction_thread=_AliveThread())
    assert LuminaWindow._emergency_rearm_ready(fake) is False

    emergency_stop.rearm_local()


def test_rearm_ready_false_while_telegram_running(monkeypatch):
    import comms.telegram_bridge as bridge_module
    monkeypatch.setattr(bridge_module, "is_running", lambda: True)
    emergency_stop.latch(source="test", reason="rearm-ready-telegram")

    fake = _window_fake()
    assert LuminaWindow._emergency_rearm_ready(fake) is False

    emergency_stop.rearm_local()


def test_rearm_ready_true_when_everything_clear(monkeypatch):
    import comms.telegram_bridge as bridge_module
    monkeypatch.setattr(bridge_module, "is_running", lambda: False)
    emergency_stop.latch(source="test", reason="rearm-ready-clear")

    fake = _window_fake()
    assert LuminaWindow._emergency_rearm_ready(fake) is True

    emergency_stop.rearm_local()


# ---------------------------------------------------------------------------
# Part E -- button click branching (second interaction is NOT a toggle)
# ---------------------------------------------------------------------------

def _click_fake(latched, rearm_ready):
    calls = []
    fake = types.SimpleNamespace(
        _trigger_emergency_stop=lambda source: calls.append(("trigger", source)),
        _emergency_rearm_ready=lambda: rearm_ready,
        _confirm_and_rearm=lambda: calls.append(("confirm",)),
    )
    return fake, calls


def test_click_while_unlatched_triggers_emergency_stop(monkeypatch):
    monkeypatch.setattr(emergency_stop, "is_latched", lambda: False)
    fake, calls = _click_fake(latched=False, rearm_ready=False)

    LuminaWindow._on_emergency_button_clicked(fake)

    assert calls == [("trigger", "gui_button")]


def test_click_while_latched_and_not_rearm_ready_repeats_trigger_never_rearms(monkeypatch):
    monkeypatch.setattr(emergency_stop, "is_latched", lambda: True)
    fake, calls = _click_fake(latched=True, rearm_ready=False)

    LuminaWindow._on_emergency_button_clicked(fake)

    assert calls == [("trigger", "gui_button")]
    assert ("confirm",) not in calls  # never opens the rearm confirmation


def test_click_while_latched_and_rearm_ready_opens_confirmation_not_trigger(monkeypatch):
    monkeypatch.setattr(emergency_stop, "is_latched", lambda: True)
    fake, calls = _click_fake(latched=True, rearm_ready=True)

    LuminaWindow._on_emergency_button_clicked(fake)

    assert calls == [("confirm",)]  # never re-triggers, never calls rearm_local() itself


# ---------------------------------------------------------------------------
# _confirm_and_rearm -- confirm rearms, cancel leaves latch untouched
# ---------------------------------------------------------------------------

def test_confirm_and_rearm_cancel_leaves_latch_set(monkeypatch):
    monkeypatch.setattr(main_window.QMessageBox, "question", lambda *a, **k: main_window.QMessageBox.No)
    emergency_stop.latch(source="test", reason="confirm-cancel")

    fake = _window_fake()
    LuminaWindow._confirm_and_rearm(fake)

    assert emergency_stop.is_latched() is True
    assert fake.chat_widget.notices == []  # no message emitted on cancel

    emergency_stop.rearm_local()


def test_confirm_and_rearm_confirm_rearms_and_notifies(monkeypatch):
    monkeypatch.setattr(main_window.QMessageBox, "question", lambda *a, **k: main_window.QMessageBox.Yes)
    emergency_stop.latch(source="test", reason="confirm-yes")

    fake = _window_fake()
    LuminaWindow._confirm_and_rearm(fake)

    assert emergency_stop.is_latched() is False
    assert "Re-armed" in fake.chat_widget.notices[-1]
    assert "Telegram remains off" in fake.chat_widget.notices[-1]


# ---------------------------------------------------------------------------
# Part D -- /stop all, /stop while latched, invalid argument
# ---------------------------------------------------------------------------

def test_command_stop_all_triggers_emergency_stop(monkeypatch):
    calls = []
    fake = types.SimpleNamespace(_trigger_emergency_stop=lambda source: calls.append(source))

    LuminaWindow._command_stop(fake, "all")

    assert calls == ["operator_command"]


def test_command_stop_all_case_insensitive(monkeypatch):
    calls = []
    fake = types.SimpleNamespace(_trigger_emergency_stop=lambda source: calls.append(source))

    LuminaWindow._command_stop(fake, "ALL")

    assert calls == ["operator_command"]


def test_command_stop_invalid_argument_is_a_usage_error():
    fake = _window_fake()
    LuminaWindow._command_stop(fake, "everything")
    assert "'all'" in fake.chat_widget.notices[-1]


def test_command_stop_no_argument_while_latched_reports_already_controlling(monkeypatch):
    emergency_stop.latch(source="test", reason="stop-no-arg-latched")
    fake = _window_fake()

    LuminaWindow._command_stop(fake, "")

    assert "already controlling" in fake.chat_widget.notices[-1]
    emergency_stop.rearm_local()


def test_command_stop_no_argument_while_not_latched_behaves_as_2b2():
    worker = _Worker(running=True)
    fake = _window_fake(worker=worker)
    fake.status_lbl = types.SimpleNamespace(setText=lambda t: None)

    LuminaWindow._command_stop(fake, "")

    assert worker.cancel_requested_calls == 1
    assert "foreground turn only" in fake.chat_widget.notices[-1]


# ---------------------------------------------------------------------------
# R12 -- latched input: /btw and /compact blocked, chat blocked
# ---------------------------------------------------------------------------

def test_command_btw_blocked_while_latched():
    emergency_stop.latch(source="test", reason="btw-blocked")
    fake = _window_fake()

    LuminaWindow._command_btw(fake, "what's the weather")

    assert "blocked" in fake.chat_widget.notices[-1].lower()
    assert fake._btw_task_ids == {}
    emergency_stop.rearm_local()


def test_command_compact_blocked_while_latched():
    emergency_stop.latch(source="test", reason="compact-blocked")
    fake = _window_fake()

    LuminaWindow._command_compact(fake, "")

    assert "blocked" in fake.chat_widget.notices[-1].lower()
    assert fake._manual_compaction_thread is None
    emergency_stop.rearm_local()


def test_on_user_message_blocked_while_latched_preserves_pending_image():
    emergency_stop.latch(source="test", reason="on-user-message-blocked")
    fake = _window_fake()
    fake._pending_image = ("path.png", "b64data", "image/png")
    fake._pending_audio = None
    fake.input = types.SimpleNamespace(toPlainText=lambda: "", setPlainText=lambda t: None)

    LuminaWindow._on_user_message(fake, "hello while stopped")

    assert "blocked" in fake.chat_widget.notices[-1].lower()
    assert fake._pending_image == ("path.png", "b64data", "image/png")  # untouched
    emergency_stop.rearm_local()


# ---------------------------------------------------------------------------
# Part I -- /status emergency block
# ---------------------------------------------------------------------------

def test_command_status_includes_emergency_block_while_latched(monkeypatch):
    import comms.telegram_bridge as bridge_module
    monkeypatch.setattr(bridge_module, "is_running", lambda: False)
    emergency_stop.latch(source="test", reason="status-block")

    fake = _window_fake()
    LuminaWindow._command_status(fake, "")

    text = fake.chat_widget.notices[-1]
    assert "E-stop: ACTIVE" in text
    assert "Re-arm:" in text
    assert "Telegram:" in text

    emergency_stop.rearm_local()


def test_command_status_no_emergency_block_when_not_latched():
    fake = _window_fake()
    LuminaWindow._command_status(fake, "")

    text = fake.chat_widget.notices[-1]
    assert "E-stop" not in text


# ---------------------------------------------------------------------------
# Part H (auto-compaction) / R9 -- epoch captured before the daemon thread
# starts; admission failure preserves the pending batch untouched; a
# mid-flight latch discards the stale summary before the Palace write.
# ---------------------------------------------------------------------------

class _FakeCompactionCtx:
    def __init__(self, batch):
        self._compacting = False
        self._batch = batch
        self.taken = False
        self.restored_batches = []

    def pending_compaction_tokens(self):
        return 999999  # always over threshold

    def take_pending_compaction(self):
        self.taken = True
        batch, self._batch = self._batch, []
        return batch

    def restore_pending_compaction(self, batch):
        self.restored_batches.append(batch)
        self._batch = list(batch) + self._batch


def test_maybe_compact_admission_failure_never_takes_the_pending_batch(monkeypatch):
    monkeypatch.setattr(main_window.config, "CONTEXT_COMPACTION_ENABLED", True)
    monkeypatch.setattr(main_window.config, "CONTEXT_COMPACTION_BATCH_TOKENS", 1)

    ctx = _FakeCompactionCtx(batch=[{"role": "user", "content": "hello"}])
    fake = types.SimpleNamespace(agent=types.SimpleNamespace(ctx=ctx))

    emergency_stop.latch(source="test", reason="maybe-compact-admission")

    LuminaWindow._maybe_compact(fake, chat_id=42)
    time.sleep(0.2)  # let the daemon thread's except/finally actually run

    assert ctx.taken is False  # batch never taken -- still sitting in ctx._pending_compaction
    assert ctx.restored_batches == []  # nothing to restore -- it was never drained
    assert ctx._compacting is False  # cleanup still ran despite the admission failure

    emergency_stop.rearm_local()


def test_maybe_compact_mid_flight_latch_discards_stale_summary_before_write(monkeypatch):
    """R9-corrective: latch while the summarizer call is blocked -- zero
    Palace write from the stale output, AND the drained batch is restored
    to ctx._pending_compaction via restore_pending_compaction() rather than
    permanently lost. Supersedes the original 3A.2 checkpoint's documented
    data-loss gap now that core/context.py has a restore primitive and
    _maybe_compact()'s worker is transactional."""
    monkeypatch.setattr(main_window.config, "CONTEXT_COMPACTION_ENABLED", True)
    monkeypatch.setattr(main_window.config, "CONTEXT_COMPACTION_BATCH_TOKENS", 1)

    ctx = _FakeCompactionCtx(batch=[{"role": "user", "content": "hello"}])
    fake = types.SimpleNamespace(agent=types.SimpleNamespace(ctx=ctx))

    gate = threading.Event()
    release = threading.Event()

    def _slow_summarize(raw_text, prompt=None, max_tokens=300):
        gate.set()
        assert release.wait(timeout=5)
        return "- stale summary"

    # _maybe_compact()'s _run() closure imports both locally
    # (`from core.dreaming import run_summarization_call` /
    # `from tools.palace import palace_store`), not at main_window module
    # level -- patch the real source modules so those imports pick up fakes.
    import core.dreaming as dreaming_module
    monkeypatch.setattr(dreaming_module, "run_summarization_call", _slow_summarize)
    import tools.palace as palace_module
    palace_calls = []
    monkeypatch.setattr(palace_module, "palace_store", lambda **kw: palace_calls.append(kw))

    LuminaWindow._maybe_compact(fake, chat_id=42)
    assert gate.wait(timeout=5)

    assert ctx.taken is True  # batch already drained by this point
    assert emergency_stop.can_rearm() is False  # auto_compaction lease active

    emergency_stop.latch(source="test", reason="maybe-compact-mid-flight")
    release.set()

    # Poll on ctx._compacting rather than can_rearm(): the lease releases
    # (unblocking can_rearm()) a moment BEFORE the outer finally's restore
    # runs, so waiting on _compacting instead guarantees restore has
    # actually completed before the assertions below run.
    for _ in range(50):
        if not ctx._compacting:
            break
        time.sleep(0.02)

    assert palace_calls == []  # stale output correctly discarded before the write
    assert ctx._compacting is False
    assert ctx.restored_batches == [[{"role": "user", "content": "hello"}]]
    assert ctx._batch == [{"role": "user", "content": "hello"}]  # given back, not lost

    emergency_stop.rearm_local()


def test_maybe_compact_empty_summary_restores_batch_without_writing(monkeypatch):
    """No E-stop involved at all -- the summarizer just returns nothing
    useful. The drained batch must still come back rather than vanish."""
    monkeypatch.setattr(main_window.config, "CONTEXT_COMPACTION_ENABLED", True)
    monkeypatch.setattr(main_window.config, "CONTEXT_COMPACTION_BATCH_TOKENS", 1)

    batch_in = [{"role": "user", "content": "hello"}]
    ctx = _FakeCompactionCtx(batch=list(batch_in))
    fake = types.SimpleNamespace(agent=types.SimpleNamespace(ctx=ctx))

    import core.dreaming as dreaming_module
    monkeypatch.setattr(
        dreaming_module, "run_summarization_call",
        lambda raw_text, prompt=None, max_tokens=300: "",
    )
    import tools.palace as palace_module
    palace_calls = []
    monkeypatch.setattr(palace_module, "palace_store", lambda **kw: palace_calls.append(kw))

    LuminaWindow._maybe_compact(fake, chat_id=42)
    for _ in range(50):
        if not ctx._compacting:
            break
        time.sleep(0.02)

    assert ctx.taken is True
    assert palace_calls == []
    assert ctx.restored_batches == [batch_in]
    assert ctx._batch == batch_in
    assert ctx._compacting is False


def test_maybe_compact_palace_write_failure_restores_batch(monkeypatch):
    """Summarizer succeeds, but the Palace write itself raises -- the batch
    must not be silently consumed; it comes back for the next attempt. The
    _run() worker deliberately does not swallow this exception (matching
    the pre-existing behavior for every failure mode besides
    EmergencyStopError): it's expected to propagate out to the thread's
    exception hook, which is exactly the "not silent" outcome the spec
    asks for. Captured here via a local threading.excepthook override
    (restored in finally) so the test can assert on it directly instead of
    letting it fall through to pytest's own session-level unhandled-thread
    -exception reporting."""
    monkeypatch.setattr(main_window.config, "CONTEXT_COMPACTION_ENABLED", True)
    monkeypatch.setattr(main_window.config, "CONTEXT_COMPACTION_BATCH_TOKENS", 1)

    batch_in = [{"role": "user", "content": "hello"}]
    ctx = _FakeCompactionCtx(batch=list(batch_in))
    fake = types.SimpleNamespace(agent=types.SimpleNamespace(ctx=ctx))

    import core.dreaming as dreaming_module
    monkeypatch.setattr(
        dreaming_module, "run_summarization_call",
        lambda raw_text, prompt=None, max_tokens=300: "- a summary",
    )
    import tools.palace as palace_module

    def _boom(**kw):
        raise RuntimeError("palace write failed")

    monkeypatch.setattr(palace_module, "palace_store", _boom)

    caught = []
    original_hook = threading.excepthook
    threading.excepthook = lambda args: caught.append(args)
    try:
        LuminaWindow._maybe_compact(fake, chat_id=42)
        for _ in range(50):
            if not ctx._compacting:
                break
            time.sleep(0.02)
    finally:
        threading.excepthook = original_hook

    assert len(caught) == 1
    assert isinstance(caught[0].exc_value, RuntimeError)  # genuinely propagated, not swallowed
    assert ctx.taken is True
    assert ctx.restored_batches == [batch_in]  # not silently consumed by the failed write
    assert ctx._batch == batch_in
    assert ctx._compacting is False


def test_maybe_compact_success_commits_without_restoring_and_leaves_newer_pending_untouched(monkeypatch):
    """Full success path: original batch is consumed exactly once (never
    restored, never duplicated), and any newer messages the trim loop
    captured while summarization of the OLD batch was still in flight
    remain pending normally, untouched by the commit."""
    monkeypatch.setattr(main_window.config, "CONTEXT_COMPACTION_ENABLED", True)
    monkeypatch.setattr(main_window.config, "CONTEXT_COMPACTION_BATCH_TOKENS", 1)

    ctx = _FakeCompactionCtx(batch=[{"role": "user", "content": "hello"}])
    fake = types.SimpleNamespace(agent=types.SimpleNamespace(ctx=ctx))

    gate = threading.Event()
    release = threading.Event()

    def _slow_summarize(raw_text, prompt=None, max_tokens=300):
        gate.set()
        assert release.wait(timeout=5)
        return "- a real summary"

    import core.dreaming as dreaming_module
    monkeypatch.setattr(dreaming_module, "run_summarization_call", _slow_summarize)
    import tools.palace as palace_module
    palace_calls = []
    monkeypatch.setattr(palace_module, "palace_store", lambda **kw: palace_calls.append(kw))

    LuminaWindow._maybe_compact(fake, chat_id=42)
    assert gate.wait(timeout=5)
    assert ctx.taken is True

    # A newer message trims into the pending buffer while the OLD batch's
    # summarization is still in flight.
    ctx._batch.append({"role": "user", "content": "newer"})

    release.set()
    for _ in range(50):
        if not ctx._compacting:
            break
        time.sleep(0.02)

    assert len(palace_calls) == 1
    assert ctx.restored_batches == []  # committed -- never restored
    assert ctx._batch == [{"role": "user", "content": "newer"}]  # untouched, not duplicated
    assert ctx._compacting is False
