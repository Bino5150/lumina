"""
DeepSeek backend — OpenAI-compatible API for DeepSeek models.
Supports deepseek-v4-flash (non-thinking) and deepseek-v4-pro (thinking).
Note: deepseek-chat and deepseek-reasoner are deprecated as of 2026-07-24.
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
        "deepseek-v4-flash",    # DeepSeek-V4-Flash — fast, cheap, thinking mode default
        "deepseek-v4-pro",      # DeepSeek-V4-Pro — higher quality, slower
        "deepseek-chat",        # deprecated 2026-07-24 (alias for v4-flash non-thinking)
        "deepseek-reasoner",    # deprecated 2026-07-24 (alias for v4-flash thinking)
    ]

    def __init__(self, base_url: Optional[str] = None):
        self.base_url = (base_url or self.default_url).rstrip("/")
        self.api_key = getattr(config, "DEEPSEEK_API_KEY", "")
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        self._model = getattr(config, "DEEPSEEK_DEFAULT_MODEL", "deepseek-v4-flash")

    def get_model(self) -> str:
        return self._model

    def list_models(self) -> list[str]:
        return self.KNOWN_MODELS

    def health_check(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "DEEPSEEK_API_KEY not set in config.py"
        return True, f"Configured — {self._model}"
