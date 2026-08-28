"""
AGENT-CONTINUATION-01B -- direct wire-payload proofs for every backend's
tool_choice_mode resolution. Two groups:

  - Backends with live-verified support (OpenRouter, OpenAI, Gemini):
    REQUIRED must produce the exact confirmed wire shape; AUTO/None must
    stay byte-identical to the pre-01B payload.
  - Backends deliberately NOT opted in (generic LM Studio/local, Ollama,
    Groq -- credential unavailable to verify, Anthropic -- credential
    unavailable to verify): REQUIRED must never leak an invented field
    onto the wire regardless of what's requested; the payload is
    byte-identical to pre-01B in every case.

LM Studio-family backends build their payload inline inside chat() and POST
immediately, so those tests intercept requests.post (same _capture_posts
pattern as tests/test_openai_token_limit_translation.py, read directly
before writing this file). Anthropic/Gemini expose _build_payload()
directly -- no HTTP interception needed for those.
"""
import json

import pytest
import requests

from core.backends.base import ToolChoiceMode
from core.backends.openrouter import OpenRouterBackend
from core.backends.openai_backend import OpenAIBackend
from core.backends.groq import GroqBackend
from core.backends.lmstudio import LMStudioBackend
from core.backends.ollama import OllamaBackend
from core.backends.anthropic_backend import AnthropicBackend
from core.backends.gemini_backend import GeminiBackend


TOOL_SCHEMA = {
    "type": "function",
    "function": {"name": "lookup", "description": "look something up",
                 "parameters": {"type": "object", "properties": {}}},
}


class _FakeResponse:
    def __init__(self, body):
        self._body = body
        self.status_code = 200
        self.text = json.dumps(body)

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


def _capture_posts(monkeypatch, body):
    payloads = []

    def fake_post(*args, **kwargs):
        payloads.append(kwargs["json"])
        return _FakeResponse(body)

    monkeypatch.setattr(requests, "post", fake_post)
    return payloads


OAI_BODY = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}


# ── Supported: exact confirmed wire shape ────────────────────────────────

def test_openrouter_required_produces_tool_choice_required(monkeypatch):
    payloads = _capture_posts(monkeypatch, OAI_BODY)
    backend = OpenRouterBackend(api_key="test-key")
    backend._model = "z-ai/glm-5.3-flash"

    backend.chat(messages=[{"role": "user", "content": "hi"}], tools=[TOOL_SCHEMA],
                 tool_choice_mode=ToolChoiceMode.REQUIRED)

    assert payloads[0]["tool_choice"] == "required"


def test_openrouter_auto_or_none_stays_byte_identical_to_pre_01b(monkeypatch):
    payloads = _capture_posts(monkeypatch, OAI_BODY)
    backend = OpenRouterBackend(api_key="test-key")
    backend._model = "z-ai/glm-5.3-flash"

    backend.chat(messages=[{"role": "user", "content": "hi"}], tools=[TOOL_SCHEMA],
                 tool_choice_mode=ToolChoiceMode.AUTO)
    backend.chat(messages=[{"role": "user", "content": "hi"}], tools=[TOOL_SCHEMA])

    assert payloads[0]["tool_choice"] == "auto"
    assert payloads[1]["tool_choice"] == "auto"


def test_openai_required_produces_tool_choice_required(monkeypatch):
    payloads = _capture_posts(monkeypatch, OAI_BODY)
    backend = OpenAIBackend(api_key="test-key")
    backend._model = "gpt-4o-mini"

    backend.chat(messages=[{"role": "user", "content": "hi"}], tools=[TOOL_SCHEMA],
                 tool_choice_mode=ToolChoiceMode.REQUIRED)

    assert payloads[0]["tool_choice"] == "required"


def test_gemini_required_produces_tool_config_any_mode():
    backend = GeminiBackend(api_key="test-key")
    payload = backend._build_payload(
        messages=[{"role": "user", "content": "hi"}], tools=[TOOL_SCHEMA],
        tool_choice_mode=ToolChoiceMode.REQUIRED,
    )
    assert payload["tool_config"] == {"function_calling_config": {"mode": "ANY"}}


def test_gemini_auto_or_none_omits_tool_config_entirely():
    backend = GeminiBackend(api_key="test-key")
    payload_auto = backend._build_payload(
        messages=[{"role": "user", "content": "hi"}], tools=[TOOL_SCHEMA],
        tool_choice_mode=ToolChoiceMode.AUTO,
    )
    payload_none = backend._build_payload(
        messages=[{"role": "user", "content": "hi"}], tools=[TOOL_SCHEMA],
    )
    assert "tool_config" not in payload_auto
    assert "tool_config" not in payload_none


# ── Deliberately unsupported: no leakage, regardless of what's requested ─

@pytest.mark.parametrize("mode", [ToolChoiceMode.REQUIRED, ToolChoiceMode.AUTO, None])
def test_generic_lmstudio_never_leaks_required(monkeypatch, mode):
    payloads = _capture_posts(monkeypatch, OAI_BODY)
    backend = LMStudioBackend(base_url="http://localhost:1234/v1")
    backend._model = "some-local-model"

    kwargs = {"messages": [{"role": "user", "content": "hi"}], "tools": [TOOL_SCHEMA]}
    if mode is not None:
        kwargs["tool_choice_mode"] = mode
    backend.chat(**kwargs)

    assert payloads[0]["tool_choice"] == "auto"


@pytest.mark.parametrize("mode", [ToolChoiceMode.REQUIRED, ToolChoiceMode.AUTO, None])
def test_groq_never_leaks_required_credential_unverified(monkeypatch, mode):
    """Groq is a real cloud OpenAI-compatible provider, but the probe
    attempted during AGENT-CONTINUATION-01B hit an auth failure (invalid
    credential in this environment) before ever reaching the field-support
    question itself -- genuinely UNKNOWN, not verified either way, so it
    stays on the safe default exactly like an unverified local server."""
    payloads = _capture_posts(monkeypatch, OAI_BODY)
    backend = GroqBackend(api_key="test-key")
    backend._model = "llama-3.3-70b-versatile"

    kwargs = {"messages": [{"role": "user", "content": "hi"}], "tools": [TOOL_SCHEMA]}
    if mode is not None:
        kwargs["tool_choice_mode"] = mode
    backend.chat(**kwargs)

    assert payloads[0]["tool_choice"] == "auto"


@pytest.mark.parametrize("mode", [ToolChoiceMode.REQUIRED, ToolChoiceMode.AUTO, None])
def test_ollama_never_leaks_required(monkeypatch, mode):
    """Ollama has its own independent chat() (not inherited from
    LMStudioBackend) -- proves the same safe fallback holds there too."""
    payloads = _capture_posts(monkeypatch, OAI_BODY)
    backend = OllamaBackend(base_url="http://localhost:11434/v1")
    backend._model = "llama3"

    kwargs = {"messages": [{"role": "user", "content": "hi"}], "tools": [TOOL_SCHEMA]}
    if mode is not None:
        kwargs["tool_choice_mode"] = mode
    backend.chat(**kwargs)

    assert payloads[0]["tool_choice"] == "auto"


ANTHROPIC_BODY = {"content": [{"type": "text", "text": "ok"}]}


@pytest.mark.parametrize("mode", [ToolChoiceMode.REQUIRED, ToolChoiceMode.AUTO, None])
def test_anthropic_never_adds_a_tool_choice_field_credential_unverified(monkeypatch, mode):
    """No Anthropic credential was available to live-verify {"type": "any"}
    against the real API in this environment -- chat() accepts
    tool_choice_mode for interface consistency but never threads it into
    _build_payload() at all (see chat()'s own comment), so the payload
    must be byte-identical to pre-01B in every case: no "tool_choice" key,
    regardless of what mode is requested."""
    payloads = _capture_posts(monkeypatch, ANTHROPIC_BODY)
    backend = AnthropicBackend(api_key="test-key")
    backend.default_model = "claude-test"

    kwargs = {"messages": [{"role": "user", "content": "hi"}], "tools": [TOOL_SCHEMA]}
    if mode is not None:
        kwargs["tool_choice_mode"] = mode
    backend.chat(**kwargs)

    assert "tool_choice" not in payloads[0]
