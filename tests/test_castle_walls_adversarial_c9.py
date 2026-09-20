"""CASTLE-WALLS-ADVERSARIAL-01, phase C9 -- independent hostile evidence.

This file records a new approval-event replay violation found against
CASTLE-WALLS-REPAIR-03 (75309a4d).  It does not repair production code.
All provider behavior is mocked; no network, messages, credentials, or spend.
"""
from types import SimpleNamespace

import core.image_generation_draft as draft_store
import tools.image_generation as image_tool
from core.agent import _maybe_approve_pending_draft


CHANNEL = "telegram-owner"
MODEL = "higgsfield-ai/soul/standard"


def _stage_presented(*, prompt: str, turn_seq: int):
    draft = draft_store.stage_draft(
        specialist="higgsfield",
        model=MODEL,
        settings={"prompt": prompt},
        cost_estimate=0.10,
        cost_unit="usd",
        manifest_provider="higgsfield",
        channel_id=CHANNEL,
        chat_id=None,
        staged_at_turn_seq=turn_seq,
    )
    assert draft_store.mark_draft_presented(
        draft.draft_id, channel_id=CHANNEL, chat_id=None
    )
    return draft


def test_c9_replayed_owner_affirmation_authorizes_a_different_paid_draft(monkeypatch):
    """One unique owner approval event must not authorize two operations.

    The approval hook has no event/message identity and resolves an affirmation
    against whichever sole eligible draft exists *when that text is admitted*.
    Once the first draft is approved, find_pending_draft_id() excludes it.  If
    the same owner turn is replayed after a second presented draft appears, the
    identical text silently approves that different draft.  Both approvals then
    pass the real generate_image() spend gate and reach the mocked provider.
    """
    draft_store._drafts.clear()
    draft_store._approvals.clear()
    draft_store._presented.clear()

    submitted = []
    target = SimpleNamespace(
        specialist="higgsfield", model=MODEL, registry=object(), policy=object()
    )
    monkeypatch.setattr(
        image_tool.svc, "resolve_image_generation_target", lambda: target
    )
    monkeypatch.setattr(
        image_tool,
        "_build_adapter",
        lambda: (SimpleNamespace(estimate_cost=lambda **_kwargs: 0.10), None),
    )

    def fake_generate_image(**kwargs):
        submitted.append(kwargs)
        return SimpleNamespace(
            outcome="success",
            cost_estimate=0.10,
            diagnostic=None,
            artifacts=(),
            failed_output_indices=(),
            failed_manifest_indices=(),
        )

    monkeypatch.setattr(image_tool.svc, "generate_image", fake_generate_image)

    first = _stage_presented(prompt="first operation", turn_seq=0)

    # The owner's one genuine approval event is admitted once and approves the
    # only presented draft.  Keep the draft unconsumed to exercise the explicit
    # already-approved + unapproved mixture requested by the C9 attack order.
    owner_event_text = "yes"
    _maybe_approve_pending_draft(
        owner_event_text, "OWNER_DIRECT", CHANNEL, None
    )
    assert draft_store.is_approved(first.draft_id) is True

    second = _stage_presented(prompt="different second operation", turn_seq=1)

    # Simulate delayed/duplicate delivery of the SAME owner event.  No event id,
    # nonce, draft binding, or consumed-affirmation record reaches this hook, so
    # the replay is indistinguishable from fresh authority and selects `second`.
    _maybe_approve_pending_draft(
        owner_event_text, "OWNER_DIRECT", CHANNEL, None
    )

    assert draft_store.is_approved(first.draft_id) is True
    assert draft_store.is_approved(second.draft_id) is True

    first_result = image_tool.generate_image(
        first.draft_id, channel_id=CHANNEL, chat_id=None, current_turn_seq=2
    )
    second_result = image_tool.generate_image(
        second.draft_id, channel_id=CHANNEL, chat_id=None, current_turn_seq=2
    )

    assert "outcome: success" in first_result
    assert "outcome: success" in second_result
    assert [call["settings"]["prompt"] for call in submitted] == [
        "first operation",
        "different second operation",
    ]

    draft_store._drafts.clear()
    draft_store._approvals.clear()
    draft_store._presented.clear()
