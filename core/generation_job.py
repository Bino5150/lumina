"""
core/generation_job.py -- MULTIMODAL-M4-GENERATION-SUBSTRATE-DESIGN-01

The provider-neutral GenerationJob record: the durable, submitted unit of
work for any async media-generation capability (image_generation today;
audio/video/3D later slices, unchanged shape). Consumes M1
(core/capability_router.py) for capability vocabulary and routing
provenance -- a job cannot exist without an admitted RoutingDecision, the
same "never improvised" discipline M2 (core/vision_lane.py) already
applies to vision. Does NOT modify M1: Capability, SpecialistRecord,
CapabilityRegistry, RoutingPolicy, and resolve_capability() are consumed
as-is.

Why this module exists (see MULTIMODAL_M4_IMAGE_GENERATION_SOURCE_VET_
2026-09-17.md): M2's only executed specialist precedent is a synchronous
bounded text call. Real media generation -- Higgsfield and every other
serious provider -- is submit -> opaque job id -> queued/running ->
terminal, with cost estimates distinct from actual charges and a binary
result that must be locally fetched, hashed, and owned before it counts as
durable (core/generation_artifact.py). This module is the job side of
that; the artifact side lives in generation_artifact.py; spending policy
in generation_spending_policy.py; the provider seam in
generation_adapter.py. All four are frozen together as one campaign and
deliberately share no code with any real provider adapter -- none exists
yet.

Persistence follows core/context_checkpoints.py's established convention
exactly: config.DB_PATH read fresh on every call (never captured at import
time), core.db.connect() for WAL + busy_timeout + the TEST-DATA-ISOLATION-01
production-path guard, BEGIN IMMEDIATE for compare-and-swap transitions,
frozen dataclass records reconstructed from rows, typed exceptions per
failure mode.

Idempotency (Sol's review of the source-vet draft, folded in before this
module was written): a lumina_job_id minted before the network call is
necessary but not sufficient -- if the network call times out before the
provider's own job id comes back, the provider may already have accepted
the request while Lumina has nothing to reattach to. submission_fingerprint
is a deterministic hash over the normalized (capability, specialist, model,
settings, reference_assets) -- never secrets, never raw media bytes,
reusing core.idempotency.make_request_id() rather than inventing a second
hashing scheme. begin_generation_job() refuses to create a second row for
a fingerprint that already has an unresolved (non-terminal) job, so a
retried caller detects the ambiguity and stops instead of silently
double-submitting. provider_idempotency_key is carried for adapters whose
real API turns out to expose one (unconfirmed for Higgsfield as of the
source-vet pass); when absent, submission_fingerprint is the honest
fallback and is strictly weaker than a provider-enforced key -- it can
only stop Lumina from *knowingly* resubmitting, not stop two genuinely
distinct callers from submitting the same content on purpose.

Status vocabulary is closed and describes the PROVIDER's view of the job
only: queued/running/succeeded/failed/cancelled/unknown. It is never
extended with a Lumina-side ingestion concept -- see generation_artifact.py
for why "succeeded" and "a GeneratedArtifact exists" are different events
that must stay independently observable.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping, Optional, Sequence, Tuple

from core.capability_router import Capability, RoutingDecision
from core.idempotency import make_request_id
from core.redaction import redact_secret_shapes

__all__ = [
    "GenerationJob",
    "GenerationJobError",
    "UnknownCapability",
    "RouteNotAdmitted",
    "AmbiguousDuplicateSubmission",
    "JobNotFound",
    "JobStateConflict",
    "RetryBudgetExhausted",
    "STATUS_QUEUED", "STATUS_RUNNING", "STATUS_SUCCEEDED", "STATUS_FAILED",
    "STATUS_CANCELLED", "STATUS_UNKNOWN",
    "TERMINAL_STATUSES",
    "INGESTION_PENDING", "INGESTION_INGESTED", "INGESTION_FAILED",
    "compute_submission_fingerprint",
    "normalize_provider_status",
    "init_generation_job_db",
    "begin_generation_job",
    "mark_submitted",
    "update_status",
    "set_ingestion_result",
    "record_cost_actual",
    "bump_retry",
    "get_generation_job",
    "find_unresolved_by_fingerprint",
    "list_generation_jobs",
]

# ---------------------------------------------------------------------------
# Canonical vocabulary
# ---------------------------------------------------------------------------

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_UNKNOWN = "unknown"

_ALL_STATUSES = frozenset({
    STATUS_QUEUED, STATUS_RUNNING, STATUS_SUCCEEDED, STATUS_FAILED,
    STATUS_CANCELLED, STATUS_UNKNOWN,
})
TERMINAL_STATUSES = frozenset({STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELLED})

INGESTION_PENDING = "pending"
INGESTION_INGESTED = "ingested"
INGESTION_FAILED = "failed"
_ALL_INGESTION_STATES = frozenset({INGESTION_PENDING, INGESTION_INGESTED, INGESTION_FAILED})

_MAX_SETTINGS_BYTES = 16 * 1024
_MAX_ERROR_LEN = 500

# Best-effort normalization of raw provider status strings into the closed
# vocabulary above. Deliberately small and provider-agnostic (no Higgsfield-
# specific strings) -- an adapter is free to normalize more precisely
# itself before calling into this module; anything this table doesn't
# recognize becomes STATUS_UNKNOWN, never guessed into succeeded/failed.
_STATUS_SYNONYMS = {
    "queued": STATUS_QUEUED, "pending": STATUS_QUEUED, "created": STATUS_QUEUED,
    "running": STATUS_RUNNING, "processing": STATUS_RUNNING, "in_progress": STATUS_RUNNING,
    "started": STATUS_RUNNING,
    "succeeded": STATUS_SUCCEEDED, "success": STATUS_SUCCEEDED, "completed": STATUS_SUCCEEDED,
    "complete": STATUS_SUCCEEDED, "done": STATUS_SUCCEEDED,
    "failed": STATUS_FAILED, "error": STATUS_FAILED, "errored": STATUS_FAILED,
    "cancelled": STATUS_CANCELLED, "canceled": STATUS_CANCELLED, "aborted": STATUS_CANCELLED,
}


def normalize_provider_status(raw: object) -> str:
    """Fail-closed normalization: an unrecognized or non-string value
    becomes STATUS_UNKNOWN rather than being coerced into a guess. Matches
    core.capability_router's EvidenceClass discipline -- unknown is a
    distinct, representable state, never silently collapsed into a
    positive or negative claim."""
    if not isinstance(raw, str):
        return STATUS_UNKNOWN
    return _STATUS_SYNONYMS.get(raw.strip().lower(), STATUS_UNKNOWN)


class GenerationJobError(Exception):
    """Base class for every error this module raises."""


class UnknownCapability(GenerationJobError):
    """capability is not in core.capability_router's canonical vocabulary."""


class RouteNotAdmitted(GenerationJobError):
    """The supplied RoutingDecision did not admit a specialist. A
    GenerationJob can never be constructed from an unrouted decision --
    generation is never improvised, mirroring M2's vision-lane law."""


class AmbiguousDuplicateSubmission(GenerationJobError):
    """An unresolved (non-terminal) job with this exact
    submission_fingerprint already exists. Raised instead of silently
    creating a second job, per the idempotency discipline above."""


class JobNotFound(GenerationJobError):
    """No generation_jobs row exists for this lumina_job_id."""


class JobStateConflict(GenerationJobError):
    """The requested transition is illegal from the row's current state."""


class RetryBudgetExhausted(GenerationJobError):
    """bump_retry() would exceed max_retries. Retries are bounded and the
    exhaustion is reported, never silently retried past the cap."""


@dataclass(frozen=True)
class GenerationJob:
    lumina_job_id: str
    capability: str
    specialist: str
    model: str
    route_decision: Mapping[str, object]
    settings: Mapping[str, object]
    reference_assets: Tuple[Tuple[str, str], ...]
    submission_fingerprint: str
    provider_job_id: Optional[str]
    provider_idempotency_key: Optional[str]
    status: str
    failure_class: Optional[str]
    cost_estimate: Optional[float]
    cost_actual: Optional[float]
    cost_unit: Optional[str]
    ingestion_state: Optional[str]
    ingestion_error: Optional[str]
    retry_count: int
    max_retries: int
    created_at: str
    submitted_at: Optional[str]
    terminal_at: Optional[str]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_generation_job_db():
    """Create the generation_jobs table/indexes if they don't exist. Safe to
    call on every operation -- CREATE TABLE/INDEX IF NOT EXISTS, idempotent,
    never migrates or touches existing rows. Never captures a DB path at
    import time: connect() reads config.DB_PATH fresh on every call,
    matching core/context_checkpoints.py's init_context_checkpoint_db()."""
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
        raise GenerationJobError("could not initialize generation job database")
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS generation_jobs (
                lumina_job_id               TEXT PRIMARY KEY,
                capability                  TEXT NOT NULL,
                specialist                  TEXT NOT NULL,
                model                       TEXT NOT NULL,
                route_decision_json         TEXT NOT NULL,
                settings_json               TEXT NOT NULL,
                reference_assets_json       TEXT NOT NULL,
                submission_fingerprint      TEXT NOT NULL,
                provider_job_id             TEXT,
                provider_idempotency_key    TEXT,
                status                      TEXT NOT NULL,
                failure_class               TEXT,
                cost_estimate               REAL,
                cost_actual                 REAL,
                cost_unit                   TEXT,
                ingestion_state             TEXT,
                ingestion_error             TEXT,
                retry_count                 INTEGER NOT NULL DEFAULT 0,
                max_retries                 INTEGER NOT NULL DEFAULT 1,
                created_at                  TEXT NOT NULL,
                submitted_at                TEXT,
                terminal_at                 TEXT
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_generation_jobs_fingerprint
            ON generation_jobs(submission_fingerprint, status)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_generation_jobs_status
            ON generation_jobs(status, capability)
        """)
        conn.commit()
    finally:
        conn.close()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_json(value, *, max_bytes: Optional[int] = None) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as e:
        raise GenerationJobError(f"value is not JSON-serializable: {e}") from e
    if max_bytes is not None and len(encoded.encode("utf-8")) > max_bytes:
        raise GenerationJobError(f"value is {len(encoded)} bytes, exceeds cap of {max_bytes} bytes")
    return encoded


def compute_submission_fingerprint(capability: str, specialist: str, model: str,
                                    settings: Mapping[str, object],
                                    reference_assets: Sequence[Tuple[str, str]]) -> str:
    """Deterministic fingerprint over the normalized request -- same inputs,
    same fingerprint, no timestamps or random ids involved. Reuses
    core.idempotency.make_request_id() rather than inventing a second
    hashing scheme; that function already guarantees the same "derived from
    actual arguments" discipline this needs."""
    normalized_refs = sorted((str(role), str(ref)) for role, ref in reference_assets)
    return make_request_id(str(capability), str(specialist), str(model),
                            json.loads(_canonical_json(dict(settings))), normalized_refs)


def _routing_decision_to_dict(decision: RoutingDecision) -> dict:
    return {
        "capability": decision.capability,
        "outcome": decision.outcome,
        "selected": decision.selected,
        "classification": decision.classification,
        "reason": redact_secret_shapes(str(decision.reason or "")),
        "evidence": dict(decision.evidence or {}),
        "excluded_primary_backend": decision.excluded_primary_backend,
    }


def _row_to_record(row) -> GenerationJob:
    return GenerationJob(
        lumina_job_id=row["lumina_job_id"],
        capability=row["capability"],
        specialist=row["specialist"],
        model=row["model"],
        route_decision=json.loads(row["route_decision_json"]),
        settings=json.loads(row["settings_json"]),
        reference_assets=tuple(tuple(pair) for pair in json.loads(row["reference_assets_json"])),
        submission_fingerprint=row["submission_fingerprint"],
        provider_job_id=row["provider_job_id"],
        provider_idempotency_key=row["provider_idempotency_key"],
        status=row["status"],
        failure_class=row["failure_class"],
        cost_estimate=row["cost_estimate"],
        cost_actual=row["cost_actual"],
        cost_unit=row["cost_unit"],
        ingestion_state=row["ingestion_state"],
        ingestion_error=row["ingestion_error"],
        retry_count=row["retry_count"],
        max_retries=row["max_retries"],
        created_at=row["created_at"],
        submitted_at=row["submitted_at"],
        terminal_at=row["terminal_at"],
    )


def _sanitize_error(text) -> Optional[str]:
    if text is None:
        return None
    text = redact_secret_shapes(str(text)).strip()
    if not text:
        return None
    return text[:_MAX_ERROR_LEN]


# ---------------------------------------------------------------------------
# Storage API
# ---------------------------------------------------------------------------

def begin_generation_job(capability, specialist: str, model: str,
                          route_decision: RoutingDecision,
                          settings: Mapping[str, object],
                          reference_assets: Sequence[Tuple[str, str]] = (),
                          provider_idempotency_key: Optional[str] = None,
                          cost_estimate: Optional[float] = None,
                          cost_unit: Optional[str] = None,
                          max_retries: int = 1) -> GenerationJob:
    """Create a new job in STATUS_UNKNOWN -- honestly, since nothing has
    been submitted to a provider yet at this point (mark_submitted() is the
    transition once an adapter call actually reaches the provider).
    Requires an ADMITTED RoutingDecision naming this exact specialist:
    generation is never improvised, matching M2's vision-lane law. Refuses
    to create a second job for a submission_fingerprint that already has an
    unresolved (non-terminal) job -- see AmbiguousDuplicateSubmission."""
    cap = capability.value if isinstance(capability, Capability) else capability
    try:
        Capability(cap)
    except ValueError:
        raise UnknownCapability(f"not a canonical capability: {capability!r}")

    if route_decision.outcome != "routed" or not route_decision.selected:
        raise RouteNotAdmitted(
            f"RoutingDecision for {cap!r} was not admitted (outcome={route_decision.outcome!r}); "
            "a GenerationJob cannot be created from an unrouted decision"
        )
    if route_decision.selected != specialist:
        raise RouteNotAdmitted(
            f"RoutingDecision admitted {route_decision.selected!r}, not the requested specialist {specialist!r}"
        )
    if not isinstance(specialist, str) or not specialist:
        raise GenerationJobError("specialist must be a non-empty string")
    if not isinstance(model, str) or not model:
        raise GenerationJobError("model must be a non-empty string")
    if not isinstance(max_retries, int) or isinstance(max_retries, bool) or max_retries < 0:
        raise GenerationJobError("max_retries must be a non-negative int")
    if cost_estimate is not None and (not isinstance(cost_estimate, (int, float)) or cost_estimate < 0):
        raise GenerationJobError("cost_estimate must be a non-negative number or None")

    settings = dict(settings or {})
    reference_assets = tuple((str(role), str(ref)) for role, ref in (reference_assets or ()))
    settings_json = _canonical_json(settings, max_bytes=_MAX_SETTINGS_BYTES)
    refs_json = _canonical_json(list(reference_assets))
    fingerprint = compute_submission_fingerprint(cap, specialist, model, settings, reference_assets)

    init_generation_job_db()
    from core.db import connect
    conn = connect()
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT lumina_job_id FROM generation_jobs "
            "WHERE submission_fingerprint = ? AND status NOT IN (?, ?, ?)",
            (fingerprint, STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELLED),
        ).fetchone()
        if existing is not None:
            conn.execute("ROLLBACK")
            raise AmbiguousDuplicateSubmission(
                f"an unresolved job ({existing['lumina_job_id']}) already exists for this exact "
                "request (capability/specialist/model/settings/references); reattach to it "
                "instead of submitting again"
            )
        lumina_job_id = uuid.uuid4().hex
        now = _utcnow_iso()
        conn.execute(
            "INSERT INTO generation_jobs "
            "(lumina_job_id, capability, specialist, model, route_decision_json, settings_json, "
            " reference_assets_json, submission_fingerprint, provider_idempotency_key, status, "
            " cost_estimate, cost_unit, retry_count, max_retries, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (lumina_job_id, cap, specialist, model,
             _canonical_json(_routing_decision_to_dict(route_decision)),
             settings_json, refs_json, fingerprint, provider_idempotency_key,
             STATUS_UNKNOWN, cost_estimate, cost_unit, max_retries, now),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return get_generation_job(lumina_job_id)


def mark_submitted(lumina_job_id: str, provider_job_id: str, provider_status) -> GenerationJob:
    """The moment an adapter's submit() call actually reaches the provider
    and returns an id. Only legal once, from STATUS_UNKNOWN with no
    provider_job_id yet -- a second call is a caller bug (calling
    mark_submitted twice for the same job), not a legitimate retry path."""
    if not isinstance(provider_job_id, str) or not provider_job_id:
        raise GenerationJobError("provider_job_id must be a non-empty string")
    status = normalize_provider_status(provider_status)

    init_generation_job_db()
    from core.db import connect
    conn = connect()
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM generation_jobs WHERE lumina_job_id=?", (lumina_job_id,)
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            raise JobNotFound(f"no generation job {lumina_job_id}")
        if row["status"] != STATUS_UNKNOWN or row["provider_job_id"] is not None:
            conn.execute("ROLLBACK")
            raise JobStateConflict(
                f"job {lumina_job_id} was already submitted (status={row['status']!r}, "
                f"provider_job_id={row['provider_job_id']!r})"
            )
        now = _utcnow_iso()
        conn.execute(
            "UPDATE generation_jobs SET provider_job_id=?, status=?, submitted_at=?, "
            "terminal_at=CASE WHEN ? IN (?, ?, ?) THEN ? ELSE terminal_at END "
            "WHERE lumina_job_id=?",
            (provider_job_id, status, now,
             status, STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELLED, now,
             lumina_job_id),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return get_generation_job(lumina_job_id)


def update_status(lumina_job_id: str, new_status: str, failure_class: Optional[str] = None) -> GenerationJob:
    """CAS status transition. Refuses to transition out of a terminal
    status (succeeded/failed/cancelled are permanent for a given job) --
    unlike a StaleSpine-style repair, there is no legitimate reason for a
    provider to reopen a terminal job; a provider that appears to do so is
    an adapter/provider bug to surface, not something this module papers
    over silently."""
    if new_status not in _ALL_STATUSES:
        raise GenerationJobError(f"not a canonical status: {new_status!r}")

    init_generation_job_db()
    from core.db import connect
    conn = connect()
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM generation_jobs WHERE lumina_job_id=?", (lumina_job_id,)
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            raise JobNotFound(f"no generation job {lumina_job_id}")
        if row["status"] in TERMINAL_STATUSES:
            conn.execute("ROLLBACK")
            raise JobStateConflict(
                f"job {lumina_job_id} is already terminal ({row['status']!r}); cannot transition to {new_status!r}"
            )
        now = _utcnow_iso()
        terminal_at = now if new_status in TERMINAL_STATUSES else row["terminal_at"]
        conn.execute(
            "UPDATE generation_jobs SET status=?, failure_class=?, terminal_at=? WHERE lumina_job_id=?",
            (new_status, _sanitize_error(failure_class) if new_status == STATUS_FAILED else None,
             terminal_at, lumina_job_id),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return get_generation_job(lumina_job_id)


def set_ingestion_result(lumina_job_id: str, ingestion_state: str,
                          error: Optional[str] = None) -> GenerationJob:
    """Records the OUTCOME of local ingestion, kept structurally separate
    from the provider status vocabulary (see module docstring). Only legal
    once the job's provider status is STATUS_SUCCEEDED -- ingestion is
    meaningless before the provider says the result exists. Called by
    core.generation_artifact.ingest_artifact(); not expected to be called
    directly by application code."""
    if ingestion_state not in _ALL_INGESTION_STATES:
        raise GenerationJobError(f"not a canonical ingestion state: {ingestion_state!r}")

    init_generation_job_db()
    from core.db import connect
    conn = connect()
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM generation_jobs WHERE lumina_job_id=?", (lumina_job_id,)
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            raise JobNotFound(f"no generation job {lumina_job_id}")
        if row["status"] != STATUS_SUCCEEDED:
            conn.execute("ROLLBACK")
            raise JobStateConflict(
                f"job {lumina_job_id} has provider status {row['status']!r}, not succeeded; "
                "ingestion is not yet meaningful"
            )
        conn.execute(
            "UPDATE generation_jobs SET ingestion_state=?, ingestion_error=? WHERE lumina_job_id=?",
            (ingestion_state, _sanitize_error(error), lumina_job_id),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return get_generation_job(lumina_job_id)


def record_cost_actual(lumina_job_id: str, cost_actual: float, cost_unit: Optional[str] = None) -> GenerationJob:
    """Records the ACTUAL charged cost, kept in a field separate from
    cost_estimate for the life of the job -- never overwrites or reuses the
    estimate field. Callable at any point after submission; does not
    require a terminal status, since some providers may report charges
    incrementally."""
    if not isinstance(cost_actual, (int, float)) or cost_actual < 0:
        raise GenerationJobError("cost_actual must be a non-negative number")

    init_generation_job_db()
    from core.db import connect
    conn = connect()
    try:
        row = conn.execute(
            "SELECT lumina_job_id, cost_unit FROM generation_jobs WHERE lumina_job_id=?",
            (lumina_job_id,),
        ).fetchone()
        if row is None:
            raise JobNotFound(f"no generation job {lumina_job_id}")
        conn.execute(
            "UPDATE generation_jobs SET cost_actual=?, cost_unit=COALESCE(?, cost_unit) WHERE lumina_job_id=?",
            (cost_actual, cost_unit, lumina_job_id),
        )
        conn.commit()
    finally:
        conn.close()
    return get_generation_job(lumina_job_id)


def bump_retry(lumina_job_id: str) -> GenerationJob:
    """Increments retry_count if under max_retries; raises
    RetryBudgetExhausted and leaves the counter untouched otherwise.
    Retries are bounded by construction -- there is no code path in this
    module that retries past the cap."""
    init_generation_job_db()
    from core.db import connect
    conn = connect()
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT retry_count, max_retries FROM generation_jobs WHERE lumina_job_id=?",
            (lumina_job_id,),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            raise JobNotFound(f"no generation job {lumina_job_id}")
        if row["retry_count"] >= row["max_retries"]:
            conn.execute("ROLLBACK")
            raise RetryBudgetExhausted(
                f"job {lumina_job_id} has used {row['retry_count']}/{row['max_retries']} retries"
            )
        conn.execute(
            "UPDATE generation_jobs SET retry_count = retry_count + 1 WHERE lumina_job_id=?",
            (lumina_job_id,),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return get_generation_job(lumina_job_id)


def get_generation_job(lumina_job_id: str) -> GenerationJob:
    init_generation_job_db()
    from core.db import connect
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM generation_jobs WHERE lumina_job_id=?", (lumina_job_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise JobNotFound(f"no generation job {lumina_job_id}")
    return _row_to_record(row)


def find_unresolved_by_fingerprint(submission_fingerprint: str) -> Optional[GenerationJob]:
    """Returns the unresolved (non-terminal) job for this fingerprint, if
    any -- the same check begin_generation_job() runs internally, exposed
    so a caller can proactively reattach before even attempting to submit."""
    init_generation_job_db()
    from core.db import connect
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM generation_jobs WHERE submission_fingerprint = ? "
            "AND status NOT IN (?, ?, ?) ORDER BY created_at DESC LIMIT 1",
            (submission_fingerprint, STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELLED),
        ).fetchone()
    finally:
        conn.close()
    return _row_to_record(row) if row is not None else None


def list_generation_jobs(capability: Optional[str] = None, status: Optional[str] = None) -> list:
    init_generation_job_db()
    from core.db import connect
    conn = connect()
    try:
        clauses, params = [], []
        if capability is not None:
            clauses.append("capability = ?")
            params.append(capability)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM generation_jobs {where} ORDER BY created_at DESC", params
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_record(row) for row in rows]
