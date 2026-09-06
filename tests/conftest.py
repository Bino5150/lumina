"""
TEST-DATA-ISOLATION-01: the module-level code below (not a fixture) is what
actually keeps the suite from touching the owner's real Lumina state
(~/.local/share/lumina/*, ~/.config/lumina/*). It has to run here, at
conftest.py's own import, rather than in even an autouse session fixture --
config.py resolves DATA_DIR (and every *_PATH/*_DIR constant derived from
it: prefs.json, the shared sqlite DB behind chat/Palace/Knowledge
Base/Skills/checkpoints, telemetry, the idempotency ledger, project
bindings, custom tools) exactly once, from the LUMINA_DATA_DIR env var, the
first time anything does `import config` -- and pytest imports every test
module (many of which `import config` at their own top level) during
collection, before any fixture, autouse or not, has had a chance to run.
This mirrors a fix already proven for the eval harness (eval/run_eval.py's
own "CRITICAL" comment, eval/scratch_dir.py) after a stale prefs.json there
once silently contaminated two already-cited eval runs -- the pytest suite
had no equivalent guard at all until TEST-DATA-ISOLATION-01.

core/secrets.py's credentials.json is hardcoded outside DATA_DIR entirely
(by design -- see that module's docstring), so it needs its own env var,
LUMINA_SECRETS_PATH, set below for the same import-time reason: config.py
itself reads get_secret("custom_api_key") at its own module level, so even
this needs to be in place before the first `import config` anywhere, not
just isolated per-test.

core/test_isolation.py is the second, independent layer: persistence.py,
secrets.py and db.py each refuse outright if a resolved path still lands in
the real owner-data roots while LUMINA_TESTING=1, catching a rogue
monkeypatch or a symlinked test root even if the isolation below were ever
removed or bypassed.

Per-test isolation (tmp_path-scoped PREFS_PATH/DATA_DIR/etc. via
monkeypatch, already used throughout this suite) still matters and is
unaffected by this file -- this is a backstop for tests that don't bother,
not a replacement for tests that already isolate themselves more tightly.
"""
import os
import shutil
import sys
import tempfile

_TEST_DATA_ROOT = tempfile.mkdtemp(prefix="lumina-pytest-")
os.environ["LUMINA_DATA_DIR"] = _TEST_DATA_ROOT
os.environ["LUMINA_SECRETS_PATH"] = os.path.join(_TEST_DATA_ROOT, "credentials.json")
os.environ["LUMINA_TESTING"] = "1"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PySide6.QtWidgets import QApplication
except ImportError:
    QApplication = None


@pytest.fixture(scope="session", autouse=True)
def _cleanup_test_data_root():
    """Best-effort removal of the whole-session scratch data dir at the end
    of the run. Failure to clean up is not a safety issue (it's still a
    throwaway /tmp dir, never owner state) so this never raises."""
    yield
    shutil.rmtree(_TEST_DATA_ROOT, ignore_errors=True)


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
