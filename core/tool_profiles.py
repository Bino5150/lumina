"""
Lumina Tool Profile loader.
Tool profiles live in ~/lumina/tool_profiles/*.json
Each profile is a named set of enabled tools.
"""

import json
import os

PROFILES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tool_profiles")


def list_profiles() -> list[dict]:
    """Return list of tool profile dicts, sorted by name."""
    if not os.path.isdir(PROFILES_DIR):
        return []
    results = []
    for fname in sorted(os.listdir(PROFILES_DIR)):
        if fname.endswith(".json"):
            path = os.path.join(PROFILES_DIR, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data["_file"] = path
                data["_fname"] = fname
                results.append(data)
            except Exception as e:
                print(f"[TOOL_PROFILES] Failed to load {fname}: {e}", flush=True)
    return results


def load_profile(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_profile(path: str, data: dict):
    os.makedirs(PROFILES_DIR, exist_ok=True)
    # Strip internal keys before saving
    clean = {k: v for k, v in data.items() if not k.startswith("_")}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(clean, f, indent=2)


def delete_profile(path: str):
    if os.path.exists(path):
        os.remove(path)


def profile_display_name(profile: dict) -> str:
    """Returns 'Research (11)' style display name."""
    name = profile.get("name", "unnamed")
    count = len(profile.get("enabled", []))
    return f"{name} ({count})"


def fname_from_name(name: str) -> str:
    """Convert profile name to safe filename."""
    safe = "".join(c if c.isalnum() or c in " _-" else "" for c in name)
    return safe.strip().replace(" ", "_").lower() + ".json"