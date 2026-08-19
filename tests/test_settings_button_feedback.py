"""Patch 3A.3B Part A / R1-R3 -- proves ButtonFeedback (ui/settings/_widgets.py)
actually delivers the semantic-feedback contract shared by every Settings
tab's Save/Apply button: success() reverts after a delay, failure() does
NOT auto-revert, and a stale delayed reset from an older call can never
stomp state set by a newer one. Also proves the rebuild-safety guard: a
delayed reset against a deleted QPushButton is a safe no-op, not a crash.

Real offscreen QApplication, real QPushButton, real QTimer.singleShot --
only the HOLD_MS class attribute is shortened via monkeypatch so the tests
run fast instead of waiting ~1.75s for every revert.

Run headless from repo root:
    QT_QPA_PLATFORM=offscreen python -m pytest tests/test_settings_button_feedback.py -v
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QPushButton

from ui.settings._widgets import ButtonFeedback


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _pump_until(condition, timeout=5.0, interval=0.005):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if condition():
            return True
        time.sleep(interval)
    QApplication.processEvents()
    return condition()


# ── R1: success feedback + delayed revert ────────────────────────────────

def test_r1_success_shows_text_then_reverts_to_idle_text(qapp, monkeypatch):
    monkeypatch.setattr(ButtonFeedback, "HOLD_MS", 30)
    btn = QPushButton("Save All Settings")
    fb = ButtonFeedback(btn)

    fb.success("✓ Saved")
    assert btn.text() == "✓ Saved"

    assert _pump_until(lambda: btn.text() == "Save All Settings")


def test_r1_idle_text_can_be_overridden_explicitly(qapp, monkeypatch):
    monkeypatch.setattr(ButtonFeedback, "HOLD_MS", 30)
    btn = QPushButton("placeholder")
    fb = ButtonFeedback(btn, idle_text="Apply Change")

    fb.success("✓ Applied")
    assert _pump_until(lambda: btn.text() == "Apply Change")


# ── R2: stale delayed-reset safety ───────────────────────────────────────

def test_r2_stale_reset_cannot_overwrite_a_newer_success(qapp):
    btn = QPushButton("Save")
    fb = ButtonFeedback(btn)

    fb.success("✓ Saved", hold_ms=30)  # operation A -- short revert window
    assert btn.text() == "✓ Saved"

    # Operation B starts immediately after A, with a much longer window, so
    # A's stale timer firing can be observed strictly inside B's own window.
    fb.success("✓ Saved Again", hold_ms=300)
    assert btn.text() == "✓ Saved Again"

    # Let A's now-stale ~30ms window pass while B's own 300ms window has not.
    _pump_until(lambda: False, timeout=0.15)
    assert btn.text() == "✓ Saved Again", (
        "A's stale timer overwrote B's text before B's own revert was due"
    )

    # B's own revert must still fire normally afterward.
    assert _pump_until(lambda: btn.text() == "Save", timeout=2)


def test_r2_stale_reset_cannot_overwrite_a_newer_failure(qapp, monkeypatch):
    monkeypatch.setattr(ButtonFeedback, "HOLD_MS", 30)
    btn = QPushButton("Save")
    fb = ButtonFeedback(btn)

    fb.success("✓ Saved")  # schedules a revert ~30ms out
    fb.failure("✗ Failed")  # a newer, persistent failure replaces it

    # Let A's now-stale timer's window pass.
    _pump_until(lambda: False, timeout=0.15)
    assert btn.text() == "✗ Failed", (
        "stale success timer overwrote a newer, persistent failure"
    )


# ── R3: failure persists past the success-reset interval ────────────────

def test_r3_failure_does_not_auto_revert(qapp, monkeypatch):
    monkeypatch.setattr(ButtonFeedback, "HOLD_MS", 30)
    btn = QPushButton("Save")
    fb = ButtonFeedback(btn)

    fb.failure("✗ Failed")
    assert btn.text() == "✗ Failed"

    # Wait well past what would have been a success-style hold.
    _pump_until(lambda: False, timeout=0.2)
    assert btn.text() == "✗ Failed", "failure text auto-reverted like a success would"


def test_r3_next_deliberate_action_replaces_failure(qapp, monkeypatch):
    monkeypatch.setattr(ButtonFeedback, "HOLD_MS", 30)
    btn = QPushButton("Save")
    fb = ButtonFeedback(btn)

    fb.failure("✗ Failed")
    assert btn.text() == "✗ Failed"

    # The user retries -- a fresh success replaces the stale failure and
    # still reverts normally afterward.
    fb.success("✓ Saved")
    assert btn.text() == "✓ Saved"
    assert _pump_until(lambda: btn.text() == "Save")


# ── Rebuild-safety: deleted button must not crash a pending revert ──────

def test_delayed_revert_against_a_deleted_button_is_a_safe_noop(qapp, monkeypatch):
    monkeypatch.setattr(ButtonFeedback, "HOLD_MS", 30)
    btn = QPushButton("Save")
    fb = ButtonFeedback(btn)

    fb.success("✓ Saved")
    btn.deleteLater()
    # Actually destroy the C++ object before the revert timer fires --
    # mirrors PersonasTab's right-panel rebuild landmine, where
    # _clear_right() deletes the old Save button while a delayed reset from
    # an earlier click may still be pending.
    QApplication.processEvents()
    QApplication.sendPostedEvents(None, 0)

    # Must not raise RuntimeError ("wrapped C/C++ object ... deleted").
    _pump_until(lambda: False, timeout=0.15)
