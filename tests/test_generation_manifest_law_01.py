"""
tests/test_generation_manifest_law_01.py -- MULTIMODAL-M4-GENERATION-MANIFEST-LAW-01

Law-by-law proof for core/generation_manifest.py. Every test name carries the
law it proves. Fully offline: no DB, no network, no Qt, no real provider
identities -- fake providers are injected via monkeypatch, and the production
provider vocabulary itself is asserted to ship deliberately EMPTY (Higgsfield's
entry lands deliberately during the M4 integration rollout, never here).
"""
from __future__ import annotations

import inspect
import json

import pytest

import core.generation_manifest as gm
from core.generation_artifact import GeneratedArtifact
from core.generation_job import GenerationJob
from core.generation_manifest import (
    MANIFEST_FIELDS,
    MissingAuthorizationRef,
    SecretMaterialInManifest,
    UnknownManifestCapability,
    UnknownManifestProvider,
    build_manifest,
    serialize_manifest,
    validate_manifest,
)

FAKE_PROVIDERS = frozenset({"fake-flickr", "fake-motion"})


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _fake_provider_vocabulary(request, monkeypatch):
    """Inject FAKE provider identities for every test by default, so laws
    other than L3 can be exercised without touching the production
    vocabulary. Tests declaring the `real_provider_vocabulary` fixture opt
    out: they see the production vocabulary exactly as shipped -- empty."""
    if "real_provider_vocabulary" in request.fixturenames:
        return
    monkeypatch.setattr(gm, "CANONICAL_PROVIDERS", FAKE_PROVIDERS)


@pytest.fixture
def fake_providers():
    """Handle for tests that want to reference the injected fake set."""
    return FAKE_PROVIDERS


@pytest.fixture
def real_provider_vocabulary():
    """Opt-out declaration: this test needs the production provider
    vocabulary UNTOUCHED (deliberately empty at freeze)."""


def _job(**over) -> GenerationJob:
    base = dict(
        lumina_job_id="job-1",
        capability="image_generation",
        specialist="generation:fake-flickr",
        model="fake-model-1",
        route_decision={},
        settings={"size": "512x512"},
        reference_assets=(),
        submission_fingerprint="fp-1",
        provider_job_id="prov-req-1",
        provider_idempotency_key=None,
        status="succeeded",
        failure_class=None,
        cost_estimate=0.25,
        cost_actual=None,
        cost_unit="usd",
        ingestion_state="ingested",
        ingestion_error=None,
        retry_count=0,
        max_retries=1,
        created_at="2026-09-17T10:00:00+00:00",
        submitted_at="2026-09-17T10:00:01+00:00",
        terminal_at="2026-09-17T10:02:00+00:00",
    )
    base.update(over)
    return GenerationJob(**base)


def _artifact(**over) -> GeneratedArtifact:
    base = dict(
        artifact_id="art-1",
        lumina_job_id="job-1",
        local_path="/data/artifacts/ab/art-1",
        sha256="0" * 64,
        mime_type="image/png",
        size_bytes=1234,
        metadata={},
        provenance={},
        created_at="2026-09-17T10:02:05+00:00",
        provider_output_index=0,
    )
    base.update(over)
    return GeneratedArtifact(**base)


def _manifest(**over) -> dict:
    base = {
        "capability": "image_generation",
        "provider": "fake-flickr",
        "model": "fake-model-1",
        "lumina_job_id": "job-1",
        "provider_job_id": "prov-req-1",
        "artifact_id": "art-1",
        "artifact_path": "/data/artifacts/ab/art-1",
        "provider_output_index": 0,
        "job_ingestion_state": "ingested",
        "review_state": "completed",
        "cost_estimate": 0.25,
        "cost_actual": 0.25,
        "cost_unit": "usd",
        "authorization_ref": "owner-envelope-77",
        "created_utc": "2026-09-17T10:02:05+00:00",
        "params": {"size": "512x512"},
        "failure_class": None,
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# L1 + L6 -- exact allowed fields; extra fields fail closed
# ---------------------------------------------------------------------------

def test_L1_exact_set_missing_field_fails_closed():
    m = _manifest()
    del m["provider"]
    with pytest.raises(gm.ManifestValidationError) as exc:
        validate_manifest(m)
    assert "provider" in str(exc.value)


def test_L1_every_schema_field_is_required_even_when_value_is_none():
    m = _manifest(failure_class=None, cost_actual=None)  # None-valued: still present
    assert validate_manifest(m)["failure_class"] is None  # presence != truthiness


def test_L6_extra_field_fails_closed_even_one_named_success():
    for extra in ({"success": True}, {"status": "succeeded"}, {"ok": 1}):
        m = _manifest(**extra)
        with pytest.raises(gm.ManifestValidationError) as exc:
            validate_manifest(m)
        assert "extra" in str(exc.value)


def test_L1_L6_exact_set_roundtrips_clean():
    validated = validate_manifest(_manifest())
    assert set(validated.keys()) == set(MANIFEST_FIELDS)


# ---------------------------------------------------------------------------
# L2 -- capability and provider are separate axes
# ---------------------------------------------------------------------------

def test_L2_capability_and_provider_are_independent_axes(fake_providers):
    # same capability, two different providers: both lawful
    validate_manifest(_manifest(provider="fake-flickr"))
    validate_manifest(_manifest(provider="fake-motion"))


def test_L2_swapping_across_axes_fails_on_both(fake_providers):
    with pytest.raises(UnknownManifestCapability):
        validate_manifest(_manifest(capability="fake-flickr"))
    with pytest.raises(UnknownManifestProvider):
        validate_manifest(_manifest(provider="image_generation"))


def test_L2_capability_enum_value_accepted_and_normalized():
    from core.capability_router import Capability
    m = _manifest(capability=Capability.IMAGE_GENERATION)
    assert validate_manifest(m)["capability"] == "image_generation"


# ---------------------------------------------------------------------------
# L3 -- missing provider registration fails closed
# ---------------------------------------------------------------------------

def test_L3_unknown_provider_fails_closed(fake_providers):
    with pytest.raises(UnknownManifestProvider) as exc:
        validate_manifest(_manifest(provider="fake-unknown"))
    assert "unknown provider" in str(exc.value)


def test_L3_production_provider_vocabulary_ships_deliberately_empty(real_provider_vocabulary):
    # The frozen law: no provider is registered at freeze time. Entries are
    # added by deliberate owner-authorized production edits only.
    assert gm.CANONICAL_PROVIDERS == frozenset()


def test_L3_higgsfield_rejected_until_deliberate_registration(real_provider_vocabulary):
    # The guard earning its keep, verbatim: with the REAL production
    # vocabulary (no fake injection), the first Higgsfield manifest is
    # refused. That refusal is the paperwork working -- NOT a bug, and NOT
    # fixed by adding the entry here. Fake providers above exist so the
    # guard's mechanics can be proven without polluting production.
    with pytest.raises(UnknownManifestProvider) as exc:
        validate_manifest(_manifest(provider="higgsfield"))
    assert "higgsfield" in str(exc.value)
    assert "deliberate" in str(exc.value)


def test_L3_provider_must_be_plain_string_not_enum():
    from core.capability_router import Capability
    with pytest.raises(UnknownManifestProvider):
        validate_manifest(_manifest(provider=Capability.IMAGE_GENERATION))


# ---------------------------------------------------------------------------
# L4 -- unknown capability fails closed
# ---------------------------------------------------------------------------

def test_L4_unknown_capability_fails_closed():
    for bad in ("3d_generation", "higgsfield", "image_generation_extra", 123, None):
        with pytest.raises(UnknownManifestCapability):
            validate_manifest(_manifest(capability=bad))


# ---------------------------------------------------------------------------
# L5 -- missing owner-authorization reference fails closed for cost-bearing
# ---------------------------------------------------------------------------

def test_L5_cost_estimate_without_authorization_ref_fails_closed():
    with pytest.raises(MissingAuthorizationRef):
        validate_manifest(_manifest(authorization_ref=None))


def test_L5_empty_authorization_ref_is_not_a_reference():
    with pytest.raises(MissingAuthorizationRef):
        validate_manifest(_manifest(authorization_ref="   "))


def test_L5_actual_cost_alone_is_also_cost_bearing():
    with pytest.raises(MissingAuthorizationRef):
        validate_manifest(_manifest(cost_estimate=None, cost_actual=0.5,
                                    authorization_ref=None))


def test_L5_zero_cost_job_without_authorization_ref_is_lawful():
    m = _manifest(cost_estimate=None, cost_actual=None, cost_unit=None,
                  authorization_ref=None)
    assert validate_manifest(m)["authorization_ref"] is None


# ---------------------------------------------------------------------------
# L7 -- submitted / completed / ingested / reviewed never collapse
# ---------------------------------------------------------------------------

def test_L7_no_single_success_state_exists_in_the_schema():
    # "success" cannot even be expressed -- exact-set refuses the field (L6),
    # and no manifest field aggregates the lifecycle axes.
    assert "success" not in MANIFEST_FIELDS
    assert "status" not in MANIFEST_FIELDS


def test_L7_reviewed_and_partial_ingestion_coexist_without_collapse():
    # Owner reviewed the one good sibling of a partially-ingested job:
    # both truths stay independently observable in one lawful manifest.
    m = _manifest(job_ingestion_state="partial", review_state="reviewed")
    validated = validate_manifest(m)
    assert validated["job_ingestion_state"] == "partial"
    assert validated["review_state"] == "reviewed"


def test_L7_completed_provider_state_is_not_owner_review():
    # ingested locally + owner-review still pending: two axes, both visible.
    m = _manifest(job_ingestion_state="ingested", review_state="submitted")
    validated = validate_manifest(m)
    assert validated["review_state"] == "submitted"


def test_L7_invalid_review_state_fails_closed():
    for bad in ("done", "success", "ingested", None, 3):
        with pytest.raises(gm.ManifestValidationError):
            validate_manifest(_manifest(review_state=bad))


# ---------------------------------------------------------------------------
# L8 -- partial / multi-output results cannot be laundered
# ---------------------------------------------------------------------------

def test_L8_partial_ingestion_snapshots_verbatim_from_job_record():
    job = _job(ingestion_state="partial")
    artifact = _artifact(provider_output_index=0)
    m = build_manifest(job, artifact, provider="fake-flickr",
                       review_state="completed",
                       authorization_ref="env-1")
    assert m["job_ingestion_state"] == "partial"  # never upgraded


def test_L8_builder_has_no_ingestion_state_override():
    # The laundering door doesn't exist: the signature carries no
    # job_ingestion_state (or capability/model/cost) parameter at all.
    sig = inspect.signature(build_manifest)
    for forbidden in ("job_ingestion_state", "capability", "model",
                      "cost_estimate", "cost_actual"):
        assert forbidden not in sig.parameters


def test_L8_job_ingestion_state_required_in_every_manifest():
    m = _manifest()
    del m["job_ingestion_state"]
    with pytest.raises(gm.ManifestValidationError):
        validate_manifest(m)


def test_L8_junk_ingestion_snapshot_fails_closed():
    with pytest.raises(gm.ManifestValidationError):
        validate_manifest(_manifest(job_ingestion_state="all_good"))


# ---------------------------------------------------------------------------
# L9 -- estimate and actual cost remain separate
# ---------------------------------------------------------------------------

def test_L9_estimate_and_actual_survive_as_distinct_fields():
    m = _manifest(cost_estimate=0.25, cost_actual=0.30)
    validated = validate_manifest(m)
    assert validated["cost_estimate"] == 0.25
    assert validated["cost_actual"] == 0.30


def test_L9_no_merged_cost_field_possible():
    with pytest.raises(gm.ManifestValidationError):
        validate_manifest(_manifest(cost=0.55))  # extra field, exact-set law


def test_L9_negative_or_boolean_cost_fails_closed():
    for bad in (-0.01, True, False, "0.25"):
        with pytest.raises(gm.ManifestValidationError):
            validate_manifest(_manifest(cost_estimate=bad))


def test_L9_cost_unit_required_when_cost_present():
    with pytest.raises(gm.ManifestValidationError):
        validate_manifest(_manifest(cost_unit=None))


# ---------------------------------------------------------------------------
# L10 -- no credential/secret material can enter the manifest
# ---------------------------------------------------------------------------

def test_L10_secret_key_marker_in_params_refused():
    with pytest.raises(SecretMaterialInManifest):
        validate_manifest(_manifest(params={"api_key": "harmless-value"}))


def test_L10_secret_value_shape_in_params_refused():
    with pytest.raises(SecretMaterialInManifest):
        validate_manifest(_manifest(params={"note": "sk-abcdefghijklmnopqrst"}))


def test_L10_bearer_token_in_authorization_ref_refused():
    with pytest.raises(SecretMaterialInManifest):
        validate_manifest(_manifest(authorization_ref="Bearer abcdefghijklmnopqr"))


def test_L10_secret_value_shape_in_plain_field_refused():
    with pytest.raises(SecretMaterialInManifest):
        validate_manifest(_manifest(model="ghp_" + "a" * 24))


# ---------------------------------------------------------------------------
# Structural laws: local path, ordinal, provider id, timestamps, params cap
# ---------------------------------------------------------------------------

def test_artifact_path_must_be_local_never_remote():
    for bad in ("https://cdn.example.com/x.png", "http://cdn.example.com/x.png",
                "//cdn.example.com/x.png"):
        with pytest.raises(gm.ManifestValidationError) as exc:
            validate_manifest(_manifest(artifact_path=bad))
        assert "LOCAL" in str(exc.value)


def test_provider_output_index_int_zero_ok_negative_and_bool_refused():
    assert validate_manifest(_manifest(provider_output_index=0))["provider_output_index"] == 0
    for bad in (-1, True, False, 1.5, "0", None):
        with pytest.raises(gm.ManifestValidationError):
            validate_manifest(_manifest(provider_output_index=bad))


def test_manifest_of_never_submitted_job_refused_by_builder():
    job = _job(provider_job_id=None)
    with pytest.raises(gm.ManifestValidationError):
        build_manifest(job, _artifact(), provider="fake-flickr",
                       review_state="completed", authorization_ref="env-1")


def test_builder_requires_assigned_output_ordinal():
    with pytest.raises(gm.ManifestValidationError):
        build_manifest(_job(), _artifact(provider_output_index=None),
                       provider="fake-flickr", review_state="completed",
                       authorization_ref="env-1")


def test_created_utc_must_be_timezone_aware():
    with pytest.raises(gm.ManifestValidationError):
        validate_manifest(_manifest(created_utc="2026-09-17T10:02:05"))  # naive
    validate_manifest(_manifest(created_utc="2026-09-17T10:02:05+00:00"))


def test_params_must_be_mapping_json_serializable_and_capped():
    with pytest.raises(gm.ManifestValidationError):
        validate_manifest(_manifest(params="not-a-mapping"))
    with pytest.raises(gm.ManifestValidationError):
        validate_manifest(_manifest(params={"x": object()}))
    with pytest.raises(gm.ManifestValidationError):
        validate_manifest(_manifest(params={"blob": "x" * (gm.MAX_PARAMS_BYTES + 1)}))


def test_validate_rejects_non_mapping():
    for bad in ("nope", 42, None, ["a"], b"bytes"):
        with pytest.raises(gm.ManifestValidationError):
            validate_manifest(bad)


# ---------------------------------------------------------------------------
# Round-trip: the write path IS the law path
# ---------------------------------------------------------------------------

def test_serialize_validates_then_roundtrips_exact_set(fake_providers):
    payload = serialize_manifest(_manifest())
    revived = json.loads(payload)
    assert validate_manifest(revived) == validate_manifest(_manifest())
    assert set(revived.keys()) == set(MANIFEST_FIELDS)


def test_serialize_refuses_what_validate_refuses(fake_providers):
    with pytest.raises(UnknownManifestProvider):
        serialize_manifest(_manifest(provider="fake-unknown"))


def test_full_builder_path_from_records_is_lawful(fake_providers):
    m = build_manifest(
        _job(cost_estimate=0.25, cost_actual=0.30),
        _artifact(provider_output_index=2),
        provider="fake-motion",
        review_state="submitted",
        params={"size": "512x512", "num_images": 4},
        authorization_ref="owner-envelope-77",
    )
    assert m["provider"] == "fake-motion"
    assert m["provider_output_index"] == 2
    assert m["cost_estimate"] == 0.25 and m["cost_actual"] == 0.30
    assert m["created_utc"] == "2026-09-17T10:02:05+00:00"  # artifact birth
    assert m["failure_class"] is None  # verbatim from job
