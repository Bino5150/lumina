"""BC-01C-B1.2: immutable event records and irreversible commit consumption.

Linux/POSIX only. Initialization is an explicit, disconnected setup operation.
Opening requires the identity returned by that setup; it never creates or
repairs state. This module stores digests and bounded metadata, not authority
or payloads. A durable consumption winner is NOT permission to dispatch: the
future kernel must still authenticate the event and verify its live scope.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import os
import re
import secrets
import stat
import threading

from chrome_companion import state
from .model import ContractError, Runtime, canonical_json, decode_json


STORE_DIR = "site_action_claims"
REFERENCE_FILE = "site_action_store.json"
IDENTITY_FILE = "identity.json"
VERSION = 1
MAX_RECORD_BYTES = 2048
_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class ClaimStoreError(ValueError):
    """Missing, conflicting, uncertain or insecure immutable claim state."""


class RecordKind(str, Enum):
    ADMISSION = "admission"
    BINDING = "binding"
    SPENT = "spent"


def _digest(value: object) -> None:
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ClaimStoreError("invalid digest")


def _number(value: object) -> None:
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ClaimStoreError("invalid bounded integer")


def _fields(data: object, keys: set[str]) -> dict:
    if type(data) is not dict or set(data) != keys:
        raise ClaimStoreError("missing or unknown record fields")
    return data


@dataclass(frozen=True, slots=True)
class EventKey:
    digest: str

    def __post_init__(self) -> None:
        _digest(self.digest)

    @classmethod
    def from_ingress(cls, authority_domain: str, event_id: str) -> EventKey:
        """Stable transport event slot. No action/payload/random-ID component.

        These inputs must eventually come from trusted ingress. Hashing an
        assertion does not authenticate it. Original inputs are not persisted.
        """
        for value in (authority_domain, event_id):
            if type(value) is not str or not value or len(value) > 1024:
                raise ClaimStoreError("invalid ingress identity")
        try:
            encoded = canonical_json({"namespace": "site_action_event_v1",
                                      "authority_domain": authority_domain, "event_id": event_id})
        except ContractError as exc:
            raise ClaimStoreError("invalid ingress identity encoding") from exc
        return cls(hashlib.sha256(encoded).hexdigest())


@dataclass(frozen=True, slots=True)
class StoreIdentity:
    epoch: str
    companion_device: int
    companion_inode: int
    store_device: int
    store_inode: int

    def __post_init__(self) -> None:
        _digest(self.epoch)
        for value in (self.companion_device, self.companion_inode, self.store_device, self.store_inode):
            _number(value)

    def to_data(self) -> dict:
        return {"v": VERSION, "epoch": self.epoch, "companion_device": self.companion_device,
                "companion_inode": self.companion_inode, "store_device": self.store_device,
                "store_inode": self.store_inode}

    @classmethod
    def from_data(cls, value: object) -> StoreIdentity:
        data = _fields(value, {"v", "epoch", "companion_device", "companion_inode", "store_device", "store_inode"})
        if type(data["v"]) is not int or data["v"] != VERSION:
            raise ClaimStoreError("unsupported store version")
        return cls(data["epoch"], data["companion_device"], data["companion_inode"],
                   data["store_device"], data["store_inode"])


@dataclass(frozen=True, slots=True)
class AdmissionMetadata:
    intent_digest: str
    boot_digest: str
    admitted_ns: int
    expires_ns: int

    def __post_init__(self) -> None:
        _digest(self.intent_digest)
        _digest(self.boot_digest)
        _number(self.admitted_ns)
        _number(self.expires_ns)
        if self.expires_ns < self.admitted_ns:
            raise ClaimStoreError("invalid original admission lifetime")

    def to_data(self) -> dict:
        return {"intent_digest": self.intent_digest, "boot_digest": self.boot_digest,
                "admitted_ns": self.admitted_ns, "expires_ns": self.expires_ns}


@dataclass(frozen=True, slots=True)
class BindingMetadata:
    specification_digest: str
    manifest_fingerprint: str
    principal_digest: str
    context_digest: str
    runtime: Runtime

    def __post_init__(self) -> None:
        for value in (self.specification_digest, self.manifest_fingerprint, self.principal_digest, self.context_digest):
            _digest(value)
        if type(self.runtime) is not Runtime:
            raise ClaimStoreError("invalid runtime metadata")

    def to_data(self) -> dict:
        return {"specification_digest": self.specification_digest, "manifest_fingerprint": self.manifest_fingerprint,
                "principal_digest": self.principal_digest, "context_digest": self.context_digest,
                "runtime": self.runtime.value}


@dataclass(frozen=True, slots=True)
class ClaimRecord:
    epoch: str
    event_key: EventKey
    kind: RecordKind
    parent_digest: str | None
    metadata: AdmissionMetadata | BindingMetadata | None

    def __post_init__(self) -> None:
        _digest(self.epoch)
        if type(self.event_key) is not EventKey or type(self.kind) is not RecordKind:
            raise ClaimStoreError("invalid record identity")
        if self.kind is RecordKind.ADMISSION:
            if self.parent_digest is not None or type(self.metadata) is not AdmissionMetadata:
                raise ClaimStoreError("invalid admission record")
        else:
            _digest(self.parent_digest)
            if self.kind is RecordKind.BINDING and type(self.metadata) is not BindingMetadata:
                raise ClaimStoreError("invalid binding record")
            if self.kind is RecordKind.SPENT and self.metadata is not None:
                raise ClaimStoreError("spent record cannot contain additional data")

    def to_data(self) -> dict:
        return {"v": VERSION, "epoch": self.epoch, "event_key": self.event_key.digest,
                "kind": self.kind.value, "parent_digest": self.parent_digest,
                "metadata": None if self.metadata is None else self.metadata.to_data()}

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json(self.to_data())

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()

    @classmethod
    def from_data(cls, value: object) -> ClaimRecord:
        data = _fields(value, {"v", "epoch", "event_key", "kind", "parent_digest", "metadata"})
        if type(data["v"]) is not int or data["v"] != VERSION or type(data["kind"]) is not str:
            raise ClaimStoreError("unsupported claim record")
        try:
            kind = RecordKind(data["kind"])
            if kind is RecordKind.ADMISSION:
                fields = _fields(data["metadata"], {"intent_digest", "boot_digest", "admitted_ns", "expires_ns"})
                metadata = AdmissionMetadata(**fields)
            elif kind is RecordKind.BINDING:
                fields = _fields(data["metadata"], {"specification_digest", "manifest_fingerprint",
                                                   "principal_digest", "context_digest", "runtime"})
                if type(fields["runtime"]) is not str:
                    raise ClaimStoreError("invalid runtime metadata")
                metadata = BindingMetadata(fields["specification_digest"], fields["manifest_fingerprint"],
                                           fields["principal_digest"], fields["context_digest"], Runtime(fields["runtime"]))
            else:
                metadata = data["metadata"]
            return cls(data["epoch"], EventKey(data["event_key"]), kind, data["parent_digest"], metadata)
        except ValueError as exc:
            raise ClaimStoreError("invalid claim record") from exc


def _directory(fd: int, device: int, inode: int) -> None:
    info = os.fstat(fd)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700 or (info.st_dev, info.st_ino) != (device, inode)):
        raise ClaimStoreError("private store directory identity changed")


def _file(fd: int) -> os.stat_result:
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
            or not 0 <= info.st_size <= MAX_RECORD_BYTES):
        raise ClaimStoreError("insecure or oversized immutable record")
    return info


def _entry(dir_fd: int, name: str, fd: int) -> os.stat_result:
    info = _file(fd)
    named = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    if not stat.S_ISREG(named.st_mode) or (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino):
        raise ClaimStoreError("immutable record name changed")
    return info


def _stamp(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _leaf(name: str) -> None:
    if type(name) is not str or not re.fullmatch(
            r"(?:identity\.json|site_action_store\.json|[0-9a-f]{64}\.(?:admission|binding|spent)\.json)", name):
        raise ClaimStoreError("invalid immutable record leaf")


def _read(dir_fd: int, name: str) -> tuple[bytes, tuple]:
    _leaf(name)
    fd = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    try:
        before = _entry(dir_fd, name, fd)
        chunks, size = [], 0
        while size <= MAX_RECORD_BYTES:
            chunk = os.read(fd, min(512, MAX_RECORD_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        after = _entry(dir_fd, name, fd)
        if size > MAX_RECORD_BYTES or size != before.st_size or _stamp(before) != _stamp(after):
            raise ClaimStoreError("immutable record changed while reading")
        return b"".join(chunks), (after.st_dev, after.st_ino)
    finally:
        os.close(fd)


def _exclusive_write(dir_fd: int, name: str, raw: bytes) -> bool:
    """No replacement/cleanup. Any existing name obstructs a new creation."""
    _leaf(name)
    directory = os.fstat(dir_fd)
    _directory(dir_fd, directory.st_dev, directory.st_ino)
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_RECORD_BYTES:
        raise ClaimStoreError("invalid record bytes")
    try:
        fd = os.open(name, _WRITE_FLAGS, 0o600, dir_fd=dir_fd)
    except FileExistsError:
        return False
    try:
        _entry(dir_fd, name, fd)
        view = memoryview(raw)
        while view:
            try:
                written = os.write(fd, view)
            except InterruptedError:
                continue
            if type(written) is not int or not 0 < written <= len(view):
                raise ClaimStoreError("incomplete immutable record write")
            view = view[written:]
        if _entry(dir_fd, name, fd).st_size != len(raw):
            raise ClaimStoreError("incomplete immutable record")
        os.fsync(fd)
        os.fsync(dir_fd)
        _entry(dir_fd, name, fd)
        return True
    finally:
        os.close(fd)


def initialize_store(data_dir) -> StoreIdentity:
    """Explicit one-time setup while actions are disabled; never auto-repairs.

    A failed/partial setup remains an obstruction and returns no identity.
    The owner data directory must already exist. Setup creates only its
    private Companion/store children and syncs each created directory entry.
    Installer/product integration is deliberately absent at this checkpoint.
    """
    data_fd = -1
    companion_fd = -1
    store_fd = -1
    try:
        data_fd = state.open_trusted_dir(data_dir, create=False)
        companion_fd = state.open_private_subdir(data_fd, state.COMPANION_DIRNAME, create=True,
                                                path=state.COMPANION_DIRNAME)
        os.fsync(data_fd)  # the Companion entry must be durable in its parent
        parent = os.fstat(companion_fd)
        _directory(companion_fd, parent.st_dev, parent.st_ino)
        # Refuse even a dangling reference; nothing is deleted or overwritten.
        try:
            os.stat(REFERENCE_FILE, dir_fd=companion_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ClaimStoreError("store already initialized or obstructed")
        os.mkdir(STORE_DIR, 0o700, dir_fd=companion_fd)
        store_fd = state.open_private_subdir(companion_fd, STORE_DIR, create=False, path=STORE_DIR)
        info = os.fstat(store_fd)
        _directory(store_fd, info.st_dev, info.st_ino)
        identity = StoreIdentity(secrets.token_hex(32), parent.st_dev, parent.st_ino, info.st_dev, info.st_ino)
        raw = canonical_json(identity.to_data())
        if not _exclusive_write(store_fd, IDENTITY_FILE, raw):
            raise ClaimStoreError("store identity obstructed")
        if not _exclusive_write(companion_fd, REFERENCE_FILE, raw):
            raise ClaimStoreError("store reference obstructed")
        # Rewalk the supplied path after all mandatory syncs before publishing.
        with ClaimStore(data_dir, identity):
            pass
        return identity
    except (OSError, state.StateError, ContractError) as exc:
        raise ClaimStoreError("store initialization failed; retained obstruction") from exc
    finally:
        if store_fd >= 0:
            os.close(store_fd)
        if companion_fd >= 0:
            os.close(companion_fd)
        if data_fd >= 0:
            os.close(data_fd)


class ClaimStore:
    """Descriptor-held immutable storage; opening creates nothing.

    False from admit/bind means identical existing data, never a new event.
    False from consume means any occupied spent name (valid or not). No
    method re-arms authority, refreshes lifetime, deletes or repairs a record.
    """

    def __init__(self, data_dir, expected: StoreIdentity):
        if type(expected) is not StoreIdentity:
            raise ClaimStoreError("explicit initialized identity required")
        self.identity = expected
        self._data_dir = state.absolute_path(data_dir)
        self._companion_fd = -1
        self._store_fd = -1
        self._lock = threading.RLock()
        self._poisoned = False
        self._reference_inode = None
        self._identity_inode = None
        try:
            self._companion_fd = state.open_companion_dir(self._data_dir, create=False)
            self._store_fd = state.open_private_subdir(self._companion_fd, STORE_DIR, create=False, path=STORE_DIR)
            self._check_live()
        except (OSError, state.StateError, ContractError, ClaimStoreError) as exc:
            self.close()
            raise ClaimStoreError("cannot open initialized claim store") from exc

    def __enter__(self) -> ClaimStore:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            for attr in ("_store_fd", "_companion_fd"):
                fd = getattr(self, attr)
                if fd >= 0:
                    os.close(fd)
                    setattr(self, attr, -1)

    def _check_live(self) -> None:
        if self._poisoned or self._store_fd < 0 or self._companion_fd < 0:
            raise ClaimStoreError("claim store closed or uncertain")
        identity = self.identity
        _directory(self._companion_fd, identity.companion_device, identity.companion_inode)
        _directory(self._store_fd, identity.store_device, identity.store_inode)
        fresh_parent = state.open_companion_dir(self._data_dir, create=False)
        fresh_store = -1
        try:
            _directory(fresh_parent, identity.companion_device, identity.companion_inode)
            fresh_store = state.open_private_subdir(fresh_parent, STORE_DIR, create=False, path=STORE_DIR)
            _directory(fresh_store, identity.store_device, identity.store_inode)
            expected_bytes = canonical_json(identity.to_data())
            reference, reference_inode = _read(self._companion_fd, REFERENCE_FILE)
            marker, identity_inode = _read(self._store_fd, IDENTITY_FILE)
            for raw in (reference, marker):
                parsed = StoreIdentity.from_data(decode_json(raw))
                if parsed != identity or raw != expected_bytes:
                    raise ClaimStoreError("initialized identity record changed")
            if self._reference_inode is not None and self._reference_inode != reference_inode:
                raise ClaimStoreError("store reference replaced")
            if self._identity_inode is not None and self._identity_inode != identity_inode:
                raise ClaimStoreError("store marker replaced")
            self._reference_inode = reference_inode
            self._identity_inode = identity_inode
        finally:
            if fresh_store >= 0:
                os.close(fresh_store)
            os.close(fresh_parent)

    def _name(self, key: EventKey, kind: RecordKind) -> str:
        if type(key) is not EventKey or type(kind) is not RecordKind:
            raise ClaimStoreError("invalid event slot")
        return key.digest + "." + kind.value + ".json"

    def _read_record(self, key: EventKey, kind: RecordKind) -> ClaimRecord:
        raw, _inode = _read(self._store_fd, self._name(key, kind))
        record = ClaimRecord.from_data(decode_json(raw))
        if record.event_key != key or record.epoch != self.identity.epoch or record.kind is not kind:
            raise ClaimStoreError("record belongs to another event/store/kind")
        if raw != record.canonical_bytes:
            raise ClaimStoreError("noncanonical immutable record")
        return record

    def _create(self, record: ClaimRecord, *, occupied_is_consumed: bool = False) -> bool:
        name = self._name(record.event_key, record.kind)
        created = _exclusive_write(self._store_fd, name, record.canonical_bytes)
        if not created and not occupied_is_consumed:
            if self._read_record(record.event_key, record.kind) != record:
                raise ClaimStoreError("event record conflict")
        self._check_live()
        return created

    def admit(self, key: EventKey, metadata: AdmissionMetadata) -> bool:
        with self._lock:
            try:
                self._check_live()
                record = ClaimRecord(self.identity.epoch, key, RecordKind.ADMISSION, None, metadata)
                return self._create(record)
            except (OSError, state.StateError, ContractError, ClaimStoreError) as exc:
                self._poisoned = True
                raise ClaimStoreError("admission refused; no record is removed") from exc

    def bind(self, key: EventKey, metadata: BindingMetadata) -> bool:
        with self._lock:
            try:
                self._check_live()
                admission = self._read_record(key, RecordKind.ADMISSION)
                record = ClaimRecord(self.identity.epoch, key, RecordKind.BINDING, admission.digest, metadata)
                return self._create(record)
            except (OSError, state.StateError, ContractError, ClaimStoreError) as exc:
                self._poisoned = True
                raise ClaimStoreError("binding refused; no record is removed") from exc

    def consume(self, key: EventKey, expected_binding: BindingMetadata) -> bool:
        """One durable winner, not an actuator/permit. Existing spent always denies."""
        with self._lock:
            try:
                self._check_live()
                admission = self._read_record(key, RecordKind.ADMISSION)
                binding = self._read_record(key, RecordKind.BINDING)
                if (type(expected_binding) is not BindingMetadata or binding.metadata != expected_binding
                        or binding.parent_digest != admission.digest):
                    raise ClaimStoreError("binding conflict")
                record = ClaimRecord(self.identity.epoch, key, RecordKind.SPENT, binding.digest, None)
                return self._create(record, occupied_is_consumed=True)
            except (OSError, state.StateError, ContractError, ClaimStoreError) as exc:
                self._poisoned = True
                raise ClaimStoreError("consumption refused; any obstruction is retained") from exc

    def inspect(self, key: EventKey, kind: RecordKind) -> ClaimRecord:
        with self._lock:
            try:
                self._check_live()
                record = self._read_record(key, kind)
                self._check_live()
                return record
            except (OSError, state.StateError, ContractError, ClaimStoreError) as exc:
                raise ClaimStoreError("immutable record unavailable or invalid") from exc
