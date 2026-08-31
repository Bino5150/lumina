"""core/context_reconstruction.py -- CONTEXT-LIFECYCLE-A2

Neutral, non-Qt kernel for the question ordinary chat reconstruction has
always answered inline inside ui/main_window.py::_load_chat(): "given this
chat's persisted rows and its current manual-compaction skip state, what
exact durable conversation history should become active?"

This module has no Qt dependency, no MainWindow dependency, performs no
rendering, no model call, no tool dispatch, no writes, and no compaction.
It reads already-persisted rows and core/manual_compaction.py's existing
context-skip state, and returns an immutable candidate -- it does not
mutate any live ContextManager. Callers (today: _load_chat(); later: A5)
decide what to do with the candidate.

Row-eligibility truth: `eligible_durable_rows()` used to live in
core/context_inventory.py (CONTEXT-LIFECYCLE-A1). It moved here because
A1 is a read-only *observation/telemetry* module -- it should depend on
the reconstruction truth, not the other way around. Making the hot path
(_load_chat(), every chat open) depend on a module whose own docstring
says "read-only observation/accounting" for its core correctness would
be the wrong dependency direction. core/context_inventory.py now
re-exports this function and delegates durable_spine_fingerprint() to
reconstruct_chat_context() below, so there is exactly one eligibility
implementation and exactly one fingerprint implementation.
"""
from dataclasses import dataclass, field
import hashlib

from core.context import ContextManager
from core.db import connect

CONVERSATION_ROLES = ("user", "assistant")


def load_durable_rows(chat_id: int) -> list:
    """Read-only query against chat_messages, ordered exactly like
    tools/memory.py::load_chat_messages() (ORDER BY created_at, no
    secondary tiebreak -- matched deliberately for reconstruction-order
    parity, not "improved" with an id tiebreak that function doesn't use).

    Selects `id` in addition to that function's own columns -- needed as
    this module's fingerprint row-identity anchor. `metadata` is returned
    as the raw stored string (normalized None -> ""), not JSON-parsed --
    ordinary reconstruction never reads message metadata (confirmed: the
    only place a persisted-message metadata field is written today is
    the {"cancelled": True} tag in ui/main_window.py::_on_cancelled(),
    and _load_chat() never reads it back), but it must stay load-bearing
    for the fingerprint so a cancelled-tag mutation is still detectable.
    """
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, role, content, metadata FROM chat_messages "
            "WHERE chat_id=? ORDER BY created_at",
            (chat_id,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"id": r["id"], "role": r["role"], "content": r["content"],
         "metadata": r["metadata"] or ""}
        for r in rows
    ]


def eligible_durable_rows(rows: list, context_skip: int) -> list:
    """Reproduce ui/main_window.py::_load_chat()'s exact eligibility logic
    for which durable rows actually re-enter ctx.history:

    - only role in ("user", "assistant") is ever restored (no elif branch
      exists for any other role, so any other role -- none occur in
      practice today -- is silently never restored regardless of skip);
    - `conversation_index` counts every conversation-class row in order,
      checked against context_skip BEFORE incrementing for the current
      row (a 0-indexed "skip the first N conversational rows" cut);
    - an empty-content row still consumes a conversation_index slot even
      though it is never itself restored -- CONTEXT-LIFECYCLE-A0 confirmed
      this exact ordering in the live source, so it is reproduced here
      rather than "corrected."

    Any divergence from _load_chat()'s real behavior here is a parity
    bug in this function, not a design choice.
    """
    eligible = []
    conversation_index = 0
    for row in rows:
        if row["role"] not in CONVERSATION_ROLES:
            continue
        restore = conversation_index >= context_skip
        conversation_index += 1
        if not row["content"]:
            continue
        if restore:
            eligible.append(row)
    return eligible


def _durable_spine_hash(rows: list) -> str:
    """SHA-256 over exactly the rows passed in, in order. Row-shape and
    field order must match core/context_inventory.py's pre-A2
    durable_spine_fingerprint() byte-for-byte, or every existing
    fingerprint changes underneath chats that already recorded one."""
    h = hashlib.sha256()
    for row in rows:
        h.update(str(row["id"]).encode("utf-8"))
        h.update(b"\x00")
        h.update(row["role"].encode("utf-8"))
        h.update(b"\x00")
        h.update(row["content"].encode("utf-8"))
        h.update(b"\x00")
        h.update(row["metadata"].encode("utf-8"))
        h.update(b"\x1e")
    return h.hexdigest()


def resolve_context_skip(chat_id: int) -> int:
    """Resolve chat_id's current manual-compaction skip with the exact
    graceful-degradation _load_chat() has always used on a checkpoint
    read failure: log and fall back to 0 rather than crashing chat load.

    This is a caller-invoked helper, not reconstruct_chat_context()'s own
    default-resolution path (see there) -- core/context_inventory.py's
    durable_spine_fingerprint() deliberately does not go through this
    helper. It lets a read failure raise instead, its exact pre-A2
    contract, which no live caller depends on tolerating today. Both
    paths still resolve through the same latest_manual_compaction_skip()
    -- this only wraps the failure handling, not the row-selection truth.
    """
    from core.manual_compaction import latest_manual_compaction_skip
    try:
        return latest_manual_compaction_skip(chat_id)
    except Exception as e:
        print(f"[COMPACTION] checkpoint read failed for chat {chat_id}: {e}", flush=True)
        return 0


@dataclass(frozen=True)
class ReconstructionResult:
    """Everything a caller needs to install a candidate durable history
    without re-reading or reinterpreting the database.

    `rows` carries every durable row for this chat, unfiltered -- callers
    that need the full persisted transcript for display (e.g. _load_chat's
    visible-bubble restoration, which shows more rows than ever re-enter
    ctx.history) use this instead of issuing a second, separately-shaped
    query for the same underlying data.
    `messages` is built by replaying eligible_rows through a throwaway
    ContextManager's real add_user()/add_assistant() -- not by
    hand-assembling {"role", "content"} dicts -- so any future
    normalization those methods grow is picked up here automatically
    instead of silently drifting out of parity.
    """
    chat_id: int
    context_skip: int
    rows: list = field(default_factory=list)
    eligible_rows: list = field(default_factory=list)
    messages: list = field(default_factory=list)
    restored_row_count: int = 0
    skipped_row_count: int = 0
    durable_spine_fingerprint: str = ""


def reconstruct_chat_context(chat_id: int, context_skip: int = None) -> ReconstructionResult:
    """Build a candidate durable history for `chat_id`. Read-only: issues
    one SELECT against chat_messages (via load_durable_rows) and, when
    context_skip is not given, one read of manual-compaction state (which
    raises straight through on failure here -- callers that need
    graceful degradation, like _load_chat(), resolve via
    resolve_context_skip() first and pass the result in explicitly).
    Never touches a live ContextManager -- constructs its own throwaway
    instance so a caller can discover the candidate before committing to
    it (no destructive clear-then-discover step required)."""
    if context_skip is None:
        from core.manual_compaction import latest_manual_compaction_skip
        context_skip = latest_manual_compaction_skip(chat_id)

    rows = load_durable_rows(chat_id)
    eligible = eligible_durable_rows(rows, context_skip)

    candidate_ctx = ContextManager()
    for row in eligible:
        if row["role"] == "user":
            candidate_ctx.add_user(row["content"])
        else:
            candidate_ctx.add_assistant(row["content"])

    conversation_row_count = sum(1 for r in rows if r["role"] in CONVERSATION_ROLES)
    restored_row_count = len(eligible)

    return ReconstructionResult(
        chat_id=chat_id,
        context_skip=context_skip,
        rows=rows,
        eligible_rows=eligible,
        messages=candidate_ctx.history,
        restored_row_count=restored_row_count,
        skipped_row_count=conversation_row_count - restored_row_count,
        durable_spine_fingerprint=_durable_spine_hash(eligible),
    )
