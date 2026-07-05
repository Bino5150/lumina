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


def profile_display_name(profile: dict) -> str:
    """Returns 'Research (11)' style display name."""
    name = profile.get("name", "unnamed")
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
    "list_dir": "read_only", "search_files": "read_only", "read_file": "read_only",
    "palace_recall": "read_only", "palace_status": "read_only",
    "list_skills": "read_only", "recall_skill": "read_only",
    "search_chat_history": "read_only", "get_chat_session": "read_only",
    "list_recent_chats": "read_only", "list_custom_tools": "read_only",
    "browser_current_url": "read_only", "browser_get_links": "read_only",
    "browser_extract": "read_only", "browser_screenshot": "read_only",
    "diff_texts": "read_only", "diff_files": "read_only", "submit_pin": "read_only",

    "edit_prompt": "write_local", "reset_chat": "write_local",
    "save_memory": "write_local", "delete_memory": "write_local",
    "save_knowledge": "write_local", "delete_knowledge": "write_local",
    "save_person": "write_local", "write_file": "write_local",
    "palace_remember": "write_local", "palace_hall": "write_local",
    "save_skill": "write_local", "apply_patch": "write_local",
    "browser_navigate": "write_local", "browser_click": "write_local",
    "browser_type": "write_local", "browser_scroll": "write_local",
    "browser_close": "write_local",
    "send_telegram_file": "write_local", "send_telegram_message": "write_local",


    "run_python": "execute", "run_command": "execute",

    "create_tool": "self_modifying", "delete_tool": "self_modifying",
    # Outbound action tier intentionally empty — populate the moment any
    # send_telegram_message / post_discord / send_email tool is built.
}

# Hard-excluded from every non-owner session, independent of tier, independent
# of what any profile JSON says. Toolmaker breaks the allowlist model by
# design — it doesn't get to participate in it.
OWNER_ONLY_TOOLS = {
    "create_tool", "list_custom_tools", "delete_tool",
    "list_pending_tools", "show_pending_tool_source", "reject_pending_tool",
    "palace_review_writes", "palace_undo_write",
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


def resolve_enabled_set(profile_name: str = None, tools_enabled: list = None, owner: bool = True):
    """
    Single source of truth for 'what should be enabled.'
    - profile_name: looks up a named tool_profiles/*.json (the tools_profile field)
    - tools_enabled: an inline list (legacy persona field) — used if profile_name absent
    - owner: if False, OWNER_ONLY_TOOLS are stripped no matter what's in the input.
    Returns None if neither input is given.
    """
    enabled = None
    if profile_name:
        profile = find_profile_by_name(profile_name)
        if profile is not None:
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
    """
    all_tools = registry.all_tool_names()
    enabled = resolve_enabled_set(profile_name, tools_enabled, owner=owner)

    if enabled is None:
        if owner:
            return
        enabled = set()

    disabled = [t for t in all_tools if t not in enabled]
    registry.set_disabled(disabled)