"""DOCS-01B Checkpoint B -- official Lumina Knowledge Pack bootstrap.

Covers core/knowledge_bootstrap.py's ensure_official_knowledge_bootstrapped()/
bootstrap_official_knowledge(): idempotent installation of the shipped
self-knowledge pack (knowledge_packs/official/lumina_self_knowledge.json)
into the ordinary Knowledge Base, retrieval through the real tools/knowledge.py
functions, and the security property the campaign's Documentation Trust Law
requires -- an official knowledge entry is never elevated above ordinary
tool-output provenance. Every test targets an isolated tmp_path database;
none ever touches a real data directory.
"""
import sqlite3

import pytest

import config
import tools.knowledge as knowledge
from core.context import ContextManager
from core.knowledge_bootstrap import (
    CATEGORY,
    bootstrap_official_knowledge,
    ensure_official_knowledge_bootstrapped,
    load_official_pack,
)


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    """Isolate config.DB_PATH to a throwaway sqlite file -- same pattern as
    tests/test_knowledge_base_resurrection_01.py -- so the real
    tools.knowledge functions (which use ambient config.DB_PATH) operate
    against the same isolated database the bootstrap wrote to."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(config, "DB_PATH", db_path)
    return db_path


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
    and no title repeats (a repeat would make the dedup-by-title bootstrap
    silently drop one entry)."""
    entries = load_official_pack()
    assert len(entries) >= 15, "expected a substantive pack, not a stub"
    titles = [e["title"] for e in entries]
    assert len(titles) == len(set(titles)), "duplicate titles in the shipped pack"
    for e in entries:
        assert e["title"].strip()
        assert e["content"].strip()


def test_bootstrap_installs_every_shipped_entry(db_env):
    inserted = bootstrap_official_knowledge(db_path=db_env)
    entries = load_official_pack()
    assert sorted(inserted) == sorted(e["title"] for e in entries)
    assert _row_count(db_env, category=CATEGORY) == len(entries)


def test_bootstrap_is_idempotent(db_env):
    first = bootstrap_official_knowledge(db_path=db_env)
    assert len(first) == len(load_official_pack())
    second = bootstrap_official_knowledge(db_path=db_env)
    assert second == [], "a re-run must not insert duplicates"
    assert _row_count(db_env, category=CATEGORY) == len(load_official_pack())


def test_bootstrap_respects_partial_manual_deletion(db_env):
    """If the owner (or Lumina, via an approved delete_knowledge) removes
    ONE official entry, re-running the bootstrap must not resurrect it.
    This is exactly the case an earlier per-title 'insert if this exact
    title is missing' version got wrong -- a per-title check cannot tell
    'never installed' from 'deliberately deleted' apart, since both look
    like 'row absent'. The fix checks whether CATEGORY has ANY row left,
    not whether this specific title does."""
    bootstrap_official_knowledge(db_path=db_env)
    entries = load_official_pack()
    doomed_title = entries[0]["title"]

    conn = sqlite3.connect(db_env)
    conn.execute("DELETE FROM knowledge WHERE category=? AND title=?", (CATEGORY, doomed_title))
    conn.commit()
    conn.close()

    reinserted = bootstrap_official_knowledge(db_path=db_env)
    assert reinserted == [], "a non-empty category must be treated as already seeded"
    assert _row_count(db_env, category=CATEGORY) == len(entries) - 1


def test_bootstrap_reseeds_after_a_full_wipe(db_env):
    """The other side of the same rule: if EVERY official entry has been
    removed, the category is empty again, and the bootstrap correctly
    reads that as 'never seeded' and reseeds the whole pack -- a
    reasonable reading of 'the owner cleared this out', not a bug."""
    bootstrap_official_knowledge(db_path=db_env)
    entries = load_official_pack()

    conn = sqlite3.connect(db_env)
    conn.execute("DELETE FROM knowledge WHERE category=?", (CATEGORY,))
    conn.commit()
    conn.close()
    assert _row_count(db_env, category=CATEGORY) == 0

    reseeded = bootstrap_official_knowledge(db_path=db_env)
    assert sorted(reseeded) == sorted(e["title"] for e in entries)
    assert _row_count(db_env, category=CATEGORY) == len(entries)


def test_bootstrap_never_touches_other_categories(db_env):
    """A user's own knowledge entries, in an unrelated category, must be
    completely untouched by the official bootstrap running alongside them."""
    monkeypatch_conn = sqlite3.connect(db_env)
    # Table must exist before we can insert into it by hand; bootstrap will
    # also happily create it, but this exercises the "table already exists
    # with unrelated user rows in it" path specifically.
    monkeypatch_conn.execute("""
        CREATE TABLE IF NOT EXISTS knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT NOT NULL,
            title TEXT, content TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )
    """)
    monkeypatch_conn.execute(
        "INSERT INTO knowledge (category, title, content, created_at, updated_at) VALUES (?,?,?,?,?)",
        ("recipes", "Chili", "two cans of beans", "2026-01-01", "2026-01-01"),
    )
    monkeypatch_conn.commit()
    monkeypatch_conn.close()

    bootstrap_official_knowledge(db_path=db_env)

    assert _row_count(db_env, category="recipes") == 1
    assert _row_count(db_env, category=CATEGORY) == len(load_official_pack())


def test_retrieval_via_ordinary_knowledge_tools(db_env):
    """Retrieval must work through the exact same tools any other knowledge
    entry uses -- no separate indexing format, no dedicated lookup path.
    Spot-checks a representative sample of the example questions the
    campaign asked the pack to answer."""
    ensure_official_knowledge_bootstrapped(data_dir="unused", db_path=db_env)

    listing = knowledge.list_knowledge(category=CATEGORY, limit=50)
    assert "MemPalace Halls" in listing
    assert "Browser Companion" in listing

    halls_hits = knowledge.search_knowledge("Halls", category=CATEGORY)
    assert "cross-cutting" in halls_hits

    revoke_hits = knowledge.search_knowledge("Revoke", category=CATEGORY)
    assert "durably" in revoke_hits

    # read_knowledge needs a real row id -- pull one from a direct query.
    conn = sqlite3.connect(db_env)
    row = conn.execute(
        "SELECT id FROM knowledge WHERE category=? AND title=?",
        (CATEGORY, "Dreaming"),
    ).fetchone()
    conn.close()
    full = knowledge.read_knowledge(row[0])
    assert "nightstand" in full
    assert "Dreaming" in full


def test_official_entries_get_ordinary_tool_output_provenance(db_env):
    """The Documentation Trust Law's structural requirement: a knowledge
    tool's result is tagged exactly like any other tool's result when it
    enters context -- no special-case elevation for 'official' content.
    Uses the real ContextManager.add_tool_result(), not a stub, so a
    future special-case branch added for any tool named '*knowledge*'
    would be caught here."""
    bootstrap_official_knowledge(db_path=db_env)
    result = knowledge.search_knowledge("Information is not authority", category=CATEGORY)
    assert result, "expected the pack's own authority-boundary entry to be findable"

    cm = ContextManager()
    cm.add_tool_result(tool_call_id="tc1", name="search_knowledge", result=result)
    tagged = cm.history[-1]["content"]
    assert tagged.startswith("[TOOL_OUTPUT")
    assert cm.history[-1]["role"] == "tool"


def test_missing_pack_file_is_a_clean_no_op(db_env, monkeypatch):
    """An absent or corrupt shipped pack file must never block startup --
    mirrors ensure_official_skills_bootstrapped()'s fail-safe posture for
    a missing skill_packages/official/ directory."""
    import core.knowledge_bootstrap as kb
    monkeypatch.setattr(kb, "_official_pack_path", lambda: "/nonexistent/path.json")
    result = ensure_official_knowledge_bootstrapped(data_dir="unused", db_path=db_env)
    assert result == []
    assert _row_count(db_env) == 0


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
