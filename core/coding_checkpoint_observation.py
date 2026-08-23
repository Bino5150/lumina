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
import subprocess
import threading
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Optional, Sequence

import core.coding_checkpoint as checkpoint_store
from core.project_context import ProjectContext, load_project_binding


GIT_TIMEOUT_SECONDS = 5
MAX_GIT_STDOUT_BYTES = 8 * 1024 * 1024
MAX_GIT_STDERR_BYTES = 16 * 1024
FILE_READ_CHUNK_BYTES = 128 * 1024

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
    metadata: Optional[SafeRowMetadata] = None


@dataclass(frozen=True)
class _GitCommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


def _bounded_reader(stream, limit: int, sink: list, overflow: threading.Event):
    total = 0
    while True:
        chunk = stream.read(64 * 1024)
        if not chunk:
            break
        remaining = limit - total
        if remaining > 0:
            sink.append(chunk[:remaining])
        total += len(chunk)
        if total > limit:
            overflow.set()


def _run_git(root: str, args: Sequence[str]) -> _GitCommandResult:
    """Run Git with argument arrays, hard time/output bounds, and no prompts."""
    env = os.environ.copy()
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "LC_ALL": "C",
    })
    try:
        process = subprocess.Popen(
            ["git", "-C", root, *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            env=env,
        )
    except OSError as error:
        raise GitProbeError("Git executable is unavailable") from error

    stdout_parts = []
    stderr_parts = []
    stdout_overflow = threading.Event()
    stderr_overflow = threading.Event()
    stdout_thread = threading.Thread(
        target=_bounded_reader,
        args=(process.stdout, MAX_GIT_STDOUT_BYTES, stdout_parts, stdout_overflow),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_bounded_reader,
        args=(process.stderr, MAX_GIT_STDERR_BYTES, stderr_parts, stderr_overflow),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    try:
        process.wait(timeout=GIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        process.kill()
        process.wait()
        stdout_thread.join()
        stderr_thread.join()
        raise GitProbeError("Git probe timed out") from error

    stdout_thread.join()
    stderr_thread.join()
    if stdout_overflow.is_set() or stderr_overflow.is_set():
        raise GitProbeError("Git probe output exceeded its bounded capture limit")
    return _GitCommandResult(
        returncode=process.returncode,
        stdout=b"".join(stdout_parts),
        stderr=b"".join(stderr_parts),
    )


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


def _validation_applicability(record, current: LiveObservation) -> tuple:
    results = []
    for validation in record.validations:
        complete = validation.get("binding_complete") is True
        matches = complete and validation.get("state_ref") == current.state_ref
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
    return LiveCheckpointRead(
        status="ok",
        record=record,
        current=current,
        freshness=freshness,
        validation_applicability=_validation_applicability(record, current),
    )
