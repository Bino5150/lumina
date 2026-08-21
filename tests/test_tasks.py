import os
import threading
import time

import pytest

from tools import tasks
from core.project_context import ProjectContext, ProjectContextState


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

    from core.project_context import ProjectContextState

    class _FakeAgent:
        _background_task_ids = set()
        project_context = ProjectContextState()

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


# ── CODING-02B-B: background/scheduled Project-context inheritance ──────

class _FakeAgent:
    def __init__(self, project_context=None):
        self._background_task_ids = set()
        self.project_context = ProjectContextState(project_context)


class _FakeRegistry:
    def __init__(self):
        self.tools = {}
    def register(self, name, fn, **kw):
        self.tools[name] = fn


@pytest.fixture
def _isolated_projects_dirs(tmp_path, monkeypatch):
    import tools.projects as tp
    import core.project_context as pc
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    monkeypatch.setattr(tp, "PROJECTS_DIR", str(projects_dir))
    monkeypatch.setattr(pc, "PROJECT_BINDINGS_DIR", str(tmp_path / "bindings"))


def _make_tracked_project(tmp_path, name):
    import tools.projects as tp
    import core.project_context as pc
    root = tmp_path / f"{name}-root"
    root.mkdir()
    os.makedirs(os.path.join(tp.PROJECTS_DIR, name))
    pc.save_project_binding(name, str(root))
    return str(root)


def _wait_for_terminal(task_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = tasks.check_background_task(task_id)
        if status["status"] not in ("running", "scheduled"):
            return status
        time.sleep(0.02)
    raise AssertionError(f"task {task_id} never reached a terminal status")


def test_immediate_background_child_receives_snapshot_not_later_parent_switch(monkeypatch):
    """Section 24: parent active A, dispatch background task, block the
    worker before it actually runs spawn_subagent, switch parent to B,
    release the worker, and confirm it still received A. Deterministic via
    threading.Event, never a timing sleep."""
    ctx_a = ProjectContext(name="a", root="/tmp/root-a")
    agent = _FakeAgent(project_context=ctx_a)
    registry = _FakeRegistry()
    tasks.register_task_tools(registry, agent)

    entered = threading.Event()
    release = threading.Event()
    captured = {}

    def _blocking_spawn(*a, **k):
        entered.set()
        release.wait(timeout=5)
        captured.update(k)
        return {"success": True, "result": "ok", "tool_calls_made": 0, "error": None}

    monkeypatch.setattr(tasks, "spawn_subagent", _blocking_spawn)

    result = registry.tools["run_background_subagent"]("do a thing")
    assert entered.wait(timeout=5)

    # Parent switches to B WHILE the worker is blocked mid-flight.
    agent.project_context.set(ProjectContext(name="b", root="/tmp/root-b"))

    release.set()
    status = _wait_for_terminal(result["task_id"])
    assert status["status"] == "success"
    assert captured["_project_context"] == ctx_a


def test_scheduled_child_uses_context_captured_at_schedule_time_not_binding_at_fire_time(
        monkeypatch, tmp_path, _isolated_projects_dirs):
    """Section 25 -- the most important regression in this slice: an
    explicit project= override is resolved to an immutable ProjectContext at
    schedule_background_subagent() call time. Rewriting the tracked
    project's binding.json afterward, before run_at fires, must NOT change
    what the child receives. Proves no execution-time binding.json reload."""
    import tools.projects as tp
    import core.project_context as pc

    root_a1 = _make_tracked_project(tmp_path, "proj")

    fired = threading.Event()
    captured = {}

    def _capturing_spawn(*a, **k):
        captured.update(k)
        fired.set()
        return {"success": True, "result": "ok", "tool_calls_made": 0, "error": None}

    monkeypatch.setattr(tasks, "spawn_subagent", _capturing_spawn)

    run_at = time.time() + 0.05
    result = tasks.schedule_background_subagent("do it later", run_at, project="proj")
    assert "error" not in result
    assert result["task_id"] is not None

    # Rewrite the binding to a DIFFERENT root before the scheduled job fires.
    root_a2 = tmp_path / "proj-root-2"
    root_a2.mkdir()
    pc.save_project_binding("proj", str(root_a2))
    assert pc.load_project_binding("proj").root == str(root_a2)

    assert fired.wait(timeout=5)
    status = _wait_for_terminal(result["task_id"])
    assert status["status"] == "success"

    assert captured["_project_context"] == ProjectContext(name="proj", root=root_a1)
    assert captured["_project_context"].root != str(root_a2)


def test_scheduled_child_immune_to_parent_switching_projects_after_schedule(monkeypatch):
    """Same T1/T4 shape as above, but with inherited (non-override) context:
    parent active A at schedule time, parent switches to B before run_at,
    scheduled child still receives A."""
    ctx_a = ProjectContext(name="a", root="/tmp/root-a")
    agent = _FakeAgent(project_context=ctx_a)
    registry = _FakeRegistry()
    tasks.register_task_tools(registry, agent)

    fired = threading.Event()
    captured = {}

    def _capturing_spawn(*a, **k):
        captured.update(k)
        fired.set()
        return {"success": True, "result": "ok", "tool_calls_made": 0, "error": None}

    monkeypatch.setattr(tasks, "spawn_subagent", _capturing_spawn)

    run_at = time.time() + 0.05
    result = registry.tools["schedule_background_subagent"]("do it later", run_at)

    agent.project_context.set(ProjectContext(name="b", root="/tmp/root-b"))
    agent.project_context.clear()  # even clearing it afterward must not matter

    assert fired.wait(timeout=5)
    _wait_for_terminal(result["task_id"])
    assert captured["_project_context"] == ctx_a


def test_sibling_background_children_retain_independent_snapshots(monkeypatch):
    """Section 26: two children dispatched under different snapshots, later
    parent mutation affects neither."""
    ctx_a = ProjectContext(name="a", root="/tmp/root-a")
    agent = _FakeAgent(project_context=ctx_a)
    registry = _FakeRegistry()
    tasks.register_task_tools(registry, agent)

    captured = []

    def _capturing_spawn(*a, **k):
        captured.append(k.get("_project_context"))
        return {"success": True, "result": "ok", "tool_calls_made": 0, "error": None}

    monkeypatch.setattr(tasks, "spawn_subagent", _capturing_spawn)

    result_1 = registry.tools["run_background_subagent"]("task one")
    agent.project_context.set(ProjectContext(name="b", root="/tmp/root-b"))
    result_2 = registry.tools["run_background_subagent"]("task two")
    agent.project_context.set(ProjectContext(name="c", root="/tmp/root-c"))

    _wait_for_terminal(result_1["task_id"])
    _wait_for_terminal(result_2["task_id"])

    assert ProjectContext(name="a", root="/tmp/root-a") in captured
    assert ProjectContext(name="b", root="/tmp/root-b") in captured
    assert ProjectContext(name="c", root="/tmp/root-c") not in captured


# ── CODING-02B-B Part E: background error contract ───────────────────────

def test_immediate_background_invalid_override_fails_before_enqueue(monkeypatch, _isolated_projects_dirs):
    submitted = []
    monkeypatch.setattr(tasks, "submit_task", lambda *a, **k: submitted.append((a, k)) or "should-not-happen")

    result = tasks.run_background_subagent("do a thing", project="never-heard-of-it")
    assert result["task_id"] is None
    assert "error" in result and result["error"]
    assert submitted == []


def test_scheduled_invalid_override_fails_before_enqueue(monkeypatch, _isolated_projects_dirs):
    scheduled = []
    monkeypatch.setattr(tasks, "schedule_task", lambda *a, **k: scheduled.append((a, k)) or "should-not-happen")

    result = tasks.schedule_background_subagent("do it later", time.time() + 5, project="never-heard-of-it")
    assert result["task_id"] is None
    assert "error" in result and result["error"]
    assert scheduled == []


def test_registered_wrapper_does_not_record_none_task_id_on_override_failure(_isolated_projects_dirs):
    agent = _FakeAgent()
    registry = _FakeRegistry()
    tasks.register_task_tools(registry, agent)

    result = registry.tools["run_background_subagent"]("do a thing", project="never-heard-of-it")
    assert result["task_id"] is None
    assert None not in agent._background_task_ids
    assert agent._background_task_ids == set()

    result2 = registry.tools["schedule_background_subagent"]("do it later", time.time() + 5,
                                                               project="never-heard-of-it")
    assert result2["task_id"] is None
    assert agent._background_task_ids == set()


def test_success_shape_has_no_error_key(monkeypatch):
    """Approved shape: success stays exactly {"task_id": "<id>"} -- no
    error: null clutter added to the successful case."""
    monkeypatch.setattr(tasks, "spawn_subagent",
                         lambda *a, **k: {"success": True, "result": "ok",
                                           "tool_calls_made": 0, "error": None})
    result = tasks.run_background_subagent("do a thing")
    assert set(result.keys()) == {"task_id"}
    assert isinstance(result["task_id"], str)
