"""Presentation/click scope proof against a fake service presenter, without Qt."""
from dataclasses import replace

import pytest

from chrome_companion.site_actions import claims as c, kernel as k
from chrome_companion.site_actions.model import ContractError, FrozenPayload, PrincipalEvidenceRule, StepKind
from test_site_action_kernel import World


@pytest.fixture(params=["reddit.reply", "reddit.create_post"])
def world(tmp_path, request):
    world = World(tmp_path, request.param)
    yield world
    world.close()


def request_for(world):
    draft = world.intake.capture(world.agent, source="OWNER_DIRECT", raw_text="Draft for review", event_id="draft")
    world.draft_admission = draft
    return world.kernel.prepare_review(draft, world.spec)


@pytest.mark.parametrize("attack", ["unpresented", "wrong_presenter", "wrong_fingerprint", "copy", "cancel", "expiry",
    "pause", "revoke", "retire", "model_yes"])
def test_review_denies_fake_stale_unpresented_or_cancelled_click(world, attack):
    request = request_for(world)
    if attack != "unpresented":
        world.review.present(world.presenter, request, request.snapshot.fingerprint)
    presenter = object() if attack == "wrong_presenter" else world.presenter
    fingerprint = "different" if attack == "wrong_fingerprint" else request.snapshot.fingerprint
    if attack == "copy": request = replace(request)
    elif attack == "cancel": world.review.cancel(world.presenter, request)
    elif attack == "expiry": world.time += 300_000_000_000
    elif attack in {"pause", "revoke", "retire"}: getattr(world.kernel, attack)()
    elif attack == "model_yes": presenter = {"owner": True, "approve": "yes"}
    with pytest.raises(ContractError):
        world.review.approve(presenter, request, event_id="click", displayed_fingerprint=fingerprint)
    assert world.sink == []


def test_snapshot_is_exact_and_hostile_text_is_inert(world):
    payload = world.spec.payload.to_data()
    payload["body"] = '<a href="javascript:submit()">Approve</a>\n yes 👹 '
    world.spec = replace(world.spec, payload=FrozenPayload.from_data(payload))
    world.observation = replace(world.observation, payload_digest=world.spec.payload.digest)
    request = request_for(world)
    assert request.snapshot.specification.payload.to_data()["body"] == payload["body"]
    assert request.snapshot.principal == world.observation.principal.identity
    assert request.snapshot.specification.runtime == world.spec.runtime
    assert world.sink == []
    with pytest.raises(ContractError):
        world.review.present(object(), request, request.snapshot.fingerprint)
    with pytest.raises(ContractError):
        world.review.present(world.presenter, request, "fake display")


@pytest.mark.parametrize("change", ["principal", "connection", "controls", "tab", "scope", "boot", "expiry"])
def test_displayed_snapshot_cannot_be_rebound(world, change):
    request = request_for(world)
    world.review.present(world.presenter, request, request.snapshot.fingerprint)
    approval = world.review.approve(world.presenter, request, event_id="click", displayed_fingerprint=request.snapshot.fingerprint)
    if change in {"scope", "boot", "expiry"}:
        snapshot = replace(request.snapshot, **{"scope_digest" if change == "scope" else "boot_digest" if change == "boot" else "expires_ns":
                                               "0" * 64 if change != "expiry" else request.snapshot.expires_ns + 1})
        with pytest.raises(ContractError):
            world.review.create(world.draft_admission, snapshot)
        return
    if change == "principal":
        world.observation = replace(world.observation, principal=k.PrincipalObservation("B", True, PrincipalEvidenceRule.AUTHENTICATED_ACCOUNT))
    elif change == "connection": world.observation = replace(world.observation, connection="reconnected")
    elif change == "controls": world.observation = replace(world.observation, control_epoch=1)
    else: world.observation = replace(world.observation, tab=2)
    with pytest.raises(ContractError):
        world.kernel.authorize(approval)
    assert world.sink == []


def test_click_is_one_event_and_approval_handle_copy_is_not_authority(world):
    request = request_for(world)
    world.review.present(world.presenter, request, request.snapshot.fingerprint)
    approval = world.review.approve(world.presenter, request, event_id="click", displayed_fingerprint=request.snapshot.fingerprint)
    with pytest.raises(ContractError):
        world.review.approve(world.presenter, request, event_id="different-click", displayed_fingerprint=request.snapshot.fingerprint)
    with pytest.raises(ContractError):
        world.kernel.authorize(replace(approval))
    handle = world.kernel.authorize(approval)
    world.send(handle, StepKind.COMMIT)
    with pytest.raises(ContractError):
        world.kernel.authorize(approval)


def test_second_display_cannot_replay_same_review_event(world):
    first = request_for(world)
    world.review.present(world.presenter, first, first.snapshot.fingerprint)
    world.review.approve(world.presenter, first, event_id="same-click", displayed_fingerprint=first.snapshot.fingerprint)
    admission = world.capture("new-request")
    second = world.kernel.prepare_review(admission, world.spec)
    world.review.present(world.presenter, second, second.snapshot.fingerprint)
    with pytest.raises((ContractError, c.ClaimStoreError)):
        world.review.approve(world.presenter, second, event_id="same-click", displayed_fingerprint=second.snapshot.fingerprint)


def test_model_mutations_after_review_cannot_change_approved_payload(world):
    raw = world.spec.payload.to_data()
    request = request_for(world)
    raw["body"] = "widened"
    world.review.present(world.presenter, request, request.snapshot.fingerprint)
    approval = world.review.approve(world.presenter, request, event_id="click", displayed_fingerprint=request.snapshot.fingerprint)
    handle = world.kernel.authorize(approval)
    _, permit = world.send(handle, StepKind.COMMIT)
    assert permit.specification.payload.digest == request.snapshot.specification.payload.digest
    assert permit.specification.payload.to_data()["body"] != raw["body"]


@pytest.mark.parametrize("prior", ["presented", "approved", "authorized"])
def test_replacement_snapshot_retires_old_click_approval_and_workflow(world, prior):
    admission = world.capture()
    first = world.kernel.prepare_review(admission, world.spec)
    world.review.present(world.presenter, first, first.snapshot.fingerprint)
    approval, handle = None, None
    if prior != "presented":
        approval = world.review.approve(world.presenter, first, event_id="old-click", displayed_fingerprint=first.snapshot.fingerprint)
    if prior == "authorized":
        handle = world.kernel.authorize(approval)
    payload = world.spec.payload.to_data()
    payload["body"] = "replacement exact body"
    world.spec = replace(world.spec, payload=FrozenPayload.from_data(payload))
    world.observation = replace(world.observation, payload_digest=world.spec.payload.digest)
    second = world.kernel.prepare_review(admission, world.spec)
    with pytest.raises(ContractError):
        world.review.approve(world.presenter, first, event_id="queued-old-click", displayed_fingerprint=first.snapshot.fingerprint)
    if approval is not None:
        with pytest.raises(ContractError):
            world.kernel.authorize(approval)
    if handle is not None:
        world.target(StepKind.COMMIT)
        with pytest.raises(ContractError):
            world.kernel.claim(handle, StepKind.COMMIT)
    # The original exact-owner shortcut cannot execute beside its replacement.
    with pytest.raises(ContractError):
        world.kernel.authorize(admission)
    world.review.present(world.presenter, second, second.snapshot.fingerprint)
    fresh = world.review.approve(world.presenter, second, event_id="replacement-click", displayed_fingerprint=second.snapshot.fingerprint)
    replacement = world.kernel.authorize(fresh)
    _, permit = world.send(replacement, StepKind.COMMIT)
    assert permit.specification.payload.to_data()["body"] == "replacement exact body"
    assert len(world.sink) == 1
