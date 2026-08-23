"""
core/coding_checkpoint.py -- CODING-05A1: durable checkpoint foundation.

Storage-only slice. No model-facing tool, no agent/profile wiring, no
compaction integration. A coding-agent workflow checkpoint is a small,
bounded, untrusted record of "what task, what phase, what's next" for one
(Project name, target) pair, persisted through the same WAL-mode SQLite
database every other module uses (core/db.py's connect()).

Identity model
--------------
A checkpoint's storage key is (project_name, target_key). project_name is
whatever core.project_context already validates as a Project name -- reused
here, not reinvented, so the two systems can't silently drift on what a
valid name looks like. target_key is an opaque SHA-256 digest computed by
resolve_target_identity() over a canonical identity document containing
only target kind ("git" or "directory"), canonical root, the Git
administrative directory when applicable, and filesystem identity (device +
inode) only when os.stat() actually produced one. It deliberately excludes
branch and HEAD -- this is a stable "which repo/directory is this" key, not
a snapshot of repo state.

Concurrency model
------------------
Optimistic concurrency via expected_revision + SQLite's own serialization
(BEGIN IMMEDIATE takes the write lock up front; busy_timeout, set by
core.db.connect(), queues a second writer instead of erroring). No
in-process lock guards this -- deliberately. The point of the exercise is
that SQLite itself is the correctness boundary; a Python-level lock would
only paper over whether that's actually true. Verified directly: two
independent connections racing BEGIN IMMEDIATE against the same row, one
serializes behind the other's transaction and then correctly observes the
revision has moved, never a lost update.

Schema/corruption handling
---------------------------
A row's schema_version column gates whether its JSON payload is even
attempted to be parsed. A schema_version this code doesn't recognize raises
UnsupportedSchema without touching the row. A recognized schema_version
whose payload doesn't parse as JSON raises CheckpointCorrupt. A recognized
schema_version whose payload parses but doesn't match the expected shape
also raises CheckpointCorrupt (a supported version producing a malformed
document is corruption, not a future format). Neither path deletes,
rewrites, or "helpfully" migrates the row.
"""
import hashlib
import json
import os
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath, PureWindowsPath
from typing import Optional

from core.project_context import validate_project_name

SCHEMA_VERSION = 2
_KNOWN_SCHEMA_VERSIONS = {1, 2}
_TARGET_IDENTITY_ALGO_VERSION = 1
_GIT_PROBE_TIMEOUT = 5

# CODING-06A3: schema_version 2 adds the machine_validations envelope key
# (see _CHECKPOINT_TOP_KEYS below). A v1 row predates it and never has it;
# _validate_checkpoint() treats an absent key as [] for either version, so
# old rows keep loading exactly as they always have. Every NEW save (an
# insert or an update of a loaded v1 row) now writes SCHEMA_VERSION=2 --
# see _save_checkpoint_for_identity(), unchanged in that respect. A prior
# Lumina build that only knows _KNOWN_SCHEMA_VERSIONS={1} raises
# UnsupportedSchema on a v2 row before ever parsing its payload, rather
# than silently mishandling a machine_validations key it doesn't expect.

# ---------------------------------------------------------------------------
# Bounds. Deliberately stricter per-field than the ~32 KiB total cap alone
# would require -- a single giant summary or next_step must not be able to
# consume the whole budget by itself. See tests/test_coding_checkpoint_store.py
# for the actual-encoded-size proof against MAX_TOTAL_PAYLOAD_BYTES.
# ---------------------------------------------------------------------------
MAX_TASK_ID_LEN = 128
MAX_TITLE_LEN = 150
MAX_SLICE_ID_LEN = 100
MAX_SUMMARY_LEN = 1000
MAX_NEXT_STEPS = 8
MAX_NEXT_STEP_LEN = 160
MAX_BLOCKERS = 8
MAX_BLOCKER_LEN = 160
MAX_RELEVANT_FILES = 32
MAX_CHANGED_PATHS = 64
MAX_PATH_LEN = 110
MAX_VALIDATIONS = 8
MAX_VALIDATION_LABEL_LEN = 48
MAX_VALIDATION_SUMMARY_LEN = 240
MAX_VALIDATION_STATE_REF_LEN = 100
MAX_VALIDATION_TIMESTAMP_LEN = 40
MAX_MACHINE_VALIDATIONS = 8
MAX_TOTAL_PAYLOAD_BYTES = 32 * 1024

VALID_PHASES = {
    "planned", "in_progress", "validating", "reviewing",
    "blocked", "paused", "complete",
}
VALID_VALIDATION_OUTCOMES = {"pass", "fail", "error", "skipped"}
_VALID_VALIDATION_SOURCE = "reported"
# CODING-06A3: the only other legal validation source, and the only one
# that means "Lumina itself launched and observed this" rather than "a
# model said so." Never accepted through save_coding_checkpoint's
# model-facing path -- see core/coding_checkpoint_observation.py's
# _bind_reported_validations(), which unconditionally forces "reported"
# regardless of what a caller supplies. Only
# core.coding_checkpoint_observation._bind_machine_evidence() ever
# constructs a validation dict with this source, from an internal
# core.coding_validation_evidence.EvidenceRecord -- never from a
# model-visible JSON string.
MACHINE_VALIDATION_SOURCE = "lumina_local"

_CHECKPOINT_TOP_KEYS = {
    "workflow", "relevant_files", "changed_paths", "validations",
    "machine_validations", "observation",
}
_WORKFLOW_KEYS = {"task_id", "title", "phase", "slice_id", "summary", "next_steps", "blockers"}
_PATH_RECORD_KEYS = {"path", "sha256", "missing", "is_symlink"}
_CHANGED_PATH_RECORD_KEYS = _PATH_RECORD_KEYS | {
    "record_type", "status", "original_path",
}
_VALIDATION_KEYS = {
    "label", "outcome", "exit_code", "summary", "timestamp", "source",
    "state_ref", "binding_complete",
}
_OBSERVATION_KEYS = {
    "target_key", "target_kind", "canonical_root", "head", "branch",
    "detached", "unborn", "operation_state", "status_digest",
    "status_complete", "changed_paths_truncated", "upstream_ref",
    "push_posture", "state_ref", "binding_complete",
}
_PUSH_POSTURE_KEYS = {"push_default", "configured_safety_sentinel"}
_VALID_OPERATION_STATES = {
    "merge", "cherry_pick", "rebase", "revert", "bisect",
}
_VALID_CHANGED_RECORD_TYPES = {"ordinary", "rename", "unmerged", "untracked"}


class CheckpointError(Exception):
    """Base class for every error this module raises."""


class RevisionConflict(CheckpointError):
    """expected_revision didn't match the row's current revision. Never a
    silent overwrite -- the caller must re-read and retry."""


class CheckpointNotFound(CheckpointError):
    """No row exists for this (project_name, target_key)."""


class CheckpointCorrupt(CheckpointError):
    """A row exists, its schema_version is recognized, but its payload
    doesn't parse as JSON or doesn't match the expected shape. The row is
    left exactly as found."""


class UnsupportedSchema(CheckpointError):
    """A row exists but its schema_version is not one this code understands.
    The row is left exactly as found -- never migrated, never touched."""


class CheckpointValidationError(CheckpointError):
    """The caller-supplied checkpoint content violates the bounded schema
    (unknown/forbidden field, bad type, out-of-range value, or over a
    length/count bound)."""


class CheckpointTooLarge(CheckpointValidationError):
    """The validated checkpoint exceeds the durable payload byte cap."""


@dataclass(frozen=True)
class TargetIdentity:
    kind: str               # "git" | "directory"
    canonical_root: str
    target_key: str         # opaque sha256 hex digest
    target_display: str     # human-readable, e.g. "git:/home/bino/lumina-release"


@dataclass(frozen=True)
class CheckpointRecord:
    project_name: str
    target_key: str
    target_display: str
    target_kind: str
    revision: int
    schema_version: int
    workflow: dict
    relevant_files: list
    changed_paths: list
    validations: list
    machine_validations: list
    observation: Optional[dict]
    created_at: str
    updated_at: str


# ---------------------------------------------------------------------------
# Schema creation
# ---------------------------------------------------------------------------

def init_checkpoint_db():
    """Create the coding_checkpoints table if it doesn't exist. Safe to call
    on every operation -- CREATE TABLE IF NOT EXISTS, no-op after the first
    call. Never captures a DB path at import time: connect() reads
    config.DB_PATH fresh on every call, so tests that monkeypatch
    config.DB_PATH per-test see it here too."""
    from core.db import connect
    # Two first-ever callers can both reach core.db.connect() while the
    # database is still switching into WAL mode. SQLite's journal-mode
    # transition may surface SQLITE_BUSY before core.db has installed its
    # explicit busy_timeout. Retry that narrow initialization race only;
    # ordinary transaction contention remains SQLite/BEGIN IMMEDIATE's job.
    conn = None
    for attempt in range(20):
        try:
            conn = connect()
            break
        except sqlite3.OperationalError as error:
            if (
                getattr(error, "sqlite_errorcode", None) != sqlite3.SQLITE_BUSY
                or attempt == 19
            ):
                raise
            time.sleep(0.01)
    if conn is None:  # pragma: no cover - defensive, loop either sets or raises
        raise CheckpointError("could not initialize checkpoint database")
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS coding_checkpoints (
                project_name   TEXT NOT NULL,
                target_key     TEXT NOT NULL,
                target_display TEXT NOT NULL,
                target_kind    TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                revision       INTEGER NOT NULL,
                payload        TEXT NOT NULL,
                created_at     TEXT NOT NULL,
                updated_at     TEXT NOT NULL,
                PRIMARY KEY (project_name, target_key)
            )
        """)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Target identity resolution
# ---------------------------------------------------------------------------

def _filesystem_identity(path: str) -> Optional[dict]:
    """Best-effort (dev, ino) pair for path, or None when unavailable/not
    meaningful. Never raises -- any stat failure just means this signal is
    omitted from the identity document, not that resolution fails."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    ino = getattr(st, "st_ino", 0)
    dev = getattr(st, "st_dev", 0)
    if not ino:
        return None
    return {"dev": dev, "ino": ino}


def _probe_git(canonical_root: str):
    """Returns (toplevel, git_common_dir) both already absolute+resolved, or
    (None, None) if canonical_root isn't inside a Git working tree (or git
    isn't available, or the probe times out). Uses a single read-only
    argument-array invocation -- no shell, no write operations. --git-common-dir
    (not --git-dir) is what makes this correct for worktrees: it resolves to
    the shared main repository's .git directory even when canonical_root is a
    linked worktree whose own .git is just a file pointing back at it."""
    try:
        result = subprocess.run(
            ["git", "-C", canonical_root, "rev-parse",
             "--path-format=absolute", "--show-toplevel", "--git-common-dir"],
            capture_output=True, text=True, timeout=_GIT_PROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None

    if result.returncode != 0:
        return None, None

    lines = result.stdout.splitlines()
    if len(lines) < 2 or not lines[0].strip() or not lines[1].strip():
        return None, None

    toplevel = os.path.realpath(lines[0].strip())
    git_common_dir = os.path.realpath(lines[1].strip())
    return toplevel, git_common_dir


def resolve_target_identity(root: str) -> TargetIdentity:
    """Smallest target-identity resolver storage needs: is `root` a Git
    working tree or a plain directory, and what's its canonical, stable
    identity. Never captures branch, HEAD, working-tree status, or remotes
    -- that's CODING-05A2's job. Deterministic: the same real target always
    produces the same target_key, independent of which equivalent path
    string (symlink, trailing slash, subdirectory-of-a-repo) was passed in."""
    canonical_root = os.path.realpath(os.path.expanduser(root))

    kind = "directory"
    resolved_root = canonical_root
    git_common_dir = None

    toplevel, common_dir = _probe_git(canonical_root)
    if toplevel is not None:
        kind = "git"
        resolved_root = toplevel
        git_common_dir = common_dir

    identity_doc = {
        "v": _TARGET_IDENTITY_ALGO_VERSION,
        "kind": kind,
        "root": resolved_root,
    }
    if git_common_dir is not None:
        identity_doc["git_common_dir"] = git_common_dir

    fs_id = _filesystem_identity(resolved_root)
    if fs_id is not None:
        identity_doc["fs"] = fs_id

    canonical_json = json.dumps(identity_doc, sort_keys=True, separators=(",", ":"))
    target_key = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    target_display = f"{kind}:{resolved_root}"

    return TargetIdentity(
        kind=kind,
        canonical_root=resolved_root,
        target_key=target_key,
        target_display=target_display,
    )


def _build_target_identity(
    kind: str, resolved_root: str, git_common_dir: Optional[str]
) -> TargetIdentity:
    """Build the stable A1 identity from already-measured target facts.

    A2 uses this seam after its bounded Git discovery so the store writes
    under the exact identity that was observed, rather than probing a
    second time after capture and potentially selecting a different target.
    It remains private: callers never get to submit a target key.
    """
    if kind not in {"git", "directory"}:
        raise ValueError(f"unsupported target kind: {kind!r}")
    resolved_root = os.path.realpath(resolved_root)
    if kind == "git":
        if not git_common_dir:
            raise ValueError("git_common_dir is required for a Git target")
        git_common_dir = os.path.realpath(git_common_dir)
    else:
        git_common_dir = None

    identity_doc = {
        "v": _TARGET_IDENTITY_ALGO_VERSION,
        "kind": kind,
        "root": resolved_root,
    }
    if git_common_dir is not None:
        identity_doc["git_common_dir"] = git_common_dir

    fs_id = _filesystem_identity(resolved_root)
    if fs_id is not None:
        identity_doc["fs"] = fs_id

    canonical_json = json.dumps(identity_doc, sort_keys=True, separators=(",", ":"))
    target_key = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    target_display = f"{kind}:{resolved_root}"

    return TargetIdentity(
        kind=kind,
        canonical_root=resolved_root,
        target_key=target_key,
        target_display=target_display,
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _fail(msg: str):
    raise CheckpointValidationError(msg)


def _check_keys(d, allowed: set, where: str):
    if not isinstance(d, dict):
        _fail(f"{where} must be an object")
    extra = set(d.keys()) - allowed
    if extra:
        _fail(f"{where} contains unknown or forbidden field(s): {sorted(extra)}")


def _require_str(d: dict, key: str, where: str, max_len: int) -> str:
    v = d.get(key)
    if not isinstance(v, str) or not v.strip():
        _fail(f"{where}.{key} must be a non-empty string")
    if len(v) > max_len:
        _fail(f"{where}.{key} exceeds max length {max_len}")
    return v


def _optional_str(d: dict, key: str, where: str, max_len: int) -> Optional[str]:
    if key not in d or d.get(key) is None:
        return None
    v = d[key]
    if not isinstance(v, str):
        _fail(f"{where}.{key} must be a string")
    if len(v) > max_len:
        _fail(f"{where}.{key} exceeds max length {max_len}")
    return v


def _looks_absolute(path: str) -> bool:
    """Platform-neutral absolute-path check -- rejects both POSIX-style and
    Windows-style absolute paths regardless of the OS actually running this
    code, since a checkpoint written on one platform may be inspected on
    another."""
    return PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()


def _validate_relative_path(path: str, where: str) -> str:
    if not isinstance(path, str) or not path.strip():
        _fail(f"{where} must be a non-empty string")
    if len(path) > MAX_PATH_LEN:
        _fail(f"{where} exceeds max length {MAX_PATH_LEN}")
    if _looks_absolute(path):
        _fail(f"{where} must be relative, got absolute path: {path!r}")
    parts = PurePosixPath(path.replace("\\", "/")).parts
    if ".." in parts:
        _fail(f"{where} must not contain '..' segments: {path!r}")
    return path


def _validate_path_record(item, where: str) -> dict:
    _check_keys(item, _PATH_RECORD_KEYS, where)
    path = _require_str(item, "path", where, MAX_PATH_LEN)
    _validate_relative_path(path, f"{where}.path")

    sha256 = _optional_str(item, "sha256", where, 64)
    if sha256 is not None:
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            _fail(f"{where}.sha256 must be a 64-char lowercase hex digest")

    missing = item.get("missing")
    if missing is not None and not isinstance(missing, bool):
        _fail(f"{where}.missing must be a bool or omitted")

    is_symlink = item.get("is_symlink")
    if is_symlink is not None and not isinstance(is_symlink, bool):
        _fail(f"{where}.is_symlink must be a bool or omitted")

    return {"path": path, "sha256": sha256, "missing": missing, "is_symlink": is_symlink}


def _validate_changed_path_record(item, where: str) -> dict:
    """Accept A1's original path-fact shape and A2's structural Git shape.

    A2 never flattens a rename into display prose: ``path`` is the new path
    and ``original_path`` is the old path. Keeping A1's four optional fields
    accepted preserves already-written schema-version-1 rows.
    """
    _check_keys(item, _CHANGED_PATH_RECORD_KEYS, where)
    structural = any(
        key in item for key in ("record_type", "status", "original_path")
    )
    if structural and any(
        key in item for key in ("sha256", "missing", "is_symlink")
    ):
        _fail(f"{where} must not mix Git status fields with file-fact fields")
    if structural:
        path = _require_str(item, "path", where, MAX_PATH_LEN)
        _validate_relative_path(path, f"{where}.path")
        base = {"path": path}
    else:
        base = _validate_path_record(item, where)
    record_type = _optional_str(item, "record_type", where, 20)
    if record_type is not None and record_type not in _VALID_CHANGED_RECORD_TYPES:
        _fail(
            f"{where}.record_type must be one of "
            f"{sorted(_VALID_CHANGED_RECORD_TYPES)}, got {record_type!r}"
        )
    status = _optional_str(item, "status", where, 8)
    original_path = _optional_str(item, "original_path", where, MAX_PATH_LEN)
    if original_path is not None:
        _validate_relative_path(original_path, f"{where}.original_path")
    if record_type == "rename" and original_path is None:
        _fail(f"{where}.original_path is required for a rename")
    result = dict(base)
    if "record_type" in item:
        result["record_type"] = record_type
    if "status" in item:
        result["status"] = status
    if "original_path" in item:
        result["original_path"] = original_path
    return result


def _validate_path_list(items, where: str, max_count: int) -> list:
    if items is None:
        return []
    if not isinstance(items, list):
        _fail(f"{where} must be a list")
    if len(items) > max_count:
        _fail(f"{where} exceeds max count {max_count}")
    return [_validate_path_record(item, f"{where}[{i}]") for i, item in enumerate(items)]


def _validate_changed_paths(items, where: str) -> list:
    if items is None:
        return []
    if not isinstance(items, list):
        _fail(f"{where} must be a list")
    if len(items) > MAX_CHANGED_PATHS:
        _fail(f"{where} exceeds max count {MAX_CHANGED_PATHS}")
    return [
        _validate_changed_path_record(item, f"{where}[{i}]")
        for i, item in enumerate(items)
    ]


def _validate_validation_record(item, where: str, *, expected_source: str = _VALID_VALIDATION_SOURCE) -> dict:
    _check_keys(item, _VALIDATION_KEYS, where)
    label = _require_str(item, "label", where, MAX_VALIDATION_LABEL_LEN)
    outcome = _require_str(item, "outcome", where, 20)
    if outcome not in VALID_VALIDATION_OUTCOMES:
        _fail(f"{where}.outcome must be one of {sorted(VALID_VALIDATION_OUTCOMES)}, got {outcome!r}")

    source = item.get("source")
    if source != expected_source:
        reason = (
            "a reported validation is not a verified one"
            if expected_source == _VALID_VALIDATION_SOURCE
            else "machine evidence must originate from Lumina's own execution path"
        )
        _fail(f"{where}.source must be exactly {expected_source!r} -- {reason}")

    exit_code = item.get("exit_code")
    if exit_code is not None:
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            _fail(f"{where}.exit_code must be an int or omitted")
        if not (-1 <= exit_code <= 255):
            _fail(f"{where}.exit_code out of range: {exit_code}")

    summary = _optional_str(item, "summary", where, MAX_VALIDATION_SUMMARY_LEN)
    timestamp = _optional_str(item, "timestamp", where, MAX_VALIDATION_TIMESTAMP_LEN)
    state_ref = _optional_str(item, "state_ref", where, MAX_VALIDATION_STATE_REF_LEN)
    binding_complete = item.get("binding_complete")
    if binding_complete is not None and not isinstance(binding_complete, bool):
        _fail(f"{where}.binding_complete must be a bool or omitted")

    result = {
        "label": label,
        "outcome": outcome,
        "source": source,
        "exit_code": exit_code,
        "summary": summary,
        "timestamp": timestamp,
        "state_ref": state_ref,
    }
    if "binding_complete" in item:
        result["binding_complete"] = binding_complete
    return result


def _validate_validations(items, where: str) -> list:
    if items is None:
        return []
    if not isinstance(items, list):
        _fail(f"{where} must be a list")
    if len(items) > MAX_VALIDATIONS:
        _fail(f"{where} exceeds max count {MAX_VALIDATIONS}")
    return [_validate_validation_record(item, f"{where}[{i}]") for i, item in enumerate(items)]


def _validate_machine_validations(items, where: str) -> list:
    """Same shape as _validate_validations(), but every entry must carry
    source == MACHINE_VALIDATION_SOURCE. Only
    core.coding_checkpoint_observation._bind_machine_evidence() ever
    populates this list before it reaches _validate_checkpoint() -- there
    is no model-facing path that can supply this key at all (see
    tools/coding_checkpoint.py's save_coding_checkpoint schema, which
    doesn't expose it)."""
    if items is None:
        return []
    if not isinstance(items, list):
        _fail(f"{where} must be a list")
    if len(items) > MAX_MACHINE_VALIDATIONS:
        _fail(f"{where} exceeds max count {MAX_MACHINE_VALIDATIONS}")
    return [
        _validate_validation_record(item, f"{where}[{i}]", expected_source=MACHINE_VALIDATION_SOURCE)
        for i, item in enumerate(items)
    ]


def _validate_str_list(items, where: str, max_count: int, max_item_len: int) -> list:
    if items is None:
        return []
    if not isinstance(items, list):
        _fail(f"{where} must be a list")
    if len(items) > max_count:
        _fail(f"{where} exceeds max count {max_count}")
    out = []
    for i, item in enumerate(items):
        if not isinstance(item, str) or not item.strip():
            _fail(f"{where}[{i}] must be a non-empty string")
        if len(item) > max_item_len:
            _fail(f"{where}[{i}] exceeds max length {max_item_len}")
        out.append(item)
    return out


def _validate_workflow(workflow, where: str) -> dict:
    _check_keys(workflow, _WORKFLOW_KEYS, where)
    task_id = _require_str(workflow, "task_id", where, MAX_TASK_ID_LEN)
    phase = _require_str(workflow, "phase", where, 20)
    if phase not in VALID_PHASES:
        _fail(f"{where}.phase must be one of {sorted(VALID_PHASES)}, got {phase!r}")

    title = _optional_str(workflow, "title", where, MAX_TITLE_LEN)
    slice_id = _optional_str(workflow, "slice_id", where, MAX_SLICE_ID_LEN)
    summary = _optional_str(workflow, "summary", where, MAX_SUMMARY_LEN)
    next_steps = _validate_str_list(workflow.get("next_steps"), f"{where}.next_steps",
                                     MAX_NEXT_STEPS, MAX_NEXT_STEP_LEN)
    blockers = _validate_str_list(workflow.get("blockers"), f"{where}.blockers",
                                   MAX_BLOCKERS, MAX_BLOCKER_LEN)

    return {
        "task_id": task_id,
        "title": title,
        "phase": phase,
        "slice_id": slice_id,
        "summary": summary,
        "next_steps": next_steps,
        "blockers": blockers,
    }


def _require_bool(d: dict, key: str, where: str) -> bool:
    value = d.get(key)
    if not isinstance(value, bool):
        _fail(f"{where}.{key} must be a bool")
    return value


def _optional_hex(d: dict, key: str, where: str, lengths: set) -> Optional[str]:
    value = _optional_str(d, key, where, max(lengths))
    if value is not None and (
        len(value) not in lengths
        or any(char not in "0123456789abcdef" for char in value)
    ):
        _fail(
            f"{where}.{key} must be lowercase hex with length in "
            f"{sorted(lengths)}"
        )
    return value


def _validate_observation(observation, where: str) -> Optional[dict]:
    if observation is None:
        return None
    _check_keys(observation, _OBSERVATION_KEYS, where)
    target_key = _optional_hex(observation, "target_key", where, {64})
    if target_key is None:
        _fail(f"{where}.target_key is required")
    target_kind = _require_str(observation, "target_kind", where, 20)
    if target_kind not in {"git", "directory"}:
        _fail(f"{where}.target_kind must be 'git' or 'directory'")
    canonical_root = _require_str(observation, "canonical_root", where, 1024)
    head = _optional_hex(observation, "head", where, {40, 64})
    branch = _optional_str(observation, "branch", where, 512)
    detached = _require_bool(observation, "detached", where)
    unborn = _require_bool(observation, "unborn", where)

    operations = observation.get("operation_state")
    if not isinstance(operations, list):
        _fail(f"{where}.operation_state must be a list")
    if len(operations) > len(_VALID_OPERATION_STATES):
        _fail(f"{where}.operation_state has too many entries")
    if any(op not in _VALID_OPERATION_STATES for op in operations):
        _fail(f"{where}.operation_state contains an unsupported value")
    if operations != sorted(set(operations)):
        _fail(f"{where}.operation_state must be unique and sorted")

    status_digest = _optional_hex(observation, "status_digest", where, {64})
    status_complete = _require_bool(observation, "status_complete", where)
    changed_paths_truncated = _require_bool(
        observation, "changed_paths_truncated", where
    )
    upstream_ref = _optional_str(observation, "upstream_ref", where, 512)

    push_posture = observation.get("push_posture")
    if push_posture is not None:
        _check_keys(push_posture, _PUSH_POSTURE_KEYS, f"{where}.push_posture")
        push_default = _optional_str(
            push_posture, "push_default", f"{where}.push_posture", 64
        )
        sentinel = push_posture.get("configured_safety_sentinel")
        if not isinstance(sentinel, bool):
            _fail(
                f"{where}.push_posture.configured_safety_sentinel must be a bool"
            )
        push_posture = {
            "push_default": push_default,
            "configured_safety_sentinel": sentinel,
        }

    state_ref = _optional_hex(observation, "state_ref", where, {64})
    if state_ref is None:
        _fail(f"{where}.state_ref is required")
    binding_complete = _require_bool(observation, "binding_complete", where)

    if target_kind == "directory":
        if any(value is not None for value in (head, branch, status_digest, upstream_ref, push_posture)):
            _fail(f"{where} must not fabricate Git fields for a directory target")
        if detached or unborn or operations:
            _fail(f"{where} must not fabricate Git state for a directory target")
    else:
        if status_complete and status_digest is None:
            _fail(f"{where}.status_digest is required for complete Git status")
        if detached and branch is not None:
            _fail(f"{where}.branch must be null for detached HEAD")
        if unborn and (head is not None or detached or branch is None):
            _fail(f"{where} has inconsistent unborn-branch fields")

    return {
        "target_key": target_key,
        "target_kind": target_kind,
        "canonical_root": canonical_root,
        "head": head,
        "branch": branch,
        "detached": detached,
        "unborn": unborn,
        "operation_state": operations,
        "status_digest": status_digest,
        "status_complete": status_complete,
        "changed_paths_truncated": changed_paths_truncated,
        "upstream_ref": upstream_ref,
        "push_posture": push_posture,
        "state_ref": state_ref,
        "binding_complete": binding_complete,
    }


def _validate_checkpoint(checkpoint) -> dict:
    _check_keys(checkpoint, _CHECKPOINT_TOP_KEYS, "checkpoint")
    if "workflow" not in checkpoint:
        _fail("checkpoint.workflow is required")

    workflow = _validate_workflow(checkpoint["workflow"], "checkpoint.workflow")
    relevant_files = _validate_path_list(checkpoint.get("relevant_files"),
                                         "checkpoint.relevant_files", MAX_RELEVANT_FILES)
    changed_paths = _validate_changed_paths(
        checkpoint.get("changed_paths"), "checkpoint.changed_paths"
    )
    validations = _validate_validations(checkpoint.get("validations"), "checkpoint.validations")
    machine_validations = _validate_machine_validations(
        checkpoint.get("machine_validations"), "checkpoint.machine_validations"
    )
    observation = _validate_observation(
        checkpoint.get("observation"), "checkpoint.observation"
    )

    envelope = {
        "workflow": workflow,
        "relevant_files": relevant_files,
        "changed_paths": changed_paths,
        "validations": validations,
        "machine_validations": machine_validations,
        "observation": observation,
    }

    encoded = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_TOTAL_PAYLOAD_BYTES:
        raise CheckpointTooLarge(
            f"serialized checkpoint is {len(encoded)} bytes, "
            f"exceeds cap of {MAX_TOTAL_PAYLOAD_BYTES} bytes"
        )

    return envelope


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_record(row, project_name: str) -> CheckpointRecord:
    schema_version = row["schema_version"]
    if schema_version not in _KNOWN_SCHEMA_VERSIONS:
        raise UnsupportedSchema(
            f"checkpoint for project {project_name!r} has schema_version "
            f"{schema_version}, which this code does not understand"
        )

    try:
        envelope = json.loads(row["payload"])
    except json.JSONDecodeError as e:
        raise CheckpointCorrupt(
            f"checkpoint payload for project {project_name!r} is not valid JSON: {e}"
        )

    try:
        validated = _validate_checkpoint(envelope)
    except CheckpointValidationError as e:
        raise CheckpointCorrupt(
            f"checkpoint payload for project {project_name!r} does not match "
            f"schema_version {schema_version}'s expected shape: {e}"
        )

    return CheckpointRecord(
        project_name=project_name,
        target_key=row["target_key"],
        target_display=row["target_display"],
        target_kind=row["target_kind"],
        revision=row["revision"],
        schema_version=schema_version,
        workflow=validated["workflow"],
        relevant_files=validated["relevant_files"],
        changed_paths=validated["changed_paths"],
        validations=validated["validations"],
        machine_validations=validated["machine_validations"],
        observation=validated["observation"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ---------------------------------------------------------------------------
# Storage API
# ---------------------------------------------------------------------------

def save_checkpoint(project_name: str, target_root: str, checkpoint: dict,
                     *, expected_revision: int) -> CheckpointRecord:
    """Create or update the checkpoint for (project_name, target_root).

    expected_revision=0 means "create only if no row exists yet". For an
    existing row, expected_revision must exactly equal its current revision.
    A mismatch raises RevisionConflict and never touches the row -- no
    last-writer-wins. A successful write increments the revision by exactly
    one and returns the resulting CheckpointRecord.
    """
    project_name = validate_project_name(project_name)
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 0:
        raise CheckpointValidationError("expected_revision must be a non-negative int")
    identity = resolve_target_identity(target_root)
    return _save_checkpoint_for_identity(
        project_name, identity, checkpoint, expected_revision=expected_revision
    )


def _save_checkpoint_for_identity(
    project_name: str,
    identity: TargetIdentity,
    checkpoint: dict,
    *,
    expected_revision: int,
    refuse_unreadable_existing: bool = False,
) -> CheckpointRecord:
    """A2-only storage seam for an identity measured during stable capture."""
    project_name = validate_project_name(project_name)
    if not isinstance(identity, TargetIdentity):
        raise CheckpointValidationError("identity must be a measured TargetIdentity")
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 0:
        raise CheckpointValidationError("expected_revision must be a non-negative int")

    envelope = _validate_checkpoint(checkpoint)
    payload_json = json.dumps(envelope, sort_keys=True, separators=(",", ":"))

    init_checkpoint_db()
    from core.db import connect
    conn = connect()
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM coding_checkpoints WHERE project_name = ? AND target_key = ?",
            (project_name, identity.target_key),
        ).fetchone()
        if row is not None and refuse_unreadable_existing:
            # Model-facing/A2 saves must never erase forensic recovery data.
            # Validate under the same BEGIN IMMEDIATE transaction that will
            # perform CAS, so corruption cannot race an adapter pre-check.
            _row_to_record(row, project_name)
        current_revision = row["revision"] if row is not None else 0

        if current_revision != expected_revision:
            conn.execute("ROLLBACK")
            raise RevisionConflict(
                f"expected_revision={expected_revision} but current revision "
                f"for project {project_name!r} is {current_revision}"
            )

        new_revision = current_revision + 1
        now = _utcnow_iso()

        if row is None:
            conn.execute(
                """INSERT INTO coding_checkpoints
                   (project_name, target_key, target_display, target_kind,
                    schema_version, revision, payload, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (project_name, identity.target_key, identity.target_display, identity.kind,
                 SCHEMA_VERSION, new_revision, payload_json, now, now),
            )
            created_at = now
        else:
            conn.execute(
                """UPDATE coding_checkpoints
                   SET target_display = ?, target_kind = ?, schema_version = ?,
                       revision = ?, payload = ?, updated_at = ?
                   WHERE project_name = ? AND target_key = ?""",
                (identity.target_display, identity.kind, SCHEMA_VERSION,
                 new_revision, payload_json, now, project_name, identity.target_key),
            )
            created_at = conn.execute(
                "SELECT created_at FROM coding_checkpoints WHERE project_name = ? AND target_key = ?",
                (project_name, identity.target_key),
            ).fetchone()["created_at"]

        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()

    return CheckpointRecord(
        project_name=project_name,
        target_key=identity.target_key,
        target_display=identity.target_display,
        target_kind=identity.kind,
        revision=new_revision,
        schema_version=SCHEMA_VERSION,
        workflow=envelope["workflow"],
        relevant_files=envelope["relevant_files"],
        changed_paths=envelope["changed_paths"],
        validations=envelope["validations"],
        machine_validations=envelope["machine_validations"],
        observation=envelope["observation"],
        created_at=created_at,
        updated_at=now,
    )


def load_checkpoint(project_name: str, target_root: str) -> CheckpointRecord:
    """Raises CheckpointNotFound / CheckpointCorrupt / UnsupportedSchema --
    never returns None, so a caller can't mistake "no checkpoint" for a
    falsy-but-valid result."""
    project_name = validate_project_name(project_name)
    identity = resolve_target_identity(target_root)

    init_checkpoint_db()
    from core.db import connect
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM coding_checkpoints WHERE project_name = ? AND target_key = ?",
            (project_name, identity.target_key),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        raise CheckpointNotFound(
            f"no checkpoint for project {project_name!r} at target {identity.target_display!r}"
        )

    return _row_to_record(row, project_name)
