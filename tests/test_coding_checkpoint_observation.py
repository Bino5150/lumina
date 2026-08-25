"""CODING-05A2 live-observation and stale-state behavioral suite."""

import json
import os
import shutil
import subprocess
from dataclasses import replace

import pytest

import config
import core.coding_checkpoint as cp
import core.coding_checkpoint_observation as obs
import core.project_context as project_context
from core.project_context import save_project_binding


def _git(path, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=path, check=check, capture_output=True, text=True
    )


def _init_repo(path, *, commit=True):
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    if commit:
        (path / "README.md").write_text("hello\n")
        _git(path, "add", ".")
        _git(path, "commit", "-q", "-m", "initial")


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "lumina.db"))
    monkeypatch.setattr(
        project_context, "PROJECT_BINDINGS_DIR", str(tmp_path / "bindings")
    )


@pytest.fixture
def repo_context(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    return root, save_project_binding("proj", str(root))


@pytest.fixture
def directory_context(tmp_path):
    root = tmp_path / "directory"
    root.mkdir()
    return root, save_project_binding("proj", str(root))


def _workflow(**overrides):
    value = {"task_id": "CODING-05", "phase": "in_progress"}
    value.update(overrides)
    return value


def _marker_path(repo, marker):
    result = _git(repo, "rev-parse", "--path-format=absolute", "--git-path", marker)
    return repo.__class__(result.stdout.strip())


def test_normal_branch_clean_tree_full_head(repo_context):
    root, context = repo_context
    current = obs.capture_live_observation(context)

    assert current.identity.kind == "git"
    assert current.head == _git(root, "rev-parse", "HEAD").stdout.strip()
    assert len(current.head) in {40, 64}
    assert current.branch == _git(root, "branch", "--show-current").stdout.strip()
    assert current.detached is False
    assert current.unborn is False
    assert current.changed_paths == ()
    assert current.status_complete is True
    assert len(current.status_digest) == 64


def test_detached_head_semantics(repo_context):
    root, context = repo_context
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    _git(root, "checkout", "-q", "--detach", head)

    current = obs.capture_live_observation(context)
    assert current.head == head
    assert current.branch is None
    assert current.detached is True
    assert current.unborn is False


def test_unborn_branch_semantics(tmp_path):
    root = tmp_path / "unborn"
    root.mkdir()
    _init_repo(root, commit=False)
    context = save_project_binding("proj", str(root))

    current = obs.capture_live_observation(context)
    assert current.identity.kind == "git"
    assert current.head is None
    assert current.branch
    assert current.detached is False
    assert current.unborn is True


def test_staged_unstaged_untracked_and_mixed_status(repo_context):
    root, context = repo_context
    (root / "README.md").write_text("staged\n")
    _git(root, "add", "README.md")
    (root / "README.md").write_text("mixed\n")
    (root / "new.txt").write_text("untracked\n")

    current = obs.capture_live_observation(context)
    by_path = {item.path: item for item in current.changed_paths}
    assert by_path["README.md"].status == "MM"
    assert by_path["README.md"].record_type == "ordinary"
    assert by_path["new.txt"].status == "??"
    assert by_path["new.txt"].record_type == "untracked"


def test_rename_is_structural_not_flattened(repo_context):
    root, context = repo_context
    _git(root, "mv", "README.md", "RENAMED.md")

    current = obs.capture_live_observation(context)
    rename = next(item for item in current.changed_paths if item.record_type == "rename")
    assert rename.path == "RENAMED.md"
    assert rename.original_path == "README.md"
    assert " -> " not in rename.path


def test_more_than_64_paths_truncates_display_but_digest_binds_tail(repo_context):
    root, context = repo_context
    for number in range(65):
        (root / f"f{number:02}.txt").write_text("original\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "many files")
    for number in range(65):
        (root / f"f{number:02}.txt").write_text("changed\n")

    first = obs.capture_live_observation(context)
    assert len(first.changed_paths) == 64
    assert first.changed_paths_truncated is True

    _git(root, "checkout", "--", "f64.txt")
    second = obs.capture_live_observation(context)
    assert first.status_digest != second.status_digest


@pytest.mark.parametrize(
    "operation,marker,is_directory",
    [
        ("merge", "MERGE_HEAD", False),
        ("cherry_pick", "CHERRY_PICK_HEAD", False),
        ("rebase", "rebase-merge", True),
        ("revert", "REVERT_HEAD", False),
        ("bisect", "BISECT_LOG", False),
    ],
)
def test_hidden_operation_state_uses_git_resolved_paths(
    repo_context, operation, marker, is_directory
):
    root, context = repo_context
    marker_path = _marker_path(root, marker)
    if is_directory:
        marker_path.mkdir(parents=True)
    else:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(_git(root, "rev-parse", "HEAD").stdout)

    current = obs.capture_live_observation(context)
    assert operation in current.operation_state


def test_linked_worktree_has_distinct_target_and_git_file(repo_context, tmp_path):
    root, main_context = repo_context
    worktree = tmp_path / "linked"
    _git(root, "worktree", "add", "-q", "-b", "linked-branch", str(worktree))
    linked_context = save_project_binding("linked", str(worktree))

    main = obs.capture_live_observation(main_context)
    linked = obs.capture_live_observation(linked_context)
    assert (worktree / ".git").is_file()
    assert linked.identity.kind == "git"
    assert linked.identity.target_key != main.identity.target_key
    assert linked.branch == "linked-branch"


def test_hidden_operation_in_linked_worktree_resolves_via_worktree_git_path(
    repo_context, tmp_path
):
    root, main_context = repo_context
    worktree = tmp_path / "linked"
    _git(root, "worktree", "add", "-q", "-b", "linked-branch", str(worktree))
    linked_context = save_project_binding("linked", str(worktree))

    main_marker = _marker_path(root, "MERGE_HEAD")
    worktree_marker = _marker_path(worktree, "MERGE_HEAD")
    assert worktree_marker != main_marker

    obs.save_live_checkpoint(linked_context, _workflow(), expected_revision=0)

    worktree_marker.parent.mkdir(parents=True, exist_ok=True)
    worktree_marker.write_text(_git(worktree, "rev-parse", "HEAD").stdout)

    during = obs.load_live_checkpoint(linked_context)
    assert "merge" in during.current.operation_state
    assert "operation_state_changed" in during.freshness.reasons

    main_current = obs.capture_live_observation(main_context)
    assert "merge" not in main_current.operation_state

    worktree_marker.unlink()
    after = obs.load_live_checkpoint(linked_context)
    assert after.freshness.state == obs.FRESH


def test_upstream_and_push_posture_are_factual_without_urls(repo_context):
    root, context = repo_context
    branch = _git(root, "branch", "--show-current").stdout.strip()
    _git(root, "remote", "add", "origin", "DISABLED")
    _git(root, "update-ref", f"refs/remotes/origin/{branch}", "HEAD")
    _git(root, "config", f"branch.{branch}.remote", "origin")
    _git(root, "config", f"branch.{branch}.merge", f"refs/heads/{branch}")
    _git(root, "config", "push.default", "nothing")

    current = obs.capture_live_observation(context)
    assert current.upstream_ref == f"origin/{branch}"
    assert current.push_posture.push_default == "nothing"
    assert current.push_posture.configured_safety_sentinel is True
    assert "DISABLED" not in json.dumps(current.observation_payload())


def test_directory_target_has_no_fabricated_git_fields(directory_context):
    _root, context = directory_context
    current = obs.capture_live_observation(context)

    assert current.identity.kind == "directory"
    assert current.head is None
    assert current.branch is None
    assert current.status_digest is None
    assert current.push_posture is None
    assert current.operation_state == ()


def test_regular_relevant_file_hash_and_change_staleness(repo_context):
    root, context = repo_context
    expected = hashlib_sha256((root / "README.md").read_bytes())
    saved = obs.save_live_checkpoint(
        context,
        _workflow(),
        relevant_paths=["README.md"],
        expected_revision=0,
    )
    assert saved.record.relevant_files[0]["sha256"] == expected

    (root / "README.md").write_text("changed\n")
    loaded = obs.load_live_checkpoint(context)
    assert loaded.freshness.state == obs.STALE
    assert "working_state_changed" in loaded.freshness.reasons
    assert "relevant_files_changed" in loaded.freshness.reasons


def hashlib_sha256(value):
    import hashlib
    return hashlib.sha256(value).hexdigest()


def test_relevant_delete_and_missing_to_created_make_stale(repo_context):
    root, context = repo_context
    saved = obs.save_live_checkpoint(
        context,
        _workflow(),
        relevant_paths=["README.md", "future.txt"],
        expected_revision=0,
    )
    by_path = {item["path"]: item for item in saved.record.relevant_files}
    assert by_path["future.txt"]["missing"] is True

    (root / "README.md").unlink()
    (root / "future.txt").write_text("created\n")
    loaded = obs.load_live_checkpoint(context)
    assert "relevant_files_changed" in loaded.freshness.reasons


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_symlink_is_not_followed_and_replacement_is_stale(repo_context, tmp_path):
    root, context = repo_context
    outside = tmp_path / "secret.txt"
    outside.write_text("must not be hashed\n")
    os.symlink(outside, root / "link.txt")

    saved = obs.save_live_checkpoint(
        context,
        _workflow(),
        relevant_paths=["link.txt"],
        expected_revision=0,
    )
    link = saved.record.relevant_files[0]
    assert link["is_symlink"] is True
    assert link["sha256"] is None
    assert str(outside) not in json.dumps(link)

    (root / "link.txt").unlink()
    (root / "link.txt").write_text("regular now\n")
    loaded = obs.load_live_checkpoint(context)
    assert "relevant_files_changed" in loaded.freshness.reasons


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_intermediate_symlink_escape_is_rejected(directory_context, tmp_path):
    root, context = directory_context
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file.txt").write_text("secret\n")
    os.symlink(outside, root / "escape")

    with pytest.raises(obs.RelevantPathError):
        obs.capture_live_observation(context, ["escape/file.txt"])


@pytest.mark.parametrize(
    "path",
    ["../outside.txt", "/absolute.txt", r"C:\\Windows\\file.txt", "a/../../b"],
)
def test_relevant_path_escape_and_absolute_forms_rejected(directory_context, path):
    _root, context = directory_context
    with pytest.raises(obs.RelevantPathError):
        obs.capture_live_observation(context, [path])


def test_relevant_path_normalizes_and_directory_is_rejected(directory_context):
    root, context = directory_context
    (root / "sub").mkdir()
    (root / "sub" / "file.txt").write_text("x\n")
    current = obs.capture_live_observation(context, [r"./sub\\file.txt"])
    assert current.relevant_files[0].path == "sub/file.txt"

    with pytest.raises(obs.RelevantPathError):
        obs.capture_live_observation(context, ["sub"])


def test_file_hash_retries_once_then_succeeds(repo_context, monkeypatch):
    _root, context = repo_context
    original = obs._hash_regular_file_once
    calls = {"count": 0}

    def flaky(path, before):
        calls["count"] += 1
        if calls["count"] == 1:
            raise obs._FileChanged()
        return original(path, before)

    monkeypatch.setattr(obs, "_hash_regular_file_once", flaky)
    current = obs.capture_live_observation(context, ["README.md"])
    assert current.relevant_files[0].sha256
    assert calls["count"] == 2


def test_file_hash_second_instability_fails_capture(repo_context, monkeypatch):
    _root, context = repo_context

    def always_changes(_path, _before):
        raise obs._FileChanged()

    monkeypatch.setattr(obs, "_hash_regular_file_once", always_changes)
    with pytest.raises(obs.UnstableCapture):
        obs.capture_live_observation(context, ["README.md"])


def test_stale_immutable_context_refuses_after_durable_rebind(repo_context, tmp_path):
    _root, old_context = repo_context
    new_root = tmp_path / "new-root"
    new_root.mkdir()
    save_project_binding("proj", str(new_root))

    with pytest.raises(obs.ProjectBindingChanged, match="refresh/reactivate"):
        obs.capture_live_observation(old_context)


def test_exact_save_load_and_reported_validation_binding(repo_context):
    _root, context = repo_context
    saved = obs.save_live_checkpoint(
        context,
        _workflow(summary="caller prose"),
        relevant_paths=["README.md"],
        reported_validations=[{
            "label": "pytest",
            "outcome": "pass",
            "exit_code": 0,
            "summary": "reported green",
        }],
        expected_revision=0,
    )
    validation = saved.record.validations[0]
    assert validation["source"] == "reported"
    assert validation["state_ref"] == saved.observation.state_ref
    assert validation["binding_complete"] is True

    loaded = obs.load_live_checkpoint(context)
    assert loaded.status == "ok"
    assert loaded.freshness == obs.FreshnessResult(obs.FRESH, ())
    assert loaded.validation_applicability[0].applies_to_current_state is True
    assert loaded.validation_applicability[0].reason == "captured_state_matches"


@pytest.mark.parametrize(
    "forbidden",
    ["state_ref", "source", "binding_complete", "target_key", "head"],
)
def test_caller_cannot_supply_validation_or_observation_binding(repo_context, forbidden):
    _root, context = repo_context
    report = {"label": "pytest", "outcome": "pass", forbidden: "fake"}
    with pytest.raises(cp.CheckpointValidationError, match="caller-forbidden"):
        obs.save_live_checkpoint(
            context,
            _workflow(),
            reported_validations=[report],
            expected_revision=0,
        )


def test_validation_becomes_inapplicable_without_mutating_stored_report(repo_context):
    root, context = repo_context
    obs.save_live_checkpoint(
        context,
        _workflow(),
        relevant_paths=["README.md"],
        reported_validations=[{"label": "pytest", "outcome": "pass"}],
        expected_revision=0,
    )
    (root / "README.md").write_text("changed\n")

    loaded = obs.load_live_checkpoint(context)
    applicability = loaded.validation_applicability[0]
    assert applicability.applies_to_current_state is False
    assert applicability.reason == "captured_state_no_longer_matches"
    assert loaded.record.validations[0]["outcome"] == "pass"
    assert loaded.record.validations[0]["source"] == "reported"


def test_incomplete_validation_binding_never_matches(repo_context):
    _root, context = repo_context
    saved = obs.save_live_checkpoint(
        context,
        _workflow(),
        reported_validations=[{"label": "pytest", "outcome": "pass"}],
        expected_revision=0,
    )
    from core.db import connect
    connection = connect()
    row = connection.execute("SELECT payload FROM coding_checkpoints").fetchone()
    payload = json.loads(row["payload"])
    payload["validations"][0]["binding_complete"] = False
    connection.execute(
        "UPDATE coding_checkpoints SET payload = ?",
        (json.dumps(payload, sort_keys=True, separators=(",", ":")),),
    )
    connection.commit()
    connection.close()

    loaded = obs.load_live_checkpoint(context)
    assert loaded.validation_applicability[0].applies_to_current_state is False
    assert loaded.validation_applicability[0].reason == "binding_incomplete"
    assert loaded.record.target_key == saved.record.target_key


def test_workflow_text_does_not_change_state_reference(repo_context):
    _root, context = repo_context
    first = obs.save_live_checkpoint(
        context, _workflow(summary="first"), expected_revision=0
    )
    second = obs.save_live_checkpoint(
        context, _workflow(summary="different prose"), expected_revision=1
    )
    assert first.observation.state_ref == second.observation.state_ref


def test_head_advance_is_stale_and_mutation_proof_for_head_comparison(repo_context):
    root, context = repo_context
    obs.save_live_checkpoint(context, _workflow(), expected_revision=0)
    (root / "next.txt").write_text("next\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "advance")

    loaded = obs.load_live_checkpoint(context)
    assert loaded.freshness.state == obs.STALE
    assert "head_changed" in loaded.freshness.reasons


def test_branch_switch_same_head_is_stale(repo_context):
    root, context = repo_context
    obs.save_live_checkpoint(context, _workflow(), expected_revision=0)
    _git(root, "switch", "-q", "-c", "other")

    loaded = obs.load_live_checkpoint(context)
    assert "branch_changed" in loaded.freshness.reasons


def test_detached_switch_is_stale(repo_context):
    root, context = repo_context
    obs.save_live_checkpoint(context, _workflow(), expected_revision=0)
    _git(root, "checkout", "-q", "--detach", "HEAD")

    loaded = obs.load_live_checkpoint(context)
    assert "head_mode_changed" in loaded.freshness.reasons


def test_working_state_digest_comparison_is_load_bearing(repo_context):
    root, context = repo_context
    obs.save_live_checkpoint(context, _workflow(), expected_revision=0)
    (root / "README.md").write_text("dirty\n")

    loaded = obs.load_live_checkpoint(context)
    assert loaded.freshness.state == obs.STALE
    assert "working_state_changed" in loaded.freshness.reasons


def test_hidden_operation_start_and_end_affect_freshness(repo_context):
    root, context = repo_context
    obs.save_live_checkpoint(context, _workflow(), expected_revision=0)
    marker = _marker_path(root, "MERGE_HEAD")
    marker.write_text(_git(root, "rev-parse", "HEAD").stdout)

    during = obs.load_live_checkpoint(context)
    assert "operation_state_changed" in during.freshness.reasons
    marker.unlink()
    after = obs.load_live_checkpoint(context)
    assert after.freshness.state == obs.FRESH


def test_push_posture_change_is_reported_conservatively_stale(repo_context):
    root, context = repo_context
    obs.save_live_checkpoint(context, _workflow(), expected_revision=0)
    _git(root, "config", "push.default", "nothing")

    loaded = obs.load_live_checkpoint(context)
    assert "push_posture_changed" in loaded.freshness.reasons
    assert loaded.freshness.state == obs.STALE


def test_repo_disappears_at_same_root_reports_target_change(repo_context):
    root, context = repo_context
    obs.save_live_checkpoint(context, _workflow(), expected_revision=0)
    shutil.rmtree(root / ".git")

    loaded = obs.load_live_checkpoint(context)
    assert loaded.status == "ok"
    assert loaded.current.identity.kind == "directory"
    assert "target_identity_changed" in loaded.freshness.reasons
    assert "target_kind_changed" in loaded.freshness.reasons


def test_repo_disappears_when_binding_was_a_subdirectory_reports_target_change(
    repo_context,
):
    root, _context = repo_context
    subdirectory = root / "subdirectory"
    subdirectory.mkdir()
    context = save_project_binding("subproject", str(subdirectory))
    obs.save_live_checkpoint(context, _workflow(), expected_revision=0)
    shutil.rmtree(root / ".git")

    loaded = obs.load_live_checkpoint(context)
    assert loaded.status == "ok"
    assert loaded.current.identity.kind == "directory"
    assert "target_identity_changed" in loaded.freshness.reasons


def test_repository_created_inside_former_directory_target_is_stale(directory_context):
    root, context = directory_context
    obs.save_live_checkpoint(context, _workflow(), expected_revision=0)
    _init_repo(root)

    loaded = obs.load_live_checkpoint(context)
    assert loaded.status == "ok"
    assert loaded.current.identity.kind == "git"
    assert "target_kind_changed" in loaded.freshness.reasons


def test_directory_relevant_file_exact_match_and_change(directory_context):
    root, context = directory_context
    (root / "file.txt").write_text("one\n")
    obs.save_live_checkpoint(
        context, _workflow(), relevant_paths=["file.txt"], expected_revision=0
    )
    assert obs.load_live_checkpoint(context).freshness.state == obs.FRESH
    (root / "file.txt").write_text("two\n")
    assert "relevant_files_changed" in obs.load_live_checkpoint(context).freshness.reasons


def test_capture_retries_whole_sequence_once(repo_context, monkeypatch):
    root, context = repo_context
    stable = obs._capture_git_state(str(root))
    changed = replace(stable, status_digest="f" * 64)
    states = iter([stable, changed, stable, stable])
    monkeypatch.setattr(obs, "_capture_git_state", lambda _root: next(states))

    current = obs.capture_live_observation(context)
    assert current.status_digest == stable.status_digest


def test_second_capture_instability_fails_without_persisting(repo_context, monkeypatch):
    root, context = repo_context
    stable = obs._capture_git_state(str(root))
    changed = replace(stable, status_digest="f" * 64)
    states = iter([stable, changed, stable, changed])
    monkeypatch.setattr(obs, "_capture_git_state", lambda _root: next(states))

    with pytest.raises(obs.UnstableCapture):
        obs.save_live_checkpoint(context, _workflow(), expected_revision=0)
    assert not os.path.exists(config.DB_PATH)


def test_not_found_returns_bounded_result(repo_context):
    _root, context = repo_context
    loaded = obs.load_live_checkpoint(context)
    assert loaded.status == "not_found"
    assert loaded.record is None
    assert loaded.freshness == obs.FreshnessResult(
        obs.UNVERIFIABLE, ("checkpoint_not_found",)
    )


def test_corrupt_payload_returns_metadata_without_workflow_text(repo_context):
    _root, context = repo_context
    saved = obs.save_live_checkpoint(context, _workflow(), expected_revision=0)
    from core.db import connect
    connection = connect()
    connection.execute("UPDATE coding_checkpoints SET payload = ?", ("{malformed",))
    connection.commit()
    connection.close()

    loaded = obs.load_live_checkpoint(context)
    assert loaded.status == "corrupt"
    assert loaded.record is None
    assert loaded.current is None
    assert loaded.metadata.target_key_prefix == saved.record.target_key[:12]


def test_unsupported_schema_returns_safe_metadata(repo_context):
    _root, context = repo_context
    obs.save_live_checkpoint(context, _workflow(), expected_revision=0)
    from core.db import connect
    connection = connect()
    connection.execute("UPDATE coding_checkpoints SET schema_version = 999")
    connection.commit()
    connection.close()

    loaded = obs.load_live_checkpoint(context)
    assert loaded.status == "unsupported_schema"
    assert loaded.record is None
    assert loaded.metadata.schema_version == 999


def test_observation_facts_are_schema_validated_not_generic_metadata(repo_context):
    _root, context = repo_context
    saved = obs.save_live_checkpoint(context, _workflow(), expected_revision=0)
    assert saved.record.observation == saved.observation.observation_payload()
    assert set(saved.record.observation) == cp._OBSERVATION_KEYS


def test_no_model_facing_registration_or_existing_git_tool_dependency():
    source = open(obs.__file__, encoding="utf-8").read()
    assert "registry.register" not in source
    assert "tools.git_status" not in source
    assert "shell=True" not in source


# ===========================================================================
# CODING-06A3 corrective 1 -- content-sensitive validation_state_ref.
#
# The general state_ref (above) is Git-status-classification-sensitive
# only: it is built from status_digest, which for an already-dirty tracked
# path or an already-untracked path reflects only path + XY classification
# (+ the *index* object id for a tracked entry) -- never the worktree's
# actual current bytes. validation_state_ref is a narrower, validation-
# specific fingerprint layered on top for machine-evidence binding only;
# it must never replace or be conflated with the persisted checkpoint
# schema's own state_ref/observation semantics (unaffected by this suite).
# ===========================================================================

def test_clean_tree_has_complete_validation_state_ref(repo_context):
    _root, context = repo_context
    current = obs.capture_live_observation(context)
    assert current.validation_state_complete is True
    assert current.validation_state_ref is not None
    assert len(current.validation_state_ref) == 64


def test_directory_target_has_complete_validation_state_ref(directory_context):
    _root, context = directory_context
    current = obs.capture_live_observation(context)
    assert current.validation_state_complete is True
    assert current.validation_state_ref is not None


def test_modified_tracked_file_content_a_to_content_b_detected(repo_context):
    """The core corrective 1 proof: an ALREADY-modified tracked file's
    further content edit is invisible to `git status` (still just "M"),
    so the general state_ref never moves -- but validation_state_ref must."""
    root, context = repo_context
    (root / "README.md").write_text("content A\n")
    first = obs.capture_live_observation(context)
    (root / "README.md").write_text("content B\n")
    second = obs.capture_live_observation(context)

    assert first.state_ref == second.state_ref  # proves the exploit is real
    assert first.validation_state_ref != second.validation_state_ref  # and fixed


def test_untracked_file_content_a_to_content_b_detected(repo_context):
    root, context = repo_context
    (root / "scratch.txt").write_text("content A\n")
    first = obs.capture_live_observation(context)
    (root / "scratch.txt").write_text("content B\n")
    second = obs.capture_live_observation(context)

    assert first.state_ref == second.state_ref  # untracked path/status unchanged
    assert first.validation_state_ref != second.validation_state_ref


def test_clean_to_modified_tracked_file_also_changes_validation_state_ref(repo_context):
    root, context = repo_context
    clean = obs.capture_live_observation(context)
    (root / "README.md").write_text("modified once\n")
    dirty = obs.capture_live_observation(context)
    assert dirty.validation_state_ref != clean.validation_state_ref


def test_unchanged_dirty_content_remains_stable(repo_context):
    root, context = repo_context
    (root / "README.md").write_text("dirty\n")
    first = obs.capture_live_observation(context)
    second = obs.capture_live_observation(context)
    assert first.validation_state_ref == second.validation_state_ref
    assert first.validation_state_complete is True


def test_restore_exact_bytes_returns_same_validation_state_ref(repo_context):
    root, context = repo_context
    (root / "README.md").write_text("original\n")
    original = obs.capture_live_observation(context)

    (root / "README.md").write_text("different\n")
    changed = obs.capture_live_observation(context)
    assert changed.validation_state_ref != original.validation_state_ref

    (root / "README.md").write_text("original\n")
    restored = obs.capture_live_observation(context)
    assert restored.validation_state_ref == original.validation_state_ref


def test_deleted_tracked_file_detected_structurally_and_stable(repo_context):
    root, context = repo_context
    clean = obs.capture_live_observation(context)

    (root / "README.md").unlink()
    deleted = obs.capture_live_observation(context)
    assert deleted.validation_state_ref != clean.validation_state_ref
    assert deleted.validation_state_complete is True

    still_deleted = obs.capture_live_observation(context)
    assert still_deleted.validation_state_ref == deleted.validation_state_ref  # stable while missing

    # Recreating the EXACT original committed bytes returns to the EXACT
    # original fingerprint -- proves this is content-addressed, not merely
    # a "changed vs not changed" flag.
    (root / "README.md").write_text("hello\n")
    recreated = obs.capture_live_observation(context)
    assert recreated.validation_state_ref == clean.validation_state_ref


def test_symlink_retarget_detected_without_following(repo_context):
    root, context = repo_context
    (root / "target_b.txt").write_text("b\n")
    (root / "target_c.txt").write_text("c\n")
    link = root / "link.txt"
    try:
        link.symlink_to("target_a.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported in this environment")
    (root / "target_a.txt").write_text("a\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "add symlink")

    # Both retargets point somewhere OTHER than the committed target, so
    # Git's own "M" classification (and therefore the general state_ref)
    # is identical in both cases -- only the symlink's own target text
    # differs, and only validation_state_ref may see that.
    link.unlink()
    link.symlink_to("target_b.txt")
    first = obs.capture_live_observation(context)

    link.unlink()
    link.symlink_to("target_c.txt")
    second = obs.capture_live_observation(context)

    assert first.state_ref == second.state_ref
    assert first.validation_state_ref != second.validation_state_ref

    # And stable when unchanged.
    third = obs.capture_live_observation(context)
    assert third.validation_state_ref == second.validation_state_ref


def test_worktree_identity_isolates_validation_state_ref(tmp_path):
    root = tmp_path / "wtmain"
    root.mkdir()
    _init_repo(root)
    worktree = tmp_path / "wt"
    result = _git(root, "worktree", "add", "-b", "wt-branch", str(worktree), check=False)
    if result.returncode != 0:
        pytest.skip("git worktree unavailable in this environment")

    main_context = save_project_binding("wtmain", str(root))
    wt_context = save_project_binding("wtchild", str(worktree))

    main_current = obs.capture_live_observation(main_context)
    wt_current = obs.capture_live_observation(wt_context)
    assert main_current.identity.target_key != wt_current.identity.target_key
    assert main_current.validation_state_ref != wt_current.validation_state_ref

    (worktree / "only_in_worktree.txt").write_text("x\n")
    wt_dirty = obs.capture_live_observation(wt_context)
    assert wt_dirty.validation_state_ref != wt_current.validation_state_ref
    # The main worktree's own fingerprint is untouched by a change made
    # only in the linked worktree.
    main_unchanged = obs.capture_live_observation(main_context)
    assert main_unchanged.validation_state_ref == main_current.validation_state_ref


def test_changed_paths_truncated_makes_validation_state_incomplete(repo_context, monkeypatch):
    """Bounded/fail-closed: when the Git-visible changed-path list itself
    was truncated, the fingerprint cannot honestly claim full coverage of
    what changed -- it must report incomplete (None ref) rather than a
    partial, misleadingly-confident value."""
    root, context = repo_context
    (root / "dirty.txt").write_text("dirty\n")

    real_capture = obs._capture_git_state

    def _forced_truncated(path):
        return replace(real_capture(path), changed_paths_truncated=True)

    monkeypatch.setattr(obs, "_capture_git_state", _forced_truncated)
    current = obs.capture_live_observation(context)
    assert current.validation_state_complete is False
    assert current.validation_state_ref is None


# ===========================================================================
# CODING-06A3 final verification corrective -- validation_state_ref must
# also be Git index/status-sensitive, not content-sensitive alone.
#
# _validation_state_fingerprint()'s `entries` list is keyed by deduplicated
# path only, and each entry hashes current *worktree* bytes -- never which
# XY status produced them. Two observations whose Git-visible dirty paths
# happen to end up with byte-identical worktree content must still be
# distinguishable when Git's own index/status state differs (unstaged vs.
# staged vs. partially staged), because a machine-evidence record bound to
# one index state must never silently "apply" to a different index state
# just because the file bytes on disk currently match.
# ===========================================================================

def test_unstaged_vs_staged_same_bytes_validation_state_ref_differs(repo_context):
    """Same HEAD, same final worktree bytes -- unstaged (index==HEAD) vs.
    staged (index==worktree, `git add`) must NOT collide."""
    root, context = repo_context

    (root / "README.md").write_text("changed\n")
    unstaged = obs.capture_live_observation(context)

    _git(root, "checkout", "--", "README.md")
    (root / "README.md").write_text("changed\n")
    _git(root, "add", "README.md")
    staged = obs.capture_live_observation(context)

    assert unstaged.head == staged.head
    assert unstaged.validation_state_ref != staged.validation_state_ref


def test_partially_staged_vs_unstaged_only_same_final_bytes_differs(repo_context):
    """Same HEAD, same final worktree bytes, different index content:
    a partially-staged ("MM") path (index holds an intermediate edit,
    worktree holds a further one) vs. a path never staged at all whose
    worktree happens to land on that exact same final content. Git status
    classifies these differently (MM vs ".M") with a different index blob
    id -- the fingerprint must too."""
    root, context = repo_context

    _git(root, "reset", "--hard", "HEAD")
    (root / "README.md").write_text("intermediate\n")
    _git(root, "add", "README.md")
    (root / "README.md").write_text("final\n")
    mixed = obs.capture_live_observation(context)
    by_path = {item.path: item for item in mixed.changed_paths}
    assert by_path["README.md"].status == "MM"

    _git(root, "reset", "--hard", "HEAD")
    (root / "README.md").write_text("final\n")
    unstaged_only = obs.capture_live_observation(context)
    by_path = {item.path: item for item in unstaged_only.changed_paths}
    assert by_path["README.md"].status == ".M"  # porcelain v2: "." is unchanged, not space

    assert mixed.head == unstaged_only.head
    assert mixed.validation_state_ref != unstaged_only.validation_state_ref


# ===========================================================================
# CODING-08R.1: core.fsmonitor sentinel (this module's own read-only Git
# invocation surface shares core.git_review's exposure -- see
# core/git_review.py's "_GLOBAL_SAFE_ARGS" comment for the full empirical
# record). This module never calls `git diff` on worktree content (dirty
# content hashing is a direct filesystem read -- see obs._hash_regular_file_
# once), so the content-filter vector core.git_review needed does not apply
# here; only fsmonitor does.
# ===========================================================================

def test_configured_fsmonitor_does_not_execute_during_live_observation(repo_context, tmp_path):
    root, context = repo_context
    sentinel = tmp_path / "fsmonitor_ran"
    script = tmp_path / "fake_fsmonitor.sh"
    script.write_text(f"#!/bin/sh\ntouch '{sentinel}'\nexit 0\n")
    script.chmod(0o755)
    _git(root, "config", "core.fsmonitor", str(script))

    (root / "README.md").write_text("dirty change\n")
    obs.capture_live_observation(context)

    assert not sentinel.exists(), (
        "core.fsmonitor executed during live observation -- the "
        "-c core.fsmonitor= guard did not hold"
    )


def test_fsmonitor_removed_flag_would_execute_helper(repo_context, tmp_path, monkeypatch):
    """Companion proof: with the guard removed, the same observation path
    DOES invoke the configured fsmonitor hook -- empirical evidence that
    _GLOBAL_SAFE_ARGS's core.fsmonitor override is load-bearing here, not
    merely structural."""
    root, context = repo_context
    sentinel = tmp_path / "fsmonitor_ran"
    script = tmp_path / "fake_fsmonitor.sh"
    script.write_text(f"#!/bin/sh\ntouch '{sentinel}'\nexit 0\n")
    script.chmod(0o755)
    _git(root, "config", "core.fsmonitor", str(script))

    (root / "README.md").write_text("dirty change\n")
    monkeypatch.setattr(obs, "_GLOBAL_SAFE_ARGS", ())
    obs.capture_live_observation(context)

    assert sentinel.exists(), (
        "expected the configured core.fsmonitor hook to run once the guard "
        "was removed -- if it did not, the sentinel test above may be "
        "vacuous for this Git version"
    )
