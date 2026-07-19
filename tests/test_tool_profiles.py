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
from core.tool_profiles import TOOL_TIERS


def test_telegram_tools_are_outbound_action():
    assert TOOL_TIERS["send_telegram_file"] == "outbound_action"
    assert TOOL_TIERS["send_telegram_message"] == "outbound_action"


def test_telegram_tools_no_longer_write_local():
    # write_local is NOT in SENSITIVE_TIERS (core/agent.py), so a future
    # edit that accidentally reverted these tools to write_local would
    # silently un-gate them -- this guards specifically against that.
    assert TOOL_TIERS["send_telegram_file"] != "write_local"
    assert TOOL_TIERS["send_telegram_message"] != "write_local"
