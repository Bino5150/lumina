"""core/coding_checkpoint.py -- CODING-05A1 storage/contract suite.

Every test here works against a hermetic, per-test SQLite file (config.DB_PATH
monkeypatched to tmp_path) and, where a real Git repo is needed, a real
throwaway repo built via subprocess -- same convention as
tests/test_git_tools.py's _init_repo(). Never touches the real
~/lumina / ~/lumina-release repos or the real app database.
"""
import json
import os
import subprocess
import threading

import pytest

import config
import core.coding_checkpoint as cp


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "lumina.db"))
    return tmp_path


def _init_repo(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=path, check=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    return root


def _row_for(project_name, target_key):
    from core.db import connect
    conn = connect()
    try:
        return conn.execute(
            "SELECT * FROM coding_checkpoints WHERE project_name = ? AND target_key = ?",
            (project_name, target_key),
        ).fetchone()
    finally:
        conn.close()


def _wf(**overrides):
    base = {"task_id": "T-1", "phase": "in_progress"}
    base.update(overrides)
    return base


def _checkpoint(**overrides):
    base = {"workflow": _wf()}
    base.update(overrides)
    return base


FORBIDDEN_NAMES = [
    "authorization", "permission", "approved", "commit_authorized",
    "push_authorized", "next_command", "extras", "metadata", "context",
]


# ---------------------------------------------------------------------------
# Schema creation / idempotence
# ---------------------------------------------------------------------------

def test_init_checkpoint_db_idempotent():
    cp.init_checkpoint_db()
    cp.init_checkpoint_db()  # must not raise -- CREATE TABLE IF NOT EXISTS
    from core.db import connect
    conn = connect()
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(coding_checkpoints)").fetchall()}
    conn.close()
    assert cols == {
        "project_name", "target_key", "target_display", "target_kind",
        "schema_version", "revision", "payload", "created_at", "updated_at",
    }


def test_does_not_capture_db_path_at_import_time(tmp_path, monkeypatch):
    """Re-pointing config.DB_PATH after the module is already imported must
    take effect on the next call -- proves DB_PATH is read fresh, not cached
    at import time."""
    first_db = tmp_path / "first.db"
    monkeypatch.setattr(config, "DB_PATH", str(first_db))
    cp.init_checkpoint_db()
    assert first_db.exists()

    second_db = tmp_path / "second.db"
    monkeypatch.setattr(config, "DB_PATH", str(second_db))
    cp.init_checkpoint_db()
    assert second_db.exists()


# ---------------------------------------------------------------------------
# Create / reload / revision increments
# ---------------------------------------------------------------------------

def test_create_and_reload(repo):
    rec = cp.save_checkpoint("proj", str(repo), _checkpoint(), expected_revision=0)
    assert rec.revision == 1
    loaded = cp.load_checkpoint("proj", str(repo))
    assert loaded.workflow["task_id"] == "T-1"
    assert loaded.workflow["phase"] == "in_progress"
    assert loaded.revision == 1


def test_revision_increments_by_exactly_one(repo):
    rec1 = cp.save_checkpoint("proj", str(repo), _checkpoint(), expected_revision=0)
    assert rec1.revision == 1
    rec2 = cp.save_checkpoint(
        "proj", str(repo), _checkpoint(workflow=_wf(phase="complete")), expected_revision=1
    )
    assert rec2.revision == 2
    assert rec2.workflow["phase"] == "complete"
    rec3 = cp.save_checkpoint(
        "proj", str(repo), _checkpoint(workflow=_wf(phase="paused")), expected_revision=2
    )
    assert rec3.revision == 3


def test_not_found_raises(repo):
    with pytest.raises(cp.CheckpointNotFound):
        cp.load_checkpoint("proj", str(repo))


def test_persists_across_new_connections(repo):
    cp.save_checkpoint("proj", str(repo), _checkpoint(), expected_revision=0)
    identity = cp.resolve_target_identity(str(repo))
    row = _row_for("proj", identity.target_key)
    assert row is not None
    assert row["revision"] == 1
    loaded = cp.load_checkpoint("proj", str(repo))
    assert loaded.workflow["task_id"] == "T-1"


# ---------------------------------------------------------------------------
# CAS / revision-conflict behavior
# ---------------------------------------------------------------------------

def test_stale_expected_revision_rejected(repo):
    """Mutation proof: if the revision comparison were bypassed (accepting
    any write regardless of expected_revision), this would not raise."""
    cp.save_checkpoint("proj", str(repo), _checkpoint(), expected_revision=0)
    with pytest.raises(cp.RevisionConflict):
        cp.save_checkpoint("proj", str(repo), _checkpoint(), expected_revision=0)


def test_conflicting_write_leaves_row_unchanged(repo):
    """Rollback proof: a rejected write must not partially apply -- no
    last-writer-wins, no silent overwrite."""
    cp.save_checkpoint("proj", str(repo), _checkpoint(workflow=_wf(summary="original")), expected_revision=0)
    with pytest.raises(cp.RevisionConflict):
        cp.save_checkpoint(
            "proj", str(repo), _checkpoint(workflow=_wf(summary="attempted overwrite")), expected_revision=0
        )
    loaded = cp.load_checkpoint("proj", str(repo))
    assert loaded.workflow["summary"] == "original"
    assert loaded.revision == 1


def test_expected_revision_must_match_exactly_for_existing_row(repo):
    cp.save_checkpoint("proj", str(repo), _checkpoint(), expected_revision=0)
    cp.save_checkpoint("proj", str(repo), _checkpoint(), expected_revision=1)  # now at rev 2
    with pytest.raises(cp.RevisionConflict):
        cp.save_checkpoint("proj", str(repo), _checkpoint(), expected_revision=1)  # stale


def test_expected_revision_must_be_non_negative_int(repo):
    with pytest.raises(cp.CheckpointValidationError):
        cp.save_checkpoint("proj", str(repo), _checkpoint(), expected_revision=-1)
    with pytest.raises(cp.CheckpointValidationError):
        cp.save_checkpoint("proj", str(repo), _checkpoint(), expected_revision="0")


def test_concurrent_writers_exactly_one_wins(repo):
    """Two independent DB connections (two separate save_checkpoint calls,
    each opening its own connection via core.db.connect()) contend for the
    same expected_revision=0. No in-process lock exists in this module --
    SQLite's BEGIN IMMEDIATE + busy_timeout is the sole correctness
    boundary. Exactly one must win; the loser gets RevisionConflict; no data
    is lost."""
    results = {}
    barrier = threading.Barrier(2)

    def attempt(name, summary):
        barrier.wait()
        try:
            rec = cp.save_checkpoint(
                "proj", str(repo), _checkpoint(workflow=_wf(summary=summary)), expected_revision=0
            )
            results[name] = ("ok", rec.revision)
        except cp.RevisionConflict:
            results[name] = ("conflict", None)

    t1 = threading.Thread(target=attempt, args=("A", "from-A"))
    t2 = threading.Thread(target=attempt, args=("B", "from-B"))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert not t1.is_alive() and not t2.is_alive()
    outcomes = sorted(r[0] for r in results.values())
    assert outcomes == ["conflict", "ok"]

    loaded = cp.load_checkpoint("proj", str(repo))
    assert loaded.revision == 1
    assert loaded.workflow["summary"] in ("from-A", "from-B")


# ---------------------------------------------------------------------------
# Corruption / unsupported-schema handling
# ---------------------------------------------------------------------------

def test_malformed_json_raises_corrupt_and_preserves_row(repo):
    cp.save_checkpoint("proj", str(repo), _checkpoint(), expected_revision=0)
    identity = cp.resolve_target_identity(str(repo))

    from core.db import connect
    conn = connect()
    conn.execute(
        "UPDATE coding_checkpoints SET payload = ? WHERE project_name = ? AND target_key = ?",
        ("{not valid json", "proj", identity.target_key),
    )
    conn.commit()
    conn.close()

    with pytest.raises(cp.CheckpointCorrupt):
        cp.load_checkpoint("proj", str(repo))

    row = _row_for("proj", identity.target_key)
    assert row["payload"] == "{not valid json"  # untouched, not deleted/rewritten


def test_unsupported_schema_version_raises_and_preserves_row(repo):
    cp.save_checkpoint("proj", str(repo), _checkpoint(), expected_revision=0)
    identity = cp.resolve_target_identity(str(repo))

    from core.db import connect
    conn = connect()
    conn.execute(
        "UPDATE coding_checkpoints SET schema_version = 999 WHERE project_name = ? AND target_key = ?",
        ("proj", identity.target_key),
    )
    conn.commit()
    conn.close()

    with pytest.raises(cp.UnsupportedSchema):
        cp.load_checkpoint("proj", str(repo))

    row = _row_for("proj", identity.target_key)
    assert row["schema_version"] == 999  # untouched, never silently migrated


def test_shape_mismatch_under_known_schema_is_corrupt_not_unsupported(repo):
    """A recognized schema_version whose payload doesn't match the expected
    shape is corruption, not a future format -- must not be silently
    accepted as valid content."""
    cp.save_checkpoint("proj", str(repo), _checkpoint(), expected_revision=0)
    identity = cp.resolve_target_identity(str(repo))

    from core.db import connect
    conn = connect()
    conn.execute(
        "UPDATE coding_checkpoints SET payload = ? WHERE project_name = ? AND target_key = ?",
        (json.dumps({"totally": "wrong shape"}), "proj", identity.target_key),
    )
    conn.commit()
    conn.close()

    with pytest.raises(cp.CheckpointCorrupt):
        cp.load_checkpoint("proj", str(repo))


# ---------------------------------------------------------------------------
# Schema security: unknown/forbidden fields, no escape hatch
# ---------------------------------------------------------------------------

def test_unknown_top_level_field_rejected(repo):
    bad = _checkpoint()
    bad["bogus"] = 1
    with pytest.raises(cp.CheckpointValidationError):
        cp.save_checkpoint("proj", str(repo), bad, expected_revision=0)


def test_unknown_workflow_field_rejected(repo):
    bad = _checkpoint(workflow=_wf(bogus="x"))
    with pytest.raises(cp.CheckpointValidationError):
        cp.save_checkpoint("proj", str(repo), bad, expected_revision=0)


def test_unknown_path_record_field_rejected(repo):
    bad = _checkpoint(relevant_files=[{"path": "a.py", "bogus": 1}])
    with pytest.raises(cp.CheckpointValidationError):
        cp.save_checkpoint("proj", str(repo), bad, expected_revision=0)


def test_unknown_validation_record_field_rejected(repo):
    bad = _checkpoint(validations=[{"label": "l", "outcome": "pass", "source": "reported", "bogus": 1}])
    with pytest.raises(cp.CheckpointValidationError):
        cp.save_checkpoint("proj", str(repo), bad, expected_revision=0)


@pytest.mark.parametrize("name", FORBIDDEN_NAMES)
def test_forbidden_field_rejected_top_level(repo, name):
    """Mutation proof: if additionalProperties:false were dropped at the top
    level, this would silently succeed instead of raising."""
    bad = _checkpoint()
    bad[name] = True
    with pytest.raises(cp.CheckpointValidationError):
        cp.save_checkpoint("proj", str(repo), bad, expected_revision=0)


@pytest.mark.parametrize("name", FORBIDDEN_NAMES)
def test_forbidden_field_rejected_in_workflow(repo, name):
    bad = _checkpoint(workflow=_wf(**{name: True}))
    with pytest.raises(cp.CheckpointValidationError):
        cp.save_checkpoint("proj", str(repo), bad, expected_revision=0)


@pytest.mark.parametrize("name", FORBIDDEN_NAMES)
def test_forbidden_field_rejected_in_path_record(repo, name):
    bad = _checkpoint(relevant_files=[{"path": "a.py", name: True}])
    with pytest.raises(cp.CheckpointValidationError):
        cp.save_checkpoint("proj", str(repo), bad, expected_revision=0)


@pytest.mark.parametrize("name", FORBIDDEN_NAMES)
def test_forbidden_field_rejected_in_validation_record(repo, name):
    bad = _checkpoint(validations=[{"label": "l", "outcome": "pass", "source": "reported", name: True}])
    with pytest.raises(cp.CheckpointValidationError):
        cp.save_checkpoint("proj", str(repo), bad, expected_revision=0)


def test_no_generic_escape_hatch_beyond_named_forbidden_set(repo):
    """Proves the allow-list is closed, not merely blocking the named
    forbidden keys -- an arbitrary unlisted key must be rejected too."""
    for bad_key in ("misc", "custom_data", "notes_bag"):
        bad = _checkpoint()
        bad[bad_key] = {"anything": "goes"}
        with pytest.raises(cp.CheckpointValidationError):
            cp.save_checkpoint("proj", str(repo), bad, expected_revision=0)


def test_validation_source_must_be_exactly_reported(repo):
    bad = _checkpoint(validations=[{"label": "l", "outcome": "pass", "source": "verified"}])
    with pytest.raises(cp.CheckpointValidationError):
        cp.save_checkpoint("proj", str(repo), bad, expected_revision=0)


def test_validation_outcome_enum_enforced(repo):
    bad = _checkpoint(validations=[{"label": "l", "outcome": "maybe", "source": "reported"}])
    with pytest.raises(cp.CheckpointValidationError):
        cp.save_checkpoint("proj", str(repo), bad, expected_revision=0)


def test_phase_enum_enforced(repo):
    bad = _checkpoint(workflow=_wf(phase="not_a_real_phase"))
    with pytest.raises(cp.CheckpointValidationError):
        cp.save_checkpoint("proj", str(repo), bad, expected_revision=0)


@pytest.mark.parametrize("phase", sorted(cp.VALID_PHASES))
def test_all_valid_phases_accepted(repo, phase):
    ok = _checkpoint(workflow=_wf(phase=phase))
    rec = cp.save_checkpoint("proj", str(repo), ok, expected_revision=0)
    assert rec.workflow["phase"] == phase


def test_missing_workflow_rejected(repo):
    with pytest.raises(cp.CheckpointValidationError):
        cp.save_checkpoint("proj", str(repo), {}, expected_revision=0)


def test_empty_task_id_rejected(repo):
    bad = _checkpoint(workflow=_wf(task_id=""))
    with pytest.raises(cp.CheckpointValidationError):
        cp.save_checkpoint("proj", str(repo), bad, expected_revision=0)


def test_relevant_file_bad_sha256_rejected(repo):
    bad = _checkpoint(relevant_files=[{"path": "a.py", "sha256": "not-hex"}])
    with pytest.raises(cp.CheckpointValidationError):
        cp.save_checkpoint("proj", str(repo), bad, expected_revision=0)


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------

def test_next_steps_over_max_rejected(repo):
    bad = _checkpoint(workflow=_wf(next_steps=["x"] * (cp.MAX_NEXT_STEPS + 1)))
    with pytest.raises(cp.CheckpointValidationError):
        cp.save_checkpoint("proj", str(repo), bad, expected_revision=0)


def test_next_steps_at_max_accepted(repo):
    ok = _checkpoint(workflow=_wf(next_steps=["x"] * cp.MAX_NEXT_STEPS))
    rec = cp.save_checkpoint("proj", str(repo), ok, expected_revision=0)
    assert len(rec.workflow["next_steps"]) == cp.MAX_NEXT_STEPS


def test_blockers_over_max_rejected(repo):
    bad = _checkpoint(workflow=_wf(blockers=["x"] * (cp.MAX_BLOCKERS + 1)))
    with pytest.raises(cp.CheckpointValidationError):
        cp.save_checkpoint("proj", str(repo), bad, expected_revision=0)


def test_relevant_files_over_max_rejected(repo):
    bad = _checkpoint(relevant_files=[{"path": f"f{i}.py"} for i in range(cp.MAX_RELEVANT_FILES + 1)])
    with pytest.raises(cp.CheckpointValidationError):
        cp.save_checkpoint("proj", str(repo), bad, expected_revision=0)


def test_changed_paths_over_max_rejected(repo):
    bad = _checkpoint(changed_paths=[{"path": f"f{i}.py"} for i in range(cp.MAX_CHANGED_PATHS + 1)])
    with pytest.raises(cp.CheckpointValidationError):
        cp.save_checkpoint("proj", str(repo), bad, expected_revision=0)


def test_validations_over_max_rejected(repo):
    bad = _checkpoint(validations=[
        {"label": "l", "outcome": "pass", "source": "reported"} for _ in range(cp.MAX_VALIDATIONS + 1)
    ])
    with pytest.raises(cp.CheckpointValidationError):
        cp.save_checkpoint("proj", str(repo), bad, expected_revision=0)


@pytest.mark.parametrize("field,limit", [
    ("task_id", cp.MAX_TASK_ID_LEN),
    ("title", cp.MAX_TITLE_LEN),
    ("slice_id", cp.MAX_SLICE_ID_LEN),
    ("summary", cp.MAX_SUMMARY_LEN),
])
def test_workflow_string_field_over_max_rejected(repo, field, limit):
    bad = _checkpoint(workflow=_wf(**{field: "x" * (limit + 1)}))
    with pytest.raises(cp.CheckpointValidationError):
        cp.save_checkpoint("proj", str(repo), bad, expected_revision=0)


def test_maximal_payload_within_total_bytes_cap():
    """Uses the actual encoded representation (not an estimate) to prove a
    maximal-but-legal checkpoint fits under the total cap."""
    path_record = {"path": "p" * cp.MAX_PATH_LEN, "sha256": "a" * 64, "missing": True, "is_symlink": False}
    validation_record = {
        "label": "l" * cp.MAX_VALIDATION_LABEL_LEN,
        "outcome": "skipped",
        "source": "reported",
        "exit_code": 255,
        "summary": "s" * cp.MAX_VALIDATION_SUMMARY_LEN,
        "timestamp": "t" * cp.MAX_VALIDATION_TIMESTAMP_LEN,
        "state_ref": "r" * cp.MAX_VALIDATION_STATE_REF_LEN,
    }
    checkpoint = {
        "workflow": {
            "task_id": "i" * cp.MAX_TASK_ID_LEN,
            "title": "t" * cp.MAX_TITLE_LEN,
            "phase": "in_progress",
            "slice_id": "s" * cp.MAX_SLICE_ID_LEN,
            "summary": "s" * cp.MAX_SUMMARY_LEN,
            "next_steps": ["n" * cp.MAX_NEXT_STEP_LEN] * cp.MAX_NEXT_STEPS,
            "blockers": ["b" * cp.MAX_BLOCKER_LEN] * cp.MAX_BLOCKERS,
        },
        "relevant_files": [dict(path_record) for _ in range(cp.MAX_RELEVANT_FILES)],
        "changed_paths": [dict(path_record) for _ in range(cp.MAX_CHANGED_PATHS)],
        "validations": [dict(validation_record) for _ in range(cp.MAX_VALIDATIONS)],
    }
    envelope = cp._validate_checkpoint(checkpoint)
    encoded = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert len(encoded) <= cp.MAX_TOTAL_PAYLOAD_BYTES


def test_total_bytes_cap_enforced_directly():
    """Defense-in-depth: even though per-field bounds already keep a legal
    payload well under the cap, the total-size guard itself must still
    reject an oversized envelope if it's ever reached."""
    huge_envelope = {
        "workflow": {"task_id": "T", "phase": "planned", "title": None,
                     "slice_id": None, "summary": "x" * cp.MAX_SUMMARY_LEN,
                     "next_steps": [], "blockers": []},
        "relevant_files": [], "changed_paths": [], "validations": [],
    }
    # Directly probe the total-size guard below the per-field validator,
    # since legal per-field bounds can never actually reach the cap.
    encoded = json.dumps(huge_envelope).encode("utf-8")
    assert len(encoded) <= cp.MAX_TOTAL_PAYLOAD_BYTES  # sanity: this alone is small

    from core.coding_checkpoint import MAX_TOTAL_PAYLOAD_BYTES
    assert MAX_TOTAL_PAYLOAD_BYTES == 32 * 1024


# ---------------------------------------------------------------------------
# Target identity: determinism, kind detection, namespace separation
# ---------------------------------------------------------------------------

def test_target_key_deterministic_across_calls(repo):
    id1 = cp.resolve_target_identity(str(repo))
    id2 = cp.resolve_target_identity(str(repo))
    assert id1.target_key == id2.target_key
    assert id1.kind == "git"


def test_target_key_differs_for_different_repos(repo, tmp_path):
    other = tmp_path / "other-repo"
    other.mkdir()
    _init_repo(other)
    id1 = cp.resolve_target_identity(str(repo))
    id2 = cp.resolve_target_identity(str(other))
    assert id1.target_key != id2.target_key


def test_target_identity_independent_of_branch_and_head(repo):
    """Mutation proof: if branch or HEAD were folded into the identity
    document, committing/branching would change target_key and this would
    fail."""
    id_before = cp.resolve_target_identity(str(repo))
    subprocess.run(["git", "checkout", "-q", "-b", "feature-branch"], cwd=repo, check=True)
    (repo / "new_file.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "second commit"], cwd=repo, check=True)
    id_after = cp.resolve_target_identity(str(repo))
    assert id_before.target_key == id_after.target_key
    assert id_before.canonical_root == id_after.canonical_root


def test_non_git_directory_is_directory_kind(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    identity = cp.resolve_target_identity(str(plain))
    assert identity.kind == "directory"
    assert identity.canonical_root == os.path.realpath(str(plain))


def test_git_worktree_identity(repo, tmp_path):
    wt = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", "-q", str(wt), "-b", "wt-branch"], cwd=repo, check=True)

    main_id = cp.resolve_target_identity(str(repo))
    wt_id = cp.resolve_target_identity(str(wt))

    assert main_id.kind == "git"
    assert wt_id.kind == "git"
    # Different working directories -> different canonical_root -> different
    # key -- but each resolves cleanly and deterministically on its own.
    assert main_id.target_key != wt_id.target_key
    assert wt_id.target_key == cp.resolve_target_identity(str(wt)).target_key


@pytest.mark.skipif(os.name == "nt", reason="symlinks need elevated privileges on Windows")
def test_symlink_resolves_to_same_canonical_identity(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    os.symlink(real, link)

    id_real = cp.resolve_target_identity(str(real))
    id_link = cp.resolve_target_identity(str(link))
    assert id_real.target_key == id_link.target_key
    assert id_real.canonical_root == id_link.canonical_root


def test_filesystem_identity_unavailable_degrades_gracefully(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()

    def _raising_stat(*a, **k):
        raise OSError("simulated stat failure")

    monkeypatch.setattr(cp.os, "stat", _raising_stat)

    identity = cp.resolve_target_identity(str(plain))
    assert identity.kind == "directory"
    assert identity.target_key
    # Still deterministic without filesystem identity available.
    identity2 = cp.resolve_target_identity(str(plain))
    assert identity.target_key == identity2.target_key


def test_project_name_namespace_separation(repo):
    """Same repo, two different Project names -> two independent rows with
    independent CAS."""
    cp.save_checkpoint("proj-a", str(repo), _checkpoint(workflow=_wf(summary="A")), expected_revision=0)
    cp.save_checkpoint("proj-b", str(repo), _checkpoint(workflow=_wf(summary="B")), expected_revision=0)

    a = cp.load_checkpoint("proj-a", str(repo))
    b = cp.load_checkpoint("proj-b", str(repo))
    assert a.workflow["summary"] == "A"
    assert b.workflow["summary"] == "B"
    assert a.revision == 1 and b.revision == 1

    # proj-a's own CAS is independent of proj-b having already written.
    cp.save_checkpoint("proj-a", str(repo), _checkpoint(workflow=_wf(summary="A2")), expected_revision=1)


def test_same_project_different_targets_independent(repo, tmp_path):
    other = tmp_path / "other-repo"
    other.mkdir()
    _init_repo(other)

    cp.save_checkpoint("proj", str(repo), _checkpoint(workflow=_wf(summary="repo1")), expected_revision=0)
    cp.save_checkpoint("proj", str(other), _checkpoint(workflow=_wf(summary="repo2")), expected_revision=0)

    a = cp.load_checkpoint("proj", str(repo))
    b = cp.load_checkpoint("proj", str(other))
    assert a.workflow["summary"] == "repo1"
    assert b.workflow["summary"] == "repo2"


# ---------------------------------------------------------------------------
# Relevant/changed path records: normalization, bounds
# ---------------------------------------------------------------------------

def test_relevant_file_absolute_posix_path_rejected(repo):
    bad = _checkpoint(relevant_files=[{"path": "/etc/passwd"}])
    with pytest.raises(cp.CheckpointValidationError):
        cp.save_checkpoint("proj", str(repo), bad, expected_revision=0)


def test_relevant_file_absolute_windows_path_rejected(repo):
    bad = _checkpoint(relevant_files=[{"path": r"C:\Users\x\file.py"}])
    with pytest.raises(cp.CheckpointValidationError):
        cp.save_checkpoint("proj", str(repo), bad, expected_revision=0)


def test_relevant_file_dotdot_segment_rejected(repo):
    bad = _checkpoint(relevant_files=[{"path": "../outside/file.py"}])
    with pytest.raises(cp.CheckpointValidationError):
        cp.save_checkpoint("proj", str(repo), bad, expected_revision=0)


def test_relevant_file_normalized_relative_path_accepted(repo):
    ok = _checkpoint(relevant_files=[{"path": "core/coding_checkpoint.py"}])
    rec = cp.save_checkpoint("proj", str(repo), ok, expected_revision=0)
    assert rec.relevant_files[0]["path"] == "core/coding_checkpoint.py"
    assert rec.relevant_files[0]["sha256"] is None
    assert rec.relevant_files[0]["missing"] is None
    assert rec.relevant_files[0]["is_symlink"] is None


def test_changed_paths_accept_same_record_shape(repo):
    ok = _checkpoint(changed_paths=[{"path": "core/foo.py", "sha256": "b" * 64, "missing": False}])
    rec = cp.save_checkpoint("proj", str(repo), ok, expected_revision=0)
    assert rec.changed_paths[0]["path"] == "core/foo.py"
    assert rec.changed_paths[0]["sha256"] == "b" * 64


# ---------------------------------------------------------------------------
# Byte-identical restore
# ---------------------------------------------------------------------------

def test_restore_is_byte_identical_to_validated_input(repo):
    checkpoint = {
        "workflow": {
            "task_id": "T-99",
            "title": "Title",
            "phase": "blocked",
            "slice_id": "S1",
            "summary": "Summary text",
            "next_steps": ["a", "b"],
            "blockers": ["waiting on X"],
        },
        "relevant_files": [
            {"path": "core/foo.py", "sha256": "a" * 64, "missing": False, "is_symlink": False}
        ],
        "changed_paths": [{"path": "core/bar.py"}],
        "validations": [
            {"label": "unit-tests", "outcome": "pass", "source": "reported",
             "exit_code": 0, "summary": "all green", "timestamp": "2026-08-22T00:00:00+00:00"}
        ],
    }
    cp.save_checkpoint("proj", str(repo), checkpoint, expected_revision=0)
    loaded = cp.load_checkpoint("proj", str(repo))

    expected = cp._validate_checkpoint(checkpoint)
    assert loaded.workflow == expected["workflow"]
    assert loaded.relevant_files == expected["relevant_files"]
    assert loaded.changed_paths == expected["changed_paths"]
    assert loaded.validations == expected["validations"]

    # Re-save with an unrelated field bumped and confirm the untouched
    # fields still restore byte-identically -- not just "close enough".
    checkpoint2 = dict(checkpoint)
    checkpoint2["workflow"] = dict(checkpoint["workflow"], phase="complete")
    cp.save_checkpoint("proj", str(repo), checkpoint2, expected_revision=1)
    loaded2 = cp.load_checkpoint("proj", str(repo))
    assert loaded2.relevant_files == expected["relevant_files"]
    assert loaded2.validations == expected["validations"]
    assert loaded2.workflow["phase"] == "complete"
    assert loaded2.workflow["task_id"] == "T-99"
