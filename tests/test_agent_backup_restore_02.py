"""AGENT-BACKUP-RESTORE-02: isolated staging Restore into an empty data folder.

Synthetic disposable sources, archives, destinations and install folders
only. Every destination lives under tmp_path; the engine refuses production
roots unconditionally, which is pinned here without ever touching one.
"""
import ast
import contextlib
import errno
import hashlib
import io
import shutil
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

import pytest

import config
import core.agent_backup as ab
import core.agent_backup_export as abx
import core.agent_backup_restore as ar
import core.agent_backup_restore_intake as ri
from core import memory_backup, operator_ingress
from core import suspension as S
from core import suspension_backup as c1
from test_agent_backup import _make_env, NO_IDENTITY_TRAILERS, NO_CREDENTIALS

REPO = Path(__file__).resolve().parents[1]
SENTINELS = ("PIN-SENTINEL", "SALT-SENTINEL", "KEY-SENTINEL", "OWNER-SENTINEL", "SOUL-SENTINEL",
             "LEDGER-SENTINEL")
OFFICIAL_TEXT = b"# Official Skill\nShipped content.\n"


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

@pytest.fixture
def source(tmp_path, monkeypatch):
    """A source install with chats, Palace, memories, Skills (user and
    official), a project, and secrets that must never come back."""
    data, base = map(Path, _make_env(tmp_path / "src"))
    db = data / "memory/lumina.db"
    from core import skills
    from tools import palace
    monkeypatch.setattr(config, "DB_PATH", str(db))
    skills.init_skills_db(str(db))
    palace.init_palace_db()
    monkeypatch.undo()
    (base / "skills/official-thing.md").write_bytes(OFFICIAL_TEXT)
    with contextlib.closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("INSERT INTO chats (id, name, created_at, updated_at) VALUES (166, 'Prime', 't', 't')")
        for i, text in enumerate(("hello Lumina", "hello Bino", "I approve the deploy")):
            conn.execute("INSERT INTO chat_messages (chat_id, role, content, created_at) VALUES (166, ?, ?, 't')",
                         ("user" if i % 2 == 0 else "assistant", text))
        conn.execute("INSERT INTO memories (label, content, created_at) VALUES ('fact', 'Bino likes rain', 't')")
        room = conn.execute("INSERT INTO palace_rooms (wing_id, name) VALUES (1, 'origin')").lastrowid
        conn.execute("INSERT INTO palace_drawers (room_id, content, created_at) VALUES (?, 'first boot', 't')",
                     (room,))
        conn.execute("INSERT INTO skills (name, description, path, created_at, updated_at, origin) "
                     "VALUES ('Duck', 'debugging', ?, 't', 't', 'user')",
                     (str(base / "skills/rubber-duck-debugging.md"),))
        conn.execute("INSERT INTO skills (name, description, path, created_at, updated_at, origin, content_sha256) "
                     "VALUES ('Official Thing', 'shipped', ?, 't', 't', 'official', ?)",
                     (str(base / "skills/official-thing.md"), hashlib.sha256(OFFICIAL_TEXT).hexdigest()))
    (data / "knowledge_packs_installed.json").write_text(json.dumps({
        "lumina-self-knowledge": {"version": 1, "installed_at": "2026-10-20"},
        "future-pack": {"version": 3, "deleted_at": "2026-10-21", "extra": {"x": 1}}}))
    (data / "memory/prefs.json").write_text(json.dumps({
        "human_bio": "SYNTHETIC HUMAN", "last_persona": "/old/personas/lumina.json",
        "pin_hash": "PIN-SENTINEL", "pin_salt": "SALT-SENTINEL", "openai_api_key": "KEY-SENTINEL",
        "telegram_remote_access": True, "telegram_owner_chat_id": "OWNER-SENTINEL"}))
    with contextlib.closing(sqlite3.connect(data / "memory/ledger.db")) as conn, conn:
        conn.execute("INSERT INTO ledger VALUES ('LEDGER-SENTINEL')")
    return data, base


def _build_v2(source, dest):
    data, base = source
    abx.build_review_export_with_receipt(str(data), str(base), str(dest),
                                         identity_trailers_path=NO_IDENTITY_TRAILERS,
                                         credentials_path=NO_CREDENTIALS)
    return dest


@pytest.fixture
def v2(source, tmp_path):
    return _build_v2(source, tmp_path / "archives" / "v2.zip")


@pytest.fixture
def v1(source, tmp_path):
    data, base = source
    dest = tmp_path / "archives" / "v1.zip"
    dest.parent.mkdir(exist_ok=True)
    ab.build_agent_backup(str(data), str(base), str(dest),
                          identity_trailers_path=NO_IDENTITY_TRAILERS, credentials_path=NO_CREDENTIALS)
    return dest


class _FakeConn:
    def execute(self, *_a):
        return self

    def close(self):
        pass


@pytest.fixture
def legacy(source, tmp_path, monkeypatch):
    """A real Backup Memory (Legacy) ZIP holding archived C1 state."""
    data, _base = source
    (data / "memory/suspension.db").write_bytes(b"SYNTHETIC C1 STORE")
    (data / "memory/soul_identity.json").write_text('{"soul_id": "SOUL-SENTINEL"}')
    # The real builder checkpoints the live database first; _FakeConn stands
    # in for that connection, so checkpoint the synthetic source here.
    with contextlib.closing(sqlite3.connect(data / "memory/lumina.db")) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    monkeypatch.setattr(memory_backup, "_db", lambda: _FakeConn())
    dest = tmp_path / "archives" / "legacy.zip"
    dest.parent.mkdir(exist_ok=True)
    memory_backup.build_memory_backup(str(data), str(dest))
    return dest


@pytest.fixture
def target(tmp_path):
    """An empty destination path and a fresh install folder (the shipped
    persona and official Skill a fresh checkout would carry)."""
    base = tmp_path / "fresh_install"
    (base / "skills").mkdir(parents=True)
    (base / "personas").mkdir()
    (base / "skills/official-thing.md").write_bytes(OFFICIAL_TEXT)
    (tmp_path / "restore_parent").mkdir()
    return tmp_path / "restore_parent" / "data", base


def entries(path):
    with zipfile.ZipFile(path) as z:
        return [(i.filename, z.read(i.filename)) for i in z.infolist()]


def repack(path, items, canonical=True):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in items:
            z.writestr(ab._make_v1_zipinfo(name) if canonical else name, data)
    return path


def with_manifest(path, mutate):
    items = entries(path)
    manifest = json.loads(dict(items)["manifest.json"])
    mutate(manifest)
    return repack(path, [(n, json.dumps(manifest).encode() if n == "manifest.json" else d) for n, d in items])


def files_of(root):
    root = Path(root)
    if not root.exists():
        return None
    return {str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None)
            for p in sorted(root.rglob("*"))}


def restore(archive, data_dir, base_dir, **kw):
    plan = ar.plan_restore(archive, data_dir, base_dir, **kw)
    return plan, ar.restore_isolated(plan, archive, approved_plan_digest=plan["plan_digest"])


def leftovers(data_dir):
    return [p.name for p in Path(data_dir).parent.iterdir() if ".restore-" in p.name]


def rows(db, sql):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Complete controlled archive
# --------------------------------------------------------------------------

def test_verified_export_restores_into_an_empty_root_with_honest_labels(v2, source, target):
    data_dir, base = target
    plan, receipt = restore(v2, data_dir, base)
    assert plan["status"] == "ready"
    assert receipt["restore_class"] == ar.INCOMPLETE == "incomplete_state_export"
    assert receipt["verified_agent_backup"] == {"available": False, "reason": "c1_snapshot_contract_absent"}
    assert receipt["archive"]["archive_sha256"] == hashlib.sha256(v2.read_bytes()).hexdigest()
    assert receipt["state"] == "published_execution_fenced"
    assert ar.verify_restored_root(data_dir, receipt)["passed"] is True
    assert leftovers(data_dir) == []
    journal = json.loads((data_dir / "restore_history" / receipt["restore_id"] / "journal.json").read_text())
    assert journal["state"] == "published"


def test_recovered_conversation_palace_memory_and_project_facts_match_the_source(v2, source, target):
    src_data, src_base = source
    data_dir, base = target
    _plan, _receipt = restore(v2, data_dir, base)
    for sql in ("SELECT id, name FROM chats ORDER BY id",
                "SELECT chat_id, role, content FROM chat_messages ORDER BY id",
                "SELECT label, content FROM memories ORDER BY id",
                "SELECT r.name, d.content FROM palace_drawers d JOIN palace_rooms r ON r.id = d.room_id",
                "SELECT name, description FROM palace_wings ORDER BY id"):
        assert rows(data_dir / "memory/lumina.db", sql) == rows(src_data / "memory/lumina.db", sql)
    assert (data_dir / "projects/demo/chats.json").read_bytes() == (src_data / "projects/demo/chats.json").read_bytes()
    assert (base / "projects/demo/project.md").read_bytes() == (src_base / "projects/demo/project.md").read_bytes()
    assert (base / "projects/projectlist.md").read_bytes() == (src_base / "projects/projectlist.md").read_bytes()
    assert (base / "skills/rubber-duck-debugging.md").read_bytes() == \
        (src_base / "skills/rubber-duck-debugging.md").read_bytes()
    installed = json.loads((data_dir / "knowledge_packs_installed.json").read_text())
    assert installed["future-pack"] == {"version": 3, "deleted_at": "2026-10-21", "extra": {"x": 1}}


def test_v1_backup_restores_as_incomplete_export_with_older_coverage(v1, target):
    data_dir, base = target
    _plan, receipt = restore(v1, data_dir, base)
    assert receipt["restore_class"] == ar.INCOMPLETE
    assert receipt["coverage"]["format_coverage"] == "older_full_mode"
    assert receipt["coverage"]["installation_history"] == "not_captured_by_format"
    assert not (data_dir / "knowledge_packs_installed.json").exists()
    assert ar.verify_restored_root(data_dir, receipt)["passed"] is True


def test_empty_root_startup_binds_to_the_restored_state_without_authority(v2, source, target, tmp_path):
    data_dir, base = target
    _plan, receipt = restore(v2, data_dir, base)
    assert ar.verify_restored_root(data_dir, receipt)["passed"] is True
    probe = tmp_path / "probe.py"
    probe.write_text(textwrap.dedent("""
        import json, os, sys
        sys.path.insert(0, os.getcwd())
        import config
        from core.db import connect
        from core import skills
        from core import suspension as S
        from tools import toolmaker
        conn = connect()
        out = {
            "data_dir": os.path.realpath(config.DATA_DIR),
            "chats": [r[0] for r in conn.execute("SELECT name FROM chats ORDER BY id")],
            "messages": conn.execute("SELECT COUNT(*) FROM chat_messages WHERE chat_id=166").fetchone()[0],
            "duck": skills.load_skill("Duck"),
            "origins": dict(conn.execute("SELECT name, origin FROM skills").fetchall()),
            "c1": S.probe(config.DATA_DIR).status.value,
            "custom_tools_loaded": toolmaker.load_approved_custom_tools(None),
            "telegram": config._p.get("telegram_remote_access") if hasattr(config, "_p") else None,
        }
        conn.close()
        print(json.dumps(out))
    """))
    env = dict(os.environ, LUMINA_DATA_DIR=str(data_dir), LUMINA_TESTING="1",
               LUMINA_SECRETS_PATH=str(tmp_path / "no-credentials.json"),
               LUMINA_CHATGPT_AUTH_DIR=str(tmp_path / "chatgpt_auth"),
               LUMINA_RUNTIME_DIR=str(tmp_path / "runtime"))
    run = subprocess.run([sys.executable, str(probe)], cwd=REPO, env=env, capture_output=True,
                         text=True, timeout=120)
    assert run.returncode == 0, run.stderr[-2000:]
    out = json.loads(run.stdout.strip().splitlines()[-1])
    assert out["data_dir"] == os.path.realpath(data_dir)
    assert out["chats"] == ["Test Chat", "Prime"] and out["messages"] == 3
    assert out["duck"] == "# Skill: Rubber Duck Debugging\n"
    assert out["origins"]["Official Thing"] == "user"   # official comes only from bootstrap
    assert out["c1"] == "absent_with_trace"
    assert out["custom_tools_loaded"] == []
    assert out["telegram"] in (False, None)


# --------------------------------------------------------------------------
# Authority stays with the destination
# --------------------------------------------------------------------------

def test_destination_c1_is_independent_of_archived_state(legacy, target):
    data_dir, base = target
    _plan, receipt = restore(legacy, data_dir, base)
    assert not (data_dir / "memory/suspension.db").exists()
    assert not (data_dir / "memory/soul_identity.json").exists()
    assert S.probe(data_dir).status is S.StoreStatus.ABSENT_WITH_TRACE
    trace = json.loads((data_dir / receipt["c1"]["trace"]).read_text())
    assert trace["archived_c1_state"] == "not_restored" and "soul_id" not in trace
    # First-run Initialize is refused; owner-local Recover starts the owner suspended.
    with pytest.raises(S.RecoveryBlocked):
        S.initialize(data_dir, ingress=operator_ingress.local_suspension_control("initialize"))
    with pytest.raises(S.RecoveryBlocked):
        S.recover_destructively(data_dir, ingress=operator_ingress.local_suspension_control("recover"))
    outcome = S.recover_destructively(data_dir, ingress=operator_ingress.local_suspension_control("recover"),
                                      acknowledge_unreadable_runtimes=True)
    assert outcome.suspended_scopes == (S.OWNER_SCOPE,) and outcome.unknown_scope_policy == "suspended"
    with S.open_store(data_dir) as store:
        assert store.scope_state(S.OWNER_SCOPE).suspended
    assert (data_dir / receipt["c1"]["trace"]).exists()   # Recover preserved the trace


def test_a_relayed_ingress_cannot_recover_a_restored_root(v2, target):
    data_dir, base = target
    restore(v2, data_dir, base)
    with pytest.raises(Exception):
        S.recover_destructively(data_dir, ingress=object(), acknowledge_unreadable_runtimes=True)
    assert S.probe(data_dir).status is S.StoreStatus.ABSENT_WITH_TRACE


def test_credentials_owner_bindings_and_ledger_never_come_back(v1, legacy, target, tmp_path):
    for archive, name in ((v1, "a"), (legacy, "b")):
        data_dir = tmp_path / "restore_parent" / name
        _plan, receipt = restore(archive, data_dir, target[1])
        blob = b"".join(p.read_bytes() for p in data_dir.rglob("*") if p.is_file())
        assert not any(s.encode() in blob for s in SENTINELS), archive.name
        prefs = json.loads((data_dir / "memory/prefs.json").read_text())
        assert prefs["telegram_remote_access"] is False and prefs["human_bio"] == "SYNTHETIC HUMAN"
        assert receipt["rebind"]["preferences"]["security_fields_dropped"] >= 4
        assert not (data_dir / "memory/ledger.db").exists()
        assert receipt["fence"]["credentials_imported"] is False


def test_executable_authority_is_kept_only_as_inactive_history(v2, target):
    data_dir, base = target
    _plan, receipt = restore(v2, data_dir, base)
    history = data_dir / "restore_history" / receipt["restore_id"]
    assert (history / "custom_tools/my_tool.py").exists()
    assert (history / "audit/tool_audit.log").exists()
    assert (history / "projects/demo/binding.json").exists()
    for live in ("custom_tools", "memory/tool_audit.log", "memory/pending_actions.json",
                 "projects/demo/binding.json"):
        assert not (data_dir / live).exists(), live


def test_last_persona_is_rebound_to_the_destination_or_dropped(v2, target, tmp_path):
    data_dir, base = target
    _plan, receipt = restore(v2, data_dir, base)
    prefs = json.loads((data_dir / "memory/prefs.json").read_text())
    assert prefs["last_persona"] == str(base / "personas/lumina.json")
    assert receipt["rebind"]["preferences"]["last_persona"] == "rebound"
    bare = tmp_path / "bare_install"
    bare.mkdir()
    other = tmp_path / "restore_parent" / "nopersona"
    _plan, receipt = restore(v2, other, bare, selection={"preferences": "recover"})
    assert "last_persona" not in json.loads((other / "memory/prefs.json").read_text())
    assert receipt["rebind"]["preferences"]["last_persona"] == "dropped_not_in_destination"


def test_official_skill_trust_comes_only_from_the_destination_install(v2, tmp_path):
    base = tmp_path / "bare_install"
    (base / "skills").mkdir(parents=True)
    (tmp_path / "p").mkdir()
    data_dir = tmp_path / "p" / "data"
    _plan, receipt = restore(v2, data_dir, base)
    origins = dict(rows(data_dir / "memory/lumina.db", "SELECT name, origin FROM skills"))
    assert origins["Official Thing"] == "user"      # archive-written file vouches for nothing
    assert receipt["rebind"]["primary_database"]["official_skills_demoted"] == 1
    paths = dict(rows(data_dir / "memory/lumina.db", "SELECT name, path FROM skills"))
    assert paths == {"Duck": str(base / "skills/rubber-duck-debugging.md"),
                     "Official Thing": str(base / "skills/official-thing.md")}


def test_tampered_official_bytes_in_the_destination_do_not_vouch(v2, target):
    data_dir, base = target
    (base / "skills/official-thing.md").write_bytes(b"something else")
    plan = ar.plan_restore(v2, data_dir, base, selection={"primary_database": "recover"})
    ar.restore_isolated(plan, v2, approved_plan_digest=plan["plan_digest"])
    assert dict(rows(data_dir / "memory/lumina.db", "SELECT name, origin FROM skills"))["Official Thing"] == "user"


def _schema(db):
    return {name: " ".join(sql.split()) for name, sql in rows(
        db, "SELECT name, sql FROM sqlite_master WHERE type IN ('trigger', 'view')")}


def test_archived_triggers_and_views_cannot_survive_or_repromote_a_skill(source, target, tmp_path, monkeypatch):
    # Goblin review G1: an ordinary archived trigger ran inside the authority
    # scrub and re-promoted an unvouched Skill into the official prompt block.
    data, base = source
    (base / "skills/rubber-duck-debugging.md").write_text("ARCHIVE_ONLY_SKILL_SENTINEL")
    payload = (base / "skills/rubber-duck-debugging.md").read_bytes()
    with contextlib.closing(sqlite3.connect(data / "memory/lumina.db")) as conn, conn:
        conn.execute("UPDATE skills SET origin='official', content_sha256=? WHERE name='Duck'",
                     (hashlib.sha256(payload).hexdigest(),))
        conn.execute("CREATE TRIGGER restore_repromote AFTER UPDATE OF origin ON skills WHEN NEW.name='Duck' "
                     "BEGIN UPDATE skills SET origin='official' WHERE id=NEW.id; END")
        # Same name as a shipped trigger, other body: CREATE ... IF NOT EXISTS
        # at startup would never replace it.
        conn.execute("DROP TRIGGER skills_au")
        conn.execute("CREATE TRIGGER skills_au AFTER UPDATE ON skills BEGIN "
                     "UPDATE skills SET origin='official'; END")
        conn.execute("CREATE VIEW archived_view AS SELECT name FROM skills")
    archived = _schema(data / "memory/lumina.db")
    shipped = {n for n, sql in archived.items() if ar._SHIPPED_TRIGGERS.get(n) == sql}
    assert {"skills_ai", "skills_ad"} <= shipped and "skills_au" not in shipped
    archive = _build_v2(source, tmp_path / "trigger.zip")
    data_dir, install = target
    _plan, receipt = restore(archive, data_dir, install)
    db = data_dir / "memory/lumina.db"
    assert dict(rows(db, "SELECT name, origin FROM skills"))["Duck"] == "user"
    assert set(_schema(db)) == shipped                          # shipped FTS sync kept, nothing else
    assert receipt["rebind"]["primary_database"]["untrusted_schema_dropped"] == len(archived) - len(shipped)
    checks = ar.verify_restored_root(data_dir, receipt)["checks"]
    assert checks["sqlite_schema_vetted"] and checks["skill_authority_fenced"]
    from core import skills
    monkeypatch.setattr(config, "DB_PATH", str(db))
    official_block, user_block = skills.build_skills_block("debugging")
    assert "ARCHIVE_ONLY_SKILL_SENTINEL" not in official_block


def test_shipped_trigger_allowlist_matches_the_runtime_schema(tmp_path, monkeypatch):
    from core import chat_history, skills
    db = tmp_path / "fresh.db"
    monkeypatch.setattr(config, "DB_PATH", str(db))
    with contextlib.closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("CREATE TABLE chat_messages (id INTEGER PRIMARY KEY, chat_id INTEGER, role TEXT, "
                     "content TEXT, created_at TEXT)")
    skills.init_skills_db(str(db))
    chat_history.init_chat_history_fts()
    assert _schema(db) == ar._SHIPPED_TRIGGERS
    fts = {n: ar._norm_sql(sql) for n, sql in rows(db, "SELECT name, sql FROM sqlite_master WHERE name LIKE '%_fts'")}
    assert fts == ar._SHIPPED_FTS


def test_acceptance_rederives_authority_from_the_published_bytes(v2, target):
    data_dir, base = target
    _plan, receipt = restore(v2, data_dir, base)
    db = data_dir / "memory/lumina.db"
    with contextlib.closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("UPDATE skills SET origin='official' WHERE name='Duck'")
    checks = ar.verify_restored_root(data_dir, receipt)["checks"]
    assert checks["skill_authority_fenced"] is False and checks["sqlite_schema_vetted"] is True
    with contextlib.closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("UPDATE skills SET origin='user' WHERE name='Duck'")
        conn.execute("CREATE TRIGGER planted AFTER INSERT ON memories BEGIN SELECT 1; END")
    checks = ar.verify_restored_root(data_dir, receipt)["checks"]
    assert checks["sqlite_schema_vetted"] is False and checks["skill_authority_fenced"] is True
    with contextlib.closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("DROP TRIGGER planted")
        conn.execute("ALTER TABLE skills RENAME COLUMN content_sha256 TO Content_Sha256")
    checks = ar.verify_restored_root(data_dir, receipt)["checks"]
    assert checks["sqlite_schema_vetted"] is False and checks["skill_authority_fenced"] is False


def test_an_official_row_the_fence_misses_fails_closed(v2, target, monkeypatch):
    # The post-check measures with its own comparison, not the fence's.
    data_dir, base = target
    plan = ar.plan_restore(v2, data_dir, base)
    monkeypatch.setattr(ar, "_claims_official", lambda origin: False)
    with pytest.raises(ar.RestoreRefused) as exc:
        ar.restore_isolated(plan, v2, approved_plan_digest=plan["plan_digest"])
    assert exc.value.code == "SKILL_AUTHORITY_UNVERIFIED" and not data_dir.exists()


# --------------------------------------------------------------------------
# Labels of incomplete, legacy and selective inputs
# --------------------------------------------------------------------------

def test_legacy_archive_restores_only_as_limited_legacy_recovery(legacy, source, target):
    data_dir, base = target
    before = files_of(base)
    archived = dict(entries(legacy))["memory/lumina.db"]
    assert archived[18:20] == b"\x02\x02"     # a genuine live database is WAL-mode
    _plan, receipt = restore(legacy, data_dir, base)
    choices = {u["unit"]: u["choice"] for u in receipt["units"]}
    assert choices["primary_database"] == "recover" and choices["flight_recorder"] == "recover"
    restored = data_dir / "memory/lumina.db"
    assert restored.read_bytes()[18:20] == b"\x01\x01"
    for sql in ("SELECT id, name FROM chats ORDER BY id", "SELECT content FROM chat_messages ORDER BY id",
                "SELECT content FROM palace_drawers ORDER BY id"):
        assert rows(restored, sql) == rows(source[0] / "memory/lumina.db", sql)
    assert (data_dir / "projects/demo/chats.json").exists()
    assert receipt["restore_class"] == ar.LEGACY
    assert receipt["coverage"]["format_coverage"] == "legacy_data_folder_only"
    assert receipt["coverage"]["provenance"] == "unknown"
    assert {u["unit"] for u in receipt["units_not_in_archive"]} >= {"skills", "personas", "project_list"}
    assert all(u["disposition"] != "recover" or u["availability"] != "unavailable" or u["choice"] != "recover"
               for u in receipt["units"])
    assert receipt["install_files"] == [] and files_of(base) == before
    assert ar.verify_restored_root(data_dir, receipt)["passed"] is True


def test_damaged_archive_is_never_a_full_restore_only_owner_selected_units(v2, target):
    data_dir, base = target
    repack(v2, [(n, b'{"name": "TAMPERED"}' if n == "overlay/personas/lumina.json" else d)
                for n, d in entries(v2)])
    with pytest.raises(ar.RestoreRefused) as exc:
        ar.plan_restore(v2, data_dir, base)
    assert exc.value.code == "SELECTION_REQUIRED"
    with pytest.raises(ar.RestoreRefused) as exc:
        ar.plan_restore(v2, data_dir, base, selection={"personas": "recover"})
    assert exc.value.code == "SELECTION_INVALID"
    _plan, receipt = restore(v2, data_dir, base, selection={"primary_database": "recover", "skills": "recover"})
    assert receipt["restore_class"] == ar.SELECTIVE
    choices = {u["unit"]: u["choice"] for u in receipt["units"]}
    assert choices["primary_database"] == "recover" and choices["preferences"] == "skip"
    assert choices["personas"] == "not_recoverable"
    assert not (base / "personas/lumina.json").exists()
    assert ar.verify_restored_root(data_dir, receipt)["passed"] is True


# --------------------------------------------------------------------------
# Malformed and hostile archives fail closed, writing nothing
# --------------------------------------------------------------------------

def _hostile(kind, archive):
    if kind == "traversal":
        repack(archive, entries(archive) + [("../escape.txt", b"x")], canonical=False)
    elif kind == "duplicate":
        items = entries(archive)
        with pytest.warns(UserWarning):
            repack(archive, items + [items[1]])
    elif kind == "case_collision":
        repack(archive, entries(archive) + [("STATE/databases/lumina.db", b"x")])
    elif kind == "malformed_manifest":
        repack(archive, [(n, b"{not json" if n == "manifest.json" else d) for n, d in entries(archive)])
    elif kind == "newer_format":
        with_manifest(archive, lambda m: m.update(format_version={"major": 99, "minor": 0}))
    elif kind == "not_a_zip":
        archive.write_bytes(b"PK definitely not a zip")
    elif kind == "symlink_entry":
        items = entries(archive)
        with zipfile.ZipFile(archive, "w") as z:
            for n, d in items:
                z.writestr(ab._make_v1_zipinfo(n), d)
            info = zipfile.ZipInfo("overlay/skills/link.md")
            info.external_attr = 0o120777 << 16
            z.writestr(info, "/etc/passwd")


@pytest.mark.parametrize("kind", ["traversal", "duplicate", "case_collision", "malformed_manifest",
                                  "newer_format", "not_a_zip", "symlink_entry"])
def test_hostile_archives_are_rejected_before_any_write(kind, v2, target):
    data_dir, base = target
    before = files_of(base)
    _hostile(kind, v2)
    with pytest.raises(ar.RestoreRefused) as exc:
        ar.plan_restore(v2, data_dir, base)
    assert exc.value.code == "ARCHIVE_REJECTED"
    assert not data_dir.exists() and leftovers(data_dir) == [] and files_of(base) == before


def test_unsupported_required_feature_blocks_restore(v2, target):
    data_dir, base = target
    with_manifest(v2, lambda m: m.update(required_features=["future.feature"]))
    intake = ri.intake_archive(v2)
    if intake["kind"] == ri.REJECTED:
        pytest.skip("export verifier rejects unknown required features outright")
    with pytest.raises(ar.RestoreRefused) as exc:
        ar.plan_restore(v2, data_dir, base)
    assert exc.value.code == "UNSUPPORTED_REQUIRED_FEATURES"


# --------------------------------------------------------------------------
# Consent binding, conflicts, repeats
# --------------------------------------------------------------------------

def test_restore_requires_the_exact_approved_plan(v2, target):
    data_dir, base = target
    plan = ar.plan_restore(v2, data_dir, base)
    for digest in (None, "0" * 64):
        with pytest.raises(ar.RestoreRefused) as exc:
            ar.restore_isolated(plan, v2, approved_plan_digest=digest)
        assert exc.value.code == "PLAN_NOT_APPROVED"
    forged = dict(plan, selection={**plan["selection"], "custom_tools": "skip"})
    with pytest.raises(ar.RestoreRefused) as exc:
        ar.restore_isolated(forged, v2, approved_plan_digest=plan["plan_digest"])
    assert exc.value.code == "PLAN_NOT_APPROVED"
    assert not data_dir.exists()


@pytest.mark.parametrize("change", ["archive", "install", "destination"])
def test_anything_changed_since_approval_refuses(change, v2, source, target, tmp_path):
    data_dir, base = target
    plan = ar.plan_restore(v2, data_dir, base)
    if change == "archive":
        (source[0] / "memory/prefs.json").write_text(json.dumps({"human_bio": "changed"}))
        v2.unlink()
        _build_v2(source, v2)
    elif change == "install":
        (base / "personas/lumina.json").write_text("{}")
    else:
        data_dir.mkdir()
    with pytest.raises(ar.RestoreRefused) as exc:
        ar.restore_isolated(plan, v2, approved_plan_digest=plan["plan_digest"])
    assert exc.value.code in ("PLAN_CHANGED", "DESTINATION_NOT_EMPTY")
    assert not (data_dir / "memory").exists() and leftovers(data_dir) == []


def test_install_conflicts_need_an_owner_choice_and_are_disclosed(v2, target):
    data_dir, base = target
    (base / "personas/lumina.json").write_text('{"name": "Destination"}')
    plan = ar.plan_restore(v2, data_dir, base)
    assert plan["status"] == "needs_owner_conflict_choice"
    with pytest.raises(ar.RestoreRefused) as exc:
        ar.restore_isolated(plan, v2, approved_plan_digest=plan["plan_digest"])
    assert exc.value.code == "OWNER_CONFLICT_CHOICE_REQUIRED" and not data_dir.exists()
    selection = {**plan["selection"], "personas": "recover_keep_destination"}
    _plan, receipt = restore(v2, data_dir, base, selection=selection)
    assert (base / "personas/lumina.json").read_text() == '{"name": "Destination"}'
    personas = next(u for u in receipt["units"] if u["unit"] == "personas")
    assert personas["conflicts"] == ["personas/lumina.json"]
    assert ar.verify_restored_root(data_dir, receipt)["passed"] is True


def test_repeating_a_completed_restore_never_overwrites(v2, target):
    data_dir, base = target
    _plan, receipt = restore(v2, data_dir, base)
    before = files_of(data_dir)
    for _ in range(2):
        with pytest.raises(ar.RestoreRefused) as exc:
            restore(v2, data_dir, base)
        assert exc.value.code == "DESTINATION_NOT_EMPTY"
    assert files_of(data_dir) == before


def test_an_empty_existing_directory_is_an_acceptable_destination(v2, target):
    data_dir, base = target
    data_dir.mkdir()
    _plan, receipt = restore(v2, data_dir, base)
    assert ar.verify_restored_root(data_dir, receipt)["passed"] is True


# --------------------------------------------------------------------------
# Failure, rollback and interruption
# --------------------------------------------------------------------------

def test_insufficient_space_is_refused_before_writing(v2, target, monkeypatch):
    data_dir, base = target
    before = files_of(base)
    plan = ar.plan_restore(v2, data_dir, base)
    monkeypatch.setattr(ar, "_free_bytes", lambda _p: plan["required_bytes"] - 1)
    with pytest.raises(ar.RestoreRefused) as exc:
        ar.restore_isolated(plan, v2, approved_plan_digest=plan["plan_digest"])
    assert exc.value.code == "INSUFFICIENT_SPACE"
    assert not data_dir.exists() and leftovers(data_dir) == [] and files_of(base) == before


@pytest.mark.parametrize("fail_at", [1, 5, 30])
def test_disk_full_mid_restore_rolls_back_completely(fail_at, v2, target, monkeypatch):
    data_dir, base = target
    before = files_of(base)
    plan = ar.plan_restore(v2, data_dir, base)
    real, calls = ar._write_new, []

    def flaky(path, data, mode):
        calls.append(path)
        if len(calls) == fail_at:
            raise OSError(errno.ENOSPC, "No space left on device", path)
        return real(path, data, mode)

    monkeypatch.setattr(ar, "_write_new", flaky)
    with pytest.raises(ar.RestoreRefused) as exc:
        ar.restore_isolated(plan, v2, approved_plan_digest=plan["plan_digest"])
    assert exc.value.code == "WRITE_FAILED"
    assert exc.value.rollback == {"staging_removed": True, "install_files_left": [],
                                  "install_paths_preserved": [], "destination_untouched": True}
    assert not data_dir.exists() and leftovers(data_dir) == [] and files_of(base) == before


def test_destination_appearing_at_publish_rolls_back(v2, target, monkeypatch):
    data_dir, base = target
    before = files_of(base)
    plan = ar.plan_restore(v2, data_dir, base)

    def squat(stage):
        if stage == "before_publish":
            data_dir.mkdir()
            (data_dir / "someone_elses.txt").write_text("keep me")

    monkeypatch.setattr(ar, "_checkpoint", squat)
    with pytest.raises(ar.RestoreRefused) as exc:
        ar.restore_isolated(plan, v2, approved_plan_digest=plan["plan_digest"])
    assert exc.value.code == "DESTINATION_CHANGED"
    assert files_of(data_dir) == {"someone_elses.txt": b"keep me"}
    assert leftovers(data_dir) == [] and files_of(base) == before


def _killed_restore(archive, data_dir, base, stage, tmp_path):
    script = tmp_path / f"kill_{stage}.py"
    script.write_text(textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {str(REPO)!r})
        import core.agent_backup_restore as ar
        def die(stage):
            if stage == {stage!r}:
                os._exit(77)
        ar._checkpoint = die
        plan = ar.plan_restore({str(archive)!r}, {str(data_dir)!r}, {str(base)!r})
        ar.restore_isolated(plan, {str(archive)!r}, approved_plan_digest=plan["plan_digest"])
        os._exit(0)
    """))
    run = subprocess.run([sys.executable, str(script)], cwd=REPO, capture_output=True, text=True, timeout=120)
    assert run.returncode == 77, run.stderr[-2000:]


@pytest.mark.parametrize("stage", ["members_verified", "data_staged", "staged_validated",
                                   "install_file_created", "before_publish"])
def test_a_killed_restore_is_recoverable_and_never_half_published(stage, v2, target, tmp_path):
    data_dir, base = target
    before = files_of(base)
    _killed_restore(v2, data_dir, base, stage, tmp_path)
    assert not data_dir.exists()
    assert leftovers(data_dir)
    with pytest.raises(ar.RestoreRefused) as exc:
        ar.plan_restore(v2, data_dir, base)
    assert exc.value.code == "INTERRUPTED_RESTORE_PENDING"
    report = ar.recover_interrupted_restore(data_dir)
    assert len(report["rolled_back"]) == 1 and report["left_alone"] == [] and report["install_files_left"] == []
    assert report["install_paths_preserved"] == []
    assert leftovers(data_dir) == [] and files_of(base) == before
    _plan, receipt = restore(v2, data_dir, base)
    assert ar.verify_restored_root(data_dir, receipt)["passed"] is True


def test_a_restore_killed_after_publishing_is_complete(v2, target, tmp_path):
    data_dir, base = target
    _killed_restore(v2, data_dir, base, "published", tmp_path)
    assert leftovers(data_dir) == []
    restore_id = next((data_dir / "restore_history").iterdir()).name
    receipt = json.loads((data_dir / "restore_history" / restore_id / "receipt.json").read_text())
    assert ar.verify_restored_root(data_dir, receipt)["passed"] is True
    assert ar.recover_interrupted_restore(data_dir) == {
        "rolled_back": [], "cleared_never_journaled": [], "left_alone": [], "quarantined": [],
        "install_files_left": [], "install_paths_preserved": []}


def test_foreign_staging_lookalikes_are_left_alone(target):
    data_dir, _base = target
    stray = data_dir.parent / f".{data_dir.name}.restore-{'a' * 32}.staging"
    stray.mkdir()
    (stray / "not_ours.txt").write_text("x")
    report = ar.recover_interrupted_restore(data_dir)
    assert report["left_alone"] == [str(stray)] and (stray / "not_ours.txt").exists()


def _journal_path(data_dir):
    (staging,) = [p for p in Path(data_dir).parent.iterdir() if p.name.endswith(".staging")]
    return staging, staging / "restore_history" / staging.name.rsplit(".restore-", 1)[1][:-8] / "journal.json"


@pytest.mark.parametrize("tamper", ["outside_install", "dotdot", "temp_name", "install_identity",
                                    "install_dir_moved", "state", "extra_field", "staging_with_entries"])
def test_recovery_acts_only_on_a_journal_this_engine_could_have_written(tamper, v2, target, tmp_path):
    # Goblin review G2: a syntactically valid lookalike journal named a file
    # outside the install folder, and recovery deleted it.
    data_dir, base = target
    _killed_restore(v2, data_dir, base, "install_file_created", tmp_path)
    staging, path = _journal_path(data_dir)
    journal = json.loads(path.read_text())
    foreign = tmp_path / "unrelated-owner-file.txt"
    foreign.write_bytes(b"KEEP FOREIGN OWNER DATA")
    st = foreign.stat()
    entry = {"path": str(foreign), "tmp": ar._install_tmp(str(foreign), journal["restore_id"]),
             "sha256": hashlib.sha256(foreign.read_bytes()).hexdigest(), "identity": [st.st_dev, st.st_ino]}
    created = journal["install_created"][0]
    if tamper == "outside_install":
        journal["install_created"].append(entry)
    elif tamper == "dotdot":
        created["path"] = str(base) + "/skills/../../" + foreign.name
    elif tamper == "temp_name":
        created["tmp"] = str(foreign)
    elif tamper == "install_identity":
        journal["install_identity"] = [0, 0]
    elif tamper == "install_dir_moved":
        journal["install_dir"] = str(tmp_path)
    elif tamper == "state":
        journal["state"] = "published"
    elif tamper == "extra_field":
        created["delete_also"] = str(foreign)
    else:
        journal["state"] = "staging"
    path.write_text(json.dumps(journal))
    installed = files_of(base)
    report = ar.recover_interrupted_restore(data_dir)
    assert report["left_alone"] == [str(staging)] and report["rolled_back"] == []
    assert foreign.read_bytes() == b"KEEP FOREIGN OWNER DATA"
    assert staging.is_dir() and json.loads(path.read_text()) == journal     # evidence preserved
    assert files_of(base) == installed


def test_a_concurrently_created_install_file_is_never_rolled_back(v2, target, monkeypatch):
    # Goblin review G3: after a genuine EEXIST the rollback deleted the other
    # writer's file because its bytes matched.
    data_dir, base = target
    plan = ar.plan_restore(v2, data_dir, base)
    real, planted = os.link, []

    def racing_link(src, dst, **kw):
        Path(dst).write_bytes(Path(src).read_bytes())
        planted.append(Path(dst))
        return real(src, dst, **kw)

    monkeypatch.setattr(os, "link", racing_link)
    with pytest.raises(ar.RestoreRefused) as exc:
        ar.restore_isolated(plan, v2, approved_plan_digest=plan["plan_digest"])
    assert exc.value.code == "INSTALL_TARGET_CHANGED"
    assert planted[0].exists()
    assert exc.value.rollback["install_paths_preserved"] == [str(planted[0])]
    assert exc.value.rollback["install_files_left"] == [] and leftovers(data_dir) == []


def test_a_temp_created_before_its_identity_was_journaled_is_preserved(v2, target, tmp_path):
    data_dir, base = target
    before = files_of(base)
    _killed_restore(v2, data_dir, base, "install_tmp_created", tmp_path)
    report = ar.recover_interrupted_restore(data_dir)
    (tmp,) = report["install_paths_preserved"]
    assert ".restore-tmp" in tmp and Path(tmp).exists() and report["rolled_back"]
    regular = lambda tree: {k: v for k, v in tree.items() if v is not None and not k.endswith(".restore-tmp")}
    assert regular(files_of(base)) == regular(before)


def test_an_official_row_without_a_recorded_hash_is_never_vouched(source, target, tmp_path):
    data, _base = source
    with contextlib.closing(sqlite3.connect(data / "memory/lumina.db")) as conn, conn:
        conn.execute("UPDATE skills SET origin='official', content_sha256=NULL WHERE name='Duck'")
    archive = _build_v2(source, tmp_path / "nullhash.zip")
    data_dir, base = target
    _plan, receipt = restore(archive, data_dir, base)
    assert dict(rows(data_dir / "memory/lumina.db", "SELECT name, origin FROM skills"))["Duck"] == "user"
    assert ar.verify_restored_root(data_dir, receipt)["passed"] is True


# --------------------------------------------------------------------------
# SQLite integrity, source untouched
# --------------------------------------------------------------------------

def test_sqlite_integrity_holds_and_the_source_is_untouched(v2, source, target):
    src_data, src_base = source
    src_before, archive_before = files_of(src_data.parent), v2.read_bytes()
    data_dir, base = target
    _plan, receipt = restore(v2, data_dir, base)
    assert receipt["sqlite"] == {"memory/lumina.db": "ok", "telemetry/flight_recorder.db": "ok"}
    # Compared before this test opens the WAL-mode source (even a read-only
    # open of it creates a -shm sidecar).
    assert files_of(src_data.parent) == src_before and v2.read_bytes() == archive_before
    for db in (src_data / "memory/lumina.db", data_dir / "memory/lumina.db",
               data_dir / "telemetry/flight_recorder.db"):
        assert rows(db, "PRAGMA integrity_check") == [("ok",)]
    for side in ("-wal", "-shm", "-journal"):
        assert not (data_dir / f"memory/lumina.db{side}").exists()


def test_a_corrupted_staged_database_fails_closed(v2, target, monkeypatch):
    data_dir, base = target
    plan = ar.plan_restore(v2, data_dir, base)
    monkeypatch.setattr(ar, "_integrity", lambda _p: False)
    with pytest.raises(ar.RestoreRefused) as exc:
        ar.restore_isolated(plan, v2, approved_plan_digest=plan["plan_digest"])
    assert exc.value.code == "SQLITE_INVALID" and not data_dir.exists() and leftovers(data_dir) == []


def test_staged_validation_catches_an_unexpected_live_authority_file(v2, target, monkeypatch):
    data_dir, base = target
    plan = ar.plan_restore(v2, data_dir, base)

    def plant(stage):
        if stage == "data_staged":
            staging = next(p for p in data_dir.parent.iterdir() if p.name.endswith(".staging"))
            (staging / "memory/pending_actions.json").write_text("[]")

    monkeypatch.setattr(ar, "_checkpoint", plant)
    with pytest.raises(ar.RestoreRefused) as exc:
        ar.restore_isolated(plan, v2, approved_plan_digest=plan["plan_digest"])
    assert exc.value.code == "STAGED_VALIDATION_FAILED" and not data_dir.exists()


# --------------------------------------------------------------------------
# Production isolation (never touches a production root to prove it)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("where", ["owner_dev", "owner_release", "config", "process_data_dir",
                                   "inside_install", "install_is_running_checkout"])
def test_production_and_live_roots_are_refused_before_any_access(where, v2, target, monkeypatch):
    data_dir, base = target
    home = os.path.expanduser("~")
    if where == "owner_dev":
        data_dir = Path(home, ".local/share/lumina/restore-test")
    elif where == "owner_release":
        data_dir = Path(home, ".local/share/lumina-release")
    elif where == "config":
        data_dir = Path(home, ".config/lumina/x")
    elif where == "process_data_dir":
        data_dir = Path(os.environ["LUMINA_DATA_DIR"], "restored")
    elif where == "inside_install":
        data_dir = REPO / "restored_data"
    else:
        base = REPO
    monkeypatch.setattr(ar.ri, "intake_archive", lambda *_a: pytest.fail("intake ran"))
    monkeypatch.setattr(ar.os, "mkdir", lambda *_a, **_k: pytest.fail("mkdir ran"))
    with pytest.raises(ar.RestoreRefused) as exc:
        ar.plan_restore(v2, data_dir, base)
    assert exc.value.code == "PRODUCTION_ROOT"


def test_the_engine_never_reads_the_process_configuration():
    tree = ast.parse((REPO / "core/agent_backup_restore.py").read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    imports = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imports |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert "config" not in names and "config" not in imports and "DATA_DIR" not in names
    assert "core.suspension" not in imports   # C1 only through the backup interface


# --------------------------------------------------------------------------
# C1 restore trace
# --------------------------------------------------------------------------

def test_restore_trace_is_write_new_only_and_routes_to_recover(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    rel = c1.write_restore_trace(root, restore_id="a" * 32, archive_sha256="b" * 64,
                                 restore_class="incomplete_state_export")
    assert S.probe(root).status is S.StoreStatus.ABSENT_WITH_TRACE
    assert c1.restore_trace_state(root) == "absent_with_trace"
    assert not (root / "memory" / S.STORE_NAME).exists() and not (root / "memory" / S.IDENTITY_NAME).exists()
    assert oct((root / rel).stat().st_mode & 0o777) == "0o600"
    with pytest.raises(c1.RestoreTraceRefused):
        c1.write_restore_trace(root, restore_id="c" * 32, archive_sha256="b" * 64, restore_class="x")


@pytest.mark.parametrize("existing", [S.STORE_NAME, S.IDENTITY_NAME, S.STORE_NAME + "-wal"])
def test_restore_trace_never_touches_existing_c1_state(existing, tmp_path):
    root = tmp_path / "root"
    (root / "memory").mkdir(parents=True, mode=0o700)
    (root / "memory" / existing).write_bytes(b"live")
    with pytest.raises(c1.RestoreTraceRefused):
        c1.write_restore_trace(root, restore_id="a" * 32, archive_sha256="b" * 64, restore_class="x")
    assert sorted(p.name for p in (root / "memory").iterdir()) == [existing]


@pytest.mark.parametrize("field", [{"restore_id": "../x"}, {"archive_sha256": "nothex"},
                                   {"restore_class": "Bad Class"}])
def test_restore_trace_fields_are_strict(field, tmp_path):
    kw = {"restore_id": "a" * 32, "archive_sha256": "b" * 64, "restore_class": "x", **field}
    with pytest.raises(ValueError):
        c1.write_restore_trace(tmp_path, **kw)
    assert not (tmp_path / "memory").exists()


def test_a_damaged_legacy_unit_needs_an_owner_choice_never_a_silent_drop(legacy, target):
    data_dir, base = target
    items = entries(legacy)
    repack(legacy, items + [("memory/lumina.db-wal", b"uncheckpointed frames")], canonical=False)
    intake = ri.intake_archive(legacy)
    assert intake["kind"] == ri.LEGACY
    with pytest.raises(ar.RestoreRefused) as exc:
        ar.plan_restore(legacy, data_dir, base)
    assert exc.value.code == "SELECTION_REQUIRED" and "primary_database" in exc.value.detail
    _plan, receipt = restore(legacy, data_dir, base, selection={"preferences": "recover"})
    assert not (data_dir / "memory/lumina.db").exists()
    assert {u["unit"]: u["choice"] for u in receipt["units"]}["primary_database"] == "not_recoverable"


# --------------------------------------------------------------------------
# R3 -- Sonnet 5.5 review of R2: skills shape, Skill authority, journal reach,
# transaction-bound ownership, owner exits from interrupted recovery
# --------------------------------------------------------------------------

EVIL = b"# Skill: Evil\nHOSTILE_AUTHORITY_SENTINEL\n"
_SKILLS_DDL = ("CREATE TABLE skills (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, "
               "description TEXT NOT NULL, path TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
               "content_sha256 TEXT, origin TEXT NOT NULL DEFAULT 'user')")


# The skills table exactly as released core.skills versions created and
# migrated it (verbatim from git: 6c0b78d's CREATE, HEAD's CREATE, the ALTERs).
_OLD_CREATE = ("\n        CREATE TABLE IF NOT EXISTS skills (\n            id          INTEGER PRIMARY KEY AUTOINCREMENT,\n"
               "            name        TEXT NOT NULL UNIQUE,\n            description TEXT NOT NULL,\n"
               "            path        TEXT NOT NULL,\n            created_at  TEXT NOT NULL,\n"
               "            updated_at  TEXT NOT NULL\n        )\n    ")
_NEW_CREATE = _OLD_CREATE.replace("updated_at  TEXT NOT NULL\n", "updated_at  TEXT NOT NULL,\n            content_sha256 TEXT\n")
_ADD_ORIGIN = "ALTER TABLE skills ADD COLUMN origin TEXT NOT NULL DEFAULT 'user'"
_ADD_HASH = "ALTER TABLE skills ADD COLUMN content_sha256 TEXT"
_HISTORICAL_SKILLS = {
    "six_columns": [_OLD_CREATE],
    "origin_added": [_OLD_CREATE, _ADD_ORIGIN],
    "origin_then_hash_added": [_OLD_CREATE, _ADD_ORIGIN, _ADD_HASH],
    "fresh_current": [_NEW_CREATE, _ADD_ORIGIN],
}


def _source_sql(source, *statements, rebuild=None):
    """Mutate the source database; rebuild= swaps in another skills table
    (one CREATE, or a list of statements run in order)."""
    data, _base = source
    with contextlib.closing(sqlite3.connect(data / "memory/lumina.db")) as conn, conn:
        if rebuild:
            old = conn.execute("SELECT id, name, description, path, created_at, updated_at, content_sha256, origin "
                               "FROM skills").fetchall()
            conn.execute("DROP TABLE skills")
            for sql in [rebuild] if isinstance(rebuild, str) else rebuild:
                conn.execute(sql)
            cols = [r[1] for r in conn.execute("PRAGMA table_info(skills)")]
            names = ["id", "name", "description", "path", "created_at", "updated_at", "content_sha256", "origin"]
            keep = [i for i, n in enumerate(names) if n in cols]
            for row in old:
                conn.execute(f"INSERT INTO skills ({', '.join(names[i] for i in keep)}) "
                             f"VALUES ({', '.join('?' * len(keep))})", [row[i] for i in keep])
        for sql in statements:
            conn.execute(sql)


def _official_block(db, query, monkeypatch):
    from core import skills
    monkeypatch.setattr(config, "DB_PATH", str(db))
    return skills.build_skills_block(query)[0]


@pytest.mark.parametrize("shape", [
    "hash_column_case", "origin_column_case", "name_column_case", "generated_origin_virtual",
    "generated_origin_stored", "extra_column", "origin_default_official", "id_not_primary_key",
    "name_not_unique", "hash_column_blob", "required_column_missing"])
def test_a_skills_table_outside_the_shipped_shape_is_refused_before_any_authority_decision(
        shape, source, target, tmp_path):
    # Sonnet R2 review G1: a case-variant or generated column hid 'official'
    # from the scrub while the runtime still read it.
    data, base = source
    (base / "skills/evil.md").write_bytes(EVIL)
    evil = ("INSERT INTO skills (name, description, path, created_at, updated_at, content_sha256) "
            f"VALUES ('Evil', 'evilword', '/x/skills/evil.md', 't', 't', '{hashlib.sha256(EVIL).hexdigest()}')")
    ddl = {
        "generated_origin_virtual": _SKILLS_DDL.replace("origin TEXT NOT NULL DEFAULT 'user'",
                                                        "origin TEXT GENERATED ALWAYS AS ('official') VIRTUAL"),
        "generated_origin_stored": _SKILLS_DDL.replace("origin TEXT NOT NULL DEFAULT 'user'",
                                                       "origin TEXT GENERATED ALWAYS AS ('official') STORED"),
        "origin_default_official": _SKILLS_DDL.replace("DEFAULT 'user'", "DEFAULT 'official'"),
        "id_not_primary_key": _SKILLS_DDL.replace("id INTEGER PRIMARY KEY AUTOINCREMENT", "id INTEGER"),
        "name_not_unique": _SKILLS_DDL.replace("name TEXT NOT NULL UNIQUE", "name TEXT NOT NULL"),
        "hash_column_blob": _SKILLS_DDL.replace("content_sha256 TEXT", "content_sha256 BLOB"),
    }.get(shape)
    statements = {
        "hash_column_case": ["ALTER TABLE skills RENAME COLUMN content_sha256 TO Content_Sha256"],
        "origin_column_case": ["ALTER TABLE skills RENAME COLUMN origin TO Origin"],
        "name_column_case": ["ALTER TABLE skills RENAME COLUMN name TO NAME"],
        "extra_column": ["ALTER TABLE skills ADD COLUMN trust TEXT"],
        "required_column_missing": ["ALTER TABLE skills DROP COLUMN created_at"],
    }.get(shape, [])
    _source_sql(source, *statements, rebuild=ddl)
    if shape.startswith("generated") or shape == "origin_default_official":
        _source_sql(source, evil)
    if shape in ("name_column_case", "required_column_missing"):
        # The shipped exporter already refuses these; hold the engine gate to
        # the same shape directly so it never relies on that earlier layer.
        with pytest.raises(ab.AgentBackupError):
            _build_v2(source, tmp_path / f"{shape}.zip")
        with contextlib.closing(sqlite3.connect(data / "memory/lumina.db")) as conn:
            with pytest.raises(ar.RestoreRefused) as exc:
                ar._skills_shape(conn)
        assert exc.value.code == "SQLITE_INVALID" and "unsupported schema" in exc.value.detail
        return
    archive = _build_v2(source, tmp_path / f"{shape}.zip")
    data_dir, install = target
    before = files_of(install)
    with pytest.raises(ar.RestoreRefused) as exc:
        restore(archive, data_dir, install)
    assert exc.value.code == "SQLITE_INVALID" and "unsupported schema" in exc.value.detail
    assert not data_dir.exists() and leftovers(data_dir) == [] and files_of(install) == before


@pytest.mark.parametrize("history", list(_HISTORICAL_SKILLS))
def test_every_released_skills_table_restores_and_bootstraps(history, source, target, tmp_path, monkeypatch):
    from core import skill_transport, skills
    _source_sql(source, rebuild=_HISTORICAL_SKILLS[history])
    data_dir, install = target
    shutil.copytree(REPO / "skill_packages", install / "skill_packages")
    _plan, receipt = restore(_build_v2(source, tmp_path / f"{history}.zip"), data_dir, install)
    assert ar.verify_restored_root(data_dir, receipt)["passed"] is True
    db = data_dir / "memory/lumina.db"
    monkeypatch.setattr(config, "DB_PATH", str(db))
    skills.init_skills_db(str(db))
    skill_transport.ensure_official_skills_bootstrapped(data_dir=str(data_dir), db_path=str(db))
    after = dict(rows(db, "SELECT name, origin FROM skills"))
    assert all(after[n] == "official" for n in _tracked()) and after["Duck"] == "user"


def test_the_pinned_skills_definitions_are_exactly_the_released_ones(tmp_path):
    for history, statements in _HISTORICAL_SKILLS.items():
        db = tmp_path / f"{history}.db"
        with contextlib.closing(sqlite3.connect(db)) as conn, conn:
            for sql in statements:
                conn.execute(sql)
        (sql,), = rows(db, "SELECT sql FROM sqlite_master WHERE name='skills'")
        assert ar._norm_sql(sql) in ar._SHIPPED_SKILLS_TABLE, history
    assert len(ar._SHIPPED_SKILLS_TABLE) == len(_HISTORICAL_SKILLS)


@pytest.mark.parametrize("variant", ["lowercase", "uppercase_value", "padded_value", "null_hash",
                                     "no_hash_column", "no_origin_column"])
def test_no_restored_skill_is_official_whatever_the_archive_claims(variant, source, target, tmp_path, monkeypatch):
    data, base = source
    (base / "skills/evil.md").write_bytes(EVIL)
    value = {"uppercase_value": "OFFICIAL", "padded_value": " Official "}.get(variant, "official")
    sha = "NULL" if variant == "null_hash" else f"'{hashlib.sha256(EVIL).hexdigest()}'"
    _source_sql(source, "INSERT INTO skills (name, description, path, created_at, updated_at, origin, content_sha256) "
                        f"VALUES ('Evil', 'evilword', '/attacker/skills/evil.md', 't', 't', '{value}', {sha})")
    if variant == "no_hash_column":                  # a released table from before content_sha256
        _source_sql(source, rebuild=_HISTORICAL_SKILLS["origin_added"])
    if variant == "no_origin_column":                # a released table from before origin
        _source_sql(source, rebuild=_HISTORICAL_SKILLS["six_columns"])
    archive = _build_v2(source, tmp_path / f"{variant}.zip")
    data_dir, install = target
    _plan, receipt = restore(archive, data_dir, install)
    db = data_dir / "memory/lumina.db"
    if variant != "no_origin_column":
        assert {o for _n, o in rows(db, "SELECT name, origin FROM skills")} == {"user"}
    from core import skills
    monkeypatch.setattr(config, "DB_PATH", str(db))
    skills.init_skills_db(str(db))                 # the startup migration, then the runtime reader
    assert "HOSTILE_AUTHORITY_SENTINEL" not in skills.build_skills_block("evilword")[0]
    assert ar.verify_restored_root(data_dir, receipt)["checks"]["skill_authority_fenced"] is True


def test_a_local_user_skill_file_cannot_vouch_an_archive_official_claim(source, target, tmp_path, monkeypatch):
    # Sonnet R2 review G5: write_skill() fills <install>/skills with user Skills,
    # so a file there with the claimed hash proves nothing.
    data_dir, install = target
    from core import skills
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "local.db"))
    monkeypatch.setattr(config, "BASE_DIR", str(install))
    skills.init_skills_db(str(tmp_path / "local.db"))
    saved = skills.write_skill("injected helper", "injectedword", "INJECTION_SENTINEL")
    body = Path(saved["path"]).read_bytes()
    monkeypatch.undo()
    _source_sql(source, "INSERT INTO skills (name, description, path, created_at, updated_at, origin, content_sha256) "
                        "VALUES ('injected helper', 'injectedword', '/old/skills/injected-helper.md', 't', 't', "
                        f"'official', '{hashlib.sha256(body).hexdigest()}')")
    archive = _build_v2(source, tmp_path / "vouch.zip")
    _plan, receipt = restore(archive, data_dir, install)
    db = data_dir / "memory/lumina.db"
    assert dict(rows(db, "SELECT name, origin FROM skills"))["injected helper"] == "user"
    assert "INJECTION_SENTINEL" not in _official_block(db, "injectedword", monkeypatch)


def test_official_skills_return_only_through_the_destination_bootstrap(source, target, tmp_path):
    from core import skill_transport
    data, _base = source
    skill_transport.bootstrap_official_skills(skills_dir=str(data / "skills/official"),
                                              db_path=str(data / "memory/lumina.db"))
    tracked = sorted(json.loads(m.read_text())["name"] for m in (REPO / "skill_packages/official").glob("*/manifest.json"))
    assert set(tracked) <= {n for n, o in rows(data / "memory/lumina.db", "SELECT name, origin FROM skills")
                            if o == "official"}
    archive = _build_v2(source, tmp_path / "bootstrapped.zip")
    data_dir, install = target
    shutil.copytree(REPO / "skill_packages", install / "skill_packages")
    _plan, receipt = restore(archive, data_dir, install)
    db = data_dir / "memory/lumina.db"
    note = receipt["rebind"]["primary_database"]
    assert note["official_skills_deferred_to_bootstrap"] == tracked
    assert note["official_skills_demoted"] == 1                     # 'Official Thing' is not a shipped package
    restored = dict(rows(db, "SELECT name, origin FROM skills"))
    assert not set(tracked) & set(restored) and set(restored.values()) == {"user"}
    assert ar.verify_restored_root(data_dir, receipt)["passed"] is True
    skill_transport.bootstrap_official_skills(skills_dir=str(data_dir / "skills/official"), db_path=str(db))
    after = dict(rows(db, "SELECT name, origin FROM skills"))
    assert all(after[n] == "official" for n in tracked) and after["Official Thing"] == "user"


def test_a_user_skill_named_like_a_shipped_package_is_disclosed(source, target, tmp_path):
    _source_sql(source, "INSERT INTO skills (name, description, path, created_at, updated_at, origin) "
                        "VALUES ('media-generation', 'squat', '/x/skills/m.md', 't', 't', 'user')")
    archive = _build_v2(source, tmp_path / "squat.zip")
    data_dir, install = target
    shutil.copytree(REPO / "skill_packages", install / "skill_packages")
    _plan, receipt = restore(archive, data_dir, install)
    assert receipt["rebind"]["primary_database"]["user_skills_named_like_tracked_official"] == ["media-generation"]


def test_a_trigger_differing_only_by_unicode_space_is_not_shipped(source, target, tmp_path):
    # Sonnet R2 review P3: str.split() collapsed NBSP, which SQLite reads as part of a name.
    shipped = ar._SHIPPED_TRIGGERS["skills_ai"]
    variant = shipped.replace("(rowid, name,", "(rowid,\xa0name,", 1)
    assert variant != shipped and ar._norm_sql(variant) != shipped
    _source_sql(source, "DROP TRIGGER skills_ai", variant)
    archive = _build_v2(source, tmp_path / "nbsp.zip")
    data_dir, install = target
    _plan, receipt = restore(archive, data_dir, install)
    assert "skills_ai" not in _schema(data_dir / "memory/lumina.db")
    assert receipt["rebind"]["primary_database"]["untrusted_schema_dropped"] == 1


def test_install_namespace_is_exactly_what_placement_can_produce():
    rid = "a" * 32
    for member in ("overlay/skills/x.md", "overlay/personas/p.json", "overlay/avatars/a.png",
                   "overlay/voices/v/w.bin", "overlay/tool_profiles/t.json", "overlay/projects/projectlist.md",
                   "overlay/projects/demo/project.md", "overlay/projects/demo/codebase.md"):
        root, rel, _t = ar._placement(member, rid)
        assert root == "install" and ar._install_namespace(rel)
    for rel in ("src/main.py", "skills", "../skills/x.md", "skills/../x.md", "projects/demo/binding.json",
                "projects/a/b/c.md", "assets/x.png", "config.py", ""):
        assert not ar._install_namespace(rel), rel
    assert ar._install_namespace("assets", directory=True) and ar._install_namespace("projects/demo", directory=True)
    assert not ar._install_namespace("src", directory=True)


def _private_staging(data_dir, rid, install, entries, dirs=(), at=None):
    """A forged staging folder with the private shape this engine creates
    (built in `at` instead of next to data_dir when given)."""
    staging = (at or data_dir.parent) / f".{data_dir.name}.restore-{rid}.staging"
    for folder in (staging, staging / "restore_history", staging / "restore_history" / rid):
        folder.mkdir(mode=0o700)
        folder.chmod(0o700)
    st = install.stat()
    (staging / "restore_history" / rid / "journal.json").write_text(json.dumps({
        "schema": ar.JOURNAL_SCHEMA, "version": ar.VERSION, "restore_id": rid, "state": "install_writing",
        "data_dir": str(data_dir), "install_dir": str(install), "install_identity": [st.st_dev, st.st_ino],
        "plan_digest": "0" * 64, "install_dirs_created": list(dirs), "install_created": entries}))
    return staging


def _forged_entry(path, rid, mark=False):
    st = path.stat()
    if mark:
        os.setxattr(path, ar._OWNER_XATTR, rid.encode())
    return {"path": str(path), "tmp": ar._install_tmp(str(path), rid),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "identity": [st.st_dev, st.st_ino], "seal": [st.st_size, st.st_mtime_ns]}


@pytest.mark.parametrize("where", ["outside_namespace", "in_namespace_unmarked"])
def test_a_forged_private_journal_cannot_delete_what_this_restore_did_not_create(where, target, tmp_path):
    # Sonnet R2 review G2: the journal named its own jurisdiction.
    data_dir, install = target
    rid = "b" * 32
    victim = install / ("src/main.py" if where == "outside_namespace" else "skills/precious.md")
    victim.parent.mkdir(exist_ok=True)
    victim.write_bytes(b"precious")
    staging = _private_staging(data_dir, rid, install, [_forged_entry(victim, rid)])
    report = ar.recover_interrupted_restore(data_dir)
    assert victim.read_bytes() == b"precious"
    if where == "outside_namespace":
        assert report["left_alone"] == [str(staging)] and staging.is_dir()
    else:
        assert report["install_paths_preserved"] == [str(victim)]


@pytest.mark.parametrize("shape", ["symlink", "group_writable"])
def test_a_staging_folder_that_is_not_private_is_never_followed_and_can_be_quarantined(shape, v2, target, tmp_path):
    data_dir, install = target
    rid = "c" * 32
    victim = install / "skills/precious.md"
    victim.write_bytes(b"precious")
    entries = [_forged_entry(victim, rid, mark=True)]
    if shape == "symlink":
        (tmp_path / "elsewhere").mkdir()
        # Otherwise a fully valid journal for this very data_dir: only the link differs.
        real = _private_staging(data_dir, rid, install, entries, at=tmp_path / "elsewhere")
        staging = data_dir.parent / real.name
        staging.symlink_to(real, target_is_directory=True)
    else:
        staging = _private_staging(data_dir, rid, install, entries)
        staging.chmod(0o770)
    report = ar.recover_interrupted_restore(data_dir)
    assert report["left_alone"] == [str(staging)] and victim.exists()
    with pytest.raises(ar.RestoreRefused) as exc:
        ar.plan_restore(v2, data_dir, install)
    assert exc.value.code == "INTERRUPTED_RESTORE_PENDING" and str(staging) in exc.value.detail
    report = ar.recover_interrupted_restore(data_dir, quarantine_unrecognized=True)
    (moved,) = report["quarantined"]
    assert moved["from"] == str(staging) and os.path.lexists(moved["to"]) and victim.exists()
    if shape == "symlink":
        assert Path(moved["to"]).is_symlink() and (real / "restore_history").is_dir()
    assert leftovers(data_dir) == [Path(moved["to"]).name]
    restore(v2, data_dir, install)


def test_a_folder_swapped_for_a_link_mid_rollback_cannot_redirect_the_unlink(v2, target, tmp_path, monkeypatch):
    # Sonnet R2 review G2c: the check and the unlink resolved the path separately.
    data_dir, base = target
    _killed_restore(v2, data_dir, base, "install_file_created", tmp_path)
    _staging, jpath = _journal_path(data_dir)
    target_path = Path(json.loads(jpath.read_text())["install_created"][0]["path"])
    victim_dir = tmp_path / "victim"
    victim_dir.mkdir()
    (victim_dir / target_path.name).write_bytes(b"VICTIM")
    real_mark, swapped = ar._owner_mark, []

    def swap_then_mark(fd):
        if not swapped:
            swapped.append(1)
            target_path.parent.rename(target_path.parent.with_name("orig"))
            target_path.parent.symlink_to(victim_dir, target_is_directory=True)
        return real_mark(fd)

    monkeypatch.setattr(ar, "_owner_mark", swap_then_mark)
    report = ar.recover_interrupted_restore(data_dir)
    assert swapped and (victim_dir / target_path.name).read_bytes() == b"VICTIM"
    assert not (target_path.parent.with_name("orig") / target_path.name).exists()   # ours, removed where it was
    assert report["rolled_back"]


@pytest.mark.parametrize("change", ["mark_removed", "mtime_changed"])
def test_ownership_needs_the_restore_mark_and_the_seal_not_just_inode_and_bytes(change, v2, target, tmp_path):
    # Sonnet R2 review G3: inode reuse plus equal bytes passed as ownership.
    data_dir, base = target
    _killed_restore(v2, data_dir, base, "install_file_created", tmp_path)
    _staging, jpath = _journal_path(data_dir)
    path = Path(json.loads(jpath.read_text())["install_created"][0]["path"])
    if change == "mark_removed":
        os.removexattr(path, ar._OWNER_XATTR)
    else:
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1))
    report = ar.recover_interrupted_restore(data_dir)
    assert path.exists() and report["rolled_back"]
    key = "install_paths_preserved" if change == "mark_removed" else "install_files_left"
    assert report[key] == [str(path)]


def test_an_install_folder_without_ownership_marks_is_refused_cleanly(v2, target, monkeypatch):
    data_dir, base = target
    before = files_of(base)
    plan = ar.plan_restore(v2, data_dir, base)

    def unsupported(*_a, **_k):
        raise OSError(errno.ENOTSUP, "Operation not supported")

    monkeypatch.setattr(os, "setxattr", unsupported)
    with pytest.raises(ar.RestoreRefused) as exc:
        ar.restore_isolated(plan, v2, approved_plan_digest=plan["plan_digest"])
    assert exc.value.code == "INSTALL_FOLDER_UNSAFE"
    assert files_of(base) == before and leftovers(data_dir) == [] and not data_dir.exists()


def test_installed_files_carry_their_restore_id(v2, target):
    data_dir, base = target
    _plan, receipt = restore(v2, data_dir, base)
    created = [f["rel"] for f in receipt["install_files"] if f["action"] == "create"]
    assert created and all(os.getxattr(base / rel, ar._OWNER_XATTR) == receipt["restore_id"].encode()
                           for rel in created)


@pytest.mark.parametrize("contents", ["empty", "journal_temp_only", "unexpected_file"])
def test_a_staging_folder_killed_before_its_first_journal_is_cleared(contents, v2, target):
    # Sonnet R2 review G4: an empty, journal-less staging folder blocked restores forever.
    data_dir, base = target
    rid = "d" * 32
    staging = data_dir.parent / f".{data_dir.name}.restore-{rid}.staging"
    staging.mkdir(mode=0o700)
    if contents != "empty":
        history = staging / "restore_history" / rid
        history.mkdir(parents=True)
        for folder in (staging / "restore_history", history):
            folder.chmod(0o700)
        (history / "journal.json.0a1b2c3d.tmp").write_text("{")
        if contents == "unexpected_file":
            (history / "data.bin").write_text("?")
    report = ar.recover_interrupted_restore(data_dir)
    if contents == "unexpected_file":
        assert report["left_alone"] == [str(staging)] and staging.is_dir()
    else:
        assert report["cleared_never_journaled"] == [str(staging)] and leftovers(data_dir) == []
        restore(v2, data_dir, base)


def test_a_journal_whose_device_number_changed_still_rolls_back(v2, target, tmp_path):
    # Sonnet R2 review G4b: a remount can renumber st_dev.
    data_dir, base = target
    before = files_of(base)
    _killed_restore(v2, data_dir, base, "install_file_created", tmp_path)
    _staging, jpath = _journal_path(data_dir)
    journal = json.loads(jpath.read_text())
    journal["install_identity"][0] += 1
    jpath.write_text(json.dumps(journal))
    report = ar.recover_interrupted_restore(data_dir)
    assert report["rolled_back"] and report["install_paths_preserved"] == [] and files_of(base) == before


def test_a_file_replaced_after_its_ownership_was_proven_is_not_unlinked(v2, target, tmp_path, monkeypatch):
    data_dir, base = target
    _killed_restore(v2, data_dir, base, "install_file_created", tmp_path)
    _staging, jpath = _journal_path(data_dir)
    path = Path(json.loads(jpath.read_text())["install_created"][0]["path"])
    real_mark, swapped = ar._owner_mark, []

    def mark_then_swap(fd):
        mark = real_mark(fd)
        if not swapped and mark is not None:
            swapped.append(1)
            path.rename(path.with_name("ours.moved"))
            path.write_bytes(b"SOMEONE ELSE'S NEW FILE")
        return mark

    monkeypatch.setattr(ar, "_owner_mark", mark_then_swap)
    report = ar.recover_interrupted_restore(data_dir)
    assert swapped and path.read_bytes() == b"SOMEONE ELSE'S NEW FILE"
    assert str(path) in report["install_paths_preserved"]


def test_a_marked_temp_journaled_before_its_bytes_is_rolled_back(v2, target, tmp_path):
    # Killed after the identity reached the journal but before the seal did:
    # the temp is provably ours (inode + mark) and is removed, not stranded.
    data_dir, base = target
    before = files_of(base)
    _killed_restore(v2, data_dir, base, "install_tmp_journaled", tmp_path)
    report = ar.recover_interrupted_restore(data_dir)
    assert report["rolled_back"] and report["install_paths_preserved"] == [] and report["install_files_left"] == []
    assert files_of(base) == before and leftovers(data_dir) == []



# --------------------------------------------------------------------------
# R4 -- Sonnet 5.5 review of R3: shipped-package squats, search tables,
# first-start replay, install-folder litter, recovery that keeps going
# --------------------------------------------------------------------------

def _hand_built(source, tmp_path, name, mutate):
    """A benign export whose primary database is swapped for mutate(conn)'s
    result, manifest size/sha256 recomputed: what an attacker would build,
    past the exporter's own pre-filtering."""
    benign = _build_v2(source, tmp_path / f"benign-{name}.zip")
    items = entries(benign)
    manifest = json.loads(dict(items)["manifest.json"])
    member = next(m for m in manifest["members"] if m["logical_id"] == "state.databases.lumina_db")
    work = tmp_path / f"{name}.db"
    work.write_bytes(dict(items)[member["archive_path"]])
    with contextlib.closing(sqlite3.connect(work, isolation_level=None)) as conn:
        mutate(conn)
        conn.execute("PRAGMA journal_mode=DELETE")
    new = work.read_bytes()
    member["size"], member["sha256"] = len(new), hashlib.sha256(new).hexdigest()
    return repack(tmp_path / f"{name}.zip", [(n, abx._json_bytes(manifest) if n == "manifest.json"
                                              else new if n == member["archive_path"] else d) for n, d in items])


def _tracked():
    return sorted(json.loads(m.read_text())["name"] for m in (REPO / "skill_packages/official").glob("*/manifest.json"))


@pytest.mark.parametrize("squat", ["generated-artifact-manifest", "media-generation"])
def test_a_user_skill_squatting_a_shipped_name_is_renamed_and_every_package_bootstraps(squat, source, target, tmp_path):
    # Sonnet R3 review F-1: the bootstrap stopped at the squat and skipped every later package.
    from core import skill_transport
    data_dir, install = target
    shutil.copytree(REPO / "skill_packages", install / "skill_packages")

    def mutate(conn):
        conn.execute("INSERT INTO skills (name, description, path, created_at, updated_at, origin) "
                     f"VALUES ('{squat}', 'mine', '/x/skills/mine.md', 't', 't', 'user')")
        conn.execute("INSERT INTO skills (name, description, path, created_at, updated_at, origin) "
                     f"VALUES ('{squat} (restored)', 'older', '/x/skills/older.md', 't', 't', 'user')")
    _plan, receipt = restore(_hand_built(source, tmp_path, "squat", mutate), data_dir, install)
    note = receipt["rebind"]["primary_database"]
    assert note["skills_renamed_for_bootstrap"] == [{"from": squat, "to": f"{squat} (restored 2)"}]
    assert note["user_skills_named_like_tracked_official"] == [squat]
    db = data_dir / "memory/lumina.db"
    skill_transport.ensure_official_skills_bootstrapped(data_dir=str(data_dir), db_path=str(db))
    after = dict(rows(db, "SELECT name, origin FROM skills"))
    assert all(after[n] == "official" for n in _tracked())
    assert after[f"{squat} (restored 2)"] == "user" and after[f"{squat} (restored)"] == "user"


@pytest.mark.parametrize("collation", ["column_nocase", "index_only_nocase", "column_nocase_index_binary",
                                       "column_rtrim", "index_only_rtrim", "separate_rtrim_index"])
def test_skill_names_not_unique_under_binary_comparison_are_refused(collation, source, target, tmp_path):
    # Sonnet R3 review F-1(b)(c): NOCASE uniqueness made the bootstrap's own INSERT
    # fail at every start; a NOCASE column behind a BINARY index makes its
    # WHERE name=? match a case variant instead.
    ddl = {
        "column_nocase": _SKILLS_DDL.replace("name TEXT NOT NULL UNIQUE", "name TEXT NOT NULL UNIQUE COLLATE NOCASE"),
        "index_only_nocase": _SKILLS_DDL.replace("name TEXT NOT NULL UNIQUE", "name TEXT NOT NULL")[:-1]
        + ", UNIQUE (name COLLATE NOCASE))",
        "column_nocase_index_binary": _SKILLS_DDL.replace("name TEXT NOT NULL UNIQUE", "name TEXT NOT NULL COLLATE NOCASE")[:-1]
        + ", UNIQUE (name COLLATE BINARY))",
        # Sonnet R4 review G6: RTRIM ignores trailing spaces, which a case-only probe never sees.
        "column_rtrim": _SKILLS_DDL.replace("name TEXT NOT NULL UNIQUE", "name TEXT NOT NULL UNIQUE COLLATE RTRIM"),
        "index_only_rtrim": _SKILLS_DDL.replace("name TEXT NOT NULL UNIQUE", "name TEXT NOT NULL")[:-1]
        + ", UNIQUE (name COLLATE RTRIM))",
        "separate_rtrim_index": _SKILLS_DDL,
    }[collation]

    def mutate(conn):
        old = conn.execute("SELECT id, name, description, path, created_at, updated_at, content_sha256, origin "
                           "FROM skills").fetchall()
        conn.execute("DROP TABLE skills")
        conn.execute(ddl)
        for row in old:
            conn.execute("INSERT INTO skills (id, name, description, path, created_at, updated_at, content_sha256, "
                         "origin) VALUES (?,?,?,?,?,?,?,?)", row)
        for name in ("skills_ai", "skills_au", "skills_ad"):
            conn.execute(ar._SHIPPED_TRIGGERS[name])
        if collation == "separate_rtrim_index":
            conn.execute("CREATE UNIQUE INDEX skills_name_rtrim ON skills (name COLLATE RTRIM)")
    data_dir, install = target
    with pytest.raises(ar.RestoreRefused) as exc:
        restore(_hand_built(source, tmp_path, collation, mutate), data_dir, install)
    assert exc.value.code == "SQLITE_INVALID" and "unsupported schema" in exc.value.detail
    assert not data_dir.exists() and leftovers(data_dir) == []


@pytest.mark.parametrize("variant", ["skills_fts_plain_table", "skills_fts_fewer_columns", "skills_fts_points_nowhere",
                                     "chat_fts_plain_table"])
def test_a_search_table_other_than_the_shipped_one_is_refused(variant, source, target, tmp_path):
    # Sonnet R3 review F-2: an accepted root could fail first start or chat deletes.
    stmts = {
        "skills_fts_plain_table": ["DROP TABLE skills_fts", "CREATE TABLE skills_fts (name TEXT, description TEXT)"],
        "skills_fts_fewer_columns": ["DROP TABLE skills_fts", "CREATE VIRTUAL TABLE skills_fts USING fts5(name)"],
        "skills_fts_points_nowhere": ["DROP TABLE skills_fts", "CREATE VIRTUAL TABLE skills_fts USING fts5(name, "
                                      "description, content='nonexistent', content_rowid='id')"],
        "chat_fts_plain_table": ["DROP TABLE IF EXISTS chat_messages_fts",
                                 "CREATE TABLE chat_messages_fts (role TEXT, content TEXT)"],
    }[variant]

    def mutate(conn):
        conn.execute("DELETE FROM skills")
        for sql in stmts:
            conn.execute(sql)
    data_dir, install = target
    with pytest.raises(ar.RestoreRefused) as exc:
        restore(_hand_built(source, tmp_path, variant, mutate), data_dir, install)
    assert exc.value.code == "SQLITE_INVALID" and "search table" in exc.value.detail
    assert not data_dir.exists()


def test_missing_search_tables_are_not_refused_and_first_start_rebuilds_them(source, target, tmp_path, monkeypatch):
    def mutate(conn):
        conn.execute("DROP TABLE skills_fts")          # shipped triggers still name it, as after a partial init
    data_dir, install = target
    _plan, receipt = restore(_hand_built(source, tmp_path, "nofts", mutate), data_dir, install)
    assert ar.verify_restored_root(data_dir, receipt)["passed"] is True
    from core import skills
    db = data_dir / "memory/lumina.db"
    monkeypatch.setattr(config, "DB_PATH", str(db))
    skills.init_skills_db(str(db))
    assert skills.build_skills_block("debugging")[1]


def test_the_first_start_replay_catches_what_static_checks_cannot(source, target, tmp_path):
    # A write the runtime makes on every start fails: refuse instead of handing it over.
    def mutate(conn):
        conn.execute("CREATE TABLE skills_x AS SELECT * FROM skills")
        conn.execute("DROP TABLE skills_fts")
        conn.execute("CREATE TABLE skills_fts_data (id INTEGER PRIMARY KEY, block BLOB)")   # an fts5 shadow name
    data_dir, install = target
    with pytest.raises(ar.RestoreRefused) as exc:
        restore(_hand_built(source, tmp_path, "shadow", mutate), data_dir, install)
    assert exc.value.code == "SQLITE_INVALID" and "first-start replay" in exc.value.detail


def test_acceptance_replays_first_start_on_a_copy(v2, target):
    data_dir, base = target
    _plan, receipt = restore(v2, data_dir, base)
    db = data_dir / "memory/lumina.db"
    before = db.read_bytes()
    assert ar.verify_restored_root(data_dir, receipt)["checks"]["sqlite_schema_vetted"] is True
    assert db.read_bytes() == before                                   # the replay never touched the root
    with contextlib.closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("DROP TABLE skills_fts")
        conn.execute("CREATE TABLE skills_fts (name TEXT, description TEXT)")
    assert ar.verify_restored_root(data_dir, receipt)["checks"]["sqlite_schema_vetted"] is False


def _killed_at_journal_write(archive, data_dir, base, tmp_path, n):
    script = tmp_path / f"kill_j{n}.py"
    script.write_text(textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {str(REPO)!r})
        import core.agent_backup_restore as ar
        real, count = ar._write_journal, [0]
        def counting(*a, **k):
            count[0] += 1
            if count[0] == {n}:
                os._exit(77)
            return real(*a, **k)
        ar._write_journal = counting
        plan = ar.plan_restore({str(archive)!r}, {str(data_dir)!r}, {str(base)!r})
        ar.restore_isolated(plan, {str(archive)!r}, approved_plan_digest=plan["plan_digest"])
        os._exit(0)
    """))
    run = subprocess.run([sys.executable, str(script)], cwd=REPO, capture_output=True, text=True, timeout=120)
    assert run.returncode == 77, run.stderr[-2000:]


@pytest.mark.parametrize("write", [3, 4])
def test_no_install_folder_is_left_behind_whichever_journal_write_a_kill_lands_on(write, v2, target, tmp_path):
    # Sonnet R3 review F-3: a kill between mkdir and its journal write left empty folders.
    # (Write 3 records the folders, 4 the file's intent. A kill at 5 is the
    # documented window: a temp exists before its inode is journaled, so it and
    # its folders are preserved and reported -- see the identity tests.)
    data_dir, base = target
    shutil.rmtree(base / "personas")
    before = files_of(base)
    _killed_at_journal_write(v2, data_dir, base, tmp_path, write)
    report = ar.recover_interrupted_restore(data_dir)
    assert report["rolled_back"] and files_of(base) == before


def test_recovery_reports_a_staging_folder_it_cannot_remove_and_keeps_going(v2, target, tmp_path, monkeypatch):
    # Sonnet R3 review F-4: an unremovable folder aborted recovery and lost its report.
    data_dir, base = target
    _killed_restore(v2, data_dir, base, "install_file_created", tmp_path)
    (first,) = [p for p in data_dir.parent.iterdir() if p.name.endswith(".staging")]
    second = data_dir.parent / f".{data_dir.name}.restore-{'0' * 32}.staging"
    second.mkdir(mode=0o700)
    real = shutil.rmtree

    def stuck(path, *a, **k):
        if str(path) == str(first):
            raise PermissionError(13, "Permission denied", str(path))
        return real(path, *a, **k)

    monkeypatch.setattr(shutil, "rmtree", stuck)
    report = ar.recover_interrupted_restore(data_dir)
    assert report["left_alone"] == [str(first)] and report["cleared_never_journaled"] == [str(second)]
    assert report["rolled_back"] == [] and report["install_paths_preserved"] == []


def test_an_unreadable_never_journaled_staging_folder_is_left_alone_not_fatal(target):
    data_dir, _base = target
    rid = "e" * 32
    staging = data_dir.parent / f".{data_dir.name}.restore-{rid}.staging"
    hidden = staging / "restore_history" / rid
    hidden.mkdir(parents=True)
    for folder in (staging, staging / "restore_history", hidden):
        folder.chmod(0o700)
    hidden.chmod(0o000)
    try:
        report = ar.recover_interrupted_restore(data_dir)
    finally:
        hidden.chmod(0o700)
    assert report["left_alone"] == [str(staging)]


def test_a_large_journal_this_engine_wrote_reads_back(target):
    # Sonnet R3 review F-5: a 1 MiB read cap stranded journals past ~2,500 entries.
    data_dir, base = target
    rid = "9" * 32
    staging = data_dir.parent / f".{data_dir.name}.restore-{rid}.staging"
    staging.mkdir(mode=0o700)
    st = base.stat()
    paths = [str(base / "skills" / f"bulk-{i:05d}.md") for i in range(3000)]
    entries_ = [{"path": p, "tmp": ar._install_tmp(p, rid), "sha256": "0" * 64,
                 "identity": [st.st_dev, 10 ** 6 + i], "seal": [10, 1761700000123456789]} for i, p in enumerate(paths)]
    ar._write_journal(str(staging), rid, {"schema": ar.JOURNAL_SCHEMA, "version": ar.VERSION, "restore_id": rid,
                                          "install_created": entries_})
    assert (staging / "restore_history" / rid / "journal.json").stat().st_size > 1 << 20
    assert ar._read_journal(str(staging))["install_created"] == entries_


def test_more_install_files_than_the_journal_is_built_for_is_refused_at_plan_time(v2, target, monkeypatch):
    data_dir, base = target
    monkeypatch.setattr(ar, "MAX_INSTALL_FILES", 1)
    with pytest.raises(ar.RestoreRefused) as exc:
        ar.plan_restore(v2, data_dir, base)
    assert exc.value.code == "INSTALL_FILE_LIMIT" and not data_dir.exists()



def test_the_replay_holds_whatever_this_builds_foreign_key_default(v2, target, monkeypatch):
    # Sonnet R4 review R4d: the probe chat row names no real chat; with foreign
    # keys on by default every benign restore was refused.
    data_dir, base = target
    real = sqlite3.connect

    def fk_on(*a, **k):
        conn = real(*a, **k)
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    monkeypatch.setattr(sqlite3, "connect", fk_on)
    _plan, receipt = restore(v2, data_dir, base)
    assert ar.verify_restored_root(data_dir, receipt)["passed"] is True


def test_a_folder_another_writer_made_first_survives_the_rollback(v2, target, monkeypatch):
    # Sonnet R4 review R4f: journaled before mkdir, then made by someone else.
    data_dir, base = target
    stranger = base / "assets"

    def race(stage):
        if stage == "install_dirs_journaled" and not stranger.exists():
            stranger.mkdir()

    monkeypatch.setattr(ar, "_checkpoint", race)
    with pytest.raises(ar.RestoreRefused) as exc:
        restore(v2, data_dir, base)
    assert exc.value.code == "INSTALL_TARGET_CHANGED" and stranger.is_dir() and leftovers(data_dir) == []



_BOOTSTRAP_BREAKERS = {
    # Sonnet R5 review: tables the R5 gate accepted whose constraints make the
    # official bootstrap's own INSERT fail at every start.
    "check_not_official": (_SKILLS_DDL[:-1] + ", CHECK (origin <> 'official'))", []),
    "check_user_only": (_SKILLS_DDL[:-1] + ", CHECK (origin = 'user'))", []),
    "check_package_name": (_SKILLS_DDL[:-1] + ", CHECK (name <> 'media-generation'))", []),
    "unique_hash": (_SKILLS_DDL[:-1] + ", UNIQUE (content_sha256))", ["published_hash_row"]),
    "expression_unique_index": (_SKILLS_DDL, ["CREATE UNIQUE INDEX skills_squash ON skills (replace(name, '-', ''))",
                                              "INSERT INTO skills (name, description, path, created_at, updated_at, origin)"
                                              " VALUES ('mediageneration', 'x', '/x/skills/m.md', 't', 't', 'user')"]),
}


def _hostile_skills(conn, ddl, extra):
    old = conn.execute("SELECT id, name, description, path, created_at, updated_at, content_sha256, origin "
                       "FROM skills").fetchall()
    conn.execute("DROP TABLE skills")
    conn.execute(ddl)
    for row in old:                                   # as user rows: some of these tables forbid anything else
        conn.execute("INSERT INTO skills (id, name, description, path, created_at, updated_at, content_sha256, origin) "
                     "VALUES (?,?,?,?,?,?,?,'user')", row[:7])
    for name in ("skills_ai", "skills_au", "skills_ad"):
        conn.execute(ar._SHIPPED_TRIGGERS[name])
    for sql in extra:
        if sql == "published_hash_row":
            manifest = json.loads((REPO / "skill_packages/official/media-generation/manifest.json").read_text())
            conn.execute("INSERT INTO skills (name, description, path, created_at, updated_at, origin, content_sha256) "
                         "VALUES ('copy', 'x', '/x/skills/c.md', 't', 't', 'user', ?)", (manifest["payload_sha256"],))
        else:
            conn.execute(sql)


@pytest.mark.parametrize("breaker", list(_BOOTSTRAP_BREAKERS))
def test_a_skills_table_that_would_break_the_official_bootstrap_is_refused(breaker, source, target, tmp_path):
    ddl, extra = _BOOTSTRAP_BREAKERS[breaker]
    data_dir, install = target
    with pytest.raises(ar.RestoreRefused) as exc:
        restore(_hand_built(source, tmp_path, breaker, lambda conn: _hostile_skills(conn, ddl, extra)), data_dir, install)
    assert exc.value.code == "SQLITE_INVALID" and "unsupported schema" in exc.value.detail
    assert not data_dir.exists() and leftovers(data_dir) == []


def _replay_only(ddl, extra=()):
    """The first-start replay on its own (the static pin bypassed), so each
    layer is held to its own guard."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute(ddl)
    for sql in extra:
        conn.execute(sql)
    return conn


@pytest.mark.parametrize("ddl", [
    _SKILLS_DDL.replace("name TEXT NOT NULL UNIQUE", "name TEXT NOT NULL UNIQUE COLLATE NOCASE"),
    _SKILLS_DDL.replace("name TEXT NOT NULL UNIQUE", "name TEXT NOT NULL UNIQUE COLLATE RTRIM"),
    _SKILLS_DDL.replace("name TEXT NOT NULL UNIQUE", "name TEXT NOT NULL COLLATE NOCASE")[:-1] + ", UNIQUE (name COLLATE BINARY))",
])
def test_the_replay_alone_proves_binary_names(ddl, monkeypatch):
    monkeypatch.setattr(ar, "_skills_shape", lambda conn: True)
    with contextlib.closing(_replay_only(ddl)) as conn:
        with pytest.raises(ar.RestoreRefused) as exc:
            ar._startup_replay(conn)
    assert "BINARY" in exc.value.detail


@pytest.mark.parametrize("breaker", ["check_not_official", "check_user_only"])
def test_the_replay_alone_writes_an_official_row_as_the_bootstrap_does(breaker, monkeypatch):
    monkeypatch.setattr(ar, "_skills_shape", lambda conn: True)
    with contextlib.closing(_replay_only(_BOOTSTRAP_BREAKERS[breaker][0])) as conn:
        with pytest.raises(ar.RestoreRefused) as exc:
            ar._startup_replay(conn)
    assert "first-start replay" in exc.value.detail


def test_the_replay_migrates_an_old_table_before_writing(monkeypatch):
    with contextlib.closing(_replay_only(_OLD_CREATE)) as conn:
        ar._startup_replay(conn)                                      # passes, and leaves nothing behind
        assert [r[1] for r in conn.execute("PRAGMA table_info(skills)")][-1] == "updated_at"


@pytest.mark.parametrize("index", [
    "CREATE UNIQUE INDEX skills_squash ON skills (replace(name, '-', ''))",
    "CREATE INDEX skills_by_origin ON skills (origin)",
])
def test_a_released_skills_table_with_an_extra_index_is_refused(index, source, target, tmp_path):
    # The definition text matches a released form; only the index rule can see this.
    def mutate(conn):
        conn.execute(index)
        conn.execute("INSERT INTO skills (name, description, path, created_at, updated_at, origin)"
                     " VALUES ('mediageneration', 'x', '/x/skills/m.md', 't', 't', 'user')")
    data_dir, install = target
    with pytest.raises(ar.RestoreRefused) as exc:
        restore(_hand_built(source, tmp_path, "extra-index", mutate), data_dir, install)
    assert exc.value.code == "SQLITE_INVALID" and "indexes are not the shipped one" in exc.value.detail
    assert not data_dir.exists()


def test_the_restore_runs_the_first_start_replay_before_accepting(source, target, tmp_path):
    # Every pinned table is as shipped; a chat write first start would make
    # fails, which only the replay on the staged copy can see.
    def mutate(conn):
        conn.execute("ALTER TABLE chat_messages RENAME TO chat_messages_old")
        conn.execute("CREATE TABLE chat_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, "
                     "role TEXT NOT NULL CHECK (role <> 'user'), content TEXT NOT NULL, metadata TEXT, "
                     "created_at TEXT NOT NULL)")
        conn.execute("DROP TABLE chat_messages_old")
    data_dir, install = target
    with pytest.raises(ar.RestoreRefused) as exc:
        restore(_hand_built(source, tmp_path, "chat-check", mutate), data_dir, install)
    assert exc.value.code == "SQLITE_INVALID" and "first-start replay" in exc.value.detail
    assert not data_dir.exists()


# --------------------------------------------------------------------------
# R9 -- the R8 pre-gate finding: an archived default or generated column must
# never stamp provenance (PALACE-GUARD-01B-2) onto rows the runtime writes
# --------------------------------------------------------------------------

def _redefine_column(conn, table, column, clause):
    """Rebuild `table` with `column` declared as `clause`; rows kept."""
    (sql,), = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchall()
    declared = next(part for part in (f"{column} TEXT", f"{column} INTEGER") if part in sql)
    conn.execute("PRAGMA legacy_alter_table=ON")    # keep other tables' foreign keys on `table`
    conn.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
    conn.execute(sql.replace(declared, clause, 1))
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table}_old)")]
    hidden = {r[1] for r in conn.execute(f"PRAGMA table_xinfo({table})") if r[6]}
    keep = ", ".join(c for c in cols if c not in hidden)
    conn.execute(f"INSERT INTO {table} ({keep}) SELECT {keep} FROM {table}_old")
    conn.execute(f"DROP TABLE {table}_old")


def test_a_model_written_lookalike_never_becomes_a_trusted_compaction_checkpoint(source, target, tmp_path, monkeypatch):
    # The R8 pre-gate finding, kept as a permanent regression. Control: a
    # shipped schema restores, and the model's lookalike authorizes no skip.
    data_dir, install = target
    _plan, receipt = restore(_hand_built(source, tmp_path, "palace-control", lambda conn: None), data_dir, install)
    assert ar.verify_restored_root(data_dir, receipt)["passed"] is True
    db = data_dir / "memory/lumina.db"
    monkeypatch.setattr(config, "DB_PATH", str(db))
    from core import manual_compaction
    from tools import palace
    palace.init_palace_db()
    palace._palace_remember_tool("summary", wing="nightstand", room="166",
                                 tags=["manual-compaction", "session:166", "context-skip:2"])
    assert rows(db, "SELECT origin FROM palace_drawers ORDER BY id DESC LIMIT 1")[0][0] is None
    assert manual_compaction.latest_manual_compaction_skip(166) == 0


@pytest.mark.parametrize("table, column, clause", [
    ("palace_drawers", "origin", "origin TEXT DEFAULT 'manual_compaction'"),        # the R8 finding itself
    ("palace_drawers", "origin", "origin TEXT DEFAULT 'dream_sweep'"),
    ("palace_drawers", "source_memory_id", "source_memory_id INTEGER DEFAULT 1"),
    ("palace_halls", "admission", "admission TEXT DEFAULT 'explicit_owner_promotion'"),
    ("palace_closets", "withheld_reason", "withheld_reason TEXT DEFAULT 'source_deleted_pending_review'"),
    ("palace_drawers", "origin", "origin TEXT GENERATED ALWAYS AS ('manual_compaction') VIRTUAL"),
    ("palace_drawers", "origin", "origin TEXT GENERATED ALWAYS AS ('manual_compaction') STORED"),
    # Sonnet R9 review: an expression default is evaluated per insert, so it can
    # read NULL during the rehearsal and stamp later (a clock bomb, or random()).
    ("palace_drawers", "origin", "origin TEXT DEFAULT (CASE WHEN CAST(strftime('%s','now') AS INTEGER) "
                                 ">= 4102444800 THEN 'manual_compaction' END)"),
    ("palace_drawers", "origin", "origin TEXT DEFAULT (CASE WHEN abs(random()) % 8 = 0 "
                                 "THEN 'manual_compaction' END)"),
    ("palace_halls", "admission", "admission TEXT DEFAULT CURRENT_TIMESTAMP"),
    ("memories", "content", "content TEXT DEFAULT (lower('GENERAL'))"),
])
def test_an_archived_default_or_generated_column_cannot_stamp_provenance(table, column, clause, source, target, tmp_path):
    data_dir, install = target
    archive = _hand_built(source, tmp_path, f"{table}-{column}",
                          lambda conn: _redefine_column(conn, table, column, clause))
    with pytest.raises(ar.RestoreRefused) as exc:
        restore(archive, data_dir, install)
    assert exc.value.code == "SQLITE_INVALID" and "unsupported schema" in exc.value.detail
    expected = ("generated column" if "GENERATED" in clause
                else "non-literal default" if "(" in clause.split("DEFAULT", 1)[-1] or "CURRENT_" in clause
                else f"sets {table}.{column}")
    assert expected in exc.value.detail
    assert not data_dir.exists() and leftovers(data_dir) == []


def test_the_runtime_s_own_schema_passes_the_write_rehearsal(tmp_path, monkeypatch):
    # No false refusal: the tables exactly as the shipped runtime creates them.
    from core import chat_history, skills
    from tools import memory, palace
    db = tmp_path / "fresh.db"
    monkeypatch.setattr(config, "DB_PATH", str(db))
    memory.init_memory_db()
    memory.init_chat_db()
    palace.init_palace_db()
    skills.init_skills_db(str(db))
    chat_history.init_chat_history_fts()
    with contextlib.closing(sqlite3.connect(db, isolation_level=None)) as conn:
        assert ar._generated_columns(conn) == []
        assert set(ar._RUNTIME_WRITES) <= {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        ar._startup_replay(conn)
        assert conn.execute("SELECT count(*) FROM palace_drawers").fetchone()[0] == 0     # rolled back


@pytest.mark.parametrize("clause", ["origin TEXT DEFAULT 'manual_compaction'",
                                    "origin TEXT GENERATED ALWAYS AS ('manual_compaction') VIRTUAL",
                                    "origin TEXT DEFAULT (CASE WHEN abs(random()) % 2 = 9 THEN 'x' END)"])
def test_acceptance_re_derives_the_provenance_rule(clause, v2, target):
    data_dir, base = target
    _plan, receipt = restore(v2, data_dir, base)
    with contextlib.closing(sqlite3.connect(data_dir / "memory/lumina.db")) as conn, conn:
        _redefine_column(conn, "palace_drawers", "origin", clause)
    assert ar.verify_restored_root(data_dir, receipt)["checks"]["sqlite_schema_vetted"] is False



def test_every_shipped_default_is_a_literal(tmp_path, monkeypatch):
    # No false refusal: the literal-only rule holds for the runtime's own tables.
    from core import chat_history, flight_recorder, skills
    from tools import memory, palace
    db = tmp_path / "fresh.db"
    monkeypatch.setattr(config, "DB_PATH", str(db))
    memory.init_memory_db()
    memory.init_chat_db()
    palace.init_palace_db()
    skills.init_skills_db(str(db))
    chat_history.init_chat_history_fts()
    recorder = flight_recorder.FlightRecorder(db_path=str(tmp_path / "flight_recorder.db"))
    recorder.close() if hasattr(recorder, "close") else None
    for path in (db, tmp_path / "flight_recorder.db"):
        with contextlib.closing(sqlite3.connect(path)) as conn:
            assert ar._non_literal_defaults(conn) == [] and ar._generated_columns(conn) == []
    with contextlib.closing(sqlite3.connect(db)) as conn:
        defaults = {str(r[0]) for r in conn.execute(
            "SELECT p.dflt_value FROM sqlite_master m, pragma_table_xinfo(m.name) p "
            "WHERE m.type='table' AND p.dflt_value IS NOT NULL")}
    assert defaults and all(ar._LITERAL_DEFAULT.fullmatch(d) for d in defaults)


# --------------------------------------------------------------------------
# R11 -- Sonnet R10 review: archived foreign-key actions (the runtime runs with
# foreign keys on) rewrote runtime-written Palace layers and trust flags after
# acceptance. Owner decision: allow exactly the shipped foreign keys.
# --------------------------------------------------------------------------

def _rewrite_table(conn, table, old, new):
    """Rebuild `table` from its stored CREATE text with `old` replaced by `new`."""
    (sql,), = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchall()
    assert sql.count(old) == 1, (table, old)
    conn.execute("PRAGMA legacy_alter_table=ON")    # keep other tables' foreign keys on `table`
    conn.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
    conn.execute(sql.replace(old, new))
    cols = ", ".join(r[1] for r in conn.execute(f"PRAGMA table_info({table}_old)"))
    conn.execute(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM {table}_old")
    conn.execute(f"DROP TABLE {table}_old")


@pytest.mark.parametrize("table, old, new, column", [
    # Sonnet T7: a self-referencing SET DEFAULT lifted a model-written closet to layer 0.
    ("palace_closets", "layer      INTEGER NOT NULL DEFAULT 2",
     "layer INTEGER NOT NULL DEFAULT 0 REFERENCES palace_closets(id) ON DELETE SET DEFAULT", "layer"),
    # Sonnet T8: an owner's unrelated memory deletion flipped a drawer to trusted.
    ("palace_drawers", "untrusted  INTEGER NOT NULL DEFAULT 0",
     "untrusted INTEGER NOT NULL DEFAULT 0 REFERENCES memories(id) ON DELETE SET DEFAULT", "untrusted"),
    # The same class through a shipped action on an unshipped column: un-withholding.
    ("palace_closets", "withheld_reason TEXT", "withheld_reason TEXT REFERENCES palace_rooms(id) ON DELETE SET NULL",
     "withheld_reason"),
    # A shipped key with another action, and an ON UPDATE action.
    ("palace_drawers", "ON DELETE SET NULL", "ON DELETE CASCADE", "closet_id"),
    ("palace_rooms", "ON DELETE CASCADE", "ON DELETE CASCADE ON UPDATE SET NULL", "wing_id"),
])
def test_only_the_shipped_foreign_keys_and_actions_restore(table, old, new, column, source, target, tmp_path):
    data_dir, install = target
    archive = _hand_built(source, tmp_path, f"fk-{table}", lambda conn: _rewrite_table(conn, table, old, new))
    with pytest.raises(ar.RestoreRefused) as exc:
        restore(archive, data_dir, install)
    assert exc.value.detail == f"unsupported schema: foreign key not shipped on {table}.{column}"
    assert not data_dir.exists() and leftovers(data_dir) == []


def test_the_foreign_key_allow_list_is_exactly_what_the_runtime_declares(tmp_path, monkeypatch):
    import re as _re
    from core import chat_history, context_checkpoints, generation_artifact, generation_job, skills
    from tools import memory, palace
    db = tmp_path / "fresh.db"
    monkeypatch.setattr(config, "DB_PATH", str(db))
    memory.init_memory_db()
    memory.init_chat_db()
    palace.init_palace_db()
    skills.init_skills_db(str(db))
    chat_history.init_chat_history_fts()
    context_checkpoints.init_context_checkpoint_db()
    generation_job.init_generation_job_db()
    generation_artifact.init_generation_artifact_db()
    binding = (REPO / "core/chatgpt_plan_chat_binding.py").read_text()
    (ddl,) = _re.findall(r'"""\s*(CREATE TABLE IF NOT EXISTS chatgpt_plan_chat_bindings \(.*?\))\s*"""', binding, _re.S)
    with contextlib.closing(sqlite3.connect(db)) as conn, conn:
        conn.execute(ddl)
    with contextlib.closing(sqlite3.connect(db)) as conn:
        declared = {(t, frm, to_t, to, upd, dele) for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                    for _i, _s, to_t, frm, to, upd, dele, _m in conn.execute(f'PRAGMA foreign_key_list("{t}")')}
        assert declared == ar._SHIPPED_FOREIGN_KEYS and ar._unshipped_foreign_keys(conn) == []


def test_acceptance_re_derives_the_foreign_key_rule(v2, target):
    data_dir, base = target
    _plan, receipt = restore(v2, data_dir, base)
    with contextlib.closing(sqlite3.connect(data_dir / "memory/lumina.db")) as conn, conn:
        _rewrite_table(conn, "palace_closets", "withheld_reason TEXT",
                       "withheld_reason TEXT REFERENCES palace_rooms(id) ON DELETE SET NULL")
    with contextlib.closing(sqlite3.connect(data_dir / "memory/lumina.db")) as conn:
        assert ar._unshipped_foreign_keys(conn) == ["palace_closets.withheld_reason"]
    assert ar.verify_restored_root(data_dir, receipt)["checks"]["sqlite_schema_vetted"] is False
