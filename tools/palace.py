"""
MemPalace — Layered memory architecture for Lumina.

Structure:
  Wings    → Major topic domains (identity, projects, people, preferences, sessions)
  Rooms    → Sub-topics within a wing
  Closets  → AAAK-compressed summaries (~30x token reduction, always readable)
  Drawers  → Verbatim originals (retrieved on demand only)

Layers:
  L0  ~50 tok  — Identity core. Always injected. Never changes unless you update it.
  L1  ~120 tok — Critical facts. Always injected. Updated when something important changes.
  (L0/L1 are privileged: only the startup seed or an owner-approved promotion may place a
   row there -- a model's request for them is stored at L2 and staged. See the admission
   section below.)
  L2  ~300 tok — Recent sessions + active projects. Loaded at session start.
  L3  unlimited — Full verbatim originals. Searched on demand via recall tool.
"""

import sqlite3
import json
import re
import threading
import weakref
from datetime import datetime
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from tools.temporal_decay import decay_engine

# ── DB Setup ───────────────────────────────────────────────────────────────────

def get_db():
    from core.db import connect
    return connect()


def init_palace_db():
    """Create palace tables if they don't exist. Safe to call on every startup."""
    # PALACE-GUARD-01B-1: palace_drawers / palace_halls carry a nullable
    # source_memory_id FK to memories(id). With foreign keys enforced (every
    # core.db connection), SQLite refuses ANY insert into a table whose FK
    # parent table doesn't exist -- even with a NULL key -- so the parent must
    # exist before the Palace is usable. init_memory_db() is idempotent.
    from tools.memory import init_memory_db
    init_memory_db()

    conn = get_db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS palace_wings (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            description TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS palace_rooms (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            wing_id INTEGER NOT NULL,
            name    TEXT NOT NULL,
            UNIQUE(wing_id, name),
            FOREIGN KEY (wing_id) REFERENCES palace_wings(id) ON DELETE CASCADE
        )
    """)

    # Closets: compressed AAAK summaries — injected into context
    conn.execute("""
        CREATE TABLE IF NOT EXISTS palace_closets (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id    INTEGER NOT NULL,
            layer      INTEGER NOT NULL DEFAULT 2,  -- 0=identity, 1=critical, 2=recent, 3=deep
            compressed TEXT NOT NULL,               -- AAAK format
            token_est  INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (room_id) REFERENCES palace_rooms(id) ON DELETE CASCADE
        )
    """)

    # Drawers: verbatim originals — retrieved only via recall()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS palace_drawers (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            closet_id  INTEGER,                     -- optional link to parent closet
            room_id    INTEGER NOT NULL,
            content    TEXT NOT NULL,               -- raw original
            tags       TEXT,                        -- JSON array of search tags
            untrusted  INTEGER NOT NULL DEFAULT 0,  -- durable per-record provenance
            created_at TEXT NOT NULL,
            FOREIGN KEY (room_id)   REFERENCES palace_rooms(id)   ON DELETE CASCADE,
            FOREIGN KEY (closet_id) REFERENCES palace_closets(id) ON DELETE SET NULL
        )
    """)

    # CANNON-08: tags were model-supplied/searchable observability text, not
    # structural provenance, so migration MUST NOT parse them back into
    # authority. Existing drawers have unknowable per-record trust and enter
    # fail-closed as lower-trust. Every new write below supplies the bit
    # explicitly, so genuine runtime owner writes remain trusted thereafter.
    try:
        conn.execute(
            "ALTER TABLE palace_drawers ADD COLUMN untrusted "
            "INTEGER NOT NULL DEFAULT 1"
        )
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            raise

    # Halls: cross-cutting fact streams (events, discoveries, preferences, advice)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS palace_halls (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            hall       TEXT NOT NULL,               -- 'facts' | 'events' | 'preferences' | 'advice' | 'discoveries'
            compressed TEXT NOT NULL,               -- AAAK fact
            layer      INTEGER NOT NULL DEFAULT 2,
            created_at TEXT NOT NULL
        )
    """)

    # CASTLE-WALLS-REPAIR-01 R1D -- ever_had_untrusted_merge is a coarse,
    # sticky, closet-level signal used for exactly one thing: telling
    # ContextManager whether to fold the soft "Provenance reminder" nudge
    # into _untrusted_content_seen. It never gates or reshapes what text a
    # closet actually renders -- that's done per-SEGMENT, inline, by
    # tag_untrusted()-wrapping only the newly-added segment at merge time
    # (see palace_store() below), so one lower-trust contribution can never
    # retroactively discredit unrelated segments that already shared the
    # same closet. Idempotent in-place migration, same idiom as
    # core/skills.py's origin column -- existing closets default to 0
    # (never had an untrusted merge), which is exactly correct: nothing
    # before this repair ever recorded provenance at all.
    try:
        conn.execute(
            "ALTER TABLE palace_closets ADD COLUMN ever_had_untrusted_merge "
            "INTEGER NOT NULL DEFAULT 0"
        )
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            raise

    # Halls are never merged (palace_store_hall() always INSERTs a fresh,
    # independent row -- each fact is already atomic), so a plain per-row
    # flag is correct here, unlike closets.
    try:
        conn.execute("ALTER TABLE palace_halls ADD COLUMN untrusted INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            raise

    # PALACE-GUARD-01B-1: structural source linkage. A drawer/hall derived
    # from a flat memory (save_memory's write-through) records that memory's
    # exact id; everything else -- model-native palace_remember/palace_hall,
    # Dreaming, compaction -- and every pre-existing row stays NULL. Existing
    # rows are never linked after the fact: no content matching, no inferred
    # provenance. Default ON DELETE NO ACTION on purpose: deleting a linked
    # memory directly fails under FK enforcement instead of orphaning its
    # derivatives (CASCADE could drop drawers but can't rebuild closets).
    for table in ("palace_drawers", "palace_halls"):
        try:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN source_memory_id "
                "INTEGER REFERENCES memories(id)"
            )
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e):
                raise
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table}_source_memory_id "
            f"ON {table}(source_memory_id)"
        )

    # PALACE-GUARD-01B-2: lifecycle quarantine + trusted synthesis origin.
    # Both migrate as NULL and nothing classifies existing rows: NULL
    # withheld_reason = ordinary (not withheld); NULL origin = not provably
    # synthesized. Readers treat ANY non-NULL withheld_reason as withheld --
    # an unknown or malformed value fails closed instead of becoming
    # injectable.
    for table, column in (("palace_closets", "withheld_reason"), ("palace_drawers", "origin")):
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e):
                raise

    # PALACE-GUARD-01B-3: privileged-layer admission provenance. Layer lives on
    # the closet (and on the hall row); these columns record HOW a row came to
    # occupy L0/L1: 'trusted_startup_seed' or 'explicit_owner_promotion' (see
    # ADMISSION_CLASSES). Like withheld_reason/origin above, both migrate as
    # NULL and nothing classifies existing rows: NULL = ordinary, legacy or
    # unknown provenance. Never inferred from content, tags or layer.
    for table in ("palace_closets", "palace_halls"):
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN admission TEXT")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e):
                raise

    # PALACE-GUARD-01B-4: the promotion ledger. One row per executed owner
    # promotion; approval_id is the PRIMARY KEY, so an approval can be consumed
    # at most once even if the in-memory registry were bypassed. It is also the
    # audit receipt: what was promoted, from/to which layer, against which
    # approved snapshot digest, and the trust bit as it was when promoted (which
    # promotion never changes). No FK to drawers/halls: a receipt outlives a
    # later deletion of its target.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS palace_promotion_receipts (
            approval_id      TEXT PRIMARY KEY,
            operation_id     TEXT,
            owner_event      TEXT,
            target_kind      TEXT NOT NULL,
            target_id        INTEGER NOT NULL,
            from_layer       INTEGER,
            to_layer         INTEGER NOT NULL,
            snapshot_digest  TEXT NOT NULL,
            untrusted        INTEGER NOT NULL,
            result_closet_id INTEGER,
            closet_outcome   TEXT,
            promoted_at      TEXT NOT NULL
        )
    """)

    # Seed default wings if empty
    wings = [
        ("identity",    "Who Lumina is, who Bino is, core relationship"),
        ("projects",    "Active and past projects Bino is working on"),
        ("people",      "People in Bino's life"),
        ("preferences", "Bino's preferences, habits, likes/dislikes"),
        ("sessions",    "Per-session summaries and discoveries"),
    ]
    for name, desc in wings:
        conn.execute(
            "INSERT OR IGNORE INTO palace_wings (name, description) VALUES (?, ?)",
            (name, desc)
        )

    # Seed L0 identity closet if palace is brand new
    existing = conn.execute("SELECT COUNT(*) as c FROM palace_closets").fetchone()["c"]
    if existing == 0:
        _seed_l0(conn)

    # REDDIT-INGRESS-AUTHORITY-01: historical Dream/compaction drawers may
    # predate the rule that every model-authored durable summary is
    # lower-trust.  Repair both the per-drawer authority bit and each linked
    # rolling closet's rendered segments.  Idempotent: once every matching
    # row is untrusted, this is a read-only no-op on later startups.
    _migrate_synthesized_drawer_authority(conn)

    conn.commit()
    conn.close()


# ── Privileged-layer admission (PALACE-GUARD-01B-3) ────────────────────────────
#
# Layer is not authority, and a requested layer is not an admission. L0/L1 are
# injected into every owner turn and exempt from decay and inject limits, so no
# generic write may place a row there. A row may occupy layer < 2 only when its
# writer presents a PrivilegedAdmission -- an opaque capability that:
#
#   * can only be constructed by this module's private minters (a module-private
#     token) and is registered in _MINTED_ADMISSIONS at mint time, so an object
#     forged with object.__new__/copy/pickle or a look-alike (dict, str, bool,
#     SimpleNamespace, subclass) is refused;
#   * is scoped to a closed admission class, a row kind (closet|hall), the
#     layers it may place, and -- for owner promotion -- one exact target;
#   * is never a parameter of the generic writers. palace_store /
#     palace_store_hall / _store_in_conn / _store_hall_in_conn take no admission
#     argument at all and refuse layer < 2 unconditionally. The ONLY functions
#     that accept one are the two INSERT chokepoints below, and only
#     _seed_l0() (and, in 01B-4, the owner-promotion service) call them with one.
#
# A model may ask for the crown (palace_remember(layer=1)); the wrapper stores
# the material at L2 and stages the request. Nothing here reads a tag, a string
# or a flag to decide privilege, and none of it is a host sandbox: it closes the
# tool/argument path, not hostile in-process Python.

PRIVILEGED_LAYERS = frozenset({0, 1})
ADMISSION_TRUSTED_STARTUP_SEED = "trusted_startup_seed"
ADMISSION_EXPLICIT_OWNER_PROMOTION = "explicit_owner_promotion"
ADMISSION_CLASSES = frozenset({
    ADMISSION_TRUSTED_STARTUP_SEED,
    ADMISSION_EXPLICIT_OWNER_PROMOTION,
})


class PrivilegedLayerRefused(ValueError):
    """A write tried to occupy L0/L1 without a valid, correctly-scoped
    PrivilegedAdmission (or with an ill-typed layer). Nothing was written."""


_MINT = object()  # module-private mint token; never exported or stored on a row
_MINTED_ADMISSIONS = weakref.WeakSet()
_MINT_LOCK = threading.Lock()


class PrivilegedAdmission:
    """Opaque, immutable, non-copyable, non-subclassable admission capability.
    Construct only through the private _mint_* functions."""

    __slots__ = ("_admission_class", "_kinds", "_layers", "_target", "__weakref__")

    def __init_subclass__(cls, **kwargs):
        raise TypeError("PrivilegedAdmission cannot be subclassed")

    def __init__(self, token, admission_class, *, kinds, layers, target=None):
        if token is not _MINT:
            raise TypeError("PrivilegedAdmission can only be minted by the Palace admission minters")
        if admission_class not in ADMISSION_CLASSES:
            raise ValueError(f"unknown admission class {admission_class!r}")
        set_ = object.__setattr__
        set_(self, "_admission_class", admission_class)
        set_(self, "_kinds", frozenset(kinds))
        set_(self, "_layers", frozenset(layers))
        set_(self, "_target", target)

    def __setattr__(self, name, value):
        raise AttributeError("PrivilegedAdmission is immutable")

    def __delattr__(self, name):
        raise AttributeError("PrivilegedAdmission is immutable")

    def __copy__(self):
        raise TypeError("PrivilegedAdmission cannot be copied")

    def __deepcopy__(self, memo):
        raise TypeError("PrivilegedAdmission cannot be copied")

    def __reduce_ex__(self, protocol):
        raise TypeError("PrivilegedAdmission cannot be serialized")

    @property
    def admission_class(self) -> str:
        return self._admission_class

    def permits(self, kind: str, layer: int, target=None) -> bool:
        return (
            kind in self._kinds
            and layer in self._layers
            and (self._target is None or self._target == target)
        )


def _register_admission(admission: PrivilegedAdmission) -> PrivilegedAdmission:
    with _MINT_LOCK:
        _MINTED_ADMISSIONS.add(admission)
    return admission


def _mint_startup_seed_admission() -> PrivilegedAdmission:
    """The startup identity seed's admission: one L0 closet, nothing else. It
    cannot place L1, a hall, or anything bound to a target."""
    return _register_admission(PrivilegedAdmission(
        _MINT, ADMISSION_TRUSTED_STARTUP_SEED, kinds=("closet",), layers=(0,),
    ))


def _mint_owner_promotion_admission(target_kind: str, target_id: int,
                                    dest_layer: int) -> PrivilegedAdmission:
    """01B-4: admission for placing ONE record at ONE privileged layer.
    Minted only by core.palace_promotion.promote_with_owner_approval(), and only
    after it has re-read the live record inside its write transaction and found
    it byte-for-byte what the owner approved. It cannot place a different
    record, a different layer, or a different row kind."""
    if target_kind not in ("drawer", "hall"):
        raise ValueError(f"unknown promotion target kind {target_kind!r}")
    if type(target_id) is not int or type(dest_layer) is not int \
            or dest_layer not in PRIVILEGED_LAYERS:
        raise ValueError("promotion admission needs an int target id and a layer of 0 or 1")
    return _register_admission(PrivilegedAdmission(
        _MINT, ADMISSION_EXPLICIT_OWNER_PROMOTION,
        kinds=("closet" if target_kind == "drawer" else "hall",),
        layers=(dest_layer,), target=(target_kind, target_id),
    ))


def _strict_layer(layer) -> int:
    """A layer must be a real int. SQLite's INTEGER affinity would otherwise
    store "1", 1.0 and True as layer 1, so a comparison on the raw value is not
    a gate. bool, float, str, None and int subclasses are refused outright."""
    if type(layer) is not int:
        raise PrivilegedLayerRefused(
            f"palace layer must be an int, got {type(layer).__name__}"
        )
    return layer


def _require_admission(admission, kind: str, layer: int, target=None) -> str:
    """Returns the admission class to stamp, or raises. Fails closed on any
    object that is not a live, minted PrivilegedAdmission scoped to this row."""
    try:
        with _MINT_LOCK:
            minted = admission in _MINTED_ADMISSIONS
    except TypeError:
        minted = False
    if type(admission) is not PrivilegedAdmission or not minted:
        raise PrivilegedLayerRefused(
            f"layer {layer} requires a trusted admission; none was presented"
        )
    try:
        allowed = admission.permits(kind, layer, target)
        admission_class = admission.admission_class
    except AttributeError:
        allowed = False
    if not allowed:
        raise PrivilegedLayerRefused(
            f"admission does not permit a {kind} at layer {layer}"
        )
    return admission_class


def _refuse_generic_privileged_layer(layer) -> int:
    """The generic writers' whole policy: strict int, and never below L2."""
    layer = _strict_layer(layer)
    if layer < 2:
        raise PrivilegedLayerRefused(
            f"layer {layer} is a privileged layer; generic Palace writes are limited to "
            "L2 and below. Request promotion instead."
        )
    return layer


def _insert_closet_row(conn, *, room_id, layer, compressed, token_est, now,
                       ever_untrusted, admission=None, target=None) -> int:
    """THE only place a palace_closets row is INSERTed (01B-3 chokepoint). A
    layer < 2 needs a PrivilegedAdmission scoped to a closet at that layer and
    is stamped with its class; an ordinary layer takes none and stays NULL.
    `target` names the record the closet is being made for; a target-bound
    admission (owner promotion) is refused unless it matches exactly."""
    layer = _strict_layer(layer)
    if layer < 2:
        stamp = _require_admission(admission, "closet", layer, target)
    elif admission is not None:
        raise PrivilegedLayerRefused("an admission is only meaningful for layer < 2")
    else:
        stamp = None
    cur = conn.execute(
        "INSERT INTO palace_closets (room_id, layer, compressed, token_est, created_at, "
        "updated_at, ever_had_untrusted_merge, admission) VALUES (?,?,?,?,?,?,?,?)",
        (room_id, layer, compressed, token_est, now, now, 1 if ever_untrusted else 0, stamp),
    )
    return cur.lastrowid


def _insert_hall_row(conn, *, hall, compressed, layer, now, untrusted,
                     source_memory_id=None, admission=None) -> int:
    """THE only place a palace_halls row is INSERTed (01B-3 chokepoint); same
    admission rule as _insert_closet_row(). source_memory_id is written only
    when given, exactly as before."""
    layer = _strict_layer(layer)
    if layer < 2:
        stamp = _require_admission(admission, "hall", layer)
    elif admission is not None:
        raise PrivilegedLayerRefused("an admission is only meaningful for layer < 2")
    else:
        stamp = None
    columns = ["hall", "compressed", "layer", "created_at", "untrusted"]
    values = [hall, compressed, layer, now, 1 if untrusted else 0]
    if source_memory_id is not None:
        columns.append("source_memory_id")
        values.append(source_memory_id)
    if stamp is not None:
        columns.append("admission")
        values.append(stamp)
    cur = conn.execute(
        f"INSERT INTO palace_halls ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' * len(columns))})",
        values,
    )
    return cur.lastrowid


def _seed_l0(conn):
    """Plant the L0 identity core. ~50 tokens. Auto-populated from config.

    The one legitimate startup L0 write: it presents the startup-seed
    admission (L0 closet only) and its row is stamped
    admission='trusted_startup_seed'. Rows seeded by older builds stay NULL."""
    wing_id = conn.execute("SELECT id FROM palace_wings WHERE name='identity'").fetchone()["id"]
    room_id = _ensure_room(conn, wing_id, "core")

    agent = config.AGENT_NAME
    user  = config.USER_NAME
    now   = datetime.now().isoformat()

    compressed = (
        f"AGENT: {agent} | USER: {user} | "
        f"STACK: lmstudio+qwen3+pyside6 | PLATFORM: linux-mint | "
        f"MODE: local-first | LAYER: L0-identity"
    )
    token_est = estimate_tokens(compressed)

    _insert_closet_row(
        conn, room_id=room_id, layer=0, compressed=compressed, token_est=token_est,
        now=now, ever_untrusted=False, admission=_mint_startup_seed_admission(),
    )


# ── AAAK Compression Engine ────────────────────────────────────────────────────

def estimate_tokens(text: str) -> int:
    return max(1, len(str(text)) // 4)


# Abbreviation map — expands over time as Lumina uses the palace more
AAAK_ABBREV = {
    "project":      "PROJ",
    "preference":   "PREF",
    "discovery":    "DISC",
    "important":    "IMP",
    "conversation": "CONV",
    "session":      "SESS",
    "running":      "RUN",
    "completed":    "DONE",
    "in progress":  "WIP",
    "working on":   "WIP",
    # NOTE (UI-TRUST-01 follow-up, 2026-08-27): Bino/Lumina name
    # abbreviations were deliberately removed. They mangled identity
    # strings wherever AAAK ran -- including commit-trailer emails
    # (therealagentlumina@gmail.com -> therealagentLUM@gmail.com), which
    # then propagated onto real git commits. Identities are never
    # compressed.
    "local":        "LOC",
    "database":     "DB",
    "filesystem":   "FS",
    "terminal":     "TERM",
    "knowledge":    "KNW",
    "memory":       "MEM",
    "interface":    "UI",
    "python":       "PY",
    "function":     "FN",
    "because":      "b/c",
    "with":         "w/",
    "without":      "w/o",
    "between":      "btwn",
    "regarding":    "re:",
    "approximately":"~",
    "and":          "+",
    "also":         "+",
}


# Identifier-shaped spans protected from ALL compression (UI-TRUST-01
# follow-up, Sol 5.6 corrective): whole-word lookarounds stop shredding
# plain English words but cannot protect opaque identifiers, because
# '@', '.', '/', ':' are non-word boundaries -- e.g. "local"->LOC would
# legally match inside user@local.com or /srv/local/project. These spans
# are stashed verbatim before abbreviation and restored after.
_IDENTIFIER_SPANS = [
    # order matters: email before handle (both contain @),
    # url before path ("://" contains "/")
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",   # emails
    r"(?:https?|ftp)://[^\s]+",                              # URLs
    r"\bwww\.[^\s]+",                                        # bare www
    r"`[^`]+`",                                              # code spans
    r"(?:/[\w.\-]+)+/?",                                    # unix paths
    r"\b[0-9a-fA-F]{7,64}\b",                                # git SHAs / hashes
    r"(?<![\w@])@[A-Za-z0-9_]+",                             # @handles
]
_IDENTIFIER_RE = re.compile("|".join(_IDENTIFIER_SPANS))
_PLACEHOLDER_RE = re.compile(r"\x00(\d+)\x00")


def _mask_identifiers(text: str):
    """Stash identifier-shaped spans verbatim; return (masked_text, stash)."""
    stash = []

    def _keep(m):
        stash.append(m.group(0))
        return f"\x00{len(stash) - 1}\x00"

    return _IDENTIFIER_RE.sub(_keep, text), stash


def _unmask_identifiers(text: str, stash) -> str:
    return _PLACEHOLDER_RE.sub(lambda m: stash[int(m.group(1))], text)


def aaak_compress(text: str, label: str = None) -> str:
    """
    Compress text into AAAK format — AI-readable shorthand.
    ~30x reduction on verbose prose. No decoder needed; Lumina reads it natively.

    INVARIANT (UI-TRUST-01 follow-up, 2026-08-27): compression may abbreviate
    CONCEPTS only -- never proper names, identity fields, email addresses,
    usernames, URLs, hashes, or other opaque identifiers. Those must survive
    verbatim. Violations of this rule mangled co-author commit trailers for a
    month before being caught (see tests/test_aaak_identity_safety.py).
    All abbreviations are applied whole-word via lookarounds; word interiors
    are never touched. Identifier-shaped tokens (emails, URLs, paths, git
    SHAs, code spans, handles) are additionally stashed verbatim around the
    entire transformation -- see _IDENTIFIER_SPANS.

    Output examples:
      "Bino pref: dark-mode+vim-bindings | PROJ: Lumina(LOC.ai.agent) WIP"
      "DISC: qwen3 uses reasoning_content field NOT inline think tags"
      "SESS:2026-04-08 — added FS+sandbox+terminal+toolmaker tools; 20 tools total"
    """
    if not text:
        return ""

    result, stash = _mask_identifiers(text.strip())

    # Apply abbreviations (longest match first to avoid partial replacements).
    # Lookarounds keep matches whole-word: without them, "and"->"+" shredded
    # interior letters of unrelated words (standard -> st+ard, handoff ->
    # h+off, candidate -> c+idate, sandbox -> s+box) and substrings of names
    # and emails got rewritten too -- the root cause of the UI-TRUST-01
    # co-author-trailer corruption. Standalone words still compress normally.
    for full, abbr in sorted(AAAK_ABBREV.items(), key=lambda x: -len(x[0])):
        pattern = rf"(?<!\w){re.escape(full)}(?!\w)"
        result = re.sub(pattern, lambda _m, _a=abbr: _a, result, flags=re.IGNORECASE)

    # Collapse whitespace
    result = re.sub(r'\s+', ' ', result).strip()

    # Strip filler words
    fillers = r'\b(the|a|an|is|are|was|were|has|have|had|be|been|being|that|this|which|very|really|just|some|any)\b'
    result = re.sub(fillers, '', result, flags=re.IGNORECASE)
    result = re.sub(r'\s+', ' ', result).strip()

    # Prepend label if given
    if label:
        result = f"{label.upper()}: {result}"

    return _unmask_identifiers(result, stash)


def aaak_compress_list(items: list[str], label: str = None) -> str:
    """Compress a list of facts into a pipe-separated AAAK line."""
    compressed = [aaak_compress(item) for item in items if item.strip()]
    line = " | ".join(compressed)
    if label:
        line = f"{label.upper()}: {line}"
    return line


# ── Room/Wing Helpers ──────────────────────────────────────────────────────────

def _ensure_room(conn, wing_id: int, room_name: str) -> int:
    row = conn.execute(
        "SELECT id FROM palace_rooms WHERE wing_id=? AND name=?", (wing_id, room_name)
    ).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO palace_rooms (wing_id, name) VALUES (?, ?)", (wing_id, room_name)
    )
    return cur.lastrowid


def _get_wing_id(conn, wing_name: str) -> int | None:
    row = conn.execute("SELECT id FROM palace_wings WHERE name=?", (wing_name,)).fetchone()
    return row["id"] if row else None


_SYNTHESIZED_MEMORY_TAGS = frozenset({
    "dream-sweep", "auto-compaction", "manual-compaction",
})

# PALACE-GUARD-01B-2 closed vocabularies. withheld_reason: the only value
# this code ever writes. origin: stamped only by the internal synthesized
# writers (core/dreaming.py, core/manual_compaction.py, ui/main_window.py's
# auto-compaction) -- never reachable from a model tool argument -- mapped to
# the tag each writer also records.
WITHHELD_SOURCE_DELETED = "source_deleted_pending_review"
# PALACE-GUARD-01B-4: a drawer was extracted (owner promotion) from a rolling
# closet whose stored text no longer equals the render of its drawers. The
# reason describes why the REMAINING closet can't be trusted for rendering; the
# promotion's own provenance lives on the promoted row and its receipt.
WITHHELD_DRAWER_EXTRACTED = "drawer_extracted_pending_review"
WITHHELD_REASONS = frozenset({WITHHELD_SOURCE_DELETED, WITHHELD_DRAWER_EXTRACTED})
SYNTHESIZED_ORIGINS = {
    "dream_sweep": "dream-sweep",
    "auto_compaction": "auto-compaction",
    "manual_compaction": "manual-compaction",
}


def render_closet_text(label: str, drawers) -> str:
    """Pure: the closet text palace_store() would have produced for these
    drawers (each with "content" and "untrusted"), already in
    (created_at, id) order. Shared by _rebuild_closet_from_drawers() and
    PALACE-GUARD-01A's read-only render-drift rule, so the two can never
    disagree about what a closet "should" say."""
    from core.context import tag_untrusted
    segments = []
    for row in drawers:
        raw = aaak_compress(row["content"], label=label)
        segments.append(tag_untrusted(label, raw) if row["untrusted"] else raw)
    return " | ".join(segments)


def _rebuild_closet_from_drawers(conn, closet_id: int) -> None:
    """Rebuild one rolling closet from its drawers and their authority bits."""
    location = conn.execute(
        "SELECT w.name AS wing, r.name AS room, c.compressed, c.token_est, "
        "c.ever_had_untrusted_merge, c.withheld_reason "
        "FROM palace_closets c "
        "JOIN palace_rooms r ON c.room_id=r.id "
        "JOIN palace_wings w ON r.wing_id=w.id WHERE c.id=?",
        (closet_id,),
    ).fetchone()
    if location is None:
        return

    remaining = conn.execute(
        "SELECT content, untrusted FROM palace_drawers "
        "WHERE closet_id=? ORDER BY created_at, id",
        (closet_id,),
    ).fetchall()
    if not remaining:
        conn.execute("DELETE FROM palace_closets WHERE id=?", (closet_id,))
        return
    if location["withheld_reason"] is not None:
        # PALACE-GUARD-01B-2: a withheld closet is frozen until explicit
        # remediation -- never re-rendered (and never un-withheld) here.
        return

    label = f"{location['wing']}.{location['room']}"
    rebuilt = render_closet_text(label, remaining)
    token_est = estimate_tokens(rebuilt)
    ever_untrusted = 1 if any(row["untrusted"] for row in remaining) else 0
    if (
        location["compressed"] != rebuilt
        or location["token_est"] != token_est
        or location["ever_had_untrusted_merge"] != ever_untrusted
    ):
        conn.execute(
            "UPDATE palace_closets SET compressed=?, token_est=?, updated_at=?, "
            "ever_had_untrusted_merge=? WHERE id=?",
            (rebuilt, token_est, datetime.now().isoformat(), ever_untrusted, closet_id),
        )


def _extract_drawer_in_conn(conn, drawer_id: int, *, keep_row: bool = False,
                            withhold_reason: str = WITHHELD_SOURCE_DELETED) -> str:
    """PALACE-GUARD-01B-2 closet law, shared by deletion and (01B-4) promotion:
    take one drawer out of its closet and settle that closet, on a caller-owned
    write transaction. The closet is judged against its CURRENT drawers BEFORE
    anything changes:

    - withheld closet: drop the drawer; keep the closet withheld (never
      re-rendered, never un-withheld) unless it is now empty -> delete it.
    - single-drawer closet: drop drawer and closet.
    - multi-drawer, render-exact (stored text == render of its drawers):
      drop the drawer and rebuild from the rest -- every other segment is
      reproduced byte for byte.
    - multi-drawer, drifted: drop the drawer, do NOT splice or re-render the
      unrelated segments; mark the closet withheld (with `withhold_reason`)
      pending review.

    "Drop" is a DELETE of the drawer row by default (memory deletion). With
    keep_row=True the row survives, detached (closet_id NULL) for the caller to
    re-home -- that is the only difference; the closet decision is the same
    code. A drawer in an already-withheld closet is quarantined and is never
    extractable with keep_row=True.

    Returns one of: "no_closet", "withheld_closet_emptied",
    "withheld_kept", "closet_deleted", "closet_rebuilt", "closet_withheld".
    """
    if withhold_reason not in WITHHELD_REASONS:
        raise ValueError(f"unknown withheld reason {withhold_reason!r}")
    row = conn.execute(
        "SELECT closet_id FROM palace_drawers WHERE id=?", (drawer_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"drawer {drawer_id} not found")
    closet_id = row["closet_id"]

    def drop_drawer():
        if keep_row:
            conn.execute("UPDATE palace_drawers SET closet_id=NULL WHERE id=?", (drawer_id,))
        else:
            conn.execute("DELETE FROM palace_drawers WHERE id=?", (drawer_id,))

    if closet_id is None:
        drop_drawer()
        return "no_closet"

    closet = conn.execute(
        "SELECT c.compressed, c.withheld_reason, w.name AS wing, r.name AS room "
        "FROM palace_closets c JOIN palace_rooms r ON c.room_id=r.id "
        "JOIN palace_wings w ON r.wing_id=w.id WHERE c.id=?",
        (closet_id,),
    ).fetchone()
    before = conn.execute(
        "SELECT id, content, untrusted FROM palace_drawers "
        "WHERE closet_id=? ORDER BY created_at, id",
        (closet_id,),
    ).fetchall()
    remaining = [d for d in before if d["id"] != drawer_id]

    if closet is None or closet["withheld_reason"] is not None:
        if keep_row and closet is not None:
            raise ValueError(
                f"drawer {drawer_id} sits in a withheld closet; a quarantined drawer "
                "cannot be extracted"
            )
        drop_drawer()
        if not remaining:
            conn.execute("DELETE FROM palace_closets WHERE id=?", (closet_id,))
            return "withheld_closet_emptied"
        return "withheld_kept"

    if not remaining:
        drop_drawer()
        conn.execute("DELETE FROM palace_closets WHERE id=?", (closet_id,))
        return "closet_deleted"

    label = f"{closet['wing']}.{closet['room']}"
    exact = render_closet_text(label, before) == closet["compressed"]
    drop_drawer()
    if exact:
        rebuilt = render_closet_text(label, remaining)
        conn.execute(
            "UPDATE palace_closets SET compressed=?, token_est=?, updated_at=?, "
            "ever_had_untrusted_merge=? WHERE id=?",
            (rebuilt, estimate_tokens(rebuilt), datetime.now().isoformat(),
             1 if any(d["untrusted"] for d in remaining) else 0, closet_id),
        )
        return "closet_rebuilt"
    conn.execute(
        "UPDATE palace_closets SET withheld_reason=? WHERE id=?",
        (withhold_reason, closet_id),
    )
    return "closet_withheld"


def _remove_drawer_in_conn(conn, drawer_id: int) -> str:
    """PALACE-GUARD-01B-2: delete one drawer and settle its closet -- the
    keep_row=False form of _extract_drawer_in_conn(). This is the deletion
    entry point tools.memory and palace_undo_write call; behavior is exactly
    what it was before promotion existed."""
    return _extract_drawer_in_conn(conn, drawer_id)


def _promote_drawer_in_conn(conn, drawer_id: int, dest_layer: int, admission) -> dict:
    """PALACE-GUARD-01B-4: move ONE drawer to its own closet at a privileged
    layer, on a caller-owned write transaction. Only
    core.palace_promotion.promote_with_owner_approval() calls this, after it
    has revalidated the live record against the owner's approved snapshot.

    Layer lives on the closet, so promotion = (1) take the drawer out of its
    current closet under B2's exact law (_extract_drawer_in_conn with
    keep_row=True: single-drawer closet removed, render-exact closet rebuilt
    byte for byte from the survivors, drifted closet withheld untouched), then
    (2) give the SAME drawer row a fresh single-drawer closet at the destination
    (L0/L1 closets never merge), rendered by the same render_closet_text() every
    other closet uses and stamped with the promotion admission.

    Trust is never touched: the drawer's `untrusted` column is not written, and
    an untrusted drawer's new closet renders framed (tag_untrusted) with
    ever_had_untrusted_merge=1, exactly as a fresh untrusted write would."""
    _strict_layer(dest_layer)
    # Scope check before ANY mutation: wrong record/layer/kind fails here.
    _require_admission(admission, "closet", dest_layer, ("drawer", drawer_id))
    drawer = conn.execute(
        "SELECT d.id, d.room_id, d.content, d.untrusted, w.name AS wing, r.name AS room "
        "FROM palace_drawers d JOIN palace_rooms r ON r.id=d.room_id "
        "JOIN palace_wings w ON w.id=r.wing_id WHERE d.id=?",
        (drawer_id,),
    ).fetchone()
    if drawer is None:
        raise LookupError(f"drawer {drawer_id} not found")
    extraction = _extract_drawer_in_conn(
        conn, drawer_id, keep_row=True, withhold_reason=WITHHELD_DRAWER_EXTRACTED,
    )
    text = render_closet_text(f"{drawer['wing']}.{drawer['room']}", [drawer])
    closet_id = _insert_closet_row(
        conn, room_id=drawer["room_id"], layer=dest_layer, compressed=text,
        token_est=estimate_tokens(text), now=datetime.now().isoformat(),
        ever_untrusted=bool(drawer["untrusted"]), admission=admission,
        target=("drawer", drawer_id),
    )
    conn.execute("UPDATE palace_drawers SET closet_id=? WHERE id=?", (closet_id, drawer_id))
    return {"closet_id": closet_id, "extraction": extraction}


def _promote_hall_in_conn(conn, hall_id: int, dest_layer: int, admission) -> dict:
    """PALACE-GUARD-01B-4: raise ONE hall row to a privileged layer. Halls are
    atomic rows with their own layer, so this is a layer + admission stamp and
    nothing else: text, `untrusted`, source link and every other column stay as
    they are. It only ever moves a row toward a MORE privileged layer."""
    _strict_layer(dest_layer)
    stamp = _require_admission(admission, "hall", dest_layer, ("hall", hall_id))
    cur = conn.execute(
        "UPDATE palace_halls SET layer=?, admission=? WHERE id=? AND layer > ?",
        (dest_layer, stamp, hall_id, dest_layer),
    )
    if cur.rowcount != 1:
        raise LookupError(f"hall {hall_id} not found or already at/above layer {dest_layer}")
    return {}


def _migrate_synthesized_drawer_authority(conn) -> int:
    """Fail closed for model-authored durable summaries created by old builds.

    Returns the number of drawers changed, mainly for deterministic tests.
    Tags identify the trusted runtime write path here; they never promote
    authority.  This migration only moves matching rows in the safer
    direction (trusted -> untrusted).
    """
    rows = conn.execute(
        "SELECT id, closet_id, tags, untrusted FROM palace_drawers"
    ).fetchall()
    changed = 0
    affected_closets = set()
    for row in rows:
        try:
            tags = json.loads(row["tags"] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(tags, list):
            continue
        tag_set = {tag for tag in tags if isinstance(tag, str)}
        if not (_SYNTHESIZED_MEMORY_TAGS & tag_set):
            continue
        needs_update = not bool(row["untrusted"])
        if "trust:untrusted" not in tags:
            tags.append("trust:untrusted")
            needs_update = True
        if needs_update:
            conn.execute(
                "UPDATE palace_drawers SET untrusted=1, tags=? WHERE id=?",
                (json.dumps(tags), row["id"]),
            )
            changed += 1
        if row["closet_id"] is not None:
            affected_closets.add(row["closet_id"])

    for closet_id in affected_closets:
        _rebuild_closet_from_drawers(conn, closet_id)
    return changed


# ── Write API ──────────────────────────────────────────────────────────────────

def palace_store(
    content: str,
    wing: str = "sessions",
    room: str = "general",
    layer: int = 2,
    tags: list[str] = None,
    compress: bool = True,
    untrusted: bool = True,
    *,
    origin: str = None,
) -> dict:
    """
    Store a memory in the palace.
    - Saves verbatim original to a Drawer
    - If compress=True, creates/updates a Closet with AAAK-compressed version
    Returns {'closet_id': ..., 'drawer_id': ..., 'compressed': ..., 'tokens_saved': ...}

    untrusted (CASTLE-WALLS-REPAIR-01 R1D, default flipped fail-safe in
    REPAIR-03 / C8 finding 2 -- every current real caller already passes
    this explicitly, so the flip changes no live behavior, only what a
    future forgetful caller silently gets): True when THIS specific
    contribution's source material was not the owner's own words (e.g.
    summarized from an EXTERNAL_CHANNEL_INBOUND-tagged chat message).
    Closets are rolling, pipe-separated merges (see below) -- only the
    NEWLY-ADDED segment gets tag_untrusted()-wrapped here, never the prior
    segments already in an existing closet, so one lower-trust
    contribution can never retroactively discredit unrelated facts that
    merely happen to share the same closet (the earlier, rejected design
    made the whole closet sticky-untrusted on any merge, which would let
    an attacker who can never gain authority instead degrade the standing
    of legitimate owner knowledge -- a trust-destruction failure, not
    merely an over-conservative one). Also flips the closet's
    ever_had_untrusted_merge column (sticky, whole-closet) -- consulted
    ONLY by build_context_block(return_meta=True) to decide whether to
    fold ContextManager's soft "Provenance reminder" nudge on; it is never
    consulted to decide what text actually renders -- that's the
    per-segment tag's job alone.

    Model-native and autonomous callers (palace_remember, Dreaming,
    compaction) come through here and never carry a source link; only
    tools.memory.save_memory() links a drawer to its flat memory, inside its
    own transaction, via _store_in_conn().

    origin (PALACE-GUARD-01B-2, keyword-only): stamped ONLY by the internal
    synthesized writers -- core/dreaming.py ("dream_sweep"),
    core/manual_compaction.py ("manual_compaction"), and ui/main_window.py's
    auto-compaction ("auto_compaction"). No model tool can pass it: the
    registered palace_remember wrapper has a fixed signature without it.
    It is the provenance palace_undo_write() requires; tags alone can't
    prove synthesis, because palace_remember accepts model-chosen
    wing/room/tags and can reproduce any tag shape exactly.

    layer (PALACE-GUARD-01B-3): must be a real int and >= 2. This generic
    writer has no admission parameter and refuses L0/L1 outright with
    PrivilegedLayerRefused -- privileged placement is not something a caller
    can request here (see the admission section above). The model-facing
    palace_remember wrapper turns a request for L0/L1 into an L2 write plus a
    staged promotion request instead.
    """
    if origin is not None and origin not in SYNTHESIZED_ORIGINS:
        raise ValueError(f"unknown synthesized origin {origin!r}")
    conn = get_db()
    try:
        result = _store_in_conn(conn, content, wing, room, layer, tags, compress, untrusted,
                                origin=origin)
        conn.commit()
    finally:
        conn.close()
    return result


def _store_in_conn(conn, content, wing, room, layer, tags, compress, untrusted,
                   source_memory_id=None, origin=None) -> dict:
    """palace_store()'s body on a caller-owned connection: no commit, no
    close -- the caller's transaction decides. source_memory_id (PALACE-
    GUARD-01B-1) and origin (01B-2) are written only when given, so a caller
    passing neither writes exactly the columns and values it did before."""
    from core.context import tag_untrusted
    # PALACE-GUARD-01B-3: refuse before ANY row is written (wing, room, drawer),
    # so a refused write leaves nothing behind even if the caller never rolls back.
    layer = _refuse_generic_privileged_layer(layer)
    now = datetime.now().isoformat()

    wing_id = _get_wing_id(conn, wing)
    if not wing_id:
        # Auto-create unknown wings
        cur = conn.execute("INSERT INTO palace_wings (name) VALUES (?)", (wing,))
        wing_id = cur.lastrowid

    room_id = _ensure_room(conn, wing_id, room)

    # Save verbatim drawer. "trust:untrusted" tag is observability only
    # (palace_recall() display) -- the untrusted bool argument above, not
    # this tag string, is what actually drives the closet/hall flags below.
    drawer_tags = list(tags or [])
    if untrusted and "trust:untrusted" not in drawer_tags:
        drawer_tags.append("trust:untrusted")
    columns = ["room_id", "content", "tags", "untrusted", "created_at"]
    values = [room_id, content, json.dumps(drawer_tags), 1 if untrusted else 0, now]
    if source_memory_id is not None:
        columns.append("source_memory_id")
        values.append(source_memory_id)
    if origin is not None:
        columns.append("origin")
        values.append(origin)
    drawer_cur = conn.execute(
        f"INSERT INTO palace_drawers ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' * len(columns))})",
        values,
    )
    drawer_id = drawer_cur.lastrowid

    closet_id = None
    compressed = None
    tokens_saved = 0

    if compress:
        label = f"{wing}.{room}"
        raw_segment = aaak_compress(content, label=label)
        segment = tag_untrusted(label, raw_segment) if untrusted else raw_segment
        token_est = estimate_tokens(raw_segment)
        orig_tokens = estimate_tokens(content)
        tokens_saved = max(0, orig_tokens - token_est)

        # Check if a closet already exists for this room+layer — update it (rolling summary)
        existing = conn.execute(
            "SELECT id, compressed, ever_had_untrusted_merge FROM palace_closets "
            "WHERE room_id=? AND layer=? AND withheld_reason IS NULL",
            (room_id, layer)
        ).fetchone()

        if existing and layer >= 2:
            # Append to existing closet (pipe-separated AAAK facts) --
            # existing["compressed"] (every prior segment) is carried
            # through byte-for-byte, untouched.
            merged = existing["compressed"] + " | " + segment
            merged_token_est = estimate_tokens(merged)
            ever_untrusted = 1 if (existing["ever_had_untrusted_merge"] or untrusted) else 0
            conn.execute(
                "UPDATE palace_closets SET compressed=?, token_est=?, updated_at=?, "
                "ever_had_untrusted_merge=? WHERE id=?",
                (merged, merged_token_est, now, ever_untrusted, existing["id"])
            )
            closet_id = existing["id"]
            compressed = merged
        else:
            closet_id = _insert_closet_row(
                conn, room_id=room_id, layer=layer, compressed=segment,
                token_est=token_est, now=now, ever_untrusted=untrusted,
            )
            compressed = segment

        # Link drawer to closet
        conn.execute("UPDATE palace_drawers SET closet_id=? WHERE id=?", (closet_id, drawer_id))

    return {
        "closet_id": closet_id,
        "drawer_id": drawer_id,
        "compressed": compressed,
        "tokens_saved": tokens_saved,
    }


def palace_store_hall(content: str, hall: str = "facts", layer: int = 2,
                       untrusted: bool = True) -> int:
    """Store a cross-cutting fact into a Hall (events, facts, preferences,
    discoveries, advice).

    untrusted (CASTLE-WALLS-REPAIR-01 R1D, default flipped fail-safe in
    REPAIR-03 / C8 finding 2 -- every current real caller already passes
    this explicitly, so the flip changes no live behavior, only what a
    future forgetful caller silently gets): unlike palace_store()'s
    closets, every call here INSERTs a fresh, independent row -- there is
    no merge/append, so each hall fact is already atomic and a plain
    per-row flag (no segment-tagging needed) is correct. The row's own
    compressed text is tag_untrusted()-wrapped when true.

    layer (PALACE-GUARD-01B-3): a real int, >= 2; L0/L1 is refused with
    PrivilegedLayerRefused exactly as in palace_store()."""
    conn = get_db()
    try:
        hall_id = _store_hall_in_conn(conn, content, hall, layer, untrusted)
        conn.commit()
    finally:
        conn.close()
    return hall_id


def _store_hall_in_conn(conn, content, hall, layer, untrusted, source_memory_id=None) -> int:
    """palace_store_hall()'s body on a caller-owned connection (no commit,
    no close). source_memory_id is written only when given.

    PALACE-GUARD-01B-3: like _store_in_conn(), refuses layer < 2 outright --
    a Hall is not a way around the drawer boundary."""
    from core.context import tag_untrusted
    layer = _refuse_generic_privileged_layer(layer)
    raw = aaak_compress(content, label=hall)
    compressed = tag_untrusted(hall, raw) if untrusted else raw
    now = datetime.now().isoformat()
    return _insert_hall_row(
        conn, hall=hall, compressed=compressed, layer=layer, now=now,
        untrusted=untrusted, source_memory_id=source_memory_id,
    )


# ── Load API ───────────────────────────────────────────────────────────────────

def load_layer(layer: int) -> list[dict]:
    """
    Load all closets at a given layer.
    Returns list of {'wing', 'room', 'compressed', 'token_est',
    'ever_had_untrusted_merge'}
    """
    conn = get_db()
    rows = conn.execute("""
        SELECT c.id, c.compressed, c.token_est, c.updated_at,
               c.ever_had_untrusted_merge,
               r.name as room, w.name as wing
        FROM palace_closets c
        JOIN palace_rooms r ON c.room_id = r.id
        JOIN palace_wings w ON r.wing_id = w.id
        WHERE c.layer = ? AND c.withheld_reason IS NULL
        ORDER BY w.name, r.name
    """, (layer,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def load_halls(layer: int) -> list[dict]:
    """Load hall facts at a specific layer — mirrors load_layer()'s
    exact-layer model rather than a cumulative layer_max. (FE-03: the old
    layer_max<=N semantics duplicated L0 halls into the L1 injection block,
    since layer_max=1 matches layer 0 AND layer 1 rows. Exact-layer matching
    can't double-count across the build_context_block() loop the way a
    cumulative filter silently did.)"""
    conn = get_db()
    rows = conn.execute(
        "SELECT hall, compressed, untrusted FROM palace_halls WHERE layer = ? "
        "ORDER BY created_at DESC LIMIT 30",
        (layer,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _find_pinned_closet_ids(pin_tag: str) -> set[int]:
    """Closet ids with at least one drawer carrying pin_tag. Tags live on
    palace_drawers, not palace_closets — same tags-LIKE-match join pattern
    as list_flagged_writes(), just resolving to closet_id instead of
    returning the drawer rows themselves."""
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT d.closet_id FROM palace_drawers d "
        "JOIN palace_closets c ON c.id = d.closet_id "
        "WHERE d.tags LIKE ? AND c.withheld_reason IS NULL",
        (f'%"{pin_tag}"%',)
    ).fetchall()
    conn.close()
    return {r["closet_id"] for r in rows}


def build_context_block(max_tokens: int = 400, inject_limit: int = None, pin_tag: str = None,
                        return_meta: bool = False):
    """
    Build the memory injection block for the system prompt.
    Always loads L0 + L1 in full. Loads L2 (recent/episodic) up to two caps:
    token budget (max_tokens) and count (inject_limit) — whichever hits first.
    inject_limit only applies to L2: L0 identity and L1 critical facts are
    small, curated, and meant to always be present regardless of this knob.
    A low inject_limit on a small local context window keeps a handful of the
    most relevant recent memories instead of diluting a tight budget across
    many low-value fragments; raising it (e.g. on a large cloud context) lets
    substantially more episodic history ride along even when token budget
    isn't the binding constraint.

    pin_tag (MB-11): when set, any L2 closet with a drawer carrying this tag
    (e.g. "session:{chat_id}") is included unconditionally, ahead of the
    normal decay-sorted L2 selection and exempt from inject_limit — same
    always-present treatment as L0/L1. This is how the current session's
    rolling nightstand closet resurfaces on reopen regardless of what else
    is competing for the L2 slot count. Still counted against max_tokens
    like everything else here; only inject_limit is bypassed. None (default)
    reproduces prior behavior exactly.

    return_meta (CASTLE-WALLS-REPAIR-01 R1D): False (default) returns the
    block as a plain str exactly as before -- every existing caller/test
    is untouched. True returns (str, any_untrusted) instead, where
    any_untrusted is True iff any closet actually included in this render
    ever had an untrusted merge, or any hall included is itself untrusted
    -- consulted ONLY to decide whether to fold ContextManager's soft
    "Provenance reminder" nudge on. It is never used to decide what text
    renders; that's already correctly framed per-segment/per-row by the
    tag_untrusted() wrapping palace_store()/palace_store_hall() applied at
    write time (see those functions' docstrings).

    Returns a compact string ready to append to system prompt.
    """
    lines = ["## Memory Palace"]
    tokens_used = 4
    any_untrusted = False

    for layer in [0, 1, 2]:
        closets = load_layer(layer)
        halls   = load_halls(layer)

        layer_label = ["L0:Identity", "L1:Critical", "L2:Recent"][layer]
        layer_lines = []
        # Apply temporal decay ordering to L2 — most recently updated closets first
        if layer == 2:
            if pin_tag:
                pinned_ids = _find_pinned_closet_ids(pin_tag)
                pinned = [c for c in closets if c["id"] in pinned_ids]
                rest   = [c for c in closets if c["id"] not in pinned_ids]
            else:
                pinned, rest = [], closets
            rest = decay_engine.sort_by_recency(rest)
            if inject_limit is not None:
                rest = rest[:inject_limit]
            closets = pinned + rest

        for c in closets:
            tok = c["token_est"] or estimate_tokens(c["compressed"])
            if tokens_used + tok > max_tokens:
                break
            layer_lines.append(c["compressed"])
            tokens_used += tok
            if c.get("ever_had_untrusted_merge"):
                any_untrusted = True

        for h in halls:
            tok = estimate_tokens(h["compressed"])
            if tokens_used + tok > max_tokens:
                break
            layer_lines.append(h["compressed"])
            tokens_used += tok
            if h.get("untrusted"):
                any_untrusted = True

        if layer_lines:
            lines.append(f"[{layer_label}]")
            lines.extend(layer_lines)

        if tokens_used >= max_tokens:
            break

    if len(lines) == 1:
        block = ""  # Nothing stored yet — don't inject empty block
    else:
        block = "\n".join(lines)

    return (block, any_untrusted) if return_meta else block


# ── Recall (L3 search) ─────────────────────────────────────────────────────────

def palace_recall(query: str, wing: str = None, limit: int = 5) -> str:
    """
    Search verbatim Drawer contents (L3).
    Returns formatted results with source wing/room.

    PALACE-GUARD-01B-2: drawers of a lifecycle-withheld closet never surface
    here (nor in build_context_block / palace_review_writes) until explicit
    remediation -- the whole closet is pending owner review.
    """
    conn = get_db()
    if wing:
        wing_id = _get_wing_id(conn, wing)
        if not wing_id:
            conn.close()
            return f"Wing '{wing}' not found."
        rows = conn.execute("""
            SELECT d.id, d.content, d.tags, d.created_at, r.name as room, w.name as wing
            FROM palace_drawers d
            JOIN palace_rooms r ON d.room_id = r.id
            JOIN palace_wings w ON r.wing_id = w.id
            LEFT JOIN palace_closets c ON c.id = d.closet_id
            WHERE (d.closet_id IS NULL OR c.withheld_reason IS NULL) AND w.id=? AND d.content LIKE ?
            ORDER BY d.created_at DESC LIMIT ?
        """, (wing_id, f"%{query}%", limit)).fetchall()
    else:
        rows = conn.execute("""
            SELECT d.id, d.content, d.tags, d.created_at, r.name as room, w.name as wing
            FROM palace_drawers d
            JOIN palace_rooms r ON d.room_id = r.id
            JOIN palace_wings w ON r.wing_id = w.id
            LEFT JOIN palace_closets c ON c.id = d.closet_id
            WHERE (d.closet_id IS NULL OR c.withheld_reason IS NULL) AND d.content LIKE ?
            ORDER BY d.created_at DESC LIMIT ?
        """, (f"%{query}%", limit)).fetchall()
    conn.close()

    if not rows:
        return f"No memories found for '{query}'."

    out = []
    for r in rows:
        tags = json.loads(r["tags"]) if r["tags"] else []
        tag_str = f" [{', '.join(tags)}]" if tags else ""
        out.append(f"[{r['wing']}/{r['room']}]{tag_str} {r['content'][:300]}")
    return "\n".join(out)


def palace_status() -> str:
    """Return a compact status summary of the palace."""
    conn = get_db()
    wings   = conn.execute("SELECT COUNT(*) as c FROM palace_wings").fetchone()["c"]
    rooms   = conn.execute("SELECT COUNT(*) as c FROM palace_rooms").fetchone()["c"]
    closets = conn.execute("SELECT COUNT(*) as c FROM palace_closets").fetchone()["c"]
    drawers = conn.execute("SELECT COUNT(*) as c FROM palace_drawers").fetchone()["c"]
    halls   = conn.execute("SELECT COUNT(*) as c FROM palace_halls").fetchone()["c"]
    total_tok = conn.execute("SELECT SUM(token_est) as t FROM palace_closets").fetchone()["t"] or 0
    conn.close()
    return (
        f"Palace: {wings} wings | {rooms} rooms | {closets} closets | "
        f"{drawers} drawers | {halls} hall entries | ~{total_tok} ctx tokens loaded"
    )

def list_flagged_writes(tag: str = "dream-sweep", limit: int = 20) -> list[dict]:
    """List recent auto-writes by tag, for review before deciding to undo."""
    conn = get_db()
    rows = conn.execute("""
        SELECT d.id as drawer_id, d.content, d.tags, d.created_at, d.closet_id,
               r.name as room, w.name as wing
        FROM palace_drawers d
        JOIN palace_rooms r ON d.room_id = r.id
        JOIN palace_wings w ON r.wing_id = w.id
        LEFT JOIN palace_closets c ON c.id = d.closet_id
        WHERE (d.closet_id IS NULL OR c.withheld_reason IS NULL) AND d.tags LIKE ?
        ORDER BY d.created_at DESC LIMIT ?
    """, (f'%"{tag}"%', limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _undo_refusal(conn, drawer) -> str | None:
    """Why palace_undo_write must refuse this drawer, or None when its
    provenance PROVES it is a synthesized nightstand write. The trusted
    `origin` stamp is the proof; everything else is a consistency check. A
    caller-supplied id, a tag shape, or a docstring is never provenance on
    its own: palace_remember can write any wing/room/tags, so a model-native
    drawer can mirror a dream/compaction drawer exactly -- except for origin.
    Legacy drawers written before origin existed are NULL, i.e. ambiguous,
    and are refused (fail closed)."""
    if drawer["source_memory_id"] is not None:
        return "linked_to_source_memory"
    origin = drawer["origin"]
    if origin not in SYNTHESIZED_ORIGINS:
        return "not_provably_synthesized"
    loc = conn.execute(
        "SELECT w.name AS wing, r.name AS room FROM palace_rooms r "
        "JOIN palace_wings w ON r.wing_id = w.id WHERE r.id=?",
        (drawer["room_id"],),
    ).fetchone()
    room = loc["room"] if loc else None
    if (loc is None or loc["wing"] != "nightstand" or not isinstance(room, str)
            or not (room.isascii() and room.isdecimal())):
        return "not_in_a_nightstand_chat_room"
    try:
        tags = json.loads(drawer["tags"] or "[]")
    except (TypeError, ValueError):
        return "provenance_inconsistent"
    if (not isinstance(tags, list) or SYNTHESIZED_ORIGINS[origin] not in tags
            or f"session:{room}" not in tags or not drawer["untrusted"]):
        return "provenance_inconsistent"
    if drawer["closet_id"] is None:
        return "provenance_inconsistent"
    closet = conn.execute(
        "SELECT layer FROM palace_closets WHERE id=?", (drawer["closet_id"],)
    ).fetchone()
    if closet is None or closet["layer"] != 2:
        return "privileged_or_unknown_layer"
    return None


def palace_undo_write(drawer_id: int) -> dict:
    """
    Delete a single synthesized nightstand write (dream sweep or compaction)
    and settle its parent closet under the same law as memory deletion
    (PALACE-GUARD-01B-2, _remove_drawer_in_conn): rebuild only when the
    closet is render-exact, otherwise withhold it pending review.

    Refuses -- without changing anything -- every drawer whose provenance
    doesn't prove it is synthesized nightstand material: source-linked
    memory drawers, model-native drawers (even ones written into nightstand
    with dream/compaction-shaped tags), legacy drawers with no origin stamp,
    and anything outside layer 2. See _undo_refusal().
    """
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        drawer = conn.execute("SELECT * FROM palace_drawers WHERE id=?", (drawer_id,)).fetchone()
        if not drawer:
            conn.rollback()
            return {"ok": False, "error": "drawer not found"}
        reason = _undo_refusal(conn, drawer)
        if reason:
            conn.rollback()
            return {"ok": False, "error": f"refused: {reason}"}
        closet_id = drawer["closet_id"]
        outcome = _remove_drawer_in_conn(conn, drawer_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"ok": True, "closet_id": closet_id, "drawer_id": drawer_id, "closet_outcome": outcome}

# ── Model-facing layer requests (PALACE-GUARD-01B-3) ───────────────────────────

_MODEL_LAYER_LIMIT = 2 ** 31  # nothing near this is a layer; it can't be stored as one


def _strict_model_layer(raw) -> int | None:
    """The model's layer argument is understood ONLY as a real int in a sane
    range. Tool arguments arrive as JSON and SQLite would store True, 1.0, "1"
    or " 1 " as integer 1, so none of those is allowed to mean "layer 1" -- in
    particular none may mean "the owner should review this for promotion".
    Returns the int, or None for bool, float, str, None, containers, and any
    out-of-range value."""
    if type(raw) is not int:
        return None
    if not -_MODEL_LAYER_LIMIT <= raw < _MODEL_LAYER_LIMIT:
        return None
    return raw


def _admit_model_layer(raw) -> tuple[int, int | None, str]:
    """A model-requested layer is a *requested destination*, never an
    admission. Returns (layer_to_write, requested_privileged_layer_or_None,
    warning). Only an exact int 0 or 1 is written at L2 and remembered as a
    promotion request; any other in-range int passes through exactly as
    before; everything else (bool, 1.0, "1", junk, oversized) falls back to
    L2 with a warning and NEVER stages a request."""
    layer = _strict_model_layer(raw)
    if layer is None:
        return 2, None, " (layer must be an integer; stored at L2)"
    if layer in PRIVILEGED_LAYERS:
        return 2, layer, ""
    if layer < 0:
        return 2, None, " (invalid layer; stored at L2)"
    return layer, None, ""


def _stage_privileged_request(target_kind: str, target_id: int, requested: int | None) -> str:
    """After the L2 write has committed, stage the requested L0/L1 destination
    as a promotion request for that exact record. Returns the sentence the
    model is shown. A staging failure never un-writes the L2 memory and is
    reported truthfully."""
    if requested is None:
        return ""
    try:
        from tools.pending_actions import stage_palace_promotion
        action_id = stage_palace_promotion(target_kind, target_id, requested)
    except Exception as e:
        return (f" Requested L{requested} was NOT granted and could not be staged for "
                f"owner review ({type(e).__name__}); the memory is stored at L2.")
    return (f" Requested L{requested} was NOT granted: L0/L1 placement needs the owner's "
            f"explicit approval. Staged as promotion request #{action_id} "
            f"(Settings > Tools > Pending Actions); nothing is promoted until then.")


def _palace_remember_tool(content, wing="sessions", room="general", layer=2, tags=None) -> str:
    write_layer, requested, warning = _admit_model_layer(layer)
    r = palace_store(content, wing, room, write_layer, tags, untrusted=True)
    note = _stage_privileged_request("drawer", r["drawer_id"], requested)
    return (f"Stored in {wing}/{room} (L{write_layer}){warning}.{note} "
            f"Compressed: {r['compressed']} | Saved ~{r['tokens_saved']} tokens.")


def _palace_hall_tool(content, hall="facts", layer=2) -> str:
    write_layer, requested, warning = _admit_model_layer(layer)
    hall_id = palace_store_hall(content, hall, write_layer, untrusted=True)
    note = _stage_privileged_request("hall", hall_id, requested)
    return f"Hall entry stored: {hall}/#{hall_id} (L{write_layer}){warning}.{note}"


# ── Tool Registration ──────────────────────────────────────────────────────────

def register_palace_tools(registry):
    init_palace_db()

    registry.register(
        name="palace_remember",
        fn=_palace_remember_tool,
        description="Store a memory in the palace. Wing options: identity, projects, people, preferences, sessions. Layers 0 and 1 (always-injected) are never granted by this tool: a request for them stores the memory at layer 2 and stages a promotion request for the owner to approve.",
        parameters={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The memory to store."},
                "wing":    {"type": "string", "description": "Wing: identity|projects|people|preferences|sessions", "default": "sessions"},
                "room":    {"type": "string", "description": "Sub-topic room name.", "default": "general"},
                "layer":   {"type": "integer", "description": "Requested layer: 2=recent 3=deep. 0=identity and 1=critical are owner-granted only: asking for them stores at layer 2 and stages a promotion request.", "default": 2},
                "tags":    {"type": "array", "items": {"type": "string"}, "description": "Optional search tags."}
            },
            "required": ["content"]
        }
    )

    registry.register(
        name="palace_hall",
        fn=_palace_hall_tool,
        description="Store a cross-cutting fact in a Hall: facts|events|preferences|discoveries|advice. Layers 0 and 1 are never granted by this tool: a request for them stores the entry at layer 2 and stages a promotion request for the owner to approve.",
        parameters={
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "hall":    {"type": "string", "description": "facts|events|preferences|discoveries|advice", "default": "facts"},
                "layer":   {"type": "integer", "description": "Requested layer (default 2). 0/1 are owner-granted only: they store at layer 2 and stage a promotion request.", "default": 2}
            },
            "required": ["content"]
        }
    )

    registry.register(
        name="palace_recall",
        fn=palace_recall,
        description="Search verbatim memories (L3 deep recall) by keyword, optionally in a specific wing.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "wing":  {"type": "string", "description": "Optional: identity|projects|people|preferences|sessions"},
                "limit": {"type": "integer", "default": 5}
            },
            "required": ["query"]
        }
    )

    registry.register(
        name="palace_status",
        fn=palace_status,
        description="Show palace memory stats — wings, rooms, closets, drawers, total tokens.",
        parameters={"type": "object", "properties": {}, "required": []}
    )

    registry.register(
        name="palace_review_writes",
        fn=lambda tag="dream-sweep": (
            lambda writes: "\n".join(
                f"[{w['drawer_id']}] {w['created_at']} ({w['wing']}/{w['room']}): {w['content'][:120]}"
                for w in writes
            ) if writes else f"No entries tagged '{tag}' found."
        )(list_flagged_writes(tag)),
        description="List recent auto-written memory entries (dream-sweeps or compactions) for review before deciding whether to undo one.",
        parameters={
            "type": "object",
            "properties": {
                "tag": {"type": "string", "description": "Which auto-write type to review: 'dream-sweep' or 'auto-compaction'.", "default": "dream-sweep"}
            },
            "required": []
        }
    )

    registry.register(
        name="palace_undo_write",
        fn=lambda drawer_id: (
            lambda r: (f"Undone — drawer {r['drawer_id']} removed "
                       f"(closet {r['closet_id']}: {r['closet_outcome']}).") if r["ok"]
            else f"[Error: {r.get('error')}]"
        )(palace_undo_write(drawer_id)),
        description="Delete a single synthesized nightstand write (a dream sweep or compaction, by drawer_id from palace_review_writes). Anything else -- memories, notes stored with palace_remember, or older writes without a provenance stamp -- is refused.",
        parameters={
            "type": "object",
            "properties": {
                "drawer_id": {"type": "integer", "description": "The drawer_id shown in brackets from palace_review_writes output."}
            },
            "required": ["drawer_id"]
        }
    )
