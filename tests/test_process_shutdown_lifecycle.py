"""CODING-04A2-B1 ordinary application-lifecycle process cleanup.

These tests exercise main.py's real GUI/CLI ownership boundaries.  The
process manager's whole-tree shutdown behavior itself is proven with real
children in test_process_manager.py; here lightweight front-end fakes keep the
assertion focused on whether every exit path actually invokes that core.
"""

import builtins
import sys
import types

import pytest

import main
from core import emergency_stop, process_manager
from core import reasoning_preferences
from process_test_helpers import sleeping_python_command


@pytest.fixture(autouse=True)
def _clean_process_state():
    process_manager._reset_for_tests()
    emergency_stop._reset_for_tests()
    yield
    process_manager._reset_for_tests()
    emergency_stop._reset_for_tests()


class _FakeAgent:
    def __init__(self, **kwargs):
        self.llm = object()
        self.registry = object()
        self.owner = True
        self.on_tool_call = kwargs.get("on_tool_call")

    def test_connection(self):
        return "connected"

    def get_token_count(self):
        return 0

    def chat(self, text, reasoning_effort=None):
        return "reply"


def _install_fake_agent(monkeypatch, agent_class=_FakeAgent):
    fake_module = types.ModuleType("core.agent")
    fake_module.LuminaAgent = agent_class
    monkeypatch.setitem(sys.modules, "core.agent", fake_module)
    monkeypatch.setattr(
        reasoning_preferences,
        "resolve_reasoning_effort",
        lambda llm: None,
    )


@pytest.mark.parametrize("exit_kind", ["quit", "eof", "keyboard", "chat_error"])
def test_cli_every_exit_path_requests_normal_process_shutdown(monkeypatch, exit_kind):
    calls = []
    _install_fake_agent(monkeypatch)
    monkeypatch.setattr(process_manager, "shutdown_all", lambda: calls.append("shutdown"))

    if exit_kind == "quit":
        fake_input = lambda prompt: "quit"
    elif exit_kind == "eof":
        def fake_input(prompt):
            raise EOFError
    elif exit_kind == "keyboard":
        def fake_input(prompt):
            raise KeyboardInterrupt
    else:
        fake_input = lambda prompt: "hello"

        def fail_chat(self, text, reasoning_effort=None):
            raise RuntimeError("adjacent CLI failure")

        monkeypatch.setattr(_FakeAgent, "chat", fail_chat)

    monkeypatch.setattr(builtins, "input", fake_input)
    if exit_kind == "chat_error":
        with pytest.raises(RuntimeError, match="adjacent CLI failure"):
            main.run_cli()
    else:
        main.run_cli()

    assert calls == ["shutdown"]
    assert emergency_stop.is_latched() is False


def test_cli_lifecycle_really_shutdowns_a_managed_child(monkeypatch, tmp_path):
    launched = []
    working_dir = tmp_path / "CLI lifecycle working directory"
    working_dir.mkdir()

    class LaunchingAgent(_FakeAgent):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            launched.append(process_manager.launch(
                sleeping_python_command(), cwd=str(working_dir)
            ))

    _install_fake_agent(monkeypatch, LaunchingAgent)
    monkeypatch.setattr(builtins, "input", lambda prompt: "quit")
    main.run_cli()

    snap = process_manager.get_job_snapshot(launched[0])
    assert snap["status"] == "shutdown_killed"
    assert emergency_stop.is_latched() is False


def _install_fake_gui_modules(monkeypatch, exec_behavior):
    class FakeApplication:
        def __init__(self, argv):
            self.argv = argv

        def setApplicationName(self, name):
            pass

        def setApplicationVersion(self, version):
            pass

        def setFont(self, font):
            pass

        def exec(self):
            if isinstance(exec_behavior, BaseException):
                raise exec_behavior
            return exec_behavior

    class FakeFont:
        def __init__(self, name, size):
            self.name = name

        def exactMatch(self):
            return True

    class FakeWindow:
        def __init__(self, agent, stt=None):
            self.chat_widget = types.SimpleNamespace(add_tool_indicator=lambda *args: None)

        def show(self):
            pass

    widgets = types.ModuleType("PySide6.QtWidgets")
    widgets.QApplication = FakeApplication
    gui = types.ModuleType("PySide6.QtGui")
    gui.QFont = FakeFont
    agent_module = types.ModuleType("core.agent")
    agent_module.LuminaAgent = _FakeAgent
    window_module = types.ModuleType("ui.main_window")
    window_module.LuminaWindow = FakeWindow
    tts_module = types.ModuleType("tts.loader")
    tts_module.get_tts_backend = lambda: object()
    stt_module = types.ModuleType("stt.whisper_bridge")
    stt_module.WhisperBridge = lambda **kwargs: object()

    monkeypatch.setitem(sys.modules, "PySide6.QtWidgets", widgets)
    monkeypatch.setitem(sys.modules, "PySide6.QtGui", gui)
    monkeypatch.setitem(sys.modules, "core.agent", agent_module)
    monkeypatch.setitem(sys.modules, "ui.main_window", window_module)
    monkeypatch.setitem(sys.modules, "tts.loader", tts_module)
    monkeypatch.setitem(sys.modules, "stt.whisper_bridge", stt_module)


def test_gui_normal_event_loop_exit_requests_shutdown(monkeypatch):
    calls = []
    _install_fake_gui_modules(monkeypatch, 23)
    monkeypatch.setattr(process_manager, "shutdown_all", lambda: calls.append("shutdown"))

    with pytest.raises(SystemExit) as exc_info:
        main.run_gui()

    assert exc_info.value.code == 23
    assert calls == ["shutdown"]
    assert emergency_stop.is_latched() is False


def test_gui_adjacent_event_loop_failure_cannot_skip_shutdown(monkeypatch):
    calls = []
    _install_fake_gui_modules(monkeypatch, RuntimeError("adjacent GUI failure"))
    monkeypatch.setattr(process_manager, "shutdown_all", lambda: calls.append("shutdown"))

    with pytest.raises(RuntimeError, match="adjacent GUI failure"):
        main.run_gui()

    assert calls == ["shutdown"]
    assert emergency_stop.is_latched() is False
