"""
core/git_repos.py — shared Git-repository authorization boundary for the
four read-only Git tools (tools/git_status.py, git_diff.py, git_log.py,
git_branches.py).

CODING-02B-B: this is a SEPARATE axis from core/project_context.py's Mode A
path/cwd defaulting. An active Project is ergonomic defaulting only — it can
supply a *candidate* repo_path when the caller omits one, but that candidate
must still pass the exact same authorization check as an explicit repo_path.
A Project binding (however valid) does not, and must never, expand
_ALLOWED_REPOS. Project selection != Git authorization.

Extracted verbatim from the four git_*.py tools, which previously each
carried a byte-identical copy of _ALLOWED_REPOS/_resolve_allowed_repo —
confirmed identical across all four during CODING-02B-B0's source vet before
this centralization.
"""
import os
from typing import Optional, Tuple

from core.project_context import ProjectContextState

# Repo path allowlist — restricts every Git tool to known project roots.
# Oracle/MoTunes paths intentionally excluded pending a separate, deliberate
# Git-authorization decision — never implicitly granted via a Project
# binding, no matter how valid that binding is.
_ALLOWED_REPOS = {
    os.path.realpath(os.path.expanduser(p)) for p in ("~/lumina", "~/lumina-release")
}


def resolve_git_repo(
    repo_path: Optional[str],
    project_state: Optional[ProjectContextState],
) -> Tuple[Optional[str], Optional[str]]:
    """Returns (real_path, error_message_or_None).

    repo_path supplied — canonicalized (expanduser -> realpath) and checked
    against _ALLOWED_REPOS exactly as every Git tool always did. Explicit
    caller intent wins; the active Project has no effect on this path at
    all, not even to reinterpret it.

    repo_path omitted + active Project — the Project's root becomes the
    *candidate* repo_path, then runs through the identical canonicalize +
    _ALLOWED_REPOS check as an explicit path would. An allowlisted root
    proceeds; a non-allowlisted one (e.g. Oracle, MoTunes) fails closed with
    the same authorization error an arbitrary unauthorized path would get —
    a valid Project binding is never treated as Git authorization.

    repo_path omitted + no active Project — fails clearly. Never guesses
    cwd, HOME, most-recently-used repo, first allowlisted repo, or a
    tracked Markdown Root.
    """
    if repo_path is None:
        context = project_state.get() if project_state is not None else None
        if context is None:
            return None, "Error: repo_path omitted and no active project is set for this session."
        repo_path = context.root

    real = os.path.realpath(os.path.expanduser(repo_path))
    if real not in _ALLOWED_REPOS:
        allowed = ", ".join(sorted(_ALLOWED_REPOS))
        return real, f"Error: repo_path not in allowlist. Allowed: {allowed}. Got: {repo_path}"
    return real, None
