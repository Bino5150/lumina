"""MB-34-LIVE-VISION-TOOL-CAPABILITY-01 -- live-instance vision+tools
capability hydration repair.

THE DEFECT (murderboard MB-34, institutionalized 2026-09-12): the live
OpenRouter backend instance carrying the conversation is constructed by
loader.get_llm_backend() with an empty _vision_tool_cache and NEVER runs
discover_models() -- capability discovery only ever ran on separate
throwaway Settings probe instances (ui/settings/general_tab.py constructs
its own backend for _refresh_models()/_get_or_create_reasoning_probe()).
With image-bearing history plus tools, the inherited LMStudioBackend.chat()
has_vision guard (lmstudio.py:384-385) consults
supports_vision_with_tools(model) on the LIVE instance, whose unpopulated
cache truthfully-but-wrongly yields False via dict.get(model, False) --
silently dropping tools/tool_choice from every image-bearing WORK round all
session, even for a model OpenRouter's own /models data positively
advertises as vision+tools capable.

THE REPAIR CONTRACT: the instance that carries the conversation owns its
own capability truth. supports_vision_with_tools() on OpenRouterBackend now
establishes that truth at first consultation -- exactly ONE bounded,
latched discover_models() attempt per instance lifetime (success or failure
latched; no uncontrolled re-discovery), reusing the single established
provider-owned population path. Failure never manufactures support.
UNKNOWN IS NOT UNSUPPORTED: vision_tool_capability_state() distinguishes
supported / unsupported / unknown_model_absent / unknown_discovery_failed /
unknown_not_discovered.

No live paid provider traffic: every HTTP surface is monkeypatched.
"""

import types
from pathlib import Path

import pytest
import requests

from core.backends.base import ModelDiscoveryOutcome, ModelDiscoveryResult, ToolChoiceMode
from core.backends.openrouter import OpenRouterBackend
from core.agent import _maybe_emit_vision_tool_capability_notice


_IMAGE = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
_PRODUCT_TOOLS = [{"type": "function", "function": {
    "name": "read_file", "description": "", "parameters": {}}}]

_CAPABLE_ENTRY = {
    "id": "z-ai/glm-5.3-flash",
    "architecture": {"input_modalities": ["text", "image"]},
    "supported_parameters": ["tools", "tool_choice"],
}
_TEXT_ONLY_ENTRY = {
    "id": "meta-llama/llama-3.1-8b-instruct:free",
    "architecture": {"input_modalities": ["text"]},
    "supported_parameters": ["tools"],
}
_VISION_NO_TOOLS_ENTRY = {
    "id": "vendor/vision-no-tools",
    "architecture": {"input_modalities": ["text", "image"]},
    "supported_parameters": ["temperature"],
}


def _bare_openrouter_backend(model="z-ai/glm-5.3-flash"):
    """A live-shaped instance: the exact attribute set the established
    __new__ test convention uses (empty caches, discovery never run), so
    no config/credential read and no network can happen at construction."""
    backend = OpenRouterBackend.__new__(OpenRouterBackend)
    backend.base_url = "https://openrouter.ai/api/v1"
    backend.headers = {"Content-Type": "application/json", "Authorization": "Bearer test"}
    backend._model = model
    backend._reasoning_cache = {}
    backend._reasoning_cache_ready = False
    backend._vision_tool_cache = {}
    return backend


def _models_resp(*entries):
    class _ModelsResp:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return {"data": list(entries)}
    return _ModelsResp()


def _chat_resp(message):
    class _ChatResp:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return {"choices": [{"message": message}]}
    return _ChatResp()


def _install_discovery(monkeypatch, entries, calls):
    """Mock GET /models; records every discovery URL hit."""
    class _ModelsResp:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return {"data": list(entries)}
    def _fake_get(url, headers=None, timeout=None):
        calls.append(url)
        return _ModelsResp()
    monkeypatch.setattr(requests, "get", _fake_get)


def _capture_post(monkeypatch, message=None):
    captured = {}
    class _ChatResp:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return {"choices": [{"message": message}]}
    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["payload"] = json
        return _ChatResp()
    monkeypatch.setattr(requests, "post", _fake_post)
    return captured


_VISION_MESSAGES = [{"role": "user", "content": [
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    {"type": "text", "text": "hi"},
]}]


# ── THE regression: live instance hydrates its own truth, keeps tools ──────

def test_mb34_regression_live_instance_hydrates_capability_and_keeps_tools(monkeypatch):
    """THE MB-34 regression. Pre-repair this fails with the original
    mechanism: the live conversation instance never discovers, its empty
    _vision_tool_cache yields False, and the has_vision guard silently
    drops tools/tool_choice from an image-bearing request even though
    OpenRouter's own /models entry positively advertises vision+tools for
    the configured model. Post-repair: ONE discovery call on THIS
    instance, then tools ride the wire."""
    calls = []
    _install_discovery(monkeypatch, [_CAPABLE_ENTRY], calls)
    captured = _capture_post(monkeypatch, {"content": "ok"})

    backend = _bare_openrouter_backend()
    assert backend._vision_tool_cache == {}  # the MB-34 starting state

    backend.chat(_VISION_MESSAGES, tools=_PRODUCT_TOOLS, tool_choice_mode=ToolChoiceMode.AUTO)

    # The live instance established its own capability truth.
    assert calls == ["https://openrouter.ai/api/v1/models"]
    assert backend._vision_tool_cache.get("z-ai/glm-5.3-flash") is True
    payload = captured["payload"]
    assert payload.get("tools") == _PRODUCT_TOOLS
    assert payload.get("tool_choice") == "auto"


def test_mb34_regression_notice_stays_silent_once_hydrated(monkeypatch):
    """The capability notice must NOT fire for a capable model on the live
    instance: hydration establishes truth BEFORE the guard/notice decide,
    so a vision+tools-capable model keeps its tools without a false
    'doesn't support' notice."""
    calls = []
    _install_discovery(monkeypatch, [_CAPABLE_ENTRY], calls)
    _capture_post(monkeypatch, {"content": "ok"})

    backend = _bare_openrouter_backend()
    notices = []
    agent = types.SimpleNamespace(llm=backend, on_commentary=lambda t: notices.append(t))

    from core.agent import _maybe_emit_vision_tool_capability_notice
    fired = _maybe_emit_vision_tool_capability_notice(agent, _VISION_MESSAGES, _PRODUCT_TOOLS)

    assert fired is False
    assert notices == []
    assert calls == ["https://openrouter.ai/api/v1/models"]  # hydrated by the notice's own consult
    payload_backend = backend
    assert payload_backend.supports_vision_with_tools("z-ai/glm-5.3-flash") is True


# ── Independence: live instance vs Settings probes ─────────────────────────

def test_live_instance_hydrates_without_any_settings_probe(monkeypatch):
    """Scroll #1: a fresh live instance obtains capability truth with NO
    Settings probe anywhere in the picture."""
    calls = []
    _install_discovery(monkeypatch, [_CAPABLE_ENTRY], calls)
    backend = _bare_openrouter_backend()
    assert backend.supports_vision_with_tools("z-ai/glm-5.3-flash") is True
    assert len(calls) == 1


def test_settings_probe_population_cannot_make_live_instance_true(monkeypatch):
    """Scroll #2: discovery on a SEPARATE probe instance can never be what
    makes the live instance's answer correct. The live instance's own
    latched attempt failed; a fully-populated sibling changes nothing."""
    # The "Settings probe": its own instance, successfully discovered.
    probe_calls = []
    _install_discovery(monkeypatch, [_CAPABLE_ENTRY], probe_calls)
    probe = _bare_openrouter_backend()
    assert probe.discover_models().outcome == ModelDiscoveryOutcome.SUCCESS
    assert probe.supports_vision_with_tools("z-ai/glm-5.3-flash") is True

    # The live instance: discovery fails (transport down), stays UNKNOWN.
    live = _bare_openrouter_backend()
    live_calls = []
    def _failed_discovery():
        live_calls.append(1)
        return ModelDiscoveryResult(ModelDiscoveryOutcome.FAILED,
                                    diagnostic="OpenRouter model discovery failed (ConnectionError).")
    live.discover_models = _failed_discovery

    assert live.supports_vision_with_tools("z-ai/glm-5.3-flash") is False
    assert live.vision_tool_capability_state("z-ai/glm-5.3-flash") == "unknown_discovery_failed"
    assert len(live_calls) == 1  # its own latched attempt, not the probe's data


# ── Selection truth: incompatible / absent / failed ────────────────────────

def test_discovered_incompatible_model_still_drops_tools_and_reports_unsupported(monkeypatch):
    """Scroll #4 + #16: a model the discovery response positively saw
    WITHOUT both required signals keeps tools suppressed -- and is
    reported as UNSUPPORTED, not unknown."""
    calls = []
    _install_discovery(monkeypatch, [_VISION_NO_TOOLS_ENTRY], calls)
    captured = _capture_post(monkeypatch, {"content": "ok"})

    backend = _bare_openrouter_backend(model="vendor/vision-no-tools")
    backend.chat(_VISION_MESSAGES, tools=_PRODUCT_TOOLS, tool_choice_mode=ToolChoiceMode.AUTO)

    assert len(calls) == 1  # hydrated once, truthfully
    assert captured["payload"].get("tools") is None
    assert backend.vision_tool_capability_state("vendor/vision-no-tools") == "unsupported"


def test_model_absent_from_discovery_response_stays_unknown_and_drops_tools(monkeypatch):
    """Scroll #5/#6: a successful discovery that does not list the selected
    model must NOT manufacture support -- UNKNOWN, tools stay dropped."""
    calls = []
    _install_discovery(monkeypatch, [_CAPABLE_ENTRY], calls)  # response lacks our model
    backend = _bare_openrouter_backend(model="vendor/brand-new-model")
    assert backend.supports_vision_with_tools("vendor/brand-new-model") is False
    assert backend.vision_tool_capability_state("vendor/brand-new-model") == "unknown_model_absent"


def test_failed_discovery_never_manufactures_support(monkeypatch):
    """Scroll #6: FAILED discovery -> conservative False, recorded reason,
    and a raising discovery double is equally contained."""
    backend = _bare_openrouter_backend()
    def _failed():
        return ModelDiscoveryResult(ModelDiscoveryOutcome.FAILED,
                                    diagnostic="OpenRouter model discovery failed (ConnectionError).")
    backend.discover_models = _failed
    assert backend.supports_vision_with_tools("z-ai/glm-5.3-flash") is False
    assert backend.vision_tool_capability_state("z-ai/glm-5.3-flash") == "unknown_discovery_failed"

    backend2 = _bare_openrouter_backend()
    def _boom():
        raise RuntimeError("boom")
    backend2.discover_models = _boom
    assert backend2.supports_vision_with_tools("z-ai/glm-5.3-flash") is False
    assert backend2.vision_tool_capability_state("z-ai/glm-5.3-flash") == "unknown_discovery_failed"


def test_malformed_discovery_response_fails_safely(monkeypatch):
    """Scroll #15: malformed /models payload -> FAILED outcome -> no false
    positive, tools stay dropped."""
    class _GarbageResp:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return {"data": "not-a-list"}
    monkeypatch.setattr(requests, "get", lambda url, headers=None, timeout=None: _GarbageResp())
    backend = _bare_openrouter_backend()
    assert backend.supports_vision_with_tools("z-ai/glm-5.3-flash") is False
    assert backend.vision_tool_capability_state("z-ai/glm-5.3-flash") == "unknown_discovery_failed"


# ── Cache / model-switch integrity ─────────────────────────────────────────

def test_capability_does_not_leak_between_models(monkeypatch):
    """Scroll #7: model A's capability never becomes model B's. One
    discovery response evaluates each model on its own signals."""
    calls = []
    _install_discovery(monkeypatch, [_CAPABLE_ENTRY, _TEXT_ONLY_ENTRY], calls)
    backend = _bare_openrouter_backend()
    assert backend.supports_vision_with_tools("z-ai/glm-5.3-flash") is True
    assert backend.supports_vision_with_tools("meta-llama/llama-3.1-8b-instruct:free") is False
    assert backend.vision_tool_capability_state("z-ai/glm-5.3-flash") == "supported"
    assert backend.vision_tool_capability_state("meta-llama/llama-3.1-8b-instruct:free") == "unsupported"
    assert len(calls) == 1  # both answered from the ONE response, no second fetch


def test_model_switch_new_instance_evaluates_fresh(monkeypatch):
    """Scroll #8: switching models means a NEW live instance (the
    established general_tab._save -> get_llm_backend() lifecycle); it must
    evaluate the new model on its own discovery, never inherit A's truth."""
    calls = []
    _install_discovery(monkeypatch, [_CAPABLE_ENTRY, _TEXT_ONLY_ENTRY], calls)

    instance_a = _bare_openrouter_backend(model="z-ai/glm-5.3-flash")
    assert instance_a.supports_vision_with_tools("z-ai/glm-5.3-flash") is True

    # "Switch" to a text-only model == brand-new backend instance.
    instance_b = _bare_openrouter_backend(model="meta-llama/llama-3.1-8b-instruct:free")
    assert instance_b.supports_vision_with_tools("meta-llama/llama-3.1-8b-instruct:free") is False
    assert instance_b.vision_tool_capability_state("meta-llama/llama-3.1-8b-instruct:free") == "unsupported"

    # The old instance's truth is untouched by the switch.
    assert instance_a.supports_vision_with_tools("z-ai/glm-5.3-flash") is True
    assert len(calls) == 2  # one discovery per instance, never shared


def test_repeated_capability_sensitive_calls_discover_exactly_once(monkeypatch):
    """Scroll #14: the hydration latch -- N capability-sensitive calls,
    exactly ONE discovery, then pure cache reads."""
    calls = []
    _install_discovery(monkeypatch, [_CAPABLE_ENTRY], calls)
    captured = _capture_post(monkeypatch, {"content": "ok"})

    backend = _bare_openrouter_backend()
    for _ in range(5):
        backend.chat(_VISION_MESSAGES, tools=_PRODUCT_TOOLS, tool_choice_mode=ToolChoiceMode.AUTO)
        assert captured["payload"].get("tools") == _PRODUCT_TOOLS
    assert len(calls) == 1


def test_failed_discovery_is_latched_no_uncontrolled_retry(monkeypatch):
    """Scroll #14 (failure side): a failed attempt never re-fires on later
    capability-sensitive calls -- no per-WORK-round hammering of a down
    provider."""
    attempts = []
    backend = _bare_openrouter_backend()
    def _failed():
        attempts.append(1)
        return ModelDiscoveryResult(ModelDiscoveryOutcome.FAILED,
                                    diagnostic="OpenRouter model discovery failed (ConnectionError).")
    backend.discover_models = _failed
    for _ in range(5):
        assert backend.supports_vision_with_tools("z-ai/glm-5.3-flash") is False
    assert len(attempts) == 1
    assert backend.vision_tool_capability_state("z-ai/glm-5.3-flash") == "unknown_discovery_failed"


# ── Unchanged behavior for non-capability-sensitive requests ───────────────

def test_text_only_request_performs_no_discovery_and_keeps_tools(monkeypatch):
    """Scroll #9: text-only WORK is byte-identical AND performs zero
    discovery -- the guard short-circuits before the capability question."""
    calls = []
    _install_discovery(monkeypatch, [_CAPABLE_ENTRY], calls)
    captured = _capture_post(monkeypatch, {"content": "ok"})

    backend = _bare_openrouter_backend()
    backend.chat([{"role": "user", "content": "what's 2+2?"}],
                 tools=_PRODUCT_TOOLS, tool_choice_mode=ToolChoiceMode.AUTO)
    assert calls == []  # no discovery on a text-only request
    assert captured["payload"].get("tools") == _PRODUCT_TOOLS


def test_tool_only_round_without_images_still_uses_cache_not_discovery(monkeypatch):
    """A text-only round whose history merely carries an OLD image keeps
    tools only via established truth; with truth established once, later
    rounds never re-discover (covered by the latch test) -- and a model
    with NO vision signal at all keeps the conservative drop."""
    calls = []
    _install_discovery(monkeypatch, [_TEXT_ONLY_ENTRY], calls)
    captured = _capture_post(monkeypatch, {"content": "ok"})

    backend = _bare_openrouter_backend(model="meta-llama/llama-3.1-8b-instruct:free")
    backend.chat(_VISION_MESSAGES, tools=_PRODUCT_TOOLS, tool_choice_mode=ToolChoiceMode.AUTO)
    assert captured["payload"].get("tools") is None
    assert captured["payload"].get("tool_choice") is None


# ── Notice truthfulness ────────────────────────────────────────────────────

def _notice_agent(backend, notices):
    return types.SimpleNamespace(llm=backend, on_commentary=lambda t: notices.append(t))


def test_notice_unknown_wording_when_discovery_failed():
    """Scroll #5/#11: when discovery failed, the notice must NOT claim the
    model 'doesn't support' the combination -- it must say UNKNOWN."""
    from core.agent import _maybe_emit_vision_tool_capability_notice
    backend = _bare_openrouter_backend()
    def _failed():
        return ModelDiscoveryResult(ModelDiscoveryOutcome.FAILED,
                                    diagnostic="OpenRouter model discovery failed (ConnectionError).")
    backend.discover_models = _failed
    notices = []
    fired = _maybe_emit_vision_tool_capability_notice(
        _notice_agent(backend, notices), _VISION_MESSAGES, _PRODUCT_TOOLS)
    assert fired is True
    assert len(notices) == 1
    assert "unknown" in notices[0].lower()
    assert "Unknown is not unsupported" in notices[0]
    assert "doesn't support combining tools with image input" not in notices[0]


def test_notice_historical_wording_for_truly_unsupported(monkeypatch):
    """A model the discovery response positively saw WITHOUT the required
    signals keeps the exact historical 'doesn't support' wording."""
    calls = []
    _install_discovery(monkeypatch, [_VISION_NO_TOOLS_ENTRY], calls)
    backend = _bare_openrouter_backend(model="vendor/vision-no-tools")
    backend.supports_vision_with_tools("vendor/vision-no-tools")  # hydrate
    notices = []
    fired = _maybe_emit_vision_tool_capability_notice(
        _notice_agent(backend, notices), _VISION_MESSAGES, _PRODUCT_TOOLS)
    assert fired is True
    assert "doesn't support combining tools with image input" in notices[0]
    assert "Unknown is not unsupported" not in notices[0]


def test_notice_silent_when_capable_after_hydration(monkeypatch):
    calls = []
    _install_discovery(monkeypatch, [_CAPABLE_ENTRY], calls)
    backend = _bare_openrouter_backend()
    notices = []
    fired = _maybe_emit_vision_tool_capability_notice(
        _notice_agent(backend, notices), _VISION_MESSAGES, _PRODUCT_TOOLS)
    assert fired is False
    assert notices == []


def test_other_backends_keep_historical_notice_wording():
    """Backends without the state seam (every non-OpenRouter backend today)
    keep the exact historical wording -- zero behavior change."""
    from core.agent import _maybe_emit_vision_tool_capability_notice
    fake_llm = types.SimpleNamespace(
        display_name="LM Studio", name="lmstudio",
        configured_model=lambda: "local-vision-model",
        supports_vision_with_tools=lambda model=None: False,
    )
    notices = []
    fired = _maybe_emit_vision_tool_capability_notice(
        _notice_agent(fake_llm, notices), _VISION_MESSAGES, _PRODUCT_TOOLS)
    assert fired is True
    assert "doesn't support combining tools with image input" in notices[0]


# ── Boundaries: no backend switch, no persistence, no M1 coupling ─────────

def test_hydration_never_touches_loader_or_switches_backend(monkeypatch):
    """Scroll #12: capability hydration must never construct or select a
    different backend."""
    import core.backends.loader as loader
    def _forbidden(*a, **k):
        raise AssertionError("loader.get_llm_backend must not be called by hydration")
    monkeypatch.setattr(loader, "get_llm_backend", _forbidden)
    calls = []
    _install_discovery(monkeypatch, [_CAPABLE_ENTRY], calls)
    backend = _bare_openrouter_backend()
    assert backend.supports_vision_with_tools("z-ai/glm-5.3-flash") is True


def test_hydration_performs_no_persistence_or_memory_writes(monkeypatch):
    """Scroll #17: the repair writes nothing outside the instance."""
    def _forbidden(*a, **k):
        raise AssertionError("persistence writes are forbidden in the hydration path")
    monkeypatch.setattr("core.persistence.save", _forbidden)
    calls = []
    _install_discovery(monkeypatch, [_CAPABLE_ENTRY], calls)
    backend = _bare_openrouter_backend()
    assert backend.supports_vision_with_tools("z-ai/glm-5.3-flash") is True


def test_m1_capability_router_stays_byte_uncoupled():
    """Scroll #7/#18: the MB-34 repair must not import or reference the M1
    capability registry -- specialist routing truth and the legacy
    primary-backend transport guard are different architectural concepts."""
    source = (Path(__file__).parent.parent / "core" / "backends" / "openrouter.py").read_text()
    assert "capability_router" not in source
    source_agent = (Path(__file__).parent.parent / "core" / "agent.py").read_text()
    assert "capability_router" not in source_agent


# ── State machine + partial-construction robustness ────────────────────────

def test_capability_state_full_lifecycle(monkeypatch):
    calls = []
    backend = _bare_openrouter_backend()
    assert backend.vision_tool_capability_state("z-ai/glm-5.3-flash") == "unknown_not_discovered"
    _install_discovery(monkeypatch, [_CAPABLE_ENTRY, _TEXT_ONLY_ENTRY], calls)
    backend.discover_models()
    assert backend.vision_tool_capability_state("z-ai/glm-5.3-flash") == "supported"
    assert backend.vision_tool_capability_state("meta-llama/llama-3.1-8b-instruct:free") == "unsupported"
    assert backend.vision_tool_capability_state("vendor/unlisted-model") == "unknown_model_absent"
    assert backend.vision_tool_capability_state(None) == "unknown_not_discovered"


def test_hydration_robust_on_partially_constructed_instances(monkeypatch):
    """The established __new__ test-double convention bypasses __init__;
    the hydration path must lazily create its own ledger state rather than
    AttributeError on instances lacking it."""
    calls = []
    _install_discovery(monkeypatch, [_CAPABLE_ENTRY], calls)
    backend = OpenRouterBackend.__new__(OpenRouterBackend)
    backend.base_url = "https://openrouter.ai/api/v1"
    backend.headers = {"Content-Type": "application/json", "Authorization": "Bearer test"}
    backend._model = "z-ai/glm-5.3-flash"
    backend._reasoning_cache = {}
    backend._reasoning_cache_ready = False
    backend._vision_tool_cache = {}
    # NOTE: no _vision_tool_discovery / _discovered_model_ids attributes.
    assert backend.supports_vision_with_tools("z-ai/glm-5.3-flash") is True
    assert backend.vision_tool_capability_state("z-ai/glm-5.3-flash") == "supported"