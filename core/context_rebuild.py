"""
core/context_rebuild.py -- CONTEXT-GC-01/A6I: owner-triggered same-chat
working-context rebuild coordinator.

Qt-free trusted coordinator for the `/context rebuild` command. Owns the
lifecycle/business truth: pre-flight admission (defense-in-depth only --
ui/main_window.py's `_command_context_rebuild()` owns the primary,
UI-visible admission checks, exactly mirroring `_command_compact()`'s own
established pattern), holding one emergency_stop execution lease across
*both* checkpoint compilation and transactional reconstruction, compiling a
FRESH continuity checkpoint (core/continuity_compiler.py, A4) every single
call -- never reusing an older usable one -- and consuming that EXACT
checkpoint id, never "latest usable", through the existing A5/A6P1
transactional swap (core/context_transaction.py). Produces a
machine-grounded before/after receipt; never claims success from model
narration.

This module is the first real caller of
core.context_transaction.deliberate_reconstruct_checkpoint() (A6P1) and of
core.continuity_compiler.compile_continuity_checkpoint()'s cancel_event
(A6P2) -- both existed with zero callers anywhere in ui/main_window.py
before A6I. It never imports Qt and never imports ui.main_window; the only
external collaborator is `generation_owner`, the exact duck-typed protocol
core/context_transaction.py already documents (current_chat_id/
current_generation/bump/live_ctx) and ui/main_window.py's LuminaWindow
already implements.

There is no model-facing entry point into this module anywhere in the
codebase, and there must never be one -- see A6I's mission scroll section 8.
"""
from dataclasses import dataclass
from typing import Optional

from core import context_inventory, emergency_stop
from core.context import estimate_message_tokens
from core.context_checkpoints import STATE_READY
from core.context_transaction import (
    ActiveChatChanged,
    ActiveTurnConflict,
    CheckpointChatMismatch,
    CheckpointNotFound,
    CheckpointNotReady,
    CheckpointStale,
    ContextGenerationChanged,
    InvalidContinuityPayload,
    ReconstructionCancelled,
    deliberate_reconstruct_checkpoint,
)
from core.continuity_compiler import (
    CompilationCancelled,
    StaleSpine,
    compile_continuity_checkpoint,
)

# ---------------------------------------------------------------------
# Outcome taxonomy -- mirrors the mission scroll's UX vocabulary (section
# 19) as machine-checkable status strings, never inferred from prose.
# ---------------------------------------------------------------------

STATUS_SUCCESS = "success"
STATUS_REJECTED = "rejected"
STATUS_CANCELLED = "cancelled"
STATUS_COMPILE_FAILED = "compile_failed"
STATUS_STALE = "stale"
STATUS_CHAT_CHANGED = "chat_changed"
STATUS_EMERGENCY_STOP = "emergency_stop"
STATUS_ERROR = "error"

_TERMINAL_STATUSES = frozenset({
    STATUS_SUCCESS, STATUS_REJECTED, STATUS_CANCELLED, STATUS_COMPILE_FAILED,
    STATUS_STALE, STATUS_CHAT_CHANGED, STATUS_EMERGENCY_STOP, STATUS_ERROR,
})


@dataclass(frozen=True)
class RebuildReceipt:
    """Machine-grounded before/after receipt. Every field here is either a
    literal input, a value returned by trusted machine code (A2/A3/A4/A5),
    or a plain arithmetic derivation of those -- never assistant narration.
    `status` is always one of the STATUS_* constants above; a test may
    assert membership in _TERMINAL_STATUSES to catch a typo introducing an
    unrecognized status string."""
    status: str
    reason: str
    chat_id: Optional[int] = None
    checkpoint_id: Optional[int] = None
    generation_before: Optional[int] = None
    generation_after: Optional[int] = None
    pre_history_count: Optional[int] = None
    post_history_count: Optional[int] = None
    pre_estimated_tokens: Optional[int] = None
    post_estimated_tokens: Optional[int] = None
    durable_spine_fingerprint_before: Optional[str] = None
    durable_spine_fingerprint_after: Optional[str] = None
    durable_row_count: Optional[int] = None

    def __post_init__(self):
        if self.status not in _TERMINAL_STATUSES:
            raise ValueError(f"RebuildReceipt.status {self.status!r} is not a recognized status")


def _history_tokens(history: list) -> int:
    return sum(estimate_message_tokens(m) for m in history)


def _spine_fingerprint(chat_id: int) -> str:
    return context_inventory.durable_spine_fingerprint(chat_id)["fingerprint_sha256"]


def _emit(recorder, event_type: str, chat_id, **fields):
    if recorder is None:
        from core.flight_recorder import get_recorder
        recorder = get_recorder()
    recorder.record_machine_event(event_type, chat_id=chat_id, fields=fields)


def _rejected(chat_id, reason, *, status=STATUS_REJECTED, recorder=None, checkpoint_id=None,
              pre_history_count=None, pre_tokens=None, spine_before=None):
    _emit(recorder, "context.rebuild.rejected", chat_id, reason=reason, status=status)
    return RebuildReceipt(
        status=status, reason=reason, chat_id=chat_id, checkpoint_id=checkpoint_id,
        pre_history_count=pre_history_count, pre_estimated_tokens=pre_tokens,
        durable_spine_fingerprint_before=spine_before,
    )


def run_rebuild(
    chat_id: int,
    generation_owner,
    backend,
    ephemeral_history: list,
    *,
    cancel_event=None,
    expected_epoch: int = None,
    project_context=None,
    recorder=None,
) -> RebuildReceipt:
    """Run one full `/context rebuild` cycle for `chat_id` and return a
    RebuildReceipt -- never raises for any expected failure mode (admission
    rejection, cooperative cancel, compile failure, staleness, chat/
    generation races, emergency stop); a genuinely unexpected exception from
    compile or transaction is caught and reported as STATUS_ERROR rather
    than propagating into a caller's background-thread crash, matching the
    established `_command_compact()` `_run()` catch-all convention.

    `backend`, `ephemeral_history`, `cancel_event`, `expected_epoch`, and
    `project_context` are passed straight through to
    core.continuity_compiler.compile_continuity_checkpoint() -- see that
    function's docstring for their exact contracts. `ephemeral_history` must
    already be a snapshot the caller took before calling this (this
    function never reads a live ContextManager directly for compilation
    input; it only reads generation_owner.live_ctx() for the receipt's
    pre/post history accounting and to hand deliberate_reconstruct_
    checkpoint() the generation_owner it needs).

    Defense-in-depth admission checks below duplicate a subset of what
    ui/main_window.py's `_command_context_rebuild()` already checks
    synchronously before ever starting a worker thread that calls this
    function (no active chat, emergency stop latched, automatic compaction
    running) -- this function re-checks them because it is a public,
    independently-testable API, not because the Qt caller is expected to
    skip its own checks. Checks only the Qt layer can see (a second /context
    rebuild or /compact already in flight, tracked as plain instance
    attributes on LuminaWindow) are NOT duplicated here; see that method's
    own docstring.
    """
    if not chat_id:
        return _rejected(chat_id, "Rebuild rejected: no active chat.", recorder=recorder)

    ctx = generation_owner.live_ctx()
    pre_history = list(ctx.history)
    pre_tokens = _history_tokens(pre_history)
    spine_before = _spine_fingerprint(chat_id)
    # Captured once, immediately, before compile even starts -- see the
    # post-compile revalidation below. deliberate_reconstruct_checkpoint()
    # only captures/compares generation and chat_id from ITS OWN invocation
    # onward; without this earlier capture, a chat-switch-and-back (or any
    # other bump-then-settle) purely during THIS function's own compile
    # phase would be invisible to that later check, even though it is
    # exactly the kind of "something else invalidated this preparation"
    # event the generation counter exists to catch.
    captured_chat_id = generation_owner.current_chat_id()
    captured_generation = generation_owner.current_generation()

    if emergency_stop.is_latched():
        return _rejected(
            chat_id, "Rebuild rejected: emergency stop is active.", recorder=recorder,
            pre_history_count=len(pre_history), pre_tokens=pre_tokens, spine_before=spine_before,
        )
    if getattr(ctx, "_compacting", False):
        return _rejected(
            chat_id, "Rebuild rejected: automatic context compaction is running.", recorder=recorder,
            pre_history_count=len(pre_history), pre_tokens=pre_tokens, spine_before=spine_before,
        )

    _emit(recorder, "context.rebuild.requested", chat_id)

    try:
        with emergency_stop.execution_scope(
            kind="context_reconstruction", label=str(chat_id), expected_epoch=expected_epoch,
        ):
            _emit(recorder, "context.rebuild.compile_started", chat_id)
            try:
                checkpoint = compile_continuity_checkpoint(
                    chat_id, backend, ephemeral_history,
                    project_context=project_context, expected_epoch=expected_epoch,
                    cancel_event=cancel_event,
                )
            except CompilationCancelled as e:
                _emit(recorder, "context.rebuild.compile_finished", chat_id, outcome="cancelled")
                return _rejected(
                    chat_id, "Rebuild cancelled. Live context unchanged.",
                    status=STATUS_CANCELLED, recorder=recorder,
                    pre_history_count=len(pre_history), pre_tokens=pre_tokens, spine_before=spine_before,
                )
            except StaleSpine:
                _emit(recorder, "context.rebuild.compile_finished", chat_id, outcome="stale_spine")
                return _rejected(
                    chat_id, "Checkpoint compile failed: durable spine changed during compile. "
                    "Live context unchanged.",
                    status=STATUS_COMPILE_FAILED, recorder=recorder,
                    pre_history_count=len(pre_history), pre_tokens=pre_tokens, spine_before=spine_before,
                )

            if checkpoint.state != STATE_READY:
                _emit(
                    recorder, "context.rebuild.compile_finished", chat_id,
                    outcome="failed", failure_reason=checkpoint.failure_reason,
                )
                reason = checkpoint.failure_reason or "unknown"
                if reason.startswith("cancelled_"):
                    return _rejected(
                        chat_id, "Emergency stop invalidated reconstruction. Live context unchanged.",
                        status=STATUS_EMERGENCY_STOP, recorder=recorder, checkpoint_id=checkpoint.id,
                        pre_history_count=len(pre_history), pre_tokens=pre_tokens, spine_before=spine_before,
                    )
                return _rejected(
                    chat_id, f"Checkpoint compile failed: {reason}. Live context unchanged.",
                    status=STATUS_COMPILE_FAILED, recorder=recorder, checkpoint_id=checkpoint.id,
                    pre_history_count=len(pre_history), pre_tokens=pre_tokens, spine_before=spine_before,
                )

            _emit(recorder, "context.rebuild.compile_finished", chat_id, outcome="ready",
                  checkpoint_id=checkpoint.id)

            if (generation_owner.current_chat_id() != captured_chat_id
                    or generation_owner.current_generation() != captured_generation):
                return _rejected(
                    chat_id, "Chat changed before swap. Live context unchanged.",
                    status=STATUS_CHAT_CHANGED, recorder=recorder, checkpoint_id=checkpoint.id,
                    pre_history_count=len(pre_history), pre_tokens=pre_tokens, spine_before=spine_before,
                )

            try:
                result = deliberate_reconstruct_checkpoint(
                    chat_id, checkpoint.id, generation_owner, recorder=recorder,
                )
            except ActiveTurnConflict:
                return _rejected(
                    chat_id, "Rebuild rejected: foreground turn active. Live context unchanged.",
                    recorder=recorder, checkpoint_id=checkpoint.id,
                    pre_history_count=len(pre_history), pre_tokens=pre_tokens, spine_before=spine_before,
                )
            except (ActiveChatChanged, ContextGenerationChanged):
                return _rejected(
                    chat_id, "Chat changed before swap. Live context unchanged.",
                    status=STATUS_CHAT_CHANGED, recorder=recorder, checkpoint_id=checkpoint.id,
                    pre_history_count=len(pre_history), pre_tokens=pre_tokens, spine_before=spine_before,
                )
            except ReconstructionCancelled:
                return _rejected(
                    chat_id, "Emergency stop invalidated reconstruction. Live context unchanged.",
                    status=STATUS_EMERGENCY_STOP, recorder=recorder, checkpoint_id=checkpoint.id,
                    pre_history_count=len(pre_history), pre_tokens=pre_tokens, spine_before=spine_before,
                )
            except (CheckpointStale, CheckpointNotReady, CheckpointChatMismatch, CheckpointNotFound):
                return _rejected(
                    chat_id, "Checkpoint became stale before swap. Live context unchanged.",
                    status=STATUS_STALE, recorder=recorder, checkpoint_id=checkpoint.id,
                    pre_history_count=len(pre_history), pre_tokens=pre_tokens, spine_before=spine_before,
                )
            except InvalidContinuityPayload:
                return _rejected(
                    chat_id, "Checkpoint payload failed validation before swap. Live context unchanged.",
                    status=STATUS_ERROR, recorder=recorder, checkpoint_id=checkpoint.id,
                    pre_history_count=len(pre_history), pre_tokens=pre_tokens, spine_before=spine_before,
                )
    except (emergency_stop.EmergencyStopActive, emergency_stop.StaleExecution):
        return _rejected(
            chat_id, "Rebuild rejected: emergency stop is active. Live context unchanged.",
            status=STATUS_EMERGENCY_STOP, recorder=recorder,
            pre_history_count=len(pre_history), pre_tokens=pre_tokens, spine_before=spine_before,
        )
    except Exception as e:
        return _rejected(
            chat_id, f"Unexpected rebuild failure: {e}. Live context unchanged.",
            status=STATUS_ERROR, recorder=recorder,
            pre_history_count=len(pre_history), pre_tokens=pre_tokens, spine_before=spine_before,
        )

    post_history = list(generation_owner.live_ctx().history)
    spine_after = _spine_fingerprint(chat_id)
    receipt = RebuildReceipt(
        status=STATUS_SUCCESS,
        reason="Rebuild complete.",
        chat_id=chat_id,
        checkpoint_id=result.checkpoint_id,
        generation_before=result.generation_before,
        generation_after=result.generation_after,
        pre_history_count=result.old_entry_count,
        post_history_count=result.new_entry_count,
        pre_estimated_tokens=pre_tokens,
        post_estimated_tokens=_history_tokens(post_history),
        durable_spine_fingerprint_before=spine_before,
        durable_spine_fingerprint_after=spine_after,
        durable_row_count=result.durable_row_count,
    )
    _emit(
        recorder, "context.rebuild.finished", chat_id,
        checkpoint_id=result.checkpoint_id,
        pre_history_count=receipt.pre_history_count,
        post_history_count=receipt.post_history_count,
        pre_estimated_tokens=receipt.pre_estimated_tokens,
        post_estimated_tokens=receipt.post_estimated_tokens,
    )
    return receipt
