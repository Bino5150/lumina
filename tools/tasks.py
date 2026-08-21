"""
tools/tasks.py -- tool interface over core/task_queue.py. run_background_subagent
is the /btw pattern: dispatch a task to run in the background, return
immediately with a task_id, let the caller check on it later (or get
surfaced an ephemeral notice next turn -- see agent.py wiring in Part F).
"""
from typing import Optional

import config
from core.project_context import ProjectContext
from core.task_queue import submit_task, schedule_task, get_task_result
from tools.subagent import spawn_subagent, resolve_dispatch_project_context


def run_background_subagent(task: str, persona: str = None,
                             backend: str = None, tools_enabled: list = None,
                             project: str = None,
                             _project_context: Optional[ProjectContext] = None) -> dict:
    """Dispatches a subagent to run in the background. Returns
    {"task_id": str} immediately -- does not block. Same owner=False,
    explicit-tools-only contract as spawn_subagent(), inherited for free
    since this calls it directly rather than building a parallel path.

    project / _project_context (CODING-02B-B): resolved to an immutable
    ProjectContext HERE, synchronously, before submit_task() is ever called
    -- this function's own call is the true dispatch instant for a
    background task, well before the executor actually invokes
    spawn_subagent(). The queued job therefore only ever carries an
    already-resolved ProjectContext (or None), never a name to re-look-up
    later. project omitted (both default None) preserves exactly today's
    no-context behavior for every existing direct caller.

    On an invalid/unknown/unbound project override, fails closed BEFORE any
    task is enqueued: returns {"task_id": None, "error": "<reason>"}, the
    same shape a caller checks either way -- no task_id key ever silently
    means "it dispatched, check back later" when it didn't."""
    try:
        resolved_context = resolve_dispatch_project_context(project, _project_context)
    except Exception as e:
        return {"task_id": None, "error": str(e)}

    task_id = submit_task(spawn_subagent, task, persona=persona,
                           backend=backend, tools_enabled=tools_enabled,
                           _project_context=resolved_context)
    return {"task_id": task_id}


def check_background_task(task_id: str) -> dict:
    """Poll a background or scheduled task's status. Returns
    {"status", "result", "completed_at"} or {"status": "not_found"}."""
    result = get_task_result(task_id)
    if result is None:
        return {"status": "not_found"}
    return result


def schedule_background_subagent(task: str, run_at: float, persona: str = None,
                                  backend: str = None, tools_enabled: list = None,
                                  project: str = None,
                                  _project_context: Optional[ProjectContext] = None) -> dict:
    """Same as run_background_subagent but fires at run_at (unix timestamp)
    instead of immediately -- the scheduled/cron half of the same
    abstraction.

    project / _project_context resolved to an immutable ProjectContext HERE,
    at schedule time, exactly like run_background_subagent() above -- NOT
    re-resolved when run_at arrives. The scheduled heap entry
    (core/task_queue.py) stores this already-resolved value verbatim; the
    scheduler loop never re-derives it, no matter what the parent's active
    Project or binding.json looks like by the time the job actually fires."""
    try:
        resolved_context = resolve_dispatch_project_context(project, _project_context)
    except Exception as e:
        return {"task_id": None, "error": str(e)}

    task_id = schedule_task(run_at, spawn_subagent, task, persona=persona,
                             backend=backend, tools_enabled=tools_enabled,
                             _project_context=resolved_context)
    return {"task_id": task_id}


def register_task_tools(registry, agent):
    """Gated at the call site on config.BACKGROUND_TASKS_ENABLED, not here --
    mirrors every other conditional registration.

    agent: the owning LuminaAgent instance. Needed (not just registry) so the
    run_background_subagent/schedule_background_subagent tool wrappers below
    can record each dispatched task_id onto agent._background_task_ids -- the
    set LuminaAgent.chat() checks at the top of every turn to surface
    completed results as a one-turn ephemeral system-prompt injection (see
    agent.py's chat()). The module-level run_background_subagent/
    schedule_background_subagent functions above stay agent-agnostic (and
    directly unit-testable, see tests/test_tasks.py); only the registered
    tool wrappers here know about the owning agent.
    """
    def _tool_run_background_subagent(task, persona=None, backend=None, tools_enabled=None, project=None):
        snapshot = agent.project_context.snapshot()
        result = run_background_subagent(task, persona=persona, backend=backend,
                                          tools_enabled=tools_enabled, project=project,
                                          _project_context=snapshot)
        if result.get("task_id"):
            agent._background_task_ids.add(result["task_id"])
        return result

    def _tool_schedule_background_subagent(task, run_at, persona=None, backend=None, tools_enabled=None, project=None):
        snapshot = agent.project_context.snapshot()
        result = schedule_background_subagent(task, run_at, persona=persona, backend=backend,
                                               tools_enabled=tools_enabled, project=project,
                                               _project_context=snapshot)
        if result.get("task_id"):
            agent._background_task_ids.add(result["task_id"])
        return result

    registry.register(
        name="run_background_subagent",
        fn=_tool_run_background_subagent,
        description="Dispatch a subagent task to run in the background and return immediately "
                     "with a task_id, instead of blocking. Use for tasks you don't need the "
                     "result of right away -- check on it later with check_background_task.",
        parameters={
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "The self-contained task/instruction for the background subagent to carry out."},
                "persona": {"type": "string", "description": "Optional persona name to apply to the subagent."},
                "backend": {"type": "string", "description": "Optional LLM backend name override for the subagent."},
                "tools_enabled": {"type": "array", "items": {"type": "string"},
                                   "description": "Optional explicit list of tool names to grant the subagent. Defaults to none."},
                "project": {"type": "string",
                            "description": "Optional tracked Project name for the child. If omitted, inherits the "
                                            "parent's active Project at dispatch time. Granting a Project does not "
                                            "grant any tool capability by itself."},
            },
            "required": ["task"],
        },
    )

    registry.register(
        name="check_background_task",
        fn=check_background_task,
        description="Poll the status of a background or scheduled subagent task by its task_id.",
        parameters={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task_id returned by run_background_subagent or schedule_background_subagent."},
            },
            "required": ["task_id"],
        },
    )

    registry.register(
        name="schedule_background_subagent",
        fn=_tool_schedule_background_subagent,
        description="Schedule a subagent task to run at a future unix timestamp instead of "
                     "immediately. Returns a task_id right away; the task fires later on its own.",
        parameters={
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "The self-contained task/instruction for the subagent to carry out when it fires."},
                "run_at": {"type": "number", "description": "Unix timestamp (seconds) at which to run the task."},
                "persona": {"type": "string", "description": "Optional persona name to apply to the subagent."},
                "backend": {"type": "string", "description": "Optional LLM backend name override for the subagent."},
                "tools_enabled": {"type": "array", "items": {"type": "string"},
                                   "description": "Optional explicit list of tool names to grant the subagent. Defaults to none."},
                "project": {"type": "string",
                            "description": "Optional tracked Project name for the child. Resolved at schedule time, "
                                            "not when the task fires. If omitted, inherits the parent's active "
                                            "Project as of schedule time. Granting a Project does not grant any "
                                            "tool capability by itself."},
            },
            "required": ["task", "run_at"],
        },
    )
