"""BACKEND-CONTRACT-01A endpoint ownership and persistence regressions.

All HTTP is intercepted.  No provider traffic is permitted from this file.
"""

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import requests

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

import config
from core import persistence
import core.secrets as secrets_module
from core.backends.loader import (
    BACKENDS,
    CustomBackend,
    get_backend_endpoint,
    get_llm_backend,
    migrate_legacy_backend_endpoint,
)
from ui.main_window import COLORS
from ui.settings.general_tab import GeneralTab


FIXED_ENDPOINTS = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "kimi": "https://api.moonshot.cn/v1",
    "qwen": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    "anthropic": BACKENDS["anthropic"].default_url,
    "gemini": BACKENDS["gemini"].default_url,
}

CONFIGURABLE_ENDPOINTS = {
    "custom": "https://custom.example/v1",
    "omniroute": "http://omniroute.example/v1",
    "lmstudio": "http://lmstudio.example/v1",
    "ollama": "http://ollama.example/v1",
    "llamacpp": "http://llamacpp.example/v1",
    "vllm": "http://vllm.example/v1",
}


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(secrets_module, "SECRETS_PATH", str(tmp_path / "credentials.json"))
    monkeypatch.setattr(config, "LLM_BACKEND", "openai", raising=False)
    monkeypatch.setattr(config, "LLM_BACKEND_URL", "http://127.0.0.1:9/hostile", raising=False)
    monkeypatch.setattr(config, "BACKEND_ENDPOINTS", dict(CONFIGURABLE_ENDPOINTS), raising=False)
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


@pytest.mark.parametrize("backend, expected", FIXED_ENDPOINTS.items())
def test_fixed_endpoint_is_backend_owned_and_settings_read_only(qapp, backend, expected):
    tab = _settings_tab()
    tab.backend_combo.setCurrentText(backend)

    assert tab.url.text() == expected
    assert tab.url.isEnabled()
    assert tab.url.isReadOnly()
    assert get_backend_endpoint(backend) == expected


@pytest.mark.parametrize("backend, expected", CONFIGURABLE_ENDPOINTS.items())
def test_configurable_endpoint_is_provider_keyed_and_editable(qapp, backend, expected):
    tab = _settings_tab()
    tab.backend_combo.setCurrentText(backend)

    assert tab.url.text() == expected
    assert tab.url.isEnabled()
    assert not tab.url.isReadOnly()


def test_switching_restores_custom_and_omniroute_without_cross_contamination(qapp):
    tab = _settings_tab()

    tab.backend_combo.setCurrentText("custom")
    assert tab.url.text() == CONFIGURABLE_ENDPOINTS["custom"]
    tab.backend_combo.setCurrentText("openai")
    assert tab.url.text() == FIXED_ENDPOINTS["openai"]
    tab.backend_combo.setCurrentText("custom")
    assert tab.url.text() == CONFIGURABLE_ENDPOINTS["custom"]

    tab.backend_combo.setCurrentText("omniroute")
    assert tab.url.text() == CONFIGURABLE_ENDPOINTS["omniroute"]
    tab.backend_combo.setCurrentText("openai")
    tab.backend_combo.setCurrentText("omniroute")
    assert tab.url.text() == CONFIGURABLE_ENDPOINTS["omniroute"]
    assert get_llm_backend("custom").base_url != get_llm_backend("omniroute").base_url


def test_local_endpoint_state_cannot_contaminate_fixed_provider(qapp):
    tab = _settings_tab()
    tab.backend_combo.setCurrentText("lmstudio")
    assert tab.url.text() == CONFIGURABLE_ENDPOINTS["lmstudio"]
    tab.backend_combo.setCurrentText("groq")
    assert tab.url.text() == FIXED_ENDPOINTS["groq"]
    assert get_llm_backend("groq").base_url == FIXED_ENDPOINTS["groq"]


class _FakeResponse:
    status_code = 200
    text = "{}"

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}


@pytest.mark.parametrize(
    "backend,key_attr,key_value",
    [
        ("openai", "OPENAI_API_KEY", "sk-openai-fixed-test"),
        ("groq", "GROQ_API_KEY", "gsk-groq-fixed-test"),
    ],
)
def test_lm_family_fixed_credentials_never_reach_foreign_endpoint(
    monkeypatch, backend, key_attr, key_value
):
    hostile = "http://127.0.0.1:9/credential-sink"
    monkeypatch.setattr(config, key_attr, key_value, raising=False)
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs.get("headers", {})))
        return _FakeResponse()

    monkeypatch.setattr(requests, "post", fake_post)
    instance = get_llm_backend(backend, url=hostile)
    instance.base_url = hostile  # Simulate regressed generic live-apply.
    instance.chat([{"role": "user", "content": "intercepted"}])

    assert len(calls) == 1
    url, headers = calls[0]
    assert url == f"{FIXED_ENDPOINTS[backend]}/chat/completions"
    assert hostile not in url
    assert key_value in headers["Authorization"]


@pytest.mark.parametrize("backend", ["anthropic", "gemini"])
def test_native_backends_remain_structurally_fixed(monkeypatch, backend):
    hostile = "http://127.0.0.1:9/native-credential-sink"
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return _FakeResponse()

    monkeypatch.setattr(requests, "post", fake_post)
    instance = get_llm_backend(backend, url=hostile)
    instance.base_url = hostile
    instance.chat([{"role": "user", "content": "intercepted"}])

    assert len(calls) == 1
    assert calls[0].startswith(FIXED_ENDPOINTS[backend])
    assert hostile not in calls[0]
    assert instance.base_url == FIXED_ENDPOINTS[backend]


def test_legacy_url_seeds_selected_configurable_backend_once(monkeypatch):
    monkeypatch.setattr(config, "LLM_BACKEND", "custom", raising=False)
    monkeypatch.setattr(config, "LLM_BACKEND_URL", "https://legacy.example/v1", raising=False)
    monkeypatch.setattr(config, "BACKEND_ENDPOINTS", {}, raising=False)
    monkeypatch.setattr(config, "BACKEND_ENDPOINTS_MIGRATED", False, raising=False)

    assert migrate_legacy_backend_endpoint() is True
    assert config.BACKEND_ENDPOINTS == {"custom": "https://legacy.example/v1"}
    assert persistence.load()["backend_endpoints_migrated"] is True

    config.LLM_BACKEND_URL = "https://later.example/v1"
    assert migrate_legacy_backend_endpoint() is False
    assert config.BACKEND_ENDPOINTS == {"custom": "https://legacy.example/v1"}


def test_legacy_url_never_seeds_fixed_provider(monkeypatch):
    monkeypatch.setattr(config, "LLM_BACKEND", "openai", raising=False)
    monkeypatch.setattr(config, "BACKEND_ENDPOINTS", {}, raising=False)
    monkeypatch.setattr(config, "BACKEND_ENDPOINTS_MIGRATED", False, raising=False)

    assert migrate_legacy_backend_endpoint() is False
    saved = persistence.load()
    assert saved["backend_endpoints"] == {}
    assert saved["backend_endpoints_migrated"] is True
    assert get_llm_backend("openai").base_url == FIXED_ENDPOINTS["openai"]


def test_provider_keyed_endpoint_wins_during_legacy_migration(monkeypatch):
    persistence.save({
        "llm_backend": "custom",
        "llm_backend_url": "https://legacy.example/v1",
        "backend_endpoints": {"custom": "https://keyed.example/v1"},
    })
    monkeypatch.setattr(config, "LLM_BACKEND", "custom", raising=False)
    monkeypatch.setattr(config, "LLM_BACKEND_URL", "https://legacy.example/v1", raising=False)
    monkeypatch.setattr(config, "BACKEND_ENDPOINTS", {}, raising=False)
    monkeypatch.setattr(config, "BACKEND_ENDPOINTS_MIGRATED", False, raising=False)

    assert migrate_legacy_backend_endpoint() is False
    assert config.BACKEND_ENDPOINTS["custom"] == "https://keyed.example/v1"
    assert get_llm_backend("custom").base_url == "https://keyed.example/v1"


def test_custom_reconstruction_uses_custom_model_and_keeps_omniroute_distinct():
    custom = get_llm_backend("custom")
    omniroute = get_llm_backend("omniroute")

    assert isinstance(custom, CustomBackend)
    assert custom.get_model() == "custom-model"
    assert custom.get_model() != config.DEFAULT_MODEL
    assert omniroute.get_model() == "omniroute-model"
    assert custom.base_url == CONFIGURABLE_ENDPOINTS["custom"]
    assert omniroute.base_url == CONFIGURABLE_ENDPOINTS["omniroute"]
    assert custom.headers["Authorization"] == "Bearer custom-key"
    assert omniroute.headers["Authorization"] == "Bearer omniroute-key"


def test_settings_save_persists_and_reconstructs_custom_without_overwriting_peers(qapp):
    tab = _settings_tab()
    tab.backend_combo.setCurrentText("custom")
    tab.url.setText("https://new-custom.example/v1")
    tab.custom_model.setText("new-custom-model")
    tab.custom_api_key.setText("new-custom-key")

    tab._save()

    saved = persistence.load()
    assert saved["backend_endpoints"]["custom"] == "https://new-custom.example/v1"
    assert saved["backend_endpoints"]["omniroute"] == CONFIGURABLE_ENDPOINTS["omniroute"]
    assert "openai" not in saved["backend_endpoints"]
    assert saved["custom_default_model"] == "new-custom-model"
    assert saved["backend_endpoints_migrated"] is True
    assert tab.agent.llm.name == "custom"
    assert tab.agent.llm.base_url == "https://new-custom.example/v1"
    assert tab.agent.llm.get_model() == "new-custom-model"
    assert tab.agent.llm.headers["Authorization"] == "Bearer new-custom-key"


def test_generic_local_model_behavior_is_unchanged():
    assert get_llm_backend("lmstudio").get_model() == config.DEFAULT_MODEL
    assert get_llm_backend("ollama").get_model() == config.DEFAULT_MODEL


def test_manual_cloud_model_entry_remains_editable(qapp):
    tab = _settings_tab()
    tab.backend_combo.setCurrentText("openai")
    assert tab.cloud_model.isEditable()


def test_save_does_not_post_mutate_reconstructed_fixed_backend(qapp, monkeypatch):
    """Guards removal of the old unconditional ``llm.base_url = ...`` seam."""
    tab = _settings_tab()
    tab.backend_combo.setCurrentText("openai")
    assignments = []

    class ReconstructedFixedBackend:
        @property
        def base_url(self):
            return FIXED_ENDPOINTS["openai"]

        @base_url.setter
        def base_url(self, value):
            assignments.append(value)

    from core.backends import loader as loader_module
    monkeypatch.setattr(loader_module, "get_llm_backend", ReconstructedFixedBackend)

    tab._save()

    assert assignments == []
    assert tab.save_btn.text() == "✓ Saved"
