"""
Lumina — Entry Point
Run with GUI:  python main.py
Run CLI only:  python main.py --cli
CLI persona:   python main.py --cli --persona Lumina
CLI tool override (per-run only, doesn't touch any file):
               python main.py --cli --persona Lumina --tools Coding
"""

import argparse
import sys
import os

# Data-dir isolation — this build's own main.py only (NOT shared with
# ~/lumina's identical-looking main.py). Covers every launch path that goes
# straight through this file (start_lumina.sh, an IDE debugger pointed at
# main.py, or a bare `python main.py`) without depending on start_lumina.sh
# having run first. config.py reads LUMINA_DATA_DIR at import time, so this
# must be set before the first `import config` below. setdefault so an
# already-exported value (e.g. from start_lumina.sh) always wins.
os.environ.setdefault("LUMINA_DATA_DIR", os.path.join(os.path.expanduser("~"), ".local", "share", "lumina-release"))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def apply_cli_persona_and_tools(agent, persona_name: str = None, tools_override: str = None) -> list[str]:
    """MB-22: resolve --persona / --tools against a live agent, in order.

    Pulled out of run_cli() as its own function so the lookup/apply/message
    logic is unit-testable against a lightweight fake agent (no real
    LuminaAgent/backend/DB needed) -- run_cli() itself can't be exercised
    that cheaply since it also owns the interactive input() loop.

    Returns the list of status lines (also printed here) so callers/tests
    can assert on them without scraping stdout. Order matches run_cli()'s
    prior inline behavior: persona (and its own tools_profile, via
    apply_persona()) first, then --tools as a per-invocation override on
    top -- so --tools always wins if both are given.
    """
    from core.personas import find_persona_by_name
    from core.tool_profiles import apply_tool_profile, find_profile_by_name

    messages = []

    if persona_name:
        persona = find_persona_by_name(persona_name)
        if persona is None:
            messages.append(f"✗ No persona named '{persona_name}' found in personas/. Running with base config.")
        else:
            agent.apply_persona(persona)
            tools_label = persona.get("tools_profile") or ("inline tool list" if persona.get("tools_enabled") else "unchanged")
            messages.append(f"✓ Persona: {persona.get('name')} (tools: {tools_label})")

    if tools_override:
        if find_profile_by_name(tools_override) is None:
            messages.append(f"✗ No tool profile named '{tools_override}' found. Tool set unchanged.")
        else:
            apply_tool_profile(agent.registry, profile_name=tools_override, owner=agent.owner)
            messages.append(f"✓ Tool profile override: {tools_override}")

    for m in messages:
        print(f"  {m}\n")
    return messages


def run_cli(persona_name: str = None, tools_override: str = None):
    """Terminal-only mode for testing without PySide6.

    persona_name: MB-22 -- optional persona to load by name (core/personas.py),
        same effect as picking it in the desktop app's persona sidebar: applies
        its system prompt, voice, and its own tools_profile via apply_persona().
        None (default) preserves current behavior -- base config, no persona
        applied, matching how the desktop app itself behaves with no saved
        last_persona pref.
    tools_override: optional tool profile name (core/tool_profiles.py) applied
        AFTER persona load, for this invocation only. Mirrors the existing
        `backend: str = None` override pattern on LuminaAgent.__init__ -- an
        explicit, per-call opt-in that never touches any file on disk. Useful
        standalone too (no --persona needed) for a one-off run with the base
        identity but a trimmed tool set.
    """
    from core.agent import LuminaAgent
    from core.reasoning_preferences import resolve_reasoning_effort
    import config

    def on_tool_call(name, args):
        print(f"\n  ⚙ [{name}] {args if args else ''}", flush=True)

    def on_tool_result(name, result):
        preview = result[:120].replace('\n', ' ')
        print(f"  ✓ {preview}{'...' if len(result) > 120 else ''}", flush=True)

    print(f"\n{'='*52}")
    print(f"  LUMINA v0.2.7-beta.2 — CLI Mode")
    print(f"  Backend: {config.LLM_BACKEND} ({config.LLM_BACKEND_URL})")
    print(f"{'='*52}\n")

    # MB-22: CLI is a trusted local session, same footing as the desktop app --
    # owner=True is already LuminaAgent's default, unchanged here. channel_id
    # is now explicit ("cli-local" instead of the generic "default") so PIN
    # verification/lockout state (tools/pin.py) never shares a bucket with any
    # other owner=True call site that also left channel_id at its default.
    agent = LuminaAgent(on_tool_call=on_tool_call, on_tool_result=on_tool_result,
                         channel_id="cli-local")

    apply_cli_persona_and_tools(agent, persona_name, tools_override)

    try:
        status = agent.test_connection()
        print(f"  ✓ {status}\n")
    except ConnectionError as e:
        print(f"  ✗ {e}\n")
        sys.exit(1)

    print("  Enter to chat. 'quit' to exit. 'tokens' for context usage.\n")

    while True:
        try:
            user_input = input(f"{config.USER_NAME}: ").strip()
        except (KeyboardInterrupt, EOFError):
            print(f"\n\n  Later, {config.USER_NAME}. ✦\n")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit"):
            print(f"\n  Later, {config.USER_NAME}. ✦\n")
            break
        if user_input.lower() == "tokens":
            print(f"  Context: ~{agent.get_token_count()} tokens\n")
            continue

        print(f"\n{config.AGENT_NAME}: ", end="", flush=True)
        # Patch 3A.4 Part 4 -- resolved FRESH every turn (never cached/
        # memoized) against the actual live backend instance in use right
        # now, since Settings can swap it during the app's lifetime. agent
        # is always a real LuminaAgent here (run_cli() constructs it just
        # above), so agent.llm and LuminaAgent.chat()'s reasoning_effort
        # param are both always present -- no compatibility guard needed
        # at this call site (unlike ui/main_window.py's AgentWorker, which
        # must also tolerate pre-3A.4 GUI-test agent stubs).
        reasoning_effort = resolve_reasoning_effort(agent.llm)
        response = agent.chat(user_input, reasoning_effort=reasoning_effort)
        print(response + "\n")


def run_gui():
    """Full PySide6 GUI mode."""
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QFont
    from core.agent import LuminaAgent
    from ui.main_window import LuminaWindow
    import config

    app = QApplication(sys.argv)
    app.setApplicationName("Lumina")
    app.setApplicationVersion("0.2.7-beta.2")

    # Set default font — prefer monospace
    for font_name in ["JetBrains Mono", "Fira Code", "Cascadia Code", "Monospace"]:
        font = QFont(font_name, 12)
        if font.exactMatch() or font_name == "Monospace":
            app.setFont(font)
            break

    # Create agent — wire tool calls to UI later via signals
    from tts.loader import get_tts_backend
    from stt.whisper_bridge import WhisperBridge
    tts = get_tts_backend()
    # FE-16: this used to hardcode base/cpu regardless of what the Settings
    # tab persisted (STT_MODEL/STT_DEVICE), and never checked STT_ENABLED.
    stt = WhisperBridge(model_size=config.STT_MODEL, device=config.STT_DEVICE) if config.STT_ENABLED else None
    agent = LuminaAgent(tts=tts)

    window = LuminaWindow(agent, stt=stt)

    # Wire tool call callbacks to chat widget after window is built
    def on_tool_call(name, args):
        window.chat_widget.add_tool_indicator(name, args)

    agent.on_tool_call = on_tool_call

    window.show()
    sys.exit(app.exec())


def build_arg_parser() -> argparse.ArgumentParser:
    """MB-22: --persona and --tools are CLI-only flags -- run_gui() takes none
    of its own today, and the GUI's own persona/profile pickers already cover
    that surface there. Parsed regardless of --cli so the GUI fallback path
    (PySide6 missing) also gets to honor them."""
    parser = argparse.ArgumentParser(
        description="Lumina -- local-first AI agent. Run with no flags for the "
                    "GUI, or --cli for terminal mode."
    )
    parser.add_argument("--cli", action="store_true",
                         help="Terminal-only mode without PySide6.")
    parser.add_argument("--persona", type=str, default=None, metavar="NAME",
                         help="Load a persona by name (e.g. --persona Lumina) -- "
                              "applies its system prompt, voice, and tool profile, "
                              "same as switching personas in the desktop app. "
                              "Omit to run with base config defaults.")
    parser.add_argument("--tools", type=str, default=None, metavar="PROFILE",
                         help="Override the tool profile for this run only "
                              "(e.g. --tools Coding), applied after --persona if "
                              "both are given. Does not modify any file on disk.")
    return parser


if __name__ == "__main__":
    args = build_arg_parser().parse_args()
    if args.cli:
        run_cli(persona_name=args.persona, tools_override=args.tools)
    else:
        try:
            run_gui()
        except ImportError as e:
            print(f"\nPySide6 not found ({e}). Falling back to CLI mode.\n")
            run_cli(persona_name=args.persona, tools_override=args.tools)
