"""
core/context_transaction.py -- CONTEXT-LIFECYCLE-A5I: transactional
deliberate context reconstruction.

Consumes an already-READY checkpoint (core/context_checkpoints.py, A3) to
replace a chat's live in-memory working set (core/context.py's
ContextManager.history) with a durable-spine reconstruction (core/
context_reconstruction.py, A2) plus one synthetic continuity message
rendered from the checkpoint's provenance-separated payload (core/
continuity_compiler.py, A4). This module owns none of A2/A3/A4's own
correctness -- it consumes each verbatim and adds exactly one thing none of
them provide: a governing transaction that revalidates every time-sensitive
fact immediately before installing the candidate, so the swap is atomic
against every race a concurrent foreground turn, chat switch, manual
compaction, or emergency stop could otherwise cause.

Design lineage: CONTEXT-LIFECYCLE-A5D (LUMINA_CONTEXT_TRANSACTIONAL_
RECONSTRUCTION_DESIGN_2026-08-31.md). No design decision documented there is
re-litigated here; only implemented. Live source was re-verified against
that document's own file/line citations immediately before this module was
written and found unchanged.

Governing transaction shape (see deliberate_reconstruct()):
    capture (chat_id, generation, epoch)
        -> build candidate off-side (no lock, no ctx mutation, may be slow)
            -> enter a tiny swap critical section
                -> revalidate every captured fact against current state
                    -> atomic install (one rebind + two cache resets)
                        -> advance generation
            -> release
        -> emit bounded telemetry (outside the lock)

The old ContextManager.history remains authoritative, byte-for-byte
untouched, until and unless every revalidation check inside the critical
section passes. Any single failure aborts the whole transaction with a
typed exception; no partial install, no automatic retry, no silent
fallback to a durable-only reconstruction.

Generation ownership: `generation_owner` (this module's only external
collaborator, duck-typed, never imported) must expose:
    current_chat_id() -> int          # the chat currently selected
    current_generation() -> int       # ContextGeneration.current()
    bump() -> int                     # ContextGeneration.bump()
    live_ctx() -> core.context.ContextManager   # the live, long-lived ctx

This module never imports Qt and never imports ui.main_window -- the
protocol above is exactly the surface ui/main_window.py's LuminaWindow
implements (see CONTEXT-LIFECYCLE-A5I's wiring there), but any object
satisfying it works, matching A2/A3/A4's own "neutral kernel" precedent.
"""
import threading
import time
from dataclasses import dataclass

from core import emergency_stop
from core.context import estimate_message_tokens
from core.context_checkpoints import (
    STATE_READY,
    CheckpointNotFound,
    get_checkpoint,
    get_latest_usable_checkpoint,
)
from core.context_reconstruction import reconstruct_chat_context

# ---------------------------------------------------------------------
# Failure taxonomy (A5D section 11)
# ---------------------------------------------------------------------

class ReconstructionError(Exception):
    """Base class for every error this module raises."""


class NoUsableCheckpoint(ReconstructionError):
    """get_latest_usable_checkpoint() returned None for this chat."""


class CheckpointNotReady(ReconstructionError):
    """The selected checkpoint is not READY (raced to FAILED/SUPERSEDED, or
    was never READY -- a caller-input bug if this ever fires on a checkpoint
    just returned by get_latest_usable_checkpoint())."""


class CheckpointChatMismatch(ReconstructionError):
    """CONTEXT-LIFECYCLE-A6P1: an explicitly requested checkpoint_id exists
    but belongs to a different chat than the one this transaction was
    invoked for. Only reachable via deliberate_reconstruct_checkpoint() --
    get_latest_usable_checkpoint() (legacy selection) already scopes its
    query to the requested chat_id, so this can never fire there."""


class CheckpointStale(ReconstructionError):
    """The durable spine fingerprint no longer matches the checkpoint's
    frozen binding, observed either during off-side preparation or at the
    swap-boundary re-check."""


class ActiveTurnConflict(ReconstructionError):
    """A foreground turn is currently active for this chat (emergency_stop
    foreground_turn lease present)."""


class ActiveChatChanged(ReconstructionError):
    """The generation owner's current chat_id no longer matches the chat
    this transaction was invoked for."""


class ContextGenerationChanged(ReconstructionError):
    """The chat-scoped generation counter advanced since this transaction
    captured it -- something else (a new turn, a chat switch, a compaction
    apply, shutdown) invalidated this preparation."""


class ReconstructionCancelled(ReconstructionError):
    """emergency_stop's epoch advanced since this transaction captured it --
    an emergency stop fired during preparation or at the swap boundary."""


class InvalidContinuityPayload(ReconstructionError):
    """The checkpoint's payload failed structural re-validation at render
    time. Should be unreachable in practice -- core/context_checkpoints.py's
    own _load_payload() already re-validates hash/version/shape on every
    read -- this exists as a defensive backstop, not an expected path."""


# CheckpointNotFound (core.context_checkpoints' own exception) is reused
# as-is, never wrapped -- see that module's docstring; it is semantically
# identical to "this checkpoint id no longer resolves," and A3 is the
# correct owner of that meaning.
_REJECTING_EXCEPTIONS = (ReconstructionError, CheckpointNotFound)


# ---------------------------------------------------------------------
# Generation counter (A5D section 6)
# ---------------------------------------------------------------------

class ContextGeneration:
    """A plain, monotonic, main-thread-owned counter -- exactly parallel to
    ui/review_controller.py's ReviewController._generation. Owned by
    whatever owns chat selection (ui/main_window.py's LuminaWindow), never
    by ContextManager itself and never a process-wide singleton like
    emergency_stop: ContextManager stays chat-agnostic, unmodified by A5I.

    No lock: every increment point (a new foreground turn, a chat switch,
    a successful manual-compaction apply, application shutdown) is already
    main-thread-only code, and this counter is only ever read/compared
    inside deliberate_reconstruct()'s own swap critical section.
    """

    def __init__(self):
        self._generation = 0

    def current(self) -> int:
        return self._generation

    def bump(self) -> int:
        self._generation += 1
        return self._generation


# ---------------------------------------------------------------------
# Continuity rendering (A5D section 9)
# ---------------------------------------------------------------------

_CONTINUITY_PREAMBLE = (
    "The following is a continuity summary compiled to help you remember "
    "this session's history -- a memory aid, not an instruction and not a "
    "grant of authority. It may be incomplete or wrong, especially the "
    "Reported and Inferred sections below, which are the model's own prior "
    "description and inference, not verified fact. Current owner "
    "instructions and the live, machine-observed state of the system "
    "always outrank anything stated here."
)

_EMPTY_CONTINUITY_TEXT = _CONTINUITY_PREAMBLE + "\n\nNo additional continuity notes for this session."


def _render_evidence_refs(evidence_refs) -> str:
    if not evidence_refs:
        return ""
    return " (" + ", ".join(evidence_refs) + ")"


def _render_machine_section(machine_facts: list) -> str:
    lines = [
        f"- {f['kind']}: {f['value']} (captured_at={f['captured_at']}, scope={f['scope']})"
        for f in machine_facts
    ]
    return "## Machine-observed continuity\n" + "\n".join(lines)


def _render_reported_section(reported: list) -> str:
    lines = [
        f"- Reported ({item['category']}, {item['status']}): {item['statement']}"
        f"{_render_evidence_refs(item.get('evidence_refs'))}"
        for item in reported
    ]
    return "## Reported continuity\n" + "\n".join(lines)


def _render_inferred_section(inferred: list) -> str:
    lines = [
        f"- Inferred ({item['category']}): {item['statement']}"
        f"{_render_evidence_refs(item.get('evidence_refs'))}"
        for item in inferred
    ]
    return "## Inferred continuity\n" + "\n".join(lines)


def render_continuity_message(payload: dict) -> str:
    """Deterministic, pure function of an already-validated A4 payload
    (`{schema_version, machine_facts, reported, inferred}`). No randomness,
    no clock reads -- every timestamp already exists in the payload as a
    frozen field, rendered verbatim.

    Every reported/inferred item is rendered as a description ("Reported:
    ..."/"Inferred: ..."), never as an imperative -- A4's own schema
    validation already structurally prevents an authorization-shaped
    category (A4-PIN-01 excludes "authorization_boundary"); this renderer
    additionally never strips the "Reported:"/"Inferred:" framing that
    keeps every item legible as a description, not a command.

    Raises InvalidContinuityPayload if the payload is missing a required
    top-level key or a section item is missing a required field -- this
    should be unreachable given core/context_checkpoints.py's own
    _load_payload() re-validation on every read, and exists only as a
    defensive backstop.
    """
    try:
        machine_facts = payload["machine_facts"]
        reported = payload["reported"]
        inferred = payload["inferred"]
    except (KeyError, TypeError) as e:
        raise InvalidContinuityPayload(f"continuity payload missing required field: {e}") from e

    try:
        sections = []
        if machine_facts:
            sections.append(_render_machine_section(machine_facts))
        if reported:
            sections.append(_render_reported_section(reported))
        if inferred:
            sections.append(_render_inferred_section(inferred))
    except KeyError as e:
        raise InvalidContinuityPayload(f"continuity payload item missing required field: {e}") from e

    if not sections:
        return _EMPTY_CONTINUITY_TEXT
    return _CONTINUITY_PREAMBLE + "\n\n" + "\n\n".join(sections)


# ---------------------------------------------------------------------
# Telemetry (A5D section 13)
# ---------------------------------------------------------------------

def _history_tokens(history: list) -> int:
    return sum(estimate_message_tokens(m) for m in history)


def _emit_requested(recorder, chat_id):
    if recorder is None:
        from core.flight_recorder import get_recorder
        recorder = get_recorder()
    recorder.record_machine_event("context.reconstruction.requested", chat_id=chat_id)


def _emit_rejected(recorder, chat_id, checkpoint_id, failure_class, severity):
    if recorder is None:
        from core.flight_recorder import get_recorder
        recorder = get_recorder()
    recorder.record_machine_event(
        "context.reconstruction.rejected",
        chat_id=chat_id,
        severity=severity,
        fields={"checkpoint_id": checkpoint_id, "failure_class": failure_class},
    )


def _emit_swapped(recorder, chat_id, checkpoint_id, old_history, candidate_history,
                   durable_row_count, generation_before, generation_after, elapsed_ms):
    if recorder is None:
        from core.flight_recorder import get_recorder
        recorder = get_recorder()
    recorder.record_machine_event(
        "context.reconstruction.swapped",
        chat_id=chat_id,
        fields={
            "checkpoint_id": checkpoint_id,
            "old_entry_count": len(old_history),
            "new_entry_count": len(candidate_history),
            "old_estimated_tokens": _history_tokens(old_history),
            "new_estimated_tokens": _history_tokens(candidate_history),
            "durable_row_count": durable_row_count,
            "generation_before": generation_before,
            "generation_after": generation_after,
            "elapsed_ms": elapsed_ms,
        },
    )


# A5D section 11's retryable/internal-invariant classification: every
# failure class here is "no" under "Internal invariant violation?" except
# InvalidContinuityPayload ("yes -- should not be reachable given A3's own
# validation; if seen, log loudly") -- so InvalidContinuityPayload alone
# gets severity="error"; every other rejection is an expected, benign race.
_INTERNAL_INVARIANT_CLASSES = frozenset({"InvalidContinuityPayload"})


# ---------------------------------------------------------------------
# The governing transaction (A5D section 10)
# ---------------------------------------------------------------------

_swap_lock = threading.Lock()


@dataclass(frozen=True)
class ReconstructResult:
    chat_id: int
    checkpoint_id: int
    generation_before: int
    generation_after: int
    old_entry_count: int
    new_entry_count: int
    durable_row_count: int


def _active_turn_exists(chat_id) -> bool:
    """Reuses emergency_stop's existing per-turn foreground_turn lease
    (core/agent.py's chat() wraps every foreground turn in
    execution_scope(kind="foreground_turn", metadata={"chat_id": ...})) as
    the active-turn signal -- no new admission primitive in core/agent.py,
    per A5D section 6/16."""
    snap = emergency_stop.snapshot()
    for execution in snap["active_executions"]:
        if execution["kind"] != "foreground_turn":
            continue
        if execution.get("metadata", {}).get("chat_id") == chat_id:
            return True
    return False


def _select_latest_checkpoint(chat_id: int):
    """Off-side selection for deliberate_reconstruct()'s legacy/current
    callers -- exactly the lookup A5I has always used, unchanged by
    CONTEXT-LIFECYCLE-A6P1."""
    checkpoint = get_latest_usable_checkpoint(chat_id)
    if checkpoint is None:
        raise NoUsableCheckpoint(f"no usable READY checkpoint for chat {chat_id}")
    if checkpoint.state != STATE_READY:
        raise CheckpointNotReady(f"checkpoint {checkpoint.id} is {checkpoint.state}, not READY")
    return checkpoint


def _select_exact_checkpoint(chat_id: int, checkpoint_id: int):
    """Off-side selection for CONTEXT-LIFECYCLE-A6P1's exact-ID entry point
    (deliberate_reconstruct_checkpoint()). Never calls
    get_latest_usable_checkpoint() -- fetches exactly the row the caller
    asked for (get_checkpoint() raises CheckpointNotFound if it doesn't
    exist) and verifies chat ownership, READY state, and payload validity
    before it may become the transaction's candidate. Durable-spine
    fingerprint and context_skip agreement against this exact record are
    verified by the caller immediately after this returns -- see
    _deliberate_reconstruct()'s strict_context_skip_offside branch and its
    shared off-side fingerprint check, both compared against this same
    `checkpoint`, never a substitute."""
    checkpoint = get_checkpoint(checkpoint_id)
    if checkpoint.chat_id != chat_id:
        raise CheckpointChatMismatch(
            f"checkpoint {checkpoint_id} belongs to chat {checkpoint.chat_id}, not {chat_id}"
        )
    if checkpoint.state != STATE_READY:
        raise CheckpointNotReady(f"checkpoint {checkpoint.id} is {checkpoint.state}, not READY")
    if checkpoint.payload is None:
        raise InvalidContinuityPayload(
            f"checkpoint {checkpoint.id} payload failed validation during selection"
        )
    return checkpoint


def _deliberate_reconstruct(
    chat_id: int,
    generation_owner,
    *,
    recorder,
    select_checkpoint,
    checkpoint_id_hint,
    strict_context_skip_offside,
) -> ReconstructResult:
    """Shared A5 transaction body for both public entry points below --
    deliberate_reconstruct() (A5I, latest-usable selection) and
    deliberate_reconstruct_checkpoint() (CONTEXT-LIFECYCLE-A6P1, exact-ID
    selection). They differ only in `select_checkpoint` (how the off-side
    candidate is chosen -- see _select_latest_checkpoint()/
    _select_exact_checkpoint() above), `checkpoint_id_hint` (the ID to
    report in rejection telemetry when selection itself fails before a
    CheckpointRecord exists, e.g. a missing or wrong-chat explicit ID --
    None for legacy callers, since "latest usable" has no such ID to
    report), and `strict_context_skip_offside` (an extra off-side
    context_skip agreement check, exact-ID mode only -- see
    deliberate_reconstruct_checkpoint()'s docstring for why legacy mode
    deliberately does not gain this extra check). Every other line below --
    capture, the off-side fingerprint check, candidate rendering, the swap
    critical section and everything it revalidates, telemetry -- is
    unchanged from A5I and runs identically regardless of which selector
    supplied `checkpoint`.

    Returns a ReconstructResult on success. Raises a ReconstructionError
    subclass (or propagates core.context_checkpoints.CheckpointNotFound) on
    any failure -- in every failure case, `generation_owner.live_ctx().
    history` is guaranteed untouched: the exact same list object it was
    before this call, since the only rebind (the atomic-install step) is
    unconditionally the last mutation performed, after every check above it
    has already passed.

    A programming-bug exception raised by rendering (e.g. a malformed
    payload past InvalidContinuityPayload's own defensive check) is
    deliberately NOT caught here -- it propagates uncaught, exactly as
    A5D's test matrix requires, since ctx.history is never touched before
    the swap section regardless of where such an exception originates.
    """
    _emit_requested(recorder, chat_id)

    captured_chat_id = generation_owner.current_chat_id()
    if captured_chat_id != chat_id:
        _emit_rejected(recorder, chat_id, checkpoint_id_hint, "ActiveChatChanged", "warning")
        raise ActiveChatChanged(f"generation owner's active chat is {captured_chat_id}, not {chat_id}")
    captured_generation = generation_owner.current_generation()
    captured_epoch = emergency_stop.current_epoch()

    checkpoint_id_for_telemetry = checkpoint_id_hint
    try:
        # ---- build off-side: no lock held, no ctx mutation ----
        checkpoint = select_checkpoint()
        checkpoint_id_for_telemetry = checkpoint.id

        reconstruction = reconstruct_chat_context(chat_id)
        if reconstruction.durable_spine_fingerprint != checkpoint.durable_spine_fingerprint:
            raise CheckpointStale(f"checkpoint {checkpoint.id} spine fingerprint stale during preparation")
        if strict_context_skip_offside and reconstruction.context_skip != checkpoint.context_skip:
            raise CheckpointStale(
                f"checkpoint {checkpoint.id} context_skip ({checkpoint.context_skip}) disagrees "
                f"with the current resolved context_skip ({reconstruction.context_skip}) during "
                "off-side preparation"
            )

        continuity_text = render_continuity_message(checkpoint.payload)
        candidate_history = list(reconstruction.messages) + [
            {"role": "assistant", "content": continuity_text}
        ]

        swap_started = None
        # ---- enter the tiny swap/admission critical section ----
        with _swap_lock:
            swap_started = time.monotonic()

            if generation_owner.current_chat_id() != chat_id:
                raise ActiveChatChanged(f"active chat changed away from {chat_id} before swap")
            if generation_owner.current_generation() != captured_generation:
                raise ContextGenerationChanged(
                    f"generation advanced from {captured_generation} before swap"
                )
            if _active_turn_exists(chat_id):
                raise ActiveTurnConflict(f"a foreground turn is active for chat {chat_id}")
            if emergency_stop.current_epoch() != captured_epoch:
                raise ReconstructionCancelled(
                    f"emergency-stop epoch advanced from {captured_epoch} before swap"
                )

            fresh_checkpoint = get_checkpoint(checkpoint.id)
            if fresh_checkpoint.state != STATE_READY:
                raise CheckpointStale(f"checkpoint {checkpoint.id} is no longer READY at swap time")
            fresh_reconstruction = reconstruct_chat_context(chat_id)
            if fresh_reconstruction.durable_spine_fingerprint != checkpoint.durable_spine_fingerprint:
                raise CheckpointStale(f"checkpoint {checkpoint.id} spine fingerprint stale at swap time")
            # A5I-R1: independently obtain the current context_skip through
            # the same strict/fail-closed call (reconstruct_chat_context()
            # with context_skip=None resolves via latest_manual_compaction_
            # skip() directly -- raises on a read failure rather than core.
            # context_reconstruction.resolve_context_skip()'s graceful
            # degrade-to-zero) and compare it against the checkpoint's own
            # freshly re-fetched context_skip column. begin_checkpoint()'s
            # own contract always writes durable_spine_fingerprint and
            # context_skip together as one matched pair from a single
            # ReconstructionResult; nothing in the normal begin/finalize
            # API can make them disagree. This rejects the one case that
            # can: a row whose context_skip column was corrupted or
            # otherwise written outside that API -- caught here even though
            # the fingerprint alone still matches, and even though the
            # candidate content this transaction installs (built from
            # `reconstruction` off-side, not from the checkpoint's stored
            # context_skip) would still have been byte-correct regardless.
            if fresh_reconstruction.context_skip != fresh_checkpoint.context_skip:
                raise CheckpointStale(
                    f"checkpoint {checkpoint.id} context_skip ({fresh_checkpoint.context_skip}) "
                    f"disagrees with the current resolved context_skip ({fresh_reconstruction.context_skip})"
                )
            if fresh_checkpoint.payload is None:
                raise InvalidContinuityPayload(f"checkpoint {checkpoint.id} payload failed re-validation at swap time")

            # ---- atomic install: one rebind, two required companion resets ----
            ctx = generation_owner.live_ctx()
            old_history = ctx.history
            ctx.history = candidate_history
            ctx._last_usage_snapshot = None
            ctx._pending_compaction = []

            # ---- advance generation, still inside the lock ----
            generation_after = generation_owner.bump()
            elapsed_ms = (time.monotonic() - swap_started) * 1000.0
    except _REJECTING_EXCEPTIONS as e:
        failure_class = type(e).__name__
        severity = "error" if failure_class in _INTERNAL_INVARIANT_CLASSES else "warning"
        _emit_rejected(recorder, chat_id, checkpoint_id_for_telemetry, failure_class, severity)
        raise

    _emit_swapped(
        recorder, chat_id, checkpoint.id, old_history, candidate_history,
        reconstruction.restored_row_count, captured_generation, generation_after, elapsed_ms,
    )
    return ReconstructResult(
        chat_id=chat_id,
        checkpoint_id=checkpoint.id,
        generation_before=captured_generation,
        generation_after=generation_after,
        old_entry_count=len(old_history),
        new_entry_count=len(candidate_history),
        durable_row_count=reconstruction.restored_row_count,
    )


def deliberate_reconstruct(chat_id: int, generation_owner, *, recorder=None) -> ReconstructResult:
    """Run the full A5I transaction for `chat_id` against `generation_owner`
    (see this module's docstring for the required protocol), selecting
    whichever checkpoint get_latest_usable_checkpoint(chat_id) currently
    considers usable. Unchanged behavior from A5I -- see
    _deliberate_reconstruct() for the shared mechanics this now delegates
    to, and deliberate_reconstruct_checkpoint() below for the exact-ID
    alternative CONTEXT-LIFECYCLE-A6P1 adds alongside this."""
    return _deliberate_reconstruct(
        chat_id, generation_owner,
        recorder=recorder,
        select_checkpoint=lambda: _select_latest_checkpoint(chat_id),
        checkpoint_id_hint=None,
        strict_context_skip_offside=False,
    )


def deliberate_reconstruct_checkpoint(
    chat_id: int, checkpoint_id: int, generation_owner, *, recorder=None
) -> ReconstructResult:
    """CONTEXT-LIFECYCLE-A6P1: consume exactly `checkpoint_id`, or fail --
    never any other checkpoint. Closes A6D Blocker B2: unlike
    deliberate_reconstruct(), which independently re-derives "latest usable"
    and may therefore select a different same-spine checkpoint than the one
    a caller (e.g. A6's coordinator, immediately after a fresh A4 compile)
    just prepared, this entry point takes the checkpoint identity as a hard
    input. get_latest_usable_checkpoint() is never called anywhere in this
    path (see _select_exact_checkpoint()) -- if `checkpoint_id` is missing,
    wrong-chat, not READY (FAILED/SUPERSEDED/BUILDING), has an invalid
    payload, or its durable-spine binding (fingerprint or context_skip) no
    longer matches the current live state at ANY point before the swap
    (off-side preparation or the in-lock final re-check), the whole
    transaction fails and `generation_owner.live_ctx().history` is left
    untouched -- even if another, newer, perfectly valid READY checkpoint
    for the same chat/spine exists. The caller decides what happens next;
    this function never silently substitutes one checkpoint for another.

    Gains one extra off-side check relative to deliberate_reconstruct():
    strict_context_skip_offside compares the freshly resolved context_skip
    against `checkpoint.context_skip` before candidate rendering begins,
    per A6D section 3's exact-ID selection contract. deliberate_reconstruct()
    deliberately does NOT gain this same check -- adding it there would
    reorder an existing, already-tested A5I protection (its off-side pass
    currently checks only the fingerprint; the corrupted-context_skip case
    is caught by the in-lock recheck, which both entry points still run
    unconditionally) for a caller that never asked for stricter off-side
    behavior. Preserving deliberate_reconstruct()'s exact existing check
    order is CONTEXT-LIFECYCLE-A6P1's explicit mandate, not an oversight.

    Every other A5I protection -- off-side candidate construction,
    active-chat/generation/foreground-turn/emergency-epoch revalidation at
    the swap boundary, a fresh in-lock re-fetch of this SAME checkpoint_id
    (never "latest"), fresh strict fingerprint/context_skip/payload
    re-validation against that fresh re-fetch, one atomic history rebind,
    usage-cache and pending-compaction reset, generation advancement,
    bounded telemetry, and whole-transaction failure atomicity -- applies
    identically to this entry point, via the same shared
    _deliberate_reconstruct() body deliberate_reconstruct() itself uses.
    """
    return _deliberate_reconstruct(
        chat_id, generation_owner,
        recorder=recorder,
        select_checkpoint=lambda: _select_exact_checkpoint(chat_id, checkpoint_id),
        checkpoint_id_hint=checkpoint_id,
        strict_context_skip_offside=True,
    )
