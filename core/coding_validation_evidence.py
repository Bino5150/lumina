"""
core/coding_validation_evidence.py -- CODING-06A3: durable Lumina-local
validation evidence.

Internal-only seam. No model-facing tool. A machine evidence record can
only be created from an already-completed, internally-classified
core.test_runner.TestRunResult combined with a live
core.coding_checkpoint_observation.LiveObservation -- never from the JSON
string rendered back to the model, never from a caller-supplied dict. See
tools/tests.py's run_tests for the only production call site.

Trust semantics
----------------
source == "lumina_local" means only: Lumina launched the structured
validation through its own managed execution path
(core.test_runner.run_pytest via core.process_manager) and bound the
resulting outcome to repository state Lumina itself measured
(core.coding_checkpoint_observation.capture_live_observation). It is not
cryptographic attestation, not proof of correctness, and confers no
commit/push/merge/checkpoint authority -- core.coding_checkpoint_observation
and tools/coding_checkpoint.py enforce that wall independently; this
module only ever answers "what did Lumina observe," never "what is Lumina
allowed to do next."

Storage
-------
One SQLite table, coding_validation_evidence, in the same WAL-mode
database every other module uses (core/db.py's connect()). Same
BEGIN IMMEDIATE / optimistic-serialization convention as
core/coding_checkpoint.py -- no additional in-process lock; SQLite's own
transaction serialization is the correctness boundary here too.

Identity and matching
----------------------
A record is keyed by (project_name, target_key, state_ref, scope_key).
state_ref is the exact CODING-05A2 state reference -- evidence is never
matched by branch, "recent," or nearest-commit; only byte-identical
current state ever qualifies. scope_key distinguishes the full suite
(selectors=[]) from a specific selector set via a stable SHA-256 hash of
the caller-normalized, sorted selector list -- never from prose.

Only a conclusive validation judgment is ever persisted: the caller
(tools/tests.py) must have already established checkpoint_bindable is
True (stable Project binding + stable repository state across the whole
run + no out-of-scope selector) AND the run's classified status must be
exactly "passed" or "failed" -- record_test_evidence() enforces the
second half itself, as a policy no-op rather than a validation error, so
every other A1B status (runner_busy, timed_out, cancelled, interrupted,
abnormal_exit, runner_error, collection_error, no_tests_collected) can
never become durable evidence even if a caller forgets to filter first.
"""
import hashlib
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence

EVIDENCE_SCHEMA_VERSION = 1
_KNOWN_EVIDENCE_SCHEMA_VERSIONS = {1}

SOURCE_LUMINA_LOCAL = "lumina_local"
CHECK_KIND_PYTEST = "pytest"

# Status -> outcome. Every other core.test_runner.TestRunResult status
# (runner_busy, timed_out, cancelled, interrupted, abnormal_exit,
# runner_error, collection_error, no_tests_collected) is intentionally
# absent -- record_test_evidence() treats an absent mapping as "not a
# conclusive judgment" and silently declines to persist anything.
ELIGIBLE_OUTCOMES = {"passed": "pass", "failed": "fail"}

MAX_SELECTORS_STORED = 8
MAX_SELECTOR_DISPLAY_CHARS = 200
RETENTION_PER_SCOPE = 10
MAX_LOOKUP_SCOPES = 8
EVIDENCE_ID_BYTES = 16  # secrets.token_hex(16) -> 32 lowercase hex chars


class EvidenceError(Exception):
    """Base class for every error this module raises."""


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    schema_version: int
    project_name: str
    target_key: str
    target_kind: str
    state_ref: str
    scope_key: str
    selectors: tuple
    selectors_truncated: bool
    check_kind: str
    outcome: str  # "pass" | "fail"
    exit_code: Optional[int]
    counts: dict
    duration_seconds: Optional[float]
    source: str
    created_at: str


def init_evidence_db():
    """Create the coding_validation_evidence table if it doesn't exist.
    Safe to call on every operation -- CREATE TABLE IF NOT EXISTS, no-op
    after the first call. Mirrors core.coding_checkpoint.init_checkpoint_db()
    exactly, including its narrow initial-WAL-transition SQLITE_BUSY retry."""
    from core.db import connect
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
        raise EvidenceError("could not initialize evidence database")
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS coding_validation_evidence (
                evidence_id          TEXT NOT NULL PRIMARY KEY,
                schema_version       INTEGER NOT NULL,
                project_name         TEXT NOT NULL,
                target_key           TEXT NOT NULL,
                target_kind          TEXT NOT NULL,
                state_ref            TEXT NOT NULL,
                scope_key            TEXT NOT NULL,
                selectors_json       TEXT NOT NULL,
                selectors_truncated  INTEGER NOT NULL,
                check_kind           TEXT NOT NULL,
                outcome              TEXT NOT NULL,
                exit_code            INTEGER,
                counts_json          TEXT NOT NULL,
                duration_seconds     REAL,
                source               TEXT NOT NULL,
                created_at           TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_coding_validation_evidence_lookup
            ON coding_validation_evidence
            (project_name, target_key, state_ref, scope_key, created_at)
        """)
        conn.commit()
    finally:
        conn.close()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def compute_scope_key(selectors: Sequence[str]) -> str:
    """Deterministic identity for a validation's scope. "full" for an empty
    selector list (the whole configured suite); otherwise a stable digest
    over the EXACT ordered selector vector as supplied to the structured
    runner, duplicates and all -- never derived from human-authored prose,
    so it can't be spoofed by wording.

    CODING-06A3 corrective 3: selectors are never sorted before hashing.
    ["A", "B"] and ["B", "A"] are deliberately different scopes -- pytest
    itself can behave differently under different orderings (fixture
    scoping, test interdependence, --randomly-style plugins), so a scope
    identity that collapsed order would let evidence recorded for one
    ordering silently "apply" to a run of a different ordering it never
    actually observed. ["A", "A"] is likewise a different scope from
    ["A"] for the same reason -- a duplicate is part of what was actually
    asked for, not noise to normalize away. json.dumps's default
    ensure_ascii=True keeps Unicode/space/metacharacter selectors
    deterministic without any human-legible normalization."""
    if not selectors:
        return "full"
    canonical = json.dumps(list(selectors), separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"selectors:{digest}"


def _bounded_selectors(selectors: Sequence[str]) -> tuple:
    selectors = list(selectors)
    shown = tuple(
        (s if len(s) <= MAX_SELECTOR_DISPLAY_CHARS else s[:MAX_SELECTOR_DISPLAY_CHARS])
        for s in selectors[:MAX_SELECTORS_STORED]
    )
    truncated = len(selectors) > MAX_SELECTORS_STORED or any(
        len(s) > MAX_SELECTOR_DISPLAY_CHARS for s in selectors
    )
    return shown, truncated


def record_test_evidence(
    *,
    project_name: str,
    target_key: str,
    target_kind: str,
    state_ref: str,
    selectors: Sequence[str],
    status: str,
    exit_code: Optional[int],
    counts: Optional[dict],
    duration_seconds: Optional[float],
) -> Optional[EvidenceRecord]:
    """Internal-only. Persists one machine evidence row IF AND ONLY IF
    status is a conclusive pytest judgment ("passed" or "failed"); every
    other status is a deliberate no-op, not an error -- most callers reach
    this after checkpoint_bindable is already known True and simply pass
    through whatever core.test_runner classified, so this function is the
    authoritative gate on which outcomes ever become durable evidence.

    Never raises on a storage failure -- returns None and prints a bounded
    diagnostic (matching core.persistence.save()'s convention) so an
    already-classified, real test result is never lost, blocked, or
    reclassified just because durable evidence recording failed.
    """
    outcome = ELIGIBLE_OUTCOMES.get(status)
    if outcome is None:
        return None
    if not isinstance(counts, dict):
        return None
    if not isinstance(project_name, str) or not project_name:
        return None
    if not isinstance(target_key, str) or not target_key:
        return None
    if not isinstance(state_ref, str) or not state_ref:
        return None

    scope_key = compute_scope_key(selectors)
    stored_selectors, truncated = _bounded_selectors(selectors)

    record = EvidenceRecord(
        evidence_id=secrets.token_hex(EVIDENCE_ID_BYTES),
        schema_version=EVIDENCE_SCHEMA_VERSION,
        project_name=project_name,
        target_key=target_key,
        target_kind=target_kind,
        state_ref=state_ref,
        scope_key=scope_key,
        selectors=stored_selectors,
        selectors_truncated=truncated,
        check_kind=CHECK_KIND_PYTEST,
        outcome=outcome,
        exit_code=exit_code if isinstance(exit_code, int) else None,
        counts=dict(counts),
        duration_seconds=(
            float(duration_seconds) if isinstance(duration_seconds, (int, float)) else None
        ),
        source=SOURCE_LUMINA_LOCAL,
        created_at=_utcnow_iso(),
    )

    try:
        _persist(record)
    except Exception as error:
        print(f"[CODING_VALIDATION_EVIDENCE] persistence failed: {error}", flush=True)
        return None
    return record


def _persist(record: EvidenceRecord):
    init_evidence_db()
    from core.db import connect
    conn = connect()
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """INSERT INTO coding_validation_evidence
               (evidence_id, schema_version, project_name, target_key, target_kind,
                state_ref, scope_key, selectors_json, selectors_truncated,
                check_kind, outcome, exit_code, counts_json, duration_seconds,
                source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.evidence_id, record.schema_version, record.project_name,
                record.target_key, record.target_kind, record.state_ref,
                record.scope_key,
                json.dumps(list(record.selectors), sort_keys=True, separators=(",", ":")),
                1 if record.selectors_truncated else 0,
                record.check_kind, record.outcome, record.exit_code,
                json.dumps(record.counts, sort_keys=True, separators=(",", ":")),
                record.duration_seconds, record.source, record.created_at,
            ),
        )
        # Bounded retention: keep only the newest RETENTION_PER_SCOPE rows
        # for this exact (project_name, target_key, scope_key). Pruning is
        # always oldest-first WITHIN that one scope, so the row just
        # inserted and the newest row of every OTHER scope for this target
        # are never at risk -- a newer FAIL can never be pruned back to an
        # older PASS becoming "current" again.
        conn.execute(
            """DELETE FROM coding_validation_evidence
               WHERE project_name = ? AND target_key = ? AND scope_key = ?
               AND evidence_id NOT IN (
                   SELECT evidence_id FROM coding_validation_evidence
                   WHERE project_name = ? AND target_key = ? AND scope_key = ?
                   ORDER BY created_at DESC, rowid DESC LIMIT ?
               )""",
            (record.project_name, record.target_key, record.scope_key,
             record.project_name, record.target_key, record.scope_key,
             RETENTION_PER_SCOPE),
        )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def _row_to_record(row) -> Optional[EvidenceRecord]:
    """Fail-closed: a malformed or unrecognized row is treated as absent
    (returns None) rather than raising -- a lookup caller must never be
    able to crash a checkpoint save/read because one historical evidence
    row became unreadable. Never "repairs" a bad row into trusted shape."""
    try:
        schema_version = row["schema_version"]
        if schema_version not in _KNOWN_EVIDENCE_SCHEMA_VERSIONS:
            return None
        if row["source"] != SOURCE_LUMINA_LOCAL:
            return None
        if row["check_kind"] != CHECK_KIND_PYTEST:
            return None
        if row["outcome"] not in {"pass", "fail"}:
            return None
        state_ref = row["state_ref"]
        if not isinstance(state_ref, str) or len(state_ref) != 64:
            return None
        target_key = row["target_key"]
        if not isinstance(target_key, str) or not target_key:
            return None
        selectors = json.loads(row["selectors_json"])
        if not isinstance(selectors, list) or not all(isinstance(s, str) for s in selectors):
            return None
        counts = json.loads(row["counts_json"])
        if not isinstance(counts, dict):
            return None
        exit_code = row["exit_code"]
        if exit_code is not None and not isinstance(exit_code, int):
            return None
        duration = row["duration_seconds"]
        if duration is not None and not isinstance(duration, (int, float)):
            return None
        scope_key = row["scope_key"]
        if not isinstance(scope_key, str) or not scope_key:
            return None
        return EvidenceRecord(
            evidence_id=row["evidence_id"],
            schema_version=schema_version,
            project_name=row["project_name"],
            target_key=target_key,
            target_kind=row["target_kind"],
            state_ref=state_ref,
            scope_key=scope_key,
            selectors=tuple(selectors),
            selectors_truncated=bool(row["selectors_truncated"]),
            check_kind=row["check_kind"],
            outcome=row["outcome"],
            exit_code=exit_code,
            counts=counts,
            duration_seconds=duration,
            source=row["source"],
            created_at=row["created_at"],
        )
    except Exception:
        return None


def lookup_compatible_evidence(
    *, project_name: str, target_key: str, state_ref: str
) -> tuple:
    """Read-only. Returns the newest compatible EvidenceRecord per distinct
    scope_key for this EXACT (project_name, target_key, state_ref) -- never
    a fuzzy, nearest, or "recent" match. Malformed rows are silently
    skipped (fail closed), never raised through to the caller. Bounded to
    MAX_LOOKUP_SCOPES distinct scopes."""
    if not project_name or not target_key or not state_ref:
        return ()
    init_evidence_db()
    from core.db import connect
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT * FROM coding_validation_evidence
               WHERE project_name = ? AND target_key = ? AND state_ref = ?
               ORDER BY created_at DESC, rowid DESC""",
            (project_name, target_key, state_ref),
        ).fetchall()
    finally:
        conn.close()

    seen_scopes = set()
    results = []
    for row in rows:
        record = _row_to_record(row)
        if record is None or record.scope_key in seen_scopes:
            continue
        seen_scopes.add(record.scope_key)
        results.append(record)
        if len(results) >= MAX_LOOKUP_SCOPES:
            break
    return tuple(results)


def _reset_for_tests():
    """Test-only convenience -- no-op placeholder kept symmetrical with
    other core modules' _reset_for_tests(); this module holds no
    in-process state (isolation comes entirely from monkeypatching
    config.DB_PATH per test, same as core.coding_checkpoint)."""
