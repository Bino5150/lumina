"""
Backend loader — returns the active LLM backend instance based on config.
Import this everywhere instead of importing a specific backend class.

Usage:
    from core.backends.loader import get_llm_backend
    llm = get_llm_backend()
Reads config.LLM_BACKEND to select which backend to instantiate.
"""

import config
from .lmstudio import LMStudioBackend
from .ollama import OllamaBackend
from .llamacpp import LlamaCppBackend
from .vllm import VLLMBackend
from .openrouter import OpenRouterBackend
from .deepseek import DeepSeekBackend
from .groq import GroqBackend
from .openai_backend import OpenAIBackend
from .anthropic_backend import AnthropicBackend
from .gemini_backend import GeminiBackend
from .kimi import KimiBackend
from .qwen import QwenBackend
from .omniroute import OmniRouteBackend

class CustomBackend(LMStudioBackend):
    """Generic OpenAI-compatible endpoint. URL and optional API key set by user."""
    name = "custom"
    display_name = "Custom (OpenAI-compatible)"
    default_url = ""

    def __init__(self, base_url: str = None, api_key: str = None):
        super().__init__(base_url=base_url)
        self._model = getattr(config, "CUSTOM_DEFAULT_MODEL", "")
        configured_key = getattr(config, "CUSTOM_API_KEY", "") if api_key is None else api_key
        key = configured_key.strip()
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}" if key else "Bearer lumina",
        }

    def _apply_disable_thinking(self, payload: dict) -> None:
        """No local thinking-disable wire fields on a custom endpoint.

        UTILITY-RUNTIME-01: a custom endpoint's wire contract is unknown --
        it may be a strict cloud-compatible server that rejects
        unrecognized arguments, or a local server that accepts them. The
        safe default for an unverified transport is to emit only standard
        OpenAI-compatible fields; complete_utility()'s assistant-prefill
        plus output-side think-strip remain the anti-bleed defense. (A
        per-endpoint capability declaration so a custom endpoint CAN opt
        into LM Studio-style fields is future work, not part of this
        contract repair.)
        """
        return None

BACKENDS = {
    "lmstudio":   LMStudioBackend,
    "ollama":     OllamaBackend,
    "llamacpp":   LlamaCppBackend,
    "vllm":       VLLMBackend,
    "openrouter": OpenRouterBackend,
    "deepseek":   DeepSeekBackend,
    "groq":       GroqBackend,
    "openai":     OpenAIBackend,
    "anthropic":  AnthropicBackend,
    "gemini":     GeminiBackend,
    "kimi":       KimiBackend,
    "qwen":       QwenBackend,
    "custom":     CustomBackend,
    "omniroute":  OmniRouteBackend,
}


def endpoint_is_configurable(name: str) -> bool:
    """Return the backend-owned endpoint editability declaration."""
    cls = BACKENDS.get((name or "").lower())
    if cls is None:
        raise ValueError(f"Unknown backend '{name}'")
    return bool(cls.endpoint_configurable)


def _configurable_endpoint_map(raw: dict = None) -> dict[str, str]:
    """Keep only string endpoint entries owned by configurable backends."""
    source = raw if isinstance(raw, dict) else {}
    return {
        name: value
        for name, value in source.items()
        if isinstance(name, str)
        and isinstance(value, str)
        and name in BACKENDS
        and BACKENDS[name].endpoint_configurable
    }


def migrate_legacy_backend_endpoint(*, persist: bool = True) -> bool:
    """Consume the old generic URL once, bounded to the selected backend.

    A legacy ``llm_backend_url`` may seed only the backend that was selected
    with it, and only when that backend declares its endpoint configurable
    and has no provider-keyed value yet.  Whether seeded or intentionally
    discarded for a fixed provider, the durable marker prevents the generic
    value from becoming authority later when a different backend is chosen.

    ``persist=False`` stages the result in live config so Settings can include
    it in its existing all-or-nothing prefs transaction.  Returns True only
    when a provider-keyed value was newly seeded.  A failed direct prefs write
    leaves the marker false so the safe migration can be retried.
    """
    if getattr(config, "BACKEND_ENDPOINTS_MIGRATED", False):
        return False

    from core import persistence

    prefs = persistence.load()
    endpoints = _configurable_endpoint_map(
        prefs.get("backend_endpoints", getattr(config, "BACKEND_ENDPOINTS", {}))
    )
    backend_name = getattr(config, "LLM_BACKEND", "llamacpp").lower()
    legacy_url = getattr(config, "LLM_BACKEND_URL", "").strip()
    seeded = False
    cls = BACKENDS.get(backend_name)
    if (
        cls is not None
        and cls.endpoint_configurable
        and backend_name not in endpoints
        and legacy_url
    ):
        endpoints[backend_name] = legacy_url
        seeded = True

    prefs["backend_endpoints"] = endpoints
    prefs["backend_endpoints_migrated"] = True
    if not persist:
        config.BACKEND_ENDPOINTS = endpoints
        config.BACKEND_ENDPOINTS_MIGRATED = True
        return seeded
    if not persistence.save(prefs):
        return False

    config.BACKEND_ENDPOINTS = endpoints
    config.BACKEND_ENDPOINTS_MIGRATED = True
    return seeded


def get_backend_endpoint(name: str) -> str:
    """Return endpoint truth for Settings and backend construction."""
    backend_name = (name or "").lower()
    cls = BACKENDS.get(backend_name)
    if cls is None:
        raise ValueError(f"Unknown backend '{backend_name}'")
    if not cls.endpoint_configurable:
        return cls.default_url.rstrip("/")

    endpoints = _configurable_endpoint_map(
        getattr(config, "BACKEND_ENDPOINTS", {})
    )
    if backend_name in endpoints:
        return endpoints[backend_name].rstrip("/")

    # Read compatibility if a migration write failed: only the currently
    # selected configurable backend may see the legacy value, never a fixed
    # provider or another configurable backend.
    if (
        not getattr(config, "BACKEND_ENDPOINTS_MIGRATED", False)
        and backend_name == getattr(config, "LLM_BACKEND", "").lower()
    ):
        legacy_url = getattr(config, "LLM_BACKEND_URL", "").strip()
        if legacy_url:
            return legacy_url.rstrip("/")
    return cls.default_url.rstrip("/")

def _apply_model_override(instance, model: str) -> None:
    """MULTIMODAL-PER-CAPABILITY-MODEL-BINDING-01 -- per-invocation model
    override on a FRESHLY CONSTRUCTED, non-shared backend instance.

    ``get_llm_backend()`` always returns a brand new instance (no cache,
    no singleton) -- see core.backends.base.BaseLLMBackend's own
    ``_complete_utility_request()`` docstring, which already documents
    this for every real caller. Writing to this one instance's own model
    attribute is therefore never a global/shared-state mutation and never
    needs a save/restore dance: nothing else holds a reference to it, and
    it is never `config`, never a persisted preference, never
    `agent.llm`, never a cached/reused probe.

    Writes to whichever attribute this backend actually tracks its
    configured model on -- ``self._model`` for every backend except
    AnthropicBackend/GeminiBackend, which use ``self.default_model``
    instead (see BaseLLMBackend.configured_model()'s own documented
    distinction). ``hasattr`` is reliable here because every real
    backend's ``__init__`` already sets exactly one of the two before
    this function can ever see the instance.
    """
    if hasattr(instance, "default_model"):
        instance.default_model = model
    else:
        instance._model = model


def get_llm_backend(name: str = None, url: str = None, api_key: str = None, model: str = None):
    """
    Instantiate and return a backend by name.
    Falls back to config.LLM_BACKEND, then 'llamacpp'.

    `model`: MULTIMODAL-PER-CAPABILITY-MODEL-BINDING-01 -- optional per-
    invocation model override applied to the freshly constructed instance
    this call returns (see _apply_model_override() above). Omitted/None/
    empty leaves the instance exactly as its own __init__ configured it
    from `config` -- byte-identical to every pre-existing caller of this
    function.
    """
    backend_name = (name or getattr(config, "LLM_BACKEND", "llamacpp")).lower()
    cls = BACKENDS.get(backend_name)
    if cls is None:
        raise ValueError(
            f"Unknown backend '{backend_name}'. "
            f"Available: {', '.join(BACKENDS.keys())}"
        )
    # A caller-supplied URL is meaningful only for a backend that explicitly
    # owns a configurable endpoint.  Fixed providers always receive their
    # declared endpoint, even if a stale generic Settings value is supplied.
    endpoint = (
        (url if url is not None else get_backend_endpoint(backend_name))
        if cls.endpoint_configurable
        else cls.default_url
    )
    if api_key is not None and backend_name in {
        "openrouter", "deepseek", "groq", "openai", "anthropic",
        "gemini", "kimi", "qwen", "custom", "omniroute",
    }:
        instance = cls(base_url=endpoint, api_key=api_key)
    else:
        instance = cls(base_url=endpoint)
    if model:
        _apply_model_override(instance, model)
    return instance
