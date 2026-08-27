"""DREAM-LIFECYCLE-01 — Dream idle admission/latch lifecycle regressions.

Defect (confirmed by source-vet + reproduction): MainWindow's
_dream_fired_this_idle boolean conflates two distinct lifecycle concepts:

  SWEEP IN FLIGHT        — do not launch a second concurrent Dream worker.
  IDLE OPPORTUNITY CONSUMED — this idle window's admission contract is
                             satisfied; no further Dream admission needed.

Because the latch is set BEFORE any Dream-core eligibility runs, an
ineligible / failed / cancelled probe permanently masquerades as a
completed sweep for the rest of the idle window. Eligibility CAN
legitimately change mid-window (runtime DREAM_SWEEP_ENABLED toggle from
Settings, transient provider failure recovery, emergency-stop release),
so the consumed window silently drops a sweep that would have been
admitted exactly once.

These tests drive the real production trigger path
(LuminaWindow._check_dream_idle -> dreaming.on_session_idle) unbound
against a SimpleNamespace window, same convention as
test_emergency_stop_ui.py. Thread spawns are recorded by a fake Thread
class; synchronous mode (start() runs the target inline, exceptions
captured like a real daemon thread's isolated failure) gives full
determinism for state assertions; real-thread mode with Event barriers
proves single-flight under actual concurrency.
"""
import threading
import time
import types

import pytest

import core.dreaming as dreaming
import ui.main_window as main_window
from ui.main_window import LuminaWindow

# Captured BEFORE any test patches threading.Thread — the fake uses this
# to spawn real threads for the concurrency test.
_RealThread = threading.Thread


# ── harness ──────────────────────────────────────────────────────────────

class FakeBackend:
    """Records complete_utility calls; configurable result/raise."""

    def __init__(self, response_text="- User decided on Y", raises=False):
        self.response_text = response_text
        self.raises = raises
        self.calls = []

    def complete_utility(self, prompt, prefill="", max_tokens=500, temperature=0.3):
        self.calls.append({
            "prompt": prompt, "prefill": prefill,
            "max_tokens": max_tokens, "temperature": temperature,
        })
        if self.raises:
            return None
        return self.response_text


def make_window(**over):
    """Window stand-in carrying BOTH flag generations so the pre-repair
    code path runs and fails on behavior, not on AttributeError."""
    base = dict(
        _dream_fired_this_idle=False,   # pre-repair latch (dead post-repair)
        _dream_sweep_in_flight=False,   # DREAM-LIFECYCLE-01: worker-lifetime
        _dream_idle_consumed=False,     # DREAM-LIFECYCLE-01: window-scoped
        _current_chat_id=42,
        _last_activity=time.time() - 10_000,  # idle gate already passed
        worker=None,
    )
    base.update(over)
    w = types.SimpleNamespace(**base)
    # Unbound-method harness: the fake window carries the REAL lifecycle
    # methods, bound to itself, exactly as _check_dream_idle resolves them.
    w._run_dream_sweep = lambda chat_id, epoch, _w=w: LuminaWindow._run_dream_sweep(_w, chat_id, epoch)
    w._reset_dream_window_state = lambda _w=w: LuminaWindow._reset_dream_window_state(_w)
    return w


def make_thread_factory(run_real=False):
    """Returns (RecordingThread, created). run_real=False: start() runs the
    target synchronously on the caller thread (deterministic final state;
    exceptions captured, mirroring a real daemon thread's isolated death).
    run_real=True: start() launches a real thread (for Event-barrier
    concurrency proofs)."""
    created = []

    class RecordingThread:
        def __init__(self, target=None, args=(), daemon=False, **kw):
            self._target = target
            self._args = args
            self.errors = []
            self._real = (
                _RealThread(target=target, args=args, daemon=daemon)
                if run_real else None
            )
            created.append(self)

        def start(self):
            if self._real is not None:
                self._real.start()
                return
            try:
                self._target(*self._args)
            except BaseException as e:  # real threads die isolated
                self.errors.append(e)

        def join(self, timeout=None):
            if self._real is not None:
                self._real.join(timeout)

        def is_alive(self):
            return bool(self._real is not None and self._real.is_alive())

    return RecordingThread, created


def tick(window):
    """One timer tick through the real production entry point."""
    LuminaWindow._check_dream_idle(window)


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    dreaming._last_dream_sweep.clear()
    yield
    dreaming._last_dream_sweep.clear()


@pytest.fixture
def sweep_env(monkeypatch):
    """Standard eligible-sweep environment: enabled, above token threshold,
    one message, fake backend, recorded palace/curation calls."""
    monkeypatch.setattr(dreaming.config, "DREAM_SWEEP_ENABLED", True)
    monkeypatch.setattr(dreaming.config, "DREAM_MIN_TOKENS", 1)
    monkeypatch.setattr(dreaming.config, "HUMAN_PROFILE_CURATION_ENABLED", False)
    monkeypatch.setattr(
        dreaming, "load_chat_messages",
        lambda cid: [{"role": "user", "content": "x" * 50,
                      "created_at": "2026-07-09T00:00:00"}],
    )
    env = types.SimpleNamespace(
        backend=FakeBackend(),
        palace_calls=[],
        curate_calls=[],
        order=[],
    )

    def fake_palace_store(**kw):
        env.order.append("palace")
        env.palace_calls.append(kw)

    def fake_curate(*a, **k):
        env.order.append("curate")
        env.curate_calls.append(a)
        return "- Updated note"

    monkeypatch.setattr(dreaming, "get_llm_backend", lambda: env.backend)
    monkeypatch.setattr(dreaming, "palace_store", fake_palace_store)
    monkeypatch.setattr(dreaming, "curate_human_profile", fake_curate)
    return env


# ── A. ineligible first idle check must not consume the window ──────────

def test_ineligible_sweep_does_not_consume_idle_window(monkeypatch, sweep_env):
    """THE defect: an under-threshold probe returns ineligible, and the
    window must remain admit-able — the next tick re-admits. Pre-repair
    the latch burns the window: only one spawn ever happens."""
    monkeypatch.setattr(dreaming.config, "DREAM_MIN_TOKENS", 800)  # ineligible
    RecordingThread, created = make_thread_factory(run_real=False)
    monkeypatch.setattr(threading, "Thread", RecordingThread)
    w = make_window()

    tick(w)
    assert len(created) == 1
    # lifecycle state must be truthful: nothing in flight, nothing consumed
    assert w._dream_sweep_in_flight is False
    assert w._dream_idle_consumed is False
    # watermark untouched by an ineligible probe
    assert 42 not in dreaming._last_dream_sweep

    tick(w)  # same idle window, second tick
    assert len(created) == 2  # re-admitted — pre-repair this stays 1


# ── B. later eligibility in the same window admitted exactly once ───────

def test_later_eligibility_same_window_admitted_exactly_once(monkeypatch, sweep_env):
    """Eligibility CAN change mid-window (here: Dream enabled at runtime via
    Settings). First tick ineligible, second tick eligible -> admitted, and
    after completion the window is consumed (no third spawn)."""
    monkeypatch.setattr(dreaming.config, "DREAM_SWEEP_ENABLED", False)
    RecordingThread, created = make_thread_factory(run_real=False)
    monkeypatch.setattr(threading, "Thread", RecordingThread)
    w = make_window()

    tick(w)  # sweep disabled -> ineligible probe
    assert len(created) == 1
    assert w._dream_idle_consumed is False

    # user flips Dream on in Settings mid-window (general_tab writes config live)
    monkeypatch.setattr(dreaming.config, "DREAM_SWEEP_ENABLED", True)
    tick(w)
    assert len(created) == 2
    assert w._dream_idle_consumed is True  # validly consumed by a real sweep

    tick(w)  # exactly once — no re-admission after completion
    assert len(created) == 2


# ── C. successful sweep: one worker, write, watermark, consumed ─────────

def test_successful_sweep_sets_consumed_and_advances_watermark(monkeypatch, sweep_env):
    RecordingThread, created = make_thread_factory(run_real=False)
    monkeypatch.setattr(threading, "Thread", RecordingThread)
    w = make_window()

    tick(w)

    assert len(created) == 1
    assert len(sweep_env.palace_calls) == 1
    assert sweep_env.palace_calls[0]["wing"] == "nightstand"
    assert 42 in dreaming._last_dream_sweep  # watermark advanced
    assert w._dream_idle_consumed is True    # window validly consumed
    assert w._dream_sweep_in_flight is False
    tick(w)
    assert len(created) == 1  # consumed -> no further admission


# ── D. utility failure: no watermark, lifecycle recovers ────────────────

def test_utility_failure_no_watermark_and_retry_admitted(monkeypatch, sweep_env):
    sweep_env.backend.raises = True  # complete_utility -> None
    RecordingThread, created = make_thread_factory(run_real=False)
    monkeypatch.setattr(threading, "Thread", RecordingThread)
    w = make_window()

    tick(w)
    assert len(created) == 1
    assert 42 not in dreaming._last_dream_sweep   # watermark truth
    assert w._dream_idle_consumed is False        # failure is not consumption
    assert w._dream_sweep_in_flight is False      # not stuck

    tick(w)  # transient provider failure -> next tick retries
    assert len(created) == 2


# ── E. palace failure: no false completion/watermark, recoverable ───────

def test_palace_failure_no_false_completion(monkeypatch, sweep_env):
    def exploding_palace(**kw):
        raise RuntimeError("palace db locked")
    monkeypatch.setattr(dreaming, "palace_store", exploding_palace)
    RecordingThread, created = make_thread_factory(run_real=False)
    monkeypatch.setattr(threading, "Thread", RecordingThread)
    w = make_window()

    tick(w)
    assert 42 not in dreaming._last_dream_sweep   # no false watermark
    assert w._dream_idle_consumed is False        # not masquerading as done
    assert w._dream_sweep_in_flight is False      # no stuck in-flight flag

    tick(w)  # recoverable: re-admitted next tick
    assert len(created) == 2


# ── F. concurrent timer ticks: exactly one worker ───────────────────────

def test_concurrent_timer_ticks_launch_exactly_one_worker(monkeypatch, sweep_env):
    """Real-thread + Event barrier: while sweep one is in flight, tick B
    must not launch a second worker. Deterministic — no sleeps."""
    release = threading.Event()
    admitted = threading.Event()

    def blocking_sweep(chat_id, expected_epoch=None):
        admitted.set()
        release.wait(10)
        return dreaming.DREAM_COMPLETED

    monkeypatch.setattr(dreaming, "on_session_idle", blocking_sweep)
    RecordingThread, created = make_thread_factory(run_real=True)
    monkeypatch.setattr(threading, "Thread", RecordingThread)
    w = make_window()

    tick(w)                       # tick A admits; worker blocks mid-flight
    assert admitted.wait(10)
    tick(w)                       # tick B during flight
    assert len(created) == 1      # exactly one worker — no duplicate

    release.set()
    created[0].join(10)
    assert w._dream_sweep_in_flight is False
    assert w._dream_idle_consumed is True


# ── G. user-turn reset clears only window-scoped state ──────────────────

def test_user_turn_reset_clears_only_window_scoped_state():
    """The turn-start reset clears idle-window admission state; an in-flight
    sweep worker's single-flight flag must survive the turn boundary until
    the worker itself finishes."""
    w = make_window(_dream_idle_consumed=True, _dream_sweep_in_flight=True)
    LuminaWindow._reset_dream_window_state(w)
    assert w._dream_idle_consumed is False
    assert w._dream_sweep_in_flight is True  # worker-scoped: untouched


def test_fresh_window_starts_with_clean_lifecycle_state():
    """Both lifecycle flags initialize False on window construction."""
    import inspect
    src = inspect.getsource(main_window.LuminaWindow.__init__)
    assert "_dream_sweep_in_flight = False" in src
    assert "_dream_idle_consumed = False" in src


# ── H. emergency cancel: no stuck lifecycle state ───────────────────────

def test_emergency_cancel_leaves_no_stuck_state(monkeypatch, sweep_env):
    """Epoch latched before admission -> sweep cancelled. Lifecycle must
    stay recoverable: in-flight cleared, window not consumed, next tick
    re-admits (and re-cancels while still latched — cheap, no writes)."""
    from core import emergency_stop
    emergency_stop._reset_for_tests()
    emergency_stop.latch(source="test", reason="unit")
    try:
        RecordingThread, created = make_thread_factory(run_real=False)
        monkeypatch.setattr(threading, "Thread", RecordingThread)
        w = make_window()

        tick(w)
        assert len(created) == 1
        assert w._dream_sweep_in_flight is False   # not stuck
        assert w._dream_idle_consumed is False     # cancellation is not consumption
        assert len(sweep_env.palace_calls) == 0    # no writes while latched

        tick(w)  # still re-admittable (re-cancels, still no stuck state)
        assert len(created) == 2
        assert w._dream_sweep_in_flight is False
    finally:
        emergency_stop._reset_for_tests()


# ── I. curation composition ─────────────────────────────────────────────

def test_curation_runs_exactly_once_after_palace_on_success(monkeypatch, sweep_env):
    monkeypatch.setattr(dreaming.config, "HUMAN_PROFILE_CURATION_ENABLED", True)
    RecordingThread, created = make_thread_factory(run_real=False)
    monkeypatch.setattr(threading, "Thread", RecordingThread)
    w = make_window()

    tick(w)

    assert len(sweep_env.curate_calls) == 1            # exactly once
    assert sweep_env.order == ["palace", "curate"]     # after palace write
    assert 42 in dreaming._last_dream_sweep            # watermark after both


def test_curation_never_runs_without_valid_dream_cycle(monkeypatch, sweep_env):
    """Ineligible and failed sweeps must not reach curation."""
    monkeypatch.setattr(dreaming.config, "HUMAN_PROFILE_CURATION_ENABLED", True)
    RecordingThread, created = make_thread_factory(run_real=False)
    monkeypatch.setattr(threading, "Thread", RecordingThread)

    # ineligible (below tokens)
    monkeypatch.setattr(dreaming.config, "DREAM_MIN_TOKENS", 800)
    w = make_window()
    tick(w)
    assert sweep_env.curate_calls == []

    # failed (backend None), eligible tokens again
    monkeypatch.setattr(dreaming.config, "DREAM_MIN_TOKENS", 1)
    sweep_env.backend.raises = True
    tick(w)
    assert sweep_env.curate_calls == []
    assert sweep_env.palace_calls == []


# ── UTILITY-RUNTIME-01 integration controls ─────────────────────────────

def test_dream_sweep_reaches_complete_utility_provider_neutral_route(monkeypatch, sweep_env):
    """The sweep must reach the provider-neutral utility contract
    (run_summarization_call -> complete_utility), never a bypass."""
    RecordingThread, created = make_thread_factory(run_real=False)
    monkeypatch.setattr(threading, "Thread", RecordingThread)
    w = make_window()

    tick(w)

    assert len(sweep_env.backend.calls) >= 1           # reached complete_utility
    assert sweep_env.backend.calls[0]["prefill"] == "SUMMARY:"


def test_early_gates_consume_nothing(monkeypatch, sweep_env):
    """Worker-busy and below-idle-threshold ticks must not spawn, consume,
    or touch any lifecycle state (preserves the documented gate-3/4
    no-consume behavior)."""
    RecordingThread, created = make_thread_factory(run_real=False)
    monkeypatch.setattr(threading, "Thread", RecordingThread)

    # gate 3: agent worker mid-turn
    busy = make_window(worker=types.SimpleNamespace(isRunning=lambda: True))
    tick(busy)
    assert len(created) == 0
    assert busy._dream_idle_consumed is False
    assert busy._dream_sweep_in_flight is False

    # gate 4: idle duration below threshold
    fresh = make_window(_last_activity=time.time())
    tick(fresh)
    assert len(created) == 0
    assert fresh._dream_idle_consumed is False
    assert fresh._dream_sweep_in_flight is False


# ── dreaming-level structured outcomes ──────────────────────────────────

def test_on_session_idle_returns_structured_outcomes(monkeypatch, sweep_env):
    """on_session_idle exposes its lifecycle outcome so the UI wrapper can
    distinguish consumed from retry-worthy."""
    # ineligible: disabled
    monkeypatch.setattr(dreaming.config, "DREAM_SWEEP_ENABLED", False)
    assert dreaming.on_session_idle(chat_id=42) == dreaming.DREAM_INELIGIBLE

    # ineligible: below tokens
    monkeypatch.setattr(dreaming.config, "DREAM_SWEEP_ENABLED", True)
    monkeypatch.setattr(dreaming.config, "DREAM_MIN_TOKENS", 800)
    assert dreaming.on_session_idle(chat_id=42) == dreaming.DREAM_INELIGIBLE

    # failed: backend None
    monkeypatch.setattr(dreaming.config, "DREAM_MIN_TOKENS", 1)
    sweep_env.backend.raises = True
    assert dreaming.on_session_idle(chat_id=42) == dreaming.DREAM_FAILED

    # completed
    sweep_env.backend.raises = False
    assert dreaming.on_session_idle(chat_id=42) == dreaming.DREAM_COMPLETED
    assert 42 in dreaming._last_dream_sweep
