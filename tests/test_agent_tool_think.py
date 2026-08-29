"""
AGENT-TOOL-THINK-TELEMETRY-01A1 -- Passive Tool-Round Reasoning Telemetry.

A0's source-vet (see the task-block report) confirmed core/agent.py's
non-streaming WORK/GATE tool-decision rounds already receive whatever
legitimate reasoning a provider returns -- a "reasoning_content"/"reasoning"/
"thinking" field sibling to tool_calls on the raw response, and/or an inline
<think>...</think> span inside message["content"] that strip_think_blocks()
was already destroying before this patch -- and simply never read either.
This file exercises the new _collect_tool_round_reasoning()/
_emit_tool_round_think() pair: passive extraction only, routed through the
EXISTING on_think_start/on_think_token/on_think_end callbacks, never a new
Qt-facing signal.

Design law under test:
  - Observable order for a tool-bearing round with both reasoning and
    commentary present: Think -> Commentary -> Tool.
  - No reasoning this round -> zero Think calls, no empty Think widget --
    straight to Commentary -> Tool (or straight to Tool with no commentary).
  - Reasoning is never persisted to ctx.history, never becomes commentary,
    never becomes the final streamed answer, never touches
    _session_tool_calls or any other tool/response metric.
  - WORK and GATE rounds share identical Think/Commentary
    ordering/isolation semantics; GATE's existing non-persistence contract
    (AGENT-CONTINUATION-CONTROL-GATE-01A) is unchanged.
  - Think numbering is ONE shared, monotonically advancing counter across
    WORK rounds, GATE rounds, and the final stream -- never separate
    per-phase counters.
  - Malformed (non-string) or empty-after-strip reasoning fails inertly:
    no Think event, no exception, no str()/repr() coercion.
  - A backend with no extract_reasoning() at all keeps working exactly as
    before this patch (zero Think events, no crash).
  - Cancellation right after Think (before Commentary) and right after
    Commentary (before Tool dispatch) still raises TurnCancelled/returns
    "cancelled" per the existing cooperative-cancel contract, without
    weakening it.

Reuses the exact _ScriptedLLM / types.SimpleNamespace fake-agent pattern
from tests/test_agent_commentary.py (read directly before writing this
file), extended with a "reasoning" key per scripted turn and an
extract_reasoning() method on the fake backend.
"""
import threading
import types

import pytest

from core.agent import LuminaAgent, TurnCancelled, FINISH_TOOL_WORK_NAME, CONTINUE_TOOL_WORK_NAME
from core.backends.base import TerminationStatus


def _tc(name, call_id=None):
    return {"id": call_id or name, "type": "function",
            "function": {"name": name, "arguments": "{}"}}


class _ScriptedLLM:
    """Same scripted-turn fake as test_agent_commentary.py's _ScriptedLLM,
    extended with an optional "reasoning" key per turn:
      {"tool_calls": [...], "content": "...", "reasoning": "..."}
    extract_reasoning(response) reads turn["reasoning"] (default None) --
    mirrors how a real OpenAI-compatible backend's raw response carries a
    reasoning field sibling to tool_calls on the SAME message object.
    """
    display_name = "FakeProvider"
    name = "fake"

    def __init__(self, turns):
        self.turns = list(turns)
        self.call_count = 0
        self.tools_seen = []

    def get_model(self):
        return "fake-model"

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
        return turn.get("termination", TerminationStatus.UNKNOWN)

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
        yield "final streamed response"


class _ScriptedLLMNoExtractor(_ScriptedLLM):
    """Same fake, minus extract_reasoning entirely -- proves a backend
    that predates this patch (or one that genuinely has nothing to
    override, like Anthropic/Gemini today) keeps working unchanged."""
    extract_reasoning = None  # not callable -- getattr(..., None) path


def _fake_agent(llm, tool_result="ok"):
    history = []
    calls = {
        "ephemeral": [], "registry_calls": [], "on_tool_call": [],
        "on_tool_result": [], "response_tokens": [], "commentary": [],
        "think_start": [], "think_token": [], "think_end": 0,
    }

    def registry_call(name, args):
        calls["registry_calls"].append(name)
        return tool_result

    def on_commentary(text):
        calls["commentary"].append(text)

    def on_think_start(step):
        calls["think_start"].append(step)

    def on_think_token(tok):
        calls["think_token"].append(tok)

    def on_think_end():
        calls["think_end"] += 1

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
        on_think_start=on_think_start,
        on_think_token=on_think_token,
        on_think_end=on_think_end,
        on_response_token=lambda tok: calls["response_tokens"].append(tok),
        on_commentary=on_commentary,
        tts=None,
        _session_tool_calls=0,
        _skill_nudge_sent=False,
    )
    ns._stream_final = types.MethodType(LuminaAgent._stream_final, ns)
    return ns, calls


def _order_tracking_agent(llm, tool_result="ok"):
    """Same as _fake_agent(), but also records a single global `order`
    list of ("think_start"|"think_token"|"think_end"|"commentary"|
    "tool_call", ...) tuples across every callback, so ordering can be
    asserted across the whole turn, not just per-callback presence."""
    fake, calls = _fake_agent(llm, tool_result=tool_result)
    order = []

    real_think_start = fake.on_think_start
    real_think_token = fake.on_think_token
    real_think_end = fake.on_think_end
    real_commentary = fake.on_commentary
    real_tool_call = fake.on_tool_call

    fake.on_think_start = lambda step: (order.append(("think_start", step)), real_think_start(step))[-1]
    fake.on_think_token = lambda tok: (order.append(("think_token", tok)), real_think_token(tok))[-1]
    fake.on_think_end = lambda: (order.append(("think_end",)), real_think_end())[-1]
    fake.on_commentary = lambda text: (order.append(("commentary", text)), real_commentary(text))[-1]
    fake.on_tool_call = lambda name, args: (order.append(("tool_call", name)), real_tool_call(name, args))[-1]

    return fake, calls, order


# ── 1. reasoning + commentary + tool -> exact Think -> Commentary -> Tool ──

def test_1_reasoning_commentary_tool_exact_order():
    llm = _ScriptedLLM([
        {"content": "Checking memory for that.", "reasoning": "I should search memory first.",
         "tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls, order = _order_tracking_agent(llm)

    result = LuminaAgent.chat(fake, "find it")

    assert result == "final streamed response"
    assert order == [
        ("think_start", 1),
        ("think_token", "I should search memory first."),
        ("think_end",),
        ("commentary", "Checking memory for that."),
        ("tool_call", "search_memory"),
    ]


# ── 2. reasoning + tool, no commentary ──────────────────────────────────

def test_2_reasoning_with_tool_no_commentary():
    llm = _ScriptedLLM([
        {"content": "", "reasoning": "Need to look this up.", "tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls, order = _order_tracking_agent(llm)

    LuminaAgent.chat(fake, "find it")

    assert calls["think_start"] == [1]
    assert calls["think_token"] == ["Need to look this up."]
    assert calls["think_end"] == 1
    assert calls["commentary"] == []
    assert order == [
        ("think_start", 1), ("think_token", "Need to look this up."), ("think_end",),
        ("tool_call", "search_memory"),
    ]


# ── 3. commentary + tool, no reasoning ──────────────────────────────────

def test_3_commentary_with_tool_no_reasoning():
    llm = _ScriptedLLM([
        {"content": "Checking memory for that.", "tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls, order = _order_tracking_agent(llm)

    LuminaAgent.chat(fake, "find it")

    assert calls["think_start"] == []
    assert calls["think_end"] == 0
    assert calls["commentary"] == ["Checking memory for that."]
    assert order == [("commentary", "Checking memory for that."), ("tool_call", "search_memory")]


# ── 4. tool only -- neither reasoning nor commentary ────────────────────

def test_4_tool_only_no_reasoning_no_commentary():
    llm = _ScriptedLLM([
        {"content": "", "tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls, order = _order_tracking_agent(llm)

    LuminaAgent.chat(fake, "find it")

    assert calls["think_start"] == []
    assert calls["commentary"] == []
    assert order == [("tool_call", "search_memory")]


# ── 5. malformed (non-string) reasoning fails inertly ───────────────────

def test_5_malformed_reasoning_fails_inertly():
    llm = _ScriptedLLM([
        {"content": "", "reasoning": {"not": "a string"}, "tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls, order = _order_tracking_agent(llm)

    result = LuminaAgent.chat(fake, "find it")

    assert result == "final streamed response"
    assert calls["think_start"] == []
    assert calls["think_token"] == []
    assert calls["think_end"] == 0
    # Never coerced into commentary either.
    assert calls["commentary"] == []


# ── 6. empty / whitespace-only reasoning -> no Think event ─────────────

def test_6_empty_reasoning_no_think_event():
    llm = _ScriptedLLM([
        {"content": "", "reasoning": "   \n\t  ", "tool_calls": [_tc("search_memory")]},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls, order = _order_tracking_agent(llm)

    LuminaAgent.chat(fake, "find it")

    assert calls["think_start"] == []
    assert calls["think_end"] == 0


# ── 7. inline <think> extraction before strip_think_blocks() destroys it ─

def test_7_inline_think_extracted_before_strip():
    llm = _ScriptedLLM([
        {"content": "<think>weighing which tool to use</think>Checking the file now.",
         "tool_calls": [_tc("read_file")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls, order = _order_tracking_agent(llm)

    result = LuminaAgent.chat(fake, "read it")
    assert result == "final streamed response"

    assert calls["think_token"] == ["weighing which tool to use"]
    assert calls["commentary"] == ["Checking the file now."]
    assert "weighing which tool to use" not in calls["commentary"][0]
    assert order[:3] == [
        ("think_start", 1), ("think_token", "weighing which tool to use"), ("think_end",),
    ]
    assert ("commentary", "Checking the file now.") in order
    assert order.index(("think_end",)) < order.index(("commentary", "Checking the file now."))


# ── 8. reasoning absent -> no empty Think widget/event (explicit) ──────

def test_8_reasoning_absent_no_empty_think_widget():
    llm = _ScriptedLLM([
        {"content": "Just answering directly.", "termination": TerminationStatus.COMPLETE},
    ])
    fake, calls, order = _order_tracking_agent(llm)

    result = LuminaAgent.chat(fake, "hello")

    assert result == "final streamed response"
    assert calls["think_start"] == []
    assert calls["think_end"] == 0


# ── 9. WORK and GATE parity ─────────────────────────────────────────────

def test_9_work_and_gate_parity_think_fires_in_both():
    llm = _ScriptedLLM([
        {"content": "Running the search.", "reasoning": "First, search memory.",
         "tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},   # no-tool WORK round -> gate
        {"content": "That's everything I need.", "reasoning": "The search result is sufficient.",
         "tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},                 # GATE round
    ])
    fake, calls, order = _order_tracking_agent(llm)

    result = LuminaAgent.chat(fake, "find it")

    assert result == "final streamed response"
    # Two Think events fired: one in the WORK round, one in the GATE round.
    assert calls["think_start"] == [1, 2]
    assert calls["think_token"] == ["First, search memory.", "The search result is sufficient."]
    assert calls["think_end"] == 2
    assert calls["commentary"] == ["Running the search.", "That's everything I need."]
    # GATE round: Think (step 2) still precedes its own Commentary.
    think2_idx = order.index(("think_start", 2))
    commentary2_idx = order.index(("commentary", "That's everything I need."))
    assert think2_idx < commentary2_idx
    # GATE's existing non-persistence contract: the finish_tool_work
    # sentinel call is never persisted or dispatched as a real tool.
    assert FINISH_TOOL_WORK_NAME not in calls["registry_calls"]


def test_9b_gate_continue_outcome_also_gets_think():
    """GATE's "continue" outcome (not just "finish") is in scope too --
    both share the same (finish, continue) gating commentary already used
    before this patch."""
    llm = _ScriptedLLM([
        {"content": "", "tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"content": "Need one more lookup.", "reasoning": "Not enough evidence yet.",
         "tool_calls": [_tc(CONTINUE_TOOL_WORK_NAME)]},
        {"content": "", "tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls, order = _order_tracking_agent(llm)

    LuminaAgent.chat(fake, "find it")

    assert calls["think_token"] == ["Not enough evidence yet."]
    assert calls["commentary"] == ["Need one more lookup."]


# ── 10. reasoning never enters ctx.history ──────────────────────────────

def test_10_reasoning_never_enters_history():
    llm = _ScriptedLLM([
        {"content": "Running the search.", "reasoning": "SECRET_REASONING_TEXT_MARKER",
         "tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls, order = _order_tracking_agent(llm)

    LuminaAgent.chat(fake, "find it")

    for entry in fake.ctx.history:
        assert "SECRET_REASONING_TEXT_MARKER" not in str(entry)


# ── 11. reasoning never becomes commentary ──────────────────────────────

def test_11_reasoning_never_becomes_commentary():
    llm = _ScriptedLLM([
        {"content": "Outward text only.", "reasoning": "REASONING_ONLY_MARKER",
         "tool_calls": [_tc("search_memory")]},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls, order = _order_tracking_agent(llm)

    LuminaAgent.chat(fake, "find it")

    assert calls["commentary"] == ["Outward text only."]
    for text in calls["commentary"]:
        assert "REASONING_ONLY_MARKER" not in text


# ── 12. reasoning never becomes the final response ──────────────────────

def test_12_reasoning_never_becomes_final_response():
    llm = _ScriptedLLM([
        {"content": "", "reasoning": "FINAL_LEAK_MARKER", "tool_calls": [_tc("search_memory")]},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls, order = _order_tracking_agent(llm)

    result = LuminaAgent.chat(fake, "find it")

    assert "FINAL_LEAK_MARKER" not in result
    assert all("FINAL_LEAK_MARKER" not in tok for tok in calls["response_tokens"])


# ── 13. reasoning does not alter tool/session metrics ───────────────────

def test_13_reasoning_does_not_alter_tool_metrics():
    llm_with = _ScriptedLLM([
        {"content": "", "reasoning": "some reasoning", "tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    llm_without = _ScriptedLLM([
        {"content": "", "tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake_with, _, _ = _order_tracking_agent(llm_with)
    fake_without, _, _ = _order_tracking_agent(llm_without)

    LuminaAgent.chat(fake_with, "find it")
    LuminaAgent.chat(fake_without, "find it")

    assert fake_with._session_tool_calls == fake_without._session_tool_calls == 1


# ── 14. one shared Think-step sequence across WORK/GATE/final ──────────

def test_14_shared_think_step_sequence_across_work_gate_final():
    class _ScriptedLLMWithFinalThink(_ScriptedLLM):
        def chat_stream(self, messages, max_tokens=None, reasoning_effort=None):
            yield "__THINK_START__"
            yield "final reasoning"
            yield "__THINK_END__"
            yield "the actual final answer"

    llm = _ScriptedLLMWithFinalThink([
        {"content": "", "reasoning": "work round reasoning", "tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"content": "", "reasoning": "gate round reasoning", "tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls, order = _order_tracking_agent(llm)

    result = LuminaAgent.chat(fake, "find it")

    assert result == "the actual final answer"
    # WORK round -> step 1, GATE round -> step 2, final stream -> step 3.
    # Never reset back to 1 for the final stream.
    assert calls["think_start"] == [1, 2, 3]


# ── 15. cancellation boundaries ─────────────────────────────────────────

def test_15a_cancel_after_think_before_commentary_work_round():
    cancel_event = threading.Event()
    llm = _ScriptedLLM([
        {"content": "About to check memory.", "reasoning": "Thinking about this.",
         "tool_calls": [_tc("search_memory")]},
    ])
    fake, calls, order = _order_tracking_agent(llm)

    real_think_end = fake.on_think_end
    fake.on_think_end = lambda: (real_think_end(), cancel_event.set())[-1]

    with pytest.raises(TurnCancelled):
        LuminaAgent.chat(fake, "find it", cancel_event=cancel_event)

    # Think already, truthfully, fired.
    assert calls["think_start"] == [1]
    assert calls["think_end"] == 1
    # But commentary and the tool it described never ran.
    assert calls["commentary"] == []
    assert calls["on_tool_call"] == []
    assert calls["registry_calls"] == []
    # The existing cancellation contract is NOT weakened to get this
    # ordering: the assistant tool-call message still gets persisted, and
    # its tool_call id still gets a paired cancelled result -- exactly
    # like the tool-dispatch loop's own cancellation check already
    # guarantees (see tests/test_operator_stop.py's
    # test_cancel_after_tool_message_before_execution_closes_every_id).
    assert any(
        h.get("role") == "assistant" and h.get("tool_calls")
        for h in fake.ctx.history if isinstance(h, dict)
    )
    assert any(
        h.get("role") == "tool" and "Cancelled by operator" in str(h.get("content", ""))
        for h in fake.ctx.history if isinstance(h, dict)
    )


def test_15b_cancel_after_commentary_before_tool_dispatch_work_round():
    cancel_event = threading.Event()
    llm = _ScriptedLLM([
        {"content": "About to check memory.", "reasoning": "Thinking about this.",
         "tool_calls": [_tc("search_memory")]},
    ])
    fake, calls, order = _order_tracking_agent(llm)

    real_commentary = fake.on_commentary
    fake.on_commentary = lambda text: (real_commentary(text), cancel_event.set())[-1]

    with pytest.raises(TurnCancelled):
        LuminaAgent.chat(fake, "find it", cancel_event=cancel_event)

    assert calls["think_end"] == 1
    assert calls["commentary"] == ["About to check memory."]
    assert calls["on_tool_call"] == []
    assert calls["registry_calls"] == []


def test_15c_cancel_after_think_before_commentary_gate_round():
    cancel_event = threading.Event()
    llm = _ScriptedLLM([
        {"content": "", "tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"content": "Wrapping up.", "reasoning": "That's sufficient.",
         "tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls, order = _order_tracking_agent(llm)

    real_think_end = fake.on_think_end
    fake.on_think_end = lambda: (real_think_end(), cancel_event.set())[-1]

    with pytest.raises(TurnCancelled):
        LuminaAgent.chat(fake, "find it", cancel_event=cancel_event)

    # Round 0 (search_memory) had no reasoning -- only the GATE round's
    # Think fires, as step 1 (not 2 -- there was no earlier Think event
    # to advance past).
    assert calls["think_start"] == [1]
    # The GATE-round commentary must never have fired after cancellation.
    assert "Wrapping up." not in calls["commentary"]


# ── 16. backend without extract_reasoning() at all stays compatible ────

def test_16_backend_without_extractor_stays_compatible():
    llm = _ScriptedLLMNoExtractor([
        {"content": "Checking memory for that.", "tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls, order = _order_tracking_agent(llm)

    result = LuminaAgent.chat(fake, "find it")

    assert result == "final streamed response"
    assert calls["think_start"] == []
    assert calls["commentary"] == ["Checking memory for that."]
    assert calls["registry_calls"] == ["search_memory"]
