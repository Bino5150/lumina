"""
Patch 3A.4 Part 3 -- reasoning_effort payload plumbing through the real
chat()/chat_stream() request builders (not just apply_reasoning() called
directly, which Part 1/2A/2B's own test files already cover).

Every test here goes through a backend's actual chat()/chat_stream()
method with HTTP mocked (requests.post/requests.get patched, following the
exact convention already used by tests/test_lmstudio_backend.py and
tests/test_reasoning_translation_part2b.py's OpenRouter section), and
inspects the EXACT payload dict that would have been sent on the wire --
never a mocked-call assertion alone.

Sections:
  1. OpenAIBackend  -- chat()/chat_stream() reasoning_effort key
  2. AnthropicBackend -- chat()/chat_stream() output_config.effort
  3. GeminiBackend -- chat()/chat_stream() thinkingConfig.thinkingLevel
  4. GroqBackend -- GPT-OSS effort + Qwen-on-Groq literal "default" vs None
  5. OpenRouterBackend -- primed vs unprimed capability cache
  6. QwenBackend -- enable_thinking through the full widened chat() path
  7. OmniRouteBackend / OllamaBackend -- accept-and-ignore, no-op confirmed
  8. disable_thinking always wins over a conflicting reasoning_effort
  9. No hidden state -- back-to-back calls with different efforts don't
     cross-contaminate the same backend instance
"""
import json

import requests

from core.backends.openai_backend import OpenAIBackend
from core.backends.anthropic_backend import AnthropicBackend
from core.backends.gemini_backend import GeminiBackend
from core.backends.groq import GroqBackend
from core.backends.openrouter import OpenRouterBackend
from core.backends.qwen import QwenBackend
from core.backends.omniroute import OmniRouteBackend
from core.backends.ollama import OllamaBackend


# ══════════════════════════════════════════════════════════════════════
# Shared HTTP-mocking helpers
# ══════════════════════════════════════════════════════════════════════

class _FakeResp:
    """Minimal stand-in for a requests.Response covering both the
    non-streaming (.json()) and streaming (.iter_lines()) paths used
    across every backend in this file."""

    def __init__(self, status_code=200, json_body=None, stream_lines=None):
        self.status_code = status_code
        self._json_body = json_body if json_body is not None else {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        }
        self.text = json.dumps(self._json_body)
        self._stream_lines = stream_lines or []

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} error")

    def json(self):
        return self._json_body

    def iter_lines(self):
        return iter(self._stream_lines)


def _capture_post(monkeypatch, json_body=None, stream_lines=None):
    """Patch the shared requests.post (every backend module does `import
    requests` then calls requests.post(...) fully qualified, so patching
    the one shared module object here affects all of them) and return a
    dict that gets filled in with the exact call's url/kwargs once made."""
    captured = {}

    def _fake_post(*args, **kwargs):
        captured["url"] = args[0] if args else kwargs.get("url")
        captured["json"] = kwargs.get("json")
        captured["kwargs"] = kwargs
        return _FakeResp(json_body=json_body, stream_lines=stream_lines)

    monkeypatch.setattr(requests, "post", _fake_post)
    return captured


def _capture_get(monkeypatch, models_data):
    """models_data: the list that would appear under the /models response's
    top-level "data" key (OpenRouterBackend.list_models() reads
    resp.json().get("data", []) -- confirmed against the real source)."""
    def _fake_get(*args, **kwargs):
        return _FakeResp(json_body={"data": models_data})

    monkeypatch.setattr(requests, "get", _fake_get)


def _drain(gen):
    """Force a generator-based chat_stream() to actually execute its body
    (the HTTP call happens before any yield) -- list() safely consumes it
    even if it never yields anything."""
    return list(gen)


# ══════════════════════════════════════════════════════════════════════
# 1. OpenAIBackend -- reasoning object on the Responses payload
# ══════════════════════════════════════════════════════════════════════
#
# OPENAI-RESPONSES-01: OpenAIBackend now speaks /v1/responses, whose
# reasoning wire shape is a nested {"reasoning": {"effort": ..., "summary":
# "auto"}} object, not Chat Completions' flat "reasoning_effort" string
# field (see core/backends/openai_backend.py's module docstring for the
# live-verified root cause this migration fixes).

def test_openai_chat_valid_effort_reaches_reasoning_object(monkeypatch):
    captured = _capture_post(monkeypatch)
    backend = OpenAIBackend()
    backend._model = "gpt-5.6"

    backend.chat(messages=[{"role": "user", "content": "hi"}], reasoning_effort="high")

    assert captured["json"]["reasoning"] == {"effort": "high", "summary": "auto"}
    assert captured["json"]["model"] == "gpt-5.6"


def test_openai_chat_stream_valid_effort_reaches_reasoning_object(monkeypatch):
    captured = _capture_post(monkeypatch)
    backend = OpenAIBackend()
    backend._model = "gpt-5.6"

    _drain(backend.chat_stream(messages=[{"role": "user", "content": "hi"}], reasoning_effort="high"))

    assert captured["json"]["reasoning"] == {"effort": "high", "summary": "auto"}


def test_openai_chat_provider_default_none_emits_no_field(monkeypatch):
    captured = _capture_post(monkeypatch)
    backend = OpenAIBackend()
    backend._model = "gpt-5.6"

    backend.chat(messages=[{"role": "user", "content": "hi"}], reasoning_effort=None)

    assert "reasoning" not in captured["json"]


def test_openai_chat_stream_provider_default_none_emits_no_field(monkeypatch):
    captured = _capture_post(monkeypatch)
    backend = OpenAIBackend()
    backend._model = "gpt-5.6"

    _drain(backend.chat_stream(messages=[{"role": "user", "content": "hi"}], reasoning_effort=None))

    assert "reasoning" not in captured["json"]


def test_openai_chat_unrecognized_model_emits_no_field_even_for_valid_looking_effort(monkeypatch):
    """A model with no entry in OpenAIBackend's capability table (e.g. the
    default gpt-4o-mini) must never emit a reasoning object even for a
    string that IS a real effort label on gpt-5.6 -- capability is keyed
    per-model, not accepted globally."""
    captured = _capture_post(monkeypatch)
    backend = OpenAIBackend()
    backend._model = "gpt-4o-mini"

    backend.chat(messages=[{"role": "user", "content": "hi"}], reasoning_effort="high")

    assert "reasoning" not in captured["json"]


# ══════════════════════════════════════════════════════════════════════
# 2. AnthropicBackend -- output_config.effort
# ══════════════════════════════════════════════════════════════════════

def test_anthropic_chat_xhigh_on_sonnet_5_reaches_output_config_effort(monkeypatch):
    captured = _capture_post(monkeypatch)
    backend = AnthropicBackend()
    backend.default_model = "claude-sonnet-5"

    backend.chat(messages=[{"role": "user", "content": "hi"}], reasoning_effort="xhigh")

    assert captured["json"]["output_config"]["effort"] == "xhigh"


def test_anthropic_chat_stream_uses_the_same_translator(monkeypatch):
    captured = _capture_post(monkeypatch)
    backend = AnthropicBackend()
    backend.default_model = "claude-sonnet-5"

    _drain(backend.chat_stream(messages=[{"role": "user", "content": "hi"}], reasoning_effort="xhigh"))

    assert captured["json"]["output_config"]["effort"] == "xhigh"


def test_anthropic_xhigh_not_supported_on_sonnet_4_6(monkeypatch):
    """Confirms the two Sonnet ids genuinely have distinct effort
    matrices -- xhigh is valid on sonnet-5 but not sonnet-4-6."""
    captured = _capture_post(monkeypatch)
    backend = AnthropicBackend()
    backend.default_model = "claude-sonnet-4-6"

    backend.chat(messages=[{"role": "user", "content": "hi"}], reasoning_effort="xhigh")

    assert "output_config" not in captured["json"]


def test_anthropic_output_config_siblings_preserved():
    """_apply_reasoning_override() merges into output_config rather than
    overwriting it -- directly against apply_reasoning(), same invariant
    Part 2A's own tests already pin, re-confirmed here through the full
    Part 3 chat()-path context for completeness."""
    backend = AnthropicBackend()
    payload = {"model": "claude-sonnet-5", "output_config": {"some_other_key": True}}
    backend.apply_reasoning(payload, "high", model="claude-sonnet-5")
    assert payload["output_config"] == {"some_other_key": True, "effort": "high"}


def test_anthropic_chat_provider_default_none_leaves_output_config_untouched(monkeypatch):
    captured = _capture_post(monkeypatch)
    backend = AnthropicBackend()
    backend.default_model = "claude-sonnet-5"

    backend.chat(messages=[{"role": "user", "content": "hi"}], reasoning_effort=None)

    assert "output_config" not in captured["json"]


def test_anthropic_chat_stream_provider_default_none_leaves_output_config_untouched(monkeypatch):
    captured = _capture_post(monkeypatch)
    backend = AnthropicBackend()
    backend.default_model = "claude-sonnet-5"

    _drain(backend.chat_stream(messages=[{"role": "user", "content": "hi"}], reasoning_effort=None))

    assert "output_config" not in captured["json"]


# ══════════════════════════════════════════════════════════════════════
# 3. GeminiBackend -- thinkingConfig.thinkingLevel
# ══════════════════════════════════════════════════════════════════════

def test_gemini_chat_valid_effort_reaches_thinking_level(monkeypatch):
    captured = _capture_post(monkeypatch)
    backend = GeminiBackend()
    backend.default_model = "gemini-2.5-pro"

    backend.chat(messages=[{"role": "user", "content": "hi"}], reasoning_effort="high")

    assert captured["json"]["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "high"


def test_gemini_chat_stream_valid_effort_reaches_thinking_level(monkeypatch):
    captured = _capture_post(monkeypatch)
    backend = GeminiBackend()
    backend.default_model = "gemini-2.5-pro"

    _drain(backend.chat_stream(messages=[{"role": "user", "content": "hi"}], reasoning_effort="high"))

    assert captured["json"]["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "high"


def test_gemini_chat_provider_default_none_emits_no_thinking_config(monkeypatch):
    captured = _capture_post(monkeypatch)
    backend = GeminiBackend()
    backend.default_model = "gemini-2.5-pro"

    backend.chat(messages=[{"role": "user", "content": "hi"}], reasoning_effort=None)

    assert "thinkingConfig" not in captured["json"]["generationConfig"]


def test_gemini_existing_incompatible_budget_removal_still_holds_through_chat():
    """Part 2A's thinkingBudget-popping behavior must survive the Part 3
    plumbing unchanged -- tested directly against apply_reasoning() (as
    Part 2A's own suite does) since manufacturing a pre-existing
    thinkingBudget key requires payload control chat() itself doesn't
    expose."""
    backend = GeminiBackend()
    payload = {"generationConfig": {"thinkingConfig": {"thinkingBudget": 4096}}}
    backend.apply_reasoning(payload, "high", model="gemini-2.5-pro")
    assert "thinkingBudget" not in payload["generationConfig"]["thinkingConfig"]
    assert payload["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "high"


# ══════════════════════════════════════════════════════════════════════
# 4. GroqBackend -- GPT-OSS ladder + Qwen-on-Groq literal "default" vs None
# ══════════════════════════════════════════════════════════════════════

def test_groq_gpt_oss_valid_effort_reaches_the_wire(monkeypatch):
    captured = _capture_post(monkeypatch)
    backend = GroqBackend()
    backend._model = "openai/gpt-oss-120b"

    backend.chat(messages=[{"role": "user", "content": "hi"}], reasoning_effort="high")

    assert captured["json"]["reasoning_effort"] == "high"


def test_groq_qwen_literal_default_string_reaches_the_wire_as_itself(monkeypatch):
    """"default" is a REAL provider-advertised literal for Qwen-on-Groq --
    distinct from Lumina's own None Provider-Default sentinel. Re-proving
    this survives the full widened chat() plumbing, not just
    apply_reasoning() called directly (which Part 2A's suite already
    covers)."""
    captured = _capture_post(monkeypatch)
    backend = GroqBackend()
    backend._model = "qwen/qwen3.6-27b"

    backend.chat(messages=[{"role": "user", "content": "hi"}], reasoning_effort="default")

    assert captured["json"]["reasoning_effort"] == "default"


def test_groq_qwen_none_provider_default_is_distinct_from_the_literal_string(monkeypatch):
    """The other half of the same proof: Lumina's None sentinel must NOT
    emit the literal "default" string (or any reasoning_effort key at
    all) -- the two must never collapse into each other."""
    captured = _capture_post(monkeypatch)
    backend = GroqBackend()
    backend._model = "qwen/qwen3.6-27b"

    backend.chat(messages=[{"role": "user", "content": "hi"}], reasoning_effort=None)

    assert "reasoning_effort" not in captured["json"]


# ══════════════════════════════════════════════════════════════════════
# 5. OpenRouterBackend -- primed vs unprimed capability cache
# ══════════════════════════════════════════════════════════════════════

def test_openrouter_primed_cache_valid_effort_reaches_reasoning_effort_key(monkeypatch):
    _capture_get(monkeypatch, [{"id": "openai/gpt-5.6", "reasoning": {"supported_efforts": ["low", "high"]}}])
    backend = OpenRouterBackend()
    backend.list_models()  # explicit prior discovery call primes the cache
    backend._model = "openai/gpt-5.6"

    captured = _capture_post(monkeypatch)
    backend.chat(messages=[{"role": "user", "content": "hi"}], reasoning_effort="high")

    assert captured["json"]["reasoning"]["effort"] == "high"


def test_openrouter_unprimed_cache_same_requested_string_emits_nothing(monkeypatch):
    """No list_models() call was ever made on this instance -- the cache
    is empty, reasoning_capabilities() correctly returns NO_REASONING_CONTROL,
    and the request must safely emit no override rather than guessing."""
    captured = _capture_post(monkeypatch)
    backend = OpenRouterBackend()
    backend._model = "openai/gpt-5.6"

    backend.chat(messages=[{"role": "user", "content": "hi"}], reasoning_effort="high")

    assert "reasoning" not in captured["json"]


def test_openrouter_chat_never_triggers_implicit_capability_discovery(monkeypatch):
    """No chat()/chat_stream() call may ever perform its own GET /models
    discovery -- only an explicit prior list_models() call populates the
    cache. Prove it by breaking requests.get and confirming chat() still
    completes successfully (via requests.post only)."""
    def _explode(*a, **kw):
        raise AssertionError("chat() must never call requests.get")
    monkeypatch.setattr(requests, "get", _explode)
    _capture_post(monkeypatch)
    backend = OpenRouterBackend()
    backend._model = "openai/gpt-5.6"

    backend.chat(messages=[{"role": "user", "content": "hi"}], reasoning_effort="high")
    # No AssertionError raised above -- confirms zero implicit discovery.


# ══════════════════════════════════════════════════════════════════════
# 6. QwenBackend -- enable_thinking through the full widened chat() path
# ══════════════════════════════════════════════════════════════════════

def test_qwen_chat_enabled_reaches_enable_thinking_true(monkeypatch):
    captured = _capture_post(monkeypatch)
    backend = QwenBackend()
    backend._model = "qwen3.5-plus"

    backend.chat(messages=[{"role": "user", "content": "hi"}], reasoning_effort="enabled")

    assert captured["json"]["enable_thinking"] is True


def test_qwen_chat_disabled_reaches_enable_thinking_false(monkeypatch):
    captured = _capture_post(monkeypatch)
    backend = QwenBackend()
    backend._model = "qwen3.5-plus"

    backend.chat(messages=[{"role": "user", "content": "hi"}], reasoning_effort="disabled")

    assert captured["json"]["enable_thinking"] is False


def test_qwen_chat_stream_enabled_reaches_enable_thinking_true(monkeypatch):
    captured = _capture_post(monkeypatch)
    backend = QwenBackend()
    backend._model = "qwen3.5-plus"

    _drain(backend.chat_stream(messages=[{"role": "user", "content": "hi"}], reasoning_effort="enabled"))

    assert captured["json"]["enable_thinking"] is True


def test_qwen_chat_none_emits_neither_field(monkeypatch):
    captured = _capture_post(monkeypatch)
    backend = QwenBackend()
    backend._model = "qwen3.5-plus"

    backend.chat(messages=[{"role": "user", "content": "hi"}], reasoning_effort=None)

    assert "enable_thinking" not in captured["json"]
    assert "thinking_budget" not in captured["json"]


def test_qwen_chat_fake_unsupported_high_emits_nothing(monkeypatch):
    """Qwen never advertised low/medium/high -- must fail safe through the
    full chat() path exactly as apply_reasoning() alone already proves."""
    captured = _capture_post(monkeypatch)
    backend = QwenBackend()
    backend._model = "qwen3.5-plus"

    backend.chat(messages=[{"role": "user", "content": "hi"}], reasoning_effort="high")

    assert "enable_thinking" not in captured["json"]


# ══════════════════════════════════════════════════════════════════════
# 7. OmniRouteBackend / OllamaBackend -- no-op, but must not error
# ══════════════════════════════════════════════════════════════════════

def test_omniroute_chat_accepts_reasoning_effort_without_error_and_stays_unchanged(monkeypatch):
    captured = _capture_post(monkeypatch)
    backend = OmniRouteBackend()
    backend._model = "some-omniroute-model"

    backend.chat(messages=[{"role": "user", "content": "hi"}], reasoning_effort="high")

    payload = captured["json"]
    assert payload["model"] == "some-omniroute-model"
    assert "reasoning_effort" not in payload
    assert "reasoning" not in payload
    assert "output_config" not in payload
    assert "thinkingConfig" not in payload
    assert "enable_thinking" not in payload


def test_omniroute_chat_stream_accepts_reasoning_effort_without_error(monkeypatch):
    captured = _capture_post(monkeypatch)
    backend = OmniRouteBackend()
    backend._model = "some-omniroute-model"

    _drain(backend.chat_stream(messages=[{"role": "user", "content": "hi"}], reasoning_effort="high"))

    assert "reasoning_effort" not in captured["json"]


def test_ollama_chat_accepts_reasoning_effort_without_error_payload_unchanged(monkeypatch):
    captured = _capture_post(monkeypatch)
    backend = OllamaBackend()
    backend._model = "llama3"

    backend.chat(messages=[{"role": "user", "content": "hi"}], reasoning_effort="high")

    payload = captured["json"]
    assert payload["model"] == "llama3"
    assert "reasoning_effort" not in payload
    assert "reasoning" not in payload


def test_ollama_chat_stream_accepts_reasoning_effort_without_error(monkeypatch):
    captured = _capture_post(monkeypatch)
    backend = OllamaBackend()
    backend._model = "llama3"

    _drain(backend.chat_stream(messages=[{"role": "user", "content": "hi"}], reasoning_effort="high"))

    assert "reasoning_effort" not in captured["json"]


# ══════════════════════════════════════════════════════════════════════
# 8. disable_thinking always wins over a conflicting reasoning_effort
# ══════════════════════════════════════════════════════════════════════

def test_disable_thinking_wins_over_conflicting_valid_effort_openai(monkeypatch):
    """OpenAIBackend/gpt-5.6 genuinely supports "high" -- calling chat()
    with BOTH disable_thinking=True AND reasoning_effort="high" in the
    same call must still emit NO reasoning override at all."""
    captured = _capture_post(monkeypatch)
    backend = OpenAIBackend()
    backend._model = "gpt-5.6"

    backend.chat(messages=[{"role": "assistant", "content": "SUMMARY:"}],
                 disable_thinking=True, reasoning_effort="high")

    assert "reasoning" not in captured["json"]


def test_disable_thinking_wins_over_conflicting_valid_effort_gemini(monkeypatch):
    captured = _capture_post(monkeypatch)
    backend = GeminiBackend()
    backend.default_model = "gemini-2.5-pro"

    backend.chat(messages=[{"role": "user", "content": "hi"}],
                 disable_thinking=True, reasoning_effort="high")

    assert "thinkingConfig" not in captured["json"]["generationConfig"]


def test_disable_thinking_wins_over_conflicting_valid_effort_anthropic(monkeypatch):
    captured = _capture_post(monkeypatch)
    backend = AnthropicBackend()
    backend.default_model = "claude-sonnet-5"

    backend.chat(messages=[{"role": "user", "content": "hi"}],
                 disable_thinking=True, reasoning_effort="xhigh")

    assert "output_config" not in captured["json"]


def test_disable_thinking_false_still_lets_a_valid_effort_through(monkeypatch):
    """Contrast case: disable_thinking=False (the default) must not
    accidentally suppress a legitimate reasoning_effort -- proves the
    precedence guard only fires on True, not unconditionally."""
    captured = _capture_post(monkeypatch)
    backend = OpenAIBackend()
    backend._model = "gpt-5.6"

    backend.chat(messages=[{"role": "user", "content": "hi"}],
                 disable_thinking=False, reasoning_effort="high")

    assert captured["json"]["reasoning"] == {"effort": "high", "summary": "auto"}


def test_effective_reasoning_effort_helper_direct():
    """Direct unit check of the shared precedence helper itself, isolated
    from any HTTP/payload machinery."""
    backend = OpenAIBackend()
    assert backend._effective_reasoning_effort("high", disable_thinking=True) is None
    assert backend._effective_reasoning_effort("high", disable_thinking=False) == "high"
    assert backend._effective_reasoning_effort(None, disable_thinking=True) is None
    assert backend._effective_reasoning_effort(None, disable_thinking=False) is None


# ══════════════════════════════════════════════════════════════════════
# 9. No hidden state -- no cross-call contamination on the same instance
# ══════════════════════════════════════════════════════════════════════

def test_openai_backend_two_calls_different_efforts_do_not_cross_contaminate(monkeypatch):
    """Call A with "low", call B with "high" on the SAME backend instance
    -- B's payload must show "high", not a blend/leftover of A's value,
    and a subsequent call with None must show Provider Default (no field)
    rather than silently reusing either prior explicit value."""
    backend = OpenAIBackend()
    backend._model = "gpt-5.6"

    captured = _capture_post(monkeypatch)
    backend.chat(messages=[{"role": "user", "content": "hi"}], reasoning_effort="low")
    assert captured["json"]["reasoning"] == {"effort": "low", "summary": "auto"}

    captured = _capture_post(monkeypatch)
    backend.chat(messages=[{"role": "user", "content": "hi"}], reasoning_effort="high")
    assert captured["json"]["reasoning"] == {"effort": "high", "summary": "auto"}

    captured = _capture_post(monkeypatch)
    backend.chat(messages=[{"role": "user", "content": "hi"}], reasoning_effort=None)
    assert "reasoning" not in captured["json"]


def test_apply_reasoning_direct_two_fresh_payloads_no_cross_contamination():
    """Same invariant proven directly against apply_reasoning() with two
    completely fresh payload dicts, per the task spec's suggested
    approach -- no shared mutable state on the backend could leak between
    them even in principle."""
    backend = OpenAIBackend()

    payload_a = {"model": "gpt-5.6"}
    backend.apply_reasoning(payload_a, "low", model="gpt-5.6")
    payload_b = {"model": "gpt-5.6"}
    backend.apply_reasoning(payload_b, "high", model="gpt-5.6")

    assert payload_a["reasoning"] == {"effort": "low", "summary": "auto"}
    assert payload_b["reasoning"] == {"effort": "high", "summary": "auto"}

    payload_c = {"model": "gpt-5.6"}
    backend.apply_reasoning(payload_c, None, model="gpt-5.6")
    assert "reasoning" not in payload_c
