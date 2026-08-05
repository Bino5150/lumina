import time
from tools import tasks


def test_run_background_subagent_returns_task_id(monkeypatch):
    monkeypatch.setattr(tasks, "spawn_subagent",
                         lambda *a, **k: {"success": True, "result": "ok",
                                           "tool_calls_made": 0, "error": None})
    result = tasks.run_background_subagent("do a thing")
    assert "task_id" in result

    for _ in range(50):
        status = tasks.check_background_task(result["task_id"])
        if status["status"] != "running":
            break
        time.sleep(0.05)
    assert status["status"] == "success"
    assert status["result"]["result"] == "ok"


def test_check_background_task_not_found():
    assert tasks.check_background_task("nope")["status"] == "not_found"


def test_schedule_background_subagent_fires_later(monkeypatch):
    monkeypatch.setattr(tasks, "spawn_subagent",
                         lambda *a, **k: {"success": True, "result": "scheduled-ok",
                                           "tool_calls_made": 0, "error": None})
    run_at = time.time() + 0.2
    result = tasks.schedule_background_subagent("do it later", run_at)
    status = tasks.check_background_task(result["task_id"])
    assert status["status"] == "scheduled"
    time.sleep(0.5)
    status = tasks.check_background_task(result["task_id"])
    assert status["status"] == "success"


def test_register_task_tools_wrappers_record_task_id_on_agent(monkeypatch):
    """The registered tool wrappers (not the module-level functions tested
    above) must additionally record each dispatched task_id onto the owning
    agent's _background_task_ids -- that's what LuminaAgent.chat() polls to
    surface a completion notice next turn."""
    monkeypatch.setattr(tasks, "spawn_subagent",
                         lambda *a, **k: {"success": True, "result": "ok",
                                           "tool_calls_made": 0, "error": None})

    class _FakeAgent:
        _background_task_ids = set()

    class _FakeRegistry:
        def __init__(self):
            self.tools = {}
        def register(self, name, fn, **kw):
            self.tools[name] = fn

    agent = _FakeAgent()
    registry = _FakeRegistry()
    tasks.register_task_tools(registry, agent)

    result = registry.tools["run_background_subagent"]("do a thing")
    assert result["task_id"] in agent._background_task_ids

    run_at = time.time() + 0.2
    result2 = registry.tools["schedule_background_subagent"]("do it later", run_at)
    assert result2["task_id"] in agent._background_task_ids
