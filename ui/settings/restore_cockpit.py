"""Settings -> Memory -> Recover Archive (Isolated): the owner-facing cockpit for
the shipped isolated recovery engine (AGENT-BACKUP-RESTORE-UI-01).

It inspects an archive read-only, previews the exact changes, binds the
owner's approval to the engine's plan_digest and runs core.agent_backup_restore
into a NEW folder. It restores nothing into the running Lumina, offers no
"Restore Current Lumina" action, and cannot label any result a Verified Agent
Backup. All decisions and wording live in the Qt-free
core.agent_backup_restore_cockpit; this module is widgets and threads.

Threads: every engine call runs in a QThread that this module retains at
application level (like the Agent Backup export worker), so closing the dialog
or Settings can never orphan one, and normal application quit waits for it.
The engine offers no cancellation, so none is offered: the dialog refuses to
close while a mutating operation runs, and a read-only operation that is still
running when the dialog closes finishes unseen.
"""
import os
from datetime import datetime

from PySide6.QtCore import Qt, QCoreApplication, QThread, Signal
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from core import agent_backup_restore_cockpit as cp

from ._widgets import _btn, _lbl, _sec

# Operation lifetime belongs to the application, not a disposable dialog.
_WORKERS = set()
_SHUTDOWN_PROPERTY = "agentRestoreCockpitShutdownConnected"


def _release_worker(worker):
    _WORKERS.discard(worker)
    worker.deleteLater()


def _wait_for_workers():
    for worker in tuple(_WORKERS):
        worker.wait()


def _retain_worker(worker):
    _WORKERS.add(worker)
    worker.finished.connect(lambda: _release_worker(worker))
    app = QCoreApplication.instance()
    if app is not None and not app.property(_SHUTDOWN_PROPERTY):
        app.aboutToQuit.connect(_wait_for_workers)
        app.setProperty(_SHUTDOWN_PROPERTY, True)


def mutating_operation_running():
    return any(w.mutating for w in tuple(_WORKERS))


def operation_running():
    return bool(_WORKERS)


class _CockpitWorker(QThread):
    """Runs one cockpit operation off the GUI thread. The operation returns a
    plain dict and never touches a widget; this thread only emits it."""
    done = Signal(str, int, object)

    def __init__(self, op, generation, fn, args, mutating):
        super().__init__(None)
        self.op, self.generation, self.mutating = op, generation, mutating
        self._fn, self._args = fn, args

    def run(self):
        try:
            result = self._fn(*self._args)
        except BaseException as exc:  # nothing may escape a worker thread
            result = cp.unexpected_failure(self.op, exc)
        self.done.emit(self.op, self.generation, result)


_WORKING = {
    "inspect": "Inspecting the archive (read-only)...",
    "preview": "Building the preview (nothing is written)...",
    "execute": "Recovering into the new folder. This cannot be cancelled; keep this window open...",
    "interrupted": "Checking for an interrupted recovery...",
    "quarantine": "Moving unrecognised staging folders aside...",
    "verify_folder": "Verifying the folder (read-only)...",
}


def _plain(label):
    label.setTextFormat(Qt.PlainText)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return label


def _view(height, name):
    box = QPlainTextEdit()
    box.setObjectName(name)
    box.setReadOnly(True)
    box.setMinimumHeight(height)
    box.setLineWrapMode(QPlainTextEdit.WidgetWidth)
    return box


class RestoreCockpitDialog(QDialog):
    def __init__(self, agent, c, parent=None):
        super().__init__(parent)
        self.agent, self.c = agent, c
        self.session = cp.RestoreSession()
        self._worker = None
        self._closed = False
        self._request = None
        self._shown_digest = None
        self._result = None
        self.setObjectName("restore_cockpit")
        self.setWindowTitle("Recover Archive (Isolated)")
        self.setModal(True)
        self.resize(940, 780)
        self._build()
        self._refresh()

    # -- layout ------------------------------------------------------------
    def _build(self):
        c = self.c
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        scroll.setWidget(body)
        col = QVBoxLayout(body)
        col.setSpacing(8)
        outer.addWidget(scroll, 1)

        self.banner = _plain(QLabel(cp.BANNER))
        self.banner.setObjectName("banner")
        self.banner.setStyleSheet(f"color:{c['text_primary']};background:{c['bg_card']};"
                                  f"border:1px solid {c['border']};border-radius:6px;padding:8px;")
        col.addWidget(self.banner)

        col.addWidget(_sec("1  ARCHIVE", c))
        row = QHBoxLayout()
        self.archive_edit = QLineEdit()
        self.archive_edit.setObjectName("archive_path")
        self.archive_edit.setReadOnly(True)
        self.archive_edit.setPlaceholderText("No archive chosen")
        self.choose_archive_btn = _btn("Choose archive...", c)
        self.choose_archive_btn.setObjectName("choose_archive")
        self.choose_archive_btn.clicked.connect(self._choose_archive)
        self.reinspect_btn = _btn("Inspect again", c)
        self.reinspect_btn.setObjectName("inspect_again")
        self.reinspect_btn.clicked.connect(self._inspect_again)
        row.addWidget(self.archive_edit, 1)
        row.addWidget(self.choose_archive_btn)
        row.addWidget(self.reinspect_btn)
        col.addLayout(row)
        self.archive_view = _view(170, "archive_view")
        col.addWidget(self.archive_view)

        col.addWidget(_sec("2  WHAT TO RECOVER", c))
        col.addWidget(_lbl("Only units that verify independently can be chosen. Anything damaged is listed "
                           "with its reason and is never recovered.", c))
        self.units_table = QTableWidget(0, 4)
        self.units_table.setObjectName("units_table")
        self.units_table.setHorizontalHeaderLabels(["Unit", "Contents", "Status", "Your choice"])
        self.units_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.units_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.units_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.units_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.units_table.setMinimumHeight(190)
        self.units_table.setEditTriggers(QTableWidget.NoEditTriggers)
        col.addWidget(self.units_table)

        col.addWidget(_sec("3  NEW FOLDERS", c))
        col.addWidget(_plain(_lbl(
            "New data folder: must not exist yet (or be empty). The recovered database, preferences and "
            "inactive history are written here. It must be outside every Lumina production folder and "
            "outside the running installation; those are refused.", c)))
        row = QHBoxLayout()
        self.data_dir_edit = QLineEdit()
        self.data_dir_edit.setObjectName("data_dir")
        self.data_dir_edit.setPlaceholderText("/path/to/new-recovered-data-folder")
        self.data_dir_edit.textChanged.connect(self._folders_edited)
        self.choose_parent_btn = _btn("Choose parent folder...", c)
        self.choose_parent_btn.setObjectName("choose_parent")
        self.choose_parent_btn.clicked.connect(self._choose_parent)
        row.addWidget(self.data_dir_edit, 1)
        row.addWidget(self.choose_parent_btn)
        col.addLayout(row)
        col.addWidget(_plain(_lbl(
            "Install folder: where recovered Skills, personas, avatars, voices, tool profiles and project "
            "documents are placed. Use a fresh Lumina install folder, not the running one. A file that "
            "already exists and differs is shown as a conflict; it is never overwritten.", c)))
        row = QHBoxLayout()
        self.install_dir_edit = QLineEdit()
        self.install_dir_edit.setObjectName("install_dir")
        self.install_dir_edit.setPlaceholderText("/path/to/a/fresh/lumina/install")
        self.install_dir_edit.textChanged.connect(self._folders_edited)
        self.choose_install_btn = _btn("Choose install folder...", c)
        self.choose_install_btn.setObjectName("choose_install")
        self.choose_install_btn.clicked.connect(self._choose_install)
        row.addWidget(self.install_dir_edit, 1)
        row.addWidget(self.choose_install_btn)
        col.addLayout(row)
        row = QHBoxLayout()
        self.interrupted_btn = _btn("Check for interrupted recovery...", c)
        self.interrupted_btn.setObjectName("check_interrupted")
        self.interrupted_btn.setToolTip("Rolls back what a recovery killed before publishing left behind at "
                                        "the new data folder above. Unrecognised folders are left alone.")
        self.interrupted_btn.clicked.connect(lambda: self._start_interrupted(False))
        self.quarantine_btn = _btn("Move unrecognised staging aside...", c)
        self.quarantine_btn.setObjectName("quarantine_unrecognised")
        self.quarantine_btn.clicked.connect(lambda: self._start_interrupted(True))
        row.addWidget(self.interrupted_btn)
        row.addWidget(self.quarantine_btn)
        row.addStretch()
        col.addLayout(row)

        col.addWidget(_sec("4  PREVIEW", c))
        row = QHBoxLayout()
        self.preview_btn = _btn("Preview exact changes", c, accent=True)
        self.preview_btn.setObjectName("preview")
        self.preview_btn.clicked.connect(self._start_preview)
        self.preview_status = _plain(_lbl("", c))
        self.preview_status.setObjectName("preview_status")
        row.addWidget(self.preview_btn)
        row.addWidget(self.preview_status, 1)
        col.addLayout(row)
        self.plan_view = _view(240, "plan_view")
        col.addWidget(self.plan_view)

        col.addWidget(_sec("5  APPROVE AND RECOVER", c))
        self.approve_check = QCheckBox("Approve the plan shown above")
        self.approve_check.setObjectName("approve_plan")
        self.approve_check.toggled.connect(self._approval_toggled)
        self.recover_btn = _btn("Recover into the new folder", c, accent=True)
        self.recover_btn.setObjectName("recover")
        self.recover_btn.clicked.connect(self._start_execute)
        col.addWidget(self.approve_check)
        col.addWidget(self.recover_btn)

        col.addWidget(_sec("RESULT", c))
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(8)
        col.addWidget(self.progress)
        self.result_view = _view(200, "result_view")
        col.addWidget(self.result_view)
        row = QHBoxLayout()
        self.copy_receipt_btn = _btn("Copy receipt path", c)
        self.copy_receipt_btn.setObjectName("copy_receipt_path")
        self.copy_receipt_btn.clicked.connect(self._copy_receipt_path)
        self.view_receipt_btn = _btn("View receipt", c)
        self.view_receipt_btn.setObjectName("view_receipt")
        self.view_receipt_btn.clicked.connect(self._view_receipt)
        self.save_receipt_btn = _btn("Save a copy of the receipt...", c)
        self.save_receipt_btn.setObjectName("save_receipt_copy")
        self.save_receipt_btn.clicked.connect(self._save_receipt_copy)
        self.verify_folder_btn = _btn("Verify a recovered folder...", c)
        self.verify_folder_btn.setObjectName("verify_folder")
        self.verify_folder_btn.setToolTip("Read-only re-check of an earlier isolated recovery against the "
                                          "receipt stored inside it.")
        self.verify_folder_btn.clicked.connect(self._verify_folder)
        for b in (self.copy_receipt_btn, self.view_receipt_btn, self.save_receipt_btn, self.verify_folder_btn):
            row.addWidget(b)
        row.addStretch()
        col.addLayout(row)

        foot = QHBoxLayout()
        self.status_label = _plain(_lbl("", c))
        self.status_label.setObjectName("status")
        self.close_btn = _btn("Close", c)
        self.close_btn.setObjectName("close")
        self.close_btn.clicked.connect(self.close)
        foot.addWidget(self.status_label, 1)
        foot.addWidget(self.close_btn)
        outer.addLayout(foot)
        # An Enter keypress in a path field must never activate a button (least of
        # all "Recover into the new folder"): nothing here is a default button.
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
            button.setDefault(False)
        for widget, name in ((self.archive_view, "Archive inspection details"),
                             (self.units_table, "Recoverable units and your choices"),
                             (self.plan_view, "Recovery plan preview"),
                             (self.result_view, "Recovery result"),
                             (self.data_dir_edit, "New data folder"),
                             (self.install_dir_edit, "Install folder"),
                             (self.approve_check, "Approve the plan shown"),
                             (self.archive_edit, "Chosen archive")):
            widget.setAccessibleName(name)
        # Nothing in this dialog is rich text; archive-derived strings must
        # never be interpreted as markup, so no label may auto-detect it.
        for label in self.findChildren(QLabel):
            label.setTextFormat(Qt.PlainText)

    # -- state ---------------------------------------------------------------
    @property
    def busy(self):
        return self._worker is not None

    @property
    def close_blocked(self):
        """A mutating step is running -- or this dialog still holds its worker.
        The registry can release a worker on its own thread before the GUI
        thread has processed that worker's result, so the application-wide
        flag alone leaves a window in which closing would lose the result."""
        return mutating_operation_running() or (self._worker is not None and self._worker.mutating)

    def _set_status(self, text):
        self.status_label.setText(text)

    def _refresh(self):
        s, busy = self.session, self.busy
        has_intake = s.intake is not None
        for w in (self.choose_archive_btn, self.choose_parent_btn, self.choose_install_btn,
                  self.verify_folder_btn):
            w.setEnabled(not busy)
        for w in (self.data_dir_edit, self.install_dir_edit):
            w.setEnabled(not busy)
        self.reinspect_btn.setEnabled(not busy and s.archive_path is not None)
        self.units_table.setEnabled(has_intake and not busy)
        blocker = s.preview_blocker()
        self.preview_btn.setEnabled(not busy and blocker is None)
        self.preview_btn.setToolTip(blocker or "Shows exactly what would be written. Writes nothing.")
        ready = s.plan is not None and s.plan["status"] == "ready"
        self.approve_check.setEnabled(ready and not busy)
        self.recover_btn.setEnabled(s.can_execute and not busy)
        self.interrupted_btn.setEnabled(not busy and bool(self.data_dir_edit.text().strip()))
        self.progress.setVisible(busy)
        blocked = self.close_blocked
        self.close_btn.setEnabled(not blocked)
        self.close_btn.setToolTip("A recovery step is running and cannot be cancelled." if blocked else "")
        has_receipt = bool(self._result and self._result.get("receipt_path"))
        for w in (self.copy_receipt_btn, self.view_receipt_btn, self.save_receipt_btn):
            w.setEnabled(has_receipt and not busy)
        self.quarantine_btn.setVisible(bool(self._result and self._result.get("needs_quarantine_decision")))
        self.quarantine_btn.setEnabled(not busy)

    def _owner_only(self):
        if getattr(self.agent, "owner", False):
            return True
        self._set_status("Only the local owner can use isolated recovery.")
        return False

    def _start_worker(self, op, generation, fn, args, mutating):
        if self.busy:
            return False
        worker = _CockpitWorker(op, generation, fn, args, mutating)
        self._worker = worker
        # Retained first: its release must run before the dialog's own
        # refresh on `finished`, or Close would still see this worker.
        _retain_worker(worker)
        worker.done.connect(self._on_done)
        worker.finished.connect(self._on_finished)
        self._set_status(_WORKING[op])
        self._refresh()
        worker.start()
        return True

    def _on_finished(self):
        self._worker = None
        if not self._closed:
            self._refresh()

    # -- results -------------------------------------------------------------
    def _on_done(self, op, generation, result):
        if self._closed:
            return  # closed while a read-only step ran: its result is not shown
        handler = {"inspect": self._inspect_done, "preview": self._preview_done,
                   "execute": self._execute_done, "interrupted": self._interrupted_done,
                   "quarantine": self._interrupted_done, "verify_folder": self._verify_done}[op]
        handler(generation, result)

    # -- archive -------------------------------------------------------------
    def _choose_archive(self):
        if self.busy:
            return
        start = os.path.dirname(self.session.archive_path) if self.session.archive_path else os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(self, "Choose a backup archive to inspect", start,
                                              "Zip Archives (*.zip);;All files (*)")
        if path:
            self._begin_inspect(path)

    def _inspect_again(self):
        if self.session.archive_path and not self.busy:
            self._begin_inspect(self.session.archive_path)

    def _begin_inspect(self, path):
        try:
            generation = self.session.set_archive(path)
        except cp.SessionError:
            return
        self.archive_edit.setText(path)
        self.archive_view.clear()
        self.units_table.setRowCount(0)
        self._clear_plan("Choose the folders, then preview.")
        self._start_worker("inspect", generation, cp.inspect_archive, (path,), False)

    def _inspect_done(self, generation, result):
        if not self.session.accept_intake(generation, result):
            if not result.get("ok") and generation == self.session.generation:
                self.archive_view.setPlainText(cp.describe_failure(result) + (
                    f"\n\nArchive SHA-256: {result['sha256']}" if result.get("sha256") else ""))
                self._set_status("This archive cannot be recovered.")
            return
        view = result["view"]
        self.archive_view.setPlainText(cp.render_intake_text(view))
        self._fill_units(view)
        self._set_status("Archive inspected. Review the limits above, then choose the folders.")

    def _fill_units(self, view):
        table = self.units_table
        table.blockSignals(True)
        table.setRowCount(0)
        for unit in view["units"]:
            row = table.rowCount()
            table.insertRow(row)
            status = unit["availability"]
            if unit["inactive_history"]:
                status += "; inactive history only"
            if unit["reasons"]:
                status += " - " + "; ".join(unit["reasons"])
            for i, text in enumerate((unit["label"], unit["description"], status)):
                table.setItem(row, i, QTableWidgetItem(text))
            if unit["selectable"]:
                combo = QComboBox()
                combo.setObjectName("choice_" + unit["unit"])
                combo.setAccessibleName("Your choice for " + unit["label"])
                for value in ("recover", "recover_keep_destination", "skip"):
                    combo.addItem(cp.CHOICE_TEXT[value], value)
                combo.setCurrentIndex(combo.findData(self.session.selection.get(unit["unit"], "skip")))
                combo.currentIndexChanged.connect(lambda _i, u=unit["unit"], cb=combo: self._choice_changed(u, cb))
                table.setCellWidget(row, 3, combo)
            else:
                table.setItem(row, 3, QTableWidgetItem("Not recoverable"))
        table.blockSignals(False)
        table.resizeRowsToContents()
        self._refresh()

    def _choice_changed(self, unit, combo):
        try:
            self.session.set_choice(unit, combo.currentData())
        except cp.SessionError:
            combo.blockSignals(True)
            combo.setCurrentIndex(combo.findData(self.session.selection.get(unit, "skip")))
            combo.blockSignals(False)
            return
        self._inputs_changed()

    # -- folders -------------------------------------------------------------
    def _choose_parent(self):
        if self.busy:
            return
        folder = QFileDialog.getExistingDirectory(self, "Choose the folder that will contain the new data folder",
                                                  os.path.expanduser("~"))
        if folder:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            self.data_dir_edit.setText(os.path.join(folder, f"lumina-recovered-{stamp}"))

    def _choose_install(self):
        if self.busy:
            return
        folder = QFileDialog.getExistingDirectory(self, "Choose a fresh Lumina install folder",
                                                  os.path.expanduser("~"))
        if folder:
            self.install_dir_edit.setText(folder)

    def _folders_edited(self):
        try:
            self.session.set_folders(self.data_dir_edit.text(), self.install_dir_edit.text())
        except cp.SessionError:
            return
        self._inputs_changed()

    def _inputs_changed(self):
        # A plan is on screen but the session no longer holds it: say so.
        if self._shown_digest and self.session.plan is None:
            self._clear_plan(f"Preview again: {self.session.invalidated_because}.")
        self._refresh()

    # -- preview -------------------------------------------------------------
    def _clear_plan(self, note):
        self._shown_digest = None
        self.plan_view.clear()
        self.preview_status.setText(note)
        self.approve_check.blockSignals(True)
        self.approve_check.setChecked(False)
        self.approve_check.setText("Approve the plan shown above")
        self.approve_check.blockSignals(False)

    def _start_preview(self):
        if self.busy:
            return
        try:
            request = self.session.request_preview()
        except cp.SessionError:
            self._refresh()
            return
        self._request = request
        self._clear_plan("")
        self._start_worker("preview", request["generation"], cp.build_plan,
                           (request["archive_path"], request["data_dir"], request["base_dir"],
                            request["selection"], request["inspected_sha256"]), False)

    def _preview_done(self, generation, result):
        reason = self.session.accept_plan(generation, self._request, result)
        if reason == "stale":
            return
        if reason == "refused":
            self.plan_view.setPlainText(cp.describe_failure(result))
            self.preview_status.setText(result["category"].replace("_", " ").capitalize() + ".")
            self._set_status("The preview was refused. Nothing was written.")
            if result["code"] == "ARCHIVE_CHANGED":
                self._forget_inspection("the archive changed after it was inspected")
        elif reason:
            self.plan_view.setPlainText(
                f"The plan did not match what you chose ({reason}).\n\nInspect the archive again, then preview.")
            self.preview_status.setText("Plan not usable.")
            self._forget_inspection(reason)
            self._set_status("Inspect the archive again.")
        else:
            plan = self.session.plan
            self._shown_digest = plan["plan_digest"]
            self.plan_view.setPlainText(cp.render_plan_text(plan, result.get("warnings", [])))
            self.approve_check.setText(f"I have read this plan and approve it (digest {plan['plan_digest'][:16]}...)")
            if plan["status"] == "ready":
                self.preview_status.setText("Ready for your approval.")
                self._set_status("Review the plan, then approve it.")
            else:
                self.preview_status.setText("Needs your decision on install-folder conflicts.")
                self._set_status("Choose 'keep my file' or 'Skip' for the conflicting unit, then preview again.")
        self._refresh()

    # -- approval and execution ---------------------------------------------
    def _approval_toggled(self, checked):
        if checked:
            if not self.session.approve(self._shown_digest):
                self.approve_check.blockSignals(True)
                self.approve_check.setChecked(False)
                self.approve_check.blockSignals(False)
                self._set_status("That plan can no longer be approved. Preview again.")
        else:
            self.session.revoke()
        self._refresh()

    def _start_execute(self):
        if self.busy or not self.session.can_execute or not self._owner_only():
            return
        try:
            ticket = self.session.begin_execution()
        except cp.SessionError:
            return
        self._result = None
        self.result_view.clear()
        self._start_worker("execute", ticket["generation"], cp.execute_recovery,
                           (ticket["plan"], ticket["archive_path"], ticket["digest"]), True)

    def _execute_done(self, generation, result):
        self.session.finish_execution("a recovery attempt used and consumed that plan")
        self._result = result if result.get("receipt_path") else None
        self._clear_plan("That plan was used. Preview again to run another recovery.")
        if result["ok"]:
            self.result_view.setPlainText(cp.describe_success(result))
            self._set_status("Isolated recovery published and verified. Lumina was not changed.")
        elif result.get("outcome") == "published_unverified":
            self.result_view.setPlainText(cp.describe_failure(result) + "\n\n" + cp.describe_success(result))
            self._set_status("PUBLISHED BUT NOT VERIFIED: do not use that folder.")
        else:
            self.result_view.setPlainText(cp.describe_failure(result))
            self._set_status("Recovery did not complete. " + result["title"])
            if result.get("code") == "ARCHIVE_CHANGED":
                self._forget_inspection("the archive changed after it was inspected")
        self._refresh()

    def _forget_inspection(self, because):
        """The archive is not the one the table describes: drop the table, the
        selection and the plan, and make the owner inspect it again."""
        self.session.require_reinspection(because)
        self.units_table.setRowCount(0)
        self.archive_view.appendPlainText("\nTHIS ARCHIVE CHANGED after it was inspected. The details above are "
                                          "out of date: press 'Inspect again'.")

    # -- interrupted recovery ------------------------------------------------
    def _confirm(self, title, text):
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle(title)
        box.setTextFormat(Qt.PlainText)
        box.setText(text)
        yes = box.addButton("Continue", QMessageBox.AcceptRole)
        no = box.addButton("Cancel", QMessageBox.RejectRole)
        box.setDefaultButton(no)
        box.setEscapeButton(no)
        box.exec()
        return box.clickedButton() is yes

    def _start_interrupted(self, quarantine):
        data_dir = cp.normalize_folder(self.data_dir_edit.text())
        if self.busy or not data_dir or not self._owner_only():
            return
        if quarantine:
            ok = self._confirm("Move unrecognised staging aside",
                               "This renames every staging folder next to the destination that this engine "
                               "does not recognise to '.<name>.restore-<id>.quarantined-<hex>'. Nothing is "
                               "deleted, and no install file they name is touched. Recognised interrupted "
                               "recoveries are rolled back as before.\n\nDestination:\n" + data_dir)
        else:
            ok = self._confirm("Check for interrupted recovery",
                               "This looks for a recovery that was killed before publishing to the destination "
                               "below. It removes that attempt's staging folder and only the install-folder "
                               "files the attempt's own journal proves it created (each carries its ownership "
                               "mark). Folders it does not recognise are left alone and reported.\n\n"
                               "Destination:\n" + data_dir)
        if ok:
            self._start_worker("quarantine" if quarantine else "interrupted", self.session.generation,
                               cp.recover_interrupted, (data_dir, quarantine), True)

    def _interrupted_done(self, generation, result):
        self._result = result if result["ok"] else None
        if result["ok"]:
            self.result_view.setPlainText("\n".join(result["lines"]))
            self._set_status("Interrupted-recovery check finished."
                             + (" Some folders were left alone: see below." if result["needs_quarantine_decision"] else ""))
            if result["needs_quarantine_decision"]:
                self.result_view.appendPlainText(
                    "\nThese folders still block new recoveries here. 'Move unrecognised staging aside' renames "
                    "them (never deletes) so you can inspect them.")
        else:
            self.result_view.setPlainText(cp.describe_failure(result))
            self._set_status(result["title"])
        self._refresh()

    # -- verify a folder -----------------------------------------------------
    def _verify_folder(self):
        if self.busy:
            return
        folder = QFileDialog.getExistingDirectory(self, "Choose a recovered data folder to verify",
                                                  os.path.expanduser("~"))
        if folder:
            self._start_worker("verify_folder", self.session.generation, cp.verify_recovered_folder, (folder,), False)

    def _verify_done(self, generation, result):
        self._result = result if result.get("receipt_path") else None
        if result["ok"]:
            self.result_view.setPlainText(cp.describe_success(result).replace(
                "ISOLATED RECOVERY PUBLISHED AND VERIFIED", "RECOVERED FOLDER RE-VERIFIED", 1))
            self._set_status("The folder still matches its stored receipt.")
        else:
            self.result_view.setPlainText(cp.describe_failure(result) + (
                "\n\n" + cp.describe_success(result) if result.get("receipt") else ""))
            self._set_status("Verification failed: " + result["title"])
        self._refresh()

    # -- receipt -------------------------------------------------------------
    def _receipt_bytes(self):
        r = self._result
        if not r or not r.get("receipt_path"):
            return None
        return cp.read_stored_receipt_bytes(r["data_dir"], r["receipt"]["restore_id"])

    def _copy_receipt_path(self):
        if self._result and self._result.get("receipt_path"):
            QApplication.clipboard().setText(self._result["receipt_path"])
            self._set_status("Receipt path copied.")

    def _view_receipt(self):
        raw = self._receipt_bytes()
        text = cp.receipt_display_text(raw) if raw is not None else None
        if text is None:
            self._set_status("The stored receipt could not be read.")
            return
        viewer = QDialog(self)
        viewer.setObjectName("receipt_viewer")
        viewer.setWindowTitle("Recovery receipt (read-only)")
        viewer.resize(760, 600)
        layout = QVBoxLayout(viewer)
        box = _view(400, "receipt_text")
        box.setPlainText(text)
        layout.addWidget(box)
        viewer.exec()

    def _save_receipt_copy(self):
        raw = self._receipt_bytes()
        if raw is None:
            self._set_status("The stored receipt could not be read.")
            return
        dest, _ = QFileDialog.getSaveFileName(self, "Save a copy of the receipt", "lumina_recovery_receipt.json",
                                              "JSON (*.json)")
        if not dest:
            return
        try:
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except FileExistsError:
            self._set_status("That file already exists; a copy never overwrites. Choose a new name.")
            return
        except OSError as exc:
            self._set_status(f"Could not create that file ({type(exc).__name__}).")
            return
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            self._set_status(f"Could not write the copy ({type(exc).__name__}).")
            return
        self._set_status("Receipt copy saved: " + cp.safe_text(dest, 300))

    # -- closing -------------------------------------------------------------
    def _refuse_close(self):
        self._set_status("A recovery step is running and cannot be cancelled. This window stays open until "
                         "it finishes.")

    def _shutdown(self):
        """Stop listening. A read-only worker still running keeps going, is
        tracked by the application, and its result is never shown."""
        self._closed = True
        worker = self._worker
        if worker is not None:
            try:
                worker.done.disconnect(self._on_done)
                worker.finished.disconnect(self._on_finished)
            except (RuntimeError, TypeError):
                pass
        self._worker = None

    def reject(self):
        if self.close_blocked:
            self._refuse_close()
            return
        self._shutdown()
        super().reject()

    def closeEvent(self, event):
        if self.close_blocked:
            self._refuse_close()
            event.ignore()
            return
        self._shutdown()
        super().closeEvent(event)


def open_cockpit(agent, c, parent=None):
    """Open the cockpit modally, unless a mutating step is still running."""
    if mutating_operation_running():
        QMessageBox.warning(parent, "Recovery running",
                            "A recovery step is still running. It cannot be cancelled; wait for it to finish.")
        return None
    dialog = RestoreCockpitDialog(agent, c, parent)
    try:
        dialog.exec()
    finally:
        dialog.deleteLater()   # it is closed here; a still-running read-only step is tracked by the application
    return dialog
