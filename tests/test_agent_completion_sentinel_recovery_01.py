"""
AGENT-COMPLETION-SENTINEL-RECOVERY-01 -- Completion-Gate Contract-Violation
Candidate Recovery.

Live-reproduced defect (2026-09-03, GUI, z-ai/glm-5.3-flash, dev data dir
Flight Recorder turn_ids 4af4cbf6-96fd-402e-86f4-1d683d23107d and
4cae5158-980d-418a-99a9-8b099b031ba9): an ordinary plain-text conversational
turn produced a genuinely complete, non-truncated WORK-round answer with
zero tool calls -- this became a `completion_candidate` exactly as
AGENT-WORK-COMPLETE-DISCARD-01 (and AGENT-PRETOOL-ACTION-INTEGRITY-01, which
routes EVERY zero-tool-call round through the gate, not just in-tool-work-
phase ones) intends. The completion gate's own separate, isolated
REQUIRED two-choice request (continue_tool_work / finish_tool_work) then
failed to select either control tool -- twice, exhausting the one bounded
corrective retry _run_tool_work_control_gate() gets. core/agent.py's old
_chat_impl() fell straight through to the opaque completion sentinel in
that case, with NO regard for the completion_candidate sitting right next
to it and NO Flight Recorder trace of the branch at all (confirmed via a
live DB query: completion_candidate.created fired, then nothing but
turn.completed with response_chars matching the sentinel, not the
candidate).

Root cause: the gate's own inability to answer "is more tool work needed"
was being treated as proof the turn wasn't actually finished, when the
independent evidence that mattered -- a WORK round that itself, unprompted,
returned zero tool calls with clean (non-INCOMPLETE) termination -- already
established completion. The gate is a confirmation mechanism, not the sole
source of truth; its own failure to confirm must not erase evidence that
already exists.

Fix: _run_tool_work_control_gate()'s "malformed"/"incomplete" corrective-
retry-exhausted branch in _chat_impl() now checks `completion_candidate`
before giving up. A live candidate is promoted through the exact same
_promote_completion_candidate() helper the "finish" outcome already uses
(AGENT-FINAL-INTEGRITY-01 reconciliation still applies when real Commentary
exists this same attempt) -- reason="gate_contract_violated" distinguishes
this recovery from an ordinary reason="gate_finish" promotion in Flight
Recorder telemetry. No candidate (the blank-content case, or a turn that
never entered WORK-phase candidate creation at all) still falls through to
the unchanged, truthful sentinel -- this fix recovers a real answer that
already existed, it does not suppress or weaken the sentinel itself.

Reuses the types.SimpleNamespace fake-agent pattern established across
tests/test_agent_work_complete_discard.py (read directly before writing
this file) -- restated here rather than imported, matching this codebase's
existing convention of self-contained test files (no cross-test-file
imports exist anywhere in tests/).
"""
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
from core.context import RECONCILIATION_CONTINUATION_CUE
from core.flight_recorder import FlightRecorder


@pytest.fixture(autouse=True)
def _no_skill_injection(monkeypatch):
    monkeypatch.setattr("core.agent.build_skills_block", lambda user_input: "")


def _tc(name, call_id=None):
    return {"id": call_id or name, "type": "function",
            "function": {"name": name, "arguments": "{}"}}


class _ScriptedLLM:
    display_name = "FakeProvider"
    name = "fake-backend"
    supports_required_tool_choice = True

    def __init__(self, turns):
        self.turns = list(turns)
        self.call_count = 0
        self.chat_stream_calls = 0
        self.tools_seen = []
        self.tool_choice_modes_seen = []

    def get_model(self):
        return "fake-model"

    def configured_model(self):
        return "z-ai/glm-5.3-flash"

    def chat(self, messages, tools=None, max_tokens=None, reasoning_effort=None,
              tool_choice_mode=None):
        self.tools_seen.append([t["function"]["name"] for t in (tools or [])])
        self.tool_choice_modes_seen.append(tool_choice_mode)
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
        "ephemeral": [], "ephemeral_messages": [], "registry_calls": [], "on_tool_call": [],
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
        push_ephemeral_assistant=lambda content: calls["ephemeral_messages"].append(
            {"role": "assistant", "content": content}),
        push_ephemeral_reconciliation_request=lambda: calls["ephemeral_messages"].append(
            {"role": "user", "content": RECONCILIATION_CONTINUATION_CUE}),
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
    import sqlite3
    conn = sqlite3.connect(ns.flight_recorder.db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _malformed_gate_turn(content="I looked at this and I don't think a tool is needed here."):
    """A gate-phase response with NO control-tool call at all -- neither
    continue_tool_work nor finish_tool_work -- the "malformed" outcome
    (see _run_tool_work_control_gate(): finish_call is None and
    continue_call is None -> outcome = "malformed"). `content` defaults to
    plausible stray prose a model might emit instead of picking a control
    tool, deliberately distinct from any candidate text under test so a
    leak is unambiguous."""
    return {"content": content, "termination": TerminationStatus.COMPLETE}


# ── 1. Reproducer A: plain conversational turn, no prior tool work ──────

def test_reproducer_a_plain_turn_survives_gate_contract_violation():
    """Exact live shape of Flight Recorder turn_id 4af4cbf6... : a first
    WORK round with genuinely zero tool calls and clean termination
    produces a real, complete answer with no tool work needed at all. The
    gate then fails to comply twice. Pre-fix, this returned the opaque
    sentinel and threw the real answer away; post-fix, the real answer
    (never the gate's own stray prose) must be what the user sees."""
    real_answer = "Got it -- what's the kink you're running into?"
    llm = _ScriptedLLM([
        {"content": real_answer, "termination": TerminationStatus.COMPLETE},  # WORK round 0 -> candidate
        _malformed_gate_turn("garbage-gate-prose-must-never-be-shown"),        # gate attempt 1
        _malformed_gate_turn("garbage-gate-prose-must-never-be-shown"),        # gate attempt 2 (retry exhausted)
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "So that's a win... I have a small issue with the process...")

    assert result == real_answer
    assert "[Lumina:" not in result
    assert "garbage-gate-prose" not in result
    assert llm.chat_stream_calls == 0  # no regeneration needed -- direct promotion
    assert llm.call_count == 3  # bounded: WORK round + gate + one corrective retry, no more
    assert fake.ctx.history[-1] == {"role": "assistant", "content": real_answer}
    assert all("garbage-gate-prose" not in tok for tok in calls["response_tokens"])


def test_reproducer_a_delivers_via_on_response_token_not_silently():
    real_answer = "Short acknowledgement text long enough to span multiple delivery chunks."
    llm = _ScriptedLLM([
        {"content": real_answer, "termination": TerminationStatus.COMPLETE},
        _malformed_gate_turn(),
        _malformed_gate_turn(),
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "trailing off...")

    assert result == real_answer
    assert "".join(calls["response_tokens"]) == real_answer
    assert len(calls["response_tokens"]) > 1  # chunked like a real stream, not one blast


# ── 2. Reproducer B: real tool call + Commentary, then a trivial ────────
#      wrap-up candidate, then gate contract violation -> reconciliation.

def test_reproducer_b_shaped_commentary_reconciles_after_contract_violation():
    """Exact live shape of Flight Recorder turn_id 4cae5158... : a
    substantive owner-facing answer goes out as Commentary alongside a
    real tool call (save_memory), a LATER trivial "wrap-up" round becomes
    the candidate, and the gate then fails twice. AGENT-FINAL-INTEGRITY-01
    reconciliation must still fire here exactly as it would for a clean
    gate "finish" -- the substantive Commentary must not be silently
    dropped in favor of either the trivial candidate alone or the
    sentinel."""
    substantive_commentary = "SUBSTANTIVE DESIGN NARRATION: here is the full proposal..."
    trivial_wrapup = "Saved. Anything else?"
    canonical_final = "Canonical reconciled final: full proposal, now saved."
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")], "content": substantive_commentary},  # real tool + commentary
        {"content": trivial_wrapup, "termination": TerminationStatus.COMPLETE},     # -> trivial candidate
        _malformed_gate_turn("garbage-gate-prose"),                                  # gate attempt 1
        _malformed_gate_turn("garbage-gate-prose"),                                  # gate attempt 2
    ])

    def reconciliation_stream(messages, max_tokens=None, reasoning_effort=None):
        llm.chat_stream_calls += 1
        yield canonical_final

    llm.chat_stream = reconciliation_stream
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "design it and save it")

    assert result == canonical_final
    assert llm.chat_stream_calls == 1  # exactly one reconciliation call, not zero, not many
    assert "garbage-gate-prose" not in result
    assert calls["commentary"] == [substantive_commentary]
    reconciliation_message = calls["ephemeral_messages"][-2]
    assert reconciliation_message["role"] == "assistant"
    assert substantive_commentary in reconciliation_message["content"]
    assert trivial_wrapup in reconciliation_message["content"]


# ── 3. Malformed/incomplete gate output never auto-certifies completion ──
#      by itself -- only an INDEPENDENTLY produced candidate is ever
#      promoted, and the sentinel survives untouched when none exists.

def test_no_candidate_gate_contract_violation_still_returns_truthful_sentinel():
    """The blank-content case: a WORK round with no real content forms NO
    candidate (completion_candidate stays None -- see _chat_impl()'s
    candidate-creation site). If the gate then fails twice, there is
    nothing safe to promote -- this fix must NOT invent an answer or
    suppress the sentinel; the pre-existing truthful-failure behavior is
    unchanged."""
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},  # -> no candidate
        _malformed_gate_turn(),
        _malformed_gate_turn(),
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "find it")

    assert result == "[Lumina: tool-work continuation ended without confirming completion.]"
    assert llm.chat_stream_calls == 0


def test_incomplete_gate_termination_with_candidate_also_recovers():
    """Covers the "incomplete" outcome (positively truncated gate
    response), not just "malformed" (no/both control calls) -- both share
    the same corrective-retry-exhausted fallback branch in _chat_impl()."""
    real_answer = "Complete answer produced before the gate ever ran."
    llm = _ScriptedLLM([
        {"content": real_answer, "termination": TerminationStatus.COMPLETE},
        {"content": "cut off mid", "termination": TerminationStatus.INCOMPLETE},
        {"content": "cut off mid again", "termination": TerminationStatus.INCOMPLETE},
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "hello")

    assert result == real_answer
    assert "cut off mid" not in result


def test_malformed_gate_reasoning_never_leaks_into_final():
    """No private reasoning (Think, or the gate's own failed-attempt
    content) is ever copied into the final answer -- only the
    independently-produced candidate text."""
    real_answer = "The only text that may ever reach the user."
    llm = _ScriptedLLM([
        {"content": real_answer, "termination": TerminationStatus.COMPLETE,
         "reasoning": "PRIVATE WORK-ROUND THINK -- must never be shown."},
        {"content": "PRIVATE GATE THINK -- must never be shown.",
         "termination": TerminationStatus.COMPLETE,
         "reasoning": "PRIVATE GATE REASONING FIELD -- must never be shown."},
        {"content": "PRIVATE GATE THINK 2 -- must never be shown.",
         "termination": TerminationStatus.COMPLETE,
         "reasoning": "PRIVATE GATE REASONING FIELD 2 -- must never be shown."},
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "hi")

    assert result == real_answer
    assert "PRIVATE" not in result
    assert all("PRIVATE" not in tok for tok in calls["response_tokens"])


# ── 4. Retry/continuation budgets remain bounded ─────────────────────────

def test_gate_contract_violation_recovery_does_not_extend_retry_budget():
    """Exactly one corrective retry, exactly two gate attempts -- recovery
    must not open the door to unbounded retrying just because a candidate
    happened to exist."""
    real_answer = "Bounded recovery answer."
    llm = _ScriptedLLM([
        {"content": real_answer, "termination": TerminationStatus.COMPLETE},
        _malformed_gate_turn(),
        _malformed_gate_turn(),
        _malformed_gate_turn(),  # must NEVER be reached
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "hi")

    assert result == real_answer
    assert llm.call_count == 3  # WORK round + 2 gate attempts; third scripted turn unused


# ── 5. No early candidate leaks across a continue -> contract-violation ──

def test_continue_then_contract_violation_only_leaks_the_new_candidate():
    llm = _ScriptedLLM([
        {"content": "STALE-CANDIDATE-MUST-NOT-SURVIVE", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(CONTINUE_TOOL_WORK_NAME)]},          # discards the stale candidate
        {"tool_calls": [_tc("read_file")]},                      # real work
        {"content": "FRESH-CANDIDATE-AFTER-CONTINUE", "termination": TerminationStatus.COMPLETE},
        _malformed_gate_turn(),
        _malformed_gate_turn(),
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "iterate")

    assert result == "FRESH-CANDIDATE-AFTER-CONTINUE"
    assert "STALE-CANDIDATE" not in result
    assert not any("STALE-CANDIDATE" in str(h) for h in fake.ctx.history)


# ── 6. Cancellation still wins at this boundary ──────────────────────────

def test_cancellation_still_wins_over_contract_violation_recovery():
    llm = _ScriptedLLM([
        {"content": "candidate about to be cancelled", "termination": TerminationStatus.COMPLETE},
        _malformed_gate_turn(),
        _malformed_gate_turn(),
    ])
    fake, calls = _fake_agent(llm)
    event = threading.Event()

    real_is_tool_call = llm.is_tool_call

    def is_tool_call_then_maybe_cancel(message):
        result = real_is_tool_call(message)
        if llm.call_count == 3:  # second (retry-exhausting) gate response just processed
            event.set()
        return result

    llm.is_tool_call = is_tool_call_then_maybe_cancel

    with pytest.raises(TurnCancelled):
        LuminaAgent.chat(fake, "find it", cancel_event=event)

    assert not any("candidate about to be cancelled" in str(h) for h in fake.ctx.history)
    assert all("candidate" not in tok for tok in calls["response_tokens"])


# ── 7. Conversation history is not mutated by gate-control traffic ──────

def test_gate_traffic_never_mutates_durable_history():
    real_answer = "Answer that must be the only thing durably recorded."
    llm = _ScriptedLLM([
        {"content": real_answer, "termination": TerminationStatus.COMPLETE},
        _malformed_gate_turn("gate prose 1"),
        _malformed_gate_turn("gate prose 2"),
    ])
    fake, calls = _fake_agent(llm)

    LuminaAgent.chat(fake, "hi")

    assert fake.ctx.history == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": real_answer},
    ]
    assert not any("gate prose" in block for block in calls["ephemeral"])


# ── 8. Flight Recorder observability for this branch (previously none) ──

def test_flight_recorder_records_gate_unresolved_and_recovery_reason(tmp_path):
    real_answer = "Recovered answer for telemetry test."
    llm = _ScriptedLLM([
        {"content": real_answer, "termination": TerminationStatus.COMPLETE},
        _malformed_gate_turn(),
        _malformed_gate_turn(),
    ])
    fake, calls = _fake_agent(llm, flight_recorder_path=tmp_path / "fr.db")

    result = LuminaAgent.chat(fake, "hi")
    assert result == real_answer

    events = _events(fake)
    unresolved = [e for e in events if e["event_type"] == "turn.completion_gate_unresolved"]
    assert len(unresolved) == 1
    import json
    fields = json.loads(unresolved[0]["fields_json"])
    assert fields["candidate_recovered"] is True
    assert fields["outcome"] == "malformed"
    assert unresolved[0]["severity"] == "warning"

    accepted = [e for e in events if e["event_type"] == "completion_candidate.accepted"]
    assert len(accepted) == 1
    assert json.loads(accepted[0]["fields_json"])["reason"] == "gate_contract_violated"


def test_flight_recorder_records_gate_unresolved_without_recovery_when_no_candidate(tmp_path):
    """The previously-uninstrumented no-candidate sentinel path now emits
    the same observability event, with candidate_recovered=False -- prior
    to this fix, this branch produced ZERO Flight Recorder trace at all
    (the live incident's own root-cause investigation had to fall back to
    inference from silence for exactly this reason)."""
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        _malformed_gate_turn(),
        _malformed_gate_turn(),
    ])
    fake, calls = _fake_agent(llm, flight_recorder_path=tmp_path / "fr.db")

    result = LuminaAgent.chat(fake, "find it")
    assert result == "[Lumina: tool-work continuation ended without confirming completion.]"

    events = _events(fake)
    unresolved = [e for e in events if e["event_type"] == "turn.completion_gate_unresolved"]
    assert len(unresolved) == 1
    import json
    fields = json.loads(unresolved[0]["fields_json"])
    assert fields["candidate_recovered"] is False
    assert fields["source_round"] is None
    assert not any(e["event_type"] == "completion_candidate.accepted" for e in events)


def test_gate_finish_promotion_still_tagged_with_gate_finish_reason(tmp_path):
    """The pre-existing "finish" outcome path (unchanged behavior) now
    flows through the same shared _promote_completion_candidate() helper
    -- confirm it is still tagged distinctly (reason="gate_finish") so
    telemetry can tell an ordinary clean finish apart from this ticket's
    contract-violation recovery."""
    llm = _ScriptedLLM([
        {"content": "Clean finish candidate.", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm, flight_recorder_path=tmp_path / "fr.db")

    result = LuminaAgent.chat(fake, "hi")
    assert result == "Clean finish candidate."

    events = _events(fake)
    accepted = [e for e in events if e["event_type"] == "completion_candidate.accepted"]
    assert len(accepted) == 1
    import json
    assert json.loads(accepted[0]["fields_json"])["reason"] == "gate_finish"
    assert not any(e["event_type"] == "turn.completion_gate_unresolved" for e in events)
