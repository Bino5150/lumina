"""C1 backup exclusion interface (core/suspension_backup.py).

An export of ordinary history must leave C1's protected state alone: this
module names that state, observes it by lstat only, refuses what it can't
observe, and re-checks the boundary before publication. Every data root here
is a disposable tmp_path; no real store is touched.
"""
import ast
import builtins
import hashlib
import io
import json
import os
import sqlite3
from pathlib import Path

import pytest

from core import operator_ingress as oi
from core import suspension as S
from core import suspension_backup as B

REPO = Path(__file__).resolve().parent.parent
SELECTOR_EVIDENCE = "SYNTHETIC-EVIDENCE-NEVER-EXPORT"


def _root(tmp_path, name="data"):
    root = tmp_path / name
    (root / S.MEMORY_DIRNAME).mkdir(parents=True, mode=0o700)
    (root / S.MEMORY_DIRNAME).chmod(0o700)
    (root / S.MEMORY_DIRNAME / "lumina.db").write_bytes(b"ordinary history")
    (root / S.MEMORY_DIRNAME / "prefs.json").write_text("{}")
    return root


def _initialize(root, *, trip=True):
    store = S.initialize(root, ingress=oi.local_suspension_control("test"))
    selector = None
    if trip:
        store.trip(store.capture(S.OWNER_SCOPE), trigger=S.Trigger.BREAKER_TRIPPED,
                   evidence={"tool": SELECTOR_EVIDENCE})
        selector = store.operator_view()[0].selector
    soul, incarnation = store.soul_id, store.incarnation
    store.close()
    return selector, soul, incarnation


def _inventory(root):
    out = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            digest = None
            if os.path.isfile(p) and not os.path.islink(p) and st.st_mode & 0o400:
                with open(p, "rb") as f:
                    digest = hashlib.sha256(f.read()).hexdigest()
            out[os.path.relpath(p, root)] = (st.st_mode, st.st_size, st.st_mtime_ns, st.st_ino, digest)
    return out


def _rels(exclusion):
    return [e.relpath for e in exclusion.entries]


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("relpath", [
    "memory/suspension.db", "memory/suspension.db-wal", "memory/suspension.db-shm",
    "memory/suspension.db-journal", "memory/suspension.db-mj1A2B", "memory/soul_identity.json",
    "memory/soul_identity.json.0123456789ab.tmp", "memory/suspension_evidence",
    "memory/suspension_evidence/20261020T000000Z-ab12/suspension.db",
    "memory/suspension_evidence/x/salvage-scratch/suspension.db-wal",
])
def test_the_protected_family(relpath):
    assert B.is_protected_relpath(relpath)


@pytest.mark.parametrize("relpath", [
    "memory/lumina.db", "memory/lumina.db-wal", "memory/prefs.json", "memory/ledger.db",
    "memory/tool_audit.log", "memory/pending_actions.json", "memory/pending_actions_audit.log",
    "memory/flight_recorder.db", "suspension.db", "skills/suspension.db",
    "memory/sub/suspension.db", "memory/soul_identity.json.bak", "memory/soul_identity.json.XYZ.tmp",
    "memory/suspension.dbx", "memory/suspension_evidence_old",
])
def test_ordinary_content_is_not_protected(relpath):
    assert not B.is_protected_relpath(relpath)


@pytest.mark.parametrize("bad", ["/memory/suspension.db", "memory/../memory/suspension.db",
                                 "memory//suspension.db", "./memory/suspension.db", ""])
def test_a_path_that_is_not_plain_relative_is_rejected_not_guessed(bad):
    with pytest.raises(ValueError):
        B.is_protected_relpath(bad)
    with pytest.raises(TypeError):
        B.is_protected_relpath(Path("memory/suspension.db"))


def test_drift_every_file_c1_creates_is_protected(tmp_path, monkeypatch):
    """Ties the family to what core/suspension.py actually writes: initialize,
    a trip, a pin repair (atomic tmp write) and a destructive recovery
    (evidence and salvage scratch)."""
    root = _root(tmp_path)
    before = set(_inventory(root))
    written = []
    real_write = S._write_new_private
    monkeypatch.setattr(S, "_write_new_private", lambda path, data: (written.append(path), real_write(path, data))[1])

    _initialize(root)
    (root / "memory" / S.IDENTITY_NAME).write_bytes(b"garbage pin")
    S.repair_identity(root, ingress=oi.local_suspension_control("test")).close()
    (root / "memory" / S.STORE_NAME).write_bytes(b"corrupt store bytes")
    for side in ("-wal", "-shm"):
        (root / "memory" / (S.STORE_NAME + side)).unlink(missing_ok=True)
    S.recover_destructively(root, ingress=oi.local_suspension_control("test"),
                            acknowledge_unreadable_runtimes=True)

    created = set(_inventory(root)) - before
    assert any("suspension_evidence" in c for c in created) and any(c.endswith(".tmp") for c in
                                                                    (os.path.relpath(w, root) for w in written))
    for rel in created | {os.path.relpath(w, root) for w in written}:
        assert B.is_protected_relpath(rel.replace(os.sep, "/")), rel
    for rel in before:
        assert not B.is_protected_relpath(rel.replace(os.sep, "/")), rel


# ---------------------------------------------------------------------------
# describe_exclusions: statuses, never opens or writes
# ---------------------------------------------------------------------------

def test_an_initialized_store_with_an_active_suspension_is_present_excluded(tmp_path):
    root = _root(tmp_path)
    _initialize(root)
    exclusion = B.describe_exclusions(root)
    assert exclusion.status == "present_excluded"
    assert {"memory/suspension.db", "memory/soul_identity.json"} <= set(_rels(exclusion))
    assert "memory/lumina.db" not in _rels(exclusion)
    stable = {e.relpath for e in exclusion.entries if e.stable}
    assert stable == {"memory/suspension.db", "memory/soul_identity.json"}
    st = os.lstat(root / "memory" / S.STORE_NAME)
    assert (st.st_dev, st.st_ino) in exclusion.denied_identities()


def test_no_trace_is_absent_unknown_never_clear(tmp_path):
    root = _root(tmp_path)
    exclusion = B.describe_exclusions(root)
    assert exclusion.status == "absent_unknown" and exclusion.entries == ()
    record = exclusion.manifest_record()
    assert record["status"] == "absent_unknown"
    assert record["restore_fence"] == "c1_trace_and_owner_recover_required"
    assert "clear" not in json.dumps(record) and "first" not in json.dumps(record)


def test_no_memory_directory_is_absent_unknown(tmp_path):
    root = tmp_path / "bare"
    root.mkdir()
    exclusion = B.describe_exclusions(root)
    assert exclusion.status == "absent_unknown" and exclusion.memory_dir is None


@pytest.mark.parametrize("damage", ["corrupt", "future_schema", "garbage_pin", "mode_000", "pin_only", "tmp_leftover"])
def test_damaged_newer_or_unreadable_protected_state_is_still_just_excluded(tmp_path, damage):
    root = _root(tmp_path)
    memory = root / "memory"
    if damage == "pin_only":
        (memory / S.IDENTITY_NAME).write_text('{"schema": "future", "version": 99}')
    elif damage == "tmp_leftover":
        (memory / (S.IDENTITY_NAME + ".a1b2c3d4e5f6.tmp")).write_bytes(b"half-written pin")
    else:
        _initialize(root)
        if damage == "corrupt":
            (memory / S.STORE_NAME).write_bytes(b"not sqlite")
        elif damage == "future_schema":
            conn = sqlite3.connect(memory / S.STORE_NAME)
            conn.execute("UPDATE meta SET value = '999' WHERE key = 'schema_version'")
            conn.commit(), conn.close()
        elif damage == "garbage_pin":
            (memory / S.IDENTITY_NAME).write_bytes(b"\x00\xff")
        elif damage == "mode_000":
            (memory / S.STORE_NAME).chmod(0)
    exclusion = B.describe_exclusions(root)
    assert exclusion.status == "present_excluded" and exclusion.warnings == ()
    B.verify_exclusion_unchanged(root, exclusion)


def test_it_never_opens_reads_or_writes_anything_under_memory(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _initialize(root)
    (root / "memory" / S.EVIDENCE_DIRNAME / "stamp").mkdir(parents=True)
    (root / "memory" / S.EVIDENCE_DIRNAME / "stamp" / S.STORE_NAME).write_bytes(b"evidence")
    before = _inventory(root)
    memory = str(root / "memory")

    def guard(name, real):
        def wrapped(path, *args, **kwargs):
            if isinstance(path, (str, bytes, os.PathLike)) and os.fsdecode(os.fspath(path)).startswith(memory):
                raise AssertionError(f"{name} touched {path}")
            return real(path, *args, **kwargs)
        return wrapped

    for module, name in ((os, "open"), (builtins, "open"), (io, "open"), (sqlite3, "connect"),
                         (os, "replace"), (os, "rename"), (os, "unlink"), (os, "remove"),
                         (os, "mkdir"), (os, "chmod"), (os, "rmdir"), (os, "link"), (os, "symlink"),
                         (os, "utime"), (os, "truncate")):
        monkeypatch.setattr(module, name, guard(name, getattr(module, name)))
    exclusion = B.describe_exclusions(root)
    B.verify_exclusion_unchanged(root, exclusion)
    monkeypatch.undo()
    assert _inventory(root) == before
    assert not (root / "memory" / (S.STORE_NAME + "-wal")).exists() or "memory/suspension.db-wal" in before


def test_the_module_imports_no_sqlite_runtime_qt_or_backup_code():
    tree = ast.parse((REPO / "core" / "suspension_backup.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    assert imported <= {"__future__", "os", "re", "stat", "dataclasses", "datetime", "typing", "core.suspension"}


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------

def test_an_unobservable_memory_directory_is_refused_never_absent(tmp_path):
    root = _root(tmp_path)
    _initialize(root)
    (root / "memory").chmod(0)
    try:
        with pytest.raises(B.ExclusionUnavailable) as exc:
            B.describe_exclusions(root)
        assert exc.value.code == "PROTECTED_ENTRY_UNOBSERVABLE"
    finally:
        (root / "memory").chmod(0o700)


def test_an_unscannable_evidence_tree_is_refused(tmp_path):
    root = _root(tmp_path)
    inner = root / "memory" / S.EVIDENCE_DIRNAME / "stamp"
    inner.mkdir(parents=True)
    inner.chmod(0)
    try:
        with pytest.raises(B.ExclusionUnavailable) as exc:
            B.describe_exclusions(root)
        assert exc.value.code == "PROTECTED_ENTRY_UNOBSERVABLE"
    finally:
        inner.chmod(0o700)


@pytest.mark.parametrize("where", ["memory", "evidence"])
def test_a_protected_name_that_can_be_listed_but_not_lstated_is_refused(tmp_path, where):
    root = _root(tmp_path)
    if where == "memory":
        _initialize(root, trip=False)
        locked = root / "memory"
    else:
        locked = root / "memory" / S.EVIDENCE_DIRNAME / "stamp"
        locked.mkdir(parents=True)
        (locked / S.STORE_NAME).write_bytes(b"evidence")
    locked.chmod(0o500 & ~0o100)  # readable (names list) but not searchable (lstat fails)
    try:
        with pytest.raises(B.ExclusionUnavailable) as exc:
            B.describe_exclusions(root)
        assert exc.value.code == "PROTECTED_ENTRY_UNOBSERVABLE"
    finally:
        locked.chmod(0o700)


def test_every_protected_entry_is_on_the_deny_list_not_only_the_stable_ones(tmp_path):
    root = _root(tmp_path)
    _initialize(root, trip=False)
    memory = root / "memory"
    (memory / (S.STORE_NAME + "-wal")).write_bytes(b"sidecar")
    (memory / (S.IDENTITY_NAME + ".abc123.tmp")).write_bytes(b"leftover")
    (memory / S.EVIDENCE_DIRNAME / "s").mkdir(parents=True)
    (memory / S.EVIDENCE_DIRNAME / "s" / S.STORE_NAME).write_bytes(b"evidence")
    exclusion = B.describe_exclusions(root)
    denied = exclusion.denied_identities()
    for rel in (S.STORE_NAME + "-wal", S.IDENTITY_NAME + ".abc123.tmp", S.EVIDENCE_DIRNAME,
                f"{S.EVIDENCE_DIRNAME}/s", f"{S.EVIDENCE_DIRNAME}/s/{S.STORE_NAME}"):
        st = os.lstat(memory / rel)
        assert (st.st_dev, st.st_ino) in denied, rel
    st = os.lstat(memory / "lumina.db")
    assert (st.st_dev, st.st_ino) not in denied


@pytest.mark.parametrize("target", ["store", "pin", "sidecar", "evidence_file"])
def test_a_hard_link_to_any_protected_file_refuses(tmp_path, target):
    root = _root(tmp_path)
    _initialize(root)
    memory = root / "memory"
    if target == "sidecar":
        conn = sqlite3.connect(memory / S.STORE_NAME)  # leave a real WAL behind
        conn.execute("PRAGMA journal_mode=WAL"), conn.execute("CREATE TABLE IF NOT EXISTS x(y)")
        source = memory / (S.STORE_NAME + "-wal")
        assert source.exists()
    elif target == "evidence_file":
        (memory / S.EVIDENCE_DIRNAME / "s").mkdir(parents=True)
        source = memory / S.EVIDENCE_DIRNAME / "s" / S.STORE_NAME
        source.write_bytes(b"evidence")
    else:
        source = memory / (S.STORE_NAME if target == "store" else S.IDENTITY_NAME)
    (root / "skills").mkdir()
    os.link(source, root / "skills" / "innocent.md")
    try:
        with pytest.raises(B.ExclusionUnavailable) as exc:
            B.describe_exclusions(root)
        assert exc.value.code == "PROTECTED_ENTRY_ALIASED"
    finally:
        if target == "sidecar":
            conn.close()


def test_a_symlink_at_a_protected_name_is_excluded_and_never_followed(tmp_path, monkeypatch):
    root = _root(tmp_path)
    outside = tmp_path / "outside-tripwire"
    outside.write_bytes(b"never look at me")
    os.symlink(outside, root / "memory" / S.STORE_NAME)
    os.symlink(tmp_path / "missing", root / "memory" / S.IDENTITY_NAME)
    real_lstat, real_stat = os.lstat, os.stat
    seen = []
    monkeypatch.setattr(os, "lstat", lambda p, *a, **k: (seen.append(os.fspath(p)), real_lstat(p, *a, **k))[1])
    monkeypatch.setattr(os, "stat", lambda p, *a, **k: (seen.append(("stat", os.fspath(p), k)), real_stat(p, *a, **k))[1])
    exclusion = B.describe_exclusions(root)
    monkeypatch.undo()
    assert exclusion.status == "present_excluded"
    assert exclusion.warnings == ("protected_entry_not_regular",)
    assert {e.kind for e in exclusion.entries} == {"other"}
    assert str(outside) not in [s for s in seen if isinstance(s, str)]
    assert not [s for s in seen if isinstance(s, tuple) and s[2].get("follow_symlinks", True)]


def test_a_symlink_loop_in_the_evidence_tree_is_never_followed(tmp_path):
    root = _root(tmp_path)
    evidence = root / "memory" / S.EVIDENCE_DIRNAME
    evidence.mkdir()
    os.symlink(evidence, evidence / "loop")
    exclusion = B.describe_exclusions(root)
    assert "memory/suspension_evidence/loop" in _rels(exclusion)
    assert exclusion.warnings == ("protected_entry_not_regular",)


@pytest.mark.parametrize("shape", ["memory_symlink", "memory_file", "root_symlink", "root_file", "root_missing"])
def test_an_unsafe_root_is_refused(tmp_path, shape):
    root = tmp_path / "data"
    if shape == "memory_symlink":
        real = _root(tmp_path, "real")
        root.mkdir()
        os.symlink(real / "memory", root / "memory")
        code = "MEMORY_ROOT_UNSAFE"
    elif shape == "memory_file":
        root.mkdir()
        (root / "memory").write_text("x")
        code = "MEMORY_ROOT_UNSAFE"
    elif shape == "root_symlink":
        os.symlink(_root(tmp_path, "real"), root)
        code = "DATA_ROOT_UNSAFE"
    elif shape == "root_file":
        root.write_text("x")
        code = "DATA_ROOT_UNSAFE"
    else:
        code = "DATA_ROOT_UNSAFE"
    with pytest.raises(B.ExclusionUnavailable) as exc:
        B.describe_exclusions(root)
    assert exc.value.code == code


def test_an_oversized_or_too_deep_evidence_tree_is_refused(tmp_path, monkeypatch):
    root = _root(tmp_path)
    evidence = root / "memory" / S.EVIDENCE_DIRNAME
    deep = evidence.joinpath("a", "b", "c", "d")
    deep.mkdir(parents=True)
    B.describe_exclusions(root)  # depth 4 is allowed
    (deep / "e").write_bytes(b"x")
    with pytest.raises(B.ExclusionUnavailable) as exc:
        B.describe_exclusions(root)
    assert exc.value.code == "EVIDENCE_INVENTORY_UNBOUNDED"
    (deep / "e").unlink()
    monkeypatch.setattr(B, "EVIDENCE_MAX_ENTRIES", 5)
    for i in range(3):
        (evidence / f"f{i}").write_bytes(b"x")
    with pytest.raises(B.ExclusionUnavailable) as exc:
        B.describe_exclusions(root)
    assert exc.value.code == "EVIDENCE_INVENTORY_UNBOUNDED"


# ---------------------------------------------------------------------------
# verify_exclusion_unchanged
# ---------------------------------------------------------------------------

def test_verify_passes_when_nothing_stable_changed_even_while_c1_runs(tmp_path):
    root = _root(tmp_path)
    _initialize(root, trip=False)
    exclusion = B.describe_exclusions(root)
    store = S.open_store(root)              # a real open/close: sidecars come and go,
    store.trip(store.capture(S.OWNER_SCOPE), trigger=S.Trigger.BREAKER_TRIPPED, evidence={})
    B.verify_exclusion_unchanged(root, exclusion)   # bytes changed, identities didn't
    store.close()
    B.verify_exclusion_unchanged(root, exclusion)
    (root / "memory" / (S.IDENTITY_NAME + ".00aa.tmp")).write_bytes(b"leftover")
    B.verify_exclusion_unchanged(root, exclusion)


def _boundary_changed(root, exclusion):
    with pytest.raises(B.ExclusionUnavailable) as exc:
        B.verify_exclusion_unchanged(root, exclusion)
    assert exc.value.code == "BOUNDARY_CHANGED"


def test_verify_catches_a_store_initialized_after_an_absent_observation(tmp_path):
    root = _root(tmp_path)
    exclusion = B.describe_exclusions(root)
    _initialize(root)
    _boundary_changed(root, exclusion)


def test_verify_catches_a_repaired_pin_and_a_recovered_store(tmp_path):
    root = _root(tmp_path)
    _initialize(root)
    exclusion = B.describe_exclusions(root)
    (root / "memory" / S.IDENTITY_NAME).write_bytes(b"garbage")
    S.repair_identity(root, ingress=oi.local_suspension_control("test")).close()
    _boundary_changed(root, exclusion)

    exclusion = B.describe_exclusions(root)
    (root / "memory" / S.STORE_NAME).write_bytes(b"corrupt")
    S.recover_destructively(root, ingress=oi.local_suspension_control("test"),
                            acknowledge_unreadable_runtimes=True)
    _boundary_changed(root, exclusion)


def test_verify_catches_evidence_appearing_and_a_store_moved_out(tmp_path):
    root = _root(tmp_path)
    _initialize(root)
    exclusion = B.describe_exclusions(root)
    (root / "memory" / S.EVIDENCE_DIRNAME).mkdir()
    _boundary_changed(root, exclusion)
    (root / "memory" / S.EVIDENCE_DIRNAME).rmdir()
    (root / "skills").mkdir()
    store_identity = os.lstat(root / "memory" / S.STORE_NAME)
    os.rename(root / "memory" / S.STORE_NAME, root / "skills" / "renamed.md")
    _boundary_changed(root, exclusion)
    # the moved inode is still on the deny list a capture checks
    assert (store_identity.st_dev, store_identity.st_ino) in exclusion.denied_identities()


def test_verify_catches_a_swapped_memory_directory_or_data_root(tmp_path):
    root = _root(tmp_path)
    _initialize(root)
    exclusion = B.describe_exclusions(root)
    os.rename(root / "memory", root / "memory.old")
    _root(tmp_path)  # same path, new memory/
    _initialize(root)
    _boundary_changed(root, exclusion)

    exclusion = B.describe_exclusions(root)
    os.rename(root, tmp_path / "data.old")
    (tmp_path / "data" / "memory").mkdir(parents=True)
    _boundary_changed(tmp_path / "data", exclusion)


def test_verify_catches_a_swapped_memory_directory_with_no_c1_state(tmp_path):
    root = _root(tmp_path)
    exclusion = B.describe_exclusions(root)        # absent_unknown: nothing stable to compare
    os.rename(root / "memory", root / "memory.old")
    (root / "memory").mkdir(mode=0o700)
    _boundary_changed(root, exclusion)


def test_verify_catches_a_swapped_data_root_with_no_c1_state(tmp_path):
    root = _root(tmp_path)
    shutil_root = tmp_path / "data.old"
    exclusion = B.describe_exclusions(root)
    os.rename(root, shutil_root)
    root.mkdir()
    os.rename(shutil_root / "memory", root / "memory")   # same memory/ inode, new root
    _boundary_changed(root, exclusion)


def test_verify_still_refuses_a_new_alias(tmp_path):
    root = _root(tmp_path)
    _initialize(root)
    exclusion = B.describe_exclusions(root)
    os.link(root / "memory" / S.IDENTITY_NAME, root / "alias")
    with pytest.raises(B.ExclusionUnavailable) as exc:
        B.verify_exclusion_unchanged(root, exclusion)
    assert exc.value.code == "PROTECTED_ENTRY_ALIASED"


def test_verify_takes_only_a_real_descriptor(tmp_path):
    root = _root(tmp_path)
    with pytest.raises(TypeError):
        B.verify_exclusion_unchanged(root, B.describe_exclusions(root).manifest_record())


# ---------------------------------------------------------------------------
# What an export may say
# ---------------------------------------------------------------------------

def test_the_manifest_record_is_closed_and_carries_nothing_protected(tmp_path):
    root = _root(tmp_path)
    selector, soul, incarnation = _initialize(root)
    (root / "memory" / S.EVIDENCE_DIRNAME / "s").mkdir(parents=True)
    exclusion = B.describe_exclusions(root)
    record = exclusion.manifest_record()
    assert set(record) == {"schema", "version", "status", "observed_at", "warnings", "snapshot_captured",
                           "enforcement_compatibility", "c1_schema_version", "suspended_at_capture",
                           "restore_fence"}
    assert record["snapshot_captured"] is False and record["enforcement_compatibility"] == "unproven"
    assert record["c1_schema_version"] == "unknown" and record["suspended_at_capture"] == "unknown"
    dumped = json.dumps(record)
    for secret in (selector, soul, incarnation, S.OWNER_SCOPE + '"', SELECTOR_EVIDENCE, "suspension.db",
                   "soul_identity", "suspension_evidence", str(root)):
        assert secret not in dumped, secret
    for entry in exclusion.entries:
        assert str(entry.ino) not in dumped


def test_the_descriptor_is_immutable_and_refusal_codes_are_closed(tmp_path):
    exclusion = B.describe_exclusions(_root(tmp_path))
    with pytest.raises(Exception):
        exclusion.status = "clear"
    with pytest.raises(ValueError):
        B.ExclusionUnavailable("SOMETHING_ELSE")


def test_only_backup_modules_may_import_the_exclusion_interface():
    tracked = [REPO / p for p in os.popen(f"git -C {REPO} ls-files '*.py'").read().split()]
    importers = []
    for path in tracked:
        rel = path.relative_to(REPO).as_posix()
        if rel.startswith("tests/") or not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if ((isinstance(node, ast.ImportFrom) and (node.module == "core.suspension_backup"
                 or (node.module == "core" and any(a.name == "suspension_backup" for a in node.names))))
                    or (isinstance(node, ast.Import) and any(a.name == "core.suspension_backup" for a in node.names))):
                importers.append(rel)
    assert all(rel.startswith("core/agent_backup") for rel in importers), importers
