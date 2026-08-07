from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFrame, QTableWidgetItem,
    QFileDialog, QMessageBox, QLineEdit
)

import os

from ._widgets import _lbl, _te, _le, _btn, _table


def _db():
    from core.db import connect
    return connect()


# ── Tab: Knowledge ─────────────────────────────────────────────────────────────

class KnowledgeTab(QWidget):
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
        top.addWidget(_lbl("Knowledge Base", self.c))
        top.addStretch()
        self.cat_filter = QLineEdit()
        self.cat_filter.setPlaceholderText("Filter category...")
        self.cat_filter.setFixedHeight(32)
        self.cat_filter.setFixedWidth(180)
        self.cat_filter.setStyleSheet(f"QLineEdit{{background:{self.c['bg_input']};color:{self.c['text_primary']};border:1px solid {self.c['border']};border-radius:6px;padding:4px 10px;font-size:12px;}}QLineEdit:focus{{border:1px solid {self.c['border_accent']};}}")
        self.cat_filter.textChanged.connect(self._filter)
        top.addWidget(self.cat_filter)
        refresh_btn = _btn("⟳ Refresh", self.c)
        refresh_btn.setFixedHeight(32)
        refresh_btn.clicked.connect(self._load)
        top.addWidget(refresh_btn)
        layout.addLayout(top)

        # Table
        self.table = _table(["ID", "Category", "Title", "Content", "Updated"], self.c)
        self.table.setColumnWidth(0, 40)
        self.table.setColumnWidth(1, 100)
        self.table.setColumnWidth(2, 120)
        self.table.setColumnWidth(3, 320)
        layout.addWidget(self.table, 1)

        # Add entry
        add_frame = QFrame()
        add_frame.setStyleSheet(f"QFrame{{background:{self.c['bg_card']};border:1px solid {self.c['border']};border-radius:8px;}}")
        add_layout = QVBoxLayout(add_frame)
        add_layout.setContentsMargins(12,10,12,10)
        add_layout.setSpacing(6)
        add_layout.addWidget(_lbl("Add Knowledge Entry", self.c))

        meta_row = QHBoxLayout()
        self.new_cat = _le("notes", self.c)
        self.new_cat.setPlaceholderText("category")
        self.new_cat.setFixedWidth(120)
        self.new_title = _le("", self.c)
        self.new_title.setPlaceholderText("title (optional)")
        self.new_title.setFixedWidth(160)
        meta_row.addWidget(self.new_cat)
        meta_row.addWidget(self.new_title)
        meta_row.addStretch()
        add_layout.addLayout(meta_row)

        self.new_content = _te("", self.c, height=80)
        self.new_content.setPlaceholderText("Paste content here — large blocks will be chunked automatically...")
        add_layout.addWidget(self.new_content)

        btn_row = QHBoxLayout()
        save_btn = _btn("Save Entry", self.c, accent=True)
        save_btn.clicked.connect(self._add_entry)
        file_btn = _btn("📄 Upload File", self.c)
        file_btn.clicked.connect(self._upload_file)
        btn_row.addWidget(save_btn)
        btn_row.addWidget(file_btn)
        btn_row.addStretch()
        add_layout.addLayout(btn_row)
        layout.addWidget(add_frame)

        # Bottom actions
        bot = QHBoxLayout()
        del_btn = _btn("Delete Selected", self.c, danger=True)
        del_btn.clicked.connect(self._delete_selected)
        bot.addWidget(del_btn)
        bot.addStretch()
        layout.addLayout(bot)

    def _load(self):
        conn = _db()
        try:
            rows = conn.execute("SELECT id, category, title, content, updated_at FROM knowledge ORDER BY updated_at DESC").fetchall()
            self._all_rows = [dict(r) for r in rows]
        except Exception:
            self._all_rows = []
        conn.close()
        self._render(self._all_rows)

    def _render(self, rows: list):
        self.table.setRowCount(0)
        for r in rows:
            i = self.table.rowCount()
            self.table.insertRow(i)
            self.table.setItem(i, 0, QTableWidgetItem(str(r["id"])))
            self.table.setItem(i, 1, QTableWidgetItem(r.get("category", "")))
            self.table.setItem(i, 2, QTableWidgetItem(r.get("title") or ""))
            self.table.setItem(i, 3, QTableWidgetItem(r.get("content","")[:100]))
            self.table.setItem(i, 4, QTableWidgetItem(str(r.get("updated_at",""))[:16]))

    def _filter(self, text: str):
        if not text:
            self._render(self._all_rows)
            return
        t = text.lower()
        filtered = [r for r in self._all_rows if t in r.get("category","").lower() or t in r.get("title","").lower()]
        self._render(filtered)

    def _add_entry(self):
        content = self.new_content.toPlainText().strip()
        cat = self.new_cat.text().strip() or "notes"
        title = self.new_title.text().strip() or None
        if not content:
            return
        from tools.knowledge import save_knowledge
        # Chunk large content (>2000 chars)
        if len(content) > 2000:
            chunks = [content[i:i+1800] for i in range(0, len(content), 1800)]
            for idx, chunk in enumerate(chunks):
                t = f"{title} (part {idx+1})" if title else f"chunk {idx+1}"
                save_knowledge(cat, chunk, t)
        else:
            save_knowledge(cat, content, title)
        self.new_content.clear()
        self._load()

    def _upload_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Upload to Knowledge Base", "",
            "Text files (*.txt *.md *.py *.json *.csv);;All files (*)"
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            cat = self.new_cat.text().strip() or "files"
            title = os.path.basename(path)
            from tools.knowledge import save_knowledge
            if len(content) > 2000:
                chunks = [content[i:i+1800] for i in range(0, len(content), 1800)]
                for idx, chunk in enumerate(chunks):
                    save_knowledge(cat, chunk, f"{title} (part {idx+1})")
            else:
                save_knowledge(cat, content, title)
            self._load()
            QMessageBox.information(self, "Uploaded", f"'{title}' added to knowledge base.")
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Could not read file: {e}")

    def _delete_selected(self):
        rows = self.table.selectedItems()
        if not rows:
            return
        ids = list({self.table.item(r.row(), 0).text() for r in rows})
        reply = QMessageBox.question(self, "Delete", f"Delete {len(ids)} entries?",
                                     QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            conn = _db()
            for eid in ids:
                conn.execute("DELETE FROM knowledge WHERE id=?", (int(eid),))
            conn.commit()
            conn.close()
            self._load()
