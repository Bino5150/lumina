"""CODING-08A4 -- generation-guarded Qt review controller.

    A1 Git truth
        |
    A2 snapshot/fingerprint/retrieval   (core.git_review / core.git_review_snapshot)
        |
    ReviewController (this module)
        |
    ReviewPanel (ui/review_panel.py)

This module is the ONLY place that decides which background worker result
may ever reach the UI. It never parses raw ``git diff`` output, never
consumes ``tools/review.py`` JSON, and never treats model tool output as
repository truth -- it calls A1/A2's own structured Python API directly,
exactly like ``tools/review.py`` (A3) does, as a sibling consumer.

Generation/selection sequencing (LOAD-BEARING)
-------------------------------------------------
Two independent monotonic counters:

- ``_generation`` -- bumped by every ``refresh()`` (a fresh target and every
  re-refresh of the same target both count as a new generation). A capture
  worker's result publishes only when its captured generation still equals
  the controller's current generation.
- ``_selection_seq`` -- bumped by every ``select_file()``/``load_more()``
  call. A retrieval worker's result publishes only when its captured
  ``(generation, selection_seq)`` pair still matches current -- so a stale
  file selection is discarded independently of whether the target itself
  changed.

Sticky-stale latch (mirrors core.git_review_snapshot's own ``ever_stale``
design at this layer): once any result within the CURRENT generation
reports a non-live state (STALE/TARGET_UNAVAILABLE/ERROR), that generation
latches -- no later-arriving result claiming CURRENT/CURRENT_METADATA_ONLY
for the SAME generation may un-latch it. Only a brand new ``refresh()``
(a new generation) can return to a live state. This is what makes race #10
("no stale worker can restore CURRENT after a newer stale/error state")
true by construction rather than by ordering luck.

``invalidate()`` (panel close/hide-on-shutdown/app teardown) bumps both
counters and clears the valid flag, so every in-flight worker's eventual
result becomes structurally unpublishable -- workers are never killed
unsafely; they are left to finish and their results are simply discarded.
Every Git subprocess A1/A2 launches carries its own hard timeout, so this
never produces an unbounded shutdown wait.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal

import core.git_review as git_review
import core.git_review_snapshot as git_review_snapshot
from core.coding_checkpoint import TargetIdentity

CURRENT = git_review_snapshot.CURRENT
CURRENT_METADATA_ONLY = git_review_snapshot.CURRENT_METADATA_ONLY
STALE = git_review_snapshot.STALE
TARGET_UNAVAILABLE = git_review_snapshot.TARGET_UNAVAILABLE
ERROR = git_review_snapshot.ERROR
# Qt-only transitional state -- never an A2 applicability value, never
# latched, never published by a worker result.
REFRESHING = "refreshing"

_LIVE_STATES = (CURRENT, CURRENT_METADATA_ONLY)


@dataclass(frozen=True)
class CaptureOutcome:
    generation: int
    state: str
    reasons: tuple
    handle: Optional[object]  # core.git_review_snapshot.SnapshotHandle, only when state is live
    message: Optional[str]


@dataclass(frozen=True)
class RetrievalOutcome:
    generation: int
    selection_seq: int
    change_id: str
    layer: str
    start_hunk_index: int
    state: str
    reasons: tuple
    file: Optional[object]  # core.git_review_snapshot.FileDiffResult, only when live
    message: Optional[str]


class _CaptureWorker(QThread):
    """Runs core.git_review_snapshot.capture_snapshot() off the GUI thread."""
    result_ready = Signal(object)  # CaptureOutcome

    def __init__(self, target_identity: TargetIdentity, generation: int):
        super().__init__()
        self._target = target_identity
        self._generation = generation

    def run(self):
        try:
            handle = git_review_snapshot.capture_snapshot(self._target)
        except git_review_snapshot.UnstableSnapshotCapture as exc:
            outcome = CaptureOutcome(self._generation, ERROR, (), None, str(exc))
        except git_review.ReviewTargetError as exc:
            outcome = CaptureOutcome(self._generation, TARGET_UNAVAILABLE, (), None, str(exc))
        except git_review.GitReviewError as exc:
            outcome = CaptureOutcome(self._generation, ERROR, (), None, str(exc))
        else:
            fingerprint = handle.snapshot.fingerprint
            state = CURRENT if fingerprint.content_complete else CURRENT_METADATA_ONLY
            reasons = () if fingerprint.content_complete else fingerprint.omissions
            outcome = CaptureOutcome(self._generation, state, reasons, handle, None)
        self.result_ready.emit(outcome)


class _RetrievalWorker(QThread):
    """Runs core.git_review_snapshot.retrieve_review_file() off the GUI
    thread. A2 itself revalidates applicability before and after capturing
    content (see its own docstring) -- this worker only relays the truth,
    never re-derives it."""
    result_ready = Signal(object)  # RetrievalOutcome

    def __init__(self, snapshot_ref: str, change_id: str, layer: str,
                 start_hunk_index: int, generation: int, selection_seq: int):
        super().__init__()
        self._snapshot_ref = snapshot_ref
        self._change_id = change_id
        self._layer = layer
        self._start_hunk_index = start_hunk_index
        self._generation = generation
        self._selection_seq = selection_seq

    def _outcome(self, state, reasons, file, message):
        return RetrievalOutcome(
            self._generation, self._selection_seq, self._change_id, self._layer,
            self._start_hunk_index, state, reasons, file, message,
        )

    def run(self):
        try:
            outcome = git_review_snapshot.retrieve_review_file(
                self._snapshot_ref, self._change_id, self._layer,
                start_hunk_index=self._start_hunk_index,
            )
        except git_review_snapshot.UnknownSnapshot as exc:
            result = self._outcome(TARGET_UNAVAILABLE, (), None, str(exc))
        except git_review_snapshot.UnknownChange as exc:
            result = self._outcome(ERROR, (), None, str(exc))
        except git_review_snapshot.RetrievalLayerError as exc:
            result = self._outcome(ERROR, (), None, str(exc))
        except git_review.GitReviewError as exc:
            result = self._outcome(ERROR, (), None, str(exc))
        else:
            result = self._outcome(
                outcome.status, outcome.applicability.reasons, outcome.file, None,
            )
        self.result_ready.emit(result)


class ReviewController(QObject):
    """Sole authority on which worker result may reach the UI. See module
    docstring for the generation/selection/sticky-stale design."""

    state_changed = Signal(str, tuple)   # (state, reasons) -- may include REFRESHING
    snapshot_ready = Signal(object)      # core.git_review_snapshot.BoundedSnapshot
    file_ready = Signal(object)          # RetrievalOutcome, only when publishable

    def __init__(self, parent=None):
        super().__init__(parent)
        self._generation = 0
        self._selection_seq = 0
        self._current_gen_is_stale = False
        self._current_snapshot_ref: Optional[str] = None
        self._valid = True
        self._workers: list = []

    # ── lifecycle ──────────────────────────────────────────────────────

    def invalidate(self) -> None:
        """Panel close / app shutdown. No further work is scheduled; every
        in-flight worker's eventual result becomes unpublishable.

        Workers are never killed unsafely -- but they ARE waited for here,
        synchronously and briefly, before returning. This is not "cancel
        the worker": run() is left to finish exactly as it would otherwise;
        this only blocks the calling (GUI) thread until that already-bounded
        work completes, because PySide6/Qt aborts the process outright if a
        QThread's C++ object is destroyed while its underlying OS thread is
        still alive (confirmed empirically: closing the window immediately
        after starting a refresh reproduced a real SIGABRT with "QThread:
        Destroyed while thread '' is still running" -- exactly the class of
        "prints passed, exits nonzero" teardown bug the task spec's Qt
        Teardown Debt section warns about). Every Git subprocess A1/A2
        launches carries its own hard timeout, so this wait is always
        bounded -- never an indefinite shutdown hang.
        """
        self._valid = False
        self._generation += 1
        self._selection_seq += 1
        for worker in list(self._workers):
            worker.wait()

    def is_valid(self) -> bool:
        return self._valid

    def current_snapshot_ref(self) -> Optional[str]:
        return self._current_snapshot_ref

    def _track(self, worker: QThread) -> None:
        self._workers.append(worker)

        def _forget():
            try:
                self._workers.remove(worker)
            except ValueError:
                pass
        worker.finished.connect(_forget)

    # ── capture / refresh ─────────────────────────────────────────────

    def refresh(self, target_identity: TargetIdentity) -> None:
        if not self._valid:
            return
        self._generation += 1
        gen = self._generation
        self._current_gen_is_stale = False
        self._current_snapshot_ref = None
        self.state_changed.emit(REFRESHING, ())
        worker = _CaptureWorker(target_identity, gen)
        worker.result_ready.connect(self._on_capture_result)
        self._track(worker)
        worker.start()

    def _on_capture_result(self, outcome: CaptureOutcome) -> None:
        if not self._valid or outcome.generation != self._generation:
            return
        if outcome.state not in _LIVE_STATES:
            self._current_gen_is_stale = True
            self.state_changed.emit(outcome.state, outcome.reasons)
            return
        if self._current_gen_is_stale:
            return
        self._current_snapshot_ref = outcome.handle.snapshot_ref
        self.state_changed.emit(outcome.state, outcome.reasons)
        self.snapshot_ready.emit(outcome.handle.snapshot)

    # ── file retrieval ────────────────────────────────────────────────

    def select_file(self, change_id: str, layer: str, start_hunk_index: int = 0) -> None:
        if not self._valid or self._current_snapshot_ref is None:
            return
        self._selection_seq += 1
        seq = self._selection_seq
        gen = self._generation
        worker = _RetrievalWorker(
            self._current_snapshot_ref, change_id, layer, start_hunk_index, gen, seq,
        )
        worker.result_ready.connect(self._on_retrieval_result)
        self._track(worker)
        worker.start()

    def load_more(self, change_id: str, layer: str, start_hunk_index: int) -> None:
        """Pagination is authorized exactly like a fresh selection -- same
        generation/selection-seq/staleness gate. The panel is responsible
        for appending rather than replacing when it already holds hunks
        for this change_id/layer."""
        self.select_file(change_id, layer, start_hunk_index)

    def _on_retrieval_result(self, outcome: RetrievalOutcome) -> None:
        if (
            not self._valid
            or outcome.generation != self._generation
            or outcome.selection_seq != self._selection_seq
        ):
            return
        if outcome.state not in _LIVE_STATES:
            self._current_gen_is_stale = True
            self.state_changed.emit(outcome.state, outcome.reasons)
            return
        if self._current_gen_is_stale:
            return
        self.file_ready.emit(outcome)
