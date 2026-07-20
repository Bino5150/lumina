"""core/agent.py — MB-03: no visibility into whether tool-schema bloat is
approaching TOOL_BUDGET_TOKENS. LuminaAgent.chat() now prints a "[TOOLS]"
warning once per turn when registry.schema_token_estimate() exceeds
config.TOOL_BUDGET_TOKENS. Warning only — no enforcement, no schema
narrowing.
"""
import types
import config
from core.agent import LuminaAgent


def _fake_self(schema_tokens):
    class FakeLLM:
        def chat(self, messages, tools, max_tokens):
            raise RuntimeError("stop")

    return types.SimpleNamespace(
        ctx=types.SimpleNamespace(
            add_user=lambda *a, **k: None,
            push_ephemeral=lambda *a, **k: None,
            build_messages=lambda **k: [],
        ),
        registry=types.SimpleNamespace(
            schema_token_estimate=lambda: schema_tokens,
            list_enabled=lambda: ["tool_a", "tool_b"],
            get_schemas=lambda: [],
        ),
        llm=FakeLLM(),
    )


def test_prints_warning_when_over_budget(monkeypatch, capsys):
    monkeypatch.setattr(config, "TOOL_BUDGET_TOKENS", 6000)
    monkeypatch.setattr("core.agent.build_skills_block", lambda query: "")
    fake_self = _fake_self(schema_tokens=6001)

    LuminaAgent.chat(fake_self, "hello")

    captured = capsys.readouterr()
    assert "[TOOLS]" in captured.out
    assert "6001" in captured.out


def test_no_warning_when_within_budget(monkeypatch, capsys):
    monkeypatch.setattr(config, "TOOL_BUDGET_TOKENS", 6000)
    monkeypatch.setattr("core.agent.build_skills_block", lambda query: "")
    fake_self = _fake_self(schema_tokens=6000)

    LuminaAgent.chat(fake_self, "hello")

    captured = capsys.readouterr()
    assert "[TOOLS]" not in captured.out
