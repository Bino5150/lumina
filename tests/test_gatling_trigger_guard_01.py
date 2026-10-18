"""GATLING-TRIGGER-GUARD-01 -- tool-call runaway root-cause fix and containment.

Two production runaways (2026-10-16) were each ONE GLM-5.3-flash response:
5,551 and 2,076 tool calls, both cut off at max_tokens, both executed in full.
Controlled replay proved the trigger: Lumina sent user-authored skills as a
TRAILING assistant message (an assistant prefill), and some OpenRouter
serving stacks continued it with no thinking phase (Fireworks 7/8 runaways;
0/8 with the block moved before the user turn). OpenRouter reported the
cut-off as finish_reason "tool_calls" (native "length"), so the truncated
batch read COMPLETE.

Covered here:
  F1  -- the skills block sits before the current user turn; a WORK/gate
         request ending on an assistant message is refused before dispatch.
  F2  -- native_finish_reason survives stream assembly and wins over the
         router's normalized finish_reason; per-response provider identity
         is recorded passively.
  A   -- tool execution budgets: per-response (refuse whole), cut-off batches
         (refuse whole), per-turn calls (stop before exceeding, warn at 80%),
         turn wall-clock (stop before the next request/call, warn at 75%).
"""
import json
import types

import pytest

import config
import core.agent as agent_module
from core.agent import (
    LuminaAgent,
    PREFILL_REFUSAL_NOTICE,
    TOOL_BATCH_LIMIT_NOTICE,
    TOOL_CALL_BUDGET_NOTICE,
    TURN_TIME_BUDGET_NOTICE,
    ERROR_RESPONSE_EXACT,
)
from core.backends.base import TerminationStatus
from core.context import ContextManager


# ── Harness ──────────────────────────────────────────────────────────────

def _tc(i, name="list_recent_chats", args="{}"):
    return {"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": args}}


class _ScriptedLLM:
    """Returns scripted responses in order; records every chat() kwargs."""
    display_name = "fake"

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def chat(self, **kwargs):
        self.requests.append(kwargs)
        if not self.responses:
            raise AssertionError("LLM called more times than scripted")
        return self.responses.pop(0)

    def extract_message(self, response):
        return dict(response["message"])

    def is_tool_call(self, message):
        return bool(message.get("tool_calls"))

    def get_tool_calls(self, message):
        return message["tool_calls"]

    def parse_tool_call(self, tc):
        return tc["function"]["name"], json.loads(tc["function"]["arguments"] or "{}")

    def extract_termination(self, response):
        return response.get("termination", TerminationStatus.COMPLETE)

    def extract_response_telemetry(self, response):
        return {"usage": response["usage"]} if "usage" in response else {}

    def chat_stream(self, messages, max_tokens=None, reasoning_effort=None, **kwargs):
        yield "final answer"


def _tools(n, **kw):
    return {"message": {"role": "assistant", "content": "", "tool_calls": [_tc(i, **kw) for i in range(n)]}}


def _answer(text="done"):
    return {"message": {"role": "assistant", "content": text}}


class _Ctx:
    def __init__(self):
        self.history = []
        self.events = []
        self.ephemeral = []

    def add_user(self, content, source="OWNER_DIRECT"):
        self.history.append({"role": "user", "content": content})

    def push_ephemeral(self, block):
        self.ephemeral.append(("push", block))

    def append_ephemeral(self, block):
        self.ephemeral.append(("append", block))

    def build_messages(self, tool_budget=0, chat_id=None):
        return [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]

    def add_tool_call(self, message):
        self.events.append(("tool_call_message", len(message.get("tool_calls") or [])))

    def add_tool_result(self, tool_call_id, name, result):
        self.events.append(("tool_result", tool_call_id, name))

    def add_cancelled_tool_result(self, tool_call_id, name):
        self.events.append(("cancelled", tool_call_id, name))

    def add_refused_tool_result(self, tool_call_id, name, content):
        self.events.append(("refused", tool_call_id, name, content))

    def add_assistant(self, content):
        self.history.append({"role": "assistant", "content": content})


def _agent(llm, ctx=None):
    ctx = ctx if ctx is not None else _Ctx()
    executed = []

    def registry_call(name, args):
        executed.append(name)
        return f"result:{name}"

    fake = types.SimpleNamespace(
        owner=False, llm=llm, ctx=ctx,
        registry=types.SimpleNamespace(
            schema_token_estimate=lambda: 0, get_schemas=lambda: [],
            list_enabled=lambda: [], call=registry_call,
        ),
        on_tool_call=lambda name, args: None,
        on_tool_result=lambda name, result: None,
        on_response_token=lambda text: None,
        on_think_start=lambda step: None,
        on_think_token=lambda tok: None,
        on_think_end=lambda: None,
        on_commentary=lambda text: None,
        channel_id="test-channel",
        tts=None,
        _session_tool_calls=0,
        _skill_nudge_sent=True,
    )
    for name in ("_stream_final", "_finalize_completion_candidate", "_finalize_with_reconciliation"):
        setattr(fake, name, types.MethodType(getattr(LuminaAgent, name), fake))
    return fake, ctx, executed


@pytest.fixture
def fr(monkeypatch):
    events = []
    monkeypatch.setattr(agent_module, "_fr_machine",
                        lambda agent, event_type, **kw: events.append((event_type, kw.get("fields") or {})))
    monkeypatch.setattr(agent_module, "build_skills_block", lambda query: ("", ""))
    return events


def _of(events, kind):
    return [f for t, f in events if t == kind]


# ── F1: skills reference placement ───────────────────────────────────────

def _cm():
    cm = ContextManager(owner=False)
    cm.max_tokens = 20000
    cm.reserve = 0
    return cm


def test_reference_sits_immediately_before_current_user_turn_never_trailing():
    cm = _cm()
    cm.add_user("first question")
    cm.add_assistant("first answer")
    cm.add_user("check your surroundings")
    cm.push_ephemeral_assistant_reference("## Relevant Skills (user-authored)\nSKILL")

    messages = cm.build_messages()

    assert messages[-1] == {"role": "user", "content": "check your surroundings"}
    assert messages[-2] == {"role": "assistant", "content": "## Relevant Skills (user-authored)\nSKILL"}
    assert [m["content"] for m in messages[1:3]] == ["first question", "first answer"]
    assert "SKILL" not in messages[0]["content"]          # never promoted to system


def test_reference_is_one_shot_and_never_durable():
    cm = _cm()
    cm.add_user("q")
    cm.push_ephemeral_assistant_reference("REF")

    first = cm.build_messages()
    second = cm.build_messages()

    assert any(m.get("content") == "REF" for m in first)
    assert not any(m.get("content") == "REF" for m in second)
    assert all(m.get("content") != "REF" for m in cm.history)


def test_reference_without_any_user_message_goes_first_not_last():
    cm = _cm()
    cm.add_assistant("earlier")
    cm.push_ephemeral_assistant_reference("REF")

    messages = cm.build_messages()

    assert messages[1] == {"role": "assistant", "content": "REF"}
    assert messages[-1] == {"role": "assistant", "content": "earlier"}


def test_agent_round_one_request_ends_on_the_user_turn_with_skills_before_it(monkeypatch):
    """The live failure: the first WORK request ended on the skills message."""
    monkeypatch.setattr(agent_module, "build_skills_block",
                        lambda query: ("", "## Relevant Skills (user-authored)\nkali-operating-modes"))
    llm = _ScriptedLLM([_answer("hello"), {"message": {"role": "assistant", "content": "",
                                                       "tool_calls": [_tc(0, name=agent_module.FINISH_TOOL_WORK_NAME)]}}])
    cm = _cm()
    fake, _, _ = _agent(llm, ctx=cm)

    LuminaAgent.chat(fake, "Good afternoon Lumina. Check your surroundings")

    first = llm.requests[0]["messages"]
    assert first[-1]["role"] == "user"
    assert "Check your surroundings" in str(first[-1]["content"])
    assert first[-2] == {"role": "assistant",
                         "content": "## Relevant Skills (user-authored)\nkali-operating-modes"}
    assert "placed just before the user's latest message" in first[0]["content"]


def test_reconciliation_still_closes_its_assistant_material_with_a_user_cue():
    """The other push_ephemeral_assistant() user is unchanged and never trailing."""
    from core.context import RECONCILIATION_CONTINUATION_CUE
    cm = _cm()
    cm.add_user("q")
    cm.push_ephemeral_assistant("material")
    cm.push_ephemeral_reconciliation_request()

    messages = cm.build_messages()

    assert messages[-2] == {"role": "assistant", "content": "material"}
    assert messages[-1] == {"role": "user", "content": RECONCILIATION_CONTINUATION_CUE}


def test_request_ending_on_assistant_is_refused_before_dispatch(fr):
    llm = _ScriptedLLM([_answer()])
    fake, _, _ = _agent(llm)
    kwargs = {"messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "u"},
                           {"role": "assistant", "content": "prefill"}], "max_tokens": 10}

    response, err = agent_module._provider_chat_or_error(fake, kwargs, None, set())

    assert response is None and err == PREFILL_REFUSAL_NOTICE
    assert llm.requests == []                                  # nothing was sent
    assert _of(fr, "provider.dispatch_refused") == [{"reason": "trailing_assistant_message",
                                                     "message_count": 3}]
    assert PREFILL_REFUSAL_NOTICE.startswith("[Lumina error:")  # classified as a failed turn


# ── F2: termination truth and provider identity ──────────────────────────

def _oai(finish, native=None, **extra):
    choice = {"message": {"role": "assistant", "content": ""}, "finish_reason": finish}
    if native is not None:
        choice["native_finish_reason"] = native
    return {"choices": [choice], **extra}


def test_native_length_overrides_router_normalized_tool_calls():
    from core.backends.lmstudio import LMStudioBackend
    b = LMStudioBackend.__new__(LMStudioBackend)       # concrete; inherits the base readers
    assert b.extract_termination(_oai("tool_calls", "length")) == TerminationStatus.INCOMPLETE
    assert b.extract_termination(_oai("tool_calls", "tool_calls")) == TerminationStatus.COMPLETE
    assert b.extract_termination(_oai("tool_calls")) == TerminationStatus.COMPLETE
    assert b.extract_termination(_oai("stop", "length")) == TerminationStatus.INCOMPLETE
    assert b.extract_termination(_oai("length")) == TerminationStatus.INCOMPLETE


def test_response_identity_is_read_passively_and_never_raises():
    from core.backends.lmstudio import LMStudioBackend
    b = LMStudioBackend.__new__(LMStudioBackend)       # concrete; inherits the base readers
    resp = _oai("tool_calls", "length", provider="Fireworks", model="z-ai/glm-5.3-flash")
    resp["choices"][0]["message"]["tool_calls"] = [_tc(0), _tc(1)]
    assert b.extract_response_identity(resp) == {
        "provider_name": "Fireworks", "response_model": "z-ai/glm-5.3-flash",
        "finish_reason": "tool_calls", "native_finish_reason": "length", "tool_call_count": 2}
    for junk in (None, [], {"choices": "x"}, {"choices": [None]}):
        assert b.extract_response_identity(junk)["provider_name"] is None


def test_stream_assembly_keeps_native_finish_reason_provider_and_model(monkeypatch):
    from core.backends import lmstudio
    frames = [
        {"provider": "Fireworks", "model": "z-ai/glm-5.3-flash",
         "choices": [{"delta": {"role": "assistant", "tool_calls": [
             {"index": 0, "id": "c0", "function": {"name": "chrome_status", "arguments": "{}"}}]}}]},
        {"provider": "Fireworks", "model": "z-ai/glm-5.3-flash",
         "choices": [{"delta": {}, "finish_reason": "tool_calls", "native_finish_reason": "length"}]},
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 4096, "total_tokens": 4106}},
    ]
    lines = [f"data: {json.dumps(f)}".encode() for f in frames] + [b"data: [DONE]"]

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def iter_lines(self): return iter(lines)
        def close(self): pass

    monkeypatch.setattr(lmstudio.requests, "post", lambda *a, **k: _Resp())
    backend = lmstudio.LMStudioBackend.__new__(lmstudio.LMStudioBackend)
    backend.base_url = "http://127.0.0.1:1/v1"
    backend.display_name = "test"
    backend.headers = {}
    body = backend._chat_with_stream_telemetry({"model": "m", "messages": [], "stream": True})

    assert body["choices"][0]["finish_reason"] == "tool_calls"
    assert body["choices"][0]["native_finish_reason"] == "length"
    assert body["provider"] == "Fireworks" and body["model"] == "z-ai/glm-5.3-flash"
    assert backend.extract_termination(body) == TerminationStatus.INCOMPLETE


def test_provider_response_event_is_recorded_without_touching_parsing_hooks(fr):
    class _Strict(_ScriptedLLM):
        def get_tool_calls(self, message):
            raise AssertionError("telemetry must not call the agent's parsing hooks")

        def extract_response_identity(self, response):
            return {"provider_name": "Venice", "response_model": "z-ai/glm-5.3-flash",
                    "finish_reason": "tool_calls", "native_finish_reason": "length",
                    "tool_call_count": 7}

    llm = _Strict([{"usage": {"prompt_tokens": 1, "completion_tokens": 25000, "total_tokens": 25001,
                              "completion_tokens_details": {"reasoning_tokens": 0}}}])
    fake, _, _ = _agent(llm)
    kwargs = {"messages": [{"role": "user", "content": "u"}], "max_tokens": 25000}

    agent_module._provider_chat_or_error(fake, kwargs, None, set(), phase="work")

    assert _of(fr, "provider.response") == [{
        "phase": "work", "provider_name": "Venice", "response_model": "z-ai/glm-5.3-flash",
        "finish_reason": "tool_calls", "native_finish_reason": "length", "tool_call_count": 7,
        "output_count": 25000, "reasoning_output_count": 0, "output_limit": 25000}]


# ── A: batch admission ───────────────────────────────────────────────────

def test_incident_sized_batch_runs_nothing_and_second_refusal_ends_turn(fr):
    llm = _ScriptedLLM([_tools(2076), _tools(5551, name="chrome_status")])
    fake, ctx, executed = _agent(llm)

    result = LuminaAgent.chat(fake, "check your surroundings")

    assert executed == []
    assert result == TOOL_BATCH_LIMIT_NOTICE and result in ERROR_RESPONSE_EXACT
    refused = [e for e in ctx.events if e[0] == "refused"]
    kept = [e for e in ctx.events if e[0] == "tool_call_message"]
    assert [n for _, n in kept] == [256, 256]                 # only bounded prefixes persisted
    assert len(refused) == 512                                 # exactly one row per kept id
    assert "requested 2076 tool calls, over the limit of 256" in refused[0][3]
    batches = _of(fr, "tool.batch_refused")
    assert [b["batch_size"] for b in batches] == [2076, 5551]
    assert batches[0]["reason"] == "over_limit" and batches[0]["max_identical_repeats"] == 2076
    assert _of(fr, "tool.batch") == []


def test_first_refusal_is_recoverable_and_the_model_can_continue(fr):
    llm = _ScriptedLLM([_tools(300), _tools(2), _answer("ok"),
                        {"message": {"role": "assistant", "content": "",
                                     "tool_calls": [_tc(0, name=agent_module.FINISH_TOOL_WORK_NAME)]}}])
    fake, ctx, executed = _agent(llm)

    LuminaAgent.chat(fake, "go")

    assert executed == ["list_recent_chats", "list_recent_chats"]
    assert len(_of(fr, "tool.batch_refused")) == 1


@pytest.mark.parametrize("size", [1, 22, 256])
def test_legitimate_batches_at_or_under_the_limit_run_normally(fr, size):
    # Distinct calls: a batch of `size` IDENTICAL calls is the no-progress
    # pattern AGENT-NO-PROGRESS-BREAKER-01 stops after 21 calls, so it is not
    # a legitimate batch (that path is covered in its own test module).
    batch = {"message": {"role": "assistant", "content": "",
                         "tool_calls": [_tc(i, args=json.dumps({"limit": i})) for i in range(size)]}}
    llm = _ScriptedLLM([batch, _answer("ok"),
                        {"message": {"role": "assistant", "content": "",
                                     "tool_calls": [_tc(0, name=agent_module.FINISH_TOOL_WORK_NAME)]}}])
    fake, _, executed = _agent(llm)

    LuminaAgent.chat(fake, "go")

    assert len(executed) == size
    assert _of(fr, "tool.batch_refused") == []


def test_cut_off_batch_by_termination_is_refused_even_when_small(fr):
    resp = _tools(3)
    resp["termination"] = TerminationStatus.INCOMPLETE
    llm = _ScriptedLLM([resp, _answer("ok"),
                        {"message": {"role": "assistant", "content": "",
                                     "tool_calls": [_tc(0, name=agent_module.FINISH_TOOL_WORK_NAME)]}}])
    fake, ctx, executed = _agent(llm)

    LuminaAgent.chat(fake, "go")

    assert executed == []
    refusal = _of(fr, "tool.batch_refused")[0]
    assert refusal["reason"] == "cut_off" and refusal["batch_size"] == 3
    assert "cut off at its output limit" in [e for e in ctx.events if e[0] == "refused"][0][3]


def test_cut_off_batch_by_usage_at_the_output_limit_is_refused(fr, monkeypatch):
    """Provider-neutral leg: no finish-reason vocabulary needed at all."""
    monkeypatch.setattr(config, "RESPONSE_RESERVE_TOKENS", 4096)
    resp = _tools(3)
    resp["usage"] = {"prompt_tokens": 9, "completion_tokens": 4096, "total_tokens": 4105}
    llm = _ScriptedLLM([resp, _answer("ok"),
                        {"message": {"role": "assistant", "content": "",
                                     "tool_calls": [_tc(0, name=agent_module.FINISH_TOOL_WORK_NAME)]}}])
    fake, _, executed = _agent(llm)

    LuminaAgent.chat(fake, "go")

    assert executed == []
    assert _of(fr, "tool.batch_refused")[0]["reason"] == "cut_off"


def test_usage_just_under_the_limit_is_not_a_cut_off(fr, monkeypatch):
    monkeypatch.setattr(config, "RESPONSE_RESERVE_TOKENS", 4096)
    resp = _tools(3)
    resp["usage"] = {"prompt_tokens": 9, "completion_tokens": 4095, "total_tokens": 4104}
    llm = _ScriptedLLM([resp, _answer("ok"),
                        {"message": {"role": "assistant", "content": "",
                                     "tool_calls": [_tc(0, name=agent_module.FINISH_TOOL_WORK_NAME)]}}])
    fake, _, executed = _agent(llm)

    LuminaAgent.chat(fake, "go")

    assert len(executed) == 3
    batch = _of(fr, "tool.batch")[0]
    assert batch["output_count"] == 4095 and batch["output_limit"] == 4096


# ── A: per-turn call budget ──────────────────────────────────────────────

def test_turn_call_budget_stops_before_the_call_that_would_exceed_it(fr, monkeypatch):
    monkeypatch.setattr(config, "MAX_TOOL_CALLS_PER_TURN", 5)
    llm = _ScriptedLLM([_tools(3), _tools(3, name="git_status")])
    fake, ctx, executed = _agent(llm)

    result = LuminaAgent.chat(fake, "go")

    assert result == TOOL_CALL_BUDGET_NOTICE and result in ERROR_RESPONSE_EXACT
    assert executed == ["list_recent_chats"] * 3 + ["git_status"] * 2
    stopped = [e for e in ctx.events if e[0] == "refused"]
    assert [(e[1], e[2]) for e in stopped] == [("c2", "git_status")]   # the unrun id, paired once
    assert stopped[0][3] == TOOL_CALL_BUDGET_NOTICE
    exhausted = _of(fr, "turn.budget_exhausted")
    assert exhausted[0]["kind"] == "tool_calls" and exhausted[0]["admitted_tool_calls"] == 5
    warnings = _of(fr, "turn.budget_warning")
    assert [w["admitted_tool_calls"] for w in warnings] == [4]          # once, at 80%
    assert [k for k, _ in ctx.ephemeral].count("append") == 1          # appended, not overwriting


def test_reported_productive_turn_sizes_fit_the_default_budget(fr):
    """74 and 94-call engineering turns must not be stopped by the defaults."""
    rounds = [_tools(4, name=f"tool_{r}") for r in range(24)]          # 96 calls over 24 rounds
    llm = _ScriptedLLM(rounds + [_answer("ok"),
                                 {"message": {"role": "assistant", "content": "",
                                              "tool_calls": [_tc(0, name=agent_module.FINISH_TOOL_WORK_NAME)]}}])
    fake, _, executed = _agent(llm)
    config_rounds = config.MAX_TOOL_ITERATIONS
    try:
        config.MAX_TOOL_ITERATIONS = 30
        LuminaAgent.chat(fake, "refactor it")
    finally:
        config.MAX_TOOL_ITERATIONS = config_rounds

    assert len(executed) == 96
    assert _of(fr, "turn.budget_exhausted") == [] and _of(fr, "turn.budget_warning") == []


# ── A: turn wall-clock ───────────────────────────────────────────────────

class _Clock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now


def test_wall_clock_stops_before_the_next_tool_call(fr, monkeypatch):
    monkeypatch.setattr(config, "MAX_TURN_MINUTES", 10)
    clock = _Clock()
    monkeypatch.setattr(agent_module, "_budget_clock", clock)
    llm = _ScriptedLLM([_tools(3)])
    fake, ctx, executed = _agent(llm)

    def slow_call(name, args):
        executed.append(name)
        clock.now += 11 * 60                       # the first call takes the whole budget
        return "r"

    fake.registry.call = slow_call

    result = LuminaAgent.chat(fake, "go")

    assert result == TURN_TIME_BUDGET_NOTICE and result in ERROR_RESPONSE_EXACT
    assert executed == ["list_recent_chats"]
    assert [(e[1]) for e in ctx.events if e[0] == "refused"] == ["c1", "c2"]
    assert _of(fr, "turn.budget_exhausted")[0]["kind"] == "turn_time"


def test_wall_clock_stops_before_the_next_provider_request_and_warns_at_75(fr, monkeypatch):
    monkeypatch.setattr(config, "MAX_TURN_MINUTES", 10)
    clock = _Clock()
    monkeypatch.setattr(agent_module, "_budget_clock", clock)

    class _Slow(_ScriptedLLM):
        def chat(self, **kwargs):
            clock.now += 8 * 60
            return super().chat(**kwargs)

    llm = _Slow([_tools(1), _tools(1)])
    fake, ctx, executed = _agent(llm)

    result = LuminaAgent.chat(fake, "go")

    # Request 1 ends at 8 min (its call runs); the round-2 loop head is past
    # 75% (warning, request still allowed); request 2 ends at 16 min, so its
    # call is stopped before running and paired with the stop notice.
    assert result == TURN_TIME_BUDGET_NOTICE
    assert len(llm.requests) == 2 and len(executed) == 1
    assert [(e[1], e[3]) for e in ctx.events if e[0] == "refused"] == [("c0", TURN_TIME_BUDGET_NOTICE)]
    warning = _of(fr, "turn.budget_warning")
    assert [w["kind"] for w in warning] == ["turn_time"] and warning[0]["elapsed_s"] == 480.0


# ── Settings robustness ──────────────────────────────────────────────────

@pytest.mark.parametrize("bad, expected", [(0, 1), (-5, 1), ("lots", 256), (None, 256), (3.9, 3)])
def test_bad_settings_never_disable_a_budget(monkeypatch, bad, expected):
    monkeypatch.setattr(config, "MAX_TOOL_CALLS_PER_RESPONSE", bad)
    assert agent_module._tool_calls_per_response_limit() == expected


def test_turn_started_reports_every_budget(fr):
    budgets = agent_module._effective_tool_budgets(types.SimpleNamespace(registry=None, ctx=None, llm=None))
    assert budgets["tool_calls_per_response_limit"] == 256
    assert budgets["tool_calls_per_turn_limit"] == 512
    assert budgets["turn_time_limit_s"] == 3600


def test_budget_warning_and_skill_reminder_in_one_round_both_reach_the_model(fr, monkeypatch):
    """The skill reminder used to push_ephemeral() (replace); a budget warning
    queued in the same round would have been silently overwritten."""
    monkeypatch.setattr(config, "MAX_TOOL_CALLS_PER_TURN", 5)
    monkeypatch.setattr(config, "SKILLS_TRIGGER_THRESHOLD", 1)
    llm = _ScriptedLLM([_tools(4), _answer("ok"),
                        {"message": {"role": "assistant", "content": "",
                                     "tool_calls": [_tc(0, name=agent_module.FINISH_TOOL_WORK_NAME)]}}])
    cm = _cm()
    fake, _, _ = _agent(llm, ctx=cm)
    fake._skill_nudge_sent = False

    LuminaAgent.chat(fake, "go")

    second_system = llm.requests[1]["messages"][0]["content"]
    assert "## Tool-call budget" in second_system
    assert "## Skill reminder" in second_system


def test_wall_clock_stops_before_the_next_provider_request_with_nothing_pending(fr, monkeypatch):
    """Time runs out at the END of a batch: no call is pending, so only the
    check before the next provider request can stop the turn -- and nothing
    further may be sent."""
    monkeypatch.setattr(config, "MAX_TURN_MINUTES", 10)
    clock = _Clock()
    monkeypatch.setattr(agent_module, "_budget_clock", clock)
    llm = _ScriptedLLM([_tools(1), _tools(1)])
    fake, ctx, executed = _agent(llm)

    def slow_call(name, args):
        executed.append(name)
        clock.now += 11 * 60
        return "r"

    fake.registry.call = slow_call

    result = LuminaAgent.chat(fake, "go")

    assert result == TURN_TIME_BUDGET_NOTICE
    assert len(llm.requests) == 1 and executed == ["list_recent_chats"]
    assert [e for e in ctx.events if e[0] == "refused"] == []      # nothing was pending
    exhausted = _of(fr, "turn.budget_exhausted")[0]
    assert exhausted["kind"] == "turn_time" and exhausted["work_rounds_used"] == 1


# ── Resource lifecycle: search_skills() connection cleanup ───────────────
#
# Found during this campaign: search_skills() closed its SQLite connection
# only on the success path. A query that raised left it to the garbage
# collector, whose "unclosed database" ResourceWarning then landed inside
# whatever test was running (test_backend_endpoint_contract.py's zero-warning
# assertions) and failed the full gate intermittently.

class _TrackedConnection:
    """Delegates to a real sqlite3 connection and records close()."""

    def __init__(self, real):
        self._real = real
        self.closed = False

    def execute(self, *args, **kwargs):
        return self._real.execute(*args, **kwargs)

    def close(self):
        self.closed = True
        self._real.close()


def _track_skills_connections(monkeypatch):
    import core.skills as skills_module
    opened = []
    real_get_db = skills_module.get_db

    def tracked_get_db():
        conn = _TrackedConnection(real_get_db())
        opened.append(conn)
        return conn

    monkeypatch.setattr(skills_module, "get_db", tracked_get_db)
    return opened


def test_search_skills_closes_its_connection_when_a_query_raises(tmp_path, monkeypatch):
    import sqlite3
    from core.skills import search_skills
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "no_skill_tables.db"))
    opened = _track_skills_connections(monkeypatch)

    with pytest.raises(sqlite3.OperationalError):          # no skills table: the LIKE fallback raises
        search_skills("castle cannon procedure")

    assert len(opened) == 1 and opened[0].closed is True


def test_search_skills_closes_its_connection_and_keeps_its_results(tmp_path, monkeypatch):
    from core.skills import init_skills_db, search_skills, write_skill
    monkeypatch.setattr(config, "BASE_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "skills.db"))
    init_skills_db()
    write_skill("castle cannon procedure", "castle cannon adversarial procedure", "body")
    opened = _track_skills_connections(monkeypatch)

    fts_hits = search_skills("castle cannon")
    like_hits = search_skills("adversarial procedure")      # same row, whichever path finds it

    assert [h["name"] for h in fts_hits] == ["castle cannon procedure"]
    assert fts_hits[0]["origin"] == "user" and set(fts_hits[0]) == {"name", "description", "path", "origin"}
    assert [h["name"] for h in like_hits] == ["castle cannon procedure"]
    assert len(opened) == 2 and all(c.closed for c in opened)
    assert search_skills("   ") == [] and len(opened) == 2   # blank query never opens one
