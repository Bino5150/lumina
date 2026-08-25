"""CODING-08A4 -- owner-facing Qt diff-review cockpit panel.

A first-class peer panel next to Chat/Settings (see ui/main_window.py's
``_show_panel``), built the same way SettingsPanel is
(``Panel(agent, colors)``). Read-only viewer: no stage/unstage/discard/
checkout/reset/clean/commit/push/merge/branch-delete/worktree-remove/
Project-rebind controls exist anywhere in this file, and none may be added
without revisiting this module's whole threat model.

Owner Project state is only ever READ here (``agent.project_context.
snapshot()``). Nothing in this file ever calls ``.set()``/``.clear()`` on a
ProjectContextState, ``save_project_binding()``, or ``activate_project`` --
opening, targeting, refreshing, switching, hiding, and closing Review must
never mutate the owner's active Project or durable binding. A managed
worktree is a review target, never a Project rebinding event.

Repository content is untrusted, inert data. Diff text and paths render as
plain text only (QPlainTextEdit, never HTML/Markdown/rich-text
interpretation); hostile filenames are escaped through
core.review_display.escape_display_path (the exact same logic
tools/review.py/A3 uses) before ever reaching a widget; ``change_id``
(never the displayed path) is the only file-retrieval selector.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QSizePolicy,
    QSplitter, QTextEdit, QVBoxLayout, QWidget,
)

import core.git_review as git_review
import core.git_review_snapshot as git_review_snapshot
import core.review_target as review_target
from core.review_display import escape_display_path
from ui.review_controller import (
    CURRENT, CURRENT_METADATA_ONLY, ERROR, REFRESHING, STALE,
    TARGET_UNAVAILABLE, ReviewController,
)

_STATE_LABELS = {
    CURRENT: "CURRENT",
    CURRENT_METADATA_ONLY: "CURRENT — METADATA ONLY",
    STALE: "STALE — REPOSITORY CHANGED",
    TARGET_UNAVAILABLE: "TARGET UNAVAILABLE",
    ERROR: "ERROR",
    REFRESHING: "REFRESHING…",
}
_LIVE_STATES = (CURRENT, CURRENT_METADATA_ONLY)

_CHANGE_ID_ROLE = Qt.UserRole
_LAYER_ROLE = Qt.UserRole + 1

REASON_SUBMODULE = git_review_snapshot.REASON_SUBMODULE
REASON_UNMERGED = git_review_snapshot.REASON_UNMERGED


def _mode_is_symlink(mode: Optional[str]) -> bool:
    return mode == "120000"


def _relation_label(change: "git_review.ReviewChange") -> str:
    labels = {
        "ordinary": "changed", "rename": "renamed", "copy": "copied",
        "unmerged": "conflict", "untracked": "untracked",
    }
    return labels.get(change.relation, change.relation)


class ReviewPanel(QWidget):
    def __init__(self, agent, colors: dict, parent=None):
        super().__init__(parent)
        self.agent = agent
        self.colors = colors
        self.controller = ReviewController(self)
        self.controller.state_changed.connect(self._on_state_changed)
        self.controller.snapshot_ready.connect(self._on_snapshot_ready)
        self.controller.file_ready.connect(self._on_file_ready)

        self._destroyed = False
        self._current_target: Optional[review_target.ReviewTarget] = None
        self._changes_by_id: dict = {}
        self._current_review = None       # core.git_review.ReviewSnapshot
        self._current_fingerprint = None  # core.git_review_snapshot.ReviewFingerprint
        self._current_change_id: Optional[str] = None
        self._current_layer: Optional[str] = None
        self._loaded_hunks: list = []
        self._line_kinds: list = []
        self._interactive_enabled = True

        self._build_ui()

    # ── construction ───────────────────────────────────────────────────

    def _build_ui(self):
        c = self.colors
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # ── Target selector row ──
        selector = QHBoxLayout()
        self.btn_active_project = QPushButton("Use Active Project")
        self.btn_active_project.clicked.connect(self._on_active_project_clicked)
        selector.addWidget(self.btn_active_project)

        self.worktree_combo = QComboBox()
        self.worktree_combo.setMinimumWidth(220)
        selector.addWidget(self.worktree_combo)
        self.btn_use_worktree = QPushButton("Use Worktree")
        self.btn_use_worktree.clicked.connect(self._on_use_worktree_clicked)
        selector.addWidget(self.btn_use_worktree)
        self.btn_refresh_worktrees = QPushButton("⟳")
        self.btn_refresh_worktrees.setFixedWidth(28)
        self.btn_refresh_worktrees.setToolTip("Refresh managed worktree list")
        self.btn_refresh_worktrees.clicked.connect(self._refresh_worktree_combo)
        selector.addWidget(self.btn_refresh_worktrees)

        self.btn_browse = QPushButton("Browse…")
        self.btn_browse.clicked.connect(self._on_browse_clicked)
        selector.addWidget(self.btn_browse)

        selector.addStretch()
        self.btn_refresh = QPushButton("⟳  Refresh")
        self.btn_refresh.clicked.connect(self._on_refresh_clicked)
        self.btn_refresh.setEnabled(False)
        selector.addWidget(self.btn_refresh)
        root.addLayout(selector)

        # ── Applicability banner ──
        self.banner = QLabel("No review target selected.")
        self.banner.setStyleSheet(
            f"background:{c['bg_card']};color:{c['text_muted']};"
            f"border:1px solid {c['border']};border-radius:6px;padding:6px 10px;"
        )
        root.addWidget(self.banner)

        # ── Target header ──
        self.header_frame = QFrame()
        self.header_frame.setStyleSheet(
            f"background:{c['bg_panel']};border:1px solid {c['border']};border-radius:6px;"
        )
        header_layout = QVBoxLayout(self.header_frame)
        header_layout.setContentsMargins(10, 8, 10, 8)
        header_layout.setSpacing(2)
        self.header_labels = {}
        for key, caption in (
            ("label", "Target"), ("kind", "Kind"), ("root", "Repository"),
            ("worktree_id", "Worktree ID"), ("branch", "Branch"),
            ("head", "HEAD"), ("captured_at", "Captured"),
        ):
            row = QLabel(f"{caption}: —")
            row.setStyleSheet(f"color:{c['text_primary']};font-size:11px;background:transparent;")
            row.setWordWrap(True)
            self.header_labels[key] = (caption, row)
            header_layout.addWidget(row)
        root.addWidget(self.header_frame)

        # ── Content: navigator | diff viewer ──
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)

        nav_widget = QWidget()
        nav_layout = QVBoxLayout(nav_widget)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        self.lists = {}
        for layer_key, caption in (
            ("staged", "STAGED"), ("unstaged", "UNSTAGED"), ("untracked", "UNTRACKED"),
        ):
            lbl = QLabel(caption)
            lbl.setStyleSheet(
                f"color:{c['text_dim']};font-size:10px;letter-spacing:2px;background:transparent;"
            )
            nav_layout.addWidget(lbl)
            lw = QListWidget()
            lw.setMaximumHeight(160)
            lw.itemClicked.connect(self._on_change_item_clicked)
            nav_layout.addWidget(lw)
            self.lists[layer_key] = lw
        nav_layout.addStretch()
        splitter.addWidget(nav_widget)

        viewer_widget = QWidget()
        viewer_layout = QVBoxLayout(viewer_widget)
        viewer_layout.setContentsMargins(0, 0, 0, 0)
        self.file_info_label = QLabel("Select a change to view its diff.")
        self.file_info_label.setStyleSheet(f"color:{c['text_muted']};font-size:11px;background:transparent;")
        self.file_info_label.setWordWrap(True)
        viewer_layout.addWidget(self.file_info_label)

        self.diff_view = QPlainTextEdit()
        self.diff_view.setReadOnly(True)
        self.diff_view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.diff_view.setStyleSheet(
            f"background:{c['bg_input']};color:{c['text_primary']};border:1px solid {c['border']};"
        )
        viewer_layout.addWidget(self.diff_view, 1)

        self.btn_load_more = QPushButton("Load more")
        self.btn_load_more.setVisible(False)
        self.btn_load_more.clicked.connect(self._on_load_more_clicked)
        viewer_layout.addWidget(self.btn_load_more)

        splitter.addWidget(viewer_widget)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, 1)

        self._set_interactive(False)

    # ── lifecycle ──────────────────────────────────────────────────────

    def shutdown(self):
        """Called from LuminaWindow.closeEvent(). Idempotent."""
        self._destroyed = True
        self.controller.invalidate()

    # ── target selection ──────────────────────────────────────────────

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh_worktree_combo()

    def _refresh_worktree_combo(self):
        if self._destroyed:
            return
        self.worktree_combo.clear()
        try:
            statuses = review_target.list_managed_worktrees()
        except Exception:
            statuses = ()
        for status in statuses:
            handle = status.handle
            self.worktree_combo.addItem(
                f"{handle.worktree_id}  [{status.state}]", handle.worktree_id,
            )

    def _on_active_project_clicked(self):
        try:
            target = review_target.resolve_active_project_target(
                getattr(self.agent, "project_context", None),
            )
        except review_target.TargetResolutionError as exc:
            self._show_target_error(exc.message)
            return
        self._start_review(target)

    def _on_use_worktree_clicked(self):
        worktree_id = self.worktree_combo.currentData()
        if not worktree_id:
            self._show_target_error("No managed worktree selected.")
            return
        try:
            target = review_target.resolve_worktree_target(worktree_id)
        except review_target.TargetResolutionError as exc:
            self._show_target_error(exc.message)
            return
        self._start_review(target)

    def _on_browse_clicked(self):
        path = QFileDialog.getExistingDirectory(self, "Select repository to review", "")
        if not path:
            return
        try:
            target = review_target.resolve_explicit_path_target(path)
        except review_target.TargetResolutionError as exc:
            self._show_target_error(exc.message)
            return
        self._start_review(target)

    def _on_refresh_clicked(self):
        if self._current_target is None:
            return
        self._start_review(self._current_target, is_refresh=True)

    def _show_target_error(self, message: str):
        QMessageBox.warning(self, "Review target unavailable", message)

    def _start_review(self, target: "review_target.ReviewTarget", is_refresh: bool = False):
        self._current_target = target
        self.btn_refresh.setEnabled(True)
        self._update_header_target(target)
        self.controller.refresh(target.identity)

    # ── controller signal handlers ────────────────────────────────────

    def _on_state_changed(self, state: str, reasons: tuple):
        if self._destroyed:
            return
        label = _STATE_LABELS.get(state, state)
        if reasons:
            label = f"{label} ({', '.join(reasons)})"
        self.banner.setText(label)
        colors = self.colors
        palette = {
            CURRENT: colors["success"],
            CURRENT_METADATA_ONLY: colors["warning"],
            STALE: colors["warning"],
            TARGET_UNAVAILABLE: colors["danger"],
            ERROR: colors["danger"],
            REFRESHING: colors["accent_dim"],
        }
        fg = palette.get(state, colors["text_muted"])
        self.banner.setStyleSheet(
            f"background:{colors['bg_card']};color:{fg};border:1px solid {colors['border']};"
            f"border-radius:6px;padding:6px 10px;font-weight:bold;"
        )
        self._set_interactive(state in _LIVE_STATES)
        if state not in _LIVE_STATES and state != REFRESHING:
            # Section 7: keep old content visible, clearly marked stale --
            # never silently pretend it is still current. The banner above
            # already carries the truth; the change lists/diff below are
            # simply frozen (no further selection) until an explicit Refresh.
            pass

    def _set_interactive(self, enabled: bool):
        self._interactive_enabled = enabled
        for lw in self.lists.values():
            lw.setEnabled(enabled)
        self.btn_load_more.setEnabled(enabled)

    def _update_header_target(self, target: "review_target.ReviewTarget"):
        self._set_header("label", target.label)
        self._set_header("kind", target.identity.kind)
        self._set_header("root", escape_display_path(target.identity.canonical_root))
        self._set_header("worktree_id", target.worktree_id or "—")
        self._set_header("branch", "—")
        self._set_header("head", "—")
        self._set_header("captured_at", "—")

    def _set_header(self, key: str, value: str):
        caption, row = self.header_labels[key]
        row.setText(f"{caption}: {value}")

    def _on_snapshot_ready(self, snapshot):
        if self._destroyed:
            return
        review = snapshot.review
        self._current_review = review
        self._current_fingerprint = snapshot.fingerprint
        self._set_header(
            "branch",
            "(detached)" if review.detached else (review.branch or "(unborn)"),
        )
        self._set_header("head", review.head or "—")
        self._set_header("captured_at", snapshot.captured_at)

        self._changes_by_id = {
            git_review_snapshot.review_change_id(change): change
            for change in review.changes
        }
        for layer_key, lw in self.lists.items():
            lw.clear()
        for change in review.changes:
            change_id = git_review_snapshot.review_change_id(change)
            display = escape_display_path(change.path)
            if change.original_path is not None:
                display = f"{escape_display_path(change.original_path)} → {display}"
            label = f"{display}  [{_relation_label(change)}]"
            if change.staged:
                self._add_change_item("staged", change_id, "staged", label, change)
            if change.unstaged:
                self._add_change_item("unstaged", change_id, "unstaged", label, change)
            if change.untracked:
                self._add_change_item("untracked", change_id, "unstaged", label, change)

    def _add_change_item(self, list_key, change_id, layer, label, change):
        item = QListWidgetItem(label)
        item.setData(_CHANGE_ID_ROLE, change_id)
        item.setData(_LAYER_ROLE, layer)
        if change.submodule.is_submodule:
            item.setToolTip("Submodule (gitlink)")
        elif change.relation == "unmerged":
            item.setToolTip("Unmerged / conflict")
        elif _mode_is_symlink(change.worktree_mode) or _mode_is_symlink(change.index_mode):
            item.setToolTip("Symlink")
        self.lists[list_key].addItem(item)

    # ── file selection / diff viewer ──────────────────────────────────

    def _on_change_item_clicked(self, item: QListWidgetItem):
        if not self._interactive_enabled:
            return
        change_id = item.data(_CHANGE_ID_ROLE)
        layer = item.data(_LAYER_ROLE)
        change = self._changes_by_id.get(change_id)
        if change is None:
            return
        self._current_change_id = change_id
        self._current_layer = layer
        self._loaded_hunks = []
        self.btn_load_more.setVisible(False)

        if change.submodule.is_submodule:
            self._render_submodule(change)
            return
        if change.relation == "unmerged":
            self._render_unmerged(change)
            return

        self.file_info_label.setText(
            f"{escape_display_path(change.path)}  [{layer}]  (loading…)"
        )
        # Deliberately does NOT clear diff_view here -- section 7's "keep
        # old content visible only if clearly marked stale" means a
        # not-yet-resolved selection must never blank the viewer; it is
        # only ever replaced once a LIVE result actually arrives in
        # _on_file_ready(). A non-live (stale/unavailable/error) outcome
        # leaves the previous content exactly as it was.
        self.controller.select_file(change_id, layer, start_hunk_index=0)

    def _render_submodule(self, change: "git_review.ReviewChange"):
        lines = [
            "SUBMODULE",
            f"path: {escape_display_path(change.path)}",
            f"old commit: {change.head_object_id or '—'}",
            f"new commit: {change.index_object_id or '—'}",
            f"dirty tracked state: {change.submodule.has_tracked_changes}",
            f"dirty untracked state: {change.submodule.has_untracked_changes}",
            "nested content not reviewed",
        ]
        self.file_info_label.setText(f"{escape_display_path(change.path)}  [submodule]")
        self.diff_view.setPlainText("\n".join(lines))
        self._line_kinds = ["metadata"] * len(lines)
        self._apply_line_highlighting()

    def _render_unmerged(self, change: "git_review.ReviewChange"):
        lines = ["UNMERGED / CONFLICT", f"path: {escape_display_path(change.path)}", ""]
        stage_names = {1: "base", 2: "ours", 3: "theirs"}
        seen = set()
        for stage in change.unmerged_stages:
            seen.add(stage.stage)
            lines.append(f"{stage_names.get(stage.stage, stage.stage)}: mode {stage.mode}  object {stage.object_id}")
        for missing in (1, 2, 3):
            if missing not in seen:
                lines.append(f"{stage_names[missing]}: (absent)")
        lines.append("")
        lines.append("Specialized conflict-content viewing is not supported in this version.")
        self.file_info_label.setText(f"{escape_display_path(change.path)}  [conflict]")
        self.diff_view.setPlainText("\n".join(lines))
        self._line_kinds = ["metadata"] * len(lines)
        self._apply_line_highlighting()

    def _on_load_more_clicked(self):
        if self._current_change_id is None or self._current_layer is None:
            return
        next_cursor = getattr(self, "_next_cursor", None)
        if next_cursor is None:
            return
        self.controller.load_more(self._current_change_id, self._current_layer, next_cursor)

    def _on_file_ready(self, outcome):
        if self._destroyed:
            return
        if outcome.change_id != self._current_change_id or outcome.layer != self._current_layer:
            return  # a different file is now selected; ignore
        change = self._changes_by_id.get(outcome.change_id)
        path_display = escape_display_path(change.path) if change is not None else outcome.change_id

        if outcome.file is None:
            self.file_info_label.setText(f"{path_display}  [{outcome.layer}]  — content unavailable")
            self.btn_load_more.setVisible(False)
            return

        file_result = outcome.file
        self._next_cursor = file_result.next_cursor
        self.btn_load_more.setVisible(file_result.next_cursor is not None)

        if file_result.binary:
            self.file_info_label.setText(f"{path_display}  [{outcome.layer}]  — binary file changed")
            self.diff_view.setPlainText(
                f"Binary file changed\npath: {path_display}\nlayer: {outcome.layer}"
            )
            self._line_kinds = ["metadata", "metadata", "metadata"]
            self._apply_line_highlighting()
            return

        self._loaded_hunks.extend(file_result.hunks)
        status_note = "" if file_result.complete else f"  (incomplete: {file_result.omission_reason})"
        self.file_info_label.setText(f"{path_display}  [{outcome.layer}]{status_note}")
        self._render_hunks()

    def _render_hunks(self):
        lines = []
        kinds = []
        for hunk in self._loaded_hunks:
            if hunk.omitted:
                text = f"[Hunk omitted: {hunk.omission_reason}]"
                lines.append(text)
                kinds.append("omitted")
                continue
            header = hunk.header
            if header is not None:
                heading = f" {header.section_heading}" if header.section_heading else ""
                lines.append(
                    f"@@ -{header.old_start},{header.old_count} "
                    f"+{header.new_start},{header.new_count} @@{heading}"
                )
                kinds.append("hunk_header")
            old_no = header.old_start if header else None
            new_no = header.new_start if header else None
            for line in hunk.lines:
                if line.kind == "context":
                    prefix = " "
                    old_disp, new_disp = old_no, new_no
                    old_no = (old_no + 1) if old_no is not None else None
                    new_no = (new_no + 1) if new_no is not None else None
                elif line.kind == "add":
                    prefix = "+"
                    old_disp, new_disp = None, new_no
                    new_no = (new_no + 1) if new_no is not None else None
                elif line.kind == "remove":
                    prefix = "-"
                    old_disp, new_disp = old_no, None
                    old_no = (old_no + 1) if old_no is not None else None
                else:
                    prefix = "\\"
                    old_disp, new_disp = None, None
                old_s = f"{old_disp:>5}" if old_disp is not None else "     "
                new_s = f"{new_disp:>5}" if new_disp is not None else "     "
                lines.append(f"{old_s} {new_s} {prefix} {line.text}")
                kinds.append(line.kind)
        self.diff_view.setPlainText("\n".join(lines))
        self._line_kinds = kinds
        self._apply_line_highlighting()

    def _apply_line_highlighting(self):
        colors = self.colors
        fmt_map = {
            "add": QColor(colors["success"]),
            "remove": QColor(colors["danger"]),
            "hunk_header": QColor(colors["accent"]),
            "omitted": QColor(colors["warning"]),
            "metadata": QColor(colors["text_muted"]),
        }
        selections = []
        block = self.diff_view.document().firstBlock()
        for kind in self._line_kinds:
            color = fmt_map.get(kind)
            if color is not None and block.isValid():
                fmt = QTextCharFormat()
                fmt.setForeground(color)
                cursor = QTextCursor(block)
                cursor.select(QTextCursor.LineUnderCursor)
                sel = QTextEdit.ExtraSelection()
                sel.cursor = cursor
                sel.format = fmt
                selections.append(sel)
            block = block.next()
        self.diff_view.setExtraSelections(selections)
