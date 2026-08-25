"""CODING-08A2 review snapshot fingerprint/bounding/retrieval test suite."""

import os
import shutil
import subprocess
import sys

import pytest

import core.coding_checkpoint as cp
import core.git_review as gr
import core.git_review_snapshot as grs


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


def _change_by_path(snapshot, path):
    matches = [c for c in snapshot.review.changes if c.path == path]
    assert len(matches) == 1, (path, snapshot.review.changes)
    return matches[0]


def _only_change(snapshot):
    assert len(snapshot.review.changes) == 1, snapshot.review.changes
    return snapshot.review.changes[0]


# ===========================================================================
# Fingerprint / applicability.
# ===========================================================================

def test_clean_repository_is_current_and_complete(repo_context):
    root, identity = repo_context
    handle = grs.capture_snapshot(identity)
    assert handle.snapshot.fingerprint.content_complete is True
    assert handle.snapshot.fingerprint.fingerprint is not None
    applicability = grs.resolve_review_applicability(handle.snapshot_ref)
    assert applicability.state == grs.CURRENT


def test_unstaged_dirty_file_is_current_after_capture(repo_context):
    root, identity = repo_context
    (root / "tracked.txt").write_text("line1\nCHANGED\nline3\nline4\nline5\n")
    handle = grs.capture_snapshot(identity)
    applicability = grs.resolve_review_applicability(handle.snapshot_ref)
    assert applicability.state == grs.CURRENT


def test_dirty_bytes_rewritten_with_identical_status_is_stale(repo_context):
    """The CODING-08A0 case: XY status and object IDs are unchanged
    (still ".M", still the same head/index object id -- git status never
    reads worktree bytes for an already-dirty tracked path) but the
    actual content differs. A status-only fingerprint would miss this."""
    root, identity = repo_context
    (root / "tracked.txt").write_text("line1\nCHANGED-A\nline3\nline4\nline5\n")
    handle = grs.capture_snapshot(identity)
    before = _only_change(handle.snapshot)
    assert before.xy_status == ".M"

    (root / "tracked.txt").write_text("line1\nCHANGED-B\nline3\nline4\nline5\n")
    after_change = gr.capture_review_snapshot(identity).changes[0]
    assert after_change.xy_status == ".M"
    assert after_change.head_object_id == before.head_object_id
    assert after_change.index_object_id == before.index_object_id

    applicability = grs.resolve_review_applicability(handle.snapshot_ref)
    assert applicability.state == grs.STALE
    assert "content_changed" in applicability.reasons


def test_staged_content_restaged_is_stale(repo_context):
    root, identity = repo_context
    (root / "tracked.txt").write_text("line1\nline2\nline3\nline4\nline5\nline6\n")
    _git(root, "add", "tracked.txt")
    handle = grs.capture_snapshot(identity)
    assert grs.resolve_review_applicability(handle.snapshot_ref).state == grs.CURRENT

    (root / "tracked.txt").write_text("line1\nline2\nline3\nline4\nline5\nline7\n")
    _git(root, "add", "tracked.txt")
    assert grs.resolve_review_applicability(handle.snapshot_ref).state == grs.STALE


def test_untracked_bytes_changed_is_stale(repo_context):
    root, identity = repo_context
    (root / "scratch.txt").write_text("hello\n")
    handle = grs.capture_snapshot(identity)
    assert grs.resolve_review_applicability(handle.snapshot_ref).state == grs.CURRENT

    (root / "scratch.txt").write_text("goodbye\n")
    assert grs.resolve_review_applicability(handle.snapshot_ref).state == grs.STALE


def test_deletion_then_recreation_is_stale(repo_context):
    root, identity = repo_context
    os.remove(root / "tracked.txt")
    handle = grs.capture_snapshot(identity)
    assert _only_change(handle.snapshot).xy_status == ".D"
    assert grs.resolve_review_applicability(handle.snapshot_ref).state == grs.CURRENT

    (root / "tracked.txt").write_text("line1\nline2\nline3\nline4\nline5\n")
    assert grs.resolve_review_applicability(handle.snapshot_ref).state == grs.STALE


@pytest.mark.skipif(
    not hasattr(os, "symlink") or sys.platform.startswith("win"),
    reason="symlinks not supported",
)
def test_symlink_retarget_is_stale(repo_context):
    root, identity = repo_context
    (root / "target_a.txt").write_text("a")
    (root / "target_b.txt").write_text("b")
    link = root / "link.txt"
    os.symlink("target_a.txt", link)
    handle = grs.capture_snapshot(identity)
    assert grs.resolve_review_applicability(handle.snapshot_ref).state == grs.CURRENT

    os.remove(link)
    os.symlink("target_b.txt", link)
    assert grs.resolve_review_applicability(handle.snapshot_ref).state == grs.STALE


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX file modes only")
def test_mode_only_change_captured(repo_context):
    root, identity = repo_context
    (root / "tracked.txt").chmod(0o755)
    handle = grs.capture_snapshot(identity)
    change = _only_change(handle.snapshot)
    assert change.worktree_mode == "100755"
    assert grs.resolve_review_applicability(handle.snapshot_ref).state == grs.CURRENT


def test_primary_and_linked_worktree_have_distinct_snapshots(repo_context, tmp_path):
    root, main_identity = repo_context
    worktree = tmp_path / "linked"
    _git(root, "worktree", "add", "-q", str(worktree), "-b", "feature")
    (worktree / "wt_only.txt").write_text("in the linked worktree\n")
    worktree_identity = cp.resolve_target_identity(str(worktree))

    main_handle = grs.capture_snapshot(main_identity)
    wt_handle = grs.capture_snapshot(worktree_identity)
    assert main_handle.snapshot_ref != wt_handle.snapshot_ref
    assert main_handle.snapshot.review.changes == ()
    assert _only_change(wt_handle.snapshot).path == "wt_only.txt"


def test_same_path_replacement_is_target_unavailable(repo_context, monkeypatch):
    """A same-path repository replacement (rm -rf .git && git init in
    place) leaves canonical_root's own inode unchanged and
    git_common_dir resolving to the same path *string* both times, so
    TargetIdentity.target_key alone is unchanged -- InstanceFacts'
    git_common_dir filesystem identity is what must catch this.

    Deterministic via monkeypatch rather than a real rm+reinit: on a
    fresh tmp filesystem with few intervening allocations, the OS can
    (and empirically sometimes does) reuse the freed .git directory's
    inode number, making a real end-to-end reproduction flaky. This
    directly proves the InstanceFacts comparison mechanism instead --
    see test_same_path_replacement_end_to_end below for the real-world
    companion, which accepts either non-CURRENT outcome.
    """
    root, identity = repo_context
    handle = grs.capture_snapshot(identity)
    assert grs.resolve_review_applicability(handle.snapshot_ref).state == grs.CURRENT

    real_resolve = grs._resolve_live

    def forged(root_arg):
        live_identity, live_instance = real_resolve(root_arg)
        forged_instance = grs.InstanceFacts(
            git_common_dir=live_instance.git_common_dir,
            git_common_dir_filesystem=(999999, 999999),
            worktree_root_filesystem=live_instance.worktree_root_filesystem,
        )
        return live_identity, forged_instance

    monkeypatch.setattr(grs, "_resolve_live", forged)
    applicability = grs.resolve_review_applicability(handle.snapshot_ref)
    assert applicability.state == grs.TARGET_UNAVAILABLE
    assert "target_instance_replaced" in applicability.reasons


def test_same_path_replacement_end_to_end(repo_context):
    """Real rm -rf .git && git init in place. See the determinism caveat
    on test_same_path_replacement_is_target_unavailable above -- either
    non-CURRENT outcome is acceptable here; the property under test is
    that a same-path replacement never silently reads as CURRENT."""
    root, identity = repo_context
    handle = grs.capture_snapshot(identity)
    assert grs.resolve_review_applicability(handle.snapshot_ref).state == grs.CURRENT

    shutil.rmtree(root / ".git")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")

    applicability = grs.resolve_review_applicability(handle.snapshot_ref)
    assert applicability.state in (grs.TARGET_UNAVAILABLE, grs.STALE)


def test_branch_movement_is_stale(repo_context):
    root, identity = repo_context
    handle = grs.capture_snapshot(identity)
    _git(root, "checkout", "-q", "-b", "other")
    assert grs.resolve_review_applicability(handle.snapshot_ref).state == grs.STALE


def test_operation_state_change_is_stale(repo_context):
    root, identity = repo_context
    _git(root, "checkout", "-q", "-b", "branch-a")
    (root / "tracked.txt").write_text("a-version\n")
    _git(root, "commit", "-q", "-am", "a")
    _git(root, "checkout", "-q", "main")
    (root / "tracked.txt").write_text("b-version\n")
    _git(root, "commit", "-q", "-am", "b")

    handle = grs.capture_snapshot(identity)
    assert grs.resolve_review_applicability(handle.snapshot_ref).state == grs.CURRENT

    _git(root, "merge", "branch-a", "-q", check=False)
    applicability = grs.resolve_review_applicability(handle.snapshot_ref)
    assert applicability.state == grs.STALE


def _forged_unstable_fingerprint(stable_fp):
    """A fingerprint whose path_fingerprints tuple genuinely differs from
    stable_fp's -- _fingerprint_equal compares path_fingerprints (never
    the summary hex digest alone), so the forged copy must differ there
    to actually exercise instability detection."""
    extra = grs.PathFingerprint(path="\x00phantom", state="content", sha256="0" * 64)
    return grs.ReviewFingerprint(
        algo_version=stable_fp.algo_version, fingerprint=None,
        content_complete=True, omissions=(),
        path_fingerprints=stable_fp.path_fingerprints + (extra,),
    )


def test_capture_retries_once_on_transient_content_instability(repo_context, monkeypatch):
    root, identity = repo_context
    (root / "tracked.txt").write_text("line1\nCHANGED\nline3\nline4\nline5\n")
    stable_review = gr.capture_review_snapshot(identity)
    stable_fp = grs._fingerprint_content(
        identity, grs._resolve_live(identity.canonical_root)[1], stable_review
    )
    unstable_fp = _forged_unstable_fingerprint(stable_fp)
    assert not grs._fingerprint_equal(unstable_fp, stable_fp)

    states = iter([unstable_fp, stable_fp, stable_fp, stable_fp])
    monkeypatch.setattr(grs, "_fingerprint_content", lambda *a, **k: next(states))

    handle = grs.capture_snapshot(identity)
    assert handle.snapshot.fingerprint.fingerprint == stable_fp.fingerprint
    assert handle.snapshot.fingerprint.path_fingerprints == stable_fp.path_fingerprints


def test_second_instability_raises_without_registering_a_mixed_snapshot(repo_context, monkeypatch):
    root, identity = repo_context
    (root / "tracked.txt").write_text("line1\nCHANGED\nline3\nline4\nline5\n")
    stable_review = gr.capture_review_snapshot(identity)
    stable_fp = grs._fingerprint_content(
        identity, grs._resolve_live(identity.canonical_root)[1], stable_review
    )
    unstable_fp = _forged_unstable_fingerprint(stable_fp)
    states = iter([unstable_fp, stable_fp, stable_fp, unstable_fp])
    monkeypatch.setattr(grs, "_fingerprint_content", lambda *a, **k: next(states))

    before_count = len(grs._registry)
    with pytest.raises(grs.UnstableSnapshotCapture):
        grs.capture_snapshot(identity)
    assert len(grs._registry) == before_count


# ===========================================================================
# Completeness: submodule, unmerged, huge file.
# ===========================================================================

def test_dirty_submodule_is_metadata_only_and_does_not_block_capture(submodule_context):
    outer, identity = submodule_context
    (outer / "sub" / "tracked.txt").write_text("mutated inside the submodule\n")
    handle = grs.capture_snapshot(identity)
    assert handle.snapshot.fingerprint.content_complete is False
    assert grs.REASON_SUBMODULE in handle.snapshot.fingerprint.omissions
    applicability = grs.resolve_review_applicability(handle.snapshot_ref)
    assert applicability.state == grs.CURRENT_METADATA_ONLY


def test_submodule_dirtiness_elsewhere_does_not_block_unrelated_review(submodule_context):
    outer, identity = submodule_context
    (outer / "sub" / "tracked.txt").write_text("mutated inside the submodule\n")
    (outer / "unrelated.txt").write_text("outer repo file\n")
    handle = grs.capture_snapshot(identity)
    unrelated = _change_by_path(handle.snapshot, "unrelated.txt")
    fps = {fp.path: fp for fp in handle.snapshot.fingerprint.path_fingerprints}
    assert fps["unrelated.txt"].state == "content"
    assert fps["unrelated.txt"].omission_reason is None
    assert unrelated.untracked is True


def test_unmerged_conflict_is_metadata_only(conflict_context):
    root, identity = conflict_context
    handle = grs.capture_snapshot(identity)
    assert handle.snapshot.fingerprint.content_complete is False
    assert grs.REASON_UNMERGED in handle.snapshot.fingerprint.omissions
    applicability = grs.resolve_review_applicability(handle.snapshot_ref)
    assert applicability.state == grs.CURRENT_METADATA_ONLY


def test_oversized_file_is_metadata_only(repo_context, monkeypatch):
    root, identity = repo_context
    monkeypatch.setattr(grs, "MAX_FINGERPRINT_FILE_BYTES", 8)
    (root / "tracked.txt").write_text("this is definitely more than eight bytes\n")
    handle = grs.capture_snapshot(identity)
    assert handle.snapshot.fingerprint.content_complete is False
    assert grs.REASON_TOO_LARGE in handle.snapshot.fingerprint.omissions


def test_too_many_changed_paths_is_metadata_only(repo_context, monkeypatch):
    root, identity = repo_context
    monkeypatch.setattr(grs, "MAX_FINGERPRINT_PATHS", 2)
    (root / "a.txt").write_text("a\n")
    (root / "b.txt").write_text("b\n")
    (root / "c.txt").write_text("c\n")
    handle = grs.capture_snapshot(identity)
    assert handle.snapshot.fingerprint.content_complete is False
    assert grs.REASON_TOO_MANY_PATHS in handle.snapshot.fingerprint.omissions
    assert handle.snapshot.fingerprint.path_fingerprints == ()


def test_purely_staged_path_needs_no_filesystem_read(repo_context, monkeypatch):
    root, identity = repo_context
    (root / "tracked.txt").write_text("line1\nline2\nline3\nline4\nline5\nline6\n")
    _git(root, "add", "tracked.txt")

    calls = []
    real = grs._fingerprint_worktree_path

    def spy(root_arg, path, omissions):
        calls.append(path)
        return real(root_arg, path, omissions)

    monkeypatch.setattr(grs, "_fingerprint_worktree_path", spy)
    handle = grs.capture_snapshot(identity)
    assert calls == []
    fp = handle.snapshot.fingerprint.path_fingerprints[0]
    assert fp.state == "index_content_addressed"
    assert handle.snapshot.fingerprint.content_complete is True


# ===========================================================================
# Snapshot registry: unknown / eviction / TTL / sticky-stale latch.
# ===========================================================================

def test_unknown_snapshot_ref_raises(repo_context):
    with pytest.raises(grs.UnknownSnapshot):
        grs.resolve_review_applicability("revsnap-does-not-exist")


def test_registry_evicts_oldest_over_count_cap(repo_context, monkeypatch):
    root, identity = repo_context
    monkeypatch.setattr(grs, "MAX_SNAPSHOT_COUNT", 1)
    first = grs.capture_snapshot(identity)
    second = grs.capture_snapshot(identity)
    assert second.snapshot_ref != first.snapshot_ref
    with pytest.raises(grs.UnknownSnapshot):
        grs.resolve_review_applicability(first.snapshot_ref)
    assert grs.resolve_review_applicability(second.snapshot_ref).state == grs.CURRENT


def test_expired_snapshot_becomes_unknown(repo_context, monkeypatch):
    root, identity = repo_context
    handle = grs.capture_snapshot(identity)
    monkeypatch.setattr(grs, "SNAPSHOT_TTL_SECONDS", -1)
    with pytest.raises(grs.UnknownSnapshot):
        grs.resolve_review_applicability(handle.snapshot_ref)


def test_stale_snapshot_never_silently_regains_current(repo_context):
    root, identity = repo_context
    (root / "tracked.txt").write_text("line1\nCHANGED\nline3\nline4\nline5\n")
    handle = grs.capture_snapshot(identity)

    (root / "tracked.txt").write_text("line1\nCHANGED-AGAIN\nline3\nline4\nline5\n")
    assert grs.resolve_review_applicability(handle.snapshot_ref).state == grs.STALE

    # Revert back to exactly the byte content the snapshot was captured
    # against -- content now matches again, but the ref must not flip
    # back to CURRENT once observed stale.
    (root / "tracked.txt").write_text("line1\nCHANGED\nline3\nline4\nline5\n")
    applicability = grs.resolve_review_applicability(handle.snapshot_ref)
    assert applicability.state == grs.STALE
    assert "previously_observed_stale" in applicability.reasons


# ===========================================================================
# Bounded diff retrieval.
# ===========================================================================

def test_retrieve_small_unstaged_diff(repo_context):
    root, identity = repo_context
    (root / "tracked.txt").write_text("line1\nCHANGED\nline3\nline4\nline5\n")
    handle = grs.capture_snapshot(identity)
    change = _only_change(handle.snapshot)
    change_id = grs.review_change_id(change)

    outcome = grs.retrieve_review_file(handle.snapshot_ref, change_id, grs.LAYER_UNSTAGED)
    assert outcome.status == grs.CURRENT
    assert outcome.file.binary is False
    assert outcome.file.complete is True
    assert len(outcome.file.hunks) == 1
    hunk = outcome.file.hunks[0]
    assert hunk.omitted is False
    texts = [(line.kind, line.text) for line in hunk.lines]
    assert ("remove", "line2") in texts
    assert ("add", "CHANGED") in texts


def test_retrieve_staged_diff(repo_context):
    root, identity = repo_context
    (root / "tracked.txt").write_text("line1\nline2\nline3\nline4\nline5\nline6\n")
    _git(root, "add", "tracked.txt")
    handle = grs.capture_snapshot(identity)
    change = _only_change(handle.snapshot)
    change_id = grs.review_change_id(change)

    outcome = grs.retrieve_review_file(handle.snapshot_ref, change_id, grs.LAYER_STAGED)
    assert outcome.status == grs.CURRENT
    assert outcome.file.complete is True
    assert any(line.kind == "add" and line.text == "line6" for h in outcome.file.hunks for line in h.lines)


def test_retrieve_untracked_file_full_content(repo_context):
    root, identity = repo_context
    (root / "scratch.txt").write_text("hello\nworld\n")
    handle = grs.capture_snapshot(identity)
    change = _only_change(handle.snapshot)
    change_id = grs.review_change_id(change)

    outcome = grs.retrieve_review_file(handle.snapshot_ref, change_id, grs.LAYER_UNSTAGED)
    assert outcome.status == grs.CURRENT
    assert outcome.file.complete is True
    lines = [line.text for h in outcome.file.hunks for line in h.lines if line.kind == "add"]
    assert lines == ["hello", "world"]


def test_retrieve_untracked_wrong_layer_raises(repo_context):
    root, identity = repo_context
    (root / "scratch.txt").write_text("hello\n")
    handle = grs.capture_snapshot(identity)
    change = _only_change(handle.snapshot)
    change_id = grs.review_change_id(change)
    with pytest.raises(grs.RetrievalLayerError):
        grs.retrieve_review_file(handle.snapshot_ref, change_id, grs.LAYER_STAGED)


def test_retrieve_wrong_layer_for_unstaged_only_change_raises(repo_context):
    root, identity = repo_context
    (root / "tracked.txt").write_text("line1\nCHANGED\nline3\nline4\nline5\n")
    handle = grs.capture_snapshot(identity)
    change = _only_change(handle.snapshot)
    change_id = grs.review_change_id(change)
    with pytest.raises(grs.RetrievalLayerError):
        grs.retrieve_review_file(handle.snapshot_ref, change_id, grs.LAYER_STAGED)


def test_retrieve_multiple_hunks(repo_context):
    root, identity = repo_context
    lines = [f"line{i}\n" for i in range(1, 41)]
    (root / "many.txt").write_text("".join(lines))
    _git(root, "add", "many.txt")
    _git(root, "commit", "-q", "-m", "many")
    lines[1] = "CHANGED-NEAR-TOP\n"
    lines[35] = "CHANGED-NEAR-BOTTOM\n"
    (root / "many.txt").write_text("".join(lines))

    handle = grs.capture_snapshot(identity)
    change = _change_by_path(handle.snapshot, "many.txt")
    change_id = grs.review_change_id(change)
    outcome = grs.retrieve_review_file(handle.snapshot_ref, change_id, grs.LAYER_UNSTAGED)
    assert outcome.file.complete is True
    assert len(outcome.file.hunks) == 2


def test_retrieve_multiple_files_independently(repo_context):
    root, identity = repo_context
    (root / "a.txt").write_text("a\n")
    (root / "b.txt").write_text("b\n")
    handle = grs.capture_snapshot(identity)
    a_change = _change_by_path(handle.snapshot, "a.txt")
    b_change = _change_by_path(handle.snapshot, "b.txt")

    a_outcome = grs.retrieve_review_file(
        handle.snapshot_ref, grs.review_change_id(a_change), grs.LAYER_UNSTAGED
    )
    b_outcome = grs.retrieve_review_file(
        handle.snapshot_ref, grs.review_change_id(b_change), grs.LAYER_UNSTAGED
    )
    assert [l.text for h in a_outcome.file.hunks for l in h.lines] == ["a"]
    assert [l.text for h in b_outcome.file.hunks for l in h.lines] == ["b"]


def test_huge_single_line_hunk_is_omitted_as_one_unit(repo_context):
    """CODING-08A0: a single huge line produces a tiny hunk *line count*
    occupying tens of kilobytes. Real production MAX_HUNK_BYTES (no
    monkeypatch) must omit the whole hunk, never slice the line."""
    root, identity = repo_context
    huge_before = "x" * 300_000 + "\n"
    huge_after = "y" * 300_000 + "\n"
    (root / "huge.txt").write_text(huge_before)
    _git(root, "add", "huge.txt")
    _git(root, "commit", "-q", "-m", "huge")
    (root / "huge.txt").write_text(huge_after)

    handle = grs.capture_snapshot(identity)
    change = _change_by_path(handle.snapshot, "huge.txt")
    change_id = grs.review_change_id(change)
    outcome = grs.retrieve_review_file(handle.snapshot_ref, change_id, grs.LAYER_UNSTAGED)
    assert outcome.status == grs.CURRENT
    result = outcome.file
    assert result.complete is False
    assert result.omitted_hunks == 1
    assert result.omission_reason == grs.REASON_HUNK_TOO_LARGE
    assert len(result.hunks) == 1
    assert result.hunks[0].omitted is True
    assert result.hunks[0].lines == ()
    assert result.hunks[0].header is not None


def test_hunk_exact_byte_boundary_is_included_one_byte_over_is_omitted(repo_context, monkeypatch):
    root, identity = repo_context
    (root / "b.txt").write_text("start\n")
    _git(root, "add", "b.txt")
    _git(root, "commit", "-q", "-m", "b")
    (root / "b.txt").write_text("end\n")
    handle = grs.capture_snapshot(identity)
    change = _change_by_path(handle.snapshot, "b.txt")
    change_id = grs.review_change_id(change)

    outcome = grs.retrieve_review_file(handle.snapshot_ref, change_id, grs.LAYER_UNSTAGED)
    hunk_size = outcome.file.hunks[0].byte_size

    monkeypatch.setattr(grs, "MAX_HUNK_BYTES", hunk_size)
    at_boundary = grs.retrieve_review_file(handle.snapshot_ref, change_id, grs.LAYER_UNSTAGED)
    assert at_boundary.file.hunks[0].omitted is False

    monkeypatch.setattr(grs, "MAX_HUNK_BYTES", hunk_size - 1)
    over_boundary = grs.retrieve_review_file(handle.snapshot_ref, change_id, grs.LAYER_UNSTAGED)
    assert over_boundary.file.hunks[0].omitted is True
    assert over_boundary.file.omission_reason == grs.REASON_HUNK_TOO_LARGE


def test_file_budget_pagination_via_next_cursor(repo_context, monkeypatch):
    root, identity = repo_context
    lines = [f"line{i}\n" for i in range(1, 61)]
    (root / "paged.txt").write_text("".join(lines))
    _git(root, "add", "paged.txt")
    _git(root, "commit", "-q", "-m", "paged")
    for i in (1, 20, 40, 58):
        lines[i] = f"CHANGED-{i}\n"
    (root / "paged.txt").write_text("".join(lines))

    handle = grs.capture_snapshot(identity)
    change = _change_by_path(handle.snapshot, "paged.txt")
    change_id = grs.review_change_id(change)

    full = grs.retrieve_review_file(handle.snapshot_ref, change_id, grs.LAYER_UNSTAGED)
    assert full.file.complete is True
    total_hunks = len(full.file.hunks)
    assert total_hunks >= 2
    per_hunk_bytes = max(h.byte_size for h in full.file.hunks)

    monkeypatch.setattr(grs, "MAX_FILE_DIFF_BYTES", per_hunk_bytes)
    page1 = grs.retrieve_review_file(handle.snapshot_ref, change_id, grs.LAYER_UNSTAGED)
    assert page1.file.complete is False
    assert page1.file.omission_reason == grs.REASON_FILE_BUDGET
    assert page1.file.next_cursor is not None
    assert len(page1.file.hunks) == total_hunks
    assert sum(1 for h in page1.file.hunks if not h.omitted) < total_hunks

    page2 = grs.retrieve_review_file(
        handle.snapshot_ref, change_id, grs.LAYER_UNSTAGED,
        start_hunk_index=page1.file.next_cursor,
    )
    assert page2.file.hunks[0].omitted is False


def test_binary_file_is_represented_without_patch_content(repo_context):
    root, identity = repo_context
    (root / "data.bin").write_bytes(b"\x00\x01\x02\xff\xfe binary content \x00")
    handle = grs.capture_snapshot(identity)
    change = _only_change(handle.snapshot)
    change_id = grs.review_change_id(change)
    outcome = grs.retrieve_review_file(handle.snapshot_ref, change_id, grs.LAYER_UNSTAGED)
    assert outcome.file.binary is True
    assert outcome.file.hunks == ()
    assert outcome.file.complete is True


def test_unmerged_retrieval_is_truthfully_omitted(conflict_context):
    root, identity = conflict_context
    handle = grs.capture_snapshot(identity)
    change = _only_change(handle.snapshot)
    change_id = grs.review_change_id(change)
    outcome = grs.retrieve_review_file(handle.snapshot_ref, change_id, grs.LAYER_UNSTAGED)
    assert outcome.status == grs.CURRENT_METADATA_ONLY
    assert outcome.file.complete is False
    assert outcome.file.omission_reason == grs.REASON_UNMERGED


def test_submodule_retrieval_is_truthfully_omitted(submodule_context):
    outer, identity = submodule_context
    (outer / "sub" / "tracked.txt").write_text("mutated inside the submodule\n")
    handle = grs.capture_snapshot(identity)
    change = _only_change(handle.snapshot)
    change_id = grs.review_change_id(change)
    outcome = grs.retrieve_review_file(handle.snapshot_ref, change_id, grs.LAYER_UNSTAGED)
    assert outcome.status == grs.CURRENT_METADATA_ONLY
    assert outcome.file.complete is False
    assert outcome.file.omission_reason == grs.REASON_SUBMODULE


def test_deleted_file_retrieval_shows_full_removal(repo_context):
    root, identity = repo_context
    os.remove(root / "tracked.txt")
    handle = grs.capture_snapshot(identity)
    change = _only_change(handle.snapshot)
    change_id = grs.review_change_id(change)
    outcome = grs.retrieve_review_file(handle.snapshot_ref, change_id, grs.LAYER_UNSTAGED)
    removed = [l.text for h in outcome.file.hunks for l in h.lines if l.kind == "remove"]
    assert removed == ["line1", "line2", "line3", "line4", "line5"]


def test_added_file_retrieval_shows_full_addition(repo_context):
    root, identity = repo_context
    (root / "new.txt").write_text("brand new\ncontent\n")
    _git(root, "add", "new.txt")
    handle = grs.capture_snapshot(identity)
    change = _only_change(handle.snapshot)
    change_id = grs.review_change_id(change)
    outcome = grs.retrieve_review_file(handle.snapshot_ref, change_id, grs.LAYER_STAGED)
    added = [l.text for h in outcome.file.hunks for l in h.lines if l.kind == "add"]
    assert added == ["brand new", "content"]


def test_rename_retrieval_shows_current_path_content(repo_context):
    root, identity = repo_context
    _git(root, "mv", "tracked.txt", "renamed.txt")
    _git(root, "add", "-A")
    handle = grs.capture_snapshot(identity)
    change = _only_change(handle.snapshot)
    assert change.relation == "rename"
    change_id = grs.review_change_id(change)
    outcome = grs.retrieve_review_file(handle.snapshot_ref, change_id, grs.LAYER_STAGED)
    assert outcome.file.complete is True


def test_copy_retrieval_shows_current_path_content(repo_context):
    root, identity = repo_context
    (root / "copy.txt").write_text((root / "tracked.txt").read_text())
    (root / "tracked.txt").write_text("line1\nline2\nline3\nline4\nline5\nmodified\n")
    _git(root, "add", "-A")
    handle = grs.capture_snapshot(identity)
    change = _change_by_path(handle.snapshot, "copy.txt")
    assert change.relation == "copy"
    change_id = grs.review_change_id(change)
    outcome = grs.retrieve_review_file(handle.snapshot_ref, change_id, grs.LAYER_STAGED)
    assert outcome.file.complete is True


def test_hunk_order_is_deterministic_across_repeated_retrieval(repo_context):
    root, identity = repo_context
    lines = [f"line{i}\n" for i in range(1, 41)]
    (root / "many.txt").write_text("".join(lines))
    _git(root, "add", "many.txt")
    _git(root, "commit", "-q", "-m", "many")
    lines[1] = "CHANGED-A\n"
    lines[35] = "CHANGED-B\n"
    (root / "many.txt").write_text("".join(lines))
    handle = grs.capture_snapshot(identity)
    change = _change_by_path(handle.snapshot, "many.txt")
    change_id = grs.review_change_id(change)

    first = grs.retrieve_review_file(handle.snapshot_ref, change_id, grs.LAYER_UNSTAGED)
    second = grs.retrieve_review_file(handle.snapshot_ref, change_id, grs.LAYER_UNSTAGED)
    assert first.file.hunks == second.file.hunks


# ===========================================================================
# Retrieval revalidation contract.
# ===========================================================================

def test_retrieval_unknown_snapshot_raises(repo_context):
    with pytest.raises(grs.UnknownSnapshot):
        grs.retrieve_review_file("revsnap-nope", "0" * 16, grs.LAYER_UNSTAGED)


def test_retrieval_unknown_change_raises(repo_context):
    root, identity = repo_context
    (root / "a.txt").write_text("a\n")
    handle = grs.capture_snapshot(identity)
    with pytest.raises(grs.UnknownChange):
        grs.retrieve_review_file(handle.snapshot_ref, "0" * 16, grs.LAYER_UNSTAGED)


def test_retrieval_invalid_layer_raises(repo_context):
    root, identity = repo_context
    (root / "a.txt").write_text("a\n")
    handle = grs.capture_snapshot(identity)
    change = _only_change(handle.snapshot)
    with pytest.raises(grs.RetrievalLayerError):
        grs.retrieve_review_file(handle.snapshot_ref, grs.review_change_id(change), "sideways")


def test_retrieval_refuses_when_snapshot_stale_before_call(repo_context):
    root, identity = repo_context
    (root / "tracked.txt").write_text("line1\nCHANGED\nline3\nline4\nline5\n")
    handle = grs.capture_snapshot(identity)
    change = _only_change(handle.snapshot)
    change_id = grs.review_change_id(change)

    (root / "tracked.txt").write_text("line1\nCHANGED-AGAIN\nline3\nline4\nline5\n")
    outcome = grs.retrieve_review_file(handle.snapshot_ref, change_id, grs.LAYER_UNSTAGED)
    assert outcome.status == grs.STALE
    assert outcome.file is None


def test_retrieval_detects_mutation_during_capture(repo_context, monkeypatch):
    root, identity = repo_context
    (root / "tracked.txt").write_text("line1\nCHANGED\nline3\nline4\nline5\n")
    handle = grs.capture_snapshot(identity)
    change = _only_change(handle.snapshot)
    change_id = grs.review_change_id(change)

    real_capture = grs._capture_file_diff

    def mutate_then_capture(*args, **kwargs):
        result = real_capture(*args, **kwargs)
        (root / "tracked.txt").write_text("line1\nCHANGED-DURING-READ\nline3\nline4\nline5\n")
        return result

    monkeypatch.setattr(grs, "_capture_file_diff", mutate_then_capture)
    outcome = grs.retrieve_review_file(handle.snapshot_ref, change_id, grs.LAYER_UNSTAGED)
    assert outcome.status == grs.STALE
    assert outcome.file is None


def test_retrieval_target_removed_is_target_unavailable(repo_context):
    root, identity = repo_context
    (root / "a.txt").write_text("a\n")
    handle = grs.capture_snapshot(identity)
    change = _only_change(handle.snapshot)
    change_id = grs.review_change_id(change)
    shutil.rmtree(root)
    outcome = grs.retrieve_review_file(handle.snapshot_ref, change_id, grs.LAYER_UNSTAGED)
    assert outcome.status == grs.TARGET_UNAVAILABLE
    assert outcome.file is None


def test_retrieval_target_replaced_is_target_unavailable(repo_context, monkeypatch):
    root, identity = repo_context
    (root / "a.txt").write_text("a\n")
    handle = grs.capture_snapshot(identity)
    change = _only_change(handle.snapshot)
    change_id = grs.review_change_id(change)

    real_resolve = grs._resolve_live

    def forged(root_arg):
        live_identity, live_instance = real_resolve(root_arg)
        forged_instance = grs.InstanceFacts(
            git_common_dir=live_instance.git_common_dir,
            git_common_dir_filesystem=(999999, 999999),
            worktree_root_filesystem=live_instance.worktree_root_filesystem,
        )
        return live_identity, forged_instance

    monkeypatch.setattr(grs, "_resolve_live", forged)
    outcome = grs.retrieve_review_file(handle.snapshot_ref, change_id, grs.LAYER_UNSTAGED)
    assert outcome.status == grs.TARGET_UNAVAILABLE
    assert outcome.file is None


def test_old_snapshot_stays_stale_after_new_generation_captured(repo_context):
    root, identity = repo_context
    (root / "tracked.txt").write_text("line1\nCHANGED\nline3\nline4\nline5\n")
    old_handle = grs.capture_snapshot(identity)

    (root / "tracked.txt").write_text("line1\nCHANGED-V2\nline3\nline4\nline5\n")
    new_handle = grs.capture_snapshot(identity)

    assert grs.resolve_review_applicability(old_handle.snapshot_ref).state == grs.STALE
    assert grs.resolve_review_applicability(new_handle.snapshot_ref).state == grs.CURRENT


# ===========================================================================
# Security: diff.external / textconv sentinels against the real retrieval
# path (patch-producing, unlike A1's --numstat calls -- see module
# docstring). These are genuinely load-bearing, not merely structural.
# ===========================================================================

def test_retrieval_configured_diff_external_does_not_execute(repo_context, tmp_path):
    root, identity = repo_context
    sentinel = tmp_path / "diff_external_ran"
    script = tmp_path / "fake_diff.sh"
    script.write_text(f"#!/bin/sh\ntouch '{sentinel}'\nexit 0\n")
    script.chmod(0o755)
    _git(root, "config", "diff.external", str(script))

    (root / "tracked.txt").write_text("changed enough to trigger a real diff\n")
    handle = grs.capture_snapshot(identity)
    change = _only_change(handle.snapshot)
    change_id = grs.review_change_id(change)
    grs.retrieve_review_file(handle.snapshot_ref, change_id, grs.LAYER_UNSTAGED)

    assert not sentinel.exists(), (
        "diff.external executed during bounded retrieval -- the "
        "--no-ext-diff guard did not hold"
    )


def test_retrieval_configured_textconv_does_not_execute(repo_context, tmp_path):
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
    handle = grs.capture_snapshot(identity)
    change = _change_by_path(handle.snapshot, "tracked.txt")
    change_id = grs.review_change_id(change)
    grs.retrieve_review_file(handle.snapshot_ref, change_id, grs.LAYER_UNSTAGED)

    assert not sentinel.exists(), (
        "textconv executed during bounded retrieval -- the "
        "--no-textconv guard did not hold"
    )


def test_retrieval_diff_invocation_argv_carries_safety_flags(repo_context, monkeypatch):
    root, identity = repo_context
    (root / "tracked.txt").write_text("changed enough to trigger a real diff\n")
    handle = grs.capture_snapshot(identity)
    change = _only_change(handle.snapshot)
    change_id = grs.review_change_id(change)

    recorded = []
    real = grs.run_bounded_git

    def spy(root_arg, args, **kwargs):
        recorded.append(tuple(args))
        return real(root_arg, args, **kwargs)

    monkeypatch.setattr(grs, "run_bounded_git", spy)
    grs.retrieve_review_file(handle.snapshot_ref, change_id, grs.LAYER_UNSTAGED)

    diff_calls = [args for args in recorded if "diff" in args]
    assert diff_calls, recorded
    for args in diff_calls:
        assert "--no-ext-diff" in args, args
        assert "--no-textconv" in args, args


def test_diff_external_removed_flag_would_execute_helper(repo_context, tmp_path, monkeypatch):
    """Companion proof that the sentinel test above is genuinely
    load-bearing (unlike A1's --numstat calls): with the safety flags
    monkeypatched away, the SAME retrieval path DOES invoke the
    configured external helper. This is the empirical evidence that
    _DIFF_SAFETY_ARGS is load-bearing for this module's patch calls."""
    root, identity = repo_context
    sentinel = tmp_path / "diff_external_ran"
    script = tmp_path / "fake_diff.sh"
    script.write_text(f"#!/bin/sh\ntouch '{sentinel}'\nexit 0\n")
    script.chmod(0o755)
    _git(root, "config", "diff.external", str(script))

    (root / "tracked.txt").write_text("changed enough to trigger a real diff\n")
    handle = grs.capture_snapshot(identity)
    change = _only_change(handle.snapshot)
    change_id = grs.review_change_id(change)

    monkeypatch.setattr(gr, "_DIFF_SAFETY_ARGS", ())
    grs.retrieve_review_file(handle.snapshot_ref, change_id, grs.LAYER_UNSTAGED)
    assert sentinel.exists(), (
        "expected the configured diff.external helper to run once the "
        "safety flags were removed -- if it did not, the sentinel test "
        "above may be vacuous for this Git version"
    )


# ===========================================================================
# Security: hostile filenames, no shell construction.
# ===========================================================================

def test_retrieve_filename_beginning_with_dash(repo_context):
    root, identity = repo_context
    name = "-dangerous-looking-name.txt"
    (root / name).write_text("content\n")
    handle = grs.capture_snapshot(identity)
    change = _only_change(handle.snapshot)
    assert change.path == name
    change_id = grs.review_change_id(change)
    outcome = grs.retrieve_review_file(handle.snapshot_ref, change_id, grs.LAYER_UNSTAGED)
    assert outcome.status == grs.CURRENT
    added = [l.text for h in outcome.file.hunks for l in h.lines if l.kind == "add"]
    assert added == ["content"]


def test_retrieve_filename_with_spaces(repo_context):
    root, identity = repo_context
    name = "a file with spaces.txt"
    (root / name).write_text("content\n")
    handle = grs.capture_snapshot(identity)
    change = _only_change(handle.snapshot)
    change_id = grs.review_change_id(change)
    outcome = grs.retrieve_review_file(handle.snapshot_ref, change_id, grs.LAYER_UNSTAGED)
    assert outcome.status == grs.CURRENT
    assert outcome.file.path == name


def test_retrieve_filename_with_embedded_newline(repo_context):
    root, identity = repo_context
    name = "weird\nname.txt"
    (root / name).write_text("content\n")
    handle = grs.capture_snapshot(identity)
    change = _only_change(handle.snapshot)
    assert change.path == name
    change_id = grs.review_change_id(change)
    outcome = grs.retrieve_review_file(handle.snapshot_ref, change_id, grs.LAYER_UNSTAGED)
    assert outcome.status == grs.CURRENT
    assert outcome.file.path == name


def test_fingerprint_filename_beginning_with_dash(repo_context):
    root, identity = repo_context
    name = "-looks-like-a-flag.txt"
    (root / name).write_text("content\n")
    handle = grs.capture_snapshot(identity)
    assert handle.snapshot.fingerprint.content_complete is True
    fps = {fp.path: fp for fp in handle.snapshot.fingerprint.path_fingerprints}
    assert fps[name].state == "content"


def test_no_shell_true_in_source():
    source = open(grs.__file__, encoding="utf-8").read()
    assert "shell=True" not in source


# ===========================================================================
# Mutation F: symlink must never be followed for fingerprinting.
# ===========================================================================

@pytest.mark.skipif(
    not hasattr(os, "symlink") or sys.platform.startswith("win"),
    reason="symlinks not supported",
)
def test_symlink_fingerprint_ignores_target_file_content_changes(repo_context):
    root, identity = repo_context
    target = root / "target_a.txt"
    target.write_text("original target content\n")
    link = root / "link.txt"
    os.symlink("target_a.txt", link)

    handle_before = grs.capture_snapshot(identity)
    link_fp_before = next(
        fp for fp in handle_before.snapshot.fingerprint.path_fingerprints if fp.path == "link.txt"
    )
    assert link_fp_before.state == "symlink"

    # Change the bytes of the file the link POINTS AT -- the symlink's own
    # target text ("target_a.txt") is unchanged. A correct fingerprint must
    # be invariant to this; only os.readlink() output may ever be hashed.
    target.write_text("MUTATED target content -- should never be read\n")
    handle_after = grs.capture_snapshot(identity)
    link_fp_after = next(
        fp for fp in handle_after.snapshot.fingerprint.path_fingerprints if fp.path == "link.txt"
    )
    assert link_fp_after.sha256 == link_fp_before.sha256

    # Retargeting the symlink itself DOES change the fingerprint.
    os.remove(link)
    os.symlink("does_not_exist.txt", link)
    handle_retargeted = grs.capture_snapshot(identity)
    link_fp_retargeted = next(
        fp for fp in handle_retargeted.snapshot.fingerprint.path_fingerprints
        if fp.path == "link.txt"
    )
    assert link_fp_retargeted.sha256 != link_fp_before.sha256


# ===========================================================================
# Architecture boundary.
# ===========================================================================

def test_no_tool_registration_or_qt_dependency():
    source = open(grs.__file__, encoding="utf-8").read()
    assert "registry.register" not in source
    assert "PySide6" not in source
    assert "PyQt" not in source


def test_capture_requires_git_target_identity(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    identity = cp.resolve_target_identity(str(plain))
    assert identity.kind == "directory"
    with pytest.raises(gr.ReviewTargetError):
        grs.capture_snapshot(identity)


def test_capture_requires_target_identity_instance():
    with pytest.raises(gr.ReviewTargetError):
        grs.capture_snapshot("/not/an/identity")


def test_capture_stale_identity_after_repository_removed_is_refused(repo_context):
    root, identity = repo_context
    shutil.rmtree(root)
    with pytest.raises(gr.ReviewTargetError):
        grs.capture_snapshot(identity)
