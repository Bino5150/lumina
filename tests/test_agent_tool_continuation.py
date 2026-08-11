"""core/agent.py — Bug B observability companion. Before this fix, a
provider rejecting a tool-call continuation (e.g. Gemini's thoughtSignature
400) was caught by chat()'s tool-loop except-block and returned as a plain
string WITHOUT ever calling on_response_token(). Since the GUI's live chat
bubble is only ever populated via that streaming callback (_on_finished()
never displays its `response` argument -- it assumes the text was already
streamed, and only uses it for persistence/auto-naming), the user saw a
finalized, empty bubble: an apparent silent hang, no error, no explanation.

Fix: when a tool already ran this turn, build a message naming which tool(s)
ran and that the provider (not "the model") rejected the continuation, and
push it through self.on_response_token() -- the same live-rendering path
_stream_final()'s own error handler already uses for stream-time failures --
so it actually reaches the visible bubble regardless of which callback the
caller wired up. Does NOT change chat()'s never-raises contract: main.py's
CLI loop calls agent.chat() with no try/except around it, relying on that.
"""
import types
from core.agent import LuminaAgent


def _fake_agent(llm, tool_result="tool ran fine"):
    calls = {"response_tokens": [], "tool_calls": []}

    def on_response_token(tok):
        calls["response_tokens"].append(tok)

    def on_tool_call(name, args):
        calls["tool_calls"].append((name, args))

    ns = types.SimpleNamespace(
        llm=llm,
        ctx=types.SimpleNamespace(
            add_user=lambda content, source="OWNER_DIRECT": None,
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
        on_tool_call=on_tool_call,
        on_tool_result=lambda name, result: None,
        on_response_token=on_response_token,
        _session_tool_calls=0,
        _skill_nudge_sent=False,
    )
    return ns, calls


class _RaisingAfterNCallsLLM:
    """Fake backend: first N calls behave like a normal tool-call turn, then
    raises like GeminiBackend.chat() does on an HTTP error."""
    name = "gemini"
    display_name = "Gemini (Google)"

    def __init__(self, calls_before_raise, raise_msg="Gemini API HTTP error: 400 Client Error"):
        self.calls_before_raise = calls_before_raise
        self.raise_msg = raise_msg
        self.call_count = 0

    def get_model(self):
        return "gemini-3.5-flash"

    def chat(self, messages, tools=None, max_tokens=None):
        self.call_count += 1
        if self.call_count > self.calls_before_raise:
            raise RuntimeError(self.raise_msg)
        return {"_round": self.call_count}

    def extract_message(self, response):
        return {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "c0", "type": "function",
                             "function": {"name": "search_memory", "arguments": "{}"}}],
        }

    def is_tool_call(self, message):
        return bool(message.get("tool_calls"))

    def get_tool_calls(self, message):
        return message.get("tool_calls", [])

    def parse_tool_call(self, tc):
        return tc["function"]["name"], {}


def test_continuation_failure_after_tool_use_names_the_tool_and_provider():
    """The core Bug B observability behavior: fails on round 2 (the
    continuation, after search_memory already ran on round 1)."""
    llm = _RaisingAfterNCallsLLM(calls_before_raise=1)
    fake_self, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake_self, "what's the launch date?")

    assert "search_memory" in result
    assert "completed" in result
    assert "rejected the continuation" in result
    assert "Gemini" in result
    # Must have been streamed live, not just returned silently.
    assert calls["response_tokens"] == [result]
    # The tool itself really did fire before the continuation failed.
    assert calls["tool_calls"] == [("search_memory", {})]


def test_first_call_failure_before_any_tool_use_stays_generic():
    """Contrast case: nothing has run yet, so there's no tool to name --
    must not claim a tool "completed" when none did."""
    llm = _RaisingAfterNCallsLLM(calls_before_raise=0)
    fake_self, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake_self, "hello")

    assert result.startswith("[Lumina error:")
    assert "completed" not in result
    assert "rejected the continuation" not in result
    assert calls["response_tokens"] == [result]
    assert calls["tool_calls"] == []


def test_two_sequential_tool_calls_then_continuation_failure_names_both():
    """Two tools ran (across two rounds) before the third round's
    continuation fails -- both should be named, not just the most recent."""
    llm = _RaisingAfterNCallsLLM(calls_before_raise=2)
    fake_self, calls = _fake_agent(llm)

    # Second tool call must have a different name to prove both accumulate in
    # tools_used_this_turn — reuse the same fake LLM's extract_message, which
    # always names search_memory, so patch it after round 1.
    real_extract = llm.extract_message
    round_state = {"n": 0}

    def extract_alternating(response):
        round_state["n"] += 1
        name = "search_memory" if round_state["n"] == 1 else "read_file"
        return {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": f"c{round_state['n']}", "type": "function",
                             "function": {"name": name, "arguments": "{}"}}],
        }

    llm.extract_message = extract_alternating

    result = LuminaAgent.chat(fake_self, "find and read the file")

    assert "search_memory" in result
    assert "read_file" in result
    assert "rejected the continuation" in result
    assert calls["tool_calls"] == [("search_memory", {}), ("read_file", {})]
