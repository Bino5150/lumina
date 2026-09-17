"""
core/generation_artifact.py -- MULTIMODAL-M4-GENERATION-SUBSTRATE-DESIGN-01

The durable, Lumina-owned result of a generation job. Companion to
core/generation_job.py -- see that module's docstring for why the two are
split, and MULTIMODAL_M4_IMAGE_GENERATION_SOURCE_VET_2026-09-17.md for the
full design rationale (both external sources inspected there independently
converge on "a remote URL is untrusted data until locally fetched, hashed,
and owned").

Structural law: a GeneratedArtifact is constructible ONLY through
ingest_artifact(), which requires actual bytes in hand, computes their
hash, writes them under LUMINA_DATA_DIR, and only then inserts the row.
There is no code path that builds a GeneratedArtifact from a bare remote
URL or an unfetched reference -- "trust a URL as durable" is structurally
impossible here, not merely discouraged by convention.

Provider job status (core.generation_job's queued/running/succeeded/
failed/cancelled/unknown) and local ingestion outcome are two different
events and are recorded separately -- see core.generation_job.
set_ingestion_result(). A job can legitimately be status=succeeded with
ingestion_state=failed: the provider finished and a remote result exists,
but Lumina's own fetch/hash/store step failed. That combination is a real,
first-class state this module makes reachable and observable on purpose,
not an edge case papered over -- the whole point is that nobody should be
able to see "succeeded" and assume the remote URL is the deliverable.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Optional

import config
from core.redaction import redact_secret_shapes
from core.test_isolation import refuse_if_production_path
from core.generation_job import (
    INGESTION_FAILED,
    INGESTION_INGESTED,
    STATUS_SUCCEEDED,
    set_ingestion_result,
)

__all__ = [
    "GeneratedArtifact",
    "GenerationArtifactError",
    "IngestionError",
    "ArtifactNotFound",
    "init_generation_artifact_db",
    "ingest_artifact",
    "get_generation_artifact",
    "get_artifact_bytes",
    "list_artifacts_for_job",
]

_MAX_METADATA_BYTES = 8 * 1024
_MAX_PROVENANCE_BYTES = 8 * 1024


class GenerationArtifactError(Exception):
    """Base class for every error this module raises."""


class IngestionError(GenerationArtifactError):
    """Ingestion (hash/validate/store) failed. The caller's job is left at
    ingestion_state=INGESTION_FAILED with the sanitized reason recorded;
    no GeneratedArtifact row exists for this attempt."""


class ArtifactNotFound(GenerationArtifactError):
    """No generation_artifacts row exists for this artifact_id."""


@dataclass(frozen=True)
class GeneratedArtifact:
    artifact_id: str
    lumina_job_id: str
    local_path: str
    sha256: str
    mime_type: str
    size_bytes: int
    metadata: Mapping[str, object]
    provenance: Mapping[str, object]
    created_at: str


def init_generation_artifact_db():
    """Create the generation_artifacts table/indexes if they don't exist.
    Same idempotent CREATE-IF-NOT-EXISTS + WAL-transition retry convention
    as core.generation_job.init_generation_job_db()."""
    from core.db import connect
    conn = None
    for attempt in range(20):
        try:
            conn = connect()
            break
        except sqlite3.OperationalError as error:
            if getattr(error, "sqlite_errorcode", None) != sqlite3.SQLITE_BUSY or attempt == 19:
                raise
            time.sleep(0.01)
    if conn is None:  # pragma: no cover - defensive, loop either sets or raises
        raise GenerationArtifactError("could not initialize generation artifact database")
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS generation_artifacts (
                artifact_id     TEXT PRIMARY KEY,
                lumina_job_id   TEXT NOT NULL,
                local_path      TEXT NOT NULL,
                sha256          TEXT NOT NULL,
                mime_type       TEXT NOT NULL,
                size_bytes      INTEGER NOT NULL,
                metadata_json   TEXT NOT NULL,
                provenance_json TEXT NOT NULL,
                created_at      TEXT NOT NULL,
                FOREIGN KEY (lumina_job_id) REFERENCES generation_jobs(lumina_job_id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_generation_artifacts_job
            ON generation_artifacts(lumina_job_id)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_generation_artifacts_sha256
            ON generation_artifacts(sha256)
        """)
        conn.commit()
    finally:
        conn.close()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_json(value, *, max_bytes: int) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as e:
        raise IngestionError(f"value is not JSON-serializable: {e}") from e
    if len(encoded.encode("utf-8")) > max_bytes:
        raise IngestionError(f"value is {len(encoded)} bytes, exceeds cap of {max_bytes} bytes")
    return encoded


def _artifact_storage_root() -> str:
    """Computed fresh on every call from config.DATA_DIR -- never captured
    as a module-level constant -- so it reflects whatever DATA_DIR/DB_PATH
    isolation is active for the current process (production install, dev
    mirror, or a test's LUMINA_DATA_DIR override) rather than whatever was
    true at import time."""
    return os.path.join(config.DATA_DIR, "artifacts", "generation")


def _artifact_path(artifact_id: str) -> str:
    # Sharded by artifact_id prefix so one directory never accumulates
    # every artifact ever generated. Filename carries no extension -- the
    # DB row's mime_type is the authoritative type; this substrate slice
    # has no UI that needs a file-manager-friendly name.
    return os.path.join(_artifact_storage_root(), artifact_id[:2], artifact_id)


def _row_to_record(row) -> GeneratedArtifact:
    return GeneratedArtifact(
        artifact_id=row["artifact_id"],
        lumina_job_id=row["lumina_job_id"],
        local_path=row["local_path"],
        sha256=row["sha256"],
        mime_type=row["mime_type"],
        size_bytes=row["size_bytes"],
        metadata=json.loads(row["metadata_json"]),
        provenance=json.loads(row["provenance_json"]),
        created_at=row["created_at"],
    )


def _write_atomic(path: str, data: bytes) -> None:
    """Same temp-file + fsync + os.replace() idiom as core/persistence.py's
    save() -- a crash mid-write can never leave a half-written artifact
    file in place."""
    refuse_if_production_path(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def ingest_artifact(lumina_job_id: str, data: bytes, mime_type: str,
                     metadata: Optional[Mapping[str, object]] = None,
                     provenance: Optional[Mapping[str, object]] = None) -> GeneratedArtifact:
    """The ONLY constructor path for a GeneratedArtifact. `data` is the
    already-fetched remote result -- fetching it from wherever the
    provider's adapter says it lives (a URL, a signed blob endpoint, etc.)
    is the adapter's job, not this module's; this function's job starts
    once the bytes are already in the caller's hand, and is entirely about
    making them durable and trustworthy: hash, store, record.

    Requires the job's provider status to already be STATUS_SUCCEEDED and
    that no artifact has already been ingested for this job -- both checked
    BEFORE any bytes are written or any row inserted, so a precondition
    violation never leaves a half-written artifact or a misleading
    ingestion_state behind. Only once those checks pass does a failure
    (bad mime_type, oversized metadata, a disk write error) get recorded as
    ingestion_state=INGESTION_FAILED with a sanitized reason; this function
    always re-raises on failure, and no artifact row is left behind for a
    failed attempt."""
    if not isinstance(data, (bytes, bytearray)) or len(data) == 0:
        raise IngestionError("data must be non-empty bytes")
    if not isinstance(mime_type, str) or not mime_type:
        raise IngestionError("mime_type must be a non-empty string")

    init_generation_artifact_db()
    from core.generation_job import get_generation_job
    # get_generation_job() raises JobNotFound (a GenerationJobError, not
    # caught here) for an unknown job -- deliberately not translated into
    # an IngestionError, since there is no job row to record a failure
    # against.
    job = get_generation_job(lumina_job_id)
    if job.status != STATUS_SUCCEEDED:
        raise IngestionError(
            f"job {lumina_job_id} has provider status {job.status!r}, not succeeded; "
            "ingestion is not yet meaningful"
        )
    existing = list_artifacts_for_job(lumina_job_id)
    if existing:
        raise IngestionError(
            f"job {lumina_job_id} already has an ingested artifact ({existing[0].artifact_id}); "
            "ingest_artifact() is not a re-ingestion path"
        )

    try:
        metadata_json = _canonical_json(dict(metadata or {}), max_bytes=_MAX_METADATA_BYTES)
        redacted_provenance = {
            k: (redact_secret_shapes(str(v)) if isinstance(v, str) else v)
            for k, v in dict(provenance or {}).items()
        }
        provenance_json = _canonical_json(redacted_provenance, max_bytes=_MAX_PROVENANCE_BYTES)

        artifact_id = uuid.uuid4().hex
        sha256 = hashlib.sha256(data).hexdigest()
        local_path = _artifact_path(artifact_id)
        _write_atomic(local_path, bytes(data))

        from core.db import connect
        conn = connect()
        try:
            now = _utcnow_iso()
            conn.execute(
                "INSERT INTO generation_artifacts "
                "(artifact_id, lumina_job_id, local_path, sha256, mime_type, size_bytes, "
                " metadata_json, provenance_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (artifact_id, lumina_job_id, local_path, sha256, mime_type, len(data),
                 metadata_json, provenance_json, now),
            )
            conn.commit()
        finally:
            conn.close()
        set_ingestion_result(lumina_job_id, INGESTION_INGESTED)
    except Exception as e:
        set_ingestion_result(lumina_job_id, INGESTION_FAILED, error=str(e))
        if isinstance(e, IngestionError):
            raise
        raise IngestionError(f"ingestion failed: {type(e).__name__}: {e}") from e

    return get_generation_artifact(artifact_id)


def get_generation_artifact(artifact_id: str) -> GeneratedArtifact:
    init_generation_artifact_db()
    from core.db import connect
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM generation_artifacts WHERE artifact_id=?", (artifact_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ArtifactNotFound(f"no generation artifact {artifact_id}")
    return _row_to_record(row)


def get_artifact_bytes(artifact_id: str) -> bytes:
    """Reads the owned bytes back off disk, re-verifying the recorded hash
    so silent on-disk corruption/tampering is caught at read time rather
    than trusted forever after ingestion."""
    record = get_generation_artifact(artifact_id)
    refuse_if_production_path(record.local_path)
    with open(record.local_path, "rb") as f:
        data = f.read()
    if hashlib.sha256(data).hexdigest() != record.sha256:
        raise IngestionError(
            f"artifact {artifact_id} on-disk content no longer matches its recorded sha256"
        )
    return data


def list_artifacts_for_job(lumina_job_id: str) -> list:
    init_generation_artifact_db()
    from core.db import connect
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM generation_artifacts WHERE lumina_job_id=? ORDER BY created_at",
            (lumina_job_id,),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_record(row) for row in rows]
