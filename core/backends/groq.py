"""
Groq backend — OpenAI-compatible API, extremely fast inference (LPU hardware).
Great for Llama 3, Mixtral, Gemma at 500+ tok/s.
Set GROQ_API_KEY in config.py
"""

from typing import Optional
from .lmstudio import LMStudioBackend
import config


class GroqBackend(LMStudioBackend):

    name = "groq"
    display_name = "Groq"
    default_url = "https://api.groq.com/openai/v1"

    KNOWN_MODELS = [
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
        "llama3-groq-70b-8192-tool-use-preview",  # explicit tool-call tuned variant
        "llama3-groq-8b-8192-tool-use-preview",
        "mixtral-8x7b-32768",
        "gemma2-9b-it",
    ]

    def __init__(self, base_url: Optional[str] = None):
        self.base_url = (base_url or self.default_url).rstrip("/")
        self.api_key = getattr(config, "GROQ_API_KEY", "")
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        self._model = getattr(config, "GROQ_DEFAULT_MODEL", "llama-3.3-70b-versatile")

    def get_model(self) -> str:
        return self._model

    def list_models(self) -> list[str]:
        """Groq supports /models endpoint too."""
        import requests
        try:
            resp = requests.get(f"{self.base_url}/models", headers=self.headers, timeout=10)
            data = resp.json().get("data", [])
            if data:
                return [m["id"] for m in data]
        except Exception:
            pass
        return self.KNOWN_MODELS

    def health_check(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "GROQ_API_KEY not set in config.py"
        return True, f"Configured — {self._model}"
