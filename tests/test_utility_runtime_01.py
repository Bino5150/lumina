"""UTILITY-RUNTIME-01 — provider-neutral utility call contract.

complete_utility() expresses provider-neutral intent: a short background
completion, no conversational tool loop, thinking suppressed where the
transport actually supports expressing that, bounded output. The wire
translation of that intent belongs at the provider boundary — the same
architecture _apply_output_token_limit() already established for the
output-budget field.

Pre-repair defect (BACKGROUND-RUNTIME-01 recon, confirmed against source):
LMStudioBackend.chat() emitted LM Studio's local-only wire fields
(``thinking`` and ``chat_template_kwargs``) whenever disable_thinking=True,
and every cloud OpenAI-compatible subclass (OpenAI, OpenRouter, DeepSeek,
Groq, Kimi, Qwen, OmniRoute, Custom) inherited that emission unmodified.
Every background utility job — chat auto-naming, dream-sweep summarization,
My Human curation, context compaction — running against those providers
shipped fields the provider rejects (HTTP 400), and complete_utility()'s
never-raises contract converted the failure into a silent None. Owner-
invisible: the job just never produced output.

The repair is the shared provider contract, not per-caller workarounds.
These tests pin:

  1. Cloud OpenAI-compatible utility requests carry NO local-only fields
     (OpenAI asserted as the exact full wire payload — any unexpected field
     of any kind fails).
  2. The local-server family (LM Studio, llama.cpp, vLLM) KEEPS the fields
     — LM Studio's server 400s on a prefilled assistant turn while thinking
     is enabled (the S41 correction), so deleting them globally would break
     the local utility contract.
  3. Anthropic / Gemini / Ollama utility requests remain structurally valid
     with no local fields (their own chat() implementations never emitted
     them; the repair must not change that).
  4. Ordinary conversational behavior is preserved: reasoning-effort
     translation on normal chat() calls is untouched by the utility repair.
  5. The real consumers compose correctly through the production call path:
     auto-name (live agent.llm), dream summarization + My Human curation
     (fresh get_llm_backend()), and compaction (run_summarization_call with
     the compaction prompt) — driven against real backend objects with the
     transport intercepted immediately before requests.post, no synthetic
     helper bypassing the production translation.
"""
import os
import types

import pytest
import requests

from core.backends.deepseek import DeepSeekBackend
from core.backends.gemini_backend import GeminiBackend
from core.backends.groq import GroqBackend
from core.backends.kimi import KimiBackend
from core.backends.llamacpp import LlamaCppBackend
from core.backends.loader import CustomBackend
from core.backends.lmstudio import LMStudioBackend
from core.backends.omniroute import OmniRouteBackend
from core.backends.openai_backend import OpenAIBackend
from core.backends.openrouter import OpenRouterBackend
from core.backends.ollama import OllamaBackend
from core.backends.qwen import QwenBackend
from core.backends.anthropic_backend import AnthropicBackend

LOCAL_ONLY_UTILITY_FIELDS = ("thinking", "chat_template_kwargs")


class _FakeResponse:
    def __init__(self, body):
        self._body = body
        self.status_code = 200
        self.text = ""

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


class _FakeDiscoveryResponse:
    def __init__(self, body):
        self._body = body
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


def _mock_empty_discovery(monkeypatch):
    """UTILITY-RUNTIME-01 (mandatory-reasoning slice): _complete_utility_
    request() now primes reasoning-capability discovery (self.
    reasoning_capabilities_ready()/refresh_reasoning_capabilities()) before
    its one chat() call, so _effective_reasoning_effort() has real
    mandatory-reasoning data to read for backends (OpenRouter only) whose
    capabilities are live-discovered rather than static. That priming step
    performs a real GET to /models the first time any instance's
    capabilities aren't ready yet -- mocked here to a valid, empty model
    list so it resolves deterministically with no reasoning metadata for
    any model (matching this file's existing 'utility calls request no
    reasoning override' assertions) instead of reaching real network.
    Harmless to call for every other backend in this file: their
    reasoning_capabilities_ready() is always True (static capability
    tables), so they never reach requests.get at all regardless."""
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _FakeDiscoveryResponse({"data": []}))


def _capture_posts(monkeypatch, body=None):
    """Intercept requests.post immediately before transport; record every
    wire payload. This is the real production translation path — nothing
    between the caller and the provider boundary is bypassed."""
    payloads = []

    def fake_post(*args, **kwargs):
        payloads.append(kwargs["json"])
        return _FakeResponse(body if body is not None else {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        })

    monkeypatch.setattr(requests, "post", fake_post)
    return payloads


def _utility_body(content="TITLE: Neon Skyline"):
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def _openai_utility_body(content="TITLE: Neon Skyline"):
    """OPENAI-RESPONSES-01: OpenAIBackend now speaks /v1/responses, whose
    response shape is fundamentally different from every other cloud
    OpenAI-compatible sibling in this file (still Chat Completions) --
    this is the ONLY body shape OpenAIBackend's normalization understands."""
    return {
        "status": "completed",
        "output": [{"type": "message", "status": "completed",
                    "content": [{"type": "output_text", "text": content}]}],
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
                  "input_tokens_details": {"cached_tokens": 0},
                  "output_tokens_details": {"reasoning_tokens": 0}},
    }


# ══════════════════════════════════════════════════════════════════════════
# 1. Cloud OpenAI-compatible family — utility requests must be provider-valid
# ══════════════════════════════════════════════════════════════════════════

def _cloud_backends():
    openai = OpenAIBackend(api_key="test-key")
    openai._model = "gpt-5.6-luna"
    openrouter = OpenRouterBackend(api_key="test-key")
    openrouter._model = "meta-llama/llama-3.1-8b-instruct:free"
    deepseek = DeepSeekBackend(api_key="test-key")
    deepseek._model = "deepseek-v4-flash"
    groq = GroqBackend(api_key="test-key")
    groq._model = "llama-3.3-70b-versatile"
    kimi = KimiBackend(api_key="test-key")
    kimi._model = "kimi-latest"
    qwen = QwenBackend(api_key="test-key")
    qwen._model = "qwen3.5-plus"
    omniroute = OmniRouteBackend(api_key="test-key")
    omniroute._model = "gateway-model"
    custom = CustomBackend(base_url="http://custom.invalid/v1", api_key="test-key")
    custom._model = "custom-model"
    return [
        ("openai", openai),
        ("openrouter", openrouter),
        ("deepseek", deepseek),
        ("groq", groq),
        ("kimi", kimi),
        ("qwen", qwen),
        ("omniroute", omniroute),
        ("custom", custom),
    ]


@pytest.mark.parametrize("name,backend", _cloud_backends(), ids=lambda v: v if isinstance(v, str) else "")
def test_cloud_utility_requests_carry_no_local_thinking_fields(monkeypatch, name, backend):
    """Every cloud OpenAI-compatible transport must receive a payload free
    of LM Studio's local-only thinking-disable fields. Pre-repair, ALL of
    these leaked both fields via inherited LMStudioBackend.chat()."""
    _mock_empty_discovery(monkeypatch)
    body = _openai_utility_body("ok") if name == "openai" else None
    payloads = _capture_posts(monkeypatch, body=body)

    result = backend.complete_utility("summarize this", prefill="SUMMARY:", max_tokens=100)

    assert result == "ok"
    for field in LOCAL_ONLY_UTILITY_FIELDS:
        assert field not in payloads[0], (
            f"{name} utility request leaked local-only wire field '{field}'"
        )


def test_openai_utility_wire_payload_is_exactly_provider_valid(monkeypatch):
    """Strongest form of the contract: the OpenAI utility wire payload is
    EXACTLY the minimal valid request — no thinking, no
    chat_template_kwargs, no temperature (UTILITY-OPENAI-PARAMETER-
    CAPABILITY-01: live-verified 2026-09-05 HTTP 400 for any non-default
    temperature on this reasoning-capable model), and no other unrequested
    field of any kind. If a future edit reintroduces any local-only field
    or reasoning-model-illegal temperature, this fails before a provider
    ever sees it."""
    payloads = _capture_posts(monkeypatch, body=_openai_utility_body())
    backend = OpenAIBackend(api_key="test-key")
    backend._model = "gpt-5.6-luna"

    result = backend.complete_utility("name this chat", prefill="TITLE:", max_tokens=30)

    assert result == "Neon Skyline"
    assert payloads[0] == {
        "model": "gpt-5.6-luna",
        "input": [
            {"role": "user", "content": "name this chat"},
            {"role": "assistant", "content": [{"type": "output_text", "text": "TITLE:"}]},
        ],
        "store": False,
        "max_output_tokens": 30,
    }


def test_openrouter_utility_wire_payload_is_provider_valid(monkeypatch):
    """OpenRouter sits on the same OpenAI-compatible transport family as
    OpenAI for this contract: no local-only fields, OpenAI-shaped output
    budget. (Its reasoning-capability translation is separately owned and
    never fires on utility calls — complete_utility passes no effort.)"""
    _mock_empty_discovery(monkeypatch)
    payloads = _capture_posts(monkeypatch, body=_utility_body())
    backend = OpenRouterBackend(api_key="test-key")
    backend._model = "meta-llama/llama-3.1-8b-instruct:free"

    result = backend.complete_utility("name this chat", prefill="TITLE:", max_tokens=30)

    assert result == "Neon Skyline"
    assert payloads[0] == {
        "model": "meta-llama/llama-3.1-8b-instruct:free",
        "messages": [
            {"role": "user", "content": "name this chat"},
            {"role": "assistant", "content": "TITLE:"},
        ],
        "temperature": 0.3,
        "stream": False,
        "max_tokens": 30,
    }


def test_utility_call_against_strict_provider_succeeds_post_contract(monkeypatch):
    """Symptom-level reproduction of the owner-invisible failure: a strict
    provider (OpenAI-like) rejects unknown arguments with HTTP 400. Pre-repair
    this path silently returned None — the job died with no owner-visible
    evidence. Post-repair the same request is provider-valid and succeeds."""
    captured = []

    def strict_provider_post(url, **kwargs):
        payload = kwargs["json"]
        leaked = [f for f in LOCAL_ONLY_UTILITY_FIELDS if f in payload]
        if leaked:
            captured.append(leaked)
            raise requests.exceptions.HTTPError(
                f"400 Client Error: Unrecognized request argument: {leaked[0]}"
            )
        return _FakeResponse(_openai_utility_body())

    monkeypatch.setattr(requests, "post", strict_provider_post)
    backend = OpenAIBackend(api_key="test-key")
    backend._model = "gpt-5.6-luna"

    result = backend.complete_utility("name this chat", prefill="TITLE:", max_tokens=30)

    assert captured == [], "utility request shipped fields a strict provider rejects"
    assert result == "Neon Skyline"


# ══════════════════════════════════════════════════════════════════════════
# 2. Local-server family — the thinking-disable fields must SURVIVE here
# ══════════════════════════════════════════════════════════════════════════

def test_lmstudio_utility_requests_keep_thinking_disable_fields(monkeypatch):
    """LM Studio's server explicitly 400s ('Assistant response prefill is
    incompatible with enable_thinking') when a prefilled assistant turn is
    sent while thinking is enabled — the S41 correction. The fields are
    load-bearing for the local utility contract; a global strip (M3) must
    fail here."""
    payloads = _capture_posts(monkeypatch)
    backend = LMStudioBackend(base_url="http://local.invalid/v1")
    backend._model = "local-model"

    result = backend.complete_utility("summarize this", prefill="SUMMARY:", max_tokens=100)

    assert result == "ok"
    assert payloads[0]["thinking"] == {"type": "disabled"}
    assert payloads[0]["chat_template_kwargs"] == {"enable_thinking": False}


def test_llamacpp_utility_requests_keep_thinking_disable_fields(monkeypatch):
    """llama.cpp server inherits the LM Studio-family utility contract:
    chat_template_kwargs is a genuine llama.cpp-server feature and the
    prefill-vs-thinking conflict applies to it the same way."""
    payloads = _capture_posts(monkeypatch)
    backend = LlamaCppBackend(base_url="http://local.invalid/v1")
    backend._model = "local-model"

    result = backend.complete_utility("summarize this", prefill="SUMMARY:", max_tokens=100)

    assert result == "ok"
    assert payloads[0]["thinking"] == {"type": "disabled"}
    assert payloads[0]["chat_template_kwargs"] == {"enable_thinking": False}


def test_local_ordinary_chat_without_utility_intent_emits_no_thinking_fields(monkeypatch):
    """Normal interactive turns never prefill and never set disable_thinking
    — the fields must stay tied to the utility intent, not baked into every
    local request (the exact regression the S41 correction avoided)."""
    payloads = _capture_posts(monkeypatch)
    backend = LMStudioBackend(base_url="http://local.invalid/v1")
    backend._model = "local-model"

    backend.chat(messages=[{"role": "user", "content": "hi"}], max_tokens=64)

    for field in LOCAL_ONLY_UTILITY_FIELDS:
        assert field not in payloads[0]


# ══════════════════════════════════════════════════════════════════════════
# 3. Independent-implementation backends — structurally valid, no local fields
# ══════════════════════════════════════════════════════════════════════════

def test_anthropic_utility_request_remains_structurally_valid(monkeypatch):
    # Anthropic-shaped response body — its extract_message() consumes
    # {"content": [{"type": "text", ...}]} blocks, not OpenAI choices.
    payloads = _capture_posts(monkeypatch, body={
        "content": [{"type": "text", "text": "ok"}],
    })
    backend = AnthropicBackend()
    backend.default_model = "claude-sonnet-5"

    result = backend.complete_utility("summarize this", prefill="SUMMARY:", max_tokens=100)

    assert result == "ok"
    for field in LOCAL_ONLY_UTILITY_FIELDS:
        assert field not in payloads[0]
    assert payloads[0]["max_tokens"] == 100
    assert [m["role"] for m in payloads[0]["messages"]] == ["user", "assistant"]


def test_gemini_utility_request_remains_structurally_valid(monkeypatch):
    # Gemini-shaped response body — its extract_message() consumes
    # {"candidates": [{"content": {"parts": [...]}}]}.
    payloads = _capture_posts(monkeypatch, body={
        "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
    })
    backend = GeminiBackend()
    backend.default_model = "gemini-2.5-pro"

    result = backend.complete_utility("summarize this", prefill="SUMMARY:", max_tokens=100)

    assert result == "ok"
    for field in LOCAL_ONLY_UTILITY_FIELDS:
        assert field not in payloads[0]
    assert payloads[0]["generationConfig"]["maxOutputTokens"] == 100
    roles = [c["role"] for c in payloads[0]["contents"]]
    assert roles == ["user", "model"]


def test_ollama_utility_request_carries_no_local_thinking_fields(monkeypatch):
    """OllamaBackend accepts disable_thinking as interface consistency but
    has never emitted local-only fields (its own chat() implementation) —
    its utility contract must stay exactly as it is."""
    payloads = _capture_posts(monkeypatch)
    backend = OllamaBackend(base_url="http://local.invalid/v1")
    backend._model = "ollama-model"

    result = backend.complete_utility("summarize this", prefill="SUMMARY:", max_tokens=100)

    assert result == "ok"
    for field in LOCAL_ONLY_UTILITY_FIELDS:
        assert field not in payloads[0]
    assert payloads[0]["max_tokens"] == 100


# ══════════════════════════════════════════════════════════════════════════
# 4. Ordinary conversational behavior is preserved (Section 4 of the slice)
# ══════════════════════════════════════════════════════════════════════════

def test_openai_ordinary_chat_reasoning_translation_untouched(monkeypatch):
    """The utility repair must not disturb the established conversational
    reasoning contract: a valid effort on a capable model still reaches the
    wire, and no utility-repair side effects appear on normal chat calls."""
    payloads = _capture_posts(monkeypatch)
    backend = OpenAIBackend(api_key="test-key")
    backend._model = "gpt-5.6"

    backend.chat(messages=[{"role": "user", "content": "hi"}],
                 max_tokens=222, reasoning_effort="high")

    assert payloads[0]["reasoning"] == {"effort": "high", "summary": "auto"}
    for field in LOCAL_ONLY_UTILITY_FIELDS:
        assert field not in payloads[0]


def test_openai_chat_with_disable_thinking_intent_emits_no_local_fields(monkeypatch):
    """disable_thinking=True through chat() directly (complete_utility is
    its only production caller today) must translate to NO wire fields on
    OpenAI — the intent has no supported OpenAI syntax, so it emits nothing
    rather than LM Studio's syntax."""
    payloads = _capture_posts(monkeypatch)
    backend = OpenAIBackend(api_key="test-key")
    backend._model = "gpt-5.6-luna"

    backend.chat(messages=[{"role": "user", "content": "hi"}],
                 max_tokens=64, disable_thinking=True)

    for field in LOCAL_ONLY_UTILITY_FIELDS:
        assert field not in payloads[0]


def test_utility_calls_still_ignore_saved_reasoning_effort(monkeypatch):
    """Utility isolation (established contract from Patch 3A.4 Part 4): a
    saved conversational effort for the active backend+model must not alter
    the utility wire payload — and the repaired translation must not change
    that either."""
    from core.reasoning_preferences import resolve_reasoning_effort

    payloads = _capture_posts(monkeypatch)
    backend = OpenAIBackend(api_key="test-key")
    backend._model = "gpt-5.6-luna"

    prefs = {"backend_reasoning": {"openai": {"gpt-5.6-luna": "max"}}}
    assert resolve_reasoning_effort(backend, prefs=prefs) == "max"  # sanity

    backend.complete_utility("summarize this", prefill="SUMMARY:", max_tokens=100)

    assert "reasoning_effort" not in payloads[0]
    assert "reasoning" not in payloads[0]
    for field in LOCAL_ONLY_UTILITY_FIELDS:
        assert field not in payloads[0]


# ══════════════════════════════════════════════════════════════════════════
# 5. Consumer integration — the real production call paths compose
# ══════════════════════════════════════════════════════════════════════════

def test_dream_summarization_openai_path_sends_provider_valid_request(monkeypatch):
    """Dream summarization drives the production path end-to-end:
    run_summarization_call -> fresh get_llm_backend() -> complete_utility ->
    chat() -> wire. Backend swapped for a real OpenAIBackend (the family
    that failed pre-repair); transport intercepted at requests.post."""
    import core.dreaming as dreaming

    payloads = _capture_posts(monkeypatch, body=_openai_utility_body(
        "SUMMARY: - Bino tested the OpenAI backend"))
    backend = OpenAIBackend(api_key="test-key")
    backend._model = "gpt-5.6-luna"
    monkeypatch.setattr(dreaming, "get_llm_backend", lambda: backend)

    summary = dreaming.run_summarization_call("user: tested the backend\nassistant: nice")

    assert summary == "- Bino tested the OpenAI backend"
    for field in LOCAL_ONLY_UTILITY_FIELDS:
        assert field not in payloads[0]
    assert "temperature" not in payloads[0]
    assert payloads[0]["max_output_tokens"] == 500


def test_dream_summarization_local_path_keeps_thinking_disable_fields(monkeypatch):
    """Control: the same consumer against the local family must still emit
    the fields — proving the repair is provider-aware, not a global strip."""
    import core.dreaming as dreaming

    payloads = _capture_posts(monkeypatch, body=_utility_body(
        "SUMMARY: - Bino tested locally"))
    backend = LMStudioBackend(base_url="http://local.invalid/v1")
    backend._model = "local-model"
    monkeypatch.setattr(dreaming, "get_llm_backend", lambda: backend)

    summary = dreaming.run_summarization_call("user: tested locally\nassistant: nice")

    assert summary == "- Bino tested locally"
    assert payloads[0]["thinking"] == {"type": "disabled"}
    assert payloads[0]["chat_template_kwargs"] == {"enable_thinking": False}


def test_human_profile_curation_openai_path_sends_provider_valid_request(monkeypatch):
    """My Human curation drives the production path: curate_human_profile ->
    fresh get_llm_backend() -> complete_utility. PREFS-STALE-WRITE-01 already
    repaired the persistence side; this proves the wire side."""
    import core.dreaming as dreaming

    payloads = _capture_posts(monkeypatch, body=_openai_utility_body(
        "NOTES: - rides motorcycles"))
    backend = OpenAIBackend(api_key="test-key")
    backend._model = "gpt-5.6-luna"
    monkeypatch.setattr(dreaming, "get_llm_backend", lambda: backend)

    result = dreaming.curate_human_profile(
        "user: I ride motorcycles on weekends", bio="Bino", existing_profile="- old note")

    assert result == "- rides motorcycles"
    for field in LOCAL_ONLY_UTILITY_FIELDS:
        assert field not in payloads[0]
    assert "temperature" not in payloads[0]
    assert payloads[0]["max_output_tokens"] == 400


def test_compaction_openai_path_sends_provider_valid_request(monkeypatch):
    """Compaction is a healthy consumer of the same mechanism
    (run_summarization_call with the compaction prompt) — cured by the same
    shared repair, no compaction-specific code touched."""
    import core.dreaming as dreaming

    payloads = _capture_posts(monkeypatch, body=_openai_utility_body(
        "SUMMARY: - decided to ship the fix"))
    backend = OpenAIBackend(api_key="test-key")
    backend._model = "gpt-5.6-luna"
    monkeypatch.setattr(dreaming, "get_llm_backend", lambda: backend)

    summary = dreaming.run_summarization_call(
        "user: ship it\nassistant: done",
        prompt=dreaming.COMPACTION_PROMPT, max_tokens=300)

    assert summary == "- decided to ship the fix"
    for field in LOCAL_ONLY_UTILITY_FIELDS:
        assert field not in payloads[0]
    assert "temperature" not in payloads[0]
    assert payloads[0]["max_output_tokens"] == 300


def test_auto_name_chat_openai_path_sends_provider_valid_request_and_titles(monkeypatch):
    """Auto-name drives the production path with its distinctive wiring:
    the LIVE agent.llm instance (not a fresh get_llm_backend()) inside a
    fire-and-forget daemon thread. Proves the exact call site is cured —
    not merely that complete_utility() in isolation is."""
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import ui.main_window as main_window
    from ui.main_window import LuminaWindow

    class ImmediateThread:
        def __init__(self, target=None, daemon=None, **kw):
            self._target = target

        def start(self):
            self._target()

    payloads = []
    captured_bodies = []

    def fake_post(*args, **kwargs):
        payloads.append(kwargs["json"])
        return _FakeResponse(_openai_utility_body())

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(main_window, "threading",
                        types.SimpleNamespace(Thread=ImmediateThread))
    monkeypatch.setattr(main_window, "rename_chat",
                        lambda chat_id, title: captured_bodies.append((chat_id, title)))
    monkeypatch.setattr(main_window.QTimer, "singleShot", lambda *a, **k: None)

    backend = OpenAIBackend(api_key="test-key")
    backend._model = "gpt-5.6-luna"
    win = types.SimpleNamespace(
        agent=types.SimpleNamespace(llm=backend),
        _refresh_chat_list=lambda: None,
    )

    LuminaWindow._auto_name_chat(win, chat_id=42,
                                 user_msg="help me name this session",
                                 assistant_msg="sure thing")

    assert captured_bodies == [(42, "Neon Skyline")]
    for field in LOCAL_ONLY_UTILITY_FIELDS:
        assert field not in payloads[0]
    assert "temperature" not in payloads[0]
    assert payloads[0]["max_output_tokens"] == 30
