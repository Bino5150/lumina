"""Shared authority contract against fake observations and an inert sink only."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
import json
import threading
from types import SimpleNamespace

import pytest

from chrome_companion.site_actions import claims as c, kernel as k, owner_intent as o, review as r
from chrome_companion.site_actions.model import (ActionSpec, Consequence, ContractError, DestinationIdentity,
    EvidenceKind, FrozenPayload, PrincipalEvidenceRule, PrincipalPolicy, PrincipalRequirement,
    ReceiptState, Runtime, StepKind, UnavailablePolicy)
from chrome_companion.site_actions.registry import load_manifest


def spec_for(capability):
    manifest = load_manifest(capability)
    destination = DestinationIdentity("reddit", "comment", "t1_abc") if capability == "reddit.reply" else (
        DestinationIdentity("reddit", "subreddit", "t5_abc"))
    payload = {"body": " exact\r\n👹 e\u0301 "} if capability == "reddit.reply" else {
        "mode": "text", "title": " exact title 👹 ", "body": " exact\r\n👹 e\u0301 "}
    return ActionSpec(manifest, Runtime.CHROME_COMPANION, destination,
                      FrozenPayload.from_data(payload), Consequence.EXTERNAL_COMMIT)


def command(spec):
    return o.PREFIX + json.dumps({"v": 1, "runtime": spec.runtime.value,
        "capability": spec.manifest.capability_id, "destination": {
            "kind": spec.destination.kind, "id": spec.destination.canonical_key}, "payload": spec.payload.to_data()},
        ensure_ascii=False)


class World:
    def __init__(self, tmp_path, capability):
        self.spec = spec_for(capability)
        self.data = tmp_path / "data"
        self.data.mkdir(mode=0o700)
        self.identity = c.initialize_store(self.data)
        o.pin_store_identity(self.data, self.identity)
        self.store = o.reopen_claim_store(self.data)
        self.time = 100
        self.agent = SimpleNamespace(owner=True)
        self.intake = o.OwnerIntake(self.agent, self.store, session="session", task="task", channel="desktop",
                                    chat="chat", authority_domain="desktop/owner/chat", clock=lambda: self.time)
        self.presenter = object()
        self.review = r.ReviewController(self.intake, self.presenter)
        self.observation = k.Observation(Runtime.CHROME_COMPANION, "instance", "connection", "worker-boot",
            1, 2, "https://www.reddit.com", "https://www.reddit.com/synthetic", "document", "target",
            StepKind.PREPARE, 1, self.time, self.spec.destination, self.spec.payload.digest,
            k.PrincipalObservation("synthetic-account-A", True, PrincipalEvidenceRule.AUTHENTICATED_ACCOUNT),
            True, True, True, False, False, 0)
        self.proof = None
        self.kernel = k.AuthorityKernel(self.intake, self.observe, review=self.review, evidence=lambda op: self.proof)
        self.sink = []

    def observe(self, spec, kind):
        return self.observation

    def capture(self, event="event"):
        return self.intake.capture(self.agent, source="OWNER_DIRECT", raw_text=command(self.spec), event_id=event)

    def authorize(self):
        return self.kernel.authorize(self.capture())

    def target(self, kind):
        self.observation = replace(self.observation, target_kind=kind, generation=self.observation.generation + 1,
                                   observed_ns=self.time)

    def send(self, handle, kind):
        self.target(kind)
        claim = self.kernel.claim(handle, kind)
        permit = self.kernel.consume_commit(handle, claim) if kind is StepKind.COMMIT else (
            self.kernel.consume_step(handle, claim))
        assert type(permit) is k.DispatchPermit
        self.sink.append(permit)
        return claim, permit

    def remote(self, handle):
        return k.RemoteEvidence(handle.identifier, self.spec.digest, self.spec.runtime,
            self.observation.surface_digest, self.spec.destination, self.observation.principal.fingerprint,
            self.spec.payload.digest, True, True, "synthetic-created-object",
            "https://www.reddit.com/synthetic/result", tuple(EvidenceKind))

    def close(self):
        self.store.close()


@pytest.fixture(params=["reddit.reply", "reddit.create_post"])
def world(tmp_path, request):
    world = World(tmp_path, request.param)
    yield world
    world.close()


def test_both_manifests_use_one_reducer_and_distinct_claims(world):
    handle = world.authorize()
    assert type(handle) is k.WorkflowAuthorization
    for kind in (StepKind.PREPARE, StepKind.FILL):
        claim, permit = world.send(handle, kind)
        assert type(claim) is k.StepClaim and permit.kind is kind
        assert permit.specification is world.spec or permit.specification == world.spec
    claim, permit = world.send(handle, StepKind.COMMIT)
    assert type(claim) is k.CommitClaim and permit.kind is StepKind.COMMIT
    receipt = world.kernel.receipt(handle)
    assert receipt.authorization_source is k.AuthorizationSource.OWNER_EXACT
    assert receipt.state is ReceiptState.DISPATCHED and receipt.spent_or_uncertain and receipt.closed
    key = c.EventKey.from_ingress("desktop/owner/chat", "event")
    assert world.store.inspect(key, c.RecordKind.SPENT).parent_digest == world.store.inspect(key, c.RecordKind.BINDING).digest
    with pytest.raises(ContractError):
        world.kernel.consume_commit(handle, claim)
    with pytest.raises(ContractError):
        world.kernel.claim(handle, StepKind.COMMIT)
    assert len(world.sink) == 3


def test_generated_payload_requires_distinct_presented_review_event(world):
    draft = world.intake.capture(world.agent, source="OWNER_DIRECT", raw_text="Draft a response for my review", event_id="draft")
    with pytest.raises(ContractError):
        world.kernel.authorize(draft)
    request = world.kernel.prepare_review(draft, world.spec)
    world.review.present(world.presenter, request, request.snapshot.fingerprint)
    approval = world.review.approve(world.presenter, request, event_id="owner-click", displayed_fingerprint=request.snapshot.fingerprint)
    handle = world.kernel.authorize(approval)
    world.send(handle, StepKind.COMMIT)
    assert world.kernel.receipt(handle).authorization_source is k.AuthorizationSource.OWNER_REVIEW
    review_key = c.EventKey.from_ingress("desktop/owner/chat/review", "owner-click")
    assert world.store.inspect(review_key, c.RecordKind.SPENT)
    with pytest.raises(c.ClaimStoreError):
        world.store.inspect(c.EventKey.from_ingress("desktop/owner/chat", "draft"), c.RecordKind.BINDING)


@pytest.mark.parametrize("field,value", [
    ("runtime", "playwright"), ("runtime", "api"), ("instance", "other-instance"),
    ("connection", "reconnected"), ("worker_boot", "restarted"), ("tab", 4), ("window", 5),
    ("origin", "https://example.com"), ("destination", DestinationIdentity("reddit", "comment", "t1_other")),
    ("payload_digest", "0" * 64), ("principal", k.PrincipalObservation("B", True, PrincipalEvidenceRule.AUTHENTICATED_ACCOUNT)),
    ("principal", k.PrincipalObservation(None, False, PrincipalEvidenceRule.AUTHENTICATED_ACCOUNT)),
    ("unique", False), ("permission", False), ("allowed", False), ("paused", True), ("revoked", True),
    ("control_epoch", 1), ("observed_ns", 101),
])
def test_scope_switch_or_unverified_surface_never_delivers(world, field, value):
    handle = world.authorize()
    world.target(StepKind.COMMIT)
    claim = world.kernel.claim(handle, StepKind.COMMIT)
    # Bypass dataclass validation only to model an invalid provider result.
    original = world.observation
    altered = replace(original)
    object.__setattr__(altered, field, value)
    world.observation = altered
    with pytest.raises((ContractError, c.ClaimStoreError)):
        world.kernel.consume_commit(handle, claim)
    assert world.sink == [] and world.kernel.receipt(handle).closed
    world.observation = original
    world.target(StepKind.COMMIT)
    with pytest.raises(ContractError):
        world.kernel.claim(handle, StepKind.COMMIT)


@pytest.mark.parametrize("field,value", [("document", "new-document"), ("target", "other-target"),
    ("url", "https://www.reddit.com/synthetic/other"), ("generation", 999), ("target_kind", StepKind.COMMIT)])
def test_exact_step_binding_cannot_move_or_hide_submit(world, field, value):
    handle = world.authorize()
    world.target(StepKind.FILL)
    claim = world.kernel.claim(handle, StepKind.FILL)
    world.observation = replace(world.observation, **{field: value})
    with pytest.raises(ContractError):
        world.kernel.consume_step(handle, claim)
    assert world.sink == []


def test_step_claim_copy_wrong_workflow_replay_and_fill_commit_swap(world):
    handle = world.authorize()
    world.target(StepKind.FILL)
    claim = world.kernel.claim(handle, StepKind.FILL)
    for wrong in (replace(claim), replace(claim, kind=StepKind.COMMIT), k.CommitClaim(claim.identifier, claim.workflow, claim.target_digest)):
        with pytest.raises(ContractError):
            world.kernel.consume_step(handle, wrong)
        with pytest.raises(ContractError):
            world.kernel.consume_commit(handle, wrong)
    with pytest.raises(ContractError):
        world.kernel.consume_step(replace(handle), claim)
    world.sink.append(world.kernel.consume_step(handle, claim))
    with pytest.raises(ContractError):
        world.kernel.consume_step(handle, claim)
    assert world.kernel.receipt(handle).state is ReceiptState.NOT_DISPATCHED
    assert len(world.sink) == 1


def test_declared_counts_and_forward_step_order(world):
    handle = world.authorize()
    world.send(handle, StepKind.PREPARE)
    world.target(StepKind.PREPARE)
    with pytest.raises(ContractError):
        world.kernel.claim(handle, StepKind.PREPARE)
    for _ in range(next(s.max_count for s in world.spec.manifest.steps if s.kind is StepKind.FILL)):
        world.send(handle, StepKind.FILL)
    world.target(StepKind.FILL)
    with pytest.raises(ContractError):
        world.kernel.claim(handle, StepKind.FILL)
    world.send(handle, StepKind.COMMIT)


@pytest.mark.parametrize("control", ["pause", "revoke", "retire", "expiry"])
@pytest.mark.parametrize("phase", ["before_claim", "claimed", "delivered"])
def test_controls_expiry_and_resume_never_resurrect(world, control, phase):
    handle = world.authorize()
    world.target(StepKind.COMMIT)
    claim = None
    if phase != "before_claim":
        claim = world.kernel.claim(handle, StepKind.COMMIT)
    if phase == "delivered":
        world.sink.append(world.kernel.consume_commit(handle, claim))
    if control == "expiry":
        world.time = world.intake.resolve(world.capture("independent")).metadata.admitted_ns + o.LIFETIME_NS
    else:
        getattr(world.kernel, control)()
    if control == "pause":
        world.kernel.resume()
    if control == "revoke":
        world.kernel.allow()
    world.observation = replace(world.observation, observed_ns=world.time)
    with pytest.raises(ContractError):
        if claim is None:
            world.kernel.claim(handle, StepKind.COMMIT)
        else:
            world.kernel.consume_commit(handle, claim)
    receipt = world.kernel.receipt(handle)
    assert receipt.closed
    assert receipt.state is (ReceiptState.AMBIGUOUS_AFTER_DISPATCH if phase == "delivered" else ReceiptState.NOT_DISPATCHED)
    assert len(world.sink) == (1 if phase == "delivered" else 0)


@pytest.mark.parametrize("control", ["pause", "revoke", "retire", "expiry", "account", "document"])
def test_invalidation_during_actual_directory_fsync_retains_spent_no_permit(world, monkeypatch, control):
    handle = world.authorize()
    world.target(StepKind.COMMIT)
    claim = world.kernel.claim(handle, StepKind.COMMIT)
    entered, release = threading.Event(), threading.Event()
    original = c.os.fsync
    def barrier(fd):
        if fd == world.store._store_fd:
            entered.set()
            assert release.wait(20)
        original(fd)
    monkeypatch.setattr(c.os, "fsync", barrier)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(world.kernel.consume_commit, handle, claim)
        assert entered.wait(20)
        if control == "expiry":
            world.time += o.LIFETIME_NS
        elif control == "account":
            world.observation = replace(world.observation, principal=k.PrincipalObservation("B", True, PrincipalEvidenceRule.AUTHENTICATED_ACCOUNT))
        elif control == "document":
            world.observation = replace(world.observation, document="replacement")
        else:
            getattr(world.kernel, control)()
        release.set()
        with pytest.raises(ContractError):
            future.result(timeout=20)
    key = c.EventKey.from_ingress("desktop/owner/chat", "event")
    assert world.store.inspect(key, c.RecordKind.SPENT)
    assert world.sink == [] and world.kernel.receipt(handle).spent_or_uncertain
    with pytest.raises(ContractError):
        world.kernel.consume_commit(handle, claim)


def test_concurrent_double_commit_one_kernel_delivers_at_most_one(world):
    handle = world.authorize()
    world.target(StepKind.COMMIT)
    claim = world.kernel.claim(handle, StepKind.COMMIT)
    barrier = threading.Barrier(8)
    def consume():
        barrier.wait(timeout=20)
        try:
            return world.kernel.consume_commit(handle, claim)
        except ContractError:
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: consume(), range(8)))
    assert sum(type(result) is k.DispatchPermit for result in results) == 1


def test_durable_failure_never_delivers_and_obstruction_survives(world, monkeypatch):
    handle = world.authorize()
    world.target(StepKind.COMMIT)
    claim = world.kernel.claim(handle, StepKind.COMMIT)
    monkeypatch.setattr(c.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("uncertain")))
    with pytest.raises(c.ClaimStoreError):
        world.kernel.consume_commit(handle, claim)
    assert world.sink == [] and world.kernel.receipt(handle).spent_or_uncertain
    with pytest.raises(ContractError):
        world.kernel.consume_commit(handle, claim)
    assert list((world.data / "chrome_companion" / c.STORE_DIR).glob("*.spent.json"))


@pytest.mark.parametrize("status,identity,allow,success", [
    (PrincipalRequirement.OPTIONAL, "A", UnavailablePolicy.DENY, True),
    (PrincipalRequirement.OPTIONAL, None, UnavailablePolicy.ALLOW, True),
    (PrincipalRequirement.OPTIONAL, None, UnavailablePolicy.DENY, False),
    (PrincipalRequirement.UNAVAILABLE, None, UnavailablePolicy.ALLOW, True),
    (PrincipalRequirement.UNAVAILABLE, None, UnavailablePolicy.DENY, False),
])
def test_optional_unavailable_policy_and_assurance_changes(world, status, identity, allow, success):
    rule = PrincipalEvidenceRule.UNAVAILABLE if status is PrincipalRequirement.UNAVAILABLE else PrincipalEvidenceRule.AUTHENTICATED_ACCOUNT
    manifest = replace(world.spec.manifest, principal=PrincipalPolicy(status, allow, rule))
    world.spec = replace(world.spec, manifest=manifest)
    world.observation = replace(world.observation, principal=k.PrincipalObservation(identity, identity is not None, rule))
    # Non-shipped policy fixtures enter only via an exact presented review.
    draft = world.intake.capture(world.agent, source="OWNER_DIRECT", raw_text="Draft", event_id="draft")
    if not success:
        with pytest.raises(ContractError):
            world.kernel.prepare_review(draft, world.spec)
        return
    request = world.kernel.prepare_review(draft, world.spec)
    world.review.present(world.presenter, request, request.snapshot.fingerprint)
    approval = world.review.approve(world.presenter, request, event_id="click", displayed_fingerprint=request.snapshot.fingerprint)
    handle = world.kernel.authorize(approval)
    world.observation = replace(world.observation, principal=k.PrincipalObservation("different", True, rule))
    world.target(StepKind.COMMIT)
    with pytest.raises(ContractError):
        world.kernel.claim(handle, StepKind.COMMIT)
    assert world.sink == []


@pytest.mark.parametrize("alteration", ["operation", "specification_digest", "runtime", "surface_digest",
    "destination", "principal_digest", "payload_digest", "new_object", "dispatch_correlated",
    "object_identity", "result_url", "vocabulary", "toast", "boolean"])
def test_core_rejects_forged_or_incomplete_remote_evidence(world, alteration):
    handle = world.authorize()
    world.send(handle, StepKind.COMMIT)
    proof = world.remote(handle)
    values = {"operation": "other", "specification_digest": "0" * 64, "runtime": "api", "surface_digest": "0" * 64,
        "destination": DestinationIdentity("reddit", "post", "t3_other"), "principal_digest": "0" * 64,
        "payload_digest": "0" * 64, "new_object": False, "dispatch_correlated": False, "object_identity": "",
        "result_url": "https://evil.example/result", "vocabulary": tuple(EvidenceKind)[:-1]}
    world.proof = {"remote_confirmed": True} if alteration == "toast" else True if alteration == "boolean" else (
        replace(proof, **{alteration: values[alteration]}))
    with pytest.raises(ContractError):
        world.kernel.reconcile(handle)
    assert world.kernel.receipt(handle).state is ReceiptState.DISPATCHED
    with pytest.raises(ContractError):
        world.kernel.claim(handle, StepKind.COMMIT)


@pytest.mark.parametrize("outcome", ["remote", "noncommit"])
def test_receipt_transitions_never_release_spent_even_after_revocation(world, outcome):
    handle = world.authorize()
    world.send(handle, StepKind.COMMIT)
    world.kernel.note_local_effect(handle)
    assert world.kernel.receipt(handle).state is ReceiptState.BROWSER_LOCAL_EFFECT_OBSERVED
    world.kernel.mark_ambiguous(handle)
    world.kernel.revoke()
    world.proof = world.remote(handle) if outcome == "remote" else k.NoncommitEvidence(
        handle.identifier, world.spec.digest, world.observation.surface_digest, True)
    world.kernel.reconcile(handle)
    state = ReceiptState.REMOTE_CONFIRMED if outcome == "remote" else ReceiptState.DEFINITE_NONCOMMIT
    assert world.kernel.receipt(handle).state is state
    world.kernel.allow()
    assert world.kernel.receipt(handle).state is state
    for call in (lambda: world.kernel.reconcile(handle), lambda: world.kernel.note_local_effect(handle),
                 lambda: world.kernel.claim(handle, StepKind.COMMIT)):
        with pytest.raises(ContractError):
            call()
    assert world.store.inspect(c.EventKey.from_ingress("desktop/owner/chat", "event"), c.RecordKind.SPENT)


def test_receipt_remote_reconciliation_has_one_atomic_terminal_winner(world):
    handle = world.authorize()
    world.send(handle, StepKind.COMMIT)
    world.proof = world.remote(handle)
    barrier = threading.Barrier(2)
    world.kernel._evidence = lambda op: (barrier.wait(timeout=20), world.proof)[1]
    def reconcile():
        try:
            world.kernel.reconcile(handle)
            return True
        except ContractError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: reconcile(), range(2)))
    assert sorted(results) == [False, True]


def test_expiry_is_monotonic_and_claim_needs_new_generation(world):
    handle = world.authorize()
    world.send(handle, StepKind.PREPARE)
    world.observation = replace(world.observation, target_kind=StepKind.FILL)
    with pytest.raises(ContractError):
        world.kernel.claim(handle, StepKind.FILL)
    world.time += o.LIFETIME_NS
    world.target(StepKind.COMMIT)
    with pytest.raises(ContractError):
        world.kernel.claim(handle, StepKind.COMMIT)


def test_imported_or_reconstructed_handles_and_spec_are_not_authority(world):
    admission = world.capture()
    for fake in (replace(admission), world.spec, {"owner": True, "event": admission.identifier}, True):
        with pytest.raises(ContractError):
            world.kernel.authorize(fake)
    handle = world.kernel.authorize(admission)
    with pytest.raises(ContractError):
        world.kernel.authorize(admission)
    other = k.AuthorityKernel(world.intake, world.observe)
    with pytest.raises(ContractError):
        other.authorize(admission)
    with pytest.raises(ContractError):
        other.claim(handle, StepKind.COMMIT)
    with pytest.raises(FrozenInstanceError):
        handle.identifier = "changed"


def test_nested_control_inside_observation_fails_closed(world):
    original = world.kernel._observe
    def revoked(spec, kind):
        world.kernel.revoke()
        return original(spec, kind)
    world.kernel._observe = revoked
    with pytest.raises(ContractError):
        world.authorize()
    assert world.sink == []


@pytest.mark.parametrize("control,restore", [("pause", "resume"), ("revoke", "allow")])
def test_control_before_binding_invalidates_admitted_turn_even_after_restore(world, control, restore):
    admission = world.capture()
    getattr(world.kernel, control)()
    getattr(world.kernel, restore)()
    with pytest.raises(ContractError):
        world.kernel.authorize(admission)
    fresh = world.capture("new-owner-decision")
    handle = world.kernel.authorize(fresh)
    world.send(handle, StepKind.COMMIT)
    assert len(world.sink) == 1


def test_nested_revoke_and_allow_cannot_hide_control_epoch(world):
    original = world.kernel._observe
    def revoked(spec, kind):
        world.kernel.revoke()
        world.kernel.allow()
        return original(spec, kind)
    world.kernel._observe = revoked
    with pytest.raises(ContractError):
        world.authorize()
    assert world.sink == []


@pytest.mark.parametrize("delta", [-1, k.FRESH_NS + 1])
def test_stale_or_future_clock_observation_fails_closed(world, delta):
    handle = world.authorize()
    world.target(StepKind.COMMIT)
    claim = world.kernel.claim(handle, StepKind.COMMIT)
    world.time += delta
    with pytest.raises(ContractError):
        world.kernel.consume_commit(handle, claim)
    assert world.sink == []


def test_wall_clock_changes_do_not_extend_monotonic_expiry(world, monkeypatch):
    import time
    handle = world.authorize()
    monkeypatch.setattr(time, "time", lambda: -10**20)
    world.time += o.LIFETIME_NS
    world.target(StepKind.COMMIT)
    with pytest.raises(ContractError):
        world.kernel.claim(handle, StepKind.COMMIT)
    assert world.kernel.receipt(handle).closed


def test_N01_reply_and_create_post_share_the_same_kernel_instance(tmp_path):
    world = World(tmp_path, "reddit.reply")
    try:
        kernel = world.kernel
        first_spec = world.spec
        reply = world.authorize()
        old_fill, _ = world.send(reply, StepKind.FILL)
        old_commit, _ = world.send(reply, StepKind.COMMIT)
        world.spec = spec_for("reddit.create_post")
        world.observation = replace(world.observation, destination=world.spec.destination,
                                   payload_digest=world.spec.payload.digest)
        create_post = kernel.authorize(world.capture("second-real-owner-event"))
        world.target(StepKind.FILL)
        fill = kernel.claim(create_post, StepKind.FILL)
        with pytest.raises(ContractError):
            kernel.consume_step(create_post, old_fill)
        world.sink.append(kernel.consume_step(create_post, fill))
        world.target(StepKind.COMMIT)
        commit = kernel.claim(create_post, StepKind.COMMIT)
        with pytest.raises(ContractError):
            kernel.consume_commit(create_post, old_commit)
        world.sink.append(kernel.consume_commit(create_post, commit))
        assert world.kernel is kernel
        assert kernel.receipt(reply).specification_digest == first_spec.digest
        assert kernel.receipt(create_post).specification_digest == world.spec.digest
        assert kernel.receipt(reply).capability == "reddit.reply"
        assert kernel.receipt(create_post).capability == "reddit.create_post"
        assert sum(p.kind is StepKind.COMMIT for p in world.sink) == 2
        assert len(list((world.data / "chrome_companion" / c.STORE_DIR).glob("*.spent.json"))) == 2
    finally:
        world.close()
