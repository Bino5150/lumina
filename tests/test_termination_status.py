"""
AGENT-CONTINUATION-01A -- TerminationStatus mapping, direct against each
concrete backend's extract_termination(). Values are deliberately narrow
and source-vetted (see docstrings in core/backends/base.py,
anthropic_backend.py, gemini_backend.py for exactly where each value came
from): only what's positively understood maps to COMPLETE/INCOMPLETE,
everything else -- including a missing field entirely -- stays UNKNOWN.
"""
from core.backends.base import TerminationStatus
from core.backends.lmstudio import LMStudioBackend
from core.backends.openrouter import OpenRouterBackend
from core.backends.anthropic_backend import AnthropicBackend
from core.backends.gemini_backend import GeminiBackend


# ── OpenAI-compatible family (concrete BaseLLMBackend default) ─────────

def test_oai_stop_is_complete():
    backend = LMStudioBackend(base_url="http://localhost:1234/v1")
    resp = {"choices": [{"finish_reason": "stop"}]}
    assert backend.extract_termination(resp) == TerminationStatus.COMPLETE


def test_oai_eos_is_complete():
    backend = LMStudioBackend(base_url="http://localhost:1234/v1")
    resp = {"choices": [{"finish_reason": "eos"}]}
    assert backend.extract_termination(resp) == TerminationStatus.COMPLETE


def test_oai_tool_calls_is_complete():
    backend = LMStudioBackend(base_url="http://localhost:1234/v1")
    resp = {"choices": [{"finish_reason": "tool_calls"}]}
    assert backend.extract_termination(resp) == TerminationStatus.COMPLETE


def test_oai_length_is_incomplete():
    backend = LMStudioBackend(base_url="http://localhost:1234/v1")
    resp = {"choices": [{"finish_reason": "length"}]}
    assert backend.extract_termination(resp) == TerminationStatus.INCOMPLETE


def test_oai_content_filter_is_unknown_not_guessed():
    backend = LMStudioBackend(base_url="http://localhost:1234/v1")
    resp = {"choices": [{"finish_reason": "content_filter"}]}
    assert backend.extract_termination(resp) == TerminationStatus.UNKNOWN


def test_oai_missing_finish_reason_is_unknown():
    backend = LMStudioBackend(base_url="http://localhost:1234/v1")
    resp = {"choices": [{}]}
    assert backend.extract_termination(resp) == TerminationStatus.UNKNOWN


def test_oai_malformed_response_is_unknown_not_a_crash():
    backend = LMStudioBackend(base_url="http://localhost:1234/v1")
    assert backend.extract_termination({}) == TerminationStatus.UNKNOWN
    assert backend.extract_termination({"choices": []}) == TerminationStatus.UNKNOWN


def test_openrouter_inherits_the_same_oai_mapping():
    """OpenRouterBackend never overrides extract_termination -- it should
    get the concrete base default for free, same as every other
    LMStudioBackend descendant. Directly regression-guards the live
    AGENT-CONTINUATION-01 capture: real GLM/OpenRouter response carried
    both "native_finish_reason" and a normalized "finish_reason": "length"
    -- this backend must classify the latter as INCOMPLETE."""
    backend = OpenRouterBackend(api_key="test-key")
    resp = {
        "choices": [{"finish_reason": "length", "native_finish_reason": "length"}],
    }
    assert backend.extract_termination(resp) == TerminationStatus.INCOMPLETE


# ── Anthropic ────────────────────────────────────────────────────────────

def test_anthropic_end_turn_is_complete():
    backend = AnthropicBackend(api_key="test-key")
    assert backend.extract_termination({"stop_reason": "end_turn"}) == TerminationStatus.COMPLETE


def test_anthropic_tool_use_is_complete():
    backend = AnthropicBackend(api_key="test-key")
    assert backend.extract_termination({"stop_reason": "tool_use"}) == TerminationStatus.COMPLETE


def test_anthropic_max_tokens_is_incomplete():
    backend = AnthropicBackend(api_key="test-key")
    assert backend.extract_termination({"stop_reason": "max_tokens"}) == TerminationStatus.INCOMPLETE


def test_anthropic_stop_sequence_is_unknown_not_guessed():
    backend = AnthropicBackend(api_key="test-key")
    assert backend.extract_termination({"stop_reason": "stop_sequence"}) == TerminationStatus.UNKNOWN


def test_anthropic_missing_stop_reason_is_unknown():
    backend = AnthropicBackend(api_key="test-key")
    assert backend.extract_termination({}) == TerminationStatus.UNKNOWN


# ── Gemini ───────────────────────────────────────────────────────────────

def test_gemini_stop_is_complete():
    backend = GeminiBackend(api_key="test-key")
    resp = {"candidates": [{"finishReason": "STOP"}]}
    assert backend.extract_termination(resp) == TerminationStatus.COMPLETE


def test_gemini_max_tokens_is_incomplete():
    backend = GeminiBackend(api_key="test-key")
    resp = {"candidates": [{"finishReason": "MAX_TOKENS"}]}
    assert backend.extract_termination(resp) == TerminationStatus.INCOMPLETE


def test_gemini_safety_is_unknown_not_guessed():
    backend = GeminiBackend(api_key="test-key")
    resp = {"candidates": [{"finishReason": "SAFETY"}]}
    assert backend.extract_termination(resp) == TerminationStatus.UNKNOWN


def test_gemini_missing_candidates_is_unknown_not_a_crash():
    backend = GeminiBackend(api_key="test-key")
    assert backend.extract_termination({}) == TerminationStatus.UNKNOWN
    assert backend.extract_termination({"candidates": []}) == TerminationStatus.UNKNOWN
