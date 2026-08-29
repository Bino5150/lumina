"""AGENT-TOOL-THINK-TELEMETRY-01A1 -- explicit scope boundary.

This ticket's hard scope line: passive extract_reasoning() overrides only
for backends whose non-streaming response already carries a reasoning
field (the LMStudioBackend family + OllamaBackend -- see
tests/test_lmstudio_backend.py and tests/test_ollama_backend.py).
AnthropicBackend and GeminiBackend get NO override -- A0's source-vet and
this ticket's own Phase 0 confirmed neither backend's chat() payload
requests extended thinking / includeThoughts today, so there is nothing on
their wire to extract, and this slice must not start requesting it (that
would be a provider-behavior change, out of scope). This file exists so a
future accidental override on either class fails a real test instead of
silently drifting the scope line.
"""
from core.backends.anthropic_backend import AnthropicBackend
from core.backends.gemini_backend import GeminiBackend
from core.backends.base import BaseLLMBackend


def test_anthropic_backend_does_not_override_extract_reasoning():
    assert "extract_reasoning" not in AnthropicBackend.__dict__


def test_gemini_backend_does_not_override_extract_reasoning():
    assert "extract_reasoning" not in GeminiBackend.__dict__


def test_anthropic_backend_extract_reasoning_returns_none():
    backend = AnthropicBackend.__new__(AnthropicBackend)
    assert backend.extract_reasoning({"content": [{"type": "thinking", "thinking": "x"}]}) is None


def test_gemini_backend_extract_reasoning_returns_none():
    backend = GeminiBackend.__new__(GeminiBackend)
    assert backend.extract_reasoning({"candidates": [{"content": {"parts": [{"thought": True, "text": "x"}]}}]}) is None


def test_base_default_extract_reasoning_is_none():
    """Fail-safe default for any backend that never overrides this at
    all -- same posture as reasoning_capabilities()'s NO_REASONING_CONTROL."""

    class _Bare(BaseLLMBackend):
        def get_model(self): return "x"
        def list_models(self): return []
        def health_check(self): return True, "ok"
        def chat(self, *a, **kw): return {}
        def chat_stream(self, *a, **kw): yield ""

    assert _Bare().extract_reasoning({"anything": "goes"}) is None
