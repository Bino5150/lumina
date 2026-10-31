"""TURN-RECOVERY-UI-01 (LUMINA-PULSE-01, P0) -- native "Continue interrupted turn".

A foreground owner turn whose provider request was lost to a transport
failure (ConnectionError/TimeoutError -- the TLS storms in chat #166) can be
continued by an explicit owner click instead of a typed "Continue". This
module is the Qt-free kernel: what makes a turn resumable, the fences a
claim must pass, and the state carried into the continued attempt. It never
dispatches anything itself; core/agent.py runs the continued attempt and
ui/main_window.py owns the button.

Design law (Design R1 + owner GO, 2026-10-30):
  * A continuation re-sends the provider request over the turn's unchanged
    in-memory history. It never re-runs a tool: every tool call of the turn
    must already be closed by exactly one tool row (tool_history_closed()).
  * A consequential call (tier != read_only) counts as clean only on
    POSITIVE evidence: a verified Chrome action receipt, or result framing
    that tool itself generates on its completed path (_KNOWN_OUTCOME_SHAPES).
    Every other consequential result -- an unlisted tool, an unrecognised
    shape, any error, timeout or JSON status -- is "unknown" and makes the
    turn non-resumable (owner requirement: unknown outcomes fail closed).
  * Only ConnectionError/TimeoutError qualify. Any received HTTP response
    (429/502/401/...), refusal, intentional stop, cancellation or E-stop
    does not.
  * Authority is the intersection of the original turn's envelope and what
    is valid now: same chat, same backend/model, unchanged durable and live
    context, no newer admission, original suspension token still valid,
    E-stop neither latched nor re-armed since, the enabled tool set no wider
    and the active project unchanged. Any miss is terminal.
  * Owner authority behind unchanged tool names (AUTHORITY_PROBES: this
    chat's approved image drafts, the Telegram remote-access switch) is
    captured when the turn fails and may only narrow: refused at claim, at
    turn start and before every consequential dispatch if it widened since.
    A later re-offer is captured live and may only narrow again. Revocations
    need no fence -- the tools read them live.
  * At most MAX_ATTEMPTS owner-initiated continuations per root turn, with
    the tool-call/time budgets and no-progress state carried, never reset.
  * The record lives in memory only: a restarted session has nothing to
    resume.
  * Exact-argument duplicate suppression (is_recovery_duplicate()) is
    defense-in-depth for the continued attempt, not proof of idempotency.
"""
from __future__ import annotations

import hashlib
import json
import threading
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

MAX_ATTEMPTS = 3

FAILURE_PROVIDER_TRANSPORT = "provider_transport"

# Refusal / not-offered reasons -- fixed codes, never free text from a
# provider, a tool or the model.
NOT_TRANSPORT = "not_transport_failure"
NOT_THIS_FAILURE = "result_is_not_the_interruption"
NOT_OWNER_DESKTOP = "not_owner_desktop_turn"
ATTEMPTS_EXHAUSTED = "attempts_exhausted"
TOOL_HISTORY_OPEN = "tool_history_not_closed"
UNKNOWN_SIDE_EFFECT = "consequential_outcome_unknown"
ANCHOR_MISSING = "turn_anchor_missing"
NO_RECORD = "nothing_to_continue"
WRONG_OFFER = "offer_superseded"
CHAT_CHANGED = "chat_changed"
CONTEXT_CHANGED = "context_changed"
ADMISSION_CHANGED = "newer_turn_admitted"
BACKEND_CHANGED = "backend_or_model_changed"
ESTOP_LATCHED = "emergency_stop_latched"
ESTOP_EPOCH_CHANGED = "emergency_stop_rearmed_since"
SUSPENSION_REFUSED = "suspension_refused"
SCOPE_WIDENED = "tool_or_project_scope_changed"
AUTHORITY_WIDENED = "authority_widened_since_interruption"
AUTHORITY_UNREADABLE = "authority_state_unreadable"

REASON_TEXT = {
    NO_RECORD: "there is nothing left to continue",
    WRONG_OFFER: "this offer was replaced by a newer one",
    CHAT_CHANGED: "a different conversation is open",
    CONTEXT_CHANGED: "the conversation changed since the interruption",
    ADMISSION_CHANGED: "another turn started since the interruption",
    BACKEND_CHANGED: "the provider or model changed since the interruption",
    ESTOP_LATCHED: "Emergency Stop is active",
    ESTOP_EPOCH_CHANGED: "Emergency Stop was used since the interruption",
    SUSPENSION_REFUSED: "runaway suspension no longer admits this work",
    SCOPE_WIDENED: "enabled tools or the active project changed since the interruption",
    AUTHORITY_WIDENED: "an approval or permission was granted since the interruption",
    AUTHORITY_UNREADABLE: "the approvals and permissions in force could not be read",
    ATTEMPTS_EXHAUSTED: f"it was already continued {MAX_ATTEMPTS} times",
    TOOL_HISTORY_OPEN: "a tool call from this turn is not closed",
    UNKNOWN_SIDE_EFFECT: "a tool action earlier in this turn has an uncertain outcome",
}

READ_ONLY = "read_only"

# Positive evidence of a completed, known outcome, per tool: the framing that
# tool itself writes on that path (never text a command, file or model can
# put first). run_command: [stdout]/[stderr]/[exit code: N] mean the command
# ran to completion (any exit status is a known outcome); [BLOCKED: ...] means
# it never ran. Every error/timeout path of these tools starts differently and
# so stays "unknown". A tool or wording not listed here is "unknown".
_KNOWN_OUTCOME_SHAPES = {
    "run_command": ("[stdout]\n", "[stderr]\n", "[exit code: ", "[BLOCKED: "),
    "edit_file": ("[Edited: ",),
    "write_file": ("[Written: ",),
    "save_memory": ("Memory saved [",),
    "save_knowledge": ("Knowledge saved to '",),
    "palace_remember": ("Stored in ",),
    "palace_hall": ("Hall entry stored: ",),
}
_KNOWN_OUTCOME_EXACT = {"run_command": frozenset({"[No output]"})}

RECOVERY_AUTHORITY_NOTICE = (
    "[Not run: the tools, project, approvals or permissions in force widened since this "
    "turn was interrupted. Continuing an interrupted turn never uses authority granted "
    "after the interruption; the owner can ask for it again in a new message.]"
)

RECOVERY_UNREADABLE_NOTICE = (
    "[Not run: the tools, project, approvals or permissions in force could not be read, "
    "so whether this action is still allowed could not be confirmed. Continuing an "
    "interrupted turn never acts on unconfirmed authority; the owner can ask for it again "
    "in a new message.]"
)


def refused_notice(reason: str) -> str:
    """The closed tool row for a call the authority fence refused, worded
    for the reason: unreadable state is not reported as a widening."""
    return RECOVERY_UNREADABLE_NOTICE if reason == AUTHORITY_UNREADABLE else RECOVERY_AUTHORITY_NOTICE


RECOVERY_DUPLICATE_NOTICE = (
    "[Not run: identical to a tool call already executed in this turn before it "
    "was interrupted -- its result is above. Continuing an interrupted turn never "
    "repeats an action; the owner can ask for it again in a new message.]"
)


def is_transport_failure(exc) -> bool:
    """True only for the builtin transport/timeout classes the backends raise
    when no provider response was received. A RuntimeError (an HTTP response
    WAS received -- 429/502/401/400 ...) is never transport."""
    return isinstance(exc, (ConnectionError, TimeoutError))


@dataclass(frozen=True)
class ProviderInterruption:
    """What _provider_chat_or_error()/_stream_final() observed. error_text
    is the exact sentinel they returned, so chat() can prove the turn's
    result IS this failure and not a later stop notice."""
    phase: str
    stage: str
    exception_type: str
    cause_type: Optional[str]
    transport: bool
    error_text: str
    partial_final_chars: int = 0


@dataclass(frozen=True)
class ExecutedCall:
    name: str
    tier: str
    args_hash: str
    outcome: str  # "clean" | "unknown" | "skipped"


def call_outcome(name: str, tier: str, result, loop_success: bool,
                 receipt_verified: bool = False) -> str:
    """Outcome of one executed call for recovery purposes. read_only calls
    are always "clean" (no side effect to be uncertain about). A
    consequential call is "clean" only on positive evidence: a verified
    Chrome action receipt, or that tool's own completed-path framing."""
    if tier == READ_ONLY:
        return "clean"
    if not loop_success:
        return "unknown"
    if receipt_verified:
        return "clean"
    if not isinstance(result, str):
        return "unknown"
    if result in _KNOWN_OUTCOME_EXACT.get(name, ()):
        return "clean"
    shapes = _KNOWN_OUTCOME_SHAPES.get(name)
    if shapes and result.startswith(shapes):
        return "clean"
    return "unknown"


def args_hash(args) -> str:
    try:
        blob = json.dumps(args, sort_keys=True, default=str)
    except Exception:
        blob = str(args)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def tool_history_closed(messages) -> bool:
    """Every tool_calls[].id has exactly one following role:"tool" row before
    the next non-tool message; no orphan, duplicate or id-less row. Ids must
    be unique within one assistant message; a provider may reuse them in a
    later round, since pairing is per batch."""
    pending = set()
    for message in messages:
        if not isinstance(message, dict):
            return False
        role = message.get("role")
        if role == "tool":
            tool_id = message.get("tool_call_id")
            if tool_id not in pending:
                return False
            pending.discard(tool_id)
            continue
        if pending:
            return False
        if role == "assistant" and message.get("tool_calls"):
            for call in message.get("tool_calls") or ():
                tool_id = call.get("id") if isinstance(call, dict) else None
                if not isinstance(tool_id, str) or not tool_id or tool_id in pending:
                    return False
                pending.add(tool_id)
    return not pending


def history_fingerprint(history) -> str:
    blob = json.dumps(history, sort_keys=True, default=repr, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def anchor_index(history, anchor) -> Optional[int]:
    for index, message in enumerate(history):
        if message is anchor:
            return index
    return None


@dataclass
class TurnCarry:
    """Per-root-turn execution state carried into a continued attempt so
    nothing is reset: Gatling budgets, the no-progress ledger, and the
    executed-call list duplicate suppression reads."""
    admitted_tool_calls: int = 0
    active_elapsed_s: float = 0.0
    refused_tool_batches: int = 0
    corrective_retry_used: bool = False
    tools_used: frozenset = frozenset()
    tool_batch_ordinal: int = 0
    work_rounds_used: int = 0
    progress_ledger: object = None
    executed_calls: tuple = ()
    anchor: object = None
    candidate_pending: bool = False


@dataclass(frozen=True)
class InterruptedTurn:
    key: str
    chat_id: int
    root_turn_id: str
    turn_id: str
    next_attempt: int
    source: str
    channel_id: object
    interruption: ProviderInterruption
    dispatch_identity: object
    reasoning_effort: object
    history_ref: list
    history_len: int
    history_fp: str
    turn_seq: object
    estop_epoch: object
    suspension_token: object
    enabled_tools: frozenset
    project: object
    authority: Optional[tuple]
    carry: TurnCarry = field(compare=False)


@dataclass(frozen=True)
class Claim:
    """A consumed, fenced offer -- the only thing chat(recovery=...) accepts."""
    record: InterruptedTurn


@dataclass(frozen=True)
class Refusal:
    reason: str

    @property
    def text(self) -> str:
        return REASON_TEXT.get(self.reason, self.reason)


def evaluate_capture(*, interruption: Optional[ProviderInterruption], result: str,
                     owner: bool, source: str, desktop: bool, attempt_just_run: int,
                     history, carry: Optional[TurnCarry]) -> Optional[str]:
    """None when a just-failed turn may be offered; else a not-offered code.
    attempt_just_run is 0 for an ordinary turn, n for continuation n."""
    if interruption is None or not interruption.transport:
        return NOT_TRANSPORT
    if result != interruption.error_text:
        return NOT_THIS_FAILURE
    if not (owner and desktop and source == "OWNER_DIRECT") or carry is None:
        return NOT_OWNER_DESKTOP
    if attempt_just_run + 1 > MAX_ATTEMPTS:
        return ATTEMPTS_EXHAUSTED
    start = anchor_index(history, carry.anchor)
    if start is None:
        return ANCHOR_MISSING
    if not tool_history_closed(history[start:]):
        return TOOL_HISTORY_OPEN
    if any(call.outcome == "unknown" for call in carry.executed_calls):
        return UNKNOWN_SIDE_EFFECT
    return None


class RecoveryHolder:
    """One per agent. Holds at most one offer; claim() consumes it exactly
    once, under a lock, whether the fences pass or not."""

    def __init__(self):
        self._lock = threading.Lock()
        self._record: Optional[InterruptedTurn] = None
        self._claimed: set = set()   # keys handed out as a Claim, not yet run

    def offer(self, record: InterruptedTurn) -> None:
        with self._lock:
            self._record = record

    def drop(self) -> None:
        with self._lock:
            self._record = None

    def current(self, chat_id) -> Optional[InterruptedTurn]:
        with self._lock:
            record = self._record
        if record is None or record.chat_id != chat_id:
            return None
        return record

    def claim(self, key: str, *, chat_id, history, turn_seq, live_identity,
              estop_latched: bool, estop_epoch, verify_token: Callable[[object], None],
              enabled_tools: frozenset = frozenset(), project=None,
              live_authority: Optional[tuple] = None):
        """Consume the offer named by key and check every fence against the
        live state the caller passes in. Returns Claim or Refusal. A matching
        key consumes the offer whether or not the fences pass; a stale key
        (an older offer) is refused without touching the current one.
        enabled_tools=None means the live tool list could not be read."""
        with self._lock:
            record = self._record
            if record is None:
                return Refusal(NO_RECORD)
            if record.key != key:
                return Refusal(WRONG_OFFER)
            self._record = None
        if record.chat_id != chat_id:
            return Refusal(CHAT_CHANGED)
        if record.next_attempt > MAX_ATTEMPTS:
            return Refusal(ATTEMPTS_EXHAUSTED)
        if (history is not record.history_ref or len(history) != record.history_len
                or history_fingerprint(history) != record.history_fp):
            return Refusal(CONTEXT_CHANGED)
        start = anchor_index(history, record.carry.anchor)
        if start is None or not tool_history_closed(history[start:]):
            return Refusal(CONTEXT_CHANGED)
        if turn_seq != record.turn_seq:
            return Refusal(ADMISSION_CHANGED)
        if live_identity != record.dispatch_identity:
            return Refusal(BACKEND_CHANGED)
        if enabled_tools is None:
            return Refusal(AUTHORITY_UNREADABLE)
        if not scope_within(record, enabled_tools, project):
            return Refusal(SCOPE_WIDENED)
        widened = authority_refusal(record.authority, live_authority)
        if widened is not None:
            return Refusal(widened)
        if estop_latched:
            return Refusal(ESTOP_LATCHED)
        if estop_epoch != record.estop_epoch:
            return Refusal(ESTOP_EPOCH_CHANGED)
        try:
            verify_token(record.suspension_token)
        except Exception:
            return Refusal(SUSPENSION_REFUSED)
        with self._lock:
            self._claimed.add(record.key)
        return Claim(record)

    def begin(self, claim) -> bool:
        """Spend a Claim exactly once: True only for a Claim this holder
        issued and that has not run yet."""
        with self._lock:
            if not isinstance(claim, Claim) or claim.record.key not in self._claimed:
                return False
            self._claimed.discard(claim.record.key)
            return True


def scope_within(record: InterruptedTurn, enabled_tools: frozenset, project) -> bool:
    """Authority may only narrow between interruption and continuation: no
    tool enabled now that was not enabled then, and the same project."""
    return frozenset(enabled_tools) <= record.enabled_tools and project == record.project


@dataclass(frozen=True)
class AuthorityProbe:
    """One owner-controlled authority a tool reads live behind an unchanged
    name. capture(channel_id, chat_id) reads it now; widened(then, now) is
    True when ``now`` permits something ``then`` did not. A new such control
    is fenced by adding a probe here, not by changing the fences."""
    name: str
    capture: Callable
    widened: Callable


def _approved_drafts(channel_id, chat_id) -> frozenset:
    from core import image_generation_draft
    return image_generation_draft.approved_draft_ids(channel_id=channel_id, chat_id=chat_id)


def _telegram_remote_access(channel_id, chat_id) -> tuple:
    # on_since changes on every OFF->ON flip, so re-granting after a
    # revocation counts as new authority even if the switch reads ON again.
    from comms import telegram_access
    if not telegram_access.remote_access_enabled():
        return (False, None)
    return (True, telegram_access.on_since())


def _set_grew(then, now) -> bool:
    return not frozenset(now) <= frozenset(then)


def _switch_grew(then, now) -> bool:
    return bool(now[0]) and tuple(now) != tuple(then)


AUTHORITY_PROBES = (
    AuthorityProbe("image_draft_approvals", _approved_drafts, _set_grew),
    AuthorityProbe("telegram_remote_access", _telegram_remote_access, _switch_grew),
)


def capture_authority(channel_id, chat_id, probes=AUTHORITY_PROBES) -> Optional[tuple]:
    """((name, value), ...) for every probe, or None if any cannot be read."""
    try:
        return tuple((probe.name, probe.capture(channel_id, chat_id)) for probe in probes)
    except Exception:
        return None


def authority_refusal(then: Optional[tuple], now: Optional[tuple],
                      probes=AUTHORITY_PROBES) -> Optional[str]:
    """None when ``now`` grants nothing ``then`` did not; else a refusal
    code. Unreadable or missing state fails closed."""
    if then is None or now is None:
        return AUTHORITY_UNREADABLE
    then, now = dict(then), dict(now)
    for probe in probes:
        if probe.name not in then or probe.name not in now:
            return AUTHORITY_UNREADABLE
        try:
            if probe.widened(then[probe.name], now[probe.name]):
                return AUTHORITY_WIDENED
        except Exception:
            return AUTHORITY_UNREADABLE
    return None


def new_record(**kwargs) -> InterruptedTurn:
    return InterruptedTurn(key=uuid.uuid4().hex, **kwargs)


def prior_consequential_hashes(carry: Optional[TurnCarry]) -> frozenset:
    if carry is None:
        return frozenset()
    return frozenset((c.name, c.args_hash) for c in carry.executed_calls
                     if c.tier != READ_ONLY and c.outcome != "skipped")


def is_recovery_duplicate(prior: frozenset, name: str, tier: str, hashed: str) -> bool:
    return tier != READ_ONLY and (name, hashed) in prior
