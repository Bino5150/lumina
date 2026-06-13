"""
Lumina Main Window
- Wider sidebar (160px) with full text labels and larger avatar
- Think block batching — flushes every 80ms instead of per-character
- Full chat persistence — loads previous chats on startup
- Avatar persistence — remembers path between sessions
"""

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QLabel,
    QPushButton, QFrame, QComboBox, QSizePolicy, QInputDialog,
    QMessageBox, QFileDialog, QScrollArea
)
from PySide6.QtCore import Qt, QThread, Signal, QTimer, QObject
from PySide6.QtGui import QFont, QPixmap, QPainter, QBrush, QColor, QPainterPath, QIcon

import os
import sys
import base64
import re 
import requests
import threading 
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from core.personas import list_personas, load_persona
from core import persistence
from ui.chat_widget import ChatWidget, LiveResponseBubble
from ui.settings import SettingsPanel
from tools.memory import (
    init_chat_db, create_chat, list_chats, save_chat_message,
    load_chat_messages, rename_chat, delete_chat, get_chat_name
)
from tools.browser import browser_manager

COLORS = {
    "bg_deep":      "#0a0b0f",
    "bg_panel":     "#0f1117",
    "bg_sidebar":   "#0c0d12",
    "bg_card":      "#13151e",
    "bg_input":     "#1a1d28",
    "accent":       "#00e5ff",
    "accent_dim":   "#0099b3",
    "accent_glow":  "#00e5ff33",
    "text_primary": "#e8eaf0",
    "text_muted":   "#6b7280",
    "text_dim":     "#3d4355",
    "border":       "#1e2133",
    "border_accent":"#00e5ff44",
    "user_bubble":  "#1a2035",
    "ai_bubble":    "#111420",
    "tool_bg":      "#0d1520",
    "tool_text":    "#00b4cc",
    "think_bg":     "#0a1020",
    "think_text":   "#4a7a9b",
    "danger":       "#ff4757",
    "success":      "#2ed573",
    "warning":      "#ffa502",
}

APP_STYLESHEET = f"""
QMainWindow, QWidget {{
    background-color: {COLORS['bg_deep']};
    color: {COLORS['text_primary']};
    font-family: 'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace;
    font-size: 13px;
}}
QScrollBar:vertical {{
    background: {COLORS['bg_panel']}; width: 6px; border-radius: 3px;
}}
QScrollBar::handle:vertical {{
    background: {COLORS['text_dim']}; border-radius: 3px; min-height: 20px;
}}
QScrollBar::handle:vertical:hover {{ background: {COLORS['accent_dim']}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ height: 0; }}
QComboBox {{
    background: {COLORS['bg_input']}; color: {COLORS['text_primary']};
    border: 1px solid {COLORS['border']}; border-radius: 6px;
    padding: 4px 8px; font-size: 11px;
}}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{
    background: {COLORS['bg_card']}; color: {COLORS['text_primary']};
    border: 1px solid {COLORS['border_accent']};
    selection-background-color: {COLORS['accent_glow']};
}}
QToolTip {{
    background: {COLORS['bg_card']}; color: {COLORS['text_primary']};
    border: 1px solid {COLORS['border_accent']}; padding: 4px 8px;
}}
"""


def make_round_pixmap(path: str, size: int) -> QPixmap:
    src = QPixmap(path).scaled(size, size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    out = QPixmap(size, size)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    clip = QPainterPath()
    clip.addEllipse(0, 0, size, size)
    p.setClipPath(clip)
    p.drawPixmap(0, 0, src)
    p.end()
    return out


# ── Streaming signals ──────────────────────────────────────────────────────────

class StreamSignals(QObject):
    tool_call      = Signal(str, dict)
    tool_result    = Signal(str, str)
    think_start    = Signal(int)
    think_chunk    = Signal(str)   # batched, not per-character
    think_end      = Signal()
    response_chunk = Signal(str)   # batched response text
    finished       = Signal(str)
    error          = Signal(str)
    
class STTSignals(QObject):
    transcribed = Signal(str)
    error       = Signal(str)


class AgentWorker(QThread):
    """
    Runs agent.chat() in a background thread.
    Batches think and response tokens — flushes every BATCH_CHARS characters
    to avoid overwhelming Qt's signal queue.
    """
    BATCH_CHARS = 12  # flush every N characters

    def __init__(self, agent, user_input, signals: StreamSignals):
        super().__init__()
        self.agent = agent
        self.user_input = user_input
        self.signals = signals
        self._think_buf = ""
        self._resp_buf = ""

    def _flush_think(self):
        if self._think_buf:
            self.signals.think_chunk.emit(self._think_buf)
            self._think_buf = ""

    def _flush_resp(self):
        if self._resp_buf:
            self.signals.response_chunk.emit(self._resp_buf)
            self._resp_buf = ""

    def run(self):
        def on_think_start(step):
            self._flush_resp()
            self.signals.think_start.emit(step)

        def on_think_token(t):
            self._think_buf += t
            if len(self._think_buf) >= self.BATCH_CHARS:
                self._flush_think()

        def on_think_end():
            self._flush_think()
            self.signals.think_end.emit()

        def on_response_token(t):
            self._resp_buf += t
            if len(self._resp_buf) >= self.BATCH_CHARS:
                self._flush_resp()

        def on_tool_call(n, a):
            self._flush_think()
            self._flush_resp()
            self.signals.tool_call.emit(n, a)

        def on_tool_result(n, r):
            self.signals.tool_result.emit(n, r)

        self.agent.on_tool_call      = on_tool_call
        self.agent.on_tool_result    = on_tool_result
        self.agent.on_think_start    = on_think_start
        self.agent.on_think_token    = on_think_token
        self.agent.on_think_end      = on_think_end
        self.agent.on_response_token = on_response_token

        try:
            result = self.agent.chat(self.user_input)
            self._flush_think()
            self._flush_resp()
            self.signals.finished.emit(result)
        except Exception as e:
            self.signals.error.emit(str(e))


# ── Sidebar helpers ────────────────────────────────────────────────────────────

def _nav_btn(icon: str, label: str) -> QPushButton:
    btn = QPushButton(f"  {icon}  {label}")
    btn.setCursor(Qt.PointingHandCursor)
    btn.setFixedHeight(34)
    btn.setCheckable(True)
    btn.setStyleSheet(f"""
        QPushButton {{
            background: transparent; border: none; border-radius: 8px;
            color: {COLORS['text_muted']}; font-size: 12px;
            text-align: left; padding: 0 10px;
        }}
        QPushButton:hover {{ background: {COLORS['bg_card']}; color: {COLORS['accent']}; }}
        QPushButton:checked {{
            background: {COLORS['accent_glow']}; color: {COLORS['accent']};
            border: 1px solid {COLORS['border_accent']};
        }}
    """)
    return btn


def _action_btn(icon: str, label: str, danger: bool = False) -> QPushButton:
    hover_color = COLORS['danger'] if danger else COLORS['accent']
    btn = QPushButton(f"  {icon}  {label}")
    btn.setCursor(Qt.PointingHandCursor)
    btn.setFixedHeight(30)
    btn.setStyleSheet(f"""
        QPushButton {{
            background: transparent; border: none; border-radius: 6px;
            color: {COLORS['text_muted']}; font-size: 11px;
            text-align: left; padding: 0 8px;
        }}
        QPushButton:hover {{ background: {COLORS['bg_card']}; color: {hover_color}; }}
    """)
    return btn


class StatusBar(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(28)
        self.setStyleSheet(f"QFrame{{background:{COLORS['bg_panel']};border-top:1px solid {COLORS['border']};}}")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12,0,12,0)
        layout.setSpacing(16)
        self.dot = QLabel("●")
        self.dot.setStyleSheet(f"color:{COLORS['success']};font-size:10px;background:transparent;")
        self.model_lbl = QLabel("Connecting...")
        self.model_lbl.setStyleSheet(f"color:{COLORS['text_muted']};font-size:11px;background:transparent;")
        self.token_lbl = QLabel("")
        self.token_lbl.setStyleSheet(f"color:{COLORS['text_dim']};font-size:11px;background:transparent;")
        layout.addWidget(self.dot)
        layout.addWidget(self.model_lbl)
        layout.addStretch()
        layout.addWidget(self.token_lbl)

    def set_connected(self, model: str):
        self.dot.setStyleSheet(f"color:{COLORS['success']};font-size:10px;background:transparent;")
        self.model_lbl.setText(model[:60]+"..." if len(model)>60 else model)

    def set_error(self, msg: str = "LM Studio offline"):
        self.dot.setStyleSheet(f"color:{COLORS['danger']};font-size:10px;background:transparent;")
        self.model_lbl.setText(msg)

    def set_tokens(self, count: int):
        self.token_lbl.setText(f"~{count:,} ctx tokens")


# ── Main Window ────────────────────────────────────────────────────────────────

class LuminaWindow(QMainWindow):
    def __init__(self, agent, stt=None):
        super().__init__()
        self.agent = agent
        self.stt = stt
        self._stt_signals = STTSignals()
        self._stt_signals.transcribed.connect(self._on_stt_done)
        self._stt_signals.error.connect(self._on_stt_error)
        self.worker = None
        self.signals = StreamSignals()
        self._live_bubble = None
        self._pending_image = None   # (path, b64_data, media_type) when image is queued
        self._pending_audio = None
        self._current_chat_id = None
        self._prefs = persistence.load()

        try:
            init_chat_db()
        except Exception as e:
            print(f"[DB] Failed to initialize chat database: {e}", flush=True)
            QMessageBox.critical(None, "Database Error",
                f"Lumina couldn't initialize the chat database.\n\n{e}\n\nCheck that your ~/lumina/memory/ directory is accessible.")
        
        
        self._setup_window()
        self._build_ui()
        self._connect_signals()
        self._restore_session()
        QTimer.singleShot(600, self._check_connection)

    def _setup_window(self):
        self.setWindowTitle("Lumina")
        self.setMinimumSize(960, 660)
        w = self._prefs.get("window_width", 1150)
        h = self._prefs.get("window_height", 760)
        self.resize(w, h)
        self.setStyleSheet(APP_STYLESHEET)

    def closeEvent(self, event):
    # Load fresh prefs so we don't overwrite settings changes
        prefs = persistence.load()
        prefs["window_width"] = self.width()
        prefs["window_height"] = self.height()
        persistence.save(prefs)
        browser_manager.close()
        super().closeEvent(event)

    def _restore_session(self):
        """Load avatar and most recent chat on startup."""
        # Restore avatar
        avatar_path = self._prefs.get("avatar_path")
        if avatar_path and os.path.exists(avatar_path):
            self._apply_avatar(avatar_path)
            
        user_avatar_path = self._prefs.get("user_avatar_path")
        if user_avatar_path and os.path.exists(user_avatar_path):
            self._apply_user_avatar(user_avatar_path)
            
        # ── Restore last persona ──
        last_persona = self._prefs.get("last_persona")
        if last_persona and os.path.exists(last_persona):
            self._load_persona_from_file(last_persona)
            # Sync the combo box to show the right selection
            for i in range(self.persona_combo.count()):
                if self.persona_combo.itemData(i) == last_persona:
                    self.persona_combo.blockSignals(True)
                    self.persona_combo.setCurrentIndex(i)
                    self.persona_combo.blockSignals(False)
                    break    

        # Load chat list
        chats = list_chats()
        if chats:
            self._refresh_chat_list()
            # Try to restore last chat
            last_id = self._prefs.get("last_chat_id")
            target_id = last_id if last_id else chats[0]["id"]
            self._load_chat(target_id)
        else:
            self._new_chat()
            
    def _load_persona_from_file(self, path: str):
        """Load and apply a persona JSON to the agent and UI."""
        try:
            persona = load_persona(path)
        except Exception as e:
            print(f"[PERSONA] Failed to load {path}: {e}", flush=True)
            return
        self.agent.apply_persona(persona)
        name = persona.get("name", config.AGENT_NAME)
        avatar = self.agent.persona_avatar or self._prefs.get("avatar_path")
        self.chat_widget.set_persona(name, avatar)
        self.header_title.setText(name)
        self.name_lbl.setText(name)
        self.name_lbl.setStyleSheet(
            f"color:{COLORS['accent']};font-size:12px;font-weight:bold;"
            f"letter-spacing:1px;background:transparent;"
        )
        if avatar and os.path.exists(avatar):
            self._apply_avatar(avatar)

    def _on_persona_applied(self, name: str, avatar_path: str):
        """Signal handler — SettingsPanel applied a persona."""
        resolved = avatar_path or self._prefs.get("avatar_path")
        self.chat_widget.set_persona(name, resolved)
        self.header_title.setText(name)
        self.name_lbl.setText(name)
        self.name_lbl.setStyleSheet(
            f"color:{COLORS['accent']};font-size:12px;font-weight:bold;"
            f"letter-spacing:1px;background:transparent;"
        )
        if resolved and os.path.exists(resolved):
            self._apply_avatar(resolved)
        print(f"[PERSONA] UI updated via settings: {name}", flush=True)      

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0,0,0,0)
        root.setSpacing(0)

        root.addWidget(self._build_sidebar())

        div = QFrame()
        div.setFixedWidth(1)
        div.setStyleSheet(f"background:{COLORS['border']};")
        root.addWidget(div)

        main = QVBoxLayout()
        main.setContentsMargins(0,0,0,0)
        main.setSpacing(0)
        main.addWidget(self._build_header())

        self.chat_widget = ChatWidget(
            COLORS,
            avatar_path=self._prefs.get("avatar_path"),
            user_avatar_path=self._prefs.get("user_avatar_path"),
            tts=self.agent.tts
        )
        self.settings_panel = SettingsPanel(self.agent, COLORS)
        self.settings_panel.setVisible(False)
        self.settings_panel.persona_applied.connect(self._on_persona_applied)

        main.addWidget(self.chat_widget, 1)
        main.addWidget(self.settings_panel, 1)

        self.status_bar = StatusBar()
        main.addWidget(self.status_bar)

        container = QWidget()
        container.setLayout(main)
        root.addWidget(container, 1)

        self.chat_widget.message_submitted.connect(self._on_user_message)
        self.chat_widget.files_dropped.connect(self._on_files_dropped)
        self.chat_widget.audio_preview_cancelled.connect(lambda: setattr(self, '_pending_audio', None))
        self.chat_widget.set_persona(
            config.AGENT_NAME,
            self._prefs.get("avatar_path")
        )

    def _build_sidebar(self):
        sidebar = QFrame()
        sidebar.setFixedWidth(160)
        sidebar.setStyleSheet(f"background:{COLORS['bg_sidebar']};border:none;")

        # Scrollable interior so it never clips
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        inner = QWidget()
        inner.setStyleSheet("background:transparent;")
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(10,14,10,12)
        layout.setSpacing(6)

        # ── Avatar ──
        self.avatar_btn = QPushButton()
        self.avatar_btn.setFixedSize(72, 72)
        self.avatar_btn.setCursor(Qt.PointingHandCursor)
        self.avatar_btn.setToolTip("Click to set Lumina avatar")
        self._set_avatar_placeholder()
        self.avatar_btn.clicked.connect(self._pick_avatar)
        layout.addWidget(self.avatar_btn, alignment=Qt.AlignHCenter)

        self.name_lbl = QLabel(config.AGENT_NAME)
        self.name_lbl.setAlignment(Qt.AlignCenter)
        self.name_lbl.setStyleSheet(f"color:{COLORS['accent']};font-size:12px;font-weight:bold;letter-spacing:1px;background:transparent;")
        layout.addWidget(self.name_lbl)

        layout.addWidget(self._sep())

        # ── Persona selector ──
        self._section_lbl(layout, "PERSONA")
        self.persona_combo = QComboBox()
        self.persona_combo.setFixedHeight(30)
        self.persona_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.persona_combo.addItem("— select —", None)
        personas = list_personas()
        if personas:
            for p in personas:
                self.persona_combo.addItem(p.get("name", "unnamed"), p["_file"])
        else:
            self.persona_combo.addItem("No personas found", None)
            print("[PERSONA] No personas found in personas/ directory", flush=True)
            self.persona_combo.currentIndexChanged.connect(self._on_persona_selected)
        layout.addWidget(self.persona_combo)

        layout.addWidget(self._sep())

        # ── Chat list label ──
        self._section_lbl(layout, "CHATS")

        # ── Chat selector ──
        self.chat_combo = QComboBox()
        self.chat_combo.setFixedHeight(30)
        self.chat_combo.setMaximumWidth(140)
        self.chat_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.chat_combo.currentIndexChanged.connect(self._on_chat_selected)
        layout.addWidget(self.chat_combo)

        # ── Chat actions ──
        self.btn_new    = _action_btn("＋", "New Chat")
        self.btn_clear  = _action_btn("⟳", "Clear Chat")
        self.btn_rename = _action_btn("✎", "Rename")
        self.btn_delete = _action_btn("✕", "Delete", danger=True)

        self.btn_new.clicked.connect(self._new_chat)
        self.btn_clear.clicked.connect(self._clear_chat)
        self.btn_rename.clicked.connect(self._rename_chat)
        self.btn_delete.clicked.connect(self._delete_chat)

        for btn in [self.btn_new, self.btn_clear, self.btn_rename, self.btn_delete]:
            layout.addWidget(btn)

        layout.addWidget(self._sep())
        self._section_lbl(layout, "PANELS")

        self.btn_chat_nav = _nav_btn("💬", "Chat")
        self.btn_chat_nav.setChecked(True)
        self.btn_settings_nav = _nav_btn("⚙", "Settings")

        self.btn_chat_nav.clicked.connect(lambda: self._show_panel("chat"))
        self.btn_settings_nav.clicked.connect(lambda: self._show_panel("settings"))

        layout.addWidget(self.btn_chat_nav)
        layout.addWidget(self.btn_settings_nav)
        layout.addStretch()
        
        credit1 = QLabel("LuminaAI by: BINO the Great")
        credit1.setAlignment(Qt.AlignCenter)
        credit1.setStyleSheet(f"color:{COLORS['text_dim']};font-size:10px;background:transparent;")
        layout.addWidget(credit1)

        credit2 = QLabel("Mo Thugs South 2026")
        credit2.setAlignment(Qt.AlignCenter)
        credit2.setStyleSheet(f"color:{COLORS['text_dim']};font-size:10px;background:transparent;")
        layout.addWidget(credit2)

        ver = QLabel("v0.1.9-beta.1")
        ver.setAlignment(Qt.AlignCenter)
        ver.setStyleSheet(f"color:{COLORS['text_dim']};font-size:13px;background:transparent;")
        layout.addWidget(ver)

        scroll.setWidget(inner)
        outer = QVBoxLayout(sidebar)
        outer.setContentsMargins(0,0,0,0)
        outer.addWidget(scroll)
        return sidebar

    def _sep(self) -> QFrame:
        f = QFrame()
        f.setFixedHeight(1)
        f.setStyleSheet(f"background:{COLORS['border']};margin:2px 0;")
        return f

    def _section_lbl(self, layout, text: str):
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color:{COLORS['text_dim']};font-size:9px;letter-spacing:2px;background:transparent;padding:2px 0 0 2px;")
        layout.addWidget(lbl)

    def _set_avatar_placeholder(self):
        self.avatar_btn.setText("✦")
        self.avatar_btn.setStyleSheet(f"""
            QPushButton{{
                background:{COLORS['accent_glow']};border:1px solid {COLORS['border_accent']};
                border-radius:36px;color:{COLORS['accent']};font-size:26px;
            }}
            QPushButton:hover{{background:{COLORS['bg_card']};}}
        """)

    def _apply_avatar(self, path: str):
        pix = make_round_pixmap(path, 72)
        self.avatar_btn.setIcon(QIcon(pix))
        self.avatar_btn.setIconSize(pix.size())
        self.avatar_btn.setText("")
        self.avatar_btn.setStyleSheet(f"""
            QPushButton{{background:transparent;border:2px solid {COLORS['border_accent']};border-radius:36px;}}
            QPushButton:hover{{border-color:{COLORS['accent']};}}
        """)

    def _apply_user_avatar(self, path: str):
        self._prefs["user_avatar_path"] = path
        persistence.save(self._prefs)
        self.chat_widget.user_avatar_path = path
    def _pick_avatar(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select Lumina Avatar", "", "Images (*.png *.jpg *.jpeg *.webp)")
        if path:
            self._prefs["avatar_path"] = path
            persistence.save(self._prefs)
            self._apply_avatar(path)

    def _build_header(self):
        header = QFrame()
        header.setFixedHeight(50)
        header.setStyleSheet(f"QFrame{{background:{COLORS['bg_panel']};border-bottom:1px solid {COLORS['border']};}}")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(20,0,16,0)
        self.header_title = QLabel(config.AGENT_NAME)
        self.header_title.setStyleSheet(f"color:{COLORS['text_primary']};font-size:15px;font-weight:bold;letter-spacing:2px;background:transparent;")
        self.status_lbl = QLabel("")
        self.status_lbl.setStyleSheet(f"color:{COLORS['accent_dim']};font-size:12px;background:transparent;")
        layout.addWidget(self.header_title)
        layout.addStretch()
        layout.addWidget(self.status_lbl)
        return header

    def _connect_signals(self):
        self.signals.tool_call.connect(self._on_tool_call)
        self.signals.tool_result.connect(self._on_tool_result)
        self.signals.think_start.connect(self._on_think_start)
        self.signals.think_chunk.connect(self._on_think_chunk)
        self.signals.think_end.connect(self._on_think_end)
        self.signals.response_chunk.connect(self._on_response_chunk)
        self.signals.finished.connect(self._on_finished)
        self.signals.error.connect(self._on_error)
        self.chat_widget.mic_pressed.connect(self._on_mic_pressed)

    # ── Chat management ────────────────────────────────────────────────────────

    def _refresh_chat_list(self):
        self.chat_combo.blockSignals(True)
        self.chat_combo.clear()
        for chat in list_chats():
            self.chat_combo.addItem(chat["name"], chat["id"])
        for i in range(self.chat_combo.count()):
            if self.chat_combo.itemData(i) == self._current_chat_id:
                self.chat_combo.setCurrentIndex(i)
                break
        self.chat_combo.blockSignals(False)

    def _new_chat(self):
        self._current_chat_id = create_chat()
        self._prefs["last_chat_id"] = self._current_chat_id
        persistence.save(self._prefs)
        self.agent.ctx.clear()
        # Re-apply active persona so new chat inherits identity
        path = self.persona_combo.currentData()
        if path:
            self._load_persona_from_file(path)
        self.chat_widget.clear_messages()
        self.chat_widget.add_system_message(f"New chat — {config.AGENT_NAME} is ready.")
        self._refresh_chat_list()

    def _clear_chat(self):
        self.agent.ctx.clear()
        self.chat_widget.clear_messages()
        self.chat_widget.add_system_message("Chat cleared.")

    def _load_chat(self, chat_id: int):
        self._current_chat_id = chat_id
        self._prefs["last_chat_id"] = chat_id
        persistence.save(self._prefs)
        self.agent.ctx.clear()
        self.chat_widget.clear_messages()
        msgs = load_chat_messages(chat_id)
        for m in msgs:
            content = m.get("content") or ""
            if not content:
                continue
            if m["role"] == "user":
                self.chat_widget.add_user_message(content)
                self.agent.ctx.add_user(content)
            elif m["role"] == "assistant":
                bubble = self.chat_widget.create_live_bubble()
                bubble._response_text = content
                bubble.finalize()
                self.agent.ctx.add_assistant(content)
        self._refresh_chat_list()

    def _on_chat_selected(self, idx: int):
        if idx < 0:
            return
        chat_id = self.chat_combo.itemData(idx)
        if chat_id and chat_id != self._current_chat_id:
            self._load_chat(chat_id)
            
    def _on_persona_selected(self, idx: int):
        path = self.persona_combo.itemData(idx)
        if path:
            self._prefs["last_persona"] = path
            persistence.save(self._prefs)
            self._load_persona_from_file(path)        

    def _rename_chat(self):
        if not self._current_chat_id:
            return
        name, ok = QInputDialog.getText(self, "Rename Chat", "New name:")
        if ok and name.strip():
            rename_chat(self._current_chat_id, name.strip())
            self._refresh_chat_list()

    def _delete_chat(self):
        if not self._current_chat_id:
            return
        reply = QMessageBox.question(self, "Delete Chat", "Delete this chat permanently?",
                                     QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            delete_chat(self._current_chat_id)
            self._new_chat()
            
    def _auto_name_chat(self, chat_id: int, user_msg: str, assistant_msg: str):
        """Fire-and-forget: generate a 3-5 word title for a new chat."""

        def _run():
            print(f"[AUTO-NAME] thread started", flush=True)

            messages = [
                {"role": "user", "content": (
                    f"Generate a 3-5 word title for this conversation:\n\n"
                    f"User: {user_msg[:200]}\n"
                    f"Assistant: {assistant_msg[:100]}\n\n"
                    f"Reply with the title only. No explanation."
                )},
                {"role": "assistant", "content": "TITLE:"},
            ]

            try:
                response = requests.post(
                    f"{config.LLM_BACKEND_URL}/chat/completions",
                    headers={"Content-Type": "application/json"},
                    json={
                        "model": self.agent.llm.get_model(),
                        "messages": messages,
                        "max_tokens": 30,
                        "temperature": 0.3,
                        "stream": False,
                        "thinking": {"type": "disabled"},
                        "chat_template_kwargs": {"enable_thinking": False},
                    },
                    timeout=60,
                ).json()
                print(f"[AUTO-NAME] got response: {str(response)[:200]}", flush=True)
            except Exception as e:
                import traceback
                print(f"[AUTO-NAME] requests failed: {e}", flush=True)
                traceback.print_exc()
                return

            try:
                msg = response["choices"][0]["message"]
                raw = msg.get("content", "").strip()
                if not raw:
                    raw = msg.get("reasoning_content", "").strip()
            except Exception as e:
                print(f"[AUTO-NAME] extract failed: {e}", flush=True)
                raw = ""

            print(f"[AUTO-NAME] raw repr: {repr(raw[:200])}", flush=True)
            raw = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()

            title = ""
            for line in raw.splitlines():
                m = re.match(r"(?i)title\s*:?\s*(.+)", line.strip())
                if m:
                    title = m.group(1).strip()
                    break

            if not title:
                _thinking_prefixes = (
                    "the user", "i should", "i need", "let me", "okay",
                    "sure", "i'll", "i will", "thinking", "so the", "the assistant",
                    "looking at", "this is", "they want", "analyze", "thinking process",
                    "**analyze", "1.", "2.", "3.",
                )
                for line in raw.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    if any(line.lower().startswith(p) for p in _thinking_prefixes):
                        continue
                    title = line
                    break

            title = re.sub(r'^["\']|["\']$', '', title).strip()
            title = re.sub(r'\*+', '', title).strip()
            title = title.rstrip(".,;:!?").strip()
            title = title[:60]

            if not title or len(title) < 3:
                print("[AUTO-NAME] no usable title extracted", flush=True)
                return

            rename_chat(chat_id, title)
            QTimer.singleShot(0, self._refresh_chat_list)

        threading.Thread(target=_run, daemon=True).start()

    # ── Panel switching ────────────────────────────────────────────────────────

    def _show_panel(self, panel: str):
        self.btn_chat_nav.setChecked(panel == "chat")
        self.btn_settings_nav.setChecked(panel == "settings")
        self.chat_widget.setVisible(panel == "chat")
        self.settings_panel.setVisible(panel == "settings")
        self.header_title.setText(config.AGENT_NAME if panel == "chat" else "Settings")

    def _check_connection(self):
        try:
            result = self.agent.test_connection()
            model = result.replace("Connected — model: ", "")
            self.status_bar.set_connected(model)
        except Exception:
            self.status_bar.set_error("No backend connected — go to Settings to configure")

    # ── Message handling ───────────────────────────────────────────────────────

    def _on_user_message(self, text: str):
        if not text.strip():
            return
        if self.worker is not None and self.worker.isRunning():
            return
        self.worker = None

        content = None
        display_text = text
        clean_text = re.sub(r'\[(image|audio): [^\]]+\]\n?', '', text).strip()

        if self._pending_image:
            path, b64, media_type = self._pending_image
            fname = os.path.basename(path)
            content = [{"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}"}}]
            content.append({"type": "text", "text": clean_text if clean_text else "What do you see in this image?"})
            self._pending_image = None
            self.chat_widget.clear_image_preview()
            display_text = f"[🖼 {fname}]" + (f"  {clean_text}" if clean_text else "")

        elif self._pending_audio:
            path, b64, media_type = self._pending_audio
            fname = os.path.basename(path)
            content = [
                {"type": "input_audio", "input_audio": {"data": b64, "format": media_type}},
            ]
            if clean_text:
                content.append({"type": "text", "text": clean_text})
            self._pending_audio = None
            self.chat_widget.clear_audio_preview()
            display_text = f"[🎵 {fname}]" + (f"  {clean_text}" if clean_text else "")

        else:
            content = text

        self.chat_widget.add_user_message(display_text)
        self.chat_widget.set_input_enabled(False)
        self.status_lbl.setText("processing...")
        if self._current_chat_id:
            save_chat_message(self._current_chat_id, "user", display_text)
        self._live_bubble = self.chat_widget.create_live_bubble()
        self.worker = AgentWorker(self.agent, content, self.signals)
        self.worker.start()
        

    def _on_files_dropped(self, paths: list):
        print(f"[DROP] received paths: {paths}", flush=True)
        current = self.chat_widget.input.toPlainText()
        parts = [current] if current else []

        image_exts = {'.png', '.jpg', '.jpeg', '.webp', '.gif'}
        audio_exts = {'.mp3', '.wav', '.ogg', '.flac', '.m4a'}
        text_exts  = {'.txt', '.md', '.py', '.js', '.ts', '.json', '.csv',
                      '.yaml', '.yml', '.toml', '.ini', '.sh', '.html',
                      '.css', '.xml', '.log'}

        for p in paths:
            ext = os.path.splitext(p)[1].lower()

            if ext in image_exts:
                try:
                    # Resize before encoding — cap longest side at 512px
                    pix_orig = QPixmap(p)
                    if pix_orig.width() > 512 or pix_orig.height() > 512:
                        pix_orig = pix_orig.scaled(512, 512, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    from PySide6.QtCore import QBuffer, QIODevice
                    buf = QBuffer()
                    buf.open(QIODevice.WriteOnly)
                    pix_orig.save(buf, "PNG")
                    raw = bytes(buf.data())
                    b64 = base64.b64encode(raw).decode('utf-8')
                    media_type = 'image/png'
                    self._pending_image = (p, b64, media_type)
                    pix = pix_orig.scaledToHeight(72, Qt.SmoothTransformation)
                    fname = os.path.basename(p)
                    self.chat_widget.show_image_preview(pix, fname)
                    parts.append(f"[image: {fname}]")
                    print(f"[DROP] image encoded: {fname} ({len(b64)} b64 chars)", flush=True)
                except Exception as e:
                    parts.append(f"[image:{p}] (encode error: {e})")
                    
            elif ext in audio_exts:
                try:
                    with open(p, 'rb') as f:
                        raw = f.read()
                    b64 = base64.b64encode(raw).decode('utf-8')
                    mime_map = {
                        '.mp3': 'audio/mpeg', '.wav': 'audio/wav',
                        '.ogg': 'audio/ogg', '.flac': 'audio/flac', '.m4a': 'audio/mp4'
                    }
                    media_type = mime_map.get(ext, 'audio/mpeg')
                    self._pending_audio = (p, b64, media_type)
                    fname = os.path.basename(p)
                    self.chat_widget.show_audio_preview(fname)
                    parts.append(f"[audio: {fname}]")
                    print(f"[DROP] audio encoded: {fname} ({len(b64)} b64 chars)", flush=True)
                except Exception as e:
                    parts.append(f"[audio:{p}] (encode error: {e})")        

            elif ext in text_exts:
                try:
                    with open(p, 'r', encoding='utf-8', errors='replace') as f:
                        contents = f.read()
                    filename = os.path.basename(p)
                    parts.append(f"[file: {filename}]\n```\n{contents}\n```")
                except Exception as e:
                    parts.append(f"[file:{p}] (read error: {e})")

            else:
                # Try reading extensionless files as text
                try:
                    with open(p, 'r', encoding='utf-8', errors='replace') as f:
                        contents = f.read(8192)
                    filename = os.path.basename(p)
                    parts.append(f"[file: {filename}]\n```\n{contents}\n```")
                except Exception:
                    parts.append(f"[file:{p}]")

        self.chat_widget.input.setPlainText("\n\n".join(parts).strip())


    # ── Streaming signal handlers ──────────────────────────────────────────────

    def _on_tool_call(self, name: str, args: dict):
        self.status_lbl.setText(f"⚙ {name}...")
        if self._live_bubble:
            self._live_bubble.add_tool_call(name, args)

    def _on_tool_result(self, name: str, result: str):
        self.status_lbl.setText("processing...")

    def _on_think_start(self, step: int):
        self.status_lbl.setText(f"thinking (step {step})...")
        if self._live_bubble:
            self._live_bubble.open_think_block(step)

    def _on_think_chunk(self, chunk: str):
        if self._live_bubble:
            self._live_bubble.append_think_token(chunk)
            

    def _on_think_end(self):
        self.status_lbl.setText("responding...")
        if self._live_bubble:
            self._live_bubble.close_think_block()

    def _on_response_chunk(self, chunk: str):
        self.status_lbl.setText("")
        if self._live_bubble:
            self._live_bubble.append_response_token(chunk)
            self.chat_widget._scroll_to_bottom_if_near()
            

    def _on_finished(self, response: str):
        

        if self._live_bubble:
            self._live_bubble.finalize()
            self._live_bubble = None
        self.chat_widget.set_input_enabled(True)
        self.status_lbl.setText("")
        self.status_bar.set_tokens(self.agent.get_token_count())
        if self._current_chat_id and response:
            save_chat_message(self._current_chat_id, "assistant", response)
            self._refresh_chat_list()
            # ── Auto-name if still has default timestamp name ──
            current_name = get_chat_name(self._current_chat_id)
            if current_name.startswith("Chat "):
                msgs = load_chat_messages(self._current_chat_id)
                user_msgs = [m["content"] for m in msgs if m["role"] == "user"]
                if user_msgs:
                    print(f"[AUTO-NAME] trigger — chat_id={self._current_chat_id} name='{current_name}'", flush=True)
                    print(f"[AUTO-NAME] user_msg preview: {user_msgs[0][:80]}", flush=True)
                    self._auto_name_chat(self._current_chat_id, user_msgs[0], response)
                

    def _on_error(self, error: str):
        if self._live_bubble:
            self._live_bubble.append_response_token(f"[Error: {error}]")
            self._live_bubble.finalize()
            self._live_bubble = None
        self.chat_widget.set_input_enabled(True)
        self.status_lbl.setText("")
    
    def _on_mic_pressed(self):
        if self.worker and self.worker.isRunning():
            return
        self.chat_widget.mic_btn.setChecked(True)
        self.chat_widget.set_input_enabled(False)
        self.status_lbl.setText("listening...")

        def on_done(text):
            self._stt_signals.transcribed.emit(text)

        def on_error(err):
            self._stt_signals.error.emit(err)

        self.stt.record_and_transcribe(on_done=on_done, on_error=on_error)

    def _on_stt_done(self, text: str):
        self.chat_widget.input.setPlainText(text)
        self.chat_widget.mic_btn.setChecked(False)
        self.chat_widget.set_input_enabled(True)
        self.status_lbl.setText("")

    def _on_stt_error(self, err: str):
        self.chat_widget.mic_btn.setChecked(False)
        self.chat_widget.set_input_enabled(True)
        self.status_lbl.setText(f"STT error: {err}")    
