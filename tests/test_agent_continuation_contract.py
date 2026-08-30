"""
AGENT-CONTINUATION-01A -- Explicit Tool-Work Completion Contract.

Prior to this fix, core/agent.py's _chat_impl() inferred "tool work is
finished" purely from bool(message.get("tool_calls")) -- absence of a tool
call was treated as proof of completion, with no independent signal. Live
verification against the real OpenRouter/GLM path (AGENT-CONTINUATION-01)
showed this collapses a genuinely-finished answer and a truncated/aborted
mid-task response into the exact same code path, discarding intended tool
work silently.

This file exercises the replacement state machine end to end:
  - finish_tool_work: an explicit, provider-neutral completion signal,
    offered only once real tool work has begun this turn, that never
    touches ToolRegistry/callbacks/accounting (isolation contract).
  - TerminationStatus (core/backends/base.py): distinguishes a positively
    truncated generation from a clean stop, consumed only to gate the
    initial (pre-tool-work) round.
  - Exactly one bounded corrective retry per turn when a continuation
    response is ambiguous (no real tool call, no finish_tool_work), then a
    visible non-success result rather than a silently laundered "final"
    answer -- Design Law: silence is not completion.

Uses the same types.SimpleNamespace fake-agent pattern established in
tests/test_agent_tool_continuation.py and
tests/test_reasoning_agent_propagation.py (read directly before writing
this file) -- LuminaAgent.chat(fake_self, ...) called unbound against a
minimal stand-in, with _stream_final bound on as a real method so the
success path genuinely streams rather than being mocked away.
"""
import types

from core.agent import LuminaAgent, FINISH_TOOL_WORK_NAME, CONTINUE_TOOL_WORK_NAME
from core.backends.base import TerminationStatus


def _tc(name, call_id=None):
    return {"id": call_id or name, "type": "function",
            "function": {"name": name, "arguments": "{}"}}


class _ScriptedLLM:
    """Fake backend driven by a list of scripted turns, one per non-
    streaming chat() call:
      {"tool_calls": [...]}                       -- real and/or sentinel calls
      {"content": "...", "termination": TerminationStatus.X}  -- no tool_calls
      {"raise": SomeException(...)}                -- chat() raises

    is_tool_call/get_tool_calls/parse_tool_call mirror the real
    BaseLLMBackend concrete defaults exactly (bool(tool_calls), etc.), and
    extract_termination is genuinely implemented (not defaulted away) so
    these tests exercise the same contract real backends do, not a weaker
    stand-in of it.
    """
    display_name = "FakeProvider"
    name = "fake"

    def __init__(self, turns):
        self.turns = list(turns)
        self.call_count = 0
        self.tools_seen = []   # tool-name lists offered on each chat() call

    def get_model(self):
        return "fake-model"

    def chat(self, messages, tools=None, max_tokens=None, reasoning_effort=None):
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
        return turn.get("termination", TerminationStatus.UNKNOWN)

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
        "on_tool_result": [], "response_tokens": [],
    }

    def registry_call(name, args):
        calls["registry_calls"].append(name)
        return tool_result

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
        build_messages=lambda tool_budget=0, chat_id=None: [],
    )
    registry = types.SimpleNamespace(
        schema_token_estimate=lambda: 0,
        get_schemas=lambda: [],
        list_enabled=lambda: [],
        all_tool_names=lambda: [],
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
        tts=None,
        _session_tool_calls=0,
        _skill_nudge_sent=False,
    )
    ns._stream_final = types.MethodType(LuminaAgent._stream_final, ns)
    ns._finalize_completion_candidate = types.MethodType(LuminaAgent._finalize_completion_candidate, ns)
    return ns, calls


# ── A/B. Initial round ──────────────────────────────────────────────────

def test_A_initial_no_tool_complete_streams_final_normally():
    llm = _ScriptedLLM([{"content": "hi there", "termination": TerminationStatus.COMPLETE}])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "hello")

    assert result == "final streamed response"
    assert llm.call_count == 1
    # Sentinel never offered before any tool has run.
    assert llm.tools_seen == [[]]


def test_B_initial_no_tool_incomplete_does_not_silently_finalize():
    llm = _ScriptedLLM([
        {"content": "cut off mid-", "termination": TerminationStatus.INCOMPLETE},
        {"content": "complete now", "termination": TerminationStatus.COMPLETE},
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "hello")

    # One corrective retry, then a genuine final stream -- never finalized
    # on the truncated first response.
    assert llm.call_count == 2
    assert len(calls["ephemeral"]) == 1
    assert result == "final streamed response"


# ── C/D/E. Tool-work completion ─────────────────────────────────────────

def test_C_tool_then_finish_tool_work_streams_final_exactly_once():
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "find it")

    assert result == "final streamed response"
    assert llm.call_count == 3
    assert calls["registry_calls"] == ["search_memory"]
    # The two internal control primitives are offered ONLY at the gate --
    # never on a WORK round, real-tool or not.
    assert FINISH_TOOL_WORK_NAME not in llm.tools_seen[0]
    assert FINISH_TOOL_WORK_NAME not in llm.tools_seen[1]
    assert FINISH_TOOL_WORK_NAME in llm.tools_seen[2]
    assert CONTINUE_TOOL_WORK_NAME in llm.tools_seen[2]


def test_D_two_tools_then_finish_tool_work_both_run_then_final():
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("tool_a")]},
        {"tool_calls": [_tc("tool_b")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "do two things")

    assert result == "final streamed response"
    assert calls["registry_calls"] == ["tool_a", "tool_b"]
    assert llm.call_count == 4


def test_E_tool_result_then_another_real_tool_remains_in_tool_work_phase():
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("tool_a")]},
        {"tool_calls": [_tc("tool_b")]},
    ])
    fake, calls = _fake_agent(llm)

    # Further calls would raise if reached with no scripted turn -- proves
    # the loop is still correctly mid-tool-work after two real tool calls
    # rather than having (wrongly) finalized already. We stop it here by
    # scripting a no-tool round (triggers the gate) then a finish so the
    # turn can complete for the assertion below.
    llm.turns.append({"content": "", "termination": TerminationStatus.COMPLETE})
    llm.turns.append({"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]})

    result = LuminaAgent.chat(fake, "chain two tools")

    assert calls["registry_calls"] == ["tool_a", "tool_b"]
    assert result == "final streamed response"


# ── F/G/H/I. Ambiguous continuation + corrective retry ─────────────────

def test_F_no_tool_no_sentinel_triggers_exactly_one_corrective_retry():
    """AGENT-WORK-COMPLETE-DISCARD-01 -- "let me think about that" is a
    preserved completion candidate; once the gate confirms finish_tool_work
    it is promoted directly rather than regenerated via chat_stream() (which
    would have produced the fake's fixed "final streamed response"
    sentinel instead). llm.call_count == 3 (unchanged) already proves no
    extra provider call happened for the final answer."""
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "let me think about that", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "find it")

    assert result == "let me think about that"
    assert llm.call_count == 3
    assert len(calls["ephemeral"]) == 1
    assert "finish_tool_work" in calls["ephemeral"][0]


def test_G_corrective_retry_then_finish_tool_work_streams_final():
    """AGENT-WORK-COMPLETE-DISCARD-01 -- "hmm" is a genuine (if terse)
    complete answer: COMPLETE termination, zero tool_calls, in tool work
    phase. Once the gate confirms finish_tool_work, that preserved
    candidate becomes the final answer directly -- it is NOT thrown away
    in favor of a fresh chat_stream() regeneration (which would have
    produced the fake's fixed "final streamed response" sentinel instead)."""
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "hmm", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "find it")

    assert result == "hmm"


def test_H_gate_continue_then_real_tool_executes_and_continues():
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "hmm", "termination": TerminationStatus.COMPLETE},  # WORK -> gate
        {"tool_calls": [_tc(CONTINUE_TOOL_WORK_NAME)]},                  # GATE -> continue
        {"tool_calls": [_tc("read_file")]},                              # WORK (restored) -> real tool
        {"content": "", "termination": TerminationStatus.COMPLETE},      # WORK -> gate
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},                    # GATE -> finish
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "find and read it")

    assert calls["registry_calls"] == ["search_memory", "read_file"]
    assert result == "final streamed response"


def test_I_second_ambiguous_gate_response_after_retry_is_visible_non_success():
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "hmm", "termination": TerminationStatus.COMPLETE},        # WORK -> gate
        {"content": "still hmm", "termination": TerminationStatus.COMPLETE},  # GATE -> malformed, 1 retry spent
        {"content": "still hmm again", "termination": TerminationStatus.COMPLETE},  # GATE retry -> malformed, budget gone
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "find it")

    assert llm.call_count == 4
    assert len(calls["ephemeral"]) == 2  # the gate's own instruction, then its one retry nudge
    assert "confirming completion" in result
    assert result.startswith("[Lumina:")
    # Streamed live, same convention as the provider-continuation-failure
    # path, but never routed through chat_stream()/_stream_final().
    assert calls["response_tokens"] == [result]
    assert "final streamed response" not in result


# ── J/K/L. Truncation ────────────────────────────────────────────────────

def test_J_incomplete_continuation_after_tool_never_finalizes_silently():
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "The answer is", "termination": TerminationStatus.INCOMPLETE},  # WORK -> truncation retry
        {"content": "", "termination": TerminationStatus.COMPLETE},                  # WORK retry -> gate
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},                                # GATE -> finish
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "find it")

    assert llm.call_count == 4
    assert result == "final streamed response"


def test_K_multiple_tools_then_incomplete_never_finalizes_silently():
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("tool_a")]},
        {"tool_calls": [_tc("tool_b")]},
        {"content": "partial", "termination": TerminationStatus.INCOMPLETE},
        {"content": "still partial", "termination": TerminationStatus.INCOMPLETE},
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "chain two then stop")

    # Never streams "final streamed response" -- the retry is spent and the
    # second ambiguous/incomplete response surfaces the visible notice.
    assert result != "final streamed response"
    assert result.startswith("[Lumina:")


def test_L_initial_generation_incomplete_cannot_silently_finalize():
    llm = _ScriptedLLM([
        {"content": "The an", "termination": TerminationStatus.INCOMPLETE},
        {"content": "still cut", "termination": TerminationStatus.INCOMPLETE},
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "hello")

    assert result != "final streamed response"
    assert result.startswith("[Lumina:")
    assert "cut off" in result


# ── M-S. Control primitive isolation ─────────────────────────────────────

def test_MNPQR_finish_tool_work_never_touches_registry_callbacks_or_accounting():
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)
    fake._session_tool_calls = 0

    LuminaAgent.chat(fake, "find it")

    # M: never dispatched through registry.call()
    assert FINISH_TOOL_WORK_NAME not in calls["registry_calls"]
    assert calls["registry_calls"] == ["search_memory"]
    # N/O: never surfaced through the tool-call/tool-result UI callbacks
    assert all(name != FINISH_TOOL_WORK_NAME for name, _ in calls["on_tool_call"])
    assert all(name != FINISH_TOOL_WORK_NAME for name, _ in calls["on_tool_result"])
    # P: accounting only reflects the one real tool call
    assert fake._session_tool_calls == 1
    # R: no ordinary persisted tool-result row for the sentinel
    assert not any(
        isinstance(h, dict) and h.get("role") == "tool" and h.get("name") == FINISH_TOOL_WORK_NAME
        for h in fake.ctx.history
    )
    # The whole finish_tool_work-bearing response is discarded, same as an
    # ordinary final turn -- never persisted via add_tool_call either.
    assert not any(
        isinstance(h, dict) and h.get("role") == "assistant" and h.get("tool_calls")
        and any(tc.get("function", {}).get("name") == FINISH_TOOL_WORK_NAME
                for tc in h.get("tool_calls", []))
        for h in fake.ctx.history
    )


def test_S_sentinel_constructed_locally_not_via_registry_enumeration():
    llm = _ScriptedLLM([
        {"content": "hi", "termination": TerminationStatus.COMPLETE},
    ])
    fake, calls = _fake_agent(llm)

    LuminaAgent.chat(fake, "hello")

    # registry.get_schemas() itself never needs to know about the
    # sentinel -- it stays an empty/unaffected fixture throughout; the
    # ordinary first round never even sees it offered.
    assert FINISH_TOOL_WORK_NAME not in llm.tools_seen[0]


def test_Q_skill_nudge_not_triggered_by_finish_tool_work_alone(monkeypatch):
    import core.agent as agent_module
    monkeypatch.setattr(agent_module.config, "SKILLS_TRIGGER_THRESHOLD", 1)
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    LuminaAgent.chat(fake, "find it")

    # One real tool call did cross the threshold -- the nudge firing here
    # is attributable to that tool call's accounting, not to
    # finish_tool_work independently contributing to _session_tool_calls.
    assert fake._session_tool_calls == 1


# ── T/U. Distinguishable from provider-exception failures ──────────────

def test_TU_anomaly_notice_is_textually_distinct_from_provider_exception_notice():
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "hmm", "termination": TerminationStatus.COMPLETE},
        {"content": "still hmm", "termination": TerminationStatus.COMPLETE},
        {"content": "still hmm again", "termination": TerminationStatus.COMPLETE},
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "find it")

    assert "rejected the continuation" not in result
    assert result.startswith("[Lumina: tool-work continuation ended")


# ── V/W/X. Cancellation around the new branches ─────────────────────────

def test_V_cancel_before_control_gate_entry_raises_turn_cancelled():
    """Isolates the specific cancellation check guarding entry into the
    completion-control gate (added right before `next_action = "gate"` in
    core/agent.py) from the pre-existing check immediately after
    self.llm.chat() returns. Flips the event inside is_tool_call() -- which
    runs strictly after that pre-existing check and strictly before the new
    one -- so only the new check can be what raises here."""
    import threading
    import pytest
    from core.agent import TurnCancelled

    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "hmm", "termination": TerminationStatus.COMPLETE},
    ])
    fake, calls = _fake_agent(llm)
    event = threading.Event()

    real_is_tool_call = llm.is_tool_call

    def is_tool_call_then_maybe_cancel(message):
        result = real_is_tool_call(message)
        if llm.call_count == 2:  # the WORK round about to trigger the gate
            event.set()
        return result

    llm.is_tool_call = is_tool_call_then_maybe_cancel

    with pytest.raises(TurnCancelled):
        LuminaAgent.chat(fake, "find it", cancel_event=event)

    # Never reached the control gate's own ephemeral push or a third call.
    assert calls["ephemeral"] == []
    assert llm.call_count == 2


def test_W_cancel_during_corrective_retrys_own_provider_call_raises_turn_cancelled():
    """The corrective retry is implemented as `continue` back to the top of
    the SAME loop -- so a cancellation arriving during the retry's own
    self.llm.chat() call is caught by the loop's pre-existing post-chat()
    check with zero new code needed. Proves that inheritance actually
    holds for the retry round specifically (call_count == 2), not just for
    an ordinary first-round call."""
    import threading
    import pytest
    from core.agent import TurnCancelled

    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "hmm", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)
    event = threading.Event()

    real_chat = llm.chat

    def chat_and_maybe_cancel(messages, tools=None, max_tokens=None, reasoning_effort=None):
        response = real_chat(messages, tools=tools, max_tokens=max_tokens,
                              reasoning_effort=reasoning_effort)
        if llm.call_count == 3:  # the corrective-retry round's own request
            event.set()
        return response

    llm.chat = chat_and_maybe_cancel

    with pytest.raises(TurnCancelled):
        LuminaAgent.chat(fake, "find it", cancel_event=event)

    assert llm.call_count == 3
    assert len(calls["ephemeral"]) == 1  # the retry was in fact entered


def test_X_cancel_before_sentinel_transition_raises_turn_cancelled():
    import threading
    import pytest
    from core.agent import TurnCancelled

    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)
    event = threading.Event()

    real_chat = llm.chat

    def chat_and_maybe_cancel(messages, tools=None, max_tokens=None, reasoning_effort=None):
        response = real_chat(messages, tools=tools, max_tokens=max_tokens,
                              reasoning_effort=reasoning_effort)
        if llm.call_count == 2:
            # Cancellation lands right as the finish_tool_work-bearing
            # response comes back, before the harness acts on it.
            event.set()
        return response

    llm.chat = chat_and_maybe_cancel

    with pytest.raises(TurnCancelled):
        LuminaAgent.chat(fake, "find it", cancel_event=event)

    # Never reached _stream_final() -- no "final streamed response" text
    # anywhere in ctx.history from this turn.
    assert not any(
        isinstance(h, dict) and h.get("content") == "final streamed response"
        for h in fake.ctx.history
    )
