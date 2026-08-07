from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFrame, QTableWidgetItem,
    QMessageBox, QLineEdit, QInputDialog
)

import json

from ._widgets import _lbl, _le, _btn, _table


def _db():
    from core.db import connect
    return connect()


# ── Tab: Memory ────────────────────────────────────────────────────────────────

class MemoryTab(QWidget):
    def __init__(self, agent, c: dict, parent=None):
        super().__init__(parent)
        self.agent = agent
        self.c = c
        self._build()
        self._load()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20,16,20,16)
        layout.setSpacing(10)

        # Top bar
        top = QHBoxLayout()
        top.addWidget(_lbl("Stored Memories", self.c))
        top.addStretch()

        self.filter_le = QLineEdit()
        self.filter_le.setPlaceholderText("Filter by label or keyword...")
        self.filter_le.setFixedHeight(32)
        self.filter_le.setFixedWidth(220)
        self.filter_le.setStyleSheet(f"QLineEdit{{background:{self.c['bg_input']};color:{self.c['text_primary']};border:1px solid {self.c['border']};border-radius:6px;padding:4px 10px;font-size:12px;}}QLineEdit:focus{{border:1px solid {self.c['border_accent']};}}")
        self.filter_le.textChanged.connect(self._filter)
        top.addWidget(self.filter_le)

        refresh_btn = _btn("⟳ Refresh", self.c)
        refresh_btn.setFixedHeight(32)
        refresh_btn.clicked.connect(self._load)
        top.addWidget(refresh_btn)
        layout.addLayout(top)

        # Table
        self.table = _table(["ID", "Label", "Content", "Created"], self.c)
        self.table.setColumnWidth(0, 40)
        self.table.setColumnWidth(1, 90)
        self.table.setColumnWidth(2, 400)
        self.table.setColumnWidth(3, 120)
        layout.addWidget(self.table, 1)

        # Add memory row
        add_frame = QFrame()
        add_frame.setStyleSheet(f"QFrame{{background:{self.c['bg_card']};border:1px solid {self.c['border']};border-radius:8px;}}")
        add_layout = QVBoxLayout(add_frame)
        add_layout.setContentsMargins(12,10,12,10)
        add_layout.setSpacing(6)
        add_layout.addWidget(_lbl("Add Memory", self.c))

        inp_row = QHBoxLayout()
        self.new_label = _le("general", self.c)
        self.new_label.setPlaceholderText("label")
        self.new_label.setFixedWidth(120)
        self.new_content = _le("", self.c)
        self.new_content.setPlaceholderText("Memory content (max 512 chars)...")
        add_mem_btn = _btn("Save", self.c, accent=True)
        add_mem_btn.setFixedWidth(70)
        add_mem_btn.clicked.connect(self._add_memory)
        inp_row.addWidget(self.new_label)
        inp_row.addWidget(self.new_content, 1)
        inp_row.addWidget(add_mem_btn)
        add_layout.addLayout(inp_row)
        layout.addWidget(add_frame)

        # Bottom actions
        bot = QHBoxLayout()
        del_btn = _btn("Delete Selected", self.c, danger=True)
        del_btn.clicked.connect(self._delete_selected)
        paste_btn = _btn("Paste from Sapphire...", self.c)
        paste_btn.setToolTip("Paste a block of memories exported from Sapphire")
        paste_btn.clicked.connect(self._paste_import)
        bot.addWidget(del_btn)
        bot.addWidget(paste_btn)
        bot.addStretch()
        layout.addLayout(bot)

    def _load(self):
        conn = _db()
        rows = conn.execute("SELECT id, label, content, created_at FROM memories ORDER BY created_at DESC").fetchall()
        conn.close()
        self._all_rows = [dict(r) for r in rows]
        self._render(self._all_rows)

    def _render(self, rows: list):
        self.table.setRowCount(0)
        for r in rows:
            i = self.table.rowCount()
            self.table.insertRow(i)
            self.table.setItem(i, 0, QTableWidgetItem(str(r["id"])))
            self.table.setItem(i, 1, QTableWidgetItem(r["label"]))
            self.table.setItem(i, 2, QTableWidgetItem(r["content"][:120]))
            self.table.setItem(i, 3, QTableWidgetItem(r["created_at"][:16]))

    def _filter(self, text: str):
        if not text:
            self._render(self._all_rows)
            return
        t = text.lower()
        filtered = [r for r in self._all_rows if t in r["label"].lower() or t in r["content"].lower()]
        self._render(filtered)

    def _add_memory(self):
        content = self.new_content.text().strip()
        label = self.new_label.text().strip() or "general"
        if not content:
            return
        from tools.memory import save_memory
        save_memory(content, label)
        self.new_content.clear()
        self._load()

    def _delete_selected(self):
        rows = self.table.selectedItems()
        if not rows:
            return
        ids = list({self.table.item(r.row(), 0).text() for r in rows})
        reply = QMessageBox.question(self, "Delete", f"Delete {len(ids)} memory entries?",
                                     QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            conn = _db()
            for mid in ids:
                conn.execute("DELETE FROM memories WHERE id=?", (int(mid),))
            conn.commit()
            conn.close()
            self._load()

    def _paste_import(self):
        """Import memories pasted as plain text — one per line or JSON array."""
        text, ok = QInputDialog.getMultiLineText(
            self, "Import Memories",
            "Paste memories (one per line, or JSON array of {content, label} objects):"
        )
        if not ok or not text.strip():
            return
        from tools.memory import save_memory
        count = 0
        # Try JSON first
        try:
            items = json.loads(text.strip())
            for item in items:
                if isinstance(item, dict):
                    save_memory(item.get("content",""), item.get("label","imported"))
                    count += 1
                elif isinstance(item, str):
                    save_memory(item, "imported")
                    count += 1
        except Exception:
            # Plain text — one memory per line
            for line in text.strip().splitlines():
                line = line.strip()
                if line:
                    save_memory(line, "imported")
                    count += 1
        self._load()
        QMessageBox.information(self, "Import Complete", f"Imported {count} memories.")
