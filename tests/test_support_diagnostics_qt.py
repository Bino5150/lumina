"""Real Qt button/worker path with isolated Recorder data, no running agent."""
import os
from pathlib import Path
import sqlite3
import threading
import time
import zipfile

import pytest

if os.environ.get("LUMINA_REQUIRE_QT") == "1":
    import PySide6
else:
    pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from ui.settings.about_tab import AboutTab
from ui.settings import support_diagnostics as ui

COLORS = {k: "#888888" for k in ("accent", "text_primary", "border", "bg_card", "text_muted")}


def pump_until(qapp, predicate):
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    assert predicate()


@pytest.fixture
def tab(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    import config
    root = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", str(root))
    db = root / "telemetry/flight_recorder.db"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE events (seq INTEGER, ts REAL, event_type TEXT, severity TEXT, provenance TEXT, fields_json TEXT)")
        c.execute("CREATE INDEX idx_events_ts ON events(ts)")
        c.execute("INSERT INTO events VALUES (1, ?, 'turn.completed', 'info', 'machine', '{}')", (time.time(),))
    widget = AboutTab(COLORS)
    yield app, widget
    worker = widget.support_diagnostics.worker
    if worker is not None:
        worker.cancel_event.set()
        assert worker.wait(5000)
        app.processEvents()
    widget.deleteLater()
    app.processEvents()


def test_button_invokes_shared_exporter_without_agent(tab, tmp_path, monkeypatch):
    app, widget = tab
    out = tmp_path / "<private>.zip"
    monkeypatch.setattr(ui.QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    called = []
    original = ui.diagnostics.export_support_diagnostics
    def observed(*a, **kw):
        called.append(threading.get_ident())
        return original(*a, **kw)
    monkeypatch.setattr(ui.diagnostics, "export_support_diagnostics", observed)
    widget.support_export_btn.click()
    assert not widget.support_export_btn.isEnabled()
    pump_until(app, lambda: widget.support_diagnostics.worker is None)
    assert called and called[0] != threading.get_ident()
    assert out.exists()
    with zipfile.ZipFile(out) as z:
        assert z.testzip() is None
    assert str(out) in widget.support_export_status.text()
    assert widget.support_export_status.textFormat() == Qt.PlainText
    assert "Saved support diagnostics" in widget.support_export_status.text()


def test_long_success_receipt_fits_scrollable_about_tab(tab):
    app, widget = tab
    from core.support_diagnostics import ExportReceipt
    widget.resize(880, 760)
    widget.show()
    widget.support_diagnostics._result(("saved", ExportReceipt(
        Path("/very/long/private/destination/" + "nested/" * 16 + "support.zip"), "complete", 1)))
    for width, height in ((880, 760), (480, 500)):
        widget.resize(width, height)
        app.processEvents()
        status = widget.support_export_status
        assert status.height() >= status.heightForWidth(status.width())
    widget.hide()


def test_save_dialog_cancel_does_nothing(tab, monkeypatch):
    app, widget = tab
    monkeypatch.setattr(ui.QFileDialog, "getSaveFileName", lambda *a, **k: ("", ""))
    monkeypatch.setattr(ui.diagnostics, "export_support_diagnostics", lambda *a, **k: pytest.fail("must not run"))
    widget.support_export_btn.click()
    assert widget.support_diagnostics.worker is None
    assert widget.support_export_status.text() == ""


def test_real_worker_cancellation_and_single_flight(tab, tmp_path, monkeypatch):
    app, widget = tab
    out = tmp_path / "cancel.zip"
    monkeypatch.setattr(ui.QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    entered = threading.Event()
    def blocked(*a, cancel_event, **kw):
        entered.set()
        assert cancel_event.wait(3)
        raise ui.diagnostics.ExportCancelled()
    monkeypatch.setattr(ui.diagnostics, "export_support_diagnostics", blocked)
    widget.support_export_btn.click()
    assert entered.wait(1)
    first = widget.support_diagnostics.worker
    widget.support_diagnostics.start()
    assert first is widget.support_diagnostics.worker
    widget.support_diagnostics.progress.cancel()
    # QProgressDialog.cancel() hides; its canceled signal is emitted by user action.
    widget.support_diagnostics.progress.canceled.emit()
    pump_until(app, lambda: widget.support_diagnostics.worker is None)
    assert not out.exists()
    assert "cancelled" in widget.support_export_status.text()
    assert widget.support_export_btn.isEnabled()


def test_failure_receipt_does_not_expose_exception_text(tab, tmp_path, monkeypatch):
    app, widget = tab
    out = tmp_path / "fail.zip"
    monkeypatch.setattr(ui.QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    def fail(*a, **kw):
        raise PermissionError("private detail sk-" + "A"*30)
    monkeypatch.setattr(ui.diagnostics, "export_support_diagnostics", fail)
    widget.support_export_btn.click()
    pump_until(app, lambda: widget.support_diagnostics.worker is None)
    assert "No ZIP was saved" in widget.support_export_status.text()
    assert "private detail" not in widget.support_export_status.text()
    assert not out.exists()


def test_partial_receipt_is_honest(tab, tmp_path, monkeypatch):
    app, widget = tab
    import config
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "missing"))
    out = tmp_path / "partial.zip"
    monkeypatch.setattr(ui.QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    widget.support_export_btn.click()
    pump_until(app, lambda: widget.support_diagnostics.worker is None)
    assert out.exists()
    assert "partial" in widget.support_export_status.text()
    assert "manifest.json" in widget.support_export_status.text()


def test_save_dialog_uses_home_zip_suffix_and_no_replace_prompt(tab, tmp_path, monkeypatch):
    app, widget = tab
    seen = []
    def choose(*args, **kwargs):
        seen.append((args, kwargs))
        return str(tmp_path / "new-support"), ""
    monkeypatch.setattr(ui.QFileDialog, "getSaveFileName", choose)
    widget.support_export_btn.click()
    pump_until(app, lambda: widget.support_diagnostics.worker is None)
    assert (tmp_path / "new-support.zip").is_file()
    args, kw = seen[0]
    assert Path(args[2]).parent == Path.home()
    assert kw["options"] & ui.QFileDialog.DontConfirmOverwrite


def test_existing_file_receipt_names_the_actual_problem(tab, tmp_path, monkeypatch):
    app, widget = tab
    out = tmp_path / "existing.zip"
    out.write_bytes(b"preserve")
    monkeypatch.setattr(ui.QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    widget.support_export_btn.click()
    pump_until(app, lambda: widget.support_diagnostics.worker is None)
    assert "already exists" in widget.support_export_status.text()
    assert out.read_bytes() == b"preserve"


def test_application_quit_cancels_and_joins_worker(tab, tmp_path, monkeypatch):
    app, widget = tab
    entered = threading.Event()
    stopped = threading.Event()
    def blocked(*args, cancel_event, **kwargs):
        entered.set()
        assert cancel_event.wait(2)
        stopped.set()
        raise ui.diagnostics.ExportCancelled()
    monkeypatch.setattr(ui.diagnostics, "export_support_diagnostics", blocked)
    monkeypatch.setattr(ui.QFileDialog, "getSaveFileName", lambda *a, **k: (str(tmp_path/"quit.zip"), ""))
    widget.support_export_btn.click()
    assert entered.wait(1)
    worker = widget.support_diagnostics.worker
    app.aboutToQuit.emit()
    assert stopped.is_set()
    assert not worker.isRunning()
    pump_until(app, lambda: widget.support_diagnostics.worker is None)
