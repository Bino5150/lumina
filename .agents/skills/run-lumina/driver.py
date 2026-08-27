#!/usr/bin/env python3
"""
Driver for launching and screenshotting the Lumina PySide6 desktop app
headlessly (QT_QPA_PLATFORM=offscreen — no Xvfb needed, Qt's own
offscreen platform plugin rasterizes real widgets).

Two modes, pick based on what you're actually testing:

  full     Boots the REAL app: TTS backend (loads an actual model onto
           GPU if CUDA is available), STT bridge, LuminaAgent, and the
           full LuminaWindow. Slow (15-30s+, dominated by TTS model
           load) but end-to-end real. Use this to confirm a change that
           touches agent/TTS/STT wiring, or once per session as a
           smoke test that the app still boots at all.

  widget   Instantiates a single Settings tab widget directly (e.g.
           GeneralTab) under a real QApplication with a lightweight
           stub agent. Skips TTS/STT/agent init entirely — starts in
           under a second. Use this for iterating on Settings-tab UI
           changes (the overwhelming majority of ui/settings.py edits)
           where you don't need a live backend connection.

Usage:
  python3 driver.py full   --out /tmp/lumina_shots
  python3 driver.py widget --out /tmp/lumina_shots --tab GeneralTab \
      --switch-backend llamacpp,gemini,anthropic,omniroute

Run from the project root (the directory containing main.py/config.py).
"""
import os, sys, argparse, time

# Data-dir isolation — this build's own driver copy only (NOT shared with
# ~/lumina's identical-looking driver.py). This is a direct `import config`
# entry point that bypasses start_lumina.sh entirely, so without this it
# would silently fall through to the shared platformdirs default and read/write
# the dev build's real memory/chat state. setdefault so an explicit
# LUMINA_DATA_DIR already in the environment (e.g. a future test harness)
# still wins.
os.environ.setdefault("LUMINA_DATA_DIR", os.path.join(os.path.expanduser("~"), ".local", "share", "lumina-release"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
# .claude/skills/run-lumina/driver.py -> walk up 3 to project root
PROJECT_ROOT = os.path.abspath(os.path.join(PROJECT_ROOT, "..", "..", ".."))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)


def _mkout(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    return out_dir


def run_full(out_dir):
    """Real end-to-end boot: TTS + STT + LuminaAgent + LuminaWindow."""
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QFont
    import config
    from core.agent import LuminaAgent
    from ui.main_window import LuminaWindow

    app = QApplication(sys.argv)
    app.setApplicationName("Lumina")

    from tts.loader import get_tts_backend
    from stt.whisper_bridge import WhisperBridge
    tts = get_tts_backend()
    stt = WhisperBridge(model_size=config.STT_MODEL, device=config.STT_DEVICE) if config.STT_ENABLED else None
    agent = LuminaAgent(tts=tts)

    window = LuminaWindow(agent, stt=stt)

    def on_tool_call(name, args):
        window.chat_widget.add_tool_indicator(name, args)
    agent.on_tool_call = on_tool_call

    window.resize(1280, 800)
    window.show()
    app.processEvents()
    time.sleep(0.2)
    app.processEvents()

    path = os.path.join(out_dir, "full_01_chat.png")
    window.grab().save(path)
    print(f"[full] chat window -> {path}")

    # Drive: click the real Settings nav button (not a shortcut around it).
    window.btn_settings_nav.click()
    app.processEvents()
    time.sleep(0.1)
    app.processEvents()
    path = os.path.join(out_dir, "full_02_settings.png")
    window.grab().save(path)
    print(f"[full] settings panel -> {path}")

    window.close()
    app.quit()
    print("[full] done")


def run_widget(out_dir, tab_name, switch_backends):
    """Fast, isolated Settings-tab widget test — no TTS/STT/agent boot."""
    from types import SimpleNamespace
    from PySide6.QtWidgets import QApplication
    import config
    import ui.settings as settings_mod
    from ui.main_window import COLORS

    app = QApplication(sys.argv)

    TabClass = getattr(settings_mod, tab_name)
    stub_agent = SimpleNamespace(owner=True, current_persona=None, tts=None, registry=None)
    tab = TabClass(stub_agent, COLORS)
    tab.resize(1100, 700)
    tab.show()
    app.processEvents()

    path = os.path.join(out_dir, f"widget_{tab_name}_initial.png")
    tab.grab().save(path)
    print(f"[widget] {tab_name} initial -> {path}")
    if hasattr(tab, "backend_combo"):
        print("  backend:", config.LLM_BACKEND)
    if hasattr(tab, "result_spin"):
        print("  Tool Result Max Chars:", tab.result_spin.value())
    if hasattr(tab, "ctx_spin"):
        print("  Max Context Tokens:", tab.ctx_spin.value())
    if hasattr(tab, "mem_spin"):
        print("  Memory Inject Limit:", tab.mem_spin.value())

    for backend in switch_backends:
        if not hasattr(tab, "backend_combo"):
            print(f"  (tab {tab_name} has no backend_combo — skipping switch to {backend})")
            continue
        tab.backend_combo.setCurrentText(backend)
        app.processEvents()
        default = config.BACKEND_CONTEXT_DEFAULTS.get(backend, config.BACKEND_CONTEXT_DEFAULTS["llamacpp"])
        print(f"\n--- switched to: {backend} ---")
        if hasattr(tab, "ctx_spin"):
            print("  ctx_spin    =", tab.ctx_spin.value(), " default:", default["max_context_tokens"])
        if hasattr(tab, "mem_spin"):
            print("  mem_spin    =", tab.mem_spin.value(), " default:", default["memory_inject_limit"])
        if hasattr(tab, "result_spin"):
            print("  result_spin =", tab.result_spin.value(), " default:", default["tool_result_max_chars"])
        path = os.path.join(out_dir, f"widget_{tab_name}_{backend}.png")
        tab.grab().save(path)
        print(f"  screenshot -> {path}")

    print("\n[widget] done")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="mode", required=True)

    p_full = sub.add_parser("full")
    p_full.add_argument("--out", default="/tmp/lumina_shots")

    p_widget = sub.add_parser("widget")
    p_widget.add_argument("--out", default="/tmp/lumina_shots")
    p_widget.add_argument("--tab", default="GeneralTab")
    p_widget.add_argument("--switch-backend", default="",
                           help="comma-separated backend names to switch through, e.g. llamacpp,gemini")

    args = p.parse_args()
    out_dir = _mkout(args.out)

    if args.mode == "full":
        run_full(out_dir)
    else:
        backends = [b.strip() for b in args.switch_backend.split(",") if b.strip()]
        run_widget(out_dir, args.tab, backends)
