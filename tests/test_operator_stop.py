import threading
import types

import pytest

import core.agent as agent_module
from core.agent import LuminaAgent, TurnCancelled
from core.context import ContextManager


class _Ctx:
    def __init__(self):
        self.history = []
        self.events = []

    def add_user(self, content, source="OWNER_DIRECT"):
        self.history.append({"role": "user", "content": content})

    def push_ephemeral(self, block):
        pass

    def build_messages(self, tool_budget=0, chat_id=None):
        return []

    def add_tool_call(self, message):
        self.events.append(("tool_call_message", message))

    def add_tool_result(self, tool_call_id, name, result):
        self.events.append(("tool_result", tool_call_id, name, result))

    def add_cancelled_tool_result(self, tool_call_id, name):
        self.events.append(("cancelled_tool", tool_call_id, name))

    def add_assistant(self, content):
        self.history.append({"role": "assistant", "content": content})


def _tool_agent(llm, registry_call):
    ctx = _Ctx()
    fake = types.SimpleNamespace(
        owner=False,
        llm=llm,
        ctx=ctx,
        registry=types.SimpleNamespace(
            schema_token_estimate=lambda: 0,
            get_schemas=lambda: [],
            list_enabled=lambda: [],
            call=registry_call,
        ),
        on_tool_call=lambda name, args: None,
        on_tool_result=lambda name, result: None,
        on_response_token=lambda text: None,
        _session_tool_calls=0,
        _skill_nudge_sent=False,
    )
    return fake, ctx


class _TwoToolLLM:
    display_name = "fake"

    def chat(self, **kwargs):
        return {"ok": True}

    def extract_message(self, response):
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "function": {"name": "one", "arguments": "{}"}},
                {"id": "c2", "function": {"name": "two", "arguments": "{}"}},
            ],
        }

    def is_tool_call(self, message):
        return True

    def get_tool_calls(self, message):
        return message["tool_calls"]

    def parse_tool_call(self, tc):
        return tc["function"]["name"], {}


def test_cancelled_tool_result_is_internal_control_not_untrusted_content():
    cm = ContextManager(owner=True)
    assert cm._untrusted_content_seen is False

    cm.add_cancelled_tool_result("call-1", "dangerous_tool")

    assert cm._untrusted_content_seen is False
    assert cm.history[-1] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "name": "dangerous_tool",
        "content": "[Cancelled by operator before execution.]",
    }


def test_cancel_already_requested_preserves_user_turn_without_provider_work():
    event = threading.Event()
    event.set()
    ctx = _Ctx()
    fake = types.SimpleNamespace(ctx=ctx)

    with pytest.raises(TurnCancelled):
        LuminaAgent.chat(fake, "keep this user turn", cancel_event=event)

    assert ctx.history == [{"role": "user", "content": "keep this user turn"}]


def test_cancel_during_first_tool_closes_remaining_id_without_running_it(monkeypatch):
    monkeypatch.setattr(agent_module, "build_skills_block", lambda query: "")
    event = threading.Event()
    calls = []

    def registry_call(name, args):
        calls.append(name)
        if name == "one":
            event.set()
        return f"result:{name}"

    fake, ctx = _tool_agent(_TwoToolLLM(), registry_call)

    with pytest.raises(TurnCancelled):
        LuminaAgent.chat(fake, "do both", cancel_event=event)

    assert calls == ["one"]
    assert ("tool_result", "c1", "one", "result:one") in ctx.events
    assert ("cancelled_tool", "c2", "two") in ctx.events
    assert not any(e[:3] == ("tool_result", "c2", "two") for e in ctx.events)


def test_cancel_after_tool_message_before_execution_closes_every_id(monkeypatch):
    monkeypatch.setattr(agent_module, "build_skills_block", lambda query: "")
    event = threading.Event()
    calls = []
    llm = _TwoToolLLM()
    real_get_tool_calls = llm.get_tool_calls

    def get_tool_calls_and_cancel(message):
        calls_out = real_get_tool_calls(message)
        event.set()
        return calls_out

    llm.get_tool_calls = get_tool_calls_and_cancel
    fake, ctx = _tool_agent(llm, lambda name, args: calls.append(name))

    with pytest.raises(TurnCancelled):
        LuminaAgent.chat(fake, "do neither", cancel_event=event)

    assert calls == []
    assert ("cancelled_tool", "c1", "one") in ctx.events
    assert ("cancelled_tool", "c2", "two") in ctx.events


class _StreamingLLM:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    def chat_stream(self, messages, max_tokens, reasoning_effort=None):
        yield from self.chunks


def _stream_agent(chunks, on_response_token=None, on_think_token=None, on_think_end=None):
    ctx = _Ctx()
    fake = types.SimpleNamespace(
        llm=_StreamingLLM(chunks),
        ctx=ctx,
        on_response_token=on_response_token or (lambda text: None),
        on_think_start=lambda step: None,
        on_think_token=on_think_token or (lambda text: None),
        on_think_end=on_think_end or (lambda: None),
        tts=None,
    )
    return fake, ctx


def test_stream_cancel_preserves_only_the_partial_assistant_text():
    event = threading.Event()

    def on_response(text):
        event.set()

    fake, ctx = _stream_agent(["partial", " should-not-arrive"], on_response_token=on_response)

    with pytest.raises(TurnCancelled) as exc:
        LuminaAgent._stream_final(fake, [], [0], cancel_event=event)

    assert exc.value.partial_response == "partial"
    assert ctx.history == [{"role": "assistant", "content": "partial"}]


def test_stream_cancel_while_thinking_closes_think_block_without_assistant_text():
    event = threading.Event()
    think_ends = []

    def on_think(text):
        event.set()

    fake, ctx = _stream_agent(
        ["__THINK_START__", "private reasoning", "__THINK_END__", "answer"],
        on_think_token=on_think,
        on_think_end=lambda: think_ends.append(True),
    )

    with pytest.raises(TurnCancelled) as exc:
        LuminaAgent._stream_final(fake, [], [0], cancel_event=event)

    assert exc.value.partial_response == ""
    assert ctx.history == []
    assert think_ends == [True]


def test_provider_exception_after_stop_request_is_reported_as_cancellation(monkeypatch):
    monkeypatch.setattr(agent_module, "build_skills_block", lambda query: "")
    event = threading.Event()

    class _LLM:
        display_name = "fake"

        def chat(self, **kwargs):
            event.set()
            raise RuntimeError("network timeout after stop")

    ctx = _Ctx()
    fake = types.SimpleNamespace(
        owner=False,
        ctx=ctx,
        llm=_LLM(),
        registry=types.SimpleNamespace(
            schema_token_estimate=lambda: 0,
            get_schemas=lambda: [],
            list_enabled=lambda: [],
        ),
    )

    with pytest.raises(TurnCancelled):
        LuminaAgent.chat(fake, "hello", cancel_event=event)

    assert ctx.history == [{"role": "user", "content": "hello"}]
