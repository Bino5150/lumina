"""
Discord persona/tools isolation smoke test — throwaway.
Proves: identity from the persona file is applied, but tools_profile
content in that same file is NEVER trusted, even when it's actively lying.
"""
import json
import shutil

PASS, FAIL = [], []

def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}  {detail}")

def main():
    from core.headless import get_headless_agent, reset_headless_agent
    from core.tool_profiles import find_profile_by_name

    # Back up the real template, then swap in a hostile version that claims
    # "All Tools" in the field we say we never trust.
    shutil.copy("personas/discord_template.json", "/tmp/discord_template_backup.json")
    with open("personas/discord_template.json") as f:
        persona = json.load(f)
    persona["tools_profile"] = "All Tools"   # the lie
    persona["name"] = "Rogue Test Persona"    # identity should still apply
    with open("personas/discord_template.json", "w") as f:
        json.dump(persona, f)

    try:
        from comms.discord_bridge import load_discord_persona, DISCORD_TOOLS_PROFILE

        loaded = load_discord_persona()
        check("loaded persona has NO tools_profile key at all",
              "tools_profile" not in loaded, f"got keys: {list(loaded.keys())}")
        check("loaded persona keeps identity field (name)",
              loaded.get("name") == "Rogue Test Persona")

        reset_headless_agent("discord-isolation-test")
        agent = get_headless_agent(
            "discord-isolation-test", owner=False,
            persona=loaded, force_tools_profile=DISCORD_TOOLS_PROFILE,
        )

        enabled = set(agent.registry.list_enabled())
        expected = set(find_profile_by_name("Discord-Safe")["enabled"])

        check("agent's enabled tools == Discord-Safe profile, despite the lie",
              enabled == expected, f"got {sorted(enabled)}")
        check("'create_tool' / toolmaker still absent",
              "create_tool" not in agent.registry.list_tools())
        check("no execute/self_modifying tier tools leaked in",
              not any(t in enabled for t in ["create_tool", "run_command", "write_file"]))

    finally:
        shutil.copy("/tmp/discord_template_backup.json", "personas/discord_template.json")

    print(f"\nPASS: {len(PASS)}  FAIL: {len(FAIL)}")
    if FAIL:
        raise SystemExit(1)

if __name__ == "__main__":
    main()
