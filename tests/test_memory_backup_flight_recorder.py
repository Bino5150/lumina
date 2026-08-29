"""
AGENT-FLIGHT-RECORDER-01A1 -- Memory Backup integration (mission section
10: "Do not defer this to A5... no separate backup button").

ui/settings/memory_tab.py's _build_memory_backup() already walks the whole
DATA_DIR tree indiscriminately -- placing flight_recorder.db under
DATA_DIR/telemetry/ means it's captured with zero new sweep logic; these
tests cover the one real addition (a deliberate WAL checkpoint of the
recorder before the zip, so the archive holds a coherent state) and that
backup survives a recorder that has nothing to checkpoint / fails to.
"""
import os
import zipfile

import pytest

from ui.settings.memory_tab import _build_memory_backup


class _FakeDbConn:
    """Stand-in for _db()'s real sqlite3.Connection -- _build_memory_backup()
    only ever calls .execute() (the WAL checkpoint pragma) and .close() on
    it, so a minimal fake avoids needing a real config.DB_PATH at all."""
    def execute(self, *a, **kw):
        pass

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _fake_main_db(monkeypatch):
    import ui.settings.memory_tab as memory_tab
    monkeypatch.setattr(memory_tab, "_db", lambda: _FakeDbConn())


def _make_data_dir_with_telemetry(tmp_path, content=b"fake sqlite bytes"):
    data_dir = tmp_path / "data_dir"
    telemetry_dir = data_dir / "telemetry"
    telemetry_dir.mkdir(parents=True)
    (telemetry_dir / "flight_recorder.db").write_bytes(content)
    (data_dir / "memory").mkdir()
    (data_dir / "memory" / "lumina.db").write_bytes(b"fake main db")
    return data_dir


def test_backup_includes_telemetry_db(tmp_path):
    data_dir = _make_data_dir_with_telemetry(tmp_path)
    dest = tmp_path / "backup.zip"

    _build_memory_backup(str(data_dir), str(dest))

    with zipfile.ZipFile(dest) as zf:
        names = zf.namelist()
    assert os.path.join("telemetry", "flight_recorder.db") in names or \
        "telemetry/flight_recorder.db" in names
    assert os.path.join("memory", "lumina.db") in names or "memory/lumina.db" in names


def test_backup_checkpoints_flight_recorder_before_zipping(tmp_path, monkeypatch):
    import core.flight_recorder as flight_recorder

    calls = []
    monkeypatch.setattr(flight_recorder, "checkpoint", lambda: calls.append(True))

    data_dir = _make_data_dir_with_telemetry(tmp_path)
    _build_memory_backup(str(data_dir), str(tmp_path / "backup.zip"))

    assert calls == [True]


def test_backup_survives_flight_recorder_checkpoint_failure(tmp_path, monkeypatch):
    import core.flight_recorder as flight_recorder

    def _boom():
        raise RuntimeError("checkpoint exploded")

    monkeypatch.setattr(flight_recorder, "checkpoint", _boom)

    data_dir = _make_data_dir_with_telemetry(tmp_path)
    dest = tmp_path / "backup.zip"

    _build_memory_backup(str(data_dir), str(dest))  # must not raise

    assert dest.exists()
    with zipfile.ZipFile(dest) as zf:
        assert len(zf.namelist()) > 0


def test_backup_survives_no_telemetry_db_present_yet(tmp_path):
    """A fresh install / a session that never touched the recorder has no
    telemetry/ directory at all -- backup must still succeed."""
    data_dir = tmp_path / "data_dir"
    (data_dir / "memory").mkdir(parents=True)
    (data_dir / "memory" / "lumina.db").write_bytes(b"fake main db")
    dest = tmp_path / "backup.zip"

    _build_memory_backup(str(data_dir), str(dest))  # must not raise

    assert dest.exists()
