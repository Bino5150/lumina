"""core/suspension.py -- AGENT-RUNAWAY-SUSPEND-01 (Gatling Slice C) runaway-suspension core.

Slice C1a: this module is INERT. Nothing in Lumina imports it yet; it makes
no file, starts no thread and changes no behavior until Slice C1b wires it
into admission, capture and the desktop recovery dialog. A test pins that.

What it is
----------
A durable, cross-process latch that is separate from, and never touches, the
process-local Emergency Interlock (core/emergency_stop.py) and the Telegram
remote-access switch (comms/telegram_access.py). One SQLite store at
DATA_DIR/memory/suspension.db (mode 600, WAL), next to a data-root identity
pin at DATA_DIR/memory/soul_identity.json.

State model (design R2 §2):
  store_incarnation  random 128-bit id; changes only on destructive recovery.
  scope              "owner" or "channel:<transport>:<canonical id>".
  execution_epoch    per scope; +1 on every clear->suspended transition.
                     Never changed by a resume, never reset in an incarnation.
  state_revision     per scope; +1 on every committed transition.
  ExecutionToken     (store_incarnation, scope, execution_epoch). Minted only
                     by SuspensionStore.capture(); immutable; there is no
                     refresh API. Work captured at epoch e is permanently
                     stale once a trip moves the scope to e+1.

Admission (admit/verify/capture) needs all of: a healthy, identity-verified
store; a clear scope; an exact token match; and (admit) a registered,
compatible runtime. Each is one short BEGIN IMMEDIATE transaction; no
transaction is ever held across network or tool execution. The receiving
runtime's E-stop is a SEPARATE gate the caller checks too; this module never
reads or clears it.

trip() and resume() are compare-and-swap transactions. resume() consumes its
recovery event exactly once in the same transaction and stores the durable
outcome, so a retried or replayed event returns the stored outcome and never
makes a second transition. A recovery event is bound to the exact incident
the owner was shown (soul, incarnation, scope, epoch, revision, suspension
id); a desktop event is fresh only in the process and boot that minted it,
for a bounded monotonic age -- never by wall-clock ordering. Resume is refused while stale-epoch leases are in
flight; leases of dead runtimes are "orphaned, outcome unknown" and need an
explicit owner acknowledgement. A timeout never restores authority.

Initialization is not recovery
------------------------------
Nothing here ever creates a store implicitly: open_store() opens an existing
store or raises. initialize() runs only on an explicit local owner envelope,
and only when probe() finds no trace at all. probe() reporting
ABSENT_NO_TRACE is NOT proof of a first run -- a prior store could have been
deleted with all of its traces -- so the caller (the desktop, Slice C1b) must
put the decision to the owner. Recovery (repair_identity, recover_destructively)
is likewise local-owner only, preserves byte-identical evidence before it
changes anything, and destructive recovery rotates the incarnation so every
old token is rejected.

Not guaranteed (design R2 §1): interrupting work admitted before a trip;
control over runtimes that do not run this code or that are bound to a
different data root; resistance to arbitrary same-UID code.

Linux/POSIX only (liveness reads /proc). Pure module: no Qt, no Telegram, no
config import -- every entry point takes data_dir explicitly.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import socket
import sqlite3
import stat
import threading
import time
import urllib.parse
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from core import operator_ingress
from core.test_isolation import refuse_if_production_path


SCHEMA_VERSION = 1
ENFORCEMENT_VERSION = 1

MEMORY_DIRNAME = "memory"
STORE_NAME = "suspension.db"
IDENTITY_NAME = "soul_identity.json"
EVIDENCE_DIRNAME = "suspension_evidence"
_STORE_FILES = (STORE_NAME, STORE_NAME + "-wal", STORE_NAME + "-shm", STORE_NAME + "-journal")
_TRACE_NAMES = _STORE_FILES + (IDENTITY_NAME, EVIDENCE_DIRNAME)
_IDENTITY_SCHEMA = "lumina.soul_identity"

BUSY_TIMEOUT_S = 5.0
# A runtime on another host can't be probed through /proc; a heartbeat older
# than this makes its in-flight leases "orphaned, outcome unknown".
HEARTBEAT_STALE_S = 90.0
# A desktop recovery event is authority minted by one owner action in one
# process. It is fresh only in that process and boot, and only this long on
# the monotonic clock. The wall clock is never consulted for it.
DESKTOP_EVENT_MAX_AGE_S = 300.0

OWNER_SCOPE = "owner"
_SCOPE_RE = re.compile(r"owner|channel:[a-z][a-z0-9_]{0,15}:[A-Za-z0-9_.@:-]{1,128}")
_HEX32 = re.compile(r"[0-9a-f]{32}")

SELECTOR_LENGTH = 8
SELECTOR_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32

_EVIDENCE_KEY = re.compile(r"[a-z][a-z0-9_]{0,47}")
_EVIDENCE_STR = re.compile(r"[A-Za-z0-9_.:@/-]{0,96}")
_EVIDENCE_MAX_ITEMS = 16

_OPEN_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

class StoreStatus(Enum):
    HEALTHY = "healthy"
    ABSENT_NO_TRACE = "absent_no_trace"
    ABSENT_WITH_TRACE = "absent_with_trace"
    FAULT = "fault"


class Fault(Enum):
    MISSING = "missing"
    IDENTITY_MISSING = "identity_missing"
    IDENTITY_INVALID = "identity_invalid"
    SOUL_MISMATCH = "soul_mismatch"
    CORRUPT = "corrupt"
    SCHEMA = "incompatible_schema"
    INSECURE = "insecure_permissions"
    REPLACED = "replaced_under_handle"
    INCARNATION_CHANGED = "incarnation_changed"
    ENFORCEMENT_TOO_OLD = "enforcement_too_old"
    CLOSED = "closed"


class Refusal(Enum):
    SUSPENDED = "suspended"
    STALE_TOKEN = "stale_token"
    FOREIGN_INCARNATION = "foreign_incarnation"
    INVALID_TOKEN = "invalid_token"
    MISSING_TOKEN = "missing_token"
    STORE_FAULT = "store_fault"
    STORE_BUSY = "store_busy"
    LOCAL_STOP = "local_stop"
    UNREGISTERED_RUNTIME = "unregistered_runtime"


class Trigger(Enum):
    """What may latch a scope (design R2 D1: budget exhaustion alone does not)."""
    BREAKER_TRIPPED = "breaker_tripped"
    SECOND_BATCH_REFUSED = "second_batch_refused"


# Incidents re-created by destructive recovery. Not a trip() trigger.
RECOVERY_TRIGGER = "recovery_unknown_state"


class LeaseKind(Enum):
    TOOL = "tool"
    PROVIDER = "provider"
    WRITE = "write"
    BACKGROUND = "background"


class TripResult(Enum):
    TRIPPED = "tripped"
    ALREADY_SUSPENDED = "already_suspended"
    LATE_EVIDENCE = "late_evidence"
    FOREIGN_INCARNATION = "foreign_incarnation"
    INVALID_TOKEN = "invalid_token"
    GLOBAL_UNCONFIRMED = "global_suspension_unconfirmed"


class ResumeResult(Enum):
    RESUMED = "resumed"
    NO_SUCH_INCIDENT = "no_such_incident"
    NOT_ACTIVE = "incident_not_active"
    MISMATCH = "expectation_mismatch"
    EVENT_BEFORE_INCIDENT = "event_before_incident"
    EVENT_BINDING_MISMATCH = "event_not_bound_to_this_incident"
    STALE_AUTHORITY = "stale_local_authority"
    DRAIN_PENDING = "drain_pending"
    UNKNOWN_NEEDS_ACK = "unknown_outcome_needs_acknowledgement"


class SuspensionError(Exception):
    """Base class for everything this module raises."""


class StoreFault(SuspensionError):
    """The store is missing, corrupt, foreign, insecure or replaced. Every
    scope refuses admission until the owner resolves it."""

    def __init__(self, fault: Fault, detail: str = ""):
        super().__init__(f"{fault.value}: {detail}" if detail else fault.value)
        self.fault = fault
        self.detail = detail


class StoreUnavailable(SuspensionError):
    """Transient: the store was busy or locked. Nothing changed."""


class AdmissionRefused(SuspensionError):
    def __init__(self, refusal: Refusal, detail: str = "", fault: Optional[Fault] = None):
        super().__init__(f"{refusal.value}: {detail}" if detail else refusal.value)
        self.refusal = refusal
        self.detail = detail
        self.fault = fault


class NotAuthorized(SuspensionError):
    """A local-owner operation was attempted without a genuine local desktop
    suspension-control envelope."""


class RecoveryBlocked(SuspensionError):
    def __init__(self, reason: str, runtimes: tuple = ()):
        super().__init__(reason)
        self.reason = reason
        self.runtimes = runtimes


# ---------------------------------------------------------------------------
# Tokens and their inheritance
# ---------------------------------------------------------------------------

_MINT = object()


@dataclass(frozen=True)
class ExecutionToken:
    """Minted only by SuspensionStore.capture(). Immutable; carried unchanged
    through every handoff. Equality ignores the mint."""
    store_incarnation: str
    scope: str
    execution_epoch: int
    _mint: object = field(repr=False, compare=False)

    def __post_init__(self):
        if self._mint is not _MINT:
            raise TypeError("ExecutionToken is minted only by SuspensionStore.capture()")


def _is_token(value) -> bool:
    return type(value) is ExecutionToken and value._mint is _MINT


_token_var: ContextVar = ContextVar("lumina_suspension_token", default=None)


@contextlib.contextmanager
def carrying(token: ExecutionToken):
    """Bind a captured token for synchronous descendants on this call stack.
    Asynchronous descendants receive the token as an explicit argument."""
    if not _is_token(token):
        raise AdmissionRefused(Refusal.MISSING_TOKEN, "carrying() needs a captured token")
    reset = _token_var.set(token)
    try:
        yield token
    finally:
        _token_var.reset(reset)


def inherited_token() -> ExecutionToken:
    """The token bound by the nearest carrying(). Missing lineage fails
    closed: it never defaults to the owner scope or to current state."""
    token = _token_var.get()
    if not _is_token(token):
        raise AdmissionRefused(Refusal.MISSING_TOKEN, "no captured execution token on this path")
    return token


def validate_scope(scope) -> str:
    if type(scope) is not str or not _SCOPE_RE.fullmatch(scope):
        raise ValueError("invalid suspension scope")
    return scope


def channel_scope(transport: str, channel_id) -> str:
    return validate_scope(f"channel:{transport}:{channel_id}")


# ---------------------------------------------------------------------------
# Recovery events
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ResumeExpectation:
    """Exactly the incident the owner was shown. resume() commits only if
    every field still equals the store."""
    soul_id: str
    store_incarnation: str
    scope: str
    execution_epoch: int
    state_revision: int
    suspension_id: str


@dataclass(frozen=True)
class RecoveryEvent:
    """An authenticated owner recovery event, consumed exactly once by
    resume(). It is bound to the exact incident the owner was shown
    (`binding`: soul, incarnation, scope, epoch, revision, suspension id).

    C1 mints desktop events only. A desktop event's freshness is local:
    minted by this process, in this boot, within DESKTOP_EVENT_MAX_AGE_S on
    the monotonic clock -- never wall-clock ordering, which a clock
    correction can move. Verified Telegram /resume events arrive in Slice C3
    with their own transport freshness rules."""
    event_id: str
    channel: str
    event_time: float
    ack_unknown: bool
    binding: ResumeExpectation
    minted_pid: int
    minted_boot: str
    minted_monotonic: float
    _mint: object = field(repr=False, compare=False)

    def __post_init__(self):
        if self._mint is not _MINT:
            raise TypeError("RecoveryEvent is created only by core.suspension factories")


def _require_local_owner(ingress) -> None:
    if not operator_ingress.may_control_suspension(ingress):
        raise NotAuthorized("needs a local desktop suspension-control envelope")


def desktop_recovery_event(ingress, expectation: ResumeExpectation, *, ack_unknown: bool = False,
                           now: Optional[Callable[[], float]] = None,
                           monotonic: Optional[Callable[[], float]] = None) -> RecoveryEvent:
    """A desktop owner resume request for exactly `expectation` (the
    incident on screen). Needs a genuine local envelope."""
    _require_local_owner(ingress)
    if type(expectation) is not ResumeExpectation:
        raise TypeError("expectation must be a ResumeExpectation")
    if type(ack_unknown) is not bool:
        raise TypeError("ack_unknown must be a bool")
    return RecoveryEvent(f"desktop:{uuid.uuid4().hex}", "desktop", float((now or time.time)()),
                         ack_unknown, expectation, os.getpid(), _boot_id(),
                         float((monotonic or time.monotonic)()), _MINT)


def _is_event(value) -> bool:
    return type(value) is RecoveryEvent and value._mint is _MINT


def _desktop_authority_fresh(event: RecoveryEvent) -> bool:
    if event.minted_pid != os.getpid() or event.minted_boot != _boot_id():
        return False
    age = time.monotonic() - event.minted_monotonic
    return 0.0 <= age <= DESKTOP_EVENT_MAX_AGE_S


# ---------------------------------------------------------------------------
# Views and outcomes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IncidentView:
    """Desktop operator display only. The selector never belongs in a
    public reply, a model-visible result, a transcript, a task result, a log
    or the Flight Recorder; it is excluded from repr()."""
    suspension_id: str
    scope: str
    execution_epoch: int
    state_revision: int
    soul_id: str
    store_incarnation: str
    trigger: str
    created_at: float
    evidence: dict
    in_flight_at_trip: int
    requires_ack: bool
    selector: str = field(repr=False)

    def expectation(self) -> ResumeExpectation:
        return ResumeExpectation(self.soul_id, self.store_incarnation, self.scope,
                                 self.execution_epoch, self.state_revision, self.suspension_id)


@dataclass(frozen=True)
class ScopeState:
    scope: str
    execution_epoch: int
    state_revision: int
    suspended: bool
    active_incident: Optional[str]


@dataclass(frozen=True)
class LeaseInfo:
    lease_id: str
    scope: str
    execution_epoch: int
    runtime_id: str
    kind: str
    name: str
    admitted_at: float


@dataclass(frozen=True)
class LeaseReport:
    live: tuple
    orphaned: tuple


@dataclass(frozen=True)
class TripOutcome:
    result: TripResult
    scope: str
    suspension_id: Optional[str] = None
    execution_epoch: Optional[int] = None
    state_revision: Optional[int] = None
    in_flight_at_trip: int = 0


@dataclass(frozen=True)
class ResumeOutcome:
    result: ResumeResult
    suspension_id: Optional[str]
    scope: Optional[str]
    state_revision: Optional[int] = None
    live_leases: tuple = ()
    orphaned_leases: tuple = ()
    replayed: bool = False

    def to_record(self) -> dict:
        return {"result": self.result.value, "suspension_id": self.suspension_id,
                "scope": self.scope, "state_revision": self.state_revision,
                "live_leases": list(self.live_leases),
                "orphaned_leases": list(self.orphaned_leases)}

    @classmethod
    def from_record(cls, record: dict, *, replayed: bool) -> "ResumeOutcome":
        return cls(ResumeResult(record["result"]), record["suspension_id"], record["scope"],
                   record["state_revision"], tuple(record["live_leases"]),
                   tuple(record["orphaned_leases"]), replayed)


@dataclass(frozen=True)
class StoreProbe:
    status: StoreStatus
    fault: Optional[Fault] = None
    traces: tuple = ()


@dataclass(frozen=True)
class RecoveryOutcome:
    evidence_dir: str
    old_incarnation: Optional[str]
    new_incarnation: str
    soul_id: str
    suspended_scopes: tuple
    salvage_complete: bool
    unknown_scope_policy: str
    unknown_outcome_leases: int


# ---------------------------------------------------------------------------
# Filesystem identity helpers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Paths:
    data_dir: str
    memory_dir: str
    db: str
    identity: str
    evidence: str


def _paths(data_dir) -> _Paths:
    if not isinstance(data_dir, (str, os.PathLike)):
        raise TypeError("data_dir must be a path")
    data_dir = os.path.abspath(os.fspath(data_dir))
    refuse_if_production_path(data_dir)
    memory = os.path.join(data_dir, MEMORY_DIRNAME)
    return _Paths(data_dir, memory, os.path.join(memory, STORE_NAME),
                  os.path.join(memory, IDENTITY_NAME), os.path.join(memory, EVIDENCE_DIRNAME))


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _identity_bytes(soul_id: str) -> bytes:
    return _canonical({"schema": _IDENTITY_SCHEMA, "version": 1, "soul_id": soul_id})


def _check_private(st: os.stat_result, what: str) -> None:
    if not stat.S_ISREG(st.st_mode):
        raise StoreFault(Fault.INSECURE, f"{what} is not a regular file")
    if st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise StoreFault(Fault.INSECURE, f"{what} must be private to this user (mode 600)")


def _memory_dir_identity(paths: _Paths) -> tuple:
    try:
        st = os.stat(paths.memory_dir, follow_symlinks=False)
    except FileNotFoundError:
        raise StoreFault(Fault.MISSING, "memory directory missing") from None
    if not stat.S_ISDIR(st.st_mode):
        raise StoreFault(Fault.INSECURE, "memory directory is not a directory")
    if st.st_uid != os.getuid() or st.st_mode & 0o022:
        raise StoreFault(Fault.INSECURE, "memory directory is writable by others")
    return (st.st_dev, st.st_ino)


def _read_identity(paths: _Paths) -> str:
    """The pinned soul_id, read without following a symlink."""
    try:
        fd = os.open(paths.identity, _READ_FLAGS)
    except FileNotFoundError:
        raise StoreFault(Fault.IDENTITY_MISSING) from None
    except OSError as exc:
        raise StoreFault(Fault.IDENTITY_INVALID, type(exc).__name__) from None
    try:
        _check_private(os.fstat(fd), "soul identity")
        raw = os.read(fd, 4097)
    finally:
        os.close(fd)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise StoreFault(Fault.IDENTITY_INVALID, "not JSON") from None
    if (type(data) is not dict or set(data) != {"schema", "version", "soul_id"}
            or data["schema"] != _IDENTITY_SCHEMA or data["version"] != 1
            or type(data["soul_id"]) is not str or not _HEX32.fullmatch(data["soul_id"])
            or raw != _identity_bytes(data["soul_id"])):
        raise StoreFault(Fault.IDENTITY_INVALID, "noncanonical identity pin")
    return data["soul_id"]


def _db_identity(paths: _Paths) -> tuple:
    try:
        st = os.stat(paths.db, follow_symlinks=False)
    except FileNotFoundError:
        raise StoreFault(Fault.MISSING, "suspension store missing") from None
    _check_private(st, "suspension store")
    return (st.st_dev, st.st_ino)


def _fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, _OPEN_DIR_FLAGS)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_new_private(path: str, data: bytes) -> None:
    fd = os.open(path, _CREATE_FLAGS, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def _replace_private(path: str, data: bytes) -> None:
    tmp = f"{path}.{secrets.token_hex(6)}.tmp"
    _write_new_private(tmp, data)
    try:
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    _fsync_dir(os.path.dirname(path))


def _connect(db_path: str) -> sqlite3.Connection:
    """Open an EXISTING store. mode=rw: SQLite never creates a missing file."""
    uri = "file:" + urllib.parse.quote(db_path) + "?mode=rw"
    conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_S, isolation_level=None,
                           check_same_thread=False)
    try:
        conn.execute("PRAGMA trusted_schema=OFF")
        conn.execute("PRAGMA synchronous=FULL")
    except Exception:
        conn.close()
        raise
    return conn


# ---------------------------------------------------------------------------
# Runtime liveness
# ---------------------------------------------------------------------------

def _boot_id() -> str:
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as fh:
            return fh.read().strip() or "unknown"
    except OSError:
        return "unknown"


def _proc_start_ticks(pid: int) -> Optional[int]:
    try:
        with open(f"/proc/{int(pid)}/stat", encoding="ascii", errors="replace") as fh:
            raw = fh.read()
    except (OSError, ValueError):
        return None
    try:
        # Field 22 (starttime); fields after the parenthesized comm.
        return int(raw[raw.rindex(")") + 2:].split()[19])
    except (ValueError, IndexError):
        return None


def _runtime_alive(row: dict, *, now: float) -> bool:
    """Is the runtime that holds a lease still running? A dead pid, a pid
    reused by a later process, or a reboot all mean dead. A runtime on
    another host can only be judged by its heartbeat."""
    if row["hostname"] == socket.gethostname():
        if row["boot_id"] != "unknown" and row["boot_id"] != _boot_id():
            return False
        ticks = _proc_start_ticks(row["pid"])
        return ticks is not None and ticks == row["start_ticks"]
    return (now - float(row["heartbeat_at"])) <= HEARTBEAT_STALE_S


# ---------------------------------------------------------------------------
# Evidence (counts and names only)
# ---------------------------------------------------------------------------

def _sanitize_evidence(evidence) -> dict:
    """Bounded counts-and-names evidence. Anything else is dropped and
    counted, never allowed to stop a trip."""
    if evidence is None:
        return {}
    if type(evidence) is not dict:
        return {"evidence_dropped": 1}
    clean, dropped = {}, 0
    for key, value in evidence.items():
        if len(clean) >= _EVIDENCE_MAX_ITEMS:
            dropped += 1
            continue
        if type(key) is not str or not _EVIDENCE_KEY.fullmatch(key) or key == "evidence_dropped":
            dropped += 1
        elif type(value) is bool or (type(value) is int and abs(value) < 2 ** 53):
            clean[key] = value
        elif type(value) is str and _EVIDENCE_STR.fullmatch(value):
            clean[key] = value
        else:
            dropped += 1
    if dropped:
        clean["evidence_dropped"] = dropped
    return clean


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE scopes (
    scope TEXT PRIMARY KEY,
    execution_epoch INTEGER NOT NULL CHECK (execution_epoch >= 0),
    state_revision INTEGER NOT NULL CHECK (state_revision >= 0),
    suspended INTEGER NOT NULL CHECK (suspended IN (0, 1)),
    active_incident TEXT,
    CHECK ((suspended = 1) = (active_incident IS NOT NULL))
);
CREATE TABLE incidents (
    suspension_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    execution_epoch INTEGER NOT NULL,
    trigger TEXT NOT NULL,
    evidence TEXT NOT NULL,
    created_at REAL NOT NULL,
    leases_at_trip TEXT NOT NULL,
    selector TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'resumed')),
    requires_ack INTEGER NOT NULL CHECK (requires_ack IN (0, 1)),
    resume_outcome TEXT
);
CREATE UNIQUE INDEX incidents_active_selector ON incidents(selector) WHERE status = 'active';
CREATE INDEX incidents_scope_epoch ON incidents(scope, execution_epoch);
CREATE TABLE leases (
    lease_id TEXT PRIMARY KEY,
    store_incarnation TEXT NOT NULL,
    scope TEXT NOT NULL,
    execution_epoch INTEGER NOT NULL,
    runtime_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    admitted_at REAL NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('in_flight', 'done', 'orphan_acknowledged')),
    finished_at REAL
);
CREATE INDEX leases_scope_state ON leases(scope, state);
CREATE TABLE runtimes (
    runtime_id TEXT PRIMARY KEY,
    pid INTEGER NOT NULL,
    boot_id TEXT NOT NULL,
    hostname TEXT NOT NULL,
    start_ticks INTEGER,
    enforcement_version INTEGER NOT NULL,
    data_root TEXT NOT NULL,
    registered_at REAL NOT NULL,
    heartbeat_at REAL NOT NULL
);
CREATE TABLE events (
    event_id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    suspension_id TEXT,
    outcome TEXT NOT NULL,
    consumed_at REAL NOT NULL
);
CREATE TABLE audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    action TEXT NOT NULL,
    scope TEXT,
    suspension_id TEXT,
    detail TEXT NOT NULL
);
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit
    BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit
    BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
-- Reserved for Slice C2 (token-bound deferred authority). Nothing writes it in C1.
CREATE TABLE deferred (
    kind TEXT NOT NULL,
    record_id TEXT NOT NULL,
    store_incarnation TEXT NOT NULL,
    scope TEXT NOT NULL,
    execution_epoch INTEGER NOT NULL,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (kind, record_id)
);
"""


def _new_selector(conn: sqlite3.Connection) -> str:
    while True:
        selector = "".join(secrets.choice(SELECTOR_ALPHABET) for _ in range(SELECTOR_LENGTH))
        if conn.execute("SELECT 1 FROM incidents WHERE selector = ? AND status = 'active'",
                        (selector,)).fetchone() is None:
            return selector


def _audit(conn, at: float, action: str, *, scope=None, suspension_id=None, detail=None) -> None:
    conn.execute("INSERT INTO audit (at, action, scope, suspension_id, detail) VALUES (?, ?, ?, ?, ?)",
                 (at, action, scope, suspension_id, json.dumps(detail or {}, sort_keys=True)))


def _insert_incident(conn, *, now: float, scope: str, epoch: int, trigger: str, evidence: dict,
                     leases: list, requires_ack: bool) -> str:
    suspension_id = secrets.token_hex(16)
    conn.execute(
        "INSERT INTO incidents (suspension_id, scope, execution_epoch, trigger, evidence, created_at, "
        "leases_at_trip, selector, status, requires_ack) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)",
        (suspension_id, scope, epoch, trigger, json.dumps(evidence, sort_keys=True), now,
         json.dumps(leases, sort_keys=True), _new_selector(conn), int(requires_ack)))
    return suspension_id


def _create_schema(conn, *, now: float, soul_id: str, incarnation: str,
                   unknown_scope_policy: str = "clear") -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Statement by statement: executescript() would COMMIT on its own.
        for statement in _SCHEMA.split(";\n"):
            if statement.strip():
                conn.execute(statement)
        conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", [
            ("schema_version", str(SCHEMA_VERSION)),
            ("min_enforcement_version", str(ENFORCEMENT_VERSION)),
            ("store_incarnation", incarnation),
            ("soul_id", soul_id),
            ("created_at", repr(now)),
            ("unknown_scope_policy", unknown_scope_policy),
        ])
        conn.execute("INSERT INTO scopes VALUES (?, 0, 0, 0, NULL)", (OWNER_SCOPE,))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _meta(conn) -> dict:
    return dict(conn.execute("SELECT key, value FROM meta").fetchall())


# ---------------------------------------------------------------------------
# Probe, initialize, open
# ---------------------------------------------------------------------------

def _traces(paths: _Paths) -> tuple:
    present = []
    for name in _TRACE_NAMES:
        with contextlib.suppress(FileNotFoundError):
            os.stat(os.path.join(paths.memory_dir, name), follow_symlinks=False)
            present.append(name)
    return tuple(present)


def probe(data_dir) -> StoreProbe:
    """Read-only classification. Never creates or repairs anything.

    ABSENT_NO_TRACE means only that no store, identity pin, WAL/journal or
    recovery evidence exists here. It is NOT proof that no store ever
    existed for this soul."""
    paths = _paths(data_dir)
    traces = _traces(paths)
    if STORE_NAME not in traces:
        return StoreProbe(StoreStatus.ABSENT_WITH_TRACE if traces else StoreStatus.ABSENT_NO_TRACE,
                          Fault.MISSING, traces)
    try:
        open_store(data_dir).close()
    except StoreFault as exc:
        return StoreProbe(StoreStatus.FAULT, exc.fault, traces)
    return StoreProbe(StoreStatus.HEALTHY, None, traces)


def initialize(data_dir, *, ingress, now: Optional[Callable[[], float]] = None) -> "SuspensionStore":
    """Create a brand-new store. Local owner only, and only when probe()
    finds no trace at all. The caller has already put the first-run question
    to the owner (ABSENT_NO_TRACE is not proof of a first run)."""
    _require_local_owner(ingress)
    clock = now or time.time
    paths = _paths(data_dir)
    os.makedirs(paths.memory_dir, mode=0o700, exist_ok=True)
    _memory_dir_identity(paths)
    traces = _traces(paths)
    if traces:
        raise RecoveryBlocked("prior suspension state exists here; use recovery, not initialization",
                              traces)
    soul_id, incarnation = secrets.token_hex(16), secrets.token_hex(16)
    # O_EXCL: losing a race with a concurrent initializer fails, never adopts.
    _write_new_private(paths.db, b"")
    conn = _connect(paths.db)
    try:
        _create_schema(conn, now=clock(), soul_id=soul_id, incarnation=incarnation)
        conn.execute("BEGIN IMMEDIATE")
        _audit(conn, clock(), "initialize", detail={"incarnation": incarnation})
        conn.execute("COMMIT")
    finally:
        conn.close()
    _write_new_private(paths.identity, _identity_bytes(soul_id))
    _fsync_dir(paths.memory_dir)
    return open_store(data_dir, now=now)


def open_store(data_dir, *, now: Optional[Callable[[], float]] = None) -> "SuspensionStore":
    """Open and identity-verify an existing store. Never creates one."""
    paths = _paths(data_dir)
    memory_id = _memory_dir_identity(paths)
    soul_id = _read_identity(paths)
    db_id = _db_identity(paths)
    try:
        conn = _connect(paths.db)
    except sqlite3.Error as exc:
        raise StoreFault(Fault.CORRUPT, type(exc).__name__) from None
    try:
        if _db_identity(paths) != db_id:
            raise StoreFault(Fault.REPLACED, "store replaced while opening")
        try:
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise StoreFault(Fault.CORRUPT, "quick_check failed")
            meta = _meta(conn)
        except sqlite3.DatabaseError as exc:
            raise StoreFault(Fault.CORRUPT, type(exc).__name__) from None
        if meta.get("schema_version") != str(SCHEMA_VERSION):
            raise StoreFault(Fault.SCHEMA, "unknown schema version")
        incarnation = meta.get("store_incarnation", "")
        if not _HEX32.fullmatch(incarnation):
            raise StoreFault(Fault.CORRUPT, "invalid incarnation")
        if meta.get("soul_id") != soul_id:
            raise StoreFault(Fault.SOUL_MISMATCH, "store belongs to a different data root")
        try:
            minimum = int(meta.get("min_enforcement_version", ""))
        except ValueError:
            raise StoreFault(Fault.CORRUPT, "invalid enforcement version") from None
        if ENFORCEMENT_VERSION < minimum:
            raise StoreFault(Fault.ENFORCEMENT_TOO_OLD, "this runtime is older than the store requires")
    except BaseException:
        conn.close()
        raise
    return SuspensionStore(paths, conn, soul_id=soul_id, incarnation=incarnation,
                           memory_id=memory_id, db_id=db_id, clock=now or time.time)


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

class SuspensionStore:
    """One open, identity-pinned store. Every operation re-walks the pinned
    identity (directory, store inode, identity pin, meta) inside its own
    transaction; any mismatch is a sticky fault for this handle."""

    def __init__(self, paths: _Paths, conn, *, soul_id, incarnation, memory_id, db_id, clock):
        self._paths = paths
        self._conn = conn
        self._lock = threading.RLock()
        self._clock = clock
        self._fault: Optional[StoreFault] = None
        self.soul_id = soul_id
        self.incarnation = incarnation
        self._memory_id = memory_id
        self._db_id = db_id
        # Trips that could not be committed: scope -> (token, trigger, evidence).
        # The scope stays stopped in this process until one commits.
        self._pending_trips: dict = {}
        self._unfinished: dict = {}   # lease_id -> runtime_id, finish() retried later

    # -- plumbing ----------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @property
    def fault(self) -> Optional[Fault]:
        return self._fault.fault if self._fault else None

    def _set_fault(self, fault: StoreFault) -> StoreFault:
        if self._fault is None:
            self._fault = fault
        return self._fault

    def _verify_identity(self, conn) -> None:
        if self._fault is not None:
            raise self._fault
        try:
            if _memory_dir_identity(self._paths) != self._memory_id:
                raise StoreFault(Fault.REPLACED, "memory directory replaced")
            if _db_identity(self._paths) != self._db_id:
                raise StoreFault(Fault.REPLACED, "store file replaced")
            if _read_identity(self._paths) != self.soul_id:
                raise StoreFault(Fault.SOUL_MISMATCH, "identity pin changed")
            meta = _meta(conn)
            if meta.get("store_incarnation") != self.incarnation:
                raise StoreFault(Fault.INCARNATION_CHANGED)
            if meta.get("soul_id") != self.soul_id:
                raise StoreFault(Fault.SOUL_MISMATCH, "store soul changed")
            if ENFORCEMENT_VERSION < int(meta.get("min_enforcement_version", "0")):
                raise StoreFault(Fault.ENFORCEMENT_TOO_OLD)
        except StoreFault as exc:
            raise self._set_fault(exc) from None
        except (sqlite3.DatabaseError, ValueError) as exc:
            raise self._set_fault(StoreFault(Fault.CORRUPT, type(exc).__name__)) from None

    @contextlib.contextmanager
    def _txn(self, write: bool = True):
        with self._lock:
            if self._fault is not None:
                raise self._fault
            conn = self._conn
            if conn is None:
                raise StoreFault(Fault.CLOSED)
            try:
                conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            except sqlite3.Error as exc:
                raise self._sqlite_error(exc) from None
            try:
                self._verify_identity(conn)
                yield conn
                conn.execute("COMMIT")
            except BaseException as exc:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                if isinstance(exc, sqlite3.Error):
                    raise self._sqlite_error(exc) from None
                raise

    def _sqlite_error(self, exc: sqlite3.Error) -> SuspensionError:
        """Busy/locked is transient (nothing changed). Anything else is store
        damage and a sticky fault."""
        if isinstance(exc, sqlite3.OperationalError) and any(
                word in str(exc).lower() for word in ("locked", "busy")):
            return StoreUnavailable(str(exc))
        return self._set_fault(StoreFault(Fault.CORRUPT, type(exc).__name__))

    def _scope_row(self, conn, scope: str):
        row = conn.execute("SELECT execution_epoch, state_revision, suspended, active_incident "
                           "FROM scopes WHERE scope = ?", (scope,)).fetchone()
        return None if row is None else {"epoch": row[0], "revision": row[1],
                                         "suspended": bool(row[2]), "incident": row[3]}

    def _ensure_scope(self, conn, scope: str) -> dict:
        row = self._scope_row(conn, scope)
        if row is not None:
            return row
        now = self._clock()
        if _meta(conn).get("unknown_scope_policy") == "suspended":
            # Destructive recovery could not read which scopes existed; a
            # scope first seen afterwards starts suspended until the owner
            # resumes it or acknowledges the unknown scopes.
            sid = _insert_incident(conn, now=now, scope=scope, epoch=1, trigger=RECOVERY_TRIGGER,
                                   evidence={"reason": "scope_state_unknown_after_recovery"},
                                   leases=[], requires_ack=True)
            conn.execute("INSERT INTO scopes VALUES (?, 1, 1, 1, ?)", (scope, sid))
            _audit(conn, now, "scope_created_suspended", scope=scope, suspension_id=sid)
        else:
            conn.execute("INSERT INTO scopes VALUES (?, 0, 0, 0, NULL)", (scope,))
        return self._scope_row(conn, scope)

    def _admission_error(self, exc: Exception) -> AdmissionRefused:
        if isinstance(exc, StoreFault):
            return AdmissionRefused(Refusal.STORE_FAULT, exc.detail, exc.fault)
        return AdmissionRefused(Refusal.STORE_BUSY, str(exc))

    def _check_local_stop(self, scope: str) -> None:
        with self._lock:
            pending = self._pending_trips.get(scope)
            if pending is None:
                return
            token, trigger, evidence = pending
            try:
                with self._txn() as conn:
                    self._commit_trip(conn, token, trigger, evidence)
            except (StoreFault, StoreUnavailable):
                raise AdmissionRefused(Refusal.LOCAL_STOP,
                                       "a suspension could not be recorded; this scope stays stopped here") from None
            self._pending_trips.pop(scope, None)

    @staticmethod
    def _token_refusal(token: ExecutionToken, incarnation: str, row) -> Optional[AdmissionRefused]:
        if token.store_incarnation != incarnation:
            return AdmissionRefused(Refusal.FOREIGN_INCARNATION)
        if row is None or token.execution_epoch > row["epoch"]:
            return AdmissionRefused(Refusal.INVALID_TOKEN)
        if token.execution_epoch < row["epoch"]:
            return AdmissionRefused(Refusal.STALE_TOKEN)
        if row["suspended"]:
            return AdmissionRefused(Refusal.SUSPENDED)
        return None

    # -- runtimes ----------------------------------------------------------

    def register_runtime(self, *, enforcement_version: int = ENFORCEMENT_VERSION) -> str:
        if type(enforcement_version) is not int:
            raise TypeError("enforcement_version must be an int")
        runtime_id = secrets.token_hex(16)
        try:
            root = os.stat(self._paths.data_dir)
        except OSError:
            raise StoreFault(Fault.MISSING, "data root missing") from None
        with self._txn() as conn:
            if enforcement_version < int(_meta(conn)["min_enforcement_version"]):
                raise StoreFault(Fault.ENFORCEMENT_TOO_OLD)
            now = self._clock()
            conn.execute("INSERT INTO runtimes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (runtime_id, os.getpid(), _boot_id(), socket.gethostname(),
                          _proc_start_ticks(os.getpid()), enforcement_version,
                          f"{root.st_dev}:{root.st_ino}:{self.soul_id}", now, now))
            _audit(conn, now, "runtime_registered", detail={"runtime_id": runtime_id,
                                                           "pid": os.getpid()})
        return runtime_id

    def heartbeat(self, runtime_id: str) -> None:
        with self._txn() as conn:
            conn.execute("UPDATE runtimes SET heartbeat_at = ? WHERE runtime_id = ? AND pid = ?",
                         (self._clock(), runtime_id, os.getpid()))
        self._flush_unfinished()

    # -- capture / verify / admit / finish ---------------------------------

    def capture(self, scope: str) -> ExecutionToken:
        """Mint the token for NEW root work in `scope`, before its first
        asynchronous handoff. Refused while the scope is suspended."""
        validate_scope(scope)
        self._check_local_stop(scope)
        try:
            # Committed even when refused: a scope first seen after a lossy
            # recovery is created suspended, and that incident must persist.
            with self._txn() as conn:
                row = self._ensure_scope(conn, scope)
        except (StoreFault, StoreUnavailable) as exc:
            raise self._admission_error(exc) from None
        if row["suspended"]:
            raise AdmissionRefused(Refusal.SUSPENDED)
        return ExecutionToken(self.incarnation, scope, row["epoch"], _MINT)

    def verify(self, token: ExecutionToken) -> None:
        """Read-only admission check (no lease): raises AdmissionRefused."""
        if not _is_token(token):
            raise AdmissionRefused(Refusal.MISSING_TOKEN)
        self._check_local_stop(token.scope)
        try:
            with self._txn(write=False) as conn:
                refusal = self._token_refusal(token, self.incarnation, self._scope_row(conn, token.scope))
        except (StoreFault, StoreUnavailable) as exc:
            raise self._admission_error(exc) from None
        if refusal is not None:
            raise refusal

    def admit(self, token: ExecutionToken, *, kind: LeaseKind, name: str, runtime_id: str) -> str:
        """Admit one dispatch / provider request / consequential write and
        lease it, in the transaction that validates the token. No dispatch
        whose admission linearizes after a committed trip can start."""
        if not _is_token(token):
            raise AdmissionRefused(Refusal.MISSING_TOKEN)
        if type(kind) is not LeaseKind:
            raise TypeError("kind must be a LeaseKind")
        name = str(name)[:96]
        self._check_local_stop(token.scope)
        lease_id = secrets.token_hex(16)
        try:
            with self._txn() as conn:
                runtime = conn.execute("SELECT pid, boot_id, enforcement_version FROM runtimes "
                                       "WHERE runtime_id = ?", (runtime_id,)).fetchone()
                if (runtime is None or runtime[0] != os.getpid() or runtime[1] != _boot_id()
                        or runtime[2] < int(_meta(conn)["min_enforcement_version"])):
                    raise AdmissionRefused(Refusal.UNREGISTERED_RUNTIME)
                refusal = self._token_refusal(token, self.incarnation, self._scope_row(conn, token.scope))
                if refusal is not None:
                    raise refusal
                conn.execute("INSERT INTO leases VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'in_flight', NULL)",
                             (lease_id, token.store_incarnation, token.scope, token.execution_epoch,
                              runtime_id, kind.value, name, self._clock()))
        except (StoreFault, StoreUnavailable) as exc:
            raise self._admission_error(exc) from None
        self._flush_unfinished()
        return lease_id

    def finish(self, lease_id: str, runtime_id: str) -> bool:
        """Best effort. On failure the lease stays in_flight (which blocks a
        resume) and this handle retries it on a later operation."""
        with self._lock:
            try:
                with self._txn() as conn:
                    conn.execute("UPDATE leases SET state = 'done', finished_at = ? WHERE lease_id = ? "
                                 "AND runtime_id = ? AND state = 'in_flight'",
                                 (self._clock(), lease_id, runtime_id))
            except (StoreFault, StoreUnavailable):
                self._unfinished[lease_id] = runtime_id
                return False
            self._unfinished.pop(lease_id, None)
            return True

    def _flush_unfinished(self) -> None:
        with self._lock:
            pending = list(self._unfinished.items())
        for lease_id, runtime_id in pending:
            if not self.finish(lease_id, runtime_id):
                return

    # -- trip --------------------------------------------------------------

    def _commit_trip(self, conn, token: ExecutionToken, trigger: Trigger, evidence: dict) -> TripOutcome:
        now = self._clock()
        scope = token.scope
        if token.store_incarnation != self.incarnation:
            _audit(conn, now, "trip_foreign_incarnation", scope=scope, detail=evidence)
            return TripOutcome(TripResult.FOREIGN_INCARNATION, scope)
        row = self._scope_row(conn, scope)
        if row is None or token.execution_epoch > row["epoch"]:
            _audit(conn, now, "trip_invalid_token", scope=scope, detail=evidence)
            return TripOutcome(TripResult.INVALID_TOKEN, scope)
        if token.execution_epoch < row["epoch"]:
            # A late trip from work that is already stale: evidence for the
            # incident that made it stale, never a new suspension.
            found = conn.execute("SELECT suspension_id FROM incidents WHERE scope = ? AND execution_epoch = ?",
                                 (scope, token.execution_epoch + 1)).fetchone()
            sid = found[0] if found else None
            _audit(conn, now, "late_trip_evidence", scope=scope, suspension_id=sid,
                   detail={"trigger": trigger.value, **evidence})
            return TripOutcome(TripResult.LATE_EVIDENCE, scope, sid, row["epoch"], row["revision"])
        if row["suspended"]:
            _audit(conn, now, "trip_while_suspended", scope=scope, suspension_id=row["incident"],
                   detail={"trigger": trigger.value, **evidence})
            return TripOutcome(TripResult.ALREADY_SUSPENDED, scope, row["incident"],
                               row["epoch"], row["revision"])
        leases = [{"lease_id": r[0], "kind": r[1], "name": r[2], "runtime_id": r[3]}
                  for r in conn.execute("SELECT lease_id, kind, name, runtime_id FROM leases "
                                        "WHERE scope = ? AND state = 'in_flight' ORDER BY admitted_at",
                                        (scope,))]
        epoch, revision = row["epoch"] + 1, row["revision"] + 1
        sid = _insert_incident(conn, now=now, scope=scope, epoch=epoch, trigger=trigger.value,
                               evidence=evidence, leases=leases, requires_ack=False)
        conn.execute("UPDATE scopes SET execution_epoch = ?, state_revision = ?, suspended = 1, "
                     "active_incident = ? WHERE scope = ?", (epoch, revision, sid, scope))
        _audit(conn, now, "trip", scope=scope, suspension_id=sid,
               detail={"trigger": trigger.value, "in_flight": len(leases), **evidence})
        return TripOutcome(TripResult.TRIPPED, scope, sid, epoch, revision, len(leases))

    def trip(self, expected_token: ExecutionToken, *, trigger: Trigger, evidence: dict = None) -> TripOutcome:
        """Suspend the token's scope -- if and only if the token is still
        current (compare-and-swap). If the transaction cannot commit, the
        scope stops in THIS process and the outcome says the global
        suspension is unconfirmed; it is retried on the next admission."""
        if not _is_token(expected_token):
            raise TypeError("trip() needs a captured ExecutionToken")
        if type(trigger) is not Trigger:
            raise TypeError("trigger must be a Trigger")
        clean = _sanitize_evidence(evidence)
        with self._lock:
            try:
                with self._txn() as conn:
                    outcome = self._commit_trip(conn, expected_token, trigger, clean)
            except (StoreFault, StoreUnavailable):
                self._pending_trips.setdefault(expected_token.scope, (expected_token, trigger, clean))
                return TripOutcome(TripResult.GLOBAL_UNCONFIRMED, expected_token.scope)
            self._pending_trips.pop(expected_token.scope, None)
            return outcome

    def local_stops(self) -> tuple:
        """Scopes stopped in this process because a trip could not be recorded."""
        with self._lock:
            return tuple(sorted(self._pending_trips))

    # -- leases, drain ------------------------------------------------------

    def _stale_leases(self, conn, scope: str, epoch: int) -> tuple:
        now = self._clock()
        runtimes = {r[0]: {"pid": r[1], "boot_id": r[2], "hostname": r[3], "start_ticks": r[4],
                           "heartbeat_at": r[5]}
                    for r in conn.execute("SELECT runtime_id, pid, boot_id, hostname, start_ticks, "
                                          "heartbeat_at FROM runtimes")}
        live, orphaned = [], []
        for r in conn.execute("SELECT lease_id, scope, execution_epoch, runtime_id, kind, name, admitted_at "
                              "FROM leases WHERE scope = ? AND state = 'in_flight' AND execution_epoch < ? "
                              "ORDER BY admitted_at", (scope, epoch)):
            info = LeaseInfo(*r)
            runtime = runtimes.get(info.runtime_id)
            alive = runtime is not None and _runtime_alive(runtime, now=now)
            (live if alive else orphaned).append(info)
        return tuple(live), tuple(orphaned)

    def lease_report(self, scope: str) -> LeaseReport:
        """In-flight leases older than the scope's current epoch, split into
        still-running (blocks resume) and orphaned (outcome unknown)."""
        validate_scope(scope)
        with self._txn(write=False) as conn:
            row = self._scope_row(conn, scope)
            if row is None:
                return LeaseReport((), ())
            return LeaseReport(*self._stale_leases(conn, scope, row["epoch"]))

    # -- resume -------------------------------------------------------------

    def resume(self, event: RecoveryEvent, expectation: ResumeExpectation) -> ResumeOutcome:
        """Clear exactly the incident the owner was shown, consuming `event`
        exactly once in the same transaction. A replayed event returns its
        stored outcome and changes nothing. Raises StoreFault/StoreUnavailable
        if the store can't be used (nothing changed)."""
        if not _is_event(event):
            raise NotAuthorized("resume() needs a RecoveryEvent from a core.suspension factory")
        if type(expectation) is not ResumeExpectation:
            raise TypeError("expectation must be a ResumeExpectation")
        with self._txn() as conn:
            prior = conn.execute("SELECT outcome FROM events WHERE event_id = ?", (event.event_id,)).fetchone()
            if prior is not None:
                return ResumeOutcome.from_record(json.loads(prior[0]), replayed=True)
            outcome = self._decide_resume(conn, event, expectation)
            now = self._clock()
            conn.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?)",
                         (event.event_id, event.channel, outcome.suspension_id,
                          json.dumps(outcome.to_record(), sort_keys=True), now))
            _audit(conn, now, "resume" if outcome.result is ResumeResult.RESUMED else "resume_refused",
                   scope=outcome.scope, suspension_id=outcome.suspension_id,
                   detail={"channel": event.channel, "result": outcome.result.value,
                           "orphans_acknowledged": len(outcome.orphaned_leases)
                           if outcome.result is ResumeResult.RESUMED else 0})
            return outcome

    def _decide_resume(self, conn, event: RecoveryEvent, exp: ResumeExpectation) -> ResumeOutcome:
        sid = exp.suspension_id
        if event.binding != exp:
            return ResumeOutcome(ResumeResult.EVENT_BINDING_MISMATCH, sid, exp.scope)
        if event.channel == "desktop" and not _desktop_authority_fresh(event):
            return ResumeOutcome(ResumeResult.STALE_AUTHORITY, sid, exp.scope)
        inc = conn.execute("SELECT scope, execution_epoch, created_at, status, requires_ack FROM incidents "
                           "WHERE suspension_id = ?", (sid,)).fetchone()
        if inc is None:
            return ResumeOutcome(ResumeResult.NO_SUCH_INCIDENT, sid, exp.scope)
        scope, epoch, created_at, status, requires_ack = inc
        row = self._scope_row(conn, scope)
        if status != "active" or row is None or not row["suspended"] or row["incident"] != sid:
            return ResumeOutcome(ResumeResult.NOT_ACTIVE, sid, scope)
        if (exp.soul_id != self.soul_id or exp.store_incarnation != self.incarnation or exp.scope != scope
                or exp.execution_epoch != row["epoch"] or exp.execution_epoch != epoch
                or exp.state_revision != row["revision"]):
            return ResumeOutcome(ResumeResult.MISMATCH, sid, scope)
        # Transport freshness for external channels (Telegram /resume, C3):
        # an event dated before the incident cannot target it. Desktop
        # events are bound and locally fresh instead (see RecoveryEvent).
        if event.channel != "desktop" and event.event_time < math.floor(created_at):
            return ResumeOutcome(ResumeResult.EVENT_BEFORE_INCIDENT, sid, scope)
        live, orphaned = self._stale_leases(conn, scope, row["epoch"])
        live_ids = tuple(l.lease_id for l in live)
        orphan_ids = tuple(l.lease_id for l in orphaned)
        if live:
            return ResumeOutcome(ResumeResult.DRAIN_PENDING, sid, scope, live_leases=live_ids,
                                 orphaned_leases=orphan_ids)
        if (orphaned or requires_ack) and not event.ack_unknown:
            return ResumeOutcome(ResumeResult.UNKNOWN_NEEDS_ACK, sid, scope, orphaned_leases=orphan_ids)
        now = self._clock()
        revision = row["revision"] + 1
        conn.executemany("UPDATE leases SET state = 'orphan_acknowledged', finished_at = ? "
                         "WHERE lease_id = ? AND state = 'in_flight'", [(now, i) for i in orphan_ids])
        updated = conn.execute("UPDATE scopes SET suspended = 0, active_incident = NULL, state_revision = ? "
                               "WHERE scope = ? AND active_incident = ? AND state_revision = ? "
                               "AND execution_epoch = ?",
                               (revision, scope, sid, row["revision"], row["epoch"])).rowcount
        if updated != 1:  # defense in depth; the checks above make this unreachable
            raise StoreFault(Fault.CORRUPT, "resume compare-and-swap lost")
        outcome = ResumeOutcome(ResumeResult.RESUMED, sid, scope, revision, orphaned_leases=orphan_ids)
        conn.execute("UPDATE incidents SET status = 'resumed', resume_outcome = ? WHERE suspension_id = ?",
                     (json.dumps(outcome.to_record(), sort_keys=True), sid))
        return outcome

    # -- views ---------------------------------------------------------------

    def scope_state(self, scope: str) -> ScopeState:
        validate_scope(scope)
        with self._txn(write=False) as conn:
            row = self._scope_row(conn, scope)
        if row is None:
            return ScopeState(scope, 0, 0, False, None)
        return ScopeState(scope, row["epoch"], row["revision"], row["suspended"], row["incident"])

    def operator_view(self) -> tuple:
        """Active incidents for the desktop operator display (carries the
        selector; see IncidentView)."""
        with self._txn(write=False) as conn:
            rows = conn.execute(
                "SELECT i.suspension_id, i.scope, i.execution_epoch, s.state_revision, i.trigger, "
                "i.created_at, i.evidence, i.leases_at_trip, i.requires_ack, i.selector "
                "FROM incidents i JOIN scopes s ON s.active_incident = i.suspension_id "
                "WHERE i.status = 'active' ORDER BY i.created_at").fetchall()
        return tuple(IncidentView(r[0], r[1], r[2], r[3], self.soul_id, self.incarnation, r[4], r[5],
                                  json.loads(r[6]),
                                  len(json.loads(r[7])), bool(r[8]), r[9]) for r in rows)

    def audit_records(self) -> tuple:
        with self._txn(write=False) as conn:
            return tuple(conn.execute("SELECT seq, at, action, scope, suspension_id, detail FROM audit "
                                      "ORDER BY seq").fetchall())

    def acknowledge_unknown_scopes(self, *, ingress) -> None:
        """End the post-recovery 'unknown scopes start suspended' policy."""
        _require_local_owner(ingress)
        with self._txn() as conn:
            conn.execute("UPDATE meta SET value = 'clear' WHERE key = 'unknown_scope_policy'")
            _audit(conn, self._clock(), "unknown_scopes_acknowledged")


# ---------------------------------------------------------------------------
# Recovery (local owner only)
# ---------------------------------------------------------------------------

def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _preserve_evidence(paths: _Paths, now: float) -> str:
    """Copy every store file and the identity pin, byte for byte, before
    anything else happens. Returns the evidence directory."""
    os.makedirs(paths.evidence, mode=0o700, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now)) + "-" + secrets.token_hex(4)
    target = os.path.join(paths.evidence, stamp)
    os.mkdir(target, 0o700)
    for name in _STORE_FILES + (IDENTITY_NAME,):
        source = os.path.join(paths.memory_dir, name)
        try:
            st = os.stat(source, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(st.st_mode):
            raise RecoveryBlocked(f"{name} is not a regular file; not touching it")
        copy = os.path.join(target, name)
        shutil.copyfile(source, copy, follow_symlinks=False)
        os.chmod(copy, 0o600)
        if _sha256(source) != _sha256(copy):
            raise RecoveryBlocked(f"evidence copy of {name} does not match; stopping")
    _fsync_dir(target)
    return target


def _salvage(evidence_dir: str) -> dict:
    """Read what can still be read, from a scratch copy of the evidence
    (the evidence itself stays byte-identical)."""
    found = {"meta": None, "scopes": None, "runtimes": None, "in_flight": None}
    if not os.path.exists(os.path.join(evidence_dir, STORE_NAME)):
        return found
    scratch = os.path.join(evidence_dir, "salvage-scratch")
    os.mkdir(scratch, 0o700)
    try:
        for name in _STORE_FILES:
            source = os.path.join(evidence_dir, name)
            if os.path.exists(source):
                shutil.copyfile(source, os.path.join(scratch, name))
        try:
            conn = sqlite3.connect(os.path.join(scratch, STORE_NAME), isolation_level=None)
        except sqlite3.Error:
            return found
        try:
            for key, query in (("meta", "SELECT key, value FROM meta"),
                               ("scopes", "SELECT scope, execution_epoch, suspended, active_incident FROM scopes"),
                               ("runtimes", "SELECT runtime_id, pid, boot_id, hostname, start_ticks, "
                                            "heartbeat_at FROM runtimes"),
                               ("in_flight", "SELECT scope, COUNT(*) FROM leases WHERE state = 'in_flight' "
                                             "GROUP BY scope")):
                with contextlib.suppress(sqlite3.Error):
                    found[key] = conn.execute(query).fetchall()
        finally:
            conn.close()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return found


def repair_identity(data_dir, *, ingress, now: Optional[Callable[[], float]] = None) -> SuspensionStore:
    """Non-destructive repair: the store is intact but its identity pin is
    missing or unreadable. Re-pins the store's own soul_id, keeping its
    incarnation and every suspension. A pin that is readable but names a
    different soul is NOT repaired here (that store may belong to another
    data root): use recover_destructively()."""
    _require_local_owner(ingress)
    clock = now or time.time
    paths = _paths(data_dir)
    _memory_dir_identity(paths)
    try:
        _read_identity(paths)
    except StoreFault as exc:
        if exc.fault not in (Fault.IDENTITY_MISSING, Fault.IDENTITY_INVALID):
            raise
    else:
        raise RecoveryBlocked("the identity pin is readable; nothing to repair")
    _db_identity(paths)
    conn = _connect(paths.db)
    try:
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RecoveryBlocked("the store itself is damaged; non-destructive repair is impossible")
        meta = _meta(conn)
    except sqlite3.DatabaseError:
        raise RecoveryBlocked("the store itself is damaged; non-destructive repair is impossible") from None
    finally:
        conn.close()
    soul_id = meta.get("soul_id", "")
    if meta.get("schema_version") != str(SCHEMA_VERSION) or not _HEX32.fullmatch(soul_id):
        raise RecoveryBlocked("the store's own identity is unreadable")
    evidence_dir = _preserve_evidence(paths, clock())
    _replace_private(paths.identity, _identity_bytes(soul_id))
    store = open_store(data_dir, now=now)
    with store._txn() as c:
        _audit(c, clock(), "identity_repaired", detail={"evidence": os.path.basename(evidence_dir)})
    return store


def recover_destructively(data_dir, *, ingress, acknowledged_runtimes=frozenset(),
                          acknowledge_unreadable_runtimes: bool = False,
                          acknowledge_unknown_scopes: bool = False,
                          now: Optional[Callable[[], float]] = None) -> RecoveryOutcome:
    """Destructive recovery, for a store that can't be opened and can't be
    repaired non-destructively (design R2 §4.4). Local owner only:

      1. preserve byte-identical evidence (store, WAL, shm, identity pin);
      2. quiesce: every other runtime registered in the old store must be
         confirmed dead or explicitly acknowledged by the owner;
      3. rotate store_incarnation, so every old token is rejected;
      4. re-create a suspension for every scope last known suspended, and
         for the owner scope unless it was readably clear. If the scope
         table was unreadable, new scopes start suspended until the owner
         acknowledges them (unless acknowledge_unknown_scopes).

    A healthy store is never destroyed."""
    _require_local_owner(ingress)
    clock = now or time.time
    paths = _paths(data_dir)
    _memory_dir_identity(paths)
    status = probe(data_dir).status
    if status is StoreStatus.HEALTHY:
        raise RecoveryBlocked("the store is healthy; refusing to destroy it")
    if status is StoreStatus.ABSENT_NO_TRACE:
        raise RecoveryBlocked("no prior state exists here; this is initialization, not recovery")

    evidence_dir = _preserve_evidence(paths, clock())
    found = _salvage(evidence_dir)

    current = (os.getpid(), _boot_id(), socket.gethostname(), _proc_start_ticks(os.getpid()))
    if found["runtimes"] is None:
        if not acknowledge_unreadable_runtimes:
            raise RecoveryBlocked("the old runtime registry is unreadable; the owner must "
                                  "acknowledge that unknown runtimes may still be running")
    else:
        blocking = []
        for rid, pid, boot, host, ticks, heartbeat in found["runtimes"]:
            if (pid, boot, host, ticks) == current or rid in acknowledged_runtimes:
                continue
            row = {"pid": pid, "boot_id": boot, "hostname": host, "start_ticks": ticks,
                   "heartbeat_at": heartbeat}
            if _runtime_alive(row, now=clock()):
                blocking.append(rid)
        if blocking:
            raise RecoveryBlocked("other runtimes may still be running; stop them or acknowledge them",
                                  tuple(blocking))

    old_meta = dict(found["meta"]) if found["meta"] is not None else {}
    try:
        soul_id = _read_identity(paths)
    except StoreFault:
        soul_id = old_meta.get("soul_id", "")
        if not _HEX32.fullmatch(soul_id):
            soul_id = secrets.token_hex(16)
    old_incarnation = old_meta.get("store_incarnation")
    in_flight = {scope: count for scope, count in (found["in_flight"] or [])}
    unknown_leases = sum(in_flight.values()) if found["in_flight"] is not None else -1

    salvage_complete = found["scopes"] is not None
    if salvage_complete:
        suspended = sorted({s for s, _e, susp, _i in found["scopes"] if susp})
        known_clear = {s for s, _e, susp, _i in found["scopes"] if not susp}
        if OWNER_SCOPE not in known_clear:
            suspended = sorted(set(suspended) | {OWNER_SCOPE})
    else:
        suspended = [OWNER_SCOPE]
    policy = "suspended" if not salvage_complete and not acknowledge_unknown_scopes else "clear"

    for name in _STORE_FILES:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(os.path.join(paths.memory_dir, name))
    _fsync_dir(paths.memory_dir)

    incarnation = secrets.token_hex(16)
    _write_new_private(paths.db, b"")
    conn = _connect(paths.db)
    try:
        _create_schema(conn, now=clock(), soul_id=soul_id, incarnation=incarnation,
                       unknown_scope_policy=policy)
        conn.execute("BEGIN IMMEDIATE")
        at = clock()
        for scope in suspended:
            ack = unknown_leases != 0 or not salvage_complete
            sid = _insert_incident(conn, now=at, scope=scope, epoch=1, trigger=RECOVERY_TRIGGER,
                                   evidence={"reason": "state_preserved_through_recovery",
                                             "unknown_outcome_leases": in_flight.get(scope, 0)},
                                   leases=[], requires_ack=ack)
            conn.execute("INSERT OR IGNORE INTO scopes VALUES (?, 1, 1, 1, ?)", (scope, sid))
            conn.execute("UPDATE scopes SET execution_epoch = 1, state_revision = 1, suspended = 1, "
                         "active_incident = ? WHERE scope = ?", (sid, scope))
        _audit(conn, at, "destructive_recovery",
               detail={"old_incarnation": old_incarnation or "unknown", "new_incarnation": incarnation,
                       "evidence": os.path.basename(evidence_dir), "salvage_complete": salvage_complete,
                       "suspended_scopes": len(suspended), "unknown_scope_policy": policy})
        conn.execute("COMMIT")
    finally:
        conn.close()
    _replace_private(paths.identity, _identity_bytes(soul_id))
    _fsync_dir(paths.memory_dir)
    open_store(data_dir, now=now).close()
    return RecoveryOutcome(evidence_dir, old_incarnation, incarnation, soul_id, tuple(suspended),
                           salvage_complete, policy, unknown_leases)
