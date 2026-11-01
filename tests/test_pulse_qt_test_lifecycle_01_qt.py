"""PULSE-QT-TEST-LIFECYCLE-01 -- no test-built LuminaWindow keeps working
after its test.

The pytest session retains one QApplication, and the process-wide Companion
routing broker keeps every presenter window reachable. A closed test window
used to keep its once-a-minute Dreaming idle check running, and once it had
been idle for DREAM_IDLE_MINUTES it started a `_run_dream_sweep` thread
during a later, unrelated test (a failed thread count in one full run, a
hang in another). conftest's `_retire_lumina_windows_after_each_test` now
stops every timer a surviving window owns after each test, and deliberately
does not delete the window (deleting it segfaulted the full suite).

  * the hazard is real: a closed but surviving window past the idle
    threshold starts a sweep as soon as its timer fires;
  * a window left behind by one test -- closed, past the idle threshold,
    still referenced, its check firing every millisecond -- has every timer
    stopped before the next test begins, starts nothing while that test
    pumps events, and is still a valid (undeleted) Qt object.

The two `left_behind` tests run in file order on purpose: the first leaves
the window, the second checks what the fixture did with it.

Missing Qt fails (never skips) when LUMINA_REQUIRE_QT=1, as in CI's
blocking qt-security-guards job. Every store is disposable.
"""
import os
import threading
import time

import pytest

if os.environ.get("LUMINA_REQUIRE_QT") == "1":
    import PySide6  # noqa: F401
else:
    pytest.importorskip("PySide6")

import shiboken6
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import config as config_module
import core.persistence as persistence


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def sweeps(monkeypatch):
    """Record every Dreaming sweep a window tries to start; start none."""
    import ui.main_window as mw
    started = []
    real_thread = threading.Thread

    class _Recorder:
        def __init__(self, *args, target=None, **kwargs):
            self._real = None
            if getattr(target, "__name__", "") == "_run_dream_sweep":
                self.target = target
            else:
                self._real = real_thread(*args, target=target, **kwargs)

        def start(self):
            if self._real is not None:
                return self._real.start()
            started.append(self.target)

    monkeypatch.setattr(mw.threading, "Thread", _Recorder)
    return started


def _closed_idle_window(tmp_path, monkeypatch):
    """A window the way every Qt test fixture leaves it -- closed, not
    deleted -- that has been idle past the threshold, with its Dreaming
    check sped up to fire every millisecond."""
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
    from core.agent import LuminaAgent
    from tools.memory import create_chat, init_chat_db
    init_chat_db()
    chat_id = create_chat("lifecycle")
    win = mw.LuminaWindow(LuminaAgent(owner=True, channel_id="lifecycle-qt"))
    win._load_chat(chat_id, persist_as_last=False)
    QApplication.processEvents()
    win.close()
    win._last_activity = time.time() - getattr(config_module, "DREAM_IDLE_MINUTES", 20) * 60 - 1
    win._dream_timer.setInterval(1)
    win._dream_timer.start()
    return win


def _pump_until(predicate, timeout_s=5.0):
    # Late in the full suite a 1 ms timer can fire far less often than once
    # per millisecond, so wait for the event itself, never a fixed interval.
    deadline = time.monotonic() + timeout_s
    while not predicate() and time.monotonic() < deadline:
        QTest.qWait(10)
    return predicate()


def test_a_closed_window_past_the_idle_threshold_starts_a_sweep(qapp, tmp_path, monkeypatch, sweeps):
    win = _closed_idle_window(tmp_path, monkeypatch)
    assert _pump_until(lambda: sweeps)
    assert len(sweeps) == 1                      # the hazard the fixture removes
    assert sweeps[0].__self__ is win


_left_behind = []


def test_left_behind_a_closed_idle_window(qapp, tmp_path, monkeypatch):
    win = _closed_idle_window(tmp_path, monkeypatch)
    _left_behind.append(win)                     # held, like the routing broker holds it
    assert win._dream_timer.isActive()


def test_left_behind_window_is_quiet_before_the_next_test(qapp, sweeps):
    assert _left_behind, "run this file whole: the previous test leaves the window"
    win = _left_behind.pop()
    assert shiboken6.isValid(win)                # stopped, never deleted from Qt's side
    timers = win.findChildren(QTimer)
    assert win._dream_timer in timers and win._telemetry_timer in timers
    assert [t for t in timers if t.isActive()] == []
    ticks = []                                   # proves the loop really ran: 50 ticks of a
    control = QTimer()                           # 1 ms timer, the old Dreaming check's interval
    control.setInterval(1)
    control.timeout.connect(lambda: ticks.append(1))
    control.start()
    try:
        assert _pump_until(lambda: len(ticks) >= 50)
    finally:
        control.stop()
    assert sweeps == []
