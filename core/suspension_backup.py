"""core/suspension_backup.py -- what a backup must leave alone
(AGENT-RUNAWAY-SUSPEND-01 C1, the backup exclusion interface).

The runaway-suspension state in a data folder (memory/suspension.db and its
SQLite sidecars, the memory/soul_identity.json pin and its atomic-write
leftovers, and the memory/suspension_evidence tree) is protected: an export
may neither copy it nor claim anything about it. This module is the one
place that says which files those are, so an export of ordinary history can
exclude them without knowing C1's internals.

It only looks. Every filesystem call is an lstat or a directory scan; it
never follows a symlink, never opens a file (the store least of all: opening
a WAL-mode SQLite database can create or checkpoint its sidecars), and never
creates, writes, renames or chmods anything. What it can't observe is
refused, never reported as absent.

This is exclusion only. It captures no snapshot, so it can never support a
complete backup, and it has no restore semantics: whatever a later restore
finds, it must still leave a trace and send the owner to Recover.

The one exception to "it only looks" is write_restore_trace()
(AGENT-BACKUP-RESTORE-02): a restore leaves C1 a durable, write-new-only
receipt in a freshly staged data root that has no C1 state at all, so the
restored root probes ABSENT_WITH_TRACE (owner Recover), never
ABSENT_NO_TRACE (first-run Initialize). It only adds a restriction: it never
creates or opens a store, never writes the identity pin, never copies
archived C1 state, and needs no owner envelope because it grants nothing.
"""
from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import core.suspension as _c1
from core.suspension import EVIDENCE_DIRNAME, IDENTITY_NAME, MEMORY_DIRNAME, STORE_NAME

SCHEMA = "lumina.c1_exclusion"
VERSION = 1
EVIDENCE_MAX_ENTRIES = 4096
EVIDENCE_MAX_DEPTH = 4

REFUSALS = frozenset({
    "DATA_ROOT_UNSAFE",              # missing, not a directory, or a symlink
    "MEMORY_ROOT_UNSAFE",            # memory/ is a symlink or not a directory
    "PROTECTED_ENTRY_UNOBSERVABLE",  # a protected name couldn't be lstat'd or scanned
    "PROTECTED_ENTRY_ALIASED",       # a protected regular file has another hard link
    "EVIDENCE_INVENTORY_UNBOUNDED",  # the evidence tree is too large or too deep
    "BOUNDARY_CHANGED",              # verify_exclusion_unchanged() saw a stable change
})
WARNINGS = frozenset({
    "protected_entry_not_regular",   # symlink/FIFO/device at a protected name: excluded, never followed
})

_PIN_TMP = re.compile(re.escape(IDENTITY_NAME) + r"\.[0-9a-f]+\.tmp")
_STABLE = frozenset({f"{MEMORY_DIRNAME}/{STORE_NAME}", f"{MEMORY_DIRNAME}/{IDENTITY_NAME}",
                     f"{MEMORY_DIRNAME}/{EVIDENCE_DIRNAME}"})


class ExclusionUnavailable(Exception):
    """The protected boundary can't be established; the export must not run."""

    def __init__(self, code: str, detail: str = ""):
        if code not in REFUSALS:
            raise ValueError(f"unknown exclusion refusal {code!r}")
        super().__init__(code + (f": {detail}" if detail else ""))
        self.code = code


@dataclass(frozen=True)
class ProtectedEntry:
    relpath: str   # data-root-relative, "/"-separated
    kind: str      # "file" | "dir" | "other"
    dev: int
    ino: int
    stable: bool   # identity-bound by verify_exclusion_unchanged()


@dataclass(frozen=True)
class C1Exclusion:
    data_root: tuple
    memory_dir: Optional[tuple]
    status: str    # "absent_unknown" | "present_excluded"
    entries: tuple
    warnings: tuple
    observed_at: str

    def denied_identities(self) -> frozenset:
        """(st_dev, st_ino) of every protected entry, for a capture deny list."""
        return frozenset((e.dev, e.ino) for e in self.entries)

    def manifest_record(self) -> dict:
        """What an export may say about C1: no paths, identities, counts,
        soul or incarnation, and nothing read from the store."""
        return {
            "schema": SCHEMA,
            "version": VERSION,
            "status": self.status,
            "observed_at": self.observed_at,
            "warnings": list(self.warnings),
            "snapshot_captured": False,
            "enforcement_compatibility": "unproven",
            "c1_schema_version": "unknown",
            "suspended_at_capture": "unknown",
            "restore_fence": "c1_trace_and_owner_recover_required",
        }


def _is_protected_leaf(name: str) -> bool:
    return (name == STORE_NAME or name.startswith(STORE_NAME + "-")
            or name == IDENTITY_NAME or _PIN_TMP.fullmatch(name) is not None
            or name == EVIDENCE_DIRNAME)


def is_protected_relpath(relpath: str) -> bool:
    """True for a data-root-relative path inside the protected family.
    Raises on anything that isn't a plain relative path, rather than guess."""
    if type(relpath) is not str:
        raise TypeError("relpath must be a str")
    parts = relpath.replace(os.sep, "/").split("/")
    if relpath.startswith(("/", os.sep)) or any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"not a normalized data-root-relative path: {relpath!r}")
    if len(parts) < 2 or parts[0] != MEMORY_DIRNAME:
        return False
    if parts[1] == EVIDENCE_DIRNAME:
        return True
    return len(parts) == 2 and _is_protected_leaf(parts[1])


def _lstat_protected(path: str):
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ExclusionUnavailable("PROTECTED_ENTRY_UNOBSERVABLE", type(exc).__name__) from None


def _scan(path: str) -> list:
    try:
        with os.scandir(path) as it:
            return sorted(entry.name for entry in it)
    except OSError as exc:
        raise ExclusionUnavailable("PROTECTED_ENTRY_UNOBSERVABLE", type(exc).__name__) from None


def _entry(relpath: str, st, warnings: set) -> ProtectedEntry:
    if stat.S_ISREG(st.st_mode):
        if st.st_nlink != 1:
            raise ExclusionUnavailable("PROTECTED_ENTRY_ALIASED", relpath)
        kind = "file"
    elif stat.S_ISDIR(st.st_mode):
        kind = "dir"
    else:
        kind = "other"
        warnings.add("protected_entry_not_regular")
    return ProtectedEntry(relpath, kind, st.st_dev, st.st_ino, relpath in _STABLE)


def _walk_evidence(root_path: str, root_rel: str, entries: list, warnings: set) -> None:
    count = 0
    pending = [(root_path, root_rel, 0)]
    while pending:
        path, rel, depth = pending.pop()
        for name in _scan(path):
            count += 1
            if count > EVIDENCE_MAX_ENTRIES or depth + 1 > EVIDENCE_MAX_DEPTH:
                raise ExclusionUnavailable("EVIDENCE_INVENTORY_UNBOUNDED")
            child, child_rel = os.path.join(path, name), f"{rel}/{name}"
            st = _lstat_protected(child)
            if st is None:
                continue  # removed since the scan; it can't be captured either
            entries.append(_entry(child_rel, st, warnings))
            if stat.S_ISDIR(st.st_mode):
                pending.append((child, child_rel, depth + 1))


def describe_exclusions(data_dir) -> C1Exclusion:
    """Observe the protected family under `data_dir` without touching it."""
    data_dir = os.fspath(data_dir)
    try:
        root = os.lstat(data_dir)
    except OSError:
        raise ExclusionUnavailable("DATA_ROOT_UNSAFE") from None
    if not stat.S_ISDIR(root.st_mode):
        raise ExclusionUnavailable("DATA_ROOT_UNSAFE")
    observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    memory_path = os.path.join(data_dir, MEMORY_DIRNAME)
    memory = _lstat_protected(memory_path)
    if memory is None:
        return C1Exclusion((root.st_dev, root.st_ino), None, "absent_unknown", (), (), observed_at)
    if not stat.S_ISDIR(memory.st_mode):
        raise ExclusionUnavailable("MEMORY_ROOT_UNSAFE")

    entries, warnings = [], set()
    for name in _scan(memory_path):
        if not _is_protected_leaf(name):
            continue
        path, rel = os.path.join(memory_path, name), f"{MEMORY_DIRNAME}/{name}"
        st = _lstat_protected(path)
        if st is None:
            continue
        entries.append(_entry(rel, st, warnings))
        if name == EVIDENCE_DIRNAME and stat.S_ISDIR(st.st_mode):
            _walk_evidence(path, rel, entries, warnings)
    entries.sort(key=lambda e: e.relpath)
    return C1Exclusion((root.st_dev, root.st_ino), (memory.st_dev, memory.st_ino),
                       "present_excluded" if entries else "absent_unknown",
                       tuple(entries), tuple(sorted(warnings)), observed_at)


def _stable_view(exclusion: C1Exclusion) -> dict:
    return {e.relpath: (e.kind, e.dev, e.ino) for e in exclusion.entries if e.stable}


def verify_exclusion_unchanged(data_dir, exclusion: C1Exclusion) -> None:
    """Re-observe just before an export is published. Raises BOUNDARY_CHANGED
    if the data root, memory/, the store, the pin or the evidence directory
    appeared, disappeared or changed identity. SQLite sidecars, pin-write
    leftovers and evidence children may come and go (a running Lumina opens
    and closes the store); they are still re-checked for aliases and bounds."""
    if type(exclusion) is not C1Exclusion:
        raise TypeError("exclusion must come from describe_exclusions()")
    now = describe_exclusions(data_dir)
    if (now.data_root != exclusion.data_root or now.memory_dir != exclusion.memory_dir
            or _stable_view(now) != _stable_view(exclusion)):
        raise ExclusionUnavailable("BOUNDARY_CHANGED")


RESTORE_TRACE_NAME = "restore_trace.json"
RESTORE_TRACE_SCHEMA = "lumina.c1_restore_trace"
_RESTORE_ID = re.compile(r"[0-9a-f]{32}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_RESTORE_CLASS = re.compile(r"[a-z][a-z0-9_]{0,47}")


class RestoreTraceRefused(Exception):
    """The staged root is not a C1-empty root; no trace was written."""


def write_restore_trace(data_dir, *, restore_id: str, archive_sha256: str, restore_class: str) -> str:
    """Leave C1 a restore trace in a staged data root with no C1 state.

    Refuses unless memory/ holds no store, sidecar, identity pin or evidence
    tree: a restore never writes over, merges with or vouches for existing C1
    state. The receipt sits in its own evidence directory, so a later
    owner-local recover_destructively() preserves it and starts the owner
    suspended with a fresh identity. Returns the receipt's data-root-relative
    path. The record names the archive but says nothing about archived C1.
    """
    if (type(restore_id) is not str or not _RESTORE_ID.fullmatch(restore_id)
            or type(archive_sha256) is not str or not _SHA256.fullmatch(archive_sha256)
            or type(restore_class) is not str or not _RESTORE_CLASS.fullmatch(restore_class)):
        raise ValueError("restore trace fields must be a restore id, a sha256 and a class label")
    paths = _c1._paths(data_dir)
    os.makedirs(paths.memory_dir, mode=0o700, exist_ok=True)
    try:
        _c1._memory_dir_identity(paths)
    except _c1.StoreFault as exc:
        raise RestoreTraceRefused(f"memory directory unsafe: {exc}") from None
    if _c1._traces(paths):
        raise RestoreTraceRefused("C1 state already exists in this root; a restore never adds to it")
    at = datetime.now(timezone.utc).timestamp()
    receipt_dir = _c1._new_evidence_dir(paths, at)
    _c1._write_new_private(os.path.join(receipt_dir, RESTORE_TRACE_NAME), _c1._canonical({
        "schema": RESTORE_TRACE_SCHEMA, "version": 1, "at": at, "restore_id": restore_id,
        "archive_sha256": archive_sha256, "restore_class": restore_class,
        "archived_c1_state": "not_restored", "required_next_step": "owner_local_recover"}))
    _c1._fsync_dir(receipt_dir)
    _c1._fsync_dir(paths.evidence)
    _c1._fsync_dir(paths.memory_dir)
    if _c1.probe(data_dir).status is not _c1.StoreStatus.ABSENT_WITH_TRACE:
        raise RestoreTraceRefused("the restore trace is not visible to C1")
    return os.path.relpath(os.path.join(receipt_dir, RESTORE_TRACE_NAME), paths.data_dir).replace(os.sep, "/")


def restore_trace_state(data_dir) -> str:
    """C1's own read-only probe status for a restored root ("absent_with_trace"
    until the owner recovers). Opens SQLite only if a store exists."""
    return _c1.probe(data_dir).status.value
