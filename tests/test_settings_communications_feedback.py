"""Patch 3A.3B Part C / R9-R11 -- proves CommunicationsTab's three
previously-silent Save actions (Telegram Save, Discord token Save, Discord
Identity Save) give truthful semantic feedback, and that a bot token is
never echoed back in any visible feedback text.

Real CommunicationsTab, real persistence.save() and real core.personas
save/load against isolated tmp paths -- only the failure seams under test
(persistence.save, core.secrets.set_secret, core.personas.save_persona)
are faked/spied per-test.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication
from types import SimpleNamespace

import config
import core.personas as personas_module
import core.secrets as secrets_module
from core import persistence


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(personas_module, "PERSONAS_DIR", str(tmp_path / "personas"))
    monkeypatch.setattr(personas_module, "DISCORD_TEMPLATE_PATH",
                         str(tmp_path / "personas" / "discord_template.json"))
    monkeypatch.setattr(secrets_module, "SECRETS_PATH", str(tmp_path / "credentials.json"))
    monkeypatch.setattr(config, "TELEGRAM_OWNER_CHAT_ID", None, raising=False)


def _make_tab(monkeypatch):
    from ui.main_window import COLORS
    from ui.settings.communications_tab import CommunicationsTab

    agent = SimpleNamespace()
    return CommunicationsTab(agent, COLORS)


@pytest.fixture
def tab(qapp, isolated_paths, monkeypatch):
    from ui.settings._widgets import ButtonFeedback
    monkeypatch.setattr(ButtonFeedback, "HOLD_MS", 30)
    return _make_tab(monkeypatch)


def _pump_until(condition, timeout=2.0, interval=0.005):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if condition():
            return True
        time.sleep(interval)
    QApplication.processEvents()
    return condition()


# ── R9: Telegram Save ─────────────────────────────────────────────────────

def test_r9_full_success_saves_chat_id_and_token(tab):
    tab.tg_chat_id.setText("123456789")
    tab.tg_token.setText("bot-token-secret")

    tab._save_telegram()

    assert tab.tg_save_btn.text() == "✓ Saved"
    assert persistence.load()["telegram_owner_chat_id"] == "123456789"
    assert config.TELEGRAM_OWNER_CHAT_ID == "123456789"
    from core.secrets import get_secret
    assert get_secret("telegram_bot_token") == "bot-token-secret"
    # Token field is cleared and never shown anywhere.
    assert tab.tg_token.text() == ""
    assert "bot-token-secret" not in tab.tg_save_btn.text()
    assert "bot-token-secret" not in tab.tg_save_status_lbl.text()
    assert _pump_until(lambda: tab.tg_save_btn.text() == "Save")


def test_r9_empty_token_is_not_a_failure_when_chat_id_saves(tab):
    tab.tg_chat_id.setText("987654321")
    tab.tg_token.setText("")

    tab._save_telegram()

    assert tab.tg_save_btn.text() == "✓ Saved"
    assert persistence.load()["telegram_owner_chat_id"] == "987654321"


def test_r9_persistence_failure_reports_truthfully(tab, monkeypatch):
    monkeypatch.setattr(persistence, "save", lambda prefs: False)
    tab.tg_chat_id.setText("111222333")

    tab._save_telegram()

    assert tab.tg_save_btn.text() == "✗ Failed"
    assert "not saved" in tab.tg_save_status_lbl.text().lower()
    # Live config mutation still happened -- must not be hidden by the message.
    assert config.TELEGRAM_OWNER_CHAT_ID == "111222333"


def test_r9_secret_failure_reports_truthfully_and_hides_token(tab, monkeypatch):
    tab.tg_chat_id.setText("444555666")
    tab.tg_token.setText("super-secret-token")
    monkeypatch.setattr(
        secrets_module, "set_secret",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("keychain locked")),
    )

    tab._save_telegram()

    assert tab.tg_save_btn.text() == "✗ Failed"
    msg = tab.tg_save_status_lbl.text()
    # 3A.3B Correction 2: type name only, never str(e).
    assert "RuntimeError" in msg
    assert "keychain locked" not in msg
    assert "super-secret-token" not in msg
    # Chat ID (not a secret) still saved even though the token write failed.
    assert persistence.load()["telegram_owner_chat_id"] == "444555666"


def test_r9_malicious_exception_embedding_token_never_surfaces_it(tab, monkeypatch):
    """3A.3B Correction 2: even an adversarial exception message that
    literally embeds the token value must never reach the button or the
    status label -- only the exception type name is ever shown."""
    tab.tg_chat_id.setText("777888999")
    token_value = "tg-extremely-sensitive-leak-me-not"
    tab.tg_token.setText(token_value)
    monkeypatch.setattr(
        secrets_module, "set_secret",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError(f"storage failure for {token_value}")),
    )

    tab._save_telegram()

    assert tab.tg_save_btn.text() == "✗ Failed"
    assert token_value not in tab.tg_save_btn.text()
    assert token_value not in tab.tg_save_status_lbl.text()
    assert token_value not in (tab.tg_save_btn.toolTip() or "")
    assert "RuntimeError" in tab.tg_save_status_lbl.text()


# ── R10: Discord token Save ────────────────────────────────────────────

def test_r10_real_token_saves_successfully(tab):
    tab.dc_token.setText("discord-bot-token-value")

    tab._save_discord_token()

    assert tab.dc_save_btn.text() == "✓ Saved"
    from core.secrets import get_secret
    assert get_secret("discord_bot_token") == "discord-bot-token-value"
    assert tab.dc_token.text() == ""
    assert "discord-bot-token-value" not in tab.dc_save_btn.text()


def test_r10_empty_field_is_not_a_false_success(tab):
    tab.dc_token.setText("")

    tab._save_discord_token()

    assert tab.dc_save_btn.text() == "No token entered"
    assert "✓" not in tab.dc_save_btn.text()


def test_r10_secret_store_exception_is_visible_and_hides_token(tab, monkeypatch):
    tab.dc_token.setText("another-secret-token")
    monkeypatch.setattr(
        secrets_module, "set_secret",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")),
    )

    tab._save_discord_token()

    assert tab.dc_save_btn.text() == "✗ Failed"
    msg = tab.dc_token_status_lbl.text()
    # 3A.3B Correction 2: type name only, never str(e).
    assert "RuntimeError" in msg
    assert "disk full" not in msg
    assert "another-secret-token" not in msg
    assert "another-secret-token" not in tab.dc_save_btn.text()


def test_r10_malicious_exception_embedding_token_never_surfaces_it(tab, monkeypatch):
    """3A.3B Correction 2: even an adversarial exception message that
    literally embeds the token value must never reach the button or the
    status label -- only the exception type name is ever shown."""
    token_value = "dc-extremely-sensitive-leak-me-not"
    tab.dc_token.setText(token_value)
    monkeypatch.setattr(
        secrets_module, "set_secret",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError(f"storage failure for {token_value}")),
    )

    tab._save_discord_token()

    assert tab.dc_save_btn.text() == "✗ Failed"
    assert token_value not in tab.dc_save_btn.text()
    assert token_value not in tab.dc_token_status_lbl.text()
    assert token_value not in (tab.dc_save_btn.toolTip() or "")
    assert "RuntimeError" in tab.dc_token_status_lbl.text()


# ── R11: Discord Identity Save ───────────────────────────────────────────

def test_r11_successful_write_shows_saved(tab):
    tab.dc_name.setText("Test Bot")
    tab.dc_tagline.setText("A test tagline")
    tab.dc_prompt.setPlainText("You are a test bot.")

    tab._save_discord_identity()

    assert tab.dc_identity_save_btn.text() == "✓ Saved"
    with open(personas_module.DISCORD_TEMPLATE_PATH) as f:
        data = json.load(f)
    assert data["name"] == "Test Bot"
    assert data["tagline"] == "A test tagline"


def test_r11_write_exception_shows_failure(tab, monkeypatch):
    monkeypatch.setattr(
        personas_module, "save_persona",
        lambda *a, **k: (_ for _ in ()).throw(OSError("permission denied")),
    )
    tab.dc_name.setText("Whatever")

    tab._save_discord_identity()

    assert tab.dc_identity_save_btn.text() == "✗ Failed"
    assert "permission denied" in tab.dc_identity_status_lbl.text()
