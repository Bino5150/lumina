"""
AGENT-GLM-COMPLETION-GATE-01 -- gate-only repair.

Root cause (source-vetted, confirmed via a network-free local reproduction
before this fix was written): core/backends/lmstudio.py's has_vision guard
(`any(isinstance(m.get("content"), list) for m in messages)`) silently drops
`tools`/`tool_choice` from ANY request the moment a single multipart message
exists anywhere in the conversation being sent -- not just the WORK round's
own vision-bearing turn (existing, deliberately UNCHANGED policy --
VISION-TOOL-INTEROP-01, its own separate ticket), but also the completion
gate's own tiny, unrelated two-schema REQUIRED call, every time it's asked
in a chat that has ever contained an image. GLM was never non-compliant with
a forced tool_choice -- it was never offered a tool to call in the first
place, because has_vision scans the ENTIRE accumulated history, not just the
current turn, so it stays True for every later request in that chat.

Fix: _run_tool_work_control_gate() (core/agent.py) now builds a
request-local, sanitized copy of `messages` via the new
_sanitize_messages_for_gate()/_gate_safe_content() before calling the
backend -- multipart content collapses to a single string (real text
preserved verbatim and in order; every non-text block replaced by a short,
deterministic placeholder naming its position and declared type, never its
payload), so the gate's own request never trips has_vision. Nothing else
changes: core/backends/lmstudio.py is untouched (no new "force_tools"
bypass parameter anywhere), the WORK round's own messages are still built
and sent completely unmodified (still vision-blind per existing policy),
and ContextManager.history / the original `messages` objects returned by
build_messages() are never mutated -- build_messages() reuses the SAME
dict/list objects that live in ctx.history for every non-"tool" role, so
this matters for real, not just as a defensive habit.
"""
import types

import pytest

from core.agent import (
    LuminaAgent, FINISH_TOOL_WORK_NAME, CONTINUE_TOOL_WORK_NAME,
    _gate_safe_content, _sanitize_messages_for_gate, _run_tool_work_control_gate,
)
from core.backends.base import TerminationStatus, ToolChoiceMode
from core.backends.openrouter import OpenRouterBackend
from core.context import ContextManager


@pytest.fixture(autouse=True)
def _no_skill_injection(monkeypatch):
    """Same guard test_agent_continuation_control_gate.py uses -- without
    this, a real skill matched against this file's literal prompt text can
    add an extra, unrelated ephemeral push with nothing to do with the
    gate behavior under test."""
    monkeypatch.setattr("core.agent.build_skills_block", lambda user_input: "")


def _tc(name, call_id=None):
    return {"id": call_id or name, "type": "function",
            "function": {"name": name, "arguments": "{}"}}


_IMAGE_A = {"type": "image_url", "image_url": {"url": "data:image/png;base64,REDCIRCLEDATA"}}
_IMAGE_B = {"type": "image_url", "image_url": {"url": "data:image/png;base64,BLUESQUAREDATA"}}
_QUESTION = {"type": "text", "text": "Describe each attached image separately, then state the most obvious difference between them."}
_VISION_CONTENT = [_IMAGE_A, _IMAGE_B, _QUESTION]


# ── _gate_safe_content() -- pure unit tests ──────────────────────────────────

def test_plain_string_content_returned_unchanged():
    assert _gate_safe_content("hello") == "hello"


def test_none_content_returned_unchanged():
    assert _gate_safe_content(None) is None


def test_single_image_block_becomes_one_deterministic_placeholder():
    content = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]
    result = _gate_safe_content(content)
    assert isinstance(result, str)
    assert result == "[image attachment 1]"
    assert "AAAA" not in result
    assert "data:" not in result
    assert "base64" not in result


def test_multiple_image_blocks_produce_one_placeholder_per_block_in_order():
    content = [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,BBBB"}},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,CCCC"}},
        {"type": "text", "text": "describe each one"},
    ]
    result = _gate_safe_content(content)
    assert "AAAA" not in result and "BBBB" not in result and "CCCC" not in result
    assert (result.index("[image attachment 1]") < result.index("[image attachment 2]")
            < result.index("[image attachment 3]"))
    assert result.strip().endswith("describe each one")


def test_text_before_and_after_images_stays_in_order():
    content = [
        {"type": "text", "text": "first image is A, second is B"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,BBB"}},
    ]
    result = _gate_safe_content(content)
    lines = result.split("\n")
    assert lines[0] == "first image is A, second is B"


def test_empty_caption_text_block_is_skipped_not_sent_as_empty_line():
    content = [
        {"type": "text", "text": ""},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
    ]
    result = _gate_safe_content(content)
    assert result.strip() == result  # no leading blank line from the empty caption


def test_sanitization_is_deterministic_across_repeated_calls():
    content = [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        {"type": "text", "text": "hi"},
    ]
    assert _gate_safe_content(content) == _gate_safe_content(content)


def test_does_not_mutate_the_original_content_list_or_blocks():
    block = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    content = [block]
    _gate_safe_content(content)
    assert content == [block]
    assert content[0] is block
    assert block["image_url"]["url"] == "data:image/png;base64,AAAA"


def test_no_data_url_or_base64_payload_survives_sanitization_for_a_multi_image_batch():
    content = [
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{'X' * 500}"}}
        for _ in range(5)
    ] + [{"type": "text", "text": "compare all five"}]
    result = _gate_safe_content(content)
    assert isinstance(result, str)
    assert "data:image/png;base64," not in result
    assert "X" * 500 not in result
    assert "compare all five" in result


def test_non_image_non_text_block_also_gets_a_deterministic_placeholder():
    """has_vision is content-type-blind (any list content trips it) -- the
    sanitizer mirrors that: an audio block hits the identical guard and
    must be handled the same way, not just image_url specifically."""
    content = [{"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}}]
    result = _gate_safe_content(content)
    assert isinstance(result, str)
    assert result == "[audio attachment 1]"
    assert "AAAA" not in result


def test_unrecognized_block_type_falls_back_to_its_own_declared_type_as_the_label():
    content = [{"type": "widget_blob", "widget_blob": {"data": "AAAA"}}]
    result = _gate_safe_content(content)
    assert result == "[widget_blob attachment 1]"
    assert "AAAA" not in result


# ── _sanitize_messages_for_gate() -- role/order/non-mutation ────────────────

def test_sanitize_messages_preserves_roles_and_ordering_across_mixed_message_list():
    messages = [
        {"role": "system", "content": "sys prompt"},
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
            {"type": "text", "text": "what is this"},
        ]},
        {"role": "assistant", "content": "a previous reply", "tool_calls": [_tc("search_memory", "call-1")]},
        {"role": "tool", "tool_call_id": "call-1", "name": "search_memory", "content": "result text"},
    ]

    sanitized = _sanitize_messages_for_gate(messages)

    assert [m["role"] for m in sanitized] == ["system", "user", "assistant", "tool"]
    assert sanitized[0]["content"] == "sys prompt"
    assert isinstance(sanitized[1]["content"], str)
    assert "what is this" in sanitized[1]["content"]
    assert "data:image/png;base64,AAA" not in sanitized[1]["content"]
    assert sanitized[2]["content"] == "a previous reply"
    assert sanitized[2]["tool_calls"] == [_tc("search_memory", "call-1")]
    assert sanitized[3]["tool_call_id"] == "call-1"
    assert sanitized[3]["content"] == "result text"

    # Originals completely untouched.
    assert messages[1]["content"][0]["type"] == "image_url"
    assert messages[1]["content"][0]["image_url"]["url"] == "data:image/png;base64,AAA"
    assert messages[2]["tool_calls"] == [_tc("search_memory", "call-1")]


def test_sanitize_messages_returns_new_list_and_new_dicts_never_the_originals():
    messages = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    sanitized = _sanitize_messages_for_gate(messages)
    assert sanitized is not messages
    assert sanitized[0] is not messages[0]


# ── Non-mutation of a REAL ContextManager's live/durable history ────────────

def test_gate_sanitization_never_mutates_a_real_contextmanagers_history():
    """build_messages() (core/context.py) reuses the SAME dict/list objects
    that live in ContextManager.history for every non-"tool" role -- this
    is not a hypothetical risk, it's the actual shared-reference shape the
    fix has to respect. Verified against the real ContextManager, not a
    fake."""
    ctx = ContextManager(owner=False)
    ctx.add_user(list(_VISION_CONTENT))
    ctx.add_assistant("A red circle and a blue square.")

    history_before_ids = [id(m) for m in ctx.history]
    content_before_ids = [id(m.get("content")) for m in ctx.history]
    import copy
    deep_snapshot = copy.deepcopy(ctx.history)

    messages = ctx.build_messages(tool_budget=0)
    sanitized = _sanitize_messages_for_gate(messages)

    # Same objects, same order, same values -- nothing rewritten in place.
    assert [id(m) for m in ctx.history] == history_before_ids
    assert [id(m.get("content")) for m in ctx.history] == content_before_ids
    assert ctx.history == deep_snapshot

    user_entry = next(m for m in ctx.history if m.get("role") == "user")
    assert isinstance(user_entry["content"], list)
    assert user_entry["content"] == _VISION_CONTENT

    # The sanitized copy really is different -- proves this isn't a no-op
    # silently matching by coincidence.
    sanitized_user = next(m for m in sanitized if m.get("role") == "user")
    assert isinstance(sanitized_user["content"], str)
    assert "REDCIRCLEDATA" not in sanitized_user["content"]
    assert "BLUESQUAREDATA" not in sanitized_user["content"]


# ── Scripted end-to-end: gate-only, WORK round untouched ────────────────────

class _ScriptedLLM:
    """Same scripted-turn shape as test_agent_continuation_control_gate.py's
    _ScriptedLLM, extended to record the raw `messages` argument per call so
    a test can inspect exactly what reached each round."""
    display_name = "FakeProvider"
    name = "fake"
    supports_required_tool_choice = True

    def __init__(self, turns):
        self.turns = list(turns)
        self.call_count = 0
        self.tools_seen = []
        self.tool_choice_modes_seen = []
        self.messages_seen = []

    def get_model(self):
        return "fake-model"

    def chat(self, messages, tools=None, max_tokens=None, reasoning_effort=None,
             tool_choice_mode=None):
        self.messages_seen.append(messages)
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

    def is_tool_call(self, message):
        return bool(message.get("tool_calls"))

    def get_tool_calls(self, message):
        return message.get("tool_calls", [])

    def parse_tool_call(self, tc):
        return tc["function"]["name"], {}

    def chat_stream(self, messages, max_tokens=None, reasoning_effort=None):
        yield "final streamed response"


def _fake_agent_with_real_context(llm):
    """Real ContextManager for ctx (so build_messages()'s actual reference-
    sharing behavior is exercised), everything else a minimal duck-typed
    stand-in -- same convention test_agent_continuation_control_gate.py
    already uses."""
    ctx = ContextManager(owner=False)
    calls = {"response_tokens": [], "commentary": []}
    registry = types.SimpleNamespace(
        schema_token_estimate=lambda: 0,
        get_schemas=lambda: [
            {"type": "function", "function": {"name": "search_memory", "description": "", "parameters": {}}},
        ],
        list_enabled=lambda: ["search_memory"],
        all_tool_names=lambda: ["search_memory"],
        call=lambda name, args: "ok",
    )
    ns = types.SimpleNamespace(
        llm=llm, ctx=ctx, registry=registry,
        on_tool_call=lambda name, args: None,
        on_tool_result=lambda name, result: None,
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
    return ns, calls


def test_work_round_messages_are_never_sanitized_vision_policy_unchanged():
    """VISION-TOOL-INTEROP-01 is explicitly out of scope for this repair --
    the WORK round must keep seeing the real image content exactly as
    before, proving this fix touches only the gate's own request."""
    llm = _ScriptedLLM([
        {"content": "**Image 1:** a red circle...\n**Image 2:** a blue square...",
         "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent_with_real_context(llm)

    LuminaAgent.chat(fake, _VISION_CONTENT)

    work_round_messages = llm.messages_seen[0]
    user_msg = next(m for m in work_round_messages if m.get("role") == "user")
    assert isinstance(user_msg["content"], list)
    assert user_msg["content"] == _VISION_CONTENT


def test_gate_round_messages_contain_no_image_url_or_data_url_payload():
    llm = _ScriptedLLM([
        {"content": "**Image 1:** a red circle...\n**Image 2:** a blue square...",
         "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent_with_real_context(llm)

    LuminaAgent.chat(fake, _VISION_CONTENT)

    gate_round_messages = llm.messages_seen[1]
    for m in gate_round_messages:
        content = m.get("content")
        assert not isinstance(content, list)
        if isinstance(content, str):
            assert "REDCIRCLEDATA" not in content
            assert "BLUESQUAREDATA" not in content
            assert "data:image" not in content
            assert "base64," not in content


def test_gate_still_requests_both_control_schemas_and_required_with_vision_history():
    llm = _ScriptedLLM([
        {"content": "**Image 1:** a red circle...\n**Image 2:** a blue square...",
         "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent_with_real_context(llm)

    LuminaAgent.chat(fake, _VISION_CONTENT)

    assert set(llm.tools_seen[1]) == {CONTINUE_TOOL_WORK_NAME, FINISH_TOOL_WORK_NAME}
    assert llm.tool_choice_modes_seen[1] == ToolChoiceMode.REQUIRED


def test_finish_tool_work_promotes_the_held_candidate_even_with_vision_history():
    """AGENT-WORK-COMPLETE-DISCARD-01: once the gate confirms finish, a
    preserved candidate IS the final answer -- delivered directly, never
    re-asked of the provider. This is the exact mechanism that recovers
    GLM's real image description once the gate can actually reach
    "finish" at all (previously impossible: the gate never even got its
    own tools, so it could never confirm anything)."""
    candidate_text = "**Image 1:** a red circle...\n**Image 2:** a blue square..."
    llm = _ScriptedLLM([
        {"content": candidate_text, "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent_with_real_context(llm)

    result = LuminaAgent.chat(fake, _VISION_CONTENT)

    assert result == candidate_text
    # Delivered in small chunks (_deliver_held_text), but reassembles to
    # exactly the candidate -- nothing lost, nothing re-generated.
    assert "".join(calls["response_tokens"]) == candidate_text


def test_continue_tool_work_does_not_prematurely_deliver_the_candidate_with_vision_history():
    llm = _ScriptedLLM([
        {"content": "partial thought", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(CONTINUE_TOOL_WORK_NAME)]},
        {"tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent_with_real_context(llm)

    result = LuminaAgent.chat(fake, _VISION_CONTENT)

    assert result == "final streamed response"
    assert calls["response_tokens"] == [result]  # "partial thought" never delivered early


def test_malformed_gate_with_vision_history_promotes_the_real_candidate_it_never_saw():
    """AGENT-COMPLETION-SENTINEL-RECOVERY-01 -- supersedes this test's
    pre-existing assumption (from before candidate-recovery existed for
    this branch) that a malformed gate response under vision history must
    always fall back to the opaque sentinel. The WORK round that produced
    the "red circle"/"blue square" description saw the REAL image bytes
    (unsanitized -- VISION-TOOL-INTEROP-01 policy, unchanged) and returned
    a complete, non-truncated answer with zero tool calls: an independent,
    trustworthy completion_candidate. The gate's OWN confusion here is
    caused by _sanitize_messages_for_gate()'s placeholder text (AGENT-GLM-
    COMPLETION-GATE-02's documented failure mode -- the model doubts it
    can "see" images it already described), not by anything wrong with
    the candidate, so promoting that already-correct candidate is now the
    right outcome instead of discarding it. What must still never happen:
    the gate's OWN malformed prose ("still deciding") leaking into the
    result -- see the paired no-candidate variant below for the genuinely-
    nothing-to-recover case, which this repair leaves untouched."""
    llm = _ScriptedLLM([
        {"content": "**Image 1:** a red circle...\n**Image 2:** a blue square...",
         "termination": TerminationStatus.COMPLETE},
        {"content": "still deciding", "termination": TerminationStatus.COMPLETE},
        {"content": "still deciding again", "termination": TerminationStatus.COMPLETE},
    ])
    fake, calls = _fake_agent_with_real_context(llm)

    result = LuminaAgent.chat(fake, _VISION_CONTENT)

    assert result == "**Image 1:** a red circle...\n**Image 2:** a blue square..."
    assert "still deciding" not in result
    assert not result.startswith("[Lumina:")


def test_malformed_gate_with_vision_history_and_no_candidate_still_returns_sentinel():
    """The genuinely-nothing-to-recover case, unaffected by AGENT-
    COMPLETION-SENTINEL-RECOVERY-01: a blank-content WORK round forms no
    candidate at all (see _chat_impl()'s candidate-creation site -- empty
    content never becomes a completion_candidate), so a malformed gate
    under vision history still falls back to the truthful, unchanged
    sentinel. This repair only recovers an answer that already existed;
    it never invents one."""
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"content": "still deciding", "termination": TerminationStatus.COMPLETE},
        {"content": "still deciding again", "termination": TerminationStatus.COMPLETE},
    ])
    fake, calls = _fake_agent_with_real_context(llm)

    result = LuminaAgent.chat(fake, _VISION_CONTENT)

    assert result.startswith("[Lumina:")
    assert "still deciding" not in result


def test_text_only_gate_behavior_is_unaffected():
    """Sanity check that the sanitizer is a true no-op for ordinary
    text-only turns -- the pre-existing full
    test_agent_continuation_control_gate.py suite is the real regression
    coverage for this; this is just a direct confirmation here too."""
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("search_memory")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake, calls = _fake_agent_with_real_context(llm)

    result = LuminaAgent.chat(fake, "find it")

    assert result == "final streamed response"
    gate_messages = llm.messages_seen[2]
    user_msg = next(m for m in gate_messages if m.get("role") == "user")
    assert user_msg["content"] == "find it"


# ── Real backend, wire-level proof (requests.post mocked only) ──────────────

def test_gate_request_reaches_real_backend_wire_with_tools_and_required_choice(monkeypatch):
    """End-to-end through the REAL OpenRouterBackend.chat() (only
    requests.post is mocked, no network) -- proves the fix closes the
    has_vision gap at the actual wire payload, not just that core/agent.py
    intends to send tools."""
    import requests

    backend = OpenRouterBackend.__new__(OpenRouterBackend)
    backend.base_url = "https://openrouter.ai/api/v1"
    backend.headers = {"Content-Type": "application/json", "Authorization": "Bearer test"}
    backend._model = "z-ai/glm-5.3-flash"
    backend._reasoning_cache = {}
    backend._reasoning_cache_ready = False
    backend._vision_tool_cache = {}

    captured = {}

    class _FakeResp:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return {"choices": [{"message": {
                "content": "",
                "tool_calls": [_tc(FINISH_TOOL_WORK_NAME)],
            }}]}

    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["payload"] = json
        return _FakeResp()

    monkeypatch.setattr(requests, "post", _fake_post)

    ctx = ContextManager(owner=False)
    ctx.add_user(list(_VISION_CONTENT))

    agent = types.SimpleNamespace(
        ctx=ctx, llm=backend,
        on_think_start=lambda step: None, on_think_token=lambda tok: None,
        on_think_end=lambda: None, on_commentary=lambda text: None,
    )

    outcome, err = _run_tool_work_control_gate(
        agent, tools_used_this_turn=set(), cancel_event=None,
        reasoning_effort=None, chat_id=None, think_step=[0],
    )

    assert err is None
    assert outcome == "finish"
    payload = captured["payload"]
    assert "tools" in payload
    assert {t["function"]["name"] for t in payload["tools"]} == {CONTINUE_TOOL_WORK_NAME, FINISH_TOOL_WORK_NAME}
    assert payload.get("tool_choice") == "required"
    payload_str = str(payload["messages"])
    assert "REDCIRCLEDATA" not in payload_str
    assert "BLUESQUAREDATA" not in payload_str


def test_work_round_still_loses_product_tools_on_vision_turn_unchanged_policy(monkeypatch):
    """VISION-TOOL-INTEROP-01 is explicitly its own, separate, unfixed
    ticket -- confirms this repair does not accidentally also change (or
    otherwise touch) the WORK round's pre-existing has_vision-strips-
    product-tools behavior. core/backends/lmstudio.py is untouched by this
    repair; this is the direct proof."""
    import requests

    backend = OpenRouterBackend.__new__(OpenRouterBackend)
    backend.base_url = "https://openrouter.ai/api/v1"
    backend.headers = {"Content-Type": "application/json", "Authorization": "Bearer test"}
    backend._model = "z-ai/glm-5.3-flash"
    backend._reasoning_cache = {}
    backend._reasoning_cache_ready = False
    backend._vision_tool_cache = {}

    captured = {}

    class _FakeResp:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return {"choices": [{"message": {"content": "a description"}}]}

    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["payload"] = json
        return _FakeResp()

    monkeypatch.setattr(requests, "post", _fake_post)

    messages = [{"role": "user", "content": _VISION_CONTENT}]
    product_tools = [{"type": "function", "function": {"name": "read_file", "description": "", "parameters": {}}}]
    backend.chat(messages, tools=product_tools, tool_choice_mode=ToolChoiceMode.AUTO)

    assert "tools" not in captured["payload"]
    assert "tool_choice" not in captured["payload"]
