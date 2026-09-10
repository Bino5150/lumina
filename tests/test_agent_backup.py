"""
AGENT-BACKUP-RESTORE-A2F3 -- tests for core/agent_backup.py.

Every test builds its own fake data_dir/base_dir under tmp_path and passes
an explicit identity_trailers_path and credentials_path (never the bare
defaults) so nothing here can ever read, depend on, or touch this
machine's real ~/.config/lumina/identity_trailers.json or
~/.config/lumina/credentials.json, and no test ever mutates a live
DATA_DIR/BASE_DIR or a live user overlay.

Organized by AGENT-BACKUP-RESTORE-A2R2/A2R3's blocker classes, converting
each reproduced failure into a regression test. Tests inherited unchanged
from earlier passes (already independently confirmed working by prior
reviews) are kept as locked-in regressions rather than removed.
"""
import ast
import errno
import hashlib
import inspect
import io
import itertools
import json
import os
import shutil
import socket
import sqlite3
import stat
import struct
import subprocess
import threading
import time
import warnings
import zipfile

import pytest

import core.agent_backup as ab


NO_IDENTITY_TRAILERS = "/nonexistent/identity_trailers.json"
NO_CREDENTIALS = "/nonexistent/credentials.json"

# The exact table/column set core/agent.py's Agent.__init__ initializes
# UNCONDITIONALLY on every startup (init_memory_db/init_chat_db/
# init_skills_db) -- mirrors core/agent_backup.py's own
# _LUMINA_DB_REQUIRED_TABLES fingerprint so _make_env's fake lumina.db
# passes the new role-schema verification instead of being a generic
# "any healthy SQLite file."
_REAL_LUMINA_DB_SCHEMA = (
    """CREATE TABLE memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        label TEXT DEFAULT 'general',
        content TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE chats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE chat_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        metadata TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE skills (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        description TEXT NOT NULL,
        path TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
)

# Mirrors core/flight_recorder.py's own _SCHEMA_SQL exactly.
_REAL_FLIGHT_RECORDER_DB_SCHEMA = """
    CREATE TABLE events (
        seq         INTEGER PRIMARY KEY AUTOINCREMENT,
        ts          REAL NOT NULL,
        runtime_id  TEXT NOT NULL,
        chat_id     INTEGER,
        turn_id     TEXT,
        task_id     TEXT,
        process_id  TEXT,
        worktree_id TEXT,
        activity_id TEXT,
        epoch       INTEGER,
        event_type  TEXT NOT NULL,
        severity    TEXT NOT NULL,
        provenance  TEXT NOT NULL,
        backend     TEXT,
        model       TEXT,
        fields_json TEXT NOT NULL,
        expires_at  REAL NOT NULL
    )
"""


def _real_lumina_db_bytes_and_hash(tmp_path, name="real_lumina.db", with_chat_row=True):
    """A schema-correct (role-check-passing) lumina.db, for tests that
    construct a hostile MANIFEST by hand but still need a genuinely
    valid lumina_db payload so the specific violation under test is
    isolated rather than drowned out by an unrelated schema failure."""
    p = tmp_path / name
    if p.exists():
        p.unlink()
    conn = sqlite3.connect(str(p))
    for stmt in _REAL_LUMINA_DB_SCHEMA:
        conn.execute(stmt)
    if with_chat_row:
        conn.execute("INSERT INTO chats (id, name, created_at, updated_at) VALUES (1, 'Chat', 't', 't')")
    conn.commit()
    conn.close()
    data = p.read_bytes()
    return data, hashlib.sha256(data).hexdigest()


def _real_flight_recorder_db_bytes_and_hash(tmp_path, name="real_fr.db"):
    p = tmp_path / name
    if p.exists():
        p.unlink()
    conn = sqlite3.connect(str(p))
    conn.execute(_REAL_FLIGHT_RECORDER_DB_SCHEMA)
    conn.commit()
    conn.close()
    data = p.read_bytes()
    return data, hashlib.sha256(data).hexdigest()


def _git_commit_all(repo_dir, gitignore_patterns=()):
    """git-inits repo_dir (not yet a git checkout) and commits its
    current contents, optionally .gitignore-ing some patterns first (so
    a file like the real release's untracked rubber-duck-debugging.md
    can be reproduced as genuinely untracked, not merely omitted from a
    hand-rolled baseline dict)."""
    if gitignore_patterns:
        with open(os.path.join(repo_dir, ".gitignore"), "w") as f:
            f.write("\n".join(gitignore_patterns) + "\n")
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_dir, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo_dir, check=True)


def _real_baseline_for(base_dir, gitignore_patterns=()):
    """git-inits+commits base_dir (a _make_env fixture, not yet a git
    checkout) and returns a REAL baseline dict via
    ab.generate_baseline_hashes -- AGENT-BACKUP-RESTORE-A2F3 added git-
    object cross-verification to _load_baseline_hashes (Section 10), so a
    hand-rolled _wrap_baseline() with a fake git_commit is now correctly
    REJECTED (falls back to provenance=unknown) rather than trusted; tests
    that need a baseline the new verification will actually TRUST must
    use a real git-backed one instead."""
    _git_commit_all(base_dir, gitignore_patterns)
    return ab.generate_baseline_hashes(base_dir)


def _make_env(tmp_path, with_ledger=True):
    """Minimal-but-real environment covering every collected category."""
    data_dir = tmp_path / "data_dir"
    base_dir = tmp_path / "base_dir"

    (data_dir / "memory").mkdir(parents=True)
    (data_dir / "telemetry").mkdir(parents=True)
    (data_dir / "custom_tools" / "_pending").mkdir(parents=True)
    (data_dir / "projects" / "demo").mkdir(parents=True)
    (base_dir / "personas").mkdir(parents=True)
    (base_dir / "assets" / "avatars").mkdir(parents=True)
    (base_dir / "assets" / "voices").mkdir(parents=True)
    (base_dir / "skills").mkdir(parents=True)
    (base_dir / "tool_profiles").mkdir(parents=True)
    (base_dir / "projects" / "demo").mkdir(parents=True)

    conn = sqlite3.connect(str(data_dir / "memory" / "lumina.db"))
    for stmt in _REAL_LUMINA_DB_SCHEMA:
        conn.execute(stmt)
    conn.execute(
        "INSERT INTO chats (id, name, created_at, updated_at) VALUES (1, 'Test Chat', 't', 't')"
    )
    conn.commit()
    conn.close()

    conn = sqlite3.connect(str(data_dir / "telemetry" / "flight_recorder.db"))
    conn.execute(_REAL_FLIGHT_RECORDER_DB_SCHEMA)
    conn.commit()
    conn.close()

    if with_ledger:
        conn = sqlite3.connect(str(data_dir / "memory" / "ledger.db"))
        conn.execute("CREATE TABLE ledger (request_id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO ledger VALUES ('deadbeef')")
        conn.commit()
        conn.close()

    (data_dir / "memory" / "prefs.json").write_text(
        json.dumps({"last_persona": "/old/base_dir/personas/lumina.json"})
    )
    (data_dir / "custom_tools" / "my_tool.py").write_text("def foo(): pass\n")
    (data_dir / "custom_tools" / "_pending" / "staged_tool.py").write_text("def bar(): pass\n")
    (data_dir / "memory" / "tool_audit.log").write_text(
        json.dumps({"event": "approved", "name": "my_tool"}) + "\n"
    )
    (data_dir / "memory" / "pending_actions.json").write_text("{}")
    (data_dir / "memory" / "pending_actions_audit.log").write_text("")

    (base_dir / "personas" / "lumina.json").write_text(json.dumps({"name": "Lumina"}))
    (base_dir / "assets" / "avatars" / "lumina.png").write_bytes(b"\x89PNG fake")
    (base_dir / "assets" / "voices" / "lumina.wav").write_bytes(b"RIFF fake")
    (base_dir / "skills" / "rubber-duck-debugging.md").write_text("# Skill: Rubber Duck Debugging\n")
    (base_dir / "tool_profiles" / "chat.json").write_text(json.dumps({"name": "Chat"}))
    (base_dir / "projects" / "projectlist.md").write_text("# Projects\n")
    (base_dir / "projects" / "demo" / "project.md").write_text("# demo\n")
    (base_dir / "projects" / "demo" / "codebase.md").write_text("# Codebase Index\n")
    (data_dir / "projects" / "demo" / "binding.json").write_text(json.dumps({"root": "/tmp/somewhere"}))
    (data_dir / "projects" / "demo" / "chats.json").write_text(
        json.dumps([{"chat_id": 1, "summary": "hi"}])
    )

    return str(data_dir), str(base_dir)


def _build(data_dir, base_dir, dest, **kwargs):
    kwargs.setdefault("identity_trailers_path", NO_IDENTITY_TRAILERS)
    kwargs.setdefault("credentials_path", NO_CREDENTIALS)
    return ab.build_agent_backup(data_dir, base_dir, dest, **kwargs)


def _members_by_path(manifest):
    return {m["archive_path"]: m for m in manifest["members"]}


def _wrap_baseline(files, lumina_version="unknown", git_commit="0" * 40):
    """A2F2's baseline format is version/commit-bound, not a flat
    {path: hash} dict -- this builds a syntactically valid one for tests
    that don't care about a specific commit/version pin. lumina_version
    defaults to 'unknown', which _load_baseline_hashes treats as a
    wildcard that always matches the running build's own version."""
    return {
        "format": ab.BASELINE_FORMAT,
        "format_version": ab.BASELINE_FORMAT_VERSION,
        "lumina_version": lumina_version,
        "git_commit": git_commit,
        "hash_algorithm": "sha256",
        "files": files,
    }


# =======================================================================
# Blocker 1 -- Database Snapshot Correctness (unchanged from A2F, locked in)
# =======================================================================

def test_snapshot_via_backup_api_captures_committed_rows(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)

    with zipfile.ZipFile(dest) as zf:
        staged_bytes = zf.read("state/databases/lumina.db")
    staged_path = tmp_path / "extracted_lumina.db"
    staged_path.write_bytes(staged_bytes)
    conn = sqlite3.connect(str(staged_path))
    n = conn.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
    conn.close()
    assert n == 1


def test_concurrent_writer_lock_releases_before_timeout_snapshot_waits_and_is_consistent(tmp_path):
    """The exact class of failure A2R reproduced: 'source DB: 1 row,
    backup DB: 0 rows, verification: valid=True'. A checkpoint-then-copy
    can silently miss a commit; the online backup API must wait out a
    lock rather than racing past it."""
    db_path = tmp_path / "solo.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE chats (id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO chats VALUES (1)")
    conn.commit()
    conn.close()

    writer = sqlite3.connect(str(db_path), timeout=30)
    writer.execute("BEGIN EXCLUSIVE")
    writer.execute("INSERT INTO chats VALUES (2)")

    result = {}

    def _run():
        try:
            staged = str(tmp_path / "staged.db")
            ab._snapshot_sqlite(str(db_path), staged, timeout_seconds=10)
            c = sqlite3.connect(staged)
            result["rows"] = c.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
            c.close()
        except Exception as e:
            result["error"] = e

    t = threading.Thread(target=_run)
    t.start()
    time.sleep(1.0)
    writer.commit()
    writer.close()
    t.join(timeout=15)

    assert result.get("rows") == 2, f"snapshot did not wait for the committed row: {result}"


def test_sustained_lock_contention_fails_loudly_not_silently_stale(tmp_path):
    db_path = tmp_path / "solo.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE chats (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    writer = sqlite3.connect(str(db_path), timeout=30)
    writer.execute("BEGIN EXCLUSIVE")
    writer.execute("INSERT INTO chats VALUES (99)")

    result = {}

    def _run():
        try:
            staged = str(tmp_path / "staged.db")
            ab._snapshot_sqlite(str(db_path), staged, timeout_seconds=1.5)
            result["succeeded"] = True
        except ab.AgentBackupError as e:
            result["error"] = str(e)

    t = threading.Thread(target=_run)
    t.start()
    t.join(timeout=10)
    writer.rollback()
    writer.close()

    assert "error" in result, f"expected AgentBackupError on sustained contention, got {result}"


def test_sqlite_connect_timeout_matches_configured_deadline_not_default_five_seconds(tmp_path):
    """AGENT-BACKUP-RESTORE-A2R2: 'configured timeout = 0.25s, actual
    block ~= 5.01s'. Python's sqlite3.connect() defaults to a 5-second
    busy_timeout regardless of any deadline this module documents unless
    explicitly overridden -- this proves _snapshot_sqlite's connect call
    actually honors ITS OWN configured timeout_seconds, not the stdlib
    default."""
    db_path = tmp_path / "solo.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    writer = sqlite3.connect(str(db_path), timeout=30)
    writer.execute("BEGIN EXCLUSIVE")
    writer.execute("INSERT INTO t VALUES (1)")

    result = {}

    def _run():
        start = time.monotonic()
        try:
            staged = str(tmp_path / "staged.db")
            ab._snapshot_sqlite(str(db_path), staged, timeout_seconds=0.5)
            result["succeeded"] = True
        except ab.AgentBackupError:
            result["failed"] = True
        result["elapsed"] = time.monotonic() - start

    t = threading.Thread(target=_run)
    t.start()
    t.join(timeout=15)
    writer.rollback()
    writer.close()

    assert result.get("failed") is True, f"expected a bounded failure, got {result}"
    assert result["elapsed"] < 3.0, (
        f"snapshot took {result['elapsed']:.2f}s against a 0.5s deadline -- looks like "
        f"the stdlib sqlite3 default 5s busy_timeout dominated instead of the configured one"
    )


def test_corrupt_required_database_fails(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    with open(os.path.join(data_dir, "memory", "lumina.db"), "wb") as f:
        f.write(b"not a real sqlite file at all, just garbage bytes")
    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError):
        _build(data_dir, base_dir, dest)
    assert not os.path.exists(dest)


def test_required_primary_database_missing_fails(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    os.remove(os.path.join(data_dir, "memory", "lumina.db"))
    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError):
        _build(data_dir, base_dir, dest)
    assert not os.path.exists(dest)


def test_required_prefs_missing_fails(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    os.remove(os.path.join(data_dir, "memory", "prefs.json"))
    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError):
        _build(data_dir, base_dir, dest)
    assert not os.path.exists(dest)


def test_flight_recorder_missing_fails_full_backup(tmp_path):
    """A1 classifies flight_recorder.db as REQUIRED_AGENT_STATE with no
    documented valid-absence exception -- AGENT-BACKUP-RESTORE-A2R2 found
    the A2F candidate silently treated its absence as a valid, successful
    archive. Flight Recorder is a core-instrumentation singleton created
    at startup in real operation, not a lazily-created optional file, so
    this inverts the OLD (bug) test rather than keeping it: absence now
    fails the build, matching lumina.db/prefs.json's existing rigor."""
    data_dir, base_dir = _make_env(tmp_path)
    os.remove(os.path.join(data_dir, "telemetry", "flight_recorder.db"))
    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError, match="Flight Recorder"):
        _build(data_dir, base_dir, dest)
    assert not os.path.exists(dest)


def test_ledger_db_never_staged_snapshotted_or_hashed_even_when_present(tmp_path):
    data_dir, base_dir = _make_env(tmp_path, with_ledger=True)
    assert os.path.exists(os.path.join(data_dir, "memory", "ledger.db"))
    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest)

    assert not any("ledger" in m["archive_path"] for m in manifest["members"])
    with zipfile.ZipFile(dest) as zf:
        assert not any("ledger" in n for n in zf.namelist())

    regen_ids = [r["logical_id"] for r in manifest["regenerate"]]
    assert "state.databases.ledger_db" in regen_ids
    ledger_entry = next(r for r in manifest["regenerate"] if r["logical_id"] == "state.databases.ledger_db")
    assert ledger_entry["restore_policy"] == "always_regenerate_empty"
    assert "archive_path" not in ledger_entry
    assert "sha256" not in ledger_entry
    assert "size" not in ledger_entry


# =======================================================================
# Blocker 2 -- SQLite Sources Must Obey the Same Credential Boundary
# =======================================================================

_MARKER = b"sk-TEST-ONLY-FAKE-MARKER-9f3a1c"


def _make_fake_credentials(tmp_path):
    creds_dir = tmp_path / "fake_config_lumina"
    creds_dir.mkdir()
    creds_path = creds_dir / "credentials.json"
    creds_path.write_bytes(b'{"openai_api_key": "' + _MARKER + b'"}')
    return str(creds_path)


def _make_fake_credentials_valid_sqlite(tmp_path, name="fake_credentials_store.db"):
    """A fake credentials store that is ALSO a syntactically valid SQLite
    database (AGENT-BACKUP-RESTORE-A2R3's connect-time swap attack relies
    on sqlite3.connect() successfully opening it, not merely being
    link()-able)."""
    creds_path = tmp_path / name
    conn = sqlite3.connect(str(creds_path))
    conn.execute("CREATE TABLE secrets (k TEXT, v TEXT)")
    conn.execute("INSERT INTO secrets VALUES ('api_key', ?)", (_MARKER.decode(),))
    conn.commit()
    conn.close()
    return str(creds_path)


def _assert_no_leak(dest):
    with zipfile.ZipFile(dest) as zf:
        for name in zf.namelist():
            assert _MARKER not in zf.read(name), f"credential marker leaked into {name}"


def test_sqlite_lumina_db_symlinked_to_credentials_rejected(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    creds_path = _make_fake_credentials(tmp_path)
    lumina_db = os.path.join(data_dir, "memory", "lumina.db")
    os.remove(lumina_db)
    os.symlink(creds_path, lumina_db)

    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError):
        _build(data_dir, base_dir, dest, credentials_path=creds_path)
    assert not os.path.exists(dest)


def test_sqlite_lumina_db_hardlinked_to_credentials_rejected(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    creds_path = _make_fake_credentials(tmp_path)
    lumina_db = os.path.join(data_dir, "memory", "lumina.db")
    os.remove(lumina_db)
    os.link(creds_path, lumina_db)

    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError):
        _build(data_dir, base_dir, dest, credentials_path=creds_path)
    assert not os.path.exists(dest)


def test_sqlite_flight_recorder_symlinked_to_credentials_rejected(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    creds_path = _make_fake_credentials(tmp_path)
    fr_db = os.path.join(data_dir, "telemetry", "flight_recorder.db")
    os.remove(fr_db)
    os.symlink(creds_path, fr_db)

    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError):
        _build(data_dir, base_dir, dest, credentials_path=creds_path)
    assert not os.path.exists(dest)


def test_sqlite_flight_recorder_hardlinked_to_credentials_rejected(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    creds_path = _make_fake_credentials(tmp_path)
    fr_db = os.path.join(data_dir, "telemetry", "flight_recorder.db")
    os.remove(fr_db)
    os.link(creds_path, fr_db)

    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError):
        _build(data_dir, base_dir, dest, credentials_path=creds_path)
    assert not os.path.exists(dest)


def test_verify_sqlite_source_identity_rejects_credentials_hardlink_directly(tmp_path):
    """Unit-level proof that the SQLite trust-boundary helper itself --
    not just the end-to-end build -- rejects a hardlink alias, without
    reading the credentials file's contents."""
    data_dir, base_dir = _make_env(tmp_path)
    creds_path = _make_fake_credentials(tmp_path)
    creds_identity = ab._resolve_credentials_identity(creds_path)
    fr_db = os.path.join(data_dir, "telemetry", "flight_recorder.db")
    os.remove(fr_db)
    os.link(creds_path, fr_db)

    ok, reason, existence_failure, identity = ab._verify_sqlite_source_identity(
        fr_db, ab._validate_root(data_dir), ab._build_forbidden_identities(creds_identity, None))
    assert ok is False
    assert "credential" in reason.lower()
    assert existence_failure is False


# =======================================================================
# Blocker 3 -- Immutable Staging Boundary / Boundary-Check-then-Open Race
# =======================================================================

def test_required_source_vanishing_between_discovery_and_staging_fails(tmp_path):
    """AGENT-BACKUP-RESTORE-A2F5: personas are captured via the atomic
    _stage_physical_state_file boundary now, not _stage_plain_file --
    the sabotage targets whichever primitive is actually in the call
    path for a required overlay member."""
    data_dir, base_dir = _make_env(tmp_path)
    target = os.path.join(base_dir, "personas", "lumina.json")

    real_stage = ab._stage_physical_state_file

    def _sabotage(source_path, *args, **kwargs):
        if source_path == target:
            os.remove(target)
        return real_stage(source_path, *args, **kwargs)

    ab._stage_physical_state_file = _sabotage
    try:
        dest = str(tmp_path / "backup.zip")
        with pytest.raises(ab.AgentBackupError):
            _build(data_dir, base_dir, dest)
        assert not os.path.exists(dest)
    finally:
        ab._stage_physical_state_file = real_stage


def test_archive_hashes_staged_bytes_not_reopened_live_bytes(tmp_path):
    """After staging, the source is mutated again before the zip is
    written -- the archived bytes must reflect what was staged, never a
    reopened, now-different live source."""
    data_dir, base_dir = _make_env(tmp_path)
    target = os.path.join(base_dir, "personas", "lumina.json")
    original_content = open(target, "rb").read()

    real_stage = ab._stage_physical_state_file
    mutated = {"done": False}

    def _mutate_after_staging(source_path, *args, **kwargs):
        result = real_stage(source_path, *args, **kwargs)
        if source_path == target and not mutated["done"]:
            with open(target, "wb") as f:
                f.write(b'{"name": "MUTATED AFTER STAGING"}')
            mutated["done"] = True
        return result

    ab._stage_physical_state_file = _mutate_after_staging
    try:
        dest = str(tmp_path / "backup.zip")
        manifest = _build(data_dir, base_dir, dest)
    finally:
        ab._stage_physical_state_file = real_stage

    with zipfile.ZipFile(dest) as zf:
        archived = zf.read("overlay/personas/lumina.json")
    assert archived == original_content, "archive contains post-staging mutated bytes, not the staged snapshot"
    assert b"MUTATED" not in archived


def test_source_replaced_with_symlink_after_staging_does_not_affect_archive(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    target = os.path.join(base_dir, "personas", "lumina.json")

    real_stage = ab._stage_physical_state_file
    replaced = {"done": False}

    def _replace_with_symlink_after(source_path, *args, **kwargs):
        result = real_stage(source_path, *args, **kwargs)
        if source_path == target and not replaced["done"]:
            os.remove(target)
            os.symlink("/etc/hostname", target)
            replaced["done"] = True
        return result

    ab._stage_physical_state_file = _replace_with_symlink_after
    try:
        dest = str(tmp_path / "backup.zip")
        manifest = _build(data_dir, base_dir, dest)
    finally:
        ab._stage_physical_state_file = real_stage

    members = _members_by_path(manifest)
    assert members["overlay/personas/lumina.json"]["content_kind"] == "json"
    with zipfile.ZipFile(dest) as zf:
        archived = zf.read("overlay/personas/lumina.json")
    assert b"Lumina" in archived


def test_open_verified_hardlink_swap_between_lstat_and_open_is_still_caught(tmp_path, monkeypatch):
    """AGENT-BACKUP-RESTORE-A2R2's original reproduction, proven directly
    at the primitive level: 'boundary check passes -> file swapped to
    hardlink of credentials -> open occurs -> credential bytes archived'.
    The swap is injected exactly between the lstat pre-filter and the
    O_NOFOLLOW open inside _open_verified -- proving the security
    decision is bound to fstat(open fd), not to a separate pre-open
    os.stat() call, since a hardlink is still a 'regular file' and would
    sail past a symlink-only defense.

    AGENT-BACKUP-RESTORE-A2R7/A2F7: every physical byte-staging call
    site (including the two, identity_trailers.json/shipped_baseline_
    hashes.json, that used to route through the lenient _open_verified/
    _stage_plain_file path via _collect_metadata_members) now uses the
    universal _stage_physical_state_file/_open_state_bearing boundary,
    which never does a separate lstat() at all -- so this specific
    lstat-vs-open race no longer has an INTEGRATION-level call site to
    demonstrate it through a full _build() call. _open_verified's own
    lstat-pre-filter-then-open shape is UNCHANGED, however -- it remains
    the pre-connect trust boundary for SQLite sources
    (_verify_sqlite_source_identity) -- so this test now calls it
    directly, the same way test_verify_sqlite_source_identity_rejects_
    credentials_hardlink_directly already tests that caller directly."""
    creds_path = _make_fake_credentials(tmp_path)
    target_path = tmp_path / "some_plain_file.json"
    target_path.write_text('{"note": "ordinary file"}')
    target = str(target_path)

    real_lstat = os.lstat
    swapped = {"done": False}

    def _sabotage_lstat(path, *a, **kw):
        result = real_lstat(path, *a, **kw)
        if path == target and not swapped["done"]:
            swapped["done"] = True
            os.remove(target)
            os.link(creds_path, target)
        return result

    monkeypatch.setattr(os, "lstat", _sabotage_lstat)
    root_real = os.path.realpath(str(tmp_path))
    forbidden = ab._build_forbidden_identities(ab._resolve_credentials_identity(creds_path), None)
    fd, reason, existence_failure = ab._open_verified(target, root_real, forbidden)
    monkeypatch.undo()

    assert swapped["done"], "the sabotage never fired -- test setup is broken, not the code"
    assert fd is None
    assert "credentials store" in reason
    assert existence_failure is False


def test_state_bearing_hardlink_swap_between_would_be_lstat_and_open_is_caught(tmp_path, monkeypatch):
    """The _open_state_bearing equivalent of the regression above: since
    this primitive never does a separate lstat() at all (Section 5/6,
    A2F5), there is no lstat call to race against open() -- proven here
    by hooking os.lstat and confirming it is simply never invoked for the
    target path, while the credentials hardlink swap (performed directly,
    with no race needed since there's nothing to race) still fails the
    WHOLE backup, per Section 9/10's escalation for state-bearing files."""
    data_dir, base_dir = _make_env(tmp_path)
    creds_path = _make_fake_credentials(tmp_path)
    target = os.path.join(base_dir, "personas", "lumina.json")
    os.remove(target)
    os.link(creds_path, target)

    real_lstat = os.lstat
    lstat_called_on_target = {"v": False}

    def _spy_lstat(path, *a, **kw):
        if path == target:
            lstat_called_on_target["v"] = True
        return real_lstat(path, *a, **kw)

    monkeypatch.setattr(os, "lstat", _spy_lstat)
    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError, match="credentials store"):
        _build(data_dir, base_dir, dest, credentials_path=creds_path)
    assert not os.path.exists(dest)
    assert lstat_called_on_target["v"] is False, (
        "the state-bearing capture boundary must never rely on a separate "
        "lstat() as its security decision -- os.open() with O_NOFOLLOW is "
        "the entire check"
    )


# =======================================================================
# AGENT-BACKUP-RESTORE-A2R6 (Section 7-12, BLOCKER): a stable, trust-
# boundary-verified, uniquely-linked descriptor does not guarantee stable
# BYTES against a concurrent same-inode writer. Every attack A2R6 used to
# defeat a size-only staleness check is reproduced directly against a
# real _build_agent_backup_core call.
# =======================================================================

def _install_mid_read_mutation(monkeypatch, target_path, mutate_fn, trigger_count=1, max_triggers=1):
    """Installs hooks so that mutate_fn(target_path) fires the
    trigger_count-th time ab._read_fd_fully_keep_open is called on a
    descriptor freshly opened against target_path -- simulating a real
    concurrent writer racing the stable-capture primitive's own
    bracketed double-read (fstat A -> read_1 [trigger_count==1 fires
    here] -> fstat B -> seek(0) -> read_2 [trigger_count==2 fires here]
    -> fstat C). max_triggers bounds how many separate _open_state_
    bearing attempts (i.e. retries, each opening a brand-new descriptor)
    this fires for: max_triggers=1 lets the bounded retry recover on its
    second attempt; max_triggers>=2 exhausts it, forcing a failed
    backup. Returns the shared state dict for assertions (e.g.
    state['triggers_fired'])."""
    real_open = os.open
    real_read_keep_open = ab._read_fd_fully_keep_open
    state = {"watched_fd": None, "reads_on_watched": 0, "triggers_fired": 0}

    def _open_hook(path, *a, **kw):
        fd = real_open(path, *a, **kw)
        if path == target_path:
            state["watched_fd"] = fd
            state["reads_on_watched"] = 0
        return fd

    def _read_hook(fd):
        data = real_read_keep_open(fd)
        if fd == state["watched_fd"]:
            state["reads_on_watched"] += 1
            if state["reads_on_watched"] == trigger_count and state["triggers_fired"] < max_triggers:
                mutate_fn(target_path)
                state["triggers_fired"] += 1
        return data

    monkeypatch.setattr(os, "open", _open_hook)
    monkeypatch.setattr(ab, "_read_fd_fully_keep_open", _read_hook)
    return state


_LARGE_DETERMINISTIC_CONTENT = b'{"marker": "ORIGINAL_VERSION", "padding": "' + b"A" * 800 + b'"}'
_LARGE_DETERMINISTIC_MUTATED = b'{"marker": "MUTATED_VERSION_", "padding": "' + b"B" * 800 + b'"}'


def _truncate_mutation(path):
    with open(path, "r+b") as f:
        f.truncate(64)


def _append_mutation(path):
    with open(path, "ab") as f:
        f.write(b"\nAPPENDED_BY_CONCURRENT_WRITER")


def _overwrite_begin_mutation(path):
    with open(path, "r+b") as f:
        f.write(b"MUTATED_AT_START_OF_FILE")


def _overwrite_middle_mutation(path):
    with open(path, "r+b") as f:
        f.seek(400)
        f.write(b"MUTATED_RIGHT_IN_THE_MIDDLE")


def _overwrite_end_mutation(path):
    size = os.path.getsize(path)
    with open(path, "r+b") as f:
        f.seek(max(0, size - 24))
        f.write(b"MUTATED_AT_THE_VERY_END")


def _size_preserving_rewrite_mutation(path):
    assert len(_LARGE_DETERMINISTIC_MUTATED) == len(_LARGE_DETERMINISTIC_CONTENT)
    with open(path, "wb") as f:
        f.write(_LARGE_DETERMINISTIC_MUTATED)


@pytest.mark.parametrize("mutate_fn", [
    _truncate_mutation, _append_mutation, _overwrite_begin_mutation,
    _overwrite_middle_mutation, _overwrite_end_mutation, _size_preserving_rewrite_mutation,
], ids=["truncate", "append", "overwrite_begin", "overwrite_middle", "overwrite_end", "size_preserving_rewrite"])
def test_persona_same_inode_mutation_race_never_produces_hybrid_bytes(tmp_path, monkeypatch, mutate_fn):
    """Every same-inode mutation shape A2R6 used to defeat a size-only
    check (including the size-PRESERVING rewrite, which changes neither
    length nor an incomplete metadata check) is reproduced here directly
    against a real build. The mutation fires mid-read, is caught, and
    recovered via ONE bounded retry from a fresh secure open -- the
    published archive must equal ONE WHOLE version exactly (confirmed
    against the actual final on-disk bytes, never a hybrid stitched from
    two different reads)."""
    data_dir, base_dir = _make_env(tmp_path)
    persona_path = os.path.join(base_dir, "personas", "lumina.json")
    with open(persona_path, "wb") as f:
        f.write(_LARGE_DETERMINISTIC_CONTENT)

    _install_mid_read_mutation(monkeypatch, persona_path, mutate_fn, trigger_count=1, max_triggers=1)

    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest)
    monkeypatch.undo()

    with zipfile.ZipFile(dest) as zf:
        archived = zf.read("overlay/personas/lumina.json")
    final_on_disk = open(persona_path, "rb").read()
    assert archived == final_on_disk, "archived bytes are not even the final, complete on-disk version"
    assert any("stable-capture retry" in w for w in manifest["warnings"]), (
        "a recovered mid-read mutation must be recorded truthfully in warnings, "
        "never silently absorbed as if nothing happened"
    )


def test_persona_repeated_mutation_exhausts_bounded_retry_and_fails_backup(tmp_path, monkeypatch):
    """Section 11: the retry is truly BOUNDED -- a persistently unstable
    source (mutated on every single attempt, never settling) must fail
    the whole backup rather than eventually accepting a torn read or
    retrying forever."""
    data_dir, base_dir = _make_env(tmp_path)
    persona_path = os.path.join(base_dir, "personas", "lumina.json")
    with open(persona_path, "wb") as f:
        f.write(_LARGE_DETERMINISTIC_CONTENT)

    state = _install_mid_read_mutation(monkeypatch, persona_path, _overwrite_middle_mutation,
                                        trigger_count=1, max_triggers=2)

    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError, match="concurrent mutation"):
        _build(data_dir, base_dir, dest)
    monkeypatch.undo()

    assert not os.path.exists(dest)
    assert state["triggers_fired"] == 2, "expected the mutation to fire on both attempts, exhausting the retry"


def test_skill_same_inode_mutation_race_recovers_via_bounded_retry(tmp_path, monkeypatch):
    data_dir, base_dir = _make_env(tmp_path)
    skill_path = os.path.join(base_dir, "skills", "rubber-duck-debugging.md")
    with open(skill_path, "wb") as f:
        f.write(_LARGE_DETERMINISTIC_CONTENT)
    _install_mid_read_mutation(monkeypatch, skill_path, _overwrite_middle_mutation, max_triggers=1)

    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest)
    monkeypatch.undo()

    with zipfile.ZipFile(dest) as zf:
        archived = zf.read("overlay/skills/rubber-duck-debugging.md")
    assert archived == open(skill_path, "rb").read()
    assert any("stable-capture retry" in w for w in manifest["warnings"])


def test_custom_tool_same_inode_mutation_race_recovers_via_bounded_retry(tmp_path, monkeypatch):
    data_dir, base_dir = _make_env(tmp_path)
    tool_path = os.path.join(data_dir, "custom_tools", "my_tool.py")
    with open(tool_path, "wb") as f:
        f.write(_LARGE_DETERMINISTIC_CONTENT)
    _install_mid_read_mutation(monkeypatch, tool_path, _truncate_mutation, max_triggers=1)

    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest)
    monkeypatch.undo()

    with zipfile.ZipFile(dest) as zf:
        archived = zf.read("state/custom_tools/my_tool.py")
    assert archived == open(tool_path, "rb").read()
    assert any("stable-capture retry" in w for w in manifest["warnings"])


def test_custom_tool_repeated_mutation_exhausts_bounded_retry_and_fails_backup(tmp_path, monkeypatch):
    data_dir, base_dir = _make_env(tmp_path)
    tool_path = os.path.join(data_dir, "custom_tools", "my_tool.py")
    with open(tool_path, "wb") as f:
        f.write(_LARGE_DETERMINISTIC_CONTENT)
    _install_mid_read_mutation(monkeypatch, tool_path, _append_mutation, trigger_count=1, max_triggers=2)

    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError, match="concurrent mutation"):
        _build(data_dir, base_dir, dest)
    monkeypatch.undo()
    assert not os.path.exists(dest)


def test_pending_actions_json_same_inode_mutation_race_recovers_via_bounded_retry(tmp_path, monkeypatch):
    data_dir, base_dir = _make_env(tmp_path)
    pending_path = os.path.join(data_dir, "memory", "pending_actions.json")
    with open(pending_path, "wb") as f:
        f.write(_LARGE_DETERMINISTIC_CONTENT)
    _install_mid_read_mutation(monkeypatch, pending_path, _overwrite_begin_mutation, max_triggers=1)

    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest)
    monkeypatch.undo()

    with zipfile.ZipFile(dest) as zf:
        archived = zf.read("state/audit/pending_actions.json")
    assert archived == open(pending_path, "rb").read()
    assert any("stable-capture retry" in w for w in manifest["warnings"])


def test_project_chats_json_same_inode_mutation_race_recovers_via_bounded_retry(tmp_path, monkeypatch):
    data_dir, base_dir = _make_env(tmp_path)
    chats_path = os.path.join(data_dir, "projects", "demo", "chats.json")
    with open(chats_path, "wb") as f:
        f.write(_LARGE_DETERMINISTIC_CONTENT)
    _install_mid_read_mutation(monkeypatch, chats_path, _overwrite_end_mutation, max_triggers=1)

    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest)
    monkeypatch.undo()

    with zipfile.ZipFile(dest) as zf:
        archived = zf.read("overlay/projects/demo/chats.json")
    assert archived == open(chats_path, "rb").read()
    assert any("stable-capture retry" in w for w in manifest["warnings"])


def test_project_binding_json_same_inode_mutation_race_recovers_via_bounded_retry(tmp_path, monkeypatch):
    data_dir, base_dir = _make_env(tmp_path)
    binding_path = os.path.join(data_dir, "projects", "demo", "binding.json")
    with open(binding_path, "wb") as f:
        f.write(_LARGE_DETERMINISTIC_CONTENT)
    _install_mid_read_mutation(monkeypatch, binding_path, _append_mutation, max_triggers=1)

    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest)
    monkeypatch.undo()

    with zipfile.ZipFile(dest) as zf:
        archived = zf.read("overlay/projects/demo/binding.json")
    assert archived == open(binding_path, "rb").read()
    assert any("stable-capture retry" in w for w in manifest["warnings"])


def test_prefs_json_same_inode_size_preserving_mutation_race_recovers(tmp_path, monkeypatch):
    """prefs.json content is independently JSON-validated at verify time
    (_verify_prefs_payload) -- the size-preserving rewrite mutation is
    used deliberately here since both its 'before' and 'after' states
    are valid JSON, isolating the stable-capture behavior under test from
    an unrelated JSON-validity failure a truncation would also trigger."""
    data_dir, base_dir = _make_env(tmp_path)
    prefs_path = os.path.join(data_dir, "memory", "prefs.json")
    with open(prefs_path, "wb") as f:
        f.write(_LARGE_DETERMINISTIC_CONTENT)
    _install_mid_read_mutation(monkeypatch, prefs_path, _size_preserving_rewrite_mutation, max_triggers=1)

    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest)
    monkeypatch.undo()

    with zipfile.ZipFile(dest) as zf:
        archived = zf.read("state/preferences/prefs.json")
    assert archived == open(prefs_path, "rb").read()
    assert json.loads(archived)  # still independently valid JSON, never a torn hybrid
    assert any("stable-capture retry" in w for w in manifest["warnings"])


def test_stable_capture_never_applied_to_sqlite_sources():
    """Section 10: the stable-capture primitive must never be layered
    onto SQLite sources -- they already have their own point-in-time-
    consistent snapshot mechanism (the online backup API,
    _snapshot_sqlite, independently cleared by prior passes and
    untouched by A2F6). Static proof that _snapshot_sqlite's own source
    never references the new primitive at all -- not a claim about one
    particular build's runtime behavior, but that the code path
    physically cannot call it."""
    source = inspect.getsource(ab._snapshot_sqlite)
    assert "_read_stable_bytes_or_none" not in source
    assert "_open_state_bearing" not in source


def test_staged_sqlite_file_corrupted_after_capture_does_not_affect_archive(tmp_path, monkeypatch):
    """AGENT-BACKUP-RESTORE-A2R9 (Section 8-10, BLOCKER B1, superseding
    the identically-named-in-spirit AGENT-BACKUP-RESTORE-A2R2 test this
    replaces): A2R9 proved live that the OLD pipeline reopened
    staged_path by name multiple times after _snapshot_sqlite returned
    (_sqlite_metadata(staged), _sha256_file(staged), zf.write(staged)),
    so corrupting the on-disk staged file AFTER _snapshot_sqlite's own
    internal integrity_check (but before those later reopens) WAS able to
    reach the archive -- which is exactly why the old version of this
    test asserted the build must fail in that scenario.

    A2F9 closes that reopen entirely: _snapshot_sqlite now captures the
    exact checkpointed bytes via serialize() on the SAME live connection,
    synchronously, before it ever returns -- staged_path becomes
    transport/cache only from that instant on, and nothing downstream
    ever rereads it. This test proves the architectural fix directly: a
    same-inode corruption of staged_path AFTER _snapshot_sqlite has
    already returned must have ZERO effect on the archive -- the build
    must succeed, and the archived database must be byte-identical to the
    pre-corruption committed state, not merely 'still fail safely.'"""
    data_dir, base_dir = _make_env(tmp_path)
    real_snapshot = ab._snapshot_sqlite
    corrupted = {"done": False}

    def _corrupt_after_snapshot(source_path, staged_path, *args, **kwargs):
        data = real_snapshot(source_path, staged_path, *args, **kwargs)
        if source_path.endswith("lumina.db") and not corrupted["done"]:
            corrupted["done"] = True
            with open(staged_path, "r+b") as f:
                f.seek(100)
                f.write(b"\xff" * 40)
        return data

    monkeypatch.setattr(ab, "_snapshot_sqlite", _corrupt_after_snapshot)
    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest)
    monkeypatch.undo()

    assert corrupted["done"], "the sabotage never fired -- test setup is broken, not the code"
    report = ab.verify_agent_backup(dest)
    assert report["valid"], report["errors"]
    with zipfile.ZipFile(dest) as zf:
        archived = zf.read("state/databases/lumina.db")
    conn = sqlite3.connect(":memory:")
    conn.deserialize(archived)
    assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    conn.close()


def test_snapshot_captured_data_matches_disk_before_post_capture_corruption(tmp_path):
    """Source-vetting regression for the serialize()-based capture
    mechanism itself (independent of the full build pipeline above):
    _snapshot_sqlite's returned bytes must be byte-identical to what was
    actually on disk at staged_path the instant it returned, proving
    serialize() is a faithful capture and not some other in-memory
    representation that happens to also pass integrity_check."""
    data_dir, base_dir = _make_env(tmp_path)
    lumina_db = os.path.join(data_dir, "memory", "lumina.db")
    staged = str(tmp_path / "staged_capture_check.db")
    data = ab._snapshot_sqlite(lumina_db, staged, timeout_seconds=10)
    with open(staged, "rb") as f:
        disk_bytes_immediately_after = f.read()
    assert data == disk_bytes_immediately_after


# =======================================================================
# Blocker 5/6 -- Canonical Required-State Inventory
# =======================================================================

def test_zero_member_backup_is_structurally_impossible(tmp_path):
    """A2R produced a zero-member backup that verified as valid. The
    primary-database presence check makes this impossible to reach via
    the normal build path; this test proves the floor exists even if a
    future refactor weakens collection."""
    data_dir, base_dir = _make_env(tmp_path)
    os.remove(os.path.join(data_dir, "memory", "lumina.db"))
    os.remove(os.path.join(data_dir, "memory", "prefs.json"))
    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError):
        _build(data_dir, base_dir, dest)


def test_directory_enumeration_failure_escalates_not_treated_as_empty(tmp_path):
    """'custom_tools directory exists and traversal failed' must never be
    silently treated as 'no custom tools'. Uses a genuinely unreadable
    subdirectory (real permission error through the real code path, not a
    mocked substitute) so this exercises _safe_walk's actual onerror
    wrapping, not a stand-in for it."""
    if os.geteuid() == 0:
        pytest.skip("running as root -- permission bits don't block access")
    data_dir, base_dir = _make_env(tmp_path)
    blocked = os.path.join(data_dir, "custom_tools", "blocked_dir")
    os.makedirs(blocked)
    os.chmod(blocked, 0o000)
    try:
        dest = str(tmp_path / "backup.zip")
        with pytest.raises(ab.AgentBackupError):
            _build(data_dir, base_dir, dest)
    finally:
        os.chmod(blocked, 0o755)


def test_custom_tools_root_itself_unreadable_escalates(tmp_path):
    """AGENT-BACKUP-RESTORE-A2R2: 'custom-tools root becomes unreadable
    -> treated as absent'. Chmod's the ROOT directory itself (not a
    subdirectory) to 0 -- os.walk's own onerror wrapping must still
    escalate for the top-level scandir failure, not just for
    subdirectories."""
    if os.geteuid() == 0:
        pytest.skip("running as root -- permission bits don't block access")
    data_dir, base_dir = _make_env(tmp_path)
    custom_tools_dir = os.path.join(data_dir, "custom_tools")
    os.chmod(custom_tools_dir, 0o000)
    try:
        dest = str(tmp_path / "backup.zip")
        with pytest.raises(ab.AgentBackupError):
            _build(data_dir, base_dir, dest)
    finally:
        os.chmod(custom_tools_dir, 0o755)


def test_classify_root_distinguishes_enoent_from_genuine_unreadability(tmp_path):
    """Unit-level proof of the fix underneath the integration tests above:
    _classify_root must not conflate 'doesn't exist' (a valid absence)
    with 'exists but lstat failed for another reason' (a real failure a
    caller must escalate for required-capable categories) -- the OLD
    _validate_root's blanket `except OSError: return None` couldn't tell
    these apart at all."""
    missing = os.path.join(str(tmp_path), "does_not_exist_at_all")
    real, exists, reason = ab._classify_root(missing)
    assert real is None and exists is False

    if os.geteuid() != 0:
        parent = tmp_path / "unreadable_parent"
        parent.mkdir()
        target = parent / "target_dir"
        target.mkdir()
        os.chmod(str(parent), 0o000)
        try:
            real, exists, reason = ab._classify_root(str(target))
            assert real is None
            assert exists is True
            assert reason.startswith("lstat failed")
        finally:
            os.chmod(str(parent), 0o755)


def test_listdir_failure_on_overlay_dir_escalates(tmp_path, monkeypatch):
    data_dir, base_dir = _make_env(tmp_path)

    real_listdir = ab._safe_listdir

    def _boom(dir_path):
        if dir_path.endswith("personas"):
            raise ab.AgentBackupError(f"failed to enumerate directory {dir_path}: simulated I/O error")
        return real_listdir(dir_path)

    monkeypatch.setattr(ab, "_safe_listdir", _boom)
    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError):
        _build(data_dir, base_dir, dest)


def test_persona_disappearing_between_listdir_and_lstat_fails(tmp_path, monkeypatch):
    """AGENT-BACKUP-RESTORE-A2R2: 'persona disappears after enumeration
    -> successful archive'. The old _collect_overlay_dir swallowed the
    per-file lstat's OSError with a silent `continue`, which is a
    DIFFERENT (and until now unfixed) gap than the file vanishing inside
    _stage_plain_file itself (already covered above) -- this sabotages
    exactly that window, between _safe_listdir returning the name and
    the per-file lstat that follows it."""
    data_dir, base_dir = _make_env(tmp_path)
    target = os.path.join(base_dir, "personas", "lumina.json")
    real_listdir = ab._safe_listdir

    def _sabotage(dir_path):
        names = real_listdir(dir_path)
        if dir_path.endswith("personas") and "lumina.json" in names:
            os.remove(target)
        return names

    monkeypatch.setattr(ab, "_safe_listdir", _sabotage)
    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError):
        _build(data_dir, base_dir, dest)
    assert not os.path.exists(dest)


def test_required_custom_tool_disappearing_during_staging_fails(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    target = os.path.join(data_dir, "custom_tools", "my_tool.py")

    real_stage = ab._stage_physical_state_file

    def _sabotage(source_path, *args, **kwargs):
        if source_path == target:
            os.remove(target)
        return real_stage(source_path, *args, **kwargs)

    ab._stage_physical_state_file = _sabotage
    try:
        dest = str(tmp_path / "backup.zip")
        with pytest.raises(ab.AgentBackupError):
            _build(data_dir, base_dir, dest)
        assert not os.path.exists(dest)
    finally:
        ab._stage_physical_state_file = real_stage


def test_absent_custom_tools_directory_is_valid_not_an_error(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    import shutil
    shutil.rmtree(os.path.join(data_dir, "custom_tools"))
    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest)  # must not raise
    assert not any(m["archive_path"].startswith("state/custom_tools/") for m in manifest["members"])


def test_data_only_project_binding_and_chats_captured_without_base_dir_project(tmp_path):
    """Section 6: a project with binding.json/chats.json under
    DATA_DIR/projects/<name> but NO matching BASE_DIR/projects/<name>/
    directory must still be discovered -- Project state is a logical
    identity spanning both roots, not something one physical root must
    exist to unlock the other."""
    data_dir, base_dir = _make_env(tmp_path)
    data_only_dir = os.path.join(data_dir, "projects", "data_only_project")
    os.makedirs(data_only_dir)
    with open(os.path.join(data_only_dir, "binding.json"), "w") as f:
        json.dump({"root": "/tmp/elsewhere"}, f)
    with open(os.path.join(data_only_dir, "chats.json"), "w") as f:
        json.dump([{"chat_id": 7}], f)

    assert not os.path.isdir(os.path.join(base_dir, "projects", "data_only_project"))

    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest)
    members = _members_by_path(manifest)
    assert "overlay/projects/data_only_project/binding.json" in members
    assert "overlay/projects/data_only_project/chats.json" in members
    assert members["overlay/projects/data_only_project/binding.json"]["state_class"] == "REBIND_REQUIRED"
    assert members["overlay/projects/data_only_project/chats.json"]["state_class"] == "REQUIRED_AGENT_STATE"


# =======================================================================
# Blocker 4/8 -- Credential and Filesystem Boundary (plain files)
# =======================================================================

def test_persona_symlink_to_credentials_fails_whole_backup(tmp_path):
    """AGENT-BACKUP-RESTORE-A2R4 (Section 5): superseded from A2F3's own
    'excluded with a warning, rest of the backup still succeeds' -- a
    symlink discovered at a state-bearing position (here, anything
    matching the personas category's own *.json filter) now fails the
    WHOLE backup, exactly like a FIFO/socket would, regardless of what it
    points at. The old warn-and-exclude-just-this-file outcome is no
    longer correct: 'the backup cannot truthfully claim complete recovery
    while deliberately omitting current durable state.'"""
    data_dir, base_dir = _make_env(tmp_path)
    creds_path = _make_fake_credentials(tmp_path)
    evil = os.path.join(base_dir, "personas", "evil_symlink.json")
    os.symlink(creds_path, evil)

    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError, match="SECURITY_REJECTED_STATE|symlink"):
        _build(data_dir, base_dir, dest, credentials_path=creds_path)
    assert not os.path.exists(dest)


def test_state_bearing_hardlink_to_credentials_fails_whole_backup(tmp_path):
    """AGENT-BACKUP-RESTORE-A2R5 (Section 9/10, MEDIUM->release-gating):
    superseded from A2F4's own 'excluded with a warning, rest of the
    backup still succeeds' -- a REQUIRED, state-bearing position (here,
    anything matching the personas category's own *.json filter)
    hardlinked to the canonical credentials store now fails the WHOLE
    backup via the descriptor-bound forbidden-identity check inside
    _open_state_bearing, exactly like a symlink at the same position
    already does (A2F4) and a ledger.db hardlink now also does (below).
    'Never: warn / rename logically / archive under another path'
    (Section 10) -- credentials bytes must never even be considered for
    exclusion-with-warning at a state-bearing position."""
    data_dir, base_dir = _make_env(tmp_path)
    creds_path = _make_fake_credentials(tmp_path)
    evil = os.path.join(base_dir, "personas", "evil_hardlink.json")
    os.link(creds_path, evil)

    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError, match="credentials store"):
        _build(data_dir, base_dir, dest, credentials_path=creds_path)
    assert not os.path.exists(dest)


def test_optional_metadata_hardlink_to_credentials_now_fails_whole_backup(tmp_path):
    """AGENT-BACKUP-RESTORE-A2R7 (Section 2-4, BLOCKER) SUPERSEDES the
    prior 'lenient boundary for optional members' behavior this test
    used to assert (see git history for the old _stage_plain_file-era
    version). identity_trailers.json is USER_OVERLAY/required:false --
    its ABSENCE is still fine, never escalated -- but A2R7 found that
    'optional' had been conflated with 'gets a weaker security boundary
    when present,' letting a credentials hardlink at this exact position
    be silently excluded-with-a-warning rather than failing the backup.
    It now goes through the SAME universal _stage_physical_state_file
    boundary as every required member: a credentials hardlink here fails
    the WHOLE backup, identically to a persona hardlink."""
    data_dir, base_dir = _make_env(tmp_path)
    creds_path = _make_fake_credentials(tmp_path)
    trailers_path = tmp_path / "identity_trailers.json"
    os.link(creds_path, trailers_path)

    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError, match="credentials store"):
        _build(data_dir, base_dir, dest, credentials_path=creds_path,
               identity_trailers_path=str(trailers_path))
    assert not os.path.exists(dest)


# =======================================================================
# AGENT-BACKUP-RESTORE-A2R5 (Section 9, MEDIUM->release-gating): ledger.db
# joins the forbidden-identity set -- a hardlink alias to it must be
# rejected exactly like credentials, for every required-state collector.
# =======================================================================

def _leak_marker_ledger(data_dir):
    """Writes a real ledger.db (matching core/idempotency.py's own
    LEDGER_PATH: DATA_DIR/memory/ledger.db) containing a distinctive
    marker row, and returns its path."""
    ledger_path = os.path.join(data_dir, "memory", "ledger.db")
    if os.path.exists(ledger_path):
        os.remove(ledger_path)
    conn = sqlite3.connect(ledger_path)
    conn.execute("CREATE TABLE ledger (request_id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO ledger VALUES ('LEDGER_EXFIL_MARKER')")
    conn.commit()
    conn.close()
    return ledger_path


def test_persona_hardlinked_to_ledger_fails_whole_backup_no_leak(tmp_path):
    """The literal AGENT-BACKUP-RESTORE-A2R5 reproduction: a persona
    filename hardlinked to ledger.db's inode used to leak ledger's real
    rows into the archive under the persona's own archive_path, since
    only credentials_identity was ever checked. Now rejected exactly
    like a credentials alias, and no ledger content ever reaches the
    archive."""
    data_dir, base_dir = _make_env(tmp_path)
    ledger_path = _leak_marker_ledger(data_dir)
    evil = os.path.join(base_dir, "personas", "evil_ledger.json")
    os.link(ledger_path, evil)

    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError, match="ledger"):
        _build(data_dir, base_dir, dest)
    assert not os.path.exists(dest)


def test_custom_tool_hardlinked_to_ledger_fails_whole_backup(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    ledger_path = _leak_marker_ledger(data_dir)
    tool = os.path.join(data_dir, "custom_tools", "my_tool.py")
    os.remove(tool)
    os.link(ledger_path, tool)

    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError, match="ledger"):
        _build(data_dir, base_dir, dest)
    assert not os.path.exists(dest)


def test_tool_audit_log_hardlinked_to_ledger_fails_whole_backup(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    ledger_path = _leak_marker_ledger(data_dir)
    audit = os.path.join(data_dir, "memory", "tool_audit.log")
    os.remove(audit)
    os.link(ledger_path, audit)

    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError, match="ledger"):
        _build(data_dir, base_dir, dest)
    assert not os.path.exists(dest)


def test_project_binding_json_hardlinked_to_ledger_fails_whole_backup(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    ledger_path = _leak_marker_ledger(data_dir)
    binding = os.path.join(data_dir, "projects", "demo", "binding.json")
    os.remove(binding)
    os.link(ledger_path, binding)

    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError, match="ledger"):
        _build(data_dir, base_dir, dest)
    assert not os.path.exists(dest)


def test_project_chats_json_hardlinked_to_ledger_fails_whole_backup(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    ledger_path = _leak_marker_ledger(data_dir)
    chats = os.path.join(data_dir, "projects", "demo", "chats.json")
    os.remove(chats)
    os.link(ledger_path, chats)

    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError, match="ledger"):
        _build(data_dir, base_dir, dest)
    assert not os.path.exists(dest)


def test_ordinary_duplicate_agent_state_hardlink_now_fails_closed(tmp_path):
    """AGENT-BACKUP-RESTORE-A2R6 (Section 1-3, BLOCKER) SUPERSEDES A2F5's
    own Section 11 'ordinary duplicate Agent state -> captured
    independently, neither forbidden nor deduplicated' policy (see git
    history / AGENT_BACKUP_RESTORE_A2F5_IMPLEMENTATION_NOTE_2026-09-09.md
    for what this test used to assert). A2R6 proved that policy was the
    same structural mistake as a forbidden-identity blacklist: a hardlink
    to an ORDINARY state file today can be re-pointed, tomorrow, at
    credentials/ledger.db (or any future excluded source) by simply
    replacing what the OTHER name refers to -- this module can never
    prove, from either name alone, that a hardlink will stay 'ordinary'
    forever. v1's tightened answer: a state-bearing file may never be a
    hardlink AT ALL, regardless of what its other name currently points
    to -- 'ordinary' and 'forbidden' get exactly the same fail-closed
    outcome now, closing the alias class instead of trying to keep
    classifying its members."""
    data_dir, base_dir = _make_env(tmp_path)
    persona_path = os.path.join(base_dir, "personas", "lumina.json")
    twin_path = os.path.join(base_dir, "personas", "lumina_twin.json")
    os.link(persona_path, twin_path)

    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError, match="hardlink"):
        _build(data_dir, base_dir, dest)
    assert not os.path.exists(dest)


def _unrelated_file(tmp_path, name="unrelated_ordinary_file.txt"):
    """A file with NO relationship to credentials, ledger.db, or any
    other Agent state position -- used to prove the v1 hardlink policy
    (Section 5) fires regardless of what the OTHER name points to, not
    only for the specific forbidden identities this module already knows
    about by name."""
    p = tmp_path / name
    p.write_text("nothing sensitive here, just an ordinary unrelated file\n")
    return str(p)


@pytest.mark.parametrize("relpath", [
    os.path.join("skills", "evil_hardlinked_skill.md"),
    os.path.join("tool_profiles", "evil_hardlinked_profile.json"),
    os.path.join("assets", "avatars", "evil_hardlinked_avatar.png"),
    os.path.join("assets", "voices", "evil_hardlinked_voice.wav"),
])
def test_new_overlay_member_hardlinked_to_ordinary_file_fails_whole_backup(tmp_path, relpath):
    """Section 5's hardlink matrix, generalized across every flat overlay
    family sharing _collect_overlay_dir's own _stage_physical_state_file
    call -- a hardlink to an ORDINARY, unrelated file (never credentials,
    never ledger.db) still fails closed, proving the fix is 'a
    state-bearing file may never be a hardlink,' not merely a wider
    forbidden-identity blacklist."""
    data_dir, base_dir = _make_env(tmp_path)
    unrelated = _unrelated_file(tmp_path)
    target = os.path.join(base_dir, relpath)
    os.link(unrelated, target)

    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError, match="hardlink"):
        _build(data_dir, base_dir, dest)
    assert not os.path.exists(dest)


def test_custom_tool_hardlinked_to_ordinary_file_fails_whole_backup(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    unrelated = _unrelated_file(tmp_path)
    tool = os.path.join(data_dir, "custom_tools", "my_tool.py")
    os.remove(tool)
    os.link(unrelated, tool)

    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError, match="hardlink"):
        _build(data_dir, base_dir, dest)
    assert not os.path.exists(dest)


def test_tool_audit_log_hardlinked_to_ordinary_file_fails_whole_backup(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    unrelated = _unrelated_file(tmp_path)
    audit = os.path.join(data_dir, "memory", "tool_audit.log")
    os.remove(audit)
    os.link(unrelated, audit)

    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError, match="hardlink"):
        _build(data_dir, base_dir, dest)
    assert not os.path.exists(dest)


def test_pending_actions_json_hardlinked_to_ordinary_file_fails_whole_backup(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    unrelated = _unrelated_file(tmp_path)
    pending = os.path.join(data_dir, "memory", "pending_actions.json")
    os.remove(pending)
    os.link(unrelated, pending)

    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError, match="hardlink"):
        _build(data_dir, base_dir, dest)
    assert not os.path.exists(dest)


def test_project_binding_json_hardlinked_to_ordinary_file_fails_whole_backup(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    unrelated = _unrelated_file(tmp_path)
    binding = os.path.join(data_dir, "projects", "demo", "binding.json")
    os.remove(binding)
    os.link(unrelated, binding)

    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError, match="hardlink"):
        _build(data_dir, base_dir, dest)
    assert not os.path.exists(dest)


def test_project_chats_json_hardlinked_to_ordinary_file_fails_whole_backup(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    unrelated = _unrelated_file(tmp_path)
    chats = os.path.join(data_dir, "projects", "demo", "chats.json")
    os.remove(chats)
    os.link(unrelated, chats)

    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError, match="hardlink"):
        _build(data_dir, base_dir, dest)
    assert not os.path.exists(dest)


def test_prefs_json_hardlinked_to_ordinary_file_fails_whole_backup(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    unrelated = _unrelated_file(tmp_path)
    prefs = os.path.join(data_dir, "memory", "prefs.json")
    os.remove(prefs)
    os.link(unrelated, prefs)

    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError, match="hardlink"):
        _build(data_dir, base_dir, dest)
    assert not os.path.exists(dest)


def test_projectlist_md_hardlinked_to_ordinary_file_fails_whole_backup(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    unrelated = _unrelated_file(tmp_path)
    projectlist = os.path.join(base_dir, "projects", "projectlist.md")
    os.remove(projectlist)
    os.link(unrelated, projectlist)

    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError, match="hardlink"):
        _build(data_dir, base_dir, dest)
    assert not os.path.exists(dest)


def test_state_bearing_hardlink_open_returns_hardlink_kind_directly(tmp_path):
    """Direct unit-level proof of _open_state_bearing's own new outcome,
    independent of the whole-backup wiring above -- st_nlink > 1 on an
    otherwise perfectly ordinary regular file (not a forbidden identity,
    not a symlink, not a shape violation) is reported as kind='hardlink',
    never 'ok', and never returns a live fd."""
    data_dir, base_dir = _make_env(tmp_path)
    unrelated = _unrelated_file(tmp_path)
    target = os.path.join(base_dir, "personas", "hardlinked_direct.json")
    os.link(unrelated, target)
    root_real = os.path.realpath(os.path.join(base_dir, "personas"))

    fd, reason, kind = ab._open_state_bearing(target, root_real, {"canonical credentials store": None})
    assert fd is None
    assert kind == "hardlink"
    assert "hardlink" in reason
    assert "st_nlink=2" in reason


# =======================================================================
# AGENT-BACKUP-RESTORE-A2R7 (Section 2-5, BLOCKER): the three physical
# members that used to route through the retired lenient _stage_plain_
# file boundary -- Project codebase.md, identity_trailers.json, shipped_
# baseline_hashes.json -- now go through the SAME universal
# _stage_physical_state_file boundary as every required member. Every
# attack already proven against personas/skills/etc. is replayed here
# against all three, directly, to independently confirm the migration.
# =======================================================================

def _rotate_file_to_new_inode(path: str, new_content: bytes) -> None:
    """Replaces path's CONTENT via a brand-new inode (os.replace over a
    freshly-written sibling file) -- simulating a genuine credential/
    ledger rotation event, never merely truncating/rewriting the same
    inode in place (which would NOT change its identity)."""
    tmp = path + ".rotated_new"
    with open(tmp, "wb") as f:
        f.write(new_content)
    os.replace(tmp, path)


def test_codebase_md_hardlinked_to_ordinary_file_fails_whole_backup(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    unrelated = _unrelated_file(tmp_path)
    target = os.path.join(base_dir, "projects", "demo", "codebase.md")
    os.remove(target)
    os.link(unrelated, target)
    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError, match="hardlink"):
        _build(data_dir, base_dir, dest)
    assert not os.path.exists(dest)


def test_codebase_md_hardlinked_to_credentials_fails_whole_backup(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    creds_path = _make_fake_credentials(tmp_path)
    target = os.path.join(base_dir, "projects", "demo", "codebase.md")
    os.remove(target)
    os.link(creds_path, target)
    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError, match="credentials store"):
        _build(data_dir, base_dir, dest, credentials_path=creds_path)
    assert not os.path.exists(dest)


def test_codebase_md_hardlinked_to_ledger_fails_whole_backup(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    ledger_path = _leak_marker_ledger(data_dir)
    target = os.path.join(base_dir, "projects", "demo", "codebase.md")
    os.remove(target)
    os.link(ledger_path, target)
    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError, match="ledger"):
        _build(data_dir, base_dir, dest)
    assert not os.path.exists(dest)


def test_codebase_md_hardlinked_to_rotated_credentials_fails_whole_backup(tmp_path):
    """Section 8: the credentials store is rotated to a genuinely NEW
    inode AFTER this test's own setup begins, then hardlinked -- proving
    the fix (categorical anti-hardlink, not an identity blacklist) holds
    for this newly-migrated position regardless of when the credentials
    inode came into existence."""
    data_dir, base_dir = _make_env(tmp_path)
    creds_path = _make_fake_credentials(tmp_path)
    _rotate_file_to_new_inode(creds_path, b'{"api_key": "ROTATED_SECRET_MUST_NEVER_LEAK"}')
    target = os.path.join(base_dir, "projects", "demo", "codebase.md")
    os.remove(target)
    os.link(creds_path, target)
    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError, match="hardlink|credentials store"):
        _build(data_dir, base_dir, dest, credentials_path=creds_path)
    assert not os.path.exists(dest)


def test_codebase_md_same_inode_mutation_race_recovers_via_bounded_retry(tmp_path, monkeypatch):
    data_dir, base_dir = _make_env(tmp_path)
    target = os.path.join(base_dir, "projects", "demo", "codebase.md")
    with open(target, "wb") as f:
        f.write(_LARGE_DETERMINISTIC_CONTENT)
    _install_mid_read_mutation(monkeypatch, target, _overwrite_middle_mutation, max_triggers=1)
    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest)
    monkeypatch.undo()
    with zipfile.ZipFile(dest) as zf:
        archived = zf.read("overlay/projects/demo/codebase.md")
    assert archived == open(target, "rb").read()
    assert any("stable-capture retry" in w for w in manifest["warnings"])


def _identity_trailers_env(tmp_path):
    trailers_path = tmp_path / "identity_trailers.json"
    trailers_path.write_text(json.dumps({"note": "engineering metadata"}))
    return str(trailers_path)


def test_identity_trailers_hardlinked_to_ordinary_file_fails_whole_backup(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    unrelated = _unrelated_file(tmp_path)
    target = _identity_trailers_env(tmp_path)
    os.remove(target)
    os.link(unrelated, target)
    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError, match="hardlink"):
        _build(data_dir, base_dir, dest, identity_trailers_path=target)
    assert not os.path.exists(dest)


def test_identity_trailers_hardlinked_to_ledger_fails_whole_backup(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    ledger_path = _leak_marker_ledger(data_dir)
    target = _identity_trailers_env(tmp_path)
    os.remove(target)
    os.link(ledger_path, target)
    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError, match="ledger"):
        _build(data_dir, base_dir, dest, identity_trailers_path=target)
    assert not os.path.exists(dest)


def test_identity_trailers_hardlinked_to_rotated_ledger_fails_whole_backup(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    ledger_path = _leak_marker_ledger(data_dir)
    # Rotate ledger.db to a genuine, distinct NEW inode (a fresh real SQLite file).
    rotated = str(tmp_path / "rotated_ledger.db")
    conn = sqlite3.connect(rotated)
    conn.execute("CREATE TABLE ledger (request_id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO ledger VALUES ('ROTATED_LEDGER_MARKER')")
    conn.commit()
    conn.close()
    with open(rotated, "rb") as f:
        rotated_bytes = f.read()
    _rotate_file_to_new_inode(ledger_path, rotated_bytes)

    target = _identity_trailers_env(tmp_path)
    os.remove(target)
    os.link(ledger_path, target)
    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError, match="hardlink|ledger"):
        _build(data_dir, base_dir, dest, identity_trailers_path=target)
    assert not os.path.exists(dest)


def test_identity_trailers_same_inode_mutation_race_recovers_via_bounded_retry(tmp_path, monkeypatch):
    data_dir, base_dir = _make_env(tmp_path)
    target_path = tmp_path / "identity_trailers.json"
    with open(target_path, "wb") as f:
        f.write(_LARGE_DETERMINISTIC_CONTENT)
    target = str(target_path)
    _install_mid_read_mutation(monkeypatch, target, _truncate_mutation, max_triggers=1)
    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest, identity_trailers_path=target)
    monkeypatch.undo()
    with zipfile.ZipFile(dest) as zf:
        archived = zf.read("metadata/identity_trailers.json")
    assert archived == open(target, "rb").read()
    assert any("stable-capture retry" in w for w in manifest["warnings"])


def _shipped_baseline_hashes_env(tmp_path):
    p = tmp_path / "shipped_baseline_hashes.json"
    p.write_text(json.dumps({
        "format": ab.BASELINE_FORMAT, "format_version": ab.BASELINE_FORMAT_VERSION,
        "lumina_version": "unknown", "git_commit": "0" * 40,
        "hash_algorithm": "sha256", "files": {},
    }))
    return str(p)


def test_shipped_baseline_hashes_hardlinked_to_ordinary_file_fails_whole_backup(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    unrelated = _unrelated_file(tmp_path)
    target = _shipped_baseline_hashes_env(tmp_path)
    os.remove(target)
    os.link(unrelated, target)
    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError, match="hardlink"):
        _build(data_dir, base_dir, dest, baseline_hashes_path=target)
    assert not os.path.exists(dest)


def test_shipped_baseline_hashes_hardlinked_to_credentials_fails_whole_backup(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    creds_path = _make_fake_credentials(tmp_path)
    target = _shipped_baseline_hashes_env(tmp_path)
    os.remove(target)
    os.link(creds_path, target)
    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError, match="credentials store"):
        _build(data_dir, base_dir, dest, credentials_path=creds_path, baseline_hashes_path=target)
    assert not os.path.exists(dest)


def test_shipped_baseline_hashes_hardlinked_to_rotated_credentials_fails_whole_backup(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    creds_path = _make_fake_credentials(tmp_path)
    _rotate_file_to_new_inode(creds_path, b'{"api_key": "ROTATED_SECRET_MUST_NEVER_LEAK"}')
    target = _shipped_baseline_hashes_env(tmp_path)
    os.remove(target)
    os.link(creds_path, target)
    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError, match="hardlink|credentials store"):
        _build(data_dir, base_dir, dest, credentials_path=creds_path, baseline_hashes_path=target)
    assert not os.path.exists(dest)


def test_shipped_baseline_hashes_same_inode_mutation_race_recovers_via_bounded_retry(tmp_path, monkeypatch):
    data_dir, base_dir = _make_env(tmp_path)
    target_path = tmp_path / "shipped_baseline_hashes.json"
    with open(target_path, "wb") as f:
        f.write(_LARGE_DETERMINISTIC_CONTENT)
    target = str(target_path)
    _install_mid_read_mutation(monkeypatch, target, _append_mutation, max_triggers=1)
    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest, baseline_hashes_path=target)
    monkeypatch.undo()
    with zipfile.ZipFile(dest) as zf:
        archived = zf.read("metadata/shipped_baseline_hashes.json")
    assert archived == open(target, "rb").read()
    assert any("stable-capture retry" in w for w in manifest["warnings"])


def test_directory_symlink_escape_excluded_not_leaked(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    sensitive = tmp_path / "sensitive_elsewhere"
    sensitive.mkdir()
    (sensitive / "secret_plans.md").write_text("do not archive this")
    import shutil
    shutil.rmtree(os.path.join(base_dir, "skills"))  # replace the real dir with a symlink
    os.symlink(str(sensitive), os.path.join(base_dir, "skills"))

    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest)
    assert not any("secret_plans" in m["archive_path"] for m in manifest["members"])
    with zipfile.ZipFile(dest) as zf:
        assert not any("secret_plans" in n for n in zf.namelist())


def test_symlinked_subdirectory_inside_custom_tools_not_traversed(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    sensitive = tmp_path / "sensitive_tools"
    sensitive.mkdir()
    (sensitive / "evil.py").write_text("# should never be archived\n")
    os.symlink(str(sensitive), os.path.join(data_dir, "custom_tools", "escape_link"))

    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest)
    tool_paths = [m["archive_path"] for m in manifest["members"] if "custom_tools" in m["archive_path"]]
    assert not any("evil" in p or "escape_link" in p for p in tool_paths)
    assert any(p.endswith("my_tool.py") for p in tool_paths)


def test_state_bearing_credentials_hardlink_does_not_leak_via_persisted_partial_state(tmp_path):
    """AGENT-BACKUP-RESTORE-A2R5: a hardlink alias to credentials is NOT
    a symlink -- os.lstat()/fstat() reports a genuine regular file -- so
    it is caught by the SAME descriptor-bound identity check
    _open_state_bearing already performs for the shape check, not a
    separate mechanism. Confirms the whole backup fails closed (no
    partial archive, no credentials bytes anywhere) rather than the
    pre-A2R5 'excludes just this one file, backup still succeeds'
    outcome, which is no longer correct for a state-bearing position."""
    data_dir, base_dir = _make_env(tmp_path)
    creds_path = _make_fake_credentials(tmp_path)
    os.link(creds_path, os.path.join(base_dir, "personas", "evil_hardlink2.json"))

    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError, match="credentials store"):
        _build(data_dir, base_dir, dest, credentials_path=creds_path)
    assert not os.path.exists(dest)


# =======================================================================
# Blocker 5/9 -- Safe Destination / Publication Order (unchanged, locked in)
# =======================================================================

def test_destination_inside_data_dir_rejected(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    dest = os.path.join(data_dir, "sneaky.zip")
    with pytest.raises(ab.AgentBackupError, match="DATA_DIR"):
        _build(data_dir, base_dir, dest)
    assert not os.path.exists(dest)


def test_destination_inside_base_dir_rejected(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    dest = os.path.join(base_dir, "sneaky.zip")
    with pytest.raises(ab.AgentBackupError, match="BASE_DIR"):
        _build(data_dir, base_dir, dest)
    assert not os.path.exists(dest)


def test_destination_equal_to_existing_persona_file_rejected(tmp_path):
    """A2R reproduced destroying a real persona file this way."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = os.path.join(base_dir, "personas", "lumina.json")
    original = open(dest, "rb").read()
    with pytest.raises(ab.AgentBackupError):
        _build(data_dir, base_dir, dest)
    assert open(dest, "rb").read() == original, "the persona file was overwritten by a rejected destination!"


def test_destination_symlink_rejected(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "sneaky_symlink.zip")
    os.symlink("/etc/hostname", dest)
    with pytest.raises(ab.AgentBackupError, match="symlink"):
        _build(data_dir, base_dir, dest)


def test_existing_good_destination_survives_a_build_failure(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    good_sha = ab._sha256_file(dest)

    os.remove(os.path.join(data_dir, "memory", "prefs.json"))
    with pytest.raises(ab.AgentBackupError):
        _build(data_dir, base_dir, dest)

    assert os.path.exists(dest)
    assert ab._sha256_file(dest) == good_sha


def test_existing_good_destination_survives_a_verification_failure(tmp_path, monkeypatch):
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    good_sha = ab._sha256_file(dest)

    def _always_invalid(path, resident_bytes_hint=0):
        return {"valid": False, "errors": ["forced failure for test"], "manifest": None,
                "verified_sqlite_members": []}

    monkeypatch.setattr(ab, "verify_agent_backup", _always_invalid)
    with pytest.raises(ab.AgentBackupError):
        _build(data_dir, base_dir, dest)

    assert os.path.exists(dest)
    assert ab._sha256_file(dest) == good_sha


def test_no_leftover_temp_files_after_failure(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    os.remove(os.path.join(data_dir, "memory", "prefs.json"))
    with pytest.raises(ab.AgentBackupError):
        _build(data_dir, base_dir, dest)
    leftovers = [f for f in os.listdir(tmp_path) if f.startswith(".agent_backup_")]
    assert not leftovers


# =======================================================================
# Blocker 6/7 -- Strict verify_agent_backup()
# =======================================================================

def _write_manifest_only_zip(path, manifest):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))


_MINIMAL_MANIFEST = {
    "format": "lumina-agent-backup",
    "format_version": {"major": 1, "minor": 0},
    "lumina_version": "unknown",
    "created_at": "2026-01-01T00:00:00+00:00",
    "source_platform": {}, "source_data_root": {}, "backup_mode": "full",
    "hash_algorithm": "sha256",
    "members": [], "regenerate": [], "exclusions": [], "warnings": [],
}

# A minimal manifest that also carries the canonical policy records
# (credentials exclusion + ledger regenerate entry) -- used by tests that
# want to isolate ONE specific violation rather than tripping the
# "missing canonical policy records" error on every single case.
_MINIMAL_MANIFEST_WITH_POLICY = {
    **_MINIMAL_MANIFEST,
    "exclusions": [dict(ab._CANONICAL_CREDENTIALS_EXCLUSION, note="test")],
    "regenerate": [{"logical_id": ab._CANONICAL_LEDGER_LOGICAL_ID,
                     "state_class": "REBUILDABLE_GENERATED", "restore_policy": "always_regenerate_empty"}],
}

_LUMINA_DB_MEMBER = {
    "logical_id": "state.databases.lumina_db", "state_class": "REQUIRED_AGENT_STATE",
    "content_kind": "sqlite_database", "archive_path": "state/databases/lumina.db",
    "sha256": None, "size": 0, "required": True,
    "portable": "PORTABLE_WITH_REBIND",
    "restore_policy": "overwrite_atomic_then_rewrite_skills_path_column",
    "rebind_policy": "rewrite_skills_path_column_to_dest_base_dir",
}


def _sqlite_bytes_and_hash(tmp_path, name="tiny.db"):
    p = tmp_path / name
    conn = sqlite3.connect(str(p))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    data = p.read_bytes()
    return data, hashlib.sha256(data).hexdigest()


def test_verify_rejects_zero_member_backup(tmp_path):
    dest = str(tmp_path / "zero.zip")
    _write_manifest_only_zip(dest, _MINIMAL_MANIFEST_WITH_POLICY)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("zero members" in e for e in report["errors"])


def test_verify_malformed_format_version_returns_invalid_never_raises(tmp_path):
    dest = str(tmp_path / "bad.zip")
    m = dict(_MINIMAL_MANIFEST)
    m["format_version"] = "not-a-dict"
    _write_manifest_only_zip(dest, m)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("malformed" in e for e in report["errors"])


def test_verify_rejects_unsupported_major(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    with zipfile.ZipFile(dest, "r") as zf:
        entries = {n: zf.read(n) for n in zf.namelist()}
    manifest = json.loads(entries["manifest.json"])
    manifest["format_version"] = {"major": 99, "minor": 0}
    entries["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("not supported by this code" in e for e in report["errors"])


def test_verify_rejects_major_zero(tmp_path):
    m = dict(_MINIMAL_MANIFEST_WITH_POLICY)
    m["members"] = [dict(_LUMINA_DB_MEMBER, sha256="a" * 64)]
    m["format_version"] = {"major": 0, "minor": 0}
    dest = str(tmp_path / "major_zero.zip")
    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr("manifest.json", json.dumps(m))
        zf.writestr("state/databases/lumina.db", b"")
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("major" in e for e in report["errors"])


def test_verify_rejects_boolean_major(tmp_path):
    m = dict(_MINIMAL_MANIFEST_WITH_POLICY)
    m["format_version"] = {"major": True, "minor": 0}
    dest = str(tmp_path / "bool_major.zip")
    _write_manifest_only_zip(dest, m)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("must be an int" in e and "major" in e for e in report["errors"])


def test_verify_rejects_boolean_minor(tmp_path):
    m = dict(_MINIMAL_MANIFEST_WITH_POLICY)
    m["format_version"] = {"major": 1, "minor": False}
    dest = str(tmp_path / "bool_minor.zip")
    _write_manifest_only_zip(dest, m)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("must be an int" in e and "minor" in e for e in report["errors"])


def test_verify_rejects_negative_minor(tmp_path):
    m = dict(_MINIMAL_MANIFEST_WITH_POLICY)
    m["format_version"] = {"major": 1, "minor": -1}
    dest = str(tmp_path / "neg_minor.zip")
    _write_manifest_only_zip(dest, m)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("minor" in e and ">= 0" in e for e in report["errors"])


def test_verify_rejects_missing_credential_exclusion_record(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    with zipfile.ZipFile(dest, "r") as zf:
        entries = {n: zf.read(n) for n in zf.namelist()}
    manifest = json.loads(entries["manifest.json"])
    manifest["exclusions"] = [e for e in manifest["exclusions"] if e.get("logical_id") != "credentials"]
    entries["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("credentials exclusion record" in e for e in report["errors"])


def test_verify_rejects_missing_ledger_regenerate_entry(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    with zipfile.ZipFile(dest, "r") as zf:
        entries = {n: zf.read(n) for n in zf.namelist()}
    manifest = json.loads(entries["manifest.json"])
    manifest["regenerate"] = []
    entries["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("ledger regenerate entry" in e for e in report["errors"])


def test_verify_rejects_ledger_payload_at_alternate_path(tmp_path):
    """Section 8: forbidden payload detection must be semantic, not just
    'archive_path == state/databases/ledger.db' -- a ledger dressed up
    under an unexpected path must still be rejected, by the basename
    check and by falling outside the allowlist."""
    m = dict(_MINIMAL_MANIFEST_WITH_POLICY)
    data, digest = _sqlite_bytes_and_hash(tmp_path)
    m["members"] = [
        dict(_LUMINA_DB_MEMBER, sha256=digest, size=len(data)),
        {
            "logical_id": "misc.sneaky_ledger", "state_class": "REBUILDABLE_GENERATED",
            "content_kind": "sqlite_database", "archive_path": "state/audit/renamed_ledger_copy.db",
            "sha256": "b" * 64, "size": 0, "required": False,
            "portable": "REBUILD", "restore_policy": "always_regenerate_empty",
        },
    ]
    dest = str(tmp_path / "alt_ledger.zip")
    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr("manifest.json", json.dumps(m))
        zf.writestr("state/databases/lumina.db", data)
        zf.writestr("state/audit/renamed_ledger_copy.db", b"")
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("ledger" in e.lower() for e in report["errors"])


def test_verify_rejects_worktree_payload(tmp_path):
    m = dict(_MINIMAL_MANIFEST_WITH_POLICY)
    data, digest = _sqlite_bytes_and_hash(tmp_path)
    m["members"] = [
        dict(_LUMINA_DB_MEMBER, sha256=digest, size=len(data)),
        {
            "logical_id": "misc.worktree_leak", "state_class": "EXCLUDED",
            "content_kind": "binary", "archive_path": "worktrees/some_project/leaked_file.txt",
            "sha256": "c" * 64, "size": 0, "required": False,
            "portable": "EXCLUDED", "restore_policy": "never",
        },
    ]
    dest = str(tmp_path / "worktree.zip")
    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr("manifest.json", json.dumps(m))
        zf.writestr("state/databases/lumina.db", data)
        zf.writestr("worktrees/some_project/leaked_file.txt", b"")
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("namespace" in e.lower() for e in report["errors"])


def test_verify_rejects_required_canonical_member_marked_not_required(tmp_path):
    """The verifier knows Lumina's own v1 contract for known logical IDs
    -- it must not trust an archive that declares lumina_db as
    required:false, regardless of what the manifest itself claims."""
    m = dict(_MINIMAL_MANIFEST_WITH_POLICY)
    data, digest = _sqlite_bytes_and_hash(tmp_path)
    m["members"] = [dict(_LUMINA_DB_MEMBER, sha256=digest, size=len(data), required=False)]
    dest = str(tmp_path / "not_required.zip")
    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr("manifest.json", json.dumps(m))
        zf.writestr("state/databases/lumina.db", data)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("lumina_db" in e and "required" in e for e in report["errors"])


def test_verify_rejects_custom_tool_with_autoapprove_semantics(tmp_path):
    m = dict(_MINIMAL_MANIFEST_WITH_POLICY)
    data, digest = _sqlite_bytes_and_hash(tmp_path)
    tool_bytes = b"def foo(): pass\n"
    m["members"] = [
        dict(_LUMINA_DB_MEMBER, sha256=digest, size=len(data)),
        {
            "logical_id": "state.custom_tools.evil.py", "state_class": "REQUIRED_AGENT_STATE",
            "content_kind": "python_source", "archive_path": "state/custom_tools/evil.py",
            "sha256": hashlib.sha256(tool_bytes).hexdigest(), "size": len(tool_bytes), "required": True,
            "portable": "PORTABLE_WITH_REBIND", "restore_policy": "restore_verbatim",
            "rebind_policy": "restore_and_autoapprove",
        },
    ]
    dest = str(tmp_path / "autoapprove.zip")
    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr("manifest.json", json.dumps(m))
        zf.writestr("state/databases/lumina.db", data)
        zf.writestr("state/custom_tools/evil.py", tool_bytes)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("quarantine_until_explicit_reapproval" in e for e in report["errors"])


def test_verify_rejects_wrong_root_field_types(tmp_path):
    m = dict(_MINIMAL_MANIFEST_WITH_POLICY)
    m["source_platform"] = "not-a-dict"
    m["backup_mode"] = 12345
    dest = str(tmp_path / "wrong_types.zip")
    _write_manifest_only_zip(dest, m)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("source_platform" in e for e in report["errors"])
    assert any("backup_mode" in e for e in report["errors"])


def test_verify_malformed_utf8_manifest_returns_invalid_never_raises(tmp_path):
    dest = tmp_path / "bad_utf8.zip"
    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr("manifest.json", b"\x80\x81not valid utf-8 or json at all\xfe")
    report = ab.verify_agent_backup(str(dest))
    assert report["valid"] is False
    assert report["manifest"] is None


def test_verify_rejects_missing_declared_member(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    with zipfile.ZipFile(dest, "r") as zf:
        entries = {n: zf.read(n) for n in zf.namelist()}
    del entries["state/preferences/prefs.json"]
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("required member missing" in e and "prefs.json" in e for e in report["errors"])


def test_verify_rejects_wrong_size(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    with zipfile.ZipFile(dest, "r") as zf:
        entries = {n: zf.read(n) for n in zf.namelist()}
    manifest = json.loads(entries["manifest.json"])
    for m in manifest["members"]:
        if m["archive_path"] == "overlay/personas/lumina.json":
            m["size"] = m["size"] + 1000
    entries["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("size mismatch" in e for e in report["errors"])


def test_verify_rejects_wrong_hash(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    with zipfile.ZipFile(dest, "r") as zf:
        entries = {n: zf.read(n) for n in zf.namelist()}
    entries["overlay/personas/lumina.json"] = b'{"tampered": true}'
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("hash mismatch" in e and "personas/lumina.json" in e for e in report["errors"])


def test_verify_rejects_non_sqlite_bytes_masquerading_as_primary_db(tmp_path):
    """Section 4: a non-SQLite byte string with a matching hash is still
    an invalid Agent Backup -- the verifier must independently prove the
    archived bytes are a real SQLite database, not just trust the
    hash/size match."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    with zipfile.ZipFile(dest, "r") as zf:
        entries = {n: zf.read(n) for n in zf.namelist()}
    manifest = json.loads(entries["manifest.json"])
    fake_bytes = b"not a real sqlite database, just plain bytes padded out" * 20
    fake_hash = hashlib.sha256(fake_bytes).hexdigest()
    for m in manifest["members"]:
        if m["archive_path"] == "state/databases/lumina.db":
            m["sha256"] = fake_hash
            m["size"] = len(fake_bytes)
    entries["state/databases/lumina.db"] = fake_bytes
    entries["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("sqlite" in e.lower() for e in report["errors"])


def test_verify_rejects_corrupt_flight_recorder_payload(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    with zipfile.ZipFile(dest, "r") as zf:
        entries = {n: zf.read(n) for n in zf.namelist()}
    manifest = json.loads(entries["manifest.json"])
    original = entries["state/telemetry/flight_recorder.db"]
    corrupted = bytearray(original)
    # Offset 100 lands inside page 1's own header/schema region -- a
    # midpoint or late-file offset can land in a genuinely unallocated/
    # free page on a lightly-populated database and leave quick_check
    # untouched (measured directly against this fixture's schema).
    start = min(100, max(0, len(corrupted) - 64))
    for i in range(start, min(start + 64, len(corrupted))):
        corrupted[i] = 0xFF
    corrupted = bytes(corrupted)
    corrupted_hash = hashlib.sha256(corrupted).hexdigest()
    for m in manifest["members"]:
        if m["archive_path"] == "state/telemetry/flight_recorder.db":
            m["sha256"] = corrupted_hash
            m["size"] = len(corrupted)
    entries["state/telemetry/flight_recorder.db"] = corrupted
    entries["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False


def test_verify_rejects_undeclared_physical_member(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    with zipfile.ZipFile(dest, "r") as zf:
        entries = {n: zf.read(n) for n in zf.namelist()}
    entries["overlay/personas/injected_extra.json"] = b'{"not": "declared"}'
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("undeclared physical member" in e for e in report["errors"])


def test_verify_rejects_duplicate_zip_member_name(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    with zipfile.ZipFile(dest, "r") as zf:
        entries = [(n, zf.read(n)) for n in zf.namelist()]
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            zf.writestr(name, data)
        # deliberately write one entry a second time under the same name
        zf.writestr(entries[-1][0], entries[-1][1])
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("duplicate zip member name" in e for e in report["errors"])


def test_verify_rejects_absolute_and_dotdot_archive_paths(tmp_path):
    for bad_path in ("/etc/passwd", "overlay/../../../etc/passwd", "overlay//personas/x.json"):
        m = dict(_MINIMAL_MANIFEST_WITH_POLICY)
        m["members"] = [{
            "logical_id": "state.databases.lumina_db", "state_class": "REQUIRED_AGENT_STATE",
            "content_kind": "sqlite_database", "archive_path": bad_path,
            "sha256": "a" * 64, "size": 0, "required": True,
            "portable": "PORTABLE", "restore_policy": "overwrite_atomic",
        }]
        dest = str(tmp_path / f"bad_{abs(hash(bad_path))}.zip")
        with zipfile.ZipFile(dest, "w") as zf:
            zf.writestr("manifest.json", json.dumps(m))
            zf.writestr(bad_path.lstrip("/"), b"")
        report = ab.verify_agent_backup(dest)
        assert report["valid"] is False, f"expected {bad_path!r} to be rejected"


def test_verify_rejects_duplicate_logical_id(tmp_path):
    m = dict(_MINIMAL_MANIFEST_WITH_POLICY)
    base_member = {
        "state_class": "REQUIRED_AGENT_STATE", "content_kind": "json",
        "sha256": "a" * 64, "size": 0, "required": True,
        "portable": "PORTABLE", "restore_policy": "overwrite_atomic",
    }
    m["members"] = [
        {**base_member, "logical_id": "state.databases.lumina_db", "archive_path": "state/databases/lumina.db"},
        {**base_member, "logical_id": "state.databases.lumina_db", "archive_path": "state/preferences/prefs.json"},
    ]
    dest = str(tmp_path / "dup_logical.zip")
    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr("manifest.json", json.dumps(m))
        zf.writestr("state/databases/lumina.db", b"")
        zf.writestr("state/preferences/prefs.json", b"")
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("duplicate logical_id" in e for e in report["errors"])


def test_verify_rejects_physical_ledger_payload(tmp_path):
    m = dict(_MINIMAL_MANIFEST_WITH_POLICY)
    m["members"] = [{
        "logical_id": "state.databases.ledger_db", "state_class": "REBUILDABLE_GENERATED",
        "content_kind": "sqlite_database", "archive_path": "state/databases/ledger.db",
        "sha256": "a" * 64, "size": 0, "required": False,
        "portable": "REBUILD", "restore_policy": "always_regenerate_empty",
    }, {
        "logical_id": "state.databases.lumina_db", "state_class": "REQUIRED_AGENT_STATE",
        "content_kind": "sqlite_database", "archive_path": "state/databases/lumina.db",
        "sha256": "b" * 64, "size": 0, "required": True,
        "portable": "PORTABLE", "restore_policy": "overwrite_atomic",
    }]
    dest = str(tmp_path / "ledger_payload.zip")
    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr("manifest.json", json.dumps(m))
        zf.writestr("state/databases/ledger.db", b"")
        zf.writestr("state/databases/lumina.db", b"")
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("ledger" in e.lower() and "never" in e.lower() for e in report["errors"])


def test_verify_rejects_regenerate_entry_with_physical_fields(tmp_path):
    m = dict(_MINIMAL_MANIFEST_WITH_POLICY)
    m["members"] = [{
        "logical_id": "state.databases.lumina_db", "state_class": "REQUIRED_AGENT_STATE",
        "content_kind": "sqlite_database", "archive_path": "state/databases/lumina.db",
        "sha256": "a" * 64, "size": 0, "required": True,
        "portable": "PORTABLE", "restore_policy": "overwrite_atomic",
    }]
    m["regenerate"] = [{
        "logical_id": "state.databases.ledger_db", "state_class": "REBUILDABLE_GENERATED",
        "restore_policy": "always_regenerate_empty", "archive_path": "state/databases/ledger.db",
        "sha256": "a" * 64,
    }]
    dest = str(tmp_path / "regen_bad.zip")
    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr("manifest.json", json.dumps(m))
        zf.writestr("state/databases/lumina.db", b"")
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("must not carry" in e for e in report["errors"])


def test_verify_rejects_non_zip_file(tmp_path):
    dest = tmp_path / "not_a_zip.zip"
    dest.write_bytes(b"this is not a zip file")
    report = ab.verify_agent_backup(str(dest))
    assert report["valid"] is False
    assert report["manifest"] is None


def test_verify_agent_backup_passes_on_a_real_archive(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is True
    assert report["errors"] == []
    assert report["manifest"]["format"] == ab.MANIFEST_FORMAT
    assert "state/databases/lumina.db" in report["verified_sqlite_members"]
    assert "state/telemetry/flight_recorder.db" in report["verified_sqlite_members"]


# =======================================================================
# AGENT-BACKUP-RESTORE-A2R7 (Section 20): a valid ZIP + arbitrary
# trailing bytes/comment passed strict verification entirely undetected
# -- Lumina's v1 format defines no use for either.
# =======================================================================

def test_verify_rejects_trailing_bytes_appended_after_valid_archive(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    assert ab.verify_agent_backup(dest)["valid"] is True  # sanity: valid before tampering

    with open(dest, "ab") as f:
        f.write(b"X" * 23)  # the literal AGENT-BACKUP-RESTORE-A2R7 reproduction size

    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("trailing data" in e for e in report["errors"])


def test_verify_rejects_zip_comment(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)

    with zipfile.ZipFile(dest, "a") as zf:
        zf.comment = b"not part of Lumina's v1 format"

    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("ZIP comment" in e for e in report["errors"])


def test_verify_rejects_trailing_bytes_via_build_backup_receipt(tmp_path):
    """The trailing-bytes check must fire for the BytesIO-backed receipt
    path too, not only the path-based verify_agent_backup entry point."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    with open(dest, "ab") as f:
        f.write(b"trailing garbage, no defined A1 purpose")

    receipt = ab.build_backup_receipt(dest)
    assert receipt["verification"] == "FAIL"
    assert any("trailing data" in e for e in receipt["errors"])


def test_check_no_trailing_zip_bytes_accepts_a_genuinely_clean_archive(tmp_path):
    """Direct unit-level sanity check on the helper itself, isolated from
    the full verify pipeline -- a real, freshly-built archive's raw bytes
    exactly account for themselves."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    with open(dest, "rb") as f:
        raw = f.read()
    assert ab._check_no_trailing_zip_bytes(raw) is None


# =======================================================================
# Unicode / namespace collisions (Section 9)
# =======================================================================

def test_verify_rejects_unicode_nfc_normalization_collision(tmp_path):
    m = dict(_MINIMAL_MANIFEST_WITH_POLICY)
    data, digest = _sqlite_bytes_and_hash(tmp_path)
    # "e" + combining acute accent (NFD) vs the precomposed "é" (NFC)
    # normalize to the SAME string but are byte-distinct as written here.
    nfd_path = "overlay/personas/café.json"
    nfc_path = "overlay/personas/café.json"
    m["members"] = [
        dict(_LUMINA_DB_MEMBER, sha256=digest, size=len(data)),
        {
            "logical_id": "overlay.personas.a", "state_class": "USER_OVERLAY",
            "content_kind": "json", "archive_path": nfd_path,
            "sha256": "a" * 64, "size": 0, "required": True,
            "portable": "PORTABLE", "restore_policy": "baseline_aware_merge",
        },
        {
            "logical_id": "overlay.personas.b", "state_class": "USER_OVERLAY",
            "content_kind": "json", "archive_path": nfc_path,
            "sha256": "b" * 64, "size": 0, "required": True,
            "portable": "PORTABLE", "restore_policy": "baseline_aware_merge",
        },
    ]
    dest = str(tmp_path / "unicode_collision.zip")
    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr("manifest.json", json.dumps(m))
        zf.writestr("state/databases/lumina.db", data)
        zf.writestr(nfd_path, b"")
        zf.writestr(nfc_path, b"")
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("normalization collision" in e for e in report["errors"])


def test_verify_rejects_manifest_json_case_and_unicode_collision(tmp_path):
    m = dict(_MINIMAL_MANIFEST_WITH_POLICY)
    data, digest = _sqlite_bytes_and_hash(tmp_path)
    m["members"] = [
        dict(_LUMINA_DB_MEMBER, sha256=digest, size=len(data)),
        {
            "logical_id": "overlay.skills.a", "state_class": "USER_OVERLAY",
            "content_kind": "markdown", "archive_path": "Manifest.json",
            "sha256": "a" * 64, "size": 0, "required": True,
            "portable": "PORTABLE", "restore_policy": "baseline_aware_merge",
        },
    ]
    dest = str(tmp_path / "manifest_collision.zip")
    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr("manifest.json", json.dumps(m))
        zf.writestr("state/databases/lumina.db", data)
        zf.writestr("Manifest.json", b"not the real manifest")
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("reserved manifest.json" in e for e in report["errors"])


# =======================================================================
# Baseline provenance (Section 10) + rubber duck (Section 11)
# =======================================================================

def test_generate_baseline_hashes_only_hashes_git_tracked_files():
    """Real regression against the actual release checkout -- confirms
    the duck (gitignored) is excluded from the baseline while its shipped
    sibling skills are included."""
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if not os.path.isdir(os.path.join(base_dir, ".git")):
        pytest.skip("not running inside a git checkout")
    baseline = ab.generate_baseline_hashes(base_dir)
    assert baseline["format"] == ab.BASELINE_FORMAT
    assert "git_commit" in baseline and len(baseline["git_commit"]) == 40
    assert "skills/rubber-duck-debugging.md" not in baseline["files"]
    assert "personas/lumina.json" in baseline["files"]


def test_baseline_from_git_tracked_files_excludes_untracked_duck(tmp_path):
    """Synthetic git repo, so this doesn't depend on the real release
    tree's current tracked-file set."""
    base_dir = tmp_path / "fake_release"
    base_dir.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=base_dir, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=base_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=base_dir, check=True)
    (base_dir / "skills").mkdir()
    (base_dir / "skills" / "shipped-skill.md").write_text("# shipped\n")
    subprocess.run(["git", "add", "skills/shipped-skill.md"], cwd=base_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=base_dir, check=True)
    # duck exists on disk but was never committed/tracked
    (base_dir / "skills" / "rubber-duck-debugging.md").write_text("# duck\n")

    baseline = ab.generate_baseline_hashes(str(base_dir))
    assert "skills/shipped-skill.md" in baseline["files"]
    assert "skills/rubber-duck-debugging.md" not in baseline["files"]


def test_generate_baseline_hashes_uses_git_object_bytes_not_dirty_working_tree(tmp_path):
    """AGENT-BACKUP-RESTORE-A2R2: the previous generator listed git-
    tracked NAMES but hashed whatever was currently on disk, so a dirty
    tracked file was falsely canonized as 'shipped' truth. This proves
    the fix: the committed blob's bytes are hashed via `git show`, not
    the working tree's current (dirty) bytes."""
    base_dir = tmp_path / "fake_release"
    base_dir.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=base_dir, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=base_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=base_dir, check=True)
    (base_dir / "skills").mkdir()
    (base_dir / "skills" / "shipped-skill.md").write_text("# shipped v1\n")
    subprocess.run(["git", "add", "skills/shipped-skill.md"], cwd=base_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=base_dir, check=True)
    committed_hash = ab._sha256_file(str(base_dir / "skills" / "shipped-skill.md"))

    (base_dir / "skills" / "shipped-skill.md").write_text("# DIRTY UNCOMMITTED EDIT\n")
    dirty_hash = ab._sha256_file(str(base_dir / "skills" / "shipped-skill.md"))
    assert dirty_hash != committed_hash

    baseline = ab.generate_baseline_hashes(str(base_dir))
    assert baseline["files"]["skills/shipped-skill.md"] == committed_hash
    assert baseline["files"]["skills/shipped-skill.md"] != dirty_hash


def test_baseline_version_mismatch_falls_back_to_unknown_truthfully(tmp_path):
    """Section 10: 'never silently treat a stale/wrong-release baseline
    as authoritative' -- a baseline pinned to a DIFFERENT lumina_version
    than the running build must fall back to unknown + a warning, not be
    trusted."""
    data_dir, base_dir = _make_env(tmp_path)
    with open(os.path.join(base_dir, "main.py"), "w") as f:
        f.write('LUMINA_VERSION = "1.2.3"\n')
    persona_hash = ab._sha256_file(os.path.join(base_dir, "personas", "lumina.json"))
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(_wrap_baseline(
        {"personas/lumina.json": persona_hash}, lumina_version="9.9.9-different-release")))

    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest, baseline_hashes_path=str(baseline_path))
    members = _members_by_path(manifest)
    assert members["overlay/personas/lumina.json"]["provenance"] == "unknown"
    assert any("stale" in w or "wrong-release" in w for w in manifest["warnings"])


def test_legacy_flat_baseline_format_is_treated_as_untrusted(tmp_path):
    """A pre-A2F2 flat {path: hash} baseline carries no format/version/
    commit binding at all -- must not be silently trusted just because
    it happens to parse as JSON."""
    data_dir, base_dir = _make_env(tmp_path)
    persona_hash = ab._sha256_file(os.path.join(base_dir, "personas", "lumina.json"))
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps({"personas/lumina.json": persona_hash}))

    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest, baseline_hashes_path=str(baseline_path))
    members = _members_by_path(manifest)
    assert members["overlay/personas/lumina.json"]["provenance"] == "unknown"
    assert any("legacy flat format" in w for w in manifest["warnings"])


def test_duck_present_but_not_in_baseline_classifies_unknown_not_shipped(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    # A real git-backed baseline (AGENT-BACKUP-RESTORE-A2F3's git-object
    # cross-verification rejects a hand-rolled fake-commit baseline) --
    # the duck is .gitignore'd before committing, exactly mirroring the
    # real release tree, so it's genuinely untracked rather than merely
    # omitted from a hand-built dict.
    baseline = _real_baseline_for(base_dir, gitignore_patterns=["skills/rubber-duck-debugging.md"])
    assert "skills/rubber-duck-debugging.md" not in baseline["files"]
    assert "personas/lumina.json" in baseline["files"]
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(baseline))

    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest, baseline_hashes_path=str(baseline_path))
    members = _members_by_path(manifest)
    assert members["overlay/skills/rubber-duck-debugging.md"]["provenance"] == "unknown"
    assert members["overlay/personas/lumina.json"]["provenance"] == "shipped_baseline_unmodified"


def test_duck_is_always_captured_never_pruned(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest)
    members = _members_by_path(manifest)
    assert "overlay/skills/rubber-duck-debugging.md" in members
    with zipfile.ZipFile(dest) as zf:
        content = zf.read("overlay/skills/rubber-duck-debugging.md")
    assert b"Rubber Duck" in content


def test_modified_shipped_overlay_classified_as_modified(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    persona_path = os.path.join(base_dir, "personas", "lumina.json")
    # Commit the ORIGINAL content as the real git-backed baseline truth
    # BEFORE modifying it -- generate_baseline_hashes reads immutable git
    # object bytes, never the working tree, so the baseline correctly
    # keeps reflecting the pre-edit content after the edit below.
    baseline = _real_baseline_for(base_dir)
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(baseline))

    with open(persona_path, "w") as f:
        json.dump({"name": "Lumina", "system_prompt": "edited by owner"}, f)

    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest, baseline_hashes_path=str(baseline_path))
    members = _members_by_path(manifest)
    assert members["overlay/personas/lumina.json"]["provenance"] == "shipped_baseline_modified"


def test_new_user_created_overlay_preserved(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    (tmp_path / "base_dir" / "personas" / "custom_persona.json").write_text(
        json.dumps({"name": "Custom"})
    )
    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest)
    members = _members_by_path(manifest)
    assert "overlay/personas/custom_persona.json" in members
    assert members["overlay/personas/custom_persona.json"]["provenance"] == "unknown"


def test_no_baseline_supplied_defaults_to_unknown_with_warning(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest)
    members = _members_by_path(manifest)
    assert members["overlay/personas/lumina.json"]["provenance"] == "unknown"
    assert any("baseline_hashes_path" in w for w in manifest["warnings"])


def test_default_baseline_path_auto_discovered_from_base_dir(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    baseline = _real_baseline_for(base_dir)
    metadata_dir = os.path.join(base_dir, "metadata")
    os.makedirs(metadata_dir)
    with open(os.path.join(metadata_dir, "shipped_baseline_hashes.json"), "w") as f:
        json.dump(baseline, f)

    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest)  # baseline_hashes_path not passed at all
    members = _members_by_path(manifest)
    assert members["overlay/personas/lumina.json"]["provenance"] == "shipped_baseline_unmodified"
    assert "metadata/shipped_baseline_hashes.json" in members


# =======================================================================
# Custom-tool authority (Section 10 -- A1) + Project coverage (Section 6)
# =======================================================================

def test_custom_tools_and_audit_log_require_quarantine_rebind(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest)
    members = _members_by_path(manifest)

    for path in ("state/custom_tools/my_tool.py",
                 "state/custom_tools/_pending/staged_tool.py",
                 "state/audit/tool_audit.log"):
        m = members[path]
        assert m["required"] is True, f"{path} content must still be REQUIRED"
        assert m["portable"] == "PORTABLE_WITH_REBIND"
        assert m["rebind_policy"] == "quarantine_until_explicit_reapproval"


def test_project_chats_json_is_portable_not_rebind(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest)
    members = _members_by_path(manifest)
    assert members["overlay/projects/demo/chats.json"]["portable"] == "PORTABLE"
    assert members["overlay/projects/demo/binding.json"]["portable"] == "PORTABLE_WITH_REBIND"
    assert members["overlay/projects/demo/codebase.md"]["state_class"] == "REBUILDABLE_GENERATED"
    assert members["overlay/projects/demo/project.md"]["state_class"] == "USER_OVERLAY"


# =======================================================================
# Manifest root completeness (Section 12 -- A1) + receipt (Section 12)
# =======================================================================

def test_manifest_root_has_all_a1_fields(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest)
    for key in ("format", "format_version", "lumina_version", "created_at", "source_platform",
                "source_data_root", "backup_mode", "hash_algorithm", "compatibility",
                "members", "regenerate", "exclusions", "warnings"):
        assert key in manifest, f"missing root field {key!r}"
    assert manifest["format_version"] == {"major": 1, "minor": 0}
    assert manifest["lumina_version"] != ""  # either the real constant or 'unknown', never blank


def test_lumina_version_read_from_main_py_not_fabricated(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    with open(os.path.join(base_dir, "main.py"), "w") as f:
        f.write('LUMINA_VERSION = "9.9.9-test"\n')
    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest)
    assert manifest["lumina_version"] == "9.9.9-test"


def test_lumina_version_falls_back_to_unknown_when_absent(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)  # no main.py in this fixture
    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest)
    assert manifest["lumina_version"] == "unknown"


def test_flight_recorder_metadata_includes_retention_config(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest)
    members = _members_by_path(manifest)
    fr = members["state/telemetry/flight_recorder.db"]
    retention = fr["database_metadata"].get("retention_policy", {})
    from core import flight_recorder as real_fr
    assert retention.get("full_retention_seconds") == real_fr.FULL_RETENTION_SECONDS
    assert retention.get("error_retention_seconds") == real_fr.ERROR_RETENTION_SECONDS


def test_backup_receipt_fields_are_real_not_fabricated(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    receipt = ab.build_backup_receipt(dest)

    assert receipt["verification"] == "PASS"
    assert receipt["archive_sha256"] == ab._sha256_file(dest)
    assert receipt["archive_size"] == os.path.getsize(dest)
    assert receipt["member_count"] > 0
    assert receipt["flight_recorder_included"] is True
    assert receipt["database_snapshots_verified"] == 2  # lumina.db + flight_recorder.db, independently proven
    assert receipt["archive_policy_credentials_excluded"] is True
    # Section 14/15 (R4-B4): build_backup_receipt(archive_path) alone, with
    # no build_credentials_boundary_verified argument, can never honestly
    # claim the ephemeral build-time boundary check ran during THIS
    # invocation -- it wasn't the one that built this archive.
    assert receipt["build_credentials_boundary_verified"] == "not_provable_from_archive_alone"
    assert receipt["regenerate_count"] == 1
    assert receipt["personas_count"] == 1
    assert receipt["projects_count"] == 1
    assert "archive_contains_no_secrets" not in receipt


def test_build_agent_backup_with_receipt_honestly_proves_build_time_boundary_check(tmp_path):
    """Section 15: build_credentials_boundary_verified=True is only ever
    permitted immediately after THIS invocation's own build actually ran
    the live filesystem/inode credential-boundary checks -- proven here
    via the one code path that may honestly assert it."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    manifest, receipt = ab.build_agent_backup_with_receipt(
        data_dir, base_dir, dest, identity_trailers_path=NO_IDENTITY_TRAILERS,
        credentials_path=NO_CREDENTIALS,
    )
    assert receipt["verification"] == "PASS"
    assert receipt["build_credentials_boundary_verified"] is True
    assert receipt["archive_policy_credentials_excluded"] is True
    assert manifest["backup_mode"] == "full"


def test_build_backup_receipt_public_signature_has_no_evidence_parameter(tmp_path):
    """AGENT-BACKUP-RESTORE-A2R5 (Section 12, BLOCKER): the public
    build_backup_receipt used to accept build_credentials_boundary_
    verified as an ordinary boolean keyword argument -- any caller could
    mint the claim directly. Proves the parameter no longer exists on
    the public function at all (a TypeError, not merely 'ignored'), so
    there is no longer any public API surface through which a caller can
    even ATTEMPT to assert build-time evidence for an archive it didn't
    build."""
    import inspect
    sig = inspect.signature(ab.build_backup_receipt)
    assert list(sig.parameters) == ["archive_path"]
    with pytest.raises(TypeError):
        ab.build_backup_receipt("/nonexistent/path.zip", build_credentials_boundary_verified=True)


def test_hand_built_archive_cannot_forge_build_evidence_via_public_receipt_api(tmp_path):
    """The literal AGENT-BACKUP-RESTORE-A2R5 reproduction: a fully valid,
    hand-built archive (never touched by build_agent_backup at all) can
    still honestly prove archive_policy_credentials_excluded=True (a
    claim about its own verified manifest), but build_backup_receipt --
    the only public entry point available to code that didn't build the
    archive -- can never be made to report build_credentials_boundary_
    verified as anything but the honest 'not_provable_from_archive_alone'
    string, regardless of what the caller wants."""
    data_dir, base_dir = _make_env(tmp_path)
    real_dest = str(tmp_path / "real.zip")
    ab.build_agent_backup(data_dir, base_dir, real_dest,
                           identity_trailers_path=NO_IDENTITY_TRAILERS, credentials_path=NO_CREDENTIALS)
    # A completely independent call, with no evidence object anywhere in
    # scope -- exactly what any other module's code would have access to.
    receipt = ab.build_backup_receipt(real_dest)
    assert receipt["verification"] == "PASS"
    assert receipt["archive_policy_credentials_excluded"] is True
    assert receipt["build_credentials_boundary_verified"] == "not_provable_from_archive_alone"


def test_build_security_evidence_cannot_be_constructed_outside_the_module(tmp_path):
    """Direct proof of the sentinel guard (Section 14): even reaching
    into the module's own private class, an ordinary caller supplying
    its own sentinel object (not the module's real, unexported
    _EVIDENCE_SENTINEL) cannot construct usable evidence."""
    with pytest.raises(ab.AgentBackupError):
        ab._BuildSecurityEvidence(object(), "/some/path.zip", "a" * 64)


def test_build_evidence_reuse_across_different_archive_is_rejected(tmp_path):
    """AGENT-BACKUP-RESTORE-A2R5 Section 15's explicit attack: 'build
    archive A, capture verified=true, reuse evidence for archive B.'
    Evidence is bound to a specific (archive_path, archive_sha256) pair
    -- reusing it against DIFFERENT bytes at the same path must fall
    back to the honest 'not provable' answer, never silently trust
    stale/foreign evidence."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    ab.build_agent_backup(data_dir, base_dir, dest,
                           identity_trailers_path=NO_IDENTITY_TRAILERS, credentials_path=NO_CREDENTIALS)
    with open(dest, "rb") as f:
        real_bytes = f.read()

    # Evidence honestly bound to a DIFFERENT (nonexistent) archive/hash.
    foreign_evidence = ab._BuildSecurityEvidence(ab._EVIDENCE_SENTINEL, dest, "0" * 64)
    receipt = ab._build_receipt_from_bytes(dest, real_bytes, len(real_bytes), foreign_evidence)
    assert receipt["verification"] == "PASS"
    assert receipt["build_credentials_boundary_verified"] == "not_provable_from_archive_alone"

    # Evidence bound to a DIFFERENT path with the SAME real hash --
    # path must match too, not just the hash.
    foreign_path_evidence = ab._BuildSecurityEvidence(
        ab._EVIDENCE_SENTINEL, "/some/other/archive.zip", hashlib.sha256(real_bytes).hexdigest())
    receipt2 = ab._build_receipt_from_bytes(dest, real_bytes, len(real_bytes), foreign_path_evidence)
    assert receipt2["build_credentials_boundary_verified"] == "not_provable_from_archive_alone"

    # The genuinely matching (path, hash) pair IS trusted.
    matching_evidence = ab._BuildSecurityEvidence(
        ab._EVIDENCE_SENTINEL, dest, hashlib.sha256(real_bytes).hexdigest())
    receipt3 = ab._build_receipt_from_bytes(dest, real_bytes, len(real_bytes), matching_evidence)
    assert receipt3["build_credentials_boundary_verified"] is True


def test_build_evidence_cannot_transfer_to_a_replacement_archive_the_literal_a2r6_reproduction(
        tmp_path, monkeypatch):
    """The literal AGENT-BACKUP-RESTORE-A2R6 reproduction (Section 15-18,
    BLOCKER): build a legitimate archive A, then -- in the exact gap the
    OLD design left open, between the internal build's own os.replace()
    landing and a LATER, separate, path-based reopen of dest_path for
    receipt evidence -- replace dest_path with a DIFFERENT, independently
    verifier-valid archive B (carrying an obvious marker). Under the old
    design the receipt described B while still claiming A's build-time
    boundary evidence. A2F6 closes this structurally: there is no
    separate path-based reopen left anywhere in build_agent_backup_with_
    receipt's own call chain (it reads from a descriptor bound to the
    built archive BEFORE the publish), and its own post-replace identity
    check independently confirms dest_path still refers to exactly what
    was just published -- so this attack can no longer even find a
    window to land in silently: it is caught and the whole call fails
    closed instead."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")

    # Archive B: a fully independent, legitimate, verifier-valid archive
    # (built from a completely different environment) with an unmistakable
    # marker, so a leak/transfer would be obvious rather than merely
    # byte-coincidental.
    evil_data_dir, evil_base_dir = _make_env(tmp_path / "evil_env")
    decoy_dest = str(tmp_path / "decoy.zip")
    ab.build_agent_backup(evil_data_dir, evil_base_dir, decoy_dest,
                           identity_trailers_path=NO_IDENTITY_TRAILERS, credentials_path=NO_CREDENTIALS)
    with open(decoy_dest, "rb") as f:
        decoy_bytes = f.read()

    real_replace = os.replace
    state = {"swapped": False}

    def _sabotage_replace(src, dst, *a, **kw):
        real_replace(src, dst, *a, **kw)
        if dst == dest and not state["swapped"]:
            state["swapped"] = True
            # Race a DIFFERENT, already-valid archive into dest_path in
            # the instant after the real build's own os.replace() lands.
            decoy_tmp = dest + ".decoy_swap"
            with open(decoy_tmp, "wb") as f:
                f.write(decoy_bytes)
            real_replace(decoy_tmp, dst)

    monkeypatch.setattr(os, "replace", _sabotage_replace)
    with pytest.raises(ab.AgentBackupError, match="destination changed"):
        ab.build_agent_backup_with_receipt(
            data_dir, base_dir, dest,
            identity_trailers_path=NO_IDENTITY_TRAILERS, credentials_path=NO_CREDENTIALS)
    monkeypatch.undo()

    assert state["swapped"], "the sabotage never fired -- test setup is broken, not the code"
    # No PASS receipt was ever produced for the decoy -- confirmed by the
    # raise above. dest_path now genuinely holds the decoy (proving the
    # swap really landed), which is exactly why failing closed here
    # matters: nothing may describe it as this call's own honest output.
    with open(dest, "rb") as f:
        assert f.read() == decoy_bytes


def test_build_agent_backup_plain_also_fails_closed_on_post_replace_tampering(tmp_path, monkeypatch):
    """The publication-integrity gate (Section 18) lives in the SHARED
    _build_agent_backup_core, not only in the receipt path -- the plain,
    manifest-only build_agent_backup call is protected by the same
    check, since both public entry points are thin wrappers over one
    core."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")

    real_replace = os.replace
    state = {"swapped": False}

    def _sabotage_replace(src, dst, *a, **kw):
        real_replace(src, dst, *a, **kw)
        if dst == dest and not state["swapped"]:
            state["swapped"] = True
            decoy_tmp = dest + ".decoy_swap"
            with open(decoy_tmp, "wb") as f:
                f.write(b"a completely different, unrelated file replacing the real backup")
            real_replace(decoy_tmp, dst)

    monkeypatch.setattr(os, "replace", _sabotage_replace)
    with pytest.raises(ab.AgentBackupError, match="destination changed"):
        _build(data_dir, base_dir, dest)
    monkeypatch.undo()
    assert state["swapped"], "the sabotage never fired -- test setup is broken, not the code"


# =======================================================================
# AGENT-BACKUP-RESTORE-A2R7 (Section 10-19, BLOCKER): the SHARPER finding
# -- a same-inode IN-PLACE rewrite (no os.replace(), no new inode at all)
# is not caught by identity alone (Section 18: 'same dev/inode != same
# archive bytes'). A2F6's own fix only closed the PATH-level replacement
# attack above; these tests reproduce the deeper one A2F6 left open.
# =======================================================================

def test_build_evidence_cannot_survive_same_inode_content_mutation_before_receipt(tmp_path, monkeypatch):
    """The literal, sharper AGENT-BACKUP-RESTORE-A2R7 reproduction: after
    the real build's own os.replace() publishes archive A, dest_path's
    SAME INODE (no new path, no os.replace() call at all -- just an
    ordinary open+write+truncate on the existing file) is rewritten
    in-place to a different, independently-valid archive B. An
    identity-only check (A2F6's own fix) cannot detect this at all,
    since the inode never changes -- only the NEW content-based final
    publication observation (Section 16-17) can."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")

    evil_data_dir, evil_base_dir = _make_env(tmp_path / "evil_env_inplace")
    decoy_dest = str(tmp_path / "decoy_inplace.zip")
    ab.build_agent_backup(evil_data_dir, evil_base_dir, decoy_dest,
                           identity_trailers_path=NO_IDENTITY_TRAILERS, credentials_path=NO_CREDENTIALS)
    with open(decoy_dest, "rb") as f:
        decoy_bytes = f.read()

    real_replace = os.replace
    state = {"swapped": False}

    def _sabotage_replace(src, dst, *a, **kw):
        real_replace(src, dst, *a, **kw)
        if dst == dest and not state["swapped"]:
            state["swapped"] = True
            # SAME inode -- no new path, no os.replace(), just an
            # in-place rewrite of the file os.replace() just published.
            with open(dest, "r+b") as f:
                f.seek(0)
                f.write(decoy_bytes)
                f.truncate()

    monkeypatch.setattr(os, "replace", _sabotage_replace)
    with pytest.raises(ab.AgentBackupError, match="destination changed|different bytes"):
        ab.build_agent_backup_with_receipt(
            data_dir, base_dir, dest,
            identity_trailers_path=NO_IDENTITY_TRAILERS, credentials_path=NO_CREDENTIALS)
    monkeypatch.undo()

    assert state["swapped"], "the sabotage never fired -- test setup is broken, not the code"
    with open(dest, "rb") as f:
        assert f.read() == decoy_bytes, "confirms the same-inode rewrite really landed"


def test_build_evidence_cannot_survive_same_inode_content_mutation_racing_the_observation_read(
        tmp_path, monkeypatch):
    """A distinct timing point from the test above: the same-inode
    rewrite is timed to land DURING the final publication observation's
    own bracketed double-read (Section 19's 'during publication
    verification' timing point), not merely before it starts. The
    observation's own stability proof (the same primitive used for every
    other physical source) must catch this too."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")

    evil_data_dir, evil_base_dir = _make_env(tmp_path / "evil_env_racing")
    decoy_dest = str(tmp_path / "decoy_racing.zip")
    ab.build_agent_backup(evil_data_dir, evil_base_dir, decoy_dest,
                           identity_trailers_path=NO_IDENTITY_TRAILERS, credentials_path=NO_CREDENTIALS)
    with open(decoy_dest, "rb") as f:
        decoy_bytes = f.read()

    def _rewrite_in_place_to_decoy(path):
        with open(path, "r+b") as f:
            f.seek(0)
            f.write(decoy_bytes)
            f.truncate()

    state = _install_mid_read_mutation(
        monkeypatch, dest, _rewrite_in_place_to_decoy, trigger_count=1, max_triggers=1)

    with pytest.raises(ab.AgentBackupError, match="destination changed|different bytes"):
        ab.build_agent_backup_with_receipt(
            data_dir, base_dir, dest,
            identity_trailers_path=NO_IDENTITY_TRAILERS, credentials_path=NO_CREDENTIALS)
    monkeypatch.undo()
    assert state["triggers_fired"] == 1, "the sabotage never fired -- test setup is broken, not the code"


def test_receipt_attests_only_built_bytes_never_the_currently_observed_destination(tmp_path, monkeypatch):
    """Section 14/19: once a receipt is successfully returned, it must
    keep attesting the ORIGINAL built archive A forever -- even after
    dest_path is subsequently mutated to B by something else entirely
    AFTER this call has already returned. The receipt object itself must
    never 're-describe' whatever currently sits at dest_path; a FRESH,
    independent check of dest_path (what Restore would eventually do) is
    what correctly reports B -- proving the two are properly decoupled."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    manifest, receipt = ab.build_agent_backup_with_receipt(
        data_dir, base_dir, dest,
        identity_trailers_path=NO_IDENTITY_TRAILERS, credentials_path=NO_CREDENTIALS)
    assert receipt["verification"] == "PASS"
    original_sha256 = receipt["archive_sha256"]
    assert receipt["publication"]["matched_built_artifact"] is True
    assert receipt["publication"]["observed_sha256"] == original_sha256

    # Mutate dest_path AFTER the call has already returned -- a genuinely
    # later, independent event this module cannot and does not claim to
    # prevent (Section 11).
    evil_data_dir, evil_base_dir = _make_env(tmp_path / "evil_env_after_return")
    decoy_dest = str(tmp_path / "decoy_after_return.zip")
    ab.build_agent_backup(evil_data_dir, evil_base_dir, decoy_dest,
                           identity_trailers_path=NO_IDENTITY_TRAILERS, credentials_path=NO_CREDENTIALS)
    with open(decoy_dest, "rb") as f:
        decoy_bytes = f.read()
    with open(dest, "r+b") as f:
        f.seek(0)
        f.write(decoy_bytes)
        f.truncate()

    # The ALREADY-RETURNED receipt is an ordinary Python dict -- nothing
    # mutates it after the fact. It still describes A.
    assert receipt["archive_sha256"] == original_sha256
    assert receipt["archive_sha256"] != hashlib.sha256(decoy_bytes).hexdigest()

    # A FRESH, independent check of dest_path correctly reports B, not A
    # -- this is what a future Restore's own reverification would see
    # (Section 12: Restore must never trust a stale receipt, it must
    # verify the bytes it is about to consume).
    fresh_receipt = ab.build_backup_receipt(dest)
    assert fresh_receipt["archive_sha256"] == hashlib.sha256(decoy_bytes).hexdigest()
    assert fresh_receipt["archive_sha256"] != original_sha256


# =======================================================================
# AGENT-BACKUP-RESTORE-A2R7 (Section 26): os.replace() already overwrites
# whatever good archive previously lived at dest_path BEFORE the final
# publication observation runs -- if that observation then detects
# tampering, the prior good archive is otherwise gone with no way back.
# =======================================================================

def test_publication_mismatch_restores_the_prior_good_destination(tmp_path, monkeypatch):
    """A genuinely pre-existing good backup at dest_path must survive a
    detected same-inode tampering during a LATER build attempt -- the
    module preserves a hardlink reference to it before publishing the
    new build, and restores it if the final observation fails, rather
    than leaving dest_path holding known-bad, unattested bytes with the
    original good archive unrecoverable."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")

    # A genuinely pre-existing good archive already at dest_path.
    _build(data_dir, base_dir, dest)
    with open(dest, "rb") as f:
        prior_good_bytes = f.read()
    prior_good_sha256 = hashlib.sha256(prior_good_bytes).hexdigest()

    evil_data_dir, evil_base_dir = _make_env(tmp_path / "evil_env_rollback")
    decoy_dest = str(tmp_path / "decoy_rollback.zip")
    ab.build_agent_backup(evil_data_dir, evil_base_dir, decoy_dest,
                           identity_trailers_path=NO_IDENTITY_TRAILERS, credentials_path=NO_CREDENTIALS)
    with open(decoy_dest, "rb") as f:
        decoy_bytes = f.read()

    real_replace = os.replace
    state = {"swapped": False}

    def _sabotage_replace(src, dst, *a, **kw):
        real_replace(src, dst, *a, **kw)
        if dst == dest and not state["swapped"]:
            state["swapped"] = True
            with open(dest, "r+b") as f:
                f.seek(0)
                f.write(decoy_bytes)
                f.truncate()

    monkeypatch.setattr(os, "replace", _sabotage_replace)
    with pytest.raises(ab.AgentBackupError, match="prior destination restored"):
        _build(data_dir, base_dir, dest)
    monkeypatch.undo()

    assert state["swapped"], "the sabotage never fired -- test setup is broken, not the code"
    with open(dest, "rb") as f:
        restored_bytes = f.read()
    assert restored_bytes == prior_good_bytes, "the prior good archive must be restored, not left as the decoy"
    assert hashlib.sha256(restored_bytes).hexdigest() == prior_good_sha256
    # No leftover snapshot file left behind either way.
    assert not os.path.exists(dest + ".a2f7_prior_good.tmp")


def test_publication_mismatch_with_no_prior_destination_reports_honestly(tmp_path, monkeypatch):
    """The fresh-install case: no prior archive existed at dest_path at
    all, so there is nothing to roll back to -- this must be reported
    honestly (not silently, and not as if a restore happened) rather
    than implying a recovery that didn't occur."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")

    evil_data_dir, evil_base_dir = _make_env(tmp_path / "evil_env_no_prior")
    decoy_dest = str(tmp_path / "decoy_no_prior.zip")
    ab.build_agent_backup(evil_data_dir, evil_base_dir, decoy_dest,
                           identity_trailers_path=NO_IDENTITY_TRAILERS, credentials_path=NO_CREDENTIALS)
    with open(decoy_dest, "rb") as f:
        decoy_bytes = f.read()

    real_replace = os.replace
    state = {"swapped": False}

    def _sabotage_replace(src, dst, *a, **kw):
        real_replace(src, dst, *a, **kw)
        if dst == dest and not state["swapped"]:
            state["swapped"] = True
            with open(dest, "r+b") as f:
                f.seek(0)
                f.write(decoy_bytes)
                f.truncate()

    monkeypatch.setattr(os, "replace", _sabotage_replace)
    with pytest.raises(ab.AgentBackupError, match="no prior destination existed"):
        _build(data_dir, base_dir, dest)
    monkeypatch.undo()
    assert state["swapped"], "the sabotage never fired -- test setup is broken, not the code"


def test_backup_receipt_credential_exclusion_derives_from_manifest_not_hardcoded(tmp_path):
    """Section 12: the receipt's archive_policy_credentials_excluded
    claim must be DERIVED from the verified manifest's own exclusion
    record, never hardcoded True regardless of content."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    with zipfile.ZipFile(dest, "r") as zf:
        entries = {n: zf.read(n) for n in zf.namelist()}
    manifest = json.loads(entries["manifest.json"])
    manifest["exclusions"] = [e for e in manifest["exclusions"] if e.get("logical_id") != "credentials"]
    entries["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)

    receipt = ab.build_backup_receipt(dest)
    # the archive is now unverifiable (missing the canonical exclusion
    # record) -- the receipt must FAIL rather than assert the exclusion
    # claim from an archive that can no longer prove it.
    assert receipt["verification"] == "FAIL"


def test_backup_receipt_reports_fail_on_invalid_archive(tmp_path):
    dest = str(tmp_path / "zero.zip")
    _write_manifest_only_zip(dest, _MINIMAL_MANIFEST_WITH_POLICY)
    receipt = ab.build_backup_receipt(dest)
    assert receipt["verification"] == "FAIL"
    assert receipt["errors"]


# =======================================================================
# Crash durability / publication ordering (Section 14, unchanged, locked in)
# =======================================================================

def test_atomic_write_leaves_no_partial_file_on_zip_failure(tmp_path, monkeypatch):
    """AGENT-BACKUP-RESTORE-A2R9: the builder now writes every member via
    zf.writestr(ZipInfo, m['data']) (immutable captured bytes), never
    zf.write(staged_path) -- this sabotages the writestr call instead of
    the now-unused write() method, but the invariant under test is
    unchanged: a mid-zip-write disk failure must leave no partial
    destination file and no leftover staging artifact."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")

    real_writestr = zipfile.ZipFile.writestr

    def _boom(self, zinfo_or_arcname, data, *args, **kwargs):
        name = zinfo_or_arcname.filename if isinstance(zinfo_or_arcname, zipfile.ZipInfo) else zinfo_or_arcname
        if name.endswith("prefs.json"):
            raise OSError("simulated disk error mid-zip-write")
        return real_writestr(self, zinfo_or_arcname, data, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "writestr", _boom)
    with pytest.raises(OSError):
        _build(data_dir, base_dir, dest)
    assert not os.path.exists(dest)
    leftovers = [f for f in os.listdir(tmp_path) if f.startswith(".agent_backup_")]
    assert not leftovers


# =======================================================================
# AGENT-BACKUP-RESTORE-A2F3 -- Section 16 (MANDATORY): SQLite connection-
# bound credential regression, reproducing the exact Backup Goblin
# exploit A2R3 used to defeat the previous path-based re-check.
# =======================================================================

def test_sqlite_connect_time_hardlink_swap_and_restore_still_caught(tmp_path):
    """AGENT-BACKUP-RESTORE-A2R3's exact reproduction: safe lumina.db
    exists -> sqlite3.connect(path) begins -> pathname swapped to a
    hardlink of a valid-SQLite fake credentials store -> the connection
    opens the credentials inode -> pathname restored to the safe
    lumina.db BEFORE this test's post-connect defense would run again --
    a PATH-based re-check would see the restored, safe pathname and be
    satisfied. sqlite3.connect() itself isn't a Python-level call this
    module can hook mid-execution, so the swap-and-restore is performed
    inside a monkeypatched sqlite3.connect wrapper -- exactly matching
    the granularity _snapshot_sqlite actually has available (before the
    call, after the call), which is also exactly the boundary A2R3's
    attack operates across.

    Required proof (per the mission): no archive publication, no
    credential marker leak, explicit failure/boundary rejection."""
    data_dir, base_dir = _make_env(tmp_path)
    creds_path = _make_fake_credentials_valid_sqlite(tmp_path)
    lumina_db = os.path.join(data_dir, "memory", "lumina.db")

    real_connect = sqlite3.connect
    swap_state = {"fired": False}

    def _sabotage_connect(database, *a, **kw):
        if isinstance(database, str) and lumina_db in database:
            os.remove(lumina_db)
            os.link(creds_path, lumina_db)
            conn = real_connect(database, *a, **kw)
            os.remove(lumina_db)
            safe = real_connect(lumina_db)
            for stmt in _REAL_LUMINA_DB_SCHEMA:
                safe.execute(stmt)
            safe.execute("INSERT INTO chats (id, name, created_at, updated_at) VALUES (1, 'c', 't', 't')")
            safe.commit()
            safe.close()
            swap_state["fired"] = True
            return conn
        return real_connect(database, *a, **kw)

    sqlite3.connect = _sabotage_connect
    try:
        dest = str(tmp_path / "backup.zip")
        with pytest.raises(ab.AgentBackupError, match="credential"):
            _build(data_dir, base_dir, dest, credentials_path=creds_path)
    finally:
        sqlite3.connect = real_connect

    assert swap_state["fired"], "the sabotage never fired -- test setup is broken, not the code"
    assert not os.path.exists(dest), "no archive may ever be published from a rejected connection"


def test_sqlite_connect_time_hardlink_swap_flight_recorder_still_caught(tmp_path):
    """Same reproduction as the lumina.db variant above, against
    flight_recorder.db -- the mission asks for both DB roles to be
    tested, not just the primary database."""
    data_dir, base_dir = _make_env(tmp_path)
    creds_path = _make_fake_credentials_valid_sqlite(tmp_path)
    fr_db = os.path.join(data_dir, "telemetry", "flight_recorder.db")

    real_connect = sqlite3.connect
    swap_state = {"fired": False}

    def _sabotage_connect(database, *a, **kw):
        if isinstance(database, str) and fr_db in database:
            os.remove(fr_db)
            os.link(creds_path, fr_db)
            conn = real_connect(database, *a, **kw)
            os.remove(fr_db)
            safe = real_connect(fr_db)
            safe.execute(_REAL_FLIGHT_RECORDER_DB_SCHEMA)
            safe.commit()
            safe.close()
            swap_state["fired"] = True
            return conn
        return real_connect(database, *a, **kw)

    sqlite3.connect = _sabotage_connect
    try:
        dest = str(tmp_path / "backup.zip")
        with pytest.raises(ab.AgentBackupError, match="credential"):
            _build(data_dir, base_dir, dest, credentials_path=creds_path)
    finally:
        sqlite3.connect = real_connect

    assert swap_state["fired"], "the sabotage never fired -- test setup is broken, not the code"
    assert not os.path.exists(dest), "no archive may ever be published from a rejected connection"


def test_connect_sqlite_with_bound_identity_rejects_credentials_fd_directly(tmp_path):
    """Unit-level proof of the mechanism itself: a connection whose only
    newly-opened descriptor is bound to credentials_identity is rejected,
    independent of the end-to-end build path above."""
    creds_path = tmp_path / "credentials.json"
    creds_path.write_bytes(b'{"k": "v"}')
    creds_identity = (os.stat(str(creds_path)).st_dev, os.stat(str(creds_path)).st_ino)

    def _open_creds():
        return sqlite3.connect(f"file:{creds_path}?mode=ro", uri=True)

    with pytest.raises(ab.AgentBackupError, match="credential"):
        ab._connect_sqlite_with_bound_identity(_open_creds, str(creds_path), None, creds_identity)


def test_connect_sqlite_with_bound_identity_fails_closed_when_proc_fd_missing(tmp_path, monkeypatch):
    """Section 2's explicit requirement: 'if the platform cannot prove
    connection identity: FAIL CLOSED.' Simulated by pointing the module's
    own _PROC_FD_DIR constant at a path that doesn't exist."""
    db_path = tmp_path / "solo.db"
    conn = sqlite3.connect(str(db_path))
    conn.close()
    monkeypatch.setattr(ab, "_PROC_FD_DIR", str(tmp_path / "no_such_proc_fd"))

    def _open():
        return sqlite3.connect(str(db_path))

    with pytest.raises(ab.AgentBackupError, match="unavailable"):
        ab._connect_sqlite_with_bound_identity(_open, str(db_path), None, None)


# =======================================================================
# AGENT-BACKUP-RESTORE-A2R4 (R4-B1, BLOCKER): connection attribution must
# be to THIS specific connect(), never to "somewhere in the process."
# =======================================================================

def test_newly_acquired_identities_detects_fd_number_reuse_not_just_new_numbers():
    """Direct, deterministic proof of the core diffing primitive, not
    dependent on OS-level scheduling: a fd NUMBER that already existed
    before, but is closed and reused for a DIFFERENT identity between
    the two snapshots, must count as newly-acquired -- a bare "is this fd
    number new" comparison would miss this entirely (Section 3's
    explicit 'FD number reuse' test)."""
    before = {5: (1, 100), 7: (1, 200)}
    after = {5: (1, 100), 7: (1, 999)}  # fd 7's NUMBER is not new, but its IDENTITY changed
    assert ab._newly_acquired_identities(before, after) == {(1, 999)}


def test_connect_sqlite_unrelated_resting_connection_on_expected_inode_is_not_a_hall_pass(tmp_path):
    """AGENT-BACKUP-RESTORE-A2R4's exact reproduction ('the Backup
    Goblin parked an unrelated good connection on the expected inode,
    fed the new connection attacker bytes, and the pooled identity set
    gave it a hall pass'): an unrelated, ALREADY-OPEN connection resting
    on the EXPECTED inode must not let a brand-new connection that
    actually opened DIFFERENT (attacker) bytes slip through merely
    because the expected identity happens to be present SOMEWHERE in the
    process's fd table. Only fds newly acquired by THIS open_fn() call
    may ever satisfy the check."""
    safe_path = tmp_path / "safe.db"
    attacker_path = tmp_path / "attacker.db"
    for p in (safe_path, attacker_path):
        c = sqlite3.connect(str(p))
        c.execute("CREATE TABLE t(x)")
        c.commit()
        c.close()
    expected_identity = (os.stat(str(safe_path)).st_dev, os.stat(str(safe_path)).st_ino)

    resting = sqlite3.connect(str(safe_path))  # unrelated, already open, resting on the expected inode
    try:
        def _open_attacker():
            # simulates the swap: THIS connect actually opens different bytes
            return sqlite3.connect(str(attacker_path))

        with pytest.raises(ab.AgentBackupError, match="could not prove"):
            ab._connect_sqlite_with_bound_identity(_open_attacker, str(safe_path), expected_identity, None)
    finally:
        resting.close()


def test_connect_sqlite_unrelated_resting_connection_on_other_inode_does_not_block_legitimate_connect(tmp_path):
    """The symmetric non-attack case: an unrelated connection resting on
    some OTHER (non-credentials, non-expected) inode must never cause a
    false rejection of a legitimate connect to the real expected file."""
    safe_path = tmp_path / "safe2.db"
    other_path = tmp_path / "other.db"
    for p in (safe_path, other_path):
        c = sqlite3.connect(str(p))
        c.execute("CREATE TABLE t(x)")
        c.commit()
        c.close()
    expected_identity = (os.stat(str(safe_path)).st_dev, os.stat(str(safe_path)).st_ino)

    resting = sqlite3.connect(str(other_path))
    try:
        def _open_safe():
            return sqlite3.connect(str(safe_path))

        conn = ab._connect_sqlite_with_bound_identity(_open_safe, str(safe_path), expected_identity, None)
        conn.close()
    finally:
        resting.close()


def test_connect_sqlite_multiple_unrelated_connections_still_identifies_correctly(tmp_path):
    """Several unrelated, already-open connections (one resting on the
    expected inode, one on an unrelated inode) at once must not confuse
    attribution of THIS call's own, separately and legitimately opened,
    new connection to the same expected file."""
    safe_path = tmp_path / "safe3.db"
    other_path = tmp_path / "other3.db"
    for p in (safe_path, other_path):
        c = sqlite3.connect(str(p))
        c.execute("CREATE TABLE t(x)")
        c.commit()
        c.close()
    expected_identity = (os.stat(str(safe_path)).st_dev, os.stat(str(safe_path)).st_ino)

    resting_on_expected = sqlite3.connect(str(safe_path))
    resting_on_other = sqlite3.connect(str(other_path))
    try:
        def _open_safe():
            return sqlite3.connect(str(safe_path))

        conn = ab._connect_sqlite_with_bound_identity(_open_safe, str(safe_path), expected_identity, None)
        conn.close()
    finally:
        resting_on_expected.close()
        resting_on_other.close()


def test_connect_sqlite_wal_mode_extra_descriptors_still_identify_correctly(tmp_path):
    """A WAL-mode database may cause additional sibling descriptors to be
    involved around connect time -- the mechanism must still correctly
    attribute the MAIN database file's identity among whatever fds this
    call newly acquires, rather than being confused by extras."""
    db_path = tmp_path / "wal.db"
    c = sqlite3.connect(str(db_path))
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE t(x)")
    c.execute("INSERT INTO t VALUES (1)")
    c.commit()
    c.close()
    identity = (os.stat(str(db_path)).st_dev, os.stat(str(db_path)).st_ino)

    def _open():
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    conn = ab._connect_sqlite_with_bound_identity(_open, str(db_path), identity, None)
    try:
        assert conn.execute("SELECT * FROM t").fetchall() == [(1,)]
    finally:
        conn.close()


def test_connect_sqlite_survives_concurrent_unrelated_fd_churn(tmp_path):
    """Section 3's 'concurrent FD activity during connection
    establishment': ordinary, unrelated file-descriptor churn happening
    in another thread during the connect() window must not defeat
    correct attribution of this call's own connection, as long as the
    churn never coincides with the expected/credentials identity."""
    db_path = tmp_path / "solo4.db"
    c = sqlite3.connect(str(db_path))
    c.execute("CREATE TABLE t(x)")
    c.commit()
    c.close()
    identity = (os.stat(str(db_path)).st_dev, os.stat(str(db_path)).st_ino)

    stop = threading.Event()

    def _churn():
        i = 0
        while not stop.is_set():
            p = os.path.join(str(tmp_path), f"churn_{i}.txt")
            with open(p, "w") as f:
                f.write("x")
            i += 1
            if i > 2000:
                break

    t = threading.Thread(target=_churn, daemon=True)
    t.start()
    try:
        def _open():
            return sqlite3.connect(str(db_path))

        conn = ab._connect_sqlite_with_bound_identity(_open, str(db_path), identity, None)
        conn.close()
    finally:
        stop.set()
        t.join(timeout=2)


def test_connect_sqlite_with_bound_identity_survives_source_deleted_immediately_after(tmp_path):
    """Section 3's 'source deletion after connect': identity is fixed the
    instant the underlying open() succeeds -- deleting the source file
    immediately afterward must not affect the already-established
    connection or its already-proven identity."""
    db_path = tmp_path / "solo5.db"
    c = sqlite3.connect(str(db_path))
    c.execute("CREATE TABLE t(x)")
    c.commit()
    c.close()
    identity = (os.stat(str(db_path)).st_dev, os.stat(str(db_path)).st_ino)

    def _open():
        conn = sqlite3.connect(str(db_path))
        os.remove(str(db_path))  # the instant after the underlying open() succeeded
        return conn

    conn = ab._connect_sqlite_with_bound_identity(_open, str(db_path), identity, None)
    try:
        assert conn.execute("SELECT * FROM t").fetchall() == []
    finally:
        conn.close()


def test_connect_sqlite_with_bound_identity_fails_closed_when_proc_fd_unreadable(tmp_path, monkeypatch):
    """Section 3's '/proc unreadable' (distinct from '/proc unavailable',
    already covered above): the directory exists but enumerating it
    raises -- must still fail closed, never silently proceed as if no
    descriptors existed."""
    db_path = tmp_path / "solo6.db"
    c = sqlite3.connect(str(db_path))
    c.close()
    real_listdir = os.listdir

    def _boom(path):
        if path == ab._PROC_FD_DIR:
            raise PermissionError("simulated /proc/self/fd enumeration failure")
        return real_listdir(path)

    monkeypatch.setattr(os, "listdir", _boom)

    def _open():
        return sqlite3.connect(str(db_path))

    with pytest.raises(ab.AgentBackupError, match="failed to enumerate"):
        ab._connect_sqlite_with_bound_identity(_open, str(db_path), None, None)


# =======================================================================
# Section 17 (MANDATORY): Payload-role regression
# =======================================================================

def test_verify_rejects_unrelated_healthy_sqlite_as_lumina_db(tmp_path):
    """A healthy, structurally-valid SQLite database with a correct
    hash/size, placed at state/databases/lumina.db, is still an invalid
    Agent Backup if it isn't actually Lumina's own primary database
    (AGENT-BACKUP-RESTORE-A2R3)."""
    m = dict(_MINIMAL_MANIFEST_WITH_POLICY)
    data, digest = _sqlite_bytes_and_hash(tmp_path, name="unrelated.db")
    m["members"] = [dict(_LUMINA_DB_MEMBER, sha256=digest, size=len(data))]
    dest = str(tmp_path / "unrelated_as_lumina.zip")
    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr("manifest.json", json.dumps(m))
        zf.writestr("state/databases/lumina.db", data)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("primary-database schema" in e for e in report["errors"])


def test_verify_rejects_unrelated_healthy_sqlite_as_flight_recorder_db(tmp_path):
    m = dict(_MINIMAL_MANIFEST_WITH_POLICY)
    lumina_data, lumina_digest = _real_lumina_db_bytes_and_hash(tmp_path)
    fr_data, fr_digest = _sqlite_bytes_and_hash(tmp_path, name="unrelated_fr.db")
    m["members"] = [
        dict(_LUMINA_DB_MEMBER, sha256=lumina_digest, size=len(lumina_data)),
        {
            "logical_id": "state.telemetry.flight_recorder_db", "state_class": "REQUIRED_AGENT_STATE",
            "content_kind": "sqlite_database", "archive_path": "state/telemetry/flight_recorder.db",
            "sha256": fr_digest, "size": len(fr_data), "required": True,
            "portable": "PORTABLE", "restore_policy": "overwrite_atomic",
        },
    ]
    dest = str(tmp_path / "unrelated_as_fr.zip")
    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr("manifest.json", json.dumps(m))
        zf.writestr("state/databases/lumina.db", lumina_data)
        zf.writestr("state/telemetry/flight_recorder.db", fr_data)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("Flight Recorder's schema" in e for e in report["errors"])


def test_verify_rejects_prefs_syntactically_invalid_json(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    with zipfile.ZipFile(dest, "r") as zf:
        entries = {n: zf.read(n) for n in zf.namelist()}
    manifest = json.loads(entries["manifest.json"])
    bad_bytes = b"not-json"
    bad_hash = hashlib.sha256(bad_bytes).hexdigest()
    for m in manifest["members"]:
        if m["archive_path"] == "state/preferences/prefs.json":
            m["sha256"] = bad_hash
            m["size"] = len(bad_bytes)
    entries["state/preferences/prefs.json"] = bad_bytes
    entries["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, d in entries.items():
            zf.writestr(name, d)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("preferences payload" in e and "JSON" in e for e in report["errors"])


def test_verify_rejects_prefs_wrong_top_level_json_type(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    with zipfile.ZipFile(dest, "r") as zf:
        entries = {n: zf.read(n) for n in zf.namelist()}
    manifest = json.loads(entries["manifest.json"])
    bad_bytes = json.dumps(["not", "an", "object"]).encode()
    bad_hash = hashlib.sha256(bad_bytes).hexdigest()
    for m in manifest["members"]:
        if m["archive_path"] == "state/preferences/prefs.json":
            m["sha256"] = bad_hash
            m["size"] = len(bad_bytes)
    entries["state/preferences/prefs.json"] = bad_bytes
    entries["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, d in entries.items():
            zf.writestr(name, d)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("preferences payload" in e and "top level" in e for e in report["errors"])


# =======================================================================
# Section 18: canonical semantic regression matrix -- every one of these
# must invalidate the archive.
# =======================================================================

def _base_full_manifest(tmp_path):
    """A structurally-complete, canonically-correct manifest (all three
    required-floor members + valid canonical policy records) that tests
    below mutate ONE field of at a time."""
    m = dict(_MINIMAL_MANIFEST_WITH_POLICY)
    m["compatibility"] = {
        "min_supported_format_version": {"major": 1, "minor": 0},
        "max_known_format_version": ab.MANIFEST_FORMAT_VERSION,
        "unknown_newer_major_policy": "refuse",
        "unknown_newer_minor_policy": "accept_ignore_unknown_optional_fields",
        "older_same_major_policy": "supported_with_migration_if_minor_delta",
        "older_major_mismatch_policy": "refuse_direct_to_explicit_migration_tool",
    }
    lumina_data, lumina_digest = _real_lumina_db_bytes_and_hash(tmp_path)
    fr_data, fr_digest = _real_flight_recorder_db_bytes_and_hash(tmp_path)
    prefs_data = b"{}"
    prefs_digest = hashlib.sha256(prefs_data).hexdigest()
    m["members"] = [
        dict(_LUMINA_DB_MEMBER, sha256=lumina_digest, size=len(lumina_data)),
        {
            "logical_id": "state.telemetry.flight_recorder_db", "state_class": "REQUIRED_AGENT_STATE",
            "content_kind": "sqlite_database", "archive_path": "state/telemetry/flight_recorder.db",
            "sha256": fr_digest, "size": len(fr_data), "required": True,
            "portable": "PORTABLE", "restore_policy": "overwrite_atomic",
        },
        {
            "logical_id": "state.preferences.prefs_json", "state_class": "REQUIRED_AGENT_STATE",
            "content_kind": "json", "archive_path": "state/preferences/prefs.json",
            "sha256": prefs_digest, "size": len(prefs_data), "required": True,
            "portable": "PORTABLE_WITH_REBIND",
            "restore_policy": "overwrite_then_rewrite_last_persona_path",
            "rebind_policy": "rewrite_last_persona_absolute_path_to_dest_personas_dir",
        },
    ]
    payloads = {
        "state/databases/lumina.db": lumina_data,
        "state/telemetry/flight_recorder.db": fr_data,
        "state/preferences/prefs.json": prefs_data,
    }
    return m, payloads


def _write_zip(dest, manifest, payloads):
    # AGENT-BACKUP-RESTORE-A2F10 (BLOCKER B1): strict verification now
    # requires ZIP_DEFLATED (the only method the real v1 builder ever
    # emits) -- without an explicit compression argument here,
    # zipfile.ZipFile defaults every zf.writestr() to ZIP_STORED, which
    # is no longer a builder-realistic fixture for any test using this
    # helper.
    #
    # AGENT-BACKUP-RESTORE-A2F11 (Section 21-27): every entry now goes
    # through the module's own _make_v1_zipinfo, the exact same
    # canonical-metadata construction the real builder uses -- a bare
    # zf.writestr(name, data) call leaves zipfile to fill in its OWN
    # default external_attr (0o600 << 16), which no longer matches the
    # real v1 builder's canonical 0o644 << 16 and would fail every test
    # using this fixture on that unrelated ground, not on whatever field
    # each individual test is actually mutating.
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(ab._make_v1_zipinfo("manifest.json"), json.dumps(manifest))
        for archive_path, data in payloads.items():
            zf.writestr(ab._make_v1_zipinfo(archive_path), data)


def test_base_full_manifest_fixture_itself_is_valid(tmp_path):
    """Sanity check on the fixture the whole matrix below mutates one
    field of at a time -- if THIS doesn't verify, every mutation test is
    meaningless."""
    m, payloads = _base_full_manifest(tmp_path)
    dest = str(tmp_path / "base.zip")
    _write_zip(dest, m, payloads)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is True, report["errors"]


@pytest.mark.parametrize("dropped_logical_id,dropped_path", [
    ("state.preferences.prefs_json", "state/preferences/prefs.json"),
    ("state.telemetry.flight_recorder_db", "state/telemetry/flight_recorder.db"),
])
def test_verify_rejects_full_backup_missing_a_required_floor_member(tmp_path, dropped_logical_id, dropped_path):
    """AGENT-BACKUP-RESTORE-A2R3's exact Section 3 reproduction:
    backup_mode='full', members: lumina.db only, after removing prefs and
    Flight Recorder -- verify_agent_backup returned valid:true because
    completeness was only ever enforced by the BUILD path, never
    independently by the verifier. Each required-floor member is dropped
    one at a time here (not just 'lumina.db only') to prove the floor
    check names the SPECIFIC missing member, not merely a generic zero-
    members-style failure."""
    m, payloads = _base_full_manifest(tmp_path)
    m["members"] = [mm for mm in m["members"] if mm["logical_id"] != dropped_logical_id]
    del payloads[dropped_path]
    dest = str(tmp_path / f"missing_{dropped_logical_id}.zip")
    _write_zip(dest, m, payloads)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any(dropped_logical_id in e and "full" in e for e in report["errors"])


def test_verify_rejects_full_backup_with_only_lumina_db(tmp_path):
    """The literal A2R3 reproduction: backup_mode='full' with lumina.db
    as the ONLY member."""
    m, payloads = _base_full_manifest(tmp_path)
    m["members"] = [mm for mm in m["members"] if mm["logical_id"] == "state.databases.lumina_db"]
    payloads = {"state/databases/lumina.db": payloads["state/databases/lumina.db"]}
    dest = str(tmp_path / "lumina_db_only.zip")
    _write_zip(dest, m, payloads)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("state.preferences.prefs_json" in e for e in report["errors"])
    assert any("state.telemetry.flight_recorder_db" in e for e in report["errors"])


@pytest.mark.parametrize("field,value", [
    ("backup_mode", "partial"),
    ("hash_algorithm", "md5"),
])
def test_verify_rejects_root_contract_substitution(tmp_path, field, value):
    m, payloads = _base_full_manifest(tmp_path)
    m[field] = value
    dest = str(tmp_path / f"root_{field}.zip")
    _write_zip(dest, m, payloads)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any(field in e for e in report["errors"])


@pytest.mark.parametrize("field,value", [
    ("portable", "EXCLUDED"),
    ("restore_policy", "delete_without_restore"),
    ("archive_path", "state/databases/not_lumina.db"),
])
def test_verify_rejects_lumina_db_canonical_field_substitution(tmp_path, field, value):
    m, payloads = _base_full_manifest(tmp_path)
    for member in m["members"]:
        if member["logical_id"] == "state.databases.lumina_db":
            member[field] = value
    if field == "archive_path":
        # the archive_path itself changed, so the physical zip entry must
        # move with it -- otherwise this would just trip "member missing
        # from archive" instead of exercising the canonical-field check.
        payloads[value] = payloads.pop("state/databases/lumina.db")
    dest = str(tmp_path / f"lumina_db_{field}.zip")
    _write_zip(dest, m, payloads)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("lumina_db" in e and field in e for e in report["errors"])


def test_verify_rejects_prefs_wrong_canonical_archive_path(tmp_path):
    m, payloads = _base_full_manifest(tmp_path)
    for member in m["members"]:
        if member["logical_id"] == "state.preferences.prefs_json":
            member["archive_path"] = "state/preferences/not_prefs.json"
    payloads["state/preferences/not_prefs.json"] = payloads.pop("state/preferences/prefs.json")
    dest = str(tmp_path / "prefs_wrong_path.zip")
    _write_zip(dest, m, payloads)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("prefs_json" in e and "archive_path" in e for e in report["errors"])


def test_verify_rejects_flight_recorder_required_false(tmp_path):
    m, payloads = _base_full_manifest(tmp_path)
    for member in m["members"]:
        if member["logical_id"] == "state.telemetry.flight_recorder_db":
            member["required"] = False
    dest = str(tmp_path / "fr_not_required.zip")
    _write_zip(dest, m, payloads)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("flight_recorder_db" in e and "required" in e for e in report["errors"])


@pytest.mark.parametrize("field,value", [
    ("state_class", "EXCLUDED"),
    ("restore_policy", "restore_stale_rows"),
])
def test_verify_rejects_ledger_regenerate_field_substitution(tmp_path, field, value):
    m, payloads = _base_full_manifest(tmp_path)
    m["regenerate"] = [{
        "logical_id": ab._CANONICAL_LEDGER_LOGICAL_ID,
        "state_class": "REBUILDABLE_GENERATED", "restore_policy": "always_regenerate_empty",
    }]
    m["regenerate"][0][field] = value
    dest = str(tmp_path / f"ledger_{field}.zip")
    _write_zip(dest, m, payloads)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("ledger regenerate entry" in e for e in report["errors"])


def test_verify_rejects_duplicate_ledger_regenerate(tmp_path):
    m, payloads = _base_full_manifest(tmp_path)
    good = {"logical_id": ab._CANONICAL_LEDGER_LOGICAL_ID,
            "state_class": "REBUILDABLE_GENERATED", "restore_policy": "always_regenerate_empty"}
    m["regenerate"] = [good, dict(good)]
    dest = str(tmp_path / "dup_ledger.zip")
    _write_zip(dest, m, payloads)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("exactly one canonical record" in e for e in report["errors"])


def test_verify_rejects_duplicate_contradictory_credentials_exclusions(tmp_path):
    m, payloads = _base_full_manifest(tmp_path)
    m["exclusions"] = [
        dict(ab._CANONICAL_CREDENTIALS_EXCLUSION),
        {"logical_id": "credentials", "reason": "some_other_contradictory_reason"},
    ]
    dest = str(tmp_path / "dup_creds_exclusion.zip")
    _write_zip(dest, m, payloads)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("exactly one canonical record" in e for e in report["errors"])


def _custom_tool_member(**overrides):
    base = {
        "logical_id": "state.custom_tools.evil.py", "state_class": "REQUIRED_AGENT_STATE",
        "content_kind": "python_source", "archive_path": "state/custom_tools/evil.py",
        "required": True, "portable": "PORTABLE_WITH_REBIND",
        "restore_policy": "restore_verbatim_then_quarantine_pending_reapproval",
        "rebind_policy": "quarantine_until_explicit_reapproval",
    }
    base.update(overrides)
    return base


def test_verify_rejects_custom_tool_approved_true_field(tmp_path):
    """Section 6: an unrecognized field (not just a recognized field with
    a suspicious VALUE) must also invalidate authority-bearing members --
    a hostile manifest could otherwise smuggle authority through a field
    name this module's checks don't inspect by name."""
    m, payloads = _base_full_manifest(tmp_path)
    tool_bytes = b"def foo(): pass\n"
    m["members"].append(_custom_tool_member(
        sha256=hashlib.sha256(tool_bytes).hexdigest(), size=len(tool_bytes),
        approved=True,
    ))
    payloads["state/custom_tools/evil.py"] = tool_bytes
    dest = str(tmp_path / "tool_approved_field.zip")
    _write_zip(dest, m, payloads)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("unrecognized field" in e for e in report["errors"])


def test_verify_rejects_tool_audit_autoapprove_semantics(tmp_path):
    m, payloads = _base_full_manifest(tmp_path)
    log_bytes = b'{"event": "approved"}\n'
    m["members"].append({
        "logical_id": "state.audit.tool_audit_log", "state_class": "REQUIRED_AGENT_STATE",
        "content_kind": "jsonl_log", "archive_path": "state/audit/tool_audit.log",
        "sha256": hashlib.sha256(log_bytes).hexdigest(), "size": len(log_bytes), "required": True,
        "portable": "PORTABLE_WITH_REBIND",
        "restore_policy": "restore_verbatim_historical_only_never_auto_grants_executable_approval_on_destination",
        "rebind_policy": "restore_and_autoapprove",
    })
    payloads["state/audit/tool_audit.log"] = log_bytes
    dest = str(tmp_path / "tool_audit_autoapprove.zip")
    _write_zip(dest, m, payloads)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("tool_audit_log" in e and "rebind_policy" in e for e in report["errors"])


def test_verify_rejects_tool_audit_destination_authority_field(tmp_path):
    m, payloads = _base_full_manifest(tmp_path)
    log_bytes = b'{"event": "approved"}\n'
    m["members"].append({
        "logical_id": "state.audit.tool_audit_log", "state_class": "REQUIRED_AGENT_STATE",
        "content_kind": "jsonl_log", "archive_path": "state/audit/tool_audit.log",
        "sha256": hashlib.sha256(log_bytes).hexdigest(), "size": len(log_bytes), "required": True,
        "portable": "PORTABLE_WITH_REBIND",
        "restore_policy": "restore_verbatim_historical_only_never_auto_grants_executable_approval_on_destination",
        "rebind_policy": "quarantine_until_explicit_reapproval",
        "destination_authority": "inherited",
    })
    payloads["state/audit/tool_audit.log"] = log_bytes
    dest = str(tmp_path / "tool_audit_destination_authority.zip")
    _write_zip(dest, m, payloads)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("unrecognized field" in e for e in report["errors"])


@pytest.mark.parametrize("array_field,bad_entry", [
    ("members", "just a string, not an object"),
    ("regenerate", 12345),
    ("exclusions", ["nested", "list"]),
])
def test_verify_rejects_scalar_array_entries(tmp_path, array_field, bad_entry):
    """AGENT-BACKUP-RESTORE-A2R3: a scalar appended to members[] was
    silently ignored by verification while a receipt built from the raw
    array still counted it -- must invalidate the whole archive instead."""
    m, payloads = _base_full_manifest(tmp_path)
    m[array_field] = list(m[array_field]) + [bad_entry]
    dest = str(tmp_path / f"scalar_{array_field}.zip")
    _write_zip(dest, m, payloads)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any(f"{array_field}[] entry is not" in e for e in report["errors"])


def test_receipt_never_inflates_member_count_from_scalar_entry(tmp_path):
    """The receipt itself must FAIL (not report a fabricated PASS with an
    inflated member_count) when members[] carries a scalar."""
    m, payloads = _base_full_manifest(tmp_path)
    m["members"] = list(m["members"]) + ["a sneaky scalar"]
    dest = str(tmp_path / "receipt_scalar.zip")
    _write_zip(dest, m, payloads)
    receipt = ab.build_backup_receipt(dest)
    assert receipt["verification"] == "FAIL"


# =======================================================================
# AGENT-BACKUP-RESTORE-A2R4 (Section 8/9/10/13, BLOCKER): dynamic manifest
# policy for Project bindings/chats-linkage/pending-actions was entirely
# archive-authored before this pass -- a hostile manifest could freely
# redefine state_class/portable/restore_policy/rebind_policy, or smuggle
# authority through an unrecognized field, with zero enforcement.
# =======================================================================

def _project_binding_member(**overrides):
    base = {
        "logical_id": "overlay.projects.demo.binding_json", "state_class": "REBIND_REQUIRED",
        "content_kind": "json", "archive_path": "overlay/projects/demo/binding.json",
        "required": True, "portable": "PORTABLE_WITH_REBIND",
        "restore_policy": "restore_then_require_owner_confirmation_of_root_path",
        "rebind_policy": "validate_root_exists_or_prompt_owner_to_repick",
    }
    base.update(overrides)
    return base


def _project_chats_member(**overrides):
    base = {
        "logical_id": "overlay.projects.demo.chats_json", "state_class": "REQUIRED_AGENT_STATE",
        "content_kind": "json", "archive_path": "overlay/projects/demo/chats.json",
        "required": True, "portable": "PORTABLE",
        "restore_policy": "overwrite_atomic_with_lumina_db",
    }
    base.update(overrides)
    return base


def _pending_actions_member(**overrides):
    base = {
        "logical_id": "state.audit.pending_actions.json", "state_class": "REQUIRED_AGENT_STATE",
        "content_kind": "json", "archive_path": "state/audit/pending_actions.json",
        "required": True, "portable": "PORTABLE", "restore_policy": "overwrite_atomic",
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize("field,value", [
    ("state_class", "USER_OVERLAY"),
    ("required", False),
    ("portable", "EXCLUDED"),
    ("restore_policy", "restore_and_autoexecute"),
    ("rebind_policy", "auto_trust_any_root"),
])
def test_verify_rejects_project_binding_dynamic_policy_substitution(tmp_path, field, value):
    m, payloads = _base_full_manifest(tmp_path)
    data = json.dumps({"root": "/tmp/x"}).encode()
    member = _project_binding_member(sha256=hashlib.sha256(data).hexdigest(), size=len(data))
    member[field] = value
    m["members"].append(member)
    payloads["overlay/projects/demo/binding.json"] = data
    dest = str(tmp_path / f"binding_{field}.zip")
    _write_zip(dest, m, payloads)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("binding_json" in e and field in e for e in report["errors"])


@pytest.mark.parametrize("field,value", [
    ("state_class", "EXCLUDED"),
    ("portable", "EXCLUDED"),
    ("restore_policy", "restore_and_autoexecute_with_machine_authority"),
])
def test_verify_rejects_project_chats_dynamic_policy_substitution(tmp_path, field, value):
    m, payloads = _base_full_manifest(tmp_path)
    data = json.dumps([{"chat_id": 1}]).encode()
    member = _project_chats_member(sha256=hashlib.sha256(data).hexdigest(), size=len(data))
    member[field] = value
    m["members"].append(member)
    payloads["overlay/projects/demo/chats.json"] = data
    dest = str(tmp_path / f"chats_{field}.zip")
    _write_zip(dest, m, payloads)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("chats_json" in e and field in e for e in report["errors"])


def test_verify_rejects_project_chats_autoexecute_field(tmp_path):
    m, payloads = _base_full_manifest(tmp_path)
    data = json.dumps([{"chat_id": 1}]).encode()
    m["members"].append(_project_chats_member(
        sha256=hashlib.sha256(data).hexdigest(), size=len(data), autoexecute=True,
    ))
    payloads["overlay/projects/demo/chats.json"] = data
    dest = str(tmp_path / "chats_autoexecute.zip")
    _write_zip(dest, m, payloads)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("unrecognized field" in e for e in report["errors"])


def test_verify_rejects_project_chats_machine_binding_field(tmp_path):
    """Section 10: chats.json must not be able to declare machine binding
    -- rebind_policy is a legitimate field name elsewhere (binding.json,
    custom tools, tool_audit_log) but chats.json's own v1 shape never
    carries one."""
    m, payloads = _base_full_manifest(tmp_path)
    data = json.dumps([{"chat_id": 1}]).encode()
    m["members"].append(_project_chats_member(
        sha256=hashlib.sha256(data).hexdigest(), size=len(data),
        rebind_policy="bind_to_any_destination_machine",
    ))
    payloads["overlay/projects/demo/chats.json"] = data
    dest = str(tmp_path / "chats_rebind.zip")
    _write_zip(dest, m, payloads)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("unrecognized field" in e for e in report["errors"])


@pytest.mark.parametrize("extra_field,extra_value", [
    ("approved", True),
    ("autoexecute", True),
    ("destination_authority", "owner"),
    ("owner_authorized", True),
    ("restore_and_autoexecute", True),
])
def test_verify_rejects_pending_actions_authority_smuggling_field(tmp_path, extra_field, extra_value):
    """Section 11: an imported archive must not be able to smuggle
    execution authority onto the pending-actions queue through any
    unrecognized field name."""
    m, payloads = _base_full_manifest(tmp_path)
    data = b"{}"
    m["members"].append(_pending_actions_member(
        sha256=hashlib.sha256(data).hexdigest(), size=len(data), **{extra_field: extra_value},
    ))
    payloads["state/audit/pending_actions.json"] = data
    dest = str(tmp_path / f"pending_{extra_field}.zip")
    _write_zip(dest, m, payloads)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("unrecognized field" in e for e in report["errors"])


def test_verify_rejects_pending_actions_audit_log_authority_smuggling_field(tmp_path):
    m, payloads = _base_full_manifest(tmp_path)
    data = b""
    m["members"].append({
        "logical_id": "state.audit.pending_actions_audit.log", "state_class": "REQUIRED_AGENT_STATE",
        "content_kind": "jsonl_log", "archive_path": "state/audit/pending_actions_audit.log",
        "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "required": True,
        "portable": "PORTABLE", "restore_policy": "overwrite_atomic",
        "autoapprove": True,
    })
    payloads["state/audit/pending_actions_audit.log"] = data
    dest = str(tmp_path / "pending_audit_autoapprove.zip")
    _write_zip(dest, m, payloads)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("unrecognized field" in e for e in report["errors"])


@pytest.mark.parametrize("field,value", [
    ("state_class", "USER_OVERLAY"),
    ("portable", "EXCLUDED"),
    ("restore_policy", "restore_and_autoexecute"),
])
def test_verify_rejects_pending_actions_fixed_policy_substitution(tmp_path, field, value):
    m, payloads = _base_full_manifest(tmp_path)
    data = b"{}"
    member = _pending_actions_member(sha256=hashlib.sha256(data).hexdigest(), size=len(data))
    member[field] = value
    m["members"].append(member)
    payloads["state/audit/pending_actions.json"] = data
    dest = str(tmp_path / f"pending_fixed_{field}.zip")
    _write_zip(dest, m, payloads)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("pending_actions.json" in e and field in e for e in report["errors"])


def test_verify_rejects_persona_dynamic_policy_substitution(tmp_path):
    """A representative check that the SAME generic mechanism also
    applies to the per-file overlay categories (personas/skills/
    tool_profiles/avatars/voices), not just the security-critical
    binding/chats/pending-actions families above."""
    m, payloads = _base_full_manifest(tmp_path)
    data = json.dumps({"name": "x"}).encode()
    m["members"].append({
        "logical_id": "overlay.personas.evil3.json", "state_class": "USER_OVERLAY",
        "content_kind": "json", "archive_path": "overlay/personas/evil3.json",
        "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "required": True,
        "portable": "PORTABLE", "restore_policy": "overwrite_without_baseline_check",
    })
    payloads["overlay/personas/evil3.json"] = data
    dest = str(tmp_path / "persona_policy.zip")
    _write_zip(dest, m, payloads)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("overlay.personas.evil3.json" in e and "restore_policy" in e for e in report["errors"])


def test_verify_rejects_project_md_dynamic_policy_substitution(tmp_path):
    m, payloads = _base_full_manifest(tmp_path)
    data = b"# demo\n"
    m["members"].append({
        "logical_id": "overlay.projects.demo.project_md", "state_class": "REBUILDABLE_GENERATED",
        "content_kind": "markdown", "archive_path": "overlay/projects/demo/project.md",
        "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "required": False,
        "portable": "REBUILD", "restore_policy": "restore_then_mark_stale_recommend_regenerate",
    })
    payloads["overlay/projects/demo/project.md"] = data
    dest = str(tmp_path / "project_md_policy.zip")
    _write_zip(dest, m, payloads)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("overlay.projects.demo.project_md" in e for e in report["errors"])


def test_verify_rejects_identity_trailers_canonical_field_substitution(tmp_path):
    """A fixed (non-parameterized) logical_id newly enforced by this pass
    -- Section 8's explicit 'identity_trailers metadata' entry."""
    m, payloads = _base_full_manifest(tmp_path)
    data = json.dumps({"note": "x"}).encode()
    m["members"].append({
        "logical_id": "metadata.identity_trailers", "state_class": "REQUIRED_AGENT_STATE",
        "content_kind": "json", "archive_path": "metadata/identity_trailers.json",
        "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "required": True,
        "portable": "PORTABLE", "restore_policy": "restore_verbatim_if_present_never_required_never_credentials",
    })
    payloads["metadata/identity_trailers.json"] = data
    dest = str(tmp_path / "identity_trailers_policy.zip")
    _write_zip(dest, m, payloads)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("metadata.identity_trailers" in e and "state_class" in e for e in report["errors"])


# =======================================================================
# AGENT-BACKUP-RESTORE-A2R4 (Section 12): EXCLUDED is exclusions[]'s own
# state and can never legitimately appear on a physical members[] entry.
# =======================================================================

def test_verify_rejects_physical_member_declaring_state_class_excluded(tmp_path):
    m, payloads = _base_full_manifest(tmp_path)
    data = json.dumps({"name": "evil"}).encode()
    m["members"].append({
        "logical_id": "overlay.personas.evil.json", "state_class": "EXCLUDED",
        "content_kind": "json", "archive_path": "overlay/personas/evil.json",
        "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "required": True,
        "portable": "PORTABLE", "restore_policy": "baseline_aware_merge",
    })
    payloads["overlay/personas/evil.json"] = data
    dest = str(tmp_path / "physical_excluded_state_class.zip")
    _write_zip(dest, m, payloads)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("state_class" in e and "EXCLUDED" in e for e in report["errors"])


def test_verify_rejects_physical_member_declaring_portable_excluded(tmp_path):
    m, payloads = _base_full_manifest(tmp_path)
    data = json.dumps({"name": "evil"}).encode()
    m["members"].append({
        "logical_id": "overlay.personas.evil2.json", "state_class": "USER_OVERLAY",
        "content_kind": "json", "archive_path": "overlay/personas/evil2.json",
        "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "required": True,
        "portable": "EXCLUDED", "restore_policy": "baseline_aware_merge",
    })
    payloads["overlay/personas/evil2.json"] = data
    dest = str(tmp_path / "physical_excluded_portable.zip")
    _write_zip(dest, m, payloads)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("portable" in e and "EXCLUDED" in e for e in report["errors"])


# =======================================================================
# AGENT-BACKUP-RESTORE-A2R5 (Section 16-19, BLOCKER): v1 is closed-world
# -- every physical member must belong to a recognized canonical or
# dynamic family, or the whole archive is invalid.
# =======================================================================

def test_verify_rejects_unconstrained_member_the_literal_a2r5_reproduction(tmp_path):
    """The exact AGENT-BACKUP-RESTORE-A2R5 reproduction: a member whose
    logical_id matches NO recognized family, sitting under an otherwise-
    legal namespace prefix (state/audit/), previously passed strict
    verification and inflated the receipt's rebind_required_count."""
    m, payloads = _base_full_manifest(tmp_path)
    data = b"attacker-controlled, unconstrained extra payload"
    m["members"].append({
        "logical_id": "state.audit.totally_unconstrained_thing",
        "state_class": "REQUIRED_AGENT_STATE", "content_kind": "json",
        "archive_path": "state/audit/random_extra.json",
        "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "required": True,
        "portable": "PORTABLE_WITH_REBIND", "restore_policy": "whatever_the_attacker_wants",
    })
    payloads["state/audit/random_extra.json"] = data
    dest = str(tmp_path / "unconstrained.zip")
    _write_zip(dest, m, payloads)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("does not belong to any of Lumina's recognized" in e for e in report["errors"])

    receipt = ab.build_backup_receipt(dest)
    assert receipt["verification"] == "FAIL", "the receipt must never report PASS with an inflated count from this"


@pytest.mark.parametrize("bad_logical_id,archive_path", [
    ("overlay.projects.demo.binding_json.bak", "overlay/projects/demo/binding_json.bak"),
    ("overlay.projects.demo.chats_json", "overlay/projects/demo/chats.json/child"),
    ("overlay.projects.demo.PENDING_actions.json.evil", "state/audit/pending_actions.json.evil"),
    ("OVERLAY.PROJECTS.demo.binding_json", "overlay/projects/demo/binding.json"),
])
def test_verify_rejects_near_match_dynamic_logical_ids(tmp_path, bad_logical_id, archive_path):
    """Section 18: near-matches (an appended suffix, a case-altered
    family prefix, an embedded extra path segment, an empty extracted
    name) must not inherit a recognized family's policy -- they must
    fall through to the closed-world rejection instead, not be silently
    accepted as 'schema valid, policy not enforced'."""
    m, payloads = _base_full_manifest(tmp_path)
    data = b"near-match probe payload"
    m["members"].append({
        "logical_id": bad_logical_id, "state_class": "USER_OVERLAY", "content_kind": "json",
        "archive_path": archive_path, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
        "required": True, "portable": "PORTABLE", "restore_policy": "baseline_aware_merge",
    })
    payloads[archive_path] = data
    dest = str(tmp_path / f"nearmatch_{abs(hash(bad_logical_id))}.zip")
    _write_zip(dest, m, payloads)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False, f"expected {bad_logical_id!r}/{archive_path!r} to be rejected"


def test_verify_rejects_logical_id_archive_path_mismatch_within_a_real_family(tmp_path):
    """Section 17: logical_id and archive_path must be validated
    TOGETHER, not independently -- a member whose logical_id correctly
    matches the binding_json family but whose archive_path points at a
    DIFFERENT project's binding.json must be rejected, even though both
    values individually look like well-formed, recognized shapes."""
    m, payloads = _base_full_manifest(tmp_path)
    data = json.dumps({"root": "/tmp/x"}).encode()
    m["members"].append({
        "logical_id": "overlay.projects.demo.binding_json", "state_class": "REBIND_REQUIRED",
        "content_kind": "json", "archive_path": "overlay/projects/OTHER_PROJECT/binding.json",
        "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "required": True,
        "portable": "PORTABLE_WITH_REBIND",
        "restore_policy": "restore_then_require_owner_confirmation_of_root_path",
        "rebind_policy": "validate_root_exists_or_prompt_owner_to_repick",
    })
    payloads["overlay/projects/OTHER_PROJECT/binding.json"] = data
    dest = str(tmp_path / "mismatched_pair.zip")
    _write_zip(dest, m, payloads)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("does not belong to any of Lumina's recognized" in e for e in report["errors"])


def test_is_recognized_member_accepts_every_real_family_shape():
    """Direct, exhaustive proof that _is_recognized_member correctly
    reconstructs archive_path for one real example of every family --
    guards against an off-by-one in a deriver's string-slicing silently
    rejecting genuinely valid archives."""
    cases = [
        ("state.databases.lumina_db", "state/databases/lumina.db"),
        ("state.telemetry.flight_recorder_db", "state/telemetry/flight_recorder.db"),
        ("state.preferences.prefs_json", "state/preferences/prefs.json"),
        ("state.audit.tool_audit_log", "state/audit/tool_audit.log"),
        ("state.audit.pending_actions.json", "state/audit/pending_actions.json"),
        ("state.audit.pending_actions_audit.log", "state/audit/pending_actions_audit.log"),
        ("overlay.projects.projectlist", "overlay/projects/projectlist.md"),
        ("metadata.identity_trailers", "metadata/identity_trailers.json"),
        ("metadata.shipped_baseline_hashes", "metadata/shipped_baseline_hashes.json"),
        ("state.custom_tools.my_tool.py", "state/custom_tools/my_tool.py"),
        ("state.custom_tools._pending/staged_tool.py", "state/custom_tools/_pending/staged_tool.py"),
        ("overlay.personas.lumina.json", "overlay/personas/lumina.json"),
        ("overlay.skills.rubber-duck-debugging.md", "overlay/skills/rubber-duck-debugging.md"),
        ("overlay.tool_profiles.chat.json", "overlay/tool_profiles/chat.json"),
        ("overlay.avatars.lumina.png", "overlay/avatars/lumina.png"),
        ("overlay.voices.lumina.wav", "overlay/voices/lumina.wav"),
        ("overlay.projects.demo.binding_json", "overlay/projects/demo/binding.json"),
        ("overlay.projects.demo.chats_json", "overlay/projects/demo/chats.json"),
        ("overlay.projects.demo.project_md", "overlay/projects/demo/project.md"),
        ("overlay.projects.demo.codebase_md", "overlay/projects/demo/codebase.md"),
        # a project name that itself contains dots must not confuse suffix-based extraction
        ("overlay.projects.my.dotted.project.binding_json", "overlay/projects/my.dotted.project/binding.json"),
    ]
    for logical_id, archive_path in cases:
        assert ab._is_recognized_member(logical_id, archive_path), f"{logical_id!r} -> {archive_path!r}"
        assert not ab._is_recognized_member(logical_id, archive_path + ".wrong")


# =======================================================================
# AGENT-BACKUP-RESTORE-A2R6 (Section 21-24, BLOCKER): closed-world dynamic
# families must reject impossible NESTED names a real collector (a flat
# listdir for overlay families, a flat directory-name listing for Project
# names) can never physically produce -- the previous derivers only
# checked a suffix/truthiness, not that the extracted user-controlled
# component was a single path segment.
# =======================================================================

def test_verify_rejects_impossible_nested_persona_the_literal_a2r6_reproduction(tmp_path):
    """The exact AGENT-BACKUP-RESTORE-A2R6 reproduction: logical_id
    'overlay.personas.nested/evil.json' paired with archive_path
    'overlay/personas/nested/evil.json' -- both individually 'look'
    like a personas-family member, and the OLD deriver (fname.endswith
    ('.json')) reconstructed exactly this archive_path, so the pair
    passed _is_recognized_member even though _collect_overlay_dir's own
    flat os.listdir() can never emit a nested path here."""
    m, payloads = _base_full_manifest(tmp_path)
    data = b'{"attacker": "controlled"}'
    m["members"].append({
        "logical_id": "overlay.personas.nested/evil.json", "state_class": "USER_OVERLAY",
        "content_kind": "json", "archive_path": "overlay/personas/nested/evil.json",
        "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "required": True,
        "portable": "PORTABLE", "restore_policy": "baseline_aware_merge",
    })
    payloads["overlay/personas/nested/evil.json"] = data
    dest = str(tmp_path / "nested_persona.zip")
    _write_zip(dest, m, payloads)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("does not belong to any of Lumina's recognized" in e for e in report["errors"])

    receipt = ab.build_backup_receipt(dest)
    assert receipt["verification"] == "FAIL"


def test_verify_rejects_impossible_nested_project_binding_the_literal_a2r6_reproduction(tmp_path):
    """The exact AGENT-BACKUP-RESTORE-A2R6 reproduction for Project
    families: logical_id 'overlay.projects.parent/child.binding_json'
    paired with archive_path 'overlay/projects/parent/child/binding.json'
    -- the OLD project-binding deriver only checked the extracted name
    was non-empty, so a '/' embedded in the 'project name' component
    still reconstructed exactly this nested archive_path, even though
    _project_dir_names only ever lists flat, single-segment directory
    names."""
    m, payloads = _base_full_manifest(tmp_path)
    data = json.dumps({"root": "/tmp/x"}).encode()
    m["members"].append({
        "logical_id": "overlay.projects.parent/child.binding_json", "state_class": "REBIND_REQUIRED",
        "content_kind": "json", "archive_path": "overlay/projects/parent/child/binding.json",
        "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "required": True,
        "portable": "PORTABLE_WITH_REBIND",
        "restore_policy": "restore_then_require_owner_confirmation_of_root_path",
        "rebind_policy": "validate_root_exists_or_prompt_owner_to_repick",
    })
    payloads["overlay/projects/parent/child/binding.json"] = data
    dest = str(tmp_path / "nested_binding.zip")
    _write_zip(dest, m, payloads)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("does not belong to any of Lumina's recognized" in e for e in report["errors"])


@pytest.mark.parametrize("logical_id,archive_path", [
    ("overlay.skills.nested/evil.md", "overlay/skills/nested/evil.md"),
    ("overlay.tool_profiles.nested/evil.json", "overlay/tool_profiles/nested/evil.json"),
    ("overlay.avatars.nested/evil.png", "overlay/avatars/nested/evil.png"),
    ("overlay.voices.nested/evil.wav", "overlay/voices/nested/evil.wav"),
    ("overlay.projects.parent/child.chats_json", "overlay/projects/parent/child/chats.json"),
    ("overlay.projects.parent/child.project_md", "overlay/projects/parent/child/project.md"),
    ("overlay.projects.parent/child.codebase_md", "overlay/projects/parent/child/codebase.md"),
    ("overlay.personas...json", "overlay/personas/../.json"),
    ("overlay.projects...binding_json", "overlay/projects/../binding.json"),
])
def test_verify_rejects_impossible_nested_names_across_every_flat_and_project_family(
        tmp_path, logical_id, archive_path):
    """Section 21-24: the same impossible-nesting class, proven across
    every OTHER flat overlay and Project sub-file family, not just
    personas/binding_json -- plus the degenerate '..'-as-the-extracted-
    name case (a project/file 'name' of exactly '..' is not a real
    filename either)."""
    m, payloads = _base_full_manifest(tmp_path)
    data = b"attacker-controlled payload"
    m["members"].append({
        "logical_id": logical_id, "state_class": "USER_OVERLAY", "content_kind": "json",
        "archive_path": archive_path, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
        "required": True, "portable": "PORTABLE", "restore_policy": "baseline_aware_merge",
    })
    payloads[archive_path] = data
    dest = str(tmp_path / f"nested_{abs(hash(logical_id))}.zip")
    _write_zip(dest, m, payloads)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False, f"expected {logical_id!r} / {archive_path!r} to be rejected"


def test_legitimate_weird_names_still_pass_through_the_real_collector(tmp_path):
    """Section 26: the A2R6 security fix must not regress legitimate
    user-owned naming -- spaces, multiple dots, Unicode, mixed case, and
    punctuation, built through the REAL collector (never hand-rolled),
    for both a flat overlay family (personas) and a Project name, must
    still verify and be individually recognized by _is_recognized_member.
    Confirms verifier language == collector language in both directions,
    not just 'rejects the impossible' but also 'accepts the real.'"""
    data_dir, base_dir = _make_env(tmp_path)
    weird_persona = "Über Persona 2026 (draft).v1.json"  # "Über Persona 2026 (draft).v1.json"
    with open(os.path.join(base_dir, "personas", weird_persona), "w") as f:
        json.dump({"name": "weird"}, f)
    weird_project = "My.Dotted Project - Ü"  # "My.Dotted Project - Ü"
    os.makedirs(os.path.join(base_dir, "projects", weird_project))
    with open(os.path.join(base_dir, "projects", weird_project, "project.md"), "w") as f:
        f.write("# weird\n")
    os.makedirs(os.path.join(data_dir, "projects", weird_project))
    with open(os.path.join(data_dir, "projects", weird_project, "binding.json"), "w") as f:
        json.dump({"root": "/tmp/x"}, f)
    with open(os.path.join(data_dir, "projects", weird_project, "chats.json"), "w") as f:
        f.write("[]")

    dest = str(tmp_path / "weird_names.zip")
    manifest = _build(data_dir, base_dir, dest)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is True, report["errors"]

    members = _members_by_path(manifest)
    persona_archive_path = f"overlay/personas/{weird_persona}"
    project_binding_path = f"overlay/projects/{weird_project}/binding.json"
    assert persona_archive_path in members
    assert project_binding_path in members
    assert ab._is_recognized_member(members[persona_archive_path]["logical_id"], persona_archive_path)
    assert ab._is_recognized_member(members[project_binding_path]["logical_id"], project_binding_path)


# =======================================================================
# AGENT-BACKUP-RESTORE-A2R4 (Section 16/17, R4-B5): receipt verification,
# hashing, and sizing must bind to ONE immutable capture of the archive,
# never a path reopened by name after verification already completed.
# =======================================================================

def test_receipt_hash_size_verify_bind_to_one_open_despite_pathname_replacement(tmp_path, monkeypatch):
    """The Goblin's reproduction: verify(path A) succeeds, the pathname is
    replaced with a DIFFERENT file before hash/size are computed, and the
    old code's receipt described the replacement's bytes while still
    reporting PASS. The fix reads the archive exactly once, up front --
    prove the pathname can be swapped to garbage the INSTANT after that
    one read starts, and the receipt still describes the ORIGINAL bytes."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    with open(dest, "rb") as f:
        original_bytes = f.read()
    original_hash = hashlib.sha256(original_bytes).hexdigest()
    original_size = len(original_bytes)

    real_open = os.open
    state = {"swapped": False}

    def _sabotage(path, *a, **kw):
        fd = real_open(path, *a, **kw)
        if path == dest and not state["swapped"]:
            state["swapped"] = True
            os.remove(dest)
            with open(dest, "wb") as f:
                f.write(b"not a real archive at all -- swapped in after the real fd was already bound")
        return fd

    monkeypatch.setattr(os, "open", _sabotage)
    receipt = ab.build_backup_receipt(dest)
    monkeypatch.undo()

    assert state["swapped"], "the sabotage never fired -- test setup is broken, not the code"
    assert receipt["verification"] == "PASS"
    assert receipt["archive_sha256"] == original_hash
    assert receipt["archive_size"] == original_size


def test_receipt_survives_inode_replacement_via_atomic_rename_after_open(tmp_path, monkeypatch):
    """A distinct mechanism from the remove+rewrite race above: an atomic
    os.replace() swaps the pathname's underlying INODE entirely (Section
    17's explicit 'inode replacement' ask) rather than truncating/
    rewriting the same path in place."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    with open(dest, "rb") as f:
        original_bytes = f.read()
    original_hash = hashlib.sha256(original_bytes).hexdigest()

    real_open = os.open
    state = {"swapped": False}

    def _sabotage(path, *a, **kw):
        fd = real_open(path, *a, **kw)
        if path == dest and not state["swapped"]:
            state["swapped"] = True
            decoy = dest + ".decoy"
            with open(decoy, "wb") as f:
                f.write(b"decoy bytes at a completely different inode")
            os.replace(decoy, dest)
        return fd

    monkeypatch.setattr(os, "open", _sabotage)
    receipt = ab.build_backup_receipt(dest)
    monkeypatch.undo()

    assert state["swapped"], "the sabotage never fired -- test setup is broken, not the code"
    assert receipt["verification"] == "PASS"
    assert receipt["archive_sha256"] == original_hash


# =======================================================================
# Section 19: overlay / custom-tool special-file regression -- state that
# cannot be faithfully captured must fail the backup, not vanish silently.
# =======================================================================

def test_sole_persona_replaced_with_fifo_fails_backup(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    persona = os.path.join(base_dir, "personas", "lumina.json")
    os.remove(persona)
    os.mkfifo(persona)
    dest = str(tmp_path / "backup.zip")
    try:
        with pytest.raises(ab.AgentBackupError, match="SECURITY_REJECTED_STATE|not a regular file"):
            _build(data_dir, base_dir, dest)
        assert not os.path.exists(dest)
    finally:
        os.remove(persona)  # a bare FIFO would otherwise hang tmp_path cleanup on some platforms


def test_sole_persona_replaced_with_unix_socket_fails_backup(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    persona = os.path.join(base_dir, "personas", "lumina.json")
    os.remove(persona)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(persona)
        dest = str(tmp_path / "backup.zip")
        with pytest.raises(ab.AgentBackupError, match="SECURITY_REJECTED_STATE|not a regular file"):
            _build(data_dir, base_dir, dest)
        assert not os.path.exists(dest)
    finally:
        sock.close()
        if os.path.exists(persona):
            os.remove(persona)


def test_skill_markdown_replaced_with_fifo_fails_backup(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    skill = os.path.join(base_dir, "skills", "rubber-duck-debugging.md")
    os.remove(skill)
    os.mkfifo(skill)
    dest = str(tmp_path / "backup.zip")
    try:
        with pytest.raises(ab.AgentBackupError):
            _build(data_dir, base_dir, dest)
        assert not os.path.exists(dest)
    finally:
        os.remove(skill)


def test_custom_tool_replaced_with_special_file_fails_backup(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    tool = os.path.join(data_dir, "custom_tools", "my_tool.py")
    os.remove(tool)
    os.mkfifo(tool)
    dest = str(tmp_path / "backup.zip")
    try:
        with pytest.raises(ab.AgentBackupError):
            _build(data_dir, base_dir, dest)
        assert not os.path.exists(dest)
    finally:
        os.remove(tool)


def test_custom_tool_replaced_with_symlink_fails_backup(tmp_path):
    """AGENT-BACKUP-RESTORE-A2R4 (Section 5/25): superseded from A2F3's
    own warn-and-exclude treatment of a symlink here -- a required
    custom-tool source discovered as a symlink now fails the whole
    backup, exactly like the special-file case, and (separately) the
    symlink target is still never leaked into the archive."""
    data_dir, base_dir = _make_env(tmp_path)
    sensitive = tmp_path / "sensitive_elsewhere.py"
    sensitive.write_text("# should never be archived\n")
    tool = os.path.join(data_dir, "custom_tools", "my_tool.py")
    os.remove(tool)
    os.symlink(str(sensitive), tool)
    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError, match="SECURITY_REJECTED_STATE|symlink"):
        _build(data_dir, base_dir, dest)
    assert not os.path.exists(dest)


# =======================================================================
# AGENT-BACKUP-RESTORE-A2R4 (Section 4/6/7/25): the required-state
# special-file matrix, extended beyond personas/skills/custom-tools to
# tool_audit.log, pending_actions*, and Project chats.json/binding.json
# -- none of these were checked for a special-file/symlink substitution
# before staging previously began.
# =======================================================================

def test_tool_audit_log_replaced_with_fifo_fails_backup(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    audit = os.path.join(data_dir, "memory", "tool_audit.log")
    os.remove(audit)
    os.mkfifo(audit)
    dest = str(tmp_path / "backup.zip")
    try:
        with pytest.raises(ab.AgentBackupError, match="SECURITY_REJECTED_STATE"):
            _build(data_dir, base_dir, dest)
        assert not os.path.exists(dest)
    finally:
        os.remove(audit)  # a bare FIFO would otherwise hang tmp_path cleanup on some platforms


def test_tool_audit_log_replaced_with_symlink_fails_backup(tmp_path):
    """The Goblin's own reproduction: tool_audit.log's REPLACE-with-FIFO
    was already caught above; a symlink at the same load-bearing position
    (Section 6: 'no dereference, no hang, no omission') must fail
    identically, not fall through to the old warn-and-exclude path."""
    data_dir, base_dir = _make_env(tmp_path)
    audit = os.path.join(data_dir, "memory", "tool_audit.log")
    decoy = tmp_path / "decoy_audit.log"
    decoy.write_text("not the real audit log")
    os.remove(audit)
    os.symlink(str(decoy), audit)
    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError, match="SECURITY_REJECTED_STATE"):
        _build(data_dir, base_dir, dest)
    assert not os.path.exists(dest)


def test_tool_audit_log_broken_symlink_fails_backup_not_treated_as_absent(tmp_path):
    """A broken symlink (target does not exist) is not the same as
    genuine absence -- os.path.exists() alone would say False and the
    old code would silently skip it as 'not present.' lstat() (used by
    the reject-check) still sees the symlink itself and must escalate."""
    data_dir, base_dir = _make_env(tmp_path)
    audit = os.path.join(data_dir, "memory", "tool_audit.log")
    os.remove(audit)
    os.symlink(str(tmp_path / "does_not_exist_at_all.log"), audit)
    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError, match="SECURITY_REJECTED_STATE"):
        _build(data_dir, base_dir, dest)
    assert not os.path.exists(dest)


def test_pending_actions_json_replaced_with_symlink_fails_backup(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    pending = os.path.join(data_dir, "memory", "pending_actions.json")
    decoy = tmp_path / "decoy_pending.json"
    decoy.write_text("{}")
    os.remove(pending)
    os.symlink(str(decoy), pending)
    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError, match="SECURITY_REJECTED_STATE"):
        _build(data_dir, base_dir, dest)
    assert not os.path.exists(dest)


def test_project_chats_json_replaced_with_symlink_fails_backup(tmp_path):
    """Section 7's explicit ask: a state-bearing chats.json symlink must
    fail the whole backup, and this must hold regardless of whether a
    matching BASE_DIR/projects/<name> directory exists at all."""
    data_dir, base_dir = _make_env(tmp_path)
    chats = os.path.join(data_dir, "projects", "demo", "chats.json")
    decoy = tmp_path / "decoy_chats.json"
    decoy.write_text("[]")
    os.remove(chats)
    os.symlink(str(decoy), chats)
    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError, match="SECURITY_REJECTED_STATE"):
        _build(data_dir, base_dir, dest)
    assert not os.path.exists(dest)


def test_project_chats_json_replaced_with_fifo_fails_backup(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    chats = os.path.join(data_dir, "projects", "demo", "chats.json")
    os.remove(chats)
    os.mkfifo(chats)
    dest = str(tmp_path / "backup.zip")
    try:
        with pytest.raises(ab.AgentBackupError, match="SECURITY_REJECTED_STATE"):
            _build(data_dir, base_dir, dest)
        assert not os.path.exists(dest)
    finally:
        os.remove(chats)


def test_project_binding_json_replaced_with_symlink_fails_backup(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    binding = os.path.join(data_dir, "projects", "demo", "binding.json")
    decoy = tmp_path / "decoy_binding.json"
    decoy.write_text("{}")
    os.remove(binding)
    os.symlink(str(decoy), binding)
    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError, match="SECURITY_REJECTED_STATE"):
        _build(data_dir, base_dir, dest)
    assert not os.path.exists(dest)


def test_projectlist_md_replaced_with_symlink_fails_backup(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    projectlist = os.path.join(base_dir, "projects", "projectlist.md")
    decoy = tmp_path / "decoy_projectlist.md"
    decoy.write_text("# fake\n")
    os.remove(projectlist)
    os.symlink(str(decoy), projectlist)
    dest = str(tmp_path / "backup.zip")
    with pytest.raises(ab.AgentBackupError, match="SECURITY_REJECTED_STATE"):
        _build(data_dir, base_dir, dest)
    assert not os.path.exists(dest)


# =======================================================================
# Section 20: baseline runtime validation regressions -- provenance is
# trusted only when authenticated against real git objects.
# =======================================================================

def test_baseline_nonexistent_git_commit_falls_back_to_unknown(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    baseline = _real_baseline_for(base_dir)
    baseline["git_commit"] = "f" * 40  # well-formed sha, does not exist in this repo
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(baseline))

    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest, baseline_hashes_path=str(baseline_path))
    members = _members_by_path(manifest)
    assert members["overlay/personas/lumina.json"]["provenance"] == "unknown"
    assert any("could not be authenticated against" in w for w in manifest["warnings"])


def test_baseline_different_real_commit_falls_back_to_unknown(tmp_path):
    """The commit resolves (it's real) but the claimed hash doesn't match
    what's actually at that commit for that path -- a forged hash map
    riding on top of a genuine, unrelated commit."""
    data_dir, base_dir = _make_env(tmp_path)
    baseline = _real_baseline_for(base_dir)
    # a second commit that changes the persona -- baseline still claims
    # the FIRST commit's hash, but points git_commit at the SECOND
    persona_path = os.path.join(base_dir, "personas", "lumina.json")
    with open(persona_path, "w") as f:
        json.dump({"name": "Lumina", "system_prompt": "second commit content"}, f)
    subprocess.run(["git", "add", "-A"], cwd=base_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "second"], cwd=base_dir, check=True)
    second_commit = subprocess.run(
        ["git", "-C", base_dir, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    baseline["git_commit"] = second_commit  # real commit, but files{} still holds the FIRST commit's hash
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(baseline))

    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest, baseline_hashes_path=str(baseline_path))
    members = _members_by_path(manifest)
    assert members["overlay/personas/lumina.json"]["provenance"] == "unknown"


def test_baseline_ancestor_commit_with_unrelated_later_commit_still_authenticates(tmp_path):
    """AGENT-BACKUP-RESTORE-A2R5 (PRIORITY ZERO)/A2F5's new provenance
    model, Section 3 scenario 'C': a real release checkpoint necessarily
    advances HEAD past whatever commit the baseline artifact declares as
    its provenance anchor (the artifact cannot declare the SHA of the
    commit that contains it -- a git fixed-point impossibility). A later
    commit that changes UNRELATED files (not any baseline-owned path)
    must not invalidate an otherwise-correct baseline -- this is the
    exact case A2F4's own exact-HEAD-match design got wrong, proven live
    against a disposable simulation of the real release-checkpoint
    lifecycle during the A2R5 review."""
    data_dir, base_dir = _make_env(tmp_path)
    baseline = _real_baseline_for(base_dir)  # generated at the first (so far only) commit
    anchor_commit = baseline["git_commit"]
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(baseline))

    # A later commit -- e.g. the actual checkpoint that commits the
    # baseline artifact and the campaign's own code -- that never
    # touches any baseline-owned path (personas/skills/avatars/voices/
    # tool_profiles/projectlist.md).
    with open(os.path.join(base_dir, "unrelated_marker.txt"), "w") as f:
        f.write("unrelated later commit content")
    subprocess.run(["git", "add", "-A"], cwd=base_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "later unrelated commit"], cwd=base_dir, check=True)
    current_head = subprocess.run(
        ["git", "-C", base_dir, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert current_head != anchor_commit

    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest, baseline_hashes_path=str(baseline_path))
    members = _members_by_path(manifest)
    assert members["overlay/personas/lumina.json"]["provenance"] == "shipped_baseline_unmodified"


def test_baseline_ancestor_commit_with_changed_baseline_owned_file_rejected_as_stale(tmp_path):
    """Section 3 scenario 'D': the ancestor relation alone is NOT
    sufficient (A2R5's own explicit warning against a bare
    merge-base --is-ancestor fix) -- if a LATER, legitimate commit
    changes a file the baseline itself claims to describe, the baseline
    is stale and must be rejected, not silently trusted just because its
    anchor commit is a real ancestor of current HEAD."""
    data_dir, base_dir = _make_env(tmp_path)
    baseline = _real_baseline_for(base_dir)
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(baseline))

    # A later, real commit changes an ACTUAL baseline-owned file.
    persona_path = os.path.join(base_dir, "personas", "lumina.json")
    with open(persona_path, "w") as f:
        json.dump({"name": "Lumina", "system_prompt": "changed in a later legitimate commit"}, f)
    subprocess.run(["git", "add", "-A"], cwd=base_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "persona updated"], cwd=base_dir, check=True)

    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest, baseline_hashes_path=str(baseline_path))
    members = _members_by_path(manifest)
    assert members["overlay/personas/lumina.json"]["provenance"] == "unknown"
    assert any("stale" in w for w in manifest["warnings"])


def test_baseline_release_checkpoint_lifecycle_A_through_D(tmp_path):
    """AGENT-BACKUP-RESTORE-A2F5 Section 3's full required lifecycle
    simulation, in one disposable repo:

        A = release before Agent Backup checkpoint
        baseline generated from A
        B = commit containing the artifact itself (the real checkpoint)
            -> baseline must still authenticate (fixes A2R5's Priority
               Zero finding: A2F4's exact-HEAD design failed here)
        C = later commit changing unrelated code only
            -> baseline must still authenticate
        D = later commit changing a baseline-owned persona/skill asset
            -> old baseline must be rejected as stale
    """
    data_dir, base_dir = _make_env(tmp_path)

    # A: release before the checkpoint.
    _git_commit_all(base_dir)
    commit_a = subprocess.run(
        ["git", "-C", base_dir, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    baseline = ab.generate_baseline_hashes(base_dir, commit=commit_a)
    assert baseline["git_commit"] == commit_a

    # B: the checkpoint commit itself, containing the baseline artifact.
    baseline_path = os.path.join(base_dir, "metadata", "shipped_baseline_hashes.json")
    os.makedirs(os.path.dirname(baseline_path), exist_ok=True)
    with open(baseline_path, "w") as f:
        json.dump(baseline, f)
    subprocess.run(["git", "add", "-A"], cwd=base_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "B: checkpoint containing the baseline artifact"], cwd=base_dir, check=True)
    dest = str(tmp_path / "backup_B.zip")
    manifest = _build(data_dir, base_dir, dest, baseline_hashes_path=baseline_path)
    assert _members_by_path(manifest)["overlay/personas/lumina.json"]["provenance"] == "shipped_baseline_unmodified", \
        "B: baseline must authenticate immediately after the checkpoint commit that contains it"

    # C: a later commit touching only unrelated code.
    with open(os.path.join(base_dir, "unrelated_code.py"), "w") as f:
        f.write("# unrelated\n")
    subprocess.run(["git", "add", "-A"], cwd=base_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "C: unrelated code change"], cwd=base_dir, check=True)
    dest = str(tmp_path / "backup_C.zip")
    manifest = _build(data_dir, base_dir, dest, baseline_hashes_path=baseline_path)
    assert _members_by_path(manifest)["overlay/personas/lumina.json"]["provenance"] == "shipped_baseline_unmodified", \
        "C: baseline must still authenticate after an unrelated later commit"

    # D: a later commit changing a baseline-owned asset.
    persona_path = os.path.join(base_dir, "personas", "lumina.json")
    with open(persona_path, "w") as f:
        json.dump({"name": "Lumina", "system_prompt": "D: changed"}, f)
    subprocess.run(["git", "add", "-A"], cwd=base_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "D: persona changed"], cwd=base_dir, check=True)
    dest = str(tmp_path / "backup_D.zip")
    manifest = _build(data_dir, base_dir, dest, baseline_hashes_path=baseline_path)
    assert _members_by_path(manifest)["overlay/personas/lumina.json"]["provenance"] == "unknown", \
        "D: the old baseline must be rejected as stale once a baseline-owned file changed"


def test_baseline_dev_descendant_lifecycle(tmp_path):
    """Section 3/24's dev-mirror scenario: B = release commit (with the
    baseline artifact already committed), E = a dev-local-only
    descendant commit. If dev hasn't touched any baseline-owned file,
    the SAME artifact must still authenticate on the dev descendant --
    proving Section 3's 'ordinary dev operation gets trusted shipped
    provenance' requirement without mutating the real ~/lumina repo (a
    disposable repo stands in for it)."""
    data_dir, base_dir = _make_env(tmp_path)
    _git_commit_all(base_dir)
    commit_a = subprocess.run(
        ["git", "-C", base_dir, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    baseline = ab.generate_baseline_hashes(base_dir, commit=commit_a)
    baseline_path = os.path.join(base_dir, "metadata", "shipped_baseline_hashes.json")
    os.makedirs(os.path.dirname(baseline_path), exist_ok=True)
    with open(baseline_path, "w") as f:
        json.dump(baseline, f)
    subprocess.run(["git", "add", "-A"], cwd=base_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "B: release commit with baseline artifact"], cwd=base_dir, check=True)

    # E: a dev-only descendant commit that does not touch any
    # baseline-owned file.
    with open(os.path.join(base_dir, "dev_only_file.py"), "w") as f:
        f.write("# dev-local only\n")
    subprocess.run(["git", "add", "-A"], cwd=base_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "E: dev-local descendant"], cwd=base_dir, check=True)

    dest = str(tmp_path / "backup_E.zip")
    manifest = _build(data_dir, base_dir, dest, baseline_hashes_path=baseline_path)
    assert _members_by_path(manifest)["overlay/personas/lumina.json"]["provenance"] == "shipped_baseline_unmodified"

    # If dev THEN changes a baseline-owned file, the same artifact must
    # be rejected as stale on the dev tree too.
    persona_path = os.path.join(base_dir, "personas", "lumina.json")
    with open(persona_path, "w") as f:
        json.dump({"name": "Lumina", "system_prompt": "dev-side edit"}, f)
    subprocess.run(["git", "add", "-A"], cwd=base_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "F: dev-local persona edit"], cwd=base_dir, check=True)
    dest2 = str(tmp_path / "backup_F.zip")
    manifest2 = _build(data_dir, base_dir, dest2, baseline_hashes_path=baseline_path)
    assert _members_by_path(manifest2)["overlay/personas/lumina.json"]["provenance"] == "unknown"


def test_baseline_dirty_working_tree_does_not_invalidate_matching_baseline(tmp_path):
    """Section 19/21: an uncommitted, unrelated local edit sitting on top
    of a checkout must not spuriously redefine or invalidate an otherwise
    HEAD-matching baseline -- provenance is about git OBJECT identity,
    not about whatever happens to be dirty in the working tree."""
    data_dir, base_dir = _make_env(tmp_path)
    baseline = _real_baseline_for(base_dir)
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(baseline))
    # dirty the working tree AFTER generating the baseline -- HEAD itself
    # does not move, and this file is outside every category this module
    # collects, so it cannot incidentally affect anything else either.
    with open(os.path.join(base_dir, "scratch_uncommitted.txt"), "w") as f:
        f.write("not committed")

    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest, baseline_hashes_path=str(baseline_path))
    members = _members_by_path(manifest)
    assert members["overlay/personas/lumina.json"]["provenance"] == "shipped_baseline_unmodified"


def test_shipped_baseline_artifact_authenticates_through_full_load_path(tmp_path):
    """Section 22: re-verify the ACTUAL candidate metadata/shipped_
    baseline_hashes.json end-to-end through the full _load_baseline_hashes
    path a real build would use (not just the lower-level
    _verify_baseline_against_git helper already exercised by
    test_baseline_valid_artifact_matching_current_release_is_trusted)."""
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    shipped_path = os.path.join(base_dir, "metadata", "shipped_baseline_hashes.json")
    if not os.path.isdir(os.path.join(base_dir, ".git")) or not os.path.isfile(shipped_path):
        pytest.skip("not running inside a release checkout with a shipped baseline")
    with open(shipped_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    files, warning = ab._load_baseline_hashes(shipped_path, raw.get("lumina_version"), base_dir)
    assert warning is None, warning
    assert files == raw["files"]


def test_baseline_hash_algorithm_not_sha256_falls_back_to_unknown(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    baseline = _real_baseline_for(base_dir)
    baseline["hash_algorithm"] = "md5"
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(baseline))

    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest, baseline_hashes_path=str(baseline_path))
    members = _members_by_path(manifest)
    assert members["overlay/personas/lumina.json"]["provenance"] == "unknown"


def test_baseline_tampered_file_hash_falls_back_to_unknown(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    baseline = _real_baseline_for(base_dir)
    baseline["files"]["personas/lumina.json"] = "a" * 64  # forged, doesn't match the real git blob
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(baseline))

    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest, baseline_hashes_path=str(baseline_path))
    members = _members_by_path(manifest)
    assert members["overlay/personas/lumina.json"]["provenance"] == "unknown"


def test_baseline_malformed_files_map_falls_back_to_unknown(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    baseline = _real_baseline_for(base_dir)
    baseline["files"] = "not-a-dict-at-all"
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(baseline))

    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest, baseline_hashes_path=str(baseline_path))
    members = _members_by_path(manifest)
    assert members["overlay/personas/lumina.json"]["provenance"] == "unknown"


def test_baseline_unknown_overlay_path_falls_back_to_unknown(tmp_path):
    """A path claimed in files{} that falls outside the allowed shipped-
    overlay namespace (_BASELINE_CATEGORIES) -- e.g. reaching into
    state/databases/ -- is fabricated by construction and must not be
    trusted even if it happens to resolve at that commit."""
    data_dir, base_dir = _make_env(tmp_path)
    baseline = _real_baseline_for(base_dir)
    baseline["files"]["state/databases/lumina.db"] = "a" * 64
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(baseline))

    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest, baseline_hashes_path=str(baseline_path))
    members = _members_by_path(manifest)
    assert members["overlay/personas/lumina.json"]["provenance"] == "unknown"


def test_baseline_missing_git_metadata_context_falls_back_to_unknown(tmp_path):
    """base_dir is never git-initialized at all -- 'no-git installations:
    do not invent trust' (Section 10)."""
    data_dir, base_dir = _make_env(tmp_path)
    assert not os.path.isdir(os.path.join(base_dir, ".git"))
    persona_path = os.path.join(base_dir, "personas", "lumina.json")
    baseline = _wrap_baseline({"personas/lumina.json": ab._sha256_file(persona_path)})
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(baseline))

    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest, baseline_hashes_path=str(baseline_path))
    members = _members_by_path(manifest)
    assert members["overlay/personas/lumina.json"]["provenance"] == "unknown"
    assert any("could not be authenticated against" in w for w in manifest["warnings"])


def test_baseline_valid_artifact_matching_current_release_is_trusted(tmp_path):
    """The positive case, proven against the REAL release checkout (not
    a synthetic one) -- if the real generator + real verification chain
    is broken, this is the test that would catch it."""
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if not os.path.isdir(os.path.join(base_dir, ".git")):
        pytest.skip("not running inside a git checkout")
    real_baseline = ab.generate_baseline_hashes(base_dir)
    ok, reason = ab._verify_baseline_against_git(base_dir, real_baseline["git_commit"], real_baseline["files"])
    assert ok is True, reason


# =======================================================================
# Section 21: portable namespace regressions -- Windows trailing-dot/
# space and reserved-device-name ambiguity.
# =======================================================================

@pytest.mark.parametrize("bad_path", [
    "overlay/skills/duck.md.",
    "overlay/skills/quack.md ",
    "overlay/projects/demo./project.md",
    "overlay/projects/demo /project.md",
])
def test_verify_rejects_windows_trailing_dot_or_space(tmp_path, bad_path):
    m, payloads = _base_full_manifest(tmp_path)
    m["members"].append({
        "logical_id": f"overlay.namespace_probe.{abs(hash(bad_path))}", "state_class": "USER_OVERLAY",
        "content_kind": "markdown", "archive_path": bad_path,
        "sha256": "a" * 64, "size": 0, "required": True,
        "portable": "PORTABLE", "restore_policy": "baseline_aware_merge",
    })
    payloads[bad_path] = b""
    dest = str(tmp_path / f"winpath_{abs(hash(bad_path))}.zip")
    _write_zip(dest, m, payloads)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False, f"expected {bad_path!r} to be rejected"
    assert any("Windows" in e for e in report["errors"])


@pytest.mark.parametrize("reserved", ["CON", "con", "PRN", "COM1", "LPT1", "NUL"])
def test_verify_rejects_windows_reserved_device_names(tmp_path, reserved):
    m, payloads = _base_full_manifest(tmp_path)
    bad_path = f"overlay/skills/{reserved}.md"
    m["members"].append({
        "logical_id": f"overlay.namespace_probe.{reserved}", "state_class": "USER_OVERLAY",
        "content_kind": "markdown", "archive_path": bad_path,
        "sha256": "a" * 64, "size": 0, "required": True,
        "portable": "PORTABLE", "restore_policy": "baseline_aware_merge",
    })
    payloads[bad_path] = b""
    dest = str(tmp_path / f"windevice_{reserved}.zip")
    _write_zip(dest, m, payloads)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False, f"expected {reserved!r} to be rejected"
    assert any("reserved Windows device name" in e for e in report["errors"])


# =======================================================================
# Security boundary: no code path anywhere near credentials
# =======================================================================

def test_module_never_imports_credentials_store():
    tree = ast.parse(inspect.getsource(ab))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "secrets" not in alias.name, f"unexpected import: {alias.name}"
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert "secrets" not in node.module, f"unexpected import: {node.module}"


def test_tests_never_touch_real_machine_state():
    """A guard on the guards: confirm the test module itself never
    references the real credentials/identity_trailers default paths
    without an explicit override in the same call."""
    test_source = open(__file__, encoding="utf-8").read()
    assert "NO_IDENTITY_TRAILERS" in test_source
    assert "NO_CREDENTIALS" in test_source


# =======================================================================
# AGENT-BACKUP-RESTORE-A2R8/A2F8 -- four release-gating BLOCKERs, each
# attacking the SEAM between two individually-correct operations rather
# than either operation alone:
#   B1 verification and immutable capture used different archive snapshots
#   B2 baseline archived payload and control/provenance evidence used
#      different snapshots
#   B3 rollback trusted a mutable, unverified recovery pathname
#   B4 strict v1 accepted ZIP prefixes / concatenated archives
# See core/agent_backup.py's module docstring and
# AGENT_BACKUP_RESTORE_A2F8_IMPLEMENTATION_NOTE_2026-09-09.md for the
# full mechanism each fix below regression-tests.
# =======================================================================

def test_b1_capture_and_verify_archive_opens_target_path_exactly_once(tmp_path, monkeypatch):
    """The literal AGENT-BACKUP-RESTORE-A2R8 B1 reproduction: the OLD
    _build_agent_backup_core called verify_agent_backup(tmp_path) (which
    opens tmp_path, verifies it, and closes its own handle internally)
    and THEN separately reopened tmp_path BY NAME a second time to
    capture built_data -- a pathname swapped in that gap let a DIFFERENT,
    independently verifier-valid archive B inherit archive A's already-
    completed PASS verdict. Proves _capture_and_verify_archive now opens
    its target path exactly ONCE for the whole capture+verify sequence --
    there is no reopen-by-name window left to race at all, and (as a
    belt-and-suspenders check) that if a reopen ever WERE reintroduced,
    the returned bytes/hash/manifest would visibly describe the wrong
    archive rather than silently passing."""
    data_dir, base_dir = _make_env(tmp_path)
    dest_a = str(tmp_path / "a.zip")
    _build(data_dir, base_dir, dest_a)
    evil_data_dir, evil_base_dir = _make_env(tmp_path / "evil_b1")
    dest_b = str(tmp_path / "b.zip")
    ab.build_agent_backup(evil_data_dir, evil_base_dir, dest_b,
                           identity_trailers_path=NO_IDENTITY_TRAILERS, credentials_path=NO_CREDENTIALS)
    with open(dest_b, "rb") as f:
        b_bytes = f.read()
    with open(dest_a, "rb") as f:
        a_bytes = f.read()

    target = str(tmp_path / "target.zip")
    shutil.copyfile(dest_a, target)

    real_open = os.open
    opens = {"count": 0}

    def _counting_open(path, *a, **kw):
        if path == target:
            opens["count"] += 1
            if opens["count"] == 2:
                # If _capture_and_verify_archive ever reopened the path a
                # second time, THIS is exactly where the classic A2R8 B1
                # attack would swap A for B mid-flight.
                with open(target, "wb") as g:
                    g.write(b_bytes)
        return real_open(path, *a, **kw)

    monkeypatch.setattr(os, "open", _counting_open)
    data, sha256, size, manifest = ab._capture_and_verify_archive(
        target, {}, str(tmp_path), "adversarial capture")
    monkeypatch.undo()

    assert opens["count"] == 1, (
        f"expected exactly one os.open() of the captured path, observed {opens['count']} -- "
        f"a second open reopens exactly the verify-then-reopen gap AGENT-BACKUP-RESTORE-A2R8 "
        f"exploited"
    )
    assert data == a_bytes
    assert sha256 == hashlib.sha256(a_bytes).hexdigest()
    assert size == len(a_bytes)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert json.loads(zf.read("manifest.json")) == manifest


def test_b1_build_result_manifest_derives_from_the_same_captured_bytes(tmp_path):
    """Section 4: every historical field _build_agent_backup_core returns
    -- manifest, built_sha256, built_size -- must derive from the ONE
    immutable captured object, not the pre-serialization Python dict
    built before zipping. Proves the manifest build_agent_backup returns
    is byte-for-byte identical to manifest.json as it actually exists
    inside the published archive."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest)
    with zipfile.ZipFile(dest) as zf:
        archived_manifest = json.loads(zf.read("manifest.json"))
    assert manifest == archived_manifest


def test_b2_baseline_path_opened_at_most_once_and_archive_matches_provenance(tmp_path, monkeypatch):
    """AGENT-BACKUP-RESTORE-A2R8 B2 reproduction: the OLD build staged
    shipped_baseline_hashes.json into the archive via one open, then
    separately called _load_baseline_hashes(baseline_hashes_path, ...),
    reopening the LIVE path a second time for the provenance-control
    decision applied to every other USER_OVERLAY member. Swap the file
    between what WOULD have been those two opens and an archived, Git-
    authenticated A could coexist with a completely different B actually
    driving provenance classification. Proves baseline_hashes_path is
    opened at most once during a build, and that the archived baseline
    payload and the provenance decision it produces always agree (never
    a hybrid of two different snapshots)."""
    data_dir, base_dir = _make_env(tmp_path)
    baseline_a = _real_baseline_for(base_dir)
    baseline_path = str(tmp_path / "baseline.json")
    with open(baseline_path, "w") as f:
        json.dump(baseline_a, f)

    # B: identical file/commit but a hash_algorithm the module will never
    # trust -- if THIS were what drove the provenance decision instead of
    # the archived A, every USER_OVERLAY member's provenance would
    # silently fall back to 'unknown' instead of 'shipped_baseline_
    # unmodified'.
    baseline_b = dict(baseline_a)
    baseline_b["hash_algorithm"] = "md5"

    real_os_open = os.open
    opens = {"count": 0}

    def _counting_open(path, *a, **kw):
        if path == baseline_path:
            opens["count"] += 1
            if opens["count"] == 2:
                with open(baseline_path, "w") as g:
                    json.dump(baseline_b, g)
        return real_os_open(path, *a, **kw)

    monkeypatch.setattr(os, "open", _counting_open)
    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest, baseline_hashes_path=baseline_path)
    monkeypatch.undo()

    assert opens["count"] == 1, (
        f"expected baseline_hashes_path to be opened at most once during a build, observed "
        f"{opens['count']} -- a second open reopens exactly the gap AGENT-BACKUP-RESTORE-A2R8 "
        f"exploited"
    )
    members = _members_by_path(manifest)
    assert members["overlay/personas/lumina.json"]["provenance"] == "shipped_baseline_unmodified"
    with zipfile.ZipFile(dest) as zf:
        archived_baseline = json.loads(zf.read("metadata/shipped_baseline_hashes.json"))
    assert archived_baseline["hash_algorithm"] == "sha256", (
        "the archived baseline payload must be A (matching the provenance decision above), "
        "never B -- both must come from the same single captured snapshot"
    )


def test_b2_load_baseline_hashes_standalone_entry_point_still_works(tmp_path):
    """The standalone, path-based _load_baseline_hashes (now a thin
    wrapper delegating to _parse_baseline_hashes) must still work exactly
    as before for callers outside a build -- e.g. a future inspection
    tool, or this suite's own test_shipped_baseline_artifact_authenticates_
    through_full_load_path."""
    data_dir, base_dir = _make_env(tmp_path)
    baseline = _real_baseline_for(base_dir)
    baseline_path = str(tmp_path / "baseline.json")
    with open(baseline_path, "w") as f:
        json.dump(baseline, f)
    files, warning = ab._load_baseline_hashes(baseline_path, baseline["lumina_version"], base_dir)
    assert warning is None
    assert files == baseline["files"]


def test_b3_rollback_leaves_no_predictable_or_reusable_recovery_pathname(tmp_path, monkeypatch):
    """AGENT-BACKUP-RESTORE-A2R8 B3 reproduction: the OLD rollback design
    preserved the prior destination via a bare os.link() to a PREDICTABLE
    sibling path (dest_path + '.a2f7_prior_good.tmp') -- a reusable,
    guessable filename, not a hash-verified immutable snapshot. Proves
    that after a full sabotage-and-rollback cycle, dest_dir contains
    nothing with a predictable/fixed naming scheme -- only the original
    published archive itself."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    with open(dest, "rb") as f:
        prior_good_bytes = f.read()

    evil_data_dir, evil_base_dir = _make_env(tmp_path / "evil_env_b3_predictable")
    decoy_dest = str(tmp_path / "decoy_b3_predictable.zip")
    ab.build_agent_backup(evil_data_dir, evil_base_dir, decoy_dest,
                           identity_trailers_path=NO_IDENTITY_TRAILERS, credentials_path=NO_CREDENTIALS)
    with open(decoy_dest, "rb") as f:
        decoy_bytes = f.read()

    real_replace = os.replace
    state = {"swapped": False}

    def _sabotage_replace(src, dst, *a, **kw):
        real_replace(src, dst, *a, **kw)
        if dst == dest and not state["swapped"]:
            state["swapped"] = True
            with open(dest, "r+b") as f:
                f.seek(0)
                f.write(decoy_bytes)
                f.truncate()

    before_files = {n for n in os.listdir(tmp_path) if os.path.isfile(str(tmp_path / n))}
    monkeypatch.setattr(os, "replace", _sabotage_replace)
    with pytest.raises(ab.AgentBackupError, match="prior destination restored"):
        _build(data_dir, base_dir, dest)
    monkeypatch.undo()

    assert state["swapped"], "the sabotage never fired -- test setup is broken, not the code"
    with open(dest, "rb") as f:
        assert f.read() == prior_good_bytes

    # Only files this specific build call created beside dest_path matter
    # here -- this module never creates anything with a predictable/
    # reusable name for rollback purposes, only randomly-suffixed
    # .agent_backup_* temps (which a successful publish/rollback always
    # consumes or removes).
    after_files = {n for n in os.listdir(tmp_path) if os.path.isfile(str(tmp_path / n))}
    new_files = after_files - before_files
    assert not new_files, f"unexpected fixed-name leftover file(s) beside dest_path: {new_files}"
    assert not os.path.exists(str(tmp_path / (os.path.basename(dest) + ".a2f7_prior_good.tmp")))


def test_b3_rollback_authority_is_in_memory_bytes_not_a_filesystem_artifact(tmp_path, monkeypatch):
    """AGENT-BACKUP-RESTORE-A2R8 B3's deeper point: rollback authority is
    the immutable PriorArchiveSnapshot bytes/hash captured in memory
    BEFORE publication even begins, never a filesystem artifact whose
    presence something else could delete or race. Proves rollback still
    succeeds correctly even when every stray '.agent_backup_*' temp file
    in dest_dir is deleted out from under it immediately before the
    publishing replace lands."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    with open(dest, "rb") as f:
        prior_good_bytes = f.read()

    evil_data_dir, evil_base_dir = _make_env(tmp_path / "evil_env_b3_deletion")
    decoy_dest = str(tmp_path / "decoy_b3_deletion.zip")
    ab.build_agent_backup(evil_data_dir, evil_base_dir, decoy_dest,
                           identity_trailers_path=NO_IDENTITY_TRAILERS, credentials_path=NO_CREDENTIALS)
    with open(decoy_dest, "rb") as f:
        decoy_bytes = f.read()

    real_replace = os.replace
    state = {"swapped": False}

    def _sabotage_replace(src, dst, *a, **kw):
        if dst == dest:
            # Delete every stray temp file in dest_dir (except the one
            # about to be replaced) right before the real replace lands
            # -- proving rollback does not depend on any of them.
            for n in os.listdir(tmp_path):
                p = str(tmp_path / n)
                if n.startswith(".agent_backup_") and p != src:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
        real_replace(src, dst, *a, **kw)
        if dst == dest and not state["swapped"]:
            state["swapped"] = True
            with open(dest, "r+b") as f:
                f.seek(0)
                f.write(decoy_bytes)
                f.truncate()

    monkeypatch.setattr(os, "replace", _sabotage_replace)
    with pytest.raises(ab.AgentBackupError, match="prior destination restored"):
        _build(data_dir, base_dir, dest)
    monkeypatch.undo()

    assert state["swapped"], "the sabotage never fired -- test setup is broken, not the code"
    with open(dest, "rb") as f:
        assert f.read() == prior_good_bytes


def test_b3_rollback_failure_preserves_recovery_artifact_with_expected_hash(tmp_path, monkeypatch):
    """Section 16: if BOTH the new build's own publication AND the
    immutable-prior-bytes rollback fail, the prior good archive's bytes
    must be preserved to a freshly-written recovery artifact (never
    silently lost), and the raised error must name it and its expected
    sha256 -- never claim 'restored' when it wasn't."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    with open(dest, "rb") as f:
        prior_good_bytes = f.read()
    prior_good_sha256 = hashlib.sha256(prior_good_bytes).hexdigest()

    evil_data_dir, evil_base_dir = _make_env(tmp_path / "evil_env_b3_rollback_fail")
    decoy_dest = str(tmp_path / "decoy_b3_rollback_fail.zip")
    ab.build_agent_backup(evil_data_dir, evil_base_dir, decoy_dest,
                           identity_trailers_path=NO_IDENTITY_TRAILERS, credentials_path=NO_CREDENTIALS)
    with open(decoy_dest, "rb") as f:
        decoy_bytes = f.read()

    real_replace = os.replace
    state = {"replace_calls_to_dest": 0}

    def _sabotage_replace(src, dst, *a, **kw):
        if dst != dest:
            return real_replace(src, dst, *a, **kw)
        state["replace_calls_to_dest"] += 1
        if state["replace_calls_to_dest"] == 1:
            # The forward publish's own replace -- let it land, then
            # immediately corrupt the destination in place so the
            # post-publish content check fails and rollback triggers.
            real_replace(src, dst, *a, **kw)
            with open(dest, "r+b") as f:
                f.seek(0)
                f.write(decoy_bytes)
                f.truncate()
            return
        # The ROLLBACK's own replace -- force it to fail outright.
        raise OSError("simulated disk failure during rollback replace")

    monkeypatch.setattr(os, "replace", _sabotage_replace)
    with pytest.raises(ab.AgentBackupError) as excinfo:
        _build(data_dir, base_dir, dest)
    monkeypatch.undo()

    assert state["replace_calls_to_dest"] == 2
    message = str(excinfo.value)
    assert "could NOT be restored" in message
    assert prior_good_sha256 in message

    recovery_candidates = [
        n for n in os.listdir(tmp_path) if n.startswith(".agent_backup_ROLLBACK_FAILED_RECOVERY_")
    ]
    assert len(recovery_candidates) == 1, recovery_candidates
    with open(str(tmp_path / recovery_candidates[0]), "rb") as f:
        recovered_bytes = f.read()
    assert recovered_bytes == prior_good_bytes
    assert hashlib.sha256(recovered_bytes).hexdigest() == prior_good_sha256


def test_b3_existing_unverifiable_destination_blocks_build_and_is_preserved(tmp_path):
    """Section 12-13 (BLOCKER B3): an existing destination that cannot be
    strictly verified as a genuine Agent Backup must block the build
    entirely, BEFORE any publication step runs -- 'preserving an unknown
    existing backup is preferable to publishing over it without
    recoverable rollback.' The unverifiable prior content must survive
    completely untouched."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    garbage = b"not an agent backup archive at all, just garbage bytes"
    with open(dest, "wb") as f:
        f.write(garbage)

    with pytest.raises(ab.AgentBackupError, match="existing destination archive"):
        _build(data_dir, base_dir, dest)

    with open(dest, "rb") as f:
        assert f.read() == garbage, "the unverifiable existing destination must be left completely untouched"


def test_b3_existing_valid_destination_is_captured_and_replaced_normally(tmp_path):
    """The ordinary, non-adversarial case: rebuilding over an EXISTING,
    genuinely valid backup must still succeed normally -- B3's new
    prior-capture-and-verify step must not falsely reject a real prior
    destination."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    manifest2 = _build(data_dir, base_dir, dest)
    assert manifest2["format"] == ab.MANIFEST_FORMAT
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is True, report["errors"]


def _extract_one_local_record(archive_bytes: bytes) -> bytes:
    """Returns the raw bytes of exactly one complete local file header +
    its compressed payload from `archive_bytes` -- for constructing an
    injected 'extra unreferenced local record' elsewhere in this
    archive's own byte stream."""
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zf:
        info = min(zf.infolist(), key=lambda i: i.header_offset)
    local = ab._parse_local_header(archive_bytes, info.header_offset)
    total = local["header_size"] + local["comp_size"]
    return archive_bytes[info.header_offset:info.header_offset + total]


def _splice_unreferenced_local_record(archive_bytes: bytes, injected_record: bytes) -> bytes:
    """Inserts `injected_record` (a complete, real local file header plus
    its compressed payload, taken from some OTHER archive) immediately
    before archive_bytes' own central directory, and patches the EOCD's
    cd_offset field so the central directory itself still parses
    perfectly -- every ORIGINAL member still found at its own correct,
    unchanged offset -- while the injected record is referenced by
    NOTHING in the central directory at all. This is the clean,
    Section-24-literal 'extra unreferenced local-file record' attack,
    distinct from merely shifting every subsequent member's physical
    position."""
    idx = archive_bytes.rfind(ab._EOCD_SIGNATURE)
    _sig, _dn, _dcd, _et, _te, cd_size, cd_offset = struct.unpack(
        "<4sHHHHII", archive_bytes[idx:idx + 20])
    spliced = bytearray(archive_bytes[:cd_offset] + injected_record + archive_bytes[cd_offset:])
    new_idx = idx + len(injected_record)
    struct.pack_into("<I", spliced, new_idx + 16, cd_offset + len(injected_record))
    return bytes(spliced)


def test_b4_leading_prefix_bytes_rejected(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    with open(dest, "rb") as f:
        real_bytes = f.read()
    report = ab.verify_agent_backup(io.BytesIO(b"self-extracting-stub-shaped-junk" + real_bytes))
    assert report["valid"] is False
    assert any("does not start at byte 0" in e for e in report["errors"])


def test_b4_single_leading_byte_rejected(tmp_path):
    """The minimal case -- even ONE leading byte must be rejected, not
    just a conveniently large prefix."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    with open(dest, "rb") as f:
        real_bytes = f.read()
    report = ab.verify_agent_backup(io.BytesIO(b"\x00" + real_bytes))
    assert report["valid"] is False
    assert any("does not start at byte 0" in e for e in report["errors"])


def test_b4_concatenated_archives_rejected(tmp_path):
    """A complete, independently valid ZIP A followed immediately by a
    complete, independently valid ZIP B -- Lumina's own format never has
    more than one archive's worth of bytes."""
    data_dir, base_dir = _make_env(tmp_path)
    dest_a = str(tmp_path / "a.zip")
    _build(data_dir, base_dir, dest_a)
    evil_data_dir, evil_base_dir = _make_env(tmp_path / "evil_b4_concat")
    dest_b = str(tmp_path / "b.zip")
    ab.build_agent_backup(evil_data_dir, evil_base_dir, dest_b,
                           identity_trailers_path=NO_IDENTITY_TRAILERS, credentials_path=NO_CREDENTIALS)
    with open(dest_a, "rb") as f:
        a_bytes = f.read()
    with open(dest_b, "rb") as f:
        b_bytes = f.read()

    report = ab.verify_agent_backup(io.BytesIO(a_bytes + b_bytes))
    assert report["valid"] is False
    assert any("does not start at byte 0" in e for e in report["errors"])

    # The reverse order must be rejected too -- not merely a one-sided check.
    report = ab.verify_agent_backup(io.BytesIO(b_bytes + a_bytes))
    assert report["valid"] is False
    assert any("does not start at byte 0" in e for e in report["errors"])


def test_b4_extra_unreferenced_local_record_rejected(tmp_path):
    """Section 24's literal attack: a real, complete local file header
    plus payload spliced into the archive's byte stream, with the
    central directory patched to still parse perfectly around it (every
    real member still exactly where it always was) -- the injected
    record itself is referenced by nothing. Only the physical-layout
    tiling check (never zipfile's own already-decoded member list) can
    catch this."""
    data_dir, base_dir = _make_env(tmp_path)
    dest_a = str(tmp_path / "a.zip")
    _build(data_dir, base_dir, dest_a)
    evil_data_dir, evil_base_dir = _make_env(tmp_path / "evil_b4_extra")
    dest_b = str(tmp_path / "b.zip")
    ab.build_agent_backup(evil_data_dir, evil_base_dir, dest_b,
                           identity_trailers_path=NO_IDENTITY_TRAILERS, credentials_path=NO_CREDENTIALS)
    with open(dest_a, "rb") as f:
        a_bytes = f.read()
    with open(dest_b, "rb") as f:
        b_bytes = f.read()

    injected = _extract_one_local_record(b_bytes)
    spliced = _splice_unreferenced_local_record(a_bytes, injected)

    # Sanity: zipfile itself still happily parses every REAL member fine
    # (proving this isn't caught by a coincidental corruption elsewhere).
    with zipfile.ZipFile(io.BytesIO(spliced)) as zf:
        assert zf.testzip() is None
        assert json.loads(zf.read("manifest.json"))["format"] == ab.MANIFEST_FORMAT

    report = ab.verify_agent_backup(io.BytesIO(spliced))
    assert report["valid"] is False
    assert any("unaccounted gap" in e or "unreferenced" in e for e in report["errors"])


def test_b4_zip_comment_still_rejected_alongside_new_structural_checks(tmp_path):
    """Regression guard: the new physical-structure check must not have
    replaced or weakened the existing A2R7 trailing-bytes/comment check."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    with zipfile.ZipFile(dest, "r") as zf:
        entries = {n: zf.read(n) for n in zf.namelist()}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        for name, data in entries.items():
            out.writestr(name, data)
        out.comment = b"not part of lumina's v1 format"
    report = ab.verify_agent_backup(io.BytesIO(buf.getvalue()))
    assert report["valid"] is False
    assert any("ZIP comment" in e for e in report["errors"])


def test_b4_local_central_filename_mismatch_rejected(tmp_path):
    """Section 22-23: a local file header's filename must agree with the
    central directory's declared filename for that same physical member
    -- a mismatch could otherwise induce a reader to interpret a
    different physical member than the one the central directory
    describes."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    with open(dest, "rb") as f:
        original = bytearray(f.read())

    with zipfile.ZipFile(io.BytesIO(bytes(original))) as zf:
        info = min(zf.infolist(), key=lambda i: i.header_offset)
    local = ab._parse_local_header(bytes(original), info.header_offset)
    fname_offset = info.header_offset + ab._LOCAL_HEADER_SIZE
    assert len(local["fname"]) > 0
    original[fname_offset] ^= 0x20  # flip a bit in the local header's own filename bytes

    report = ab.verify_agent_backup(io.BytesIO(bytes(original)))
    assert report["valid"] is False
    assert any("does not match the central directory" in e for e in report["errors"])


def test_b4_genuine_builder_archive_still_verifies_valid(tmp_path):
    """Sanity anchor for the whole B4 matrix: an untampered, genuinely
    build_agent_backup-produced archive must still pass every new
    physical-structure check -- these checks validate Lumina's own
    controlled v1 subset, they must never reject the builder's own real
    output."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is True, report["errors"]


def test_b4_multi_disk_zip_rejected(tmp_path):
    """Section 20: Lumina's builder never produces a multi-disk/split
    ZIP -- an EOCD declaring one must be rejected outright."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    with open(dest, "rb") as f:
        original = bytearray(f.read())
    idx = bytes(original).rfind(ab._EOCD_SIGNATURE)
    struct.pack_into("<H", original, idx + 4, 1)  # disk_number = 1
    report = ab.verify_agent_backup(io.BytesIO(bytes(original)))
    assert report["valid"] is False
    assert any("multi-disk" in e for e in report["errors"])


# =======================================================================
# AGENT-BACKUP-RESTORE-A2R9 -- Immutable Member Pipeline, Verified
# Recovery Artifacts, Strict JSON & ZIP64 Contract
# =======================================================================

def test_staged_persona_file_mutated_after_capture_does_not_affect_archive(tmp_path):
    """AGENT-BACKUP-RESTORE-A2R9 (Section 2-6, BLOCKER B1): the exact
    reproduction A2R9 used against the OLD architecture -- capture a
    physical member's bytes, then mutate the STAGED file itself (not the
    live source -- that seam was already closed by A2F7/A2F8's own
    stability-capture proof at the SOURCE) same-inode, in place, before
    the archive is written. Under the old pipeline this reached the
    archive (a second reopen of staged_path for zf.write). Under A2F9,
    m['data'] is authoritative from the moment _stage_physical_state_file
    returns -- the staged file on disk is transport/cache only and must
    never be reread."""
    data_dir, base_dir = _make_env(tmp_path)
    target = os.path.join(base_dir, "personas", "lumina.json")
    original_content = open(target, "rb").read()

    real_stage = ab._stage_physical_state_file

    def _mutate_staged_after_capture(source_path, *args, **kwargs):
        data, staged_path = real_stage(source_path, *args, **kwargs)
        if source_path == target and staged_path is not None:
            with open(staged_path, "r+b") as f:
                f.seek(0)
                f.write(b'{"name": "MUTATED STAGED FILE, NOT SOURCE"}')
                f.truncate()
        return data, staged_path

    ab._stage_physical_state_file = _mutate_staged_after_capture
    try:
        dest = str(tmp_path / "backup.zip")
        manifest = _build(data_dir, base_dir, dest)
    finally:
        ab._stage_physical_state_file = real_stage

    with zipfile.ZipFile(dest) as zf:
        archived = zf.read("overlay/personas/lumina.json")
    assert archived == original_content, "archive contains post-capture staged-file mutation, not the captured bytes"
    assert b"MUTATED" not in archived
    members = _members_by_path(manifest)
    assert members["overlay/personas/lumina.json"]["sha256"] == hashlib.sha256(original_content).hexdigest()


def test_staged_persona_file_deleted_after_capture_does_not_affect_archive(tmp_path):
    """The staging file is transport/cache only (Section 4) -- deleting
    it entirely after capture must have zero effect on the build, since
    nothing downstream ever reopens it by name again."""
    data_dir, base_dir = _make_env(tmp_path)
    target = os.path.join(base_dir, "personas", "lumina.json")
    original_content = open(target, "rb").read()

    real_stage = ab._stage_physical_state_file

    def _delete_staged_after_capture(source_path, *args, **kwargs):
        data, staged_path = real_stage(source_path, *args, **kwargs)
        if source_path == target and staged_path is not None:
            os.remove(staged_path)
        return data, staged_path

    ab._stage_physical_state_file = _delete_staged_after_capture
    try:
        dest = str(tmp_path / "backup.zip")
        manifest = _build(data_dir, base_dir, dest)
    finally:
        ab._stage_physical_state_file = real_stage

    with zipfile.ZipFile(dest) as zf:
        archived = zf.read("overlay/personas/lumina.json")
    assert archived == original_content
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is True, report["errors"]


def test_flight_recorder_db_staged_file_corrupted_after_capture_does_not_affect_archive(tmp_path, monkeypatch):
    """The SQLite equivalent of the persona test above, for the SECOND
    database member (flight_recorder.db) -- Section 10 explicitly asks
    for both databases, not only lumina.db."""
    data_dir, base_dir = _make_env(tmp_path)
    real_snapshot = ab._snapshot_sqlite
    corrupted = {"done": False}

    def _corrupt_after_snapshot(source_path, staged_path, *args, **kwargs):
        data = real_snapshot(source_path, staged_path, *args, **kwargs)
        if source_path.endswith("flight_recorder.db") and not corrupted["done"]:
            corrupted["done"] = True
            with open(staged_path, "r+b") as f:
                f.seek(100)
                f.write(b"\xff" * 40)
        return data

    monkeypatch.setattr(ab, "_snapshot_sqlite", _corrupt_after_snapshot)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    monkeypatch.undo()

    assert corrupted["done"], "the sabotage never fired -- test setup is broken, not the code"
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is True, report["errors"]
    with zipfile.ZipFile(dest) as zf:
        archived = zf.read("state/telemetry/flight_recorder.db")
    conn = sqlite3.connect(":memory:")
    conn.deserialize(archived)
    assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    conn.close()


def test_immutable_member_pipeline_survives_same_inode_atomic_replacement(tmp_path):
    """Section 6's full mutation matrix, atomic-replacement variant: the
    staged file's inode itself is replaced (os.replace with a
    freshly-written different file, not an in-place write) after
    capture -- must be exactly as inert as an in-place mutation."""
    data_dir, base_dir = _make_env(tmp_path)
    target = os.path.join(base_dir, "personas", "lumina.json")
    original_content = open(target, "rb").read()

    real_stage = ab._stage_physical_state_file

    def _atomic_replace_staged_after_capture(source_path, *args, **kwargs):
        data, staged_path = real_stage(source_path, *args, **kwargs)
        if source_path == target and staged_path is not None:
            decoy = staged_path + ".decoy"
            with open(decoy, "wb") as f:
                f.write(b'{"name": "DECOY VIA ATOMIC REPLACE"}')
            os.replace(decoy, staged_path)
        return data, staged_path

    ab._stage_physical_state_file = _atomic_replace_staged_after_capture
    try:
        dest = str(tmp_path / "backup.zip")
        _build(data_dir, base_dir, dest)
    finally:
        ab._stage_physical_state_file = real_stage

    with zipfile.ZipFile(dest) as zf:
        archived = zf.read("overlay/personas/lumina.json")
    assert archived == original_content
    assert b"DECOY" not in archived


# -----------------------------------------------------------------------
# Recovery artifact verification (Section 12-16)
# -----------------------------------------------------------------------

def _make_real_archive_bytes(tmp_path, suffix=""):
    data_dir, base_dir = _make_env(tmp_path / f"recovery_fixture_env{suffix}")
    dest = str(tmp_path / f"recovery_fixture{suffix}.zip")
    _build(data_dir, base_dir, dest)
    with open(dest, "rb") as f:
        return f.read()


def test_preserve_rollback_failure_artifact_reports_verified_on_clean_success(tmp_path):
    """Direct unit test of _preserve_rollback_failure_artifact's happy
    path: given genuinely valid Agent Backup archive bytes, the result
    must report verified_at_preservation=True, an observed_sha256 that
    matches expected, and a path whose on-disk content is byte-identical
    to what was preserved."""
    data = _make_real_archive_bytes(tmp_path)
    expected_sha256 = hashlib.sha256(data).hexdigest()
    dest_dir = str(tmp_path)
    dest_dir_real = os.path.realpath(dest_dir)

    result = ab._preserve_rollback_failure_artifact(data, dest_dir, dest_dir_real, {}, expected_sha256)

    assert result["verified_at_preservation"] is True
    assert result["observed_sha256"] == expected_sha256
    assert result["path"] is not None
    with open(result["path"], "rb") as f:
        assert f.read() == data
    assert os.path.basename(result["path"]).startswith(".agent_backup_ROLLBACK_FAILED_RECOVERY_")
    assert not os.path.basename(result["path"]).endswith(".part")


def test_preserve_rollback_failure_artifact_reports_unverified_on_post_finalize_corruption(tmp_path, monkeypatch):
    """Section 14's mutation matrix: a same-inode corruption landing
    AFTER the recovery artifact is atomically finalized (but before/
    during this function's own re-observation step) must produce
    verified_at_preservation=False and an UNVERIFIED note -- never a
    false 'recovery preserved' claim -- while still preserving the
    (now-known-bad) bytes on disk rather than deleting the last
    remaining copy."""
    data = _make_real_archive_bytes(tmp_path)
    expected_sha256 = hashlib.sha256(data).hexdigest()
    dest_dir = str(tmp_path)
    dest_dir_real = os.path.realpath(dest_dir)

    real_fsync_dir = ab._best_effort_fsync_dir

    def _corrupt_during_directory_fsync(dir_path):
        # Fires right after finalization, before re-observation -- the
        # natural injection point for "corrupted after finalize, before
        # verification" from Section 14's matrix.
        ok = real_fsync_dir(dir_path)
        for name in os.listdir(dir_path):
            if name.startswith(".agent_backup_ROLLBACK_FAILED_RECOVERY_") and name.endswith(".zip"):
                path = os.path.join(dir_path, name)
                with open(path, "r+b") as f:
                    f.seek(50)
                    f.write(b"\xff" * 20)
        return ok

    monkeypatch.setattr(ab, "_best_effort_fsync_dir", _corrupt_during_directory_fsync)
    result = ab._preserve_rollback_failure_artifact(data, dest_dir, dest_dir_real, {}, expected_sha256)
    monkeypatch.undo()

    assert result["verified_at_preservation"] is False
    assert result["path"] is not None, "the corrupted-but-existing artifact must still be reported, not silently lost"
    assert "UNVERIFIED" in result["note"]
    assert os.path.exists(result["path"]), "a partial/unverified last-resort artifact must never be deleted"


def test_preserve_rollback_failure_artifact_reports_unverified_on_non_archive_bytes(tmp_path):
    """If `data` hash-matches `expected_sha256` (proving content-fidelity
    end to end) but is not actually a valid Agent Backup archive, the
    strict re-verification step must still catch it -- verified_at_
    preservation must be False, never True merely because the hash
    matched."""
    data = b"not a real agent backup archive at all, just filler bytes"
    expected_sha256 = hashlib.sha256(data).hexdigest()
    dest_dir = str(tmp_path)
    dest_dir_real = os.path.realpath(dest_dir)

    result = ab._preserve_rollback_failure_artifact(data, dest_dir, dest_dir_real, {}, expected_sha256)

    assert result["verified_at_preservation"] is False
    assert result["observed_sha256"] == expected_sha256
    assert "UNVERIFIED" in result["note"] and "Agent Backup archive" in result["note"]
    assert os.path.exists(result["path"])


def test_preserve_rollback_failure_artifact_reports_unverified_on_hash_mismatch(tmp_path):
    """If the caller's own expected_sha256 does not match `data` at all
    (a defensive/adversarial-input case), the function must report the
    mismatch rather than ever claiming verified_at_preservation=True."""
    data = _make_real_archive_bytes(tmp_path)
    wrong_expected = "0" * 64
    dest_dir = str(tmp_path)
    dest_dir_real = os.path.realpath(dest_dir)

    result = ab._preserve_rollback_failure_artifact(data, dest_dir, dest_dir_real, {}, wrong_expected)

    assert result["verified_at_preservation"] is False
    assert result["observed_sha256"] != wrong_expected
    assert "does not match" in result["note"]


def test_b3_rollback_failure_message_reports_verified_preservation(tmp_path, monkeypatch):
    """Integration-level companion to test_b3_rollback_failure_preserves_
    recovery_artifact_with_expected_hash: the raised error message for a
    genuine (non-corrupted) recovery must now say VERIFIED-preserved, not
    merely name a path and a hash with no verification claim at all."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    with open(dest, "rb") as f:
        prior_good_bytes = f.read()

    evil_data_dir, evil_base_dir = _make_env(tmp_path / "evil_env_verified_msg")
    decoy_dest = str(tmp_path / "decoy_verified_msg.zip")
    ab.build_agent_backup(evil_data_dir, evil_base_dir, decoy_dest,
                           identity_trailers_path=NO_IDENTITY_TRAILERS, credentials_path=NO_CREDENTIALS)
    with open(decoy_dest, "rb") as f:
        decoy_bytes = f.read()

    real_replace = os.replace
    state = {"n": 0}

    def _sabotage_replace(src, dst, *a, **kw):
        if dst != dest:
            return real_replace(src, dst, *a, **kw)
        state["n"] += 1
        if state["n"] == 1:
            real_replace(src, dst, *a, **kw)
            with open(dest, "r+b") as f:
                f.seek(0)
                f.write(decoy_bytes)
                f.truncate()
            return
        raise OSError("simulated disk failure during rollback replace")

    monkeypatch.setattr(os, "replace", _sabotage_replace)
    with pytest.raises(ab.AgentBackupError) as excinfo:
        _build(data_dir, base_dir, dest)
    monkeypatch.undo()

    message = str(excinfo.value)
    assert "VERIFIED-preserved" in message
    recovery_candidates = [
        n for n in os.listdir(tmp_path)
        if n.startswith(".agent_backup_ROLLBACK_FAILED_RECOVERY_") and n.endswith(".zip")
    ]
    assert len(recovery_candidates) == 1, recovery_candidates
    with open(tmp_path / recovery_candidates[0], "rb") as f:
        assert f.read() == prior_good_bytes


# -----------------------------------------------------------------------
# Strict JSON decoder (Section 17-20)
# -----------------------------------------------------------------------

def test_strict_json_loads_rejects_duplicate_key_at_root():
    with pytest.raises(ValueError):
        ab._strict_json_loads('{"a": 1, "a": 2}')


def test_strict_json_loads_rejects_duplicate_key_nested():
    with pytest.raises(ValueError):
        ab._strict_json_loads('{"members": [{"logical_id": "x", "logical_id": "y"}]}')


def test_strict_json_loads_rejects_duplicate_key_in_bytes_input():
    with pytest.raises(ValueError):
        ab._strict_json_loads(b'{"format_version": {"major": 1, "major": 2}}')


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_strict_json_loads_rejects_non_finite_constants(token):
    with pytest.raises(ValueError):
        ab._strict_json_loads(f'{{"size": {token}}}')


def test_strict_json_loads_accepts_ordinary_well_formed_json():
    assert ab._strict_json_loads('{"a": 1, "b": [1, 2, 3], "c": {"d": true}}') == \
        {"a": 1, "b": [1, 2, 3], "c": {"d": True}}


def test_verify_rejects_manifest_with_duplicate_root_level_key(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    with zipfile.ZipFile(dest) as zf:
        manifest_text = zf.read("manifest.json").decode("utf-8")
        other = {n: zf.read(n) for n in zf.namelist() if n != "manifest.json"}
    # Inject a duplicate "backup_mode" key at the root by literal text
    # surgery -- a hand-crafted attack a hostile archive builder could
    # produce, which json.loads (last-key-wins) would silently accept.
    assert manifest_text.count('"backup_mode"') == 1
    tampered = manifest_text.replace(
        '"backup_mode": "full",', '"backup_mode": "full", "backup_mode": "partial",', 1
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        for name, blob in other.items():
            out.writestr(name, blob)
        out.writestr("manifest.json", tampered)
    report = ab.verify_agent_backup(io.BytesIO(buf.getvalue()))
    assert report["valid"] is False
    assert any("not valid JSON" in e for e in report["errors"])


def test_verify_rejects_manifest_with_duplicate_member_field_key(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    with zipfile.ZipFile(dest) as zf:
        manifest_text = zf.read("manifest.json").decode("utf-8")
        other = {n: zf.read(n) for n in zf.namelist() if n != "manifest.json"}
    assert manifest_text.count('"required": true') >= 1
    tampered = manifest_text.replace(
        '"required": true', '"required": true, "required": false', 1
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        for name, blob in other.items():
            out.writestr(name, blob)
        out.writestr("manifest.json", tampered)
    report = ab.verify_agent_backup(io.BytesIO(buf.getvalue()))
    assert report["valid"] is False
    assert any("not valid JSON" in e for e in report["errors"])


def test_verify_rejects_manifest_with_nan_numeric_constant(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    with zipfile.ZipFile(dest) as zf:
        manifest_text = zf.read("manifest.json").decode("utf-8")
        other = {n: zf.read(n) for n in zf.namelist() if n != "manifest.json"}
    manifest = json.loads(manifest_text)
    first_member = manifest["members"][0]
    needle = f'"size": {first_member["size"]}'
    assert needle in manifest_text
    tampered = manifest_text.replace(needle, '"size": NaN', 1)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        for name, blob in other.items():
            out.writestr(name, blob)
        out.writestr("manifest.json", tampered)
    report = ab.verify_agent_backup(io.BytesIO(buf.getvalue()))
    assert report["valid"] is False
    assert any("not valid JSON" in e for e in report["errors"])


def test_baseline_hashes_with_duplicate_key_falls_back_to_unknown_not_crash(tmp_path):
    """A hostile/corrupted shipped_baseline_hashes.json with a duplicate
    key must fall back to the same honest 'unknown provenance' outcome
    every other malformed baseline already produces -- never raise, and
    never silently accept last-key-wins semantics for an authority-
    bearing field."""
    baseline_path = tmp_path / "shipped_baseline_hashes.json"
    baseline_path.write_text(
        '{"format": "lumina-shipped-baseline", '
        '"format_version": {"major": 1, "minor": 0}, '
        '"lumina_version": "unknown", "git_commit": "' + ("0" * 40) + '", '
        '"hash_algorithm": "sha256", '
        '"files": {"personas/a.json": "' + ("a" * 64) + '", '
        '"personas/a.json": "' + ("b" * 64) + '"}}'
    )
    files, warning = ab._load_baseline_hashes(str(baseline_path), "unknown", None)
    assert files == {}
    assert warning is not None


def test_prefs_payload_with_duplicate_key_rejected():
    ok, reason = ab._verify_prefs_payload(b'{"last_persona": "/a", "last_persona": "/b"}')
    assert ok is False
    assert "not valid JSON" in reason


# -----------------------------------------------------------------------
# Full local/central ZIP header agreement (Section 21-25)
# -----------------------------------------------------------------------

def _first_local_header_offset(raw_bytes):
    with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
        info = min(zf.infolist(), key=lambda i: i.header_offset)
    return info.header_offset


def test_b4_local_central_crc_mismatch_rejected(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    with open(dest, "rb") as f:
        original = bytearray(f.read())
    offset = _first_local_header_offset(bytes(original))
    crc_offset = offset + 14  # local header: sig(4)+version(2)+flags(2)+method(2)+time(2)+date(2) = 14
    original[crc_offset] ^= 0xFF
    report = ab.verify_agent_backup(io.BytesIO(bytes(original)))
    assert report["valid"] is False
    assert any("CRC-32" in e for e in report["errors"])


def test_b4_local_central_uncompressed_size_mismatch_rejected(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    with open(dest, "rb") as f:
        original = bytearray(f.read())
    offset = _first_local_header_offset(bytes(original))
    uncomp_size_offset = offset + 22  # +crc32(4)+comp_size(4) past the 14-byte prefix above
    struct.pack_into("<I", original, uncomp_size_offset,
                      struct.unpack_from("<I", original, uncomp_size_offset)[0] ^ 0xFFFF)
    report = ab.verify_agent_backup(io.BytesIO(bytes(original)))
    assert report["valid"] is False
    assert any("uncompressed size" in e for e in report["errors"])


def test_b4_local_central_flags_mismatch_rejected(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    with open(dest, "rb") as f:
        original = bytearray(f.read())
    offset = _first_local_header_offset(bytes(original))
    flags_offset = offset + 6  # sig(4)+version(2)
    original[flags_offset] ^= 0x01  # flip a low bit of the local flags word
    report = ab.verify_agent_backup(io.BytesIO(bytes(original)))
    assert report["valid"] is False
    assert any("general-purpose flags" in e for e in report["errors"])


def test_b4_unicode_member_name_verifies_with_utf8_flag_set(tmp_path):
    """Section 24: a genuinely Unicode overlay member legitimately
    carries flag_bits & 0x800 in BOTH its local and central records --
    the strict equality check must accept this real, builder-emitted
    subset, not assume every member's flags are always zero."""
    data_dir, base_dir = _make_env(tmp_path)
    unicode_name = "lümina☃.json"
    with open(os.path.join(base_dir, "personas", unicode_name), "w", encoding="utf-8") as f:
        f.write(json.dumps({"name": "Unicode Persona"}))
    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest)

    archive_path = f"overlay/personas/{unicode_name}"
    assert archive_path in _members_by_path(manifest)
    with zipfile.ZipFile(dest) as zf:
        info = zf.getinfo(archive_path)
        assert info.flag_bits & 0x800, "a non-ASCII filename must set the UTF-8 flag"
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is True, report["errors"]


# -----------------------------------------------------------------------
# ZIP64 builder/verifier contract (Section 26-30)
# -----------------------------------------------------------------------

def test_builder_refuses_to_emit_zip64_archive_before_publication(tmp_path, monkeypatch):
    """Section 26-29, HIGH/RELEASE GATE: the builder must never silently
    create an archive its own verifier would later reject as ZIP64.
    Patches zipfile's own file-count limit down to a trivially small
    value so a genuinely small, real _build() call exercises the exact
    same zipfile.LargeZipFile path a real 65,536+-member archive would --
    a safe, fast integration exercise of real builder behavior (Section
    29), not a hand-simulated stand-in."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    monkeypatch.setattr(zipfile, "ZIP_FILECOUNT_LIMIT", 2)
    try:
        with pytest.raises(ab.AgentBackupError, match="ZIP64"):
            _build(data_dir, base_dir, dest)
    finally:
        monkeypatch.undo()
    assert not os.path.exists(dest)
    leftovers = [f for f in os.listdir(tmp_path) if f.startswith(".agent_backup_")]
    assert not leftovers


def test_strict_verifier_rejects_hand_built_zip64_eocd(tmp_path):
    """Sanity anchor for the OTHER half of the contract (unchanged since
    A2R7/A2F7, now given explicit direct coverage): a hand-crafted
    archive whose EOCD declares the ZIP64 sentinel values must still be
    rejected by the strict verifier -- builder and verifier agree v1 is
    non-ZIP64 on both sides."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    with open(dest, "rb") as f:
        original = bytearray(f.read())
    idx = bytes(original).rfind(ab._EOCD_SIGNATURE)
    # entries_this_disk (offset+8) AND total_entries (offset+10) both need
    # the sentinel -- leaving them unequal would trip the (earlier-checked)
    # multi-disk rejection instead of ever reaching the ZIP64 check.
    struct.pack_into("<H", original, idx + 8, ab._ZIP64_SENTINEL_16)
    struct.pack_into("<H", original, idx + 10, ab._ZIP64_SENTINEL_16)
    report = ab.verify_agent_backup(io.BytesIO(bytes(original)))
    assert report["valid"] is False
    assert any("ZIP64" in e for e in report["errors"])


# =======================================================================
# AGENT-BACKUP-RESTORE-A2F10
# B1 -- closed v1 ZIP language (compression/flags/extra-field closure,
# physical entry-count ceiling)
# =======================================================================

def _central_header_offset(raw_bytes: bytes) -> int:
    idx = raw_bytes.rfind(ab._EOCD_SIGNATURE)
    cd_offset = struct.unpack_from("<I", raw_bytes, idx + 16)[0]
    assert raw_bytes[cd_offset:cd_offset + 4] == b"PK\x01\x02"
    return cd_offset


def test_builder_output_matches_source_vetted_v1_zip_language(tmp_path):
    """Section 4-7 anchor: source-vets the REAL builder (not folklore)
    across ASCII/Unicode filenames, a zero-byte payload, binary content,
    and both SQLite databases -- every single physical member (and
    manifest.json) must come back ZIP_DEFLATED, flags in {0x0000,
    0x0800}, and a zero-length extra field in both local and central
    records. If this ever fails, the closed v1 language itself (not just
    the verifier's enforcement of it) has drifted and every allowlist
    constant below needs re-deriving from fresh source-vetting, not
    patched to match."""
    data_dir, base_dir = _make_env(tmp_path)
    # A genuinely non-ASCII filename and a zero-byte payload, alongside
    # _make_env's existing ASCII/binary/SQLite fixtures.
    with open(os.path.join(base_dir, "personas", "lümina☃.json"), "w", encoding="utf-8") as f:
        json.dump({"name": "snowman"}, f)
    open(os.path.join(base_dir, "assets", "voices", "empty.wav"), "wb").close()
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    with open(dest, "rb") as f:
        raw = f.read()
    with zipfile.ZipFile(dest) as zf:
        infolist = zf.infolist()
    assert len(infolist) >= 15
    for info in infolist:
        local = ab._parse_local_header(raw, info.header_offset)
        assert local is not None, info.filename
        assert info.compress_type == zipfile.ZIP_DEFLATED, (info.filename, info.compress_type)
        assert local["compression"] == zipfile.ZIP_DEFLATED, (info.filename, local["compression"])
        assert info.flag_bits in (0x0000, 0x0800), (info.filename, info.flag_bits)
        assert local["flags"] in (0x0000, 0x0800), (info.filename, local["flags"])
        assert len(info.extra) == 0, (info.filename, info.extra)
        assert local["extra_len"] == 0, (info.filename, local["extra_len"])
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is True, report["errors"]


def test_verify_rejects_zip_stored_even_when_local_and_central_agree(tmp_path):
    """Section 5: local==central agreement is not sufficient -- both
    records agreeing on ZIP_STORED (a method the v1 builder never emits)
    must still be rejected."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    with open(dest, "rb") as f:
        original = bytearray(f.read())
    with zipfile.ZipFile(io.BytesIO(bytes(original))) as zf:
        info = min(zf.infolist(), key=lambda i: i.header_offset)
    local_method_offset = info.header_offset + 8  # sig(4)+version(2)+flags(2)
    struct.pack_into("<H", original, local_method_offset, 0)  # ZIP_STORED
    cd_offset = _central_header_offset(bytes(original))
    # Walk the central directory to find THIS member's own record (by
    # matching header_offset, not merely taking the first record) --
    # central and local ordering is not guaranteed to coincide in
    # general, even though Lumina's own builder happens to emit them in
    # the same order.
    cursor = cd_offset
    while True:
        assert original[cursor:cursor + 4] == b"PK\x01\x02"
        fname_len, extra_len, comment_len = struct.unpack_from("<HHH", original, cursor + 28)
        rel_offset = struct.unpack_from("<I", original, cursor + 42)[0]
        if rel_offset == info.header_offset:
            struct.pack_into("<H", original, cursor + 10, 0)  # central compression method
            break
        cursor += 46 + fname_len + extra_len + comment_len
    report = ab.verify_agent_backup(io.BytesIO(bytes(original)))
    assert report["valid"] is False
    assert any("ZIP_DEFLATED" in e for e in report["errors"])


@pytest.mark.parametrize("bad_flags", [0x0001, 0x0002, 0x0004, 0x0020, 0x0040, 0x2000])
def test_verify_rejects_reserved_flag_bits_even_when_local_and_central_agree(tmp_path, bad_flags):
    """Section 6: encryption (0x0001), DEFLATE option bits (0x0002/
    0x0004), patched-data (0x0020), strong encryption (0x0040), and a
    historically-reserved bit (0x2000) are all outside the v1 builder's
    actual emitted set ({0x0000, 0x0800}) -- rejected even when local and
    central fully agree on the (impossible) value."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    with open(dest, "rb") as f:
        original = bytearray(f.read())
    with zipfile.ZipFile(io.BytesIO(bytes(original))) as zf:
        info = min(zf.infolist(), key=lambda i: i.header_offset)
    local_flags_offset = info.header_offset + 6  # sig(4)+version(2)
    struct.pack_into("<H", original, local_flags_offset, bad_flags)
    cd_offset = _central_header_offset(bytes(original))
    cursor = cd_offset
    while True:
        assert original[cursor:cursor + 4] == b"PK\x01\x02"
        fname_len, extra_len, comment_len = struct.unpack_from("<HHH", original, cursor + 28)
        rel_offset = struct.unpack_from("<I", original, cursor + 42)[0]
        if rel_offset == info.header_offset:
            struct.pack_into("<H", original, cursor + 8, bad_flags)  # central flags
            break
        cursor += 46 + fname_len + extra_len + comment_len
    report = ab.verify_agent_backup(io.BytesIO(bytes(original)))
    assert report["valid"] is False
    assert any("general-purpose flags" in e for e in report["errors"])


def test_verify_rejects_nonzero_extra_field_even_when_local_and_central_agree(tmp_path):
    """Section 7: a real, well-formed, LOCAL/CENTRAL-CONSISTENT ZIP
    extra field (built via zipfile's own public ZipInfo.extra, not raw
    surgery) -- e.g. an unknown extra-field ID a real ZIP tool might
    write -- is still not part of Lumina's v1 format, which never adds
    one. Covers the 'unknown extra' and (structurally) 'fake ZIP64
    extra' cases: the check rejects ANY nonzero extra-field length,
    never inspecting the extra field's own header ID/content."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zi = zipfile.ZipInfo("manifest.json")
        zi.compress_type = zipfile.ZIP_DEFLATED
        zi.extra = struct.pack("<HH", 0x1234, 0) + struct.pack("<HH", 0x0001, 0)  # unknown + fake ZIP64 IDs
        zf.writestr(zi, json.dumps({"format": ab.MANIFEST_FORMAT}))
    report = ab.verify_agent_backup(io.BytesIO(buf.getvalue()))
    assert report["valid"] is False
    assert any("extra field" in e for e in report["errors"])


def test_verify_rejects_local_only_extra_field(tmp_path):
    """Section 7: an extra field present ONLY in the local header (never
    mirrored to the central directory) is caught independently by the
    local-side check -- not merely inferred from a local/central
    disagreement. Surgery targets the LAST local record (manifest.json,
    always written last by the real builder) so inserting bytes shifts
    only the trailing central directory + EOCD, never another member's
    own local record."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    with open(dest, "rb") as f:
        original = bytearray(f.read())
    with zipfile.ZipFile(io.BytesIO(bytes(original))) as zf:
        last_info = max(zf.infolist(), key=lambda i: i.header_offset)
    assert last_info.filename == "manifest.json"
    local = ab._parse_local_header(bytes(original), last_info.header_offset)
    fname_len = len(last_info.filename.encode("utf-8") if last_info.flag_bits & 0x800 else
                     last_info.filename.encode("cp437", "replace"))
    extra_len_field_offset = last_info.header_offset + 28  # sig+ver+flags+method+time+date+crc+comp+uncomp+fnamelen
    insert_at = last_info.header_offset + 30 + fname_len  # right after the (currently empty) local extra field
    inserted = b"\x99\x99\x04\x00\xde\xad\xbe\xef"  # a fake 4-byte extra field, ID 0x9999
    struct.pack_into("<H", original, extra_len_field_offset, len(inserted))
    original[insert_at:insert_at] = inserted
    # Inserting bytes into the middle of the file shifts everything after
    # insert_at (manifest.json's own payload, the whole central directory,
    # and the EOCD) forward by len(inserted) -- the EOCD's own recorded
    # cd_offset must be corrected by the same amount, or zipfile itself
    # will fail to locate the central directory at all. No OTHER local
    # record's offset needs adjustment (insert_at is inside the LAST local
    # record, so nothing else in the archive lies at or after it), and
    # manifest.json's own central-directory "relative offset of local
    # header" field (pointing at last_info.header_offset, BEFORE
    # insert_at) is unaffected too.
    eocd_idx = bytes(original).rfind(ab._EOCD_SIGNATURE)
    old_cd_offset = struct.unpack_from("<I", original, eocd_idx + 16)[0]
    struct.pack_into("<I", original, eocd_idx + 16, old_cd_offset + len(inserted))
    report = ab.verify_agent_backup(io.BytesIO(bytes(original)))
    assert report["valid"] is False
    assert any("extra field" in e for e in report["errors"])


# =======================================================================
# AGENT-BACKUP-RESTORE-A2F11 (P0, Section 21-30) -- canonical v1 ZIP
# member metadata: version needed, disk number start, create_system,
# external/internal file attributes, per-member comment. The literal
# A2R11 finding: strict verification accepted builder-impossible
# central-directory state (version needed 63, disk number start 1, Unix
# symlink external attributes) because local==central agreement was
# checked but neither side was ever compared against what the real
# builder actually emits.
# =======================================================================

def _central_record_offset_for_local_offset(raw_bytes: bytes, local_header_offset: int) -> int:
    """Walks the central directory to find the record whose own
    'relative offset of local header' field matches local_header_offset,
    returning that record's own starting byte offset -- central and
    local ordering is not guaranteed to coincide in general, even though
    Lumina's own builder happens to emit them in the same order (same
    technique the pre-existing B1/H2 mutation tests already use inline,
    factored out here so the new A2F11 mutation tests below don't repeat
    it six more times)."""
    cursor = _central_header_offset(raw_bytes)
    while True:
        assert raw_bytes[cursor:cursor + 4] == b"PK\x01\x02"
        fname_len, extra_len, comment_len = struct.unpack_from("<HHH", raw_bytes, cursor + 28)
        rel_offset = struct.unpack_from("<I", raw_bytes, cursor + 42)[0]
        if rel_offset == local_header_offset:
            return cursor
        cursor += 46 + fname_len + extra_len + comment_len


def _build_and_load_for_mutation(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    with open(dest, "rb") as f:
        original = bytearray(f.read())
    with zipfile.ZipFile(io.BytesIO(bytes(original))) as zf:
        info = min(zf.infolist(), key=lambda i: i.header_offset)
    cursor = _central_record_offset_for_local_offset(bytes(original), info.header_offset)
    return original, info, cursor


def test_verify_rejects_version_needed_63_in_central_directory(tmp_path):
    """The literal AGENT-BACKUP-RESTORE-A2R11 finding: version needed 63
    -- neither this module's canonical 20 nor a genuine ZIP64 archive's
    45 -- passed strict verification because nothing compared
    create_version/extract_version against what the real v1 builder
    actually emits."""
    original, info, cursor = _build_and_load_for_mutation(tmp_path)
    struct.pack_into("<B", original, cursor + 6, 63)  # central extract_version (1 byte)
    report = ab.verify_agent_backup(io.BytesIO(bytes(original)))
    assert report["valid"] is False
    assert any("version needed to extract" in e and "63" in e for e in report["errors"])


def test_verify_rejects_version_needed_63_in_local_header_even_when_central_agrees(tmp_path):
    original, info, cursor = _build_and_load_for_mutation(tmp_path)
    struct.pack_into("<H", original, info.header_offset + 4, 63)  # local version_needed (full 2-byte field)
    struct.pack_into("<B", original, cursor + 6, 63)  # central extract_version, kept in agreement
    report = ab.verify_agent_backup(io.BytesIO(bytes(original)))
    assert report["valid"] is False
    assert any("version needed to extract" in e for e in report["errors"])


def test_verify_rejects_local_central_version_needed_disagreement(tmp_path):
    original, info, cursor = _build_and_load_for_mutation(tmp_path)
    struct.pack_into("<H", original, info.header_offset + 4, 21)  # local only
    report = ab.verify_agent_backup(io.BytesIO(bytes(original)))
    assert report["valid"] is False
    assert any(
        "declares a version needed to extract of" in e and "local file header" in e
        for e in report["errors"]
    )


def test_verify_rejects_nonzero_disk_number_start(tmp_path):
    """The literal AGENT-BACKUP-RESTORE-A2R11 finding: disk number start
    1 passed strict verification -- v1 is never a multi-disk/split
    archive, and nothing enforced that per-member (only the EOCD's own
    disk fields were ever checked)."""
    original, info, cursor = _build_and_load_for_mutation(tmp_path)
    struct.pack_into("<H", original, cursor + 34, 1)  # central disk number start
    report = ab.verify_agent_backup(io.BytesIO(bytes(original)))
    assert report["valid"] is False
    assert any("disk number start" in e for e in report["errors"])


def test_verify_rejects_non_unix_create_system(tmp_path):
    original, info, cursor = _build_and_load_for_mutation(tmp_path)
    struct.pack_into("<B", original, cursor + 5, 0)  # central create_system: 0 = FAT/Windows
    report = ab.verify_agent_backup(io.BytesIO(bytes(original)))
    assert report["valid"] is False
    assert any("create_system" in e for e in report["errors"])


def test_verify_rejects_nonzero_internal_attributes(tmp_path):
    original, info, cursor = _build_and_load_for_mutation(tmp_path)
    struct.pack_into("<H", original, cursor + 36, 1)  # central internal file attributes
    report = ab.verify_agent_backup(io.BytesIO(bytes(original)))
    assert report["valid"] is False
    assert any("internal file attributes" in e for e in report["errors"])


@pytest.mark.parametrize("bad_external_attr,label", [
    ((0o120000 | 0o777) << 16, "symlink"),  # S_IFLNK
    ((0o040000 | 0o755) << 16, "directory"),  # S_IFDIR
    ((0o140000 | 0o755) << 16, "socket"),  # S_IFSOCK
    ((0o010000 | 0o644) << 16, "fifo"),  # S_IFIFO
    ((0o060000 | 0o644) << 16, "block-device"),  # S_IFBLK
    ((0o020000 | 0o644) << 16, "char-device"),  # S_IFCHR
    (0o644 << 16, "type-less-0644"),  # AGENT-BACKUP-RESTORE-A2R12 P0-2: no S_IFREG bit at all
    ((stat.S_IFREG | 0o600) << 16, "regular-wrong-perms-0600"),
    ((stat.S_IFREG | 0o755) << 16, "regular-wrong-perms-0755"),
    ((0o644 << 16) | 0x10, "dos-directory-bit"),  # low 16 bits: DOS directory attribute
])
def test_verify_rejects_non_regular_file_external_attributes(tmp_path, bad_external_attr, label):
    """The literal AGENT-BACKUP-RESTORE-A2R11 finding: Unix symlink
    external file attributes passed strict verification -- v1 payload
    members are always regular files, never symlinks, directories, or
    other special-file types, and the DOS attribute byte (the low 16
    bits of the same field) must likewise never carry the directory bit.
    AGENT-BACKUP-RESTORE-A2R12 (P0-2) added 'type-less-0644' -- the exact
    historical bug: 0o644 << 16 alone carries only permission bits with NO
    file-type bit set at all (stat.S_ISREG of it is False), which was this
    module's OWN old canonical value and so would have passed unnoticed;
    it must now be rejected exactly like every other non-canonical type,
    and the two regular-file/wrong-permission cases confirm the exact
    canonical value (0100644) is enforced, not merely 'any regular-file
    type bit with any permissions.'"""
    original, info, cursor = _build_and_load_for_mutation(tmp_path)
    struct.pack_into("<I", original, cursor + 38, bad_external_attr)  # central external file attributes
    report = ab.verify_agent_backup(io.BytesIO(bytes(original)))
    assert report["valid"] is False, label
    assert any("external file attributes" in e for e in report["errors"]), label


def test_verify_rejects_central_directory_member_comment(tmp_path):
    """Built via zipfile's own public ZipInfo.comment API (not raw
    surgery) -- a real, well-formed per-member central-directory file
    comment a genuine ZIP tool could write, still not part of Lumina's
    v1 format, which never writes one (distinct from the archive-level
    EOCD comment, already covered by the trailing-bytes check)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zi = ab._make_v1_zipinfo("manifest.json")
        zi.comment = b"hello"
        zf.writestr(zi, json.dumps({"format": ab.MANIFEST_FORMAT}))
    report = ab.verify_agent_backup(io.BytesIO(buf.getvalue()))
    assert report["valid"] is False
    assert any("file comment" in e for e in report["errors"])


def test_v1_zip_external_attr_encodes_canonical_regular_file_type():
    """AGENT-BACKUP-RESTORE-A2F12 (P0-2, Sections 21-22): the canonical
    constant itself, independent of any archive, must decode to an
    EXPLICIT regular file -- not merely 'permission bits happen to read
    0644.' The old value (0o644 << 16 alone) carried no file-type bit and
    so stat.S_ISREG of it was False."""
    mode = ab._V1_ZIP_EXTERNAL_ATTR >> 16
    assert mode == 0o100644
    assert stat.S_ISREG(mode) is True
    assert stat.S_IMODE(mode) == 0o644
    assert ab._V1_ZIP_EXTERNAL_ATTR & 0xFFFF == 0  # low word (DOS attrs) untouched by the fix


def test_make_v1_zipinfo_matches_source_vetted_real_builder_output(tmp_path):
    """AGENT-BACKUP-RESTORE-A2F11 anchor (Section 22): source-vets the
    REAL builder's own emitted metadata against the canonical constants
    -- if this ever fails, the canonical contract itself has drifted
    from what the builder actually produces, not just the verifier's
    enforcement of it. AGENT-BACKUP-RESTORE-A2F12 (Section 29) extends
    this same corpus (ASCII/unicode/zero-byte/binary/SQLite/manifest
    members, via the ordinary _build() path) with an explicit decoded-
    mode/S_ISREG check per member, not just raw external_attr equality."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    with zipfile.ZipFile(dest) as zf:
        infolist = zf.infolist()
    assert len(infolist) >= 5
    for info in infolist:
        assert info.create_system == ab._V1_ZIP_CREATE_SYSTEM, info.filename
        assert info.create_version == ab._V1_ZIP_VERSION, info.filename
        assert info.extract_version == ab._V1_ZIP_VERSION, info.filename
        assert info.volume == ab._V1_ZIP_VOLUME, info.filename
        assert info.internal_attr == ab._V1_ZIP_INTERNAL_ATTR, info.filename
        assert info.external_attr == ab._V1_ZIP_EXTERNAL_ATTR, info.filename
        assert info.comment == b"", info.filename
        decoded_mode = info.external_attr >> 16
        assert stat.S_ISREG(decoded_mode) is True, info.filename
        assert stat.S_IMODE(decoded_mode) == 0o644, info.filename
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is True, report["errors"]


# =======================================================================
# B1 (continued) -- physical ZIP entry-count ceiling (Section 8-10)
# =======================================================================

def test_max_physical_zip_entries_below_zip64_sentinel_boundary():
    assert ab._MAX_PHYSICAL_ZIP_ENTRIES == ab._ZIP64_SENTINEL_16 - 1


def test_builder_refuses_when_physical_entry_count_would_reach_v1_ceiling(tmp_path, monkeypatch):
    """Section 9-10: the builder must refuse BEFORE ever opening a
    zipfile.ZipFile for writing -- not merely rely on the (already
    correct) strict-verifier rejection after a wasted full build.
    Patches the ceiling down to a value _make_env's ~18 real members
    trivially exceeds, for a fast, real exercise of the actual
    preflight check rather than constructing tens of thousands of fake
    members."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    monkeypatch.setattr(ab, "_MAX_PHYSICAL_ZIP_ENTRIES", 2)
    with pytest.raises(ab.AgentBackupError, match="physical ZIP entries"):
        _build(data_dir, base_dir, dest)
    assert not os.path.exists(dest)
    leftovers = [f for f in os.listdir(tmp_path) if f.startswith(".agent_backup_")]
    assert not leftovers


def test_real_65534_physical_entries_builds_and_strictly_verifies():
    """Live, real-scale confirmation of the exact empirical boundary
    A2R10 measured (65,534 succeeds and verifies; 65,535 is builder-
    successful but verifier-ambiguous; 65,536 raises LargeZipFile) --
    exercised directly against raw zipfile (no code path in this
    module's real collectors produces 65k+ fake members), proving the
    ceiling this module chose (65534) is not an off-by-one guess."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, allowZip64=False) as zf:
        for i in range(ab._MAX_PHYSICAL_ZIP_ENTRIES):
            zf.writestr(ab._make_v1_zipinfo(f"m{i:06d}.txt"), b"")
    data = buf.getvalue()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        infolist = zf.infolist()
    assert ab._validate_zip_physical_structure(data, infolist) == []


def test_real_65535_physical_entries_builds_but_verifier_rejects_as_ambiguous():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, allowZip64=False) as zf:
        for i in range(ab._MAX_PHYSICAL_ZIP_ENTRIES + 1):
            zf.writestr(ab._make_v1_zipinfo(f"m{i:06d}.txt"), b"")
    data = buf.getvalue()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        infolist = zf.infolist()
    errors = ab._validate_zip_physical_structure(data, infolist)
    assert any("ZIP64" in e for e in errors)


def test_real_65536_physical_entries_raises_large_zip_file():
    buf = io.BytesIO()
    with pytest.raises(zipfile.LargeZipFile):
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, allowZip64=False) as zf:
            for i in range(ab._MAX_PHYSICAL_ZIP_ENTRIES + 2):
                zf.writestr(f"m{i:06d}.txt", b"")


# =======================================================================
# H1 -- publication/rollback directory-durability truth (Section 13-19)
# =======================================================================

def test_publish_immutable_bytes_reports_directory_fsynced_true_on_real_success(tmp_path):
    dest = str(tmp_path / "dest.bin")
    data = b"hello world"
    sha = hashlib.sha256(data).hexdigest()
    result = ab._publish_immutable_bytes(data, sha, dest, str(tmp_path),
                                          os.path.realpath(str(tmp_path)), {}, "test artifact")
    assert result["directory_fsynced"] is True
    assert result["matched_built_artifact"] is True
    with open(dest, "rb") as f:
        assert f.read() == data


def test_publish_immutable_bytes_reports_directory_fsynced_false_without_failing_publication(
        tmp_path, monkeypatch):
    """HIGH H1: a parent-directory fsync failure must never be silently
    swallowed, and must never turn an otherwise-successful, content-
    verified publish into a raised error (A1's historical best-effort
    durability model, preserved -- not tightened into an unrequested
    destructive rule)."""
    dest = str(tmp_path / "dest.bin")
    data = b"hello world"
    sha = hashlib.sha256(data).hexdigest()
    monkeypatch.setattr(ab, "_best_effort_fsync_dir", lambda _dir: False)
    result = ab._publish_immutable_bytes(data, sha, dest, str(tmp_path),
                                          os.path.realpath(str(tmp_path)), {}, "test artifact")
    assert result["directory_fsynced"] is False
    assert result["matched_built_artifact"] is True
    with open(dest, "rb") as f:
        assert f.read() == data


def test_build_agent_backup_manifest_warns_when_directory_fsync_fails(tmp_path, monkeypatch):
    """AGENT-BACKUP-RESTORE-A2F11 (P1, Section 32-37, repairing the
    A2R11 P1 manifest-truth blocker): build_agent_backup's returned
    manifest must be EXACTLY the manifest archived as manifest.json --
    even when this specific publish's own parent-directory fsync did
    not succeed. A2F10's own version of this function appended a
    directory-fsync-failure warning onto the returned dict, which this
    exact test used to assert on (`manifest["warnings"]` containing the
    fsync text) -- that was itself the A2R11-flagged bug: manifest
    truth demoted to make room for post-publication evidence. Durability
    is now surfaced ONLY via AgentBackupDurabilityWarning (a real,
    catchable Python warning), never inside the manifest dict."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    monkeypatch.setattr(ab, "_best_effort_fsync_dir", lambda _dir: False)
    with pytest.warns(ab.AgentBackupDurabilityWarning, match="directory.*fsync"):
        manifest = _build(data_dir, base_dir, dest)
    # The returned manifest carries NO durability-related warning of any
    # kind -- that fact was not yet knowable when manifest.json's bytes
    # were serialized and archived, before publication ever happened.
    assert not any("fsync" in w for w in manifest["warnings"])
    # Section 33's plain-API contract: the returned manifest is EXACTLY
    # what was archived, not a post-hoc mutated copy of it.
    with zipfile.ZipFile(dest) as zf:
        archived_manifest = json.loads(zf.read("manifest.json"))
    assert manifest == archived_manifest
    # The archive itself was still genuinely published and is valid --
    # content success and durability confirmation are independent facts.
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is True, report["errors"]


def test_build_agent_backup_without_directory_fsync_failure_carries_no_spurious_warning(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    with warnings.catch_warnings():
        warnings.simplefilter("error", ab.AgentBackupDurabilityWarning)
        manifest = _build(data_dir, base_dir, dest)
    assert not any("fsync" in w for w in manifest["warnings"])


def test_build_agent_backup_with_receipt_publication_reports_directory_fsynced_false(tmp_path, monkeypatch):
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    monkeypatch.setattr(ab, "_best_effort_fsync_dir", lambda _dir: False)
    manifest, receipt = ab.build_agent_backup_with_receipt(
        data_dir, base_dir, dest, identity_trailers_path=NO_IDENTITY_TRAILERS,
        credentials_path=NO_CREDENTIALS)
    assert receipt["publication"]["directory_fsynced"] is False
    assert receipt["verification"] == "PASS"


def test_build_agent_backup_with_receipt_publication_reports_directory_fsynced_true(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    manifest, receipt = ab.build_agent_backup_with_receipt(
        data_dir, base_dir, dest, identity_trailers_path=NO_IDENTITY_TRAILERS,
        credentials_path=NO_CREDENTIALS)
    assert receipt["publication"]["directory_fsynced"] is True


def _sabotage_first_replace_to_dest(dest, decoy_bytes):
    """Shared sabotage helper (same technique as the pre-existing B3
    rollback tests): swaps in `decoy_bytes` immediately after the FIRST
    os.replace(..., dest) lands, forcing _publish_immutable_bytes's own
    content re-observation to detect a mismatch and raise -- which in
    turn drives _build_agent_backup_core into its rollback path."""
    real_replace = os.replace
    state = {"swapped": False}

    def _sabotage_replace(src, dst, *a, **kw):
        real_replace(src, dst, *a, **kw)
        if dst == dest and not state["swapped"]:
            state["swapped"] = True
            with open(dest, "r+b") as f:
                f.seek(0)
                f.write(decoy_bytes)
                f.truncate()

    return _sabotage_replace, state


def test_rollback_success_reports_directory_durability_confirmed(tmp_path, monkeypatch):
    """Section 17, 'Rollback Durability Truth': a successful rollback
    (content restored, content-verified by re-observation) reports
    directory durability CONFIRMED when the rollback's own parent-
    directory fsync genuinely succeeds."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)

    evil_data_dir, evil_base_dir = _make_env(tmp_path / "evil_env_h1_confirmed")
    decoy_dest = str(tmp_path / "decoy_h1_confirmed.zip")
    ab.build_agent_backup(evil_data_dir, evil_base_dir, decoy_dest,
                           identity_trailers_path=NO_IDENTITY_TRAILERS, credentials_path=NO_CREDENTIALS)
    with open(decoy_dest, "rb") as f:
        decoy_bytes = f.read()

    sabotage, state = _sabotage_first_replace_to_dest(dest, decoy_bytes)
    monkeypatch.setattr(os, "replace", sabotage)
    with pytest.raises(ab.AgentBackupError,
                        match=r"prior destination restored.*directory durability confirmed"):
        _build(data_dir, base_dir, dest)
    monkeypatch.undo()
    assert state["swapped"]


def test_rollback_success_reports_directory_durability_best_effort_when_fsync_fails(tmp_path, monkeypatch):
    """Same scenario, but the rollback's own directory fsync does not
    succeed -- the raised message must say so honestly (never 'durably
    restored') while still correctly describing content as restored
    (never 'rollback failed', since content restoration itself did
    succeed and was content-verified)."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)

    evil_data_dir, evil_base_dir = _make_env(tmp_path / "evil_env_h1_besteffort")
    decoy_dest = str(tmp_path / "decoy_h1_besteffort.zip")
    ab.build_agent_backup(evil_data_dir, evil_base_dir, decoy_dest,
                           identity_trailers_path=NO_IDENTITY_TRAILERS, credentials_path=NO_CREDENTIALS)
    with open(decoy_dest, "rb") as f:
        decoy_bytes = f.read()

    sabotage, state = _sabotage_first_replace_to_dest(dest, decoy_bytes)
    monkeypatch.setattr(os, "replace", sabotage)
    monkeypatch.setattr(ab, "_best_effort_fsync_dir", lambda _dir: False)
    with pytest.raises(
            ab.AgentBackupError,
            match=r"prior destination restored.*directory durability best-effort only, not confirmed"):
        _build(data_dir, base_dir, dest)
    monkeypatch.undo()
    assert state["swapped"]
    with open(dest, "rb") as f:
        assert f.read() != decoy_bytes  # content genuinely restored despite the fsync failure


# =======================================================================
# H2 -- finite aggregate resource budget (Section 20-33)
# =======================================================================

def test_aggregate_budget_charges_incrementally_and_rejects_over_limit():
    budget = ab._AggregateBudget(100)
    budget.charge(40, "a")
    budget.charge(60, "b")
    assert budget.consumed_bytes == 100
    with pytest.raises(ab.AgentBackupError, match="resource budget"):
        budget.charge(1, "c")
    # A rejected charge must not partially apply.
    assert budget.consumed_bytes == 100


def test_aggregate_budget_exact_boundary_at_limit_succeeds():
    budget = ab._AggregateBudget(100)
    budget.charge(100, "exact")
    assert budget.consumed_bytes == 100


def test_aggregate_budget_one_byte_over_limit_fails():
    budget = ab._AggregateBudget(100)
    with pytest.raises(ab.AgentBackupError):
        budget.charge(101, "over")


def test_determine_aggregate_payload_budget_uses_finite_fallback_when_memory_unavailable_everywhere(monkeypatch):
    """AGENT-BACKUP-RESTORE-A2F11 (Section 5, repairing the A2R11 P0
    resource-policy blocker): when NEITHER host (/proc/meminfo) NOR
    cgroup v2 memory can be discovered at all, the build-time budget
    still derives from the finite FALLBACK working-set ceiling -- it
    must NOT simply return the bare configured/default ceiling
    unclamped. That was the exact A2R11-flagged failure mode: total
    discovery failure treated as an effectively unbounded host-safety
    allowance (up to 16 GiB via operator override)."""
    monkeypatch.setattr(ab, "_read_meminfo_available_bytes", lambda: None)
    monkeypatch.setattr(ab, "_discover_cgroup_v2_availability", lambda: ("unknown", None))
    monkeypatch.delenv(ab._AGENT_BACKUP_MAX_PAYLOAD_BYTES_ENV, raising=False)
    budget = ab._determine_aggregate_payload_budget()
    assert 0 < budget < ab._DEFAULT_MAX_AGGREGATE_UNCOMPRESSED_BYTES


def test_determine_aggregate_payload_budget_clamps_downward_for_low_available_memory(monkeypatch):
    monkeypatch.setattr(ab, "_effective_available_memory_bytes", lambda: 200 * 1024 * 1024)  # 200 MiB
    monkeypatch.delenv(ab._AGENT_BACKUP_MAX_PAYLOAD_BYTES_ENV, raising=False)
    budget = ab._determine_aggregate_payload_budget()
    assert 0 < budget < ab._DEFAULT_MAX_AGGREGATE_UNCOMPRESSED_BYTES


def test_determine_aggregate_payload_budget_does_not_floor_a_tiny_host_upward(monkeypatch):
    """AGENT-BACKUP-RESTORE-A2F11 (Section 6, the literal A2R11 P0
    finding): the old `if safe_for_available < _MIN_...: safe_for_available
    = _MIN_...` floor is GONE -- a host small enough that the derived
    build-payload allowance would fall below the (16 MiB) operator-
    override sanity floor must receive that SMALLER value, never be
    pushed back up past what the working-set ceiling actually supports.
    A2R11's own reproduction: a 256 MiB host was pushed to an estimated
    ~160 MiB peak against its own 128 MiB safety target by this exact
    floor. 140 MiB here derives well under 16 MiB (140 MiB * 50% = 70
    MiB ceiling; 70 MiB - 64 MiB fixed overhead = 6 MiB; 6 MiB / 6x
    safety factor = 1 MiB)."""
    monkeypatch.setattr(ab, "_effective_available_memory_bytes", lambda: 140 * 1024 * 1024)  # 140 MiB
    monkeypatch.delenv(ab._AGENT_BACKUP_MAX_PAYLOAD_BYTES_ENV, raising=False)
    budget = ab._determine_aggregate_payload_budget()
    assert 0 < budget < ab._MIN_AGGREGATE_UNCOMPRESSED_BYTES


def test_determine_aggregate_payload_budget_refuses_outright_when_host_cannot_cover_fixed_overhead(monkeypatch):
    """AGENT-BACKUP-RESTORE-A2F11 (Section 6): when this build's working-
    set ceiling cannot even cover the fixed overhead reserve (let alone
    any actual payload), the correct behavior is an explicit,
    honestly-labeled refusal -- never a floored-up nonzero budget that
    quietly authorizes an operation the host cannot safely support."""
    monkeypatch.setattr(ab, "_effective_available_memory_bytes", lambda: 100 * 1024 * 1024)  # 100 MiB
    monkeypatch.delenv(ab._AGENT_BACKUP_MAX_PAYLOAD_BYTES_ENV, raising=False)
    with pytest.raises(ab.AgentBackupError, match="insufficient memory budget"):
        ab._determine_aggregate_payload_budget()


def test_determine_aggregate_payload_budget_never_exceeds_configured_default(monkeypatch):
    """Host-memory awareness only ever clamps DOWNWARD (Section 27) --
    an abundant host must never receive a budget larger than the
    configured/default ceiling."""
    monkeypatch.setattr(ab, "_effective_available_memory_bytes", lambda: 1024 * 1024 * 1024 * 1024)  # 1 TiB
    monkeypatch.delenv(ab._AGENT_BACKUP_MAX_PAYLOAD_BYTES_ENV, raising=False)
    assert ab._determine_aggregate_payload_budget() == ab._DEFAULT_MAX_AGGREGATE_UNCOMPRESSED_BYTES


def test_determine_aggregate_payload_budget_accounts_for_prior_backup_bytes(monkeypatch):
    monkeypatch.setattr(ab, "_effective_available_memory_bytes", lambda: 1024 * 1024 * 1024)  # 1 GiB
    monkeypatch.delenv(ab._AGENT_BACKUP_MAX_PAYLOAD_BYTES_ENV, raising=False)
    no_prior = ab._determine_aggregate_payload_budget(prior_backup_bytes=0)
    with_prior = ab._determine_aggregate_payload_budget(prior_backup_bytes=400 * 1024 * 1024)
    assert with_prior < no_prior


# =======================================================================
# A2F12/A2F13 -- process-relative cgroup v2 discovery (Sections 3-20)
# =======================================================================
#
# AGENT-BACKUP-RESTORE-A2R12 found the A2F11 implementation above assumed
# the current process is always a direct member of the cgroup2 mount's own
# ROOT cgroup (fixed /sys/fs/cgroup/memory.max /memory.current paths). Real
# processes -- any desktop session, container, or sandboxed app scope --
# are nested several levels deep instead. A2F12 replaced the old root-only
# fixtures with a full fake /proc/self/cgroup + /proc/self/mountinfo +
# multi-level cgroup2 directory tree, exercising the actual resolution
# chain rather than bypassing it.
#
# AGENT-BACKUP-RESTORE-A2R13 then found A2F12's own mount discovery
# (_read_cgroup_v2_mount, singular) unconditionally returned the FIRST
# cgroup2 record in mountinfo, with no regard for whether it was actually
# applicable to this process's own cgroup path -- a host with more than one
# cgroup2 mount (a bind mount / duplicate view is common) could silently
# pick an unrelated mount, fail process-path resolution under it, and
# discard a real, applicable, tighter cgroup constraint, falling back to
# host-only memory accounting. A2F13 replaces the whole single-mount model
# (_read_cgroup_v2_mount, _resolve_process_cgroup_dir-as-sole-answer,
# _read_memory_max_current's two-state None) with a multi-mount-aware,
# tri-state ('finite' / 'unlimited' / 'unknown') discovery chain -- these
# tests cover both the original A2F12 process-relative regressions (now
# expressed against the new function names/shapes) and the new A2F13
# multi-mount/tri-state matrix.

def _mountinfo_line(mount_root, mount_point, mount_id=38, parent_id=28, fstype="cgroup2"):
    escaped_root = str(mount_root).replace("\\", "\\134").replace(" ", "\\040")
    escaped_point = str(mount_point).replace("\\", "\\134").replace(" ", "\\040")
    return f"{mount_id} {parent_id} 0:32 {escaped_root} {escaped_point} rw,nosuid,nodev - {fstype} {fstype} rw\n"


def _write_proc_self_cgroup(tmp_path, monkeypatch, logical_path, filename="proc_self_cgroup"):
    proc_cgroup = tmp_path / filename
    proc_cgroup.write_text(f"0::{logical_path}\n")
    monkeypatch.setattr(ab, "_PROC_SELF_CGROUP_PATH", str(proc_cgroup))


def _write_mountinfo(tmp_path, monkeypatch, records, filename="proc_self_mountinfo"):
    """`records` is a list of (mount_root, mount_point) pairs -- each is
    written as its own cgroup2 mountinfo line, in order, alongside an
    unrelated non-cgroup2 decoy line (so parsing must actually check
    fstype, not merely line position)."""
    lines = ["22 27 0:21 / /sys shared:7 - sysfs sysfs rw\n"]
    for i, (mount_root, mount_point) in enumerate(records):
        lines.append(_mountinfo_line(mount_root, mount_point, mount_id=38 + i, parent_id=28))
    mountinfo = tmp_path / filename
    mountinfo.write_text("".join(lines))
    monkeypatch.setattr(ab, "_PROC_SELF_MOUNTINFO_PATH", str(mountinfo))


def _write_cgroup_v2_fixture(tmp_path, monkeypatch, logical_path, mount_root, mount_point_name="fake_cgroup_mount"):
    """Single-mount convenience fixture (the A2F12-era shape): builds a
    fake /proc/self/cgroup and a fake /proc/self/mountinfo with exactly
    ONE cgroup2 entry, then points the module's own discovery constants
    at both fakes. Returns the mount-point Path so callers can populate
    memory.max/memory.current at whatever levels they need."""
    _write_proc_self_cgroup(tmp_path, monkeypatch, logical_path)
    mount_point = tmp_path / mount_point_name
    mount_point.mkdir(parents=True, exist_ok=True)
    _write_mountinfo(tmp_path, monkeypatch, [(mount_root, str(mount_point))])
    return mount_point


def _write_memory_files(cgroup_dir, max_text, current_text):
    cgroup_dir.mkdir(parents=True, exist_ok=True)
    if max_text is not None:
        (cgroup_dir / "memory.max").write_text(max_text)
    if current_text is not None:
        (cgroup_dir / "memory.current").write_text(current_text)


def test_decode_mountinfo_field_handles_standard_escapes():
    assert ab._decode_mountinfo_field("no\\040escapes\\040needed") == "no escapes needed"
    assert ab._decode_mountinfo_field(r"tab\011here") == "tab\there"
    assert ab._decode_mountinfo_field(r"newline\012here") == "newline\nhere"
    assert ab._decode_mountinfo_field(r"backslash\134here") == "backslash\\here"
    assert ab._decode_mountinfo_field("/plain/path") == "/plain/path"


def test_read_process_cgroup_v2_path_finds_unified_hierarchy_line(tmp_path, monkeypatch):
    proc_cgroup = tmp_path / "cgroup"
    proc_cgroup.write_text("0::/user.slice/app.scope\n")
    monkeypatch.setattr(ab, "_PROC_SELF_CGROUP_PATH", str(proc_cgroup))
    assert ab._read_process_cgroup_v2_path() == "/user.slice/app.scope"


def test_read_process_cgroup_v2_path_ignores_cgroup_v1_lines(tmp_path, monkeypatch):
    """A hybrid host reports cgroup v1 controller lines alongside the
    unified hierarchy -- Section 3's 'multiple unrelated controller
    lines' input. Only the hierarchy-ID-0, empty-controller-list line is
    the process's actual cgroup v2 path."""
    proc_cgroup = tmp_path / "cgroup"
    proc_cgroup.write_text(
        "12:pids:/user.slice/user-1000.slice\n"
        "11:cpu,cpuacct:/user.slice/user-1000.slice\n"
        "1:name=systemd:/user.slice/user-1000.slice\n"
        "0::/user.slice/user-1000.slice/app.scope\n"
    )
    monkeypatch.setattr(ab, "_PROC_SELF_CGROUP_PATH", str(proc_cgroup))
    assert ab._read_process_cgroup_v2_path() == "/user.slice/user-1000.slice/app.scope"


def test_read_process_cgroup_v2_path_root_process_is_just_root(tmp_path, monkeypatch):
    proc_cgroup = tmp_path / "cgroup"
    proc_cgroup.write_text("0::/\n")
    monkeypatch.setattr(ab, "_PROC_SELF_CGROUP_PATH", str(proc_cgroup))
    assert ab._read_process_cgroup_v2_path() == "/"


@pytest.mark.parametrize("bad_content", [
    "",
    "not a valid cgroup line at all\n",
    "12:pids:/only/a/v1/line\n",  # no unified-hierarchy line present at all
    "0:cpu:/malformed/nonempty/controllers/on/hierarchy/zero\n",
])
def test_read_process_cgroup_v2_path_none_on_malformed_content(tmp_path, monkeypatch, bad_content):
    proc_cgroup = tmp_path / "cgroup"
    proc_cgroup.write_text(bad_content)
    monkeypatch.setattr(ab, "_PROC_SELF_CGROUP_PATH", str(proc_cgroup))
    assert ab._read_process_cgroup_v2_path() is None


def test_read_process_cgroup_v2_path_none_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(ab, "_PROC_SELF_CGROUP_PATH", str(tmp_path / "does_not_exist"))
    assert ab._read_process_cgroup_v2_path() is None


def test_read_cgroup_v2_mounts_finds_cgroup2_entry_and_decodes_escapes(tmp_path, monkeypatch):
    mountinfo = tmp_path / "mountinfo"
    mount_point = tmp_path / "fake cgroup mount"  # real space -- must round-trip through \040
    mountinfo.write_text(
        "22 27 0:21 / /sys shared:7 - sysfs sysfs rw\n"
        f"38 28 0:32 / {str(mount_point).replace(' ', chr(92) + '040')} rw,nosuid - cgroup2 cgroup2 rw,nsdelegate\n"
        "39 28 0:33 / /proc rw - proc proc rw\n"
    )
    monkeypatch.setattr(ab, "_PROC_SELF_MOUNTINFO_PATH", str(mountinfo))
    assert ab._read_cgroup_v2_mounts() == [("/", str(mount_point))]


def test_read_cgroup_v2_mounts_returns_every_cgroup2_entry_in_file_order(tmp_path, monkeypatch):
    """AGENT-BACKUP-RESTORE-A2F13 (Section 2/4): the literal repair -- a
    mountinfo with MORE THAN ONE cgroup2 record must yield every one of
    them, not just whichever sorts first."""
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "22 27 0:21 / /sys shared:7 - sysfs sysfs rw\n"
        "38 28 0:32 /other.slice /fake/cgroup-a rw - cgroup2 cgroup2 rw\n"
        "39 28 0:32 / /fake/cgroup-b rw - cgroup2 cgroup2 rw\n"
    )
    monkeypatch.setattr(ab, "_PROC_SELF_MOUNTINFO_PATH", str(mountinfo))
    assert ab._read_cgroup_v2_mounts() == [
        ("/other.slice", "/fake/cgroup-a"),
        ("/", "/fake/cgroup-b"),
    ]


def test_read_cgroup_v2_mounts_empty_when_no_cgroup2_entry(tmp_path, monkeypatch):
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text("22 27 0:21 / /sys shared:7 - sysfs sysfs rw\n")
    monkeypatch.setattr(ab, "_PROC_SELF_MOUNTINFO_PATH", str(mountinfo))
    assert ab._read_cgroup_v2_mounts() == []


@pytest.mark.parametrize("bad_content", ["", "not mountinfo at all\n", "38 28 0:32 too few fields\n"])
def test_read_cgroup_v2_mounts_empty_on_malformed_content(tmp_path, monkeypatch, bad_content):
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(bad_content)
    monkeypatch.setattr(ab, "_PROC_SELF_MOUNTINFO_PATH", str(mountinfo))
    assert ab._read_cgroup_v2_mounts() == []


def test_read_cgroup_v2_mounts_empty_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(ab, "_PROC_SELF_MOUNTINFO_PATH", str(tmp_path / "does_not_exist"))
    assert ab._read_cgroup_v2_mounts() == []


# --- _resolve_cgroup_mapping: pure, per-mount applicability + resolution ---

def test_resolve_cgroup_mapping_mount_root_regression():
    """AGENT-BACKUP-RESTORE-A2F12 Section 15's literal regression, still
    required under the new per-mount API: logical process cgroup
    /user.slice/app.scope, cgroup2 mount root /user.slice, mount point
    <fake>. The resolved leaf must be <fake>/app.scope, NOT
    <fake>/user.slice/app.scope (double-appending the mount's own root
    segment is exactly the namespace/subtree-mount mistake this guards
    against)."""
    resolved = ab._resolve_cgroup_mapping("/user.slice/app.scope", "/user.slice", "/fake/cgroup")
    assert resolved == "/fake/cgroup/app.scope"
    assert resolved != "/fake/cgroup/user.slice/app.scope"


def test_resolve_cgroup_mapping_root_mount_root():
    resolved = ab._resolve_cgroup_mapping(
        "/user.slice/user-1000.slice/app.scope", "/", "/fake/cgroup",
    )
    assert resolved == "/fake/cgroup/user.slice/user-1000.slice/app.scope"


def test_resolve_cgroup_mapping_process_at_mount_root_itself():
    assert ab._resolve_cgroup_mapping("/", "/", "/fake/cgroup") == "/fake/cgroup"


def test_resolve_cgroup_mapping_exact_match_root_equals_logical_path():
    assert ab._resolve_cgroup_mapping(
        "/user.slice/app.scope", "/user.slice/app.scope", "/fake/cgroup",
    ) == "/fake/cgroup"


def test_resolve_cgroup_mapping_none_when_logical_path_outside_mount_root():
    """The process's cgroup is not visible under this mount at all (a
    namespace/subtree boundary) -- an honest None, never a guess."""
    assert ab._resolve_cgroup_mapping(
        "/some/other/slice/app.scope", "/user.slice", "/fake/cgroup",
    ) is None


def test_resolve_cgroup_mapping_component_boundary_not_naive_string_prefix():
    """AGENT-BACKUP-RESTORE-A2F13 Section 5's explicit warning: a mount
    rooted at /user.slice must NOT be treated as applicable to a process
    logically under /user.slice2 -- a sibling path that merely shares a
    string prefix, never a real subtree."""
    assert ab._resolve_cgroup_mapping(
        "/user.slice2/app.scope", "/user.slice", "/fake/cgroup",
    ) is None


# --- _resolve_process_cgroup_dir: single-answer convenience wrapper ---

def test_resolve_process_cgroup_dir_uses_first_applicable_mount(tmp_path, monkeypatch):
    mount_point = _write_cgroup_v2_fixture(
        tmp_path, monkeypatch,
        logical_path="/user.slice/app.scope",
        mount_root="/user.slice",
    )
    resolved = ab._resolve_process_cgroup_dir()
    assert resolved == str(mount_point / "app.scope")


def test_resolve_process_cgroup_dir_none_when_logical_path_outside_mount_root(tmp_path, monkeypatch):
    _write_cgroup_v2_fixture(
        tmp_path, monkeypatch,
        logical_path="/some/other/slice/app.scope",
        mount_root="/user.slice",
    )
    assert ab._resolve_process_cgroup_dir() is None


def test_resolve_process_cgroup_dir_none_when_cgroup_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(ab, "_PROC_SELF_CGROUP_PATH", str(tmp_path / "missing_cgroup"))
    monkeypatch.setattr(ab, "_PROC_SELF_MOUNTINFO_PATH", str(tmp_path / "missing_mountinfo"))
    assert ab._resolve_process_cgroup_dir() is None


# --- _classify_cgroup_memory_level: tri-state single-level reader ---

def test_classify_cgroup_memory_level_finite(tmp_path):
    _write_memory_files(tmp_path, "536870912", "134217728")  # 512 MiB / 128 MiB
    assert ab._classify_cgroup_memory_level(str(tmp_path)) == ("finite", 402653184)  # 384 MiB


def test_classify_cgroup_memory_level_never_negative_when_current_exceeds_max(tmp_path):
    _write_memory_files(tmp_path, "1000", "2000")
    assert ab._classify_cgroup_memory_level(str(tmp_path)) == ("finite", 0)


def test_classify_cgroup_memory_level_unlimited_on_literal_max(tmp_path):
    _write_memory_files(tmp_path, "max", "999999999")
    assert ab._classify_cgroup_memory_level(str(tmp_path)) == ("unlimited", None)


def test_classify_cgroup_memory_level_unknown_when_memory_max_missing(tmp_path):
    assert ab._classify_cgroup_memory_level(str(tmp_path)) == ("unknown", None)


def test_classify_cgroup_memory_level_unknown_when_memory_max_malformed(tmp_path):
    _write_memory_files(tmp_path, "not-a-number", "0")
    assert ab._classify_cgroup_memory_level(str(tmp_path)) == ("unknown", None)


def test_classify_cgroup_memory_level_unknown_when_memory_current_missing(tmp_path):
    _write_memory_files(tmp_path, "1024", None)
    assert ab._classify_cgroup_memory_level(str(tmp_path)) == ("unknown", None)


# --- Descriptor-bound positive cgroup-root authentication (A2F15). ---

def _make_synthetic_cgroup2_root(path, monkeypatch):
    """Model kernel-created cgroup2 nodes while retaining tmp_path control.

    Filesystem-type authentication itself is separately tested against the
    live cgroup2 mount and a real ordinary temporary filesystem.
    """
    path.mkdir(parents=True, exist_ok=True)
    (path / ab._CGROUP_V2_CONTROLLERS_FILENAME).write_bytes(b"")
    monkeypatch.setattr(ab, "_descriptor_is_cgroup2", lambda _fd: True)


def test_descriptor_is_cgroup2_accepts_live_interface_and_rejects_ordinary_directory(tmp_path):
    if not os.path.isdir("/sys/fs/cgroup"):
        pytest.skip("live cgroup2 mount unavailable")
    live_fd = os.open("/sys/fs/cgroup", os.O_RDONLY | os.O_DIRECTORY)
    ordinary_fd = os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert ab._descriptor_is_cgroup2(live_fd) is True
        assert ab._descriptor_is_cgroup2(ordinary_fd) is False
    finally:
        os.close(live_fd)
        os.close(ordinary_fd)


def test_observe_control_file_at_accepts_successful_empty_observation(tmp_path):
    (tmp_path / "cgroup.controllers").write_bytes(b"")
    dir_fd = os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert ab._observe_control_file_at(dir_fd, "cgroup.controllers") is True
    finally:
        os.close(dir_fd)


def test_probe_control_file_enoent_true_when_confirmed_missing(tmp_path):
    dir_fd = os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert ab._probe_control_file_enoent(dir_fd, "does-not-exist") is True
    finally:
        os.close(dir_fd)


def test_probe_control_file_enoent_false_when_openable(tmp_path):
    (tmp_path / "present").write_text("max")
    dir_fd = os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert ab._probe_control_file_enoent(dir_fd, "present") is False
    finally:
        os.close(dir_fd)


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EPERM, errno.EIO, errno.ENOTDIR])
def test_probe_control_file_enoent_none_on_non_enoent_error(tmp_path, monkeypatch, error_number):
    dir_fd = os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY)
    real_open = os.open

    def failing_open(path, flags, *args, **kwargs):
        if kwargs.get("dir_fd") == dir_fd:
            raise OSError(error_number, os.strerror(error_number))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(ab.os, "open", failing_open)
    try:
        assert ab._probe_control_file_enoent(dir_fd, "control") is None
    finally:
        os.close(dir_fd)


def test_probe_control_file_enoent_none_when_control_is_directory(tmp_path):
    (tmp_path / "control").mkdir()
    dir_fd = os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert ab._probe_control_file_enoent(dir_fd, "control") is None
    finally:
        os.close(dir_fd)


def test_confirmed_true_cgroup_root_false_when_candidate_does_not_exist(tmp_path):
    assert ab._confirmed_true_cgroup_root(str(tmp_path / "missing")) is False


def test_confirmed_true_cgroup_root_false_for_empty_ordinary_directory(tmp_path):
    assert ab._confirmed_true_cgroup_root(str(tmp_path)) is False


def test_confirmed_true_cgroup_root_false_for_ordinary_directory_with_forged_core_file(tmp_path):
    (tmp_path / "cgroup.controllers").write_text("memory")
    assert ab._confirmed_true_cgroup_root(str(tmp_path)) is False


def test_confirmed_true_cgroup_root_true_after_positive_interface_authentication(tmp_path, monkeypatch):
    _make_synthetic_cgroup2_root(tmp_path, monkeypatch)
    assert ab._confirmed_true_cgroup_root(str(tmp_path)) is True


def test_confirmed_true_cgroup_root_false_when_memory_max_present(tmp_path, monkeypatch):
    _make_synthetic_cgroup2_root(tmp_path, monkeypatch)
    _write_memory_files(tmp_path, "max", "0")
    assert ab._confirmed_true_cgroup_root(str(tmp_path)) is False


def test_confirmed_true_cgroup_root_false_when_cgroup_type_present(tmp_path, monkeypatch):
    _make_synthetic_cgroup2_root(tmp_path, monkeypatch)
    (tmp_path / "cgroup.type").write_text("domain")
    assert ab._confirmed_true_cgroup_root(str(tmp_path)) is False


def test_confirmed_true_cgroup_root_observations_use_one_directory_descriptor(tmp_path, monkeypatch):
    _make_synthetic_cgroup2_root(tmp_path, monkeypatch)
    real_open = os.open
    directory_opens = []
    child_opens = []

    def recording_open(path, flags, *args, **kwargs):
        if path == str(tmp_path) and kwargs.get("dir_fd") is None:
            fd = real_open(path, flags, *args, **kwargs)
            directory_opens.append(fd)
            return fd
        elif kwargs.get("dir_fd") is not None:
            child_opens.append((path, kwargs["dir_fd"]))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(ab.os, "open", recording_open)
    assert ab._confirmed_true_cgroup_root(str(tmp_path)) is True
    assert len(directory_opens) == 1
    assert [name for name, _ in child_opens] == [
        "cgroup.controllers", "cgroup.type", "memory.max",
    ]
    assert all(fd == directory_opens[0] for _, fd in child_opens)


def test_confirmed_true_cgroup_root_path_replacement_stays_on_opened_identity(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate"
    displaced = tmp_path / "displaced"
    replacement = candidate
    candidate.mkdir()
    (candidate / "cgroup.controllers").write_bytes(b"")
    calls = {"n": 0}

    def replace_after_acquisition(_fd):
        calls["n"] += 1
        if calls["n"] == 1:
            candidate.rename(displaced)
            replacement.mkdir()
            return True
        return False  # the ordinary replacement never authenticates

    monkeypatch.setattr(ab, "_descriptor_is_cgroup2", replace_after_acquisition)
    assert ab._confirmed_true_cgroup_root(str(candidate)) is True
    assert ab._confirmed_true_cgroup_root(str(candidate)) is False


def test_confirmed_true_cgroup_root_partial_interface_disappearance_fails_closed(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    controllers = candidate / "cgroup.controllers"
    controllers.write_bytes(b"")

    def remove_core_after_acquisition(_fd):
        controllers.unlink()
        return True

    monkeypatch.setattr(ab, "_descriptor_is_cgroup2", remove_core_after_acquisition)
    assert ab._confirmed_true_cgroup_root(str(candidate)) is False


@pytest.mark.parametrize("failure_case", ["success", "permission", "malformed", "unexpected"])
def test_confirmed_true_cgroup_root_always_closes_directory_descriptor(
    tmp_path, monkeypatch, failure_case,
):
    candidate = tmp_path / failure_case
    _make_synthetic_cgroup2_root(candidate, monkeypatch)
    real_open = os.open
    opened = {}

    def tracking_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if path == str(candidate) and kwargs.get("dir_fd") is None:
            opened["directory_fd"] = fd
        return fd

    monkeypatch.setattr(ab.os, "open", tracking_open)
    if failure_case == "permission":
        monkeypatch.setattr(ab.os, "read", lambda *_args: (_ for _ in ()).throw(PermissionError()))
    elif failure_case == "malformed":
        (candidate / "cgroup.controllers").unlink()
        (candidate / "cgroup.controllers").mkdir()
    elif failure_case == "unexpected":
        monkeypatch.setattr(ab, "_descriptor_is_cgroup2", lambda _fd: (_ for _ in ()).throw(RuntimeError()))

    expected = failure_case == "success"
    assert ab._confirmed_true_cgroup_root(str(candidate)) is expected
    with pytest.raises(OSError) as excinfo:
        os.fstat(opened["directory_fd"])
    assert excinfo.value.errno == errno.EBADF


# --- _evaluate_cgroup_mapping: ancestor walk for ONE resolved mapping ---
#
# These tests pass a non-"/" `mount_root` ("/fake-root") throughout --
# that keeps them exercising the general ancestor-walk logic without
# tripping the true-kernel-root special case covered separately below.

def test_evaluate_cgroup_mapping_does_not_stop_at_first_finite_limit(tmp_path):
    """AGENT-BACKUP-RESTORE-A2F12 Section 7's literal regression, still
    required: the walk must find the TIGHTER nested ancestor limit, not
    just the mount point's own root figure."""
    mount_point = tmp_path / "mount"
    leaf = mount_point / "nested"
    _write_memory_files(mount_point, "4096", "512")  # mount point: available 3584
    _write_memory_files(leaf, "1024", "768")  # nested: available 256
    assert ab._evaluate_cgroup_mapping(str(leaf), "/fake-root", str(mount_point)) == ("finite", 256)


def test_evaluate_cgroup_mapping_mount_point_unlimited_leaf_finite(tmp_path):
    """Section 8's literal regression: the old implementation, reading
    only the mount-point level, would have reported unlimited (max at
    that level). The fix must find the finite NESTED limit instead."""
    mount_point = tmp_path / "mount"
    leaf = mount_point / "nested"
    _write_memory_files(mount_point, "max", "999999999")
    nested_max, nested_current = 512 * 1024 * 1024, 128 * 1024 * 1024
    _write_memory_files(leaf, str(nested_max), str(nested_current))
    assert ab._evaluate_cgroup_mapping(str(leaf), "/fake-root", str(mount_point)) == (
        "finite", nested_max - nested_current,
    )


def test_evaluate_cgroup_mapping_mount_point_broken_leaf_finite_retains_uncertainty(tmp_path):
    """AGENT-BACKUP-RESTORE-A2F14 (P0-1, repairing A2R14's literal
    same-mapping regression): this test used to assert plain
    ('finite', 192) -- exactly the forbidden 'FINITE(leaf) + UNKNOWN
    (mount point) -> FINITE(leaf)' collapse. A malformed mount-point-
    level memory.max is a genuinely unresolved reading; it can conceal an
    arbitrarily tighter real ceiling this walk simply failed to observe,
    so it must never be silently discarded just because a valid nested
    leaf happened to read cleanly. The known, tighter leaf figure (192)
    must still be RETAINED alongside the uncertainty, never dropped
    outright either -- more uncertainty may reduce authority, but it must
    never erase a known number along with it."""
    mount_point = tmp_path / "mount"
    leaf = mount_point / "nested"
    _write_memory_files(mount_point, "not-a-number", "0")
    _write_memory_files(leaf, "256", "64")
    assert ab._evaluate_cgroup_mapping(str(leaf), "/fake-root", str(mount_point)) == ("unknown", 192)


def test_evaluate_cgroup_mapping_unlimited_when_every_level_explicit_max(tmp_path):
    mount_point = tmp_path / "mount"
    leaf = mount_point / "user.slice" / "app.scope"
    _write_memory_files(mount_point, "max", "0")
    _write_memory_files(mount_point / "user.slice", "max", "0")
    _write_memory_files(leaf, "max", "0")
    assert ab._evaluate_cgroup_mapping(str(leaf), "/fake-root", str(mount_point)) == ("unlimited", None)


def test_evaluate_cgroup_mapping_unknown_when_no_level_has_memory_files(tmp_path):
    mount_point = tmp_path / "mount"
    leaf = mount_point / "user.slice" / "app.scope"
    leaf.mkdir(parents=True)
    assert ab._evaluate_cgroup_mapping(str(leaf), "/fake-root", str(mount_point)) == ("unknown", None)


def test_evaluate_cgroup_mapping_three_level_tightest_wins(tmp_path):
    """Section 14's requested fixture shape: a three-level fake hierarchy
    (mount point / user.slice / user.slice/app.scope), tightest ancestor
    wins."""
    mount_point = tmp_path / "mount"
    leaf = mount_point / "user.slice" / "app.scope"
    _write_memory_files(mount_point, "2147483648", "0")  # 2 GiB available
    _write_memory_files(mount_point / "user.slice", "1073741824", "0")  # 1 GiB available
    _write_memory_files(leaf, "268435456", "134217728")  # 128 MiB available
    assert ab._evaluate_cgroup_mapping(str(leaf), "/fake-root", str(mount_point)) == ("finite", 134217728)


def test_evaluate_cgroup_mapping_true_kernel_root_missing_memory_max_is_not_broken(tmp_path, monkeypatch):
    """Live-verified against a real host (Documentation/admin-guide/
    cgroup-v2.rst): the true, whole-system ROOT cgroup structurally has
    NO memory.max file at all -- this is the kernel's own documented
    shape, not a broken/unreadable level. A mount whose OWN root is '/'
    (it exposes the entire real hierarchy) must not have its terminal,
    file-less level poison an otherwise fully-resolved, all-unlimited
    mapping into 'unknown' -- every REAL ancestor here explicitly says
    unlimited, so the mapping as a whole must be 'unlimited', matching
    what this process's own real /sys/fs/cgroup tree does."""
    mount_point = tmp_path / "mount"  # mount_root IS "/" -- stands in for /sys/fs/cgroup itself
    leaf = mount_point / "user.slice" / "app.scope"
    _make_synthetic_cgroup2_root(mount_point, monkeypatch)  # valid interface, no root-only files
    _write_memory_files(mount_point / "user.slice", "max", "0")
    _write_memory_files(leaf, "max", "0")
    assert ab._evaluate_cgroup_mapping(str(leaf), "/", str(mount_point)) == ("unlimited", None)


def test_evaluate_cgroup_mapping_true_kernel_root_missing_memory_max_still_finds_finite_nested(
    tmp_path, monkeypatch,
):
    """Same true-root exception, but with a real finite ceiling found at
    a nested level -- the exception only forgives the ROOT level's
    absence, it never suppresses a genuine finite reading elsewhere."""
    mount_point = tmp_path / "mount"
    leaf = mount_point / "user.slice" / "app.scope"
    _make_synthetic_cgroup2_root(mount_point, monkeypatch)  # valid interface, no root-only files
    _write_memory_files(mount_point / "user.slice", "max", "0")
    _write_memory_files(leaf, "268435456", "134217728")  # 128 MiB available
    assert ab._evaluate_cgroup_mapping(str(leaf), "/", str(mount_point)) == ("finite", 134217728)


def test_evaluate_cgroup_mapping_missing_memory_max_at_non_root_mount_point_is_still_unknown(tmp_path):
    """The true-root exception is scoped to mount_root == '/' ONLY -- a
    subtree-bound or namespaced mount's own top level is a real, ordinary
    cgroup (not the kernel's special rootless root), so a missing
    memory.max there is a genuine, unresolvable failure, exactly as
    before this exception existed."""
    mount_point = tmp_path / "mount"
    leaf = mount_point / "app.scope"
    mount_point.mkdir()  # no memory.max -- but mount_root below is NOT "/"
    _write_memory_files(leaf, "max", "0")
    assert ab._evaluate_cgroup_mapping(str(leaf), "/user.slice", str(mount_point)) == ("unknown", None)


def test_evaluate_cgroup_mapping_empty_ordinary_root_candidate_is_unknown(tmp_path):
    """A directory is not a cgroup interface merely because root-only files
    are absent from it."""
    mount_point = tmp_path / "ordinary"
    mount_point.mkdir()
    assert ab._evaluate_cgroup_mapping(str(mount_point), "/", str(mount_point)) == ("unknown", None)


def test_discover_cgroup_v2_availability_stale_mount_mapping_is_unknown(tmp_path, monkeypatch):
    """A once-present mount path that vanishes after mountinfo capture can
    never mint UNLIMITED authority from its children's ENOENT results."""
    _write_proc_self_cgroup(tmp_path, monkeypatch, "/")
    stale_mount = tmp_path / "fake" / "cgroup"
    stale_mount.mkdir(parents=True)
    _write_mountinfo(tmp_path, monkeypatch, [("/", str(stale_mount))])
    stale_mount.rmdir()
    assert ab._discover_cgroup_v2_availability() == ("unknown", None)


def test_discover_cgroup_v2_availability_finite_plus_vanished_root_retains_both_facts(
    tmp_path, monkeypatch,
):
    """Literal A2R15 algebra replay: FINITE(1 GiB) plus a vanished root
    candidate is UNKNOWN-with-known-finite, never a clean FINITE verdict."""
    _write_proc_self_cgroup(tmp_path, monkeypatch, "/app.scope")
    finite_mount = tmp_path / "finite"
    vanished_mount = tmp_path / "vanished"
    _write_memory_files(finite_mount, str(1024**3), "0")
    vanished_mount.mkdir()
    _write_mountinfo(tmp_path, monkeypatch, [
        ("/app.scope", str(finite_mount)),
        ("/", str(vanished_mount)),
    ])
    vanished_mount.rmdir()
    assert ab._discover_cgroup_v2_availability() == ("unknown", 1024**3)


# --- Section 13's namespace-root regression: mount_root == "/" alone must
# never be treated as proof of true-root identity (A2F14 P0-2). ---

def test_evaluate_cgroup_mapping_namespace_displayed_root_with_finite_memory_max_reads_normally(tmp_path):
    """Both /proc/self/cgroup and this mount's own root field display
    '/' -- exactly like the true kernel root -- but this is really a
    namespace-remapped non-root cgroup that DOES have a real, finite
    memory.max of its own. It must be read completely normally,
    regardless of what '/' happens to display (Section 13's first
    requirement)."""
    mount_point = tmp_path / "mount"  # mount_root will be passed as "/"
    _write_memory_files(mount_point, "268435456", "134217728")  # 128 MiB available
    (mount_point / "cgroup.type").write_text("domain")  # proves this is NOT the true root
    assert ab._evaluate_cgroup_mapping(str(mount_point), "/", str(mount_point)) == ("finite", 134217728)


def test_evaluate_cgroup_mapping_namespace_displayed_root_unresolved_control_is_unknown_not_unlimited(tmp_path):
    """AGENT-BACKUP-RESTORE-A2F14's literal P0-2 replay: same '/' display
    on both sides as the true root, but cgroup.type is PRESENT -- the
    kernel-documented signature of a real non-root cgroup
    (cgroups-v2.rst Sec 5.5) -- and memory.max cannot be established.
    The old bare `mount_root == "/"` check forgave this outright as
    'unlimited'; it must now be 'unknown' instead (Section 13's second
    requirement: 'do not forgive it merely because both displayed paths
    equal /')."""
    mount_point = tmp_path / "mount"
    mount_point.mkdir()  # no memory.max at all
    (mount_point / "cgroup.type").write_text("domain")  # proves this is NOT the true root
    assert ab._evaluate_cgroup_mapping(str(mount_point), "/", str(mount_point)) == ("unknown", None)


def test_evaluate_cgroup_mapping_true_root_still_recognized_when_cgroup_type_also_absent(
    tmp_path, monkeypatch,
):
    """Section 14's true-root regression, restated directly against the
    new positive signal: when cgroup.type is ALSO confirmed absent
    (never written in this fixture, matching this host's own live-
    verified /sys/fs/cgroup), the true-root exception must still apply,
    exactly as it did before A2F14 -- this pass must not 'fix' safety by
    permanently forcing every normal host through the conservative
    fallback."""
    mount_point = tmp_path / "mount"
    leaf = mount_point / "user.slice" / "app.scope"
    _make_synthetic_cgroup2_root(mount_point, monkeypatch)
    _write_memory_files(mount_point / "user.slice", "max", "0")
    _write_memory_files(leaf, "max", "0")
    assert ab._evaluate_cgroup_mapping(str(leaf), "/", str(mount_point)) == ("unlimited", None)


# --- _discover_cgroup_v2_availability: single-mount regressions (A2F12) ---

def test_discover_cgroup_v2_availability_never_negative_when_current_exceeds_max(tmp_path, monkeypatch):
    """A transient accounting race (current briefly above max) must
    still report a finite, non-negative figure."""
    mount_point = _write_cgroup_v2_fixture(tmp_path, monkeypatch, logical_path="/", mount_root="/")
    _write_memory_files(mount_point, "1000", "2000")
    assert ab._discover_cgroup_v2_availability() == ("finite", 0)


def test_discover_cgroup_v2_availability_computes_max_minus_current_at_resolved_leaf(tmp_path, monkeypatch):
    mount_point = _write_cgroup_v2_fixture(tmp_path, monkeypatch, logical_path="/", mount_root="/")
    _write_memory_files(mount_point, "536870912", "134217728")  # 512 MiB / 128 MiB
    assert ab._discover_cgroup_v2_availability() == ("finite", 402653184)  # 384 MiB


def test_discover_cgroup_v2_availability_unlimited_when_max_is_literal_max_at_every_level(tmp_path, monkeypatch):
    """AGENT-BACKUP-RESTORE-A2F13 Section 9's semantic split from A2F12:
    this used to collapse to bare None -- now it must be the explicit
    'unlimited' state, distinguishable from 'unknown'."""
    mount_point = _write_cgroup_v2_fixture(
        tmp_path, monkeypatch, logical_path="/user.slice/app.scope", mount_root="/",
    )
    _write_memory_files(mount_point, "max", "0")
    _write_memory_files(mount_point / "user.slice", "max", "0")
    _write_memory_files(mount_point / "user.slice" / "app.scope", "max", "0")
    assert ab._discover_cgroup_v2_availability() == ("unlimited", None)


def test_discover_cgroup_v2_availability_unknown_when_no_ancestor_has_memory_files(tmp_path, monkeypatch):
    mount_point = _write_cgroup_v2_fixture(
        tmp_path, monkeypatch, logical_path="/user.slice/app.scope", mount_root="/",
    )
    (mount_point / "user.slice" / "app.scope").mkdir(parents=True)
    assert ab._discover_cgroup_v2_availability() == ("unknown", None)


def test_discover_cgroup_v2_availability_unknown_when_cgroup2_mount_absent(tmp_path, monkeypatch):
    """Section 19's discovery-failure matrix: no cgroup2 entry in
    mountinfo at all (a masked/sandboxed /proc) -- must degrade to
    'unknown', never raise, never synthesize a constraint out of
    nothing."""
    _write_proc_self_cgroup(tmp_path, monkeypatch, "/")
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text("22 27 0:21 / /sys shared:7 - sysfs sysfs rw\n")
    monkeypatch.setattr(ab, "_PROC_SELF_MOUNTINFO_PATH", str(mountinfo))
    assert ab._discover_cgroup_v2_availability() == ("unknown", None)


def test_discover_cgroup_v2_availability_unknown_when_process_cgroup_path_unreadable(tmp_path, monkeypatch):
    monkeypatch.setattr(ab, "_PROC_SELF_CGROUP_PATH", str(tmp_path / "missing"))
    mount_point = tmp_path / "mount"
    mount_point.mkdir()
    _write_mountinfo(tmp_path, monkeypatch, [("/", str(mount_point))])
    assert ab._discover_cgroup_v2_availability() == ("unknown", None)


# --- _discover_cgroup_v2_availability: the A2R13 multi-mount matrix ---

def test_discover_cgroup_v2_availability_literal_a2r13_regression(tmp_path, monkeypatch):
    """AGENT-BACKUP-RESTORE-A2F13's own reason for existing -- the exact
    A2R13 P0 fixture (Section 12): mountinfo carries TWO cgroup2 records,
    an unrelated one first (root /other.slice) and the actually-
    applicable one second (root /). The old first-match reader picked
    the unrelated mount, failed process-path resolution under it, and
    discarded the cgroup constraint entirely (falling back to host-only).
    The fix must find the applicable SECOND mount and report its finite
    384 MiB figure -- never 'unknown' and never a host-only result."""
    _write_proc_self_cgroup(tmp_path, monkeypatch, "/user.slice/app.scope")
    cgroup_a = tmp_path / "fake" / "cgroup-a"  # unrelated -- must never be consulted
    cgroup_b = tmp_path / "fake" / "cgroup-b"  # applicable
    cgroup_a.mkdir(parents=True)
    cgroup_b.mkdir(parents=True)
    _write_mountinfo(tmp_path, monkeypatch, [
        ("/other.slice", str(cgroup_a)),
        ("/", str(cgroup_b)),
    ])
    # AGENT-BACKUP-RESTORE-A2F14: this test's own concern is mount
    # SELECTION (finding the applicable second mount), not ancestor-walk
    # uncertainty -- so every ancestor above the leaf is given an
    # explicit, unambiguous 'unlimited' reading. Leaving these
    # unpopulated (as before A2F14) would now correctly surface as
    # genuine uncertainty at those levels, which is a different concern
    # covered by its own dedicated tests above.
    _write_memory_files(cgroup_b, "max", "0")
    _write_memory_files(cgroup_b / "user.slice", "max", "0")
    _write_memory_files(cgroup_b / "user.slice" / "app.scope", "536870912", "134217728")  # 512/128 MiB
    assert ab._discover_cgroup_v2_availability() == ("finite", 402653184)  # 384 MiB


def test_discover_cgroup_v2_availability_applicable_first_unlimited_second_finite(tmp_path, monkeypatch):
    _write_proc_self_cgroup(tmp_path, monkeypatch, "/app.scope")
    mount_a, mount_b = tmp_path / "mount-a", tmp_path / "mount-b"
    mount_a.mkdir()
    mount_b.mkdir()
    _make_synthetic_cgroup2_root(mount_a, monkeypatch)
    _make_synthetic_cgroup2_root(mount_b, monkeypatch)
    _write_mountinfo(tmp_path, monkeypatch, [("/", str(mount_a)), ("/", str(mount_b))])
    _write_memory_files(mount_a / "app.scope", "max", "0")
    _write_memory_files(mount_b / "app.scope", "268435456", "134217728")  # 128 MiB available
    assert ab._discover_cgroup_v2_availability() == ("finite", 134217728)


def test_discover_cgroup_v2_availability_applicable_first_finite_second_tighter(tmp_path, monkeypatch):
    _write_proc_self_cgroup(tmp_path, monkeypatch, "/app.scope")
    mount_a, mount_b = tmp_path / "mount-a", tmp_path / "mount-b"
    mount_a.mkdir()
    mount_b.mkdir()
    _make_synthetic_cgroup2_root(mount_a, monkeypatch)
    _make_synthetic_cgroup2_root(mount_b, monkeypatch)
    _write_mountinfo(tmp_path, monkeypatch, [("/", str(mount_a)), ("/", str(mount_b))])
    _write_memory_files(mount_a / "app.scope", "1073741824", "0")  # 1 GiB available
    _write_memory_files(mount_b / "app.scope", "268435456", "134217728")  # 128 MiB -- tighter
    assert ab._discover_cgroup_v2_availability() == ("finite", 134217728)


def test_discover_cgroup_v2_availability_applicable_subtree_and_root_mounts(tmp_path, monkeypatch):
    """One mount exposes only the /user.slice subtree, another exposes
    the whole hierarchy from /  -- both are applicable to a process under
    /user.slice/app.scope, and the tighter of the two must win."""
    _write_proc_self_cgroup(tmp_path, monkeypatch, "/user.slice/app.scope")
    subtree_mount, root_mount = tmp_path / "subtree-mount", tmp_path / "root-mount"
    subtree_mount.mkdir()
    root_mount.mkdir()
    _write_mountinfo(tmp_path, monkeypatch, [
        ("/user.slice", str(subtree_mount)),
        ("/", str(root_mount)),
    ])
    # AGENT-BACKUP-RESTORE-A2F14: this test's own concern is which of two
    # APPLICABLE mounts wins (tighter figure), not ancestor-walk
    # uncertainty -- every ancestor above each leaf is given an explicit,
    # unambiguous 'unlimited' reading so neither mapping's own walk
    # introduces incidental uncertainty.
    _write_memory_files(subtree_mount, "max", "0")
    _write_memory_files(subtree_mount / "app.scope", "1073741824", "0")  # 1 GiB available
    _write_memory_files(root_mount, "max", "0")
    _write_memory_files(root_mount / "user.slice", "max", "0")
    _write_memory_files(root_mount / "user.slice" / "app.scope", "268435456", "134217728")  # 128 MiB
    assert ab._discover_cgroup_v2_availability() == ("finite", 134217728)


def test_discover_cgroup_v2_availability_broken_applicable_first_valid_second_retains_uncertainty(tmp_path, monkeypatch):
    """AGENT-BACKUP-RESTORE-A2F14 (P0-1, repairing A2R14's literal
    cross-mapping regression): this test used to assert plain
    ('finite', 134217728) -- exactly the forbidden 'mapping A: FINITE,
    mapping B: UNKNOWN -> FINITE' collapse. mount_a is a genuinely
    unresolvable applicable mapping (no memory files anywhere in its own
    walk); it can conceal an arbitrarily tighter real ceiling, so it must
    never be silently outvoted just because a DIFFERENT applicable
    mapping (mount_b) happened to read cleanly. The known, tighter figure
    mount_b observed (128 MiB) must still be retained alongside the
    uncertainty."""
    _write_proc_self_cgroup(tmp_path, monkeypatch, "/app.scope")
    mount_a, mount_b = tmp_path / "mount-a", tmp_path / "mount-b"
    (mount_a / "app.scope").mkdir(parents=True)  # applicable, no memory files -- unresolvable
    (mount_b / "app.scope").mkdir(parents=True)  # applicable, finite
    _write_mountinfo(tmp_path, monkeypatch, [("/", str(mount_a)), ("/", str(mount_b))])
    _write_memory_files(mount_b / "app.scope", "268435456", "134217728")  # 128 MiB
    assert ab._discover_cgroup_v2_availability() == ("unknown", 134217728)


def test_discover_cgroup_v2_availability_unrelated_then_broken_then_valid_retains_uncertainty(tmp_path, monkeypatch):
    """Same A2F14 P0-1 repair as the sibling test above, with a third,
    genuinely inapplicable mount mixed in -- inapplicable mounts must
    still be excluded entirely (never contribute 'unknown' themselves),
    but the genuinely-applicable-and-unresolvable mount_broken must still
    make the overall result 'unknown', retaining mount_valid's known
    128 MiB figure rather than letting it silently win outright."""
    _write_proc_self_cgroup(tmp_path, monkeypatch, "/app.scope")
    mount_unrelated = tmp_path / "mount-unrelated"
    mount_broken = tmp_path / "mount-broken"
    mount_valid = tmp_path / "mount-valid"
    mount_unrelated.mkdir()
    (mount_broken / "app.scope").mkdir(parents=True)
    (mount_valid / "app.scope").mkdir(parents=True)
    _write_mountinfo(tmp_path, monkeypatch, [
        ("/other.slice", str(mount_unrelated)),
        ("/", str(mount_broken)),
        ("/", str(mount_valid)),
    ])
    _write_memory_files(mount_valid / "app.scope", "268435456", "134217728")  # 128 MiB
    assert ab._discover_cgroup_v2_availability() == ("unknown", 134217728)


def test_discover_cgroup_v2_availability_all_unrelated_is_unknown(tmp_path, monkeypatch):
    _write_proc_self_cgroup(tmp_path, monkeypatch, "/user.slice/app.scope")
    mount_a, mount_b = tmp_path / "mount-a", tmp_path / "mount-b"
    mount_a.mkdir()
    mount_b.mkdir()
    _write_mountinfo(tmp_path, monkeypatch, [
        ("/other.slice", str(mount_a)),
        ("/another.slice", str(mount_b)),
    ])
    assert ab._discover_cgroup_v2_availability() == ("unknown", None)


def test_discover_cgroup_v2_availability_all_applicable_but_unreadable_is_unknown(tmp_path, monkeypatch):
    _write_proc_self_cgroup(tmp_path, monkeypatch, "/app.scope")
    mount_a, mount_b = tmp_path / "mount-a", tmp_path / "mount-b"
    (mount_a / "app.scope").mkdir(parents=True)  # no memory files
    (mount_b / "app.scope").mkdir(parents=True)  # no memory files
    _write_mountinfo(tmp_path, monkeypatch, [("/", str(mount_a)), ("/", str(mount_b))])
    assert ab._discover_cgroup_v2_availability() == ("unknown", None)


def test_discover_cgroup_v2_availability_duplicate_views_same_constraint_no_double_counting(tmp_path, monkeypatch):
    """Section 14: two mountinfo entries can expose the SAME underlying
    cgroup hierarchy through different mount points (a bind mount). When
    both report the identical finite constraint, the result must remain
    that one constraint -- never doubled, never a fluke of only one
    candidate ever being checked."""
    _write_proc_self_cgroup(tmp_path, monkeypatch, "/app.scope")
    bind_a, bind_b = tmp_path / "bind-a", tmp_path / "bind-b"
    _make_synthetic_cgroup2_root(bind_a, monkeypatch)
    _make_synthetic_cgroup2_root(bind_b, monkeypatch)
    _write_mountinfo(tmp_path, monkeypatch, [("/", str(bind_a)), ("/", str(bind_b))])
    _write_memory_files(bind_a / "app.scope", "268435456", "134217728")  # 128 MiB
    _write_memory_files(bind_b / "app.scope", "268435456", "134217728")  # identical 128 MiB
    assert ab._discover_cgroup_v2_availability() == ("finite", 134217728)


def test_discover_cgroup_v2_availability_duplicate_views_tighter_wins(tmp_path, monkeypatch):
    _write_proc_self_cgroup(tmp_path, monkeypatch, "/app.scope")
    bind_a, bind_b = tmp_path / "bind-a", tmp_path / "bind-b"
    _make_synthetic_cgroup2_root(bind_a, monkeypatch)
    _make_synthetic_cgroup2_root(bind_b, monkeypatch)
    _write_mountinfo(tmp_path, monkeypatch, [("/", str(bind_a)), ("/", str(bind_b))])
    _write_memory_files(bind_a / "app.scope", "1073741824", "0")  # 1 GiB
    _write_memory_files(bind_b / "app.scope", "268435456", "134217728")  # tighter, 128 MiB
    assert ab._discover_cgroup_v2_availability() == ("finite", 134217728)


# --- Section 16: combining BOTH A2F14 repairs at once -- monotonic
# aggregation and true-root recognition, mixed across mounts. ---

def test_discover_cgroup_v2_availability_finite_mount_plus_namespace_root_ambiguous_retains_uncertainty(tmp_path, monkeypatch):
    """mapping A: an ordinary, fully-resolved finite mount (1 GiB).
    mapping B: displays as '/' like the true root, but cgroup.type
    proves it is a real non-root cgroup whose own memory.max cannot be
    established. The combined result must retain the uncertainty
    (never silently resolve to mapping A's clean finite figure alone)."""
    _write_proc_self_cgroup(tmp_path, monkeypatch, "/app.scope")
    mount_finite, mount_ambiguous_root = tmp_path / "mount-finite", tmp_path / "mount-ambiguous-root"
    _write_memory_files(mount_finite / "app.scope", "1073741824", "0")  # 1 GiB, applicable
    (mount_ambiguous_root / "app.scope").mkdir(parents=True)
    (mount_ambiguous_root / "cgroup.type").write_text("domain")  # proves NOT the true root
    _write_mountinfo(tmp_path, monkeypatch, [("/", str(mount_finite)), ("/", str(mount_ambiguous_root))])
    assert ab._discover_cgroup_v2_availability() == ("unknown", 1073741824)


def test_discover_cgroup_v2_availability_finite_mount_plus_proven_true_root_unlimited_stays_finite(tmp_path, monkeypatch):
    """mapping A: an ordinary, fully-resolved finite mount (384 MiB).
    mapping B: positively established true root (both memory.max and
    cgroup.type confirmed absent, matching the kernel's own documented
    shape) -- contributes nothing. The combined result must be the
    clean finite figure, not degraded to 'unknown' just because another
    mapping happened to be the (legitimately unlimited) true root."""
    _write_proc_self_cgroup(tmp_path, monkeypatch, "/app.scope")
    mount_finite = tmp_path / "mount-finite"
    mount_root = tmp_path / "mount-root"
    _write_memory_files(mount_finite / "app.scope", "402653184", "0")  # 384 MiB available
    _make_synthetic_cgroup2_root(mount_finite, monkeypatch)
    # mount_root's own leaf (app.scope, the process's logical path appended
    # under this mount) is explicitly unlimited; mount_root ITSELF (the
    # true root, reached one level further up the walk) has no memory.max
    # and no cgroup.type at all -- the kernel's own documented shape.
    _write_memory_files(mount_root / "app.scope", "max", "0")
    _make_synthetic_cgroup2_root(mount_root, monkeypatch)
    _write_mountinfo(tmp_path, monkeypatch, [("/", str(mount_finite)), ("/", str(mount_root))])
    assert ab._discover_cgroup_v2_availability() == ("finite", 402653184)  # 384 MiB


# --- _discover_cgroup_v2_availability: one-snapshot-per-invocation (Section 15) ---

def test_discover_cgroup_v2_availability_reads_each_proc_source_exactly_once(tmp_path, monkeypatch):
    """AGENT-BACKUP-RESTORE-A2F13 Section 15: one discovery invocation
    must capture exactly ONE /proc/self/cgroup snapshot and exactly ONE
    /proc/self/mountinfo snapshot -- never rereading either mid-
    resolution."""
    mount_point = tmp_path / "mount"
    mount_point.mkdir()
    _make_synthetic_cgroup2_root(mount_point, monkeypatch)
    _write_proc_self_cgroup(tmp_path, monkeypatch, "/app.scope")
    _write_mountinfo(tmp_path, monkeypatch, [("/", str(mount_point))])
    _write_memory_files(mount_point / "app.scope", "268435456", "134217728")

    cgroup_path_calls = []
    real_read_cgroup_path = ab._read_process_cgroup_v2_path

    def counting_cgroup_path():
        cgroup_path_calls.append(1)
        return real_read_cgroup_path()

    mounts_calls = []
    real_read_mounts = ab._read_cgroup_v2_mounts

    def counting_mounts():
        mounts_calls.append(1)
        return real_read_mounts()

    monkeypatch.setattr(ab, "_read_process_cgroup_v2_path", counting_cgroup_path)
    monkeypatch.setattr(ab, "_read_cgroup_v2_mounts", counting_mounts)
    assert ab._discover_cgroup_v2_availability() == ("finite", 134217728)
    assert len(cgroup_path_calls) == 1
    assert len(mounts_calls) == 1


def test_discover_cgroup_v2_availability_does_not_mix_snapshot_generations(tmp_path, monkeypatch):
    """A stronger version of the same invariant: if the mount table were
    reread mid-resolution, a second, DIFFERENT generation would leak into
    the result. Simulates that by having the (mocked) mount reader return
    a first-generation value on its first call and a second,
    incompatible generation on any further call -- the real result must
    reflect ONLY the first generation."""
    _write_proc_self_cgroup(tmp_path, monkeypatch, "/app.scope")
    mount_gen_a, mount_gen_b = tmp_path / "mount-gen-a", tmp_path / "mount-gen-b"
    _make_synthetic_cgroup2_root(mount_gen_a, monkeypatch)
    _make_synthetic_cgroup2_root(mount_gen_b, monkeypatch)
    _write_mountinfo(tmp_path, monkeypatch, [("/", str(mount_gen_a))])
    _write_memory_files(mount_gen_a / "app.scope", "268435456", "134217728")  # 128 MiB
    _write_memory_files(mount_gen_b / "app.scope", "1073741824", "0")  # would be 1 GiB -- must NEVER be seen

    real_read_mounts = ab._read_cgroup_v2_mounts
    call_count = {"n": 0}

    def mutating_mounts():
        call_count["n"] += 1
        if call_count["n"] == 1:
            return real_read_mounts()
        return [("/", str(mount_gen_b))]  # a later "reread" would see a different mount

    monkeypatch.setattr(ab, "_read_cgroup_v2_mounts", mutating_mounts)
    assert ab._discover_cgroup_v2_availability() == ("finite", 134217728)
    assert call_count["n"] == 1


# --- Section 7: permutation invariance -- _finalize_cgroup_states is an
# evidence lattice, not a parser-order algorithm. ---

@pytest.mark.parametrize(
    "evidence",
    [
        [("finite", 1024**3), ("unknown", None), ("unlimited", None), ("finite", 64 * 1024**2)],
        [("finite", 5), ("finite", 3), ("finite", 9)],
        [("unknown", 512), ("unknown", None), ("finite", 256)],
        [("unlimited", None), ("unlimited", None), ("unlimited", None)],
        [("unknown", None), ("unknown", None)],
    ],
)
def test_finalize_cgroup_states_is_permutation_invariant(evidence):
    """AGENT-BACKUP-RESTORE-A2F14 Section 7: every permutation of the same
    evidence set must produce the IDENTICAL result -- this is an evidence
    lattice built from min()/any() over the whole set (Section 2/7), never
    a pairwise left-to-right fold whose outcome could depend on order."""
    results = {ab._finalize_cgroup_states(list(p)) for p in itertools.permutations(evidence)}
    assert len(results) == 1


# --- end-to-end: _effective_available_memory_bytes / _working_set_ceiling_bytes ---

def test_effective_available_memory_bytes_literal_a2r13_impact_regression(tmp_path, monkeypatch):
    """End-to-end: an abundant host (8 GiB) alongside the A2R13 multi-
    mount fixture (a real 384 MiB applicable cgroup constraint reachable
    only via the SECOND mount) must drive the combined effective-
    availability and working-set figures down to the cgroup's own
    tighter number -- not the host's, and not a host-only fallback caused
    by picking the wrong mount."""
    _write_proc_self_cgroup(tmp_path, monkeypatch, "/user.slice/app.scope")
    cgroup_a = tmp_path / "fake" / "cgroup-a"
    cgroup_b = tmp_path / "fake" / "cgroup-b"
    cgroup_a.mkdir(parents=True)
    cgroup_b.mkdir(parents=True)
    _write_mountinfo(tmp_path, monkeypatch, [
        ("/other.slice", str(cgroup_a)),
        ("/", str(cgroup_b)),
    ])
    # AGENT-BACKUP-RESTORE-A2F14: this test's own concern is end-to-end
    # mount selection driving the effective-availability figure, not
    # ancestor-walk uncertainty -- every ancestor above the leaf is given
    # an explicit, unambiguous 'unlimited' reading.
    _write_memory_files(cgroup_b, "max", "0")
    _write_memory_files(cgroup_b / "user.slice", "max", "0")
    nested_max, nested_current = 512 * 1024 * 1024, 128 * 1024 * 1024
    _write_memory_files(cgroup_b / "user.slice" / "app.scope", str(nested_max), str(nested_current))
    monkeypatch.setattr(ab, "_read_meminfo_available_bytes", lambda: 8 * 1024**3)  # 8 GiB host
    expected_n = nested_max - nested_current  # 384 MiB
    assert ab._effective_available_memory_bytes() == expected_n
    assert ab._working_set_ceiling_bytes() == int(expected_n * ab._RESOURCE_BUDGET_HOST_MEMORY_FRACTION)  # 192 MiB


def test_effective_available_memory_bytes_uses_tighter_of_host_and_cgroup(monkeypatch):
    monkeypatch.setattr(ab, "_read_meminfo_available_bytes", lambda: 8 * 1024**3)  # 8 GiB
    monkeypatch.setattr(ab, "_discover_cgroup_v2_availability", lambda: ("finite", 512 * 1024**2))  # 512 MiB
    assert ab._effective_available_memory_bytes() == 512 * 1024**2


def test_effective_available_memory_bytes_finite_cgroup_and_missing_host_uses_cgroup(monkeypatch):
    monkeypatch.setattr(ab, "_read_meminfo_available_bytes", lambda: None)
    monkeypatch.setattr(ab, "_discover_cgroup_v2_availability", lambda: ("finite", 256 * 1024**2))
    assert ab._effective_available_memory_bytes() == 256 * 1024**2


def test_effective_available_memory_bytes_unlimited_cgroup_uses_host(monkeypatch):
    """Section 11: an EXPLICITLY unlimited cgroup contributes no ceiling
    at all -- N is the host figure alone, not the fallback."""
    monkeypatch.setattr(ab, "_read_meminfo_available_bytes", lambda: 512 * 1024**2)  # 512 MiB
    monkeypatch.setattr(ab, "_discover_cgroup_v2_availability", lambda: ("unlimited", None))
    assert ab._effective_available_memory_bytes() == 512 * 1024**2


def test_effective_available_memory_bytes_unlimited_cgroup_and_missing_host_uses_fallback(monkeypatch):
    monkeypatch.setattr(ab, "_read_meminfo_available_bytes", lambda: None)
    monkeypatch.setattr(ab, "_discover_cgroup_v2_availability", lambda: ("unlimited", None))
    assert ab._effective_available_memory_bytes() == ab._FALLBACK_AVAILABLE_MEMORY_BYTES


def test_effective_available_memory_bytes_unknown_cgroup_caps_abundant_host(monkeypatch):
    """AGENT-BACKUP-RESTORE-A2F13 Section 10 -- the load-bearing
    fail-closed rule this whole pass exists to add: an UNKNOWN cgroup
    resolution must NEVER be treated as 'no constraint' and simply fall
    through to an abundant host figure (that is the literal A2R13 P0).
    It must be capped at the existing finite conservative fallback
    instead."""
    monkeypatch.setattr(ab, "_read_meminfo_available_bytes", lambda: 8 * 1024**3)  # 8 GiB
    monkeypatch.setattr(ab, "_discover_cgroup_v2_availability", lambda: ("unknown", None))
    assert ab._effective_available_memory_bytes() == ab._FALLBACK_AVAILABLE_MEMORY_BYTES


def test_effective_available_memory_bytes_unknown_cgroup_caps_at_min_of_host_and_fallback(monkeypatch):
    """The cap is min(host, fallback), not the bare fallback constant --
    a host that is itself SMALLER than the fallback must still win."""
    tiny_host = ab._FALLBACK_AVAILABLE_MEMORY_BYTES // 2
    monkeypatch.setattr(ab, "_read_meminfo_available_bytes", lambda: tiny_host)
    monkeypatch.setattr(ab, "_discover_cgroup_v2_availability", lambda: ("unknown", None))
    assert ab._effective_available_memory_bytes() == tiny_host


def test_effective_available_memory_bytes_falls_back_when_both_unavailable(monkeypatch):
    monkeypatch.setattr(ab, "_read_meminfo_available_bytes", lambda: None)
    monkeypatch.setattr(ab, "_discover_cgroup_v2_availability", lambda: ("unknown", None))
    assert ab._effective_available_memory_bytes() == ab._FALLBACK_AVAILABLE_MEMORY_BYTES


# --- AGENT-BACKUP-RESTORE-A2F14 Section 4/17: 'unknown' can now carry a
# retained known-finite figure (the P0-1 repair) -- _effective_available_
# memory_bytes must honor it as an ADDITIONAL cap, on top of (never
# instead of) the existing host/fallback fail-closed behavior. ---

def test_effective_available_memory_bytes_unknown_with_looser_known_finite_still_capped_by_fallback(monkeypatch):
    """Section 17's first example literally: host 8 GiB, known cgroup
    1 GiB retained alongside UNKNOWN, fallback 256 MiB -- the fallback is
    TIGHTER than the known figure here, so it still wins. The retained
    known figure narrows the result when it is the tightest candidate; it
    never widens it past what fallback/host alone would already give."""
    monkeypatch.setattr(ab, "_read_meminfo_available_bytes", lambda: 8 * 1024**3)  # 8 GiB
    monkeypatch.setattr(ab, "_discover_cgroup_v2_availability", lambda: ("unknown", 1024**3))  # 1 GiB retained
    assert ab._effective_available_memory_bytes() == ab._FALLBACK_AVAILABLE_MEMORY_BYTES  # 256 MiB


def test_effective_available_memory_bytes_unknown_with_tighter_known_finite_wins_over_fallback(monkeypatch):
    """Section 17's second example literally: host 8 GiB, known cgroup
    64 MiB retained alongside UNKNOWN -- TIGHTER than the 256 MiB
    fallback, so the known figure wins. This is the exact A2R14 repair:
    before A2F14, 'unknown' always carried None and this 64 MiB would
    have been silently discarded, leaving the much looser 256 MiB
    fallback in effect instead."""
    monkeypatch.setattr(ab, "_read_meminfo_available_bytes", lambda: 8 * 1024**3)  # 8 GiB
    tighter_known = 64 * 1024**2  # 64 MiB
    monkeypatch.setattr(ab, "_discover_cgroup_v2_availability", lambda: ("unknown", tighter_known))
    assert ab._effective_available_memory_bytes() == tighter_known


def test_effective_available_memory_bytes_unknown_with_known_finite_and_missing_host(monkeypatch):
    """Same retained-value contract with host discovery itself failing --
    the known cgroup figure must still be weighed against the fallback,
    never simply discarded because host is also unknown."""
    monkeypatch.setattr(ab, "_read_meminfo_available_bytes", lambda: None)
    tighter_known = 32 * 1024**2  # 32 MiB
    monkeypatch.setattr(ab, "_discover_cgroup_v2_availability", lambda: ("unknown", tighter_known))
    assert ab._effective_available_memory_bytes() == tighter_known


def test_effective_available_memory_bytes_unknown_with_known_finite_zero_is_still_honored(monkeypatch):
    """0 is a legitimate, tightest-possible known-finite figure (a cgroup
    already at or past its own limit) -- it must not be mistaken for
    'no known figure' (Section 3/4's None-vs-0 distinction) and must
    still win as the tightest candidate."""
    monkeypatch.setattr(ab, "_read_meminfo_available_bytes", lambda: 8 * 1024**3)  # 8 GiB
    monkeypatch.setattr(ab, "_discover_cgroup_v2_availability", lambda: ("unknown", 0))
    assert ab._effective_available_memory_bytes() == 0


# --- Section 8: monotonicity property -- adding uncertainty, or a
# tighter known constraint, must never WIDEN the effective allowance.
# No new dependency: a fixed, representative set of evidence
# combinations stands in for a property-style check. ---

_MONOTONICITY_BASE_EVIDENCE_SETS = [
    [("finite", 1024**3)],                                    # single finite
    [("finite", 1024**3), ("finite", 64 * 1024**2)],           # two finite, tighter present
    [("unlimited", None)],                                     # single unlimited
    [("unlimited", None), ("unlimited", None)],                 # two unlimited
    [("finite", 384 * 1024**2), ("unlimited", None)],           # finite + unlimited
    [("unknown", None)],                                        # single bare unknown
    [("unknown", 128 * 1024**2)],                               # unknown w/ retained finite
    [("finite", 512 * 1024**2), ("unknown", 128 * 1024**2)],    # finite + unknown-with-value
]


def _effective_bytes_for_evidence(monkeypatch, host_bytes, evidence_set):
    monkeypatch.setattr(ab, "_read_meminfo_available_bytes", lambda: host_bytes)
    monkeypatch.setattr(
        ab, "_discover_cgroup_v2_availability", lambda: ab._finalize_cgroup_states(evidence_set),
    )
    return ab._effective_available_memory_bytes()


@pytest.mark.parametrize("base", _MONOTONICITY_BASE_EVIDENCE_SETS)
def test_monotonicity_adding_unknown_never_widens_effective_bytes(monkeypatch, base):
    """AGENT-BACKUP-RESTORE-A2F14 Section 8: cap(S + UNKNOWN) <= cap(S)
    for every representative base evidence set S -- this would have
    caught A2R14 mechanically."""
    host = 8 * 1024**3  # 8 GiB, deliberately abundant
    before = _effective_bytes_for_evidence(monkeypatch, host, base)
    after = _effective_bytes_for_evidence(monkeypatch, host, base + [("unknown", None)])
    assert after <= before


@pytest.mark.parametrize("base", _MONOTONICITY_BASE_EVIDENCE_SETS)
@pytest.mark.parametrize("extra_finite", [0, 1024, 16 * 1024**2, 8 * 1024**3])
def test_monotonicity_adding_finite_never_widens_effective_bytes(monkeypatch, base, extra_finite):
    """AGENT-BACKUP-RESTORE-A2F14 Section 8: cap(S + FINITE(x)) <= cap(S)
    for every representative base evidence set S and a range of x from
    tighter-than-anything-present to looser-than-everything-present --
    a known finite constraint can only ever narrow the result, whatever
    its own value turns out to be relative to what was already there."""
    host = 8 * 1024**3  # 8 GiB, deliberately abundant
    before = _effective_bytes_for_evidence(monkeypatch, host, base)
    after = _effective_bytes_for_evidence(monkeypatch, host, base + [("finite", extra_finite)])
    assert after <= before


def test_working_set_ceiling_bytes_is_conservative_fraction_of_effective_available(monkeypatch):
    monkeypatch.setattr(ab, "_effective_available_memory_bytes", lambda: 1024 * 1024 * 1024)  # 1 GiB
    assert ab._working_set_ceiling_bytes() == int(1024 * 1024 * 1024 * ab._RESOURCE_BUDGET_HOST_MEMORY_FRACTION)


def test_resolve_configured_payload_budget_env_override_respected(monkeypatch):
    override = ab._MIN_AGGREGATE_UNCOMPRESSED_BYTES + 1000
    monkeypatch.setenv(ab._AGENT_BACKUP_MAX_PAYLOAD_BYTES_ENV, str(override))
    assert ab._resolve_configured_payload_budget() == override


@pytest.mark.parametrize("bad_value", ["not-an-int", "0", "-5", str(16 * 1024 ** 4)])
def test_resolve_configured_payload_budget_env_override_ignored_when_invalid(monkeypatch, bad_value):
    monkeypatch.setenv(ab._AGENT_BACKUP_MAX_PAYLOAD_BYTES_ENV, bad_value)
    assert ab._resolve_configured_payload_budget() == ab._DEFAULT_MAX_AGGREGATE_UNCOMPRESSED_BYTES


def test_build_fails_when_aggregate_payload_exceeds_configured_budget(tmp_path, monkeypatch):
    """Full-pipeline enforcement: a tiny configured budget -- well below
    even this fixture's two required databases -- must fail the whole
    build before any publication, leaving no destination and no leftover
    temp state."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    # The 16 MiB floor is itself comfortably above _make_env's real
    # ~90 KB of required databases, so the floor must be patched down
    # too, alongside the env override, to force a genuinely tiny
    # effective budget.
    monkeypatch.setattr(ab, "_MIN_AGGREGATE_UNCOMPRESSED_BYTES", 1000)
    monkeypatch.setenv(ab._AGENT_BACKUP_MAX_PAYLOAD_BYTES_ENV, "1000")
    with pytest.raises(ab.AgentBackupError, match="resource budget"):
        _build(data_dir, base_dir, dest)
    assert not os.path.exists(dest)
    leftovers = [f for f in os.listdir(tmp_path) if f.startswith(".agent_backup_")]
    assert not leftovers


def test_build_fails_partway_through_many_small_members_summing_over_budget(tmp_path, monkeypatch):
    """Section 33: many small members, none individually near the
    limit, whose SUM exceeds it -- must still fail, and must fail
    without ever completing collection (proving the charge happens
    per-member, not only as a post-hoc total)."""
    data_dir, base_dir = _make_env(tmp_path)
    # 30 avatar files of ~20 KB each (~600 KB total), none individually
    # large, on top of _make_env's own ~90 KB of required databases.
    for i in range(30):
        with open(os.path.join(base_dir, "assets", "avatars", f"extra_{i:03d}.png"), "wb") as f:
            f.write(os.urandom(20_000))
    dest = str(tmp_path / "backup.zip")
    # Budget well above the two required DBs alone (~90 KB) but well
    # below DBs + all 30 avatars (~690 KB) -- rejection must land
    # partway through avatar collection, not on the very first member.
    monkeypatch.setattr(ab, "_MIN_AGGREGATE_UNCOMPRESSED_BYTES", 1000)
    monkeypatch.setenv(ab._AGENT_BACKUP_MAX_PAYLOAD_BYTES_ENV, "200000")
    with pytest.raises(ab.AgentBackupError, match="resource budget"):
        _build(data_dir, base_dir, dest)
    assert not os.path.exists(dest)


def test_build_fails_on_single_oversized_member(tmp_path, monkeypatch):
    """Section 33: a single member alone exceeding the budget -- as
    distinct from many small members summing over it."""
    data_dir, base_dir = _make_env(tmp_path)
    with open(os.path.join(base_dir, "assets", "avatars", "huge.png"), "wb") as f:
        f.write(os.urandom(500_000))
    dest = str(tmp_path / "backup.zip")
    monkeypatch.setattr(ab, "_MIN_AGGREGATE_UNCOMPRESSED_BYTES", 1000)
    monkeypatch.setenv(ab._AGENT_BACKUP_MAX_PAYLOAD_BYTES_ENV, "200000")
    with pytest.raises(ab.AgentBackupError, match="resource budget"):
        _build(data_dir, base_dir, dest)
    assert not os.path.exists(dest)


def test_large_sqlite_snapshot_charged_against_same_budget(tmp_path, monkeypatch):
    """Section 23: no database exemption -- a large lumina.db must be
    able to trip the SAME aggregate budget SQLite snapshotting shares
    with every other collector."""
    data_dir, base_dir = _make_env(tmp_path)
    conn = sqlite3.connect(os.path.join(data_dir, "memory", "lumina.db"))
    conn.execute(
        "INSERT INTO memories (label, content, created_at) VALUES ('x', ?, 't')",
        ("A" * 500_000,)
    )
    conn.commit()
    conn.close()
    dest = str(tmp_path / "backup.zip")
    monkeypatch.setattr(ab, "_MIN_AGGREGATE_UNCOMPRESSED_BYTES", 1000)
    monkeypatch.setenv(ab._AGENT_BACKUP_MAX_PAYLOAD_BYTES_ENV, "200000")
    with pytest.raises(ab.AgentBackupError, match="resource budget"):
        _build(data_dir, base_dir, dest)
    assert not os.path.exists(dest)


def test_build_succeeds_comfortably_under_budget(tmp_path):
    """Anchor: an ordinary, real-sized build is nowhere near the
    default budget and is completely unaffected by any of the above."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    manifest = _build(data_dir, base_dir, dest)
    assert os.path.exists(dest)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is True, report["errors"]


def test_build_with_raised_configured_budget_does_not_fail_its_own_self_verification(tmp_path, monkeypatch):
    """Self-check on this pass's own resource-budget design: verify-time's
    decompression budget (_resolve_configured_payload_budget) must track
    the SAME operator-configured ceiling the builder itself is allowed to
    use -- never the bare _DEFAULT_MAX_AGGREGATE_UNCOMPRESSED_BYTES
    constant directly. An archive that legitimately grew beyond that bare
    default (via a raised LUMINA_AGENT_BACKUP_MAX_PAYLOAD_BYTES override)
    must not then fail the build's own immediate self-verification of the
    archive it just produced (_capture_and_verify_archive, called before
    publication) -- which would otherwise silently reintroduce two
    independently-tunable, divergence-prone resource numbers."""
    data_dir, base_dir = _make_env(tmp_path)
    with open(os.path.join(base_dir, "assets", "avatars", "big.png"), "wb") as f:
        f.write(os.urandom(80_000))  # bigger than this build's own manifest.json (~11.5 KB)
    dest = str(tmp_path / "backup.zip")
    monkeypatch.setattr(ab, "_MIN_AGGREGATE_UNCOMPRESSED_BYTES", 1000)
    # Bare default well BELOW this build's real aggregate (~132 KB) --
    # proves the operator override, not the bare default, is what's
    # actually in effect on both the build and verify sides.
    monkeypatch.setattr(ab, "_DEFAULT_MAX_AGGREGATE_UNCOMPRESSED_BYTES", 60_000)
    monkeypatch.setenv(ab._AGENT_BACKUP_MAX_PAYLOAD_BYTES_ENV, "150000")
    manifest = _build(data_dir, base_dir, dest)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is True, report["errors"]


# =======================================================================
# H2 (continued) -- verify-side decompression budget (Section 29-31)
# =======================================================================

def test_verify_rejects_single_member_declaring_oversized_uncompressed_size(tmp_path, monkeypatch):
    """Compression-bomb protection: rejects based purely on the
    TRUSTED, un-decompressed central-directory-declared size -- never
    actually decompresses the (genuinely tiny) real payload to notice a
    mismatch. Mirrors a real high-ratio bomb's central-directory claim
    without needing gigabytes of real test data."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    with open(dest, "rb") as f:
        original = bytearray(f.read())
    with zipfile.ZipFile(io.BytesIO(bytes(original))) as zf:
        info = min(zf.infolist(), key=lambda i: i.header_offset)
    huge = 3 * 1024 * 1024 * 1024  # 3 GiB -- not a ZIP64 sentinel value
    local_uncomp_offset = info.header_offset + 22
    struct.pack_into("<I", original, local_uncomp_offset, huge)
    cd_offset = _central_header_offset(bytes(original))
    cursor = cd_offset
    while True:
        assert original[cursor:cursor + 4] == b"PK\x01\x02"
        fname_len, extra_len, comment_len = struct.unpack_from("<HHH", original, cursor + 28)
        rel_offset = struct.unpack_from("<I", original, cursor + 42)[0]
        if rel_offset == info.header_offset:
            struct.pack_into("<I", original, cursor + 24, huge)  # central uncompressed size
            break
        cursor += 46 + fname_len + extra_len + comment_len
    monkeypatch.setattr(ab, "_DEFAULT_MAX_AGGREGATE_UNCOMPRESSED_BYTES", 1024 * 1024 * 1024)  # 1 GiB
    report = ab.verify_agent_backup(io.BytesIO(bytes(original)))
    assert report["valid"] is False
    assert any("decompression budget" in e for e in report["errors"])
    assert any("single-member" in e for e in report["errors"])


def test_verify_rejects_aggregate_declared_size_over_budget_with_no_single_member_over(tmp_path, monkeypatch):
    """Section 31: absolute per-member AND aggregate limits, not a
    compression-ratio heuristic -- many legitimately-sized members whose
    SUM exceeds a (patched-down) budget must be rejected even though
    none individually does."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)  # real archive, ~52 KB aggregate, largest single member ~11.5 KB (manifest.json)
    monkeypatch.setattr(ab, "_DEFAULT_MAX_AGGREGATE_UNCOMPRESSED_BYTES", 30_000)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is False
    assert any("aggregate" in e and "decompression budget" in e for e in report["errors"])
    assert not any("single-member" in e for e in report["errors"])


def test_verify_accepts_real_archive_comfortably_under_decompression_budget(tmp_path):
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is True, report["errors"]


def test_build_backup_receipt_also_honors_decompression_budget(tmp_path, monkeypatch):
    """Section 29 names build_backup_receipt(existing_archive)
    explicitly, alongside verify_agent_backup -- both share
    _verify_agent_backup_inner, so one gate protects both."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    monkeypatch.setattr(ab, "_DEFAULT_MAX_AGGREGATE_UNCOMPRESSED_BYTES", 30_000)
    receipt = ab.build_backup_receipt(dest)
    assert receipt["verification"] == "FAIL"
    assert any("decompression budget" in e for e in receipt["errors"])


# =======================================================================
# AGENT-BACKUP-RESTORE-A2F11 (P0, Section 11-14) -- verification's own
# decompression budget now derives from the SAME host-safe working-set
# contract the build side uses, never the raw configured ceiling alone.
# =======================================================================

def test_determine_verification_payload_budget_derives_from_working_set_ceiling(monkeypatch):
    monkeypatch.setattr(ab, "_effective_available_memory_bytes", lambda: 1024 * 1024 * 1024)  # 1 GiB
    monkeypatch.delenv(ab._AGENT_BACKUP_MAX_PAYLOAD_BYTES_ENV, raising=False)
    budget = ab._determine_verification_payload_budget()
    ceiling = int(1024 * 1024 * 1024 * ab._RESOURCE_BUDGET_HOST_MEMORY_FRACTION)
    expected = (ceiling - ab._RESOURCE_BUDGET_FIXED_OVERHEAD_BYTES) // ab._VERIFICATION_AMPLIFICATION_FACTOR
    assert budget == expected
    assert budget < ab._DEFAULT_MAX_AGGREGATE_UNCOMPRESSED_BYTES


def test_determine_verification_payload_budget_zero_not_negative_when_insufficient(monkeypatch):
    monkeypatch.setattr(ab, "_effective_available_memory_bytes", lambda: 1024)  # effectively nothing
    assert ab._determine_verification_payload_budget() == 0
    assert ab._determine_verification_payload_budget(resident_bytes_hint=10 ** 9) == 0


def test_determine_verification_payload_budget_charges_resident_bytes_hint(monkeypatch):
    """AGENT-BACKUP-RESTORE-A2F11 (Section 3/10): bytes a caller already
    knows are resident for reasons OTHER than the archive itself (e.g. a
    build's own already-collected new-member payload, still resident
    while that build verifies its prior destination) shrink the
    remaining decompression allowance."""
    monkeypatch.setattr(ab, "_effective_available_memory_bytes", lambda: 1024 * 1024 * 1024)
    monkeypatch.delenv(ab._AGENT_BACKUP_MAX_PAYLOAD_BYTES_ENV, raising=False)
    no_hint = ab._determine_verification_payload_budget(resident_bytes_hint=0)
    with_hint = ab._determine_verification_payload_budget(resident_bytes_hint=100 * 1024 * 1024)
    assert with_hint < no_hint


def test_determine_verification_payload_budget_charges_per_member_structural_overhead(monkeypatch):
    """Section 18: a very large member COUNT is never free merely
    because aggregate declared payload is small."""
    monkeypatch.setattr(ab, "_effective_available_memory_bytes", lambda: 1024 * 1024 * 1024)
    monkeypatch.delenv(ab._AGENT_BACKUP_MAX_PAYLOAD_BYTES_ENV, raising=False)
    few_members = ab._determine_verification_payload_budget(member_count=1)
    many_members = ab._determine_verification_payload_budget(member_count=100_000)
    assert many_members < few_members


def test_determine_verification_payload_budget_never_exceeds_configured(monkeypatch):
    monkeypatch.setattr(ab, "_effective_available_memory_bytes", lambda: 1024 ** 4)  # 1 TiB
    monkeypatch.delenv(ab._AGENT_BACKUP_MAX_PAYLOAD_BYTES_ENV, raising=False)
    assert ab._determine_verification_payload_budget() == ab._DEFAULT_MAX_AGGREGATE_UNCOMPRESSED_BYTES


def test_verify_agent_backup_resident_bytes_hint_shrinks_effective_budget(tmp_path, monkeypatch):
    """Direct proof that verify_agent_backup's own optional
    resident_bytes_hint parameter (Section 3/10/11) actually reaches the
    decompression-budget derivation, on a real (small) archive -- a
    large-enough hint must be able to tip an otherwise-passing
    verification into a decompression-budget rejection."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)
    monkeypatch.setattr(ab, "_effective_available_memory_bytes", lambda: 256 * 1024 * 1024)  # 256 MiB
    report_no_hint = ab.verify_agent_backup(dest)
    assert report_no_hint["valid"] is True, report_no_hint["errors"]
    report_with_hint = ab.verify_agent_backup(dest, resident_bytes_hint=200 * 1024 * 1024)
    assert report_with_hint["valid"] is False
    assert any("decompression budget" in e for e in report_with_hint["errors"])


def test_replacement_verification_refuses_prior_large_uncompressed_content_on_low_memory_host(
        tmp_path, monkeypatch):
    """AGENT-BACKUP-RESTORE-A2F11 Section 14, the literal AGENT-BACKUP-
    RESTORE-A2R11 reproduction: a simulated 256 MiB effective-available
    host, a PRIOR destination archive whose compressed bytes are tiny
    but whose declared uncompressed content is large (highly
    compressible), and a default (2 GiB) configured ceiling. Before this
    pass, existing-destination verification used the raw configured
    ceiling directly (_resolve_configured_payload_budget()), so this
    exact tiny-compressed/large-uncompressed prior sailed through
    unbounded by host safety -- replacement would have proceeded to
    decompress the whole thing regardless of how little memory this host
    actually has. After this pass, verification derives its own
    decompression allowance from the SAME host-safe working-set contract
    the build side uses, and must refuse BEFORE decompressing the prior
    archive's oversized content -- leaving the existing destination
    completely untouched. The replacement build's own NEW payload is
    kept small (the oversized member is removed from base_dir before the
    second build) specifically to isolate the PRIOR-destination
    verification path from the separate, already-covered build-payload
    budget path."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    big_avatar = os.path.join(base_dir, "assets", "avatars", "big_compressible.bin")
    with open(big_avatar, "wb") as f:
        f.write(b"A" * 64_000_000)  # 64 MB, ~1000:1 DEFLATE compression -> tiny on disk
    _build(data_dir, base_dir, dest)  # first publish -- real host memory, no simulation
    assert os.path.getsize(dest) < 1_000_000, (
        "fixture assumption: the prior archive's own compressed size must stay tiny"
    )
    with open(dest, "rb") as f:
        prior_bytes = f.read()

    os.remove(big_avatar)  # second build's own NEW payload is small again
    monkeypatch.setattr(ab, "_effective_available_memory_bytes", lambda: 256 * 1024 * 1024)  # 256 MiB
    with pytest.raises(ab.AgentBackupError, match="decompression budget"):
        _build(data_dir, base_dir, dest)
    with open(dest, "rb") as f:
        assert f.read() == prior_bytes  # existing destination completely untouched


# =======================================================================
# Section 19 (directory-fsync attack matrix) -- replacement-backup case
# Section 42 -- builder corpus, strict + receipt verification
# =======================================================================

def test_replacement_backup_reports_directory_fsync_failure_same_as_first_backup(tmp_path, monkeypatch):
    """Section 19: 'new backup publication' is already covered by
    test_build_agent_backup_manifest_warns_when_directory_fsync_fails and
    friends -- this is the 'replacement backup' case specifically: a
    SECOND, ordinary (non-sabotaged) build overwriting an already-good
    existing destination must report directory-fsync truth identically,
    not just on a first-ever publish."""
    data_dir, base_dir = _make_env(tmp_path)
    dest = str(tmp_path / "backup.zip")
    _build(data_dir, base_dir, dest)  # first publish -- ordinary, no fsync failure
    with open(dest, "rb") as f:
        first_bytes = f.read()

    monkeypatch.setattr(ab, "_best_effort_fsync_dir", lambda _dir: False)
    with pytest.warns(ab.AgentBackupDurabilityWarning, match="directory.*fsync"):
        manifest = _build(data_dir, base_dir, dest)  # replacement publish
    # AGENT-BACKUP-RESTORE-A2F11 (P1): the replacement's returned manifest
    # is exactly what was archived, same as the first-publish case --
    # no post-publication warning ever lands inside the manifest dict.
    assert not any("fsync" in w for w in manifest["warnings"])
    with zipfile.ZipFile(dest) as zf:
        archived_manifest = json.loads(zf.read("manifest.json"))
    assert manifest == archived_manifest
    # Content itself published correctly regardless of the directory-
    # fsync outcome -- a real, valid, strictly-verifying replacement,
    # not merely "some bytes landed."
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is True, report["errors"]
    with open(dest, "rb") as f:
        assert f.read() != first_bytes  # a genuine second build, not a no-op leaving the first archive in place


def test_builder_corpus_every_variant_strict_and_receipt_verifies(tmp_path):
    """Section 42: a builder corpus spanning ASCII/Unicode filenames, a
    zero-byte payload, binary content, both SQLite databases, and many
    members comfortably below the v1 entry ceiling -- every successful
    builder result must pass BOTH verify_agent_backup and
    build_backup_receipt, not merely one or the other."""
    data_dir, base_dir = _make_env(tmp_path)
    with open(os.path.join(base_dir, "personas", "lümina☃.json"), "w", encoding="utf-8") as f:
        json.dump({"name": "snowman"}, f)
    open(os.path.join(base_dir, "assets", "voices", "empty.wav"), "wb").close()
    with open(os.path.join(base_dir, "assets", "avatars", "binary.bin"), "wb") as f:
        f.write(bytes(range(256)) * 10)
    with open(os.path.join(base_dir, "skills", "incompressible.md"), "wb") as f:
        f.write(os.urandom(4096))
    with open(os.path.join(base_dir, "skills", "compressible.md"), "w") as f:
        f.write("A" * 4096)
    for i in range(50):
        with open(os.path.join(base_dir, "tool_profiles", f"profile_{i:03d}.json"), "w") as f:
            json.dump({"name": f"p{i}"}, f)
    dest = str(tmp_path / "backup.zip")
    manifest, receipt = ab.build_agent_backup_with_receipt(
        data_dir, base_dir, dest, identity_trailers_path=NO_IDENTITY_TRAILERS,
        credentials_path=NO_CREDENTIALS)
    assert receipt["verification"] == "PASS"
    assert len(manifest["members"]) > 60
    report = ab.verify_agent_backup(dest)
    assert report["valid"] is True, report["errors"]
    receipt2 = ab.build_backup_receipt(dest)
    assert receipt2["verification"] == "PASS"
