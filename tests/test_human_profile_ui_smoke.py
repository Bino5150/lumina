"""
MB-08 Part 2 UI smoke test — throwaway, same convention as
test_owner_isolation.py / test_discord_persona_isolation.py.

Confirms UserProfileTab._save() and GeneralTab._apply_prompt() no longer
statically bake human_bio into self.agent.ctx.system_prompt (the pattern
already removed from core/agent.py in Part 1), and that the dynamic
human_block path in ContextManager._build_system_prompt() still delivers
the bio correctly through build_messages() after both UI actions fire.

Real LuminaAgent, real GeneralTab/UserProfileTab, real persona applied,
real button-equivalent method calls — nothing mocked. Only prefs.json is
redirected to a throwaway temp file so this doesn't touch the real bio.

Run headless from repo root:
    QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 tests/test_human_profile_ui_smoke.py
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
import tempfile

PASS, FAIL = [], []


def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}  {detail}")


def main():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)

    import core.persistence as persistence
    tmp_dir = tempfile.mkdtemp()
    persistence.PREFS_PATH = os.path.join(tmp_dir, "prefs.json")

    BIO_TEXT = "Bino is a software engineer building Lumina."
    persistence.save({
        "human_bio": BIO_TEXT,
        "human_bio_public": "",
        "human_profile_curated": "",
    })

    from core.agent import LuminaAgent
    from core.personas import load_persona
    from ui.settings import GeneralTab, UserProfileTab
    from ui.main_window import COLORS

    agent = LuminaAgent(owner=True, channel_id="mb08-ui-smoke")
    persona = load_persona("personas/lumina.json")
    agent.apply_persona(persona)

    # Sanity: dynamic path delivers bio right after construction + persona apply
    msgs = agent.ctx.build_messages()
    check(
        "sanity: bio present via build_messages() before any UI action",
        BIO_TEXT in msgs[0]["content"],
    )

    general_tab = GeneralTab(agent, COLORS)
    profile_tab = UserProfileTab(agent, COLORS)

    # ── UserProfileTab._save() (Step 1's method) ──────────────────────────
    profile_tab.human_bio.setPlainText(BIO_TEXT)
    profile_tab._save()

    check(
        "UserProfileTab._save(): static system_prompt field has NO '## About'",
        "## About" not in agent.ctx.system_prompt,
        f"tail: ...{agent.ctx.system_prompt[-200:]}",
    )

    msgs = agent.ctx.build_messages()
    check(
        "UserProfileTab._save(): dynamic build_messages() STILL delivers bio",
        BIO_TEXT in msgs[0]["content"],
    )

    # ── GeneralTab._apply_prompt() (Step 2's method) ──────────────────────
    # self.prompt is pre-populated with config.SYSTEM_PROMPT in _build(); use
    # it as-is, same as a user hitting "Apply Change" without editing the text.
    general_tab._apply_prompt()

    check(
        "GeneralTab._apply_prompt(): static system_prompt field has NO '## About'",
        "## About" not in agent.ctx.system_prompt,
        f"tail: ...{agent.ctx.system_prompt[-200:]}",
    )

    msgs = agent.ctx.build_messages()
    check(
        "GeneralTab._apply_prompt(): dynamic build_messages() STILL delivers bio",
        BIO_TEXT in msgs[0]["content"],
    )

    print(f"\n{'='*60}")
    print(f"PASS: {len(PASS)}   FAIL: {len(FAIL)}")
    if FAIL:
        print("\nFailed checks:")
        for f in FAIL:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print("\nAll checks passed. UI actions no longer re-bake bio statically.")
        sys.exit(0)


if __name__ == "__main__":
    main()
