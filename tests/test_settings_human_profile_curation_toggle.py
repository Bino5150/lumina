"""ui/settings.py UserProfileTab -- HUMAN_PROFILE_CURATION_ENABLED toggle
(same bug class as FE-07/DREAM_SWEEP_ENABLED, S52): checkbox gating
dream-sweep profile curation, wired to persist through prefs.json and apply
to the live config module so a Settings-UI change survives restart instead
of reverting.

Real UserProfileTab, offscreen Qt, same convention as
test_settings_subagent_toggles.py. Only prefs.json is redirected to an
isolated temp file. UserProfileTab never touches self.agent beyond storing
it, so a bare SimpleNamespace() stands in for the real LuminaAgent.
"""
import os
import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace
import config
from core import persistence


@pytest.fixture
def isolated_prefs(tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(config, "HUMAN_PROFILE_CURATION_ENABLED", True)
    persistence.save({})


@pytest.fixture
def tab(isolated_prefs):
    from PySide6.QtWidgets import QApplication
    from ui.main_window import COLORS
    from ui.settings import UserProfileTab

    QApplication.instance() or QApplication([])
    fake_agent = SimpleNamespace()
    return UserProfileTab(fake_agent, COLORS)


def test_initial_state_reflects_config_default(tab):
    assert tab.human_profile_curation_cb.isChecked() is True


def test_toggling_off_applies_live_and_persists(tab):
    tab.human_profile_curation_cb.setChecked(False)

    assert config.HUMAN_PROFILE_CURATION_ENABLED is False
    assert persistence.load()["human_profile_curation_enabled"] is False


def test_toggling_on_after_off_applies_live_and_persists(tab):
    tab.human_profile_curation_cb.setChecked(False)
    tab.human_profile_curation_cb.setChecked(True)

    assert config.HUMAN_PROFILE_CURATION_ENABLED is True
    assert persistence.load()["human_profile_curation_enabled"] is True


class _FakeDreamBackend:
    def complete_utility(self, prompt, prefill="", max_tokens=500, temperature=0.3):
        return "- Did a thing"


def _prime_sweep_mocks(monkeypatch, dreaming, chat_id):
    """Shared setup so on_session_idle() reaches the HUMAN_PROFILE_CURATION_ENABLED
    branch at all: sweep enabled, past the token floor, one fresh message, a
    fake LLM backend for the summarization call, and a no-op palace_store so
    the nightstand write doesn't need a real DB. Mirrors the mocking already
    used in tests/test_dreaming.py's own on_session_idle tests."""
    monkeypatch.setattr(dreaming.config, "DREAM_SWEEP_ENABLED", True)
    monkeypatch.setattr(dreaming.config, "DREAM_MIN_TOKENS", 1)
    monkeypatch.setattr(
        dreaming, "load_chat_messages",
        lambda cid: [{"role": "user", "content": "x" * 50, "created_at": "2026-07-09T00:00:00"}],
    )
    monkeypatch.setattr(dreaming, "get_llm_backend", lambda: _FakeDreamBackend())
    monkeypatch.setattr(dreaming, "palace_store", lambda **kw: None)
    dreaming._last_dream_sweep.pop(chat_id, None)


def test_dreaming_sweep_actually_skips_curation_when_toggled_off(tab, monkeypatch):
    """Real behavioral check, not just an attribute assertion: flips the
    checkbox off, then runs the real on_session_idle() sweep gate end to end
    and confirms curate_human_profile() is never invoked -- proving
    core/dreaming.py's own getattr() re-read actually branches on the live
    value, not just that config's attribute changed in isolation."""
    from core import dreaming

    tab.human_profile_curation_cb.setChecked(False)
    _prime_sweep_mocks(monkeypatch, dreaming, chat_id=9001)

    curate_calls = []
    monkeypatch.setattr(dreaming, "curate_human_profile", lambda *a, **k: curate_calls.append(a))

    dreaming.on_session_idle(chat_id=9001)

    assert curate_calls == []


def test_dreaming_sweep_actually_curates_and_persists_when_toggled_on(tab, monkeypatch):
    """Positive-direction counterpart: goes off then on (exercising the
    actual toggle path rather than relying on the config default), then
    confirms the real on_session_idle() sweep both calls
    curate_human_profile() and writes the result to prefs.json -- end-to-end
    proof the checkbox flip changes real sweep behavior, not just the
    config attribute."""
    from core import dreaming

    tab.human_profile_curation_cb.setChecked(False)
    tab.human_profile_curation_cb.setChecked(True)
    _prime_sweep_mocks(monkeypatch, dreaming, chat_id=9002)
    monkeypatch.setattr(dreaming, "curate_human_profile", lambda *a, **k: "- freshly curated note")

    prefs = persistence.load()
    prefs["human_bio"] = "Bino is a software engineer."
    prefs["human_profile_curated"] = ""
    persistence.save(prefs)

    dreaming.on_session_idle(chat_id=9002)

    assert persistence.load()["human_profile_curated"] == "- freshly curated note"


def test_restart_survives_via_prefs_not_reverting_to_shipped_default(isolated_prefs, monkeypatch):
    """MB-24-class gotcha check, same as test_settings_subagent_toggles.py's
    test_ui_reflects_prefs_stored_override_not_just_shipped_default: save an
    override, simulate what config.py's module-level `_p.get(...)` line does
    on next launch, then reconstruct the tab and confirm it reflects the
    persisted override -- not config.py's bare True default -- on load."""
    persistence.save({"human_profile_curation_enabled": False})

    reloaded_value = persistence.load().get("human_profile_curation_enabled", True)
    monkeypatch.setattr(config, "HUMAN_PROFILE_CURATION_ENABLED", reloaded_value)

    from PySide6.QtWidgets import QApplication
    from ui.main_window import COLORS
    from ui.settings import UserProfileTab

    QApplication.instance() or QApplication([])
    fake_agent = SimpleNamespace()
    tab = UserProfileTab(fake_agent, COLORS)

    assert tab.human_profile_curation_cb.isChecked() is False
