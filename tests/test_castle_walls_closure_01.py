"""CASTLE-WALLS-CLOSURE-01 -- causal intent binding + C10 S14 invariant.

Closure hardening on top of CASTLE-WALLS-REPAIR-04 (tests/test_castle_
walls_repaired_cannon11.py), not a new adversarial campaign. C10
(project-evidence/campaign-reports/LUMINA_CASTLE_WALLS_C10_INDEPENDENT_
REATTACK_2026-09-20.md) passed the exactly-once invariant and flagged two
residual-by-design intent-binding gaps (S5/S6/S7-dir2) plus one
structural note (S14). This file proves both are closed:

  - resolve_causal_draft_id() (core/image_generation_draft.py): an owner
    authorization event may only ever resolve, via the implicit
    word-match path, to a draft that causally existed (by ctx.turn_seq)
    at the event's FIRST admission -- never to one staged afterward
    (S5/S7-dir2), and never to a sibling from a genuinely ambiguous set
    merely because every OTHER sibling was separately approved elsewhere
    (S6). A sibling that is discarded/expires (not approved) does NOT
    poison the set -- that is the pre-existing, intentionally-preserved
    "ambiguity resolves once it narrows" behavior (see
    test_castle_walls_repaired_cannon11.py's own
    test_event_stuck_on_ambiguity_remains_usable_once_it_resolves,
    unmodified and still green).
  - the _maybe_approve_pending_draft() finally-block guard (core/agent.py):
    releases an event's claim only if the draft store's OWN state agrees
    nothing was approved, not merely if no exception occurred (S14).

Synthetic state only. No network, no real Telegram, no spend (mock
provider submission where exercised)."""
import json
import threading
import time
from types import SimpleNamespace

import pytest

import core.idempotency as idempotency
import core.image_generation_draft as draft_store
from core.agent import _maybe_approve_pending_draft

SPECIALIST = "higgsfield"
MODEL = "higgsfield-ai/soul/standard"
CHANNEL = "telegram-owner"
CHAT_ID = None


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    # Same isolation as test_castle_walls_repaired_cannon11.py's own
    # fixture -- the exactly-once/pin/ever-approved ledgers are all
    # disk-backed by design (core.idempotency), so tests would otherwise
    # see each other's claimed events, pins, and approval history.
    monkeypatch.setattr(idempotency, "LEDGER_PATH", str(tmp_path / "ledger.db"))
    draft_store._drafts.clear()
    draft_store._approvals.clear()
    draft_store._presented.clear()
    yield
    draft_store._drafts.clear()
    draft_store._approvals.clear()
    draft_store._presented.clear()


def _stage(*, channel_id=CHANNEL, chat_id=CHAT_ID, cost=0.10, prompt="x", turn_seq):
    return draft_store.stage_draft(
        specialist=SPECIALIST, model=MODEL, settings={"prompt": prompt},
        cost_estimate=cost, cost_unit="usd", manifest_provider="higgsfield",
        channel_id=channel_id, chat_id=chat_id, staged_at_turn_seq=turn_seq,
    )


def _stage_presented(*, channel_id=CHANNEL, chat_id=CHAT_ID, cost=0.10, prompt="x", turn_seq):
    draft = _stage(channel_id=channel_id, chat_id=chat_id, cost=cost, prompt=prompt,
                    turn_seq=turn_seq)
    assert draft_store.mark_draft_presented(draft.draft_id, channel_id=channel_id, chat_id=chat_id)
    return draft


# ===========================================================================
# Gap 1 (C10 S5/S7-dir2) -- a released event must not authorize a draft
# staged AFTER its first admission
# ===========================================================================

def test_old_event_cannot_approve_draft_created_after_first_admission():
    """owner event -> no candidate -> NEW draft staged -> replay old event
    must not authorize the new draft. Companion to (and, for the causal
    path, supersedes) test_castle_walls_repaired_cannon11.py's own
    test_event_with_no_candidate_remains_usable_once_one_appears, which
    exercises the pre-closure fallback path (no current_turn_seq) and is
    intentionally left unmodified -- both must keep passing."""
    event = "telegram:closure:1"
    _maybe_approve_pending_draft("yes", "OWNER_DIRECT", CHANNEL, CHAT_ID, event,
                                  current_turn_seq=1)

    new_draft = _stage_presented(cost=9.99, turn_seq=5)  # staged AFTER admission
    _maybe_approve_pending_draft("yes", "OWNER_DIRECT", CHANNEL, CHAT_ID, event,
                                  current_turn_seq=6)  # replay, later turn

    assert draft_store.is_approved(new_draft.draft_id) is False


def test_restart_crossing_variant_of_gap_1_stays_closed():
    """The same Gap 1 chain, but the release and the new draft are
    separated by a simulated process restart (C10 S7-dir2): drafts are
    process-lifetime-only by design and vanish, but the pin recorded at
    first admission is durable (core.idempotency) and must survive too --
    a post-restart draft always postdates a pre-restart event."""
    event = "telegram:closure:1b"
    _maybe_approve_pending_draft("yes", "OWNER_DIRECT", CHANNEL, CHAT_ID, event,
                                  current_turn_seq=1)

    # Simulate the restart boundary: every in-memory draft/approval/
    # presentation record is gone, exactly like test_castle_walls_
    # repaired_cannon11.py's own restart test -- but nothing here touches
    # the ledger.db file the isolated_state fixture pointed
    # core.idempotency at, exactly like a real restart never touches the
    # real on-disk ledger.db either.
    draft_store._drafts.clear()
    draft_store._approvals.clear()
    draft_store._presented.clear()

    post_restart_draft = _stage_presented(cost=9.99, turn_seq=1)  # only exists post-"restart"
    _maybe_approve_pending_draft("yes", "OWNER_DIRECT", CHANNEL, CHAT_ID, event,
                                  current_turn_seq=1)

    assert draft_store.is_approved(post_restart_draft.draft_id) is False


# ===========================================================================
# Gap 2 (C10 S6) -- ambiguity laundering: a sibling separately approved
# must permanently poison the event for every OTHER sibling
# ===========================================================================

def test_ambiguity_then_button_approval_of_sibling_blocks_replay_from_taking_the_other():
    """owner event -> D1/D2 ambiguous -> button approves D1 -> replay old
    event must not authorize D2, even though D2 is now the sole
    remaining live candidate."""
    event = "telegram:closure:2"
    d1 = _stage_presented(cost=0.10, prompt="D1", turn_seq=1)
    d2 = _stage_presented(cost=5.00, prompt="D2", turn_seq=1)

    _maybe_approve_pending_draft("yes", "OWNER_DIRECT", CHANNEL, CHAT_ID, event,
                                  current_turn_seq=2)
    assert draft_store.is_approved(d1.draft_id) is False
    assert draft_store.is_approved(d2.draft_id) is False

    approved = draft_store.approve_draft(d1.draft_id, channel_id=CHANNEL, chat_id=CHAT_ID)
    assert approved is True

    _maybe_approve_pending_draft("yes", "OWNER_DIRECT", CHANNEL, CHAT_ID, event,
                                  current_turn_seq=3)
    assert draft_store.is_approved(d2.draft_id) is False


def test_ambiguity_then_approve_and_consume_of_sibling_blocks_replay_from_taking_the_other():
    """Realistic variant of the above: D1 is not just approved but
    immediately consumed (the real generation submission), popping it out
    of _approvals entirely. The durable ever-approved marker, not the
    in-memory _approvals dict, is what must still poison the event."""
    event = "telegram:closure:2b"
    d1 = _stage_presented(cost=0.10, prompt="D1", turn_seq=1)
    d2 = _stage_presented(cost=5.00, prompt="D2", turn_seq=1)

    _maybe_approve_pending_draft("yes", "OWNER_DIRECT", CHANNEL, CHAT_ID, event,
                                  current_turn_seq=2)

    assert draft_store.approve_draft(d1.draft_id, channel_id=CHANNEL, chat_id=CHAT_ID) is True
    consumed = draft_store.consume_draft(d1.draft_id)
    assert consumed is not None
    assert d1.draft_id not in draft_store._approvals  # confirm it's really gone from the in-memory dict

    _maybe_approve_pending_draft("yes", "OWNER_DIRECT", CHANNEL, CHAT_ID, event,
                                  current_turn_seq=3)
    assert draft_store.is_approved(d2.draft_id) is False


def test_ambiguity_then_discard_of_sibling_still_resolves_the_remaining_one():
    """Contrast case, causal path: discarding (not approving) a sibling
    does NOT poison the event -- the remaining candidate stays
    resolvable, same intentional behavior as test_castle_walls_repaired_
    cannon11.py's test_event_stuck_on_ambiguity_remains_usable_once_it_
    resolves (which exercises the pre-closure fallback path). Proves the
    S6 fix distinguishes "a different authorization already spent a
    sibling" from "a sibling was merely removed from consideration"."""
    event = "telegram:closure:2c"
    a = _stage_presented(cost=0.10, prompt="A", turn_seq=1)
    b = _stage_presented(cost=5.00, prompt="B", turn_seq=1)

    _maybe_approve_pending_draft("yes", "OWNER_DIRECT", CHANNEL, CHAT_ID, event,
                                  current_turn_seq=2)
    assert draft_store.is_approved(a.draft_id) is False

    draft_store.discard_draft(b.draft_id)  # owner declines B -- not an approval of anything

    _maybe_approve_pending_draft("yes", "OWNER_DIRECT", CHANNEL, CHAT_ID, event,
                                  current_turn_seq=3)
    assert draft_store.is_approved(a.draft_id) is True


# ===========================================================================
# Legitimate race preserved -- a pre-existing draft merely presented late
# ===========================================================================

def test_pre_existing_draft_presentation_race_retains_usability():
    """A draft staged BEFORE the event's first admission, but not yet
    marked presented at that exact moment (an internal delivery-ordering
    race between the model's tool call and the GUI/headless transport's
    own presentation update), must remain approvable once presentation
    catches up -- this is the legitimate race the task explicitly
    requires preserving, distinct from a genuinely new draft staged
    after admission."""
    event = "telegram:closure:3"
    draft = _stage(cost=0.10, turn_seq=1)  # exists, but NOT yet presented
    assert draft_store.is_presented(draft.draft_id) is False

    _maybe_approve_pending_draft("yes", "OWNER_DIRECT", CHANNEL, CHAT_ID, event,
                                  current_turn_seq=2)
    assert draft_store.is_approved(draft.draft_id) is False  # not yet -- unpresented

    # Presentation catches up (e.g. the GUI/headless transport's own
    # outbound send/update finally lands).
    assert draft_store.mark_draft_presented(draft.draft_id, channel_id=CHANNEL, chat_id=CHAT_ID)

    _maybe_approve_pending_draft("yes", "OWNER_DIRECT", CHANNEL, CHAT_ID, event,
                                  current_turn_seq=2)
    assert draft_store.is_approved(draft.draft_id) is True


# ===========================================================================
# A genuinely new owner event is unaffected
# ===========================================================================

def test_genuinely_new_owner_event_can_approve_a_later_draft():
    event1 = "telegram:closure:4a"
    first = _stage_presented(cost=0.10, turn_seq=1)
    _maybe_approve_pending_draft("yes", "OWNER_DIRECT", CHANNEL, CHAT_ID, event1,
                                  current_turn_seq=2)
    assert draft_store.is_approved(first.draft_id) is True
    draft_store.consume_draft(first.draft_id)

    event2 = "telegram:closure:4b"
    later = _stage_presented(cost=1.00, turn_seq=9)
    _maybe_approve_pending_draft("yes", "OWNER_DIRECT", CHANNEL, CHAT_ID, event2,
                                  current_turn_seq=10)
    assert draft_store.is_approved(later.draft_id) is True


# ===========================================================================
# Concurrency -- unaffected by the causal-binding addition
# ===========================================================================

def test_same_event_concurrent_replay_remains_exactly_once():
    event = "telegram:closure:5"
    draft = _stage_presented(cost=0.10, turn_seq=1)

    barrier = threading.Barrier(16)

    def _admit():
        barrier.wait(timeout=5)
        _maybe_approve_pending_draft("yes", "OWNER_DIRECT", CHANNEL, CHAT_ID, event,
                                      current_turn_seq=2)

    threads = [threading.Thread(target=_admit) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert draft_store.is_approved(draft.draft_id) is True
    assert len(draft_store._approvals) == 1


# ===========================================================================
# Distinctness under the causal path
# ===========================================================================

def test_distinct_identical_text_events_remain_distinct_under_causal_path():
    import uuid
    a = _stage_presented(cost=0.10, turn_seq=1)
    _maybe_approve_pending_draft("yes", "OWNER_DIRECT", CHANNEL, CHAT_ID, uuid.uuid4().hex,
                                  current_turn_seq=2)
    assert draft_store.is_approved(a.draft_id) is True

    b = _stage_presented(cost=5.00, turn_seq=3)
    _maybe_approve_pending_draft("yes", "OWNER_DIRECT", CHANNEL, CHAT_ID, uuid.uuid4().hex,
                                  current_turn_seq=4)
    assert draft_store.is_approved(b.draft_id) is True


def test_ambiguity_fail_closed_still_holds_under_causal_path():
    """Repair-03's ambiguity gate must still fire on first admission
    through the NEW causal path, not just the pre-closure fallback."""
    a = _stage_presented(cost=0.10, turn_seq=1)
    b = _stage_presented(cost=5.00, turn_seq=1)
    _maybe_approve_pending_draft("yes", "OWNER_DIRECT", CHANNEL, CHAT_ID, "telegram:closure:6",
                                  current_turn_seq=2)
    assert draft_store.is_approved(a.draft_id) is False
    assert draft_store.is_approved(b.draft_id) is False


# ===========================================================================
# C10 S14 -- approve_draft mutate-then-fail must never create reusable
# authority
# ===========================================================================

def test_s14_mutate_then_raise_inside_approve_draft_cannot_create_reusable_authority(monkeypatch):
    """Fault injection matching C10 S14's control-flow probe exactly: a
    hypothetical future approve_draft() that mutates _approvals and THEN
    raises. Pre-closure, the hook's `finally: if not approved: release`
    would trust the local `approved` flag (never set, because the
    exception fires before the assignment completes) and release the
    event's claim -- freeing an event whose approval already stands, so
    a replay could burn a SECOND draft under the same event. Proves that
    can no longer happen: the finally-block guard checks the draft
    store's OWN state (is_approved), not the local flag, so the claim
    stays spent and the event is provably not reusable."""
    import core.image_generation_draft as draft_module

    event = "telegram:closure:s14"
    draft = _stage_presented(cost=0.10, turn_seq=1)

    real_approve_draft = draft_module.approve_draft

    def _mutate_then_raise(draft_id, *, channel_id, chat_id):
        # Reproduce the exact hazard: perform the real mutation, THEN
        # raise, simulating a future refactor that added a step after
        # approve_draft()'s current final statement.
        with draft_module._lock:
            draft_module._approvals[draft_id] = time.time()
        raise RuntimeError("simulated post-mutation failure")

    monkeypatch.setattr(draft_module, "approve_draft", _mutate_then_raise)

    _maybe_approve_pending_draft("yes", "OWNER_DIRECT", CHANNEL, CHAT_ID, event,
                                  current_turn_seq=2)

    # The mutation stuck...
    assert draft_store.is_approved(draft.draft_id) is True
    # ...and, load-bearing: the claim was NOT released, so the same event
    # can never be re-admitted -- it is not a reusable bearer token for a
    # second draft.
    monkeypatch.setattr(draft_module, "approve_draft", real_approve_draft)
    assert draft_store.try_claim_approval_event(event) is False


def test_s14_ordinary_failure_before_any_mutation_still_releases_normally(monkeypatch):
    """Contrast case: a failure that happens BEFORE any mutation (the
    ordinary, already-covered case -- e.g. resolve_causal_draft_id itself
    raising) must still release the claim exactly as before, so a
    genuinely later admission of the same event remains usable. The S14
    guard must not turn into an unconditional never-release."""
    import core.image_generation_draft as draft_module

    event = "telegram:closure:s14b"

    def _raise_before_mutation(*, channel_id, chat_id, approval_event_id, current_turn_seq):
        raise RuntimeError("simulated pre-mutation failure")

    monkeypatch.setattr(draft_module, "resolve_causal_draft_id", _raise_before_mutation)
    _maybe_approve_pending_draft("yes", "OWNER_DIRECT", CHANNEL, CHAT_ID, event,
                                  current_turn_seq=1)
    monkeypatch.undo()

    assert draft_store.try_claim_approval_event(event) is True  # claim was released, so this re-claims cleanly
