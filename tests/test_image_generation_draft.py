"""
tests/test_image_generation_draft.py -- MEDIA-GENERATION-CONVERSATIONAL-
RUNTIME-01.

core/image_generation_draft.py is the structural stage/confirm gate: the
owner's "yes" is never itself the authorization_ref, only a key that
unlocks consuming a specific, already-estimated draft exactly once.
"""
import time

import pytest

import core.image_generation_draft as draft_store


@pytest.fixture(autouse=True)
def _clean_module_state():
    """The store is module-level/in-memory by design (see the module
    docstring) -- reset it around every test so tests never see each
    other's drafts."""
    draft_store._drafts.clear()
    draft_store._approvals.clear()
    draft_store._presented.clear()
    yield
    draft_store._drafts.clear()
    draft_store._approvals.clear()
    draft_store._presented.clear()


def _stage(**overrides):
    kwargs = dict(
        specialist="higgsfield",
        model="higgsfield-ai/soul/standard",
        settings={"prompt": "a purple neon cassette deck"},
        cost_estimate=0.0938,
        cost_unit="usd",
        manifest_provider="higgsfield",
    )
    kwargs.update(overrides)
    return draft_store.stage_draft(**kwargs)


def test_stage_then_consume_returns_the_same_draft():
    staged = _stage()
    consumed = draft_store.consume_draft(staged.draft_id)

    assert consumed is not None
    assert consumed.draft_id == staged.draft_id
    assert consumed.specialist == "higgsfield"
    assert consumed.cost_estimate == 0.0938


def test_consume_is_single_use():
    staged = _stage()
    first = draft_store.consume_draft(staged.draft_id)
    second = draft_store.consume_draft(staged.draft_id)

    assert first is not None
    assert second is None  # already consumed -- gone, not reusable


def test_unknown_draft_id_returns_none():
    assert draft_store.consume_draft("not-a-real-draft-id") is None


def test_expired_draft_cannot_be_consumed():
    staged = _stage(ttl_seconds=0.01)
    time.sleep(0.05)

    assert draft_store.consume_draft(staged.draft_id) is None


def test_expired_draft_cannot_be_peeked_either():
    staged = _stage(ttl_seconds=0.01)
    time.sleep(0.05)

    assert draft_store.peek_draft(staged.draft_id) is None


def test_peek_does_not_consume():
    staged = _stage()
    peeked = draft_store.peek_draft(staged.draft_id)

    assert peeked is not None
    # Still consumable afterward -- peek must be read-only.
    assert draft_store.consume_draft(staged.draft_id) is not None


def test_discard_makes_a_draft_unconsumable():
    staged = _stage()
    draft_store.discard_draft(staged.draft_id)

    assert draft_store.consume_draft(staged.draft_id) is None


def test_settings_are_copied_not_aliased():
    original_settings = {"prompt": "x"}
    staged = _stage(settings=original_settings)
    original_settings["prompt"] = "mutated after staging"

    consumed = draft_store.consume_draft(staged.draft_id)
    assert consumed.settings["prompt"] == "x"


def test_two_drafts_from_the_same_inputs_get_different_ids():
    first = _stage()
    second = _stage()

    assert first.draft_id != second.draft_id
    # Both independently consumable -- staging is not itself a dedup key.
    assert draft_store.consume_draft(first.draft_id) is not None
    assert draft_store.consume_draft(second.draft_id) is not None
