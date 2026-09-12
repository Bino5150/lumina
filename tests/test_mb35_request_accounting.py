"""
MB-35-OPENROUTER-LIVE-WORK-TRANSPORT-01 -- request-accounting and
observability regressions.

Confirmed-defect campaign, three fixes covered here:

1. core/context.py's estimate_message_tokens() undercounted any message
   sibling field beyond "content"/"tool_calls" -- confirmed live for
   OpenRouter/GLM, whose tool-calls-bearing assistant message carries
   "reasoning"/"reasoning_details" siblings (core/backends/openrouter.py's
   _REASONING_FIELD_PRIORITY), stored verbatim by add_tool_call() and
   replayed on the wire, yet invisible to the old estimator. Fixed by
   measuring the WHOLE serialized message dict -- provider-neutral by
   construction, so it can't drift the next time a provider adds a new
   sibling key (section 13's design law).

2. core/context.py's build_messages() computed its usage snapshot BEFORE
   appending _ephemeral_messages, even though those messages ARE part of
   the returned/wire-bound list -- one-shot reconciliation material could
   be on the wire without being represented in "used_tokens"/"percent".

3. core/agent.py's _provider_chat_or_error() labeled EVERY provider
   exception "{provider} rejected the continuation", including a
   transport-class ConnectionError that never received any response at
   all. Now branches on exception class (ConnectionError/TimeoutError/
   RuntimeError/unclassified), and records sanitized structural dispatch
   telemetry via Flight Recorder ("provider.dispatch"/
   "provider.dispatch_failed") so the forensic questions this campaign
   raised (message/role counts, sibling-field byte footprint, sanitized
   transport cause) are answerable from the next live occurrence without
   guessing.

Every FlightRecorder here is its own tmp_path-isolated instance (test_
flight_recorder.py's own convention) -- never the production singleton.
"""
import json
import sqlite3
import types

import config
from core.agent import _provider_chat_or_error, _dispatch_measurements, is_error_response
from core.context import ContextManager, estimate_message_tokens
from core.flight_recorder import FlightRecorder


def _rows(fr):
    conn = sqlite3.connect(fr.db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _messages_fixture():
    """Structurally mirrors Chat 135's failing shape: a real user turn, a
    GLM-style tool-calls assistant message carrying reasoning/
    reasoning_details siblings, and its tool result."""
    return [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c0", "type": "function",
                          "function": {"name": "search_files", "arguments": "{}"}}],
         "reasoning": "some reasoning text",
         "reasoning_details": [{"type": "text", "text": "structured reasoning"}]},
        {"role": "tool", "tool_call_id": "c0", "name": "search_files", "content": "[]"},
    ]


class _FakeLLM:
    name = "openrouter"
    display_name = "OpenRouter"

    def __init__(self, exc=None, response=None):
        self._exc = exc
        self._response = response or {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    def get_model(self):
        return "z-ai/glm-5.3-flash"

    def configured_model(self):
        return "z-ai/glm-5.3-flash"

    def chat(self, **kwargs):
        if self._exc is not None:
            raise self._exc
        return self._response


# ── 1. estimate_message_tokens: sibling-field accounting gap ────────────

def test_estimate_message_tokens_counts_reasoning_sibling_fields():
    base = {"role": "assistant", "content": None,
            "tool_calls": [{"id": "c0", "type": "function",
                            "function": {"name": "x", "arguments": "{}"}}]}
    with_reasoning = dict(base, reasoning="x" * 4000,
                           reasoning_details=[{"type": "text", "text": "y" * 4000}])
    # ~8000 extra chars -> ~2000 extra tokens at the 4-chars/token model;
    # assert a large fraction of that actually shows up, not exactness.
    assert estimate_message_tokens(with_reasoning) > estimate_message_tokens(base) + 1500


def test_estimate_message_tokens_generalizes_to_unknown_future_sibling_field():
    """Section 13's design law: no hardcoded reasoning/reasoning_details
    special case -- an entirely novel future provider field must count
    too, or the estimator drifts again the next time a provider adds one."""
    base = {"role": "assistant", "content": "hi"}
    with_future_field = dict(base, some_future_provider_field="z" * 4000)
    assert estimate_message_tokens(with_future_field) > estimate_message_tokens(base) + 900


def test_estimate_message_tokens_tracks_actual_serialized_size():
    msg = {"role": "assistant", "content": "hello",
           "tool_calls": [{"id": "1", "type": "function",
                            "function": {"name": "f", "arguments": '{"a": 1}'}}],
           "reasoning": "some reasoning text here"}
    serialized_len = len(json.dumps(msg, default=str, ensure_ascii=False))
    assert estimate_message_tokens(msg) == serialized_len // 4 + 4


def test_estimate_message_tokens_never_raises_on_unserializable_field():
    class Weird:
        def __str__(self):
            return "weird-repr"

    msg = {"role": "assistant", "content": "hi", "custom": Weird()}
    assert estimate_message_tokens(msg) > 0


def test_estimate_message_tokens_handles_null_content_like_real_glm_response():
    """Live-verified GLM shape: content is JSON null (Python None) on a
    tool-calls-bearing message, not an empty string."""
    msg = {"role": "assistant", "content": None,
           "tool_calls": [{"id": "1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]}
    assert estimate_message_tokens(msg) > 0


# ── 2. build_messages(): ephemeral-message accounting gap ───────────────

def test_build_messages_usage_snapshot_includes_ephemeral_assistant_tokens():
    cm = ContextManager(owner=False)
    cm.add_user("hello")
    snapshot_without = cm.context_usage_snapshot(refresh=True)

    cm.push_ephemeral_assistant("Y" * 8000)
    messages = cm.build_messages()

    assert messages[-1] == {"role": "assistant", "content": "Y" * 8000}
    snapshot_with = cm._last_usage_snapshot
    assert snapshot_with["used_tokens"] > snapshot_without["used_tokens"] + 1500


def test_build_messages_ephemeral_tokens_do_not_change_history_trim_behavior():
    """The fix only corrects the reported snapshot -- it must not change
    what _fit_history_to_budget() actually trims. Ephemeral messages keep
    bypassing trimming by design (SEPT-AC-R1-F03/F04)."""
    cm = ContextManager(owner=False)
    cm.add_user("first")
    cm.add_user("second")
    cm.push_ephemeral_assistant("reconciliation material")
    messages = cm.build_messages()

    real_roles = [m["role"] for m in messages[:-1]]
    assert real_roles.count("user") == 2
    assert messages[-1] == {"role": "assistant", "content": "reconciliation material"}


def test_context_usage_snapshot_unaffected_outside_a_turn():
    """context_usage_snapshot() (the passive/UI-refresh read) never touches
    _ephemeral_messages and must stay unaffected by this fix -- there is
    nothing ephemeral to include outside of an actual build_messages() call."""
    cm = ContextManager(owner=False)
    cm.add_user("hello")
    snap1 = cm.context_usage_snapshot(refresh=True)
    snap2 = cm.context_usage_snapshot(refresh=True)
    assert snap1 == snap2


def test_tool_result_truncation_still_enforced():
    """Regression guard (mission test #7) -- untouched by this campaign,
    verified directly since it feeds the same accounting path."""
    cm = ContextManager(owner=False)
    huge = "x" * (config.TOOL_RESULT_MAX_CHARS + 5000)
    cm.add_tool_result("id1", "some_tool", huge)
    stored = cm.history[-1]["content"]
    assert huge not in stored
    assert len(stored) < len(huge)


# ── 3. _provider_chat_or_error(): exception-class-accurate wording ──────

def test_connection_error_does_not_claim_provider_rejection():
    tokens = []
    agent = types.SimpleNamespace(
        llm=_FakeLLM(exc=ConnectionError("OpenRouter not reachable at https://openrouter.ai/api/v1.")),
        on_response_token=lambda t: tokens.append(t),
    )
    chat_kwargs = {"messages": _messages_fixture(), "tools": []}
    response, err = _provider_chat_or_error(agent, chat_kwargs, None, {"search_files"})
    assert response is None
    assert "search_files" in err
    assert "completed" in err
    assert "rejected the continuation" not in err
    assert "before a response was received" in err
    assert tokens == [err]


def test_timeout_error_does_not_claim_provider_rejection():
    agent = types.SimpleNamespace(
        llm=_FakeLLM(exc=TimeoutError("OpenRouter request timed out.")),
        on_response_token=lambda t: None,
    )
    chat_kwargs = {"messages": _messages_fixture(), "tools": []}
    _, err = _provider_chat_or_error(agent, chat_kwargs, None, {"search_files"})
    assert "rejected the continuation" not in err
    assert "timed out waiting for a response" in err


def test_runtime_error_still_reads_as_genuine_provider_rejection():
    """RuntimeError is every backend's own convention for "an HTTP response
    was actually received and treated as an error" (format_provider_error()
    / format_gemini_error() / format_anthropic_error()) -- this is the ONE
    case where "rejected" is an accurate claim, and it must stay that way."""
    agent = types.SimpleNamespace(
        llm=_FakeLLM(exc=RuntimeError("OpenRouter error (billing_required, HTTP 402): no credits")),
        on_response_token=lambda t: None,
    )
    chat_kwargs = {"messages": _messages_fixture(), "tools": []}
    _, err = _provider_chat_or_error(agent, chat_kwargs, None, {"search_files"})
    assert "rejected the continuation" in err


def test_unclassified_exception_neither_claims_rejection_nor_transport():
    class WeirdError(Exception):
        pass

    agent = types.SimpleNamespace(
        llm=_FakeLLM(exc=WeirdError("something else entirely")),
        on_response_token=lambda t: None,
    )
    chat_kwargs = {"messages": _messages_fixture(), "tools": []}
    _, err = _provider_chat_or_error(agent, chat_kwargs, None, {"search_files"})
    assert "rejected the continuation" not in err
    assert "before a response was received" not in err
    assert "failed before a response was confirmed" in err


def test_all_continuation_failure_wordings_are_recognized_error_responses():
    for exc in (ConnectionError("down"), TimeoutError("slow"), RuntimeError("boom"), ValueError("weird")):
        agent = types.SimpleNamespace(llm=_FakeLLM(exc=exc), on_response_token=lambda t: None)
        _, err = _provider_chat_or_error(agent, {"messages": _messages_fixture(), "tools": []},
                                          None, {"search_files"})
        assert is_error_response(err) is True, f"{type(exc).__name__} not recognized: {err!r}"


def test_no_flight_recorder_attribute_is_a_safe_noop():
    """Every existing SimpleNamespace fake agent across this file's sibling
    tests (test_agent_tool_continuation.py etc.) has no .flight_recorder --
    dispatch instrumentation must be a total no-op for them, never an
    AttributeError, exactly like every other _fr_machine() call site."""
    agent = types.SimpleNamespace(llm=_FakeLLM(), on_response_token=lambda t: None)
    response, err = _provider_chat_or_error(agent, {"messages": [], "tools": []}, None, set())
    assert err is None
    assert response is not None


# ── 4. dispatch instrumentation: structural, sanitized, non-mutating ────

def test_dispatch_measurements_reports_structural_forensic_fields():
    chat_kwargs = {"messages": _messages_fixture(),
                   "tools": [{"type": "function", "function": {"name": "search_files"}}]}
    fields = _dispatch_measurements(chat_kwargs)
    assert fields["message_count"] == 3
    assert fields["role_counts"] == {"user": 1, "assistant": 1, "tool": 1}
    assert fields["tool_call_message_count"] == 1
    assert fields["sibling_bearing_tool_call_message_count"] == 1
    assert sorted(fields["sibling_field_names"]) == ["reasoning", "reasoning_details"]
    assert fields["sibling_field_bytes"] > 0
    assert fields["messages_bytes"] > 0
    assert fields["schema_bytes"] > 0
    assert fields["multipart_message_count"] == 0


def test_dispatch_measurements_flags_multipart_messages():
    messages = [{"role": "user", "content": [{"type": "text", "text": "hi"},
                                              {"type": "image_url", "image_url": {"url": "data:..."}}]}]
    fields = _dispatch_measurements({"messages": messages, "tools": []})
    assert fields["multipart_message_count"] == 1


def test_provider_dispatch_event_recorded_with_structural_fields(tmp_path):
    fr = FlightRecorder(db_path=str(tmp_path / "fr.db"))
    agent = types.SimpleNamespace(llm=_FakeLLM(), flight_recorder=fr, on_response_token=lambda t: None)
    chat_kwargs = {"messages": _messages_fixture(),
                   "tools": [{"type": "function", "function": {"name": "search_files"}}]}
    _provider_chat_or_error(agent, chat_kwargs, None, set(), turn_id="t1", chat_id=42)

    rows = _rows(fr)
    dispatch_rows = [r for r in rows if r["event_type"] == "provider.dispatch"]
    assert len(dispatch_rows) == 1
    fields = json.loads(dispatch_rows[0]["fields_json"])
    assert fields["message_count"] == 3
    assert fields["tool_call_message_count"] == 1
    assert sorted(fields["sibling_field_names"]) == ["reasoning", "reasoning_details"]
    assert dispatch_rows[0]["turn_id"] == "t1"
    assert dispatch_rows[0]["chat_id"] == 42
    assert dispatch_rows[0]["backend"] == "openrouter"


def test_provider_dispatch_failed_event_never_leaks_secrets(tmp_path):
    fr = FlightRecorder(db_path=str(tmp_path / "fr.db"))
    secret = "sk-realsecretkey1234567890abcdef"
    agent = types.SimpleNamespace(
        llm=_FakeLLM(exc=ConnectionError(f"failed, Authorization: Bearer {secret} leaked")),
        flight_recorder=fr, on_response_token=lambda t: None,
    )
    _provider_chat_or_error(agent, {"messages": _messages_fixture(), "tools": []}, None, {"x"})

    rows = _rows(fr)
    failed_rows = [r for r in rows if r["event_type"] == "provider.dispatch_failed"]
    assert len(failed_rows) == 1
    raw = failed_rows[0]["fields_json"]
    assert secret not in raw
    fields = json.loads(raw)
    assert fields["exception_type"] == "ConnectionError"
    assert fields["stage"] == "tool_continuation"


def test_provider_dispatch_never_records_raw_message_content(tmp_path):
    """Structural telemetry only -- never a context-history dump (section
    11/12). Flight Recorder's own _looks_like_conversation() sanitizer
    guard also catches this at the storage layer, but the fields handed to
    it must never even carry real conversation text."""
    fr = FlightRecorder(db_path=str(tmp_path / "fr.db"))
    agent = types.SimpleNamespace(llm=_FakeLLM(), flight_recorder=fr, on_response_token=lambda t: None)
    _provider_chat_or_error(agent, {"messages": _messages_fixture(), "tools": []}, None, set())

    rows = _rows(fr)
    raw = rows[0]["fields_json"]
    assert "search_files" not in raw
    assert "some reasoning text" not in raw
    assert "structured reasoning" not in raw


def test_provider_dispatch_instrumentation_never_mutates_context_history(tmp_path):
    """Section 11/14 #13 -- telemetry must never mutate the durable
    transcript. ctx.history is untouched by a dispatch call regardless of
    success or failure."""
    fr = FlightRecorder(db_path=str(tmp_path / "fr.db"))
    cm = ContextManager(owner=False)
    cm.add_user("hello")
    before = list(cm.history)
    messages = cm.build_messages()

    agent = types.SimpleNamespace(llm=_FakeLLM(), flight_recorder=fr, on_response_token=lambda t: None)
    _provider_chat_or_error(agent, {"messages": messages, "tools": []}, None, set())
    agent_fail = types.SimpleNamespace(llm=_FakeLLM(exc=ConnectionError("down")), flight_recorder=fr,
                                        on_response_token=lambda t: None)
    _provider_chat_or_error(agent_fail, {"messages": messages, "tools": []}, None, {"x"})

    assert cm.history == before
