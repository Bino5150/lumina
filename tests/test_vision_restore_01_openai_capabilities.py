"""VISION-RESTORE-01 -- OpenAI temperature policy decoupled from reasoning and
vision+tools capability, plus the bounded unsupported-temperature retry.

Live evidence (2026-09-23, real /v1/responses): gpt-6-luna with
temperature=0.7 -> HTTP 400 {"type": "invalid_request_error", "param":
"temperature", "code": null, "message": "Unsupported parameter:
'temperature' is not supported with this model."}. The routed vision
specialist call (default temperature 0.7) and every complete_utility()
consumer hit that because temperature was only omitted for models in the
reasoning table.

Qt-free on purpose: runs (and blocks) in CI's plain `test` job. All HTTP is
stubbed at requests.post.
"""

import json

import pytest
import requests

import core.flight_recorder as flight_recorder
from core.backends import openai_backend
from core.backends.openai_backend import OpenAIBackend
from core.backends.reasoning import NO_REASONING_CONTROL

GPT_5_6_FAMILY = ("gpt-5.6", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")
_IMAGE = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
_TOOL = {"type": "function", "function": {
    "name": "lookup", "description": "d", "parameters": {"type": "object", "properties": {}},
}}


def _ok_body(text="ok"):
    return {
        "id": "resp_1", "object": "response", "status": "completed",
        "output": [{"type": "message", "status": "completed",
                    "content": [{"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    }


class _Resp:
    def __init__(self, status=200, body=None, stream_lines=()):
        self.status_code = status
        self._body = body if body is not None else _ok_body()
        self.text = json.dumps(self._body)
        self._stream_lines = tuple(stream_lines)
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} error")

    def json(self):
        return self._body

    def iter_lines(self):
        return iter(self._stream_lines)

    def close(self):
        self.closed = True


def _stream_ok(text="ok"):
    events = [
        {"type": "response.output_text.delta", "delta": text},
        {"type": "response.completed", "response": _ok_body(text)},
    ]
    return _Resp(stream_lines=tuple(f"data: {json.dumps(e)}".encode() for e in events))


def _error(status=400, *, param="temperature", message=None, type_="invalid_request_error"):
    if message is None:
        message = "Unsupported parameter: 'temperature' is not supported with this model."
    return _Resp(status, {"error": {"message": message, "type": type_, "param": param, "code": None}})


@pytest.fixture
def posts(monkeypatch):
    """Capture every POST payload; queue responses (default: 200 ok)."""
    state = {"payloads": [], "queue": []}

    def fake_post(url, *args, **kwargs):
        state["payloads"].append(kwargs["json"])
        if state["queue"]:
            item = state["queue"].pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        return _stream_ok() if kwargs.get("stream") else _Resp()

    monkeypatch.setattr(requests, "post", fake_post)
    return state


@pytest.fixture
def mismatch_events(monkeypatch):
    events = []
    monkeypatch.setattr(
        flight_recorder, "record_machine_event",
        lambda event_type, **kw: events.append((event_type, kw)),
    )
    return events


def _backend(model):
    backend = OpenAIBackend(api_key="test-key")
    backend._model = model
    return backend


def _chat(model, **kwargs):
    kwargs.setdefault("messages", [{"role": "user", "content": "hi"}])
    kwargs.setdefault("max_tokens", 32)
    return _backend(model).chat(**kwargs)


# ── GPT-6 Luna: temperature never on the wire ─────────────────────────────


def test_gpt6_luna_chat_omits_temperature(posts):
    _chat("gpt-6-luna", temperature=0.7)
    assert "temperature" not in posts["payloads"][0]


def test_gpt6_luna_vision_specialist_call_shape_omits_temperature(posts):
    """The exact call core.vision_lane makes: images + text, no explicit
    temperature (so chat()'s 0.7 default applies)."""
    _backend("gpt-6-luna").chat(
        messages=[{"role": "user", "content": [_IMAGE, {"type": "text", "text": "describe"}]}],
        max_tokens=800,
    )
    payload = posts["payloads"][0]
    assert "temperature" not in payload
    assert len(posts["payloads"]) == 1


def test_gpt6_luna_stream_and_telemetry_paths_omit_temperature(posts):
    list(_backend("gpt-6-luna").chat_stream(
        messages=[{"role": "user", "content": "hi"}], temperature=0.4,
    ))
    _chat("gpt-6-luna", temperature=0.4, capture_telemetry=True)
    assert all("temperature" not in p for p in posts["payloads"])
    assert len(posts["payloads"]) == 2


def test_gpt6_luna_utility_omits_temperature_and_returns_text(posts):
    posts["queue"].append(_Resp(body=_ok_body("Chat title")))
    result = _backend("gpt-6-luna").complete_utility("name this chat", prefill="", temperature=0.3)
    assert result == "Chat title"
    assert "temperature" not in posts["payloads"][0]


# ── Preserved behavior ────────────────────────────────────────────────────


@pytest.mark.parametrize("model", GPT_5_6_FAMILY)
def test_gpt_5_6_family_behavior_preserved(posts, model):
    _chat(model, temperature=0.3, reasoning_effort="high")
    payload = posts["payloads"][0]
    assert "temperature" not in payload
    assert payload["reasoning"] == {"effort": "high", "summary": "auto"}
    backend = _backend(model)
    assert backend.reasoning_capabilities(model) is not NO_REASONING_CONTROL
    assert backend.supports_vision_with_tools(model) is True


@pytest.mark.parametrize("model", ("gpt-4o-mini", "gpt-4o"))
def test_temperature_capable_model_still_receives_caller_temperature(posts, model):
    _chat(model, temperature=0.25)
    list(_backend(model).chat_stream(messages=[{"role": "user", "content": "hi"}], temperature=0.25))
    assert [p["temperature"] for p in posts["payloads"]] == [0.25, 0.25]


# ── Capability independence ───────────────────────────────────────────────


def test_gpt6_luna_has_no_inferred_reasoning_or_vision_tool_capability(posts):
    backend = _backend("gpt-6-luna")
    assert backend.reasoning_capabilities("gpt-6-luna") is NO_REASONING_CONTROL
    assert backend.supports_vision_with_tools("gpt-6-luna") is False

    backend.chat(
        messages=[{"role": "user", "content": [_IMAGE, {"type": "text", "text": "x"}]}],
        tools=[_TOOL], reasoning_effort="high", max_tokens=16,
    )
    payload = posts["payloads"][0]
    assert "tools" not in payload
    assert "reasoning" not in payload


def test_temperature_rule_alone_grants_no_other_capability(posts, monkeypatch):
    """Adding a model to the temperature table must not make it reasoning-,
    vision+tools-capable -- whatever table/derivation that would go through."""
    model = "temperature-rule-only-model"
    monkeypatch.setattr(
        openai_backend, "_TEMPERATURE_FORBIDDEN_MODELS",
        openai_backend._TEMPERATURE_FORBIDDEN_MODELS | {model},
    )
    backend = _backend(model)
    assert backend.reasoning_capabilities(model) is NO_REASONING_CONTROL
    assert backend.supports_vision_with_tools(model) is False

    backend.chat(
        messages=[{"role": "user", "content": [_IMAGE, {"type": "text", "text": "x"}]}],
        tools=[_TOOL], reasoning_effort="high", temperature=0.5, max_tokens=16,
    )
    payload = posts["payloads"][0]
    assert "temperature" not in payload
    assert "tools" not in payload
    assert "reasoning" not in payload


def test_reasoning_membership_alone_does_not_decide_temperature(posts, monkeypatch):
    """Temperature is not derived from the reasoning table in either
    direction: a reasoning-table-only model keeps its temperature."""
    model = "reasoning-rule-only-model"
    monkeypatch.setitem(openai_backend._REASONING_MODELS, model, openai_backend._GPT_5_6_CAPS)
    _chat(model, temperature=0.6)
    assert posts["payloads"][0]["temperature"] == 0.6


def test_vision_tool_table_is_declared_not_derived_from_other_tables():
    assert openai_backend._VISION_TOOL_CAPABLE_MODELS == frozenset(GPT_5_6_FAMILY)
    for model in ("gpt-6-luna", "gpt-6-sol"):
        assert model in openai_backend._TEMPERATURE_FORBIDDEN_MODELS
        assert model not in openai_backend._REASONING_MODELS
        assert model not in openai_backend._VISION_TOOL_CAPABLE_MODELS
    # Unverified family members are not admitted by resemblance.
    assert "gpt-6-astra" not in openai_backend._TEMPERATURE_FORBIDDEN_MODELS


def test_gpt6_sol_omits_temperature_without_other_capabilities(posts):
    """Live-verified 2026-09-23 on its own: gpt-6-sol rejects temperature 0.3
    with the same HTTP 400 as gpt-6-luna."""
    backend = _backend("gpt-6-sol")
    backend.chat(
        messages=[{"role": "user", "content": [_IMAGE, {"type": "text", "text": "x"}]}],
        tools=[_TOOL], reasoning_effort="high", temperature=0.3, max_tokens=16,
    )
    payload = posts["payloads"][0]
    assert "temperature" not in payload
    assert "tools" not in payload
    assert "reasoning" not in payload
    assert len(posts["payloads"]) == 1


# ── Bounded unsupported-temperature retry (defense in depth) ──────────────


def test_unknown_model_temperature_rejection_retries_once_without_only_temperature(
    posts, mismatch_events
):
    posts["queue"].extend([_error(), _Resp(body=_ok_body("seen"))])
    response = _chat("future-openai-model", temperature=0.7)

    first, second = posts["payloads"]
    assert first["temperature"] == 0.7
    assert "temperature" not in second
    assert {k: v for k, v in first.items() if k != "temperature"} == second
    assert response["choices"][0]["message"]["content"] == "seen"
    assert mismatch_events == [("backend.capability_mismatch", {
        "severity": "warning", "backend": "openai", "model": "future-openai-model",
        "fields": {"parameter": "temperature", "action": "retried_once_without_parameter",
                   "capability_metadata": "stale"},
    })]


def test_retry_is_at_most_once(posts, mismatch_events):
    posts["queue"].extend([_error(), _error()])
    with pytest.raises(RuntimeError):
        _chat("future-openai-model", temperature=0.7)
    assert len(posts["payloads"]) == 2
    assert len(mismatch_events) == 1


def test_unsupported_value_shape_is_also_recognized(posts, mismatch_events):
    posts["queue"].extend([
        _error(message="Unsupported value: 'temperature' does not support 0.3 with this model. "
                       "Only the default (1) value is supported."),
        _Resp(),
    ])
    _chat("future-openai-model", temperature=0.3)
    assert len(posts["payloads"]) == 2
    assert len(mismatch_events) == 1


@pytest.mark.parametrize("failure", (
    _error(param="max_output_tokens",
           message="Unsupported parameter: 'max_output_tokens' is not supported with this model."),
    _error(message="Invalid 'temperature': decimal above maximum value."),
    _error(type_="server_error"),
    _error(status=429),
    _error(status=500),
    _Resp(400, {"error": "temperature"}),
    _Resp(400, {"unexpected": True}),
))
def test_other_failures_are_never_retried(posts, mismatch_events, failure):
    posts["queue"].append(failure)
    with pytest.raises(RuntimeError):
        _chat("future-openai-model", temperature=0.7)
    assert len(posts["payloads"]) == 1
    assert mismatch_events == []


def test_network_failure_is_never_retried(posts, mismatch_events):
    posts["queue"].append(requests.exceptions.ConnectionError("down"))
    with pytest.raises(ConnectionError):
        _chat("future-openai-model", temperature=0.7)
    assert len(posts["payloads"]) == 1
    assert mismatch_events == []


def test_rejection_without_temperature_in_payload_is_not_retried(posts, mismatch_events):
    posts["queue"].append(_error())
    with pytest.raises(RuntimeError):
        _chat("gpt-6-luna", temperature=0.7)
    assert len(posts["payloads"]) == 1
    assert mismatch_events == []


def test_stream_paths_retry_once_on_temperature_rejection(posts, mismatch_events):
    posts["queue"].extend([_error(), _stream_ok("streamed")])
    tokens = list(_backend("future-openai-model").chat_stream(
        messages=[{"role": "user", "content": "hi"}], temperature=0.7,
    ))
    assert "streamed" in tokens

    posts["queue"].extend([_error(), _stream_ok("telemetry")])
    _chat("future-openai-model", temperature=0.7, capture_telemetry=True)

    temps = ["temperature" in p for p in posts["payloads"]]
    assert temps == [True, False, True, False]
    assert len(mismatch_events) == 2


def test_utility_with_stale_capability_metadata_does_not_collapse_to_none(posts, mismatch_events):
    posts["queue"].extend([_error(), _Resp(body=_ok_body("Dream summary"))])
    result = _backend("future-openai-model").complete_utility("summarize", temperature=0.2)
    assert result == "Dream summary"
    assert len(mismatch_events) == 1


def test_recorder_failure_never_breaks_the_retried_request(posts, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("recorder down")
    monkeypatch.setattr(flight_recorder, "record_machine_event", broken)
    posts["queue"].extend([_error(), _Resp(body=_ok_body("still fine"))])
    response = _chat("future-openai-model", temperature=0.7)
    assert response["choices"][0]["message"]["content"] == "still fine"
