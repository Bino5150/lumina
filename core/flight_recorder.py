"""
core/flight_recorder.py -- AGENT-FLIGHT-RECORDER-01A1

Canonical structured runtime event log ("the .50 gets a GoPro"). One
append-only SQLite table, under DATA_DIR/telemetry/ (never the semantic-
memory database), recording enough machine-observed fact and
provider-exposed model expression to reconstruct a session from startup
through final response -- without ever treating model narration as
machine truth, and without ever changing what actually happened.

Engineering law: record what happened without changing what happened.
Two consequences that shape every design choice below:

1. Provenance is structural, not a caller-supplied flag. record_machine_
   event() and record_model_expression() are separate entry points --
   MODEL_EVENT_TYPES (turn.think/turn.commentary/turn.final) can ONLY be
   written through record_model_expression(), and every other event_type
   can ONLY be written through record_machine_event(). Passing the wrong
   one raises ValueError -- a caller-code bug, meant to fail loud in
   tests/dev, never silently miscategorized.

2. Recorder failure can never break a live agent turn. Every OPERATIONAL
   failure (disk full, locked db, an unrepresentable value in `fields`,
   init failing entirely) is caught internally, printed once, and turns
   the recorder into a safe no-op (self.enabled = False, or a swallowed
   per-write exception) -- never raised into the caller. The ONLY thing
   this module raises for is the provenance-API misuse described above,
   which is a programming-bug signal, not an operational one, and must
   stay loud precisely so tests can catch it.

Retention (locked product requirement, computed at INSERT time via each
row's own `expires_at`, oldest-first DELETE):
    severity in (warning, error) -> 90 days
    everything else (debug/info)  -> 7 days
Plus a secondary emergency byte-size ceiling per db file so one
pathological run cannot consume the disk even inside the 7-day window --
time retention is the primary policy, the size ceiling is a backstop.

Storage: core.db.connect()'s existing WAL + busy_timeout convention (the
same factory backing memory/chat/skills/coding_checkpoint/etc.) -- one
persistent connection per FlightRecorder instance, guarded by a single
threading.Lock for cross-thread write serialization within this process;
concurrent-writer safety across processes is WAL's own job, same as every
other database in this codebase.
"""

import hashlib
import json
import os
import threading
import time
import uuid

import config
from core.db import connect

TELEMETRY_DIR = os.path.join(config.DATA_DIR, "telemetry")
TELEMETRY_DB_PATH = os.path.join(TELEMETRY_DIR, "flight_recorder.db")

FULL_RETENTION_SECONDS = 7 * 24 * 3600
ERROR_RETENTION_SECONDS = 90 * 24 * 3600

# Emergency backstop only -- see module docstring. Not user-configurable in
# A1 (mission: "Do not invent a UI-configurable retention system in A1").
DEFAULT_MAX_DB_BYTES = 500 * 1024 * 1024
PRUNE_EVERY_N_WRITES = 500

MAX_FIELD_STRING_CHARS = 2000
MAX_FIELDS_JSON_BYTES = 4096
MAX_SANITIZE_DEPTH = 6
MAX_LIST_ITEMS = 50

SEVERITIES = ("debug", "info", "warning", "error")
PROVENANCES = ("machine", "model")

# AGENT-TOOL-THINK-TELEMETRY-01A1's own vocabulary: Think = provider-exposed
# reasoning, Commentary = outward work intent. turn.final joins them here --
# the model's own final-answer text is model expression exactly like Think/
# Commentary, even though the envelope around it (duration, char count) is
# machine-measured and rides along in the same event's `fields`.
MODEL_EVENT_TYPES = frozenset({"turn.think", "turn.commentary", "turn.final"})

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS events (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    runtime_id  TEXT NOT NULL,
    chat_id     INTEGER,
    turn_id     TEXT,
    task_id     TEXT,
    process_id  TEXT,
    worktree_id TEXT,
    activity_id TEXT,
    epoch       INTEGER,
    event_type  TEXT NOT NULL,
    severity    TEXT NOT NULL,
    provenance  TEXT NOT NULL,
    backend     TEXT,
    model       TEXT,
    fields_json TEXT NOT NULL,
    expires_at  REAL NOT NULL
)
"""
_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)",
    "CREATE INDEX IF NOT EXISTS idx_events_turn ON events(turn_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type)",
    "CREATE INDEX IF NOT EXISTS idx_events_expires ON events(expires_at)",
)


# ---------------------------------------------------------------------
# Redaction / bounding -- payload policy (mission section 7)
# ---------------------------------------------------------------------

# CONTEXT-LIFECYCLE-A4I: the actual key-marker tuple, value-shape regex, and
# redaction function now live in core/redaction.py -- a neutral, public
# module so the continuity compiler can share the exact same primitives
# without reaching into this module's private names. Aliased under the
# original private names so every call site below (_sanitize/_sanitize_
# fields/bounded_repr) is unchanged; behavior is byte-for-byte identical to
# the pre-A4I inline definitions -- this is a pure extraction, not a
# behavior change.
from core.redaction import SECRET_KEY_MARKERS as _SECRET_KEY_MARKERS
from core.redaction import SECRET_VALUE_RE as _SECRET_VALUE_RE
from core.redaction import redact_secret_shapes as _redact_value_shapes

_IMAGE_CONTENT_TYPES = frozenset({"image", "image_url", "input_image", "inline_data"})
_CHAT_ROLES = frozenset({"user", "assistant", "system", "tool"})


def _looks_like_conversation(value: list) -> bool:
    """True if `value` is a list of message-shaped dicts -- the structural
    shape of ctx.history / a messages[] payload. Refusing this shape at the
    door (never persisting it) is what makes "no context-history dump
    accepted" an enforced guarantee rather than a policy someone has to
    remember not to violate."""
    if not value:
        return False
    sample = value[:5]
    hits = sum(1 for m in sample if isinstance(m, dict) and m.get("role") in _CHAT_ROLES)
    return hits >= max(1, len(sample) // 2)


def _sanitize(value, depth: int = 0):
    if depth > MAX_SANITIZE_DEPTH:
        return "[max depth exceeded]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        redacted = _redact_value_shapes(value)
        if len(redacted) > MAX_FIELD_STRING_CHARS:
            return redacted[:MAX_FIELD_STRING_CHARS] + f"...[truncated, {len(redacted)} chars total]"
        return redacted
    if isinstance(value, (bytes, bytearray)):
        return {"_type": "binary", "size_bytes": len(value)}
    if isinstance(value, dict):
        btype = value.get("type")
        if isinstance(btype, str) and btype in _IMAGE_CONTENT_TYPES:
            return {"_type": "binary_stub", "content_type": btype}
        return _sanitize_fields(value, depth=depth + 1)
    if isinstance(value, (list, tuple)):
        value = list(value)
        if _looks_like_conversation(value):
            return f"[rejected: conversation-history-shaped payload, {len(value)} items]"
        capped = value[:MAX_LIST_ITEMS]
        out = [_sanitize(v, depth=depth + 1) for v in capped]
        if len(value) > MAX_LIST_ITEMS:
            out.append(f"...[{len(value) - MAX_LIST_ITEMS} more items truncated]")
        return out
    try:
        text = str(value)
    except Exception:
        return "[unrepresentable value]"
    return _sanitize(text, depth=depth)


def _sanitize_fields(fields: dict, depth: int = 0) -> dict:
    if not isinstance(fields, dict):
        return {}
    out = {}
    for k, v in fields.items():
        key = str(k)
        if any(marker in key.lower() for marker in _SECRET_KEY_MARKERS):
            out[key] = "[REDACTED]"
            continue
        out[key] = _sanitize(v, depth=depth)
    return out


def hash_args(args) -> str:
    """Stable SHA-256 hex digest of a tool-call args value, order-
    independent (sort_keys) for dict args -- the duplicate-call-detection
    primitive (mission section 6/12). Never raises: an unserializable args
    value falls back to hashing its str() representation instead."""
    try:
        blob = json.dumps(args, sort_keys=True, default=str)
    except Exception:
        blob = str(args)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def bounded_repr(value, limit: int = MAX_FIELD_STRING_CHARS) -> str:
    """Redacted, length-bounded str() of an arbitrary value -- the
    "bounded/redacted args representation" the mission asks tool.call to
    carry alongside the stable hash from hash_args()."""
    try:
        text = json.dumps(value, default=str) if not isinstance(value, str) else value
    except Exception:
        text = str(value)
    text = _redact_value_shapes(text)
    if len(text) > limit:
        return text[:limit] + f"...[truncated, {len(text)} chars total]"
    return text


# ---------------------------------------------------------------------
# The recorder itself
# ---------------------------------------------------------------------

class FlightRecorder:
    """One recorder instance = one SQLite file + one runtime_id. Production
    code uses the module-level singleton (get_recorder()); tests construct
    their own with an isolated db_path for full independence (own
    runtime_id, own file, zero shared state, safe to run concurrently)."""

    def __init__(self, db_path: str = None, max_db_bytes: int = DEFAULT_MAX_DB_BYTES):
        self.db_path = db_path or TELEMETRY_DB_PATH
        self.max_db_bytes = max_db_bytes
        self.runtime_id = str(uuid.uuid4())
        self._lock = threading.Lock()
        self._conn = None
        self._write_count = 0
        self.enabled = True
        self._init()

    def _init(self):
        try:
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
            # check_same_thread=False: this ONE connection is held open for
            # the recorder's whole lifetime and legitimately called from
            # whichever thread is recording an event (the GUI thread, a
            # background dream-sweep, a Discord/Telegram bridge thread via
            # asyncio.to_thread, ...) -- safe specifically because every
            # actual use of self._conn is serialized through self._lock in
            # _write()/_prune()/checkpoint(), the exact safe pattern
            # Python's own sqlite3 docs describe for this flag.
            self._conn = connect(path=self.db_path, row_factory=False,
                                  foreign_keys=False, check_same_thread=False)
            self._conn.execute(_SCHEMA_SQL)
            for stmt in _INDEX_SQL:
                self._conn.execute(stmt)
            self._conn.commit()
            self._prune()
        except Exception as e:
            print(f"[FLIGHT_RECORDER] init failed ({self.db_path}) -- recorder disabled: {e}", flush=True)
            self.enabled = False
            self._conn = None

    # -- provenance-separated public API (mission section 3) -----------

    def record_machine_event(self, event_type: str, **kwargs) -> None:
        if event_type in MODEL_EVENT_TYPES:
            raise ValueError(
                f"{event_type!r} is a model-expression event type -- use "
                f"record_model_expression(), not record_machine_event()."
            )
        self._write(event_type, provenance="machine", **kwargs)

    def record_model_expression(self, event_type: str, *, text: str, fields: dict = None, **kwargs) -> None:
        if event_type not in MODEL_EVENT_TYPES:
            raise ValueError(
                f"{event_type!r} is not a recognized model-expression event type "
                f"({sorted(MODEL_EVENT_TYPES)}) -- use record_machine_event()."
            )
        merged = dict(fields or {})
        merged["text"] = text
        self._write(event_type, provenance="model", fields=merged, **kwargs)

    # -- write path ------------------------------------------------------

    def _write(self, event_type: str, *, provenance: str, severity: str = "info",
               fields: dict = None, chat_id=None, turn_id: str = None,
               task_id: str = None, process_id: str = None, worktree_id: str = None,
               activity_id: str = None, epoch: int = None, backend: str = None,
               model: str = None) -> None:
        if not self.enabled or self._conn is None:
            return
        if severity not in SEVERITIES:
            severity = "info"
        if provenance not in PROVENANCES:
            provenance = "machine"
        try:
            clean_fields = _sanitize_fields(fields or {})
            blob = json.dumps(clean_fields, default=str)
            if len(blob.encode("utf-8")) > MAX_FIELDS_JSON_BYTES:
                clean_fields = {"_truncated": True, "_original_keys": sorted(clean_fields.keys())}
                blob = json.dumps(clean_fields)
            ts = time.time()
            expires_at = ts + (ERROR_RETENTION_SECONDS if severity in ("warning", "error") else FULL_RETENTION_SECONDS)
            with self._lock:
                self._conn.execute(
                    "INSERT INTO events (ts, runtime_id, chat_id, turn_id, task_id, "
                    "process_id, worktree_id, activity_id, epoch, event_type, severity, "
                    "provenance, backend, model, fields_json, expires_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ts, self.runtime_id, chat_id, turn_id, task_id, process_id,
                     worktree_id, activity_id, epoch, event_type, severity,
                     provenance, backend, model, blob, expires_at),
                )
                self._conn.commit()
                self._write_count += 1
                if self._write_count % PRUNE_EVERY_N_WRITES == 0:
                    self._prune()
        except Exception as e:
            print(f"[FLIGHT_RECORDER] write failed (event_type={event_type}): {e}", flush=True)

    # -- retention (mission section 1 / 9) --------------------------------

    def _prune(self) -> None:
        """Time-based expiry (primary policy) + emergency size ceiling
        (backstop). Called at init (startup) and every PRUNE_EVERY_N_WRITES
        writes -- never on every single write. Caller-side: _write() already
        holds self._lock when calling this from the periodic path; _init()
        calls it before any concurrent writer can exist. Never raises."""
        if self._conn is None:
            return
        try:
            self._conn.execute("DELETE FROM events WHERE expires_at < ?", (time.time(),))
            self._conn.commit()
            self._enforce_size_ceiling()
        except Exception as e:
            print(f"[FLIGHT_RECORDER] prune failed: {e}", flush=True)

    def _enforce_size_ceiling(self) -> None:
        """Emergency backstop only -- see module docstring. Oldest-first
        (ORDER BY seq ASC, and seq is monotonically increasing -- see
        `seq` ordering guarantee below) by construction."""
        try:
            size = os.path.getsize(self.db_path)
        except OSError:
            return
        if size <= self.max_db_bytes:
            return
        row = self._conn.execute("SELECT COUNT(*) FROM events").fetchone()
        total_rows = row[0] if row else 0
        if not total_rows:
            return
        # Shed a fraction proportional to the overage, floored so a
        # barely-over ceiling still makes real progress in one pass rather
        # than requiring many prune cycles to converge.
        overage_fraction = min(0.9, max(0.1, (size - self.max_db_bytes) / size))
        to_delete = max(1, int(total_rows * overage_fraction))
        self._conn.execute(
            "DELETE FROM events WHERE seq IN "
            "(SELECT seq FROM events ORDER BY seq ASC LIMIT ?)",
            (to_delete,),
        )
        self._conn.commit()
        # Deliberate checkpoint: this is the rare emergency path, not the
        # routine per-N-writes prune -- mission section 9 explicitly says
        # not to checkpoint on every routine prune, not that an emergency
        # size-driven eviction shouldn't reclaim the space it just freed.
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass

    # -- deliberate lifecycle-boundary checkpoint (mission section 9/10) --

    def checkpoint(self) -> None:
        """Explicit WAL checkpoint for a deliberate lifecycle boundary
        (Memory Backup, graceful shutdown) -- never called from the
        routine per-N-writes prune path. Never raises."""
        if not self.enabled or self._conn is None:
            return
        try:
            with self._lock:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._conn.commit()
        except Exception as e:
            print(f"[FLIGHT_RECORDER] checkpoint failed: {e}", flush=True)

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


# ---------------------------------------------------------------------
# Module-level singleton + production convenience API
# ---------------------------------------------------------------------

_default_recorder = None
_default_lock = threading.Lock()


def get_recorder() -> FlightRecorder:
    """Lazily-created process-wide default instance, bound to
    TELEMETRY_DB_PATH (DATA_DIR/telemetry/flight_recorder.db). Tests should
    never call this -- construct FlightRecorder(db_path=...) directly for
    isolation instead."""
    global _default_recorder
    if _default_recorder is None:
        with _default_lock:
            if _default_recorder is None:
                _default_recorder = FlightRecorder()
    return _default_recorder


def new_turn_id() -> str:
    """Mint one turn_id -- called once per foreground agent turn (mission
    section 2). Centralized here (not a bare uuid.uuid4() at each call
    site) so the id format is one owned decision, not duplicated."""
    return str(uuid.uuid4())


def record_machine_event(event_type: str, **kwargs) -> None:
    get_recorder().record_machine_event(event_type, **kwargs)


def record_model_expression(event_type: str, *, text: str, **kwargs) -> None:
    get_recorder().record_model_expression(event_type, text=text, **kwargs)


def checkpoint() -> None:
    """Best-effort checkpoint of the default recorder -- for Memory Backup
    and graceful process shutdown. No-op if the recorder was never used
    this process (nothing to checkpoint)."""
    if _default_recorder is not None:
        _default_recorder.checkpoint()
