"""Real Settings signals, synthetic paths; never starts LuminaAgent."""
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QPushButton, QFileDialog, QMessageBox
from test_agent_backup_verified_export_01 import source


@pytest.fixture
def tab(source, monkeypatch):
    import config
    from ui.settings.memory_tab import MemoryTab
    from ui.main_window import COLORS
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(config, "DATA_DIR", str(source[0]))
    monkeypatch.setattr(config, "BASE_DIR", str(source[1]))
    monkeypatch.setattr(MemoryTab, "_load", lambda self: None)
    widget = MemoryTab(SimpleNamespace(owner=True), COLORS)
    messages = []
    for kind in ["information", "warning", "critical"]:
        monkeypatch.setattr(QMessageBox, kind, lambda parent, title, text, _kind=kind:
                            messages.append((_kind, title, text)))
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(source[2]), ""))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    yield widget, messages, app
    worker = getattr(widget, "_backup_worker", None)
    if worker is not None:
        worker.wait(10000)
    widget.close()


def click_verified(tab, *, incomplete=False):
    widget, messages, app = tab
    button = widget.findChild(QPushButton, "incomplete_state_export" if incomplete else "verified_agent_backup")
    assert button is not None, "new verified entry point is absent"
    button.click()
    deadline = time.monotonic() + 10
    while getattr(widget, "_backup_worker", None) is not None and time.monotonic() < deadline:
        app.processEvents();time.sleep(.005)
    app.processEvents()
    assert getattr(widget, "_backup_worker", None) is None
    return messages


def test_actual_settings_button_real_builder_verifier_receipt_no_false_full_success(tab, source, monkeypatch):
    from core import agent_backup_export as export
    import core.agent_backup as ab
    calls = []
    original = export.export_incomplete_state
    def isolated_review(data, base, destination, **kwargs):
        calls.append((data, base, destination))
        return original(data, base, destination, **kwargs, identity_trailers_path="/nonexistent/identity.json",
                        credentials_path="/nonexistent/credentials.json")
    monkeypatch.setattr(export, "export_incomplete_state", isolated_review)
    messages = click_verified(tab, incomplete=True)
    assert calls == [(str(source[0]), str(source[1]), str(source[2]))]
    with __import__("zipfile").ZipFile(source[2]) as z:
        manifest = json.loads(z.read("manifest.json"))
    assert manifest["format_version"]["major"] == 2
    assert export.verify_export(str(source[2]))["valid"]
    assert any("incomplete" in (title + text).lower() for _, title, text in messages)
    assert not any("Verified Agent Backup created" in text for _, _, text in messages)
    assert not any(kind == "information" for kind, _, _ in messages)


def test_cancel_has_no_builder_or_destination_effect(tab, source, monkeypatch):
    from core import agent_backup_export as export
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: ("", ""))
    monkeypatch.setattr(export, "export_verified_agent_backup", lambda *a, **k: pytest.fail("cancel called builder"))
    assert not click_verified(tab)
    assert not source[2].exists()


def test_production_safety_unavailable_leaves_prior_destination_intact(tab, source):
    source[2].write_bytes(b"PRIOR OWNER FILE")
    messages = click_verified(tab)
    assert source[2].read_bytes() == b"PRIOR OWNER FILE"
    assert any("suspension" in text.lower() for _, _, text in messages)
    assert not any("Verified Agent Backup created" in text for _, _, text in messages)


def test_claimed_pass_without_actual_artifact_never_succeeds(tab, monkeypatch):
    from core import agent_backup_export as export
    monkeypatch.setattr(export, "export_verified_agent_backup", lambda *a, **k:
                        ({}, {"verification": "PASS", "full_agent_coverage": True}))
    messages = click_verified(tab)
    assert any(kind == "critical" for kind, _, _ in messages)
    assert not any("Verified Agent Backup created" in text for _, _, text in messages)


def test_legacy_remains_separate_and_truthfully_labeled(tab):
    widget, _, _ = tab
    labels = [b.text() for b in widget.findChildren(QPushButton)]
    assert any("Legacy" in t and "Backup Memory" in t for t in labels)
    assert any("Verified Agent Backup" in t for t in labels)


def test_nonowner_cannot_export(tab, source):
    tab[0].agent.owner = False
    messages = click_verified(tab)
    assert not source[2].exists()
    assert any("owner" in text.lower() for _, _, text in messages)


def test_incomplete_acknowledgement_defaults_no_and_decline_writes_nothing(tab, source, monkeypatch):
    seen = []
    def decline(*args):
        seen.append(args)
        return QMessageBox.No
    monkeypatch.setattr(QMessageBox, "question", decline)
    assert not click_verified(tab, incomplete=True)
    assert seen and seen[0][-1] == QMessageBox.No
    assert "soul identity" in seen[0][2]
    assert not source[2].exists()


def test_incomplete_cancel_after_ack_never_calls_builder(tab, source, monkeypatch):
    from core import agent_backup_export as export
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: ("", ""))
    monkeypatch.setattr(export, "export_incomplete_state", lambda *a, **k: pytest.fail("cancel capture"))
    assert not click_verified(tab, incomplete=True)
    assert not source[2].exists()


def test_incomplete_unexpected_error_is_redacted_and_no_success(tab, monkeypatch):
    from core import agent_backup_export as export
    def fail(*args, **kwargs):
        raise RuntimeError("SENSITIVE-PREF-AND-KEY-SENTINEL")
    monkeypatch.setattr(export, "export_incomplete_state", fail)
    messages = click_verified(tab, incomplete=True)
    assert any(kind == "critical" for kind, _, _ in messages)
    assert "SENSITIVE-PREF" not in repr(messages)
    assert not any(kind == "information" for kind, _, _ in messages)


def test_incomplete_nonowner_refuses_before_acknowledgement(tab, source, monkeypatch):
    tab[0].agent.owner = False
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: pytest.fail("nonowner acknowledgement"))
    assert any(kind == "critical" for kind, _, _ in click_verified(tab, incomplete=True))
    assert not source[2].exists()


def test_settings_destruction_and_application_shutdown_do_not_destroy_running_export(tmp_path):
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[1]
    script = r'''
import sys, threading, time, resource, gc
from types import SimpleNamespace
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
sys.path[:0] = [sys.argv[1] + '/tests', sys.argv[1]]
import conftest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QMessageBox, QFileDialog
from ui.settings.memory_tab import MemoryTab, agent_backup_export
from ui.main_window import COLORS
MemoryTab._load = lambda self: None
app = QApplication([])
started = threading.Event(); release = threading.Event(); completed = threading.Event()
def blocked(*args, **kwargs):
    started.set()
    assert release.wait(3)
    completed.set()
    raise agent_backup_export.ExportUnavailable('synthetic refusal')
agent_backup_export.export_incomplete_state = blocked
QMessageBox.question = lambda *a, **k: QMessageBox.Yes
QMessageBox.critical = lambda *a, **k: None
QFileDialog.getSaveFileName = lambda *a, **k: ('/unused/synthetic-only.zip', '')
w = MemoryTab(SimpleNamespace(owner=True), COLORS)
w._export_agent_backup(incomplete=True)
assert started.wait(2)
w.deleteLater(); QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
del w; gc.collect()
threading.Timer(.05, release.set).start()
app.aboutToQuit.emit()
assert completed.is_set(), 'application shutdown did not wait for exporter'
for _ in range(20): app.processEvents(); time.sleep(.005)
print('SETTINGS_DESTRUCTION_AND_SHUTDOWN_SURVIVED')
'''
    result = subprocess.run([sys.executable, '-c', script, str(root)], cwd=tmp_path,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    assert 'SETTINGS_DESTRUCTION_AND_SHUTDOWN_SURVIVED' in result.stdout
    assert 'Traceback' not in result.stderr
