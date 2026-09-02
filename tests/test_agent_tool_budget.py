"""core/agent.py — MB-03: no visibility into whether tool-schema bloat is
approaching TOOL_BUDGET_TOKENS. LuminaAgent.chat() now prints a "[TOOLS]"
warning once per turn when registry.schema_token_estimate() exceeds
config.TOOL_BUDGET_TOKENS. Warning only — no enforcement, no schema
narrowing.
"""
import types
import pytest
import config
from core.agent import LuminaAgent


def _fake_self(schema_tokens, schemas=None):
    class FakeLLM:
        def __init__(self):
            self.tools_seen = []

        def chat(self, messages, tools, max_tokens, reasoning_effort=None):
            self.tools_seen.append(tools)
            raise RuntimeError("stop")

    llm = FakeLLM()
    return types.SimpleNamespace(
        ctx=types.SimpleNamespace(
            add_user=lambda *a, **k: None,
            push_ephemeral=lambda *a, **k: None,
            build_messages=lambda **k: [],
        ),
        registry=types.SimpleNamespace(
            schema_token_estimate=lambda: schema_tokens,
            list_enabled=lambda: ["tool_a", "tool_b"],
            get_schemas=lambda: schemas if schemas is not None else [],
        ),
        llm=llm,
    )


def test_prints_new_ceiling_warning_without_altering_schemas(monkeypatch, capsys):
    monkeypatch.setattr("core.agent.build_skills_block", lambda query: "")
    schemas = [
        {"type": "function", "function": {"name": "tool_a"}},
        {"type": "function", "function": {"name": "tool_b"}},
    ]
    fake_self = _fake_self(schema_tokens=11001, schemas=schemas)

    LuminaAgent.chat(fake_self, "hello")

    captured = capsys.readouterr()
    assert captured.out.count("[TOOLS] schema budget exceeded") == 1
    assert "11001 tokens > 11000 configured ceiling" in captured.out
    assert fake_self.llm.tools_seen == [schemas]
    assert fake_self.llm.tools_seen[0] is schemas


@pytest.mark.parametrize("schema_tokens", [10501, 11000])
def test_no_warning_at_or_below_new_ceiling(schema_tokens, monkeypatch, capsys):
    monkeypatch.setattr("core.agent.build_skills_block", lambda query: "")
    fake_self = _fake_self(schema_tokens=schema_tokens)

    LuminaAgent.chat(fake_self, "hello")

    captured = capsys.readouterr()
    assert "[TOOLS]" not in captured.out
