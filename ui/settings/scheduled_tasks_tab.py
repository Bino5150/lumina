from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QTableWidgetItem, QHeaderView

import time, json

from ._widgets import _sec, _lbl, _btn, _table


class ScheduledTasksTab(QWidget):
    """S51 Part C. Owner-only surface over core/task_queue.py — same tier as
    ToolsTab's Pending Tools panel, and deliberately built to match its
    shape: a list of async/queued state on the left/top, a read-only detail
    preview for the selected row, a manual Refresh button rather than
    auto-polling. No per-item Approve/Reject here (nothing to approve) --
    the equivalent actions are "select a row to view its result" and
    "Cancel" for anything still sitting in the scheduled heap.
    """

    def __init__(self, agent, c: dict, parent=None):
        super().__init__(parent)
        self.agent = agent
        self.c = c
        self._task_ids = []
        self._build()
        self._load_tasks()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)

        layout.addWidget(_sec("SCHEDULED / BACKGROUND TASKS", self.c))
        layout.addWidget(_lbl(
            "Tasks dispatched via run_background_subagent or scheduled via "
            "schedule_background_subagent. Scheduled tasks not yet started "
            "can be cancelled; running/completed tasks cannot.", self.c
        ))

        top = QHBoxLayout()
        top.addStretch()
        refresh_btn = _btn("⟳ Refresh", self.c)
        refresh_btn.setFixedHeight(30)
        refresh_btn.clicked.connect(self._load_tasks)
        top.addWidget(refresh_btn)
        self.cancel_btn = _btn("✕ Cancel", self.c, danger=True)
        self.cancel_btn.setFixedHeight(30)
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel_selected)
        top.addWidget(self.cancel_btn)
        layout.addLayout(top)

        row = QHBoxLayout()
        row.setSpacing(10)

        list_col = QVBoxLayout()
        self.table = _table(["Task ID", "Status", "Completed At"], self.c)
        self.table.setColumnWidth(0, 260)
        self.table.setColumnWidth(1, 90)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.itemSelectionChanged.connect(self._on_task_selected)
        list_col.addWidget(self.table)
        row.addLayout(list_col, 2)

        preview_col = QVBoxLayout()
        preview_col.addWidget(_lbl("Result (select a task above)", self.c))
        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setStyleSheet(f"""
            QTextEdit{{background:{self.c['bg_input']};color:{self.c['text_primary']};
            border:1px solid {self.c['border']};border-radius:7px;padding:6px 10px;
            font-family:'JetBrains Mono',monospace;font-size:11px;}}
        """)
        preview_col.addWidget(self.preview)
        row.addLayout(preview_col, 2)
        layout.addLayout(row, 1)

        self.status_lbl = _lbl("", self.c)
        layout.addWidget(self.status_lbl)

    def _load_tasks(self, preserve_status: bool = False):
        """preserve_status: True when called right after an action (Cancel)
        that already set its own status_lbl message this call chain --
        otherwise this method's own empty-state message overwrites it before
        it's ever seen. (The Pending Tools panel above has this same
        call-order gap in _reject_pending()/_load_pending_tools() --
        pre-existing, out of scope here, but caught live while testing this
        new tab and worth fixing in code that's actually being touched.)"""
        from core.task_queue import list_all_tasks, get_task_result
        self._task_ids = sorted(list_all_tasks())
        self.table.blockSignals(True)
        self.table.setRowCount(len(self._task_ids))
        for row, tid in enumerate(self._task_ids):
            r = get_task_result(tid) or {"status": "expired", "completed_at": None}
            self.table.setItem(row, 0, QTableWidgetItem(tid))
            self.table.setItem(row, 1, QTableWidgetItem(r["status"]))
            ts = time.strftime("%H:%M:%S", time.localtime(r["completed_at"])) if r.get("completed_at") else "—"
            self.table.setItem(row, 2, QTableWidgetItem(ts))
        self.table.blockSignals(False)
        self.preview.clear()
        self.cancel_btn.setEnabled(False)
        if not preserve_status:
            self.status_lbl.setText("" if self._task_ids else "Nothing running or scheduled.")

    def _selected_task_id(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        return self._task_ids[rows[0].row()]

    def _on_task_selected(self):
        tid = self._selected_task_id()
        if tid is None:
            self.preview.clear()
            self.cancel_btn.setEnabled(False)
            return
        from core.task_queue import get_task_result
        r = get_task_result(tid)
        if r is None:
            self.preview.setPlainText("[Expired — task_queue's RESULT_TTL_SECONDS has passed since it completed.]")
            self.cancel_btn.setEnabled(False)
            return
        self.preview.setPlainText(json.dumps(r, indent=2, default=str))
        self.cancel_btn.setEnabled(r["status"] == "scheduled")

    def _cancel_selected(self):
        tid = self._selected_task_id()
        if tid is None:
            return
        from core.task_queue import cancel_task
        if cancel_task(tid):
            self.status_lbl.setText(f"Cancelled {tid}.")
        else:
            self.status_lbl.setText(f"Could not cancel {tid} — already running, completed, or unknown.")
        self._load_tasks(preserve_status=True)
