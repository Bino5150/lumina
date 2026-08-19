from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFileDialog, QLineEdit
from PySide6.QtGui import QIcon
from PySide6.QtCore import Qt

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import config
from core import persistence

from ._widgets import _sec, _lbl, _te, _le, _btn, make_round_pixmap, _scroll_wrap, ButtonFeedback, safe_error_detail


# ── Tab: Communications ──────────────────────────────────────────────────────
# Config for "who the world talks to" — separate from PersonasTab, which is
# "who you talk to." Anything a non-owner channel can see or say lives here,
# not mixed into the desktop persona switcher. See discord_template.json's
# channel_bound flag (core/personas.py list_personas()) for why the Discord
# identity file never shows up in the Personas tab at all.

class CommunicationsTab(QWidget):
    def __init__(self, agent, c: dict, parent=None):
        super().__init__(parent)
        self.agent = agent
        self.c = c
        self._prefs = persistence.load()
        self._build()

    def _wlbl(self, text: str) -> QLabel:
        """Word-wrapping variant of _lbl() — this tab has longer descriptive
        sentences than the rest of Settings, and _lbl() doesn't wrap by
        default, which was forcing the whole tab wider than the window
        (horizontal scroll) instead of wrapping onto multiple lines."""
        lbl = _lbl(text, self.c)
        lbl.setWordWrap(True)
        return lbl

    def _build(self):
        outer = QWidget()
        outer.setStyleSheet(f"background:{self.c['bg_deep']};")
        layout = QVBoxLayout(outer)
        layout.setContentsMargins(28, 20, 28, 20)
        layout.setSpacing(8)
        c = self.c

        # ── Public-Facing Identity (shared across every non-owner channel) ──
        layout.addWidget(_sec("PUBLIC-FACING IDENTITY", c))
        layout.addWidget(self._wlbl(
            "What Lumina knows about you on Discord (and any future public "
            "channel) — separate from your private bio, which never leaves "
            "the desktop. Empty by default; nothing shown to strangers "
            "until you write something here."
        ))
        self.public_bio = _te(
            self._prefs.get("human_bio_public", ""), c, height=90
        )
        self.public_bio.setPlaceholderText(
            "e.g. \"Built by Bino — also makes music as BINO the Great.\""
        )
        layout.addWidget(self.public_bio)
        self.public_bio.textChanged.connect(self._autosave_public_bio)

        # ── Telegram ──
        layout.addWidget(_sec("TELEGRAM", c))
        layout.addWidget(self._wlbl(
            "Owner-only channel — full toolset, no PIN gate. The chat ID "
            "check below is the entire trust boundary; anyone else who "
            "messages the bot is silently ignored."
        ))

        layout.addWidget(_lbl("Owner Chat ID", c))
        # Same prefs-first/config-fallback the bridge itself uses (see
        # comms/telegram_bridge.py's _owner_chat_id()) — otherwise this field
        # shows blank for anyone who set TELEGRAM_OWNER_CHAT_ID in config.py
        # before this tab existed, even though the bridge is reading it fine.
        _existing_chat_id = self._prefs.get("telegram_owner_chat_id") or config.TELEGRAM_OWNER_CHAT_ID or ""
        self.tg_chat_id = _le(str(_existing_chat_id), c)
        self.tg_chat_id.setPlaceholderText("Your numeric Telegram chat ID")
        layout.addWidget(self.tg_chat_id)

        layout.addWidget(_lbl("Bot Token", c))
        tg_token_row = QHBoxLayout()
        self.tg_token = _le("", c)
        self.tg_token.setEchoMode(QLineEdit.Password)
        self.tg_token.setPlaceholderText(
            "•••• configured" if get_secret_safe("telegram_bot_token") else "Not set"
        )
        tg_token_row.addWidget(self.tg_token, 1)
        self.tg_save_btn = _btn("Save", c)
        self.tg_save_btn.clicked.connect(self._save_telegram)
        tg_token_row.addWidget(self.tg_save_btn)
        layout.addLayout(tg_token_row)
        self.tg_save_status_lbl = self._wlbl("")
        layout.addWidget(self.tg_save_status_lbl)
        self._tg_save_feedback = ButtonFeedback(self.tg_save_btn)

        # ── Telegram bridge on/off ──
        tg_bridge_row = QHBoxLayout()
        self.tg_bridge_status = _lbl("Bridge: checking...", c)
        tg_bridge_row.addWidget(self.tg_bridge_status)
        tg_bridge_row.addStretch()
        self.tg_bridge_toggle = _btn("Start", c, accent=True)
        self.tg_bridge_toggle.clicked.connect(self._toggle_telegram_bridge)
        tg_bridge_row.addWidget(self.tg_bridge_toggle)
        layout.addLayout(tg_bridge_row)
        self._refresh_telegram_bridge_status()

        # ── Discord ──
        layout.addWidget(_sec("DISCORD", c))
        layout.addWidget(self._wlbl(
            "Public channel — restricted tool profile (Discord-Safe), "
            "hardcoded persona file, PIN-gated for anything sensitive. "
            "Tool access is fixed in code regardless of what's edited below."
        ))

        layout.addWidget(_lbl("Bot Token", c))
        dc_token_row = QHBoxLayout()
        self.dc_token = _le("", c)
        self.dc_token.setEchoMode(QLineEdit.Password)
        self.dc_token.setPlaceholderText(
            "•••• configured" if get_secret_safe("discord_bot_token") else "Not set"
        )
        dc_token_row.addWidget(self.dc_token, 1)
        self.dc_save_btn = _btn("Save", c)
        self.dc_save_btn.clicked.connect(self._save_discord_token)
        dc_token_row.addWidget(self.dc_save_btn)
        layout.addLayout(dc_token_row)
        self.dc_token_status_lbl = self._wlbl("")
        layout.addWidget(self.dc_token_status_lbl)
        self._dc_token_feedback = ButtonFeedback(self.dc_save_btn)

        layout.addWidget(_sec("DISCORD BOT IDENTITY", c))
        layout.addWidget(self._wlbl(
            "This is who Lumina is on Discord — name, avatar, and voice are "
            "yours to customize freely. This file is always what loads for "
            "Discord, no matter what it's renamed to. Tool access is not "
            "controlled here — it's a fixed constant in comms/discord_bridge.py, "
            "not read from this identity."
        ))

        discord_persona = self._load_discord_identity()

        id_row = QHBoxLayout()
        id_row.setSpacing(16)

        self.dc_av_btn = QPushButton()
        self.dc_av_btn.setFixedSize(72, 72)
        self.dc_av_btn.setCursor(Qt.PointingHandCursor)
        self._dc_avatar_path = discord_persona.get("avatar", "")
        self._refresh_dc_avatar_btn(discord_persona.get("name", "?"))
        self.dc_av_btn.clicked.connect(self._pick_discord_avatar)
        id_row.addWidget(self.dc_av_btn)

        id_col = QVBoxLayout()
        id_col.setSpacing(6)
        self.dc_name = _le(discord_persona.get("name", ""), c)
        id_col.addWidget(self.dc_name)
        self.dc_tagline = _le(discord_persona.get("tagline", ""), c)
        self.dc_tagline.setPlaceholderText("Short tagline...")
        id_col.addWidget(self.dc_tagline)
        id_row.addLayout(id_col, 1)
        layout.addLayout(id_row)

        layout.addWidget(_lbl("System Prompt", c))
        self.dc_prompt = _te(discord_persona.get("system_prompt", ""), c, height=140)
        layout.addWidget(self.dc_prompt)

        self.dc_identity_save_btn = _btn("Save Discord Identity", c, accent=True)
        self.dc_identity_save_btn.clicked.connect(self._save_discord_identity)
        dc_identity_save_row = QHBoxLayout()
        self.dc_identity_status_lbl = self._wlbl("")
        dc_identity_save_row.addWidget(self.dc_identity_status_lbl, 1)
        dc_identity_save_row.addWidget(self.dc_identity_save_btn)
        layout.addLayout(dc_identity_save_row)
        self._dc_identity_feedback = ButtonFeedback(self.dc_identity_save_btn)

        # ── Email (stub — Epic B not yet built) ──
        layout.addWidget(_sec("EMAIL", c))
        stub = QLabel("Coming in Epic B — dedicated Gmail default, "
                       "outbound sends routed through Telegram for approval.")
        stub.setStyleSheet(f"color:{c['text_dim']};font-size:12px;background:transparent;")
        stub.setWordWrap(True)
        layout.addWidget(stub)

        layout.addStretch()
        self.setLayout(QVBoxLayout())
        self.layout().setContentsMargins(0, 0, 0, 0)
        self.layout().addWidget(_scroll_wrap(outer, self.c))

    # ── Telegram ──
    def _save_telegram(self):
        """Preserves the original unconditional execution order (prefs save,
        then live config mutation, then optional token secret write) exactly
        -- only the truthful-feedback layer is new. persistence.save()'s
        return value used to be discarded outright; now it's read, but every
        step that used to run unconditionally still runs unconditionally."""
        chat_id = self.tg_chat_id.text().strip()
        self._prefs["telegram_owner_chat_id"] = chat_id
        prefs_ok = persistence.save(self._prefs)
        config.TELEGRAM_OWNER_CHAT_ID = chat_id or None
        token = self.tg_token.text().strip()
        secret_error = None
        if token:
            from core.secrets import set_secret
            try:
                set_secret("telegram_bot_token", token)
                self.tg_token.clear()
                self.tg_token.setPlaceholderText("•••• configured")
            except Exception as e:
                secret_error = e

        if not prefs_ok:
            self._tg_save_feedback.failure("✗ Failed")
            msg = "Chat ID was not saved to disk."
            if secret_error is not None:
                # Never str(secret_error) -- see safe_error_detail()'s
                # docstring; the exception body is untrusted here.
                msg += f" Bot token was also not stored (credential store error: {safe_error_detail(secret_error)})."
            elif token:
                msg += " Bot token was stored, but this may not survive a restart."
            self.tg_save_status_lbl.setText(msg)
            return
        if secret_error is not None:
            self._tg_save_feedback.failure("✗ Failed")
            self.tg_save_status_lbl.setText(
                f"Chat ID saved; bot token was not stored (credential store error: {safe_error_detail(secret_error)})."
            )
            return

        self._tg_save_feedback.success("✓ Saved")
        self.tg_save_status_lbl.setText("")

    def _refresh_telegram_bridge_status(self):
        try:
            from comms.telegram_bridge import is_running
            running = is_running()
        except Exception:
            running = False
        if running:
            self.tg_bridge_status.setText("Bridge: ● Running")
            self.tg_bridge_toggle.setText("Stop")
        else:
            self.tg_bridge_status.setText("Bridge: ○ Stopped")
            self.tg_bridge_toggle.setText("Start")

    def _toggle_telegram_bridge(self):
        try:
            from comms.telegram_bridge import start_bridge, stop_bridge, is_running
        except Exception as e:
            self.tg_bridge_status.setText(f"Bridge: import error — {e}")
            return
        if is_running():
            success, msg = stop_bridge()
        else:
            success, msg = start_bridge()
        self.tg_bridge_status.setText(f"Bridge: {msg}")
        self._refresh_telegram_bridge_status()

    # ── Discord: token ──
    def _save_discord_token(self):
        token = self.dc_token.text().strip()
        if not token:
            self._dc_token_feedback.success("No token entered")
            self.dc_token_status_lbl.setText("")
            return
        from core.secrets import set_secret
        try:
            set_secret("discord_bot_token", token)
        except Exception as e:
            self._dc_token_feedback.failure("✗ Failed")
            # Never str(e) -- see safe_error_detail()'s docstring.
            self.dc_token_status_lbl.setText(
                f"Bot token was not stored (credential store error: {safe_error_detail(e)})."
            )
            return
        self.dc_token.clear()
        self.dc_token.setPlaceholderText("•••• configured")
        self._dc_token_feedback.success("✓ Saved")
        self.dc_token_status_lbl.setText("")

    # ── Discord: identity file ──
    def _load_discord_identity(self) -> dict:
        from core.personas import load_persona, DISCORD_TEMPLATE_PATH
        try:
            return load_persona(DISCORD_TEMPLATE_PATH)
        except Exception:
            return {}

    def _refresh_dc_avatar_btn(self, fallback_letter: str):
        c = self.c
        path = self._dc_avatar_path
        if path and not os.path.isabs(path):
            path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), path
            )
        if path and os.path.exists(path):
            pix = make_round_pixmap(path, 72)
            self.dc_av_btn.setIcon(QIcon(pix))
            self.dc_av_btn.setIconSize(pix.size())
            self.dc_av_btn.setText("")
            self.dc_av_btn.setStyleSheet(f"""
                QPushButton{{background:transparent;border:2px solid {c['border_accent']};border-radius:36px;}}
                QPushButton:hover{{border-color:{c['accent']};}}
            """)
        else:
            self.dc_av_btn.setText((fallback_letter or "?")[0].upper())
            self.dc_av_btn.setStyleSheet(f"""
                QPushButton{{background:{c['accent_glow']};border:1px solid {c['border_accent']};
                border-radius:36px;color:{c['accent']};font-size:24px;font-weight:bold;}}
                QPushButton:hover{{background:{c['bg_card']};}}
            """)

    def _pick_discord_avatar(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Discord Bot Avatar", "", "Images (*.png *.jpg *.jpeg *.webp)"
        )
        if path:
            self._dc_avatar_path = path
            self._refresh_dc_avatar_btn(self.dc_name.text())

    def _save_discord_identity(self):
        """Writes ONLY identity fields (name/tagline/avatar/system_prompt) to
        discord_template.json. Deliberately does not touch tools_profile,
        channel_bound, or protected — those are set once and are not meant
        to be editable from this form, and tools_profile in the file is
        inert anyway (comms/discord_bridge.py never reads it — see that
        file's module docstring)."""
        from core.personas import load_persona, save_persona, DISCORD_TEMPLATE_PATH
        try:
            data = load_persona(DISCORD_TEMPLATE_PATH)
        except Exception:
            data = {"tools_profile": "Discord-Safe", "protected": True, "channel_bound": True}

        data["name"] = self.dc_name.text().strip() or data.get("name", "Lumina")
        data["tagline"] = self.dc_tagline.text().strip()
        data["avatar"] = self._dc_avatar_path
        data["system_prompt"] = self.dc_prompt.toPlainText().strip()
        # Preserve everything else in the file untouched — channel_bound,
        # protected, tts_*, description, and the (inert) tools_profile field.
        try:
            save_persona(DISCORD_TEMPLATE_PATH, data)
        except Exception as e:
            self._dc_identity_feedback.failure("✗ Failed")
            self.dc_identity_status_lbl.setText(f"Discord identity was not saved: {e}")
            return
        self._dc_identity_feedback.success("✓ Saved")
        self.dc_identity_status_lbl.setText("")

    # ── Public bio autosave ──
    def _autosave_public_bio(self):
        self._prefs["human_bio_public"] = self.public_bio.toPlainText().strip()
        persistence.save(self._prefs)


def get_secret_safe(key: str):
    """Local import wrapper so this file doesn't need a hard top-level
    dependency on core.secrets just to check "is something configured"."""
    try:
        from core.secrets import get_secret
        return get_secret(key)
    except Exception:
        return None
