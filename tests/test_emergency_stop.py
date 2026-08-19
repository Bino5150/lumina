"""
tests/test_emergency_stop.py — Patch 3A.1 emergency interlock kernel.

Focused invariant tests, not just helper getters: latch/epoch stickiness,
local-only re-arm, active-execution/in-flight-tool-dispatch blocking
re-arm, permanent epoch staleness surviving re-arm, the registry blast
door, task_queue epoching for both immediate and scheduled work, the real
LuminaAgent.chat() blocked-provider race against the existing TurnCancelled
boundary, and incident-snapshot safety.

core/emergency_stop.py is process-wide global state -- this module resets
it before and after every test via an autouse fixture so nothing leaks
into (or in from) any other test file in the same pytest session. Epoch
itself is left monotonic by _reset_for_tests() by design; every assertion
here is relative (delta/equality-to-a-captured-value), never an absolute
epoch number.
"""
import json
import threading
import time
import types

import pytest

import core.agent as agent_module
from core import emergency_stop
from core import task_queue
from core.agent import LuminaAgent, TurnCancelled
from tools.registry import ToolRegistry


@pytest.fixture(autouse=True)
def _clean_emergency_state():
    emergency_stop._reset_for_tests()
    yield
    emergency_stop._reset_for_tests()


class _Ctx:
    """Minimal ContextManager stand-in -- just enough for _chat_impl()'s
    prologue through the first llm.chat() call."""

    def __init__(self):
        self.history = []

    def add_user(self, content, source="OWNER_DIRECT"):
        self.history.append({"role": "user", "content": content})

    def push_ephemeral(self, block):
        pass

    def build_messages(self, tool_budget=0, chat_id=None):
        return []

    def add_assistant(self, content):
        self.history.append({"role": "assistant", "content": content})


# ---------------------------------------------------------------------------
# 1. Latch is sticky and epoch increments exactly once
# ---------------------------------------------------------------------------

def test_latch_is_sticky_and_epoch_increments_once():
    start_epoch = emergency_stop.current_epoch()

    incident1 = emergency_stop.latch(source="test", reason="first trigger")
    assert emergency_stop.current_epoch() == start_epoch + 1
    assert emergency_stop.is_latched() is True

    incident2 = emergency_stop.latch(source="someone-else", reason="second trigger")
    assert emergency_stop.current_epoch() == start_epoch + 1  # no further increment
    assert incident2["incident_id"] == incident1["incident_id"]
    assert incident2["source"] == "test"          # original trigger preserved
    assert incident2["reason"] == "first trigger"  # not overwritten


# ---------------------------------------------------------------------------
# 2. Local-only re-arm
# ---------------------------------------------------------------------------

def test_rearm_local_only_succeeds_from_main_thread():
    emergency_stop.latch(source="test", reason="local-only")
    assert emergency_stop.can_rearm() is True

    outcome = {}

    def _try_from_worker():
        try:
            emergency_stop.rearm_local()
            outcome["blocked"] = False
        except emergency_stop.RearmBlocked:
            outcome["blocked"] = True

    t = threading.Thread(target=_try_from_worker)
    t.start()
    t.join(timeout=5)

    assert outcome["blocked"] is True
    assert emergency_stop.is_latched() is True  # worker's attempt had no effect

    assert emergency_stop.rearm_local() is True
    assert emergency_stop.is_latched() is False


# ---------------------------------------------------------------------------
# 3. Active execution blocks re-arm
# ---------------------------------------------------------------------------

def test_active_execution_scope_blocks_rearm():
    entered = threading.Event()
    release = threading.Event()

    def _worker():
        with emergency_stop.execution_scope(kind="test_unit", label="w"):
            entered.set()
            assert release.wait(timeout=5)

    t = threading.Thread(target=_worker)
    t.start()
    assert entered.wait(timeout=5)

    emergency_stop.latch(source="test", reason="active-execution")
    assert emergency_stop.can_rearm() is False
    with pytest.raises(emergency_stop.RearmBlocked):
        emergency_stop.rearm_local()

    release.set()
    t.join(timeout=5)

    assert emergency_stop.can_rearm() is True
    assert emergency_stop.rearm_local() is True


# ---------------------------------------------------------------------------
# 4. Stale epoch remains dead after re-arm
# ---------------------------------------------------------------------------

def test_stale_epoch_stays_dead_after_rearm():
    old_epoch = emergency_stop.current_epoch()

    emergency_stop.latch(source="test", reason="stale-epoch")
    emergency_stop.rearm_local()
    assert emergency_stop.current_epoch() == old_epoch + 1  # never rolled back

    with pytest.raises(emergency_stop.StaleExecution):
        with emergency_stop.execution_scope(kind="zombie", label="old", expected_epoch=old_epoch):
            pytest.fail("stale-epoch execution scope body must never run")


# ---------------------------------------------------------------------------
# 5. Registry blocks all tool execution while latched (and preserves the
#    existing not-found / disabled / gate ordering ahead of the new check)
# ---------------------------------------------------------------------------

def test_registry_blocks_all_tool_execution_while_latched():
    registry = ToolRegistry()
    counter = {"n": 0}

    def _tool():
        counter["n"] += 1
        return "ran"

    registry.register("harmless", _tool, "harmless test tool", {"type": "object", "properties": {}})

    emergency_stop.latch(source="test", reason="block-tools")

    # Pre-existing checks still take priority over the new emergency gate.
    assert "not found" in registry.call("does_not_exist", {}).lower()
    registry.disable("harmless")
    assert "disabled" in registry.call("harmless", {}).lower()
    registry.enable("harmless")

    result = registry.call("harmless", {})
    assert "blocked" in result.lower()
    assert counter["n"] == 0  # never invoked

    emergency_stop.rearm_local()
    result2 = registry.call("harmless", {})
    assert result2 == "ran"
    assert counter["n"] == 1


# ---------------------------------------------------------------------------
# 6. In-flight tool dispatch keeps re-arm blocked
# ---------------------------------------------------------------------------

def test_in_flight_tool_dispatch_blocks_rearm_until_it_closes():
    registry = ToolRegistry()
    entered = threading.Event()
    release = threading.Event()

    def _slow(**kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return "done"

    registry.register("slow", _slow, "slow tool", {"type": "object", "properties": {}})

    result_box = {}

    def _call():
        result_box["result"] = registry.call("slow", {})

    t = threading.Thread(target=_call)
    t.start()
    assert entered.wait(timeout=5)

    # Won the lock and registered its dispatch lease before the latch --
    # still considered in-flight, must keep blocking re-arm.
    emergency_stop.latch(source="test", reason="in-flight-tool")
    assert emergency_stop.can_rearm() is False
    with pytest.raises(emergency_stop.RearmBlocked):
        emergency_stop.rearm_local()

    release.set()
    t.join(timeout=5)

    assert result_box["result"] == "done"  # allowed to finish, not killed
    assert emergency_stop.can_rearm() is True
    assert emergency_stop.rearm_local() is True


# ---------------------------------------------------------------------------
# 7. PIN/disabled gates still behave normally when not latched
# ---------------------------------------------------------------------------

def test_disabled_and_gate_checks_unaffected_when_not_latched():
    registry = ToolRegistry()
    calls = []
    registry.register("t1", lambda: calls.append("ran") or "ok", "t", {"type": "object", "properties": {}})

    registry.disable("t1")
    assert "disabled" in registry.call("t1", {}).lower()
    assert calls == []
    registry.enable("t1")

    gate_state = {"allow": False}
    registry.set_gate(lambda name: (gate_state["allow"], "gate closed"))
    result = registry.call("t1", {})
    assert "blocked" in result.lower()
    assert "gate closed" in result
    assert calls == []

    gate_state["allow"] = True
    result2 = registry.call("t1", {})
    assert result2 == "ok"
    assert calls == ["ran"]


# ---------------------------------------------------------------------------
# 8. Task creation while latched never runs
# ---------------------------------------------------------------------------

def test_submit_task_while_latched_never_runs():
    emergency_stop.latch(source="test", reason="task-block")

    ran = threading.Event()
    tid = task_queue.submit_task(lambda: ran.set())

    time.sleep(0.2)
    assert not ran.is_set()
    r = task_queue.get_task_result(tid)
    assert r["status"] == "cancelled"


# ---------------------------------------------------------------------------
# 9. Already-running background task blocks re-arm and cannot finish as
#    success after panic
# ---------------------------------------------------------------------------

def test_running_background_task_blocks_rearm_and_terminates_cancelled():
    entered = threading.Event()
    release = threading.Event()

    def _slow():
        entered.set()
        assert release.wait(timeout=5)
        return "finished normally"

    tid = task_queue.submit_task(_slow)
    assert entered.wait(timeout=5)

    emergency_stop.latch(source="test", reason="running-task")
    assert emergency_stop.can_rearm() is False
    with pytest.raises(emergency_stop.RearmBlocked):
        emergency_stop.rearm_local()

    release.set()

    r = None
    for _ in range(100):
        r = task_queue.get_task_result(tid)
        if r["status"] != "running":
            break
        time.sleep(0.05)

    # The function ran to completion (it was never killed), but a
    # compromised pre-stop result must never be surfaced as a clean
    # success after the fact.
    assert r["status"] == "cancelled"
    assert r["result"] is None

    assert emergency_stop.can_rearm() is True
    assert emergency_stop.rearm_local() is True


# ---------------------------------------------------------------------------
# 10. Old scheduled task cannot resurrect after re-arm
# ---------------------------------------------------------------------------

def test_old_scheduled_task_cannot_resurrect_after_rearm():
    ran = threading.Event()
    run_at = time.time() + 0.3
    tid = task_queue.schedule_task(run_at, lambda: ran.set())

    emergency_stop.latch(source="test", reason="scheduled-stale")
    emergency_stop.rearm_local()

    r = None
    for _ in range(100):
        r = task_queue.get_task_result(tid)
        if r["status"] not in ("scheduled", "running"):
            break
        time.sleep(0.05)

    assert not ran.is_set()
    assert r["status"] == "cancelled"


# ---------------------------------------------------------------------------
# 11. Blocked provider turn uses the existing TurnCancelled boundary
# ---------------------------------------------------------------------------

def test_blocked_provider_turn_cancelled_via_emergency_latch(monkeypatch):
    monkeypatch.setattr(agent_module, "build_skills_block", lambda query: "")

    provider_entered = threading.Event()
    release_provider = threading.Event()

    class _BlockingLLM:
        display_name = "fake"

        def __init__(self):
            self.extract_calls = 0

        def chat(self, **kwargs):
            provider_entered.set()
            assert release_provider.wait(timeout=5)
            return {"ok": True}

        def extract_message(self, response):
            self.extract_calls += 1
            return {"role": "assistant", "content": "should never be reached"}

        def is_tool_call(self, message):
            return False

    ctx = _Ctx()
    llm = _BlockingLLM()
    fake = types.SimpleNamespace(
        owner=False,
        channel_id="test-channel",
        llm=llm,
        ctx=ctx,
        registry=types.SimpleNamespace(
            schema_token_estimate=lambda: 0,
            get_schemas=lambda: [],
            list_enabled=lambda: [],
        ),
    )

    outcome = {}

    def _run_turn():
        try:
            LuminaAgent.chat(fake, "hello")
        except TurnCancelled:
            outcome["cancelled"] = True
        else:
            outcome["cancelled"] = False

    t = threading.Thread(target=_run_turn)
    t.start()
    assert provider_entered.wait(timeout=5)

    old_epoch = emergency_stop.current_epoch()
    emergency_stop.latch(source="test", reason="blocked-provider")
    assert emergency_stop.current_epoch() == old_epoch + 1

    # Turn's execution scope is still open (blocked inside llm.chat()) --
    # re-arm must be refused.
    assert emergency_stop.can_rearm() is False
    with pytest.raises(emergency_stop.RearmBlocked):
        emergency_stop.rearm_local()

    release_provider.set()
    t.join(timeout=5)

    assert outcome["cancelled"] is True
    assert llm.extract_calls == 0  # never claimed a fake completion

    # Scope has exited -- only now may local re-arm succeed.
    assert emergency_stop.can_rearm() is True
    assert emergency_stop.rearm_local() is True


# ---------------------------------------------------------------------------
# 3A.2 Part A / R2 -- pre-scope emergency race surfaces as TurnCancelled,
# not a generic error, and still preserves the submitted user turn.
# ---------------------------------------------------------------------------

def test_prescope_latch_race_raises_turncancelled_and_preserves_user_turn():
    emergency_stop.latch(source="test", reason="prescope-race")

    ctx = _Ctx()
    fake = types.SimpleNamespace(ctx=ctx)

    with pytest.raises(TurnCancelled) as exc:
        LuminaAgent.chat(fake, "keep this user turn")

    assert exc.value.partial_response == ""
    assert ctx.history == [{"role": "user", "content": "keep this user turn"}]

    emergency_stop.rearm_local()


def test_prescope_latch_race_does_not_swallow_unrelated_errors():
    class _BoomCtx(_Ctx):
        def add_user(self, content, source="OWNER_DIRECT"):
            raise RuntimeError("unrelated programmer error")

    emergency_stop.latch(source="test", reason="prescope-race-unrelated-error")
    fake = types.SimpleNamespace(ctx=_BoomCtx())

    with pytest.raises(RuntimeError, match="unrelated programmer error"):
        LuminaAgent.chat(fake, "hello")

    emergency_stop.rearm_local()


def test_worker_held_before_scope_admission_then_latched_finishes_cancelled_never_starting_work(monkeypatch):
    """R2: start a worker, hold it before execution-scope admission, latch,
    release. Must finish as TurnCancelled, never call the provider."""
    monkeypatch.setattr(agent_module, "build_skills_block", lambda query: "")

    entered_wrapper = threading.Event()
    release_wrapper = threading.Event()
    provider_calls = []

    real_execution_scope = emergency_stop.execution_scope

    def _held_execution_scope(*args, **kwargs):
        entered_wrapper.set()
        assert release_wrapper.wait(timeout=5)
        return real_execution_scope(*args, **kwargs)

    monkeypatch.setattr(agent_module.emergency_stop, "execution_scope", _held_execution_scope)

    class _LLM:
        display_name = "fake"

        def chat(self, **kwargs):
            provider_calls.append(1)
            return {"ok": True}

    ctx = _Ctx()
    fake = types.SimpleNamespace(
        owner=False, channel_id="dev-race", llm=_LLM(), ctx=ctx,
        registry=types.SimpleNamespace(schema_token_estimate=lambda: 0, get_schemas=lambda: [], list_enabled=lambda: []),
    )

    outcome = {}

    def _run_turn():
        try:
            LuminaAgent.chat(fake, "hello")
        except TurnCancelled:
            outcome["cancelled"] = True
        else:
            outcome["cancelled"] = False

    t = threading.Thread(target=_run_turn)
    t.start()
    assert entered_wrapper.wait(timeout=5)

    emergency_stop.latch(source="test", reason="worker-before-scope-race")
    release_wrapper.set()
    t.join(timeout=5)

    assert outcome["cancelled"] is True
    assert provider_calls == []  # never reached provider/tool work
    assert ctx.history == [{"role": "user", "content": "hello"}]

    emergency_stop.rearm_local()


# ---------------------------------------------------------------------------
# 12. Incident snapshot is safe and useful
# ---------------------------------------------------------------------------

def test_incident_snapshot_is_safe_and_useful():
    turn_entered = threading.Event()
    turn_release = threading.Event()

    def _turn_worker():
        with emergency_stop.execution_scope(
            kind="foreground_turn", label="chan-1",
            metadata={"channel_id": "chan-1", "owner": True, "chat_id": 42},
        ):
            turn_entered.set()
            assert turn_release.wait(timeout=5)

    t1 = threading.Thread(target=_turn_worker)
    t1.start()
    assert turn_entered.wait(timeout=5)

    registry = ToolRegistry()
    tool_entered = threading.Event()
    tool_release = threading.Event()

    def _slow_tool(**kwargs):
        tool_entered.set()
        assert tool_release.wait(timeout=5)
        return "tool done"

    registry.register("slow", _slow_tool, "t", {"type": "object", "properties": {}})
    tool_box = {}

    def _tool_worker():
        tool_box["result"] = registry.call("slow", {})

    t2 = threading.Thread(target=_tool_worker)
    t2.start()
    assert tool_entered.wait(timeout=5)

    incident = emergency_stop.latch(source="test", reason="snapshot-safety")

    assert incident["source"] == "test"
    assert incident["reason"] == "snapshot-safety"
    assert incident["old_epoch"] + 1 == incident["new_epoch"]

    assert len(incident["executions_at_latch"]) == 1
    exec_entry = incident["executions_at_latch"][0]
    assert exec_entry["kind"] == "foreground_turn"
    assert exec_entry["label"] == "chan-1"
    assert exec_entry["metadata"] == {"channel_id": "chan-1", "owner": True, "chat_id": 42}

    assert len(incident["tool_dispatches_at_latch"]) == 1
    assert incident["tool_dispatches_at_latch"][0]["name"] == "slow"

    # Must be plain JSON-serializable data -- no function objects, no
    # tool results, no raw prompts.
    blob = json.dumps(incident)
    assert "tool_done" not in blob
    assert "_slow_tool" not in blob

    snap = emergency_stop.snapshot()
    assert snap["latched"] is True
    assert snap["can_rearm"] is False
    assert snap["active_execution_count"] == 1
    assert snap["active_tool_dispatch_count"] == 1
    json.dumps(snap)  # must not raise

    turn_release.set()
    tool_release.set()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert tool_box["result"] == "tool done"

    assert emergency_stop.can_rearm() is True
    assert emergency_stop.rearm_local() is True
