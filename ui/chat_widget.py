"""
Chat Widget — live streaming bubble, token metrics, think blocks, tool indicators.
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QPushButton,
    QScrollArea, QLabel, QFrame, QSizePolicy, QTextBrowser
)
from PySide6.QtCore import Qt, Signal, QTimer, QMimeData
from PySide6.QtGui import QKeyEvent, QDragEnterEvent, QDropEvent, QPixmap, QAction, QTextCursor

import re 
import time
import sys 
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
import shiboken6
from core.chat_render import md_to_html
from ui.spellcheck_highlighter import SpellCheckHighlighter


# ── Think Block ────────────────────────────────────────────────────────────────

class ThinkBlock(QFrame):
    def __init__(self, step: int, colors: dict, parent=None):
        super().__init__(parent)
        self.colors = colors
        self._content = ""
        self._expanded = False
        self._build(step)

    def _build(self, step: int):
        self.setStyleSheet(f"QFrame{{background:{self.colors['think_bg']};border:1px solid #1a2535;border-radius:6px;margin:2px 0;}}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(0)

        header = QFrame()
        header.setStyleSheet("background:transparent;border:none;")
        header.setCursor(Qt.PointingHandCursor)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(10,6,10,6)
        hl.setSpacing(8)

        self.arrow = QLabel("▶")
        self.arrow.setStyleSheet(f"color:{self.colors['think_text']};font-size:10px;background:transparent;border:none;")
        self.title = QLabel(f"Think (Step {step})")
        self.title.setStyleSheet(f"color:{self.colors['think_text']};font-size:12px;font-weight:bold;background:transparent;border:none;")

        hl.addWidget(self.arrow)
        hl.addWidget(self.title)
        hl.addStretch()

        header.mousePressEvent = lambda e: self._toggle()
        layout.addWidget(header)

        self.body = QFrame()
        self.body.setStyleSheet("background:transparent;border:none;")
        bl = QVBoxLayout(self.body)
        bl.setContentsMargins(10,0,10,8)
        self.text_lbl = QLabel("")
        self.text_lbl.setWordWrap(True)
        self.text_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.text_lbl.setStyleSheet(f"color:{self.colors['think_text']};font-size:12px;font-style:italic;background:transparent;border:none;")
        bl.addWidget(self.text_lbl)
        self.body.setVisible(False)
        layout.addWidget(self.body)

    def _toggle(self):
        self._expanded = not self._expanded
        self.arrow.setText("▼" if self._expanded else "▶")
        self.body.setVisible(self._expanded)

    def append_token(self, token: str):
        self._content += token
        self.text_lbl.setText(self._content[-2000:])  # cap display at 2k chars

    def finalize_content(self):
        """Swap the capped QLabel for a QTextBrowser holding the FULL _content —
        mirrors the exact pattern LiveResponseBubble.finalize() uses for response text."""
        if not self._content:
            return
        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        browser.setReadOnly(True)
        browser.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        browser.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        browser.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        browser.setStyleSheet(
            f"QTextBrowser{{background:transparent;border:none;"
            f"color:{self.colors['think_text']};font-size:12px;font-style:italic;}}"
        )
        browser.setPlainText(self._content)
        layout = self.body.layout()
        layout.replaceWidget(self.text_lbl, browser)
        self.text_lbl.deleteLater()
        self.text_lbl = browser


# ── Tool Row ───────────────────────────────────────────────────────────────────

class ToolRow(QFrame):
    def __init__(self, name: str, args: dict, colors: dict, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10,4,10,4)
        args_str = ", ".join(f"{k}={repr(v)[:30]}" for k,v in args.items()) if args else ""
        lbl = QLabel(f"⚙  {name}({args_str})" if args_str else f"⚙  {name}()")
        lbl.setStyleSheet(f"color:{colors['tool_text']};font-size:11px;font-family:monospace;background:transparent;border:none;")
        layout.addWidget(lbl)
        self.setStyleSheet(f"QFrame{{background:{colors['tool_bg']};border-left:2px solid {colors['accent_dim']};border-radius:0 4px 4px 0;margin:1px 0;}}")


# ── Token Metrics Bar ──────────────────────────────────────────────────────────

class MetricsBar(QFrame):
    def __init__(self, colors: dict, avatar_path: str = None, tts=None,
                 tts_speech_allowed=None, parent=None):
        self.avatar_path = avatar_path
        self._tts = tts
        self._tts_speech_allowed = tts_speech_allowed or (lambda: True)
        self._response_text = ""
        super().__init__(parent)
        self.colors = colors
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(8)
        self.lbl = QLabel("")
        self.lbl.setStyleSheet(f"color:{colors['text_dim']};font-size:10px;font-family:monospace;background:transparent;border:none;")
        layout.addWidget(self.lbl)
        layout.addStretch()
        self.replay_btn = QPushButton("🔊")
        self.replay_btn.setFixedSize(22, 22)
        self.replay_btn.setToolTip("Replay response")
        self.replay_btn.setStyleSheet(f"QPushButton{{background:transparent;border:none;font-size:13px;color:{colors['text_dim']};}}QPushButton:hover{{color:{colors['text_primary']};}}")
        self.replay_btn.setVisible(False)
        self.replay_btn.clicked.connect(self._replay)
        layout.addWidget(self.replay_btn)
        self.copy_btn = QPushButton("⧉")
        self.copy_btn.setFixedSize(22, 22)
        self.copy_btn.setToolTip("Copy response")
        self.copy_btn.setStyleSheet(f"QPushButton{{background:transparent;border:none;font-size:13px;color:{colors['text_dim']};}}QPushButton:hover{{color:{colors['text_primary']};}}")
        self.copy_btn.setVisible(False)
        self.copy_btn.clicked.connect(self._copy)
        layout.addWidget(self.copy_btn)
        self.setStyleSheet("background:transparent;border:none;")

    def _replay(self):
        if self._tts and self._response_text and self._tts_speech_allowed():
            self.replay_btn.setEnabled(False)
            self.replay_btn.setText("⏳")
            self._tts.speak(self._response_text, blocking=False, on_done=self._replay_done)

    def _replay_done(self):
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self._reset_replay_btn)

    def _reset_replay_btn(self):
        self.replay_btn.setText("🔊")
        self.replay_btn.setEnabled(True)

    def _copy(self):
        from PySide6.QtWidgets import QApplication
        if self._response_text:
            QApplication.clipboard().setText(self._response_text)

    def set_metrics(self, elapsed: float, tok_in: int, tok_out: int, tool_calls: int, think_time: float):
        tok_s = tok_out / elapsed if elapsed > 0 else 0
        parts = [
            f"{elapsed:.1f}s",
            f"{tok_s:.1f} tok/s",
            f"{tok_in}in / {tok_out}out / {tok_in+tok_out}total",
        ]
        if tool_calls:
            parts.append(f"{tool_calls} tool calls")
        if think_time > 0:
            parts.append(f"think: {think_time:.1f}s")
        self.lbl.setText("  ·  ".join(parts))
        self.copy_btn.setVisible(True)
        if self._tts:
            self.replay_btn.setVisible(True)


# ── Live Response Bubble ───────────────────────────────────────────────────────

class LiveResponseBubble(QFrame):
    def __init__(self, colors: dict, avatar_path: str = None, agent_name: str = None,
                 tts=None, tts_speech_allowed=None, parent=None):
        self.avatar_path = avatar_path
        self._agent_name = agent_name or config.AGENT_NAME
        self._tts = tts
        self._tts_speech_allowed = tts_speech_allowed
        super().__init__(parent)
        self.colors = colors
        self._think_block = None
        self._response_text = ""
        self._start_time = time.time()
        self._think_start_time = 0.0
        self._think_time = 0.0
        self._tok_out = 0
        self._tool_calls = 0
        self._build()

    def _build(self):
        self.setStyleSheet("background:transparent;border:none;")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0,4,0,4)
        outer.setSpacing(4)

        role_row = QHBoxLayout()
        role_row.setContentsMargins(0,0,0,0)
        role_row.setSpacing(6)

        if self.avatar_path and os.path.exists(self.avatar_path):
            from PySide6.QtGui import QPixmap, QPainter, QPainterPath
            src = QPixmap(self.avatar_path).scaled(24, 24, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            out = QPixmap(24, 24)
            out.fill(Qt.transparent)
            p = QPainter(out)
            p.setRenderHint(QPainter.Antialiasing)
            path = QPainterPath()
            path.addEllipse(0, 0, 24, 24)
            p.setClipPath(path)
            p.drawPixmap(0, 0, src)
            p.end()
            av = QLabel()
            av.setPixmap(out)
            av.setFixedSize(24, 24)
            role_row.addWidget(av)

        role = QLabel(self._agent_name)
        role.setAlignment(Qt.AlignLeft)
        role.setStyleSheet(f"color:{self.colors['accent']};font-size:11px;font-weight:bold;letter-spacing:1px;background:transparent;border:none;")
        role_row.addWidget(role)
        role_row.addStretch()
        outer.addLayout(role_row)

        self.bubble = QFrame()
        self.bubble.setStyleSheet(f"QFrame{{background:{self.colors['ai_bubble']};border:1px solid {self.colors['border_accent']};border-radius:4px 12px 12px 12px;}}")
        self.bubble_layout = QVBoxLayout(self.bubble)
        self.bubble_layout.setContentsMargins(12,10,12,10)
        self.bubble_layout.setSpacing(4)

        # Streaming text label — visible while streaming
        self.stream_lbl = QLabel("▋")  # cursor blink placeholder
        self.stream_lbl.setWordWrap(True)
        self.stream_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.stream_lbl.setStyleSheet(f"color:{self.colors['text_primary']};font-size:13px;background:transparent;border:none;")
        self.stream_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.bubble_layout.addWidget(self.stream_lbl)

        # Metrics bar (hidden until finalized)
        self.metrics = MetricsBar(
            self.colors, tts=self._tts,
            tts_speech_allowed=self._tts_speech_allowed,
        )
        self.metrics.setVisible(False)
        self.bubble_layout.addWidget(self.metrics)

        outer.addWidget(self.bubble)

        # Cursor blink
        self._cursor_visible = True
        self._cursor_timer = QTimer()
        self._cursor_timer.timeout.connect(self._blink_cursor)
        self._cursor_timer.start(500)

    def _blink_cursor(self):
        if not self._response_text:
            self._cursor_visible = not self._cursor_visible
            self.stream_lbl.setText("▋" if self._cursor_visible else " ")

    def open_think_block(self, step: int):
        if not shiboken6.isValid(self.bubble_layout):
            return
        self._think_start_time = time.time()
        self._think_block = ThinkBlock(step, self.colors)
        idx = self.bubble_layout.indexOf(self.stream_lbl)
        self.bubble_layout.insertWidget(idx, self._think_block)
        # Show "thinking..." placeholder
        self.stream_lbl.setText("▋")

    def append_think_token(self, token: str):
        if self._think_block:
            self._think_block.append_token(token)

    def close_think_block(self):
        if self._think_start_time:
            self._think_time += time.time() - self._think_start_time
            self._think_start_time = 0.0
        if self._think_block:
            self._think_block.finalize_content()
        self._think_block = None

    def add_tool_call(self, name: str, args: dict):
        if not shiboken6.isValid(self.bubble_layout):
            return
        self._tool_calls += 1
        row = ToolRow(name, args, self.colors)
        idx = self.bubble_layout.indexOf(self.stream_lbl)
        self.bubble_layout.insertWidget(idx, row)

    def append_response_token(self, token: str):
        if not shiboken6.isValid(self.stream_lbl):
            return
        if not self._response_text:
            self._start_time = time.time()  # reset clock at first response token
        self._cursor_timer.stop()
        self._response_text += token
        self._tok_out += 1
        self.stream_lbl.setText(self._response_text + "▋")
        

    def finalize(self):
        if not shiboken6.isValid(self.bubble_layout):
            return
        self._cursor_timer.stop()
        elapsed = time.time() - self._start_time

        if self._response_text.strip():
            # Swap streaming label for rendered QTextBrowser
            browser = QTextBrowser()
            browser.setOpenExternalLinks(True)
            browser.setReadOnly(True)
            browser.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            browser.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            browser.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
            html = md_to_html(self._response_text, self.colors)
            browser.setHtml(f"""<html><body style="
                background:{self.colors['ai_bubble']};color:{self.colors['text_primary']};
                font-family:'JetBrains Mono','Fira Code','Cascadia Code',monospace;
                font-size:13px;line-height:1.6;margin:0;padding:0;">
                {html}</body></html>""")
            browser.setStyleSheet("QTextBrowser{background:transparent;border:none;padding:0;}")
            browser.document().setTextWidth(900)
            h = int(browser.document().size().height()) + 16
            browser.setFixedHeight(max(h, 30))

            idx = self.bubble_layout.indexOf(self.stream_lbl)
            self.bubble_layout.removeWidget(self.stream_lbl)
            self.stream_lbl.deleteLater()
            self.bubble_layout.insertWidget(idx, browser)
        else:
            self.stream_lbl.setText("")

        # Show metrics — estimate tok_in from context (~183 shown in screenshot)
        self.metrics.set_metrics(elapsed, 0, self._tok_out, self._tool_calls, self._think_time)
        self.metrics.setVisible(True)
        self.metrics._response_text = self._response_text


# ── Smart Input (drag & drop aware) ───────────────────────────────────────────

class SmartInput(QTextEdit):
    submit = Signal(str)
    files_dropped = Signal(list)

    def __init__(self, colors: dict, avatar_path: str = None, user_avatar_path: str = None, parent=None):
        self.lumina_avatar_path = avatar_path
        self.user_avatar_path = user_avatar_path
        super().__init__(parent)
        self.setPlaceholderText(f"Message {config.AGENT_NAME}...  (Shift+Enter for newline, drag & drop files)")
        self.setMaximumHeight(120)
        self.setMinimumHeight(44)
        self.setAcceptDrops(True)
        self.setStyleSheet(f"""
            QTextEdit{{background:{colors['bg_input']};color:{colors['text_primary']};
            border:1px solid {colors['border']};border-radius:10px;padding:10px 14px;font-size:13px;}}
            QTextEdit:focus{{border:1px solid {colors['border_accent']};}}
        """)
        self._spell_highlighter = SpellCheckHighlighter(self.document())

    def update_placeholder(self, name: str):
        self.setPlaceholderText(f"Message {name}...  (Shift+Enter for newline, drag & drop files)")
    
    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key_Return and not (event.modifiers() & Qt.ShiftModifier):
            text = self.toPlainText().strip()
            if text:
                self.submit.emit(text)
                self.clear()
        else:
            super().keyPressEvent(event)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls() or event.mimeData().hasText():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event: QDropEvent):
        mime = event.mimeData()
        if mime.hasUrls():
            paths = [u.toLocalFile() for u in mime.urls() if u.isLocalFile()]
            if paths:
                self.files_dropped.emit(paths)
                event.acceptProposedAction()
                return
        super().dropEvent(event)

    def contextMenuEvent(self, event):
        """Right-click menu: standard Qt actions plus spelling suggestions
        prepended above them when the click landed on a misspelled word.
        Menu order built via repeated insertAction(first, ...) against the
        same anchor -- each insert lands right before the anchor, so
        inserting in order [suggestions..., add-to-dict, separator]
        produces exactly that order, ending right before the standard
        Undo/Redo/Cut/Copy/Paste block. Verified against a real QMenu
        before this was written into the task block, not assumed."""
        from core.spellcheck import check_word, suggest, add_to_dictionary

        menu = self.createStandardContextMenu()
        cursor = self.cursorForPosition(event.pos())
        cursor.select(QTextCursor.SelectionType.WordUnderCursor)
        word = cursor.selectedText()

        if word and any(c.isalpha() for c in word) and not check_word(word):
            first = menu.actions()[0] if menu.actions() else None
            suggestions = suggest(word)

            if suggestions:
                for s in suggestions:
                    action = QAction(s, menu)
                    action.triggered.connect(
                        lambda checked=False, s=s, c=cursor: self._apply_suggestion(c, s)
                    )
                    menu.insertAction(first, action)
            else:
                no_sugg = QAction("(no suggestions)", menu)
                no_sugg.setEnabled(False)
                menu.insertAction(first, no_sugg)

            add_action = QAction(f'Add "{word}" to Dictionary', menu)
            add_action.triggered.connect(lambda checked=False, w=word: add_to_dictionary(w))
            menu.insertAction(first, add_action)
            menu.insertSeparator(first)

        menu.exec(event.globalPos())

    def _apply_suggestion(self, cursor: QTextCursor, replacement: str):
        cursor.insertText(replacement)


# ── Chat Widget ────────────────────────────────────────────────────────────────

class ChatWidget(QWidget):
    message_submitted = Signal(str)
    files_dropped = Signal(list)
    audio_preview_cancelled = Signal()
    mic_pressed = Signal()

    def __init__(self, colors: dict, avatar_path: str = None, user_avatar_path: str = None,
                 tts=None, tts_speech_allowed=None, parent=None):
        super().__init__(parent)
        self.colors = colors
        self.avatar_path = avatar_path
        self._persona_name = config.AGENT_NAME
        self.user_avatar_path = user_avatar_path
        self._tts = tts
        self._tts_speech_allowed = tts_speech_allowed
        self._preview_frame = None
        self._build()

    def set_persona(self, name: str, avatar_path: str):
        self.avatar_path = avatar_path
        self._persona_name = name
        self.input.update_placeholder(name)
        
    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(0)
        self._main_layout = layout

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet(f"QScrollArea{{background:{self.colors['bg_deep']};border:none;}}")

        self.msgs_container = QWidget()
        self.msgs_container.setStyleSheet(f"background:{self.colors['bg_deep']};")
        self.msgs_layout = QVBoxLayout(self.msgs_container)
        self.msgs_layout.setContentsMargins(24,20,24,20)
        self.msgs_layout.setSpacing(12)
        self.msgs_layout.addStretch()

        self.scroll.setWidget(self.msgs_container)
        layout.addWidget(self.scroll, 1)

        # Input bar
        bar = QFrame()
        bar.setStyleSheet(f"QFrame{{background:{self.colors['bg_panel']};border-top:1px solid {self.colors['border']};}}")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(16,12,16,12)
        bl.setSpacing(10)

        self.input = SmartInput(self.colors)
        self.input.submit.connect(self.message_submitted.emit)
        self.input.files_dropped.connect(self.files_dropped.emit)

        self.send_btn = QPushButton("↑")
        self.send_btn.setFixedSize(40,40)
        self.send_btn.setCursor(Qt.PointingHandCursor)
        self.send_btn.setStyleSheet(f"""
            QPushButton{{background:{self.colors['accent']};color:{self.colors['bg_deep']};border:none;border-radius:10px;font-size:18px;font-weight:bold;}}
            QPushButton:hover{{background:#33ecff;}}
            QPushButton:disabled{{background:{self.colors['text_dim']};color:{self.colors['bg_panel']};}}
        """)
        self.send_btn.clicked.connect(self._send)
        
        self.mic_btn = QPushButton("🎙")
        self.mic_btn.setFixedSize(40, 40)
        self.mic_btn.setCursor(Qt.PointingHandCursor)
        self.mic_btn.setCheckable(True)
        self.mic_btn.setStyleSheet(f"""
            QPushButton{{background:{self.colors['bg_card']};color:{self.colors['text_primary']};border:1px solid {self.colors['border']};border-radius:10px;font-size:18px;}}
            QPushButton:hover{{background:{self.colors['bg_panel']};border-color:{self.colors['accent']};}}
            QPushButton:checked{{background:{self.colors['accent']};color:{self.colors['bg_deep']};border:none;}}
            QPushButton:disabled{{background:{self.colors['text_dim']};color:{self.colors['bg_panel']};}}
        """)
        self.mic_btn.clicked.connect(self.mic_pressed.emit)

        bl.addWidget(self.input, 1)
        bl.addWidget(self.mic_btn)
        bl.addWidget(self.send_btn)
        layout.addWidget(bar)

    def _send(self):
        text = self.input.toPlainText().strip()
        if text:
            self.message_submitted.emit(text)
            self.input.clear()

    def _insert(self, widget: QWidget):
        self.msgs_layout.insertWidget(self.msgs_layout.count()-1, widget)
        QTimer.singleShot(80, self._scroll_to_bottom)

    def _scroll_to_bottom(self):
        self.scroll.verticalScrollBar().setValue(
            self.scroll.verticalScrollBar().maximum()
        )
        
    def _scroll_to_bottom_if_near(self):
        bar = self.scroll.verticalScrollBar()
        if bar.maximum() - bar.value() < 200:
            bar.setValue(bar.maximum())    

    def add_user_message(self, text: str):
        frame = QFrame()
        frame.setStyleSheet("background:transparent;border:none;")
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(0,4,0,4)
        fl.setSpacing(4)

        # Header row — avatar + name
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0,0,0,0)
        header_row.setSpacing(6)
        header_row.addStretch()

        role = QLabel(config.USER_NAME)
        role.setAlignment(Qt.AlignRight)
        role.setStyleSheet(f"color:{self.colors['accent_dim']};font-size:11px;font-weight:bold;letter-spacing:1px;background:transparent;border:none;")
        header_row.addWidget(role)

        # User avatar thumbnail
        if self.user_avatar_path and os.path.exists(self.user_avatar_path):
            from PySide6.QtGui import QPixmap, QPainter, QPainterPath
            src = QPixmap(self.user_avatar_path).scaled(24, 24, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            out = QPixmap(24, 24)
            out.fill(Qt.transparent)
            p = QPainter(out)
            p.setRenderHint(QPainter.Antialiasing)
            path = QPainterPath()
            path.addEllipse(0, 0, 24, 24)
            p.setClipPath(path)
            p.drawPixmap(0, 0, src)
            p.end()
            av = QLabel()
            av.setPixmap(out)
            av.setFixedSize(24, 24)
            header_row.addWidget(av)

        fl.addLayout(header_row)

        content = QLabel(text)
        content.setWordWrap(True)
        # UI-TRUST-01A: text inside the user bubble reads left-to-right from
        # the card's LEFT edge (prose, task blocks, lists, code). The bubble
        # itself keeps its right-side visual identity via the header row and
        # corner styling above -- only the inner text alignment changes here.
        content.setAlignment(Qt.AlignLeft)
        content.setTextInteractionFlags(Qt.TextSelectableByMouse)
        content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        content.setStyleSheet(f"background:{self.colors['user_bubble']};color:{self.colors['text_primary']};padding:10px 14px;border-radius:12px 4px 12px 12px;border:1px solid {self.colors['border']};font-size:13px;")
        fl.addWidget(content)
        self._insert(frame)

    def add_system_message(self, text: str):
        lbl = QLabel(text)
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet(f"color:{self.colors['text_muted']};font-size:11px;padding:4px;font-style:italic;background:transparent;border:none;")
        self._insert(lbl)

    def add_operator_message(self, text: str):
        """Render out-of-band cockpit output without writing it into chat history."""
        frame = QFrame()
        frame.setStyleSheet(
            f"QFrame{{background:{self.colors['bg_card']};border:1px solid {self.colors['border']};"
            "border-radius:8px;margin:2px 24px;}}"
        )
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(10, 8, 10, 8)
        lbl = QLabel(f"⌘  {text}")
        lbl.setWordWrap(True)
        lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lbl.setStyleSheet(
            f"color:{self.colors['text_muted']};font-size:11px;background:transparent;border:none;"
        )
        layout.addWidget(lbl)
        self._insert(frame)

    def create_live_bubble(self) -> LiveResponseBubble:
        bubble = LiveResponseBubble(self.colors, avatar_path=self.avatar_path,
                                    agent_name=getattr(self, '_persona_name', config.AGENT_NAME),
                                    tts=self._tts,
                                    tts_speech_allowed=self._tts_speech_allowed)
        self._insert(bubble)
        return bubble

    def set_input_enabled(self, enabled: bool):
        self.input.setEnabled(enabled)
        self.send_btn.setEnabled(enabled)
        self.mic_btn.setEnabled(enabled)
        if enabled:
            self.input.setFocus()

    def set_turn_running(self, running: bool):
        """Keep text commands usable mid-turn while preventing a second foreground turn."""
        self.input.setEnabled(True)
        self.send_btn.setEnabled(True)
        self.mic_btn.setEnabled(not running)
        if running:
            self.input.setPlaceholderText("Main task running — /status · /btw <question> · /stop")
        else:
            self.input.update_placeholder(self._persona_name)
            self.input.setFocus()

    def clear_messages(self):
        while self.msgs_layout.count() > 1:
            item = self.msgs_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
                
    def show_image_preview(self, pixmap: QPixmap, filename: str):
        """Show a thumbnail preview of the pending image above the input bar."""
        self.clear_image_preview()
        frame = QFrame()
        frame.setStyleSheet(f"""
            QFrame{{background:{self.colors['bg_card']};
            border-top:1px solid {self.colors['border_accent']};
            border-bottom:none;padding:4px 16px;}}
        """)
        row = QHBoxLayout(frame)
        row.setContentsMargins(0, 4, 0, 4)
        row.setSpacing(10)

        img_lbl = QLabel()
        img_lbl.setPixmap(pixmap)
        row.addWidget(img_lbl)

        name_lbl = QLabel(f"🖼  {filename}")
        name_lbl.setStyleSheet(f"color:{self.colors['text_muted']};font-size:11px;background:transparent;")
        row.addWidget(name_lbl, 1)

        clear_btn = QPushButton("✕")
        clear_btn.setFixedSize(20, 20)
        clear_btn.setCursor(Qt.PointingHandCursor)
        clear_btn.setStyleSheet(f"""
            QPushButton{{background:transparent;border:none;
            color:{self.colors['text_dim']};font-size:12px;}}
            QPushButton:hover{{color:{self.colors['danger']};}}
        """)
        clear_btn.clicked.connect(self._on_preview_cleared)
        row.addWidget(clear_btn)

        # Insert above input bar (second-to-last item in main layout)
        insert_pos = self._main_layout.count() - 1
        self._main_layout.insertWidget(insert_pos, frame)
        self._preview_frame = frame

    def clear_image_preview(self):
        """Remove the image preview frame if present."""
        if self._preview_frame is not None:
            self._preview_frame.setParent(None)
            self._preview_frame.deleteLater()
            self._preview_frame = None
            
    def show_audio_preview(self, fname: str):
        """Show an audio file preview above the input bar."""
        self.clear_audio_preview()
        frame = QFrame()
        frame.setStyleSheet(f"""
            QFrame{{background:{self.colors['bg_card']};
            border-top:1px solid {self.colors['border_accent']};
            border-bottom:none;padding:4px 16px;}}
        """)
        row = QHBoxLayout(frame)
        row.setContentsMargins(0, 4, 0, 4)
        row.setSpacing(10)
        icon_lbl = QLabel("🎵")
        icon_lbl.setStyleSheet("background:transparent;font-size:18px;")
        row.addWidget(icon_lbl)
        name_lbl = QLabel(fname)
        name_lbl.setStyleSheet(f"color:{self.colors['text_muted']};font-size:11px;background:transparent;")
        row.addWidget(name_lbl, 1)
        clear_btn = QPushButton("✕")
        clear_btn.setFixedSize(20, 20)
        clear_btn.setCursor(Qt.PointingHandCursor)
        clear_btn.setStyleSheet(f"""
            QPushButton{{background:transparent;border:none;
            color:{self.colors['text_dim']};font-size:12px;}}
            QPushButton:hover{{color:{self.colors['danger']};}}
        """)
        clear_btn.clicked.connect(lambda: self._cancel_audio(fname))
        row.addWidget(clear_btn)
        self._audio_preview_frame = frame
        self._main_layout.insertWidget(self._main_layout.count() - 1, frame)
    def clear_audio_preview(self):
        if hasattr(self, '_audio_preview_frame') and self._audio_preview_frame is not None:
            self._audio_preview_frame.setParent(None)
            self._audio_preview_frame.deleteLater()
            self._audio_preview_frame = None

    def _cancel_audio(self, fname):
        self.clear_audio_preview()
        # Signal main window to clear _pending_audio
        self.audio_preview_cancelled.emit()

    def _on_preview_cleared(self):
        """User clicked ✕ — clear preview and notify parent window."""
        self.clear_image_preview()
        parent = self.parent()
        while parent is not None:
            if hasattr(parent, '_pending_image'):
                parent._pending_image = None
                text = self.input.toPlainText()
                text = re.sub(r'\[image: [^\]]+\]\n?', '', text).strip()
                self.input.setPlainText(text)
                break
            parent = parent.parent()

            
