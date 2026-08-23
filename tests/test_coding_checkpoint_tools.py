"""CODING-05A3 model-adapter, authority, and real-registry regressions."""

import concurrent.futures
import json
import subprocess

import pytest

import config
import core.coding_checkpoint as checkpoint_store
import core.coding_checkpoint_observation as checkpoint_observation
import core.pin_gate as pin_gate
import core.project_context as project_context
import core.secrets as secrets_module
import tools.projects as projects_module
from core import emergency_stop, persistence
from core.agent import LuminaAgent
from core.project_context import ProjectContext, ProjectContextState, save_project_binding
from core.tool_profiles import (
    OWNER_ONLY_TOOLS,
    TOOL_TIERS,
    apply_tool_profile,
    list_profiles,
)
from tools.coding_checkpoint import register_coding_checkpoint_tools
from tools.registry import ToolRegistry


CHECKPOINT_TOOLS = ("read_coding_checkpoint", "save_coding_checkpoint")
EXPECTED_TIERS = {
    "read_coding_checkpoint": "read_only",
    "save_coding_checkpoint": "write_local",
}
FORBIDDEN_REALITY_FIELDS = {
    "project",
    "project_name",
    "root",
    "target_key",
    "target_kind",
    "head",
    "branch",
    "status_digest",
    "state_ref",
    "binding_complete",
    "commit_authorized",
    "push_authorized",
    "authorization",
    "authority",
}


def _git(path, *args):
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    )


def _init_repo(path):
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("initial\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "initial")


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    projectlist = projects_dir / "projectlist.md"
    projectlist.write_text("# Projects\n\n")
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
    emergency_stop._reset_for_tests()
    yield
    emergency_stop._reset_for_tests()


def _make_project(tmp_path, name="proj"):
    root = tmp_path / f"{name}-repo"
    root.mkdir()
    _init_repo(root)
    tracked = projects_module.PROJECTS_DIR + f"/{name}"
    from pathlib import Path

    tracked_path = Path(tracked)
    tracked_path.mkdir()
    (tracked_path / "project.md").write_text(f"# {name}\n")
    return root, save_project_binding(name, str(root))


def _agent(owner=True, channel_id="checkpoint-integration"):
    return LuminaAgent(owner=owner, channel_id=channel_id, backend="llamacpp")


def _enable_coding(agent):
    apply_tool_profile(agent.registry, profile_name="Coding", owner=agent.owner)


def _activate(agent, name):
    result = agent.registry.call("activate_project", {"name": name})
    assert "Active project" in result


def _call_json(agent, name, args=None):
    return json.loads(agent.registry.call(name, args or {}))


def _save_args(expected_revision=0, **overrides):
    value = {
        "expected_revision": expected_revision,
        "task_id": "CODING-05A3",
        "phase": "in_progress",
        "summary": "adapter integration",
    }
    value.update(overrides)
    return value


def _row():
    from core.db import connect

    connection = connect()
    try:
        return dict(connection.execute("SELECT * FROM coding_checkpoints").fetchone())
    finally:
        connection.close()


def test_real_agent_registers_each_checkpoint_tool_once_with_exact_schemas():
    agent = _agent()
    isolated = ToolRegistry()
    register_coding_checkpoint_tools(isolated, ProjectContextState())
    names = agent.registry.all_tool_names()
    for name in CHECKPOINT_TOOLS:
        assert names.count(name) == 1
    assert agent.registry.get_schemas(list(CHECKPOINT_TOOLS)) == isolated.get_schemas(
        list(CHECKPOINT_TOOLS)
    )

    schemas = {
        item["function"]["name"]: item["function"]["parameters"]
        for item in isolated.get_schemas()
    }
    assert schemas["read_coding_checkpoint"] == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    save = schemas["save_coding_checkpoint"]
    assert save["required"] == ["expected_revision", "task_id"]
    assert save["additionalProperties"] is False
    assert FORBIDDEN_REALITY_FIELDS.isdisjoint(save["properties"])
    assert save["properties"]["validations"]["items"]["additionalProperties"] is False


def test_checkpoint_tool_tiers_and_independent_owner_only_membership_are_exact():
    assert {name: TOOL_TIERS[name] for name in CHECKPOINT_TOOLS} == EXPECTED_TIERS
    for name in CHECKPOINT_TOOLS:
        assert name in OWNER_ONLY_TOOLS


def test_checkpoint_tools_are_only_added_to_coding_profile():
    profiles = list_profiles()
    coding = next(item for item in profiles if item.get("name") == "Coding")
    assert set(CHECKPOINT_TOOLS).issubset(coding["enabled"])
    for profile in profiles:
        if profile.get("name") != "Coding":
            assert set(CHECKPOINT_TOOLS).isdisjoint(profile.get("enabled", []))


def test_owner_coding_matrix_and_project_requirement(tmp_path):
    agent = _agent(owner=True)
    _enable_coding(agent)
    assert set(CHECKPOINT_TOOLS).issubset(agent.registry.list_enabled())
    assert _call_json(agent, "read_coding_checkpoint")["error"] == "project_required"
    assert _call_json(agent, "save_coding_checkpoint", _save_args())["error"] == "project_required"

    _make_project(tmp_path)
    before = set(agent.registry.list_enabled())
    _activate(agent, "proj")
    assert set(agent.registry.list_enabled()) == before


def test_owner_explicit_empty_selection_disables_both_tools():
    agent = _agent(owner=True)
    apply_tool_profile(agent.registry, tools_enabled=[], owner=True)
    assert set(CHECKPOINT_TOOLS).isdisjoint(agent.registry.list_enabled())


@pytest.mark.parametrize("grant_kind", ["coding", "explicit"])
def test_nonowner_cannot_regain_checkpoint_tools_even_when_pin_verified(
    monkeypatch, grant_kind
):
    monkeypatch.setattr(pin_gate, "is_verified", lambda channel_id=None: True)
    agent = _agent(owner=False, channel_id=f"guest-{grant_kind}")
    if grant_kind == "coding":
        apply_tool_profile(agent.registry, profile_name="Coding", owner=False)
    else:
        apply_tool_profile(
            agent.registry, tools_enabled=list(CHECKPOINT_TOOLS), owner=False
        )
    assert set(CHECKPOINT_TOOLS).isdisjoint(agent.registry.list_enabled())
    for name in CHECKPOINT_TOOLS:
        assert "disabled" in agent.registry.call(name, {}).lower()


@pytest.mark.parametrize("tool_name", CHECKPOINT_TOOLS)
def test_each_checkpoint_tool_explicit_grant_is_independently_stripped_for_nonowner(
    tool_name,
):
    agent = _agent(owner=False, channel_id=f"independent-{tool_name}")
    apply_tool_profile(agent.registry, tools_enabled=[tool_name], owner=False)
    assert tool_name not in agent.registry.list_enabled()
    assert "disabled" in agent.registry.call(tool_name, {}).lower()


def test_unrelated_sensitive_pin_behavior_is_unchanged(monkeypatch):
    monkeypatch.setattr(pin_gate, "is_verified", lambda channel_id=None: False)
    agent = _agent(owner=False, channel_id="pin-separation")
    apply_tool_profile(agent.registry, tools_enabled=["run_command"], owner=False)
    assert agent.registry._gate_fn("run_command") == (
        False,
        "PIN verification required for this action.",
    )


def test_nonowner_subagent_cannot_inherit_or_explicitly_regain_tools(monkeypatch):
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
        "task", tools_enabled=list(CHECKPOINT_TOOLS), _parent_depth=0
    )
    assert result["success"] is True
    assert created[0].owner is False
    assert set(CHECKPOINT_TOOLS).isdisjoint(created[0].registry.list_enabled())


def test_coding_persona_parent_ownership_and_project_inheritance_do_not_restore_tools(
    tmp_path, monkeypatch
):
    import core.personas as personas_module
    import tools.subagent as subagent_module

    _root, context = _make_project(tmp_path)
    created = []

    class _NoChatAgent(LuminaAgent):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.enabled_after_persona = None
            created.append(self)

        def apply_persona(self, persona):
            super().apply_persona(persona)
            self.enabled_after_persona = set(self.registry.list_enabled())

        def chat(self, _task):
            return "done"

    monkeypatch.setattr(subagent_module, "LuminaAgent", _NoChatAgent)
    monkeypatch.setattr(
        personas_module,
        "list_personas",
        lambda: [{"name": "Coding Child", "_file": "coding-child.json"}],
    )
    monkeypatch.setattr(
        personas_module,
        "load_persona",
        lambda _path: {"name": "Coding Child", "tools_profile": "Coding"},
    )

    parent = _agent(True, "checkpoint-parent")
    _enable_coding(parent)
    parent.project_context.set(context)
    assert set(CHECKPOINT_TOOLS).issubset(parent.registry.list_enabled())
    result = subagent_module.spawn_subagent(
        "task",
        persona="Coding Child",
        _project_context=parent.project_context.snapshot(),
        _parent_depth=0,
    )
    assert result["success"] is True
    child = created[0]
    assert child.project_context.get() == context
    assert set(CHECKPOINT_TOOLS).isdisjoint(child.enabled_after_persona)
    assert set(CHECKPOINT_TOOLS).isdisjoint(child.registry.list_enabled())


def test_real_registry_save_read_stale_resave_and_validation_applicability(tmp_path):
    root, _context = _make_project(tmp_path)
    agent = _agent(owner=True)
    _enable_coding(agent)
    _activate(agent, "proj")

    saved = _call_json(
        agent,
        "save_coding_checkpoint",
        _save_args(
            relevant_paths=["README.md"],
            validations=[{
                "label": "pytest",
                "outcome": "pass",
                "exit_code": 0,
                "summary": "reported green",
            }],
        ),
    )
    assert saved["status"] == "saved"
    assert saved["revision"] == 1

    fresh = _call_json(agent, "read_coding_checkpoint")
    assert fresh["status"] == "ok"
    assert fresh["freshness"] == "fresh"
    assert fresh["validations"][0]["source"] == "reported"
    assert fresh["validations"][0]["applies_to_current_state"] is True

    (root / "README.md").write_text("changed\n")
    stale = _call_json(agent, "read_coding_checkpoint")
    assert stale["freshness"] == "stale"
    assert "working_state_changed" in stale["mismatch_reasons"]
    assert "relevant_files_changed" in stale["mismatch_reasons"]
    assert stale["validations"][0]["applies_to_current_state"] is False

    resaved = _call_json(
        agent, "save_coding_checkpoint", _save_args(1, summary="reconciled")
    )
    assert resaved["revision"] == 2
    final = _call_json(agent, "read_coding_checkpoint")
    assert final["freshness"] == "fresh"
    assert final["workflow"]["summary"] == "reconciled"


def test_two_agents_racing_same_revision_produce_one_save_and_one_conflict(tmp_path):
    _root, context = _make_project(tmp_path)
    agents = [_agent(True, "cas-a"), _agent(True, "cas-b")]
    for agent in agents:
        _enable_coding(agent)
        agent.project_context.set(context)
    first = _call_json(agents[0], "save_coding_checkpoint", _save_args())
    assert first["revision"] == 1

    def update(index):
        return _call_json(
            agents[index],
            "save_coding_checkpoint",
            _save_args(1, summary=f"writer-{index}"),
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, [0, 1]))
    assert sorted(item["status"] for item in results) == ["error", "saved"]
    assert [item for item in results if item["status"] == "error"][0]["error"] == "revision_conflict"
    loaded = _call_json(agents[0], "read_coding_checkpoint")
    assert loaded["revision"] == 2
    assert loaded["workflow"]["summary"] in {"writer-0", "writer-1"}


def test_project_switch_keeps_checkpoint_namespaces_isolated(tmp_path):
    _root_a, context_a = _make_project(tmp_path, "a")
    _root_b, context_b = _make_project(tmp_path, "b")
    agent = _agent(True)
    _enable_coding(agent)
    agent.project_context.set(context_a)
    assert _call_json(agent, "save_coding_checkpoint", _save_args(summary="A"))["revision"] == 1
    agent.project_context.set(context_b)
    assert _call_json(agent, "read_coding_checkpoint")["status"] == "not_found"
    assert _call_json(agent, "save_coding_checkpoint", _save_args(summary="B"))["revision"] == 1
    agent.project_context.set(context_a)
    assert _call_json(agent, "read_coding_checkpoint")["workflow"]["summary"] == "A"


def test_stale_binding_refuses_then_reactivation_uses_new_target(tmp_path):
    _old_root, old_context = _make_project(tmp_path)
    agent = _agent(True)
    _enable_coding(agent)
    agent.project_context.set(old_context)
    assert _call_json(agent, "save_coding_checkpoint", _save_args())["revision"] == 1

    new_root = tmp_path / "new-repo"
    new_root.mkdir()
    _init_repo(new_root)
    save_project_binding("proj", str(new_root))
    assert _call_json(agent, "read_coding_checkpoint")["error"] == "project_binding_changed"
    assert _call_json(agent, "save_coding_checkpoint", _save_args(1))["error"] == "project_binding_changed"

    _activate(agent, "proj")
    refreshed = _call_json(agent, "read_coding_checkpoint")
    assert refreshed["status"] == "ok"
    assert refreshed["freshness"] == "stale"
    assert "target_identity_changed" in refreshed["mismatch_reasons"]
    assert _call_json(agent, "save_coding_checkpoint", _save_args())["revision"] == 1
    assert _call_json(agent, "read_coding_checkpoint")["freshness"] == "fresh"


@pytest.mark.parametrize(
    "fake_field,fake_value",
    [
        ("state_ref", "model-fabricated"),
        ("head", "0" * 40),
        ("target_key", "1" * 64),
        ("binding_complete", True),
        ("commit_authorized", True),
    ],
)
def test_direct_fake_reality_or_authority_injection_is_rejected_without_write(
    tmp_path, fake_field, fake_value
):
    _root, context = _make_project(tmp_path)
    state = ProjectContextState(context)
    registry = ToolRegistry()
    register_coding_checkpoint_tools(registry, state)
    result = json.loads(
        registry.call(
            "save_coding_checkpoint",
            {**_save_args(), fake_field: fake_value},
        )
    )
    assert result["error"] == "invalid_checkpoint"
    loaded = checkpoint_observation.load_live_checkpoint(context)
    assert loaded.status == "not_found"


def test_missing_required_fields_are_stable_errors_not_dispatch_type_errors(tmp_path):
    _root, context = _make_project(tmp_path)
    registry = ToolRegistry()
    register_coding_checkpoint_tools(registry, ProjectContextState(context))
    for args in ({}, {"expected_revision": 0}, {"task_id": "task"}):
        result = json.loads(registry.call("save_coding_checkpoint", args))
        assert result["error"] == "invalid_checkpoint"
    read = json.loads(registry.call("read_coding_checkpoint", {"project": "proj"}))
    assert read["error"] == "invalid_checkpoint"


@pytest.mark.parametrize("damage", ["corrupt", "unsupported"])
def test_ordinary_save_refuses_unreadable_row_atomically(tmp_path, damage):
    _root, context = _make_project(tmp_path)
    agent = _agent(True)
    _enable_coding(agent)
    agent.project_context.set(context)
    assert _call_json(agent, "save_coding_checkpoint", _save_args())["revision"] == 1

    from core.db import connect

    connection = connect()
    if damage == "corrupt":
        connection.execute("UPDATE coding_checkpoints SET payload = ?", ("{broken",))
    else:
        connection.execute("UPDATE coding_checkpoints SET schema_version = 999")
    connection.commit()
    before = dict(connection.execute("SELECT * FROM coding_checkpoints").fetchone())
    connection.close()

    read = _call_json(agent, "read_coding_checkpoint")
    assert read["status"] == ("corrupt" if damage == "corrupt" else "unsupported_schema")
    assert "workflow" not in read
    attempted = _call_json(agent, "save_coding_checkpoint", _save_args(1))
    assert attempted["error"] == (
        "checkpoint_corrupt" if damage == "corrupt" else "unsupported_schema"
    )
    assert _row() == before


def test_unexpected_failure_is_generic_and_does_not_leak_raw_detail(tmp_path, monkeypatch):
    _root, context = _make_project(tmp_path)
    registry = ToolRegistry()
    register_coding_checkpoint_tools(registry, ProjectContextState(context))
    monkeypatch.setattr(
        checkpoint_observation,
        "save_live_checkpoint",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("SECRET /root/path")),
    )
    raw = registry.call("save_coding_checkpoint", _save_args())
    result = json.loads(raw)
    assert result["error"] == "checkpoint_operation_failed"
    assert "SECRET" not in raw
    assert "/root/path" not in raw


def test_result_budget_is_live_valid_json_and_deterministically_degrades(tmp_path, monkeypatch):
    _root, context = _make_project(tmp_path)
    agent = _agent(True)
    _enable_coding(agent)
    agent.project_context.set(context)
    summary = 'quoted "value" and newline\n' + ("x" * 900)
    saved = _call_json(
        agent,
        "save_coding_checkpoint",
        _save_args(
            summary=summary,
            next_steps=["n" * 150] * 8,
            blockers=["b" * 150] * 8,
            validations=[{
                "label": f"gate-{number}",
                "outcome": "pass",
                "summary": "v" * 200,
            } for number in range(8)],
        ),
    )
    assert saved["status"] == "saved"

    ordinary = agent.registry.call("read_coding_checkpoint", {})
    ordinary_payload = json.loads(ordinary)
    assert len(ordinary) <= config.TOOL_RESULT_MAX_CHARS
    assert ordinary_payload["workflow"]["summary"] == summary
    assert ordinary_payload["output_truncated"] is False

    monkeypatch.setattr(config, "TOOL_RESULT_MAX_CHARS", 240)
    first = agent.registry.call("read_coding_checkpoint", {})
    second = agent.registry.call("read_coding_checkpoint", {})
    assert first == second
    assert len(first) <= 240
    compact = json.loads(first)
    assert compact["status"] == "ok"
    assert compact["revision"] == 1
    assert compact["freshness"] == "fresh"
    assert compact["output_truncated"] is True

    monkeypatch.setattr(config, "TOOL_RESULT_MAX_CHARS", 2)
    tiny = agent.registry.call("read_coding_checkpoint", {})
    assert len(tiny) <= 2
    assert json.loads(tiny) == {}

    no_project = _agent(True, "budget-error")
    _enable_coding(no_project)
    monkeypatch.setattr(config, "TOOL_RESULT_MAX_CHARS", 32)
    bounded_error = no_project.registry.call("read_coding_checkpoint", {})
    assert len(bounded_error) <= 32
    assert json.loads(bounded_error)["error"] == "project_required"


def test_result_budget_of_one_returns_smallest_valid_json_scalar(tmp_path, monkeypatch):
    _root, context = _make_project(tmp_path)
    agent = _agent(True)
    _enable_coding(agent)
    agent.project_context.set(context)
    assert _call_json(agent, "save_coding_checkpoint", _save_args())["revision"] == 1

    monkeypatch.setattr(config, "TOOL_RESULT_MAX_CHARS", 1)
    first = agent.registry.call("read_coding_checkpoint", {})
    second = agent.registry.call("read_coding_checkpoint", {})
    assert first == second == "0"
    assert json.loads(first) == 0


def test_result_budget_of_zero_is_the_documented_exceptional_empty_case(
    tmp_path, monkeypatch
):
    _root, context = _make_project(tmp_path)
    agent = _agent(True)
    _enable_coding(agent)
    agent.project_context.set(context)
    assert _call_json(agent, "save_coding_checkpoint", _save_args())["revision"] == 1

    monkeypatch.setattr(config, "TOOL_RESULT_MAX_CHARS", 0)
    first = agent.registry.call("read_coding_checkpoint", {})
    second = agent.registry.call("read_coding_checkpoint", {})
    assert first == second == ""
    with pytest.raises(json.JSONDecodeError):
        json.loads(first)


@pytest.mark.parametrize("misconfigured", [-1, -500, None, "not-a-number"])
def test_negative_or_unparsable_budget_clamps_to_zero_not_unlimited(
    tmp_path, monkeypatch, misconfigured
):
    _root, context = _make_project(tmp_path)
    agent = _agent(True)
    _enable_coding(agent)
    agent.project_context.set(context)
    assert _call_json(agent, "save_coding_checkpoint", _save_args())["revision"] == 1

    monkeypatch.setattr(config, "TOOL_RESULT_MAX_CHARS", misconfigured)
    result = agent.registry.call("read_coding_checkpoint", {})
    assert result == ""


def test_bounded_error_path_also_honors_budgets_one_and_zero(monkeypatch):
    no_project = _agent(True, "budget-error-tiny")
    _enable_coding(no_project)

    monkeypatch.setattr(config, "TOOL_RESULT_MAX_CHARS", 1)
    at_one = no_project.registry.call("read_coding_checkpoint", {})
    assert at_one == "0"

    monkeypatch.setattr(config, "TOOL_RESULT_MAX_CHARS", 0)
    at_zero = no_project.registry.call("read_coding_checkpoint", {})
    assert at_zero == ""


def test_adapter_source_does_not_probe_git_hash_or_authority_itself():
    import tools.coding_checkpoint as adapter

    source = open(adapter.__file__, encoding="utf-8").read()
    for forbidden in (
        "subprocess",
        "hashlib",
        "_run_git",
        "resolve_target_identity",
        "commit_authorized",
        "push_authorized",
    ):
        assert forbidden not in source


def test_emergency_stop_blocks_dispatch_and_rearm_restores_same_profile(tmp_path):
    _root, context = _make_project(tmp_path)
    agent = _agent(True)
    _enable_coding(agent)
    agent.project_context.set(context)
    before = set(agent.registry.list_enabled())
    assert _call_json(agent, "save_coding_checkpoint", _save_args())["revision"] == 1

    emergency_stop.latch("checkpoint-test")
    blocked = agent.registry.call("read_coding_checkpoint", {})
    assert "emergency stop active" in blocked.lower()
    emergency_stop.rearm_local()
    assert set(agent.registry.list_enabled()) == before
    assert _call_json(agent, "read_coding_checkpoint")["revision"] == 1
