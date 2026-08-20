"""
Patch 3A.4 Part 3 -- agent-level reasoning_effort turn propagation.

Covers the plumbing seam threaded through this patch:
    LuminaAgent.chat(..., reasoning_effort=...)
        -> _chat_impl(...)
            -> every self.llm.chat(...) call in the tool loop
            -> self._stream_final(..., reasoning_effort=...)
                -> self.llm.chat_stream(...)

The SAME per-turn value must reach every provider call made during that
turn -- the initial request, every tool-continuation, and the final
stream -- with zero recomputation/reassignment in between, and zero
leakage across separate turns on the same agent object.

Follows the existing types.SimpleNamespace fake-agent pattern from
tests/test_agent_tool_continuation.py's _fake_agent() helper (read
directly before writing this file) rather than inventing a new one --
LuminaAgent.chat(fake_self, ...) is called UNBOUND against a minimal
stand-in, exactly like every other test in that file.

These tests inspect the exact list of reasoning_effort values the fake
backend actually observed on each call -- never a mocked-call assertion
alone.
"""
import types

import config
from core.agent import LuminaAgent


class _RecordingLLM:
    """Fake backend: records the exact reasoning_effort observed on every
    chat()/chat_stream() call it receives, in call order. tool_call_rounds
    controls how many non-streaming chat() calls return a tool call before
    the loop is allowed to fall through to a tool-free final message (or,
    if tool_call_rounds is large enough relative to a monkeypatched
    config.MAX_TOOL_ITERATIONS, never falls through at all -- exercising
    the max-iteration forced-final path in core/agent.py instead)."""

    def __init__(self, tool_call_rounds=0, tool_name="search_memory"):
        self.tool_call_rounds = tool_call_rounds
        self.tool_name = tool_name
        self.chat_calls = []    # reasoning_effort observed per chat() call
        self.stream_calls = []  # reasoning_effort observed per chat_stream() call
        self._chat_count = 0

    def get_model(self):
        return "test-model"

    def chat(self, messages, tools=None, max_tokens=None, reasoning_effort=None):
        self.chat_calls.append(reasoning_effort)
        self._chat_count += 1
        if self._chat_count <= self.tool_call_rounds:
            return {"_round": self._chat_count}
        return {"_final": True}

    def extract_message(self, response):
        if "_round" in response:
            n = response["_round"]
            return {
                "role": "assistant", "content": "",
                "tool_calls": [{"id": f"c{n}", "type": "function",
                                 "function": {"name": self.tool_name, "arguments": "{}"}}],
            }
        return {"role": "assistant", "content": "no tools needed"}

    def is_tool_call(self, message):
        return bool(message.get("tool_calls"))

    def get_tool_calls(self, message):
        return message.get("tool_calls", [])

    def parse_tool_call(self, tc):
        return tc["function"]["name"], {}

    def chat_stream(self, messages, max_tokens=None, reasoning_effort=None):
        self.stream_calls.append(reasoning_effort)
        yield "final streamed response"


def _fake_agent(llm, tool_result="tool ran fine"):
    """Same shape as test_agent_tool_continuation.py's _fake_agent(), plus
    the extra ctx.add_assistant/tts/on_think_* attributes _stream_final()
    needs on its success path (that file's fakes never reach chat_stream()
    at all, since every one of its scenarios raises before getting there)."""
    history = []

    def add_user(content, source="OWNER_DIRECT"):
        history.append({"role": "user", "content": content})

    def add_assistant(content):
        history.append({"role": "assistant", "content": content})

    ns = types.SimpleNamespace(
        llm=llm,
        ctx=types.SimpleNamespace(
            history=history,
            add_user=add_user,
            add_assistant=add_assistant,
            push_ephemeral=lambda block: None,
            build_messages=lambda tool_budget=0, chat_id=None: [],
            add_tool_call=lambda message: None,
            add_tool_result=lambda tool_call_id, name, result: None,
        ),
        registry=types.SimpleNamespace(
            schema_token_estimate=lambda: 0,
            get_schemas=lambda: [],
            list_enabled=lambda: [],
            call=lambda name, args: tool_result,
        ),
        on_tool_call=lambda name, args: None,
        on_tool_result=lambda name, result: None,
        on_response_token=lambda tok: None,
        on_think_start=lambda step: None,
        on_think_token=lambda tok: None,
        on_think_end=lambda: None,
        tts=None,
        _session_tool_calls=0,
        _skill_nudge_sent=False,
    )
    # chat() calls self._stream_final(...) as a bound method (same pattern
    # confirmed in tests/test_background_task_notify.py's _fake_agent()) --
    # a bare SimpleNamespace has no such attribute, so bind the real
    # unbound implementation onto this fake instance.
    ns._stream_final = types.MethodType(LuminaAgent._stream_final, ns)
    return ns


# ── A. No-tool turn ──────────────────────────────────────────────────────

def test_no_tool_turn_forwards_reasoning_effort_to_final_stream():
    llm = _RecordingLLM(tool_call_rounds=0)
    fake_self = _fake_agent(llm)

    result = LuminaAgent.chat(fake_self, "hello", reasoning_effort="high")

    assert llm.chat_calls == ["high"]
    assert llm.stream_calls == ["high"]
    assert result == "final streamed response"


# ── B. Tool turn ─────────────────────────────────────────────────────────

def test_tool_turn_forwards_reasoning_effort_to_every_call():
    """First chat() returns a tool call, the continuation chat() runs
    after the tool result, then the final chat_stream() runs -- all three
    observed calls must carry the same turn value, not just the last one."""
    llm = _RecordingLLM(tool_call_rounds=1)
    fake_self = _fake_agent(llm)

    result = LuminaAgent.chat(fake_self, "do something", reasoning_effort="high")

    assert llm.chat_calls == ["high", "high"]
    assert llm.stream_calls == ["high"]
    assert result == "final streamed response"


# ── C. Provider Default (None) ───────────────────────────────────────────

def test_provider_default_none_propagates_through_tool_loop_and_stream():
    llm = _RecordingLLM(tool_call_rounds=1)
    fake_self = _fake_agent(llm)

    LuminaAgent.chat(fake_self, "do something", reasoning_effort=None)

    assert llm.chat_calls == [None, None]
    assert llm.stream_calls == [None]


def test_omitting_reasoning_effort_entirely_defaults_to_none():
    """Legacy-callers path: not passing reasoning_effort at all must behave
    identically to passing None explicitly (default preserves current
    behavior for every existing caller)."""
    llm = _RecordingLLM(tool_call_rounds=0)
    fake_self = _fake_agent(llm)

    LuminaAgent.chat(fake_self, "hello")

    assert llm.chat_calls == [None]
    assert llm.stream_calls == [None]


# ── D. Max-iteration finalization ────────────────────────────────────────

def test_max_iteration_forced_final_forwards_the_same_turn_value(monkeypatch):
    """LLM always returns a tool call, so config.MAX_TOOL_ITERATIONS
    (monkeypatched small) is exhausted and _chat_impl() falls through to
    the 'Give your final answer now' forced-final path -- which must still
    forward this turn's reasoning_effort into its own _stream_final() call,
    exactly like the ordinary no-tool-call path does."""
    monkeypatch.setattr(config, "MAX_TOOL_ITERATIONS", 2)
    llm = _RecordingLLM(tool_call_rounds=999)  # never stops returning tool calls
    fake_self = _fake_agent(llm)

    result = LuminaAgent.chat(fake_self, "loop forever", reasoning_effort="high")

    # Exactly MAX_TOOL_ITERATIONS non-streaming calls (loop exhausted),
    # then exactly one final stream call -- all carrying "high".
    assert llm.chat_calls == ["high", "high"]
    assert llm.stream_calls == ["high"]
    assert result == "final streamed response"


# ── E. No cross-turn state ───────────────────────────────────────────────

def test_no_cross_turn_reasoning_state_leakage():
    """Turn 1 with an explicit effort, turn 2 (fresh chat() call on the
    SAME fake agent object / same backend instance) with None -- turn 2
    must show None, never a leftover 'high' from turn 1. Proves
    reasoning_effort is genuinely per-call state, not cached anywhere on
    the agent or the backend."""
    llm = _RecordingLLM(tool_call_rounds=0)
    fake_self = _fake_agent(llm)

    LuminaAgent.chat(fake_self, "turn one", reasoning_effort="high")
    assert llm.chat_calls == ["high"]
    assert llm.stream_calls == ["high"]

    LuminaAgent.chat(fake_self, "turn two", reasoning_effort=None)

    # Only turn 2's own calls should be None -- turn 1's recorded "high"
    # entries stay at index 0 (call history accumulates), turn 2's new
    # entries at index 1 must not have inherited turn 1's value.
    assert llm.chat_calls == ["high", None]
    assert llm.stream_calls == ["high", None]


def test_reversed_order_no_cross_turn_leakage_in_either_direction():
    """Same invariant as above, opposite order -- None first, then an
    explicit effort -- so the guarantee isn't accidentally direction-
    specific (e.g. only proven for 'explicit value doesn't leak forward',
    not 'None doesn't get contaminated backward')."""
    llm = _RecordingLLM(tool_call_rounds=0)
    fake_self = _fake_agent(llm)

    LuminaAgent.chat(fake_self, "turn one", reasoning_effort=None)
    LuminaAgent.chat(fake_self, "turn two", reasoning_effort="low")

    assert llm.chat_calls == [None, "low"]
    assert llm.stream_calls == [None, "low"]
