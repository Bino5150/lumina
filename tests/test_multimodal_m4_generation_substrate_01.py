"""
tests/test_multimodal_m4_generation_substrate_01.py -- lifecycle and
frozen-law tests for MULTIMODAL-M4-GENERATION-SUBSTRATE-DESIGN-01
(core/generation_job.py, core/generation_artifact.py,
core/generation_spending_policy.py, core/generation_adapter.py).

No real provider, no network, no credentials -- FakeAdapter below is the
only adapter exercised, matching this campaign's scope (see
MULTIMODAL_M4_IMAGE_GENERATION_SOURCE_VET_2026-09-17.md Sec 7: the real
Higgsfield HTTP contract is a separate, later campaign). Every test
isolates config.DB_PATH to tmp_path, same convention
tests/test_context_checkpoints.py already uses; config.DATA_DIR is already
isolated for the whole test session by tests/conftest.py.
"""
import hashlib

import pytest

import config
import core.generation_job as gj
import core.generation_artifact as ga
import core.generation_spending_policy as sp
from core.capability_router import Capability, RoutingDecision


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "lumina.db"))
    return tmp_path


def _routed_decision(specialist="fake_provider", capability=Capability.IMAGE_GENERATION.value):
    return RoutingDecision(
        capability=capability,
        outcome="routed",
        selected=specialist,
        classification="explicit",
        reason="test fixture",
    )


def _begin(specialist="fake_provider", model="fake-model-1", settings=None, refs=(), **kw):
    return gj.begin_generation_job(
        Capability.IMAGE_GENERATION, specialist, model,
        _routed_decision(specialist),
        settings if settings is not None else {"prompt": "a red bicycle"},
        reference_assets=refs,
        **kw,
    )


def _succeeded_job(**kw):
    job = _begin(**kw)
    gj.mark_submitted(job.lumina_job_id, "provider-1", "queued")
    return gj.update_status(job.lumina_job_id, gj.STATUS_SUCCEEDED)


class FakeAdapter:
    """Minimal core.generation_adapter.GenerationAdapter implementation.
    In-memory only -- no network, no credentials. Every submitted job
    reports "succeeded" on first poll() so tests don't need real waiting."""

    def __init__(self):
        self._jobs = {}
        self._next_id = 0

    def submit(self, *, model, settings, reference_assets, provider_idempotency_key=None):
        self._next_id += 1
        provider_job_id = f"fake-job-{self._next_id}"
        self._jobs[provider_job_id] = "succeeded"
        return provider_job_id, "queued"

    def poll(self, provider_job_id):
        return self._jobs[provider_job_id]

    def fetch_result(self, provider_job_id):
        return b"\x89PNG\r\n\x1a\nfake-image-bytes", "image/png"

    def describe_model(self, model):
        return {"aspect_ratios": ["1:1", "16:9"]}

    def estimate_cost(self, *, model, settings):
        return 0.04


# ---------------------------------------------------------------------------
# GenerationJob: creation requires an admitted route (never improvised)
# ---------------------------------------------------------------------------

def test_begin_requires_routed_outcome():
    unrouted = RoutingDecision(capability=Capability.IMAGE_GENERATION.value, outcome="disabled")
    with pytest.raises(gj.RouteNotAdmitted):
        gj.begin_generation_job(Capability.IMAGE_GENERATION, "fake_provider", "fake-model-1",
                                 unrouted, {"prompt": "x"})


def test_begin_requires_specialist_matches_decision():
    decision = _routed_decision(specialist="fake_provider")
    with pytest.raises(gj.RouteNotAdmitted):
        gj.begin_generation_job(Capability.IMAGE_GENERATION, "someone_else", "fake-model-1",
                                 decision, {"prompt": "x"})


def test_begin_rejects_unknown_capability():
    decision = _routed_decision()
    with pytest.raises(gj.UnknownCapability):
        gj.begin_generation_job("not_a_real_capability", "fake_provider", "fake-model-1",
                                 decision, {"prompt": "x"})


def test_begin_creates_unknown_status_job():
    job = _begin()
    assert job.status == gj.STATUS_UNKNOWN
    assert job.provider_job_id is None
    assert job.submitted_at is None
    assert job.retry_count == 0
    assert job.ingestion_state is None


def test_integration_with_real_m1_resolve_capability():
    """Proves the substrate actually consumes M1's real resolve_capability()
    output, not just a hand-built RoutingDecision -- and that M1 needed no
    changes to plug in here."""
    from core.capability_router import (
        CapabilityRegistry, RoutingPolicy, CapabilityRoute, SpecialistRecord,
        EvidenceClass, resolve_capability, LANE_SPECIALIST,
    )
    registry = CapabilityRegistry([
        SpecialistRecord(
            name="fake_provider", kind="external", local=False,
            capabilities={Capability.IMAGE_GENERATION: EvidenceClass.EVIDENCED},
        )
    ])
    policy = RoutingPolicy(routes={
        Capability.IMAGE_GENERATION.value: CapabilityRoute(mode=LANE_SPECIALIST, specialist="fake_provider"),
    })
    decision = resolve_capability(registry, policy, Capability.IMAGE_GENERATION)
    assert decision.outcome == "routed"

    job = gj.begin_generation_job(Capability.IMAGE_GENERATION, "fake_provider", "fake-model-1",
                                   decision, {"prompt": "integration test"})
    assert job.status == gj.STATUS_UNKNOWN
    assert job.route_decision["selected"] == "fake_provider"


# ---------------------------------------------------------------------------
# Idempotency: submission_fingerprint + ambiguous-duplicate detection
# (Sol's review, folded into the frozen design before this code was written)
# ---------------------------------------------------------------------------

def test_fingerprint_deterministic():
    a = gj.compute_submission_fingerprint("image_generation", "p", "m", {"prompt": "x"}, [])
    b = gj.compute_submission_fingerprint("image_generation", "p", "m", {"prompt": "x"}, [])
    assert a == b


def test_fingerprint_differs_on_settings():
    a = gj.compute_submission_fingerprint("image_generation", "p", "m", {"prompt": "x"}, [])
    b = gj.compute_submission_fingerprint("image_generation", "p", "m", {"prompt": "y"}, [])
    assert a != b


def test_duplicate_unresolved_submission_is_refused():
    """The exact ambiguity Sol flagged: a job still in flight (never mind
    why -- pre-submission or a timed-out network call) must block a second,
    identical submission rather than silently double-spending."""
    _begin(settings={"prompt": "same"})
    with pytest.raises(gj.AmbiguousDuplicateSubmission):
        _begin(settings={"prompt": "same"})


def test_resubmission_allowed_after_terminal_status():
    job = _begin(settings={"prompt": "same-again"})
    gj.mark_submitted(job.lumina_job_id, "provider-1", "queued")
    gj.update_status(job.lumina_job_id, gj.STATUS_FAILED)
    # Same fingerprint, but the prior job is terminal -- a fresh attempt is legitimate.
    second = _begin(settings={"prompt": "same-again"})
    assert second.lumina_job_id != job.lumina_job_id


def test_find_unresolved_by_fingerprint():
    job = _begin(settings={"prompt": "findme"})
    fp = job.submission_fingerprint
    assert gj.find_unresolved_by_fingerprint(fp).lumina_job_id == job.lumina_job_id
    gj.mark_submitted(job.lumina_job_id, "provider-1", "queued")
    gj.update_status(job.lumina_job_id, gj.STATUS_SUCCEEDED)
    assert gj.find_unresolved_by_fingerprint(fp) is None


# ---------------------------------------------------------------------------
# Status normalization -- fail-closed, never a fabricated positive/negative
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("queued", gj.STATUS_QUEUED), ("pending", gj.STATUS_QUEUED), ("created", gj.STATUS_QUEUED),
    ("running", gj.STATUS_RUNNING), ("processing", gj.STATUS_RUNNING), ("in_progress", gj.STATUS_RUNNING),
    ("succeeded", gj.STATUS_SUCCEEDED), ("completed", gj.STATUS_SUCCEEDED), ("done", gj.STATUS_SUCCEEDED),
    ("failed", gj.STATUS_FAILED), ("error", gj.STATUS_FAILED),
    ("cancelled", gj.STATUS_CANCELLED), ("canceled", gj.STATUS_CANCELLED), ("aborted", gj.STATUS_CANCELLED),
    ("QUEUED", gj.STATUS_QUEUED),  # case-insensitive
])
def test_normalize_known_synonyms(raw, expected):
    assert gj.normalize_provider_status(raw) == expected


@pytest.mark.parametrize("raw", ["something_new", "", None, 42, {"status": "succeeded"}, ["succeeded"]])
def test_normalize_unrecognized_becomes_unknown(raw):
    assert gj.normalize_provider_status(raw) == gj.STATUS_UNKNOWN


# ---------------------------------------------------------------------------
# mark_submitted / update_status CAS discipline
# ---------------------------------------------------------------------------

def test_mark_submitted_transitions_from_unknown():
    job = _begin()
    updated = gj.mark_submitted(job.lumina_job_id, "provider-42", "queued")
    assert updated.provider_job_id == "provider-42"
    assert updated.status == gj.STATUS_QUEUED
    assert updated.submitted_at is not None


def test_mark_submitted_twice_conflicts():
    job = _begin()
    gj.mark_submitted(job.lumina_job_id, "provider-42", "queued")
    with pytest.raises(gj.JobStateConflict):
        gj.mark_submitted(job.lumina_job_id, "provider-43", "queued")


def test_update_status_refuses_transition_out_of_terminal():
    job = _begin()
    gj.mark_submitted(job.lumina_job_id, "provider-1", "queued")
    gj.update_status(job.lumina_job_id, gj.STATUS_SUCCEEDED)
    with pytest.raises(gj.JobStateConflict):
        gj.update_status(job.lumina_job_id, gj.STATUS_FAILED)


def test_update_status_sets_terminal_at_and_failure_class():
    job = _begin()
    gj.mark_submitted(job.lumina_job_id, "provider-1", "queued")
    updated = gj.update_status(job.lumina_job_id, gj.STATUS_FAILED, failure_class="content_policy")
    assert updated.terminal_at is not None
    assert updated.failure_class == "content_policy"


def test_update_status_rejects_noncanonical_status():
    job = _begin()
    with pytest.raises(gj.GenerationJobError):
        gj.update_status(job.lumina_job_id, "some_made_up_status")


def test_get_unknown_job_raises():
    with pytest.raises(gj.JobNotFound):
        gj.get_generation_job("does-not-exist")


def test_init_db_idempotent():
    gj.init_generation_job_db()
    gj.init_generation_job_db()  # must not raise


# ---------------------------------------------------------------------------
# Bounded retries -- never retried past the cap
# ---------------------------------------------------------------------------

def test_bump_retry_within_budget():
    job = _begin(max_retries=2)
    once = gj.bump_retry(job.lumina_job_id)
    assert once.retry_count == 1
    twice = gj.bump_retry(job.lumina_job_id)
    assert twice.retry_count == 2


def test_bump_retry_exhausted_raises_and_does_not_mutate():
    job = _begin(max_retries=1)
    gj.bump_retry(job.lumina_job_id)
    with pytest.raises(gj.RetryBudgetExhausted):
        gj.bump_retry(job.lumina_job_id)
    assert gj.get_generation_job(job.lumina_job_id).retry_count == 1


# ---------------------------------------------------------------------------
# Cost: estimate and actual are independent fields, never conflated
# ---------------------------------------------------------------------------

def test_cost_estimate_and_actual_are_independent():
    job = _begin(cost_estimate=0.05, cost_unit="usd")
    assert job.cost_estimate == 0.05
    assert job.cost_actual is None
    updated = gj.record_cost_actual(job.lumina_job_id, 0.07)
    assert updated.cost_actual == 0.07
    assert updated.cost_estimate == 0.05  # untouched


# ---------------------------------------------------------------------------
# Persistence round-trip fidelity
# ---------------------------------------------------------------------------

def test_round_trip_preserves_route_decision_and_references():
    decision = _routed_decision()
    job = gj.begin_generation_job(
        Capability.IMAGE_GENERATION, "fake_provider", "fake-model-1", decision,
        {"prompt": "a bicycle", "aspect_ratio": "16:9"},
        reference_assets=[("style", "artifact-abc"), ("identity", "artifact-def")],
    )
    reloaded = gj.get_generation_job(job.lumina_job_id)
    assert reloaded.route_decision["selected"] == "fake_provider"
    assert reloaded.route_decision["outcome"] == "routed"
    assert set(reloaded.reference_assets) == {("style", "artifact-abc"), ("identity", "artifact-def")}
    assert reloaded.settings == {"prompt": "a bicycle", "aspect_ratio": "16:9"}


# ---------------------------------------------------------------------------
# GeneratedArtifact: only constructible via successful ingestion
# ---------------------------------------------------------------------------

def test_ingest_requires_succeeded_job():
    job = _begin()  # still STATUS_UNKNOWN
    with pytest.raises(ga.IngestionError):
        ga.ingest_artifact(job.lumina_job_id, b"bytes", "image/png")
    # Precondition violation, not an ingestion attempt -- ingestion_state
    # is never touched.
    assert gj.get_generation_job(job.lumina_job_id).ingestion_state is None


def test_ingest_success_produces_artifact_and_updates_job():
    job = _succeeded_job()
    artifact = ga.ingest_artifact(job.lumina_job_id, b"\x89PNGfakebytes", "image/png",
                                   metadata={"width": 512, "height": 512},
                                   provenance={"model": "fake-model-1"})
    assert artifact.sha256 == hashlib.sha256(b"\x89PNGfakebytes").hexdigest()
    assert artifact.mime_type == "image/png"
    assert artifact.metadata == {"width": 512, "height": 512}
    updated_job = gj.get_generation_job(job.lumina_job_id)
    assert updated_job.ingestion_state == gj.INGESTION_INGESTED
    assert updated_job.status == gj.STATUS_SUCCEEDED  # provider status untouched by ingestion


def test_ingest_bytes_readable_back_and_hash_verified():
    job = _succeeded_job()
    artifact = ga.ingest_artifact(job.lumina_job_id, b"round-trip-bytes", "application/octet-stream")
    assert ga.get_artifact_bytes(artifact.artifact_id) == b"round-trip-bytes"


def test_ingest_refuses_second_ingestion_for_same_job():
    job = _succeeded_job()
    ga.ingest_artifact(job.lumina_job_id, b"first", "image/png")
    with pytest.raises(ga.IngestionError):
        ga.ingest_artifact(job.lumina_job_id, b"second", "image/png")


def test_ingest_rejects_empty_data_without_touching_job():
    job = _succeeded_job()
    with pytest.raises(ga.IngestionError):
        ga.ingest_artifact(job.lumina_job_id, b"", "image/png")
    updated = gj.get_generation_job(job.lumina_job_id)
    assert updated.status == gj.STATUS_SUCCEEDED
    assert updated.ingestion_state is None  # caller-input error, caught before any write attempt


def test_ingest_write_failure_sets_ingestion_failed_not_generation_failed(monkeypatch):
    """Sol's flagged state, made concrete: provider succeeded, a remote
    result existed, but Lumina's own local ingestion failed. Must be
    distinguishable from both a generation failure and a usable artifact --
    status stays SUCCEEDED, ingestion_state becomes FAILED, no artifact
    row exists."""
    job = _succeeded_job()

    def _boom(path, data):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(ga, "_write_atomic", _boom)
    with pytest.raises(ga.IngestionError):
        ga.ingest_artifact(job.lumina_job_id, b"some-bytes", "image/png")

    updated = gj.get_generation_job(job.lumina_job_id)
    assert updated.status == gj.STATUS_SUCCEEDED
    assert updated.ingestion_state == gj.INGESTION_FAILED
    assert updated.ingestion_error is not None
    assert ga.list_artifacts_for_job(job.lumina_job_id) == []


def test_artifact_tamper_on_disk_is_detected():
    job = _succeeded_job()
    artifact = ga.ingest_artifact(job.lumina_job_id, b"original-bytes", "image/png")
    with open(artifact.local_path, "wb") as f:
        f.write(b"tampered-bytes")
    with pytest.raises(ga.IngestionError):
        ga.get_artifact_bytes(artifact.artifact_id)


def test_provenance_secret_shapes_are_redacted():
    job = _succeeded_job()
    artifact = ga.ingest_artifact(
        job.lumina_job_id, b"bytes", "image/png",
        provenance={"note": "used key sk-abcdefghijklmnopqrstuvwxyz123456"},
    )
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in artifact.provenance["note"]
    assert "[REDACTED]" in artifact.provenance["note"]


def test_get_unknown_artifact_raises():
    with pytest.raises(ga.ArtifactNotFound):
        ga.get_generation_artifact("does-not-exist")


# ---------------------------------------------------------------------------
# Spending policy: pure, ceiling-based, never silently-free on unknown cost
# ---------------------------------------------------------------------------

def test_within_ceiling_allowed():
    policy = sp.SpendingPolicy(single_job_ceiling=1.0, session_ceiling=5.0)
    decision = sp.evaluate_spend(policy, cost_estimate=0.5, session_spent=1.0)
    assert decision.outcome == sp.OUTCOME_ALLOWED


def test_over_single_job_ceiling_requires_approval():
    policy = sp.SpendingPolicy(single_job_ceiling=1.0)
    decision = sp.evaluate_spend(policy, cost_estimate=7.0)
    assert decision.outcome == sp.OUTCOME_REQUIRES_APPROVAL


def test_over_session_ceiling_requires_approval():
    policy = sp.SpendingPolicy(session_ceiling=2.0)
    decision = sp.evaluate_spend(policy, cost_estimate=1.5, session_spent=1.0)
    assert decision.outcome == sp.OUTCOME_REQUIRES_APPROVAL


def test_unknown_cost_fails_closed_by_default():
    policy = sp.SpendingPolicy(require_estimate=True)  # fail_closed_on_unknown_cost=True default
    decision = sp.evaluate_spend(policy, cost_estimate=None)
    assert decision.outcome == sp.OUTCOME_DENIED


def test_unknown_cost_requires_approval_when_not_fail_closed():
    policy = sp.SpendingPolicy(require_estimate=True, fail_closed_on_unknown_cost=False)
    decision = sp.evaluate_spend(policy, cost_estimate=None)
    assert decision.outcome == sp.OUTCOME_REQUIRES_APPROVAL


def test_unknown_cost_allowed_when_not_required():
    policy = sp.SpendingPolicy(require_estimate=False)
    decision = sp.evaluate_spend(policy, cost_estimate=None)
    assert decision.outcome == sp.OUTCOME_ALLOWED


def test_evaluate_spend_is_pure_and_deterministic():
    policy = sp.SpendingPolicy(single_job_ceiling=1.0)
    a = sp.evaluate_spend(policy, cost_estimate=0.5)
    b = sp.evaluate_spend(policy, cost_estimate=0.5)
    assert a == b


def test_negative_ceiling_rejected():
    with pytest.raises(ValueError):
        sp.SpendingPolicy(single_job_ceiling=-1.0)


def test_negative_cost_estimate_rejected():
    policy = sp.SpendingPolicy()
    with pytest.raises(ValueError):
        sp.evaluate_spend(policy, cost_estimate=-0.01)


# ---------------------------------------------------------------------------
# Full lifecycle through a FakeAdapter -- proves the seam is usable end to
# end without a real provider, network, or credentials.
# ---------------------------------------------------------------------------

def test_full_lifecycle_via_fake_adapter():
    adapter = FakeAdapter()
    decision = _routed_decision(specialist="fake_provider")
    model = "fake-model-1"
    settings = {"prompt": "a lighthouse at dawn", "aspect_ratio": "16:9"}

    policy = sp.SpendingPolicy(single_job_ceiling=1.0, session_ceiling=5.0)
    estimate = adapter.estimate_cost(model=model, settings=settings)
    spend_decision = sp.evaluate_spend(policy, estimate, session_spent=0.0)
    assert spend_decision.outcome == sp.OUTCOME_ALLOWED

    job = gj.begin_generation_job(Capability.IMAGE_GENERATION, "fake_provider", model,
                                   decision, settings, cost_estimate=estimate, cost_unit="usd")

    provider_job_id, raw_status = adapter.submit(model=model, settings=settings, reference_assets=())
    job = gj.mark_submitted(job.lumina_job_id, provider_job_id, raw_status)
    assert job.status == gj.STATUS_QUEUED

    raw_status = adapter.poll(provider_job_id)
    job = gj.update_status(job.lumina_job_id, gj.normalize_provider_status(raw_status))
    assert job.status == gj.STATUS_SUCCEEDED

    data, mime_type = adapter.fetch_result(provider_job_id)
    artifact = ga.ingest_artifact(job.lumina_job_id, data, mime_type,
                                   metadata={"width": 1920, "height": 1080},
                                   provenance={"model": model})

    final = gj.get_generation_job(job.lumina_job_id)
    assert final.ingestion_state == gj.INGESTION_INGESTED
    assert ga.get_artifact_bytes(artifact.artifact_id) == data
    assert artifact.mime_type == "image/png"
