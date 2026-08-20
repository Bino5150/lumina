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
from .reasoning import ReasoningCapabilities, NO_REASONING_CONTROL
import config


# Patch 3A.4 Part 2B -- per-model reasoning ("thinking") capability data.
#
# Declared here on QwenBackend, NOT on LMStudioBackend -- same
# inheritance-safety rule every other Part 2A/2B provider in this patch
# follows (OpenAIBackend/GroqBackend/AnthropicBackend/GeminiBackend all
# keep their capability tables on their own concrete class; see
# tests/test_reasoning_translation.py's sibling-leakage checks). An
# explicit id -> ReasoningCapabilities table, not substring/family-name
# matching -- an unlisted model id (including every other entry in
# KNOWN_MODELS above) falls through to NO_REASONING_CONTROL, never a
# guess.
#
# Verified 2026-08-20 against Alibaba Cloud Model Studio's authoritative
# "Use deep thinking models via API" doc (the canonical DashScope/Model
# Studio reasoning-control reference):
#   https://www.alibabacloud.com/help/en/model-studio/deep-thinking
#
# Confirmed facts pulled from that page:
#
#   - qwen3.5-plus, qwen3.5-flash, qwen3.6-plus, qwen3.6-flash,
#     qwen3.7-plus, qwen3.7-max are each explicitly listed under
#     "Thinking Enabled by Default" -- real, togglable hybrid-thinking
#     models (a genuine enable_thinking on/off control), default ON.
#     These are exactly the "Qwen 3.5/3.6/3.7 hybrid models" this slice
#     was scoped to cover, including qwen3.5-plus, the repo's actual
#     QWEN_DEFAULT_MODEL.
#
#   - "qwen3-235b-a22b-thinking-2507" is explicitly listed under
#     "Thinking-Only (Cannot Disable)" -- always reasons, no
#     enable_thinking=False path exists for it. Not in KNOWN_MODELS or
#     QWEN_DEFAULT_MODEL today, but mapped here anyway since a live
#     list_models() call against DashScope's real /models endpoint could
#     surface it, same as any other id not in the static KNOWN_MODELS
#     fallback list.
#
#   - Deliberately LEFT UNMAPPED (fall through to NO_REASONING_CONTROL):
#     "qwen3.5-max" is not mentioned anywhere on that page under either
#     name. "qwen-max" (bare, no "3") is also not mentioned -- only the
#     distinct id "qwen3-max" is, under "Qwen3 (commercial)" / "thinking
#     disabled by default". "qwen-plus" and "qwen-turbo" (bare,
#     unversioned aliases) ARE explicitly confirmed hybrid there too
#     ("Qwen3 (commercial)", thinking disabled by default) -- but that
#     "Qwen3 (commercial)" bucket is a distinct doc category from the
#     versioned "Qwen3.5/3.6/3.7" sections this slice was scoped to, so
#     they are intentionally left unmapped here rather than stretching
#     scope beyond what was asked for. Leaving them unmapped is safe --
#     they simply advertise no reasoning control today rather than
#     silently getting an unverified one.
#
#   - thinking_budget: the page states "The thinking_budget parameter is
#     supported by Qwen3 (in thinking mode) and Kimi models" -- a
#     generation-level statement, not a verbatim per-model-id list.
#     Interpreted here as covering every confirmed-hybrid Qwen3-generation
#     model above while thinking is active, since thinking_budget only has
#     meaning when thinking mode is actually on -- i.e. precisely the
#     models that can BE in thinking mode per the bullets above. This is
#     an interpretive step from a family-level doc statement, not a
#     verbatim per-id quote; flagging that distinction explicitly rather
#     than overstating the citation. NOT applied to the thinking-only
#     mapping below -- that model's budget support was not independently
#     confirmed, so supports_budget stays False there (conservative).
#
#   - reasoning_effort is confirmed NOT to be Qwen's control on DashScope:
#     the same page's DeepSeek (deepseek-v4-pro/-flash) and GLM entries
#     use reasoning_effort; Qwen's OpenAI-compatible mode uses
#     enable_thinking/thinking_budget instead. No fake low/medium/high/
#     xhigh ladder is advertised for Qwen here as a result -- see
#     _apply_reasoning_override() below, which never emits
#     reasoning_effort for this backend.
_QWEN_HYBRID_ENABLED_BY_DEFAULT = ReasoningCapabilities(
    efforts=("disabled", "enabled"),
    default_effort="enabled",
    supports_budget=True,
)

_QWEN_THINKING_ONLY = ReasoningCapabilities(
    efforts=(),
    mandatory=True,
    supports_budget=False,
)

_REASONING_MODELS = {
    "qwen3.5-plus": _QWEN_HYBRID_ENABLED_BY_DEFAULT,
    "qwen3.5-flash": _QWEN_HYBRID_ENABLED_BY_DEFAULT,
    "qwen3.6-plus": _QWEN_HYBRID_ENABLED_BY_DEFAULT,
    "qwen3.6-flash": _QWEN_HYBRID_ENABLED_BY_DEFAULT,
    "qwen3.7-plus": _QWEN_HYBRID_ENABLED_BY_DEFAULT,
    "qwen3.7-max": _QWEN_HYBRID_ENABLED_BY_DEFAULT,
    "qwen3-235b-a22b-thinking-2507": _QWEN_THINKING_ONLY,
}


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

    # ------------------------------------------------------------------
    # Patch 3A.4 Part 2B -- reasoning-effort capability + wire translation
    # ------------------------------------------------------------------

    def reasoning_capabilities(self, model: Optional[str] = None) -> ReasoningCapabilities:
        """
        Implemented directly here, NOT on LMStudioBackend -- see the
        module-level comment above _REASONING_MODELS for the full
        verification citation and scope notes. Explicit table lookup
        only -- deliberately NOT substring/prefix matching (e.g. "if
        'qwen3' in model_name"), so an unverified model id (including
        "qwen-max"/"qwen-plus"/"qwen-turbo"/"qwen3.5-max", all present in
        KNOWN_MODELS above but NOT in _REASONING_MODELS) correctly falls
        through to NO_REASONING_CONTROL rather than being guessed into a
        family by name pattern. `model=None` always falls through to
        NO_REASONING_CONTROL, matching every other backend's contract in
        this patch (get_model() is not consulted).
        """
        if model is None:
            return NO_REASONING_CONTROL
        return _REASONING_MODELS.get(model, NO_REASONING_CONTROL)

    def _apply_reasoning_override(self, payload: dict, effort: str,
                                   model: Optional[str] = None) -> None:
        """
        Real DashScope hybrid-thinking control -- enable_thinking, NOT an
        OpenAI/Groq-style reasoning_effort string (Qwen's OpenAI-compatible
        mode on DashScope does not use that field for Qwen models; see the
        module docstring above for the DeepSeek/GLM-vs-Qwen citation).
        Only "disabled"/"enabled" can ever reach here -- both already
        validated against _REASONING_MODELS above by apply_reasoning()
        before this hook is even called -- so this is a direct, exhaustive
        two-way mapping rather than an if/else with a silent fallthrough
        that could swallow an unexpected third value without emitting
        anything.

        Provider Default (`None` on the original request) never reaches
        this method at all -- apply_reasoning() returns early on None --
        so neither "enable_thinking" nor "thinking_budget" is ever emitted
        for a Provider Default request, preserving Qwen's own native
        default behavior untouched. No numeric thinking_budget value is
        emitted here regardless of supports_budget -- that's capability
        metadata only for later work per the Part 2B spec; nothing
        consumes it yet.
        """
        if effort == "disabled":
            payload["enable_thinking"] = False
        elif effort == "enabled":
            payload["enable_thinking"] = True
