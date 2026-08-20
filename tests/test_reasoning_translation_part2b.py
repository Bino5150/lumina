"""
Patch 3A.4 Part 2B -- OpenRouter dynamic capability discovery, direct Qwen
(DashScope) hybrid-thinking control, and the OmniRoute scope-correction
regression.

Three independent provider slices, one file:

  1. OpenRouterBackend -- reasoning_capabilities() is populated ONLY via an
     explicit, successful list_models() discovery call and stays a pure
     cache read (zero HTTP) otherwise. All discovery here is against a
     monkeypatched requests.get -- no real network access.

  2. QwenBackend -- direct DashScope OpenAI-compatible hybrid-thinking
     control (enable_thinking / thinking_budget), NOT a fake
     low/medium/high effort ladder. See core/backends/qwen.py's
     _REASONING_MODELS module comment for the full doc citation
     (https://www.alibabacloud.com/help/en/model-studio/deep-thinking,
     verified 2026-08-20) backing every model mapped below.

  3. OmniRouteBackend -- explicitly DESCOPED for this patch per the Part
     2B task correction. One regression test proves it stays
     NO_REASONING_CONTROL with zero name-based inference, and that
     apply_reasoning() leaves an arbitrary payload untouched.

These tests inspect exact resulting dicts/tuples, never mocked call
assertions alone -- every payload-mutation test reads the real dict back.
"""
from typing import Optional

import pytest
import requests

from core.backends.reasoning import ReasoningCapabilities, NO_REASONING_CONTROL
from core.backends.openrouter import OpenRouterBackend
from core.backends.qwen import QwenBackend
from core.backends.omniroute import OmniRouteBackend


# ════════════════════════════════════════════════════════════════════════
# 1. OpenRouter -- dynamic capability discovery
# ════════════════════════════════════════════════════════════════════════

class _FakeModelsResp:
    """Minimal stand-in for a requests.Response from GET /models."""
    def __init__(self, data):
        self._data = data

    def json(self):
        return {"data": self._data}


def _mock_models(monkeypatch, data):
    """Patch requests.get to return a fixed /models payload regardless of
    URL/headers -- matches the mocking convention already used elsewhere
    in this suite (e.g. tests/test_elevenlabs_bridge.py)."""
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _FakeModelsResp(data))


def _mock_models_raises(monkeypatch, exc):
    def _raise(*a, **kw):
        raise exc
    monkeypatch.setattr(requests, "get", _raise)


# ── 1a. No discovery yet -> no HTTP, NO_REASONING_CONTROL ──────────────

def test_openrouter_capability_lookup_before_discovery_makes_no_network_call(monkeypatch):
    called = {"get": False}
    monkeypatch.setattr(requests, "get", lambda *a, **kw: called.__setitem__("get", True))

    backend = OpenRouterBackend()
    caps = backend.reasoning_capabilities("anthropic/claude-sonnet-5")

    assert caps is NO_REASONING_CONTROL
    assert called["get"] is False


def test_openrouter_constructor_performs_zero_network_work(monkeypatch):
    called = {"get": False}
    monkeypatch.setattr(requests, "get", lambda *a, **kw: called.__setitem__("get", True))
    OpenRouterBackend()
    assert called["get"] is False


# ── 1b. list_models() contract unchanged ────────────────────────────────

def test_openrouter_list_models_returns_same_id_list_contract(monkeypatch):
    data = [{"id": "openai/gpt-5.6"}, {"id": "anthropic/claude-sonnet-5"}]
    _mock_models(monkeypatch, data)
    backend = OpenRouterBackend()

    result = backend.list_models()

    assert result == ["openai/gpt-5.6", "anthropic/claude-sonnet-5"]


def test_openrouter_list_models_fallback_on_error_unchanged(monkeypatch):
    _mock_models_raises(monkeypatch, ConnectionError("boom"))
    backend = OpenRouterBackend()

    result = backend.list_models()

    assert result == [backend._model]


# ── 1c. Successful discovery parses metadata correctly ──────────────────

def test_openrouter_discovery_parses_supported_efforts_preserving_order(monkeypatch):
    data = [{
        "id": "openai/gpt-5.6",
        "reasoning": {"supported_efforts": ["high", "low", "medium"]},
    }]
    _mock_models(monkeypatch, data)
    backend = OpenRouterBackend()
    backend.list_models()

    caps = backend.reasoning_capabilities("openai/gpt-5.6")
    assert caps.efforts == ("high", "low", "medium")


def test_openrouter_discovery_parses_default_effort(monkeypatch):
    data = [{
        "id": "openai/gpt-5.6",
        "reasoning": {"supported_efforts": ["low", "high"], "default_effort": "high"},
    }]
    _mock_models(monkeypatch, data)
    backend = OpenRouterBackend()
    backend.list_models()

    caps = backend.reasoning_capabilities("openai/gpt-5.6")
    assert caps.default_effort == "high"


@pytest.mark.parametrize("mandatory_value", [True, False])
def test_openrouter_discovery_parses_explicit_mandatory_both_ways(monkeypatch, mandatory_value):
    data = [{
        "id": "some/model",
        "reasoning": {"mandatory": mandatory_value},
    }]
    _mock_models(monkeypatch, data)
    backend = OpenRouterBackend()
    backend.list_models()

    caps = backend.reasoning_capabilities("some/model")
    assert caps.mandatory is mandatory_value


def test_openrouter_discovery_absent_mandatory_is_not_inferred_true(monkeypatch):
    data = [{
        "id": "some/model",
        "reasoning": {"supported_efforts": ["low", "high"]},
    }]
    _mock_models(monkeypatch, data)
    backend = OpenRouterBackend()
    backend.list_models()

    caps = backend.reasoning_capabilities("some/model")
    assert caps.mandatory is False


def test_openrouter_model_with_no_reasoning_key_advertises_no_control(monkeypatch):
    data = [{"id": "meta-llama/llama-3.1-8b-instruct:free"}]
    _mock_models(monkeypatch, data)
    backend = OpenRouterBackend()
    backend.list_models()

    caps = backend.reasoning_capabilities("meta-llama/llama-3.1-8b-instruct:free")
    assert caps is NO_REASONING_CONTROL


def test_openrouter_malformed_supported_efforts_string_not_list_never_invents_efforts(monkeypatch):
    data = [{
        "id": "some/model",
        "reasoning": {"supported_efforts": "high", "mandatory": True},
    }]
    _mock_models(monkeypatch, data)
    backend = OpenRouterBackend()
    backend.list_models()

    caps = backend.reasoning_capabilities("some/model")
    # mandatory was validly present -> capability entry exists with it
    # preserved, but efforts must never become a non-empty tuple invented
    # from the malformed string.
    assert caps is not NO_REASONING_CONTROL
    assert caps.efforts == ()
    assert caps.mandatory is True


def test_openrouter_malformed_supported_efforts_empty_list_never_invents_efforts(monkeypatch):
    data = [{
        "id": "some/model",
        "reasoning": {"supported_efforts": []},
    }]
    _mock_models(monkeypatch, data)
    backend = OpenRouterBackend()
    backend.list_models()

    caps = backend.reasoning_capabilities("some/model")
    assert caps.efforts == ()


def test_openrouter_reasoning_object_with_nothing_valid_still_no_reasoning_control_in_effect(monkeypatch):
    """default_enabled alone (no matching ReasoningCapabilities field)
    yields an all-default capability object -- behaviorally indistinguishable
    from NO_REASONING_CONTROL for validate() purposes, even though it isn't
    the same object identity."""
    data = [{
        "id": "some/model",
        "reasoning": {"default_enabled": True},
    }]
    _mock_models(monkeypatch, data)
    backend = OpenRouterBackend()
    backend.list_models()

    caps = backend.reasoning_capabilities("some/model")
    assert caps.efforts == ()
    assert caps.mandatory is False
    assert caps.default_effort is None
    assert caps.validate("high") is None


def test_openrouter_one_malformed_entry_does_not_break_valid_siblings(monkeypatch):
    data = [
        {"id": "broken/model", "reasoning": {"supported_efforts": "not-a-list"}},
        {"id": "good/model", "reasoning": {"supported_efforts": ["low", "high"], "default_effort": "high"}},
    ]
    _mock_models(monkeypatch, data)
    backend = OpenRouterBackend()
    backend.list_models()

    good_caps = backend.reasoning_capabilities("good/model")
    assert good_caps.efforts == ("low", "high")
    assert good_caps.default_effort == "high"


def test_openrouter_two_models_same_response_no_cross_contamination(monkeypatch):
    data = [
        {"id": "model/a", "reasoning": {"supported_efforts": ["low", "medium"], "mandatory": True}},
        {"id": "model/b", "reasoning": {"supported_efforts": ["high"], "mandatory": False}},
    ]
    _mock_models(monkeypatch, data)
    backend = OpenRouterBackend()
    backend.list_models()

    caps_a = backend.reasoning_capabilities("model/a")
    caps_b = backend.reasoning_capabilities("model/b")
    assert caps_a.efforts == ("low", "medium")
    assert caps_a.mandatory is True
    assert caps_b.efforts == ("high",)
    assert caps_b.mandatory is False


# ── 1d. Cache-safety invariants ─────────────────────────────────────────

def test_openrouter_second_refresh_with_smaller_set_removes_stale_entries(monkeypatch):
    data_ab = [
        {"id": "model/a", "reasoning": {"supported_efforts": ["low"]}},
        {"id": "model/b", "reasoning": {"supported_efforts": ["high"]}},
    ]
    _mock_models(monkeypatch, data_ab)
    backend = OpenRouterBackend()
    backend.list_models()
    assert backend.reasoning_capabilities("model/a") is not NO_REASONING_CONTROL
    assert backend.reasoning_capabilities("model/b") is not NO_REASONING_CONTROL

    data_b_only = [{"id": "model/b", "reasoning": {"supported_efforts": ["high"]}}]
    _mock_models(monkeypatch, data_b_only)
    backend.list_models()

    assert backend.reasoning_capabilities("model/a") is NO_REASONING_CONTROL
    assert backend.reasoning_capabilities("model/b") is not NO_REASONING_CONTROL


def test_openrouter_failed_refresh_leaves_old_cache_intact(monkeypatch):
    data = [{"id": "model/a", "reasoning": {"supported_efforts": ["low", "high"], "default_effort": "high"}}]
    _mock_models(monkeypatch, data)
    backend = OpenRouterBackend()
    backend.list_models()
    before = backend.reasoning_capabilities("model/a")
    assert before.efforts == ("low", "high")

    _mock_models_raises(monkeypatch, ConnectionError("network down"))
    result = backend.list_models()

    assert result == [backend._model]
    after = backend.reasoning_capabilities("model/a")
    assert after.efforts == ("low", "high")
    assert after.default_effort == "high"


def test_openrouter_failed_refresh_on_first_ever_call_leaves_cache_empty_not_crashed(monkeypatch):
    _mock_models_raises(monkeypatch, ConnectionError("network down"))
    backend = OpenRouterBackend()
    backend.list_models()

    assert backend.reasoning_capabilities("anything") is NO_REASONING_CONTROL


# ── 1e. Wire translation ────────────────────────────────────────────────

def test_openrouter_valid_effort_emits_exact_reasoning_effort_key(monkeypatch):
    data = [{"id": "openai/gpt-5.6", "reasoning": {"supported_efforts": ["low", "high"]}}]
    _mock_models(monkeypatch, data)
    backend = OpenRouterBackend()
    backend.list_models()

    payload = {"model": "openai/gpt-5.6", "messages": []}
    backend.apply_reasoning(payload, "high", model="openai/gpt-5.6")
    assert payload["reasoning"]["effort"] == "high"


def test_openrouter_preserves_existing_sibling_keys_in_reasoning_object(monkeypatch):
    data = [{"id": "openai/gpt-5.6", "reasoning": {"supported_efforts": ["low", "high"]}}]
    _mock_models(monkeypatch, data)
    backend = OpenRouterBackend()
    backend.list_models()

    payload = {"model": "openai/gpt-5.6", "reasoning": {"exclude": True}}
    backend.apply_reasoning(payload, "high", model="openai/gpt-5.6")
    assert payload["reasoning"] == {"exclude": True, "effort": "high"}


def test_openrouter_provider_default_emits_no_reasoning_key(monkeypatch):
    data = [{"id": "openai/gpt-5.6", "reasoning": {"supported_efforts": ["low", "high"]}}]
    _mock_models(monkeypatch, data)
    backend = OpenRouterBackend()
    backend.list_models()

    payload = {"model": "openai/gpt-5.6", "messages": []}
    before = dict(payload)
    backend.apply_reasoning(payload, None, model="openai/gpt-5.6")
    assert payload == before
    assert "reasoning" not in payload


def test_openrouter_unsupported_effort_emits_nothing(monkeypatch):
    data = [{"id": "openai/gpt-5.6", "reasoning": {"supported_efforts": ["low", "high"]}}]
    _mock_models(monkeypatch, data)
    backend = OpenRouterBackend()
    backend.list_models()

    payload = {"model": "openai/gpt-5.6"}
    backend.apply_reasoning(payload, "ultra", model="openai/gpt-5.6")
    assert payload == {"model": "openai/gpt-5.6"}
    assert "reasoning" not in payload


def test_openrouter_reasoning_capabilities_itself_performs_zero_http_after_discovery(monkeypatch):
    """Once cached, reasoning_capabilities() must never re-hit the
    network -- prove by breaking requests.get after a successful
    discovery and confirming subsequent lookups still work."""
    data = [{"id": "openai/gpt-5.6", "reasoning": {"supported_efforts": ["low", "high"]}}]
    _mock_models(monkeypatch, data)
    backend = OpenRouterBackend()
    backend.list_models()

    def _explode(*a, **kw):
        raise AssertionError("reasoning_capabilities() must not call requests.get")
    monkeypatch.setattr(requests, "get", _explode)

    caps = backend.reasoning_capabilities("openai/gpt-5.6")
    assert caps.efforts == ("low", "high")


# ════════════════════════════════════════════════════════════════════════
# 2. Qwen (DashScope) -- hybrid-thinking control, not a fake effort ladder
# ════════════════════════════════════════════════════════════════════════

def test_qwen_default_model_advertises_real_hybrid_controls():
    """qwen3.5-plus (repo's actual QWEN_DEFAULT_MODEL) -- real, verified
    disabled/enabled toggle, not an invented effort ladder."""
    backend = QwenBackend()
    caps = backend.reasoning_capabilities("qwen3.5-plus")
    assert caps.efforts == ("disabled", "enabled")
    assert caps.default_effort == "enabled"
    assert caps.mandatory is False


@pytest.mark.parametrize("model", [
    "qwen3.5-plus", "qwen3.5-flash",
    "qwen3.6-plus", "qwen3.6-flash",
    "qwen3.7-plus", "qwen3.7-max",
])
def test_qwen_all_verified_hybrid_models_share_disabled_enabled_order(model):
    backend = QwenBackend()
    caps = backend.reasoning_capabilities(model)
    assert caps.efforts == ("disabled", "enabled")
    assert caps.default_effort == "enabled"
    assert caps.supports_budget is True


def test_qwen_provider_default_emits_neither_enable_thinking_nor_budget():
    backend = QwenBackend()
    payload = {"model": "qwen3.5-plus", "messages": [], "temperature": 0.7}
    before = dict(payload)
    backend.apply_reasoning(payload, None, model="qwen3.5-plus")
    assert payload == before
    assert "enable_thinking" not in payload
    assert "thinking_budget" not in payload


def test_qwen_explicit_disabled_emits_enable_thinking_false_exactly():
    backend = QwenBackend()
    payload = {"model": "qwen3.5-plus", "messages": []}
    backend.apply_reasoning(payload, "disabled", model="qwen3.5-plus")
    assert payload == {"model": "qwen3.5-plus", "messages": [], "enable_thinking": False}


def test_qwen_explicit_enabled_emits_enable_thinking_true_exactly():
    backend = QwenBackend()
    payload = {"model": "qwen3.5-plus", "messages": []}
    backend.apply_reasoning(payload, "enabled", model="qwen3.5-plus")
    assert payload == {"model": "qwen3.5-plus", "messages": [], "enable_thinking": True}


def test_qwen_fake_high_effort_does_not_reach_the_wire():
    """The explicit anti-behavior this slice was scoped to prevent: Qwen
    never advertised low/medium/high/xhigh, so requesting "high" must
    validate to None and leave the payload untouched."""
    backend = QwenBackend()
    caps = backend.reasoning_capabilities("qwen3.5-plus")
    assert caps.validate("high") is None

    payload = {"model": "qwen3.5-plus", "messages": []}
    backend.apply_reasoning(payload, "high", model="qwen3.5-plus")
    assert payload == {"model": "qwen3.5-plus", "messages": []}
    assert "enable_thinking" not in payload


def test_qwen_does_not_inherit_openai_groq_low_medium_high_semantics():
    """QwenBackend and OpenAIBackend/GroqBackend all subclass
    LMStudioBackend -- confirm Qwen's capability table is genuinely its
    own, not accidentally shared/leaked state."""
    backend = QwenBackend()
    assert backend.reasoning_capabilities("qwen3.5-plus").validate("medium") is None
    assert backend.reasoning_capabilities("qwen3.5-plus").validate("low") is None


def test_qwen_thinking_only_model_is_mandatory_with_empty_efforts():
    backend = QwenBackend()
    caps = backend.reasoning_capabilities("qwen3-235b-a22b-thinking-2507")
    assert caps.mandatory is True
    assert caps.efforts == ()
    assert "disabled" not in caps.efforts


def test_qwen_thinking_only_model_disabled_never_reaches_the_wire():
    """"disabled" is not a member of the thinking-only model's efforts,
    so requesting it must fail safe -- never emit enable_thinking=False
    for a model that cannot actually turn thinking off."""
    backend = QwenBackend()
    payload = {"model": "qwen3-235b-a22b-thinking-2507", "messages": []}
    backend.apply_reasoning(payload, "disabled", model="qwen3-235b-a22b-thinking-2507")
    assert payload == {"model": "qwen3-235b-a22b-thinking-2507", "messages": []}
    assert "enable_thinking" not in payload


@pytest.mark.parametrize("model", ["qwen3.5-max", "qwen-max", "qwen-plus", "qwen-turbo"])
def test_qwen_unverified_or_out_of_scope_models_advertise_no_control(model):
    """qwen3.5-max is unconfirmed by DashScope docs entirely; qwen-max/
    qwen-plus/qwen-turbo are the bare "Qwen3 (commercial)" alias family,
    a distinct doc category from the versioned 3.5/3.6/3.7 lines this
    slice was scoped to -- all four are present in QwenBackend's own
    KNOWN_MODELS but deliberately left unmapped."""
    backend = QwenBackend()
    assert backend.reasoning_capabilities(model) is NO_REASONING_CONTROL


def test_qwen_unknown_model_string_advertises_no_control():
    backend = QwenBackend()
    assert backend.reasoning_capabilities("qwen-totally-made-up-model") is NO_REASONING_CONTROL
    assert backend.reasoning_capabilities() is NO_REASONING_CONTROL
    assert backend.reasoning_capabilities(None) is NO_REASONING_CONTROL


# ════════════════════════════════════════════════════════════════════════
# 3. OmniRoute -- explicitly descoped, stays NO_REASONING_CONTROL
# ════════════════════════════════════════════════════════════════════════

def test_omniroute_stays_no_reasoning_control_no_name_based_inference():
    """Part 2B scope correction: OmniRoute gets zero dynamic reasoning
    discovery/translation in this patch. Proven two ways: (1) a real,
    plausible model name gets no control, and (2) a made-up model string
    ALSO gets no control -- ruling out any accidental substring/family
    name-based inference creeping in (e.g. "if 'qwen' in model" or
    similar) rather than genuine absence of any capability table at all."""
    backend = OmniRouteBackend()
    assert backend.reasoning_capabilities() is NO_REASONING_CONTROL
    assert backend.reasoning_capabilities(model="gpt-5.6") is NO_REASONING_CONTROL
    assert backend.reasoning_capabilities(model="qwen3.5-plus") is NO_REASONING_CONTROL
    # A completely made-up, never-real model string -- proves there is no
    # name-pattern-based capability inference happening anywhere.
    assert backend.reasoning_capabilities(model="totally-fictional-omniroute-model-xyz-123") is NO_REASONING_CONTROL

    payload = {"model": "totally-fictional-omniroute-model-xyz-123", "messages": [{"role": "user", "content": "hi"}], "temperature": 0.5}
    before = dict(payload)
    backend.apply_reasoning(payload, "high", model="totally-fictional-omniroute-model-xyz-123")
    assert payload == before
