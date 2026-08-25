"""CODING-07A4 isolated subagent dispatch acceptance and attack tests.

Every Git mutation is confined to pytest-owned repositories and worktrees.
The production release/dev topology is never a source or target here.
"""

import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

import config
import core.persistence as persistence
import core.project_context as project_context_module
import core.secrets as secrets_module
from core import emergency_stop, process_manager, worktree_manager
from core.agent import LuminaAgent
from core.project_context import ProjectContext, ProjectContextState
from core.tool_profiles import OWNER_ONLY_TOOLS, apply_tool_profile
from tools import subagent as subagent_module
from tools import tasks as tasks_module
import tools.projects as projects_module
from tools.filesystem import register_filesystem_tools
from tools.registry import ToolRegistry
from tools.subagent import register_subagent_tools
from tools.tasks import register_task_tools
from tools.worktrees import register_worktree_tools


def _git(cwd, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=check, capture_output=True, text=True,
    )


def _repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.name", "Lumina A4 Test")
    _git(path, "config", "user.email", "lumina-a4@example.invalid")
    (path / "tracked.txt").write_text("primary\n", encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-qm", "initial")
    return path


class _Harness:
    def __init__(self, repo, state, registry, resolver, created):
        self.repo = repo
        self.state = state
        self.registry = registry
        self.resolver = resolver
        self.worktree_id = created["worktree_id"]
        self.root = Path(created["worktree_root"])


@pytest.fixture
def managed(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "lumina.db"))
    monkeypatch.setattr(config, "SUBAGENTS_ENABLED", True)
    monkeypatch.setattr(config, "BACKGROUND_TASKS_ENABLED", True)
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(
        secrets_module, "SECRETS_PATH", str(tmp_path / "credentials.json"),
    )
    monkeypatch.setattr(
        project_context_module, "PROJECT_BINDINGS_DIR", str(tmp_path / "bindings"),
    )
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    projectlist = projects_dir / "projectlist.md"
    projectlist.write_text("# Projects\n\n", encoding="utf-8")
    monkeypatch.setattr(projects_module, "PROJECTS_DIR", str(projects_dir))
    monkeypatch.setattr(projects_module, "PROJECTLIST", str(projectlist))
    monkeypatch.setattr(
        projects_module, "PROJECT_CHATS_DIR", str(tmp_path / "project-chats"),
    )
    monkeypatch.setattr(worktree_manager, "_PROTECTED_ENGINEERING_ROOTS", frozenset())
    process_manager._reset_for_tests()
    emergency_stop._reset_for_tests()
    worktree_manager._reset_for_tests()

    repo = _repo(tmp_path / "repo")
    owner_context = ProjectContext(name="owner-project", root=str(repo))
    state = ProjectContextState(owner_context)
    registry = ToolRegistry()
    resolver = register_worktree_tools(registry, project_state=state)
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
    if emergency_stop.is_latched() and emergency_stop.can_rearm():
        emergency_stop.rearm_local()
    emergency_stop._reset_for_tests()


def _spawn_registry(managed):
    registry = ToolRegistry()
    register_subagent_tools(
        registry, parent_depth=0, project_state=managed.state,
        worktree_resolver=managed.resolver,
    )
    return registry


def _spawn(managed, payload):
    return ast.literal_eval(_spawn_registry(managed).call("spawn_subagent", payload))


def test_public_schemas_expose_only_immediate_worktree_selection(managed):
    spawn_registry = _spawn_registry(managed)
    spawn_schema = spawn_registry.get_schemas(["spawn_subagent"])[0]["function"]["parameters"]
    assert "worktree_id" in spawn_schema["properties"]
    assert "_worktree_resolver" not in spawn_schema["properties"]
    assert "_project_context" not in spawn_schema["properties"]

    class _Agent:
        project_context = managed.state
        _background_task_ids = set()

    task_registry = ToolRegistry()
    register_task_tools(task_registry, _Agent(), worktree_resolver=managed.resolver)
    run_schema = task_registry.get_schemas(["run_background_subagent"])[0]["function"]["parameters"]
    scheduled_schema = task_registry.get_schemas(["schedule_background_subagent"])[0]["function"]["parameters"]
    assert "worktree_id" in run_schema["properties"]
    assert "worktree_id" not in scheduled_schema["properties"]


def test_real_agent_wires_its_worktree_session_into_dispatch(managed, monkeypatch):
    children = []

    class _Child:
        def __init__(self, owner, channel_id, backend, depth, project_context,
                     _review_target_grant=None):
            self.owner = owner
            self.project_context = ProjectContextState(project_context)
            self.registry = ToolRegistry()
            children.append(self)

        def chat(self, task):
            return self.project_context.get().root

    owner = LuminaAgent(owner=True, channel_id="a4-real-owner", backend="llamacpp")
    owner.project_context.set(managed.state.snapshot())
    apply_tool_profile(
        owner.registry,
        tools_enabled=["create_worktree", "spawn_subagent"], owner=True,
    )
    created = json.loads(owner.registry.call("create_worktree", {
        "source_repository": str(managed.repo), "base": "HEAD",
    }))
    assert created["status"] == "created"

    monkeypatch.setattr(subagent_module, "LuminaAgent", _Child)
    result = ast.literal_eval(owner.registry.call("spawn_subagent", {
        "task": "real wiring", "worktree_id": created["worktree_id"],
    }))
    assert result["success"] is True
    assert children[0].owner is False
    assert children[0].project_context.get() == ProjectContext(
        name=f"worktree-{created['worktree_id']}", root=created["worktree_root"],
    )
    assert owner.project_context.snapshot() == managed.state.snapshot()


def test_valid_dispatch_roots_child_edits_and_preserves_owner_state(managed, monkeypatch):
    children = []

    class _EditingChild:
        def __init__(self, owner, channel_id, backend, depth, project_context,
                     _review_target_grant=None):
            self.owner = owner
            self.channel_id = channel_id
            self.depth = depth
            self.project_context = ProjectContextState(project_context)
            self.registry = ToolRegistry()
            register_filesystem_tools(self.registry, project_state=self.project_context)
            children.append(self)

        def chat(self, task):
            return self.registry.call(
                "write_file", {"path": "child-only.txt", "content": task},
            )

    monkeypatch.setattr(subagent_module, "LuminaAgent", _EditingChild)
    before = managed.state.snapshot()
    result = _spawn(managed, {
        "task": "inside worktree\n",
        "tools_enabled": ["write_file"],
        "worktree_id": managed.worktree_id,
    })

    assert result["success"] is True
    assert children[0].owner is False
    assert children[0].project_context.get() == ProjectContext(
        name=f"worktree-{managed.worktree_id}", root=str(managed.root),
    )
    assert managed.state.snapshot() == before
    assert (managed.root / "child-only.txt").read_text() == "inside worktree\n"
    assert not (managed.repo / "child-only.txt").exists()
    assert managed.root.is_dir()


def test_dispatch_cannot_restore_owner_only_capabilities(managed, monkeypatch):
    children = []
    requested = sorted(OWNER_ONLY_TOOLS)

    class _AuthorityChild:
        def __init__(self, owner, channel_id, backend, depth, project_context,
                     _review_target_grant=None):
            self.owner = owner
            self.project_context = ProjectContextState(project_context)
            self.registry = ToolRegistry()
            for name in requested:
                self.registry.register(
                    name=name, fn=lambda: "unexpected", description=name,
                    parameters={"type": "object", "properties": {}},
                )
            children.append(self)

        def chat(self, task):
            return "checked"

    monkeypatch.setattr(subagent_module, "LuminaAgent", _AuthorityChild)
    result = _spawn(managed, {
        "task": "authority attack",
        "tools_enabled": requested,
        "worktree_id": managed.worktree_id,
    })

    assert result["success"] is True
    assert children[0].owner is False
    assert set(requested).isdisjoint(children[0].registry.list_enabled())
    for name in (
        "create_worktree", "list_worktrees", "remove_worktree",
        "set_project_root", "create_project", "run_tests",
        "start_process", "read_process", "send_process_input",
        "stop_process", "list_processes", "save_coding_checkpoint",
    ):
        assert children[0].registry.call(name, {}) == (
            f"[Tool '{name}' is currently disabled.]"
        )


@pytest.mark.parametrize("bad_id", [
    "wt-000000000000000000000000",
    "/tmp/unmanaged-worktree",
    "main",
])
def test_fabricated_or_path_selector_spawns_no_child(managed, monkeypatch, bad_id):
    monkeypatch.setattr(
        subagent_module, "LuminaAgent",
        lambda **kwargs: pytest.fail("child must not be constructed"),
    )
    result = _spawn(managed, {
        "task": "must fail", "worktree_id": bad_id,
    })
    assert result["success"] is False


def test_manager_known_handle_from_another_session_spawns_no_child(managed, monkeypatch):
    other_registry = ToolRegistry()
    other_resolver = register_worktree_tools(
        other_registry, project_state=managed.state,
    )
    spawn_registry = ToolRegistry()
    register_subagent_tools(
        spawn_registry, parent_depth=0, project_state=managed.state,
        worktree_resolver=other_resolver,
    )
    monkeypatch.setattr(
        subagent_module, "LuminaAgent",
        lambda **kwargs: pytest.fail("child must not be constructed"),
    )

    result = ast.literal_eval(spawn_registry.call("spawn_subagent", {
        "task": "cross-session attack", "worktree_id": managed.worktree_id,
    }))
    assert result["success"] is False
    assert result["error"] == "managed worktree was not found in this session"


def test_externally_removed_target_spawns_no_child(managed, monkeypatch):
    _git(managed.repo, "worktree", "remove", "--force", str(managed.root))
    monkeypatch.setattr(
        subagent_module, "LuminaAgent",
        lambda **kwargs: pytest.fail("child must not be constructed"),
    )
    result = _spawn(managed, {
        "task": "must fail", "worktree_id": managed.worktree_id,
    })
    assert result["success"] is False
    assert "absent" in result["error"] or "missing" in result["error"]


def test_same_path_replacement_spawns_no_child(managed, monkeypatch):
    _git(managed.repo, "worktree", "remove", "--force", str(managed.root))
    managed.root.mkdir(parents=True)
    _git(managed.root, "init", "-q")
    monkeypatch.setattr(
        subagent_module, "LuminaAgent",
        lambda **kwargs: pytest.fail("child must not be constructed"),
    )
    result = _spawn(managed, {
        "task": "must fail", "worktree_id": managed.worktree_id,
    })
    assert result["success"] is False


def test_stale_handle_spawns_no_child(managed, monkeypatch):
    (managed.root / "tracked.txt").write_text("new commit\n", encoding="utf-8")
    _git(managed.root, "add", "tracked.txt")
    _git(managed.root, "commit", "-qm", "advance")
    monkeypatch.setattr(
        subagent_module, "LuminaAgent",
        lambda **kwargs: pytest.fail("child must not be constructed"),
    )
    stale = _spawn(managed, {
        "task": "must fail", "worktree_id": managed.worktree_id,
    })
    assert stale["success"] is False
    assert "HEAD differs" in stale["error"]


def test_locked_handle_spawns_no_child(managed, monkeypatch):
    _git(managed.repo, "worktree", "lock", "--reason", "test lock", str(managed.root))
    monkeypatch.setattr(
        subagent_module, "LuminaAgent",
        lambda **kwargs: pytest.fail("child must not be constructed"),
    )
    result = _spawn(managed, {
        "task": "must fail", "worktree_id": managed.worktree_id,
    })
    assert result["success"] is False
    assert "test lock" in result["error"]


def test_project_and_worktree_are_mutually_exclusive_before_spawn(managed, monkeypatch):
    monkeypatch.setattr(
        subagent_module, "LuminaAgent",
        lambda **kwargs: pytest.fail("child must not be constructed"),
    )
    result = _spawn(managed, {
        "task": "ambiguous", "project": "owner-project",
        "worktree_id": managed.worktree_id,
    })
    assert result["success"] is False
    assert result["error"] == "project and worktree_id are mutually exclusive"


def test_background_admission_verifies_before_queue_and_captures_context(managed, monkeypatch):
    captured = {}

    def _submit(fn, *args, **kwargs):
        captured.update(kwargs)
        return "task-a4"

    monkeypatch.setattr(tasks_module, "submit_task", _submit)
    result = tasks_module.run_background_subagent(
        "background", worktree_id=managed.worktree_id,
        _project_context=managed.state.snapshot(),
        _worktree_resolver=managed.resolver,
    )
    assert result == {"task_id": "task-a4"}
    assert captured["_project_context"] == ProjectContext(
        name=f"worktree-{managed.worktree_id}", root=str(managed.root),
    )
    assert "worktree_id" not in captured


def test_background_stale_race_fails_before_queue(managed, monkeypatch):
    entered = threading.Event()
    proceed = threading.Event()
    original = worktree_manager._observe_reality

    def _blocked_observation(source, target):
        entered.set()
        assert proceed.wait(5)
        return original(source, target)

    monkeypatch.setattr(worktree_manager, "_observe_reality", _blocked_observation)
    result = {}

    def _dispatch():
        result.update(tasks_module.run_background_subagent(
            "race", worktree_id=managed.worktree_id,
            _worktree_resolver=managed.resolver,
        ))

    thread = threading.Thread(target=_dispatch)
    thread.start()
    assert entered.wait(5)
    (managed.root / "tracked.txt").write_text("raced\n", encoding="utf-8")
    _git(managed.root, "add", "tracked.txt")
    _git(managed.root, "commit", "-qm", "race advance")
    proceed.set()
    thread.join(5)
    assert not thread.is_alive()
    assert result["task_id"] is None
    assert "HEAD differs" in result["error"]


def test_scheduled_worktree_dispatch_is_explicitly_unsupported(managed, monkeypatch):
    monkeypatch.setattr(
        tasks_module, "schedule_task",
        lambda *args, **kwargs: pytest.fail("scheduled task must not be created"),
    )
    result = tasks_module.schedule_background_subagent(
        "later", time.time() + 60, worktree_id=managed.worktree_id,
    )
    assert result == {
        "task_id": None, "error": "scheduled worktree dispatch is unsupported",
    }


def test_child_dispatched_managed_jobs_block_removal_after_child_finishes(managed, monkeypatch):
    process_ids = []

    class _ProcessChild:
        def __init__(self, owner, channel_id, backend, depth, project_context,
                     _review_target_grant=None):
            self.owner = owner
            self.project_context = ProjectContextState(project_context)
            self.registry = ToolRegistry()

        def chat(self, task):
            root = self.project_context.get().root
            process_ids.append(process_manager.launch(
                f'{sys.executable} -c "import time; time.sleep(60)"',
                cwd=root, channel_id="a4-child",
            ))
            process_ids.append(process_manager.launch_argv(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                cwd=root, provenance={"kind": "a4-child-test"},
                visibility="internal",
            ))
            return "launched"

    monkeypatch.setattr(subagent_module, "LuminaAgent", _ProcessChild)
    dispatched = _spawn(managed, {
        "task": "launch", "worktree_id": managed.worktree_id,
    })
    assert dispatched["success"] is True
    refused = json.loads(managed.registry.call(
        "remove_worktree", {"worktree_id": managed.worktree_id, "force": True},
    ))
    assert refused["status"] == "live_process_refused"
    assert set(refused["blocking_process_ids"]) == set(process_ids)
    assert managed.root.is_dir()


def test_emergency_kills_child_rooted_work_but_preserves_worktree(managed, monkeypatch):
    process_ids = []

    class _ProcessChild:
        def __init__(self, owner, channel_id, backend, depth, project_context,
                     _review_target_grant=None):
            self.owner = owner
            self.project_context = ProjectContextState(project_context)
            self.registry = ToolRegistry()

        def chat(self, task):
            process_ids.append(process_manager.launch_argv(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                cwd=self.project_context.get().root,
                provenance={"kind": "a4-emergency-test"}, visibility="internal",
            ))
            return "launched"

    monkeypatch.setattr(subagent_module, "LuminaAgent", _ProcessChild)
    assert _spawn(managed, {
        "task": "launch", "worktree_id": managed.worktree_id,
    })["success"] is True

    emergency_stop.latch(source="test", reason="a4 emergency")
    process_manager.emergency_kill_all()
    deadline = time.time() + 10
    while time.time() < deadline:
        snapshot = process_manager._get_internal_job_snapshot(process_ids[0])
        if snapshot is not None and snapshot["status"] != "running":
            break
        time.sleep(0.02)
    assert snapshot["status"] != "running"
    assert managed.root.is_dir()
    assert managed.worktree_id in worktree_manager._registry
