"""CASTLE-WALLS-REPAIR-04 repaired-state regressions for CANNON-11.

Intentionally separate from the frozen C9 hostile evidence corpus
(tests/test_castle_walls_adversarial_c9.py, never edited -- see
tests/conftest.py's castle_walls_evidence marker). This proves the
repaired exactly-once authorization-event invariant directly with
synthetic data and providers -- no network, no real Telegram, no spend.
"""
from types import SimpleNamespace

import pytest

import core.idempotency as idempotency
import core.image_generation_draft as draft_store
import tools.image_generation as image_tool
from core.agent import _maybe_approve_pending_draft

SPECIALIST = "higgsfield"
MODEL = "higgsfield-ai/soul/standard"
CHANNEL = "telegram-owner"
CHAT_ID = None


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    # The exactly-once ledger is disk-backed by design (see
    # test_replay_after_restart_fails_closed_when_ledger_survives_the_
    # restart below) -- isolate it per test exactly like
    # tests/test_idempotency.py's own isolated_ledger fixture, or these
    # tests would see each other's claimed event ids.
    monkeypatch.setattr(idempotency, "LEDGER_PATH", str(tmp_path / "ledger.db"))
    draft_store._drafts.clear()
    draft_store._approvals.clear()
    draft_store._presented.clear()
    yield
    draft_store._drafts.clear()
    draft_store._approvals.clear()
    draft_store._presented.clear()


def _stage_presented(*, channel_id=CHANNEL, chat_id=CHAT_ID, cost=0.10,
                      prompt="x", turn_seq=0):
    draft = draft_store.stage_draft(
        specialist=SPECIALIST, model=MODEL, settings={"prompt": prompt},
        cost_estimate=cost, cost_unit="usd", manifest_provider="higgsfield",
        channel_id=channel_id, chat_id=chat_id, staged_at_turn_seq=turn_seq,
    )
    assert draft_store.mark_draft_presented(draft.draft_id, channel_id=channel_id, chat_id=chat_id)
    return draft


def _arm_mock_submission(monkeypatch, calls):
    target = SimpleNamespace(specialist=SPECIALIST, model=MODEL, registry=object(), policy=object())
    monkeypatch.setattr(image_tool.svc, "resolve_image_generation_target", lambda: target)
    monkeypatch.setattr(image_tool, "_build_adapter", lambda: (
        SimpleNamespace(estimate_cost=lambda **_kwargs: 0.10), None))

    def fake_generate_image(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(outcome="success", cost_estimate=0.10, diagnostic=None,
                                artifacts=(), failed_output_indices=(), failed_manifest_indices=())

    monkeypatch.setattr(image_tool.svc, "generate_image", fake_generate_image)


# ===========================================================================
# Core exactly-once invariant -- CANNON-11 itself, repaired
# ===========================================================================

def test_same_event_replayed_after_approval_grants_no_new_authority():
    """The exact frozen C9 scenario, repaired: replaying the same owner
    event after a second, different draft appears must not approve it."""
    event = "telegram:42:1001"
    first = _stage_presented(cost=0.10, prompt="first operation")

    _maybe_approve_pending_draft("yes", "OWNER_DIRECT", CHANNEL, CHAT_ID, event)
    assert draft_store.is_approved(first.draft_id) is True

    second = _stage_presented(cost=5.00, prompt="different second operation")
    _maybe_approve_pending_draft("yes", "OWNER_DIRECT", CHANNEL, CHAT_ID, event)

    assert draft_store.is_approved(first.draft_id) is True
    assert draft_store.is_approved(second.draft_id) is False


def test_same_event_admitted_twice_produces_at_most_one_provider_submission(monkeypatch):
    """Provider submission count must never exceed the number of unique
    owner authorization events bound to eligible drafts -- here exactly
    one event, so exactly one submission, even though it's admitted twice
    and two drafts exist."""
    submissions = []
    _arm_mock_submission(monkeypatch, submissions)
    event = "telegram:42:1002"
    first = _stage_presented(cost=0.10, prompt="first")

    _maybe_approve_pending_draft("yes", "OWNER_DIRECT", CHANNEL, CHAT_ID, event)
    second = _stage_presented(cost=5.00, prompt="second")
    _maybe_approve_pending_draft("yes", "OWNER_DIRECT", CHANNEL, CHAT_ID, event)

    first_result = image_tool.generate_image(first.draft_id, channel_id=CHANNEL, chat_id=CHAT_ID,
                                              current_turn_seq=1)
    second_result = image_tool.generate_image(second.draft_id, channel_id=CHANNEL, chat_id=CHAT_ID,
                                               current_turn_seq=1)

    assert "outcome: success" in first_result
    assert "outcome: success" not in second_result
    assert len(submissions) == 1
    assert submissions[0]["settings"]["prompt"] == "first"


def test_delayed_replay_after_a_different_estimate_is_presented_grants_zero_authority():
    """Frames the same invariant as a delayed/duplicate transport delivery
    arriving after the owner's real spend already completed and moved on."""
    event = "telegram:42:1003"
    first = _stage_presented(cost=0.10)
    _maybe_approve_pending_draft("yes", "OWNER_DIRECT", CHANNEL, CHAT_ID, event)
    draft_store.consume_draft(first.draft_id)  # the real spend completes

    later = _stage_presented(cost=9.99)
    _maybe_approve_pending_draft("yes", "OWNER_DIRECT", CHANNEL, CHAT_ID, event)

    assert draft_store.is_approved(later.draft_id) is False


# ===========================================================================
# Distinctness -- identity is never derived from message text
# ===========================================================================

def test_same_text_from_two_genuinely_different_events_is_distinguishable():
    a = _stage_presented(cost=0.10)
    _maybe_approve_pending_draft("yes", "OWNER_DIRECT", CHANNEL, CHAT_ID, "telegram:42:2001")
    assert draft_store.is_approved(a.draft_id) is True

    b = _stage_presented(cost=5.00)
    _maybe_approve_pending_draft("yes", "OWNER_DIRECT", CHANNEL, CHAT_ID, "telegram:42:2002")
    assert draft_store.is_approved(b.draft_id) is True


def test_same_text_from_two_separate_gui_submissions_is_not_deduplicated_by_content():
    """Two GUI 'yes' messages are two distinct owner actions -- proven
    here with two independently-minted identities, exactly what
    LuminaAgent.chat()'s own auto-mint produces for two real calls (see
    test_chat_mints_a_fresh_approval_event_id_per_call below for the mint
    itself)."""
    import uuid
    a = _stage_presented(cost=0.10)
    _maybe_approve_pending_draft("yes", "OWNER_DIRECT", CHANNEL, CHAT_ID, uuid.uuid4().hex)
    assert draft_store.is_approved(a.draft_id) is True

    b = _stage_presented(cost=5.00)
    _maybe_approve_pending_draft("yes", "OWNER_DIRECT", CHANNEL, CHAT_ID, uuid.uuid4().hex)
    assert draft_store.is_approved(b.draft_id) is True


def test_chat_mints_a_fresh_approval_event_id_per_call(monkeypatch):
    """Direct proof at LuminaAgent.chat()'s own boundary: two calls with
    byte-identical user_input get two independent, non-None identities.
    The model has no path to this parameter -- chat() mints it before
    _chat_impl() ever runs, from uuid.uuid4(), never from user_input."""
    import core.agent as agent_module
    from core.agent import LuminaAgent

    class _StopHereLLM:
        name = "mock-stop-here"
        display_name = "Mock Stop Here"
        supports_required_tool_choice = True

        def get_model(self):
            return "mock-model"

        def configured_model(self):
            return "mock-model"

        def chat(self, *args, **kwargs):
            raise RuntimeError("stop here -- never reached by this test")

    monkeypatch.setattr(agent_module, "get_llm_backend", lambda name=None: _StopHereLLM())
    agent = LuminaAgent(owner=True, channel_id="cannon11-mint-test")

    seen = []
    real_hook = agent_module._maybe_approve_pending_draft

    def _spy(user_input, source, channel_id, chat_id, approval_event_id=None):
        seen.append(approval_event_id)
        return real_hook(user_input, source, channel_id, chat_id, approval_event_id)

    monkeypatch.setattr(agent_module, "_maybe_approve_pending_draft", _spy)

    for _ in range(2):
        try:
            agent.chat("yes", source="OWNER_DIRECT")
        except Exception:
            pass  # the fake backend always raises -- irrelevant to this test

    assert len(seen) == 2
    assert seen[0] is not None and seen[1] is not None
    assert seen[0] != seen[1]


# ===========================================================================
# Migration -- a spent event's authority cannot cross a context boundary
# ===========================================================================

def test_cross_channel_and_cross_chat_replay_cannot_migrate_authority():
    event = "telegram:42:3001"
    a = _stage_presented(channel_id="channel-a", chat_id=1, cost=0.10)
    _maybe_approve_pending_draft("yes", "OWNER_DIRECT", "channel-a", 1, event)
    assert draft_store.is_approved(a.draft_id) is True

    b = _stage_presented(channel_id="channel-b", chat_id=2, cost=5.00)
    _maybe_approve_pending_draft("yes", "OWNER_DIRECT", "channel-b", 2, event)
    assert draft_store.is_approved(b.draft_id) is False


# ===========================================================================
# An event that granted nothing must not become a permanent bearer token
# ===========================================================================

def test_event_with_no_candidate_remains_usable_once_one_appears():
    """event -> no candidate -> candidate later appears -> replay. Nothing
    was spent the first time (no eligible draft existed), so this must NOT
    be treated the same as a real prior approval -- the event's authority
    survives to catch the draft once it exists."""
    event = "telegram:42:5001"
    _maybe_approve_pending_draft("yes", "OWNER_DIRECT", CHANNEL, CHAT_ID, event)

    draft = _stage_presented(cost=0.10)
    _maybe_approve_pending_draft("yes", "OWNER_DIRECT", CHANNEL, CHAT_ID, event)

    assert draft_store.is_approved(draft.draft_id) is True


def test_event_stuck_on_ambiguity_remains_usable_once_it_resolves():
    """event -> ambiguous candidates -> one disappears -> replay. Same
    non-spending principle as above, via the OTHER zero-authorization
    path (2+ eligible drafts, not zero). Companion to
    test_castle_walls_repaired_repair03_c8_closure.py's own version of
    this scenario, which uses two separate events instead of one replayed
    event -- both must work."""
    event = "telegram:42:5002"
    a = _stage_presented(cost=0.10)
    b = _stage_presented(cost=5.00)

    _maybe_approve_pending_draft("yes", "OWNER_DIRECT", CHANNEL, CHAT_ID, event)
    assert draft_store.is_approved(a.draft_id) is False
    assert draft_store.is_approved(b.draft_id) is False

    draft_store.discard_draft(b.draft_id)  # owner declines B via the button/discard path

    _maybe_approve_pending_draft("yes", "OWNER_DIRECT", CHANNEL, CHAT_ID, event)
    assert draft_store.is_approved(a.draft_id) is True


# ===========================================================================
# Restart semantics
# ===========================================================================

def test_replay_after_restart_fails_closed_when_ledger_survives_the_restart():
    """core.image_generation_draft's in-memory drafts are process-lifetime
    only by design -- a restart invalidates every outstanding draft. But
    the exactly-once ledger is disk-backed (core.idempotency.LEDGER_PATH),
    so an event already spent before a restart must stay spent after one,
    even against a brand-new draft that only exists post-restart."""
    event = "telegram:42:4001"
    a = _stage_presented(cost=0.10)
    _maybe_approve_pending_draft("yes", "OWNER_DIRECT", CHANNEL, CHAT_ID, event)
    assert draft_store.is_approved(a.draft_id) is True

    # Simulate the restart boundary: every in-memory draft/approval/
    # presentation record is gone, exactly like a real process restart --
    # but nothing here touches the ledger.db file the isolated_state
    # fixture pointed core.idempotency at, exactly like a real restart
    # never touches the real on-disk ledger.db either.
    draft_store._drafts.clear()
    draft_store._approvals.clear()
    draft_store._presented.clear()

    b = _stage_presented(cost=1.00)  # only exists post-"restart"
    _maybe_approve_pending_draft("yes", "OWNER_DIRECT", CHANNEL, CHAT_ID, event)

    assert draft_store.is_approved(b.draft_id) is False


# ===========================================================================
# Unaffected paths
# ===========================================================================

def test_exact_draft_button_approval_is_unaffected_by_the_event_ledger(monkeypatch):
    """The button path (approve_draft() called directly with a known
    draft_id, never through the word-match hook) needs no event identity
    at all and must keep working exactly as before."""
    submissions = []
    _arm_mock_submission(monkeypatch, submissions)
    intended = _stage_presented(cost=0.10)
    _stage_presented(cost=5.00)  # unrelated second pending draft

    approved = draft_store.approve_draft(intended.draft_id, channel_id=CHANNEL, chat_id=CHAT_ID)
    assert approved is True

    result = image_tool.generate_image(intended.draft_id, channel_id=CHANNEL, chat_id=CHAT_ID,
                                        current_turn_seq=1)
    assert "outcome: success" in result
    assert len(submissions) == 1


def test_ambiguous_two_plus_drafts_still_fails_closed_with_a_real_identity():
    """Repair-03's own ambiguity gate must still fire even when a fully
    valid, never-before-seen approval_event_id is supplied -- the event
    identity gate is additive, not a replacement for it."""
    a = _stage_presented(cost=0.10)
    b = _stage_presented(cost=5.00)

    _maybe_approve_pending_draft("yes", "OWNER_DIRECT", CHANNEL, CHAT_ID, "telegram:42:6001")

    assert draft_store.is_approved(a.draft_id) is False
    assert draft_store.is_approved(b.draft_id) is False
