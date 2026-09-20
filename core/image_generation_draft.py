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
    "approve_draft",
    "is_approved",
    "find_pending_draft_id",
]

DEFAULT_TTL_SECONDS = 600.0  # 10 minutes


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
_lock = Lock()


def _prune_expired_locked(now: float) -> None:
    expired = [draft_id for draft_id, d in _drafts.items() if d.expires_at <= now]
    for draft_id in expired:
        del _drafts[draft_id]
        _approvals.pop(draft_id, None)


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


def approve_draft(draft_id: str, *, channel_id: Optional[str], chat_id: Optional[int]) -> bool:
    """CASTLE-WALLS-REPAIR-01 R2, Gate 2 -- NOT registered as an agent
    tool, mirroring tools/pending_actions.py's _apply_action(): reachable
    only from non-model runtime code (the word-match turn-admission hook
    in core/agent.py, or a real GUI button click), never from a tool call
    the model itself can make. Records approval only if the draft exists,
    is unexpired, and belongs to this EXACT (channel_id, chat_id)
    authorization context -- an approval typed/clicked in one chat can
    never authorize a draft staged in another. Returns whether approval
    was recorded."""
    with _lock:
        now = time.time()
        _prune_expired_locked(now)
        draft = _drafts.get(draft_id)
        if draft is None:
            return False
        if draft.channel_id != channel_id or draft.chat_id != chat_id:
            return False
        _approvals[draft_id] = now
        return True


def is_approved(draft_id: str) -> bool:
    """Read-only -- does not prune or mutate. Consulted by
    tools/image_generation.py's generate_image() via peek_draft() first;
    this only answers the approval question in isolation."""
    with _lock:
        return draft_id in _approvals


def find_pending_draft_id(*, channel_id: Optional[str], chat_id: Optional[int]) -> Optional[str]:
    """Most recently staged, unexpired, not-yet-approved draft for this
    exact (channel_id, chat_id) context, or None. Used only by the
    word-match turn-admission hook, which knows a context but not a
    specific draft_id -- an owner's plain "yes" approves whichever
    generation request is actually still pending in their own
    conversation, without the model needing to (or being able to) name
    the draft_id itself."""
    with _lock:
        now = time.time()
        _prune_expired_locked(now)
        candidates = [
            d for d in _drafts.values()
            if d.channel_id == channel_id and d.chat_id == chat_id
            and d.draft_id not in _approvals
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda d: d.created_at).draft_id
