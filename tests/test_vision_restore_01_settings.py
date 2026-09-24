"""VISION-RESTORE-01 -- vision specialist model persistence, specialist model
discovery, and the General "Model Name" row.

Root cause (live-reproduced against the owner's persisted state): the
editable vision model combo keeps its previous currentIndex while the owner
types, so _vision_model_override() read currentData() of the PREVIOUS item --
"" (provider default) or a stale override -- and a typed model never reached
prefs.json. Relaunch then rendered "Provider default — <OpenAI default>",
which looked like a stuck override.

Every edit here uses real key events on the real widget, a real Save, and a
"restart" that re-executes config.py's own loader against the written
prefs.json and builds a fresh tab from it. Provider HTTP is stubbed at
requests.get; nothing talks to a live provider.

Blocking in hosted CI: the qt-security-guards job in .github/workflows/
tests.yml runs this file with LUMINA_REQUIRE_QT=1, where a missing PySide6
fails collection instead of skipping (same contract as
test_castle_walls_repaired_wiring_01.py). The plain `test` job has no PySide6
and skips it.
"""

import os
import runpy
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import requests

if os.environ.get("LUMINA_REQUIRE_QT") == "1":
    import PySide6  # noqa: F401 -- the Qt guard job must fail, never skip
else:
    pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import config
from core import persistence
from core.backends.openai_backend import OpenAIBackend
from ui.main_window import COLORS
from ui.settings.tts_tab import MultimodalTab

_RESTART_KEYS = (
    "MULTIMODAL_ROUTES", "MULTIMODAL_DISABLED_PROVIDERS", "OPENAI_DEFAULT_MODEL",
    "OPENROUTER_DEFAULT_MODEL",
    "OPENAI_API_KEY", "GEMINI_DEFAULT_MODEL", "TTS_ENABLED", "TTS_BACKEND",
    "TTS_HOST", "VOICEBOX_HOST", "VOICEBOX_PROFILE", "STT_ENABLED",
    "STT_BACKEND", "STT_MODEL", "STT_DEVICE", "LLM_BACKEND",
    "CUSTOM_DEFAULT_MODEL", "OMNIROUTE_DEFAULT_MODEL",
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def owner_state(tmp_path, monkeypatch):
    """The owner's live topology: OpenRouter primary, OpenAI vision
    specialist with NO explicit model, OpenAI provider default gpt-6-luna."""
    for key in _RESTART_KEYS:
        monkeypatch.setattr(config, key, getattr(config, key, None))
    unexpected = []

    def no_live_provider(url, *args, **kwargs):
        unexpected.append(url)
        raise requests.ConnectionError("live provider I/O is forbidden in this module")

    monkeypatch.setattr(requests, "get", no_live_provider)
    monkeypatch.setattr(requests, "post", no_live_provider)
    monkeypatch.setenv("LUMINA_DATA_DIR", str(tmp_path))
    (tmp_path / "memory").mkdir()
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "memory" / "prefs.json"))
    persistence.save({
        "llm_backend": "openrouter",
        "tts_enabled": False,
        "cloud_credentials": {
            "openai": {"default_model": "gpt-6-luna"},
            "openrouter": {"default_model": "z-ai/glm-5.3-flash"},
        },
        "custom_default_model": "LongCat-2.0",
        "multimodal_routes": {
            "vision_understanding": {"mode": "specialist", "specialist": "openai", "fallbacks": []},
        },
    })
    _restart(monkeypatch)
    yield
    assert unexpected == [], f"unexpected live provider requests: {unexpected}"


def _restart(monkeypatch):
    """Re-execute config.py's real loader against prefs.json on disk (never a
    hard-coded key here) and publish the result on the live config module."""
    loaded = runpy.run_path(config.__file__)
    for key in _RESTART_KEYS:
        monkeypatch.setattr(config, key, loaded[key])


def _tab(qapp):
    tab = MultimodalTab(SimpleNamespace(tts=None), COLORS)
    tab._apply_live_backend_config = lambda *a, **k: None  # unrelated TTS seam
    tab.show()
    QTest.qWaitForWindowExposed(tab)
    return tab


def _type_model(tab, text):
    edit = tab.vision_model_combo.lineEdit()
    edit.setFocus()
    edit.selectAll()
    if text:
        QTest.keyClicks(edit, text)
    else:
        QTest.keyClick(edit, Qt.Key.Key_Delete)


def _save(tab):
    QTest.mouseClick(tab.save_btn, Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    return persistence.load()["multimodal_routes"]["vision_understanding"]


def _shown(tab):
    return tab.vision_model_combo.currentText()


# ── Persistence ───────────────────────────────────────────────────────────


def test_typed_override_is_saved_and_survives_restart(qapp, owner_state, monkeypatch):
    tab = _tab(qapp)
    assert _shown(tab) == "Provider default — gpt-6-luna"

    _type_model(tab, "gpt-5.6-luna")
    assert _save(tab)["model"] == "gpt-5.6-luna"

    _restart(monkeypatch)
    assert config.MULTIMODAL_ROUTES["vision_understanding"]["model"] == "gpt-5.6-luna"
    reopened = _tab(qapp)
    assert _shown(reopened) == "gpt-5.6-luna"
    assert reopened._vision_model_override() == "gpt-5.6-luna"


def test_typed_override_committed_with_enter_is_saved(qapp, owner_state):
    tab = _tab(qapp)
    _type_model(tab, "gpt-5.6-sol")
    QTest.keyClick(tab.vision_model_combo.lineEdit(), Qt.Key.Key_Return)

    assert _save(tab)["model"] == "gpt-5.6-sol"
    # NoInsert: Enter never appends a data-less duplicate item.
    assert tab.vision_model_combo.count() == 1


def test_stored_gpt6_override_can_be_changed_twice_across_restarts(qapp, owner_state, monkeypatch):
    first = _tab(qapp)
    _type_model(first, "gpt-6-luna")
    assert _save(first)["model"] == "gpt-6-luna"
    _restart(monkeypatch)

    second = _tab(qapp)
    assert _shown(second) == "gpt-6-luna"
    _type_model(second, "gpt-5.6-luna")
    assert _save(second)["model"] == "gpt-5.6-luna"
    _restart(monkeypatch)

    third = _tab(qapp)
    assert _shown(third) == "gpt-5.6-luna"
    _type_model(third, "my-private-vision-finetune")
    assert _save(third)["model"] == "my-private-vision-finetune"
    _restart(monkeypatch)

    assert _shown(_tab(qapp)) == "my-private-vision-finetune"


@pytest.mark.parametrize("clear_by", ("select_provider_default", "delete_text"))
def test_clearing_override_restores_provider_default_and_old_value_does_not_resurrect(
    qapp, owner_state, monkeypatch, clear_by
):
    # Seeded on disk (not through the typing path) so this holds even if
    # typed saves regress: an already-stored explicit gpt-6-luna.
    prefs = persistence.load()
    prefs["multimodal_routes"]["vision_understanding"]["model"] = "gpt-6-luna"
    persistence.save(prefs)
    _restart(monkeypatch)

    tab = _tab(qapp)
    assert tab._vision_model_override() == "gpt-6-luna"
    if clear_by == "select_provider_default":
        tab.vision_model_combo.setCurrentIndex(0)
    else:
        _type_model(tab, "")
    saved = _save(tab)
    assert "model" not in saved
    assert saved["specialist"] == "openai"

    _restart(monkeypatch)
    reopened = _tab(qapp)
    assert reopened._vision_model_override() == ""
    assert reopened.vision_model_combo.currentIndex() == 0
    assert _shown(reopened) == "Provider default — gpt-6-luna"
    assert reopened.vision_model_combo.findText("gpt-6-luna") == -1


def test_provider_default_is_not_persisted_as_an_explicit_override(qapp, owner_state):
    """A default is not an override: saving an untouched provider-default
    selection must not write the provider's current default as a model."""
    tab = _tab(qapp)
    saved = _save(tab)
    assert "model" not in saved


def test_provider_change_keeps_typed_override(qapp, owner_state, monkeypatch):
    monkeypatch.setattr(config, "GEMINI_DEFAULT_MODEL", "gemini-3.5-flash")
    tab = _tab(qapp)
    _type_model(tab, "vision-model-x")
    tab.vision_provider_combo.setCurrentText("gemini")

    assert tab.vision_model_combo.itemText(0) == "Provider default — gemini-3.5-flash"
    assert _shown(tab) == "vision-model-x"
    assert _save(tab)["model"] == "vision-model-x"


# ── Discovery ─────────────────────────────────────────────────────────────


def _openai_models(monkeypatch, *ids):
    seen = []

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": i} for i in ids]}

    def fake_get(url, *args, **kwargs):
        seen.append(url)
        return _Resp()

    monkeypatch.setattr(requests, "get", fake_get)
    return seen


@pytest.fixture
def general_tab(qapp, owner_state, monkeypatch):
    from core.agent import LuminaAgent
    from ui.settings.general_tab import GeneralTab
    agent = LuminaAgent(owner=True, channel_id="vision-restore-01-general")
    return GeneralTab(agent, COLORS)


def test_specialist_refresh_receives_same_model_set_as_main_openai_refresh(
    qapp, general_tab, monkeypatch
):
    live = ("gpt-6-luna", "gpt-6-sol", "gpt-5.6-luna", "some-future-model")
    seen = _openai_models(monkeypatch, *live)
    monkeypatch.setattr(config, "OPENAI_API_KEY", "saved-openai-key")

    general_tab.backend_combo.setCurrentText("openai")
    general_tab._refresh_models()
    main_models = tuple(general_tab.cloud_model.itemText(i) for i in range(general_tab.cloud_model.count()))

    tab = _tab(qapp)
    tab._refresh_vision_models()
    combo = tab.vision_model_combo
    specialist_models = tuple(combo.itemText(i) for i in range(1, combo.count()))

    assert main_models == live
    assert specialist_models == main_models
    assert all(url.startswith(OpenAIBackend.default_url) for url in seen)
    # Live IDs outside the static offline catalog prove this is discovery.
    assert "gpt-6-luna" not in OpenAIBackend.KNOWN_MODELS
    assert combo.currentIndex() == 0


def test_refresh_never_replaces_explicit_override_absent_from_discovery(qapp, owner_state, monkeypatch):
    tab = _tab(qapp)
    _type_model(tab, "my-private-vision-finetune")
    _openai_models(monkeypatch, "gpt-6-luna", "gpt-6-sol")

    tab._refresh_vision_models()

    assert _shown(tab) == "my-private-vision-finetune"
    assert tab.vision_model_combo.findText("gpt-6-sol") >= 1
    assert _save(tab)["model"] == "my-private-vision-finetune"


def test_discovered_model_can_be_selected_and_persisted(qapp, owner_state, monkeypatch):
    _openai_models(monkeypatch, "gpt-6-luna", "gpt-5.6-luna")
    tab = _tab(qapp)
    tab._refresh_vision_models()

    tab.vision_model_combo.setCurrentIndex(tab.vision_model_combo.findText("gpt-5.6-luna"))
    assert _save(tab)["model"] == "gpt-5.6-luna"
    _restart(monkeypatch)
    assert _shown(_tab(qapp)) == "gpt-5.6-luna"


def test_failed_refresh_keeps_list_and_override_and_reports(qapp, owner_state, monkeypatch):
    tab = _tab(qapp)
    _type_model(tab, "gpt-5.6-luna")
    monkeypatch.setattr(
        requests, "get",
        lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError()),
    )

    tab._refresh_vision_models()

    assert _shown(tab) == "gpt-5.6-luna"
    assert tab.status_lbl.text()
    assert tab.vision_model_combo.findText("gpt-4o") == -1  # offline catalog never shown


def test_provider_change_shows_only_that_providers_discovered_models(qapp, owner_state, monkeypatch):
    _openai_models(monkeypatch, "gpt-6-luna")
    tab = _tab(qapp)
    tab._refresh_vision_models()
    assert tab.vision_model_combo.findText("gpt-6-luna") >= 1

    tab.vision_provider_combo.setCurrentText("gemini")
    assert tab.vision_model_combo.count() == 1

    tab.vision_provider_combo.setCurrentText("openai")
    assert tab.vision_model_combo.findText("gpt-6-luna") >= 1


# ── General "Model Name" row ─────────────────────────────────────────────


def test_custom_model_name_row_hidden_for_openrouter_backend(general_tab):
    """Model Name is the custom/omniroute backend's own saved slot
    (custom_default_model). Under OpenRouter it is not the active model and
    must not be shown as if it were."""
    assert config.LLM_BACKEND == "openrouter"
    assert general_tab.custom_model.text() == "LongCat-2.0"
    assert general_tab.custom_model_widget.isHidden()
    assert general_tab.cloud_model.currentText() == "z-ai/glm-5.3-flash"

    general_tab.backend_combo.setCurrentText("custom")
    assert not general_tab.custom_model_widget.isHidden()
    assert general_tab.custom_model.text() == "LongCat-2.0"


def test_custom_model_name_row_visible_when_custom_backend_active(qapp, owner_state, monkeypatch):
    from core.agent import LuminaAgent
    from ui.settings.general_tab import GeneralTab
    monkeypatch.setattr(config, "LLM_BACKEND", "custom")
    tab = GeneralTab(LuminaAgent(owner=True, channel_id="vision-restore-01-custom"), COLORS)
    assert not tab.custom_model_widget.isHidden()
    assert tab.custom_model.text() == "LongCat-2.0"
