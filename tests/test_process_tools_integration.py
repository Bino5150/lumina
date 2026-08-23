"""CODING-04A2-B4 authority and real-agent integration regressions."""

import json
import shlex
import subprocess
import sys
import time

import pytest

import core.pin_gate as pin_gate
import core.secrets as secrets_module
from core import emergency_stop, persistence, process_manager
from core.agent import LuminaAgent
from core.project_context import ProjectContext
from core.tool_profiles import (
    OWNER_ONLY_TOOLS,
    TOOL_TIERS,
    apply_tool_profile,
    list_profiles,
)
from tools.processes import register_process_tools
from tools.registry import ToolRegistry
from process_test_helpers import python_command


PROCESS_TOOLS = (
    "start_process",
    "read_process",
    "send_process_input",
    "stop_process",
    "list_processes",
)

EXPECTED_TIERS = {
    "start_process": "execute",
    "read_process": "read_only",
    "send_process_input": "execute",
    "stop_process": "execute",
    "list_processes": "read_only",
}


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(
        secrets_module, "SECRETS_PATH", str(tmp_path / "credentials.json")
    )
    emergency_stop._reset_for_tests()
    process_manager._reset_for_tests()
    yield
    process_manager._reset_for_tests()
    emergency_stop._reset_for_tests()


def _agent(owner=True, channel_id="process-integration"):
    return LuminaAgent(owner=owner, channel_id=channel_id, backend="llamacpp")


def _enable_coding(agent):
    apply_tool_profile(agent.registry, profile_name="Coding", owner=agent.owner)


def _wait_for_terminal(agent, process_id, timeout=10.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = json.loads(agent.registry.call("read_process", {"process_id": process_id}))
        if last["status"] != "running":
            return last
        time.sleep(0.02)
    pytest.fail(f"process did not reach terminal state: {last}")


def test_python_command_quotes_both_platform_branches(monkeypatch):
    source = "import sys; print(repr(sys.argv[1]))"
    argument = 'value with spaces and "quotes"'
    expected_argv = [sys.executable, "-u", "-c", source, argument]

    posix_command = python_command(source, argument, platform_name="posix")
    assert shlex.split(posix_command) == expected_argv

    calls = []
    monkeypatch.setattr(
        subprocess,
        "list2cmdline",
        lambda argv: calls.append(argv) or "windows-quoted-command",
    )
    windows_command = python_command(source, argument, platform_name="nt")
    assert calls == [expected_argv]
    assert windows_command == "windows-quoted-command"


def test_real_agent_registers_each_process_tool_once_with_existing_schemas():
    agent = _agent()
    names = agent.registry.all_tool_names()
    for name in PROCESS_TOOLS:
        assert names.count(name) == 1

    isolated = ToolRegistry()
    register_process_tools(isolated)
    assert agent.registry.get_schemas(list(PROCESS_TOOLS)) == isolated.get_schemas(
        list(PROCESS_TOOLS)
    )


def test_process_tool_tiers_are_exact():
    assert {name: TOOL_TIERS[name] for name in PROCESS_TOOLS} == EXPECTED_TIERS


@pytest.mark.parametrize("tool_name", PROCESS_TOOLS)
def test_every_process_tool_is_independently_owner_only(tool_name):
    assert tool_name in OWNER_ONLY_TOOLS


def test_coding_profile_contains_exactly_all_five_process_tools():
    coding = next(p for p in list_profiles() if p.get("name") == "Coding")
    assert set(PROCESS_TOOLS).issubset(coding["enabled"])


def test_owner_coding_agent_enables_all_five_process_tools():
    agent = _agent(owner=True)
    _enable_coding(agent)
    assert set(PROCESS_TOOLS).issubset(agent.registry.list_enabled())


def test_owner_status_alone_does_not_override_explicit_empty_selection():
    agent = _agent(owner=True)
    apply_tool_profile(agent.registry, tools_enabled=[], owner=True)
    assert set(PROCESS_TOOLS).isdisjoint(agent.registry.list_enabled())


@pytest.mark.parametrize("grant_kind", ["coding", "explicit"])
def test_nonowner_cannot_regain_any_process_tool(grant_kind):
    agent = _agent(owner=False, channel_id=f"guest-{grant_kind}")
    if grant_kind == "coding":
        apply_tool_profile(agent.registry, profile_name="Coding", owner=False)
    else:
        apply_tool_profile(
            agent.registry, tools_enabled=list(PROCESS_TOOLS), owner=False
        )
    assert set(PROCESS_TOOLS).isdisjoint(agent.registry.list_enabled())
    for name in PROCESS_TOOLS:
        assert "disabled" in agent.registry.call(name, {}).lower()


def test_verified_nonowner_still_cannot_regain_process_tools(monkeypatch):
    monkeypatch.setattr(pin_gate, "is_verified", lambda channel_id=None: True)
    agent = _agent(owner=False, channel_id="verified-guest")
    apply_tool_profile(agent.registry, tools_enabled=list(PROCESS_TOOLS), owner=False)
    assert set(PROCESS_TOOLS).isdisjoint(agent.registry.list_enabled())
    assert agent.registry._gate_fn("start_process") == (True, "")


def test_unrelated_sensitive_tool_pin_behavior_is_unchanged(monkeypatch):
    monkeypatch.setattr(pin_gate, "is_verified", lambda channel_id=None: False)
    agent = _agent(owner=False, channel_id="pin-separation")
    apply_tool_profile(agent.registry, tools_enabled=["run_command"], owner=False)
    assert "run_command" in agent.registry.list_enabled()
    allowed, reason = agent.registry._gate_fn("run_command")
    assert allowed is False
    assert reason == "PIN verification required for this action."


def test_project_activation_never_changes_process_capability(tmp_path):
    project_root = tmp_path / "project capability root"
    project_root.mkdir()
    owner = _agent(owner=True, channel_id="project-owner")
    apply_tool_profile(owner.registry, tools_enabled=[], owner=True)
    before = owner.registry.list_enabled()
    owner.project_context.set(ProjectContext(name="p", root=str(project_root)))
    assert owner.registry.list_enabled() == before
    assert set(PROCESS_TOOLS).isdisjoint(before)

    _enable_coding(owner)
    assert set(PROCESS_TOOLS).issubset(owner.registry.list_enabled())

    guest = _agent(owner=False, channel_id="project-guest")
    guest.project_context.set(ProjectContext(name="p", root=str(project_root)))
    apply_tool_profile(guest.registry, profile_name="Coding", owner=False)
    assert set(PROCESS_TOOLS).isdisjoint(guest.registry.list_enabled())


def test_nonowner_subagent_cannot_inherit_or_explicitly_regain_process_tools(monkeypatch):
    import tools.subagent as subagent_module

    created = []

    class _NoChatAgent(LuminaAgent):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            created.append(self)

        def chat(self, _task):
            return "done"

    monkeypatch.setattr(subagent_module, "LuminaAgent", _NoChatAgent)
    parent = _agent(owner=True, channel_id="process-parent")
    _enable_coding(parent)
    assert set(PROCESS_TOOLS).issubset(parent.registry.list_enabled())

    result = subagent_module.spawn_subagent(
        "task", tools_enabled=list(PROCESS_TOOLS), _parent_depth=0
    )
    assert result["success"] is True
    child = created[0]
    assert child.owner is False
    assert set(PROCESS_TOOLS).isdisjoint(child.registry.list_enabled())


@pytest.mark.parametrize("grant_mode", ["none", "coding_persona"])
def test_normal_and_coding_profile_subagents_receive_no_process_tools(
    monkeypatch, grant_mode
):
    import core.personas as personas_module
    import tools.subagent as subagent_module

    created = []

    class _NoChatAgent(LuminaAgent):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.enabled_after_persona = None
            created.append(self)

        def chat(self, _task):
            return "done"

        def apply_persona(self, persona):
            super().apply_persona(persona)
            self.enabled_after_persona = set(self.registry.list_enabled())

    monkeypatch.setattr(subagent_module, "LuminaAgent", _NoChatAgent)
    kwargs = {}
    if grant_mode == "coding_persona":
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
        kwargs["persona"] = "Coding Child"

    result = subagent_module.spawn_subagent("task", _parent_depth=0, **kwargs)
    assert result["success"] is True
    assert created[0].owner is False
    assert set(PROCESS_TOOLS).isdisjoint(created[0].registry.list_enabled())
    if grant_mode == "coding_persona":
        assert set(PROCESS_TOOLS).isdisjoint(created[0].enabled_after_persona)


def test_process_records_are_process_global_not_channel_filtered():
    launcher = _agent(owner=True, channel_id="owner-a")
    observer = _agent(owner=True, channel_id="owner-b")
    _enable_coding(launcher)
    _enable_coding(observer)

    command = python_command("import sys; sys.stdin.read()")
    started = json.loads(
        launcher.registry.call("start_process", {"command": command})
    )
    process_id = started["process_id"]
    listed = json.loads(observer.registry.call("list_processes", {}))
    assert process_id in {item["process_id"] for item in listed["processes"]}

    stopped = json.loads(observer.registry.call("stop_process", {"process_id": process_id}))
    assert stopped["process_id"] == process_id
    assert stopped["stop_requested"] is True
    _wait_for_terminal(observer, process_id)


def test_internal_argv_job_is_unaddressable_through_all_existing_model_tools(tmp_path):
    agent = _agent(owner=True, channel_id="internal-visibility-proof")
    _enable_coding(agent)
    process_id = process_manager.launch_argv(
        [sys.executable, "-u", "-c", "import time; time.sleep(30)"],
        cwd=str(tmp_path),
    )
    try:
        listed = json.loads(agent.registry.call("list_processes", {}))
        assert process_id not in {
            item["process_id"] for item in listed["processes"]
        }

        for tool_name, arguments in (
            ("read_process", {"process_id": process_id}),
            ("send_process_input", {"process_id": process_id, "text": "x"}),
            ("stop_process", {"process_id": process_id}),
        ):
            result = json.loads(agent.registry.call(tool_name, arguments))
            assert result["error"] == "process_not_found"
    finally:
        process_manager.shutdown_all(wait_seconds=5)
        process_manager.wait_for_completion(process_id, timeout=5)


def test_real_owner_registry_process_lifecycle_and_emergency_blast_door(tmp_path):
    agent = _agent(owner=True, channel_id="registry-smoke")
    _enable_coding(agent)
    command = python_command(
        "import sys; data=sys.stdin.read(); "
        "print('ECHO:' + data.strip(), flush=True)"
    )
    working_dir = tmp_path / "registry path with spaces"
    working_dir.mkdir()

    started = json.loads(agent.registry.call(
        "start_process", {"command": command, "cwd": str(working_dir)}
    ))
    process_id = started["process_id"]
    assert started["status"] == "running"
    listed = json.loads(agent.registry.call("list_processes", {}))
    assert process_id in {item["process_id"] for item in listed["processes"]}

    sent = json.loads(agent.registry.call("send_process_input", {
        "process_id": process_id,
        "text": "hello\n",
        "close_stdin": True,
    }))
    assert sent["process_id"] == process_id
    terminal = _wait_for_terminal(agent, process_id)
    assert terminal["status"] == "exited"
    assert terminal["stdout"]["text"] == "ECHO:hello\n"
    assert emergency_stop.is_latched() is False

    emergency_stop.latch("integration-test")
    blocked = agent.registry.call("start_process", {"command": command})
    assert "emergency stop active" in blocked.lower()
    emergency_stop.rearm_local()
    assert emergency_stop.is_latched() is False
