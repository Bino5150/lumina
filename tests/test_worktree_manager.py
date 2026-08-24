"""CODING-07A1 internal worktree kernel tests.

All Git mutations are confined to disposable repositories.  The production
Lumina repositories and real DATA_DIR are never used.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import ntpath
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading

import pytest

from core import emergency_stop, process_manager
import core.worktree_manager as wm
from process_test_helpers import wait_for_path


def _git(cwd, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=check,
    )


def _repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.name", "Lumina Test")
    _git(path, "config", "user.email", "lumina-test@example.invalid")
    (path / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-qm", "initial")
    return path


def _write_hook(repo: Path, source: str):
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!{sys.executable}\n{source}\n", encoding="utf-8")
    hook.chmod(0o755)
    return hook


@pytest.fixture(autouse=True)
def _isolated_kernel(tmp_path, monkeypatch):
    process_manager._reset_for_tests()
    emergency_stop._reset_for_tests()
    wm._reset_for_tests()
    monkeypatch.setattr(wm.config, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(wm, "_PROTECTED_ENGINEERING_ROOTS", frozenset())
    yield
    process_manager._reset_for_tests()
    wm._reset_for_tests()
    if emergency_stop.is_latched() and emergency_stop.can_rearm():
        emergency_stop.rearm_local()
    emergency_stop._reset_for_tests()


def test_porcelain_parser_preserves_state_reasons_and_unknown_attributes(tmp_path):
    first = tmp_path / "first path"
    second = tmp_path / "second"
    payload = (
        f"worktree {first}\0"
        "HEAD 0123456789012345678901234567890123456789\0"
        "detached\0locked maintenance window\0"
        "future-key future value\0future-flag\0\0"
        f"worktree {second}\0"
        "HEAD abcdefabcdefabcdefabcdefabcdefabcdefabcd\0"
        "branch refs/heads/topic\0prunable gitdir file points nowhere\0\0"
    )

    entries = wm.parse_worktree_porcelain(payload)

    assert len(entries) == 2
    assert entries[0].canonical_path == os.path.realpath(first)
    assert entries[0].detached is True
    assert entries[0].branch is None
    assert entries[0].locked is True
    assert entries[0].locked_reason == "maintenance window"
    assert entries[0].unknown_attributes == (
        wm.UnknownPorcelainAttribute("future-key", "future value"),
        wm.UnknownPorcelainAttribute("future-flag", None),
    )
    assert entries[1].branch == "refs/heads/topic"
    assert entries[1].prunable is True
    assert entries[1].prunable_reason == "gitdir file points nowhere"


def test_create_real_worktree_returns_frozen_verified_handle(tmp_path):
    repo = _repo(tmp_path / "repo")
    expected = _git(repo, "rev-parse", "HEAD").stdout.strip()

    result = wm.create_worktree(str(repo), "HEAD")

    assert result.status == "created"
    assert result.process_status == "exited"
    assert result.returncode == 0
    assert result.handle is not None
    assert result.handle.worktree_id.startswith("wt-")
    assert result.handle.source_identity.kind == "git"
    assert result.handle.source_identity.canonical_root == os.path.realpath(repo)
    assert result.handle.worktree_root == os.path.realpath(result.target_path)
    assert result.handle.branch == result.branch
    assert result.handle.base_commit == expected == result.resolved_base_commit
    assert result.ledger_entry is not None
    assert result.ledger_entry.head == expected
    assert result.ledger_entry.branch == f"refs/heads/{result.branch}"
    assert result.identity_error is None
    assert _git(result.target_path, "rev-parse", "HEAD").stdout.strip() == expected
    with pytest.raises(FrozenInstanceError):
        result.handle.branch = "changed"


def test_source_and_target_protected_roots_are_refused_before_mutation(tmp_path, monkeypatch):
    protected_source = _repo(tmp_path / "protected-source")
    monkeypatch.setattr(
        wm, "_PROTECTED_ENGINEERING_ROOTS",
        frozenset({os.path.realpath(protected_source)}),
    )
    with pytest.raises(wm.ProtectedRootRefused, match="source"):
        wm.create_worktree(str(protected_source), "HEAD")
    assert _git(protected_source, "worktree", "list", "--porcelain").stdout.count("worktree ") == 1

    ordinary = _repo(tmp_path / "ordinary")
    protected_target = tmp_path / "protected-target"
    protected_target.mkdir()
    monkeypatch.setattr(
        wm, "_PROTECTED_ENGINEERING_ROOTS",
        frozenset({os.path.realpath(protected_target)}),
    )
    monkeypatch.setattr(wm.config, "DATA_DIR", str(protected_target / "runtime"))
    with pytest.raises(wm.ProtectedRootRefused, match="target area"):
        wm.create_worktree(str(ordinary), "HEAD")
    assert not (protected_target / "runtime" / "worktrees").exists()
    assert _git(ordinary, "branch", "--list", "lumina/*").stdout == ""


def test_captured_base_sha_not_later_moving_ref_is_passed_to_git(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    captured = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "branch", "moving", captured)
    (repo / "tracked.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "second")
    moved_to = _git(repo, "rev-parse", "HEAD").stdout.strip()
    seen_argv = []
    real_launch = process_manager.launch_argv

    def move_ref_then_launch(argv, **kwargs):
        seen_argv.append(list(argv))
        _git(repo, "branch", "-f", "moving", moved_to)
        return real_launch(argv, **kwargs)

    monkeypatch.setattr(wm.process_manager, "launch_argv", move_ref_then_launch)
    result = wm.create_worktree(str(repo), "moving")

    assert result.status == "created"
    assert result.resolved_base_commit == captured
    assert seen_argv[0][-1] == captured
    assert seen_argv[0][-1] not in {"HEAD", "moving"}
    assert _git(result.target_path, "rev-parse", "HEAD").stdout.strip() == captured
    assert _git(repo, "rev-parse", "moving").stdout.strip() == moved_to


def test_handle_is_not_registered_until_live_post_create_verification(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    verification_calls = []

    def reject_after_check(observation, **kwargs):
        verification_calls.append(True)
        assert wm._registry == {}
        assert observation.ledger_entry is not None
        assert observation.identity is not None
        return "injected post-create verification failure"

    monkeypatch.setattr(wm, "_verification_error", reject_after_check)
    result = wm.create_worktree(str(repo), "HEAD")

    assert verification_calls == [True]
    assert result.status == "verification_failed"
    assert result.handle is None
    assert wm.list_managed_worktrees() == ()
    assert result.target_exists is True
    assert result.ledger_entry is not None


def test_post_create_identity_failure_prevents_registration(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    real_resolve = wm.coding_checkpoint.resolve_target_identity

    def fail_new_target(path):
        canonical = os.path.realpath(path)
        if f"{os.sep}worktrees{os.sep}wt-" in canonical:
            raise RuntimeError("identity probe injected failure")
        return real_resolve(path)

    monkeypatch.setattr(wm.coding_checkpoint, "resolve_target_identity", fail_new_target)
    result = wm.create_worktree(str(repo), "HEAD")

    assert result.status == "verification_failed"
    assert result.handle is None
    assert "identity probe injected failure" in result.identity_error
    assert result.ledger_entry is not None
    assert wm.list_managed_worktrees() == ()


def test_concurrent_creation_allocates_distinct_ids_paths_and_branches(tmp_path, monkeypatch):
    first_repo = _repo(tmp_path / "first-repo")
    second_repo = _repo(tmp_path / "second-repo")
    values = iter(("a" * 24, "a" * 24, "b" * 24, "c" * 24))
    values_lock = threading.Lock()
    real_token_hex = wm.secrets.token_hex

    def colliding_tokens(size):
        if size != 12:
            return real_token_hex(size)
        with values_lock:
            return next(values)

    monkeypatch.setattr(wm.secrets, "token_hex", colliding_tokens)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(
            lambda repo: wm.create_worktree(str(repo), "HEAD"),
            (first_repo, second_repo),
        ))

    assert {result.status for result in results} == {"created"}
    assert len({result.handle.worktree_id for result in results}) == 2
    assert len({result.target_path for result in results}) == 2
    assert len({result.branch for result in results}) == 2
    assert all(Path(result.target_path).is_dir() for result in results)


def test_listing_requeries_git_marks_locked_stale_and_missing_and_never_adopts(tmp_path):
    repos = [_repo(tmp_path / f"repo-{index}") for index in range(3)]
    created = [wm.create_worktree(str(repo), "HEAD") for repo in repos]
    assert all(result.status == "created" for result in created)

    _git(repos[0], "worktree", "lock", "--reason", "external lock", created[0].target_path)
    _git(created[1].target_path, "checkout", "-qb", "externally-altered")
    shutil.rmtree(created[2].target_path)
    unrelated = tmp_path / "unrelated"
    _git(repos[0], "worktree", "add", "-q", "-b", "unrelated", str(unrelated))

    statuses = {item.handle.worktree_id: item for item in wm.list_managed_worktrees()}

    assert len(statuses) == 3
    assert statuses[created[0].handle.worktree_id].state == "locked"
    assert statuses[created[0].handle.worktree_id].ledger_entry.locked_reason == "external lock"
    assert statuses[created[1].handle.worktree_id].state == "stale"
    assert statuses[created[2].handle.worktree_id].state in {"externally_missing", "prunable"}
    assert all(item.handle.worktree_root != os.path.realpath(unrelated) for item in statuses.values())


def test_listing_does_not_trust_registry_without_fresh_git_query(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    created = wm.create_worktree(str(repo), "HEAD")
    calls = []
    real_read = wm._read_worktree_ledger

    def observed(source_root):
        calls.append(source_root)
        return real_read(source_root)

    monkeypatch.setattr(wm, "_read_worktree_ledger", observed)
    shutil.rmtree(created.target_path)
    status = wm.list_managed_worktrees()[0]

    assert calls == [os.path.realpath(repo)]
    assert status.state in {"externally_missing", "prunable"}
    assert status.state != "live"


def test_creation_uses_internal_managed_argv_and_runs_normal_hook(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    marker = tmp_path / "hook-ran"
    _write_hook(
        repo,
        "from pathlib import Path; "
        f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')",
    )
    launches = []
    real_launch = process_manager.launch_argv

    def observed_launch(argv, **kwargs):
        launches.append((list(argv), kwargs.copy()))
        return real_launch(argv, **kwargs)

    monkeypatch.setattr(wm.process_manager, "launch_argv", observed_launch)
    result = wm.create_worktree(str(repo), "HEAD")

    assert result.status == "created"
    assert marker.read_text(encoding="utf-8") == "ran"
    assert len(launches) == 1
    argv, kwargs = launches[0]
    assert argv[:4] == ["git", "worktree", "add", "-b"]
    assert argv[-1] == result.resolved_base_commit
    assert kwargs["visibility"] == "internal"
    assert kwargs["provenance"]["kind"] == "worktree_create"


@pytest.mark.parametrize(
    ("mode", "expected_status"),
    (("timeout", "timed_out"), ("cancel", "cancelled")),
)
def test_timeout_and_cancellation_reobserve_partial_git_reality(
    tmp_path, mode, expected_status,
):
    repo = _repo(tmp_path / "repo")
    marker = tmp_path / "hook-started"
    _write_hook(
        repo,
        "import time; from pathlib import Path; "
        f"Path({str(marker)!r}).write_text('started', encoding='utf-8'); time.sleep(30)",
    )
    cancel_event = threading.Event()
    if mode == "cancel":
        cancel_event.set()

    result = wm.create_worktree(
        str(repo), "HEAD", timeout=0.1 if mode == "timeout" else 30,
        cancel_event=cancel_event,
    )

    assert result.status == expected_status
    assert result.handle is None
    assert result.ledger_error is None
    if mode == "timeout":
        assert result.target_exists is True
        assert result.ledger_entry is not None
    else:
        # A pre-set cancellation may win before Git creates anything.  The
        # truthful observation is absence, not an invented partial target.
        assert result.target_exists is False
        assert result.ledger_entry is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group topology proof")
def test_emergency_kills_hook_inside_same_managed_process_tree(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    marker = tmp_path / "hook-pgid"
    _write_hook(
        repo,
        "import os, time; from pathlib import Path; "
        f"Path({str(marker)!r}).write_text(str(os.getpgrp()), encoding='utf-8'); time.sleep(30)",
    )
    controls = []
    real_launch = process_manager.launch_argv

    def capture_control(argv, **kwargs):
        process_id = real_launch(argv, **kwargs)
        controls.append(process_manager._get_internal_job_snapshot(process_id)["control_metadata"])
        return process_id

    monkeypatch.setattr(wm.process_manager, "launch_argv", capture_control)
    holder = {}
    thread = threading.Thread(
        target=lambda: holder.setdefault("result", wm.create_worktree(str(repo), "HEAD")),
    )
    thread.start()
    assert wait_for_path(marker)

    emergency_stop.latch(source="test", reason="worktree hook boundary")
    assert process_manager.emergency_kill_all() >= 1
    thread.join(timeout=10)

    assert not thread.is_alive()
    result = holder["result"]
    assert result.status == "emergency_killed"
    assert result.handle is None
    assert int(marker.read_text(encoding="utf-8")) == controls[0]["pgid"]
    assert result.target_exists is True
    assert result.ledger_entry is not None


def test_nonzero_hook_exit_returns_truthful_git_failure_observation(tmp_path):
    repo = _repo(tmp_path / "repo")
    _write_hook(repo, "raise SystemExit(7)")

    result = wm.create_worktree(str(repo), "HEAD")

    assert result.status == "git_failed"
    assert result.process_status == "exited"
    assert result.returncode != 0
    assert result.handle is None
    assert result.target_exists is True
    assert result.ledger_entry is not None
    assert wm.list_managed_worktrees() == ()


# ── CODING-07A2: process lifetime + safe removal ────────────────────────

def _sleeping_argv(seconds=30):
    return [sys.executable, "-u", "-c", "import time; time.sleep(float(__import__('sys').argv[1]))", str(seconds)]


def _stop_all_jobs():
    process_manager.shutdown_all(wait_seconds=5)


def test_rooted_job_path_matching_is_canonical_and_cross_platform(tmp_path):
    root = tmp_path / "root"
    descendant = root / "child"
    sibling = tmp_path / "root-sibling"
    root.mkdir()
    descendant.mkdir()
    sibling.mkdir()

    canonical_root = process_manager._canonical_managed_path(str(root))
    assert process_manager._same_or_descendant(canonical_root, canonical_root)
    assert process_manager._same_or_descendant(
        process_manager._canonical_managed_path(str(descendant)), canonical_root,
    )
    assert not process_manager._same_or_descendant(
        process_manager._canonical_managed_path(str(sibling)), canonical_root,
    )

    win_root = process_manager._canonical_managed_path(r"C:\Work\Tree", ntpath)
    win_child = process_manager._canonical_managed_path(r"c:\work\tree\Sub", ntpath)
    win_sibling = process_manager._canonical_managed_path(r"C:\Work\Tree-Other", ntpath)
    other_drive = process_manager._canonical_managed_path(r"D:\Work\Tree", ntpath)
    assert process_manager._same_or_descendant(win_child, win_root, ntpath)
    assert not process_manager._same_or_descendant(win_sibling, win_root, ntpath)
    assert not process_manager._same_or_descendant(other_drive, win_root, ntpath)


def test_jobs_rooted_under_includes_model_and_internal_jobs_and_excludes_sibling(tmp_path):
    root = tmp_path / "root"
    child = root / "child"
    sibling = tmp_path / "sibling"
    child.mkdir(parents=True)
    sibling.mkdir()
    model_id = process_manager.launch("sleep 30", cwd=str(root))
    internal_id = process_manager.launch_argv(
        _sleeping_argv(), cwd=str(child), visibility="internal",
    )
    sibling_id = process_manager.launch_argv(
        _sleeping_argv(), cwd=str(sibling), visibility="internal",
    )
    try:
        jobs = {job["process_id"]: job for job in process_manager._jobs_rooted_under(str(root))}
        assert set(jobs) == {model_id, internal_id}
        assert jobs[model_id]["visibility"] == "model"
        assert jobs[internal_id]["visibility"] == "internal"
        assert sibling_id not in jobs
    finally:
        _stop_all_jobs()


def test_rooted_job_uses_captured_cwd_when_symlink_is_retargeted(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    link = tmp_path / "current"
    link.symlink_to(first, target_is_directory=True)
    process_id = process_manager.launch_argv(
        _sleeping_argv(), cwd=str(link), visibility="internal",
    )
    try:
        link.unlink()
        link.symlink_to(second, target_is_directory=True)
        first_ids = {job["process_id"] for job in process_manager._jobs_rooted_under(str(first))}
        second_ids = {job["process_id"] for job in process_manager._jobs_rooted_under(str(second))}
        assert process_id in first_ids
        assert process_id not in second_ids
    finally:
        _stop_all_jobs()


def test_remove_clean_worktree_uses_managed_argv_forgets_handle_and_keeps_branch(
    tmp_path, monkeypatch,
):
    repo = _repo(tmp_path / "repo")
    created = wm.create_worktree(str(repo), "HEAD")
    launches = []
    real_launch = process_manager.launch_argv

    def observed_launch(argv, **kwargs):
        launches.append((list(argv), kwargs.copy()))
        return real_launch(argv, **kwargs)

    monkeypatch.setattr(wm.process_manager, "launch_argv", observed_launch)
    result = wm.remove_worktree(created.handle.worktree_id)

    assert result.status == "removed"
    assert result.process_status == "exited"
    assert result.returncode == 0
    assert result.target_exists is False
    assert result.ledger_entry is None
    assert result.identity_error is None
    assert wm.list_managed_worktrees() == ()
    assert len(launches) == 1
    assert launches[0][0] == ["git", "worktree", "remove", created.target_path]
    assert launches[0][1]["visibility"] == "internal"
    assert launches[0][1]["provenance"]["kind"] == "worktree_remove"
    assert _git(repo, "branch", "--list", created.branch).stdout.strip()


def test_dirty_worktree_refused_by_default_and_force_only_overrides_dirty(tmp_path):
    repo = _repo(tmp_path / "repo")
    created = wm.create_worktree(str(repo), "HEAD")
    (Path(created.target_path) / "untracked.txt").write_text("dirty\n", encoding="utf-8")

    refused = wm.remove_worktree(created.handle.worktree_id)
    assert refused.status == "dirty_refused"
    assert refused.dirty is True
    assert refused.target_exists is True
    assert wm.list_managed_worktrees()[0].state == "live"

    removed = wm.remove_worktree(created.handle.worktree_id, force=True)
    assert removed.status == "removed"
    assert removed.force is True
    assert removed.dirty is True
    assert not Path(created.target_path).exists()


def test_locked_worktree_fails_closed_even_with_force(tmp_path):
    repo = _repo(tmp_path / "repo")
    created = wm.create_worktree(str(repo), "HEAD")
    _git(repo, "worktree", "lock", "--reason", "operator lock", created.target_path)

    result = wm.remove_worktree(created.handle.worktree_id, force=True)

    assert result.status == "locked_refused"
    assert result.target_exists is True
    assert result.ledger_entry.locked is True
    assert result.ledger_entry.locked_reason == "operator lock"


def test_force_never_overrides_internal_live_process_in_descendant(tmp_path):
    repo = _repo(tmp_path / "repo")
    created = wm.create_worktree(str(repo), "HEAD")
    child = Path(created.target_path) / "nested"
    child.mkdir()
    process_id = process_manager.launch_argv(
        _sleeping_argv(), cwd=str(child), visibility="internal",
    )
    try:
        result = wm.remove_worktree(created.handle.worktree_id, force=True)
        assert result.status == "live_process_refused"
        assert result.blocking_process_ids == (process_id,)
        assert result.target_exists is True
    finally:
        _stop_all_jobs()


def test_second_live_process_check_catches_job_started_after_dirty_check(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    created = wm.create_worktree(str(repo), "HEAD")
    real_blocking = wm._blocking_jobs
    calls = []
    started = []

    def inject_on_second(handle):
        calls.append(True)
        if len(calls) == 2:
            started.append(process_manager.launch_argv(
                _sleeping_argv(), cwd=handle.worktree_root, visibility="internal",
            ))
        return real_blocking(handle)

    monkeypatch.setattr(wm, "_blocking_jobs", inject_on_second)
    try:
        result = wm.remove_worktree(created.handle.worktree_id, force=True)
        assert len(calls) == 2
        assert result.status == "live_process_refused"
        assert result.blocking_process_ids == tuple(started)
        assert result.target_exists is True
    finally:
        _stop_all_jobs()


def test_second_identity_check_catches_external_branch_change(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    created = wm.create_worktree(str(repo), "HEAD")
    real_observe = wm._observe_reality
    calls = []

    def alter_before_second(source_root, target):
        calls.append(True)
        if len(calls) == 2:
            _git(target, "checkout", "-qb", "external-change")
        return real_observe(source_root, target)

    monkeypatch.setattr(wm, "_observe_reality", alter_before_second)
    result = wm.remove_worktree(created.handle.worktree_id, force=True)

    assert len(calls) == 2
    assert result.status == "identity_refused"
    assert "branch state" in result.diagnostic
    assert result.target_exists is True


def test_path_reuse_is_refused_against_captured_target_identity(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    created = wm.create_worktree(str(repo), "HEAD")
    _git(repo, "worktree", "remove", created.target_path)
    # Recreate the same path at the same branch and HEAD. Only the captured
    # target identity (including filesystem identity) distinguishes it.
    _git(repo, "worktree", "add", "-q", created.target_path, created.branch)
    launches = []
    monkeypatch.setattr(
        wm.process_manager, "launch_argv",
        lambda *args, **kwargs: launches.append((args, kwargs)),
    )

    result = wm.remove_worktree(created.handle.worktree_id, force=True)

    assert result.status == "identity_refused"
    assert "target identity differs" in result.diagnostic
    assert launches == []
    assert Path(created.target_path).exists()


def test_external_removal_is_reported_not_silently_adopted_or_repeated(tmp_path):
    repo = _repo(tmp_path / "repo")
    created = wm.create_worktree(str(repo), "HEAD")
    _git(repo, "worktree", "remove", created.target_path)

    result = wm.remove_worktree(created.handle.worktree_id)

    assert result.status == "identity_refused"
    assert result.target_exists is False
    assert result.ledger_entry is None
    statuses = wm.list_managed_worktrees()
    assert len(statuses) == 1
    assert statuses[0].state == "externally_missing"


def test_concurrent_remove_has_single_mutator(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    created = wm.create_worktree(str(repo), "HEAD")
    entered = threading.Event()
    release = threading.Event()
    real_observe = wm._observe_reality
    first = [True]

    def block_first_observation(source_root, target):
        if first[0]:
            first[0] = False
            entered.set()
            assert release.wait(5)
        return real_observe(source_root, target)

    monkeypatch.setattr(wm, "_observe_reality", block_first_observation)
    holder = {}
    thread = threading.Thread(
        target=lambda: holder.setdefault(
            "first", wm.remove_worktree(created.handle.worktree_id)
        ),
    )
    thread.start()
    assert entered.wait(5)
    second = wm.remove_worktree(created.handle.worktree_id)
    release.set()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert second.status == "removal_in_progress"
    assert holder["first"].status == "removed"


def test_zero_exit_without_removal_is_verification_failure_not_success(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    created = wm.create_worktree(str(repo), "HEAD")
    real_launch = process_manager.launch_argv

    def fake_success(argv, **kwargs):
        assert argv[:3] == ["git", "worktree", "remove"]
        return real_launch([sys.executable, "-c", "pass"], **kwargs)

    monkeypatch.setattr(wm.process_manager, "launch_argv", fake_success)
    result = wm.remove_worktree(created.handle.worktree_id)

    assert result.status == "verification_failed"
    assert result.process_status == "exited"
    assert result.returncode == 0
    assert result.target_exists is True
    assert result.ledger_entry is not None
    assert len(wm.list_managed_worktrees()) == 1


@pytest.mark.parametrize(
    ("mode", "expected"),
    (("timeout", "timed_out"), ("cancel", "cancelled")),
)
def test_remove_timeout_and_cancel_reobserve_unchanged_reality(
    tmp_path, monkeypatch, mode, expected,
):
    repo = _repo(tmp_path / "repo")
    created = wm.create_worktree(str(repo), "HEAD")
    real_launch = process_manager.launch_argv
    cancel = threading.Event()
    if mode == "cancel":
        cancel.set()

    def sleeping_remove(argv, **kwargs):
        return real_launch(_sleeping_argv(), **kwargs)

    monkeypatch.setattr(wm.process_manager, "launch_argv", sleeping_remove)
    result = wm.remove_worktree(
        created.handle.worktree_id,
        timeout=0.1 if mode == "timeout" else 30,
        cancel_event=cancel,
    )

    assert result.status == expected
    assert result.target_exists is True
    assert result.ledger_entry is not None
    assert len(wm.list_managed_worktrees()) == 1


def test_emergency_kills_managed_remove_and_reobserves_git_reality(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    created = wm.create_worktree(str(repo), "HEAD")
    marker = tmp_path / "remove-started"
    real_launch = process_manager.launch_argv

    def sleeping_remove(argv, **kwargs):
        source = (
            "import pathlib, sys, time; "
            "pathlib.Path(sys.argv[1]).write_text('started', encoding='utf-8'); "
            "time.sleep(30)"
        )
        return real_launch([sys.executable, "-u", "-c", source, str(marker)], **kwargs)

    monkeypatch.setattr(wm.process_manager, "launch_argv", sleeping_remove)
    holder = {}
    thread = threading.Thread(target=lambda: holder.setdefault(
        "result", wm.remove_worktree(created.handle.worktree_id)
    ))
    thread.start()
    assert wait_for_path(marker)
    emergency_stop.latch(source="test", reason="remove boundary")
    assert process_manager.emergency_kill_all() >= 1
    thread.join(timeout=10)

    assert not thread.is_alive()
    result = holder["result"]
    assert result.status == "emergency_killed"
    assert result.target_exists is True
    assert result.ledger_entry is not None
    assert len(wm.list_managed_worktrees()) == 1
