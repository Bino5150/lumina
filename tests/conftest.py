"""
Shared fixtures. Every fixture here exists to keep tests from ever touching
real app data (~/lumina/memory/*, ~/.config/lumina/*) — each test gets its
own throwaway directory.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PySide6.QtWidgets import QApplication
except ImportError:
    QApplication = None


@pytest.fixture(scope="session", autouse=True)
def _retained_qapplication():
    """CODING-08R.1C: guarantee one QApplication survives for the whole
    pytest session, independent of which tests are selected or in what
    order.

    Without a live QApplication anywhere in the process, PySide6's C++-side
    static teardown at interpreter exit can destroy a still-queued
    QQueuedMetaCallEvent left behind by any test that imports ui.main_window
    (test_emergency_stop_ui.py, test_operator_stop_ui.py,
    test_compaction_trigger.py, etc.) — destroying it calls back into Python
    (PyGILState_Ensure) after the interpreter has already finalized, which
    segfaults (native exit 139) *after* every test has already reported
    passed. Confirmed reproducible with several of those files run in
    isolation or in a subset that happens not to include one of the ~20
    files that already build their own local QApplication (e.g.
    test_review_panel.py's `qapp` fixture); confirmed to disappear the
    moment any real QApplication exists earlier in the same process.

    This mirrors the ad hoc `QApplication.instance() or QApplication([])`
    pattern already duplicated across those ~20 files (none of which ever
    tear it down either) — centralizing it here just guarantees it exists
    *before* the first test runs, regardless of selection, rather than
    depending on some other file happening to have created one first.
    """
    if QApplication is None:
        yield
        return
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _drain_qt_events_after_each_test(_retained_qapplication):
    """CODING-08R.1C: process any events left queued by the test that just
    ran, before the next one starts.

    Retaining one QApplication for the whole session (above) fixes the
    exit-139 teardown crash, but it also means a queued cross-thread or
    deferred call (e.g. `QTimer.singleShot(0, ...)`) that a test schedules
    and never pumps no longer just vanishes at interpreter exit -- it stays
    queued and can fire during a *later*, unrelated test's own
    `QApplication.processEvents()` call, against objects that test never
    set up (see the test_on_user_message_blocked_while_latched fake-wiring
    fix in test_emergency_stop_ui.py, found via exactly this mechanism).
    Draining here attributes any such leak to the test that actually
    caused it instead of contaminating whichever test happens to run next.
    """
    yield
    if _retained_qapplication is not None:
        _retained_qapplication.processEvents()
