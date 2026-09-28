"""
Memory Tools — persistent memory across sessions via SQLite.
Write-through to MemPalace on save. Flat table preserved for migration + fallback.
"""

import sqlite3
import json
from datetime import datetime
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config


def get_db():
    from core.db import connect
    return connect()


def init_memory_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT DEFAULT 'general',
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            untrusted INTEGER NOT NULL DEFAULT 0
        )
    """)
    try:
        conn.execute(
            "ALTER TABLE memories ADD COLUMN untrusted INTEGER NOT NULL DEFAULT 0"
        )
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            raise
    conn.commit()
    conn.close()


# Label → palace wing mapping
LABEL_TO_WING = {
    "people":      "people",
    "person":      "people",
    "project":     "projects",
    "projects":    "projects",
    "preference":  "preferences",
    "preferences": "preferences",
    "prefs":       "preferences",
    "identity":    "identity",
    "session":     "sessions",
    "sessions":    "sessions",
    "discovery":   "sessions",
    "fact":        "sessions",
    "advice":      "sessions",
}

# Label → palace hall mapping (cross-cutting streams)
LABEL_TO_HALL = {
    "discovery":   "discoveries",
    "advice":      "advice",
    "preference":  "preferences",
    "preferences": "preferences",
    "prefs":       "preferences",
    "fact":        "facts",
    "event":       "events",
}


def save_memory(content: str, label: str = "general", *, untrusted: bool = True) -> str:
    """Save a memory. Also writes to palace with AAAK compression.

    CASTLE-WALLS-REPAIR-03 / C8 finding 2: untrusted now defaults to True
    (fail-safe). A durable memory write is lower-trust unless a caller
    that is actually entitled to make that determination explicitly says
    otherwise. Before this, the default was untrusted=False -- the
    model-callable registered tool always overrode it to True explicitly,
    but ui/settings/memory_tab.py's _paste_import() (owner pastes/imports
    content copied from elsewhere -- a different provenance domain, same
    reasoning as R1A's dropped files) called this directly and silently
    inherited the permissive default, storing copied content as fully
    trusted system-prompt material (see
    tests/test_castle_walls_adversarial_c8.py). Only
    ui/settings/memory_tab.py's _add_memory() -- a single field the owner
    is directly typing into, in Settings, GUI-only, no import/paste
    involved -- explicitly passes untrusted=False now."""
    if len(content) > 512:
        content = content[:512]

    # PALACE-GUARD-01B-1: the flat row and every Palace derivative it produces
    # (drawer + rolling-closet segment, plus a hall when the label maps to
    # one) are ONE write transaction. The flat row's own id is captured and
    # stamped on each derivative as source_memory_id, so later lifecycle
    # operations follow a recorded link instead of guessing by content.
    # Previously the flat row committed first and a Palace failure was
    # swallowed as a successful save (the source of flat memories with no
    # Palace copy); now it's all or nothing, and a failure says so.
    from tools.palace import _store_in_conn, _store_hall_in_conn
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT INTO memories (label, content, created_at, untrusted) VALUES (?, ?, ?, ?)",
            (label, content, datetime.now().isoformat(), 1 if untrusted else 0)
        )
        memory_id = cur.lastrowid
        wing = LABEL_TO_WING.get(label.lower(), "sessions")
        room = label.lower() if label != "general" else "general"
        result = _store_in_conn(
            conn, content, wing, room, 2, None, True, untrusted,
            source_memory_id=memory_id,
        )
        # Also drop into a hall if this label maps to one
        hall = LABEL_TO_HALL.get(label.lower())
        if hall:
            _store_hall_in_conn(conn, content, hall, 2, untrusted,
                                source_memory_id=memory_id)
        conn.commit()
    except Exception as e:
        conn.rollback()
        return f"[Error: memory not saved ({type(e).__name__}); nothing was written.]"
    finally:
        conn.close()

    compressed_preview = (result["compressed"] or "")[:80]
    return f"Memory saved [{label}]. Compressed: {compressed_preview}"


def search_memory(query: str, label: str = None) -> str:
    """Search memories by keyword, optionally filtered by label."""
    conn = get_db()
    if label:
        rows = conn.execute(
            "SELECT id, label, content, created_at FROM memories WHERE label=? AND content LIKE ? ORDER BY created_at DESC LIMIT 10",
            (label, f"%{query}%")
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, label, content, created_at FROM memories WHERE content LIKE ? ORDER BY created_at DESC LIMIT 10",
            (f"%{query}%",)
        ).fetchall()
    conn.close()
    if not rows:
        # Fall through to palace recall if flat search empty
        try:
            from tools.palace import palace_recall
            return palace_recall(query)
        except Exception:
            return f"No memories found for '{query}'."
    return "\n".join(f"[{r['id']}] ({r['label']}) {r['content']}" for r in rows)


def get_recent_memories(limit: int = 5, label: str = None) -> str:
    """Get most recent memories, optionally filtered by label."""
    conn = get_db()
    if label:
        rows = conn.execute(
            "SELECT id, label, content, created_at FROM memories WHERE label=? ORDER BY created_at DESC LIMIT ?",
            (label, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, label, content, created_at FROM memories ORDER BY created_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
    conn.close()
    if not rows:
        return "No memories stored yet."
    return "\n".join(f"[{r['id']}] ({r['label']}) {r['content']}" for r in rows)


LEGACY_UNLINKED_NOTICE = (
    "It predates deterministic Palace linkage, so no Palace copy was "
    "inferred or removed."
)


def delete_memory_with_derivatives(memory_id: int) -> dict:
    """PALACE-GUARD-01B-2: THE deletion path for a flat memory -- used by
    Settings > Memory > Delete and by the approved delete_memory pending
    action alike. One BEGIN IMMEDIATE transaction:

    1. every drawer linked by source_memory_id is removed, and its closet
       settled under tools.palace._remove_drawer_in_conn()'s law (delete /
       exact rebuild / withhold -- never string surgery, never a re-render
       of unrelated drawers);
    2. every hall linked by source_memory_id is removed (halls are atomic);
    3. the flat row is deleted LAST (the source_memory_id FK makes any
       other order fail).

    Any failure rolls the whole thing back. A memory with no linked
    derivatives -- one written before linkage existed -- is deleted flat-only
    and reported as legacy_unlinked: nothing is matched by text, hash or
    meaning, and no candidate is touched.

    Returns {"found", "memory_id", "drawers_removed", "halls_removed",
    "closets": {outcome: count}, "legacy_unlinked"}.
    """
    from tools.palace import _remove_drawer_in_conn
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM memories WHERE id=?", (memory_id,)).fetchone() is None:
            conn.rollback()
            return {"found": False, "memory_id": memory_id}
        # A DB with no Palace (or one not yet migrated to source links) can't
        # hold a linked derivative; it's handled exactly like legacy state.
        linkable = {
            t for t in ("palace_drawers", "palace_halls")
            if conn.execute("SELECT 1 FROM pragma_table_info(?) WHERE name='source_memory_id'",
                            (t,)).fetchone()
        }
        drawer_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM palace_drawers WHERE source_memory_id=? ORDER BY id", (memory_id,))
        ] if "palace_drawers" in linkable else []
        closets = {}
        for drawer_id in drawer_ids:
            outcome = _remove_drawer_in_conn(conn, drawer_id)
            closets[outcome] = closets.get(outcome, 0) + 1
        halls_removed = conn.execute(
            "DELETE FROM palace_halls WHERE source_memory_id=?", (memory_id,)
        ).rowcount if "palace_halls" in linkable else 0
        conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "found": True,
        "memory_id": memory_id,
        "drawers_removed": len(drawer_ids),
        "halls_removed": halls_removed,
        "closets": closets,
        "legacy_unlinked": not drawer_ids and not halls_removed,
    }


def describe_memory_deletion(result: dict) -> str:
    """One owner-facing sentence for a delete_memory_with_derivatives() result."""
    mid = result["memory_id"]
    if not result.get("found"):
        return f"Memory {mid} not found."
    if result["legacy_unlinked"]:
        return f"Memory {mid} deleted. {LEGACY_UNLINKED_NOTICE}"
    parts = [f"Memory {mid} deleted with its linked Palace copies "
             f"({result['drawers_removed']} drawer(s), {result['halls_removed']} hall(s))."]
    withheld = result["closets"].get("closet_withheld", 0)
    if withheld:
        parts.append(f"{withheld} shared closet(s) couldn't be rebuilt exactly and are "
                     "withheld from Lumina's memory pending review.")
    return " ".join(parts)


def _delete_memory_direct(memory_id: int) -> str:
    """Approved delete_memory pending action -- same lifecycle as Settings."""
    return describe_memory_deletion(delete_memory_with_derivatives(memory_id))


def delete_memory(memory_id: int) -> str:
    """Stages a memory deletion for approval — does not delete directly."""
    from tools.pending_actions import stage_action
    return stage_action("delete_memory", {"memory_id": memory_id})


def register_memory_tools(registry):
    init_memory_db()

    registry.register(
        "save_memory", lambda content, label="general": save_memory(
            content, label, untrusted=True
        ),
        "Save a memory. Label to categorize (e.g. 'people', 'projects', 'preferences', 'discovery').",
        {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "label": {"type": "string", "default": "general"}
            },
            "required": ["content"]
        }
    )

    registry.register(
        "search_memory", search_memory,
        "Search memories by keyword. Optionally filter by label.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "label": {"type": "string"}
            },
            "required": ["query"]
        }
    )

    registry.register(
        "get_recent_memories", get_recent_memories,
        "Get most recent memories. Optionally filter by label.",
        {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 5},
                "label": {"type": "string"}
            },
            "required": []
        }
    )

    registry.register(
        "delete_memory", delete_memory,
        "Delete a memory by its ID number.",
        {
            "type": "object",
            "properties": {
                "memory_id": {"type": "integer"}
            },
            "required": ["memory_id"]
        }
    )


# ── Chat Persistence ───────────────────────────────────────────────────────────

def init_chat_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
        )
    """)
    conn.commit()
    conn.close()


def create_chat(name: str = None) -> int:
    now = datetime.now().isoformat()
    if not name:
        name = f"Chat {datetime.now().strftime('%b %d, %I:%M %p')}"
    conn = get_db()
    cur = conn.execute("INSERT INTO chats (name, created_at, updated_at) VALUES (?, ?, ?)", (name, now, now))
    chat_id = cur.lastrowid
    conn.commit()
    conn.close()
    return chat_id


def list_chats() -> list:
    conn = get_db()
    rows = conn.execute("SELECT id, name, updated_at FROM chats ORDER BY updated_at DESC").fetchall()
    conn.close()
    return [{"id": r["id"], "name": r["name"], "updated_at": r["updated_at"]} for r in rows]


def save_chat_message(chat_id: int, role: str, content: str, metadata: dict = None):
    now = datetime.now().isoformat()
    conn = get_db()
    conn.execute(
        "INSERT INTO chat_messages (chat_id, role, content, metadata, created_at) VALUES (?, ?, ?, ?, ?)",
        (chat_id, role, content, json.dumps(metadata) if metadata else None, now)
    )
    conn.execute("UPDATE chats SET updated_at=? WHERE id=?", (now, chat_id))
    conn.commit()
    conn.close()


def load_chat_messages(chat_id: int) -> list:
    conn = get_db()
    rows = conn.execute(
        "SELECT role, content, metadata FROM chat_messages WHERE chat_id=? ORDER BY created_at",
        (chat_id,)
    ).fetchall()
    conn.close()
    return [{"role": r["role"], "content": r["content"],
             "metadata": json.loads(r["metadata"]) if r["metadata"] else None} for r in rows]


def rename_chat(chat_id: int, name: str):
    conn = get_db()
    conn.execute("UPDATE chats SET name=? WHERE id=?", (name, chat_id))
    conn.commit()
    conn.close()
    
def get_chat_name(chat_id: int) -> str:
    conn = get_db()
    row = conn.execute("SELECT name FROM chats WHERE id=?", (chat_id,)).fetchone()
    conn.close()
    return row["name"] if row else ""    


def delete_chat(chat_id: int):
    conn = get_db()
    conn.execute("DELETE FROM chat_messages WHERE chat_id=?", (chat_id,))
    conn.execute("DELETE FROM chats WHERE id=?", (chat_id,))
    conn.commit()
    conn.close()
