"""BACKEND-CONTRACT-01B truthful model discovery regressions.

Every provider request is hermetically stubbed. No live provider traffic is
allowed from this module.
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import requests
import pytest

import config
from core import persistence
from core.backends.base import ModelDiscoveryOutcome
from core.backends.openai_backend import OpenAIBackend
from core.backends.deepseek import DeepSeekBackend
from core.backends.groq import GroqBackend
from core.backends.kimi import KimiBackend
from core.backends.qwen import QwenBackend
from core.backends.openrouter import OpenRouterBackend
from core.backends.anthropic_backend import AnthropicBackend
from core.backends.gemini_backend import GeminiBackend
from core.backends.lmstudio import LMStudioBackend
from core.backends.loader import CustomBackend
from core.backends.omniroute import OmniRouteBackend


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError()

    def json(self):
        return self.payload


def openai_payload(*model_ids):
    return {"data": [{"id": model_id} for model_id in model_ids]}


def test_openai_live_discovery_returns_luna_and_future_model(monkeypatch):
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: FakeResponse(openai_payload("gpt-5.6-luna", "some-future-model")),
    )

    result = OpenAIBackend(api_key="typed-key").discover_models()

    assert result.outcome is ModelDiscoveryOutcome.SUCCESS
    assert result.models == ("gpt-5.6-luna", "some-future-model")


def test_openai_static_catalog_cannot_satisfy_success(monkeypatch):
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError()),
    )
    backend = OpenAIBackend(api_key="bad-key")

    result = backend.discover_models()

    assert result.outcome is ModelDiscoveryOutcome.FAILED
    assert result.models == ()
    assert result.offline_suggestions == tuple(backend.KNOWN_MODELS)
    assert backend.list_models() == backend.KNOWN_MODELS


def test_openai_http_failure_is_failed_and_diagnostic_hides_body(monkeypatch):
    secret_body = "Bearer top-secret-provider-body"
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: FakeResponse({"error": secret_body}, status_code=401),
    )

    result = OpenAIBackend(api_key="typed-key").discover_models()

    assert result.outcome is ModelDiscoveryOutcome.FAILED
    assert "HTTP 401" in result.diagnostic
    assert secret_body not in result.diagnostic
    assert "typed-key" not in result.diagnostic


def test_openai_successful_zero_result_is_empty(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse({"data": []}))

    result = OpenAIBackend(api_key="typed-key").discover_models()

    assert result.outcome is ModelDiscoveryOutcome.EMPTY
    assert result.models == ()


@pytest.mark.parametrize(
    ("backend_cls", "live_id"),
    [
        (DeepSeekBackend, "deepseek-live-only"),
        (GroqBackend, "groq-live-only"),
        (KimiBackend, "kimi-live-only"),
        (QwenBackend, "qwen-live-only"),
    ],
)
def test_openai_compatible_cloud_live_success_differs_from_static_fallback(
    monkeypatch, backend_cls, live_id
):
    monkeypatch.setattr(
        requests, "get", lambda *a, **k: FakeResponse(openai_payload(live_id))
    )
    backend = backend_cls(api_key="typed-key")

    result = backend.discover_models()

    assert result.outcome is ModelDiscoveryOutcome.SUCCESS
    assert result.models == (live_id,)
    assert live_id not in backend.KNOWN_MODELS


def test_openrouter_non_2xx_cannot_mark_discovery_ready(monkeypatch):
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: FakeResponse(openai_payload("false-success"), status_code=500),
    )
    backend = OpenRouterBackend(api_key="typed-key")

    result = backend.discover_models()

    assert result.outcome is ModelDiscoveryOutcome.FAILED
    assert backend._reasoning_cache_ready is False
    assert backend._last_discovery_succeeded is False


@pytest.mark.parametrize("payload", [{"error": {"message": "nope"}}, ["wrong"]])
def test_openrouter_malformed_or_error_shaped_json_is_failed(monkeypatch, payload):
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(payload))
    backend = OpenRouterBackend(api_key="typed-key")

    result = backend.discover_models()

    assert result.outcome is ModelDiscoveryOutcome.FAILED
    assert backend._reasoning_cache_ready is False
    assert backend._last_discovery_succeeded is False


def test_openrouter_failed_refresh_preserves_prior_reasoning_cache(monkeypatch):
    responses = iter([
        FakeResponse({
            "data": [{
                "id": "openai/gpt-5.6",
                "reasoning": {"supported_efforts": ["low", "high"]},
            }]
        }),
        FakeResponse({"error": "later failure"}, status_code=503),
    ])
    monkeypatch.setattr(requests, "get", lambda *a, **k: next(responses))
    backend = OpenRouterBackend(api_key="typed-key")

    assert backend.discover_models().outcome is ModelDiscoveryOutcome.SUCCESS
    before = backend.reasoning_capabilities("openai/gpt-5.6")
    assert backend.discover_models().outcome is ModelDiscoveryOutcome.FAILED

    assert backend.reasoning_capabilities("openai/gpt-5.6") is before
    assert backend._reasoning_cache_ready is True
    assert backend._last_discovery_succeeded is False


def test_anthropic_native_live_enumeration(monkeypatch):
    seen = {}

    def fake_get(url, **kwargs):
        seen["url"] = url
        seen["headers"] = kwargs["headers"]
        return FakeResponse(openai_payload("claude-provider-new"))

    monkeypatch.setattr(requests, "get", fake_get)
    result = AnthropicBackend(api_key="typed-anthropic").discover_models()

    assert result.outcome is ModelDiscoveryOutcome.SUCCESS
    assert result.models == ("claude-provider-new",)
    assert seen["url"].endswith("/v1/models")
    assert seen["headers"]["x-api-key"] == "typed-anthropic"


def test_gemini_filters_non_generation_models_from_provider_metadata(monkeypatch):
    payload = {
        "models": [
            {
                "name": "models/gemini-generation-model",
                "supportedGenerationMethods": ["generateContent", "countTokens"],
            },
            {
                "name": "models/text-embedding-model",
                "supportedGenerationMethods": ["embedContent"],
            },
        ]
    }
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(payload))

    result = GeminiBackend(api_key="typed-gemini").discover_models()

    assert result.outcome is ModelDiscoveryOutcome.SUCCESS
    assert result.models == ("gemini-generation-model",)


def test_lmstudio_unreachable_is_failed(monkeypatch):
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError()),
    )

    result = LMStudioBackend(base_url="http://localhost:1234/v1").discover_models()

    assert result.outcome is ModelDiscoveryOutcome.FAILED


def test_lmstudio_reachable_empty_is_empty(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse({"data": []}))

    result = LMStudioBackend(base_url="http://localhost:1234/v1").discover_models()

    assert result.outcome is ModelDiscoveryOutcome.EMPTY


def test_omniroute_discovery_uses_its_own_endpoint(monkeypatch):
    seen = {}

    def fake_get(url, **kwargs):
        seen["url"] = url
        return FakeResponse(openai_payload("omniroute/live"))

    monkeypatch.setattr(requests, "get", fake_get)
    backend = OmniRouteBackend(
        base_url="http://omniroute.internal:20128/v1", api_key="typed-key"
    )

    result = backend.discover_models()

    assert result.outcome is ModelDiscoveryOutcome.SUCCESS
    assert seen["url"] == "http://omniroute.internal:20128/v1/models"


def test_custom_failure_keeps_configured_model_available(monkeypatch):
    monkeypatch.setattr(config, "CUSTOM_DEFAULT_MODEL", "typed-custom-model")
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError()),
    )
    backend = CustomBackend(base_url="http://custom.invalid/v1", api_key="draft")

    result = backend.discover_models()

    assert result.outcome is ModelDiscoveryOutcome.FAILED
    assert backend.configured_model() == "typed-custom-model"


def test_unknown_discovered_model_uses_provider_default_reasoning(monkeypatch):
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: FakeResponse(openai_payload("some-future-model")),
    )
    backend = OpenAIBackend(api_key="typed-key")
    assert backend.discover_models().outcome is ModelDiscoveryOutcome.SUCCESS

    assert backend.reasoning_capabilities("some-future-model").efforts == ()
    payload = {}
    backend.apply_reasoning(payload, "high", model="some-future-model")
    assert payload == {}


def test_luna_reasoning_ladder_is_independent_of_discovery_membership():
    backend = OpenAIBackend(api_key="typed-key")

    assert backend.reasoning_capabilities("gpt-5.6-luna").efforts == (
        "none", "low", "medium", "high", "xhigh", "max"
    )


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def settings_tab(qapp, tmp_path, monkeypatch):
    from core.agent import LuminaAgent
    from ui.main_window import COLORS
    from ui.settings.general_tab import GeneralTab

    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(config, "LLM_BACKEND", "llamacpp")
    agent = LuminaAgent(owner=True, channel_id="backend-discovery-settings-test")
    return GeneralTab(agent, COLORS)


def test_failed_refresh_preserves_manual_model_and_never_labels_suggestions_live(
    settings_tab, monkeypatch
):
    settings_tab.backend_combo.setCurrentText("openai")
    settings_tab.cloud_model.clear()
    settings_tab.cloud_model.addItems(["previous-live-model"])
    settings_tab.cloud_model.setCurrentText("gpt-5.6-luna")
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError()),
    )

    settings_tab._refresh_models()

    assert settings_tab.cloud_model.currentText() == "gpt-5.6-luna"
    assert settings_tab.cloud_model.findText("previous-live-model") >= 0
    assert settings_tab.cloud_model.findText("gpt-4o") == -1
    assert "not shown as provider results" in settings_tab.status_lbl.text()


def test_successful_refresh_preserves_manual_model_absent_from_discovery(
    settings_tab, monkeypatch
):
    settings_tab.backend_combo.setCurrentText("openai")
    settings_tab.cloud_model.setCurrentText("gpt-5.6-luna")
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: FakeResponse(openai_payload("provider-listed-model")),
    )

    settings_tab._refresh_models()

    assert settings_tab.cloud_model.currentText() == "gpt-5.6-luna"
    assert settings_tab.cloud_model.findText("provider-listed-model") >= 0


def test_custom_settings_failure_preserves_typed_model(settings_tab, monkeypatch):
    settings_tab.backend_combo.setCurrentText("custom")
    settings_tab.url.setText("http://custom.invalid/v1")
    settings_tab.custom_model.setText("typed-custom-model")
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError()),
    )

    settings_tab._refresh_models()

    assert settings_tab.custom_model.text() == "typed-custom-model"
    assert "failed" in settings_tab.status_lbl.text().lower()


def test_custom_success_updates_completions_without_replacing_typed_model(
    settings_tab, monkeypatch
):
    settings_tab.backend_combo.setCurrentText("custom")
    settings_tab.url.setText("http://custom.invalid/v1")
    settings_tab.custom_model.setText("typed-custom-model")
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: FakeResponse(openai_payload("custom/live-model")),
    )

    settings_tab._refresh_models()

    completer_model = settings_tab.custom_model.completer().model()
    assert settings_tab.custom_model.text() == "typed-custom-model"
    assert completer_model.index(0, 0).data() == "custom/live-model"


def test_switching_providers_never_relabels_another_providers_live_models(
    settings_tab, monkeypatch
):
    settings_tab.backend_combo.setCurrentText("openai")
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: FakeResponse(openai_payload("openai/live-only")),
    )
    settings_tab._refresh_models()
    assert settings_tab.cloud_model.findText("openai/live-only") >= 0

    settings_tab.backend_combo.setCurrentText("groq")

    assert settings_tab.cloud_model.findText("openai/live-only") == -1


def test_refresh_uses_typed_key_without_config_or_prefs_mutation(
    settings_tab, monkeypatch
):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "saved-process-key")
    settings_tab.backend_combo.setCurrentText("openai")
    settings_tab.cloud_key.setText("draft-typed-key")
    seen = {}

    def fake_get(url, **kwargs):
        seen["authorization"] = kwargs["headers"]["Authorization"]
        seen["config_during_request"] = config.OPENAI_API_KEY
        return FakeResponse(openai_payload("gpt-5.6-luna"))

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(
        persistence,
        "save",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not persist")),
    )

    settings_tab._refresh_models()

    assert seen["authorization"] == "Bearer draft-typed-key"
    assert seen["config_during_request"] == "saved-process-key"
    assert config.OPENAI_API_KEY == "saved-process-key"
    assert settings_tab.cloud_model.currentText() != ""
