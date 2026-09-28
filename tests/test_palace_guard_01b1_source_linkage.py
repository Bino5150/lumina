"""PALACE-GUARD-01B-1 -- source linkage + atomic save.

palace_drawers / palace_halls gain a nullable source_memory_id FK to
memories(id). Only tools.memory.save_memory() sets it, and it now writes the
flat row and every Palace derivative in ONE transaction. Everything else --
model-native palace_remember / palace_hall, Dreaming, compaction, and every
pre-existing row -- stays NULL. No legacy row is ever linked after the fact.

Deletion propagation is deliberately NOT part of this slice (01B-2): until
then, deleting a linked memory directly fails under FK enforcement, which is
exactly the guarantee these tests pin down.
"""
import inspect
import json
import os
import pathlib
import sqlite3
import threading

import pytest

import config
from core import db as core_db
from tools import memory, palace

REPO = pathlib.Path(__file__).resolve().parent.parent


def _tracked_product_py(root=REPO):
    """Git-tracked, non-test .py files under root. Structural guards must be
    judged on product code only: untracked or ignored local files (e.g. a
    dev checkout's gitignored reports/ scripts) aren't product code and must
    not decide the result either way."""
    import subprocess
    out = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--", "*.py"],
                         capture_output=True, check=True).stdout.decode("utf-8")
    return [root / p for p in out.split("\0") if p and not p.startswith("tests/")]


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "linkage.db")
    monkeypatch.setattr(config, "DB_PATH", path)
    palace.init_palace_db()      # also guarantees memories exists (FK parent)
    memory.init_chat_db()
    return path


def _raw(path, fk=True):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA foreign_keys={'ON' if fk else 'OFF'}")
    return conn


def _counts(path):
    c = _raw(path)
    out = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
           for t in ("memories", "palace_drawers", "palace_closets", "palace_halls")}
    c.close()
    return out


def _latest_memory_id(path):
    c = _raw(path)
    mid = c.execute("SELECT MAX(id) FROM memories").fetchone()[0]
    c.close()
    return mid


def _derived(path, memory_id):
    c = _raw(path)
    drawers = c.execute("SELECT id, closet_id, untrusted FROM palace_drawers "
                        "WHERE source_memory_id=?", (memory_id,)).fetchall()
    halls = c.execute("SELECT id, untrusted FROM palace_halls WHERE source_memory_id=?",
                      (memory_id,)).fetchall()
    c.close()
    return drawers, halls


# ── Schema / migration ────────────────────────────────────────────────────────

LEGACY_SCHEMA = """
CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT DEFAULT 'general',
    content TEXT NOT NULL, created_at TEXT NOT NULL, untrusted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE palace_wings (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
    description TEXT);
CREATE TABLE palace_rooms (id INTEGER PRIMARY KEY AUTOINCREMENT, wing_id INTEGER NOT NULL,
    name TEXT NOT NULL, UNIQUE(wing_id, name),
    FOREIGN KEY (wing_id) REFERENCES palace_wings(id) ON DELETE CASCADE);
CREATE TABLE palace_closets (id INTEGER PRIMARY KEY AUTOINCREMENT, room_id INTEGER NOT NULL,
    layer INTEGER NOT NULL DEFAULT 2, compressed TEXT NOT NULL, token_est INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    ever_had_untrusted_merge INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (room_id) REFERENCES palace_rooms(id) ON DELETE CASCADE);
CREATE TABLE palace_drawers (id INTEGER PRIMARY KEY AUTOINCREMENT, closet_id INTEGER,
    room_id INTEGER NOT NULL, content TEXT NOT NULL, tags TEXT,
    untrusted INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
    FOREIGN KEY (room_id) REFERENCES palace_rooms(id) ON DELETE CASCADE,
    FOREIGN KEY (closet_id) REFERENCES palace_closets(id) ON DELETE SET NULL);
CREATE TABLE palace_halls (id INTEGER PRIMARY KEY AUTOINCREMENT, hall TEXT NOT NULL,
    compressed TEXT NOT NULL, layer INTEGER NOT NULL DEFAULT 2, created_at TEXT NOT NULL,
    untrusted INTEGER NOT NULL DEFAULT 0);
INSERT INTO memories (label, content, created_at, untrusted)
    VALUES ('people', 'legacy exact twin', '2026-08-01T00:00:00', 0);
INSERT INTO palace_wings (name) VALUES ('people');
INSERT INTO palace_rooms (wing_id, name) VALUES (1, 'people');
INSERT INTO palace_closets (room_id, layer, compressed, token_est, created_at, updated_at)
    VALUES (1, 2, 'people.people: legacy exact twin', 5, '2026-08-01T00:00:00', '2026-08-01T00:00:00');
INSERT INTO palace_drawers (closet_id, room_id, content, tags, untrusted, created_at)
    VALUES (1, 1, 'legacy exact twin', '[]', 1, '2026-08-01T00:00:00');
INSERT INTO palace_halls (hall, compressed, layer, created_at, untrusted)
    VALUES ('facts', 'FACTS: legacy exact twin', 2, '2026-08-01T00:00:00', 0);
"""


def test_migration_adds_nullable_links_and_never_links_legacy_rows(tmp_path, monkeypatch):
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA)
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", path)

    palace.init_palace_db()
    palace.init_palace_db()  # idempotent

    c = _raw(path)
    for table in ("palace_drawers", "palace_halls"):
        cols = {r["name"]: r for r in c.execute(f"PRAGMA table_info({table})")}
        assert "source_memory_id" in cols and cols["source_memory_id"]["notnull"] == 0
        fks = [tuple(r) for r in c.execute(f"PRAGMA foreign_key_list({table})")
               if r["from"] == "source_memory_id"]
        assert fks and fks[0][2] == "memories" and fks[0][4] == "id"
        assert fks[0][6] == "NO ACTION"  # deliberately not CASCADE
        idx = {r["name"] for r in c.execute(f"PRAGMA index_list({table})")}
        assert f"idx_{table}_source_memory_id" in idx
    # The legacy drawer and hall have an exact-content twin in memories, and
    # still stay unlinked: no content matching, no inferred provenance.
    assert c.execute("SELECT source_memory_id FROM palace_drawers").fetchall()[0][0] is None
    assert c.execute("SELECT source_memory_id FROM palace_halls").fetchall()[0][0] is None
    assert c.execute("SELECT COUNT(*) FROM palace_drawers WHERE source_memory_id IS NOT NULL"
                     ).fetchone()[0] == 0
    c.close()


def test_palace_only_init_creates_the_fk_parent_so_palace_writes_work(tmp_path, monkeypatch):
    """With FKs on, SQLite refuses any insert into a table whose FK parent
    table doesn't exist -- even with a NULL key. init_palace_db() alone must
    therefore leave the Palace usable."""
    path = str(tmp_path / "palace_only.db")
    monkeypatch.setattr(config, "DB_PATH", path)
    palace.init_palace_db()
    r = palace.palace_store("works", wing="people", room="x", untrusted=True)
    assert r["drawer_id"]
    assert palace.palace_store_hall("works too", hall="facts", untrusted=True)


# ── Correct links on every save_memory path ───────────────────────────────────

def test_settings_add_shape_links_drawer_and_keeps_trust(db):
    result = memory.save_memory("owner typed fact", "people", untrusted=False)  # _add_memory()
    assert result.startswith("Memory saved")
    mid = _latest_memory_id(db)
    drawers, halls = _derived(db, mid)
    assert len(drawers) == 1 and drawers[0]["untrusted"] == 0 and drawers[0]["closet_id"]
    assert halls == []  # "people" maps to no hall


def test_paste_import_shape_links_as_lower_trust(db):
    memory.save_memory("pasted line", "imported", untrusted=True)  # _paste_import()
    drawers, _ = _derived(db, _latest_memory_id(db))
    assert len(drawers) == 1 and drawers[0]["untrusted"] == 1


def test_model_save_memory_tool_links_drawer_and_hall(db):
    tools = {}

    class Reg:
        def register(self, name, fn, *a, **k):
            tools[name] = fn

    memory.register_memory_tools(Reg())
    tools["save_memory"](content="a discovered fact", label="fact")  # fact -> sessions + facts hall
    mid = _latest_memory_id(db)
    drawers, halls = _derived(db, mid)
    assert len(drawers) == 1 and drawers[0]["untrusted"] == 1
    assert len(halls) == 1 and halls[0]["untrusted"] == 1


def test_each_save_links_only_its_own_derivatives(db):
    memory.save_memory("first", "fact", untrusted=True)
    first = _latest_memory_id(db)
    memory.save_memory("second", "fact", untrusted=True)
    second = _latest_memory_id(db)
    d1, h1 = _derived(db, first)
    d2, h2 = _derived(db, second)
    assert len(d1) == len(d2) == len(h1) == len(h2) == 1
    assert d1[0]["id"] != d2[0]["id"] and h1[0]["id"] != h2[0]["id"]
    assert d1[0]["closet_id"] == d2[0]["closet_id"]  # same rolling closet, separate links


# ── Unlinked writers stay unlinked ────────────────────────────────────────────

def test_model_native_palace_tools_stay_unlinked(db):
    tools = {}

    class Reg:
        def register(self, name, fn, *a, **k):
            tools[name] = fn

    palace.register_palace_tools(Reg())
    tools["palace_remember"](content="model note", wing="people", room="people")
    tools["palace_hall"](content="model hall fact", hall="facts")
    c = _raw(db)
    assert c.execute("SELECT COUNT(*) FROM palace_drawers WHERE source_memory_id IS NOT NULL"
                     ).fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM palace_halls WHERE source_memory_id IS NOT NULL"
                     ).fetchone()[0] == 0
    c.close()


def test_dreaming_write_stays_unlinked(db, monkeypatch):
    from core import dreaming
    chat_id = memory.create_chat("idle")
    monkeypatch.setattr(dreaming.config, "DREAM_SWEEP_ENABLED", True)
    monkeypatch.setattr(dreaming.config, "DREAM_MIN_TOKENS", 1)
    monkeypatch.setattr(dreaming.config, "HUMAN_PROFILE_CURATION_ENABLED", False, raising=False)
    monkeypatch.setattr(dreaming, "load_chat_messages", lambda cid: [
        {"role": "user", "content": "x" * 50, "created_at": "2026-07-09T00:00:00"}])
    monkeypatch.setattr(dreaming, "run_summarization_call", lambda *a, **k: "- did a thing")
    dreaming._last_dream_sweep.pop(chat_id, None)

    assert dreaming._run_session_idle_sweep(chat_id) == dreaming.DREAM_COMPLETED

    c = _raw(db)
    rows = c.execute("SELECT source_memory_id FROM palace_drawers WHERE tags LIKE '%dream-sweep%'"
                     ).fetchall()
    c.close()
    assert rows and all(r[0] is None for r in rows)


def test_only_save_memory_can_set_a_source_link():
    """Structural: the public Palace write API has no source parameter, and
    no module other than tools/memory.py reaches the connection-aware
    helpers -- so Dreaming, compaction, palace_remember and palace_hall
    cannot link, whatever arguments they pass."""
    assert "source_memory_id" not in inspect.signature(palace.palace_store).parameters
    assert "source_memory_id" not in inspect.signature(palace.palace_store_hall).parameters
    offenders = []
    for py in _tracked_product_py():
        rel = py.relative_to(REPO).as_posix()
        if rel.startswith(("tests/", ".git/")) or rel in ("tools/palace.py", "tools/memory.py"):
            continue
        text = py.read_text(encoding="utf-8", errors="replace")
        if "_store_in_conn" in text or "_store_hall_in_conn" in text or "source_memory_id" in text:
            offenders.append(rel)
    assert offenders == []


# ── FK enforcement ────────────────────────────────────────────────────────────

def test_derivative_cannot_reference_a_missing_memory(db):
    conn = core_db.connect(db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            palace._store_in_conn(conn, "x", "people", "p", 2, None, True, True,
                                  source_memory_id=987654)
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            palace._store_hall_in_conn(conn, "x", "facts", 2, True, source_memory_id=987654)
        conn.rollback()
    finally:
        conn.close()


def test_deleting_a_linked_memory_directly_fails(db):
    memory.save_memory("keep my derivatives honest", "fact", untrusted=True)
    mid = _latest_memory_id(db)
    conn = core_db.connect(db)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute("DELETE FROM memories WHERE id=?", (mid,))  # any raw, non-lifecycle delete
    conn.close()
    # (01B-2 routes Settings and the approved action through
    # delete_memory_with_derivatives(); see test_palace_guard_01b2_*.)
    drawers, halls = _derived(db, mid)
    assert _latest_memory_id(db) == mid and drawers and halls


def test_unlinked_legacy_memory_still_deletes(db):
    c = _raw(db)
    c.execute("INSERT INTO memories (label, content, created_at) VALUES ('general','old','2026-08-01')")
    c.commit()
    mid = c.execute("SELECT MAX(id) FROM memories").fetchone()[0]
    c.close()
    result = memory._delete_memory_direct(mid)
    assert result.startswith(f"Memory {mid} deleted.")
    assert "predates deterministic Palace linkage" in result  # 01B-2 legacy notice


# ── Atomicity ─────────────────────────────────────────────────────────────────

def test_failed_drawer_write_rolls_back_the_flat_memory(db, monkeypatch):
    before = _counts(db)
    real = palace._store_in_conn

    def fails_after_writing(conn, *a, **k):
        real(conn, *a, **k)  # drawer + closet really written inside the txn...
        raise RuntimeError("disk on fire")  # ...then the write-through fails

    monkeypatch.setattr(palace, "_store_in_conn", fails_after_writing)
    result = memory.save_memory("doomed", "people", untrusted=True)
    assert result.startswith("[Error") and "not saved" in result
    assert "doomed" not in result
    assert _counts(db) == before


def test_failed_hall_write_rolls_back_flat_drawer_and_closet(db, monkeypatch):
    before = _counts(db)

    def hall_fails(*a, **k):
        raise sqlite3.OperationalError("hall table unavailable")

    monkeypatch.setattr(palace, "_store_hall_in_conn", hall_fails)
    result = memory.save_memory("doomed fact", "fact", untrusted=True)  # fact maps to a hall
    assert result.startswith("[Error")
    assert _counts(db) == before


def test_failed_flat_write_creates_no_palace_derivative(db):
    c = _raw(db)
    c.execute("CREATE TRIGGER no_flat BEFORE INSERT ON memories "
              "BEGIN SELECT RAISE(ABORT, 'flat write refused'); END")
    c.commit()
    c.close()
    before = _counts(db)
    result = memory.save_memory("never lands", "fact", untrusted=True)
    assert result.startswith("[Error")
    assert _counts(db) == before


def test_concurrent_saves_and_deletes_never_orphan_a_derivative(db):
    c = _raw(db)
    for i in range(10):  # unlinked legacy rows the deleters may legitimately remove
        c.execute("INSERT INTO memories (label, content, created_at) VALUES ('general', ?, '2026-08-01')",
                  (f"legacy {i}",))
    c.commit()
    c.close()
    errors = []

    def saver(n):
        for i in range(8):
            r = memory.save_memory(f"saver {n} item {i}", "fact", untrusted=True)
            if not r.startswith("Memory saved"):
                errors.append(r)

    def deleter():
        for _ in range(40):
            conn = core_db.connect(db)
            try:
                mid = conn.execute("SELECT id FROM memories ORDER BY RANDOM() LIMIT 1").fetchone()
                if mid:
                    try:
                        conn.execute("DELETE FROM memories WHERE id=?", (mid[0],))
                        conn.commit()
                    except sqlite3.IntegrityError:
                        conn.rollback()  # linked: refused, never orphaned
            finally:
                conn.close()

    threads = [threading.Thread(target=saver, args=(n,)) for n in range(3)]
    threads += [threading.Thread(target=deleter) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert errors == []
    c = _raw(db)
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    assert c.execute("""SELECT COUNT(*) FROM palace_drawers d WHERE d.source_memory_id IS NOT NULL
                        AND NOT EXISTS (SELECT 1 FROM memories m WHERE m.id=d.source_memory_id)"""
                     ).fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM palace_drawers WHERE source_memory_id IS NOT NULL"
                     ).fetchone()[0] == 24  # every save survived with its link
    c.close()


def test_delete_blocked_behind_an_open_save_cannot_orphan(db, monkeypatch):
    """A delete that arrives while save_memory's transaction is open waits on
    the write lock, then meets the committed link and is refused."""
    release, entered = threading.Event(), threading.Event()
    real = palace._store_in_conn

    def slow(conn, *a, **k):
        out = real(conn, *a, **k)
        entered.set()
        assert release.wait(10)
        return out

    monkeypatch.setattr(palace, "_store_in_conn", slow)
    saver = threading.Thread(target=memory.save_memory, args=("racy", "people"),
                             kwargs={"untrusted": True})
    saver.start()
    assert entered.wait(10)
    outcome = {}

    def delete_everything():
        conn = core_db.connect(db)
        try:
            conn.execute("DELETE FROM memories")
            conn.commit()
            outcome["deleted"] = True
        except sqlite3.IntegrityError:
            conn.rollback()
            outcome["deleted"] = False
        finally:
            conn.close()

    deleter = threading.Thread(target=delete_everything)
    deleter.start()
    release.set()
    saver.join(10)
    deleter.join(10)
    assert outcome == {"deleted": False}
    c = _raw(db)
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    assert c.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    c.close()


# ── Unchanged in B1 ───────────────────────────────────────────────────────────

def test_l0_l1_behavior_is_unchanged_in_b1(db):
    """01B-3 adds the admission boundary; B1 must not change layer behavior."""
    tools = {}

    class Reg:
        def register(self, name, fn, *a, **k):
            tools[name] = fn

    palace.register_palace_tools(Reg())
    tools["palace_remember"](content="still L1 in B1", wing="identity", room="self", layer=1)
    c = _raw(db)
    row = c.execute("SELECT c.layer, d.source_memory_id FROM palace_closets c "
                    "JOIN palace_drawers d ON d.closet_id=c.id WHERE d.content='still L1 in B1'"
                    ).fetchone()
    c.close()
    assert row["layer"] == 1 and row["source_memory_id"] is None


def test_tests_never_touch_owner_data(db):
    assert os.environ.get("LUMINA_TESTING")
    real_home = os.path.realpath(os.path.expanduser("~/.local/share/lumina"))
    assert not os.path.realpath(db).startswith(real_home)
    from core.test_isolation import refuse_if_production_path
    with pytest.raises(RuntimeError):
        refuse_if_production_path(os.path.join(real_home, "memory", "lumina.db"))


# ── Settings reporting (Qt; skipped where PySide6 is absent) ─────────────────

def test_settings_add_and_paste_import_report_failures(db, monkeypatch):
    pytest.importorskip("PySide6")
    from ui.main_window import COLORS
    from ui.settings import memory_tab

    shown = []
    monkeypatch.setattr(memory_tab.QMessageBox, "warning",
                        lambda *a: shown.append(("warning", a[-1])))
    monkeypatch.setattr(memory_tab.QMessageBox, "information",
                        lambda *a: shown.append(("info", a[-1])))
    def palace_write_fails(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(palace, "_store_in_conn", palace_write_fails)
    tab = memory_tab.MemoryTab(None, COLORS)
    tab.new_content.setText("typed but failing")
    tab._add_memory()
    assert shown[-1][0] == "warning" and tab.new_content.text() == "typed but failing"

    monkeypatch.setattr(memory_tab.QInputDialog, "getMultiLineText",
                        lambda *a, **k: ("one\ntwo", True))
    tab._paste_import()
    assert shown[-1] == ("info", "Imported 0 memories. 2 could not be saved.")
