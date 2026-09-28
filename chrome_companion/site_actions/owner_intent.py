"""Dormant trusted intake and exact-owner grammar; no agent integration yet.

Only a service-owned intake with its actual owner agent can issue admission
handles. Data classes, parsed commands and asserted provenance are not grants.
The data-root identity pin is an explicit setup action, never a restart repair.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
import secrets
import threading
import time

from chrome_companion import state
from . import claims
from .model import (ActionSpec, Consequence, ContractError, DestinationIdentity,
                    FrozenPayload, MAX_JSON_BYTES, Runtime, canonical_json, decode_json)
from .registry import load_manifest


LIFETIME_NS = 300_000_000_000
PREFIX = "/companion-action "


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def text(value: object) -> str:
    if type(value) is not str or not value:
        raise ContractError("invalid trusted identity/text")
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeError as exc:
        raise ContractError("invalid trusted Unicode") from exc
    if size > MAX_JSON_BYTES:
        raise ContractError("oversize trusted identity/text")
    return value


def parse_exact(raw: str) -> ActionSpec:
    """Pure parsing, never admission. No coercion or payload normalization."""
    text(raw)
    if not raw.startswith(PREFIX):
        raise ContractError("exact owner command required")
    data = decode_json(raw[len(PREFIX):])
    if type(data) is not dict or set(data) != {"v", "runtime", "capability", "destination", "payload"}:
        raise ContractError("invalid command fields")
    if type(data["v"]) is not int or data["v"] != 1 or data["runtime"] != Runtime.CHROME_COMPANION.value:
        raise ContractError("invalid command version/runtime")
    destination = data["destination"]
    if type(destination) is not dict or set(destination) != {"kind", "id"}:
        raise ContractError("invalid command destination")
    manifest = load_manifest(data["capability"])
    return ActionSpec(manifest, Runtime.CHROME_COMPANION,
                      DestinationIdentity(manifest.namespace, destination["kind"], destination["id"]),
                      FrozenPayload.from_data(data["payload"]), Consequence.EXTERNAL_COMMIT)


def pin_store_identity(data_dir, expected: claims.StoreIdentity) -> None:
    """Explicit setup: pin an already initialized identity outside Companion.

    No overwrite/adoption. Losing or uncertain setup requires owner recovery;
    this function is disconnected from startup, installer and agent ingress.
    """
    with claims.ClaimStore(data_dir, expected):
        pass
    fd = state.open_trusted_dir(data_dir, create=False)
    try:
        raw = canonical_json(expected.to_data())
        if not claims._exclusive_write(fd, claims.REFERENCE_FILE, raw):
            raise claims.ClaimStoreError("trusted identity pin already exists or is obstructed")
    finally:
        os.close(fd)


def reopen_claim_store(data_dir) -> claims.ClaimStore:
    """Restart reads only the trusted data-root pin; never initializes state."""
    fd = state.open_trusted_dir(data_dir, create=False)
    try:
        raw, _inode = claims._read(fd, claims.REFERENCE_FILE)
        expected = claims.StoreIdentity.from_data(decode_json(raw))
        if raw != canonical_json(expected.to_data()):
            raise claims.ClaimStoreError("noncanonical trusted identity pin")
        store = claims.ClaimStore(data_dir, expected)
        try:
            # Re-read through the held root, then rewalk the supplied root.
            after, after_inode = claims._read(fd, claims.REFERENCE_FILE)
            fresh = state.open_trusted_dir(data_dir, create=False)
            try:
                current, current_inode = claims._read(fresh, claims.REFERENCE_FILE)
                if (os.fstat(fd).st_dev, os.fstat(fd).st_ino) != (os.fstat(fresh).st_dev, os.fstat(fresh).st_ino):
                    raise claims.ClaimStoreError("trusted identity root replaced")
                if raw != after or raw != current or _inode != after_inode or _inode != current_inode:
                    raise claims.ClaimStoreError("trusted identity pin replaced")
            finally:
                os.close(fresh)
        except Exception:
            store.close()
            raise
        return store
    finally:
        os.close(fd)


@dataclass(frozen=True, slots=True)
class OwnerAdmission:
    identifier: str


@dataclass(frozen=True, slots=True)
class AdmittedIntent:
    key: claims.EventKey
    metadata: claims.AdmissionMetadata
    scope_digest: str
    specification: ActionSpec | None


class OwnerIntake:
    """One trusted agent/session/task intake. No ambient/global guard.

    Construction belongs to the service. The agent object, provenance and raw
    event ID must come from authenticated ingress in B1.4, never tool arguments.
    B1.3 tests inject fake service agents. No production caller exists here.
    """

    def __init__(self, agent, store: claims.ClaimStore, *, session: str, task: str,
                 channel: str, chat: str, authority_domain: str, clock=time.monotonic_ns):
        if type(store) is not claims.ClaimStore or getattr(agent, "owner", None) is not True:
            raise ContractError("actual owner agent and initialized store required")
        self._agent = agent
        self._process = os.getpid()
        self.store = store
        self._domain = text(authority_domain)
        self.scope_digest = digest({"session": text(session), "task": text(task),
                                    "channel": text(channel), "chat": text(chat)})
        self.boot_digest = digest(secrets.token_hex(32))
        self._clock = clock
        self._admissions = {}
        self._review_only = set()
        self._lock = threading.RLock()
        self._alive = True

    def now(self) -> int:
        value = self._clock()
        claims._number(value)
        return value

    def _owner(self, agent) -> None:
        if (not self._alive or os.getpid() != self._process or agent is not self._agent
                or getattr(agent, "owner", None) is not True):
            raise ContractError("owner intake retired or wrong agent")

    def capture(self, agent, *, source: str, raw_text: str, event_id: str) -> OwnerAdmission:
        with self._lock:
            self._owner(agent)
            if type(source) is not str or source != "OWNER_DIRECT":
                raise ContractError("raw OWNER_DIRECT ingress required")
            text(raw_text)
            text(event_id)
            # Malformed command syntax does not become a generated-review task.
            specification = parse_exact(raw_text) if raw_text.startswith("/companion-action") else None
            key = claims.EventKey.from_ingress(self._domain, event_id)
            now = self.now()
            metadata = claims.AdmissionMetadata(digest(raw_text), self.boot_digest, now, now + LIFETIME_NS)
            if not self.store.admit(key, metadata):
                raise ContractError("event already admitted; replay cannot renew authority")
            handle = OwnerAdmission(secrets.token_hex(32))
            self._admissions[handle.identifier] = (handle, AdmittedIntent(key, metadata, self.scope_digest, specification))
            return handle

    def resolve(self, handle: OwnerAdmission) -> AdmittedIntent:
        with self._lock:
            self._owner(self._agent)
            if type(handle) is not OwnerAdmission:
                raise ContractError("trusted admission handle required")
            entry = self._admissions.get(handle.identifier)
            if entry is None or entry[0] is not handle:
                raise ContractError("unknown/copied/cross-intake admission")
            admitted = entry[1]
            if not admitted.metadata.admitted_ns <= self.now() < admitted.metadata.expires_ns:
                raise ContractError("original admission expired")
            return admitted

    def _review_event(self, snapshot_digest: str, expires_ns: int, event_id: str) -> AdmittedIntent:
        with self._lock:
            self._owner(self._agent)
            now = self.now()
            if now >= expires_ns:
                raise ContractError("review expired")
            key = claims.EventKey.from_ingress(self._domain + "/review", text(event_id))
            metadata = claims.AdmissionMetadata(snapshot_digest, self.boot_digest, now, expires_ns)
            if not self.store.admit(key, metadata):
                raise ContractError("review event replay")
            return AdmittedIntent(key, metadata, self.scope_digest, None)

    def resolve_direct(self, handle: OwnerAdmission) -> AdmittedIntent:
        with self._lock:
            admitted = self.resolve(handle)
            if handle.identifier in self._review_only:
                raise ContractError("revised task requires its replacement owner review")
            return admitted

    def _require_review(self, handle: OwnerAdmission) -> None:
        with self._lock:
            self.resolve(handle)
            self._review_only.add(handle.identifier)

    def retire(self) -> None:
        with self._lock:
            self._alive = False
            self._admissions.clear()
            self._review_only.clear()

    def invalidate(self) -> None:
        """Drop pending raw-turn handles; durable admissions are never released."""
        with self._lock:
            self._admissions.clear()
            self._review_only.clear()
