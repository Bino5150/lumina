"""
tests/test_emergency_stop_maintenance.py -- Patch 3A.2 Part H: dream sweep
and automatic context-compaction execution leases.

Both maintenance paths spawn raw daemon threads from ui/main_window.py.
Before 3A.2 neither was represented by a Patch 3A.1 execution lease at
all, so an emergency latch couldn't truthfully claim to have stopped them.
These tests exercise core.dreaming.on_session_idle(expected_epoch=...)
directly (the dream-sweep half); the automatic-compaction half lives in
ui/main_window.py's _maybe_compact() and is covered by
tests/test_emergency_stop_ui.py instead, since it needs a LuminaWindow-
shaped fake the same way the rest of that file already does.
"""
import threading
import time

import pytest

import core.dreaming as dreaming
import core.persistence as persistence
from core import emergency_stop


@pytest.fixture(autouse=True)
def _clean_state():
    emergency_stop._reset_for_tests()
    dreaming._last_dream_sweep.clear()
    yield
    dreaming._last_dream_sweep.clear()
    emergency_stop._reset_for_tests()


class FakeBackend:
    def __init__(self, response_text="- Did a thing", gate: threading.Event = None,
                 release: threading.Event = None):
        self.response_text = response_text
        self.calls = []
        self._gate = gate      # set once the call is actually in-flight
        self._release = release  # blocks the call until released

    def complete_utility(self, prompt, prefill="", max_tokens=500, temperature=0.3):
        self.calls.append({"prompt": prompt, "prefill": prefill})
        if self._gate is not None:
            self._gate.set()
        if self._release is not None:
            assert self._release.wait(timeout=5)
        return self.response_text


def _enable_sweep(monkeypatch, min_tokens=1):
    monkeypatch.setattr(dreaming.config, "DREAM_SWEEP_ENABLED", True)
    monkeypatch.setattr(dreaming.config, "DREAM_MIN_TOKENS", min_tokens)
    monkeypatch.setattr(
        dreaming, "load_chat_messages",
        lambda cid: [{"role": "user", "content": "x" * 50, "created_at": "2026-07-09T00:00:00"}],
    )


# ---------------------------------------------------------------------------
# Compatibility: omitted expected_epoch behaves exactly as before 3A.2
# ---------------------------------------------------------------------------

def test_omitted_expected_epoch_runs_with_no_execution_lease(monkeypatch):
    _enable_sweep(monkeypatch)
    fake = FakeBackend()
    monkeypatch.setattr(dreaming, "get_llm_backend", lambda: fake)
    stored = {}
    monkeypatch.setattr(
        dreaming, "palace_store",
        lambda **kw: stored.update(kw),
    )

    assert emergency_stop.snapshot()["active_execution_count"] == 0
    dreaming.on_session_idle(chat_id=42)  # no expected_epoch at all

    assert stored["content"] == "- Did a thing"
    assert emergency_stop.snapshot()["active_execution_count"] == 0  # no lease was ever registered


# ---------------------------------------------------------------------------
# Admission failure: stale/latched epoch before the sweep starts at all
# ---------------------------------------------------------------------------

def test_stale_epoch_admits_no_work_at_all(monkeypatch):
    _enable_sweep(monkeypatch)
    fake = FakeBackend()
    monkeypatch.setattr(dreaming, "get_llm_backend", lambda: fake)
    monkeypatch.setattr(dreaming, "palace_store", lambda **kw: pytest.fail("must not write"))

    old_epoch = emergency_stop.current_epoch()
    emergency_stop.latch(source="test", reason="dream-sweep-stale")
    emergency_stop.rearm_local()

    dreaming.on_session_idle(chat_id=42, expected_epoch=old_epoch)

    assert fake.calls == []  # no summarizer call at all
    assert dreaming._last_dream_sweep == {}  # watermark never advanced


def test_latched_epoch_admits_no_work_at_all(monkeypatch):
    _enable_sweep(monkeypatch)
    fake = FakeBackend()
    monkeypatch.setattr(dreaming, "get_llm_backend", lambda: fake)
    monkeypatch.setattr(dreaming, "palace_store", lambda **kw: pytest.fail("must not write"))

    epoch = emergency_stop.current_epoch()
    emergency_stop.latch(source="test", reason="dream-sweep-latched")

    dreaming.on_session_idle(chat_id=42, expected_epoch=epoch)

    assert fake.calls == []
    emergency_stop.rearm_local()


# ---------------------------------------------------------------------------
# R8: dream lease active, summarizer blocked, latch, release -> zero write
# ---------------------------------------------------------------------------

def test_r8_latch_while_summarizer_blocked_discards_result_before_palace_write(monkeypatch):
    _enable_sweep(monkeypatch)
    gate = threading.Event()
    release = threading.Event()
    fake = FakeBackend(gate=gate, release=release)
    monkeypatch.setattr(dreaming, "get_llm_backend", lambda: fake)
    monkeypatch.setattr(dreaming, "palace_store", lambda **kw: pytest.fail("stale result must not be written"))

    epoch = emergency_stop.current_epoch()
    outcome = {}

    def _run():
        dreaming.on_session_idle(chat_id=42, expected_epoch=epoch)
        outcome["done"] = True

    t = threading.Thread(target=_run)
    t.start()
    assert gate.wait(timeout=5)  # summarizer call is genuinely in-flight

    assert emergency_stop.can_rearm() is False  # dream lease is active
    with pytest.raises(emergency_stop.RearmBlocked):
        emergency_stop.rearm_local()

    emergency_stop.latch(source="test", reason="r8-dream-blocked")
    release.set()
    t.join(timeout=5)

    assert outcome["done"] is True
    assert dreaming._last_dream_sweep == {}  # watermark not advanced
    assert emergency_stop.can_rearm() is True  # lease closed once the sweep returned
    assert emergency_stop.rearm_local() is True


# ---------------------------------------------------------------------------
# Profile curation blocked mid-call -> discarded before the prefs write
# ---------------------------------------------------------------------------

def test_latch_while_profile_curation_blocked_discards_output_before_save(monkeypatch):
    _enable_sweep(monkeypatch)
    monkeypatch.setattr(dreaming.config, "HUMAN_PROFILE_CURATION_ENABLED", True)

    summarizer = FakeBackend(response_text="- Did a thing")
    stored = {}
    monkeypatch.setattr(dreaming, "get_llm_backend", lambda: summarizer)
    monkeypatch.setattr(dreaming, "palace_store", lambda **kw: stored.update(kw))
    # dreaming.py imports load/save locally inside the function
    # (`from core.persistence import load as load_prefs, save as save_prefs`),
    # not at module level -- patch the real source module so that local
    # import picks up the fakes.
    monkeypatch.setattr(persistence, "load", lambda: {"human_bio": "", "human_profile_curated": ""})

    save_calls = []
    monkeypatch.setattr(persistence, "save", lambda prefs: save_calls.append(prefs))

    gate = threading.Event()
    release = threading.Event()

    def _curate(raw_text, bio, existing):
        gate.set()
        assert release.wait(timeout=5)
        return "- Curated note"

    monkeypatch.setattr(dreaming, "curate_human_profile", _curate)

    epoch = emergency_stop.current_epoch()
    outcome = {}

    def _run():
        dreaming.on_session_idle(chat_id=42, expected_epoch=epoch)
        outcome["done"] = True

    t = threading.Thread(target=_run)
    t.start()
    assert gate.wait(timeout=5)  # profile curation call is genuinely in-flight

    emergency_stop.latch(source="test", reason="profile-curation-blocked")
    release.set()
    t.join(timeout=5)

    assert outcome["done"] is True
    assert stored["content"] == "- Did a thing"  # Palace write happened before the latch
    assert save_calls == []  # curated prefs write discarded

    emergency_stop.rearm_local()
