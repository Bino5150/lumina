"""
SUBSCRIPTION-PLAN-BACKENDS-01B -- fuel isolation (scroll section 18.E, "the
big one").

Law under test: ONE OPERATION, ONE LANE, ONE AUTH SOURCE, ONE FUEL SOURCE.
Failure is not authorization to spend from another tank.

Setup, every test: paid-looking credentials are deliberately populated for
OpenAI, Anthropic, OpenRouter and Gemini (a fall-through would have
something real-looking to use); the selected lane is the reserved
"openai_chatgpt_plan"; the plan backend is a failing/absent stub; and a
NETWORK TRIPWIRE records -- and refuses -- every outbound request and every
DNS lookup. The expected counts are asserted exactly:

    plan stub dispatches  -> exactly as many as the lane policy allows
    OpenAI / Anthropic / OpenRouter / Gemini API requests -> 0
    network of any kind   -> 0

No OAuth, no live plan call, no real network, no real credential. The plan
lane has no implementation in 01B, so "enabled" scenarios simulate a LATER
slice having admitted an operation on the lane (by swapping the descriptor
table) purely to prove that, once enabled, retries / continuations / final
generation / Reforge stay on that lane.
"""
import ast
import dataclasses
import json
import os
import socket
import sqlite3
import threading
import types
from types import MappingProxyType

import pytest
import requests

import config
import core.backend_identity as bi
import core.capability_router as cr
import tools.memory as memory
import tools.palace as palace
from core.agent import (CONTINUE_TOOL_WORK_NAME, FINISH_TOOL_WORK_NAME, LuminaAgent,
                        is_error_response)
from core.backend_identity import OperationKind, ReservedBackendLaneError
from core.backends import loader
from core.backends.base import BaseLLMBackend, TerminationStatus
from core.flight_recorder import FlightRecorder

PLAN = bi.OPENAI_CHATGPT_PLAN_LANE
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# Network tripwire + populated paid credentials
# ---------------------------------------------------------------------------

class _Tripwire:
    def __init__(self):
        self.requests = []
        self.dns = []

    @property
    def total(self):
        return len(self.requests) + len(self.dns)

    def api_requests(self):
        hosts = ("api.openai.com", "api.anthropic.com", "openrouter.ai",
                 "generativelanguage.googleapis.com")
        return [(m, u) for m, u in self.requests if any(h in u for h in hosts)]


@pytest.fixture
def tripwire(monkeypatch):
    tw = _Tripwire()

    def _request(self, method, url, *args, **kwargs):
        tw.requests.append((method, str(url)))
        raise requests.ConnectionError("network tripwire: no outbound request in 01B tests")

    def _getaddrinfo(host, *args, **kwargs):
        tw.dns.append(str(host))
        raise OSError("network tripwire: no DNS in 01B tests")

    monkeypatch.setattr(requests.sessions.Session, "request", _request)
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo)
    return tw


@pytest.fixture(autouse=True)
def paid_credentials(monkeypatch):
    """Real-looking paid credentials in the (isolated) process config, and the
    plan lane selected. If any path fell through to a paid lane, there is a
    key sitting right there for it to use."""
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-proj-" + "A" * 40)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-api03-" + "B" * 40)
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-or-v1-" + "C" * 40)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "AIza" + "D" * 35)
    monkeypatch.setattr(config, "LLM_BACKEND", PLAN)


@pytest.fixture
def plan_operations_enabled(monkeypatch):
    """Simulate a LATER slice having admitted every operation on the plan lane
    (01E/01F will do this one capability at a time, each after live
    acceptance). Used only to prove behavior once the lane is enabled."""
    open_plan = dataclasses.replace(bi.LANES[PLAN], admitted_operations=frozenset(OperationKind))
    monkeypatch.setattr(bi, "LANES", MappingProxyType({**bi.LANES, PLAN: open_plan}))


# ---------------------------------------------------------------------------
# Agent-loop doubles
# ---------------------------------------------------------------------------

def _tc(name, call_id=None):
    return {"id": call_id or name, "type": "function",
            "function": {"name": name, "arguments": "{}"}}


class _ScriptedLLM:
    """Scripted agent-facing backend. `name` is the lane it claims."""

    display_name = "FakeProvider"

    def __init__(self, name, turns=(), stream_raises=None):
        self.name = name
        self.turns = list(turns)
        self.chat_calls = 0
        self.stream_calls = 0
        self._stream_raises = stream_raises

    def get_model(self):
        return "fake-model"

    def configured_model(self):
        return "fake-model-configured"

    def chat(self, messages, tools=None, max_tokens=None, reasoning_effort=None, tool_choice_mode=None):
        idx = self.chat_calls
        self.chat_calls += 1
        turn = self.turns[idx]
        if "raise" in turn:
            raise turn["raise"]
        return {"_turn": idx}

    def extract_message(self, response):
        turn = self.turns[response["_turn"]]
        message = {"role": "assistant", "content": turn.get("content", "")}
        if "tool_calls" in turn:
            message["tool_calls"] = turn["tool_calls"]
        return message

    def extract_termination(self, response):
        return self.turns[response["_turn"]].get("termination", TerminationStatus.UNKNOWN)

    def extract_reasoning(self, response):
        return None

    def is_tool_call(self, message):
        return bool(message.get("tool_calls"))

    def get_tool_calls(self, message):
        return message.get("tool_calls", [])

    def parse_tool_call(self, tc):
        return tc["function"]["name"], {}

    def chat_stream(self, messages, max_tokens=None, reasoning_effort=None):
        self.stream_calls += 1
        if self._stream_raises is not None:
            raise self._stream_raises
        yield "final streamed response"


class _ForbiddenAPI(_ScriptedLLM):
    """A paid API lane that must never be dispatched: any chat/stream is a
    test failure AND is counted so assertions can state the exact zero."""

    def chat(self, *a, **k):
        self.chat_calls += 1
        raise AssertionError(f"paid lane {self.name!r} was dispatched")

    def chat_stream(self, *a, **k):
        self.stream_calls += 1
        raise AssertionError(f"paid lane {self.name!r} was streamed")
        yield  # pragma: no cover


def _agent(llm, tmp_path, on_tool=None):
    history = []

    def registry_call(name, args):
        if on_tool is not None:
            on_tool(name)
        return "ok"

    ctx = types.SimpleNamespace(
        history=history, max_tokens=8000,
        add_user=lambda content, source="OWNER_DIRECT": history.append({"role": "user", "content": content}),
        add_assistant=lambda content: history.append({"role": "assistant", "content": content}),
        add_tool_call=lambda message: history.append(message),
        add_tool_result=lambda tool_call_id, name, result: history.append(
            {"role": "tool", "tool_call_id": tool_call_id, "name": name, "content": result}),
        add_cancelled_tool_result=lambda tool_call_id, name: history.append(
            {"role": "tool", "tool_call_id": tool_call_id, "name": name, "content": "[cancelled]"}),
        push_ephemeral=lambda block: None,
        build_messages=lambda tool_budget=0, chat_id=None: [],
        context_usage_snapshot=lambda tool_budget=0, chat_id=None, refresh=False: (
            {"used_tokens": 1, "max_tokens": 8000, "percent": 0.1, "chat_id": chat_id}),
    )
    registry = types.SimpleNamespace(
        schema_token_estimate=lambda: 0, get_schemas=lambda: [], list_enabled=lambda: [],
        all_tool_names=lambda: [], call=registry_call,
    )
    tokens = []
    ns = types.SimpleNamespace(
        llm=llm, ctx=ctx, registry=registry, channel_id="t", owner=True,
        on_tool_call=lambda n, a: None, on_tool_result=lambda n, r: None,
        on_think_start=lambda s: None, on_think_token=lambda t: None, on_think_end=lambda: None,
        on_response_token=lambda t: tokens.append(t), on_commentary=lambda t: None,
        tts=None, _session_tool_calls=0, _skill_nudge_sent=False,
        flight_recorder=FlightRecorder(db_path=str(tmp_path / "fr.db")),
    )
    ns.streamed = tokens
    ns._stream_final = types.MethodType(LuminaAgent._stream_final, ns)
    ns._finalize_completion_candidate = types.MethodType(LuminaAgent._finalize_completion_candidate, ns)
    return ns


def _events(agent, event_type=None):
    conn = sqlite3.connect(agent.flight_recorder.db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY seq")]
    conn.close()
    return [r for r in rows if event_type is None or r["event_type"] == event_type]


def _no_fuel_leak(tw, *paid):
    assert tw.requests == [], f"outbound requests: {tw.requests}"
    assert tw.dns == [], f"DNS lookups: {tw.dns}"
    for duck in paid:
        assert duck.chat_calls == 0 and duck.stream_calls == 0, duck.name


# ---------------------------------------------------------------------------
# The tripwire itself is not vacuous
# ---------------------------------------------------------------------------

def test_canary_the_tripwire_catches_a_real_paid_lane_dispatch(tripwire):
    """If anything DID fall through to the OpenAI API lane, this is what it
    would look like -- and the tripwire sees it. Without this, every zero
    below could be a broken instrument."""
    backend = loader.get_llm_backend("openai")
    with pytest.raises(Exception):
        backend.chat(messages=[{"role": "user", "content": "hi"}])
    assert any("api.openai.com" in u for _m, u in tripwire.requests)
    assert tripwire.api_requests()


def test_canary_anthropic_lane_is_also_visible_to_the_tripwire(tripwire):
    backend = loader.get_llm_backend("anthropic")
    with pytest.raises(Exception):
        backend.chat(messages=[{"role": "user", "content": "hi"}])
    assert any("api.anthropic.com" in u for _m, u in tripwire.requests)


# ---------------------------------------------------------------------------
# Construction: no path builds a paid backend for the plan lane
# ---------------------------------------------------------------------------

def test_no_construction_path_falls_through_to_a_paid_backend(tripwire):
    for kwargs in ({}, {"name": PLAN}, {"name": PLAN.upper()}, {"name": PLAN, "api_key": "sk-" + "z" * 24}):
        assert type(loader.get_llm_backend(**kwargs)).__name__ == "ChatGPTPlanBackend"
    assert type(LuminaAgent(owner=False, backend=PLAN).llm).__name__ == "ChatGPTPlanBackend"
    assert type(LuminaAgent(owner=False).llm).__name__ == "ChatGPTPlanBackend"
    assert tripwire.total == 0


# ---------------------------------------------------------------------------
# Foreground dispatch
# ---------------------------------------------------------------------------

def test_foreground_admission_keeps_nonforeground_operations_refused(tripwire):
    assert bi.operation_refusal(PLAN, OperationKind.FOREGROUND_CHAT) is None
    for kind in OperationKind:
        if kind is not OperationKind.FOREGROUND_CHAT:
            assert bi.operation_refusal(PLAN, kind)
    assert tripwire.total == 0


def test_foreground_failing_plan_attempts_exactly_once_and_never_leaves_the_lane(
        tripwire, tmp_path, plan_operations_enabled):
    plan = _ScriptedLLM(PLAN, [{"raise": RuntimeError("plan route unavailable")}])
    paid = [_ForbiddenAPI(n) for n in ("openai", "anthropic", "openrouter")]
    agent = _agent(plan, tmp_path)

    result = LuminaAgent.chat(agent, "hello")

    assert is_error_response(result)
    assert plan.chat_calls == 1                      # "attempted as expected"
    _no_fuel_leak(tripwire, *paid)
    dispatch = _events(agent, "provider.dispatch")
    assert [e["backend"] for e in dispatch] == [PLAN]
    assert json.loads(dispatch[0]["fields_json"])["backend_lane"] == PLAN
    failed = _events(agent, "provider.dispatch_failed")
    assert [e["backend"] for e in failed] == [PLAN]
    started = json.loads(_events(agent, "turn.started")[0]["fields_json"])
    assert started["provider_family"] == "openai" and started["backend_lane"] == PLAN
    assert started["operation_kind"] == "foreground_chat"
    assert started["cost_class"] == "unknown_debit"


# ---------------------------------------------------------------------------
# Retry / tool continuation / gate / final generation stay in lane
# ---------------------------------------------------------------------------

def test_tool_continuation_failure_stays_in_lane(tripwire, tmp_path, plan_operations_enabled):
    plan = _ScriptedLLM(PLAN, [
        {"tool_calls": [_tc("search_memory")]},
        {"raise": ConnectionError("plan connection dropped")},
    ])
    paid = [_ForbiddenAPI(n) for n in ("openai", "anthropic", "openrouter")]
    agent = _agent(plan, tmp_path)

    result = LuminaAgent.chat(agent, "find it")

    assert "before a response was received" in result or "before a response" in result
    assert plan.chat_calls == 2                      # the tool round AND the continuation
    _no_fuel_leak(tripwire, *paid)
    assert {e["backend"] for e in _events(agent, "provider.dispatch")} == {PLAN}


def test_full_loop_gate_continue_retry_and_streamed_final_all_dispatch_on_the_plan_lane(
        tripwire, tmp_path, plan_operations_enabled):
    plan = _ScriptedLLM(PLAN, [
        {"tool_calls": [_tc("search_memory")]},
        {"content": "hmm", "termination": TerminationStatus.COMPLETE},      # WORK -> gate
        {"tool_calls": [_tc(CONTINUE_TOOL_WORK_NAME)]},                      # GATE continue
        {"tool_calls": [_tc("read_file")]},                                  # WORK again
        {"content": "", "termination": TerminationStatus.COMPLETE},          # WORK -> gate
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},                        # GATE finish
    ])
    paid = [_ForbiddenAPI(n) for n in ("openai", "anthropic", "openrouter")]
    agent = _agent(plan, tmp_path)

    result = LuminaAgent.chat(agent, "find and read it")

    assert result == "final streamed response"
    assert plan.chat_calls == 6 and plan.stream_calls == 1
    _no_fuel_leak(tripwire, *paid)
    assert {e["backend"] for e in _events(agent, "provider.dispatch")} == {PLAN}
    assert agent._turn_dispatch_identity.backend_lane == PLAN


def test_failed_final_generation_does_not_retry_on_another_backend(
        tripwire, tmp_path, plan_operations_enabled):
    plan = _ScriptedLLM(PLAN, [
        {"tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ], stream_raises=ConnectionError("plan stream dropped"))
    paid = [_ForbiddenAPI(n) for n in ("openai", "anthropic", "openrouter")]
    agent = _agent(plan, tmp_path)

    result = LuminaAgent.chat(agent, "go")

    assert result.startswith("[Stream error:")
    assert plan.stream_calls == 1
    _no_fuel_leak(tripwire, *paid)


# ---------------------------------------------------------------------------
# D. Dispatch immutability: a lane swap mid-operation is refused
# ---------------------------------------------------------------------------

def test_lane_swapped_to_a_paid_backend_mid_turn_is_refused_not_dispatched(
        tripwire, tmp_path, plan_operations_enabled):
    """Simulates the bug class this slice exists to prevent: some future
    fallback code replaces agent.llm after the plan backend fails. The
    operation began on the plan lane; the paid lane must never be reached."""
    openai = _ForbiddenAPI("openai")
    plan = _ScriptedLLM(PLAN, [{"tool_calls": [_tc("search_memory")]}])
    holder = {}
    agent = _agent(plan, tmp_path, on_tool=lambda name: holder["agent"].__setattr__("llm", openai))
    holder["agent"] = agent

    result = LuminaAgent.chat(agent, "find it")

    assert "does not change fuel" in result
    assert plan.chat_calls == 1
    _no_fuel_leak(tripwire, openai)
    refused = _events(agent, "provider.dispatch_refused")
    assert [e["backend"] for e in refused] == ["openai"]       # attributed to what was REFUSED
    # The operator is told the tool already ran; the sentinel still classifies
    # as a failed turn (never mistaken for assistant content).
    assert "Tool `search_memory` completed, but the next request was refused" in result
    assert is_error_response(result)


def test_lane_swapped_to_a_paid_backend_before_the_streamed_final_is_refused(
        tripwire, tmp_path, plan_operations_enabled):
    """Every tool/gate round dispatches on the plan lane; then, exactly at the
    streamed-final boundary, agent.llm is replaced by a paid lane (the shape a
    careless "fall back to the API" patch would take). The final generation is
    refused visibly and the paid lane is never streamed."""
    openai = _ForbiddenAPI("openai")
    plan = _ScriptedLLM(PLAN, [
        {"tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    agent = _agent(plan, tmp_path)
    real_stream_final = agent._stream_final
    swapped = []

    def swap_then_stream(*a, **k):
        agent.llm = openai
        swapped.append(True)
        return real_stream_final(*a, **k)

    agent._stream_final = swap_then_stream

    result = LuminaAgent.chat(agent, "go")

    assert swapped
    assert result.startswith("[Stream error:") and "does not change fuel" in result
    assert plan.chat_calls == 3
    _no_fuel_leak(tripwire, openai)
    assert [e["backend"] for e in _events(agent, "provider.dispatch_refused")] == ["openai"]


def test_lane_swapped_from_a_paid_backend_to_the_plan_lane_is_refused(tripwire, tmp_path, plan_operations_enabled):
    """The reverse direction: a turn that began on the paid API must not
    silently continue on (and drain) the owner's plan allowance."""
    plan_duck = _ScriptedLLM(PLAN, [{"content": "x", "termination": TerminationStatus.COMPLETE}])
    openai = _ScriptedLLM("openai", [{"tool_calls": [_tc("search_memory")]}])
    holder = {}
    agent = _agent(openai, tmp_path, on_tool=lambda name: holder["agent"].__setattr__("llm", plan_duck))
    holder["agent"] = agent

    result = LuminaAgent.chat(agent, "find it")

    assert "does not change fuel" in result
    assert plan_duck.chat_calls == 0 and plan_duck.stream_calls == 0
    assert openai.chat_calls == 1
    assert tripwire.total == 0


def test_legacy_to_legacy_swap_mid_turn_is_grandfathered_unchanged(tripwire, tmp_path):
    """Owner saves Settings (openai -> anthropic) while a turn is in flight:
    the pre-01B behavior (the next round goes to the new backend) is preserved
    -- no protected fuel is involved, so the guard does not interfere."""
    anthropic = _ScriptedLLM("anthropic", [
        {"content": "from anthropic", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    openai = _ScriptedLLM("openai", [{"tool_calls": [_tc("search_memory")]}])
    holder = {}
    agent = _agent(openai, tmp_path, on_tool=lambda name: holder["agent"].__setattr__("llm", anthropic))
    holder["agent"] = agent

    result = LuminaAgent.chat(agent, "find it")

    assert not is_error_response(result)
    assert openai.chat_calls == 1 and anthropic.chat_calls >= 1


def test_legacy_lanes_dispatch_exactly_as_before_with_identity_attributed(tripwire, tmp_path):
    for lane in ("openai", "anthropic", "lmstudio", "fake-unregistered"):
        llm = _ScriptedLLM(lane, [
            {"content": "hi", "termination": TerminationStatus.COMPLETE},
            {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
        ])
        agent = _agent(llm, tmp_path / lane if False else tmp_path)
        result = LuminaAgent.chat(agent, "hello")
        assert not is_error_response(result), (lane, result)
        assert llm.chat_calls == 2
        started = json.loads(_events(agent, "turn.started")[-1]["fields_json"])
        assert started["backend_lane"] == (lane if lane != "fake-unregistered" else "fake-unregistered")
        assert _events(agent, "provider.dispatch_refused") == []
        if os.path.exists(agent.flight_recorder.db_path):
            os.remove(agent.flight_recorder.db_path)


# ---------------------------------------------------------------------------
# Capability route / fallback admission and vision admission
# ---------------------------------------------------------------------------

_PAID = ("openai", "anthropic", "openrouter", "gemini", "kimi", "qwen", "groq", "deepseek")


@pytest.mark.parametrize("mode", ["specialist", "auto"])
def test_vision_route_from_the_plan_lane_never_constructs_a_paid_backend(
        mode, tripwire, monkeypatch, tmp_path):
    from core import vision_lane

    route = {"mode": mode, "fallbacks": list(_PAID)}
    if mode == "specialist":
        route["specialist"] = "openai"
    monkeypatch.setattr(config, "MULTIMODAL_ROUTES", {"vision_understanding": route})
    monkeypatch.setattr(config, "MULTIMODAL_DISABLED_PROVIDERS", [])
    built = []
    monkeypatch.setattr(loader, "get_llm_backend", lambda **kw: built.append(kw) or pytest.fail("built"))

    primary = _ScriptedLLM(PLAN)
    agent = types.SimpleNamespace(
        llm=primary, ctx=types.SimpleNamespace(history=[], mark_untrusted_seen=lambda: None),
        flight_recorder=FlightRecorder(db_path=str(tmp_path / "fr.db")))
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    content, ctx = vision_lane.prepare_routed_turn(agent, [image, {"type": "text", "text": "what is this"}])
    assert ctx.decision.outcome == cr.OUTCOME_NO_BACKEND
    for name in _PAID:
        assert "cross-fuel" in ctx.decision.evidence[name]["skip_reason"], name
    agent.ctx.history.append({"role": "user", "content": list(content)})
    result = vision_lane.execute_routed_vision(agent, ctx, turn_id="t", chat_id=1)

    assert result.outcome == "no_specialist"
    assert built == []
    assert all(b.get("type") != "image_url" for b in agent.ctx.history[-1]["content"])
    _no_fuel_leak(tripwire)


def test_vision_route_from_a_paid_primary_cannot_pick_up_the_plan_lane(tripwire, monkeypatch):
    from core import vision_lane
    monkeypatch.setattr(config, "MULTIMODAL_ROUTES", {"vision_understanding": {
        "mode": "auto", "fallbacks": [PLAN, "lmstudio"]}})
    monkeypatch.setattr(config, "MULTIMODAL_DISABLED_PROVIDERS", [])
    agent = types.SimpleNamespace(llm=_ScriptedLLM("openai"),
                                  ctx=types.SimpleNamespace(history=[], mark_untrusted_seen=lambda: None))
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    _content, ctx = vision_lane.prepare_routed_turn(agent, [image, {"type": "text", "text": "q"}])
    # The plan lane is skipped on fuel; the local candidate is still eligible.
    assert "cross-fuel" in ctx.decision.evidence[PLAN]["skip_reason"]
    assert ctx.decision.selected == "lmstudio"
    assert tripwire.total == 0


def test_the_router_has_no_override_for_cross_fuel(tripwire):
    """01B ships no switch that permits subscription <-> paid crossing: not in
    the route record, not in the policy, not in resolve_capability()."""
    import inspect
    assert not any("cross" in f.name or "quota" in f.name
                   for f in dataclasses.fields(cr.CapabilityRoute))
    assert not any("cross" in f.name or "quota" in f.name
                   for f in dataclasses.fields(cr.RoutingPolicy))
    params = inspect.signature(cr.resolve_capability).parameters
    assert not any("allow" in p or "override" in p for p in params)
    assert cr.parse_routes({"vision_understanding": {
        "mode": "auto", "allow_cross_quota_fallback": True}})[0]["vision_understanding"].fallbacks == ()


# ---------------------------------------------------------------------------
# Utility lane selection (auto-name / Dream / My Human / compaction)
# ---------------------------------------------------------------------------

def test_dream_profile_and_compaction_on_the_plan_lane_are_unavailable_not_rerouted(tripwire, capsys):
    from core import dreaming
    assert dreaming.run_summarization_call("raw text to summarize") is None
    assert dreaming.run_summarization_call("raw", prompt=dreaming.COMPACTION_PROMPT, max_tokens=300) is None
    assert dreaming.curate_human_profile("convo", "bio", "existing") is None
    out = capsys.readouterr().out
    assert out.count("failed:") == 3
    _no_fuel_leak(tripwire)


class _PlanBackend(BaseLLMBackend):
    """A BaseLLMBackend that claims the plan lane. NOT the shipped class
    hierarchy -- there is none -- just the minimal concrete double the shared
    utility seam needs. chat() fails like an unavailable plan route."""

    default_url = "http://plan.invalid"
    name = PLAN

    def __init__(self, fail=True):
        self.chat_calls = 0
        self._fail = fail

    def get_model(self):
        return "plan-model"

    def list_models(self):
        return []

    def health_check(self):
        return False, "stub"

    def configured_model(self):
        return "plan-model"

    def chat(self, messages, tools=None, temperature=0.7, max_tokens=1024,
             disable_thinking=False, reasoning_effort=None, tool_choice_mode=None):
        self.chat_calls += 1
        raise RuntimeError("plan route unavailable")

    def chat_stream(self, messages, max_tokens=1024, temperature=0.7, reasoning_effort=None):
        yield ""


def test_auto_name_utility_on_the_plan_lane_is_unavailable_without_dispatch(tripwire):
    plan = _PlanBackend()
    assert plan.complete_utility("Generate a 3-5 word title", prefill="TITLE:") is None
    assert plan.chat_calls == 0
    _no_fuel_leak(tripwire)


def test_auto_name_follows_the_live_lane_object_it_never_constructs_another():
    """_auto_name_chat() calls self.agent.llm.complete_utility() -- the live
    lane object -- and constructs nothing. If that ever changed, the call-site
    scan below would also fail."""
    src = open(os.path.join(ROOT, "ui", "main_window.py"), encoding="utf-8").read()
    start = src.index("def _auto_name_chat")
    body = src[start:start + 3000]
    assert "self.agent.llm.complete_utility(" in body
    assert "get_llm_backend" not in body


# ---------------------------------------------------------------------------
# Reforge lane selection (the real continuity compiler)
# ---------------------------------------------------------------------------

@pytest.fixture
def compiler_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "lumina.db"))
    memory.init_chat_db()
    palace.init_palace_db()
    chat_id = memory.create_chat("reforge chat")
    memory.save_chat_message(chat_id, "user", "please keep going")
    return chat_id


def test_reforge_on_the_plan_lane_fails_unavailable_in_lane(tripwire, compiler_db):
    import core.continuity_compiler as cc
    from core.context_checkpoints import STATE_FAILED

    plan = _PlanBackend()
    record = cc.compile_continuity_checkpoint(compiler_db, plan, ephemeral_history=[])

    assert record.state == STATE_FAILED
    assert "utility_call_returned_none" in record.failure_reason
    assert plan.chat_calls == 0                    # refused at the lane's own seam
    _no_fuel_leak(tripwire)


def test_reforge_retries_stay_on_the_plan_lane_once_it_is_enabled(tripwire, compiler_db, monkeypatch):
    """Reforge's bounded compile retries (CONTINUITY_COMPILER_MAX_ATTEMPTS) are
    same-backend retries. With REFORGE admitted on a failing plan lane, every
    attempt hits the plan stub and none reaches a paid lane."""
    import core.continuity_compiler as cc
    only_reforge = dataclasses.replace(bi.LANES[PLAN], admitted_operations=frozenset({OperationKind.REFORGE}))
    monkeypatch.setattr(bi, "LANES", MappingProxyType({**bi.LANES, PLAN: only_reforge}))

    plan = _PlanBackend()
    cc.compile_continuity_checkpoint(compiler_db, plan, ephemeral_history=[])

    assert plan.chat_calls == cc.CONTINUITY_COMPILER_MAX_ATTEMPTS
    _no_fuel_leak(tripwire)


def test_reforge_is_enabled_separately_from_background_utility(tripwire, compiler_db, monkeypatch):
    import core.continuity_compiler as cc
    only_utility = dataclasses.replace(bi.LANES[PLAN], admitted_operations=frozenset({OperationKind.UTILITY}))
    monkeypatch.setattr(bi, "LANES", MappingProxyType({**bi.LANES, PLAN: only_utility}))
    plan = _PlanBackend()
    cc.compile_continuity_checkpoint(compiler_db, plan, ephemeral_history=[])
    assert plan.chat_calls == 0          # utility admitted, Reforge not: still unavailable


def test_reforge_receives_the_live_backend_never_constructs_one():
    """ui/main_window.py hands Reforge the live agent.llm. The continuity
    compiler and rebuild service construct no backend of their own."""
    for rel in ("core/continuity_compiler.py", "core/context_rebuild.py"):
        text = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        assert "get_llm_backend" not in text, rel
    mw = open(os.path.join(ROOT, "ui", "main_window.py"), encoding="utf-8").read()
    assert "backend = self.agent.llm" in mw


# ---------------------------------------------------------------------------
# Subagents / background tasks with every credential populated
# ---------------------------------------------------------------------------

def test_subagent_and_background_overrides_cannot_reach_a_paid_lane_from_the_plan_lane(tripwire):
    import tools.subagent as subagent
    import tools.tasks as tasks

    for backend in (None, "openai", "anthropic", "openrouter", PLAN):
        out = subagent.spawn_subagent("do a thing", backend=backend)
        assert out["success"] is False and out["tool_calls_made"] == 0, backend
        assert out["error"], backend
    # Scheduled / background wrappers end in the same choke point.
    seen = []
    import core.task_queue as tq
    orig = tq.submit_task
    tasks.submit_task = lambda fn, *a, **k: seen.append((fn, k)) or "tid"
    try:
        tasks.run_background_subagent("bg", backend="openai")
        fn, kwargs = seen[0]
        assert fn is subagent.spawn_subagent
        assert fn("bg", **{k: v for k, v in kwargs.items() if k in ("backend",)})["success"] is False
    finally:
        tasks.submit_task = orig
    _no_fuel_leak(tripwire)


# ---------------------------------------------------------------------------
# Structural guard: nothing can construct a backend by a literal lane name,
# and no new construction site appears without review.
# ---------------------------------------------------------------------------

# get_llm_backend() call sites in product code, by file. A NEW call site fails
# this test on purpose: every construction site is a place a fuel decision
# can hide, so adding one is a reviewed act.
_KNOWN_CONSTRUCTION_SITES = {
    "core/agent.py": 1,                  # self.llm = get_llm_backend(name=backend)
    "core/dreaming.py": 2,               # selected lane, utility + profile curation
    "core/vision_lane.py": 2,            # specialist (with/without model override)
    "ui/settings/general_tab.py": 4,     # owner-driven probes + plan catalog + live swap
    "ui/settings/tts_tab.py": 1,         # owner-driven discovery for a route's provider
}


# Every directory that holds tracked product Python (everything but tests/,
# docs, data and tooling dirs), plus the root-level modules.
_PRODUCT_DIRS = ("core", "ui", "tools", "chrome_companion", "tts", "stt", "comms", "eval", "scripts")


def _product_sources():
    for top in _PRODUCT_DIRS:
        base = os.path.join(ROOT, top)
        for dirpath, _dirs, files in os.walk(base):
            if "__pycache__" in dirpath:
                continue
            for fname in files:
                if fname.endswith(".py"):
                    yield os.path.join(dirpath, fname)
    for fname in sorted(os.listdir(ROOT)):
        if fname.endswith(".py"):
            yield os.path.join(ROOT, fname)


def test_construction_sites_are_exactly_the_reviewed_set_and_none_use_a_literal_lane():
    found = {}
    literal = []
    for path in _product_sources():
        rel = os.path.relpath(path, ROOT)
        if rel == "core/backends/loader.py":
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            callee = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else None)
            if callee != "get_llm_backend":
                continue
            found[rel] = found.get(rel, 0) + 1
            name_args = list(node.args[:1]) + [kw.value for kw in node.keywords if kw.arg == "name"]
            for arg in name_args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    literal.append((rel, node.lineno, arg.value))
    assert found == _KNOWN_CONSTRUCTION_SITES, found
    assert literal == [], f"hardcoded backend lane at a construction site: {literal}"


def test_no_product_code_instantiates_a_backend_class_directly():
    offenders = []
    for path in _product_sources():
        rel = os.path.relpath(path, ROOT)
        if rel.startswith("core/backends/"):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                callee = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else "")
                if callee.endswith("Backend") and callee != "BaseLLMBackend":
                    offenders.append((rel, node.lineno, callee))
    assert offenders == [], offenders
