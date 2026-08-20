"""
Groq backend — OpenAI-compatible API, extremely fast inference (LPU hardware).
Great for Llama 3, Mixtral, Gemma at 500+ tok/s.
Set GROQ_API_KEY in config.py
"""

from typing import Optional
from .lmstudio import LMStudioBackend
from .reasoning import ReasoningCapabilities, NO_REASONING_CONTROL
import config


# Patch 3A.4 Part 2A -- per-model reasoning-effort capability data.
#
# Declared here on GroqBackend, NOT on LMStudioBackend -- OpenAIBackend and
# every other LMStudioBackend-derived backend must keep reporting
# NO_REASONING_CONTROL unless it has its own real data.
#
# Model-specific, not provider-wide: GPT-OSS and Qwen3 reasoning models on
# Groq advertise two completely different, non-overlapping effort
# vocabularies (verified against console.groq.com/docs/reasoning,
# 2026-08-20) -- offering one family's values to the other would be a
# silent capability lie, so each gets its own ReasoningCapabilities
# instance rather than a shared one.
#
# GPT-OSS 120B/20B: reasoning_effort accepts low/medium/high, default medium.
_GPT_OSS_CAPS = ReasoningCapabilities(
    efforts=("low", "medium", "high"),
    default_effort="medium",
)

# Qwen3.6 27B (current non-deprecated Qwen3 reasoning model on Groq as of
# 2026-08-20 -- qwen/qwen3-32b was deprecated 2026-06-17 in favor of this
# model per the same docs page): reasoning_effort accepts none/default.
# "default" here is a REAL, distinct, provider-advertised literal string --
# not to be confused with Lumina's own `None` Provider-Default sentinel.
# See tests/test_reasoning_translation.py for the explicit side-by-side
# proof this distinction can't regress silently.
_QWEN_REASONING_CAPS = ReasoningCapabilities(
    efforts=("none", "default"),
)

_REASONING_MODELS = {
    "openai/gpt-oss-120b": _GPT_OSS_CAPS,
    "openai/gpt-oss-20b": _GPT_OSS_CAPS,
    "qwen/qwen3.6-27b": _QWEN_REASONING_CAPS,
}


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

    # ------------------------------------------------------------------
    # Patch 3A.4 Part 2A -- reasoning-effort capability + wire translation
    # ------------------------------------------------------------------

    def reasoning_capabilities(self, model: Optional[str] = None) -> ReasoningCapabilities:
        """
        Implemented directly here, NOT on LMStudioBackend -- see the module
        docstring above _REASONING_MODELS for why. `model=None` always
        falls through to NO_REASONING_CONTROL, matching the base class's
        documented contract (get_model() is not consulted).
        """
        if model is None:
            return NO_REASONING_CONTROL
        return _REASONING_MODELS.get(model, NO_REASONING_CONTROL)

    def _apply_reasoning_override(self, payload: dict, effort: str,
                                   model: Optional[str] = None) -> None:
        """
        Same Chat-Completions-shaped `reasoning_effort` field GPT-OSS and
        Qwen3 both use on Groq -- only the set of values that validated to
        get here differs (see reasoning_capabilities() / _REASONING_MODELS
        above). For a Qwen model this emits the literal provider value
        "default" verbatim when that's what was requested and validated --
        this is NOT the same thing as Lumina's `None` Provider-Default
        sentinel, which never reaches this method at all (apply_reasoning()
        returns early on None before calling here).
        """
        payload["reasoning_effort"] = effort
