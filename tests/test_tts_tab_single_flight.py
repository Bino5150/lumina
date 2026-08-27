"""
Patch 3A.3A Part B/C, R2-R8 -- proves TTSTab's Test/Save single-flight gate
actually prevents overlapping backend reloads, with truthful state feedback
and no stranded UI on failure.

Real TTSTab, real threading, real cross-thread Qt signal marshaling -- only
tts.loader.get_tts_backend and core.persistence.save are faked/spied so
these run instantly, offscreen, with no GPU/network/model dependency, and
so each race can be deterministically held open and released on demand.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import threading
import time
import types

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

import config
import core.secrets as secrets_module
import tts.loader as loader_module
from core import persistence
from ui.main_window import COLORS
from ui.settings.tts_tab import TTSTab


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def _config_snapshot():
    keys = [
        "TTS_ENABLED", "TTS_BACKEND", "TTS_HOST", "VOICEBOX_HOST",
        "VOICEBOX_PROFILE", "ELEVENLABS_API_KEY",
        "STT_ENABLED", "STT_BACKEND", "STT_MODEL", "STT_DEVICE",
    ]
    original = {k: getattr(config, k, None) for k in keys}
    yield
    for k, v in original.items():
        setattr(config, k, v)


@pytest.fixture
def tmp_prefs(monkeypatch):
    tmp_dir = tempfile.mkdtemp()
    monkeypatch.setattr(persistence, "PREFS_PATH", os.path.join(tmp_dir, "prefs.json"))
    return tmp_dir


class _FakeTTSBackend:
    def __init__(self):
        self.enabled = False
        self.spoken = []

    def test(self):
        return True

    def speak(self, text, blocking=False):
        self.spoken.append(text)


def _pump_until(condition, timeout=5.0, interval=0.005):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if condition():
            return True
        time.sleep(interval)
    QApplication.processEvents()
    return condition()


def _make_holding_loader():
    """A fake get_tts_backend that blocks on release_evt once called, and
    records every call plus max concurrent entries into `state`."""
    entered = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    state = {"current": 0, "max": 0, "calls": []}

    def fake_get_tts_backend(force_reload=False):
        with lock:
            state["calls"].append(force_reload)
            state["current"] += 1
            state["max"] = max(state["max"], state["current"])
        entered.set()
        release.wait(timeout=10)
        with lock:
            state["current"] -= 1
        return _FakeTTSBackend()

    return fake_get_tts_backend, entered, release, state


def _make_tab(monkeypatch, agent_tts=None):
    monkeypatch.setattr(config, "TTS_BACKEND", "kokoro", raising=False)
    monkeypatch.setattr(config, "TTS_ENABLED", True, raising=False)
    agent = types.SimpleNamespace(tts=agent_tts)
    tab = TTSTab(agent, COLORS)
    return tab


# ── R2: Test -> Test ─────────────────────────────────────────────────────

def test_r2_repeated_test_does_not_queue_a_second_reload(qapp, monkeypatch, _config_snapshot):
    fake_loader, entered, release, state = _make_holding_loader()
    monkeypatch.setattr(loader_module, "get_tts_backend", fake_loader)

    tab = _make_tab(monkeypatch)

    tab._test_tts()
    assert not tab.test_btn.isEnabled()
    assert not tab.save_btn.isEnabled()

    assert entered.wait(timeout=5), "first Test worker never reached the loader"

    # Second activation while the first is held -- must be a no-op, not a
    # queued duplicate. Exercise both the button-click path and a direct
    # method call, since setEnabled(False) alone doesn't guard the latter.
    tab.test_btn.click()
    tab._test_tts()

    assert state["calls"] == [True], f"expected exactly one reload call, got {state['calls']}"
    assert state["max"] == 1
    assert not tab.test_btn.isEnabled()
    assert not tab.save_btn.isEnabled()

    release.set()
    assert _pump_until(lambda: tab.test_btn.isEnabled() and tab.save_btn.isEnabled())
    assert state["calls"] == [True]
    assert not tab._busy


# ── R3: Save -> Save ─────────────────────────────────────────────────────

def test_r3_repeated_save_does_not_queue_a_second_reload_or_repeat_persistence(
    qapp, monkeypatch, _config_snapshot, tmp_prefs
):
    fake_loader, entered, release, state = _make_holding_loader()
    monkeypatch.setattr(loader_module, "get_tts_backend", fake_loader)

    save_calls = []
    orig_save = persistence.save

    def spy_save(prefs):
        save_calls.append(dict(prefs))
        return orig_save(prefs)

    monkeypatch.setattr(persistence, "save", spy_save)

    tab = _make_tab(monkeypatch, agent_tts=_FakeTTSBackend())
    tab.tts_backend_combo.setCurrentText("voicebox")

    tab._save()
    assert not tab.test_btn.isEnabled()
    assert not tab.save_btn.isEnabled()
    assert entered.wait(timeout=5), "first Save worker never reached the loader"
    assert len(save_calls) == 1

    tab.save_btn.click()
    tab._save()

    assert len(save_calls) == 1, "Save was persisted more than once for one accepted operation"
    assert state["calls"] == [True], f"expected exactly one reload call, got {state['calls']}"

    release.set()
    assert _pump_until(lambda: tab.test_btn.isEnabled() and tab.save_btn.isEnabled())
    assert len(save_calls) == 1
    assert state["calls"] == [True]
    assert not tab._busy


# ── R4: Test -> Save ─────────────────────────────────────────────────────

def test_r4_save_blocked_while_test_in_flight(qapp, monkeypatch, _config_snapshot, tmp_prefs):
    fake_loader, entered, release, state = _make_holding_loader()
    monkeypatch.setattr(loader_module, "get_tts_backend", fake_loader)

    save_calls = []
    monkeypatch.setattr(persistence, "save", lambda prefs: save_calls.append(prefs) or True)

    tab = _make_tab(monkeypatch, agent_tts=_FakeTTSBackend())

    tab._test_tts()
    assert entered.wait(timeout=5)
    assert state["calls"] == [True]

    tab._save()

    assert save_calls == [], "Save persisted settings while Test was in flight"
    assert state["calls"] == [True], "Save started a second reload while Test was in flight"

    release.set()
    assert _pump_until(lambda: tab.test_btn.isEnabled() and tab.save_btn.isEnabled())


# ── R5: Save -> Test ─────────────────────────────────────────────────────

def test_r5_test_blocked_while_save_in_flight(qapp, monkeypatch, _config_snapshot, tmp_prefs):
    fake_loader, entered, release, state = _make_holding_loader()
    monkeypatch.setattr(loader_module, "get_tts_backend", fake_loader)
    monkeypatch.setattr(persistence, "save", lambda prefs: True)

    tab = _make_tab(monkeypatch, agent_tts=_FakeTTSBackend())
    tab.tts_backend_combo.setCurrentText("voicebox")

    tab._save()
    assert entered.wait(timeout=5)
    assert state["calls"] == [True]

    tab._test_tts()

    assert state["calls"] == [True], "Test started a second reload while Save was in flight"

    release.set()
    assert _pump_until(lambda: tab.test_btn.isEnabled() and tab.save_btn.isEnabled())


# ── R6: Test failure ─────────────────────────────────────────────────────

def test_r6_test_failure_clears_busy_and_surfaces_error(qapp, monkeypatch, _config_snapshot):
    calls = []

    def failing_loader(force_reload=False):
        calls.append(force_reload)
        raise RuntimeError("boom")

    monkeypatch.setattr(loader_module, "get_tts_backend", failing_loader)

    tab = _make_tab(monkeypatch)
    tab._test_tts()

    assert _pump_until(lambda: tab.test_btn.isEnabled() and tab.save_btn.isEnabled())
    assert calls == [True]
    assert "✗" in tab.status_lbl.text() and "boom" in tab.status_lbl.text()
    assert not tab._busy

    # Both buttons must be genuinely usable afterward, not just re-enabled --
    # a second Test should be accepted and run normally.
    ok_loader_calls = []

    def ok_loader(force_reload=False):
        ok_loader_calls.append(force_reload)
        return _FakeTTSBackend()

    monkeypatch.setattr(loader_module, "get_tts_backend", ok_loader)
    tab._test_tts()
    assert _pump_until(lambda: tab.test_btn.isEnabled())
    assert ok_loader_calls == [True]
    assert "✓" in tab.status_lbl.text()


# ── R7: Save swap failure AFTER persistence ─────────────────────────────

def test_r7_save_swap_failure_after_persistence_reports_truthfully(
    qapp, monkeypatch, _config_snapshot, tmp_prefs
):
    save_calls = []
    orig_save = persistence.save

    def spy_save(prefs):
        save_calls.append(dict(prefs))
        return orig_save(prefs)

    monkeypatch.setattr(persistence, "save", spy_save)

    def failing_loader(force_reload=False):
        raise RuntimeError("gpu on fire")

    monkeypatch.setattr(loader_module, "get_tts_backend", failing_loader)

    tab = _make_tab(monkeypatch, agent_tts=_FakeTTSBackend())
    tab.tts_backend_combo.setCurrentText("voicebox")
    tab._save()

    assert _pump_until(lambda: tab.test_btn.isEnabled() and tab.save_btn.isEnabled())

    assert len(save_calls) == 1, "prefs must be saved exactly once"
    msg = tab.status_lbl.text()
    assert "Settings saved" in msg and "gpu on fire" in msg
    assert "Save failed" not in msg, "swap failure must not be reported as nothing saved"
    assert not tab._busy

    # The persisted prefs file must actually exist and contain the save.
    with open(persistence.PREFS_PATH) as f:
        import json
        saved = json.load(f)
    assert saved["tts_backend"] == "voicebox"


# ── R8: Persistence failure BEFORE worker launch ────────────────────────

def test_r8_persistence_failure_blocks_worker_and_recovers_ui(
    qapp, monkeypatch, _config_snapshot, tmp_prefs
):
    monkeypatch.setattr(persistence, "save", lambda prefs: False)

    loader_calls = []
    monkeypatch.setattr(
        loader_module, "get_tts_backend",
        lambda force_reload=False: loader_calls.append(force_reload) or _FakeTTSBackend(),
    )

    tab = _make_tab(monkeypatch, agent_tts=_FakeTTSBackend())
    tab._save()

    # Synchronous path -- no async wait needed, but pump briefly in case any
    # unexpected async work was scheduled.
    _pump_until(lambda: tab.test_btn.isEnabled() and tab.save_btn.isEnabled(), timeout=1)

    assert loader_calls == [], "a backend worker/reload started despite persistence failure"
    msg = tab.status_lbl.text()
    # config.TTS_*/STT_* were already mutated live before persistence.save()
    # was ever reached, so a bare "Save failed" would falsely claim nothing
    # changed -- see R12/R13 for the corrective-pass regression on this.
    assert "not fully saved" in msg and "live values" in msg, msg
    assert tab.test_btn.isEnabled() and tab.save_btn.isEnabled()
    assert not tab._busy


# ── R12: persistence-failure truthfulness ────────────────────────────────

def test_r12_persistence_failure_message_reflects_partial_state_not_full_rollback(
    qapp, monkeypatch, _config_snapshot, tmp_prefs
):
    monkeypatch.setattr(persistence, "save", lambda prefs: False)
    loader_calls = []
    monkeypatch.setattr(
        loader_module, "get_tts_backend",
        lambda force_reload=False: loader_calls.append(force_reload) or _FakeTTSBackend(),
    )

    tab = _make_tab(monkeypatch, agent_tts=_FakeTTSBackend())
    # Pick a value different from the config default so the live-mutation
    # claim is actually meaningful, not just "unchanged == unchanged".
    tab.stt_model_combo.setCurrentText("large-v3")
    prior_stt_model = config.STT_MODEL

    tab._save()
    _pump_until(lambda: tab.test_btn.isEnabled() and tab.save_btn.isEnabled(), timeout=1)

    assert loader_calls == [], "backend reload worker must never start on a persistence failure"
    assert not tab._busy
    assert tab.test_btn.isEnabled() and tab.save_btn.isEnabled()

    # The live in-process value really did change, even though the prefs
    # write failed -- proving the message's "live values may remain
    # changed" claim is factually correct, not just cautious-sounding.
    assert config.STT_MODEL == "large-v3"
    assert config.STT_MODEL != prior_stt_model

    msg = tab.status_lbl.text()
    assert "Save failed; nothing changed" not in msg
    assert msg == "Settings were not fully saved; live values may remain changed until restart.", msg


# ── R13: credential already durably written before the prefs failure ────

def test_r13_persistence_failure_after_elevenlabs_credential_already_written(
    qapp, monkeypatch, _config_snapshot, tmp_prefs
):
    secret_calls = []
    monkeypatch.setattr(
        secrets_module, "set_secret",
        lambda key, value: secret_calls.append((key, value)),
    )
    monkeypatch.setattr(persistence, "save", lambda prefs: False)

    loader_calls = []
    monkeypatch.setattr(
        loader_module, "get_tts_backend",
        lambda force_reload=False: loader_calls.append(force_reload) or _FakeTTSBackend(),
    )

    tab = _make_tab(monkeypatch, agent_tts=_FakeTTSBackend())
    tab.tts_backend_combo.setCurrentText("elevenlabs")
    tab.eleven_key.setText("sk-test-key-123")

    tab._save()
    _pump_until(lambda: tab.test_btn.isEnabled() and tab.save_btn.isEnabled(), timeout=1)

    # The credential write is real, durable, and unconditional -- it must
    # have actually happened despite the prefs failure, or this test would
    # be proving the message against a scenario that can't occur.
    assert secret_calls == [("elevenlabs_api_key", "sk-test-key-123")]
    assert loader_calls == [], "backend reload worker must never start on a persistence failure"
    assert not tab._busy
    assert tab.test_btn.isEnabled() and tab.save_btn.isEnabled()

    msg = tab.status_lbl.text()
    assert "Save failed; nothing changed" not in msg
    assert "API key was stored" in msg
    assert "live values may remain changed" in msg


# ── R11: stale feedback-reset timer cannot overwrite a newer operation ──

def test_r11_stale_test_reset_does_not_overwrite_in_flight_save(
    qapp, monkeypatch, _config_snapshot, tmp_prefs
):
    monkeypatch.setattr(TTSTab, "_RESULT_HOLD_MS", 30)
    monkeypatch.setattr(persistence, "save", lambda prefs: True)

    fast_calls = []
    monkeypatch.setattr(
        loader_module, "get_tts_backend",
        lambda force_reload=False: fast_calls.append(force_reload) or _FakeTTSBackend(),
    )

    tab = _make_tab(monkeypatch, agent_tts=_FakeTTSBackend())

    # Operation A: Test, completes immediately and schedules its own
    # delayed button-text reset ~30ms out.
    tab._test_tts()
    assert _pump_until(lambda: tab.test_btn.isEnabled())
    assert tab.test_btn.text() == "✓ Tested"
    assert fast_calls == [True]

    # Operation B: Save, accepted and held before A's 30ms timer can fire.
    hold_fn, entered, release, hold_state = _make_holding_loader()
    monkeypatch.setattr(loader_module, "get_tts_backend", hold_fn)
    tab.tts_backend_combo.setCurrentText("voicebox")

    tab._save()
    assert entered.wait(timeout=5), "Save never reached the loader"
    assert tab._busy
    assert not tab.test_btn.isEnabled() and not tab.save_btn.isEnabled()
    b_save_text = tab.save_btn.text()
    assert b_save_text == "Saving…"

    # Let A's stale timer's window pass while B is still genuinely in flight.
    _pump_until(lambda: False, timeout=0.15)

    # B's state must be completely untouched by A's now-expired stale timer.
    assert tab._busy, "A's stale timer cleared busy while B was still in flight"
    assert not tab.test_btn.isEnabled() and not tab.save_btn.isEnabled(), (
        "A's stale timer re-enabled controls while B was still in flight"
    )
    assert tab.save_btn.text() == b_save_text, "A's stale timer mutated B's button text"
    assert tab.test_btn.text() == "✓ Tested", (
        "A's own button text was reverted mid-B despite the generation guard"
    )

    # Finish B normally.
    release.set()
    assert _pump_until(lambda: tab.save_btn.isEnabled())
    assert tab.save_btn.text() == "✓ Saved"
    assert tab.test_btn.isEnabled() and tab.save_btn.isEnabled()

    # B's own delayed reset must still work normally.
    assert _pump_until(lambda: tab.save_btn.text() == "Save Settings", timeout=2)


def test_r11_stale_save_reset_does_not_overwrite_in_flight_test(
    qapp, monkeypatch, _config_snapshot, tmp_prefs
):
    monkeypatch.setattr(TTSTab, "_RESULT_HOLD_MS", 30)
    monkeypatch.setattr(persistence, "save", lambda prefs: True)

    fast_calls = []
    monkeypatch.setattr(
        loader_module, "get_tts_backend",
        lambda force_reload=False: fast_calls.append(force_reload) or _FakeTTSBackend(),
    )

    tab = _make_tab(monkeypatch, agent_tts=_FakeTTSBackend())
    tab.tts_backend_combo.setCurrentText("voicebox")

    # Operation A: Save (agent.tts truthy -> async swap path, but the loader
    # is fast/non-holding here), completes immediately and schedules its own
    # delayed reset ~30ms out.
    tab._save()
    assert _pump_until(lambda: tab.save_btn.isEnabled())
    assert tab.save_btn.text() == "✓ Saved"
    assert fast_calls == [True]

    # Operation B: Test, accepted and held before A's 30ms timer can fire.
    hold_fn, entered, release, hold_state = _make_holding_loader()
    monkeypatch.setattr(loader_module, "get_tts_backend", hold_fn)

    tab._test_tts()
    assert entered.wait(timeout=5), "Test never reached the loader"
    assert tab._busy
    assert not tab.test_btn.isEnabled() and not tab.save_btn.isEnabled()
    b_test_text = tab.test_btn.text()
    assert b_test_text == "Testing…"

    # Let A's stale timer's window pass while B is still genuinely in flight.
    _pump_until(lambda: False, timeout=0.15)

    assert tab._busy, "A's stale timer cleared busy while B was still in flight"
    assert not tab.test_btn.isEnabled() and not tab.save_btn.isEnabled(), (
        "A's stale timer re-enabled controls while B was still in flight"
    )
    assert tab.test_btn.text() == b_test_text, "A's stale timer mutated B's button text"
    assert tab.save_btn.text() == "✓ Saved", (
        "A's own button text was reverted mid-B despite the generation guard"
    )

    # Finish B normally.
    release.set()
    assert _pump_until(lambda: tab.test_btn.isEnabled())
    assert tab.test_btn.text() == "✓ Tested"
    assert tab.test_btn.isEnabled() and tab.save_btn.isEnabled()

    # B's own delayed reset must still work normally.
    assert _pump_until(lambda: tab.test_btn.text() == "▶ Test TTS", timeout=2)
