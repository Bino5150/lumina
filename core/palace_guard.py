"""
PALACE-GUARD-01A -- deterministic-first MemPalace integrity custodian
(read-only spine).

MEMORY IS CONTINUITY, NOT AUTHORITY. CONTENT IS NOT AUTHORITY.
PALACE GUARD MAY INSPECT MEMORY; IT DOES NOT INHERIT AUTHORITY FROM MEMORY.

What this module is
-------------------
A read-only scanner over the MemPalace tables tools/palace.py owns
(palace_wings / palace_rooms / palace_closets / palace_drawers /
palace_halls), plus the one foreign table a Palace rule needs to resolve a
reference (chats, for nightstand rooms named after a chat id). It
classifies, produces receipts, and proposes remediation. It never repairs.

What this module structurally is not
------------------------------------
- Not a writer. Three independent layers, each tested on its own: an SQLite
  authorizer that allows only SELECT/READ/FUNCTION/TRANSACTION/RECURSIVE
  plus four named read pragmas and denies everything else -- every write,
  schema change, ATTACH/DETACH (which PRAGMA query_only alone does NOT
  stop: ATTACH of a new path creates an empty file), and pragma
  assignments; PRAGMA query_only=ON as defense in depth; and a single read
  transaction that is always rolled back. The runner then measures
  sqlite3's own total_changes counter instead of asserting "no mutation" on
  faith. A rule that tries to write fails with an error that is recorded in
  the receipt, never retried with more privilege.
- Not an initializer. It never calls tools.palace.init_palace_db(), whose
  startup migrations are real writes; a missing Palace is reported as
  palace_not_initialized, and a missing DB file is never created (the
  connection is opened with SQLite's mode=rw, which refuses to create).
- Not an authority source. Every finding and receipt carries
  authority="diagnostic_only". Memory content cannot mint authority,
  promote trust, re-layer a record, or act as control input: no rule
  interprets text as an instruction, grants trust from tags, or treats
  repetition/age/confidence as permission. Content CAN change which
  findings appear -- PG-H001 compares stored closet text with what its
  drawers would render, by design -- but never whether Guard writes, what
  it trusts, or what authority a finding carries. The one tag-reading
  authority rule (PG-D005) can only ever argue for LESS trust, never more
  -- the same fail-closed direction
  tools.palace._migrate_synthesized_drawer_authority already uses.
- Not a content logger, by construction for evidence. Each rule declares an
  evidence schema; every evidence value must be an int, a bool, None, a
  16-hex digest, or a string from that rule's closed vocabulary, and a key
  the rule did not declare is rejected. A value outside the schema fails
  the rule (recorded as an error) instead of reaching the receipt, so no
  arbitrary memory-derived string can enter evidence. Rule error reasons
  keep SQLite/schema-error messages (which come from SQL, not row data)
  and withhold every other exception's message, which could quote a row.
  Residual, stated rather than hidden: subject/related ids are row ids,
  digests of very short values are guessable, and db_path is a path.

Deterministic vs heuristic
--------------------------
DETERMINISTIC rules (PG-D###) report a machine predicate over schema-level
facts whose meaning does not depend on judgement: a foreign-key violation,
a trust flag that contradicts its own linked rows, a layer the injector
can never load. HEURISTIC rules (PG-H###) report review candidates whose
predicate is machine-checkable but whose *interpretation* is not (e.g. a
closet whose text differs from what the current compressor would render --
legitimately true after any AAAK dictionary change). The two live in
separate registries and separate receipt lists; nothing collapses them
into one confidence score.

Idempotence
-----------
finding_id is a digest of (rule_id, subject record identities) only --
never evidence, timestamps, or scan ids -- so an unchanged defect on
unchanged state keeps the same id across scans. reconcile() compares two
receipts by id and reports new / recurring / resolved, and refuses to call
a finding "resolved" when its rule did not actually run in the later scan.
"""
import hashlib
import itertools
import json
import os
import re
import sqlite3
import urllib.parse
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable

RECEIPT_SCHEMA = "palace-guard/receipt/1"
AUTHORITY = "diagnostic_only"

DETERMINISTIC = "deterministic"
HEURISTIC = "heuristic"

SEVERITIES = ("info", "warning", "high")

PALACE_TABLES = (
    "palace_wings", "palace_rooms", "palace_closets",
    "palace_drawers", "palace_halls",
)

# build_context_block() iterates exactly these layers; anything else is
# stored but never injected (tools/palace.py).
INJECTED_LAYERS = (0, 1, 2)
ALWAYS_INJECTED_LAYERS = (0, 1)
DOCUMENTED_LAYERS = (0, 1, 2, 3)

_MAX_VOCAB_STR = 80
_MAX_RELATED = 50

# Evidence value types a rule may declare. A spec is one of these, a
# frozenset of allowed strings (closed vocabulary), or a tuple of
# alternatives.
INT = "int"
BOOL = "bool"
DIGEST = "digest"
NONE = "none"
_DIGEST_RE = re.compile(r"[0-9a-f]{16}")
# What _scalar_repr() can return for a non-integer column value.
_TYPE_PLACEHOLDERS = frozenset({"<str>", "<bytes>", "<float>", "<other>"})


# ── Model ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RecordRef:
    table: str
    row_id: int

    def to_dict(self) -> dict:
        return {"table": self.table, "id": self.row_id}


@dataclass(frozen=True)
class Hit:
    """One rule firing, before the runner turns it into a Finding."""
    subject: tuple
    evidence: dict
    related: tuple = ()
    severity: str = None


@dataclass(frozen=True)
class Rule:
    rule_id: str
    version: int
    kind: str
    category: str
    severity: str
    title: str
    requires: dict  # table -> frozenset of required columns
    check: Callable[[sqlite3.Connection], Iterable[Hit]]
    remediation: str
    uncertainty: str
    rationale: str = ""
    evidence: dict = field(default_factory=dict)  # key -> spec (see INT/BOOL/...)

    def describe(self) -> dict:
        return {
            "rule_id": self.rule_id, "version": self.version, "kind": self.kind,
            "category": self.category, "severity": self.severity,
            "title": self.title, "remediation": self.remediation,
            "uncertainty": self.uncertainty, "rationale": self.rationale,
        }


def finding_id(rule_id: str, subject: Iterable[RecordRef]) -> str:
    """Stable identity: rule + subject records, nothing that varies per scan."""
    key = json.dumps(
        {"rule": rule_id, "subject": sorted([r.table, int(r.row_id)] for r in subject)},
        sort_keys=True, separators=(",", ":"),
    )
    return "pgf-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]


def _digest(text) -> str:
    """Short content digest for evidence -- lets a reviewer confirm two
    values differ (or match) without the receipt carrying the text."""
    if text is None:
        return None
    if isinstance(text, bytes):
        data = text
    else:
        data = str(text).encode("utf-8", "surrogatepass")
    return hashlib.sha256(data).hexdigest()[:16]


class EvidenceSchemaError(ValueError):
    """A hit's evidence fell outside its rule's declared schema. Messages
    name only declared keys and spec kinds -- never the offending value."""


def _spec_alternatives(spec) -> tuple:
    return spec if isinstance(spec, tuple) else (spec,)


def _matches(value, spec) -> bool:
    for alt in _spec_alternatives(spec):
        if alt == INT and isinstance(value, int) and not isinstance(value, bool):
            return True
        if alt == BOOL and isinstance(value, bool):
            return True
        if alt == NONE and value is None:
            return True
        if alt == DIGEST and isinstance(value, str) and _DIGEST_RE.fullmatch(value):
            return True
        if isinstance(alt, frozenset) and isinstance(value, str) and value in alt:
            return True
    return False


def _validate_evidence(schema: dict, evidence: dict) -> dict:
    """Structural content boundary: evidence carries only declared keys whose
    values are ints, bools, None, 16-hex digests, or members of the rule's
    closed string vocabulary. Enforced at the runner, not left to each
    rule's good manners."""
    if not isinstance(evidence, dict):
        raise EvidenceSchemaError("evidence must be a dict")
    for k, v in evidence.items():
        spec = schema.get(k) if isinstance(k, str) else None
        if spec is None:
            raise EvidenceSchemaError("undeclared evidence key")  # key may be data
        if not _matches(v, spec):
            raise EvidenceSchemaError(f"evidence[{k!r}] outside its declared schema")
    return dict(evidence)


def _validate_schema_spec(rule_id: str, key, spec) -> None:
    if not isinstance(key, str):
        raise ValueError(f"{rule_id}: evidence keys must be str")
    for alt in _spec_alternatives(spec):
        if alt in (INT, BOOL, DIGEST, NONE):
            continue
        if isinstance(alt, frozenset) and alt and all(
            isinstance(s, str) and 0 < len(s) <= _MAX_VOCAB_STR and "\n" not in s
            for s in alt
        ):
            continue
        raise ValueError(f"{rule_id}: bad evidence spec for {key!r}")


def _joined_subsets(names) -> frozenset:
    """Every sorted, comma-joined non-empty subset of a fixed name set."""
    names = sorted(names)
    return frozenset(
        ",".join(combo)
        for r in range(1, len(names) + 1)
        for combo in itertools.combinations(names, r)
    )


def _error_reason(e: Exception) -> str:
    """SQLite and evidence-schema errors describe SQL/schema, not row data;
    anything else could quote a row (e.g. ValueError on int('<content>')),
    so its message is withheld."""
    if isinstance(e, (sqlite3.Error, EvidenceSchemaError)):
        return f"{type(e).__name__}: {str(e)[:200]}"
    return f"{type(e).__name__} (message withheld: may quote row data)"


# ── Rule helpers ──────────────────────────────────────────────────────────────

def _parse_tags(raw):
    """Return (tags_list, defect). defect is None when tags is a JSON list of
    strings (or NULL, which every reader already treats as [])."""
    if raw is None:
        return [], None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None, "invalid_json"
    if not isinstance(value, list):
        return None, "not_a_list"
    if not all(isinstance(t, str) for t in value):
        return None, "non_string_element"
    return value, None


def _scalar_repr(value):
    """Evidence-safe rendering of a column value that should have been an int:
    the int itself, None, or a type placeholder from _TYPE_PLACEHOLDERS."""
    if value is None or (isinstance(value, int) and not isinstance(value, bool)):
        return value
    placeholder = f"<{type(value).__name__}>"
    return placeholder if placeholder in _TYPE_PLACEHOLDERS else "<other>"


def _related_ids(conn, sql, params=()) -> tuple:
    rows = conn.execute(sql, params).fetchall()
    return tuple(RecordRef(r[0], r[1]) for r in rows[:_MAX_RELATED])


# ── Deterministic rules ───────────────────────────────────────────────────────

_FK_CONSEQUENCE = {
    "palace_drawers": "unreachable_by_palace_recall",
    "palace_closets": "never_injected_by_build_context_block",
    "palace_rooms": "contents_unreachable",
}
_FK_PARENTS = frozenset({"palace_wings", "palace_rooms", "palace_closets"})

# Kept literal (not imported) so the evidence vocabulary is closed at import
# time; tests pin it to tools.palace._SYNTHESIZED_MEMORY_TAGS so it can't drift.
_SYNTHESIZED_TAG_VOCAB = frozenset({"dream-sweep", "auto-compaction", "manual-compaction"})

# Legacy-hall provenance epoch. palace_halls.untrusted arrived in
# CASTLE-WALLS-REPAIR-01 (e9ab412, 2026-09-20T07:46:03Z) with DEFAULT 0, so
# every hall written before any build carrying that commit inherited
# "trusted" from the schema default, not from its writer. Hall created_at is
# naive local time (datetime.now().isoformat()); the earliest local clock
# reading of that instant anywhere is UTC-12, so a naive created_at before
# the value below is provably pre-column in every timezone. Later rows are
# not flagged even if the owner upgraded late (documented false negative).
HALL_PROVENANCE_EPOCH_UTC = datetime(2026, 9, 20, 7, 46, 3, tzinfo=timezone.utc)
HALL_PROVENANCE_EPOCH_NAIVE_FLOOR = datetime(2026, 9, 19, 19, 46, 3)
_LAYER_SPEC = (INT, NONE, _TYPE_PLACEHOLDERS)


def _check_fk_violations(conn):
    by_row = {}
    for table in ("palace_rooms", "palace_closets", "palace_drawers"):
        for row in conn.execute(f"PRAGMA foreign_key_check({table})").fetchall():
            parent = row[2] if row[2] in _FK_PARENTS else "other"
            by_row.setdefault((row[0], row[1]), set()).add(parent)
    for (table, rowid), parents in sorted(by_row.items()):
        yield Hit(
            subject=(RecordRef(table, rowid),),
            evidence={
                "missing_parent_tables": ",".join(sorted(parents)),
                "consequence": _FK_CONSEQUENCE.get(table, "unknown"),
            },
        )


def _check_closet_trust_flag_understated(conn):
    rows = conn.execute("""
        SELECT c.id AS closet_id, c.layer AS layer,
               SUM(CASE WHEN d.untrusted != 0 THEN 1 ELSE 0 END) AS untrusted_drawers,
               COUNT(d.id) AS linked_drawers
        FROM palace_closets c
        JOIN palace_drawers d ON d.closet_id = c.id
        WHERE c.ever_had_untrusted_merge = 0
        GROUP BY c.id
        HAVING untrusted_drawers > 0
        ORDER BY c.id
    """).fetchall()
    for r in rows:
        yield Hit(
            subject=(RecordRef("palace_closets", r["closet_id"]),),
            related=_related_ids(
                conn,
                "SELECT 'palace_drawers', id FROM palace_drawers "
                "WHERE closet_id=? AND untrusted != 0 ORDER BY id",
                (r["closet_id"],),
            ),
            evidence={
                "layer": _scalar_repr(r["layer"]),
                "closet_ever_had_untrusted_merge": 0,
                "untrusted_linked_drawers": r["untrusted_drawers"],
                "linked_drawers": r["linked_drawers"],
            },
        )


def _check_lower_trust_in_always_injected_layer(conn):
    closets = conn.execute("""
        SELECT c.id AS closet_id, c.layer AS layer,
               c.ever_had_untrusted_merge AS flag,
               (SELECT COUNT(*) FROM palace_drawers d
                 WHERE d.closet_id = c.id AND d.untrusted != 0) AS untrusted_drawers,
               (SELECT COUNT(*) FROM palace_drawers d
                 WHERE d.closet_id = c.id) AS linked_drawers
        FROM palace_closets c
        WHERE c.layer IN (0, 1)
        ORDER BY c.id
    """).fetchall()
    for r in closets:
        if not (r["flag"] or r["untrusted_drawers"]):
            continue
        # L0/L1 writes never merge (palace_store INSERTs one closet per
        # write below layer 2), and palace_store/_rebuild_closet_from_drawers
        # set the flag and the tag_untrusted() framing together. So at
        # L0/L1, flag=0 with a lower-trust drawer means the text is injected
        # on every owner turn WITHOUT lower-trust framing (legacy rows);
        # flag=1 means it was framed when written.
        unframed = not r["flag"]
        yield Hit(
            subject=(RecordRef("palace_closets", r["closet_id"]),),
            evidence={
                "layer": r["layer"],
                "framing": "unframed_legacy" if unframed else "framed_at_write",
                "closet_ever_had_untrusted_merge": 0 if unframed else 1,
                "untrusted_linked_drawers": r["untrusted_drawers"],
                "linked_drawers": r["linked_drawers"],
            },
            severity="high" if unframed else "warning",
        )
    halls = conn.execute("""
        SELECT id, layer FROM palace_halls
        WHERE layer IN (0, 1) AND untrusted != 0
        ORDER BY id
    """).fetchall()
    for r in halls:
        # palace_store_hall() frames the row's text whenever untrusted is set.
        yield Hit(
            subject=(RecordRef("palace_halls", r["id"]),),
            evidence={"layer": r["layer"], "framing": "framed_at_write",
                      "hall_untrusted": 1},
        )


def _check_drawer_tags_malformed(conn):
    rows = conn.execute(
        "SELECT id, tags, typeof(tags) AS sqlite_type FROM palace_drawers "
        "WHERE tags IS NOT NULL ORDER BY id"
    ).fetchall()
    for r in rows:
        _, defect = _parse_tags(r["tags"])
        if defect is None:
            continue
        yield Hit(
            subject=(RecordRef("palace_drawers", r["id"]),),
            evidence={"defect": defect, "sqlite_type": r["sqlite_type"]},
        )


def _synthesized_tags():
    from tools.palace import _SYNTHESIZED_MEMORY_TAGS
    return _SYNTHESIZED_MEMORY_TAGS


def _check_synthesized_drawer_marked_trusted(conn):
    synthesized = _synthesized_tags()
    rows = conn.execute(
        "SELECT id, tags FROM palace_drawers WHERE untrusted = 0 ORDER BY id"
    ).fetchall()
    for r in rows:
        tags, defect = _parse_tags(r["tags"])
        if defect is not None:
            continue  # PG-D004 reports the unparseable row; no guessing here
        matched = synthesized & set(tags)
        if not matched:
            continue
        yield Hit(
            subject=(RecordRef("palace_drawers", r["id"]),),
            evidence={"synthesized_tags": ",".join(sorted(matched)), "untrusted": 0},
        )


def _check_layer_not_injectable(conn):
    for table in ("palace_closets", "palace_halls"):
        rows = conn.execute(
            f"SELECT id, layer FROM {table} WHERE layer NOT IN (0, 1, 2) "
            "OR typeof(layer) != 'integer' ORDER BY id"
        ).fetchall()
        for r in rows:
            layer = r["layer"]
            documented = (
                isinstance(layer, int) and not isinstance(layer, bool)
                and layer in DOCUMENTED_LAYERS
            )
            yield Hit(
                subject=(RecordRef(table, r["id"]),),
                evidence={
                    "layer": _scalar_repr(layer),
                    "documented_layer": documented,
                },
                severity="info" if documented else "warning",
            )


def _check_nightstand_room_without_chat(conn):
    chats = {r[0] for r in conn.execute("SELECT id FROM chats").fetchall()}
    rooms = conn.execute("""
        SELECT r.id AS room_id, r.name AS name
        FROM palace_rooms r
        JOIN palace_wings w ON r.wing_id = w.id
        WHERE w.name = 'nightstand'
        ORDER BY r.id
    """).fetchall()
    for r in rooms:
        name = r["name"]
        closets = conn.execute(
            "SELECT COUNT(*) FROM palace_closets WHERE room_id=?", (r["room_id"],)
        ).fetchone()[0]
        drawers = conn.execute(
            "SELECT COUNT(*) FROM palace_drawers WHERE room_id=?", (r["room_id"],)
        ).fetchone()[0]
        if isinstance(name, str) and name.isascii() and name.isdecimal():
            chat_id = int(name)
            if chat_id in chats:
                continue
            evidence = {"defect": "chat_missing", "chat_id": chat_id}
        else:
            evidence = {"defect": "room_name_not_a_chat_id",
                        "room_name_chars": len(name) if isinstance(name, str) else None}
        evidence.update({"closets": closets, "drawers": drawers})
        yield Hit(subject=(RecordRef("palace_rooms", r["room_id"]),), evidence=evidence)


def _hall_predates_provenance(created_at):
    """True / False, or None when created_at can't be parsed."""
    if not isinstance(created_at, str):
        return None
    try:
        ts = datetime.fromisoformat(created_at)
    except ValueError:
        return None
    if ts.tzinfo is not None:
        return ts.astimezone(timezone.utc) < HALL_PROVENANCE_EPOCH_UTC
    return ts < HALL_PROVENANCE_EPOCH_NAIVE_FLOOR


def _check_legacy_hall_presumed_trusted(conn):
    rows = conn.execute(
        "SELECT id, layer, created_at FROM palace_halls WHERE untrusted = 0 ORDER BY id"
    ).fetchall()
    for r in rows:
        legacy = _hall_predates_provenance(r["created_at"])
        if legacy is False:
            continue  # written by a build that set the bit explicitly
        layer = r["layer"]
        always_injected = (
            isinstance(layer, int) and not isinstance(layer, bool)
            and layer in ALWAYS_INJECTED_LAYERS
        )
        yield Hit(
            subject=(RecordRef("palace_halls", r["id"]),),
            evidence={
                "layer": _scalar_repr(layer),
                "hall_untrusted": 0,
                "provenance": ("legacy_unknown_presumed_trusted" if legacy
                               else "created_at_unparseable_presumed_trusted"),
            },
            severity="high" if always_injected else "warning",
        )


# ── Heuristic rules ───────────────────────────────────────────────────────────

def _check_closet_render_drift(conn):
    from tools.palace import render_closet_text
    closets = conn.execute("""
        SELECT c.id AS closet_id, c.compressed AS compressed,
               c.ever_had_untrusted_merge AS flag,
               w.name AS wing, r.name AS room
        FROM palace_closets c
        JOIN palace_rooms r ON c.room_id = r.id
        JOIN palace_wings w ON r.wing_id = w.id
        ORDER BY c.id
    """).fetchall()
    for c in closets:
        drawers = conn.execute(
            "SELECT content, untrusted FROM palace_drawers "
            "WHERE closet_id=? ORDER BY created_at, id",
            (c["closet_id"],),
        ).fetchall()
        if not drawers:
            continue  # nothing to render from (e.g. the seeded L0 identity closet)
        expected = render_closet_text(f"{c['wing']}.{c['room']}", drawers)
        stored = c["compressed"]
        if stored == expected:
            continue
        yield Hit(
            subject=(RecordRef("palace_closets", c["closet_id"]),),
            evidence={
                "stored_sha256_16": _digest(stored),
                "expected_sha256_16": _digest(expected),
                "stored_chars": len(stored) if isinstance(stored, str) else None,
                "expected_chars": len(expected),
                "linked_drawers": len(drawers),
                "closet_ever_had_untrusted_merge": 1 if c["flag"] else 0,
            },
        )


# ── Registry ──────────────────────────────────────────────────────────────────

_DRAWERS_TRUST = frozenset({"id", "closet_id", "untrusted", "tags"})

DETERMINISTIC_RULES = (
    Rule(
        rule_id="PG-D001", version=1, kind=DETERMINISTIC,
        category="orphan", severity="warning",
        title="Palace row references a parent row that does not exist",
        requires={
            "palace_wings": frozenset({"id"}),
            "palace_rooms": frozenset({"id", "wing_id"}),
            "palace_closets": frozenset({"id", "room_id"}),
            "palace_drawers": frozenset({"id", "room_id", "closet_id"}),
        },
        check=_check_fk_violations,
        remediation=(
            "Owner review. Orphans are invisible to palace_recall / "
            "build_context_block (both INNER JOIN through rooms and wings), so "
            "the content is effectively lost but still on disk. Options: "
            "re-home under an existing room, or delete. Guard does neither."
        ),
        uncertainty=(
            "Every core.db connection enables foreign keys, so an orphan implies "
            "a legacy build, an FK-off connection, a restore, or an external edit; "
            "the rule cannot tell which."
        ),
        rationale="SQLite PRAGMA foreign_key_check against the schema's own FK declarations.",
        evidence={
            "missing_parent_tables": _joined_subsets(_FK_PARENTS | {"other"}),
            "consequence": frozenset(_FK_CONSEQUENCE.values()) | {"unknown"},
        },
    ),
    Rule(
        rule_id="PG-D002", version=1, kind=DETERMINISTIC,
        category="provenance", severity="warning",
        title="Closet renders as never-untrusted while a linked drawer is lower-trust",
        requires={
            "palace_closets": frozenset({"id", "layer", "ever_had_untrusted_merge"}),
            "palace_drawers": _DRAWERS_TRUST,
        },
        check=_check_closet_trust_flag_understated,
        remediation=(
            "Owner review. Candidate bounded repair (NOT enabled in 01A): "
            "tools.palace._rebuild_closet_from_drawers(closet_id) re-renders the "
            "closet from its drawers, framing each lower-trust segment and "
            "setting the flag -- this changes how Lumina reads these memories, "
            "so it is an owner decision, not a janitorial one."
        ),
        uncertainty=(
            "A drawer's untrusted=1 may be the CANNON-08 fail-closed migration "
            "default for a legacy row of unknowable origin, not evidence that "
            "the content came from an untrusted source."
        ),
        rationale=(
            "tools.palace invariant: ever_had_untrusted_merge is set whenever an "
            "untrusted segment is merged and recomputed as any(drawer.untrusted) "
            "on rebuild; a 0 with an untrusted linked drawer contradicts both."
        ),
        evidence={
            "layer": _LAYER_SPEC, "closet_ever_had_untrusted_merge": INT,
            "untrusted_linked_drawers": INT, "linked_drawers": INT,
        },
    ),
    Rule(
        rule_id="PG-D003", version=1, kind=DETERMINISTIC,
        category="promotion", severity="warning",
        title="Lower-trust record occupies an always-injected layer (L0/L1)",
        requires={
            "palace_closets": frozenset({"id", "layer", "ever_had_untrusted_merge"}),
            "palace_drawers": _DRAWERS_TRUST,
            "palace_halls": frozenset({"id", "layer", "untrusted"}),
        },
        check=_check_lower_trust_in_always_injected_layer,
        remediation=(
            "Owner review: keep, move to L2, or remove. L0/L1 are injected on "
            "every owner turn and are exempt from MEMORY_INJECT_LIMIT and decay "
            "ordering. severity=high when the record is injected WITHOUT its "
            "lower-trust framing (framing=unframed_legacy): a provenance/"
            "integrity risk -- lower-trust text presented like owner-voice "
            "memory in the always-present tier -- not an authority bypass (no "
            "tool/permission decision reads Palace text). Guard never re-layers, "
            "re-frames, or demotes a record."
        ),
        uncertainty=(
            "palace_remember lets the model choose layer 0/1 directly and always "
            "writes lower-trust (framed_at_write), so this may be Lumina's own "
            "deliberate note; unframed_legacy rows may be owner-authored text "
            "whose drawer was fail-closed by the CANNON-08 migration."
        ),
        rationale=(
            "Structural fields only: layer in (0,1) plus the closet flag / drawer "
            "bit / hall bit. Framing is inferred from the write-path invariant "
            "that the flag and tag_untrusted() framing are set together and L0/L1 "
            "never merge; PG-H001 independently catches rendered text that drifted."
        ),
        evidence={
            "layer": INT, "framing": frozenset({"unframed_legacy", "framed_at_write"}),
            "closet_ever_had_untrusted_merge": INT, "untrusted_linked_drawers": INT,
            "linked_drawers": INT, "hall_untrusted": INT,
        },
    ),
    Rule(
        rule_id="PG-D004", version=1, kind=DETERMINISTIC,
        category="malformed", severity="warning",
        title="Drawer tags are not a JSON list of strings",
        requires={"palace_drawers": frozenset({"id", "tags"})},
        check=_check_drawer_tags_malformed,
        remediation=(
            "Owner review. palace_recall() json.loads() every returned row's "
            "tags, so invalid JSON breaks any recall that matches this drawer; "
            "manual-compaction context-skip readers silently skip it. Guard "
            "does not rewrite tags."
        ),
        uncertainty="The rule cannot tell which writer produced the malformed value.",
        rationale="Every Palace writer stores json.dumps(list[str]); every reader assumes that shape.",
        evidence={
            "defect": frozenset({"invalid_json", "not_a_list", "non_string_element"}),
            "sqlite_type": frozenset({"text", "integer", "real", "blob"}),
        },
    ),
    Rule(
        rule_id="PG-D005", version=1, kind=DETERMINISTIC,
        category="authority", severity="high",
        title="Model-synthesized drawer (dream/compaction) is marked trusted",
        requires={"palace_drawers": frozenset({"id", "tags", "untrusted"})},
        check=_check_synthesized_drawer_marked_trusted,
        remediation=(
            "Owner review; the next init_palace_db() startup migration "
            "(_migrate_synthesized_drawer_authority) already fails these closed. "
            "A finding here means that migration has not run on this DB, or a "
            "writer regressed. Guard does not flip the bit."
        ),
        uncertainty=(
            "Tags are write-path identifiers here and are read ONLY in the "
            "fail-closed direction; a hostile tag can at most cause a finding "
            "arguing for less trust, never more."
        ),
        rationale="REDDIT-INGRESS-AUTHORITY-01: every model-authored durable summary is lower-trust.",
        evidence={"synthesized_tags": _joined_subsets(_SYNTHESIZED_TAG_VOCAB), "untrusted": INT},
    ),
    Rule(
        rule_id="PG-D006", version=1, kind=DETERMINISTIC,
        category="reachability", severity="info",
        title="Closet/hall layer is never injected into the system prompt",
        requires={
            "palace_closets": frozenset({"id", "layer"}),
            "palace_halls": frozenset({"id", "layer"}),
        },
        check=_check_layer_not_injectable,
        remediation=(
            "Owner review. build_context_block() only loads layers 0-2; this row's "
            "text is stored but never rendered (a closet's drawers stay searchable "
            "via palace_recall). Layer 3 is documented as 'deep' (info); any other "
            "value is outside the documented enum (warning)."
        ),
        uncertainty="Layer 3 closets may be intentional deep storage.",
        rationale="build_context_block() iterates layers [0, 1, 2] only.",
        evidence={"layer": _LAYER_SPEC, "documented_layer": BOOL},
    ),
    Rule(
        rule_id="PG-D007", version=1, kind=DETERMINISTIC,
        category="stale_reference", severity="info",
        title="Nightstand room does not correspond to an existing chat",
        requires={
            "palace_wings": frozenset({"id", "name"}),
            "palace_rooms": frozenset({"id", "wing_id", "name"}),
            "palace_closets": frozenset({"id", "room_id"}),
            "palace_drawers": frozenset({"id", "room_id"}),
            "chats": frozenset({"id"}),
        },
        check=_check_nightstand_room_without_chat,
        remediation=(
            "Owner review. Every nightstand writer uses room=str(chat_id); "
            "delete_chat() removes chat rows but not the chat's nightstand room, "
            "so a deleted chat's dream/compaction summaries keep competing for "
            "L2 injection. Guard does not delete them."
        ),
        uncertainty=(
            "Keeping a deleted chat's summaries may be intended continuity; "
            "chat ids are AUTOINCREMENT, so a stale room can never re-pin to a "
            "new chat."
        ),
        rationale="Nightstand room names are chat ids by construction (dreaming, auto/manual compaction).",
        evidence={
            "defect": frozenset({"chat_missing", "room_name_not_a_chat_id"}),
            "chat_id": INT, "room_name_chars": (INT, NONE),
            "closets": INT, "drawers": INT,
        },
    ),
    Rule(
        rule_id="PG-D008", version=1, kind=DETERMINISTIC,
        category="provenance", severity="warning",
        title="Legacy hall of unknown provenance is represented as trusted",
        requires={"palace_halls": frozenset({"id", "layer", "created_at", "untrusted"})},
        check=_check_legacy_hall_presumed_trusted,
        remediation=(
            "Owner review; remediation belongs to the legacy Palace provenance "
            "campaign, not Guard. Halls have no verbatim drawer to rebuild from, "
            "so the candidate repair is fail-closing the bit (untrusted=1, which "
            "re-frames the rendered text) the way CANNON-08 did for drawers. "
            "severity=high when the hall sits in always-injected L0/L1."
        ),
        uncertainty=(
            "untrusted=0 on these rows is the schema DEFAULT the column was "
            "added with (e9ab412), not a decision by the writer; the true source "
            "is unknowable and may well have been the owner. Rows created after "
            "the epoch are not flagged even if this DB upgraded late (false "
            "negatives by design); an unparseable created_at is flagged as "
            "provenance-unknown."
        ),
        rationale=(
            "palace_halls.untrusted was added with DEFAULT 0 in CASTLE-WALLS-"
            "REPAIR-01 (2026-09-20T07:46:03Z), while drawers were fail-closed to "
            "DEFAULT 1 (C7, c539162); created_at before the UTC-12 local reading "
            "of that instant proves a row predates every build with the column."
        ),
        evidence={
            "layer": _LAYER_SPEC, "hall_untrusted": INT,
            "provenance": frozenset({"legacy_unknown_presumed_trusted",
                                     "created_at_unparseable_presumed_trusted"}),
        },
    ),
)

HEURISTIC_RULES = (
    Rule(
        rule_id="PG-H001", version=1, kind=HEURISTIC,
        category="drift", severity="info",
        title="Closet text differs from what its drawers would render today",
        requires={
            "palace_wings": frozenset({"id", "name"}),
            "palace_rooms": frozenset({"id", "wing_id", "name"}),
            "palace_closets": frozenset({"id", "room_id", "compressed", "ever_had_untrusted_merge"}),
            "palace_drawers": frozenset({"id", "closet_id", "content", "untrusted", "created_at"}),
        },
        check=_check_closet_render_drift,
        remediation=(
            "Review candidate only. Compare the closet with its drawers; if the "
            "rendered text carries content with no verbatim backing, treat it as "
            "a provenance question. Guard does not rebuild closets."
        ),
        uncertainty=(
            "HEURISTIC: the comparison target is the CURRENT compressor. AAAK "
            "dictionary/identifier-masking changes (e.g. UI-TRUST-01) and "
            "pre-provenance legacy writes legitimately produce mismatches, so a "
            "mismatch alone shows neither corruption nor tampering."
        ),
        rationale="tools.palace.render_closet_text() is the shared renderer used by closet rebuilds.",
        evidence={
            "stored_sha256_16": (DIGEST, NONE), "expected_sha256_16": DIGEST,
            "stored_chars": (INT, NONE), "expected_chars": INT,
            "linked_drawers": INT, "closet_ever_had_untrusted_merge": INT,
        },
    ),
)


def _validate_registry(deterministic, heuristic) -> None:
    seen = set()
    for rules, kind, prefix in (
        (deterministic, DETERMINISTIC, "PG-D"),
        (heuristic, HEURISTIC, "PG-H"),
    ):
        for rule in rules:
            if rule.kind != kind or not rule.rule_id.startswith(prefix):
                raise ValueError(
                    f"{rule.rule_id}: kind={rule.kind!r} does not belong in the "
                    f"{kind} registry"
                )
            if rule.severity not in SEVERITIES:
                raise ValueError(f"{rule.rule_id}: unknown severity {rule.severity!r}")
            if rule.rule_id in seen:
                raise ValueError(f"duplicate rule_id {rule.rule_id}")
            if not rule.evidence:
                raise ValueError(f"{rule.rule_id}: must declare an evidence schema")
            for key, spec in rule.evidence.items():
                _validate_schema_spec(rule.rule_id, key, spec)
            seen.add(rule.rule_id)


_validate_registry(DETERMINISTIC_RULES, HEURISTIC_RULES)


def rule_catalog() -> list[dict]:
    return [r.describe() for r in DETERMINISTIC_RULES + HEURISTIC_RULES]


# ── Runner ────────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_AUTHORIZED_ACTIONS = frozenset({
    sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_RECURSIVE,
})
# Read-only pragmas Guard issues (measured with an authorizer log, not
# assumed). table_info/foreign_key_check take a table argument; the two
# setting pragmas are allowed only as bare reads, never assignments.
_ARG_READ_PRAGMAS = frozenset({"table_info", "foreign_key_check"})
_BARE_READ_PRAGMAS = frozenset({"query_only", "data_version"})


def _read_only_authorizer(action, arg1, arg2, _db_name, _source):
    """Allowlist: anything not explicitly a read is denied at prepare time --
    INSERT/UPDATE/DELETE, every CREATE/DROP/ALTER, ATTACH/DETACH (which
    query_only does not stop from creating files), and pragma writes."""
    if action in _AUTHORIZED_ACTIONS:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA:
        name = (arg1 or "").lower()
        if name in _ARG_READ_PRAGMAS:
            return sqlite3.SQLITE_OK
        if name in _BARE_READ_PRAGMAS and arg2 is None:
            return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _open_snapshot_connection(db_path: str) -> sqlite3.Connection:
    """mode=rw never creates a missing file; query_only refuses writes.
    Deliberately NOT core.db.connect(): that issues PRAGMA journal_mode=WAL,
    which converts a non-WAL database -- a write Guard has no business doing.
    Also deliberately not mode=ro: a read-only handle on a WAL database leaves
    -wal/-shm side files behind that it cannot clean up on close."""
    uri = "file:" + urllib.parse.quote(os.path.abspath(db_path)) + "?mode=rw"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA query_only=ON")
    conn.set_authorizer(_read_only_authorizer)  # last: the two pragmas above are assignments
    return conn


def _schema(conn) -> dict:
    tables = {}
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall():
        tables[name] = frozenset(
            r[0] for r in conn.execute(
                "SELECT name FROM pragma_table_info(?)", (name,)
            ).fetchall()
        )
    return tables


def _missing_requirements(rule: Rule, schema: dict) -> list[str]:
    missing = []
    for table, cols in sorted(rule.requires.items()):
        if table not in schema:
            missing.append(table)
            continue
        missing.extend(f"{table}.{c}" for c in sorted(cols - schema[table]))
    return missing


def _finding_dict(rule: Rule, hit: Hit, observed_at: str) -> dict:
    subject = tuple(hit.subject)
    if not subject or not all(isinstance(r, RecordRef) for r in subject):
        raise ValueError("hit.subject must be a non-empty tuple of RecordRef")
    severity = hit.severity or rule.severity
    if severity not in SEVERITIES:
        raise ValueError(f"unknown severity {severity!r}")
    return {
        "finding_id": finding_id(rule.rule_id, subject),
        "rule_id": rule.rule_id,
        "rule_version": rule.version,
        "kind": rule.kind,
        "category": rule.category,
        "severity": severity,
        "title": rule.title,
        "subject": [r.to_dict() for r in subject],
        "related": [r.to_dict() for r in hit.related],
        "evidence": _validate_evidence(rule.evidence, hit.evidence),
        "proposed_remediation": rule.remediation,
        "uncertainty": rule.uncertainty,
        "mutation_state": "none",
        "authority": AUTHORITY,
        "observed_at": observed_at,
    }


def scan(db_path: str = None, *, rules: Iterable[Rule] = None) -> dict:
    """Run every registered rule against one read-consistent snapshot of the
    Palace DB and return a machine-readable receipt. Never mutates, never
    initializes, never repairs. `rules` exists for tests; production callers
    use the registries."""
    import config
    from core.test_isolation import refuse_if_production_path

    path = db_path or config.DB_PATH
    refuse_if_production_path(path)
    started = _now()
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "scan_id": uuid.uuid4().hex,
        "started_at": started,
        "finished_at": None,
        "db_path": os.path.abspath(path),
        "status": None,
        "authority": AUTHORITY,
        "mutation_state": "none",
        "row_counts": {},
        "rules": [],
        "deterministic_findings": [],
        "heuristic_findings": [],
    }
    rule_list = tuple(rules) if rules is not None else DETERMINISTIC_RULES + HEURISTIC_RULES

    if not os.path.exists(path):
        receipt["status"] = "no_database"
        receipt["finished_at"] = _now()
        return receipt

    try:
        conn = _open_snapshot_connection(path)
    except sqlite3.OperationalError as e:
        # e.g. the file vanished between the exists() check and the open --
        # mode=rw refuses to create it, and the scan reports rather than raises.
        receipt["status"] = "unavailable"
        receipt["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        receipt["finished_at"] = _now()
        return receipt
    try:
        # One deferred read transaction: every rule below sees the same
        # snapshot. Under WAL (the production journal mode) ordinary
        # concurrent writers -- Dreaming, compaction, the UI -- can still
        # commit while it is open; the open reader can delay WAL checkpoints
        # (a TRUNCATE checkpoint reports busy). On a non-WAL database the
        # read lock makes writers wait up to their own busy_timeout.
        conn.execute("BEGIN")
        schema = _schema(conn)
        present = [t for t in PALACE_TABLES if t in schema]
        for t in present:
            receipt["row_counts"][t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        if not present:
            receipt["status"] = "palace_not_initialized"
        else:
            incomplete = False
            for rule in rule_list:
                entry = {"rule_id": rule.rule_id, "version": rule.version,
                         "kind": rule.kind, "state": None, "findings": 0}
                missing = _missing_requirements(rule, schema)
                if missing:
                    entry.update(state="skipped", reason="missing: " + ", ".join(missing))
                    incomplete = True
                    receipt["rules"].append(entry)
                    continue
                try:
                    observed_at = _now()
                    found = [_finding_dict(rule, h, observed_at) for h in rule.check(conn)]
                except Exception as e:
                    entry.update(state="error", reason=_error_reason(e))
                    incomplete = True
                    receipt["rules"].append(entry)
                    continue
                ids = [f["finding_id"] for f in found]
                if len(ids) != len(set(ids)):
                    entry.update(state="error", reason="rule produced duplicate finding ids")
                    incomplete = True
                    receipt["rules"].append(entry)
                    continue
                entry.update(state="ran", findings=len(found))
                receipt["rules"].append(entry)
                target = ("deterministic_findings" if rule.kind == DETERMINISTIC
                          else "heuristic_findings")
                receipt[target].extend(found)
            receipt["status"] = "partial" if incomplete else "complete"
    finally:
        try:
            conn.rollback()
        finally:
            changes = conn.total_changes
            conn.close()
    if changes:
        receipt["mutation_state"] = "unexpected_change_detected"
        receipt["status"] = "error"
    receipt["finished_at"] = _now()
    return receipt


# ── Query surface ─────────────────────────────────────────────────────────────

def findings(receipt: dict, *, kind: str = None, rule_id: str = None,
             severity: str = None) -> list[dict]:
    """Filter a receipt's findings. Deterministic first, then heuristic --
    each keeps its own kind; nothing here merges or re-scores them."""
    out = []
    for key, k in (("deterministic_findings", DETERMINISTIC),
                   ("heuristic_findings", HEURISTIC)):
        if kind is not None and kind != k:
            continue
        for f in receipt.get(key, []):
            if rule_id is not None and f.get("rule_id") != rule_id:
                continue
            if severity is not None and f.get("severity") != severity:
                continue
            out.append(f)
    return out


def inspect_finding(receipt: dict, fid: str) -> dict | None:
    """One finding plus its rule's full description, or None."""
    catalog = {r.rule_id: r for r in DETERMINISTIC_RULES + HEURISTIC_RULES}
    for f in findings(receipt):
        if f.get("finding_id") == fid:
            rule = catalog.get(f.get("rule_id"))
            return {"finding": f, "rule": rule.describe() if rule else None}
    return None


def reconcile(previous: dict, current: dict) -> dict:
    """Compare two receipts by finding_id. Reads only ids and rule states
    from `previous` -- a prior receipt is data, and nothing it claims
    (mutation_state, authority, remediation text) carries forward.

    A previous finding absent now is "resolved" only if its rule actually
    ran in the current scan; if the rule was skipped or errored it is
    "unverified", never silently resolved."""
    def ids_by_rule(receipt):
        out = {}
        for f in findings(receipt or {}):
            fid, rid = f.get("finding_id"), f.get("rule_id")
            if isinstance(fid, str) and isinstance(rid, str):
                out[fid] = rid
        return out

    prev = ids_by_rule(previous)
    cur = ids_by_rule(current)
    ran_now = {r.get("rule_id") for r in current.get("rules", []) if r.get("state") == "ran"}
    gone = set(prev) - set(cur)
    return {
        "new": sorted(set(cur) - set(prev)),
        "recurring": sorted(set(cur) & set(prev)),
        "resolved": sorted(f for f in gone if prev[f] in ran_now),
        "unverified": sorted(f for f in gone if prev[f] not in ran_now),
        "same_database": (previous or {}).get("db_path") == current.get("db_path"),
    }
