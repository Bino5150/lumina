"""core/tool_profiles.py -- profile_display_name() stale "All Tools" count.

Bino caught this by eye: the Tools tab's profile dropdown showed "All Tools
(60)" sitting right next to the new MB-10 follow-up label correctly showing
"67/68 enabled ... schema tokens" -- same screen, two different counts for
the same profile. Root cause: FE-11 already fixed *enforcement* for the
"All Tools" profile (resolve_enabled_set() computes it live off
registry.all_tool_names() rather than trusting all_tools.json's own
"enabled" list, specifically because that snapshot drifts every time a new
tool ships and nobody regenerates the file). FE-11 never touched *display*
-- profile_display_name() was still reading straight out of the same stale
JSON, so the dropdown kept showing whatever count the file happened to have
the day it was last regenerated (60), regardless of how many tools the live
registry actually had (68).

Fix mirrors FE-11's exact "all tools" name check (profile.get("name", "")
.strip().lower() == "all tools") so the two paths -- enforcement and
display -- can't drift apart from each other again. Every other named
profile is deliberately unaffected: their JSON's "enabled" list IS the
source of truth for them, same as always.
"""
from core.tool_profiles import profile_display_name


def test_all_tools_profile_uses_live_count_when_provided():
    profile = {"name": "All Tools", "enabled": ["a"] * 60}  # stale snapshot: 60
    live_tools = ["t"] * 68  # real registry: 68
    assert profile_display_name(profile, all_tools=live_tools) == "All Tools (68)"


def test_all_tools_profile_falls_back_to_stale_count_without_live_tools():
    """Backward compatibility: callers that don't pass all_tools (none should
    remain after this fix, but nothing should crash if one shows up later)
    keep the old, imperfect-but-functional behavior rather than erroring."""
    profile = {"name": "All Tools", "enabled": ["a"] * 60}
    assert profile_display_name(profile) == "All Tools (60)"


def test_named_profile_is_unaffected_by_live_tools_param():
    """Research/Coding/Minimal/etc. -- their own JSON "enabled" list is
    genuinely authoritative. Passing all_tools must not change their count."""
    profile = {"name": "Research", "enabled": ["a", "b", "c"]}
    live_tools = ["t"] * 68
    assert profile_display_name(profile, all_tools=live_tools) == "Research (3)"
    assert profile_display_name(profile) == "Research (3)"


def test_all_tools_name_match_is_case_and_whitespace_insensitive():
    """Matches the exact same check resolve_enabled_set() uses for
    enforcement -- verifying display can't silently diverge from it again."""
    profile = {"name": "  ALL TOOLS  ", "enabled": ["a"] * 60}
    live_tools = ["t"] * 68
    assert profile_display_name(profile, all_tools=live_tools) == "  ALL TOOLS   (68)"
