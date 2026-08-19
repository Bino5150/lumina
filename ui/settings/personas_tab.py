from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QScrollArea, QFileDialog, QMessageBox, QSlider,
    QLineEdit, QInputDialog
)
from PySide6.QtGui import QIcon
from PySide6.QtCore import Qt

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import config

from ._widgets import _sec, _lbl, _te, _le, _btn, _combo, make_round_pixmap, ButtonFeedback


# ── Tab: Personas ─────────────────────────────────────────────────────────────

class PersonasTab(QWidget):
    def __init__(self, agent, c: dict, parent=None):
        super().__init__(parent)
        self.agent = agent
        self.c = c
        self._current_path = None
        self._current_persona = None
        self._build()
        self._load_personas()

    def _build(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Left sidebar ──
        sidebar = QFrame()
        sidebar.setFixedWidth(220)
        sidebar.setStyleSheet(f"QFrame{{background:{self.c['bg_sidebar']};border-right:1px solid {self.c['border']};}}")
        sb_layout = QVBoxLayout(sidebar)
        sb_layout.setContentsMargins(0, 0, 0, 0)
        sb_layout.setSpacing(0)

        # Sidebar header
        sb_header = QFrame()
        sb_header.setFixedHeight(48)
        sb_header.setStyleSheet(f"QFrame{{background:{self.c['bg_panel']};border-bottom:1px solid {self.c['border']};}}")
        sb_header_layout = QHBoxLayout(sb_header)
        sb_header_layout.setContentsMargins(12, 0, 8, 0)
        sb_lbl = QLabel("PERSONAS")
        sb_lbl.setStyleSheet(f"color:{self.c['accent']};font-size:10px;font-weight:bold;letter-spacing:2px;background:transparent;")
        sb_header_layout.addWidget(sb_lbl)
        sb_header_layout.addStretch()

        new_btn = QPushButton("＋")
        new_btn.setFixedSize(28, 28)
        new_btn.setCursor(Qt.PointingHandCursor)
        new_btn.setToolTip("New Persona")
        new_btn.setStyleSheet(f"""
            QPushButton{{background:{self.c['accent_glow']};color:{self.c['accent']};
            border:1px solid {self.c['border_accent']};border-radius:6px;font-size:16px;}}
            QPushButton:hover{{background:{self.c['accent']};color:{self.c['bg_deep']};}}
        """)
        new_btn.clicked.connect(self._new_persona)
        sb_header_layout.addWidget(new_btn)

        refresh_btn = QPushButton("⟳")
        refresh_btn.setFixedSize(28, 28)
        refresh_btn.setCursor(Qt.PointingHandCursor)
        refresh_btn.setToolTip("Refresh personas from disk")
        refresh_btn.setStyleSheet(f"""
            QPushButton{{background:transparent;color:{self.c['text_muted']};
            border:1px solid {self.c['border']};border-radius:6px;font-size:14px;}}
            QPushButton:hover{{color:{self.c['accent']};border-color:{self.c['border_accent']};}}
        """)
        refresh_btn.clicked.connect(self._load_personas)
        sb_header_layout.addWidget(refresh_btn)
        sb_layout.addWidget(sb_header)

        # Persona list scroll area
        self.persona_scroll = QScrollArea()
        self.persona_scroll.setWidgetResizable(True)
        self.persona_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.persona_scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}")

        self.persona_list_widget = QWidget()
        self.persona_list_widget.setStyleSheet("background:transparent;")
        self.persona_list_layout = QVBoxLayout(self.persona_list_widget)
        self.persona_list_layout.setContentsMargins(0, 4, 0, 4)
        self.persona_list_layout.setSpacing(0)
        self.persona_list_layout.addStretch()

        self.persona_scroll.setWidget(self.persona_list_widget)
        sb_layout.addWidget(self.persona_scroll, 1)
        root.addWidget(sidebar)

        # ── Right panel ──
        right = QScrollArea()
        right.setWidgetResizable(True)
        right.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        right.setStyleSheet(f"QScrollArea{{background:{self.c['bg_deep']};border:none;}}")

        self.right_widget = QWidget()
        self.right_widget.setStyleSheet(f"background:{self.c['bg_deep']};")
        self.right_layout = QVBoxLayout(self.right_widget)
        self.right_layout.setContentsMargins(28, 24, 28, 24)
        self.right_layout.setSpacing(10)

        # Placeholder
        self.placeholder = QLabel("← Select a persona or create a new one")
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.placeholder.setStyleSheet(f"color:{self.c['text_dim']};font-size:13px;background:transparent;")
        self.right_layout.addWidget(self.placeholder)
        self.right_layout.addStretch()

        right.setWidget(self.right_widget)
        root.addWidget(right, 1)

        # Store right panel ref for rebuilding
        self._right_scroll = right

    def _load_personas(self):
        """Reload persona list from disk."""
        from core.personas import list_personas
        self._personas = list_personas()

        # Clear list
        while self.persona_list_layout.count() > 1:
            item = self.persona_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for p in self._personas:
            self.persona_list_layout.insertWidget(
                self.persona_list_layout.count() - 1,
                self._make_persona_card(p)
            )

        # Reselect current if still exists
        if self._current_path:
            still_exists = any(p["_file"] == self._current_path for p in self._personas)
            if not still_exists:
                self._current_path = None
                self._current_persona = None
                self._show_placeholder()

    def _make_persona_card(self, persona: dict) -> QFrame:
        path = persona["_file"]
        is_selected = path == self._current_path

        card = QFrame()
        card.setFixedHeight(64)
        card.setCursor(Qt.PointingHandCursor)
        card.setStyleSheet(f"""
            QFrame{{
                background:{'#1a1d28' if is_selected else 'transparent'};
                border:none;
                border-left: 3px solid {'#00e5ff' if is_selected else 'transparent'};
            }}
            QFrame:hover{{background:{self.c['bg_card']};}}
        """)

        layout = QHBoxLayout(card)
        layout.setContentsMargins(8, 8, 6, 8)
        layout.setSpacing(10)

        # Avatar circle
        av_lbl = QLabel()
        av_lbl.setFixedSize(40, 40)
        avatar_path = persona.get("avatar", "")
        if avatar_path:
            if not os.path.isabs(avatar_path):
                avatar_path = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    avatar_path
                )
        if avatar_path and os.path.exists(avatar_path):
            pix = make_round_pixmap(avatar_path, 40)
            av_lbl.setPixmap(pix)
        else:
            av_lbl.setText(persona.get("name", "?")[0].upper())
            av_lbl.setAlignment(Qt.AlignCenter)
            av_lbl.setStyleSheet(f"""
                background:{self.c['accent_glow']};border:1px solid {self.c['border_accent']};
                border-radius:20px;color:{self.c['accent']};font-size:16px;font-weight:bold;
            """)
        layout.addWidget(av_lbl)

        # Name + tagline
        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        name_lbl = QLabel(persona.get("name", "unnamed"))
        name_lbl.setStyleSheet(f"color:{'#00e5ff' if is_selected else self.c['text_primary']};font-size:12px;font-weight:bold;background:transparent;")
        tagline = QLabel(persona.get("tagline", ""))
        tagline.setStyleSheet(f"color:{self.c['text_dim']};font-size:10px;background:transparent;")
        tagline.setWordWrap(False)
        text_col.addWidget(name_lbl)
        text_col.addWidget(tagline)
        layout.addLayout(text_col, 1)

        # Click handler
        card.mousePressEvent = lambda e, p=persona: self._select_persona(p)
        return card

    def _show_placeholder(self):
        self._clear_right()
        self.placeholder = QLabel("← Select a persona or create a new one")
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.placeholder.setStyleSheet(f"color:{self.c['text_dim']};font-size:13px;background:transparent;")
        self.right_layout.addWidget(self.placeholder)
        self.right_layout.addStretch()

    def _clear_right(self):
        while self.right_layout.count():
            item = self.right_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _select_persona(self, persona: dict):
        self._current_path = persona["_file"]
        self._current_persona = persona
        self._load_personas()  # Refresh cards to update selection highlight
        self._build_right_panel(persona)

    def _build_right_panel(self, persona: dict):
        from core.tool_profiles import list_profiles, profile_display_name
        self._clear_right()
        c = self.c
        layout = self.right_layout
        protected = persona.get("protected", False)


        # ── Top: avatar + name + tagline + action buttons ──
        top_row = QHBoxLayout()
        top_row.setSpacing(20)

        # Large avatar
        self.rp_av_btn = QPushButton()
        self.rp_av_btn.setFixedSize(120, 120)
        self.rp_av_btn.setCursor(Qt.PointingHandCursor)
        self.rp_av_btn.setToolTip("Click to change avatar")
        avatar_path = persona.get("avatar", "")
        if avatar_path and not os.path.isabs(avatar_path):
            avatar_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                avatar_path
            )
        if avatar_path and os.path.exists(avatar_path):
            pix = make_round_pixmap(avatar_path, 120)
            self.rp_av_btn.setIcon(QIcon(pix))
            self.rp_av_btn.setIconSize(pix.size())
            self.rp_av_btn.setStyleSheet(f"""
                QPushButton{{background:transparent;border:2px solid {c['border_accent']};border-radius:60px;}}
                QPushButton:hover{{border-color:{c['accent']};}}
            """)
        else:
            self.rp_av_btn.setText(persona.get("name", "?")[0].upper())
            self.rp_av_btn.setStyleSheet(f"""
                QPushButton{{background:{c['accent_glow']};border:1px solid {c['border_accent']};
                border-radius:60px;color:{c['accent']};font-size:40px;font-weight:bold;}}
                QPushButton:hover{{background:{c['bg_card']};}}
            """)
        self.rp_av_btn.clicked.connect(self._pick_avatar)
        top_row.addWidget(self.rp_av_btn)

        # Name + tagline + buttons
        meta_col = QVBoxLayout()
        meta_col.setSpacing(6)

        self.rp_name = _le(persona.get("name", ""), c)
        self.rp_name.setStyleSheet(f"""
            QLineEdit{{background:{c['bg_input']};color:{c['accent']};
            border:1px solid {c['border']};border-radius:7px;
            padding:5px 10px;font-size:16px;font-weight:bold;}}
            QLineEdit:focus{{border:1px solid {c['border_accent']};}}
        """)
        meta_col.addWidget(self.rp_name)

        self.rp_tagline = _le(persona.get("tagline", ""), c)
        self.rp_tagline.setPlaceholderText("Short tagline...")
        meta_col.addWidget(self.rp_tagline)

        # Action buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        activate_btn = _btn("⚡ Activate", c, accent=True)
        activate_btn.clicked.connect(self._activate)
        btn_row.addWidget(activate_btn)

        duplicate_btn = _btn("⧉ Duplicate", c)
        duplicate_btn.clicked.connect(self._duplicate)
        btn_row.addWidget(duplicate_btn)

        export_btn = _btn("↑ Export", c)
        export_btn.clicked.connect(self._export)
        btn_row.addWidget(export_btn)

        if not protected:
            delete_btn = _btn("✕ Delete", c, danger=True)
            delete_btn.clicked.connect(self._delete)
            btn_row.addWidget(delete_btn)

        btn_row.addStretch()
        meta_col.addLayout(btn_row)
        meta_col.addStretch()
        top_row.addLayout(meta_col, 1)
        layout.addLayout(top_row)

        # ── System Prompt ──
        layout.addWidget(_sec("SYSTEM PROMPT", c))
        self.rp_prompt = _te(persona.get("system_prompt", ""), c, height=140)
        layout.addWidget(self.rp_prompt)

        if protected:
            self.rp_name.setReadOnly(True)
            self.rp_tagline.setReadOnly(True)
            self.rp_prompt.setReadOnly(True)
        if protected:
            locked_style = f"background:{self.c['bg_panel']};color:{self.c['text_dim']};border:1px solid {self.c['border']};border-radius:7px;padding:5px 10px;font-size:12px;"
            self.rp_name.setStyleSheet(locked_style)
            self.rp_tagline.setStyleSheet(locked_style)
            self.rp_prompt.setStyleSheet(locked_style)

        # ── Tools Profile ──
        layout.addWidget(_sec("TOOLS PROFILE", c))
        tools_row = QHBoxLayout()
        self.rp_tools_combo = _combo(c)
        profiles = list_profiles()
        self.rp_tools_combo.addItem("— none —", None)
        # Same live-count fix as ToolsTab's profile dropdown -- see
        # profile_display_name()'s docstring. "All Tools" shows the real
        # registry count instead of all_tools.json's stale snapshot.
        live_tools = self.agent.registry.all_tool_names()
        for p in profiles:
            self.rp_tools_combo.addItem(profile_display_name(p, all_tools=live_tools), p["_file"])
        # Select current
        current_tools = (persona.get("tools_profile", "") or "").strip().lower()
        matched = False
        if current_tools:
            for i in range(self.rp_tools_combo.count()):
                item_data = self.rp_tools_combo.itemData(i)
                if item_data and os.path.splitext(os.path.basename(item_data))[0].lower() == current_tools:
                    self.rp_tools_combo.setCurrentIndex(i)
                    matched = True
                    break
            if not matched:
                # fallback: display name case-insensitive startswith
                for i in range(self.rp_tools_combo.count()):
                    if self.rp_tools_combo.itemText(i).lower().startswith(current_tools.split(" (")[0]):
                        self.rp_tools_combo.setCurrentIndex(i)
                        break
        tools_row.addWidget(self.rp_tools_combo, 1)
        layout.addLayout(tools_row)

        # ── TTS ──
        layout.addWidget(_sec("TTS VOICE", c))
        tts_row = QHBoxLayout()
        tts_row.setSpacing(12)

        voice_col = QVBoxLayout()
        voice_col.addWidget(_lbl("Voice", c))
        self.rp_voice = _combo(c)
        self.rp_voice.setFixedHeight(34)
        voices = self._fetch_voices()
        self.rp_voice.addItems(voices)
        current_voice = persona.get("tts_voice", config.TTS_VOICE)
        if current_voice in voices:
            self.rp_voice.setCurrentText(current_voice)
        voice_col.addWidget(self.rp_voice)
        tts_row.addLayout(voice_col, 2)



        # Speed
        spd_col = QVBoxLayout()
        self.rp_speed_lbl = QLabel(f"Speed: {persona.get('tts_speed', 1.0):.1f}x")
        self.rp_speed_lbl.setStyleSheet(f"color:{c['text_muted']};font-size:11px;background:transparent;")
        self.rp_speed = QSlider(Qt.Horizontal)
        self.rp_speed.setRange(50, 200)
        self.rp_speed.setValue(int(persona.get("tts_speed", 1.0) * 100))
        self.rp_speed.setStyleSheet(f"""
            QSlider::groove:horizontal{{background:{c['border']};height:4px;border-radius:2px;}}
            QSlider::handle:horizontal{{background:{c['accent']};width:14px;height:14px;border-radius:7px;margin:-5px 0;}}
            QSlider::sub-page:horizontal{{background:{c['accent_dim']};height:4px;border-radius:2px;}}
        """)
        self.rp_speed.valueChanged.connect(lambda v: self.rp_speed_lbl.setText(f"Speed: {v/100:.1f}x"))
        spd_col.addWidget(self.rp_speed_lbl)
        spd_col.addWidget(self.rp_speed)
        tts_row.addLayout(spd_col, 2)

        # Pitch
        pch_col = QVBoxLayout()
        self.rp_pitch_lbl = QLabel(f"Pitch: {persona.get('tts_pitch', 1.0):.1f}x")
        self.rp_pitch_lbl.setStyleSheet(f"color:{c['text_muted']};font-size:11px;background:transparent;")
        self.rp_pitch = QSlider(Qt.Horizontal)
        self.rp_pitch.setRange(50, 200)
        self.rp_pitch.setValue(int(persona.get("tts_pitch", 1.0) * 100))
        self.rp_pitch.setStyleSheet(f"""
            QSlider::groove:horizontal{{background:{c['border']};height:4px;border-radius:2px;}}
            QSlider::handle:horizontal{{background:{c['accent']};width:14px;height:14px;border-radius:7px;margin:-5px 0;}}
            QSlider::sub-page:horizontal{{background:{c['accent_dim']};height:4px;border-radius:2px;}}
        """)
        self.rp_pitch.valueChanged.connect(lambda v: self.rp_pitch_lbl.setText(f"Pitch: {v/100:.1f}x"))
        pch_col.addWidget(self.rp_pitch_lbl)
        pch_col.addWidget(self.rp_pitch)
        tts_row.addLayout(pch_col, 2)

        # Volume
        vol_col = QVBoxLayout()
        self.rp_vol_lbl = QLabel(f"Volume: {persona.get('tts_volume', 1.0):.1f}x")
        self.rp_vol_lbl.setStyleSheet(f"color:{c['text_muted']};font-size:11px;background:transparent;")
        self.rp_vol = QSlider(Qt.Horizontal)
        self.rp_vol.setRange(50, 200)
        self.rp_vol.setValue(int(persona.get("tts_volume", 1.0) * 100))
        self.rp_vol.setStyleSheet(f"""
            QSlider::groove:horizontal{{background:{c['border']};height:4px;border-radius:2px;}}
            QSlider::handle:horizontal{{background:{c['accent']};width:14px;height:14px;border-radius:7px;margin:-5px 0;}}
            QSlider::sub-page:horizontal{{background:{c['accent_dim']};height:4px;border-radius:2px;}}
        """)
        self.rp_vol.valueChanged.connect(lambda v: self.rp_vol_lbl.setText(f"Volume: {v/100:.1f}x"))
        vol_col.addWidget(self.rp_vol_lbl)
        vol_col.addWidget(self.rp_vol)
        tts_row.addLayout(vol_col, 2)

        layout.addLayout(tts_row)

        # TTS test button
        tts_btn_row = QHBoxLayout()
        test_tts_btn = _btn("▶ Test Voice", c)
        test_tts_btn.clicked.connect(self._test_tts)
        tts_btn_row.addWidget(test_tts_btn)
        tts_btn_row.addStretch()
        layout.addLayout(tts_btn_row)

        # ── Description / Notes ──
        layout.addWidget(_sec("DESCRIPTION & NOTES", c))
        layout.addWidget(_lbl("Use case, model settings, notes — anything relevant to this persona.", c))
        self.rp_desc = _te(persona.get("description", ""), c, height=100)
        layout.addWidget(self.rp_desc)

        # ── Save button ──
        # New QPushButton + ButtonFeedback every call -- this whole panel is
        # torn down (_clear_right()'s deleteLater()) and rebuilt on every
        # persona selection, so nothing here can be created once in __init__.
        # ButtonFeedback's shiboken6.isValid() guard is what keeps a pending
        # delayed reset from an old panel safe once that old button is gone.
        save_row = QHBoxLayout()
        self.rp_save_status_lbl = _lbl("", c)
        save_row.addWidget(self.rp_save_status_lbl, 1)
        self.rp_save_btn = _btn("💾 Save Persona", c, accent=True)
        self.rp_save_btn.clicked.connect(self._save_persona)
        save_row.addWidget(self.rp_save_btn)
        layout.addLayout(save_row)
        self._persona_save_feedback = ButtonFeedback(self.rp_save_btn)
        layout.addStretch()

    def refresh_voices(self):
        if not hasattr(self, 'rp_voice'):
            return
        voices = self._fetch_voices()
        current = self.rp_voice.currentText()
        self.rp_voice.clear()
        self.rp_voice.addItems(voices)
        if current in voices:
            self.rp_voice.setCurrentText(current)
    # ── Actions ───────────────────────────────────────────────────────────────

    def _pick_avatar(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Avatar", "", "Images (*.png *.jpg *.jpeg *.webp)"
        )
        if not path or not self._current_persona:
            return
        # Make path relative to lumina root if possible
        lumina_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        try:
            rel = os.path.relpath(path, lumina_root)
            save_path = rel if not rel.startswith("..") else path
        except ValueError:
            save_path = path
        self._current_persona["avatar"] = save_path
        pix = make_round_pixmap(path, 120)
        self.rp_av_btn.setIcon(QIcon(pix))
        self.rp_av_btn.setIconSize(pix.size())
        self.rp_av_btn.setText("")
        self.rp_av_btn.setStyleSheet(f"""
            QPushButton{{background:transparent;border:2px solid {self.c['border_accent']};border-radius:60px;}}
            QPushButton:hover{{border-color:{self.c['accent']};}}
        """)

    def _collect_persona_data(self) -> dict:
        """Read all right-panel fields into a dict."""
        tools_display = self.rp_tools_combo.currentText()
        tools_profile_name = tools_display.split(" (")[0] if tools_display != "— none —" else ""
        return {
            "name": self.rp_name.text().strip(),
            "tagline": self.rp_tagline.text().strip(),
            "avatar": self._current_persona.get("avatar", ""),
            "system_prompt": self.rp_prompt.toPlainText().strip(),
            "tools_profile": tools_profile_name,
            "tts_voice": self.rp_voice.currentText(),
            "tts_speed": self.rp_speed.value() / 100.0,
            "tts_pitch": self.rp_pitch.value() / 100.0,
            "tts_volume": self.rp_vol.value() / 100.0,
            "description": self.rp_desc.toPlainText().strip(),
            "protected": self._current_persona.get("protected", False),
        }

    def _save_persona(self):
        from core.personas import save_persona
        if not self._current_path:
            return
        data = self._collect_persona_data()
        try:
            save_persona(self._current_path, data)
        except Exception as e:
            self._persona_save_feedback.failure("✗ Failed")
            self.rp_save_status_lbl.setText(f"Persona was not saved: {e}")
            return
        self._current_persona = data
        self._current_persona["_file"] = self._current_path
        self._load_personas()
        print(f"[PERSONA] Saved: {data['name']}", flush=True)
        self._persona_save_feedback.success("✓ Saved")
        self.rp_save_status_lbl.setText("")

    def _activate(self):
        if not self._current_persona:
            return
        data = self._collect_persona_data()
        self.agent.apply_persona(data)
        # Apply tool profile first
        if data.get("tools_profile"):
            from core.tool_profiles import list_profiles
            for p in list_profiles():
                if p.get("name") == data["tools_profile"]:
                    enabled_set = set(p.get("enabled", []))
                    all_tools = list(self.agent.registry._tools.keys())
                    disabled = [t for t in all_tools if t not in enabled_set]
                    self.agent.registry.set_disabled(disabled)
                    break
        self.agent.apply_persona(data)
        w = self
        while w and not hasattr(w, 'persona_applied'):
            w = w.parent()
        if w:
            w.persona_applied.emit(data["name"], data.get("avatar") or "")
        print(f"[PERSONA] Activated from settings: {data['name']}", flush=True)

    def _duplicate(self):
        from core.personas import save_persona, PERSONAS_DIR, list_personas
        if not self._current_persona:
            return
        data = self._collect_persona_data()
        data["name"] = data["name"] + " (copy)"
        data["protected"] = False
        from core.personas import fname_from_name
        fname = fname_from_name(data["name"])
        new_path = os.path.join(PERSONAS_DIR, fname)
        save_persona(new_path, data)
        self._load_personas()

    def _export(self):
        from core.personas import save_persona
        if not self._current_persona:
            return
        data = self._collect_persona_data()
        name = data.get("name", "persona")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Persona", f"{name}.json", "JSON files (*.json)"
        )
        if path:
            save_persona(path, data)
            QMessageBox.information(self, "Exported", f"Persona exported to:\n{path}")

    def _delete(self):
        if not self._current_path or not self._current_persona:
            return
        if self._current_persona.get("protected", False):
            QMessageBox.warning(self, "Protected", "This persona cannot be deleted.")
            return
        name = self._current_persona.get("name", "this persona")
        reply = QMessageBox.question(
            self, "Delete Persona",
            f"Delete '{name}'? This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            os.remove(self._current_path)
            self._current_path = None
            self._current_persona = None
            self._load_personas()
            self._show_placeholder()

    def _new_persona(self):
        from core.personas import save_persona, PERSONAS_DIR, fname_from_name
        name, ok = QInputDialog.getText(self, "New Persona", "Persona name:")
        if not ok or not name.strip():
            return
        name = name.strip()
        fname = fname_from_name(name)
        new_path = os.path.join(PERSONAS_DIR, fname)
        if os.path.exists(new_path):
            QMessageBox.warning(self, "Exists", f"A persona named '{name}' already exists.")
            return
        data = {
            "name": name,
            "tagline": "",
            "avatar": "",
            "system_prompt": "",
            "tools_profile": "",
            "tts_voice": config.VOICEBOX_PROFILE if getattr(config, 'TTS_BACKEND', 'kokoro') == 'voicebox' else config.TTS_VOICE,
            "tts_speed": 1.0,
            "tts_pitch": 1.0,
            "tts_volume": 1.0,
            "description": "",
            "protected": False,
        }
        save_persona(new_path, data)
        self._load_personas()
        # Auto-select the new persona
        for p in self._personas:
            if p["_file"] == new_path:
                self._select_persona(p)
                break

    def _test_tts(self):
        if not self.agent.tts:
            return
        tts = self.agent.tts
        voice = self.rp_voice.currentText()
        # Backend-aware voice assignment
        if hasattr(tts, 'set_profile'):
            tts.set_profile(voice)
        elif hasattr(tts, 'set_voice'):
            tts.set_voice(voice)
            tts.speed = self.rp_speed.value() / 100.0
            tts.pitch = self.rp_pitch.value() / 100.0
            tts.volume = self.rp_vol.value() / 100.0
        tts.speak("Persona voice test.", blocking=False)

    def _fetch_voices(self) -> list:
        fallback = ["af_bella", "af_sarah", "af_nicole", "af_sky",
                    "am_adam", "am_michael", "bf_emma", "bf_isabella", "bf_lily"]
        try:
            backend = getattr(config, "TTS_BACKEND", "kokoro")
            if backend in ("voicebox", "elevenlabs"):
                from tts.loader import get_tts_backend
                return get_tts_backend().list_voices() or fallback
            else:
                import urllib.request, json
                host = config.TTS_HOST.rstrip("/")
                with urllib.request.urlopen(f"{host}/v1/audio/voices", timeout=3) as r:
                    data = json.loads(r.read())
                    voices = sorted(data.get("voices", []))
                    return voices if voices else fallback
        except Exception:
            return fallback
