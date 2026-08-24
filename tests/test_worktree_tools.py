"""CODING-07A3 model-facing worktree authority and adapter regressions.

All Git mutations are confined to pytest-owned disposable repositories.
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import config
import core.coding_checkpoint_observation as checkpoint_observation
import core.headless as headless
import core.persistence as persistence
import core.pin_gate as pin_gate
import core.process_manager as process_manager
import core.project_context as project_context
import core.secrets as secrets_module
import core.tool_profiles as tool_profiles
import core.worktree_manager as worktree_manager
import tools.projects as projects_module
import tools.worktrees as worktree_tools
from core import emergency_stop
from core.agent import LuminaAgent, TurnCancellation
from core.project_context import ProjectContextState, save_project_binding
from core.tool_profiles import OWNER_ONLY_TOOLS, TOOL_TIERS, apply_tool_profile, list_profiles
from tools.registry import ToolRegistry
from tools.worktrees import register_worktree_tools


WORKTREE_TOOLS = ("create_worktree", "list_worktrees", "remove_worktree")
EXPECTED_TIERS = {
    "create_worktree": "execute",
    "list_worktrees": "read_only",
    "remove_worktree": "execute",
}


def _git(path, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=path, check=check, capture_output=True, text=True,
    )


def _init_repo(path):
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "tracked.txt").write_text("initial\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "initial")


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    projectlist = projects_dir / "projectlist.md"
    projectlist.write_text("# Projects\n\n", encoding="utf-8")
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "lumina.db"))
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(
        secrets_module, "SECRETS_PATH", str(tmp_path / "credentials.json")
    )
    monkeypatch.setattr(
        project_context, "PROJECT_BINDINGS_DIR", str(tmp_path / "bindings")
    )
    monkeypatch.setattr(projects_module, "PROJECTS_DIR", str(projects_dir))
    monkeypatch.setattr(projects_module, "PROJECTLIST", str(projectlist))
    monkeypatch.setattr(
        projects_module, "PROJECT_CHATS_DIR", str(tmp_path / "project-chats")
    )
    monkeypatch.setattr(worktree_manager, "_PROTECTED_ENGINEERING_ROOTS", frozenset())
    worktree_manager._reset_for_tests()
    process_manager._reset_for_tests()
    emergency_stop._reset_for_tests()
    saved_cache = (
        dict(headless._agents), dict(headless._last_used), dict(headless._is_owner)
    )
    headless._agents.clear()
    headless._last_used.clear()
    headless._is_owner.clear()
    yield

    # Best-effort disposable cleanup without using the product removal path,
    # so a failed assertion cannot leave a linked-worktree ledger entry behind.
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
    headless._agents.clear()
    headless._last_used.clear()
    headless._is_owner.clear()
    headless._agents.update(saved_cache[0])
    headless._last_used.update(saved_cache[1])
    headless._is_owner.update(saved_cache[2])


def _make_project(tmp_path, name="proj"):
    root = tmp_path / f"{name}-repo"
    root.mkdir()
    _init_repo(root)
    tracked = Path(projects_module.PROJECTS_DIR) / name
    tracked.mkdir()
    (tracked / "project.md").write_text(f"# {name}\n", encoding="utf-8")
    return root, save_project_binding(name, str(root))


def _agent(owner=True, channel_id="worktree-integration"):
    return LuminaAgent(owner=owner, channel_id=channel_id, backend="llamacpp")


def _enable_coding(agent):
    apply_tool_profile(agent.registry, profile_name="Coding", owner=agent.owner)


def _bare_registry(state=None, cancel_state=None):
    registry = ToolRegistry()
    register_worktree_tools(registry, project_state=state, cancel_state=cancel_state)
    return registry


def _create(registry, **args):
    return json.loads(registry.call("create_worktree", args))


def _list(registry):
    return json.loads(registry.call("list_worktrees", {}))


def _remove(registry, worktree_id, force=False, **extra):
    return json.loads(registry.call(
        "remove_worktree",
        {"worktree_id": worktree_id, "force": force, **extra},
    ))


def test_real_agent_registers_exact_surface_once_with_exact_schemas():
    agent = _agent()
    names = agent.registry.all_tool_names()
    assert [name for name in names if name in WORKTREE_TOOLS] == list(WORKTREE_TOOLS)

    isolated = _bare_registry()
    assert agent.registry.get_schemas(list(WORKTREE_TOOLS)) == isolated.get_schemas(
        list(WORKTREE_TOOLS)
    )
    remove_schema = isolated.get_schemas(["remove_worktree"])[0]["function"]["parameters"]
    assert set(remove_schema["properties"]) == {"worktree_id", "force"}
    assert remove_schema["required"] == ["worktree_id"]
    assert remove_schema["additionalProperties"] is False
    assert set(names).isdisjoint({
        "prune_worktrees", "delete_worktree_branch", "run_isolated_task",
    })


def test_tiers_owner_membership_and_coding_profile_are_exact():
    assert {name: TOOL_TIERS[name] for name in WORKTREE_TOOLS} == EXPECTED_TIERS
    assert set(WORKTREE_TOOLS).issubset(OWNER_ONLY_TOOLS)
    coding = next(profile for profile in list_profiles() if profile["name"] == "Coding")
    assert set(WORKTREE_TOOLS).issubset(coding["enabled"])


def test_owner_coding_profile_has_tools_and_explicit_empty_selection_removes_them():
    agent = _agent(owner=True)
    _enable_coding(agent)
    assert set(WORKTREE_TOOLS).issubset(agent.registry.list_enabled())
    apply_tool_profile(agent.registry, tools_enabled=[], owner=True)
    assert set(WORKTREE_TOOLS).isdisjoint(agent.registry.list_enabled())


@pytest.mark.parametrize("grant_kind", ["coding", "explicit"])
def test_nonowner_cannot_regain_tools_via_profile_or_explicit_grant(grant_kind):
    agent = _agent(owner=False, channel_id=f"nonowner-{grant_kind}")
    if grant_kind == "coding":
        apply_tool_profile(agent.registry, profile_name="Coding", owner=False)
    else:
        apply_tool_profile(agent.registry, tools_enabled=list(WORKTREE_TOOLS), owner=False)
    assert set(WORKTREE_TOOLS).isdisjoint(agent.registry.list_enabled())
    for name in WORKTREE_TOOLS:
        assert "disabled" in agent.registry.call(name, {}).lower()


def test_nonowner_verified_pin_and_active_project_still_cannot_regain_tools(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(pin_gate, "is_verified", lambda channel_id=None: True)
    _root, context = _make_project(tmp_path)
    agent = _agent(owner=False, channel_id="verified-project-guest")
    agent.project_context.set(context)
    apply_tool_profile(agent.registry, tools_enabled=list(WORKTREE_TOOLS), owner=False)
    assert set(WORKTREE_TOOLS).isdisjoint(agent.registry.list_enabled())
    assert agent.registry._gate_fn("create_worktree") == (True, "")


def test_cached_headless_owner_mismatch_cannot_restore_tools():
    channel = "cached-worktree-guest"
    guest = headless.get_headless_agent(channel, owner=False)
    same = headless.get_headless_agent(
        channel, owner=True, force_tools_profile="Coding",
    )
    assert same is guest
    assert guest.owner is False
    assert set(WORKTREE_TOOLS).isdisjoint(guest.registry.list_enabled())


def test_subagent_explicit_grant_cannot_restore_tools(monkeypatch):
    import tools.subagent as subagent_module

    created = []

    class _NoChatAgent(LuminaAgent):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            created.append(self)

        def chat(self, _task):
            return "done"

    monkeypatch.setattr(subagent_module, "LuminaAgent", _NoChatAgent)
    result = subagent_module.spawn_subagent(
        "task", tools_enabled=list(WORKTREE_TOOLS), _parent_depth=0,
    )
    assert result["success"] is True
    assert created[0].owner is False
    assert set(WORKTREE_TOOLS).isdisjoint(created[0].registry.list_enabled())


def test_owner_only_backstop_is_load_bearing_mutation_proof(monkeypatch):
    mutant = set(tool_profiles.OWNER_ONLY_TOOLS) - set(WORKTREE_TOOLS)
    monkeypatch.setattr(tool_profiles, "OWNER_ONLY_TOOLS", mutant)
    agent = _agent(owner=False, channel_id="mutated-owner-backstop")
    apply_tool_profile(agent.registry, tools_enabled=list(WORKTREE_TOOLS), owner=False)
    assert set(WORKTREE_TOOLS).issubset(agent.registry.list_enabled())


def test_omitted_source_valid_active_project_creates_exact_commit_and_removes(tmp_path):
    root, context = _make_project(tmp_path)
    state = ProjectContextState(context)
    registry = _bare_registry(state)
    expected = _git(root, "rev-parse", "HEAD").stdout.strip()
    source_head_before = expected
    source_status_before = _git(root, "status", "--porcelain=v1").stdout
    binding_path = Path(project_context.PROJECT_BINDINGS_DIR) / context.name / "binding.json"
    binding_before = binding_path.read_bytes()
    assert not Path(config.DB_PATH).exists()

    created = _create(registry)
    assert created["status"] == "created"
    assert created["base_commit"] == expected
    assert created["worktree_id"].startswith("wt-")
    assert Path(created["worktree_root"]).is_dir()

    listed = _list(registry)
    assert listed["total"] == 1
    assert listed["worktrees"][0]["state"] == "live"
    assert listed["worktrees"][0]["worktree_id"] == created["worktree_id"]

    removed = _remove(registry, created["worktree_id"])
    assert removed["status"] == "removed"
    assert not Path(created["worktree_root"]).exists()
    assert _git(root, "rev-parse", "HEAD").stdout.strip() == source_head_before
    assert _git(root, "status", "--porcelain=v1").stdout == source_status_before
    assert _git(root, "branch", "--list", created["branch"]).stdout.strip()
    assert binding_path.read_bytes() == binding_before
    assert not Path(config.DB_PATH).exists()


def test_omitted_source_stale_binding_rejects_before_manager_mutation(
    tmp_path, monkeypatch,
):
    _root, context = _make_project(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    _init_repo(other)
    save_project_binding(context.name, str(other))
    state = ProjectContextState(context)
    registry = _bare_registry(state)
    calls = []

    def forbidden_create(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("manager mutation boundary must not be reached")

    monkeypatch.setattr(worktree_manager, "create_worktree", forbidden_create)
    result = _create(registry)
    assert result["error"] == "stale_project_binding"
    assert calls == []


def test_stale_project_gate_is_load_bearing_mutation_proof(tmp_path, monkeypatch):
    _root, context = _make_project(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    _init_repo(other)
    save_project_binding(context.name, str(other))
    state = ProjectContextState(context)
    registry = _bare_registry(state)
    reached = []

    monkeypatch.setattr(checkpoint_observation, "verify_project_binding", lambda project: project)

    def mutant_reached(source, base, cancel_event=None):
        reached.append((source, base, cancel_event))
        raise worktree_manager.InvalidWorktreeRequest("mutation proof")

    monkeypatch.setattr(worktree_manager, "create_worktree", mutant_reached)
    result = _create(registry)
    assert result["error"] == "invalid_worktree_request"
    assert reached and reached[0][0] == context.root


def test_explicit_source_remains_authoritative_despite_stale_active_project(
    tmp_path, monkeypatch,
):
    _stale_root, context = _make_project(tmp_path, "stale")
    rebound = tmp_path / "rebound"
    rebound.mkdir()
    _init_repo(rebound)
    save_project_binding(context.name, str(rebound))
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    _init_repo(explicit)
    state = ProjectContextState(context)
    registry = _bare_registry(state)

    def must_not_verify(_project):
        raise AssertionError("explicit source must not consult active Project binding")

    monkeypatch.setattr(checkpoint_observation, "verify_project_binding", must_not_verify)
    created = _create(registry, source_repository=str(explicit))
    assert created["status"] == "created"
    assert worktree_manager._registry[created["worktree_id"]].source_root == str(explicit)
    assert _remove(registry, created["worktree_id"])["status"] == "removed"


def test_protected_source_and_target_area_are_classified_before_git_mutation(
    tmp_path, monkeypatch,
):
    source = tmp_path / "source"
    source.mkdir()
    _init_repo(source)
    registry = _bare_registry()

    monkeypatch.setattr(
        worktree_manager, "_PROTECTED_ENGINEERING_ROOTS",
        frozenset({os.path.realpath(source)}),
    )
    refused_source = _create(registry, source_repository=str(source))
    assert refused_source["error"] == "protected_root_refused"
    assert _git(source, "branch", "--list", "lumina/*").stdout == ""

    protected_target = tmp_path / "protected-target"
    monkeypatch.setattr(config, "DATA_DIR", str(protected_target / "data"))
    monkeypatch.setattr(
        worktree_manager, "_PROTECTED_ENGINEERING_ROOTS",
        frozenset({os.path.realpath(protected_target)}),
    )
    refused_target = _create(registry, source_repository=str(source))
    assert refused_target["error"] == "protected_root_refused"
    assert _git(source, "branch", "--list", "lumina/*").stdout == ""


def test_list_is_session_scoped_and_never_adopts_external_or_other_session(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _init_repo(source)
    external = tmp_path / "external"
    _git(source, "worktree", "add", "-q", "-b", "external", str(external), "HEAD")

    first = _bare_registry()
    second = _bare_registry()
    created = _create(first, source_repository=str(source))
    assert created["status"] == "created"
    assert len(worktree_manager.list_managed_worktrees()) == 1
    assert _list(first)["total"] == 1
    assert _list(second) == {
        "output_truncated": False, "total": 0, "worktrees": [],
    }
    assert all(
        item["worktree_id"] != str(external)
        for item in _list(first)["worktrees"]
    )
    assert _remove(first, created["worktree_id"])["status"] == "removed"
    _git(source, "worktree", "remove", "--force", str(external))


def test_opaque_id_boundary_rejects_paths_branches_and_raw_git_arguments_before_manager(
    monkeypatch,
):
    registry = _bare_registry()
    calls = []
    monkeypatch.setattr(
        worktree_manager, "remove_worktree",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    assert _remove(registry, "/tmp/arbitrary")["error"] == "invalid_worktree_id"
    assert _remove(registry, "main")["error"] == "invalid_worktree_id"
    result = _remove(
        registry, "wt-" + "a" * 24, path="/tmp/arbitrary", branch="main",
    )
    assert result["error"] == "invalid_arguments"
    assert calls == []


def test_stale_id_and_externally_removed_replaced_target_fail_safely(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _init_repo(source)
    registry = _bare_registry()
    assert _remove(registry, "wt-" + "0" * 24)["error"] == "worktree_not_found"

    created = _create(registry, source_repository=str(source))
    target = Path(created["worktree_root"])
    _git(source, "worktree", "remove", "--force", str(target))
    target.mkdir(parents=True)
    (target / "replacement.txt").write_text("do not delete\n", encoding="utf-8")
    refused = _remove(registry, created["worktree_id"], force=True)
    assert refused["status"] == "identity_refused"
    assert target.is_dir()
    assert (target / "replacement.txt").read_text(encoding="utf-8") == "do not delete\n"


def test_dirty_force_semantics_and_branch_preservation_through_adapter(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _init_repo(source)
    registry = _bare_registry()
    created = _create(registry, source_repository=str(source))
    target = Path(created["worktree_root"])
    (target / "dirty.txt").write_text("dirty\n", encoding="utf-8")

    refused = _remove(registry, created["worktree_id"], force=False)
    assert refused["status"] == "dirty_refused"
    assert refused["dirty"] is True
    removed = _remove(registry, created["worktree_id"], force=True)
    assert removed["status"] == "removed"
    assert _git(source, "branch", "--list", created["branch"]).stdout.strip()


def test_force_never_overrides_locked_or_live_process_refusal(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _init_repo(source)
    registry = _bare_registry()

    locked = _create(registry, source_repository=str(source))
    _git(source, "worktree", "lock", "--reason", "operator lock", locked["worktree_root"])
    locked_result = _remove(registry, locked["worktree_id"], force=True)
    assert locked_result["status"] == "locked_refused"
    _git(source, "worktree", "unlock", locked["worktree_root"])
    assert _remove(registry, locked["worktree_id"])["status"] == "removed"

    live = _create(registry, source_repository=str(source))
    process_id = process_manager.launch_argv(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=live["worktree_root"], visibility="internal",
    )
    try:
        refused = _remove(registry, live["worktree_id"], force=True)
        assert refused["status"] == "live_process_refused"
        assert refused["blocking_process_ids"] == [process_id]
        assert Path(live["worktree_root"]).is_dir()
    finally:
        process_manager.shutdown_all(wait_seconds=5)
    assert _remove(registry, live["worktree_id"])["status"] == "removed"


def test_force_live_process_check_is_load_bearing_mutation_proof(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    _init_repo(source)
    registry = _bare_registry()
    created = _create(registry, source_repository=str(source))
    process_id = process_manager.launch_argv(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=created["worktree_root"], visibility="internal",
    )
    try:
        monkeypatch.setattr(worktree_manager, "_blocking_jobs", lambda handle: ())
        mutant = _remove(registry, created["worktree_id"], force=True)
        assert mutant["status"] == "removed"
        assert not Path(created["worktree_root"]).exists()
    finally:
        process_manager.shutdown_all(wait_seconds=5)
    assert process_id not in process_manager.list_job_ids()


def test_cancel_state_is_read_live_and_passed_to_create_and_remove(monkeypatch):
    cancel = TurnCancellation()
    event = threading.Event()
    cancel._set(event)
    registry = _bare_registry(cancel_state=cancel)
    captured = []

    def fake_create(source, base, cancel_event=None):
        captured.append(("create", cancel_event))
        raise worktree_manager.InvalidWorktreeRequest("stop after capture")

    monkeypatch.setattr(worktree_manager, "create_worktree", fake_create)
    result = _create(registry, source_repository="/tmp/source")
    assert result["error"] == "invalid_worktree_request"
    assert captured == [("create", event)]


def test_model_cancellation_reaches_underlying_managed_remove(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    _init_repo(source)
    cancel = TurnCancellation()
    event = threading.Event()
    cancel._set(event)
    registry = _bare_registry(cancel_state=cancel)
    created = _create(registry, source_repository=str(source))
    assert created["status"] == "created"
    real_launch = process_manager.launch_argv

    def sleeping_remove(argv, **kwargs):
        return real_launch(
            [sys.executable, "-c", "import time; time.sleep(30)"], **kwargs
        )

    monkeypatch.setattr(worktree_manager.process_manager, "launch_argv", sleeping_remove)
    event.set()
    result = _remove(registry, created["worktree_id"])
    assert result["status"] == "cancelled"
    assert result["target_exists"] is True
    assert Path(created["worktree_root"]).is_dir()
    assert process_manager._jobs_rooted_under(created["worktree_root"]) == ()


@pytest.mark.skipif(os.name == "nt", reason="POSIX hook/process-group proof")
def test_model_cancellation_reaches_underlying_git_tree_and_no_descendant_escapes(
    tmp_path,
):
    source = tmp_path / "source"
    source.mkdir()
    _init_repo(source)
    started = tmp_path / "hook-started"
    heartbeat = tmp_path / "hook-heartbeat"
    hook = source / ".git" / "hooks" / "post-checkout"
    hook.write_text(
        "#!/bin/sh\n"
        f"printf started > {started}\n"
        "(\n"
        "  i=0\n"
        "  while :; do\n"
        "    i=$((i + 1))\n"
        f"    printf '%s' \"$i\" > {heartbeat}\n"
        "    sleep 0.02\n"
        "  done\n"
        ") &\n"
        "wait\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)

    cancel = TurnCancellation()
    event = threading.Event()
    cancel._set(event)
    registry = _bare_registry(cancel_state=cancel)
    outcome = {}

    thread = threading.Thread(
        target=lambda: outcome.setdefault(
            "result", _create(registry, source_repository=str(source))
        )
    )
    thread.start()
    deadline = time.monotonic() + 10
    while not started.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert started.exists()
    event.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert outcome["result"]["status"] == "cancelled"
    deadline = time.monotonic() + 5
    while not heartbeat.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    if heartbeat.exists():
        before = heartbeat.read_text(encoding="utf-8")
        time.sleep(0.2)
        assert heartbeat.read_text(encoding="utf-8") == before
    assert process_manager._jobs_rooted_under(str(source)) == ()


def test_output_is_bounded_json_and_unknown_exception_detail_is_hidden(
    monkeypatch,
):
    registry = _bare_registry()
    monkeypatch.setattr(config, "TOOL_RESULT_MAX_CHARS", 96)
    monkeypatch.setattr(
        worktree_manager, "create_worktree",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("SECRET" + "x" * 10000)
        ),
    )
    raw = registry.call(
        "create_worktree", {"source_repository": "/tmp/source"}
    )
    assert len(raw) <= 96
    assert "SECRET" not in raw
    assert json.loads(raw)["error"] == "worktree_operation_failed"

    monkeypatch.setattr(config, "TOOL_RESULT_MAX_CHARS", 1)
    assert registry.call("list_worktrees", {}) == "0"
    monkeypatch.setattr(config, "TOOL_RESULT_MAX_CHARS", 0)
    assert registry.call("list_worktrees", {}) == ""
