"""Lumina Knowledge Pack -- official self-knowledge bootstrap (DOCS-01B
Checkpoint B).

Ships a small set of concise, factual entries about Lumina's own
architecture into the ordinary Knowledge Base (tools/knowledge.py's
`knowledge` SQLite table), retrievable through the same tools
(list_knowledge/search_knowledge/read_knowledge) as anything else stored
there -- no new indexing format, no new trust channel. Mirrors
core/skill_transport.py's ensure_official_skills_bootstrapped() pattern:
an explicit, auditable startup call site (never LuminaAgent.__init__
itself), deterministic, offline, idempotent, and never blocking on
failure.

Reserved category: every entry this module installs uses CATEGORY
("lumina-self-knowledge") and nothing else. That is the only thing that
distinguishes an official entry from anything the owner or Lumina saves
under their own category name -- there is no separate table, no schema
change, and no elevated trust: an official self-knowledge entry is
retrieved via the exact same read-only tools as any other knowledge row,
and its content passes through the exact same generic tool-output
provenance tagging (core/context.py's add_tool_result()) as any other
tool result. Knowledge is information; it is never authority -- see the
"Information is not authority" entry in the pack itself, and
docs/README.md's "How to read this documentation" preface.

Idempotency model: install-once-per-data-directory, not install-each-
missing-title. The bootstrap checks whether CATEGORY already has ANY row
in it; if so, it assumes this data directory has already been seeded and
does nothing further. This is deliberate, not a simplification for its
own sake -- an earlier per-title "insert if this exact title is missing"
version was caught by this file's own test suite silently resurrecting a
title the owner had just deleted (deletion and "never yet installed" are
indistinguishable to a per-title check, since both look like "row
absent"). Checking category occupancy as a whole instead means: any
partial deletion is respected forever (the category is non-empty, so the
bootstrap never touches it again), and only a full wipe of the category
is treated as "start fresh" and reseeds everything -- a reasonable
reading of "the owner cleared this out," not a bug.

Known limitation, stated plainly rather than hidden: if a future release
adds a new entry to the shipped pack or corrects existing wording, an
install that already has a non-empty category will never receive it --
this bootstrap only ever seeds a data directory once. That is
deliberately out of scope for this first pass (see DOCS-01B's final
report for the residual note), the same way Reforge's own reference
document names its "strict-B" seam as known future work rather than
pretending it is solved.
"""
import json
import os
import sqlite3
import sys
from datetime import datetime

from core.db import connect as db_connect

CATEGORY = "lumina-self-knowledge"


def _official_pack_path() -> str:
    """Repo-relative, resolved from this module's own location -- never
    config/cwd-relative, so it is correct from any checkout path."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(repo_root, "knowledge_packs", "official", "lumina_self_knowledge.json")


def load_official_pack() -> list[dict]:
    """Read the shipped pack file. Returns [] if it is missing rather than
    raising -- an absent/corrupt pack file should never block startup."""
    path = _official_pack_path()
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        entries = json.load(f)
    return [e for e in entries if e.get("title") and e.get("content")]


def bootstrap_official_knowledge(*, db_path: str) -> list[str]:
    """Seed the official self-knowledge pack into the knowledge table at
    an explicit db_path, but only if CATEGORY is currently completely
    empty there. Never touches any category other than CATEGORY, and
    never inserts anything at all if CATEGORY already has one or more
    rows (see module docstring's "Idempotency model" for why this is a
    category-occupancy check, not a per-title one). Returns the titles
    actually inserted this call (empty on a no-op -- either "already
    seeded" or "shipped pack is empty/missing").
    """
    entries = load_official_pack()
    if not entries:
        return []

    conn = db_connect(path=db_path)
    inserted = []
    try:
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
        already_seeded = conn.execute(
            "SELECT 1 FROM knowledge WHERE category=? LIMIT 1", (CATEGORY,)
        ).fetchone()
        if already_seeded:
            return []
        now = datetime.now().isoformat()
        for entry in entries:
            title = entry["title"]
            content = entry["content"]
            conn.execute(
                "INSERT INTO knowledge (category, title, content, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (CATEGORY, title, content, now, now),
            )
            inserted.append(title)
        conn.commit()
    finally:
        conn.close()
    return inserted


def ensure_official_knowledge_bootstrapped(*, data_dir: str, db_path: str) -> list[str]:
    """The real-process-startup entry point. Wired into main.py's
    run_cli()/run_gui() and core/headless.py's get_headless_agent(),
    alongside (never inside) ensure_official_skills_bootstrapped() --
    same explicit-call-site discipline, for the same reason: no test's
    Agent construction should ever pick up unexpected Knowledge Base rows
    as a side effect of simply constructing a LuminaAgent.

    `data_dir` is accepted (unused directly here) to keep this call's
    signature and call sites symmetric with the skill bootstrap's; the
    knowledge table itself only needs db_path. Never blocks startup on an
    ordinary/expected failure (missing or corrupt pack file, a filesystem
    error, a locked database) -- mirrors
    ensure_official_skills_bootstrapped()'s own fail-safe posture.
    Deliberately does NOT catch core/test_isolation.py's
    refuse_if_production_path() RuntimeError (reached via
    core.db.connect()) -- that guard exists specifically to be loud and
    unmissable, and swallowing it here would quietly defeat its own
    purpose; it is a no-op in real (non-LUMINA_TESTING) use in any case.
    """
    try:
        return bootstrap_official_knowledge(db_path=db_path)
    except (OSError, sqlite3.Error, json.JSONDecodeError, ValueError) as e:
        print(f"[knowledge] official knowledge-pack bootstrap failed, "
              f"continuing without it: {e}", file=sys.stderr)
        return []
