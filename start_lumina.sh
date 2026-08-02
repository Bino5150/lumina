#!/bin/bash
# ── Lumina Launcher ────────────────────────────────────────────────────────
# Start your LLM backend (llama.cpp, Ollama, LM Studio, etc.) before running
# this script. See README for recommended setup and configuration.

# ── Data-dir isolation ──────────────────────────────────────────────────────
# This build is the disposable release copy — it must NEVER read or write
# ~/lumina's (the dev build's) shared memory/chat/prefs state. config.py
# resolves DATA_DIR from this env var if set, falling back to a shared
# platformdirs path otherwise (see config.py's DATA_DIR line) — so this must
# be exported before config.py is ever imported, i.e. before anything else
# in this script runs. Same pattern eval/run_eval.py (MB-23) already proved
# out for isolating eval runs from the real data dir.
export LUMINA_DATA_DIR="$HOME/.local/share/lumina-release"

LUMINA_DIR="$(dirname "$0")"
# Was "$LUMINA_DIR/memory/prefs.json" — a stale pre-FE-13, BASE_DIR-relative
# path that never matched where prefs.json actually lives (DATA_DIR, not
# BASE_DIR, since the platformdirs migration). That meant this check almost
# always fell through to the "chatterbox" default regardless of the real
# configured backend — plausible root cause of prior backend-switching
# weirdness. Now points at this build's own isolated data dir.
PREFS="$LUMINA_DATA_DIR/memory/prefs.json"

# ── Detect active TTS backend from prefs.json ──────────────────────────────
if [ -f "$PREFS" ]; then
    TTS_BACKEND=$(python3 -c "import json; d=json.load(open('$PREFS')); print(d.get('tts_backend','chatterbox'))" 2>/dev/null)
else
    TTS_BACKEND="chatterbox"
fi
echo "[Lumina] TTS backend: $TTS_BACKEND"

# ── Start TTS backend ──────────────────────────────────────────────────────
if [ "$TTS_BACKEND" = "voicebox" ]; then
    if ! curl -s http://localhost:17493/health > /dev/null 2>&1; then
        echo "[Lumina] Voicebox not detected on port 17493."
        echo "[Lumina] Please start Voicebox manually before launching Lumina."
        echo "[Lumina] See README for Voicebox setup instructions."
    else
        echo "[Lumina] Voicebox already running."
    fi

elif [ "$TTS_BACKEND" = "chatterbox" ]; then
    echo "[Lumina] Chatterbox native backend — model loads in-process, no server to start."

elif [ "$TTS_BACKEND" = "supertonic" ]; then
    if ! curl -s http://localhost:7788/v1/audio/voices > /dev/null 2>&1; then
        echo "[Lumina] Supertonic not detected on port 7788."
        echo "[Lumina] Please start Supertonic manually before launching Lumina."
        echo "[Lumina] See README for Supertonic setup instructions."
    else
        echo "[Lumina] Supertonic already running."
    fi

elif [ "$TTS_BACKEND" = "kokoro" ]; then
    if ! curl -s http://localhost:8880/v1/models > /dev/null 2>&1; then
        echo "[Lumina] Kokoro not detected on port 8880."
        echo "[Lumina] Please start Kokoro manually before launching Lumina."
        echo "[Lumina] See README for Kokoro setup instructions."
    else
        echo "[Lumina] Kokoro already running."
    fi

elif [ "$TTS_BACKEND" = "piper" ]; then
    echo "[Lumina] Piper backend — no server to start."

else
    echo "[Lumina] Unknown TTS backend '$TTS_BACKEND' — skipping TTS startup."
fi

# ── Launch Lumina ──────────────────────────────────────────────────────────
cd "$LUMINA_DIR" && python main.py