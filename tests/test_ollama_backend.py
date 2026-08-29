"""core/backends/ollama.py -- AGENT-TOOL-THINK-TELEMETRY-01A1.

OllamaBackend shares LMStudioBackend's exact OpenAI-compatible response
shape (module docstring: "nearly identical to LM Studio") but doesn't
subclass it, so extract_reasoning() needs its own one-line delegation to
the shared core.backends.lmstudio.extract_openai_compatible_reasoning()
parser rather than inheriting it. This file only covers that delegation --
the parser's own field-priority/malformed-shape behavior is already
covered directly in tests/test_lmstudio_backend.py.
"""
from core.backends.ollama import OllamaBackend


def _oai_response(message: dict) -> dict:
    return {"choices": [{"message": message, "finish_reason": "tool_calls"}]}


def test_ollama_extract_reasoning_delegates_to_shared_openai_compatible_parser():
    backend = OllamaBackend()
    resp = _oai_response({"role": "assistant", "content": "", "reasoning_content": "ollama reasoning"})
    assert backend.extract_reasoning(resp) == "ollama reasoning"


def test_ollama_extract_reasoning_absent_returns_none():
    backend = OllamaBackend()
    resp = _oai_response({"role": "assistant", "content": "", "tool_calls": []})
    assert backend.extract_reasoning(resp) is None


def test_ollama_extract_reasoning_malformed_response_returns_none_not_raise():
    backend = OllamaBackend()
    assert backend.extract_reasoning({}) is None
    assert backend.extract_reasoning(None) is None
