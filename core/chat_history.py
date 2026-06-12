"""
Chat History Search — Full-text search over Lumina's conversation log.

Gives Lumina the ability to actively search past conversations by keyword,
retrieve specific sessions by date, and surface context from previous work.

Unlike MemPalace (compressed summaries injected passively), this searches
the raw message log on demand — tool-only, never auto-injected.

Tools registered:
  search_chat_history  — FTS5 keyword search across all messages
  get_chat_session     — Load all messages from a specific chat by id or name
  list_recent_chats    — List recent chat sessions with timestamps
"""

import os
import re
import sqlite3
from datetime import datetime

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config


# ── Helpers ────────────────────────────────────────────────────────────────────

def get_db():
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ── DB Init ────────────────────────────────────────────────────────────────────

def init_chat_history_fts():
    """
    Create FTS5 index over chat_messages if it doesn't exist.
    Retroactively indexes all existing messages on first run.
    Safe to call on every startup.
    """
    conn = get_db()

    # FTS5 virtual table — indexes role + content
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS chat_messages_fts
            USING fts5(role, content, content='chat_messages', content_rowid='id')
    """)

    # Auto-sync triggers — messages are immutable so no UPDATE trigger needed
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS chat_messages_ai AFTER INSERT ON chat_messages BEGIN
            INSERT INTO chat_messages_fts(rowid, role, content)
            VALUES (new.id, new.role, new.content);
        END
    """)

    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS chat_messages_ad AFTER DELETE ON chat_messages BEGIN
            INSERT INTO chat_messages_fts(chat_messages_fts, rowid, role, content)
            VALUES ('delete', old.id, old.role, old.content);
        END
    """)

    # Retroactively index any existing messages not yet in FTS
    conn.execute("""
        INSERT INTO chat_messages_fts(rowid, role, content)
        SELECT m.id, m.role, m.content
        FROM chat_messages m
        WHERE m.id NOT IN (
            SELECT rowid FROM chat_messages_fts
        )
    """)

    conn.commit()
    conn.close()


# ── Search API ────────────────────────────────────────────────────────────────

def search_chat_history(query: str, limit: int = 8) -> list[dict]:
    """
    FTS5 keyword search over all chat messages.
    Returns list of {'chat_id', 'chat_name', 'role', 'content', 'created_at'}
    sorted by recency. Falls back to LIKE if FTS returns nothing.
    """
    if not query or not query.strip():
        return []

    conn = get_db()

    # Tokenize into OR-joined FTS5 keywords — same pattern as skills.py
    _stopwords = {'how', 'do', 'i', 'a', 'an', 'the', 'to', 'and', 'or',
                  'in', 'on', 'at', 'is', 'it', 'of', 'for', 'with', 'my',
                  'what', 'when', 'where', 'did', 'we', 'was', 'that', 'this'}
    words = [w for w in re.split(r'\W+', query.lower()) if w and w not in _stopwords]
    fts_query = " OR ".join(words) if words else query.strip()

    try:
        rows = conn.execute("""
            SELECT
                c.id        AS chat_id,
                c.name      AS chat_name,
                c.created_at AS chat_date,
                m.role,
                m.content,
                m.created_at
            FROM chat_messages_fts f
            JOIN chat_messages m ON m.id = f.rowid
            JOIN chats c ON c.id = m.chat_id
            WHERE chat_messages_fts MATCH ?
            ORDER BY m.created_at DESC
            LIMIT ?
        """, (fts_query, limit)).fetchall()
    except sqlite3.OperationalError:
        rows = []

    # Fallback: LIKE search
    if not rows:
        pattern = f"%{query.strip()}%"
        rows = conn.execute("""
            SELECT
                c.id        AS chat_id,
                c.name      AS chat_name,
                c.created_at AS chat_date,
                m.role,
                m.content,
                m.created_at
            FROM chat_messages m
            JOIN chats c ON c.id = m.chat_id
            WHERE m.content LIKE ?
            ORDER BY m.created_at DESC
            LIMIT ?
        """, (pattern, limit)).fetchall()

    conn.close()
    return [dict(r) for r in rows]


def get_chat_session(chat_id: int = None, chat_name: str = None) -> list[dict]:
    """
    Load all messages from a specific chat session by id or name fragment.
    Returns list of {'role', 'content', 'created_at'} in chronological order.
    """
    conn = get_db()

    if chat_id:
        rows = conn.execute("""
            SELECT m.role, m.content, m.created_at, c.name AS chat_name
            FROM chat_messages m
            JOIN chats c ON c.id = m.chat_id
            WHERE m.chat_id = ?
            ORDER BY m.created_at ASC
        """, (chat_id,)).fetchall()
    elif chat_name:
        rows = conn.execute("""
            SELECT m.role, m.content, m.created_at, c.name AS chat_name
            FROM chat_messages m
            JOIN chats c ON c.id = m.chat_id
            WHERE c.name LIKE ?
            ORDER BY m.created_at ASC
        """, (f"%{chat_name}%",)).fetchall()
    else:
        conn.close()
        return []

    conn.close()
    return [dict(r) for r in rows]


def list_recent_chats(limit: int = 20) -> list[dict]:
    """
    List recent chat sessions with timestamps and message counts.
    Returns list of {'id', 'name', 'created_at', 'updated_at', 'message_count'}.
    """
    conn = get_db()
    rows = conn.execute("""
        SELECT
            c.id,
            c.name,
            c.created_at,
            c.updated_at,
            COUNT(m.id) AS message_count
        FROM chats c
        LEFT JOIN chat_messages m ON m.chat_id = c.id
        GROUP BY c.id
        ORDER BY c.updated_at DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Formatters ────────────────────────────────────────────────────────────────

def _format_search_results(results: list[dict]) -> str:
    if not results:
        return "No matching messages found in chat history."

    lines = [f"Found {len(results)} result(s) in chat history:\n"]
    for r in results:
        # Format timestamp to human-readable
        try:
            dt = datetime.fromisoformat(r["created_at"])
            timestamp = dt.strftime("%B %d, %Y at %I:%M %p")
        except Exception:
            timestamp = r["created_at"]

        # Truncate long messages for readability
        content = r["content"]
        if len(content) > 400:
            content = content[:400] + "..."

        lines.append(
            f"📅 {timestamp} — Chat: \"{r['chat_name']}\" (id: {r['chat_id']})\n"
            f"[{r['role']}]: {content}\n"
        )

    return "\n".join(lines)


def _format_session(messages: list[dict]) -> str:
    if not messages:
        return "No messages found for that chat session."

    chat_name = messages[0].get("chat_name", "Unknown")
    try:
        dt = datetime.fromisoformat(messages[0]["created_at"])
        date_str = dt.strftime("%B %d, %Y")
    except Exception:
        date_str = messages[0]["created_at"]

    lines = [f"Chat: \"{chat_name}\" — {date_str} ({len(messages)} messages)\n"]
    for m in messages:
        role_label = "Lumina" if m["role"] == "assistant" else m["role"].capitalize()
        content = m["content"]
        if len(content) > 600:
            content = content[:600] + "..."
        lines.append(f"[{role_label}]: {content}\n")

    return "\n".join(lines)


def _format_recent_chats(chats: list[dict]) -> str:
    if not chats:
        return "No chat sessions found."

    lines = [f"Recent chats ({len(chats)}):\n"]
    for c in chats:
        try:
            dt = datetime.fromisoformat(c["updated_at"])
            timestamp = dt.strftime("%B %d, %Y")
        except Exception:
            timestamp = c["updated_at"]

        lines.append(
            f"• [{c['id']}] \"{c['name']}\" — {timestamp} ({c['message_count']} messages)"
        )

    return "\n".join(lines)


# ── Tool Registration ─────────────────────────────────────────────────────────

def register_chat_history_tools(registry):
    """Register chat history tools into the tool registry. Call once at startup."""
    init_chat_history_fts()

    registry.register(
        name="search_chat_history",
        fn=lambda query, limit=8: _format_search_results(
            search_chat_history(query, limit)
        ),
        description=(
            "Search past conversation history by keyword or topic. "
            "Use this to find what was discussed in previous sessions — "
            "specific decisions, debugging steps, topics, dates, or any past work. "
            "Returns matching messages with timestamps and chat session names."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keywords or phrase to search for, e.g. 'soundscan report', 'tool call debugging', 'album sales'"
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return (default 8, max 20)",
                    "default": 8
                }
            },
            "required": ["query"]
        }
    )

    registry.register(
        name="get_chat_session",
        fn=lambda chat_id=None, chat_name=None: _format_session(
            get_chat_session(chat_id=chat_id, chat_name=chat_name)
        ),
        description=(
            "Load the full message log of a specific past chat session by id or name. "
            "Use after search_chat_history to get the full context of a conversation. "
            "chat_id is the numeric id from search results; chat_name accepts partial matches."
        ),
        parameters={
            "type": "object",
            "properties": {
                "chat_id": {
                    "type": "integer",
                    "description": "Numeric chat session id from list_recent_chats or search_chat_history results"
                },
                "chat_name": {
                    "type": "string",
                    "description": "Partial chat name to match if id is not known"
                }
            },
            "required": []
        }
    )

    registry.register(
        name="list_recent_chats",
        fn=lambda limit=20: _format_recent_chats(list_recent_chats(limit)),
        description=(
            "List recent chat sessions with dates and message counts. "
            "Use this to browse what sessions exist before searching or loading one."
        ),
        parameters={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Number of recent chats to list (default 20)",
                    "default": 20
                }
            },
            "required": []
        }
    )
