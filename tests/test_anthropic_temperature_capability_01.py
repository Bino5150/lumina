"""Anthropic temperature capability and bounded provider-proven fallback.

All HTTP is intercepted at requests.post; no Anthropic key or live call is used.
"""

import copy
import json

import pytest
import requests

import core.backends.anthropic_backend as anthropic_backend
from core.backends.anthropic_backend import AnthropicBackend
from core.backends.reasoning import NO_REASONING_CONTROL


_MESSAGES = [{"role": "user", "content": "hello"}]
_OK_BODY = {"content": [{"type": "text", "text": "SUMMARY: done"}]}


class _Response:
    def __init__(self, status=200, body=None, lines=()):
        self.status_code = status
        self._body = _OK_BODY if body is None else body
        self.text = json.dumps(self._body) if isinstance(self._body, dict) else str(self._body)
        self._lines = lines
        self.closed = False

    def json(self):
        if not isinstance(self._body, dict):
            raise ValueError("malformed JSON")
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} error")

    def iter_lines(self):
        yield from self._lines

    def close(self):
        self.closed = True


def _error(status=400, kind="invalid_request_error",
           message="temperature is deprecated for this model"):
    return _Response(status, {"type": "error", "error": {"type": kind, "message": message}})


def _backend(model):
    backend = AnthropicBackend.__new__(AnthropicBackend)
    backend.api_key = "test-key"
    backend.default_model = model
    backend.headers = {"Content-Type": "application/json", "x-api-key": "test-key"}
    backend.timeout = 30
    return backend


@pytest.fixture
def wire(monkeypatch):
    sent = []
    queue = []

    def post(url, **kwargs):
        sent.append((url, copy.deepcopy(kwargs)))
        return queue.pop(0) if queue else _Response()

    monkeypatch.setattr(requests, "post", post)
    return sent, queue


@pytest.fixture
def events(monkeypatch):
    import core.flight_recorder as recorder
    captured = []
    monkeypatch.setattr(recorder, "record_machine_event",
                        lambda name, **kwargs: captured.append((name, kwargs)))
    return captured


@pytest.mark.parametrize("model", [
    "claude-sonnet-5", "claude-sonnet-5-5", "claude-opus-5",
    "claude-opus-5-5", "claude-fable-5-1", "claude-opus-4-7-20261001",
])
def test_known_families_omit_temperature_from_chat(model, wire):
    sent, _ = wire
    assert _backend(model).chat(_MESSAGES, temperature=0.3) == _OK_BODY
    assert len(sent) == 1
    assert "temperature" not in sent[0][1]["json"]


@pytest.mark.parametrize("model", ["claude-sonnet-4-6", "claude-haiku-4-5-20251001"])
def test_older_models_preserve_requested_temperature(model, wire):
    sent, _ = wire
    _backend(model).chat(_MESSAGES, temperature=0.3)
    assert sent[0][1]["json"]["temperature"] == 0.3


@pytest.mark.parametrize("model,expected", [
    ("claude-sonnet-5", False), ("claude-sonnet-4-6", True),
])
def test_stream_uses_same_policy_and_preserves_stream_transport(model, expected, wire):
    sent, queue = wire
    queue.append(_Response(lines=(
        b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"ok"}}',
        b'data: {"type":"message_stop"}',
    )))
    assert "".join(_backend(model).chat_stream(_MESSAGES, temperature=0.4)) == "ok"
    assert len(sent) == 1
    assert sent[0][1]["stream"] is True
    assert sent[0][1]["json"]["stream"] is True
    assert ("temperature" in sent[0][1]["json"]) is expected
    if expected:
        assert sent[0][1]["json"]["temperature"] == 0.4


def test_utility_on_forbidden_model_has_exact_valid_wire_shape(wire):
    sent, _ = wire
    result = _backend("claude-sonnet-5").complete_utility(
        "summarize", prefill="SUMMARY:", max_tokens=100, temperature=0.3,
    )
    assert result == "done"
    assert len(sent) == 1
    assert sent[0][1]["json"] == {
        "model": "claude-sonnet-5", "max_tokens": 100,
        "messages": [
            {"role": "user", "content": "summarize"},
            {"role": "assistant", "content": "SUMMARY:"},
        ],
        "stream": False,
    }


def test_unknown_model_retry_removes_only_temperature_and_records_event(wire, events):
    sent, queue = wire
    rejected = _error()
    queue.extend((rejected, _Response()))
    backend = _backend("claude-future-9")
    response = backend.chat(
        [{"role": "system", "content": "brief"}, *_MESSAGES],
        tools=[{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
        temperature=0.3, max_tokens=50,
    )
    assert response == _OK_BODY
    assert len(sent) == 2
    first, second = (call[1]["json"] for call in sent)
    assert first["temperature"] == 0.3
    assert second == {k: v for k, v in first.items() if k != "temperature"}
    assert rejected.closed
    assert events == [("backend.capability_mismatch", {
        "severity": "warning", "backend": "anthropic", "model": "claude-future-9",
        "fields": {"parameter": "temperature", "action": "retried_once_without_parameter",
                   "capability_metadata": "stale"},
    })]


def test_stream_unknown_model_retries_once_without_temperature(wire, events):
    sent, queue = wire
    rejected = _error(message="temperature is not supported with this model")
    queue.extend((rejected, _Response(lines=(b'data: {"type":"message_stop"}',))))
    assert list(_backend("claude-future-9").chat_stream(_MESSAGES)) == []
    assert len(sent) == 2
    assert all(call[1]["stream"] is True for call in sent)
    assert sent[1][1]["json"] == {k: v for k, v in sent[0][1]["json"].items()
                                   if k != "temperature"}
    assert rejected.closed
    assert len(events) == 1


@pytest.mark.parametrize("message", [
    "temperature is deprecated for this model",
    "temperature is not supported with this model",
    "setting temperature to a non-default value is unsupported for this model",
    "temperature does not support 0.3 with this model. Only the default (1) value is supported.",
])
def test_explicit_provider_rejection_wording_is_recognized(message):
    assert anthropic_backend._is_temperature_rejection(_error(message=message))


@pytest.mark.parametrize("first", [
    _error(status=401), _error(status=429), _error(status=529),
    _error(message="max_tokens is deprecated for this model"),
    _error(message="temperature must be a number between 0 and 1"),
    _error(message="temperature 1.7 is unsupported as a numeric value"),
    _error(kind="authentication_error"),
    _Response(400, "not JSON"),
    _Response(400, {"error": {"type": "invalid_request_error", "message": 4}}),
])
def test_unrelated_errors_are_never_retried(first, wire, events):
    sent, queue = wire
    queue.append(first)
    with pytest.raises(RuntimeError):
        _backend("claude-future-9").chat(_MESSAGES)
    assert len(sent) == 1
    assert not first.closed
    assert events == []


def test_request_without_temperature_never_retries(wire, events):
    sent, queue = wire
    queue.append(_error())
    with pytest.raises(RuntimeError):
        _backend("claude-sonnet-5").chat(_MESSAGES)
    assert len(sent) == 1
    assert events == []


def test_retry_failure_keeps_existing_error_and_never_tries_third_time(wire, events):
    sent, queue = wire
    queue.extend((_error(), _error(message="max_tokens is invalid")))
    with pytest.raises(RuntimeError, match="max_tokens is invalid"):
        _backend("claude-future-9").chat(_MESSAGES)
    assert len(sent) == 2
    assert len(events) == 1


def test_telemetry_failure_does_not_break_retry(wire, monkeypatch):
    import core.flight_recorder as recorder
    sent, queue = wire
    queue.extend((_error(), _Response()))

    def fail(*args, **kwargs):
        raise RuntimeError("telemetry unavailable")

    monkeypatch.setattr(recorder, "record_machine_event", fail)
    assert _backend("claude-future-9").chat(_MESSAGES) == _OK_BODY
    assert len(sent) == 2


def test_temperature_membership_grants_no_other_capability(monkeypatch):
    model = "claude-2-temperature-only"
    monkeypatch.setattr(
        anthropic_backend, "_TEMPERATURE_FORBIDDEN_PREFIXES",
        anthropic_backend._TEMPERATURE_FORBIDDEN_PREFIXES + (model,),
    )
    backend = _backend(model)
    assert backend._accepts_temperature(model) is False
    assert backend.reasoning_capabilities(model) is NO_REASONING_CONTROL
    assert backend.supports_vision(model) is False
    assert backend.supports_vision_with_tools(model) is False
    assert backend.supports_required_tool_choice is False
