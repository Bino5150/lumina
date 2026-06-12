"""
DeepSeek backend — OpenAI-compatible API for DeepSeek models.
Supports deepseek-chat (V3) and deepseek-reasoner (R1).
Set DEEPSEEK_API_KEY in config.py
"""

from typing import Optional
from .lmstudio import LMStudioBackend
import config


class DeepSeekBackend(LMStudioBackend):

    name = "deepseek"
    display_name = "DeepSeek"
    default_url = "https://api.deepseek.com/v1"

    KNOWN_MODELS = [
        "deepseek-chat",        # DeepSeek-V3 — fast, cheap, great for tool calling
        "deepseek-reasoner",    # DeepSeek-R1 — chain-of-thought, slower
    ]

    def __init__(self, base_url: Optional[str] = None):
        self.base_url = (base_url or self.default_url).rstrip("/")
        self.api_key = getattr(config, "DEEPSEEK_API_KEY", "")
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        self._model = getattr(config, "DEEPSEEK_DEFAULT_MODEL", "deepseek-chat")

    def get_model(self) -> str:
        return self._model

    def list_models(self) -> list[str]:
        return self.KNOWN_MODELS

    def health_check(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "DEEPSEEK_API_KEY not set in config.py"
        return True, f"Configured — {self._model}"
