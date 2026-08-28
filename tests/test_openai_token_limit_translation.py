"""BACKEND-CONTRACT-01C OpenAI Chat Completions token-limit regressions.

All HTTP is intercepted. These tests cover the concrete backend payloads and
the complete Lumina turn shape without sending provider or tool traffic.
"""

import inspect
import json
import types

import pytest
import requests

from core.agent import LuminaAgent
from core.backends.groq import GroqBackend
from core.backends.llamacpp import LlamaCppBackend
from core.backends.lmstudio import LMStudioBackend
from core.backends.openai_backend import OpenAIBackend
from core.backends.vllm import VLLMBackend


class _FakeResponse:
    def __init__(self, body=None, stream_lines=()):
        self._body = body if body is not None else {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        }
        self._stream_lines = tuple(stream_lines)
        self.status_code = 200
        self.text = json.dumps(self._body)

    def raise_for_status(self):
        return None

    def json(self):
        return self._body

    def iter_lines(self):
        return iter(self._stream_lines)


def _capture_posts(monkeypatch, responses=None):
    payloads = []
    queued = iter(responses or ())

    def fake_post(*args, **kwargs):
        payloads.append(kwargs["json"])
        try:
            return next(queued)
        except StopIteration:
            return _FakeResponse()

    monkeypatch.setattr(requests, "post", fake_post)
    return payloads


@pytest.mark.parametrize(
    "model",
    ("gpt-5.6", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"),
)
def test_gpt_5_6_family_nonstream_uses_openai_token_limit_and_reasoning_none(
    monkeypatch, model
):
    payloads = _capture_posts(monkeypatch)
    backend = OpenAIBackend(api_key="test-key")
    backend._model = model

    backend.chat(
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=321,
        reasoning_effort="none",
    )

    assert payloads == [{
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "temperature": 0.7,
        "stream": False,
        "max_completion_tokens": 321,
        "reasoning_effort": "none",
    }]
    assert "max_tokens" not in payloads[0]


def test_luna_stream_uses_openai_token_limit_and_preserves_reasoning(monkeypatch):
    payloads = _capture_posts(monkeypatch)
    backend = OpenAIBackend(api_key="test-key")
    backend._model = "gpt-5.6-luna"

    list(backend.chat_stream(
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=654,
        reasoning_effort="high",
    ))

    assert payloads[0]["max_completion_tokens"] == 654
    assert "max_tokens" not in payloads[0]
    assert payloads[0]["reasoning_effort"] == "high"
    assert payloads[0]["stream"] is True


def test_openai_tool_payload_retains_tools_and_tool_choice(monkeypatch):
    payloads = _capture_posts(monkeypatch)
    backend = OpenAIBackend(api_key="test-key")
    backend._model = "gpt-5.6-luna"
    tools = [{
        "type": "function",
        "function": {
            "name": "lookup",
            "description": "look something up",
            "parameters": {"type": "object", "properties": {}},
        },
    }]

    backend.chat(
        messages=[{"role": "user", "content": "hi"}],
        tools=tools,
        max_tokens=222,
        reasoning_effort="none",
    )

    assert payloads[0]["tools"] == tools
    assert payloads[0]["tool_choice"] == "auto"
    assert payloads[0]["max_completion_tokens"] == 222
    assert "max_tokens" not in payloads[0]


@pytest.mark.parametrize("model", ("gpt-4o", "gpt-4o-mini", "future-openai-chat-model"))
def test_openai_provider_wide_rule_preserves_older_and_unknown_model_reasoning_default(
    monkeypatch, model
):
    payloads = _capture_posts(monkeypatch)
    backend = OpenAIBackend(api_key="test-key")
    backend._model = model

    backend.chat(
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=111,
        reasoning_effort="high",
    )

    assert payloads[0]["max_completion_tokens"] == 111
    assert "max_tokens" not in payloads[0]
    assert "reasoning_effort" not in payloads[0]


def test_openai_utility_completion_uses_same_provider_owned_translation(monkeypatch):
    payloads = _capture_posts(monkeypatch)
    backend = OpenAIBackend(api_key="test-key")
    backend._model = "gpt-5.6-luna"

    assert backend.complete_utility("name this", max_tokens=30) == "ok"

    assert payloads[0]["max_completion_tokens"] == 30
    assert "max_tokens" not in payloads[0]


@pytest.mark.parametrize(
    "backend",
    (
        LMStudioBackend(base_url="http://local.invalid/v1"),
        LlamaCppBackend(base_url="http://local.invalid/v1"),
        VLLMBackend(base_url="http://local.invalid/v1"),
        GroqBackend(api_key="test-key"),
    ),
)
def test_openai_compatible_siblings_keep_max_tokens(monkeypatch, backend):
    payloads = _capture_posts(monkeypatch)
    backend._model = "sibling-model"

    backend.chat(
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=444,
    )

    assert payloads[0]["max_tokens"] == 444
    assert "max_completion_tokens" not in payloads[0]


def test_lmstudio_stream_keeps_max_tokens(monkeypatch):
    payloads = _capture_posts(monkeypatch)
    backend = LMStudioBackend(base_url="http://local.invalid/v1")
    backend._model = "local-model"

    list(backend.chat_stream(
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=555,
    ))

    assert payloads[0]["max_tokens"] == 555
    assert "max_completion_tokens" not in payloads[0]


def test_openai_wire_decision_is_owned_by_concrete_backend():
    assert "_apply_output_token_limit" in OpenAIBackend.__dict__
    assert "max_completion_tokens" not in inspect.getsource(LuminaAgent)


def _fake_agent(llm, tool_schema, tool_calls):
    history = []

    def add_user(content, source="OWNER_DIRECT"):
        history.append({"role": "user", "content": content})

    def add_assistant(content):
        history.append({"role": "assistant", "content": content})

    fake = types.SimpleNamespace(
        llm=llm,
        owner=False,
        ctx=types.SimpleNamespace(
            history=history,
            add_user=add_user,
            add_assistant=add_assistant,
            push_ephemeral=lambda block: None,
            build_messages=lambda tool_budget=0, chat_id=None: [
                {"role": "user", "content": "do the thing"},
            ],
            add_tool_call=lambda message: None,
            add_tool_result=lambda tool_call_id, name, result: None,
        ),
        registry=types.SimpleNamespace(
            schema_token_estimate=lambda: 1,
            get_schemas=lambda: [tool_schema],
            list_enabled=lambda: ["lookup"],
            call=lambda name, args: tool_calls.append((name, args)) or "tool result",
        ),
        on_tool_call=lambda name, args: None,
        on_tool_result=lambda name, result: None,
        on_response_token=lambda token: None,
        on_think_start=lambda step: None,
        on_think_token=lambda token: None,
        on_think_end=lambda: None,
        tts=None,
        _session_tool_calls=0,
        _skill_nudge_sent=False,
    )
    fake._stream_final = types.MethodType(LuminaAgent._stream_final, fake)
    return fake


def test_complete_openai_tool_loop_uses_correct_field_on_every_request(
    monkeypatch
):
    tool_call_response = _FakeResponse({
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                }],
            },
        }],
    })
    # AGENT-CONTINUATION-01A: a real tool already ran (round 1), so the
    # harness now requires an explicit finish_tool_work completion signal
    # on the continuation round instead of inferring "done" from a bare
    # no-tool-calls message -- see core/agent.py's tool loop. A bare
    # content-only response here would (correctly) trigger the bounded
    # corrective retry instead of proceeding straight to _stream_final().
    continuation_response = _FakeResponse({
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call-2",
                    "type": "function",
                    "function": {"name": "finish_tool_work", "arguments": "{}"},
                }],
            },
        }],
    })
    stream_response = _FakeResponse(stream_lines=(
        b'data: {"choices":[{"delta":{"content":"final answer"}}]}',
        b"data: [DONE]",
    ))
    payloads = _capture_posts(
        monkeypatch,
        responses=(tool_call_response, continuation_response, stream_response),
    )
    monkeypatch.setattr("core.agent.build_skills_block", lambda user_input: "")

    backend = OpenAIBackend(api_key="test-key")
    backend._model = "gpt-5.6-luna"
    tool_schema = {
        "type": "function",
        "function": {
            "name": "lookup",
            "description": "look something up",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    tool_calls = []
    fake = _fake_agent(backend, tool_schema, tool_calls)

    result = LuminaAgent.chat(fake, "do the thing", reasoning_effort="none")

    assert result == "final answer"
    assert tool_calls == [("lookup", {})]
    assert len(payloads) == 3
    assert [payload["stream"] for payload in payloads] == [False, False, True]
    for payload in payloads:
        assert payload["max_completion_tokens"] > 0
        assert "max_tokens" not in payload
        assert payload["reasoning_effort"] == "none"
    assert payloads[0]["tools"] == [tool_schema]
    assert payloads[0]["tool_choice"] == "auto"
    # Round 2 is a continuation after a real tool already ran, so it also
    # carries the finish_tool_work sentinel (AGENT-CONTINUATION-01A) --
    # tool_schema plus exactly one more entry named finish_tool_work.
    assert tool_schema in payloads[1]["tools"]
    assert len(payloads[1]["tools"]) == 2
    sentinel_names = [
        t["function"]["name"] for t in payloads[1]["tools"] if t != tool_schema
    ]
    assert sentinel_names == ["finish_tool_work"]
    assert payloads[1]["tool_choice"] == "auto"
    assert "tools" not in payloads[2]
    assert "tool_choice" not in payloads[2]
