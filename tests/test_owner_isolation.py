"""
Owner=False Isolation Smoke Test
Throwaway script — run once from repo root, then delete.

Confirms the last open item from the Epic A trace:
  - owner=False sessions start with registry.list_enabled() EMPTY
  - toolmaker tools (create_tool, etc.) are structurally absent from
    list_tools() for owner=False — not just disabled, never registered
  - owner=True regression: normal launch still works, toolmaker present,
    tools enabled as expected

Run from the ~/lumina (OG dev build) root:
    python test_owner_isolation.py

Exits 0 on pass, 1 on any failure — safe to wire into CI later if wanted.
"""

import sys

PASS = []
FAIL = []


def check(label, condition, detail=""):
    if condition:
        PASS.append(label)
        print(f"  [PASS] {label}")
    else:
        FAIL.append(label)
        print(f"  [FAIL] {label}  {detail}")


def main():
    from core.agent import LuminaAgent

    # ── Test 1: owner=True regression ──────────────────────────────────
    print("\n=== owner=True (desktop) regression ===")
    owner_agent = LuminaAgent(owner=True, channel_id="smoketest-owner")
    owner_tools = owner_agent.registry.list_tools()
    owner_enabled = owner_agent.registry.list_enabled()

    check(
        "owner=True: registry has tools registered",
        len(owner_tools) > 0,
        f"got {len(owner_tools)} tools",
    )
    check(
        "owner=True: enabled tools non-empty (no accidental default-deny)",
        len(owner_enabled) > 0,
        f"got {len(owner_enabled)} enabled",
    )
    check(
        "owner=True: toolmaker tools present (create_tool)",
        "create_tool" in owner_tools,
        f"create_tool in list_tools()? {'create_tool' in owner_tools}",
    )
    check(
        "owner=True: registry.call() on a real tool doesn't hard-block",
        owner_agent.registry.call("create_tool", {}) != "[Tool error: 'create_tool' not found]",
    )

    # ── Test 2: owner=False isolation ──────────────────────────────────
    print("\n=== owner=False (channel/subagent) isolation ===")
    guest_agent = LuminaAgent(owner=False, channel_id="smoketest-guest")
    guest_tools = guest_agent.registry.list_tools()
    guest_enabled = guest_agent.registry.list_enabled()

    check(
        "owner=False: list_enabled() is EMPTY at construction",
        len(guest_enabled) == 0,
        f"got {len(guest_enabled)} enabled: {guest_enabled[:5]}...",
    )
    check(
        "owner=False: toolmaker tools structurally ABSENT from list_tools()",
        "create_tool" not in guest_tools,
        f"create_tool in list_tools()? {'create_tool' in guest_tools}",
    )
    check(
        "owner=False: calling create_tool returns 'not found', not 'disabled'",
        guest_agent.registry.call("create_tool", {"name": "x", "code": "x"})
        == "[Tool error: 'create_tool' not found]",
    )
    # Pick any tool that DOES exist for both owner/guest (e.g. web_search) and
    # confirm it's blocked pre-persona for the guest even though it's a real,
    # registered tool — this is the actual default-deny check.
    probe_tool = next((t for t in guest_tools if t in owner_tools), None)
    if probe_tool:
        result = guest_agent.registry.call(probe_tool, {})
        check(
            f"owner=False: real registered tool '{probe_tool}' is disabled pre-persona",
            "disabled" in result.lower(),
            f"got: {result[:80]}",
        )
    else:
        check("owner=False: found a shared probe tool to test disabled-call path", False, "no shared tool found")

    # ── Test 3: no cross-contamination between the two agent instances ──
    print("\n=== cross-contamination check ===")
    check(
        "owner=True agent's enabled set unaffected by owner=False construction",
        len(owner_agent.registry.list_enabled()) > 0,
        "owner agent's tools got disabled by the guest agent's construction — BAD",
    )

    # ── Summary ───────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"PASS: {len(PASS)}   FAIL: {len(FAIL)}")
    if FAIL:
        print("\nFailed checks:")
        for f in FAIL:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print("\nAll checks passed. owner=False isolation confirmed.")
        sys.exit(0)


if __name__ == "__main__":
    main()
