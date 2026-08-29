"""
AGENT-CONTINUATION-01B -- Require Structural Choice During Tool Continuation.

01A gave the agent an explicit finish_tool_work completion signal, but
tool_choice stayed "auto" throughout -- live acceptance against
z-ai/glm-5.3-flash via OpenRouter showed a real model can simply ignore the
sentinel and answer in prose, twice, even after the corrective retry. 01A
behaved safely (visible non-success, never laundered), but a backend that
positively supports a required/forced tool-choice mode can rule that whole
class of ambiguity out structurally instead of just detecting it after the
fact.

This file covers:
  - agent policy: AUTO on the initial round, REQUIRED once tools_used_
    this_turn is non-empty (core/agent.py's tool loop), threaded into
    self.llm.chat() ONLY when the backend's chat() signature actually
    accepts tool_choice_mode= (_accepts_tool_choice_mode() -- the same
    backward-compatibility discipline 01A established for
    extract_termination()).
  - the shared resolver (BaseLLMBackend._resolve_tool_choice_mode()):
    REQUIRED only survives for a backend with supports_required_tool_choice
    = True; every other backend (unverified, or a pre-01B fake with no
    tool_choice_mode parameter at all) is byte-identical to pre-01B
    behavior.
  - that 01A's ambiguous/corrective-retry/terminal-notice machinery is
    completely UNCHANGED and remains the safety net regardless of which
    tool_choice_mode was requested (section 12: a supported backend that
    still returns no tool call is a genuine contract violation, not a new
    code path).

Reuses the types.SimpleNamespace fake-agent pattern from
tests/test_agent_continuation_contract.py (read directly before writing
this file).
"""
import types

from core.agent import LuminaAgent, FINISH_TOOL_WORK_NAME, CONTINUE_TOOL_WORK_NAME
from core.backends.base import TerminationStatus, ToolChoiceMode


def _tc(name, call_id=None):
    return {"id": call_id or name, "type": "function",
            "function": {"name": name, "arguments": "{}"}}


class _ScriptedLLM:
    """Same scripted-turn fake as test_agent_continuation_contract.py's
    _ScriptedLLM, extended to record the tool_choice_mode observed on each
    call and to simulate a real backend's supports_required_tool_choice
    flag (default True here -- most tests in this file want to observe
    what the agent REQUESTS; unsupported-backend behavior gets its own
    dedicated fake below rather than a flag flip, so the two code paths
    stay visibly distinct in the test bodies)."""
    display_name = "FakeProvider"
    name = "fake"
    supports_required_tool_choice = True

    def __init__(self, turns):
        self.turns = list(turns)
        self.call_count = 0
        self.tools_seen = []
        self.tool_choice_modes_seen = []

    def get_model(self):
        return "fake-model"

    def _resolve_tool_choice_mode(self, tool_choice_mode):
        if tool_choice_mode == ToolChoiceMode.REQUIRED and self.supports_required_tool_choice:
            return ToolChoiceMode.REQUIRED
        return ToolChoiceMode.AUTO

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
        return turn.get("termination", TerminationStatus.UNKNOWN)

    def is_tool_call(self, message):
        return bool(message.get("tool_calls"))

    def get_tool_calls(self, message):
        return message.get("tool_calls", [])

    def parse_tool_call(self, tc):
        return tc["function"]["name"], {}

    def chat_stream(self, messages, max_tokens=None, reasoning_effort=None):
        yield "final streamed response"


class _PreO1BScriptedLLM(_ScriptedLLM):
    """A fake LLM whose chat() has NO tool_choice_mode parameter and no
    **kwargs -- exactly what every hand-rolled test double looked like
    before 01B. Proves _accepts_tool_choice_mode() correctly falls back
    and 01A behavior is completely undisturbed for a caller like this."""

    def chat(self, messages, tools=None, max_tokens=None, reasoning_effort=None):
        self.tools_seen.append([t["function"]["name"] for t in (tools or [])])
        idx = self.call_count
        self.call_count += 1
        turn = self.turns[idx]
        if "raise" in turn:
            raise turn["raise"]
        return {"_turn": idx}


def _fake_agent(llm, tool_result="ok"):
    history = []
    calls = {
        "ephemeral": [], "registry_calls": [], "on_tool_call": [],
        "on_tool_result": [], "response_tokens": [],
    }

    def registry_call(name, args):
        calls["registry_calls"].append(name)
        return tool_result

    ctx = types.SimpleNamespace(
        history=history,
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
        build_messages=lambda tool_budget=0, chat_id=None: [],
    )
    registry = types.SimpleNamespace(
        schema_token_estimate=lambda: 0,
        get_schemas=lambda: [],
        list_enabled=lambda: [],
        all_tool_names=lambda: [],
        call=registry_call,
    )
    ns = types.SimpleNamespace(
        llm=llm,
        ctx=ctx,
        registry=registry,
        on_tool_call=lambda name, args: calls["on_tool_call"].append((name, args)),
        on_tool_result=lambda name, result: calls["on_tool_result"].append((name, result)),
        on_think_start=lambda step: None,
        on_think_token=lambda tok: None,
        on_think_end=lambda: None,
        on_response_token=lambda tok: calls["response_tokens"].append(tok),
        tts=None,
        _session_tool_calls=0,
        _skill_nudge_sent=False,
    )
    ns._stream_final = types.MethodType(LuminaAgent._stream_final, ns)
    return ns, calls


# ── A/B. Initial round ──────────────────────────────────────────────────

def test_A_initial_round_requests_auto():
    llm = _ScriptedLLM([{"content": "hi", "termination": TerminationStatus.COMPLETE}])
    fake, calls = _fake_agent(llm)

    LuminaAgent.chat(fake, "hello")

    assert llm.tool_choice_modes_seen == [ToolChoiceMode.AUTO]


def test_B_initial_round_does_not_expose_finish_tool_work():
    llm = _ScriptedLLM([{"content": "hi", "termination": TerminationStatus.COMPLETE}])
    fake, calls = _fake_agent(llm)

    LuminaAgent.chat(fake, "hello")

    assert FINISH_TOOL_WORK_NAME not in llm.tools_seen[0]


# ── C/D/E/F. Continuation requests REQUIRED, sentinel still works ──────

def test_C_gate_request_after_work_phase_no_tool_response_requests_required():
    """AGENT-CONTINUATION-CONTROL-GATE-01A -- REQUIRED is requested ONLY
    for the completion-control gate now, never for an ordinary WORK round,
    however many real tools already ran this turn."""
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    LuminaAgent.chat(fake, "find it")

    assert llm.tool_choice_modes_seen == [
        ToolChoiceMode.AUTO, ToolChoiceMode.AUTO, ToolChoiceMode.REQUIRED,
    ]


def test_D_tool_a_then_tool_b_work_rounds_both_stay_auto():
    """The core semantic flip from AGENT-REQUIRED-FULL-SCHEMA-01A's finding:
    every WORK-phase round requests AUTO now, including the second and
    third real tool calls in a chain -- REQUIRED never applies to the full
    product-tool selection surface, only to the tiny two-control gate."""
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("tool_a")]},
        {"tool_calls": [_tc("tool_b")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    LuminaAgent.chat(fake, "chain two tools")

    assert llm.tool_choice_modes_seen == [
        ToolChoiceMode.AUTO, ToolChoiceMode.AUTO, ToolChoiceMode.AUTO, ToolChoiceMode.REQUIRED,
    ]


def test_E_finish_tool_work_transitions_to_stream_final_exactly_once():
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "find it")

    assert result == "final streamed response"
    assert llm.call_count == 3


def test_F_required_mode_never_requested_by_final_streaming():
    """chat_stream()'s signature carries no tool_choice_mode param at all
    (mirrors 01A's own chat_stream()-carries-no-tools precedent) -- this is
    a structural guarantee, not a per-call observation, so assert directly
    against the signature."""
    import inspect
    from core.backends.base import BaseLLMBackend
    params = inspect.signature(BaseLLMBackend.chat_stream).parameters
    assert "tool_choice_mode" not in params


# ── G/H/I. Unsupported-backend fallback ─────────────────────────────────

def test_G_unsupported_backend_preserves_01a_bounded_retry():
    """supports_required_tool_choice = False on this instance -- the AGENT
    still unconditionally REQUESTS REQUIRED for the gate call (resolution
    against backend capability is the backend layer's job, never the
    agent's -- see _run_tool_work_control_gate()'s comment), and the gate
    still reaches a decision, completing the turn."""
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    llm.supports_required_tool_choice = False
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "find it")

    assert result == "final streamed response"
    assert len(calls["ephemeral"]) == 1  # the gate's own single instruction
    assert llm.tool_choice_modes_seen == [
        ToolChoiceMode.AUTO, ToolChoiceMode.AUTO, ToolChoiceMode.REQUIRED,
    ]


def test_H_preO1B_fake_never_receives_the_new_kwarg_at_all():
    """The compatibility guard (_accepts_tool_choice_mode) must keep an
    old-style fake -- no tool_choice_mode param, no **kwargs -- working
    completely unmodified. This IS the "no invented provider fields" proof
    for a backend that doesn't even know the concept exists."""
    llm = _PreO1BScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "find it")

    assert result == "final streamed response"  # would have raised TypeError otherwise


def test_I_unsupported_backend_can_still_complete_via_a_single_gate_round():
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    llm.supports_required_tool_choice = False
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "find it")

    assert result == "final streamed response"
    # The gate's own instruction is its own single ephemeral push -- not a
    # corrective retry. No corrective-retry nudge was ever needed.
    assert len(calls["ephemeral"]) == 1


# ── J/K. Isolation -- not sticky ─────────────────────────────────────────

def test_J_required_mode_is_per_round_not_sticky():
    """After finish_tool_work, a whole separate NEW turn on the SAME agent/
    backend instance must observe AUTO again at its own round 1 -- proves
    tool_choice_mode is derived fresh from this turn's tools_used_this_turn
    every time, never cached on the backend."""
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
        {"content": "second turn, no tool needed", "termination": TerminationStatus.COMPLETE},
    ])
    fake, calls = _fake_agent(llm)

    LuminaAgent.chat(fake, "find it")
    LuminaAgent.chat(fake, "unrelated new question")

    assert llm.tool_choice_modes_seen == [
        ToolChoiceMode.AUTO, ToolChoiceMode.AUTO, ToolChoiceMode.REQUIRED, ToolChoiceMode.AUTO,
    ]


def test_K_new_turn_starts_auto_again():
    # Same assertion as J, phrased as its own scenario per the task's test
    # matrix naming -- kept as a separate, minimal test for direct mapping
    # to matrix item K.
    llm = _ScriptedLLM([
        {"content": "first", "termination": TerminationStatus.COMPLETE},
        {"content": "second", "termination": TerminationStatus.COMPLETE},
    ])
    fake, calls = _fake_agent(llm)

    LuminaAgent.chat(fake, "turn one")
    LuminaAgent.chat(fake, "turn two")

    assert llm.tool_choice_modes_seen == [ToolChoiceMode.AUTO, ToolChoiceMode.AUTO]


# ── L/M/N. Unaffected surfaces ───────────────────────────────────────────

def test_L_complete_utility_never_touches_tool_choice():
    """complete_utility() (core/backends/base.py) always calls
    self.chat(tools=None, ...) -- no tools means no tool_choice field on
    any real backend's wire payload regardless of what this patch adds,
    and it never passes tool_choice_mode at all."""
    import inspect
    from core.backends.base import BaseLLMBackend
    src = inspect.getsource(BaseLLMBackend.complete_utility)
    assert "tool_choice_mode" not in src
    assert "tools=None" in src


def test_M_reasoning_settings_unaffected(monkeypatch):
    """A turn that both requests REQUIRED and forwards a reasoning_effort
    must still carry that reasoning_effort through untouched -- proves the
    two concerns are threaded independently, not coupled."""
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)

    reasoning_effort_seen = []
    real_chat = llm.chat

    def chat_and_record(messages, tools=None, max_tokens=None, reasoning_effort=None,
                         tool_choice_mode=None):
        reasoning_effort_seen.append(reasoning_effort)
        return real_chat(messages, tools=tools, max_tokens=max_tokens,
                          reasoning_effort=reasoning_effort, tool_choice_mode=tool_choice_mode)

    llm.chat = chat_and_record

    LuminaAgent.chat(fake, "find it", reasoning_effort="high")

    assert reasoning_effort_seen == ["high", "high", "high"]
    assert llm.tool_choice_modes_seen == [
        ToolChoiceMode.AUTO, ToolChoiceMode.AUTO, ToolChoiceMode.REQUIRED,
    ]


def test_N_registry_schema_content_unaffected_by_tool_choice_mode():
    """The REGISTRY's own get_schemas() output (tool profile membership) is
    identical across every WORK round regardless of tool_choice_mode -- and
    the control gate NEVER mixes a product tool into its own request at
    all, proving work-selection and completion-control are two
    structurally disjoint tool lists (AGENT-CONTINUATION-CONTROL-GATE-01A),
    not one combined list gated only by tool_choice."""
    fixed_schema = {"type": "function", "function": {
        "name": "search_memory", "description": "", "parameters": {}}}
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)
    fake.registry.get_schemas = lambda: [fixed_schema]

    LuminaAgent.chat(fake, "find it")

    assert llm.tools_seen[0] == ["search_memory"]
    assert llm.tools_seen[1] == ["search_memory"]
    assert set(llm.tools_seen[2]) == {CONTINUE_TOOL_WORK_NAME, FINISH_TOOL_WORK_NAME}
    assert "search_memory" not in llm.tools_seen[2]


# ── O. Provider exception stays distinguishable ─────────────────────────

def test_O_provider_rejection_of_required_stays_a_provider_exception():
    """A continuation chat() call that RAISES (provider rejected the
    REQUIRED request) must produce the pre-existing "rejected the
    continuation" observability message -- never the 01A ambiguity notice.
    These are structurally different code paths (exception vs a returned
    ambiguous response) and requesting REQUIRED must not blur that."""
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"raise": RuntimeError("OpenRouter error (generic, HTTP 400): tool_choice rejected")},
    ])
    fake, calls = _fake_agent(llm)

    result = LuminaAgent.chat(fake, "find it")

    assert "rejected the continuation" in result
    assert "search_memory" in result
    assert not result.startswith("[Lumina: tool-work continuation ended")


# ── P/Q. Cancellation around the required-continuation call ────────────

def test_P_cancel_before_required_continuation_prevents_the_call():
    import threading
    import pytest
    from core.agent import TurnCancelled

    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent(llm)
    event = threading.Event()

    real_get_tool_calls = llm.get_tool_calls

    def get_tool_calls_then_cancel(message):
        out = real_get_tool_calls(message)
        event.set()
        return out

    llm.get_tool_calls = get_tool_calls_then_cancel

    with pytest.raises(TurnCancelled):
        LuminaAgent.chat(fake, "find it", cancel_event=event)

    assert llm.call_count == 1  # the would-be REQUIRED continuation never fired


def test_Q_cancel_after_required_response_before_dispatch_respects_boundary():
    import threading
    import pytest
    from core.agent import TurnCancelled

    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"tool_calls": [_tc("read_file")]},
    ])
    fake, calls = _fake_agent(llm)
    event = threading.Event()

    real_chat = llm.chat

    def chat_and_maybe_cancel(messages, tools=None, max_tokens=None, reasoning_effort=None,
                               tool_choice_mode=None):
        response = real_chat(messages, tools=tools, max_tokens=max_tokens,
                              reasoning_effort=reasoning_effort, tool_choice_mode=tool_choice_mode)
        if llm.call_count == 2:  # the REQUIRED continuation's own response just arrived
            event.set()
        return response

    llm.chat = chat_and_maybe_cancel

    with pytest.raises(TurnCancelled):
        LuminaAgent.chat(fake, "find it", cancel_event=event)

    # The second tool (read_file) never got far enough to be dispatched.
    assert calls["registry_calls"] == ["search_memory"]
