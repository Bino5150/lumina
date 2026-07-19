"""
Idempotency ledger — dedupes retried side-effecting calls.
SQLite-backed, request_id derived deterministically from the call's
actual arguments (not a timestamp, not a random UUID) so a genuine
retry produces the same key and a legitimately new call doesn't.
"""
import sqlite3
import hashlib
import json
import os
import config

LEDGER_PATH = os.path.join(config.DATA_DIR, "memory", "ledger.db")


def _init_db():
    from core.db import connect
    conn = connect(path=LEDGER_PATH, row_factory=False, foreign_keys=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ledger (
            request_id TEXT PRIMARY KEY,
            result TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def make_request_id(*args) -> str:
    """Deterministic key from call arguments — same inputs, same key."""
    raw = json.dumps(args, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def check(request_id: str, ttl_hours: float = 24):
    """Returns cached result if this request_id succeeded within the last
    `ttl_hours`, else None. FE-17: dedupe used to be permanent (no TTL),
    which silently blocked any proactive/recurring send with identical
    text (e.g. a daily notification) forever after the first success. A
    stale row isn't deleted here — record() on the next successful call
    REPLACEs it and refreshes created_at naturally."""
    from core.db import connect
    from datetime import datetime, timedelta, timezone
    _init_db()
    conn = connect(path=LEDGER_PATH, row_factory=False, foreign_keys=False)
    row = conn.execute(
        "SELECT result, created_at FROM ledger WHERE request_id = ?", (request_id,)
    ).fetchone()
    conn.close()
    if row is None:
        return None
    result, created_at = row
    try:
        recorded = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        # Unexpected timestamp format — fail open to the old (permanent
        # dedupe) behavior rather than silently disabling protection.
        return result
    if datetime.now(timezone.utc) - recorded > timedelta(hours=ttl_hours):
        return None
    return result


def record(request_id: str, result: str):
    """Store a successful result so a retry short-circuits instead of re-sending."""
    from core.db import connect
    _init_db()
    conn = connect(path=LEDGER_PATH, row_factory=False, foreign_keys=False)
    conn.execute(
        "INSERT OR REPLACE INTO ledger (request_id, result) VALUES (?, ?)",
        (request_id, result),
    )
    conn.commit()
    conn.close()