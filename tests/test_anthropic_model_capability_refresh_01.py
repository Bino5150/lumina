"""ANTHROPIC-MODEL-CAPABILITY-REFRESH-01 -- Haiku 5.5 live-failure regression.

Specimen: the first live Claude Haiku 5.5 chat turn returned HTTP 400
"`temperature` is deprecated for this model." (exact bytes recovered from the
local chat store; the chat UI rendered the backticks as Markdown). Two
independent defects: the sampling policy did not cover Haiku 5.5, and the
one-time recovery detector did not recognize the provider's backtick-quoted
wording, so it never retried.

Every fixture here is shaped like the real Anthropic wire format: error
envelopes carry ``type``/``error``/``request_id``, streams are real SSE event
sequences (``event:`` lines included) with signed thinking blocks, and model
discovery pages carry the Models API ``capabilities`` tree. The transport fake
is strict: an unscripted request fails the test instead of silently
succeeding. No Anthropic key or live call is used.
"""

import copy
import json

import pytest
import requests

import core.backends.anthropic_backend as anthropic_backend
from core.backends.anthropic_backend import AnthropicBackend
from core.backends.base import TerminationStatus
from core.backends.reasoning import NO_REASONING_CONTROL


_MESSAGES = [
    {"role": "system", "content": "You are Lumina."},
    {"role": "user", "content": "Good evening Lumina"},
]
_SECRET = "sk-ant-test-not-a-real-key"

# Exact provider message observed live on 2026-10-13 (backticks included).
_OBSERVED_MESSAGE = "`temperature` is deprecated for this model."

_ALL_EFFORTS = ("low", "medium", "high", "xhigh", "max")


# ---------------------------------------------------------------------------
# Strict recording transport
# ---------------------------------------------------------------------------

class _Response:
    def __init__(self, status=200, body=None, lines=()):
        self.status_code = status
        self._body = body
        if isinstance(body, (dict, list)):
            self.text = json.dumps(body)
        else:
            self.text = "" if body is None else str(body)
        self._lines = lines
        self.closed = False

    def json(self):
        if not isinstance(self._body, (dict, list)):
            raise ValueError("malformed JSON")
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} error")

    def iter_lines(self):
        yield from self._lines

    def close(self):
        self.closed = True


def _envelope(message, kind="invalid_request_error", status=400):
    return _Response(status, {
        "type": "error",
        "error": {"type": kind, "message": message},
        "request_id": "req_011CTestRequestId",
    })


def _message_body(content, stop_reason="end_turn"):
    return {
        "id": "msg_01Test", "type": "message", "role": "assistant",
        "model": "claude-haiku-5-5", "content": content,
        "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 12, "output_tokens": 7},
    }


_TEXT_OK = _message_body([
    {"type": "thinking", "thinking": "", "signature": "EqQBCkgSIGNhbm5vdC1zZWUtdGhpcw"},
    {"type": "text", "text": "Good evening!"},
])


def _sse(*events):
    """Render real SSE framing: an ``event:`` line, a ``data:`` line, a blank."""
    lines = []
    for event in events:
        lines.append(f"event: {event['type']}".encode())
        lines.append(f"data: {json.dumps(event)}".encode())
        lines.append(b"")
    return tuple(lines)


def _stream(text="Good evening!", stop_reason="end_turn", stop_details=None,
            signature="EqQBCkgSIGNhbm5vdC1zZWUtdGhpcw"):
    delta = {"stop_reason": stop_reason, "stop_sequence": None}
    if stop_details is not None:
        delta["stop_details"] = stop_details
    return _Response(200, None, _sse(
        {"type": "message_start", "message": {
            "id": "msg_01Test", "type": "message", "role": "assistant",
            "model": "claude-haiku-5-5", "content": [], "stop_reason": None,
            "usage": {"input_tokens": 12, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "signature_delta", "signature": signature}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": delta, "usage": {"output_tokens": 9}},
        {"type": "message_stop"},
    ))


class _Wire:
    def __init__(self):
        self.posts = []
        self.gets = []
        self.queue = []

    def payloads(self):
        return [call["json"] for call in self.posts]


@pytest.fixture
def wire(monkeypatch):
    w = _Wire()

    def post(url, **kwargs):
        w.posts.append({"url": url, **copy.deepcopy(kwargs)})
        if not w.queue:
            raise AssertionError(f"unscripted POST to {url}")
        item = w.queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def get(url, **kwargs):
        w.gets.append({"url": url, **copy.deepcopy(kwargs)})
        if not w.queue:
            raise AssertionError(f"unscripted GET to {url}")
        item = w.queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(requests, "get", get)
    return w


@pytest.fixture
def events(monkeypatch):
    import core.flight_recorder as recorder
    captured = []
    monkeypatch.setattr(recorder, "record_machine_event",
                        lambda name, **kwargs: captured.append((name, kwargs)))
    return captured


def _backend(model, key=_SECRET):
    backend = AnthropicBackend.__new__(AnthropicBackend)
    backend.api_key = key
    backend.default_model = model
    backend.headers = {"Content-Type": "application/json", "x-api-key": key,
                       "anthropic-version": "2023-06-01"}
    backend.timeout = 30
    return backend


def _no_sampling(payload):
    return not ({"temperature", "top_p", "top_k"} & set(payload))


# ---------------------------------------------------------------------------
# 1. The original specimen: Haiku 5.5 ordinary chat and streaming chat
# ---------------------------------------------------------------------------

def test_haiku_5_5_chat_sends_no_sampling_parameters_and_keeps_model(wire):
    wire.queue.append(_Response(200, _TEXT_OK))
    backend = _backend("claude-haiku-5-5")
    response = backend.chat(_MESSAGES, temperature=0.7, max_tokens=4096)
    assert len(wire.posts) == 1
    payload = wire.payloads()[0]
    assert payload["model"] == "claude-haiku-5-5"
    assert _no_sampling(payload), payload
    assert payload["system"] == "You are Lumina."
    assert payload["messages"] == [{"role": "user", "content": "Good evening Lumina"}]
    assert backend.extract_message(response) == {"role": "assistant",
                                                 "content": "Good evening!"}


def test_haiku_5_5_stream_uses_the_same_sampling_policy(wire):
    wire.queue.append(_stream())
    chunks = list(_backend("claude-haiku-5-5").chat_stream(_MESSAGES, temperature=0.7))
    assert "".join(chunks) == "Good evening!"
    assert len(wire.posts) == 1
    assert wire.posts[0]["stream"] is True
    payload = wire.payloads()[0]
    assert payload["model"] == "claude-haiku-5-5"
    assert payload["stream"] is True
    assert _no_sampling(payload), payload


def test_haiku_5_5_default_temperature_is_never_substituted(wire):
    """No 'harmless' replacement value (e.g. 1) is ever sent in its place."""
    for temperature in (0.7, 0.3, 1, 0.0):
        wire.queue.append(_Response(200, _TEXT_OK))
        _backend("claude-haiku-5-5").chat(_MESSAGES, temperature=temperature)
    assert all(_no_sampling(p) for p in wire.payloads())


# ---------------------------------------------------------------------------
# 2. Family regression matrix -- identical policy on both request builders
# ---------------------------------------------------------------------------

_DOCUMENTED_REJECTS = (
    "claude-haiku-5-5",
    "claude-sonnet-5", "claude-sonnet-5-5",
    "claude-opus-5", "claude-opus-5-5",
    "claude-fable-5", "claude-fable-5-1",
    "claude-mythos-5", "claude-mythos-5-1",
    "claude-opus-4-7", "claude-opus-4-8",
)
_DOCUMENTED_ACCEPTS = (
    "claude-haiku-4-5", "claude-haiku-4-5-20251001",
    "claude-sonnet-4-6", "claude-opus-4-6",
    "claude-sonnet-4-5", "claude-sonnet-4-5-20250929",
    "claude-opus-4-5-20251101", "claude-opus-4-1-20250805",
    "claude-opus-4-20250514", "claude-sonnet-4-20250514",
    "claude-3-7-sonnet-20250219", "claude-3-5-haiku-20241022",
)


@pytest.mark.parametrize("model", _DOCUMENTED_REJECTS)
def test_documented_rejecting_families_omit_sampling_on_both_paths(model, wire):
    wire.queue.extend((_Response(200, _TEXT_OK), _stream()))
    backend = _backend(model)
    backend.chat(_MESSAGES, temperature=0.4)
    "".join(backend.chat_stream(_MESSAGES, temperature=0.4))
    assert [p["model"] for p in wire.payloads()] == [model, model]
    assert all(_no_sampling(p) for p in wire.payloads())
    assert anthropic_backend.sampling_support(model) is \
        anthropic_backend.SamplingSupport.DOCUMENTED_REJECTS


@pytest.mark.parametrize("model", _DOCUMENTED_ACCEPTS)
def test_documented_legacy_families_keep_requested_temperature_on_both_paths(model, wire):
    wire.queue.extend((_Response(200, _TEXT_OK), _stream()))
    backend = _backend(model)
    backend.chat(_MESSAGES, temperature=0.4)
    "".join(backend.chat_stream(_MESSAGES, temperature=0.4))
    for payload in wire.payloads():
        assert payload["temperature"] == 0.4
        assert "top_p" not in payload and "top_k" not in payload
    assert anthropic_backend.sampling_support(model) is \
        anthropic_backend.SamplingSupport.DOCUMENTED_ACCEPTS


@pytest.mark.parametrize("model", [
    "claude-nova-7", "claude-haiku-6", "claude-opus-4-9", "claude-sonnet-4-7",
    "claude-opus-40", "claude-3x-experimental", "", None,
])
def test_unknown_or_malformed_models_default_to_provider_sampling(model, wire):
    """Unknown means 'not known to accept', so no sampling field is sent:
    the provider's own default applies and the request cannot 400 on it."""
    wire.queue.extend((_Response(200, _TEXT_OK), _stream()))
    backend = _backend(model)
    backend.chat(_MESSAGES, temperature=0.4)
    "".join(backend.chat_stream(_MESSAGES, temperature=0.4))
    assert all(_no_sampling(p) for p in wire.payloads())
    assert [p["model"] for p in wire.payloads()] == [model, model]
    assert anthropic_backend.sampling_support(model) is \
        anthropic_backend.SamplingSupport.UNKNOWN


@pytest.mark.parametrize("model", ["claude-nova-7", "claude-haiku-6"])
def test_unknown_model_gets_no_invented_capability_claims(model):
    backend = _backend(model)
    assert backend.reasoning_capabilities(model) is NO_REASONING_CONTROL
    assert backend.supports_required_tool_choice is False


# ---------------------------------------------------------------------------
# 3. Haiku 5.5 native tools -- complete translated batches, ids and arguments
# ---------------------------------------------------------------------------

_TOOLS = [
    {"type": "function", "function": {
        "name": "read_file", "description": "Read a file.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                       "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "search_memory", "description": "Search memory.",
        "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}}},
]


def test_haiku_5_5_native_tool_round_trip(wire):
    history = _MESSAGES + [
        {"role": "assistant", "content": "Checking.", "tool_calls": [
            {"id": "toolu_01A", "type": "function",
             "function": {"name": "read_file", "arguments": json.dumps({"path": "a.txt"})}},
            {"id": "toolu_01B", "type": "function",
             "function": {"name": "search_memory", "arguments": json.dumps({"q": "x"})}},
        ]},
        {"role": "tool", "tool_call_id": "toolu_01A", "name": "read_file", "content": "A"},
        {"role": "tool", "tool_call_id": "toolu_01B", "name": "search_memory", "content": "B"},
    ]
    wire.queue.append(_Response(200, _message_body([
        {"type": "thinking", "thinking": "", "signature": "SIG-NEVER-SURFACED"},
        {"type": "tool_use", "id": "toolu_01C", "name": "read_file",
         "input": {"path": "b.txt"}},
        {"type": "tool_use", "id": "toolu_01D", "name": "search_memory",
         "input": {"q": "ünïcode/slash"}},
    ], stop_reason="tool_use")))
    backend = _backend("claude-haiku-5-5")
    response = backend.chat(history, tools=_TOOLS, temperature=0.7)

    payload = wire.payloads()[0]
    assert _no_sampling(payload)
    assert "tool_choice" not in payload
    assert payload["tools"] == [
        {"name": "read_file", "description": "Read a file.",
         "input_schema": _TOOLS[0]["function"]["parameters"]},
        {"name": "search_memory", "description": "Search memory.",
         "input_schema": _TOOLS[1]["function"]["parameters"]},
    ]
    assistant_turn, result_a, result_b = payload["messages"][1:]
    assert assistant_turn == {"role": "assistant", "content": [
        {"type": "text", "text": "Checking."},
        {"type": "tool_use", "id": "toolu_01A", "name": "read_file", "input": {"path": "a.txt"}},
        {"type": "tool_use", "id": "toolu_01B", "name": "search_memory", "input": {"q": "x"}},
    ]}
    assert result_a["content"] == [{"type": "tool_result", "tool_use_id": "toolu_01A", "content": "A"}]
    assert result_b["content"] == [{"type": "tool_result", "tool_use_id": "toolu_01B", "content": "B"}]

    message = backend.extract_message(response)
    assert backend.extract_termination(response) is TerminationStatus.COMPLETE
    assert [backend.parse_tool_call(tc) for tc in message["tool_calls"]] == [
        ("read_file", {"path": "b.txt"}), ("search_memory", {"q": "ünïcode/slash"}),
    ]
    assert [tc["id"] for tc in message["tool_calls"]] == ["toolu_01C", "toolu_01D"]
    assert "SIG-NEVER-SURFACED" not in json.dumps(message)


# ---------------------------------------------------------------------------
# 4. One-time recovery investigation -- the real _post_messages()
# ---------------------------------------------------------------------------

def test_observed_envelope_is_recognized_by_the_detector():
    assert anthropic_backend._is_temperature_rejection(_envelope(_OBSERVED_MESSAGE))


@pytest.mark.parametrize("message", [
    _OBSERVED_MESSAGE,
    "temperature is deprecated for this model",
    "'temperature' is not supported with this model",
    '"temperature" is unsupported for this model.',
    "Setting `temperature` to a non-default value is not supported for this model.",
    "`temperature` does not support 0.7 with this model. Only the default (1) value is supported.",
])
def test_documented_and_quoted_rejection_wordings_are_recognized(message):
    assert anthropic_backend._is_temperature_rejection(_envelope(message))


@pytest.mark.parametrize("message", [
    "`temperature`: Input should be less than or equal to 1",
    "`temperature` must be a number between 0 and 1",
    "`top_p` is deprecated for this model.",
    "`max_tokens` is deprecated for this model.",
    "messages: final assistant content cannot end with trailing whitespace",
])
def test_unrelated_quoted_400s_are_not_recognized(message):
    assert not anthropic_backend._is_temperature_rejection(_envelope(message))


# A documented-accepting family is the only way temperature is ever sent, so a
# rejection from one is genuinely stale capability data -- the one case the
# narrow recovery exists for.
_STALE = "claude-sonnet-4-6"


@pytest.mark.parametrize("streaming", [False, True])
def test_observed_rejection_then_success_retries_exactly_once(streaming, wire, events):
    rejected = _envelope(_OBSERVED_MESSAGE)
    wire.queue.extend((rejected, _stream() if streaming else _Response(200, _TEXT_OK)))
    backend = _backend(_STALE)
    if streaming:
        assert "".join(backend.chat_stream(_MESSAGES, temperature=0.7)) == "Good evening!"
    else:
        assert backend.chat(_MESSAGES, temperature=0.7) == _TEXT_OK
    first, second = wire.payloads()
    assert first["temperature"] == 0.7
    assert second == {k: v for k, v in first.items() if k != "temperature"}
    if streaming:
        assert all(call["stream"] is True for call in wire.posts)
    else:
        assert all("stream" not in call for call in wire.posts)
    assert rejected.closed
    assert [name for name, _ in events] == ["backend.capability_mismatch"]
    assert _SECRET not in json.dumps(events)


@pytest.mark.parametrize("streaming", [False, True])
def test_observed_rejection_then_second_failure_surfaces_the_second(streaming, wire, events):
    wire.queue.extend((_envelope(_OBSERVED_MESSAGE),
                       _envelope("Overloaded", kind="overloaded_error", status=529)))
    backend = _backend(_STALE)
    with pytest.raises(RuntimeError, match=r"HTTP 529\): Overloaded"):
        if streaming:
            list(backend.chat_stream(_MESSAGES))
        else:
            backend.chat(_MESSAGES)
    assert len(wire.posts) == 2
    assert len(events) == 1


@pytest.mark.parametrize("first", [
    _envelope("messages.0.content: Field required"),
    _envelope("Your credit balance is too low.", kind="billing_error", status=402),
    _envelope("invalid x-api-key", kind="authentication_error", status=401),
    _envelope("Number of request tokens has exceeded your rate limit.",
              kind="rate_limit_error", status=429),
    _Response(400, "<html>Bad Request</html>"),
    _Response(400, {"type": "error", "error": "temperature is deprecated"}),
])
def test_unrelated_or_malformed_errors_are_never_retried(first, wire, events):
    wire.queue.append(first)
    with pytest.raises(RuntimeError):
        _backend(_STALE).chat(_MESSAGES)
    assert len(wire.posts) == 1
    assert events == []


@pytest.mark.parametrize("failure,expected", [
    (requests.exceptions.ConnectionError("reset"), ConnectionError),
    (requests.exceptions.Timeout("slow"), TimeoutError),
])
def test_transport_failure_is_never_blindly_retried(failure, expected, wire, events):
    wire.queue.append(failure)
    with pytest.raises(expected):
        _backend(_STALE).chat(_MESSAGES)
    assert len(wire.posts) == 1
    assert events == []


def test_rejection_when_temperature_was_never_sent_is_not_retried(wire, events):
    wire.queue.append(_envelope(_OBSERVED_MESSAGE))
    with pytest.raises(RuntimeError, match="invalid_request|generic"):
        _backend("claude-haiku-5-5").chat(_MESSAGES)
    assert len(wire.posts) == 1
    assert events == []


# ---------------------------------------------------------------------------
# 5. Capability discovery -- provider-reported effort, documented fallback
# ---------------------------------------------------------------------------

def _model_entry(model_id, effort=None, extra_caps=None):
    caps = {
        "image_input": {"supported": True},
        "thinking": {"supported": True, "types": {
            "enabled": {"supported": False}, "adaptive": {"supported": True}}},
    }
    if effort is not None:
        caps["effort"] = effort
    caps.update(extra_caps or {})
    return {"type": "model", "id": model_id, "display_name": model_id,
            "created_at": "2026-09-01T00:00:00Z", "max_input_tokens": 1000000,
            "max_tokens": 128000, "capabilities": caps}


def _effort(*levels, supported=True):
    tree = {"supported": supported}
    for level in _ALL_EFFORTS:
        tree[level] = {"supported": level in levels}
    return tree


def _page(*entries):
    return _Response(200, {"data": list(entries), "has_more": False,
                           "first_id": None, "last_id": None})


def test_documented_haiku_5_5_reasoning_is_available_offline_without_io(wire):
    backend = _backend("claude-haiku-5-5")
    caps = backend.reasoning_capabilities("claude-haiku-5-5")
    assert caps.efforts == _ALL_EFFORTS
    assert caps.default_effort == "medium"
    assert caps.mandatory is False and caps.supports_budget is False
    assert backend.reasoning_capabilities_ready("claude-haiku-5-5") is True
    assert wire.posts == [] and wire.gets == []


def test_haiku_5_5_effort_reaches_the_wire_on_both_paths(wire):
    wire.queue.extend((_Response(200, _TEXT_OK), _stream()))
    backend = _backend("claude-haiku-5-5")
    backend.chat(_MESSAGES, reasoning_effort="low")
    "".join(backend.chat_stream(_MESSAGES, reasoning_effort="xhigh"))
    assert [p["output_config"] for p in wire.payloads()] == [
        {"effort": "low"}, {"effort": "xhigh"}]
    assert all("thinking" not in p for p in wire.payloads())


def test_provider_reported_effort_outranks_and_is_cached_without_per_turn_io(wire):
    wire.queue.append(_page(
        _model_entry("claude-opus-5-5", _effort(*_ALL_EFFORTS)),
        _model_entry("claude-sonnet-4-6", _effort("low", "medium", "high", "max")),
        _model_entry("claude-haiku-5-5", _effort("low", "medium", "high")),
    ))
    backend = _backend("claude-opus-5-5")
    assert backend.reasoning_capabilities_ready("claude-opus-5-5") is False
    assert backend.refresh_reasoning_capabilities() is True
    assert len(wire.gets) == 1

    opus = backend.reasoning_capabilities("claude-opus-5-5")
    assert opus.efforts == _ALL_EFFORTS
    assert opus.default_effort is None  # the Models API does not report defaults
    assert backend.reasoning_capabilities("claude-sonnet-4-6").efforts == \
        ("low", "medium", "high", "max")
    haiku = backend.reasoning_capabilities("claude-haiku-5-5")
    assert haiku.efforts == ("low", "medium", "high")  # provider outranks documentation
    assert haiku.default_effort == "medium"            # documented default still applies
    assert backend.reasoning_capabilities_ready("claude-opus-5-5") is True

    for _ in range(3):
        backend.reasoning_capabilities("claude-opus-5-5")
    assert len(wire.gets) == 1
    assert _SECRET not in json.dumps(wire.gets[0]["url"])


def test_explicit_provider_effort_unsupported_overrides_documentation(wire):
    wire.queue.append(_page(_model_entry("claude-haiku-5-5", {"supported": False})))
    backend = _backend("claude-haiku-5-5")
    backend.refresh_reasoning_capabilities()
    assert backend.reasoning_capabilities("claude-haiku-5-5") is NO_REASONING_CONTROL


@pytest.mark.parametrize("effort_tree", [
    None,                                     # no effort capability reported
    "yes",                                    # wrong type
    {"supported": "true"},                    # stringly-typed flag
    {"supported": True},                      # supported but no level advertised
    {"supported": True, "low": True, "high": {"supported": 1}},
    {"supported": True, "ultra": {"supported": True}},  # unknown label only
])
def test_malformed_capability_metadata_falls_back_truthfully(effort_tree, wire):
    entries = [_model_entry("claude-haiku-5-5", effort_tree),
               _model_entry("claude-nova-7", effort_tree)]
    entries.append({"type": "model", "id": "claude-broken", "capabilities": ["x"]})
    entries.append({"type": "model", "id": "claude-broken-2"})
    wire.queue.append(_page(*entries))
    backend = _backend("claude-haiku-5-5")
    assert backend.refresh_reasoning_capabilities() is True
    assert backend.reasoning_capabilities("claude-haiku-5-5").efforts == _ALL_EFFORTS
    for model in ("claude-nova-7", "claude-broken", "claude-broken-2"):
        assert backend.reasoning_capabilities(model) is NO_REASONING_CONTROL


def test_failed_discovery_reports_failure_and_keeps_offline_truth(wire):
    wire.queue.append(_envelope("invalid x-api-key", kind="authentication_error", status=401))
    backend = _backend("claude-nova-7")
    assert backend.refresh_reasoning_capabilities() is False
    assert backend.reasoning_capabilities_ready("claude-nova-7") is False
    assert backend.reasoning_capabilities("claude-nova-7") is NO_REASONING_CONTROL
    assert backend.reasoning_capabilities("claude-haiku-5-5").efforts == _ALL_EFFORTS
    assert len(wire.gets) == 1


def test_discovery_listing_success_does_not_change_sampling_policy(wire):
    """A model appearing in the catalog proves nothing about request shape."""
    wire.queue.append(_page(_model_entry("claude-nova-7", _effort(*_ALL_EFFORTS))))
    backend = _backend("claude-nova-7")
    backend.refresh_reasoning_capabilities()
    wire.queue.append(_Response(200, _TEXT_OK))
    backend.chat(_MESSAGES, temperature=0.7)
    assert _no_sampling(wire.payloads()[0])


# ---------------------------------------------------------------------------
# 6. Model / account / backend switch -- no replay, no identity confusion
# ---------------------------------------------------------------------------

def test_switching_models_and_keys_carries_no_state_between_requests(wire):
    history = _MESSAGES + [
        {"role": "assistant", "content": "Earlier answer from another model."},
        {"role": "user", "content": "And now?"},
    ]
    wire.queue.extend((_Response(200, _TEXT_OK), _Response(200, _TEXT_OK),
                       _Response(200, _TEXT_OK)))
    first = _backend("claude-haiku-4-5", key="sk-ant-account-one")
    second = _backend("claude-haiku-5-5", key="sk-ant-account-two")
    first.chat(history, temperature=0.7)
    second.chat(history, temperature=0.7)
    first.default_model = "claude-haiku-5-5"
    first.chat(history, temperature=0.7)

    p1, p2, p3 = wire.payloads()
    assert (p1["model"], p1.get("temperature")) == ("claude-haiku-4-5", 0.7)
    assert (p2["model"], "temperature" in p2) == ("claude-haiku-5-5", False)
    assert (p3["model"], "temperature" in p3) == ("claude-haiku-5-5", False)
    assert [c["headers"]["x-api-key"] for c in wire.posts] == [
        "sk-ant-account-one", "sk-ant-account-two", "sk-ant-account-one"]
    for payload in wire.payloads():
        serialized = json.dumps(payload["messages"])
        assert '"thinking"' not in serialized and "signature" not in serialized


# ---------------------------------------------------------------------------
# 7. Streamed refusal / truncation / mid-stream error -- no false Final
# ---------------------------------------------------------------------------

def test_clean_stream_never_surfaces_the_thinking_signature(wire):
    wire.queue.append(_stream(signature="SECRET-SIGNATURE-BLOB"))
    chunks = list(_backend("claude-haiku-5-5").chat_stream(_MESSAGES))
    assert "SECRET-SIGNATURE-BLOB" not in "".join(chunks)
    assert chunks == ["Good evening!"]


@pytest.mark.parametrize("stop_reason,details,pattern", [
    ("refusal", {"type": "refusal", "category": "cyber", "explanation": None},
     r"stop_reason=refusal.*category=cyber"),
    ("refusal", None, r"stop_reason=refusal"),
    ("max_tokens", None, r"stop_reason=max_tokens"),
    ("model_context_window_exceeded", None, r"stop_reason=model_context_window_exceeded"),
])
def test_stream_bad_terminal_raises_after_partial_text(stop_reason, details, pattern, wire):
    wire.queue.append(_stream(text="Partial", stop_reason=stop_reason, stop_details=details))
    stream = _backend("claude-haiku-5-5").chat_stream(_MESSAGES)
    assert next(stream) == "Partial"
    with pytest.raises(RuntimeError, match=pattern):
        list(stream)


def test_mid_stream_error_event_raises_classified_error(wire):
    lines = _stream()._lines[:12] + _sse(
        {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})
    wire.queue.append(_Response(200, None, lines))
    with pytest.raises(RuntimeError, match=r"Anthropic error \(generic, mid-stream\): Overloaded"):
        list(_backend("claude-haiku-5-5").chat_stream(_MESSAGES))


@pytest.mark.parametrize("stop_reason", ["end_turn", "stop_sequence"])
def test_stream_clean_terminal_is_unchanged(stop_reason, wire):
    wire.queue.append(_stream(stop_reason=stop_reason))
    assert "".join(_backend("claude-haiku-5-5").chat_stream(_MESSAGES)) == "Good evening!"


def test_stream_error_message_is_an_agent_error_response_not_a_final():
    from core.agent import ERROR_RESPONSE_PREFIXES
    assert "[Stream error:" in ERROR_RESPONSE_PREFIXES


@pytest.mark.parametrize("stop_reason,expected", [
    ("end_turn", TerminationStatus.COMPLETE),
    ("tool_use", TerminationStatus.COMPLETE),
    ("max_tokens", TerminationStatus.INCOMPLETE),
    ("model_context_window_exceeded", TerminationStatus.INCOMPLETE),
    ("refusal", TerminationStatus.UNKNOWN),
    ("pause_turn", TerminationStatus.UNKNOWN),
    (None, TerminationStatus.UNKNOWN),
])
def test_non_streaming_termination_mapping(stop_reason, expected):
    backend = _backend("claude-haiku-5-5")
    assert backend.extract_termination(_message_body([], stop_reason=stop_reason)) is expected
