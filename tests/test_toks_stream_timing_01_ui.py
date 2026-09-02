"""
tests/test_toks_stream_timing_01_ui.py -- TOKS-STREAM-TIMING-01 (UI half)

Real-Qt (offscreen) regressions for the presentation side of truthful
final-stream throughput telemetry: MetricsBar.set_metrics() (ui/
chat_widget.py), LiveResponseBubble's turn/stream timing, and the
AgentWorker -> StreamSignals.final_stream_timing -> MainWindow wiring
(ui/main_window.py). See tests/test_toks_stream_timing_01.py for the
core/agent.py-side coverage of what on_final_stream_timing actually
measures; this file covers what the UI does with it once it arrives.

Reuses the exact fixtures/conventions from tests/test_ui_chat_commentary.py
(COLORS, qapp, AgentWorker.run()-called-directly pattern -- QThread.run()
is just a method; same-thread Qt signal connections fire synchronously, so
plain list.append callbacks observe emission order faithfully) -- read
directly before writing this file.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

import ui.chat_widget as chat_widget_module
from ui.chat_widget import ChatWidget, LiveResponseBubble, MetricsBar
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


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _FakeClock:
    def __init__(self, ticks):
        self._ticks = list(ticks)
        self._i = 0

    def __call__(self):
        if self._i >= len(self._ticks):
            raise AssertionError(
                f"fake time.monotonic() called more times than scripted "
                f"({len(self._ticks)} ticks provided)")
        v = self._ticks[self._i]
        self._i += 1
        return v


@pytest.fixture
def fake_clock(monkeypatch):
    def _install(ticks):
        clock = _FakeClock(ticks)
        monkeypatch.setattr(chat_widget_module.time, "monotonic", clock)
        return clock
    return _install


# ── MetricsBar.set_metrics(): presentation/guard logic in isolation ─────────

def test_set_metrics_shows_turn_and_stream_when_both_available(qapp):
    bar = MetricsBar(COLORS)
    bar.set_metrics(18.4, 2.1, 24, 0, 187, 2, 3.2, ttfa=1.2, final_ttft=0.3)
    text = bar.lbl.text()
    assert "18.4s turn" in text
    assert "1.2s first answer" in text
    assert "2.1s stream" in text
    assert f"{24/2.1:.1f} tok/s" in text
    assert "0.3s Final TTFT" in text
    assert "0in / 187out / 187total" in text
    assert "2 tool calls" in text
    assert "think: 3.2s" in text


def test_set_metrics_shows_stream_unavailable_when_never_measured(qapp):
    bar = MetricsBar(COLORS)
    bar.set_metrics(18.4, None, None, 0, 187, 0, 0.0)
    text = bar.lbl.text()
    assert "18.4s turn" in text
    assert "stream n/a" in text
    assert "tok/s" not in text
    # ttfa/final_ttft default to None (not passed) -- both render unavailable.
    assert "first answer n/a" in text
    assert "Final TTFT n/a" in text


def test_set_metrics_shows_turn_unavailable_for_restored_bubble(qapp):
    bar = MetricsBar(COLORS)
    bar.set_metrics(None, None, None, 0, 42, 0, 0.0)
    text = bar.lbl.text()
    assert "turn n/a" in text
    assert "stream n/a" in text
    assert "first answer n/a" in text
    assert "Final TTFT n/a" in text
    # Existing chunk-count usage display survives untouched.
    assert "0in / 42out / 42total" in text


@pytest.mark.parametrize("ttfa,final_ttft", [
    (0.0, 0.3),    # zero ttfa
    (-1.0, 0.3),   # negative ttfa
    (1.2, 0.0),    # zero final_ttft
    (1.2, -1.0),   # negative final_ttft
])
def test_set_metrics_never_shows_degenerate_ttfa_or_final_ttft(qapp, ttfa, final_ttft):
    bar = MetricsBar(COLORS)
    bar.set_metrics(18.4, 2.1, 24, 0, 187, 0, 0.0, ttfa=ttfa, final_ttft=final_ttft)
    text = bar.lbl.text()
    if ttfa <= 0:
        assert "first answer n/a" in text
    if final_ttft <= 0:
        assert "Final TTFT n/a" in text


@pytest.mark.parametrize("stream_elapsed,stream_tokens", [
    (0.0, 10),      # zero duration
    (-1.0, 10),     # negative/invalid duration
    (2.0, 0),       # zero tokens
    (2.0, None),    # missing token count
])
def test_set_metrics_never_divides_on_degenerate_stream_values(qapp, stream_elapsed, stream_tokens):
    bar = MetricsBar(COLORS)
    # Must not raise (ZeroDivisionError or otherwise) and must not show a
    # bogus/absurd/infinite rate.
    bar.set_metrics(5.0, stream_elapsed, stream_tokens, 0, 10, 0, 0.0)
    text = bar.lbl.text()
    assert "stream n/a" in text
    assert "inf" not in text.lower()


# ── LiveResponseBubble: turn/stream timing wiring ────────────────────────────

def test_live_bubble_turn_elapsed_spans_creation_to_finalize(qapp, fake_clock):
    fake_clock([100.0, 145.0])  # creation, finalize
    bubble = LiveResponseBubble(COLORS)
    bubble.append_response_token("hello")
    bubble.finalize()
    text = bubble.metrics.lbl.text()
    assert "45.0s turn" in text
    # No stream timing was ever supplied for this bubble.
    assert "stream n/a" in text


def test_live_bubble_think_time_does_not_change_turn_start_anchor(qapp, fake_clock):
    """open_think_block()/close_think_block() read the clock too, but for
    their OWN separate _think_time accumulator (unaffected by this fix,
    kept for the "think: N.Ns" metrics segment) -- never for
    _turn_start_time, which append_response_token() used to reset at the
    first response token (the pre-fix conflation this ticket removes).
    Scripting exactly 4 ticks (creation, think-open, think-close,
    finalize) and asserting turn_elapsed == finalize - creation (10.0s),
    NOT some value perturbed by the 2s think interval in between, proves
    the two clocks stay independent."""
    fake_clock([200.0, 205.0, 207.0, 210.0])  # creation, think-open, think-close, finalize
    bubble = LiveResponseBubble(COLORS)
    bubble.open_think_block(1)
    bubble.append_think_token("reasoning")
    bubble.close_think_block()
    bubble.append_response_token("answer")
    bubble.finalize()
    text = bubble.metrics.lbl.text()
    assert "10.0s turn" in text
    assert "think: 2.0s" in text


def test_live_bubble_set_stream_timing_feeds_metrics_bar(qapp, fake_clock):
    fake_clock([1000.0, 1005.0])
    bubble = LiveResponseBubble(COLORS)
    bubble.append_response_token("final answer text")
    bubble.set_stream_timing(2.5, 12)
    bubble.finalize()
    text = bubble.metrics.lbl.text()
    assert "5.0s turn" in text
    assert "2.5s stream" in text
    assert f"{12/2.5:.1f} tok/s" in text


def test_live_bubble_never_calls_set_stream_timing_shows_unavailable(qapp, fake_clock):
    """A turn whose final never carried trustworthy timing (a synthetic
    notice, an empty/tool-only final, a cancelled stream -- see
    core/agent.py's on_final_stream_timing docstring for the exhaustive
    list) never calls set_stream_timing() at all. finalize() must show
    that as unavailable, never as some stale or zero-valued rate."""
    fake_clock([50.0, 53.0])
    bubble = LiveResponseBubble(COLORS)
    bubble.append_response_token("[Lumina: some notice text]")
    bubble.finalize()
    text = bubble.metrics.lbl.text()
    assert "3.0s turn" in text
    assert "stream n/a" in text


def test_live_bubble_set_final_ttft_and_answer_feed_metrics_bar(qapp, fake_clock):
    fake_clock([2000.0, 2008.0])
    bubble = LiveResponseBubble(COLORS)
    bubble.set_final_ttft(0.3)
    bubble.set_time_to_first_answer(1.2)
    bubble.append_response_token("final answer text")
    bubble.set_stream_timing(2.5, 12)
    bubble.finalize()
    text = bubble.metrics.lbl.text()
    assert "8.0s turn" in text
    assert "1.2s first answer" in text
    assert "2.5s stream" in text
    assert "0.3s Final TTFT" in text


def test_live_bubble_never_calls_set_time_to_first_answer_shows_unavailable(qapp, fake_clock):
    """Same 'no signal this turn = unavailable' contract as stream timing
    (see the test above) applies independently to TTFA/Final TTFT -- a
    live bubble that streamed real content but whose turn never fired
    either callback (e.g. a held-candidate promotion never fires Final
    TTFT; a cancelled-before-Final turn never fires either) shows both as
    n/a without disturbing turn/stream, which come from separate sources."""
    fake_clock([100.0, 102.0])
    bubble = LiveResponseBubble(COLORS)
    bubble.append_response_token("held answer text")
    bubble.set_stream_timing(0.0, 0)  # never actually called this way in
                                       # production (guarded at the source),
                                       # confirms the bubble-side guard too
    bubble.finalize()
    text = bubble.metrics.lbl.text()
    assert "2.0s turn" in text
    assert "stream n/a" in text
    assert "first answer n/a" in text
    assert "Final TTFT n/a" in text


def test_live_bubble_held_candidate_shape_shows_final_ttft_and_stream_unavailable_only(qapp, fake_clock):
    """Regression (Bino final review, item 3), at the UI layer: the exact
    held-candidate shape -- set_time_to_first_answer() DOES fire (the
    user really did wait this long to see the first word), turn_elapsed
    keeps ticking from real bubble creation, but set_final_ttft()/
    set_stream_timing() are NEVER called (a promoted candidate's WORK
    round is blocking/non-streaming -- see core/agent.py's
    _finalize_completion_candidate() docstring). Renders as "Final TTFT
    n/a · stream n/a" while turn and first-answer stay honest, never
    fabricated, never each other's default."""
    fake_clock([500.0, 503.0])
    bubble = LiveResponseBubble(COLORS)
    bubble.set_time_to_first_answer(2.5)  # fires -- real, delivery-time TTFA
    bubble.append_response_token("a complete answer delivered from a held candidate")
    # set_final_ttft()/set_stream_timing() deliberately never called --
    # this IS the held-candidate shape.
    bubble.finalize()
    text = bubble.metrics.lbl.text()
    assert "3.0s turn" in text          # real, honest turn wall time
    assert "2.5s first answer" in text  # real, honest TTFA
    assert "Final TTFT n/a" in text     # unavailable -- no streaming request
    assert "stream n/a" in text         # unavailable -- same reason


# ── LiveResponseBubble: restored (historical/reconstructed) bubbles ────────

def test_restored_bubble_never_fabricates_turn_or_stream_timing(qapp, fake_clock):
    """ui/main_window.py::_load_chat()'s reconstruction loop sets
    _response_text directly and never streams a single token -- restored=
    True must mean finalize() computes NOTHING from a live clock (no
    monotonic() call at all is scripted here; _FakeClock would raise if
    one occurred), matching "historical messages ... degrade honestly --
    omit the rate or show it as unavailable. Do not recompute the old
    false metric from whole-turn time.\""""
    fake_clock([])  # zero ticks -- any monotonic() call is a bug
    bubble = LiveResponseBubble(COLORS, restored=True)
    bubble._response_text = "an answer from three days ago"
    bubble.finalize()
    text = bubble.metrics.lbl.text()
    assert "turn n/a" in text
    assert "stream n/a" in text
    # Bino-approved expansion: a restored message never ran a live turn
    # at all, so neither TTFA nor Final TTFT has anything to report
    # either -- set_final_ttft()/set_time_to_first_answer() are simply
    # never called for a restored bubble (no agent.py callback ever fires
    # for it), leaving both at their None default.
    assert "first answer n/a" in text
    assert "Final TTFT n/a" in text


def test_chat_widget_create_live_bubble_restored_flag_propagates(qapp):
    chat = ChatWidget(COLORS)
    live = chat.create_live_bubble(restored=False)
    restored = chat.create_live_bubble(restored=True)
    assert live._restored is False
    assert live._turn_start_time is not None
    assert restored._restored is True
    assert restored._turn_start_time is None


# ── AgentWorker -> StreamSignals.final_stream_timing wiring ────────────────

def test_agent_worker_wires_on_final_stream_timing_to_dedicated_signal(qapp):
    class _Agent:
        on_tool_call = None
        on_tool_result = None
        on_think_start = None
        on_think_token = None
        on_think_end = None
        on_response_token = None
        on_commentary = None
        on_final_stream_timing = None

        def chat(self, user_input, chat_id=None, cancel_event=None, reasoning_effort=None):
            self.on_response_token("final answer")
            self.on_final_stream_timing(1.5, 3)
            return "final answer"

    signals = StreamSignals()
    received = []
    signals.final_stream_timing.connect(lambda d, t: received.append((d, t)))
    worker = AgentWorker(_Agent(), "question", signals, chat_id=1)

    worker.run()

    assert received == [(1.5, 3)]


def test_final_stream_timing_never_emitted_for_a_turn_that_never_fires_it(qapp):
    """A turn whose result never fires on_final_stream_timing (e.g. a
    synthetic notice/error path in core/agent.py) must leave the signal
    silent -- proving the UI's only correct default really is "no signal
    this turn", not a value that has to be actively cleared."""
    class _Agent:
        on_tool_call = None
        on_tool_result = None
        on_think_start = None
        on_think_token = None
        on_think_end = None
        on_response_token = None
        on_commentary = None
        on_final_stream_timing = None

        def chat(self, user_input, chat_id=None, cancel_event=None, reasoning_effort=None):
            self.on_response_token("[Lumina error: tool-work iteration limit reached.]")
            return "[Lumina error: tool-work iteration limit reached.]"

    signals = StreamSignals()
    received = []
    signals.final_stream_timing.connect(lambda d, t: received.append((d, t)))
    worker = AgentWorker(_Agent(), "question", signals, chat_id=1)

    worker.run()

    assert received == []


# ── AgentWorker -> final_ttft/time_to_first_answer wiring ──────────────────

def test_agent_worker_wires_final_ttft_and_ttfa_to_dedicated_signals(qapp):
    class _Agent:
        on_tool_call = None
        on_tool_result = None
        on_think_start = None
        on_think_token = None
        on_think_end = None
        on_response_token = None
        on_commentary = None
        on_final_stream_timing = None
        on_final_ttft = None
        on_time_to_first_answer = None

        def chat(self, user_input, chat_id=None, cancel_event=None, reasoning_effort=None):
            self.on_response_token("final answer")
            self.on_final_ttft(0.3)
            self.on_time_to_first_answer(1.2)
            self.on_final_stream_timing(1.5, 3)
            return "final answer"

    signals = StreamSignals()
    received_final_ttft = []
    received_ttfa = []
    order = []
    signals.final_ttft.connect(lambda t: (order.append("final_ttft"), received_final_ttft.append(t)))
    signals.time_to_first_answer.connect(lambda t: (order.append("ttfa"), received_ttfa.append(t)))
    worker = AgentWorker(_Agent(), "question", signals, chat_id=1)

    worker.run()

    assert received_final_ttft == [0.3]
    assert received_ttfa == [1.2]
    assert order == ["final_ttft", "ttfa"]


def test_final_ttft_and_ttfa_never_emitted_for_a_turn_that_never_fires_them(qapp):
    class _Agent:
        on_tool_call = None
        on_tool_result = None
        on_think_start = None
        on_think_token = None
        on_think_end = None
        on_response_token = None
        on_commentary = None
        on_final_stream_timing = None
        on_final_ttft = None
        on_time_to_first_answer = None

        def chat(self, user_input, chat_id=None, cancel_event=None, reasoning_effort=None):
            self.on_response_token("[Lumina error: tool-work iteration limit reached.]")
            return "[Lumina error: tool-work iteration limit reached.]"

    signals = StreamSignals()
    received_final_ttft = []
    received_ttfa = []
    signals.final_ttft.connect(lambda t: received_final_ttft.append(t))
    signals.time_to_first_answer.connect(lambda t: received_ttfa.append(t))
    worker = AgentWorker(_Agent(), "question", signals, chat_id=1)

    worker.run()

    assert received_final_ttft == []
    assert received_ttfa == []


def test_final_stream_timing_final_ttft_ttfa_are_independent_signals(qapp):
    """Regression (Bino final review, item 3), at the signal-wiring
    layer: a held-candidate-shaped turn fires NEITHER final_stream_timing
    NOR final_ttft (both unavailable -- a promoted candidate's WORK
    round is blocking/non-streaming), but DOES fire time_to_first_answer
    (turn wall time is left to the bubble's own independent clock,
    unrelated to any of these three signals). This must leave
    final_stream_timing/final_ttft signals silent while
    time_to_first_answer still lands, proving Qt's own signal routing
    keeps all three independent -- no signal's connected slot ever
    receives another signal's payload."""
    class _Agent:
        on_tool_call = None
        on_tool_result = None
        on_think_start = None
        on_think_token = None
        on_think_end = None
        on_response_token = None
        on_commentary = None
        on_final_stream_timing = None
        on_final_ttft = None
        on_time_to_first_answer = None

        def chat(self, user_input, chat_id=None, cancel_event=None, reasoning_effort=None):
            self.on_response_token("held answer")
            self.on_time_to_first_answer(6.01)  # held candidate: TTFA fires, Final TTFT does not
            return "held answer"

    signals = StreamSignals()
    received = {"final_stream_timing": [], "final_ttft": [], "ttfa": []}
    signals.final_stream_timing.connect(lambda d, t: received["final_stream_timing"].append((d, t)))
    signals.final_ttft.connect(lambda t: received["final_ttft"].append(t))
    signals.time_to_first_answer.connect(lambda t: received["ttfa"].append(t))
    worker = AgentWorker(_Agent(), "question", signals, chat_id=1)

    worker.run()

    assert received["final_stream_timing"] == []
    assert received["final_ttft"] == []
    assert received["ttfa"] == [6.01]


# ── Presentation: expanded metrics bar at Lumina's minimum supported width ──

def test_metrics_bar_wraps_at_minimum_supported_width_instead_of_clipping(qapp):
    """ui/main_window.py's LuminaWindow._setup_window() calls
    setMinimumSize(960, 660) -- Lumina's documented minimum supported
    window size. The chat column gets that width minus the sidebar's
    fixed 160px + 1px divider (~799px), narrower still inside the
    bubble's own margins. With every metric segment populated (turn/
    first-answer/Final TTFT/stream/tok-s/usage/tools/think), the
    unwrapped line is long enough that a plain single-line QLabel would
    either get clipped by its container or force the bubble wider than
    its column. wordWrap(True) must let Qt grow the label's HEIGHT at a
    narrow width instead of losing content -- verified two ways: (1) a
    relational heightForWidth() comparison (narrower width -> taller
    layout, the Qt-native signature of wrapping having occurred, never
    magic pixel values per this file's own geometry-assertion
    convention), and (2) every one of the 8 segments is still present in
    the label's text -- wrapping is a rendering/layout concern, never
    data loss."""
    bar = MetricsBar(COLORS)
    bar.set_metrics(18.4, 2.1, 24, 0, 187, 2, 3.2, ttfa=1.2, final_ttft=0.3)
    assert bar.lbl.wordWrap() is True

    # 799px: the derived minimum chat-column width. 300px: an aggressive
    # stress case well below it, matching this repo's existing "narrow"
    # convention (tests/test_ui_chat_bubble_height_01.py's 450px case).
    minimum_column_height = bar.lbl.heightForWidth(799)
    narrow_height = bar.lbl.heightForWidth(300)
    wide_height = bar.lbl.heightForWidth(1600)
    assert narrow_height >= minimum_column_height >= wide_height

    text = bar.lbl.text()
    for expected in ("18.4s turn", "1.2s first answer", "0.3s Final TTFT",
                      "2.1s stream", "tok/s", "0in / 187out / 187total",
                      "2 tool calls", "think: 3.2s"):
        assert expected in text


def test_metrics_bar_embedded_in_narrow_chat_widget_does_not_clip(qapp):
    """Same claim as the direct heightForWidth() test above, proven
    against a REAL embedded bubble inside a REAL ChatWidget resized to
    the derived minimum chat-column width (960 - 161 sidebar ~= 799px),
    exercising actual layout/geometry rather than the label in
    isolation."""
    chat = ChatWidget(COLORS)
    chat.resize(799, 700)
    chat.show()
    bubble = chat.create_live_bubble()
    bubble.append_response_token("a reasonably long final answer to push the metrics line wide")
    bubble.set_stream_timing(2.1, 24)
    bubble.set_final_ttft(0.3)
    bubble.set_time_to_first_answer(1.2)
    bubble.finalize()
    chat._settle_layout()

    assert bubble.metrics.lbl.wordWrap() is True
    # The metrics row's actual rendered width must never exceed the
    # bubble's own -- if it did, the row would be clipped by its parent
    # frame or spill outside the visible chat column.
    assert bubble.metrics.width() <= bubble.bubble.width()
    text = bubble.metrics.lbl.text()
    for expected in ("turn", "first answer", "Final TTFT", "stream", "tok/s"):
        assert expected in text
