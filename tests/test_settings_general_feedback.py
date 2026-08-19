"""Patch 3A.3B Part B / R4-R8 -- proves GeneralTab's Apply Change and Save
All Settings buttons give truthful semantic feedback instead of the
previous "click and hope" silence.

Real GeneralTab, real LuminaAgent, real persistence.save() against an
isolated prefs.json -- only get_llm_backend and (where a test needs to
force a specific failure) persistence.save/core.secrets.set_secret are
faked/spied, same "fake only the seam under test" approach
test_tts_tab_single_flight.py already established for this exact class of
save-transaction test.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

import config
import core.secrets as secrets_module
from core import persistence
from ui.settings._widgets import ButtonFeedback


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def isolated_prefs(tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(secrets_module, "SECRETS_PATH", str(tmp_path / "credentials.json"))


@pytest.fixture
def _config_snapshot():
    keys = [
        "SYSTEM_PROMPT", "MAX_CONTEXT_TOKENS", "MEMORY_INJECT_LIMIT",
        "TOOL_RESULT_MAX_CHARS", "MAX_TOOL_ITERATIONS", "RESPONSE_RESERVE_TOKENS",
        "CONTEXT_COMPACTION_ENABLED", "CONTEXT_COMPACTION_BATCH_TOKENS",
        "DREAM_SWEEP_ENABLED", "DREAM_IDLE_MINUTES", "SHOW_THINK_BLOCKS",
        "LLM_BACKEND", "LLM_BACKEND_URL", "LM_STUDIO_BASE_URL",
    ]
    original = {k: getattr(config, k, None) for k in keys}
    yield
    for k, v in original.items():
        setattr(config, k, v)


def _make_tab(monkeypatch):
    from core.agent import LuminaAgent
    from ui.main_window import COLORS
    from ui.settings.general_tab import GeneralTab

    monkeypatch.setattr(config, "LLM_BACKEND", "llamacpp", raising=False)
    agent = LuminaAgent(owner=True, channel_id="settings-general-feedback-test")
    return GeneralTab(agent, COLORS)


@pytest.fixture
def tab(qapp, isolated_prefs, _config_snapshot, monkeypatch):
    monkeypatch.setattr(ButtonFeedback, "HOLD_MS", 30)
    return _make_tab(monkeypatch)


def _pump_until(condition, timeout=5.0, interval=0.005):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if condition():
            return True
        time.sleep(interval)
    QApplication.processEvents()
    return condition()


# ── R4/R5: Apply Change ──────────────────────────────────────────────────

def test_r4_apply_with_real_text_updates_context_and_shows_applied(tab):
    tab.prompt.setPlainText("Be extremely concise.")

    tab._on_apply_clicked()

    assert "Be extremely concise." in tab.agent.ctx.system_prompt
    assert tab.apply_btn.text() == "✓ Applied"
    assert _pump_until(lambda: tab.apply_btn.text() == "Apply Change")


def test_r5_apply_with_empty_prompt_is_not_a_false_success(tab):
    tab.prompt.setPlainText("")
    prior_prompt = tab.agent.ctx.system_prompt

    tab._on_apply_clicked()

    assert tab.agent.ctx.system_prompt == prior_prompt, "empty Apply must not touch the live prompt"
    assert tab.apply_btn.text() == "No change"
    assert "✓" not in tab.apply_btn.text(), "empty no-op must not claim success"


def test_apply_exception_is_visible_not_swallowed(tab, monkeypatch):
    tab.prompt.setPlainText("Some real text")
    monkeypatch.setattr(
        tab.agent.ctx, "update_system_prompt",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ctx exploded")),
    )

    tab._on_apply_clicked()

    assert tab.apply_btn.text() == "✗ Failed"
    assert "ctx exploded" in tab.status_lbl.text()
    # Failure must persist past the success-reset interval.
    assert not _pump_until(lambda: tab.apply_btn.text() != "✗ Failed", timeout=0.15)


# ── R6: full success ──────────────────────────────────────────────────────

def test_r6_full_success_saves_and_shows_saved(tab):
    tab.iter_spin.setValue(17)

    tab._save()

    assert tab.save_btn.text() == "✓ Saved"
    assert config.MAX_TOOL_ITERATIONS == 17
    saved = persistence.load()
    assert saved["max_tool_iterations"] == 17
    assert _pump_until(lambda: tab.save_btn.text() == "Save All Settings")


# ── R7: prefs persistence failure ─────────────────────────────────────────

def test_r7_prefs_failure_does_not_claim_nothing_changed(tab, monkeypatch):
    monkeypatch.setattr(persistence, "save", lambda prefs: False)
    tab.iter_spin.setValue(42)
    prior = config.MAX_TOOL_ITERATIONS

    tab._save()

    # Live config really did change even though the write failed.
    assert config.MAX_TOOL_ITERATIONS == 42
    assert config.MAX_TOOL_ITERATIONS != prior
    assert tab.save_btn.text() == "⚠ Not Saved"
    msg = tab.status_lbl.text()
    assert "not fully saved" in msg
    assert "nothing changed" not in msg.lower()
    # Must persist -- not auto-revert like a clean success.
    assert not _pump_until(lambda: tab.save_btn.text() != "⚠ Not Saved", timeout=0.15)


def test_r7_prefs_failure_skips_live_backend_reconstruction(tab, monkeypatch):
    """Mirrors TTSTab's R8 precedent: a failed disk write must not still
    barrel ahead into backend reconstruction -- that's what makes the B/D
    outcomes actually distinguishable instead of always cascading into D."""
    monkeypatch.setattr(persistence, "save", lambda prefs: False)
    calls = []
    from core.backends import loader as loader_module
    monkeypatch.setattr(
        loader_module, "get_llm_backend",
        lambda *a, **k: calls.append(1) or tab.agent.llm,
    )

    tab._save()

    assert calls == [], "backend reconstruction ran despite a persistence failure"


# ── R8: backend-apply failure AFTER successful persistence ──────────────

def test_r8_backend_apply_failure_after_persistence_reports_truthfully(tab, monkeypatch):
    from core.backends import loader as loader_module
    monkeypatch.setattr(
        loader_module, "get_llm_backend",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("backend on fire")),
    )
    tab.iter_spin.setValue(9)

    tab._save()

    assert tab.save_btn.text() == "⚠ Partial"
    msg = tab.status_lbl.text()
    assert "Settings saved" in msg and "backend on fire" in msg
    assert "Save failed" not in msg
    # The durable save really happened despite the live-apply failure.
    saved = persistence.load()
    assert saved["max_tool_iterations"] == 9
    assert config.MAX_TOOL_ITERATIONS == 9
    # Must persist, not auto-revert.
    assert not _pump_until(lambda: tab.save_btn.text() != "⚠ Partial", timeout=0.15)


# ── Credential-write failure (Class C) ────────────────────────────────────
# Corrective pass (3A.3B Correction 1/2): the secret write itself failing
# (nothing durable happened) and a LATER same-call step failing AFTER the
# secret write already succeeded (credential IS durably stored) are
# genuinely different outcomes -- the message must not collapse them into
# one uniform claim. And no exception message body -- however it's
# constructed -- may ever reach button text, status label, or tooltip.

def test_secret_write_itself_failing_does_not_claim_credential_was_stored(tab, monkeypatch):
    """Case A: set_secret() is the call that raises -- credential_written
    stays False, nothing was durably written. The message must (1) identify
    this specifically as a credential-storage failure, (2) acknowledge only
    the earlier LIVE CONFIG mutation as already-changed, and (3) never claim
    the credential itself was stored."""
    monkeypatch.setattr(config, "LLM_BACKEND", "openai", raising=False)
    tab.backend_combo.setCurrentText("openai")
    tab.cloud_key.setText("sk-super-secret-value")

    monkeypatch.setattr(
        secrets_module, "set_secret",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")),
    )

    tab._save()

    assert tab.save_btn.text() == "✗ Failed"
    msg = tab.status_lbl.text()
    # Identifies credential-storage failure specifically.
    assert "Credential storage failed" in msg
    assert "RuntimeError" in msg
    # Acknowledges the earlier live-state mutation -- and that mutation
    # really did happen, proving the claim is factually correct.
    assert "live values may already have changed" in msg
    assert config.LLM_BACKEND == "openai"
    # Must NOT claim the credential itself was stored -- it wasn't.
    assert "was stored" not in msg
    assert "already stored" not in msg
    assert "credentials may" not in msg, "must not imply credentials (plural) may have changed too"
    assert "rolled back" not in msg.lower() and "reverted" not in msg.lower()
    assert "sk-super-secret-value" not in msg
    assert "sk-super-secret-value" not in tab.save_btn.text()
    # The credential really was never durably written.
    from core.secrets import get_secret
    assert get_secret("openai_api_key") is None
    # Prefs must never have been written -- the exception-abort point was
    # before persistence.save() was ever reached.
    assert not os.path.exists(persistence.PREFS_PATH)


def test_secret_succeeds_then_later_same_call_step_fails_reports_credential_stored(tab):
    """Case B: for custom/omniroute, self.agent.llm._model = ... runs in the
    SAME try block immediately after set_secret() already succeeded. If
    that later step raises (e.g. agent.llm is None), credential_written is
    True -- the message must (1) explicitly say the credential was stored,
    (2) identify a LATER live-update failure distinct from credential
    storage, (3) never call it a credential-store error, and (4) still
    convey prefs were not saved."""
    config.LLM_BACKEND = "custom"
    tab.backend_combo.setCurrentText("custom")
    tab.custom_model.setText("mistral-7b-instruct")
    tab.custom_api_key.setText("sk-custom-secret-value")
    tab.agent.llm = None  # attribute-set after set_secret() succeeds raises AttributeError

    tab._save()

    assert tab.save_btn.text() == "✗ Failed"
    msg = tab.status_lbl.text()
    # Explicitly says the credential was stored.
    assert "your credential was stored" in msg
    # Identifies a later live-update failure, distinctly -- not mislabeled
    # as a credential-store error.
    assert "later live update failed" in msg
    assert "AttributeError" in msg
    assert "credential store" not in msg.lower()
    assert "storage failed" not in msg.lower()
    # Still conveys prefs were not saved.
    assert "not fully saved" in msg
    assert "sk-custom-secret-value" not in msg
    assert "sk-custom-secret-value" not in tab.save_btn.text()
    # The secret really was durably written despite the overall failure.
    from core.secrets import get_secret
    assert get_secret("custom_api_key") == "sk-custom-secret-value"
    assert not os.path.exists(persistence.PREFS_PATH)


def test_malicious_exception_embedding_secret_never_surfaces_it(tab, monkeypatch):
    """3A.3B Correction 2: even if the secret store raised an exception
    whose message body literally contains the credential value, that value
    must never reach button text, status label, or tooltip -- only the
    exception TYPE name is ever shown, never str(e)."""
    monkeypatch.setattr(config, "LLM_BACKEND", "openai", raising=False)
    tab.backend_combo.setCurrentText("openai")
    secret_value = "sk-extremely-sensitive-leak-me-not"
    tab.cloud_key.setText(secret_value)

    monkeypatch.setattr(
        secrets_module, "set_secret",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError(f"storage failure for {secret_value}")),
    )

    tab._save()

    assert tab.save_btn.text() == "✗ Failed"
    assert secret_value not in tab.save_btn.text()
    assert secret_value not in tab.status_lbl.text()
    assert secret_value not in (tab.save_btn.toolTip() or "")
    assert "RuntimeError" in tab.status_lbl.text()
