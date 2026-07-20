"""
Update Checker — read-only git fetch + rev-list comparison against origin.
Never pulls, merges, or modifies tracked files. Refreshes remote-tracking
refs only (git fetch) and reports how far local HEAD is from upstream.

Shared by the check_for_updates tool (agent.py registration) and the About
tab's Check for Updates button (ui/settings.py) — one implementation, two callers.
"""

import subprocess
import config


def check_for_updates() -> str:
    """Check how many commits behind/ahead of the tracked remote branch this install is."""
    repo_dir = config.BASE_DIR

    try:
        fetch = subprocess.run(
            ["git", "fetch", "--quiet"],
            cwd=repo_dir, capture_output=True, text=True, timeout=20,
        )
    except FileNotFoundError:
        return "[Update check unavailable: git not found on this system.]"
    except subprocess.TimeoutExpired:
        return "[Update check timed out — could not reach the remote.]"
    except Exception as e:
        return f"[Update check error: {e}]"

    if fetch.returncode != 0:
        return f"[Update check failed: {fetch.stderr.strip() or 'git fetch returned a non-zero exit code'}]"

    try:
        branch_result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_dir, capture_output=True, text=True, timeout=10,
        )
        branch = branch_result.stdout.strip()
        upstream = f"origin/{branch}" if branch and branch != "HEAD" else "origin/main"

        behind = subprocess.run(
            ["git", "rev-list", "--count", f"HEAD..{upstream}"],
            cwd=repo_dir, capture_output=True, text=True, timeout=10,
        )
        ahead = subprocess.run(
            ["git", "rev-list", "--count", f"{upstream}..HEAD"],
            cwd=repo_dir, capture_output=True, text=True, timeout=10,
        )
    except Exception as e:
        return f"[Update check error comparing against remote: {e}]"

    if behind.returncode != 0:
        return f"[Update check failed: could not compare against {upstream} — is a remote tracking branch configured?]"

    behind_count = behind.stdout.strip()
    ahead_count = ahead.stdout.strip() if ahead.returncode == 0 else None

    if behind_count == "0":
        if ahead_count and ahead_count != "0":
            return f"Up to date with {upstream}. ({ahead_count} local commit(s) not yet pushed.)"
        return f"Up to date with {upstream}."

    return (f"{behind_count} commit(s) behind {upstream}. "
            f"Run `git pull` manually to update — this check never pulls automatically.")


def register_update_tools(registry):
    registry.register(
        name="check_for_updates",
        fn=check_for_updates,
        description=(
            "Check whether this Lumina installation is behind the tracked GitHub branch. "
            "Read-only: runs `git fetch` then compares commit counts — never pulls, merges, "
            "or modifies any files."
        ),
        parameters={"type": "object", "properties": {}, "required": []},
    )
