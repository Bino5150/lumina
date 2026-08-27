"""UI-TRUST-01 regressions: chat readability + truthful connection status.

Slice A -- user-message text is LEFT-aligned inside its right-positioned
bubble (prose, long task blocks, lists, code all scan like a document).

Slice B -- the bottom-left connection status is invalidated immediately on
a settings-driven backend/model change and then re-reported TRUTHFULLY
from the live agent object: green only for a passing health check, red
with the real reason on failure, never stale previous-provider text.

All HTTP is intercepted / constructed objects never dial out. No provider
traffic, no TTS/audio generation.
"""

import os
import types
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QSizePolicy,
    QTextBrowser,
)

import config
from core import persistence
import core.secrets as secrets_module
from core.backends.base import ModelDiscoveryOutcome, ModelDiscoveryResult
from ui.main_window import COLORS, LuminaWindow, StatusBar
from ui.chat_widget import ChatWidget, LiveResponseBubble
from ui.settings.general_tab import GeneralTab


# ── shared isolation (mirrors BACKEND-CONTRACT-01A harness) ───────────────────


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(secrets_module, "SECRETS_PATH", str(tmp_path / "credentials.json"))
    monkeypatch.setattr(config, "LLM_BACKEND", "openai", raising=False)
    monkeypatch.setattr(config, "LLM_BACKEND_URL", "http://127.0.0.1:9/hostile", raising=False)
    monkeypatch.setattr(config, "BACKEND_ENDPOINTS", {}, raising=False)
    monkeypatch.setattr(config, "BACKEND_ENDPOINTS_MIGRATED", True, raising=False)
    monkeypatch.setattr(config, "CUSTOM_DEFAULT_MODEL", "custom-model", raising=False)
    monkeypatch.setattr(config, "OMNIROUTE_DEFAULT_MODEL", "omniroute-model", raising=False)
    monkeypatch.setattr(config, "CUSTOM_API_KEY", "custom-key", raising=False)
    monkeypatch.setattr(config, "OMNIROUTE_API_KEY", "omniroute-key", raising=False)
    monkeypatch.setattr(config, "DEFAULT_MODEL", "local-model", raising=False)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _settings_tab():
    agent = SimpleNamespace(
        owner=True,
        current_persona=None,
        tts=None,
        registry=None,
        llm=SimpleNamespace(_model=None),
        ctx=SimpleNamespace(max_tokens=0, reserve=0, update_system_prompt=lambda _p: None),
    )
    return GeneralTab(agent, COLORS)


class _SignalSpy:
    def __init__(self, signal):
        self.count = 0
        signal.connect(self._bump)

    def _bump(self, *_args):
        self.count += 1


# ═══════════════════════════ Slice A: chat rendering ══════════════════════════


def _last_user_widgets(chat):
    """Return (content_label, role_label, header_row_layout, frame) for the
    most recently added user message."""
    frame = chat.msgs_layout.itemAt(chat.msgs_layout.count() - 2).widget()
    labels = frame.findChildren(QLabel)
    # The content bubble carries the sharp-top-right corner radius signature;
    # stylesheets are color-interpolated so the dict key itself never appears.
    content = [lbl for lbl in labels if "12px 4px" in lbl.styleSheet()]
    assert len(content) == 1, "exactly one user-bubble-styled label expected"
    role = [lbl for lbl in labels if lbl is not content[0]]
    header_row = frame.layout().itemAt(0).layout()
    return content[0], (role[0] if role else None), header_row, frame


def test_user_text_left_aligned_inside_right_positioned_bubble(qapp):
    chat = ChatWidget(COLORS)
    chat.add_user_message("hello there")
    content, role, header_row, _frame = _last_user_widgets(chat)

    # Inner text: left-aligned (the defect repair).
    assert int(content.alignment()) & int(Qt.AlignLeft)
    assert not (int(content.alignment()) & int(Qt.AlignRight))

    # Outer identity: the card keeps its right-side visual distinction --
    # header row pushed right by a leading stretch, right-aligned role
    # label, sharp top-right bubble corner, full-width expanding card.
    assert header_row.itemAt(0).spacerItem() is not None
    assert int(role.alignment()) & int(Qt.AlignRight)
    assert "12px 4px" in content.styleSheet()
    assert content.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Expanding


LONG_TASK_BLOCK = "\n".join(
    f"step {i}: do the thing precisely and verify it" for i in range(30)
)


def test_long_multiline_user_content_reads_top_to_bottom(qapp):
    chat = ChatWidget(COLORS)
    chat.add_user_message(LONG_TASK_BLOCK)
    content, _role, _header, _frame = _last_user_widgets(chat)

    assert int(content.alignment()) & int(Qt.AlignLeft)
    assert content.wordWrap() is True
    assert content.text() == LONG_TASK_BLOCK  # every line survives verbatim


MARKDOWN_PAYLOAD = (
    "plan:\n- alpha\n- beta\n\n```\ncode_line_one()\n    indented_keep()\n```"
)


def test_user_list_and_code_block_structure_preserved(qapp):
    """User messages render as selectable plain text: a left-aligned QLabel
    preserves list dashes and code indentation instead of scattering them
    against the right margin."""
    chat = ChatWidget(COLORS)
    chat.add_user_message(MARKDOWN_PAYLOAD)
    content, _role, _header, _frame = _last_user_widgets(chat)

    assert int(content.alignment()) & int(Qt.AlignLeft)
    assert content.text() == MARKDOWN_PAYLOAD
    assert "- alpha" in content.text()
    assert "    indented_keep()" in content.text()


def test_assistant_rendering_unchanged_by_alignment_fix(qapp):
    bubble = LiveResponseBubble(COLORS)
    bubble.append_response_token("**bold** answer text")
    bubble.finalize()

    browser = bubble.bubble.findChild(QTextBrowser)
    assert browser is not None, "assistant finalize still swaps in a QTextBrowser"
    doc_align = int(browser.document().defaultTextOption().alignment())
    assert doc_align & int(Qt.AlignLeft)
    assert not (doc_align & int(Qt.AlignRight))
    # Assistant bubble keeps its mirrored (sharp top-LEFT) corner styling.
    assert "4px 12px" in bubble.bubble.styleSheet()


# ══════════════════ Slice B: StatusBar semantics (unit level) ═════════════════


class RecordingStatusBar(StatusBar):
    def __init__(self):
        super().__init__()
        self.calls = []

    def set_checking(self, msg="Checking connection..."):
        self.calls.append(("checking", msg))
        super().set_checking(msg)

    def set_connected(self, model):
        self.calls.append(("connected", model))
        super().set_connected(model)

    def set_error(self, msg="LM Studio offline"):
        self.calls.append(("error", msg))
        super().set_error(msg)


def _fake_window(llm):
    win = SimpleNamespace(
        status_bar=RecordingStatusBar(),
        agent=SimpleNamespace(llm=llm),
    )
    # Bind the real method so the slot's internal re-check call resolves.
    win._check_connection = types.MethodType(LuminaWindow._check_connection, win)
    return win


def _drive_transition(old_display, llm):
    """Preset the stale previous-backend state, then run the slot exactly as
    the wired signal would."""
    win = _fake_window(llm)
    win.status_bar.set_connected(old_display)
    win.status_bar.calls.clear()
    LuminaWindow._on_backend_connection_changed(win)
    return win.status_bar


def test_transitional_state_comes_first_and_is_neutral(qapp):
    llm = SimpleNamespace(health_check=lambda: (True, "Configured — glm-5"))
    bar = _drive_transition("Configured — gpt-4o-mini", llm)

    assert bar.calls[0][0] == "checking", "old state invalidated before re-check"
    assert bar.calls[0][1] == "Checking connection..."
    # Transitional state is transient by design: the synchronous re-check
    # immediately replaces it with the live truth (green dot restored).
    assert bar.model_lbl.text() == "Configured — glm-5"
    assert COLORS["success"] in bar.dot.styleSheet()
    assert COLORS["text_dim"] not in bar.dot.styleSheet()


@pytest.mark.parametrize(
    "old_display,new_ok,new_msg,expected_kind",
    [
        # A: OpenAI/Luna -> OpenRouter/GLM
        ("Configured — gpt-4o-mini", True, "Configured — glm-5", "connected"),
        # B: OpenRouter/GLM -> OpenAI/Luna
        ("Configured — glm-5", True, "Configured — gpt-4o-mini", "connected"),
        # D: configurable local -> fixed cloud
        ("Connected — qwopus-4b", True, "Configured — glm-5", "connected"),
        # E: fixed cloud -> local
        ("Configured — glm-5", True, "Connected — qwopus-4b", "connected"),
    ],
)
def test_backend_switch_shows_new_truth_never_old_text(
    qapp, old_display, new_ok, new_msg, expected_kind
):
    bar = _drive_transition(old_display, SimpleNamespace(health_check=lambda: (new_ok, new_msg)))

    assert bar.calls[0][0] == "checking"
    kind, shown = bar.calls[-1]
    assert kind == expected_kind
    assert new_msg.replace("Connected — model: ", "") in bar.model_lbl.text()
    # The previous provider/model text must be gone from the display.
    prev_fragment = old_display.split("—")[-1].strip()
    assert prev_fragment not in bar.model_lbl.text()
    assert COLORS["success"] in bar.dot.styleSheet()


def test_same_backend_model_change_updates_displayed_model(qapp):
    bar = _drive_transition(
        "Configured — glm-5",
        SimpleNamespace(health_check=lambda: (True, "Configured — kimi-k2-thinking")),
    )

    kind, _shown = bar.calls[-1]
    assert kind == "connected"
    assert "kimi-k2-thinking" in bar.model_lbl.text()
    assert "glm-5" not in bar.model_lbl.text()


def test_failed_new_backend_cannot_keep_previous_green_state(qapp):
    llm = SimpleNamespace(
        health_check=lambda: (False, "OPENROUTER_API_KEY not set in config.py")
    )
    bar = _drive_transition("Configured — gpt-4o-mini", llm)

    kind, shown = bar.calls[-1]
    assert kind == "error"
    assert shown == "OPENROUTER_API_KEY not set in config.py"
    assert "gpt-4o-mini" not in bar.model_lbl.text()
    assert COLORS["danger"] in bar.dot.styleSheet()
    assert COLORS["success"] not in bar.dot.styleSheet()


def test_raising_health_check_falls_back_to_configure_hint(qapp):
    def _boom():
        raise ConnectionError("refused")

    bar = _drive_transition("Configured — gpt-4o-mini", SimpleNamespace(health_check=_boom))

    kind, shown = bar.calls[-1]
    assert kind == "error"
    assert "go to Settings" in shown
    assert "gpt-4o-mini" not in bar.model_lbl.text()


def test_switch_a_to_b_to_a_refreshes_every_transition(qapp):
    states = iter([
        (True, "Configured — gpt-4o-mini"),   # back to A
        (True, "Configured — glm-5"),         # B
        (True, "Configured — gpt-4o-mini"),   # A again
    ])
    llm = SimpleNamespace(health_check=lambda: next(states))
    win = _fake_window(llm)
    win.status_bar.set_connected("stale pre-existing text")
    win.status_bar.calls.clear()

    seen_finals = []
    for _ in range(3):
        LuminaWindow._on_backend_connection_changed(win)
        seen_finals.append(win.status_bar.calls[-1])

    assert all(kind == "connected" for kind, _ in seen_finals)
    assert win.status_bar.calls[0][0] == "checking"
    # Each transition began with a fresh invalidation.
    checking_count = sum(1 for c in win.status_bar.calls if c[0] == "checking")
    assert checking_count == 3
    assert "gpt-4o-mini" in win.status_bar.model_lbl.text()


def test_startup_green_path_still_reports_configured_model(qapp):
    win = _fake_window(
        SimpleNamespace(health_check=lambda: (True, "Configured — gpt-4o-mini"))
    )
    LuminaWindow._check_connection(win)

    assert win.status_bar.model_lbl.text() == "Configured — gpt-4o-mini"
    assert COLORS["success"] in win.status_bar.dot.styleSheet()


# ═════════════ Slice B: GeneralTab emission discipline ════════════════════════


def test_save_emits_when_custom_model_changes_within_same_backend(qapp):
    tab = _settings_tab()
    tab.backend_combo.setCurrentText("custom")
    tab._save()  # phase 1: establish custom as the already-live backend
    spy = _SignalSpy(tab.backend_connection_changed)

    tab.custom_model.setText("brand-new-custom-model")
    tab._save()  # phase 2: SAME backend, model-only live swap

    assert spy.count >= 1, "live _model swap must invalidate displayed status"


def test_save_emits_on_cloud_backend_switch(qapp):
    tab = _settings_tab()
    spy = _SignalSpy(tab.backend_connection_changed)

    tab.backend_combo.setCurrentText("openrouter")
    tab.cloud_key.setText("or-key")
    tab.cloud_model.setCurrentText("glm-5")
    tab._save()

    assert spy.count >= 1


def test_full_success_save_emits_exactly_once_per_phase(qapp):
    tab = _settings_tab()
    spy = _SignalSpy(tab.backend_connection_changed)

    tab._save()  # openai -> openai, nothing exotic

    # One post-credential invalidation + one post-reconstruction refresh.
    assert spy.count == 2
    assert tab.save_btn.text() == "✓ Saved"


def test_partial_apply_failure_still_refreshes_status(qapp, monkeypatch):
    from core.backends import loader as loader_module

    def _explode(*_a, **_kw):
        raise RuntimeError("reconstruction refused")

    monkeypatch.setattr(loader_module, "get_llm_backend", _explode)
    tab = _settings_tab()
    spy = _SignalSpy(tab.backend_connection_changed)

    tab._save()

    # Post-credential invalidation + post-partial refresh: the operator must
    # see the NEW state's failure, not the previous backend's green text.
    assert spy.count == 2
    assert "live backend apply failed" in tab.status_lbl.text()


def test_credential_failure_refreshes_nothing_because_nothing_changed(qapp, monkeypatch):
    monkeypatch.setattr(
        secrets_module, "set_secret", lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("no"))
    )
    tab = _settings_tab()
    spy = _SignalSpy(tab.backend_connection_changed)

    tab.backend_combo.setCurrentText("openrouter")
    tab.cloud_key.setText("k")
    tab._save()

    assert spy.count == 0, "live runtime object untouched -> old display stays truthful"
    assert "not fully saved" in tab.status_lbl.text()


class _Caps:
    efforts = ()
    default_effort = None
    mandatory = False

    def validate(self, _value):
        return None


class _ReasoningReadyProbe:
    """Discovery probe double that satisfies the reasoning-row contract
    without any network traffic."""

    def __init__(self, models=("glm-5", "kimi-k2-thinking")):
        self._models = models
        self.name = "openrouter"

    def discover_models(self):
        return ModelDiscoveryResult(
            ModelDiscoveryOutcome.SUCCESS, models=self._models
        )

    def reasoning_capabilities_ready(self, _model):
        return True

    def reasoning_capabilities(self, _model):
        return _Caps()


def test_model_discovery_alone_never_claims_backend_applied(qapp, monkeypatch):
    from core.backends import loader as loader_module

    monkeypatch.setattr(
        loader_module, "get_llm_backend", lambda **_kw: _ReasoningReadyProbe()
    )
    tab = _settings_tab()
    spy = _SignalSpy(tab.backend_connection_changed)

    tab.backend_combo.setCurrentText("openrouter")
    tab._refresh_models()

    assert spy.count == 0, "discovery answers 'what did the provider enumerate', not 'applied'"
    assert "glm-5" in [tab.cloud_model.itemText(i) for i in range(tab.cloud_model.count())]


def test_failed_discovery_also_never_claims_applied(qapp, monkeypatch):
    from core.backends import loader as loader_module

    class _OfflineProbe(_ReasoningReadyProbe):
        def discover_models(self):
            raise RuntimeError("offline")

    monkeypatch.setattr(
        loader_module, "get_llm_backend", lambda **_kw: _OfflineProbe()
    )
    tab = _settings_tab()
    spy = _SignalSpy(tab.backend_connection_changed)

    tab.backend_combo.setCurrentText("openrouter")
    tab._refresh_models()

    assert spy.count == 0


def test_editing_settings_without_saving_changes_nothing(qapp):
    """The no-cancel-needed analogue: selecting a different backend in the
    combo (no Save click) emits nothing anywhere."""
    tab = _settings_tab()
    spy = _SignalSpy(tab.backend_connection_changed)

    tab.backend_combo.setCurrentText("openrouter")
    tab.custom_model.setText("typed-but-not-saved")

    assert spy.count == 0


def test_panel_declares_relay_signal(qapp):
    from ui.settings.panel import SettingsPanel

    assert hasattr(SettingsPanel, "backend_connection_changed"), (
        "SettingsPanel must expose the GeneralTab relay for MainWindow wiring"
    )
