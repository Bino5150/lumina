"""
Lumina Persona loader.
Personas live in ~/lumina/personas/*.json
"""

import json
import os

PERSONAS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "personas")
DISCORD_TEMPLATE_PATH = os.path.join(PERSONAS_DIR, "discord_template.json")


def list_personas(include_channel_bound: bool = False) -> list[dict]:
    """Return list of persona dicts, sorted by name. Empty list if none found.

    include_channel_bound: personas flagged "channel_bound": true (e.g.
    personas/discord_template.json) are excluded by default. Those files
    are identity templates for comms transports, edited from a dedicated
    Communications tab screen via load_persona()/save_persona() directly
    on their fixed path - not selectable in the normal desktop Persona
    sidebar. Pass True only if a caller genuinely needs the full set
    (e.g. an admin/debug view), which the sidebar itself never should.
    """
    if not os.path.isdir(PERSONAS_DIR):
        return []
    results = []
    for fname in sorted(os.listdir(PERSONAS_DIR)):
        if fname.endswith(".json"):
            path = os.path.join(PERSONAS_DIR, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("channel_bound") and not include_channel_bound:
                    continue
                data["_file"] = path
                results.append(data)
            except Exception as e:
                print(f"[PERSONA] Failed to load {fname}: {e}", flush=True)
    return results


def load_persona(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def find_persona_by_name(name: str, include_channel_bound: bool = False) -> dict | None:
    """Case-insensitive lookup by persona name (not filename).

    Mirrors core/tool_profiles.py's find_profile_by_name() -- same shape,
    same case/whitespace handling -- so the two "look up a thing by its
    human-facing name" call sites (MB-22's CLI --persona flag and the
    Settings tool-profile dropdown) can't drift into different matching
    rules. Excludes channel_bound templates (e.g. discord_template.json) by
    default, same as list_personas(), since those aren't meant to be
    selected by name outside their dedicated Communications tab flow.
    """
    if not name:
        return None
    target = name.strip().lower()
    for p in list_personas(include_channel_bound=include_channel_bound):
        if p.get("name", "").strip().lower() == target:
            return p
    return None


def save_persona(path: str, data: dict):
    os.makedirs(PERSONAS_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        
def fname_from_name(name: str) -> str:
    """Convert persona name to safe filename."""
    safe = "".join(c if c.isalnum() or c in " _-" else "" for c in name)
    return safe.strip().replace(" ", "_").lower() + ".json"        