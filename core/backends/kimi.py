"""
core/backends/kimi.py

Kimi (Moonshot AI) backend — OpenAI-compatible API.
Set KIMI_API_KEY in config.py.

Fully OpenAI-compatible wire format (same shape as Groq/OpenRouter/DeepSeek) —
subclasses LMStudioBackend and inherits chat()/chat_stream()/extract_message()
and the full think-block parsing pipeline unmodified. Only __init__, get_model(),
list_models(), health_check() are overridden, matching the groq.py/openrouter.py
pattern exactly.
"""

from typing import Optional
from .lmstudio import LMStudioBackend, discover_openai_compatible_models
from .base import ModelDiscoveryOutcome, ModelDiscoveryResult
import config


class KimiBackend(LMStudioBackend):

    name = "kimi"
    display_name = "Kimi (Moonshot AI)"
    default_url = "https://api.moonshot.cn/v1"
    endpoint_configurable = False

    # Offline suggestions only; never evidence of a successful refresh.
    KNOWN_MODELS = [
        "moonshot-v1-8k",
        "moonshot-v1-32k",
        "moonshot-v1-128k",
        "kimi-latest",
    ]

    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None):
        self.base_url = (base_url or self.default_url).rstrip("/")
        self.api_key = getattr(config, "KIMI_API_KEY", "") if api_key is None else api_key
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        self._model = getattr(config, "KIMI_DEFAULT_MODEL", "kimi-latest")

    def get_model(self) -> str:
        return self._model

    def list_models(self) -> list[str]:
        """Compatibility output: live IDs, else explicit offline suggestions."""
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
            return False, "KIMI_API_KEY not set in config.py"
        return True, f"Configured — {self._model}"
