from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QCheckBox, QFileDialog
from PySide6.QtGui import QIcon
from PySide6.QtCore import Qt

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import config
from core import persistence

from ._widgets import _sec, _lbl, _te, _le, _btn, make_round_pixmap, _scroll_wrap, ButtonFeedback


# ── Tab: User Profile ──────────────────────────────────────────────────────────

class UserProfileTab(QWidget):
    def __init__(self, agent, c: dict, parent=None):
        super().__init__(parent)
        self.agent = agent
        self.c = c
        self._prefs = persistence.load()
        self._build()

    def _build(self):
        outer = QWidget()
        outer.setStyleSheet(f"background:{self.c['bg_deep']};")
        layout = QVBoxLayout(outer)
        layout.setContentsMargins(28, 20, 28, 20)
        layout.setSpacing(8)

        # ── Your Identity ──
        layout.addWidget(_sec("YOUR IDENTITY", self.c))
        layout.addWidget(_lbl("Your Name", self.c))
        self.user_name = _le(config.USER_NAME, self.c)
        layout.addWidget(self.user_name)

        # ── Bio ──
        layout.addWidget(_sec(f"MY HUMAN ({config.USER_NAME}'s profile)", self.c))
        layout.addWidget(_lbl("Tell Lumina about yourself — this injects into every session.", self.c))
        self.human_bio = _te(
            self._prefs.get("human_bio", f"Name: {config.USER_NAME}\n"),
            self.c, height=180
        )
        layout.addWidget(self.human_bio)
        self.human_bio.textChanged.connect(self._autosave_bio)

        self.human_profile_curation_cb = QCheckBox("Auto-curate this profile during dream sweeps")
        self.human_profile_curation_cb.setChecked(config.HUMAN_PROFILE_CURATION_ENABLED)
        self.human_profile_curation_cb.toggled.connect(self._on_curation_toggled)
        layout.addWidget(self.human_profile_curation_cb)

        layout.addWidget(_lbl("Lumina's notes about you -- refined automatically over time. Edit freely if it drifts.", self.c))
        self.human_profile_curated = _te(
            self._prefs.get("human_profile_curated", ""),
            self.c, height=120
        )
        layout.addWidget(self.human_profile_curated)
        self.human_profile_curated.textChanged.connect(self._autosave_curated_profile)

        # ── Your Avatar ──
        layout.addWidget(_sec("YOUR AVATAR", self.c))
        av_row = QHBoxLayout()
        av_row.setSpacing(24)

        self.usr_av = self._av_btn("user_avatar_path", config.USER_NAME)
        av_row.addWidget(self.usr_av, alignment=Qt.AlignLeft)

        av_col = QVBoxLayout()
        usr_pick = _btn("Browse...", self.c)
        usr_pick.clicked.connect(
            lambda: self._pick_av("user_avatar_path", self.usr_av, config.USER_NAME)
        )
        av_col.addWidget(usr_pick)
        av_col.addStretch()
        av_row.addLayout(av_col)
        av_row.addStretch()
        layout.addLayout(av_row)

        self.save_btn = _btn("Save Profile", self.c, accent=True)
        self.save_btn.clicked.connect(self._save)
        save_row = QHBoxLayout()
        self.save_status_lbl = _lbl("", self.c)
        save_row.addWidget(self.save_status_lbl, 1)
        save_row.addWidget(self.save_btn)
        layout.addLayout(save_row)
        self._save_feedback = ButtonFeedback(self.save_btn)
        layout.addStretch()

        self.setLayout(QVBoxLayout())
        self.layout().setContentsMargins(0, 0, 0, 0)
        self.layout().addWidget(_scroll_wrap(outer, self.c))

    def _av_btn(self, key: str, name: str) -> QPushButton:
        btn = QPushButton()
        btn.setFixedSize(100, 100)
        path = self._prefs.get(key)
        if path and os.path.exists(path):
            pix = make_round_pixmap(path, 100)
            btn.setIcon(QIcon(pix))
            btn.setIconSize(pix.size())
            btn.setStyleSheet(f"""
                QPushButton{{background:transparent;border:2px solid {self.c['border_accent']};
                border-radius:50px;}}
                QPushButton:hover{{border-color:{self.c['accent']};}}
            """)
        else:
            btn.setText(name[0].upper())
            btn.setStyleSheet(f"""
                QPushButton{{background:{self.c['accent_glow']};border:1px solid {self.c['border_accent']};
                border-radius:50px;color:{self.c['accent']};font-size:28px;font-weight:bold;}}
                QPushButton:hover{{background:{self.c['bg_card']};}}
            """)
        return btn

    def _pick_av(self, key: str, btn: QPushButton, name: str):
        path, _ = QFileDialog.getOpenFileName(
            self, f"Select {name} Avatar", "", "Images (*.png *.jpg *.jpeg *.webp)"
        )
        if path:
            self._prefs[key] = path  # read-cache only — PREFS-STALE-WRITE-01
            persistence.update({key: path})
            pix = make_round_pixmap(path, 100)
            btn.setIcon(QIcon(pix))
            btn.setIconSize(pix.size())
            btn.setText("")
            btn.setStyleSheet(f"""
                QPushButton{{background:transparent;border:2px solid {self.c['border_accent']};
                border-radius:50px;}}
                QPushButton:hover{{border-color:{self.c['accent']};}}
            """)

    def _save(self):
        # USER_NAME is mutated live before persistence.save() below is even
        # attempted -- a failure here does not roll that back, so the
        # failure message must not imply nothing changed.
        config.USER_NAME = self.user_name.text().strip() or config.USER_NAME
        updates = {
            "user_name": config.USER_NAME,
            "human_bio": self.human_bio.toPlainText().strip(),
        }
        # PREFS-STALE-WRITE-01: publish only the keys this tab owns via a
        # fresh-load transaction; never the startup-era whole snapshot.
        self._prefs.update(updates)  # read-cache only
        if persistence.update(updates):
            self._save_feedback.success("✓ Saved")
            self.save_status_lbl.setText("")
        else:
            self._save_feedback.failure("✗ Failed")
            self.save_status_lbl.setText(
                "Profile was not saved to disk; your name change may "
                "already be live and will not survive a restart."
            )

    def _autosave_bio(self):
        self._prefs["human_bio"] = self.human_bio.toPlainText().strip()  # read-cache only
        persistence.update({"human_bio": self._prefs["human_bio"]})

    def _autosave_curated_profile(self):
        self._prefs["human_profile_curated"] = self.human_profile_curated.toPlainText().strip()  # read-cache only
        persistence.update({"human_profile_curated": self._prefs["human_profile_curated"]})

    def _on_curation_toggled(self, checked: bool):
        """HUMAN_PROFILE_CURATION_ENABLED -- same live-apply pattern as
        DREAM_SWEEP_ENABLED/_on_subagents_toggled: dreaming.py re-reads this
        config value via getattr() on every sweep, so no tool re-registration
        is needed here -- just the config attr + prefs round-trip."""
        config.HUMAN_PROFILE_CURATION_ENABLED = checked
        prefs = persistence.load()
        prefs["human_profile_curation_enabled"] = checked
        persistence.save(prefs)
