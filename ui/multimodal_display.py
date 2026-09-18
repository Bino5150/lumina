"""
ui/multimodal_display.py -- MULTIMODAL-PER-CAPABILITY-MODEL-BINDING-01 CI
repair.

Qt-free display-only data for the Image Generation section of Multimodal
Settings (ui.settings.tts_tab.MultimodalTab). Deliberately lives OUTSIDE
the ui.settings package: importing anything under ui.settings.* runs
ui/settings/__init__.py first, which unconditionally imports every Settings
tab (PySide6 and all) -- so even a pure-Python constant defined in a
ui.settings submodule drags in a hard PySide6 dependency purely as a side
effect of package import order. ui/__init__.py is empty, so this module
(zero imports beyond stdlib) is safe to import in a headless environment
with no PySide6 installed at all -- exactly the CI environment this module
exists to make these values testable in.

Cosmetic labels ONLY -- this is deliberately NOT a second Higgsfield model
catalog. core.higgsfield_adapter.SUPPORTED_MODELS remains the sole
canonical model list; an id missing from HIGGSFIELD_MODEL_LABELS just
displays as itself. Persistence/invocation always use the canonical id
(e.g. "higgsfield-ai/soul/standard"), never a label from this module.
"""

HIGGSFIELD_MODEL_LABELS = {
    "higgsfield-ai/soul/standard": "Soul Standard",
}

# Source-vetted 2026-09-18 (see project-evidence/campaign-reports/
# MULTIMODAL_M4_HIGGSFIELD_PRICING_REPAIR_01_2026-09-18.md Sec 2): the
# official public Higgsfield model/pricing marketplace -- lists every
# current model's live price, so this link stays correct even if a future
# model is added without a Settings code change. No API key, no account
# identifier, no query string -- a bare, static, official URL.
HIGGSFIELD_PRICING_URL = "https://console.higgsfield.ai"


def higgsfield_model_label(model_id: str) -> str:
    return HIGGSFIELD_MODEL_LABELS.get(model_id, model_id)
