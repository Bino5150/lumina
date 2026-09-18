"""MULTIMODAL-M4-HIGGSFIELD-CREDENTIALS-SURFACE-01 focused UI contracts.

Real MultimodalTab, real core.secrets against an isolated credentials.json --
only the failure seam under test (core.secrets.set_secret/set_secrets) is
faked per-test. No network I/O, no live Higgsfield request anywhere here.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QLineEdit, QPushButton

import config
import core.secrets as secrets_module
from core import persistence


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(secrets_module, "SECRETS_PATH", str(tmp_path / "credentials.json"))


def _agent():
    return SimpleNamespace(tts=SimpleNamespace(enabled=True))


@pytest.fixture
def tab(qapp, isolated_paths, monkeypatch):
    from ui.settings._widgets import ButtonFeedback
    from ui.settings.tts_tab import MultimodalTab
    from ui.main_window import COLORS

    monkeypatch.setattr(ButtonFeedback, "HOLD_MS", 30)
    return MultimodalTab(_agent(), COLORS)


# ── 1. Fields exist in the intended settings surface ─────────────────────

def test_higgsfield_credential_fields_exist_on_multimodal_tab(tab):
    assert isinstance(tab.hf_key_id, QLineEdit)
    assert isinstance(tab.hf_key_secret, QLineEdit)
    assert isinstance(tab.hf_save_btn, QPushButton)
    # Masked / secret-safe entry -- never plain-text echo.
    assert tab.hf_key_id.echoMode() == QLineEdit.EchoMode.Password
    assert tab.hf_key_secret.echoMode() == QLineEdit.EchoMode.Password


# ── 2. Saving writes the exact identifiers HiggsfieldAdapter expects ─────

def test_save_writes_exact_secret_identifiers_and_adapter_recognizes_them(tab):
    tab.hf_key_id.setText("hf-key-id-value")
    tab.hf_key_secret.setText("hf-key-secret-value")

    tab._save_higgsfield_credentials()

    from core.secrets import get_secret
    assert get_secret("higgsfield_key_id") == "hf-key-id-value"
    assert get_secret("higgsfield_key_secret") == "hf-key-secret-value"

    # End-to-end proof against the real adapter's own credential lookup --
    # no network call, _default_transport() only reads secrets and builds a
    # local object.
    from core.higgsfield_adapter import _default_transport
    transport = _default_transport()
    assert "REDACTED" in repr(transport)


# ── 3. Stored only through the existing secrets mechanism ────────────────

def test_credentials_never_land_in_prefs_json(tab):
    tab.hf_key_id.setText("only-in-secrets-id")
    tab.hf_key_secret.setText("only-in-secrets-secret")

    tab._save_higgsfield_credentials()

    assert os.path.exists(secrets_module.SECRETS_PATH)
    with open(secrets_module.SECRETS_PATH) as f:
        stored = json.load(f)
    assert stored["higgsfield_key_id"] == "only-in-secrets-id"
    assert stored["higgsfield_key_secret"] == "only-in-secrets-secret"

    prefs = persistence.load()
    assert "higgsfield_key_id" not in prefs
    assert "higgsfield_key_secret" not in prefs
    assert "only-in-secrets-id" not in json.dumps(prefs)
    assert "only-in-secrets-secret" not in json.dumps(prefs)


# ── 4. Existing credentials are never rendered back as plaintext ─────────

def test_existing_credential_never_populates_the_field(qapp, isolated_paths, monkeypatch):
    from core.secrets import set_secret
    set_secret("higgsfield_key_id", "super-secret-id-value")
    set_secret("higgsfield_key_secret", "super-secret-secret-value")

    from ui.settings._widgets import ButtonFeedback
    from ui.settings.tts_tab import MultimodalTab
    from ui.main_window import COLORS
    monkeypatch.setattr(ButtonFeedback, "HOLD_MS", 30)

    tab = MultimodalTab(_agent(), COLORS)

    assert tab.hf_key_id.text() == ""
    assert tab.hf_key_secret.text() == ""
    assert "super-secret-id-value" not in tab.hf_key_id.placeholderText()
    assert "super-secret-secret-value" not in tab.hf_key_secret.placeholderText()
    assert tab.hf_key_id.placeholderText() == "•••• configured"
    assert tab.hf_key_secret.placeholderText() == "•••• configured"


def test_field_is_cleared_after_a_successful_save(tab):
    tab.hf_key_id.setText("clear-me-id")
    tab.hf_key_secret.setText("clear-me-secret")

    tab._save_higgsfield_credentials()

    assert tab.hf_key_id.text() == ""
    assert tab.hf_key_secret.text() == ""


# ── 5. Configured / not-configured status is truthful ────────────────────

def test_status_is_not_configured_before_anything_is_saved(tab):
    assert tab.hf_status_lbl.text() == "API credentials: Not configured"


def test_status_is_configured_only_once_both_values_are_present(tab):
    tab.hf_key_id.setText("id-only")
    tab._save_higgsfield_credentials()
    assert tab.hf_status_lbl.text() == "API credentials: Not configured"

    tab.hf_key_secret.setText("secret-only")
    tab._save_higgsfield_credentials()
    assert tab.hf_status_lbl.text() == "API credentials: Configured"


# ── 6. Unrelated Multimodal saves never erase Higgsfield credentials ─────

def test_unrelated_save_settings_click_does_not_touch_higgsfield_credentials(
    tab, monkeypatch
):
    from core.secrets import set_secret, get_secret
    set_secret("higgsfield_key_id", "preexisting-id")
    set_secret("higgsfield_key_secret", "preexisting-secret")

    monkeypatch.setattr(config, "MULTIMODAL_ROUTES", {})
    monkeypatch.setattr(config, "MULTIMODAL_DISABLED_PROVIDERS", [])
    monkeypatch.setattr(config, "TTS_ENABLED", False)

    # The tab-wide Save Settings action (TTS/STT/vision route) -- opening
    # and saving unrelated fields must not read or write these two secrets
    # at all.
    tab._save()

    assert get_secret("higgsfield_key_id") == "preexisting-id"
    assert get_secret("higgsfield_key_secret") == "preexisting-secret"


# ── 7. Partial/failed writes do not falsely report a configured pair ─────

def test_failed_write_reports_failure_and_status_stays_truthful(tab, monkeypatch):
    tab.hf_key_id.setText("will-not-be-stored-id")
    tab.hf_key_secret.setText("will-not-be-stored-secret")
    monkeypatch.setattr(
        secrets_module, "set_secrets",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")),
    )

    tab._save_higgsfield_credentials()

    assert tab.hf_save_btn.text() == "✗ Failed"
    assert tab.hf_status_lbl.text() == "API credentials: Not configured"
    from core.secrets import get_secret
    assert get_secret("higgsfield_key_id") is None
    assert get_secret("higgsfield_key_secret") is None


def test_empty_fields_are_a_no_op_not_a_false_success(tab):
    tab.hf_key_id.setText("")
    tab.hf_key_secret.setText("")

    tab._save_higgsfield_credentials()

    assert tab.hf_save_btn.text() == "No credentials entered"
    assert "✓" not in tab.hf_save_btn.text()
    assert tab.hf_status_lbl.text() == "API credentials: Not configured"


# ── 8. Secret values never appear in logs/errors/preferences ─────────────

def test_malicious_exception_embedding_secret_never_surfaces_it(tab, monkeypatch):
    """Even an adversarial exception message that literally embeds the
    credential value must never reach any visible label -- only the
    exception type name is ever shown (safe_error_detail() convention,
    already proven for Telegram/Discord in
    test_settings_communications_feedback.py)."""
    secret_value = "hf-extremely-sensitive-leak-me-not"
    tab.hf_key_id.setText("some-id")
    tab.hf_key_secret.setText(secret_value)
    monkeypatch.setattr(
        secrets_module, "set_secrets",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError(f"storage failure for {secret_value}")),
    )

    tab._save_higgsfield_credentials()

    assert tab.hf_save_btn.text() == "✗ Failed"
    assert secret_value not in tab.hf_save_btn.text()
    assert secret_value not in tab.hf_save_status_lbl.text()
    assert secret_value not in (tab.hf_save_btn.toolTip() or "")
    assert "RuntimeError" in tab.hf_save_status_lbl.text()
    assert "disk full" not in tab.hf_save_status_lbl.text()


# ── 9 & 10. Existing multimodal / secrets behavior remains intact ────────
# Covered by running the existing test_settings_multimodal_m3.py and
# test_multimodal_m4_*.py suites unmodified alongside this file (see
# verification notes in project-evidence/) -- not duplicated here.
