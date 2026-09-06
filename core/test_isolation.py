"""TEST-DATA-ISOLATION-01 -- fail-closed guard against a test process ever
writing (or reading) the owner's real persistent Lumina state.

tests/conftest.py isolates the whole suite structurally by pointing
LUMINA_DATA_DIR at a throwaway directory before any project module is
imported (config.py resolves it once, at import time -- see that file's
own comment). This module is the second, independent layer: called at the
actual point of file/DB access, it refuses outright if the resolved path
still lands inside the owner's real data directories while running under
that guard (LUMINA_TESTING=1, set by conftest.py alongside LUMINA_DATA_DIR).

This catches what the structural fix alone cannot: a test that explicitly
monkeypatches a *_PATH constant back to a real location, a symlinked test
root that resolves into owner state, or the structural guard being removed
or bypassed later. Path-level evidence only in the error -- never file
contents -- so a refusal is safe to print in CI logs.
"""
import os


def _is_guard_active() -> bool:
    return os.environ.get("LUMINA_TESTING") == "1"


def _forbidden_roots() -> list:
    """Real owner-data roots this process must never touch while the guard
    is active. Computed fresh each call (not cached at import time) so it
    reflects the actual production resolution rather than whatever
    LUMINA_DATA_DIR/monkeypatching has done to the current process."""
    from platformdirs import user_data_dir

    return [
        os.path.realpath(user_data_dir("lumina", appauthor=False)),
        os.path.realpath(os.path.expanduser("~/.config/lumina")),
    ]


def refuse_if_production_path(resolved_path: str) -> None:
    """Raise if `resolved_path` (or a symlink/`..`/relative alias of it)
    lands inside a forbidden owner-data root, while the isolation guard is
    active. A no-op outside of tests (LUMINA_TESTING unset) -- this must
    never affect real app behavior."""
    if not _is_guard_active():
        return
    candidate = os.path.realpath(resolved_path)
    for root in _forbidden_roots():
        if candidate == root or candidate.startswith(root + os.sep):
            raise RuntimeError(
                "[TEST-ISOLATION] REFUSING production-state access\n"
                f"resolved={candidate}\n"
                f"test_root={os.environ.get('LUMINA_DATA_DIR', '(unset)')}"
            )
