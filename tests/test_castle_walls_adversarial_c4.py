"""CASTLE-WALLS-ADVERSARIAL-01, phase C4 money-wall reproducers.

The provider submission function is always a local in-memory stub.  No
credential lookup, network call, artifact write, or real spend is possible.
"""

from threading import Barrier, Thread
from types import SimpleNamespace

import pytest

import core.image_generation_draft as draft_store
import tools.image_generation as image_tool


SPECIALIST = "higgsfield"
MODEL = "higgsfield-ai/soul/standard"


@pytest.fixture(autouse=True)
def _clean_drafts():
    draft_store._drafts.clear()
    yield
    draft_store._drafts.clear()


class _EstimateOnlyAdapter:
    def __init__(self, estimate=0.0938):
        self.estimate = estimate

    def estimate_cost(self, *, model, settings):
        return self.estimate


def _stage(*, channel_id=None, settings=None, ttl_seconds=600):
    return draft_store.stage_draft(
        specialist=SPECIALIST,
        model=MODEL,
        settings=settings or {"prompt": "synthetic purple deck"},
        cost_estimate=0.0938,
        cost_unit="usd",
        manifest_provider="higgsfield",
        channel_id=channel_id,
        ttl_seconds=ttl_seconds,
    )


def _arm_mock_submission(monkeypatch, calls, *, estimate=0.0938):
    target = SimpleNamespace(
        specialist=SPECIALIST,
        model=MODEL,
        registry=object(),
        policy=object(),
    )
    monkeypatch.setattr(
        image_tool.svc,
        "resolve_image_generation_target",
        lambda: target,
    )
    monkeypatch.setattr(
        image_tool,
        "_build_adapter",
        lambda: (_EstimateOnlyAdapter(estimate), None),
    )

    def fake_generate_image(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            outcome="success",
            cost_estimate=0.0938,
            diagnostic=None,
            artifacts=(),
            failed_output_indices=(),
            failed_manifest_indices=(),
        )

    monkeypatch.setattr(image_tool.svc, "generate_image", fake_generate_image)


def test_c4_draft_possession_alone_reaches_mock_provider_submission(monkeypatch):
    submissions = []
    _arm_mock_submission(monkeypatch, submissions)
    draft = _stage()

    result = image_tool.generate_image(draft.draft_id)

    assert "outcome: success" in result
    assert len(submissions) == 1
    assert submissions[0]["authorization_ref"] == draft.draft_id


def test_c4_draft_from_another_channel_is_accepted(monkeypatch):
    submissions = []
    _arm_mock_submission(monkeypatch, submissions)
    draft = _stage(channel_id="owner-channel-a")

    # generate_image has no caller/channel argument, so this represents a
    # different owner runtime possessing channel A's identifier.
    result = image_tool.generate_image(draft.draft_id)

    assert "outcome: success" in result
    assert len(submissions) == 1
    assert submissions[0]["authorization_ref"] == draft.draft_id


def test_c4_concurrent_draft_consumption_has_exactly_one_winner():
    draft = _stage()
    barrier = Barrier(9)
    outcomes = []

    def consume():
        barrier.wait()
        outcomes.append(draft_store.consume_draft(draft.draft_id) is not None)

    threads = [Thread(target=consume) for _ in range(8)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 7


def test_c4_fabricated_replayed_expired_and_restart_drafts_fail_closed():
    assert draft_store.consume_draft("fabricated-draft") is None

    replay = _stage()
    assert draft_store.consume_draft(replay.draft_id) is not None
    assert draft_store.consume_draft(replay.draft_id) is None

    expired = _stage(ttl_seconds=-1)
    assert draft_store.consume_draft(expired.draft_id) is None

    restarted = _stage()
    draft_store._drafts.clear()  # Exact process-lifetime state loss.
    assert draft_store.consume_draft(restarted.draft_id) is None


def test_c4_staged_settings_are_immutable_against_caller_mutation():
    settings = {"prompt": "operation A", "width": 1280}
    draft = _stage(settings=settings)

    settings["prompt"] = "operation B"
    settings["width"] = 4096

    stored = draft_store.peek_draft(draft.draft_id)
    assert stored.settings == {"prompt": "operation A", "width": 1280}


def test_c4_price_drift_refuses_before_mock_submission(monkeypatch):
    submissions = []
    _arm_mock_submission(monkeypatch, submissions, estimate=0.1000)
    draft = _stage()

    result = image_tool.generate_image(draft.draft_id)

    assert "outcome: draft_stale" in result
    assert "cost changed" in result
    assert submissions == []
