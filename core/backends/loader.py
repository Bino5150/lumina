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

class CustomBackend(LMStudioBackend):
    """Generic OpenAI-compatible endpoint. URL and optional API key set by user."""
    name = "custom"
    display_name = "Custom (OpenAI-compatible)"
    default_url = ""

    def __init__(self, base_url: str = None):
        super().__init__(base_url=base_url)
        key = getattr(config, "CUSTOM_API_KEY", "").strip()
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}" if key else "Bearer lumina",
        }

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
}

def get_llm_backend(name: str = None, url: str = None):
    """
    Instantiate and return a backend by name.
    Falls back to config.LLM_BACKEND, then 'llamacpp'.
    """
    backend_name = (name or getattr(config, "LLM_BACKEND", "llamacpp")).lower()
    cls = BACKENDS.get(backend_name)
    if cls is None:
        raise ValueError(
            f"Unknown backend '{backend_name}'. "
            f"Available: {', '.join(BACKENDS.keys())}"
        )
    return cls(base_url=url) if url else cls()
