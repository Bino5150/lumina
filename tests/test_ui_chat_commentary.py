"""UI-CHAT-COMMENTARY-01 (AGENT-COMMENTARY-01A UI half) -- real-Qt (offscreen)
regressions for the operator-facing commentary timeline: CommentaryRow
(ui/chat_widget.py), LiveResponseBubble.add_commentary(), and the
agent -> AgentWorker -> StreamSignals.commentary -> MainWindow wiring
(ui/main_window.py).

Reuses the exact fixtures/helpers/COLORS dict from
tests/test_ui_chat_bubble_height_01.py and tests/test_ui_chat_scroll_01.py
(read directly before writing this file), and the AgentWorker.run()-called-
directly pattern from tests/test_operator_stop_ui.py -- QThread.run() is
just a method; calling it synchronously exercises the real callback wiring
without needing actual thread concurrency, and same-thread Qt signal
connections fire synchronously (DirectConnection), so plain list.append
callbacks observe emission order faithfully.

Design invariants under test (task-block sections 8-11, 19):
  - commentary renders as its own row, distinct from ToolRow/ThinkBlock
  - insertion position matches emission order in the live timeline
  - commentary never enters _response_text, MetricsBar's copy/replay
    source, tok_out, or _tool_calls
  - commentary never creates/mutates a ThinkBlock
  - no explicit scroll call -- same "passive" behavior as existing
    tool_call/think-chunk events (UI-CHAT-SCROLL-01)
  - finalize()'s content-sized ResponseBrowser (UI-CHAT-BUBBLE-HEIGHT-01)
    is unaffected by commentary rows added earlier in the same bubble
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from ui.chat_widget import ChatWidget, LiveResponseBubble, CommentaryRow, ThinkBlock, ToolRow
from ui.main_window import AgentWorker, StreamSignals

COLORS = {
    "bg_deep": "#0a0b0f", "bg_panel": "#0f1117", "bg_sidebar": "#0c0d12",
    "bg_card": "#13151e", "bg_input": "#1a1d28", "accent": "#00e5ff",
    "accent_dim": "#0099b3", "accent_glow": "#00e5ff33",
    "text_primary": "#e8eaf0", "text_muted": "#6b7280", "text_dim": "#3d4355",
    "border": "#1e2133", "border_accent": "#00e5ff44", "user_bubble": "#1a2035",
    "ai_bubble": "#111420", "tool_bg": "#0d1520", "tool_text": "#00b4cc",
    "think_bg": "#0a1020", "think_text": "#4a7a9b", "danger": "#ff4757",
    "success": "#2ed573", "warning": "#ffa502",
}

LONG_COMMENTARY = (
    "That reproduces the bug reliably across every one of the routing nodes "
    "I checked, so the next step is narrowing down which mixdown bus config "
    "actually diverges from the checksum baseline before I touch anything. "
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _make_chat(qapp, w=900, h=700):
    chat = ChatWidget(COLORS)
    chat.resize(w, h)
    chat.show()
    chat._settle_layout()
    return chat


def _live_bubble(colors=COLORS):
    return LiveResponseBubble(colors)


def _widget_types(bubble):
    return [
        type(bubble.bubble_layout.itemAt(i).widget()).__name__
        for i in range(bubble.bubble_layout.count())
    ]


# ── N. Dedicated commentary signal reaches the agent -> worker -> UI chain ─

def test_agent_worker_wires_on_commentary_to_dedicated_signal(qapp):
    class _Agent:
        on_tool_call = None
        on_tool_result = None
        on_think_start = None
        on_think_token = None
        on_think_end = None
        on_response_token = None
        on_commentary = None

        def chat(self, user_input, chat_id=None, cancel_event=None, reasoning_effort=None):
            self.on_commentary("checking the persistence path")
            self.on_tool_call("search_memory", {})
            return "done"

    signals = StreamSignals()
    received_commentary = []
    received_tool_calls = []
    order = []
    signals.commentary.connect(lambda text: (order.append("commentary"), received_commentary.append(text)))
    signals.tool_call.connect(lambda name, args: (order.append("tool_call"), received_tool_calls.append(name)))
    worker = AgentWorker(_Agent(), "question", signals, chat_id=1)

    worker.run()

    assert received_commentary == ["checking the persistence path"]
    assert received_tool_calls == ["search_memory"]
    assert order == ["commentary", "tool_call"]


def test_worker_commentary_handler_flushes_buffered_response_before_emitting(qapp):
    """The worker batches response tokens (BATCH_CHARS) before flushing them
    as response_chunk signals. on_commentary must flush any such buffered
    text first, same discipline as on_tool_call already has -- otherwise a
    leftover buffered fragment could render out of order relative to the
    commentary event that logically followed it."""
    class _Agent:
        on_tool_call = None
        on_tool_result = None
        on_think_start = None
        on_think_token = None
        on_think_end = None
        on_response_token = None
        on_commentary = None

        def chat(self, user_input, chat_id=None, cancel_event=None, reasoning_effort=None):
            self.on_response_token("a")  # below BATCH_CHARS -- stays buffered
            self.on_commentary("narration")
            return "done"

    signals = StreamSignals()
    order = []
    signals.response_chunk.connect(lambda chunk: order.append(("response_chunk", chunk)))
    signals.commentary.connect(lambda text: order.append(("commentary", text)))
    worker = AgentWorker(_Agent(), "question", signals, chat_id=1)

    worker.run()

    assert order[0] == ("response_chunk", "a")
    assert order[1] == ("commentary", "narration")


def test_main_window_on_commentary_renders_into_live_bubble(qapp):
    from ui.main_window import LuminaWindow
    import types as _types

    bubble = _live_bubble()
    fake_window = _types.SimpleNamespace(
        _live_bubble=bubble,
        _operator_phase="idle",
        _operator_current_tool=None,
        _operator_last_progress_at=0,
        _mark_operator_progress=lambda phase, tool=None: None,
    )

    LuminaWindow._on_commentary(fake_window, "checking the persistence path")

    types_ = _widget_types(bubble)
    assert "CommentaryRow" in types_


# ── A. CommentaryRow basic shape ─────────────────────────────────────────

def test_commentary_row_is_selectable_and_wraps(qapp):
    row = CommentaryRow("brief narration", COLORS)
    lbl = row.findChildren(type(row.layout().itemAt(0).widget()))[0]
    assert lbl.wordWrap() is True
    from PySide6.QtCore import Qt
    assert lbl.textInteractionFlags() & Qt.TextSelectableByMouse
    assert lbl.text() == "brief narration"


# ── B. add_commentary() inserts a distinct widget type ──────────────────

def test_add_commentary_inserts_commentary_row_not_tool_or_think(qapp):
    bubble = _live_bubble()
    bubble.add_commentary("checking the persistence path")

    types_ = _widget_types(bubble)
    assert "CommentaryRow" in types_
    assert "ToolRow" not in types_
    assert "ThinkBlock" not in types_


# ── O. Ordering: Think, Commentary, Tool, Commentary, Tool, Final ───────

def test_full_event_sequence_renders_in_emission_order(qapp):
    bubble = _live_bubble()
    bubble.open_think_block(1)
    bubble.close_think_block()
    bubble.add_commentary("first: checking memory")
    bubble.add_tool_call("search_memory", {})
    bubble.add_commentary("second: reading the file it found")
    bubble.add_tool_call("read_file", {"path": "x.py"})
    bubble.append_response_token("final answer")

    types_ = _widget_types(bubble)
    # stream_lbl (still the live text label pre-finalize) is last.
    assert types_ == ["ThinkBlock", "CommentaryRow", "ToolRow", "CommentaryRow", "ToolRow", "QLabel", "MetricsBar"]


# ── P. Multi-tool batch: one commentary, not repeated per tool ──────────

def test_one_commentary_then_multiple_tools_not_interleaved(qapp):
    bubble = _live_bubble()
    bubble.add_commentary("running three checks")
    bubble.add_tool_call("tool_a", {})
    bubble.add_tool_call("tool_b", {})
    bubble.add_tool_call("tool_c", {})

    types_ = _widget_types(bubble)
    assert types_ == ["CommentaryRow", "ToolRow", "ToolRow", "ToolRow", "QLabel", "MetricsBar"]


# ── Q/R. Wrapping + resize ────────────────────────────────────────────────

def test_commentary_wraps_taller_at_narrow_width(qapp):
    chat_narrow = _make_chat(qapp, w=450)
    bubble_n = chat_narrow.create_live_bubble()
    bubble_n.add_commentary(LONG_COMMENTARY)
    chat_narrow._settle_layout()

    chat_wide = _make_chat(qapp, w=1900)
    bubble_w = chat_wide.create_live_bubble()
    bubble_w.add_commentary(LONG_COMMENTARY)
    chat_wide._settle_layout()

    row_n = bubble_n.bubble_layout.itemAt(0).widget()
    row_w = bubble_w.bubble_layout.itemAt(0).widget()
    assert row_n.height() > row_w.height()


def test_commentary_row_resizes_without_phantom_blank(qapp):
    chat = _make_chat(qapp, w=1900)
    bubble = chat.create_live_bubble()
    bubble.add_commentary(LONG_COMMENTARY)
    chat._settle_layout()
    row = bubble.bubble_layout.itemAt(0).widget()
    h_wide = row.height()

    chat.resize(450, 700)
    chat._settle_layout()
    h_narrow = row.height()

    assert h_narrow > h_wide  # reflowed, not frozen at the wide-width height
    assert h_narrow > 0 and h_wide > 0


# ── S/T/U/V. Final-response / copy / replay / metrics isolation ─────────

def test_commentary_does_not_enter_response_text_or_metrics(qapp):
    bubble = _live_bubble()
    bubble.add_commentary("scoping the investigation")
    bubble.add_tool_call("search_memory", {})
    bubble.add_commentary("narrowing it down")
    bubble.append_response_token("The root cause is a stale write.")
    bubble.finalize()

    assert bubble._response_text == "The root cause is a stale write."
    assert "scoping" not in bubble._response_text
    assert "narrowing" not in bubble._response_text
    # MetricsBar's copy/replay source is exactly _response_text, nothing more.
    assert bubble.metrics._response_text == "The root cause is a stale write."
    # tok_out only counts real response tokens, never commentary calls.
    assert bubble._tok_out == 1
    # Tool count reflects the one real tool call, not the two commentary events.
    assert bubble._tool_calls == 1


def test_copy_button_copies_only_final_response(qapp, monkeypatch):
    from PySide6.QtWidgets import QApplication as QA
    bubble = _live_bubble()
    bubble.add_commentary("long internal narration nobody asked to copy")
    bubble.append_response_token("short final answer")
    bubble.finalize()

    copied = {}
    monkeypatch.setattr(QA.clipboard(), "setText", lambda text: copied.setdefault("text", text))
    bubble.metrics._copy()

    assert copied["text"] == "short final answer"
    assert "narration" not in copied["text"]


def test_replay_speaks_only_final_response_not_commentary(qapp):
    spoken = []

    class _FakeTTS:
        def speak(self, text, blocking=True, on_done=None):
            spoken.append(text)

    bubble = LiveResponseBubble(COLORS, tts=_FakeTTS(), tts_speech_allowed=lambda: True)
    bubble.add_commentary("internal narration, never spoken")
    bubble.append_response_token("final answer")
    bubble.finalize()

    bubble.metrics._replay()

    assert spoken == ["final answer"]


# ── W. Think isolation ────────────────────────────────────────────────────

def test_commentary_does_not_create_or_mutate_think_block(qapp):
    bubble = _live_bubble()
    bubble.open_think_block(1)
    bubble.append_think_token("reasoning content")
    bubble.close_think_block()
    think_widget_before = bubble.bubble_layout.itemAt(0).widget()

    bubble.add_commentary("unrelated narration")

    # The finalized ThinkBlock is untouched -- still the same widget, same content.
    assert bubble.bubble_layout.itemAt(0).widget() is think_widget_before
    assert bubble._think_block is None


# ── X. Scroll intent preserved (UI-CHAT-SCROLL-01) ───────────────────────

def test_commentary_insertion_does_not_force_scroll(qapp):
    chat = _make_chat(qapp, w=900, h=300)
    for i in range(20):
        chat.add_user_message(f"user turn {i}")
        b = chat.create_live_bubble()
        b.append_response_token(f"assistant turn {i}: padding padding padding")
        b.finalize()
    chat._settle_layout()

    bar = chat.scroll.verticalScrollBar()
    # Operator deliberately scrolls away from the tail.
    bar.setValue(0)
    pos_before = bar.value()

    live = chat.create_live_bubble()
    live.add_commentary("background narration the operator did not ask to follow")
    chat._settle_layout()

    assert bar.value() == pos_before  # never teleported back to the tail


# ── Y. finalize() sizing still content-driven with commentary rows present ─

def test_finalize_sizes_correctly_with_commentary_rows_present(qapp):
    chat = _make_chat(qapp, w=900)
    bubble = chat.create_live_bubble()
    bubble.add_commentary("first narration")
    bubble.add_tool_call("search_memory", {})
    bubble.add_commentary("second narration")
    bubble.append_response_token("A short final answer.")
    bubble.finalize()
    chat._settle_layout()

    browser = bubble.bubble_layout.itemAt(bubble.bubble_layout.count() - 2).widget()
    assert type(browser).__name__ == "ResponseBrowser"
    assert browser.height() < 100  # short answer stays compact regardless of prior rows
