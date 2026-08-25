"""Authoritative live observation and freshness for coding checkpoints.

CODING-05A2 only: this module is an internal source-controlled seam.  It
registers no tools and grants no Git, commit, push, or validation authority.
Callers provide workflow prose, relevant *paths*, and descriptive validation
reports.  Target identity, repository state, file facts, state references,
and validation bindings are measured here.
"""

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Optional, Sequence

import core.coding_checkpoint as checkpoint_store
import core.coding_validation_evidence as validation_evidence
from core.git_read import GitCommandResult, GitReadError, run_bounded_git
from core.project_context import ProjectContext, load_project_binding


GIT_TIMEOUT_SECONDS = 5
MAX_GIT_STDOUT_BYTES = 8 * 1024 * 1024
MAX_GIT_STDERR_BYTES = 16 * 1024
FILE_READ_CHUNK_BYTES = 128 * 1024

# CODING-06A3 corrective 1: the validation-state fingerprint is a narrower,
# validation-specific layer on top of CODING-05A2's observation, not a
# replacement for its target/state architecture (see _validation_state_
# fingerprint() below). Versioned independently of _TARGET_IDENTITY_ALGO_
# VERSION and the general state reference document so either can evolve
# without the other.
#
# v2 (CODING-06A3 final verification corrective): folded git_state.
# status_digest into the hashed document. v1 hashed only HEAD/branch/
# detached/unborn/operation_state plus dirty-path *content* bytes, keyed
# by deduplicated path -- never by XY status -- so a staged edit and the
# byte-identical unstaged edit it came from produced the same fingerprint.
# Bumped so a v1 fingerprint is never compared as equal to a v2 one purely
# by coincidence of encoding; nothing persists validation_state_ref across
# a schema change (it is recomputed live every observation), so there is
# no stored-row migration concern here.
_VALIDATION_FINGERPRINT_ALGO_VERSION = 2
MAX_SYMLINK_TARGET_CHARS = 4096

FRESH = "fresh"
STALE = "stale"
UNVERIFIABLE = "unverifiable"

_NON_REPOSITORY_MARKER = b"not a git repository"
_OPERATION_PATHS = {
    "merge": ("MERGE_HEAD",),
    "cherry_pick": ("CHERRY_PICK_HEAD",),
    "rebase": ("rebase-merge", "rebase-apply"),
    "revert": ("REVERT_HEAD",),
    "bisect": ("BISECT_LOG",),
}
_REPORTED_VALIDATION_KEYS = {
    "label", "outcome", "exit_code", "summary", "timestamp",
}


class ObservationError(checkpoint_store.CheckpointError):
    """Stable, bounded failure from live observation."""

    code = "observation_error"

    def __init__(self, message: str):
        super().__init__(message)


class ProjectBindingChanged(ObservationError):
    code = "project_binding_changed"


class GitProbeError(ObservationError):
    code = "git_probe_failed"


class RelevantPathError(ObservationError):
    code = "invalid_relevant_path"


class UnstableCapture(ObservationError):
    code = "unstable_capture"


@dataclass(frozen=True)
class PushPosture:
    push_default: Optional[str]
    configured_safety_sentinel: bool

    def as_dict(self) -> dict:
        return {
            "push_default": self.push_default,
            "configured_safety_sentinel": self.configured_safety_sentinel,
        }


@dataclass(frozen=True)
class ChangedPath:
    path: str
    record_type: str
    status: str
    original_path: Optional[str] = None

    def as_dict(self) -> dict:
        value = {
            "path": self.path,
            "record_type": self.record_type,
            "status": self.status,
        }
        if self.original_path is not None:
            value["original_path"] = self.original_path
        return value


@dataclass(frozen=True)
class RelevantFileObservation:
    path: str
    sha256: Optional[str]
    missing: bool
    is_symlink: bool

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "missing": self.missing,
            "is_symlink": self.is_symlink,
        }


@dataclass(frozen=True)
class _GitState:
    head: Optional[str]
    branch: Optional[str]
    detached: bool
    unborn: bool
    operation_state: tuple
    status_digest: str
    changed_paths: tuple
    changed_paths_truncated: bool
    upstream_ref: Optional[str]
    push_posture: PushPosture


@dataclass(frozen=True)
class LiveObservation:
    identity: checkpoint_store.TargetIdentity
    head: Optional[str]
    branch: Optional[str]
    detached: bool
    unborn: bool
    operation_state: tuple
    status_digest: Optional[str]
    status_complete: bool
    changed_paths: tuple
    changed_paths_truncated: bool
    upstream_ref: Optional[str]
    push_posture: Optional[PushPosture]
    relevant_files: tuple
    state_ref: str
    binding_complete: bool
    validation_state_ref: Optional[str]
    validation_state_complete: bool

    def observation_payload(self) -> dict:
        return {
            "target_key": self.identity.target_key,
            "target_kind": self.identity.kind,
            "canonical_root": self.identity.canonical_root,
            "head": self.head,
            "branch": self.branch,
            "detached": self.detached,
            "unborn": self.unborn,
            "operation_state": list(self.operation_state),
            "status_digest": self.status_digest,
            "status_complete": self.status_complete,
            "changed_paths_truncated": self.changed_paths_truncated,
            "upstream_ref": self.upstream_ref,
            "push_posture": (
                self.push_posture.as_dict() if self.push_posture is not None else None
            ),
            "state_ref": self.state_ref,
            "binding_complete": self.binding_complete,
        }

    def relevant_file_payload(self) -> list:
        return [item.as_dict() for item in self.relevant_files]

    def changed_path_payload(self) -> list:
        return [item.as_dict() for item in self.changed_paths]


@dataclass(frozen=True)
class FreshnessResult:
    state: str
    reasons: tuple


@dataclass(frozen=True)
class ValidationApplicability:
    label: str
    outcome: str
    source: str
    applies_to_current_state: bool
    reason: str


@dataclass(frozen=True)
class SafeRowMetadata:
    project_name: str
    target_key_prefix: str
    revision: int
    schema_version: int
    status: str


@dataclass(frozen=True)
class SavedLiveCheckpoint:
    record: checkpoint_store.CheckpointRecord
    observation: LiveObservation


@dataclass(frozen=True)
class LiveCheckpointRead:
    status: str
    record: Optional[checkpoint_store.CheckpointRecord]
    current: Optional[LiveObservation]
    freshness: FreshnessResult
    validation_applicability: tuple
    machine_validation_applicability: tuple = ()
    live_machine_validations: tuple = ()
    metadata: Optional[SafeRowMetadata] = None


# CODING-08A1: the bounded argv-only subprocess boundary itself now lives in
# core.git_read, shared with the review kernel. This stays a thin wrapper
# binding this module's own timeout/byte bounds and translating a transport
# failure into GitProbeError -- the domain exception every caller in this
# module already expects. Behavior (env, bounds, exception type raised at
# each call site) is unchanged from the pre-extraction implementation.
_GitCommandResult = GitCommandResult

# CODING-08R.1: a repository-local core.fsmonitor value names an external
# program Git executes on this module's own `status` call (_capture_git_
# state below) -- confirmed empirically (CODING-08R) against real Git
# 2.43.0, the exact same exposure found and closed in core.git_review.
# Forced onto every invocation this module makes, mirroring
# core.git_review._GLOBAL_SAFE_ARGS exactly, including the empty-string
# (never "false") choice -- see that constant's own comment for the
# Git <=2.35.1 boolean/pathname compatibility reasoning. This module never
# calls `git diff` on worktree content (dirty-path content hashing goes
# through direct, unconverting filesystem reads -- see _hash_regular_file_
# once below, which mirrors core.git_review_snapshot's own fingerprinting
# read), so the content-filter (clean/smudge/process) vector core.git_review
# needed a structural fix for does not reach this module at all; confirmed
# by inspection, nothing here was changed for that vector.
_GLOBAL_SAFE_ARGS = ("-c", "core.fsmonitor=")


def _run_git(root: str, args: Sequence[str]) -> GitCommandResult:
    try:
        return run_bounded_git(
            root, (*_GLOBAL_SAFE_ARGS, *args),
            timeout=GIT_TIMEOUT_SECONDS,
            max_stdout_bytes=MAX_GIT_STDOUT_BYTES,
            max_stderr_bytes=MAX_GIT_STDERR_BYTES,
        )
    except GitReadError as error:
        raise GitProbeError(str(error)) from error


def _decode_git_data(value: bytes) -> str:
    return value.decode("utf-8", errors="surrogateescape")


def _one_line(result: _GitCommandResult, what: str) -> str:
    lines = result.stdout.splitlines()
    if result.returncode != 0 or len(lines) != 1 or not lines[0]:
        raise GitProbeError(f"Git could not provide {what}")
    return _decode_git_data(lines[0])


def verify_project_binding(project: ProjectContext) -> ProjectContext:
    """Require an immutable snapshot to still match durable local binding."""
    if not isinstance(project, ProjectContext):
        raise TypeError("project must be an immutable ProjectContext")
    try:
        current = load_project_binding(project.name)
    except Exception as error:
        raise ProjectBindingChanged(
            f"project {project.name!r} binding cannot be verified; refresh/reactivate it"
        ) from error

    snapshot_root = os.path.normcase(os.path.abspath(project.root))
    durable_root = os.path.normcase(os.path.abspath(current.root))
    if snapshot_root != durable_root:
        raise ProjectBindingChanged(
            f"project {project.name!r} binding changed; refresh/reactivate it"
        )
    return current


def _resolve_target_identity(root: str) -> checkpoint_store.TargetIdentity:
    canonical_requested = os.path.realpath(os.path.expanduser(root))
    result = _run_git(
        canonical_requested,
        ["rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir"],
    )
    if result.returncode == 0:
        lines = result.stdout.splitlines()
        if len(lines) != 2 or not lines[0] or not lines[1]:
            raise GitProbeError("Git target discovery returned an invalid shape")
        toplevel = os.path.realpath(_decode_git_data(lines[0]))
        common_dir = os.path.realpath(_decode_git_data(lines[1]))
        return checkpoint_store._build_target_identity("git", toplevel, common_dir)

    if _NON_REPOSITORY_MARKER in result.stderr.lower():
        return checkpoint_store._build_target_identity(
            "directory", canonical_requested, None
        )
    raise GitProbeError("Git target discovery failed")


def _serialize_relative_path(value: bytes) -> str:
    path = _decode_git_data(value)
    if os.sep != "/":
        path = path.replace(os.sep, "/")
    if os.altsep and os.altsep != "/":
        path = path.replace(os.altsep, "/")
    return path


def _canonical_status_entry(parts: list, path: bytes, original: Optional[bytes] = None) -> dict:
    entry = {
        "type": _decode_git_data(parts[0]),
        "fields": [_decode_git_data(field) for field in parts[1:]],
        "path": _serialize_relative_path(path),
    }
    if original is not None:
        entry["original_path"] = _serialize_relative_path(original)
    return entry


def _parse_porcelain_v2(raw: bytes) -> tuple:
    records = raw.split(b"\0")
    if records and records[-1] == b"":
        records.pop()
    canonical = []
    display = []
    display_truncated = False
    index = 0
    while index < len(records):
        record = records[index]
        if record.startswith(b"1 "):
            parts = record.split(b" ", 8)
            if len(parts) != 9:
                raise GitProbeError("Git status ordinary entry was malformed")
            path = parts[8]
            canonical.append(_canonical_status_entry(parts[:8], path))
            changed = ChangedPath(
                path=_serialize_relative_path(path),
                record_type="ordinary",
                status=_decode_git_data(parts[1]),
            )
        elif record.startswith(b"2 "):
            parts = record.split(b" ", 9)
            if len(parts) != 10 or index + 1 >= len(records):
                raise GitProbeError("Git status rename entry was malformed")
            path = parts[9]
            index += 1
            original = records[index]
            canonical.append(_canonical_status_entry(parts[:9], path, original))
            changed = ChangedPath(
                path=_serialize_relative_path(path),
                original_path=_serialize_relative_path(original),
                record_type="rename",
                status=_decode_git_data(parts[1]),
            )
        elif record.startswith(b"u "):
            parts = record.split(b" ", 10)
            if len(parts) != 11:
                raise GitProbeError("Git status unmerged entry was malformed")
            path = parts[10]
            canonical.append(_canonical_status_entry(parts[:10], path))
            changed = ChangedPath(
                path=_serialize_relative_path(path),
                record_type="unmerged",
                status=_decode_git_data(parts[1]),
            )
        elif record.startswith(b"? "):
            path = record[2:]
            if not path:
                raise GitProbeError("Git status untracked entry was malformed")
            canonical.append({"type": "?", "path": _serialize_relative_path(path)})
            changed = ChangedPath(
                path=_serialize_relative_path(path),
                record_type="untracked",
                status="??",
            )
        else:
            raise GitProbeError("Git status contained an unsupported entry type")

        if (
            len(display) < checkpoint_store.MAX_CHANGED_PATHS
            and len(changed.path) <= checkpoint_store.MAX_PATH_LEN
            and (
                changed.original_path is None
                or len(changed.original_path) <= checkpoint_store.MAX_PATH_LEN
            )
        ):
            display.append(changed)
        else:
            display_truncated = True
        index += 1

    encoded_entries = [
        json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        for item in canonical
    ]
    encoded_entries.sort()
    status_document = "[" + ",".join(encoded_entries) + "]"
    digest = hashlib.sha256(status_document.encode("ascii")).hexdigest()
    return digest, tuple(display), display_truncated


def _capture_head(root: str) -> tuple:
    symbolic = _run_git(root, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    if symbolic.returncode not in {0, 1}:
        raise GitProbeError("Git could not resolve HEAD mode")
    branch = _one_line(symbolic, "branch") if symbolic.returncode == 0 else None

    head_result = _run_git(root, ["rev-parse", "--verify", "HEAD^{commit}"])
    if head_result.returncode == 0:
        head = _one_line(head_result, "full HEAD object ID")
        if len(head) not in {40, 64} or any(c not in "0123456789abcdef" for c in head):
            raise GitProbeError("Git returned an invalid full HEAD object ID")
    elif head_result.returncode == 128:
        head = None
    else:
        raise GitProbeError("Git could not resolve HEAD")

    unborn = branch is not None and head is None
    detached = branch is None and head is not None
    if branch is None and head is None:
        raise GitProbeError("Git HEAD is neither a branch, detached commit, nor unborn branch")
    return head, branch, detached, unborn


def _capture_operations(root: str) -> tuple:
    found = []
    for operation, markers in _OPERATION_PATHS.items():
        for marker in markers:
            result = _run_git(
                root, ["rev-parse", "--path-format=absolute", "--git-path", marker]
            )
            path = _one_line(result, f"administrative path for {operation}")
            if os.path.exists(path):
                found.append(operation)
                break
    return tuple(sorted(found))


def _capture_upstream(root: str, branch: Optional[str]) -> Optional[str]:
    if branch is None:
        return None
    result = _run_git(
        root, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"]
    )
    if result.returncode != 0:
        return None
    upstream = _one_line(result, "symbolic upstream")
    if len(upstream) > 512:
        raise GitProbeError("Git upstream ref exceeded its bound")
    return upstream


def _capture_push_posture(root: str) -> PushPosture:
    push_default_result = _run_git(root, ["config", "--get", "push.default"])
    if push_default_result.returncode == 0:
        push_default = _one_line(push_default_result, "push.default")
        if len(push_default) > 64:
            raise GitProbeError("Git push.default exceeded its bound")
    elif push_default_result.returncode == 1:
        push_default = None
    else:
        raise GitProbeError("Git could not inspect push.default")

    urls_result = _run_git(
        root, ["config", "--get-regexp", r"^remote\..*\.(pushurl|url)$"]
    )
    if urls_result.returncode not in {0, 1}:
        raise GitProbeError("Git could not inspect configured push safety posture")
    sentinel = False
    if urls_result.returncode == 0:
        for line in urls_result.stdout.splitlines():
            _key, separator, value = line.partition(b" ")
            if separator and value.strip() == b"DISABLED":
                sentinel = True
                break
    return PushPosture(
        push_default=push_default,
        configured_safety_sentinel=sentinel,
    )


def _capture_git_state(root: str) -> _GitState:
    head, branch, detached, unborn = _capture_head(root)
    status = _run_git(root, ["status", "--porcelain=v2", "-z", "--untracked-files=all"])
    if status.returncode != 0:
        raise GitProbeError("Git working-state observation failed")
    status_digest, changed_paths, truncated = _parse_porcelain_v2(status.stdout)
    return _GitState(
        head=head,
        branch=branch,
        detached=detached,
        unborn=unborn,
        operation_state=_capture_operations(root),
        status_digest=status_digest,
        changed_paths=changed_paths,
        changed_paths_truncated=truncated,
        upstream_ref=_capture_upstream(root, branch),
        push_posture=_capture_push_posture(root),
    )


def _looks_absolute(path: str) -> bool:
    return (
        PurePosixPath(path).is_absolute()
        or PureWindowsPath(path).is_absolute()
        or bool(PureWindowsPath(path).drive)
    )


def _normalize_relevant_paths(paths: Sequence[str]) -> tuple:
    if isinstance(paths, (str, bytes)) or not isinstance(paths, Sequence):
        raise RelevantPathError("relevant paths must be a sequence of relative paths")
    if len(paths) > checkpoint_store.MAX_RELEVANT_FILES:
        raise RelevantPathError(
            f"relevant paths exceed maximum {checkpoint_store.MAX_RELEVANT_FILES}"
        )
    normalized = []
    seen = set()
    for raw in paths:
        if not isinstance(raw, str) or not raw.strip():
            raise RelevantPathError("each relevant path must be a non-empty string")
        if _looks_absolute(raw):
            raise RelevantPathError("relevant paths must be relative to the target")
        portable = raw.replace("\\", "/")
        parts = []
        for part in PurePosixPath(portable).parts:
            if part in {"", "."}:
                continue
            if part == "..":
                raise RelevantPathError("relevant paths must not escape the target")
            parts.append(part)
        if not parts:
            raise RelevantPathError("a relevant path must name a file")
        value = "/".join(parts)
        if len(value) > checkpoint_store.MAX_PATH_LEN:
            raise RelevantPathError(
                f"relevant path exceeds maximum length {checkpoint_store.MAX_PATH_LEN}"
            )
        if value in seen:
            raise RelevantPathError("relevant paths must be unique after normalization")
        seen.add(value)
        normalized.append(value)
    return tuple(sorted(normalized))


def _within_root(root: str, path: str) -> bool:
    try:
        return os.path.normcase(os.path.commonpath([root, path])) == os.path.normcase(root)
    except ValueError:
        return False


def _stat_fingerprint(info) -> tuple:
    identity = []
    device = getattr(info, "st_dev", 0)
    inode = getattr(info, "st_ino", 0)
    if device or inode:
        identity.extend((device, inode))
    return (
        *identity,
        info.st_mode,
        info.st_size,
        getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000)),
    )


class _FileChanged(Exception):
    pass


def _hash_regular_file_once(path: str, before) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise _FileChanged() from error
    try:
        opened_before = os.fstat(descriptor)
        if not stat.S_ISREG(opened_before.st_mode):
            raise _FileChanged()
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, FILE_READ_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
        opened_after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        path_after = os.lstat(path)
    except OSError as error:
        raise _FileChanged() from error
    fingerprints = {
        _stat_fingerprint(before),
        _stat_fingerprint(opened_before),
        _stat_fingerprint(opened_after),
        _stat_fingerprint(path_after),
    }
    if len(fingerprints) != 1 or not stat.S_ISREG(path_after.st_mode):
        raise _FileChanged()
    return digest.hexdigest()


def _observe_relevant_file(root: str, relative_path: str) -> RelevantFileObservation:
    candidate = os.path.join(root, *relative_path.split("/"))
    resolved_parent = os.path.realpath(os.path.dirname(candidate))
    if not _within_root(root, resolved_parent):
        raise RelevantPathError("relevant path resolves outside the target root")

    for attempt in range(2):
        try:
            before = os.lstat(candidate)
        except FileNotFoundError:
            return RelevantFileObservation(relative_path, None, True, False)
        except OSError as error:
            if attempt == 0:
                continue
            raise UnstableCapture("relevant file could not be observed stably") from error

        if stat.S_ISLNK(before.st_mode):
            return RelevantFileObservation(relative_path, None, False, True)
        if stat.S_ISDIR(before.st_mode):
            raise RelevantPathError("relevant paths must identify files, not directories")
        if not stat.S_ISREG(before.st_mode):
            raise RelevantPathError("relevant path is not a regular file, symlink, or missing")
        try:
            digest = _hash_regular_file_once(candidate, before)
            return RelevantFileObservation(relative_path, digest, False, False)
        except _FileChanged as error:
            if attempt == 1:
                raise UnstableCapture("relevant file changed during hashing") from error
    raise AssertionError("unreachable relevant-file retry state")


def _observe_relevant_files(root: str, paths: tuple) -> tuple:
    return tuple(_observe_relevant_file(root, path) for path in paths)


def _state_reference(
    identity: checkpoint_store.TargetIdentity,
    git_state: Optional[_GitState],
    relevant_files: tuple,
) -> str:
    document = {
        "target_key": identity.target_key,
        "target_kind": identity.kind,
        "relevant_files": [item.as_dict() for item in relevant_files],
    }
    if git_state is not None:
        document["git"] = {
            "head": git_state.head,
            "branch": git_state.branch,
            "detached": git_state.detached,
            "unborn": git_state.unborn,
            "operation_state": list(git_state.operation_state),
            "status_digest": git_state.status_digest,
        }
    encoded = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _dirty_path_state(root: str, relative_path: str) -> dict:
    """One Git-visible dirty or untracked path's content-sensitive state,
    for the validation fingerprint only (CODING-06A3 corrective 1).

    Mirrors _observe_relevant_file()'s safety properties -- O_NOFOLLOW,
    lstat before opening a regular file, one retry on an observed mid-read
    change -- but additionally captures a symlink's own target text. A
    boolean is_symlink flag (all _observe_relevant_file needs for the
    general relevant-files feature) cannot see a symlink retarget; the
    target string is this fingerprint's only content signal for a symlink,
    and it is read, never followed.

    Terminal states: "content" (regular file, sha256 of its bytes),
    "symlink" (its own target text, bounded), or "missing" (deleted, or any
    path that stopped being a regular file/symlink between measurement and
    read -- deleted/missing is represented structurally, never guessed at).
    Raises UnstableCapture if a regular file's bytes changed while being
    hashed and a single retry still observed a change -- fail closed rather
    than persist a fingerprint that never actually existed at one instant.
    """
    candidate = os.path.join(root, *relative_path.split("/"))
    for attempt in range(2):
        try:
            before = os.lstat(candidate)
        except OSError:
            return {"path": relative_path, "state": "missing"}

        if stat.S_ISLNK(before.st_mode):
            try:
                target = os.readlink(candidate)
            except OSError:
                return {"path": relative_path, "state": "missing"}
            if len(target) > MAX_SYMLINK_TARGET_CHARS:
                target = target[:MAX_SYMLINK_TARGET_CHARS]
            return {"path": relative_path, "state": "symlink", "target": target}

        if not stat.S_ISREG(before.st_mode):
            # Git only ever tracks blobs (regular files or symlinks); any
            # other entry type showing up as a Git-visible changed path is
            # unexpected -- fail closed rather than guess at its content.
            raise UnstableCapture(
                f"unsupported filesystem entry type for {relative_path!r}"
            )
        try:
            digest = _hash_regular_file_once(candidate, before)
            return {"path": relative_path, "state": "content", "sha256": digest}
        except _FileChanged as error:
            if attempt == 1:
                raise UnstableCapture(
                    f"{relative_path!r} changed during validation hashing"
                ) from error
    raise AssertionError("unreachable dirty-path retry state")


def _validation_state_fingerprint(
    identity: checkpoint_store.TargetIdentity, git_state: Optional[_GitState]
) -> tuple:
    """Content-sensitive fingerprint for machine-evidence binding only.

    CODING-06A3 corrective 1: CODING-05A2's general state_ref is Git-
    status-classification-sensitive, not content-sensitive -- an already-
    modified tracked file or an already-untracked file can have its bytes
    rewritten without status_digest (or therefore state_ref) changing at
    all, since git status only reports path/XY classification and (for
    tracked entries) the *index* object id, never the actual current
    worktree bytes. This function layers a narrower, validation-specific
    fingerprint on top rather than changing general state_ref's already-
    persisted (schema_version 1, CODING-05A2) semantics -- see the module
    docstring reasoning replicated in capture_live_observation().

    CODING-06A3 final verification corrective: content-sensitivity alone is
    not sufficient -- the *entries* list is keyed by deduplicated path only
    (never by XY status), and worktree bytes are identical for a path
    whether it is staged, unstaged, or both ("MM"). Without also folding in
    git_state.status_digest (CODING-05A2's own index/status-classification
    digest -- XY code plus HEAD and index blob ids per path), two
    observations with byte-identical dirty-path content but different
    index state (an unstaged edit vs. `git add`-ing that exact same edit,
    or a partially-staged "MM" path vs. a wholly-unstaged one with matching
    final bytes) would collide onto the same fingerprint. status_digest is
    included verbatim as an already-computed field, not recomputed or
    redesigned here.

    Returns (fingerprint_hex_or_None, complete_bool). complete is False
    (fingerprint None) only when the Git-visible changed-path list itself
    was truncated (bounded/fail-closed: a partial view of "what changed"
    cannot honestly stand behind a fingerprint claiming full coverage) --
    callers must treat that as "cannot certify this state for machine
    evidence," not as any particular content.

    HEAD anchors all *clean* tracked content (a commit is already content-
    addressed), so only Git-visible dirty tracked paths and untracked paths
    need their own bytes measured -- never a full-tree walk. Ignored files
    stay outside repository state exactly as they already do for status_
    digest; this never makes them Git-visible on their own.
    """
    if git_state is None:
        document = {
            "v": _VALIDATION_FINGERPRINT_ALGO_VERSION,
            "kind": identity.kind,
            "target_key": identity.target_key,
        }
        encoded = json.dumps(
            document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        return hashlib.sha256(encoded).hexdigest(), True

    if git_state.changed_paths_truncated:
        return None, False

    dirty_paths = sorted({changed.path for changed in git_state.changed_paths})
    entries = [
        _dirty_path_state(identity.canonical_root, path) for path in dirty_paths
    ]
    entries.sort(key=lambda item: item["path"])

    document = {
        "v": _VALIDATION_FINGERPRINT_ALGO_VERSION,
        "kind": identity.kind,
        "target_key": identity.target_key,
        "head": git_state.head,
        "branch": git_state.branch,
        "detached": git_state.detached,
        "unborn": git_state.unborn,
        "operation_state": list(git_state.operation_state),
        "status_digest": git_state.status_digest,
        "entries": entries,
    }
    encoded = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), True


def capture_live_observation(
    project: ProjectContext, relevant_paths: Sequence[str] = ()
) -> LiveObservation:
    """Capture a stable, subsystem-measured target/file observation.

    The whole A/files/B sequence is retried once.  A second inconsistency
    fails without returning or persisting a mixed observation.
    """
    normalized_paths = _normalize_relevant_paths(relevant_paths)
    for attempt in range(2):
        durable = verify_project_binding(project)
        identity_a = _resolve_target_identity(durable.root)
        git_a = (
            _capture_git_state(identity_a.canonical_root)
            if identity_a.kind == "git"
            else None
        )
        relevant_files = _observe_relevant_files(
            identity_a.canonical_root, normalized_paths
        )
        identity_b = _resolve_target_identity(durable.root)
        git_b = (
            _capture_git_state(identity_b.canonical_root)
            if identity_b.kind == "git"
            else None
        )
        # A binding can be repointed independently while observation is in
        # flight.  Re-check the immutable snapshot before accepting either
        # half; a changed durable binding is a refresh/reactivation failure,
        # never a retry against the newer root.
        verify_project_binding(project)

        if identity_a == identity_b and git_a == git_b:
            state_ref = _state_reference(identity_b, git_b, relevant_files)
            validation_state_ref, validation_state_complete = (
                _validation_state_fingerprint(identity_b, git_b)
            )
            if git_b is None:
                return LiveObservation(
                    identity=identity_b,
                    head=None,
                    branch=None,
                    detached=False,
                    unborn=False,
                    operation_state=(),
                    status_digest=None,
                    status_complete=True,
                    changed_paths=(),
                    changed_paths_truncated=False,
                    upstream_ref=None,
                    push_posture=None,
                    relevant_files=relevant_files,
                    state_ref=state_ref,
                    binding_complete=True,
                    validation_state_ref=validation_state_ref,
                    validation_state_complete=validation_state_complete,
                )
            return LiveObservation(
                identity=identity_b,
                head=git_b.head,
                branch=git_b.branch,
                detached=git_b.detached,
                unborn=git_b.unborn,
                operation_state=git_b.operation_state,
                status_digest=git_b.status_digest,
                status_complete=True,
                changed_paths=git_b.changed_paths,
                changed_paths_truncated=git_b.changed_paths_truncated,
                upstream_ref=git_b.upstream_ref,
                push_posture=git_b.push_posture,
                relevant_files=relevant_files,
                state_ref=state_ref,
                binding_complete=True,
                validation_state_ref=validation_state_ref,
                validation_state_complete=validation_state_complete,
            )
        if attempt == 1:
            raise UnstableCapture("target or repository changed during capture")
    raise AssertionError("unreachable capture retry state")


def _bind_reported_validations(reports: Sequence[dict], observation: LiveObservation) -> list:
    if isinstance(reports, (str, bytes)) or not isinstance(reports, Sequence):
        raise checkpoint_store.CheckpointValidationError(
            "reported_validations must be a sequence"
        )
    if len(reports) > checkpoint_store.MAX_VALIDATIONS:
        raise checkpoint_store.CheckpointValidationError(
            f"reported_validations exceed maximum {checkpoint_store.MAX_VALIDATIONS}"
        )
    bound = []
    for index, report in enumerate(reports):
        if not isinstance(report, dict):
            raise checkpoint_store.CheckpointValidationError(
                f"reported_validations[{index}] must be an object"
            )
        extras = set(report) - _REPORTED_VALIDATION_KEYS
        if extras:
            raise checkpoint_store.CheckpointValidationError(
                f"reported_validations[{index}] contains caller-forbidden fields: "
                f"{sorted(extras)}"
            )
        value = dict(report)
        value.update({
            "source": "reported",
            "state_ref": observation.state_ref,
            "binding_complete": observation.binding_complete,
        })
        bound.append(value)
    return bound


def _machine_validation_label(record: validation_evidence.EvidenceRecord) -> str:
    if record.scope_key == "full":
        label = "pytest:full"
    else:
        count = len(record.selectors)
        suffix = "+" if record.selectors_truncated else ""
        label = f"pytest:{count}{suffix} selector{'s' if count != 1 else ''}"
    return label[: checkpoint_store.MAX_VALIDATION_LABEL_LEN]


def _machine_validation_summary(record: validation_evidence.EvidenceRecord) -> str:
    counts = record.counts if isinstance(record.counts, dict) else {}
    summary = (
        f"passed={counts.get('passed', 0)} failed={counts.get('failed', 0)} "
        f"errors={counts.get('errors', 0)} collected={counts.get('collected', 0)}"
    )
    return summary[: checkpoint_store.MAX_VALIDATION_SUMMARY_LEN]


def _machine_validation_dict(record: validation_evidence.EvidenceRecord) -> dict:
    """Translate one internal EvidenceRecord into the checkpoint store's
    validation-record shape. This is the ONLY place a validation dict with
    source == MACHINE_VALIDATION_SOURCE is ever constructed -- always from
    an EvidenceRecord that itself only ever came from
    core.coding_validation_evidence.lookup_compatible_evidence(), never
    from model-visible JSON or caller input."""
    return {
        "label": _machine_validation_label(record),
        "outcome": record.outcome,
        "source": checkpoint_store.MACHINE_VALIDATION_SOURCE,
        "exit_code": record.exit_code,
        "summary": _machine_validation_summary(record),
        "timestamp": record.created_at,
        "state_ref": record.state_ref,
        "binding_complete": True,
    }


def _bind_machine_evidence(project_name: str, observation: LiveObservation) -> list:
    """Automatic association (CODING-06A3 section 12): looks up durable
    Lumina-local evidence compatible with THIS exact observation's
    (project_name, target_key, validation_state_ref) and returns it in
    checkpoint validation-record shape, newest-per-scope. Evidence is keyed
    by the content-sensitive validation fingerprint (corrective 1), not the
    general state_ref -- a lookup with an incomplete fingerprint (bounded
    changed-path observation was truncated) or a lookup failure both
    degrade to "no machine evidence this save" rather than blocking the
    checkpoint save itself -- evidence lookup and checkpoint save are
    separate truths, same as core.coding_validation_evidence's own
    persistence failures never blocking a real test result."""
    if not observation.validation_state_complete or observation.validation_state_ref is None:
        return []
    try:
        records = validation_evidence.lookup_compatible_evidence(
            project_name=project_name,
            target_key=observation.identity.target_key,
            state_ref=observation.validation_state_ref,
        )
    except Exception:
        return []
    return [
        _machine_validation_dict(record)
        for record in records[: checkpoint_store.MAX_MACHINE_VALIDATIONS]
    ]


def _live_machine_validations(project_name: str, current: LiveObservation) -> tuple:
    """CODING-06A3 corrective 2: the read-time counterpart of
    _bind_machine_evidence() above -- a fresh lookup against CURRENT
    measured state, never the checkpoint row's baked machine_validations
    from whenever it was last saved. This is the sole authority for what
    read_coding_checkpoint shows as machine evidence: the durable evidence
    store's newest compatible record per scope wins, full stop. A stale
    baked PASS can never outrank a newer machine FAIL (or vice versa)
    because the baked list is never consulted here at all. Fails closed
    (empty tuple) on an incomplete fingerprint or a lookup error -- never
    falls back to the baked list, and never raises out to the caller."""
    if not current.validation_state_complete or current.validation_state_ref is None:
        return ()
    try:
        records = validation_evidence.lookup_compatible_evidence(
            project_name=project_name,
            target_key=current.identity.target_key,
            state_ref=current.validation_state_ref,
        )
    except Exception:
        return ()
    return tuple(
        _machine_validation_dict(record)
        for record in records[: checkpoint_store.MAX_MACHINE_VALIDATIONS]
    )


def save_live_checkpoint(
    project: ProjectContext,
    workflow: dict,
    *,
    relevant_paths: Sequence[str] = (),
    reported_validations: Sequence[dict] = (),
    expected_revision: int,
) -> SavedLiveCheckpoint:
    """Capture facts, bind caller reports, validate with A1, and CAS-save."""
    observation = capture_live_observation(project, relevant_paths)
    verify_project_binding(project)
    payload = {
        "workflow": workflow,
        "relevant_files": observation.relevant_file_payload(),
        "changed_paths": observation.changed_path_payload(),
        "validations": _bind_reported_validations(reported_validations, observation),
        "machine_validations": _bind_machine_evidence(project.name, observation),
        "observation": observation.observation_payload(),
    }
    record = checkpoint_store._save_checkpoint_for_identity(
        project.name,
        observation.identity,
        payload,
        expected_revision=expected_revision,
        refuse_unreadable_existing=True,
    )
    return SavedLiveCheckpoint(record=record, observation=observation)


def _normalized_file_payload(items: Sequence[dict]) -> list:
    return sorted(
        [
            {
                "path": item.get("path"),
                "sha256": item.get("sha256"),
                "missing": item.get("missing"),
                "is_symlink": item.get("is_symlink"),
            }
            for item in items
        ],
        key=lambda item: item["path"],
    )


def compare_freshness(
    recorded_observation: Optional[dict],
    recorded_relevant_files: Sequence[dict],
    current: LiveObservation,
) -> FreshnessResult:
    if recorded_observation is None:
        return FreshnessResult(UNVERIFIABLE, ("recorded_observation_missing",))
    if not recorded_observation.get("binding_complete") or not current.binding_complete:
        return FreshnessResult(UNVERIFIABLE, ("observation_incomplete",))
    if (
        recorded_observation.get("target_kind") == "git"
        and not recorded_observation.get("status_complete")
    ):
        return FreshnessResult(UNVERIFIABLE, ("working_state_incomplete",))

    reasons = []
    comparisons = (
        ("target_key", current.identity.target_key, "target_identity_changed"),
        ("target_kind", current.identity.kind, "target_kind_changed"),
        ("canonical_root", current.identity.canonical_root, "target_root_changed"),
    )
    for key, current_value, reason in comparisons:
        if recorded_observation.get(key) != current_value:
            reasons.append(reason)

    if recorded_observation.get("target_kind") == "git" and current.identity.kind == "git":
        git_comparisons = (
            ("head", current.head, "head_changed"),
            ("branch", current.branch, "branch_changed"),
            ("detached", current.detached, "head_mode_changed"),
            ("unborn", current.unborn, "head_mode_changed"),
            ("operation_state", list(current.operation_state), "operation_state_changed"),
            ("status_digest", current.status_digest, "working_state_changed"),
            ("upstream_ref", current.upstream_ref, "upstream_changed"),
            (
                "push_posture",
                current.push_posture.as_dict() if current.push_posture else None,
                "push_posture_changed",
            ),
        )
        for key, current_value, reason in git_comparisons:
            if recorded_observation.get(key) != current_value and reason not in reasons:
                reasons.append(reason)

    if _normalized_file_payload(recorded_relevant_files) != _normalized_file_payload(
        current.relevant_file_payload()
    ):
        reasons.append("relevant_files_changed")

    if not reasons and recorded_observation.get("state_ref") != current.state_ref:
        reasons.append("state_reference_changed")
    return FreshnessResult(STALE if reasons else FRESH, tuple(reasons))


def _applicability_for(validations, reference: Optional[str]) -> tuple:
    """Shared by reported and machine-evidence validations alike: an
    entry's own recorded state_ref must exactly match the CURRENT
    reference value for it to apply. Source plays no role in this
    comparison -- a lumina_local entry goes stale under an unchanged
    repository exactly the same way a reported one does (CODING-06A3
    section 21: stale evidence must never present as current, fail closed
    by omission of 'applies'). `reference` is the caller's current measured
    value to compare against -- the general state_ref for reported
    validations, or the content-sensitive validation_state_ref (corrective
    1) for machine-evidence validations; a None reference (e.g. an
    incomplete validation fingerprint) never matches anything."""
    results = []
    for validation in validations:
        complete = validation.get("binding_complete") is True
        matches = (
            complete and reference is not None and validation.get("state_ref") == reference
        )
        if not complete:
            reason = "binding_incomplete"
        elif not matches:
            reason = "captured_state_no_longer_matches"
        else:
            reason = "captured_state_matches"
        results.append(ValidationApplicability(
            label=validation["label"],
            outcome=validation["outcome"],
            source=validation["source"],
            applies_to_current_state=matches,
            reason=reason,
        ))
    return tuple(results)


def _validation_applicability(record, current: LiveObservation) -> tuple:
    return _applicability_for(record.validations, current.state_ref)


def _machine_validation_applicability(live_machine_validations, current: LiveObservation) -> tuple:
    return _applicability_for(live_machine_validations, current.validation_state_ref)


def _find_checkpoint_row(project_name: str, identity) -> Optional[object]:
    checkpoint_store.init_checkpoint_db()
    from core.db import connect
    connection = connect()
    try:
        row = connection.execute(
            "SELECT * FROM coding_checkpoints WHERE project_name = ? AND target_key = ?",
            (project_name, identity.target_key),
        ).fetchone()
        if row is not None:
            return row
        displays = (
            f"git:{identity.canonical_root}",
            f"directory:{identity.canonical_root}",
        )
        row = connection.execute(
            """SELECT * FROM coding_checkpoints
               WHERE project_name = ? AND target_display IN (?, ?)
               ORDER BY updated_at DESC, revision DESC LIMIT 1""",
            (project_name, *displays),
        ).fetchone()
        if row is not None:
            return row

        # A binding may name a subdirectory of a Git worktree.  If that
        # repository later disappears, Git can no longer recover the old
        # toplevel from the subdirectory, so target_display cannot match.
        # A sole row is still unambiguous for this Project and lets the
        # freshness engine report target_identity_changed.  Multiple rows
        # fail closed as not-found rather than guessing which target won.
        candidates = connection.execute(
            """SELECT * FROM coding_checkpoints WHERE project_name = ?
               ORDER BY updated_at DESC, revision DESC LIMIT 2""",
            (project_name,),
        ).fetchall()
        return candidates[0] if len(candidates) == 1 else None
    finally:
        connection.close()


def _safe_metadata(row, project_name: str, status: str) -> SafeRowMetadata:
    key = row["target_key"] if isinstance(row["target_key"], str) else ""
    return SafeRowMetadata(
        project_name=project_name,
        target_key_prefix=key[:12],
        revision=row["revision"] if isinstance(row["revision"], int) else 0,
        schema_version=(
            row["schema_version"] if isinstance(row["schema_version"], int) else 0
        ),
        status=status,
    )


def load_live_checkpoint(project: ProjectContext) -> LiveCheckpointRead:
    """Load recorded state, recapture reality, and compare without mutation."""
    durable = verify_project_binding(project)
    initial_identity = _resolve_target_identity(durable.root)
    row = _find_checkpoint_row(project.name, initial_identity)
    if row is None:
        current = capture_live_observation(project, ())
        return LiveCheckpointRead(
            status="not_found",
            record=None,
            current=current,
            freshness=FreshnessResult(UNVERIFIABLE, ("checkpoint_not_found",)),
            validation_applicability=(),
        )

    try:
        record = checkpoint_store._row_to_record(row, project.name)
    except checkpoint_store.UnsupportedSchema:
        return LiveCheckpointRead(
            status="unsupported_schema",
            record=None,
            current=None,
            freshness=FreshnessResult(UNVERIFIABLE, ("unsupported_schema",)),
            validation_applicability=(),
            metadata=_safe_metadata(row, project.name, "unsupported_schema"),
        )
    except checkpoint_store.CheckpointCorrupt:
        return LiveCheckpointRead(
            status="corrupt",
            record=None,
            current=None,
            freshness=FreshnessResult(UNVERIFIABLE, ("checkpoint_corrupt",)),
            validation_applicability=(),
            metadata=_safe_metadata(row, project.name, "corrupt"),
        )

    relevant_paths = tuple(item["path"] for item in record.relevant_files)
    current = capture_live_observation(project, relevant_paths)
    freshness = compare_freshness(record.observation, record.relevant_files, current)
    # CODING-06A3 corrective 2: the machine-evidence view shown by a bare
    # read is always a FRESH lookup against current measured state, never
    # this row's baked machine_validations from whenever it was last saved
    # -- see _live_machine_validations()'s docstring for the precedence
    # rule this implements. No resave, no revision bump: this is read-only.
    live_machine = _live_machine_validations(project.name, current)
    return LiveCheckpointRead(
        status="ok",
        record=record,
        current=current,
        freshness=freshness,
        validation_applicability=_validation_applicability(record, current),
        machine_validation_applicability=_machine_validation_applicability(live_machine, current),
        live_machine_validations=live_machine,
    )
