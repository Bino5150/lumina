"""F-DR-5: a second backup in one running Lumina session works.

Found by the DR-A2 drill on gremlin-pi (2026-11-24): inside a running Lumina
only the first export of a session could succeed. The SQLite connection
identity proof (core.agent_backup._connect_sqlite_with_bound_identity) diffs
/proc/self/fd around connect(); while another connection in the process
holds a lock on the same file (the running app's flight recorder), SQLite
parks a closed read-only descriptor and hands it to the next read-only open,
so the next export sees no new descriptor and fails closed. The fix runs each
owner export in a fresh child process (core.agent_backup_export_child); the
identity proof itself is unchanged.

Synthetic data roots only (tmp_path), the real builder, verifier and child.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import core.agent_backup as ab
import core.agent_backup_export as ex

from test_agent_backup_verified_export_01 import source  # noqa: F401
from test_c1_verified_backup_s3a import _fresh_cache, c1_store, vsource  # noqa: F401

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def running_app_lock(vsource):
    """What the running application holds: its own flight-recorder
    connection with an open transaction on the file the export snapshots."""
    conn = sqlite3.connect(str(vsource[0] / "telemetry" / "flight_recorder.db"), isolation_level=None)
    conn.execute("BEGIN")
    conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    yield conn
    conn.execute("ROLLBACK")
    conn.close()


def _no_staging_left(folder):
    return not [p for p in Path(folder).iterdir() if p.name.startswith((".incomplete_export_", ".agent_backup_"))]


# ── the defect, and that the in-process guard is unchanged ──────────────────

def test_in_process_a_second_export_still_fails_closed_while_the_app_holds_a_lock(vsource, running_app_lock, tmp_path):
    data, base, _ = map(str, vsource)
    ex._owner_export_in_process("incomplete", data, base, str(tmp_path / "first.zip"))
    with pytest.raises(ab.AgentBackupError, match="could not attribute any newly-opened file descriptor"):
        ex._owner_export_in_process("incomplete", data, base, str(tmp_path / "second.zip"))
    assert not (tmp_path / "second.zip").exists() and _no_staging_left(tmp_path)


# ── the repair: each export in its own process ───────────────────────────────

def test_two_verified_backups_and_an_incomplete_export_in_one_session(vsource, running_app_lock, tmp_path):
    data, base, _ = map(str, vsource)
    receipts = []
    for mode, name in (("verified", "v1.zip"), ("verified", "v2.zip"), ("incomplete", "i.zip")):
        dest = tmp_path / name
        receipt = ex.run_owner_export(mode, data, base, str(dest))
        assert receipt["verification"] == "PASS" and receipt["destination"] == str(dest)
        assert ex.verify_export(str(dest))["valid"]
        receipts.append(receipt)
    assert [r["export_kind"] for r in receipts] == ["verified_agent_backup", "verified_agent_backup",
                                                    "incomplete_state_export"]
    assert receipts[0]["full_agent_coverage"] is True and receipts[1]["full_agent_coverage"] is True
    title, _text = ex.owner_receipt_text(receipts[1])
    assert title == "Verified Agent Backup"


def test_a_refusal_comes_back_in_owner_words_and_publishes_nothing(source, tmp_path):
    data, base, _ = map(str, source)  # no C1 store: Verified is refused
    dest = tmp_path / "refused.zip"
    with pytest.raises(ex.ExportUnavailable) as info:
        ex.run_owner_export("verified", data, base, str(dest))
    assert info.value.code == "C1_STATE_ABSENT"
    assert ex.owner_message(info.value) == ex.OWNER_MESSAGES["C1_STATE_ABSENT"]
    assert not dest.exists() and _no_staging_left(tmp_path)


def test_an_existing_destination_is_refused_and_kept(vsource, tmp_path):
    data, base, _ = map(str, vsource)
    dest = tmp_path / "prior.zip"
    dest.write_bytes(b"PRIOR OWNER FILE")
    with pytest.raises((ex.ExportUnavailable, ab.AgentBackupError)):
        ex.run_owner_export("verified", data, base, str(dest))
    assert dest.read_bytes() == b"PRIOR OWNER FILE" and _no_staging_left(tmp_path)


def test_a_child_that_fails_publishes_nothing_and_reports_no_detail(vsource, tmp_path, monkeypatch):
    data, base, _ = map(str, vsource)
    monkeypatch.setattr(ex, "OWNER_EXPORT_CHILD", "core.no_such_export_child")
    dest = tmp_path / "x.zip"
    with pytest.raises(ab.AgentBackupError) as info:
        ex.run_owner_export("incomplete", data, base, str(dest))
    assert str(info.value) == "EXPORT_CHILD_FAILED" and not dest.exists()


# ── the credential boundary is the same one ─────────────────────────────────

def test_the_child_gets_the_credentials_store_the_in_process_call_would_use(monkeypatch, tmp_path):
    seen = []

    def fake_run(argv, **kwargs):
        seen.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"receipt": {"ok": 1}}).encode())

    monkeypatch.setattr(ex.subprocess, "run", fake_run)
    monkeypatch.setenv("LUMINA_SECRETS_PATH", str(tmp_path / "secrets.json"))
    assert ex.run_owner_export("incomplete", "/d", "/b", "/x.zip") == {"ok": 1}
    monkeypatch.chdir(tmp_path)
    ex.run_owner_export("incomplete", "rel/d", "rel/b", "rel/x.zip")
    monkeypatch.delenv("LUMINA_SECRETS_PATH")
    ex.run_owner_export("verified", "/d", "/b", "/y.zip")
    (argv1, kw1), (argv_rel, _), (argv2, _kw2) = seen
    assert argv1[1:4] == ["-B", "-m", ex.OWNER_EXPORT_CHILD]  # same site configuration as the app
    assert argv1[4:] == ["incomplete", "/d", "/b", "/x.zip", str(tmp_path / "secrets.json"),
                         os.path.expanduser("~/.config/lumina/identity_trailers.json")]
    assert argv_rel[5:8] == [str(tmp_path / "rel/d"), str(tmp_path / "rel/b"), str(tmp_path / "rel/x.zip")]
    assert argv2[8] == os.path.expanduser("~/.config/lumina/credentials.json")
    assert kw1["stdin"] is subprocess.DEVNULL and kw1["stderr"] is subprocess.DEVNULL
    assert "env" not in kw1  # the child inherits the same environment


def test_a_credentials_store_inside_the_data_folder_never_reaches_the_archive(vsource, tmp_path, monkeypatch):
    data, base, _ = map(str, vsource)
    secret = vsource[0] / "memory" / "credentials.json"
    secret.write_text(json.dumps({"openai_api_key": "CHILD-CREDENTIAL-SENTINEL"}))
    secret.chmod(0o600)
    monkeypatch.setenv("LUMINA_SECRETS_PATH", str(secret))
    dest = tmp_path / "i.zip"
    try:
        ex.run_owner_export("incomplete", data, base, str(dest))
    except (ex.ExportUnavailable, ab.AgentBackupError):
        assert not dest.exists()
        return
    with zipfile.ZipFile(dest) as z:
        assert b"CHILD-CREDENTIAL-SENTINEL" not in b"".join(z.read(n) for n in z.namelist())


@pytest.mark.parametrize("argv", [[], ["restore", "a", "b", "c", "d", "e"], ["verified", "a"]])
def test_the_child_refuses_anything_but_its_two_modes(argv):
    result = subprocess.run([sys.executable, "-B", "-m", "core.agent_backup_export_child", *argv],
                            cwd=REPO, capture_output=True)
    assert result.returncode == 2 and result.stdout == b""


# ── the Settings worker uses the child ──────────────────────────────────────

def test_the_settings_worker_runs_every_export_through_the_child_runner():
    import ast
    module = ast.parse((REPO / "ui" / "settings" / "memory_tab.py").read_text(encoding="utf-8"))
    worker = next(n for n in ast.walk(module) if isinstance(n, ast.ClassDef) and n.name == "_AgentBackupWorker")
    calls = {getattr(c.func, "attr", None) for c in ast.walk(worker) if isinstance(c, ast.Call)}
    assert "run_owner_export" in calls
    assert not calls & {"export_incomplete_state", "export_verified_agent_backup", "confirm_export_result"}


def test_two_backups_from_the_real_settings_button_in_one_session(vsource, running_app_lock, tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    import time
    from types import SimpleNamespace
    import config
    from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QPushButton
    from ui.main_window import COLORS
    from ui.settings.memory_tab import MemoryTab
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(config, "DATA_DIR", str(vsource[0]))
    monkeypatch.setattr(config, "BASE_DIR", str(vsource[1]))
    monkeypatch.setattr(MemoryTab, "_load", lambda self: None)
    messages, dests = [], iter([tmp_path / "ui1.zip", tmp_path / "ui2.zip", tmp_path / "ui3.zip"])
    for kind in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, kind, lambda parent, title, text, _k=kind: messages.append((_k, title)))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(next(dests)), ""))
    widget = MemoryTab(SimpleNamespace(owner=True), COLORS)
    try:
        for name in ("verified_agent_backup", "verified_agent_backup", "incomplete_state_export"):
            widget.findChild(QPushButton, name).click()
            deadline = time.monotonic() + 120
            while widget._backup_worker is not None and time.monotonic() < deadline:
                app.processEvents(); time.sleep(.01)
            app.processEvents()
            assert widget._backup_worker is None
    finally:
        widget.close()
    assert [k for k, _ in messages] == ["information", "information", "warning"], messages
    assert all(ex.verify_export(str(tmp_path / n))["valid"] for n in ("ui1.zip", "ui2.zip", "ui3.zip"))
