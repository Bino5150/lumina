"""Settings > Skills -- read-only inventory of Lumina's persistent skills
(SKILLS-SETTINGS-UI-01).

Skills are procedural .md documents Lumina saves for herself via the
save_skill tool and recalls automatically via build_skills_block() (see
core/skills.py). Until this tab, that inventory was invisible in the GUI
the way Tools/Memory/Knowledge already are.

V1 is deliberately read-only, and deliberately does not show a scope
(built-in/user/project) column: source-vetting core/skills.py found a
single flat inventory -- one SQLite `skills` table (name, description,
path, created_at, updated_at) plus one directory of .md files -- with no
scope column, no project binding, and no enabled/disabled flag anywhere in
the schema. Every skill, however it originated, reaches storage through
the same write_skill() path, so there is nothing truthful to distinguish
skills by scope; fabricating "Built-in" vs "User" would be exactly the
kind of invented metadata this tab exists to avoid. The one real,
computed state this data supports is whether a skill's backing file is
still on disk, so that's what's surfaced (as "Status"), not an enable
toggle that doesn't exist.

This tab performs no writes: it calls only core.skills.list_skills() and
core.skills.load_skill(), the same pure-read functions the agent's own
recall path uses, plus core.skills.init_skills_db() (idempotent schema
creation, same call every agent already makes at startup) so opening
Settings before an agent has been constructed doesn't misreport an empty
schema as "no skills saved yet."
"""

import os
import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QTextEdit,
    QTableWidgetItem, QHeaderView,
)

from ._widgets import _sec, _lbl, _btn, _table


class SkillsTab(QWidget):
    def __init__(self, agent, c: dict, parent=None):
        super().__init__(parent)
        self.agent = agent
        self.c = c
        self._rows = []       # full inventory from the last _load(), augmented
        self._visible = []    # currently filtered/displayed subset, same order as table rows
        self._selected_name = None
        self._build()
        self._load()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)

        layout.addWidget(_sec("SKILLS", self.c))
        layout.addWidget(_lbl(
            "Procedural documents Lumina has saved for herself via save_skill() "
            "and recalls automatically when relevant to the conversation. "
            "Read-only inventory -- creating, editing, or deleting skills here "
            "is not supported.",
            self.c
        ))

        top = QHBoxLayout()
        self.filter_le = QLineEdit()
        self.filter_le.setPlaceholderText("Filter by name or description...")
        self.filter_le.setFixedHeight(32)
        self.filter_le.setFixedWidth(220)
        self.filter_le.setStyleSheet(
            f"QLineEdit{{background:{self.c['bg_input']};color:{self.c['text_primary']};"
            f"border:1px solid {self.c['border']};border-radius:6px;padding:4px 10px;"
            f"font-size:12px;}}QLineEdit:focus{{border:1px solid {self.c['border_accent']};}}"
        )
        self.filter_le.textChanged.connect(self._apply_filter)
        top.addWidget(self.filter_le)
        top.addStretch()
        refresh_btn = _btn("⟳ Refresh", self.c)
        refresh_btn.setFixedHeight(32)
        refresh_btn.clicked.connect(self._load)
        top.addWidget(refresh_btn)
        layout.addLayout(top)

        row = QHBoxLayout()
        row.setSpacing(10)

        list_col = QVBoxLayout()
        self.table = _table(["Name", "Status", "Last Modified"], self.c)
        self.table.setColumnWidth(0, 200)
        self.table.setColumnWidth(1, 120)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.itemSelectionChanged.connect(self._on_selected)
        list_col.addWidget(self.table)
        row.addLayout(list_col, 2)

        detail_col = QVBoxLayout()
        # Skill-controlled strings (name/description/path) must never be
        # interpreted as rich text -- QLabel defaults to Qt::AutoText, which
        # auto-detects and renders HTML-looking content. Forcing PlainText
        # makes literal display a Qt-enforced invariant, not a convention.
        self.detail_name = QLabel("")
        self.detail_name.setTextFormat(Qt.PlainText)
        self.detail_name.setWordWrap(True)
        self.detail_name.setStyleSheet(
            f"color:{self.c['text_primary']};font-size:13px;font-weight:bold;background:transparent;"
        )
        detail_col.addWidget(self.detail_name)

        self.detail_meta = QLabel("Select a skill to inspect it.")
        self.detail_meta.setTextFormat(Qt.PlainText)
        self.detail_meta.setWordWrap(True)
        self.detail_meta.setStyleSheet(
            f"color:{self.c['text_muted']};font-size:11px;background:transparent;"
        )
        detail_col.addWidget(self.detail_meta)

        # setPlainText() (never setHtml/setMarkdown) is what keeps this a
        # literal viewer -- content is skill-authored text, not trusted markup.
        self.content_view = QTextEdit()
        self.content_view.setReadOnly(True)
        self.content_view.setStyleSheet(f"""
            QTextEdit{{background:{self.c['bg_input']};color:{self.c['text_primary']};
            border:1px solid {self.c['border']};border-radius:7px;padding:6px 10px;
            font-family:'JetBrains Mono',monospace;font-size:11px;}}
        """)
        detail_col.addWidget(self.content_view, 1)
        row.addLayout(detail_col, 3)

        layout.addLayout(row, 1)

        self.status_lbl = _lbl("", self.c)
        layout.addWidget(self.status_lbl)

    # ── Inventory (machine truth: core.skills.list_skills/load_skill) ───────

    def _load(self):
        from core.skills import init_skills_db, list_skills

        db_error = None
        skills = []
        try:
            init_skills_db()
            skills = list_skills()
        except Exception as e:
            db_error = type(e).__name__

        self._rows = [self._augment(s) for s in skills]
        self._apply_filter(self.filter_le.text())

        if db_error:
            self.status_lbl.setText(f"Skill index unavailable ({db_error}).")
        elif not self._rows:
            self.status_lbl.setText("No skills saved yet.")
        else:
            self.status_lbl.setText(f"{len(self._rows)} skill(s).")

    @staticmethod
    def _augment(s: dict) -> dict:
        path = s.get("path") or ""
        exists = bool(path) and os.path.exists(path)
        row = dict(s)
        row["available"] = exists
        row["mtime"] = os.path.getmtime(path) if exists else None
        return row

    def _apply_filter(self, text: str):
        text = (text or "").lower().strip()
        if not text:
            filtered = list(self._rows)
        else:
            filtered = [
                r for r in self._rows
                if text in r["name"].lower() or text in r["description"].lower()
            ]
        self._render(filtered)

    def _render(self, rows: list):
        self._visible = rows
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self.table.setItem(i, 0, QTableWidgetItem(r["name"]))
            status = "Available" if r["available"] else "Missing source"
            self.table.setItem(i, 1, QTableWidgetItem(status))
            when = (
                time.strftime("%Y-%m-%d %H:%M", time.localtime(r["mtime"]))
                if r["mtime"] else "—"
            )
            self.table.setItem(i, 2, QTableWidgetItem(when))
        self.table.blockSignals(False)

        # Re-select by NAME, never by row index -- a refresh/filter can
        # reorder or shrink the table, and row index would silently show a
        # different skill's content under the still-selected old row number.
        if self._selected_name:
            for i, r in enumerate(rows):
                if r["name"] == self._selected_name:
                    self.table.selectRow(i)
                    self._show_detail(r)
                    return
            # Previously selected skill is gone from this view (removed,
            # renamed, or filtered out) -- show it as stale, never fall
            # through to whatever row now occupies its old index.
            self._selected_name = None
        self._show_empty_detail()

    def _show_empty_detail(self):
        self.detail_name.setText("")
        self.detail_meta.setText("Select a skill to inspect it.")
        self.content_view.clear()

    def _on_selected(self):
        idxs = self.table.selectionModel().selectedRows()
        if not idxs:
            self._selected_name = None
            self._show_empty_detail()
            return
        r = self._visible[idxs[0].row()]
        self._selected_name = r["name"]
        self._show_detail(r)

    def _show_detail(self, row: dict):
        self.detail_name.setText(row["name"])
        meta_lines = [
            f"Description: {row['description']}",
            f"Source: {row['path']}",
            "Scope: Persistent skill",
            f"Indexed: {(row.get('created_at') or '')[:16]}",
            f"Last DB update: {(row.get('updated_at') or '')[:16]}",
            "Status: Available" if row["available"]
            else "Status: Missing source (backing file not found on disk)",
        ]
        self.detail_meta.setText("\n".join(meta_lines))

        if not row["available"]:
            self.content_view.setPlainText(
                "[Backing file not found on disk -- content unavailable.]"
            )
            return

        from core.skills import load_skill
        content = load_skill(row["name"])
        if content is None:
            self.content_view.setPlainText(
                "[Content became unavailable after the inventory was read -- "
                "the file may have just been removed.]"
            )
        else:
            self.content_view.setPlainText(content)
