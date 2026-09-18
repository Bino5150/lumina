"""
core/image_generation_service.py -- MULTIMODAL-M4-IMAGE-GENERATION-SERVICE-01

The smallest provider-neutral orchestration layer that wires the already-
landed M1/M4 substrate into one truthful image_generation workflow:

    M1 capability-route resolution (core.capability_router)
        -> explicit provider/model target (caller-supplied, never inferred)
        -> cost estimate (core.generation_adapter.GenerationAdapter)
        -> spending-policy decision (core.generation_spending_policy)
        -> GenerationJob construction (core.generation_job)
        -> adapter submission
        -> bounded, cooperatively-cancellable polling
        -> 0..N output discovery + retrieval
        -> Lumina-owned durable ingestion (core.generation_artifact)
        -> one core.generation_manifest per ingested artifact
        -> a truthful, closed-vocabulary aggregate result

This module is orchestration only. It owns no new persistence, no new
vocabulary, and no provider-specific behavior -- every law it touches is
already frozen in the module that owns it, and this module's only job is
to call those modules in the right order with the right data. It performs
real I/O (DB, network via the adapter) and is therefore NOT import-pure
like core.capability_router/core.generation_spending_policy, but nothing
executes at import time and no provider SDK is imported here.

Design precedent this module deliberately follows rather than inventing
its own conventions (source-vetted before writing a line of this module):

  - "Never raises for an expected runtime outcome; returns a closed-
    vocabulary outcome instead." Exactly core.vision_lane.py's own
    ImageGenerationResult-equivalent (VisionLaneResult) discipline.
    Exceptions here are reserved for genuine caller-contract violations
    (bad arguments to THIS function) -- never for a provider's ordinary
    failure/cancellation/timeout/ambiguity, which are all first-class,
    testable outcome values instead.
  - `cancel_event=None`, checked via `.is_set()` before a blocking call
    and again after -- the same cooperative-cancellation contract
    core.vision_lane.execute_routed_vision() and
    core.process_manager.wait_for_completion() already use. Never a
    thread kill, never an uncooperative interrupt.
  - A `time.monotonic()` deadline + bounded sleep loop for polling,
    matching core.process_manager.wait_for_completion()'s own idiom.
  - Sanitize-then-truncate diagnostics via core.redaction.
    redact_secret_shapes(), the same primitive every other diagnostic
    path in this codebase already uses (core.vision_lane._sanitize_
    diagnostic(), core.generation_job._sanitize_error()).

CRITICAL MODEL-BINDING LAW: `model` is a per-call, per-invocation string
threaded explicitly through every adapter call this module makes
(estimate_cost, submit). Nothing in this module ever reads or writes a
`.model` attribute on any object, and no provider/global configuration is
mutated, temporarily or otherwise -- there is no "swap the provider's
configured model, call, swap it back" pattern anywhere here, which would
create a cross-lane race the moment two capabilities' work overlaps.

MULTI-OUTPUT DISCOVERY, KEPT PROVIDER-NEUTRAL: core.generation_adapter.
GenerationAdapter's frozen Protocol only guarantees a single-output
fetch_result() (it raises MultipleOutputsError for a job with more than
one output -- see core/generation_adapter.py and core/higgsfield_adapter.
py's own docstrings on why promoting a multi-output method into that
frozen Protocol is deliberately deferred). This module never modifies
that Protocol. Instead it duck-types: when an adapter ALSO exposes
list_outputs()/fetch_output() (an optional, additive pair -- exactly what
core.higgsfield_adapter.HiggsfieldAdapter already ships, described there
as "Higgsfield-specific, NOT Protocol-mandated"), this module uses them
for genuine 0..N discovery; otherwise it falls back to exactly one output
via fetch_result(). No branch anywhere checks a provider name -- the
check is purely structural (hasattr/callable), so any future adapter that
grows the same optional pair is picked up automatically, and Higgsfield
gets no special-cased behavior in this module.

MANIFEST LAW, UNWEAKENED: every artifact this module successfully makes
durable gets exactly one core.generation_manifest.build_manifest() call,
which is itself validate_manifest() underneath -- capability validation,
canonical-provider validation, the authorization_ref requirement, secret
refusal, and the exact-set field law all still run in full for every
manifest this module produces. This module ADDS two of its own upfront,
fail-closed preflight checks (manifest_provider registration, and an
authorization_ref for a cost-bearing request) purely so a misconfigured
call fails BEFORE any spend or submission rather than after -- it never
relaxes what generation_manifest.py itself requires.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence, Tuple

from core.capability_router import (
    Capability,
    CapabilityRegistry,
    RoutingPolicy,
    OUTCOME_ROUTED,
    resolve_capability,
)
from core.generation_adapter import GenerationAdapter
from core.generation_artifact import GeneratedArtifact
import core.generation_artifact as ga
import core.generation_job as gj
import core.generation_manifest as gm
import core.generation_spending_policy as sp
from core.redaction import redact_secret_shapes

__all__ = [
    "ImageGenerationResult",
    "ImageGenerationServiceError",
    "generate_image",
    "OUTCOME_SUCCESS",
    "OUTCOME_PARTIAL",
    "OUTCOME_ZERO_OUTPUTS",
    "OUTCOME_INGESTION_FAILED",
    "OUTCOME_DISCOVERY_FAILED",
    "OUTCOME_PROVIDER_FAILED",
    "OUTCOME_CANCELLED",
    "OUTCOME_TIMED_OUT",
    "OUTCOME_NOT_ROUTED",
    "OUTCOME_UNKNOWN_MANIFEST_PROVIDER",
    "OUTCOME_MISSING_AUTHORIZATION",
    "OUTCOME_SPEND_DENIED",
    "OUTCOME_SPEND_REQUIRES_APPROVAL",
    "OUTCOME_SUBMISSION_FAILED",
    "OUTCOME_AMBIGUOUS_DUPLICATE",
]

_DIAGNOSTIC_MAX_CHARS = 300

# ---------------------------------------------------------------------------
# Outcome vocabulary -- closed, this module's own aggregate-result axis.
# Distinct from core.generation_job's provider-status vocabulary (which
# stays exactly as frozen there) and from core.generation_job's ingestion-
# state vocabulary (ditto): this is "what happened when THIS module ran
# one generation request end to end," truthfully covering the cases the
# provider-status/ingestion-state axes alone cannot express (spend denial,
# missing authorization, an unregistered manifest provider, submission
# ambiguity, a bounded poll exhausting without a terminal status).
# ---------------------------------------------------------------------------

OUTCOME_SUCCESS = "success"                              # every attempted output ingested
OUTCOME_PARTIAL = "partial"                               # some ingested, some failed (never laundered)
OUTCOME_ZERO_OUTPUTS = "zero_outputs"                     # provider succeeded with genuinely 0 outputs
OUTCOME_INGESTION_FAILED = "ingestion_failed"             # provider succeeded, every attempt failed
OUTCOME_DISCOVERY_FAILED = "discovery_failed"             # provider succeeded, output discovery itself failed
OUTCOME_PROVIDER_FAILED = "provider_failed"               # provider terminal status: failed
OUTCOME_CANCELLED = "cancelled"                           # provider-confirmed terminal cancellation
OUTCOME_TIMED_OUT = "timed_out"                           # bounded polling exhausted, never reached terminal
OUTCOME_NOT_ROUTED = "not_routed"                         # M1 did not admit the requested specialist
OUTCOME_UNKNOWN_MANIFEST_PROVIDER = "unknown_manifest_provider"  # manifest_provider not registered (L3)
OUTCOME_MISSING_AUTHORIZATION = "missing_authorization"   # cost-bearing request with no authorization_ref
OUTCOME_SPEND_DENIED = "spend_denied"                     # spending policy: denied
OUTCOME_SPEND_REQUIRES_APPROVAL = "spend_requires_approval"  # spending policy: requires_approval
OUTCOME_SUBMISSION_FAILED = "submission_failed"           # adapter.submit() raised; never assumed to have reached the provider
OUTCOME_AMBIGUOUS_DUPLICATE = "ambiguous_duplicate"       # an unresolved twin already exists for this exact request


class ImageGenerationServiceError(Exception):
    """Raised ONLY for a genuine caller-contract violation in how
    generate_image() itself was invoked (a missing/malformed argument to
    THIS function) -- never for a provider/network/policy runtime
    condition, which is always reported via ImageGenerationResult.outcome
    instead, matching core.vision_lane's own "never raises for an expected
    outcome" discipline."""


@dataclass(frozen=True)
class ImageGenerationResult:
    """Provider-neutral, truthful result of one end-to-end image_generation
    request.

    outcome: one of the OUTCOME_* constants above.
    lumina_job_id / provider_job_id / job_status: the underlying
        GenerationJob's identity and FINAL provider-status axis, when a
        job was created at all (None for outcomes decided before job
        creation, e.g. OUTCOME_NOT_ROUTED).
    cost_estimate: the adapter's own pre-submission estimate, whenever one
        was obtained (None when never reached or genuinely unavailable).
    artifacts / manifests: only the artifacts that actually became
        Lumina-owned durable bytes, and the one validated manifest built
        for each of them, in the same order. An output that failed to
        fetch or ingest contributes to NEITHER tuple -- there is no
        manifest for an artifact that never became durable.
    failed_output_indices: provider_output_index values whose fetch or
        ingestion attempt failed, for a partial/ingestion_failed outcome.
    diagnostic: sanitized (redact_secret_shapes'd), truncated, human
        explanation -- "" on a clean success.
    """

    outcome: str
    lumina_job_id: Optional[str] = None
    provider_job_id: Optional[str] = None
    job_status: Optional[str] = None
    cost_estimate: Optional[float] = None
    artifacts: Tuple[GeneratedArtifact, ...] = ()
    manifests: Tuple[Mapping[str, object], ...] = ()
    failed_output_indices: Tuple[int, ...] = ()
    diagnostic: str = ""


def _sanitize_diagnostic(text) -> str:
    """Sanitize, then truncate -- the same idiom as
    core.vision_lane._sanitize_diagnostic(): redact_secret_shapes() first
    so key-shaped material can never ride a failure diagnostic out of this
    module, then a bounded truncation so provider error bodies stay
    bounded."""
    text = (text or "").strip()
    try:
        text = redact_secret_shapes(text)
    except Exception:
        pass  # redaction must never break the failure path itself
    if len(text) > _DIAGNOSTIC_MAX_CHARS:
        text = text[:_DIAGNOSTIC_MAX_CHARS] + " …"
    return text


def _discover_output_plan(adapter: GenerationAdapter, provider_job_id: str):
    """Returns (indices, multi). Purely structural (hasattr/callable)
    duck-typing against an OPTIONAL, additive pair of methods -- never a
    provider-name check -- so this stays provider-neutral even though
    core.higgsfield_adapter.HiggsfieldAdapter is, today, the only adapter
    that actually implements them. An adapter lacking the pair is assumed
    to produce exactly one output via the frozen fetch_result() Protocol
    method, matching generation_adapter.GenerationAdapter's own contract."""
    list_outputs = getattr(adapter, "list_outputs", None)
    fetch_output = getattr(adapter, "fetch_output", None)
    if callable(list_outputs) and callable(fetch_output):
        outputs = list_outputs(provider_job_id)
        return list(range(len(outputs))), True
    return [0], False


def generate_image(
    *,
    registry: CapabilityRegistry,
    policy: RoutingPolicy,
    specialist: str,
    model: str,
    adapter: GenerationAdapter,
    settings: Mapping[str, object],
    spending_policy: sp.SpendingPolicy,
    manifest_provider: str,
    authorization_ref: Optional[str] = None,
    cost_unit: Optional[str] = None,
    session_spent: float = 0.0,
    reference_assets: Sequence[Tuple[str, str]] = (),
    provider_idempotency_key: Optional[str] = None,
    max_retries: int = 1,
    primary_backend: Optional[str] = None,
    poll_interval: float = 2.0,
    poll_timeout: Optional[float] = 120.0,
    cancel_event=None,
    review_state: str = gm.REVIEW_SUBMITTED,
    extra_metadata: Optional[Mapping[str, object]] = None,
) -> ImageGenerationResult:
    """Run one image_generation request end to end. Never raises for an
    expected runtime outcome (routing refusal, spend denial, missing
    authorization, submission ambiguity/failure, provider failure,
    cancellation, a bounded-poll timeout, per-output fetch/ingestion
    failure) -- every one of those is a distinct ImageGenerationResult.
    outcome instead. Raises ImageGenerationServiceError only for a
    malformed call to this function itself, and lets
    core.generation_manifest's own ManifestValidationError subclasses
    propagate in the one case they can still legitimately fire this late
    (secret-shaped material discovered inside settings/params at build
    time -- see this module's docstring: every other manifest-law
    precondition is preflighted above).

    `specialist` (the core.capability_router / core.generation_job
    identity M1 must admit) and `manifest_provider` (the separate
    core.generation_manifest provider/manifest identity axis, L2) are
    deliberately two independent parameters, never merged into one, even
    though a real caller will usually pass the same literal string for
    both -- preserving the exact capability/provider axis separation both
    M1 and the manifest law already require.

    `model` is threaded explicitly into every adapter call below and
    nowhere else -- see this module's docstring on the model-binding law.
    """
    if not isinstance(specialist, str) or not specialist:
        raise ImageGenerationServiceError("specialist must be a non-empty string")
    if not isinstance(model, str) or not model:
        raise ImageGenerationServiceError("model must be a non-empty string")
    if not isinstance(manifest_provider, str) or not manifest_provider:
        raise ImageGenerationServiceError("manifest_provider must be a non-empty string")
    if cancel_event is not None and not callable(getattr(cancel_event, "is_set", None)):
        raise ImageGenerationServiceError("cancel_event must provide is_set()")

    settings = dict(settings or {})

    # 1. Capability-route resolution -- generation is never improvised.
    # M1 owns this decision in full; this module never re-derives priority,
    # health, or fallback ordering.
    decision = resolve_capability(
        registry, policy, Capability.IMAGE_GENERATION, primary_backend=primary_backend,
    )
    if decision.outcome != OUTCOME_ROUTED or decision.selected != specialist:
        return ImageGenerationResult(
            outcome=OUTCOME_NOT_ROUTED,
            diagnostic=_sanitize_diagnostic(
                f"image_generation route did not admit {specialist!r}: "
                f"outcome={decision.outcome!r} selected={decision.selected!r} "
                f"({decision.reason})"
            ),
        )

    # Preflight (manifest law L3, checked early): an unregistered
    # manifest_provider is refused BEFORE any spend or submission, not
    # discovered only after money is spent and bytes are fetched.
    if manifest_provider not in gm.CANONICAL_PROVIDERS:
        return ImageGenerationResult(
            outcome=OUTCOME_UNKNOWN_MANIFEST_PROVIDER,
            diagnostic=_sanitize_diagnostic(
                f"manifest_provider {manifest_provider!r} is not a registered "
                "canonical generation-manifest provider"
            ),
        )

    # 3. Cost estimate -- read-only preflight per the GenerationAdapter
    # Protocol's own contract (no charge, no provider-side side effect).
    # An adapter is expected to raise only for a genuine caller-side bug
    # (unknown model/parameter); that is never swallowed here.
    estimate = adapter.estimate_cost(model=model, settings=dict(settings))
    if estimate is not None and (
        not isinstance(estimate, (int, float)) or isinstance(estimate, bool) or estimate < 0
    ):
        raise ImageGenerationServiceError(
            f"adapter.estimate_cost() must return a non-negative number or None, got {estimate!r}"
        )
    if estimate is not None and not (isinstance(cost_unit, str) and cost_unit.strip()):
        raise ImageGenerationServiceError(
            "cost_unit is required whenever adapter.estimate_cost() returns a real estimate"
        )
    if estimate is None and cost_unit is not None:
        raise ImageGenerationServiceError("cost_unit must be None when no cost estimate is available")

    # Owner-authorization prerequisite -- distinct from, and layered on
    # top of, the spending policy's own numeric ceilings below (Spending
    # Policy has no concept of authorization_ref at all; the manifest law
    # is what actually requires one for a cost-bearing manifest, L5).
    # Checked here so a missing reference is never discovered only after
    # a real spend has already happened.
    if estimate is not None and not (isinstance(authorization_ref, str) and authorization_ref.strip()):
        return ImageGenerationResult(
            outcome=OUTCOME_MISSING_AUTHORIZATION,
            cost_estimate=estimate,
            diagnostic=(
                "a real cost estimate is available but no owner authorization_ref "
                "was supplied; refusing before any spend or submission"
            ),
        )

    # 4. Spending-policy decision. No configuration of SpendingPolicy can
    # resolve an unknown cost to ALLOWED (see core.generation_spending_
    # policy's own module docstring) -- this module adds no override.
    spend_decision = sp.evaluate_spend(spending_policy, estimate, session_spent=session_spent)
    if spend_decision.outcome == sp.OUTCOME_DENIED:
        return ImageGenerationResult(
            outcome=OUTCOME_SPEND_DENIED, cost_estimate=estimate,
            diagnostic=_sanitize_diagnostic(spend_decision.reason),
        )
    if spend_decision.outcome == sp.OUTCOME_REQUIRES_APPROVAL:
        return ImageGenerationResult(
            outcome=OUTCOME_SPEND_REQUIRES_APPROVAL, cost_estimate=estimate,
            diagnostic=_sanitize_diagnostic(spend_decision.reason),
        )

    # 5. GenerationJob construction. The submission-fingerprint ambiguity
    # defense lives entirely in begin_generation_job() (BEGIN IMMEDIATE,
    # transactional) -- this module adds no second, racy pre-check of its
    # own and instead converts that one authoritative refusal into a
    # truthful soft outcome, exactly like every other runtime condition.
    try:
        job = gj.begin_generation_job(
            Capability.IMAGE_GENERATION, specialist, model, decision, settings,
            reference_assets=reference_assets, provider_idempotency_key=provider_idempotency_key,
            cost_estimate=estimate, cost_unit=cost_unit, max_retries=max_retries,
        )
    except gj.AmbiguousDuplicateSubmission as exc:
        fingerprint = gj.compute_submission_fingerprint(
            Capability.IMAGE_GENERATION.value, specialist, model, settings, reference_assets,
        )
        existing = gj.find_unresolved_by_fingerprint(fingerprint)
        return ImageGenerationResult(
            outcome=OUTCOME_AMBIGUOUS_DUPLICATE,
            lumina_job_id=existing.lumina_job_id if existing else None,
            provider_job_id=existing.provider_job_id if existing else None,
            job_status=existing.status if existing else None,
            cost_estimate=estimate,
            diagnostic=_sanitize_diagnostic(str(exc)),
        )

    # 6. Adapter submission -- never claim success on an ambiguous/failed
    # call. The job is left exactly as begin_generation_job() created it
    # (STATUS_UNKNOWN, no provider_job_id) so nothing here pretends a
    # submission reached the provider when it did not, or might not have.
    try:
        provider_job_id, raw_status = adapter.submit(
            model=model, settings=dict(settings), reference_assets=tuple(reference_assets),
            provider_idempotency_key=provider_idempotency_key,
        )
    except Exception as exc:
        return ImageGenerationResult(
            outcome=OUTCOME_SUBMISSION_FAILED, lumina_job_id=job.lumina_job_id,
            job_status=job.status, cost_estimate=estimate,
            diagnostic=_sanitize_diagnostic(f"{type(exc).__name__}: {exc}"),
        )
    job = gj.mark_submitted(job.lumina_job_id, provider_job_id, raw_status)

    # 7 + 8. Bounded, cooperatively-cancellable polling. `deadline` is a
    # monotonic-clock bound (never wall-clock), matching core.
    # process_manager.wait_for_completion()'s own idiom. STATUS_UNKNOWN is
    # never guessed into a terminal outcome -- it simply keeps the loop
    # going until either a real terminal status arrives or the bound is
    # hit (OUTCOME_TIMED_OUT).
    deadline = None if poll_timeout is None else time.monotonic() + poll_timeout
    timed_out = False
    while job.status not in gj.TERMINAL_STATUSES:
        if cancel_event is not None and cancel_event.is_set():
            try:
                job = adapter.cancel(job)
            except gj.JobStateConflict:
                # Already resolved by the time cancellation reached it --
                # re-fetch rather than assume any particular outcome.
                job = gj.get_generation_job(job.lumina_job_id)
            if job.status in gj.TERMINAL_STATUSES:
                break
            # Provider only ACKNOWLEDGED the request without confirming a
            # terminal outcome (core.generation_adapter.GenerationAdapter.
            # cancel()'s own documented contract) -- never assumed
            # cancelled; a later poll() is what actually resolves it,
            # within this same bound.

        if deadline is not None and time.monotonic() >= deadline:
            timed_out = True
            break

        raw_status = adapter.poll(job.provider_job_id)
        normalized = gj.normalize_provider_status(raw_status)
        if normalized != job.status:
            job = gj.update_status(job.lumina_job_id, normalized)
        if job.status in gj.TERMINAL_STATUSES:
            break

        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            time.sleep(min(poll_interval, remaining))
        else:
            time.sleep(poll_interval)

    if timed_out:
        return ImageGenerationResult(
            outcome=OUTCOME_TIMED_OUT, lumina_job_id=job.lumina_job_id,
            provider_job_id=job.provider_job_id, job_status=job.status, cost_estimate=estimate,
            diagnostic=f"polling bound exceeded; last observed provider status={job.status!r}",
        )

    if job.status == gj.STATUS_FAILED:
        return ImageGenerationResult(
            outcome=OUTCOME_PROVIDER_FAILED, lumina_job_id=job.lumina_job_id,
            provider_job_id=job.provider_job_id, job_status=job.status, cost_estimate=estimate,
            diagnostic=_sanitize_diagnostic(job.failure_class or "provider reported failure"),
        )

    if job.status == gj.STATUS_CANCELLED:
        return ImageGenerationResult(
            outcome=OUTCOME_CANCELLED, lumina_job_id=job.lumina_job_id,
            provider_job_id=job.provider_job_id, job_status=job.status, cost_estimate=estimate,
        )

    # job.status == STATUS_SUCCEEDED: 9/10/11/12/13 -- multi-output
    # discovery, retrieval, Lumina-owned durable ingestion, GeneratedArtifact
    # construction, one manifest per successfully ingested artifact.
    try:
        indices, multi = _discover_output_plan(adapter, job.provider_job_id)
    except Exception as exc:
        # Discovery failed BEFORE any per-output attempt could even be
        # made -- still a genuine "we could not retrieve what the provider
        # says exists" event, so it is recorded the same truthful way a
        # per-output failure below is, rather than left indistinguishable
        # from a job that genuinely had zero outputs (ingestion_state
        # would otherwise stay None either way).
        gj.set_ingestion_result(
            job.lumina_job_id, gj.INGESTION_FAILED,
            error=f"output discovery failed: {type(exc).__name__}: {exc}",
        )
        final_job = gj.get_generation_job(job.lumina_job_id)
        return ImageGenerationResult(
            outcome=OUTCOME_DISCOVERY_FAILED, lumina_job_id=job.lumina_job_id,
            provider_job_id=job.provider_job_id, job_status=final_job.status, cost_estimate=estimate,
            diagnostic=_sanitize_diagnostic(f"{type(exc).__name__}: {exc}"),
        )

    provenance = {"specialist": specialist, "model": model, "provider_job_id": job.provider_job_id}
    ingested = []
    failed_indices = []
    for index in indices:
        # A remote URL is never durable (substrate law) -- fetch() below
        # always returns bytes already in hand; ingest_artifact() is what
        # hashes and owns them. One sibling's failure never corrupts,
        # invalidates, or blocks another (core.generation_artifact.
        # ingest_artifact()'s own per-attempt-independence contract).
        try:
            if multi:
                data, mime_type = adapter.fetch_output(job.provider_job_id, index)
            else:
                data, mime_type = adapter.fetch_result(job.provider_job_id)
        except Exception as exc:
            # The fetch itself failed -- ingest_artifact() is never reached,
            # so nothing else will record this attempt. A fetch failure is
            # exactly as real an ingestion failure as a storage failure
            # (core.generation_artifact.ingest_artifact()'s own internal
            # failure path records the latter); this is the former's
            # equivalent, merged into the SAME truthful job-level aggregate.
            failed_indices.append(index)
            gj.set_ingestion_result(
                job.lumina_job_id, gj.INGESTION_FAILED,
                error=f"output {index} fetch failed: {type(exc).__name__}: {exc}",
            )
            continue
        try:
            artifact = ga.ingest_artifact(
                job.lumina_job_id, data, mime_type,
                metadata=dict(extra_metadata or {}), provenance=provenance,
                provider_output_index=index,
            )
        except Exception:
            # ga.ingest_artifact() already recorded this attempt's outcome
            # internally -- recording it again here would double-count one
            # real-world attempt as two.
            failed_indices.append(index)
            continue
        ingested.append(artifact)

    final_job = gj.get_generation_job(job.lumina_job_id)

    # One manifest per ingested artifact, built from the SAME final job
    # snapshot for every sibling -- so a partially-ingested job's earlier,
    # individually-successful artifacts still honestly carry
    # job_ingestion_state="partial" rather than a stale "ingested" snapshot
    # taken before a later sibling's failure was known (L7/L8: the
    # aggregate is truthful for every manifest built from it).
    try:
        manifests = tuple(
            gm.build_manifest(
                final_job, artifact, provider=manifest_provider, review_state=review_state,
                params=dict(settings), authorization_ref=authorization_ref,
            )
            for artifact in ingested
        )
    except gm.ManifestValidationError as exc:
        # Re-raised with the job id folded in (the artifacts ARE already
        # durable at this point) -- never laundered into a soft outcome:
        # the manifest law's refusal must stay loud (see this module's
        # docstring), it just shouldn't also strand the caller without a
        # way to find what was already made durable.
        raise type(exc)([f"job {final_job.lumina_job_id}: {msg}" for msg in exc.errors]) from exc

    if final_job.ingestion_state == gj.INGESTION_INGESTED:
        outcome = OUTCOME_SUCCESS
    elif final_job.ingestion_state == gj.INGESTION_PARTIAL:
        outcome = OUTCOME_PARTIAL
    elif final_job.ingestion_state == gj.INGESTION_FAILED:
        outcome = OUTCOME_INGESTION_FAILED
    else:
        outcome = OUTCOME_ZERO_OUTPUTS

    return ImageGenerationResult(
        outcome=outcome, lumina_job_id=job.lumina_job_id, provider_job_id=job.provider_job_id,
        job_status=final_job.status, cost_estimate=estimate,
        artifacts=tuple(ingested), manifests=manifests,
        failed_output_indices=tuple(failed_indices),
    )
