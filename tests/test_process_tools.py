"""CODING-04A2-B2 isolated read-only persistent-process adapters."""

import json
import os
import tempfile
import time

import pytest

import config
from core import emergency_stop, process_manager
from core.tool_profiles import TOOL_TIERS
from tools.processes import MAX_CHARS_PER_STREAM, register_process_tools
from process_test_helpers import python_command


@pytest.fixture(autouse=True)
def _clean_real_manager_state():
    process_manager._reset_for_tests()
    emergency_stop._reset_for_tests()
    yield
    process_manager._reset_for_tests()
    emergency_stop._reset_for_tests()


class _CapturingRegistry:
    def __init__(self):
        self.fns = {}
        self.registrations = {}

    def register(self, name, fn, description, parameters):
        self.fns[name] = fn
        self.registrations[name] = {
            "description": description,
            "parameters": parameters,
        }


def _registered(project_state=None, channel_id="private-channel"):
    registry = _CapturingRegistry()
    register_process_tools(
        registry, project_state=project_state, channel_id=channel_id
    )
    return registry


def _stream(text="", start=0):
    return {
        "start_offset": start,
        "end_offset": start + len(text),
        "text": text,
    }


def _snapshot(process_id="p-000000000001", stdout="", stderr="",
              stdout_start=0, stderr_start=0, status="running",
              returncode=None, channel_id="secret-channel"):
    return {
        "process_id": process_id,
        "command": "worker command",
        "cwd": os.path.join(tempfile.gettempdir(), "project"),
        "channel_id": channel_id,
        "epoch": 99,
        "pid": 4242,
        "control_metadata": {
            "backend": "test", "pgid": 4242, "job_handle": "private-handle"
        },
        "status": status,
        "returncode": returncode,
        "termination_reason": None,
        "start_at": 100.0,
        "finish_at": 200.0 if status != "running" else None,
        "stdout": _stream(stdout, stdout_start),
        "stderr": _stream(stderr, stderr_start),
    }


def _install_snapshots(monkeypatch, snapshots):
    by_id = {snap["process_id"]: snap for snap in snapshots}
    monkeypatch.setattr(
        process_manager, "list_job_ids", lambda: list(by_id.keys())
    )
    monkeypatch.setattr(
        process_manager, "get_job_snapshot", lambda process_id: by_id.get(process_id)
    )


def _read(monkeypatch, snapshot=None):
    if snapshot is None:
        snapshot = _snapshot()
    _install_snapshots(monkeypatch, [snapshot])
    return _registered().fns["read_process"], snapshot["process_id"]


def _iter_json_keys(node):
    """Yield every dict key appearing anywhere in a decoded JSON structure."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _iter_json_keys(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_json_keys(item)


def _iter_json_string_values(node):
    """Yield every string leaf value appearing anywhere in a decoded JSON
    structure -- e.g. a legitimate `cwd` path is a value, never a key, so
    checking key names and value content separately (CODING-08R.1C) lets a
    real user-visible path coincidentally containing a forbidden substring
    (e.g. ".../lumina-release/..." contains "lease") pass, while an actual
    forbidden key or a genuinely secret value still fails."""
    if isinstance(node, dict):
        for value in node.values():
            yield from _iter_json_string_values(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_json_string_values(item)
    elif isinstance(node, str):
        yield node


# Internal snapshot fields (core/process_manager.py) that model-visible tool
# output must never expose as JSON keys, regardless of what legitimate
# values (a cwd path, say) might incidentally contain as a substring.
_FORBIDDEN_METADATA_KEYS = frozenset({
    "channel_id", "epoch", "pid", "pgid", "control_metadata", "lease", "job_handle",
})


def test_registrar_contains_all_five_tools_with_intended_tiers():
    registry = _registered()
    assert list(registry.fns) == [
        "start_process", "read_process", "send_process_input",
        "stop_process", "list_processes",
    ]
    assert TOOL_TIERS["read_process"] == "read_only"
    assert TOOL_TIERS["list_processes"] == "read_only"
    assert TOOL_TIERS["start_process"] == "execute"
    assert TOOL_TIERS["send_process_input"] == "execute"
    assert TOOL_TIERS["stop_process"] == "execute"


def test_start_process_blocks_raw_worktree_remove_before_manager_launch(monkeypatch):
    registry = _registered()
    launched = []
    monkeypatch.setattr(
        process_manager, "launch",
        lambda *args, **kwargs: launched.append((args, kwargs)),
    )
    result = json.loads(registry.fns["start_process"](
        "git worktree remove /tmp/definitely-not-run"
    ))
    assert result["error"] == "command_blocked"
    assert launched == []


def test_read_process_schema_contains_only_model_arguments():
    props = _registered().registrations["read_process"]["parameters"]["properties"]
    assert set(props) == {
        "process_id", "stdout_cursor", "stderr_cursor", "max_chars_per_stream"
    }
    assert "project_state" not in props
    assert "channel_id" not in props


def test_read_process_unknown_process(monkeypatch):
    _install_snapshots(monkeypatch, [])
    result = json.loads(_registered().fns["read_process"]("p-ffffffffffff"))
    assert result["error"] == "process_not_found"


def test_read_process_running_process_with_no_output(monkeypatch):
    read, pid = _read(monkeypatch)
    result = json.loads(read(pid))
    assert result["status"] == "running"
    for name in ("stdout", "stderr"):
        assert result[name] == {
            "requested_cursor": 0,
            "effective_cursor": 0,
            "next_cursor": 0,
            "buffer_start": 0,
            "buffer_end": 0,
            "history_evicted": False,
            "text": "",
            "output_truncated": False,
        }


@pytest.mark.parametrize(
    "stdout,stderr", [("stdout-only", ""), ("", "stderr-only"), ("out", "err")]
)
def test_read_process_returns_each_stream_independently(monkeypatch, stdout, stderr):
    read, pid = _read(monkeypatch, _snapshot(stdout=stdout, stderr=stderr))
    result = json.loads(read(pid))
    assert result["stdout"]["text"] == stdout
    assert result["stderr"]["text"] == stderr


def test_read_process_incremental_repeated_reads(monkeypatch):
    read, pid = _read(monkeypatch, _snapshot(stdout="abcdefgh"))
    first = json.loads(read(pid, max_chars_per_stream=3))
    assert first["stdout"]["text"] == "abc"
    assert first["stdout"]["next_cursor"] == 3
    assert first["stdout"]["output_truncated"] is True

    second = json.loads(read(
        pid, stdout_cursor=first["stdout"]["next_cursor"],
        max_chars_per_stream=3,
    ))
    assert second["stdout"]["text"] == "def"
    assert second["stdout"]["next_cursor"] == 6


def test_read_process_cursor_exactly_at_end_is_valid_empty(monkeypatch):
    read, pid = _read(monkeypatch, _snapshot(stdout="abc"))
    stream = json.loads(read(pid, stdout_cursor=3))["stdout"]
    assert stream["text"] == ""
    assert stream["next_cursor"] == 3
    assert stream["output_truncated"] is False


@pytest.mark.parametrize("cursor", [-1, True, 1.5])
def test_read_process_rejects_invalid_cursor(monkeypatch, cursor):
    read, pid = _read(monkeypatch)
    result = json.loads(read(pid, stdout_cursor=cursor))
    assert result["error"] == "invalid_stdout_cursor"


def test_read_process_rejects_future_cursor(monkeypatch):
    read, pid = _read(monkeypatch, _snapshot(stdout="abc"))
    result = json.loads(read(pid, stdout_cursor=4))
    assert result["error"] == "future_stdout_cursor"


def test_read_process_evicted_history_clamps_and_reports(monkeypatch):
    read, pid = _read(monkeypatch, _snapshot(stdout="fghij", stdout_start=5))
    stream = json.loads(read(pid, stdout_cursor=2))["stdout"]
    assert stream["requested_cursor"] == 2
    assert stream["effective_cursor"] == 5
    assert stream["next_cursor"] == 10
    assert stream["buffer_start"] == 5
    assert stream["buffer_end"] == 10
    assert stream["history_evicted"] is True
    assert stream["text"] == "fghij"


def test_read_process_retained_terminal_job_has_true_status_and_returncode(monkeypatch):
    snap = _snapshot(stdout="done", status="exited", returncode=7)
    read, pid = _read(monkeypatch, snap)
    result = json.loads(read(pid))
    assert result["status"] == "exited"
    assert result["returncode"] == 7
    assert result["stdout"]["text"] == "done"


def test_read_and_list_round_trip_against_real_manager(tmp_path):
    working_dir = tmp_path / "round trip working directory"
    working_dir.mkdir()
    process_id = process_manager.launch(
        python_command(
            "import sys; "
            "sys.stdout.write('real-stdout'); sys.stdout.flush(); "
            "sys.stderr.write('real-stderr'); sys.stderr.flush()"
        ),
        cwd=str(working_dir),
    )
    deadline = time.time() + 5.0
    while time.time() < deadline:
        snapshot = process_manager.get_job_snapshot(process_id)
        if snapshot["status"] != "running":
            break
        time.sleep(0.02)

    registry = _registered()
    read_result = json.loads(registry.fns["read_process"](process_id))
    list_result = json.loads(registry.fns["list_processes"]())
    assert read_result["status"] == "exited"
    assert read_result["returncode"] == 0
    assert read_result["stdout"]["text"] == "real-stdout"
    assert read_result["stderr"]["text"] == "real-stderr"
    assert [item["process_id"] for item in list_result["processes"]] == [process_id]


@pytest.mark.parametrize("value", [0, True, MAX_CHARS_PER_STREAM + 1])
def test_read_process_validates_per_stream_cap(monkeypatch, value):
    read, pid = _read(monkeypatch)
    result = json.loads(read(pid, max_chars_per_stream=value))
    assert result["error"] == "invalid_max_chars_per_stream"


def test_read_process_requested_cap_limits_each_stream(monkeypatch):
    read, pid = _read(monkeypatch, _snapshot(stdout="abcdef", stderr="uvwxyz"))
    result = json.loads(read(pid, max_chars_per_stream=2))
    assert result["stdout"]["text"] == "ab"
    assert result["stderr"]["text"] == "uv"
    assert result["stdout"]["next_cursor"] == 2
    assert result["stderr"]["next_cursor"] == 2
    assert result["stdout"]["output_truncated"] is True
    assert result["stderr"]["output_truncated"] is True


def test_read_process_self_bounds_and_cursor_tracks_actual_returned_text(monkeypatch):
    read, pid = _read(monkeypatch, _snapshot(stdout="A" * 500, stderr="B" * 500))
    monkeypatch.setattr(config, "TOOL_RESULT_MAX_CHARS", 650)
    raw = read(pid, max_chars_per_stream=500)
    result = json.loads(raw)
    assert len(raw) <= 650
    assert result["stdout"]["text"]
    assert result["stderr"]["text"]
    for name in ("stdout", "stderr"):
        stream = result[name]
        assert stream["next_cursor"] == stream["effective_cursor"] + len(stream["text"])
        assert stream["next_cursor"] < stream["buffer_end"]
        assert stream["output_truncated"] is True


@pytest.mark.parametrize("limit,expected", [(25, {"output_truncated": True}), (2, {}), (1, None), (0, None)])
def test_read_process_tiny_live_budget_never_overshoots(monkeypatch, limit, expected):
    read, pid = _read(monkeypatch, _snapshot(stdout="output"))
    monkeypatch.setattr(config, "TOOL_RESULT_MAX_CHARS", limit)
    raw = read(pid)
    assert len(raw) <= limit
    if expected is None:
        assert raw == ""
    else:
        assert json.loads(raw) == expected


def test_read_process_reads_budget_live_after_module_import(monkeypatch):
    read, pid = _read(monkeypatch, _snapshot(stdout="x" * 200))
    monkeypatch.setattr(config, "TOOL_RESULT_MAX_CHARS", 1000)
    large = read(pid)
    monkeypatch.setattr(config, "TOOL_RESULT_MAX_CHARS", 2)
    tiny = read(pid)
    assert len(large) > len(tiny)
    assert tiny == "{}"


def test_read_process_never_leaks_internal_metadata(monkeypatch):
    read, pid = _read(monkeypatch)
    raw = read(pid)
    result = json.loads(raw)

    leaked_keys = _FORBIDDEN_METADATA_KEYS & set(_iter_json_keys(result))
    assert not leaked_keys, f"forbidden internal keys leaked: {sorted(leaked_keys)}"

    string_values = list(_iter_json_string_values(result))
    assert not any("secret-channel" in v for v in string_values), (
        "the bound channel_id value leaked into a visible field"
    )


def test_list_processes_empty_is_stable(monkeypatch):
    _install_snapshots(monkeypatch, [])
    result = json.loads(_registered().fns["list_processes"]())
    assert result == {"processes": [], "total": 0, "output_truncated": False}


def test_list_processes_mixed_jobs_preserves_insertion_order(monkeypatch):
    snapshots = [
        _snapshot(process_id="p-000000000001"),
        _snapshot(process_id="p-000000000002", status="exited", returncode=0),
        _snapshot(process_id="p-000000000003"),
    ]
    _install_snapshots(monkeypatch, snapshots)
    result = json.loads(_registered().fns["list_processes"]())
    assert [p["process_id"] for p in result["processes"]] == [
        snap["process_id"] for snap in snapshots
    ]
    assert [p["status"] for p in result["processes"]] == [
        "running", "exited", "running"
    ]


def test_list_processes_is_compact_and_leaks_no_stream_or_control_data(monkeypatch):
    snap = _snapshot(stdout="TOP_SECRET_OUTPUT", stderr="SECRET_ERROR")
    _install_snapshots(monkeypatch, [snap])
    raw = _registered().fns["list_processes"]()
    result = json.loads(raw)
    assert result["total"] == 1
    assert len(raw) < 500

    forbidden_keys = _FORBIDDEN_METADATA_KEYS | {"stdout", "stderr"}
    leaked_keys = forbidden_keys & set(_iter_json_keys(result))
    assert not leaked_keys, f"forbidden internal keys leaked: {sorted(leaked_keys)}"

    string_values = list(_iter_json_string_values(result))
    for forbidden in ("TOP_SECRET_OUTPUT", "SECRET_ERROR", "secret-channel"):
        assert not any(forbidden in v for v in string_values), (
            f"{forbidden!r} leaked into a visible field"
        )


def test_list_processes_uses_manager_ttl_authority(monkeypatch):
    retained = _snapshot(process_id="p-000000000001", status="exited", returncode=0)
    expired = _snapshot(process_id="p-000000000002", status="exited", returncode=0)
    snapshots = {retained["process_id"]: retained, expired["process_id"]: expired}
    monkeypatch.setattr(process_manager, "list_job_ids", lambda: [retained["process_id"]])
    monkeypatch.setattr(process_manager, "get_job_snapshot", snapshots.get)
    result = json.loads(_registered().fns["list_processes"]())
    assert [p["process_id"] for p in result["processes"]] == [retained["process_id"]]


def test_list_processes_self_bounds_to_live_budget(monkeypatch):
    snapshots = [_snapshot(process_id=f"p-{i:012x}") for i in range(6)]
    _install_snapshots(monkeypatch, snapshots)
    monkeypatch.setattr(config, "TOOL_RESULT_MAX_CHARS", 400)
    raw = _registered().fns["list_processes"]()
    result = json.loads(raw)
    assert len(raw) <= 400
    assert result["output_truncated"] is True
    assert result["total"] == 6
    assert len(result["processes"]) < 6


def test_adapter_source_uses_only_public_manager_apis():
    import inspect
    from tools import processes

    source = inspect.getsource(processes)
    assert "process_manager._registry" not in source
    assert "process_manager._Job" not in source
    assert "process_manager.get_job_snapshot" in source
    assert "process_manager.list_job_ids" in source
