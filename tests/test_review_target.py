"""CODING-08A4 core.review_target tests. Qt-free -- no PySide6 dependency,
matching core/review_target.py's own module docstring rationale.

Every Git mutation is confined to pytest-owned repositories and worktrees.
The production release/dev topology is never a source or target here.
"""

import os
import subprocess

import pytest

import core.persistence as persistence
import core.project_context as project_context_module
from core import worktree_manager
from core.project_context import ProjectContext, ProjectContextState
import core.review_target as review_target


def _git(cwd, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=check, capture_output=True, text=True,
    )


def _repo(path):
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "A4 Test")
    _git(path, "config", "user.email", "a4@example.invalid")
    (path / "tracked.txt").write_text("line1\n", encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-qm", "initial")
    return path


@pytest.fixture
def hermetic(tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(
        project_context_module, "PROJECT_BINDINGS_DIR", str(tmp_path / "bindings"),
    )
    monkeypatch.setattr(worktree_manager, "_PROTECTED_ENGINEERING_ROOTS", frozenset())
    worktree_manager._reset_for_tests()
    yield tmp_path
    worktree_manager._reset_for_tests()


# ===========================================================================
# Active Project
# ===========================================================================

def test_active_project_resolves(hermetic):
    repo = _repo(hermetic / "repo")
    project_context_module.save_project_binding("proj", str(repo))
    state = ProjectContextState(ProjectContext(name="proj", root=str(repo)))

    target = review_target.resolve_active_project_target(state)

    assert target.identity.kind == "git"
    assert target.identity.canonical_root == str(repo)
    assert target.project_name == "proj"
    assert target.worktree_id is None
    assert "proj" in target.label


def test_no_project_state_raises_no_active_project():
    with pytest.raises(review_target.NoActiveProject):
        review_target.resolve_active_project_target(None)


def test_no_snapshot_raises_no_active_project(hermetic):
    state = ProjectContextState(None)
    with pytest.raises(review_target.NoActiveProject):
        review_target.resolve_active_project_target(state)


def test_stale_project_binding_raises(hermetic):
    repo = _repo(hermetic / "repo")
    other = _repo(hermetic / "other")
    project_context_module.save_project_binding("proj", str(repo))
    # Snapshot captured before the durable binding was repointed elsewhere.
    state = ProjectContextState(ProjectContext(name="proj", root=str(repo)))
    project_context_module.save_project_binding("proj", str(other))

    with pytest.raises(review_target.StaleProjectBinding):
        review_target.resolve_active_project_target(state)


def test_active_project_non_git_raises_not_a_git_target(hermetic):
    plain = hermetic / "plain"
    plain.mkdir()
    project_context_module.save_project_binding("proj", str(plain))
    state = ProjectContextState(ProjectContext(name="proj", root=str(plain)))

    with pytest.raises(review_target.NotAGitTarget):
        review_target.resolve_active_project_target(state)


# ===========================================================================
# Explicit path
# ===========================================================================

def test_explicit_path_resolves(hermetic):
    repo = _repo(hermetic / "repo")
    target = review_target.resolve_explicit_path_target(str(repo))
    assert target.identity.kind == "git"
    assert target.identity.canonical_root == str(repo)
    assert target.project_name is None
    assert target.worktree_id is None


def test_explicit_path_missing_raises_invalid_path(hermetic):
    with pytest.raises(review_target.InvalidExplicitPath):
        review_target.resolve_explicit_path_target(str(hermetic / "does-not-exist"))


def test_explicit_path_non_git_raises_not_a_git_target(hermetic):
    plain = hermetic / "plain"
    plain.mkdir()
    with pytest.raises(review_target.NotAGitTarget):
        review_target.resolve_explicit_path_target(str(plain))


def test_explicit_path_expands_user_and_relative(hermetic, monkeypatch):
    repo = _repo(hermetic / "repo")
    monkeypatch.chdir(str(hermetic))
    target = review_target.resolve_explicit_path_target("repo")
    assert target.identity.canonical_root == str(repo)


# ===========================================================================
# Managed worktree
# ===========================================================================

def test_worktree_target_resolves(hermetic):
    repo = _repo(hermetic / "repo")
    result = worktree_manager.create_worktree(str(repo), "HEAD")
    assert result.status == "created"
    worktree_id = result.handle.worktree_id

    target = review_target.resolve_worktree_target(worktree_id)
    assert target.identity.kind == "git"
    assert target.identity.canonical_root == result.handle.worktree_root
    assert target.worktree_id == worktree_id
    assert target.project_name is None


def test_worktree_target_unknown_id_raises_unavailable(hermetic):
    with pytest.raises(review_target.WorktreeUnavailable):
        review_target.resolve_worktree_target("wt-000000000000000000000000")


def test_worktree_target_removed_raises_unavailable(hermetic):
    repo = _repo(hermetic / "repo")
    result = worktree_manager.create_worktree(str(repo), "HEAD")
    worktree_id = result.handle.worktree_id
    _git(repo, "worktree", "remove", "--force", result.handle.worktree_root)

    with pytest.raises(review_target.WorktreeUnavailable):
        review_target.resolve_worktree_target(worktree_id)


def test_worktree_target_stale_head_raises_unavailable(hermetic):
    repo = _repo(hermetic / "repo")
    result = worktree_manager.create_worktree(str(repo), "HEAD")
    worktree_id = result.handle.worktree_id
    root = result.handle.worktree_root
    with open(os.path.join(root, "tracked.txt"), "a", encoding="utf-8") as f:
        f.write("advance\n")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-qm", "advance")

    with pytest.raises(review_target.WorktreeUnavailable):
        review_target.resolve_worktree_target(worktree_id)


def test_worktree_target_replaced_same_path_raises_unavailable(hermetic):
    repo = _repo(hermetic / "repo")
    result = worktree_manager.create_worktree(str(repo), "HEAD")
    worktree_id = result.handle.worktree_id
    root = result.handle.worktree_root
    _git(repo, "worktree", "remove", "--force", root)
    os.makedirs(root)
    _git(root, "init", "-q")

    with pytest.raises(review_target.WorktreeUnavailable):
        review_target.resolve_worktree_target(worktree_id)


# ===========================================================================
# list_managed_worktrees
# ===========================================================================

def test_list_managed_worktrees_reflects_created_worktree(hermetic):
    repo = _repo(hermetic / "repo")
    result = worktree_manager.create_worktree(str(repo), "HEAD")
    worktree_id = result.handle.worktree_id

    statuses = review_target.list_managed_worktrees()

    ids = {status.handle.worktree_id for status in statuses}
    assert worktree_id in ids


def test_list_managed_worktrees_empty_when_none_created(hermetic):
    assert review_target.list_managed_worktrees() == ()
