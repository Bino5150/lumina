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

Idempotency model -- history, because this went through two wrong designs
before landing here, and the campaign's own discipline is to record that
rather than erase it:

  v1 (per-title): "insert if this exact title is missing." Caught by this
  file's own test suite silently resurrecting a title the owner had just
  deleted -- a per-title check cannot tell "never installed" from
  "deliberately deleted" apart, since both look like "row absent."

  v2 (category-occupancy): "seed once, iff CATEGORY currently has zero
  rows." Fixed the v1 bug for a *partial* deletion (the category still
  has other rows, so it is never touched again) -- but reintroduced the
  identical identity problem one level up: deleting *every* row in the
  category empties it, which is indistinguishable from "never seeded,"
  so the next startup silently reseeded the whole pack. Caught in review
  before it shipped, not by a test -- worth recording that the test
  suite's coverage of "partial deletion" gave false confidence about
  "total deletion" until someone asked the question directly.

  v3 (this version): a durable installed-pack marker, stored in its own
  file under data_dir, entirely separate from the knowledge rows the pack
  creates. The marker -- not row presence, partial or total -- is the
  sole gate on reseeding. Deleting some, or all, of the pack's rows can
  never be misread as "never installed," because the marker does not
  live in the table being edited. The marker also carries a version
  number, so a future pack revision has a real place to record "this
  data directory has seen v1, needs the v2 update" -- see
  PACK_VERSION below. Writing that future per-version migration logic
  is out of scope for this pass (there is only a v1 pack to migrate
  from), but the primitive it would build on now exists.
"""
import json
import os
import sqlite3
import sys
from datetime import datetime

from core.db import connect as db_connect
from core.test_isolation import refuse_if_production_path

CATEGORY = "lumina-self-knowledge"
PACK_VERSION = 1
_MARKER_FILENAME = "knowledge_packs_installed.json"


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


def _marker_path(data_dir: str) -> str:
    return os.path.join(data_dir, _MARKER_FILENAME)


def _load_marker(data_dir: str) -> dict:
    """{} for an absent, unreadable, or corrupt marker file -- all three
    are treated identically to "never installed." A corrupt marker fails
    safe toward re-seeding rather than silently refusing forever; that
    re-seed is itself safe (see bootstrap_official_knowledge's per-title
    existing-row check), so this can never duplicate rows even if a
    corrupt marker causes a spurious retry."""
    path = _marker_path(data_dir)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _save_marker(data_dir: str, marker: dict) -> None:
    """Same atomic tmp-file + os.replace() pattern as core/persistence.py's
    save() -- a crash mid-write can never leave the marker half-written,
    which matters here specifically: a torn/partial marker write is
    exactly the kind of corruption _load_marker must tolerate above."""
    path = _marker_path(data_dir)
    refuse_if_production_path(path)
    os.makedirs(data_dir, exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(marker, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def bootstrap_official_knowledge(*, data_dir: str, db_path: str) -> list[str]:
    """Seed the official self-knowledge pack into the knowledge table,
    gated on the durable marker described in the module docstring -- NOT
    on whether any row currently exists. Never touches any category other
    than CATEGORY. Returns the titles actually inserted this call (empty
    on a no-op: already installed at PACK_VERSION or newer, or the
    shipped pack is empty/missing).

    The per-title "already exists" check inside the insert loop is a
    separate, narrower safety net for one specific case: a prior run that
    inserted some rows and then crashed before writing the marker. It is
    not the reseed gate (the marker is) -- it just stops that crash-
    recovery retry from duplicating rows it already wrote.
    """
    marker = _load_marker(data_dir)
    if marker.get(CATEGORY, {}).get("version", 0) >= PACK_VERSION:
        return []

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
        now = datetime.now().isoformat()
        for entry in entries:
            title = entry["title"]
            content = entry["content"]
            existing = conn.execute(
                "SELECT 1 FROM knowledge WHERE category=? AND title=?",
                (CATEGORY, title),
            ).fetchone()
            if existing:
                continue
            conn.execute(
                "INSERT INTO knowledge (category, title, content, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (CATEGORY, title, content, now, now),
            )
            inserted.append(title)
        conn.commit()
    finally:
        conn.close()

    marker[CATEGORY] = {"version": PACK_VERSION, "installed_at": datetime.now().isoformat()}
    _save_marker(data_dir, marker)
    return inserted


def ensure_official_knowledge_bootstrapped(*, data_dir: str, db_path: str) -> list[str]:
    """The real-process-startup entry point. Wired into main.py's
    run_cli()/run_gui() and core/headless.py's get_headless_agent(),
    alongside (never inside) ensure_official_skills_bootstrapped() --
    same explicit-call-site discipline, for the same reason: no test's
    Agent construction should ever pick up unexpected Knowledge Base rows
    as a side effect of simply constructing a LuminaAgent.

    Never blocks startup on an ordinary/expected failure (missing or
    corrupt pack file, a filesystem error, a locked database) -- mirrors
    ensure_official_skills_bootstrapped()'s own fail-safe posture.
    Deliberately does NOT catch core/test_isolation.py's
    refuse_if_production_path() RuntimeError (reached via
    core.db.connect() and via _save_marker()'s own explicit check) --
    that guard exists specifically to be loud and unmissable, and
    swallowing it here would quietly defeat its own purpose; it is a
    no-op in real (non-LUMINA_TESTING) use in any case.
    """
    try:
        return bootstrap_official_knowledge(data_dir=data_dir, db_path=db_path)
    except (OSError, sqlite3.Error, json.JSONDecodeError, ValueError) as e:
        print(f"[knowledge] official knowledge-pack bootstrap failed, "
              f"continuing without it: {e}", file=sys.stderr)
        return []
