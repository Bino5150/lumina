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
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_memory_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT DEFAULT 'general',
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
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


def save_memory(content: str, label: str = "general") -> str:
    """Save a memory. Also writes to palace with AAAK compression."""
    if len(content) > 512:
        content = content[:512]

    # Write to flat memories table (preserved for compatibility)
    conn = get_db()
    conn.execute(
        "INSERT INTO memories (label, content, created_at) VALUES (?, ?, ?)",
        (label, content, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

    # Write-through to palace
    try:
        from tools.palace import palace_store, palace_store_hall
        wing = LABEL_TO_WING.get(label.lower(), "sessions")
        room = label.lower() if label != "general" else "general"
        result = palace_store(content, wing=wing, room=room, layer=2)

        # Also drop into a hall if this label maps to one
        hall = LABEL_TO_HALL.get(label.lower())
        if hall:
            palace_store_hall(content, hall=hall, layer=2)

        compressed_preview = (result["compressed"] or "")[:80]
        return f"Memory saved [{label}]. Compressed: {compressed_preview}"
    except Exception as e:
        # Palace failure is non-fatal — flat save already succeeded
        return f"Memory saved [{label}]: {content[:80]} (palace: {e})"


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


def delete_memory(memory_id: int) -> str:
    """Delete a memory by its ID."""
    conn = get_db()
    cur = conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
    conn.commit()
    conn.close()
    if cur.rowcount:
        return f"Memory {memory_id} deleted."
    return f"Memory {memory_id} not found."


def register_memory_tools(registry):
    init_memory_db()

    registry.register(
        "save_memory", save_memory,
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
