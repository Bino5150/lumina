"""
core/image_generation_draft.py -- MEDIA-GENERATION-CONVERSATIONAL-RUNTIME-01

Structural stage/confirm gate between an owner's spend approval and a real
paid core.image_generation_service.generate_image() call. Mirrors
tools/pending_actions.py's stage/apply split -- the owner's "yes" is never
itself the authorization_ref; it only unlocks a confirm() call against a
specific, already-estimated, already-resolved operation this module staged
first. Chosen over a single "confirmed=True" tool argument specifically
because that would make the owner's approval nothing but the model's own
say-so -- see project memory's "peer-AI is not authorization" and "prompt-
level constraints are unreliable by measurement" findings. A structural
gate holds even when the prose around it doesn't.

A draft is:
  - single-use -- consume_draft() pops it out; a second confirm attempt
    with the same id always finds nothing, whether the first attempt
    succeeded, failed on drift, or failed for any other reason;
  - short-lived (TTL, default 10 minutes) -- long enough for an owner to
    read an estimate and reply, short enough that a draft from an
    abandoned conversation branch can never be resurrected much later;
  - bound to the exact operation it was estimated for (specialist, model,
    settings, cost_estimate, cost_unit, manifest_provider). The caller
    (tools/image_generation.py) re-resolves the route and re-estimates
    fresh at confirm time and must fail closed if either has drifted --
    this module only stores what was staged, it does not itself re-check
    anything against live config.

In-memory, module-level, process-lifetime only -- deliberately no disk
persistence. A restart invalidating every outstanding draft is the correct
default: a spend intent the owner never got to explicitly approve (or that
crossed a process restart) must never be resumable later.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from threading import Lock
from typing import Mapping, Optional

__all__ = [
    "ImageGenerationDraft",
    "DEFAULT_TTL_SECONDS",
    "stage_draft",
    "peek_draft",
    "consume_draft",
    "discard_draft",
    "mark_draft_presented",
    "is_presented",
    "approve_draft",
    "is_approved",
    "find_pending_draft_id",
    "try_claim_approval_event",
    "release_approval_event",
    "resolve_causal_draft_id",
]

DEFAULT_TTL_SECONDS = 600.0  # 10 minutes

# CASTLE-WALLS-REPAIR-04 / CANNON-11 -- namespace prefix for the durable
# exactly-once ledger entries below (core.idempotency.claim()/release()),
# so this event-identity space can never collide with any other
# core.idempotency caller's own request_ids (e.g. tools/telegram_send.py's
# send-dedup) even if the raw approval_event_id string were ever reused
# for an unrelated purpose. 24h matches telegram_origin_routing.
# ROUTE_TTL_SECONDS' own scale and core.idempotency.check()'s own default
# ttl_hours -- comfortably longer than any realistic transport-retry
# window, bounded so the ledger never grows without a lifecycle policy.
_APPROVAL_EVENT_NAMESPACE = "image_generation_draft_approval"
APPROVAL_EVENT_TTL_HOURS = 24.0

# CASTLE-WALLS-CLOSURE-01 -- separate namespace for the causal-intent-
# binding pin recorded by resolve_causal_draft_id() below. Deliberately a
# DIFFERENT idempotency request_id than the claim row above (same
# approval_event_id, different namespace): release_approval_event() only
# ever deletes the claim row, never this one, so the pin recorded at an
# event's first admission survives every later release+reclaim of that
# same event -- including across a process restart, since both live in
# core.idempotency's durable ledger. Same TTL as the claim itself: a pin
# has no reason to outlive the event identity it is pinned to.
_APPROVAL_PIN_NAMESPACE = "image_generation_draft_approval_pin"

# CASTLE-WALLS-CLOSURE-01 -- durable, best-effort "this draft_id was
# approved at some point" marker, keyed by draft_id (globally unique,
# uuid4-derived -- no collision risk across channel/chat/restart). Exists
# because consume_draft() pops _approvals for a spent draft_id, so the
# in-memory dict alone can't tell resolve_causal_draft_id() "a sibling in
# my pinned candidate set was separately approved and already spent" --
# only "was approved and NOT YET spent". Written by _record_ever_
# approved() below, never itself raised on failure (see that function's
# own docstring).
_DRAFT_EVER_APPROVED_NAMESPACE = "image_generation_draft_ever_approved"


@dataclass(frozen=True)
class ImageGenerationDraft:
    draft_id: str
    specialist: str
    model: str
    settings: Mapping[str, object]
    cost_estimate: Optional[float]
    cost_unit: Optional[str]
    manifest_provider: str
    channel_id: Optional[str]
    created_at: float
    expires_at: float
    # CASTLE-WALLS-REPAIR-01 R2 -- chat_id: the narrowest stable
    # conversation identity available (channel_id alone is fixed per
    # LuminaAgent instance -- one GUI session, every open chat -- so it
    # can't distinguish two different GUI chats; chat_id already can, see
    # core/agent.py's LuminaAgent._current_chat_id). staged_at_turn_seq:
    # ContextManager.turn_seq at staging time -- consumption requires a
    # STRICTLY LATER value, i.e. a genuinely new top-level turn since this
    # draft was staged, defeating same-turn confused-deputy chains. Both
    # default to None only for stage_draft()'s own backward-compatible
    # signature (frozen pre-repair evidence-file callers); no real
    # production caller ever omits them -- see
    # tools/image_generation.py's register_image_generation_tools().
    chat_id: Optional[int] = None
    staged_at_turn_seq: Optional[int] = None


_drafts: dict[str, ImageGenerationDraft] = {}
_approvals: dict[str, float] = {}  # draft_id -> approved_at epoch
_presented: dict[str, float] = {}  # draft_id -> owner-facing delivery epoch
_lock = Lock()


def _prune_expired_locked(now: float) -> None:
    expired = [draft_id for draft_id, d in _drafts.items() if d.expires_at <= now]
    for draft_id in expired:
        del _drafts[draft_id]
        _approvals.pop(draft_id, None)
        _presented.pop(draft_id, None)


def stage_draft(
    *,
    specialist: str,
    model: str,
    settings: Mapping[str, object],
    cost_estimate: Optional[float],
    cost_unit: Optional[str],
    manifest_provider: str,
    channel_id: Optional[str] = None,
    chat_id: Optional[int] = None,
    staged_at_turn_seq: Optional[int] = None,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
) -> ImageGenerationDraft:
    """Record a resolved, priced, not-yet-authorized operation. No spend,
    no submission -- purely bookkeeping for a later confirm_draft-style
    call. Every field is a plain value already computed by the caller;
    this module never resolves a route or fetches a price itself."""
    now = time.time()
    draft = ImageGenerationDraft(
        draft_id=uuid.uuid4().hex,
        specialist=specialist,
        model=model,
        settings=dict(settings),
        cost_estimate=cost_estimate,
        cost_unit=cost_unit,
        manifest_provider=manifest_provider,
        channel_id=channel_id,
        created_at=now,
        expires_at=now + ttl_seconds,
        chat_id=chat_id,
        staged_at_turn_seq=staged_at_turn_seq,
    )
    with _lock:
        _prune_expired_locked(now)
        _drafts[draft.draft_id] = draft
    return draft


def peek_draft(draft_id: str) -> Optional[ImageGenerationDraft]:
    """Read-only lookup -- does not consume. None if missing or expired."""
    with _lock:
        draft = _drafts.get(draft_id)
        if draft is None:
            return None
        if draft.expires_at <= time.time():
            del _drafts[draft_id]
            _approvals.pop(draft_id, None)
            _presented.pop(draft_id, None)
            return None
        return draft


def consume_draft(draft_id: str) -> Optional[ImageGenerationDraft]:
    """Single-use lookup: pops the draft out so it can never be reused,
    regardless of what the caller does with it afterward. None if missing,
    already consumed, or expired. Unchanged/unweakened by
    CASTLE-WALLS-REPAIR-01 -- the new approval/turn-boundary/channel
    checks live in tools/image_generation.py's generate_image(), checked
    via peek_draft() BEFORE this is ever reached, so a rejected attempt
    never burns the draft. This function's own two checks (exists,
    unexpired) remain the final, unconditional backstop."""
    with _lock:
        draft = _drafts.pop(draft_id, None)
        _approvals.pop(draft_id, None)
        _presented.pop(draft_id, None)
        if draft is None:
            return None
        if draft.expires_at <= time.time():
            return None
        return draft


def discard_draft(draft_id: str) -> None:
    """Explicitly abandon a staged draft (e.g. the owner declined)."""
    with _lock:
        _drafts.pop(draft_id, None)
        _approvals.pop(draft_id, None)
        _presented.pop(draft_id, None)


def mark_draft_presented(draft_id: str, *, channel_id: Optional[str],
                         chat_id: Optional[int]) -> bool:
    """Record a real owner-facing delivery event for this exact draft.

    CASTLE-WALLS-REPAIR-02 / CANNON-10: staging is not presentation. This
    primitive is not registered as a model tool. GUI/CLI code calls it only
    after updating the owner's real tool-result surface; headless transports
    call it only after their outbound send succeeds. Approval is impossible
    until this state exists, so an overlapping generic ``yes`` cannot approve
    an estimate that is still hidden inside another in-flight turn.
    """
    with _lock:
        now = time.time()
        _prune_expired_locked(now)
        draft = _drafts.get(draft_id)
        if draft is None:
            return False
        if draft.channel_id != channel_id or draft.chat_id != chat_id:
            return False
        _presented[draft_id] = now
        return True


def is_presented(draft_id: str) -> bool:
    """Read-only presentation-state observation for runtime/tests."""
    with _lock:
        return draft_id in _presented


def approve_draft(draft_id: str, *, channel_id: Optional[str], chat_id: Optional[int]) -> bool:
    """CASTLE-WALLS-REPAIR-01 R2, Gate 2 -- NOT registered as an agent
    tool, mirroring tools/pending_actions.py's _apply_action(): reachable
    only from non-model runtime code (the word-match turn-admission hook
    in core/agent.py, or a real GUI button click), never from a tool call
    the model itself can make. Records approval only if the draft exists,
    is unexpired, has actually been presented by trusted runtime code, and
    belongs to this EXACT (channel_id, chat_id)
    authorization context -- an approval typed/clicked in one chat can
    never authorize a draft staged in another. Returns whether approval
    was recorded.

    The _approvals mutation above remains the one and only in-memory
    state change, still the final statement under _lock exactly as
    before CASTLE-WALLS-CLOSURE-01 (see C10 S14 and core/agent.py's
    _maybe_approve_pending_draft finally-block comment for why that
    matters). _record_ever_approved() below runs AFTER the lock releases,
    as a clearly separate, best-effort durability step -- by the time it
    runs (or fails to), the real in-memory approval this function's
    return value is about has already unconditionally happened, so a
    ledger hiccup here can never make this function's own return value
    or the caller's is_approved()-based S14 guard lie."""
    with _lock:
        now = time.time()
        _prune_expired_locked(now)
        draft = _drafts.get(draft_id)
        if draft is None:
            return False
        if draft.channel_id != channel_id or draft.chat_id != chat_id:
            return False
        if draft_id not in _presented:
            return False
        _approvals[draft_id] = now
    _record_ever_approved(draft_id)
    return True


def is_approved(draft_id: str) -> bool:
    """Read-only -- does not prune or mutate. Consulted by
    tools/image_generation.py's generate_image() via peek_draft() first;
    this only answers the approval question in isolation."""
    with _lock:
        return draft_id in _approvals


def find_pending_draft_id(*, channel_id: Optional[str], chat_id: Optional[int]) -> Optional[str]:
    """The SOLE eligible (presented, unexpired, unapproved) draft for this
    exact (channel_id, chat_id) context, or None. Used only by the
    word-match turn-admission hook, which knows a context but not a
    specific draft_id -- an owner's plain "yes" approves whichever
    generation request is actually still pending in their own
    conversation, without the model needing to (or being able to) name
    the draft_id itself.

    CASTLE-WALLS-REPAIR-03 / C8 finding 1: a bare conversational
    affirmation can only ever resolve to a draft_id when the resolution
    is UNAMBIGUOUS. Zero eligible drafts -> None (nothing to approve).
    Exactly one -> that draft_id. Two or more -> None, ALWAYS -- fails
    closed rather than guessing by recency, price, or any other
    heuristic. Previously this picked "most recently staged" among ANY
    unapproved draft (not even requiring presentation), which let a bare
    "yes" silently approve a different, more expensive draft than the
    owner plausibly meant whenever two estimates were pending at once
    (see tests/test_castle_walls_adversarial_c8.py). "Presented" is now
    required here too -- an unpresented draft was never a candidate this
    function should have been offering in the first place; approve_draft()
    itself already refuses one regardless, this just stops it from ever
    being the one silently selected over a real, presented, eligible one.

    The exact-draft button path (approve_draft() called directly with a
    known draft_id) is entirely unaffected by this function and remains
    valid no matter how many drafts are pending."""
    with _lock:
        now = time.time()
        _prune_expired_locked(now)
        candidates = [
            d for d in _drafts.values()
            if d.channel_id == channel_id and d.chat_id == chat_id
            and d.draft_id not in _approvals
            and d.draft_id in _presented
        ]
        if len(candidates) != 1:
            return None
        return candidates[0].draft_id


def _record_ever_approved(draft_id: str) -> None:
    """CASTLE-WALLS-CLOSURE-01 -- durable, best-effort marker that
    draft_id was approved at some point, independent of whether it is
    later consumed (consume_draft() clears _approvals/_presented for a
    spent draft_id -- this durable record is the only trace left once
    that happens). Consulted by resolve_causal_draft_id()'s ambiguity-
    poisoning check. Never raises: this is a secondary signal for a
    later replay's ambiguity check, never itself the approval -- the
    real, load-bearing in-memory approval already unconditionally
    happened in approve_draft() before this is ever called, and a ledger
    hiccup here must not surface as a broken button click or a broken
    chat turn."""
    try:
        from core import idempotency
        request_id = idempotency.make_request_id(_DRAFT_EVER_APPROVED_NAMESPACE, draft_id)
        idempotency.record(request_id, "approved")
    except Exception:
        pass


def _any_ever_approved(draft_ids) -> bool:
    from core import idempotency
    for draft_id in draft_ids:
        request_id = idempotency.make_request_id(_DRAFT_EVER_APPROVED_NAMESPACE, draft_id)
        if idempotency.check(request_id, ttl_hours=APPROVAL_EVENT_TTL_HOURS) is not None:
            return True
    return False


def resolve_causal_draft_id(
    *,
    channel_id: Optional[str],
    chat_id: Optional[int],
    approval_event_id: str,
    current_turn_seq: Optional[int],
) -> Optional[str]:
    """CASTLE-WALLS-CLOSURE-01 -- causal intent binding on top of
    find_pending_draft_id()'s existing (channel_id, chat_id)/presented/
    unambiguous resolution. Closes the C10 S5/S6/S7-dir2 residual: an
    owner authorization event may resolve, via the implicit word-match
    path, ONLY to an operation that already causally existed at the
    event's FIRST admission -- never to a draft staged afterward. The
    exact-draft approve_draft() button path is untouched by any of this
    and remains valid regardless.

    current_turn_seq is the caller's ctx.turn_seq at the moment of THIS
    admission (core/agent.py's _maybe_approve_pending_draft, called
    immediately after the real ctx.add_user() turn admission -- so it is
    always strictly greater than any draft.staged_at_turn_seq a model
    tool call could have recorded before this turn even began). None
    (no turn-sequence context supplied) falls back to
    find_pending_draft_id()'s pre-closure resolution unchanged -- the
    one production call site always supplies a real value; only a
    caller with no ctx to read from (frozen pre-closure test evidence)
    omits it.

    THE PIN (recorded exactly once per approval_event_id, in
    core.idempotency's durable ledger under _APPROVAL_PIN_NAMESPACE, so
    it survives try_claim_approval_event()/release_approval_event()
    cycles AND a process restart): at first resolution for a given
    event, freeze the SET of every draft_id in this exact (channel_id,
    chat_id) context with staged_at_turn_seq <= current_turn_seq -- i.e.
    every operation that causally existed no later than the turn this
    owner event was admitted on AND was not already approved at that
    exact moment (an already-approved draft is already spoken for by
    whatever approved it -- never a real candidate for a DIFFERENT,
    brand-new event, exactly like find_pending_draft_id()'s own
    pre-closure candidate filter), regardless of whether it had been
    marked presented yet (a presentation-timing race must not burn a
    pre-existing operation's legitimate authority). This set can never
    grow or change identity afterward; a draft staged later is causally
    new and can never join it, no matter how many times this event is
    later reclaimed.

    Every subsequent resolution for the same approval_event_id (this
    admission or any later reclaim) re-evaluates the frozen set against
    CURRENT state instead of recomputing candidates:

    - if ANY member of the set was EVER approved (_any_ever_approved,
      durable -- catches a sibling approved via an unrelated event or
      the exact-draft button, whether or not it has since been spent),
      this event is permanently poisoned and resolves to nothing, ever.
      A genuine ambiguity the owner's own single affirmation never
      actually disambiguated must not be laundered into an approval of
      whichever sibling merely happens to remain once a DIFFERENT
      authorization already spent one of them (C10 S6).
    - otherwise, of the members still live (still in _drafts, still
      presented -- simple discard or TTL expiry drops a member from
      this list without poisoning anything, exactly like the pre-
      closure ambiguity-then-attrition behavior this preserves),
      exactly one remaining -> resolve to it; zero or two-or-more ->
      not yet resolvable this call, but still eligible on a later
      reclaim (e.g. the presentation-timing race, or a sibling still
      pending discard)."""
    if current_turn_seq is None:
        return find_pending_draft_id(channel_id=channel_id, chat_id=chat_id)

    from core import idempotency
    pin_request_id = idempotency.make_request_id(_APPROVAL_PIN_NAMESPACE, approval_event_id)
    cached = idempotency.check(pin_request_id, ttl_hours=APPROVAL_EVENT_TTL_HOURS)
    if cached is None:
        with _lock:
            now = time.time()
            _prune_expired_locked(now)
            pinned_ids = [
                d.draft_id for d in _drafts.values()
                if d.channel_id == channel_id and d.chat_id == chat_id
                and d.staged_at_turn_seq is not None
                and d.staged_at_turn_seq <= current_turn_seq
                and d.draft_id not in _approvals
            ]
        idempotency.record(pin_request_id, json.dumps({"draft_ids": pinned_ids}))
    else:
        pinned_ids = json.loads(cached).get("draft_ids", [])

    if not pinned_ids:
        return None
    if _any_ever_approved(pinned_ids):
        return None

    with _lock:
        now = time.time()
        _prune_expired_locked(now)
        remaining = [d for d in pinned_ids if d in _drafts and d in _presented]
    if len(remaining) != 1:
        return None
    return remaining[0]


def try_claim_approval_event(approval_event_id: str) -> bool:
    """CASTLE-WALLS-REPAIR-04 / CANNON-11 -- exactly-once admission gate for
    a unique owner authorization event (see core.agent._maybe_approve_
    pending_draft(), the word-match turn-admission hook that calls this
    before ever resolving a candidate draft). Durable across threads,
    processes, and restarts -- backed by core.idempotency's own SQLite
    ledger.db, not an in-memory set, because the same authenticated
    external event (e.g. a Telegram update redelivered by transport-level
    retry) can legitimately reach this process again after either.

    Returns True for exactly the one admission that wins the claim across
    every replay of the same approval_event_id within
    APPROVAL_EVENT_TTL_HOURS; False for every other one. Callers must
    release_approval_event() the claim if it turns out not to correspond
    to a real approval -- see that function's own docstring."""
    from core import idempotency
    request_id = idempotency.make_request_id(_APPROVAL_EVENT_NAMESPACE, approval_event_id)
    return idempotency.claim(request_id, ttl_hours=APPROVAL_EVENT_TTL_HOURS)


def release_approval_event(approval_event_id: str) -> None:
    """Undo try_claim_approval_event() when the claimed event turned out
    not to correspond to a real approval -- no eligible draft was found,
    or approve_draft() itself declined (e.g. the draft expired in the
    interim). An event that authorized nothing must remain available to a
    genuinely later admission rather than becoming a permanently spent
    bearer token for nothing (CASTLE-WALLS-REPAIR-04, section 9's closing
    invariant: an event that failed to authorize must never become a
    stored bearer token for unrelated future work)."""
    from core import idempotency
    request_id = idempotency.make_request_id(_APPROVAL_EVENT_NAMESPACE, approval_event_id)
    idempotency.release(request_id)
