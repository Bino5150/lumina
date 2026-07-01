"""
Dedicated credential storage — deliberately separate from prefs.json.
prefs.json gets dragged into Project uploads and (genericized) into the
public repo; this file never should. See blueprint Part 4a.
"""
import json, os, stat

SECRETS_PATH = os.path.expanduser("~/.config/lumina/credentials.json")


def _load() -> dict:
    try:
        with open(SECRETS_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data: dict):
    os.makedirs(os.path.dirname(SECRETS_PATH), exist_ok=True)
    with open(SECRETS_PATH, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(SECRETS_PATH, stat.S_IRUSR | stat.S_IWUSR)  # chmod 600


def get_secret(key: str, default=None):
    return _load().get(key, default)


def set_secret(key: str, value: str):
    data = _load()
    data[key] = value
    _save(data)