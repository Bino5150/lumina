"""
OpenRouter backend — OpenAI-compatible cloud inference aggregator.
Supports hundreds of models from OpenAI, Anthropic, Meta, Google, etc.
Set OPENROUTER_API_KEY in config.py
"""

from typing import Optional, Generator
from .lmstudio import LMStudioBackend
import config


class OpenRouterBackend(LMStudioBackend):

    name = "openrouter"
    display_name = "OpenRouter"
    default_url = "https://openrouter.ai/api/v1"

    def __init__(self, base_url: Optional[str] = None):
        self.base_url = (base_url or self.default_url).rstrip("/")
        self.api_key = getattr(config, "OPENROUTER_API_KEY", "")
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": "https://github.com/mothugs/lumina",   # optional but good practice
            "X-Title": "Lumina",
        }
        self._model = getattr(config, "OPENROUTER_DEFAULT_MODEL", "meta-llama/llama-3.1-8b-instruct:free")

    def get_model(self) -> str:
        return self._model

    def list_models(self) -> list[str]:
        """Fetch live model list from OpenRouter."""
        import requests
        try:
            resp = requests.get(f"{self.base_url}/models", headers=self.headers, timeout=10)
            return [m["id"] for m in resp.json().get("data", [])]
        except Exception:
            return [self._model]

    def health_check(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "OPENROUTER_API_KEY not set in config.py"
        return True, f"Configured — {self._model}"
