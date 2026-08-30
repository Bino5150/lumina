"""
AGENT-COMMENTARY-01A -- First-Class Tool-Phase Commentary Timeline.

Source-vetting (see the task-block report) confirmed core/agent.py's
_chat_impl() already preserves message["content"] alongside tool_calls
through strip_think_blocks() before persisting the assistant message via
ctx.add_tool_call() -- it was simply never surfaced to a callback. This
file exercises the new on_commentary(text) callback: a UI observation of
that already-preserved content, emitted once per provider response that
carries it, strictly before the tool call(s) (or finish_tool_work) that
response describes are acted on.

Design law under test:
  - A commentary event exists only when a provider response contains
    structured tool_call(s) (real, or finish_tool_work) AND non-empty
    outward content after <think> stripping. No prose heuristic.
  - Commentary fires exactly once per response, before on_tool_call for
    any real tool in that response, and before finish_tool_work is acted
    on as the internal sentinel it is.
  - Ambiguous post-tool prose (no tool_calls, no finish_tool_work) and an
    INCOMPLETE-terminated initial response are explicitly NOT commentary
    -- they remain AGENT-CONTINUATION-01A's existing corrective-retry /
    non-success paths, unchanged.
  - Commentary never touches _session_tool_calls, never duplicates the
    persisted ctx history entry, and a provider exception fabricates zero
    commentary.

Reuses the types.SimpleNamespace fake-agent pattern from
tests/test_agent_continuation_contract.py (read directly before writing
this file) -- LuminaAgent.chat(fake_self, ...) called unbound against a
minimal stand-in, with _stream_final bound on as a real method so the
success path genuinely streams rather than being mocked away.
"""
import threading
import types

import pytest

from core.agent import LuminaAgent, TurnCancelled, FINISH_TOOL_WORK_NAME
from core.backends.base import TerminationStatus


def _tc(name, call_id=None):
    return {"id": call_id or name, "type": "function",
            "function": {"name": name, "arguments": "{}"}}


class _ScriptedLLM:
    """Same scripted-turn fake as test_agent_continuation_contract.py's
    _ScriptedLLM: one dict per non-streaming chat() call --
      {"tool_calls": [...], "content": "..."}   -- real and/or sentinel calls,
                                                     content optional (commentary)
      {"content": "...", "termination": TerminationStatus.X}  -- no tool_calls
      {"raise": SomeException(...)}             -- chat() raises
    """
    display_name = "FakeProvider"
    name = "fake"

    def __init__(self, turns):
        self.turns = list(turns)
        self.call_count = 0
        self.tools_seen = []

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
        "on_tool_result": [], "response_tokens": [], "commentary": [],
    }

    def registry_call(name, args):
        calls["registry_calls"].append(name)
        return tool_result

    def on_commentary(text):
        calls["commentary"].append(text)

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
        on_commentary=on_commentary,
        tts=None,
        _session_tool_calls=0,
        _skill_nudge_sent=False,
    )
    ns._stream_final = types.MethodType(LuminaAgent._stream_final, ns)
    ns._finalize_completion_candidate = types.MethodType(LuminaAgent._finalize_completion_candidate, ns)
    return ns, calls


# ── A. Ordinary no-tool answer ──────────────────────────────────────────

def test_A_ordinary_no_tool_answer_emits_zero_commentary():
    llm = _ScriptedLLM([{"content": "hi there", "termination": TerminationStatus.COMPLETE}])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "hello")

    assert result == "final streamed response"
    assert calls["commentary"] == []


# ── B. One tool + commentary ────────────────────────────────────────────

def test_B_one_tool_with_commentary_fires_once_before_tool_call():
    llm = _ScriptedLLM([
        {"content": "Checking memory for that.", "tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)
    order = []
    real_on_commentary = fake.on_commentary
    real_on_tool_call = fake.on_tool_call
    fake.on_commentary = lambda text: (order.append(("commentary", text)), real_on_commentary(text))
    fake.on_tool_call = lambda name, args: (order.append(("tool_call", name)), real_on_tool_call(name, args))

    result = LuminaAgent.chat(fake, "find it")

    assert result == "final streamed response"
    assert calls["commentary"] == ["Checking memory for that."]
    assert order == [("commentary", "Checking memory for that."), ("tool_call", "search_memory")]


# ── C. Multiple tools in one response ───────────────────────────────────

def test_C_multi_tool_response_emits_commentary_exactly_once():
    llm = _ScriptedLLM([
        {"content": "Doing three things at once.",
         "tool_calls": [_tc("tool_a"), _tc("tool_b"), _tc("tool_c")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "do three things")

    assert result == "final streamed response"
    assert calls["commentary"] == ["Doing three things at once."]
    assert calls["registry_calls"] == ["tool_a", "tool_b", "tool_c"]
    assert len(calls["on_tool_call"]) == 3


# ── D/E. Empty / whitespace-only content ────────────────────────────────

def test_D_empty_content_with_tool_emits_no_commentary():
    llm = _ScriptedLLM([
        {"content": "", "tool_calls": [_tc("search_memory")]},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    LuminaAgent.chat(fake, "find it")

    assert calls["commentary"] == []


def test_E_whitespace_only_content_with_tool_emits_no_commentary():
    llm = _ScriptedLLM([
        {"content": "   \n\t  ", "tool_calls": [_tc("search_memory")]},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    LuminaAgent.chat(fake, "find it")

    assert calls["commentary"] == []


# ── F. Embedded <think> block stripped from commentary ──────────────────

def test_F_embedded_think_block_stripped_outward_content_preserved():
    llm = _ScriptedLLM([
        {"content": "<think>internal deliberation, never shown</think>Checking the file now.",
         "tool_calls": [_tc("read_file")]},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    LuminaAgent.chat(fake, "read it")

    assert calls["commentary"] == ["Checking the file now."]
    assert "internal deliberation" not in calls["commentary"][0]
    assert "<think>" not in calls["commentary"][0]


# ── G. Commentary + finish_tool_work ─────────────────────────────────────

def test_G_commentary_with_finish_tool_work_emits_then_streams_final():
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"content": "That confirms it. I have enough evidence now.",
         "tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "find it")

    assert result == "final streamed response"
    assert calls["commentary"] == ["That confirms it. I have enough evidence now."]
    # The sentinel-bearing message itself was never persisted or dispatched.
    assert calls["registry_calls"] == ["search_memory"]
    assert FINISH_TOOL_WORK_NAME not in calls["registry_calls"]
    assert not any(
        isinstance(h, dict) and h.get("role") == "tool" and h.get("name") == FINISH_TOOL_WORK_NAME
        for h in fake.ctx.history
    )


# ── G2. Embedded <think> block stripped on the finish_tool_work path ─────

def test_G2_finish_tool_work_commentary_also_strips_embedded_think_block():
    """G above covers finish_tool_work + plain commentary; this covers the
    same <think>-stripping guarantee F already proves for the real-tool-call
    path, but on _extract_commentary()'s own code path (the finish_tool_work
    branch reads through _extract_commentary() rather than the inline
    strip_think_blocks() call the real-tool-call branch uses) -- the two
    branches must not silently diverge on this."""
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"content": "<think>weighing options</think>That confirms it.",
         "tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    LuminaAgent.chat(fake, "find it")

    assert calls["commentary"] == ["That confirms it."]
    assert "weighing options" not in calls["commentary"][0]
    assert "<think>" not in calls["commentary"][0]


# ── H. Ambiguous post-tool prose is NOT commentary ───────────────────────

def test_H_ambiguous_post_tool_prose_is_not_commentary():
    """AGENT-WORK-COMPLETE-DISCARD-01 -- "let me think about that" is a
    preserved completion candidate (COMPLETE, zero tool_calls, in tool work
    phase), promoted directly once the gate confirms finish_tool_work --
    NOT regenerated via chat_stream() (which would have produced the fake's
    fixed "final streamed response" sentinel instead). Still never
    Commentary, which is this test's actual point: a candidate answer is
    not outward tool-decision narration merely for having arrived mid-WORK."""
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "let me think about that", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "find it")

    assert result == "let me think about that"
    # The corrective-retry contract (AGENT-CONTINUATION-01A) still fired --
    # proves this path was reached, not skipped.
    assert len(calls["ephemeral"]) == 1
    assert "finish_tool_work" in calls["ephemeral"][0]
    # But the ambiguous prose itself was never treated as commentary.
    assert calls["commentary"] == []


# ── I. INCOMPLETE no-tool initial response is NOT commentary ────────────

def test_I_incomplete_initial_response_is_not_commentary():
    llm = _ScriptedLLM([
        {"content": "cut off mid-", "termination": TerminationStatus.INCOMPLETE},
        {"content": "complete now", "termination": TerminationStatus.COMPLETE},
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "hello")

    assert result == "final streamed response"
    assert len(calls["ephemeral"]) == 1
    assert calls["commentary"] == []


# ── J. Provider exception fabricates zero commentary ─────────────────────

def test_J_provider_exception_emits_zero_commentary():
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"raise": RuntimeError("provider exploded")},
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "find it")

    assert "rejected the continuation" in result
    assert calls["commentary"] == []
    assert calls["response_tokens"] == [result]


# ── K. Cancellation right after commentary, before tool dispatch ─────────

def test_K_cancellation_after_commentary_before_tool_dispatch():
    cancel_event = threading.Event()
    llm = _ScriptedLLM([
        {"content": "About to check memory.", "tool_calls": [_tc("search_memory")]},
    ])
    fake, calls = _fake_agent(llm)

    # Simulate the operator hitting /stop in the narrow window right after
    # commentary renders but before the tool it describes actually runs.
    real_on_commentary = fake.on_commentary
    fake.on_commentary = lambda text: (real_on_commentary(text), cancel_event.set())

    with pytest.raises(TurnCancelled):
        LuminaAgent.chat(fake, "find it", cancel_event=cancel_event)

    # The commentary that was already, truthfully, emitted stays emitted.
    assert calls["commentary"] == ["About to check memory."]
    # But the tool it described never actually dispatched.
    assert calls["on_tool_call"] == []
    assert calls["registry_calls"] == []


# ── L. Commentary never touches session tool-call accounting ─────────────

def test_L_commentary_does_not_affect_session_tool_calls():
    llm = _ScriptedLLM([
        {"content": "Running the search.", "tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    LuminaAgent.chat(fake, "find it")

    # Exactly one real tool ran -- commentary itself contributed nothing.
    assert fake._session_tool_calls == 1


# ── M. Commentary is not a synthetic duplicate history entry ─────────────

def test_M_commentary_not_duplicated_in_ctx_history():
    llm = _ScriptedLLM([
        {"content": "Running the search.", "tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    LuminaAgent.chat(fake, "find it")

    matches = [
        h for h in fake.ctx.history
        if isinstance(h, dict) and "Running the search." in str(h.get("content", ""))
    ]
    # Present exactly once -- the real tool-call assistant message itself --
    # never a second synthetic user/assistant/system entry echoing it back.
    assert len(matches) == 1
    assert matches[0]["role"] == "assistant"
    assert matches[0].get("tool_calls")
