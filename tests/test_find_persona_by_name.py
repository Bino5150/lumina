"""core/personas.py -- find_persona_by_name() (MB-22).

MB-22 (CLI --persona flag) needed a case-insensitive "look up a persona by
its human-facing name" primitive, same shape as core/tool_profiles.py's
already-existing find_profile_by_name(). This is that primitive, plus the
channel_bound exclusion list_personas() already enforces for the desktop
sidebar -- a --persona flag shouldn't be able to select
personas/discord_template.json (its own description field says the
tools_profile there is documentation-only and never actually applied; it's
not a persona meant to be picked by name outside comms/discord_bridge.py).
"""
from core.personas import find_persona_by_name, list_personas


def test_finds_default_lumina_persona_by_exact_name():
    persona = find_persona_by_name("Lumina")
    assert persona is not None
    assert persona.get("name") == "Lumina"
    # Must resolve to the real desktop persona (personas/lumina.json), not
    # the channel_bound Discord template -- see next test.
    assert persona.get("tools_profile") == "All Tools"


def test_lookup_is_case_and_whitespace_insensitive():
    assert find_persona_by_name("  lumina  ") is not None
    assert find_persona_by_name("LUMINA") is not None
    assert find_persona_by_name("lUmInA") is not None


def test_unknown_name_returns_none():
    assert find_persona_by_name("Definitely Not A Real Persona Name") is None


def test_empty_or_none_name_returns_none():
    assert find_persona_by_name("") is None
    assert find_persona_by_name(None) is None


def test_channel_bound_discord_template_excluded_by_default():
    """discord_template.json also declares name="Lumina" -- without the
    channel_bound exclusion, a name lookup would be ambiguous/order-dependent
    between it and the real default persona. list_personas() already solves
    this for the desktop sidebar; find_persona_by_name() must inherit it so
    a --persona flag can't accidentally select a comms-only template whose
    own tools_profile field the code doesn't even honor."""
    matches = [p for p in list_personas(include_channel_bound=True)
               if p.get("name", "").strip().lower() == "lumina"]
    assert len(matches) == 2, "expected exactly the two known name='Lumina' files"

    resolved = find_persona_by_name("Lumina")
    assert resolved.get("_file", "").endswith("discord_template.json") is False


def test_include_channel_bound_true_can_still_reach_the_template():
    """Escape hatch preserved for a caller that genuinely needs it (mirrors
    list_personas' own include_channel_bound param) -- not used by the CLI
    flag. With both name="Lumina" candidates in play, list_personas() sorts
    by filename ("discord_template.json" < "lumina.json"), so the template
    wins the first-match lookup -- documenting that ordering here so a future
    third same-named persona file doesn't change this silently."""
    resolved = find_persona_by_name("Lumina", include_channel_bound=True)
    assert resolved is not None
    assert resolved.get("_file", "").endswith("discord_template.json")
