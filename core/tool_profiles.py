"""
Lumina Tool Profile loader.
Tool profiles live in ~/lumina/tool_profiles/*.json
Each profile is a named set of enabled tools.
"""

import json
import os

PROFILES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tool_profiles")


def list_profiles() -> list[dict]:
    """Return list of tool profile dicts, sorted by name."""
    if not os.path.isdir(PROFILES_DIR):
        return []
    results = []
    for fname in sorted(os.listdir(PROFILES_DIR)):
        if fname.endswith(".json"):
            path = os.path.join(PROFILES_DIR, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data["_file"] = path
                data["_fname"] = fname
                results.append(data)
            except Exception as e:
                print(f"[TOOL_PROFILES] Failed to load {fname}: {e}", flush=True)
    return results


def load_profile(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_profile(path: str, data: dict):
    os.makedirs(PROFILES_DIR, exist_ok=True)
    # Strip internal keys before saving
    clean = {k: v for k, v in data.items() if not k.startswith("_")}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(clean, f, indent=2)


def delete_profile(path: str):
    if os.path.exists(path):
        os.remove(path)


def profile_display_name(profile: dict, all_tools: list = None) -> str:
    """Returns 'Research (11)' style display name.

    all_tools: the live registry's full tool universe (registry.all_tool_names()),
    same parameter FE-11 already threads into resolve_enabled_set() below. That
    fix made *enforcement* for the "All Tools" profile compute live rather than
    trust its JSON's own "enabled" list -- which drifts stale every time a new
    tool ships and nobody remembers to regenerate all_tools.json (confirmed: it
    sat at 60 while the live registry had grown to 68). FE-11 never touched
    *display* -- this was still reading the same stale count straight out of
    the file, so the dropdown showed "All Tools (60)" next to a session that
    actually had 68 enabled. Mirrors the identical "all tools" name check
    resolve_enabled_set() uses, so the two paths can't drift apart from each
    other again. Every other named profile (Research, Coding, Minimal...) is
    unaffected -- their JSON count is genuinely authoritative for them, same
    as it always was.
    """
    name = profile.get("name", "unnamed")
    if name.strip().lower() == "all tools" and all_tools is not None:
        count = len(all_tools)
    else:
        count = len(profile.get("enabled", []))
    return f"{name} ({count})"


def fname_from_name(name: str) -> str:
    """Convert profile name to safe filename."""
    safe = "".join(c if c.isalnum() or c in " _-" else "" for c in name)
    return safe.strip().replace(" ", "_").lower() + ".json"


# ── Tool Sensitivity Tiers (Epic A2) ────────────────────────────────────────
TOOL_TIERS = {
    "get_time": "read_only", "list_tools": "read_only", "view_prompt": "read_only",
    "search_memory": "read_only", "get_recent_memories": "read_only",
    "search_knowledge": "read_only", "search_people": "read_only",
    "web_search": "read_only", "get_website": "read_only", "get_wikipedia": "read_only",
    "check_for_updates": "read_only",
    "list_dir": "read_only", "search_files": "read_only", "read_file": "read_only",
    "search_code": "read_only",
    "read_process": "read_only", "list_processes": "read_only",
    "read_coding_checkpoint": "read_only",
    # CODING-08A3: read-only Git review observation/retrieval. Target
    # authorization (owner cwd/Project/worktree_id vs a non-owner's exact
    # review_target_grant) is a separate axis enforced inside tools/review.py
    # itself -- this tier only controls schema visibility/PIN-gating, same as
    # every other read_only tool; it never grants a target by itself.
    "review_changes": "read_only", "review_file_diff": "read_only",
    "start_process": "execute", "send_process_input": "execute", "stop_process": "execute",
    # CODING-06A2: pytest executes arbitrary repository Python -- materially
    # different from a read-only code-analysis operation. execute tier plus
    # OWNER_ONLY_TOOLS below (not PIN alone) is what actually gates it.
    "run_tests": "execute",
    "create_worktree": "execute", "list_worktrees": "read_only",
    "remove_worktree": "execute",
    "palace_recall": "read_only", "palace_status": "read_only",
    "list_skills": "read_only", "recall_skill": "read_only",
    "search_chat_history": "read_only", "get_chat_session": "read_only",
    "list_recent_chats": "read_only", "list_custom_tools": "read_only",
    "browser_current_url": "read_only", "browser_get_links": "read_only",
    "browser_extract": "read_only", "browser_screenshot": "read_only",
    "diff_texts": "read_only", "diff_files": "read_only", "submit_pin": "read_only",
    "get_active_project": "read_only",

    "edit_prompt": "write_local", "reset_chat": "write_local",
    # CODING-02B-A: project-context tools. activate_project/clear_active_project
    # only ever mutate the calling agent's own per-instance ProjectContextState
    # (core/project_context.py) -- no filesystem/system state beyond that, same
    # tier as edit_file. set_project_root is the one that writes persistent
    # machine-local configuration (DATA_DIR/projects/<name>/binding.json) and
    # is additionally hard-excluded via OWNER_ONLY_TOOLS below -- the tier here
    # documents its write_local semantics, the owner-only set is what actually
    # enforces the exclusion.
    "activate_project": "write_local", "clear_active_project": "write_local",
    "set_project_root": "write_local",
    "save_memory": "write_local", "delete_memory": "write_local",
    "save_knowledge": "write_local", "delete_knowledge": "write_local",
    "save_person": "write_local", "write_file": "write_local",
    "edit_file": "write_local",
    "save_coding_checkpoint": "write_local",
    "palace_remember": "write_local", "palace_hall": "write_local",
    "save_skill": "write_local", "apply_patch": "write_local",
    "browser_navigate": "write_local", "browser_click": "write_local",
    "browser_type": "write_local", "browser_scroll": "write_local",
    "browser_close": "write_local",


    "run_python": "execute", "run_command": "execute",

    "create_tool": "self_modifying", "delete_tool": "self_modifying",

    # FE-14: outbound_action now actually has members. send_telegram_file/
    # send_telegram_message moved here from write_local -- today this is
    # mostly latent (sends go to the owner's own chat ID, and Discord-Safe
    # doesn't include these tools), but the moment any profile hands a
    # non-owner session these tools, this tier is what PIN-gates them
    # instead of letting them fire free.
    "send_telegram_file": "outbound_action", "send_telegram_message": "outbound_action",
}

# Hard-excluded from every non-owner session, independent of tier, independent
# of what any profile JSON says. Toolmaker breaks the allowlist model by
# design — it doesn't get to participate in it.
OWNER_ONLY_TOOLS = {
    "create_tool", "list_custom_tools", "delete_tool",
    "list_pending_tools", "show_pending_tool_source", "reject_pending_tool",
    "palace_review_writes", "palace_undo_write",
    # CODING-04A2-B4: the persistent-process manager is process-global.
    # Observation and control are therefore owner-only just like launch;
    # profiles and PIN verification are separate axes and cannot restore
    # these capabilities to a non-owner session.
    "start_process", "read_process", "send_process_input",
    "stop_process", "list_processes",
    # CODING-05A3: durable engineering checkpoint state is owner-only in v1.
    # Profile selection, explicit grants, PIN verification, Project context,
    # and parent ownership are separate axes and cannot restore either tool.
    "read_coding_checkpoint", "save_coding_checkpoint",
    # CODING-06A2: run_tests executes arbitrary repository Python through
    # pytest. Locked v1 product decision: owner-only, full stop. Profile
    # selection, explicit tools_enabled grants, PIN verification, Project
    # activation, and parent ownership are separate axes and cannot restore
    # it for a non-owner session -- see tools/tests.py's module docstring.
    "run_tests",
    # CODING-07A3: linked-worktree lifecycle and even its session inventory
    # are owner-only in v1. Profile selection, explicit grants, PIN state,
    # active Project context, cached headless state, and parent authority are
    # all separate axes and can never restore these tools to owner=False.
    "create_worktree", "list_worktrees", "remove_worktree",
    # CODING-02B-A: persistent machine-local configuration (writes
    # DATA_DIR/projects/<name>/binding.json) -- distinct from
    # activate_project/clear_active_project, which only ever touch the
    # calling agent's own in-memory ProjectContextState and stay available
    # to a non-owner session that's been explicitly granted them.
    "set_project_root",
    # CODING-02B-A1: create_project also calls save_project_binding()
    # directly (tools/projects.py) -- it writes the exact same
    # DATA_DIR/projects/<name>/binding.json as set_project_root, so it must
    # be excluded for the identical reason. CODING-02B-A classified
    # set_project_root as owner-only but left create_project unclassified,
    # leaving a second, un-gated path to the same privileged write. Left
    # deliberately absent from TOOL_TIERS (not given a "write_local" entry)
    # -- OWNER_ONLY_TOOLS is checked before tier/PIN logic ever runs for a
    # non-owner session, so a tier entry here would be redundant at best.
    "create_project",
}


def find_profile_by_name(name: str) -> dict | None:
    """Case-insensitive lookup by profile name (not filename)."""
    if not name:
        return None
    target = name.strip().lower()
    for p in list_profiles():
        if p.get("name", "").strip().lower() == target:
            return p
    return None


def resolve_enabled_set(profile_name: str = None, tools_enabled: list = None, owner: bool = True,
                         all_tools: list = None):
    """
    Single source of truth for 'what should be enabled.'
    - profile_name: looks up a named tool_profiles/*.json (the tools_profile field)
    - tools_enabled: an inline list (legacy persona field) — used if profile_name absent
    - owner: if False, OWNER_ONLY_TOOLS are stripped no matter what's in the input.
    - all_tools: the live registry's full tool universe (registry.all_tool_names()).
      FE-11: the "All Tools" profile's enabled set is computed from THIS, live,
      every time — never read from all_tools.json's own "enabled" list. That
      file was a hand-maintained snapshot that drifted stale (missing 5+ tools
      as of S38) every time a new tool shipped and nobody remembered to
      regenerate it. Every other named profile (Research, Coding, Minimal...)
      is unaffected — those are deliberately curated, hand-picked sets, not
      "everything," so their JSON stays the source of truth for them.
    Returns None if neither input is given.
    """
    enabled = None
    if profile_name:
        profile = find_profile_by_name(profile_name)
        if profile is not None:
            if profile.get("name", "").strip().lower() == "all tools" and all_tools is not None:
                enabled = set(all_tools)
            else:
                enabled = set(profile.get("enabled", []))
    if enabled is None and tools_enabled is not None:
        enabled = set(tools_enabled)

    if enabled is None:
        return None

    if not owner:
        enabled -= OWNER_ONLY_TOOLS

    return enabled


def apply_tool_profile(registry, profile_name: str = None, tools_enabled: list = None, owner: bool = True):
    """
    THE function every code path uses to gate a registry — apply_persona(),
    the Settings UI, and the headless comms/subagent invoker. Computes against
    registry.all_tool_names(), the full raw universe — never get_schemas() or
    get_disabled() — so a previous restriction is never silently lost on switch.

    Default-deny: owner=False with nothing resolvable disables EVERYTHING,
    not everything. A missing/typo'd/broken profile fails closed.

    TOOL-PROFILE-REFRESH-01: explicit user-disabled tools (prefs
    "disabled_tools") survive profile application. A profile is a
    capability-selection convenience, never permission authority and never a
    silent resurrection of tools the user explicitly disabled — the union
    below can only ever shrink the enabled set, for owner and non-owner
    alike. Mirrors the construction-time overlay in LuminaAgent.__init__,
    which applies the same persisted list after registration; without this,
    any post-construction profile switch (persona apply_persona(), Settings
    profile combo, headless force_tools_profile) would re-enable a
    user-disabled tool merely because the selected profile lists it.
    """
    all_tools = registry.all_tool_names()
    enabled = resolve_enabled_set(profile_name, tools_enabled, owner=owner, all_tools=all_tools)

    if enabled is None:
        if owner:
            return
        enabled = set()

    from core.persistence import load as _load_persisted_prefs
    user_disabled = set(_load_persisted_prefs().get("disabled_tools", []) or [])

    # Union of the profile complement with the persisted user-disabled list
    # (clamped to the live universe — a stale prefs entry for a tool that no
    # longer exists must not linger in the disabled set forever).
    disabled = sorted((set(all_tools) - enabled) | (user_disabled & set(all_tools)))
    registry.set_disabled(disabled)
