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
  L2  ~300 tok — Recent sessions + active projects. Loaded at session start.
  L3  unlimited — Full verbatim originals. Searched on demand via recall tool.
"""

import sqlite3
import json
import re
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
            created_at TEXT NOT NULL,
            FOREIGN KEY (room_id)   REFERENCES palace_rooms(id)   ON DELETE CASCADE,
            FOREIGN KEY (closet_id) REFERENCES palace_closets(id) ON DELETE SET NULL
        )
    """)

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

    conn.commit()
    conn.close()


def _seed_l0(conn):
    """Plant the L0 identity core. ~50 tokens. Auto-populated from config."""
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

    conn.execute(
        "INSERT INTO palace_closets (room_id, layer, compressed, token_est, created_at, updated_at) VALUES (?,?,?,?,?,?)",
        (room_id, 0, compressed, token_est, now, now)
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
    "Bino":         "BIN",
    "Lumina":       "LUM",
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


def aaak_compress(text: str, label: str = None) -> str:
    """
    Compress text into AAAK format — AI-readable shorthand.
    ~30x reduction on verbose prose. No decoder needed; Lumina reads it natively.

    Output examples:
      "BIN pref: dark-mode+vim-bindings | PROJ: LUMINA(LOC.ai.agent) WIP"
      "DISC: qwen3 uses reasoning_content field NOT inline think tags"
      "SESS:2026-04-08 — added FS+sandbox+terminal+toolmaker tools; 20 tools total"
    """
    if not text:
        return ""

    result = text.strip()

    # Apply abbreviations (longest match first to avoid partial replacements)
    for full, abbr in sorted(AAAK_ABBREV.items(), key=lambda x: -len(x[0])):
        result = re.sub(re.escape(full), abbr, result, flags=re.IGNORECASE)

    # Collapse whitespace
    result = re.sub(r'\s+', ' ', result).strip()

    # Strip filler words
    fillers = r'\b(the|a|an|is|are|was|were|has|have|had|be|been|being|that|this|which|very|really|just|some|any)\b'
    result = re.sub(fillers, '', result, flags=re.IGNORECASE)
    result = re.sub(r'\s+', ' ', result).strip()

    # Prepend label if given
    if label:
        result = f"{label.upper()}: {result}"

    return result


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


# ── Write API ──────────────────────────────────────────────────────────────────

def palace_store(
    content: str,
    wing: str = "sessions",
    room: str = "general",
    layer: int = 2,
    tags: list[str] = None,
    compress: bool = True,
) -> dict:
    """
    Store a memory in the palace.
    - Saves verbatim original to a Drawer
    - If compress=True, creates/updates a Closet with AAAK-compressed version
    Returns {'closet_id': ..., 'drawer_id': ..., 'compressed': ..., 'tokens_saved': ...}
    """
    conn = get_db()
    now = datetime.now().isoformat()

    wing_id = _get_wing_id(conn, wing)
    if not wing_id:
        # Auto-create unknown wings
        cur = conn.execute("INSERT INTO palace_wings (name) VALUES (?)", (wing,))
        wing_id = cur.lastrowid

    room_id = _ensure_room(conn, wing_id, room)

    # Save verbatim drawer
    drawer_cur = conn.execute(
        "INSERT INTO palace_drawers (room_id, content, tags, created_at) VALUES (?,?,?,?)",
        (room_id, content, json.dumps(tags or []), now)
    )
    drawer_id = drawer_cur.lastrowid

    closet_id = None
    compressed = None
    tokens_saved = 0

    if compress:
        label = f"{wing}.{room}"
        compressed = aaak_compress(content, label=label)
        token_est = estimate_tokens(compressed)
        orig_tokens = estimate_tokens(content)
        tokens_saved = max(0, orig_tokens - token_est)

        # Check if a closet already exists for this room+layer — update it (rolling summary)
        existing = conn.execute(
            "SELECT id, compressed FROM palace_closets WHERE room_id=? AND layer=?",
            (room_id, layer)
        ).fetchone()

        if existing and layer >= 2:
            # Append to existing closet (pipe-separated AAAK facts)
            merged = existing["compressed"] + " | " + aaak_compress(content)
            token_est = estimate_tokens(merged)
            conn.execute(
                "UPDATE palace_closets SET compressed=?, token_est=?, updated_at=? WHERE id=?",
                (merged, token_est, now, existing["id"])
            )
            closet_id = existing["id"]
        else:
            closet_cur = conn.execute(
                "INSERT INTO palace_closets (room_id, layer, compressed, token_est, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (room_id, layer, compressed, token_est, now, now)
            )
            closet_id = closet_cur.lastrowid

        # Link drawer to closet
        conn.execute("UPDATE palace_drawers SET closet_id=? WHERE id=?", (closet_id, drawer_id))

    conn.commit()
    conn.close()

    return {
        "closet_id": closet_id,
        "drawer_id": drawer_id,
        "compressed": compressed,
        "tokens_saved": tokens_saved,
    }


def palace_store_hall(content: str, hall: str = "facts", layer: int = 2) -> int:
    """Store a cross-cutting fact into a Hall (events, facts, preferences, discoveries, advice)."""
    conn = get_db()
    compressed = aaak_compress(content, label=hall)
    cur = conn.execute(
        "INSERT INTO palace_halls (hall, compressed, layer, created_at) VALUES (?,?,?,?)",
        (hall, compressed, layer, datetime.now().isoformat())
    )
    hall_id = cur.lastrowid
    conn.commit()
    conn.close()
    return hall_id


# ── Load API ───────────────────────────────────────────────────────────────────

def load_layer(layer: int) -> list[dict]:
    """
    Load all closets at a given layer.
    Returns list of {'wing', 'room', 'compressed', 'token_est'}
    """
    conn = get_db()
    rows = conn.execute("""
        SELECT c.id, c.compressed, c.token_est, c.updated_at,
               r.name as room, w.name as wing
        FROM palace_closets c
        JOIN palace_rooms r ON c.room_id = r.id
        JOIN palace_wings w ON r.wing_id = w.id
        WHERE c.layer = ?
        ORDER BY w.name, r.name
    """, (layer,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def load_halls(layer_max: int = 1) -> list[dict]:
    """Load hall facts up to a given layer."""
    conn = get_db()
    rows = conn.execute(
        "SELECT hall, compressed FROM palace_halls WHERE layer <= ? ORDER BY created_at DESC LIMIT 30",
        (layer_max,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def build_context_block(max_tokens: int = 400) -> str:
    """
    Build the memory injection block for the system prompt.
    Always loads L0 + L1. Loads L2 if tokens allow.
    Returns a compact string ready to append to system prompt.
    """
    lines = ["## Memory Palace"]
    tokens_used = 4

    for layer in [0, 1, 2]:
        closets = load_layer(layer)
        halls   = load_halls(layer_max=layer) if layer <= 1 else []

        layer_label = ["L0:Identity", "L1:Critical", "L2:Recent"][layer]
        layer_lines = []
        # Apply temporal decay ordering to L2 — most recently updated closets first
        if layer == 2:
            closets = decay_engine.sort_by_recency(closets)

        for c in closets:
            tok = c["token_est"] or estimate_tokens(c["compressed"])
            if tokens_used + tok > max_tokens:
                break
            layer_lines.append(c["compressed"])
            tokens_used += tok

        for h in halls:
            tok = estimate_tokens(h["compressed"])
            if tokens_used + tok > max_tokens:
                break
            layer_lines.append(h["compressed"])
            tokens_used += tok

        if layer_lines:
            lines.append(f"[{layer_label}]")
            lines.extend(layer_lines)

        if tokens_used >= max_tokens:
            break

    if len(lines) == 1:
        return ""  # Nothing stored yet — don't inject empty block

    return "\n".join(lines)


# ── Recall (L3 search) ─────────────────────────────────────────────────────────

def palace_recall(query: str, wing: str = None, limit: int = 5) -> str:
    """
    Search verbatim Drawer contents (L3).
    Returns formatted results with source wing/room.
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
            WHERE w.id=? AND d.content LIKE ?
            ORDER BY d.created_at DESC LIMIT ?
        """, (wing_id, f"%{query}%", limit)).fetchall()
    else:
        rows = conn.execute("""
            SELECT d.id, d.content, d.tags, d.created_at, r.name as room, w.name as wing
            FROM palace_drawers d
            JOIN palace_rooms r ON d.room_id = r.id
            JOIN palace_wings w ON r.wing_id = w.id
            WHERE d.content LIKE ?
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
        WHERE d.tags LIKE ?
        ORDER BY d.created_at DESC LIMIT ?
    """, (f'%"{tag}"%', limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def palace_undo_write(drawer_id: int) -> dict:
    """
    Delete a single auto-write and rebuild its parent closet from
    whatever drawers are still linked to it — repairs the rolling
    merge instead of nuking the whole closet. Safe by construction
    for nightstand-wing writes, since only nightstand drawers ever
    feed a nightstand closet.
    """
    conn = get_db()
    drawer = conn.execute("SELECT * FROM palace_drawers WHERE id=?", (drawer_id,)).fetchone()
    if not drawer:
        conn.close()
        return {"ok": False, "error": "drawer not found"}

    closet_id = drawer["closet_id"]
    conn.execute("DELETE FROM palace_drawers WHERE id=?", (drawer_id,))

    if closet_id:
        remaining = conn.execute(
            "SELECT content FROM palace_drawers WHERE closet_id=? ORDER BY created_at",
            (closet_id,)
        ).fetchall()

        if remaining:
            rebuilt = " | ".join(aaak_compress(r["content"]) for r in remaining)
            token_est = estimate_tokens(rebuilt)
            conn.execute(
                "UPDATE palace_closets SET compressed=?, token_est=?, updated_at=? WHERE id=?",
                (rebuilt, token_est, datetime.now().isoformat(), closet_id)
            )
        else:
            conn.execute("DELETE FROM palace_closets WHERE id=?", (closet_id,))

    conn.commit()
    conn.close()
    return {"ok": True, "closet_id": closet_id, "drawer_id": drawer_id}

# ── Tool Registration ──────────────────────────────────────────────────────────

def register_palace_tools(registry):
    init_palace_db()

    registry.register(
        name="palace_remember",
        fn=lambda content, wing="sessions", room="general", layer=2, tags=None: (
            lambda r: f"Stored in {wing}/{room} (L{layer}). Compressed: {r['compressed']} | Saved ~{r['tokens_saved']} tokens."
        )(palace_store(content, wing, room, layer, tags)),
        description="Store a memory in the palace. Wing options: identity, projects, people, preferences, sessions.",
        parameters={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The memory to store."},
                "wing":    {"type": "string", "description": "Wing: identity|projects|people|preferences|sessions", "default": "sessions"},
                "room":    {"type": "string", "description": "Sub-topic room name.", "default": "general"},
                "layer":   {"type": "integer", "description": "0=identity 1=critical 2=recent 3=deep", "default": 2},
                "tags":    {"type": "array", "items": {"type": "string"}, "description": "Optional search tags."}
            },
            "required": ["content"]
        }
    )

    registry.register(
        name="palace_hall",
        fn=lambda content, hall="facts", layer=2: f"Hall entry stored: {aaak_compress(content, hall)}",
        description="Store a cross-cutting fact in a Hall: facts|events|preferences|discoveries|advice.",
        parameters={
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "hall":    {"type": "string", "description": "facts|events|preferences|discoveries|advice", "default": "facts"},
                "layer":   {"type": "integer", "default": 2}
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
            lambda r: f"Undone — drawer {r['drawer_id']} removed, closet {r['closet_id']} rebuilt." if r["ok"]
            else f"[Error: {r.get('error')}]"
        )(palace_undo_write(drawer_id)),
        description="Delete a single flagged auto-write (by drawer_id from palace_review_writes) and safely rebuild its parent closet. Owner-only — touches the nightstand wing, isolated from curated memory.",
        parameters={
            "type": "object",
            "properties": {
                "drawer_id": {"type": "integer", "description": "The drawer_id shown in brackets from palace_review_writes output."}
            },
            "required": ["drawer_id"]
        }
    )