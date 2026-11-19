"""Wire seat quarantine for restored data roots (W-B1).

A data root that Agent Restore created carries a restore trace,
``memory/suspension_evidence/<stamp>/restore_trace.json``, written by every
restore before it publishes (``core.suspension_backup.write_restore_trace``).
Such a root is a *copy* of an installation whose original may still be
running and holding the Wire seat ``lumina``. Two holders of one seat share
one inbox cursor, so either can consume the other's messages.

This module answers one question, read-only: may the process's data root use
the Wire seat? It allows only when the root has no restore trace, or when
every valid restore trace has a valid, matching owner release record
(``wire_release.json``, written by a later owner-local release slice). The
adapter (``tools/aivengers_wire.py``) asks before any token read or socket.

Rules (design: WB-PROPOSAL, amendment A1, erratum A1-E1):

* Anything about the evidence that cannot be checked refuses, with a stable
  reason code and a plain remedy. Unreadable never means "no trace".
* An invalid restore trace refuses (``TRACE_INVALID``).
* An invalid release record is "not a release": it never allows and never
  blocks a later valid release. Pairing is by content only, the exact
  ``(restore_id, sha256(trace bytes))``; where a release sits does not matter.
* Only the two exact record names are ever opened. Nothing is cached.

It writes nothing, follows no symlink, imports only the standard library,
and never imports ``core.suspension``. The record formats it shares with the
C1-side writers are pinned by tests.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import sys
from dataclasses import dataclass

__all__ = [
    "SeatVerdict", "seat_verdict", "process_seat_verdict", "describe",
    "NO_TRACE", "RELEASED", "RECOVERED_UNRELEASED", "TRACE_INVALID",
    "EVIDENCE_UNREADABLE", "OVER_CAP", "ROOT_UNKNOWN", "ROOT_MISMATCH", "GATE_ERROR",
]

DATA_DIR_ENV = "LUMINA_DATA_DIR"
MEMORY_DIRNAME = "memory"
EVIDENCE_DIRNAME = "suspension_evidence"

TRACE_NAME = "restore_trace.json"
TRACE_SCHEMA = "lumina.c1_restore_trace"
TRACE_KEYS = frozenset({"schema", "version", "at", "restore_id", "archive_sha256",
                        "restore_class", "archived_c1_state", "required_next_step"})

RELEASE_NAME = "wire_release.json"
RELEASE_SCHEMA = "lumina.wire_seat_release"
RELEASE_KEYS = frozenset({"schema", "version", "at", "restore_id", "restore_trace_sha256",
                          "source_installation_status", "acknowledgement_sha256",
                          "store_incarnation", "ingress"})
#: The only source-installation status a release may carry. A value meaning
#: "verified retired" is not defined: nothing can verify it today.
SOURCE_STATUS_UNVERIFIED = "unverified_owner_acknowledged"
RELEASE_INGRESS = "local_suspension_control:wire_seat_release"

MAX_EVIDENCE_CHILDREN = 1024
MAX_RECORD_BYTES = 4096

# Reason codes (stable; tests and texts key on them).
NO_TRACE = "no_trace"
RELEASED = "released"
RECOVERED_UNRELEASED = "recovered_unreleased"
TRACE_INVALID = "trace_invalid"
EVIDENCE_UNREADABLE = "evidence_unreadable"
OVER_CAP = "over_cap"
ROOT_UNKNOWN = "root_unknown"
ROOT_MISMATCH = "root_mismatch"
GATE_ERROR = "gate_error"

#: Refusal precedence, strongest first (A1-E1 E1-2). GATE_ERROR replaces any
#: of them when the scan itself fails.
_PRECEDENCE = (ROOT_UNKNOWN, ROOT_MISMATCH, EVIDENCE_UNREADABLE, OVER_CAP, TRACE_INVALID)

_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_RESTORE_CLASS = re.compile(r"[a-z][a-z0-9_]{0,47}")

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC

# ── owner-facing texts (module constants so the wording lint covers them) ──

_DO_NOT_DELETE = "Do not delete the restore records to unlock the Wire; that bypasses this check."

STATUS_TEXT = {
    RECOVERED_UNRELEASED: "Wire locked: this is a restored copy.",
    TRACE_INVALID: "Wire locked: a restore record in this data folder can't be checked.",
    EVIDENCE_UNREADABLE: "Wire locked: Lumina can't read its safety evidence folder.",
    OVER_CAP: "Wire locked: the safety evidence folder is too large to check.",
    ROOT_UNKNOWN: "Wire locked: Lumina couldn't confirm which data folder it is using.",
    ROOT_MISMATCH: "Wire locked: Lumina couldn't confirm which data folder it is using.",
    GATE_ERROR: "Wire locked: the Wire seat check failed.",
}

REMEDY_TEXT = {
    # W-B1 build: no in-product release exists yet. W-B2 replaces this text.
    RECOVERED_UNRELEASED: "This version of Lumina cannot allow it yet.",
    TRACE_INVALID: ("Restore owner-only permissions (files 600, folders 700) under "
                    "memory/suspension_evidence, and remove any extra hard link to a restore "
                    "record, or restore again from the backup. " + _DO_NOT_DELETE),
    EVIDENCE_UNREADABLE: ("Make sure memory/ is a real folder, not a link, and restore "
                          "owner-only permissions on memory/suspension_evidence "
                          "(folders 700, files 600). " + _DO_NOT_DELETE),
    OVER_CAP: ("This needs manual review. Lumina will not remove evidence by itself. "
               + _DO_NOT_DELETE),
    ROOT_UNKNOWN: "Restart Lumina with its normal launcher.",
    ROOT_MISMATCH: "Restart Lumina with its normal launcher.",
    GATE_ERROR: "Restart Lumina. If it continues, report it.",
}


@dataclass(frozen=True)
class SeatVerdict:
    allowed: bool
    reason: str
    #: Valid traces with no valid matching release: (restore_id, trace_sha256, at).
    unreleased: tuple = ()
    #: Release-named records that were not valid releases (diagnostics only).
    ignored_releases: int = 0
    #: Exception class name for GATE_ERROR (never its message).
    error_class: str | None = None


def describe(verdict: SeatVerdict) -> str:
    """Plain owner-facing text for a refusal: status, then remedy."""
    if verdict.allowed:
        return ""
    status = STATUS_TEXT.get(verdict.reason, STATUS_TEXT[GATE_ERROR])
    remedy = REMEDY_TEXT.get(verdict.reason, REMEDY_TEXT[GATE_ERROR])
    return f"{status} {remedy}"


def _canonical(value: dict) -> bytes:
    # Byte-for-byte the encoding core.suspension._canonical uses (pinned by test).
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


class _Refuse(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _is_time(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _parse(raw: bytes, keys: frozenset):
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if type(data) is not dict or set(data) != keys or _canonical(data) != raw:
        return None
    return data


def _trace_identity(raw: bytes):
    """(restore_id, sha256) of a valid trace, else None."""
    d = _parse(raw, TRACE_KEYS)
    if (d is None or d["schema"] != TRACE_SCHEMA or type(d["version"]) is not int
            or d["version"] != 1 or not _is_time(d["at"])
            or type(d["restore_id"]) is not str or not _HEX32.fullmatch(d["restore_id"])
            or type(d["archive_sha256"]) is not str or not _HEX64.fullmatch(d["archive_sha256"])
            or type(d["restore_class"]) is not str or not _RESTORE_CLASS.fullmatch(d["restore_class"])
            or d["archived_c1_state"] != "not_restored"
            or d["required_next_step"] != "owner_local_recover"):
        return None
    return d["restore_id"], hashlib.sha256(raw).hexdigest(), d["at"]


def _release_pair(raw: bytes):
    """(restore_id, restore_trace_sha256) of a valid release, else None."""
    d = _parse(raw, RELEASE_KEYS)
    if (d is None or d["schema"] != RELEASE_SCHEMA or type(d["version"]) is not int
            or d["version"] != 1 or not _is_time(d["at"])
            or type(d["restore_id"]) is not str or not _HEX32.fullmatch(d["restore_id"])
            or type(d["restore_trace_sha256"]) is not str
            or not _HEX64.fullmatch(d["restore_trace_sha256"])
            or d["source_installation_status"] != SOURCE_STATUS_UNVERIFIED
            or type(d["acknowledgement_sha256"]) is not str
            or not _HEX64.fullmatch(d["acknowledgement_sha256"])
            or type(d["store_incarnation"]) is not str
            or not _HEX32.fullmatch(d["store_incarnation"])
            or d["ingress"] != RELEASE_INGRESS):
        return None
    return d["restore_id"], d["restore_trace_sha256"]


_ABSENT = object()


def _read_record(dir_fd: int, name: str):
    """Bytes of a private, regular, single-link record; _ABSENT if there is
    no entry; None if an entry exists but is not such a record."""
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return _ABSENT
    except OSError:
        return None
    if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.getuid()
            or st.st_mode & 0o077 or st.st_size > MAX_RECORD_BYTES):
        return None
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        fst = os.fstat(fd)
        if (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino) or not stat.S_ISREG(fst.st_mode):
            return None
        raw = os.read(fd, MAX_RECORD_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(raw) > MAX_RECORD_BYTES:
        return None
    return raw


def _private_dir(st) -> bool:
    return (stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid()
            and not st.st_mode & 0o022)


def _open_dir(path: str, *, dir_fd: int | None = None, private: bool = True) -> int:
    """Open a directory without following a link; refuse if it is not the
    directory lstat saw (and, for evidence directories, not private).
    ``memory/`` itself is only required to be a real directory: C1 guards
    its permissions, and a native root must not lose the Wire over them."""
    try:
        st = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        raise _Refuse(EVIDENCE_UNREADABLE) from None
    if not stat.S_ISDIR(st.st_mode) or (private and not _private_dir(st)):
        raise _Refuse(EVIDENCE_UNREADABLE)
    try:
        fd = os.open(path, _DIR_FLAGS, dir_fd=dir_fd)
    except OSError:
        raise _Refuse(EVIDENCE_UNREADABLE) from None
    fst = os.fstat(fd)
    if (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
        os.close(fd)
        raise _Refuse(EVIDENCE_UNREADABLE)
    return fd


def _exists_nofollow(path: str) -> bool:
    try:
        os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        raise _Refuse(EVIDENCE_UNREADABLE) from None
    return True


def _scan(data_root: str) -> SeatVerdict:
    memory = os.path.join(data_root, MEMORY_DIRNAME)
    if not _exists_nofollow(memory):
        return SeatVerdict(True, NO_TRACE)
    mem_fd = _open_dir(memory, private=False)
    try:
        try:
            os.stat(EVIDENCE_DIRNAME, dir_fd=mem_fd, follow_symlinks=False)
        except FileNotFoundError:
            return SeatVerdict(True, NO_TRACE)
        except OSError:
            raise _Refuse(EVIDENCE_UNREADABLE) from None
        ev_fd = _open_dir(EVIDENCE_DIRNAME, dir_fd=mem_fd)
    finally:
        os.close(mem_fd)
    refusals: set[str] = set()
    traces: dict[tuple, float] = {}
    releases: set[tuple] = set()
    ignored = 0
    try:
        try:
            names = os.listdir(ev_fd)
        except OSError:
            raise _Refuse(EVIDENCE_UNREADABLE) from None
        if len(names) > MAX_EVIDENCE_CHILDREN:
            raise _Refuse(OVER_CAP)
        for name in sorted(names):
            try:
                cst = os.stat(name, dir_fd=ev_fd, follow_symlinks=False)
            except OSError:
                refusals.add(EVIDENCE_UNREADABLE)
                continue
            if stat.S_ISLNK(cst.st_mode):
                # A linked child could hide a trace; it is never followed.
                refusals.add(EVIDENCE_UNREADABLE)
                continue
            if not stat.S_ISDIR(cst.st_mode):
                continue  # C1 writes only directories here; a stray file holds no record
            try:
                child_fd = _open_dir(name, dir_fd=ev_fd)
            except _Refuse as exc:
                refusals.add(exc.reason)
                continue
            try:
                raw = _read_record(child_fd, TRACE_NAME)
                if raw is not _ABSENT:
                    ident = _trace_identity(raw) if raw is not None else None
                    if ident is None:
                        refusals.add(TRACE_INVALID)
                    else:
                        traces[(ident[0], ident[1])] = ident[2]
                raw = _read_record(child_fd, RELEASE_NAME)
                if raw is not _ABSENT:
                    pair = _release_pair(raw) if raw is not None else None
                    if pair is None:
                        ignored += 1
                    else:
                        releases.add(pair)
            finally:
                os.close(child_fd)
    finally:
        os.close(ev_fd)
    unreleased = tuple(sorted((rid, sha, at) for (rid, sha), at in traces.items()
                              if (rid, sha) not in releases))
    for reason in _PRECEDENCE:
        if reason in refusals:
            return SeatVerdict(False, reason, unreleased, ignored)
    if not traces:
        return SeatVerdict(True, NO_TRACE, (), ignored)
    if unreleased:
        return SeatVerdict(False, RECOVERED_UNRELEASED, unreleased, ignored)
    return SeatVerdict(True, RELEASED, (), ignored)


def seat_verdict(data_root) -> SeatVerdict:
    """Read-only verdict for one data root. Never raises: any failure refuses."""
    try:
        return _scan(os.fspath(data_root))
    except _Refuse as exc:
        return SeatVerdict(False, exc.reason)
    except Exception as exc:  # fail closed, and name the class only
        return SeatVerdict(False, GATE_ERROR, error_class=type(exc).__name__)


def _process_data_root():
    raw = os.environ.get(DATA_DIR_ENV)
    if not raw or not os.path.isabs(raw):
        return None, ROOT_UNKNOWN
    root = os.path.realpath(raw)
    cfg = sys.modules.get("config")
    if cfg is not None:
        configured = getattr(cfg, "DATA_DIR", None)
        if not isinstance(configured, str) or os.path.realpath(configured) != root:
            return None, ROOT_MISMATCH
    return root, None


def process_seat_verdict() -> SeatVerdict:
    """Verdict for this process's data root (``LUMINA_DATA_DIR``, which
    ``data_directory`` exports before ``config.DATA_DIR`` is bound; both must
    agree). Never raises."""
    try:
        root, reason = _process_data_root()
    except Exception as exc:
        return SeatVerdict(False, GATE_ERROR, error_class=type(exc).__name__)
    if reason is not None:
        return SeatVerdict(False, reason)
    return seat_verdict(root)
