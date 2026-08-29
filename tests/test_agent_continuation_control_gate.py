"""
AGENT-CONTINUATION-CONTROL-GATE-01A -- Two-Phase Continuation Protocol.

Replaces AGENT-REQUIRED-FULL-SCHEMA-01A's live-verified finding: offering
finish_tool_work alongside the full ~88-tool owner profile under REQUIRED
made completion an ~89-way forced-choice contest that z-ai/glm-5.3-flash
intermittently lost to an irrelevant ordinary tool. This file exercises the
replacement state machine:

  WORK PHASE:   full enabled product profile, tool_choice=AUTO. Real tool
                calls execute normally and loop back to another WORK round.
                A response with no real tool call is NOT completion -- it
                is the trigger to ask the completion-control gate (unless
                this is a genuine first-round answer, or the response was
                positively truncated -- both keep their pre-existing
                AGENT-CONTINUATION-01A behavior unchanged).
  CONTROL GATE: exactly [continue_tool_work, finish_tool_work] -- never a
                product tool -- tool_choice=REQUIRED where the backend
                supports it. "finish" streams the final answer. "continue"
                restores the full WORK-phase profile for exactly one more
                round before the gate can be re-entered. Both/neither
                selected, or a positively truncated gate response, are
                treated as one bounded retry of the GATE ITSELF (not a
                WORK round), sharing corrective_retry_used's one-shot
                per-turn budget. A "continue" immediately contradicted by
                another no-real-tool WORK round is tolerated exactly once
                (Section 13's ping-pong bound) before surfacing a visible
                non-success -- never an unbounded loop.

Reuses the types.SimpleNamespace fake-agent pattern from
tests/test_agent_continuation_contract.py (read directly before writing
this file) -- LuminaAgent.chat(fake_self, ...) called unbound against a
minimal stand-in, with _stream_final bound on as a real method so the
success path genuinely streams rather than being mocked away.
"""
import types

import pytest

from core.agent import LuminaAgent, FINISH_TOOL_WORK_NAME, CONTINUE_TOOL_WORK_NAME
from core.backends.base import TerminationStatus, ToolChoiceMode


@pytest.fixture(autouse=True)
def _no_skill_injection(monkeypatch):
    """Same pattern as test_agent_tool_budget.py/test_openai_token_limit_
    translation.py -- without this, a real skill matched against this
    file's literal prompt strings ("find it", etc.) can add an extra,
    unrelated ephemeral push that has nothing to do with the control-gate
    behavior under test."""
    monkeypatch.setattr("core.agent.build_skills_block", lambda user_input: "")


def _tc(name, call_id=None):
    return {"id": call_id or name, "type": "function",
            "function": {"name": name, "arguments": "{}"}}


class _ScriptedLLM:
    """Same scripted-turn fake as test_agent_continuation_contract.py's
    _ScriptedLLM, extended to record tool_choice_mode per call (like
    test_agent_required_tool_choice.py's own fake) so gate-vs-work request
    shape can be asserted directly."""
    display_name = "FakeProvider"
    name = "fake"
    supports_required_tool_choice = True

    def __init__(self, turns):
        self.turns = list(turns)
        self.call_count = 0
        self.tools_seen = []
        self.tool_choice_modes_seen = []

    def get_model(self):
        return "fake-model"

    def chat(self, messages, tools=None, max_tokens=None, reasoning_effort=None,
             tool_choice_mode=None):
        self.tools_seen.append([t["function"]["name"] for t in (tools or [])])
        self.tool_choice_modes_seen.append(tool_choice_mode)
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

    def is_tool_call(self, message):
        return bool(message.get("tool_calls"))

    def get_tool_calls(self, message):
        return message.get("tool_calls", [])

    def parse_tool_call(self, tc):
        return tc["function"]["name"], {}

    def chat_stream(self, messages, max_tokens=None, reasoning_effort=None):
        yield "final streamed response"


def _fake_agent(llm, tool_result="ok"):
    history = []
    calls = {
        "ephemeral": [], "registry_calls": [], "on_tool_call": [],
        "on_tool_result": [], "response_tokens": [], "commentary": [],
        "build_messages_calls": 0,
    }

    def registry_call(name, args):
        calls["registry_calls"].append(name)
        return tool_result

    def build_messages(tool_budget=0, chat_id=None):
        calls["build_messages_calls"] += 1
        # Return a distinguishable marker per call so a test can prove the
        # exact same (already ephemeral-consumed) message list is reused
        # for _stream_final(), or that a fresh call happened.
        return [{"role": "system", "content": f"system-prompt-build-{calls['build_messages_calls']}"}]

    ctx = types.SimpleNamespace(
        history=history,
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
    )
    registry = types.SimpleNamespace(
        schema_token_estimate=lambda: 0,
        get_schemas=lambda: [
            {"type": "function", "function": {"name": "search_memory", "description": "", "parameters": {}}},
            {"type": "function", "function": {"name": "read_file", "description": "", "parameters": {}}},
        ],
        list_enabled=lambda: ["search_memory", "read_file"],
        all_tool_names=lambda: ["search_memory", "read_file"],
        call=registry_call,
    )
    ns = types.SimpleNamespace(
        llm=llm,
        ctx=ctx,
        registry=registry,
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
    return ns, calls


# ── Schema/mode separation invariants (Section 17, 21) ──────────────────

def test_work_rounds_never_offer_either_control_primitive():
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"tool_calls": [_tc("read_file")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    LuminaAgent.chat(fake, "find and read it")

    for round_tools in llm.tools_seen[:3]:  # every WORK round, including the trigger round
        assert FINISH_TOOL_WORK_NAME not in round_tools
        assert CONTINUE_TOOL_WORK_NAME not in round_tools


def test_gate_offers_exactly_the_two_control_primitives_and_nothing_else():
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    LuminaAgent.chat(fake, "find it")

    gate_tools = llm.tools_seen[2]
    assert set(gate_tools) == {CONTINUE_TOOL_WORK_NAME, FINISH_TOOL_WORK_NAME}
    assert "search_memory" not in gate_tools
    assert "read_file" not in gate_tools


def test_work_rounds_always_request_auto_never_required():
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"tool_calls": [_tc("read_file")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    LuminaAgent.chat(fake, "find and read it")

    assert llm.tool_choice_modes_seen[:3] == [
        ToolChoiceMode.AUTO, ToolChoiceMode.AUTO, ToolChoiceMode.AUTO,
    ]
    assert llm.tool_choice_modes_seen[3] == ToolChoiceMode.REQUIRED


# ── Full-profile restoration after "continue" (Section 11, 31) ──────────

def test_continue_restores_full_work_profile_for_the_next_round():
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},              # INITIAL WORK -> real tool
        {"content": "", "termination": TerminationStatus.COMPLETE},  # WORK -> gate
        {"tool_calls": [_tc(CONTINUE_TOOL_WORK_NAME)]},        # GATE -> continue
        {"tool_calls": [_tc("read_file")]},                    # WORK (restored) -> real tool
        {"content": "", "termination": TerminationStatus.COMPLETE},  # WORK -> gate
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},          # GATE -> finish
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "find and read it")

    assert result == "final streamed response"
    assert calls["registry_calls"] == ["search_memory", "read_file"]
    # The WORK round immediately after "continue" sees BOTH enabled
    # product tools again -- the full profile, not a narrowed or empty set.
    post_continue_tools = llm.tools_seen[3]
    assert set(post_continue_tools) == {"search_memory", "read_file"}
    assert llm.tool_choice_modes_seen[3] == ToolChoiceMode.AUTO


def test_continue_does_not_touch_registry_or_tool_accounting():
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(CONTINUE_TOOL_WORK_NAME)]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)
    fake._session_tool_calls = 0

    LuminaAgent.chat(fake, "find it")

    assert CONTINUE_TOOL_WORK_NAME not in calls["registry_calls"]
    assert calls["registry_calls"] == ["search_memory"]
    assert fake._session_tool_calls == 1
    assert all(name != CONTINUE_TOOL_WORK_NAME for name, _ in calls["on_tool_call"])


# ── Malformed gate responses (Section 16) ────────────────────────────────

def test_malformed_gate_both_controls_selected_retries_the_gate_and_recovers():
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(CONTINUE_TOOL_WORK_NAME), _tc(FINISH_TOOL_WORK_NAME)]},  # malformed
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},  # retry -> finish
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "find it")

    assert result == "final streamed response"
    # The retry re-entered the GATE (2 control-tool requests), not a WORK
    # round -- both post-malformed requests offer only the two controls.
    assert set(llm.tools_seen[2]) == {CONTINUE_TOOL_WORK_NAME, FINISH_TOOL_WORK_NAME}
    assert set(llm.tools_seen[3]) == {CONTINUE_TOOL_WORK_NAME, FINISH_TOOL_WORK_NAME}
    assert llm.tool_choice_modes_seen[2] == ToolChoiceMode.REQUIRED
    assert llm.tool_choice_modes_seen[3] == ToolChoiceMode.REQUIRED


def test_malformed_gate_neither_control_selected_after_retry_budget_spent_is_visible_non_success():
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"content": "still thinking", "termination": TerminationStatus.COMPLETE},  # malformed: neither
        {"content": "still thinking again", "termination": TerminationStatus.COMPLETE},  # retry: malformed again
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "find it")

    assert result.startswith("[Lumina:")
    assert "final streamed response" not in result
    assert calls["response_tokens"] == [result]
    assert llm.call_count == 4


def test_malformed_gate_response_never_dispatches_the_wrong_names_through_registry():
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(CONTINUE_TOOL_WORK_NAME), _tc(FINISH_TOOL_WORK_NAME)]},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    LuminaAgent.chat(fake, "find it")

    assert calls["registry_calls"] == ["search_memory"]


def test_gate_positively_incomplete_response_is_not_treated_as_finish():
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"content": "cut off", "termination": TerminationStatus.INCOMPLETE},  # gate: incomplete
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},  # retry -> finish
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "find it")

    assert result == "final streamed response"
    assert llm.call_count == 4


# ── Ping-pong bound (Section 13) ─────────────────────────────────────────

def test_continue_contradicted_by_immediate_no_tool_again_is_bounded_not_infinite():
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},               # INITIAL WORK
        {"content": "", "termination": TerminationStatus.COMPLETE},  # WORK -> gate
        {"tool_calls": [_tc(CONTINUE_TOOL_WORK_NAME)]},        # GATE -> continue
        {"content": "", "termination": TerminationStatus.COMPLETE},  # WORK (restored) -> no tool AGAIN -> gate
        {"tool_calls": [_tc(CONTINUE_TOOL_WORK_NAME)]},        # GATE -> continue AGAIN (contradiction)
        {"content": "", "termination": TerminationStatus.COMPLETE},  # must NEVER be reached
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "find it")

    assert result.startswith("[Lumina:")
    assert "final streamed response" not in result
    # Stopped at the second contradicting "continue" -- never reached a
    # third WORK round or the 6th scripted turn.
    assert llm.call_count == 5


def test_continue_contradiction_recovers_if_second_gate_call_finishes_instead():
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(CONTINUE_TOOL_WORK_NAME)]},
        {"content": "", "termination": TerminationStatus.COMPLETE},  # contradicts the continue
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},  # this final adjudication resolves cleanly
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "find it")

    assert result == "final streamed response"


def test_real_tool_work_between_continues_resets_the_ping_pong_bound():
    """Two SEPARATE continue decisions, each followed by real tool work in
    between, must NOT trip the ping-pong bound -- only a continue
    immediately contradicted by another no-tool round is bounded."""
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(CONTINUE_TOOL_WORK_NAME)]},
        {"tool_calls": [_tc("read_file")]},                    # real work -- resets the bound
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(CONTINUE_TOOL_WORK_NAME)]},         # a SECOND continue -- must still be granted
        {"tool_calls": [_tc("search_memory")]},                 # more real work
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "find and read it, iteratively")

    assert result == "final streamed response"
    assert calls["registry_calls"] == ["search_memory", "read_file", "search_memory"]


# ── Ephemeral non-pollution (Section 7, 10) ──────────────────────────────

def test_gate_ephemeral_instruction_does_not_leak_into_final_stream_messages():
    """The gate's own ephemeral instruction is consumed by ITS OWN
    build_messages() call; _stream_final() must be built from a separate,
    later build_messages() call so the instruction never reaches the final
    stream's system prompt."""
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    LuminaAgent.chat(fake, "find it")

    # The fake's push_ephemeral records every push made -- exactly one,
    # the gate's own instruction, and it is never pushed again afterward
    # (i.e. never re-pushed right before the final stream).
    assert len(calls["ephemeral"]) == 1
    assert "completion gate" in calls["ephemeral"][0]
    # build_messages() was called for: WORK round 1, WORK round 2 (gate
    # trigger), the gate's own request, AND a final clean build for
    # _stream_final() -- four calls, not a reuse of the gate's own build.
    assert calls["build_messages_calls"] == 4


def test_continue_handoff_ephemeral_is_not_re_pushed_on_the_following_gate_call():
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(CONTINUE_TOOL_WORK_NAME)]},
        {"tool_calls": [_tc("read_file")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    LuminaAgent.chat(fake, "find and read it")

    # Exactly 3 ephemeral pushes: the first gate call's instruction, the
    # continue-handoff nudge for the next WORK round, and the second gate
    # call's own (fresh) instruction -- never a stale duplicate.
    assert len(calls["ephemeral"]) == 3
    assert "completion gate" in calls["ephemeral"][0]
    assert "continue" in calls["ephemeral"][1].lower()
    assert "completion gate" in calls["ephemeral"][2]


# ── Cancellation around the gate (Section 26 W/X) ────────────────────────

def test_cancel_during_gates_own_provider_call_raises_turn_cancelled():
    import threading
    import pytest
    from core.agent import TurnCancelled

    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)
    event = threading.Event()

    real_chat = llm.chat

    def chat_and_maybe_cancel(messages, tools=None, max_tokens=None, reasoning_effort=None,
                               tool_choice_mode=None):
        response = real_chat(messages, tools=tools, max_tokens=max_tokens,
                              reasoning_effort=reasoning_effort, tool_choice_mode=tool_choice_mode)
        if llm.call_count == 3:  # the gate's own request just returned
            event.set()
        return response

    llm.chat = chat_and_maybe_cancel

    with pytest.raises(TurnCancelled):
        LuminaAgent.chat(fake, "find it", cancel_event=event)

    assert llm.call_count == 3
    # Never reached _stream_final() -- no persisted "final streamed
    # response" content anywhere in ctx.history from this turn.
    assert not any(
        isinstance(h, dict) and h.get("content") == "final streamed response"
        for h in fake.ctx.history
    )


def test_cancel_before_finish_transition_prevents_stream_final():
    import threading
    import pytest
    from core.agent import TurnCancelled

    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)
    event = threading.Event()

    real_is_tool_call = llm.is_tool_call

    def is_tool_call_then_maybe_cancel(message):
        result = real_is_tool_call(message)
        if llm.call_count == 3:  # the gate's response is being processed
            event.set()
        return result

    llm.is_tool_call = is_tool_call_then_maybe_cancel

    with pytest.raises(TurnCancelled):
        LuminaAgent.chat(fake, "find it", cancel_event=event)

    assert not any(
        isinstance(h, dict) and h.get("content") == "final streamed response"
        for h in fake.ctx.history
    )


def test_provider_exception_during_gate_call_stays_a_provider_exception():
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"raise": RuntimeError("OpenRouter error (generic, HTTP 400): tool_choice rejected")},
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "find it")

    assert "rejected the continuation" in result
    assert "search_memory" in result
    assert not result.startswith("[Lumina: tool-work continuation ended")
