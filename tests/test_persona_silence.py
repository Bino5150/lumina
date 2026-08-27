"""Regression coverage for the explicit per-persona silence contract."""

import json
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QFileDialog

import config
import core.personas as personas_module
from core.agent import LuminaAgent
from tools.registry import ToolRegistry


class FakeTTS:
    def __init__(self, enabled=True, profile_backend=False):
        self.enabled = enabled
        self.profile_backend = profile_backend
        self.voice_calls = []
        self.profile_calls = []
        self.speak_calls = []
        self.speed = 1.0
        self.pitch = 1.0
        self.volume = 1.0

    def set_voice(self, voice):
        self.voice_calls.append(voice)

    def speak(self, *args, **kwargs):
        self.speak_calls.append((args, kwargs))


class FakeProfileTTS:
    def __init__(self):
        self.enabled = True
        self.profile_calls = []

    def set_profile(self, profile):
        self.profile_calls.append(profile)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def tab(qapp, tmp_path, monkeypatch):
    from ui.main_window import COLORS
    from ui.settings.personas_tab import PersonasTab

    monkeypatch.setattr(personas_module, "PERSONAS_DIR", str(tmp_path / "personas"))
    monkeypatch.setattr(
        PersonasTab, "_fetch_voices", lambda self: ["af_bella", "am_adam"]
    )
    agent = SimpleNamespace(registry=ToolRegistry(), tts=FakeTTS())
    return PersonasTab(agent, COLORS)


def _persona(name="Test", **overrides):
    data = {
        "name": name,
        "tagline": "",
        "avatar": "",
        "system_prompt": "",
        "tools_profile": "",
        "tts_voice": "af_bella",
        "tts_speed": 1.0,
        "tts_pitch": 1.0,
        "tts_volume": 1.0,
        "description": "",
        "protected": False,
    }
    data.update(overrides)
    return data


def _write_and_select(tab, data):
    path = os.path.join(personas_module.PERSONAS_DIR, f"{data['name']}.json")
    personas_module.save_persona(path, data)
    tab._load_personas()
    loaded = next(p for p in tab._personas if p["_file"] == path)
    tab._select_persona(loaded)
    return path


def _agent_with_tts(tts):
    return SimpleNamespace(tts=tts, _persona_speech_suppressed=False)


def _stream_once(agent, content="Visible response"):
    agent.llm = SimpleNamespace(chat_stream=lambda **kwargs: iter([content]))
    added = []
    agent.ctx = SimpleNamespace(add_assistant=added.append)
    agent.on_think_start = lambda step: None
    agent.on_think_token = lambda token: None
    agent.on_think_end = lambda: None
    agent.on_response_token = lambda token: None
    result = LuminaAgent._stream_final(agent, [], [0])
    return result, added


def test_voice_combo_and_explicit_null_save_reload_round_trip(tab):
    path = _write_and_select(tab, _persona(tts_voice=None))

    assert tab.rp_voice.itemText(0) == "None"
    assert tab.rp_voice.currentText() == "None"
    assert tab.rp_voice.currentData() is None
    assert not tab.rp_test_tts_btn.isEnabled()
    assert tab._collect_persona_data()["tts_voice"] is None

    tab._save_persona()
    with open(path, encoding="utf-8") as handle:
        assert json.load(handle)["tts_voice"] is None
    tab._load_personas()
    reloaded = next(p for p in tab._personas if p["_file"] == path)
    tab._select_persona(reloaded)
    assert tab.rp_voice.currentData() is None


def test_legacy_missing_voice_is_not_silent(tab):
    data = _persona(name="Legacy")
    del data["tts_voice"]
    _write_and_select(tab, data)

    # config.TTS_VOICE may itself be an old catalog value; the historical UI
    # fallback in that case is the first real backend voice.
    assert tab.rp_voice.currentText() == "af_bella"
    assert tab.rp_voice.currentData() == "af_bella"
    assert tab.rp_test_tts_btn.isEnabled()


def test_unknown_voice_preserves_first_real_voice_compatibility(tab):
    _write_and_select(tab, _persona(name="Unknown", tts_voice="old_catalog_voice"))

    assert tab.rp_voice.currentText() == "af_bella"
    assert tab.rp_voice.currentData() == "af_bella"


def test_explicit_voice_round_trips_unchanged(tab):
    path = _write_and_select(tab, _persona(name="Voiced", tts_voice="am_adam"))
    assert tab.rp_voice.currentData() == "am_adam"
    tab._save_persona()
    with open(path, encoding="utf-8") as handle:
        assert json.load(handle)["tts_voice"] == "am_adam"


def test_null_survives_duplicate_and_export(tab, tmp_path, monkeypatch):
    _write_and_select(tab, _persona(name="Silent", tts_voice=None))
    tab._duplicate()
    duplicate = next(p for p in personas_module.list_personas() if p["name"] == "Silent (copy)")
    assert duplicate["tts_voice"] is None

    export_path = tmp_path / "silent-export.json"
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName",
        lambda *args, **kwargs: (str(export_path), "JSON files (*.json)"),
    )
    monkeypatch.setattr(
        "ui.settings.personas_tab.QMessageBox.information", lambda *args, **kwargs: None
    )
    tab._export()
    with open(export_path, encoding="utf-8") as handle:
        assert json.load(handle)["tts_voice"] is None


def test_none_test_voice_never_uses_previous_voice_and_real_voice_restores(tab):
    _write_and_select(tab, _persona(tts_voice=None))
    tts = tab.agent.tts

    tab._test_tts()
    assert tts.voice_calls == []
    assert tts.speak_calls == []

    tab.rp_voice.setCurrentText("am_adam")
    assert tab.rp_test_tts_btn.isEnabled()
    tab._test_tts()
    assert tts.voice_calls == ["am_adam"]
    assert len(tts.speak_calls) == 1


def test_silent_persona_does_not_mutate_backend_or_global_tts(monkeypatch):
    tts = FakeTTS(enabled=True)
    agent = _agent_with_tts(tts)
    global_enabled = config.TTS_ENABLED

    LuminaAgent.apply_persona(agent, {"tts_voice": None})

    assert agent._persona_speech_suppressed is True
    assert tts.voice_calls == []
    assert tts.enabled is True
    assert config.TTS_ENABLED is global_enabled


def test_silent_persona_never_reaches_profile_setter():
    tts = FakeProfileTTS()
    agent = _agent_with_tts(tts)

    LuminaAgent.apply_persona(agent, {"tts_voice": None})
    LuminaAgent.apply_persona(agent, {"tts_voice": None})

    assert tts.profile_calls == []
    assert agent._persona_speech_suppressed is True


def test_silent_final_response_stays_visible_without_speech():
    tts = FakeTTS(enabled=True)
    agent = _agent_with_tts(tts)
    LuminaAgent.apply_persona(agent, {"tts_voice": None})

    result, added = _stream_once(agent)

    assert result == "Visible response"
    assert added == ["Visible response"]
    assert tts.speak_calls == []
    assert tts.enabled is True


def test_voiced_silent_voiced_transitions_apply_voice_and_resume_speech():
    tts = FakeTTS(enabled=True)
    agent = _agent_with_tts(tts)

    LuminaAgent.apply_persona(agent, {"tts_voice": "af_bella"})
    _stream_once(agent, "voice one")
    LuminaAgent.apply_persona(agent, {"tts_voice": None})
    _stream_once(agent, "silent")
    LuminaAgent.apply_persona(agent, {"tts_voice": "am_adam"})
    _stream_once(agent, "voice two")

    assert tts.voice_calls == ["af_bella", "am_adam"]
    assert [call[0][0] for call in tts.speak_calls] == ["voice one", "voice two"]
    assert agent._persona_speech_suppressed is False
    assert tts.enabled is True


def test_silent_to_legacy_clears_suppression_without_voice_mutation():
    tts = FakeTTS()
    agent = _agent_with_tts(tts)
    LuminaAgent.apply_persona(agent, {"tts_voice": None})

    LuminaAgent.apply_persona(agent, {"name": "Legacy"})
    _stream_once(agent, "legacy speaks")

    assert agent._persona_speech_suppressed is False
    assert tts.voice_calls == []
    assert len(tts.speak_calls) == 1


def test_silent_to_default_clears_suppression():
    tts = FakeTTS()
    agent = _agent_with_tts(tts)
    LuminaAgent.apply_persona(agent, {"tts_voice": None})

    LuminaAgent.clear_persona_speech_suppression(agent)
    _stream_once(agent, "default speaks")

    assert agent._persona_speech_suppressed is False
    assert len(tts.speak_calls) == 1


def test_no_persona_selector_clears_suppression():
    calls = []
    fake_window = SimpleNamespace(
        persona_combo=SimpleNamespace(itemData=lambda idx: None),
        agent=SimpleNamespace(clear_persona_speech_suppression=lambda: calls.append("cleared")),
    )
    from ui.main_window import LuminaWindow

    LuminaWindow._on_persona_selected(fake_window, 0)
    assert calls == ["cleared"]


def test_replay_obeys_active_persona_silence(qapp):
    from ui.main_window import COLORS
    from ui.chat_widget import MetricsBar

    tts = FakeTTS()
    allowed = {"value": False}
    metrics = MetricsBar(
        COLORS, tts=tts, tts_speech_allowed=lambda: allowed["value"]
    )
    metrics._response_text = "Replay me"

    metrics._replay()
    assert tts.speak_calls == []
    allowed["value"] = True
    metrics._replay()
    assert len(tts.speak_calls) == 1
