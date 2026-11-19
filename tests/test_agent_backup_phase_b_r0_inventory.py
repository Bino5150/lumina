"""AGENT-BACKUP-RESTORE-PHASE-B R0: data-root inventory completeness.

Synthetic data only. Pins that the inventory classifies every data-root
entry, never opens or follows anything, blocks on state the Agent Backup
contract does not capture, and agrees with a real export manifest.
"""
import json
import os
import sqlite3
from pathlib import Path

import pytest

import core.suspension as S
from core import agent_backup_export as abx
from core import agent_backup_inventory as inv
from core import operator_ingress as oi
from test_agent_backup import _make_env, NO_IDENTITY_TRAILERS, NO_CREDENTIALS


@pytest.fixture
def env(tmp_path):
    data, base = map(Path, _make_env(tmp_path))
    (data / "memory").chmod(0o700)
    (data / "knowledge_packs_installed.json").write_text(json.dumps(
        {"lumina-self-knowledge": {"version": 1, "installed_at": "2026-10-20"}}))
    return data, base, tmp_path


def _export(env, name="export.zip"):
    data, base, tmp = env
    manifest, _receipt = abx.build_review_export_with_receipt(
        str(data), str(base), str(tmp / name),
        identity_trailers_path=NO_IDENTITY_TRAILERS, credentials_path=NO_CREDENTIALS)
    return manifest


def _classes(report):
    return {e.relpath: e.cls for e in report.entries}


def _tree(root):
    out = {}
    for dirpath, dirs, files in os.walk(root):
        for name in dirs + files:
            path = os.path.join(dirpath, name)
            st = os.lstat(path)
            out[os.path.relpath(path, root)] = (st.st_ino, st.st_size, st.st_mtime_ns)
    return out


def test_clean_synthetic_root_is_complete_and_matches_a_real_export(env):
    report = inv.inventory(env[0])
    classes = _classes(report)
    assert report.no_known_blockers, report.blockers
    assert classes["memory/lumina.db"] == inv.CAPTURED
    assert classes["memory/ledger.db"] == inv.REGENERATED
    assert classes["custom_tools/_pending/staged_tool.py"] == inv.CAPTURED
    assert classes["projects/demo/binding.json"] == inv.CAPTURED
    result = inv.parity(report, _export(env))
    assert result == {"missing_from_archive": [], "unexpected_in_archive": [], "parity": True}


def test_logical_ids_match_the_export_manifest(env):
    report = inv.inventory(env[0])
    manifest = _export(env)
    by_path = {m["archive_path"]: m["logical_id"] for m in manifest["members"]}
    for entry in report.entries:
        if entry.archive_path:
            assert by_path[entry.archive_path] == entry.logical_id


def test_c1_state_is_classified_without_being_opened(env):
    data = env[0]
    store = S.initialize(data, ingress=oi.local_suspension_control("r0"))
    store.trip(store.capture(S.OWNER_SCOPE), trigger=S.Trigger.BREAKER_TRIPPED, evidence={})
    store.close()
    before = _tree(data / "memory")
    report = inv.inventory(data)
    assert _tree(data / "memory") == before
    classes = _classes(report)
    assert classes["memory/suspension.db"] == inv.C1_PROTECTED
    assert classes["memory/soul_identity.json"] == inv.C1_PROTECTED
    assert report.no_known_blockers, report.blockers


def test_generation_blobs_block_and_are_absent_from_a_real_export(env):
    data = env[0]
    blob = data / "artifacts" / "generation" / "ab" / "abcd.png"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"synthetic generation bytes")
    report = inv.inventory(data)
    # S3: the family is captured (export 2.2); per-file completeness is the
    # reference check's job. A blob with no database table behind it is an
    # unknown state there, so the export says so and blocks, never "complete".
    assert _classes(report)["artifacts/generation"] == inv.CAPTURED
    assert report.no_known_blockers, report.blockers
    manifest = _export(env)
    assert not any("generation" in m["archive_path"] for m in manifest["members"])
    assert manifest["export_observations"]["generation_artifacts"]["status"] == "unavailable"
    assert manifest["coverage"]["generation_artifacts"]["status"] == "unavailable"
    assert inv.parity(report, manifest)["parity"] is True


@pytest.mark.parametrize("rel", ["mystery.db", "memory/notes.txt", "telemetry/old.db",
                                 "projects/demo/extra.json", "custom_tools/readme.md"])
def test_unknown_files_are_undeclared_blockers(env, rel):
    path = env[0] / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    report = inv.inventory(env[0])
    assert _classes(report)[rel] == inv.UNDECLARED
    assert rel in report.blockers


@pytest.mark.parametrize("rel", ["somedir", "memory/subdir"])
def test_unknown_directories_are_undeclared_and_not_walked(env, rel):
    (env[0] / rel / "deep").mkdir(parents=True)
    report = inv.inventory(env[0])
    assert _classes(report)[rel] == inv.UNDECLARED
    assert not any(e.relpath.startswith(rel + "/") for e in report.entries)


def test_known_uncaptured_families_block(env):
    for rel in ("restore_history/x/receipt.json", "chrome_companion/install.json"):
        path = env[0] / rel
        path.parent.mkdir(parents=True)
        path.write_text("{}")
    report = inv.inventory(env[0])
    # S2: chrome_companion is now excluded by owner decision D1, not blocking.
    assert set(report.blockers) == {"restore_history"}
    assert _classes(report)["chrome_companion"] == inv.EXCLUDED_DECLARED


def test_non_blocking_families(env):
    data = env[0]
    (data / "skills" / "official" / "s").mkdir(parents=True)
    (data / "worktrees" / "w").mkdir(parents=True)
    (data / "memory" / "prefs.json.123.456.tmp").write_text("{}")
    (data / "credentials.json").write_text("{}")
    (data / "custom_tools" / "__pycache__").mkdir()
    (data / "custom_tools" / "__pycache__" / "my_tool.cpython-313.pyc").write_bytes(b"pyc")
    report = inv.inventory(data)
    classes = _classes(report)
    assert classes["skills/official"] == inv.REGENERATED
    assert classes["worktrees"] == inv.EPHEMERAL
    assert classes["memory/prefs.json.123.456.tmp"] == inv.EPHEMERAL
    assert classes["credentials.json"] == inv.EXCLUDED
    assert classes["custom_tools/__pycache__"] == inv.REGENERATED
    assert report.no_known_blockers, report.blockers


@pytest.mark.parametrize("rel", inv.REQUIRED)
def test_missing_required_source_blocks(env, rel):
    (env[0] / rel).unlink()
    report = inv.inventory(env[0])
    assert "missing_required:" + rel in report.blockers


def test_links_are_unsafe_and_never_followed(env, tmp_path):
    data = env[0]
    outside = tmp_path / "outside"
    (outside / "secret").mkdir(parents=True)
    (outside / "secret" / "x.py").write_text("x")
    os.symlink(outside, data / "custom_tools" / "linked")
    os.symlink(outside, data / "artifacts")
    report = inv.inventory(data)
    classes = _classes(report)
    assert classes["custom_tools/linked"] == inv.UNSAFE
    assert classes["artifacts"] == inv.UNSAFE
    assert {"custom_tools/linked", "artifacts"} <= set(report.blockers)
    assert not any("secret" in e.relpath for e in report.entries)


@pytest.mark.parametrize("rel", ["worktrees", "skills/official", "artifacts/generation"])
def test_a_link_at_a_named_subtree_is_unsafe(env, tmp_path, rel):
    outside = tmp_path / "outside_subtree"
    outside.mkdir()
    link = env[0] / rel
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(outside, link)
    report = inv.inventory(env[0])
    assert _classes(report)[rel] == inv.UNSAFE
    assert rel in report.blockers


def test_linked_primary_database_is_unsafe_and_counts_as_missing(env, tmp_path):
    db = env[0] / "memory" / "lumina.db"
    real = tmp_path / "elsewhere.db"
    db.rename(real)
    os.symlink(real, db)
    report = inv.inventory(env[0])
    assert _classes(report)["memory/lumina.db"] == inv.UNSAFE
    assert "missing_required:memory/lumina.db" in report.blockers


def test_parity_reports_both_directions(env):
    report = inv.inventory(env[0])
    manifest = _export(env)
    dropped = dict(manifest, members=[m for m in manifest["members"]
                                      if m["archive_path"] != "state/audit/tool_audit.log"])
    assert inv.parity(report, dropped) == {"missing_from_archive": ["state/audit/tool_audit.log"],
                                           "unexpected_in_archive": [], "parity": False}
    extra = dict(manifest, members=manifest["members"] + [{"archive_path": "state/forged/x.db"}])
    assert inv.parity(report, extra) == {"missing_from_archive": [],
                                         "unexpected_in_archive": ["state/forged/x.db"], "parity": False}


def test_unobservable_or_unbounded_roots_refuse(env, tmp_path, monkeypatch):
    with pytest.raises(inv.InventoryUnavailable):
        inv.inventory(tmp_path / "absent")
    os.symlink(env[0], tmp_path / "linked_root")
    with pytest.raises(inv.InventoryUnavailable):
        inv.inventory(tmp_path / "linked_root")
    monkeypatch.setattr(inv, "MAX_ENTRIES", 3)
    with pytest.raises(inv.InventoryUnavailable):
        inv.inventory(env[0])


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unreadable_directory_refuses(env):
    target = env[0] / "projects" / "demo"
    target.chmod(0)
    try:
        with pytest.raises(inv.InventoryUnavailable):
            inv.inventory(env[0])
    finally:
        target.chmod(0o700)


def test_inventory_never_opens_sqlite(env, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("inventory opened a database")
    monkeypatch.setattr(sqlite3, "connect", refuse)
    import builtins
    real_open = builtins.open
    monkeypatch.setattr(builtins, "open", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("inventory opened a file")))
    try:
        inv.inventory(env[0])
    finally:
        monkeypatch.setattr(builtins, "open", real_open)


# --- Review round 1 (Sonnet HOLD) regressions -------------------------------

def test_inventory_opens_only_directories_without_following(env, monkeypatch):
    real_open = os.open
    seen = []

    def guarded(path, flags, *a, **k):
        seen.append(flags)
        assert flags & os.O_DIRECTORY and flags & os.O_NOFOLLOW, "opened a non-directory"
        return real_open(path, flags, *a, **k)
    monkeypatch.setattr(os, "open", guarded)
    inv.inventory(env[0])
    assert seen


def test_custom_tools_git_is_not_laundered_as_captured(env):
    hook = env[0] / "custom_tools" / ".git" / "hooks" / "hook.py"
    hook.parent.mkdir(parents=True)
    hook.write_text("x")
    report = inv.inventory(env[0])
    assert _classes(report)["custom_tools/.git"] == inv.UNCAPTURED_KNOWN
    assert "custom_tools/.git" in report.blockers
    assert not any(e.relpath.startswith("custom_tools/.git/") for e in report.entries)
    manifest = _export(env)
    assert not any(".git" in m["archive_path"] for m in manifest["members"])
    assert inv.parity(report, manifest)["parity"] is True


def test_directory_swapped_for_a_link_mid_walk_is_refused(env, tmp_path, monkeypatch):
    outside = tmp_path / "outside_swap"
    outside.mkdir()
    (outside / "x.py").write_text("x")
    target = env[0] / "custom_tools"
    real_lstat = os.lstat

    def swapping(path, *a, **k):
        st = real_lstat(path, *a, **k)
        if path == "custom_tools" and k.get("dir_fd") is not None:
            target.rename(tmp_path / "moved_tools")
            os.symlink(outside, target)
        return st
    monkeypatch.setattr(os, "lstat", swapping)
    with pytest.raises(inv.InventoryUnavailable):
        inv.inventory(env[0])


def test_directory_replaced_by_another_directory_mid_walk_is_refused(env, tmp_path, monkeypatch):
    target = env[0] / "projects"
    real_lstat = os.lstat

    def swapping(path, *a, **k):
        st = real_lstat(path, *a, **k)
        if path == "projects" and k.get("dir_fd") is not None:
            target.rename(tmp_path / "moved_projects")
            target.mkdir()
        return st
    monkeypatch.setattr(os, "lstat", swapping)
    with pytest.raises(inv.InventoryUnavailable, match="changed during"):
        inv.inventory(env[0])


def test_parity_holds_when_the_marker_source_is_absent(env):
    (env[0] / "knowledge_packs_installed.json").unlink()
    report = inv.inventory(env[0])
    assert report.no_known_blockers, report.blockers
    assert inv.parity(report, _export(env)) == {
        "missing_from_archive": [], "unexpected_in_archive": [], "parity": True}


@pytest.mark.parametrize("rel", ["memory/prefs.json.bak", "memory/prefs.json.1.tmp",
                                 "memory/lumina.db.bak", "memory/ledger.db-old"])
def test_near_miss_names_are_undeclared(env, rel):
    (env[0] / rel).write_text("x")
    report = inv.inventory(env[0])
    assert _classes(report)[rel] == inv.UNDECLARED
    assert rel in report.blockers


@pytest.mark.parametrize("rel", ["projects/demo/sub", "telemetry/archive"])
def test_nested_directories_outside_the_contract_block(env, rel):
    (env[0] / rel).mkdir()
    (env[0] / rel / "binding.json").write_text("{}")
    report = inv.inventory(env[0])
    assert _classes(report)[rel] == inv.UNDECLARED
    assert rel in report.blockers
    assert not any(e.relpath.startswith(rel + "/") for e in report.entries)


@pytest.mark.parametrize("rel,cls", [
    ("memory/lumina.db-wal", inv.CAPTURED), ("memory/lumina.db-journal", inv.CAPTURED),
    ("telemetry/flight_recorder.db-wal", inv.CAPTURED), ("telemetry/flight_recorder.db-shm", inv.CAPTURED),
    ("memory/ledger.db-wal", inv.REGENERATED), ("memory/ledger.db-journal", inv.REGENERATED)])
def test_sqlite_sidecars(env, rel, cls):
    (env[0] / rel).write_bytes(b"")
    report = inv.inventory(env[0])
    assert _classes(report)[rel] == cls
    assert report.no_known_blockers, report.blockers


def test_depth_bound_refuses(env, monkeypatch):
    deep = env[0] / "custom_tools" / "a" / "b"
    deep.mkdir(parents=True)
    (deep / "t.py").write_text("x")
    monkeypatch.setattr(inv, "MAX_DEPTH", 2)
    with pytest.raises(inv.InventoryUnavailable, match="bounds"):
        inv.inventory(env[0])


@pytest.mark.parametrize("rel", ["worktrees", "skills/official"])
def test_special_file_at_a_named_subtree_is_unsafe(env, rel):
    path = env[0] / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(path)
    report = inv.inventory(env[0])
    assert _classes(report)[rel] == inv.UNSAFE
    assert rel in report.blockers


@pytest.mark.parametrize("rel", inv.REQUIRED)
def test_empty_required_source_blocks(env, rel):
    (env[0] / rel).write_bytes(b"")
    report = inv.inventory(env[0])
    assert "empty_required:" + rel in report.blockers
    assert not report.no_known_blockers


def test_scan_failure_refuses(env, monkeypatch):
    real_scandir = os.scandir

    def failing(target):
        if isinstance(target, int) and os.path.basename(os.readlink(f"/proc/self/fd/{target}")) == "demo":
            raise PermissionError("synthetic scan failure")
        return real_scandir(target)
    monkeypatch.setattr(os, "scandir", failing)
    with pytest.raises(inv.InventoryUnavailable, match="projects/demo"):
        inv.inventory(env[0])


@pytest.mark.parametrize("rel", ["projects/demo/credentials.json", "custom_tools/credentials.json",
                                 "memory/credentials.json"])
def test_credentials_name_is_exempt_only_at_the_root(env, rel):
    (env[0] / rel).write_text("{}")
    report = inv.inventory(env[0])
    assert _classes(report)[rel] == inv.UNDECLARED
    assert rel in report.blockers


# --- R3 (owner GO 2026-11-18: exact root, no link anywhere on the path) ------

def _track_fds(monkeypatch):
    real_open, real_close = os.open, os.close
    opened, closed = [], []

    def tracking_open(*a, **k):
        fd = real_open(*a, **k)
        opened.append(fd)
        return fd

    def tracking_close(fd):
        closed.append(fd)
        return real_close(fd)
    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "close", tracking_close)
    return opened, closed


@pytest.mark.parametrize("suffix", ["/", "//", "/.", "/./"])
def test_root_with_a_trailing_slash_or_dot_is_refused(env, suffix):
    with pytest.raises(inv.InventoryUnavailable, match="exact absolute path"):
        inv.inventory(str(env[0]) + suffix)


def test_trailing_slash_does_not_launder_a_linked_root(env, tmp_path, monkeypatch):
    link = tmp_path / "linked_root"
    os.symlink(env[0], link)
    opened, _closed = _track_fds(monkeypatch)
    with pytest.raises(inv.InventoryUnavailable):
        inv.inventory(str(link) + "/")
    assert opened == [], "a refused root path was opened"


def test_a_root_under_a_linked_ancestor_is_refused_without_walking(env, tmp_path, monkeypatch):
    parent_link = tmp_path / "parent_link"
    os.symlink(env[0].parent, parent_link)
    via_link = parent_link / env[0].name
    assert via_link.is_dir()
    scanned = []
    real_scandir = os.scandir
    monkeypatch.setattr(os, "scandir", lambda fd: scanned.append(fd) or real_scandir(fd))
    with pytest.raises(inv.InventoryUnavailable, match="link"):
        inv.inventory(via_link)
    assert scanned == []


@pytest.mark.parametrize("bad", ["relative/data", "/a/../b", "//leading/double", ""])
def test_inexact_root_paths_are_refused(bad):
    with pytest.raises(inv.InventoryUnavailable):
        inv.inventory(bad)


def test_bytes_root_is_refused(env):
    with pytest.raises(inv.InventoryUnavailable):
        inv.inventory(os.fsencode(str(env[0])))


def test_exact_root_still_inventories(env):
    report = inv.inventory(str(env[0]))
    assert report.no_known_blockers, report.blockers


def test_fstat_failure_mid_walk_refuses_and_closes_the_descriptor(env, monkeypatch):
    opened, closed = _track_fds(monkeypatch)
    real_fstat = os.fstat

    def failing(fd):
        if len(opened) > 1 + len(Path(env[0]).parts):   # past the root descent
            raise PermissionError("synthetic fstat failure")
        return real_fstat(fd)
    monkeypatch.setattr(os, "fstat", failing)
    with pytest.raises(inv.InventoryUnavailable, match="PermissionError"):
        inv.inventory(env[0])
    assert sorted(opened) == sorted(closed), "a descriptor leaked"


def test_no_descriptor_leaks_on_success_or_refusal(env, tmp_path, monkeypatch):
    opened, closed = _track_fds(monkeypatch)
    inv.inventory(env[0])
    with pytest.raises(inv.InventoryUnavailable):
        inv.inventory(tmp_path / "absent")
    assert opened and sorted(opened) == sorted(closed)


def test_leading_double_slash_on_a_real_root_is_refused(env):
    with pytest.raises(inv.InventoryUnavailable, match="exact absolute path"):
        inv.inventory("/" + str(env[0]))


# --- R4: F1, a NUL byte or a ValueError in the descent ----------------------

def test_r4_a_nul_byte_in_the_root_is_refused_before_anything_opens(env, monkeypatch):
    opened, closed = _track_fds(monkeypatch)
    for bad in (str(env[0]) + "\0x", "/tmp/a\0b"):
        with pytest.raises(inv.InventoryUnavailable, match="NUL"):
            inv.inventory(bad)
    assert opened == [] and closed == []


def test_r4_a_value_error_mid_descent_closes_the_descriptor(env, monkeypatch):
    opened, closed = _track_fds(monkeypatch)
    tracked_open = os.open
    calls = []

    def flaky(*a, **k):
        calls.append(a[0])
        if len(calls) == 3:
            raise ValueError("synthetic")
        return tracked_open(*a, **k)
    monkeypatch.setattr(os, "open", flaky)
    with pytest.raises(inv.InventoryUnavailable, match="ValueError"):
        inv.inventory(env[0])
    assert opened and sorted(opened) == sorted(closed), "a descriptor leaked"
