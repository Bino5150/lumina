"""OPENAI-RESPONSES-01 OpenAI /v1/responses wire-contract regressions.

All HTTP is intercepted. These tests cover the concrete backend payloads and
the complete Lumina turn shape without sending provider or tool traffic.

Supersedes the old BACKEND-CONTRACT-01C Chat Completions coverage this file
used to hold: OpenAIBackend now speaks /v1/responses exclusively (see
core/backends/openai_backend.py's module docstring for the live-verified
root cause -- Chat Completions returns HTTP 400 the moment a gpt-5.6-family
reasoning model is asked for both function tools and a non-"none"
reasoning_effort in the same request).
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
from core.backends.openai_backend import OpenAIBackend, _normalize_responses_body
from core.backends.vllm import VLLMBackend


def _responses_body(output=None, status="completed", usage=None):
    return {
        "status": status,
        "output": output if output is not None else [
            {"type": "message", "status": "completed",
             "content": [{"type": "output_text", "text": "ok"}]},
        ],
        "usage": usage or {
            "input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }


class _FakeResponse:
    def __init__(self, body=None, stream_lines=()):
        self._body = body if body is not None else _responses_body()
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
def test_gpt_5_6_family_nonstream_uses_responses_endpoint_and_reasoning_object(
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
        "input": [{"role": "user", "content": "hi"}],
        "store": False,
        "max_output_tokens": 321,
        "reasoning": {"effort": "none", "summary": "auto"},
    }]
    assert "temperature" not in payloads[0]
    assert "max_tokens" not in payloads[0]
    assert "reasoning_effort" not in payloads[0]


def test_luna_stream_uses_responses_endpoint_and_preserves_reasoning(monkeypatch):
    payloads = _capture_posts(monkeypatch)
    backend = OpenAIBackend(api_key="test-key")
    backend._model = "gpt-5.6-luna"

    list(backend.chat_stream(
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=654,
        reasoning_effort="high",
    ))

    assert payloads[0]["max_output_tokens"] == 654
    assert "max_tokens" not in payloads[0]
    assert payloads[0]["reasoning"] == {"effort": "high", "summary": "auto"}
    assert "temperature" not in payloads[0]
    assert payloads[0]["stream"] is True


def test_openai_tool_payload_translated_flat_and_retains_tool_choice(monkeypatch):
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

    # OPENAI-RESPONSES-01: tools are flattened (no nested "function" key)
    # for the Responses wire shape -- live-verified 2026-09-05.
    assert payloads[0]["tools"] == [{
        "type": "function",
        "name": "lookup",
        "description": "look something up",
        "parameters": {"type": "object", "properties": {}},
    }]
    assert payloads[0]["tool_choice"] == "auto"
    assert payloads[0]["max_output_tokens"] == 222
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
        temperature=0.5,
    )

    assert payloads[0]["max_output_tokens"] == 111
    assert "max_tokens" not in payloads[0]
    assert "reasoning" not in payloads[0]
    # UTILITY-OPENAI-PARAMETER-CAPABILITY-01: a non-reasoning-capable model
    # keeps receiving temperature unchanged -- only reasoning-capable
    # models have it stripped (see the dedicated section below).
    assert payloads[0]["temperature"] == 0.5


@pytest.mark.parametrize(
    "model",
    ("gpt-5.6", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"),
)
def test_openai_reasoning_capable_model_never_sends_temperature(monkeypatch, model):
    """UTILITY-OPENAI-PARAMETER-CAPABILITY-01 -- live-verified 2026-09-05:
    gpt-5.6-luna returns HTTP 400 ("Unsupported value: 'temperature' does
    not support 0.3 with this model. Only the default (1) value is
    supported.") for ANY non-default temperature, on both /chat/completions
    and /v1/responses. Every complete_utility() call site passes an
    explicit temperature (0.2-0.3) -- this must never reach the wire for a
    reasoning-capable model, regardless of what the caller passed."""
    payloads = _capture_posts(monkeypatch)
    backend = OpenAIBackend(api_key="test-key")
    backend._model = model

    backend.chat(
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=30,
        temperature=0.3,
    )

    assert "temperature" not in payloads[0]


def test_openai_utility_completion_omits_temperature_and_uses_responses_endpoint(monkeypatch):
    payloads = _capture_posts(monkeypatch, responses=(_FakeResponse(_responses_body(
        output=[{"type": "message", "status": "completed",
                 "content": [{"type": "output_text", "text": "ok"}]}],
    )),))
    backend = OpenAIBackend(api_key="test-key")
    backend._model = "gpt-5.6-luna"

    assert backend.complete_utility("name this", max_tokens=30) == "ok"

    assert payloads[0]["max_output_tokens"] == 30
    assert "max_tokens" not in payloads[0]
    assert "temperature" not in payloads[0]
    assert "reasoning" not in payloads[0]


@pytest.mark.parametrize(
    "backend",
    (
        LMStudioBackend(base_url="http://local.invalid/v1"),
        LlamaCppBackend(base_url="http://local.invalid/v1"),
        VLLMBackend(base_url="http://local.invalid/v1"),
        GroqBackend(api_key="test-key"),
    ),
)
def test_openai_compatible_siblings_keep_chat_completions_and_max_tokens(monkeypatch, backend):
    """OPENAI-RESPONSES-01 is scoped to OpenAIBackend only -- every sibling
    LMStudioBackend descendant must keep using /chat/completions and
    max_tokens completely unchanged."""
    payloads = _capture_posts(monkeypatch)
    backend._model = "sibling-model"

    backend.chat(
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=444,
    )

    assert payloads[0]["max_tokens"] == 444
    assert "max_completion_tokens" not in payloads[0]
    assert "max_output_tokens" not in payloads[0]
    assert "messages" in payloads[0]
    assert "input" not in payloads[0]


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
    assert "max_output_tokens" not in payloads[0]


def test_openai_wire_decision_is_owned_by_concrete_backend():
    assert "_apply_output_token_limit" in OpenAIBackend.__dict__
    assert "max_output_tokens" not in inspect.getsource(LuminaAgent)


def test_openai_posts_to_responses_endpoint_not_chat_completions(monkeypatch):
    captured_urls = []

    def fake_post(*args, **kwargs):
        captured_urls.append(args[0] if args else kwargs.get("url"))
        return _FakeResponse()

    monkeypatch.setattr(requests, "post", fake_post)
    backend = OpenAIBackend(api_key="test-key")
    backend._model = "gpt-5.6-luna"

    backend.chat(messages=[{"role": "user", "content": "hi"}])

    assert captured_urls == ["https://api.openai.com/v1/responses"]


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


def test_complete_openai_tool_loop_uses_responses_endpoint_on_every_request(
    monkeypatch
):
    tool_call_response = _FakeResponse(_responses_body(output=[
        {"type": "function_call", "status": "completed", "call_id": "call-1",
         "name": "lookup", "arguments": "{}"},
    ]))
    # AGENT-CONTINUATION-CONTROL-GATE-01A: a real tool already ran (round
    # 1), so the next WORK round (round 2) stays AUTO and offers only the
    # product tool schema -- no completion control mixed in. It returns no
    # real tool call, which triggers the completion-control gate (round 3):
    # a REQUIRED request offering ONLY the two internal control
    # primitives, never the product tool schema.
    work_no_tool_response = _FakeResponse(_responses_body(output=[]))
    gate_response = _FakeResponse(_responses_body(output=[
        {"type": "function_call", "status": "completed", "call_id": "call-2",
         "name": "finish_tool_work", "arguments": "{}"},
    ]))
    stream_response = _FakeResponse(stream_lines=(
        b'data: {"type":"response.output_text.delta","delta":"final answer"}',
        b'data: {"type":"response.completed"}',
    ))
    payloads = _capture_posts(
        monkeypatch,
        responses=(tool_call_response, work_no_tool_response, gate_response, stream_response),
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
    assert len(payloads) == 4
    assert [payload.get("stream", False) for payload in payloads] == [False, False, False, True]
    for payload in payloads:
        assert payload["max_output_tokens"] > 0
        assert "max_tokens" not in payload
        assert payload["reasoning"] == {"effort": "none", "summary": "auto"}
    assert payloads[0]["tools"] == [{
        "type": "function", "name": "lookup",
        "description": "look something up",
        "parameters": {"type": "object", "properties": {}},
    }]
    assert payloads[0]["tool_choice"] == "auto"
    # Round 2 (the WORK round right after a real tool ran) stays AUTO and
    # offers ONLY the product tool schema -- no completion control mixed
    # in (AGENT-CONTINUATION-CONTROL-GATE-01A replaces the old design's
    # finish_tool_work-appended-to-the-full-schema shape).
    assert payloads[1]["tools"][0]["name"] == "lookup"
    assert payloads[1]["tool_choice"] == "auto"
    # Round 3 is the completion-control gate: ONLY the two internal
    # control primitives, REQUIRED (OpenAIBackend has live-verified
    # support), never the product tool schema.
    gate_names = {t["name"] for t in payloads[2]["tools"]}
    assert gate_names == {"continue_tool_work", "finish_tool_work"}
    assert "lookup" not in gate_names
    assert payloads[2]["tool_choice"] == "required"
    assert "tools" not in payloads[3]
    assert "tool_choice" not in payloads[3]


# ══════════════════════════════════════════════════════════════════════
# _normalize_responses_body() -- unit coverage independent of HTTP
# ══════════════════════════════════════════════════════════════════════

def test_normalize_plain_message_maps_to_stop():
    body = _responses_body(output=[
        {"type": "message", "status": "completed",
         "content": [{"type": "output_text", "text": "hello there"}]},
    ])
    normalized = _normalize_responses_body(body)
    message = normalized["choices"][0]["message"]
    assert message["content"] == "hello there"
    assert "tool_calls" not in message
    assert "reasoning_content" not in message
    assert normalized["choices"][0]["finish_reason"] == "stop"


def test_normalize_parallel_tool_calls_all_collected():
    body = _responses_body(output=[
        {"type": "function_call", "status": "completed", "call_id": "c1",
         "name": "tool_a", "arguments": "{}"},
        {"type": "function_call", "status": "completed", "call_id": "c2",
         "name": "tool_b", "arguments": "{\"x\": 1}"},
    ])
    normalized = _normalize_responses_body(body)
    message = normalized["choices"][0]["message"]
    assert message["tool_calls"] == [
        {"id": "c1", "type": "function", "function": {"name": "tool_a", "arguments": "{}"}},
        {"id": "c2", "type": "function", "function": {"name": "tool_b", "arguments": "{\"x\": 1}"}},
    ]
    assert normalized["choices"][0]["finish_reason"] == "tool_calls"


def test_normalize_reasoning_summary_surfaces_as_reasoning_content_never_final():
    """AGENT-TOOL-THINK-TELEMETRY-01A1 / Lumina law: reasoning is evidence,
    never Final. A reasoning item's summary text must land in
    reasoning_content (the same field LMStudioBackend.extract_reasoning()
    already reads for Think telemetry), never mixed into "content"."""
    body = _responses_body(output=[
        {"type": "reasoning", "summary": [
            {"type": "summary_text", "text": "Checking the time zone first."},
        ]},
        {"type": "function_call", "status": "completed", "call_id": "c1",
         "name": "get_time", "arguments": "{}"},
    ])
    normalized = _normalize_responses_body(body)
    message = normalized["choices"][0]["message"]
    assert message["reasoning_content"] == "Checking the time zone first."
    assert message["content"] == ""
    assert message["tool_calls"][0]["function"]["name"] == "get_time"


def test_normalize_empty_reasoning_summary_is_ordinary_not_an_error():
    """Live-verified 2026-09-05: a `reasoning` output item's `summary` is
    genuinely optional per-response (an adaptive model decision) -- an
    empty summary list must normalize cleanly with no reasoning_content
    key at all, not a crash or a fabricated empty string."""
    body = _responses_body(output=[
        {"type": "reasoning", "summary": [], "encrypted_content": "opaque-blob"},
        {"type": "function_call", "status": "completed", "call_id": "c1",
         "name": "get_time", "arguments": "{}"},
    ])
    normalized = _normalize_responses_body(body)
    message = normalized["choices"][0]["message"]
    assert "reasoning_content" not in message
    # encrypted_content must never be read into anything -- no key in the
    # normalized message could possibly carry it.
    assert "encrypted_content" not in str(message)


def test_normalize_incomplete_status_maps_to_length():
    body = _responses_body(
        status="incomplete",
        output=[{"type": "message", "status": "incomplete",
                 "content": [{"type": "output_text", "text": "cut off mid"}]}],
    )
    body["incomplete_details"] = {"reason": "max_output_tokens"}
    normalized = _normalize_responses_body(body)
    assert normalized["choices"][0]["finish_reason"] == "length"


def test_normalize_usage_field_mapping():
    body = _responses_body(usage={
        "input_tokens": 100, "output_tokens": 40, "total_tokens": 140,
        "input_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 0},
        "output_tokens_details": {"reasoning_tokens": 15},
    })
    normalized = _normalize_responses_body(body)
    assert normalized["usage"] == {
        "prompt_tokens": 100,
        "completion_tokens": 40,
        "total_tokens": 140,
        "prompt_tokens_details": {"cached_tokens": 20},
        "completion_tokens_details": {"reasoning_tokens": 15},
    }


# ══════════════════════════════════════════════════════════════════════
# chat_stream() -- reasoning-summary Think markers (live-verified event
# vocabulary, see core/backends/openai_backend.py's chat_stream() docstring)
# ══════════════════════════════════════════════════════════════════════

def test_stream_reasoning_summary_wrapped_in_think_markers(monkeypatch):
    lines = (
        b'data: {"type":"response.created"}',
        b'data: {"type":"response.reasoning_summary_text.delta","delta":"Check"}',
        b'data: {"type":"response.reasoning_summary_text.delta","delta":"ing time"}',
        b'data: {"type":"response.output_text.delta","delta":"It"}',
        b'data: {"type":"response.output_text.delta","delta":"\'s 3pm."}',
        b'data: {"type":"response.completed"}',
    )
    _capture_posts(monkeypatch, responses=(_FakeResponse(stream_lines=lines),))
    backend = OpenAIBackend(api_key="test-key")
    backend._model = "gpt-5.6-luna"

    tokens = list(backend.chat_stream(
        messages=[{"role": "user", "content": "what time is it"}],
        reasoning_effort="high",
    ))

    assert tokens == [
        "__THINK_START__", "Check", "ing time", "__THINK_END__",
        "It", "'s 3pm.",
    ]


def test_stream_with_no_reasoning_summary_never_emits_think_markers(monkeypatch):
    lines = (
        b'data: {"type":"response.created"}',
        b'data: {"type":"response.output_text.delta","delta":"Hello"}',
        b'data: {"type":"response.completed"}',
    )
    _capture_posts(monkeypatch, responses=(_FakeResponse(stream_lines=lines),))
    backend = OpenAIBackend(api_key="test-key")
    backend._model = "gpt-5.6-luna"

    tokens = list(backend.chat_stream(
        messages=[{"role": "user", "content": "hi"}],
        reasoning_effort="high",
    ))

    assert tokens == ["Hello"]
    assert "__THINK_START__" not in tokens
