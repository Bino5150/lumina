"""
core/generation_adapter.py -- MULTIMODAL-M4-GENERATION-SUBSTRATE-DESIGN-01

The seam a generation provider must implement to plug into the
GenerationJob/GeneratedArtifact substrate (core/generation_job.py,
core/generation_artifact.py). Mirrors core/capability_router.py's own
injectable-lookup pattern (CapabilityRegistry's health_lookup/
support_lookup): the substrate never imports a concrete provider SDK, and
a fake/test adapter stands in for a real one without either side of the
seam knowing the difference. Also mirrors core/backends/loader.py's
existing role for LLM backends -- this is the same idea one layer over,
for specialists that produce jobs instead of chat completions.

No adapter implementation ships in this module -- this is the shape only.
A real Higgsfield adapter is explicitly out of scope for this campaign
(MULTIMODAL-M4-GENERATION-SUBSTRATE-DESIGN-01): per
MULTIMODAL_M4_IMAGE_GENERATION_SOURCE_VET_2026-09-17.md Sec 7, its raw
HTTP/SDK contract is still unread as of this module's writing, and that is
handled by a separate campaign (MULTIMODAL-M4-HIGGSFIELD-API-VET-01) which
maps the real contract onto this already-frozen seam, rather than letting
Higgsfield's specific API shape define it.

`GenerationAdapter` is a typing.Protocol, not an ABC -- structural typing,
matching this codebase's existing backend-loader convention rather than
introducing a new plugin-registration mechanism.

Amendment (MULTIMODAL-M4-GENERATION-SUBSTRATE-AMENDMENT-01, 2026-09-17):
this Protocol originally had no cancellation seam at all.
MULTIMODAL-M4-HIGGSFIELD-API-VET-01 found a real, OpenAPI-specified
`POST /requests/{id}/cancel` endpoint (also wrapped by the official
Python/TypeScript SDKs), while core.generation_job's STATUS_CANCELLED
already existed as a first-class terminal status with nothing able to
reach it through this seam. cancel() below closes that gap -- purely
additive, no other method's signature changed.
"""
from __future__ import annotations

from typing import Optional, Protocol, Tuple

from core.generation_job import GenerationJob


class GenerationAdapter(Protocol):
    """One provider's implementation of the generation seam. All methods
    are expected to raise on failure rather than return a fabricated
    id/status/estimate -- the substrate treats "the adapter raised" as the
    only trustworthy failure signal; a caught-and-swallowed provider error
    that returns a made-up placeholder value would defeat the whole point
    of a closed, honest status vocabulary."""

    def submit(self, *, model: str, settings: dict,
               reference_assets: Tuple[Tuple[str, str], ...],
               provider_idempotency_key: Optional[str] = None) -> Tuple[str, str]:
        """Submit one generation request. Returns (provider_job_id,
        provider_status) -- provider_status is whatever raw string the
        provider reports; the caller normalizes it via
        core.generation_job.normalize_provider_status() before storing it,
        never trusts it as already-canonical."""
        ...

    def poll(self, provider_job_id: str) -> str:
        """Return the provider's current raw status string for this job.
        Never called on a job the adapter itself hasn't returned a
        provider_job_id for."""
        ...

    def fetch_result(self, provider_job_id: str) -> Tuple[bytes, str]:
        """Fetch the completed remote result. Returns (raw_bytes,
        mime_type). Only ever called after poll()/submit() reports a
        terminal success status. The substrate treats the returned bytes as
        UNTRUSTED until core.generation_artifact.ingest_artifact() has
        hashed and stored them -- this method's return value is not itself
        a GeneratedArtifact and must never be treated as one."""
        ...

    def describe_model(self, model: str) -> Optional[dict]:
        """Live schema discovery for one model (accepted media roles,
        parameter bounds, aspect ratios, etc.), or None if the provider/
        adapter doesn't support discovery. Never a static, hardcoded
        catalog baked into this codebase -- see
        MULTIMODAL_M4_IMAGE_GENERATION_SOURCE_VET_2026-09-17.md Sec 2 on
        why per-model schema is live, server-owned, mutable state that
        Lumina should query, not assume."""
        ...

    def estimate_cost(self, *, model: str, settings: dict) -> Optional[float]:
        """Pre-submit cost estimate in the adapter's own currency/unit, or
        None if unavailable. MUST NOT itself incur a charge or have any
        side effect on the provider's job queue -- read-only preflight
        only, matching the source-vet finding that Higgsfield's own CLI
        keeps `generate cost` a fully separate, non-submitting command from
        `generate create`."""
        ...

    def cancel(self, job: GenerationJob) -> GenerationJob:
        """Request cancellation of an in-flight job and return the
        refreshed GenerationJob reflecting whatever the provider actually
        confirmed. Unlike the other methods here, this one takes and
        returns the full record (not just a provider_job_id/raw string)
        because a well-behaved implementation needs the current status to
        decide whether attempting cancellation even makes sense, and is
        expected to call core.generation_job.update_status() itself to
        durably record the outcome before returning.

        Cancellation is PROVIDER work, never a local status-assignment
        shortcut:
          - MUST refuse rather than contact the provider at all when
            `job.status` is already in core.generation_job.TERMINAL_STATUSES
            (succeeded/failed/cancelled) -- raise
            core.generation_job.JobStateConflict. A job that already
            resolved must never be treated as if new cancellation work
            occurred, silently or otherwise.
          - MUST NOT set the returned job's status to STATUS_CANCELLED
            before the provider's response actually supports it. If the
            provider only ACKNOWLEDGES the cancellation request without
            confirming a terminal outcome, the returned job may legitimately
            remain in its current non-terminal status (queued/running) --
            a later poll() is what eventually confirms the real outcome,
            exactly like any other status change.
          - Whatever the provider reports must be mapped into the
            canonical status vocabulary the same way poll() results are
            (via core.generation_job.normalize_provider_status() or an
            adapter's own more precise translation) -- an unrecognized or
            unnormalizable provider response fails closed into
            STATUS_UNKNOWN, never assumed to mean cancelled.
          - STATUS_CANCELLED remains the only cancellation-terminal value;
            do not invent an intermediate "cancel requested" status --
            neither this Protocol nor core.generation_job's vocabulary has
            one, and none should be added without real provider evidence
            forcing it (see MULTIMODAL_M4_HIGGSFIELD_API_VET_2026-09-17.md
            Sec 13, finding 2 and the amendment record for why)."""
        ...
