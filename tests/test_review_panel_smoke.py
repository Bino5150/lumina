"""CODING-08A4 standalone Qt teardown smoke test.

Section 24 of the task spec: a standalone Qt invocation can print "all
tests passed" and still exit nonzero (e.g. 139/SIGSEGV) during native Qt
teardown, well after pytest's own summary line already looked green. That
class of bug is invisible to `pytest -q` (which only sees Python-level
results) -- it requires checking the REAL subprocess exit code of a
standalone `python3` invocation that constructs real widgets and lets the
interpreter exit normally afterward, exercising the exact same native
teardown path a live desktop session takes.

Not pytest-collected (no test_* functions) -- like
tests/test_settings_extras_smoke.py, this is meant to be run directly:

    QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 tests/test_review_panel_smoke.py

and its exit code checked explicitly by the caller (never inferred from
printed output alone).
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import subprocess
import sys
import tempfile

PASS, FAIL = [], []


def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}  {detail}")


def _git(cwd, *args, check_rc=True):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=check_rc, capture_output=True, text=True,
    )


def main():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)

    import config
    tmp_data = tempfile.mkdtemp()
    config.DATA_DIR = tmp_data
    config.DB_PATH = os.path.join(tmp_data, "memory", "lumina.db")

    import core.persistence as persistence
    persistence.PREFS_PATH = os.path.join(tmp_data, "prefs.json")
    import core.project_context as project_context_module
    project_context_module.PROJECT_BINDINGS_DIR = os.path.join(tmp_data, "bindings")

    from core.agent import LuminaAgent
    from ui.main_window import LuminaWindow

    repo = tempfile.mkdtemp()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "smoke@example.invalid")
    _git(repo, "config", "user.name", "Smoke")
    with open(os.path.join(repo, "a.txt"), "w") as f:
        f.write("line1\nline2\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "init")
    with open(os.path.join(repo, "a.txt"), "a") as f:
        f.write("line3\n")
    _git(repo, "add", "a.txt")

    agent = LuminaAgent(owner=True, channel_id="a4-teardown-smoke")
    win = LuminaWindow(agent)
    win.show()
    check("LuminaWindow constructs with ReviewPanel", win.review_panel is not None)

    win._show_panel("review")
    check("Review panel becomes visible", win.review_panel.isVisible())

    import core.review_target as rt
    target = rt.resolve_explicit_path_target(repo)
    win.review_panel._start_review(target)

    import time
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and win.review_panel._current_review is None:
        app.processEvents()
        time.sleep(0.01)
    check("Review capture completes", win.review_panel._current_review is not None)

    staged = win.review_panel.lists["staged"]
    check("Staged change appears", staged.count() >= 1)
    if staged.count() >= 1:
        win.review_panel._on_change_item_clicked(staged.item(0))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not win.review_panel.diff_view.toPlainText():
            app.processEvents()
            time.sleep(0.01)
        check("Diff content renders", bool(win.review_panel.diff_view.toPlainText()))

    win._show_panel("chat")
    win._show_panel("review")
    check("Panel can be hidden and reopened", win.review_panel.isVisible())

    # Close mid-refresh: start a fresh refresh, close immediately, and let
    # the worker (if still running) finish naturally in the background --
    # exercises section 20/26's "main window shutdown during capture"
    # teardown path for real, not just at the controller-unit-test level.
    win.review_panel._start_review(target)
    win.close()
    check("closeEvent completes without raising", True)

    print(f"\n{'='*60}")
    print(f"PASS: {len(PASS)}   FAIL: {len(FAIL)}")
    if FAIL:
        print("\nFailed checks:")
        for f in FAIL:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print("\nAll checks passed.")
        sys.exit(0)


if __name__ == "__main__":
    main()
