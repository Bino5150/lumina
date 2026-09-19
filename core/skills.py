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

import hashlib
import os
import re
import sqlite3
from datetime import datetime

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config


# ── Helpers ────────────────────────────────────────────────────────────────────

def get_db():
    from core.db import connect
    return connect()


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

def init_skills_db(db_path: str = None):
    """Create skills tables if they don't exist. Safe to call on every startup.

    db_path: explicit target DB file (SKILLS-IMPORT-EXPORT-PORTABILITY-01 --
    lets the transporter operate against an isolated release/test DB without
    touching ambient config.DB_PATH). None (default) preserves every existing
    caller's behavior exactly via get_db()."""
    if db_path is None:
        conn = get_db()
    else:
        from core.db import connect
        conn = connect(path=db_path)

    # Main skills table — metadata + path only (content lives on disk)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS skills (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL,
            path        TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            content_sha256 TEXT
        )
    """)

    # SKILLS-IMPORT-EXPORT-PORTABILITY-01: ownership/origin ('official' |
    # 'user'), needed to keep a normal save_skill/import from silently
    # overwriting a War Room-approved OFFICIAL skill. Idempotent in-place
    # migration -- existing rows (all pre-campaign skills were user-authored
    # via save_skill) default to 'user', which is exactly correct, not a guess.
    try:
        conn.execute("ALTER TABLE skills ADD COLUMN origin TEXT NOT NULL DEFAULT 'user'")
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            raise

    # Persist the content identity the registry claims to serve. Existing
    # user-authored rows remain nullable; OFFICIAL imports always bind this
    # field and load/export paths fail closed if it is absent or disagrees.
    try:
        conn.execute("ALTER TABLE skills ADD COLUMN content_sha256 TEXT")
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            raise

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

    # An external-content FTS table can be internally valid while no longer
    # matching its authoritative skills rows (for example after an interrupted
    # manual restore). Ask FTS5 to compare against the content table; rebuild
    # only when that check proves split brain. The rebuild remains in this same
    # SQLite transaction, so callers never observe a half-rebuilt index.
    try:
        conn.execute(
            "INSERT INTO skills_fts(skills_fts, rank) VALUES ('integrity-check', 1)"
        )
    except sqlite3.DatabaseError:
        conn.execute("INSERT INTO skills_fts(skills_fts) VALUES ('rebuild')")

    conn.commit()
    conn.close()


# ── Write API ──────────────────────────────────────────────────────────────────

class OfficialSkillOverwriteError(Exception):
    """Raised when a normal (save_skill / write_skill) write would overwrite
    a War Room-approved OFFICIAL skill. See SKILLS-IMPORT-EXPORT-PORTABILITY-01
    T9 -- OFFICIAL skills are only ever replaced through a deliberate future
    upgrade/migration mechanism, never silently by this path."""


class OfficialSkillIntegrityError(Exception):
    """Raised when an OFFICIAL registry row cannot prove the exact bytes it
    is about to serve. Runtime drift must be loud, never silently injected."""


class SkillPayloadPolicyError(Exception):
    """Raised when a USER or OFFICIAL skill violates the bounded payload law."""


def write_skill(name: str, description: str, content: str) -> dict:
    """
    Write a skill document to disk and index it in SQLite.
    If a skill with this name already exists, it is updated -- unless that
    existing skill is OFFICIAL (origin='official'), in which case this
    raises OfficialSkillOverwriteError rather than silently replacing it.
    Returns {'path': ..., 'name': ..., 'updated': bool}
    """
    # Ensure content has the standard header
    if not content.strip().startswith("# Skill:"):
        content = f"# Skill: {name}\n**Description:** {description}\n\n{content.strip()}"
    content_bytes = content.encode("utf-8")
    from core.skill_transport import MAX_SKILL_PAYLOAD_BYTES
    if len(content_bytes) > MAX_SKILL_PAYLOAD_BYTES:
        raise SkillPayloadPolicyError(
            f"Skill payload is {len(content_bytes)} bytes; maximum is "
            f"{MAX_SKILL_PAYLOAD_BYTES}."
        )

    skills_dir = _skills_dir()
    filename = _safe_filename(name)
    path = os.path.join(skills_dir, filename)
    now = datetime.now().isoformat()
    content_sha256 = hashlib.sha256(content_bytes).hexdigest()

    # Serialize the identity check with transporter imports. Without an
    # immediate transaction, an OFFICIAL import can commit after this SELECT
    # but before this INSERT, producing a raw UNIQUE error and an orphan user
    # file even though the OFFICIAL row correctly wins.
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT id, origin FROM skills WHERE name=?", (name,)
        ).fetchone()

        if existing and existing["origin"] == "official":
            raise OfficialSkillOverwriteError(
                f"'{name}' is an OFFICIAL skill and cannot be overwritten via save_skill."
            )

        # Write to disk only after the serialized OFFICIAL guard.
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

        if existing:
            conn.execute(
                "UPDATE skills SET description=?, path=?, updated_at=?, content_sha256=? "
                "WHERE name=?",
                (description, path, now, content_sha256, name)
            )
            updated = True
        else:
            conn.execute(
                "INSERT INTO skills "
                "(name, description, path, created_at, updated_at, origin, content_sha256) "
                "VALUES (?,?,?,?,?,'user',?)",
                (name, description, path, now, now, content_sha256)
            )
            updated = False

        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
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

    if not isinstance(query, str) or not query.strip():
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
    row = conn.execute(
        "SELECT path, origin, content_sha256 FROM skills WHERE name=?", (name,)
    ).fetchone()
    conn.close()

    if not row:
        return None

    path = row["path"]
    if not os.path.exists(path):
        if row["origin"] == "official":
            raise OfficialSkillIntegrityError(
                f"OFFICIAL skill '{name}' has no backing file at {path}."
            )
        return None

    if row["origin"] == "official" and os.path.islink(path):
        raise OfficialSkillIntegrityError(
            f"OFFICIAL skill '{name}' points to a symlink, refusing runtime recall."
        )

    from core.skill_transport import (
        MAX_SKILL_PAYLOAD_BYTES,
        PackageValidationError,
        _read_bounded_regular_file,
    )
    try:
        content = _read_bounded_regular_file(
            path,
            max_bytes=MAX_SKILL_PAYLOAD_BYTES,
            label=f"installed skill {name!r}",
            missing_code="missing_payload",
            too_large_code="payload_too_large",
        )
    except PackageValidationError as e:
        if row["origin"] == "official":
            raise OfficialSkillIntegrityError(
                f"OFFICIAL skill '{name}' failed runtime payload policy: {e}"
            ) from e
        raise SkillPayloadPolicyError(
            f"Skill '{name}' failed runtime payload policy: {e}"
        ) from e

    if row["origin"] == "official":
        expected = row["content_sha256"]
        actual = hashlib.sha256(content).hexdigest()
        if not expected:
            raise OfficialSkillIntegrityError(
                f"OFFICIAL skill '{name}' has no persisted expected content hash."
            )
        if actual != expected:
            raise OfficialSkillIntegrityError(
                f"OFFICIAL skill '{name}' failed runtime integrity verification "
                f"(expected {expected}, found {actual})."
            )

    return content.decode("utf-8")


def list_skills() -> list[dict]:
    """Return all indexed skills as {'name', 'description', 'path', 'origin'}."""
    conn = get_db()
    rows = conn.execute(
        "SELECT name, description, path, created_at, updated_at, origin FROM skills ORDER BY name"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Context Injection ─────────────────────────────────────────────────────────

def build_skills_block(query: str) -> str:
    """
    Search for relevant skills and return an injection block for the system prompt.
    Returns empty string if no relevant skills found.
    """
    if isinstance(query, list):
        # Multipart content (image turn) — search on the text portion only.
        query = " ".join(
            b.get("text", "") for b in query
            if isinstance(b, dict) and isinstance(b.get("text"), str)
        ).strip()
    if not query:
        return ""
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
