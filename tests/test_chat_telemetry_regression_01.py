"""
tests/test_chat_telemetry_regression_01.py -- CHAT-TELEMETRY-REGRESSION-01

End-to-end coverage for the live-observed footer regression on
OpenRouter/GLM (z-ai/glm-5.3-flash) turns:

    68.5s turn . 68.5s first answer . Final TTFT n/a . stream n/a .
    0in / 79out / 79total . 1 tool calls . think: <0.1s

Source-vetted root cause (see the two production diffs this file covers,
in core/backends/lmstudio.py and ui/chat_widget.py):

1. `0in` was never a regression from a prior working state -- input token
   count was hardcoded to a literal 0 for EVERY backend before
   OPENAI-RESPONSES-01 (commit 61641b3) introduced the whole provider-usage
   pipeline (_accumulate_provider_telemetry/on_token_usage/_provider_usage),
   OpenAI-first. LMStudioBackend (and therefore OpenRouterBackend and every
   other OpenAI-compatible-family descendant) never got its own
   extract_response_telemetry() override, so _provider_usage stayed None
   forever and ui/chat_widget.py's `usage.get("prompt_tokens", 0)` silently
   manufactured a zero the provider never reported -- exactly the "0 vs
   unknown" conflation this campaign exists to close. The raw usage data
   was already reachable the whole time: chat()'s non-streaming response
   already carries it (`return resp.json()` unmodified), and chat_stream()
   already receives a trailing empty-choices metadata frame carrying it
   (previously silently discarded -- see tests/test_lmstudio_backend.py's
   OpenCode-shaped fixture).

2. "Final TTFT n/a . stream n/a" is NOT a bug for a promoted
   completion_candidate turn (the shape the live symptom's "1 tool calls"
   footer almost certainly took) -- it is deliberate, extensively
   documented, unchanged design predating 61641b3 entirely (TOKS-STREAM-
   TIMING-01's _fire_final_stream_timing()/_fire_final_ttft() docstrings in
   core/agent.py): a non-streaming WORK-round request has no observable
   first-token/generation boundary, so "n/a" is the truthful answer, not a
   defect. What changed the FREQUENCY of this correct "n/a" (unrelated to
   this campaign, not touched here) is AGENT-PRETOOL-ACTION-INTEGRITY-01's
   control-gate mandate, which now routes nearly every turn -- tool-call or
   not -- through the promoted-candidate fast path instead of _stream_final().
   This file proves both halves independently: n/a stays correct for a
   promoted candidate (test A/B below), and real TTFT/stream/usage together
   are still measurable whenever the turn genuinely reaches _stream_final()
   (test C below) -- so a repair that only fixes one of these would be
   incomplete per the campaign's own truthfulness requirements.

Fake LLM shape deliberately mirrors LMStudioBackend/OpenRouterBackend's
REAL capability contract, not core/backends/openai_backend.py's: chat()
has no `capture_telemetry` parameter at all (so _accepts_capture_telemetry()
correctly reads False, exactly like the live backend), and
extract_response_telemetry() implements the exact same passthrough contract
CHAT-TELEMETRY-REGRESSION-01 added to core/backends/lmstudio.py. The raw
HTTP-response-shape parsing that feeds that contract is covered separately,
at the backend-parsing layer, in tests/test_lmstudio_backend.py -- this
file isolates AGENT-side orchestration (turn_telemetry aggregation,
on_token_usage, Final TTFT/stream truthfulness) from that parsing, the same
separation of concerns tests/test_openai_responses_telemetry_01.py and
tests/test_toks_stream_timing_01.py already establish.
"""
import json
import sqlite3
import types

import pytest

from core.agent import LuminaAgent, FINISH_TOOL_WORK_NAME
from core.backends.base import BackendStreamTelemetry, TerminationStatus
from core.flight_recorder import FlightRecorder

import core.agent as agent_module


@pytest.fixture(autouse=True)
def _no_skill_injection(monkeypatch):
    monkeypatch.setattr("core.agent.build_skills_block", lambda user_input: "")


class _FakeClock:
    def __init__(self, ticks):
        self._ticks = list(ticks)
        self._i = 0

    def __call__(self):
        if self._i >= len(self._ticks):
            raise AssertionError(
                f"fake time.monotonic() called more times than scripted "
                f"({len(self._ticks)} ticks provided; this would be call #{self._i + 1})"
            )
        v = self._ticks[self._i]
        self._i += 1
        return v


@pytest.fixture
def fake_clock(monkeypatch):
    def _install(ticks):
        clock = _FakeClock(ticks)
        monkeypatch.setattr(agent_module.time, "monotonic", clock)
        return clock
    return _install


def _tc(name, call_id):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}


class _OpenRouterShapedLLM:
    """No `capture_telemetry` kwarg on chat() -- matches LMStudioBackend/
    OpenRouterBackend's real signature exactly, so _accepts_capture_telemetry()
    reads False, same as the live backend. extract_response_telemetry()
    mirrors CHAT-TELEMETRY-REGRESSION-01's real core/backends/lmstudio.py
    contract: surface `usage` when the scripted turn carries one, {}
    otherwise -- never fabricate, never default a missing field."""
    display_name = "OpenRouter"
    name = "openrouter"
    supports_required_tool_choice = True

    def __init__(self, turns, stream_scripts=None):
        self.turns = list(turns)
        self.call_count = 0
        self.stream_scripts = list(stream_scripts or [])
        self.stream_call_count = 0

    def get_model(self):
        return "z-ai/glm-5.3-flash"

    def configured_model(self):
        return "z-ai/glm-5.3-flash"

    def chat(self, messages, tools=None, max_tokens=None, reasoning_effort=None,
             tool_choice_mode=None):
        idx = self.call_count
        self.call_count += 1
        return {"_turn": idx}

    def chat_stream(self, messages, max_tokens=None, reasoning_effort=None):
        idx = self.stream_call_count
        self.stream_call_count += 1
        for chunk in self.stream_scripts[idx]:
            yield chunk

    def _entry(self, response):
        return self.turns[response["_turn"]]

    def extract_message(self, response):
        turn = self._entry(response)
        message = {"role": "assistant", "content": turn.get("content", "")}
        if "tool_calls" in turn:
            message["tool_calls"] = turn["tool_calls"]
        return message

    def extract_termination(self, response):
        return self._entry(response).get("termination", TerminationStatus.COMPLETE)

    def extract_reasoning(self, response):
        return self._entry(response).get("reasoning")

    def extract_response_telemetry(self, response):
        usage = self._entry(response).get("usage")
        return {"usage": usage} if isinstance(usage, dict) else {}

    def is_tool_call(self, message):
        return bool(message.get("tool_calls"))

    def get_tool_calls(self, message):
        return message.get("tool_calls", [])

    def parse_tool_call(self, tc):
        return tc["function"]["name"], {}


def _agent_for_telemetry(llm, tmp_path):
    history = []
    callbacks = {
        "usage": [], "think_timing": [], "final_ttft": [],
        "stream": [], "tools": [], "response": [], "ttfa": [],
    }
    ctx = types.SimpleNamespace(
        history=history,
        max_tokens=8000,
        add_user=lambda content, source="OWNER_DIRECT": history.append(
            {"role": "user", "content": content}),
        add_assistant=lambda content: history.append(
            {"role": "assistant", "content": content}),
        add_tool_call=lambda message: history.append(message),
        add_tool_result=lambda call_id, name, result: history.append(
            {"role": "tool", "tool_call_id": call_id, "name": name, "content": result}),
        add_cancelled_tool_result=lambda call_id, name: None,
        push_ephemeral=lambda block: None,
        push_ephemeral_assistant=lambda content: None,
        push_ephemeral_reconciliation_request=lambda: None,
        build_messages=lambda tool_budget=0, chat_id=None: list(history),
        context_usage_snapshot=lambda tool_budget=0, chat_id=None, refresh=False: {
            "used_tokens": 100, "max_tokens": 8000, "percent": 1.25,
        },
    )
    registry = types.SimpleNamespace(
        schema_token_estimate=lambda: 10,
        get_schemas=lambda: [],
        list_enabled=lambda: ["tool_a"],
        call=lambda name, args: f"{name} result",
    )
    agent = types.SimpleNamespace(
        llm=llm,
        ctx=ctx,
        registry=registry,
        owner=True,
        channel_id="telemetry-regression-test",
        on_tool_call=lambda name, args: callbacks["tools"].append(name),
        on_tool_result=lambda name, result: None,
        on_think_start=lambda step: None,
        on_think_token=lambda token: None,
        on_think_end=lambda: None,
        on_response_token=lambda token: callbacks["response"].append(token),
        on_commentary=lambda text: None,
        on_token_usage=lambda usage: callbacks["usage"].append(usage),
        on_think_timing=lambda duration: callbacks["think_timing"].append(duration),
        on_final_ttft=lambda duration: callbacks["final_ttft"].append(duration),
        on_final_stream_timing=lambda duration, count: callbacks["stream"].append(
            (duration, count)),
        on_time_to_first_answer=lambda duration: callbacks["ttfa"].append(duration),
        tts=None,
        _session_tool_calls=0,
        _skill_nudge_sent=False,
        flight_recorder=FlightRecorder(db_path=str(tmp_path / "telemetry.db")),
    )
    agent._finalize_completion_candidate = types.MethodType(
        LuminaAgent._finalize_completion_candidate, agent)
    agent._finalize_with_reconciliation = types.MethodType(
        LuminaAgent._finalize_with_reconciliation, agent)
    agent._stream_final = types.MethodType(LuminaAgent._stream_final, agent)
    return agent, callbacks


def _turn_completed_fields(agent):
    conn = sqlite3.connect(agent.flight_recorder.db_path)
    row = conn.execute(
        "SELECT fields_json FROM events WHERE event_type='turn.completed'"
    ).fetchone()
    conn.close()
    assert row is not None, "no turn.completed event recorded"
    return json.loads(row[0])


# ── A. Tool-call -> final: the exact live-observed symptom shape ────────────

def test_tool_call_then_final_promoted_candidate_restores_real_usage_keeps_ttft_na(
    tmp_path,
):
    """Matches the live footer exactly: one real tool call, then a no-tool
    WORK round with the answer, then the gate confirms finish -- promoted
    via the zero-extra-call fast path (_finalize_completion_candidate()),
    never re-streamed. Before this fix: 0in (fabricated). After: real
    summed prompt/completion/total tokens across all three non-streaming
    rounds (WORK x2 + gate). Final TTFT/stream correctly stay unavailable
    -- this is NOT part of what this campaign restores; a promoted
    candidate genuinely has no first-token boundary to observe."""
    llm = _OpenRouterShapedLLM(turns=[
        {  # WORK round 1: real tool call
            "tool_calls": [_tc("tool_a", "1")],
            "usage": {"prompt_tokens": 100, "completion_tokens": 15, "total_tokens": 115},
        },
        {  # WORK round 2: no more tool calls, complete answer -> candidate
            "content": "final answer",
            "termination": TerminationStatus.COMPLETE,
            "usage": {"prompt_tokens": 130, "completion_tokens": 20, "total_tokens": 150},
        },
        {  # gate: finish_tool_work
            "tool_calls": [_tc(FINISH_TOOL_WORK_NAME, "2")],
            "usage": {"prompt_tokens": 60, "completion_tokens": 5, "total_tokens": 65},
        },
    ])
    agent, callbacks = _agent_for_telemetry(llm, tmp_path)

    result = LuminaAgent.chat(agent, "do the thing")

    assert result == "final answer"
    assert callbacks["tools"] == ["tool_a"]
    # Real, provider-reported, summed across every foreground round --
    # never a fabricated 0, never an estimate.
    assert callbacks["usage"][-1]["prompt_tokens"] == 290
    assert callbacks["usage"][-1]["completion_tokens"] == 40
    assert callbacks["usage"][-1]["total_tokens"] == 330
    # Truthfully unavailable, not a defect: a promoted candidate's answer
    # came from a non-streaming request with no boundary to observe.
    assert callbacks["final_ttft"] == []
    assert callbacks["stream"] == []

    fields = _turn_completed_fields(agent)
    assert fields["input_count"] == 290
    assert fields["output_count"] == 40
    assert fields["total_count"] == 330
    assert "final_ttft_s" not in fields
    assert "final_stream_duration_s" not in fields


# ── B. Simple no-tool-call final: same promoted-candidate shape, no tools ───

def test_simple_no_tool_final_restores_real_usage_keeps_ttft_na(tmp_path):
    """A trivial conversational turn ("what's 2+2?") also goes through the
    completion-candidate + gate fast path under the current control-gate
    design (AGENT-PRETOOL-ACTION-INTEGRITY-01) -- not just tool-bearing
    turns. Same truthful contract applies: real summed usage, n/a TTFT/
    stream."""
    llm = _OpenRouterShapedLLM(turns=[
        {
            "content": "4",
            "termination": TerminationStatus.COMPLETE,
            "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
        },
        {
            "tool_calls": [_tc(FINISH_TOOL_WORK_NAME, "1")],
            "usage": {"prompt_tokens": 40, "completion_tokens": 5, "total_tokens": 45},
        },
    ])
    agent, callbacks = _agent_for_telemetry(llm, tmp_path)

    result = LuminaAgent.chat(agent, "what's 2+2?")

    assert result == "4"
    assert callbacks["tools"] == []
    assert callbacks["usage"][-1]["prompt_tokens"] == 90
    assert callbacks["usage"][-1]["completion_tokens"] == 15
    assert callbacks["usage"][-1]["total_tokens"] == 105
    assert callbacks["final_ttft"] == []
    assert callbacks["stream"] == []


# ── C. Genuinely streamed final: real TTFT/stream/usage together ────────────

def test_streamed_final_restores_ttft_stream_and_usage_together(tmp_path, fake_clock):
    """Forces the turn to reach the REAL _stream_final() path -- a tool
    call, then an empty-content no-tool WORK round (no candidate content to
    preserve), so the gate's "finish" outcome finds no completion_candidate
    and falls through to a genuine chat_stream() call, exactly like a
    reconciliation or first-round-ever-empty edge case would live. Proves
    the OTHER half of the campaign's completeness requirement: when a real
    streaming boundary exists, Final TTFT, stream tok/s, AND provider usage
    (now surfaced via the trailing-frame BackendStreamTelemetry event, same
    as core/backends/lmstudio.py's chat_stream() fix) all come back real
    and simultaneously -- not just one of them."""
    llm = _OpenRouterShapedLLM(
        turns=[
            {  # WORK round 1: real tool call
                "tool_calls": [_tc("tool_a", "1")],
                "usage": {"prompt_tokens": 80, "completion_tokens": 12, "total_tokens": 92},
            },
            {  # WORK round 2: empty content, no tool call -> no candidate
                "content": "",
                "termination": TerminationStatus.COMPLETE,
                "usage": {"prompt_tokens": 70, "completion_tokens": 3, "total_tokens": 73},
            },
            {  # gate: finish_tool_work, but completion_candidate is None
                "tool_calls": [_tc(FINISH_TOOL_WORK_NAME, "2")],
                "usage": {"prompt_tokens": 30, "completion_tokens": 4, "total_tokens": 34},
            },
        ],
        stream_scripts=[
            [
                "The answer is 42.",
                BackendStreamTelemetry({
                    "usage": {"prompt_tokens": 20, "completion_tokens": 6, "total_tokens": 26},
                    "request_streamed": True,
                }),
            ],
        ],
    )
    agent, callbacks = _agent_for_telemetry(llm, tmp_path)
    # Generous, strictly-increasing tick supply -- this test only asserts
    # realness/positivity of the measured intervals, not exact internal
    # monotonic() call-count boundaries (those are already covered per-field
    # in tests/test_toks_stream_timing_01.py).
    fake_clock([100.0 + 0.01 * i for i in range(200)])

    result = LuminaAgent.chat(agent, "do the thing")

    assert result == "The answer is 42."
    # Final TTFT and stream tok/s are now REAL, positive measurements --
    # this path never depends on capture_telemetry (that seam is a
    # different, OpenAI-only opt-in for the promoted-candidate case covered
    # in tests A/B above; _stream_final()'s own boundary tracking is and
    # always was backend-agnostic).
    assert len(callbacks["final_ttft"]) == 1
    assert callbacks["final_ttft"][0] > 0
    assert len(callbacks["stream"]) == 1
    stream_duration, stream_tokens = callbacks["stream"][0]
    assert stream_duration > 0
    assert stream_tokens > 0
    # Usage is real and summed across every foreground round, including the
    # streamed final's own trailing-frame usage -- restored via the same
    # BackendStreamTelemetry seam OpenAIBackend's Responses stream already
    # used, now shared by the OpenAI-compatible/LMStudio-family backends.
    assert callbacks["usage"][-1]["prompt_tokens"] == 80 + 70 + 30 + 20
    assert callbacks["usage"][-1]["completion_tokens"] == 12 + 3 + 4 + 6
    assert callbacks["usage"][-1]["total_tokens"] == 92 + 73 + 34 + 26

    fields = _turn_completed_fields(agent)
    assert fields["final_ttft_s"] == pytest.approx(callbacks["final_ttft"][0])
    assert fields["final_stream_duration_s"] == pytest.approx(stream_duration)
    assert fields["input_count"] == 80 + 70 + 30 + 20
    assert fields["output_count"] == 12 + 3 + 4 + 6


# ── D. Missing usage never becomes a fabricated zero ─────────────────────────

def test_missing_usage_never_fabricates_zero_or_partial_total(tmp_path):
    """A backend/response that genuinely never reports usage (this
    campaign's category-6 case: intentional provider absence) must leave
    turn_telemetry's usage fields untouched -- on_token_usage must never
    fire at all for this turn, so the UI's own None-vs-0 handling
    (ui/chat_widget.py finalize()/MetricsBar.set_metrics(), covered in
    tests/test_openai_responses_telemetry_01.py's GUI footer test) has
    truthful "never captured" state to work with, not a manufactured 0."""
    llm = _OpenRouterShapedLLM(turns=[
        {
            "content": "no usage on this provider",
            "termination": TerminationStatus.COMPLETE,
            "usage": None,
        },
        {
            "tool_calls": [_tc(FINISH_TOOL_WORK_NAME, "1")],
            "usage": None,
        },
    ])
    agent, callbacks = _agent_for_telemetry(llm, tmp_path)

    result = LuminaAgent.chat(agent, "hello")

    assert result == "no usage on this provider"
    assert callbacks["usage"] == []

    fields = _turn_completed_fields(agent)
    assert "input_count" not in fields
    assert "output_count" not in fields
    assert "total_count" not in fields
