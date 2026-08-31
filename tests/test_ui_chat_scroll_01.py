"""UI-CHAT-SCROLL-01 — send-time viewport anchor + insertion scroll intents.

Real-Qt (offscreen) regressions for the chat scroll lifecycle:
- a foreground send anchors the new turn (no absolute-bottom teleport; the
  start of the submitted message stays visible, response space below)
- passive insertions follow only an operator who is already following the
  tail, and preserve the viewport of one who deliberately scrolled upward
- history restore / chat switch end at the latest turn via ONE explicit
  layout-settled positioning, not dozens of racing delayed timers
- the anchor is synchronous: no delayed callback can overwrite it (the old
  80 ms fire-and-hope timer race is gone, and stays gone)

Geometry assertions are relational (visibility, ordering, deltas), never
magic pixel values, per the slice contract.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from core.context_transaction import ContextGeneration
from ui.chat_widget import ChatWidget

try:
    from ui import main_window as mw
    from core.context_reconstruction import ReconstructionResult
    _HAVE_MW = True
except ModuleNotFoundError:
    _HAVE_MW = False

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

TALL_TASK_BLOCK = "LUMINA TASK BLOCK — RENDER BUS\n" + "\n".join(
    f"{i}. verify bus routing node {i} against the console snapshot"
    for i in range(40)
)
LONG_OPERATOR_TEXT = "background maintenance: chunk checksummed\n" * 18


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_chat(qapp, w=800, h=600):
    chat = ChatWidget(COLORS)
    chat.resize(w, h)
    chat.show()
    qapp.processEvents()
    return chat


def _populate(chat, qapp, turns=30):
    for i in range(turns):
        chat.add_user_message(f"user turn {i}: question about the mixdown bus {i}")
        bubble = chat.create_live_bubble()
        bubble._response_text = f"assistant turn {i}: the bus routing answer {i}."
        bubble.finalize()
    qapp.processEvents()


def _bar(chat):
    return chat.scroll.verticalScrollBar()


def _viewport_height(chat):
    return chat.scroll.viewport().height()


def _top_visible(chat, widget):
    b = _bar(chat)
    return b.value() <= widget.geometry().top() < b.value() + _viewport_height(chat)


def _send(chat, qapp, text):
    """Mirror MainWindow._on_user_message's exact insertion sequence:
    anchored user card + live bubble, then let the event loop (and any
    stale delayed callback a mutation could schedule) fully settle."""
    chat.add_user_message(text, mode="anchor")
    card = chat.msgs_layout.itemAt(chat.msgs_layout.count() - 2).widget()
    bubble = chat.create_live_bubble()
    qapp.processEvents()
    QTest.qWait(120)  # outlive the old 80 ms delayed-scroll window
    qapp.processEvents()
    return card, bubble


# ── A. exact send-time bug ────────────────────────────────────────────────────

def test_send_presents_new_turn_instead_of_teleporting_to_bottom(qapp):
    chat = _make_chat(qapp)
    _populate(chat, qapp)
    b = _bar(chat)
    b.setValue(b.maximum() - 300)  # readable tail: last turn on screen
    qapp.processEvents()
    assert b.value() < b.maximum()  # positioning took (layout settled)

    card, _bubble = _send(chat, qapp, TALL_TASK_BLOCK)

    # The start of what the operator just sent is on screen...
    assert _top_visible(chat, card)
    # ...and the viewport did NOT teleport to the absolute transcript bottom.
    assert b.value() < b.maximum()


# ── B. operator already at bottom ─────────────────────────────────────────────

def test_send_at_bottom_keeps_new_turn_fully_readable(qapp):
    chat = _make_chat(qapp)
    _populate(chat, qapp)
    b = _bar(chat)
    b.setValue(b.maximum())
    qapp.processEvents()

    card, bubble = _send(chat, qapp, "continue")

    assert _top_visible(chat, card)
    assert card.geometry().bottom() <= b.value() + _viewport_height(chat)
    assert bubble.geometry().top() < b.value() + _viewport_height(chat)


# ── C. deliberate upward scroll + passive arrival ────────────────────────────

def test_scrolled_up_operator_preserved_on_passive_insert(qapp):
    chat = _make_chat(qapp)
    _populate(chat, qapp)
    b = _bar(chat)
    b.setValue(b.maximum() - 2000)
    qapp.processEvents()
    v0 = b.value()

    chat.add_system_message("checkpoint saved")
    chat.add_operator_message("background job finished")
    qapp.processEvents()
    QTest.qWait(120)
    qapp.processEvents()

    assert b.value() == v0  # no yank, viewport exactly preserved


# ── C2. follower at bottom still sees large passive arrivals ─────────────────

def test_follower_at_bottom_follows_large_passive_insert(qapp):
    chat = _make_chat(qapp)
    _populate(chat, qapp)
    b = _bar(chat)
    b.setValue(b.maximum())
    qapp.processEvents()

    chat.add_operator_message(LONG_OPERATOR_TEXT)  # taller than the 200 px window
    qapp.processEvents()
    QTest.qWait(120)
    qapp.processEvents()

    assert b.value() == b.maximum()  # pre-insert follow state honored


# ── D. send while scrolled upward ─────────────────────────────────────────────

def test_send_while_scrolled_up_moves_to_anchor_not_bottom(qapp):
    chat = _make_chat(qapp)
    _populate(chat, qapp)
    b = _bar(chat)
    b.setValue(b.maximum() - 2000)
    qapp.processEvents()

    card, _bubble = _send(chat, qapp, TALL_TASK_BLOCK)

    # The operator initiated a new turn, so moving is legitimate — but the
    # destination is the new-turn anchor, never the absolute bottom.
    assert _top_visible(chat, card)
    assert b.value() < b.maximum()


# ── E. large submitted task block ─────────────────────────────────────────────

def test_large_task_block_anchors_at_its_start(qapp):
    chat = _make_chat(qapp)
    _populate(chat, qapp)
    b = _bar(chat)

    card, _bubble = _send(chat, qapp, TALL_TASK_BLOCK)

    assert _top_visible(chat, card)
    # Anchored to the card's top with the chosen 12 px margin (clamped).
    expected = max(0, card.geometry().top() - 12)
    assert b.value() == min(expected, b.maximum())


# ── F. live bubble insertion must not override the send anchor ───────────────

def test_live_bubble_insertion_preserves_send_anchor(qapp):
    chat = _make_chat(qapp)
    _populate(chat, qapp)
    b = _bar(chat)
    b.setValue(b.maximum() - 300)
    qapp.processEvents()

    chat.add_user_message(TALL_TASK_BLOCK, mode="anchor")
    v_anchor = b.value()
    assert _top_visible(
        chat, chat.msgs_layout.itemAt(chat.msgs_layout.count() - 2).widget()
    )

    chat.create_live_bubble()
    qapp.processEvents()
    QTest.qWait(120)
    qapp.processEvents()

    assert b.value() == v_anchor  # bubble creation repositioned nothing


# ── G. tool / think growth never yanks a scrolled-away operator ──────────────

def test_tool_and_think_growth_do_not_yank_scrolled_operator(qapp):
    chat = _make_chat(qapp)
    _populate(chat, qapp)
    b = _bar(chat)
    b.setValue(b.maximum() - 2000)
    qapp.processEvents()
    v0 = b.value()

    bubble = chat.create_live_bubble()  # "none": creation moves nothing
    qapp.processEvents()
    assert b.value() == v0

    bubble.add_tool_call("web_search", {"query": "bus routing"})
    bubble.open_think_block(1)
    qapp.processEvents()
    QTest.qWait(120)
    qapp.processEvents()

    assert b.value() == v0


# ── §8. the anchor is synchronous — no delayed callback can win a race ───────

def test_anchor_is_synchronous_before_any_event_loop_pass(qapp):
    chat = _make_chat(qapp)
    _populate(chat, qapp)
    b = _bar(chat)
    b.setValue(b.maximum() - 300)
    qapp.processEvents()

    chat.add_user_message(TALL_TASK_BLOCK, mode="anchor")
    card = chat.msgs_layout.itemAt(chat.msgs_layout.count() - 2).widget()

    # No processEvents, no qWait: the anchor must already hold.
    assert _top_visible(chat, card)
    v_now = b.value()

    chat.create_live_bubble()
    assert b.value() == v_now  # and the bubble cannot move it either


# ── H/I. history restore + chat switch (real _load_chat path) ────────────────

class _FakeCtx:
    def __init__(self):
        self.history = []
        self.users = []
        self.assistants = []

    def clear(self):
        self.history.clear()
        self.users.clear()
        self.assistants.clear()

    def add_user(self, content):
        self.history.append(("user", content))
        self.users.append(content)

    def add_assistant(self, content):
        self.history.append(("assistant", content))
        self.assistants.append(content)


def _fake_window(chat):
    from types import SimpleNamespace
    return SimpleNamespace(
        _current_chat_id=42,
        _prefs={},
        agent=SimpleNamespace(ctx=_FakeCtx()),
        chat_widget=chat,
        _refresh_chat_list=lambda: None,
        worker=None,
        _context_generation=ContextGeneration(),
        _chat_switch_admitted=lambda: True,
    )


def _restore_msgs(turns):
    msgs = []
    for i in range(turns):
        msgs.append({"role": "user", "content": f"restored user turn {i}"})
        msgs.append({"role": "assistant", "content": f"restored answer {i}"})
    return msgs


def _fake_reconstruct(store):
    """CONTEXT-LIFECYCLE-A2: _load_chat() now gets its candidate history
    from core.context_reconstruction.reconstruct_chat_context() instead
    of independently calling load_chat_messages()/latest_manual_
    compaction_skip(). These scroll/geometry tests don't care about real
    persistence or compaction semantics (test_context_reconstruction.py
    and test_manual_compaction_ui.py already cover those) -- they just
    need an arbitrary in-memory row set, so the kernel entrypoint itself
    is faked here rather than routing an in-memory store through a real
    on-disk DB."""
    def _reconstruct(chat_id, context_skip=0):
        msgs = store[chat_id]
        rows = [{"id": i, "role": m["role"], "content": m["content"], "metadata": ""}
                for i, m in enumerate(msgs, start=1)]
        messages = [{"role": m["role"], "content": m["content"]} for m in msgs]
        return ReconstructionResult(
            chat_id=chat_id, context_skip=context_skip, rows=rows,
            eligible_rows=rows, messages=messages,
            restored_row_count=len(messages), skipped_row_count=0,
            durable_spine_fingerprint="test-fixture",
        )
    return _reconstruct


@pytest.mark.skipif(not _HAVE_MW, reason="ui.main_window unavailable")
def test_history_restore_ends_at_latest_turn_with_one_positioning(qapp, monkeypatch):
    chat = _make_chat(qapp)
    fake = _fake_window(chat)

    store = {42: _restore_msgs(30)}
    monkeypatch.setattr(mw, "resolve_context_skip", lambda cid: 0)
    monkeypatch.setattr(mw, "reconstruct_chat_context", _fake_reconstruct(store))
    monkeypatch.setattr(mw.persistence, "update", lambda *a, **kw: None)

    calls = []
    original = chat._scroll_to_bottom
    chat._scroll_to_bottom = lambda: (calls.append(1), original())

    mw.LuminaWindow._load_chat(fake, 42)

    b = _bar(chat)
    # max must reflect the real restored content (not a stale pre-restore
    # range) and the viewport must sit exactly at the latest turn.
    assert b.maximum() > _viewport_height(chat)
    assert b.value() == b.maximum()  # reopens at the latest turn
    assert len(calls) == 1  # exactly ONE intentional positioning, no timer pile-up


@pytest.mark.skipif(not _HAVE_MW, reason="ui.main_window unavailable")
def test_chat_switch_positions_deterministically(qapp, monkeypatch):
    chat = _make_chat(qapp)
    fake = _fake_window(chat)

    store = {42: _restore_msgs(30), 43: _restore_msgs(3)}
    monkeypatch.setattr(mw, "resolve_context_skip", lambda cid: 0)
    monkeypatch.setattr(mw, "reconstruct_chat_context", _fake_reconstruct(store))
    monkeypatch.setattr(mw.persistence, "update", lambda *a, **kw: None)

    mw.LuminaWindow._load_chat(fake, 42)
    b = _bar(chat)
    assert b.value() == b.maximum()

    mw.LuminaWindow._load_chat(fake, 43)  # switch long -> short
    assert b.value() == b.maximum()


# ── J. repeated sends stay stable, no stale state across turns ───────────────

def test_repeated_sends_each_anchor_to_their_own_turn(qapp):
    chat = _make_chat(qapp)
    _populate(chat, qapp)
    b = _bar(chat)

    cards = []
    for i in range(3):
        card, _bubble = _send(chat, qapp, f"turn {i}: " + TALL_TASK_BLOCK)
        cards.append(card)
        assert _top_visible(chat, card), f"send {i} lost its own anchor"
        assert b.value() < b.maximum()

    # After extra settling, the latest anchor still holds (no stale drift).
    qapp.processEvents()
    QTest.qWait(120)
    qapp.processEvents()
    assert _top_visible(chat, cards[-1])
