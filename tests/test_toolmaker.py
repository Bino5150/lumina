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
    monkeypatch.setattr(toolmaker, "TOOLS_DIR", str(tmp_path))
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
