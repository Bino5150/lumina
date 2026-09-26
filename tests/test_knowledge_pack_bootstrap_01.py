"""DOCS-01B Checkpoint B -- official Lumina Knowledge Pack bootstrap.

Covers core/knowledge_bootstrap.py's ensure_official_knowledge_bootstrapped()/
bootstrap_official_knowledge(): idempotent installation of the shipped
self-knowledge pack (knowledge_packs/official/lumina_self_knowledge.json)
into the ordinary Knowledge Base, retrieval through the real tools/knowledge.py
functions, and the security property the campaign's Documentation Trust Law
requires -- an official knowledge entry is never elevated above ordinary
tool-output provenance. Every test targets an isolated tmp_path data
directory and database; none ever touches a real data directory.

This file's own history is part of what it tests: an earlier per-title
design ("insert if this exact title is missing") was caught here
resurrecting a deliberately-deleted entry; a follow-up category-occupancy
design ("seed once iff the category is empty") fixed that but reintroduced
the identical bug for a *full* wipe of the category, caught in review
(Sol) before it shipped. test_bootstrap_never_reseeds_after_full_wipe
below is the regression test for exactly that finding -- see
core/knowledge_bootstrap.py's module docstring for the full account of
why a durable marker file, separate from the knowledge rows themselves,
replaced both prior designs.
"""
import sqlite3

import pytest

import config
import tools.knowledge as knowledge
from core.context import ContextManager
from core.knowledge_bootstrap import (
    CATEGORY,
    PACK_VERSION,
    bootstrap_official_knowledge,
    ensure_official_knowledge_bootstrapped,
    load_official_pack,
)


@pytest.fixture
def kb_env(tmp_path, monkeypatch):
    """Isolate both config.DB_PATH and the marker file's data_dir to the
    same throwaway tmp_path -- same db-isolation pattern as
    tests/test_knowledge_base_resurrection_01.py, extended with the
    marker's own directory since v3's idempotency gate lives there, not
    in the database."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(config, "DB_PATH", db_path)
    return {"data_dir": str(tmp_path), "db_path": db_path}


def _bootstrap(env):
    return bootstrap_official_knowledge(data_dir=env["data_dir"], db_path=env["db_path"])


def _row_count(db_path, category=None):
    conn = sqlite3.connect(db_path)
    try:
        if category:
            n = conn.execute("SELECT COUNT(*) FROM knowledge WHERE category=?", (category,)).fetchone()[0]
        else:
            n = conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
    except sqlite3.OperationalError:
        n = 0  # table never created -- semantically zero rows
    conn.close()
    return n


def test_pack_file_is_well_formed():
    """The shipped pack itself: every entry has a non-empty title/content,
    and no title repeats (a repeat would make the per-title existing-row
    check silently drop one entry during the initial seed)."""
    entries = load_official_pack()
    assert len(entries) >= 15, "expected a substantive pack, not a stub"
    titles = [e["title"] for e in entries]
    assert len(titles) == len(set(titles)), "duplicate titles in the shipped pack"
    for e in entries:
        assert e["title"].strip()
        assert e["content"].strip()


def test_bootstrap_installs_every_shipped_entry(kb_env):
    inserted = _bootstrap(kb_env)
    entries = load_official_pack()
    assert sorted(inserted) == sorted(e["title"] for e in entries)
    assert _row_count(kb_env["db_path"], category=CATEGORY) == len(entries)


def test_bootstrap_writes_a_durable_marker(kb_env):
    """The actual idempotency primitive: a marker file under data_dir,
    recording the installed pack version, independent of the knowledge
    rows it caused to exist."""
    import os
    _bootstrap(kb_env)
    marker_path = os.path.join(kb_env["data_dir"], "knowledge_packs_installed.json")
    assert os.path.isfile(marker_path)
    import json
    with open(marker_path, encoding="utf-8") as f:
        marker = json.load(f)
    assert marker[CATEGORY]["version"] == PACK_VERSION
    assert "installed_at" in marker[CATEGORY]


def test_bootstrap_is_idempotent(kb_env):
    first = _bootstrap(kb_env)
    assert len(first) == len(load_official_pack())
    second = _bootstrap(kb_env)
    assert second == [], "a re-run must not insert duplicates"
    assert _row_count(kb_env["db_path"], category=CATEGORY) == len(load_official_pack())


def test_bootstrap_respects_partial_manual_deletion(kb_env):
    """If the owner (or Lumina, via an approved delete_knowledge) removes
    ONE official entry, re-running the bootstrap must not resurrect it."""
    _bootstrap(kb_env)
    entries = load_official_pack()
    doomed_title = entries[0]["title"]

    conn = sqlite3.connect(kb_env["db_path"])
    conn.execute("DELETE FROM knowledge WHERE category=? AND title=?", (CATEGORY, doomed_title))
    conn.commit()
    conn.close()

    reinserted = _bootstrap(kb_env)
    assert reinserted == [], "an installed marker must gate reseeding, not row presence"
    assert _row_count(kb_env["db_path"], category=CATEGORY) == len(entries) - 1


def test_bootstrap_never_reseeds_after_full_wipe(kb_env):
    """The specific regression this file exists to prevent: deleting
    EVERY official row must NOT be reinterpreted as 'never seeded.' A
    category-occupancy check gets this wrong (empty category looks
    identical to 'never installed'); the durable marker does not, because
    it lives outside the table being emptied."""
    _bootstrap(kb_env)
    entries = load_official_pack()

    conn = sqlite3.connect(kb_env["db_path"])
    conn.execute("DELETE FROM knowledge WHERE category=?", (CATEGORY,))
    conn.commit()
    conn.close()
    assert _row_count(kb_env["db_path"], category=CATEGORY) == 0

    reinserted = _bootstrap(kb_env)
    assert reinserted == [], (
        "a full wipe of the category must stay deleted -- the marker, not row "
        "presence, gates reseeding"
    )
    assert _row_count(kb_env["db_path"], category=CATEGORY) == 0
    assert len(entries) > 0  # sanity: there really was something to wipe


def test_corrupt_marker_reseeds_missing_titles_without_duplicating(kb_env):
    """An unreadable/corrupt marker fails safe toward re-seeding (treated
    as 'never installed') rather than refusing forever -- but the
    per-title existing-row check inside the seed loop must still prevent
    that re-seed from duplicating rows that are still present."""
    _bootstrap(kb_env)
    entries = load_official_pack()

    import os
    marker_path = os.path.join(kb_env["data_dir"], "knowledge_packs_installed.json")
    with open(marker_path, "w", encoding="utf-8") as f:
        f.write("{not valid json")

    result = _bootstrap(kb_env)
    assert result == [], "every title was already present -- nothing new to insert"
    assert _row_count(kb_env["db_path"], category=CATEGORY) == len(entries), (
        "must not have duplicated any row"
    )
    # and the marker itself must have been repaired (written, valid JSON again)
    with open(marker_path, encoding="utf-8") as f:
        import json
        marker = json.load(f)
    assert marker[CATEGORY]["version"] == PACK_VERSION


def test_crash_recovery_partial_insert_then_marker_write(kb_env):
    """Simulates a prior run that inserted some rows and then crashed
    before writing the marker (marker absent, rows partially present).
    The per-title check must fill in only what is missing, never
    duplicate what already landed, and then write the marker."""
    entries = load_official_pack()
    already_written = entries[0]

    conn = sqlite3.connect(kb_env["db_path"])
    conn.execute("""
        CREATE TABLE IF NOT EXISTS knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT NOT NULL,
            title TEXT, content TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )
    """)
    conn.execute(
        "INSERT INTO knowledge (category, title, content, created_at, updated_at) VALUES (?,?,?,?,?)",
        (CATEGORY, already_written["title"], already_written["content"], "2026-01-01", "2026-01-01"),
    )
    conn.commit()
    conn.close()

    inserted = _bootstrap(kb_env)
    assert already_written["title"] not in inserted
    assert len(inserted) == len(entries) - 1
    assert _row_count(kb_env["db_path"], category=CATEGORY) == len(entries)


def test_bootstrap_never_touches_other_categories(kb_env):
    """A user's own knowledge entries, in an unrelated category, must be
    completely untouched by the official bootstrap running alongside them."""
    conn = sqlite3.connect(kb_env["db_path"])
    conn.execute("""
        CREATE TABLE IF NOT EXISTS knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT NOT NULL,
            title TEXT, content TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )
    """)
    conn.execute(
        "INSERT INTO knowledge (category, title, content, created_at, updated_at) VALUES (?,?,?,?,?)",
        ("recipes", "Chili", "two cans of beans", "2026-01-01", "2026-01-01"),
    )
    conn.commit()
    conn.close()

    _bootstrap(kb_env)

    assert _row_count(kb_env["db_path"], category="recipes") == 1
    assert _row_count(kb_env["db_path"], category=CATEGORY) == len(load_official_pack())


def test_retrieval_via_ordinary_knowledge_tools(kb_env):
    """Retrieval must work through the exact same tools any other knowledge
    entry uses -- no separate indexing format, no dedicated lookup path.
    Spot-checks a representative sample of the example questions the
    campaign asked the pack to answer."""
    ensure_official_knowledge_bootstrapped(**kb_env)

    listing = knowledge.list_knowledge(category=CATEGORY, limit=50)
    assert "MemPalace Halls" in listing
    assert "Browser Companion" in listing

    halls_hits = knowledge.search_knowledge("Halls", category=CATEGORY)
    assert "cross-cutting" in halls_hits

    revoke_hits = knowledge.search_knowledge("Revoke", category=CATEGORY)
    assert "durably" in revoke_hits

    # read_knowledge needs a real row id -- pull one from a direct query.
    conn = sqlite3.connect(kb_env["db_path"])
    row = conn.execute(
        "SELECT id FROM knowledge WHERE category=? AND title=?",
        (CATEGORY, "Dreaming"),
    ).fetchone()
    conn.close()
    full = knowledge.read_knowledge(row[0])
    assert "nightstand" in full
    assert "Dreaming" in full


def test_official_entries_get_ordinary_tool_output_provenance(kb_env):
    """The Documentation Trust Law's structural requirement: a knowledge
    tool's result is tagged exactly like any other tool's result when it
    enters context -- no special-case elevation for 'official' content.
    Uses the real ContextManager.add_tool_result(), not a stub, so a
    future special-case branch added for any tool named '*knowledge*'
    would be caught here."""
    _bootstrap(kb_env)
    result = knowledge.search_knowledge("Information is not authority", category=CATEGORY)
    assert result, "expected the pack's own authority-boundary entry to be findable"

    cm = ContextManager()
    cm.add_tool_result(tool_call_id="tc1", name="search_knowledge", result=result)
    tagged = cm.history[-1]["content"]
    assert tagged.startswith("[TOOL_OUTPUT")
    assert cm.history[-1]["role"] == "tool"


def test_missing_pack_file_is_a_clean_no_op(kb_env, monkeypatch):
    """An absent or corrupt shipped pack file must never block startup --
    mirrors ensure_official_skills_bootstrapped()'s fail-safe posture for
    a missing skill_packages/official/ directory."""
    import core.knowledge_bootstrap as kb
    monkeypatch.setattr(kb, "_official_pack_path", lambda: "/nonexistent/path.json")
    result = ensure_official_knowledge_bootstrapped(**kb_env)
    assert result == []
    assert _row_count(kb_env["db_path"]) == 0


def test_wired_into_real_startup_call_sites():
    """Regression guard: main.py's run_cli()/run_gui() and
    core/headless.py's get_headless_agent() must each still call the
    bootstrap -- the same drift class Checkpoint A's doc-sync tests guard
    against, applied to a runtime call site instead of a doc."""
    with open("main.py", encoding="utf-8") as f:
        main_src = f.read()
    with open("core/headless.py", encoding="utf-8") as f:
        headless_src = f.read()
    assert main_src.count("ensure_official_knowledge_bootstrapped") >= 2, (
        "expected both run_cli() and run_gui() to call the knowledge bootstrap"
    )
    assert "ensure_official_knowledge_bootstrapped" in headless_src
