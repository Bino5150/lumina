"""Read-only launch preflight. Never imports config, opens state, or migrates it.

All entry points use this before importing code that reads preferences or starts
runtime services. The intentional dev overlay changes DEFAULT_PROFILE to "dev";
profile is installation policy, never inferred from a checkout name or Git branch.
"""
from __future__ import annotations

import os
import stat

from platformdirs import user_data_dir


DEFAULT_PROFILE = "release"


class DataDirectoryError(RuntimeError):
    """Folder selection needs a local owner's explicit decision."""


def _lstat(path):
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DataDirectoryError(f"Cannot inspect {path!r}: {exc}. No state was changed.") from None


def select_data_directory(*, profile=None) -> str:
    """Honor explicit selection; otherwise retain the installation's default.

    Another plausible existing folder is a stop, never permission to adopt it.
    Relative overrides are pinned to the caller's cwd without resolving links or
    folding '..', so the shell's subsequent cd cannot redirect them.
    """
    if "LUMINA_DATA_DIR" in os.environ:
        value = os.environ["LUMINA_DATA_DIR"]
        if not value.strip():
            raise DataDirectoryError("LUMINA_DATA_DIR is empty. Set it to your verified data folder.")
        return value if os.path.isabs(value) else os.path.join(os.getcwd(), value)

    profile = DEFAULT_PROFILE if profile is None else profile
    if profile not in ("release", "dev"):
        raise DataDirectoryError(f"Unknown installation data profile {profile!r}; refusing to guess.")
    home = os.path.expanduser("~")
    release = os.path.join(home, ".local", "share", "lumina-release")
    native = user_data_dir("lumina", appauthor=False)
    preferred = release if profile == "release" else native
    # The non-XDG Linux location may still hold history after XDG_DATA_HOME was
    # changed. Retain the old public main.py location even on macOS/Windows.
    candidates = dict.fromkeys((release, native, os.path.join(home, ".local", "share", "lumina")))
    others = [p for p in candidates if p != preferred and _lstat(p) is not None]
    if others:
        raise DataDirectoryError(
            f"Data folder selection needs review. This {profile} installation defaults to "
            f"{preferred!r}; other existing data folders: {', '.join(repr(p) for p in others)}. "
            "Set LUMINA_DATA_DIR explicitly to the folder holding your established history. "
            "Do not delete or merge folders to bypass this check. No state was changed.")
    current = _lstat(preferred)
    if current is not None and not stat.S_ISDIR(current.st_mode):
        raise DataDirectoryError(
            f"Default data folder {preferred!r} is not a plain directory. "
            "Review it and set LUMINA_DATA_DIR explicitly. No state was changed.")
    return preferred


def _check_legacy_source(base_dir, selected):
    """Never let a launch implicitly relocate old in-checkout user state."""
    if os.path.abspath(base_dir) == os.path.abspath(selected):
        return  # the owner explicitly selected that existing in-repo state
    names = [os.path.join(base_dir, "memory", name) for name in
             ("lumina.db", "prefs.json", "tool_audit.log", "ledger.db")]
    names.append(os.path.join(base_dir, "tools", "_pending"))
    projects = os.path.join(base_dir, "projects")
    if _lstat(projects) is not None:
        try:
            with os.scandir(projects) as entries:
                names.extend(os.path.join(entry.path, "chats.json") for entry in entries
                             if entry.is_dir(follow_symlinks=False))
        except OSError as exc:
            raise DataDirectoryError(f"Cannot inspect legacy projects: {exc}. No state was changed.") from None
    found = [path for path in names if _lstat(path) is not None]
    if found:
        raise DataDirectoryError(
            f"Unreviewed legacy state remains in the installation: {', '.join(repr(p) for p in found)}. "
            f"Selected data folder: {selected!r}. Startup will not migrate it. "
            "Preserve both locations and review docs/upgrading.md before continuing. "
            "No state was changed.")


def configure_data_directory(*, base_dir, profile=None) -> str:
    selected = select_data_directory(profile=profile)
    _check_legacy_source(os.fspath(base_dir), selected)
    # Export only after all checks pass. config and every subprocess see exactly
    # the same choice; this does not create even an empty destination directory.
    os.environ["LUMINA_DATA_DIR"] = selected
    return selected


def require_data_directory(*, base_dir) -> str:
    try:
        return configure_data_directory(base_dir=base_dir)
    except DataDirectoryError as exc:
        raise SystemExit(f"[Lumina] {exc}") from None


if __name__ == "__main__":
    # Used by start_lumina.sh before it reads prefs or performs TTS operations.
    import argparse
    import sys
    parser = argparse.ArgumentParser(description="Check Lumina's data folder without opening state.")
    parser.add_argument("--shell", action="store_true", help="Append a sentinel for Bash command substitution.")
    args = parser.parse_args()
    selected = require_data_directory(base_dir=os.path.dirname(os.path.abspath(__file__)))
    # Bash strips trailing newlines from command substitution. A sentinel lets
    # the launcher preserve even a legitimate folder name ending in a newline.
    sys.stdout.write(selected + ("." if args.shell else "\n"))
