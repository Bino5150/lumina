"""AGENT-PRETOOL-ACTION-INTEGRITY-01 -- pre-tool action integrity.

Live-reproduced defect (2026-09-01, real OpenRouter/z-ai/glm-5.3-flash
traffic -- see Flight Recorder turn_ids 60cced76-5485-4ed6-866c-46d6b8e941a1
and 77423fd4-3964-4ecf-be8f-381bfc16a8b8): a user asks for a concrete
machine action ("Save the memory to your mempalace"). The model's own
turn.think explicitly recognizes it needs to call save_memory. The model's
first response nonetheless carries zero tool_calls and a clean (non-
INCOMPLETE) termination -- e.g. "Executing the save FIRST -- no prose
before the receipt:". core/agent.py's pre-repair first-round path treated
ANY zero-tool, non-INCOMPLETE first response as "no tool work has
occurred yet, so this must be a genuinely complete, no-tool-needed
answer" and streamed it straight out via _stream_final() -- which never
offers a tool schema at all (chat_stream() is called with no `tools=`
kwarg by design, since it exists to deliver an already-decided final).
Once that shortcut fired, the turn was structurally guaranteed to never
call a tool for the rest of its life, regardless of what the model
actually needed to do.

Repair: a first-round zero-tool response is no longer automatically
eligible for finalization. It becomes a completion_candidate and goes
through the exact same control gate every in-tool-work-phase zero-tool
response already went through (AGENT-WORK-COMPLETE-DISCARD-01's existing
protocol) -- "finish_tool_work" with no prior Commentary this turn takes
the pre-existing zero-extra-call fast path (_finalize_completion_
candidate, verbatim promotion, no regeneration); "continue_tool_work"
returns to a full WORK round with the complete tool profile restored --
the model's actual second chance to invoke the tool it already said it
needed.

Held-candidate delivery (per explicit product direction): promoting a
completion_candidate still delivers through the same on_response_token()
channel/UI buffering a real stream already uses (_deliver_held_text()),
chunked with NO artificial delay -- reusing the existing pipe, never
fabricating provider-streaming telemetry for an answer that was already
fully generated.

Reuses the types.SimpleNamespace fake-agent pattern established across
tests/test_agent_work_complete_discard.py and
tests/test_agent_continuation_control_gate.py (read directly before
writing this file).
"""
import json
import sqlite3
import types

import pytest

from core.agent import (
    LuminaAgent,
    TurnCancelled,
    FINISH_TOOL_WORK_NAME,
    CONTINUE_TOOL_WORK_NAME,
)
from core.backends.base import TerminationStatus
from core.flight_recorder import FlightRecorder


@pytest.fixture(autouse=True)
def _no_skill_injection(monkeypatch):
    monkeypatch.setattr("core.agent.build_skills_block", lambda user_input: "")


def _tc(name, call_id=None):
    return {"id": call_id or name, "type": "function",
            "function": {"name": name, "arguments": "{}"}}


class _ScriptedLLM:
    """Same scripted-turn fake as tests/test_agent_work_complete_discard.py."""
    display_name = "FakeProvider"
    name = "fake-backend"
    supports_required_tool_choice = True

    def __init__(self, turns):
        self.turns = list(turns)
        self.call_count = 0
        self.chat_stream_calls = 0
        self.tools_seen = []

    def get_model(self):
        return "fake-model"

    def configured_model(self):
        return "fake-model-configured"

    def chat(self, messages, tools=None, max_tokens=None, reasoning_effort=None,
              tool_choice_mode=None):
        self.tools_seen.append([t["function"]["name"] for t in (tools or [])])
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
        return turn.get("termination", TerminationStatus.COMPLETE)

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
        self.chat_stream_calls += 1
        yield "REGENERATED-FROM-CHAT-STREAM"


def _fake_agent(llm, tool_result="ok", flight_recorder_path=None, fail_tool=None):
    history = []
    calls = {
        "ephemeral": [], "registry_calls": [], "on_tool_call": [],
        "on_tool_result": [], "response_tokens": [], "commentary": [],
        "build_messages_calls": 0,
    }

    def registry_call(name, args):
        calls["registry_calls"].append(name)
        if fail_tool and name == fail_tool:
            return "[error] tool execution failed: simulated failure"
        return tool_result

    def build_messages(tool_budget=0, chat_id=None):
        calls["build_messages_calls"] += 1
        return [{"role": "system", "content": f"system-prompt-build-{calls['build_messages_calls']}"}]

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
        push_ephemeral=lambda block: calls["ephemeral"].append(block),
        build_messages=build_messages,
        context_usage_snapshot=lambda tool_budget=0, chat_id=None, refresh=False: (
            {"used_tokens": 1, "max_tokens": 8000, "percent": 0.0, "chat_id": chat_id}
        ),
    )
    registry = types.SimpleNamespace(
        schema_token_estimate=lambda: 0,
        get_schemas=lambda: [
            {"type": "function", "function": {"name": "save_memory", "description": "", "parameters": {}}},
            {"type": "function", "function": {"name": "write_file", "description": "", "parameters": {}}},
            {"type": "function", "function": {"name": "run_tests", "description": "", "parameters": {}}},
            {"type": "function", "function": {"name": "read_file", "description": "", "parameters": {}}},
            {"type": "function", "function": {"name": "search_memory", "description": "", "parameters": {}}},
        ],
        list_enabled=lambda: ["save_memory", "write_file", "run_tests", "read_file", "search_memory"],
        all_tool_names=lambda: ["save_memory", "write_file", "run_tests", "read_file", "search_memory"],
        call=registry_call,
    )
    ns = types.SimpleNamespace(
        llm=llm,
        ctx=ctx,
        registry=registry,
        channel_id="test-channel",
        owner=True,
        on_tool_call=lambda name, args: calls["on_tool_call"].append((name, args)),
        on_tool_result=lambda name, result: calls["on_tool_result"].append((name, result)),
        on_think_start=lambda step: None,
        on_think_token=lambda tok: None,
        on_think_end=lambda: None,
        on_response_token=lambda tok: calls["response_tokens"].append(tok),
        on_commentary=lambda text: calls["commentary"].append(text),
        tts=None,
        _session_tool_calls=0,
        _skill_nudge_sent=False,
    )
    ns._stream_final = types.MethodType(LuminaAgent._stream_final, ns)
    ns._finalize_completion_candidate = types.MethodType(LuminaAgent._finalize_completion_candidate, ns)
    ns._finalize_with_reconciliation = types.MethodType(LuminaAgent._finalize_with_reconciliation, ns)
    if flight_recorder_path is not None:
        ns.flight_recorder = FlightRecorder(db_path=str(flight_recorder_path))
    return ns, calls


def _events(ns):
    conn = sqlite3.connect(ns.flight_recorder.db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── 1-4. explicit action request -> real tool executes before success ────

def test_1_palace_save_request_executes_save_memory_before_success_final():
    llm = _ScriptedLLM([
        {"content": "Executing the save FIRST — no prose before the receipt:",
         "termination": TerminationStatus.COMPLETE},          # round 1: narrated, zero tool_calls
        {"tool_calls": [_tc(CONTINUE_TOOL_WORK_NAME)]},        # gate: model recognizes it isn't done
        {"tool_calls": [_tc("save_memory")]},                  # round 2: the REAL tool call
        {"content": "Saved to your mempalace.",
         "termination": TerminationStatus.COMPLETE},           # round 3: genuinely done
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},          # gate: finish
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "Save the memory to your mempalace")

    assert calls["registry_calls"] == ["save_memory"]
    assert result == "Saved to your mempalace."


def test_2_file_write_request_executes_write_tool_before_success_claim():
    llm = _ScriptedLLM([
        {"content": "I'll write that file now:", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(CONTINUE_TOOL_WORK_NAME)]},
        {"tool_calls": [_tc("write_file")]},
        {"content": "File written successfully.", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "write this to disk")

    assert calls["registry_calls"] == ["write_file"]
    assert result == "File written successfully."


def test_3_test_run_request_executes_run_tests_before_result_claim():
    llm = _ScriptedLLM([
        {"content": "Running the test suite now:", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(CONTINUE_TOOL_WORK_NAME)]},
        {"tool_calls": [_tc("run_tests")]},
        {"content": "All tests passed.", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "run the tests")

    assert calls["registry_calls"] == ["run_tests"]
    assert result == "All tests passed."


def test_4_readonly_observation_request_executes_tool_before_evidence_claim():
    llm = _ScriptedLLM([
        {"content": "Let me check that file:", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(CONTINUE_TOOL_WORK_NAME)]},
        {"tool_calls": [_tc("read_file")]},
        {"content": "The file contains the expected content.", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "what does that file say")

    assert calls["registry_calls"] == ["read_file"]
    assert result == "The file contains the expected content."


# ── 5. narrated-but-uninvoked intent must never become a fabricated
#      successful completed Final via the old no-tools shortcut ─────────

def test_5_narrated_intent_with_zero_tool_calls_never_takes_the_old_shortcut():
    """The exact live-reproduced shape: a first response reads like a
    firm action commitment but carries zero tool_calls. It must not be
    streamed straight out via the old no-tools _stream_final() shortcut --
    it must pass through the control gate. If the gate is (worst case)
    told to finish anyway with no tool ever having run, the Final is
    exactly the model's own candidate text -- never anything invented by
    Lumina, and never delivered via a provider regeneration call."""
    llm = _ScriptedLLM([
        {"content": "Executing the save FIRST — no prose before the receipt:",
         "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},  # worst case: gate says finish anyway
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "Save the memory to your mempalace")

    # Never reached via the old no-tools regeneration shortcut.
    assert llm.chat_stream_calls == 0
    # The Final is exactly what the model said -- Lumina never invents an
    # extra "Saved successfully!" on top of it.
    assert result == "Executing the save FIRST — no prose before the receipt:"
    # Machine-verifiable truth: no tool of any kind actually executed.
    assert calls["registry_calls"] == []


# ── 6. bounded retry -- repeated zero-tool intent never infinite-loops ──

def test_6_repeated_zero_tool_intent_is_bounded_not_an_infinite_loop():
    llm = _ScriptedLLM([
        {"content": "I'll do it now:", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(CONTINUE_TOOL_WORK_NAME)]},
        {"content": "Really, doing it now:", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(CONTINUE_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "do the thing")

    # Exhausted the scripted sequence with a bounded, honest non-success
    # notice -- never an unbounded retry loop, and never a fabricated
    # success claim.
    assert result == "[Lumina: tool-work continuation ended without confirming completion.]"
    assert calls["registry_calls"] == []
    assert llm.call_count == 4


# ── 7. required tool unavailable -- truthful failure, not fabricated
#      success. Registry omits the tool entirely from its schemas; the
#      model can never structurally select it, and no code path invents a
#      success claim in its place. ──────────────────────────────────────

def test_7_tool_not_offered_never_produces_a_fabricated_success():
    fake_llm = _ScriptedLLM([
        {"content": "I can't find a tool for that in my current toolset.",
         "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(fake_llm)
    fake.registry.get_schemas = lambda: []  # the required tool is unavailable
    fake.registry.list_enabled = lambda: []

    result = LuminaAgent.chat(fake, "do something requiring a disabled tool")

    assert calls["registry_calls"] == []
    assert result == "I can't find a tool for that in my current toolset."


# ── 8. tool invocation fails -- truthful failure surfaces, not a
#      fabricated success claim ──────────────────────────────────────────

def test_8_failed_tool_invocation_surfaces_truthfully_not_as_success():
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("write_file")]},
        {"content": "The write failed, so I was not able to save this.",
         "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm, fail_tool="write_file")

    result = LuminaAgent.chat(fake, "write this to disk")

    assert calls["registry_calls"] == ["write_file"]
    assert "failed" in result.lower() or "not" in result.lower()
    assert result != "File written successfully."


# ── 9/10. ordinary conversation remains zero-tool, no forced tool call ──

def test_9_ordinary_conversational_turn_stays_zero_tool():
    llm = _ScriptedLLM([
        {"content": "The capital of France is Paris.", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "what's the capital of France?")

    assert result == "The capital of France is Paris."
    assert calls["registry_calls"] == []


def test_10_known_information_answer_is_never_force_routed_through_a_tool():
    llm = _ScriptedLLM([
        {"content": "2 + 2 = 4.", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "what's 2+2?")

    assert result == "2 + 2 = 4."
    assert calls["registry_calls"] == []
    # The gate offered ONLY the two internal control primitives -- never
    # the registry's own product tools alongside a forced REQUIRED choice.
    assert llm.tools_seen[-1] == [CONTINUE_TOOL_WORK_NAME, FINISH_TOOL_WORK_NAME]


# ── 11. real parallel tool calls remain legal ────────────────────────────

def test_11_real_parallel_tool_calls_remain_legal():
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("read_file", "c1"), _tc("search_memory", "c2")]},
        {"content": "Both done.", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "look at both")

    assert calls["registry_calls"] == ["read_file", "search_memory"]
    assert result == "Both done."


# ── 12. Commentary remains distinct from execution evidence ─────────────

def test_12_commentary_is_never_treated_as_execution_evidence():
    """Commentary accompanying a real tool call describes intent, not the
    tool's result -- registry_calls (the actual dispatch) is the only
    execution evidence, regardless of how confidently Commentary reads."""
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("save_memory")],
         "content": "Already saved and verified, all good."},  # confident-sounding Commentary
        {"content": "Saved.", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    LuminaAgent.chat(fake, "save it")

    # The Commentary fired, but the ONLY reason this counts as a real save
    # is that registry_calls actually recorded the dispatch -- Commentary
    # text is never itself the evidence.
    assert calls["commentary"] == ["Already saved and verified, all good."]
    assert calls["registry_calls"] == ["save_memory"]


# ── 13. Think/reasoning remains non-authoritative ────────────────────────

def test_13_think_reasoning_never_triggers_or_substitutes_for_a_tool_call():
    """A reasoning field explicitly stating intent to call a tool must not,
    by itself, cause any tool dispatch or completion-state change -- only
    the structured tool_calls field is ever acted on."""
    llm = _ScriptedLLM([
        {"content": "Noted.", "reasoning": "I need to call save_memory right now to persist this.",
         "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "remember this")

    # Reasoning text alone never dispatches a tool.
    assert calls["registry_calls"] == []
    assert result == "Noted."


# ── 14. cancellation never converts an unresolved obligation into
#       claimed success ─────────────────────────────────────────────────

def test_14_cancellation_after_candidate_creation_raises_not_finalizes():
    llm = _ScriptedLLM([
        {"content": "Executing the save FIRST — no prose before the receipt:",
         "termination": TerminationStatus.COMPLETE},
    ])
    fake, calls = _fake_agent(llm)
    cancel_event = types.SimpleNamespace(is_set=lambda: True)

    with pytest.raises(TurnCancelled):
        LuminaAgent.chat(fake, "Save the memory to your mempalace", cancel_event=cancel_event)

    # Never silently promoted as a completed success despite cancellation.
    assert calls["registry_calls"] == []
    assert not any(
        e for e in calls["response_tokens"]
        if "success" in e.lower() or "saved" in e.lower()
    )


# ── Flight Recorder machine-truth proof (Section 3 of the task) ─────────

def test_flight_recorder_shows_candidate_then_gate_then_real_tool_call(tmp_path):
    llm = _ScriptedLLM([
        {"content": "Executing the save FIRST — no prose before the receipt:",
         "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(CONTINUE_TOOL_WORK_NAME)]},
        {"tool_calls": [_tc("save_memory")]},
        {"content": "Saved to your mempalace.", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm, flight_recorder_path=tmp_path / "fr.db")

    LuminaAgent.chat(fake, "Save the memory to your mempalace")

    events = _events(fake)
    types_in_order = [e["event_type"] for e in events]
    assert "turn.started" in types_in_order
    assert "completion_candidate.created" in types_in_order
    assert "completion_candidate.discarded" in types_in_order  # the narrated-only round, discarded on continue
    assert "tool.call" in types_in_order
    assert "tool.result" in types_in_order
    assert "turn.completed" in types_in_order
    tool_calls = [e for e in events if e["event_type"] == "tool.call"]
    assert len(tool_calls) == 1
    assert json.loads(tool_calls[0]["fields_json"])["tool_name"] == "save_memory"
    # The real tool call happened strictly before turn.completed.
    assert tool_calls[0]["seq"] < events[types_in_order.index("turn.completed")]["seq"]


def test_flight_recorder_zero_tool_calls_when_model_never_invokes_one(tmp_path):
    """The exact machine-truth assertion Section 3 asks for: requested
    machine action AND tool.call == 0 AND turn.completed -- this proves
    the harness's own telemetry can distinguish that state; the fix's job
    is only to make sure the model gets a genuine chance before it, never
    to guarantee the model always takes it."""
    llm = _ScriptedLLM([
        {"content": "Executing the save FIRST — no prose before the receipt:",
         "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm, flight_recorder_path=tmp_path / "fr.db")

    LuminaAgent.chat(fake, "Save the memory to your mempalace")

    events = _events(fake)
    assert not any(e["event_type"] == "tool.call" for e in events)
    assert any(e["event_type"] == "turn.completed" for e in events)
    assert any(e["event_type"] == "completion_candidate.accepted" for e in events)


# ── Held-candidate delivery: same channel a real stream uses, no fake
#    provider-streaming telemetry ───────────────────────────────────────

def test_held_candidate_delivers_chunked_through_the_same_channel_a_stream_uses():
    llm = _ScriptedLLM([
        {"content": "This is a long enough candidate to span several chunks.",
         "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "hello")

    assert result == "This is a long enough candidate to span several chunks."
    # Delivered via more than one on_response_token() call (the same
    # buffered channel AgentWorker already flushes real stream chunks
    # through), not one single call -- but reconstructs byte-for-byte.
    assert len(calls["response_tokens"]) > 1
    assert "".join(calls["response_tokens"]) == result
    # No regeneration call was made to produce this text.
    assert llm.chat_stream_calls == 0
