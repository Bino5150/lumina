"""OPENROUTER-PROVIDER-ROUTING-01 -- opt-in upstream-provider pin for OpenRouter.

LUMINA-CONVERSATION-CONTINUITY-01 Stage 1D: the same synthetic request failed
continuity 5/5 when OpenRouter routed it to OpenInference and passed 3/3 when
pinned to Crusoe. Lumina had no way to request a provider, so OpenRouter's
load balancer chose freely. prefs["backend_provider_routing"]["openrouter"]
now becomes OpenRouter's request-body `provider` object.

Covered here:
  1. Default: no routing configured -> no `provider` field; payloads are
     otherwise unchanged (exact golden payloads).
  2. Pin: the exact validated object rides every OpenRouter payload builder
     (chat, streamed-telemetry chat, chat_stream, complete_utility), as a
     copy that can't mutate config.
  3. Malformed config fails closed: nothing is sent.
  4. No other backend ever emits `provider`, configured or malformed.
  5. Capabilities: tools still offered on a vision-history turn with a pin.
  6. Telemetry: provider.dispatch carries the requested routing for WORK and
     gate requests; provider.pin_mismatch records a served provider outside
     the pin.
"""
import json
import types

import pytest
import requests

import config
import core.agent as agent_module
from core.backends.deepseek import DeepSeekBackend
from core.backends.groq import GroqBackend
from core.backends.kimi import KimiBackend
from core.backends.llamacpp import LlamaCppBackend
from core.backends.loader import CustomBackend
from core.backends.lmstudio import LMStudioBackend
from core.backends.omniroute import OmniRouteBackend
from core.backends.openrouter import (
    OpenRouterBackend,
    ProviderRoutingConfigError,
    validate_provider_routing,
)
from core.backends.qwen import QwenBackend
from core.backends.vllm import VLLMBackend

MODEL = "z-ai/glm-5.3-flash"
PIN = {
    "only": ["crusoe/fp4"],
    "allow_fallbacks": False,
    "require_parameters": True,
    "max_price": {"prompt": 0.15, "completion": 0.5},
}
PIN_PREFS = {"openrouter": PIN}

# A /models entry advertising what the live z-ai/glm-5.3-flash advertises:
# image input + tools (vision-with-tools) and a reasoning object.
_MODEL_ENTRY = {
    "id": MODEL,
    "architecture": {"input_modalities": ["text", "image", "video"]},
    "supported_parameters": ["tools", "tool_choice", "reasoning", "max_tokens", "temperature"],
    "reasoning": {"supported_efforts": ["low", "medium", "high"], "default_effort": "medium"},
}


class _JsonResp:
    status_code = 200
    text = ""

    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


class _StreamResp:
    status_code = 200

    def __init__(self, frames):
        self._lines = [f"data: {json.dumps(f)}".encode() for f in frames] + [b"data: [DONE]"]

    def raise_for_status(self):
        return None

    def iter_lines(self):
        return iter(self._lines)

    def close(self):
        return None


def _ok_body(provider="Crusoe", content="ok"):
    return {"provider": provider, "model": MODEL,
            "choices": [{"message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}]}


def _stream_frames(provider="Crusoe"):
    return [
        {"provider": provider, "model": MODEL,
         "choices": [{"delta": {"role": "assistant", "content": "ok"}}]},
        {"provider": provider, "model": MODEL,
         "choices": [{"delta": {}, "finish_reason": "stop", "native_finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}},
    ]


@pytest.fixture
def wire(monkeypatch):
    """Intercept requests.post/get immediately before transport, recording
    every wire payload. Discovery returns one glm-5.3-flash entry."""
    posts = []

    def fake_post(*args, **kwargs):
        posts.append(kwargs["json"])
        if kwargs.get("stream"):
            return _StreamResp(_stream_frames())
        if kwargs["json"].get("stream"):
            return _StreamResp(_stream_frames())
        return _JsonResp(_ok_body())

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _JsonResp({"data": [_MODEL_ENTRY]}))
    return posts


def _openrouter():
    backend = OpenRouterBackend(api_key="test-key")
    backend._model = MODEL
    return backend


def _route(monkeypatch, value):
    monkeypatch.setattr(config, "BACKEND_PROVIDER_ROUTING", value)


def _drive_every_path(backend):
    """One request through each OpenRouter payload builder."""
    msgs = [{"role": "user", "content": "hi"}]
    backend.chat(messages=msgs, max_tokens=64)
    backend.chat(messages=msgs, max_tokens=64, capture_telemetry=True)
    list(backend.chat_stream(messages=msgs, max_tokens=64))
    backend.complete_utility("name this chat", prefill="TITLE:", max_tokens=30)


# ── 1. Default is unchanged ──────────────────────────────────────────────

@pytest.mark.parametrize("value", [None, {}, {"openrouter": None}, {"openrouter": {}},
                                   {"some_other_backend": {"only": ["x"]}}])
def test_unconfigured_routing_validates_to_none(value):
    assert validate_provider_routing(value) is None


@pytest.mark.parametrize("value", [{}, None, {"openrouter": {}}])
def test_default_payloads_carry_no_provider_field_on_any_path(monkeypatch, wire, value):
    _route(monkeypatch, value)
    _drive_every_path(_openrouter())
    assert len(wire) == 4
    assert all("provider" not in payload for payload in wire)


def test_default_chat_wire_payload_is_exactly_unchanged(monkeypatch, wire):
    _route(monkeypatch, {})
    _openrouter().chat(messages=[{"role": "user", "content": "hi"}], max_tokens=64)
    assert wire == [{"model": MODEL, "messages": [{"role": "user", "content": "hi"}],
                     "temperature": 0.7, "stream": False, "max_tokens": 64}]


def test_default_stream_wire_payload_is_exactly_unchanged(monkeypatch, wire):
    _route(monkeypatch, {})
    list(_openrouter().chat_stream(messages=[{"role": "user", "content": "hi"}], max_tokens=64))
    assert wire == [{"model": MODEL, "messages": [{"role": "user", "content": "hi"}],
                     "temperature": 0.7, "stream": True, "max_tokens": 64}]


# ── 2. The pin rides every path, exactly ─────────────────────────────────

def test_pin_is_attached_exactly_on_every_openrouter_path(monkeypatch, wire):
    _route(monkeypatch, PIN_PREFS)
    _drive_every_path(_openrouter())
    assert len(wire) == 4
    assert all(payload["provider"] == PIN for payload in wire)


def test_pin_chat_wire_payload_is_baseline_plus_provider_only(monkeypatch, wire):
    _route(monkeypatch, PIN_PREFS)
    _openrouter().chat(messages=[{"role": "user", "content": "hi"}], max_tokens=64)
    assert wire == [{"model": MODEL, "messages": [{"role": "user", "content": "hi"}],
                     "temperature": 0.7, "stream": False, "max_tokens": 64, "provider": PIN}]


def test_payload_provider_is_a_copy_never_aliasing_config(monkeypatch, wire):
    prefs = json.loads(json.dumps(PIN_PREFS))
    _route(monkeypatch, prefs)
    backend = _openrouter()
    backend.chat(messages=[{"role": "user", "content": "hi"}], max_tokens=64)
    wire[0]["provider"]["only"].append("open-inference/fp4")
    wire[0]["provider"]["max_price"]["prompt"] = 99
    assert prefs == PIN_PREFS
    backend.chat(messages=[{"role": "user", "content": "hi"}], max_tokens=64)
    assert wire[1]["provider"] == PIN


def test_every_documented_routing_key_is_accepted():
    full = {"order": ["crusoe/fp4", "together"], "only": ["crusoe"], "ignore": ["open-inference"],
            "quantizations": ["fp4"], "allow_fallbacks": True, "require_parameters": False,
            "sort": "latency", "data_collection": "deny",
            "max_price": {"prompt": 1, "completion": 2.5, "request": 0.01, "image": 0.02}}
    assert validate_provider_routing({"openrouter": full}) == full


# ── 3. Malformed config fails closed ─────────────────────────────────────

MALFORMED = [
    ["openrouter"],
    {"openrouter": "crusoe/fp4"},
    {"openrouter": {"fallbacks": False}},
    {"openrouter": {"only": "crusoe/fp4"}},
    {"openrouter": {"only": []}},
    {"openrouter": {"only": [""]}},
    {"openrouter": {"only": [" crusoe/fp4"]}},
    {"openrouter": {"only": [7]}},
    {"openrouter": {"order": None}},
    {"openrouter": {"allow_fallbacks": "false"}},
    {"openrouter": {"require_parameters": 1}},
    {"openrouter": {"sort": "cheapest"}},
    {"openrouter": {"sort": {"by": "price"}}},
    {"openrouter": {"data_collection": "no"}},
    {"openrouter": {"max_price": []}},
    {"openrouter": {"max_price": {}}},
    {"openrouter": {"max_price": {"input": 0.15}}},
    {"openrouter": {"max_price": {"prompt": -1}}},
    {"openrouter": {"max_price": {"prompt": 0}}},
    {"openrouter": {"max_price": {"prompt": True}}},
    {"openrouter": {"max_price": {"prompt": "0.15"}}},
    {"openrouter": {"max_price": {"prompt": float("inf")}}},
    {"openrouter": {"max_price": {"prompt": float("nan")}}},
]


@pytest.mark.parametrize("value", MALFORMED)
def test_malformed_routing_is_rejected(value):
    with pytest.raises(ProviderRoutingConfigError):
        validate_provider_routing(value)


@pytest.mark.parametrize("value", MALFORMED)
def test_malformed_routing_sends_nothing_on_any_path(monkeypatch, wire, value):
    _route(monkeypatch, value)
    backend = _openrouter()
    msgs = [{"role": "user", "content": "hi"}]
    with pytest.raises(ProviderRoutingConfigError, match="request not sent"):
        backend.chat(messages=msgs, max_tokens=64)
    with pytest.raises(ProviderRoutingConfigError, match="request not sent"):
        backend.chat(messages=msgs, max_tokens=64, capture_telemetry=True)
    with pytest.raises(ProviderRoutingConfigError, match="request not sent"):
        list(backend.chat_stream(messages=msgs, max_tokens=64))
    # complete_utility() keeps its never-raises contract; it still sends nothing.
    assert backend.complete_utility("name this chat", prefill="TITLE:", max_tokens=30) is None
    assert wire == []


# ── 4. No other backend emits `provider` ─────────────────────────────────

def _other_backends():
    def made(cls, **kw):
        backend = cls(**kw)
        backend._model = "some-model"
        return backend
    return [
        ("lmstudio", made(LMStudioBackend)),
        ("llamacpp", made(LlamaCppBackend)),
        ("vllm", made(VLLMBackend)),
        ("deepseek", made(DeepSeekBackend, api_key="k")),
        ("groq", made(GroqBackend, api_key="k")),
        ("kimi", made(KimiBackend, api_key="k")),
        ("qwen", made(QwenBackend, api_key="k")),
        ("omniroute", made(OmniRouteBackend, api_key="k")),
        ("custom", made(CustomBackend, base_url="http://custom.invalid/v1", api_key="k")),
    ]


@pytest.mark.parametrize("value", [PIN_PREFS, {"openrouter": {"only": "malformed"}}])
@pytest.mark.parametrize("name,backend", _other_backends(), ids=lambda v: v if isinstance(v, str) else "")
def test_other_backends_never_emit_provider(monkeypatch, wire, value, name, backend):
    _route(monkeypatch, value)
    msgs = [{"role": "user", "content": "hi"}]
    backend.chat(messages=msgs, max_tokens=64)
    list(backend.chat_stream(messages=msgs, max_tokens=64))
    assert len(wire) == 2
    assert all("provider" not in payload for payload in wire), name


# ── 5. Capabilities are unchanged by the pin ─────────────────────────────

@pytest.mark.parametrize("value", [{}, PIN_PREFS])
def test_tools_still_offered_on_vision_history_with_or_without_pin(monkeypatch, wire, value):
    _route(monkeypatch, value)
    backend = _openrouter()
    backend.refresh_reasoning_capabilities()
    tools = [{"type": "function", "function": {"name": "get_time", "parameters": {"type": "object"}}}]
    history = [
        {"role": "user", "content": [{"type": "text", "text": "[1 image(s) routed to the vision lane]"},
                                     {"type": "text", "text": "[Vision specialist observation] cones"}]},
        {"role": "assistant", "content": "Three cones. Which marker?"},
        {"role": "user", "content": "The orange one."},
    ]
    backend.chat(messages=history, tools=tools, max_tokens=64, reasoning_effort="medium")
    payload = wire[-1]
    assert payload["tools"] == tools and payload["tool_choice"] == "auto"
    assert payload["reasoning"] == {"effort": "medium"}
    assert backend.supports_vision_with_tools(MODEL) is True
    assert ("provider" in payload) is bool(value)


# ── 6. Telemetry ──────────────────────────────────────────────────────────

@pytest.fixture
def fr(monkeypatch):
    events = []
    monkeypatch.setattr(agent_module, "_fr_machine",
                        lambda agent, event_type, **kw: events.append((event_type, kw.get("fields") or {})))
    return events


def _of(events, kind):
    return [f for t, f in events if t == kind]


def _fake_agent(llm):
    return types.SimpleNamespace(
        owner=False, llm=llm, ctx=None, channel_id="test-channel",
        on_response_token=lambda text: None,
    )


def _dispatch(monkeypatch, served="Crusoe", phase="work"):
    monkeypatch.setattr(requests, "post", lambda *a, **kw: _JsonResp(_ok_body(provider=served)))
    llm = _openrouter()
    llm._reasoning_cache_ready = True
    kwargs = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 64}
    return agent_module._provider_chat_or_error(_fake_agent(llm), kwargs, None, set(), phase=phase)


def test_dispatch_records_requested_routing_and_no_mismatch_when_served_in_pin(monkeypatch, fr):
    _route(monkeypatch, PIN_PREFS)
    response, err = _dispatch(monkeypatch, served="Crusoe")
    assert err is None and response["provider"] == "Crusoe"
    assert _of(fr, "provider.dispatch")[0]["provider_routing"] == PIN
    assert _of(fr, "provider.response")[0]["provider_name"] == "Crusoe"
    assert _of(fr, "provider.pin_mismatch") == []


@pytest.mark.parametrize("phase", ["work", "gate"])
def test_served_provider_outside_pin_records_mismatch(monkeypatch, fr, phase):
    _route(monkeypatch, PIN_PREFS)
    _dispatch(monkeypatch, served="OpenInference", phase=phase)
    assert _of(fr, "provider.dispatch")[0]["provider_routing"] == PIN
    assert _of(fr, "provider.pin_mismatch") == [
        {"phase": phase, "provider_name": "OpenInference", "provider_routing": PIN}]


def test_unpinned_dispatch_records_no_routing_and_no_mismatch(monkeypatch, fr):
    _route(monkeypatch, {})
    _dispatch(monkeypatch, served="OpenInference")
    assert "provider_routing" not in _of(fr, "provider.dispatch")[0]
    assert _of(fr, "provider.pin_mismatch") == []


def test_malformed_routing_is_visible_and_nothing_is_sent(monkeypatch, fr):
    _route(monkeypatch, {"openrouter": {"only": "crusoe/fp4"}})
    sent = []
    monkeypatch.setattr(requests, "post", lambda *a, **kw: sent.append(kw) or _JsonResp(_ok_body()))
    llm = _openrouter()
    kwargs = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 64}
    response, err = agent_module._provider_chat_or_error(_fake_agent(llm), kwargs, None, set(), phase="work")
    assert response is None and "request not sent" in err
    assert sent == []
    assert _of(fr, "provider.dispatch")[0]["provider_routing"] == {"invalid": True}
    assert _of(fr, "provider.dispatch_failed")[0]["exception_type"] == "ProviderRoutingConfigError"


@pytest.mark.parametrize("routing,served,mismatch", [
    (PIN, "Crusoe", False),
    (PIN, "OpenInference", True),
    ({"only": ["open-inference/fp4"]}, "OpenInference", False),
    ({"only": ["crusoe", "together"]}, "Together", False),
    ({"order": ["crusoe/fp4"], "allow_fallbacks": False}, "Fireworks", True),
    ({"order": ["crusoe/fp4"]}, "Fireworks", False),        # fallbacks allowed: no definite set
    ({"ignore": ["open-inference"]}, "OpenInference", False),  # not a pin
    (PIN, None, False),
    (PIN, "", False),
])
def test_pin_mismatch_semantics(monkeypatch, routing, served, mismatch):
    _route(monkeypatch, {"openrouter": routing})
    assert _openrouter().provider_pin_mismatch(served) is mismatch


def test_routing_fields_helper_is_safe_for_any_backend(monkeypatch):
    _route(monkeypatch, PIN_PREFS)
    assert agent_module._provider_routing_fields(_openrouter()) == {"provider_routing": PIN}
    assert agent_module._provider_routing_fields(LMStudioBackend()) == {}

    class _Raises:
        def provider_routing_summary(self):
            raise RuntimeError("boom")
    assert agent_module._provider_routing_fields(_Raises()) == {}
