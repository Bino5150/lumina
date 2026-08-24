"""Internal Git-worktree creation and live-enumeration kernel.

Git's worktree ledger is authoritative.  This module retains only frozen
current-process handles and policy metadata; it neither persists nor adopts
worktrees, and listing never mutates or prunes repository state.

No model-facing tools are registered here.
"""

from __future__ import annotations

import os
import math
import re
import secrets
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import config
from core import coding_checkpoint, emergency_stop, process_manager


_READ_ONLY_GIT_TIMEOUT_SECONDS = 10
_DEFAULT_CREATE_TIMEOUT_SECONDS = 120
_MAX_DIAGNOSTIC_CHARS = 4000
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")

# These repositories are engineering control planes, never disposable
# worktree sources or allocation areas.  Kept as a separately named policy
# boundary instead of reusing the read-only Git-tool allowlist (whose meaning
# is the opposite).  Tests replace this set with hermetic paths.
_PROTECTED_ENGINEERING_ROOTS = frozenset(
    os.path.realpath(os.path.expanduser(path))
    for path in ("~/lumina", "~/lumina-release")
)


class WorktreeManagerError(Exception):
    """Base class for pre-mutation request/source failures."""


class InvalidWorktreeRequest(WorktreeManagerError):
    """The source/base/timeout/cancellation request is malformed."""


class SourceRepositoryError(WorktreeManagerError):
    """The requested source cannot be resolved as a Git working tree."""


class BaseResolutionError(WorktreeManagerError):
    """The requested base cannot be captured as an exact commit."""


class ProtectedRootRefused(WorktreeManagerError):
    """The source repository or portable allocation area is protected."""


@dataclass(frozen=True)
class UnknownPorcelainAttribute:
    key: str
    value: Optional[str]


@dataclass(frozen=True)
class GitWorktreeEntry:
    canonical_path: str
    head: Optional[str]
    branch: Optional[str]
    detached: bool
    bare: bool
    locked: bool
    locked_reason: Optional[str]
    prunable: bool
    prunable_reason: Optional[str]
    unknown_attributes: tuple[UnknownPorcelainAttribute, ...]


@dataclass(frozen=True)
class WorktreeHandle:
    worktree_id: str
    source_identity: coding_checkpoint.TargetIdentity
    source_root: str
    worktree_root: str
    branch: str
    base_commit: str
    created_at: str


@dataclass(frozen=True)
class WorktreeCreationResult:
    status: str
    handle: Optional[WorktreeHandle]
    target_path: str
    branch: str
    resolved_base_commit: str
    process_status: Optional[str]
    returncode: Optional[int]
    termination_reason: Optional[str]
    target_exists: bool
    ledger_entry: Optional[GitWorktreeEntry]
    ledger_error: Optional[str]
    identity_error: Optional[str]
    diagnostic: Optional[str]


@dataclass(frozen=True)
class ManagedWorktreeStatus:
    handle: WorktreeHandle
    state: str
    ledger_entry: Optional[GitWorktreeEntry]
    identity_verified: bool
    diagnostic: Optional[str]


@dataclass(frozen=True)
class _SourceResolution:
    root: str
    identity: coding_checkpoint.TargetIdentity
    base_commit: str


@dataclass(frozen=True)
class _RealityObservation:
    target_exists: bool
    ledger_entry: Optional[GitWorktreeEntry]
    ledger_error: Optional[str]
    identity: Optional[coding_checkpoint.TargetIdentity]
    identity_error: Optional[str]


_registry: dict[str, WorktreeHandle] = {}
_reserved_ids: set[str] = set()
_registry_lock = threading.RLock()


def _bounded(text: object) -> Optional[str]:
    if text is None:
        return None
    rendered = str(text).strip()
    return rendered[:_MAX_DIAGNOSTIC_CHARS] or None


def _split_attribute(record: str) -> tuple[str, Optional[str]]:
    key, separator, value = record.partition(" ")
    return key, value if separator else None


def parse_worktree_porcelain(payload: bytes | str) -> tuple[GitWorktreeEntry, ...]:
    """Parse ``git worktree list --porcelain`` (line or ``-z`` form).

    Unknown attributes are retained in arrival order.  Known flag values and
    reasons remain distinguishable (including an explicitly empty reason),
    and a malformed/orphan record is ignored rather than being attached to a
    different worktree.  Creation/listing use ``-z`` so unusual path bytes do
    not depend on Git's display quoting.
    """
    if isinstance(payload, bytes):
        text = payload.decode("utf-8", errors="surrogateescape")
    elif isinstance(payload, str):
        text = payload
    else:
        raise TypeError("porcelain payload must be bytes or str")

    records = text.split("\0") if "\0" in text else text.splitlines()
    entries: list[GitWorktreeEntry] = []
    current: Optional[dict] = None

    def finish_current():
        nonlocal current
        if current is None:
            return
        path = current.get("path")
        if path:
            entries.append(GitWorktreeEntry(
                canonical_path=os.path.realpath(path),
                head=current.get("head"),
                branch=current.get("branch"),
                detached=bool(current.get("detached")),
                bare=bool(current.get("bare")),
                locked=bool(current.get("locked")),
                locked_reason=current.get("locked_reason"),
                prunable=bool(current.get("prunable")),
                prunable_reason=current.get("prunable_reason"),
                unknown_attributes=tuple(current.get("unknown", ())),
            ))
        current = None

    for record in records:
        if record == "":
            finish_current()
            continue
        key, value = _split_attribute(record)
        if key == "worktree":
            finish_current()
            if value:
                current = {"path": value, "unknown": []}
            continue
        if current is None:
            continue
        if key == "HEAD":
            current["head"] = value
        elif key == "branch":
            current["branch"] = value
        elif key == "detached":
            current["detached"] = True
        elif key == "bare":
            current["bare"] = True
        elif key == "locked":
            current["locked"] = True
            current["locked_reason"] = value
        elif key == "prunable":
            current["prunable"] = True
            current["prunable_reason"] = value
        else:
            current["unknown"].append(UnknownPorcelainAttribute(key, value))
    finish_current()
    return tuple(entries)


def _run_read_only_git(argv: list[str], *, cwd: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=_READ_ONLY_GIT_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SourceRepositoryError(f"Git probe failed: {exc}") from exc


def _path_is_protected(path: str) -> bool:
    canonical = os.path.realpath(os.path.expanduser(path))
    for protected in _PROTECTED_ENGINEERING_ROOTS:
        try:
            if os.path.commonpath((canonical, protected)) == protected:
                return True
        except ValueError:
            continue
    return False


def _reject_protected(path: str, *, role: str):
    if _path_is_protected(path):
        raise ProtectedRootRefused(
            f"protected Lumina engineering {role} refused: {os.path.realpath(path)}"
        )


def _validate_base(base: str) -> str:
    if not isinstance(base, str) or not base.strip() or "\0" in base:
        raise InvalidWorktreeRequest("base must be a non-empty Git revision string")
    return base.strip()


def _resolve_source_and_base(source_repository: str, base: str) -> _SourceResolution:
    if not isinstance(source_repository, str) or not source_repository.strip() or "\0" in source_repository:
        raise InvalidWorktreeRequest("source_repository must be a non-empty path string")
    requested = os.path.realpath(os.path.expanduser(source_repository))
    if not os.path.isdir(requested):
        raise SourceRepositoryError(f"source repository is not a directory: {requested}")

    probe = _run_read_only_git(
        ["git", "rev-parse", "--path-format=absolute", "--show-toplevel"],
        cwd=requested,
    )
    if probe.returncode != 0:
        raise SourceRepositoryError(
            _bounded(probe.stderr.decode("utf-8", errors="replace"))
            or "source is not a Git working tree"
        )
    lines = probe.stdout.decode("utf-8", errors="surrogateescape").splitlines()
    if not lines or not lines[0].strip():
        raise SourceRepositoryError("Git did not report a source working-tree root")
    root = os.path.realpath(lines[0].strip())
    _reject_protected(root, role="source")

    identity = coding_checkpoint.resolve_target_identity(root)
    if identity.kind != "git" or identity.canonical_root != root:
        raise SourceRepositoryError("source identity could not be resolved as the same Git root")

    requested_base = _validate_base(base)
    resolved = _run_read_only_git(
        ["git", "rev-parse", "--verify", "--end-of-options", f"{requested_base}^{{commit}}"],
        cwd=root,
    )
    sha = resolved.stdout.decode("ascii", errors="ignore").strip()
    if resolved.returncode != 0 or not _COMMIT_RE.fullmatch(sha):
        message = _bounded(resolved.stderr.decode("utf-8", errors="replace"))
        raise BaseResolutionError(message or f"base did not resolve to a commit: {requested_base!r}")

    return _SourceResolution(root=root, identity=identity, base_commit=sha.lower())


def _worktrees_root() -> str:
    candidate = os.path.realpath(os.path.join(os.path.expanduser(config.DATA_DIR), "worktrees"))
    _reject_protected(candidate, role="target area")
    return candidate


def _branch_exists(source_root: str, branch: str) -> bool:
    result = _run_read_only_git(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=source_root,
    )
    if result.returncode not in (0, 1):
        raise SourceRepositoryError(
            _bounded(result.stderr.decode("utf-8", errors="replace"))
            or "could not check generated branch availability"
        )
    return result.returncode == 0


def _allocate_identity(source_root: str) -> tuple[str, str, str]:
    root = _worktrees_root()
    os.makedirs(root, exist_ok=True)
    if os.path.realpath(root) != root:
        raise ProtectedRootRefused("worktree allocation root changed during creation")

    with _registry_lock:
        for _ in range(128):
            worktree_id = "wt-" + secrets.token_hex(12)
            target = os.path.join(root, worktree_id)
            branch = f"lumina/{worktree_id}"
            if worktree_id in _registry or worktree_id in _reserved_ids:
                continue
            if os.path.lexists(target) or _branch_exists(source_root, branch):
                continue
            _reserved_ids.add(worktree_id)
            return worktree_id, branch, target
    raise WorktreeManagerError("could not allocate a unique worktree ID, branch, and path")


def _read_worktree_ledger(source_root: str) -> tuple[tuple[GitWorktreeEntry, ...], Optional[str]]:
    try:
        result = _run_read_only_git(
            ["git", "worktree", "list", "--porcelain", "-z"], cwd=source_root,
        )
    except WorktreeManagerError as exc:
        return (), _bounded(exc)
    if result.returncode != 0:
        return (), (
            _bounded(result.stderr.decode("utf-8", errors="replace"))
            or f"git worktree list exited {result.returncode}"
        )
    try:
        return parse_worktree_porcelain(result.stdout), None
    except (TypeError, ValueError, OSError) as exc:
        return (), f"could not parse Git worktree ledger: {exc}"


def _observe_reality(source_root: str, target: str) -> _RealityObservation:
    entries, ledger_error = _read_worktree_ledger(source_root)
    canonical_target = os.path.realpath(target)
    entry = next((item for item in entries if item.canonical_path == canonical_target), None)
    target_exists = os.path.isdir(target)
    identity = None
    identity_error = None
    if target_exists:
        try:
            candidate = coding_checkpoint.resolve_target_identity(target)
            if candidate.kind != "git" or candidate.canonical_root != canonical_target:
                identity_error = "target identity is not the expected Git worktree root"
            else:
                identity = candidate
        except Exception as exc:  # identity boundary must classify, never hide Git reality
            identity_error = _bounded(exc) or "target identity resolution failed"
    else:
        identity_error = "target directory is missing"
    return _RealityObservation(
        target_exists=target_exists, ledger_entry=entry, ledger_error=ledger_error,
        identity=identity, identity_error=identity_error,
    )


def _verification_error(
    observation: _RealityObservation, *, branch: str, base_commit: str,
) -> Optional[str]:
    if observation.ledger_error:
        return observation.ledger_error
    entry = observation.ledger_entry
    if entry is None:
        return "new target is absent from Git's fresh worktree ledger"
    if not observation.target_exists:
        return "new worktree target directory is missing"
    if observation.identity is None:
        return observation.identity_error or "new target identity verification failed"
    if entry.head != base_commit:
        return f"new worktree HEAD mismatch: expected {base_commit}, observed {entry.head}"
    expected_ref = f"refs/heads/{branch}"
    if entry.branch != expected_ref or entry.detached:
        return f"new worktree branch mismatch: expected {expected_ref}, observed {entry.branch}"
    return None


def _creation_result(
    *, status: str, handle: Optional[WorktreeHandle], target: str, branch: str,
    base_commit: str, observation: _RealityObservation, snapshot: Optional[dict],
    diagnostic: Optional[str],
) -> WorktreeCreationResult:
    return WorktreeCreationResult(
        status=status, handle=handle, target_path=target, branch=branch,
        resolved_base_commit=base_commit,
        process_status=None if snapshot is None else snapshot.get("status"),
        returncode=None if snapshot is None else snapshot.get("returncode"),
        termination_reason=None if snapshot is None else snapshot.get("termination_reason"),
        target_exists=observation.target_exists,
        ledger_entry=observation.ledger_entry,
        ledger_error=observation.ledger_error,
        identity_error=observation.identity_error,
        diagnostic=_bounded(diagnostic),
    )


def create_worktree(
    source_repository: str,
    base: str = "HEAD",
    *,
    timeout: float = _DEFAULT_CREATE_TIMEOUT_SECONDS,
    cancel_event=None,
) -> WorktreeCreationResult:
    """Create one managed linked worktree from an exact captured commit.

    Preflight resolves the source, base commit, and protected boundaries
    before allocating any path.  The only mutating command is direct argv
    under the managed process-tree/emergency kernel, with normal Git hooks
    intact.  Every post-launch outcome is followed by a fresh ledger/path
    observation; only a normal exit plus successful live verification earns
    a current-session handle.
    """
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout < 0
    ):
        raise InvalidWorktreeRequest("timeout must be a finite non-negative number")
    if cancel_event is not None and not callable(getattr(cancel_event, "is_set", None)):
        raise InvalidWorktreeRequest("cancel_event must provide is_set()")

    source = _resolve_source_and_base(source_repository, base)
    worktree_id, branch, target = _allocate_identity(source.root)
    created_at = datetime.now(timezone.utc).isoformat()
    process_id = None
    snapshot = None
    try:
        argv = [
            "git", "worktree", "add", "-b", branch, target,
            source.base_commit,
        ]
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GCM_INTERACTIVE"] = "Never"
        try:
            process_id = process_manager.launch_argv(
                argv, cwd=source.root, env=env,
                provenance={
                    "kind": "worktree_create", "schema_version": 1,
                    "worktree_id": worktree_id,
                },
                visibility="internal",
            )
            snapshot = process_manager.wait_for_completion(
                process_id, timeout=timeout, cancel_event=cancel_event,
            )
        except process_manager.ProcessManagerError as exc:
            observation = _observe_reality(source.root, target)
            status = "emergency_killed" if emergency_stop.is_latched() else "launch_failed"
            return _creation_result(
                status=status, handle=None, target=target, branch=branch,
                base_commit=source.base_commit, observation=observation,
                snapshot=None, diagnostic=exc,
            )

        observation = _observe_reality(source.root, target)
        process_status = snapshot.get("status")
        returncode = snapshot.get("returncode")
        if process_status != "exited":
            return _creation_result(
                status=process_status or "process_failed", handle=None,
                target=target, branch=branch, base_commit=source.base_commit,
                observation=observation, snapshot=snapshot,
                diagnostic=snapshot.get("stderr", {}).get("text"),
            )
        if returncode != 0:
            return _creation_result(
                status="git_failed", handle=None, target=target, branch=branch,
                base_commit=source.base_commit, observation=observation,
                snapshot=snapshot, diagnostic=snapshot.get("stderr", {}).get("text"),
            )

        verification_error = _verification_error(
            observation, branch=branch, base_commit=source.base_commit,
        )
        if verification_error:
            return _creation_result(
                status="verification_failed", handle=None, target=target,
                branch=branch, base_commit=source.base_commit,
                observation=observation, snapshot=snapshot,
                diagnostic=verification_error,
            )

        handle = WorktreeHandle(
            worktree_id=worktree_id, source_identity=source.identity,
            source_root=source.root, worktree_root=os.path.realpath(target),
            branch=branch, base_commit=source.base_commit, created_at=created_at,
        )
        with _registry_lock:
            _registry[worktree_id] = handle
        return _creation_result(
            status="created", handle=handle, target=target, branch=branch,
            base_commit=source.base_commit, observation=observation,
            snapshot=snapshot, diagnostic=None,
        )
    finally:
        if process_id is not None and snapshot is not None:
            process_manager.forget_terminal_job(process_id)
        with _registry_lock:
            _reserved_ids.discard(worktree_id)


def list_managed_worktrees() -> tuple[ManagedWorktreeStatus, ...]:
    """Live-cross-check current-session handles against Git's fresh ledger.

    Only retained handles are returned.  Unrelated worktrees present in a
    source ledger are deliberately ignored rather than silently adopted.
    """
    with _registry_lock:
        handles = tuple(_registry.values())

    ledgers: dict[str, tuple[tuple[GitWorktreeEntry, ...], Optional[str]]] = {}
    results: list[ManagedWorktreeStatus] = []
    for handle in handles:
        if handle.source_root not in ledgers:
            ledgers[handle.source_root] = _read_worktree_ledger(handle.source_root)
        entries, ledger_error = ledgers[handle.source_root]
        entry = next(
            (item for item in entries if item.canonical_path == handle.worktree_root),
            None,
        )

        identity_verified = False
        identity_error = None
        if os.path.isdir(handle.worktree_root):
            try:
                identity = coding_checkpoint.resolve_target_identity(handle.worktree_root)
                identity_verified = (
                    identity.kind == "git"
                    and identity.canonical_root == handle.worktree_root
                )
                if not identity_verified:
                    identity_error = "target no longer resolves as its recorded Git root"
            except Exception as exc:
                identity_error = _bounded(exc)
        else:
            identity_error = "target directory is missing"

        diagnostic = ledger_error or identity_error
        if ledger_error or entry is None:
            state = "externally_missing"
        elif not os.path.isdir(handle.worktree_root):
            state = "prunable" if entry.prunable else "externally_missing"
        elif entry.prunable:
            state = "prunable"
        elif entry.locked:
            state = "locked"
        elif (
            not identity_verified
            or entry.head != handle.base_commit
            or entry.branch != f"refs/heads/{handle.branch}"
            or entry.detached
        ):
            state = "stale"
            diagnostic = diagnostic or "Git HEAD/branch/identity differs from the runtime handle"
        else:
            state = "live"

        results.append(ManagedWorktreeStatus(
            handle=handle, state=state, ledger_entry=entry,
            identity_verified=identity_verified, diagnostic=_bounded(diagnostic),
        ))
    return tuple(results)


def _reset_for_tests():
    """Forget advisory runtime state only; never touch Git or paths."""
    with _registry_lock:
        _registry.clear()
        _reserved_ids.clear()
