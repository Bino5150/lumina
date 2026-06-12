"""
ackend loader — returns the active LLM backend instance based on config.
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

BACKENDS = {
    "lmstudio":   LMStudioBackend,
    "ollama":     OllamaBackend,
    "llamacpp":   LlamaCppBackend,
    "vllm":       VLLMBackend,
    "openrouter": OpenRouterBackend,
    "deepseek":   DeepSeekBackend,
    "groq":       GroqBackend,
    "openai":     OpenAIBackend,
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
