"""
AGENT-WORK-COMPLETE-DISCARD-01 -- Completion-Candidate Preservation.

Live-reproduced defect (AGENT-GLM-THINK-TOOL-TRANSITION-01, and reconfirmed
live against real OpenRouter/z-ai/glm-5.3-flash for this ticket): once tool
work has begun this turn, a WORK-round response that arrives with
finish_reason=stop (TerminationStatus.COMPLETE or UNKNOWN) and genuinely
zero tool_calls is a real, complete assistant answer -- but core/agent.py's
_chat_impl() discarded it unconditionally (no turn.commentary, no
turn.think, zero Flight Recorder trace) and routed to the completion gate
with no memory of it. When the gate then confirmed finish_tool_work, the
old code re-asked the provider for a fresh answer via _stream_final() ->
chat_stream() -- a second generation call with no knowledge the first
answer ever existed, occasionally producing a context-blind, worse final
answer than the one already in hand.

This file exercises the fix: such a response is preserved as an explicit,
turn-scoped "completion candidate" (see _chat_impl()'s candidate-creation
site and _finalize_completion_candidate()). The gate still runs and still
decides everything -- a candidate is never authoritative merely by
existing:
  - gate "finish_tool_work"  -> the candidate IS promoted directly, with
                                 NO further provider call (no chat_stream()
                                 regeneration).
  - gate "continue_tool_work" -> the candidate is discarded; it can never
                                 leak into a later, different final answer.
  - a real tool call at any point supersedes/invalidates any candidate.
  - cancellation/failure never let a candidate leak across a turn boundary.
  - the candidate is never treated as Commentary (on_commentary/
    turn.commentary), which is reserved for outward narration accompanying
    a structural tool/control decision -- not a bare prose answer.

Reuses the types.SimpleNamespace fake-agent pattern established across
tests/test_agent_continuation_control_gate.py, tests/test_agent_tool_think.py,
and tests/test_agent_flight_recorder_integration.py (read directly before
writing this file) -- LuminaAgent.chat(fake_self, ...) called unbound
against a minimal stand-in, with _stream_final AND
_finalize_completion_candidate bound on as real methods so both the
regeneration path and the new direct-promotion path genuinely run rather
than being mocked away. Extends the _ScriptedLLM fake with a
chat_stream_calls counter (none of the existing files needed one -- this
ticket's whole point is proving a SECOND provider call does NOT happen).
"""
import json
import sqlite3
import threading
import types

import pytest

from core.agent import (
    LuminaAgent,
    TurnCancelled,
    FINISH_TOOL_WORK_NAME,
    CONTINUE_TOOL_WORK_NAME,
)
from core.backends.base import TerminationStatus
from core.flight_recorder import FlightRecorder


@pytest.fixture(autouse=True)
def _no_skill_injection(monkeypatch):
    """Same pattern as the other agent test files -- without this, a real
    skill matched against this file's literal prompt strings can add an
    extra, unrelated ephemeral push that has nothing to do with completion-
    candidate behavior under test."""
    monkeypatch.setattr("core.agent.build_skills_block", lambda user_input: "")


def _tc(name, call_id=None):
    return {"id": call_id or name, "type": "function",
            "function": {"name": name, "arguments": "{}"}}


class _ScriptedLLM:
    """Same scripted-turn fake as the other AGENT-CONTINUATION-CONTROL-
    GATE-01A test files, extended with chat_stream_calls -- the one new
    thing this ticket's regressions need to assert directly: that a
    preserved candidate is promoted WITHOUT a second (streaming) provider
    call."""
    display_name = "FakeProvider"
    name = "fake-backend"
    supports_required_tool_choice = True

    def __init__(self, turns):
        self.turns = list(turns)
        self.call_count = 0
        self.chat_stream_calls = 0
        self.tools_seen = []

    def get_model(self):
        return "fake-model"

    def configured_model(self):
        return "fake-model-configured"

    def chat(self, messages, tools=None, max_tokens=None, reasoning_effort=None,
              tool_choice_mode=None):
        self.tools_seen.append([t["function"]["name"] for t in (tools or [])])
        idx = self.call_count
        self.call_count += 1
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
        turn = self.turns[response["_turn"]]
        return turn.get("termination", TerminationStatus.COMPLETE)

    def extract_reasoning(self, response):
        turn = self.turns[response["_turn"]]
        return turn.get("reasoning")

    def is_tool_call(self, message):
        return bool(message.get("tool_calls"))

    def get_tool_calls(self, message):
        return message.get("tool_calls", [])

    def parse_tool_call(self, tc):
        return tc["function"]["name"], {}

    def chat_stream(self, messages, max_tokens=None, reasoning_effort=None):
        self.chat_stream_calls += 1
        yield "REGENERATED-FROM-CHAT-STREAM"


def _fake_agent(llm, tool_result="ok", flight_recorder_path=None):
    history = []
    calls = {
        "ephemeral": [], "registry_calls": [], "on_tool_call": [],
        "on_tool_result": [], "response_tokens": [], "commentary": [],
        "build_messages_calls": 0,
    }

    def registry_call(name, args):
        calls["registry_calls"].append(name)
        return tool_result

    def build_messages(tool_budget=0, chat_id=None):
        calls["build_messages_calls"] += 1
        return [{"role": "system", "content": f"system-prompt-build-{calls['build_messages_calls']}"}]

    ctx = types.SimpleNamespace(
        history=history,
        max_tokens=8000,
        add_user=lambda content, source="OWNER_DIRECT": history.append(
            {"role": "user", "content": content}),
        add_assistant=lambda content: history.append(
            {"role": "assistant", "content": content}),
        add_tool_call=lambda message: history.append(message),
        add_tool_result=lambda tool_call_id, name, result: history.append(
            {"role": "tool", "tool_call_id": tool_call_id, "name": name, "content": result}),
        add_cancelled_tool_result=lambda tool_call_id, name: history.append(
            {"role": "tool", "tool_call_id": tool_call_id, "name": name,
             "content": "[Cancelled by operator before execution.]"}),
        push_ephemeral=lambda block: calls["ephemeral"].append(block),
        build_messages=build_messages,
        context_usage_snapshot=lambda tool_budget=0, chat_id=None, refresh=False: (
            {"used_tokens": 1, "max_tokens": 8000, "percent": 0.0, "chat_id": chat_id}
        ),
    )
    registry = types.SimpleNamespace(
        schema_token_estimate=lambda: 0,
        get_schemas=lambda: [
            {"type": "function", "function": {"name": "search_memory", "description": "", "parameters": {}}},
            {"type": "function", "function": {"name": "read_file", "description": "", "parameters": {}}},
        ],
        list_enabled=lambda: ["search_memory", "read_file"],
        all_tool_names=lambda: ["search_memory", "read_file"],
        call=registry_call,
    )
    ns = types.SimpleNamespace(
        llm=llm,
        ctx=ctx,
        registry=registry,
        channel_id="test-channel",
        owner=True,
        on_tool_call=lambda name, args: calls["on_tool_call"].append((name, args)),
        on_tool_result=lambda name, result: calls["on_tool_result"].append((name, result)),
        on_think_start=lambda step: None,
        on_think_token=lambda tok: None,
        on_think_end=lambda: None,
        on_response_token=lambda tok: calls["response_tokens"].append(tok),
        on_commentary=lambda text: calls["commentary"].append(text),
        tts=None,
        _session_tool_calls=0,
        _skill_nudge_sent=False,
    )
    ns._stream_final = types.MethodType(LuminaAgent._stream_final, ns)
    ns._finalize_completion_candidate = types.MethodType(LuminaAgent._finalize_completion_candidate, ns)
    ns._finalize_with_reconciliation = types.MethodType(LuminaAgent._finalize_with_reconciliation, ns)
    if flight_recorder_path is not None:
        ns.flight_recorder = FlightRecorder(db_path=str(flight_recorder_path))
    return ns, calls


def _events(ns):
    conn = sqlite3.connect(ns.flight_recorder.db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── 1. Candidate survives gate acceptance, no extra provider call ───────

def test_candidate_survives_gate_acceptance_with_no_regeneration_call():
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "Here is the complete answer you asked for.",
         "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "find it")

    assert result == "Here is the complete answer you asked for."
    # The load-bearing assertion this whole ticket is about: NO second
    # provider generation call happened for the final answer.
    assert llm.chat_stream_calls == 0
    assert "REGENERATED-FROM-CHAT-STREAM" not in result
    # Persisted to history exactly like an ordinary streamed final would be.
    assert fake.ctx.history[-1] == {
        "role": "assistant", "content": "Here is the complete answer you asked for."
    }


def test_candidate_promotion_delivers_via_on_response_token():
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "Complete candidate text.", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    LuminaAgent.chat(fake, "find it")

    # AGENT-PRETOOL-ACTION-INTEGRITY-01: delivered through the same
    # chunked on_response_token() channel a real stream uses (see
    # _deliver_held_text()), not one single call -- reconstructing the
    # chunks must reproduce the exact candidate text byte-for-byte, and
    # this candidate is long enough to span more than one chunk.
    assert "".join(calls["response_tokens"]) == "Complete candidate text."
    assert len(calls["response_tokens"]) > 1


# ── 2. "continue" discards the candidate -- it never leaks ──────────────

def test_continue_discards_candidate_and_later_candidate_becomes_final():
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "CANDIDATE-A (stale)", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(CONTINUE_TOOL_WORK_NAME)]},        # discards A
        {"tool_calls": [_tc("read_file")]},                    # real work
        {"content": "CANDIDATE-B (final)", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "find and read it, iteratively")

    assert result == "CANDIDATE-B (final)"
    assert "CANDIDATE-A" not in result
    assert llm.chat_stream_calls == 0
    # A was never delivered to the UI or persisted anywhere.
    assert not any("CANDIDATE-A" in str(h) for h in fake.ctx.history)
    assert all("CANDIDATE-A" not in tok for tok in calls["response_tokens"])


def test_continue_discards_candidate_even_when_no_new_candidate_follows():
    """Isolates discard-on-continue from candidate-B-overwrites-candidate-A:
    here NO second candidate ever forms (the post-continue WORK round makes
    a real tool call, then the round after that has EMPTY content) -- if
    "continue" had not explicitly discarded the stale candidate, it would
    incorrectly survive to be promoted on the eventual finish."""
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "STALE-CANDIDATE-MUST-NOT-SURVIVE", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(CONTINUE_TOOL_WORK_NAME)]},   # must discard the stale candidate
        {"tool_calls": [_tc("read_file")]},               # real work, forms no new candidate
        {"content": "", "termination": TerminationStatus.COMPLETE},  # WORK -> gate, no candidate
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},     # finish -- must regenerate, not promote stale
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "find and read it, iteratively")

    assert result == "REGENERATED-FROM-CHAT-STREAM"
    assert llm.chat_stream_calls == 1
    assert "STALE-CANDIDATE-MUST-NOT-SURVIVE" not in result


def test_continue_with_no_candidate_is_unaffected():
    """The ordinary (pre-existing) shape -- empty-content trigger response
    -- must still work exactly as before: no candidate ever exists, so
    "continue" has nothing to discard and behavior is byte-identical to
    pre-fix."""
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(CONTINUE_TOOL_WORK_NAME)]},
        {"tool_calls": [_tc("read_file")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "find and read it")

    assert result == "REGENERATED-FROM-CHAT-STREAM"
    assert llm.chat_stream_calls == 1


# ── 3. Candidate is not Commentary ───────────────────────────────────────

def test_candidate_content_never_fires_on_commentary():
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "This reads like outward narration but is not a tool call.",
         "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "find it")

    assert result == "This reads like outward narration but is not a tool call."
    assert calls["commentary"] == []


# ── 4. No duplication: candidate output XOR streamed regenerated output ──

def test_final_output_is_never_both_candidate_and_regenerated():
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "Only this text should ever appear.",
         "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "find it")

    assert result == "Only this text should ever appear."
    assert "REGENERATED-FROM-CHAT-STREAM" not in result
    # AGENT-PRETOOL-ACTION-INTEGRITY-01: delivered chunked (see
    # _deliver_held_text()), not as one call -- the reconstructed text must
    # still be exactly (and only) the candidate, never anything regenerated.
    assert "".join(calls["response_tokens"]) == "Only this text should ever appear."


# ── 5. Empty/blank content is never promoted as a candidate ─────────────

def test_blank_content_is_not_a_candidate_falls_through_to_regeneration():
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "   \n\t  ", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "find it")

    assert result == "REGENERATED-FROM-CHAT-STREAM"
    assert llm.chat_stream_calls == 1


def test_pure_think_block_content_is_not_a_candidate():
    """Content that is ENTIRELY a <think> span strips down to empty --
    must not be promoted as a blank "final answer"."""
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "<think>only internal deliberation, nothing outward</think>",
         "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "find it")

    assert result == "REGENERATED-FROM-CHAT-STREAM"
    assert llm.chat_stream_calls == 1


# ── 6. A real tool call always wins over candidate semantics ────────────

def test_real_tool_call_after_candidate_creation_is_unreachable_but_registry_stays_correct():
    """Structural guard: even scripting a candidate immediately followed
    by more real tool work (which can only happen via "continue" in the
    real state machine) must still dispatch that tool normally -- a
    candidate must never suppress or alter ordinary tool dispatch."""
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "stale candidate", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(CONTINUE_TOOL_WORK_NAME)]},
        {"tool_calls": [_tc("read_file")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    LuminaAgent.chat(fake, "find and read it")

    assert calls["registry_calls"] == ["search_memory", "read_file"]


# ── 7. Cancellation clears the candidate (never leaks) ───────────────────

def test_cancel_before_finish_transition_prevents_candidate_promotion():
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "candidate about to be cancelled", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)
    event = threading.Event()

    real_is_tool_call = llm.is_tool_call

    def is_tool_call_then_maybe_cancel(message):
        result = real_is_tool_call(message)
        if llm.call_count == 3:  # the gate's response is being processed
            event.set()
        return result

    llm.is_tool_call = is_tool_call_then_maybe_cancel

    with pytest.raises(TurnCancelled):
        LuminaAgent.chat(fake, "find it", cancel_event=event)

    assert not any("candidate about to be cancelled" in str(h) for h in fake.ctx.history)
    assert all("candidate" not in tok for tok in calls["response_tokens"])


def test_a_fresh_turn_never_sees_a_prior_turns_candidate():
    """Two separate LuminaAgent.chat() calls against the SAME fake share no
    state -- completion_candidate is a local of _chat_impl(), so a fresh
    call starts at None regardless of what the previous call did."""
    llm1 = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "TURN-ONE-CANDIDATE", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm1)
    result1 = LuminaAgent.chat(fake, "first turn")
    assert result1 == "TURN-ONE-CANDIDATE"

    # Rewire the SAME fake agent onto a brand-new scripted LLM for a second,
    # independent turn -- if a candidate ever leaked as instance/module
    # state, it would surface here as TURN-ONE-CANDIDATE reappearing.
    llm2 = _ScriptedLLM([
        {"tool_calls": [_tc("read_file")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake.llm = llm2
    result2 = LuminaAgent.chat(fake, "second turn")

    assert result2 == "REGENERATED-FROM-CHAT-STREAM"
    assert "TURN-ONE-CANDIDATE" not in result2


# ── 8. Provider/gate failure never lets a candidate leak ────────────────

def test_provider_failure_during_gate_clears_candidate():
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "candidate that must not survive a provider failure",
         "termination": TerminationStatus.COMPLETE},
        {"raise": RuntimeError("provider exploded")},
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "find it")

    assert "candidate that must not survive" not in result
    assert "rejected the continuation" in result


# ── 9. Flight Recorder event ordering and provenance ─────────────────────

def test_flight_recorder_records_created_then_accepted_then_final(tmp_path):
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "recorded candidate text", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm, flight_recorder_path=tmp_path / "fr.db")

    result = LuminaAgent.chat(fake, "find it")

    assert result == "recorded candidate text"
    events = _events(fake)
    types_in_order = [e["event_type"] for e in events]

    created_i = types_in_order.index("completion_candidate.created")
    accepted_i = types_in_order.index("completion_candidate.accepted")
    final_i = types_in_order.index("turn.final")
    completed_i = types_in_order.index("turn.completed")
    tool_call_i = types_in_order.index("tool.call")

    assert tool_call_i < created_i < accepted_i < final_i < completed_i, types_in_order
    assert "completion_candidate.discarded" not in types_in_order

    created = next(e for e in events if e["event_type"] == "completion_candidate.created")
    assert created["provenance"] == "machine"
    fields = json.loads(created["fields_json"])
    assert fields["content_chars"] == len("recorded candidate text")
    assert fields["finish_reason"] == "complete"

    final = next(e for e in events if e["event_type"] == "turn.final")
    assert final["provenance"] == "model"
    assert json.loads(final["fields_json"])["text"] == "recorded candidate text"


def test_flight_recorder_records_discarded_on_continue(tmp_path):
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "will be discarded", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(CONTINUE_TOOL_WORK_NAME)]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm, flight_recorder_path=tmp_path / "fr.db")

    LuminaAgent.chat(fake, "find it")

    events = _events(fake)
    discarded = [e for e in events if e["event_type"] == "completion_candidate.discarded"]
    assert len(discarded) == 1
    assert discarded[0]["provenance"] == "machine"
    fields = json.loads(discarded[0]["fields_json"])
    assert fields["reason"] == "continue_tool_work"
    # No acceptance event -- this candidate was discarded, never promoted.
    assert not any(e["event_type"] == "completion_candidate.accepted" for e in events)


def test_no_candidate_events_when_no_candidate_ever_exists(tmp_path):
    """Every pre-existing empty-content-trigger scenario must produce zero
    completion_candidate.* events -- this is pure additive telemetry."""
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm, flight_recorder_path=tmp_path / "fr.db")

    LuminaAgent.chat(fake, "find it")

    events = _events(fake)
    assert not any(e["event_type"].startswith("completion_candidate.") for e in events)


# ── AGENT-FINAL-INTEGRITY-01 -- reconciliation when the real conclusion
# already went out as Commentary alongside a real tool call, and only a
# later, trivial zero-tool round becomes the completion_candidate. ────────

def test_reconciliation_triggers_when_prior_commentary_precedes_trivial_candidate():
    """The live-observed defect this repairs, reproduced exactly: a real
    tool-bearing round delivers the actual substantive conclusion as
    Commentary right before a housekeeping tool call (save_memory-shaped),
    and only a later, trivial no-tool round ("Committed to memory.")
    becomes the completion_candidate. The candidate must NOT be blindly
    promoted verbatim -- a reconciliation pass must run, and the turn's
    real output must come from that pass, not from the trivial candidate."""
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("read_file")],
         "content": "Here is the complete diagnostic report the user asked for: "
                    "root cause found in module X, line Y. Saving this to memory now."},
        {"tool_calls": [_tc("search_memory")]},  # stands in for save_memory(...)
        {"content": "Committed to memory. \U0001F499", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "diagnose it")

    # The durable Final is the reconciliation pass's own output, not the
    # trivial candidate text and not a bare string concatenation of the two.
    assert result == "REGENERATED-FROM-CHAT-STREAM"
    assert result != "Committed to memory. \U0001F499"
    # Exactly ONE additional provider call -- a single explicit
    # finalization pass, not an open-ended/repeated regeneration loop.
    assert llm.chat_stream_calls == 1
    # Persisted to history exactly like an ordinary streamed final would be
    # -- one new assistant entry, not the trivial candidate text re-appended.
    assert fake.ctx.history[-1] == {"role": "assistant", "content": "REGENERATED-FROM-CHAT-STREAM"}
    # The trivial candidate's own text is never separately persisted as a
    # plain assistant entry -- it only ever reached history-adjacent state
    # via the ephemeral instruction (asserted in the next test), never a
    # bare add_assistant()/add_tool_call() row of its own.
    assert not any(
        entry.get("role") == "assistant" and entry.get("content") == "Committed to memory. \U0001F499"
        and "tool_calls" not in entry
        for entry in fake.ctx.history
    )


def test_reconciliation_ephemeral_surfaces_both_prior_commentary_and_candidate_text():
    """AGENT-FINAL-INTEGRITY-01's whole point: the reconciliation pass must
    not be asked to reconstruct the answer from tool history while its own
    already-produced text (both the earlier substantive Commentary and the
    trivial candidate remark) is withheld from it. Both must appear in the
    one-turn ephemeral instruction, and that instruction must never leak
    into ctx.history (push_ephemeral()'s existing non-durable contract)."""
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("read_file")], "content": "SUBSTANTIVE-DIAGNOSTIC-REPORT-TEXT"},
        {"tool_calls": [_tc("search_memory")]},
        {"content": "TRIVIAL-SIGNOFF-TEXT", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    LuminaAgent.chat(fake, "diagnose it")

    ephemeral_pushes = calls["ephemeral"]
    reconciliation_push = ephemeral_pushes[-1]
    assert "SUBSTANTIVE-DIAGNOSTIC-REPORT-TEXT" in reconciliation_push
    assert "TRIVIAL-SIGNOFF-TEXT" in reconciliation_push
    # Never durable: ContextManager.push_ephemeral()/build_messages() owns
    # clearing it in the real class -- this fake only records the push,
    # so assert it was never separately appended to ctx.history itself.
    assert not any(
        "SUBSTANTIVE-DIAGNOSTIC-REPORT-TEXT" in str(entry.get("content", ""))
        and entry.get("role") == "assistant" and "tool_calls" not in entry
        for entry in fake.ctx.history
    )


def test_reconciliation_never_fires_on_commentary_a_second_time():
    """The ephemeral instruction text itself must never be routed through
    on_commentary()/turn.commentary -- it is Lumina's own internal
    finalization instruction, never model expression."""
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("read_file")], "content": "Real substantive finding."},
        {"tool_calls": [_tc("search_memory")]},
        {"content": "Saved.", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    LuminaAgent.chat(fake, "diagnose it")

    # Exactly one on_commentary() call -- the real substantive-finding
    # round -- never a second one for the ephemeral finalization prompt.
    assert calls["commentary"] == ["Real substantive finding."]


def test_original_no_regeneration_fast_path_is_unaffected_by_this_repair():
    """AGENT-WORK-COMPLETE-DISCARD-01's original promise -- a candidate
    with NO prior tool-bearing Commentary this turn is promoted verbatim,
    zero extra provider calls -- must survive this repair exactly. Same
    scenario as test_candidate_survives_gate_acceptance_with_no_regeneration_call
    above, restated here under AGENT-FINAL-INTEGRITY-01's own name so the
    two repairs' regression coverage sits side by side."""
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},   # no content -- no Commentary emitted
        {"content": "Here is the complete answer you asked for.",
         "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "find it")

    assert result == "Here is the complete answer you asked for."
    assert llm.chat_stream_calls == 0
    assert calls["commentary"] == []


def test_commentary_reset_on_continue_does_not_force_unnecessary_reconciliation():
    """Early, now-superseded Commentary from a narrative arc the model
    itself abandoned (a "continue_tool_work" gate outcome) must not force
    reconciliation on a later, independent, genuinely-first candidate --
    see turn_relevant_commentary's reset-on-continue docstring in
    core/agent.py. Ordinary early progress narration must not cause
    unnecessary final regeneration."""
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("read_file")], "content": "Early narration about a lead that gets abandoned."},
        {"content": "", "termination": TerminationStatus.COMPLETE},  # -> gate
        {"tool_calls": [_tc(CONTINUE_TOOL_WORK_NAME)]},              # continue -- resets the log
        {"tool_calls": [_tc("read_file")]},                          # fresh real work, no commentary
        {"content": "Here is the actual complete final answer.",
         "termination": TerminationStatus.COMPLETE},                 # genuinely first candidate now
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "find it")

    assert result == "Here is the actual complete final answer."
    assert llm.chat_stream_calls == 0


def test_flight_recorder_records_reconciled_not_accepted(tmp_path):
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("read_file")], "content": "Substantive finding before housekeeping."},
        {"tool_calls": [_tc("search_memory")]},
        {"content": "Done.", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm, flight_recorder_path=tmp_path / "fr.db")

    LuminaAgent.chat(fake, "diagnose it")

    events = _events(fake)
    assert any(e["event_type"] == "completion_candidate.reconciled" for e in events)
    assert not any(e["event_type"] == "completion_candidate.accepted" for e in events)
