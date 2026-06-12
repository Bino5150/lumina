"""
OpenAI backend — official OpenAI API.
Set OPENAI_API_KEY in config.py
"""

from typing import Optional
from .lmstudio import LMStudioBackend
import config


class OpenAIBackend(LMStudioBackend):

    name = "openai"
    display_name = "OpenAI"
    default_url = "https://api.openai.com/v1"

    KNOWN_MODELS = [
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-4-turbo",
        "gpt-3.5-turbo",
    ]

    def __init__(self, base_url: Optional[str] = None):
        self.base_url = (base_url or self.default_url).rstrip("/")
        self.api_key = getattr(config, "OPENAI_API_KEY", "")
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        self._model = getattr(config, "OPENAI_DEFAULT_MODEL", "gpt-4o-mini")

    def get_model(self) -> str:
        return self._model

    def list_models(self) -> list[str]:
        return self.KNOWN_MODELS

    def health_check(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "OPENAI_API_KEY not set in config.py"
        return True, f"Configured — {self._model}"
