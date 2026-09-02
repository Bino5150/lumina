"""
tests/test_toks_stream_timing_01.py -- TOKS-STREAM-TIMING-01

Truthful final-stream throughput telemetry.

Confirmed defect: the UI's displayed tok/s used to divide output tokens
by whole-turn elapsed time (ui/chat_widget.py's old MetricsBar.set_metrics()
formula), where "whole-turn elapsed" could include Think generation,
provider round trips, tool execution, WORK continuation, and reconciliation
-- none of which is final-answer generation speed. A second, opposite
defect lived in the held-candidate path: _deliver_held_text() replays an
already-generated completion candidate through on_response_token with no
delay, and the old UI timer reset its clock at the first replayed chunk --
so a candidate's real (possibly multi-second) generation time could be
reported as an implausibly fast "generated in a few milliseconds" rate.

The fix (see core/agent.py's on_final_stream_timing docstring on
LuminaAgent.__init__): core/agent.py now measures the provider's OWN
generation interval for whichever text actually becomes this turn's
visible Final answer --
  - _stream_final(): from the first real (non-Think) delta to the
    stream's terminal chunk, using time.monotonic().
  - _finalize_completion_candidate(): the ORIGINAL WORK-round request/
    response timing captured when the candidate was created, never the
    _deliver_held_text() replay loop.
  - _finalize_with_reconciliation(): inherits _stream_final()'s own fresh
    timing automatically (it re-enters that same method).
and fires on_final_stream_timing(duration_s, token_count) at most once
per turn, only when that measurement is genuinely trustworthy (positive
duration, non-empty content) -- estimate_tokens() (core/context.py's
existing char/4 estimator, the same one context-window accounting uses)
supplies token_count.

All timing is driven by an injected fake time.monotonic() (see
_FakeClock/fake_clock below) -- no sleep-based timing anywhere in this
file. _FakeClock raises loudly if the code under test calls monotonic()
more times than a test scripted, which is itself a correctness check:
e.g. test_think_does_not_touch_the_clock's 2-tick script would fail with
"called more times than scripted" if Think content were ever allowed to
advance the final-stream timer.

Reuses the types.SimpleNamespace fake-agent / _ScriptedLLM convention
established in tests/test_agent_work_complete_discard.py (read directly
before writing this file): LuminaAgent.chat()/._stream_final()/
._finalize_completion_candidate()/._finalize_with_reconciliation() called
against a minimal stand-in, real methods bound on via types.MethodType so
the actual control flow under test genuinely runs.
"""
import types

import pytest

from core.agent import (
    LuminaAgent,
    TurnCancelled,
    FINISH_TOOL_WORK_NAME,
    CONTINUE_TOOL_WORK_NAME,
)
import core.agent as agent_module
from core.backends.base import TerminationStatus
from core.context import RECONCILIATION_CONTINUATION_CUE, estimate_tokens


# ── Deterministic fake clock ─────────────────────────────────────────────

class _FakeClock:
    def __init__(self, ticks):
        self._ticks = list(ticks)
        self._i = 0

    def __call__(self):
        if self._i >= len(self._ticks):
            raise AssertionError(
                f"fake time.monotonic() called more times than scripted "
                f"({len(self._ticks)} ticks provided; this would be call "
                f"#{self._i + 1})"
            )
        v = self._ticks[self._i]
        self._i += 1
        return v

    @property
    def calls_made(self):
        return self._i


@pytest.fixture
def fake_clock(monkeypatch):
    def _install(ticks):
        clock = _FakeClock(ticks)
        monkeypatch.setattr(agent_module.time, "monotonic", clock)
        return clock
    return _install


@pytest.fixture(autouse=True)
def _no_skill_injection(monkeypatch):
    monkeypatch.setattr("core.agent.build_skills_block", lambda user_input: "")


# ── Shared fake-agent scaffolding (mirrors test_agent_work_complete_discard.py) ──

def _tc(name, call_id=None):
    return {"id": call_id or name, "type": "function",
            "function": {"name": name, "arguments": "{}"}}


class _ScriptedLLM:
    """Non-streaming `.chat()` turns (WORK rounds / the two-choice gate),
    scripted in order, plus a separate `.chat_stream()` script for
    whatever _stream_final() call(s) the turn under test makes."""
    display_name = "FakeProvider"
    name = "fake-backend"
    supports_required_tool_choice = True

    def __init__(self, turns, stream_scripts=None):
        self.turns = list(turns)
        self.call_count = 0
        # A list of chunk-lists -- each _stream_final() call (there can be
        # more than one across a turn: an ordinary final, or a
        # reconciliation final) consumes the next one in order.
        self.stream_scripts = list(stream_scripts or [])
        self.stream_call_count = 0

    def get_model(self):
        return "fake-model"

    def configured_model(self):
        return "fake-model-configured"

    def chat(self, messages, tools=None, max_tokens=None, reasoning_effort=None,
              tool_choice_mode=None):
        idx = self.call_count
        self.call_count += 1
        turn = self.turns[idx]
        if "raise" in turn:
            raise turn["raise"]
        return {"_turn": idx}

    def chat_stream(self, messages, max_tokens=None, reasoning_effort=None):
        idx = self.stream_call_count
        self.stream_call_count += 1
        for chunk in self.stream_scripts[idx]:
            yield chunk

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


def _fake_agent(llm, tool_result="ok"):
    history = []
    calls = {
        "ephemeral": [], "ephemeral_messages": [], "registry_calls": [],
        "response_tokens": [], "commentary": [], "final_stream_timing": [],
        "machine_events": [], "model_events": [],
        "final_ttft": [], "time_to_first_answer": [],
    }

    def registry_call(name, args):
        calls["registry_calls"].append(name)
        return tool_result

    def build_messages(tool_budget=0, chat_id=None):
        return [{"role": "system", "content": "system-prompt"}]

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
    )
    registry = types.SimpleNamespace(
        schema_token_estimate=lambda: 0,
        get_schemas=lambda: [
            {"type": "function", "function": {"name": "search_memory", "description": "", "parameters": {}}},
        ],
        call=registry_call,
    )
    flight_recorder = types.SimpleNamespace(
        record_machine_event=lambda event_type, **kw: calls["machine_events"].append((event_type, kw)),
        record_model_expression=lambda event_type, **kw: calls["model_events"].append((event_type, kw)),
    )
    ns = types.SimpleNamespace(
        llm=llm,
        ctx=ctx,
        registry=registry,
        channel_id="test-channel",
        owner=True,
        flight_recorder=flight_recorder,
        on_tool_call=lambda name, args: None,
        on_tool_result=lambda name, result: None,
        on_think_start=lambda step: None,
        on_think_token=lambda tok: None,
        on_think_end=lambda: None,
        on_response_token=lambda tok: calls["response_tokens"].append(tok),
        on_commentary=lambda text: calls["commentary"].append(text),
        on_final_stream_timing=lambda d, t: calls["final_stream_timing"].append((d, t)),
        on_final_ttft=lambda t: calls["final_ttft"].append(t),
        on_time_to_first_answer=lambda t: calls["time_to_first_answer"].append(t),
        tts=None,
        _session_tool_calls=0,
        _skill_nudge_sent=False,
    )
    ns._stream_final = types.MethodType(LuminaAgent._stream_final, ns)
    ns._finalize_completion_candidate = types.MethodType(LuminaAgent._finalize_completion_candidate, ns)
    ns._finalize_with_reconciliation = types.MethodType(LuminaAgent._finalize_with_reconciliation, ns)
    return ns, calls


def _turn_completed_duration(calls):
    for event_type, kw in calls["machine_events"]:
        if event_type == "turn.completed":
            return kw["fields"]["duration_s"]
    raise AssertionError(f"no turn.completed event recorded: {calls['machine_events']}")


# ── 1/3. Direct streamed Final: correct first-delta/start and terminal/end boundaries ──

def _run_direct_stream(fake_clock, chunks, ticks):
    fake, calls = _fake_agent(_ScriptedLLM(turns=[]))
    fake_clock(ticks)

    class _FakeLLM:
        def chat_stream(self, messages, max_tokens=None, reasoning_effort=None):
            for c in chunks:
                yield c
    fake.llm = _FakeLLM()
    result = LuminaAgent._stream_final(fake, messages=[], think_step=[0])
    return fake, calls, result


def test_direct_streamed_final_uses_first_delta_to_terminal_chunk_boundary(fake_clock):
    # 3 monotonic() calls for a no-Think stream: this request's own
    # dispatch (Final TTFT's anchor), the first real delta (doubles as
    # the Final TTFT read AND final_stream_started_at -- see
    # _stream_final()'s chunk_received_at reuse), and the terminal chunk.
    fake, calls, result = _run_direct_stream(fake_clock, ["hello ", "world"], [99.0, 100.0, 102.5])
    assert result == "hello world"
    assert calls["final_stream_timing"] == [(pytest.approx(2.5), estimate_tokens("hello world"))]


# ── 2. Think interval: Think must never start or extend the final-stream timer ──

def test_think_does_not_touch_the_final_stream_clock(fake_clock):
    """Exactly 4 monotonic() calls are scripted: this request's own
    dispatch, Final TTFT's own read (fired at the first NONEMPTY output
    delta of this request -- here, the first real Think text, NOT the
    bare __THINK_START__ sentinel itself, which is a protocol marker and
    never starts this clock -- see _stream_final()'s
    _mark_final_ttft_if_first() docstring), the first real Final delta
    (a SEPARATE read here, since the Final TTFT read was for an earlier,
    DIFFERENT chunk and can't be reused), and the terminal chunk. If
    Think content ever ALSO advanced final_stream_started_at's own
    clock, _stream_final() would call monotonic() a 5th time and
    _FakeClock would raise -- proving Think is structurally excluded
    from the FINAL-STREAM duration specifically, even though it
    legitimately consumes its own, separate Final TTFT read."""
    fake, calls, result = _run_direct_stream(
        fake_clock,
        ["__THINK_START__", "long reasoning ", "even longer reasoning", "__THINK_END__",
         "final ", "answer"],
        [499.0, 499.5, 500.0, 500.4],
    )
    assert result == "final answer"
    duration_s, token_count = calls["final_stream_timing"][0]
    assert duration_s == pytest.approx(0.4)
    assert token_count == estimate_tokens("final answer")


# ── 3/4. Tool execution between provider rounds: affects turn duration, not final tok/s ──

def test_tool_round_trip_affects_turn_duration_not_final_stream(fake_clock):
    # WORK1: real tool call (next_action stays "work" -- a real tool call
    # never transitions straight to the gate). WORK2: zero-tool call with
    # EMPTY content -- termination COMPLETE/UNKNOWN with no content is
    # never a candidate (see _chat_impl()'s own "empty/blank remainder is
    # not a candidate" comment), but it DOES transition next_action to
    # "gate". GATE: finish_tool_work with no live candidate falls straight
    # through to an ordinary _stream_final() call -- the plain "tool work
    # then finish" shape this scenario is about, deliberately distinct
    # from the held-candidate scenario below.
    llm = _ScriptedLLM(
        turns=[
            {"tool_calls": [_tc("search_memory")]},
            {"content": "", "termination": TerminationStatus.COMPLETE},
            {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
        ],
        stream_scripts=[["final ", "answer"]],
    )
    fake, calls = _fake_agent(llm)

    # WORK rounds and the gate's own call are NEVER timed by this fix
    # (see core/agent.py: only _stream_final()'s own reads are) -- so the
    # only monotonic() calls the whole turn makes are:
    # 1: chat() turn_started_at
    # 2: _stream_final()'s own request-dispatch anchor (Final TTFT)
    # 3: the first real delta -- doubles as the Final TTFT read and
    #    final_stream_started_at (no Think precedes it here)
    # 4: the terminal chunk -- 0.3s stream duration
    # 5: chat()'s own terminal duration_s computation
    # The big VALUE jump from tick 1 to tick 2 represents everything in
    # between (WORK1's tool call, WORK2's empty-content round, the gate
    # round trip -- ~10 real seconds) -- real elapsed time that counts
    # toward turn duration but is never observed by, or attributed to,
    # Final TTFT or final-stream timing.
    fake_clock([0.0, 9.9, 10.0, 10.3, 10.31])

    result = LuminaAgent.chat(fake, "find it")

    assert result == "final answer"
    assert calls["final_stream_timing"] == [(pytest.approx(0.3), estimate_tokens("final answer"))]
    # Regression (Bino final review, item 1): the ~10 real seconds spent
    # in WORK1/WORK2/the gate round trip -- earlier BLOCKING, non-
    # streaming rounds -- must never be mistaken for Final TTFT. Final
    # TTFT reflects only this request's own dispatch-to-first-delta gap
    # (0.1s: tick 9.9 -> 10.0), nowhere near the ~10s that preceded it.
    assert calls["final_ttft"] == [pytest.approx(0.1)]
    # The turn as a whole took ~10.31s (includes the simulated tool/gate
    # gap) -- nowhere close to the 0.3s final-stream figure above, nor
    # the 0.1s Final TTFT figure.
    assert _turn_completed_duration(calls) == pytest.approx(10.31)


# ── 5/6. Held candidate promoted after tools: NEVER reports whole-call latency as stream ──
#
# SOL FINAL-REVIEW CORRECTION (post-live-shakedown): the first version of
# this fix bracketed the WORK round's ENTIRE self.llm.chat() request/
# response round trip and stored it as the candidate's "stream" duration.
# That is real, measured latency -- but it is request/response latency,
# not final-stream wall time. A single blocking, non-streaming call has
# no visible internal boundary between prefill, TTFT, Think generation,
# and the actual Final-content generation -- exactly the shape described
# in review: "5 seconds before first Final content; 1 second of Final
# streaming" can arrive as one opaque 6-second call. Labeling the whole
# 6s "stream" would have been a differently-shaped version of the same
# lie this ticket exists to rule out. This codebase's WORK rounds are
# never dispatched via chat_stream() (only _stream_final() is) -- there
# is no way to observe that internal boundary -- so the only truthful
# repair is core/agent.py's Option 2: _finalize_completion_candidate()
# never calls on_final_stream_timing at all. The UI's already-guarded
# "no signal this turn" default renders that honestly as "stream n/a",
# never a fabricated number and never the whole-call figure.

def test_held_candidate_never_reports_whole_call_latency_as_stream(fake_clock):
    """Models the exact reviewed scenario: the candidate-producing WORK
    round's own self.llm.chat() call internally takes 6 seconds total (5s
    of unobserved prefill/Think + 1s of unobserved actual Final
    generation, collapsed into one blocking call) -- consumed directly
    from the shared fake clock BY THE FAKE LLM ITSELF, never through any
    bracket core/agent.py owns anymore. The fake_clock's tick budget is
    scripted to hold EXACTLY what the corrected call sites need (turn
    start + the fake LLM's own 2 internal reads + chat()'s terminal
    duration) -- if a regression reintroduced a monotonic() bracket
    around the WORK-round dispatch (reviving the bug), this test would
    fail via _FakeClock's own "called more times than scripted" guard,
    not just a wrong-value assertion. Immediately after promotion,
    _deliver_held_text()'s replay loop runs (no clock calls of its own --
    already proven by this same tight tick budget)."""
    class _SlowCandidateLLM(_ScriptedLLM):
        def chat(self, *a, **kw):
            if self.call_count == 1:  # about to serve the candidate-producing round
                agent_module.time.monotonic()  # 5s of prefill/Think, unobserved by the harness
                agent_module.time.monotonic()  # +1s of actual Final generation, same call
            return super().chat(*a, **kw)

    llm = _SlowCandidateLLM(turns=[
        {"tool_calls": [_tc("search_memory")]},
        {"content": "Here is the complete answer you asked for, held as a candidate.",
         "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)
    fake_clock([
        0.0,     # 1: chat()'s turn_started_at
        5.0,     # 2: fake LLM's own internal read -- 5s of prefill/Think elapses here
        6.0,     # 3: fake LLM's own internal read -- +1s of Final generation, same 6s call
        6.01,    # 4: _finalize_completion_candidate()'s TTFA read, at
                 #    _deliver_held_text()'s first actually-delivered chunk
                 #    (negligible gate-call overhead after the 6s WORK round)
        6.02,    # 5: chat()'s terminal duration_s
    ])

    result = LuminaAgent.chat(fake, "find it")

    assert result == "Here is the complete answer you asked for, held as a candidate."
    # The whole 6-second call genuinely happened (turn wall time reflects
    # it below) but NEVER as a "stream" figure of any kind -- not the
    # true 6s, not a fraction of it, and never something derived from the
    # instant local replay that immediately follows promotion.
    assert calls["final_stream_timing"] == []
    # Regression (Bino final review, item 3): a promoted held candidate
    # reports Final TTFT AND stream throughput as unavailable ("Final
    # TTFT n/a · stream n/a" at the UI layer -- see
    # tests/test_toks_stream_timing_01_ui.py) because its WORK round is
    # blocking/non-streaming -- there is no streamed request for Final
    # TTFT (which only ever fires from _stream_final()) to observe.
    assert calls["final_ttft"] == []
    # TTFA and turn time REMAIN available, per the same review item --
    # Bino's contract explicitly includes WORK-round time in "time to
    # first answer" (this really is how long the user waited before
    # seeing any real text). Different, correct answers to different
    # questions than Final TTFT/stream -- never confused with them (see
    # the independent-clocks section below).
    assert calls["time_to_first_answer"] == [pytest.approx(6.01)]
    assert _turn_completed_duration(calls) == pytest.approx(6.02)


def test_finalize_completion_candidate_never_fires_timing_regardless_of_candidate_shape():
    """Direct unit coverage of _finalize_completion_candidate() itself,
    independent of chat()'s call-count bookkeeping above: neither an
    ordinary candidate (matching current creation code -- content/
    finish_reason/source_round only) nor a hypothetical stale/legacy
    candidate dict that happens to carry old provider_duration_s/
    token_count keys (e.g. surviving a future regression that
    reintroduces them) ever reaches on_final_stream_timing -- this method
    reads neither key at all."""
    fake, calls = _fake_agent(_ScriptedLLM(turns=[]))
    ordinary = {"content": "held answer", "finish_reason": "complete", "source_round": 0}
    # No turn_started_at passed (defaults to None) -- TTFA correctly
    # never fires either, same "nothing to anchor it to" degrade as
    # every other caller that predates this threading.
    result = LuminaAgent._finalize_completion_candidate(fake, ordinary)
    assert result == "held answer"
    assert calls["final_stream_timing"] == []
    assert calls["final_ttft"] == []
    assert calls["time_to_first_answer"] == []

    fake2, calls2 = _fake_agent(_ScriptedLLM(turns=[]))
    legacy_shaped = {"content": "held answer 2", "finish_reason": "complete", "source_round": 0,
                      "provider_duration_s": 3.0, "token_count": 99}
    result2 = LuminaAgent._finalize_completion_candidate(fake2, legacy_shaped)
    assert result2 == "held answer 2"
    assert calls2["final_stream_timing"] == []
    assert calls2["final_ttft"] == []
    assert calls2["time_to_first_answer"] == []


# ── 7. Held candidate discarded: no leaked final telemetry ──

def test_discarded_candidate_never_leaks_its_timing_into_the_real_final(fake_clock):
    # WORK1: tool call. WORK2: zero-tool, non-empty content -> a candidate
    # is created (its underlying provider latency is never observed by
    # the harness at all -- see the section above). GATE1:
    # continue_tool_work -> the gate itself judges the candidate stale;
    # it is discarded, next_action returns to "work". WORK3: another tool
    # call. WORK4: zero-tool, EMPTY content -> no new candidate, reaches
    # the gate. GATE2: finish_tool_work with no live candidate ->
    # ordinary _stream_final(). None of WORK1-4 or either gate call
    # consumes a monotonic() read (see core/agent.py: only
    # _stream_final()'s own reads do), so the only ticks the whole turn
    # needs are turn_started_at, _stream_final()'s own 3 reads (request
    # dispatch, first-delta/Final TTFT, terminal), and chat()'s terminal
    # duration_s.
    llm = _ScriptedLLM(
        turns=[
            {"tool_calls": [_tc("search_memory")]},
            {"content": "premature trivial candidate",
             "termination": TerminationStatus.COMPLETE},
            {"tool_calls": [_tc(CONTINUE_TOOL_WORK_NAME)]},
            {"tool_calls": [_tc("search_memory")]},
            {"content": "", "termination": TerminationStatus.COMPLETE},
            {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
        ],
        stream_scripts=[["real ", "final ", "answer"]],
    )
    fake, calls = _fake_agent(llm)
    fake_clock([
        0.0,             # 1: turn_started_at
        49.9, 50.0, 50.2, # 2,3,4: _stream_final() -- request dispatch,
                          #        first-delta/Final TTFT, terminal -- 0.2s stream
        50.21,             # 5: chat()'s terminal duration_s
    ])

    result = LuminaAgent.chat(fake, "find it")

    assert result == "real final answer"
    # Exactly one timing event for the whole turn, and it reflects the
    # REAL final stream (0.2s) -- the earlier discarded candidate never
    # had any stored timing to leak in the first place.
    assert calls["final_stream_timing"] == [
        (pytest.approx(0.2), estimate_tokens("real final answer"))
    ]
    # Final TTFT reflects only THIS request's own dispatch-to-first-delta
    # gap (0.1s: 49.9 -> 50.0) -- the discarded candidate's WORK round
    # (a separate, non-streaming, unobserved request) never contributes.
    assert calls["final_ttft"] == [pytest.approx(0.1)]
    # TTFA reflects turn dispatch -> the real final's first delta (50.0s
    # in) -- includes all the discarded-candidate detour, correctly, per
    # Bino's TTFA contract -- never the discarded candidate's own timing.
    assert calls["time_to_first_answer"] == [pytest.approx(50.0)]


# ── 8. Reconciliation-generated Final: uses the reconciliation stream's own timing ──
#
# Also regression (Bino final review, item 2): a LATER _stream_final()
# call (reconciliation's own re-entry, after an earlier candidate-
# producing WORK round already ran) must report Final TTFT scoped only
# to ITS OWN request -- never anything carried over from, or averaged
# with, the earlier WORK round.

def test_reconciliation_final_ttft_and_stream_are_scoped_only_to_its_own_request(fake_clock):
    llm = _ScriptedLLM(
        turns=[
            # Real tool call carrying substantive Commentary.
            {"content": "Diagnostic write-up: the root cause is X.",
             "tool_calls": [_tc("search_memory")]},
            # Later trivial zero-tool candidate -- would be a lossy
            # promotion if not reconciled against the Commentary above.
            {"content": "Committed to memory.", "termination": TerminationStatus.COMPLETE},
            {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
        ],
        # _finalize_with_reconciliation() re-enters _stream_final() -- this
        # is THAT call's script, distinguishable in content from the
        # candidate's own (never-timed) WORK-round request above.
        stream_scripts=[["reconciled ", "final ", "answer"]],
    )
    fake, calls = _fake_agent(llm)
    fake_clock([
        0.0,                 # 1: turn_started_at
        29.9, 30.0, 30.6,     # 2,3,4: reconciliation's OWN _stream_final()
                              #        call -- request dispatch, first-
                              #        delta/Final TTFT, terminal -- 0.6s
                              #        stream, 0.1s Final TTFT -- neither
                              #        borrows anything from the earlier,
                              #        untimed WORK round above.
        30.61,                 # 5: chat()'s terminal duration_s
    ])

    result = LuminaAgent.chat(fake, "diagnose it")

    assert result == "reconciled final answer"
    assert calls["final_stream_timing"] == [
        (pytest.approx(0.6), estimate_tokens("reconciled final answer"))
    ]
    # Final TTFT reflects ONLY the reconciliation request's own dispatch
    # (29.9) -> first delta (30.0) gap -- 0.1s. The candidate-producing
    # WORK round earlier in this same turn is a separate, non-streaming
    # request this reads nothing from.
    assert calls["final_ttft"] == [pytest.approx(0.1)]
    # TTFA reflects turn dispatch -> reconciliation's own first delta
    # (30.0s in) -- the correct, final answer to "when did the user see
    # real text", including the earlier candidate/commentary detour.
    assert calls["time_to_first_answer"] == [pytest.approx(30.0)]


# ── 9. Empty/tool-only final: throughput unavailable ──

def test_empty_final_reports_no_timing(fake_clock):
    # 2 ticks: request dispatch (Final TTFT's anchor) + the Think chunk's
    # own Final TTFT read (it counts a Think-first response too, even
    # when no Final content ever follows) -- final_stream_started_at
    # never gets set at all (no Final delta ever arrives), so no 3rd
    # call happens.
    fake, calls, result = _run_direct_stream(
        fake_clock,
        ["__THINK_START__", "only reasoning, no real answer", "__THINK_END__"],
        [10.0, 10.5],
    )
    assert result == ""
    assert calls["final_stream_timing"] == []
    assert calls["final_ttft"] == [pytest.approx(0.5)]


def test_whitespace_only_final_reports_no_timing(fake_clock):
    # A Final delta DOES arrive (so final_stream_started_at gets set,
    # reusing the same read Final TTFT already made on this same first
    # chunk), but the assembled content strips to "".
    fake, calls, result = _run_direct_stream(fake_clock, ["   ", "\n"], [0.9, 1.0, 1.2])
    assert result == ""
    assert calls["final_stream_timing"] == []


# ── 10. Zero or invalid duration: no division or bogus infinity ──

def test_zero_duration_never_fires_timing(fake_clock):
    # Same instant for both final-stream boundary reads (request dispatch
    # is a distinct, earlier read).
    fake, calls, result = _run_direct_stream(fake_clock, ["fast ", "answer"], [41.9, 42.0, 42.0])
    assert result == "fast answer"
    assert calls["final_stream_timing"] == []


def test_negative_duration_never_fires_timing(fake_clock):
    # Pathological clock (should never happen with a real monotonic
    # clock, but the guard must not trust that) -- terminal read before
    # the start read.
    fake, calls, result = _run_direct_stream(fake_clock, ["odd ", "answer"], [41.9, 42.0, 41.0])
    assert result == "odd answer"
    assert calls["final_stream_timing"] == []


# NOTE: held-candidate timing coverage lives in the section above
# (test_held_candidate_never_reports_whole_call_latency_as_stream /
# test_finalize_completion_candidate_never_fires_timing_regardless_of_
# candidate_shape) following the Sol final-review correction -- candidate
# dicts no longer carry provider_duration_s/token_count at all.


# ── 11. Cancellation with partial visible output: no fabricated telemetry ──

def test_cancellation_mid_stream_never_fires_final_stream_timing(fake_clock):
    fake, calls = _fake_agent(_ScriptedLLM(turns=[]))
    cancel_event = types.SimpleNamespace(is_set=lambda: False)

    class _CancelAfterOneChunk:
        def chat_stream(self, messages, max_tokens=None, reasoning_effort=None):
            yield "partial "
            cancel_event.is_set = lambda: True
            yield "never delivered"
    fake.llm = _CancelAfterOneChunk()
    # 2 ticks: request dispatch, then the first (and only) chunk actually
    # processed -- doubles as the Final TTFT read and
    # final_stream_started_at. Cancellation is detected on the SECOND
    # next(stream) call, before that chunk is ever processed for content
    # -- no terminal-boundary read happens (_raise_cancelled() never
    # touches the clock).
    fake_clock([0.9, 1.0])

    with pytest.raises(TurnCancelled) as exc_info:
        LuminaAgent._stream_final(fake, messages=[], think_step=[0], cancel_event=cancel_event)

    # Existing cancellation-integrity contract is untouched by this fix:
    # partial text is still preserved on the exception.
    assert exc_info.value.partial_response == "partial"
    assert calls["response_tokens"] == ["partial "]
    # No timing for a stream that never reached its terminal boundary.
    assert calls["final_stream_timing"] == []


# ── 12. Provider/adaptor compatibility: no Think sentinels at all still works ──

def test_backend_with_no_think_sentinels_still_times_correctly(fake_clock):
    """Some backends (e.g. Gemini) never emit __THINK_START__/__THINK_END__
    at all -- the mechanism must not implicitly depend on seeing one."""
    fake, calls, result = _run_direct_stream(fake_clock, ["plain ", "content ", "only"], [8.9, 9.0, 9.5])
    assert result == "plain content only"
    assert calls["final_stream_timing"] == [
        (pytest.approx(0.5), estimate_tokens("plain content only"))
    ]


def test_stream_error_never_fires_final_stream_timing(fake_clock):
    fake, calls = _fake_agent(_ScriptedLLM(turns=[]))

    class _ErroringLLM:
        def chat_stream(self, messages, max_tokens=None, reasoning_effort=None):
            yield "partial before error "
            raise ConnectionError("provider dropped the connection")
    fake.llm = _ErroringLLM()
    # 2 ticks: request dispatch, then the one real chunk received before
    # the connection drops -- doubles as the Final TTFT read and
    # final_stream_started_at. The exception handler itself never touches
    # the clock.
    fake_clock([0.9, 1.0])

    result = LuminaAgent._stream_final(fake, messages=[], think_step=[0])

    assert result.startswith("[Stream error:")
    assert calls["final_stream_timing"] == []


# ── 13. Bino-approved expansion: Final TTFT + time-to-first-answer ─────────
#
# Four independent clocks, four independent questions, never allowed to
# overwrite one another:
#   - turn wall time       -- turn dispatch -> terminal completion (ALL of it)
#   - time-to-first-answer -- turn dispatch -> first real answer text the
#                              user actually saw (includes Think/tool/gate/
#                              WORK/reconciliation time before it)
#   - Final TTFT            -- the final-PRODUCING request's OWN dispatch
#                              -> its first NONEMPTY output delta of THAT
#                              REQUEST (Think or Final text either counts;
#                              a bare sentinel does not) -- never an
#                              earlier WORK/tool round's dispatch
#   - final-stream duration -- that same request's first FINAL delta ->
#                              terminal chunk (excludes Think entirely)

def test_independent_clocks_report_distinct_values_for_the_same_turn(fake_clock):
    """One turn, deliberately shaped so all four values differ from each
    other by a wide, unmistakable margin -- if any implementation
    accidentally aliased one clock's read to another (e.g. TTFA reusing
    final_stream_started_at's absolute reading instead of computing its
    own delta against turn_started_at, or Final TTFT leaking into the
    stream-duration calculation), at least one of these four assertions
    would fail."""
    llm = _ScriptedLLM(
        turns=[
            {"tool_calls": [_tc("search_memory")]},                       # WORK1: tool call
            {"content": "", "termination": TerminationStatus.COMPLETE},   # WORK2: empty -> gate, no candidate
            {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},                 # GATE: finish -> _stream_final()
        ],
        stream_scripts=[[
            "__THINK_START__", "reasoning about it", "__THINK_END__",
            "the ", "real ", "answer",
        ]],
    )
    fake, calls = _fake_agent(llm)
    fake_clock([
        0.0,   # 1: turn_started_at
        5.0,   # 2: _stream_final()'s own request_dispatch_at -- everything
               #    before this (WORK1's tool call, WORK2's empty round,
               #    the gate round trip) took 5s, untimed by the harness
        5.2,   # 3: first NONEMPTY output delta of this request --
               #    "reasoning about it" (the real Think text, NOT the
               #    __THINK_START__ sentinel itself) -- Final TTFT's own read
        8.0,   # 4: first REAL Final delta ("the ") -- Think itself took
               #    2.8s (5.2->8.0), never counted in final-stream
               #    duration below, but fully counted in TTFA
        8.5,   # 5: terminal chunk -- final-stream duration
        8.51,  # 6: chat()'s terminal duration_s -- whole turn wall time
    ])

    result = LuminaAgent.chat(fake, "find it")

    assert result == "the real answer"

    turn_wall_s = _turn_completed_duration(calls)
    ttfa_s = calls["time_to_first_answer"][0]
    final_ttft_s = calls["final_ttft"][0]
    stream_duration_s, stream_tokens = calls["final_stream_timing"][0]

    assert turn_wall_s == pytest.approx(8.51)
    assert ttfa_s == pytest.approx(8.0)
    assert final_ttft_s == pytest.approx(0.2)
    assert stream_duration_s == pytest.approx(0.5)
    assert stream_tokens == estimate_tokens("the real answer")

    # All four genuinely distinct -- no accidental aliasing between clocks.
    values = [turn_wall_s, ttfa_s, final_ttft_s, stream_duration_s]
    assert len(set(round(v, 3) for v in values)) == 4
    # And the ordering the scenario implies actually holds: Final TTFT <
    # stream_duration < ttfa < turn_wall (Think sits entirely between
    # Final TTFT and ttfa, tool/gate round-trip sits entirely before
    # Final TTFT, and the tiny post-stream wrapper overhead sits after ttfa).
    assert final_ttft_s < stream_duration_s < ttfa_s < turn_wall_s


def test_held_candidate_ttfa_uses_delivery_time_not_generation_time(fake_clock):
    """The exact distinction Bino's contract calls out: 'for promoted held
    text, end when the first held Final chunk is actually delivered -- not
    when the candidate was originally generated.' Both the candidate-
    producing WORK round AND the gate's own finish_tool_work call are
    given real, separated internal latency (via the fake LLM's own clock
    reads, never a harness-owned bracket) -- if TTFA had been backdated to
    generation time (t=4.0) instead of delivery time (t=6.0), this test's
    assertion would catch the 2-second discrepancy directly."""
    class _SlowGateLLM(_ScriptedLLM):
        def chat(self, *a, **kw):
            if self.call_count == 1:  # candidate-producing WORK round
                agent_module.time.monotonic()  # candidate "generated" here...
                agent_module.time.monotonic()  # ...taking until t=4.0
            elif self.call_count == 2:  # the gate's own finish_tool_work call
                agent_module.time.monotonic()  # gate round trip starts...
                agent_module.time.monotonic()  # ...taking until t=6.0
            return super().chat(*a, **kw)

    llm = _SlowGateLLM(turns=[
        {"tool_calls": [_tc("search_memory")]},
        {"content": "the answer, generated early but delivered late",
         "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)
    fake_clock([
        0.0,   # 1: turn_started_at
        1.0, 4.0,  # 2,3: WORK round 2 -- candidate "generated" by t=4.0
        4.0, 6.0,   # 4,5: gate's own call -- round trip until t=6.0
        6.01,        # 6: _finalize_completion_candidate()'s TTFA read, at
                     #    _deliver_held_text()'s first actually-delivered
                     #    chunk -- AFTER the gate, not at generation time
        6.02,         # 7: chat()'s terminal duration_s
    ])

    result = LuminaAgent.chat(fake, "find it")

    assert result == "the answer, generated early but delivered late"
    ttfa_s = calls["time_to_first_answer"][0]
    # Delivery time (~6.01s), not generation time (~4.0s) and not the
    # replay loop's own (zero) duration.
    assert ttfa_s == pytest.approx(6.01)
    assert ttfa_s != pytest.approx(4.0)
    # Still never a "stream" figure of any kind for a held candidate.
    assert calls["final_stream_timing"] == []
    assert calls["final_ttft"] == []


# ── TTFA/Final TTFT unavailable cases (Bino's contract: "Empty, error-only,
#    cancelled-before-Final, and restored messages show unavailable.") ──

def test_ttfa_unavailable_for_empty_final(fake_clock):
    fake, calls, result = _run_direct_stream(
        fake_clock,
        ["__THINK_START__", "only reasoning, no real answer", "__THINK_END__"],
        [10.0, 10.5],
    )
    assert result == ""
    assert calls["time_to_first_answer"] == []
    # Final TTFT still fires (a real, nonempty output delta of THIS
    # request WAS received) -- a different, defensible question with a
    # real answer even though the turn ultimately produced no visible text.
    assert calls["final_ttft"] == [pytest.approx(0.5)]


def test_ttfa_and_final_ttft_unavailable_for_error_only_stream(fake_clock):
    fake, calls = _fake_agent(_ScriptedLLM(turns=[]))

    class _ImmediatelyErroringLLM:
        def chat_stream(self, messages, max_tokens=None, reasoning_effort=None):
            raise ConnectionError("provider unreachable")
            yield  # pragma: no cover -- unreachable, makes this a generator
    fake.llm = _ImmediatelyErroringLLM()
    # 1 tick: final_request_dispatch_at (set before the provider call
    # opens) -- the connection error fires on the very first
    # next(stream)) call, before any chunk is ever received, so
    # final_ttft_fired never flips True and no further monotonic() call
    # happens.
    fake_clock([5.0])

    result = LuminaAgent._stream_final(fake, messages=[], think_step=[0])

    assert result.startswith("[Stream error:")
    assert calls["time_to_first_answer"] == []
    assert calls["final_ttft"] == []


def test_ttfa_unavailable_when_cancelled_before_any_final_delta(fake_clock):
    fake, calls = _fake_agent(_ScriptedLLM(turns=[]))
    cancel_event = types.SimpleNamespace(is_set=lambda: True)  # already cancelled

    class _NeverReachedLLM:
        def chat_stream(self, messages, max_tokens=None, reasoning_effort=None):
            yield "would-be answer"  # pragma: no cover -- never reached
    fake.llm = _NeverReachedLLM()
    fake_clock([])  # cancellation is observed before the stream even opens

    with pytest.raises(TurnCancelled):
        LuminaAgent._stream_final(fake, messages=[], think_step=[0], cancel_event=cancel_event)

    assert calls["time_to_first_answer"] == []
    assert calls["final_ttft"] == []
