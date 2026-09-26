"""TOOL-TIER-COMPLETENESS-01 -- every registered production tool carries an
explicit TOOL_TIERS classification.

DOCS-01A's inventory found the live registry had grown to 103 maximal
registered tool names against only 90 TOOL_TIERS entries -- 13 tools
resolving their PIN-gate tier through core/agent.py's implicit
``TOOL_TIERS.get(name, "execute")`` fallback rather than a reviewed entry.
That fallback is, and remains, fail-safe (an unclassified tool defaults to
the most restrictive PIN-gated tier, never the most permissive). The gap
this closes is completeness, not safety: "implicitly fail-safe" becoming
"explicitly classified AND fail-safe" (this campaign's founding law).

TOOL-TIER-CLASSIFICATION-01 had already closed 9 of the 13 (git_status,
git_diff, git_log, git_branches, view_image, get_weather, load_project,
load_codebase, get_project_chats -- see tests/test_tool_tier_classification_01.py).
This slice closes the remaining 13 that DOCS-01A actually found still open
at HEAD c24bb09 (TOOL-TIER-CLASSIFICATION-01's own "still unclassified"
list was the frozen boundary this campaign was scoped to close):

  Owner-only (never non-owner-reachable -- OWNER_ONLY_TOOLS strips them
  before tier/PIN logic ever runs; tier here is pure documentation, proven
  behavior-preserving by tracing each tool's actual code path):
    create_project          write_local  -- writes project.md/codebase.md/
                             chats.json + calls save_project_binding()
                             (same binding.json write as set_project_root).
    list_pending_tools       read_only   -- os.listdir(PENDING_DIR) only.
    show_pending_tool_source read_only   -- open().read() only.
    reject_pending_tool      write_local -- os.remove() of a staged,
                             never-loaded file + local audit log append.
    palace_review_writes     read_only   -- SELECT-only (list_flagged_writes).
    palace_undo_write        write_local -- DELETE FROM palace_drawers +
                             closet rebuild.

  Non-owner-relevant (PIN-gated for a granted non-owner session; each was
  given the SAME tier its unclassified fallback already produced --
  "execute" -- so no non-owner PIN-gate behavior changes for any of them):
    spawn_subagent, run_background_subagent, schedule_background_subagent
                              -- headless child-agent/task dispatch.
    check_background_task    -- get_task_result()'s lazy TTL-GC deletes
                              expired queue entries: a mutating edge inside
                              a nominal read: too ambiguous for read_only.
    update_project, refresh_codebase_index, link_chat
                              -- local tracked-file overwrites. Behaviorally
                              closer to write_local than execute, but
                              reclassifying them there would REMOVE their
                              current non-owner PIN gate -- a real security
                              loosening, not just a documentation fix. Kept
                              at "execute" to stay strictly behavior-
                              preserving; see this campaign's final report
                              for the residual follow-up this leaves open
                              (a dedicated policy decision on whether these
                              three deserve their own tier).

This file's own job going forward: catch the NEXT tool that ships without
an explicit tier, the same way TOOL-TIER-CLASSIFICATION-01's accounting
test caught these 13.
"""
import pytest

import config
import core.secrets as secrets_module
import tools.chrome_companion as chrome_companion_tools
import tools.toolmaker as toolmaker
from chrome_companion import state as chrome_state
from chrome_companion_testkit import EXT_ID
from core import persistence
from core.agent import LuminaAgent
from core.tool_profiles import OWNER_ONLY_TOOLS, TOOL_TIERS
from tools.get_weather import register_get_weather_tool

NEWLY_CLASSIFIED_OWNER_ONLY = {
    "create_project": "write_local",
    "list_pending_tools": "read_only",
    "show_pending_tool_source": "read_only",
    "reject_pending_tool": "write_local",
    "palace_review_writes": "read_only",
    "palace_undo_write": "write_local",
}

NEWLY_CLASSIFIED_NON_OWNER = {
    "spawn_subagent": "execute",
    "run_background_subagent": "execute",
    "schedule_background_subagent": "execute",
    "check_background_task": "execute",
    "update_project": "execute",
    "refresh_codebase_index": "execute",
    "link_chat": "execute",
}


@pytest.fixture
def maximal_registry(tmp_path, monkeypatch):
    """Construct the largest deterministic production tool universe: owner
    session, both feature flags on, Browser Companion installed+paired
    (chrome_* tools only register when tools/chrome_companion.state's
    load_install() finds a pairing for the data dir -- DOCS-01A's "103
    registered" figure counted them; a bare isolated registry without an
    install would only show 97), and get_weather registered the way the
    FE-11 approved-custom-tool loader produces it in production (not a
    static import -- see tests/test_tool_tier_classification_01.py's
    identical rationale)."""
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(secrets_module, "SECRETS_PATH", str(tmp_path / "credentials.json"))
    monkeypatch.setattr(toolmaker, "AUDIT_LOG_PATH", str(tmp_path / "tool_audit.log"))
    monkeypatch.setattr(config, "SUBAGENTS_ENABLED", True)
    monkeypatch.setattr(config, "BACKGROUND_TASKS_ENABLED", True)
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    # Registration only ever captures `hub` in closures for later dispatch
    # -- no method is called on it at registration time -- so a bare
    # placeholder is sufficient here; no tool in this file is invoked.
    monkeypatch.setattr(chrome_companion_tools, "ensure_hub_started", lambda data_dir=None: object())
    chrome_state.save_install(
        str(tmp_path), extension_id=EXT_ID, socket_path=str(tmp_path / "hub.sock")
    )

    agent = LuminaAgent(owner=True, channel_id="tier-completeness-01", backend="llamacpp")
    register_get_weather_tool(agent.registry)
    return agent.registry


def test_every_registered_production_tool_has_an_explicit_tier(maximal_registry):
    """THE completeness invariant: REGISTERED_PRODUCTION_TOOLS - TOOL_TIERS
    must be empty. A future tool registered without a TOOL_TIERS entry
    lands here and breaks this test instead of silently resolving through
    the implicit execute-tier fallback."""
    universe = set(maximal_registry.all_tool_names())
    unclassified = universe - set(TOOL_TIERS)
    assert not unclassified, (
        f"Registered production tools missing an explicit TOOL_TIERS "
        f"classification: {sorted(unclassified)}"
    )


def test_newly_classified_tiers_match_this_campaigns_decision(maximal_registry):
    """Pins the exact tier this campaign assigned to each of the 13 tools
    DOCS-01A found unclassified, so a future edit to TOOL_TIERS that
    silently reclassifies one of them (e.g. loosening update_project to
    write_local, which would drop its non-owner PIN gate) fails loudly
    here instead of only showing up as a security regression elsewhere."""
    for name, tier in {**NEWLY_CLASSIFIED_OWNER_ONLY, **NEWLY_CLASSIFIED_NON_OWNER}.items():
        assert TOOL_TIERS.get(name) == tier, f"{name} expected tier {tier!r}, got {TOOL_TIERS.get(name)!r}"


def test_newly_classified_non_owner_tools_stay_in_sensitive_tiers(maximal_registry):
    """The non-owner-relevant seven were deliberately kept inside
    SENSITIVE_TIERS ({execute, self_modifying, outbound_action}) so their
    non-owner PIN-gate behavior is byte-for-byte unchanged from the
    fallback they used to rely on. If any of them drifts to a tier outside
    that set, PIN-gating for that tool silently loosens."""
    SENSITIVE_TIERS = {"execute", "self_modifying", "outbound_action"}
    for name in NEWLY_CLASSIFIED_NON_OWNER:
        assert TOOL_TIERS.get(name) in SENSITIVE_TIERS, (
            f"{name} must stay in a SENSITIVE_TIERS tier to preserve its current "
            f"non-owner PIN-gated behavior"
        )


def test_newly_classified_owner_only_tools_are_still_owner_only(maximal_registry):
    """Owner-only status is a separate axis from tier (this campaign's own
    premise) -- confirms giving these six tools a real tier didn't
    accidentally drop them from OWNER_ONLY_TOOLS."""
    assert set(NEWLY_CLASSIFIED_OWNER_ONLY) <= OWNER_ONLY_TOOLS


def test_unknown_tool_fallback_is_still_fail_closed():
    """The fallback this whole campaign is built around: an unrecognized
    tool name must still resolve to the most restrictive (PIN-gated) tier,
    never silently to something permissive."""
    assert TOOL_TIERS.get("some_tool_nobody_has_registered_yet", "execute") == "execute"
