"""
Patch 3A.4 Part 4 -- persisted, per-backend/per-model reasoning-effort
preferences (core/reasoning_preferences.py) + the network-free
configured_model()/reasoning_capabilities_ready()/refresh_reasoning_
capabilities() seams on BaseLLMBackend + the three real runtime call sites
(main.py CLI loop, ui/main_window.py AgentWorker, core/headless.py
run_headless_turn()).

Sections (mirrors the task brief's numbered requirements):
  1. Persistence structure -- get/set_saved_reasoning() pure dict helpers
  2. Provider Default (null) vs Groq's literal "default" -- dedicated
     collision regression
  3. configured_model() -- base impl, Anthropic/Gemini override, falsy-
     safe, zero-HTTP (including LMStudio/Ollama's network-fallback trap)
  4. reasoning_capabilities_ready() / refresh_reasoning_capabilities() --
     static-provider base defaults
  5. Static-provider restoration (resolve_reasoning_effort end-to-end)
  6. Stale-value behavior (Sonnet 4.6 xhigh)
  7. Unsupported-provider behavior (OmniRoute/LM Studio/Custom/unknown)
  8. OpenRouter dynamic lifecycle (readiness/refresh/validate sequencing)
  9. Model switch independence
  10. Provider switch independence
  11. Runtime entry points -- main.py / AgentWorker / headless.py + fresh-
      runtime "no Settings panel" restart proof
  12. No agent mutable state
  13. Utility isolation -- complete_utility() unaffected
"""
import builtins
import types

import pytest
import requests

import core.persistence as persistence
from core.reasoning_preferences import (
    get_saved_reasoning,
    set_saved_reasoning,
    resolve_reasoning_effort,
)
from core.backends.reasoning import NO_REASONING_CONTROL
from core.backends.openai_backend import OpenAIBackend
from core.backends.anthropic_backend import AnthropicBackend
from core.backends.gemini_backend import GeminiBackend
from core.backends.groq import GroqBackend
from core.backends.qwen import QwenBackend
from core.backends.openrouter import OpenRouterBackend
from core.backends.omniroute import OmniRouteBackend
from core.backends.lmstudio import LMStudioBackend
from core.backends.ollama import OllamaBackend
from core.backends.loader import CustomBackend, BACKENDS


# ════════════════════════════════════════════════════════════════════════
# 1. Persistence structure -- pure dict helpers
# ════════════════════════════════════════════════════════════════════════

def test_get_saved_reasoning_missing_top_level_key_is_provider_default():
    assert get_saved_reasoning({}, "openai", "gpt-5.6-sol") is None


def test_get_saved_reasoning_missing_backend_is_provider_default():
    prefs = {"backend_reasoning": {"anthropic": {"claude-sonnet-5": "xhigh"}}}
    assert get_saved_reasoning(prefs, "openai", "gpt-5.6-sol") is None


def test_get_saved_reasoning_missing_model_is_provider_default():
    prefs = {"backend_reasoning": {"openai": {"gpt-5.6-sol": "max"}}}
    assert get_saved_reasoning(prefs, "openai", "gpt-5.6-luna") is None


def test_get_saved_reasoning_explicit_null_is_provider_default_and_distinct_from_absent():
    prefs = {"backend_reasoning": {"openai": {"gpt-5.6-sol": None}}}
    assert get_saved_reasoning(prefs, "openai", "gpt-5.6-sol") is None
    # Distinct at the DATA level even though both resolve to the same
    # runtime None -- the key genuinely exists with value None, versus
    # simply being absent (proven by direct dict inspection).
    assert "gpt-5.6-sol" in prefs["backend_reasoning"]["openai"]
    assert prefs["backend_reasoning"]["openai"]["gpt-5.6-sol"] is None


def test_get_saved_reasoning_arbitrary_string_round_trips_unchanged():
    prefs = {}
    set_saved_reasoning(prefs, "openai", "gpt-5.6-sol", "max")
    assert get_saved_reasoning(prefs, "openai", "gpt-5.6-sol") == "max"


def test_set_saved_reasoning_creates_nested_dicts_as_needed():
    prefs = {}
    result = set_saved_reasoning(prefs, "groq", "openai/gpt-oss-120b", "high")
    assert prefs == {"backend_reasoning": {"groq": {"openai/gpt-oss-120b": "high"}}}
    assert result is prefs  # mutates in place and returns the same dict


def test_set_saved_reasoning_does_not_call_persistence_save(monkeypatch):
    called = {"save": False}
    monkeypatch.setattr(persistence, "save", lambda p: called.__setitem__("save", True) or True)
    prefs = {}
    set_saved_reasoning(prefs, "openai", "gpt-5.6-sol", "max")
    assert called["save"] is False


def test_model_independence_within_same_backend():
    prefs = {}
    set_saved_reasoning(prefs, "openai", "gpt-5.6-sol", "max")
    set_saved_reasoning(prefs, "openai", "gpt-5.6-luna", "high")
    assert get_saved_reasoning(prefs, "openai", "gpt-5.6-sol") == "max"
    assert get_saved_reasoning(prefs, "openai", "gpt-5.6-luna") == "high"


def test_backend_independence_for_same_model_string_coincidence():
    """Two different backends storing under the same model-id string never
    collide -- backend_name is the outer key."""
    prefs = {}
    set_saved_reasoning(prefs, "openai", "shared-name", "max")
    set_saved_reasoning(prefs, "groq", "shared-name", "high")
    assert get_saved_reasoning(prefs, "openai", "shared-name") == "max"
    assert get_saved_reasoning(prefs, "groq", "shared-name") == "high"


# ════════════════════════════════════════════════════════════════════════
# 2. Provider Default (null) vs Groq's literal "default" -- collision test
# ════════════════════════════════════════════════════════════════════════

def test_provider_default_null_vs_groq_literal_default_string_stay_distinct():
    """The exact collision this patch must never regress: JSON null (->
    Python None) means Lumina's own Provider Default and never reaches the
    wire; the STRING "default" is Groq's own real, distinct, advertised
    effort literal for its Qwen models and validates/resolves normally."""
    backend = GroqBackend()
    backend._model = "qwen/qwen3.6-27b"

    prefs_null = {"backend_reasoning": {"groq": {"qwen/qwen3.6-27b": None}}}
    assert resolve_reasoning_effort(backend, prefs=prefs_null) is None

    prefs_string = {"backend_reasoning": {"groq": {"qwen/qwen3.6-27b": "default"}}}
    assert resolve_reasoning_effort(backend, prefs=prefs_string) == "default"


# ════════════════════════════════════════════════════════════════════════
# 3. configured_model() -- base impl, overrides, falsy-safety, zero-HTTP
# ════════════════════════════════════════════════════════════════════════

def test_base_configured_model_reads_underscore_model_attribute():
    backend = OpenAIBackend()
    backend._model = "gpt-5.6-sol"
    assert backend.configured_model() == "gpt-5.6-sol"


def test_base_configured_model_none_when_underscore_model_falsy():
    backend = OpenAIBackend()
    backend._model = None
    assert backend.configured_model() is None
    backend._model = ""
    assert backend.configured_model() is None


def test_anthropic_configured_model_override_reads_default_model_not_underscore_model():
    """The critical discovery: AnthropicBackend has NO self._model at all --
    a naive shared base reading only self._model would silently return None
    here even with a real configured model. Constructs a REAL AnthropicBackend
    (not just the base class in isolation) to prove the override actually
    fires via normal method resolution."""
    backend = AnthropicBackend()
    assert not hasattr(backend, "_model")
    backend.default_model = "claude-sonnet-5"
    assert backend.configured_model() == "claude-sonnet-5"


def test_gemini_configured_model_override_reads_default_model_not_underscore_model():
    backend = GeminiBackend()
    assert not hasattr(backend, "_model")
    backend.default_model = "gemini-3.5-flash"
    assert backend.configured_model() == "gemini-3.5-flash"


def test_anthropic_configured_model_falsy_safe():
    backend = AnthropicBackend()
    backend.default_model = ""
    assert backend.configured_model() is None


def test_omniroute_empty_string_model_is_none_not_empty_string():
    """OmniRouteBackend defaults self._model to "" (falsy) when unconfigured
    -- configured_model() must return None, never the empty string itself."""
    backend = OmniRouteBackend()
    assert backend._model == ""
    assert backend.configured_model() is None


def test_lmstudio_configured_model_does_not_trigger_network_fallback(monkeypatch):
    """get_model() falls through to a live HTTP call when self._model is
    unset -- configured_model() must NEVER do that."""
    called = {"hit": False}
    monkeypatch.setattr(requests, "get", lambda *a, **kw: called.__setitem__("hit", True))
    backend = LMStudioBackend()
    backend._model = None  # unconfigured
    assert backend.configured_model() is None
    assert called["hit"] is False


def test_ollama_configured_model_does_not_trigger_network_fallback(monkeypatch):
    called = {"hit": False}
    monkeypatch.setattr(requests, "get", lambda *a, **kw: called.__setitem__("hit", True))
    backend = OllamaBackend()
    backend._model = None
    assert backend.configured_model() is None
    assert called["hit"] is False


def test_every_backend_constructor_performs_zero_http(monkeypatch):
    calls = []
    monkeypatch.setattr(requests, "get", lambda *a, **kw: calls.append(("get", a, kw)))
    monkeypatch.setattr(requests, "post", lambda *a, **kw: calls.append(("post", a, kw)))
    for name, cls in BACKENDS.items():
        cls()
    assert calls == []


def test_every_backend_configured_model_call_performs_zero_http(monkeypatch):
    calls = []
    monkeypatch.setattr(requests, "get", lambda *a, **kw: calls.append(("get", a, kw)))
    monkeypatch.setattr(requests, "post", lambda *a, **kw: calls.append(("post", a, kw)))
    for name, cls in BACKENDS.items():
        backend = cls()
        backend.configured_model()
    assert calls == []


# ════════════════════════════════════════════════════════════════════════
# 4. reasoning_capabilities_ready() / refresh_reasoning_capabilities() --
#    static-provider base defaults
# ════════════════════════════════════════════════════════════════════════

def test_static_backend_is_always_ready():
    backend = OpenAIBackend()
    assert backend.reasoning_capabilities_ready() is True
    assert backend.reasoning_capabilities_ready("gpt-5.6-sol") is True


def test_static_backend_refresh_is_a_network_free_noop_returning_true(monkeypatch):
    called = {"hit": False}
    monkeypatch.setattr(requests, "get", lambda *a, **kw: called.__setitem__("hit", True))
    backend = OpenAIBackend()
    assert backend.refresh_reasoning_capabilities() is True
    assert called["hit"] is False


# ════════════════════════════════════════════════════════════════════════
# 5. Static-provider restoration -- end-to-end resolve_reasoning_effort()
# ════════════════════════════════════════════════════════════════════════

def test_restore_openai_sol_max():
    backend = OpenAIBackend()
    backend._model = "gpt-5.6-sol"
    prefs = {"backend_reasoning": {"openai": {"gpt-5.6-sol": "max"}}}
    assert resolve_reasoning_effort(backend, prefs=prefs) == "max"


def test_restore_openai_luna_high():
    backend = OpenAIBackend()
    backend._model = "gpt-5.6-luna"
    prefs = {"backend_reasoning": {"openai": {"gpt-5.6-luna": "high"}}}
    assert resolve_reasoning_effort(backend, prefs=prefs) == "high"


def test_restore_anthropic_sonnet_5_xhigh():
    backend = AnthropicBackend()
    backend.default_model = "claude-sonnet-5"
    prefs = {"backend_reasoning": {"anthropic": {"claude-sonnet-5": "xhigh"}}}
    assert resolve_reasoning_effort(backend, prefs=prefs) == "xhigh"


def test_restore_gemini_valid_model_specific_level():
    backend = GeminiBackend()
    backend.default_model = "gemini-3.5-flash"
    prefs = {"backend_reasoning": {"gemini": {"gemini-3.5-flash": "high"}}}
    assert resolve_reasoning_effort(backend, prefs=prefs) == "high"


def test_restore_groq_gpt_oss_high():
    backend = GroqBackend()
    backend._model = "openai/gpt-oss-120b"
    prefs = {"backend_reasoning": {"groq": {"openai/gpt-oss-120b": "high"}}}
    assert resolve_reasoning_effort(backend, prefs=prefs) == "high"


def test_restore_groq_qwen_literal_default():
    backend = GroqBackend()
    backend._model = "qwen/qwen3.6-27b"
    prefs = {"backend_reasoning": {"groq": {"qwen/qwen3.6-27b": "default"}}}
    assert resolve_reasoning_effort(backend, prefs=prefs) == "default"


def test_restore_qwen_enabled():
    backend = QwenBackend()
    backend._model = "qwen3.5-plus"
    prefs = {"backend_reasoning": {"qwen": {"qwen3.5-plus": "enabled"}}}
    assert resolve_reasoning_effort(backend, prefs=prefs) == "enabled"


def test_restore_qwen_disabled():
    backend = QwenBackend()
    backend._model = "qwen3.5-plus"
    prefs = {"backend_reasoning": {"qwen": {"qwen3.5-plus": "disabled"}}}
    assert resolve_reasoning_effort(backend, prefs=prefs) == "disabled"


def test_restore_unknown_value_resolves_to_none():
    backend = OpenAIBackend()
    backend._model = "gpt-5.6-sol"
    prefs = {"backend_reasoning": {"openai": {"gpt-5.6-sol": "ultra-super-max"}}}
    assert resolve_reasoning_effort(backend, prefs=prefs) is None


def test_provider_default_never_substitutes_caps_default_effort():
    """None saved must resolve to None, never to the backend's own
    documented default_effort metadata."""
    backend = OpenAIBackend()
    backend._model = "gpt-5.6-sol"
    caps = backend.reasoning_capabilities("gpt-5.6-sol")
    assert caps.default_effort == "medium"  # sanity: a real default exists
    prefs = {"backend_reasoning": {"openai": {"gpt-5.6-sol": None}}}
    assert resolve_reasoning_effort(backend, prefs=prefs) is None


# ════════════════════════════════════════════════════════════════════════
# 6. Stale-value behavior
# ════════════════════════════════════════════════════════════════════════

def test_stale_sonnet_4_6_xhigh_resolves_none_but_prefs_untouched():
    backend = AnthropicBackend()
    backend.default_model = "claude-sonnet-4-6"
    caps = backend.reasoning_capabilities("claude-sonnet-4-6")
    assert "xhigh" not in caps.efforts  # sanity: confirms this really is stale

    prefs = {"backend_reasoning": {"anthropic": {"claude-sonnet-4-6": "xhigh"}}}
    result = resolve_reasoning_effort(backend, prefs=prefs)

    assert result is None
    # Stored value must remain exactly as saved -- no silent rewrite/delete.
    assert get_saved_reasoning(prefs, "anthropic", "claude-sonnet-4-6") == "xhigh"
    assert prefs["backend_reasoning"]["anthropic"]["claude-sonnet-4-6"] == "xhigh"


# ════════════════════════════════════════════════════════════════════════
# 7. Unsupported-provider behavior
# ════════════════════════════════════════════════════════════════════════

def test_omniroute_fabricated_effort_resolves_none():
    backend = OmniRouteBackend()
    backend._model = "some-model"
    prefs = {"backend_reasoning": {"omniroute": {"some-model": "high"}}}
    assert resolve_reasoning_effort(backend, prefs=prefs) is None


def test_lmstudio_local_fabricated_effort_resolves_none():
    backend = LMStudioBackend()
    backend._model = "local-model"
    prefs = {"backend_reasoning": {"lmstudio": {"local-model": "high"}}}
    assert resolve_reasoning_effort(backend, prefs=prefs) is None


def test_custom_backend_fabricated_effort_resolves_none():
    backend = CustomBackend()
    backend._model = "custom-model"
    prefs = {"backend_reasoning": {"custom": {"custom-model": "high"}}}
    assert resolve_reasoning_effort(backend, prefs=prefs) is None


def test_unknown_backend_name_resolves_none():
    backend = OpenAIBackend()
    backend._model = "gpt-5.6-sol"
    # Saved under a backend name that doesn't match backend.name at all.
    prefs = {"backend_reasoning": {"totally-unknown-provider": {"gpt-5.6-sol": "max"}}}
    assert resolve_reasoning_effort(backend, prefs=prefs) is None


def test_unknown_model_string_resolves_none():
    backend = OpenAIBackend()
    backend._model = "some-unlisted-model-id"
    prefs = {"backend_reasoning": {"openai": {"some-unlisted-model-id": "max"}}}
    assert resolve_reasoning_effort(backend, prefs=prefs) is None


# ════════════════════════════════════════════════════════════════════════
# 8. OpenRouter dynamic lifecycle
# ════════════════════════════════════════════════════════════════════════

class _FakeModelsResp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return {"data": self._data}


def _mock_models(monkeypatch, data):
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _FakeModelsResp(data))


def _mock_models_raises(monkeypatch, exc):
    def _raise(*a, **kw):
        raise exc
    monkeypatch.setattr(requests, "get", _raise)


def test_openrouter_unready_before_any_discovery():
    backend = OpenRouterBackend()
    assert backend.reasoning_capabilities_ready() is False


def test_openrouter_provider_default_skips_refresh_entirely(monkeypatch):
    """saved is None -> resolve_reasoning_effort must return None WITHOUT
    ever calling refresh_reasoning_capabilities() / hitting the network --
    discovery is never performed merely to prove Provider Default is valid."""
    called = {"get": False}
    monkeypatch.setattr(requests, "get", lambda *a, **kw: called.__setitem__("get", True))
    backend = OpenRouterBackend()
    backend._model = "openai/gpt-5.6"
    prefs = {"backend_reasoning": {"openrouter": {"openai/gpt-5.6": None}}}

    result = resolve_reasoning_effort(backend, prefs=prefs)

    assert result is None
    assert called["get"] is False
    assert backend.reasoning_capabilities_ready() is False  # still undiscovered


def test_openrouter_unready_explicit_value_triggers_refresh_then_succeeds(monkeypatch):
    data = [{"id": "openai/gpt-5.6", "reasoning": {"supported_efforts": ["low", "high"]}}]
    _mock_models(monkeypatch, data)
    backend = OpenRouterBackend()
    backend._model = "openai/gpt-5.6"
    assert backend.reasoning_capabilities_ready() is False
    prefs = {"backend_reasoning": {"openrouter": {"openai/gpt-5.6": "high"}}}

    result = resolve_reasoning_effort(backend, prefs=prefs)

    assert result == "high"
    assert backend.reasoning_capabilities_ready() is True


def test_openrouter_unready_explicit_value_refresh_fails_resolves_none(monkeypatch):
    _mock_models_raises(monkeypatch, ConnectionError("network down"))
    backend = OpenRouterBackend()
    backend._model = "openai/gpt-5.6"
    prefs = {"backend_reasoning": {"openrouter": {"openai/gpt-5.6": "high"}}}

    result = resolve_reasoning_effort(backend, prefs=prefs)

    assert result is None
    # A failed refresh must not falsely flip readiness to True.
    assert backend.reasoning_capabilities_ready() is False
    # The stored preference itself is untouched by the failed attempt.
    assert get_saved_reasoning(prefs, "openrouter", "openai/gpt-5.6") == "high"


def test_openrouter_refresh_succeeds_but_value_unsupported_after_refresh_resolves_none(monkeypatch):
    """Discovery succeeds (model IS in the response) but the saved effort
    isn't one of ITS advertised efforts -- validation failure, not a
    refresh failure."""
    data = [{"id": "openai/gpt-5.6", "reasoning": {"supported_efforts": ["low", "high"]}}]
    _mock_models(monkeypatch, data)
    backend = OpenRouterBackend()
    backend._model = "openai/gpt-5.6"
    prefs = {"backend_reasoning": {"openrouter": {"openai/gpt-5.6": "ultra"}}}

    result = resolve_reasoning_effort(backend, prefs=prefs)

    assert result is None
    assert backend.reasoning_capabilities_ready() is True  # discovery itself succeeded
    assert get_saved_reasoning(prefs, "openrouter", "openai/gpt-5.6") == "ultra"


def test_openrouter_readiness_distinguishes_undiscovered_from_discovered_but_unsupported(monkeypatch):
    """Two different reasons a model can look 'unsupported' must be
    distinguishable via the readiness seam, even though reasoning_
    capabilities() itself collapses both to NO_REASONING_CONTROL."""
    backend = OpenRouterBackend()
    # Case 1: never discovered at all.
    assert backend.reasoning_capabilities_ready() is False
    assert backend.reasoning_capabilities("some/model") is NO_REASONING_CONTROL

    # Case 2: discovery ran, but this particular model carried no
    # "reasoning" metadata -- now ready, but still NO_REASONING_CONTROL
    # for THIS model.
    _mock_models(monkeypatch, [{"id": "some/other-model"}])
    backend.list_models()
    assert backend.reasoning_capabilities_ready() is True
    assert backend.reasoning_capabilities("some/model") is NO_REASONING_CONTROL


def test_openrouter_no_redundant_rediscovery_once_ready(monkeypatch):
    """Once reasoning_capabilities_ready() is True, resolve_reasoning_effort()
    must validate directly and never call refresh_reasoning_capabilities()
    again -- proven by counting actual HTTP GETs."""
    get_calls = {"n": 0}

    def _fake_get(*a, **kw):
        get_calls["n"] += 1
        return _FakeModelsResp([{"id": "openai/gpt-5.6",
                                  "reasoning": {"supported_efforts": ["low", "high"]}}])
    monkeypatch.setattr(requests, "get", _fake_get)

    backend = OpenRouterBackend()
    backend._model = "openai/gpt-5.6"
    backend.list_models()  # prime discovery once, outside resolve_reasoning_effort
    assert get_calls["n"] == 1
    assert backend.reasoning_capabilities_ready() is True

    prefs = {"backend_reasoning": {"openrouter": {"openai/gpt-5.6": "high"}}}
    result = resolve_reasoning_effort(backend, prefs=prefs)

    assert result == "high"
    assert get_calls["n"] == 1  # no additional GET triggered by resolve


def test_refresh_reasoning_capabilities_reports_this_attempts_success(monkeypatch):
    data = [{"id": "m", "reasoning": {"supported_efforts": ["low"]}}]
    _mock_models(monkeypatch, data)
    backend = OpenRouterBackend()
    assert backend.refresh_reasoning_capabilities() is True


def test_refresh_reasoning_capabilities_reports_this_attempts_failure(monkeypatch):
    _mock_models_raises(monkeypatch, ConnectionError("boom"))
    backend = OpenRouterBackend()
    assert backend.refresh_reasoning_capabilities() is False


# ════════════════════════════════════════════════════════════════════════
# 9. Model switch independence
# ════════════════════════════════════════════════════════════════════════

def test_model_switch_independence_same_backend_instance():
    backend = OpenAIBackend()
    prefs = {
        "backend_reasoning": {
            "openai": {"gpt-5.6-sol": "max", "gpt-5.6-luna": "high"}
        }
    }

    backend._model = "gpt-5.6-sol"
    assert resolve_reasoning_effort(backend, prefs=prefs) == "max"

    backend._model = "gpt-5.6-luna"
    assert resolve_reasoning_effort(backend, prefs=prefs) == "high"

    backend._model = "gpt-5.6-sol"
    assert resolve_reasoning_effort(backend, prefs=prefs) == "max"


def test_model_switch_independence_via_explicit_model_param():
    backend = OpenAIBackend()
    prefs = {
        "backend_reasoning": {
            "openai": {"gpt-5.6-sol": "max", "gpt-5.6-luna": "high"}
        }
    }
    assert resolve_reasoning_effort(backend, prefs=prefs, model="gpt-5.6-sol") == "max"
    assert resolve_reasoning_effort(backend, prefs=prefs, model="gpt-5.6-luna") == "high"
    assert resolve_reasoning_effort(backend, prefs=prefs, model="gpt-5.6-sol") == "max"


# ════════════════════════════════════════════════════════════════════════
# 10. Provider switch independence
# ════════════════════════════════════════════════════════════════════════

def test_provider_switch_independence():
    prefs = {
        "backend_reasoning": {
            "anthropic": {"claude-sonnet-5": "xhigh"},
            "openai": {"gpt-5.6-sol": "max"},
        }
    }

    anthropic_backend = AnthropicBackend()
    anthropic_backend.default_model = "claude-sonnet-5"
    openai_backend = OpenAIBackend()
    openai_backend._model = "gpt-5.6-sol"

    assert resolve_reasoning_effort(anthropic_backend, prefs=prefs) == "xhigh"
    assert resolve_reasoning_effort(openai_backend, prefs=prefs) == "max"

    # Resolving one leaves the other's stored value fully untouched.
    assert prefs["backend_reasoning"]["anthropic"]["claude-sonnet-5"] == "xhigh"
    assert prefs["backend_reasoning"]["openai"]["gpt-5.6-sol"] == "max"


# ════════════════════════════════════════════════════════════════════════
# 11. Runtime entry points
# ════════════════════════════════════════════════════════════════════════

@pytest.fixture
def isolated_prefs(tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    return persistence


def test_fresh_runtime_restores_without_settings_panel_ever_opened(isolated_prefs):
    """Patch 3A.4 Part 4 section 13 -- a FRESH, never-touched backend
    instance, loading prefs cold from disk (no explicit dict passed),
    must restore the saved effort with zero network and no prior warm-up
    call on the same object."""
    prefs = persistence.load()
    set_saved_reasoning(prefs, "anthropic", "claude-sonnet-5", "xhigh")
    assert persistence.save(prefs) is True

    # A brand new process would construct a brand new backend instance --
    # simulated here by a backend object that has had NO prior interaction
    # of any kind (no resolve_reasoning_effort call, no list_models(), no
    # warm-up of any sort) before this single cold call.
    fresh_backend = AnthropicBackend()
    fresh_backend.default_model = "claude-sonnet-5"

    result = resolve_reasoning_effort(fresh_backend)  # prefs=None -> loads from disk

    assert result == "xhigh"


def test_main_cli_loop_resolves_and_passes_reasoning_effort(isolated_prefs, monkeypatch):
    """Exercises main.py's real run_cli() while-loop body end to end (not
    just resolve_reasoning_effort() in isolation): monkeypatches
    core.agent.LuminaAgent (which run_cli() imports locally) with a fake
    that owns a REAL OpenAIBackend as .llm, drives one turn through a
    faked input(), and asserts the exact reasoning_effort value that
    reached agent.chat()."""
    prefs = persistence.load()
    set_saved_reasoning(prefs, "openai", "gpt-5.6-sol", "max")
    assert persistence.save(prefs) is True

    llm = OpenAIBackend()
    llm._model = "gpt-5.6-sol"

    class _FakeCliAgent:
        def __init__(self, on_tool_call=None, on_tool_result=None, channel_id=None):
            self.llm = llm
            self.chat_calls = []

        def test_connection(self):
            return "fake connection ok"

        def chat(self, user_input, reasoning_effort=None):
            self.chat_calls.append((user_input, reasoning_effort))
            return "a response"

        def get_token_count(self):
            return 0

    import core.agent as core_agent_module
    monkeypatch.setattr(core_agent_module, "LuminaAgent", _FakeCliAgent)

    inputs = iter(["hello there"])

    def _fake_input(prompt=""):
        try:
            return next(inputs)
        except StopIteration:
            raise EOFError()

    monkeypatch.setattr(builtins, "input", _fake_input)

    import main
    captured_agent = {}
    real_apply = main.apply_cli_persona_and_tools

    def _capture_apply(agent, *a, **kw):
        captured_agent["agent"] = agent
        return real_apply(agent, *a, **kw)
    monkeypatch.setattr(main, "apply_cli_persona_and_tools", _capture_apply)

    main.run_cli()

    agent = captured_agent["agent"]
    assert agent.chat_calls == [("hello there", "max")]


def test_headless_run_turn_resolves_and_passes_reasoning_effort(isolated_prefs, monkeypatch):
    import core.headless as headless

    prefs = persistence.load()
    set_saved_reasoning(prefs, "openai", "gpt-5.6-sol", "max")
    assert persistence.save(prefs) is True

    llm = OpenAIBackend()
    llm._model = "gpt-5.6-sol"

    ns = types.SimpleNamespace()
    ns.llm = llm
    ns.registry = types.SimpleNamespace(all_tool_names=lambda: [])
    ns.on_tool_call = lambda name, args: None
    ns.on_tool_result = lambda name, result: None
    calls = []

    def _chat(task, source="OWNER_DIRECT", reasoning_effort=None):
        calls.append((task, source, reasoning_effort))
        return "headless response"
    ns.chat = _chat

    monkeypatch.setattr(headless, "get_headless_agent", lambda *a, **k: ns)

    result = headless.run_headless_turn("do the thing", "chan-1", owner=True)

    assert result == {"success": True, "response": "headless response"}
    assert calls == [("do the thing", "OWNER_DIRECT", "max")]


def test_headless_legacy_fake_agent_without_llm_or_reasoning_param_still_works(isolated_prefs, monkeypatch):
    """Regression proof that tests/test_headless.py's own pre-3A.4 fakes
    (SimpleNamespace with chat(task, source=...), no .llm, no
    reasoning_effort param) are untouched by this wiring -- replicated
    here rather than editing that file."""
    import core.headless as headless

    ns = types.SimpleNamespace()
    ns.registry = types.SimpleNamespace(all_tool_names=lambda: [])
    ns.on_tool_call = lambda name, args: None
    ns.on_tool_result = lambda name, result: None
    calls = []

    def _chat(task, source="OWNER_DIRECT"):
        calls.append((task, source))
        return "legacy response"
    ns.chat = _chat

    monkeypatch.setattr(headless, "get_headless_agent", lambda *a, **k: ns)

    result = headless.run_headless_turn("hi", "chan-1", owner=True)

    assert result == {"success": True, "response": "legacy response"}
    assert calls == [("hi", "OWNER_DIRECT")]


def test_agent_worker_resolves_and_passes_reasoning_effort(isolated_prefs, monkeypatch):
    pytest.importorskip("PySide6")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from ui.main_window import AgentWorker, StreamSignals

    prefs = persistence.load()
    set_saved_reasoning(prefs, "openai", "gpt-5.6-sol", "max")
    assert persistence.save(prefs) is True

    llm = OpenAIBackend()
    llm._model = "gpt-5.6-sol"

    class _Agent:
        on_tool_call = None
        on_tool_result = None
        on_think_start = None
        on_think_token = None
        on_think_end = None
        on_response_token = None

        def __init__(self):
            self.llm = llm
            self.calls = []

        def chat(self, user_input, chat_id=None, cancel_event=None, reasoning_effort=None):
            self.calls.append((user_input, chat_id, reasoning_effort))
            return "done"

    agent = _Agent()
    signals = StreamSignals()
    finished = []
    signals.finished.connect(finished.append)
    worker = AgentWorker(agent, "question", signals, chat_id=9)

    worker.run()

    assert finished == ["done"]
    assert agent.calls == [("question", 9, "max")]


def test_agent_worker_legacy_stub_without_reasoning_param_still_works():
    """Exact replica of tests/test_operator_stop_ui.py's
    test_agent_worker_keeps_legacy_test_stub_compatibility_without_cancel_kwarg
    -- proves _agent_accepts_reasoning_effort() correctly omits the kwarg
    for a fake with neither reasoning_effort nor **kwargs, and (critically)
    that it never even touches a nonexistent .llm attribute on this fake."""
    pytest.importorskip("PySide6")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from ui.main_window import AgentWorker, StreamSignals

    calls = []

    class _Agent:
        on_tool_call = None
        on_tool_result = None
        on_think_start = None
        on_think_token = None
        on_think_end = None
        on_response_token = None

        def chat(self, user_input, chat_id=None):
            calls.append((user_input, chat_id))
            return "done"

    signals = StreamSignals()
    finished = []
    signals.finished.connect(finished.append)
    worker = AgentWorker(_Agent(), "question", signals, chat_id=9)

    worker.run()

    assert calls == [("question", 9)]
    assert finished == ["done"]


# ════════════════════════════════════════════════════════════════════════
# 12. No agent mutable state
# ════════════════════════════════════════════════════════════════════════

def test_no_leftover_state_between_turns_on_same_backend_instance():
    backend = OpenAIBackend()
    backend._model = "gpt-5.6-sol"

    prefs_a = {"backend_reasoning": {"openai": {"gpt-5.6-sol": "max"}}}
    turn_a = resolve_reasoning_effort(backend, prefs=prefs_a)
    assert turn_a == "max"

    # Turn B: same backend OBJECT, no reset call of any kind -- only the
    # prefs dict changed (simulating prefs being edited between turns).
    prefs_b = {"backend_reasoning": {"openai": {"gpt-5.6-sol": None}}}
    turn_b = resolve_reasoning_effort(backend, prefs=prefs_b)
    assert turn_b is None

    # And a third turn proves turn A's value wasn't cached anywhere either.
    turn_c = resolve_reasoning_effort(backend, prefs=prefs_a)
    assert turn_c == "max"


def test_resolve_reasoning_effort_never_sets_attributes_on_backend_or_agent():
    backend = OpenAIBackend()
    backend._model = "gpt-5.6-sol"
    before = dict(backend.__dict__)

    prefs = {"backend_reasoning": {"openai": {"gpt-5.6-sol": "max"}}}
    resolve_reasoning_effort(backend, prefs=prefs)

    assert backend.__dict__ == before
    assert not hasattr(backend, "reasoning_effort")


# ════════════════════════════════════════════════════════════════════════
# 13. Utility isolation -- complete_utility() unaffected
# ════════════════════════════════════════════════════════════════════════

class _FakeResp:
    def __init__(self, json_body):
        self._json_body = json_body
        self.status_code = 200
        self.text = "{}"

    def raise_for_status(self):
        pass

    def json(self):
        return self._json_body


def test_complete_utility_ignores_saved_reasoning_effort_entirely(monkeypatch):
    """Loading a saved conversational effort of 'high'/'max' for the active
    backend+model must NOT alter complete_utility()'s wire payload -- it
    must still call chat(..., disable_thinking=True) with no reasoning_effort
    reaching the request, unaffected by whatever's in prefs. Proves Part 4's
    persistence layer did not accidentally wire itself into this call site."""
    backend = OpenAIBackend()
    backend._model = "gpt-5.6-sol"

    # Real saved data exists for this exact backend+model -- if
    # complete_utility() ever started consulting it, this would flip the
    # wire payload's reasoning_effort key.
    prefs = {"backend_reasoning": {"openai": {"gpt-5.6-sol": "max"}}}
    assert resolve_reasoning_effort(backend, prefs=prefs) == "max"  # sanity

    captured = {}

    def _fake_post(*args, **kwargs):
        captured["json"] = kwargs.get("json")
        return _FakeResp({"choices": [{"message": {"role": "assistant", "content": "a summary"}}]})

    monkeypatch.setattr(requests, "post", _fake_post)

    result = backend.complete_utility("summarize this")

    assert result == "a summary"
    assert "reasoning_effort" not in captured["json"]
