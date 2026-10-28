"""Desktop-only support export controller. No agent, provider or network wiring."""
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal, Slot, Qt
from PySide6.QtWidgets import QApplication, QFileDialog, QProgressDialog

from core import support_diagnostics as diagnostics

# Keep parentless threads alive if the Settings widget is destroyed mid-export.
# Its destroyed signal requests cancellation; finished releases the last reference.
_WORKERS = set()


class _ExportWorker(QThread):
    result_ready = Signal(object)

    def __init__(self, output, data_dir):
        super().__init__()
        self.output = output
        self.data_dir = data_dir
        self.cancel_event = threading.Event()

    def run(self):
        try:
            receipt = diagnostics.export_support_diagnostics(
                self.output, data_dir=self.data_dir, cancel_event=self.cancel_event)
            self.result_ready.emit(("saved", receipt))
        except diagnostics.ExportCancelled:
            self.result_ready.emit(("cancelled", None))
        except Exception as exc:
            self.result_ready.emit(("failed", type(exc).__name__))

    @Slot()
    def cancel_and_wait(self):
        self.cancel_event.set()
        # Finish cancellation/cleanup before Qt and Python tear down this thread.
        # SQLite work is bounded; an OS filesystem call must return before join.
        self.wait()


class SupportDiagnosticsController(QObject):
    def __init__(self, button, status, parent):
        super().__init__(parent)
        self.button = button
        self.status = status
        self.worker = None
        self.progress = None
        status.setTextFormat(Qt.PlainText)
        button.clicked.connect(self.start)

    def start(self):
        if self.worker is not None:
            return
        output, _ = QFileDialog.getSaveFileName(
            self.parent(), "Export Support Diagnostics", str(Path.home()/diagnostics.suggested_filename()),
            "ZIP archives (*.zip)", options=QFileDialog.DontConfirmOverwrite)
        if not output:
            return
        if not output.lower().endswith(".zip"):
            output += ".zip"
        try:
            # Use the already-running app's selected root, never import config:
            # config itself reads prefs/credentials and can fail before GUI boot.
            config = sys.modules.get("config")
            data_dir = getattr(config, "DATA_DIR", None)
            if data_dir is None:
                from data_directory import select_data_directory
                data_dir = select_data_directory()
        except Exception:
            self.status.setText("Data folder selection needs review. No ZIP was saved. See the upgrade guide.")
            return
        self.status.setText("Collecting local diagnostics…")
        self.button.setEnabled(False)
        self.worker = _ExportWorker(output, data_dir)
        worker = self.worker
        _WORKERS.add(worker)
        self.progress = QProgressDialog("Collecting local diagnostics…", "Cancel", 0, 0, self.parent())
        self.progress.setWindowTitle("Support Diagnostics")
        self.progress.setWindowModality(Qt.WindowModal)
        self.progress.setMinimumDuration(0)
        self.progress.setAutoClose(False)
        self.progress.setAutoReset(False)
        self.progress.canceled.connect(worker.cancel_event.set)
        self.parent().destroyed.connect(worker.cancel_event.set)
        QApplication.instance().aboutToQuit.connect(worker.cancel_and_wait)
        worker.result_ready.connect(self._result)
        worker.finished.connect(self._finished)
        worker.finished.connect(lambda: _WORKERS.discard(worker))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _result(self, result):
        kind, value = result
        if kind == "saved":
            prefix = "Saved support diagnostics" if value.result == "complete" else "Saved partial support diagnostics (some evidence missing or limited; see manifest.json)"
            self.status.setText(f"{prefix}:\n{value.path}\nReview before sharing privately when developers request it.")
        elif kind == "cancelled":
            self.status.setText("Export cancelled. No ZIP was saved.")
        elif value == "FileExistsError":
            self.status.setText("That file already exists. Choose a new filename; no ZIP was saved.")
        else:
            self.status.setText(f"Export failed ({value}). No ZIP was saved. Check free space and choose a new, writable filename.")

    def _finished(self):
        if self.progress is not None:
            self.progress.hide()
            self.progress.deleteLater()
            self.progress = None
        self.worker = None
        self.button.setEnabled(True)
