"""tool_profiles/*.json -- MB-29: edit_prompt scoping decision.

Background: an errant edit_prompt call corrupted a running session's
in-memory system prompt during unrelated work on MB-28 (Aug 5, S51,
LUMINA_MURDERBOARD_CONSOLIDATED_1.md). MB-33 Tier 2 (S55) already closed the
*safety* half of this -- edit_prompt now only stages via
tools/pending_actions.py, it never applies directly, so an errant call can't
actually corrupt anything without an explicit human approval in the Pending
Actions Settings panel.

This closes the other half MB-29 asked about: *scoping*. edit_prompt has no
business being in a tool profile whose own description has nothing to do
with prompt engineering -- "Coding" ("File system, Python sandbox, terminal,
and memory. No web search.") is exactly that case, and it's exactly the kind
of profile that would have been active during an "unrelated work" incident
like the one that opened this item. Removing it is also a free ~68-token
trim per profile that carries it (schema_token_estimate()), which is the
same direction MB-10's follow-up already pushed on -- Bino's framing:
"win-win."

The test isn't just "coding.json doesn't have it today" -- it asserts the
actual rule going forward: no profile except the universal "All Tools" set
should carry edit_prompt, so a future profile addition can't silently
reintroduce the exact gap this item closed.
"""
from core.tool_profiles import list_profiles


def test_no_curated_profile_carries_edit_prompt():
    """Only "All Tools" -- the deliberately-unrestricted universal set -- may
    include edit_prompt. Every named/curated profile is scoped to a specific
    task shape (coding, research, chat, discord-safe, minimal), and none of
    them are "explicitly necessary" for prompt editing per MB-29's decision."""
    offenders = []
    for profile in list_profiles():
        name = profile.get("name", "").strip().lower()
        if name == "all tools":
            continue
        if "edit_prompt" in profile.get("enabled", []):
            offenders.append(profile.get("name"))
    assert offenders == [], (
        f"edit_prompt found in curated profile(s) {offenders} -- MB-29 decided "
        "this tool is excluded from every profile except All Tools unless a "
        "future profile is explicitly about prompt engineering."
    )


def test_all_tools_profile_still_carries_edit_prompt():
    """The universal profile is deliberately unrestricted -- this isn't a
    blanket ban on the tool, just a scoping decision for curated subsets."""
    all_tools = next(p for p in list_profiles() if p.get("name") == "All Tools")
    assert "edit_prompt" in all_tools.get("enabled", [])


def test_coding_profile_still_has_reset_chat_and_view_prompt():
    """Regression guard against an overzealous edit sweeping out reset_chat/
    view_prompt too -- MB-29's decision was scoped specifically to
    edit_prompt ("meta.py's prompt-editing tools (edit_prompt)"), not every
    meta.py tool. reset_chat is a separate, already Tier-2-gated concern;
    view_prompt is read-only. Neither was part of this decision."""
    coding = next(p for p in list_profiles() if p.get("name") == "Coding")
    enabled = coding.get("enabled", [])
    assert "reset_chat" in enabled
    assert "view_prompt" in enabled
    assert "edit_prompt" not in enabled
