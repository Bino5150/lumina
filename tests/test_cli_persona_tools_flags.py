"""main.py -- MB-22: CLI --persona / --tools flags.

Design note this closes out (Fresh-Eyes Audit, locked): CLI runs as
owner=True, channel_id='cli-local', per-invocation -- and the actual
question this item raised (asked directly: "add tool profiles to the CLI
along with persona, or just let it inherit whatever the persona
specifies?") landed on: --persona alone drives it by default (inherits the
persona's own tools_profile via apply_persona(), same as every other call
site -- the GUI's persona sidebar included), plus an optional --tools
override for a single run, mirroring the existing `backend: str = None`
"None preserves default" pattern on LuminaAgent.__init__.

apply_cli_persona_and_tools() is exercised directly against a lightweight
fake agent rather than a real LuminaAgent -- constructing a real one pulls
in a live LLM backend + sqlite DB init, which is the wrong thing for a unit
test of "did we look up the right name and call the right function."
build_arg_parser() is exercised via parse_args() with explicit argv lists,
independent of sys.argv / interactive input().
"""
import pytest
from main import apply_cli_persona_and_tools, build_arg_parser


class _FakeRegistry:
    def __init__(self):
        self.applied_profile = None
        self.applied_owner = None


class _FakeAgent:
    """Stands in for LuminaAgent: only the surface apply_cli_persona_and_tools
    actually touches (registry, owner, apply_persona())."""
    def __init__(self, owner=True):
        self.owner = owner
        self.registry = _FakeRegistry()
        self.applied_persona = None

    def apply_persona(self, persona):
        self.applied_persona = persona


# ── apply_cli_persona_and_tools() ──────────────────────────────────────────

def test_no_flags_is_a_no_op():
    agent = _FakeAgent()
    messages = apply_cli_persona_and_tools(agent, persona_name=None, tools_override=None)
    assert messages == []
    assert agent.applied_persona is None
    assert agent.registry.applied_profile is None


def test_persona_flag_applies_persona_and_inherits_its_tools_profile():
    """The default path: --persona alone. The persona's own tools_profile
    flows through agent.apply_persona() exactly as it does for the GUI --
    apply_cli_persona_and_tools() does NOT call apply_tool_profile() itself
    in this case, since apply_persona() already owns that."""
    agent = _FakeAgent()
    messages = apply_cli_persona_and_tools(agent, persona_name="Lumina", tools_override=None)

    assert agent.applied_persona is not None
    assert agent.applied_persona.get("name") == "Lumina"
    assert agent.registry.applied_profile is None  # no separate apply_tool_profile call
    assert any("Persona: Lumina" in m for m in messages)
    assert any("All Tools" in m for m in messages)  # lumina.json's own tools_profile


def test_unknown_persona_name_is_reported_and_falls_back_to_base_config():
    agent = _FakeAgent()
    messages = apply_cli_persona_and_tools(agent, persona_name="Not A Real Persona", tools_override=None)

    assert agent.applied_persona is None
    assert any("No persona named 'Not A Real Persona'" in m for m in messages)


def test_tools_override_alone_applies_without_any_persona():
    """--tools with no --persona: a legitimate standalone use (base identity,
    trimmed tool set for one run) -- must not require --persona."""
    import core.tool_profiles as tp

    agent = _FakeAgent()
    applied = {}

    def fake_apply_tool_profile(registry, profile_name=None, tools_enabled=None, owner=True):
        applied["profile_name"] = profile_name
        applied["owner"] = owner

    real = tp.apply_tool_profile
    tp.apply_tool_profile = fake_apply_tool_profile
    try:
        messages = apply_cli_persona_and_tools(agent, persona_name=None, tools_override="Coding")
    finally:
        tp.apply_tool_profile = real

    assert agent.applied_persona is None
    assert applied == {"profile_name": "Coding", "owner": True}
    assert any("Tool profile override: Coding" in m for m in messages)


def test_unknown_tools_override_is_reported_and_leaves_tools_unchanged():
    agent = _FakeAgent()
    messages = apply_cli_persona_and_tools(agent, persona_name=None, tools_override="Not A Real Profile")

    assert agent.registry.applied_profile is None
    assert any("No tool profile named 'Not A Real Profile'" in m for m in messages)


def test_tools_override_applies_after_persona_so_it_wins():
    """Both flags together: persona's own profile loads first via
    apply_persona(), then --tools overrides it for this run -- documented
    order in apply_cli_persona_and_tools()'s docstring."""
    import core.tool_profiles as tp

    agent = _FakeAgent()
    call_order = []

    real_apply_persona = agent.apply_persona
    def tracking_apply_persona(persona):
        call_order.append("persona")
        real_apply_persona(persona)
    agent.apply_persona = tracking_apply_persona

    def fake_apply_tool_profile(registry, profile_name=None, tools_enabled=None, owner=True):
        call_order.append("tools_override")

    real = tp.apply_tool_profile
    tp.apply_tool_profile = fake_apply_tool_profile
    try:
        apply_cli_persona_and_tools(agent, persona_name="Lumina", tools_override="Coding")
    finally:
        tp.apply_tool_profile = real

    assert call_order == ["persona", "tools_override"]


# ── build_arg_parser() ──────────────────────────────────────────────────────

def test_bare_cli_flag_only():
    args = build_arg_parser().parse_args(["--cli"])
    assert args.cli is True
    assert args.persona is None
    assert args.tools is None


def test_no_flags_defaults():
    args = build_arg_parser().parse_args([])
    assert args.cli is False
    assert args.persona is None
    assert args.tools is None


def test_persona_and_tools_flags_parse():
    args = build_arg_parser().parse_args(["--cli", "--persona", "Lumina", "--tools", "Coding"])
    assert args.cli is True
    assert args.persona == "Lumina"
    assert args.tools == "Coding"


def test_persona_flag_accepts_multi_word_names():
    args = build_arg_parser().parse_args(["--cli", "--persona", "Rogue Test Persona"])
    assert args.persona == "Rogue Test Persona"


def test_unknown_flag_raises_systemexit():
    """argparse's default error handling (no add_help=False override here) --
    a typo'd flag should fail loudly, not silently parse as a positional or
    get ignored the way the old bare `"--cli" in sys.argv` check would have."""
    with pytest.raises(SystemExit):
        build_arg_parser().parse_args(["--persno", "Lumina"])
