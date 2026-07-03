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

LEDGER_PATH = os.path.join(config.BASE_DIR, "memory", "ledger.db")


def _init_db():
    os.makedirs(os.path.dirname(LEDGER_PATH), exist_ok=True)
    conn = sqlite3.connect(LEDGER_PATH)
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


def check(request_id: str):
    """Returns cached result if this request_id already succeeded, else None."""
    _init_db()
    conn = sqlite3.connect(LEDGER_PATH)
    row = conn.execute("SELECT result FROM ledger WHERE request_id = ?", (request_id,)).fetchone()
    conn.close()
    return row[0] if row else None


def record(request_id: str, result: str):
    """Store a successful result so a retry short-circuits instead of re-sending."""
    _init_db()
    conn = sqlite3.connect(LEDGER_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO ledger (request_id, result) VALUES (?, ?)",
        (request_id, result),
    )
    conn.commit()
    conn.close()