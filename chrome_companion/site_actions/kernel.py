"""Dormant shared authority reducer. Only inert permits; no actuator exists.

Observation/evidence providers are trusted service dependencies, supplied only
at construction. B1.3 uses explicit fake providers/sinks. They are not adapters,
tool arguments or a promise that live principal/target verification exists.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import secrets
import threading
from urllib.parse import urlsplit

from . import claims
from .model import (ActionSpec, ContractError, DestinationIdentity, EvidenceKind,
                    PrincipalEvidenceRule, PrincipalRequirement, ReceiptState,
                    Runtime, StepKind, UnavailablePolicy)
from .owner_intent import OwnerAdmission, OwnerIntake, digest, text
from .review import ReviewApproval, ReviewController, ReviewSnapshot


FRESH_NS = 5_000_000_000


class AuthorizationSource(str, Enum):
    OWNER_EXACT = "owner_exact"
    OWNER_REVIEW = "owner_review"


@dataclass(frozen=True, slots=True)
class PrincipalObservation:
    identity: str | None
    verified: bool
    rule: PrincipalEvidenceRule

    def __post_init__(self):
        if type(self.verified) is not bool or type(self.rule) is not PrincipalEvidenceRule:
            raise ContractError("invalid principal observation")
        if self.identity is not None:
            text(self.identity)
            if not self.verified:
                raise ContractError("unverified principal assertion")
        elif self.verified:
            raise ContractError("missing verified principal")

    @property
    def fingerprint(self) -> str:
        return digest({"identity": self.identity, "verified": self.verified, "rule": self.rule.value})


@dataclass(frozen=True, slots=True)
class Observation:
    runtime: Runtime
    instance: str
    connection: str
    worker_boot: str
    tab: int
    window: int
    origin: str
    url: str
    document: str
    target: str
    target_kind: StepKind
    generation: int
    observed_ns: int
    destination: DestinationIdentity
    payload_digest: str
    principal: PrincipalObservation
    unique: bool
    permission: bool
    allowed: bool
    paused: bool
    revoked: bool
    control_epoch: int

    def __post_init__(self):
        if (type(self.runtime) is not Runtime or type(self.target_kind) is not StepKind
                or type(self.destination) is not DestinationIdentity or type(self.principal) is not PrincipalObservation):
            raise ContractError("invalid observation types")
        for value in (self.instance, self.connection, self.worker_boot, self.origin,
                      self.url, self.document, self.target):
            text(value)
        for value in (self.tab, self.window, self.generation, self.observed_ns, self.control_epoch):
            claims._number(value)
        for value in (self.unique, self.permission, self.allowed, self.paused, self.revoked):
            if type(value) is not bool:
                raise ContractError("invalid observation control")
        claims._digest(self.payload_digest)
        parsed = urlsplit(self.url)
        if (parsed.scheme + "://" + parsed.netloc != self.origin or parsed.username is not None
                or parsed.password is not None or not parsed.path.startswith("/")
                or any(ord(c) < 33 or c == "\\" for c in self.url)):
            raise ContractError("noncanonical observation URL")

    @property
    def surface_digest(self) -> str:
        return digest({"runtime": self.runtime.value, "instance": self.instance,
                       "connection": self.connection, "worker_boot": self.worker_boot,
                       "tab": self.tab, "window": self.window, "origin": self.origin,
                       "controls": self.control_epoch})

    @property
    def target_digest(self) -> str:
        return digest({"surface": self.surface_digest, "url": self.url,
                       "document": self.document, "target": self.target,
                       "kind": self.target_kind.value, "generation": self.generation,
                       "destination": self.destination.to_data(), "payload": self.payload_digest,
                       "principal": self.principal.fingerprint})


@dataclass(frozen=True, slots=True)
class WorkflowAuthorization:
    identifier: str


@dataclass(frozen=True, slots=True)
class StepClaim:
    identifier: str
    workflow: str
    kind: StepKind
    target_digest: str


@dataclass(frozen=True, slots=True)
class CommitClaim:
    identifier: str
    workflow: str
    target_digest: str


@dataclass(frozen=True, slots=True)
class DispatchPermit:
    """Inert exact data, one possible handoff; never serialized to a runtime."""
    operation: str
    specification: ActionSpec
    kind: StepKind
    target: Observation


@dataclass(frozen=True, slots=True)
class ActionReceipt:
    operation: str
    specification_digest: str
    capability: str
    runtime: Runtime
    authorization_source: AuthorizationSource
    principal_digest: str
    state: ReceiptState
    spent_or_uncertain: bool
    closed: bool
    failure: str
    evidence: tuple[EvidenceKind, ...]


@dataclass(frozen=True, slots=True)
class RemoteEvidence:
    operation: str
    specification_digest: str
    runtime: Runtime
    surface_digest: str
    destination: DestinationIdentity
    principal_digest: str
    payload_digest: str
    new_object: bool
    dispatch_correlated: bool
    object_identity: str
    result_url: str
    vocabulary: tuple[EvidenceKind, ...]


@dataclass(frozen=True, slots=True)
class NoncommitEvidence:
    operation: str
    specification_digest: str
    surface_digest: str
    proven_noncommit: bool


class _Workflow:
    def __init__(self, handle, admitted, spec, observation, binding, source, epoch, parent_event):
        self.handle = handle
        self.admitted = admitted
        self.spec = spec
        self.observation = observation
        self.binding = binding
        self.epoch = epoch
        self.parent_event = parent_event
        self.active = True
        self.pending = None
        self.last_generation = -1
        self.counts = {}
        self.last_kind = -1
        self.commit_started = False
        self.permit_delivered = False
        self.receipt = ActionReceipt(handle.identifier, spec.digest, spec.manifest.capability_id,
                                     spec.runtime, source, observation.principal.fingerprint,
                                     ReceiptState.NOT_DISPATCHED, False, False, "", ())


class AuthorityKernel:
    def __init__(self, intake: OwnerIntake, observe, *, review: ReviewController | None = None, evidence=None):
        if type(intake) is not OwnerIntake or (review is not None and
                (type(review) is not ReviewController or review.intake is not intake)):
            raise ContractError("trusted intake/review boundary required")
        self.intake = intake
        self._observe = observe
        self._evidence = evidence
        self.review = review
        self._lock = threading.RLock()
        self._workflows = {}
        self._events = set()
        self._epoch = 0
        self._paused = False
        self._revoked = False
        self._retired = False

    def _controls(self):
        self.intake._owner(self.intake._agent)
        if self._paused or self._revoked or self._retired:
            raise ContractError("authority controls deny")

    def _principal(self, spec, principal):
        policy = spec.manifest.principal
        if principal.rule is not policy.evidence_rule:
            raise ContractError("wrong principal evidence rule")
        if policy.requirement is PrincipalRequirement.REQUIRED and principal.identity is None:
            raise ContractError("required principal unavailable")
        if policy.requirement is PrincipalRequirement.UNAVAILABLE and principal.identity is not None:
            raise ContractError("unavailable policy cannot assert a principal")
        if principal.identity is None and policy.unavailable is not UnavailablePolicy.ALLOW:
            raise ContractError("unavailable principal denied")

    def _observation(self, spec, admitted, kind=None):
        self._controls()
        epoch = self._epoch
        observation = self._observe(spec, kind)
        self._controls()  # a nested control event during observation still wins
        now = self.intake.now()
        if (self._epoch != epoch or not admitted.metadata.admitted_ns <= now < admitted.metadata.expires_ns
                or type(observation) is not Observation
                or observation.runtime is not spec.runtime or observation.origin not in spec.manifest.origins
                or observation.destination != spec.destination or observation.payload_digest != spec.payload.digest
                or not observation.unique or not observation.permission or not observation.allowed
                or observation.paused or observation.revoked
                or observation.observed_ns < admitted.metadata.admitted_ns
                or not 0 <= now - observation.observed_ns <= FRESH_NS
                or (kind is not None and observation.target_kind is not kind)):
            raise ContractError("stale/unverified/widened observation")
        self._principal(spec, observation.principal)
        return observation

    def prepare_review(self, admission: OwnerAdmission, spec: ActionSpec):
        with self._lock:
            if self.review is None or type(spec) is not ActionSpec:
                raise ContractError("trusted review unavailable")
            admitted = self.intake.resolve(admission)
            observation = self._observation(spec, admitted)
            snapshot = ReviewSnapshot(spec, observation.principal.identity, observation.principal.fingerprint,
                                      admitted.scope_digest, admitted.metadata.boot_digest,
                                      observation.surface_digest, admitted.key.digest, self._epoch,
                                      admitted.metadata.expires_ns)
            request = self.review.create(admission, snapshot)
            for workflow in self._workflows.values():
                if workflow.parent_event == admitted.key.digest:
                    self._invalidate(workflow, "review_replaced")
            return request

    def authorize(self, authority: OwnerAdmission | ReviewApproval) -> WorkflowAuthorization:
        with self._lock:
            snapshot = None
            if type(authority) is OwnerAdmission:
                admitted = self.intake.resolve_direct(authority)
                spec = admitted.specification
                source = AuthorizationSource.OWNER_EXACT
                if spec is None:
                    raise ContractError("generated/revised payload requires owner review")
            elif type(authority) is ReviewApproval and self.review is not None:
                admitted, snapshot = self.review.resolve(authority)
                spec = snapshot.specification
                source = AuthorizationSource.OWNER_REVIEW
            else:
                raise ContractError("trusted admission or review approval required")
            observation = self._observation(spec, admitted)
            if snapshot is not None and (snapshot.surface_digest != observation.surface_digest
                    or snapshot.principal_digest != observation.principal.fingerprint
                    or snapshot.control_epoch != self._epoch):
                raise ContractError("review scope changed since display")
            if admitted.key.digest in self._events:
                raise ContractError("event already bound in this kernel")
            # A binding may never be adopted into another reducer or after restart.
            self._events.add(admitted.key.digest)
            binding = claims.BindingMetadata(spec.digest, spec.manifest.fingerprint,
                                             observation.principal.fingerprint,
                                             digest({"scope": admitted.scope_digest, "surface": observation.surface_digest}),
                                             spec.runtime)
            if not self.intake.store.bind(admitted.key, binding):
                raise ContractError("existing binding cannot reconstruct live authority")
            # A nested/during-storage control cannot slip through binding.
            self._controls()
            fresh = self._observation(spec, admitted)
            if fresh.surface_digest != observation.surface_digest or fresh.principal != observation.principal:
                raise ContractError("scope changed during authorization binding")
            handle = WorkflowAuthorization(secrets.token_hex(32))
            parent_event = admitted.key.digest if snapshot is None else snapshot.parent_event_digest
            self._workflows[handle.identifier] = _Workflow(handle, admitted, spec, fresh, binding, source, self._epoch, parent_event)
            return handle

    def _workflow(self, handle):
        if type(handle) is not WorkflowAuthorization:
            raise ContractError("workflow authorization required")
        workflow = self._workflows.get(handle.identifier)
        if workflow is None or workflow.handle is not handle:
            raise ContractError("unknown/copied/cross-kernel workflow")
        return workflow

    def _invalidate(self, workflow, reason):
        workflow.active = False
        workflow.pending = None
        if workflow.receipt.state not in {ReceiptState.REMOTE_CONFIRMED, ReceiptState.DEFINITE_NONCOMMIT}:
            state = ReceiptState.AMBIGUOUS_AFTER_DISPATCH if workflow.permit_delivered else ReceiptState.NOT_DISPATCHED
            workflow.receipt = replace(workflow.receipt, state=state, closed=True, failure=reason)

    def _current(self, workflow, kind=None):
        try:
            if not workflow.active or workflow.epoch != self._epoch:
                raise ContractError("workflow invalidated")
            observation = self._observation(workflow.spec, workflow.admitted, kind)
            if (observation.surface_digest != workflow.observation.surface_digest
                    or observation.principal != workflow.observation.principal):
                raise ContractError("runtime/surface/account changed")
            return observation
        except Exception:
            self._invalidate(workflow, "invalidated")
            raise

    def claim(self, handle: WorkflowAuthorization, kind: StepKind) -> StepClaim | CommitClaim:
        with self._lock:
            workflow = self._workflow(handle)
            if type(kind) is not StepKind or workflow.pending is not None or workflow.commit_started:
                raise ContractError("wrong kind/pending/closed commit claim")
            declarations = workflow.spec.manifest.steps
            index = next((i for i, step in enumerate(declarations) if step.kind is kind), None)
            if (index is None or index < workflow.last_kind
                    or workflow.counts.get(kind, 0) >= declarations[index].max_count):
                raise ContractError("undeclared/exhausted/backward workflow step")
            observation = self._current(workflow, kind)
            if observation.generation <= workflow.last_generation:
                raise ContractError("fresh step generation required")
            if kind is StepKind.COMMIT:
                claim = CommitClaim(secrets.token_hex(32), handle.identifier, observation.target_digest)
            else:
                claim = StepClaim(secrets.token_hex(32), handle.identifier, kind, observation.target_digest)
            workflow.pending = claim
            return claim

    def _claimed(self, workflow, claim, kind):
        if workflow.pending is not claim or (kind is StepKind.COMMIT and type(claim) is not CommitClaim) or (
                kind is not StepKind.COMMIT and (type(claim) is not StepClaim or claim.kind is not kind)):
            raise ContractError("wrong/copied/replayed claim")
        observation = self._current(workflow, kind)
        if observation.target_digest != claim.target_digest:
            self._invalidate(workflow, "target_changed")
            raise ContractError("step target/document changed")
        return observation

    def consume_step(self, handle: WorkflowAuthorization, claim: StepClaim) -> DispatchPermit:
        with self._lock:
            workflow = self._workflow(handle)
            if type(claim) is not StepClaim or claim.kind is StepKind.COMMIT:
                raise ContractError("per-mutation noncommit step claim required")
            observation = self._claimed(workflow, claim, claim.kind)
            workflow.pending = None
            workflow.counts[claim.kind] = workflow.counts.get(claim.kind, 0) + 1
            workflow.last_kind = next(i for i, step in enumerate(workflow.spec.manifest.steps) if step.kind is claim.kind)
            workflow.last_generation = observation.generation
            return DispatchPermit(secrets.token_hex(32), workflow.spec, claim.kind, observation)

    def consume_commit(self, handle: WorkflowAuthorization, claim: CommitClaim) -> DispatchPermit:
        with self._lock:
            workflow = self._workflow(handle)
            observation = self._claimed(workflow, claim, StepKind.COMMIT)
            if workflow.commit_started:
                raise ContractError("commit already consumed/uncertain")
            workflow.commit_started = True
            workflow.pending = None
            workflow.receipt = replace(workflow.receipt, spent_or_uncertain=True, closed=True)
        # No kernel lock during durable I/O: Revoke/PAUSE/retirement can win.
        try:
            if not self.intake.store.consume(workflow.admitted.key, workflow.binding):
                raise ContractError("commit slot already spent or obstructed")
            with self._lock:
                current = self._current(workflow, StepKind.COMMIT)
                if current.target_digest != observation.target_digest:
                    raise ContractError("commit target changed while spending")
                workflow.active = False
                workflow.permit_delivered = True
                workflow.receipt = replace(workflow.receipt, state=ReceiptState.DISPATCHED)
                return DispatchPermit(handle.identifier, workflow.spec, StepKind.COMMIT, current)
        except Exception:
            with self._lock:
                self._invalidate(workflow, "spend_or_delivery_refused")
            raise

    def _control(self, name, enabled):
        with self._lock:
            self._epoch += 1
            if name == "pause":
                self._paused = enabled
            elif name == "revoke":
                self._revoked = enabled
            elif name == "retire":
                self._retired = True
            for workflow in self._workflows.values():
                self._invalidate(workflow, name)
            if self.review is not None:
                self.review.invalidate()
            self.intake.invalidate()
            if name == "retire":
                self.intake.retire()

    def pause(self):
        self._control("pause", True)

    def resume(self):
        self._control("pause", False)

    def revoke(self):
        self._control("revoke", True)

    def allow(self):
        self._control("revoke", False)

    def retire(self):
        self._control("retire", True)

    def receipt(self, handle: WorkflowAuthorization) -> ActionReceipt:
        with self._lock:
            workflow = self._workflow(handle)
            unresolved = workflow.receipt.state in {ReceiptState.DISPATCHED, ReceiptState.BROWSER_LOCAL_EFFECT_OBSERVED}
            if (workflow.active or unresolved) and self.intake.now() >= workflow.admitted.metadata.expires_ns:
                self._invalidate(workflow, "expired")
            return workflow.receipt

    def note_local_effect(self, handle: WorkflowAuthorization):
        with self._lock:
            workflow = self._workflow(handle)
            if workflow.receipt.state is not ReceiptState.DISPATCHED:
                raise ContractError("local observation cannot revive or override receipt")
            workflow.receipt = replace(workflow.receipt, state=ReceiptState.BROWSER_LOCAL_EFFECT_OBSERVED)

    def mark_ambiguous(self, handle: WorkflowAuthorization):
        with self._lock:
            workflow = self._workflow(handle)
            if workflow.receipt.state not in {ReceiptState.DISPATCHED, ReceiptState.BROWSER_LOCAL_EFFECT_OBSERVED}:
                raise ContractError("no pending possible dispatch")
            self._invalidate(workflow, "answer_lost")

    def reconcile(self, handle: WorkflowAuthorization):
        with self._lock:
            workflow = self._workflow(handle)
            if not workflow.permit_delivered or self._evidence is None:
                raise ContractError("no dispatch evidence provider")
            if workflow.receipt.state in {ReceiptState.REMOTE_CONFIRMED, ReceiptState.DEFINITE_NONCOMMIT}:
                raise ContractError("terminal receipt already won")
        evidence = self._evidence(workflow.receipt.operation)
        with self._lock:
            if workflow.receipt.state in {ReceiptState.REMOTE_CONFIRMED, ReceiptState.DEFINITE_NONCOMMIT}:
                raise ContractError("terminal receipt already won")
            if (type(evidence) not in {RemoteEvidence, NoncommitEvidence}
                    or evidence.operation != workflow.receipt.operation
                    or evidence.specification_digest != workflow.spec.digest
                    or evidence.surface_digest != workflow.observation.surface_digest):
                raise ContractError("unbound evidence")
            if type(evidence) is NoncommitEvidence:
                if evidence.proven_noncommit is not True:
                    raise ContractError("noncommit unproven")
                state, vocabulary = ReceiptState.DEFINITE_NONCOMMIT, ()
            else:
                if (evidence.runtime is not workflow.spec.runtime or evidence.destination != workflow.spec.destination
                        or evidence.principal_digest != workflow.observation.principal.fingerprint
                        or evidence.payload_digest != workflow.spec.payload.digest
                        or evidence.new_object is not True or evidence.dispatch_correlated is not True
                        or type(evidence.vocabulary) is not tuple
                        or any(type(item) is not EvidenceKind for item in evidence.vocabulary)
                        or len(evidence.vocabulary) != len(EvidenceKind)
                        or set(evidence.vocabulary) != set(workflow.spec.manifest.evidence)):
                    raise ContractError("remote evidence threshold not met")
                text(evidence.object_identity)
                text(evidence.result_url)
                url = urlsplit(evidence.result_url)
                if (url.scheme + "://" + url.netloc != workflow.observation.origin
                        or url.username is not None or url.password is not None
                        or not url.path.startswith("/") or any(ord(c) < 33 or c == "\\" for c in evidence.result_url)):
                    raise ContractError("invalid canonical result location")
                state, vocabulary = ReceiptState.REMOTE_CONFIRMED, evidence.vocabulary
            workflow.receipt = replace(workflow.receipt, state=state, evidence=vocabulary, closed=True)
