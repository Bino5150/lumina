from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTextEdit, QFrame,
    QCheckBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QMessageBox, QComboBox, QInputDialog
)
from PySide6.QtCore import Qt

import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import config
from core import persistence

from ._widgets import _sec, _lbl, _btn, _spin, _table


# ── Tab: Tools ─────────────────────────────────────────────────────────────────

class ToolsTab(QWidget):
    def __init__(self, agent, c: dict, parent=None):
        super().__init__(parent)
        self.agent = agent
        self.c = c
        self._prefs = persistence.load()
        self._current_profile_path = None
        self._pending_names = []
        self._pending_action_ids = []
        self._build()
        self._load_profiles()
        self._load_pending_tools()
        self._load_pending_actions()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)

        # ── Profile bar ──
        profile_frame = QFrame()
        profile_frame.setStyleSheet(f"QFrame{{background:{self.c['bg_card']};border:1px solid {self.c['border']};border-radius:8px;}}")
        profile_layout = QHBoxLayout(profile_frame)
        profile_layout.setContentsMargins(12, 8, 12, 8)
        profile_layout.setSpacing(8)

        profile_layout.addWidget(_lbl("Profile:", self.c))

        self.profile_combo = QComboBox()
        self.profile_combo.setFixedHeight(32)
        self.profile_combo.setMinimumWidth(180)
        self.profile_combo.setStyleSheet(f"""
            QComboBox{{background:{self.c['bg_input']};color:{self.c['text_primary']};
            border:1px solid {self.c['border']};border-radius:7px;padding:4px 10px;font-size:12px;}}
            QComboBox::drop-down{{border:none;width:20px;}}
        """)
        self.profile_combo.currentIndexChanged.connect(self._on_profile_selected)
        profile_layout.addWidget(self.profile_combo)

        self.count_lbl = _lbl("", self.c)
        profile_layout.addWidget(self.count_lbl)
        profile_layout.addStretch()

        new_btn = _btn("＋ New", self.c)
        new_btn.setFixedHeight(30)
        new_btn.clicked.connect(self._new_profile)
        profile_layout.addWidget(new_btn)

        save_profile_btn = _btn("💾 Save", self.c, accent=True)
        save_profile_btn.setFixedHeight(30)
        save_profile_btn.clicked.connect(self._save_profile)
        profile_layout.addWidget(save_profile_btn)

        self.del_profile_btn = _btn("✕ Delete", self.c, danger=True)
        self.del_profile_btn.setFixedHeight(30)
        self.del_profile_btn.clicked.connect(self._delete_profile)
        profile_layout.addWidget(self.del_profile_btn)

        layout.addWidget(profile_frame)

        # ── Subagents & Background Tasks (S51 Part B) ────────────────────
        # A human-owner config surface, not a model-controllable one — the
        # model can never set its own SUBAGENTS_ENABLED/BACKGROUND_TASKS_ENABLED/
        # MAX_SUBAGENT_DEPTH regardless of what these widgets are set to.
        # Placed here rather than GeneralTab: these two toggles gate TOOL
        # REGISTRATION (spawn_subagent / run_background_subagent /
        # check_background_task / schedule_background_subagent) the same way
        # everything else in this tab manages tool availability — checked
        # immediately on toggle, same as the per-tool checkboxes in the table
        # below, not batched behind a separate Save button.
        layout.addWidget(_sec("SUBAGENTS & BACKGROUND TASKS", self.c))
        layout.addWidget(_lbl(
            "Gates the model-facing spawn_subagent tool and the background/"
            "scheduled task queue. Both default off. Independent of each "
            "other — background tasks call spawn_subagent() as a plain "
            "function, not through this tool registry.", self.c
        ))
        subagent_row = QHBoxLayout()
        subagent_row.setSpacing(20)
        self.subagents_enabled_cb = QCheckBox("Enable subagent spawning")
        self.subagents_enabled_cb.setChecked(config.SUBAGENTS_ENABLED)
        self.subagents_enabled_cb.toggled.connect(self._on_subagents_toggled)
        subagent_row.addWidget(self.subagents_enabled_cb)
        self.background_tasks_enabled_cb = QCheckBox("Enable background/scheduled tasks")
        self.background_tasks_enabled_cb.setChecked(config.BACKGROUND_TASKS_ENABLED)
        self.background_tasks_enabled_cb.toggled.connect(self._on_background_tasks_toggled)
        subagent_row.addWidget(self.background_tasks_enabled_cb)
        depth_col = QVBoxLayout()
        depth_col.addWidget(_lbl(
            "Max Subagent Depth — recursion limit for subagents spawning "
            "further subagents. Shipped default: 2.", self.c
        ))
        self.subagent_depth_spin = _spin(config.MAX_SUBAGENT_DEPTH, 1, 5, 1, self.c)
        self.subagent_depth_spin.editingFinished.connect(self._on_subagent_depth_changed)
        depth_col.addWidget(self.subagent_depth_spin)
        subagent_row.addLayout(depth_col)
        layout.addLayout(subagent_row)

        # ── Pending tools review (S41) ──────────────────────────────────
        # The only thing this replaces is scripts/approve_tool.py's ROLE of
        # "run against the live agent, no restart" — not its safety property.
        # Approve still requires a human to have actually looked at the
        # source (enforced below: Approve stays disabled until a pending
        # tool is selected, which populates the read-only preview showing
        # exactly what would run), and this button lives only in the desktop
        # Settings UI — nothing an agent says over chat, Telegram, or Discord
        # can reach it. create_tool() still only stages; this is still the
        # only place untrusted code actually executes.
        layout.addWidget(_sec("PENDING TOOLS — AWAITING REVIEW", self.c))
        pending_row = QHBoxLayout()
        pending_row.setSpacing(10)

        pending_list_col = QVBoxLayout()
        self.pending_list = QTableWidget(0, 1)
        self.pending_list.setFixedHeight(110)
        self.pending_list.horizontalHeader().setVisible(False)
        self.pending_list.verticalHeader().setVisible(False)
        self.pending_list.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.pending_list.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.pending_list.horizontalHeader().setStretchLastSection(True)
        self.pending_list.setStyleSheet(f"""
            QTableWidget{{background:{self.c['bg_card']};color:{self.c['text_primary']};
            border:1px solid {self.c['border']};border-radius:8px;gridline-color:{self.c['border']};font-size:12px;}}
            QTableWidget::item{{padding:6px 10px;border:none;}}
            QTableWidget::item:selected{{background:{self.c['accent_glow']};color:{self.c['accent']};}}
        """)
        self.pending_list.itemSelectionChanged.connect(self._on_pending_selected)
        pending_list_col.addWidget(self.pending_list)
        pending_row.addLayout(pending_list_col, 1)

        pending_preview_col = QVBoxLayout()
        pending_preview_col.addWidget(_lbl("Source (read this before approving)", self.c))
        self.pending_preview = QTextEdit()
        self.pending_preview.setReadOnly(True)
        self.pending_preview.setFixedHeight(110)
        self.pending_preview.setStyleSheet(f"""
            QTextEdit{{background:{self.c['bg_input']};color:{self.c['text_primary']};
            border:1px solid {self.c['border']};border-radius:7px;padding:6px 10px;
            font-family:'JetBrains Mono',monospace;font-size:11px;}}
        """)
        pending_preview_col.addWidget(self.pending_preview)
        pending_row.addLayout(pending_preview_col, 2)
        layout.addLayout(pending_row)

        pending_btn_row = QHBoxLayout()
        pending_btn_row.addStretch()
        pending_refresh_btn = _btn("⟳ Refresh", self.c)
        pending_refresh_btn.setFixedHeight(30)
        pending_refresh_btn.clicked.connect(self._load_pending_tools)
        pending_btn_row.addWidget(pending_refresh_btn)
        self.pending_reject_btn = _btn("✕ Reject", self.c, danger=True)
        self.pending_reject_btn.setFixedHeight(30)
        self.pending_reject_btn.setEnabled(False)
        self.pending_reject_btn.clicked.connect(self._reject_pending)
        pending_btn_row.addWidget(self.pending_reject_btn)
        self.pending_approve_btn = _btn("✓ Approve & Load", self.c, accent=True)
        self.pending_approve_btn.setFixedHeight(30)
        self.pending_approve_btn.setEnabled(False)
        self.pending_approve_btn.clicked.connect(self._approve_pending)
        pending_btn_row.addWidget(self.pending_approve_btn)
        layout.addLayout(pending_btn_row)

        self.pending_status_lbl = _lbl("", self.c)
        layout.addWidget(self.pending_status_lbl)

        # ── Pending actions review (MB-33 Tier 2) ───────────────────────
        # edit_prompt/reset_chat/delete_knowledge/delete_memory now only
        # stage_action() -- they never mutate live state on their own.
        # This panel is the only place a staged action is actually applied
        # (_apply_action is not registered as a tool; it's called only from
        # here, against the live agent's real registry/context, same
        # proven pattern as the Pending Tools panel above). Approve stays
        # disabled until a row is selected, which populates the read-only
        # preview showing exactly what's staged.
        layout.addWidget(_sec("PENDING ACTIONS — AWAITING REVIEW", self.c))
        pending_actions_row = QHBoxLayout()
        pending_actions_row.setSpacing(10)

        pending_actions_list_col = QVBoxLayout()
        self.pending_actions_list = QTableWidget(0, 1)
        self.pending_actions_list.setFixedHeight(110)
        self.pending_actions_list.horizontalHeader().setVisible(False)
        self.pending_actions_list.verticalHeader().setVisible(False)
        self.pending_actions_list.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.pending_actions_list.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.pending_actions_list.horizontalHeader().setStretchLastSection(True)
        self.pending_actions_list.setStyleSheet(f"""
            QTableWidget{{background:{self.c['bg_card']};color:{self.c['text_primary']};
            border:1px solid {self.c['border']};border-radius:8px;gridline-color:{self.c['border']};font-size:12px;}}
            QTableWidget::item{{padding:6px 10px;border:none;}}
            QTableWidget::item:selected{{background:{self.c['accent_glow']};color:{self.c['accent']};}}
        """)
        self.pending_actions_list.itemSelectionChanged.connect(self._on_pending_action_selected)
        pending_actions_list_col.addWidget(self.pending_actions_list)
        pending_actions_row.addLayout(pending_actions_list_col, 1)

        pending_actions_preview_col = QVBoxLayout()
        pending_actions_preview_col.addWidget(_lbl("Staged payload (read this before approving)", self.c))
        self.pending_actions_preview = QTextEdit()
        self.pending_actions_preview.setReadOnly(True)
        self.pending_actions_preview.setFixedHeight(110)
        self.pending_actions_preview.setStyleSheet(f"""
            QTextEdit{{background:{self.c['bg_input']};color:{self.c['text_primary']};
            border:1px solid {self.c['border']};border-radius:7px;padding:6px 10px;
            font-family:'JetBrains Mono',monospace;font-size:11px;}}
        """)
        pending_actions_preview_col.addWidget(self.pending_actions_preview)
        pending_actions_row.addLayout(pending_actions_preview_col, 2)
        layout.addLayout(pending_actions_row)

        pending_actions_btn_row = QHBoxLayout()
        pending_actions_btn_row.addStretch()
        pending_actions_refresh_btn = _btn("⟳ Refresh", self.c)
        pending_actions_refresh_btn.setFixedHeight(30)
        pending_actions_refresh_btn.clicked.connect(self._load_pending_actions)
        pending_actions_btn_row.addWidget(pending_actions_refresh_btn)
        self.pending_actions_reject_btn = _btn("✕ Reject", self.c, danger=True)
        self.pending_actions_reject_btn.setFixedHeight(30)
        self.pending_actions_reject_btn.setEnabled(False)
        self.pending_actions_reject_btn.clicked.connect(self._reject_pending_action)
        pending_actions_btn_row.addWidget(self.pending_actions_reject_btn)
        self.pending_actions_approve_btn = _btn("✓ Approve & Apply", self.c, accent=True)
        self.pending_actions_approve_btn.setFixedHeight(30)
        self.pending_actions_approve_btn.setEnabled(False)
        self.pending_actions_approve_btn.clicked.connect(self._approve_pending_action)
        pending_actions_btn_row.addWidget(self.pending_actions_approve_btn)
        layout.addLayout(pending_actions_btn_row)

        self.pending_actions_status_lbl = _lbl("", self.c)
        layout.addWidget(self.pending_actions_status_lbl)

        # ── Tool table header ──
        top = QHBoxLayout()
        top.addStretch()

        refresh_btn = _btn("⟳ Refresh", self.c)
        refresh_btn.setFixedHeight(30)
        refresh_btn.clicked.connect(self._load_tools)
        top.addWidget(refresh_btn)

        disable_all_btn = _btn("Disable All", self.c, danger=True)
        disable_all_btn.setFixedHeight(30)
        disable_all_btn.clicked.connect(self._disable_all)
        top.addWidget(disable_all_btn)

        enable_all_btn = _btn("Enable All", self.c, accent=True)
        enable_all_btn.setFixedHeight(30)
        enable_all_btn.clicked.connect(self._enable_all)
        top.addWidget(enable_all_btn)
        layout.addLayout(top)

        # ── Tool table ──
        self.table = _table(["Tool", "Description", "Enabled"], self.c)
        self.table.setColumnWidth(0, 180)
        self.table.setColumnWidth(1, 460)
        self.table.setColumnWidth(2, 70)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)

        note = QLabel("Select a profile to load its tool set. Save to write changes back to the profile file.")
        note.setStyleSheet(f"color:{self.c['text_dim']};font-size:11px;font-style:italic;background:transparent;")
        layout.addWidget(note)

    def _load_pending_tools(self, preserve_status: bool = False):
        """List whatever's staged in tools/_pending/ — same directory
        create_tool() writes to and scripts/approve_tool.py --list reads."""
        from tools.toolmaker import PENDING_DIR
        os.makedirs(PENDING_DIR, exist_ok=True)
        self._pending_names = sorted(
            f[:-3] for f in os.listdir(PENDING_DIR) if f.endswith(".py")
        )
        self.pending_list.setRowCount(len(self._pending_names))
        for row, name in enumerate(self._pending_names):
            self.pending_list.setItem(row, 0, QTableWidgetItem(name))
        self.pending_preview.clear()
        self.pending_approve_btn.setEnabled(False)
        self.pending_reject_btn.setEnabled(False)
        if not preserve_status:
            self.pending_status_lbl.setText(
                "" if self._pending_names else "Nothing pending review."
            )

    def _on_pending_selected(self):
        rows = self.pending_list.selectionModel().selectedRows()
        has_selection = bool(rows)
        self.pending_approve_btn.setEnabled(has_selection)
        self.pending_reject_btn.setEnabled(has_selection)
        if not has_selection:
            self.pending_preview.clear()
            return
        name = self._pending_names[rows[0].row()]
        from tools.toolmaker import PENDING_DIR
        path = os.path.join(PENDING_DIR, f"{name}.py")
        try:
            with open(path, "r", encoding="utf-8") as f:
                self.pending_preview.setPlainText(f.read())
        except Exception as e:
            self.pending_preview.setPlainText(f"[Could not read source: {e}]")

    def _selected_pending_name(self):
        rows = self.pending_list.selectionModel().selectedRows()
        if not rows:
            return None
        return self._pending_names[rows[0].row()]

    def _approve_pending(self):
        name = self._selected_pending_name()
        if not name:
            return
        reply = QMessageBox.question(
            self, "Approve Tool",
            f"Approve and load '{name}' into the live tool registry now?\n\n"
            "This runs the source shown in the preview — make sure you've "
            "actually read it.",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return
        from tools.toolmaker import approve_pending_tool
        result = approve_pending_tool(name, self.agent.registry)
        self.pending_status_lbl.setText(result)
        self._load_pending_tools(preserve_status=True)
        self._load_tools()  # refresh the enabled-tools table below — new tool is now live

    def _reject_pending(self):
        name = self._selected_pending_name()
        if not name:
            return
        reply = QMessageBox.question(
            self, "Reject Tool",
            f"Discard pending tool '{name}' without ever loading it? This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return
        from tools.toolmaker import PENDING_DIR, _append_audit_log
        path = os.path.join(PENDING_DIR, f"{name}.py")
        try:
            os.remove(path)
            _append_audit_log("rejected", name)
            self.pending_status_lbl.setText(f"Discarded '{name}'.")
        except Exception as e:
            self.pending_status_lbl.setText(f"Error rejecting '{name}': {e}")
        self._load_pending_tools(preserve_status=True)

    def _load_pending_actions(self, preserve_status: bool = False):
        """List whatever's staged in pending_actions.json -- same queue
        stage_action() writes to for edit_prompt/reset_chat/delete_knowledge/
        delete_memory."""
        from tools.pending_actions import _load_queue
        queue = _load_queue()
        self._pending_action_ids = sorted(queue.keys(), key=lambda aid: queue[aid]["staged_at"])
        self.pending_actions_list.setRowCount(len(self._pending_action_ids))
        for row, aid in enumerate(self._pending_action_ids):
            entry = queue[aid]
            self.pending_actions_list.setItem(row, 0, QTableWidgetItem(f"[{aid}] {entry['kind']}"))
        self.pending_actions_preview.clear()
        self.pending_actions_approve_btn.setEnabled(False)
        self.pending_actions_reject_btn.setEnabled(False)
        if not preserve_status:
            self.pending_actions_status_lbl.setText(
                "" if self._pending_action_ids else "Nothing pending review."
            )

    def _on_pending_action_selected(self):
        rows = self.pending_actions_list.selectionModel().selectedRows()
        has_selection = bool(rows)
        self.pending_actions_approve_btn.setEnabled(has_selection)
        self.pending_actions_reject_btn.setEnabled(has_selection)
        if not has_selection:
            self.pending_actions_preview.clear()
            return
        aid = self._pending_action_ids[rows[0].row()]
        from tools.pending_actions import _load_queue
        entry = _load_queue().get(aid, {})
        self.pending_actions_preview.setPlainText(json.dumps(entry, indent=2))

    def _selected_pending_action_id(self):
        rows = self.pending_actions_list.selectionModel().selectedRows()
        if not rows:
            return None
        return self._pending_action_ids[rows[0].row()]

    def _approve_pending_action(self):
        aid = self._selected_pending_action_id()
        if not aid:
            return
        from tools.pending_actions import _load_queue
        entry = _load_queue().get(aid, {})
        reply = QMessageBox.question(
            self, "Approve Action",
            f"Apply staged action #{aid} ({entry.get('kind', '?')}) now?\n\n"
            f"{json.dumps(entry.get('payload', {}))}\n\n"
            "This runs immediately against live state — make sure you've "
            "actually read the staged payload.",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return
        from tools.pending_actions import _apply_action
        result = _apply_action(aid, self.agent)
        self.pending_actions_status_lbl.setText(result)
        self._load_pending_actions(preserve_status=True)

    def _reject_pending_action(self):
        aid = self._selected_pending_action_id()
        if not aid:
            return
        reply = QMessageBox.question(
            self, "Reject Action",
            f"Discard staged action #{aid} without ever applying it? This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return
        from tools.pending_actions import reject_pending_action
        result = reject_pending_action(aid)
        self.pending_actions_status_lbl.setText(result)
        self._load_pending_actions(preserve_status=True)

    def _load_profiles(self):
        """Populate the profile dropdown from tool_profiles/ directory."""
        from core.tool_profiles import list_profiles, profile_display_name
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self._profiles = list_profiles()
        for p in self._profiles:
            self.profile_combo.addItem(profile_display_name(p), p["_file"])
        self.profile_combo.blockSignals(False)
        # Load current live tool state into table without selecting a profile
        self._load_tools()

    def _on_profile_selected(self, idx: int):
        if idx < 0 or idx >= len(self._profiles):
            return
        profile = self._profiles[idx]
        self._current_profile_path = profile["_file"]
        from core.tool_profiles import apply_tool_profile
        apply_tool_profile(self.agent.registry, profile_name=profile.get("name"), owner=True)
        self._save_state()
        self._load_tools()

    def _load_tools(self):
        """Render tool table reflecting current agent registry state."""
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        tools = self.agent.registry._tools
        disabled = set(self.agent.registry.get_disabled())
        enabled_count = 0

        for name, data in tools.items():
            i = self.table.rowCount()
            self.table.insertRow(i)
            self.table.setItem(i, 0, QTableWidgetItem(name))
            desc = data["schema"]["function"].get("description", "")
            self.table.setItem(i, 1, QTableWidgetItem(desc))
            cb = QCheckBox()
            cb.setChecked(name not in disabled)
            cb.setStyleSheet("QCheckBox { margin-left: 22px; }")
            cb.stateChanged.connect(lambda state, n=name: self._toggle(n, state))
            self.table.setCellWidget(i, 2, cb)
            if name not in disabled:
                enabled_count += 1

        total = len(tools)
        self.count_lbl.setText(f"({enabled_count}/{total})")
        self.table.blockSignals(False)

    def _toggle(self, name: str, state: int):
        if state == Qt.Checked:
            self.agent.registry.enable(name)
        else:
            self.agent.registry.disable(name)
        self._save_state()
        self._update_count()

    def _on_subagents_toggled(self, checked: bool):
        """SUBAGENTS_ENABLED — config.py value, persisted to prefs.json like
        DREAM_SWEEP_ENABLED, applied to the live agent immediately (no
        restart) rather than only taking effect on next launch. Tool
        registration is a one-time decision baked into the registry at
        LuminaAgent.__init__, so flipping the config flag alone wouldn't
        retroactively register/unregister spawn_subagent on an already-
        constructed agent — re-run the same registration call __init__
        uses, then explicitly enable/disable so a prior disable() from an
        earlier off-toggle this session doesn't linger."""
        config.SUBAGENTS_ENABLED = checked
        prefs = persistence.load()
        prefs["subagents_enabled"] = checked
        persistence.save(prefs)
        if checked:
            from tools.subagent import register_subagent_tools
            register_subagent_tools(self.agent.registry, self.agent._subagent_depth)
            self.agent.registry.enable("spawn_subagent")
        else:
            self.agent.registry.disable("spawn_subagent")
        self._load_tools()

    def _on_background_tasks_toggled(self, checked: bool):
        """BACKGROUND_TASKS_ENABLED — same live-apply reasoning as
        _on_subagents_toggled above, for the three background-task tools."""
        config.BACKGROUND_TASKS_ENABLED = checked
        prefs = persistence.load()
        prefs["background_tasks_enabled"] = checked
        persistence.save(prefs)
        task_tool_names = ("run_background_subagent", "check_background_task", "schedule_background_subagent")
        if checked:
            from tools.tasks import register_task_tools
            register_task_tools(self.agent.registry, self.agent)
            for name in task_tool_names:
                self.agent.registry.enable(name)
        else:
            for name in task_tool_names:
                self.agent.registry.disable(name)
        self._load_tools()

    def _on_subagent_depth_changed(self):
        """MAX_SUBAGENT_DEPTH — re-read live on every spawn_subagent()/
        register_subagent_tools() call rather than captured once at
        registration time, so unlike the two toggles above this needs no
        registry surgery to apply immediately; persisting to config.py +
        prefs.json is the whole job here."""
        value = self.subagent_depth_spin.value()
        config.MAX_SUBAGENT_DEPTH = value
        prefs = persistence.load()
        prefs["max_subagent_depth"] = value
        persistence.save(prefs)

    def _disable_all(self):
        for name in self.agent.registry.list_tools():
            self.agent.registry.disable(name)
        self._save_state()
        self._load_tools()

    def _enable_all(self):
        for name in self.agent.registry.list_tools():
            self.agent.registry.enable(name)
        self._save_state()
        self._load_tools()

    def _update_count(self):
        total = len(self.agent.registry.list_tools())
        enabled = len(self.agent.registry.list_enabled())
        self.count_lbl.setText(f"({enabled}/{total})")

    def _save_state(self):
        """Persist disabled tool list to prefs.json."""
        self._prefs = persistence.load()
        self._prefs["disabled_tools"] = self.agent.registry.get_disabled()
        persistence.save(self._prefs)

    def _save_profile(self):
        """Write current tool state back to the selected profile JSON."""
        from core.tool_profiles import save_profile, list_profiles, profile_display_name
        if not self._current_profile_path:
            QMessageBox.warning(self, "No Profile", "Select a profile first.")
            return
        enabled = self.agent.registry.list_enabled()
        # Load existing profile to preserve name/description
        try:
            from core.tool_profiles import load_profile
            data = load_profile(self._current_profile_path)
        except Exception:
            data = {}
        data["enabled"] = enabled
        save_profile(self._current_profile_path, data)
        # Refresh dropdown to update count
        current_path = self._current_profile_path
        self._load_profiles()
        # Reselect the same profile
        for i in range(self.profile_combo.count()):
            if self.profile_combo.itemData(i) == current_path:
                self.profile_combo.blockSignals(True)
                self.profile_combo.setCurrentIndex(i)
                self.profile_combo.blockSignals(False)
                break
        self._current_profile_path = current_path

    def _new_profile(self):
        """Create a new empty tool profile."""
        from core.tool_profiles import save_profile, PROFILES_DIR, fname_from_name, list_profiles, profile_display_name
        name, ok = QInputDialog.getText(self, "New Tool Profile", "Profile name:")
        if not ok or not name.strip():
            return
        name = name.strip()
        fname = fname_from_name(name)
        path = os.path.join(PROFILES_DIR, fname)
        if os.path.exists(path):
            QMessageBox.warning(self, "Exists", f"A profile named '{name}' already exists.")
            return
        data = {"name": name, "description": "", "enabled": []}
        save_profile(path, data)
        self._load_profiles()
        # Select the new profile
        for i in range(self.profile_combo.count()):
            if self.profile_combo.itemData(i) == path:
                self.profile_combo.setCurrentIndex(i)
                break

    def _delete_profile(self):
        """Delete the currently selected profile file."""
        from core.tool_profiles import delete_profile
        if not self._current_profile_path:
            return
        idx = self.profile_combo.currentIndex()
        if idx < 0 or idx >= len(self._profiles):
            return
        name = self._profiles[idx].get("name", "this profile")
        reply = QMessageBox.question(
            self, "Delete Profile",
            f"Delete '{name}'? This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            delete_profile(self._current_profile_path)
            self._current_profile_path = None
            self._load_profiles()
