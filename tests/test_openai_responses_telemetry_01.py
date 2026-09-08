"""OPENAI-RESPONSES-TELEMETRY-01 regression coverage.

Provider traffic is intercepted.  These tests exercise the native Responses
terminal-usage/event seam, foreground multi-round aggregation, protected tool
continuity, Flight Recorder fields, and the existing GUI footer contract.
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import sqlite3
import types

import pytest
import requests

import core.backends.openai_backend as openai_module
from core.agent import LuminaAgent, FINISH_TOOL_WORK_NAME
from core.backends.base import BackendStreamTelemetry, TerminationStatus
from core.backends.openai_backend import OpenAIBackend
from core.context import estimate_tokens
from core.flight_recorder import FlightRecorder


@pytest.fixture
def qapp():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _usage(prompt, completion, *, cached=0, reasoning=0):
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "prompt_tokens_details": {"cached_tokens": cached},
        "completion_tokens_details": {"reasoning_tokens": reasoning},
    }


def _native_usage(prompt, completion, *, cached=0, reasoning=0):
    return {
        "input_tokens": prompt,
        "output_tokens": completion,
        "total_tokens": prompt + completion,
        "input_tokens_details": {"cached_tokens": cached},
        "output_tokens_details": {"reasoning_tokens": reasoning},
    }


class _StreamResponse:
    status_code = 200
    text = ""

    def __init__(self, events):
        self.events = list(events)

    def raise_for_status(self):
        return None

    def iter_lines(self):
        for event in self.events:
            yield f"data: {json.dumps(event)}".encode("utf-8")


def test_capture_stream_uses_terminal_body_and_real_event_boundaries(monkeypatch):
    body = {
        "status": "completed",
        "output": [{
            "type": "message",
            "content": [{"type": "output_text", "text": "answer"}],
        }],
        "usage": _native_usage(100, 40, cached=20, reasoning=15),
    }
    events = [
        {"type": "response.reasoning_summary_text.delta", "delta": "checking"},
        {"type": "response.reasoning_summary_text.done", "text": "checking"},
        {"type": "response.function_call_arguments.delta", "delta": "{\"x\":"},
        {"type": "response.output_text.delta", "delta": "answer"},
        {"type": "response.completed", "response": body},
    ]
    payloads = []
    monkeypatch.setattr(requests, "post", lambda *a, **kw: (
        payloads.append(kw["json"]) or _StreamResponse(events)
    ))
    ticks = iter((10.0, 10.1, 10.4, 10.8, 11.0, 11.5))
    monkeypatch.setattr(openai_module.time, "monotonic", lambda: next(ticks))
    backend = OpenAIBackend(api_key="test-key")
    backend._model = "gpt-5.6-luna"

    response = backend.chat(
        [{"role": "user", "content": "test"}],
        reasoning_effort="medium",
        capture_telemetry=True,
    )

    assert payloads[0]["stream"] is True
    assert response["choices"][0]["message"]["content"] == "answer"
    telemetry = backend.extract_response_telemetry(response)
    assert telemetry["usage"] == _usage(100, 40, cached=20, reasoning=15)
    assert telemetry["final_ttft_s"] == pytest.approx(1.0)
    assert telemetry["think_duration_s"] == pytest.approx(0.3)
    assert telemetry["final_stream_duration_s"] == pytest.approx(0.5)


def test_chat_stream_emits_terminal_usage_as_non_text_event(monkeypatch):
    body = {
        "status": "completed",
        "output": [],
        "usage": _native_usage(11, 5, cached=3, reasoning=2),
    }
    events = [
        {"type": "response.output_text.delta", "delta": "OK"},
        {"type": "response.completed", "response": body},
    ]
    monkeypatch.setattr(requests, "post", lambda *a, **kw: _StreamResponse(events))
    backend = OpenAIBackend(api_key="test-key")
    backend._model = "gpt-5.6-luna"

    chunks = list(backend.chat_stream(
        [{"role": "user", "content": "test"}], reasoning_effort="none",
    ))

    assert chunks[0] == "OK"
    assert isinstance(chunks[1], BackendStreamTelemetry)
    assert chunks[1].fields["usage"] == _usage(11, 5, cached=3, reasoning=2)


def test_stream_without_terminal_usage_emits_no_bogus_usage_event(monkeypatch):
    events = [
        {"type": "response.output_text.delta", "delta": "partial"},
        {"type": "response.failed", "response": {
            "status": "failed", "output": [], "usage": None,
        }},
    ]
    monkeypatch.setattr(requests, "post", lambda *a, **kw: _StreamResponse(events))
    backend = OpenAIBackend(api_key="test-key")
    backend._model = "gpt-5.6-luna"

    chunks = list(backend.chat_stream(
        [{"role": "user", "content": "test"}], reasoning_effort="none",
    ))

    assert chunks == ["partial"]
    assert not any(isinstance(chunk, BackendStreamTelemetry) for chunk in chunks)


def _tc(name, call_id):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


class _TelemetryLLM:
    name = "telemetry-control"
    display_name = "Telemetry Control"

    def __init__(self):
        self.calls = []
        self.responses = [
            {
                "message": {
                    "content": "",
                    "reasoning_content": "I should use both tools.",
                    "tool_calls": [_tc("tool_a", "a"), _tc("tool_b", "b")],
                },
                "usage": _usage(100, 30, cached=10, reasoning=12),
                "telemetry": {"think_duration_s": 0.4},
            },
            {
                "message": {"content": "final answer"},
                "usage": _usage(80, 20, cached=5, reasoning=0),
                "telemetry": {
                    "final_ttft_s": 0.5,
                    "final_stream_duration_s": 0.6,
                },
                "termination": TerminationStatus.COMPLETE,
            },
            {
                "message": {"content": "", "tool_calls": [
                    _tc(FINISH_TOOL_WORK_NAME, "finish"),
                ]},
                "usage": _usage(60, 5, cached=0, reasoning=0),
            },
        ]

    def configured_model(self):
        return "control-model"

    def get_model(self):
        return "control-model"

    def chat(self, messages, tools=None, max_tokens=None, reasoning_effort=None,
             tool_choice_mode=None, capture_telemetry=False):
        self.calls.append(capture_telemetry)
        return {"index": len(self.calls) - 1}

    def _entry(self, response):
        return self.responses[response["index"]]

    def extract_message(self, response):
        return self._entry(response)["message"]

    def extract_termination(self, response):
        return self._entry(response).get("termination", TerminationStatus.COMPLETE)

    def extract_reasoning(self, response):
        return self._entry(response)["message"].get("reasoning_content")

    def extract_response_telemetry(self, response):
        entry = self._entry(response)
        return {"usage": entry["usage"], **entry.get("telemetry", {})}

    def is_tool_call(self, message):
        return bool(message.get("tool_calls"))

    def get_tool_calls(self, message):
        return message.get("tool_calls", [])

    def parse_tool_call(self, tool_call):
        return tool_call["function"]["name"], {}

    def chat_stream(self, messages, max_tokens=None, reasoning_effort=None):
        raise AssertionError("captured completion candidate must not be regenerated")


def _agent_for_telemetry(llm, tmp_path):
    history = []
    callbacks = {
        "usage": [], "think_timing": [], "final_ttft": [],
        "stream": [], "tools": [], "response": [],
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
            {"role": "tool", "tool_call_id": call_id, "name": name,
             "content": result}),
        add_cancelled_tool_result=lambda call_id, name: None,
        push_ephemeral=lambda block: None,
        build_messages=lambda tool_budget=0, chat_id=None: list(history),
        context_usage_snapshot=lambda tool_budget=0, chat_id=None, refresh=False: {
            "used_tokens": 100, "max_tokens": 8000, "percent": 1.25,
        },
    )
    registry = types.SimpleNamespace(
        schema_token_estimate=lambda: 10,
        get_schemas=lambda: [],
        list_enabled=lambda: ["tool_a", "tool_b"],
        call=lambda name, args: f"{name} result",
    )
    agent = types.SimpleNamespace(
        llm=llm,
        ctx=ctx,
        registry=registry,
        owner=True,
        channel_id="telemetry-test",
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
        on_time_to_first_answer=lambda duration: None,
        tts=None,
        _session_tool_calls=0,
        _skill_nudge_sent=False,
        flight_recorder=FlightRecorder(db_path=str(tmp_path / "telemetry.db")),
    )
    agent._finalize_completion_candidate = types.MethodType(
        LuminaAgent._finalize_completion_candidate, agent,
    )
    agent._finalize_with_reconciliation = types.MethodType(
        LuminaAgent._finalize_with_reconciliation, agent,
    )
    agent._stream_final = types.MethodType(LuminaAgent._stream_final, agent)
    return agent, callbacks


def test_reasoning_multi_tool_turn_aggregates_usage_timings_and_recorder(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr("core.agent.build_skills_block", lambda text: "")
    llm = _TelemetryLLM()
    agent, callbacks = _agent_for_telemetry(llm, tmp_path)

    result = LuminaAgent.chat(agent, "use both tools")

    assert result == "final answer"
    assert llm.calls == [True, True, True]
    assert callbacks["tools"] == ["tool_a", "tool_b"]
    assert callbacks["usage"][-1] == {
        "prompt_tokens": 240,
        "completion_tokens": 55,
        "total_tokens": 295,
        "cached_tokens": 15,
        "reasoning_tokens": 12,
        "provider_rounds": 3,
        "usage_consistent": True,
    }
    assert callbacks["think_timing"] == [pytest.approx(0.4)]
    assert callbacks["final_ttft"] == [pytest.approx(0.5)]
    assert callbacks["stream"] == [
        (pytest.approx(0.6), estimate_tokens("final answer")),
    ]

    conn = sqlite3.connect(agent.flight_recorder.db_path)
    row = conn.execute(
        "SELECT fields_json FROM events WHERE event_type='turn.completed'",
    ).fetchone()
    final_row = conn.execute(
        "SELECT fields_json FROM events WHERE event_type='turn.final'",
    ).fetchone()
    conn.close()
    fields = json.loads(row[0])
    assert fields["provider_rounds"] == 3
    assert fields["provider_rounds_by_phase"] == {"work": 2, "gate": 1}
    assert fields["input_count"] == 240
    assert fields["output_count"] == 55
    assert fields["total_count"] == 295
    assert fields["cached_input_count"] == 15
    assert fields["reasoning_output_count"] == 12
    assert fields["final_ttft_s"] == pytest.approx(0.5)
    assert fields["final_stream_duration_s"] == pytest.approx(0.6)
    final_fields = json.loads(final_row[0])
    assert final_fields["duration_s"] == pytest.approx(0.6)
    assert final_fields["token_count"] == "[REDACTED]"
    assert final_fields["visible_output_count"] == estimate_tokens("final answer")


def test_gui_footer_prefers_provider_usage_and_preserves_non_openai_fallback(
    qapp, monkeypatch,
):
    import ui.chat_widget as chat_widget_module
    from ui.chat_widget import LiveResponseBubble
    from ui.main_window import COLORS

    ticks = iter((1.0, 3.0, 10.0, 12.0))
    monkeypatch.setattr(chat_widget_module.time, "monotonic", lambda: next(ticks))

    provider = LiveResponseBubble(COLORS)
    provider.append_response_token("batched ")
    provider.append_response_token("answer")
    provider.set_token_usage({
        "prompt_tokens": 25,
        "completion_tokens": 9,
        "total_tokens": 34,
        "cached_tokens": 4,
        "reasoning_tokens": 3,
    })
    provider.set_stream_timing(2.0, 8)
    provider.add_think_timing(0.01)
    provider.finalize()
    provider_text = provider.metrics.lbl.text()
    assert "25in / 9out / 34total" in provider_text
    assert "0in / 2out / 2total" not in provider_text
    assert "4.0 tok/s" in provider_text
    assert "think: <0.1s" in provider_text
    assert provider._provider_usage["cached_tokens"] == 4
    assert provider._provider_usage["reasoning_tokens"] == 3

    fallback = LiveResponseBubble(COLORS)
    fallback.append_response_token("one")
    fallback.append_response_token("two")
    fallback.finalize()
    assert "0in / 2out / 2total" in fallback.metrics.lbl.text()


def test_agent_worker_routes_usage_and_think_timing_as_dedicated_signals(qapp):
    from ui.main_window import AgentWorker, StreamSignals

    class _Agent:
        def chat(self, user_input, chat_id=None, cancel_event=None,
                 reasoning_effort=None):
            self.on_token_usage(_usage(12, 4, cached=2, reasoning=1))
            self.on_think_timing(0.25)
            self.on_response_token("done")
            return "done"

    signals = StreamSignals()
    received_usage = []
    received_think = []
    signals.token_usage.connect(received_usage.append)
    signals.think_timing.connect(received_think.append)

    AgentWorker(_Agent(), "question", signals, chat_id=1).run()

    assert received_usage == [_usage(12, 4, cached=2, reasoning=1)]
    assert received_think == [pytest.approx(0.25)]
