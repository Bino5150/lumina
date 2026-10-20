"""AGENT-RUNAWAY-SUSPEND-01 Slice C1b -- runaway suspension wired into Lumina.

Covered here (no Qt, so these block in the main CI gate):
  * process participation is fail-closed (never started, uninitialized,
    faulted), the bridges/CLI/eval can never initialize or recover, and the
    runtime never holds its lock across store I/O;
  * turn admission in chat(): explicit > inherited > the agent's own root
    scope; missing lineage is refused and never defaults to the owner;
  * provider admission (WORK/gate requests, the final stream) and tool
    admission, each leased in the store and released afterwards;
  * the two latch triggers (no-progress breaker, second refused batch) and
    that budget exhaustion alone never latches;
  * token capture before every asynchronous handoff and no re-minting on
    fallback: the task queue (submit, schedule, discard-on-stale), the
    headless channel lock, Telegram intake (routed and headless fallback),
    Discord, subagent lineage and structured stop outcomes;
  * the Flight Recorder's best-effort retention of a tripped turn (D8).
The desktop (submit, worker, relay, /btw, receipts, the Suspension dialog)
is in test_agent_runaway_suspend_01_c1b_qt.py.

Every store is disposable (the conftest per-test store or tmp_path); no test
touches a real data directory or the running Lumina.
"""
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

import config
import core.agent as agent_module
from core import emergency_stop, flight_recorder, headless, operator_ingress as oi
from core import suspension as S, suspension_runtime as R, task_queue
from core.agent import LuminaAgent, NO_PROGRESS_NOTICE, TOOL_BATCH_LIMIT_NOTICE
from core.backends.base import TerminationStatus
from tools.registry import ToolRegistry

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _trip_owner(trigger=S.Trigger.BREAKER_TRIPPED):
    outcome = R.trip(trigger, {"tool": "read_file", "calls": 20})
    assert outcome is not None and outcome.result is S.TripResult.TRIPPED
    return outcome


@pytest.fixture
def no_lineage():
    reset = S._token_var.set(None)
    yield
    S._token_var.reset(reset)


def _leases():
    db = os.path.join(R.status().data_dir, S.MEMORY_DIRNAME, S.STORE_NAME)
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT kind, name, state, scope FROM leases ORDER BY admitted_at").fetchall()
    finally:
        conn.close()


def _fresh_runtime(tmp_path, role, *, initialize=True):
    root = tmp_path / f"root-{role}"
    (root / S.MEMORY_DIRNAME).mkdir(parents=True, mode=0o700)
    if initialize:
        S.initialize(root, ingress=oi.local_suspension_control("test")).close()
    return root, R.start(root, role=role, heartbeat=False)


# The trigger-guard harness (tests/test_gatling_trigger_guard_01.py), trimmed.

def _tc(i, name="list_recent_chats", args="{}"):
    return {"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": args}}


class _ScriptedLLM:
    display_name = "fake"

    def __init__(self, responses, on_chat=None, stream=("final answer",), on_stream=None):
        self.responses = list(responses)
        self.requests = []
        self.on_chat = on_chat
        self.stream = stream
        self.on_stream = on_stream
        self.stream_calls = 0

    def chat(self, **kwargs):
        self.requests.append(kwargs)
        if self.on_chat:
            self.on_chat()
        return self.responses.pop(0)

    def extract_message(self, response):
        return dict(response["message"])

    def is_tool_call(self, message):
        return bool(message.get("tool_calls"))

    def get_tool_calls(self, message):
        return message["tool_calls"]

    def parse_tool_call(self, tc):
        return tc["function"]["name"], json.loads(tc["function"]["arguments"] or "{}")

    def extract_termination(self, response):
        return response.get("termination", TerminationStatus.COMPLETE)

    def extract_response_telemetry(self, response):
        return {}

    def chat_stream(self, messages, max_tokens=None, reasoning_effort=None, **kwargs):
        self.stream_calls += 1
        for i, chunk in enumerate(self.stream):
            if self.on_stream:
                self.on_stream(i)
            yield chunk


def _tools(n, **kw):
    return {"message": {"role": "assistant", "content": "", "tool_calls": [_tc(i, **kw) for i in range(n)]}}


def _answer(text="done"):
    return {"message": {"role": "assistant", "content": text}}


_FINISH = {"message": {"role": "assistant", "content": "",
                       "tool_calls": [_tc(0, agent_module.FINISH_TOOL_WORK_NAME)]}}


def _done(*before):
    """A turn that does `before`, answers "done" and finishes its work."""
    return list(before) + [_answer("done"), _FINISH]


class _Ctx:
    def __init__(self):
        self.history, self.events, self.ephemeral = [], [], []

    def add_user(self, content, source="OWNER_DIRECT", **kw):
        self.history.append({"role": "user", "content": content})

    def push_ephemeral(self, block):
        self.ephemeral.append(("push", block))

    def append_ephemeral(self, block):
        self.ephemeral.append(("append", block))

    def build_messages(self, tool_budget=0, chat_id=None):
        return [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]

    def add_tool_call(self, message):
        self.events.append(("tool_call_message", len(message.get("tool_calls") or [])))

    def add_tool_result(self, tool_call_id, name, result):
        self.events.append(("tool_result", tool_call_id, name))

    def add_cancelled_tool_result(self, tool_call_id, name):
        self.events.append(("cancelled", tool_call_id, name))

    def add_refused_tool_result(self, tool_call_id, name, content):
        self.events.append(("refused", tool_call_id, name, content))

    def add_assistant(self, content):
        self.history.append({"role": "assistant", "content": content})


def _agent(llm, *, registry_call=None, owner=False, scope=None):
    ctx = _Ctx()
    executed = []

    def default_call(name, args):
        executed.append(name)
        return f"result:{name}"

    streamed = []
    fake = types.SimpleNamespace(
        owner=owner, llm=llm, ctx=ctx,
        registry=types.SimpleNamespace(schema_token_estimate=lambda: 0, get_schemas=lambda: [],
                                       list_enabled=lambda: [], call=registry_call or default_call),
        on_tool_call=lambda name, args: None, on_tool_result=lambda name, result: None,
        on_response_token=streamed.append, on_think_start=lambda step: None,
        on_think_token=lambda tok: None, on_think_end=lambda: None, on_commentary=lambda text: None,
        channel_id="test-channel", tts=None, _session_tool_calls=0, _skill_nudge_sent=True,
    )
    if scope is not None:
        fake.suspension_scope = scope
    for name in ("_stream_final", "_finalize_completion_candidate", "_finalize_with_reconciliation"):
        setattr(fake, name, types.MethodType(getattr(LuminaAgent, name), fake))
    return fake, ctx, executed, streamed


@pytest.fixture
def fr(monkeypatch):
    events = []
    monkeypatch.setattr(agent_module, "_fr_machine",
                        lambda agent, event_type, **kw: events.append((event_type, kw.get("fields") or {})))
    monkeypatch.setattr(agent_module, "build_skills_block", lambda query: ("", ""))
    return events


def _of(events, kind):
    return [f for t, f in events if t == kind]


# ---------------------------------------------------------------------------
# A. Participation is fail-closed
# ---------------------------------------------------------------------------

def test_a_process_that_never_started_admits_nothing():
    R.stop()
    with pytest.raises(R.SuspensionRefused) as exc:
        R.capture(S.OWNER_SCOPE)
    assert exc.value.refusal == "not_started"
    with pytest.raises(R.SuspensionRefused):
        with R.admitted(S.LeaseKind.TOOL, "x"):
            pytest.fail("admitted without a runtime")


def test_starting_never_creates_a_store(tmp_path):
    root, status = _fresh_runtime(tmp_path, "desktop", initialize=False)
    assert status.state is R.State.UNINITIALIZED and status.probe_status is S.StoreStatus.ABSENT_NO_TRACE
    assert os.listdir(root / S.MEMORY_DIRNAME) == []
    with pytest.raises(R.SuspensionRefused) as exc:
        R.capture(S.OWNER_SCOPE)
    assert exc.value.refusal == "uninitialized" and "Initialize" not in exc.value.text.split("(")[0]


def test_a_faulted_store_refuses_everything(tmp_path):
    root, _ = _fresh_runtime(tmp_path, "desktop")
    R.stop()
    (root / S.MEMORY_DIRNAME / S.IDENTITY_NAME).unlink()
    status = R.start(root, role="desktop", heartbeat=False)
    assert status.state is R.State.FAULT and status.fault is S.Fault.IDENTITY_MISSING
    for scope in (S.OWNER_SCOPE, S.channel_scope("discord", "1")):
        with pytest.raises(R.SuspensionRefused) as exc:
            R.capture(scope)
        assert exc.value.refusal == "store_fault"


@pytest.mark.parametrize("role", ["telegram_bridge", "discord_bridge", "cli", "eval", "driver"])
def test_only_the_desktop_may_initialize_recover_or_resume(tmp_path, role):
    _fresh_runtime(tmp_path, role, initialize=False)
    local = oi.local_suspension_control("x")
    for call in (lambda: R.initialize(ingress=local), lambda: R.repair_identity(ingress=local),
                 lambda: R.recover(ingress=local, acknowledge_unreadable_runtimes=True),
                 lambda: R.operator_view()):
        with pytest.raises(S.NotAuthorized):
            call()
    assert os.listdir(Path(R.status().data_dir) / S.MEMORY_DIRNAME) == []


def test_the_desktop_initializes_explicitly_and_becomes_active(tmp_path):
    _fresh_runtime(tmp_path, "desktop", initialize=False)
    with pytest.raises(S.NotAuthorized):
        R.initialize(ingress=oi.local_runtime_action("image_approval_button"))
    status = R.initialize(ingress=oi.local_suspension_control("initialize"))
    assert status.state is R.State.ACTIVE
    R.capture(S.OWNER_SCOPE)


def test_the_heartbeat_runs_and_stops(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "HEARTBEAT_INTERVAL_S", 0.05)
    root = tmp_path / "hb"
    (root / S.MEMORY_DIRNAME).mkdir(parents=True, mode=0o700)
    S.initialize(root, ingress=oi.local_suspension_control("t")).close()
    R.start(root, role="desktop", heartbeat=True)
    rid = R.runtime_id()
    db = root / S.MEMORY_DIRNAME / S.STORE_NAME

    def beat():
        conn = sqlite3.connect(db)
        try:
            return conn.execute("SELECT heartbeat_at FROM runtimes WHERE runtime_id = ?", (rid,)).fetchone()[0]
        finally:
            conn.close()

    first = beat()
    deadline = time.monotonic() + 10
    while beat() == first and time.monotonic() < deadline:
        time.sleep(0.02)
    assert beat() > first
    thread = R._heartbeat_thread
    R.stop()
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_status_never_holds_the_runtime_lock_during_store_io(monkeypatch):
    seen = []
    store = R._store
    real = store.operator_view

    def probe_lock():
        got = []
        t = threading.Thread(target=lambda: got.append(R._lock.acquire(timeout=2)) or R._lock.release())
        t.start()
        t.join()
        seen.append(got == [True])
        return real()

    monkeypatch.setattr(store, "operator_view", probe_lock)
    R.status()
    assert seen == [True]


# ---------------------------------------------------------------------------
# B. Turn admission in chat()
# ---------------------------------------------------------------------------

def test_a_suspended_owner_scope_refuses_the_turn_and_runs_nothing(fr, no_lineage):
    llm = _ScriptedLLM([])
    fake, ctx, executed, streamed = _agent(llm, owner=True, scope=S.OWNER_SCOPE)
    with R.carrying(R.capture(S.OWNER_SCOPE)):
        _trip_owner()
    result = LuminaAgent.chat(fake, "hello")
    assert "runaway suspension is active since" in result and "no-progress breaker" in result
    assert fake._turn_outcome == "suspended"
    assert streamed == [result] and ctx.history == [] and llm.requests == [] and executed == []
    assert _of(fr, "turn.refused")[0]["refusal"] == "suspended"


def test_work_captured_before_a_trip_never_runs_even_after_resume(fr):
    stale = R.inherited()
    _trip_owner()
    view = R.operator_view()[0]
    assert R.resume(view, ingress=oi.local_suspension_control("resume")).result is S.ResumeResult.RESUMED
    llm = _ScriptedLLM(_done())
    fake, ctx, executed, streamed = _agent(llm)
    result = LuminaAgent.chat(fake, "hello", suspension_token=stale)
    assert "began before a runaway suspension" in result and llm.requests == []
    assert _of(fr, "turn.refused") and ctx.history == []          # refused at turn admission
    fresh = R.capture(S.OWNER_SCOPE)
    assert LuminaAgent.chat(fake, "hello", suspension_token=fresh) == "done"


def test_missing_lineage_is_refused_and_never_defaults_to_the_owner(fr, no_lineage):
    llm = _ScriptedLLM([_answer()])
    fake, ctx, executed, streamed = _agent(llm, owner=False)          # a subagent-shaped agent
    result = LuminaAgent.chat(fake, "hello")
    assert "no suspension lineage" in result and llm.requests == [] and fake._turn_outcome == "suspended"


def test_a_root_turn_captures_in_the_agents_own_scope_and_binds_it(fr, no_lineage, monkeypatch):
    seen = []
    llm = _ScriptedLLM(_done(), on_chat=lambda: seen.append(R.inherited()))
    fake, ctx, executed, streamed = _agent(llm, scope=S.OWNER_SCOPE)
    assert LuminaAgent.chat(fake, "hello") == "done"
    assert seen and seen[0].scope == S.OWNER_SCOPE
    assert R.inherited() is None                                          # unbound after the turn


def test_an_explicit_token_is_bound_exactly_for_the_whole_turn(fr):
    token = R.capture(S.channel_scope("discord", "55"))
    seen = []
    llm = _ScriptedLLM(_done(_tools(1)), on_chat=lambda: seen.append(R.inherited()))
    fake, ctx, executed, streamed = _agent(
        llm, registry_call=lambda name, args: seen.append(R.inherited()) or "ok")
    assert LuminaAgent.chat(fake, "hello", suspension_token=token) == "done"
    assert seen and all(t is token for t in seen)


def test_the_real_agent_class_gives_only_owner_agents_a_root_scope():
    src = (REPO / "core" / "agent.py").read_text()
    assert "self.suspension_scope = suspension_core.OWNER_SCOPE if owner else None" in src
    assert headless.suspension_scope_for("telegram-owner", True) == S.OWNER_SCOPE
    assert headless.suspension_scope_for("discord-123", False) == "channel:discord:123"
    for channel_id in ("subagent-abc", "eval_T1", "discord-", "telegram-owner"):
        assert headless.suspension_scope_for(channel_id, False) is None


# ---------------------------------------------------------------------------
# C. Provider admission and leases
# ---------------------------------------------------------------------------

def test_a_provider_request_is_leased_while_it_runs_and_released_after(fr):
    during = []
    llm = _ScriptedLLM(_done(), on_chat=lambda: during.append(list(_leases())))
    fake, *_ = _agent(llm)
    assert LuminaAgent.chat(fake, "hello") == "done"
    assert [n for k, n, s, _ in during[0] if k == "provider" and s == "in_flight"] == ["provider.work"]
    assert all(state == "done" for kind, name, state, scope in _leases())


def test_a_provider_request_after_a_trip_is_never_dispatched(fr):
    def trip_then_answer(name, args):
        _trip_owner()
        return "ok"

    llm = _ScriptedLLM(_done(_tools(1)))
    fake, ctx, executed, streamed = _agent(llm, registry_call=trip_then_answer)
    result = LuminaAgent.chat(fake, "hello")
    assert len(llm.requests) == 1                                          # the follow-up never left
    assert "completed, but the next request was refused" in result
    assert fake._turn_outcome == "suspended"


def _stream(fake):
    return fake._stream_final([{"role": "user", "content": "u"}], [0])


def test_the_final_stream_is_admitted_at_its_start_and_released_at_its_end(fr):
    during = []
    llm = _ScriptedLLM([], stream=("a", "b"), on_stream=lambda i: during.append(_leases()))
    fake, *_ = _agent(llm)
    assert _stream(fake) == "ab"
    assert any(n == "provider.final_stream" and s == "in_flight" for k, n, s, _ in during[0])
    assert all(state == "done" for kind, name, state, scope in _leases())


def test_a_trip_mid_stream_lets_the_admitted_stream_finish_and_reports_it_in_flight(fr):
    tripped = []

    def on_stream(i):
        if i == 1:
            tripped.append(_trip_owner())

    llm = _ScriptedLLM([], stream=("a", "b", "c"), on_stream=on_stream)
    fake, *_ = _agent(llm)
    assert _stream(fake) == "abc"                                            # not cut off
    assert tripped[0].in_flight_at_trip >= 1


def test_a_final_stream_after_a_trip_never_starts(fr):
    llm = _ScriptedLLM([], stream=("never",))
    fake, ctx, executed, streamed = _agent(llm)
    _trip_owner()
    result = _stream(fake)
    assert llm.stream_calls == 0 and "began before a runaway suspension" in result
    assert streamed == [result] and fake._turn_outcome == "suspended"


# ---------------------------------------------------------------------------
# D. Tool admission (the registry's second gate)
# ---------------------------------------------------------------------------

def _registry():
    calls = []
    registry = ToolRegistry()
    registry.register("probe", lambda: calls.append(list(_leases())) or "ran", "p",
                      {"type": "object", "properties": {}})
    return registry, calls


def test_a_tool_dispatch_is_leased_while_it_runs():
    registry, calls = _registry()
    assert registry.call("probe", {}) == "ran"
    assert any(k == "tool" and n == "probe" and s == "in_flight" for k, n, s, _ in calls[0])
    assert all(state == "done" for kind, name, state, scope in _leases())


def test_a_tool_dispatch_after_a_trip_is_blocked_and_the_estop_lease_closes():
    registry, calls = _registry()
    _trip_owner()
    result = registry.call("probe", {})
    assert result.startswith("[Tool 'probe' blocked:") and "began before a runaway suspension" in result
    assert calls == [] and emergency_stop.snapshot()["active_tool_dispatch_count"] == 0


def test_a_tool_dispatch_without_lineage_is_blocked(no_lineage):
    registry, calls = _registry()
    assert "no suspension lineage" in registry.call("probe", {}) and calls == []


def test_the_estop_gate_still_runs_first():
    registry, calls = _registry()
    emergency_stop.latch(source="test", reason="order")
    try:
        assert "emergency stop active" in registry.call("probe", {})
        assert calls == [] and not any(n == "probe" for k, n, s, _ in _leases())
    finally:
        emergency_stop.rearm_local()


# ---------------------------------------------------------------------------
# E. The latch triggers
# ---------------------------------------------------------------------------

def test_the_no_progress_breaker_latches_the_turns_scope(monkeypatch):
    retained = []
    fake = types.SimpleNamespace(
        ctx=types.SimpleNamespace(add_refused_tool_result=lambda *a: None),
        llm=types.SimpleNamespace(parse_tool_call=lambda tc: ("x", {})),
        on_response_token=lambda text: None,
        flight_recorder=types.SimpleNamespace(record_machine_event=lambda *a, **k: None,
                                              retain_turn=retained.append))
    ledger = types.SimpleNamespace(thresholds=types.SimpleNamespace(read_only_trip=20, write_trip=5),
                                   observed_calls=20, distinct_fingerprints=1,
                                   repeated_names=lambda: ["read_file"])
    obs = types.SimpleNamespace(trip=True, trip_kind="read_only", stale_streak=20, write_stale_streak=0,
                                warn=False)
    assert agent_module._act_on_progress(fake, ledger, obs, [], admitted_tool_calls=20,
                                         turn_id="turn-1") == NO_PROGRESS_NOTICE
    assert fake._turn_outcome == "breaker_tripped" and retained == ["turn-1"]
    view = R.operator_view()[0]
    assert view.trigger == "breaker_tripped" and view.evidence["stale_streak"] == 20
    with pytest.raises(R.SuspensionRefused):
        R.capture(S.OWNER_SCOPE)


def test_the_second_refused_batch_latches_the_turns_scope(fr):
    llm = _ScriptedLLM([_tools(2076), _tools(5551, name="chrome_status")])
    fake, ctx, executed, streamed = _agent(llm)
    assert LuminaAgent.chat(fake, "check your surroundings") == TOOL_BATCH_LIMIT_NOTICE
    assert executed == [] and fake._turn_outcome == "batch_refused"
    view = R.operator_view()[0]
    assert view.trigger == "second_batch_refused" and view.evidence["refused_batches"] == 2
    assert _of(fr, "suspension.tripped")[0]["result"] == "tripped"


def test_the_first_refused_batch_alone_does_not_latch(fr):
    llm = _ScriptedLLM(_done(_tools(2076)))
    fake, *_ = _agent(llm)
    LuminaAgent.chat(fake, "check")
    assert R.operator_view() == ()


def test_budget_exhaustion_alone_never_latches(fr, monkeypatch):
    monkeypatch.setattr(config, "MAX_TOOL_CALLS_PER_TURN", 2)
    llm = _ScriptedLLM([_tools(3)])
    fake, *_ = _agent(llm)
    LuminaAgent.chat(fake, "go")
    assert _of(fr, "turn.budget_exhausted") and R.operator_view() == ()
    R.capture(S.OWNER_SCOPE)


def test_a_trip_from_stale_work_never_creates_a_second_incident():
    stale = R.inherited()
    first = _trip_owner()
    view = R.operator_view()[0]
    R.resume(view, ingress=oi.local_suspension_control("resume"))
    late = R.trip(S.Trigger.SECOND_BATCH_REFUSED, {}, token=stale)
    assert late.result is S.TripResult.LATE_EVIDENCE and late.suspension_id == first.suspension_id
    assert R.operator_view() == ()


# ---------------------------------------------------------------------------
# F. The task queue carries the creating turn's token
# ---------------------------------------------------------------------------

def _wait_result(task_id, done=("success", "error", "cancelled")):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        result = task_queue.get_task_result(task_id)
        if result and result["status"] in done:
            return result
        time.sleep(0.01)
    raise AssertionError(f"task {task_id} never finished")


def test_carry_into_never_mints_lineage_for_a_thread(no_lineage):
    ran = []
    wrapped = R.carry_into(lambda: ran.append(R.inherited()))
    with pytest.raises(R.SuspensionRefused) as exc:
        wrapped()
    assert exc.value.refusal == "missing_token" and ran == []


def test_a_forgotten_lease_stays_in_flight_and_blocks_resume():
    lease = R.admit(S.LeaseKind.TOOL, "forgotten")
    del lease                                         # no finalizer: never released by GC
    _trip_owner()
    report = R.lease_report(S.OWNER_SCOPE)
    assert [l.name for l in report.live] == ["forgotten"]


def test_background_work_with_no_lineage_never_runs(no_lineage):
    ran = []
    result = _wait_result(task_queue.submit_task(lambda: ran.append(1)))
    assert result["status"] == "cancelled" and result["reason"] == "suspension:missing_token" and ran == []


def test_background_work_runs_under_the_creating_turns_exact_token():
    token = R.inherited()
    seen = []
    result = _wait_result(task_queue.submit_task(lambda: seen.append(R.inherited())))
    assert result["status"] == "success" and seen == [token]


def test_scheduled_work_whose_scope_was_suspended_never_runs():
    ran = []
    task_id = task_queue.schedule_task(time.time() + 3600, lambda: ran.append(1))
    _trip_owner()
    with task_queue._scheduled_lock:                         # bring it due now, keeping its token
        task_queue._scheduled[:] = [(0,) + item[1:] for item in task_queue._scheduled if item[1] == task_id] + \
            [item for item in task_queue._scheduled if item[1] != task_id]
    task_queue._scheduler_wake.set()
    result = _wait_result(task_id)
    assert result["status"] == "cancelled" and result["reason"] == "suspension:stale_token" and ran == []


def test_a_result_that_finishes_after_its_scope_was_suspended_is_discarded():
    started, release = threading.Event(), threading.Event()

    def work():
        started.set()
        assert release.wait(timeout=10)
        return "late result"

    task_id = task_queue.submit_task(work)
    assert started.wait(timeout=10)
    _trip_owner()
    release.set()
    result = _wait_result(task_id)
    assert result == {**result, "status": "cancelled", "result": None, "reason": "suspension:stale_token"}


def test_a_scheduled_heap_entry_carries_the_token_captured_at_schedule_time():
    token = R.inherited()
    task_id = task_queue.schedule_task(time.time() + 3600, lambda: None)
    try:
        with task_queue._scheduled_lock:
            entry = next(item for item in task_queue._scheduled if item[1] == task_id)
        assert entry[3] is token
    finally:
        task_queue.cancel_task(task_id)


# ---------------------------------------------------------------------------
# G. Headless channels
# ---------------------------------------------------------------------------

class _FakeHeadlessAgent:
    def __init__(self, outcome=None):
        self.owner = True
        self.on_tool_call = self.on_tool_result = lambda *a: None
        self.calls = []
        self.outcome = outcome
        self.registry = types.SimpleNamespace(all_tool_names=lambda: [])

    def chat(self, task, **kwargs):
        self.calls.append(kwargs)
        self._turn_outcome = self.outcome
        return "answer"


@pytest.fixture
def fake_headless(monkeypatch):
    agent = _FakeHeadlessAgent()
    monkeypatch.setattr(headless, "get_headless_agent", lambda *a, **k: agent)
    return agent


def test_headless_rechecks_the_token_after_waiting_for_the_channel_lock(fake_headless):
    token = R.capture(S.OWNER_SCOPE)
    lock = headless._headless_turn_lock("telegram-owner")
    box = {}
    with lock:
        runner = threading.Thread(target=lambda: box.setdefault("r", headless.run_headless_turn(
            task="hi", channel_id="telegram-owner", owner=True, suspension_token=token)))
        runner.start()
        _trip_owner()
    runner.join(timeout=10)
    assert box["r"]["success"] is False and box["r"]["outcome"] == "suspended"
    assert fake_headless.calls == []


def test_headless_hands_chat_exactly_the_carried_token(fake_headless):
    token = R.capture(S.OWNER_SCOPE)
    result = headless.run_headless_turn(task="hi", channel_id="telegram-owner", owner=True,
                                        suspension_token=token)
    assert result == {"success": True, "response": "answer"}
    assert fake_headless.calls[0]["suspension_token"] is token


@pytest.mark.parametrize("outcome", ["suspended", "breaker_tripped", "batch_refused"])
def test_a_stopped_headless_turn_is_a_structured_failure(fake_headless, outcome):
    fake_headless.outcome = outcome
    result = headless.run_headless_turn(task="hi", channel_id="telegram-owner", owner=True)
    assert result == {"success": False, "outcome": outcome, "response": "answer"}
    assert headless.headless_reply_text(result) == "answer"


def test_headless_reply_text_for_completed_and_failed_turns():
    assert headless.headless_reply_text({"success": True, "response": "x"}) == "x"
    assert headless.headless_reply_text({"success": False, "error": "boom"}) == "[Lumina error: boom]"


# ---------------------------------------------------------------------------
# H. Subagents: owner lineage, never owner tools; structured stops
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_subagent(monkeypatch):
    import tools.subagent as subagent_module
    made = []

    class _Child:
        def __init__(self, owner, **kwargs):
            self.owner = owner
            self.registry = object()
            self.suspension_scope = None
            self.outcome = None
            made.append(self)

        def chat(self, task):
            self.inherited = R.inherited()
            self._turn_outcome = self.outcome
            return "child answer"

    monkeypatch.setattr(subagent_module, "LuminaAgent", _Child)
    monkeypatch.setattr(subagent_module, "apply_tool_profile", lambda *a, **k: None)
    monkeypatch.setattr(subagent_module, "subagent_lane_refusal", lambda *a: None)
    return subagent_module, made


def test_an_owner_subagent_inherits_owner_lineage_without_owner_authority(fake_subagent):
    subagent_module, made = fake_subagent
    parent = R.inherited()
    result = subagent_module.spawn_subagent("look something up")
    assert result["success"] is True and result["result"] == "child answer"
    assert made[0].owner is False and made[0].inherited is parent


def test_a_suspended_subagent_is_never_reported_as_completed(fake_subagent, monkeypatch):
    subagent_module, made = fake_subagent
    original = subagent_module.LuminaAgent

    def stopped(**kwargs):
        child = original(**kwargs)
        child.outcome = "suspended"
        return child

    monkeypatch.setattr(subagent_module, "LuminaAgent", stopped)
    result = subagent_module.spawn_subagent("look something up")
    assert result["success"] is False and result["outcome"] == "suspended"


# ---------------------------------------------------------------------------
# I. Telegram intake: capture after WAKE's gates, carried, never re-minted
# ---------------------------------------------------------------------------

@pytest.fixture
def telegram(monkeypatch, tmp_path, telegram_remote_access_on):
    import comms.telegram_bridge as bridge
    from comms import telegram_origin_routing as routing
    from core import idempotency
    monkeypatch.setattr(idempotency, "LEDGER_PATH", str(tmp_path / "ledger.db"))
    monkeypatch.setattr(bridge, "_owner_chat_id", lambda: "42")
    monkeypatch.setattr(bridge.telegram_access, "capture_ingress_authority", lambda: object())
    monkeypatch.setattr(bridge.telegram_access, "ingress_authority_valid", lambda a: True)
    monkeypatch.setattr(bridge, "freshness_refusal", lambda message: None)
    routing._reset_for_tests()
    getattr(bridge, "_reset_for_tests", lambda: None)()
    return bridge


def _telegram_update(text="hello", message_id=9001):
    replies = []

    async def _reply(value):
        replies.append(value)
        return types.SimpleNamespace(message_id=5001)

    message = types.SimpleNamespace(text=text, message_id=message_id, reply_text=_reply,
                                    date=None, reply_to_message=None)
    return types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=42), message=message,
                                 edited_message=None), replies


def test_a_suspended_owner_gets_the_fixed_refusal_and_nothing_runs(telegram, monkeypatch):
    monkeypatch.setattr(telegram, "run_headless_turn", lambda **k: pytest.fail("no headless turn"))
    _trip_owner()
    update, replies = _telegram_update()
    asyncio.run(telegram.handle_message(update, context=None))
    assert len(replies) == 1 and "runaway suspension is active since" in replies[0]
    assert R.operator_view()[0].selector not in replies[0]                # time and trigger only


def test_the_intake_token_reaches_the_routed_turn_and_the_fallback_unchanged(telegram, monkeypatch):
    captured, routed, fallback = [], [], []
    real_capture = telegram.suspension_runtime.capture
    monkeypatch.setattr(telegram.suspension_runtime, "capture",
                        lambda scope: captured.append(real_capture(scope)) or captured[-1])
    route = telegram.origin_routing.OriginRoute("42", 7001, 1, "runtime", 1.0, 1e12)
    monkeypatch.setattr(telegram.origin_routing, "resolve",
                        lambda **k: telegram.origin_routing.RouteResolution(route, "exact"))
    monkeypatch.setattr(telegram.origin_routing, "dispatch",
                        lambda route, text, **k: routed.append(k["suspension_token"]))   # unavailable
    monkeypatch.setattr(telegram, "run_headless_turn",
                        lambda **k: fallback.append(k["suspension_token"]) or {"success": True,
                                                                                "response": "ok"})
    update, replies = _telegram_update()
    asyncio.run(telegram.handle_message(update, context=None))
    assert len(captured) == 1
    assert routed == [captured[0]] and fallback == [captured[0]]           # never re-minted


def test_a_suspension_landing_while_the_request_waits_stops_it(telegram, monkeypatch):
    def waiting_on_prime(route, text, **k):
        _trip_owner()                                       # lands while the request waits
        return None

    route = telegram.origin_routing.OriginRoute("42", 7001, 1, "runtime", 1.0, 1e12)
    monkeypatch.setattr(telegram.origin_routing, "resolve",
                        lambda **k: telegram.origin_routing.RouteResolution(route, "exact"))
    monkeypatch.setattr(telegram.origin_routing, "dispatch", waiting_on_prime)
    ran = []

    def fallback(**k):
        try:
            R.verify(k["suspension_token"])
        except R.SuspensionRefused as refused:
            return {"success": False, "outcome": "suspended", "response": refused.text}
        ran.append(1)
        return {"success": True, "response": "should not run"}

    monkeypatch.setattr(telegram, "run_headless_turn", fallback)
    update, replies = _telegram_update()
    asyncio.run(telegram.handle_message(update, context=None))
    assert ran == [] and "began before a runaway suspension" in replies[-1]


def test_operator_command_refusal_still_comes_before_capture(telegram, monkeypatch):
    monkeypatch.setattr(telegram.suspension_runtime, "capture",
                        lambda scope: pytest.fail("captured before the command refusal"))
    update, replies = _telegram_update(" /btw hi")
    asyncio.run(telegram.handle_message(update, context=None))
    assert replies == [oi.RELAYED_COMMAND_REFUSAL]


# ---------------------------------------------------------------------------
# J. Discord: an isolated channel scope; a public refusal says nothing
# ---------------------------------------------------------------------------

@pytest.fixture
def discord(monkeypatch):
    discord_bridge = pytest.importorskip("comms.discord_bridge")
    monkeypatch.setattr(discord_bridge, "client", types.SimpleNamespace(user=types.SimpleNamespace(id=1)))
    monkeypatch.setattr(discord_bridge, "rate_check", lambda user_id: (True, 0))
    monkeypatch.setattr(discord_bridge, "load_discord_persona", lambda: None)
    return discord_bridge


def _discord_message(discord_bridge, channel_id=777):
    replies = []

    class _Typing:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    async def _reply(text):
        replies.append(text)

    me = discord_bridge.client.user
    message = types.SimpleNamespace(
        author=types.SimpleNamespace(bot=False, id=5), mentions=[me], content="<@1> hello there",
        channel=types.SimpleNamespace(id=channel_id, typing=lambda: _Typing()), reply=_reply)
    return message, replies


def test_discord_carries_its_own_channel_scope(discord, monkeypatch):
    seen = []
    monkeypatch.setattr(discord, "run_headless_turn",
                        lambda **k: seen.append(k["suspension_token"]) or {"success": True, "response": "hi"})
    message, replies = _discord_message(discord)
    asyncio.run(discord.on_message(message))
    assert seen[0].scope == "channel:discord:777" and replies == ["hi"]


def test_a_suspension_landing_while_a_discord_turn_waits_still_says_nothing(discord, monkeypatch):
    def suspended_while_waiting(**k):
        return {"success": False, "outcome": "suspended",
                "response": "[Lumina: runaway suspension is active since 2026-10-20 12:00 UTC (detail).]"}

    monkeypatch.setattr(discord, "run_headless_turn", suspended_while_waiting)
    message, replies = _discord_message(discord)
    asyncio.run(discord.on_message(message))
    assert replies == [R.PUBLIC_REFUSAL]


def test_a_suspended_discord_channel_gets_only_the_generic_public_line(discord, monkeypatch):
    monkeypatch.setattr(discord, "run_headless_turn", lambda **k: pytest.fail("must not run"))
    with R.carrying(R.capture("channel:discord:777")):
        _trip_owner()
    message, replies = _discord_message(discord)
    asyncio.run(discord.on_message(message))
    assert replies == [R.PUBLIC_REFUSAL]
    other, other_replies = _discord_message(discord, channel_id=778)
    monkeypatch.setattr(discord, "run_headless_turn", lambda **k: {"success": True, "response": "fine"})
    asyncio.run(discord.on_message(other))
    assert other_replies == ["fine"]                                       # scopes stay isolated


# ---------------------------------------------------------------------------
# K. Eval harness, Flight Recorder D8, wiring pins
# ---------------------------------------------------------------------------

def test_the_eval_harness_never_initializes_outside_its_scratch_root(tmp_path):
    code = (f"import sys; sys.path.insert(0, {str(REPO)!r}); sys.path.insert(0, {str(REPO / 'eval')!r})\n"
            "import run_eval\n"
            f"run_eval.config.DATA_DIR = {str(tmp_path / 'elsewhere')!r}\n"
            "run_eval.start_eval_suspension_runtime()\n")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          env=dict(os.environ, LUMINA_TESTING="1"))
    assert proc.returncode != 0 and "refusing to initialize" in (proc.stderr + proc.stdout)
    assert not (tmp_path / "elsewhere").exists()


def test_the_flight_recorder_retains_a_tripped_turn_best_effort(tmp_path):
    recorder = flight_recorder.FlightRecorder(db_path=str(tmp_path / "fr.db"))
    recorder.record_machine_event("tool.call", turn_id="tripped", fields={"name": "x"})
    recorder.record_machine_event("tool.call", turn_id="other", fields={"name": "x"})
    assert recorder.retain_turn("tripped") == 1
    recorder.record_machine_event("tool.result", turn_id="tripped", fields={"name": "x"})
    rows = dict(recorder._conn.execute(
        "SELECT event_type || ':' || turn_id, expires_at FROM events").fetchall())
    floor = time.time() + flight_recorder.ERROR_RETENTION_SECONDS - 60
    assert rows["tool.call:tripped"] >= floor and rows["tool.result:tripped"] >= floor
    assert rows["tool.call:other"] < floor
    recorder.enabled = False
    assert recorder.retain_turn("tripped") == 0


@pytest.mark.parametrize("path, needle", [
    ("main.py", 'suspension_runtime.start(config.DATA_DIR, role="desktop")'),
    ("main.py", 'suspension_runtime.start(config.DATA_DIR, role="cli")'),
    ("comms/telegram_bridge.py", 'suspension_runtime.start(config.DATA_DIR, role="telegram_bridge")'),
    ("comms/discord_bridge.py", 'suspension_runtime.start(config.DATA_DIR, role="discord_bridge")'),
    (".agents/skills/run-lumina/driver.py", 'suspension_runtime.start(config.DATA_DIR, role="driver")'),
])
def test_every_real_entry_point_participates(path, needle):
    assert needle in (REPO / path).read_text(encoding="utf-8")


def test_no_entry_point_initializes_a_real_store_on_its_own():
    for path in ("main.py", "comms/telegram_bridge.py", "comms/discord_bridge.py",
                 ".agents/skills/run-lumina/driver.py", "core/headless.py"):
        text = (REPO / path).read_text(encoding="utf-8")
        assert "suspension.initialize(" not in text and "recover_destructively(" not in text, path


# ---------------------------------------------------------------------------
# L. Shipped docs and self-knowledge say exactly what C1 provides
# ---------------------------------------------------------------------------

def _doc(name):
    return (REPO / "docs" / name).read_text(encoding="utf-8")


def test_the_security_doc_states_guarantees_and_the_boundaries_not_yet_covered():
    text = _doc("security.md")
    section = text[text.index("## Runaway suspension"):text.index("## Content from outside you")]
    for needle in ("Nothing new starts in it", "never runs again", "Work already running may finish",
                   "desktop-only and deliberate", "Telegram `/resume` is not active", "Initialize",
                   "Recover", "does **not** yet cover", "vision specialist", "Dreaming",
                   "Staged items wait", "pending tool", "image draft", "stays pending",
                   "*any* scope", "approve it again afterwards", "approvals made before a suspension",
                   "can still be generated", "older version", "best-effort"):
        assert needle in section, needle
    assert "isn't checked against a suspension yet" not in section


def test_the_docs_and_knowledge_pack_describe_the_staged_item_refusal():
    assert "staged items can't be applied right" in _doc("troubleshooting.md")
    entry = {e["title"]: e["content"] for e in json.loads(
        (REPO / "knowledge_packs" / "official" / "lumina_self_knowledge.json").read_text())}["Runaway suspension"]
    assert "refused while a suspension is active; it stays pending" in entry
    assert "approvals made before a suspension (they are not cancelled)" in entry
    assert "approvals of things staged earlier" not in entry


def test_no_doc_still_says_a_breaker_stop_continues_with_the_next_message():
    settings = _doc("settings.md")
    breaker = settings[settings.index("### No-progress breaker"):]
    assert "suspends" in breaker.split("The breaker ignores numbers")[0]
    trouble = _doc("troubleshooting.md")
    assert "Resume, then send a new message." in trouble and "## Runaway suspension" in trouble


def test_operator_and_telegram_docs_name_the_desktop_controls():
    assert "| `/suspension` |" in _doc("operator-commands.md")
    assert "While your work is suspended" in _doc("telegram-setup.md")
    assert "runaway-suspension" in _doc("channels.md")


def test_the_knowledge_pack_describes_runaway_suspension_truthfully():
    entries = {e["title"]: e["content"] for e in
               json.loads((REPO / "knowledge_packs" / "official" / "lumina_self_knowledge.json").read_text())}
    entry = entries["Runaway suspension"]
    for needle in ("no-progress breaker", "Work from before the suspension never runs again",
                   "/suspension", "Telegram /resume is not active yet", "A budget limit alone",
                   "Not yet covered", "Emergency Interlock"):
        assert needle in entry, needle
    assert "only the owner's next message continues." not in entries["Tool-call limits"].split("any other hard limit")[0]
    assert "/suspension" in entries["Slash commands"]


# ---------------------------------------------------------------------------
# Q. Applying staged items while suspended (C1 stopgap, owner ruling Q3)
#
# Settings pending actions, Palace promotions, Pending Tools and image-draft
# approvals are refused at the mutation itself (not at a button) while the
# affected scope is suspended or can't be verified; the staged item stays
# pending and nothing replays it later. Not C2: approvals recorded before a
# trip are not cancelled or re-validated after a resume.
# ---------------------------------------------------------------------------

import core.image_generation_draft as draft_store
import tools.image_generation as image_tool
from core import palace_promotion as pp
from tools import palace, pending_actions, toolmaker

_DRAFT_CHANNEL, _DRAFT_CHAT = "q3-channel", 11


@pytest.fixture
def staged(tmp_path, monkeypatch):
    monkeypatch.setattr(pending_actions, "QUEUE_PATH", str(tmp_path / "pending_actions.json"))
    monkeypatch.setattr(pending_actions, "AUDIT_LOG_PATH", str(tmp_path / "pending_audit.log"))
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "q3.db"))
    pp._LIVE.clear()
    draft_store._drafts.clear(), draft_store._approvals.clear(), draft_store._presented.clear()
    yield tmp_path
    pp._LIVE.clear()
    draft_store._drafts.clear(), draft_store._approvals.clear(), draft_store._presented.clear()


class _PromptCtx:
    def __init__(self):
        self.prompts, self.cleared = [], 0

    def update_system_prompt(self, text):
        self.prompts.append((text, _leases()))

    def clear(self):
        self.cleared += 1


def _pending_audit():
    try:
        with open(pending_actions.AUDIT_LOG_PATH) as f:
            return [json.loads(line)["event"] for line in f if line.strip()]
    except FileNotFoundError:
        return []


def _stage_prompt_edit():
    pending_actions.stage_action("edit_prompt", {"new_prompt": "be someone else"}, "runaway asked")
    [(aid, _)] = pending_actions._load_queue().items()
    return aid


def _trip_scope(scope):
    outcome = R.trip(S.Trigger.BREAKER_TRIPPED, {"tool": "read_file", "calls": 20}, token=R.capture(scope))
    assert outcome.result is S.TripResult.TRIPPED
    return outcome


def _resume_all():
    for view in R.operator_view():
        assert R.resume(view, ingress=oi.local_suspension_control("q3 resume")).result is S.ResumeResult.RESUMED


def test_q3_a_pending_action_applies_when_nothing_is_suspended_under_a_write_lease(staged):
    aid, agent = _stage_prompt_edit(), types.SimpleNamespace(ctx=_PromptCtx())
    assert pending_actions._apply_action(aid, agent) == "System prompt updated."
    [(text, leases_during)] = agent.ctx.prompts
    assert text == "be someone else"
    assert ("write", "pending_action:edit_prompt", "in_flight", S.OWNER_SCOPE) in leases_during
    assert ("write", "pending_action:edit_prompt", "done", S.OWNER_SCOPE) in _leases()
    assert pending_actions._load_queue() == {} and _pending_audit() == ["staged", "approved"]


@pytest.mark.parametrize("kind,payload", [("edit_prompt", {"new_prompt": "x"}), ("reset_chat", {}),
                                          ("delete_memory", {"memory_id": 1}),
                                          ("delete_knowledge", {"entry_id": 1})])
def test_q3_every_pending_action_kind_is_refused_while_the_owner_is_suspended(staged, kind, payload, monkeypatch):
    touched = []
    monkeypatch.setattr("tools.memory._delete_memory_direct", lambda *a: touched.append(a))
    monkeypatch.setattr("tools.knowledge._delete_knowledge_direct", lambda *a: touched.append(a))
    pending_actions.stage_action(kind, payload)
    [(aid, entry)] = pending_actions._load_queue().items()
    agent = types.SimpleNamespace(ctx=_PromptCtx())
    _trip_owner()
    leases_before = _leases()
    result = pending_actions._apply_action(aid, agent)
    assert "staged items can't be applied right now" in result and "still pending" in result
    assert agent.ctx.prompts == [] and agent.ctx.cleared == 0 and touched == []
    assert pending_actions._load_queue() == {aid: entry}
    assert _pending_audit() == ["staged", "refused"]
    assert _leases() == leases_before  # refused before any lease


def test_q3_a_suspended_discord_channel_also_blocks_it_because_the_queue_cannot_say_who_staged_it(staged):
    aid, agent = _stage_prompt_edit(), types.SimpleNamespace(ctx=_PromptCtx())
    _trip_scope("channel:discord:4242")
    assert "runaway suspension is active" in pending_actions._apply_action(aid, agent)
    assert agent.ctx.prompts == [] and aid in pending_actions._load_queue()


def test_q3_unknown_scopes_after_a_lossy_recovery_block_it(staged, tmp_path):
    aid, agent = _stage_prompt_edit(), types.SimpleNamespace(ctx=_PromptCtx())
    root, status = _fresh_runtime(tmp_path, "test")
    db = sqlite3.connect(os.path.join(root, S.MEMORY_DIRNAME, S.STORE_NAME))
    db.execute("UPDATE meta SET value = 'suspended' WHERE key = 'unknown_scope_policy'")
    db.commit(), db.close()
    with S.carrying(R.capture(S.OWNER_SCOPE)):
        assert "runaway suspension is active" in pending_actions._apply_action(aid, agent)
    assert agent.ctx.prompts == [] and aid in pending_actions._load_queue()


def test_q3_a_local_stop_blocks_it(staged, monkeypatch):
    aid, agent = _stage_prompt_edit(), types.SimpleNamespace(ctx=_PromptCtx())
    monkeypatch.setattr(R._active_store(), "local_stops", lambda: ("channel:discord:1",))
    assert "runaway suspension is active" in pending_actions._apply_action(aid, agent)
    assert agent.ctx.prompts == [] and aid in pending_actions._load_queue()


@pytest.mark.parametrize("how", ["never_started", "uninitialized", "faulted", "store_unreadable"])
def test_q3_it_is_refused_when_the_suspension_state_cannot_be_verified(staged, tmp_path, monkeypatch, how):
    aid, agent = _stage_prompt_edit(), types.SimpleNamespace(ctx=_PromptCtx())
    if how == "never_started":
        R.stop()
    elif how == "uninitialized":
        _fresh_runtime(tmp_path, "test", initialize=False)
    elif how == "faulted":
        root, _ = _fresh_runtime(tmp_path, "test")
        R.stop()
        db = os.path.join(root, S.MEMORY_DIRNAME, S.STORE_NAME)
        with open(db, "r+b") as f:
            f.write(b"not a sqlite database at all" * 8)
        assert R.start(root, role="test", heartbeat=False).state is not R.State.ACTIVE
    else:
        def unreadable():
            raise S.StoreUnavailable("database is locked")
        monkeypatch.setattr(R._active_store(), "operator_view", unreadable)
    result = pending_actions._apply_action(aid, agent)
    assert result.startswith("[Lumina:") and "still pending" in result
    assert agent.ctx.prompts == [] and aid in pending_actions._load_queue()
    assert _pending_audit()[-1] == "refused"


def test_q3_a_trip_landing_between_the_check_and_the_lease_is_still_refused(staged, monkeypatch):
    aid, agent = _stage_prompt_edit(), types.SimpleNamespace(ctx=_PromptCtx())
    real_admit = R.admit

    def admit_then_trip_elsewhere(*args, **kwargs):
        lease = real_admit(*args, **kwargs)
        _trip_scope("channel:discord:77")
        return lease

    monkeypatch.setattr(R, "admit", admit_then_trip_elsewhere)
    assert "runaway suspension is active" in pending_actions._apply_action(aid, agent)
    assert agent.ctx.prompts == [] and aid in pending_actions._load_queue()
    assert [lease[2] for lease in _leases() if lease[0] == "write"][-1:] == ["done"]
    assert "in_flight" not in [lease[2] for lease in _leases()]


def test_q3_a_refused_action_is_never_replayed_and_applies_exactly_once_when_asked_again(staged):
    aid, agent = _stage_prompt_edit(), types.SimpleNamespace(ctx=_PromptCtx())
    _trip_owner()
    pending_actions._apply_action(aid, agent)
    _resume_all()
    assert agent.ctx.prompts == [] and aid in pending_actions._load_queue()  # resume replays nothing
    assert pending_actions._apply_action(aid, agent) == "System prompt updated."
    assert len(agent.ctx.prompts) == 1
    assert pending_actions._apply_action(aid, agent).startswith("[Error: no pending action")
    assert _pending_audit() == ["staged", "refused", "approved"]


def test_q3_the_local_suspension_controls_still_work_with_staged_items_waiting(staged):
    aid = _stage_prompt_edit()
    _trip_owner()
    view = R.operator_view()[0]
    assert R.lease_report(S.OWNER_SCOPE).live == ()
    assert R.resume(view, ingress=oi.local_suspension_control("resume")).result is S.ResumeResult.RESUMED
    assert R.operator_view() == () and aid in pending_actions._load_queue()
    for path in ("ui/suspension_dialog.py",):
        assert "owner_application" not in (REPO / path).read_text(encoding="utf-8")


# -- Palace promotion (a pending action approved through its own review) ----

def _palace_request(staged):
    palace.init_palace_db()
    stored = palace.palace_store("crown me", wing="projects", room="r", layer=2, untrusted=True)
    snap = pp.capture_promotion_snapshot("drawer", stored["drawer_id"], 1, operation_id="q3op")
    return stored["drawer_id"], snap


def _drawer_layer(did):
    conn = sqlite3.connect(config.DB_PATH)
    try:
        return conn.execute("SELECT c.layer FROM palace_closets c JOIN palace_drawers d ON d.closet_id=c.id "
                            "WHERE d.id=?", (did,)).fetchone()[0]
    finally:
        conn.close()


def test_q3_a_palace_promotion_is_refused_while_suspended_without_consuming_the_approval(staged):
    did, snap = _palace_request(staged)
    approval = pp.mint_owner_promotion_approval(snap, owner_event="q3:click")
    _trip_owner()
    with pytest.raises(R.SuspensionRefused) as refused:
        pp.promote_with_owner_approval(approval)
    assert "runaway suspension is active" in pp.describe_refusal(refused.value)
    assert _drawer_layer(did) == 2 and len(pp._LIVE) == 1
    _resume_all()
    assert pp.promote_with_owner_approval(approval)["to_layer"] == 1 and _drawer_layer(did) == 1


def test_q3_a_palace_promotion_with_an_unverifiable_store_is_refused(staged):
    did, snap = _palace_request(staged)
    approval = pp.mint_owner_promotion_approval(snap, owner_event="q3:click")
    R.stop()
    with pytest.raises(R.SuspensionRefused):
        pp.promote_with_owner_approval(approval)
    assert _drawer_layer(did) == 2


# -- Pending Tools ------------------------------------------------------------

_TOOL_SRC = ("def register_q3_hello_tool(registry):\n"
             "    registry.register('q3_hello', lambda: 'hi', 'says hi', {'type': 'object', 'properties': {}})\n")


@pytest.fixture
def toolmaker_dirs(tmp_path, monkeypatch):
    pending, live = tmp_path / "_pending", tmp_path / "custom"
    pending.mkdir(), live.mkdir()
    monkeypatch.setattr(toolmaker, "PENDING_DIR", str(pending))
    monkeypatch.setattr(toolmaker, "CUSTOM_TOOLS_DIR", str(live))
    monkeypatch.setattr(toolmaker, "AUDIT_LOG_PATH", str(tmp_path / "toolmaker_audit.log"))
    (pending / "q3_hello.py").write_text(_TOOL_SRC)
    yield pending, live
    sys.modules.pop("q3_hello", None)


def test_q3_a_pending_tool_is_refused_while_suspended_and_stays_pending(toolmaker_dirs):
    pending, live = toolmaker_dirs
    registry = ToolRegistry()
    _trip_owner()
    result = toolmaker.approve_pending_tool("q3_hello", registry)
    assert "runaway suspension is active" in result
    assert (pending / "q3_hello.py").exists() and not (live / "q3_hello.py").exists()
    assert "q3_hello" not in registry.list_tools() and "q3_hello" not in sys.modules
    _resume_all()
    assert "approved, loaded, and live" in toolmaker.approve_pending_tool("q3_hello", registry)
    assert (live / "q3_hello.py").exists()


def test_q3_the_terminal_approval_script_opens_the_store_and_is_refused_while_suspended(toolmaker_dirs, tmp_path):
    pending, live = toolmaker_dirs
    root, _ = _fresh_runtime(tmp_path, "test")
    with S.carrying(R.capture(S.OWNER_SCOPE)):
        _trip_owner()
    R.stop()
    code = ("import sys, builtins; sys.path.insert(0, %r); import tools.toolmaker as t; "
            "t.PENDING_DIR, t.CUSTOM_TOOLS_DIR, t.AUDIT_LOG_PATH = %r, %r, %r; "
            "builtins.input = lambda *a: 'y'; sys.argv = ['approve_tool.py', 'q3_hello']; "
            "import runpy; runpy.run_path(%r, run_name='__main__')"
            % (str(REPO), str(pending), str(live), str(tmp_path / "a.log"), str(REPO / "scripts" / "approve_tool.py")))
    env = dict(os.environ, LUMINA_TESTING="1", LUMINA_DATA_DIR=str(root))
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "runaway suspension is active" in out.stdout
    assert (pending / "q3_hello.py").exists() and not (live / "q3_hello.py").exists()


# -- Image drafts -------------------------------------------------------------

def _draft(**kw):
    draft = draft_store.stage_draft(
        specialist="higgsfield", model="higgsfield-ai/soul/standard", settings={"prompt": "q3"},
        cost_estimate=0.05, cost_unit="usd", manifest_provider="higgsfield",
        channel_id=_DRAFT_CHANNEL, chat_id=_DRAFT_CHAT, staged_at_turn_seq=0, **kw)
    assert draft_store.mark_draft_presented(draft.draft_id, channel_id=_DRAFT_CHANNEL, chat_id=_DRAFT_CHAT)
    return draft


def _approve(draft):
    return draft_store.approve_draft(draft.draft_id, channel_id=_DRAFT_CHANNEL, chat_id=_DRAFT_CHAT)


def _arm_spend(monkeypatch, calls):
    target = types.SimpleNamespace(specialist="higgsfield", model="higgsfield-ai/soul/standard",
                                   registry=object(), policy=object())
    monkeypatch.setattr(image_tool.svc, "resolve_image_generation_target", lambda: target)
    monkeypatch.setattr(image_tool, "_build_adapter", lambda: (types.SimpleNamespace(
        estimate_cost=lambda *, model, settings: 0.05), None))

    def fake_generate_image(**kwargs):
        calls.append(_leases())
        return types.SimpleNamespace(outcome="success", cost_estimate=0.05, diagnostic=None, artifacts=(),
                                     failed_output_indices=(), failed_manifest_indices=())

    monkeypatch.setattr(image_tool.svc, "generate_image", fake_generate_image)


def _spend(draft):
    return image_tool.generate_image(draft.draft_id, channel_id=_DRAFT_CHANNEL, chat_id=_DRAFT_CHAT,
                                     current_turn_seq=1)


def test_q3_a_draft_records_the_scope_of_the_turn_that_staged_it(staged):
    assert _draft().suspension_scope == S.OWNER_SCOPE
    with S.carrying(R.capture("channel:discord:9")):
        assert _draft().suspension_scope == "channel:discord:9"
    box = []
    worker = threading.Thread(target=lambda: box.append(_draft()))
    worker.start(), worker.join()
    assert box[0].suspension_scope is None


def test_q3_approving_a_draft_is_refused_while_its_scope_is_suspended_and_it_stays_pending(staged):
    draft = _draft()
    _trip_owner()
    leases_before = _leases()
    with pytest.raises(R.SuspensionRefused) as refused:
        _approve(draft)
    assert "staged items can't be applied right now" in refused.value.text
    assert refused.value.refusal == "staged_application_suspended"
    assert not draft_store.is_approved(draft.draft_id) and draft_store.peek_draft(draft.draft_id) is not None
    assert _leases() == leases_before
    _resume_all()
    assert _approve(draft) is True and draft_store.is_approved(draft.draft_id)


def test_q3_a_draft_approval_checks_its_own_scope_not_someone_elses(staged):
    with S.carrying(R.capture("channel:discord:9")):
        channel_draft = _draft()
    _trip_owner()
    assert _approve(channel_draft) is True  # the owner scope is not the affected one
    other = _draft()
    with pytest.raises(R.SuspensionRefused):
        _approve(other)


def test_q3_a_draft_staged_without_lineage_is_blocked_by_any_suspension(staged):
    box = []
    worker = threading.Thread(target=lambda: box.append(_draft()))
    worker.start(), worker.join()
    _trip_scope("channel:discord:5")
    with pytest.raises(R.SuspensionRefused):
        _approve(box[0])
    assert not draft_store.is_approved(box[0].draft_id)


def test_q3_an_invalid_draft_is_still_just_false_and_never_touches_the_store(staged, monkeypatch):
    monkeypatch.setattr(R, "owner_application", lambda *a, **k: (_ for _ in ()).throw(AssertionError("store")))
    assert draft_store.approve_draft("nope", channel_id=_DRAFT_CHANNEL, chat_id=_DRAFT_CHAT) is False
    draft = _draft()
    assert draft_store.approve_draft(draft.draft_id, channel_id="elsewhere", chat_id=_DRAFT_CHAT) is False


def test_q3_a_draft_discarded_while_its_approval_waits_on_the_store_is_not_approved(staged, monkeypatch):
    draft = _draft()
    real = R.owner_application

    def discard_meanwhile(*args, **kwargs):
        draft_store.discard_draft(draft.draft_id)
        return real(*args, **kwargs)

    monkeypatch.setattr(R, "owner_application", discard_meanwhile)
    assert _approve(draft) is False and not draft_store.is_approved(draft.draft_id)


def test_q3_the_word_match_approval_is_refused_and_its_event_released_while_suspended(staged):
    draft = _draft()
    _trip_owner()
    agent_module._maybe_approve_pending_draft("yes", "OWNER_DIRECT", _DRAFT_CHANNEL, _DRAFT_CHAT,
                                              approval_event_id="q3-event-1", current_turn_seq=1)
    assert not draft_store.is_approved(draft.draft_id)
    assert draft_store.try_claim_approval_event("q3-event-1")  # released, not spent


def test_q3_an_approved_draft_cannot_spend_while_suspended_and_is_not_consumed(staged, monkeypatch):
    calls = []
    _arm_spend(monkeypatch, calls)
    draft = _draft()
    assert _approve(draft)
    _trip_owner()
    result = _spend(draft)
    assert result.startswith("outcome: suspended") and "Nothing was spent" in result
    assert calls == [] and draft_store.peek_draft(draft.draft_id) is not None
    assert draft_store.is_approved(draft.draft_id)  # C2 scope: not cancelled, only blocked


def test_q3_a_direct_spend_with_no_lineage_is_refused(staged, monkeypatch, no_lineage):
    calls = []
    _arm_spend(monkeypatch, calls)
    with S.carrying(R.capture(S.OWNER_SCOPE)):
        draft = _draft()
        assert _approve(draft)
    result = _spend(draft)
    assert result.startswith("outcome: suspended") and "no suspension lineage" in result
    assert calls == [] and draft_store.peek_draft(draft.draft_id) is not None


def test_q3_a_spend_from_a_turn_that_began_before_the_trip_is_refused_even_after_resume(staged, monkeypatch):
    calls = []
    _arm_spend(monkeypatch, calls)
    draft = _draft()
    assert _approve(draft)
    stale = R.inherited()
    _trip_owner()
    _resume_all()
    with S.carrying(stale):
        assert _spend(draft).startswith("outcome: suspended")
    assert calls == []


def test_q3_an_admitted_spend_runs_under_a_provider_lease_and_releases_it(staged, monkeypatch):
    calls = []
    _arm_spend(monkeypatch, calls)
    draft = _draft()
    assert _approve(draft)
    assert _spend(draft).startswith("outcome: success")
    assert ("provider", "image_generation", "in_flight", S.OWNER_SCOPE) in calls[0]
    assert ("provider", "image_generation", "done", S.OWNER_SCOPE) in _leases()
    assert draft_store.peek_draft(draft.draft_id) is None
