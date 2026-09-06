"""UTILITY-RUNTIME-01 (mandatory-reasoning slice).

Real live incident: complete_utility(prefill="NOTES:") -> None, repeatedly,
against the owner's actual configured backend (OpenRouter, z-ai/glm-5.3-
flash). Live-verified root cause, in two independent parts:

  1. OpenRouter's own /models metadata for this model positively advertises
     {"mandatory": true, "default_enabled": true, "supported_efforts":
     ["max", "high", "low"], "default_effort": "max"} -- this model cannot
     turn reasoning off at all. complete_utility() always calls chat(...,
     disable_thinking=True) and never supplies a reasoning_effort of its
     own, so pre-repair, _effective_reasoning_effort() unconditionally
     returned None for disable_thinking=True, and apply_reasoning() never
     added a "reasoning" object to the payload at all -- leaving the
     PROVIDER's own default effort ("max") in force. Live raw-response
     probes (auto-name-shaped prompt, max_tokens=30) confirmed this
     reliably burns the entire output budget on reasoning tokens alone
     (finish_reason="length", completion_tokens==reasoning_tokens==30,
     content=None), while an explicit "low" or "high" effort override
     against the IDENTICAL prompt returned a real answer with
     finish_reason="stop" and 0 reasoning tokens.

  2. Independently, complete_utility()'s own empty-content fallback only
     ever read message["reasoning_content"] -- correct for Qwen3/DeepSeek-
     R1-style local servers, but this model's real field (live-verified,
     also independently confirmed by lmstudio.py's own extract_reasoning()
     telemetry work) is plain "reasoning". A response with real text in
     "reasoning" but nothing in "reasoning_content" was silently treated
     as empty by the old fallback.

This file pins the repair for both, plus the discovery self-priming that
makes the capability data available to a freshly-constructed backend
instance in the first place (get_llm_backend() hands dreaming.py's three
utility consumers a brand new instance on every call -- see core/backends/
loader.py -- so a per-instance cache alone can never be warm here), and the
new bounded failure diagnostics.
"""
import re

import pytest
import requests

from core.backends.base import BaseLLMBackend
from core.backends.openrouter import OpenRouterBackend
from core.backends.openai_backend import OpenAIBackend
from core.backends.reasoning import ReasoningCapabilities, NO_REASONING_CONTROL


# ══════════════════════════════════════════════════════════════════════
# 1. ReasoningCapabilities.cheapest_effort()
# ══════════════════════════════════════════════════════════════════════

def test_cheapest_effort_picks_low_out_of_max_high_low():
    """The exact live-discovered shape for z-ai/glm-5.3-flash: efforts
    given in "max", "high", "low" order (OpenRouter's own order, NOT
    ascending) -- cheapest_effort() must rank by known verbosity, not
    position."""
    caps = ReasoningCapabilities(efforts=("max", "high", "low"), mandatory=True, default_effort="max")
    assert caps.cheapest_effort() == "low"


def test_cheapest_effort_empty_efforts_returns_none():
    caps = ReasoningCapabilities(efforts=(), mandatory=True)
    assert caps.cheapest_effort() is None


def test_cheapest_effort_ignores_unranked_labels():
    """A provider-specific label outside the known ladder must never be
    guessed at as "cheapest" just because of where it sorts -- only ranked
    labels are eligible."""
    caps = ReasoningCapabilities(efforts=("turbo-max", "ultra"), mandatory=True)
    assert caps.cheapest_effort() is None


def test_cheapest_effort_mixed_ranked_and_unranked_picks_ranked_cheapest():
    caps = ReasoningCapabilities(efforts=("max", "turbo-max", "minimal"), mandatory=True)
    assert caps.cheapest_effort() == "minimal"


def test_cheapest_effort_never_consulted_by_validate():
    """validate() stays a pure membership check -- cheapest_effort() must
    not change what a caller-requested effort validates to."""
    caps = ReasoningCapabilities(efforts=("max", "high", "low"), mandatory=True)
    assert caps.validate("low") == "low"
    assert caps.validate("medium") is None  # not in efforts, unaffected by ranking


# ══════════════════════════════════════════════════════════════════════
# 2. _effective_reasoning_effort() -- mandatory-aware, capability-driven
# ══════════════════════════════════════════════════════════════════════

class _CapabilityStubBackend(BaseLLMBackend):
    """Minimal concrete backend whose reasoning_capabilities() is fully
    caller-controlled -- proves the mandatory-reasoning branch in
    _effective_reasoning_effort() is generic backend logic, not something
    special-cased to OpenRouterBackend specifically."""

    display_name = "StubProvider"

    def __init__(self, caps: ReasoningCapabilities):
        self._caps = caps
        self._model = "stub-model"

    def get_model(self) -> str:
        return self._model

    def list_models(self) -> list:
        return [self._model]

    def health_check(self):
        return True, "ok"

    def chat(self, messages, tools=None, temperature=0.7, max_tokens=1024,
             disable_thinking=False, reasoning_effort=None, tool_choice_mode=None):
        raise AssertionError("not exercised in these tests")

    def chat_stream(self, messages, max_tokens=1024, temperature=0.7, reasoning_effort=None):
        yield ""

    def reasoning_capabilities(self, model=None):
        return self._caps


def test_mandatory_model_requests_cheapest_effort_when_disable_thinking():
    backend = _CapabilityStubBackend(
        ReasoningCapabilities(efforts=("max", "high", "low"), mandatory=True, default_effort="max")
    )
    assert backend._effective_reasoning_effort(None, disable_thinking=True, model="stub-model") == "low"


def test_non_mandatory_model_still_returns_none_when_disable_thinking():
    """A model that CAN actually disable reasoning must be completely
    unaffected by this repair -- sending nothing is still correct for it."""
    backend = _CapabilityStubBackend(
        ReasoningCapabilities(efforts=("low", "medium", "high"), mandatory=False, default_effort="medium")
    )
    assert backend._effective_reasoning_effort(None, disable_thinking=True, model="stub-model") is None


def test_mandatory_model_with_no_ranked_effort_returns_none_not_a_guess():
    backend = _CapabilityStubBackend(ReasoningCapabilities(efforts=(), mandatory=True))
    assert backend._effective_reasoning_effort(None, disable_thinking=True, model="stub-model") is None


def test_disable_thinking_false_ignores_mandatory_and_passes_requested_through():
    backend = _CapabilityStubBackend(
        ReasoningCapabilities(efforts=("max", "high", "low"), mandatory=True)
    )
    assert backend._effective_reasoning_effort("high", disable_thinking=False, model="stub-model") == "high"


def test_no_reasoning_control_model_unaffected_mandatory_defaults_false():
    backend = _CapabilityStubBackend(NO_REASONING_CONTROL)
    assert backend._effective_reasoning_effort(None, disable_thinking=True, model="stub-model") is None


def test_effective_reasoning_effort_model_param_is_optional_backward_compatible():
    """Every pre-existing call site that never passed `model` must keep
    working -- model=None resolves through reasoning_capabilities(None),
    which is always NO_REASONING_CONTROL by contract."""
    backend = OpenAIBackend()
    assert backend._effective_reasoning_effort("high", disable_thinking=True) is None
    assert backend._effective_reasoning_effort(None, disable_thinking=True) is None


# ══════════════════════════════════════════════════════════════════════
# 3. complete_utility()'s widened reasoning-field fallback
# ══════════════════════════════════════════════════════════════════════

class _MessageStubBackend(BaseLLMBackend):
    """Returns a caller-supplied raw response dict from chat() -- isolates
    complete_utility()'s own field-reading logic from any real transport."""

    display_name = "StubProvider"

    def __init__(self, response):
        self._response = response
        self._model = "stub-model"

    def get_model(self):
        return self._model

    def list_models(self):
        return [self._model]

    def health_check(self):
        return True, "ok"

    def chat(self, messages, tools=None, temperature=0.7, max_tokens=1024,
             disable_thinking=False, reasoning_effort=None, tool_choice_mode=None):
        return self._response

    def chat_stream(self, messages, max_tokens=1024, temperature=0.7, reasoning_effort=None):
        yield ""


def _resp(message):
    return {"choices": [{"message": message, "finish_reason": "stop"}]}


def test_complete_utility_falls_back_to_plain_reasoning_field():
    """The live GLM/OpenRouter shape: content empty, real text under
    "reasoning" (NOT "reasoning_content"). Pre-repair this silently
    returned None -- the old fallback only ever checked reasoning_content."""
    backend = _MessageStubBackend(_resp({"content": "", "reasoning": "the actual useful text"}))
    assert backend.complete_utility("prompt") == "the actual useful text"


def test_complete_utility_falls_back_to_thinking_field():
    backend = _MessageStubBackend(_resp({"content": "", "thinking": "fallback via thinking field"}))
    assert backend.complete_utility("prompt") == "fallback via thinking field"


def test_complete_utility_reasoning_content_still_takes_priority_over_reasoning():
    """Priority order preserved: reasoning_content wins over reasoning if
    somehow both are present (matches lmstudio.py's own established
    priority, never re-ordered by this repair)."""
    backend = _MessageStubBackend(_resp({
        "content": "", "reasoning_content": "from reasoning_content", "reasoning": "from reasoning",
    }))
    assert backend.complete_utility("prompt") == "from reasoning_content"


def test_complete_utility_content_only_never_reads_plain_reasoning_field_either():
    """The critical regression this repair must not reopen: complete_utility_
    content_only() must still have NO code path capable of returning
    reasoning-lane text, for ANY of the three field names."""
    backend = _MessageStubBackend(_resp({"content": "", "reasoning": "must never surface here"}))
    assert backend.complete_utility_content_only("prompt") is None


def test_complete_utility_malformed_reasoning_field_type_is_skipped_not_coerced():
    """A non-string reasoning-lane value (malformed/unexpected shape) must
    be skipped, never str()/repr() coerced into a fake answer."""
    backend = _MessageStubBackend(_resp({"content": "", "reasoning": {"unexpected": "shape"}}))
    assert backend.complete_utility("prompt") is None


# ══════════════════════════════════════════════════════════════════════
# 4. _complete_utility_request() -- discovery self-priming
# ══════════════════════════════════════════════════════════════════════

class _FakeModelsResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return {"data": self._data}


class _FakeChatResp:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


def test_fresh_openrouter_instance_self_primes_before_first_utility_call(monkeypatch):
    """Exactly the real-world shape: get_llm_backend() hands dreaming.py a
    brand new, never-discovered OpenRouterBackend. A single utility call
    must come back correctly effort-adjusted without any caller having to
    remember to call refresh_reasoning_capabilities() itself."""
    get_calls = []
    post_calls = []

    def _fake_get(*a, **kw):
        get_calls.append(1)
        return _FakeModelsResp([{
            "id": "z-ai/glm-5.3-flash",
            "reasoning": {"mandatory": True, "supported_efforts": ["max", "high", "low"], "default_effort": "max"},
        }])

    def _fake_post(*a, **kw):
        post_calls.append(kw.get("json"))
        return _FakeChatResp({"choices": [{"message": {"role": "assistant", "content": "a real title"}}]})

    monkeypatch.setattr(requests, "get", _fake_get)
    monkeypatch.setattr(requests, "post", _fake_post)

    backend = OpenRouterBackend(api_key="test-key")
    backend._model = "z-ai/glm-5.3-flash"
    assert backend.reasoning_capabilities_ready(backend._model) is False

    result = backend.complete_utility("title this", prefill="TITLE:", max_tokens=30)

    assert result == "a real title"
    assert len(get_calls) == 1, "must discover exactly once, not on every call"
    assert post_calls[0]["reasoning"]["effort"] == "low"
    assert backend.reasoning_capabilities_ready(backend._model) is True


def test_second_utility_call_on_same_instance_does_not_rediscover(monkeypatch):
    get_calls = []

    def _fake_get(*a, **kw):
        get_calls.append(1)
        return _FakeModelsResp([{
            "id": "z-ai/glm-5.3-flash",
            "reasoning": {"mandatory": True, "supported_efforts": ["low"], "default_effort": "low"},
        }])

    def _fake_post(*a, **kw):
        return _FakeChatResp({"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

    monkeypatch.setattr(requests, "get", _fake_get)
    monkeypatch.setattr(requests, "post", _fake_post)

    backend = OpenRouterBackend(api_key="test-key")
    backend._model = "z-ai/glm-5.3-flash"

    backend.complete_utility("first call")
    backend.complete_utility("second call")

    assert len(get_calls) == 1


def test_already_ready_instance_skips_discovery_entirely(monkeypatch):
    """A caller that already primed the cache itself (e.g. Settings) must
    not pay a redundant discovery call inside complete_utility()."""
    def _explode_get(*a, **kw):
        raise AssertionError("must not rediscover when already ready")

    def _fake_post(*a, **kw):
        return _FakeChatResp({"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

    backend = OpenRouterBackend(api_key="test-key")
    backend._model = "z-ai/glm-5.3-flash"
    backend._reasoning_cache = {"z-ai/glm-5.3-flash": NO_REASONING_CONTROL}
    backend._reasoning_cache_ready = True

    monkeypatch.setattr(requests, "get", _explode_get)
    monkeypatch.setattr(requests, "post", _fake_post)

    result = backend.complete_utility("no discovery needed")
    assert result == "ok"


def test_discovery_priming_failure_is_swallowed_not_raised(monkeypatch):
    """refresh_reasoning_capabilities() reports failure as a bool, never an
    exception -- a network hiccup during priming must not turn a utility
    call into a crash. Falls back to NO_REASONING_CONTROL, same as if
    priming had never been attempted."""
    def _fake_get(*a, **kw):
        raise ConnectionError("discovery unreachable")

    def _fake_post(*a, **kw):
        return _FakeChatResp({"choices": [{"message": {"role": "assistant", "content": "ok anyway"}}]})

    monkeypatch.setattr(requests, "get", _fake_get)
    monkeypatch.setattr(requests, "post", _fake_post)

    backend = OpenRouterBackend(api_key="test-key")
    backend._model = "z-ai/glm-5.3-flash"

    result = backend.complete_utility("prompt")
    assert result == "ok anyway"


def test_static_capability_backend_never_triggers_discovery(monkeypatch):
    """OpenAI/Anthropic/Gemini/Ollama/LMStudio: reasoning_capabilities_ready()
    is always True (static tables), so _complete_utility_request() must
    never even look at requests.get for them."""
    def _explode_get(*a, **kw):
        raise AssertionError("static-capability backend must never call requests.get")

    def _fake_post(*a, **kw):
        return _FakeChatResp({
            "status": "completed",
            "output": [{"type": "message", "status": "completed",
                        "content": [{"type": "output_text", "text": "ok"}]}],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        })

    monkeypatch.setattr(requests, "get", _explode_get)
    monkeypatch.setattr(requests, "post", _fake_post)

    backend = OpenAIBackend(api_key="test-key")
    backend._model = "gpt-4o-mini"

    result = backend.complete_utility("prompt")
    assert result == "ok"


# ══════════════════════════════════════════════════════════════════════
# 5. Live-shaped end-to-end wire proof (mocked transport, real backend)
# ══════════════════════════════════════════════════════════════════════

def test_mandatory_model_wire_payload_carries_cheapest_effort_end_to_end(monkeypatch):
    """The exact live-discovered z-ai/glm-5.3-flash shape end to end:
    discovery -> _effective_reasoning_effort -> apply_reasoning ->
    OpenRouter's own unified reasoning.effort wire field."""
    def _fake_get(*a, **kw):
        return _FakeModelsResp([{
            "id": "z-ai/glm-5.3-flash",
            "reasoning": {"mandatory": True, "supported_efforts": ["max", "high", "low"], "default_effort": "max"},
        }])

    captured = {}

    def _fake_post(*a, **kw):
        captured["json"] = kw.get("json")
        return _FakeChatResp({"choices": [{"message": {"role": "assistant", "content": "Real Title Here"}}]})

    monkeypatch.setattr(requests, "get", _fake_get)
    monkeypatch.setattr(requests, "post", _fake_post)

    backend = OpenRouterBackend(api_key="test-key")
    backend._model = "z-ai/glm-5.3-flash"

    result = backend.complete_utility("name this chat", prefill="TITLE:", max_tokens=30, temperature=0.3)

    assert result == "Real Title Here"
    assert captured["json"]["reasoning"] == {"effort": "low"}
    assert captured["json"]["max_tokens"] == 30
    assert "thinking" not in captured["json"]  # OpenRouter never gets LM Studio's local fields
    assert "chat_template_kwargs" not in captured["json"]


# ══════════════════════════════════════════════════════════════════════
# 6. Bounded failure diagnostics
# ══════════════════════════════════════════════════════════════════════

class _RaisingBackend(BaseLLMBackend):
    display_name = "StubProvider"

    def __init__(self, exc):
        self._exc = exc
        self._model = "stub-model"

    def get_model(self):
        return self._model

    def list_models(self):
        return [self._model]

    def health_check(self):
        return True, "ok"

    def chat(self, messages, tools=None, temperature=0.7, max_tokens=1024,
             disable_thinking=False, reasoning_effort=None, tool_choice_mode=None):
        raise self._exc

    def chat_stream(self, messages, max_tokens=1024, temperature=0.7, reasoning_effort=None):
        yield ""


@pytest.mark.parametrize("exc,expected_reason", [
    (TimeoutError("timed out"), "timeout"),
    (ConnectionError("not reachable"), "network_error"),
    (ValueError("Unexpected response format: bad shape"), "malformed_response"),
    (RuntimeError("OpenRouter error (rate_limited, HTTP 429): slow down"), "rate_limited"),
    (RuntimeError("OpenRouter error (authentication, HTTP 401): bad key"), "authentication"),
    (RuntimeError("some other transport error"), "provider_error"),
])
def test_failure_diagnostic_reason_classification(capsys, exc, expected_reason):
    backend = _RaisingBackend(exc)
    result = backend.complete_utility("prompt")
    assert result is None
    out = capsys.readouterr().out
    assert f"reason={expected_reason}" in out
    assert "provider=StubProvider" in out
    assert "model=stub-model" in out


def test_empty_output_reason_when_nothing_returned_at_all(capsys):
    backend = _MessageStubBackend(_resp({"content": ""}))
    result = backend.complete_utility("prompt")
    assert result is None
    out = capsys.readouterr().out
    assert "reason=empty_output" in out


def test_parser_rejection_reason_when_content_present_but_stripped_to_nothing(capsys):
    """Content WAS present but was entirely an unclosed <think> block with
    no real text after it -- the provider responded, stripping is what
    rejected it. Distinct from a provider sending nothing at all."""
    backend = _MessageStubBackend(_resp({"content": "<think>never finishes"}))
    result = backend.complete_utility("prompt")
    assert result is None
    out = capsys.readouterr().out
    assert "reason=parser_rejection" in out


def test_no_diagnostic_log_on_success(capsys):
    backend = _MessageStubBackend(_resp({"content": "a clean answer"}))
    result = backend.complete_utility("prompt")
    assert result == "a clean answer"
    out = capsys.readouterr().out
    assert "[UTILITY]" not in out


def test_failure_diagnostic_never_leaks_api_key_or_prompt_text(capsys):
    backend = _RaisingBackend(RuntimeError("OpenRouter error (rate_limited, HTTP 429): slow down"))
    backend.complete_utility("a secret prompt that must never appear in logs", prefill="NOTES:")
    out = capsys.readouterr().out
    assert "a secret prompt" not in out
    assert "test-key" not in out
