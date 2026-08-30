"""TOOL-PROFILE-REFRESH-01 — shipped tool-profile modernization regressions.

Source-vet finding this file locks in: the shipped profiles predate much of
the modern coding/process/test/Git/worktree/review stack. The Coding profile
was modernized against the LIVE registry (registry first, profiles second):

  + apply_patch, diff_texts, diff_files      (diff/patch workflow)
  + git_status, git_diff, git_log, git_branches  (read-only Git observation)
  + load_project, load_codebase, refresh_codebase_index, update_project
                                             (Project context)
  + view_image                               (UI acceptance screenshots)

Deliberately NOT added (authority / validity rationale, enforced below):
  - spawn_subagent + check_background_task + run_background_subagent +
    schedule_background_subagent: flag-gated registration
    (SUBAGENTS_ENABLED / BACKGROUND_TASKS_ENABLED) — a shipped profile
    referencing them would be a dead reference whenever the flags are off.
  - set_project_root / create_project: OWNER_ONLY_TOOLS (binding authority).
  - edit_prompt: MB-29 scoping — prompt editing is not a coding workflow.
  - browser_* / web_*: not the Coding profile's job (no web, per its own
    description).

Authority invariants re-proven here: profiles are a capability-selection
convenience, never permission authority. OWNER_ONLY_TOOLS stripping, the
non-owner default-deny, and the persisted user-disabled overlay all survive
profile application.

The registry universe used for validity checks is source-derived via AST
extraction of every literal `.register(...)` call under tools/ and core/ —
the same inventory the runtime registers (verified during the source-vet:
93 static names; the only runtime-only variance is flag-gated modules and
approved-custom tools, whose names are still literal in source).
"""

import ast
import json
import os

import pytest

import core.secrets as secrets_module
from core import emergency_stop, persistence, process_manager
from core.agent import LuminaAgent
from core.tool_profiles import (
    OWNER_ONLY_TOOLS,
    TOOL_TIERS,
    apply_tool_profile,
    list_profiles,
    resolve_enabled_set,
)
from tools.registry import ToolRegistry


# core/agent.py's non-owner PIN gate classifies unclassified tools as
# "execute" (fail closed). Same set, restated here for profile audits.
SENSITIVE_TIERS = {"execute", "self_modifying", "outbound_action"}

# The modern coding capabilities established by the TOOL-PROFILE-REFRESH-01
# source-vet. The first block is what this task added; the rest is the
# previously-landed modern stack the profile must never lose.
NEWLY_REQUIRED_MODERN = {
    "apply_patch", "diff_texts", "diff_files",
    "git_status", "git_diff", "git_log", "git_branches",
    "load_project", "load_codebase", "refresh_codebase_index", "update_project",
    "view_image",
}
PREVIOUSLY_LANDED_MODERN = {
    "search_code", "edit_file", "read_file", "write_file", "list_dir", "search_files",
    "run_python", "run_command",
    "start_process", "read_process", "send_process_input", "stop_process", "list_processes",
    "read_coding_checkpoint", "save_coding_checkpoint", "run_tests",
    "create_worktree", "list_worktrees", "remove_worktree",
    "review_changes", "review_file_diff",
    "activate_project", "get_active_project", "clear_active_project",
}
REQUIRED_MODERN_CODING = NEWLY_REQUIRED_MODERN | PREVIOUSLY_LANDED_MODERN

# Flag-gated registrations: legitimate live tools, but invalid as SHIPPED
# profile members because the shipped default for both flags is False.
FLAG_GATED_TOOLS = {
    "spawn_subagent",
    "check_background_task", "run_background_subagent", "schedule_background_subagent",
}


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(secrets_module, "SECRETS_PATH", str(tmp_path / "credentials.json"))
    emergency_stop._reset_for_tests()
    process_manager._reset_for_tests()
    yield
    process_manager._reset_for_tests()
    emergency_stop._reset_for_tests()


# ── source-derived registry universe ────────────────────────────────────────

def _registered_universe() -> set:
    """Every tool name the runtime can register, extracted from source.

    AST-based (not regex): catches both positional registry.register("name", ...)
    and keyword registry.register(name="name", ...) call shapes. tools/ + core/
    are the only registration surfaces (core/agent.py imports and invokes every
    register_*_tools entry point from there).
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    names = set()
    for sub in ("tools", "core"):
        directory = os.path.join(root, sub)
        for fname in sorted(os.listdir(directory)):
            if not fname.endswith(".py"):
                continue
            path = os.path.join(directory, fname)
            try:
                tree = ast.parse(open(path, encoding="utf-8").read())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "register"):
                    continue
                name = None
                if (node.args and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)):
                    name = node.args[0].value
                else:
                    for kw in node.keywords:
                        if (kw.arg == "name" and isinstance(kw.value, ast.Constant)
                                and isinstance(kw.value.value, str)):
                            name = kw.value.value
                            break
                if name:
                    names.add(name)
    return names


def test_registry_universe_extraction_is_nonvacuous():
    """Guard the extractor itself: if the AST scan ever regresses (renamed
    call shape, moved modules), the validity tests below would silently pass
    against an empty universe. Pin a representative sample from every
    registration era, including every tool this task added to Coding."""
    universe = _registered_universe()
    assert len(universe) >= 90
    for name in (
        "get_time",                       # meta era
        "save_memory", "web_search",      # memory/web era
        "run_python", "read_file",        # sandbox/filesystem era
        "search_code", "edit_file",       # CODING-03A1 / 01B-B
        "start_process", "run_tests",     # CODING-04A2-B4 / 06A2
        "create_worktree", "review_changes",  # CODING-07A3 / 08A3
        "git_status", "git_diff", "git_log", "git_branches",
        "apply_patch", "diff_texts", "diff_files",
        "load_project", "load_codebase", "refresh_codebase_index", "update_project",
        "view_image",
    ):
        assert name in universe, f"extractor lost {name}"


# ── 1/8: profile validity — no dead/nonexistent references ─────────────────

def test_every_shipped_profile_references_only_registered_tools():
    universe = _registered_universe()
    for profile in list_profiles():
        enabled = set(profile.get("enabled", []))
        dead = sorted(enabled - universe)
        assert not dead, (
            f"profile '{profile.get('name')}' references nonexistent tools: {dead}"
        )


def test_no_shipped_profile_references_flag_gated_tools():
    """Flag-gated tools are real registered tools when their flag is on, but
    shipped profiles must not reference them: with the flags off (the shipped
    default) the reference is a silent dead entry. They join a profile only
    when their registration becomes unconditional."""
    for profile in list_profiles():
        enabled = set(profile.get("enabled", []))
        flagged = sorted(enabled & FLAG_GATED_TOOLS)
        assert not flagged, (
            f"profile '{profile.get('name')}' references flag-gated tools: {flagged}"
        )


# ── 2: Coding completeness — the modern stack ──────────────────────────────

def test_coding_profile_includes_modern_coding_stack():
    coding = next(p for p in list_profiles() if p.get("name") == "Coding")
    enabled = set(coding.get("enabled", []))
    missing = sorted(REQUIRED_MODERN_CODING - enabled)
    assert not missing, f"Coding profile is missing modern capabilities: {missing}"


def test_coding_profile_excludes_authority_and_out_of_scope_tools():
    coding = next(p for p in list_profiles() if p.get("name") == "Coding")
    enabled = set(coding.get("enabled", []))
    for name in ("set_project_root", "create_project", "edit_prompt",
                 "web_search", "get_website", "browser_navigate"):
        assert name not in enabled, f"{name} must not be in the Coding profile"


# ── 3: owner boundary — profiles never restore owner-only tools ────────────

def test_resolve_enabled_set_strips_owner_only_for_non_owner():
    universe = sorted(_registered_universe())
    for profile in list_profiles():
        enabled = resolve_enabled_set(
            profile_name=profile.get("name"), owner=False, all_tools=universe
        )
        assert enabled is not None
        leaked = sorted(enabled & OWNER_ONLY_TOOLS)
        assert not leaked, (
            f"profile '{profile.get('name')}' restores owner-only tools "
            f"to a non-owner session: {leaked}"
        )


def test_apply_tool_profile_owner_false_cannot_dispatch_owner_only():
    reg = ToolRegistry()
    for name in ("run_tests", "save_coding_checkpoint", "create_worktree", "get_time"):
        reg.register(name, lambda: "probe", "probe", {"type": "object", "properties": {}})
    apply_tool_profile(reg, profile_name="All Tools", owner=False)
    schema_names = {s["function"]["name"] for s in reg.get_schemas()}
    assert "get_time" in schema_names          # ordinary tool survives
    assert not (schema_names & OWNER_ONLY_TOOLS)
    for name in ("run_tests", "save_coding_checkpoint", "create_worktree"):
        assert name not in schema_names
        assert "disabled" in reg.call(name, {}).lower()


# ── 4: Discord-Safe stays deliberately narrow ──────────────────────────────

def test_discord_safe_membership_unchanged_and_read_only():
    discord = next(p for p in list_profiles() if p.get("name") == "Discord-Safe")
    enabled = set(discord.get("enabled", []))
    assert enabled == {
        "get_time", "web_search", "get_website", "get_wikipedia",
        "list_skills", "recall_skill", "submit_pin",
    }
    universe = _registered_universe()
    assert enabled <= universe
    for name in enabled:
        assert name not in OWNER_ONLY_TOOLS
        tier = TOOL_TIERS.get(name)
        assert tier == "read_only", (
            f"Discord-Safe member '{name}' has tier {tier!r}; only read_only "
            f"tools may ship in this profile"
        )


def test_discord_safe_gains_no_write_or_execute_capability():
    discord = next(p for p in list_profiles() if p.get("name") == "Discord-Safe")
    enabled = set(discord.get("enabled", []))
    sensitive = {n for n in enabled if TOOL_TIERS.get(n, "execute") in SENSITIVE_TIERS}
    assert not sensitive, f"Discord-Safe gained sensitive-tier tools: {sorted(sensitive)}"


# ── 5: explicit disabled_tools survive profile application ─────────────────

def _write_prefs_disabled(monkeypatch, tmp_path, disabled):
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    persistence.save({"disabled_tools": list(disabled)})


def test_disabled_tools_survive_profile_application(tmp_path, monkeypatch):
    """A profile is a capability-selection convenience — selecting one must
    never silently re-enable a tool the user explicitly disabled (the
    pre-refresh behavior: apply_tool_profile recomputed the disabled set from
    scratch, resurrecting user-disabled profile members on every switch)."""
    _write_prefs_disabled(monkeypatch, tmp_path, ["run_command", "run_python"])
    reg = ToolRegistry()
    for name in ("get_time", "run_command", "run_python", "git_status", "web_search"):
        reg.register(name, lambda: "probe", "probe", {"type": "object", "properties": {}})
    apply_tool_profile(reg, profile_name="Coding", owner=True)
    assert not reg.is_enabled("run_command")
    assert not reg.is_enabled("run_python")
    assert reg.is_enabled("git_status")       # profile member, not user-disabled
    assert not reg.is_enabled("web_search")   # not a Coding member
    schema_names = {s["function"]["name"] for s in reg.get_schemas()}
    assert "run_command" not in schema_names and "run_python" not in schema_names


def test_disabled_tools_survive_real_agent_persona_profile_reapply(tmp_path, monkeypatch):
    """End-to-end through a real LuminaAgent: construction overlay disables
    the user's tools; a post-construction profile application (persona
    apply_persona / Settings switch path) must not resurrect them."""
    _write_prefs_disabled(monkeypatch, tmp_path, ["run_command"])
    agent = LuminaAgent(owner=True, channel_id="tpr01-overlay", backend="llamacpp")
    assert not agent.registry.is_enabled("run_command")  # construction overlay
    apply_tool_profile(agent.registry, profile_name="Coding", owner=True)
    assert not agent.registry.is_enabled("run_command")
    assert agent.registry.is_enabled("git_status")       # Coding member, enabled
    assert agent.registry.is_enabled("get_time")


# ── 6: profile switching leaves no stale capabilities ──────────────────────

def test_profile_switch_leaves_no_stale_capabilities():
    reg = ToolRegistry()
    probes = ("get_time", "web_search", "run_tests", "git_status", "save_memory", "read_file")
    for name in probes:
        reg.register(name, lambda: "probe", "probe", {"type": "object", "properties": {}})

    def enabled_now():
        return set(reg.list_enabled())

    apply_tool_profile(reg, profile_name="Coding", owner=True)
    assert enabled_now() == {"get_time", "run_tests", "git_status", "save_memory", "read_file"}

    apply_tool_profile(reg, profile_name="Research", owner=True)
    assert enabled_now() == {"get_time", "web_search", "save_memory", "read_file"}

    apply_tool_profile(reg, profile_name="Chat", owner=True)
    assert enabled_now() == {"get_time"}


# ── 7: headless default-closed for non-owner with no profile ───────────────

def test_non_owner_no_profile_fails_closed():
    reg = ToolRegistry()
    for name in ("get_time", "web_search", "run_tests"):
        reg.register(name, lambda: "probe", "probe", {"type": "object", "properties": {}})
    apply_tool_profile(reg, profile_name=None, tools_enabled=None, owner=False)
    assert reg.list_enabled() == []
    assert resolve_enabled_set(None, None, owner=False, all_tools=reg.all_tool_names()) is None


def test_non_owner_inline_grant_still_strips_owner_only():
    enabled = resolve_enabled_set(
        profile_name=None,
        tools_enabled=["get_time", "run_tests", "create_worktree"],
        owner=False,
    )
    assert enabled == {"get_time"}


# ── schema footprint hygiene ────────────────────────────────────────────────

def test_profile_cleanup_reduces_schema_footprint():
    """The 6k TOOL_BUDGET_TOKENS warning is advisory (print + UI readout), and
    the owner's All Tools footprint is inherent to All Tools (live-computed
    since FE-11 — all_tools.json's stale snapshot is never enforced). Profile
    hygiene still pays: selecting the refreshed Coding profile must estimate
    well under the All-Tools footprint, not because of stale JSON entries but
    because it is a genuinely narrower curated set."""
    universe = sorted(_registered_universe())
    reg = ToolRegistry()
    for name in universe:
        reg.register(name, lambda: "probe", "probe", {"type": "object", "properties": {}})

    def footprint():
        return sum(len(str(s)) // 4 for s in reg.get_schemas())

    apply_tool_profile(reg, profile_name="All Tools", owner=True)
    all_tools_footprint = footprint()
    apply_tool_profile(reg, profile_name="Coding", owner=True)
    coding_footprint = footprint()
    assert coding_footprint < all_tools_footprint
