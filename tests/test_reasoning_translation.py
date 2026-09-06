"""
Patch 3A.4 Part 2A -- provider-aware reasoning payload translation.

Covers:
  - BaseLLMBackend.apply_reasoning()/_apply_reasoning_override() (the
    shared pure translator introduced in core/backends/base.py).
  - OpenAIBackend, AnthropicBackend, GeminiBackend, GroqBackend
    reasoning_capabilities() + _apply_reasoning_override() overrides.
  - Inheritance safety: LMStudioBackend and its OTHER concrete subclasses
    must not pick up any capability data from OpenAIBackend/GroqBackend.

Originally written for Part 2A, when QwenBackend/OpenRouterBackend were
still untouched (NO_REASONING_CONTROL across the board) and reserved for a
later Part 2B. Part 2B has since populated real capability data for both
-- see core/backends/qwen.py / core/backends/openrouter.py and the
dedicated Part 2B test file for their full matrices and wire-translation
tests. The `test_qwen_backend_inheritance_safety_no_sibling_leakage` /
`test_openrouter_backend_untouched_in_this_slice_remains_no_reasoning_control`
tests below were updated (Qwen) or reconfirmed as still accurate
(OpenRouter, whose cache stays empty here since no discovery call is ever
made in this file) rather than left silently stale.

These tests inspect exact resulting payload dicts, never mocked calls --
no HTTP is performed anywhere in this file. Every backend constructor used
here is confirmed to do config-only setup (getattr with safe defaults),
so no config keys need to be set for these tests to run.
"""
from typing import Optional

import pytest

from core.backends.base import BaseLLMBackend
from core.backends.reasoning import ReasoningCapabilities, NO_REASONING_CONTROL
from core.backends.lmstudio import LMStudioBackend
from core.backends.openai_backend import OpenAIBackend
from core.backends.groq import GroqBackend
from core.backends.qwen import QwenBackend
from core.backends.openrouter import OpenRouterBackend
from core.backends.anthropic_backend import AnthropicBackend
from core.backends.gemini_backend import GeminiBackend


# ── 1. Base translator mechanics (no real provider data) ───────────────

class _CapableStubBackend(BaseLLMBackend):
    """Minimal concrete backend exposing fixed, non-empty capabilities so
    the shared apply_reasoning()/_apply_reasoning_override() mechanics can
    be tested in isolation from any real provider's wire format. Records
    every _apply_reasoning_override() call it receives instead of touching
    a real payload shape, so tests can assert exactly when/whether the
    provider-hook fires."""

    def __init__(self):
        self.override_calls = []

    def get_model(self):
        raise AssertionError("get_model() must not be called")

    def list_models(self):
        raise AssertionError("list_models() must not be called")

    def health_check(self):
        raise AssertionError("health_check() must not be called")

    def chat(self, messages, tools=None, temperature=0.7, max_tokens=1024, disable_thinking=False):
        raise AssertionError("chat() must not be called")

    def chat_stream(self, messages, max_tokens=1024, temperature=0.7):
        raise AssertionError("chat_stream() must not be called")

    def reasoning_capabilities(self, model: Optional[str] = None) -> ReasoningCapabilities:
        return ReasoningCapabilities(efforts=("low", "medium", "high"), default_effort="medium")

    def _apply_reasoning_override(self, payload, effort, model=None):
        self.override_calls.append((effort, model))
        payload["stub_effort"] = effort


def test_base_translator_none_requested_leaves_payload_unchanged():
    backend = _CapableStubBackend()
    payload = {"model": "x", "messages": [], "temperature": 0.7}
    before = dict(payload)
    backend.apply_reasoning(payload, None)
    assert payload == before
    assert backend.override_calls == []


def test_base_translator_unsupported_effort_leaves_payload_unchanged():
    backend = _CapableStubBackend()
    payload = {"model": "x", "messages": []}
    before = dict(payload)
    backend.apply_reasoning(payload, "ultra-mega-think")
    assert payload == before
    assert backend.override_calls == []


def test_base_translator_unknown_model_leaves_payload_unchanged():
    """Base BaseLLMBackend default (no override) reports NO_REASONING_CONTROL
    for any model, so any requested effort must leave payload untouched."""
    backend = LMStudioBackend()
    payload = {"model": "some-model", "messages": []}
    before = dict(payload)
    backend.apply_reasoning(payload, "high", model="some-totally-unknown-model")
    assert payload == before


def test_base_translator_valid_effort_calls_override_with_validated_value():
    backend = _CapableStubBackend()
    payload = {"model": "x"}
    backend.apply_reasoning(payload, "medium", model="whatever")
    assert payload["stub_effort"] == "medium"
    assert backend.override_calls == [("medium", "whatever")]


def test_default_apply_reasoning_override_is_noop_and_does_not_raise():
    """A backend that doesn't override _apply_reasoning_override at all
    (the base default) must simply do nothing if ever reached -- though in
    practice apply_reasoning()'s NO_REASONING_CONTROL fail-safe means real
    backends without capability data never get this far."""
    backend = LMStudioBackend()
    payload = {"a": 1}
    backend._apply_reasoning_override(payload, "high", model="x")
    assert payload == {"a": 1}


# ── 2. OpenAI ────────────────────────────────────────────────────────────

def test_openai_gpt56_capability_order_is_exact():
    backend = OpenAIBackend()
    caps = backend.reasoning_capabilities("gpt-5.6")
    assert caps.efforts == ("none", "low", "medium", "high", "xhigh", "max")
    assert caps.default_effort == "medium"


@pytest.mark.parametrize("model", ["gpt-5.6", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"])
def test_openai_all_gpt56_family_models_share_same_capability_set(model):
    backend = OpenAIBackend()
    caps = backend.reasoning_capabilities(model)
    assert caps.efforts == ("none", "low", "medium", "high", "xhigh", "max")
    assert caps.default_effort == "medium"


def test_openai_provider_default_emits_no_reasoning_effort_key():
    backend = OpenAIBackend()
    payload = {"model": "gpt-5.6", "messages": [], "temperature": 0.7}
    before = dict(payload)
    backend.apply_reasoning(payload, None, model="gpt-5.6")
    assert payload == before
    assert "reasoning_effort" not in payload


def test_openai_valid_effort_emits_exactly_reasoning_object():
    """OPENAI-RESPONSES-01: Responses' reasoning wire shape is a nested
    {"effort", "summary"} object, not Chat Completions' flat
    "reasoning_effort" string field."""
    backend = OpenAIBackend()
    payload = {"model": "gpt-5.6", "input": [{"role": "user", "content": "hi"}]}
    backend.apply_reasoning(payload, "high", model="gpt-5.6")
    assert payload == {
        "model": "gpt-5.6",
        "input": [{"role": "user", "content": "hi"}],
        "reasoning": {"effort": "high", "summary": "auto"},
    }


def test_openai_unsupported_value_emits_nothing():
    backend = OpenAIBackend()
    payload = {"model": "gpt-5.6"}
    backend.apply_reasoning(payload, "ultra", model="gpt-5.6")
    assert payload == {"model": "gpt-5.6"}
    assert "reasoning" not in payload


def test_openai_unknown_model_advertises_no_control():
    backend = OpenAIBackend()
    assert backend.reasoning_capabilities("gpt-4o") is NO_REASONING_CONTROL
    assert backend.reasoning_capabilities("gpt-5.6-nonexistent") is NO_REASONING_CONTROL
    payload = {"model": "gpt-4o"}
    backend.apply_reasoning(payload, "high", model="gpt-4o")
    assert payload == {"model": "gpt-4o"}


def test_openai_no_model_specified_advertises_no_control():
    backend = OpenAIBackend()
    assert backend.reasoning_capabilities() is NO_REASONING_CONTROL
    assert backend.reasoning_capabilities(None) is NO_REASONING_CONTROL


# ── 3. Anthropic ─────────────────────────────────────────────────────────

def test_anthropic_sonnet_5_capability_set_and_order_is_exact():
    """claude-sonnet-5 advertises all five effort levels, in order --
    confirmed against Anthropic's own docs during the Part 2A correction
    pass (this model was unconfirmed, and therefore unmapped, in the
    original Part 2A slice)."""
    backend = AnthropicBackend()
    caps = backend.reasoning_capabilities("claude-sonnet-5")
    assert caps.efforts == ("low", "medium", "high", "xhigh", "max")
    assert caps.default_effort == "high"


def test_anthropic_sonnet_4_6_capability_set_and_order_is_exact():
    """claude-sonnet-4-6 has a DIFFERENT effort matrix from claude-sonnet-5
    -- no 'xhigh'. This is the corrected behavior; before the Part 2A
    correction pass, claude-sonnet-4-6 incorrectly shared claude-sonnet-5's
    full five-level matrix."""
    backend = AnthropicBackend()
    caps = backend.reasoning_capabilities("claude-sonnet-4-6")
    assert caps.efforts == ("low", "medium", "high", "max")
    assert caps.default_effort == "high"


def test_anthropic_sonnet_5_xhigh_reaches_the_wire():
    backend = AnthropicBackend()
    payload = {"model": "claude-sonnet-5", "messages": []}
    backend.apply_reasoning(payload, "xhigh", model="claude-sonnet-5")
    assert payload["output_config"] == {"effort": "xhigh"}


def test_anthropic_sonnet_4_6_xhigh_validates_to_no_override():
    """claude-sonnet-4-6 does not support 'xhigh' -- requesting it must
    fail safe to Provider Default (no output_config mutation at all),
    exactly like any other unsupported effort string."""
    backend = AnthropicBackend()
    payload = {"model": "claude-sonnet-4-6", "messages": []}
    before = dict(payload)
    backend.apply_reasoning(payload, "xhigh", model="claude-sonnet-4-6")
    assert payload == before
    assert "output_config" not in payload


def test_anthropic_sonnet_4_6_xhigh_does_not_touch_existing_output_config():
    """Same as above, but with a pre-existing output_config carrying
    unrelated keys -- an unsupported effort must leave it byte-for-byte
    untouched, not merge in a spurious 'effort' key."""
    backend = AnthropicBackend()
    payload = {
        "model": "claude-sonnet-4-6",
        "messages": [],
        "output_config": {"some_other_key": "keep-me"},
    }
    backend.apply_reasoning(payload, "xhigh", model="claude-sonnet-4-6")
    assert payload["output_config"] == {"some_other_key": "keep-me"}


def test_anthropic_sonnet_4_6_max_still_reaches_the_wire():
    """'max' remains valid for claude-sonnet-4-6 -- only 'xhigh' was
    removed from its matrix."""
    backend = AnthropicBackend()
    payload = {"model": "claude-sonnet-4-6", "messages": []}
    backend.apply_reasoning(payload, "max", model="claude-sonnet-4-6")
    assert payload["output_config"] == {"effort": "max"}


def test_anthropic_valid_effort_preserves_existing_output_config_members():
    """General preservation-mechanics test -- uses claude-sonnet-5 since
    'xhigh' is only valid there (claude-sonnet-4-6 dropped 'xhigh' in the
    Part 2A correction pass; see test_anthropic_sonnet_4_6_xhigh_does_not_touch_existing_output_config
    for that model's now-different behavior with the same effort string)."""
    backend = AnthropicBackend()
    payload = {
        "model": "claude-sonnet-5",
        "messages": [],
        "output_config": {"some_other_key": "keep-me"},
    }
    backend.apply_reasoning(payload, "xhigh", model="claude-sonnet-5")
    assert payload["output_config"] == {"some_other_key": "keep-me", "effort": "xhigh"}


def test_anthropic_provider_default_emits_no_output_config_key_at_all():
    backend = AnthropicBackend()
    payload = {"model": "claude-sonnet-4-6", "messages": []}
    before = dict(payload)
    backend.apply_reasoning(payload, None, model="claude-sonnet-4-6")
    assert payload == before
    assert "output_config" not in payload


def test_anthropic_sonnet_5_provider_default_emits_no_output_config_key_at_all():
    """Same Provider Default guarantee as the claude-sonnet-4-6 test above,
    proven independently for claude-sonnet-5 -- requested=None must remain
    a pure no-op for both mapped models, not just one."""
    backend = AnthropicBackend()
    payload = {"model": "claude-sonnet-5", "messages": []}
    before = dict(payload)
    backend.apply_reasoning(payload, None, model="claude-sonnet-5")
    assert payload == before
    assert "output_config" not in payload


def test_anthropic_valid_effort_on_fresh_payload_creates_output_config():
    backend = AnthropicBackend()
    payload = {"model": "claude-sonnet-4-6", "messages": [], "temperature": 0.7}
    backend.apply_reasoning(payload, "medium", model="claude-sonnet-4-6")
    assert payload == {
        "model": "claude-sonnet-4-6",
        "messages": [],
        "temperature": 0.7,
        "output_config": {"effort": "medium"},
    }


def test_anthropic_unsupported_effort_emits_nothing():
    backend = AnthropicBackend()
    payload = {"model": "claude-sonnet-4-6"}
    backend.apply_reasoning(payload, "adaptive", model="claude-sonnet-4-6")
    assert payload == {"model": "claude-sonnet-4-6"}
    assert "output_config" not in payload


def test_anthropic_adaptive_is_never_an_advertised_effort():
    """Explicit spec requirement: 'adaptive' must never be treated as a
    valid effort value for Sonnet."""
    backend = AnthropicBackend()
    caps = backend.reasoning_capabilities("claude-sonnet-4-6")
    assert "adaptive" not in caps.efforts
    assert caps.validate("adaptive") is None


def test_anthropic_unknown_model_string_advertises_no_control():
    """An unrelated/unknown Anthropic model string must still collapse
    safely to NO_REASONING_CONTROL rather than picking up either Sonnet
    entry's capability data."""
    backend = AnthropicBackend()
    assert backend.reasoning_capabilities("claude-nonexistent-model") is NO_REASONING_CONTROL


def test_anthropic_no_other_model_mappings_beyond_confirmed_sonnets():
    """Do not add other Claude model mappings without positively
    confirming both id and effort matrix. Opus/Haiku (both present in
    list_models()) must not have picked up capability data as a side
    effect of adding either Sonnet entry."""
    backend = AnthropicBackend()
    assert backend.reasoning_capabilities("claude-opus-4-7") is NO_REASONING_CONTROL
    assert backend.reasoning_capabilities("claude-haiku-4-5-20251001") is NO_REASONING_CONTROL


# ── 4. Gemini ────────────────────────────────────────────────────────────

def test_gemini_capability_matrix_spot_check_gemini_37_flash():
    backend = GeminiBackend()
    caps = backend.reasoning_capabilities("gemini-3.7-flash")
    assert caps.efforts == ("low", "medium", "high")
    assert caps.default_effort == "medium"


def test_gemini_capability_matrix_spot_check_gemini_36_flash():
    backend = GeminiBackend()
    caps = backend.reasoning_capabilities("gemini-3.6-flash")
    assert caps.efforts == ("minimal", "low", "medium", "high")
    assert caps.default_effort == "medium"


def test_gemini_capability_matrix_spot_check_gemini_31_flash_lite_image():
    backend = GeminiBackend()
    caps = backend.reasoning_capabilities("gemini-3.1-flash-lite-image")
    assert caps.efforts == ("minimal", "high")
    assert caps.default_effort == "minimal"


def test_gemini_capability_matrix_spot_check_gemini_3_pro_preview():
    backend = GeminiBackend()
    caps = backend.reasoning_capabilities("gemini-3-pro-preview")
    assert caps.efforts == ("low", "high")
    assert caps.default_effort == "high"


def test_gemini_capability_matrix_spot_check_gemini_25_pro_no_default():
    backend = GeminiBackend()
    caps = backend.reasoning_capabilities("gemini-2.5-pro")
    assert caps.efforts == ("low", "medium", "high")
    assert caps.default_effort is None


def test_gemini_does_not_offer_minimal_where_matrix_excludes_it():
    backend = GeminiBackend()
    assert "minimal" not in backend.reasoning_capabilities("gemini-3.7-flash").efforts
    assert "minimal" not in backend.reasoning_capabilities("gemini-2.5-pro").efforts
    assert "minimal" not in backend.reasoning_capabilities("gemini-2.5-flash").efforts
    assert "minimal" not in backend.reasoning_capabilities("gemini-3.1-pro-preview").efforts


def test_gemini_unknown_model_advertises_nothing():
    backend = GeminiBackend()
    assert backend.reasoning_capabilities("gemini-1.5-pro") is NO_REASONING_CONTROL
    assert backend.reasoning_capabilities("totally-unknown-gemini") is NO_REASONING_CONTROL
    assert backend.reasoning_capabilities() is NO_REASONING_CONTROL


def test_gemini_full_matrix_all_eleven_models_exact():
    """Every model in the spec's required minimum matrix, exact set +
    default, in one place."""
    backend = GeminiBackend()
    expected = {
        "gemini-3.7-flash": (("low", "medium", "high"), "medium"),
        "gemini-3.6-flash": (("minimal", "low", "medium", "high"), "medium"),
        "gemini-3.5-flash": (("minimal", "low", "medium", "high"), "medium"),
        "gemini-3.5-flash-lite": (("minimal", "low", "medium", "high"), "minimal"),
        "gemini-3.1-pro-preview": (("low", "medium", "high"), "high"),
        "gemini-3.1-flash-lite-image": (("minimal", "high"), "minimal"),
        "gemini-3-flash-preview": (("minimal", "low", "medium", "high"), "high"),
        "gemini-3-pro-preview": (("low", "high"), "high"),
        "gemini-2.5-pro": (("low", "medium", "high"), None),
        "gemini-2.5-flash": (("low", "medium", "high"), None),
        "gemini-2.5-flash-lite": (("low", "medium", "high"), None),
    }
    for model, (efforts, default) in expected.items():
        caps = backend.reasoning_capabilities(model)
        assert caps.efforts == efforts, f"{model}: efforts mismatch"
        assert caps.default_effort == default, f"{model}: default_effort mismatch"


# ── 4b. Gemini wire-shape translation -- POSITIVELY VERIFIED, not inert ──
#
# Verified 2026-08-20 against ai.google.dev/gemini-api/docs/generate-content/
# thinking and .../gemini-3 (Google's authoritative generateContent REST
# reference): the field is generationConfig.thinkingConfig.thinkingLevel
# (camelCase), and thinkingLevel/thinkingBudget must never both appear in
# one request ("Doing so will return a 400 error." -- quoted from that doc).

def test_gemini_valid_effort_emits_thinking_level_nested_in_generation_config():
    backend = GeminiBackend()
    payload = {
        "contents": [],
        "generationConfig": {"maxOutputTokens": 1024, "temperature": 0.7},
    }
    backend.apply_reasoning(payload, "high", model="gemini-3.7-flash")
    assert payload == {
        "contents": [],
        "generationConfig": {
            "maxOutputTokens": 1024,
            "temperature": 0.7,
            "thinkingConfig": {"thinkingLevel": "high"},
        },
    }


def test_gemini_valid_effort_creates_generation_config_if_absent():
    backend = GeminiBackend()
    payload = {"contents": []}
    backend.apply_reasoning(payload, "minimal", model="gemini-3.6-flash")
    assert payload == {
        "contents": [],
        "generationConfig": {"thinkingConfig": {"thinkingLevel": "minimal"}},
    }


def test_gemini_never_emits_thinking_budget_alongside_thinking_level():
    """Defensive proof of the spec's hard requirement: even if a
    thinkingBudget key were already present in thinkingConfig (legacy /
    hand-built payload), applying a validated effort must strip it rather
    than emit both."""
    backend = GeminiBackend()
    payload = {
        "contents": [],
        "generationConfig": {"thinkingConfig": {"thinkingBudget": 2048}},
    }
    backend.apply_reasoning(payload, "high", model="gemini-3.7-flash")
    assert payload["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "high"}
    assert "thinkingBudget" not in payload["generationConfig"]["thinkingConfig"]


def test_gemini_provider_default_emits_no_generation_config_mutation():
    backend = GeminiBackend()
    payload = {"contents": [], "generationConfig": {"maxOutputTokens": 1024}}
    before = {"contents": [], "generationConfig": {"maxOutputTokens": 1024}}
    backend.apply_reasoning(payload, None, model="gemini-3.7-flash")
    assert payload == before
    assert "thinkingConfig" not in payload["generationConfig"]


def test_gemini_unsupported_effort_for_model_emits_nothing():
    """'xhigh' is not in gemini-3.7-flash's advertised set (low/medium/high
    only) -- must fail safe, not silently pass an unadvertised level to
    Google's API."""
    backend = GeminiBackend()
    payload = {"contents": [], "generationConfig": {"maxOutputTokens": 1024}}
    before = dict(payload)
    backend.apply_reasoning(payload, "xhigh", model="gemini-3.7-flash")
    assert payload == before


# ── 5. Groq ──────────────────────────────────────────────────────────────

def test_groq_gpt_oss_accepts_low_medium_high():
    backend = GroqBackend()
    caps = backend.reasoning_capabilities("openai/gpt-oss-120b")
    assert caps.efforts == ("low", "medium", "high")
    caps_20b = backend.reasoning_capabilities("openai/gpt-oss-20b")
    assert caps_20b.efforts == ("low", "medium", "high")


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_groq_gpt_oss_emits_reasoning_effort_correctly_for_each_value(effort):
    backend = GroqBackend()
    payload = {"model": "openai/gpt-oss-120b", "messages": []}
    backend.apply_reasoning(payload, effort, model="openai/gpt-oss-120b")
    assert payload == {"model": "openai/gpt-oss-120b", "messages": [], "reasoning_effort": effort}


def test_groq_qwen_accepts_none_and_default():
    backend = GroqBackend()
    caps = backend.reasoning_capabilities("qwen/qwen3.6-27b")
    assert caps.efforts == ("none", "default")


def test_groq_qwen_default_emits_literal_provider_value_on_the_wire():
    """The critical namespace-distinction test called out explicitly in the
    spec: Qwen's advertised effort literal "default" is a REAL value that
    DOES reach the wire -- this is not the same thing as Lumina's None
    sentinel."""
    backend = GroqBackend()
    payload = {"model": "qwen/qwen3.6-27b", "messages": []}
    backend.apply_reasoning(payload, "default", model="qwen/qwen3.6-27b")
    assert payload == {"model": "qwen/qwen3.6-27b", "messages": [], "reasoning_effort": "default"}


def test_groq_qwen_none_effort_also_emits_literal_provider_value():
    """'none' is Qwen's OTHER real advertised value (distinct from
    Lumina's None sentinel, which is the Python object None, not the
    string 'none')."""
    backend = GroqBackend()
    payload = {"model": "qwen/qwen3.6-27b", "messages": []}
    backend.apply_reasoning(payload, "none", model="qwen/qwen3.6-27b")
    assert payload == {"model": "qwen/qwen3.6-27b", "messages": [], "reasoning_effort": "none"}


def test_groq_lumina_provider_default_none_sentinel_emits_no_field_at_all():
    """Directly contrasted against the two tests above: Lumina's own `None`
    request (Provider Default, not the string "none") must emit NOTHING,
    for the very same Qwen model that positively advertises "default" and
    "none" as real values."""
    backend = GroqBackend()
    payload = {"model": "qwen/qwen3.6-27b", "messages": []}
    before = dict(payload)
    backend.apply_reasoning(payload, None, model="qwen/qwen3.6-27b")
    assert payload == before
    assert "reasoning_effort" not in payload


def test_groq_gpt_oss_does_not_accept_default():
    backend = GroqBackend()
    caps = backend.reasoning_capabilities("openai/gpt-oss-120b")
    assert caps.validate("default") is None
    payload = {"model": "openai/gpt-oss-120b"}
    backend.apply_reasoning(payload, "default", model="openai/gpt-oss-120b")
    assert payload == {"model": "openai/gpt-oss-120b"}
    assert "reasoning_effort" not in payload


def test_groq_qwen_does_not_accept_high():
    backend = GroqBackend()
    caps = backend.reasoning_capabilities("qwen/qwen3.6-27b")
    assert caps.validate("high") is None
    payload = {"model": "qwen/qwen3.6-27b"}
    backend.apply_reasoning(payload, "high", model="qwen/qwen3.6-27b")
    assert payload == {"model": "qwen/qwen3.6-27b"}
    assert "reasoning_effort" not in payload


def test_groq_unknown_model_advertises_no_control():
    backend = GroqBackend()
    assert backend.reasoning_capabilities("llama-3.3-70b-versatile") is NO_REASONING_CONTROL
    assert backend.reasoning_capabilities("qwen/qwen3-32b") is NO_REASONING_CONTROL  # deprecated model, not in matrix


# ── 6. Inheritance safety -- no sibling leakage between LMStudio-derived classes ──

def test_lmstudio_backend_itself_remains_no_reasoning_control():
    backend = LMStudioBackend()
    assert backend.reasoning_capabilities() is NO_REASONING_CONTROL
    assert backend.reasoning_capabilities(model="gpt-5.6") is NO_REASONING_CONTROL
    assert backend.reasoning_capabilities(model="openai/gpt-oss-120b") is NO_REASONING_CONTROL


def test_qwen_backend_inheritance_safety_no_sibling_leakage():
    """QwenBackend also subclasses LMStudioBackend, same as OpenAIBackend
    and GroqBackend. Qwen/DashScope capability data was reserved for Part
    2B at the time this test was originally written (see git history) --
    Part 2B has since populated real data for qwen3.5-plus (its own,
    positively-verified DashScope hybrid-thinking data -- see
    core/backends/qwen.py's _REASONING_MODELS and the dedicated Part 2B
    test file for the full matrix), so that assertion was updated here
    rather than left stale. What this test still guards, unchanged: Qwen
    must not have picked up OpenAIBackend's or GroqBackend's capability
    data via any shared base state, in either direction."""
    backend = QwenBackend()
    assert backend.reasoning_capabilities() is NO_REASONING_CONTROL
    # qwen3.5-plus is now a REAL, positively-verified capability (Part 2B)
    # -- no longer NO_REASONING_CONTROL. This is the intended, documented
    # change, not a regression.
    caps = backend.reasoning_capabilities(model="qwen3.5-plus")
    assert caps is not NO_REASONING_CONTROL
    assert caps.efforts == ("disabled", "enabled")
    # "qwen-max" IS in QwenBackend's own KNOWN_MODELS but was deliberately
    # left unverified/unmapped in Part 2B (see qwen.py's module comment) --
    # still a real proof point that not every KNOWN_MODELS entry
    # automatically gets capability data invented for it.
    assert backend.reasoning_capabilities(model="qwen-max") is NO_REASONING_CONTROL
    # Even Groq's own confirmed Qwen3 model id must not leak into
    # QwenBackend (different backend/provider entirely) or vice versa --
    # capability data is genuinely per-class, not per-model-name-string.
    assert backend.reasoning_capabilities(model="qwen/qwen3.6-27b") is NO_REASONING_CONTROL


def test_openrouter_backend_untouched_in_this_slice_remains_no_reasoning_control():
    """OpenRouterBackend also subclasses LMStudioBackend -- reserved for a
    later Part 2B, must not have picked up any capability data."""
    backend = OpenRouterBackend()
    assert backend.reasoning_capabilities() is NO_REASONING_CONTROL
    assert backend.reasoning_capabilities(model="gpt-5.6") is NO_REASONING_CONTROL
    assert backend.reasoning_capabilities(model="openai/gpt-oss-120b") is NO_REASONING_CONTROL


def test_openai_and_groq_capabilities_do_not_leak_into_each_other():
    """OpenAIBackend's gpt-5.6 family must not be recognized by GroqBackend,
    and Groq's gpt-oss/qwen models must not be recognized by OpenAIBackend
    -- capability data lives only on the concrete class that declared it."""
    openai_backend = OpenAIBackend()
    groq_backend = GroqBackend()

    assert groq_backend.reasoning_capabilities("gpt-5.6") is NO_REASONING_CONTROL
    assert openai_backend.reasoning_capabilities("openai/gpt-oss-120b") is NO_REASONING_CONTROL
    assert openai_backend.reasoning_capabilities("qwen/qwen3.6-27b") is NO_REASONING_CONTROL


def test_concrete_provider_overrides_still_report_their_own_real_capabilities():
    """Positive half of the sibling-leakage proof: while siblings correctly
    report NO_REASONING_CONTROL, the actual owning classes still report
    their real data for their own models -- the isolation isn't achieved
    by accidentally breaking everyone."""
    assert OpenAIBackend().reasoning_capabilities("gpt-5.6").efforts == (
        "none", "low", "medium", "high", "xhigh", "max")
    assert GroqBackend().reasoning_capabilities("openai/gpt-oss-120b").efforts == (
        "low", "medium", "high")
    assert GroqBackend().reasoning_capabilities("qwen/qwen3.6-27b").efforts == (
        "none", "default")
