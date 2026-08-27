"""Global TTS backend lifetime regressions for TTS-01B."""

import os
import sys
import threading
import time
import types
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

import config
import main
import tts.loader as loader_module
from core import persistence, process_manager
from core.agent import LuminaAgent
from ui.main_window import COLORS
from ui.settings.personas_tab import PersonasTab
from ui.settings.tts_tab import TTSTab


class FakeBackend:
    def __init__(self, name="fake"):
        self.name = name
        self.enabled = True
        self.host = "old-host"
        self.api_key = "old-key"
        self.voice_calls = []
        self.speak_calls = []
        self.stop_calls = 0

    def set_enabled(self, enabled):
        self.enabled = enabled

    def set_voice(self, voice):
        self.voice_calls.append(voice)

    def speak(self, text, **kwargs):
        self.speak_calls.append(text)

    def stop(self):
        self.stop_calls += 1

    def test(self):
        return True


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def config_snapshot(tmp_path, monkeypatch):
    keys = [
        "TTS_ENABLED", "TTS_BACKEND", "TTS_HOST", "VOICEBOX_HOST",
        "VOICEBOX_PROFILE", "ELEVENLABS_API_KEY", "STT_ENABLED",
        "STT_BACKEND", "STT_MODEL", "STT_DEVICE",
    ]
    original = {key: getattr(config, key, None) for key in keys}
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    yield
    for key, value in original.items():
        setattr(config, key, value)


def _pump_until(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if condition():
            return True
        time.sleep(0.005)
    QApplication.processEvents()
    return condition()


def _stream_once(agent, content):
    agent.llm = SimpleNamespace(chat_stream=lambda **kwargs: iter([content]))
    added = []
    agent.ctx = SimpleNamespace(add_assistant=added.append)
    agent.on_think_start = lambda step: None
    agent.on_think_token = lambda token: None
    agent.on_think_end = lambda: None
    agent.on_response_token = lambda token: None
    result = LuminaAgent._stream_final(agent, [], [0])
    return result, added


def _tab(monkeypatch, backend, *, enabled=True, persona=None):
    monkeypatch.setattr(config, "TTS_ENABLED", enabled)
    monkeypatch.setattr(config, "TTS_BACKEND", "kokoro")
    monkeypatch.setattr(config, "TTS_HOST", "http://old-host")
    agent = LuminaAgent.__new__(LuminaAgent)
    agent.tts = backend
    agent.current_persona = persona
    agent._persona_speech_suppressed = bool(
        persona is not None
        and "tts_voice" in persona
        and persona["tts_voice"] is None
    )
    return TTSTab(agent, COLORS), agent


def _install_gui_fakes(monkeypatch, *, enabled, backend):
    created_agents = []
    window_tts = []
    loader_calls = []
    unload_calls = []

    class FakeApplication:
        def __init__(self, argv):
            pass

        def setApplicationName(self, name):
            pass

        def setApplicationVersion(self, version):
            pass

        def setFont(self, font):
            pass

        def exec(self):
            return 0

    class FakeFont:
        def __init__(self, name, size):
            pass

        def exactMatch(self):
            return True

    class FakeAgent:
        def __init__(self, tts=None):
            self.tts = tts
            self.on_tool_call = None
            created_agents.append(self)

    class FakeWindow:
        def __init__(self, agent, stt=None):
            window_tts.append(agent.tts)
            self.chat_widget = SimpleNamespace(add_tool_indicator=lambda *args: None)

        def show(self):
            pass

    widgets = types.ModuleType("PySide6.QtWidgets")
    widgets.QApplication = FakeApplication
    gui = types.ModuleType("PySide6.QtGui")
    gui.QFont = FakeFont
    agent_module = types.ModuleType("core.agent")
    agent_module.LuminaAgent = FakeAgent
    window_module = types.ModuleType("ui.main_window")
    window_module.LuminaWindow = FakeWindow
    tts_module = types.ModuleType("tts.loader")

    def fake_get():
        loader_calls.append("load")
        return backend

    tts_module.get_tts_backend = fake_get
    tts_module.unload_tts_backend = lambda value=None: unload_calls.append(value)
    stt_module = types.ModuleType("stt.whisper_bridge")
    stt_module.WhisperBridge = lambda **kwargs: object()

    monkeypatch.setitem(sys.modules, "PySide6.QtWidgets", widgets)
    monkeypatch.setitem(sys.modules, "PySide6.QtGui", gui)
    monkeypatch.setitem(sys.modules, "core.agent", agent_module)
    monkeypatch.setitem(sys.modules, "ui.main_window", window_module)
    monkeypatch.setitem(sys.modules, "tts.loader", tts_module)
    monkeypatch.setitem(sys.modules, "stt.whisper_bridge", stt_module)
    monkeypatch.setattr(config, "TTS_ENABLED", enabled)
    monkeypatch.setattr(config, "STT_ENABLED", False)
    monkeypatch.setattr(process_manager, "shutdown_all", lambda: None)
    return created_agents, window_tts, loader_calls, unload_calls


@pytest.mark.parametrize("enabled", [False, True])
def test_gui_startup_owns_backend_lifetime(monkeypatch, enabled):
    backend = FakeBackend()
    agents, window_tts, loads, unloads = _install_gui_fakes(
        monkeypatch, enabled=enabled, backend=backend
    )

    with pytest.raises(SystemExit) as exc_info:
        main.run_gui()

    assert exc_info.value.code == 0
    assert loads == (["load"] if enabled else [])
    assert window_tts == ([backend] if enabled else [None])
    assert agents[0].tts is None
    assert unloads == ([backend] if enabled else [None])


def test_off_to_on_loads_once_attaches_and_restores_voiced_persona(
    qapp, monkeypatch, config_snapshot
):
    persona = {"tts_voice": "am_adam", "tts_speed": 1.2}
    tab, agent = _tab(monkeypatch, None, enabled=False, persona=persona)
    loaded = FakeBackend()
    calls = []
    monkeypatch.setattr(persistence, "save", lambda prefs: True)
    monkeypatch.setattr(
        loader_module, "get_tts_backend",
        lambda force_reload=False: calls.append(force_reload) or loaded,
    )

    tab.enabled_cb.setChecked(True)
    tab._save()
    assert _pump_until(lambda: not tab._busy)

    assert calls == [False]
    assert agent.tts is loaded
    assert loaded.voice_calls == ["am_adam"]
    assert loaded.speed == pytest.approx(1.2)
    assert agent._persona_speech_suppressed is False
    result, added = _stream_once(agent, "voiced after enable")
    assert result == "voiced after enable"
    assert added == ["voiced after enable"]
    assert loaded.speak_calls == ["voiced after enable"]


def test_on_to_off_detaches_immediately_stops_and_never_loads(
    qapp, monkeypatch, config_snapshot
):
    old = FakeBackend()
    persona = {"tts_voice": "am_adam"}
    tab, agent = _tab(monkeypatch, old, enabled=True, persona=persona)
    loads = []
    monkeypatch.setattr(persistence, "save", lambda prefs: True)
    def forbidden_reload(force_reload=False):
        loads.append(force_reload)
        pytest.fail("disabling TTS must not construct or force-reload a replacement backend")

    monkeypatch.setattr(loader_module, "get_tts_backend", forbidden_reload)

    tab.enabled_cb.setChecked(False)
    tab._save()

    assert agent.tts is None
    assert persona["tts_voice"] == "am_adam"
    assert _pump_until(lambda: not tab._busy)
    assert loads == []
    assert old.stop_calls == 1
    assert old.enabled is False
    result, added = _stream_once(agent, "text remains visible")
    assert result == "text remains visible"
    assert added == ["text remains visible"]
    assert old.speak_calls == []


def test_disabled_backend_changes_persist_without_loading_then_enable_new_choice(
    qapp, monkeypatch, config_snapshot
):
    tab, agent = _tab(monkeypatch, None, enabled=False)
    loaded = FakeBackend()
    calls = []
    monkeypatch.setattr(persistence, "save", lambda prefs: True)
    monkeypatch.setattr(
        loader_module, "get_tts_backend",
        lambda force_reload=False: calls.append((force_reload, config.TTS_BACKEND)) or loaded,
    )

    tab.tts_backend_combo.setCurrentText("voicebox")
    tab.url.setText("http://new-voicebox")
    tab._test_tts()
    assert calls == []
    assert "Enable and save" in tab.status_lbl.text()
    tab._save()
    assert _pump_until(lambda: not tab._busy)
    assert calls == []
    assert agent.tts is None
    assert config.TTS_BACKEND == "voicebox"
    assert config.VOICEBOX_HOST == "http://new-voicebox"

    tab.enabled_cb.setChecked(True)
    tab._save()
    assert _pump_until(lambda: not tab._busy)
    assert calls == [(False, "voicebox")]
    assert agent.tts is loaded


def test_same_backend_live_save_does_not_reconstruct(
    qapp, monkeypatch, config_snapshot
):
    backend = FakeBackend()
    tab, agent = _tab(monkeypatch, backend, enabled=True)
    calls = []
    monkeypatch.setattr(persistence, "save", lambda prefs: True)
    monkeypatch.setattr(
        loader_module, "get_tts_backend",
        lambda force_reload=False: calls.append(force_reload) or FakeBackend(),
    )
    tab.url.setText("http://live-host")

    tab._save()

    assert calls == []
    assert agent.tts is backend
    assert backend.host == "http://live-host"


def test_enabled_backend_change_reconstructs_once(
    qapp, monkeypatch, config_snapshot
):
    old = FakeBackend("old")
    new = FakeBackend("new")
    tab, agent = _tab(monkeypatch, old, enabled=True)
    calls = []
    monkeypatch.setattr(persistence, "save", lambda prefs: True)
    monkeypatch.setattr(
        loader_module, "get_tts_backend",
        lambda force_reload=False: calls.append(force_reload) or new,
    )
    tab.tts_backend_combo.setCurrentText("voicebox")

    tab._save()
    assert _pump_until(lambda: not tab._busy)

    assert calls == [True]
    assert agent.tts is new


def test_silent_persona_stays_silent_across_off_on(
    qapp, monkeypatch, config_snapshot
):
    persona = {"tts_voice": None}
    old = FakeBackend("old")
    tab, agent = _tab(monkeypatch, old, enabled=True, persona=persona)
    new = FakeBackend("new")
    monkeypatch.setattr(persistence, "save", lambda prefs: True)
    monkeypatch.setattr(loader_module, "get_tts_backend", lambda force_reload=False: new)

    tab.enabled_cb.setChecked(False)
    tab._save()
    assert _pump_until(lambda: not tab._busy)
    tab.enabled_cb.setChecked(True)
    tab._save()
    assert _pump_until(lambda: not tab._busy)

    assert agent.tts is new
    assert agent._persona_speech_suppressed is True
    assert persona["tts_voice"] is None
    assert new.voice_calls == []
    result, added = _stream_once(agent, "silent text")
    assert result == "silent text"
    assert added == ["silent text"]
    assert new.speak_calls == []


def test_disabled_persona_voice_catalog_does_not_load_backend(monkeypatch, config_snapshot):
    monkeypatch.setattr(config, "TTS_ENABLED", False)
    monkeypatch.setattr(config, "TTS_BACKEND", "voicebox")
    monkeypatch.setattr(
        loader_module, "get_tts_backend",
        lambda *args, **kwargs: pytest.fail("disabled voice catalog loaded TTS"),
    )

    voices = PersonasTab._fetch_voices(SimpleNamespace())
    assert "am_adam" in voices


def test_loader_unload_uses_stop_waits_for_load_and_releases_cuda(monkeypatch):
    backend = FakeBackend()
    joins = []
    backend._load_thread = SimpleNamespace(
        is_alive=lambda: True,
        join=lambda: joins.append("joined"),
    )
    backend._model = SimpleNamespace(device="cuda")
    cuda_calls = []
    torch_module = types.ModuleType("torch")
    torch_module.cuda = SimpleNamespace(empty_cache=lambda: cuda_calls.append("emptied"))
    monkeypatch.setitem(sys.modules, "torch", torch_module)
    monkeypatch.setattr(loader_module, "_backend_instance", backend)

    assert loader_module.unload_tts_backend() is True
    assert loader_module._backend_instance is None
    assert backend.enabled is False
    assert backend.stop_calls == 1
    assert joins == ["joined"]
    assert backend._model is None
    assert cuda_calls == ["emptied"]
    assert loader_module.unload_tts_backend() is False
