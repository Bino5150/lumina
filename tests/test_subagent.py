import config
from tools.subagent import spawn_subagent, register_subagent_tools


def test_depth_limit_refuses_at_max_depth(monkeypatch):
    monkeypatch.setattr(config, "MAX_SUBAGENT_DEPTH", 2)
    result = spawn_subagent("do something", _parent_depth=2)
    assert result["success"] is False
    assert "depth" in result["error"].lower()


def test_register_subagent_tools_skips_registration_at_max_depth(monkeypatch):
    monkeypatch.setattr(config, "MAX_SUBAGENT_DEPTH", 2)
    calls = []
    fake_registry = type("FakeRegistry", (), {"register": lambda self, **kw: calls.append(kw)})()
    register_subagent_tools(fake_registry, parent_depth=2)
    assert calls == []  # never registered -- maxed-out subagent sees no tool at all


def test_register_subagent_tools_registers_below_max_depth(monkeypatch):
    monkeypatch.setattr(config, "MAX_SUBAGENT_DEPTH", 2)
    calls = []
    fake_registry = type("FakeRegistry", (), {"register": lambda self, **kw: calls.append(kw)})()
    register_subagent_tools(fake_registry, parent_depth=0)
    assert len(calls) == 1
    assert calls[0]["name"] == "spawn_subagent"


def test_spawn_subagent_never_raises_on_internal_error(monkeypatch):
    # Force a construction failure and confirm it comes back as a normal
    # error dict, not an exception escaping to the caller.
    import tools.subagent as subagent_mod

    def _boom(*a, **k):
        raise RuntimeError("simulated construction failure")
    monkeypatch.setattr(subagent_mod, "LuminaAgent", _boom)

    result = spawn_subagent("task", _parent_depth=0)
    assert result["success"] is False
    assert "simulated construction failure" in result["error"]


# Real-construction integration test -- only meaningful if a live backend is
# reachable. Skip cleanly if not, don't fail CI over an infra dependency.
def test_spawn_subagent_constructs_with_owner_false(monkeypatch):
    """Confirms the owner=False contract without needing a live LLM call --
    patches LuminaAgent to a stub that just records what it was constructed
    with, verifies owner=False and depth=_parent_depth+1 regardless of what
    the caller passed."""
    import tools.subagent as subagent_mod
    captured = {}

    class _StubAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.registry = type("R", (), {})()
            self.on_tool_call = None
        def apply_persona(self, p): pass
        def chat(self, task, source=None):
            return "stub response"

    monkeypatch.setattr(subagent_mod, "LuminaAgent", _StubAgent)
    monkeypatch.setattr(subagent_mod, "apply_tool_profile", lambda *a, **k: None)

    result = spawn_subagent("task", _parent_depth=1)
    assert captured["owner"] is False
    assert captured["depth"] == 2
    assert result["success"] is True
    assert result["result"] == "stub response"
