"""
core/backends/qwen.py

Qwen (Alibaba DashScope) backend — OpenAI-compatible mode.
Set QWEN_API_KEY in config.py.

DashScope exposes an OpenAI-compatible endpoint specifically so clients don't
need custom wire-format handling: same shape as Groq/OpenRouter/Kimi. Subclasses
LMStudioBackend, inherits the full streaming + think-block pipeline unmodified.

First-class priority per project notes: Qwen3.5 is the architecture underpinning
Lumina's entire local inference stack (Qwopus3.5-v3-4B), so the cloud-tier Qwen
models are a natural fit for side-by-side comparison / fallback when local
inference is unavailable or under-resourced (e.g. during a beellama rebuild).

International endpoint used here (dashscope-intl) — if requests get region-blocked
or auth fails unexpectedly, the mainland endpoint is dashscope.aliyuncs.com
(no -intl) and may require a different account registration. Flagging this since
it's not something a code review alone would catch — worth a one-line note in
Settings UI if this trips someone up later.
"""

from typing import Optional
from .lmstudio import LMStudioBackend
import config


class QwenBackend(LMStudioBackend):

    name = "qwen"
    display_name = "Qwen (DashScope)"
    default_url = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"

    KNOWN_MODELS = [
        "qwen3.5-max",
        "qwen3.5-plus",
        "qwen3.5-flash",
        "qwen-max",
        "qwen-plus",
        "qwen-turbo",
    ]

    def __init__(self, base_url: Optional[str] = None):
        self.base_url = (base_url or self.default_url).rstrip("/")
        self.api_key = getattr(config, "QWEN_API_KEY", "")
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        self._model = getattr(config, "QWEN_DEFAULT_MODEL", "qwen3.5-plus")

    def get_model(self) -> str:
        return self._model

    def list_models(self) -> list[str]:
        """DashScope's OpenAI-compat mode supports /models."""
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
            return False, "QWEN_API_KEY not set in config.py"
        return True, f"Configured — {self._model}"
