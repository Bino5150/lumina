"""Owner target resolution for the Qt review cockpit (CODING-08A4).

Qt-free (no PySide6 import) so it stays independently unit-testable, same
rationale as core/review_display.py and core/chat_render.py. Mirrors A3's
OWNER target-resolution rules (tools/review.py's ``_resolve_owner_target``:
active Project defaulting, explicit path, managed worktree) but returns
typed Python values/exceptions for a Qt controller to consume directly,
rather than A3's bounded-JSON error strings -- a different consumer shape,
so this is an independent implementation rather than a forced shared
abstraction (matches every existing tools/*.py adapter's own convention of
keeping small serialization/resolution logic local rather than sharing it
across differently-shaped consumers).

A4 is always an owner-only surface: the desktop app's LuminaAgent is
constructed with owner=True, so there is no non-owner resolution path here
at all -- this module has no equivalent of A3's delegated review_target_grant.

Every function here is read-only with respect to Project state: only
ever ``ProjectContextState.snapshot()``, never ``.set()``/``.clear()``, and
never ``save_project_binding()``/``activate_project``. Opening, targeting,
and refreshing the review cockpit must never mutate the owner's active
Project.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import core.coding_checkpoint as checkpoint_store
import core.coding_checkpoint_observation as checkpoint_observation
from core import worktree_manager
from core.project_context import ProjectContextState


class TargetResolutionError(Exception):
    """Base class for every review-target resolution failure.

    ``reason`` is a short machine-stable code a caller can branch on without
    string-matching ``message``; ``message`` is human-readable and safe to
    show directly in the UI.
    """

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


class NoActiveProject(TargetResolutionError):
    def __init__(self):
        super().__init__(
            "no_active_project", "No active Project is set for this session.",
        )


class StaleProjectBinding(TargetResolutionError):
    def __init__(self, project_name: str):
        super().__init__(
            "stale_project_binding",
            f"Project {project_name!r}'s durable binding no longer matches this "
            "session; reactivate the Project or choose an explicit repository.",
        )


class InvalidExplicitPath(TargetResolutionError):
    def __init__(self, path: str):
        super().__init__(
            "invalid_path", f"{path!r} does not exist or is not a directory.",
        )


class NotAGitTarget(TargetResolutionError):
    def __init__(self, path: str):
        super().__init__("not_a_git_target", f"{path!r} is not a Git working tree.")


class WorktreeUnavailable(TargetResolutionError):
    def __init__(self, message: str):
        super().__init__("worktree_unavailable", message)


@dataclass(frozen=True)
class ReviewTarget:
    """One resolved, display-ready review target.

    ``identity`` is the exact value handed to
    ``core.git_review_snapshot.capture_snapshot()`` -- the only thing that
    actually determines what gets reviewed. The other fields exist purely
    for the target header (never re-derived from display text, never fed
    back into resolution).
    """
    identity: checkpoint_store.TargetIdentity
    label: str
    project_name: Optional[str]
    worktree_id: Optional[str]


def _require_git(identity: checkpoint_store.TargetIdentity, path: str) -> checkpoint_store.TargetIdentity:
    if identity.kind != "git":
        raise NotAGitTarget(path)
    return identity


def resolve_active_project_target(project_state: Optional[ProjectContextState]) -> ReviewTarget:
    """Active Project as a candidate/default -- never authority by name
    alone. Read-only: only ever calls ``.snapshot()``."""
    project = project_state.snapshot() if project_state is not None else None
    if project is None:
        raise NoActiveProject()
    try:
        checkpoint_observation.verify_project_binding(project)
    except checkpoint_observation.ProjectBindingChanged:
        raise StaleProjectBinding(project.name)
    identity = checkpoint_store.resolve_target_identity(project.root)
    identity = _require_git(identity, project.root)
    return ReviewTarget(
        identity=identity, label=f"Active Project: {project.name}",
        project_name=project.name, worktree_id=None,
    )


def resolve_explicit_path_target(path: str) -> ReviewTarget:
    """Explicit owner-chosen path (e.g. from a directory chooser) -- direct
    intent, resolved fresh, no Project involvement at all."""
    expanded = os.path.expanduser(path)
    concrete = expanded if os.path.isabs(expanded) else os.path.abspath(expanded)
    if not os.path.isdir(concrete):
        raise InvalidExplicitPath(path)
    identity = checkpoint_store.resolve_target_identity(concrete)
    identity = _require_git(identity, concrete)
    return ReviewTarget(
        identity=identity, label=f"Explicit path: {concrete}",
        project_name=None, worktree_id=None,
    )


def resolve_worktree_target(worktree_id: str) -> ReviewTarget:
    """Fresh CODING-07 live verification (ledger, locked/prunable, filesystem
    identity) -- never a cached/remembered handle. A removed/replaced/stale
    worktree fails closed here rather than silently targeting a reused path."""
    try:
        handle = worktree_manager.resolve_live_worktree(worktree_id)
    except worktree_manager.WorktreeManagerError as exc:
        raise WorktreeUnavailable(str(exc)) from exc
    identity = handle.target_identity.target
    return ReviewTarget(
        identity=identity, label=f"Managed worktree {worktree_id}",
        project_name=None, worktree_id=worktree_id,
    )


def list_managed_worktrees():
    """Returns ``tuple[worktree_manager.ManagedWorktreeStatus, ...]``.

    This is process-wide, not per-agent-session-scoped like
    tools/worktrees.py's model-facing ``list_worktrees`` (which filters
    through a per-registration closure's own session_ids set). That
    filtering exists there to stop one model-facing session from seeing
    another's handles. Worktree *creation* is owner-only
    (core.tool_profiles.OWNER_ONLY_TOOLS), and the desktop app constructs
    exactly one owner=True LuminaAgent for its whole process lifetime --
    every subagent is unconditionally owner=False (tools/subagent.py's own
    module docstring) and therefore can never create one. Process-wide
    managed-worktree state is therefore already exactly "this owner
    session's worktrees" for the single-desktop-app deployment model A4
    targets; this is not a broader adoption of arbitrary state.
    """
    return worktree_manager.list_managed_worktrees()
