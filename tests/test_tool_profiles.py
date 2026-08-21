"""core/tool_profiles.py — tool sensitivity tier classification.

FE-14 regression: send_telegram_file/send_telegram_message were classified
write_local (like save_memory, apply_patch, etc.) even though the module's
own comment planned an outbound_action tier for exactly these tools, and
core/agent.py's SENSITIVE_TIERS never included it. Today that's mostly
latent (sends go to the owner's own chat ID; Discord-Safe doesn't include
these tools), but the moment any profile hands a non-owner session these
tools, write_local wouldn't PIN-gate them -- outbound_action (now in
SENSITIVE_TIERS) does.
"""
from core.tool_profiles import TOOL_TIERS, OWNER_ONLY_TOOLS, list_profiles


def test_telegram_tools_are_outbound_action():
    assert TOOL_TIERS["send_telegram_file"] == "outbound_action"
    assert TOOL_TIERS["send_telegram_message"] == "outbound_action"


def test_telegram_tools_no_longer_write_local():
    # write_local is NOT in SENSITIVE_TIERS (core/agent.py), so a future
    # edit that accidentally reverted these tools to write_local would
    # silently un-gate them -- this guards specifically against that.
    assert TOOL_TIERS["send_telegram_file"] != "write_local"
    assert TOOL_TIERS["send_telegram_message"] != "write_local"


# ── CODING-01B-B: edit_file classification ─────────────────────────────
#
# CODING-01A confirmed an unclassified tool defaults to "execute" for the
# non-owner PIN gate (core/agent.py's fail-closed default) -- so omitting
# edit_file from TOOL_TIERS entirely would silently PIN-gate it in every
# non-owner session even though ordinary filesystem writes (write_file) are
# not. This guards specifically against that regression.

def test_edit_file_is_write_local():
    assert TOOL_TIERS["edit_file"] == "write_local"


def test_edit_file_not_sensitive_tier():
    assert TOOL_TIERS["edit_file"] not in {"execute", "self_modifying", "outbound_action"}


def test_edit_file_not_owner_only():
    assert "edit_file" not in OWNER_ONLY_TOOLS


def test_coding_profile_includes_edit_file():
    coding = next(p for p in list_profiles() if p.get("name") == "Coding")
    assert "edit_file" in coding.get("enabled", [])


def test_coding_profile_did_not_gain_unrelated_tools():
    """CODING-01B-B adds exactly one tool (edit_file) to Coding -- this is a
    regression guard against accidentally sweeping in other tools (e.g. the
    still-out-of-scope diff_texts/diff_files/git_* tools) while editing the
    same JSON file."""
    coding = next(p for p in list_profiles() if p.get("name") == "Coding")
    enabled = set(coding.get("enabled", []))
    expected = {
        "get_time", "list_tools", "view_prompt", "reset_chat",
        "save_memory", "search_memory", "get_recent_memories",
        "read_file", "write_file", "edit_file", "list_dir", "search_files",
        "run_python", "run_command",
        "create_tool", "list_custom_tools", "delete_tool",
        "palace_remember", "palace_hall", "palace_recall", "palace_status",
    }
    assert enabled == expected
