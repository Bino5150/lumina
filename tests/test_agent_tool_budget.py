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


@pytest.mark.parametrize("budget", [11000, 13500])
def test_prints_new_ceiling_warning_without_altering_schemas(budget, monkeypatch, capsys):
    monkeypatch.setattr(config, "TOOL_BUDGET_TOKENS", budget)
    monkeypatch.setattr("core.agent.build_skills_block", lambda query: "")
    schemas = [
        {"type": "function", "function": {"name": "tool_a"}},
        {"type": "function", "function": {"name": "tool_b"}},
    ]
    fake_self = _fake_self(schema_tokens=budget + 1, schemas=schemas)

    LuminaAgent.chat(fake_self, "hello")

    captured = capsys.readouterr()
    assert captured.out.count("[TOOLS] schema budget exceeded") == 1
    assert f"{budget + 1} tokens > {budget} configured ceiling" in captured.out
    assert fake_self.llm.tools_seen == [schemas]
    assert fake_self.llm.tools_seen[0] is schemas


@pytest.mark.parametrize("budget", [11000, 13500])
@pytest.mark.parametrize("offset", [-1, 0])
def test_no_warning_at_or_below_new_ceiling(budget, offset, monkeypatch, capsys):
    monkeypatch.setattr(config, "TOOL_BUDGET_TOKENS", budget)
    monkeypatch.setattr("core.agent.build_skills_block", lambda query: "")
    fake_self = _fake_self(schema_tokens=budget + offset)

    LuminaAgent.chat(fake_self, "hello")

    captured = capsys.readouterr()
    assert "[TOOLS]" not in captured.out


def test_warning_defaults_match_and_preserve_explicit_overrides():
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for filename in ("config.py", "config.example.py"):
        tree = ast.parse((root / filename).read_text(encoding="utf-8"))
        assignment = next(
            node for node in tree.body if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "TOOL_BUDGET_TOKENS"
                    for target in node.targets)
        )
        call = assignment.value
        assert isinstance(call, ast.Call) and len(call.args) == 2 and not call.keywords
        assert isinstance(call.func, ast.Attribute) and call.func.attr == "get"
        assert isinstance(call.func.value, ast.Name) and call.func.value.id == "_p"
        key, default = (ast.literal_eval(arg) for arg in call.args)
        assert key == "tool_budget_tokens"
        assert default == 13500
        assert {}.get(key, default) == 13500
        for override in (9000, 11000, 15000):
            assert {key: override}.get(key, default) == override
