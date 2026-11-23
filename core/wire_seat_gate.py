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
(``wire_release.json``, written only by the owner-local release in Suspension
controls, W-B2: ``core.suspension_runtime.release_wire_seat``). The adapter
(``tools/aivengers_wire.py``) asks before any token read or socket. The
release record's format and the dialog's confirmation texts live here too,
so reader and writer share one status literal.

Rules (design: WB-PROPOSAL, amendment A1, erratum A1-E1):

* Anything about the evidence that cannot be checked refuses, with a stable
  reason code and a plain remedy. Unreadable never means "no trace".
* An invalid restore trace refuses (``TRACE_INVALID``).
* A second, independent restore marker (C1-S2 amendment A2-7): every restore
  also leaves ``restore_history/<restore_id>/`` (with its ``receipt.json``).
  A marked restore with no valid trace and no valid release refuses
  (``TRACE_MISSING``), so deleting the trace never unlocks the Wire. The
  marker is checked by lstat only: its receipt is never opened or parsed.
* An invalid release record is "not a release": it never allows and never
  blocks a later valid release. Pairing is by content only, the exact
  ``(restore_id, sha256(trace bytes))``; where a release sits does not matter.
* Only the two exact record names under ``suspension_evidence`` are ever
  opened; ``restore_history`` is listed and lstat'ed, never read. Nothing is
  cached.

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
    "SeatVerdict", "seat_verdict", "process_seat_verdict", "process_data_root", "describe",
    "release_record_bytes", "acknowledgement_sha256", "release_acknowledgement_sha256",
    "NO_TRACE", "RELEASED", "RECOVERED_UNRELEASED", "TRACE_INVALID", "TRACE_MISSING",
    "EVIDENCE_UNREADABLE", "OVER_CAP", "ROOT_UNKNOWN", "ROOT_MISMATCH", "GATE_ERROR",
    "SingleTrace", "single_trace", "is_restored_root",
]

DATA_DIR_ENV = "LUMINA_DATA_DIR"
MEMORY_DIRNAME = "memory"
EVIDENCE_DIRNAME = "suspension_evidence"
#: The restore engine's per-restore folder and its receipt's name (pinned
#: equal to the engine's by test). Its presence is the second restore
#: marker; nothing in it is ever opened here.
MARKER_DIRNAME = "restore_history"
MARKER_RECEIPT_NAME = "receipt.json"

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
TRACE_MISSING = "trace_missing"
EVIDENCE_UNREADABLE = "evidence_unreadable"
OVER_CAP = "over_cap"
ROOT_UNKNOWN = "root_unknown"
ROOT_MISMATCH = "root_mismatch"
GATE_ERROR = "gate_error"

#: Refusal precedence, strongest first (A1-E1 E1-2). GATE_ERROR replaces any
#: of them when the scan itself fails.
_PRECEDENCE = (ROOT_UNKNOWN, ROOT_MISMATCH, EVIDENCE_UNREADABLE, OVER_CAP, TRACE_INVALID,
               TRACE_MISSING)

_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_RESTORE_CLASS = re.compile(r"[a-z][a-z0-9_]{0,47}")

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC

# ── owner-facing texts (module constants so the wording lint covers them) ──

_DO_NOT_DELETE = ("Do not delete anything under memory/suspension_evidence or restore_history: "
                  "deleting restore records never unlocks the Wire and can lock it for good.")

STATUS_TEXT = {
    RECOVERED_UNRELEASED: "Wire locked: this is a restored copy.",
    TRACE_INVALID: "Wire locked: a restore record in this data folder can't be checked.",
    TRACE_MISSING: "Wire locked: this copy was restored, but its restore record is missing.",
    EVIDENCE_UNREADABLE: "Wire locked: Lumina can't read its safety evidence folder.",
    OVER_CAP: "Wire locked: the safety evidence folder is too large to check.",
    ROOT_UNKNOWN: "Wire locked: Lumina couldn't confirm which data folder it is using.",
    ROOT_MISMATCH: "Wire locked: Lumina couldn't confirm which data folder it is using.",
    GATE_ERROR: "Wire locked: the Wire seat check failed.",
}

REMEDY_TEXT = {
    # State-neutral (C1-S2 TA2-02): the gate can't see the C1 store, so it
    # names no control; Suspension controls shows the step for this state.
    RECOVERED_UNRELEASED: ("Open Suspension controls (/suspension) to see what this copy needs "
                           "before it can use the Wire."),
    TRACE_INVALID: ("Restore owner-only permissions (files 600, folders 700) under "
                    "memory/suspension_evidence, and remove any extra hard link to a restore "
                    "record, or restore again from the backup. " + _DO_NOT_DELETE),
    EVIDENCE_UNREADABLE: ("Make sure memory/ is a real folder, not a link, and restore "
                          "owner-only permissions on memory/suspension_evidence and "
                          "restore_history (folders 700, files 600). " + _DO_NOT_DELETE),
    TRACE_MISSING: ("Restore the backup again into a new folder. Nothing from the backup is lost; "
                    "changes you made in this copy are not carried over. " + _DO_NOT_DELETE),
    OVER_CAP: ("This needs manual review. Lumina will not remove evidence by itself. "
               + _DO_NOT_DELETE),
    ROOT_UNKNOWN: "Restart Lumina with its normal launcher.",
    ROOT_MISMATCH: "Restart Lumina with its normal launcher.",
    GATE_ERROR: "Restart Lumina. If it continues, report it.",
}

# ── W-B2 owner release: dialog texts. O-WB-3 (2026-11-19) fixed the button
# text and the four points the confirmation must state; the sentences below
# are the builder's, for review. ──
# The release record binds to the sha256 of the three texts the owner is
# shown (body, acknowledgement statement, checkbox caption). None of them
# may claim that the original installation was checked, stopped or retired.

RELEASE_BUTTON_TEXT = "Allow this copy to use the Wire seat"
RELEASE_BODY_TEXT = (
    "This copy of Lumina was restored from a backup. The original Lumina installation may "
    "still be running, and Lumina cannot check whether it has been stopped or retired. If you "
    "allow this copy, two copies could use the same Wire identity “lumina” and share one "
    "inbox: either one can read the other's messages and mark them as seen. Allowing this copy "
    "does not turn on Wire conversations or the Wire listener and does not connect to the "
    "Wire; you turn those on yourself in Settings.")
RELEASE_ACK_TEXT = (
    "I understand Lumina cannot confirm whether the original installation is still running, "
    "and I am responsible for keeping only one of them on the Wire.")
RELEASE_ACK_CAPTION = "I accept this uncertainty."
RELEASE_DONE_TEXT = (
    "This copy may now connect as 'lumina'. The original installation was not checked. Wire "
    "listener and Wire conversations stay off until you turn them on in Settings.")
RELEASED_STATUS_TEXT = (
    "This copy may use the Wire seat. The original installation was not checked.")


def acknowledgement_sha256(body: str, statement: str, caption: str) -> str:
    """sha256 of the exact confirmation texts, joined by newlines."""
    return hashlib.sha256("\n".join((body, statement, caption)).encode("utf-8")).hexdigest()


def release_acknowledgement_sha256() -> str:
    """The acknowledgement hash of the reviewed dialog wording."""
    return acknowledgement_sha256(RELEASE_BODY_TEXT, RELEASE_ACK_TEXT, RELEASE_ACK_CAPTION)


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
    #: W-B2: (restore_id, trace_sha256, archive_sha256) for each unreleased trace,
    #: in the same order as ``unreleased``.
    unreleased_lineage: tuple = ()


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
    """(restore_id, sha256, at, archive_sha256) of a valid trace, else None."""
    d = _parse(raw, TRACE_KEYS)
    if (d is None or d["schema"] != TRACE_SCHEMA or type(d["version"]) is not int
            or d["version"] != 1 or not _is_time(d["at"])
            or type(d["restore_id"]) is not str or not _HEX32.fullmatch(d["restore_id"])
            or type(d["archive_sha256"]) is not str or not _HEX64.fullmatch(d["archive_sha256"])
            or type(d["restore_class"]) is not str or not _RESTORE_CLASS.fullmatch(d["restore_class"])
            or d["archived_c1_state"] != "not_restored"
            or d["required_next_step"] != "owner_local_recover"):
        return None
    return d["restore_id"], hashlib.sha256(raw).hexdigest(), d["at"], d["archive_sha256"]


def release_record_bytes(*, at, restore_id: str, restore_trace_sha256: str,
                         acknowledgement_sha256: str, store_incarnation: str) -> bytes:
    """Canonical bytes of an owner release record (W-B2 writer).

    ``source_installation_status`` is this module's single literal: no
    parameter feeds it, so no caller can record that the original
    installation was verified, stopped or retired. Raises ValueError unless
    the result is a valid release by this module's own reader."""
    raw = _canonical({
        "schema": RELEASE_SCHEMA, "version": 1, "at": at, "restore_id": restore_id,
        "restore_trace_sha256": restore_trace_sha256,
        "source_installation_status": SOURCE_STATUS_UNVERIFIED,
        "acknowledgement_sha256": acknowledgement_sha256,
        "store_incarnation": store_incarnation, "ingress": RELEASE_INGRESS})
    if len(raw) > MAX_RECORD_BYTES or _release_pair(raw) != (restore_id, restore_trace_sha256):
        raise ValueError("not a valid wire seat release record")
    return raw


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


def _scan_evidence(ev_fd: int, c: dict) -> None:
    """Valid traces and releases under ``suspension_evidence``; only the two
    exact record names are ever opened."""
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
            c["refusals"].add(EVIDENCE_UNREADABLE)
            continue
        if stat.S_ISLNK(cst.st_mode):
            # A linked child could hide a trace; it is never followed.
            c["refusals"].add(EVIDENCE_UNREADABLE)
            continue
        if not stat.S_ISDIR(cst.st_mode):
            continue  # C1 writes only directories here; a stray file holds no record
        try:
            child_fd = _open_dir(name, dir_fd=ev_fd)
        except _Refuse as exc:
            c["refusals"].add(exc.reason)
            continue
        try:
            raw = _read_record(child_fd, TRACE_NAME)
            if raw is not _ABSENT:
                ident = _trace_identity(raw) if raw is not None else None
                if ident is None:
                    c["refusals"].add(TRACE_INVALID)
                else:
                    key = (ident[0], ident[1])
                    c["traces"][key] = ident[2]
                    c["archives"][key] = ident[3]
                    c["where"].setdefault(key, []).append(name)
            raw = _read_record(child_fd, RELEASE_NAME)
            if raw is not _ABSENT:
                pair = _release_pair(raw) if raw is not None else None
                if pair is None:
                    c["ignored"] += 1
                else:
                    c["releases"].add(pair)
        finally:
            os.close(child_fd)


def _scan_markers(data_root: str, c: dict) -> None:
    """The second restore marker (A2-7): each private ``restore_history/<hex32>``
    folder names one restore. Listed and lstat'ed only: ``receipt.json`` is
    never opened or parsed. A folder without its receipt still marks the
    restore; anything that is not a plain private marker refuses."""
    path = os.path.join(data_root, MARKER_DIRNAME)
    if not _exists_nofollow(path):
        return
    hist_fd = _open_dir(path)
    try:
        try:
            names = os.listdir(hist_fd)
        except OSError:
            raise _Refuse(EVIDENCE_UNREADABLE) from None
        if len(names) > MAX_EVIDENCE_CHILDREN:
            raise _Refuse(OVER_CAP)
        for name in sorted(names):
            if not _HEX32.fullmatch(name):
                c["refusals"].add(EVIDENCE_UNREADABLE)
                continue
            try:
                child_fd = _open_dir(name, dir_fd=hist_fd)
            except _Refuse as exc:
                c["refusals"].add(exc.reason)
                continue
            c["markers"].add(name)
            try:
                try:
                    rst = os.stat(MARKER_RECEIPT_NAME, dir_fd=child_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                except OSError:
                    c["refusals"].add(EVIDENCE_UNREADABLE)
                    continue
                if (not stat.S_ISREG(rst.st_mode) or rst.st_nlink != 1
                        or rst.st_uid != os.getuid() or rst.st_mode & 0o077):
                    c["refusals"].add(EVIDENCE_UNREADABLE)
            finally:
                os.close(child_fd)
    finally:
        os.close(hist_fd)


def _collect(data_root: str) -> dict:
    c = {"refusals": set(), "traces": {}, "archives": {}, "where": {}, "releases": set(),
         "markers": set(), "ignored": 0}
    memory = os.path.join(data_root, MEMORY_DIRNAME)
    if _exists_nofollow(memory):
        mem_fd = _open_dir(memory, private=False)
        ev_fd = None
        try:
            try:
                os.stat(EVIDENCE_DIRNAME, dir_fd=mem_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            except OSError:
                raise _Refuse(EVIDENCE_UNREADABLE) from None
            else:
                ev_fd = _open_dir(EVIDENCE_DIRNAME, dir_fd=mem_fd)
        finally:
            os.close(mem_fd)
        if ev_fd is not None:
            try:
                _scan_evidence(ev_fd, c)
            finally:
                os.close(ev_fd)
    # The marker lives outside memory/, so it is checked even when memory/ or
    # its evidence folder is gone: that absence is exactly what it catches.
    _scan_markers(data_root, c)
    return c


def _verdict(c: dict) -> SeatVerdict:
    traces, releases, refusals = c["traces"], c["releases"], set(c["refusals"])
    unreleased = tuple(sorted((rid, sha, at) for (rid, sha), at in traces.items()
                              if (rid, sha) not in releases))
    lineage = tuple((rid, sha, c["archives"][(rid, sha)]) for rid, sha, _at in unreleased)
    accounted = {rid for rid, _sha in traces} | {rid for rid, _sha in releases}
    if c["markers"] - accounted:
        refusals.add(TRACE_MISSING)  # a marked restore whose trace is gone, never released
    for reason in _PRECEDENCE:
        if reason in refusals:
            return SeatVerdict(False, reason, unreleased, c["ignored"], unreleased_lineage=lineage)
    if not traces and not c["markers"]:
        return SeatVerdict(True, NO_TRACE, (), c["ignored"])
    if unreleased:
        return SeatVerdict(False, RECOVERED_UNRELEASED, unreleased, c["ignored"],
                           unreleased_lineage=lineage)
    return SeatVerdict(True, RELEASED, (), c["ignored"])


def _scan(data_root: str) -> SeatVerdict:
    return _verdict(_collect(data_root))


@dataclass(frozen=True)
class SingleTrace:
    """The one restore trace of a root awaiting recovery, read once (C1-S2
    TC2-07b): its identity and sha256 come from the same bytes."""
    restore_id: str
    trace_sha256: str
    archive_sha256: str
    at: float
    relpath: str  # data-root-relative, as the restore receipt records it


def single_trace(data_root):
    """The single valid, unreleased restore trace of a root that the gate
    reports as RECOVERED_UNRELEASED, else None. Any refusal, a second trace,
    or two folders holding the same trace bytes gives None. Never raises."""
    try:
        root = os.fspath(data_root)
        c = _collect(root)
        verdict = _verdict(c)
    except Exception:
        return None
    if verdict.reason != RECOVERED_UNRELEASED or len(c["traces"]) != 1 or len(verdict.unreleased) != 1:
        return None
    (key, at), = c["traces"].items()
    where = c["where"].get(key, [])
    if len(where) != 1:
        return None
    return SingleTrace(key[0], key[1], c["archives"][key], at,
                       f"{MEMORY_DIRNAME}/{EVIDENCE_DIRNAME}/{where[0]}/{TRACE_NAME}")


def is_restored_root(data_root) -> bool:
    """True unless the gate finds no restore evidence at all (NO_TRACE). A
    released, unreleased, missing-trace, invalid, unreadable or failed scan
    all count as restored: unknown fails closed (C1-S2 A2-4)."""
    return seat_verdict(data_root).reason != NO_TRACE


def seat_verdict(data_root) -> SeatVerdict:
    """Read-only verdict for one data root. Never raises: any failure refuses."""
    try:
        return _scan(os.fspath(data_root))
    except _Refuse as exc:
        return SeatVerdict(False, exc.reason)
    except Exception as exc:  # fail closed, and name the class only
        return SeatVerdict(False, GATE_ERROR, error_class=type(exc).__name__)


def process_data_root():
    """(realpath of this process's data root, None), or (None, reason code).
    W-B2's writer uses it to check it writes where the gate reads."""
    return _process_data_root()


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
