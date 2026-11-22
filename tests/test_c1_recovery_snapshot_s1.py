"""C1-S1 -- the consistent C1 safety-state snapshot (core/suspension.py
SuspensionStore.recovery_snapshot, core/suspension_backup.capture_recovery_state,
validate_recovery_state).

Design: C1-SNAP-CONTRACT S-1..S-6 with Tech's amendments C1-02..C1-13
(TECH-REVIEW-C1.md) and owner decisions O-1 and O-6. Every store here is a
disposable synthetic one under pytest's tmp_path. Nothing is wired into an
export yet (C1-S3), and no test touches a real data root, relay or seat.
"""
import ast
import hashlib
import json
import os
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import textwrap
import threading
import time
import types
from pathlib import Path

import pytest

from core import operator_ingress as oi
from core import suspension as S
from core import suspension_backup as B

REPO = Path(__file__).resolve().parent.parent
SIDECARS = (S.STORE_NAME + "-wal", S.STORE_NAME + "-shm", S.STORE_NAME + "-journal")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def local(action="test"):
    return oi.local_suspension_control(action)


def _root(tmp_path, name="data"):
    root = tmp_path / name
    (root / "memory").mkdir(parents=True, mode=0o700)
    return root


def _store(root):
    return S.initialize(root, ingress=local("init"))


def _trip(store, scope=S.OWNER_SCOPE, evidence=None):
    token = store.capture(scope)
    return store.trip(token, trigger=S.Trigger.BREAKER_TRIPPED,
                      evidence=evidence if evidence is not None else {"trip_kind": "stale_streak", "executed_calls": 20})


def _resume(store, scope=S.OWNER_SCOPE, ack=False):
    view = next(v for v in store.operator_view() if v.scope == scope)
    exp = view.expectation()
    return store.resume(S.desktop_recovery_event(local("resume"), exp, ack_unknown=ack), exp)


def _raw(root):
    return sqlite3.connect(root / "memory" / S.STORE_NAME, isolation_level=None)


def _inventory(root):
    """Every non-sidecar entry under the data root: content hash and inode."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root)
            if name in SIDECARS:
                continue
            st = os.lstat(path)
            digest = None
            if stat.S_ISREG(st.st_mode):
                digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            out[rel] = (stat.S_IFMT(st.st_mode), st.st_ino, digest)
    return out


def _populated(tmp_path, name="data"):
    """A store with an owner trip+resume, an active owner trip with an
    in-flight lease, a Discord and a Wire scope, and one channel trip."""
    root = _root(tmp_path, name)
    store = _store(root)
    rt = store.register_runtime()
    _trip(store)
    _resume(store)
    token = store.capture(S.OWNER_SCOPE)
    store.admit(token, kind=S.LeaseKind.TOOL, name="read_file", runtime_id=rt)
    store.trip(token, trigger=S.Trigger.BREAKER_TRIPPED, evidence={"trip_kind": "stale_streak", "executed_calls": 3})
    store.capture("channel:discord:987654321098765432")
    _trip(store, "channel:wire:aivengers-general", {"trip_kind": "write_stale"})
    store.capture("channel:eval:task_17")
    return root, store, rt


def _refused(code, call, *args, **kwargs):
    with pytest.raises(S.RecoverySnapshotRefused) as exc:
        call(*args, **kwargs)
    assert exc.value.code == code, exc.value
    return exc.value


# ---------------------------------------------------------------------------
# A. Normal capture
# ---------------------------------------------------------------------------

def test_a_fresh_store_gives_a_valid_minimal_record(tmp_path):
    root = _root(tmp_path)
    _store(root).close()
    state = B.capture_recovery_state(root)
    record = state.record()
    S.validate_recovery_state(record)
    assert hashlib.sha256(state.payload).hexdigest() == state.sha256
    assert record["schema"] == "lumina.c1_recovery_state" and record["version"] == 1
    assert record["label"] == "DERIVED-SANITIZED, not original evidence"
    assert record["owner_scope"] == {"execution_epoch": 0, "state_revision": 0, "suspended": False}
    assert record["incidents"] == [] and record["in_flight_leases"] == [] and record["runtimes"] == []
    assert record["audit_carried"]["total"] == 1 and record["audit"][0]["action"] == "initialize"
    assert record["evidence_index"] == {"entries": [], "files": 0, "dirs": 0, "bytes": 0,
                                        "ignored_transient": 0}


def test_counts_match_the_lists_and_the_state_is_described(tmp_path):
    root, store, _rt = _populated(tmp_path)
    try:
        record = store.recovery_snapshot().record()
    finally:
        store.close()
    S.validate_recovery_state(record)
    assert record["owner_scope"]["suspended"] is True and record["owner_scope"]["execution_epoch"] == 2
    assert record["channel_summary"] == {
        "discord": {"scopes": 1, "suspended": 0, "in_flight": 0},
        "wire": {"scopes": 1, "suspended": 1, "in_flight": 0},
        "other": {"scopes": 1, "suspended": 0, "in_flight": 0}}
    assert [i["status"] for i in record["incidents"]] == ["resumed", "active", "active"]
    assert [i["scope_class"] for i in record["incidents"]] == ["owner", "owner", "wire"]
    assert record["incidents"][0]["resume_outcome"]["result"] == "resumed"
    assert record["incidents"][1]["leases_at_trip"] == {"count": 1, "by_kind": {"tool": 1}}
    assert record["in_flight_leases"] == [{"scope_class": "owner", "execution_epoch": 1, "kind": "tool",
                                           "admitted_at": record["in_flight_leases"][0]["admitted_at"]}]
    assert record["lease_state_counts"] == {"in_flight": 1, "done": 0, "orphan_acknowledged": 0}
    assert record["row_counts"]["events"] == 1 and record["row_counts"]["scopes"] == 4
    assert len(record["audit"]) == record["audit_carried"]["carried"] == record["row_counts"]["audit"]
    assert record["incidents"][1]["evidence"] == {"trip_kind": "stale_streak", "executed_calls": 3}
    refs = {i["ref"] for i in record["incidents"]}
    assert {a["incident_ref"] for a in record["audit"] if a["incident_ref"]} <= refs


def test_raw_channel_ids_are_absent_and_counted_by_type(tmp_path):
    root, store, _rt = _populated(tmp_path)
    store.close()
    state = B.capture_recovery_state(root)
    text = state.payload.decode("ascii")
    for raw in ("987654321098765432", "aivengers-general", "task_17", "channel:"):
        assert raw not in text
    red = state.record()["redactions"]
    assert red["scopes.scope"] == 3 and red["incidents.scope"] == 1


def test_no_source_token_selector_or_id_reaches_the_record(tmp_path):
    root, store, _rt = _populated(tmp_path)
    store.close()
    conn = _raw(root)
    secrets_ = set()
    for query in ("SELECT suspension_id, selector FROM incidents", "SELECT lease_id, runtime_id FROM leases",
                  "SELECT runtime_id, boot_id, data_root FROM runtimes", "SELECT event_id FROM events"):
        for row in conn.execute(query):
            secrets_.update(v for v in row if v)
    conn.close()
    state = B.capture_recovery_state(root)
    text = state.payload.decode("ascii")
    assert secrets_ and not [s for s in secrets_ if s in text]
    identifier_keys = {"selector", "suspension_id", "lease_id", "runtime_id", "event_id", "hostname", "pid",
                       "boot_id", "data_root", "incarnation", "soul_id"}
    found = []

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in identifier_keys:
                    found.append((key, item))  # only a free-text field keeps the key, never the value
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(state.record())
    assert found and all(item == S.REDACTED for _key, item in found)


def test_provenance_is_claimed_and_the_digest_is_of_an_ephemeral_capture(tmp_path):
    root = _root(tmp_path)
    store = _store(root)
    try:
        prov = store.recovery_snapshot().record()["provenance"]
        assert prov["claimed_source_soul_id"] == store.soul_id
        assert prov["claimed_source_incarnation"] == store.incarnation
    finally:
        store.close()
    assert prov["capture_method"] == "sqlite_backup_read_only_in_memory"
    assert len(prov["digest_of_ephemeral_capture"]) == 64
    assert prov["sqlite_version"] == sqlite3.sqlite_version
    assert set(prov) == {"claimed_source_soul_id", "claimed_source_incarnation", "claimed_source_created_at",
                         "schema_version", "min_enforcement_version", "unknown_scope_policy", "claimed_captured_at", "assurance",
                         "capture_method", "sqlite_version", "digest_of_ephemeral_capture"}


def test_record_returns_a_fresh_object_each_time(tmp_path):
    root = _root(tmp_path)
    _store(root).close()
    state = B.capture_recovery_state(root)
    state.record()["owner_scope"]["suspended"] = True
    assert state.record()["owner_scope"]["suspended"] is False
    assert "payload" not in repr(state)


# ---------------------------------------------------------------------------
# B. Consistency under concurrent trips; trips are never blocked (W-1)
# ---------------------------------------------------------------------------

def test_a_trip_committed_after_the_pin_is_not_in_the_record_and_is_not_delayed(tmp_path, monkeypatch):
    root = _root(tmp_path)
    store = _store(root)
    other = S.open_store(root)
    real, seen = S._snapshot_copy, {}

    def trip_first(source):
        started = time.monotonic()
        seen["outcome"] = _trip(other)
        seen["elapsed"] = time.monotonic() - started
        return real(source)

    monkeypatch.setattr(S, "_snapshot_copy", trip_first)
    try:
        record = store.recovery_snapshot().record()
        assert seen["outcome"].result is S.TripResult.TRIPPED
        assert seen["elapsed"] < 1.0
        assert record["owner_scope"]["suspended"] is False and record["incidents"] == []
        assert store.scope_state(S.OWNER_SCOPE).suspended is True
    finally:
        other.close()
        store.close()


def test_a_trip_through_the_same_handle_is_not_held_up_by_its_snapshot(tmp_path, monkeypatch):
    root = _root(tmp_path)
    store = _store(root)
    real, seen = S._snapshot_copy, {}

    def trip_in_another_thread(source):
        worker = threading.Thread(target=lambda: seen.setdefault("outcome", _trip(store)))
        worker.start()
        worker.join(timeout=3)
        seen["finished"] = not worker.is_alive()
        return real(source)

    monkeypatch.setattr(S, "_snapshot_copy", trip_in_another_thread)
    try:
        store.recovery_snapshot()
        assert seen["finished"] and seen["outcome"].result is S.TripResult.TRIPPED
    finally:
        store.close()


def _assert_consistent(record):
    owner_incidents = [i for i in record["incidents"] if i["scope_class"] == "owner"]
    trips = [a for a in record["audit"] if a["action"] == "trip"]
    resumes = [a for a in record["audit"] if a["action"] == "resume"]
    assert record["owner_scope"]["execution_epoch"] == len(owner_incidents) == len(trips)
    assert record["owner_scope"]["suspended"] == any(i["status"] == "active" for i in owner_incidents)
    assert len(resumes) == sum(1 for i in owner_incidents if i["status"] == "resumed")
    assert record["owner_scope"]["state_revision"] == len(trips) + len(resumes)


def test_snapshots_during_continuous_trip_and_resume_are_never_torn(tmp_path):
    root = _root(tmp_path)
    _store(root).close()
    stop, errors, outcomes = threading.Event(), [], []

    def churn():
        store = S.open_store(root)
        try:
            while not stop.is_set():
                outcomes.append(_trip(store).result)
                outcomes.append(_resume(store).result)
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(repr(exc))
        finally:
            store.close()

    worker = threading.Thread(target=churn)
    worker.start()
    epochs = set()
    try:
        deadline = time.monotonic() + 30
        while (len(epochs) < 3 or len(outcomes) < 20) and time.monotonic() < deadline:
            record = B.capture_recovery_state(root).record()
            _assert_consistent(record)
            epochs.add(record["owner_scope"]["execution_epoch"])
    finally:
        stop.set()
        worker.join(timeout=10)
    assert not errors and len(epochs) >= 3
    assert S.TripResult.GLOBAL_UNCONFIRMED not in outcomes
    assert set(outcomes) <= {S.TripResult.TRIPPED, S.ResumeResult.RESUMED}


def _latencies(store, n):
    out = []
    for _ in range(n):
        started = time.perf_counter()
        outcome = _trip(store)
        out.append(time.perf_counter() - started)
        assert outcome.result is S.TripResult.TRIPPED
        _resume(store)
    return sorted(out)


def test_trip_latency_stays_bounded_while_snapshots_run(tmp_path):
    root = _root(tmp_path)
    store = _store(root)
    conn = _raw(root)
    with conn:  # one transaction: a store with real weight to copy
        conn.execute("BEGIN")
        conn.executemany("INSERT INTO audit (at, action, scope, suspension_id, detail) VALUES (?, 'initialize', "
                         "NULL, NULL, '{}')", [(1.0,)] * 20000)
    conn.close()
    baseline = _latencies(store, 30)
    stop, snaps, errors = threading.Event(), [], []

    def snapshot_loop():
        try:
            while not stop.is_set():
                snaps.append(B.capture_recovery_state(root))
        except Exception as exc:  # pragma: no cover
            errors.append(repr(exc))

    worker = threading.Thread(target=snapshot_loop)
    worker.start()
    try:
        during = _latencies(store, 30)
    finally:
        stop.set()
        worker.join(timeout=60)
        store.close()
    assert not errors and len(snaps) >= 1
    p95 = lambda xs: xs[int(len(xs) * 0.95) - 1]
    assert during[-1] < 1.0, during[-1]
    assert p95(during) <= max(10 * p95(baseline), 0.25), (p95(baseline), p95(during))


def test_the_snapshot_connection_is_read_only_and_issues_no_write_statements(tmp_path, monkeypatch):
    root = _root(tmp_path)
    store = _store(root)
    statements, real = [], S._snapshot_connect

    def traced(db_path):
        conn = real(db_path)
        conn.set_trace_callback(statements.append)
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        return conn

    monkeypatch.setattr(S, "_snapshot_connect", traced)
    try:
        store.recovery_snapshot()
    finally:
        store.close()
    joined = " | ".join(statements).upper()
    assert statements and "BEGIN" in joined
    for forbidden in ("IMMEDIATE", "EXCLUSIVE", "CHECKPOINT", "JOURNAL_MODE", "INSERT", "UPDATE", "DELETE",
                      "CREATE", "DROP", "VACUUM", "REINDEX", "ANALYZE"):
        assert forbidden not in joined, forbidden
    conn = S._snapshot_connect(str(root / "memory" / S.STORE_NAME))
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO meta VALUES ('x', 'y')")
    finally:
        conn.close()


def test_the_exporter_handle_is_read_only_too(tmp_path):
    root = _root(tmp_path)
    _store(root).close()
    handle = S._open_store(root, now=None, read_only=True)
    try:
        assert handle._conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            handle._conn.execute("INSERT INTO meta VALUES ('x', 'y')")
    finally:
        handle.close()


# ---------------------------------------------------------------------------
# C. Footprint (C1-06): store, pin and evidence byte- and inode-identical
# ---------------------------------------------------------------------------

def _crash_residue(root):
    """Committed frames left in the WAL, never checkpointed: the case where
    a careless close would rewrite the main store file."""
    store = S.open_store(root)
    store._conn.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
    _trip(store, "channel:discord:5550001")
    store.close()
    assert os.path.getsize(root / "memory" / (S.STORE_NAME + "-wal")) > 0


@pytest.mark.parametrize("shape", ["clean", "crash_residue", "live_writer"])
@pytest.mark.parametrize("entry", ["exporter", "handle"])
def test_the_data_root_is_unchanged_apart_from_sqlite_sidecars(tmp_path, shape, entry):
    root, store, _rt = _populated(tmp_path)
    store.close()
    evidence = root / "memory" / S.EVIDENCE_DIRNAME / "20261121T000000Z-0a1b2c3d"
    evidence.mkdir(parents=True, mode=0o700)
    (evidence / "note.json").write_bytes(b'{"kept": true}')
    for name in SIDECARS:  # start from a fully checkpointed store
        assert not (root / "memory" / name).exists()
    if shape == "crash_residue":
        _crash_residue(root)
    live = S.open_store(root) if shape == "live_writer" else None
    handle = S.open_store(root) if entry == "handle" else None
    try:
        before = _inventory(root)
        state = (handle.recovery_snapshot() if handle else B.capture_recovery_state(root))
        after = _inventory(root)
    finally:
        for h in (handle, live):
            if h is not None:
                h.close()
    assert after == before
    S.validate_recovery_state(state.record())


def test_the_exporter_never_checkpoints_a_crash_residue_store(tmp_path):
    root = _root(tmp_path)
    _store(root).close()
    _crash_residue(root)
    main = root / "memory" / S.STORE_NAME
    before = (main.read_bytes(), main.stat().st_ino)
    record = B.capture_recovery_state(root).record()
    assert (main.read_bytes(), main.stat().st_ino) == before
    assert record["channel_summary"]["discord"]["suspended"] == 1  # the WAL-only trip was captured
    assert (root / "memory" / (S.STORE_NAME + "-wal")).stat().st_size > 0


def test_the_capture_creates_no_file_anywhere(tmp_path, monkeypatch):
    root, store, _rt = _populated(tmp_path)
    store.close()
    import builtins
    import tempfile
    real_open, real_os_open = builtins.open, os.open
    writes = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

    def guarded_open(file, mode="r", *args, **kwargs):
        assert not set(mode) & set("wax+"), (file, mode)
        return real_open(file, mode, *args, **kwargs)

    def guarded_os_open(path, flags, *args, **kwargs):
        assert not flags & writes, path
        return real_os_open(path, flags, *args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("the capture must not create anything")

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(os, "open", guarded_os_open)
    for name in ("mkdir", "makedirs", "replace", "rename", "link", "symlink", "unlink", "remove", "chmod"):
        monkeypatch.setattr(os, name, forbidden)
    for name in ("mkstemp", "mkdtemp", "NamedTemporaryFile", "TemporaryDirectory"):
        monkeypatch.setattr(tempfile, name, forbidden)
    B.capture_recovery_state(root)


# ---------------------------------------------------------------------------
# D. Interruption: nothing is returned, nothing is left, nothing is faulted
# ---------------------------------------------------------------------------

STEPS = ["_snapshot_connect", "_snapshot_copy", "_derive_recovery_body", "_evidence_index",
         "_seal_recovery_state"]


@pytest.mark.parametrize("step", STEPS)
def test_an_exception_at_any_step_returns_nothing_and_changes_nothing(tmp_path, monkeypatch, step):
    root, store, _rt = _populated(tmp_path)
    incarnation = store.incarnation
    before = _inventory(root)

    def boom(*args, **kwargs):
        raise RuntimeError(f"interrupted at {step}")

    monkeypatch.setattr(S, step, boom)
    try:
        with pytest.raises(RuntimeError):
            store.recovery_snapshot()
        with pytest.raises(RuntimeError):
            B.capture_recovery_state(root)
        assert store.fault is None
        assert _trip(store, "channel:discord:1").result is S.TripResult.TRIPPED
    finally:
        store.close()
    monkeypatch.undo()
    after = _inventory(root)
    assert {k: v for k, v in after.items() if k != os.path.join("memory", S.STORE_NAME)} == \
        {k: v for k, v in before.items() if k != os.path.join("memory", S.STORE_NAME)}
    assert S.probe(root).status is S.StoreStatus.HEALTHY
    with S.open_store(root) as again:
        assert again.incarnation == incarnation


@pytest.mark.parametrize("step", STEPS)
def test_a_kill_at_any_step_leaves_the_root_unchanged(tmp_path, step):
    root, store, _rt = _populated(tmp_path)
    incarnation = store.incarnation
    store.close()
    before = _inventory(root)
    script = textwrap.dedent(f"""
        import os, signal, sys
        sys.path.insert(0, {str(REPO)!r})
        os.environ["LUMINA_TESTING"] = "1"
        from core import suspension as S, suspension_backup as B
        def die(*a, **k):
            os.kill(os.getpid(), signal.SIGKILL)
        setattr(S, {step!r}, die)
        B.capture_recovery_state({str(root)!r})
        print("survived")
    """)
    env = dict(os.environ, LUMINA_TESTING="1")
    result = subprocess.run([sys.executable, "-c", script], env=env, cwd=str(tmp_path),
                            capture_output=True, timeout=60)
    assert result.returncode == -signal.SIGKILL, result
    assert _inventory(root) == before
    assert S.probe(root).status is S.StoreStatus.HEALTHY
    with S.open_store(root) as again:
        assert again.incarnation == incarnation
    assert sorted(os.listdir(tmp_path)) == ["data"]


def test_a_failed_snapshot_never_faults_the_live_handle(tmp_path, monkeypatch):
    root = _root(tmp_path)
    store = _store(root)

    def broken(source):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(S, "_snapshot_copy", broken)
    try:
        _refused("SNAPSHOT_FAILED", store.recovery_snapshot)
        assert store.fault is None
        assert _trip(store).result is S.TripResult.TRIPPED
    finally:
        store.close()


# ---------------------------------------------------------------------------
# E. Refuse, never guess
# ---------------------------------------------------------------------------

def test_a_faulted_or_closed_handle_refuses(tmp_path):
    root = _root(tmp_path)
    store = _store(root)
    store._set_fault(S.StoreFault(S.Fault.CORRUPT, "synthetic"))
    err = _refused("STORE_FAULT", store.recovery_snapshot)
    assert err.fault is S.Fault.CORRUPT
    store.close()
    fresh = S.open_store(root)
    fresh.close()
    _refused("STORE_CLOSED", fresh.recovery_snapshot)


def test_a_store_from_another_root_refuses_as_soul_mismatch(tmp_path):
    a, b = _root(tmp_path, "a"), _root(tmp_path, "b")
    _store(a).close()
    _store(b).close()
    os.replace(b / "memory" / S.STORE_NAME, a / "memory" / S.STORE_NAME)
    err = _refused("STORE_FAULT", B.capture_recovery_state, a)
    assert err.fault is S.Fault.SOUL_MISMATCH


def test_a_group_writable_memory_directory_refuses(tmp_path):
    root = _root(tmp_path)
    _store(root).close()
    (root / "memory").chmod(0o770)
    err = _refused("STORE_FAULT", B.capture_recovery_state, root)
    assert err.fault is S.Fault.INSECURE


def test_a_missing_store_refuses(tmp_path):
    root = _root(tmp_path)
    err = _refused("STORE_FAULT", B.capture_recovery_state, root)
    assert err.fault in (S.Fault.MISSING, S.Fault.IDENTITY_MISSING)


@pytest.mark.parametrize("sql", [
    "CREATE TABLE extra (x)",
    "ALTER TABLE incidents ADD COLUMN note TEXT",
    "CREATE INDEX extra_idx ON audit(action)",
    "CREATE VIEW extra_view AS SELECT 1",
    "CREATE TRIGGER extra_trg AFTER INSERT ON scopes BEGIN SELECT 1; END",
], ids=["table", "column", "index", "view", "trigger"])
def test_an_unknown_schema_object_refuses(tmp_path, sql):
    root = _root(tmp_path)
    store = _store(root)
    conn = _raw(root)
    conn.execute(sql)
    conn.close()
    try:
        _refused("SCHEMA_UNKNOWN", store.recovery_snapshot)
    finally:
        store.close()


@pytest.mark.parametrize("key, value, code", [
    ("future_key", "1", "SCHEMA_UNKNOWN"),
    ("schema_version", "2", "SCHEMA_UNKNOWN"),
    ("unknown_scope_policy", "maybe", "VALUE_UNKNOWN"),
    ("min_enforcement_version", "one", "VALUE_UNKNOWN"),
    ("created_at", "yesterday", "VALUE_UNKNOWN"),
])
def test_unknown_or_invalid_meta_refuses(tmp_path, key, value, code):
    root = _root(tmp_path)
    store = _store(root)
    conn = _raw(root)
    conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
    conn.close()
    try:
        if key == "min_enforcement_version":
            _refused("STORE_FAULT", store.recovery_snapshot)  # the identity walk rejects it first
        else:
            _refused(code, store.recovery_snapshot)
    finally:
        store.close()


def test_restore_lineage_meta_is_dropped_and_counted_never_carried(tmp_path):
    root = _root(tmp_path)
    _store(root).close()
    conn = _raw(root)
    conn.executemany("INSERT INTO meta VALUES (?, ?)", [("lineage_restore_id", "a" * 32),
                                                       ("lineage_archive_sha256", "b" * 64),
                                                       ("lineage_trace_sha256", "c" * 64)])
    conn.close()
    state = B.capture_recovery_state(root)
    text = state.payload.decode("ascii")
    assert "a" * 32 not in text and "b" * 64 not in text and "lineage" in text
    red = state.record()["redactions"]
    assert red["meta.lineage_restore_id"] == red["meta.lineage_archive_sha256"] == 1


@pytest.mark.parametrize("sql", [
    "UPDATE incidents SET trigger = 'evil'",
    "INSERT INTO audit (at, action, scope, suspension_id, detail) VALUES (1.0, 'grant_everything', NULL, NULL, '{}')",
    "UPDATE leases SET kind = 'shell'",
    "UPDATE leases SET scope = 'channel:x:/home/alice'",
    "INSERT INTO deferred VALUES ('k', 'r', 'i', 'owner', 1, 'pending', 1.0)",
    "UPDATE incidents SET evidence = 'not json'",
    "UPDATE incidents SET evidence = '[1, 2]'",
    "UPDATE incidents SET leases_at_trip = '[{\"kind\": \"tool\"}]'",
    "UPDATE incidents SET created_at = -5",
    "UPDATE scopes SET execution_epoch = 9007199254740993 WHERE scope = 'owner'",
    "INSERT INTO audit (at, action, scope, suspension_id, detail) VALUES (1.0, 'trip', 'owner', 'ffff', '{}')",
    "UPDATE incidents SET status = 'resumed', resume_outcome = NULL",
    "UPDATE scopes SET suspended = 0, active_incident = NULL WHERE scope = 'owner'",
], ids=["trigger", "audit_action", "lease_kind", "lease_scope", "deferred", "evidence_json",
        "evidence_list", "leases_at_trip_shape", "negative_time", "huge_epoch", "audit_dangling_incident",
        "torn_suspension", "torn_scope"])
def test_values_outside_the_closed_world_refuse(tmp_path, sql):
    root = _root(tmp_path)
    store = _store(root)
    rt = store.register_runtime()
    token = store.capture(S.OWNER_SCOPE)
    store.admit(token, kind=S.LeaseKind.TOOL, name="read_file", runtime_id=rt)
    store.trip(token, trigger=S.Trigger.BREAKER_TRIPPED, evidence={"executed_calls": 1})
    conn = _raw(root)
    conn.execute(sql)
    conn.close()
    try:
        _refused("VALUE_UNKNOWN", store.recovery_snapshot)
    finally:
        store.close()


def test_over_cap_refuses(tmp_path, monkeypatch):
    root, store, _rt = _populated(tmp_path)
    try:
        monkeypatch.setattr(S, "INCIDENT_CARRY_LIMIT", 1)
        _refused("OVER_CAP", store.recovery_snapshot)  # two active incidents
        monkeypatch.setattr(S, "INCIDENT_CARRY_LIMIT", 10_000)
        monkeypatch.setattr(S, "IN_FLIGHT_CARRY_LIMIT", 0)
        _refused("OVER_CAP", store.recovery_snapshot)
        monkeypatch.setattr(S, "IN_FLIGHT_CARRY_LIMIT", 10_000)
        monkeypatch.setattr(S, "RECOVERY_STATE_MAX_BYTES", 100)
        _refused("OVER_CAP", store.recovery_snapshot)
    finally:
        store.close()


def test_a_python_without_in_memory_serialize_refuses(tmp_path, monkeypatch):
    root = _root(tmp_path)
    store = _store(root)
    monkeypatch.setattr(S, "sqlite3", types.SimpleNamespace(Connection=object))
    try:
        _refused("UNSUPPORTED", store.recovery_snapshot)
    finally:
        monkeypatch.undo()
        store.close()


def test_refusal_codes_are_a_closed_set():
    with pytest.raises(ValueError):
        S.RecoverySnapshotRefused("SOMETHING_NEW")


# ---------------------------------------------------------------------------
# F. Adversarial: canaries, hostname collisions, replacement, repair
# ---------------------------------------------------------------------------

CANARIES = ("canary-host.example", "canary-boot-id", "864213579", "/home/alice", "alice@host.local",
            "123456789123", "987654321098765432", "alice-room", "CANARY99", "7777777777")


def test_seeded_canaries_never_reach_the_record_and_are_counted(tmp_path):
    root = _root(tmp_path)
    store = _store(root)
    rt = store.register_runtime()
    token = store.capture("channel:wire:alice-room")
    store.admit(token, kind=S.LeaseKind.TOOL, name="alice@host.local", runtime_id=rt)
    store.trip(token, trigger=S.Trigger.BREAKER_TRIPPED, evidence={
        "trip_kind": "/home/alice/.config/x", "reason": "alice@host.local", "limit": "123456789123",
        "pid": 864213579, "hostname": "canary-host.example", "channel": socket.gethostname(),
        "trigger": "0123456789abcdef0123", "result": "abcdef0123456789abcd", "executed_calls": 4,
        "batch_size": 7, "chat": 555123987, "who": "alicecanary"})
    store.capture("channel:discord:987654321098765432")
    store.close()
    conn = _raw(root)
    conn.execute("UPDATE runtimes SET hostname = 'canary-host.example', boot_id = 'canary-boot-id', "
                 "pid = 864213579, start_ticks = 7777777777, data_root = '/home/alice/.local/share/lumina'")
    conn.execute("UPDATE incidents SET selector = 'CANARY99'")
    conn.execute("INSERT INTO audit (at, action, scope, suspension_id, detail) VALUES (2.0, "
                 "'runtime_registered', NULL, NULL, ?)",
                 (json.dumps({"runtime_id": "f" * 32, "pid": 864213579, "/home/alice": 1,
                              "channel": "canary-host.example", "result": "trip"}),))
    conn.close()
    state = B.capture_recovery_state(root)
    text = state.payload.decode("ascii")
    for canary in CANARIES + (socket.gethostname(), "f" * 32, "0123456789abcdef0123", "abcdef0123456789abcd",
                              "555123987", "alicecanary", '"chat"', '"who"'):
        assert canary not in text, canary
    record = state.record()
    wire = next(i for i in record["incidents"] if i["scope_class"] == "wire")
    assert wire["evidence"]["executed_calls"] == 4 and wire["evidence"]["batch_size"] == 7
    for key in ("trip_kind", "reason", "limit", "pid", "hostname", "channel", "trigger", "result"):
        assert wire["evidence"][key] == S.REDACTED, key
    assert "chat" not in wire["evidence"] and "who" not in wire["evidence"]  # unknown keys (S1-04)
    red = record["redactions"]
    for path in ("incidents.evidence.trip_kind", "incidents.evidence.reason", "incidents.evidence.limit",
                 "incidents.evidence.channel", "incidents.evidence.[unknown_key]", "runtimes.hostname", "runtimes.boot_id", "runtimes.pid",
                 "runtimes.data_root", "runtimes.start_ticks", "incidents.selector", "leases.name",
                 "audit.detail.runtime_id", "audit.detail.[invalid_key]", "audit.detail.channel"):
        assert red.get(path, 0) >= 1, path
    assert record["redactions"]["incidents.evidence.[unknown_key]"] == 2
    assert record["audit"][-1]["detail"]["result"] == "trip"


@pytest.mark.parametrize("hostname", ["trip", "owner", "desktop", "lumina", "read_file"])
def test_a_hostname_that_collides_with_vocabulary_never_changes_structure(tmp_path, monkeypatch, hostname):
    monkeypatch.setattr(S.socket, "gethostname", lambda: hostname)
    root = _root(tmp_path)
    store = _store(root)
    store.register_runtime()
    _trip(store)
    _resume(store)
    _trip(store, evidence={"trip_kind": "read_file", "reason": "desktop"})
    try:
        record = store.recovery_snapshot().record()
    finally:
        store.close()
    assert [a["action"] for a in record["audit"]] == ["initialize", "runtime_registered", "trip", "resume", "trip"]
    assert {a["scope_class"] for a in record["audit"]} == {None, "owner"}
    assert [i["trigger"] for i in record["incidents"]] == ["breaker_tripped"] * 2
    assert record["owner_scope"]["suspended"] is True
    text = json.dumps([i["evidence"] for i in record["incidents"]] + [a["detail"] for a in record["audit"]])
    assert f'"{hostname}"' not in text
    if hostname in ("desktop", "read_file"):
        assert record["redactions"].get("incidents.evidence.reason" if hostname == "desktop"
                                        else "incidents.evidence.trip_kind", 0) >= 1


@pytest.mark.parametrize("how", ["same_bytes_new_inode", "unlink_and_recreate", "other_store"])
@pytest.mark.parametrize("entry", ["exporter", "handle"])
def test_a_store_replaced_mid_capture_is_refused_never_labelled_current(tmp_path, monkeypatch, how, entry):
    root = _root(tmp_path)
    store = _store(root)
    _trip(store)
    db = root / "memory" / S.STORE_NAME
    other = _root(tmp_path, "other")
    _store(other).close()
    real = S._snapshot_copy

    def replace_then_copy(source):
        if how == "same_bytes_new_inode":
            data = db.read_bytes()
            tmp = db.with_name("swap.tmp")
            tmp.write_bytes(data)
            tmp.chmod(0o600)
            os.replace(tmp, db)
        elif how == "unlink_and_recreate":
            data = db.read_bytes()
            db.unlink()
            db.write_bytes(data)
            db.chmod(0o600)
        else:
            os.replace(other / "memory" / S.STORE_NAME, db)
        return real(source)

    monkeypatch.setattr(S, "_snapshot_copy", replace_then_copy)
    try:
        if entry == "handle":
            err = _refused("STORE_FAULT", store.recovery_snapshot)
            assert store.fault is None  # the snapshot never faults the live handle
        else:
            store.close()
            err = _refused("STORE_FAULT", B.capture_recovery_state, root)
        assert err.fault is S.Fault.REPLACED
    finally:
        store.close()


def test_repair_identity_mid_capture_gives_success_or_boundary_changed(tmp_path, monkeypatch):
    root = _root(tmp_path)
    store = _store(root)
    soul = store.soul_id
    store.close()
    pin = root / "memory" / S.IDENTITY_NAME
    real = S._snapshot_copy

    def repair_then_copy(source):
        pin.unlink()
        S.repair_identity(root, ingress=local("repair")).close()
        return real(source)

    monkeypatch.setattr(S, "_snapshot_copy", repair_then_copy)
    try:
        state = B.capture_recovery_state(root)
    except B.ExclusionUnavailable as exc:
        assert exc.code == "BOUNDARY_CHANGED"
    except S.RecoverySnapshotRefused as exc:
        assert exc.code == "BOUNDARY_CHANGED"
    else:  # pragma: no cover - only if the platform reuses the pin inode
        assert state.record()["provenance"]["claimed_source_soul_id"] == soul


def test_a_pin_removed_mid_capture_refuses(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _store(root).close()
    real = S._snapshot_copy

    def unpin_then_copy(source):
        (root / "memory" / S.IDENTITY_NAME).unlink()
        return real(source)

    monkeypatch.setattr(S, "_snapshot_copy", unpin_then_copy)
    err = _refused("STORE_FAULT", B.capture_recovery_state, root)
    assert err.fault is S.Fault.IDENTITY_MISSING


def test_a_pin_rewritten_mid_capture_is_a_boundary_change_for_the_exporter(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _store(root).close()
    pin = root / "memory" / S.IDENTITY_NAME
    real = S._snapshot_copy

    def rewrite_pin_then_copy(source):
        data = pin.read_bytes()
        tmp = pin.with_name("pin.swap")
        tmp.write_bytes(data)
        tmp.chmod(0o600)
        os.replace(tmp, pin)  # same soul, new inode
        return real(source)

    monkeypatch.setattr(S, "_snapshot_copy", rewrite_pin_then_copy)
    with pytest.raises(B.ExclusionUnavailable) as exc:
        B.capture_recovery_state(root)
    assert exc.value.code == "BOUNDARY_CHANGED"


def test_a_mismatch_seen_before_the_copy_leaves_the_live_handle_unfaulted(tmp_path):
    root, other = _root(tmp_path), _root(tmp_path, "other")
    store = _store(root)
    foreign = _store(other)
    pin = root / "memory" / S.IDENTITY_NAME
    try:
        pin.unlink()
        pin.write_bytes(S._identity_bytes(foreign.soul_id))
        pin.chmod(0o600)
        err = _refused("STORE_FAULT", store.recovery_snapshot)
        assert err.fault is S.Fault.SOUL_MISMATCH
        assert store.fault is None
    finally:
        store.close()
        foreign.close()


def test_the_snapshot_validates_its_own_output(tmp_path, monkeypatch):
    root = _root(tmp_path)
    store = _store(root)
    real = S._derive_recovery_body

    def with_extra(*args, **kwargs):
        body = real(*args, **kwargs)
        body["granted"] = True
        return body

    monkeypatch.setattr(S, "_derive_recovery_body", with_extra)
    try:
        _refused("RECORD_INVALID", store.recovery_snapshot)
    finally:
        store.close()


# ---------------------------------------------------------------------------
# G. Evidence index (C1-07)
# ---------------------------------------------------------------------------

def _evidence(root, stamp="20261121T000000Z-0a1b2c3d"):
    path = root / "memory" / S.EVIDENCE_DIRNAME / stamp
    path.mkdir(parents=True, mode=0o700)
    return path


def test_the_evidence_index_lists_names_sizes_and_hashes_never_contents(tmp_path):
    root = _root(tmp_path)
    _store(root).close()
    stamp = _evidence(root)
    (stamp / "restore_trace.json").write_bytes(b"CONTENT-CANARY-1")
    (stamp / S.SALVAGE_SCRATCH_NAME).mkdir()
    (stamp / S.SALVAGE_SCRATCH_NAME / "half.db").write_bytes(b"partial")
    (stamp / "weird name!.json").write_bytes(b"x")
    state = B.capture_recovery_state(root)
    assert b"CONTENT-CANARY" not in state.payload and b"half.db" not in state.payload
    index = state.record()["evidence_index"]
    assert index["files"] == 2 and index["dirs"] == 1 and index["ignored_transient"] == 1
    entry = next(e for e in index["entries"] if e["path"].endswith("restore_trace.json"))
    assert entry == {"path": "20261121T000000Z-0a1b2c3d/restore_trace.json", "size": 16,
                     "sha256": hashlib.sha256(b"CONTENT-CANARY-1").hexdigest()}
    assert any(e["path"].endswith(S.REDACTED) for e in index["entries"])
    assert state.record()["redactions"]["evidence_index.name"] == 1


@pytest.mark.parametrize("change", ["add_file", "grow_file", "remove_file"])
def test_evidence_that_changes_while_it_is_indexed_refuses(tmp_path, monkeypatch, change):
    root = _root(tmp_path)
    store = _store(root)
    stamp = _evidence(root)
    (stamp / "a.json").write_bytes(b"one")
    (stamp / "b.json").write_bytes(b"two")
    real, calls = S._hash_evidence_file, []

    def mutate_then_hash(path, expected):
        calls.append(path)
        if len(calls) == 1:
            if change == "add_file":
                (stamp / "c.json").write_bytes(b"three")
            elif change == "grow_file":
                with open(stamp / "b.json", "ab") as fh:
                    fh.write(b"more")
            else:
                (stamp / "b.json").unlink()
        return real(path, expected)

    monkeypatch.setattr(S, "_hash_evidence_file", mutate_then_hash)
    try:
        _refused("BOUNDARY_CHANGED", store.recovery_snapshot)
    finally:
        store.close()


@pytest.mark.parametrize("problem", ["unreadable_dir", "symlink", "fifo", "hard_link", "not_a_dir"])
def test_unreadable_or_odd_evidence_refuses_never_reads_as_empty(tmp_path, problem):
    root = _root(tmp_path)
    store = _store(root)
    evidence_root = root / "memory" / S.EVIDENCE_DIRNAME
    if problem == "not_a_dir":
        evidence_root.write_bytes(b"")
        evidence_root.chmod(0o600)
    else:
        stamp = _evidence(root)
        (stamp / "a.json").write_bytes(b"one")
        if problem == "unreadable_dir":
            stamp.chmod(0)
        elif problem == "symlink":
            os.symlink("/etc/hostname", stamp / "link.json")
        elif problem == "fifo":
            os.mkfifo(stamp / "pipe")
        else:
            os.link(stamp / "a.json", tmp_path / "outside.json")
    try:
        _refused("EVIDENCE_UNREADABLE", store.recovery_snapshot)
    finally:
        if problem == "unreadable_dir":
            stamp.chmod(0o700)
        store.close()


def test_an_unbounded_evidence_tree_refuses(tmp_path, monkeypatch):
    root = _root(tmp_path)
    store = _store(root)
    stamp = _evidence(root)
    for n in range(5):
        (stamp / f"f{n}.json").write_bytes(b"x")
    try:
        monkeypatch.setattr(S, "EVIDENCE_INDEX_MAX_ENTRIES", 3)
        _refused("EVIDENCE_UNBOUNDED", store.recovery_snapshot)
        monkeypatch.setattr(S, "EVIDENCE_INDEX_MAX_ENTRIES", 4096)
        monkeypatch.setattr(S, "EVIDENCE_INDEX_MAX_BYTES", 2)
        _refused("EVIDENCE_UNBOUNDED", store.recovery_snapshot)
        monkeypatch.setattr(S, "EVIDENCE_INDEX_MAX_BYTES", 2 * 1024 ** 3)
        monkeypatch.setattr(S, "EVIDENCE_INDEX_MAX_DEPTH", 1)
        _refused("EVIDENCE_UNBOUNDED", store.recovery_snapshot)
    finally:
        store.close()


# ---------------------------------------------------------------------------
# H. Bounds and truncation (O-6); the live tables are never pruned
# ---------------------------------------------------------------------------

def test_audit_is_truncated_to_the_last_rows_and_declared(tmp_path, monkeypatch):
    root = _root(tmp_path)
    store = _store(root)
    for _ in range(4):
        _trip(store)
        _resume(store)
    total = len(store.audit_records())
    monkeypatch.setattr(S, "AUDIT_CARRY_LIMIT", 5)
    try:
        record = store.recovery_snapshot().record()
        assert len(store.audit_records()) == total  # the live table is untouched
    finally:
        store.close()
    carried = record["audit_carried"]
    assert carried == {"total": total, "carried": 5, "limit": 5, "truncated": True,
                       "first_seq_carried": total - 4}
    assert [a["seq"] for a in record["audit"]] == list(range(total - 4, total + 1))


def test_runtimes_and_resumed_incidents_are_truncated_active_ones_never(tmp_path, monkeypatch):
    root = _root(tmp_path)
    store = _store(root)
    for _ in range(4):
        store.register_runtime()
        _trip(store)
        _resume(store)
    _trip(store)
    monkeypatch.setattr(S, "RUNTIME_CARRY_LIMIT", 2)
    monkeypatch.setattr(S, "INCIDENT_CARRY_LIMIT", 3)
    try:
        record = store.recovery_snapshot().record()
    finally:
        store.close()
    assert record["runtimes_carried"] == {"total": 4, "carried": 2, "truncated": True}
    conn = _raw(root)
    newest = [r[0] for r in conn.execute("SELECT registered_at FROM runtimes ORDER BY registered_at")][-2:]
    conn.close()
    assert [r["registered_at"] for r in record["runtimes"]] == newest
    assert record["incidents_carried"] == {"active": 1, "resumed": 2, "resumed_total": 4, "truncated": True}
    assert [i["status"] for i in record["incidents"]] == ["resumed", "resumed", "active"]
    assert [i["ref"] for i in record["incidents"]] == ["i000003", "i000004", "i000005"]


def test_the_o6_bound_at_ten_thousand_audit_rows_is_fast_and_small(tmp_path):
    root = _root(tmp_path)
    _store(root).close()
    conn = _raw(root)
    conn.execute("BEGIN")
    conn.executemany("INSERT INTO audit (at, action, scope, suspension_id, detail) VALUES (?, 'trip', 'owner', "
                     "NULL, ?)", [(float(n), json.dumps({"trigger": "breaker_tripped", "in_flight": 0,
                                                          "tool": "read_file", "calls": n}))
                                  for n in range(15_000)])
    conn.execute("COMMIT")
    conn.close()
    started = time.monotonic()
    state = B.capture_recovery_state(root)
    elapsed = time.monotonic() - started
    record = state.record()
    assert record["audit_carried"]["carried"] == S.AUDIT_CARRY_LIMIT == 10_000
    assert record["audit_carried"]["truncated"] is True
    assert elapsed < 20, elapsed
    assert len(state.payload) < 8 * 1024 * 1024


# ---------------------------------------------------------------------------
# I. Structure: closed world in sync with the code; not wired anywhere
# ---------------------------------------------------------------------------

def test_the_disposition_table_covers_every_schema_column_exactly():
    conn = sqlite3.connect(":memory:")
    for statement in S._SCHEMA.split(";\n"):
        if statement.strip():
            conn.execute(statement)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_schema WHERE type = 'table' "
                                         "AND name NOT LIKE 'sqlite_%'")}
    assert tables == set(S._DISPOSITION)
    for table, columns in S._DISPOSITION.items():
        assert [r[1] for r in conn.execute(f"PRAGMA table_info({table})")] == list(columns)
        assert set(columns.values()) <= {"carry", "transform", "drop", "count"}
    assert S._DISPOSITION["incidents"]["selector"] == "drop"
    for column in ("pid", "boot_id", "hostname", "start_ticks", "data_root", "runtime_id"):
        assert S._DISPOSITION["runtimes"][column] == "drop"
    assert set(S._DISPOSITION["events"].values()) == set(S._DISPOSITION["deferred"].values()) == {"count"}


def _audit_actions_written_by(path):
    tree = ast.parse((REPO / path).read_text(encoding="utf-8"))
    actions = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_audit" and len(node.args) >= 3:
            arg = node.args[2]
            if isinstance(arg, ast.Constant):
                actions.add(arg.value)
            elif isinstance(arg, ast.IfExp):
                actions.update(n.value for n in (arg.body, arg.orelse) if isinstance(n, ast.Constant))
            else:
                raise AssertionError(f"non-literal audit action in {path}")
    return actions


def test_the_audit_vocabulary_equals_every_action_the_code_writes():
    assert _audit_actions_written_by("core/suspension.py") == S._AUDIT_ACTIONS
    writers = [p.relative_to(REPO).as_posix() for p in REPO.rglob("*.py")
               if not p.relative_to(REPO).as_posix().startswith(("tests/", "."))
               and "INSERT INTO audit" in p.read_text(encoding="utf-8", errors="ignore")]
    assert writers == ["core/suspension.py"]
    assert S._TRIGGERS == {t.value for t in S.Trigger} | {S.RECOVERY_TRIGGER}
    assert S._LEASE_KINDS == {k.value for k in S.LeaseKind}


def test_no_product_code_calls_the_snapshot_yet():
    names = ("capture_recovery_state", "recovery_snapshot", "validate_recovery_state", "_open_store")
    hits = {}
    for path in REPO.rglob("*.py"):
        rel = path.relative_to(REPO).as_posix()
        if rel.startswith(("tests/", ".")) or rel in ("core/suspension.py", "core/suspension_backup.py"):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        found = [n for n in names if n in text]
        if found:
            hits[rel] = found
    assert hits == {}


def test_the_exporter_entry_point_adds_no_import():
    tree = ast.parse((REPO / "core" / "suspension_backup.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    assert imported <= {"__future__", "os", "re", "stat", "dataclasses", "datetime", "typing", "core.suspension"}


# ---------------------------------------------------------------------------
# J. The strict parser rejects hostile or inconsistent records
# ---------------------------------------------------------------------------

@pytest.fixture
def good_record(tmp_path):
    root, store, _rt = _populated(tmp_path)
    store.close()
    stamp = _evidence(root)
    (stamp / "a.json").write_bytes(b"one")
    record = B.capture_recovery_state(root).record()
    S.validate_recovery_state(record)
    return record


def _set(path, value):
    def apply(record):
        target = record
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
    return apply


def _del(path):
    def apply(record):
        target = record
        for key in path[:-1]:
            target = target[key]
        del target[path[-1]]
    return apply


HOSTILE = {
    "extra_top_key": _set(("granted",), True),
    "missing_top_key": _del(("redactions",)),
    "wrong_schema": _set(("schema",), "lumina.something_else"),
    "wrong_version": _set(("version",), 2),
    "bool_version": _set(("version",), True),
    "wrong_label": _set(("label",), "original evidence"),
    "soul_uppercase": _set(("provenance", "claimed_source_soul_id"), "A" * 32),
    "digest_short": _set(("provenance", "digest_of_ephemeral_capture"), "ab"),
    "bad_sqlite_version": _set(("provenance", "sqlite_version"), "3.51.1; rm -rf"),
    "bad_policy": _set(("provenance", "unknown_scope_policy"), "open"),
    "deferred_rows": _set(("row_counts", "deferred"), 1),
    "negative_count": _set(("row_counts", "audit"), -1),
    "owner_clear_but_active_incident": _set(("owner_scope", "suspended"), False),
    "channel_suspended_over_scopes": _set(("channel_summary", "discord", "suspended"), 5),
    "scope_count_lie": _set(("row_counts", "scopes"), 99),
    "incident_extra_key": _set(("incidents", 0, "selector"), "ABCDEFGH"),
    "incident_dup_ref": lambda r: r["incidents"][1].__setitem__("ref", r["incidents"][0]["ref"]),
    "incident_bad_trigger": _set(("incidents", 0, "trigger"), "owner_said_so"),
    "active_with_outcome": _set(("incidents", 1, "resume_outcome"), {"result": "resumed", "state_revision": 1,
                                                                      "live_leases": 0, "orphaned_leases": 0}),
    "evidence_path_value": _set(("incidents", 1, "evidence", "trip_kind"), "/home/alice"),
    "evidence_hex_value": _set(("incidents", 1, "evidence", "trip_kind"), "abcdef0123456789ab"),
    "evidence_bad_key": _set(("incidents", 1, "evidence", "Bad Key"), 1),
    "evidence_nested": _set(("incidents", 1, "evidence", "trip_kind"), {"x": 1}),
    "evidence_float": _set(("incidents", 1, "evidence", "executed_calls"), 1.5),
    "by_kind_sum": _set(("incidents", 1, "leases_at_trip", "count"), 7),
    "carried_lie": _set(("incidents_carried", "truncated"), True),
    "resumed_total_lie": lambda r: r["incidents_carried"].update(
        resumed_total=r["incidents_carried"]["resumed_total"] + 1, truncated=True),
    "lease_count_lie": _set(("lease_state_counts", "done"), 3),
    "lease_class": _set(("in_flight_leases", 0, "scope_class"), "telegram"),
    "runtimes_lie": _set(("runtimes_carried", "truncated"), True),
    "audit_order": lambda r: r["audit"].reverse(),
    "audit_action": _set(("audit", 0, "action"), "release_wire_seat"),
    "audit_limit": _set(("audit_carried", "limit"), 1),
    "audit_first_seq": _set(("audit_carried", "first_seq_carried"), 99),
    "evidence_dotdot": _set(("evidence_index", "entries", 0, "path"), "../../etc/passwd"),
    "evidence_bytes_lie": _set(("evidence_index", "bytes"), 1),
    "redaction_zero": _set(("redactions", "incidents.selector"), 0),
    "redaction_path": _set(("redactions", "/home/alice"), 1),
    # R1 (Sonnet S1-02, S1-05, S1-07)
    "identifier_raw_pid": _set(("incidents", 1, "evidence", "pid"), 4242),
    "identifier_raw_hostname": _set(("incidents", 1, "evidence", "hostname"), "desktop"),
    "identifier_raw_runtime_id": _set(("incidents", 1, "evidence", "runtime_id"), "abc"),
    "identifier_raw_incarnation": _set(("audit", 0, "detail", "incarnation"), "a" * 32),
    "unknown_free_text_key": _set(("incidents", 1, "evidence", "chat"), 1),
    "non_str_free_text_key": lambda r: r["incidents"][1]["evidence"].__setitem__(1, 1),
    "first_seq_bool": _set(("audit_carried", "first_seq_carried"), True),
    "files_bool": _set(("evidence_index", "files"), True),
    "limit_float": _set(("audit_carried", "limit"), 10000.0),
    "incident_ref_path": _set(("incidents", 0, "ref"), "../../etc/passwd"),
    "assurance_upgraded": _set(("provenance", "assurance"), "verified"),
    # R2 (Sonnet R1-01): equal-valued floats must not pass as the integer totals
    "audit_total_float": lambda r: r["audit_carried"].update(total=float(r["audit_carried"]["total"])),
    "audit_carried_float": lambda r: r["audit_carried"].update(carried=float(r["audit_carried"]["carried"])),
    "runtimes_total_float": lambda r: r["runtimes_carried"].update(total=float(r["runtimes_carried"]["total"])),
    "runtimes_carried_float": lambda r: r["runtimes_carried"].update(
        carried=float(r["runtimes_carried"]["carried"])),
    "evidence_bytes_float": lambda r: r["evidence_index"].update(bytes=float(r["evidence_index"]["bytes"])),
}


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_the_parser_rejects_hostile_records(good_record, name):
    HOSTILE[name](good_record)
    _refused("RECORD_INVALID", S.validate_recovery_state, good_record)


def test_the_parser_rejects_non_objects():
    for value in (None, [], "record", 1):
        _refused("RECORD_INVALID", S.validate_recovery_state, value)


# ---------------------------------------------------------------------------
# K. R1: Sonnet S1-01, S1-03, S1-04, S1-06, S1-07, S1-10
# ---------------------------------------------------------------------------

def test_a_capture_in_a_child_process_does_not_delay_trips(tmp_path):
    """S1-01: W-1 holds at the millisecond level when the capture runs in its
    own process (the C1-S3 requirement); in-process it only competes for
    Python's interpreter lock, never for the store."""
    root = _root(tmp_path)
    store = _store(root)
    conn = _raw(root)
    conn.execute("BEGIN")
    conn.executemany("INSERT INTO audit (at, action, scope, suspension_id, detail) VALUES (?, 'initialize', "
                     "NULL, NULL, '{}')", [(1.0,)] * 20000)
    conn.execute("COMMIT")
    conn.close()
    baseline = _latencies(store, 30)
    stop = tmp_path / "stop"
    script = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {str(REPO)!r})
        os.environ["LUMINA_TESTING"] = "1"
        from core import suspension_backup as B
        B.capture_recovery_state({str(root)!r})
        print("ready", flush=True)
        while not os.path.exists({str(stop)!r}):
            B.capture_recovery_state({str(root)!r})
    """)
    child = subprocess.Popen([sys.executable, "-c", script], env=dict(os.environ, LUMINA_TESTING="1"),
                             cwd=str(tmp_path), stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        during = _latencies(store, 30)
    finally:
        stop.write_text("")
        child.wait(timeout=60)
        store.close()
    assert child.returncode == 0
    p95 = lambda xs: xs[int(len(xs) * 0.95) - 1]
    assert p95(during) <= max(3 * p95(baseline), 0.05), (p95(baseline), p95(during))


def test_deeply_nested_json_in_the_store_refuses_with_a_closed_code(tmp_path):
    root = _root(tmp_path)
    store = _store(root)
    _trip(store)
    conn = _raw(root)
    conn.execute("UPDATE incidents SET evidence = ?", ("[" * 200_000,))
    conn.close()
    try:
        _refused("VALUE_UNKNOWN", store.recovery_snapshot)
    finally:
        store.close()


def test_unknown_free_text_keys_are_dropped_and_counted_never_carried(tmp_path):
    root = _root(tmp_path)
    store = _store(root)
    _trip(store, evidence={"chat": 555123987, "user": "alicecanary", "executed_calls": 2})
    try:
        state = store.recovery_snapshot()
    finally:
        store.close()
    assert b"555123987" not in state.payload and b"alicecanary" not in state.payload
    assert b'"chat"' not in state.payload and b'"user"' not in state.payload
    record = state.record()
    assert record["incidents"][0]["evidence"] == {"executed_calls": 2}
    assert record["redactions"]["incidents.evidence.[unknown_key]"] == 2


def test_provenance_says_every_field_is_a_producer_claim(tmp_path):
    root = _root(tmp_path)
    _store(root).close()
    prov = B.capture_recovery_state(root).record()["provenance"]
    assert prov["assurance"] == "producer_claims_only"
    assert "claimed_captured_at" in prov and "captured_at" not in prov


def test_zero_room_for_resumed_incidents_keeps_only_the_active_ones(tmp_path, monkeypatch):
    root = _root(tmp_path)
    store = _store(root)
    for _ in range(3):
        _trip(store)
        _resume(store)
    _trip(store)
    monkeypatch.setattr(S, "INCIDENT_CARRY_LIMIT", 1)
    try:
        record = store.recovery_snapshot().record()
    finally:
        store.close()
    assert [i["status"] for i in record["incidents"]] == ["active"]
    assert record["incidents_carried"] == {"active": 1, "resumed": 0, "resumed_total": 3, "truncated": True}


def test_the_local_hostname_is_redacted_even_with_no_registered_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(S.socket, "gethostname", lambda: "lumina")
    root = _root(tmp_path)
    store = _store(root)
    _trip(store, evidence={"reason": "lumina", "trip_kind": "stale_streak"})
    try:
        record = store.recovery_snapshot().record()
    finally:
        store.close()
    assert record["incidents"][0]["evidence"] == {"reason": S.REDACTED, "trip_kind": "stale_streak"}


def test_the_digest_follows_the_captured_content(tmp_path):
    a, b = _root(tmp_path, "a"), _root(tmp_path, "b")
    _store(a).close()
    store = _store(b)
    _trip(store)
    store.close()
    da = B.capture_recovery_state(a).record()["provenance"]["digest_of_ephemeral_capture"]
    db = B.capture_recovery_state(b).record()["provenance"]["digest_of_ephemeral_capture"]
    assert da != db and hashlib.sha256(b"").hexdigest() not in (da, db)


def test_a_same_size_rewrite_of_evidence_while_indexing_refuses(tmp_path, monkeypatch):
    root = _root(tmp_path)
    store = _store(root)
    stamp = _evidence(root)
    (stamp / "a.json").write_bytes(b"one")
    (stamp / "b.json").write_bytes(b"two")
    real, calls = S._hash_evidence_file, []

    def rewrite_then_hash(path, expected):
        calls.append(path)
        if len(calls) == 1:
            time.sleep(0.01)
            with open(stamp / "b.json", "r+b") as fh:
                fh.write(b"TWO")
        return real(path, expected)

    monkeypatch.setattr(S, "_hash_evidence_file", rewrite_then_hash)
    try:
        _refused("BOUNDARY_CHANGED", store.recovery_snapshot)
    finally:
        store.close()


def test_a_free_text_integer_beyond_2_53_is_redacted(tmp_path):
    root = _root(tmp_path)
    store = _store(root)
    _trip(store)
    conn = _raw(root)
    conn.execute("UPDATE incidents SET evidence = ?", (json.dumps({"executed_calls": 2 ** 60}),))
    conn.close()
    try:
        record = store.recovery_snapshot().record()
    finally:
        store.close()
    assert record["incidents"][0]["evidence"]["executed_calls"] == S.REDACTED


def test_only_a_salvage_scratch_directory_is_skipped(tmp_path):
    root = _root(tmp_path)
    _store(root).close()
    stamp = _evidence(root)
    (stamp / S.SALVAGE_SCRATCH_NAME).write_bytes(b"a file, not the scratch dir")
    index = B.capture_recovery_state(root).record()["evidence_index"]
    assert index["ignored_transient"] == 0
    assert [e["path"] for e in index["entries"]] == [f"{stamp.name}/{S.SALVAGE_SCRATCH_NAME}"]


def test_an_evidence_directory_that_vanishes_mid_scan_is_a_boundary_change(tmp_path, monkeypatch):
    root = _root(tmp_path)
    store = _store(root)
    stamp = _evidence(root)
    (stamp / "a.json").write_bytes(b"one")
    real = os.listdir

    def vanish(path):
        if os.fspath(path) == str(stamp):
            raise FileNotFoundError(path)
        return real(path)

    monkeypatch.setattr(S.os, "listdir", vanish)
    try:
        _refused("BOUNDARY_CHANGED", store.recovery_snapshot)
    finally:
        monkeypatch.undo()
        store.close()


def test_dropped_events_are_counted(tmp_path):
    root = _root(tmp_path)
    store = _store(root)
    _trip(store)
    _resume(store)
    try:
        record = store.recovery_snapshot().record()
    finally:
        store.close()
    assert record["row_counts"]["events"] == 1 and record["redactions"]["events.[rows]"] == 1


def _dict_keys(node):
    return {k.value for k in node.keys if isinstance(k, ast.Constant)} if isinstance(node, ast.Dict) else set()


def test_the_free_text_key_allowlist_equals_the_keys_the_code_writes():
    written = {"evidence_dropped"}  # set by _sanitize_evidence on a dropped item
    agent = ast.parse((REPO / "core" / "agent.py").read_text(encoding="utf-8"))
    callers = 0
    for node in ast.walk(agent):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_suspend_scope":
            written |= _dict_keys(node.args[2])
            callers += 1
    assert callers >= 2
    core = ast.parse((REPO / "core" / "suspension.py").read_text(encoding="utf-8"))
    for node in ast.walk(core):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) in ("_audit", "_insert_incident"):
            for kw in node.keywords:
                if kw.arg in ("detail", "evidence"):
                    written |= _dict_keys(kw.value)
    assert written - S._IDENTIFIER_KEYS == S._FREE_TEXT_KEYS
    assert not S._FREE_TEXT_KEYS & S._IDENTIFIER_KEYS
