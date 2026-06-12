"""
Knowledge Tools — persistent knowledge base for people, projects, notes.
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


def init_knowledge_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            title TEXT,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS people (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            info TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def save_knowledge(category: str, content: str, title: str = None) -> str:
    """Save a knowledge entry under a category."""
    if len(content) > 4000:
        content = content[:4000]
    now = datetime.now().isoformat()
    conn = get_db()
    conn.execute(
        "INSERT INTO knowledge (category, title, content, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (category.lower(), title, content, now, now)
    )
    conn.commit()
    conn.close()
    label = f"[{title}] " if title else ""
    return f"Knowledge saved to '{category}': {label}{content[:80]}..."


def search_knowledge(query: str, category: str = None) -> str:
    """Search knowledge base by keyword. Optionally filter by category."""
    conn = get_db()
    if category:
        rows = conn.execute(
            "SELECT id, category, title, content FROM knowledge WHERE category=? AND (content LIKE ? OR title LIKE ?) ORDER BY updated_at DESC LIMIT 10",
            (category.lower(), f"%{query}%", f"%{query}%")
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, category, title, content FROM knowledge WHERE content LIKE ? OR title LIKE ? ORDER BY updated_at DESC LIMIT 10",
            (f"%{query}%", f"%{query}%")
        ).fetchall()
    conn.close()
    if not rows:
        return f"No knowledge found for '{query}'."
    results = []
    for r in rows:
        label = f"[{r['title']}] " if r['title'] else ""
        results.append(f"[{r['id']}] ({r['category']}) {label}{r['content'][:200]}")
    return "\n".join(results)


def save_person(name: str, info: str) -> str:
    """Save or update a person's info. Upserts by name."""
    if len(info) > 2000:
        info = info[:2000]
    now = datetime.now().isoformat()
    conn = get_db()
    conn.execute(
        "INSERT INTO people (name, info, updated_at) VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE SET info=excluded.info, updated_at=excluded.updated_at",
        (name, info, now)
    )
    conn.commit()
    conn.close()
    return f"Person saved: {name}"


def search_people(query: str) -> str:
    """Search people by name or info."""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, name, info FROM people WHERE name LIKE ? OR info LIKE ? ORDER BY name LIMIT 10",
        (f"%{query}%", f"%{query}%")
    ).fetchall()
    conn.close()
    if not rows:
        return f"No people found matching '{query}'."
    return "\n".join(f"[{r['id']}] {r['name']}: {r['info'][:200]}" for r in rows)


def delete_knowledge(entry_id: int = None, category: str = None) -> str:
    """Delete a knowledge entry by ID, or all entries in a category."""
    conn = get_db()
    if entry_id:
        cur = conn.execute("DELETE FROM knowledge WHERE id=?", (entry_id,))
        conn.commit()
        conn.close()
        return f"Entry {entry_id} deleted." if cur.rowcount else f"Entry {entry_id} not found."
    elif category:
        cur = conn.execute("DELETE FROM knowledge WHERE category=?", (category.lower(),))
        conn.commit()
        conn.close()
        return f"Deleted {cur.rowcount} entries from category '{category}'."
    conn.close()
    return "Specify entry_id or category."


def register_knowledge_tools(registry):
    init_knowledge_db()

    registry.register(
        "save_knowledge", save_knowledge,
        "Save a note or info to the knowledge base under a category.",
        {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "e.g. 'projects', 'recipes', 'research'"},
                "content": {"type": "string"},
                "title": {"type": "string"}
            },
            "required": ["category", "content"]
        }
    )

    registry.register(
        "search_knowledge", search_knowledge,
        "Search the knowledge base by keyword. Optionally filter by category.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "category": {"type": "string"}
            },
            "required": ["query"]
        }
    )

    registry.register(
        "save_person", save_person,
        "Save or update a person's contact/info. Upserts by name.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "info": {"type": "string"}
            },
            "required": ["name", "info"]
        }
    )

    registry.register(
        "search_people", search_people,
        "Search people by name or info.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"}
            },
            "required": ["query"]
        }
    )

    registry.register(
        "delete_knowledge", delete_knowledge,
        "Delete a knowledge entry by ID or entire category.",
        {
            "type": "object",
            "properties": {
                "entry_id": {"type": "integer"},
                "category": {"type": "string"}
            },
            "required": []
        }
    )
