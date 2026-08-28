"""UI-CHAT-BUBBLE-HEIGHT-01 — finalized assistant bubbles size from their
real width.

Real-Qt (offscreen) regressions for LiveResponseBubble.finalize(): the
response QTextBrowser used to measure its document at a hardcoded
setTextWidth(900) and freeze the resulting height with setFixedHeight(),
regardless of the widget's actual assigned width. Reproduced (see the
UI-CHAT-BUBBLE-HEIGHT-01 task block) as two failure directions:
  - real width < 900px: content needs MORE height than the 900px
    measurement produced -> the frozen height clips the tail invisibly
    (ScrollBarAlwaysOff hides the overflow -- no visible scrollbar, just
    missing content).
  - real width > 900px: content needs LESS height than the 900px
    measurement produced -> a stale blank region sits below the content,
    exactly the live symptom, and it never shrinks on a later resize
    because nothing ever recomputes the frozen height.

ResponseBrowser (ui/chat_widget.py) replaces the one-shot measurement with
Qt's height-for-width mechanism: the document is laid out at the widget's
own real width, and heightForWidth()/sizeHint() report a truthful height
so the parent QVBoxLayout sizes the widget to its actual content -- at
first layout and after every later resize.

Geometry assertions are relational, per the slice contract -- never magic
pixel values baked to one machine's font rasterization.
"""
import inspect
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from ui.chat_widget import ChatWidget, LiveResponseBubble, ResponseBrowser

try:
    from ui import main_window as mw
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

# Long unbroken paragraphs (no manual line breaks) so wrapping -- and hence
# document height -- is genuinely sensitive to the widget's real width.
_PROSE = ("This response mirrors the shape of a known-bad live reply: it keeps "
          "going without a manual line break, mentioning the mixdown bus, the "
          "routing nodes, the checksum mismatches, the clock-skew hypothesis, "
          "and the monitoring thresholds, so how many lines it occupies depends "
          "almost entirely on how wide the widget actually is when Qt lays it "
          "out. ")
LONG_RESPONSE = "# Routing Summary\n\n" + (_PROSE * 4) + "\n\n## Findings\n\n" + (_PROSE * 3)
SHORT_RESPONSE = "All routing nodes report nominal."
LIST_RESPONSE = "# Status\n\n" + "\n".join(f"- node {i}: nominal" for i in range(12))
CODE_RESPONSE = "Here's the fix:\n\n```python\n" + "\n".join(
    f"def handler_{i}(payload):\n    return payload.get('key_{i}')" for i in range(6)
) + "\n```\n"
URL_RESPONSE = ("See https://internal.example.com/very/long/path/segment/that/keeps/"
                "going/without/any/spaces/or/manual/breaks/whatsoever/for/testing"
                "/wrap/behavior/on/an/unbroken/token for details.")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_chat(qapp, w=900, h=700):
    chat = ChatWidget(COLORS)
    chat.resize(w, h)
    chat.show()
    chat._settle_layout()
    return chat


def _finalize(chat, text):
    bubble = chat.create_live_bubble()
    bubble._response_text = text
    bubble.finalize()
    chat._settle_layout()
    return bubble


def _browser_of(bubble):
    return bubble.bubble_layout.itemAt(0).widget()


def _content_bottom(bubble):
    return _browser_of(bubble).geometry().bottom()


def _metrics_gap(bubble):
    return bubble.metrics.geometry().top() - _content_bottom(bubble)


# ── A/B. finalization sizes to content, no giant blank tail ──────────────────

def test_short_response_normal_width_is_compact(qapp):
    chat = _make_chat(qapp, w=900)
    bubble = _finalize(chat, SHORT_RESPONSE)
    browser = _browser_of(bubble)

    assert browser.height() < 100  # one short line, not a runaway allocation
    assert 0 <= _metrics_gap(bubble) <= 20


def test_long_wrapped_response_wide_width_has_no_blank_tail(qapp):
    chat = _make_chat(qapp, w=1950)
    bubble = _finalize(chat, LONG_RESPONSE)
    browser = _browser_of(bubble)

    doc_h = int(browser.document().size().height())
    # The widget's real height must track the document it actually rendered
    # -- not a taller stale allocation left over from a hardcoded-width
    # measurement.
    assert abs(browser.height() - doc_h) <= 4
    assert 0 <= _metrics_gap(bubble) <= 20


# ── C/D. width changes the wrap, height follows honestly ─────────────────────

def test_narrower_width_yields_taller_browser(qapp):
    chat_narrow = _make_chat(qapp, w=500)
    narrow = _finalize(chat_narrow, LONG_RESPONSE)
    chat_wide = _make_chat(qapp, w=1950)
    wide = _finalize(chat_wide, LONG_RESPONSE)

    assert _browser_of(narrow).height() > _browser_of(wide).height()


def test_wider_width_yields_shorter_browser(qapp):
    chat_wide = _make_chat(qapp, w=1950)
    wide = _finalize(chat_wide, LONG_RESPONSE)
    chat_narrow = _make_chat(qapp, w=500)
    narrow = _finalize(chat_narrow, LONG_RESPONSE)

    assert _browser_of(wide).height() < _browser_of(narrow).height()


# ── E/F. resize AFTER finalization reflows and re-sizes ──────────────────────

def test_resize_wide_to_narrow_grows_finalized_bubble(qapp):
    chat = _make_chat(qapp, w=1950)
    bubble = _finalize(chat, LONG_RESPONSE)
    browser = _browser_of(bubble)
    h_wide = browser.height()

    chat.resize(500, 700)
    chat._settle_layout()

    assert browser.height() > h_wide
    doc_h = int(browser.document().size().height())
    assert abs(browser.height() - doc_h) <= 4  # not stale -- recomputed


def test_resize_narrow_to_wide_shrinks_finalized_bubble(qapp):
    chat = _make_chat(qapp, w=500)
    bubble = _finalize(chat, LONG_RESPONSE)
    browser = _browser_of(bubble)
    h_narrow = browser.height()

    chat.resize(1950, 700)
    chat._settle_layout()

    assert browser.height() < h_narrow
    doc_h = int(browser.document().size().height())
    assert abs(browser.height() - doc_h) <= 4


# ── G. repeated toggling is stable, no cumulative growth ─────────────────────

def test_repeated_width_toggling_returns_to_stable_geometry(qapp):
    chat = _make_chat(qapp, w=1400)
    bubble = _finalize(chat, LONG_RESPONSE)
    browser = _browser_of(bubble)

    heights_narrow, heights_wide = [], []
    for _ in range(6):
        chat.resize(500, 700)
        chat._settle_layout()
        heights_narrow.append(browser.height())
        chat.resize(1400, 700)
        chat._settle_layout()
        heights_wide.append(browser.height())

    assert len(set(heights_narrow)) == 1
    assert len(set(heights_wide)) == 1


# ── H. no internal vertical scrollbar for normal content ─────────────────────

def test_resize_does_not_create_internal_scrollbar(qapp):
    chat = _make_chat(qapp, w=1950)
    bubble = _finalize(chat, LONG_RESPONSE)
    browser = _browser_of(bubble)
    assert browser.verticalScrollBar().maximum() == 0

    chat.resize(500, 700)
    chat._settle_layout()
    assert browser.verticalScrollBar().maximum() == 0

    chat.resize(1950, 700)
    chat._settle_layout()
    assert browser.verticalScrollBar().maximum() == 0


# ── I/J. runtime width, not a hardcoded reference width ──────────────────────

def test_document_text_width_follows_real_viewport_width_never_900(qapp):
    for width in (500, 900, 1400, 1950):
        chat = _make_chat(qapp, w=width)
        bubble = _finalize(chat, LONG_RESPONSE)
        browser = _browser_of(bubble)
        assert browser.document().textWidth() == browser.viewport().width()
        if width != 900:  # the old bug's value must not appear except by coincidence
            assert browser.document().textWidth() != 900


def test_no_hardcoded_width_dependency_in_finalize_source():
    source = inspect.getsource(LiveResponseBubble.finalize)
    assert "900" not in source
    assert "setFixedHeight" not in source
    assert "ResponseBrowser" in source


# ── K/L. metrics stay pinned to content, controls stay reachable ─────────────

def test_metrics_gap_stays_small_across_widths(qapp):
    for width in (500, 900, 1950):
        chat = _make_chat(qapp, w=width)
        bubble = _finalize(chat, LONG_RESPONSE)
        gap = _metrics_gap(bubble)
        assert 0 <= gap <= 20, f"width={width} produced a {gap}px metrics gap"


def test_copy_and_replay_controls_remain_reachable(qapp):
    chat = _make_chat(qapp, w=500)
    bubble = _finalize(chat, LONG_RESPONSE)
    assert bubble.metrics.copy_btn.isVisible()
    assert bubble.metrics.isVisible()


# ── M/N. history reconstruction uses the same finalizer, same invariants ─────

class _FakeCtx:
    def __init__(self):
        self.history = []

    def clear(self):
        self.history.clear()

    def add_user(self, content):
        self.history.append(("user", content))

    def add_assistant(self, content):
        self.history.append(("assistant", content))


def _fake_window(chat):
    from types import SimpleNamespace
    return SimpleNamespace(
        _current_chat_id=42,
        _prefs={},
        agent=SimpleNamespace(ctx=_FakeCtx()),
        chat_widget=chat,
        _refresh_chat_list=lambda: None,
    )


@pytest.mark.skipif(not _HAVE_MW, reason="ui.main_window unavailable")
def test_history_restore_produces_sane_geometry_like_live_finalize(qapp, monkeypatch):
    chat = _make_chat(qapp, w=500)
    fake = _fake_window(chat)
    store = {42: [
        {"role": "user", "content": "what's going on with the bus?"},
        {"role": "assistant", "content": LONG_RESPONSE},
    ]}
    monkeypatch.setattr(mw, "load_chat_messages", lambda cid: store[cid])
    monkeypatch.setattr(mw, "latest_manual_compaction_skip", lambda cid: 0)
    monkeypatch.setattr(mw.persistence, "update", lambda *a, **kw: None)

    mw.LuminaWindow._load_chat(fake, 42)
    chat._settle_layout()

    bubble = chat.msgs_layout.itemAt(chat.msgs_layout.count() - 2).widget()
    assert isinstance(bubble, LiveResponseBubble)
    browser = _browser_of(bubble)
    doc_h = int(browser.document().size().height())
    assert abs(browser.height() - doc_h) <= 4
    assert browser.verticalScrollBar().maximum() == 0


@pytest.mark.skipif(not _HAVE_MW, reason="ui.main_window unavailable")
def test_history_restore_at_different_width_sizes_for_new_width(qapp, monkeypatch):
    chat = _make_chat(qapp, w=1950)
    fake = _fake_window(chat)
    store = {42: [
        {"role": "user", "content": "what's going on with the bus?"},
        {"role": "assistant", "content": LONG_RESPONSE},
    ]}
    monkeypatch.setattr(mw, "load_chat_messages", lambda cid: store[cid])
    monkeypatch.setattr(mw, "latest_manual_compaction_skip", lambda cid: 0)
    monkeypatch.setattr(mw.persistence, "update", lambda *a, **kw: None)

    mw.LuminaWindow._load_chat(fake, 42)
    chat._settle_layout()
    bubble_wide = chat.msgs_layout.itemAt(chat.msgs_layout.count() - 2).widget()
    h_wide = _browser_of(bubble_wide).height()

    chat.resize(500, 700)
    chat._settle_layout()
    mw.LuminaWindow._load_chat(fake, 42)  # reload at the NEW width
    chat._settle_layout()
    bubble_narrow = chat.msgs_layout.itemAt(chat.msgs_layout.count() - 2).widget()
    h_narrow = _browser_of(bubble_narrow).height()

    assert h_narrow > h_wide


# ── O/P/Q/R. markdown shapes don't break the geometry mechanism ──────────────

@pytest.mark.parametrize("label,text", [
    ("prose_headings", LONG_RESPONSE),
    ("lists", LIST_RESPONSE),
    ("fenced_code", CODE_RESPONSE),
    ("long_url", URL_RESPONSE),
])
def test_markdown_shapes_produce_sane_geometry(qapp, label, text):
    chat = _make_chat(qapp, w=900)
    bubble = _finalize(chat, text)
    browser = _browser_of(bubble)

    doc_h = int(browser.document().size().height())
    assert abs(browser.height() - doc_h) <= 4, label
    assert browser.verticalScrollBar().maximum() == 0, label
    assert 0 <= _metrics_gap(bubble) <= 20, label


# ── S/T. adjacent scroll behavior is undisturbed ──────────────────────────────
#
# create_live_bubble() inserts in "none" mode -- bubble creation itself never
# repositions the viewport (UI-CHAT-SCROLL-01). finalize() only swaps the
# streaming label for the sized ResponseBrowser in place; it never calls
# _insert()/_scroll_to_bottom*() at all. So the invariant this fix must not
# break is narrow and precise: finalize(), and a later resize's reflow, must
# never themselves move bar.value() -- whether the operator was sitting at
# the tail or had deliberately scrolled away from it. (bar.maximum() is free
# to change since the content really did grow/shrink -- that's correct.)

def _populate_short_turns(chat, n=20):
    for i in range(n):
        chat.add_user_message(f"turn {i}")
        b = chat.create_live_bubble()
        b._response_text = f"answer {i}"
        b.finalize()
    chat._settle_layout()


def test_finalize_does_not_move_viewport_when_at_bottom(qapp):
    chat = _make_chat(qapp, w=900)
    _populate_short_turns(chat)
    bar = chat.scroll.verticalScrollBar()
    bar.setValue(bar.maximum())
    chat._settle_layout()
    v0 = bar.value()

    _finalize(chat, LONG_RESPONSE)

    assert bar.value() == v0  # finalize() itself moved nothing


def test_finalize_resize_while_scrolled_up_does_not_move_viewport(qapp):
    chat = _make_chat(qapp, w=900)
    _populate_short_turns(chat)
    bar = chat.scroll.verticalScrollBar()
    bar.setValue(bar.maximum() - 2000)
    chat._settle_layout()
    v0 = bar.value()

    chat.resize(500, 700)
    chat._settle_layout()

    assert bar.value() == v0  # the reflow from resizing must not move it either
