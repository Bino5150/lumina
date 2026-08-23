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
    """CODING-01B-B added edit_file; CODING-02B-A added exactly
    activate_project/get_active_project/clear_active_project (NOT
    set_project_root -- see test_set_project_root_not_in_coding_profile
    below); CODING-03A1 added exactly search_code (search_files stays,
    additive not replaced); CODING-04A2-B4 added exactly the five managed-
    process tools; CODING-06A2 added exactly run_tests -- this is a
    regression guard against accidentally sweeping in other tools (e.g. the
    still-out-of-scope diff_texts/diff_files/git_* tools) while editing the
    same JSON file."""
    coding = next(p for p in list_profiles() if p.get("name") == "Coding")
    enabled = set(coding.get("enabled", []))
    expected = {
        "get_time", "list_tools", "view_prompt", "reset_chat",
        "save_memory", "search_memory", "get_recent_memories",
        "read_file", "write_file", "edit_file", "list_dir", "search_files", "search_code",
        "run_python", "run_command",
        "start_process", "read_process", "send_process_input", "stop_process", "list_processes",
        "read_coding_checkpoint", "save_coding_checkpoint", "run_tests",
        "activate_project", "get_active_project", "clear_active_project",
        "create_tool", "list_custom_tools", "delete_tool",
        "palace_remember", "palace_hall", "palace_recall", "palace_status",
    }
    assert enabled == expected


# ── CODING-02B-A: project-context tool classification ──────────────────

def test_get_active_project_is_read_only():
    assert TOOL_TIERS["get_active_project"] == "read_only"


def test_activate_and_clear_active_project_are_write_local():
    assert TOOL_TIERS["activate_project"] == "write_local"
    assert TOOL_TIERS["clear_active_project"] == "write_local"


def test_set_project_root_is_write_local():
    assert TOOL_TIERS["set_project_root"] == "write_local"


def test_set_project_root_is_owner_only():
    assert "set_project_root" in OWNER_ONLY_TOOLS


def test_activate_and_clear_active_project_not_owner_only():
    # Unlike set_project_root, these only ever touch the calling agent's
    # own in-memory ProjectContextState -- an explicitly-granted non-owner
    # session must be able to reach them.
    assert "activate_project" not in OWNER_ONLY_TOOLS
    assert "clear_active_project" not in OWNER_ONLY_TOOLS
    assert "get_active_project" not in OWNER_ONLY_TOOLS


def test_project_context_tools_not_sensitive_tier():
    for name in ("activate_project", "get_active_project", "clear_active_project", "set_project_root"):
        assert TOOL_TIERS[name] not in {"execute", "self_modifying", "outbound_action"}


def test_set_project_root_not_in_coding_profile():
    coding = next(p for p in list_profiles() if p.get("name") == "Coding")
    assert "set_project_root" not in coding.get("enabled", [])


def test_old_project_tools_tier_behavior_unchanged():
    """CODING-02B-A only classified its own new tools -- create_project,
    load_project, update_project, refresh_codebase_index, load_codebase,
    link_chat, get_project_chats remained exactly as unclassified as before
    (defaulting to the fail-closed "execute" PIN tier for non-owner
    sessions, same as any other tool nobody has ever explicitly
    classified). CODING-02B-A1 changes exactly one bit of that: create_project
    is now in OWNER_ONLY_TOOLS (see test_create_project_is_owner_only below)
    -- it stays deliberately absent from TOOL_TIERS itself. The other six
    tools are untouched by this corrective."""
    for name in ("create_project", "load_project", "update_project",
                 "refresh_codebase_index", "load_codebase",
                 "link_chat", "get_project_chats"):
        assert name not in TOOL_TIERS
    for name in ("load_project", "update_project",
                 "refresh_codebase_index", "load_codebase",
                 "link_chat", "get_project_chats"):
        assert name not in OWNER_ONLY_TOOLS


# ── CODING-02B-A1: create_project owner-only binding-authority fix ──────
#
# CODING-02B-A gave set_project_root sole authority to write a machine-local
# execution-root binding (DATA_DIR/projects/<name>/binding.json) and made it
# owner-only -- but create_project(name, description, root_path) calls the
# exact same save_project_binding() to establish that same binding, and was
# left unclassified. An explicitly-granted non-owner session could reach
# create_project (unclassified tools are only PIN-gated, not hard-excluded)
# and configure a binding through the back door. This closes that gap.

def test_create_project_is_owner_only():
    assert "create_project" in OWNER_ONLY_TOOLS


def test_create_project_still_not_classified_in_tool_tiers():
    # Deliberate: OWNER_ONLY_TOOLS is checked before tier/PIN logic ever
    # runs for a non-owner session, so adding a "write_local" tier entry
    # here would be redundant at best -- see core/tool_profiles.py's
    # OWNER_ONLY_TOOLS comment for the full reasoning.
    assert "create_project" not in TOOL_TIERS
