# Getting Started

## Requirements

- Linux (Ubuntu/Mint/Debian recommended; VM/WSL2/etc. work)
- Windows and macOS are compatible but still in the testing phase — beta testers needed
- NVIDIA GPU with CUDA 12.x for local inference (4GB VRAM minimum, 8GB+ recommended)
- Python 3.10+ (miniconda/conda recommended)
- ~10GB disk space for Lumina + a local model + dependencies

## Install

```bash
git clone https://github.com/bino5150/lumina.git
cd lumina
pip install -r requirements.txt
playwright install chromium
```

### macOS

```bash
brew install portaudio
pip install -r requirements.txt --break-system-packages
```

## Configure

Copy `config.example.py` to `config.py` and point it at your llama.cpp
server (or whichever backend you're using) and preferred model:

```bash
cp config.example.py config.py
```

`config.example.py` is kept byte-identical to `config.py`'s full set of
public settings (~84 keys) as a standing test requirement
(`tests/test_config_example_parity.py`) — every setting you can configure
has a real default in the template you just copied, so a fresh install
never crashes Settings on a missing key.

## Launch

```bash
lumina   # alias to start_lumina.sh — starts TTS + UI
```

`main.py` also accepts:

- `--cli` — run in CLI mode instead of the desktop UI
- `--persona NAME` — start with a specific persona loaded
- `--tools PROFILE` — start with a specific tool profile active

### Relevant environment variables

| Variable | Purpose |
|---|---|
| `LUMINA_DATA_DIR` | Overrides the default data directory (`~/.local/share/lumina`) — used to run isolated installs/builds side by side. |
| `LUMINA_BROWSER_HEADLESS` | Set to `0` to watch the Playwright browser session instead of running it headless (default `1`). |
| `LUMINA_SECRETS_PATH` | Overrides where credentials (API keys, Higgsfield keys, Telegram token) are stored, outside `prefs.json` and outside version control (default `~/.config/lumina/credentials.json`, mode `0600`). |
| `LUMINA_CHATGPT_AUTH_DIR` | Overrides where ChatGPT sign-in sessions are stored (default `~/.config/lumina/chatgpt/`, owner-only, never inside the data directory). |
| `LUMINA_TESTING` | Test-harness only — do not set this for normal use. |

## First-run notice

If you're doing a fresh install, or updating from a build prior to the
system-prompt/identity migration off tracked source, open **Settings →
General** and click **Save All Settings** once. This writes the current
Global Agent Behavior Prompt, dreaming, and think-block values into
`prefs.json` so they persist across restarts and future `git pull`s —
otherwise they silently fall back to whatever default ships in the new
`config.py` on relaunch.

**Apply Changes** (bottom-left) is a session-only test of settings — it
does not write to `prefs.json` and does not persist past a relaunch.
**Save All Settings** (bottom-right) is what locks your preferences in.

## Where to go next

- [Settings](settings.md) for backend selection and every Settings tab
- [Memory](memory.md) for how Lumina remembers things
- [Security & Authority](security.md) for the trust model before you connect a remote channel
