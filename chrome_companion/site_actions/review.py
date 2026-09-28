"""Qt-free exact snapshot/presentation/click controller; no UI or tool wiring."""
from __future__ import annotations

from dataclasses import dataclass
import secrets
import threading

from .model import ActionSpec, ContractError
from .owner_intent import AdmittedIntent, OwnerAdmission, OwnerIntake, digest


@dataclass(frozen=True, slots=True)
class ReviewSnapshot:
    specification: ActionSpec
    principal: str | None
    principal_digest: str
    scope_digest: str
    boot_digest: str
    surface_digest: str
    parent_event_digest: str
    control_epoch: int
    expires_ns: int

    @property
    def fingerprint(self) -> str:
        return digest({"specification": self.specification.to_data(), "principal": self.principal,
                       "principal_digest": self.principal_digest, "scope": self.scope_digest,
                       "boot": self.boot_digest, "surface": self.surface_digest,
                       "parent_event": self.parent_event_digest,
                       "controls": self.control_epoch, "expiry": self.expires_ns})


@dataclass(frozen=True, slots=True)
class ReviewRequest:
    identifier: str
    snapshot: ReviewSnapshot


@dataclass(frozen=True, slots=True)
class ReviewApproval:
    identifier: str


class ReviewController:
    """Service-owned presenter identity, exact displayed bytes and direct click.

    The presenter object is not a model field. B1.4 must connect these methods
    to successful plain-text display and actual owner button events. A fake
    presenter here proves the controller contract, not the eventual Qt seam.
    """

    def __init__(self, intake: OwnerIntake, presenter):
        if type(intake) is not OwnerIntake or presenter is None:
            raise ContractError("trusted intake/presenter required")
        self.intake = intake
        self._presenter = presenter
        self._requests = {}
        self._approvals = {}
        self._lock = threading.RLock()

    def create(self, admission: OwnerAdmission, snapshot: ReviewSnapshot) -> ReviewRequest:
        with self._lock:
            admitted = self.intake.resolve(admission)
            if (type(snapshot) is not ReviewSnapshot or type(snapshot.specification) is not ActionSpec
                    or snapshot.scope_digest != admitted.scope_digest
                    or snapshot.boot_digest != admitted.metadata.boot_digest
                    or snapshot.parent_event_digest != admitted.key.digest
                    or snapshot.expires_ns != admitted.metadata.expires_ns):
                raise ContractError("review snapshot scope/lifetime mismatch")
            self.intake._require_review(admission)
            for identifier, entry in list(self._requests.items()):
                if entry[1] is admission:
                    del self._requests[identifier]
            for identifier, entry in list(self._approvals.items()):
                if entry[3] is admission:
                    del self._approvals[identifier]
            request = ReviewRequest(secrets.token_hex(32), snapshot)
            self._requests[request.identifier] = [request, admission, False]
            return request

    def _request(self, request: ReviewRequest):
        if type(request) is not ReviewRequest:
            raise ContractError("review request required")
        entry = self._requests.get(request.identifier)
        if entry is None or entry[0] is not request:
            raise ContractError("stale/copied/cross-controller review")
        self.intake.resolve(entry[1])
        return entry

    def present(self, presenter, request: ReviewRequest, displayed_fingerprint: str) -> None:
        with self._lock:
            if presenter is not self._presenter:
                raise ContractError("trusted presenter required")
            entry = self._request(request)
            if displayed_fingerprint != request.snapshot.fingerprint:
                raise ContractError("different snapshot displayed")
            entry[2] = True

    def approve(self, presenter, request: ReviewRequest, *, event_id: str,
                displayed_fingerprint: str) -> ReviewApproval:
        with self._lock:
            if presenter is not self._presenter:
                raise ContractError("trusted owner click required")
            entry = self._request(request)
            if not entry[2] or displayed_fingerprint != request.snapshot.fingerprint:
                raise ContractError("unpresented/changed snapshot")
            # Close first: uncertainty/repeated click cannot renew this request.
            del self._requests[request.identifier]
            admitted = self.intake._review_event(request.snapshot.fingerprint, request.snapshot.expires_ns, event_id)
            approval = ReviewApproval(secrets.token_hex(32))
            self._approvals[approval.identifier] = (approval, admitted, request.snapshot, entry[1])
            return approval

    def resolve(self, approval: ReviewApproval) -> tuple[AdmittedIntent, ReviewSnapshot]:
        with self._lock:
            if type(approval) is not ReviewApproval:
                raise ContractError("trusted review approval required")
            entry = self._approvals.get(approval.identifier)
            if entry is None or entry[0] is not approval:
                raise ContractError("unknown/copied/cross-controller approval")
            self.intake._owner(self.intake._agent)
            if self.intake.now() >= entry[1].metadata.expires_ns:
                raise ContractError("review approval expired")
            return entry[1], entry[2]

    def cancel(self, presenter, request: ReviewRequest) -> None:
        with self._lock:
            if presenter is not self._presenter:
                raise ContractError("trusted presenter required")
            self._request(request)
            del self._requests[request.identifier]

    def invalidate(self) -> None:
        with self._lock:
            self._requests.clear()
            self._approvals.clear()
