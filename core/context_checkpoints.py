"""
core/context_checkpoints.py -- CONTEXT-LIFECYCLE-A3I: durable
context-continuity checkpoint store.

Storage/lifecycle substrate only. A checkpoint row is a machine-owned
lifecycle record representing one build attempt, for one chat, against one
exact machine-observed durable reconstruction spine (core/context_
reconstruction.py's reconstruct_chat_context()/durable_spine_fingerprint --
the A1/A2 kernel, unmodified and re-used verbatim here, never re-derived).
It does not mean "the model says these facts are true" -- READY only means
this store completed a validated transaction. Future A4 owns generating and
provenance-tagging actual continuity content; future A5 owns consuming a
READY checkpoint during a transactional context rebuild. Neither is
implemented here: no model/compiler call, no live ContextManager mutation,
no UI, no model-facing tool.

Lifecycle
---------
    new -> BUILDING
    BUILDING -> READY
    BUILDING -> FAILED
    READY -> SUPERSEDED

No FAILED -> READY, no SUPERSEDED -> READY, no READY -> BUILDING, no direct
READY creation. A build that must be retried creates a new checkpoint
identity via begin_checkpoint() again -- the old row is never resurrected.

An orphaned BUILDING row is simply non-consumable indefinitely (A3-PIN-01):
validity never depends on age, wall-clock expiry, or process-liveness
guesses. SUPERSEDED is advisory bookkeeping only (A3-PIN-02): the newest
valid READY checkpoint wins by deterministic ordering (id DESC, an
AUTOINCREMENT primary key, never reused) whether or not older READY rows
were ever successfully marked SUPERSEDED.

Atomicity
---------
finalize_checkpoint() re-derives the current durable-spine fingerprint and
performs the BUILDING->READY compare-and-swap inside one BEGIN IMMEDIATE
transaction on this module's own connection. BEGIN IMMEDIATE acquires
SQLite's database-wide RESERVED write lock immediately (not lazily at first
write) -- every other writer in this codebase goes through core.db.connect()
(WAL + busy_timeout=5000ms, core/db.py), so no other connection can insert or
update *any* table until this transaction commits or rolls back. This
includes chat_messages AND palace_drawers -- tools.palace.get_db() is
verified, not assumed, to be the exact same connection factory
(tools/palace.py's get_db() is literally `from core.db import connect;
return connect()`), so a manual-compaction context-skip:N Drawer write
(core/manual_compaction.py's run_manual_compaction() -> tools.palace.
palace_store()) shares this module's lock domain too. That matters because
the spine recheck below reads BOTH sources (chat_messages via
reconstruct_chat_context(), palace_drawers via
core.manual_compaction.latest_manual_compaction_skip()) -- a Palace write
racing the recheck is exactly as blocked as a chat_messages write would be.
tests/test_context_checkpoints.py::
test_palace_write_cannot_land_between_finalize_spine_observation_and_ready_commit
proves this with an explicit commit-ordering log, not just this docstring's
say-so. reconstruct_chat_context() and latest_manual_compaction_skip() each
open their own read-only connection internally (unmodified -- this module
never redefines A1/A2 fingerprint semantics), but because they only execute
while this module's RESERVED lock is already held, their reads are provably
stable through the CAS commit: no durable row or compaction Drawer can be
inserted/updated between "recompute the spine" and "commit READY" without
first blocking on this transaction's lock. This is the same BEGIN IMMEDIATE
+ busy_timeout pattern core/coding_checkpoint.py already uses and documents
as its sole correctness boundary (no in-process lock). No transaction here
ever spans model/compiler/external work -- begin_checkpoint() and
finalize_checkpoint() are both bounded, local-only critical sections.

Fail-closed skip resolution
----------------------------
finalize_checkpoint() and get_latest_usable_checkpoint() both resolve the
current context_skip by calling reconstruct_chat_context(chat_id,
context_skip=None), which lets core.context_reconstruction's own default
resolution (core.manual_compaction.latest_manual_compaction_skip()) raise
straight through on a read failure -- deliberately NOT
core.context_reconstruction.resolve_context_skip()'s graceful
degrade-to-zero, which exists for _load_chat()'s UX (show something rather
than crash chat load). A machine-owned correctness store cannot make that
trade: silently substituting skip=0 on a transient read failure can make an
actually-stale checkpoint (one legitimately built against an earlier skip)
wrongly compare as matching, either promoting it to READY when it shouldn't
be, or presenting it as "usable" when the true current spine has moved past
it. A resolution failure must surface as an exception, never as a guessed
READY/usable verdict. See
tests/test_context_checkpoints.py::test_stale_checkpoint_never_becomes_usable_via_skip_resolution_failure
for the exact scenario this prevents.
"""
import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from core.context_reconstruction import reconstruct_chat_context

STATE_BUILDING = "BUILDING"
STATE_READY = "READY"
STATE_FAILED = "FAILED"
STATE_SUPERSEDED = "SUPERSEDED"

SUPPORTED_PAYLOAD_VERSIONS = {1}
MAX_PAYLOAD_BYTES = 64 * 1024
MAX_FAILURE_REASON_LEN = 500


class ContextCheckpointError(Exception):
    """Base class for every error this module raises."""


class UnknownChat(ContextCheckpointError):
    """begin_checkpoint()'s chat_id does not reference an existing chat."""


class CheckpointNotFound(ContextCheckpointError):
    """No context_checkpoints row exists for this id."""


class IdentityMismatch(ContextCheckpointError):
    """A caller-supplied expected_chat_id / expected_spine_fingerprint does
    not match this checkpoint's frozen record -- a caller bug (wrong
    checkpoint reference), not a race against live database state. See
    StaleSpine for the latter."""


class StateConflict(ContextCheckpointError):
    """The requested transition is illegal from the row's current state, or
    a concurrent transition already won the compare-and-swap."""


class StaleSpine(ContextCheckpointError):
    """The durable spine recomputed at finalize time no longer matches this
    checkpoint's frozen binding -- something changed the reconstructable
    state (a new durable message, an edit, a context-skip move) between
    begin_checkpoint() and finalize_checkpoint(). The checkpoint is
    transitioned BUILDING -> FAILED as a side effect of raising this: the
    frozen binding can never be satisfied again, so retrying finalize on
    the same id would only fail identically."""


class PayloadValidationError(ContextCheckpointError):
    """The payload envelope is malformed, an unsupported version, not
    JSON-serializable, or exceeds the size cap. The row is left in BUILDING
    -- unlike StaleSpine this is a caller-input problem, not a race, so a
    corrected payload can be retried against the same frozen spine binding
    via another finalize_checkpoint() call on the same checkpoint id."""


@dataclass(frozen=True)
class CheckpointRecord:
    id: int
    chat_id: int
    state: str
    durable_spine_fingerprint: str
    context_skip: int
    payload_version: Optional[int]
    payload: Optional[dict]
    payload_hash: Optional[str]
    failure_reason: Optional[str]
    created_at: str
    ready_at: Optional[str]
    failed_at: Optional[str]
    superseded_at: Optional[str]


def init_context_checkpoint_db():
    """Create the context_checkpoints table/indexes if they don't exist.
    Safe to call on every operation -- CREATE TABLE/INDEX IF NOT EXISTS,
    idempotent, never migrates or touches existing rows. Never captures a
    DB path at import time: connect() reads config.DB_PATH fresh on every
    call, matching core/coding_checkpoint.py's init_checkpoint_db()."""
    from core.db import connect
    # Same narrow WAL-transition retry as core/coding_checkpoint.py's
    # init_checkpoint_db() -- two first-ever callers can both reach
    # connect() while the database is still switching into WAL mode.
    conn = None
    for attempt in range(20):
        try:
            conn = connect()
            break
        except sqlite3.OperationalError as error:
            if (
                getattr(error, "sqlite_errorcode", None) != sqlite3.SQLITE_BUSY
                or attempt == 19
            ):
                raise
            time.sleep(0.01)
    if conn is None:  # pragma: no cover - defensive, loop either sets or raises
        raise ContextCheckpointError("could not initialize context checkpoint database")
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS context_checkpoints (
                id                          INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id                     INTEGER NOT NULL,
                state                       TEXT NOT NULL,
                durable_spine_fingerprint   TEXT NOT NULL,
                context_skip                INTEGER NOT NULL,
                payload_version             INTEGER,
                payload_json                TEXT,
                payload_hash                TEXT,
                failure_reason              TEXT,
                created_at                  TEXT NOT NULL,
                ready_at                    TEXT,
                failed_at                   TEXT,
                superseded_at               TEXT,
                FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_context_checkpoints_chat_state
            ON context_checkpoints(chat_id, state, id)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_context_checkpoints_chat_fingerprint
            ON context_checkpoints(chat_id, durable_spine_fingerprint)
        """)
        conn.commit()
    finally:
        conn.close()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_valid_fingerprint(value) -> bool:
    return (
        isinstance(value, str) and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


def _canonicalize_payload(payload) -> tuple:
    """Deterministic JSON serialization + its SHA-256 hex digest, hashed
    from the exact bytes that get stored -- one canonical path, so there is
    never a "storage serialization" vs "hash serialization" divergence."""
    if not isinstance(payload, dict):
        raise PayloadValidationError("payload must be a JSON object (dict)")
    try:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as e:
        raise PayloadValidationError(f"payload is not JSON-serializable: {e}") from e
    encoded = canonical.encode("utf-8")
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise PayloadValidationError(
            f"payload is {len(encoded)} bytes, exceeds cap of {MAX_PAYLOAD_BYTES} bytes"
        )
    return canonical, hashlib.sha256(encoded).hexdigest()


def _load_payload(row):
    """Returns (parsed_dict_or_None, valid: bool). Only a READY row can ever
    be valid -- BUILDING/FAILED/SUPERSEDED rows never carry a trusted
    payload regardless of what their payload_json column happens to hold."""
    if row["state"] != STATE_READY:
        return None, False
    payload_json = row["payload_json"]
    payload_version = row["payload_version"]
    payload_hash = row["payload_hash"]
    if payload_json is None or payload_version is None or payload_hash is None:
        return None, False
    if payload_version not in SUPPORTED_PAYLOAD_VERSIONS:
        return None, False
    try:
        parsed = json.loads(payload_json)
    except (TypeError, ValueError):
        return None, False
    if not isinstance(parsed, dict):
        return None, False
    if hashlib.sha256(payload_json.encode("utf-8")).hexdigest() != payload_hash:
        return None, False
    return parsed, True


def _row_to_record(row) -> CheckpointRecord:
    payload, valid = _load_payload(row)
    return CheckpointRecord(
        id=row["id"],
        chat_id=row["chat_id"],
        state=row["state"],
        durable_spine_fingerprint=row["durable_spine_fingerprint"],
        context_skip=row["context_skip"],
        payload_version=row["payload_version"],
        payload=payload if valid else None,
        payload_hash=row["payload_hash"],
        failure_reason=row["failure_reason"],
        created_at=row["created_at"],
        ready_at=row["ready_at"],
        failed_at=row["failed_at"],
        superseded_at=row["superseded_at"],
    )


def _sanitize_failure_reason(reason) -> Optional[str]:
    """Bounded, plain-text only -- never a sink for arbitrary prompt
    content. No model authority is implied by this field."""
    if reason is None:
        return None
    reason = str(reason).strip()
    if not reason:
        return None
    return reason[:MAX_FAILURE_REASON_LEN]


# ---------------------------------------------------------------------------
# Storage API
# ---------------------------------------------------------------------------

def begin_checkpoint(chat_id: int, durable_spine_fingerprint: str, context_skip: int) -> CheckpointRecord:
    """Create a new BUILDING checkpoint bound to (chat_id, durable_spine_
    fingerprint, context_skip). Validates chat binding via the schema's own
    FK enforcement (core.db.connect() defaults foreign_keys=ON) rather than
    a separate read-then-write existence check. Never calls a model or
    compiler. Commits and releases its connection before returning --
    concurrent begin_checkpoint() calls for the same or different chats are
    safe and do not serialize on anything beyond SQLite's own write lock
    (A3-PIN-01: no "one BUILDING per chat" restriction is imposed here)."""
    if not isinstance(chat_id, int) or isinstance(chat_id, bool):
        raise ContextCheckpointError("chat_id must be an int")
    if not _is_valid_fingerprint(durable_spine_fingerprint):
        raise ContextCheckpointError("durable_spine_fingerprint must be a 64-char lowercase hex sha256 digest")
    if not isinstance(context_skip, int) or isinstance(context_skip, bool) or context_skip < 0:
        raise ContextCheckpointError("context_skip must be a non-negative int")

    init_context_checkpoint_db()
    from core.db import connect
    conn = connect()
    try:
        now = _utcnow_iso()
        try:
            cur = conn.execute(
                "INSERT INTO context_checkpoints "
                "(chat_id, state, durable_spine_fingerprint, context_skip, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (chat_id, STATE_BUILDING, durable_spine_fingerprint, context_skip, now),
            )
        except sqlite3.IntegrityError as e:
            conn.rollback()
            raise UnknownChat(f"chat_id {chat_id} does not exist: {e}") from e
        checkpoint_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    return get_checkpoint(checkpoint_id)


def finalize_checkpoint(checkpoint_id: int, expected_chat_id: int, expected_spine_fingerprint: str,
                         payload_version: int, payload: dict) -> CheckpointRecord:
    """Validate, revalidate, and atomically promote a BUILDING checkpoint to
    READY. See the module docstring's "Atomicity" section for exactly what
    this BEGIN IMMEDIATE transaction covers and why the spine recheck
    inside it is honest, not merely apparent, atomicity.

    Order of checks: row exists -> chat identity matches -> spine
    fingerprint identity matches (caller's own bookkeeping) -> row is still
    BUILDING -> payload envelope is valid -> current durable spine still
    matches the frozen binding -> CAS BUILDING->READY. A stale spine
    transitions the row to FAILED (permanent for this id -- the frozen
    binding can never be satisfied again); a payload validation failure
    leaves the row BUILDING (retriable with a corrected payload)."""
    if payload_version not in SUPPORTED_PAYLOAD_VERSIONS:
        raise PayloadValidationError(
            f"payload_version {payload_version!r} is not supported; expected one of {sorted(SUPPORTED_PAYLOAD_VERSIONS)}"
        )
    canonical_payload, payload_hash = _canonicalize_payload(payload)

    init_context_checkpoint_db()
    from core.db import connect
    conn = connect()
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM context_checkpoints WHERE id=?", (checkpoint_id,)
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            raise CheckpointNotFound(f"no context checkpoint with id {checkpoint_id}")
        if row["chat_id"] != expected_chat_id:
            conn.execute("ROLLBACK")
            raise IdentityMismatch(
                f"checkpoint {checkpoint_id} belongs to chat {row['chat_id']}, not {expected_chat_id}"
            )
        if row["durable_spine_fingerprint"] != expected_spine_fingerprint:
            conn.execute("ROLLBACK")
            raise IdentityMismatch(
                f"checkpoint {checkpoint_id} was bound to a different spine fingerprint than expected"
            )
        if row["state"] != STATE_BUILDING:
            conn.execute("ROLLBACK")
            raise StateConflict(f"checkpoint {checkpoint_id} is {row['state']}, not BUILDING")

        # Spine recheck: re-resolve the CURRENT durable spine, at whatever
        # context_skip is in effect right now. context_skip=None lets
        # reconstruct_chat_context() resolve it via
        # latest_manual_compaction_skip() directly -- fail-closed: a read
        # failure raises straight through (caught by the except below,
        # rolling back with the row left BUILDING) rather than silently
        # substituting skip=0. See the module docstring's "Fail-closed
        # skip resolution" section for why a machine-owned correctness
        # store cannot use _load_chat()'s graceful degrade-to-zero here --
        # a wrong guess could make an actually-stale checkpoint compare as
        # matching. row["context_skip"] (the skip frozen at begin time) is
        # stored for diagnostics only and is deliberately NOT used for this
        # recheck -- a skip that moved between begin and finalize must
        # change the recomputed fingerprint just like a new/edited durable
        # row would (context_skip movement is one more way the durable
        # spine can move, not a separate signal). Our BEGIN IMMEDIATE above
        # already holds the database-wide RESERVED write lock, so both
        # reconstruct_chat_context()'s own connection (chat_messages) and
        # latest_manual_compaction_skip()'s own connection (palace_drawers
        # -- the exact same lock domain, see module docstring) read a state
        # that cannot change until we COMMIT or ROLLBACK below.
        current = reconstruct_chat_context(row["chat_id"], context_skip=None)
        if current.durable_spine_fingerprint != row["durable_spine_fingerprint"]:
            now = _utcnow_iso()
            conn.execute(
                "UPDATE context_checkpoints SET state=?, failed_at=?, failure_reason=? "
                "WHERE id=? AND state=?",
                (STATE_FAILED, now, "stale_spine", checkpoint_id, STATE_BUILDING),
            )
            conn.execute("COMMIT")
            raise StaleSpine(
                f"durable spine for chat {row['chat_id']} moved since begin_checkpoint "
                f"(checkpoint {checkpoint_id}); checkpoint marked FAILED"
            )

        now = _utcnow_iso()
        cur = conn.execute(
            "UPDATE context_checkpoints "
            "SET state=?, payload_version=?, payload_json=?, payload_hash=?, ready_at=? "
            "WHERE id=? AND state=?",
            (STATE_READY, payload_version, canonical_payload, payload_hash, now,
             checkpoint_id, STATE_BUILDING),
        )
        if cur.rowcount != 1:
            conn.execute("ROLLBACK")
            raise StateConflict(f"checkpoint {checkpoint_id} was not BUILDING at commit time")
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
    return get_checkpoint(checkpoint_id)


def fail_checkpoint(checkpoint_id: int, expected_chat_id: int, reason: str = None) -> CheckpointRecord:
    """Record a known build failure: BUILDING -> FAILED. Idempotent when the
    row is already FAILED (returns it unchanged, no error). Cannot demote
    READY and cannot resurrect SUPERSEDED -- both raise StateConflict. This
    is for known failures a caller observed; it is not required crash
    recovery -- an orphaned BUILDING row from a crash simply stays BUILDING
    forever (A3-PIN-01), and that is acceptable."""
    reason = _sanitize_failure_reason(reason)

    init_context_checkpoint_db()
    from core.db import connect
    conn = connect()
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM context_checkpoints WHERE id=?", (checkpoint_id,)
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            raise CheckpointNotFound(f"no context checkpoint with id {checkpoint_id}")
        if row["chat_id"] != expected_chat_id:
            conn.execute("ROLLBACK")
            raise IdentityMismatch(
                f"checkpoint {checkpoint_id} belongs to chat {row['chat_id']}, not {expected_chat_id}"
            )
        if row["state"] == STATE_FAILED:
            conn.execute("ROLLBACK")
            return _row_to_record(row)
        if row["state"] != STATE_BUILDING:
            conn.execute("ROLLBACK")
            raise StateConflict(f"checkpoint {checkpoint_id} is {row['state']}, cannot fail it")

        now = _utcnow_iso()
        cur = conn.execute(
            "UPDATE context_checkpoints SET state=?, failed_at=?, failure_reason=? "
            "WHERE id=? AND state=?",
            (STATE_FAILED, now, reason, checkpoint_id, STATE_BUILDING),
        )
        if cur.rowcount != 1:
            conn.execute("ROLLBACK")
            raise StateConflict(f"checkpoint {checkpoint_id} was not BUILDING at commit time")
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
    return get_checkpoint(checkpoint_id)


def supersede_checkpoint(checkpoint_id: int, expected_chat_id: int) -> CheckpointRecord:
    """Advisory historical bookkeeping only (A3-PIN-02): READY -> SUPERSEDED.
    No lookup correctness depends on this ever being called or succeeding --
    get_latest_usable_checkpoint() selects the newest matching READY row
    regardless of whether older READY rows were ever superseded."""
    init_context_checkpoint_db()
    from core.db import connect
    conn = connect()
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM context_checkpoints WHERE id=?", (checkpoint_id,)
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            raise CheckpointNotFound(f"no context checkpoint with id {checkpoint_id}")
        if row["chat_id"] != expected_chat_id:
            conn.execute("ROLLBACK")
            raise IdentityMismatch(
                f"checkpoint {checkpoint_id} belongs to chat {row['chat_id']}, not {expected_chat_id}"
            )
        if row["state"] != STATE_READY:
            conn.execute("ROLLBACK")
            raise StateConflict(f"checkpoint {checkpoint_id} is {row['state']}, not READY")

        now = _utcnow_iso()
        cur = conn.execute(
            "UPDATE context_checkpoints SET state=?, superseded_at=? WHERE id=? AND state=?",
            (STATE_SUPERSEDED, now, checkpoint_id, STATE_READY),
        )
        if cur.rowcount != 1:
            conn.execute("ROLLBACK")
            raise StateConflict(f"checkpoint {checkpoint_id} was not READY at commit time")
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
    return get_checkpoint(checkpoint_id)


def get_checkpoint(checkpoint_id: int) -> CheckpointRecord:
    """Raises CheckpointNotFound -- never returns None, so a caller can't
    mistake "no such checkpoint" for a falsy-but-valid result."""
    init_context_checkpoint_db()
    from core.db import connect
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM context_checkpoints WHERE id=?", (checkpoint_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise CheckpointNotFound(f"no context checkpoint with id {checkpoint_id}")
    return _row_to_record(row)


def get_latest_usable_checkpoint(chat_id: int, *, context_skip: int = None) -> Optional[CheckpointRecord]:
    """Deterministic "latest usable" lookup for chat_id. Not ORDER BY
    created_at LIMIT 1 -- constrains to this chat, constrains to READY,
    validates payload version/integrity, and searches newest-first (id
    DESC, an AUTOINCREMENT PK, never an ambiguous timestamp tiebreak) for
    the first READY row whose frozen spine binding matches the CURRENT
    required spine (recomputed fresh via the unmodified A1/A2 kernel, not
    read off any row). A newer READY row that is stale is skipped in favor
    of an older READY row that still validates -- SUPERSEDED bookkeeping is
    never consulted and never required for this to be correct (A3-PIN-02).
    Returns None if nothing usable exists; never raises for "not found".

    context_skip=None (default) resolves the current skip via
    reconstruct_chat_context()'s own default (latest_manual_compaction_
    skip()), fail-closed: a read failure raises straight through rather
    than substituting skip=0 -- see the module docstring's "Fail-closed
    skip resolution" section. This function does NOT use
    resolve_context_skip()'s graceful degrade-to-zero, deliberately: a
    wrong guess here could make an actually-stale checkpoint compare as
    matching and be returned as falsely usable. Pass an explicit
    context_skip to check against a specific skip instead of the live one.

    IMPORTANT for callers (A5): this result is a point-in-time read. Two
    connections are used (one for the spine, one for the row scan; see
    "Lookup" in the module docstring for why that's safe against a partial
    write, but it is NOT held under any lock across the return to the
    caller). Nothing prevents the durable spine or this checkpoint's own
    state from moving in the interval between this call returning and
    whatever the caller does with the result. A consumer that intends an
    atomic swap (installing this payload as live context) MUST revalidate
    -- at minimum re-check the checkpoint's state/fingerprint, or use a
    generation/version check appropriate to its own swap boundary -- at
    the moment it actually commits to using this result, not merely trust
    that this function's answer is still true by the time it acts on it."""
    current_fingerprint = reconstruct_chat_context(
        chat_id, context_skip=context_skip
    ).durable_spine_fingerprint

    init_context_checkpoint_db()
    from core.db import connect
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM context_checkpoints WHERE chat_id=? AND state=? ORDER BY id DESC",
            (chat_id, STATE_READY),
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        if row["durable_spine_fingerprint"] != current_fingerprint:
            continue
        _, valid = _load_payload(row)
        if not valid:
            continue
        return _row_to_record(row)
    return None


def list_checkpoints(chat_id: int) -> list:
    """Every checkpoint ever recorded for chat_id, oldest first. Retention
    is unbounded by design in A3I -- no age/count sweep, no startup GC."""
    init_context_checkpoint_db()
    from core.db import connect
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM context_checkpoints WHERE chat_id=? ORDER BY id",
            (chat_id,),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_record(r) for r in rows]
