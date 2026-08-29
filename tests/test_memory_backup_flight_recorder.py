"""
AGENT-FLIGHT-RECORDER-01A1 -- Memory Backup integration (mission section
10: "Do not defer this to A5... no separate backup button").

core.memory_backup.build_memory_backup() already walks the whole DATA_DIR
tree indiscriminately -- placing flight_recorder.db under
DATA_DIR/telemetry/ means it's captured with zero new sweep logic; these
tests cover the one real addition (a deliberate WAL checkpoint of the
recorder before the zip, so the archive holds a coherent state) and that
backup survives a recorder that has nothing to checkpoint / fails to.

CI-CORRECTIVE: imports from core.memory_backup, never from
ui.settings.memory_tab -- that package's __init__.py eagerly imports
SettingsPanel -> PySide6, unavailable in this project's CI population
(see .github/workflows/tests.yml's own comment). The implementation lives
in core/memory_backup.py precisely so this test can exercise the real
logic in that environment instead of needing a pytest.importorskip("
PySide6") guard that would only skip it.
"""
import os
import zipfile

import pytest

from core.memory_backup import build_memory_backup as _build_memory_backup


class _FakeDbConn:
    """Stand-in for _db()'s real sqlite3.Connection -- build_memory_backup()
    only ever calls .execute() (the WAL checkpoint pragma) and .close() on
    it, so a minimal fake avoids needing a real config.DB_PATH at all."""
    def execute(self, *a, **kw):
        pass

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _fake_main_db(monkeypatch):
    import core.memory_backup as memory_backup
    monkeypatch.setattr(memory_backup, "_db", lambda: _FakeDbConn())


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


# ── CI-CORRECTIVE: PySide6-independence regression (backup collection) ──

def test_memory_backup_module_has_no_qt_dependency():
    """Static-source counterpart to test_flight_recorder_module_has_no_qt_
    dependency (tests/test_flight_recorder.py) -- same reasoning: a module
    this test suite needs to import in the PySide6-less CI job must never
    gain a real Qt/ui.* IMPORT, even a lazy in-function one, or collection
    breaks again exactly like it did before this module existed.

    Checks actual import statements via ast, not a raw substring scan --
    this module's own docstring legitimately mentions "PySide6" in prose
    explaining why it avoids it, which a naive text search would wrongly
    flag."""
    import ast
    import inspect
    import core.memory_backup as mod

    tree = ast.parse(inspect.getsource(mod))
    QT_MARKERS = ("PySide6", "PyQt5", "PyQt6")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith(QT_MARKERS), f"unexpected import: {alias.name}"
                assert not alias.name.startswith("ui."), f"unexpected import: {alias.name}"
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith(QT_MARKERS), f"unexpected import: {node.module}"
            assert not node.module.startswith("ui."), f"unexpected import: {node.module}"


def test_memory_backup_imports_with_pyside6_genuinely_unavailable():
    """The actual regression this CI-corrective exists for: reproduces the
    exact failure (ui.settings.memory_tab's package __init__.py eagerly
    importing SettingsPanel -> PySide6) in a subprocess with PySide6
    blocked at the import-system level via sys.meta_path -- the same
    mechanism the CI job's real absence of the package produces. Run in a
    subprocess (not in-process sys.meta_path surgery) so this can never
    leak an import blocker into any other test in the same session.
    Would have failed against the pre-extraction code (which required
    `from ui.settings.memory_tab import _build_memory_backup`); passes
    now that core/memory_backup.py is the real, Qt-free home."""
    import subprocess
    import sys

    script = (
        "import sys\n"
        "class _Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'PySide6' or name.startswith('PySide6.'):\n"
        "            raise ImportError(f\"No module named '{name}'\")\n"
        "        return None\n"
        "sys.meta_path.insert(0, _Block())\n"
        "from core.memory_backup import build_memory_backup\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        f"import failed with PySide6 blocked:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "OK" in result.stdout
