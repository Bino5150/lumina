"""
tests/test_promoted_candidate_streaming_01.py -- PROMOTED-CANDIDATE-STREAMING-01

Diagnostic verdict (source-vetted 2026-09-21): the completion-candidate /
control-gate path core/agent.py's AGENT-PRETOOL-ACTION-INTEGRITY-01
(commit 84fce81) established is CORRECT, intentional behavior -- a turn's
first zero-tool WORK-round response, exactly like any later one, must clear
_run_tool_work_control_gate() before it can be promoted, so a model that
narrated a tool call it never actually invoked can never silently pass as a
"no tool needed" final answer. That machinery is explicitly OUT OF SCOPE
here and untouched by this file's production changes.

What WAS actually missing: because a promoted completion_candidate is
delivered via _deliver_held_text() (a local chunked replay of already-
generated text, never a live provider stream -- see that function's own
docstring in core/agent.py), Final TTFT/stream-duration telemetry could
only ever be real if the backend supplied provider-native stream
boundaries via the existing `capture_telemetry` opt-in
(core/agent.py's _accepts_capture_telemetry()). Before this ticket, only
core/backends/openai_backend.py's OpenAIBackend implemented that contract
(via _chat_with_stream_telemetry(), against the native /v1/responses
transport). LMStudioBackend and its whole family -- DeepSeek, Groq, Kimi,
llama.cpp, OmniRoute, OpenRouter (what GLM runs on), Qwen, vLLM, Custom --
did not, so every one of their promoted-candidate turns showed a truthful
but avoidable "Final TTFT n/a . stream n/a", confirmed by
tests/test_chat_telemetry_regression_01.py's own tests A/B (test B's
literal scenario is "what's 2+2?").

This file proves the fix: core/backends/lmstudio.py now has its own
_chat_with_stream_telemetry() (LMStudioBackend._chat_with_stream_telemetry,
model/sibling of OpenAIBackend's), reusing the SAME `capture_telemetry`
opt-in contract and the SAME `_lumina_telemetry` seam
extract_response_telemetry() already established for OpenAI -- no second
telemetry definition, no core/agent.py changes at all (its capability
detection is pure signature introspection and already picks this up for
free). Two independent layers are covered, matching the separation of
concerns tests/test_chat_telemetry_regression_01.py and
tests/test_lmstudio_backend.py already established between themselves:

  Section A/B -- backend-layer correctness, against the REAL LMStudioBackend/
  OpenRouterBackend classes with `requests.post` mocked: SSE delta
  reconstruction (content, reasoning, tool_calls-by-index), timing
  boundaries, usage-frame conventions, error/interruption semantics.

  Section C -- agent-orchestration correctness, against a fake LLM shaped
  like the NOW-FIXED live backend (chat() accepts capture_telemetry,
  extract_response_telemetry() can report real final_ttft_s/
  final_stream_duration_s): capability detection reaching every WORK/gate
  call, provider request count staying unchanged (no new round trip), and
  -- the explicit regression guard this ticket asked for -- the mandatory
  control gate still gates delivery regardless of whether real telemetry
  is available, so nobody can later "optimize" away AGENT-PRETOOL-ACTION-
  INTEGRITY-01's safety property while chasing prettier gauges.
"""
import inspect
import json
import sqlite3
import types

import pytest
import requests

from core.agent import (
    LuminaAgent,
    FINISH_TOOL_WORK_NAME,
    CONTINUE_TOOL_WORK_NAME,
    _accepts_capture_telemetry,
)
from core.backends.base import TerminationStatus
from core.backends.lmstudio import LMStudioBackend
from core.backends.openrouter import OpenRouterBackend
from core.flight_recorder import FlightRecorder


# ── Shared backend-level fixtures (mirrors tests/test_lmstudio_backend.py's
# own _sse_lines/_FakeStreamResp/_make_backend conventions exactly, kept
# local rather than cross-imported -- every test file in this suite is
# self-contained). ──────────────────────────────────────────────────────

def _sse_lines(*frames):
    lines = [f"data: {json.dumps(f)}".encode("utf-8") for f in frames]
    lines.append(b"data: [DONE]")
    return lines


class _FakeStreamResp:
    def __init__(self, lines):
        self._lines = lines

    def raise_for_status(self):
        pass

    def iter_lines(self):
        yield from self._lines


class _FakeJsonResp:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class _DisconnectingStreamResp:
    """A stream that yields a couple of real lines, then raises the same
    requests.exceptions.RequestException class a genuine mid-stream
    disconnect (e.g. ChunkedEncodingError) would -- proving
    _chat_with_stream_telemetry() inherits _iter_lines_safe()'s existing
    ConnectionError conversion rather than a novel, unconverted failure
    mode."""
    def __init__(self, lines):
        self._lines = lines

    def raise_for_status(self):
        pass

    def iter_lines(self):
        yield from self._lines
        raise requests.exceptions.ChunkedEncodingError("connection broken")


def _make_backend(cls=LMStudioBackend):
    backend = cls.__new__(cls)
    backend.base_url = "https://openrouter.ai/api/v1"
    backend.headers = {"Content-Type": "application/json", "Authorization": "Bearer test"}
    backend._model = "z-ai/glm-5.3-flash"
    if cls is OpenRouterBackend:
        # __new__() bypasses __init__(), which normally seeds this --
        # reasoning_capabilities() (called from apply_reasoning() inside
        # chat()) reads it unconditionally.
        backend._reasoning_cache = {}
    return backend


def _tc(name, call_id):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}


# ── A. Capability detection ──────────────────────────────────────────────

def test_accepts_capture_telemetry_true_for_lmstudio_and_openrouter():
    """core/agent.py's _accepts_capture_telemetry() is pure signature
    introspection, never a provider-name branch -- confirms it now reads
    True for the whole LMStudioBackend family with zero core/agent.py
    changes, exactly the way it already read True for OpenAIBackend."""
    assert _accepts_capture_telemetry(_make_backend(LMStudioBackend)) is True
    assert _accepts_capture_telemetry(_make_backend(OpenRouterBackend)) is True


def test_chat_signature_carries_capture_telemetry_kwarg():
    assert "capture_telemetry" in inspect.signature(_make_backend().chat).parameters


# ── B. Backend-layer stream-telemetry correctness ────────────────────────

def test_capture_telemetry_returns_real_ttft_and_stream_duration_for_answer_text(monkeypatch):
    """The exact shape a promoted completion_candidate's source WORK round
    takes: some reasoning, then visible answer text, then a clean stop.
    Final TTFT is measured to the first CONTENT delta (never the reasoning
    delta) -- mirrors openai_backend.py's _ResponsesTiming contract
    exactly, reused rather than reinvented."""
    backend = _make_backend()
    frames = [
        {"choices": [{"delta": {"reasoning": "pondering"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "42"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
    ]
    monkeypatch.setattr(requests, "post", lambda *a, **kw: _FakeStreamResp(_sse_lines(*frames)))
    ticks = iter([100.0, 100.1, 100.4, 100.9])
    import core.backends.lmstudio as lmstudio_module
    monkeypatch.setattr(lmstudio_module.time, "monotonic", lambda: next(ticks))

    response = backend.chat([{"role": "user", "content": "what's 7 times 6?"}],
                             capture_telemetry=True)

    assert response["choices"][0]["message"]["content"] == "42"
    assert response["choices"][0]["message"]["reasoning_content"] == "pondering"
    assert response["choices"][0]["finish_reason"] == "stop"
    telemetry = backend.extract_response_telemetry(response)
    assert telemetry["usage"] == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    assert telemetry["final_ttft_s"] == pytest.approx(0.4)
    assert telemetry["final_stream_duration_s"] == pytest.approx(0.5)
    assert telemetry["think_duration_s"] == pytest.approx(0.3)
    assert telemetry["request_streamed"] is True


def test_capture_telemetry_reasoning_fragments_concatenate_with_no_separator(monkeypatch):
    """Live-caught 2026-09-21 (real OpenRouter/z-ai/glm-5.3-flash smoke):
    reasoning arrives as many small per-token delta fragments, the same
    raw per-token shape content deltas already get -- not one fragment per
    logical paragraph. An earlier version of this fix joined
    reasoning_parts with "\\n\\n", inserting a spurious blank line between
    literally every token; this asserts the correct no-separator
    concatenation with more than one fragment, which a single-fragment
    fixture cannot distinguish from the buggy version."""
    backend = _make_backend()
    frames = [
        {"choices": [{"delta": {"reasoning": "The"}, "finish_reason": None}]},
        {"choices": [{"delta": {"reasoning": " user"}, "finish_reason": None}]},
        {"choices": [{"delta": {"reasoning": " asks."}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "42"}, "finish_reason": "stop"}]},
    ]
    monkeypatch.setattr(requests, "post", lambda *a, **kw: _FakeStreamResp(_sse_lines(*frames)))

    response = backend.chat([{"role": "user", "content": "hi"}], capture_telemetry=True)

    assert response["choices"][0]["message"]["reasoning_content"] == "The user asks."


def test_capture_telemetry_reconstructs_single_tool_call_from_index_deltas(monkeypatch):
    backend = _make_backend()
    frames = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "type": "function",
             "function": {"name": "get_weather", "arguments": ""}},
        ]}, "finish_reason": None}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "{\"city\":"}},
        ]}, "finish_reason": None}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "\"NYC\"}"}},
        ]}, "finish_reason": "tool_calls"}]},
    ]
    monkeypatch.setattr(requests, "post", lambda *a, **kw: _FakeStreamResp(_sse_lines(*frames)))

    response = backend.chat([{"role": "user", "content": "weather?"}], capture_telemetry=True)

    message = response["choices"][0]["message"]
    assert message["content"] == ""
    assert message["tool_calls"] == [{
        "id": "call_1", "type": "function",
        "function": {"name": "get_weather", "arguments": "{\"city\":\"NYC\"}"},
    }]
    assert response["choices"][0]["finish_reason"] == "tool_calls"


def test_capture_telemetry_reconstructs_multiple_tool_calls_by_index(monkeypatch):
    """Two tool calls, interleaved across frames by `index` -- proves
    reconstruction is keyed on index, not frame arrival order, and that
    output order follows sorted index, matching wire order."""
    backend = _make_backend()
    frames = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "type": "function",
             "function": {"name": "tool_a", "arguments": ""}},
            {"index": 1, "id": "call_2", "type": "function",
             "function": {"name": "tool_b", "arguments": ""}},
        ]}, "finish_reason": None}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "{\"x\":1}"}},
        ]}, "finish_reason": None}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 1, "function": {"arguments": "{\"y\":2}"}},
        ]}, "finish_reason": "tool_calls"}]},
    ]
    monkeypatch.setattr(requests, "post", lambda *a, **kw: _FakeStreamResp(_sse_lines(*frames)))

    response = backend.chat([{"role": "user", "content": "do two things"}], capture_telemetry=True)

    assert response["choices"][0]["message"]["tool_calls"] == [
        {"id": "call_1", "type": "function", "function": {"name": "tool_a", "arguments": "{\"x\":1}"}},
        {"id": "call_2", "type": "function", "function": {"name": "tool_b", "arguments": "{\"y\":2}"}},
    ]


def test_capture_telemetry_pure_tool_call_reports_no_final_ttft(monkeypatch):
    """A WORK round that resolves to a pure tool call, with zero visible
    content, must never fabricate a Final TTFT/stream-duration -- there is
    no "final answer" in this round to time. Correctly absent, not zero."""
    backend = _make_backend()
    frames = [
        {"choices": [{"delta": {"reasoning": "deciding"}, "finish_reason": None}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}},
        ]}, "finish_reason": "tool_calls"}]},
    ]
    monkeypatch.setattr(requests, "post", lambda *a, **kw: _FakeStreamResp(_sse_lines(*frames)))
    ticks = iter([100.0, 100.1, 100.3])
    import core.backends.lmstudio as lmstudio_module
    monkeypatch.setattr(lmstudio_module.time, "monotonic", lambda: next(ticks))

    response = backend.chat([{"role": "user", "content": "do a thing"}], capture_telemetry=True)

    telemetry = backend.extract_response_telemetry(response)
    assert "final_ttft_s" not in telemetry
    assert "final_stream_duration_s" not in telemetry
    # The reasoning that preceded the tool decision is still honestly timed.
    assert telemetry["think_duration_s"] == pytest.approx(0.2)
    assert response["choices"][0]["message"]["reasoning_content"] == "deciding"


def test_capture_telemetry_openrouter_repeat_terminal_frame_usage_captured(monkeypatch):
    """Live-verified OpenRouter convention (already established for
    chat_stream() by CHAT-TELEMETRY-REGRESSION-01): the terminal frame
    repeats a second time, same finish_reason, only the repeat carrying
    top-level usage. Must not be missed just because terminal_seen is
    already True."""
    backend = _make_backend()
    frames = [
        {"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": ""}, "finish_reason": "stop"}]},
        {"choices": [{"delta": {"content": ""}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 19, "completion_tokens": 8, "total_tokens": 27}},
    ]
    monkeypatch.setattr(requests, "post", lambda *a, **kw: _FakeStreamResp(_sse_lines(*frames)))

    response = backend.chat([{"role": "user", "content": "hi"}], capture_telemetry=True)

    assert response["choices"][0]["message"]["content"] == "hi"
    assert response["usage"] == {"prompt_tokens": 19, "completion_tokens": 8, "total_tokens": 27}


def test_capture_telemetry_opencode_trailing_empty_choices_usage_captured(monkeypatch):
    """OpenCode Zen's convention: a SEPARATE trailing frame with empty
    choices carries usage."""
    backend = _make_backend()
    frames = [
        {"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"total_tokens": 12}},
    ]
    monkeypatch.setattr(requests, "post", lambda *a, **kw: _FakeStreamResp(_sse_lines(*frames)))

    response = backend.chat([{"role": "user", "content": "hi"}], capture_telemetry=True)

    assert response["usage"] == {"total_tokens": 12}


def test_capture_telemetry_requests_stream_true_ordinary_chat_still_requests_stream_false(monkeypatch):
    """The externally-visible WORK call stays logically non-streaming --
    one call in, one normalized dict out -- but the wire payload underneath
    genuinely requests `stream: true` only when capture_telemetry=True was
    asked for. Ordinary chat() (capture_telemetry omitted/False) must keep
    sending byte-identical `stream: false`, proving this is a pure opt-in
    with no change to default behavior."""
    backend = _make_backend()
    payloads = []

    def _fake_post(*a, **kw):
        payloads.append(kw["json"])
        if kw["json"].get("stream"):
            return _FakeStreamResp(_sse_lines(
                {"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]},
            ))
        return _FakeJsonResp({
            "choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })
    monkeypatch.setattr(requests, "post", _fake_post)

    backend.chat([{"role": "user", "content": "hi"}], capture_telemetry=True)
    backend.chat([{"role": "user", "content": "hi"}])

    assert payloads[0]["stream"] is True
    assert payloads[1]["stream"] is False


def test_capture_telemetry_empty_stream_raises_runtime_error_not_silent_success(monkeypatch):
    """A stream that produces nothing at all -- no content, no tool_calls,
    no reasoning, no finish_reason -- must fail loudly rather than return a
    silently empty "successful" response. Mirrors
    openai_backend.py's own _chat_with_stream_telemetry() posture for
    'stream ended before a terminal response event.'"""
    backend = _make_backend()
    monkeypatch.setattr(requests, "post", lambda *a, **kw: _FakeStreamResp(_sse_lines()))

    with pytest.raises(RuntimeError):
        backend.chat([{"role": "user", "content": "hi"}], capture_telemetry=True)


def test_capture_telemetry_mid_stream_disconnect_raises_connection_error(monkeypatch):
    """A genuine transport interruption mid-stream surfaces as the SAME
    builtin ConnectionError core/agent.py's _provider_chat_or_error()
    already knows how to classify -- no new, unconverted failure mode
    introduced by consuming the stream internally instead of externally."""
    backend = _make_backend()
    lines = _sse_lines({"choices": [{"delta": {"content": "partial"}, "finish_reason": None}]})[:-1]
    monkeypatch.setattr(requests, "post", lambda *a, **kw: _DisconnectingStreamResp(lines))

    with pytest.raises(ConnectionError):
        backend.chat([{"role": "user", "content": "hi"}], capture_telemetry=True)


def test_capture_telemetry_finish_reason_vocabulary_matches_non_streaming_termination(monkeypatch):
    """extract_termination() (inherited, unmodified, from BaseLLMBackend)
    must classify a capture_telemetry response identically to a genuine
    non-streaming one: stop/tool_calls -> COMPLETE, length -> INCOMPLETE."""
    backend = _make_backend()

    def _response_for(finish_reason):
        monkeypatch.setattr(
            requests, "post",
            lambda *a, **kw: _FakeStreamResp(_sse_lines(
                {"choices": [{"delta": {"content": "x"}, "finish_reason": finish_reason}]},
            )),
        )
        return backend.chat([{"role": "user", "content": "hi"}], capture_telemetry=True)

    assert backend.extract_termination(_response_for("stop")) == TerminationStatus.COMPLETE
    assert backend.extract_termination(_response_for("length")) == TerminationStatus.INCOMPLETE
    assert backend.extract_termination(_response_for("tool_calls")) == TerminationStatus.COMPLETE


def test_capture_telemetry_extract_response_telemetry_prefers_lumina_telemetry_key(monkeypatch):
    backend = _make_backend()
    monkeypatch.setattr(requests, "post", lambda *a, **kw: _FakeStreamResp(_sse_lines(
        {"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}},
    )))

    response = backend.chat([{"role": "user", "content": "hi"}], capture_telemetry=True)

    assert "_lumina_telemetry" in response
    assert backend.extract_response_telemetry(response) == response["_lumina_telemetry"]


def test_capture_telemetry_shared_by_openrouter_subclass_no_special_casing(monkeypatch):
    """OpenRouterBackend never overrides chat()/_chat_with_stream_telemetry()
    -- confirms the actual live target (GLM via OpenRouter) gets this for
    free through the shared LMStudioBackend implementation, exactly like
    CHAT-TELEMETRY-REGRESSION-01's usage fix already did."""
    backend = _make_backend(OpenRouterBackend)
    monkeypatch.setattr(requests, "post", lambda *a, **kw: _FakeStreamResp(_sse_lines(
        {"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}},
    )))

    response = backend.chat([{"role": "user", "content": "hi"}], capture_telemetry=True)

    assert response["choices"][0]["message"]["content"] == "hi"
    assert backend.extract_response_telemetry(response)["usage"] == {
        "prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7,
    }


# ── C. Agent-orchestration correctness ───────────────────────────────────
#
# Fake LLM shaped like the NOW-FIXED live backend: chat() genuinely accepts
# capture_telemetry (so _accepts_capture_telemetry() reads True, exactly
# like the real, now-patched LMStudioBackend/OpenRouterBackend), and a
# scripted turn's own `telemetry` dict is returned verbatim by
# extract_response_telemetry() when asked -- standing in for whatever the
# real _chat_with_stream_telemetry() genuinely observed (proven correct
# independently in Section B above). This isolates AGENT-side orchestration
# from backend-level SSE parsing, the same separation of concerns
# tests/test_chat_telemetry_regression_01.py already established against
# tests/test_lmstudio_backend.py.

class _CaptureTelemetryLLM:
    display_name = "OpenRouter"
    name = "openrouter"
    supports_required_tool_choice = True

    def __init__(self, turns):
        self.turns = list(turns)
        self.call_count = 0
        self.capture_requested = []

    def get_model(self):
        return "z-ai/glm-5.3-flash"

    def configured_model(self):
        return "z-ai/glm-5.3-flash"

    def chat(self, messages, tools=None, max_tokens=None, reasoning_effort=None,
             tool_choice_mode=None, capture_telemetry=False):
        idx = self.call_count
        self.call_count += 1
        self.capture_requested.append(capture_telemetry)
        return {"_turn": idx}

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
        return dict(self._entry(response).get("telemetry", {}))

    def is_tool_call(self, message):
        return bool(message.get("tool_calls"))

    def get_tool_calls(self, message):
        return message.get("tool_calls", [])

    def parse_tool_call(self, tc):
        return tc["function"]["name"], {}


def _agent_for_orchestration(llm, tmp_path):
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
        channel_id="promoted-candidate-streaming-test",
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
        flight_recorder=FlightRecorder(db_path=str(tmp_path / "promoted-candidate-streaming.db")),
    )
    agent._finalize_completion_candidate = types.MethodType(
        LuminaAgent._finalize_completion_candidate, agent)
    agent._finalize_with_reconciliation = types.MethodType(
        LuminaAgent._finalize_with_reconciliation, agent)
    agent._stream_final = types.MethodType(LuminaAgent._stream_final, agent)
    return agent, callbacks


def test_trivial_conversational_turn_restores_real_telemetry_gate_still_runs(tmp_path):
    """THE explicit regression this ticket exists to fix: "what's 7 times
    6?"-shaped turns now get real Final TTFT/stream/usage once the backend
    supports capture_telemetry -- flipping
    tests/test_chat_telemetry_regression_01.py's
    test_simple_no_tool_final_restores_real_usage_keeps_ttft_na's n/a
    result (that test's fake deliberately still reports no
    capture_telemetry support and is untouched by this ticket -- its
    result stays n/a, correctly, for a backend that still can't observe a
    boundary). Also proves no new provider round trip was introduced: still
    exactly WORK + gate, two requests, both requesting capture_telemetry --
    never three."""
    llm = _CaptureTelemetryLLM(turns=[
        {"content": "42", "termination": TerminationStatus.COMPLETE,
         "telemetry": {"usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
                       "final_ttft_s": 0.42, "final_stream_duration_s": 0.31,
                       "request_streamed": True}},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME, "1")],
         "telemetry": {"usage": {"prompt_tokens": 40, "completion_tokens": 5, "total_tokens": 45}}},
    ])
    agent, callbacks = _agent_for_orchestration(llm, tmp_path)

    result = LuminaAgent.chat(agent, "what's 7 times 6?")

    assert result == "42"
    assert llm.call_count == 2
    assert llm.capture_requested == [True, True]
    assert callbacks["final_ttft"] == [pytest.approx(0.42)]
    assert callbacks["stream"][0][0] == pytest.approx(0.31)
    assert callbacks["usage"][-1]["prompt_tokens"] == 90
    assert callbacks["usage"][-1]["completion_tokens"] == 15
    assert callbacks["usage"][-1]["total_tokens"] == 105


def test_tool_call_then_final_promoted_candidate_gets_real_telemetry_no_extra_request(tmp_path):
    """Tool-bearing variant of the same guard: real tool call, then a
    zero-tool candidate round, then gate finish -- three requests total
    (unchanged from before this ticket), real telemetry attributed to the
    candidate-producing round specifically, never fabricated for the
    tool-call round that produced no visible answer text."""
    llm = _CaptureTelemetryLLM(turns=[
        {"tool_calls": [_tc("tool_a", "1")],
         "telemetry": {"usage": {"prompt_tokens": 100, "completion_tokens": 15, "total_tokens": 115}}},
        {"content": "final answer", "termination": TerminationStatus.COMPLETE,
         "telemetry": {"usage": {"prompt_tokens": 130, "completion_tokens": 20, "total_tokens": 150},
                       "final_ttft_s": 0.2, "final_stream_duration_s": 0.1}},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME, "2")],
         "telemetry": {"usage": {"prompt_tokens": 60, "completion_tokens": 5, "total_tokens": 65}}},
    ])
    agent, callbacks = _agent_for_orchestration(llm, tmp_path)

    result = LuminaAgent.chat(agent, "do the thing")

    assert result == "final answer"
    assert callbacks["tools"] == ["tool_a"]
    assert llm.call_count == 3
    assert callbacks["final_ttft"] == [pytest.approx(0.2)]
    assert callbacks["usage"][-1]["total_tokens"] == 115 + 150 + 65


def test_control_gate_continue_still_discards_candidate_despite_telemetry_availability(tmp_path):
    """The load-bearing guard: telemetry availability must never influence
    the continue/finish decision. A first zero-tool candidate (with its own
    telemetry already attached) is explicitly discarded when the gate says
    continue_tool_work, a real tool call follows, and only the SECOND
    candidate is ever promoted -- proving AGENT-PRETOOL-ACTION-INTEGRITY-01's
    mandatory gate is completely unmodified by this ticket. If a future
    change ever lets telemetry availability short-circuit the gate, this
    test fails: `result` would become "partial answer" instead of "final
    answer", or the discarded round's final_ttft_s (0.05) would leak into
    callbacks["final_ttft"] instead of the promoted round's (0.2)."""
    llm = _CaptureTelemetryLLM(turns=[
        {"content": "partial answer", "termination": TerminationStatus.COMPLETE,
         "telemetry": {"usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
                       "final_ttft_s": 0.05, "final_stream_duration_s": 0.02}},
        {"tool_calls": [_tc(CONTINUE_TOOL_WORK_NAME, "g1")],
         "telemetry": {"usage": {"prompt_tokens": 8, "completion_tokens": 1, "total_tokens": 9}}},
        {"tool_calls": [_tc("tool_a", "2")],
         "telemetry": {"usage": {"prompt_tokens": 20, "completion_tokens": 3, "total_tokens": 23}}},
        {"content": "final answer", "termination": TerminationStatus.COMPLETE,
         "telemetry": {"usage": {"prompt_tokens": 30, "completion_tokens": 4, "total_tokens": 34},
                       "final_ttft_s": 0.2, "final_stream_duration_s": 0.1}},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME, "g2")],
         "telemetry": {"usage": {"prompt_tokens": 6, "completion_tokens": 1, "total_tokens": 7}}},
    ])
    agent, callbacks = _agent_for_orchestration(llm, tmp_path)

    result = LuminaAgent.chat(agent, "a multi-step question")

    assert result == "final answer"
    assert llm.call_count == 5
    assert callbacks["tools"] == ["tool_a"]
    assert callbacks["final_ttft"] == [pytest.approx(0.2)]
