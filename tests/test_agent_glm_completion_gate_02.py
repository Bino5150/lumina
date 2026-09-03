"""
AGENT-GLM-COMPLETION-GATE-02 -- gate instruction clarifies sanitized
attachment placeholders when (and only when) the conversation actually
has attachment history.

Root cause (source-vetted via a real live OpenRouter/GLM shakedown against
z-ai/glm-5.3-flash, wire payloads captured, no mocking -- see the campaign
report for the full trial matrix): AGENT-GLM-COMPLETION-GATE-01 already
fixed the transport gap (the gate's own request no longer trips
core/backends/lmstudio.py's has_vision guard, so tools/tool_choice=required
do reach the wire even in a chat with image history). But the gate's own
instruction never explained *why* the conversation above it suddenly shows
"[image attachment N]" placeholders instead of real images -- and the
user's original instruction referencing those attachments (e.g. "Describe
each attached image separately...") survives sanitization verbatim right
next to the placeholder. Live-observed: GLM sometimes resolves that
apparent contradiction by narrating it in prose ("I have to be straight
with you here... no actual image content came through on my end") instead
of picking a control tool at all -- violating tool_choice=required, not
because the wire translation is broken, but because the model was never
told the placeholder is expected bookkeeping for this specific check.

Fix: _run_tool_work_control_gate() (core/agent.py) now appends one extra
clarifying sentence to its ephemeral instruction, but ONLY when
agent.ctx.history contains at least one attachment (list-content) message
-- the plain-text gate path (the overwhelming majority of real turns,
already reliable) gets byte-identical instruction text to before this fix.
"""
import types

import pytest

from core.agent import (
    LuminaAgent, FINISH_TOOL_WORK_NAME, CONTINUE_TOOL_WORK_NAME,
    _run_tool_work_control_gate,
)
from core.backends.base import TerminationStatus, ToolChoiceMode
from core.backends.openrouter import OpenRouterBackend
from core.context import ContextManager


@pytest.fixture(autouse=True)
def _no_skill_injection(monkeypatch):
    monkeypatch.setattr("core.agent.build_skills_block", lambda user_input: "")


def _tc(name, call_id=None):
    return {"id": call_id or name, "type": "function",
            "function": {"name": name, "arguments": "{}"}}


_IMAGE_A = {"type": "image_url", "image_url": {"url": "data:image/png;base64,REDCIRCLEDATA"}}
_IMAGE_B = {"type": "image_url", "image_url": {"url": "data:image/png;base64,BLUESQUAREDATA"}}
_QUESTION = {"type": "text", "text": "Describe each attached image separately, then state the most obvious difference between them."}
_VISION_CONTENT = [_IMAGE_A, _IMAGE_B, _QUESTION]


class _ScriptedLLM:
    display_name = "FakeProvider"
    name = "fake"
    supports_required_tool_choice = True

    def __init__(self, turns):
        self.turns = list(turns)
        self.call_count = 0
        self.messages_seen = []

    def get_model(self):
        return "fake-model"

    def chat(self, messages, tools=None, max_tokens=None, reasoning_effort=None,
             tool_choice_mode=None):
        self.messages_seen.append(messages)
        idx = self.call_count
        self.call_count += 1
        turn = self.turns[idx]
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


def _agent_for_gate_call(llm, seed_content):
    """A minimal duck-typed agent with a REAL ContextManager already
    seeded with one user turn -- enough to call _run_tool_work_control_gate()
    directly (it only reads agent.ctx.history/build_messages(), never the
    full LuminaAgent.chat() loop)."""
    ctx = ContextManager(owner=False)
    ctx.add_user(seed_content)
    return types.SimpleNamespace(
        llm=llm, ctx=ctx,
        on_think_start=lambda step: None, on_think_token=lambda tok: None,
        on_think_end=lambda: None, on_commentary=lambda text: None,
    )


def _gate_instruction_sent(llm) -> str:
    messages = llm.messages_seen[-1]
    sysmsg = next(m for m in messages if m.get("role") == "system")
    return sysmsg["content"]


# ── Instruction text: vision history gets the clarifying sentence ──────────

def test_vision_history_gate_instruction_includes_placeholder_clarification():
    llm = _ScriptedLLM([{"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]}])
    agent = _agent_for_gate_call(llm, _VISION_CONTENT)

    outcome, err = _run_tool_work_control_gate(
        agent, tools_used_this_turn=set(), cancel_event=None,
        reasoning_effort=None, chat_id=None, think_step=[0],
    )

    assert outcome == "finish"
    instruction = _gate_instruction_sent(llm)
    assert "## Tool-work completion gate" in instruction
    assert "attachment" in instruction
    assert "not an error" in instruction
    assert "Do not comment on" in instruction


def test_audio_history_also_gets_the_clarification():
    """The predicate is list-content, not image-specific -- an audio
    attachment (or any other multipart block type) trips the same guard
    core/backends/lmstudio.py's has_vision check does."""
    llm = _ScriptedLLM([{"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]}])
    audio_content = [{"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}},
                      {"type": "text", "text": "what did I say?"}]
    agent = _agent_for_gate_call(llm, audio_content)

    _run_tool_work_control_gate(
        agent, tools_used_this_turn=set(), cancel_event=None,
        reasoning_effort=None, chat_id=None, think_step=[0],
    )

    assert "not an error" in _gate_instruction_sent(llm)


def test_text_only_history_gate_instruction_is_byte_identical_to_pre_fix():
    """The already-reliable plain-text gate path must be completely
    untouched by this fix -- exact string match, not just a substring
    absence check."""
    llm = _ScriptedLLM([{"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]}])
    agent = _agent_for_gate_call(llm, "What's the capital of France?")

    _run_tool_work_control_gate(
        agent, tools_used_this_turn=set(), cancel_event=None,
        reasoning_effort=None, chat_id=None, think_step=[0],
    )

    instruction = _gate_instruction_sent(llm)
    gate_block = instruction[instruction.index("## Tool-work completion gate"):]
    assert gate_block == (
        "## Tool-work completion gate\n"
        "Decide whether additional real tool work is required. "
        "Call continue_tool_work if more tool work is needed. "
        "Call finish_tool_work if tool work is complete. "
        "Choose exactly one."
    )
    assert "attachment" not in instruction


def test_historical_image_then_text_only_turn_still_gets_the_clarification():
    """A turn's OWN content can be plain text while an EARLIER turn in the
    same chat carried an image -- has_vision-style detection scans full
    history, and this fix must match that scope exactly (not just the
    current turn's content) since that's the exact shape a real multi-turn
    vision conversation takes."""
    llm = _ScriptedLLM([{"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]}])
    ctx = ContextManager(owner=False)
    ctx.add_user(_VISION_CONTENT)
    ctx.add_assistant("Image 1 is a red circle, Image 2 is a blue square.")
    ctx.add_user("Thanks -- now separately, what's 17 * 23?")
    agent = types.SimpleNamespace(
        llm=llm, ctx=ctx,
        on_think_start=lambda step: None, on_think_token=lambda tok: None,
        on_think_end=lambda: None, on_commentary=lambda text: None,
    )

    _run_tool_work_control_gate(
        agent, tools_used_this_turn=set(), cancel_event=None,
        reasoning_effort=None, chat_id=None, think_step=[0],
    )

    assert "not an error" in _gate_instruction_sent(llm)


# ── End-to-end through LuminaAgent.chat(): vision, still fixed, still safe ──

def test_end_to_end_finish_still_promotes_candidate_with_clarified_gate():
    candidate_text = "**Image 1:** a red circle...\n**Image 2:** a blue square..."
    llm = _ScriptedLLM([
        {"content": candidate_text, "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    ctx = ContextManager(owner=False)
    calls = {"response_tokens": []}
    registry = types.SimpleNamespace(
        schema_token_estimate=lambda: 0,
        get_schemas=lambda: [{"type": "function", "function": {"name": "search_memory", "description": "", "parameters": {}}}],
        list_enabled=lambda: ["search_memory"],
        all_tool_names=lambda: ["search_memory"],
        call=lambda name, args: "ok",
    )
    fake = types.SimpleNamespace(
        llm=llm, ctx=ctx, registry=registry,
        on_tool_call=lambda name, args: None, on_tool_result=lambda name, result: None,
        on_think_start=lambda step: None, on_think_token=lambda tok: None, on_think_end=lambda: None,
        on_response_token=lambda tok: calls["response_tokens"].append(tok),
        on_commentary=lambda text: None,
        tts=None, _session_tool_calls=0, _skill_nudge_sent=False,
    )
    fake._stream_final = types.MethodType(LuminaAgent._stream_final, fake)
    fake._finalize_completion_candidate = types.MethodType(LuminaAgent._finalize_completion_candidate, fake)

    result = LuminaAgent.chat(fake, _VISION_CONTENT)

    assert result == candidate_text
    gate_instruction = _gate_instruction_sent(llm)
    assert "not an error" in gate_instruction


# ── Real backend wire-level proof (requests.post mocked only) ──────────────

def test_wire_payload_carries_the_clarification_for_a_vision_gate_call(monkeypatch):
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
        def raise_for_status(self): pass
        def json(self):
            return {"choices": [{"message": {"content": "", "tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]}}]}

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
    sysmsg = next(m for m in payload["messages"] if m["role"] == "system")
    assert "not an error" in sysmsg["content"]
    assert "Do not comment on" in sysmsg["content"]
    # Still never any real payload data reaching the wire.
    assert "REDCIRCLEDATA" not in str(payload)
    assert "BLUESQUAREDATA" not in str(payload)


def test_wire_payload_for_text_only_gate_call_has_no_clarification(monkeypatch):
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
        def raise_for_status(self): pass
        def json(self):
            return {"choices": [{"message": {"content": "", "tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]}}]}

    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["payload"] = json
        return _FakeResp()

    monkeypatch.setattr(requests, "post", _fake_post)

    ctx = ContextManager(owner=False)
    ctx.add_user("what's 2+2?")
    agent = types.SimpleNamespace(
        ctx=ctx, llm=backend,
        on_think_start=lambda step: None, on_think_token=lambda tok: None,
        on_think_end=lambda: None, on_commentary=lambda text: None,
    )

    _run_tool_work_control_gate(
        agent, tools_used_this_turn=set(), cancel_event=None,
        reasoning_effort=None, chat_id=None, think_step=[0],
    )

    payload = captured["payload"]
    sysmsg = next(m for m in payload["messages"] if m["role"] == "system")
    assert "not an error" not in sysmsg["content"]
    assert sysmsg["content"].rstrip().endswith("Choose exactly one.")
