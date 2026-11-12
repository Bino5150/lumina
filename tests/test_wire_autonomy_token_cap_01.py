"""WIRE-AUTONOMY-01 -- the Wire responder's own hard token allowance.

An autonomous Wire turn must not inherit the owner's general response reserve
(``response_reserve_tokens``, 25,000 on Lumina Prime) as its output limit, and a
reserve is not a cap. These tests drive the real ``LuminaAgent.chat`` tool loop
(the same fake-agent pattern as test_agent_continuation_control_gate.py) against a
scripted backend that records the ``max_tokens`` of every provider call:

* the work call, the completion gate and a streamed final are each capped at the
  turn's output limit, at the point where the request leaves for the provider;
* a call the turn's remaining allowance cannot support is refused and nothing is
  sent;
* an agent with no budget (every owner, Telegram and Discord turn) keeps the
  general reserve exactly as before.

No network, no paid inference, no owner state.
"""

import types
from unittest import mock

import pytest

import config
from comms import wire_autonomy as WA
from core.agent import (
    _CONTROL_GATE_SCHEMAS,
    _GATE_CANDIDATE_REVIEW_INSTRUCTION,
    CONTINUE_TOOL_WORK_NAME,
    FINISH_TOOL_WORK_NAME,
    GATE_DECISION_CUE,
    LuminaAgent,
    TurnBudgetRefused,
    TurnTokenBudget,
    _accumulate_provider_telemetry,
    _provider_chat_or_error,
    is_error_response,
)
from core.backends.base import TerminationStatus


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr("core.agent.build_skills_block", lambda user_input: "")
    monkeypatch.setattr(config, "RESPONSE_RESERVE_TOKENS", 25_000)  # Lumina Prime's setting


def _tc(name):
    return {"id": name, "type": "function", "function": {"name": name, "arguments": "{}"}}


class _RecordingLLM:
    """Scripted backend that records the max_tokens of every provider call."""

    display_name = "FakeProvider"
    name = "fake"
    supports_required_tool_choice = True

    def __init__(self, turns):
        self.turns = list(turns)
        self.max_tokens_seen = []
        self.stream_max_tokens_seen = []
        self.stream_calls = 0

    @property
    def call_count(self):
        return len(self.max_tokens_seen)

    def get_model(self):
        return "fake-model"

    def chat(self, messages, tools=None, max_tokens=None, reasoning_effort=None,
             tool_choice_mode=None):
        self.max_tokens_seen.append(max_tokens)
        return {"_turn": len(self.max_tokens_seen) - 1}

    def extract_response_telemetry(self, response):
        usage = self.turns[response["_turn"]].get("usage")
        return {"usage": usage} if usage else {}

    def extract_message(self, response):
        turn = self.turns[response["_turn"]]
        message = {"role": "assistant", "content": turn.get("content", "")}
        if "tool_calls" in turn:
            message["tool_calls"] = turn["tool_calls"]
        return message

    def extract_termination(self, response):
        return self.turns[response["_turn"]].get("termination", TerminationStatus.COMPLETE)

    def is_tool_call(self, message):
        return bool(message.get("tool_calls"))

    def get_tool_calls(self, message):
        return message.get("tool_calls", [])

    def parse_tool_call(self, tc):
        return tc["function"]["name"], {}

    def chat_stream(self, messages, max_tokens=None, reasoning_effort=None):
        self.stream_calls += 1
        self.stream_max_tokens_seen.append(max_tokens)
        yield "final streamed response"


def _usage(total):
    return {"prompt_tokens": total - 10, "completion_tokens": 10, "total_tokens": total}


def _zero_tool_agent(llm, *, budget=None, system_chars=0):
    """A stand-in for a Wire-Peer agent: no tools, one tiny system message."""
    history = []
    system = "s" * max(1, system_chars)

    def build_messages(tool_budget=0, chat_id=None):
        return [{"role": "system", "content": system}, {"role": "user", "content": "hi @lumina"}]

    ctx = types.SimpleNamespace(
        history=history,
        add_user=lambda content, source="OWNER_DIRECT": history.append({"role": "user", "content": content}),
        add_assistant=lambda content: history.append({"role": "assistant", "content": content}),
        add_tool_call=lambda message: history.append(message),
        add_tool_result=lambda *a, **k: None,
        add_cancelled_tool_result=lambda *a, **k: None,
        push_ephemeral=lambda block: None,
        build_messages=build_messages,
    )
    registry = types.SimpleNamespace(
        schema_token_estimate=lambda: 0,
        get_schemas=lambda: [],
        list_enabled=lambda: [],
        all_tool_names=lambda: [],
        call=lambda name, args: "",
    )
    shown = []
    ns = types.SimpleNamespace(
        llm=llm, ctx=ctx, registry=registry, token_budget=budget, shown=shown,
        on_tool_call=lambda name, args: None, on_tool_result=lambda name, result: None,
        on_think_start=lambda step: None, on_think_token=lambda tok: None,
        on_think_end=lambda: None, on_response_token=lambda tok: shown.append(tok),
        on_commentary=lambda text: None, tts=None, _session_tool_calls=0, _skill_nudge_sent=False,
    )
    ns._stream_final = types.MethodType(LuminaAgent._stream_final, ns)
    ns._finalize_completion_candidate = types.MethodType(LuminaAgent._finalize_completion_candidate, ns)
    return ns


def _answer_then_finish(first_usage=None, gate_usage=None):
    """The two provider calls every no-tools turn makes: the work call answers, the gate finishes."""
    return [
        {"content": "I hear you, tech.", "termination": TerminationStatus.COMPLETE, "usage": first_usage},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)], "usage": gate_usage},
    ]


# ── the work call and the completion gate are capped where the request leaves ──


def test_the_work_call_and_the_completion_gate_are_both_capped_at_the_provider_call():
    llm = _RecordingLLM(_answer_then_finish(_usage(900), _usage(950)))
    budget = TurnTokenBudget(total=WA.TURN_TOKEN_BUDGET, max_output=WA.WIRE_MAX_OUTPUT_TOKENS)
    reply = LuminaAgent.chat(_zero_tool_agent(llm, budget=budget), "hi @lumina")
    assert reply == "I hear you, tech."
    assert llm.max_tokens_seen == [WA.WIRE_MAX_OUTPUT_TOKENS, WA.WIRE_MAX_OUTPUT_TOKENS]
    assert max(llm.max_tokens_seen) < config.RESPONSE_RESERVE_TOKENS, "not the owner's 25,000 reserve"


def test_each_call_settles_at_the_provider_reported_usage():
    llm = _RecordingLLM(_answer_then_finish(_usage(900), _usage(950)))
    budget = TurnTokenBudget(total=WA.TURN_TOKEN_BUDGET, max_output=WA.WIRE_MAX_OUTPUT_TOKENS)
    LuminaAgent.chat(_zero_tool_agent(llm, budget=budget), "hi @lumina")
    assert budget.spent == 900 + 950 and budget.charged() == 1_850


def test_an_agent_without_a_budget_keeps_the_general_reserve_on_every_call():
    llm = _RecordingLLM(_answer_then_finish())
    reply = LuminaAgent.chat(_zero_tool_agent(llm, budget=None), "hello")
    assert reply == "I hear you, tech."
    assert llm.max_tokens_seen == [25_000, 25_000]


def test_a_budget_only_ever_lowers_a_smaller_general_reserve_never_widens_it(monkeypatch):
    monkeypatch.setattr(config, "RESPONSE_RESERVE_TOKENS", 500)
    llm = _RecordingLLM(_answer_then_finish(_usage(400), _usage(400)))
    budget = TurnTokenBudget(total=WA.TURN_TOKEN_BUDGET, max_output=WA.WIRE_MAX_OUTPUT_TOKENS)
    LuminaAgent.chat(_zero_tool_agent(llm, budget=budget), "hi")
    assert llm.max_tokens_seen == [500, 500]


# ── a call the remaining allowance cannot support is refused, nothing is sent ──


def test_a_first_call_the_allowance_cannot_support_is_refused_before_anything_is_sent():
    llm = _RecordingLLM(_answer_then_finish())
    budget = TurnTokenBudget(total=1_500, max_output=WA.WIRE_MAX_OUTPUT_TOKENS)
    agent = _zero_tool_agent(llm, budget=budget, system_chars=3_000)  # ~1,000 input + 1,024 output
    reply = LuminaAgent.chat(agent, "hi @lumina")
    assert is_error_response(reply) and "token allowance" in reply
    assert llm.call_count == 0, "no request left for the provider"
    assert budget.charged() == 0


def test_the_gate_is_refused_once_the_work_call_has_used_the_allowance():
    llm = _RecordingLLM(_answer_then_finish(_usage(1_400), _usage(1_000)))
    budget = TurnTokenBudget(total=1_500, max_output=WA.WIRE_MAX_OUTPUT_TOKENS)
    reply = LuminaAgent.chat(_zero_tool_agent(llm, budget=budget), "hi @lumina")
    assert is_error_response(reply) and "token allowance" in reply
    assert llm.call_count == 1, "the gate was never sent"
    assert "I hear you, tech." not in reply, "an unconfirmed candidate is not promoted as the answer"
    assert budget.spent == 1_400


def test_a_call_whose_usage_was_never_reported_counts_at_its_worst_case_for_the_next_one():
    llm = _RecordingLLM(_answer_then_finish(first_usage=None))  # the provider reported nothing
    budget = TurnTokenBudget(total=2_000, max_output=WA.WIRE_MAX_OUTPUT_TOKENS)
    reply = LuminaAgent.chat(_zero_tool_agent(llm, budget=budget), "hi @lumina")
    assert is_error_response(reply) and llm.call_count == 1, "no usage -> worst case -> gate refused"
    assert budget.charged() >= WA.WIRE_MAX_OUTPUT_TOKENS


# ── the streamed final goes through the same boundary ──────────────────────────


def _empty_answer_then_finish(first_usage, gate_usage):
    """No candidate to promote, so the gate's finish streams the final answer."""
    return [
        {"content": "", "termination": TerminationStatus.COMPLETE, "usage": first_usage},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)], "usage": gate_usage},
    ]


def test_a_streamed_final_is_capped_at_the_turns_output_limit():
    llm = _RecordingLLM(_empty_answer_then_finish(_usage(900), _usage(950)))
    budget = TurnTokenBudget(total=WA.TURN_TOKEN_BUDGET, max_output=WA.WIRE_MAX_OUTPUT_TOKENS)
    reply = LuminaAgent.chat(_zero_tool_agent(llm, budget=budget), "hi @lumina")
    assert reply == "final streamed response"
    assert llm.stream_max_tokens_seen == [WA.WIRE_MAX_OUTPUT_TOKENS]


def test_an_agent_without_a_budget_streams_with_the_general_reserve():
    llm = _RecordingLLM(_empty_answer_then_finish(None, None))
    LuminaAgent.chat(_zero_tool_agent(llm, budget=None), "hello")
    assert llm.stream_max_tokens_seen == [25_000]


def test_a_streamed_final_the_allowance_cannot_support_is_refused_and_not_sent():
    llm = _RecordingLLM(_empty_answer_then_finish(_usage(1_000), _usage(1_100)))
    budget = TurnTokenBudget(total=2_400, max_output=WA.WIRE_MAX_OUTPUT_TOKENS)
    reply = LuminaAgent.chat(_zero_tool_agent(llm, budget=budget), "hi @lumina")
    assert is_error_response(reply) and reply.startswith("[Stream error:") and "token allowance" in reply
    assert llm.call_count == 2 and llm.stream_calls == 0


# ── the budget itself ──────────────────────────────────────────────────────────


def test_authorize_grants_the_lower_of_the_output_cap_and_the_calls_own_limit():
    budget = TurnTokenBudget(total=50_000, max_output=1_024)
    assert budget.authorize([{"role": "user", "content": "x"}], None, 25_000) == 1_024
    assert budget.authorize([{"role": "user", "content": "x"}], None, 300) == 300


def test_authorize_refuses_a_call_whose_input_plus_output_cap_exceeds_what_is_left():
    budget = TurnTokenBudget(total=3_300, max_output=1_024)
    big = [{"role": "user", "content": "y" * 6_000}]  # ~2,000 input + 1,024 output per call
    assert budget.authorize(big, None, 25_000) == 1_024
    with pytest.raises(TurnBudgetRefused):
        budget.authorize(big, None, 25_000)  # the first, unsettled, is charged at its worst case


def test_settle_replaces_the_worst_case_with_the_reported_total_and_ignores_junk():
    budget = TurnTokenBudget(total=50_000, max_output=1_024)
    budget.authorize([{"role": "user", "content": "x"}], None, 25_000)
    worst = budget.charged()
    assert worst > 1_024
    for junk in (None, {}, {"total_tokens": -1}, {"total_tokens": True}, {"total_tokens": "5"}, "no"):
        budget.settle(junk)
    assert budget.charged() == worst, "malformed usage settles nothing"
    budget.settle({"total_tokens": 321})
    assert (budget.spent, budget.charged()) == (321, 321)
    budget.settle({"total_tokens": 999})  # a second report for a call already settled
    assert budget.charged() == 321


def test_the_input_estimate_counts_bytes_so_multibyte_text_is_not_undercounted():
    budget = TurnTokenBudget(total=1, max_output=1)
    ascii_est = budget._estimate_input([{"role": "user", "content": "a" * 3_000}], None)
    cjk_est = budget._estimate_input([{"role": "user", "content": "漢" * 3_000}], None)
    assert cjk_est > 2.5 * ascii_est


# ── admission and the allowance agree: the real bounded worst case ─────────────


def _msg(role, chars):
    return {"role": role, "content": "z" * chars}


def _wire_budget(total=None):
    return TurnTokenBudget(total=total or WA.TURN_TOKEN_BUDGET, max_output=WA.WIRE_MAX_OUTPUT_TOKENS,
                           followup_tokens=WA.GATE_OVERHEAD_TOKENS)


def test_a_maximum_size_prompt_on_the_shipped_system_prompt_fits_both_calls_and_no_third():
    """The system prompt measured on the approved live smoke was ~6,200 characters;
    7,000 here. A full-size 12,000-character prompt, the work call's maximum output
    carried to the gate, the gate's real instruction, cue and control schemas, and
    each call billed at its worst case: both calls fit TURN_TOKEN_BUDGET, a third
    (a corrective retry) does not."""
    cap = WA.WIRE_MAX_OUTPUT_TOKENS
    system = _msg("system", 7_000)
    prompt = _msg("user", WA.DEFAULT_CONTEXT_CHARS)
    gate = [system, prompt, _msg("system", len(_GATE_CANDIDATE_REVIEW_INSTRUCTION)),
            _msg("assistant", cap * WA.CHARS_PER_TOKEN),
            {"role": "user", "content": GATE_DECISION_CUE}]
    budget = _wire_budget()
    assert budget.authorize([system, prompt], None, 25_000) == cap
    assert budget.authorize(gate, _CONTROL_GATE_SCHEMAS, 25_000) == cap
    with pytest.raises(TurnBudgetRefused):
        budget.authorize(gate, _CONTROL_GATE_SCHEMAS, 25_000)


def test_the_first_call_must_leave_room_for_the_gate_or_nothing_is_sent():
    """A prompt whose first call would fit but whose follow-up cannot: the turn is not
    started at all, instead of spending the first call and then failing at the gate."""
    llm = _RecordingLLM(_answer_then_finish(_usage(900), _usage(950)))
    budget = _wire_budget()
    # ~9,000 estimated input tokens: call 1 alone (10,024) fits, call 1 + its gate (22,072) does not
    agent = _zero_tool_agent(llm, budget=budget, system_chars=27_000)
    reply = LuminaAgent.chat(agent, "hi @lumina")
    assert is_error_response(reply) and "token allowance" in reply
    assert llm.call_count == 0 and budget.charged() == 0


def test_a_multibyte_prompt_is_estimated_per_byte_so_it_is_refused_up_front():
    """3,000 CJK characters are 9,000 bytes and can cost about one token per byte."""
    llm = _RecordingLLM(_answer_then_finish())
    budget = _wire_budget()
    agent = _zero_tool_agent(llm, budget=budget, system_chars=1)
    agent.ctx.build_messages = lambda tool_budget=0, chat_id=None: [
        {"role": "system", "content": "s"}, {"role": "user", "content": "漢" * 7_000}]
    reply = LuminaAgent.chat(agent, "hi")
    assert is_error_response(reply) and llm.call_count == 0


def test_the_input_estimate_counts_non_ascii_bytes_one_token_each():
    budget = TurnTokenBudget(total=1, max_output=1)
    ascii_est = budget._estimate_input([{"role": "user", "content": "a" * 3_000}], None)
    cjk_est = budget._estimate_input([{"role": "user", "content": "漢" * 1_000}], None)
    assert 900 < ascii_est < 1_200
    assert cjk_est >= 3_000, "1,000 three-byte characters are at least 3,000 estimated tokens"


def test_a_gate_that_keeps_asking_for_more_work_is_stopped_by_the_allowance_not_only_by_the_loop_bound():
    """Each call really bills 9,000 tokens. The agent's own ping-pong bound would allow four
    calls (36,000 tokens); the allowance stops the turn at two."""
    turns = []
    for _ in range(8):
        turns.append({"content": "", "termination": TerminationStatus.COMPLETE, "usage": _usage(9_000)})
        turns.append({"tool_calls": [_tc(CONTINUE_TOOL_WORK_NAME)], "usage": _usage(9_000)})
    llm = _RecordingLLM(turns)
    budget = _wire_budget()
    reply = LuminaAgent.chat(_zero_tool_agent(llm, budget=budget, system_chars=9_000), "loop")
    assert is_error_response(reply) and "token allowance" in reply
    assert llm.call_count == 2
    assert budget.charged() == 18_000


# ── fail closed: no hard output cap, no autonomous turn ────────────────────────


def test_a_backend_that_does_not_enforce_an_output_limit_is_refused_not_left_uncapped():
    llm = _RecordingLLM(_answer_then_finish())
    llm.enforces_output_token_limit = False
    budget = _wire_budget()
    reply = LuminaAgent.chat(_zero_tool_agent(llm, budget=budget), "hi @lumina")
    assert is_error_response(reply) and "does not enforce an output token limit" in reply
    assert llm.call_count == 0


def test_the_chatgpt_plan_backend_declares_that_it_does_not_enforce_an_output_limit():
    from core.backends.openai_chatgpt_plan import ChatGPTPlanBackend
    from core.backends.openrouter import OpenRouterBackend
    assert ChatGPTPlanBackend.enforces_output_token_limit is False
    assert getattr(OpenRouterBackend, "enforces_output_token_limit", True) is True


# ── an unsent call is never charged; a refused-after-admission call frees its lease ─


def test_a_call_refused_by_suspension_is_never_charged_against_the_allowance(monkeypatch):
    llm = _RecordingLLM(_answer_then_finish())
    budget = _wire_budget()
    monkeypatch.setattr("core.agent._suspension_provider_admission",
                        lambda *a, **k: (None, "[Lumina: this channel is paused]"))
    reply = LuminaAgent.chat(_zero_tool_agent(llm, budget=budget), "hi @lumina")
    assert "paused" in reply and llm.call_count == 0
    assert budget.charged() == 0


def test_a_call_the_allowance_refuses_releases_the_suspension_lease_it_was_admitted_under(monkeypatch):
    llm = _RecordingLLM(_answer_then_finish())
    released = []
    monkeypatch.setattr("core.agent._suspension_provider_admission",
                        lambda *a, **k: (lambda: released.append(1), None))
    budget = _wire_budget(total=1_500)
    reply = LuminaAgent.chat(_zero_tool_agent(llm, budget=budget, system_chars=3_000), "hi @lumina")
    assert is_error_response(reply) and llm.call_count == 0
    assert released == [1], "the admitted lease is released when the allowance refuses the call"


# ── an answer the cap cuts off is a failed turn, never a truncated reply ───────


def test_an_answer_cut_off_at_the_cap_is_not_delivered_as_a_reply():
    cut = {"content": "I was saying that", "termination": TerminationStatus.INCOMPLETE, "usage": _usage(1_400)}
    llm = _RecordingLLM([cut, dict(cut), dict(cut), dict(cut)])
    budget = _wire_budget()
    reply = LuminaAgent.chat(_zero_tool_agent(llm, budget=budget), "hi @lumina")
    assert is_error_response(reply), reply
    assert reply != "I was saying that"
    assert llm.call_count <= 3


# ── budget-less agents and fakes are untouched ─────────────────────────────────


def test_a_magicmock_agent_passes_through_the_boundary_helpers_unchanged():
    agent = mock.MagicMock()
    agent.llm.chat.return_value = {"ok": True}
    kwargs = {"messages": [{"role": "user", "content": "x"}], "tools": [], "max_tokens": 25_000}
    response, err = _provider_chat_or_error(agent, kwargs, None, set())
    assert err is None and response == {"ok": True}
    assert agent.llm.chat.call_args.kwargs["max_tokens"] == 25_000
    _accumulate_provider_telemetry(agent, {"usage": {"total_tokens": 5}}, None, "work")  # must not raise
