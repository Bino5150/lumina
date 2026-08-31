"""CONTEXT-LIFECYCLE-A4I -- BaseLLMBackend.complete_utility_content_only().

Pins the A4D-R-designed content-only utility completion split: shared
transport/cleanup plumbing (_complete_utility_request/_strip_utility_think_
leakage), complete_utility() unchanged for every existing caller, and a new
complete_utility_content_only() with NO code path capable of returning
reasoning_content-sourced text.

No live network: a minimal concrete BaseLLMBackend subclass stubs chat()
directly (same style tests/test_backend_error_01.py uses at the requests
layer, one level higher here since the split under test lives above chat()).
"""
import core.backends.base as base_module
from core.backends.base import BaseLLMBackend


class _StubBackend(BaseLLMBackend):
    """Minimal concrete backend: chat() returns whatever response dict the
    test pre-loaded, extract_message() uses the real base implementation."""

    default_url = "http://stub.invalid"

    def __init__(self, response=None, raise_on_chat=None):
        self._response = response
        self._raise_on_chat = raise_on_chat
        self.chat_calls = []

    def get_model(self):
        return "stub-model"

    def list_models(self):
        return ["stub-model"]

    def health_check(self):
        return True, "ok"

    def chat(self, messages, tools=None, temperature=0.7, max_tokens=1024,
             disable_thinking=False, reasoning_effort=None, tool_choice_mode=None):
        self.chat_calls.append({
            "messages": messages, "tools": tools, "temperature": temperature,
            "max_tokens": max_tokens, "disable_thinking": disable_thinking,
        })
        if self._raise_on_chat is not None:
            raise self._raise_on_chat
        return self._response

    def chat_stream(self, messages, max_tokens=1024, temperature=0.7, reasoning_effort=None):
        yield ""


def _response(content=None, reasoning_content=None):
    message = {}
    if content is not None:
        message["content"] = content
    if reasoning_content is not None:
        message["reasoning_content"] = reasoning_content
    return {"choices": [{"message": message, "finish_reason": "stop"}]}


# ── content returned normally ──────────────────────────────────────────

def test_content_only_returns_plain_content():
    backend = _StubBackend(response=_response(content="the answer"))
    assert backend.complete_utility_content_only("prompt") == "the answer"


def test_legacy_complete_utility_returns_plain_content_unchanged():
    backend = _StubBackend(response=_response(content="the answer"))
    assert backend.complete_utility("prompt") == "the answer"


# ── the critical regression: reasoning_content-only response ────────────

def test_critical_regression_reasoning_content_only():
    """content="" , reasoning_content='{"reported":[...]}':
    complete_utility() preserves the legacy fallback (S41/F-62);
    complete_utility_content_only() must return None, never the
    reasoning-lane text -- even though it happens to look like valid JSON."""
    response = _response(content="", reasoning_content='{"reported":[{"category":"objective"}]}')

    legacy_backend = _StubBackend(response=response)
    assert legacy_backend.complete_utility("prompt") == '{"reported":[{"category":"objective"}]}'

    strict_backend = _StubBackend(response=response)
    assert strict_backend.complete_utility_content_only("prompt") is None


def test_content_only_ignores_reasoning_content_even_when_content_present():
    response = _response(content="real answer", reasoning_content="some reasoning trace")
    backend = _StubBackend(response=response)
    assert backend.complete_utility_content_only("prompt") == "real answer"


def test_content_only_missing_content_key_entirely_returns_none():
    """message has neither 'content' nor 'reasoning_content' at all."""
    response = {"choices": [{"message": {}, "finish_reason": "stop"}]}
    backend = _StubBackend(response=response)
    assert backend.complete_utility_content_only("prompt") is None


# ── <think>-wrapped content stripped identically on both paths ──────────

def test_content_only_strips_closed_think_block():
    response = _response(content="<think>internal reasoning</think>SUMMARY: the real text")
    backend = _StubBackend(response=response)
    assert backend.complete_utility_content_only("prompt", prefill="SUMMARY:") == "the real text"


def test_content_only_strips_unclosed_think_block():
    response = _response(content="<think>truncated mid reasoning trace with no closing tag")
    backend = _StubBackend(response=response)
    assert backend.complete_utility_content_only("prompt") is None


def test_legacy_and_content_only_strip_think_identically_when_content_present():
    response = _response(content="<think>x</think>TITLE: My Chat")
    legacy = _StubBackend(response=response).complete_utility("p", prefill="TITLE:")
    strict = _StubBackend(response=response).complete_utility_content_only("p", prefill="TITLE:")
    assert legacy == strict == "My Chat"


# ── transport failure -> None on both, distinct log labels ──────────────

def test_content_only_transport_exception_returns_none():
    backend = _StubBackend(raise_on_chat=RuntimeError("connection refused"))
    assert backend.complete_utility_content_only("prompt") is None


def test_legacy_transport_exception_returns_none():
    backend = _StubBackend(raise_on_chat=RuntimeError("connection refused"))
    assert backend.complete_utility("prompt") is None


def test_content_only_malformed_response_shape_returns_none():
    """extract_message() raises ValueError on a malformed response --
    must collapse to None like every other transport failure, not propagate."""
    backend = _StubBackend(response={"unexpected": "shape"})
    assert backend.complete_utility_content_only("prompt") is None


def test_distinct_log_labels_on_failure(capsys):
    _StubBackend(raise_on_chat=RuntimeError("boom")).complete_utility("p")
    out1 = capsys.readouterr().out
    assert "complete_utility failed" in out1
    assert "complete_utility_content_only" not in out1

    _StubBackend(raise_on_chat=RuntimeError("boom")).complete_utility_content_only("p")
    out2 = capsys.readouterr().out
    assert "complete_utility_content_only failed" in out2


# ── request shape: tools=None, disable_thinking=True, unchanged ─────────

def test_content_only_request_shape_matches_legacy():
    backend = _StubBackend(response=_response(content="x"))
    backend.complete_utility_content_only("prompt text", prefill="PFX:", max_tokens=42, temperature=0.1)
    assert len(backend.chat_calls) == 1
    call = backend.chat_calls[0]
    assert call["tools"] is None
    assert call["disable_thinking"] is True
    assert call["max_tokens"] == 42
    assert call["temperature"] == 0.1
    assert call["messages"] == [
        {"role": "user", "content": "prompt text"},
        {"role": "assistant", "content": "PFX:"},
    ]


def test_content_only_no_prefill_omits_assistant_message():
    backend = _StubBackend(response=_response(content="x"))
    backend.complete_utility_content_only("prompt text")
    assert backend.chat_calls[0]["messages"] == [{"role": "user", "content": "prompt text"}]


# ── no backend overrides either method (single shared implementation) ───

def test_no_backend_subclass_overrides_complete_utility_content_only():
    import inspect
    import core.backends.anthropic_backend as anthropic_backend
    import core.backends.gemini_backend as gemini_backend
    import core.backends.lmstudio as lmstudio
    import core.backends.ollama as ollama

    for module, cls_name in (
        (anthropic_backend, "AnthropicBackend"),
        (gemini_backend, "GeminiBackend"),
        (lmstudio, "LMStudioBackend"),
        (ollama, "OllamaBackend"),
    ):
        cls = getattr(module, cls_name)
        assert cls.complete_utility_content_only is BaseLLMBackend.complete_utility_content_only
        assert cls.complete_utility is BaseLLMBackend.complete_utility
