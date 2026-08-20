"""
OpenAI backend — official OpenAI API.
Set OPENAI_API_KEY in config.py
"""

from typing import Optional
from .lmstudio import LMStudioBackend
from .reasoning import ReasoningCapabilities, NO_REASONING_CONTROL
import config


# Patch 3A.4 Part 2A -- per-model reasoning-effort capability data.
#
# Deliberately declared here on OpenAIBackend, NOT on LMStudioBackend --
# GroqBackend and every other LMStudioBackend-derived backend must keep
# reporting NO_REASONING_CONTROL unless it has its own real data (see
# reasoning_capabilities() below and tests/test_reasoning_translation.py's
# sibling-leakage checks).
#
# This is intentionally NOT cross-referenced against KNOWN_MODELS above --
# KNOWN_MODELS is a stale placeholder list (gpt-4o/gpt-4o-mini/etc.) that
# this slice was explicitly told not to touch, and reasoning_capabilities()
# takes an explicit `model` string from the caller rather than consulting
# it. An unlisted/unknown model (including every KNOWN_MODELS entry above)
# correctly falls through to NO_REASONING_CONTROL below.
_REASONING_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")
_GPT_5_6_CAPS = ReasoningCapabilities(efforts=_REASONING_EFFORTS, default_effort="medium")

_REASONING_MODELS = {
    "gpt-5.6": _GPT_5_6_CAPS,
    "gpt-5.6-sol": _GPT_5_6_CAPS,
    "gpt-5.6-terra": _GPT_5_6_CAPS,
    "gpt-5.6-luna": _GPT_5_6_CAPS,
}


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

    # ------------------------------------------------------------------
    # Patch 3A.4 Part 2A -- reasoning-effort capability + wire translation
    # ------------------------------------------------------------------

    def reasoning_capabilities(self, model: Optional[str] = None) -> ReasoningCapabilities:
        """
        Implemented directly here, NOT on LMStudioBackend -- see the module
        docstring above _REASONING_MODELS for why. `model=None` (no model
        specified) always falls through to NO_REASONING_CONTROL rather than
        guessing this backend's currently-configured model, matching the
        base class's documented contract (get_model() is not consulted).
        """
        if model is None:
            return NO_REASONING_CONTROL
        return _REASONING_MODELS.get(model, NO_REASONING_CONTROL)

    def _apply_reasoning_override(self, payload: dict, effort: str,
                                   model: Optional[str] = None) -> None:
        """
        Wire translation for Lumina's existing Chat Completions endpoint
        (this backend inherits chat()/chat_stream() payload-building from
        LMStudioBackend, which stays on /chat/completions -- NOT the
        Responses API shape {"reasoning": {"effort": ...}}).
        """
        payload["reasoning_effort"] = effort
