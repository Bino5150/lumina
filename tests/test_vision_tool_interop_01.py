"""
VISION-TOOL-INTEROP-01 -- capability-aware vision+tools contract.

Provenance: core/backends/lmstudio.py's has_vision guard
(`any(isinstance(m.get("content"), list) for m in messages)`) has existed
unmodified since the very first commit (6c0b78d, 2026-06-12) -- a blanket,
backend-wide default that silently drops tools/tool_choice the moment ANY
message anywhere in the request (the whole accumulated history, not just
the current turn) carries multipart content. Because a real image block
stays in ctx.history indefinitely (only tool-role messages get their image
blocks stripped, see core/context.py's _strip_image_blocks()), one image
anywhere earlier in a chat permanently disables product tools for every
later request in that chat, including a purely text-only follow-up turn.
This predates VISION-MULTI-IMAGE-01/AGENT-GLM-COMPLETION-GATE-01/02 -- it
is a pre-existing defect those tickets exposed, not one they introduced.

Real live evidence (see the campaign report's full trial table) confirms
OpenRouter routing to z-ai/glm-5.3-flash handles image content and tools
(both tool_choice="auto" and "required") together correctly in the SAME
request -- HTTP 200, correct tool-call decisions in every trial.

Fix: BaseLLMBackend gains supports_vision_with_tools(model) (default
False, same fail-safe posture as reasoning_capabilities()).
LMStudioBackend.chat()'s has_vision gate now checks it before dropping
tools. OpenRouterBackend overrides it using per-model capability data
OpenRouter's own /models response already provides
(architecture.input_modalities + supported_parameters) via the SAME
discover_models() cache reasoning-capability discovery already
populates -- no new HTTP call. Every other LMStudioBackend descendant
(DeepSeek, Groq, Kimi, llama.cpp, OmniRoute, OpenAI, Qwen, vLLM, Custom,
LM Studio itself) has no override, so supports_vision_with_tools() stays
False for them -- byte-identical existing suppression behavior, now
surfaced honestly via core/agent.py's capability notice instead of a
silent drop.
"""
import types

import pytest

from core.agent import (
    LuminaAgent, FINISH_TOOL_WORK_NAME,
    _maybe_emit_vision_tool_capability_notice,
)
from core.backends.base import BaseLLMBackend, ToolChoiceMode
from core.backends.lmstudio import LMStudioBackend
from core.backends.openrouter import OpenRouterBackend
from core.context import ContextManager


@pytest.fixture(autouse=True)
def _no_skill_injection(monkeypatch):
    monkeypatch.setattr("core.agent.build_skills_block", lambda user_input: "")


def _tc(name, call_id=None):
    return {"id": call_id or name, "type": "function",
            "function": {"name": name, "arguments": "{}"}}


_IMAGE = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
_IMAGE_2 = {"type": "image_url", "image_url": {"url": "data:image/png;base64,BBBB"}}
_PRODUCT_TOOLS = [{"type": "function", "function": {
    "name": "read_file", "description": "", "parameters": {}}}]


def _openrouter_backend(vision_tool_models=()):
    backend = OpenRouterBackend.__new__(OpenRouterBackend)
    backend.base_url = "https://openrouter.ai/api/v1"
    backend.headers = {"Content-Type": "application/json", "Authorization": "Bearer test"}
    backend._model = "z-ai/glm-5.3-flash"
    backend._reasoning_cache = {}
    backend._reasoning_cache_ready = False
    backend._vision_tool_cache = {m: True for m in vision_tool_models}
    return backend


class _FakeResp:
    def __init__(self, message):
        self.status_code = 200
        self._message = message
    def raise_for_status(self):
        pass
    def json(self):
        return {"choices": [{"message": self._message}]}


# ── supports_vision_with_tools() -- base default + OpenRouter override ─────

def test_base_backend_default_is_false_for_any_model():
    class _Bare(BaseLLMBackend):
        def get_model(self): return "m"
        def list_models(self): return ["m"]
        def health_check(self): return True, "ok"
        def chat(self, *a, **k): return {}
        def chat_stream(self, *a, **k): yield ""
    b = _Bare()
    assert b.supports_vision_with_tools("any-model") is False
    assert b.supports_vision_with_tools(None) is False


def test_openrouter_reports_true_only_for_a_discovered_capable_model():
    backend = _openrouter_backend(vision_tool_models=["z-ai/glm-5.3-flash"])
    assert backend.supports_vision_with_tools("z-ai/glm-5.3-flash") is True
    assert backend.supports_vision_with_tools("some/other-model") is False
    assert backend.supports_vision_with_tools(None) is False


def test_openrouter_parses_vision_tool_capability_from_models_entry():
    capable_entry = {
        "id": "vendor/capable-model",
        "architecture": {"input_modalities": ["text", "image"]},
        "supported_parameters": ["tools", "tool_choice", "temperature"],
    }
    text_only_entry = {
        "id": "vendor/text-only-model",
        "architecture": {"input_modalities": ["text"]},
        "supported_parameters": ["tools", "tool_choice"],
    }
    no_tools_entry = {
        "id": "vendor/vision-no-tools",
        "architecture": {"input_modalities": ["text", "image"]},
        "supported_parameters": ["temperature"],
    }
    malformed_entry = {"id": "vendor/malformed", "architecture": None, "supported_parameters": "not-a-list"}

    assert OpenRouterBackend._parses_vision_tool_capability(capable_entry) is True
    assert OpenRouterBackend._parses_vision_tool_capability(text_only_entry) is False
    assert OpenRouterBackend._parses_vision_tool_capability(no_tools_entry) is False
    assert OpenRouterBackend._parses_vision_tool_capability(malformed_entry) is False


def test_discover_models_populates_vision_tool_cache_from_the_same_response(monkeypatch):
    """One HTTP call populates BOTH _reasoning_cache and _vision_tool_cache
    -- no separate fetch for vision+tools capability."""
    import requests

    backend = _openrouter_backend()

    class _ModelsResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"data": [
                {"id": "z-ai/glm-5.3-flash",
                 "architecture": {"input_modalities": ["text", "image"]},
                 "supported_parameters": ["tools", "tool_choice"],
                 "reasoning": {"mandatory": True, "supported_efforts": ["low", "high"]}},
                {"id": "meta-llama/llama-3.1-8b-instruct:free",
                 "architecture": {"input_modalities": ["text"]},
                 "supported_parameters": ["tools"]},
            ]}

    calls = []
    def _fake_get(url, headers=None, timeout=None):
        calls.append(url)
        return _ModelsResp()
    monkeypatch.setattr(requests, "get", _fake_get)

    result = backend.discover_models()

    assert len(calls) == 1  # one fetch, not two
    assert backend.supports_vision_with_tools("z-ai/glm-5.3-flash") is True
    assert backend.supports_vision_with_tools("meta-llama/llama-3.1-8b-instruct:free") is False
    assert backend.reasoning_capabilities("z-ai/glm-5.3-flash").mandatory is True


# ── LMStudioBackend.chat() wire-level: capability gates the has_vision drop ─

def test_unsupported_backend_still_drops_tools_on_vision_turn(monkeypatch):
    """Unchanged existing behavior for every backend without a
    supports_vision_with_tools() override (DeepSeek/Groq/Kimi/llama.cpp/
    OmniRoute/OpenAI/Qwen/vLLM/Custom/LM Studio itself) -- no regression."""
    import requests

    backend = LMStudioBackend.__new__(LMStudioBackend)
    backend.base_url = "http://localhost:1234/v1"
    backend.api_key = "lm-studio"
    backend.headers = {"Content-Type": "application/json", "Authorization": "Bearer lm-studio"}
    backend._model = "local-vision-model"

    captured = {}
    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["payload"] = json
        return _FakeResp({"content": "a description"})
    monkeypatch.setattr(requests, "post", _fake_post)

    messages = [{"role": "user", "content": [_IMAGE, {"type": "text", "text": "what is this?"}]}]
    backend.chat(messages, tools=_PRODUCT_TOOLS, tool_choice_mode=ToolChoiceMode.AUTO)

    assert "tools" not in captured["payload"]
    assert "tool_choice" not in captured["payload"]


def test_capable_backend_keeps_tools_on_a_current_turn_image(monkeypatch):
    import requests

    backend = _openrouter_backend(vision_tool_models=["z-ai/glm-5.3-flash"])
    captured = {}
    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["payload"] = json
        return _FakeResp({"content": "a description"})
    monkeypatch.setattr(requests, "post", _fake_post)

    messages = [{"role": "user", "content": [_IMAGE, {"type": "text", "text": "what is this?"}]}]
    backend.chat(messages, tools=_PRODUCT_TOOLS, tool_choice_mode=ToolChoiceMode.AUTO)

    assert captured["payload"]["tools"] == _PRODUCT_TOOLS
    assert captured["payload"]["tool_choice"] == "auto"


def test_capable_backend_keeps_tools_with_multiple_current_turn_images(monkeypatch):
    import requests

    backend = _openrouter_backend(vision_tool_models=["z-ai/glm-5.3-flash"])
    captured = {}
    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["payload"] = json
        return _FakeResp({"content": "two descriptions"})
    monkeypatch.setattr(requests, "post", _fake_post)

    messages = [{"role": "user", "content": [_IMAGE, _IMAGE_2, {"type": "text", "text": "compare these"}]}]
    backend.chat(messages, tools=_PRODUCT_TOOLS, tool_choice_mode=ToolChoiceMode.REQUIRED)

    assert captured["payload"]["tools"] == _PRODUCT_TOOLS
    assert captured["payload"]["tool_choice"] == "required"
    # Image ordering/content unchanged in the actual wire payload.
    sent_content = captured["payload"]["messages"][0]["content"]
    assert sent_content == [_IMAGE, _IMAGE_2, {"type": "text", "text": "compare these"}]


def test_capable_backend_keeps_tools_on_a_historical_image_text_only_turn(monkeypatch):
    """The core bug: a purely text-only CURRENT turn must not lose tools
    just because an EARLIER turn in the same request's message list
    carried an image -- has_vision scans the whole list, and this fix
    must cover that scope exactly, not just the current-turn shape."""
    import requests

    backend = _openrouter_backend(vision_tool_models=["z-ai/glm-5.3-flash"])
    captured = {}
    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["payload"] = json
        return _FakeResp({"content": "42"})
    monkeypatch.setattr(requests, "post", _fake_post)

    messages = [
        {"role": "user", "content": [_IMAGE, {"type": "text", "text": "what is this?"}]},
        {"role": "assistant", "content": "It's a small icon."},
        {"role": "user", "content": "separately, what's 6*7?"},
    ]
    backend.chat(messages, tools=_PRODUCT_TOOLS, tool_choice_mode=ToolChoiceMode.AUTO)

    assert captured["payload"]["tools"] == _PRODUCT_TOOLS
    assert captured["payload"]["tool_choice"] == "auto"


def test_incapable_model_on_openrouter_still_drops_tools(monkeypatch):
    """Capability is per-model, not backend-wide -- an OpenRouter instance
    whose CONFIGURED model was never confirmed (empty/different cache)
    keeps the safe suppression default."""
    import requests

    backend = _openrouter_backend(vision_tool_models=["some/other-capable-model"])
    assert backend._model == "z-ai/glm-5.3-flash"  # not in the capable set above
    captured = {}
    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["payload"] = json
        return _FakeResp({"content": "a description"})
    monkeypatch.setattr(requests, "post", _fake_post)

    messages = [{"role": "user", "content": [_IMAGE, {"type": "text", "text": "what is this?"}]}]
    backend.chat(messages, tools=_PRODUCT_TOOLS, tool_choice_mode=ToolChoiceMode.AUTO)

    assert "tools" not in captured["payload"]


def test_text_only_turn_never_affected_either_way(monkeypatch):
    """Sanity: a chat with no vision anywhere gets tools regardless of
    capability -- this fix only ever changes the has_vision=True branch."""
    import requests

    backend = LMStudioBackend.__new__(LMStudioBackend)
    backend.base_url = "http://localhost:1234/v1"
    backend.api_key = "lm-studio"
    backend.headers = {"Content-Type": "application/json", "Authorization": "Bearer lm-studio"}
    backend._model = "local-model"
    captured = {}
    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["payload"] = json
        return _FakeResp({"content": "42"})
    monkeypatch.setattr(requests, "post", _fake_post)

    backend.chat([{"role": "user", "content": "6*7?"}], tools=_PRODUCT_TOOLS,
                 tool_choice_mode=ToolChoiceMode.AUTO)

    assert captured["payload"]["tools"] == _PRODUCT_TOOLS


# ── Images cannot authorize tool execution ──────────────────────────────────

def test_vision_never_expands_the_offered_tool_set(monkeypatch):
    """The set of tools offered is identical whether or not an image is
    present -- this fix only changes WHETHER tools are sent, never WHICH
    ones. An image must never itself grant access to anything beyond the
    caller-supplied `tools` list."""
    import requests

    backend = _openrouter_backend(vision_tool_models=["z-ai/glm-5.3-flash"])
    captured = []
    def _fake_post(url, headers=None, json=None, timeout=None):
        captured.append(json)
        return _FakeResp({"content": "ok"})
    monkeypatch.setattr(requests, "post", _fake_post)

    backend.chat([{"role": "user", "content": "text only"}],
                 tools=_PRODUCT_TOOLS, tool_choice_mode=ToolChoiceMode.AUTO)
    backend.chat([{"role": "user", "content": [_IMAGE, {"type": "text", "text": "with image"}]}],
                 tools=_PRODUCT_TOOLS, tool_choice_mode=ToolChoiceMode.AUTO)

    assert captured[0]["tools"] == captured[1]["tools"] == _PRODUCT_TOOLS


# ── core/agent.py: explicit capability notice instead of silence ───────────

def test_notice_helper_fires_for_unsupported_backend_with_vision_and_tools():
    fake_llm = types.SimpleNamespace(
        display_name="LM Studio", name="lmstudio",
        configured_model=lambda: "local-vision-model",
        supports_vision_with_tools=lambda model=None: False,
    )
    notices = []
    fake_agent = types.SimpleNamespace(
        llm=fake_llm, on_commentary=lambda text: notices.append(text),
    )
    messages = [{"role": "user", "content": [_IMAGE, {"type": "text", "text": "hi"}]}]

    fired = _maybe_emit_vision_tool_capability_notice(fake_agent, messages, _PRODUCT_TOOLS)

    assert fired is True
    assert len(notices) == 1
    assert "LM Studio" in notices[0]
    assert "doesn't support combining tools with image input" in notices[0]


def test_notice_helper_does_not_fire_for_a_capable_backend():
    fake_llm = types.SimpleNamespace(
        display_name="OpenRouter", name="openrouter",
        configured_model=lambda: "z-ai/glm-5.3-flash",
        supports_vision_with_tools=lambda model=None: True,
    )
    notices = []
    fake_agent = types.SimpleNamespace(
        llm=fake_llm, on_commentary=lambda text: notices.append(text),
    )
    messages = [{"role": "user", "content": [_IMAGE, {"type": "text", "text": "hi"}]}]

    fired = _maybe_emit_vision_tool_capability_notice(fake_agent, messages, _PRODUCT_TOOLS)

    assert fired is False
    assert notices == []


def test_notice_helper_does_not_fire_without_vision_or_without_tools():
    fake_llm = types.SimpleNamespace(
        display_name="LM Studio", name="lmstudio",
        configured_model=lambda: "local-model",
        supports_vision_with_tools=lambda model=None: False,
    )
    notices = []
    fake_agent = types.SimpleNamespace(llm=fake_llm, on_commentary=lambda text: notices.append(text))

    # No vision at all.
    assert _maybe_emit_vision_tool_capability_notice(
        fake_agent, [{"role": "user", "content": "plain text"}], _PRODUCT_TOOLS) is False
    # Vision present but no tools offered this round (nothing would be dropped).
    assert _maybe_emit_vision_tool_capability_notice(
        fake_agent, [{"role": "user", "content": [_IMAGE, {"type": "text", "text": "hi"}]}], []) is False
    assert notices == []


def test_notice_fires_once_per_turn_not_once_per_work_round():
    """End-to-end through LuminaAgent.chat(): a turn that runs two WORK
    rounds (one real tool call, then a finish) on an unsupported backend
    with vision history must surface the capability notice exactly once,
    not once per round."""
    class _ScriptedLLM:
        display_name = "LM Studio"
        name = "lmstudio"
        supports_required_tool_choice = False

        def __init__(self):
            self.call_count = 0

        def get_model(self): return "local-vision-model"
        def configured_model(self): return "local-vision-model"
        def supports_vision_with_tools(self, model=None): return False

        def chat(self, messages, tools=None, max_tokens=None, reasoning_effort=None,
                 tool_choice_mode=None):
            idx = self.call_count
            self.call_count += 1
            return {"_turn": idx}

        def extract_message(self, response):
            idx = response["_turn"]
            if idx == 0:
                return {"role": "assistant", "content": "",
                        "tool_calls": [_tc("search_memory")]}
            return {"role": "assistant", "content": "final description"}

        def extract_termination(self, response):
            from core.backends.base import TerminationStatus
            return TerminationStatus.COMPLETE

        def is_tool_call(self, message):
            return bool(message.get("tool_calls"))

        def get_tool_calls(self, message):
            return message.get("tool_calls", [])

        def parse_tool_call(self, tc):
            return tc["function"]["name"], {}

        def chat_stream(self, messages, max_tokens=None, reasoning_effort=None):
            yield "final description"

    llm = _ScriptedLLM()
    ctx = ContextManager(owner=False)
    notices = []
    registry = types.SimpleNamespace(
        schema_token_estimate=lambda: 0,
        get_schemas=lambda: _PRODUCT_TOOLS,
        list_enabled=lambda: ["read_file"],
        all_tool_names=lambda: ["read_file"],
        call=lambda name, args: "ok",
    )
    fake = types.SimpleNamespace(
        llm=llm, ctx=ctx, registry=registry,
        on_tool_call=lambda name, args: None, on_tool_result=lambda name, result: None,
        on_think_start=lambda step: None, on_think_token=lambda tok: None, on_think_end=lambda: None,
        on_response_token=lambda tok: None,
        on_commentary=lambda text: notices.append(text),
        tts=None, _session_tool_calls=0, _skill_nudge_sent=False,
    )
    fake._stream_final = types.MethodType(LuminaAgent._stream_final, fake)
    fake._finalize_completion_candidate = types.MethodType(LuminaAgent._finalize_completion_candidate, fake)

    LuminaAgent.chat(fake, [_IMAGE, {"type": "text", "text": "look this up and describe it"}])

    capability_notices = [n for n in notices if "doesn't support combining tools" in n]
    assert len(capability_notices) == 1
