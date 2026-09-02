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
    QMessageBox, QFileDialog, QScrollArea, QProgressBar
)
from PySide6.QtCore import Qt, QThread, Signal, QTimer, QObject
from PySide6.QtGui import QFont, QPixmap, QPainter, QBrush, QColor, QPainterPath, QIcon

import os
import sys
import base64
import re 
import threading 
import time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from core.agent import TurnCancelled, is_error_response
from core import emergency_stop
from core.context_reconstruction import reconstruct_chat_context, resolve_context_skip
from core.context_transaction import ContextGeneration
from core.manual_compaction import run_manual_compaction
from core.operator_commands import (
    command_help, compaction_cut_index, format_duration, format_tokens,
    parse_operator_command, unwrap_background_result,
)
from core.personas import list_personas, load_persona
from core import persistence
from core.reasoning_preferences import resolve_reasoning_effort
from core import dreaming 
from ui.chat_widget import ChatWidget, LiveResponseBubble
from ui.settings import SettingsPanel
from ui.review_panel import ReviewPanel
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

# Same arrow-visibility fix as ui/settings/_widgets.py's _combo() -- a pure
# CSS border-triangle doesn't actually render as a triangle for Qt's
# ::down-arrow subcontrol (verified via offscreen render, not assumed), so
# this uses the same real chevron icon rather than reinventing a second
# approach for the two combo boxes (persona_combo/chat_combo) that inherit
# from this app-wide stylesheet instead of _combo(). A :hover state-swap
# icon was tried and pulled -- see _combo()'s docstring for why.
_CHEVRON_DOWN = os.path.join(config.ASSETS_DIR, "icons", "chevron_down.png").replace(os.sep, "/")

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
QComboBox:hover {{ border-color: {COLORS['accent_dim']}; }}
QComboBox::drop-down {{
    subcontrol-origin: padding; subcontrol-position: top right; width: 20px;
    border-left: 1px solid {COLORS['border']}; background: {COLORS['bg_card']};
    border-top-right-radius: 5px; border-bottom-right-radius: 5px;
}}
QComboBox::down-arrow {{
    image: url({_CHEVRON_DOWN});
}}
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
    commentary     = Signal(str)   # AGENT-COMMENTARY-01A: whole event per provider decision, never batched
    # TOKS-STREAM-TIMING-01 -- mirrors core/agent.py's on_final_stream_timing:
    # (duration_s, token_count) for whichever provider generation produced
    # this turn's visible Final text. Fires at most once per turn, always
    # before `finished`/`cancelled` (same emitting thread, same call --
    # see AgentWorker.run() below), never for a synthetic/empty/cancelled
    # final. Absence (never emitted this turn) is the UI's "unavailable"
    # signal -- there is no separate sentinel value for it.
    final_stream_timing = Signal(float, int)
    # TOKS-STREAM-TIMING-01 (Bino-approved expansion, Bino-corrected
    # naming) -- mirrors core/agent.py's on_final_ttft/
    # on_time_to_first_answer. Both fire at most once per turn,
    # independently of final_stream_timing and of each other -- see
    # core/agent.py's _stream_final() docstring for why these are three
    # separate clocks with three separate anchors, never allowed to
    # overwrite one another. Absence is each one's own "unavailable"
    # signal, same convention as above. final_ttft is scoped to THIS
    # request only -- never an earlier WORK/tool round's dispatch on a
    # multi-round turn (see core/agent.py's _fire_final_ttft() docstring).
    final_ttft            = Signal(float)
    time_to_first_answer  = Signal(float)
    finished       = Signal(str)
    error          = Signal(str)
    # SEPT-AC-R1-C01 -- presentation text and persistence authority are
    # separate. Reconciliation can have already displayed partial text while
    # explicitly forbidding it from becoming canonical assistant history.
    cancelled      = Signal(str, bool)
    manual_compaction_finished = Signal(object)
    
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

    def __init__(self, agent, user_input, signals: StreamSignals, chat_id: int = None):
        super().__init__()
        self.agent = agent
        self.user_input = user_input
        self.signals = signals
        self.chat_id = chat_id
        self._think_buf = ""
        self._resp_buf = ""
        self._cancel_event = threading.Event()

    def request_cancel(self) -> bool:
        """Request cooperative cancellation; False means it was already requested."""
        if self._cancel_event.is_set():
            return False
        self._cancel_event.set()
        return True

    def cancel_requested(self) -> bool:
        return self._cancel_event.is_set()

    def _agent_accepts_cancel_event(self) -> bool:
        """Compatibility for lightweight GUI-test stubs predating /stop."""
        import inspect
        try:
            params = inspect.signature(self.agent.chat).parameters.values()
        except (TypeError, ValueError):
            return True
        return any(p.name == "cancel_event" or p.kind == inspect.Parameter.VAR_KEYWORD for p in params)

    def _agent_accepts_reasoning_effort(self) -> bool:
        """Patch 3A.4 Part 4 -- same compatibility shape as
        _agent_accepts_cancel_event() above, for the same reason: lightweight
        GUI-test agent stubs (tests/test_operator_stop_ui.py) predate
        reasoning-effort persistence and define chat() with neither a
        reasoning_effort param nor **kwargs."""
        import inspect
        try:
            params = inspect.signature(self.agent.chat).parameters.values()
        except (TypeError, ValueError):
            return True
        return any(p.name == "reasoning_effort" or p.kind == inspect.Parameter.VAR_KEYWORD for p in params)

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

        def on_commentary(text):
            # Same flush-before-emit discipline as on_tool_call above, so
            # any buffered think/response text from an earlier stage lands
            # in the UI before this event, even though in practice nothing
            # is streaming concurrently at the point this fires (see
            # core/agent.py's on_commentary docstring).
            self._flush_think()
            self._flush_resp()
            self.signals.commentary.emit(text)

        def on_final_stream_timing(duration_s, token_count):
            # TOKS-STREAM-TIMING-01 -- no buffering: fires once, already
            # holds the two final numbers, nothing to batch.
            self.signals.final_stream_timing.emit(duration_s, token_count)

        def on_final_ttft(final_ttft_s):
            self.signals.final_ttft.emit(final_ttft_s)

        def on_time_to_first_answer(ttfa_s):
            self.signals.time_to_first_answer.emit(ttfa_s)

        self.agent.on_tool_call      = on_tool_call
        self.agent.on_tool_result    = on_tool_result
        self.agent.on_think_start    = on_think_start
        self.agent.on_think_token    = on_think_token
        self.agent.on_think_end      = on_think_end
        self.agent.on_response_token = on_response_token
        self.agent.on_commentary     = on_commentary
        self.agent.on_final_stream_timing = on_final_stream_timing
        self.agent.on_final_ttft = on_final_ttft
        self.agent.on_time_to_first_answer = on_time_to_first_answer

        try:
            kwargs = {"chat_id": self.chat_id}
            if self._agent_accepts_cancel_event():
                kwargs["cancel_event"] = self._cancel_event
            # Patch 3A.4 Part 4 -- resolved fresh on every turn against the
            # actual live backend in use (self.agent.llm), never cached on
            # this worker or the agent, since Settings can swap the active
            # backend between turns. Guarded the same way cancel_event is
            # above, for the same pre-3A.4 GUI-test-stub compatibility
            # reason (see _agent_accepts_reasoning_effort()).
            if self._agent_accepts_reasoning_effort():
                llm = getattr(self.agent, "llm", None)
                if llm is not None:
                    kwargs["reasoning_effort"] = resolve_reasoning_effort(llm)
            result = self.agent.chat(self.user_input, **kwargs)
            self._flush_think()
            self._flush_resp()
            self.signals.finished.emit(result)
        except TurnCancelled as e:
            self._flush_think()
            self._flush_resp()
            self.signals.cancelled.emit(
                e.partial_response,
                e.persist_partial_response,
            )
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
        self.task_lbl = QLabel("")
        self.task_lbl.setStyleSheet(f"color:{COLORS['success']};font-size:11px;background:transparent;")
        self.task_lbl.setVisible(False)
        self.context_lbl = QLabel("")
        self.context_lbl.setStyleSheet(f"color:{COLORS['text_muted']};font-size:11px;background:transparent;")
        self.context_bar = QProgressBar()
        self.context_bar.setRange(0, 1000)
        self.context_bar.setValue(0)
        self.context_bar.setTextVisible(False)
        self.context_bar.setFixedSize(90, 7)
        self._style_context_bar(0.0)
        self.emergency_btn = QPushButton("")
        self.emergency_btn.setCursor(Qt.PointingHandCursor)
        self.emergency_btn.setFixedHeight(20)
        self.set_emergency_state(latched=False, rearm_ready=False)
        layout.addWidget(self.dot)
        layout.addWidget(self.model_lbl)
        layout.addWidget(self.task_lbl)
        layout.addStretch()
        layout.addWidget(self.context_lbl)
        layout.addWidget(self.context_bar)
        layout.addWidget(self.emergency_btn)

    def set_connected(self, model: str):
        self.dot.setStyleSheet(f"color:{COLORS['success']};font-size:10px;background:transparent;")
        self.model_lbl.setToolTip(model)
        self.model_lbl.setText(model[:40]+"..." if len(model)>40 else model)

    def set_error(self, msg: str = "LM Studio offline"):
        self.dot.setStyleSheet(f"color:{COLORS['danger']};font-size:10px;background:transparent;")
        self.model_lbl.setText(msg)

    def set_checking(self, msg: str = "Checking connection..."):
        """UI-TRUST-01B: truthful transitional state while the live backend
        is being re-checked after a settings change. Neutral dot -- neither
        the old green nor an alarming red."""
        self.dot.setStyleSheet(f"color:{COLORS['text_dim']};font-size:10px;background:transparent;")
        self.model_lbl.setText(msg)

    @staticmethod
    def _fmt_tokens(count: int) -> str:
        count = max(0, int(count or 0))
        if count >= 1_000_000:
            return f"{count / 1_000_000:.1f}M".replace(".0M", "M")
        if count >= 1_000:
            return f"{count / 1_000:.1f}k".replace(".0k", "k")
        return str(count)

    def _style_context_bar(self, percent: float):
        if percent >= 90:
            chunk = COLORS['danger']
        elif percent >= 75:
            chunk = COLORS['warning']
        else:
            chunk = COLORS['accent_dim']
        self.context_bar.setStyleSheet(f"""
            QProgressBar {{
                background:{COLORS['bg_input']}; border:none; border-radius:3px;
            }}
            QProgressBar::chunk {{ background:{chunk}; border-radius:3px; }}
        """)

    def set_context_usage(self, usage: dict):
        used = int(usage.get("used_tokens", 0))
        limit = max(1, int(usage.get("max_tokens", 1)))
        reserve = max(0, int(usage.get("reserve_tokens", 0)))
        headroom = max(0, int(usage.get("prompt_headroom_tokens", 0)))
        percent = max(0.0, min(100.0, float(usage.get("percent", 0.0))))
        self.context_lbl.setText(
            f"Context ~{self._fmt_tokens(used)} / {self._fmt_tokens(limit)} · {percent:.0f}%"
        )
        self.context_bar.setValue(int(round(percent * 10)))
        self._style_context_bar(percent)
        tip = (
            "Estimated prompt footprint: dynamic system/memory context, retained "
            "chat history, and enabled tool schemas. "
            f"{reserve:,} tokens reserved for response; ~{headroom:,} prompt tokens remain."
        )
        self.context_lbl.setToolTip(tip)
        self.context_bar.setToolTip(tip)

    def set_background_tasks(self, running_count: int):
        running_count = max(0, int(running_count or 0))
        if running_count:
            noun = "task" if running_count == 1 else "tasks"
            self.task_lbl.setText(f"● {running_count} background {noun} running")
            self.task_lbl.setVisible(True)
        else:
            self.task_lbl.clear()
            self.task_lbl.setVisible(False)

    def set_emergency_state(self, latched: bool, rearm_ready: bool, tooltip: str = ""):
        """3A.2 Part E/J — the one persistent OH SHIT control, always
        visible regardless of the current main panel. Three presentations:
        unlatched (red, offers to latch), latched-draining (obvious but not
        clickable-to-rearm), latched-safe (offers the deliberate re-arm
        confirmation). A second click while draining is handled by the
        caller, not here — this method only ever renders state."""
        if not latched:
            self.emergency_btn.setText("⛔ OH SHIT")
            self.emergency_btn.setStyleSheet(f"""
                QPushButton {{
                    background: {COLORS['danger']}; color: white; border: none;
                    border-radius: 4px; padding: 1px 10px; font-size: 11px; font-weight: bold;
                }}
                QPushButton:hover {{ background: #ff6b7a; }}
            """)
        elif rearm_ready:
            self.emergency_btn.setText("🔒 RE-ARM")
            self.emergency_btn.setStyleSheet(f"""
                QPushButton {{
                    background: {COLORS['warning']}; color: black; border: none;
                    border-radius: 4px; padding: 1px 10px; font-size: 11px; font-weight: bold;
                }}
                QPushButton:hover {{ background: #ffb733; }}
            """)
        else:
            self.emergency_btn.setText("🔒 STOPPED")
            self.emergency_btn.setStyleSheet(f"""
                QPushButton {{
                    background: {COLORS['bg_input']}; color: {COLORS['danger']};
                    border: 1px solid {COLORS['danger']}; border-radius: 4px;
                    padding: 1px 10px; font-size: 11px; font-weight: bold;
                }}
            """)
        self.emergency_btn.setToolTip(tooltip)

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
        # VISION-MULTI-IMAGE-01: ordered list of pending image attachments,
        # each {"id", "path", "filename", "b64", "media_type"} — attachment
        # order is preserved end to end by appending here and iterating in
        # order when the turn is built (see _on_user_message()). "id" is a
        # monotonic counter (_next_image_id below), not filename or list
        # index, so an individual thumbnail can be removed (duplicate
        # filenames from different directories stay distinguishable, and
        # removing one doesn't renumber the rest).
        self._pending_images = []
        self._next_image_id = 1
        self._pending_audio = None
        self._current_chat_id = None
        # CONTEXT-LIFECYCLE-A5I: chat-scoped generation counter, exactly
        # parallel to ui/review_controller.py's ReviewController._generation
        # -- owned here (not by ContextManager, not process-wide), bumped at
        # every point that invalidates an in-flight deliberate_reconstruct()
        # preparation. See _chat_switch_admitted()/current_chat_id()/
        # current_generation()/bump()/live_ctx() below for the generation-
        # owner protocol core.context_transaction.deliberate_reconstruct()
        # consumes.
        self._context_generation = ContextGeneration()
        self._prefs = persistence.load()
        self._last_activity = time.time()
        # DREAM-LIFECYCLE-01: two orthogonal lifecycle states replace the old
        # conflated _dream_fired_this_idle boolean. in-flight = worker-lifetime
        # single-flight; consumed = idle-window admission state.
        self._dream_sweep_in_flight = False
        self._dream_idle_consumed = False
        self._operator_turn_started_at = None
        self._operator_last_progress_at = None
        self._operator_phase = "idle"
        self._operator_current_tool = None
        self._btw_task_ids = {}  # task_id -> {question, started_at}; never injected into main context
        self._manual_compaction_thread = None
        self._manual_compaction_cancel = None
        self._manual_compaction_started_at = None
        self._dream_timer = QTimer(self)
        self._dream_timer.timeout.connect(self._check_dream_idle)
        self._dream_timer.start(60_000)  # check once a minute — cheap, no need to be tighter


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
        self._telemetry_timer = QTimer(self)
        self._telemetry_timer.timeout.connect(self._refresh_operator_telemetry)
        self._telemetry_timer.start(1000)
        QTimer.singleShot(0, lambda: self._refresh_operator_telemetry(refresh_context=True))
        self._refresh_emergency_control()

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
        # CODING-08A4: invalidate the review controller's generation so any
        # in-flight capture/retrieval worker's eventual result becomes
        # unpublishable -- never killed unsafely, just discarded.
        self.review_panel.shutdown()
        # CONTEXT-LIFECYCLE-A5I: mirrors the same discard-in-flight-work
        # pattern for any in-flight deliberate_reconstruct() preparation.
        self._context_generation.bump()
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
            # Try to restore last chat -- CHAT-STARTUP-RESTORE-01: a
            # persisted last_chat_id can go stale (e.g. another Lumina
            # process sharing this same DATA_DIR/database wrote a
            # different chat's id after this one, or the chat was since
            # deleted) without ever being validated here. An unvalidated
            # stale id used to reach _load_chat() unchanged: it would
            # reconstruct zero rows for a chat_id that plain doesn't
            # exist, leaving the message pane blank while the chat_combo
            # -- finding no item matching that id -- fell back to Qt's own
            # default (index 0, chats[0], the same "most recent" row this
            # fallback already intends), a visibly mismatched restore.
            # Validating against the same `chats` list already fetched
            # above keeps the intended recency semantics (chats[0], not
            # lowest-id/oldest) as the ONE fallback path, whether
            # last_chat_id is missing or merely stale.
            last_id = self._prefs.get("last_chat_id")
            valid_ids = {c["id"] for c in chats}
            target_id = last_id if last_id in valid_ids else chats[0]["id"]
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
        if self.worker and self.worker.isRunning():
            self.worker.quit()
            self.worker.wait()
            self.worker = None
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
            tts=self.agent.tts,
            tts_speech_allowed=lambda: not getattr(
                self.agent, "_persona_speech_suppressed", False
            ),
        )
        self.settings_panel = SettingsPanel(self.agent, COLORS)
        self.settings_panel.setVisible(False)
        self.settings_panel.persona_applied.connect(self._on_persona_applied)
        self.settings_panel.backend_connection_changed.connect(
            self._on_backend_connection_changed
        )

        self.review_panel = ReviewPanel(self.agent, COLORS)
        self.review_panel.setVisible(False)

        main.addWidget(self.chat_widget, 1)
        main.addWidget(self.settings_panel, 1)
        main.addWidget(self.review_panel, 1)

        self.status_bar = StatusBar()
        main.addWidget(self.status_bar)

        container = QWidget()
        container.setLayout(main)
        root.addWidget(container, 1)

        self.chat_widget.message_submitted.connect(self._on_user_message)
        self.chat_widget.files_dropped.connect(self._on_files_dropped)
        self.chat_widget.audio_preview_cancelled.connect(lambda: setattr(self, '_pending_audio', None))
        self.chat_widget.image_preview_removed.connect(self._on_image_preview_removed)
        self.chat_widget.attach_files_requested.connect(self._on_attach_files_requested)
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
        self.btn_review_nav = _nav_btn("🔍", "Review")
        self.btn_settings_nav = _nav_btn("⚙", "Settings")

        self.btn_chat_nav.clicked.connect(lambda: self._show_panel("chat"))
        self.btn_review_nav.clicked.connect(lambda: self._show_panel("review"))
        self.btn_settings_nav.clicked.connect(lambda: self._show_panel("settings"))

        layout.addWidget(self.btn_chat_nav)
        layout.addWidget(self.btn_review_nav)
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

        ver = QLabel("v0.2.7-beta.2")
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
        # PREFS-STALE-WRITE-01: self._prefs is a startup-era READ cache.
        # Never publish it whole — fresh-load, mutate the owned key, atomic
        # save, or unrelated newer settings get reverted.
        self._prefs["user_avatar_path"] = path
        persistence.update({"user_avatar_path": path})
        self.chat_widget.user_avatar_path = path
    def _pick_avatar(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select Lumina Avatar", "", "Images (*.png *.jpg *.jpeg *.webp)")
        if path:
            self._prefs["avatar_path"] = path  # read-cache only
            persistence.update({"avatar_path": path})
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
        self.signals.commentary.connect(self._on_commentary)
        self.signals.final_stream_timing.connect(self._on_final_stream_timing)
        self.signals.final_ttft.connect(self._on_final_ttft)
        self.signals.time_to_first_answer.connect(self._on_time_to_first_answer)
        self.signals.finished.connect(self._on_finished)
        self.signals.error.connect(self._on_error)
        self.signals.cancelled.connect(self._on_cancelled)
        self.signals.manual_compaction_finished.connect(self._on_manual_compaction_finished)
        self.chat_widget.mic_pressed.connect(self._on_mic_pressed)
        self.status_bar.emergency_btn.clicked.connect(self._on_emergency_button_clicked)

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

    def _chat_switch_admitted(self) -> bool:
        """CONTEXT-LIFECYCLE-A5I / D4: fail-closed admission check for
        _new_chat()/_clear_chat()/_load_chat(). Before this method existed,
        all three called ctx.clear()/reassigned ctx.history with zero check
        of whether the AgentWorker thread was mid-turn -- the exact race
        CONTEXT-LIFECYCLE-A5D's source-vet documents (D4): a worker's next
        self.history.append() (a fresh attribute lookup every call) would
        silently land in whichever chat's list .history now points to, once
        one of these methods reassigned it out from under the running turn.
        _on_user_message()/_command_compact() already guard the opposite
        direction (a new turn/compaction while one of these is conceptually
        "in progress"); this closes the direction that was actually open.
        Must fail closed for a programmatic caller too, not merely disable
        a Qt widget -- checked here, inside the method itself, every time."""
        if self.worker is not None and self.worker.isRunning():
            self.chat_widget.add_operator_message(
                "Cannot switch, start, or clear a chat while the main turn is "
                "still running. Use /status or /stop, then try again."
            )
            return False
        return True

    # ── CONTEXT-LIFECYCLE-A5I: generation-owner protocol consumed by
    # core.context_transaction.deliberate_reconstruct(). See that module's
    # docstring for the exact contract -- LuminaWindow implements it
    # directly so a caller can pass `self` as generation_owner with no
    # separate adapter object. ──
    def current_chat_id(self) -> int:
        return self._current_chat_id

    def current_generation(self) -> int:
        return self._context_generation.current()

    def bump(self) -> int:
        return self._context_generation.bump()

    def live_ctx(self):
        return self.agent.ctx

    def _new_chat(self):
        if not self._chat_switch_admitted():
            return
        self._context_generation.bump()
        self._current_chat_id = create_chat()
        self._prefs["last_chat_id"] = self._current_chat_id  # read-cache only
        persistence.update({"last_chat_id": self._current_chat_id})
        self.agent.ctx.clear()
        # Re-apply active persona so new chat inherits identity
        path = self.persona_combo.currentData()
        if path:
            self._load_persona_from_file(path)
        self.chat_widget.clear_messages()
        self.chat_widget.add_system_message(f"New chat — {config.AGENT_NAME} is ready.")
        self._refresh_chat_list()

    def _clear_chat(self):
        if not self._chat_switch_admitted():
            return
        self._context_generation.bump()
        self.agent.ctx.clear()
        self.chat_widget.clear_messages()
        self.chat_widget.add_system_message("Chat cleared.")

    def _load_chat(self, chat_id: int):
        if not self._chat_switch_admitted():
            return
        self._context_generation.bump()
        self._current_chat_id = chat_id
        self._prefs["last_chat_id"] = chat_id  # read-cache only
        persistence.update({"last_chat_id": chat_id})

        # CONTEXT-LIFECYCLE-A2: which durable rows re-enter active context
        # is decided entirely by the neutral kernel (core/context_
        # reconstruction.py), not by a loop here -- this method now only
        # owns chat selection, visible-transcript rendering, and other
        # genuinely UI-specific state. context_skip is still resolved here
        # (not inside the kernel's default path) so a checkpoint read
        # failure degrades to "restore everything" instead of crashing
        # chat load -- the exact pre-A2 behavior.
        result = reconstruct_chat_context(chat_id, context_skip=resolve_context_skip(chat_id))

        self.agent.ctx.clear()
        self.agent.ctx.history = list(result.messages)

        self.chat_widget.clear_messages()
        for row in result.rows:
            role = row["role"]
            content = row["content"] or ""
            if not content:
                continue
            if role == "user":
                # UI-CHAT-SCROLL-01: restore inserts never scroll; one
                # explicit layout-settled positioning after the loop owns
                # the final viewport.
                self.chat_widget.add_user_message(content, mode="none")
            elif role == "assistant":
                # TOKS-STREAM-TIMING-01 -- restored=True: this bubble never
                # ran a live turn, so it must never fabricate a turn/stream
                # elapsed reading from "now" (see LiveResponseBubble.
                # __init__'s own docstring). No per-message turn/stream
                # telemetry is persisted today, so every restored bubble
                # honestly shows both as unavailable -- not a regression,
                # the same "never measured" state these rows have always
                # been in, just no longer misreported as ~0.0s / 0.0 tok/s.
                bubble = self.chat_widget.create_live_bubble(restored=True)
                bubble._response_text = content
                bubble.finalize()
        # UI-CHAT-SCROLL-01: one intentional layout-settled positioning for
        # the whole restore -- history reopens at the latest turn without
        # dozens of per-message scroll timers racing each other.
        self.chat_widget.scroll_to_bottom_now()
        self._refresh_chat_list()

    def _on_chat_selected(self, idx: int):
        if idx < 0:
            return
        chat_id = self.chat_combo.itemData(idx)
        if chat_id and chat_id != self._current_chat_id:
            self._load_chat(chat_id)
            
    def _on_persona_selected(self, idx: int):
        print(f"[PERSONA] combo selected idx={idx} path={self.persona_combo.itemData(idx)}", flush=True)
        path = self.persona_combo.itemData(idx)
        if path:
            self._prefs["last_persona"] = path  # read-cache only
            persistence.update({"last_persona": path})
            self._load_persona_from_file(path)
        else:
            self.agent.clear_persona_speech_suppression()

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
        """Fire-and-forget: generate a 3-5 word title for a new chat.

        S41 / F-62 real fix: this used to be its own requests.post() straight
        to config.LLM_BACKEND_URL with a hardcoded timeout=60 (dreaming.py had
        the exact same pattern with timeout=30 — see that file's history) and
        no auth headers at all, meaning it silently only ever worked against
        a local OpenAI-compatible server. self.agent.llm is whatever backend
        is actually active right now — local or cloud — so this now follows
        backend switches correctly instead of hardcoding the URL directly.
        """

        def _run():
            print(f"[AUTO-NAME] thread started", flush=True)

            prompt = (
                f"Generate a 3-5 word title for this conversation:\n\n"
                f"User: {user_msg[:200]}\n"
                f"Assistant: {assistant_msg[:100]}\n\n"
                f"Reply with the title only. No explanation."
            )

            raw = self.agent.llm.complete_utility(
                prompt=prompt, prefill="TITLE:", max_tokens=30, temperature=0.3,
            )
            print(f"[AUTO-NAME] cleaned result: {repr(raw)}", flush=True)
            if not raw:
                print("[AUTO-NAME] complete_utility returned nothing — skipping", flush=True)
                return

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
        self.btn_review_nav.setChecked(panel == "review")
        self.btn_settings_nav.setChecked(panel == "settings")
        self.chat_widget.setVisible(panel == "chat")
        self.review_panel.setVisible(panel == "review")
        self.settings_panel.setVisible(panel == "settings")
        self.header_title.setText(
            config.AGENT_NAME if panel == "chat"
            else "Review" if panel == "review"
            else "Settings"
        )

    def _on_backend_connection_changed(self):
        """UI-TRUST-01B: Settings live-applied a backend/model change.
        Invalidate the previously displayed state immediately, then
        re-report truth from the live agent object -- never from whatever
        Settings merely has typed into its fields."""
        self.status_bar.set_checking()
        self._check_connection()

    def _check_connection(self):
        """Display the live agent's actual backend health truthfully:
        green only for a passing check, red with the real reason on
        failure -- never a stale previous provider/model."""
        try:
            ok, msg = self.agent.llm.health_check()
        except Exception:
            self.status_bar.set_error("No backend connected — go to Settings to configure")
            return
        if ok:
            self.status_bar.set_connected(msg.replace("Connected — model: ", ""))
        else:
            self.status_bar.set_error(msg)

    def _tracked_operator_task_ids(self):
        """Union model-dispatched tasks with UI-only /btw sidequests."""
        return set(getattr(self.agent, "_background_task_ids", set())) | set(self._btw_task_ids)

    def _refresh_operator_telemetry(self, refresh_context: bool = False):
        """Refresh read-only context and desktop-owned background-task telemetry."""
        try:
            from core.task_queue import get_task_result
            running = 0
            for task_id in self._tracked_operator_task_ids():
                entry = get_task_result(task_id)
                if entry and entry.get("status") == "running":
                    running += 1
            self.status_bar.set_background_tasks(running)
            self._surface_btw_results(get_task_result)
        except Exception as e:
            print(f"[TELEMETRY] task status unavailable: {e}", flush=True)

        try:
            usage = self.agent.get_context_usage(
                chat_id=self._current_chat_id, refresh=refresh_context
            )
            self.status_bar.set_context_usage(usage)
        except Exception as e:
            print(f"[TELEMETRY] context usage unavailable: {e}", flush=True)

        try:
            self._refresh_emergency_control()
        except Exception as e:
            print(f"[TELEMETRY] emergency control refresh failed: {e}", flush=True)

    def _mark_operator_progress(self, phase: str, tool: str = None):
        self._operator_phase = phase
        self._operator_current_tool = tool
        self._operator_last_progress_at = time.time()

    # ── Emergency interlock (3A.2 Parts C/E/F/I/J) ─────────────────────────────

    def _trigger_emergency_stop(self, source: str):
        """The ONE local-control-plane OH SHIT activation path. Every
        activation route (GUI button, local /stop all) calls this and only
        this. Ordering is security-critical: emergency_stop.latch() must be
        the very first state change, before any bridge stop, task
        cancellation, dialog, or logging — see
        LUMINA_PATCH_3A2_OH_SHIT_CONTROL_SURFACE_SPEC.md Part C. Idempotent
        while already latched: latch() itself won't advance the epoch or
        create a second incident, and every step below is safe to repeat as
        a best-effort re-nudge."""
        already_latched = emergency_stop.is_latched()
        incident = emergency_stop.latch(source=source, reason="operator emergency stop")

        self._refresh_emergency_control()

        if self.worker is not None and hasattr(self.worker, "request_cancel"):
            self.worker.request_cancel()

        if self._manual_compaction_cancel is not None:
            self._manual_compaction_cancel.set()

        from core.task_queue import cancel_all_scheduled
        cancel_all_scheduled()

        from comms.telegram_bridge import request_stop_bridge
        request_stop_bridge()

        # CODING-04A1: any persistent managed process (engine only in this
        # slice -- no model-facing tool can create one yet) must be reached
        # by the same OH SHIT cascade, after the latch, same as every other
        # step here. emergency_kill_all() only requests termination; it does
        # not block waiting for it, and does not itself touch the latch.
        from core import process_manager
        process_manager.emergency_kill_all()

        if already_latched:
            s = self._emergency_snapshot_summary()
            self.chat_widget.add_operator_message(
                f"⛔ Emergency stop already latched (epoch {s['epoch']}) · still "
                f"draining · {s['active_executions']} execution(s) · "
                f"{s['active_tool_dispatches']} tool dispatch(es) · "
                f"Telegram {s['telegram_state']} · "
                f"re-arm: {'SAFE' if s['rearm_ready'] else 'BLOCKED'}"
            )
        else:
            self.chat_widget.add_operator_message(
                f"⛔ EMERGENCY STOP ACTIVE (epoch {incident['new_epoch']}) · all new "
                "execution authority revoked · in-flight work draining · new chat "
                "turns, /btw, and /compact are blocked until re-armed"
            )

        self._refresh_emergency_control()

    def _emergency_rearm_ready(self) -> bool:
        """3A.2 Part F — the kernel's own can_rearm() is necessary but not
        sufficient: the GUI has extra local control-plane state the kernel
        can't see (a QThread that hasn't yet acquired its execution lease,
        Telegram's own shutdown handshake)."""
        if not emergency_stop.can_rearm():
            return False
        if self.worker is not None and self.worker.isRunning():
            return False
        if self._manual_compaction_thread is not None:
            return False
        from comms.telegram_bridge import is_running as telegram_is_running
        if telegram_is_running():
            return False
        return True

    def _emergency_snapshot_summary(self) -> dict:
        """Shared, safe-only aggregate used by both the button tooltip and
        /status's emergency block — counts and booleans only, never the
        kernel's per-lease metadata (that's a later slice)."""
        snap = emergency_stop.snapshot()
        from comms.telegram_bridge import is_running as telegram_is_running
        return {
            "epoch": snap["epoch"],
            "active_executions": snap["active_execution_count"],
            "active_tool_dispatches": snap["active_tool_dispatch_count"],
            "worker_running": self.worker is not None and self.worker.isRunning(),
            "compaction_running": self._manual_compaction_thread is not None,
            "telegram_state": "stopping" if telegram_is_running() else "offline",
            "rearm_ready": self._emergency_rearm_ready(),
        }

    def _emergency_status_lines(self) -> list:
        s = self._emergency_snapshot_summary()
        return [
            "E-stop: ACTIVE",
            f"Epoch: {s['epoch']}",
            f"Active executions: {s['active_executions']}",
            f"Active tool dispatches: {s['active_tool_dispatches']}",
            f"Foreground worker: {'running' if s['worker_running'] else 'stopped'}",
            f"Manual compaction: {'running' if s['compaction_running'] else 'stopped'}",
            f"Telegram: {s['telegram_state']}",
            f"Re-arm: {'SAFE' if s['rearm_ready'] else 'BLOCKED'}",
        ]

    def _emergency_tooltip(self) -> str:
        s = self._emergency_snapshot_summary()
        parts = [f"Emergency stop active · epoch {s['epoch']}"]
        if s["active_executions"]:
            n = s["active_executions"]
            parts.append(f"{n} execution{'s' if n != 1 else ''} unwinding")
        if s["active_tool_dispatches"]:
            n = s["active_tool_dispatches"]
            parts.append(f"{n} tool dispatch{'es' if n != 1 else ''} unwinding")
        parts.append(f"Telegram {s['telegram_state']}")
        parts.append(f"re-arm: {'SAFE' if s['rearm_ready'] else 'BLOCKED'}")
        return " · ".join(parts)

    def _refresh_emergency_control(self):
        """3A.2 Part J — called both right after any emergency-relevant
        action and from the normal 1 Hz telemetry loop, so asynchronous
        draining (a tool finishing, Telegram actually stopping) becomes
        visible without another click."""
        latched = emergency_stop.is_latched()
        if not latched:
            self.status_bar.set_emergency_state(False, False, "")
            return
        rearm_ready = self._emergency_rearm_ready()
        self.status_bar.set_emergency_state(True, rearm_ready, self._emergency_tooltip())

    def _on_emergency_button_clicked(self):
        if not emergency_stop.is_latched():
            self._trigger_emergency_stop(source="gui_button")
            return
        if self._emergency_rearm_ready():
            self._confirm_and_rearm()
        else:
            # Second interaction while still draining is NOT a toggle and
            # never calls rearm_local() — repeat the same idempotent
            # activation, which surfaces an operator-only explanation of
            # what's still unwinding instead of a misleading dialog.
            self._trigger_emergency_stop(source="gui_button")

    def _confirm_and_rearm(self):
        reply = QMessageBox.question(
            self, "Re-arm Lumina",
            "Emergency stop is fully drained and safe to clear.\n\n"
            "Re-arming restores execution authority for NEW work only. It "
            "does not resurrect anything from before the stop, and "
            "Telegram stays off until you turn it back on manually in "
            "Settings.\n\nRe-arm now?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            emergency_stop.rearm_local()
        except emergency_stop.EmergencyStopError as e:
            self.chat_widget.add_operator_message(f"Re-arm failed: {e}")
            self._refresh_emergency_control()
            return
        self.chat_widget.add_operator_message(
            "🔓 Re-armed · execution authority restored for new work · "
            "Telegram remains off — re-enable it manually in Settings if desired."
        )
        self._refresh_emergency_control()

    def _dispatch_operator_command(self, command):
        """Single dispatch point for owner-facing slash commands."""
        if not command.known:
            self.chat_widget.add_operator_message(
                f"Unknown command /{command.name or '?'}. {command_help()}"
            )
            return
        if command.name == "status":
            self._command_status(command.argument)
        elif command.name == "btw":
            self._command_btw(command.argument)
        elif command.name == "compact":
            self._command_compact(command.argument)
        elif command.name == "stop":
            self._command_stop(command.argument)

    def _command_status(self, argument: str):
        if argument:
            self.chat_widget.add_operator_message("/status takes no arguments.")
            return

        now = time.time()
        lines = []

        # 3A.2 Part I — /status must remain fully usable while latched (it's
        # local/read-only), but prepend a compact emergency block so an
        # operator checking status while latched sees the real picture
        # first. Never dumps prompts/tool args/results/secrets — aggregate
        # counts and booleans only.
        if emergency_stop.is_latched():
            lines.extend(self._emergency_status_lines())
            lines.append("")

        if self.worker is not None and self.worker.isRunning():
            started = self._operator_turn_started_at or now
            last = self._operator_last_progress_at or started
            phase = self._operator_phase or "processing"
            stop_requested = bool(
                hasattr(self.worker, "cancel_requested") and self.worker.cancel_requested()
            )
            if stop_requested and self._operator_current_tool:
                phase = f"stop requested · waiting for tool: {self._operator_current_tool}"
            elif stop_requested:
                phase = f"stop requested · waiting for safe boundary ({phase})"
            elif self._operator_current_tool:
                phase = f"tool: {self._operator_current_tool}"
            lines.append(f"Foreground: running {format_duration(now - started)} · {phase}")
            age = now - last
            lines.append(f"Last meaningful progress: {format_duration(age)} ago")
            if age >= 60:
                lines.append(f"⚠ No meaningful progress for {format_duration(age)}; task may simply be blocked in a long tool/provider call.")
        else:
            lines.append("Foreground: idle")

        compact_thread = self._manual_compaction_thread
        if compact_thread is not None:
            started = self._manual_compaction_started_at or now
            state = "running" if compact_thread.is_alive() else "finalizing"
            lines.append(f"Compaction: {state} {format_duration(now - started)}")

        try:
            from core.task_queue import get_task_result
            counts = {"running": 0, "scheduled": 0}
            for task_id in self._tracked_operator_task_ids():
                entry = get_task_result(task_id)
                status = entry.get("status") if entry else None
                if status in counts:
                    counts[status] += 1
            lines.append(
                f"Background: {counts['running']} running · {counts['scheduled']} scheduled"
            )
        except Exception:
            lines.append("Background: status unavailable")

        try:
            usage = self.agent.get_context_usage(chat_id=self._current_chat_id)
            lines.append(
                f"Context: ~{format_tokens(usage['used_tokens'])} / "
                f"{format_tokens(usage['max_tokens'])} · {usage['percent']:.0f}%"
            )
        except Exception:
            lines.append("Context: status unavailable")

        self.chat_widget.add_operator_message("\n".join(lines))

    def _command_compact(self, argument: str):
        if argument:
            self.chat_widget.add_operator_message("/compact takes no arguments.")
            return
        if emergency_stop.is_latched():
            self.chat_widget.add_operator_message(
                "/compact is blocked while emergency stop is active."
            )
            return
        if self.worker is not None and self.worker.isRunning():
            self.chat_widget.add_operator_message(
                "/compact is idle-only; wait for the foreground turn to finish."
            )
            return
        if not self._current_chat_id:
            self.chat_widget.add_operator_message("/compact requires an active chat session.")
            return
        if getattr(self.agent.ctx, "_compacting", False):
            self.chat_widget.add_operator_message(
                "Automatic context compaction is already running; try /compact again when it finishes."
            )
            return
        if self._manual_compaction_thread is not None:
            self.chat_widget.add_operator_message("Manual context compaction is already running or finalizing.")
            return

        history_snapshot = list(self.agent.ctx.history)
        cut = compaction_cut_index(history_snapshot)
        if cut is None:
            self.chat_widget.add_operator_message(
                "/compact: nothing to compact yet — the newest two user turns are kept live."
            )
            return

        chat_id = self._current_chat_id
        cancel_event = threading.Event()
        self._manual_compaction_cancel = cancel_event
        self._manual_compaction_started_at = time.time()
        self.chat_widget.add_operator_message(
            "/compact started · preserving the newest two user turns · persisted transcript remains untouched"
        )

        # 3A.2 Part H — epoch captured BEFORE the thread starts. The
        # existing 2B2 cancel_event above remains the authoritative
        # persistence boundary (run_manual_compaction() is untouched and
        # still owns "did we write anything yet"); this lease only adds
        # re-arm visibility (can_rearm() stays False while /compact is
        # genuinely running) and refuses to even start against an
        # already-stale/latched epoch.
        epoch = emergency_stop.current_epoch()

        def _run():
            try:
                with emergency_stop.execution_scope(
                    kind="manual_compaction", label=str(chat_id), expected_epoch=epoch,
                ):
                    result = run_manual_compaction(
                        history_snapshot, chat_id=chat_id, cancel_event=cancel_event
                    )
            except emergency_stop.EmergencyStopError:
                result = {"status": "cancelled", "chat_id": chat_id}
            except Exception as e:
                result = {
                    "status": "error", "chat_id": chat_id,
                    "error": f"Unexpected compaction failure: {e}",
                }
            self.signals.manual_compaction_finished.emit(result)

        self._manual_compaction_thread = threading.Thread(target=_run, daemon=True)
        self._manual_compaction_thread.start()

    def _command_stop(self, argument: str):
        argument = (argument or "").strip().lower()
        if argument and argument != "all":
            self.chat_widget.add_operator_message(
                "/stop takes no arguments, or 'all' for emergency stop."
            )
            return

        if argument == "all":
            self._trigger_emergency_stop(source="operator_command")
            return

        if emergency_stop.is_latched():
            self.chat_widget.add_operator_message(
                "Emergency stop is already controlling active work. Use the "
                "cockpit's emergency control to re-arm once it's safe."
            )
            return

        if self.worker is not None and self.worker.isRunning():
            if hasattr(self.worker, "cancel_requested") and self.worker.cancel_requested():
                self.chat_widget.add_operator_message(
                    "/stop is already requested · waiting for the current provider/tool boundary."
                )
                return
            requested = bool(
                hasattr(self.worker, "request_cancel") and self.worker.request_cancel()
            )
            if not requested:
                self.chat_widget.add_operator_message(
                    "/stop could not signal this foreground worker; it is still running."
                )
                return
            self.status_lbl.setText("stop requested...")
            self.chat_widget.add_operator_message(
                "/stop requested · foreground turn only · waiting for the current "
                "provider/tool boundary · background tasks untouched"
            )
            return

        compact_thread = self._manual_compaction_thread
        if compact_thread is not None:
            if not compact_thread.is_alive():
                self.chat_widget.add_operator_message(
                    "/compact is already finalizing; no safe cancellation boundary remains."
                )
                return
            cancel_event = self._manual_compaction_cancel
            if cancel_event is None:
                self.chat_widget.add_operator_message("/compact has no active cancellation handle.")
                return
            if cancel_event.is_set():
                self.chat_widget.add_operator_message("/stop is already requested for /compact.")
                return
            cancel_event.set()
            self.chat_widget.add_operator_message(
                "/stop requested for /compact · it will stop before the next safe persistent-write boundary."
            )
            return

        self.chat_widget.add_operator_message(
            "/stop: no foreground turn or manual compaction is currently running."
        )

    def _command_btw(self, question: str):
        question = (question or "").strip()
        if emergency_stop.is_latched():
            self.chat_widget.add_operator_message(
                "/btw is blocked while emergency stop is active."
            )
            return
        if self._manual_compaction_thread is not None:
            self.chat_widget.add_operator_message(
                "/btw waits while /compact is using the utility backend; try again when compaction finishes."
            )
            return
        if not question:
            self.chat_widget.add_operator_message("Usage: /btw <side question>")
            return
        if not getattr(config, "BACKGROUND_TASKS_ENABLED", False) or not getattr(config, "SUBAGENTS_ENABLED", False):
            self.chat_widget.add_operator_message(
                "/btw requires both Background Tasks and Subagents to be enabled in Settings."
            )
            return

        try:
            from tools.tasks import run_background_subagent
            persona = None
            current_persona = getattr(self.agent, "current_persona", None)
            if isinstance(current_persona, dict):
                persona = current_persona.get("name")
            result = run_background_subagent(question, persona=persona)
            task_id = result["task_id"]
            # Deliberately DO NOT add this to agent._background_task_ids.
            # /btw is UI-owned and must never be injected into main context by
            # LuminaAgent.chat()'s normal background-completion mechanism.
            self._btw_task_ids[task_id] = {"question": question, "started_at": time.time()}
            self.chat_widget.add_operator_message(
                f"/btw sidequest started · {task_id[:8]} · main conversation untouched"
            )
            self._refresh_operator_telemetry()
        except Exception as e:
            self.chat_widget.add_operator_message(f"/btw failed to start: {e}")

    def _surface_btw_results(self, get_task_result):
        """Display /btw results independently; never feed them into agent history."""
        for task_id, meta in list(self._btw_task_ids.items()):
            entry = get_task_result(task_id)
            if entry is None:
                self.chat_widget.add_operator_message(
                    f"/btw {task_id[:8]} result expired before display."
                )
                self._btw_task_ids.pop(task_id, None)
                continue
            if entry.get("status") not in ("success", "error", "cancelled"):
                continue
            elapsed = format_duration(time.time() - meta["started_at"])
            result_text = unwrap_background_result(entry)
            question = meta["question"]
            if len(question) > 100:
                question = question[:97] + "..."
            self.chat_widget.add_operator_message(
                f"/btw finished · {task_id[:8]} · {elapsed}\n{question}\n\n{result_text}"
            )
            self._btw_task_ids.pop(task_id, None)

    def _on_manual_compaction_finished(self, result: dict):
        self._manual_compaction_thread = None
        self._manual_compaction_cancel = None
        self._manual_compaction_started_at = None

        status = result.get("status")
        if status == "cancelled":
            self.chat_widget.add_operator_message(
                "/compact cancelled before any persistent writes were made."
            )
            return
        if status == "nothing_to_compact":
            self.chat_widget.add_operator_message("/compact: nothing to compact.")
            return
        if status != "success":
            detail = result.get("error") or "unknown error"
            self.chat_widget.add_operator_message(f"/compact failed: {detail} · live context was not pruned")
            return

        snapshot = result.get("history_snapshot") or []
        applied_live = False
        if self._current_chat_id == result.get("chat_id") and self.agent.ctx.history == snapshot:
            self.agent.ctx.history = list(result.get("retained_history") or [])
            self.agent.ctx._last_usage_snapshot = None
            # CONTEXT-LIFECYCLE-A5I: a live-applied compaction result moves
            # the durable spine's context_skip, exactly like a chat switch --
            # invalidates any in-flight deliberate_reconstruct() preparation.
            self._context_generation.bump()
            applied_live = True

        self._refresh_operator_telemetry(refresh_context=True)
        compacted_messages = result.get("compacted_messages", 0)
        compacted_tokens = result.get("compacted_tokens", 0)
        if applied_live:
            try:
                usage = self.agent.get_context_usage(
                    chat_id=self._current_chat_id, refresh=True
                )
                context_now = (
                    f" · context now ~{format_tokens(usage['used_tokens'])} / "
                    f"{format_tokens(usage['max_tokens'])}"
                )
            except Exception:
                context_now = ""
            self.chat_widget.add_operator_message(
                f"/compact finished · summarized {compacted_messages} live messages "
                f"(~{format_tokens(compacted_tokens)} tokens) · newest two user turns kept live"
                f"{context_now} · full transcript unchanged"
            )
        else:
            self.chat_widget.add_operator_message(
                "/compact finished and checkpointed safely; the source chat changed while it ran, "
                "so its compacted context will take effect when that chat is reopened. Full transcript unchanged."
            )

    # ── Dreaming ────────────────────────────────────────────────────────────────

    def _check_dream_idle(self):
        # DREAM-LIFECYCLE-01: _dream_sweep_in_flight (worker-lifetime) and
        # _dream_idle_consumed (idle-window admission) are orthogonal. An
        # ineligible/failed/cancelled probe consumes nothing — eligibility
        # can legitimately change mid-window (Settings writes
        # DREAM_SWEEP_ENABLED live, transient provider failures recover,
        # emergency stops get released) — while only a completed sweep
        # consumes the window, exactly once. Single-flight is enforced by
        # the in-flight flag, never by consumption.
        if self._dream_sweep_in_flight or self._dream_idle_consumed \
                or not self._current_chat_id:
            return
        if self.worker is not None and self.worker.isRunning():
            # Agent is actively mid-turn — _last_activity only updates when a
            # NEW message is sent, so a long-running turn (retries, a slow
            # tool call, anything) is otherwise invisible to this timer.
            # Without this check, a dream sweep can fire while the model is
            # still busy generating, queue behind the live turn on
            # llama-server's single inference slot, and hit a read timeout.
            # Consume nothing here — let it check again next tick once the
            # turn actually finishes.
            return
        idle_minutes = getattr(config, "DREAM_IDLE_MINUTES", 20)
        if time.time() - self._last_activity >= idle_minutes * 60:
            self._dream_sweep_in_flight = True
            chat_id = self._current_chat_id
            # 3A.2 Part H — epoch captured here, on the Qt main thread,
            # BEFORE the daemon thread is created. dreaming.on_session_idle()
            # runs the whole sweep inside an execution_scope pinned to this
            # exact epoch, so a stale/latched epoch admits no work at all.
            epoch = emergency_stop.current_epoch()
            try:
                threading.Thread(
                    target=self._run_dream_sweep, args=(chat_id, epoch),
                    daemon=True,
                ).start()
            except BaseException:
                # Never stick lifecycle state on a spawn failure.
                self._dream_sweep_in_flight = False
                raise

    def _run_dream_sweep(self, chat_id: int, expected_epoch):
        """Daemon-thread body for one Dream sweep (DREAM-LIFECYCLE-01).

        Outcome mapping: only DREAM_COMPLETED consumes the idle window —
        ineligible/failed/cancelled probes leave it admit-able for the next
        timer tick. The in-flight flag is cleared in finally regardless of
        outcome or exception, so no worker failure can permanently stick
        lifecycle state (a hung provider call is bounded by the backend's
        own request timeout). Cross-thread flag writes are plain attribute
        assignments (GIL-atomic); ticks serialize on the Qt thread, so no
        duplicate-admission race exists."""
        try:
            outcome = dreaming.on_session_idle(chat_id, expected_epoch=expected_epoch)
            if outcome == dreaming.DREAM_COMPLETED:
                self._dream_idle_consumed = True
        except Exception as e:
            # A dead sweep thread must never masquerade as a completed one;
            # print for terminal observability, leave the window admit-able.
            print(f"[DREAM] sweep error: {e}", flush=True)
        finally:
            self._dream_sweep_in_flight = False

    def _reset_dream_window_state(self):
        """User-turn reset (DREAM-LIFECYCLE-01): clears idle-window-scoped
        Dream admission state only. _dream_sweep_in_flight is
        worker-lifetime-scoped and is intentionally NOT touched here — an
        in-flight sweep stays single-flight across the turn boundary until
        the worker itself finishes."""
        self._dream_idle_consumed = False

    # ── Compaction ─────────────────────────────────────────────────────────────

    def _maybe_compact(self, chat_id: int):
        """Fire-and-forget: summarize whatever build_messages()'s trim loop
        has captured into self.agent.ctx._pending_compaction and write it to
        the same nightstand/{chat_id}/L2 rolling closet on_session_idle()
        already writes to (S57 correction — NOT wing="sessions"; keeping
        auto-writes in one wing means palace_store()'s room-scoped rolling
        merge can never accidentally combine with curated memory).

        `not chat_id` guard mirrors on_session_idle()'s own `not chat_id`
        check in core/dreaming.py — same failure mode (room=str(chat_id)
        would write to a literal "None" room if this ever fired before a
        chat has a real DB-assigned id), same fix.
        """
        if not getattr(config, "CONTEXT_COMPACTION_ENABLED", False) or not chat_id:
            return
        if self.agent.ctx._compacting:
            return
        if self.agent.ctx.pending_compaction_tokens() < config.CONTEXT_COMPACTION_BATCH_TOKENS:
            return

        # 3A.2 Part H, R9-corrective — epoch captured here, before the thread
        # starts. Scope ADMISSION failure (stale/latched before the thread is
        # even let in) never calls take_pending_compaction() at all -- the
        # batch sits untouched in ctx._pending_compaction for the next
        # _maybe_compact() to pick back up, same as any other skipped
        # attempt; nothing to restore.
        #
        # Once the batch IS taken, this is transactional: any exit before a
        # successful Palace write -- a latch landing while the summarizer is
        # blocked, an empty/failed summary, or a Palace write exception --
        # restores the exact batch to ctx._pending_compaction via
        # restore_pending_compaction() (core/context.py) rather than losing
        # it. Only a confirmed Palace write marks the batch committed.
        self.agent.ctx._compacting = True
        epoch = emergency_stop.current_epoch()

        def _run():
            batch = []
            committed = False
            try:
                with emergency_stop.execution_scope(
                    kind="auto_compaction", label=str(chat_id), expected_epoch=epoch,
                ):
                    batch = self.agent.ctx.take_pending_compaction()
                    raw_text = "\n".join(f"{m.get('role')}: {m.get('content')}" for m in batch if m.get("content"))
                    from core.dreaming import run_summarization_call, COMPACTION_PROMPT
                    summary = run_summarization_call(raw_text, prompt=COMPACTION_PROMPT, max_tokens=300)
                    if not summary:
                        return
                    if not emergency_stop.execution_permitted(epoch):
                        return
                    from tools.palace import palace_store
                    palace_store(
                        content=summary, wing="nightstand", room=str(chat_id),
                        layer=2, tags=["auto-compaction", f"session:{chat_id}"],
                    )
                    committed = True
            except emergency_stop.EmergencyStopError:
                pass  # stale/latched before admission -- no summarizer call, no write
            finally:
                if batch and not committed:
                    self.agent.ctx.restore_pending_compaction(batch)
                self.agent.ctx._compacting = False

        threading.Thread(target=_run, daemon=True).start()

    # ── Message handling ───────────────────────────────────────────────────────

    def _on_user_message(self, text: str):
        if not text.strip():
            return

        command = parse_operator_command(text)
        if command is not None:
            self._dispatch_operator_command(command)
            return

        if emergency_stop.is_latched():
            self.chat_widget.add_operator_message(
                "⛔ Emergency stop is active — new turns are blocked. Use /status, "
                "or re-arm from the cockpit once it's safe. Your message was not sent."
            )
            # Same restore-on-next-tick treatment as every other rejected-turn
            # path below, and pending image/audio preview state is left
            # completely untouched by returning here.
            QTimer.singleShot(0, lambda text=text: self.chat_widget.input.setPlainText(text))
            return

        if self._manual_compaction_thread is not None:
            self.chat_widget.add_operator_message(
                "Manual context compaction is still running or finalizing. Use /status; your message was not sent."
            )
            QTimer.singleShot(0, lambda text=text: self.chat_widget.input.setPlainText(text))
            return

        if self.worker is not None and self.worker.isRunning():
            self.chat_widget.add_operator_message(
                "Main turn is still running. Use /status, /btw <question>, or /stop; your message was not sent."
            )
            # SmartInput clears immediately after emitting submit. Restore on
            # the next event-loop tick so an accidental normal message isn't lost.
            QTimer.singleShot(0, lambda text=text: self.chat_widget.input.setPlainText(text))
            return
        self.worker = None
        
        self._last_activity = time.time()
        self._reset_dream_window_state()
        self._operator_turn_started_at = time.time()
        self._mark_operator_progress("processing")

        content = None
        display_text = text
        clean_text = re.sub(r'\[(image|audio): [^\]]+\]\n?', '', text).strip()

        if self._pending_images:
            # Image blocks first, in attachment order, then exactly one
            # trailing text block — same shape core/context.py's add_user()
            # and every backend translation path (LMStudioBackend/
            # OpenRouterBackend passthrough, GeminiBackend._parts_from_
            # content()) already handle generically per-block, so N images
            # here needed no changes below the UI layer.
            content = [
                {"type": "image_url", "image_url": {"url": f"data:{img['media_type']};base64,{img['b64']}"}}
                for img in self._pending_images
            ]
            default_caption = ("What do you see in this image?" if len(self._pending_images) == 1
                                else "What do you see in these images?")
            content.append({"type": "text", "text": clean_text if clean_text else default_caption})
            fnames = [img["filename"] for img in self._pending_images]
            self._pending_images = []
            self.chat_widget.clear_image_previews()
            display_text = " ".join(f"[🖼 {fn}]" for fn in fnames) + (f"  {clean_text}" if clean_text else "")

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

        # UI-CHAT-SCROLL-01: a foreground send anchors the new turn -- the
        # submitted card's start stays visible with response space below,
        # instead of the old delayed teleport to absolute transcript bottom.
        self.chat_widget.add_user_message(display_text, mode="anchor")
        self.chat_widget.set_turn_running(True)
        self.status_lbl.setText("processing...")
        if self._current_chat_id:
            save_chat_message(self._current_chat_id, "user", display_text)
        self._live_bubble = self.chat_widget.create_live_bubble()
        # CONTEXT-LIFECYCLE-A5I: a new foreground turn invalidates any
        # in-flight deliberate_reconstruct() preparation for this chat.
        self._context_generation.bump()
        self.worker = AgentWorker(self.agent, content, self.signals, chat_id=self._current_chat_id)
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

        # Images are admitted as one atomic batch ahead of the per-file loop
        # below (VISION-MULTI-IMAGE-01) -- see _admit_images(): either every
        # image in this drop/pick makes it into _pending_images in order, or
        # none does. Non-image files keep the original per-file loop, which
        # was already independent per file.
        image_paths = [p for p in paths if os.path.splitext(p)[1].lower() in image_exts]
        if image_paths:
            parts.extend(self._admit_images(image_paths))

        for p in paths:
            ext = os.path.splitext(p)[1].lower()

            if ext in image_exts:
                continue  # handled atomically above

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

    def _admit_images(self, paths: list) -> list:
        """Validate every image in one attach/drop batch, then admit them to
        _pending_images atomically (VISION-MULTI-IMAGE-01): if any file in
        the batch fails validation, none of the batch is added -- never a
        silent partial subset. Pending images already accepted from an
        earlier, separate drop/pick are untouched either way (repeated
        selection appends, it never replaces).

        Returns the text placeholder lines to fold into the input box,
        whichever way the batch resolved -- same shape/wording the old
        single-image path already used, so a one-image drop is byte-
        identical to before.
        """
        accepted = []  # [{"path","filename","b64","media_type","pixmap"}], in order
        failures = []  # [(filename, error)], in order

        for p in paths:
            fname = os.path.basename(p)
            try:
                # 2026-08-15 fix: this used to load via bare QPixmap(p) with
                # no check that it actually worked. QPixmap(p) returns a
                # null (not raised!) pixmap on an unsupported/corrupt file
                # -- e.g. .webp support depends on an optional Qt image
                # plugin that isn't guaranteed present on every install --
                # and .save() on a null pixmap can "succeed" while writing
                # a tiny near-empty PNG. Net effect: no error anywhere, but
                # Gemini/the model gets fed garbage and the description
                # comes back nonsensical -- exactly the "gives me shit with
                # pictures" symptom, with nothing in the UI to explain why.
                # QImageReader + setAutoTransform also fixes a second, real
                # issue: bare QPixmap(p) does not reliably honor a JPEG's
                # EXIF orientation tag, so photos taken on a phone can be
                # sent sideways/upside-down with no visual indication.
                from PySide6.QtGui import QImageReader
                reader = QImageReader(p)
                reader.setAutoTransform(True)
                image = reader.read()
                if image.isNull():
                    raise ValueError(reader.errorString() or "unreadable/unsupported image file")

                # Resize before encoding — cap longest side at 512px
                if image.width() > 512 or image.height() > 512:
                    image = image.scaled(512, 512, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                pix_orig = QPixmap.fromImage(image)

                from PySide6.QtCore import QBuffer, QIODevice
                buf = QBuffer()
                buf.open(QIODevice.WriteOnly)
                if not pix_orig.save(buf, "PNG"):
                    raise ValueError("PNG re-encode failed")
                raw = bytes(buf.data())
                if len(raw) < 100:
                    # A well-formed PNG header alone is ~60+ bytes; anything
                    # this small is not a real image even though save()
                    # reported success -- same silent-garbage failure mode
                    # as the isNull() case above, caught defensively.
                    raise ValueError(f"encoded PNG suspiciously small ({len(raw)} bytes)")
                b64 = base64.b64encode(raw).decode('utf-8')
                accepted.append({
                    "path": p, "filename": fname, "b64": b64,
                    "media_type": "image/png",
                    "pixmap": pix_orig.scaledToHeight(72, Qt.SmoothTransformation),
                })
                print(f"[DROP] image validated: {fname} ({len(b64)} b64 chars)", flush=True)
            except Exception as e:
                failures.append((fname, e))
                print(f"[DROP] image load FAILED: {fname}: {e}", flush=True)

        if failures:
            parts = [f"[image:{fname}] (failed to load: {e})" for fname, e in failures]
            if accepted:
                parts += [f"[image:{a['filename']}] (not attached — rejected with the rest of this batch)"
                          for a in accepted]
            return parts

        parts = []
        for a in accepted:
            image_id = self._next_image_id
            self._next_image_id += 1
            self._pending_images.append({
                "id": image_id, "path": a["path"], "filename": a["filename"],
                "b64": a["b64"], "media_type": a["media_type"],
            })
            self.chat_widget.add_image_preview(a["pixmap"], a["filename"], image_id)
            parts.append(f"[image: {a['filename']}]")
        return parts

    def _on_image_preview_removed(self, image_id: int):
        """One thumbnail's ✕ was clicked (ChatWidget.image_preview_removed)
        -- drop just that pending image and its placeholder text, leaving
        every other pending image untouched."""
        removed = None
        kept = []
        for img in self._pending_images:
            if removed is None and img["id"] == image_id:
                removed = img
            else:
                kept.append(img)
        self._pending_images = kept
        if removed is not None:
            text = self.chat_widget.input.toPlainText()
            text = re.sub(r'\[image: ' + re.escape(removed["filename"]) + r'\]\n?', '', text).strip()
            self.chat_widget.input.setPlainText(text)

    def _on_attach_files_requested(self):
        """📎 button: a native multi-select file dialog feeding the same
        atomic admission path as drag-and-drop (_admit_images), so both
        entry points share one validation/ordering contract."""
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Attach Images", "", "Images (*.png *.jpg *.jpeg *.webp *.gif)"
        )
        if paths:
            self._on_files_dropped(paths)

    # ── Streaming signal handlers ──────────────────────────────────────────────

    def _on_tool_call(self, name: str, args: dict):
        self._mark_operator_progress("tool", tool=name)
        self.status_lbl.setText(f"⚙ {name}...")
        if self._live_bubble:
            self._live_bubble.add_tool_call(name, args)

    def _on_tool_result(self, name: str, result: str):
        self._mark_operator_progress("processing")
        self.status_lbl.setText("processing...")

    def _on_think_start(self, step: int):
        self._mark_operator_progress("thinking")
        self.status_lbl.setText(f"thinking (step {step})...")
        if self._live_bubble and config.SHOW_THINK_BLOCKS:
            self._live_bubble.open_think_block(step)

    def _on_think_chunk(self, chunk: str):
        self._mark_operator_progress("thinking")
        if self._live_bubble and config.SHOW_THINK_BLOCKS:
            self._live_bubble.append_think_token(chunk)
            

    def _on_think_end(self):
        self._mark_operator_progress("responding")
        self.status_lbl.setText("responding...")
        if self._live_bubble and config.SHOW_THINK_BLOCKS:
            self._live_bubble.close_think_block()

    def _on_response_chunk(self, chunk: str):
        self._mark_operator_progress("responding")
        self.status_lbl.setText("")
        if self._live_bubble:
            self._live_bubble.append_response_token(chunk)
            self.chat_widget._scroll_to_bottom_if_near()

    def _on_commentary(self, text: str):
        """AGENT-COMMENTARY-01A. No explicit scroll call here, deliberately
        matching _on_tool_call/_on_think_chunk above (not _on_response_chunk):
        commentary is a tool-phase event, not final-response streaming, so it
        follows the same UI-CHAT-SCROLL-01 "passive" viewport behavior those
        already have rather than force-following the tail."""
        self._mark_operator_progress("commentary")
        if self._live_bubble:
            self._live_bubble.add_commentary(text)

    def _on_final_stream_timing(self, duration_s: float, token_count: int):
        """TOKS-STREAM-TIMING-01. Always arrives before finished/cancelled/
        error for the same turn (see StreamSignals.final_stream_timing's
        own docstring) -- stash straight onto the still-live bubble so
        _on_finished()/_on_cancelled()/_on_error()'s later finalize() call
        already has it. No pending/window-level state needed: a turn that
        never emits this leaves the bubble's stream fields at their None
        default, which reads as "unavailable", not stale data from a
        previous turn (each bubble is fresh per turn)."""
        if self._live_bubble:
            self._live_bubble.set_stream_timing(duration_s, token_count)

    def _on_final_ttft(self, final_ttft_s: float):
        """TOKS-STREAM-TIMING-01 (Bino-approved expansion, Bino-corrected
        naming). Same stash-onto-the-live-bubble pattern as
        _on_final_stream_timing() above -- Final TTFT, scoped to the
        final-producing request only."""
        if self._live_bubble:
            self._live_bubble.set_final_ttft(final_ttft_s)

    def _on_time_to_first_answer(self, ttfa_s: float):
        """TOKS-STREAM-TIMING-01 (Bino-approved expansion). Same stash-
        onto-the-live-bubble pattern as _on_final_stream_timing() above."""
        if self._live_bubble:
            self._live_bubble.set_time_to_first_answer(ttfa_s)

    def _on_finished(self, response: str):
        

        if self._live_bubble:
            self._live_bubble.finalize()
            self._live_bubble = None
        self.chat_widget.set_turn_running(False)
        self._operator_turn_started_at = None
        self._operator_phase = "idle"
        self._operator_current_tool = None
        self.status_lbl.setText("")
        self._refresh_operator_telemetry(refresh_context=True)
        if self._current_chat_id and response:
            save_chat_message(self._current_chat_id, "assistant", response)
            self._refresh_chat_list()
            # A failed turn returns its error as plain response text instead
            # of raising (see core/agent.py chat()/_stream_final()) — it's
            # still saved above so the failure is visible in history, but it
            # must not trigger auto-naming, which would fire a second doomed
            # complete_utility() call against the same broken provider/
            # endpoint (duplicate noise, potentially duplicate paid traffic
            # against a metered provider).
            if is_error_response(response):
                return
            # ── Auto-name if still has default timestamp name ──
            current_name = get_chat_name(self._current_chat_id)
            if current_name.startswith("Chat "):
                msgs = load_chat_messages(self._current_chat_id)
                user_msgs = [m["content"] for m in msgs if m["role"] == "user"]
                if user_msgs:
                    print(f"[AUTO-NAME] trigger — chat_id={self._current_chat_id} name='{current_name}'", flush=True)
                    print(f"[AUTO-NAME] user_msg preview: {user_msgs[0][:80]}", flush=True)
                    self._auto_name_chat(self._current_chat_id, user_msgs[0], response)
            self._maybe_compact(self._current_chat_id)


    def _on_cancelled(self, partial_response: str,
                      persist_partial_response: bool = True):
        """Finish cancelled-turn presentation without overclaiming durability.

        The default preserves the established ordinary-stream behavior for
        direct callers and older tests. Reconciliation cancellation arrives
        with ``persist_partial_response=False``: already-streamed text remains
        visible in the live bubble, but no assistant row is written for later
        reconstruction.
        """
        partial_response = (partial_response or "").strip()
        if self._live_bubble:
            if partial_response:
                self._live_bubble.finalize()
            else:
                self._live_bubble.setParent(None)
                self._live_bubble.deleteLater()
            self._live_bubble = None

        self.chat_widget.set_turn_running(False)
        self._operator_turn_started_at = None
        self._operator_phase = "idle"
        self._operator_current_tool = None
        self.status_lbl.setText("")

        if (self._current_chat_id and partial_response
                and persist_partial_response):
            save_chat_message(
                self._current_chat_id, "assistant", partial_response,
                metadata={"cancelled": True},
            )
            self._refresh_chat_list()
            detail = "partial response kept in the transcript"
        elif partial_response:
            detail = "partial response shown but not committed"
        else:
            detail = "no assistant response was committed"

        self._refresh_operator_telemetry(refresh_context=True)
        self.chat_widget.add_operator_message(
            f"/stop completed · foreground turn stopped · {detail} · background tasks untouched"
        )

    def _on_error(self, error: str):
        if self._live_bubble:
            self._live_bubble.append_response_token(f"[Error: {error}]")
            self._live_bubble.finalize()
            self._live_bubble = None
        self.chat_widget.set_turn_running(False)
        self._operator_turn_started_at = None
        self._operator_phase = "idle"
        self._operator_current_tool = None
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
