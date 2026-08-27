"""
DeepSeek backend — OpenAI-compatible API for DeepSeek models.
Supports deepseek-v4-flash (non-thinking) and deepseek-v4-pro (thinking).
Note: deepseek-chat and deepseek-reasoner are deprecated as of 2026-07-24.
Set DEEPSEEK_API_KEY in config.py
"""

from typing import Optional
from .lmstudio import LMStudioBackend, discover_openai_compatible_models
from .base import ModelDiscoveryOutcome, ModelDiscoveryResult
import config


class DeepSeekBackend(LMStudioBackend):

    name = "deepseek"
    display_name = "DeepSeek"
    default_url = "https://api.deepseek.com/v1"
    endpoint_configurable = False

    # Offline suggestions only; never evidence of a successful refresh.
    KNOWN_MODELS = [
        "deepseek-v4-flash",    # DeepSeek-V4-Flash — fast, cheap, thinking mode default
        "deepseek-v4-pro",      # DeepSeek-V4-Pro — higher quality, slower
        "deepseek-chat",        # deprecated 2026-07-24 (alias for v4-flash non-thinking)
        "deepseek-reasoner",    # deprecated 2026-07-24 (alias for v4-flash thinking)
    ]

    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None):
        self.base_url = (base_url or self.default_url).rstrip("/")
        self.api_key = getattr(config, "DEEPSEEK_API_KEY", "") if api_key is None else api_key
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        self._model = getattr(config, "DEEPSEEK_DEFAULT_MODEL", "deepseek-v4-flash")

    def get_model(self) -> str:
        return self._model

    def list_models(self) -> list[str]:
        result = self.discover_models()
        if result.outcome is ModelDiscoveryOutcome.SUCCESS:
            return list(result.models)
        return list(result.offline_suggestions)

    def discover_models(self) -> ModelDiscoveryResult:
        return discover_openai_compatible_models(
            self, offline_suggestions=self.KNOWN_MODELS
        )

    def health_check(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "DEEPSEEK_API_KEY not set in config.py"
        return True, f"Configured — {self._model}"

    def _apply_disable_thinking(self, payload: dict) -> None:
        """No local thinking-disable wire fields on DeepSeek's transport.

        UTILITY-RUNTIME-01: DeepSeek's OpenAI-compatible API does not
        document LM Studio's local `thinking` / `chat_template_kwargs`
        fields; utility requests must carry only documented fields.
        complete_utility()'s assistant-prefill plus its own output-side
        think-strip remain the anti-bleed defense here. (DeepSeek's own
        thinking controls are model-selection-driven -- v4-flash vs
        v4-pro -- not per-request fields.)
        """
        return None
