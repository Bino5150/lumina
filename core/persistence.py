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

def save(prefs: dict):
    os.makedirs(os.path.dirname(PREFS_PATH), exist_ok=True)
    try:
        with open(PREFS_PATH, "w") as f:
            json.dump(prefs, f, indent=2)
    except Exception:
        pass

def get(key: str):
    return load().get(key, _defaults.get(key))

def set(key: str, value):
    prefs = load()
    prefs[key] = value
    save(prefs)
