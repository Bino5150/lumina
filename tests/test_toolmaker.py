"""F-34: tools/toolmaker.py — staged review gate. create_tool must never
execute untrusted code; only approve_pending_tool (never agent-callable)
does."""
import os
import json
import pytest
import tools.toolmaker as toolmaker


class _CapturingRegistry:
    def __init__(self):
        self.fns = {}
        self._tools = {}

    def register(self, name, fn, description, parameters):
        self.fns[name] = fn
        self._tools[name] = fn


class _FakeAgent:
    def __init__(self, registry):
        self.registry = registry


VALID_TOOL_CODE = """
def hello_world():
    return "hi from custom tool"

def register_hello_world_tool(registry):
    registry.register(name="hello_world", fn=hello_world, description="says hi",
                       parameters={"type": "object", "properties": {}, "required": []})
"""


@pytest.fixture
def toolmaker_env(tmp_path, monkeypatch):
    monkeypatch.setattr(toolmaker, "CUSTOM_TOOLS_DIR", str(tmp_path))
    monkeypatch.setattr(toolmaker, "PENDING_DIR", str(tmp_path / "_pending"))
    monkeypatch.setattr(toolmaker, "AUDIT_LOG_PATH", str(tmp_path / "tool_audit.log"))
    reg = _CapturingRegistry()
    agent = _FakeAgent(reg)
    toolmaker.register_toolmaker_tools(reg, agent)
    return reg


def test_create_tool_does_not_go_live(toolmaker_env):
    reg = toolmaker_env
    result = reg.fns["create_tool"]("hello_world", "says hi", VALID_TOOL_CODE)
    assert "staged for review" in result.lower() or "NOT loaded" in result
    assert "hello_world" not in reg._tools  # the live registry, unaffected


def test_create_tool_appears_in_pending_list(toolmaker_env):
    reg = toolmaker_env
    reg.fns["create_tool"]("hello_world", "says hi", VALID_TOOL_CODE)
    assert "hello_world" in reg.fns["list_pending_tools"]()


def test_show_pending_tool_source_returns_code(toolmaker_env):
    reg = toolmaker_env
    reg.fns["create_tool"]("hello_world", "says hi", VALID_TOOL_CODE)
    source = reg.fns["show_pending_tool_source"]("hello_world")
    assert "def hello_world" in source


def test_syntax_error_rejected_before_staging(toolmaker_env):
    reg = toolmaker_env
    result = reg.fns["create_tool"]("broken", "broken", "def foo(:\n  pass")
    assert "syntax error" in result.lower()
    assert "broken" not in reg.fns["list_pending_tools"]()


def test_protected_name_rejected(toolmaker_env):
    reg = toolmaker_env
    result = reg.fns["create_tool"]("sandbox", "overwrite attempt", VALID_TOOL_CODE)
    assert "protected" in result.lower()


def test_reject_pending_tool_discards_without_loading(toolmaker_env):
    reg = toolmaker_env
    reg.fns["create_tool"]("hello_world", "says hi", VALID_TOOL_CODE)
    result = reg.fns["reject_pending_tool"]("hello_world")
    assert "discarded" in result.lower()
    assert "hello_world" not in reg.fns["list_pending_tools"]()
    assert "hello_world" not in reg._tools


def test_approve_pending_tool_is_not_registered_as_agent_tool(toolmaker_env):
    """The whole point of the gate: nothing reachable from inside a chat
    turn can approve a tool. approve_pending_tool must not be in the
    registry's callable functions."""
    reg = toolmaker_env
    assert "approve_pending_tool" not in reg.fns


def test_approve_pending_tool_moves_and_hotloads(toolmaker_env, tmp_path):
    reg = toolmaker_env
    reg.fns["create_tool"]("hello_world", "says hi", VALID_TOOL_CODE)

    result = toolmaker.approve_pending_tool("hello_world", reg)

    assert "approved" in result.lower() or "live" in result.lower()
    assert "hello_world" in reg._tools
    assert reg._tools["hello_world"]() == "hi from custom tool"
    assert not os.path.exists(os.path.join(str(tmp_path / "_pending"), "hello_world.py"))
    assert os.path.exists(os.path.join(str(tmp_path), "hello_world.py"))


def test_approve_nonexistent_pending_tool_fails_cleanly(toolmaker_env):
    reg = toolmaker_env
    result = toolmaker.approve_pending_tool("does_not_exist", reg)
    assert "error" in result.lower() or "no pending" in result.lower()


def test_audit_log_records_full_lifecycle(toolmaker_env):
    reg = toolmaker_env
    reg.fns["create_tool"]("hello_world", "says hi", VALID_TOOL_CODE)
    toolmaker.approve_pending_tool("hello_world", reg)

    events = []
    with open(toolmaker.AUDIT_LOG_PATH) as f:
        for line in f:
            events.append(json.loads(line))

    event_types = [e["event"] for e in events]
    assert "staged" in event_types
    assert "approved" in event_types
    # full source should be recoverable from the audit log even after approval
    staged_entry = next(e for e in events if e["event"] == "staged")
    assert "def hello_world" in staged_entry["code"]


# ── FE-11: startup loader for tools approved in a PAST session ─────────────
# approve_pending_tool() only ever hot-loads into that call's live registry.
# Nothing about it persisted across a restart, so every approved custom tool
# silently vanished the next time the app launched. These tests simulate a
# restart with a fresh registry rather than reusing the one create/approve
# already touched.

def _write_tool_file(tools_dir: str, name: str, code: str):
    with open(os.path.join(tools_dir, f"{name}.py"), "w") as f:
        f.write(code)


def _append_fake_audit(audit_path: str, event: str, name: str):
    with open(audit_path, "a") as f:
        f.write(json.dumps({"ts": "2026-01-01T00:00:00", "event": event, "name": name}) + "\n")


BROKEN_ARITY_CODE = """
class _Engine:
    pass

engine = _Engine()

def register_broken_arity_tool(registry):
    registry.register("broken_arity", engine)  # missing description/parameters
"""

MISSING_REGISTER_FN_CODE = "value = 42\n"


def test_startup_loader_reloads_approved_tool_after_restart(toolmaker_env):
    reg = toolmaker_env
    reg.fns["create_tool"]("hello_world", "says hi", VALID_TOOL_CODE)
    toolmaker.approve_pending_tool("hello_world", reg)

    # Simulate a restart: brand new registry, nothing statically wired yet.
    fresh_reg = _CapturingRegistry()
    loaded = toolmaker.load_approved_custom_tools(fresh_reg)

    assert "hello_world" in loaded
    assert "hello_world" in fresh_reg._tools
    assert fresh_reg._tools["hello_world"]() == "hi from custom tool"


def test_startup_loader_skips_deleted_tool(toolmaker_env):
    reg = toolmaker_env
    reg.fns["create_tool"]("hello_world", "says hi", VALID_TOOL_CODE)
    toolmaker.approve_pending_tool("hello_world", reg)
    reg.fns["delete_tool"]("hello_world")

    fresh_reg = _CapturingRegistry()
    loaded = toolmaker.load_approved_custom_tools(fresh_reg)

    assert "hello_world" not in loaded
    assert "hello_world" not in fresh_reg._tools


def test_startup_loader_survives_broken_tool_and_still_loads_good_one(toolmaker_env, tmp_path):
    """Mirrors the real temporal_decay_engine.py bug found while building
    this: a custom tool whose register function calls registry.register()
    with the wrong arity. It must not block boot, and it must not take a
    sibling tool down with it."""
    reg = toolmaker_env
    reg.fns["create_tool"]("hello_world", "says hi", VALID_TOOL_CODE)
    toolmaker.approve_pending_tool("hello_world", reg)

    _write_tool_file(str(tmp_path), "broken_arity", BROKEN_ARITY_CODE)
    _append_fake_audit(toolmaker.AUDIT_LOG_PATH, "approved", "broken_arity")

    fresh_reg = _CapturingRegistry()
    loaded = toolmaker.load_approved_custom_tools(fresh_reg)

    assert "hello_world" in loaded
    assert "broken_arity" not in loaded
    assert "broken_arity" not in fresh_reg._tools


def test_startup_loader_skips_tool_with_no_register_function(toolmaker_env, tmp_path):
    _write_tool_file(str(tmp_path), "no_register_fn", MISSING_REGISTER_FN_CODE)
    _append_fake_audit(toolmaker.AUDIT_LOG_PATH, "approved", "no_register_fn")

    fresh_reg = _CapturingRegistry()
    loaded = toolmaker.load_approved_custom_tools(fresh_reg)

    assert loaded == []
    assert "no_register_fn" not in fresh_reg._tools


def test_startup_loader_ignores_files_never_approved(toolmaker_env, tmp_path):
    """A bare .py file sitting in tools/ with no matching 'approved' audit
    log entry must never be picked up — same fail-closed posture as the
    delete_tool gate this loader reuses."""
    _write_tool_file(str(tmp_path), "hello_world", VALID_TOOL_CODE)  # file exists, never approved

    fresh_reg = _CapturingRegistry()
    loaded = toolmaker.load_approved_custom_tools(fresh_reg)

    assert loaded == []
    assert "hello_world" not in fresh_reg._tools
