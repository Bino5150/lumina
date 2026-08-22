"""CODING-04A2-B3 isolated active persistent-process adapters."""

import json
import os
import tempfile
import time

import pytest

import config
from core import emergency_stop, process_manager, process_control
from core.project_context import ProjectContext, ProjectContextState
from core.tool_profiles import TOOL_TIERS
from tools.processes import register_process_tools
from process_test_helpers import (
    descendant_python_command,
    python_command,
    sleeping_python_command,
    wait_for_path,
)


@pytest.fixture(autouse=True)
def _clean_process_state():
    process_manager._reset_for_tests()
    emergency_stop._reset_for_tests()
    yield
    process_manager._reset_for_tests()
    emergency_stop._reset_for_tests()


class _Registry:
    def __init__(self):
        self.fns = {}
        self.schemas = {}

    def register(self, name, fn, description, parameters):
        self.fns[name] = fn
        self.schemas[name] = parameters


def _tools(project_state=None, channel_id="bound-channel"):
    registry = _Registry()
    register_process_tools(
        registry, project_state=project_state, channel_id=channel_id
    )
    return registry


def _snapshot(process_id="p-000000000001", cwd=None, status="running",
              returncode=None, channel_id="internal-channel"):
    if cwd is None:
        cwd = os.path.join(tempfile.gettempdir(), "project")
    return {
        "process_id": process_id,
        "command": "worker command",
        "cwd": cwd,
        "channel_id": channel_id,
        "epoch": 7,
        "pid": 9001,
        "control_metadata": {"backend": "test", "opaque_identity": 9001},
        "status": status,
        "returncode": returncode,
        "termination_reason": None,
        "start_at": 1.0,
        "finish_at": 2.0 if status != "running" else None,
        "stdout": {"start_offset": 0, "end_offset": 0, "text": ""},
        "stderr": {"start_offset": 0, "end_offset": 0, "text": ""},
    }


def _fake_launch(monkeypatch, snapshot=None):
    if snapshot is None:
        snapshot = _snapshot()
    calls = []

    def launch(command, cwd, channel_id=""):
        calls.append({"command": command, "cwd": cwd, "channel_id": channel_id})
        return snapshot["process_id"]

    monkeypatch.setattr(process_manager, "launch", launch)
    monkeypatch.setattr(process_manager, "get_job_snapshot", lambda pid: snapshot)
    return calls, snapshot


def test_five_active_and_read_schemas_expose_no_bound_dependencies():
    registry = _tools(project_state=object(), channel_id="secret")
    assert list(registry.fns) == [
        "start_process", "read_process", "send_process_input",
        "stop_process", "list_processes",
    ]
    assert set(registry.schemas["start_process"]["properties"]) == {"command", "cwd"}
    assert set(registry.schemas["send_process_input"]["properties"]) == {
        "process_id", "text", "close_stdin"
    }
    assert set(registry.schemas["stop_process"]["properties"]) == {"process_id"}
    serialized = json.dumps(registry.schemas)
    for prohibited in ("project_state", "channel_id", "manager", "process_control"):
        assert prohibited not in serialized
    assert TOOL_TIERS["start_process"] == "execute"
    assert TOOL_TIERS["send_process_input"] == "execute"
    assert TOOL_TIERS["stop_process"] == "execute"


def test_start_omitted_cwd_without_project_uses_home(monkeypatch):
    calls, snapshot = _fake_launch(monkeypatch)
    result = json.loads(_tools().fns["start_process"]("echo hello"))
    expected = os.path.expanduser("~")
    assert calls == [{"command": "echo hello", "cwd": expected, "channel_id": "bound-channel"}]
    assert result == {
        "process_id": snapshot["process_id"], "status": "running", "cwd": expected
    }


@pytest.mark.parametrize(
    "supplied,expected",
    [
        (os.path.abspath(os.path.join("absolute", "path")),
         os.path.abspath(os.path.join("absolute", "path"))),
        ("~", os.path.expanduser("~")),
        ("relative-dir", os.path.abspath("relative-dir")),
    ],
)
def test_start_explicit_cwd_without_project_resolves_from_lumina(
        monkeypatch, supplied, expected):
    calls, _ = _fake_launch(monkeypatch, _snapshot(cwd=expected))
    result = json.loads(_tools().fns["start_process"]("echo hello", cwd=supplied))
    assert calls[0]["cwd"] == expected
    assert result["cwd"] == expected


def test_start_omitted_cwd_with_active_project_uses_project_root(monkeypatch, tmp_path):
    state = ProjectContextState(ProjectContext("A", str(tmp_path)))
    calls, _ = _fake_launch(monkeypatch, _snapshot(cwd=str(tmp_path)))
    result = json.loads(_tools(state).fns["start_process"]("echo hello"))
    assert calls[0]["cwd"] == str(tmp_path)
    assert result["cwd"] == str(tmp_path)


@pytest.mark.parametrize(
    "supplied", [os.path.abspath(os.path.join("absolute", "path")), "~", "relative-dir"]
)
def test_start_explicit_cwd_overrides_active_project(monkeypatch, tmp_path, supplied):
    project_root = tmp_path / "project"
    project_root.mkdir()
    state = ProjectContextState(ProjectContext("A", str(project_root)))
    expected = os.path.abspath(os.path.expanduser(supplied))
    calls, _ = _fake_launch(monkeypatch, _snapshot(cwd=expected))
    json.loads(_tools(state).fns["start_process"]("echo hello", cwd=supplied))
    assert calls[0]["cwd"] == expected
    if not os.path.isabs(supplied):
        assert expected != os.path.abspath(os.path.join(project_root, supplied))


def test_project_switch_does_not_retarget_existing_job(tmp_path):
    root_a = tmp_path / "A"
    root_b = tmp_path / "B"
    root_a.mkdir()
    root_b.mkdir()
    state = ProjectContextState(ProjectContext("A", str(root_a)))
    start = _tools(state).fns["start_process"]

    first = json.loads(start(sleeping_python_command()))
    state.set(ProjectContext("B", str(root_b)))
    second = json.loads(start(python_command("pass")))
    try:
        assert first["cwd"] == str(root_a)
        assert process_manager.get_job_snapshot(first["process_id"])["cwd"] == str(root_a)
        assert second["cwd"] == str(root_b)
        assert process_manager.get_job_snapshot(second["process_id"])["cwd"] == str(root_b)
    finally:
        process_manager.request_stop(first["process_id"])


def test_start_passes_bound_channel_as_internal_provenance_without_leaking(monkeypatch):
    calls, snapshot = _fake_launch(monkeypatch)
    raw = _tools(channel_id="private-provenance").fns["start_process"]("worker command")
    assert calls[0]["channel_id"] == "private-provenance"
    assert "private-provenance" not in raw
    assert "channel_id" not in raw
    assert "pid" not in raw
    assert json.loads(raw)["process_id"] == snapshot["process_id"]


def test_start_snapshot_race_is_reported_without_inventing_running_state(monkeypatch):
    monkeypatch.setattr(
        process_manager, "launch", lambda command, cwd, channel_id="": "p-000000000001"
    )
    monkeypatch.setattr(process_manager, "get_job_snapshot", lambda pid: None)
    result = json.loads(_tools().fns["start_process"](
        "worker command", cwd=tempfile.gettempdir()
    ))
    assert result["status"] == "snapshot_unavailable"
    assert result["process_id"] == "p-000000000001"


def test_start_catastrophic_command_uses_real_manager_guardrail():
    result = json.loads(_tools().fns["start_process"]("rm -rf /"))
    assert result["error"] == "command_blocked"
    assert process_manager.list_job_ids() == []


def test_start_emergency_latched_is_refused_without_phantom_job():
    emergency_stop.latch(source="test", reason="B3 adapter authority")
    result = json.loads(_tools().fns["start_process"](python_command("pass")))
    assert result["error"] == "launch_refused"
    assert process_manager.list_job_ids() == []


@pytest.mark.parametrize(
    "error,code",
    [
        (process_manager.InvalidLaunchRequest("secret path"), "invalid_launch"),
        (process_manager.CommandBlocked("raw guardrail detail"), "command_blocked"),
        (process_manager.LiveProcessLimitReached("raw limit"), "process_limit_reached"),
        (process_manager.LaunchRefused("raw epoch"), "launch_refused"),
        (RuntimeError("raw spawn failure"), "process_operation_failed"),
    ],
)
def test_start_translates_manager_failures_without_raw_details(monkeypatch, error, code):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(process_manager, "launch", fail)
    raw = _tools().fns["start_process"](
        "worker command", cwd=tempfile.gettempdir()
    )
    assert json.loads(raw)["error"] == code
    assert "raw" not in raw
    assert "secret path" not in raw


def test_failed_real_spawn_leaves_no_model_visible_phantom(monkeypatch):
    def fail_spawn(command, cwd):
        raise process_control.ProcessControlError("private spawn detail")

    monkeypatch.setattr(process_control, "spawn", fail_spawn)
    result = json.loads(_tools().fns["start_process"](
        "worker command", cwd=tempfile.gettempdir()
    ))
    assert result["error"] == "process_operation_failed"
    assert process_manager.list_job_ids() == []


@pytest.mark.parametrize(
    "text,close_stdin",
    [("exact", False), ("\n", False), ("", False), ("", True), ("payload", True)],
)
def test_input_adapter_forwards_exactly_through_manager(
        monkeypatch, text, close_stdin):
    calls = []

    def send_input(process_id, text="", close_stdin=False):
        calls.append((process_id, text, close_stdin))
        return {
            "process_id": process_id,
            "chars_written": len(text),
            "stdin_closed": close_stdin,
            "already_closed": False,
        }

    monkeypatch.setattr(process_manager, "send_input", send_input)
    result = json.loads(_tools().fns["send_process_input"](
        "p-000000000001", text=text, close_stdin=close_stdin
    ))
    assert calls == [("p-000000000001", text, close_stdin)]
    assert result["chars_written"] == len(text)
    assert result["stdin_closed"] is close_stdin


@pytest.mark.parametrize(
    "error,code",
    [
        (process_manager.InvalidProcessInput("raw"), "invalid_process_input"),
        (process_manager.ProcessNotFound("raw"), "process_not_found"),
        (process_manager.ProcessNotRunning("raw"), "process_not_running"),
        (process_manager.ProcessInputClosed("raw"), "process_input_closed"),
        (process_manager.ProcessInputRefused("raw"), "process_input_refused"),
        (process_manager.ProcessInputError("BrokenPipeError raw"), "process_input_failed"),
    ],
)
def test_input_adapter_translates_manager_errors_without_pipe_details(
        monkeypatch, error, code):
    monkeypatch.setattr(process_manager, "send_input", lambda *a, **k: (_ for _ in ()).throw(error))
    raw = _tools().fns["send_process_input"]("p-000000000001", text="x")
    assert json.loads(raw)["error"] == code
    for prohibited in ("BrokenPipeError", "OSError", "ValueError", "raw"):
        assert prohibited not in raw


def test_input_adapter_error_is_self_bounded(monkeypatch):
    monkeypatch.setattr(
        process_manager, "send_input",
        lambda *a, **k: (_ for _ in ()).throw(process_manager.ProcessInputError("raw")),
    )
    monkeypatch.setattr(config, "TOOL_RESULT_MAX_CHARS", 2)
    assert _tools().fns["send_process_input"]("p-000000000001") == "{}"


def test_input_adapter_preserves_truthful_repeated_close_result(monkeypatch):
    calls = []

    def close(process_id, text="", close_stdin=False):
        calls.append((text, close_stdin))
        return {
            "process_id": process_id,
            "chars_written": 0,
            "stdin_closed": True,
            "already_closed": len(calls) > 1,
        }

    monkeypatch.setattr(process_manager, "send_input", close)
    send = _tools().fns["send_process_input"]
    first = json.loads(send("p-000000000001", close_stdin=True))
    second = json.loads(send("p-000000000001", close_stdin=True))
    assert first["already_closed"] is False
    assert second["already_closed"] is True
    assert calls == [("", True), ("", True)]


def test_stop_request_true_does_not_claim_immediate_death(monkeypatch):
    snap = _snapshot(status="running")
    calls = []
    monkeypatch.setattr(process_manager, "get_job_snapshot", lambda pid: snap)
    monkeypatch.setattr(
        process_manager, "request_stop", lambda pid: calls.append(pid) or True
    )
    result = json.loads(_tools().fns["stop_process"](snap["process_id"]))
    assert calls == [snap["process_id"]]
    assert result == {
        "process_id": snap["process_id"],
        "stop_requested": True,
        "status": "running",
        "terminal_observed": False,
    }
    assert "terminated" not in json.dumps(result)


def test_stop_already_terminal_is_truthful(monkeypatch):
    snap = _snapshot(status="exited", returncode=0)
    monkeypatch.setattr(process_manager, "get_job_snapshot", lambda pid: snap)
    monkeypatch.setattr(process_manager, "request_stop", lambda pid: False)
    result = json.loads(_tools().fns["stop_process"](snap["process_id"]))
    assert result["stop_requested"] is False
    assert result["status"] == "exited"
    assert result["terminal_observed"] is True


def test_stop_unknown_is_explicit_and_never_calls_request(monkeypatch):
    monkeypatch.setattr(process_manager, "get_job_snapshot", lambda pid: None)

    def forbidden(pid):
        raise AssertionError("unknown ID must not reach request_stop")

    monkeypatch.setattr(process_manager, "request_stop", forbidden)
    result = json.loads(_tools().fns["stop_process"]("p-ffffffffffff"))
    assert result["error"] == "process_not_found"


def test_stop_repeated_request_remains_request_not_death_claim(monkeypatch):
    snap = _snapshot(status="running")
    monkeypatch.setattr(process_manager, "get_job_snapshot", lambda pid: snap)
    monkeypatch.setattr(process_manager, "request_stop", lambda pid: True)
    stop = _tools().fns["stop_process"]
    first = json.loads(stop(snap["process_id"]))
    second = json.loads(stop(snap["process_id"]))
    assert first == second
    assert second["stop_requested"] is True
    assert second["terminal_observed"] is False


def test_stop_real_leader_exited_descendant_reaches_eventual_tree_death(tmp_path):
    tools = _tools().fns
    sentinel = tmp_path / "active adapter descendant.pid"
    started = json.loads(tools["start_process"](
        descendant_python_command(sentinel), cwd=str(tmp_path)
    ))
    process_id = started["process_id"]
    assert wait_for_path(sentinel), "descendant did not report startup"
    immediate = json.loads(tools["stop_process"](process_id))
    assert immediate["stop_requested"] is True
    deadline = time.time() + 10.0
    while time.time() < deadline:
        snapshot = process_manager.get_job_snapshot(process_id)
        if snapshot["status"] != "running":
            break
        time.sleep(0.02)
    assert snapshot["status"] == "stopped"


def test_active_adapters_delegate_only_through_public_manager_contracts():
    import inspect
    from tools import processes

    source = inspect.getsource(processes.register_process_tools)
    assert "process_manager.launch(" in source
    assert "process_manager.send_input(" in source
    assert "process_manager.request_stop(" in source
    assert "._registry" not in source
    assert ".stdin" not in source
    assert "subprocess" not in source
