"""core/context_inventory.py -- CONTEXT-LIFECYCLE-A1

Read-only observation/accounting for the active context lifecycle (the
research question behind LUMINA_CONTEXT_RECONSTRUCTION_IMPLEMENTATION_
BLUEPRINT_2026-08-30.md). Nothing in this module mutates ContextManager.
history, changes ui/main_window.py::_load_chat() or core/manual_compaction.
py behavior, injects anything into context, or persists raw conversation
content anywhere. It only measures what is already true and reports it.

Message-class taxonomy is NOT invented here -- it mirrors exactly the
message shapes core/context.py's own add_user()/add_assistant()/
add_tool_call()/add_tool_result()/add_cancelled_tool_result() already
produce (confirmed by direct read during CONTEXT-LIFECYCLE-A0, not
guessed). Think and Commentary are never ContextManager entries at all
(ui/main_window.py's _on_think_*/_on_commentary handlers only touch the
UI bubble widget, never self.agent.ctx -- see A0's finding #3/#4) --
inventory_active_history() reports that truthfully as "not resident"
rather than inventing a fake zero-token history row for them.
"""
import hashlib

from core.context import estimate_message_tokens
from core.db import connect

MESSAGE_CLASSES = ("user", "assistant_final", "assistant_tool_call", "tool_result", "other")

# Classes whose messages are EVER persisted by ordinary chat persistence
# (tools/memory.py::save_chat_message() is only ever called with
# role="user" or role="assistant" -- confirmed by grepping every call site
# in ui/main_window.py during A0; there is no code path that saves a
# role="tool" row). This is a class-level fact, not a per-instance
# guarantee -- see inventory_active_history()'s docstring below.
_DURABLE_CLASSES = ("user", "assistant_final")
_LIVE_ONLY_CLASSES = ("assistant_tool_call", "tool_result", "other")


def classify_message(msg: dict) -> str:
    """Return one of MESSAGE_CLASSES for a ContextManager.history entry.

    "other" only fires for a shape none of ContextManager's own add_*()
    methods produce today -- if this ever triggers on real traffic it
    means this classifier has gone stale against a context.py change, not
    that a genuinely new class is expected in practice.
    """
    role = msg.get("role")
    if role == "user":
        return "user"
    if role == "assistant":
        return "assistant_tool_call" if msg.get("tool_calls") else "assistant_final"
    if role == "tool":
        return "tool_result"
    return "other"


def inventory_active_history(history: list) -> dict:
    """Read-only counts + estimated tokens by class over a live
    ContextManager.history list. Never mutates `history`.

    `durable_class_entries`/`live_only_class_entries` classify by whether
    an entry's CLASS is ever persisted by ordinary chat persistence, not
    whether this specific instance has already been written to disk at the
    moment of measurement -- the most recent user message in a
    still-in-flight turn is a real example of the same class, not yet
    flushed. This function has no way to know write timing and does not
    pretend to.

    think_resident/commentary_resident are always False: Think and
    Commentary are structurally never ContextManager entries (see module
    docstring), so this reports their absence explicitly rather than
    omitting them or giving them a fake zero-token row indistinguishable
    from "measured and found empty."
    """
    by_class = {c: {"count": 0, "tokens": 0} for c in MESSAGE_CLASSES}
    for msg in history:
        cls = classify_message(msg)
        by_class[cls]["count"] += 1
        by_class[cls]["tokens"] += estimate_message_tokens(msg)

    total_tokens = sum(v["tokens"] for v in by_class.values())
    durable_class_entries = sum(by_class[c]["count"] for c in _DURABLE_CLASSES)
    live_only_class_entries = sum(by_class[c]["count"] for c in _LIVE_ONLY_CLASSES)

    return {
        "by_class": by_class,
        "total_entries": len(history),
        "total_tokens": total_tokens,
        "durable_class_entries": durable_class_entries,
        "live_only_class_entries": live_only_class_entries,
        "think_resident": False,
        "commentary_resident": False,
    }


def _load_durable_conversation_rows(chat_id: int) -> list:
    """Read-only query against chat_messages, ordered exactly like
    tools/memory.py::load_chat_messages() (ORDER BY created_at, no
    secondary tiebreak -- matched deliberately for reconstruction-order
    parity, not "improved" with an id tiebreak that function doesn't use).

    Selects `id` in addition to that function's own columns -- needed as
    this module's fingerprint row-identity anchor. A local query rather
    than adding `id` to load_chat_messages()'s shared return shape: that
    would be a safe additive change, but A1's mission is the smallest
    possible read-only footprint, and this observation has no need to
    touch a function three other subsystems already depend on.
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
    bug in this function, not a design choice -- see required proof #7.
    """
    eligible = []
    conversation_index = 0
    for row in rows:
        if row["role"] not in ("user", "assistant"):
            continue
        restore = conversation_index >= context_skip
        conversation_index += 1
        if not row["content"]:
            continue
        if restore:
            eligible.append(row)
    return eligible


def durable_spine_fingerprint(chat_id: int, context_skip: int = None) -> dict:
    """Deterministic SHA-256 over the exact durable user/assistant rows
    _load_chat() would restore into ctx.history right now for this chat,
    honoring core/manual_compaction.py's context-skip:N state.

    This is an OBSERVATION FINGERPRINT ONLY -- comparing it before/after a
    future reconstruction operation tells you whether the reconstructable
    spine changed underneath you. It is not authority, not validation
    evidence, and not an authorization signal (blueprint section 7.2 /
    CONTEXT-LIFECYCLE-A0's trust-model carryover).

    `context_skip` defaults to the chat's live latest_manual_compaction_
    skip() value; pass an explicit value to compare against a different
    skip state without a live DB round-trip.
    """
    if context_skip is None:
        from core.manual_compaction import latest_manual_compaction_skip
        context_skip = latest_manual_compaction_skip(chat_id)

    rows = _load_durable_conversation_rows(chat_id)
    eligible = eligible_durable_rows(rows, context_skip)

    h = hashlib.sha256()
    for row in eligible:
        h.update(str(row["id"]).encode("utf-8"))
        h.update(b"\x00")
        h.update(row["role"].encode("utf-8"))
        h.update(b"\x00")
        h.update(row["content"].encode("utf-8"))
        h.update(b"\x00")
        h.update(row["metadata"].encode("utf-8"))
        h.update(b"\x1e")

    return {
        "chat_id": chat_id,
        "context_skip": context_skip,
        "durable_row_count": len(rows),
        "eligible_row_count": len(eligible),
        "fingerprint_sha256": h.hexdigest(),
    }


def observe_reconstruction_boundary(history: list, chat_id: int, *,
                                     context_skip: int = None) -> dict:
    """One-shot convenience: inventory a live history list plus the
    durable spine fingerprint for `chat_id`, bundled together for a
    before/after comparison at a reconstruction boundary. Read-only on
    both inputs."""
    return {
        "inventory": inventory_active_history(history),
        "spine": durable_spine_fingerprint(chat_id, context_skip=context_skip),
    }


def record_inventory_event(recorder, *, chat_id, inventory: dict, spine: dict,
                            boundary_reason: str, turn_id: str = None) -> None:
    """Emit one context.inventory.snapshot Flight Recorder machine event.

    Fields are exclusively counts, token estimates, and the spine's SHA-256
    hex digest -- never raw message content (mission: "No raw message
    dump"). core/flight_recorder.py's own _looks_like_conversation()
    sanitizer would reject a raw history dump regardless (defense in
    depth), but this function does not attempt to pass one in the first
    place.

    `recorder`: an explicit FlightRecorder instance (required for test
    isolation, matching tests/test_flight_recorder.py's own convention) or
    None to use the production singleton via
    core.flight_recorder.get_recorder() -- same posture core/agent.py's
    own _fr_machine() helper uses.
    """
    if recorder is None:
        from core.flight_recorder import get_recorder
        recorder = get_recorder()

    fields = {
        "by_class": {
            cls: {"count": v["count"], "tokens": v["tokens"]}
            for cls, v in inventory["by_class"].items()
        },
        "total_entries": inventory["total_entries"],
        "total_tokens": inventory["total_tokens"],
        "durable_class_entries": inventory["durable_class_entries"],
        "live_only_class_entries": inventory["live_only_class_entries"],
        "think_resident": inventory["think_resident"],
        "commentary_resident": inventory["commentary_resident"],
        "durable_row_count": spine["durable_row_count"],
        "eligible_row_count": spine["eligible_row_count"],
        "context_skip": spine["context_skip"],
        "spine_fingerprint_sha256": spine["fingerprint_sha256"],
        "boundary_reason": boundary_reason,
    }
    recorder.record_machine_event(
        "context.inventory.snapshot", chat_id=chat_id, turn_id=turn_id, fields=fields,
    )
