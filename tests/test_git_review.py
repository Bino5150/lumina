"""CODING-08A1 trusted Git review observation kernel test suite."""

import os
import subprocess
import sys
import time

import pytest

import core.coding_checkpoint as cp
import core.git_read as git_read
import core.git_review as gr


def _git(path, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=path, check=check, capture_output=True, text=True
    )


def _init_repo(path):
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "tracked.txt").write_text("line1\nline2\nline3\nline4\nline5\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "initial")


@pytest.fixture
def repo_context(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    identity = cp.resolve_target_identity(str(root))
    return root, identity


def _only(changes):
    assert len(changes) == 1, changes
    return changes[0]


def _by_path(changes, path):
    matches = [c for c in changes if c.path == path]
    assert len(matches) == 1, (path, changes)
    return matches[0]


# ===========================================================================
# 1-8: baseline staged/unstaged/untracked/mixed classification.
# ===========================================================================

def test_clean_repository_has_no_changes(repo_context):
    root, identity = repo_context
    snap = gr.capture_review_snapshot(identity)
    assert snap.changes == ()
    assert snap.head is not None
    assert snap.branch == "main"
    assert snap.detached is False
    assert snap.unborn is False
    assert snap.operation_state == ()
    assert snap.identity == identity


def test_staged_modification(repo_context):
    root, identity = repo_context
    (root / "tracked.txt").write_text("line1\nline2\nline3\nline4\nline5\nline6\n")
    _git(root, "add", "tracked.txt")
    change = _only(gr.capture_review_snapshot(identity).changes)
    assert change.record_type == "1"
    assert change.relation == "ordinary"
    assert change.xy_status == "M."
    assert change.staged is True
    assert change.unstaged is False
    assert change.staged_diff == gr.DiffMetadata(binary=False, insertions=1, deletions=0)
    assert change.unstaged_diff is None


def test_unstaged_modification(repo_context):
    root, identity = repo_context
    (root / "tracked.txt").write_text("line1\nline2\nline3\nline4\nline5\nline6\n")
    change = _only(gr.capture_review_snapshot(identity).changes)
    assert change.xy_status == ".M"
    assert change.staged is False
    assert change.unstaged is True
    assert change.staged_diff is None
    assert change.unstaged_diff == gr.DiffMetadata(binary=False, insertions=1, deletions=0)


def test_same_file_staged_then_modified_again_is_MM(repo_context):
    root, identity = repo_context
    (root / "tracked.txt").write_text("line1\nline2\nline3\nline4\nline5\nline6\n")
    _git(root, "add", "tracked.txt")
    (root / "tracked.txt").write_text("line1\nline2\nline3\nline4\nline5\nline6\nline7\n")
    change = _only(gr.capture_review_snapshot(identity).changes)
    assert change.xy_status == "MM"
    assert change.staged is True
    assert change.unstaged is True
    assert change.staged_diff == gr.DiffMetadata(binary=False, insertions=1, deletions=0)
    assert change.unstaged_diff == gr.DiffMetadata(binary=False, insertions=1, deletions=0)


def test_staged_new_file(repo_context):
    root, identity = repo_context
    (root / "new.txt").write_text("brand new\n")
    _git(root, "add", "new.txt")
    change = _only(gr.capture_review_snapshot(identity).changes)
    assert change.xy_status == "A."
    assert change.staged is True
    assert change.unstaged is False
    assert change.head_mode == "000000"
    assert change.head_object_id == "0" * 40
    assert change.index_mode == "100644"


def test_untracked_file_is_not_treated_as_unstaged_tracked_diff(repo_context):
    root, identity = repo_context
    (root / "scratch.txt").write_text("hello\n")
    change = _only(gr.capture_review_snapshot(identity).changes)
    assert change.record_type == "?"
    assert change.relation == "untracked"
    assert change.staged is False
    assert change.unstaged is False
    assert change.untracked is True


def test_staged_deletion(repo_context):
    root, identity = repo_context
    _git(root, "rm", "-q", "tracked.txt")
    change = _only(gr.capture_review_snapshot(identity).changes)
    assert change.xy_status == "D."
    assert change.staged is True
    assert change.unstaged is False
    assert change.index_mode == "000000"


def test_unstaged_deletion(repo_context):
    root, identity = repo_context
    os.remove(root / "tracked.txt")
    change = _only(gr.capture_review_snapshot(identity).changes)
    assert change.xy_status == ".D"
    assert change.staged is False
    assert change.unstaged is True


# ===========================================================================
# 9-11: rename/copy relation truth (MB-39 avoidance).
# ===========================================================================

def test_rename_is_relation_rename_not_flattened(repo_context):
    root, identity = repo_context
    _git(root, "mv", "tracked.txt", "renamed.txt")
    _git(root, "add", "-A")
    change = _only(gr.capture_review_snapshot(identity).changes)
    assert change.record_type == "2"
    assert change.relation == "rename"
    assert change.path == "renamed.txt"
    assert change.original_path == "tracked.txt"
    assert change.relation_score == 100


def test_rename_plus_unstaged_edit_keeps_layers_independent(repo_context):
    root, identity = repo_context
    _git(root, "mv", "tracked.txt", "renamed.txt")
    _git(root, "add", "-A")
    (root / "renamed.txt").write_text(
        (root / "renamed.txt").read_text() + "line6\n"
    )
    change = _only(gr.capture_review_snapshot(identity).changes)
    assert change.relation == "rename"
    assert change.staged is True
    assert change.unstaged is True
    assert change.staged_diff is not None
    assert change.unstaged_diff == gr.DiffMetadata(binary=False, insertions=1, deletions=0)


def test_real_copy_record_is_relation_copy_not_rename(repo_context):
    root, identity = repo_context
    (root / "copy.txt").write_text((root / "tracked.txt").read_text())
    (root / "tracked.txt").write_text("line1\nline2\nline3\nline4\nline5\nmodified\n")
    _git(root, "add", "-A")
    change = _by_path(gr.capture_review_snapshot(identity).changes, "copy.txt")
    assert change.record_type == "2"
    assert change.relation == "copy"
    assert change.original_path == "tracked.txt"
    assert change.relation_score == 100


# ===========================================================================
# 12-13: mode-only and symlink changes (POSIX only).
# ===========================================================================

@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX file modes only")
def test_mode_only_change_preserves_mode_and_identical_object_id(repo_context):
    root, identity = repo_context
    path = root / "tracked.txt"
    path.chmod(0o755)
    change = _only(gr.capture_review_snapshot(identity).changes)
    assert change.head_mode == "100644"
    assert change.worktree_mode == "100755"
    assert change.head_object_id == change.index_object_id


@pytest.mark.skipif(
    not hasattr(os, "symlink") or sys.platform.startswith("win"),
    reason="symlinks not supported",
)
def test_symlink_target_change_uses_symlink_mode(repo_context):
    root, identity = repo_context
    (root / "outside_a.txt").write_text("a")
    (root / "outside_b.txt").write_text("b")
    link = root / "link.txt"
    os.symlink("outside_a.txt", link)
    _git(root, "add", "link.txt")
    _git(root, "commit", "-q", "-m", "add symlink")
    os.remove(link)
    os.symlink("outside_b.txt", link)
    change = _by_path(gr.capture_review_snapshot(identity).changes, "link.txt")
    assert change.head_mode == "120000"
    assert change.worktree_mode == "120000"
    assert change.staged is False
    assert change.unstaged is True
    # Git status v2 never hashes worktree content (no hW field) -- index is
    # still the old target, so index_object_id equals head_object_id until
    # staged. Staging picks up the retargeted symlink as a real content
    # change, proving the mode/object-id plumbing is genuinely live.
    assert change.head_object_id == change.index_object_id
    _git(root, "add", "link.txt")
    staged = _by_path(gr.capture_review_snapshot(identity).changes, "link.txt")
    assert staged.head_object_id != staged.index_object_id


# ===========================================================================
# 14-15: gitlink/submodule outer-state truth.
# ===========================================================================

@pytest.fixture
def submodule_context(tmp_path):
    inner = tmp_path / "inner"
    inner.mkdir()
    _init_repo(inner)
    outer = tmp_path / "outer"
    outer.mkdir()
    _git(outer, "init", "-q", "-b", "main")
    _git(outer, "config", "user.email", "test@example.com")
    _git(outer, "config", "user.name", "Test")
    _git(outer, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(inner), "sub")
    _git(outer / "sub", "config", "user.email", "test@example.com")
    _git(outer / "sub", "config", "user.name", "Test")
    _git(outer, "commit", "-q", "-m", "add submodule")
    identity = cp.resolve_target_identity(str(outer))
    return outer, identity


def test_gitlink_submodule_outer_state_is_clean(submodule_context):
    outer, identity = submodule_context
    snap = gr.capture_review_snapshot(identity)
    assert snap.changes == ()


def test_dirty_submodule_reports_tracked_change_flag(submodule_context):
    outer, identity = submodule_context
    (outer / "sub" / "tracked.txt").write_text("mutated inside the submodule\n")
    change = _only(gr.capture_review_snapshot(identity).changes)
    assert change.submodule.is_submodule is True
    assert change.submodule.has_tracked_changes is True
    assert change.submodule.commit_changed is False
    assert change.head_mode == "160000"
    assert change.index_mode == "160000"


def test_submodule_advanced_commit_reports_commit_changed_flag(submodule_context):
    outer, identity = submodule_context
    sub = outer / "sub"
    (sub / "tracked.txt").write_text("committed inside the submodule\n")
    _git(sub, "add", "tracked.txt")
    _git(sub, "commit", "-q", "-m", "inner change")
    change = _only(gr.capture_review_snapshot(identity).changes)
    assert change.submodule.is_submodule is True
    assert change.submodule.commit_changed is True
    assert change.submodule.has_tracked_changes is False


# ===========================================================================
# 16: unmerged/conflict truth.
# ===========================================================================

@pytest.fixture
def conflict_context(tmp_path):
    root = tmp_path / "conflict"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "f.txt").write_text("base\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "checkout", "-q", "-b", "branch-a")
    (root / "f.txt").write_text("a-version\n")
    _git(root, "commit", "-q", "-am", "a")
    _git(root, "checkout", "-q", "main")
    (root / "f.txt").write_text("b-version\n")
    _git(root, "commit", "-q", "-am", "b")
    _git(root, "merge", "branch-a", "-q", check=False)
    identity = cp.resolve_target_identity(str(root))
    return root, identity


def test_unmerged_conflict_is_truthfully_distinct_from_ordinary(conflict_context):
    root, identity = conflict_context
    change = _only(gr.capture_review_snapshot(identity).changes)
    assert change.record_type == "u"
    assert change.relation == "unmerged"
    assert change.xy_status == "UU"
    assert len(change.unmerged_stages) == 3
    stages = {stage.stage: stage for stage in change.unmerged_stages}
    assert set(stages) == {1, 2, 3}
    for stage in stages.values():
        assert stage.mode == "100644"
        assert len(stage.object_id) == 40
    assert change.staged_diff is None
    assert change.unstaged_diff is None


# ===========================================================================
# 17: primary vs linked-worktree target identity.
# ===========================================================================

def test_linked_worktree_has_distinct_snapshot_identity(repo_context, tmp_path):
    root, main_identity = repo_context
    worktree = tmp_path / "linked"
    _git(root, "worktree", "add", "-q", str(worktree), "-b", "feature")
    (worktree / "wt_only.txt").write_text("in the linked worktree\n")

    worktree_identity = cp.resolve_target_identity(str(worktree))
    assert worktree_identity.target_key != main_identity.target_key

    main_snap = gr.capture_review_snapshot(main_identity)
    worktree_snap = gr.capture_review_snapshot(worktree_identity)
    assert main_snap.changes == ()
    assert _only(worktree_snap.changes).path == "wt_only.txt"
    assert worktree_snap.branch == "feature"


# ===========================================================================
# 18: repository mutation during capture -> stable retry, or fail closed.
# ===========================================================================

def test_capture_retries_once_on_transient_instability(repo_context, monkeypatch):
    root, identity = repo_context
    stable = gr._capture_changes(str(root))
    changed = stable + (
        gr.ReviewChange(
            record_type="?", relation="untracked", path="phantom.txt",
            original_path=None, relation_score=None, xy_status="??",
            staged=False, unstaged=False, untracked=True,
            submodule=gr._SUBMODULE_NONE, head_mode=None, index_mode=None,
            worktree_mode=None, head_object_id=None, index_object_id=None,
            unmerged_stages=(), staged_diff=None, unstaged_diff=None,
        ),
    )
    states = iter([stable, changed, stable, stable])
    monkeypatch.setattr(gr, "_capture_changes", lambda _root: next(states))

    snap = gr.capture_review_snapshot(identity)
    assert snap.changes == stable


def test_second_instability_raises_without_returning_a_mixed_snapshot(repo_context, monkeypatch):
    root, identity = repo_context
    stable = gr._capture_changes(str(root))
    changed = stable + (
        gr.ReviewChange(
            record_type="?", relation="untracked", path="phantom.txt",
            original_path=None, relation_score=None, xy_status="??",
            staged=False, unstaged=False, untracked=True,
            submodule=gr._SUBMODULE_NONE, head_mode=None, index_mode=None,
            worktree_mode=None, head_object_id=None, index_object_id=None,
            unmerged_stages=(), staged_diff=None, unstaged_diff=None,
        ),
    )
    states = iter([stable, changed, stable, changed])
    monkeypatch.setattr(gr, "_capture_changes", lambda _root: next(states))

    with pytest.raises(gr.UnstableReviewCapture):
        gr.capture_review_snapshot(identity)


# ===========================================================================
# 19: malformed/unexpected porcelain fails safely.
# ===========================================================================

@pytest.mark.parametrize("raw", [
    b"9 weird record\0",
    b"1 A. N... 000000 100644\0",
    b"2 R. N... 100644 100644 100644 aa bb R100 onlyone\0",
    b"u UU N... 100644 100644\0",
    b"? \0",
])
def test_malformed_status_output_fails_closed(raw):
    with pytest.raises(gr.MalformedReviewStatus):
        gr._parse_review_status(raw)


@pytest.mark.parametrize("raw", [
    b"notanumber\t0\tpath.txt\0",
    b"-1\t0\tpath.txt\0",
    b"1\t0\n",
])
def test_malformed_numstat_output_fails_closed(raw):
    with pytest.raises(gr.MalformedReviewStatus):
        gr._parse_numstat(raw)


# ===========================================================================
# 20-21: external-helper non-execution proof.
# ===========================================================================

def test_configured_diff_external_does_not_execute(repo_context, tmp_path):
    root, identity = repo_context
    sentinel = tmp_path / "diff_external_ran"
    script = tmp_path / "fake_diff.sh"
    script.write_text(f"#!/bin/sh\ntouch '{sentinel}'\nexit 0\n")
    script.chmod(0o755)
    _git(root, "config", "diff.external", str(script))

    (root / "tracked.txt").write_text("changed enough to trigger a real diff\n")
    _git(root, "add", "tracked.txt")
    (root / "tracked.txt").write_text("changed again, unstaged\n")

    gr.capture_review_snapshot(identity)

    assert not sentinel.exists(), (
        "diff.external executed during review observation -- the "
        "--no-ext-diff guard did not hold"
    )


def test_configured_textconv_does_not_execute(repo_context, tmp_path):
    root, identity = repo_context
    sentinel = tmp_path / "textconv_ran"
    script = tmp_path / "fake_textconv.sh"
    script.write_text(f"#!/bin/sh\ntouch '{sentinel}'\ncat \"$1\"\n")
    script.chmod(0o755)
    (root / ".gitattributes").write_text("tracked.txt diff=fake\n")
    _git(root, "add", ".gitattributes")
    _git(root, "commit", "-q", "-m", "attrs")
    _git(root, "config", "diff.fake.textconv", str(script))

    (root / "tracked.txt").write_text("changed enough to trigger a real diff\n")
    _git(root, "add", "tracked.txt")
    (root / "tracked.txt").write_text("changed again, unstaged\n")

    gr.capture_review_snapshot(identity)

    assert not sentinel.exists(), (
        "textconv executed during review observation -- the "
        "--no-textconv guard did not hold"
    )


def test_every_diff_invocation_argv_carries_the_safety_flags(repo_context, monkeypatch):
    """Construction-level companion to the two sentinel tests above.

    Empirically, ``git diff --numstat`` never invokes diff.external or
    textconv at all under real Git (2.43.0) -- confirmed directly: with
    both hooks configured to leave sentinel evidence, a full-patch
    ``git diff`` triggers both, but ``--numstat``/``--stat``/rename
    similarity scoring never do, in any combination tried, because none
    of those code paths ever render human-readable diff text (textconv's
    entire purpose) or hand blob content to an external program whose
    freeform output Git could parse as counts (diff.external's problem).
    That makes the executable sentinel tests real but non-load-bearing
    for *this specific* choice of Git invocation: they would still pass
    even with the flags removed, which is exactly the vacuous-test
    failure mode CODING-08A1 section 11 warns against ("not merely test
    that Git itself accepts --no-ext-diff").

    This test is the actual mutation-sensitive regression: it asserts
    every diff-shaped argv this module ever constructs literally
    contains both flags, regardless of whether the specific Git mode
    chosen happens to consult them. Removing either flag from
    _DIFF_SAFETY_ARGS turns this red immediately -- see the CODING-08A1
    handoff report's mutation-proof section for the empirical evidence
    and the byte-identical mutation/restore log.
    """
    root, identity = repo_context
    (root / "tracked.txt").write_text("line1\nline2\nline3\nline4\nline5\nline6\n")
    _git(root, "add", "tracked.txt")
    (root / "tracked.txt").write_text(
        "line1\nline2\nline3\nline4\nline5\nline6\nline7\n"
    )

    recorded = []
    real = gr.run_bounded_git

    def spy(root_arg, args, **kwargs):
        recorded.append(tuple(args))
        return real(root_arg, args, **kwargs)

    monkeypatch.setattr(gr, "run_bounded_git", spy)
    gr.capture_review_snapshot(identity)

    diff_calls = [args for args in recorded if "diff" in args]
    # capture_review_snapshot captures twice (A/B stability check), each
    # with a staged + unstaged numstat call -- exact count is an internal
    # retry-loop detail, so just require at least one of each and check all.
    assert len(diff_calls) >= 2, diff_calls
    for args in diff_calls:
        assert "--no-ext-diff" in args, args
        assert "--no-textconv" in args, args


# ===========================================================================
# 22: output/time bounding.
# ===========================================================================

def test_output_bounding_raises_on_overflow(repo_context, monkeypatch):
    root, identity = repo_context
    monkeypatch.setattr(gr, "MAX_GIT_STDOUT_BYTES", 4)
    (root / "tracked.txt").write_text("more than four bytes of change\n")
    with pytest.raises(gr.ReviewProbeError):
        gr._capture_changes(str(root))


def test_time_bounding_raises_on_timeout(repo_context, monkeypatch):
    root, identity = repo_context
    monkeypatch.setattr(gr, "GIT_TIMEOUT_SECONDS", 0.0001)
    with pytest.raises(gr.ReviewProbeError):
        gr._run_git(str(root), ["status", "--porcelain=v2", "-z"])


def test_git_read_run_bounded_git_enforces_stdout_cap(repo_context):
    root, _identity = repo_context
    with pytest.raises(git_read.GitReadError):
        git_read.run_bounded_git(
            str(root), ["config", "--list"], max_stdout_bytes=1,
        )


def test_git_read_run_bounded_git_enforces_timeout(repo_context):
    root, _identity = repo_context
    with pytest.raises(git_read.GitReadError):
        git_read.run_bounded_git(str(root), ["status"], timeout=0.0001)


# ===========================================================================
# 23: paths containing spaces.
# ===========================================================================

def test_path_with_spaces_round_trips(repo_context):
    root, identity = repo_context
    (root / "a file with spaces.txt").write_text("content\n")
    _git(root, "add", "a file with spaces.txt")
    change = _only(gr.capture_review_snapshot(identity).changes)
    assert change.path == "a file with spaces.txt"


def test_rename_with_spaces_round_trips(repo_context):
    root, identity = repo_context
    _git(root, "mv", "tracked.txt", "renamed with spaces.txt")
    _git(root, "add", "-A")
    change = _only(gr.capture_review_snapshot(identity).changes)
    assert change.path == "renamed with spaces.txt"
    assert change.original_path == "tracked.txt"


# ===========================================================================
# 24: hostile path bytes/controls as parser data.
# ===========================================================================

def test_path_with_embedded_newline_round_trips(repo_context):
    root, identity = repo_context
    name = "weird\nname.txt"
    (root / name).write_text("content\n")
    change = _only(gr.capture_review_snapshot(identity).changes)
    assert change.path == name


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX raw bytes only")
def test_path_with_non_utf8_bytes_is_parsed_without_crashing(repo_context):
    root, identity = repo_context
    raw_name = b"bad-\xff-name.txt"
    full_path = os.fsencode(str(root)) + b"/" + raw_name
    try:
        fd = os.open(full_path, os.O_CREAT | os.O_WRONLY, 0o644)
    except OSError:
        pytest.skip("filesystem rejects non-UTF8 filenames")
    os.close(fd)

    change = _only(gr.capture_review_snapshot(identity).changes)
    assert change.relation == "untracked"
    # Round-trips via surrogateescape: re-encoding the captured path with
    # the same error handler reproduces the exact original bytes.
    assert change.path.encode("utf-8", errors="surrogateescape") == raw_name


# ===========================================================================
# Architectural boundary tests.
# ===========================================================================

def test_no_tool_registration_or_qt_dependency():
    source = open(gr.__file__, encoding="utf-8").read()
    assert "registry.register" not in source
    assert "PySide6" not in source
    assert "PyQt" not in source
    assert "shell=True" not in source


def test_identity_must_be_a_target_identity_instance():
    with pytest.raises(gr.ReviewTargetError):
        gr.capture_review_snapshot("/not/an/identity")


def test_directory_kind_identity_is_refused(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    identity = cp.resolve_target_identity(str(plain))
    assert identity.kind == "directory"
    with pytest.raises(gr.ReviewTargetError):
        gr.capture_review_snapshot(identity)


def test_stale_identity_after_repository_removed_is_refused(repo_context, tmp_path):
    root, identity = repo_context
    import shutil
    shutil.rmtree(root)
    with pytest.raises(gr.ReviewTargetError):
        gr.capture_review_snapshot(identity)


def test_forged_identity_pointed_at_sibling_worktree_is_refused(repo_context, tmp_path):
    """A stale/forged identity that now points at a *sibling linked
    worktree* of the same repository (shared git_common_dir, distinct
    canonical_root) must be refused just as surely as an unrelated
    repository would be -- proving worktree identity is not collapsed to
    "same repo, close enough"."""
    root, main_identity = repo_context
    worktree = tmp_path / "linked"
    _git(root, "worktree", "add", "-q", str(worktree), "-b", "feature")
    worktree_identity = cp.resolve_target_identity(str(worktree))

    from dataclasses import replace as dc_replace
    forged = dc_replace(main_identity, canonical_root=worktree_identity.canonical_root)
    with pytest.raises(gr.ReviewTargetError):
        gr.capture_review_snapshot(forged)


def test_supplied_identity_no_longer_matching_live_root_is_refused(repo_context, tmp_path):
    root, identity = repo_context
    other = tmp_path / "other"
    other.mkdir()
    _init_repo(other)
    other_identity = cp.resolve_target_identity(str(other))
    from dataclasses import replace as dc_replace
    forged = dc_replace(identity, canonical_root=other_identity.canonical_root)
    with pytest.raises(gr.ReviewTargetError):
        gr.capture_review_snapshot(forged)


# ===========================================================================
# CODING-08R.1: core.fsmonitor and content-filter (clean/process) sentinels.
#
# diff.external/textconv (tested above) are not the only Git-supported
# external-process escape hatches a status/diff invocation can reach.
# core.fsmonitor fires on plain `status` and on `--numstat` alike (staged
# AND unstaged); a `.gitattributes`-selected filter.<name>.clean/.process
# driver fires on worktree-side `--numstat` (never on `--cached`, never on
# plain `status`). See core/git_review.py's module docstring and its
# "Filter-free content comparison" section for the full empirical record
# and the structural (not flag-based) fix for the filter vector.
# ===========================================================================

def test_configured_fsmonitor_does_not_execute_during_status(repo_context, tmp_path):
    root, identity = repo_context
    sentinel = tmp_path / "fsmonitor_ran"
    script = tmp_path / "fake_fsmonitor.sh"
    script.write_text(f"#!/bin/sh\ntouch '{sentinel}'\nexit 0\n")
    script.chmod(0o755)
    _git(root, "config", "core.fsmonitor", str(script))

    (root / "tracked.txt").write_text("dirty\n")
    gr.capture_review_snapshot(identity)

    assert not sentinel.exists(), (
        "core.fsmonitor executed during A1 status/numstat observation -- "
        "the -c core.fsmonitor= guard did not hold"
    )


def test_fsmonitor_removed_flag_would_execute_helper(repo_context, tmp_path, monkeypatch):
    """Companion proof: with the guard removed, A1's own observation path
    DOES invoke the configured fsmonitor hook."""
    root, identity = repo_context
    sentinel = tmp_path / "fsmonitor_ran"
    script = tmp_path / "fake_fsmonitor.sh"
    script.write_text(f"#!/bin/sh\ntouch '{sentinel}'\nexit 0\n")
    script.chmod(0o755)
    _git(root, "config", "core.fsmonitor", str(script))

    (root / "tracked.txt").write_text("dirty\n")
    monkeypatch.setattr(gr, "_GLOBAL_SAFE_ARGS", ("--no-pager", "-c", "color.ui=never"))
    gr.capture_review_snapshot(identity)

    assert sentinel.exists(), (
        "expected the configured core.fsmonitor hook to run once the "
        "guard was removed -- if it did not, the sentinel test above may "
        "be vacuous for this Git version"
    )


def test_configured_content_filter_clean_does_not_execute_during_unstaged_numstat(
    repo_context, tmp_path,
):
    root, identity = repo_context
    sentinel = tmp_path / "clean_ran"
    script = tmp_path / "fake_clean.sh"
    script.write_text(f"#!/bin/sh\ntouch '{sentinel}'\ncat\n")
    script.chmod(0o755)
    _git(root, "config", "filter.sentinel.clean", str(script))
    (root / ".gitattributes").write_text("tracked.txt filter=sentinel\n")
    _git(root, "add", ".gitattributes")
    _git(root, "commit", "-q", "-m", "attrs")
    # Git's own index-refresh during the setup `add`/`commit` above already
    # invokes the just-configured clean filter once, empirically confirmed
    # (CODING-08R.1) -- reset the sentinel so only OUR call under test is
    # observed, not ordinary repository setup.
    sentinel.unlink(missing_ok=True)

    (root / "tracked.txt").write_text("line1\nCHANGED\nline3\nline4\nline5\n")
    snap = gr.capture_review_snapshot(identity)
    change = _by_path(snap.changes, "tracked.txt")

    assert not sentinel.exists(), (
        "filter.clean executed during A1 unstaged numstat observation -- "
        "the filter-free comparison did not hold"
    )
    assert change.unstaged_diff == gr.DiffMetadata(binary=False, insertions=1, deletions=1)


def test_configured_content_filter_process_does_not_execute(repo_context, tmp_path):
    """The newer long-running filter-process protocol (filter.<name>.process)
    is a structurally separate configuration key from .clean/.smudge --
    confirmed empirically (CODING-08R) that Git still launches the process
    even for a required filter whose protocol handshake immediately fails."""
    root, identity = repo_context
    sentinel = tmp_path / "process_ran"
    script = tmp_path / "fake_process.sh"
    script.write_text(f"#!/bin/sh\ntouch '{sentinel}'\nexec cat >/dev/null\n")
    script.chmod(0o755)
    # Commit the attribute assignment BEFORE the "sentinel" filter driver is
    # even defined in Git config -- an unrecognized filter name is a no-op,
    # so this `add`/`commit` cannot itself invoke anything. Defining the
    # driver (with required=true) only afterward avoids Git's own
    # `git add`/`git commit` index-refresh failing at SETUP time (a required
    # filter that errors makes ordinary Git operations on that path fail,
    # independent of anything under test here).
    (root / ".gitattributes").write_text("tracked.txt filter=sentinel\n")
    _git(root, "add", ".gitattributes")
    _git(root, "commit", "-q", "-m", "attrs")
    _git(root, "config", "filter.sentinel.process", str(script))
    _git(root, "config", "filter.sentinel.required", "true")

    (root / "tracked.txt").write_text("line1\nCHANGED\nline3\nline4\nline5\n")
    gr.capture_review_snapshot(identity)

    assert not sentinel.exists(), (
        "filter.process executed during A1 observation -- the filter-free "
        "comparison did not hold"
    )


def test_filter_free_numstat_bypassed_would_execute_clean_filter(repo_context, tmp_path, monkeypatch):
    """Companion proof: with the filter-free comparison bypassed in favor
    of a direct worktree-path diff (the pre-CODING-08R.1 call shape), the
    configured clean filter DOES execute."""
    root, identity = repo_context
    sentinel = tmp_path / "clean_ran"
    script = tmp_path / "fake_clean.sh"
    script.write_text(f"#!/bin/sh\ntouch '{sentinel}'\ncat\n")
    script.chmod(0o755)
    _git(root, "config", "filter.sentinel.clean", str(script))
    (root / ".gitattributes").write_text("tracked.txt filter=sentinel\n")
    _git(root, "add", ".gitattributes")
    _git(root, "commit", "-q", "-m", "attrs")
    sentinel.unlink(missing_ok=True)  # see the first clean-filter test's comment
    (root / "tracked.txt").write_text("line1\nCHANGED\nline3\nline4\nline5\n")

    def _direct_worktree_diff(root_arg, old_bytes, new_bytes, extra_args):
        result = gr._run_git(root_arg, ["diff", *extra_args, "--", "tracked.txt"])
        return result.stdout

    monkeypatch.setattr(gr, "_filter_free_diff", _direct_worktree_diff)
    try:
        gr.capture_review_snapshot(identity)
    except gr.GitReviewError:
        # The mutated helper returns an ordinary (same-path) numstat shape,
        # not the --no-index rename shape _parse_no_index_numstat expects --
        # a parse failure is an expected side effect of this deliberately
        # crude mutation, not the property under test. Only whether the
        # external helper ran is being proven here.
        pass

    assert sentinel.exists(), (
        "expected the configured clean filter to run once the filter-free "
        "comparison was bypassed -- if it did not, the sentinel test above "
        "may be vacuous"
    )


def test_smudge_filter_not_reachable_through_review(repo_context, tmp_path):
    """CODING-08R.1: smudge only runs on checkout (index/HEAD -> worktree),
    an operation review never performs. Proven, not assumed -- a smudge
    filter must never fire anywhere in a normal capture_review_snapshot
    cycle over a change whose smudge driver is configured."""
    root, identity = repo_context
    sentinel = tmp_path / "smudge_ran"
    script = tmp_path / "fake_smudge.sh"
    script.write_text(f"#!/bin/sh\ntouch '{sentinel}'\ncat\n")
    script.chmod(0o755)
    _git(root, "config", "filter.sentinel.smudge", str(script))
    _git(root, "config", "filter.sentinel.clean", "cat")
    (root / ".gitattributes").write_text("tracked.txt filter=sentinel\n")
    _git(root, "add", ".gitattributes", "tracked.txt")
    _git(root, "commit", "-q", "-m", "attrs")
    sentinel.unlink(missing_ok=True)  # see the clean-filter test's comment above

    (root / "tracked.txt").write_text("line1\nCHANGED\nline3\nline4\nline5\n")
    gr.capture_review_snapshot(identity)

    assert not sentinel.exists(), "smudge executed during review -- review must never check out content"
