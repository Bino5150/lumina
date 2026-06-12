"""
Skills — Procedural knowledge documents for Lumina.

Skills are .md files in ~/lumina/skills/ that teach Lumina how to handle
specific complex workflows. Unlike MemPalace (facts/context), skills store
procedures — step-by-step recipes Lumina can load when relevant.

Flow:
  1. On each turn, search skills by user message → inject top-N matches
  2. After N tool calls in a session, nudge Lumina to write a skill
  3. Lumina can also call save_skill() herself at any time

Directory layout:
  ~/lumina/skills/
    ├── index (SQLite FTS5 — name + description only)
    └── *.md  (full skill documents)
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


def _skills_dir() -> str:
    d = os.path.join(config.BASE_DIR, "skills")
    os.makedirs(d, exist_ok=True)
    return d


def _safe_filename(name: str) -> str:
    """Convert skill name to a safe filename slug."""
    slug = re.sub(r'[^\w\s-]', '', name.lower())
    slug = re.sub(r'[\s_]+', '-', slug).strip('-')
    return slug + ".md"


# ── DB Init ────────────────────────────────────────────────────────────────────

def init_skills_db():
    """Create skills tables if they don't exist. Safe to call on every startup."""
    conn = get_db()

    # Main skills table — metadata + path only (content lives on disk)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS skills (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL,
            path        TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        )
    """)

    # FTS5 virtual table — indexes name + description for fast keyword search.
    # content='' means external content (we manage sync manually via triggers).
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS skills_fts
            USING fts5(name, description, content='skills', content_rowid='id')
    """)

    # Keep FTS in sync with skills table
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS skills_ai AFTER INSERT ON skills BEGIN
            INSERT INTO skills_fts(rowid, name, description)
            VALUES (new.id, new.name, new.description);
        END
    """)

    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS skills_au AFTER UPDATE ON skills BEGIN
            INSERT INTO skills_fts(skills_fts, rowid, name, description)
            VALUES ('delete', old.id, old.name, old.description);
            INSERT INTO skills_fts(rowid, name, description)
            VALUES (new.id, new.name, new.description);
        END
    """)

    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS skills_ad AFTER DELETE ON skills BEGIN
            INSERT INTO skills_fts(skills_fts, rowid, name, description)
            VALUES ('delete', old.id, old.name, old.description);
        END
    """)

    conn.commit()
    conn.close()


# ── Write API ──────────────────────────────────────────────────────────────────

def write_skill(name: str, description: str, content: str) -> dict:
    """
    Write a skill document to disk and index it in SQLite.
    If a skill with this name already exists, it is updated.
    Returns {'path': ..., 'name': ..., 'updated': bool}
    """
    skills_dir = _skills_dir()
    filename = _safe_filename(name)
    path = os.path.join(skills_dir, filename)
    now = datetime.now().isoformat()

    # Ensure content has the standard header
    if not content.strip().startswith("# Skill:"):
        content = f"# Skill: {name}\n**Description:** {description}\n\n{content.strip()}"

    # Write to disk
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    # Upsert in DB
    conn = get_db()
    existing = conn.execute(
        "SELECT id FROM skills WHERE name=?", (name,)
    ).fetchone()

    if existing:
        conn.execute(
            "UPDATE skills SET description=?, path=?, updated_at=? WHERE name=?",
            (description, path, now, name)
        )
        updated = True
    else:
        conn.execute(
            "INSERT INTO skills (name, description, path, created_at, updated_at) VALUES (?,?,?,?,?)",
            (name, description, path, now, now)
        )
        updated = False

    conn.commit()
    conn.close()

    return {"path": path, "name": name, "updated": updated}


# ── Search API ────────────────────────────────────────────────────────────────

def search_skills(query: str, limit: int = None) -> list[dict]:
    """
    Search skills by keyword against name + description (FTS5).
    Returns list of {'name', 'description', 'path'} sorted by relevance.
    Falls back to LIKE search if FTS returns nothing.
    """
    if limit is None:
        limit = getattr(config, 'SKILLS_MAX_INJECT', 2)

    if not query or not query.strip():
        return []

    conn = get_db()

    # FTS5 needs keywords joined with OR — strip stopwords, split, rejoin
    _stopwords = {'how', 'do', 'i', 'a', 'an', 'the', 'to', 'and', 'or',
                  'in', 'on', 'at', 'is', 'it', 'of', 'for', 'with', 'my'}
    words = [w for w in re.split(r'\W+', query.lower()) if w and w not in _stopwords]
    fts_query = " OR ".join(words) if words else query.strip()

    # FTS5 search
    try:
        rows = conn.execute("""
            SELECT s.name, s.description, s.path
            FROM skills_fts f
            JOIN skills s ON s.id = f.rowid
            WHERE skills_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """, (fts_query, limit)).fetchall()
    except sqlite3.OperationalError:
        rows = []

    # Fallback: LIKE search on name + description
    if not rows:
        pattern = f"%{query.strip()}%"
        rows = conn.execute("""
            SELECT name, description, path FROM skills
            WHERE name LIKE ? OR description LIKE ?
            LIMIT ?
        """, (pattern, pattern, limit)).fetchall()

    conn.close()
    return [dict(r) for r in rows]


def load_skill(name: str) -> str | None:
    """Load the full content of a skill doc from disk by name."""
    conn = get_db()
    row = conn.execute("SELECT path FROM skills WHERE name=?", (name,)).fetchone()
    conn.close()

    if not row:
        return None

    path = row["path"]
    if not os.path.exists(path):
        return None

    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def list_skills() -> list[dict]:
    """Return all indexed skills as {'name', 'description', 'path'}."""
    conn = get_db()
    rows = conn.execute(
        "SELECT name, description, path, created_at, updated_at FROM skills ORDER BY name"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Context Injection ─────────────────────────────────────────────────────────

def build_skills_block(query: str) -> str:
    """
    Search for relevant skills and return an injection block for the system prompt.
    Returns empty string if no relevant skills found.
    """
    matches = search_skills(query)
    if not matches:
        return ""

    lines = ["## Relevant Skills"]
    for skill in matches:
        content = load_skill(skill["name"])
        if content:
            # Inject full doc for small skills; truncate large ones
            if len(content) <= 2000:
                lines.append(content)
            else:
                # Header + first 1500 chars
                lines.append(content[:1500] + "\n... (truncated — full skill on disk)")
        else:
            lines.append(f"**{skill['name']}**: {skill['description']}")

    return "\n\n".join(lines)


# ── Status ────────────────────────────────────────────────────────────────────

def skills_status() -> str:
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) as c FROM skills").fetchone()["c"]
    rows = conn.execute(
        "SELECT name FROM skills ORDER BY updated_at DESC LIMIT 5"
    ).fetchall()
    conn.close()
    recent = ", ".join(r["name"] for r in rows) if rows else "none"
    return f"Skills: {count} indexed | Recent: {recent}"


# ── Tool Registration ─────────────────────────────────────────────────────────

def register_skills_tools(registry):
    init_skills_db()

    registry.register(
        name="save_skill",
        fn=_save_skill_tool,
        description=(
            "Save a procedural skill document so you can recall it in future sessions. "
            "Use this when you've solved a complex or reusable workflow and want to remember how. "
            "Skills are markdown docs — include procedure steps, pitfalls, and verification."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Short descriptive skill name, e.g. 'Scrape and summarize a webpage'"
                },
                "description": {
                    "type": "string",
                    "description": "One-sentence summary used for retrieval matching — be specific."
                },
                "content": {
                    "type": "string",
                    "description": (
                        "Full skill document in markdown. Should include: "
                        "## Procedure (numbered steps), "
                        "## Pitfalls (gotchas to avoid), "
                        "## Verification (how to confirm it worked). "
                        "Start with # Skill: <name> header."
                    )
                }
            },
            "required": ["name", "description", "content"]
        }
    )

    registry.register(
        name="list_skills",
        fn=lambda: _format_skills_list(),
        description="List all saved skills with their names and descriptions.",
        parameters={"type": "object", "properties": {}, "required": []}
    )

    registry.register(
        name="recall_skill",
        fn=lambda name: load_skill(name) or f"No skill found with name '{name}'.",
        description="Load the full content of a saved skill by name.",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Exact skill name to load."}
            },
            "required": ["name"]
        }
    )


def _save_skill_tool(name: str, description: str, content: str) -> str:
    result = write_skill(name, description, content)
    action = "Updated" if result["updated"] else "Saved"
    return f"{action} skill '{name}' → {result['path']}"


def _format_skills_list() -> str:
    skills = list_skills()
    if not skills:
        return "No skills saved yet."
    lines = [f"**{s['name']}**: {s['description']}" for s in skills]
    return f"Skills ({len(skills)} total):\n" + "\n".join(lines)
