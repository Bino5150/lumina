"""CODING-08A3 model-facing read-only Git review tool acceptance and attack
tests.

Every Git mutation is confined to pytest-owned repositories and worktrees.
The production release/dev topology is never a source or target here.
"""

import ast
import json
import os
import subprocess

import pytest

import config
import core.git_review_snapshot as git_review_snapshot
import core.persistence as persistence
import core.project_context as project_context_module
import core.secrets as secrets_module
import tools.projects as projects_module
import tools.review as review_module
from core import emergency_stop, process_manager, worktree_manager
from core.agent import LuminaAgent
from core.project_context import ProjectContext, ProjectContextState
from tools import subagent as subagent_module
from tools import tasks as tasks_module
from tools.registry import ToolRegistry
from tools.review import register_review_tools
from tools.subagent import register_subagent_tools
from tools.tasks import register_task_tools
from tools.worktrees import register_worktree_tools


# ---------------------------------------------------------------------------
# Fixtures / helpers.
# ---------------------------------------------------------------------------

def _git(cwd, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=check, capture_output=True, text=True,
    )


def _repo(path):
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "Lumina A3 Test")
    _git(path, "config", "user.email", "lumina-a3@example.invalid")
    (path / "tracked.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-qm", "initial")
    return path


def _make_tracked_project(tmp_path, name, root=None):
    os.makedirs(os.path.join(projects_module.PROJECTS_DIR, name))
    concrete_root = root if root is not None else _repo(tmp_path / f"{name}-root")
    project_context_module.save_project_binding(name, str(concrete_root))
    return str(concrete_root)


class _Harness:
    def __init__(self, repo, state, registry, resolver, created):
        self.repo = repo
        self.state = state
        self.registry = registry
        self.resolver = resolver
        self.worktree_id = created["worktree_id"]
        self.root = created["worktree_root"]


@pytest.fixture
def managed(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "lumina.db"))
    monkeypatch.setattr(config, "SUBAGENTS_ENABLED", True)
    monkeypatch.setattr(config, "BACKGROUND_TASKS_ENABLED", True)
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(secrets_module, "SECRETS_PATH", str(tmp_path / "credentials.json"))
    monkeypatch.setattr(
        project_context_module, "PROJECT_BINDINGS_DIR", str(tmp_path / "bindings"),
    )
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    projectlist = projects_dir / "projectlist.md"
    projectlist.write_text("# Projects\n\n", encoding="utf-8")
    monkeypatch.setattr(projects_module, "PROJECTS_DIR", str(projects_dir))
    monkeypatch.setattr(projects_module, "PROJECTLIST", str(projectlist))
    monkeypatch.setattr(projects_module, "PROJECT_CHATS_DIR", str(tmp_path / "project-chats"))
    monkeypatch.setattr(worktree_manager, "_PROTECTED_ENGINEERING_ROOTS", frozenset())
    process_manager._reset_for_tests()
    emergency_stop._reset_for_tests()
    worktree_manager._reset_for_tests()
    review_module._reset_for_tests()

    repo = _repo(tmp_path / "repo")
    os.makedirs(os.path.join(projects_dir, "owner-project"))
    project_context_module.save_project_binding("owner-project", str(repo))
    owner_context = ProjectContext(name="owner-project", root=str(repo))
    state = ProjectContextState(owner_context)
    registry = ToolRegistry()
    resolver = register_worktree_tools(registry, project_state=state)
    register_review_tools(registry, owner=True, project_state=state, worktree_resolver=resolver)
    created = json.loads(registry.call(
        "create_worktree", {"source_repository": str(repo), "base": "HEAD"},
    ))
    assert created["status"] == "created"
    harness = _Harness(repo, state, registry, resolver, created)
    yield harness

    for handle in tuple(worktree_manager._registry.values()):
        _git(handle.source_root, "worktree", "unlock", handle.worktree_root, check=False)
        _git(
            handle.source_root, "worktree", "remove", "--force",
            handle.worktree_root, check=False,
        )
    process_manager._reset_for_tests()
    worktree_manager._reset_for_tests()
    review_module._reset_for_tests()
    if emergency_stop.is_latched() and emergency_stop.can_rearm():
        emergency_stop.rearm_local()
    emergency_stop._reset_for_tests()


def _non_owner_registry(review_target_grant=None):
    registry = ToolRegistry()
    register_review_tools(registry, owner=False, review_target_grant=review_target_grant)
    return registry


def _call(registry, name, payload=None):
    return json.loads(registry.call(name, payload or {}))


class _ReviewingChild(LuminaAgent):
    """Real LuminaAgent construction (real registry/tool-profile wiring)
    with chat() short-circuited to directly exercise this child's own
    review_changes/review_file_diff -- no live LLM backend needed, mirrors
    tests/test_subagent.py's _RealAgentNoChat / tests/test_worktree_subagent_
    dispatch.py's _EditingChild pattern."""

    def chat(self, task):
        return self.registry.call("review_changes", ast.literal_eval(task))


# ===========================================================================
# OWNER TARGETING
# ===========================================================================

def test_owner_active_project_default(managed):
    result = _call(managed.registry, "review_changes", {})
    assert result["status"] == "current"
    assert result["target"]["canonical_root"] == str(managed.repo)


def test_owner_stale_project_binding_rejects(tmp_path, managed):
    other_root = _repo(tmp_path / "elsewhere")
    project_context_module.save_project_binding("owner-project", str(other_root))
    result = _call(managed.registry, "review_changes", {})
    assert result["status"] == "target_unavailable"


def test_owner_explicit_cwd(managed):
    result = _call(managed.registry, "review_changes", {"cwd": str(managed.repo)})
    assert result["status"] == "current"
    assert result["target"]["canonical_root"] == str(managed.repo)


def test_owner_explicit_cwd_wins_over_stale_project(tmp_path, managed):
    other_root = _repo(tmp_path / "elsewhere")
    project_context_module.save_project_binding("owner-project", str(other_root))
    result = _call(managed.registry, "review_changes", {"cwd": str(managed.repo)})
    assert result["status"] == "current"


def test_owner_managed_worktree_id(managed):
    result = _call(managed.registry, "review_changes", {"worktree_id": managed.worktree_id})
    assert result["status"] == "current"
    assert result["target"]["canonical_root"] == managed.root


def test_owner_cwd_and_worktree_id_ambiguous_rejects(managed):
    result = _call(managed.registry, "review_changes", {
        "cwd": str(managed.repo), "worktree_id": managed.worktree_id,
    })
    assert result["status"] == "malformed_request"


def test_owner_no_target_at_all_rejects(managed):
    empty_registry = ToolRegistry()
    register_review_tools(empty_registry, owner=True)
    result = _call(empty_registry, "review_changes", {})
    assert result["status"] == "malformed_request"


def test_owner_stale_worktree_handle_rejects(managed):
    (os.path.join(managed.root, "tracked.txt"))
    with open(os.path.join(managed.root, "tracked.txt"), "a", encoding="utf-8") as f:
        f.write("advance\n")
    _git(managed.root, "add", "tracked.txt")
    _git(managed.root, "commit", "-qm", "advance")
    result = _call(managed.registry, "review_changes", {"worktree_id": managed.worktree_id})
    assert result["status"] == "target_unavailable"


def test_owner_removed_worktree_rejects(managed):
    _git(managed.repo, "worktree", "remove", "--force", managed.root)
    result = _call(managed.registry, "review_changes", {"worktree_id": managed.worktree_id})
    assert result["status"] == "target_unavailable"


def test_owner_unknown_worktree_id_rejects(managed):
    result = _call(managed.registry, "review_changes", {
        "worktree_id": "wt-000000000000000000000000",
    })
    assert result["status"] == "target_unavailable"


# ===========================================================================
# NON-OWNER
# ===========================================================================

def test_non_owner_without_grant_is_refused(managed):
    registry = _non_owner_registry(review_target_grant=None)
    result = _call(registry, "review_changes", {})
    assert result["status"] == "unauthorized_target"


def test_non_owner_coding_profile_alone_grants_no_target(managed):
    """Enabling review_changes through tools_enabled/a profile is a SEPARATE
    axis from target authority (module docstring). No review_target_grant
    means unauthorized_target regardless of which tools are enabled."""
    registry = ToolRegistry()
    register_review_tools(registry, owner=False, review_target_grant=None)
    registry.set_disabled([])  # simulate "every tool enabled" via profile/PIN
    result = _call(registry, "review_changes", {})
    assert result["status"] == "unauthorized_target"


def test_non_owner_cannot_supply_cwd(managed):
    grant = review_module.checkpoint_store.resolve_target_identity(str(managed.root))
    registry = _non_owner_registry(review_target_grant=grant)
    result = _call(registry, "review_changes", {"cwd": str(managed.repo)})
    assert result["status"] == "unauthorized_target"


def test_non_owner_cannot_supply_worktree_id(managed):
    grant = review_module.checkpoint_store.resolve_target_identity(str(managed.root))
    registry = _non_owner_registry(review_target_grant=grant)
    result = _call(registry, "review_changes", {"worktree_id": managed.worktree_id})
    assert result["status"] == "unauthorized_target"


def test_immediate_child_reviews_exact_granted_target(managed, monkeypatch):
    monkeypatch.setattr(subagent_module, "LuminaAgent", _ReviewingChild)
    spawn_registry = ToolRegistry()
    register_subagent_tools(
        spawn_registry, parent_depth=0, project_state=managed.state,
        worktree_resolver=managed.resolver,
    )
    result = ast.literal_eval(spawn_registry.call("spawn_subagent", {
        "task": repr({}),
        "tools_enabled": ["review_changes", "review_file_diff"],
        "worktree_id": managed.worktree_id,
    }))
    assert result["success"] is True
    payload = json.loads(result["result"])
    assert payload["status"] == "current"
    assert payload["target"]["canonical_root"] == managed.root


def test_background_child_reviews_exact_granted_target(managed, monkeypatch):
    monkeypatch.setattr(subagent_module, "LuminaAgent", _ReviewingChild)
    captured = {}

    def _run_inline(fn, *args, **kwargs):
        captured["result"] = fn(*args, **kwargs)
        return "task-a3"

    monkeypatch.setattr(tasks_module, "submit_task", _run_inline)
    task_registry = ToolRegistry()

    class _Agent:
        project_context = managed.state
        _background_task_ids = set()

    register_task_tools(task_registry, _Agent(), worktree_resolver=managed.resolver)
    dispatch = task_registry.call("run_background_subagent", {
        "task": repr({}), "tools_enabled": ["review_changes"],
        "worktree_id": managed.worktree_id,
    })
    assert ast.literal_eval(dispatch)["task_id"] == "task-a3"
    result = captured["result"]
    assert result["success"] is True
    payload = json.loads(result["result"])
    assert payload["status"] == "current"
    assert payload["target"]["canonical_root"] == managed.root


def test_dispatched_child_owner_is_false(managed, monkeypatch):
    children = []

    class _Spy(_ReviewingChild):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            children.append(self)

    monkeypatch.setattr(subagent_module, "LuminaAgent", _Spy)
    spawn_registry = ToolRegistry()
    register_subagent_tools(
        spawn_registry, parent_depth=0, project_state=managed.state,
        worktree_resolver=managed.resolver,
    )
    ast.literal_eval(spawn_registry.call("spawn_subagent", {
        "task": repr({}), "tools_enabled": ["review_changes"],
        "worktree_id": managed.worktree_id,
    }))
    assert children[0].owner is False


def test_child_without_worktree_dispatch_has_no_review_authority(tmp_path, managed, monkeypatch):
    """project= dispatch (not worktree_id) never sets a review_target_grant
    -- confirms review authority is exclusively the managed-worktree path,
    even though the dispatched child DOES get a real, valid ProjectContext
    (a tracked project pointing at the owner's own repo). A synthetic or
    tracked ProjectContext is ergonomic path defaulting, never review
    authority by itself -- this is the direct test of that invariant."""
    tracked_root = _make_tracked_project(tmp_path, "sibling-project", root=managed.repo)
    assert tracked_root == str(managed.repo)
    monkeypatch.setattr(subagent_module, "LuminaAgent", _ReviewingChild)
    spawn_registry = ToolRegistry()
    register_subagent_tools(
        spawn_registry, parent_depth=0, project_state=managed.state,
        worktree_resolver=managed.resolver,
    )
    result = ast.literal_eval(spawn_registry.call("spawn_subagent", {
        "task": repr({}), "tools_enabled": ["review_changes"],
        "project": "sibling-project",
    }))
    assert result["success"] is True
    payload = json.loads(result["result"])
    assert payload["status"] == "unauthorized_target"


def test_child_cannot_review_sibling_worktree_via_selector(managed, monkeypatch):
    other = json.loads(managed.registry.call(
        "create_worktree", {"source_repository": str(managed.repo), "base": "HEAD"},
    ))
    assert other["status"] == "created"

    monkeypatch.setattr(subagent_module, "LuminaAgent", _ReviewingChild)
    spawn_registry = ToolRegistry()
    register_subagent_tools(
        spawn_registry, parent_depth=0, project_state=managed.state,
        worktree_resolver=managed.resolver,
    )
    result = ast.literal_eval(spawn_registry.call("spawn_subagent", {
        "task": repr({"worktree_id": other["worktree_id"]}),
        "tools_enabled": ["review_changes"],
        "worktree_id": managed.worktree_id,
    }))
    payload = json.loads(result["result"])
    assert payload["status"] == "unauthorized_target"


def test_dispatch_preserves_owner_project_state(managed, monkeypatch):
    monkeypatch.setattr(subagent_module, "LuminaAgent", _ReviewingChild)
    before = managed.state.snapshot()
    spawn_registry = ToolRegistry()
    register_subagent_tools(
        spawn_registry, parent_depth=0, project_state=managed.state,
        worktree_resolver=managed.resolver,
    )
    ast.literal_eval(spawn_registry.call("spawn_subagent", {
        "task": repr({}), "tools_enabled": ["review_changes"],
        "worktree_id": managed.worktree_id,
    }))
    assert managed.state.snapshot() == before


# ===========================================================================
# SNAPSHOT AUTHORITY
# ===========================================================================

def test_owner_snapshot_usable_by_owner(managed):
    first = _call(managed.registry, "review_changes", {})
    ref = first["snapshot_ref"]
    second = _call(managed.registry, "review_changes", {"snapshot_ref": ref})
    assert second["status"] == "current"
    assert second["snapshot_ref"] == ref


def test_child_snapshot_usable_by_same_exact_target_child(managed, monkeypatch):
    monkeypatch.setattr(subagent_module, "LuminaAgent", _ReviewingChild)
    spawn_registry = ToolRegistry()
    register_subagent_tools(
        spawn_registry, parent_depth=0, project_state=managed.state,
        worktree_resolver=managed.resolver,
    )
    first = ast.literal_eval(spawn_registry.call("spawn_subagent", {
        "task": repr({}), "tools_enabled": ["review_changes"],
        "worktree_id": managed.worktree_id,
    }))
    ref = json.loads(first["result"])["snapshot_ref"]

    second = ast.literal_eval(spawn_registry.call("spawn_subagent", {
        "task": repr({"snapshot_ref": ref}), "tools_enabled": ["review_changes"],
        "worktree_id": managed.worktree_id,
    }))
    payload = json.loads(second["result"])
    assert payload["status"] == "current"
    assert payload["snapshot_ref"] == ref


def test_snapshot_ref_copied_to_unrelated_non_owner_is_refused(managed):
    owner_result = _call(managed.registry, "review_changes", {})
    ref = owner_result["snapshot_ref"]

    unrelated_registry = _non_owner_registry(review_target_grant=None)
    result = _call(unrelated_registry, "review_changes", {"snapshot_ref": ref})
    assert result["status"] == "unauthorized_target"


def test_sibling_child_snapshot_ref_is_refused(managed, monkeypatch):
    other = json.loads(managed.registry.call(
        "create_worktree", {"source_repository": str(managed.repo), "base": "HEAD"},
    ))
    assert other["status"] == "created"

    monkeypatch.setattr(subagent_module, "LuminaAgent", _ReviewingChild)
    spawn_registry = ToolRegistry()
    register_subagent_tools(
        spawn_registry, parent_depth=0, project_state=managed.state,
        worktree_resolver=managed.resolver,
    )
    child_a = ast.literal_eval(spawn_registry.call("spawn_subagent", {
        "task": repr({}), "tools_enabled": ["review_changes"],
        "worktree_id": managed.worktree_id,
    }))
    ref_a = json.loads(child_a["result"])["snapshot_ref"]

    child_b = ast.literal_eval(spawn_registry.call("spawn_subagent", {
        "task": repr({"snapshot_ref": ref_a}), "tools_enabled": ["review_changes"],
        "worktree_id": other["worktree_id"],
    }))
    payload_b = json.loads(child_b["result"])
    assert payload_b["status"] == "unauthorized_target"


def test_stale_snapshot_remains_stale(managed):
    first = _call(managed.registry, "review_changes", {})
    ref = first["snapshot_ref"]
    with open(os.path.join(str(managed.repo), "tracked.txt"), "a", encoding="utf-8") as f:
        f.write("dirty\n")
    stale = _call(managed.registry, "review_changes", {"snapshot_ref": ref})
    assert stale["status"] == "stale"
    # Sticky latch: reverting the change does not un-stale the ref.
    _git(managed.repo, "checkout", "--", "tracked.txt")
    still_stale = _call(managed.registry, "review_changes", {"snapshot_ref": ref})
    assert still_stale["status"] == "stale"


def test_removed_target_returns_unavailable_via_snapshot_ref(managed):
    first = _call(managed.registry, "review_changes", {"worktree_id": managed.worktree_id})
    ref = first["snapshot_ref"]
    _git(managed.repo, "worktree", "remove", "--force", managed.root)
    result = _call(managed.registry, "review_changes", {"snapshot_ref": ref})
    assert result["status"] == "target_unavailable"


def test_snapshot_ref_never_retargets(managed):
    first = _call(managed.registry, "review_changes", {})
    ref = first["snapshot_ref"]
    result = _call(managed.registry, "review_changes", {
        "snapshot_ref": ref, "cwd": managed.root,
    })
    assert result["status"] == "malformed_request"


def test_unknown_snapshot_ref(managed):
    result = _call(managed.registry, "review_changes", {"snapshot_ref": "revsnap-doesnotexist"})
    assert result["status"] == "unknown_snapshot"


# ===========================================================================
# CHANGE INVENTORY
# ===========================================================================

def test_staged_and_unstaged_and_untracked_changes(managed):
    repo = str(managed.repo)
    with open(os.path.join(repo, "tracked.txt"), "a", encoding="utf-8") as f:
        f.write("staged-change\n")
    _git(managed.repo, "add", "tracked.txt")
    with open(os.path.join(repo, "tracked.txt"), "a", encoding="utf-8") as f:
        f.write("unstaged-change\n")
    with open(os.path.join(repo, "new_untracked.txt"), "w", encoding="utf-8") as f:
        f.write("brand new\n")

    result = _call(managed.registry, "review_changes", {"cwd": repo})
    assert result["status"] == "current"
    by_path = {c["display_path"]: c for c in result["changes"]}
    tracked = by_path["tracked.txt"]
    assert tracked["staged"] is True and tracked["unstaged"] is True
    assert tracked["staged_diff"]["insertions"] == 1
    assert tracked["unstaged_diff"]["insertions"] == 1
    untracked = by_path["new_untracked.txt"]
    assert untracked["untracked"] is True
    assert untracked["staged"] is False and untracked["unstaged"] is False


def test_rename_and_copy(managed):
    repo = str(managed.repo)
    _git(managed.repo, "mv", "tracked.txt", "renamed.txt")
    result = _call(managed.registry, "review_changes", {"cwd": repo})
    renamed = next(c for c in result["changes"] if c["display_path"] == "renamed.txt")
    assert renamed["relation"] == "rename"
    assert renamed["display_original_path"] == "tracked.txt"


def test_binary_change(managed):
    repo = str(managed.repo)
    with open(os.path.join(repo, "binfile.bin"), "wb") as f:
        f.write(bytes(range(256)))
    _git(managed.repo, "add", "binfile.bin")
    result = _call(managed.registry, "review_changes", {"cwd": repo})
    entry = next(c for c in result["changes"] if c["display_path"] == "binfile.bin")
    assert entry["staged_diff"]["binary"] is True
    assert entry["staged_diff"]["insertions"] is None


def test_symlink_change(managed):
    repo = str(managed.repo)
    os.symlink("tracked.txt", os.path.join(repo, "link.txt"))
    result = _call(managed.registry, "review_changes", {"cwd": repo})
    entry = next(c for c in result["changes"] if c["display_path"] == "link.txt")
    assert entry["untracked"] is True


def test_submodule_gitlink_change(tmp_path, managed):
    inner = _repo(tmp_path / "inner")
    outer = _repo(tmp_path / "outer")
    _git(outer, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(inner), "sub")
    _git(outer / "sub", "config", "user.email", "test@example.com")
    _git(outer / "sub", "config", "user.name", "Test")
    _git(outer, "commit", "-qm", "add submodule")
    with open(str(outer / "sub" / "tracked.txt"), "a", encoding="utf-8") as f:
        f.write("inner change\n")
    _git(outer / "sub", "add", "tracked.txt")

    result = _call(managed.registry, "review_changes", {"cwd": str(outer)})
    entry = next(c for c in result["changes"] if c["display_path"] == "sub")
    assert entry["submodule"]["is_submodule"] is True
    assert entry["content_omission_reason"] == "nested_submodule_content_not_captured"
    assert result["status"] == "current_metadata_only"
    assert "nested_submodule_content_not_captured" in result["omissions"]


def test_unmerged_conflict_change(tmp_path, managed):
    root = tmp_path / "conflict"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "f.txt").write_text("base\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "base")
    _git(root, "checkout", "-q", "-b", "branch-a")
    (root / "f.txt").write_text("a-version\n")
    _git(root, "commit", "-qam", "a")
    _git(root, "checkout", "-q", "main")
    (root / "f.txt").write_text("b-version\n")
    _git(root, "commit", "-qam", "b")
    _git(root, "merge", "branch-a", "-q", check=False)

    result = _call(managed.registry, "review_changes", {"cwd": str(root)})
    entry = next(c for c in result["changes"] if c["display_path"] == "f.txt")
    assert entry["relation"] == "unmerged"
    assert len(entry["unmerged_stages"]) == 3
    assert result["status"] == "current_metadata_only"


# ===========================================================================
# FILE RETRIEVAL
# ===========================================================================

def test_review_file_diff_both_layers(managed):
    repo = str(managed.repo)
    with open(os.path.join(repo, "tracked.txt"), "a", encoding="utf-8") as f:
        f.write("staged\n")
    _git(managed.repo, "add", "tracked.txt")
    with open(os.path.join(repo, "tracked.txt"), "a", encoding="utf-8") as f:
        f.write("unstaged\n")

    snap = _call(managed.registry, "review_changes", {"cwd": repo})
    change_id = next(c["change_id"] for c in snap["changes"] if c["display_path"] == "tracked.txt")

    staged = _call(managed.registry, "review_file_diff", {
        "snapshot_ref": snap["snapshot_ref"], "change_id": change_id, "layer": "staged",
    })
    assert staged["status"] == "current"
    assert staged["complete"] is True
    assert any(line["kind"] == "add" and line["text"] == "staged" for h in staged["hunks"] for line in h["lines"])

    unstaged = _call(managed.registry, "review_file_diff", {
        "snapshot_ref": snap["snapshot_ref"], "change_id": change_id, "layer": "unstaged",
    })
    assert unstaged["status"] == "current"
    assert any(line["text"] == "unstaged" for h in unstaged["hunks"] for line in h["lines"])


def test_review_file_diff_wrong_layer(managed):
    repo = str(managed.repo)
    with open(os.path.join(repo, "tracked.txt"), "a", encoding="utf-8") as f:
        f.write("staged-only\n")
    _git(managed.repo, "add", "tracked.txt")

    snap = _call(managed.registry, "review_changes", {"cwd": repo})
    change_id = next(c["change_id"] for c in snap["changes"] if c["display_path"] == "tracked.txt")
    result = _call(managed.registry, "review_file_diff", {
        "snapshot_ref": snap["snapshot_ref"], "change_id": change_id, "layer": "unstaged",
    })
    assert result["status"] == "invalid_layer"


def test_review_file_diff_unknown_change(managed):
    snap = _call(managed.registry, "review_changes", {})
    result = _call(managed.registry, "review_file_diff", {
        "snapshot_ref": snap["snapshot_ref"], "change_id": "0" * 16, "layer": "staged",
    })
    assert result["status"] == "unknown_change"


def test_review_file_diff_pagination(managed):
    repo = str(managed.repo)
    lines = [f"line-{i}\n" for i in range(200)]
    with open(os.path.join(repo, "tracked.txt"), "w", encoding="utf-8") as f:
        f.writelines(lines)
    _git(managed.repo, "add", "tracked.txt")

    snap = _call(managed.registry, "review_changes", {"cwd": repo})
    change_id = next(c["change_id"] for c in snap["changes"] if c["display_path"] == "tracked.txt")
    first = _call(managed.registry, "review_file_diff", {
        "snapshot_ref": snap["snapshot_ref"], "change_id": change_id, "layer": "staged",
    })
    assert first["status"] == "current"
    assert len(first["hunks"]) >= 1


def test_review_file_diff_snapshot_authority_enforced(managed, monkeypatch):
    """Reuses the SNAPSHOT AUTHORITY invariant for review_file_diff
    specifically -- not just review_changes."""
    owner_result = _call(managed.registry, "review_changes", {})
    ref = owner_result["snapshot_ref"]
    change_id = owner_result["changes"][0]["change_id"] if owner_result["changes"] else None

    unrelated_registry = _non_owner_registry(review_target_grant=None)
    result = _call(unrelated_registry, "review_file_diff", {
        "snapshot_ref": ref, "change_id": change_id or "x" * 16, "layer": "staged",
    })
    assert result["status"] == "unauthorized_target"


def test_review_file_diff_stale_before_returns_no_content(managed):
    repo = str(managed.repo)
    with open(os.path.join(repo, "tracked.txt"), "a", encoding="utf-8") as f:
        f.write("edit\n")
    _git(managed.repo, "add", "tracked.txt")
    snap = _call(managed.registry, "review_changes", {"cwd": repo})
    change_id = next(c["change_id"] for c in snap["changes"] if c["display_path"] == "tracked.txt")

    with open(os.path.join(repo, "tracked.txt"), "a", encoding="utf-8") as f:
        f.write("more\n")

    result = _call(managed.registry, "review_file_diff", {
        "snapshot_ref": snap["snapshot_ref"], "change_id": change_id, "layer": "staged",
    })
    assert result["status"] == "stale"
    assert "hunks" not in result or result.get("hunks") in (None, [])


# ===========================================================================
# SERIALIZATION
# ===========================================================================

def test_every_result_is_valid_json(managed):
    calls = [
        ("review_changes", {}),
        ("review_changes", {"cwd": str(managed.repo)}),
        ("review_changes", {"worktree_id": managed.worktree_id}),
        ("review_changes", {"snapshot_ref": "bogus"}),
        ("review_file_diff", {"snapshot_ref": "bogus", "change_id": "x", "layer": "staged"}),
    ]
    for name, payload in calls:
        raw = managed.registry.call(name, payload)
        json.loads(raw)  # raises if not valid JSON


def test_many_changed_files_paginate(managed):
    repo = str(managed.repo)
    for i in range(120):
        with open(os.path.join(repo, f"file_{i:03d}.txt"), "w", encoding="utf-8") as f:
            f.write("x\n")
    result = _call(managed.registry, "review_changes", {"cwd": repo})
    assert result["total_changes"] == 120
    assert len(result["changes"]) <= review_module.DEFAULT_CHANGES_LIMIT
    assert result["next_cursor"] is not None

    second = _call(managed.registry, "review_changes", {
        "cwd": repo, "cursor": result["next_cursor"],
    })
    assert second["total_changes"] == 120


def test_huge_path_is_truncated_in_display():
    # A real single path COMPONENT can't exceed the filesystem's NAME_MAX
    # (~255 bytes on ext4), but a full relative PATH (many nested short
    # directories) legitimately can -- unit-test _display_path directly
    # rather than trying to create an unrealistic single filename on disk.
    long_path = "/".join(["dir"] * 400) + "/file.txt"
    assert len(long_path) > review_module.MAX_DISPLAY_PATH_CHARS
    rendered = review_module._display_path(long_path)
    assert len(rendered) <= review_module.MAX_DISPLAY_PATH_CHARS + len("...[truncated]")
    assert rendered.endswith("...[truncated]")


def test_huge_filename_within_filesystem_limits_round_trips(managed):
    repo = str(managed.repo)
    long_name = "a" * 200 + ".txt"
    with open(os.path.join(repo, long_name), "w", encoding="utf-8") as f:
        f.write("x\n")
    result = _call(managed.registry, "review_changes", {"cwd": repo})
    entry = next(c for c in result["changes"] if c["display_path"].startswith("a" * 50))
    assert entry["display_path"] == long_name


def test_hostile_control_and_bidi_filename_is_escaped(managed):
    repo = str(managed.repo)
    hostile = "innocent‮\ttxt.exe"
    path = os.path.join(repo, hostile)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("x\n")
    except OSError:
        pytest.skip("filesystem rejects this filename")
    result = _call(managed.registry, "review_changes", {"cwd": repo})
    entry = next(c for c in result["changes"] if c["display_path"].startswith("innocent"))
    assert "‮" not in entry["display_path"]
    assert "\t" not in entry["display_path"]
    assert "\\u202e" in entry["display_path"]
    # The raw JSON string itself must never contain the literal control/bidi
    # character either -- only its escaped textual form.
    raw = managed.registry.call("review_changes", {"cwd": repo})
    assert "‮" not in raw


def test_display_path_unit_surrogate_escape():
    # A1 decodes invalid path bytes with surrogateescape; confirm A3 never
    # lets a lone surrogate reach json.dumps as a raw codepoint.
    raw_with_invalid_byte = "bad" + chr(0xDCFF) + "name.txt"
    rendered = review_module._display_path(raw_with_invalid_byte)
    json.dumps(rendered)  # must not raise
    assert "\\xff" in rendered


def test_huge_diff_trims_hunks_for_output_budget(monkeypatch):
    """Direct unit test of _render_file_diff's local trimming -- constructs
    a synthetic FileDiffResult far larger than a tight TOOL_RESULT_MAX_CHARS
    budget and confirms whole hunks are dropped (never mid-hunk), next_cursor
    points at the first dropped hunk, and omission_reason/complete reflect
    the LOCAL trim truthfully."""
    hunks = tuple(
        git_review_snapshot.DiffHunk(
            header=git_review_snapshot.DiffHunkHeader(
                old_start=i, old_count=1, new_start=i, new_count=1, section_heading="",
            ),
            lines=(git_review_snapshot.DiffLine(kind="add", text="x" * 200),),
            byte_size=210, omitted=False, omission_reason=None,
        )
        for i in range(50)
    )
    file_result = git_review_snapshot.FileDiffResult(
        change_id="deadbeef00000000", path="big.txt", layer="staged",
        binary=False, hunks=hunks, complete=True, omitted_hunks=0,
        omission_reason=None, total_bytes=sum(h.byte_size for h in hunks), next_cursor=None,
    )
    applicability = git_review_snapshot.ReviewApplicability(git_review_snapshot.CURRENT, ())
    outcome = git_review_snapshot.RetrievalOutcome(
        status=git_review_snapshot.CURRENT, file=file_result, applicability=applicability,
    )

    class _Cfg:
        TOOL_RESULT_MAX_CHARS = 800

    monkeypatch.setattr(review_module, "config", _Cfg)
    encoded = review_module._render_file_diff("revsnap-x", "deadbeef00000000", "staged", 0, outcome)
    payload = json.loads(encoded)
    assert len(encoded) <= 800
    assert len(payload["hunks"]) < 50
    assert payload["complete"] is False
    assert payload["next_cursor"] == len(payload["hunks"])
    assert payload["omission_reason"] == review_module.REASON_LOCAL_OUTPUT_BUDGET


@pytest.mark.parametrize("budget", [0, 1, 2, 5])
def test_exact_output_limit_pressure_still_valid_json(monkeypatch, budget):
    class _Cfg:
        TOOL_RESULT_MAX_CHARS = budget

    monkeypatch.setattr(review_module, "config", _Cfg)
    encoded = review_module._status_json("target_unavailable", message="x" * 500)
    json.loads(encoded) if encoded else None
    assert len(encoded) <= max(budget, 0)


def test_incomplete_page_says_so_explicitly(managed):
    repo = str(managed.repo)
    for i in range(5):
        with open(os.path.join(repo, f"f{i}.txt"), "w", encoding="utf-8") as f:
            f.write("x\n")
    result = _call(managed.registry, "review_changes", {"cwd": repo, "limit": 2})
    assert result["complete_page"] is False
    assert result["next_cursor"] == 2


def test_repository_content_untrusted_marker_present(managed):
    changes = _call(managed.registry, "review_changes", {})
    assert changes["repository_content_untrusted"] is True

    with open(os.path.join(str(managed.repo), "tracked.txt"), "a", encoding="utf-8") as f:
        f.write("edit\n")
    _git(managed.repo, "add", "tracked.txt")
    snap = _call(managed.registry, "review_changes", {"cwd": str(managed.repo)})
    change_id = next(c["change_id"] for c in snap["changes"] if c["display_path"] == "tracked.txt")
    diff = _call(managed.registry, "review_file_diff", {
        "snapshot_ref": snap["snapshot_ref"], "change_id": change_id, "layer": "staged",
    })
    assert diff["repository_content_untrusted"] is True

    error = _call(managed.registry, "review_changes", {"snapshot_ref": "revsnap-bogus"})
    assert error["repository_content_untrusted"] is True


def test_review_file_diff_selector_is_change_id_never_raw_path(managed):
    """review_file_diff's registered contract is exactly
    (snapshot_ref, change_id, layer, start_hunk_index) -- a raw path
    argument is not a recognized selector and must be refused, never
    silently accepted as an alternate way to name content."""
    snap = _call(managed.registry, "review_changes", {})
    result = _call(managed.registry, "review_file_diff", {
        "snapshot_ref": snap["snapshot_ref"], "change_id": "x" * 16,
        "layer": "staged", "path": "tracked.txt",
    })
    assert result["status"] == "malformed_request"


# ===========================================================================
# AUTHORITY (structural)
# ===========================================================================

_FORBIDDEN_SUBSTRINGS = (
    "worktree_manager.create_worktree",
    "worktree_manager.remove_worktree",
    "save_project_binding",
    "save_coding_checkpoint",
    "record_test_evidence",
    "run_bounded_git",
    "subprocess",
    "os.system",
    "eval(",
    "exec(",
    "\"git\", \"add\"",
    "\"git\", \"commit\"",
    "\"git\", \"push\"",
    "\"git\", \"merge\"",
    "\"git\", \"checkout\"",
    "\"git\", \"reset\"",
    "\"git\", \"clean\"",
)


def test_review_module_contains_no_mutation_path():
    source = open(os.path.join(os.path.dirname(__file__), "..", "tools", "review.py"),
                  encoding="utf-8").read()
    for forbidden in _FORBIDDEN_SUBSTRINGS:
        assert forbidden not in source, f"found forbidden reference: {forbidden!r}"


def test_review_module_only_imports_read_kernels():
    source = open(os.path.join(os.path.dirname(__file__), "..", "tools", "review.py"),
                  encoding="utf-8").read()
    assert "import core.worktree_manager" not in source
    assert "from core import worktree_manager" not in source
    assert "core.coding_validation_evidence" not in source


# ===========================================================================
# CODING-08R.1: close the R-H test-oracle gap. The static substring check
# above only ever proves tools/review.py's own SOURCE never mentions a
# mutation-shaped call -- CODING-08R's own literal mutation (inserting a
# real checkpoint_store.save_checkpoint() call into review_changes) proved
# it does not detect a genuine durable write introduced there, since it is
# a dynamic property, not a static one. This is the dynamic regression:
# real review_changes/review_file_diff calls against isolated on-disk
# state, asserting no durable mutation occurred anywhere reachable --
# checkpoint rows, evidence rows, Project binding files, or the worktree
# manager's own runtime registry/ledger.
# ===========================================================================

def _table_row_count(table: str) -> int:
    """0 if the table does not exist at all -- review must never even cause
    it to be created, let alone populated."""
    import sqlite3
    from core.db import connect
    try:
        conn = connect()
    except sqlite3.OperationalError:
        return 0
    try:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,),
        )
        if cursor.fetchone() is None:
            return 0
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _binding_dir_snapshot(path: str) -> dict:
    if not os.path.isdir(path):
        return {}
    snapshot = {}
    for dirpath, _dirnames, filenames in os.walk(path):
        for fname in filenames:
            full = os.path.join(dirpath, fname)
            snapshot[full] = (os.path.getsize(full), os.path.getmtime(full))
    return snapshot


def test_review_read_paths_cause_no_durable_state_mutation(managed):
    """Defends the invariant directly, not by grepping for one specific
    past mutation: a review READ (capture + retrieval, owner and non-owner)
    can never acquire an undocumented durable-state side effect anywhere
    reachable from this process."""
    before_checkpoints = _table_row_count("coding_checkpoints")
    before_evidence = _table_row_count("coding_validation_evidence")
    before_bindings = _binding_dir_snapshot(project_context_module.PROJECT_BINDINGS_DIR)
    before_registry = dict(worktree_manager._registry)
    before_project = managed.state.snapshot()

    (managed.repo / "tracked.txt").write_text("line1\nCHANGED\nline3\n", encoding="utf-8")
    snap = _call(managed.registry, "review_changes", {})
    assert snap["changes"], "expected a real dirty change to exercise retrieval too"
    assert snap["status"] in ("current", "current_metadata_only")
    for change in snap["changes"]:
        _call(managed.registry, "review_file_diff", {
            "snapshot_ref": snap["snapshot_ref"],
            "change_id": change["change_id"],
            "layer": "staged" if change["staged"] else "unstaged",
        })
    # Paginate too -- load_more()-shaped calls are still a "review read".
    _call(managed.registry, "review_changes", {"snapshot_ref": snap["snapshot_ref"], "cursor": 0})

    # Non-owner, through its own granted review target -- the other real
    # entry point onto the exact same production code.
    non_owner = _non_owner_registry(review_target_grant=cp_module_target(managed.root))
    non_owner_snap = _call(non_owner, "review_changes", {})
    assert non_owner_snap["status"] in ("current", "current_metadata_only")

    assert _table_row_count("coding_checkpoints") == before_checkpoints, (
        "review_changes/review_file_diff created or modified a coding_checkpoints row"
    )
    assert _table_row_count("coding_validation_evidence") == before_evidence, (
        "review_changes/review_file_diff created or modified a coding_validation_evidence row"
    )
    assert _binding_dir_snapshot(project_context_module.PROJECT_BINDINGS_DIR) == before_bindings, (
        "review_changes/review_file_diff wrote to a Project binding file"
    )
    assert dict(worktree_manager._registry) == before_registry, (
        "review_changes/review_file_diff mutated the worktree manager's runtime registry"
    )
    assert managed.state.snapshot() == before_project, (
        "review_changes/review_file_diff mutated the owner's active ProjectContextState"
    )


def cp_module_target(root: str):
    import core.coding_checkpoint as checkpoint_store
    return checkpoint_store.resolve_target_identity(root)


def test_review_changes_durable_write_mutation_turns_red(managed, monkeypatch):
    """Literal repeat of CODING-08R's R-H mutation: insert a real durable
    checkpoint save into review_changes's success path and confirm the new
    invariant test above -- not the old static substring check, which this
    mutation does not touch at all -- turns red for the intended reason."""
    import core.coding_checkpoint as checkpoint_store

    real_bind = review_module._bind_snapshot_target

    def _bind_with_side_effect(snapshot_ref, target_key):
        real_bind(snapshot_ref, target_key)
        try:
            checkpoint_store.save_checkpoint(
                "coding-08r1-mutation-probe", managed.root,
                {"workflow": {"task_id": "r-h-mutation", "phase": "in_progress"}},
                expected_revision=0,
            )
        except Exception:
            pass

    monkeypatch.setattr(review_module, "_bind_snapshot_target", _bind_with_side_effect)
    with pytest.raises(AssertionError):
        test_review_read_paths_cause_no_durable_state_mutation(managed)


# ===========================================================================
# CODING-08R.1: close the R-K test-oracle gap. Defines the CLOSED set of
# top-level JSON keys review_changes/review_file_diff may ever emit --
# across every degraded/error candidate the bounded-output ladder can
# select, not merely the full/happy-path shape. An unreviewed schema
# addition (in particular any authority/governance-shaped key) must fail
# this test, forcing a deliberate update rather than silently shipping.
# ===========================================================================

_REVIEW_CHANGES_ALLOWED_KEYS = {
    "status", "snapshot_ref", "repository_content_untrusted", "target",
    "captured_at", "reasons", "head", "branch", "detached", "unborn",
    "operation_state", "content_complete", "omissions", "total_changes",
    "changes", "next_cursor", "complete_page", "message",
}
_REVIEW_CHANGE_RECORD_ALLOWED_KEYS = {
    "change_id", "display_path", "display_original_path", "record_type",
    "relation", "relation_score", "xy_status", "staged", "unstaged",
    "untracked", "submodule", "head_mode", "index_mode", "worktree_mode",
    "head_object_id", "index_object_id", "unmerged_stages", "staged_diff",
    "unstaged_diff", "content_omission_reason",
}
_REVIEW_FILE_DIFF_ALLOWED_KEYS = {
    "status", "snapshot_ref", "change_id", "layer",
    "repository_content_untrusted", "reasons", "display_path", "binary",
    "total_bytes", "hunks", "complete", "omitted_hunks", "omission_reason",
    "next_cursor", "message",
}
# Explicitly forbidden regardless of whether they'd otherwise pass a
# generic "unexpected key" check -- these are the specific authority/
# governance shapes CODING-08's whole threat model says review must never
# manufacture, named so a reviewer sees exactly what tripped the test.
_FORBIDDEN_AUTHORITY_KEYS = {
    "reviewed", "approved", "ready_to_commit", "commit_authorized",
    "merge_authorized", "push_authorized", "release_authorized",
    "checkpoint_authorized",
}


def _assert_closed_schema(payload: dict, allowed: set, where: str):
    extra = set(payload.keys()) - allowed
    assert not extra, f"{where} produced undeclared key(s): {sorted(extra)}"
    authority_leak = set(payload.keys()) & _FORBIDDEN_AUTHORITY_KEYS
    assert not authority_leak, f"{where} produced authority-shaped key(s): {sorted(authority_leak)}"


def test_review_changes_json_schema_is_closed(managed):
    snap = _call(managed.registry, "review_changes", {})
    _assert_closed_schema(snap, _REVIEW_CHANGES_ALLOWED_KEYS, "review_changes")
    for change in snap["changes"]:
        _assert_closed_schema(change, _REVIEW_CHANGE_RECORD_ALLOWED_KEYS, "review_changes.changes[]")

    # Degraded/error candidates: unauthorized, unknown snapshot, malformed --
    # every branch the bounded-output ladder can actually select.
    _assert_closed_schema(
        _call(_non_owner_registry(), "review_changes", {"snapshot_ref": "revsnap-does-not-exist"}),
        _REVIEW_CHANGES_ALLOWED_KEYS, "review_changes (unauthorized_target)",
    )
    _assert_closed_schema(
        _call(managed.registry, "review_changes", {"snapshot_ref": "revsnap-does-not-exist"}),
        _REVIEW_CHANGES_ALLOWED_KEYS, "review_changes (unknown_snapshot)",
    )
    _assert_closed_schema(
        _call(managed.registry, "review_changes", {"cursor": -1}),
        _REVIEW_CHANGES_ALLOWED_KEYS, "review_changes (malformed_request)",
    )


def test_review_file_diff_json_schema_is_closed(managed):
    (managed.repo / "tracked.txt").write_text("line1\nCHANGED\nline3\n", encoding="utf-8")
    snap = _call(managed.registry, "review_changes", {})
    change = snap["changes"][0]
    result = _call(managed.registry, "review_file_diff", {
        "snapshot_ref": snap["snapshot_ref"], "change_id": change["change_id"],
        "layer": "staged" if change["staged"] else "unstaged",
    })
    _assert_closed_schema(result, _REVIEW_FILE_DIFF_ALLOWED_KEYS, "review_file_diff")
    for hunk in result.get("hunks", []):
        _assert_closed_schema(
            hunk, {"header", "lines", "byte_size", "omitted", "omission_reason"},
            "review_file_diff.hunks[]",
        )
        for line in hunk.get("lines") or []:
            _assert_closed_schema(line, {"kind", "text"}, "review_file_diff.hunks[].lines[]")

    _assert_closed_schema(
        _call(managed.registry, "review_file_diff", {
            "snapshot_ref": "revsnap-does-not-exist", "change_id": "x" * 16, "layer": "staged",
        }),
        _REVIEW_FILE_DIFF_ALLOWED_KEYS, "review_file_diff (unknown_snapshot)",
    )
    _assert_closed_schema(
        _call(managed.registry, "review_file_diff", {
            "snapshot_ref": snap["snapshot_ref"], "change_id": "x" * 16, "layer": "not-a-real-layer",
        }),
        _REVIEW_FILE_DIFF_ALLOWED_KEYS, "review_file_diff (invalid_layer)",
    )


def test_review_changes_authority_key_mutation_turns_red(managed, monkeypatch):
    """Literal repeat of CODING-08R's R-K mutation."""
    real_render = review_module._render_review_changes

    def _render_with_authority_keys(snapshot_ref, snapshot, applicability, cursor, limit):
        import json as _json
        encoded = real_render(snapshot_ref, snapshot, applicability, cursor, limit)
        payload = _json.loads(encoded)
        payload["reviewed"] = True
        payload["ready_to_commit"] = applicability.state == git_review_snapshot.CURRENT
        return _json.dumps(payload)

    monkeypatch.setattr(review_module, "_render_review_changes", _render_with_authority_keys)
    with pytest.raises(AssertionError):
        test_review_changes_json_schema_is_closed(managed)


# ===========================================================================
# TOOL TIER / PROFILE WIRING
# ===========================================================================

def test_review_tools_are_read_only_tier():
    from core.tool_profiles import TOOL_TIERS
    assert TOOL_TIERS["review_changes"] == "read_only"
    assert TOOL_TIERS["review_file_diff"] == "read_only"


def test_review_tools_not_owner_only():
    from core.tool_profiles import OWNER_ONLY_TOOLS
    assert "review_changes" not in OWNER_ONLY_TOOLS
    assert "review_file_diff" not in OWNER_ONLY_TOOLS


def test_non_owner_pin_unverified_does_not_block_review(managed):
    """read_only tier is not in SENSITIVE_TIERS -- PIN gate must not block
    it even unverified. Uses a real LuminaAgent(owner=False) to exercise the
    actual gate wiring from core/agent.py, not a hand-built registry."""
    grant = review_module.checkpoint_store.resolve_target_identity(str(managed.root))

    class _GrantedChild(LuminaAgent):
        pass

    child = _GrantedChild(
        owner=False, channel_id="a3-pin-test", backend="llamacpp",
        _review_target_grant=grant,
    )
    from core.tool_profiles import apply_tool_profile
    apply_tool_profile(child.registry, tools_enabled=["review_changes"], owner=False)
    result = _call(child.registry, "review_changes", {})
    assert result["status"] == "current"
