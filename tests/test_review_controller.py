"""CODING-08A4 ReviewController generation/lifetime/race tests.

Deterministic synchronization via threading.Event, never sleep-based luck.
core.git_review_snapshot.capture_snapshot/retrieve_review_file are
monkeypatched with fakes so these tests isolate the CONTROLLER's own
publish-authority logic from real Git/A1/A2 behavior (already exhaustively
covered by tests/test_git_review.py / test_git_review_snapshot.py /
test_review_target.py).
"""

import os
import threading
import time
import types

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import ui.review_controller as review_controller
from ui.review_controller import (
    CURRENT, CURRENT_METADATA_ONLY, ERROR, REFRESHING, STALE,
    TARGET_UNAVAILABLE, ReviewController,
)


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


def _fake_handle(tag, *, content_complete=True):
    fingerprint = types.SimpleNamespace(content_complete=content_complete, omissions=() if content_complete else ("reason",))
    review = types.SimpleNamespace(changes=())
    snapshot = types.SimpleNamespace(fingerprint=fingerprint, identity=tag, review=review, captured_at="t")
    return types.SimpleNamespace(snapshot_ref=f"ref-{tag}", snapshot=snapshot)


def _fake_file_result(change_id, layer, *, complete=True):
    return types.SimpleNamespace(
        change_id=change_id, path=change_id, layer=layer, binary=False,
        hunks=(), complete=complete, omitted_hunks=0, omission_reason=None,
        total_bytes=0, next_cursor=None,
    )


def _fake_retrieval_outcome(status, file=None, reasons=()):
    applicability = types.SimpleNamespace(state=status, reasons=reasons)
    return types.SimpleNamespace(status=status, file=file, applicability=applicability)


# ===========================================================================
# Capture races
# ===========================================================================

def test_refresh_b_wins_when_a_returns_late(qapp, monkeypatch):
    """Required race: Refresh A starts slowly, Refresh B starts later and
    finishes first, Refresh A returns last -- the UI must continue showing
    B; A may never overwrite B."""
    controller = ReviewController()
    a_started = threading.Event()
    a_release = threading.Event()

    def fake_capture(identity):
        if identity == "A":
            a_started.set()
            assert a_release.wait(5), "test setup: A was never released"
        return _fake_handle(identity)

    monkeypatch.setattr(review_controller.git_review_snapshot, "capture_snapshot", fake_capture)

    published = []
    controller.snapshot_ready.connect(lambda snap: published.append(snap.identity))

    controller.refresh("A")
    assert a_started.wait(5)
    controller.refresh("B")

    assert _pump_until(lambda: published == ["B"], timeout=5)

    a_release.set()
    # Give A's worker time to finish and attempt to publish its (stale) result.
    assert _pump_until(lambda: not controller._workers, timeout=5)
    assert published == ["B"], "A's late result must never overwrite B"


def test_invalidate_discards_in_flight_capture_result(qapp, monkeypatch):
    """Required races: review closed while capture worker runs / main window
    shutdown during capture."""
    controller = ReviewController()
    started = threading.Event()
    release = threading.Event()

    def fake_capture(identity):
        started.set()
        assert release.wait(5)
        return _fake_handle(identity)

    monkeypatch.setattr(review_controller.git_review_snapshot, "capture_snapshot", fake_capture)

    published = []
    controller.snapshot_ready.connect(lambda snap: published.append(snap))
    states = []
    controller.state_changed.connect(lambda s, r: states.append(s))

    controller.refresh("A")
    assert started.wait(5)

    # invalidate() synchronously waits for in-flight workers (see its own
    # docstring), so it must be driven from a separate thread here -- this
    # test needs to control exactly when the blocked worker is allowed to
    # proceed, deterministically, not race invalidate()'s own wait.
    invalidate_thread = threading.Thread(target=controller.invalidate)
    invalidate_thread.start()
    release.set()
    invalidate_thread.join(5)
    assert not invalidate_thread.is_alive(), "invalidate() should return promptly once released"

    assert _pump_until(lambda: not controller._workers, timeout=5)
    assert published == []
    assert REFRESHING in states
    assert not any(s in (CURRENT, CURRENT_METADATA_ONLY) for s in states)


def test_target_switch_discards_earlier_in_flight_result(qapp, monkeypatch):
    """Required race: target changes while worker runs."""
    controller = ReviewController()
    a_started = threading.Event()
    a_release = threading.Event()

    def fake_capture(identity):
        if identity == "A":
            a_started.set()
            assert a_release.wait(5)
        return _fake_handle(identity)

    monkeypatch.setattr(review_controller.git_review_snapshot, "capture_snapshot", fake_capture)
    published = []
    controller.snapshot_ready.connect(lambda snap: published.append(snap.identity))

    controller.refresh("A")
    assert a_started.wait(5)
    controller.refresh("B")  # target switch before A resolves
    assert _pump_until(lambda: published == ["B"], timeout=5)

    a_release.set()
    assert _pump_until(lambda: not controller._workers, timeout=5)
    assert published == ["B"]


# ===========================================================================
# Retrieval races
# ===========================================================================

def _ready_controller(monkeypatch):
    controller = ReviewController()
    monkeypatch.setattr(
        review_controller.git_review_snapshot, "capture_snapshot",
        lambda identity: _fake_handle(identity),
    )
    ready = []
    controller.snapshot_ready.connect(lambda snap: ready.append(snap))
    controller.refresh("A")
    assert _pump_until(lambda: ready, timeout=5)
    return controller


def test_retrieval_b_wins_when_a_returns_late(qapp, monkeypatch):
    """Required race: file retrieval returns after another selection."""
    controller = _ready_controller(monkeypatch)
    a_started = threading.Event()
    a_release = threading.Event()

    def fake_retrieve(snapshot_ref, change_id, layer, *, start_hunk_index=0):
        if change_id == "file-a":
            a_started.set()
            assert a_release.wait(5)
            return _fake_retrieval_outcome(CURRENT, _fake_file_result("file-a", layer))
        return _fake_retrieval_outcome(CURRENT, _fake_file_result("file-b", layer))

    monkeypatch.setattr(review_controller.git_review_snapshot, "retrieve_review_file", fake_retrieve)

    published = []
    controller.file_ready.connect(lambda outcome: published.append(outcome.change_id))

    controller.select_file("file-a", "staged")
    assert a_started.wait(5)
    controller.select_file("file-b", "staged")
    assert _pump_until(lambda: published == ["file-b"], timeout=5)

    a_release.set()
    assert _pump_until(lambda: not controller._workers, timeout=5)
    assert published == ["file-b"], "file-a's late result must never overwrite file-b"


def test_invalidate_discards_in_flight_retrieval_result(qapp, monkeypatch):
    """Required races: review closed while retrieval worker runs / main
    window shutdown during retrieval."""
    controller = _ready_controller(monkeypatch)
    started = threading.Event()
    release = threading.Event()

    def fake_retrieve(snapshot_ref, change_id, layer, *, start_hunk_index=0):
        started.set()
        assert release.wait(5)
        return _fake_retrieval_outcome(CURRENT, _fake_file_result(change_id, layer))

    monkeypatch.setattr(review_controller.git_review_snapshot, "retrieve_review_file", fake_retrieve)
    published = []
    controller.file_ready.connect(lambda outcome: published.append(outcome))

    controller.select_file("file-a", "staged")
    assert started.wait(5)

    invalidate_thread = threading.Thread(target=controller.invalidate)
    invalidate_thread.start()
    release.set()
    invalidate_thread.join(5)
    assert not invalidate_thread.is_alive(), "invalidate() should return promptly once released"

    assert _pump_until(lambda: not controller._workers, timeout=5)
    assert published == []


def test_stale_retrieval_transitions_banner_to_stale(qapp, monkeypatch):
    """Required race: stale snapshot retrieval transitions UI to stale."""
    controller = _ready_controller(monkeypatch)

    def fake_retrieve(snapshot_ref, change_id, layer, *, start_hunk_index=0):
        return _fake_retrieval_outcome(STALE, None, reasons=("structured_state_changed",))

    monkeypatch.setattr(review_controller.git_review_snapshot, "retrieve_review_file", fake_retrieve)
    states = []
    controller.state_changed.connect(lambda s, r: states.append(s))
    published = []
    controller.file_ready.connect(lambda outcome: published.append(outcome))

    controller.select_file("file-a", "staged")
    assert _pump_until(lambda: STALE in states, timeout=5)
    assert published == []


# ===========================================================================
# Sticky-stale latch
# ===========================================================================

def test_sticky_stale_latch_blocks_later_current_within_same_generation(qapp, monkeypatch):
    """Required race: no stale worker can restore CURRENT after a newer
    stale/error state, within the SAME generation."""
    controller = _ready_controller(monkeypatch)

    order = []

    def fake_retrieve(snapshot_ref, change_id, layer, *, start_hunk_index=0):
        if change_id == "bad":
            order.append("bad-issued")
            return _fake_retrieval_outcome(ERROR, None)
        order.append("good-issued")
        return _fake_retrieval_outcome(CURRENT, _fake_file_result(change_id, layer))

    monkeypatch.setattr(review_controller.git_review_snapshot, "retrieve_review_file", fake_retrieve)

    states = []
    controller.state_changed.connect(lambda s, r: states.append(s))
    published = []
    controller.file_ready.connect(lambda outcome: published.append(outcome.change_id))

    # First: a retrieval reports ERROR (latches this generation).
    controller.select_file("bad", "staged")
    assert _pump_until(lambda: ERROR in states, timeout=5)

    # Then: a LATER retrieval within the SAME generation claims CURRENT.
    controller.select_file("good", "staged")
    time.sleep(0.05)
    QApplication.processEvents()

    assert published == [], "a later same-generation CURRENT result must not un-latch a stale/error state"

    # A brand new refresh (new generation) DOES clear the latch.
    controller.select_file("good2", "staged")
    time.sleep(0.05)
    QApplication.processEvents()
    assert published == [], "still same generation -- latch remains until an actual refresh()"


def test_new_refresh_clears_sticky_stale_latch(qapp, monkeypatch):
    controller = ReviewController()
    calls = {"n": 0}

    def fake_capture(identity):
        calls["n"] += 1
        return _fake_handle(identity)

    monkeypatch.setattr(review_controller.git_review_snapshot, "capture_snapshot", fake_capture)
    monkeypatch.setattr(
        review_controller.git_review_snapshot, "retrieve_review_file",
        lambda *a, **k: _fake_retrieval_outcome(ERROR, None),
    )

    ready = []
    controller.snapshot_ready.connect(lambda snap: ready.append(snap))
    states = []
    controller.state_changed.connect(lambda s, r: states.append(s))

    controller.refresh("A")
    assert _pump_until(lambda: ready, timeout=5)
    controller.select_file("x", "staged")
    assert _pump_until(lambda: ERROR in states, timeout=5)

    states.clear()
    ready.clear()
    controller.refresh("A")  # brand new generation
    assert _pump_until(lambda: ready, timeout=5)
    assert CURRENT in states


# ===========================================================================
# Basic lifecycle / bookkeeping
# ===========================================================================

def test_refresh_emits_refreshing_immediately(qapp, monkeypatch):
    controller = ReviewController()
    release = threading.Event()
    monkeypatch.setattr(
        review_controller.git_review_snapshot, "capture_snapshot",
        lambda identity: (release.wait(5), _fake_handle(identity))[1],
    )
    states = []
    controller.state_changed.connect(lambda s, r: states.append(s))
    controller.refresh("A")
    assert states == [REFRESHING]
    release.set()
    assert _pump_until(lambda: not controller._workers, timeout=5)


def test_select_file_without_snapshot_is_noop(qapp):
    controller = ReviewController()
    published = []
    controller.file_ready.connect(lambda outcome: published.append(outcome))
    controller.select_file("x", "staged")
    QApplication.processEvents()
    assert published == []
    assert controller._workers == []


def test_no_work_scheduled_after_invalidate(qapp, monkeypatch):
    controller = ReviewController()
    calls = {"n": 0}
    monkeypatch.setattr(
        review_controller.git_review_snapshot, "capture_snapshot",
        lambda identity: calls.__setitem__("n", calls["n"] + 1) or _fake_handle(identity),
    )
    controller.invalidate()
    controller.refresh("A")
    QApplication.processEvents()
    assert calls["n"] == 0
    assert controller._workers == []


def test_workers_are_forgotten_after_finish(qapp, monkeypatch):
    controller = ReviewController()
    monkeypatch.setattr(
        review_controller.git_review_snapshot, "capture_snapshot",
        lambda identity: _fake_handle(identity),
    )
    ready = []
    controller.snapshot_ready.connect(lambda snap: ready.append(snap))
    controller.refresh("A")
    assert _pump_until(lambda: ready, timeout=5)
    assert _pump_until(lambda: controller._workers == [], timeout=5)
