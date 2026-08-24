"""Owner-only model adapters for Lumina-managed Git worktrees (CODING-07A3).

The three public tools in this module are deliberately thin.  Git lifecycle,
identity verification, protected-root policy, dirty/locked/process refusal,
and emergency/cancellation behavior remain authoritative in
``core.worktree_manager``.  This layer owns only model argument validation,
active-Project defaulting, per-agent session visibility, and bounded JSON.

No result emitted here is durable authority.  The adapter never stages,
commits, pushes, merges, prunes, deletes branches, rewrites Project bindings,
or creates coding-checkpoint state.
"""

from __future__ import annotations

import json
import re
import threading

import config
import core.coding_checkpoint_observation as checkpoint_observation
from core import worktree_manager
from core.project_context import ProjectContext


_WORKTREE_ID_RE = re.compile(r"^wt-[0-9a-f]{24}$")
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _sanitize_text(value):
    if value is None:
        return None
    rendered = _ANSI_ESCAPE_RE.sub("", str(value))
    return _CONTROL_CHAR_RE.sub("", rendered)


def _live_budget() -> int:
    try:
        return max(0, int(config.TOOL_RESULT_MAX_CHARS))
    except (TypeError, ValueError):
        return 0


def _encode(payload) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _tiny_json(budget: int) -> str:
    if budget >= 2:
        return "{}"
    if budget == 1:
        return "0"
    return ""


def _bounded_candidates(candidates: list) -> str:
    budget = _live_budget()
    for payload in candidates:
        encoded = _encode(payload)
        if len(encoded) <= budget:
            return encoded
    return _tiny_json(budget)


def _bounded_error(code: str, message: str) -> str:
    return _bounded_candidates([
        {"status": "error", "error": code, "message": message},
        {"status": "error", "error": code},
        {"error": code},
        {"status": "error"},
    ])


def _ledger_payload(entry):
    if entry is None:
        return None
    return {
        "head": entry.head,
        "branch": entry.branch,
        "detached": entry.detached,
        "locked": entry.locked,
        "locked_reason": _sanitize_text(entry.locked_reason),
        "prunable": entry.prunable,
        "prunable_reason": _sanitize_text(entry.prunable_reason),
    }


def _render_create(result) -> str:
    handle = result.handle
    core = {
        "status": result.status,
        "worktree_id": None if handle is None else handle.worktree_id,
        "worktree_root": result.target_path,
        "branch": result.branch,
        "base_commit": result.resolved_base_commit,
        "target_exists": result.target_exists,
        "process_status": result.process_status,
        "returncode": result.returncode,
        "termination_reason": result.termination_reason,
    }
    full = dict(core)
    full["ledger"] = _ledger_payload(result.ledger_entry)
    full["diagnostic"] = _sanitize_text(result.diagnostic)
    full["ledger_error"] = _sanitize_text(result.ledger_error)
    full["identity_error"] = _sanitize_text(result.identity_error)
    compact = {
        key: core[key]
        for key in (
            "status", "worktree_id", "worktree_root", "branch", "base_commit",
            "target_exists", "termination_reason",
        )
    }
    return _bounded_candidates([
        full,
        {**core, "diagnostic": (_sanitize_text(result.diagnostic) or "")[:512]},
        core,
        compact,
        {"status": result.status, "worktree_id": compact["worktree_id"]},
        {"status": result.status},
    ])


def _status_payload(status) -> dict:
    handle = status.handle
    return {
        "worktree_id": handle.worktree_id,
        "state": status.state,
        "worktree_root": handle.worktree_root,
        "branch": handle.branch,
        "base_commit": handle.base_commit,
        "created_at": handle.created_at,
        "identity_verified": status.identity_verified,
        "ledger": _ledger_payload(status.ledger_entry),
        "diagnostic": _sanitize_text(status.diagnostic),
    }


def _render_list(statuses, missing_ids) -> str:
    full_items = [_status_payload(status) for status in statuses]
    full_items.extend(
        {
            "worktree_id": worktree_id,
            "state": "verification_problem",
            "diagnostic": "session handle is unavailable from the runtime manager",
        }
        for worktree_id in missing_ids
    )
    full_items.sort(key=lambda item: item["worktree_id"])
    compact_items = [
        {
            "worktree_id": item["worktree_id"],
            "state": item["state"],
            **({"worktree_root": item["worktree_root"]} if "worktree_root" in item else {}),
        }
        for item in full_items
    ]
    total = len(full_items)
    candidates = [
        {"worktrees": full_items, "total": total, "output_truncated": False},
        {"worktrees": compact_items, "total": total, "output_truncated": False},
    ]
    for keep in range(total - 1, -1, -1):
        candidates.append({
            "worktrees": compact_items[:keep],
            "total": total,
            "output_truncated": keep < total,
        })
    candidates.extend(({"total": total}, {"status": "ok"}))
    return _bounded_candidates(candidates)


def _render_remove(result) -> str:
    core = {
        "status": result.status,
        "worktree_id": result.worktree_id,
        "force": result.force,
        "target_exists": result.target_exists,
        "dirty": result.dirty,
        "blocking_process_ids": list(result.blocking_process_ids),
        "process_status": result.process_status,
        "returncode": result.returncode,
        "termination_reason": result.termination_reason,
    }
    full = dict(core)
    full["ledger"] = _ledger_payload(result.ledger_entry)
    full["diagnostic"] = _sanitize_text(result.diagnostic)
    full["ledger_error"] = _sanitize_text(result.ledger_error)
    full["identity_error"] = _sanitize_text(result.identity_error)
    return _bounded_candidates([
        full,
        {**core, "diagnostic": (_sanitize_text(result.diagnostic) or "")[:512]},
        core,
        {
            "status": result.status,
            "worktree_id": result.worktree_id,
            "target_exists": result.target_exists,
            "blocking_process_ids": list(result.blocking_process_ids),
        },
        {"status": result.status, "worktree_id": result.worktree_id},
        {"status": result.status},
    ])


def _manager_error(exc: Exception) -> str:
    translations = (
        (worktree_manager.ProtectedRootRefused,
         ("protected_root_refused", "protected Lumina engineering roots cannot be used")),
        (worktree_manager.BaseResolutionError,
         ("base_resolution_failed", "base did not resolve to a Git commit")),
        (worktree_manager.SourceRepositoryError,
         ("source_repository_invalid", "source repository could not be verified")),
        (worktree_manager.InvalidWorktreeRequest,
         ("invalid_worktree_request", "worktree request is invalid")),
        (worktree_manager.WorktreeManagerError,
         ("worktree_manager_refused", "worktree manager refused the operation")),
    )
    for error_type, (code, message) in translations:
        if isinstance(exc, error_type):
            return _bounded_error(code, message)
    return _bounded_error("worktree_operation_failed", "worktree operation failed")


def register_worktree_tools(registry, project_state=None, cancel_state=None):
    """Register the exact A3 surface for one LuminaAgent session.

    The closure-local ID set prevents one owner session from listing or
    removing another session's managed handles even though the lower-level
    manager is process-global.  It is advisory visibility/selection state only;
    Git reality and destructive authorization remain manager decisions.

    CODING-07A4 returns a closure over that same session set. Immediate
    subagent dispatch uses it to turn an opaque ID into a freshly verified,
    synthetic ProjectContext without exposing the set or resolver to a model.
    """
    session_ids: set[str] = set()
    session_lock = threading.RLock()

    def resolve_dispatch_context(worktree_id: str) -> ProjectContext:
        """Resolve one session-owned ID to an ephemeral, verified context."""
        if not isinstance(worktree_id, str) or not _WORKTREE_ID_RE.fullmatch(worktree_id):
            raise ValueError("worktree_id must be an opaque Lumina worktree ID")
        with session_lock:
            if worktree_id not in session_ids:
                raise ValueError("managed worktree was not found in this session")
            handle = worktree_manager.resolve_live_worktree(worktree_id)
        return ProjectContext(
            name=f"worktree-{worktree_id}", root=handle.worktree_root,
        )

    def create_worktree(base="HEAD", source_repository=None, **unsupported_fields) -> str:
        if unsupported_fields:
            return _bounded_error(
                "invalid_arguments", "create_worktree accepts no additional arguments"
            )
        if not isinstance(base, str) or not base.strip() or "\0" in base:
            return _bounded_error("invalid_base", "base must be a non-empty Git revision string")
        if source_repository is not None and (
            not isinstance(source_repository, str)
            or not source_repository.strip()
            or "\0" in source_repository
        ):
            return _bounded_error(
                "invalid_source_repository",
                "source_repository must be a non-empty path string or null",
            )

        if source_repository is None:
            active = project_state.snapshot() if project_state is not None else None
            if active is None:
                return _bounded_error(
                    "project_required",
                    "no active Project and no source_repository was given",
                )
            try:
                checkpoint_observation.verify_project_binding(active)
            except checkpoint_observation.ProjectBindingChanged:
                return _bounded_error(
                    "stale_project_binding",
                    "active Project binding changed; reactivate it or pass source_repository explicitly",
                )
            concrete_source = active.root
        else:
            # Explicit caller intent is independent of any active Project.
            concrete_source = source_repository

        cancel_event = cancel_state.get() if cancel_state is not None else None
        try:
            result = worktree_manager.create_worktree(
                concrete_source, base.strip(), cancel_event=cancel_event,
            )
        except Exception as exc:
            return _manager_error(exc)

        if result.handle is not None and result.status == "created":
            with session_lock:
                session_ids.add(result.handle.worktree_id)
        return _render_create(result)

    def list_worktrees(**unsupported_fields) -> str:
        if unsupported_fields:
            return _bounded_error(
                "invalid_arguments", "list_worktrees accepts no arguments"
            )
        try:
            all_statuses = worktree_manager.list_managed_worktrees()
        except Exception as exc:
            return _manager_error(exc)

        with session_lock:
            visible_ids = set(session_ids)
        statuses = tuple(
            status for status in all_statuses
            if status.handle.worktree_id in visible_ids
        )
        returned_ids = {status.handle.worktree_id for status in statuses}
        missing_ids = sorted(visible_ids - returned_ids)
        return _render_list(statuses, missing_ids)

    def remove_worktree(worktree_id, force=False, **unsupported_fields) -> str:
        if unsupported_fields:
            return _bounded_error(
                "invalid_arguments",
                "remove_worktree accepts only worktree_id and force",
            )
        if not isinstance(worktree_id, str) or not _WORKTREE_ID_RE.fullmatch(worktree_id):
            return _bounded_error(
                "invalid_worktree_id", "worktree_id must be an opaque Lumina worktree ID"
            )
        if not isinstance(force, bool):
            return _bounded_error("invalid_force", "force must be a boolean")
        with session_lock:
            if worktree_id not in session_ids:
                return _bounded_error(
                    "worktree_not_found", "managed worktree was not found in this session"
                )

        cancel_event = cancel_state.get() if cancel_state is not None else None
        try:
            result = worktree_manager.remove_worktree(
                worktree_id, force=force, cancel_event=cancel_event,
            )
        except Exception as exc:
            return _manager_error(exc)

        if result.status == "removed" or result.status.endswith("_removed"):
            with session_lock:
                session_ids.discard(worktree_id)
        return _render_remove(result)

    registry.register(
        name="create_worktree",
        fn=create_worktree,
        description=(
            "Create a Lumina-managed linked Git worktree at an exact resolved commit. "
            "Owner-only. Omit source_repository to use the active Project; Lumina "
            "chooses the opaque ID, branch, and target directory."
        ),
        parameters={
            "type": "object",
            "properties": {
                "base": {
                    "type": "string",
                    "description": "Git revision to capture as an exact commit; default HEAD.",
                },
                "source_repository": {
                    "type": "string",
                    "description": "Optional explicit source repository path.",
                },
            },
            "additionalProperties": False,
        },
    )
    registry.register(
        name="list_worktrees",
        fn=list_worktrees,
        description=(
            "List this session's Lumina-managed worktree handles, cross-checked "
            "against live Git reality. Owner-only; never adopts external worktrees."
        ),
        parameters={
            "type": "object", "properties": {}, "additionalProperties": False,
        },
    )
    registry.register(
        name="remove_worktree",
        fn=remove_worktree,
        description=(
            "Safely remove one session-managed worktree by opaque worktree_id. "
            "Owner-only. force may override dirty-worktree refusal only."
        ),
        parameters={
            "type": "object",
            "properties": {
                "worktree_id": {
                    "type": "string",
                    "pattern": r"^wt-[0-9a-f]{24}$",
                    "description": "Opaque Lumina worktree ID returned by create_worktree.",
                },
                "force": {
                    "type": "boolean",
                    "description": "Override dirty-worktree refusal only; default false.",
                },
            },
            "required": ["worktree_id"],
            "additionalProperties": False,
        },
    )
    return resolve_dispatch_context
