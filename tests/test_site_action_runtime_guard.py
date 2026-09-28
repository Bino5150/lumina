"""N25: trusted routing identity, concurrent tasks and registry composition.

Fake functions are tripwires. No browser, API, terminal or child is executed.
The actual Agent ingress seam is covered in test_site_action_ingress.py.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import Context, copy_context
from dataclasses import replace
import multiprocessing
import threading

import pytest

from chrome_companion.site_actions import runtime_guard as g
from chrome_companion.site_actions.model import ContractError
from tools.registry import ToolRegistry


class ServiceAgent:
    def __init__(self, broker, data_dir, owner=True, channel="local"):
        self.owner, self.channel_id = owner, channel
        self.registry = ToolRegistry()
        self.route = broker.bind_agent(self, self.registry, data_dir)
        self.calls = []

    def tool(self, name):
        def tripwire(**args):
            self.calls.append((name, args))
            return "ordinary result"
        self.registry.register(name, tripwire, "tripwire", {})


@pytest.fixture
def broker(monkeypatch):
    broker = g.RoutingBroker()
    monkeypatch.setattr(g, "_broker", broker)
    return broker


def scope(broker, agent, protected=True, event="event"):
    return broker.turn(agent, raw_text="/companion-action malformed" if protected else "ordinary task",
                       source="OWNER_DIRECT", chat=1, event=event)


@pytest.mark.parametrize("route", [
    "browser_navigate", "browser_click", "browser_type", "terminal_exec", "run_command",
    "spawn_subagent", "dispatch_to_worktree", "run_python", "create_tool", "custom_tool",
    "http_request", "reddit_api", "github_create_issue", "write_file", "get_time",
    "chrome_status", "chrome_list_tabs", "chrome_get_active_tab", "chrome_get_url_title",
    "chrome_extract_visible_text", "chrome_get_links", "chrome_open_owner_url", "chrome_switch_tab",
    "unknown_future_mutator",
])
def test_N25_protected_registry_never_reaches_an_alternate_or_unknown_route(broker, tmp_path, route):
    agent = ServiceAgent(broker, tmp_path)
    agent.tool(route)
    with scope(broker, agent):
        assert "blocked: Companion site task" in agent.registry.call(route, {"owner": True, "runtime": "chrome_companion"})
    assert agent.calls == []


def test_N25_concurrent_A_does_not_block_trusted_B_or_lend_B_authority(broker, tmp_path):
    a, b = ServiceAgent(broker, tmp_path), ServiceAgent(broker, tmp_path, channel="other")
    a.tool("browser_click"); b.tool("browser_click")
    entered, finished = threading.Barrier(2), threading.Barrier(2)
    results = []

    def protected():
        with scope(broker, a):
            entered.wait(timeout=10)
            results.append(a.registry.call("browser_click", {}))
            results.append(b.registry.call("browser_click", {}))
            finished.wait(timeout=10)

    thread = threading.Thread(target=protected)
    thread.start()
    entered.wait(timeout=10)
    with scope(broker, b, False) as task:
        assert not task.protected and task.intake is None and task.admission is None
        assert b.registry.call("browser_click", {}) == "ordinary result"
        assert "cannot borrow" in a.registry.call("browser_click", {})
    finished.wait(timeout=10)
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert len(b.calls) == 1 and a.calls == []
    assert all("blocked" in result for result in results)


def test_N25_two_chats_of_same_agent_have_distinct_trusted_tasks(broker, tmp_path):
    agent = ServiceAgent(broker, tmp_path)
    agent.tool("browser_click")
    entered, finished = threading.Barrier(2), threading.Barrier(2)
    tasks = []
    def protected():
        with scope(broker, agent) as task:
            tasks.append(task)
            entered.wait(timeout=10)
            assert "blocked" in agent.registry.call("browser_click", {})
            finished.wait(timeout=10)
    thread = threading.Thread(target=protected); thread.start(); entered.wait(timeout=10)
    with broker.turn(agent, raw_text="other chat", source="OWNER_DIRECT", chat=2, event="B") as task_b:
        assert task_b.handle is not tasks[0].handle and task_b.chat != tasks[0].chat
        assert not task_b.protected and task_b.intake is None
        assert agent.registry.call("browser_click", {}) == "ordinary result"
    finished.wait(timeout=10); thread.join(timeout=10)
    assert not thread.is_alive() and len(agent.calls) == 1


def test_ordinary_unscoped_worker_registry_calls_remain_unchanged_outside_site_work(broker, tmp_path):
    agent = ServiceAgent(broker, tmp_path)
    agent.tool("browser_click")
    with ThreadPoolExecutor(max_workers=1) as executor:
        assert executor.submit(agent.registry.call, "browser_click", {}).result(timeout=10) == "ordinary result"
    assert len(agent.calls) == 1


@pytest.mark.parametrize("attack", ["clear", "copy_seal", "model_id", "other_agent", "other_session", "unknown_registry"])
def test_N25_ambient_values_do_not_override_service_owned_execution(broker, tmp_path, attack):
    a, b = ServiceAgent(broker, tmp_path), ServiceAgent(broker, tmp_path)
    a.tool("terminal_exec"); b.tool("terminal_exec")
    unknown = ToolRegistry(); unknown.register("terminal_exec", lambda: pytest.fail("unguarded"), "", {})
    with scope(broker, a) as task:
        value = None if attack == "clear" else replace(task.handle) if attack == "copy_seal" else "model-task-id"
        token = broker._context.set(value)
        try:
            registry = b.registry if attack in {"other_agent", "other_session"} else unknown if attack == "unknown_registry" else a.registry
            assert "blocked" in registry.call("terminal_exec", {})
        finally:
            broker._context.reset(token)
        assert "blocked" in Context().run(a.registry.call, "terminal_exec", {})
    assert not broker._frames and not broker._tasks and broker._context.get() is None
    assert not a.calls and not b.calls


@pytest.mark.parametrize("mode", ["thread_raw", "executor_raw", "thread_handoff", "executor_handoff", "copied_context"])
def test_N25_thread_executor_and_context_copy_cannot_shed_guard(broker, tmp_path, mode):
    a, b = ServiceAgent(broker, tmp_path), ServiceAgent(broker, tmp_path)
    a.tool("terminal_exec"); b.tool("terminal_exec")
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(lambda: None).result(timeout=10)  # preexisting executor worker
        with scope(broker, a):
            install = broker.handoff(a)
            context = copy_context()
            def work():
                return a.registry.call("terminal_exec", {}), b.registry.call("terminal_exec", {})
            def guarded():
                with install():
                    return work()
            fn = guarded if "handoff" in mode else lambda: context.run(work) if mode == "copied_context" else work()
            if mode.startswith("thread"):
                results = []
                thread = threading.Thread(target=lambda: results.append(fn()))
                thread.start(); thread.join(timeout=10)
                assert not thread.is_alive() and len(results) == 1
                result = results[0]
            else:
                result = executor.submit(fn).result(timeout=10)
            assert all("blocked" in value for value in result)
        with pytest.raises(ContractError, match="retired"):
            with install():
                pytest.fail("retired helper ran")
    assert a.calls == b.calls == []
    assert not broker._frames and broker._context.get() is None


def test_N25_async_tasks_nested_calls_and_independent_B(broker, tmp_path):
    a, b = ServiceAgent(broker, tmp_path), ServiceAgent(broker, tmp_path)
    a.tool("browser_click"); b.tool("browser_click")

    async def run():
        with scope(broker, a):
            async def inherited():
                await asyncio.sleep(0)  # scheduling boundary, not a timing/race heuristic
                assert "blocked" in a.registry.call("browser_click", {})
                assert "blocked" in b.registry.call("browser_click", {})
                token = broker._context.set(None)
                try:
                    assert "blocked" in a.registry.call("browser_click", {})
                finally:
                    broker._context.reset(token)
            async def independent():
                with scope(broker, b, False):
                    assert b.registry.call("browser_click", {}) == "ordinary result"
            await asyncio.gather(asyncio.create_task(inherited()),
                                 asyncio.create_task(independent(), context=Context()))
            with pytest.raises(ContractError, match="borrow"):
                with scope(broker, b, False):
                    pytest.fail("nested ingress escaped")
    asyncio.run(run())
    assert a.calls == [] and len(b.calls) == 1
    assert not broker._frames and not broker._tasks


def test_N25_task_handoff_cannot_be_installed_over_B(broker, tmp_path):
    a, b = ServiceAgent(broker, tmp_path), ServiceAgent(broker, tmp_path)
    b.tool("terminal_exec")
    ready, done = threading.Barrier(2), threading.Barrier(2)
    handoffs = []
    def a_work():
        with scope(broker, a):
            handoffs.append(broker.handoff(a)); ready.wait(timeout=10); done.wait(timeout=10)
    thread = threading.Thread(target=a_work); thread.start(); ready.wait(timeout=10)
    with scope(broker, b, False):
        with pytest.raises(ContractError, match="foreign"):
            with handoffs[0]():
                pytest.fail("foreign authority installed")
        assert b.registry.call("terminal_exec", {}) == "ordinary result"
    done.wait(timeout=10); thread.join(timeout=10)
    assert not thread.is_alive()


@pytest.mark.parametrize("gate", ["disabled", "pin", "emergency", "profile", "unknown"])
def test_registry_guard_composes_with_existing_gates(broker, tmp_path, monkeypatch, gate):
    from core import emergency_stop
    agent = ServiceAgent(broker, tmp_path)
    agent.tool("browser_click")
    if gate in {"disabled", "profile"}:
        agent.registry.disable("browser_click")
    elif gate == "pin":
        agent.registry.set_gate(lambda name: (False, "PIN required"))
    elif gate == "emergency":
        def stopped(name):
            raise emergency_stop.EmergencyStopError("fixture")
        monkeypatch.setattr(emergency_stop, "begin_tool_dispatch", stopped)
    name = "future_unknown" if gate == "unknown" else "browser_click"
    with scope(broker, agent, False):
        result = agent.registry.call(name, {})
        assert "disabled" in result or "PIN required" in result or "emergency stop" in result or "not found" in result
    with scope(broker, agent):
        # Replacing PIN logic or toggling profile/disabled state cannot remove
        # the distinct registry boundary.
        agent.registry.enable("browser_click")
        agent.registry.set_gate(lambda name: (True, ""))
        assert "Companion site task" in agent.registry.call("browser_click", {})
    assert agent.calls == []


def test_N25_no_stale_context_after_failed_intake_and_ordinary_direct_calls_unchanged(broker, tmp_path):
    agent = ServiceAgent(broker, tmp_path)
    agent.tool("get_time")
    assert agent.registry.call("get_time", {}) == "ordinary result"
    with scope(broker, agent) as task:
        assert task.failure and task.admission is None
        assert "blocked" in agent.registry.call("get_time", {})
    assert not agent.route.protected and broker._context.get() is None
    assert agent.registry.call("get_time", {}) == "ordinary result"
    with scope(broker, agent, False):
        assert agent.registry.call("get_time", {}) == "ordinary result"
    assert len(agent.calls) == 3


def test_N25_native_fork_does_not_inherit_dispatch_authority(broker, tmp_path):
    agent = ServiceAgent(broker, tmp_path)
    agent.tool("terminal_exec")
    ctx = multiprocessing.get_context("fork")
    receive, send = ctx.Pipe(duplex=False)
    with scope(broker, agent):
        def child():
            send.send(agent.registry.call("terminal_exec", {})); send.close()
        process = ctx.Process(target=child); process.start()
        assert receive.poll(10)
        assert "blocked" in receive.recv()
        process.join(timeout=10)
        assert process.exitcode == 0
        assert "blocked" in agent.registry.call("terminal_exec", {})
    assert agent.calls == []


def test_actual_Playwright_registered_route_stays_ordinary_for_B_and_never_fires_for_A(broker, tmp_path, monkeypatch):
    import tools.browser as browser
    a, b = ServiceAgent(broker, tmp_path), ServiceAgent(broker, tmp_path)
    calls = []
    # The existing registered wrapper reaches this fake manager only for the
    # unrelated ordinary task. It never opens Playwright or a browser page.
    monkeypatch.setattr(browser.browser_manager, "click", lambda selector, **kwargs: calls.append(selector) or "fake click")
    browser.register_browser_tools(a.registry)
    browser.register_browser_tools(b.registry)
    ready, done = threading.Barrier(2), threading.Barrier(2)
    def protected():
        with scope(broker, a):
            ready.wait(timeout=10)
            assert "blocked" in a.registry.call("browser_click", {"selector": "synthetic"})
            done.wait(timeout=10)
    thread = threading.Thread(target=protected); thread.start(); ready.wait(timeout=10)
    try:
        with scope(broker, b, False):
            assert b.registry.call("browser_click", {"selector": "synthetic"}) == "fake click"
    finally:
        done.wait(timeout=10); thread.join(timeout=10)
    assert not thread.is_alive() and calls == ["synthetic"]
