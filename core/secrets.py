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
    """FE-09: was write-then-chmod, leaving a brief window where the file
    existed at the default (often 644) permissions before the chmod call
    landed. Now atomic: 0o600 is set at file *creation* via os.open flags
    (never exists at any other permission), written to a temp file in the
    same directory, then os.replace()'d into place — same pattern
    persistence.py already uses for prefs.json, applied here too."""
    os.makedirs(os.path.dirname(SECRETS_PATH), exist_ok=True)
    tmp_path = SECRETS_PATH + ".tmp"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, SECRETS_PATH)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def get_secret(key: str, default=None):
    return _load().get(key, default)


def set_secret(key: str, value: str):
    data = _load()
    data[key] = value
    _save(data)


def migrate_legacy_cloud_keys():
    """FE-09: cloud API keys used to live in prefs.json's cloud_credentials
    block (and custom_api_key) — the exact file this module's docstring says
    they never should. One-time, idempotent: moves any keys still sitting
    in prefs.json into this file's storage and strips them out of prefs.json
    so they stop lingering in the file that gets dragged into Project
    uploads and the (genericized) public repo. No-op on every call after the
    first — cheap enough to run unconditionally at owner-session startup."""
    from core.persistence import load as load_prefs, save as save_prefs

    prefs = load_prefs()
    changed = False

    cloud_creds = prefs.get("cloud_credentials", {})
    for provider, creds in cloud_creds.items():
        legacy_key = creds.get("api_key")
        if legacy_key:
            set_secret(f"{provider}_api_key", legacy_key)
            creds.pop("api_key", None)
            changed = True
    if changed:
        prefs["cloud_credentials"] = cloud_creds

    legacy_custom_key = prefs.get("custom_api_key")
    if legacy_custom_key:
        set_secret("custom_api_key", legacy_custom_key)
        prefs.pop("custom_api_key", None)
        changed = True

    if changed:
        save_prefs(prefs)
        print("[SECRETS] Migrated cloud API keys out of prefs.json into "
              "~/.config/lumina/credentials.json", flush=True)