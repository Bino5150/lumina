"""MULTIMODAL-M3-SETTINGS-PROMOTION-01 focused UI contracts.

These tests use real Qt widgets with isolated preferences.  Provider/model
construction is forbidden during passive page construction: Settings exposes
the owner's existing M1/M2 policy, but never executes it.
"""

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QApplication, QComboBox, QLabel, QScrollArea, QTabWidget, QWidget

import config
from core import persistence
from core.capability_router import CANONICAL_CAPABILITIES
from ui.main_window import COLORS
from ui.settings.tts_tab import MultimodalTab


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def config_snapshot():
    keys = (
        "TTS_ENABLED", "TTS_BACKEND", "TTS_HOST", "VOICEBOX_HOST",
        "VOICEBOX_PROFILE", "ELEVENLABS_API_KEY", "STT_ENABLED",
        "STT_BACKEND", "STT_MODEL", "STT_DEVICE", "MULTIMODAL_ROUTES",
        "MULTIMODAL_DISABLED_PROVIDERS", "OPENAI_DEFAULT_MODEL",
    )
    before = {key: getattr(config, key, None) for key in keys}
    yield
    for key, value in before.items():
        setattr(config, key, value)


def _agent():
    return SimpleNamespace(tts=SimpleNamespace(enabled=True))


def test_settings_promotes_one_multimodal_tab_and_removes_old_top_level_stubs(
    qapp, monkeypatch
):
    import ui.settings.panel as panel_module

    class _PlainTab(QWidget):
        backend_connection_changed = Signal()

        def __init__(self, *args, **kwargs):
            super().__init__()

        def refresh_voices(self):
            pass

    class _SignalTab(_PlainTab):
        backend_changed = Signal(str)

    for name in (
        "GeneralTab", "UserProfileTab", "MemoryTab", "KnowledgeTab",
        "SkillsTab", "ToolsTab", "CommunicationsTab", "PersonasTab",
        "ScheduledTasksTab", "AboutTab", "ComingSoonTab",
    ):
        monkeypatch.setattr(panel_module, name, _PlainTab)
    monkeypatch.setattr(panel_module, "MultimodalTab", _SignalTab)

    panel = panel_module.SettingsPanel(SimpleNamespace(), COLORS)
    tabs = panel.findChild(QTabWidget)
    labels = [tabs.tabText(i) for i in range(tabs.count())]

    assert sum("Multimodal" in label for label in labels) == 1
    assert all("TTS" not in label for label in labels)
    assert all("Image Gen" not in label for label in labels)
    assert panel.multimodal_tab is panel.tts_tab
    assert tabs.tabBar().sizeHint().width() <= 990, (
        "Settings tabs overflow the ordinary 1150px app window's ~990px "
        "content width, forcing horizontal tab-strip navigation"
    )


def test_opening_multimodal_exposes_real_route_without_provider_or_model_calls(
    qapp, monkeypatch, config_snapshot
):
    import core.backends.loader as loader_module
    import urllib.request

    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("opening Multimodal attempted provider/model I/O")

    monkeypatch.setattr(loader_module, "get_llm_backend", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(config, "MULTIMODAL_ROUTES", {
        "vision_understanding": {
            "mode": "specialist",
            "specialist": "openai",
            "fallbacks": ["gemini"],
            "quality_floor": "high",
        },
    })
    monkeypatch.setattr(config, "MULTIMODAL_DISABLED_PROVIDERS", ["openrouter"])
    monkeypatch.setattr(config, "OPENAI_DEFAULT_MODEL", "gpt-5.6-luna")

    tab = MultimodalTab(_agent(), COLORS)

    assert calls == []
    assert tab.vision_mode_combo.currentData() == "specialist"
    assert tab.vision_provider_combo.currentText() == "openai"
    assert tab.vision_fallbacks.text() == "gemini"
    assert tab.vision_disabled_providers.text() == "openrouter"
    assert tab.vision_model_combo.itemText(0) == "Provider default — gpt-5.6-luna"
    assert tab.vision_model_combo.currentIndex() == 0
    assert isinstance(tab.vision_model_combo, QComboBox)


def test_multimodal_save_round_trips_speech_and_writes_only_existing_vision_seams(
    qapp, monkeypatch, tmp_path, config_snapshot
):
    prefs_path = tmp_path / "prefs.json"
    monkeypatch.setattr(persistence, "PREFS_PATH", str(prefs_path))
    monkeypatch.setattr(config, "TTS_ENABLED", True)
    monkeypatch.setattr(config, "TTS_BACKEND", "kokoro")
    monkeypatch.setattr(config, "TTS_HOST", "http://speech.test:8880")
    monkeypatch.setattr(config, "STT_ENABLED", True)
    monkeypatch.setattr(config, "STT_BACKEND", "faster-whisper")
    monkeypatch.setattr(config, "STT_MODEL", "small")
    monkeypatch.setattr(config, "STT_DEVICE", "cpu")
    monkeypatch.setattr(config, "MULTIMODAL_ROUTES", {
        "vision_understanding": {
            "mode": "auto",
            "specialist": "openai",
            "fallbacks": ["gemini"],
            "quality_floor": "high",
        },
    })
    monkeypatch.setattr(config, "MULTIMODAL_DISABLED_PROVIDERS", ["qwen"])

    tab = MultimodalTab(_agent(), COLORS)
    tab.vision_mode_combo.setCurrentIndex(tab.vision_mode_combo.findData("specialist"))
    tab.vision_provider_combo.setCurrentText("gemini")
    tab.vision_fallbacks.setText("openai, qwen")
    tab.vision_disabled_providers.setText("deepseek")
    tab._save()

    saved = persistence.load()
    assert saved["tts_enabled"] is True
    assert saved["tts_backend"] == "kokoro"
    assert saved["tts_host"] == "http://speech.test:8880"
    assert saved["stt_enabled"] is True
    assert saved["stt_backend"] == "faster-whisper"
    assert saved["stt_model"] == "small"
    assert saved["stt_device"] == "cpu"
    assert saved["multimodal_routes"] == {
        "vision_understanding": {
            "mode": "specialist",
            "specialist": "gemini",
            "fallbacks": ["openai", "qwen"],
            "quality_floor": "high",
        },
        # image_generation is now always written on every save
        # (MULTIMODAL-PER-CAPABILITY-MODEL-BINDING-01): unlike vision there
        # is no Disabled mode and no provider-global default to fall back
        # to, so Provider/Model always resolve to a concrete selection.
        "image_generation": {
            "mode": "specialist",
            "specialist": "higgsfield",
            "model": "higgsfield-ai/soul/standard",
        },
    }
    assert saved["multimodal_disabled_providers"] == ["deepseek"]
    assert config.MULTIMODAL_ROUTES == saved["multimodal_routes"]
    assert config.MULTIMODAL_DISABLED_PROVIDERS == ["deepseek"]


def test_specialist_mode_requires_a_provider_before_any_live_or_durable_change(
    qapp, monkeypatch, tmp_path, config_snapshot
):
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(config, "MULTIMODAL_ROUTES", {})
    monkeypatch.setattr(config, "MULTIMODAL_DISABLED_PROVIDERS", [])
    original_tts_enabled = config.TTS_ENABLED

    tab = MultimodalTab(_agent(), COLORS)
    tab.enabled_cb.setChecked(not original_tts_enabled)
    tab.vision_mode_combo.setCurrentIndex(tab.vision_mode_combo.findData("specialist"))
    tab.vision_provider_combo.clear()
    tab._save()

    assert config.TTS_ENABLED is original_tts_enabled
    assert config.MULTIMODAL_ROUTES == {}
    assert not os.path.exists(persistence.PREFS_PATH)
    assert "requires a provider" in tab.status_lbl.text()


def test_speech_only_save_preserves_absent_default_disabled_vision_route(
    qapp, monkeypatch, tmp_path, config_snapshot
):
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(config, "MULTIMODAL_ROUTES", {})
    monkeypatch.setattr(config, "MULTIMODAL_DISABLED_PROVIDERS", [])

    tab = MultimodalTab(_agent(), COLORS)
    tab._save()

    # vision_understanding stays absent (untouched, still Disabled/no
    # provider) -- image_generation is always written on every save
    # (MULTIMODAL-PER-CAPABILITY-MODEL-BINDING-01: no Disabled mode, no
    # provider-global default to preserve absence against).
    assert "vision_understanding" not in config.MULTIMODAL_ROUTES
    assert config.MULTIMODAL_ROUTES == {
        "image_generation": {
            "mode": "specialist",
            "specialist": "higgsfield",
            "model": "higgsfield-ai/soul/standard",
        },
    }
    assert persistence.load()["multimodal_routes"] == config.MULTIMODAL_ROUTES


def test_multimodal_page_degrades_vertically_without_horizontal_scrolling(
    qapp, config_snapshot
):
    tab = MultimodalTab(_agent(), COLORS)
    tab.resize(720, 390)
    tab.show()
    qapp.processEvents()

    scroll = tab.findChild(QScrollArea)
    assert scroll.horizontalScrollBar().maximum() == 0
    assert scroll.verticalScrollBar().maximum() > 0
    assert scroll.widget().isAncestorOf(tab.save_btn)
    scroll.ensureWidgetVisible(tab.save_btn)
    qapp.processEvents()
    assert scroll.verticalScrollBar().value() > 0
    tab.close()


def test_m3_does_not_expand_the_frozen_capability_vocabulary():
    assert {cap.value for cap in CANONICAL_CAPABILITIES} == {
        "vision_understanding", "image_generation", "audio_understanding",
        "speech_synthesis", "video_understanding", "video_generation",
    }
    assert "speech_transcription" not in {cap.value for cap in CANONICAL_CAPABILITIES}
