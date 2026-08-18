"""Owner-triggered manual context compaction.

The chat transcript is never rewritten or annotated. A manual compaction writes
one normal nightstand Drawer carrying both the summary and a `context-skip:N`
tag. That makes the summary and its reload checkpoint one atomic Palace write;
undoing that Drawer also rolls the checkpoint back naturally.
"""
import json

from core.context import estimate_message_tokens
from core.dreaming import COMPACTION_PROMPT, run_summarization_call
from core.operator_commands import (
    chunk_compaction_history,
    compaction_cut_index,
    persisted_compaction_skip_count,
)
from tools.memory import load_chat_messages
from tools.palace import get_db as get_palace_db, palace_store


KEEP_USER_TURNS = 2
SUMMARY_CHUNK_CHARS = 5500
SUMMARY_MAX_TOKENS = 300
MANUAL_COMPACTION_TAG = "manual-compaction"
CONTEXT_SKIP_TAG_PREFIX = "context-skip:"


def _cancelled(cancel_event) -> bool:
    return bool(cancel_event is not None and cancel_event.is_set())


def latest_manual_compaction_skip(chat_id: int) -> int:
    """Return the durable persisted-row cutoff encoded in this chat's Drawers."""
    conn = get_palace_db()
    try:
        rows = conn.execute("""
            SELECT d.tags
            FROM palace_drawers d
            JOIN palace_rooms r ON d.room_id = r.id
            JOIN palace_wings w ON r.wing_id = w.id
            WHERE w.name = 'nightstand'
              AND r.name = ?
              AND d.tags LIKE ?
            ORDER BY d.created_at DESC, d.id DESC
        """, (str(chat_id), f'%"{MANUAL_COMPACTION_TAG}"%')).fetchall()
    finally:
        conn.close()

    skip = 0
    for row in rows:
        try:
            tags = json.loads(row["tags"] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        for tag in tags:
            if not isinstance(tag, str) or not tag.startswith(CONTEXT_SKIP_TAG_PREFIX):
                continue
            try:
                skip = max(skip, max(0, int(tag[len(CONTEXT_SKIP_TAG_PREFIX):])))
            except ValueError:
                continue
    return skip


def _summarize_chunks(chunks: list[str], cancel_event=None):
    """Map/reduce bounded chunks; return (summary, error)."""
    if not chunks:
        return "", "No textual history was available to summarize."

    current = list(chunks)
    while True:
        summaries = []
        for chunk in current:
            if _cancelled(cancel_event):
                return None, "cancelled"
            summary = run_summarization_call(
                chunk, prompt=COMPACTION_PROMPT, max_tokens=SUMMARY_MAX_TOKENS
            )
            if not summary or not str(summary).strip():
                return None, "The summarizer returned no usable result."
            summaries.append(str(summary).strip())

        if _cancelled(cancel_event):
            return None, "cancelled"

        joined = "\n".join(summaries).strip()
        if len(joined) <= SUMMARY_CHUNK_CHARS or len(summaries) == 1:
            return joined, None

        current = chunk_compaction_history(
            [{"role": "summary", "content": s} for s in summaries],
            max_chars=SUMMARY_CHUNK_CHARS,
        )


def run_manual_compaction(history_snapshot: list, chat_id: int, cancel_event=None) -> dict:
    """Summarize durable transcript rows that are about to leave live context.

    The persisted transcript is the preservation authority: summarize exactly
    the previously-uncompacted user/assistant rows before the newest two user
    turns. Raw tool-call/tool-result payloads are deliberately excluded because
    chat persistence never restores them today, and feeding untrusted
    TOOL_OUTPUT into a trusted Palace summary could launder provenance.

    This function does NOT mutate ContextManager.history. The UI may switch
    chats while the job runs; its completion handler prunes only if the same
    chat and exact history snapshot are still live.
    """
    snapshot = list(history_snapshot or [])
    cut = compaction_cut_index(snapshot, keep_user_turns=KEEP_USER_TURNS)
    if cut is None or cut <= 0:
        return {"status": "nothing_to_compact", "chat_id": chat_id}

    compacted = snapshot[:cut]
    retained = snapshot[cut:]

    try:
        persisted = load_chat_messages(chat_id)
        previous_skip = latest_manual_compaction_skip(chat_id)
    except Exception as e:
        return {
            "status": "error", "chat_id": chat_id,
            "error": f"Compaction state read failed: {e}",
        }

    conversational = [m for m in persisted if m.get("role") in ("user", "assistant")]
    new_skip = max(
        previous_skip,
        persisted_compaction_skip_count(persisted, keep_user_turns=KEEP_USER_TURNS),
    )
    if new_skip <= previous_skip:
        return {"status": "nothing_to_compact", "chat_id": chat_id}

    durable_prefix = conversational[previous_skip:new_skip]
    chunks = chunk_compaction_history(durable_prefix, max_chars=SUMMARY_CHUNK_CHARS)
    if not chunks:
        return {
            "status": "error", "chat_id": chat_id,
            "error": "No textual history was available to summarize.",
        }

    before_tokens = sum(estimate_message_tokens(m) for m in snapshot)
    compacted_tokens = sum(estimate_message_tokens(m) for m in compacted)

    summary, error = _summarize_chunks(chunks, cancel_event=cancel_event)
    if error == "cancelled":
        return {"status": "cancelled", "chat_id": chat_id}
    if error:
        return {"status": "error", "chat_id": chat_id, "error": error}

    # Last cancellation boundary before the single atomic Palace write.
    if _cancelled(cancel_event):
        return {"status": "cancelled", "chat_id": chat_id}

    try:
        palace_store(
            content=summary,
            wing="nightstand",
            room=str(chat_id),
            layer=2,
            tags=[
                MANUAL_COMPACTION_TAG,
                f"session:{chat_id}",
                f"{CONTEXT_SKIP_TAG_PREFIX}{new_skip}",
            ],
        )
    except Exception as e:
        return {
            "status": "error", "chat_id": chat_id,
            "error": f"Palace write failed: {e}",
        }

    return {
        "status": "success",
        "chat_id": chat_id,
        "history_snapshot": snapshot,
        "retained_history": retained,
        "compacted_messages": len(compacted),
        "compacted_persisted_rows": new_skip - previous_skip,
        "compacted_tokens": compacted_tokens,
        "before_history_tokens": before_tokens,
        "skip_conversation_messages": new_skip,
    }
