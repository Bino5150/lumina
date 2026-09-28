"""PALACE-GUARD-01B-2 -- delete propagation + safe closet quarantine.

delete_memory_with_derivatives() is the single lifecycle delete for flat
memories (Settings and the approved delete_memory action). Linked drawers
leave their closets under one law -- single-drawer: delete; render-exact:
rebuild from the rest; drifted: withhold, never splice or re-render -- linked
halls are deleted, and the flat row goes last, all in one transaction.
Withheld closets never reach ordinary model-facing retrieval and stay
withheld until remediation or until their last drawer is gone.
palace_undo_write only touches drawers whose trusted origin stamp proves
they are synthesized nightstand writes.
"""
import json
import pathlib
import sqlite3
import threading

import pytest

import config
from core import db as core_db
from tools import memory, palace

REPO = pathlib.Path(__file__).resolve().parent.parent
WITHHELD = palace.WITHHELD_SOURCE_DELETED


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
    path = str(tmp_path / "lifecycle.db")
    monkeypatch.setattr(config, "DB_PATH", path)
    palace.init_palace_db()
    memory.init_chat_db()
    return path


def _raw(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _q(path, sql, *args):
    c = _raw(path)
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def _exec(path, sql, *args):
    c = _raw(path)
    c.execute(sql, args)
    c.commit()
    c.close()


def _save(label, content, untrusted=True):
    assert memory.save_memory(content, label, untrusted=untrusted).startswith("Memory saved")
    return _q(config.DB_PATH, "SELECT MAX(id) AS m FROM memories")[0]["m"]


def _drawer_of(path, mid):
    return _q(path, "SELECT * FROM palace_drawers WHERE source_memory_id=?", mid)[0]


def _closet(path, closet_id):
    rows = _q(path, "SELECT * FROM palace_closets WHERE id=?", closet_id)
    return rows[0] if rows else None


def _fingerprint(path):
    c = sqlite3.connect(path)
    out = [c.execute(f'SELECT * FROM "{t}" ORDER BY rowid').fetchall()
           for t in ("memories", "palace_drawers", "palace_closets", "palace_halls")]
    c.close()
    return out


def _drift(path, closet_id):
    _exec(path, "UPDATE palace_closets SET compressed = compressed || ' | legacy drift' WHERE id=?",
          closet_id)


def _chat():
    return memory.create_chat("c")


def _dream(chat_id, content, *, origin="dream_sweep"):
    return palace.palace_store(content, wing="nightstand", room=str(chat_id), layer=2,
                               tags=["dream-sweep", f"session:{chat_id}"], untrusted=True,
                               origin=origin)


# ── One canonical delete path ─────────────────────────────────────────────────

def test_approved_delete_memory_action_uses_the_lifecycle_function(db, monkeypatch):
    from tools import pending_actions
    mid = _save("fact", "approved path")
    calls = []
    real = memory.delete_memory_with_derivatives
    monkeypatch.setattr(memory, "delete_memory_with_derivatives",
                        lambda m: calls.append(m) or real(m))
    staged = memory.delete_memory(mid)
    aid = staged.split("#")[1].split()[0]
    result = pending_actions._apply_action(aid, agent=None)
    assert calls == [mid]
    assert result.startswith(f"Memory {mid} deleted with its linked Palace copies")
    assert _q(db, "SELECT COUNT(*) AS n FROM memories WHERE id=?", mid)[0]["n"] == 0


def test_no_raw_memory_delete_remains_outside_the_lifecycle_function():
    hits = []
    for py in _tracked_product_py():
        rel = py.relative_to(REPO).as_posix()
        if rel.startswith(("tests/", ".git/")):
            continue
        for i, line in enumerate(py.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if "DELETE FROM memories" in line:
                hits.append((rel, i))
    assert len(hits) == 1 and hits[0][0] == "tools/memory.py"
    src = (REPO / "tools/memory.py").read_text()
    fn = src[src.index("def delete_memory_with_derivatives"):src.index("def describe_memory_deletion")]
    assert "DELETE FROM memories" in fn
    tab = (REPO / "ui/settings/memory_tab.py").read_text()
    assert "delete_memory_with_derivatives" in tab and "DELETE FROM memories" not in tab


def test_linked_parent_cannot_be_deleted_around_the_lifecycle(db):
    mid = _save("fact", "guarded")
    conn = core_db.connect(db)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute("DELETE FROM memories WHERE id=?", (mid,))
    conn.close()


# ── Linked drawer + hall ──────────────────────────────────────────────────────

def test_linked_drawer_hall_and_single_drawer_closet_go_together(db):
    mid = _save("fact", "atomic trio")  # fact -> sessions/fact drawer + facts hall
    d = _drawer_of(db, mid)
    assert _q(db, "SELECT COUNT(*) AS n FROM palace_halls WHERE source_memory_id=?", mid)[0]["n"] == 1

    r = memory.delete_memory_with_derivatives(mid)

    assert r == {"found": True, "memory_id": mid, "drawers_removed": 1, "halls_removed": 1,
                 "closets": {"closet_deleted": 1}, "legacy_unlinked": False}
    assert _closet(db, d["closet_id"]) is None
    assert _q(db, "SELECT COUNT(*) AS n FROM palace_drawers WHERE id=?", d["id"])[0]["n"] == 0
    assert _q(db, "SELECT COUNT(*) AS n FROM palace_halls WHERE source_memory_id=?", mid)[0]["n"] == 0


def test_shared_exact_closet_loses_only_the_deleted_source(db):
    a = _save("people", "alpha person fact")
    b = _save("people", "beta person fact", untrusted=False)
    native = palace.palace_store("gamma model note", wing="people", room="people", untrusted=True)
    closet_id = _drawer_of(db, a)["closet_id"]
    assert _drawer_of(db, b)["closet_id"] == closet_id == native["closet_id"]
    before = _closet(db, closet_id)["compressed"]
    kept = _q(db, "SELECT content, untrusted FROM palace_drawers WHERE closet_id=? AND id!=? "
                  "ORDER BY created_at, id", closet_id, _drawer_of(db, a)["id"])
    expected = palace.render_closet_text("people.people", kept)

    r = memory.delete_memory_with_derivatives(a)

    assert r["closets"] == {"closet_rebuilt": 1}
    after = _closet(db, closet_id)
    assert after["compressed"] == expected and after["withheld_reason"] is None
    # every unrelated segment survives byte for byte, in order
    for row in kept:
        seg = palace.render_closet_text("people.people", [row])
        assert seg in before and seg in after["compressed"]
    assert "alpha" not in after["compressed"]
    assert _drawer_of(db, b)["closet_id"] == closet_id
    assert _q(db, "SELECT COUNT(*) AS n FROM palace_drawers WHERE id=?",
              native["drawer_id"])[0]["n"] == 1


# ── Drifted closets are withheld, not re-rendered ─────────────────────────────

def _withheld_setup(db):
    a = _save("people", "WITHHELD_ALPHA fact")
    b = _save("people", "WITHHELD_BETA fact")
    closet_id = _drawer_of(db, a)["closet_id"]
    _drift(db, closet_id)
    frozen = _closet(db, closet_id)["compressed"]
    r = memory.delete_memory_with_derivatives(a)
    return a, b, closet_id, frozen, r


def test_drifted_closet_is_withheld_not_rerendered(db):
    a, b, closet_id, frozen, r = _withheld_setup(db)
    assert r["closets"] == {"closet_withheld": 1}
    c = _closet(db, closet_id)
    assert c["withheld_reason"] == WITHHELD
    assert c["compressed"] == frozen  # no splicing, no re-render of unrelated drawers
    assert _q(db, "SELECT COUNT(*) AS n FROM palace_drawers WHERE source_memory_id=?", a)[0]["n"] == 0
    assert _drawer_of(db, b)["closet_id"] == closet_id
    assert "withheld from Lumina's memory pending review" in memory.describe_memory_deletion(r)


def test_withheld_closet_never_reaches_model_facing_retrieval(db):
    a, b, closet_id, frozen, _ = _withheld_setup(db)
    block = palace.build_context_block(max_tokens=100000)
    assert "WITHHELD_ALPHA" not in block and "WITHHELD_BETA" not in block
    assert "No memories found" in palace.palace_recall("WITHHELD")
    assert all(r["closet_id"] != closet_id for r in palace.list_flagged_writes("trust:untrusted"))
    assert closet_id not in {c["id"] for c in palace.load_layer(2)}
    _exec(db, "UPDATE palace_drawers SET tags='[\"session:1\"]' WHERE closet_id=?", closet_id)
    assert closet_id not in palace._find_pinned_closet_ids("session:1")


@pytest.mark.parametrize("state", ["bogus_unknown_state", "", " "])
def test_unknown_or_malformed_withheld_state_fails_closed(db, state):
    mid = _save("people", "MALFORMED_STATE_MARKER fact")
    closet_id = _drawer_of(db, mid)["closet_id"]
    _exec(db, "UPDATE palace_closets SET withheld_reason=? WHERE id=?", state, closet_id)
    assert "MALFORMED_STATE_MARKER" not in palace.build_context_block(max_tokens=100000)
    assert "No memories found" in palace.palace_recall("MALFORMED_STATE_MARKER")


def test_later_operations_never_unwithhold(db):
    a, b, closet_id, frozen, _ = _withheld_setup(db)
    # a new save into the same room opens a fresh closet instead of merging in
    c = _save("people", "WITHHELD_GAMMA fact")
    assert _drawer_of(db, c)["closet_id"] != closet_id
    assert "WITHHELD_GAMMA" in palace.build_context_block(max_tokens=100000)
    # a rebuild (e.g. the startup migration) leaves it frozen and withheld
    conn = core_db.connect(db)
    palace._rebuild_closet_from_drawers(conn, closet_id)
    conn.commit()
    conn.close()
    palace.init_palace_db()
    after = _closet(db, closet_id)
    assert after["withheld_reason"] == WITHHELD and after["compressed"] == frozen


def test_final_drawer_removal_deletes_the_withheld_closet(db):
    a, b, closet_id, frozen, _ = _withheld_setup(db)
    r = memory.delete_memory_with_derivatives(b)
    assert r["closets"] == {"withheld_closet_emptied": 1}
    assert _closet(db, closet_id) is None


def test_withheld_closet_keeps_state_when_another_source_leaves(db):
    a = _save("people", "one")
    b = _save("people", "two")
    c = _save("people", "three")
    closet_id = _drawer_of(db, a)["closet_id"]
    _drift(db, closet_id)
    memory.delete_memory_with_derivatives(a)
    r = memory.delete_memory_with_derivatives(b)
    assert r["closets"] == {"withheld_kept": 1}
    assert _closet(db, closet_id)["withheld_reason"] == WITHHELD
    assert _drawer_of(db, c)["closet_id"] == closet_id


# ── Failure / concurrency ─────────────────────────────────────────────────────

def test_propagation_failure_leaves_memory_and_derivatives_intact(db, monkeypatch):
    mid = _save("fact", "survives a failed delete")
    _save("fact", "a second drawer so the closet is shared")
    before = _fingerprint(db)
    real = palace._remove_drawer_in_conn

    def fails_midway(conn, drawer_id):
        real(conn, drawer_id)  # drawer really removed inside the transaction...
        raise RuntimeError("crash")  # ...then the lifecycle fails

    monkeypatch.setattr(palace, "_remove_drawer_in_conn", fails_midway)
    with pytest.raises(RuntimeError):
        memory.delete_memory_with_derivatives(mid)
    assert _fingerprint(db) == before


def test_failure_after_derivatives_are_removed_rolls_everything_back(db):
    """The flat row is deleted last; if THAT fails, the already-removed
    drawer, closet and hall must come back -- nothing is committed early."""
    mid = _save("fact", "last step fails")
    _save("fact", "shared closet neighbour")
    _exec(db, "CREATE TRIGGER no_delete BEFORE DELETE ON memories "
              "BEGIN SELECT RAISE(ABORT, 'flat delete refused'); END")
    before = _fingerprint(db)
    with pytest.raises(sqlite3.IntegrityError):
        memory.delete_memory_with_derivatives(mid)
    assert _fingerprint(db) == before


def test_concurrent_saves_and_lifecycle_deletes_stay_orphan_free(db):
    errors = []

    def saver(n):
        for i in range(8):
            r = memory.save_memory(f"s{n}-{i}", "fact", untrusted=True)
            if not r.startswith("Memory saved"):
                errors.append(r)

    def deleter():
        for _ in range(20):
            rows = _q(db, "SELECT id FROM memories ORDER BY RANDOM() LIMIT 1")
            if rows:
                memory.delete_memory_with_derivatives(rows[0]["id"])

    threads = [threading.Thread(target=saver, args=(n,)) for n in range(3)]
    threads += [threading.Thread(target=deleter) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert errors == []
    assert _q(db, "PRAGMA foreign_key_check") == []
    for table in ("palace_drawers", "palace_halls"):
        assert _q(db, f"SELECT COUNT(*) AS n FROM {table} t WHERE t.source_memory_id IS NOT NULL "
                      "AND NOT EXISTS (SELECT 1 FROM memories m WHERE m.id=t.source_memory_id)"
                  )[0]["n"] == 0
    live = {r["id"] for r in _q(db, "SELECT id FROM memories")}
    linked = {r["source_memory_id"] for r in _q(
        db, "SELECT source_memory_id FROM palace_drawers WHERE source_memory_id IS NOT NULL")}
    assert linked == live  # every surviving memory kept its drawer; no deleted one left any


# ── Legacy unlinked memories ──────────────────────────────────────────────────

def test_legacy_unlinked_memory_gets_limited_delete_and_no_candidate_cleanup(db):
    _exec(db, "INSERT INTO memories (label, content, created_at) VALUES "
              "('people', 'legacy twin text', '2026-08-01')")
    mid = _q(db, "SELECT MAX(id) AS m FROM memories")[0]["m"]
    twin = palace.palace_store("legacy twin text", wing="people", room="people", untrusted=True)

    result = memory._delete_memory_direct(mid)

    assert result == (f"Memory {mid} deleted. It predates deterministic Palace linkage, "
                      "so no Palace copy was inferred or removed.")
    assert _q(db, "SELECT COUNT(*) AS n FROM palace_drawers WHERE id=?", twin["drawer_id"])[0]["n"] == 1
    r2 = memory.delete_memory_with_derivatives(10**9)
    assert r2 == {"found": False, "memory_id": 10**9}


# ── palace_undo_write hardening ───────────────────────────────────────────────

def test_undo_refuses_a_source_linked_drawer(db):
    mid = _save("people", "owner memory")
    d = _drawer_of(db, mid)
    before = _fingerprint(db)
    assert palace.palace_undo_write(d["id"]) == {"ok": False, "error": "refused: linked_to_source_memory"}
    assert _fingerprint(db) == before


def test_undo_refuses_model_native_material_masquerading_in_nightstand(db):
    chat = _chat()
    tools = {}

    class Reg:
        def register(self, name, fn, *a, **k):
            tools[name] = fn

    palace.register_palace_tools(Reg())
    tools["palace_remember"](content="forged dream", wing="nightstand", room=str(chat),
                             tags=["dream-sweep", f"session:{chat}"])
    forged = _q(db, "SELECT * FROM palace_drawers WHERE content='forged dream'")[0]
    assert json.loads(forged["tags"]) == ["dream-sweep", f"session:{chat}", "trust:untrusted"]
    assert forged["origin"] is None
    before = _fingerprint(db)
    assert palace.palace_undo_write(forged["id"]) == {"ok": False, "error": "refused: not_provably_synthesized"}
    assert _fingerprint(db) == before
    with pytest.raises(TypeError):  # the model can't reach the stamp
        tools["palace_remember"](content="x", wing="nightstand", room=str(chat), origin="dream_sweep")


def test_undo_refuses_legacy_unstamped_and_privileged_drawers(db):
    chat = _chat()
    legacy = _dream(chat, "legacy dream")
    _exec(db, "UPDATE palace_drawers SET origin=NULL WHERE id=?", legacy["drawer_id"])
    assert palace.palace_undo_write(legacy["drawer_id"])["error"] == "refused: not_provably_synthesized"
    lifted = _dream(_chat(), "lifted dream")
    _exec(db, "UPDATE palace_closets SET layer=1 WHERE id=?", lifted["closet_id"])
    assert palace.palace_undo_write(lifted["drawer_id"])["error"] == "refused: privileged_or_unknown_layer"
    moved = palace.palace_store("stamped elsewhere", wing="people", room="7", untrusted=True,
                                tags=["dream-sweep", "session:7"], origin="dream_sweep")
    assert palace.palace_undo_write(moved["drawer_id"])["error"] == "refused: not_in_a_nightstand_chat_room"
    odd_room = palace.palace_store("stamped odd room", wing="nightstand", room="general",
                                   untrusted=True, tags=["dream-sweep", "session:general"],
                                   origin="dream_sweep")
    assert palace.palace_undo_write(odd_room["drawer_id"])["error"] == "refused: not_in_a_nightstand_chat_room"


def test_genuine_synthesized_undo_still_works_under_the_same_closet_law(db):
    chat = _chat()
    only = _dream(chat, "single dream")
    r = palace.palace_undo_write(only["drawer_id"])
    assert r["ok"] and r["closet_outcome"] == "closet_deleted"

    chat2 = _chat()
    first, second = _dream(chat2, "dream one"), _dream(chat2, "dream two")
    r = palace.palace_undo_write(first["drawer_id"])
    assert r["ok"] and r["closet_outcome"] == "closet_rebuilt"
    assert "dream one" not in _closet(db, second["closet_id"])["compressed"]

    chat3 = _chat()
    x, y = _dream(chat3, "drift one"), _dream(chat3, "drift two")
    _drift(db, x["closet_id"])
    r = palace.palace_undo_write(x["drawer_id"])
    assert r["ok"] and r["closet_outcome"] == "closet_withheld"


def test_real_dreaming_sweep_is_stamped_and_undoable(db, monkeypatch):
    from core import dreaming
    chat = _chat()
    monkeypatch.setattr(dreaming.config, "DREAM_SWEEP_ENABLED", True)
    monkeypatch.setattr(dreaming.config, "DREAM_MIN_TOKENS", 1)
    monkeypatch.setattr(dreaming.config, "HUMAN_PROFILE_CURATION_ENABLED", False, raising=False)
    monkeypatch.setattr(dreaming, "load_chat_messages", lambda cid: [
        {"role": "user", "content": "x" * 50, "created_at": "2026-07-09T00:00:00"}])
    monkeypatch.setattr(dreaming, "run_summarization_call", lambda *a, **k: "- real sweep")
    dreaming._last_dream_sweep.pop(chat, None)
    assert dreaming._run_session_idle_sweep(chat) == dreaming.DREAM_COMPLETED
    d = _q(db, "SELECT * FROM palace_drawers WHERE content='- real sweep'")[0]
    assert d["origin"] == "dream_sweep"
    assert palace.palace_undo_write(d["id"])["ok"]


def test_only_internal_synthesized_writers_stamp_an_origin():
    with pytest.raises(ValueError):
        palace.palace_store("x", origin="owner_approved")  # closed vocabulary
    stamps = {}
    for py in _tracked_product_py():
        rel = py.relative_to(REPO).as_posix()
        if rel.startswith(("tests/", ".git/")) or rel == "tools/palace.py":
            continue
        text = py.read_text(encoding="utf-8", errors="replace")
        for origin in palace.SYNTHESIZED_ORIGINS:
            if f'origin="{origin}"' in text:
                stamps.setdefault(rel, set()).add(origin)
    assert stamps == {"core/dreaming.py": {"dream_sweep"},
                      "core/manual_compaction.py": {"manual_compaction"},
                      "ui/main_window.py": {"auto_compaction"}}


def test_migration_classifies_nothing(tmp_path, monkeypatch):
    path = str(tmp_path / "old.db")
    monkeypatch.setattr(config, "DB_PATH", path)
    palace.init_palace_db()
    palace.palace_store("pre-01B2 dream", wing="nightstand", room="1",
                        tags=["dream-sweep", "session:1"], untrusted=True)
    palace.init_palace_db()
    row = _q(path, "SELECT d.origin, c.withheld_reason FROM palace_drawers d "
                   "JOIN palace_closets c ON c.id=d.closet_id WHERE d.content='pre-01B2 dream'")[0]
    assert row["origin"] is None and row["withheld_reason"] is None


# ── Settings (Qt; skipped where PySide6 is absent) ────────────────────────────

def test_settings_delete_uses_the_lifecycle_and_surfaces_notices(db, monkeypatch):
    pytest.importorskip("PySide6")
    from ui.main_window import COLORS
    from ui.settings import memory_tab

    linked = _save("fact", "settings linked")
    _exec(db, "INSERT INTO memories (label, content, created_at) VALUES ('general','legacy row','2026-08-01')")
    legacy = _q(db, "SELECT MAX(id) AS m FROM memories")[0]["m"]
    calls, shown = [], []
    real = memory.delete_memory_with_derivatives
    monkeypatch.setattr(memory, "delete_memory_with_derivatives",
                        lambda m: calls.append(m) or real(m))
    monkeypatch.setattr(memory_tab.QMessageBox, "question",
                        lambda *a, **k: memory_tab.QMessageBox.Yes)
    monkeypatch.setattr(memory_tab.QMessageBox, "information", lambda *a: shown.append(a[-1]))

    tab = memory_tab.MemoryTab(None, COLORS)
    tab.table.selectAll()
    tab._delete_selected()

    assert sorted(calls) == sorted([linked, legacy])
    assert _q(db, "SELECT COUNT(*) AS n FROM memories")[0]["n"] == 0
    assert len(shown) == 1 and "predates deterministic Palace linkage" in shown[0]
    assert f"Memory {legacy} deleted." in shown[0] and f"Memory {linked}" not in shown[0]


def test_lifecycle_delete_works_on_a_db_without_palace_tables(tmp_path, monkeypatch):
    """No Palace (or an unmigrated one) means nothing can be linked: the
    delete succeeds flat-only with the limited-result notice, never an error."""
    path = str(tmp_path / "memory_only.db")
    monkeypatch.setattr(config, "DB_PATH", path)
    memory.init_memory_db()
    _exec(path, "INSERT INTO memories (label, content, created_at) VALUES ('general','x','2026-08-01')")
    r = memory.delete_memory_with_derivatives(1)
    assert r["found"] and r["legacy_unlinked"] and r["drawers_removed"] == 0 and r["halls_removed"] == 0
    assert _q(path, "SELECT COUNT(*) AS n FROM memories")[0]["n"] == 0


def test_structural_scans_ignore_untracked_local_files(tmp_path):
    """A dev checkout carries gitignored scripts (reports/) that may quote
    product code; the scans must see only tracked product files."""
    import subprocess
    repo = tmp_path / "repo"
    (repo / "core").mkdir(parents=True)
    (repo / "reports").mkdir()
    (repo / "tests").mkdir()
    (repo / "core" / "real.py").write_text("x = 1\n")
    (repo / "tests" / "t.py").write_text("DELETE FROM memories\n")
    (repo / "reports" / "local.py").write_text('origin="auto_compaction"\nDELETE FROM memories\n')
    for cmd in (["init", "-q"], ["add", "core/real.py", "tests/t.py"]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True, capture_output=True)
    found = {p.relative_to(repo).as_posix() for p in _tracked_product_py(repo)}
    assert found == {"core/real.py"}
