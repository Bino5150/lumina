"""F-08: core/db.py — every connection gets WAL + busy_timeout."""
import os
from core.db import connect


def test_default_path_pragmas(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))

    conn = connect()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.close()


def test_explicit_path_used_over_default(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "default.db"))
    explicit_path = str(tmp_path / "explicit.db")

    conn = connect(path=explicit_path)
    conn.execute("CREATE TABLE t (x INT)")
    conn.commit()
    conn.close()

    assert os.path.exists(explicit_path)
    assert not os.path.exists(str(tmp_path / "default.db"))


def test_row_factory_toggle(tmp_path):
    conn = connect(path=str(tmp_path / "t.db"), row_factory=False)
    conn.execute("CREATE TABLE t (x INT)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    row = conn.execute("SELECT x FROM t").fetchone()
    assert row == (1,)   # plain tuple, not sqlite3.Row
    conn.close()


def test_creates_missing_parent_dir(tmp_path):
    nested = tmp_path / "a" / "b" / "c.db"
    conn = connect(path=str(nested))
    conn.close()
    assert nested.exists()
