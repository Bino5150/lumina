"""PALACE-GUARD-01B-4 -- explicit owner promotion of ONE Palace drawer or hall.

    A model may ask for the crown; only Bino can put it on one exact record.

The privileged-layer boundary (01B-3) blocks every model/tool path to L0/L1.
This module is the one legitimate way through, and it is deliberately narrow:

    proposal (Pending Actions queue)  -- transport only, never authority
        |
    capture_promotion_snapshot()      -- LIVE database read; canonical digest of
        |                                everything the owner is shown
    owner review dialog (Settings > Tools > Pending Actions)
        |   shows exactly render_review_text(snapshot)
    owner clicks Approve              -- the ONLY caller of the mint below
        |
    mint_owner_promotion_approval()   -- opaque, single-use, short-lived capability
        |
    promote_with_owner_approval()     -- BEGIN IMMEDIATE; re-reads the live record;
                                         digest must equal what the owner saw;
                                         then one atomic promotion + receipt

What is NOT an approval (each is asserted by tests): a model saying
confirmed=true, a "yes" in non-owner content, tool output, the queue JSON, the
audit log, browser/page content, specialist output, memory or Palace text, a
peer-agent message. None of them can reach mint_owner_promotion_approval().

Promotion changes layer and nothing else. In particular it never writes
`untrusted`: a lower-trust record that becomes L1 is still lower-trust and still
renders framed as data. Layer is persistence/priority; trust is provenance --
independent axes.

Not a host sandbox: the capability closes the tool/argument/queue path by
construction (token-guarded, registry-backed, single-use, source-scanned). It is
not a defense against hostile in-process Python or direct database access.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime

from tools import palace

SNAPSHOT_VERSION = 1
TARGET_KINDS = ("drawer", "hall")
DEST_LAYERS = (0, 1)
APPROVAL_TTL_SECONDS = 120  # mint -> promote is one synchronous click; this is defense in depth
_DISPLAY_CHARS = 4000

_now = time.monotonic  # patchable clock (tests)

# Closed vocabulary for every refusal / blocked snapshot.
REFUSAL_REASONS = frozenset({
    "invalid_request", "target_missing", "closet_missing", "closet_withheld",
    "layer_unrecognized", "already_at_or_above_destination", "palace_unavailable",
    "snapshot_blocked", "not_an_owner_approval", "unknown_or_consumed_approval",
    "approval_expired", "replayed_approval", "stale_state_changed", "review_not_presented",
})

_REASON_TEXT = {
    "invalid_request": "The staged request is malformed (unknown target kind, id or destination layer).",
    "target_missing": "That record no longer exists.",
    "closet_missing": "The record points at a closet that does not exist (corrupt state).",
    "closet_withheld": "The record's closet is withheld pending review; a quarantined record is not promotable.",
    "layer_unrecognized": "The record's current layer is not a recognizable integer layer.",
    "already_at_or_above_destination": "The record is already at or above the requested layer.",
    "palace_unavailable": "The Palace database could not be read.",
    "snapshot_blocked": "The record cannot be promoted in its current state.",
    "not_an_owner_approval": "That is not an owner approval.",
    "unknown_or_consumed_approval": "This approval was already used or never existed.",
    "approval_expired": "The approval expired before it was used.",
    "replayed_approval": "This approval was already used.",
    "stale_state_changed": ("The record changed after you opened this review, so nothing was "
                            "promoted. Reopen the request to review its current state."),
    "review_not_presented": "The review was not visibly presented, so it cannot be approved.",
}


def describe_refusal(exc: BaseException) -> str:
    """One owner-facing sentence for a refused promotion."""
    if isinstance(exc, PromotionRefused):
        return _REASON_TEXT.get(exc.detail, "") or _REASON_TEXT.get(exc.reason, exc.reason)
    return f"Promotion failed ({type(exc).__name__}); nothing was changed."


class PromotionRefused(Exception):
    """A promotion (or the approval for one) was refused; nothing was written
    beyond consuming the approval. `reason` is a closed REFUSAL_REASONS code."""

    def __init__(self, reason: str, detail: str = ""):
        if reason not in REFUSAL_REASONS:
            raise ValueError(f"unknown refusal reason {reason!r}")
        self.reason = reason
        self.detail = detail
        super().__init__(reason + (f": {detail}" if detail else ""))


# ── Live snapshot ──────────────────────────────────────────────────────────────

def _sha(text) -> str:
    return hashlib.sha256(str(text if text is not None else "").encode("utf-8", "surrogatepass")).hexdigest()


def _j(value):
    """JSON-safe, type-preserving scalar for the digest."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (bytes, bytearray)):
        return "bytes:" + bytes(value).hex()
    return "repr:" + repr(value)


@dataclass(frozen=True)
class PromotionSnapshot:
    """Everything the owner is shown, captured in ONE live read, plus the digest
    of the facts that will be re-verified at execution. `content_text` and
    `closet_text` are display-only; they are bound to the digest through the
    content/closet SHA-256 values inside `facts_json`."""
    target_kind: str
    target_id: int
    dest_layer: int
    operation_id: str | None
    digest: str
    blocked_reason: str | None
    facts_json: str
    content_text: str = ""
    closet_text: str | None = None

    @property
    def facts(self) -> dict:
        return json.loads(self.facts_json)


def _finish(facts: dict, *, blocked=None, content_text="", closet_text=None, kind, target_id,
            dest_layer, operation_id) -> PromotionSnapshot:
    facts_json = json.dumps(facts, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return PromotionSnapshot(
        target_kind=kind, target_id=target_id, dest_layer=dest_layer, operation_id=operation_id,
        digest=hashlib.sha256(facts_json.encode("ascii")).hexdigest(),
        blocked_reason=blocked, facts_json=facts_json,
        content_text=content_text, closet_text=closet_text,
    )


def capture_promotion_snapshot(target_kind, target_id, dest_layer, *, operation_id=None,
                               conn=None) -> PromotionSnapshot:
    """Read the target LIVE and describe exactly what promoting it would do.
    Read-only. Never raises for an unpromotable target: `blocked_reason` says why
    (and the owner review shows it). `conn` lets the executor run this inside
    its own write transaction so the re-check and the promotion are one atomic
    step."""
    request_ok = (
        target_kind in TARGET_KINDS
        and type(target_id) is int
        and type(dest_layer) is int and dest_layer in DEST_LAYERS
        and (operation_id is None or type(operation_id) is str)
    )
    if not request_ok:
        facts = {"v": SNAPSHOT_VERSION, "invalid_request": True}
        return _finish(facts, blocked="invalid_request", kind=str(target_kind)[:16],
                       target_id=target_id if type(target_id) is int else -1,
                       dest_layer=dest_layer if type(dest_layer) is int else -1,
                       operation_id=operation_id if type(operation_id) is str else None)
    own = conn is None
    try:
        if own:
            conn = palace.get_db()
        if target_kind == "drawer":
            return _capture_drawer(conn, target_id, dest_layer, operation_id)
        return _capture_hall(conn, target_id, dest_layer, operation_id)
    except (sqlite3.Error, IndexError):  # IndexError: a column an unmigrated DB lacks
        facts = {"v": SNAPSHOT_VERSION, "kind": target_kind, "id": target_id,
                 "dest_layer": dest_layer, "operation_id": operation_id, "unavailable": True}
        return _finish(facts, blocked="palace_unavailable", kind=target_kind,
                       target_id=target_id, dest_layer=dest_layer, operation_id=operation_id)
    finally:
        if own and conn is not None:
            conn.close()


def _capture_drawer(conn, drawer_id, dest_layer, operation_id) -> PromotionSnapshot:
    base = {"v": SNAPSHOT_VERSION, "kind": "drawer", "id": drawer_id, "dest_layer": dest_layer,
            "operation_id": operation_id}
    common = dict(kind="drawer", target_id=drawer_id, dest_layer=dest_layer,
                  operation_id=operation_id)
    d = conn.execute(
        "SELECT d.*, r.name AS room_name, w.name AS wing_name FROM palace_drawers d "
        "JOIN palace_rooms r ON r.id=d.room_id JOIN palace_wings w ON w.id=r.wing_id "
        "WHERE d.id=?", (drawer_id,),
    ).fetchone()
    if d is None:
        return _finish({**base, "missing": True}, blocked="target_missing", **common)

    facts = {
        **base,
        "content_sha256": _sha(d["content"]), "tags_sha256": _sha(d["tags"]),
        "untrusted": _j(d["untrusted"]), "origin": _j(d["origin"]),
        "source_memory_id": _j(d["source_memory_id"]), "created_at": _j(d["created_at"]),
        "wing": d["wing_name"], "room": d["room_name"], "room_id": d["room_id"],
        "closet_id": d["closet_id"], "current_layer": None, "closet_admission": None,
        "closet_text_sha256": None, "closet_render_exact": None, "closet_drawer_ids": [],
    }
    kw = dict(content_text=d["content"] or "", **common)
    if d["closet_id"] is None:
        return _finish(facts, **kw)

    c = conn.execute(
        "SELECT id, layer, compressed, withheld_reason, admission FROM palace_closets WHERE id=?",
        (d["closet_id"],),
    ).fetchone()
    if c is None:
        return _finish(facts, blocked="closet_missing", **kw)
    siblings = conn.execute(
        "SELECT id, content, untrusted FROM palace_drawers WHERE closet_id=? "
        "ORDER BY created_at, id", (c["id"],),
    ).fetchall()
    exact = palace.render_closet_text(f"{d['wing_name']}.{d['room_name']}", siblings) == c["compressed"]
    facts.update({
        "current_layer": _j(c["layer"]), "closet_admission": _j(c["admission"]),
        "closet_withheld_reason": _j(c["withheld_reason"]),
        "closet_text_sha256": _sha(c["compressed"]), "closet_render_exact": exact,
        "closet_drawer_ids": [s["id"] for s in siblings],
    })
    kw["closet_text"] = c["compressed"]
    if c["withheld_reason"] is not None:
        return _finish(facts, blocked="closet_withheld", **kw)
    if type(c["layer"]) is not int:
        return _finish(facts, blocked="layer_unrecognized", **kw)
    if c["layer"] <= dest_layer:
        return _finish(facts, blocked="already_at_or_above_destination", **kw)
    return _finish(facts, **kw)


def _capture_hall(conn, hall_id, dest_layer, operation_id) -> PromotionSnapshot:
    base = {"v": SNAPSHOT_VERSION, "kind": "hall", "id": hall_id, "dest_layer": dest_layer,
            "operation_id": operation_id}
    common = dict(kind="hall", target_id=hall_id, dest_layer=dest_layer, operation_id=operation_id)
    h = conn.execute(
        "SELECT id, hall, compressed, layer, untrusted, source_memory_id, admission, created_at "
        "FROM palace_halls WHERE id=?", (hall_id,),
    ).fetchone()
    if h is None:
        return _finish({**base, "missing": True}, blocked="target_missing", **common)
    facts = {
        **base, "hall": h["hall"], "content_sha256": _sha(h["compressed"]),
        "untrusted": _j(h["untrusted"]), "source_memory_id": _j(h["source_memory_id"]),
        "admission": _j(h["admission"]), "created_at": _j(h["created_at"]),
        "current_layer": _j(h["layer"]),
    }
    kw = dict(content_text=h["compressed"] or "", **common)
    if type(h["layer"]) is not int:
        return _finish(facts, blocked="layer_unrecognized", **kw)
    if h["layer"] <= dest_layer:
        return _finish(facts, blocked="already_at_or_above_destination", **kw)
    return _finish(facts, **kw)


def render_review_text(snapshot: PromotionSnapshot) -> str:
    """The exact, plain-text consequence the owner approves. Built ONLY from the
    live snapshot -- never from the queue entry. The record's own text is
    model-authored data and is shown as data (the UI must render it as plain
    text)."""
    f = snapshot.facts
    lines = []
    if snapshot.blocked_reason:
        lines += ["CANNOT BE PROMOTED",
                  _REASON_TEXT.get(snapshot.blocked_reason, snapshot.blocked_reason), ""]
    if f.get("invalid_request") or f.get("missing") or f.get("unavailable"):
        lines.append(f"Request: promote {snapshot.target_kind} #{snapshot.target_id} "
                     f"to L{snapshot.dest_layer}")
        if snapshot.operation_id:
            lines.append(f"Staged request: #{snapshot.operation_id} (a request, not an approval)")
        return "\n".join(lines)

    kind, dest = snapshot.target_kind, snapshot.dest_layer
    lines.append(f"PROMOTE {kind.upper()} #{snapshot.target_id}  ->  L{dest}")
    if snapshot.operation_id:
        lines.append(f"Staged request: #{snapshot.operation_id} (a request, not an approval)")
    cur = f.get("current_layer")
    lines.append(f"Current layer: {('L' + str(cur)) if cur is not None else 'none (not in any closet)'}")
    lines.append(f"Destination:   L{dest} -- injected into EVERY owner turn, exempt from decay "
                 "and injection limits")
    lines.append("")
    if kind == "drawer":
        lines.append(f"Location: {f['wing']} / {f['room']}")
    else:
        lines.append(f"Hall: {f['hall']}")
    untrusted = f.get("untrusted")
    if untrusted:
        lines.append("Trust: LOWER-TRUST (untrusted). Promotion changes ONLY the layer. It will "
                     "stay lower-trust and be shown to Lumina framed as data, not as your own words.")
    else:
        lines.append("Trust: owner-trusted (untrusted=0). Promotion does not change trust.")
    if f.get("origin") is not None:
        lines.append(f"Origin stamp: {f['origin']}")
    if f.get("source_memory_id") is not None:
        lines.append(f"Linked to flat memory #{f['source_memory_id']}")
    if f.get("closet_admission") or f.get("admission"):
        lines.append(f"Existing admission: {f.get('closet_admission') or f.get('admission')}")

    if kind == "drawer":
        ids = f.get("closet_drawer_ids") or []
        if f.get("closet_id") is None:
            lines.append("Closet: none. The drawer gets its own L%d closet." % dest)
        elif len(ids) <= 1:
            lines.append(f"Closet #{f['closet_id']} (L{cur}) holds only this drawer: it will be "
                         f"removed and the drawer gets its own L{dest} closet.")
        elif f.get("closet_render_exact"):
            lines.append(f"Closet #{f['closet_id']} (L{cur}) holds {len(ids)} drawers and matches "
                         f"them exactly. The other {len(ids) - 1} stay and the closet is rebuilt "
                         "from them, byte for byte.")
        else:
            lines.append(f"Closet #{f['closet_id']} (L{cur}) holds {len(ids)} drawers but its text "
                         f"has DRIFTED from them. The other {len(ids) - 1} stay untouched; the "
                         "closet is WITHHELD from Lumina's memory pending review (not edited).")
    lines.append("")
    text = snapshot.content_text
    shown = text[:_DISPLAY_CHARS]
    lines.append("Record text (data, exactly as stored):")
    lines.append(shown + (f"\n... ({len(text) - _DISPLAY_CHARS} more characters not shown)"
                          if len(text) > _DISPLAY_CHARS else ""))
    lines.append("")
    lines.append(f"Bound state digest: {snapshot.digest[:16]}...  (approval fails if anything "
                 "above changes before you click)")
    return "\n".join(lines)


# ── The approval capability ────────────────────────────────────────────────────

_MINT = object()  # module-private mint token
_LOCK = threading.Lock()


@dataclass(frozen=True)
class _LiveApproval:
    digest: str
    target_kind: str
    target_id: int
    dest_layer: int
    operation_id: str | None
    owner_event: str
    expires: float


_LIVE: dict[str, _LiveApproval] = {}  # approval_id -> what the owner approved (authoritative)


class OwnerPromotionApproval:
    """Opaque approval capability. It carries only an id; everything it binds
    (digest, target, destination, owner event, expiry) lives in the private
    registry, so editing an instance can change nothing. Constructible only with
    the module-private token; not copyable, serializable, mutable or
    subclassable."""

    __slots__ = ("_approval_id", "__weakref__")

    def __init_subclass__(cls, **kwargs):
        raise TypeError("OwnerPromotionApproval cannot be subclassed")

    def __init__(self, token, approval_id):
        if token is not _MINT:
            raise TypeError("OwnerPromotionApproval can only be minted by the owner review")
        object.__setattr__(self, "_approval_id", approval_id)

    def __setattr__(self, name, value):
        raise AttributeError("OwnerPromotionApproval is immutable")

    def __delattr__(self, name):
        raise AttributeError("OwnerPromotionApproval is immutable")

    def __copy__(self):
        raise TypeError("OwnerPromotionApproval cannot be copied")

    def __deepcopy__(self, memo):
        raise TypeError("OwnerPromotionApproval cannot be copied")

    def __reduce_ex__(self, protocol):
        raise TypeError("OwnerPromotionApproval cannot be serialized")


def mint_owner_promotion_approval(snapshot: PromotionSnapshot, *, owner_event: str) -> OwnerPromotionApproval:
    """Bind an approval to exactly the snapshot the owner was shown.

    THE mint. Its one caller is the owner review dialog's Approve-click handler
    (ui/palace_promotion_review.py); a source-scan test fails if anything else
    references it. `owner_event` identifies that click for the audit receipt; it
    is a label, not a credential."""
    if type(snapshot) is not PromotionSnapshot:
        raise PromotionRefused("invalid_request", "not a promotion snapshot")
    if snapshot.blocked_reason:
        raise PromotionRefused("snapshot_blocked", snapshot.blocked_reason)
    if type(owner_event) is not str or not owner_event:
        raise PromotionRefused("invalid_request", "an owner event identity is required")
    approval_id = uuid.uuid4().hex
    now = _now()
    with _LOCK:
        for stale in [k for k, v in _LIVE.items() if v.expires < now]:
            del _LIVE[stale]
        _LIVE[approval_id] = _LiveApproval(
            digest=snapshot.digest, target_kind=snapshot.target_kind,
            target_id=snapshot.target_id, dest_layer=snapshot.dest_layer,
            operation_id=snapshot.operation_id, owner_event=owner_event,
            expires=now + APPROVAL_TTL_SECONDS,
        )
    return OwnerPromotionApproval(_MINT, approval_id)


# ── The executor ───────────────────────────────────────────────────────────────

def promote_with_owner_approval(approval) -> dict:
    """Execute ONE approved promotion, or refuse. Fails closed on everything.

    The approval is consumed the instant it is presented (a failed attempt burns
    it; the owner reviews again). Inside one BEGIN IMMEDIATE transaction it
    re-reads the live record, requires the digest to equal what the owner
    approved, mints a promotion admission scoped to that one record and layer,
    performs the promotion through the Palace chokepoints, and writes the
    receipt -- or rolls everything back. Returns the receipt."""
    if type(approval) is not OwnerPromotionApproval:
        raise PromotionRefused("not_an_owner_approval")
    try:
        approval_id = approval._approval_id
    except AttributeError:
        raise PromotionRefused("not_an_owner_approval") from None
    with _LOCK:
        live = _LIVE.pop(approval_id, None) if type(approval_id) is str else None
    if live is None:
        raise PromotionRefused("unknown_or_consumed_approval")
    if _now() > live.expires:
        raise PromotionRefused("approval_expired")

    conn = palace.get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM palace_promotion_receipts WHERE approval_id=?",
                        (approval_id,)).fetchone():
            raise PromotionRefused("replayed_approval")
        snap = capture_promotion_snapshot(live.target_kind, live.target_id, live.dest_layer,
                                          operation_id=live.operation_id, conn=conn)
        if snap.digest != live.digest:
            raise PromotionRefused("stale_state_changed")
        if snap.blocked_reason:
            raise PromotionRefused("snapshot_blocked", snap.blocked_reason)
        admission = palace._mint_owner_promotion_admission(
            live.target_kind, live.target_id, live.dest_layer)
        facts = snap.facts
        if live.target_kind == "drawer":
            done = palace._promote_drawer_in_conn(conn, live.target_id, live.dest_layer, admission)
        else:
            done = palace._promote_hall_in_conn(conn, live.target_id, live.dest_layer, admission)
        receipt = {
            "approval_id": approval_id, "operation_id": live.operation_id,
            "owner_event": live.owner_event, "target_kind": live.target_kind,
            "target_id": live.target_id, "from_layer": facts.get("current_layer"),
            "to_layer": live.dest_layer, "snapshot_digest": live.digest,
            "untrusted": facts.get("untrusted"), "result_closet_id": done.get("closet_id"),
            "closet_outcome": done.get("extraction"),
            "promoted_at": datetime.now().isoformat(),
        }
        conn.execute(
            "INSERT INTO palace_promotion_receipts (approval_id, operation_id, owner_event, "
            "target_kind, target_id, from_layer, to_layer, snapshot_digest, untrusted, "
            "result_closet_id, closet_outcome, promoted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (receipt["approval_id"], receipt["operation_id"], receipt["owner_event"],
             receipt["target_kind"], receipt["target_id"], receipt["from_layer"],
             receipt["to_layer"], receipt["snapshot_digest"],
             1 if receipt["untrusted"] else 0, receipt["result_closet_id"],
             receipt["closet_outcome"], receipt["promoted_at"]),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return receipt
