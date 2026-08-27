---
name: run-lumina
description: Build, run, and screenshot Lumina (PySide6 desktop AI agent app). Use when asked to start Lumina, launch main.py, run its tests, or take a screenshot of its Settings/chat UI to confirm a UI change works.
---

Lumina is a PySide6 desktop GUI app (`main.py` → `run_gui()`). Drive it
headlessly via `.Codex/skills/run-lumina/driver.py` under
`QT_QPA_PLATFORM=offscreen` — no Xvfb/xdotool needed, Qt's own
offscreen platform plugin rasterizes real widgets and
`widget.grab().save(path)` produces real PNG screenshots. The driver
has two modes: `full` (the actual app — TTS/STT/agent/window, slow) and
`widget` (a single Settings tab in isolation, fast). Pick based on what
you're testing — see "Run (agent path)" below.

## Prerequisites

Runs fine as-is in this container — `python3` already resolves to
`~/miniconda3/bin/python3` (3.13) with all deps installed, CUDA GPU
present (`nvidia-smi` shows a Quadro T1000). No `apt-get` packages were
needed; the offscreen path doesn't touch X11 at all.

## Setup

```bash
pip install -r requirements.txt --break-system-packages
playwright install chromium
```

(Not re-run this session — deps were already present. This is the
command `requirements.txt`'s own header specifies.)

## Run (agent path)

```bash
cd ~/lumina-release   # or ~/lumina — driver resolves project root relative to itself either way

# Fast path — iterate on a Settings tab without booting TTS/STT/agent.
# Use this for the overwhelming majority of ui/settings.py changes.
python3 .Codex/skills/run-lumina/driver.py widget \
  --out /tmp/lumina_shots --tab GeneralTab \
  --switch-backend llamacpp,gemini,anthropic,omniroute

# Real path — boots the actual TTS backend (loads a model onto GPU),
# STT, LuminaAgent, and LuminaWindow; clicks the real Settings nav
# button. Use once to confirm a change survives full app boot, or when
# testing something TTS/STT/agent-wiring related that `widget` mode
# can't reach.
python3 .Codex/skills/run-lumina/driver.py full --out /tmp/lumina_shots
```

Screenshots land in `--out` (default `/tmp/lumina_shots`) as PNGs —
`widget_<Tab>_<backend>.png` per switch, or `full_01_chat.png` /
`full_02_settings.png`. Console output for `widget` mode also prints
the live values of any `ctx_spin`/`mem_spin`/`result_spin` widgets so
you don't have to eyeball the screenshot to confirm a value — but look
at the screenshot too, a blank/garbled render is a real failure the
printed values won't catch.

`--tab` accepts any class name from `ui/settings.py` (`GeneralTab`,
`UserProfileTab`, `MemoryTab`, `ToolsTab`, `TTSTab`, etc.) — they all
take `(agent, colors_dict)`; the driver stubs `agent` with a
`SimpleNamespace(owner=True, current_persona=None, tts=None,
registry=None)`, which is enough for construction and dropdown/spinbox
interaction. It is **not** enough to click that tab's Save/Apply button
— those touch `self.agent.llm`/`self.agent.registry`/`self.agent.tts`
and will throw against the stub. If you need to test a Save handler,
use `full` mode instead, where `agent` is a real `LuminaAgent`.

## Run (human path)

```bash
python3 main.py          # opens a real window — Ctrl-C or close it to quit
python3 main.py --cli    # terminal-only chat loop, no Qt at all
```

Useless in this headless container (no display) — only for a real
desktop session.

## Test

```bash
python3 -m pytest tests/ -q
```

74 passed, ~34s, in this session.

---

## Gotchas

- **First `full` run downloads a model.** Chatterbox TTS pulls
  `ResembleAI/chatterbox-turbo` from Hugging Face on first use — that
  run took long enough to exceed a 25s timeout in testing. Once cached
  (`~/.cache/huggingface/`), a warm `full` run completes in ~5s.
- **`full` mode needs no Discord/Telegram credentials to boot.**
  `LuminaWindow.__init__` doesn't wire either bridge at startup — only
  `init_chat_db()` and the UI build run, so a fresh container with no
  bot tokens configured still reaches a real, screenshotable window.
- **A real backend doesn't need to be reachable.** With `LLM_BACKEND =
  omniroute` and no OmniRoute server running on `localhost:20128`, the
  app still boots and shows the window; it just posts "Cannot reach LM
  Studio at http://localhost:20128/v1: ..." to the status bar instead
  of crashing. Don't spin up a real backend just to screenshot the UI.
- **`prefs.json` is shared between `~/lumina` and `~/lumina-release`.**
  Both read `config.DATA_DIR` (`user_data_dir("lumina")`, not the repo
  dir), so a per-backend override saved from one checkout shows up in
  the other. A spinbox showing something other than
  `BACKEND_CONTEXT_DEFAULTS` (e.g. llamacpp's Max Context Tokens
  reading 19000 instead of the 16384 default) is very likely this, not
  a bug — check `saved.get(...)` in `ui/settings.py` before assuming
  breakage.
- **`propagateSizeHints()` warning on every offscreen launch** — comes
  from Qt's offscreen platform plugin, harmless, not worth chasing.

## Troubleshooting

- **`KeyError: 'bg_card'` (or any other color key) constructing a
  Settings tab directly**: you passed a hand-rolled color dict instead
  of the real one. Use `from ui.main_window import COLORS` (what the
  driver does) — the tabs' `_btn()`/`_lbl()` helpers index into it with
  specific keys the app actually defines.
- **Third column of a Settings row gets clipped in the screenshot**:
  the widget's default width doesn't fit 3 side-by-side fields. Call
  `tab.resize(1100, 700)` (or wider) before `.grab()` — the driver
  already does this.
