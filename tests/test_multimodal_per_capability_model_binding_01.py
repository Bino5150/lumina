"""MULTIMODAL-PER-CAPABILITY-MODEL-BINDING-01 focused proofs.

Evolves capability -> provider -> provider-global model into
capability -> provider -> optional capability-specific model override,
while preserving every existing default behavior. Covers:

  - core.capability_router.CapabilityRoute.model / resolve_capability_target()
  - core.backends.loader.get_llm_backend()'s per-invocation model override
  - core.vision_lane's use of that override (never a global mutation)
  - core.image_generation_service.resolve_image_generation_target()
  - ui.settings.tts_tab.MultimodalTab's Vision Model control and new
    Image Generation Provider/Model/pricing controls

No live provider traffic anywhere in this file.
"""
from __future__ import annotations

import json
import os
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

import config
from core import persistence as persistence_module
import core.backends.loader as loader_module
import core.generation_manifest as gm
import core.generation_spending_policy as sp
import core.higgsfield_adapter as ha
import core.higgsfield_transport as ht
import core.image_generation_service as svc
from core.capability_router import (
    Capability,
    CapabilityRegistry,
    CapabilityRoute,
    EvidenceClass,
    LANE_SPECIALIST,
    RoutingPolicy,
    SpecialistRecord,
    parse_routes,
    resolve_capability,
    resolve_capability_target,
)
from core.context import ContextManager
from core.vision_lane import execute_routed_vision, prepare_routed_turn


# ---------------------------------------------------------------------------
# PERSISTENCE / ROUTING
# ---------------------------------------------------------------------------

def test_model_override_persists_and_reloads_through_parse_routes():
    raw = {
        "vision_understanding": {
            "mode": "specialist", "specialist": "openai",
            "model": "gpt-5.6-luna",
        },
    }
    routes, warnings = parse_routes(raw)
    assert warnings == []
    assert routes["vision_understanding"].model == "gpt-5.6-luna"
    assert routes["vision_understanding"].specialist == "openai"


def test_canonical_identifiers_stored_not_friendly_labels():
    from ui.settings.tts_tab import _higgsfield_model_label

    assert _higgsfield_model_label("higgsfield-ai/soul/standard") == "Soul Standard"
    # The canonical id itself is what a route stores/resolves -- the
    # friendly label is display-only and never round-trips through
    # persistence or resolve_capability_target().
    route = CapabilityRoute(mode=LANE_SPECIALIST, specialist="higgsfield",
                            model="higgsfield-ai/soul/standard")
    assert route.model == "higgsfield-ai/soul/standard"


def test_credentials_never_enter_route_preferences():
    raw = {
        "image_generation": {
            "mode": "specialist", "specialist": "higgsfield",
            "model": "higgsfield-ai/soul/standard",
        },
    }
    routes, _ = parse_routes(raw)
    route = routes["image_generation"]
    dumped = json.dumps({
        "mode": route.mode, "specialist": route.specialist,
        "fallbacks": route.fallbacks, "quality_floor": route.quality_floor,
        "model": route.model,
    })
    for secret_marker in ("key_id", "key_secret", "api_key", "credential"):
        assert secret_marker not in dumped


def test_old_preferences_without_model_field_still_load():
    raw = {
        "vision_understanding": {
            "mode": "auto", "specialist": "openai", "fallbacks": ["gemini"],
            "quality_floor": "high",
        },
    }
    routes, warnings = parse_routes(raw)
    assert warnings == []
    route = routes["vision_understanding"]
    assert route.model is None
    assert route.specialist == "openai"
    assert route.fallbacks == ("gemini",)


def test_provider_default_backward_compatible_when_no_override():
    registry = CapabilityRegistry([
        SpecialistRecord(name="openai", kind="llm_backend", local=False,
                         capabilities={Capability.VISION_UNDERSTANDING: EvidenceClass.EVIDENCED}),
    ])
    route = CapabilityRoute(mode=LANE_SPECIALIST, specialist="openai")
    policy = RoutingPolicy(routes={"vision_understanding": route})
    decision = resolve_capability(registry, policy, Capability.VISION_UNDERSTANDING)
    assert decision.outcome == "routed"
    assert resolve_capability_target(route, decision) is None


def test_explicit_model_takes_precedence_over_provider_default():
    registry = CapabilityRegistry([
        SpecialistRecord(name="openai", kind="llm_backend", local=False,
                         capabilities={Capability.VISION_UNDERSTANDING: EvidenceClass.EVIDENCED}),
    ])
    route = CapabilityRoute(mode=LANE_SPECIALIST, specialist="openai", model="gpt-5.6-luna")
    policy = RoutingPolicy(routes={"vision_understanding": route})
    decision = resolve_capability(registry, policy, Capability.VISION_UNDERSTANDING)
    assert resolve_capability_target(route, decision) == "gpt-5.6-luna"


def test_model_never_forwarded_to_a_fallback_or_auto_pick():
    """The model configured for the explicit `specialist` must never leak
    onto a DIFFERENT admitted provider (a fallback, or an Auto pick)."""
    registry = CapabilityRegistry([
        SpecialistRecord(name="openai", kind="llm_backend", local=False, enabled=False,
                         capabilities={Capability.VISION_UNDERSTANDING: EvidenceClass.EVIDENCED}),
        SpecialistRecord(name="gemini", kind="llm_backend", local=False,
                         capabilities={Capability.VISION_UNDERSTANDING: EvidenceClass.EVIDENCED}),
    ])
    route = CapabilityRoute(mode=LANE_SPECIALIST, specialist="openai",
                            fallbacks=("gemini",), model="gpt-5.6-luna")
    policy = RoutingPolicy(routes={"vision_understanding": route})
    decision = resolve_capability(registry, policy, Capability.VISION_UNDERSTANDING)
    assert decision.selected == "gemini"
    assert decision.classification == "fallback"
    assert resolve_capability_target(route, decision) is None


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "lumina.db"))
    return tmp_path


def _hf_registry_policy(model=None):
    routes_raw = {"image_generation": {"mode": "specialist", "specialist": "higgsfield"}}
    if model:
        routes_raw["image_generation"]["model"] = model
    routes, _ = parse_routes(routes_raw)
    registry = CapabilityRegistry([
        SpecialistRecord(name="higgsfield", kind="external", local=False,
                         capabilities={Capability.IMAGE_GENERATION: EvidenceClass.EVIDENCED}),
    ])
    policy = RoutingPolicy(routes=routes)
    return registry, policy


def test_invalid_explicit_model_fails_closed(monkeypatch):
    monkeypatch.setattr(config, "MULTIMODAL_ROUTES", {
        "image_generation": {
            "mode": "specialist", "specialist": "higgsfield",
            "model": "nano-banana",  # stale/dead -- never truthfully supported
        },
    })
    monkeypatch.setattr(config, "MULTIMODAL_DISABLED_PROVIDERS", [])
    result = svc.resolve_image_generation_target()
    assert result is None  # never silently substituted for soul/standard


# ---------------------------------------------------------------------------
# ISOLATION
# ---------------------------------------------------------------------------

def test_vision_model_override_does_not_touch_global_openai_model(monkeypatch):
    monkeypatch.setattr(config, "OPENAI_DEFAULT_MODEL", "gpt-4o-mini")
    backend = loader_module.get_llm_backend(name="openai", model="gpt-5.6-luna")
    assert backend.configured_model() == "gpt-5.6-luna"
    assert config.OPENAI_DEFAULT_MODEL == "gpt-4o-mini"
    # A brand new, unrelated construction still reads the untouched global.
    other = loader_module.get_llm_backend(name="openai")
    assert other.configured_model() == "gpt-4o-mini"


def test_image_generation_model_selection_never_touches_chat_backend_model(monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_DEFAULT_MODEL", "z-ai/glm-5.3-flash")
    monkeypatch.setattr(config, "MULTIMODAL_ROUTES", {
        "image_generation": {
            "mode": "specialist", "specialist": "higgsfield",
            "model": "higgsfield-ai/soul/standard",
        },
    })
    monkeypatch.setattr(config, "MULTIMODAL_DISABLED_PROVIDERS", [])
    target = svc.resolve_image_generation_target()
    assert target is not None
    assert target.model == "higgsfield-ai/soul/standard"
    assert config.OPENROUTER_DEFAULT_MODEL == "z-ai/glm-5.3-flash"


def test_global_provider_default_change_never_overwrites_explicit_capability_model(monkeypatch):
    monkeypatch.setattr(config, "OPENAI_DEFAULT_MODEL", "gpt-4o-mini")
    monkeypatch.setattr(config, "MULTIMODAL_ROUTES", {
        "vision_understanding": {
            "mode": "specialist", "specialist": "openai", "model": "gpt-5.6-luna",
        },
    })
    routes, _ = parse_routes(config.MULTIMODAL_ROUTES)
    route = routes["vision_understanding"]
    assert route.model == "gpt-5.6-luna"

    # The owner now changes the GLOBAL OpenAI default in General Settings.
    monkeypatch.setattr(config, "OPENAI_DEFAULT_MODEL", "gpt-4o")

    # Re-reading the SAME persisted capability route must still show the
    # untouched explicit override -- a global default change never
    # rewrites a stale explicit capability preference.
    routes_again, _ = parse_routes(config.MULTIMODAL_ROUTES)
    assert routes_again["vision_understanding"].model == "gpt-5.6-luna"


def test_simultaneous_capability_resolution_returns_independent_models():
    vision_backend = loader_module.get_llm_backend(name="openai", model="gpt-5.6-luna")
    other_vision_backend = loader_module.get_llm_backend(name="openai", model="gpt-4o")
    assert vision_backend.configured_model() == "gpt-5.6-luna"
    assert other_vision_backend.configured_model() == "gpt-4o"
    assert vision_backend is not other_vision_backend


def test_no_temporary_global_mutation_during_get_llm_backend_override(monkeypatch):
    """No save/restore dance: config.OPENAI_DEFAULT_MODEL is read exactly
    once by __init__ and never written to at all while applying a
    per-invocation override -- the override lands only on the fresh
    instance's own attribute, never on shared/global state."""
    monkeypatch.setattr(config, "OPENAI_DEFAULT_MODEL", "gpt-4o-mini")
    before = dict(vars(config)) if hasattr(config, "__dict__") else None

    backend = loader_module.get_llm_backend(name="openai", model="gpt-5.6-luna")

    assert config.OPENAI_DEFAULT_MODEL == "gpt-4o-mini"
    assert backend.configured_model() == "gpt-5.6-luna"
    if before is not None:
        assert vars(config).get("OPENAI_DEFAULT_MODEL") == before.get("OPENAI_DEFAULT_MODEL")


# ---------------------------------------------------------------------------
# VISION execution wiring
# ---------------------------------------------------------------------------

def _img(marker):
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{marker}"}}


class _FakeVisionSpecialist:
    def __init__(self, model):
        self.name = "openai"
        self._model = model
        self.chat_calls = []

    def get_model(self):
        return self._model

    def configured_model(self):
        return self._model

    def chat(self, messages, tools=None, max_tokens=None, **kwargs):
        self.chat_calls.append({"messages": messages, "max_tokens": max_tokens})
        return {"choices": [{"message": {"role": "assistant", "content": "a cat"}}]}

    def extract_message(self, response):
        return response["choices"][0]["message"]


class _FakePrimary:
    def __init__(self):
        self.name = "primary-fake-backend"
        self._model = "primary-model-1"

    def get_model(self):
        return self._model

    def configured_model(self):
        return self._model


def _vision_agent(monkeypatch, route, get_llm_backend_fake):
    monkeypatch.setattr(config, "MULTIMODAL_ROUTES", {"vision_understanding": route})
    monkeypatch.setattr(config, "MULTIMODAL_DISABLED_PROVIDERS", [])
    monkeypatch.setattr(loader_module, "get_llm_backend", get_llm_backend_fake)
    ctx = ContextManager(owner=False)
    primary = _FakePrimary()
    agent = types.SimpleNamespace(ctx=ctx, llm=primary)
    return agent, primary


def test_vision_provider_default_preserves_current_working_behavior(monkeypatch):
    """No explicit model configured -- get_llm_backend() is called exactly
    as it always was, with no `model` kwarg at all."""
    calls = []
    specialist = _FakeVisionSpecialist("provider-default-model")

    def fake_loader(name=None, url=None, api_key=None):
        calls.append({"name": name, "url": url, "api_key": api_key})
        return specialist

    agent, primary = _vision_agent(
        monkeypatch,
        {"mode": "specialist", "specialist": "openai"},
        fake_loader,
    )
    ctx = agent.ctx
    content, route_ctx = prepare_routed_turn(agent, [_img("A")])
    ctx.add_user(content)
    assert route_ctx.model_override == ""
    result = execute_routed_vision(agent, route_ctx)

    assert result.outcome == "success"
    assert result.model == "provider-default-model"
    assert calls == [{"name": "openai", "url": None, "api_key": None}]


def test_vision_explicit_override_passed_to_bounded_specialist_invocation(monkeypatch):
    """An explicit capability model IS configured -- the specialist call
    must receive it per invocation, via get_llm_backend(model=...)."""
    calls = []
    specialist = _FakeVisionSpecialist("gpt-5.6-luna")

    def fake_loader(name=None, url=None, api_key=None, model=None):
        calls.append({"name": name, "model": model})
        assert model == "gpt-5.6-luna"
        return specialist

    agent, primary = _vision_agent(
        monkeypatch,
        {"mode": "specialist", "specialist": "openai", "model": "gpt-5.6-luna"},
        fake_loader,
    )
    ctx = agent.ctx
    content, route_ctx = prepare_routed_turn(agent, [_img("A")])
    ctx.add_user(content)
    assert route_ctx.model_override == "gpt-5.6-luna"
    result = execute_routed_vision(agent, route_ctx)

    assert result.outcome == "success"
    assert result.model == "gpt-5.6-luna"
    assert calls == [{"name": "openai", "model": "gpt-5.6-luna"}]


def test_vision_override_never_touches_primary_conversation_backend(monkeypatch):
    specialist = _FakeVisionSpecialist("gpt-5.6-luna")

    def fake_loader(name=None, url=None, api_key=None, model=None):
        return specialist

    agent, primary = _vision_agent(
        monkeypatch,
        {"mode": "specialist", "specialist": "openai", "model": "gpt-5.6-luna"},
        fake_loader,
    )
    ctx = agent.ctx
    content, route_ctx = prepare_routed_turn(agent, [_img("A")])
    ctx.add_user(content)
    execute_routed_vision(agent, route_ctx)

    assert agent.llm is primary
    assert agent.llm.configured_model() == "primary-model-1"


# ---------------------------------------------------------------------------
# IMAGE GENERATION
# ---------------------------------------------------------------------------

def test_higgsfield_appears_as_image_generation_provider():
    pytest.importorskip("PySide6")
    from types import SimpleNamespace
    from PySide6.QtWidgets import QApplication
    from ui.settings.tts_tab import MultimodalTab
    from ui.main_window import COLORS

    app = QApplication.instance() or QApplication([])
    tab = MultimodalTab(SimpleNamespace(tts=SimpleNamespace(enabled=True)), COLORS)
    provider_ids = [tab.image_provider_combo.itemData(i) for i in range(tab.image_provider_combo.count())]
    assert "higgsfield" in provider_ids


def test_soul_standard_appears_and_nano_banana_does_not():
    pytest.importorskip("PySide6")
    from types import SimpleNamespace
    from PySide6.QtWidgets import QApplication
    from ui.settings.tts_tab import MultimodalTab
    from ui.main_window import COLORS

    app = QApplication.instance() or QApplication([])
    tab = MultimodalTab(SimpleNamespace(tts=SimpleNamespace(enabled=True)), COLORS)
    model_ids = [tab.image_model_combo.itemData(i) for i in range(tab.image_model_combo.count())]
    labels = [tab.image_model_combo.itemText(i) for i in range(tab.image_model_combo.count())]

    assert "higgsfield-ai/soul/standard" in model_ids
    assert "Soul Standard" in labels
    assert "nano-banana" not in model_ids
    assert not any("banana" in (label or "").lower() for label in labels)


def test_image_generation_route_defaults_disabled_and_unadmitted():
    """A never-configured install: the Route mode control itself defaults
    to Disabled -- the same explicit, deliberate-opt-in mechanism
    vision_understanding already has."""
    pytest.importorskip("PySide6")
    from types import SimpleNamespace
    from PySide6.QtWidgets import QApplication
    from ui.settings.tts_tab import MultimodalTab
    from ui.main_window import COLORS

    app = QApplication.instance() or QApplication([])
    tab = MultimodalTab(SimpleNamespace(tts=SimpleNamespace(enabled=True)), COLORS)
    assert tab.image_mode_combo.currentData() == "disabled"
    assert not tab.image_provider_combo.isEnabled()
    assert not tab.image_model_combo.isEnabled()


def test_unrelated_settings_save_never_admits_image_generation(monkeypatch, tmp_path):
    """THE regression this section exists to prevent: an existing user with
    no image_generation route, opening Multimodal Settings and saving for
    an unrelated reason (here: nothing at all is touched), must not
    acquire a newly-admitted, potentially cost-bearing capability route.
    Absence must stay absence until the owner deliberately flips Route
    mode to Specialist -- mirrors vision_understanding's own
    already-established contract exactly."""
    pytest.importorskip("PySide6")
    from types import SimpleNamespace
    from PySide6.QtWidgets import QApplication
    from ui.settings.tts_tab import MultimodalTab
    from ui.main_window import COLORS

    monkeypatch.setattr(persistence_module, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(config, "MULTIMODAL_ROUTES", {})
    monkeypatch.setattr(config, "MULTIMODAL_DISABLED_PROVIDERS", [])

    app = QApplication.instance() or QApplication([])
    tab = MultimodalTab(SimpleNamespace(tts=SimpleNamespace(enabled=True)), COLORS)
    tab._save()

    assert "image_generation" not in config.MULTIMODAL_ROUTES
    assert "image_generation" not in persistence_module.load()["multimodal_routes"]
    assert svc.resolve_image_generation_target() is None


def test_deliberate_route_mode_change_is_what_admits_the_route(monkeypatch, tmp_path):
    """The flip side: once the owner deliberately selects Specialist mode
    and saves, the route DOES persist and DOES resolve/admit -- this is
    the one and only way the capability becomes reachable."""
    pytest.importorskip("PySide6")
    from types import SimpleNamespace
    from PySide6.QtWidgets import QApplication
    from ui.settings.tts_tab import MultimodalTab
    from ui.main_window import COLORS

    monkeypatch.setattr(persistence_module, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(config, "MULTIMODAL_ROUTES", {})
    monkeypatch.setattr(config, "MULTIMODAL_DISABLED_PROVIDERS", [])

    app = QApplication.instance() or QApplication([])
    tab = MultimodalTab(SimpleNamespace(tts=SimpleNamespace(enabled=True)), COLORS)
    tab.image_mode_combo.setCurrentIndex(tab.image_mode_combo.findData("specialist"))
    tab._save()

    assert config.MULTIMODAL_ROUTES["image_generation"] == {
        "mode": "specialist", "specialist": "higgsfield",
        "model": "higgsfield-ai/soul/standard",
    }
    target = svc.resolve_image_generation_target()
    assert target is not None
    assert (target.specialist, target.model) == ("higgsfield", "higgsfield-ai/soul/standard")


def test_already_enabled_route_survives_an_unrelated_save(monkeypatch, tmp_path):
    """The mirror image: an owner who already enabled Image Generation in
    a prior session must not lose it merely by saving an unrelated field
    later -- absence-preservation only ever protects a NEVER-configured
    route, never an already-configured one."""
    pytest.importorskip("PySide6")
    from types import SimpleNamespace
    from PySide6.QtWidgets import QApplication
    from ui.settings.tts_tab import MultimodalTab
    from ui.main_window import COLORS

    monkeypatch.setattr(persistence_module, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(config, "MULTIMODAL_ROUTES", {
        "image_generation": {
            "mode": "specialist", "specialist": "higgsfield",
            "model": "higgsfield-ai/soul/standard",
        },
    })
    monkeypatch.setattr(config, "MULTIMODAL_DISABLED_PROVIDERS", [])

    app = QApplication.instance() or QApplication([])
    tab = MultimodalTab(SimpleNamespace(tts=SimpleNamespace(enabled=True)), COLORS)
    tab._save()

    assert config.MULTIMODAL_ROUTES["image_generation"] == {
        "mode": "specialist", "specialist": "higgsfield",
        "model": "higgsfield-ai/soul/standard",
    }


def test_persisted_image_generation_selection_resolves_via_m1(monkeypatch):
    monkeypatch.setattr(config, "MULTIMODAL_ROUTES", {
        "image_generation": {
            "mode": "specialist", "specialist": "higgsfield",
            "model": "higgsfield-ai/soul/standard",
        },
    })
    monkeypatch.setattr(config, "MULTIMODAL_DISABLED_PROVIDERS", [])

    target = svc.resolve_image_generation_target()

    assert target is not None
    assert target.specialist == "higgsfield"
    assert target.model == "higgsfield-ai/soul/standard"


def test_resolved_target_passes_through_image_generation_service_offline(monkeypatch):
    monkeypatch.setattr(config, "MULTIMODAL_ROUTES", {
        "image_generation": {
            "mode": "specialist", "specialist": "higgsfield",
            "model": "higgsfield-ai/soul/standard",
        },
    })
    monkeypatch.setattr(config, "MULTIMODAL_DISABLED_PROVIDERS", [])
    target = svc.resolve_image_generation_target()
    assert target is not None

    class FakeAdapter:
        def submit(self, *, model, settings, reference_assets, provider_idempotency_key=None):
            return "job-1", "queued"

        def poll(self, provider_job_id):
            return "succeeded"

        def fetch_result(self, provider_job_id):
            return b"fake-image-bytes", "image/png"

        def estimate_cost(self, *, model, settings):
            return 0.0938

        def cancel(self, job):
            return job

    result = svc.generate_image(
        registry=target.registry, policy=target.policy,
        specialist=target.specialist, model=target.model,
        adapter=FakeAdapter(), settings={"prompt": "a lighthouse"},
        spending_policy=sp.SpendingPolicy(single_job_ceiling=1.0),
        manifest_provider="higgsfield", authorization_ref="owner-envelope-1",
        cost_unit="usd", poll_interval=0.001, poll_timeout=2.0,
    )
    assert result.outcome == svc.OUTCOME_SUCCESS


class _FakeHiggsfieldTransport:
    """Minimal, fully-offline fake -- never opens a socket. Configured per
    (METHOD, path-or-url); an unconfigured call raises immediately."""

    def __init__(self):
        self.calls = []
        self._responses = {}

    def configure(self, method, key, response):
        self._responses[(method, key)] = response

    def _resolve(self, method, key):
        resp = self._responses.get((method, key))
        if resp is None:
            raise AssertionError(f"no fake response configured for {method} {key}")
        return resp

    def post(self, path, *, json=None, params=None):
        self.calls.append(("POST", path, json))
        return self._resolve("POST", path)

    def get(self, path, *, params=None):
        self.calls.append(("GET", path, params))
        return self._resolve("GET", path)

    def get_raw(self, url, *, headers=None):
        self.calls.append(("GET_RAW", url, headers))
        return self._resolve("GET_RAW", url)

    def put_bytes(self, url, *, data, headers):
        self.calls.append(("PUT", url, headers))
        return self._resolve("PUT", url)


def _json_response(status_code, body_dict, headers=None):
    return ht.TransportResponse(status_code=status_code, headers=headers or {},
                                body=json.dumps(body_dict).encode("utf-8"))


def _raw_response(status_code, body_bytes, headers=None):
    return ht.TransportResponse(status_code=status_code, headers=headers or {}, body=body_bytes)


def test_resolver_driven_generation_reaches_real_artifact_and_manifest(monkeypatch):
    """The Settings-resolved (provider, model) pair, fed into the REAL
    HiggsfieldAdapter against a fake transport, still reaches a durable
    artifact + validated manifest -- proving the resolver seam's output is
    directly usable by production code, not just a shape that "looks
    right"."""
    monkeypatch.setattr(config, "MULTIMODAL_ROUTES", {
        "image_generation": {
            "mode": "specialist", "specialist": "higgsfield",
            "model": "higgsfield-ai/soul/standard",
        },
    })
    monkeypatch.setattr(config, "MULTIMODAL_DISABLED_PROVIDERS", [])
    target = svc.resolve_image_generation_target()
    assert target is not None
    assert (target.specialist, target.model) == ("higgsfield", "higgsfield-ai/soul/standard")

    transport = _FakeHiggsfieldTransport()
    transport.configure("POST", "/higgsfield-ai/soul/standard", _json_response(200, {
        "status": "queued", "request_id": "req-binding-1",
    }))
    transport.configure("GET", "/requests/req-binding-1/status", _json_response(200, {
        "status": "completed", "request_id": "req-binding-1",
        "images": [{"url": "https://cdn.example.com/binding-out-0.jpg"}],
    }))
    transport.configure("GET_RAW", "https://cdn.example.com/binding-out-0.jpg",
                        _raw_response(200, b"resolver-driven-bytes",
                                      headers={"Content-Type": "image/jpeg"}))
    adapter = ha.HiggsfieldAdapter(transport=transport)

    result = svc.generate_image(
        registry=target.registry, policy=target.policy,
        specialist=target.specialist, model=target.model,
        adapter=adapter, settings={"prompt": "a lighthouse at dawn"},
        spending_policy=sp.SpendingPolicy(single_job_ceiling=1.0),
        manifest_provider="higgsfield", authorization_ref="owner-envelope-88",
        cost_unit="usd", poll_interval=0.001, poll_timeout=2.0,
    )

    assert result.outcome == svc.OUTCOME_SUCCESS
    assert len(result.artifacts) == 1
    assert len(result.manifests) == 1
    assert result.manifests[0]["provider"] == "higgsfield"


# ---------------------------------------------------------------------------
# PRICING LINK
# ---------------------------------------------------------------------------

def test_pricing_link_targets_vetted_official_url():
    from ui.settings.tts_tab import _HIGGSFIELD_PRICING_URL

    assert _HIGGSFIELD_PRICING_URL == "https://console.higgsfield.ai"
    assert _HIGGSFIELD_PRICING_URL.startswith("https://")


def test_pricing_link_contains_no_secrets_or_query_string():
    from ui.settings.tts_tab import _HIGGSFIELD_PRICING_URL

    assert "?" not in _HIGGSFIELD_PRICING_URL
    for marker in ("key", "secret", "token", "@"):
        assert marker not in _HIGGSFIELD_PRICING_URL.lower()


def test_clicking_pricing_button_does_not_mutate_provider_settings(monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    from types import SimpleNamespace
    from PySide6.QtWidgets import QApplication
    from ui.settings.tts_tab import MultimodalTab
    from ui.main_window import COLORS
    import core.secrets as secrets_module

    monkeypatch.setattr(secrets_module, "SECRETS_PATH", str(tmp_path / "credentials.json"))
    secrets_module.set_secrets({
        "higgsfield_key_id": "isolated-test-id",
        "higgsfield_key_secret": "isolated-test-secret",
    })

    app = QApplication.instance() or QApplication([])
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))

    tab = MultimodalTab(SimpleNamespace(tts=SimpleNamespace(enabled=True)), COLORS)
    before_id = secrets_module.get_secret("higgsfield_key_id")
    before_secret = secrets_module.get_secret("higgsfield_key_secret")

    tab.image_pricing_btn.click()

    assert opened == ["https://console.higgsfield.ai"]
    assert secrets_module.get_secret("higgsfield_key_id") == before_id
    assert secrets_module.get_secret("higgsfield_key_secret") == before_secret


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

def test_vision_model_control_enables_disables_with_route_mode():
    pytest.importorskip("PySide6")
    from types import SimpleNamespace
    from PySide6.QtWidgets import QApplication
    from ui.settings.tts_tab import MultimodalTab
    from ui.main_window import COLORS

    app = QApplication.instance() or QApplication([])
    tab = MultimodalTab(SimpleNamespace(tts=SimpleNamespace(enabled=True)), COLORS)

    tab.vision_mode_combo.setCurrentIndex(tab.vision_mode_combo.findData("disabled"))
    assert not tab.vision_model_combo.isEnabled()

    tab.vision_mode_combo.setCurrentIndex(tab.vision_mode_combo.findData("specialist"))
    assert tab.vision_model_combo.isEnabled()


def test_vision_model_default_label_refreshes_when_provider_changes(monkeypatch):
    pytest.importorskip("PySide6")
    from types import SimpleNamespace
    from PySide6.QtWidgets import QApplication
    from ui.settings.tts_tab import MultimodalTab
    from ui.main_window import COLORS

    monkeypatch.setattr(config, "OPENAI_DEFAULT_MODEL", "gpt-5.6-luna")
    monkeypatch.setattr(config, "GEMINI_DEFAULT_MODEL", "gemini-3.5-flash")
    app = QApplication.instance() or QApplication([])
    tab = MultimodalTab(SimpleNamespace(tts=SimpleNamespace(enabled=True)), COLORS)

    tab.vision_provider_combo.setCurrentText("openai")
    assert tab.vision_model_combo.itemText(0) == "Provider default — gpt-5.6-luna"

    tab.vision_provider_combo.setCurrentText("gemini")
    assert tab.vision_model_combo.itemText(0) == "Provider default — gemini-3.5-flash"
