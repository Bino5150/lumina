"""
App persistence — saves avatar paths, window state, preferences.
Uses a simple JSON file so nothing is lost between sessions.
"""
import json, os
import config

PREFS_PATH = os.path.join(config.BASE_DIR, "memory", "prefs.json")

_defaults = {
    "avatar_path": None,
    "user_avatar_path": None,
    "last_chat_id": None,
    "window_width": 1150,
    "window_height": 760,
    "tts_enabled": True,
    "tts_host": "http://localhost:8880",
    "tts_voice": "af_bella",
    "tts_speed": 1.0,
}

def load() -> dict:
    try:
        with open(PREFS_PATH, "r") as f:
            data = json.load(f)
            return {**_defaults, **data}
    except Exception:
        return dict(_defaults)

def save(prefs: dict) -> bool:
    """Returns True on success, False on failure (and logs why — this used
    to be a bare `except: pass`, so a failed save vanished with zero trace).
    Writes to a temp file in the same directory then os.replace()s it into
    place, so a crash mid-write can never leave prefs.json half-written —
    the swap is atomic at the filesystem level; the old file stays intact
    until the new one is fully flushed and ready."""
    tmp_path = PREFS_PATH + ".tmp"
    try:
        os.makedirs(os.path.dirname(PREFS_PATH), exist_ok=True)
        with open(tmp_path, "w") as f:
            json.dump(prefs, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, PREFS_PATH)
        return True
    except Exception as e:
        print(f"[PERSISTENCE] save failed: {e}", flush=True)
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return False

def get(key: str):
    return load().get(key, _defaults.get(key))

def set(key: str, value):
    prefs = load()
    prefs[key] = value
    save(prefs)
