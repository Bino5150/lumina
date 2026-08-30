"""TOOL-TIER-CLASSIFICATION-01 -- explicit read_only classification of
previously unclassified, non-owner-relevant tools.

This slice is an authority-sensitive change, not metadata cleanup: adding a
"read_only" TOOL_TIERS entry exempts a tool from the non-owner PIN gate
(core/agent.py's SENSITIVE_TIERS check defaults an UNCLASSIFIED tool to the
"execute" tier, which is PIN-gated). Every classification here is therefore
backed by a source-vetted proof that the tool's complete production call
path is observational.

Per-tool evidence (source files at the 657c185 baseline):

  git_status     tools/git_status.py     -- only `git rev-parse`, `git
                    rev-list --count --left-right` (against the LOCAL
                    remote-tracking ref; no fetch), and `git status
                    --short`. No writes, no network, no ref mutation.
  git_diff       tools/git_diff.py      -- only `git diff` behind
                    --end-of-options argument-injection hardening.
  git_log        tools/git_log.py       -- only `git log --max-count
                    --format --author` (+ --end-of-options <branch>).
  git_branches   tools/git_branches.py  -- only `git branch [-a]
                    [--list <pattern>]`. No ref args are ever passed, so
                    no branch can be created/renamed/deleted.
  view_image     tools/vision.py        -- os.path.exists/isfile +
                    os.path.getsize only. File CONTENTS are never read.
  get_weather    tools/get_weather.py   -- pure hash-based function, zero
                    I/O of any kind. (Registered via the FE-11
                    approved-custom-tool loader, not statically -- the
                    tier map classifies by name either way.)
  load_project   tools/projects.py      -- reads projects/<n>/project.md.
  load_codebase  tools/projects.py      -- reads projects/<n>/codebase.md.
  get_project_chats  tools/projects.py  -- reads DATA_DIR chats.json.

Deliberately LEFT UNCLASSIFIED (non-owner-relevant, fail-closed execute
default retained -- each would need behavioral or policy work before an
honest tier claim):

  update_project          overwrites projects/<n>/project.md.
  refresh_codebase_index  overwrites projects/<n>/codebase.md.
  link_chat               rewrites DATA_DIR/<n>/chats.json.
  spawn_subagent          launches a headless child agent (flag-gated:
                          SUBAGENTS_ENABLED).
  run_background_subagent enqueues a task; mutates task-queue + agent
                          state (flag-gated: BACKGROUND_TASKS_ENABLED).
  schedule_background_subagent  enqueues a future task (same flag).
  check_background_task   AMBIGUOUS: a pure dict-copy read for live
                          entries, but get_task_result() lazy-GC DELETES
                          TTL-expired entries from the shared _results
                          dict -- a hidden mutating edge inside a
                          nominal read. Left unclassified per the
                          classification rule ("reject if normal
                          successful execution can ... persist/change
                          application state"); revisit only if the GC
                          is moved out of the read path.

Owner-only unclassified tools (create_project, list_pending_tools,
show_pending_tool_source, reject_pending_tool, palace_review_writes,
palace_undo_write) are NOT non-owner-reachable: OWNER_ONLY_TOOLS is
stripped structurally in resolve_enabled_set() before tier/PIN logic ever
runs, so a tier entry for them would be redundant at best (same reasoning
core/tool_profiles.py already documents for create_project).
"""

import json
import os

import pytest

import config
import core.secrets as secrets_module
import core.tool_profiles as tool_profiles
import tools.toolmaker as toolmaker
from core import persistence
from core.agent import LuminaAgent
from core.tool_profiles import (
    OWNER_ONLY_TOOLS,
    TOOL_TIERS,
    apply_tool_profile,
    list_profiles,
)
from tools.get_weather import register_get_weather_tool
from tools.registry import ToolRegistry

# ── The classification decision, frozen ──────────────────────────────────

NEWLY_CLASSIFIED = {
    "git_status", "git_diff", "git_log", "git_branches",
    "view_image", "get_weather",
    "load_project", "load_codebase", "get_project_chats",
}

# Non-owner-relevant tools that deliberately remain unclassified (execute
# tier fail-closed default). Exact set -- a future tool must either be
# classified here consciously or this test breaks, which is the audit.
STILL_UNCLASSIFIED_NON_OWNER = {
    "update_project",
    "refresh_codebase_index",
    "link_chat",
    "spawn_subagent",
    "run_background_subagent",
    "schedule_background_subagent",
    "check_background_task",
}

# Owner-only unclassified tools: never non-owner-reachable, tier entry
# deliberately omitted (redundant behind the OWNER_ONLY_TOOLS strip).
STILL_UNCLASSIFIED_OWNER_ONLY = {
    "create_project",
    "list_pending_tools",
    "show_pending_tool_source",
    "reject_pending_tool",
    "palace_review_writes",
    "palace_undo_write",
}

EXPECTED_UNCLASSIFIED = (
    STILL_UNCLASSIFIED_NON_OWNER | STILL_UNCLASSIFIED_OWNER_ONLY
)

# Which shipped profiles contain each newly classified tool (from the
# tool_profiles/*.json files at the 657c185 baseline -- classification
# must not change any of this). NOTE: all_tools.json is the known-stale
# hand snapshot (FE-11: All Tools enforcement is live-computed from the
# registry, so the JSON was never regenerated when the git tools and
# view_image joined Coding) -- the frozen expectation records that
# reality as-is.
EXPECTED_PROFILE_MEMBERSHIP = {
    "git_status": {"Coding"},
    "git_diff": {"Coding"},
    "git_log": {"Coding"},
    "git_branches": {"Coding"},
    "view_image": {"Coding"},
    "get_weather": set(),          # in no shipped profile
    "load_project": {"Coding", "All Tools"},
    "load_codebase": {"Coding", "All Tools"},
    "get_project_chats": {"All Tools"},
}


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
def isolated_agent_data(tmp_path, monkeypatch):
    """Standard agent-isolation fixture (same pattern as
    test_file_edit_integration.py / test_projects.py), plus a toolmaker
    audit-log isolation so the FE-11 startup loader is deterministic:
    no approved-custom-tool state from the host machine leaks into the
    registry universe under test."""
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(secrets_module, "SECRETS_PATH", str(tmp_path / "credentials.json"))
    # FE-11 loader determinism: point the audit log at an empty tmp path so
    # load_approved_custom_tools() approves nothing on its own. get_weather
    # (the one real loader-loaded product tool) is registered explicitly
    # below to represent the loader's production outcome deterministically.
    monkeypatch.setattr(toolmaker, "AUDIT_LOG_PATH", str(tmp_path / "tool_audit.log"))


def _full_agent(owner=True, channel_id="tier-classification-01"):
    """Real LuminaAgent with BOTH feature flags on, so the flag-gated
    subagent/task tools are part of the registry universe exactly as they
    are for an owner who has enabled them in Settings."""
    return LuminaAgent(
        owner=owner,
        channel_id=channel_id,
        backend="llamacpp",
    )


@pytest.fixture
def full_registry(isolated_agent_data, monkeypatch):
    """The complete production registry universe: every statically
    registered tool + flag-gated subagent/task tools + get_weather (as the
    FE-11 loader would load it)."""
    monkeypatch.setattr(config, "SUBAGENTS_ENABLED", True)
    monkeypatch.setattr(config, "BACKGROUND_TASKS_ENABLED", True)
    agent = _full_agent(owner=True)
    register_get_weather_tool(agent.registry)
    return agent.registry


# ── 1. Classification truth ──────────────────────────────────────────────

def test_newly_classified_tools_are_read_only():
    for name in NEWLY_CLASSIFIED:
        assert TOOL_TIERS.get(name) == "read_only", (
            f"{name} must carry the explicit read_only tier"
        )


def test_newly_classified_tools_carry_no_other_tier():
    """Belt-and-suspenders alongside the exact-tier assertion: none of the
    newly classified tools may appear in any non-read_only tier bucket."""
    for tier, names in (
        ("write_local", [n for n, t in TOOL_TIERS.items() if t == "write_local"]),
        ("execute", [n for n, t in TOOL_TIERS.items() if t == "execute"]),
        ("self_modifying", [n for n, t in TOOL_TIERS.items() if t == "self_modifying"]),
        ("outbound_action", [n for n, t in TOOL_TIERS.items() if t == "outbound_action"]),
    ):
        overlap = NEWLY_CLASSIFIED & set(names)
        assert not overlap, f"{tier} bucket must not contain newly classified tools: {sorted(overlap)}"


# ── 2. Complete candidate accounting (the audit) ─────────────────────────

def test_unclassified_universe_accounting_is_exact(full_registry):
    """THE audit: the live registry universe minus the tier map must be
    EXACTLY the deliberate remainder. Any future tool registered without a
    TOOL_TIERS entry lands here and breaks this test -- forcing a
    conscious classification decision instead of a silent fall-through to
    the execute-tier PIN gate."""
    universe = set(full_registry.all_tool_names())
    unclassified = universe - set(TOOL_TIERS)
    assert unclassified == EXPECTED_UNCLASSIFIED, (
        "Registry/tier accounting drifted. Newly unclassified tools: "
        f"{sorted(unclassified - EXPECTED_UNCLASSIFIED)}; "
        f"tier entries for tools that no longer register: "
        f"{sorted(EXPECTED_UNCLASSIFIED - unclassified)}"
    )


def test_non_owner_relevant_remainder_is_exact(full_registry):
    universe = set(full_registry.all_tool_names())
    unclassified = universe - set(TOOL_TIERS)
    non_owner_relevant = unclassified - OWNER_ONLY_TOOLS
    assert non_owner_relevant == STILL_UNCLASSIFIED_NON_OWNER


def test_owner_only_unclassified_remainder_is_exact(full_registry):
    universe = set(full_registry.all_tool_names())
    unclassified = universe - set(TOOL_TIERS)
    owner_only_unclassified = unclassified & OWNER_ONLY_TOOLS
    assert owner_only_unclassified == STILL_UNCLASSIFIED_OWNER_ONLY


def test_still_unclassified_non_owner_tools_are_deliberate():
    """Documentation-in-test: every tool left unclassified has a recorded
    reason (module docstring carries the full evidence)."""
    reasons = {
        "update_project": "overwrites project.md",
        "refresh_codebase_index": "overwrites codebase.md",
        "link_chat": "rewrites chats.json",
        "spawn_subagent": "launches a headless child agent",
        "run_background_subagent": "enqueues a task (mutating)",
        "schedule_background_subagent": "enqueues a future task (mutating)",
        "check_background_task": "TTL lazy-GC mutation in get_task_result",
    }
    assert set(reasons) == STILL_UNCLASSIFIED_NON_OWNER


# ── 3. No accidental profile expansion ───────────────────────────────────

def test_newly_classified_profile_membership_is_unchanged():
    """Classification must not move any tool into or out of any shipped
    profile. Frozen membership for every newly classified tool, read from
    the live profile JSONs."""
    profiles = {p["name"]: set(p.get("enabled", [])) for p in list_profiles()}
    for name, expected_profiles in EXPECTED_PROFILE_MEMBERSHIP.items():
        actual = {pname for pname, members in profiles.items() if name in members}
        assert actual == expected_profiles, (
            f"{name} profile membership drifted: {sorted(actual)} != "
            f"{sorted(expected_profiles)}"
        )


def test_discord_safe_stays_narrow_and_read_only():
    """The authority-critical narrow profile is untouched by this slice:
    same seven members, every one read_only."""
    discord = next(p for p in list_profiles() if p.get("name") == "Discord-Safe")
    assert set(discord.get("enabled", [])) == {
        "get_time", "get_website", "get_wikipedia",
        "list_skills", "recall_skill", "submit_pin", "web_search",
    }
    for name in discord["enabled"]:
        assert TOOL_TIERS.get(name, "execute") == "read_only"


def test_profile_application_ignores_tier_metadata(full_registry):
    """Mechanical no-expansion proof: apply_tool_profile() computes enabled
    sets from profile JSON / explicit lists only -- never from TOOL_TIERS.
    Applying All Tools to a non-owner registry must enable exactly
    universe - OWNER_ONLY_TOOLS, regardless of any tier entry."""
    apply_tool_profile(full_registry, profile_name="All Tools", owner=False)
    expected = set(full_registry.all_tool_names()) - OWNER_ONLY_TOOLS
    assert set(full_registry.list_enabled()) == expected


# ── 4. No disabled-tool resurrection ─────────────────────────────────────

def test_newly_classified_tool_not_resurrected_by_profile_application(
    isolated_agent_data, tmp_path, monkeypatch
):
    """A user-disabled newly-classified tool stays disabled through
    construction AND through a later profile application -- the
    TOOL-PROFILE-REFRESH-01 overlay contract must hold for the tools this
    slice classifies."""
    persistence.save({"disabled_tools": ["git_status"]})

    agent = _full_agent(owner=True, channel_id="tier-01-no-resurrect")
    assert not agent.registry.is_enabled("git_status")  # construction overlay

    apply_tool_profile(agent.registry, profile_name="Coding", owner=True)
    assert not agent.registry.is_enabled("git_status")  # still disabled


# ── 5. Owner-only boundary intact ────────────────────────────────────────

def test_owner_only_boundary_frozen():
    """The owner-only set itself is unchanged by this slice, and none of
    the newly classified tools are (or may become) owner-only."""
    assert OWNER_ONLY_TOOLS == {
        "create_tool", "list_custom_tools", "delete_tool",
        "list_pending_tools", "show_pending_tool_source", "reject_pending_tool",
        "palace_review_writes", "palace_undo_write",
        "start_process", "read_process", "send_process_input",
        "stop_process", "list_processes",
        "read_coding_checkpoint", "save_coding_checkpoint",
        "run_tests",
        "create_worktree", "list_worktrees", "remove_worktree",
        "set_project_root",
        "create_project",
    }
    assert NEWLY_CLASSIFIED.isdisjoint(OWNER_ONLY_TOOLS)


def test_create_project_stays_deliberately_absent_from_tier_map():
    """CODING-02B-A1's deliberate omission must survive this slice: an
    owner-only tool gets no tier entry (redundant behind the structural
    strip)."""
    assert "create_project" not in TOOL_TIERS


# ── 6. Non-owner PIN-gate behavior: the explicit authority delta ─────────

def _nonowner_agent_with_grants(isolated_agent_data, monkeypatch, grants):
    monkeypatch.setattr(config, "SUBAGENTS_ENABLED", True)
    monkeypatch.setattr(config, "BACKGROUND_TASKS_ENABLED", True)
    agent = _full_agent(owner=False, channel_id="tier-01-pin-gate")
    apply_tool_profile(agent.registry, tools_enabled=grants, owner=False)
    return agent


def test_newly_classified_tool_is_pin_exempt_for_granted_non_owner(
    isolated_agent_data, monkeypatch
):
    """THE authority relaxation this slice makes, stated explicitly: a
    non-owner session explicitly granted git_status can dispatch it
    WITHOUT PIN verification, because the tool is now provably
    observational."""
    agent = _nonowner_agent_with_grants(
        isolated_agent_data, monkeypatch, ["git_status"]
    )
    assert agent.registry.is_enabled("git_status")
    allowed, reason = agent.registry._gate_fn("git_status")
    assert allowed is True
    assert reason == ""


def test_still_unclassified_tools_remain_pin_gated_for_non_owner(
    isolated_agent_data, monkeypatch
):
    """The fail-closed default is preserved for everything this slice
    deliberately left unclassified: execute-tier default means a granted
    non-owner session still needs PIN verification."""
    agent = _nonowner_agent_with_grants(
        isolated_agent_data, monkeypatch,
        ["update_project", "check_background_task"],
    )
    assert agent.registry.is_enabled("update_project")
    assert agent.registry.is_enabled("check_background_task")
    for name in ("update_project", "check_background_task"):
        allowed, reason = agent.registry._gate_fn(name)
        assert allowed is False, f"{name} must stay PIN-gated"
        assert "PIN" in reason


def test_nonowner_default_deny_intact_without_grants(isolated_agent_data):
    """A non-owner session with no resolvable profile/grant still enables
    nothing -- tier entries anywhere in the map cannot bypass default-deny."""
    agent = _full_agent(owner=False, channel_id="tier-01-default-deny")
    apply_tool_profile(agent.registry, profile_name=None,
                       tools_enabled=None, owner=False)
    assert agent.registry.list_enabled() == []
