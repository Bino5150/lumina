"""
SUBSCRIPTION-PLAN-BACKENDS-01C -- OS-local custody for ChatGPT sign-in
sessions (issued client IDs, verified identities, rotating token sets) and
this installation's stable host ID.

Where it lives and why
----------------------
Default directory: ~/.config/lumina/chatgpt/ (override: LUMINA_CHATGPT_AUTH_DIR,
set by tests/conftest.py before any import -- same pattern as
LUMINA_SECRETS_PATH). It is deliberately OUTSIDE LUMINA_DATA_DIR: the data
dir is Lumina's durable "soul" (prefs, Palace, transcripts, Flight Recorder)
that Agent Backup archives and that is meant to be portable between the
Mint and Kali installations. A ChatGPT session is OS-local credential
custody: each OS installation signs in on its own and gets its own host ID
(vendor contract: one host ID per host, a client may be shared across
hosts). Nothing here is ever written into prefs.json, the data dir, the
Flight Recorder, or a backup.

Protection, stated honestly
---------------------------
Files are owner-only (directory 0700, files 0600, owner == this uid,
regular file, never a symlink, never hard-linked) -- the protection OpenAI's
sign-in documentation prescribes ("Write files atomically with owner-only
permissions (0600 on Unix)"). They are NOT encrypted at rest; the schema's
"protection" field records exactly that ("owner_only_file") so a future
OS-keyring/encrypted provider is an explicit format change, never a silent
claim. Anything running as this OS user can read them, the same as
~/.config/lumina/credentials.json today.

Crash consistency / coordination
--------------------------------
* Every write is a full-document replacement: private temp file in the same
  directory (O_EXCL, 0600) -> fsync -> os.replace -> fsync(dir). A crash
  leaves either the old or the new document, never a mix; leftover temp
  files (which may hold tokens) are removed under the lock on the next
  write.
* The document carries a schema tag and a SHA-256 of its canonical body, so
  truncation or a partial/manual edit is detected and reported as corrupt --
  never "repaired" by discarding it.
* Two advisory kernel locks (flock on POSIX, auto-released if the process
  dies -- no stale-lock heuristics):
    store lock   -- every read-modify-write of the session document.
    refresh lock -- held across a token refresh's network round trip so
                    two processes (or threads) never both spend the same
                    rotating refresh token, and held by disconnect/reconnect
                    while they settle so they cannot interleave with one.
  Lock order is always refresh lock -> store lock, never the reverse.
  Neither lock is re-entrant; a nested acquisition in the same thread is a
  programming error and raises instead of deadlocking.
"""
from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import re
import stat
import threading
import time
import uuid
from typing import Iterator, Optional

from core.test_isolation import refuse_if_production_path

SESSIONS_SCHEMA = "lumina.chatgpt.sessions/1"
HOST_SCHEMA = "lumina.chatgpt.host/1"
PROTECTION = "owner_only_file"

SESSIONS_FILENAME = "sessions.json"
HOST_FILENAME = "host.json"
STORE_LOCK_FILENAME = ".store.lock"
REFRESH_LOCK_FILENAME = ".refresh.lock"
_TEMP_PREFIX = ".tmp-"

MAX_FILE_BYTES = 1024 * 1024
DEFAULT_LOCK_TIMEOUT = 30.0
_LOCK_POLL_SECONDS = 0.02

BOOTSTRAP_CLIENT_ID = "dynamic_agent_client"
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
_PROFILE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_HOST_ID_RE = re.compile(
    r"^urn:uuid:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

# Persisted per-profile status. Session states shown to callers are derived
# from these plus credentials/scopes (core/chatgpt_auth/session.py).
STATUS_CONNECTED = "connected"
STATUS_REAUTH_REQUIRED = "reauth_required"
STATUS_DISCONNECTING = "disconnecting"
STATUS_DISCONNECTED = "disconnected"
STATUS_DISCONNECTED_REMOTE_UNCONFIRMED = "disconnected_remote_unconfirmed"
PROFILE_STATUSES = frozenset({
    STATUS_CONNECTED, STATUS_REAUTH_REQUIRED, STATUS_DISCONNECTING,
    STATUS_DISCONNECTED, STATUS_DISCONNECTED_REMOTE_UNCONFIRMED,
})


def default_store_dir() -> str:
    """Resolved at call time (not import time) so a test's environment is
    honored even if this module was imported first."""
    return os.environ.get("LUMINA_CHATGPT_AUTH_DIR") or os.path.expanduser(
        "~/.config/lumina/chatgpt"
    )


# ---------------------------------------------------------------------------
# Errors -- messages are fixed strings; never file contents or token text.
# ---------------------------------------------------------------------------

class StoreError(Exception):
    """Base class. `reason` is a short categorical code."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


class StoreUnsafe(StoreError):
    """The directory or a file is not private to this user (mode, owner,
    symlink, hardlink, wrong type). Fail closed; never auto-fixed for files."""


class StoreCorrupt(StoreError):
    """The document cannot be trusted (parse, schema, checksum, size)."""


class StoreBusy(StoreError):
    """A lock could not be acquired within its timeout."""


class LockDiscipline(StoreError):
    """A store operation ran without its lock, or a lock was re-entered."""


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class Credentials:
    """One token generation. Secret fields are excluded from repr()."""
    access_token: str = dataclasses.field(repr=False)
    refresh_token: Optional[str] = dataclasses.field(repr=False)
    id_token: Optional[str] = dataclasses.field(repr=False)
    token_type: str
    expires_at: float
    earliest_refresh_at: Optional[float]
    scopes: tuple
    received_at: float

    def to_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "id_token": self.id_token,
            "token_type": self.token_type,
            "expires_at": self.expires_at,
            "earliest_refresh_at": self.earliest_refresh_at,
            "scopes": list(self.scopes),
            "received_at": self.received_at,
        }

    @classmethod
    def from_dict(cls, value) -> "Credentials":
        _require(isinstance(value, dict), "credentials")
        _require(_nonempty_str(value.get("access_token")), "credentials.access_token")
        refresh = value.get("refresh_token")
        _require(refresh is None or _nonempty_str(refresh), "credentials.refresh_token")
        _require(value.get("id_token") is None or _nonempty_str(value.get("id_token")),
                 "credentials.id_token")
        _require(value.get("token_type") == "Bearer", "credentials.token_type")
        _require(_finite_number(value.get("expires_at")), "credentials.expires_at")
        _require(value.get("earliest_refresh_at") is None
                 or _finite_number(value.get("earliest_refresh_at")),
                 "credentials.earliest_refresh_at")
        scopes = value.get("scopes")
        _require(isinstance(scopes, list) and all(_nonempty_str(s) for s in scopes),
                 "credentials.scopes")
        _require(refresh is not None or ("offline_access" not in scopes
                 and "chatgpt.tokens.use.direct" not in scopes), "credentials.refresh_token")
        _require(_finite_number(value.get("received_at")), "credentials.received_at")
        return cls(
            access_token=value["access_token"],
            refresh_token=value["refresh_token"],
            id_token=value.get("id_token"),
            token_type="Bearer",
            expires_at=float(value["expires_at"]),
            earliest_refresh_at=(None if value.get("earliest_refresh_at") is None
                                 else float(value["earliest_refresh_at"])),
            scopes=tuple(scopes),
            received_at=float(value["received_at"]),
        )


@dataclasses.dataclass
class PendingRotation:
    """A refresh response received from the vendor but not yet published as
    the active generation. Persisted the moment it arrives (the vendor has
    already retired the previous refresh token, so this is the only usable
    renewal credential left). Never handed to a caller.

    promotable=True  -- may become generation base_generation+1 once its
                        identity (if an ID token came back) is verified.
    promotable=False -- the refresh lost to a disconnect that started while
                        it was in flight; kept only so the disconnect can
                        revoke the newest refresh token, then erased."""
    credentials: Credentials
    base_generation: int
    promotable: bool

    def to_dict(self) -> dict:
        return {
            "credentials": self.credentials.to_dict(),
            "base_generation": self.base_generation,
            "promotable": self.promotable,
        }

    @classmethod
    def from_dict(cls, value) -> "PendingRotation":
        _require(isinstance(value, dict), "pending_rotation")
        _require(_nonnegative_int(value.get("base_generation")),
                 "pending_rotation.base_generation")
        _require(isinstance(value.get("promotable"), bool), "pending_rotation.promotable")
        return cls(
            credentials=Credentials.from_dict(value.get("credentials")),
            base_generation=value["base_generation"],
            promotable=value["promotable"],
        )


@dataclasses.dataclass
class Profile:
    """One saved ChatGPT registration: a verified (issuer, subject) bound to
    the issued client ID it was authorized under. Email/name are display
    labels only -- never part of the registration key."""
    profile_id: str
    label: str
    issuer: str
    subject: str = dataclasses.field(repr=False)
    client_id: str = dataclasses.field(repr=False)
    email: Optional[str] = dataclasses.field(repr=False)
    display_name: Optional[str] = dataclasses.field(repr=False)
    status: str
    generation: int
    credentials: Optional[Credentials]
    pending_rotation: Optional[PendingRotation]
    last_error_class: Optional[str]
    created_at: float
    updated_at: float
    # Set (and persisted) before a refresh request leaves; cleared once the
    # outcome is known. While set, the vendor may have rotated the refresh
    # token without this store ever seeing the successor -- so a disconnect
    # must not claim the renewable session was revoked.
    refresh_outcome_unknown: bool = False
    # The vendor rejected this registration's issued client ID
    # (invalid_client): reconnecting must register a fresh client.
    registration_invalid: bool = False

    @property
    def registration_key(self) -> tuple:
        return (self.issuer, self.subject, self.client_id)

    def to_dict(self) -> dict:
        return {
            "profile_id": self.profile_id,
            "label": self.label,
            "issuer": self.issuer,
            "subject": self.subject,
            "client_id": self.client_id,
            "email": self.email,
            "display_name": self.display_name,
            "status": self.status,
            "generation": self.generation,
            "credentials": None if self.credentials is None else self.credentials.to_dict(),
            "pending_rotation": (None if self.pending_rotation is None
                                 else self.pending_rotation.to_dict()),
            "last_error_class": self.last_error_class,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "refresh_outcome_unknown": self.refresh_outcome_unknown,
            "registration_invalid": self.registration_invalid,
        }

    @classmethod
    def from_dict(cls, value) -> "Profile":
        _require(isinstance(value, dict), "profile")
        _require(isinstance(value.get("profile_id"), str)
                 and _PROFILE_ID_RE.match(value["profile_id"]) is not None, "profile.profile_id")
        _require(_label_ok(value.get("label")), "profile.label")
        _require(_nonempty_str(value.get("issuer")), "profile.issuer")
        _require(_nonempty_str(value.get("subject")), "profile.subject")
        _require(_client_id_ok(value.get("client_id")), "profile.client_id")
        for key in ("email", "display_name"):
            _require(value.get(key) is None or (isinstance(value[key], str) and len(value[key]) <= 320),
                     f"profile.{key}")
        _require(value.get("status") in PROFILE_STATUSES, "profile.status")
        _require(_nonnegative_int(value.get("generation")), "profile.generation")
        _require(value.get("last_error_class") is None or _nonempty_str(value.get("last_error_class")),
                 "profile.last_error_class")
        _require(_finite_number(value.get("created_at")), "profile.created_at")
        _require(_finite_number(value.get("updated_at")), "profile.updated_at")
        for key in ("refresh_outcome_unknown", "registration_invalid"):
            _require(isinstance(value.get(key, False), bool), f"profile.{key}")
        creds = value.get("credentials")
        pending = value.get("pending_rotation")
        profile = cls(
            profile_id=value["profile_id"],
            label=value["label"],
            issuer=value["issuer"],
            subject=value["subject"],
            client_id=value["client_id"],
            email=value.get("email"),
            display_name=value.get("display_name"),
            status=value["status"],
            generation=value["generation"],
            credentials=None if creds is None else Credentials.from_dict(creds),
            pending_rotation=None if pending is None else PendingRotation.from_dict(pending),
            last_error_class=value.get("last_error_class"),
            created_at=float(value["created_at"]),
            updated_at=float(value["updated_at"]),
            refresh_outcome_unknown=value.get("refresh_outcome_unknown", False),
            registration_invalid=value.get("registration_invalid", False),
        )
        # Structural invariants: token material only where a state allows it.
        if profile.status in (STATUS_DISCONNECTED, STATUS_DISCONNECTED_REMOTE_UNCONFIRMED,
                              STATUS_REAUTH_REQUIRED):
            _require(profile.credentials is None and profile.pending_rotation is None,
                     "profile: token material in a signed-out state")
        if profile.pending_rotation is not None:
            _require(profile.pending_rotation.base_generation == profile.generation
                     or not profile.pending_rotation.promotable,
                     "profile.pending_rotation generation")
        return profile


@dataclasses.dataclass
class PendingRegistration:
    """Legacy 01C field, read only for compatibility. R1 never writes or
    reuses an unverified first-registration client ID and clears old entries
    on the next sign-in."""
    registration_id: str
    client_id: str = dataclasses.field(repr=False)
    created_at: float

    def to_dict(self) -> dict:
        return {"registration_id": self.registration_id, "client_id": self.client_id,
                "created_at": self.created_at}

    @classmethod
    def from_dict(cls, value) -> "PendingRegistration":
        _require(isinstance(value, dict), "pending_registration")
        _require(isinstance(value.get("registration_id"), str)
                 and _PROFILE_ID_RE.match(value["registration_id"]) is not None,
                 "pending_registration.registration_id")
        _require(_client_id_ok(value.get("client_id")), "pending_registration.client_id")
        _require(_finite_number(value.get("created_at")), "pending_registration.created_at")
        return cls(value["registration_id"], value["client_id"], float(value["created_at"]))


@dataclasses.dataclass
class SessionDocument:
    revision: int
    active_profile_id: Optional[str]
    profiles: list
    pending_registrations: list

    @classmethod
    def empty(cls) -> "SessionDocument":
        return cls(revision=0, active_profile_id=None, profiles=[], pending_registrations=[])

    def profile(self, profile_id: Optional[str]) -> Optional[Profile]:
        for p in self.profiles:
            if p.profile_id == profile_id:
                return p
        return None

    def to_body(self) -> dict:
        return {
            "revision": self.revision,
            "active_profile_id": self.active_profile_id,
            "profiles": [p.to_dict() for p in self.profiles],
            "pending_registrations": [r.to_dict() for r in self.pending_registrations],
        }

    @classmethod
    def from_body(cls, body) -> "SessionDocument":
        _require(isinstance(body, dict), "body")
        _require(_nonnegative_int(body.get("revision")), "body.revision")
        profiles = body.get("profiles")
        regs = body.get("pending_registrations")
        _require(isinstance(profiles, list) and isinstance(regs, list), "body lists")
        doc = cls(
            revision=body["revision"],
            active_profile_id=body.get("active_profile_id"),
            profiles=[Profile.from_dict(p) for p in profiles],
            pending_registrations=[PendingRegistration.from_dict(r) for r in regs],
        )
        doc.validate()
        return doc

    def validate(self) -> None:
        ids = [p.profile_id for p in self.profiles]
        _require(len(ids) == len(set(ids)), "duplicate profile_id")
        labels = [p.label for p in self.profiles]
        _require(len(labels) == len(set(labels)), "duplicate label")
        # A client ID is bound by the vendor to one user+workspace: two
        # profiles holding the same one would be the same registration.
        clients = [p.client_id for p in self.profiles] + [r.client_id for r in self.pending_registrations]
        _require(len(clients) == len(set(clients)), "duplicate client_id")
        reg_ids = [r.registration_id for r in self.pending_registrations]
        _require(len(reg_ids) == len(set(reg_ids)), "duplicate registration_id")
        _require(self.active_profile_id is None or self.active_profile_id in ids,
                 "active_profile_id")


def _require(condition: bool, what: str) -> None:
    if not condition:
        raise StoreCorrupt("schema", f"ChatGPT session store failed validation ({what}).")


def _nonempty_str(value) -> bool:
    return isinstance(value, str) and value != ""


def _finite_number(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and value == value and value not in (float("inf"), float("-inf")))


def _nonnegative_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _client_id_ok(value) -> bool:
    return (isinstance(value, str) and _CLIENT_ID_RE.match(value) is not None
            and value != BOOTSTRAP_CLIENT_ID)


def _label_ok(value) -> bool:
    return (isinstance(value, str) and value.strip() != "" and len(value) <= 120
            and not any(ord(ch) < 0x20 for ch in value))


def new_record_id() -> str:
    return uuid.uuid4().hex


def _canonical(body: dict) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


# ---------------------------------------------------------------------------
# Locks
# ---------------------------------------------------------------------------

if os.name == "nt":  # pragma: no cover - exercised only on Windows
    import msvcrt

    def _try_lock(fd: int) -> bool:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class SessionStore:
    """File custody for one store directory. Holds no token material in
    memory between calls; every caller re-reads under the lock."""

    def __init__(self, directory: Optional[str] = None,
                 lock_timeout: float = DEFAULT_LOCK_TIMEOUT):
        self.directory = os.path.abspath(directory or default_store_dir())
        self.lock_timeout = lock_timeout
        self._held = threading.local()

    # -- paths ---------------------------------------------------------------

    @property
    def sessions_path(self) -> str:
        return os.path.join(self.directory, SESSIONS_FILENAME)

    @property
    def host_path(self) -> str:
        return os.path.join(self.directory, HOST_FILENAME)

    # -- directory -----------------------------------------------------------

    def _prepare_directory(self) -> None:
        refuse_if_production_path(self.directory)
        parent = os.path.dirname(self.directory)
        os.makedirs(parent, exist_ok=True)
        try:
            os.mkdir(self.directory, 0o700)
        except FileExistsError:
            pass
        info = os.lstat(self.directory)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise StoreUnsafe("directory_type", "ChatGPT session storage must be a private directory, not a link.")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise StoreUnsafe("directory_owner", "ChatGPT session storage must belong to the current user.")
        if os.name != "nt" and stat.S_IMODE(info.st_mode) != 0o700:
            # The directory is ours (owner verified above): tighten it. Files
            # inside are never tightened silently -- a loose file means
            # something other than this module wrote it.
            os.chmod(self.directory, 0o700)

    # -- locking -------------------------------------------------------------

    @contextlib.contextmanager
    def _lock(self, filename: str, timeout: Optional[float]) -> Iterator[None]:
        held = getattr(self._held, "names", None)
        if held is None:
            held = self._held.names = set()
        if filename in held:
            raise LockDiscipline("reentrant", "ChatGPT session lock re-entered in one thread.")
        if filename == REFRESH_LOCK_FILENAME and STORE_LOCK_FILENAME in held:
            raise LockDiscipline("lock_order", "Refresh lock requested while holding the store lock.")
        self._prepare_directory()
        path = os.path.join(self.directory, filename)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(path, flags, 0o600)
        try:
            deadline = time.monotonic() + (self.lock_timeout if timeout is None else timeout)
            while not _try_lock(fd):
                if time.monotonic() >= deadline:
                    raise StoreBusy("lock_timeout",
                                    "Another Lumina process is updating the ChatGPT connection.")
                time.sleep(_LOCK_POLL_SECONDS)
            held.add(filename)
            try:
                yield
            finally:
                held.discard(filename)
                _unlock(fd)
        finally:
            os.close(fd)

    def locked(self, timeout: Optional[float] = None):
        """Store lock: wraps every read-modify-write of sessions.json."""
        return self._lock(STORE_LOCK_FILENAME, timeout)

    def refresh_locked(self, timeout: Optional[float] = None):
        """Refresh lock: held across a refresh's network round trip and by
        disconnect/reconnect while they settle. Acquire BEFORE the store lock."""
        return self._lock(REFRESH_LOCK_FILENAME, timeout)

    def _assert_store_lock(self) -> None:
        if STORE_LOCK_FILENAME not in (getattr(self._held, "names", None) or ()):
            raise LockDiscipline("unlocked", "ChatGPT session store accessed without its lock.")

    # -- raw file I/O --------------------------------------------------------

    def _read_private_json(self, path: str):
        """Returns the parsed object, or None if the file does not exist."""
        refuse_if_production_path(path)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            return None
        except OSError as exc:
            # ELOOP: a symlink was planted at the store path.
            raise StoreUnsafe("open", "ChatGPT session file could not be opened safely.") from exc
        try:
            info = os.fstat(fd)
            self._check_private_file(info)
            if info.st_size > MAX_FILE_BYTES:
                raise StoreCorrupt("size", "ChatGPT session file is unexpectedly large.")
            chunks = []
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            os.close(fd)
        try:
            return json.loads(b"".join(chunks).decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise StoreCorrupt("parse", "ChatGPT session file is unreadable (truncated or damaged).") from exc

    @staticmethod
    def _check_private_file(info: os.stat_result) -> None:
        if not stat.S_ISREG(info.st_mode):
            raise StoreUnsafe("file_type", "ChatGPT session file is not a regular file.")
        if info.st_nlink != 1:
            raise StoreUnsafe("hardlink", "ChatGPT session file has another name (hard link); refusing to use it.")
        if os.name != "nt":
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                raise StoreUnsafe("file_owner", "ChatGPT session file must belong to the current user.")
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise StoreUnsafe("file_mode", "ChatGPT session file must be owner-only (0600).")

    def _write_private_json(self, path: str, value: dict) -> None:
        refuse_if_production_path(path)
        data = json.dumps(value, sort_keys=True, indent=1).encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise StoreCorrupt("size", "ChatGPT session document exceeds the supported size.")
        self._remove_stale_temps()
        tmp = os.path.join(self.directory, f"{_TEMP_PREFIX}{uuid.uuid4().hex}")
        flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_CLOEXEC", 0))
        fd = os.open(tmp, flags, 0o600)
        try:
            try:
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
            # A hard link to the live file would keep this generation's tokens
            # reachable under another name after the replace: refuse instead.
            try:
                current = os.lstat(path)
            except FileNotFoundError:
                current = None
            if current is not None:
                self._check_private_file(current)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        if os.name != "nt":
            # The new document is already in place; failing to fsync the
            # directory weakens durability but must not report the write as
            # failed (callers would undo state that was in fact persisted).
            with contextlib.suppress(OSError):
                dfd = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)

    def _remove_stale_temps(self) -> None:
        """Called only under the store lock, so no other writer's temp file
        can be live: anything matching is a crash leftover and may contain
        tokens."""
        for name in os.listdir(self.directory):
            if name.startswith(_TEMP_PREFIX):
                with contextlib.suppress(OSError):
                    os.unlink(os.path.join(self.directory, name))

    # -- public document API (store lock required) ---------------------------

    def read(self) -> SessionDocument:
        self._assert_store_lock()
        raw = self._read_private_json(self.sessions_path)
        if raw is None:
            return SessionDocument.empty()
        if not isinstance(raw, dict) or raw.get("schema") != SESSIONS_SCHEMA:
            raise StoreCorrupt("schema", "ChatGPT session file has an unsupported format.")
        if raw.get("protection") != PROTECTION:
            raise StoreCorrupt("protection", "ChatGPT session file uses an unsupported protection provider.")
        body = raw.get("body")
        digest = raw.get("sha256")
        if not isinstance(body, dict) or not isinstance(digest, str):
            raise StoreCorrupt("schema", "ChatGPT session file is incomplete.")
        if hashlib.sha256(_canonical(body)).hexdigest() != digest:
            raise StoreCorrupt("checksum", "ChatGPT session file failed its integrity check.")
        return SessionDocument.from_body(body)

    def write(self, doc: SessionDocument) -> None:
        self._assert_store_lock()
        doc.validate()
        doc.revision += 1
        body = doc.to_body()
        # Round-trip through the strict parser before anything touches disk.
        SessionDocument.from_body(json.loads(json.dumps(body)))
        self._write_private_json(self.sessions_path, {
            "schema": SESSIONS_SCHEMA,
            "protection": PROTECTION,
            "body": body,
            "sha256": hashlib.sha256(_canonical(body)).hexdigest(),
        })

    def get_or_create_host_id(self) -> str:
        """This installation's stable ext_agent_host_id (urn:uuid:<uuid4>,
        a vendor-supported format). Created once, before the first sign-in;
        never regenerated if the file later turns out damaged -- that is
        reported, because a silent new host ID would split this host's usage
        attribution."""
        self._assert_store_lock()
        raw = self._read_private_json(self.host_path)
        if raw is None:
            host_id = f"urn:uuid:{uuid.uuid4()}"
            self._write_private_json(self.host_path, {"schema": HOST_SCHEMA, "ext_agent_host_id": host_id})
            return host_id
        if (not isinstance(raw, dict) or raw.get("schema") != HOST_SCHEMA
                or not isinstance(raw.get("ext_agent_host_id"), str)
                or _HOST_ID_RE.match(raw["ext_agent_host_id"]) is None):
            raise StoreCorrupt("host_id", "This installation's ChatGPT host identity file is damaged.")
        return raw["ext_agent_host_id"]

    def file_identities(self) -> list:
        """(st_dev, st_ino) of the live session/host files, for Agent
        Backup's forbidden-identity set. Metadata only; never opens them."""
        found = []
        for path in (self.sessions_path, self.host_path):
            try:
                st = os.stat(path)
            except OSError:
                continue
            found.append((st.st_dev, st.st_ino))
        return found
