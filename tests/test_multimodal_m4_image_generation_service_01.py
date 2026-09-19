"""
tests/test_multimodal_m4_image_generation_service_01.py -- end-to-end proof
for core/image_generation_service.py (MULTIMODAL-M4-IMAGE-GENERATION-
SERVICE-01).

Fully offline except for one dedicated section that wires the REAL
core.higgsfield_adapter.HiggsfieldAdapter through this service against a
fake transport (zero sockets, zero credentials) -- everywhere else uses
FakeAdapter/MultiOutputFakeAdapter below. config.DB_PATH is isolated per
test the same way tests/test_multimodal_m4_generation_substrate_01.py
already does; config.DATA_DIR is isolated for the whole session by
tests/conftest.py. core.generation_manifest.CANONICAL_PROVIDERS is
monkeypatched to a fake set for every test (mirrors tests/
test_generation_manifest_law_01.py's own convention) so these tests never
depend on, or pollute, the real production provider vocabulary.
"""
from __future__ import annotations

import json
import os
import threading

import pytest

import config
import core.generation_artifact as ga
import core.generation_job as gj
import core.generation_manifest as gm
import core.generation_spending_policy as sp
import core.higgsfield_adapter as ha
import core.higgsfield_transport as ht
import core.image_generation_service as svc
from core.capability_router import (
    Capability,
    CapabilityRegistry,
    CapabilityRoute,
    EvidenceClass,
    LANE_SPECIALIST,
    RoutingPolicy,
    SpecialistRecord,
)

FAKE_MANIFEST_PROVIDERS = frozenset({"fake-flickr"})


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "lumina.db"))
    return tmp_path


@pytest.fixture(autouse=True)
def _fake_manifest_provider_vocabulary(monkeypatch):
    """Never touches the real, deliberately-registered production
    vocabulary (currently {"higgsfield"}) -- these tests exercise the
    service's own logic against a fake registration instead, exactly like
    tests/test_generation_manifest_law_01.py already does."""
    monkeypatch.setattr(gm, "CANONICAL_PROVIDERS", FAKE_MANIFEST_PROVIDERS)


# ---------------------------------------------------------------------------
# M1 registry/policy helpers
# ---------------------------------------------------------------------------

def _registry_and_policy(specialist="fake_provider", disabled_providers=()):
    registry = CapabilityRegistry([
        SpecialistRecord(
            name=specialist, kind="external", local=False,
            capabilities={Capability.IMAGE_GENERATION: EvidenceClass.EVIDENCED},
        ),
    ])
    policy = RoutingPolicy(
        routes={
            Capability.IMAGE_GENERATION.value: CapabilityRoute(
                mode=LANE_SPECIALIST, specialist=specialist,
            ),
        },
        disabled_providers=disabled_providers,
    )
    return registry, policy


# ---------------------------------------------------------------------------
# Fake adapters -- fully offline, configured explicitly per test.
# ---------------------------------------------------------------------------

_FETCH_FAILS = object()  # sentinel: this output's fetch attempt raises


class FakeAdapter:
    """Minimal, fully offline core.generation_adapter.GenerationAdapter.
    Deterministic provider_job_id scheme ("job-1", "job-2", ...) so a test
    can pre-configure poll()/outputs via next_provider_job_id() BEFORE
    calling generate_image() (which owns the actual submit() call and
    never exposes the assigned id any other way). Unless configured, a
    submitted job reports terminal "succeeded" on its very first poll()
    and fetch_result() returns one generic PNG -- so a test that doesn't
    care about the happy path needs no setup at all."""

    def __init__(self):
        self.submit_calls = []
        self.poll_calls = []
        self.estimate_calls = []
        self._next_id = 0
        self._status_queue = {}
        self._outputs = {}
        self._cancel_behavior = {}
        self.estimate = 0.04
        self.estimate_raises = None
        self.reject_idempotency_key = False

    def next_provider_job_id(self) -> str:
        return f"job-{self._next_id + 1}"

    def set_status_sequence(self, provider_job_id, *statuses):
        """poll() returns these in order; the LAST entry repeats forever."""
        self._status_queue[provider_job_id] = list(statuses)

    def set_outputs(self, provider_job_id, outputs):
        self._outputs[provider_job_id] = list(outputs)

    # -- GenerationAdapter Protocol ------------------------------------

    def submit(self, *, model, settings, reference_assets, provider_idempotency_key=None):
        if provider_idempotency_key is not None and self.reject_idempotency_key:
            raise RuntimeError("this fake provider does not support an idempotency key")
        self.submit_calls.append({
            "model": model, "settings": dict(settings),
            "reference_assets": tuple(reference_assets),
            "provider_idempotency_key": provider_idempotency_key,
        })
        self._next_id += 1
        provider_job_id = f"job-{self._next_id}"
        self._status_queue.setdefault(provider_job_id, ["succeeded"])
        return provider_job_id, "queued"

    def poll(self, provider_job_id):
        self.poll_calls.append(provider_job_id)
        queue = self._status_queue[provider_job_id]
        if len(queue) > 1:
            return queue.pop(0)
        return queue[0]

    def fetch_result(self, provider_job_id):
        outputs = self._outputs.setdefault(provider_job_id, [(b"fake-image-bytes", "image/png")])
        assert len(outputs) == 1, "fetch_result() is single-output only; use MultiOutputFakeAdapter"
        item = outputs[0]
        if item is _FETCH_FAILS:
            raise RuntimeError("simulated fetch failure")
        return item

    def describe_model(self, model):
        return {"aspect_ratios": ["1:1", "16:9"]}

    def estimate_cost(self, *, model, settings):
        self.estimate_calls.append({"model": model, "settings": dict(settings)})
        if self.estimate_raises is not None:
            raise self.estimate_raises
        return self.estimate

    def configure_cancel(self, provider_job_id, behavior):
        """behavior: 'terminal_cancelled' (default) | 'acknowledged_pending'."""
        self._cancel_behavior[provider_job_id] = behavior

    def cancel(self, job):
        if job.status in gj.TERMINAL_STATUSES:
            raise gj.JobStateConflict(f"job {job.lumina_job_id} already terminal")
        behavior = self._cancel_behavior.get(job.provider_job_id, "terminal_cancelled")
        if behavior == "terminal_cancelled":
            self._status_queue[job.provider_job_id] = ["cancelled"]
            return gj.update_status(job.lumina_job_id, gj.STATUS_CANCELLED)
        return job  # acknowledged_pending: unchanged, exactly like the real contract


class MultiOutputFakeAdapter(FakeAdapter):
    """Adds the OPTIONAL, duck-typed list_outputs()/fetch_output() pair
    core.image_generation_service recognizes structurally -- the same
    shape core.higgsfield_adapter.HiggsfieldAdapter already ships,
    reproduced here as a plain fake."""

    def __init__(self):
        super().__init__()
        self.list_outputs_raises = None

    def list_outputs(self, provider_job_id):
        if self.list_outputs_raises is not None:
            raise self.list_outputs_raises
        outputs = self._outputs.setdefault(provider_job_id, [(b"fake-image-bytes", "image/png")])
        return list(range(len(outputs)))

    def fetch_output(self, provider_job_id, output_index):
        item = self._outputs[provider_job_id][output_index]
        if item is _FETCH_FAILS:
            raise RuntimeError(f"simulated fetch failure for index {output_index}")
        return item


class _MutationGuardFakeAdapter(FakeAdapter):
    """Raises if any code OUTSIDE this adapter's own __init__ ever assigns
    a new attribute on it -- the structural proof that
    core.image_generation_service never does the forbidden
    `provider.model = requested_model` pattern the model-binding law
    prohibits. Rebinding an EXISTING attribute (the adapter's own internal
    bookkeeping) is unaffected."""

    def __init__(self):
        super().__init__()
        object.__setattr__(self, "_locked", True)

    def __setattr__(self, name, value):
        if getattr(self, "_locked", False) and not hasattr(self, name):
            raise AssertionError(
                f"unexpected new attribute {name!r} set on the adapter -- "
                "core.image_generation_service must never mutate adapter/provider "
                "state (the model-binding law)"
            )
        object.__setattr__(self, name, value)


def _call(adapter, *, specialist="fake_provider", model="fake-model-1", settings=None,
          manifest_provider="fake-flickr", authorization_ref="owner-envelope-1",
          spending_policy=None, cost_unit="usd", poll_interval=0.001, poll_timeout=2.0,
          **kw):
    registry, policy = _registry_and_policy(specialist)
    if spending_policy is None:
        spending_policy = sp.SpendingPolicy()
    return svc.generate_image(
        registry=registry, policy=policy, specialist=specialist, model=model, adapter=adapter,
        settings=settings if settings is not None else {"prompt": "a red bicycle"},
        spending_policy=spending_policy, manifest_provider=manifest_provider,
        authorization_ref=authorization_ref, cost_unit=cost_unit,
        poll_interval=poll_interval, poll_timeout=poll_timeout, **kw,
    )


# ---------------------------------------------------------------------------
# Capability-route resolution (goal 1)
# ---------------------------------------------------------------------------

def test_not_routed_when_specialist_not_admitted():
    adapter = FakeAdapter()
    registry, policy = _registry_and_policy("someone_else")
    result = svc.generate_image(
        registry=registry, policy=policy, specialist="fake_provider", model="fake-model-1",
        adapter=adapter, settings={"prompt": "x"}, spending_policy=sp.SpendingPolicy(),
        manifest_provider="fake-flickr", authorization_ref="env-1", cost_unit="usd",
    )
    assert result.outcome == svc.OUTCOME_NOT_ROUTED
    assert result.lumina_job_id is None
    assert adapter.submit_calls == []  # never even reached submission


def test_not_routed_when_lane_disabled():
    adapter = FakeAdapter()
    registry = CapabilityRegistry([])
    policy = RoutingPolicy(routes={})  # unconfigured -> default-disabled
    result = svc.generate_image(
        registry=registry, policy=policy, specialist="fake_provider", model="fake-model-1",
        adapter=adapter, settings={"prompt": "x"}, spending_policy=sp.SpendingPolicy(),
        manifest_provider="fake-flickr", authorization_ref="env-1", cost_unit="usd",
    )
    assert result.outcome == svc.OUTCOME_NOT_ROUTED


# ---------------------------------------------------------------------------
# Manifest-provider registration preflight (L3, checked early)
# ---------------------------------------------------------------------------

def test_unknown_manifest_provider_refused_before_any_spend_or_submission():
    adapter = FakeAdapter()
    result = _call(adapter, manifest_provider="not-a-registered-provider")
    assert result.outcome == svc.OUTCOME_UNKNOWN_MANIFEST_PROVIDER
    assert adapter.submit_calls == []
    assert adapter.estimate_calls == []


# ---------------------------------------------------------------------------
# Successful single-output / multi-output / zero-output generation
# (goals 9-13; required cases 1-3)
# ---------------------------------------------------------------------------

def test_successful_single_output_generation():
    adapter = FakeAdapter()
    result = _call(adapter)
    assert result.outcome == svc.OUTCOME_SUCCESS
    assert result.job_status == gj.STATUS_SUCCEEDED
    assert len(result.artifacts) == 1
    assert len(result.manifests) == 1
    assert result.artifacts[0].provider_output_index == 0
    assert ga.get_artifact_bytes(result.artifacts[0].artifact_id) == b"fake-image-bytes"
    manifest = result.manifests[0]
    assert manifest["provider"] == "fake-flickr"
    assert manifest["capability"] == "image_generation"
    assert manifest["job_ingestion_state"] == gj.INGESTION_INGESTED
    final = gj.get_generation_job(result.lumina_job_id)
    assert final.status == gj.STATUS_SUCCEEDED
    assert final.ingestion_state == gj.INGESTION_INGESTED
    # MULTIMODAL-M4-GENERATION-MANIFEST-PERSISTENCE-01: the manifest built
    # above is also, by construction, already durable on disk.
    assert len(result.manifest_paths) == 1
    assert result.failed_manifest_indices == ()
    manifest_path = result.manifest_paths[0]
    assert os.path.isfile(manifest_path)
    with open(manifest_path) as f:
        assert json.load(f) == manifest


def test_successful_multi_output_generation():
    adapter = MultiOutputFakeAdapter()
    pid = adapter.next_provider_job_id()
    adapter.set_outputs(pid, [
        (b"img-0", "image/png"), (b"img-1", "image/png"), (b"img-2", "image/png"),
    ])
    result = _call(adapter)
    assert result.outcome == svc.OUTCOME_SUCCESS
    assert len(result.artifacts) == 3
    assert len(result.manifests) == 3
    assert sorted(a.provider_output_index for a in result.artifacts) == [0, 1, 2]
    assert {ga.get_artifact_bytes(a.artifact_id) for a in result.artifacts} == {b"img-0", b"img-1", b"img-2"}
    # One durable manifest file per successful artifact -- each discoverable
    # from its own artifact_id, matched by content, not by tuple position.
    assert len(result.manifest_paths) == 3
    assert result.failed_manifest_indices == ()
    manifests_by_artifact_id = {m["artifact_id"]: m for m in result.manifests}
    for path in result.manifest_paths:
        assert os.path.isfile(path)
        with open(path) as f:
            on_disk = json.load(f)
        assert on_disk == manifests_by_artifact_id[on_disk["artifact_id"]]


def test_zero_output_provider_success():
    adapter = MultiOutputFakeAdapter()
    pid = adapter.next_provider_job_id()
    adapter.set_outputs(pid, [])
    result = _call(adapter)
    assert result.outcome == svc.OUTCOME_ZERO_OUTPUTS
    assert result.artifacts == ()
    assert result.manifests == ()
    final = gj.get_generation_job(result.lumina_job_id)
    assert final.status == gj.STATUS_SUCCEEDED
    assert final.ingestion_state is None  # honestly: no ingestion attempt was ever made


# ---------------------------------------------------------------------------
# Manifest persistence (MULTIMODAL-M4-GENERATION-MANIFEST-PERSISTENCE-01)
# -- a manifest DURABILITY failure is distinct from, and never allowed to
# masquerade as, either an ingestion failure or a manifest LAWFULNESS
# failure.
# ---------------------------------------------------------------------------

def test_manifest_persistence_failure_reported_truthfully_single_output(monkeypatch):
    """Provider succeeds, ingestion succeeds, but persist_manifest() fails:
    the artifact's bytes stay durable and DO appear in result.artifacts
    (ingestion truly succeeded), while the outcome truthfully reflects that
    the deliverable is not yet fully materialized -- provider success does
    NOT imply manifest persistence success."""
    adapter = FakeAdapter()

    def _boom(manifest):
        raise gm.ManifestPersistenceError(["simulated disk failure"])

    monkeypatch.setattr(svc.gm, "persist_manifest", _boom)
    result = _call(adapter)

    assert result.outcome == svc.OUTCOME_MANIFEST_FAILED
    assert len(result.artifacts) == 1  # bytes are still durable -- never rolled back
    assert ga.get_artifact_bytes(result.artifacts[0].artifact_id) == b"fake-image-bytes"
    assert result.manifests == ()
    assert result.manifest_paths == ()
    assert result.failed_manifest_indices == (0,)
    assert result.failed_output_indices == ()  # this was NOT an ingestion failure
    # The job's own ingestion-state axis is untouched by a manifest failure
    # -- it stays exactly what it already meant before this amendment.
    final = gj.get_generation_job(result.lumina_job_id)
    assert final.ingestion_state == gj.INGESTION_INGESTED


def test_manifest_persistence_failure_never_invalidates_a_sibling_success(monkeypatch):
    """Multi-output: one artifact's manifest fails to persist, the other's
    succeeds. Sibling success stays truthful -- the surviving manifest is
    unaffected, and the failure is attributed to exactly the right index."""
    adapter = MultiOutputFakeAdapter()
    pid = adapter.next_provider_job_id()
    adapter.set_outputs(pid, [(b"img-0", "image/png"), (b"img-1", "image/png")])

    real_persist_manifest = gm.persist_manifest

    def _fail_index_zero(manifest):
        if manifest["provider_output_index"] == 0:
            raise gm.ManifestPersistenceError(["simulated disk failure for output 0"])
        return real_persist_manifest(manifest)

    monkeypatch.setattr(svc.gm, "persist_manifest", _fail_index_zero)
    result = _call(adapter)

    assert result.outcome == svc.OUTCOME_MANIFEST_FAILED
    assert len(result.artifacts) == 2  # both artifacts' bytes are durable
    assert result.failed_manifest_indices == (0,)
    assert len(result.manifests) == 1
    assert len(result.manifest_paths) == 1
    assert result.manifests[0]["provider_output_index"] == 1
    assert os.path.isfile(result.manifest_paths[0])


def test_failed_ingestion_output_never_receives_a_false_successful_manifest():
    """An output whose fetch/ingestion failed never reaches manifest
    build/persist at all -- it can never end up with a manifest, durable or
    otherwise, since ga.ingest_artifact() never even ran for it."""
    adapter = MultiOutputFakeAdapter()
    pid = adapter.next_provider_job_id()
    adapter.set_outputs(pid, [(b"img-0", "image/png"), _FETCH_FAILS])
    result = _call(adapter)

    assert result.outcome == svc.OUTCOME_PARTIAL
    assert len(result.artifacts) == 1
    assert result.failed_output_indices == (1,)
    assert result.failed_manifest_indices == ()  # never attributed to the manifest axis
    assert len(result.manifests) == 1
    assert len(result.manifest_paths) == 1
    assert os.path.isfile(result.manifest_paths[0])


def test_manifest_persistence_failure_outcome_yields_to_partial_ingestion_outcome(monkeypatch):
    """When ingestion itself was already partial, that remains the
    reported outcome even if the one output that DID ingest also fails to
    get a durable manifest -- ingestion failure is the dominant truthful
    signal; failed_manifest_indices still carries the manifest-specific
    detail rather than it being silently dropped."""
    adapter = MultiOutputFakeAdapter()
    pid = adapter.next_provider_job_id()
    adapter.set_outputs(pid, [(b"img-0", "image/png"), _FETCH_FAILS])

    def _boom(manifest):
        raise gm.ManifestPersistenceError(["simulated disk failure"])

    monkeypatch.setattr(svc.gm, "persist_manifest", _boom)
    result = _call(adapter)

    assert result.outcome == svc.OUTCOME_PARTIAL
    assert result.failed_output_indices == (1,)
    assert result.failed_manifest_indices == (0,)
    assert result.manifests == ()
    assert result.manifest_paths == ()


# ---------------------------------------------------------------------------
# Provider failure / cancellation (required cases 4-6)
# ---------------------------------------------------------------------------

def test_provider_failure():
    adapter = FakeAdapter()
    pid = adapter.next_provider_job_id()
    adapter.set_status_sequence(pid, "queued", "failed")
    result = _call(adapter)
    assert result.outcome == svc.OUTCOME_PROVIDER_FAILED
    assert result.job_status == gj.STATUS_FAILED
    assert result.artifacts == ()


def test_provider_initiated_cancellation_observed_via_polling():
    adapter = FakeAdapter()
    pid = adapter.next_provider_job_id()
    adapter.set_status_sequence(pid, "queued", "cancelled")
    result = _call(adapter)
    assert result.outcome == svc.OUTCOME_CANCELLED
    assert result.job_status == gj.STATUS_CANCELLED


def test_user_runtime_cancellation_while_polling_confirmed_terminal():
    adapter = FakeAdapter()
    cancel_event = threading.Event()
    cancel_event.set()  # already requested before the first loop iteration
    result = _call(adapter, cancel_event=cancel_event)
    assert result.outcome == svc.OUTCOME_CANCELLED
    assert result.job_status == gj.STATUS_CANCELLED
    assert adapter.poll_calls == []  # cancelled before any poll was needed


def test_cancellation_acknowledged_but_not_confirmed_keeps_polling_to_real_outcome():
    adapter = FakeAdapter()
    pid = adapter.next_provider_job_id()
    adapter.configure_cancel(pid, "acknowledged_pending")
    adapter.set_status_sequence(pid, "queued", "running", "succeeded")
    cancel_event = threading.Event()
    cancel_event.set()
    result = _call(adapter, cancel_event=cancel_event)
    # Provider never confirmed cancellation -- the real outcome (success)
    # is what gets reported, never a locally-assumed cancellation.
    assert result.outcome == svc.OUTCOME_SUCCESS
    assert result.job_status == gj.STATUS_SUCCEEDED


# ---------------------------------------------------------------------------
# Unknown provider state (required case 7) -- never guessed, bounded
# ---------------------------------------------------------------------------

def test_unknown_provider_state_never_guessed_and_bounded():
    adapter = FakeAdapter()
    pid = adapter.next_provider_job_id()
    adapter.set_status_sequence(pid, "some-garbage-raw-status")  # never resolves
    result = _call(adapter, poll_interval=0.001, poll_timeout=0.05)
    assert result.outcome == svc.OUTCOME_TIMED_OUT
    assert result.job_status == gj.STATUS_UNKNOWN
    final = gj.get_generation_job(result.lumina_job_id)
    assert final.status == gj.STATUS_UNKNOWN  # never collapsed into succeeded/failed


# ---------------------------------------------------------------------------
# Spending policy (required cases 8-9)
# ---------------------------------------------------------------------------

def test_estimate_refused_by_spending_policy_ceiling():
    adapter = FakeAdapter()
    adapter.estimate = 5.0
    result = _call(adapter, spending_policy=sp.SpendingPolicy(single_job_ceiling=1.0))
    assert result.outcome == svc.OUTCOME_SPEND_REQUIRES_APPROVAL
    assert result.cost_estimate == 5.0
    assert adapter.submit_calls == []  # no spend, no submission


def test_unknown_estimate_fails_closed_by_default():
    adapter = FakeAdapter()
    adapter.estimate = None
    result = _call(adapter, cost_unit=None, authorization_ref=None,
                    spending_policy=sp.SpendingPolicy())
    assert result.outcome == svc.OUTCOME_SPEND_DENIED
    assert result.cost_estimate is None
    assert adapter.submit_calls == []


def test_missing_authorization_ref_refused_before_spend_or_submission():
    adapter = FakeAdapter()
    adapter.estimate = 0.5
    result = _call(adapter, authorization_ref=None)
    assert result.outcome == svc.OUTCOME_MISSING_AUTHORIZATION
    assert result.cost_estimate == 0.5
    assert adapter.submit_calls == []


def test_zero_cost_with_no_authorization_ref_is_allowed_through_to_submission():
    adapter = FakeAdapter()
    adapter.estimate = None
    result = _call(adapter, cost_unit=None, authorization_ref=None,
                    spending_policy=sp.SpendingPolicy(require_estimate=False))
    assert result.outcome == svc.OUTCOME_SUCCESS
    assert result.cost_estimate is None
    assert result.manifests[0]["authorization_ref"] is None
    assert result.manifests[0]["cost_estimate"] is None


# ---------------------------------------------------------------------------
# Submission ambiguity / idempotency (required case 10)
# ---------------------------------------------------------------------------

def test_ambiguous_duplicate_submission_returns_soft_outcome():
    from core.capability_router import resolve_capability

    registry, policy = _registry_and_policy("fake_provider")
    decision = resolve_capability(registry, policy, Capability.IMAGE_GENERATION)
    existing = gj.begin_generation_job(
        Capability.IMAGE_GENERATION, "fake_provider", "fake-model-1", decision,
        {"prompt": "a red bicycle"},
    )  # left unresolved (STATUS_UNKNOWN) on purpose -- never submitted
    adapter = FakeAdapter()
    result = _call(adapter, settings={"prompt": "a red bicycle"})
    assert result.outcome == svc.OUTCOME_AMBIGUOUS_DUPLICATE
    assert result.lumina_job_id == existing.lumina_job_id
    assert adapter.submit_calls == []


def test_provider_idempotency_key_passed_through_when_supported():
    adapter = FakeAdapter()
    result = _call(adapter, provider_idempotency_key="caller-key-1")
    assert result.outcome == svc.OUTCOME_SUCCESS
    assert adapter.submit_calls[0]["provider_idempotency_key"] == "caller-key-1"
    assert gj.get_generation_job(result.lumina_job_id).provider_idempotency_key == "caller-key-1"


def test_provider_idempotency_key_rejected_by_adapter_surfaces_as_submission_failed():
    adapter = FakeAdapter()
    adapter.reject_idempotency_key = True
    result = _call(adapter, provider_idempotency_key="unsupported-key")
    assert result.outcome == svc.OUTCOME_SUBMISSION_FAILED
    assert result.lumina_job_id is not None
    # Never claims a submission that didn't happen: job stays exactly where
    # begin_generation_job() left it.
    assert gj.get_generation_job(result.lumina_job_id).status == gj.STATUS_UNKNOWN
    assert gj.get_generation_job(result.lumina_job_id).provider_job_id is None


# ---------------------------------------------------------------------------
# Download/fetch failure + partial multi-artifact ingestion
# (required cases 11-12; manifest-per-artifact cases 13-14)
# ---------------------------------------------------------------------------

def test_download_fetch_failure_single_output():
    adapter = FakeAdapter()
    pid = adapter.next_provider_job_id()
    adapter.set_outputs(pid, [_FETCH_FAILS])
    result = _call(adapter)
    assert result.outcome == svc.OUTCOME_INGESTION_FAILED
    assert result.artifacts == ()
    assert result.manifests == ()
    assert result.failed_output_indices == (0,)
    final = gj.get_generation_job(result.lumina_job_id)
    assert final.ingestion_state == gj.INGESTION_FAILED


def test_partial_multi_artifact_ingestion_never_launders_failure():
    adapter = MultiOutputFakeAdapter()
    pid = adapter.next_provider_job_id()
    adapter.set_outputs(pid, [(b"ok-0", "image/png"), _FETCH_FAILS, (b"ok-2", "image/png")])
    result = _call(adapter)
    assert result.outcome == svc.OUTCOME_PARTIAL
    assert len(result.artifacts) == 2
    assert len(result.manifests) == 2  # one manifest per INGESTED artifact -- never for index 1
    assert result.failed_output_indices == (1,)
    assert sorted(a.provider_output_index for a in result.artifacts) == [0, 2]
    # Every manifest built from the job honestly reflects the FINAL
    # aggregate -- even artifact 0's manifest, ingested before sibling 1
    # failed, says "partial", never a stale "ingested".
    for manifest in result.manifests:
        assert manifest["job_ingestion_state"] == gj.INGESTION_PARTIAL
    final = gj.get_generation_job(result.lumina_job_id)
    assert final.ingestion_state == gj.INGESTION_PARTIAL


def test_all_outputs_fail_ingestion_is_ingestion_failed_not_partial():
    adapter = MultiOutputFakeAdapter()
    pid = adapter.next_provider_job_id()
    adapter.set_outputs(pid, [_FETCH_FAILS, _FETCH_FAILS])
    result = _call(adapter)
    assert result.outcome == svc.OUTCOME_INGESTION_FAILED
    assert result.artifacts == ()
    assert result.manifests == ()
    assert result.failed_output_indices == (0, 1)


def test_output_discovery_itself_failing_is_reported_distinctly():
    adapter = MultiOutputFakeAdapter()
    pid = adapter.next_provider_job_id()
    adapter.list_outputs_raises = RuntimeError("transport unreachable")
    result = _call(adapter)
    assert result.outcome == svc.OUTCOME_DISCOVERY_FAILED
    assert result.artifacts == ()
    final = gj.get_generation_job(result.lumina_job_id)
    # Discovery failing is a real, recorded ingestion failure -- never left
    # indistinguishable from a job that genuinely had zero outputs.
    assert final.ingestion_state == gj.INGESTION_FAILED


# ---------------------------------------------------------------------------
# Model-binding law (required case 15): per-invocation model, never a
# mutated global/provider attribute.
# ---------------------------------------------------------------------------

def test_model_target_passed_per_invocation_without_global_mutation():
    adapter = _MutationGuardFakeAdapter()
    result_a = _call(adapter, model="model-a", settings={"prompt": "first"})
    result_b = _call(adapter, model="model-b", settings={"prompt": "second"})
    assert result_a.outcome == svc.OUTCOME_SUCCESS
    assert result_b.outcome == svc.OUTCOME_SUCCESS
    assert adapter.submit_calls[0]["model"] == "model-a"
    assert adapter.submit_calls[1]["model"] == "model-b"
    assert gj.get_generation_job(result_a.lumina_job_id).model == "model-a"
    assert gj.get_generation_job(result_b.lumina_job_id).model == "model-b"
    assert not hasattr(adapter, "model")  # never assigned, not even transiently


# ---------------------------------------------------------------------------
# Secrets absent from artifacts/manifests/errors (required case 16)
# ---------------------------------------------------------------------------

def test_secret_shaped_settings_refused_loudly_even_though_bytes_are_already_durable():
    adapter = FakeAdapter()
    with pytest.raises(gm.SecretMaterialInManifest) as exc:
        _call(adapter, settings={"prompt": "x", "api_key": "harmless-looking-value"})
    # Loud, not laundered into a soft outcome -- but the job id is folded
    # into the message so the (already-durable) artifact stays traceable.
    assert "job " in str(exc.value)


def test_submission_failure_diagnostic_never_leaks_a_raw_secret():
    class LeakyAdapter(FakeAdapter):
        def submit(self, **kw):
            raise RuntimeError("auth failed for Bearer abcdefghijklmnopqr")

    adapter = LeakyAdapter()
    result = _call(adapter)
    assert result.outcome == svc.OUTCOME_SUBMISSION_FAILED
    assert "abcdefghijklmnopqr" not in result.diagnostic


# ---------------------------------------------------------------------------
# Higgsfield adapter contract compatibility, offline (required case 17)
# ---------------------------------------------------------------------------

class _FakeHiggsfieldTransport:
    """Satisfies core.higgsfield_transport.HiggsfieldTransport structurally.
    Never touches a socket, never sees real credentials."""

    def __init__(self):
        self.calls = []
        self._responses = {}

    def configure(self, method, key, response):
        self._responses[(method, key)] = response

    def _resolve(self, method, key):
        resp = self._responses.get((method, key))
        if resp is None:
            raise AssertionError(f"no response configured for {method} {key}")
        return resp() if callable(resp) else resp

    def post(self, path, *, json=None, params=None):
        self.calls.append(("POST", path, json))
        return self._resolve("POST", path)

    def get(self, path, *, params=None):
        self.calls.append(("GET", path, None))
        return self._resolve("GET", path)

    def put_bytes(self, url, *, data, headers):
        raise AssertionError("not used by this test")

    def get_raw(self, url, *, headers=None):
        self.calls.append(("GET_RAW", url, None))
        return self._resolve("GET_RAW", url)


def _json_response(status_code, body_dict, headers=None):
    return ht.TransportResponse(status_code=status_code, headers=headers or {},
                                 body=json.dumps(body_dict).encode("utf-8"))


def _raw_response(status_code, body_bytes, headers=None):
    return ht.TransportResponse(status_code=status_code, headers=headers or {}, body=body_bytes)


def test_higgsfield_adapter_contract_compatibility_offline(monkeypatch):
    # This ONE test needs "higgsfield" registered -- overrides the
    # module-level fake vocabulary for just this test, never the real one.
    monkeypatch.setattr(gm, "CANONICAL_PROVIDERS", frozenset({"higgsfield"}))

    # estimate_cost() is local/zero-network as of MULTIMODAL-M4-HIGGSFIELD-
    # PRICING-REPAIR-01 -- no /estimate/... transport response to configure.
    # "higgsfield-ai/soul/standard" is the one model with a verified local
    # price (nano-banana has none -- see that campaign's evidence doc).
    transport = _FakeHiggsfieldTransport()
    transport.configure("POST", "/higgsfield-ai/soul/standard", _json_response(200, {
        "status": "queued", "request_id": "req-svc-1",
    }))
    transport.configure("GET", "/requests/req-svc-1/status", _json_response(200, {
        "status": "completed", "request_id": "req-svc-1",
        "images": [{"url": "https://cdn.example.com/out-0.jpg"}],
    }))
    transport.configure("GET_RAW", "https://cdn.example.com/out-0.jpg",
                         _raw_response(200, b"real-adapter-bytes", headers={"Content-Type": "image/jpeg"}))

    adapter = ha.HiggsfieldAdapter(transport=transport)
    registry, policy = _registry_and_policy("higgsfield")
    result = svc.generate_image(
        registry=registry, policy=policy, specialist="higgsfield", model="higgsfield-ai/soul/standard",
        adapter=adapter, settings={"prompt": "a lighthouse at dawn", "aspect_ratio": "16:9"},
        spending_policy=sp.SpendingPolicy(single_job_ceiling=1.0),
        manifest_provider="higgsfield", authorization_ref="owner-envelope-77", cost_unit="usd",
        poll_interval=0.001, poll_timeout=2.0,
    )

    assert result.outcome == svc.OUTCOME_SUCCESS
    assert result.cost_estimate == pytest.approx(0.0938)
    assert len(result.artifacts) == 1
    assert ga.get_artifact_bytes(result.artifacts[0].artifact_id) == b"real-adapter-bytes"
    manifest = result.manifests[0]
    assert manifest["provider"] == "higgsfield"
    assert manifest["capability"] == "image_generation"
    assert manifest["authorization_ref"] == "owner-envelope-77"
    # No /estimate/... call: pricing is local now, and no live call either --
    # every response that WAS made came from the fake transport's fixed table.
    assert all(method in ("POST", "GET", "GET_RAW") for method, _, _ in transport.calls)
    assert not any(path.startswith("/estimate") for method, path, _ in transport.calls)

    # Required case 17: the fake-Higgsfield end-to-end path also reaches
    # manifest PERSISTENCE, not just build -- the exact production gap
    # MULTIMODAL-M4-HIGGSFIELD-LIVE-SMOKE-02 exposed (the harness there had
    # to write an evidence copy manually; this proves that's no longer
    # necessary). No live provider call is needed to prove this -- the fake
    # transport already exercises the real adapter/service code in full.
    assert len(result.manifest_paths) == 1
    assert result.failed_manifest_indices == ()
    manifest_path = result.manifest_paths[0]
    assert os.path.isfile(manifest_path)
    with open(manifest_path) as f:
        on_disk = json.load(f)
    assert on_disk == manifest
    # Restart-independent rediscovery: the same path is recomputable from
    # nothing but the artifact_id, with no database lookup.
    assert manifest_path == ga.artifact_manifest_path(result.artifacts[0].artifact_id)
