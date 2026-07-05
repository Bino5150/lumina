"""
Centralized SQLite connection factory.

Every connection gets WAL mode + a busy_timeout, so concurrent writers
(desktop UI thread, dream-sweep idle compaction, Discord/Telegram bridges
running via to_thread) queue on a lock instead of throwing
"database is locked" straight into the caller.

F-08 fix: previously six modules (tools/palace.py, tools/memory.py,
tools/knowledge.py, core/skills.py, core/chat_history.py, ui/settings.py)
each hand-rolled their own sqlite3.connect(config.DB_PATH) with zero
concurrency pragmas, plus core/idempotency.py connecting to a separate
ledger.db the same way. This factory replaces all of them.
"""
import sqlite3
import os
import config


def connect(path: str = None, row_factory: bool = True, foreign_keys: bool = True) -> sqlite3.Connection:
    """
    Open a SQLite connection with WAL mode and busy_timeout already set.

    Args:
        path: DB file path. Defaults to config.DB_PATH. Pass an explicit
              path for a separate DB file (e.g. the idempotency ledger).
        row_factory: if True (default), rows come back as sqlite3.Row
                     (dict-like access) instead of plain tuples.
        foreign_keys: if True (default), enables FK enforcement for this
                      connection. SQLite requires this per-connection.
    """
    db_path = path or config.DB_PATH
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    if foreign_keys:
        conn.execute("PRAGMA foreign_keys=ON")
    if row_factory:
        conn.row_factory = sqlite3.Row
    return conn
