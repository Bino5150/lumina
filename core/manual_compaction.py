"""Owner-triggered manual context compaction.

The chat transcript is never rewritten or annotated. A manual compaction writes
one normal nightstand Drawer carrying both the summary and a `context-skip:N`
tag. That makes the summary and its reload checkpoint one atomic Palace write;
undoing that Drawer also rolls the checkpoint back naturally.

Provenance (COMPACTION-CONTEXT-SKIP-PROVENANCE-01). `context-skip:N` is
reconstruction metadata, and honouring it makes reconstruction OMIT N durable
rows from live context -- so it is an authority, and authority is never taken
from what a record says about itself. Tags and text are what a record CLAIMS to
be; `palace_remember` accepts any wing / room / tags a model chooses, so a
model-native drawer can reproduce every tag of a genuine checkpoint exactly.
Only the trusted `origin = 'manual_compaction'` stamp (PALACE-GUARD-01B-2) says
what actually wrote a drawer, and no model-facing path can set it.
latest_manual_compaction_skip() therefore authorizes a checkpoint only when
_checkpoint_skip() proves all of that; every doubt resolves to "skip nothing"
(restore more history), never to "skip more".
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
# The trusted-synthesis stamp run_manual_compaction() writes and the only
# provenance that can authorize a skip (same closed vocabulary as
# tools.palace.SYNTHESIZED_ORIGINS; test_manual_compaction pins the two together).
MANUAL_COMPACTION_ORIGIN = "manual_compaction"
# No real transcript has 10**15 rows. Bounding the digit string keeps int()
# away from CPython's int<->str digit-limit ValueError on absurd input.
_MAX_SKIP_DIGITS = 15


def _cancelled(cancel_event) -> bool:
    return bool(cancel_event is not None and cancel_event.is_set())


def _parse_skip_value(text: str):
    """The canonical, non-negative decimal integer run_manual_compaction()
    emits (`str(int)`: "0", "6", "10"), else None. int() alone would also
    accept signs, padding, underscores, surrounding whitespace and non-ASCII
    digits; a trusted record carrying any of those is not something the
    trusted writer produced, so it is refused rather than normalised."""
    if not (text.isascii() and text.isdecimal()) or len(text) > _MAX_SKIP_DIGITS:
        return None
    if len(text) > 1 and text[0] == "0":
        return None
    return int(text)


def _checkpoint_skip(row, chat_id: int):
    """The skip ONE drawer authorizes for `chat_id`, or None if it authorizes
    nothing. The caller has already required the trusted origin stamp and this
    chat's nightstand room; this is the rest of what makes a drawer a live,
    genuine checkpoint (the consistency checks mirror palace._undo_refusal):

    - still eligible: it sits in a closet that exists and is not withheld. A
      withheld (quarantined) or missing closet means its summary is no longer
      injected, so the rows it replaced must not stay omitted either;
    - tags are a JSON list of strings naming this checkpoint kind and THIS
      chat, with exactly one `context-skip:` marker (duplicates are ambiguous);
    - N is a canonical integer that leaves at least one durable conversation
      row live. The writer always keeps the newest turns, so N >= the row count
      is impossible for a genuine checkpoint and would restore nothing.
    """
    if row["closet_id"] is None or row["withheld_reason"] is not None:
        return None
    try:
        tags = json.loads(row["tags"] or "[]")
    except (TypeError, ValueError):
        return None
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        return None
    if MANUAL_COMPACTION_TAG not in tags or f"session:{chat_id}" not in tags:
        return None
    markers = [t for t in tags if t.startswith(CONTEXT_SKIP_TAG_PREFIX)]
    if len(markers) != 1:
        return None
    skip = _parse_skip_value(markers[0][len(CONTEXT_SKIP_TAG_PREFIX):])
    if skip is None or skip >= row["conversation_rows"]:
        return None
    return skip


def latest_manual_compaction_skip(chat_id: int) -> int:
    """The durable persisted-row cutoff authorized by this chat's TRUSTED
    manual-compaction checkpoints, or 0.

    Only drawers stamped `origin = 'manual_compaction'` in nightstand/<chat_id>
    are candidates; a tag-only lookalike (a model-native write, a clone, a
    legacy drawer written before the stamp existed -- origin NULL) is
    indistinguishable from a forgery and authorizes nothing. Legacy
    checkpoints are not migrated, guessed at or destroyed: they simply stop
    omitting history, and the owner's next /compact mints a trusted checkpoint
    covering the whole prefix.

    Among the eligible checkpoints the highest N wins. The lifecycle only ever
    writes a checkpoint whose N exceeds every earlier one, so the highest N IS
    the latest -- and unlike row order it cannot be gamed by ids, timestamps or
    insertion order. Undoing the newest checkpoint therefore rolls the skip
    back to the previous eligible one, and undoing them all restores 0.
    """
    conn = get_palace_db()
    try:
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(palace_drawers)")}
        if "origin" not in columns:
            # A database from before PALACE-GUARD-01B-2: no drawer can carry
            # the trusted stamp yet, so none is a trusted checkpoint.
            return 0
        rows = conn.execute("""
            SELECT d.tags, d.closet_id, c.withheld_reason,
                   (SELECT COUNT(*) FROM chat_messages m
                     WHERE m.chat_id = ? AND m.role IN ('user', 'assistant'))
                       AS conversation_rows
            FROM palace_drawers d
            JOIN palace_rooms r ON d.room_id = r.id
            JOIN palace_wings w ON r.wing_id = w.id
            LEFT JOIN palace_closets c ON d.closet_id = c.id
            WHERE w.name = 'nightstand'
              AND r.name = ?
              AND d.origin = ?
        """, (chat_id, str(chat_id), MANUAL_COMPACTION_ORIGIN)).fetchall()
    finally:
        conn.close()

    skips = (_checkpoint_skip(row, chat_id) for row in rows)
    return max((s for s in skips if s is not None), default=0)


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
            origin="manual_compaction",  # PALACE-GUARD-01B-2 trusted synthesis stamp
            # REDDIT-INGRESS-AUTHORITY-01: every compaction summary is
            # model-authored derived state, not an owner command.  The current
            # incremental slice can contain a Lumina paraphrase whose original
            # external source was compacted earlier, so scanning only this
            # slice cannot prove owner provenance.
            untrusted=True,
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
