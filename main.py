"""
Lumina — Entry Point
Run with GUI:  python main.py
Run CLI only:  python main.py --cli
"""

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


def run_cli():
    """Terminal-only mode for testing without PySide6."""
    from core.agent import LuminaAgent
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

    agent = LuminaAgent(on_tool_call=on_tool_call, on_tool_result=on_tool_result)

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
        response = agent.chat(user_input)
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


if __name__ == "__main__":
    if "--cli" in sys.argv:
        run_cli()
    else:
        try:
            run_gui()
        except ImportError as e:
            print(f"\nPySide6 not found ({e}). Falling back to CLI mode.\n")
            run_cli()
