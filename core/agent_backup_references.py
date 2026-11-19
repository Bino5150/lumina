"""core/agent_backup_references.py -- database-to-file referential integrity
for generation artifacts (AGENT-BACKUP-RESTORE-PHASE-B, slice S1).

A generated image is two things: a `generation_artifacts` row in lumina.db
(artifact_id, local_path, sha256, size_bytes) and a blob under
DATA_DIR/artifacts/generation/<id[:2]>/<id>, often with a sibling
<id>.manifest.json. The exporter today archives the row (inside lumina.db)
but not the blob, so a restore keeps rows that point at nothing. Before a
Verified Agent Backup can capture those blobs it has to know, per row,
whether the file the row promises is really there and really the same
bytes. This module answers that and nothing else:

  missing_blob        the row's blob is absent                     (blocks)
  not_a_file          a link, directory or special file is there   (blocks)
  size_mismatch       on-disk size differs from size_bytes         (blocks)
  hash_mismatch       content differs from sha256 (verify_hashes)  (blocks)
  unsafe_id           artifact_id is not the 32-hex uuid ingest writes (blocks)
  unexpected_entry    anything else in the tree's directory layout (blocks)
  foreign_local_path  local_path is not this root's canonical path; a
                      restored copy must rebind it, never follow it
  orphan_file         a file no row references (unreachable in the app)
  tmp_leftover        an interrupted atomic-write temp file

The blob path is always derived from artifact_id, never from local_path:
local_path is absolute, so in a restored root it names the *source*
machine's folder, and following it would read across roots.

It reads a SNAPSHOT of lumina.db, never the live database. The snapshot is
opened `mode=ro&immutable=1`, so SQLite takes no lock and creates no -wal or
-shm beside it; making a consistent snapshot is the caller's job. Under the
data root nothing is followed: the generation tree is reached by an
O_NOFOLLOW descent from "/", each entry is lstat'ed relative to its
directory descriptor, and blob bytes are read only with verify_hashes=True,
through O_NOFOLLOW descriptors bound to the lstat that classified them.

Unknown never reads as clean: an empty snapshot, a generation_artifacts that
is not a table, or a snapshot without the table while the tree holds anything
raises ReferenceUnavailable. `ok` means no blocking finding; `verified_ok`
also requires that every captured blob was hashed against its row.

S3 wires it into the 2.2 exporter (what to capture, against a private
copy of the exact database bytes being archived) and into isolated Restore
(after rebinding, against the staged root with `expected_root` set to the
destination the root will be published as). It is still not wired to any
button.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import stat
import urllib.parse
from dataclasses import dataclass

from core import agent_backup_inventory as _inv
from core.test_isolation import refuse_if_production_path

MISSING_BLOB = "missing_blob"
NOT_A_FILE = "not_a_file"
SIZE_MISMATCH = "size_mismatch"
HASH_MISMATCH = "hash_mismatch"
UNSAFE_ID = "unsafe_id"
UNEXPECTED_ENTRY = "unexpected_entry"
FOREIGN_LOCAL_PATH = "foreign_local_path"
ORPHAN_FILE = "orphan_file"
TMP_LEFTOVER = "tmp_leftover"

BLOCKING = frozenset({MISSING_BLOB, NOT_A_FILE, SIZE_MISMATCH, HASH_MISMATCH,
                      UNSAFE_ID, UNEXPECTED_ENTRY})

GENERATION_REL = "artifacts/generation"
ARCHIVE_PREFIX = "state/"          # proposed member path: state/artifacts/generation/...
MANIFEST_SUFFIX = ".manifest.json"
MAX_ROWS = 100_000
MAX_ENTRIES = 250_000
MAX_BLOB_BYTES = 1 << 30
_ID = re.compile(r"[0-9a-f]{32}")
_SHARD = re.compile(r"[0-9a-f]{2}")
_SHA = re.compile(r"[0-9a-f]{64}")
# O_NONBLOCK: a FIFO swapped in after the lstat must not hang the open; the
# fstat identity + S_ISREG check below then refuses it.
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)


class ReferenceUnavailable(Exception):
    """The check could not observe what it needs. Never read as 'clean'."""


@dataclass(frozen=True)
class Finding:
    code: str
    artifact_id: str | None
    relpath: str | None


@dataclass(frozen=True)
class CaptureItem:
    relpath: str
    archive_path: str
    size: int
    sha256: str | None      # the row's hash for a blob; None for a manifest
    role: str               # "blob" | "manifest"


@dataclass(frozen=True)
class ReferenceReport:
    rows: int
    hashes_verified: bool
    findings: tuple
    capture: tuple

    @property
    def blockers(self) -> tuple:
        return tuple(f for f in self.findings if f.code in BLOCKING)

    @property
    def ok(self) -> bool:
        """No blocking finding. With hashes_verified False only sizes and
        the hash's format were checked; see verified_ok."""
        return not self.blockers

    @property
    def verified_ok(self) -> bool:
        """ok AND every captured blob's bytes were hashed against its row.
        This, not ok, is what a Verified backup may rely on."""
        return self.ok and self.hashes_verified


def blob_relpath(artifact_id: str) -> str:
    """Canonical data-root-relative blob path; mirrors
    core.generation_artifact._artifact_path without importing config."""
    return f"{GENERATION_REL}/{artifact_id[:2]}/{artifact_id}"


def _read_rows(db_snapshot: str):
    """-> rows, or None when the snapshot has no generation_artifacts table
    (it is created lazily, so absence is legitimate only with no tree)."""
    refuse_if_production_path(db_snapshot)
    try:
        st = os.lstat(db_snapshot)
    except OSError as exc:
        raise ReferenceUnavailable(f"database snapshot unavailable: {type(exc).__name__}") from None
    if not stat.S_ISREG(st.st_mode):
        raise ReferenceUnavailable("database snapshot is not a regular file")
    if st.st_size == 0:
        raise ReferenceUnavailable("database snapshot is empty")
    # immutable=1 ignores a -wal/-journal sidecar, so a snapshot with one
    # would be read stale. A snapshot must be a single self-contained file.
    for sidecar in ("-wal", "-journal"):
        if os.path.lexists(db_snapshot + sidecar):
            raise ReferenceUnavailable(f"database snapshot has a pending {sidecar[1:]}; "
                                       "it is not a self-contained snapshot")
    uri = "file:" + urllib.parse.quote(os.path.abspath(db_snapshot)) + "?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise ReferenceUnavailable(f"database snapshot unreadable: {type(exc).__name__}") from None
    try:
        kinds = {k for (k,) in conn.execute("SELECT type FROM sqlite_master "
                                            "WHERE name = 'generation_artifacts'")}
        if not kinds:
            return None
        if kinds != {"table"}:
            raise ReferenceUnavailable("generation_artifacts in the snapshot is not a table")
        rows = conn.execute("SELECT artifact_id, local_path, sha256, size_bytes "
                            "FROM generation_artifacts ORDER BY artifact_id "
                            "LIMIT ?", (MAX_ROWS + 1,)).fetchall()
    except sqlite3.Error as exc:
        raise ReferenceUnavailable(f"database snapshot unreadable: {type(exc).__name__}") from None
    finally:
        conn.close()
    if len(rows) > MAX_ROWS:
        raise ReferenceUnavailable("generation_artifacts exceeds the check's bounds")
    # A shipped table keys artifact_id; duplicates mean a crafted table, and
    # one path cannot verify two rows (S3 review S3-07).
    if len({r[0] for r in rows}) != len(rows):
        raise ReferenceUnavailable("generation_artifacts holds duplicate artifact ids")
    return rows


def _hash_entry(name, dir_fd, expected) -> str:
    fd = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
    try:
        st = os.fstat(fd)
        if ((st.st_dev, st.st_ino) != (expected.st_dev, expected.st_ino)
                or not stat.S_ISREG(st.st_mode)):
            raise ReferenceUnavailable("a blob changed during the check")
        h, remaining = hashlib.sha256(), MAX_BLOB_BYTES
        while chunk := os.read(fd, 1 << 20):
            remaining -= len(chunk)
            if remaining < 0:
                raise ReferenceUnavailable("a blob exceeds the check's bounds")
            h.update(chunk)
        return h.hexdigest()
    finally:
        os.close(fd)


def _bounded_names(fd, rel, counter) -> list:
    """Sorted names in one directory, counted against MAX_ENTRIES while they
    are read, so an oversized directory is refused before it is held."""
    names = []
    try:
        with os.scandir(fd) as it:
            for entry in it:
                counter[0] += 1
                if counter[0] > MAX_ENTRIES:
                    raise ReferenceUnavailable("generation tree exceeds the check's bounds")
                names.append(entry.name)
    except OSError as exc:
        raise ReferenceUnavailable(f"cannot observe {rel}: {type(exc).__name__}") from None
    return sorted(names)


def _walk_generation(data_dir: str, wanted: dict, verify_hashes: bool):
    """-> (present {relpath: (kind, size, sha|None)}, unexpected [relpath])."""
    present, unexpected, count = {}, [], [0]
    try:
        fd = _inv._open_root(data_dir)
    except _inv.InventoryUnavailable as exc:
        raise ReferenceUnavailable(str(exc)) from None
    try:
        for part in GENERATION_REL.split("/"):
            try:
                st = os.lstat(part, dir_fd=fd)
            except FileNotFoundError:
                return present, unexpected          # no generation tree at all
            except OSError as exc:
                raise ReferenceUnavailable(f"cannot observe {part}: {type(exc).__name__}") from None
            if not stat.S_ISDIR(st.st_mode):
                unexpected.append(GENERATION_REL)
                return present, unexpected
            try:
                child = _inv._open_dir(part, fd, st, part)
            except _inv.InventoryUnavailable as exc:
                raise ReferenceUnavailable(str(exc)) from None
            os.close(fd)
            fd = child
        for shard in _bounded_names(fd, GENERATION_REL, count):
            rel_shard = f"{GENERATION_REL}/{shard}"
            try:
                st = os.lstat(shard, dir_fd=fd)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ReferenceUnavailable(f"cannot observe {rel_shard}: {type(exc).__name__}") from None
            if not (_SHARD.fullmatch(shard) and stat.S_ISDIR(st.st_mode)):
                unexpected.append(rel_shard)
                continue
            try:
                sfd = _inv._open_dir(shard, fd, st, rel_shard)
            except _inv.InventoryUnavailable as exc:
                raise ReferenceUnavailable(str(exc)) from None
            try:
                for name in _bounded_names(sfd, rel_shard, count):
                    rel = f"{rel_shard}/{name}"
                    try:
                        est = os.lstat(name, dir_fd=sfd)
                    except FileNotFoundError:
                        continue
                    kind = "file" if stat.S_ISREG(est.st_mode) else "other"
                    sha = None
                    if kind == "file" and verify_hashes and rel in wanted:
                        sha = _hash_entry(name, sfd, est)
                    present[rel] = (kind, est.st_size, sha)
            except OSError as exc:
                raise ReferenceUnavailable(f"cannot observe {rel_shard}: {type(exc).__name__}") from None
            finally:
                os.close(sfd)
    finally:
        os.close(fd)
    return present, unexpected


def generation_references(db_snapshot, data_dir, *, verify_hashes: bool = False,
                          expected_root=None) -> ReferenceReport:
    """Check every generation_artifacts row in `db_snapshot` against the
    blobs under `data_dir`. Read-only; see the module docstring.

    A row's local_path is canonical when it names its blob under
    `expected_root` (default: `data_dir`). Restore passes the destination
    folder while it checks the staged copy that will be published there."""
    db_snapshot, data_dir = os.fspath(db_snapshot), os.fspath(data_dir)
    expected_root = data_dir if expected_root is None else os.fspath(expected_root)
    if not all(isinstance(x, str) for x in (data_dir, db_snapshot, expected_root)):
        raise ReferenceUnavailable("paths must be text")
    if any("\0" in x for x in (data_dir, db_snapshot, expected_root)):
        raise ReferenceUnavailable("paths must not contain a NUL byte")
    refuse_if_production_path(data_dir)
    rows = _read_rows(db_snapshot)
    if rows is None:
        # No table: there is nothing to reference, so a tree with anything in
        # it is an unknown state (a wrong, blank or foreign snapshot), never
        # a clean one with orphans.
        present, unexpected = _walk_generation(data_dir, {}, False)
        if present or unexpected:
            raise ReferenceUnavailable("generation files exist but the snapshot has no "
                                       "generation_artifacts table")
        rows = []
    findings, capture, wanted = [], [], {}
    for artifact_id, local_path, sha, size in rows:
        if not (isinstance(artifact_id, str) and _ID.fullmatch(artifact_id)):
            findings.append(Finding(UNSAFE_ID, None, None))
            continue
        wanted[blob_relpath(artifact_id)] = (artifact_id, local_path, sha, size)
    present, unexpected = _walk_generation(data_dir, wanted, verify_hashes)
    findings.extend(Finding(UNEXPECTED_ENTRY, None, rel) for rel in unexpected)
    claimed = set()
    for rel, (artifact_id, local_path, sha, size) in sorted(wanted.items()):
        manifest = rel + MANIFEST_SUFFIX
        claimed.update((rel, manifest))   # a row's manifest is never an orphan
        manifest_kind = present.get(manifest, (None,))[0]
        if manifest_kind not in (None, "file"):
            findings.append(Finding(NOT_A_FILE, artifact_id, manifest))
        if local_path != os.path.join(expected_root, rel):
            findings.append(Finding(FOREIGN_LOCAL_PATH, artifact_id, rel))
        found = present.get(rel)
        if found is None:
            findings.append(Finding(MISSING_BLOB, artifact_id, rel))
            continue
        kind, on_disk, actual = found
        if kind != "file":
            findings.append(Finding(NOT_A_FILE, artifact_id, rel))
            continue
        if not isinstance(size, int) or on_disk != size:
            findings.append(Finding(SIZE_MISMATCH, artifact_id, rel))
            continue
        if not (isinstance(sha, str) and _SHA.fullmatch(sha)) or (verify_hashes and actual != sha):
            findings.append(Finding(HASH_MISMATCH, artifact_id, rel))
            continue
        capture.append(CaptureItem(rel, ARCHIVE_PREFIX + rel, on_disk, sha, "blob"))
        if manifest_kind == "file":
            capture.append(CaptureItem(manifest, ARCHIVE_PREFIX + manifest,
                                       present[manifest][1], None, "manifest"))
    for rel, (kind, _size, _sha) in sorted(present.items()):
        if rel in claimed:
            continue
        if kind != "file":
            findings.append(Finding(UNEXPECTED_ENTRY, None, rel))
        elif rel.endswith(".tmp"):
            findings.append(Finding(TMP_LEFTOVER, None, rel))
        else:
            findings.append(Finding(ORPHAN_FILE, None, rel))
    return ReferenceReport(len(rows), bool(verify_hashes), tuple(findings), tuple(capture))
