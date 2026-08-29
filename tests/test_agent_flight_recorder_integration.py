"""
AGENT-FLIGHT-RECORDER-01A1 -- agent-loop integration tests.

Exercises the REAL instrumentation added to core/agent.py (turn_id minting
in chat(), turn.started/completed/cancelled/failed, turn.think/commentary/
final, tool.batch/call/result) end to end, through LuminaAgent.chat(fake,
...) against the same types.SimpleNamespace _ScriptedLLM fake-agent
pattern tests/test_agent_tool_think.py established -- with one addition:
fake.flight_recorder is set to a real, ISOLATED FlightRecorder(db_path=
tmp_path/...) so every event actually lands somewhere queryable, instead
of relying purely on mocked on_think_*/on_commentary/on_tool_call
callbacks the way test_agent_tool_think.py does. This is what proves the
recorder calls added alongside those callbacks actually fire, in the
right order, with the right provenance -- not just that the callbacks
still fire (already covered elsewhere).
"""
import json
import sqlite3
import threading

import pytest

from core.agent import LuminaAgent, TurnCancelled, FINISH_TOOL_WORK_NAME
from core.backends.base import TerminationStatus
from core.flight_recorder import FlightRecorder


def _tc(name, call_id=None):
    return {"id": call_id or name, "type": "function",
            "function": {"name": name, "arguments": "{}"}}


class _ScriptedLLM:
    """Same scripted-turn fake as test_agent_tool_think.py's _ScriptedLLM."""
    display_name = "FakeProvider"
    name = "fake-backend"

    def __init__(self, turns):
        self.turns = list(turns)
        self.call_count = 0

    def get_model(self):
        return "fake-model"

    def configured_model(self):
        return "fake-model-configured"

    def chat(self, messages, tools=None, max_tokens=None, reasoning_effort=None, tool_choice_mode=None):
        idx = self.call_count
        self.call_count += 1
        turn = self.turns[idx]
        if "raise" in turn:
            raise turn["raise"]
        return {"_turn": idx}

    def extract_message(self, response):
        turn = self.turns[response["_turn"]]
        message = {"role": "assistant", "content": turn.get("content", "")}
        if "tool_calls" in turn:
            message["tool_calls"] = turn["tool_calls"]
        return message

    def extract_termination(self, response):
        turn = self.turns[response["_turn"]]
        return turn.get("termination", TerminationStatus.UNKNOWN)

    def extract_reasoning(self, response):
        turn = self.turns[response["_turn"]]
        return turn.get("reasoning")

    def is_tool_call(self, message):
        return bool(message.get("tool_calls"))

    def get_tool_calls(self, message):
        return message.get("tool_calls", [])

    def parse_tool_call(self, tc):
        return tc["function"]["name"], {}

    def chat_stream(self, messages, max_tokens=None, reasoning_effort=None):
        yield "final streamed response"


def _fake_agent(llm, tmp_path, tool_result="ok", flight_recorder=True):
    import types

    history = []

    def registry_call(name, args):
        return tool_result

    ctx = types.SimpleNamespace(
        history=history,
        max_tokens=8000,
        add_user=lambda content, source="OWNER_DIRECT": history.append(
            {"role": "user", "content": content}),
        add_assistant=lambda content: history.append(
            {"role": "assistant", "content": content}),
        add_tool_call=lambda message: history.append(message),
        add_tool_result=lambda tool_call_id, name, result: history.append(
            {"role": "tool", "tool_call_id": tool_call_id, "name": name, "content": result}),
        add_cancelled_tool_result=lambda tool_call_id, name: history.append(
            {"role": "tool", "tool_call_id": tool_call_id, "name": name,
             "content": "[Cancelled by operator before execution.]"}),
        push_ephemeral=lambda block: None,
        build_messages=lambda tool_budget=0, chat_id=None: [],
        context_usage_snapshot=lambda tool_budget=0, chat_id=None, refresh=False: (
            {"used_tokens": 123, "max_tokens": 8000, "percent": 1.5, "chat_id": chat_id}
        ),
    )
    registry = types.SimpleNamespace(
        schema_token_estimate=lambda: 456,
        get_schemas=lambda: [],
        list_enabled=lambda: ["search_memory", "read_file"],
        all_tool_names=lambda: [],
        call=registry_call,
    )
    ns = types.SimpleNamespace(
        llm=llm,
        ctx=ctx,
        registry=registry,
        channel_id="test-channel",
        owner=True,
        on_tool_call=lambda name, args: None,
        on_tool_result=lambda name, result: None,
        on_think_start=lambda step: None,
        on_think_token=lambda tok: None,
        on_think_end=lambda: None,
        on_response_token=lambda tok: None,
        on_commentary=lambda text: None,
        tts=None,
        _session_tool_calls=0,
        _skill_nudge_sent=False,
    )
    ns._stream_final = types.MethodType(LuminaAgent._stream_final, ns)
    if flight_recorder:
        ns.flight_recorder = FlightRecorder(db_path=str(tmp_path / "fr.db"))
    return ns


def _events(ns):
    conn = sqlite3.connect(ns.flight_recorder.db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Think -> Commentary -> Tool ordering through the recorder (test 21) ──

def test_recorder_preserves_think_commentary_tool_ordering(tmp_path):
    llm = _ScriptedLLM([
        {"content": "Checking memory for that.", "reasoning": "I should search memory first.",
         "tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake = _fake_agent(llm, tmp_path)

    result = LuminaAgent.chat(fake, "find it")

    assert result == "final streamed response"
    events = _events(fake)
    types_in_order = [e["event_type"] for e in events]
    think_i = types_in_order.index("turn.think")
    commentary_i = types_in_order.index("turn.commentary")
    call_i = types_in_order.index("tool.call")
    assert think_i < commentary_i < call_i, types_in_order
    # Every event in this turn shares one turn_id.
    turn_ids = {e["turn_id"] for e in events}
    assert len(turn_ids) == 1
    assert all(e["turn_id"] for e in events)


def test_turn_started_carries_effective_budgets_and_identity(tmp_path):
    llm = _ScriptedLLM([{"content": "hi", "termination": TerminationStatus.COMPLETE}])
    fake = _fake_agent(llm, tmp_path)

    LuminaAgent.chat(fake, "hello")

    events = _events(fake)
    started = next(e for e in events if e["event_type"] == "turn.started")
    assert started["provenance"] == "machine"
    assert started["backend"] == "fake-backend"
    assert started["model"] == "fake-model-configured"
    fields = json.loads(started["fields_json"])
    assert fields["tool_iteration_limit"] > 0
    assert fields["tool_schema_footprint"] == 456
    assert fields["enabled_tool_count"] == 2
    assert fields["context_limit"] == 8000
    assert fields["context_used"] == 123


def test_turn_completed_recorded_on_success(tmp_path):
    llm = _ScriptedLLM([{"content": "hi", "termination": TerminationStatus.COMPLETE}])
    fake = _fake_agent(llm, tmp_path)

    LuminaAgent.chat(fake, "hello")

    events = _events(fake)
    completed = [e for e in events if e["event_type"] == "turn.completed"]
    assert len(completed) == 1
    assert completed[0]["provenance"] == "machine"
    assert completed[0]["severity"] == "info"


def test_turn_cancelled_recorded_on_cooperative_stop(tmp_path):
    llm = _ScriptedLLM([{"content": "hi", "termination": TerminationStatus.COMPLETE}])
    fake = _fake_agent(llm, tmp_path)
    cancel_event = threading.Event()
    cancel_event.set()

    with pytest.raises(TurnCancelled):
        LuminaAgent.chat(fake, "hello", cancel_event=cancel_event)

    events = _events(fake)
    cancelled = [e for e in events if e["event_type"] == "turn.cancelled"]
    assert len(cancelled) == 1
    assert cancelled[0]["severity"] == "warning"
    assert json.loads(cancelled[0]["fields_json"])["reason"] == "cooperative_stop"


def test_turn_failed_recorded_on_error_sentinel_return(tmp_path):
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"raise": RuntimeError("provider exploded")},
    ])
    fake = _fake_agent(llm, tmp_path)

    result = LuminaAgent.chat(fake, "find it")

    assert "rejected the continuation" in result
    events = _events(fake)
    failed = [e for e in events if e["event_type"] == "turn.failed"]
    assert len(failed) == 1
    assert failed[0]["severity"] == "error"
    assert json.loads(failed[0]["fields_json"])["reason"] == "error_sentinel"


# ── tool.batch / tool.call / tool.result fields ──────────────────────────

def test_tool_batch_and_call_and_result_fields(tmp_path):
    llm = _ScriptedLLM([
        {"content": "Doing two things.", "tool_calls": [_tc("search_memory", "c1"), _tc("read_file", "c2")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake = _fake_agent(llm, tmp_path)

    LuminaAgent.chat(fake, "do it")

    events = _events(fake)
    batch = next(e for e in events if e["event_type"] == "tool.batch")
    batch_fields = json.loads(batch["fields_json"])
    assert batch_fields["batch_size"] == 2
    assert batch_fields["batch_ordinal"] == 1
    assert batch_fields["concurrent"] is False  # never labeled parallel -- dispatch is sequential

    calls = [e for e in events if e["event_type"] == "tool.call"]
    assert len(calls) == 2
    call_fields = [json.loads(c["fields_json"]) for c in calls]
    assert [f["call_ordinal"] for f in call_fields] == [0, 1]
    assert [f["tool_name"] for f in call_fields] == ["search_memory", "read_file"]
    assert all(f["tool_tier"] == "read_only" for f in call_fields)  # both are read_only in TOOL_TIERS
    assert all("args_hash" in f and len(f["args_hash"]) == 64 for f in call_fields)

    results = [e for e in events if e["event_type"] == "tool.result"]
    assert len(results) == 2
    result_fields = [json.loads(r["fields_json"]) for r in results]
    assert all(f["success"] is True for f in result_fields)
    assert all(isinstance(f["duration_s"], float) for f in result_fields)
    assert all(e["provenance"] == "machine" for e in calls + results)


def test_failed_tool_call_recorded_as_warning_with_success_false(tmp_path):
    def _boom_call(name, args):
        raise RuntimeError("tool exploded")

    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake = _fake_agent(llm, tmp_path)
    fake.registry.call = _boom_call

    LuminaAgent.chat(fake, "find it")

    events = _events(fake)
    result = next(e for e in events if e["event_type"] == "tool.result")
    fields = json.loads(result["fields_json"])
    assert fields["success"] is False
    assert result["severity"] == "warning"
    assert "tool exploded" in fields["result_summary"]


# ── recorder failure cannot break an agent turn (test 22) ────────────────

def test_recorder_failure_does_not_break_the_turn(tmp_path):
    llm = _ScriptedLLM([
        {"content": "Checking memory.", "reasoning": "thinking", "tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake = _fake_agent(llm, tmp_path)

    class _BrokenRecorder:
        def record_machine_event(self, *a, **kw):
            raise RuntimeError("recorder is on fire")
        def record_model_expression(self, *a, **kw):
            raise RuntimeError("recorder is on fire")

    fake.flight_recorder = _BrokenRecorder()

    result = LuminaAgent.chat(fake, "find it")

    assert result == "final streamed response"


# ── recorder disabled/absent stays operationally safe (part of test 23) ──

def test_agent_with_no_flight_recorder_attribute_stays_safe(tmp_path):
    llm = _ScriptedLLM([{"content": "hi", "termination": TerminationStatus.COMPLETE}])
    fake = _fake_agent(llm, tmp_path, flight_recorder=False)
    assert not hasattr(fake, "flight_recorder")

    result = LuminaAgent.chat(fake, "hello")

    assert result == "final streamed response"


def test_real_luminaagent_construction_gets_a_working_recorder(tmp_path, monkeypatch):
    """LuminaAgent.__init__ actually wires self.flight_recorder -- exercised
    against an explicit isolated instance (never the real DATA_DIR
    singleton) via the flight_recorder_instance override."""
    fr = FlightRecorder(db_path=str(tmp_path / "real_agent_fr.db"))
    agent = LuminaAgent(owner=False, channel_id="fr-construction-test",
                         flight_recorder_instance=fr)
    assert agent.flight_recorder is fr
    assert agent.flight_recorder.enabled is True
