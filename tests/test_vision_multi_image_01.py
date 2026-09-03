"""VISION-MULTI-IMAGE-01 — multiple images in one user turn.

Root cause (source-vetted, not assumed): ui/main_window.py held exactly one
pending image (`self._pending_image`, a single `(path, b64, media_type)`
tuple) no matter how many image files were dropped or picked in one action.
`_on_files_dropped()`'s per-file loop overwrote that single slot on every
image it processed, so only the LAST image dropped ever survived to
`_on_user_message()` -- even though the input textbox's placeholder text
(`[image: a.png] [image: b.png] ...`) correctly accumulated one line per
file, silently misleading the user into thinking every image was attached.
`ui/chat_widget.py`'s preview strip had the same one-frame-only limitation.

Every layer BELOW the UI was already generic over an arbitrary-length list
of multipart content blocks and needed zero changes:
  - core/context.py's ContextManager.add_user() accepts any content list.
  - core/backends/lmstudio.py's LMStudioBackend.chat()/chat_stream() (which
    core/backends/openrouter.py's OpenRouterBackend -- the real configured
    z-ai/glm-5.3-flash route -- inherits UNMODIFIED) forward `messages`
    straight through to the wire with no restructuring or per-image cap;
    see test_openrouter_chat_forwards_all_image_blocks_in_order_unmodified
    below.
  - core/backends/gemini_backend.py's GeminiBackend._parts_from_content()
    already loops over every block in the list; see the three new
    multi-image cases added to test_gemini_vision_translation.py.

core/backends/anthropic_backend.py's AnthropicBackend._translate_messages()
was the one real exception at the time this ticket landed -- a PRE-EXISTING
gap unrelated to it (never translated a single OpenAI-shaped image_url
block to Anthropic's own image-block shape either, so multi-image support
neither introduced nor worsened it) -- since fixed under
ANTHROPIC-VISION-CAPABILITY-01; see
test_anthropic_backend_now_translates_image_blocks_ANTHROPIC_VISION_CAPABILITY_01
below and tests/test_anthropic_vision_capability_01.py for full coverage.

The fix itself is confined to ui/main_window.py (`_pending_image` ->
`_pending_images`, a list, plus a new `_admit_images()` atomic-batch
admission helper and a 📎 attach-button handler) and ui/chat_widget.py
(`show_image_preview`/`clear_image_preview`/`_preview_frame` -> a
multi-row `add_image_preview`/`remove_image_preview`/`clear_image_previews`
preview strip keyed by a monotonic image id, plus the attach button
itself). No image is ever written to disk as a temp file in either the old
or new code -- bytes are read directly and held as base64 in memory -- so
there is no temp-file cleanup surface to add; and no image content is
durably persisted (only the display placeholder text is saved via
tools.memory.save_chat_message()), so this fix does not add or need a new
persistence subsystem -- that boundary is intentionally left alone.
"""
import os
import types

import pytest
import requests

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from core.context_transaction import ContextGeneration
import ui.main_window as mw
from ui.main_window import LuminaWindow
from ui.chat_widget import ChatWidget
from core.backends.openrouter import OpenRouterBackend
from core.backends.anthropic_backend import AnthropicBackend

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

def _write_valid_png(path, color="red"):
    """A genuinely valid, real-decodable PNG, produced by Qt's own encoder
    (not a hand-picked byte constant) -- 16x16 so the re-encoded PNG
    _admit_images() produces safely clears the pre-existing "suspiciously
    small" >=100-byte defensive floor (a bare 1x1 pixel re-encodes under
    that floor and would be misclassified as corrupt here, which isn't
    what this fixture is testing). Distinct colors per call so different
    fixture images are never byte-identical after re-encoding."""
    image = QImage(16, 16, QImage.Format_RGB32)
    image.fill(QColor(color))
    assert image.save(str(path), "PNG")


def _write_corrupt_png(path):
    with open(path, "wb") as f:
        f.write(b"this is not a png file at all")


@pytest.fixture
def chat():
    """A real ChatWidget, explicitly owned and torn down by this fixture --
    AGENT-GLM-COMPLETION-GATE-01 test-isolation follow-up.

    This file used to construct a bare `ChatWidget(COLORS)` per test with
    no explicit teardown, letting it go out of scope and relying entirely
    on CPython's own (non-deterministic-timing) GC to eventually collect
    the last Python reference and trigger shiboken6's C++-side teardown.
    That deferred teardown was reproduced landing inside a completely
    unrelated, LATER test's own Qt event processing -- a real, minimized,
    RED/GREEN-confirmed Segmentation fault (native exit 139). The fix
    belongs HERE, not in tests/conftest.py's shared autouse fixture: this
    file is the one creating 15 real QWidgets a run with no owner, so
    this file is the one that should own their lifecycle.

    Teardown order: close() first (releases native resources, tears down
    owned children), then deleteLater() (schedules the actual C++ object
    destruction through Qt's OWN event queue -- never immediate), then a
    processEvents() call so that scheduled deletion is guaranteed to run
    to completion before this test's teardown returns, rather than
    possibly landing during some later, unrelated test.
    """
    widget = ChatWidget(COLORS)
    yield widget
    widget.close()
    widget.deleteLater()
    app = QApplication.instance()
    if app is not None:
        app.processEvents()


class _FakeAgentWorker:
    """Stands in for ui.main_window.AgentWorker (a real QThread that would
    otherwise call agent.chat() for real). Captures the exact `content`
    argument _on_user_message() builds -- the same value a real AgentWorker
    hands to LuminaAgent.chat(), i.e. the provider-request-shaped payload,
    not just intermediate UI state."""
    instances = []

    def __init__(self, agent, content, signals, chat_id=None):
        self.agent = agent
        self.content = content
        self.signals = signals
        self.chat_id = chat_id
        self.started = False
        _FakeAgentWorker.instances.append(self)

    def start(self):
        self.started = True

    def isRunning(self):
        return False


@pytest.fixture(autouse=True)
def _reset_fake_worker():
    _FakeAgentWorker.instances.clear()
    yield
    _FakeAgentWorker.instances.clear()


def _make_window(chat_widget, **overrides):
    base = dict(
        worker=None,
        _manual_compaction_thread=None,
        chat_widget=chat_widget,
        status_lbl=types.SimpleNamespace(setText=lambda t: None),
        _current_chat_id=None,
        _last_activity=0,
        _operator_turn_started_at=None,
        _live_bubble=None,
        _context_generation=ContextGeneration(),
        agent=types.SimpleNamespace(),
        signals=types.SimpleNamespace(),
        _pending_images=[],
        _pending_audio=None,
        _next_image_id=1,
    )
    base.update(overrides)
    fake = types.SimpleNamespace(**base)
    fake._reset_dream_window_state = lambda: None
    fake._mark_operator_progress = lambda *a, **kw: None
    # _on_files_dropped()/_on_attach_files_requested() call these as
    # `self._admit_images(...)` / `self._on_files_dropped(...)` -- a plain
    # SimpleNamespace has no bound methods, so wire the real
    # unbound-LuminaWindow implementations back onto `fake` itself (same
    # pattern tests/test_emergency_stop_ui.py's _window_fake() already uses
    # for _emergency_rearm_ready() etc.).
    fake._admit_images = lambda paths: LuminaWindow._admit_images(fake, paths)
    fake._on_files_dropped = lambda paths: LuminaWindow._on_files_dropped(fake, paths)
    return fake


# ── 1. one image remains backward-compatible ─────────────────────────────────

def test_single_image_drop_still_produces_one_pending_image_backward_compat(chat, tmp_path):
    fake = _make_window(chat)
    png = tmp_path / "solo.png"
    _write_valid_png(png)

    LuminaWindow._on_files_dropped(fake, [str(png)])

    assert len(fake._pending_images) == 1
    assert fake._pending_images[0]["filename"] == "solo.png"
    assert fake._pending_images[0]["media_type"] == "image/png"
    assert chat.input.toPlainText() == "[image: solo.png]"
    assert len(chat._image_preview_rows) == 1


# ── 2. two and three images plus text / 7. multi-image drag/drop ────────────

def test_three_images_dropped_together_all_become_pending_in_order(chat, tmp_path):
    fake = _make_window(chat)
    paths = []
    for name in ("a.png", "b.png", "c.png"):
        p = tmp_path / name
        _write_valid_png(p)
        paths.append(str(p))

    LuminaWindow._on_files_dropped(fake, paths)

    assert [img["filename"] for img in fake._pending_images] == ["a.png", "b.png", "c.png"]
    assert len(chat._image_preview_rows) == 3
    text = chat.input.toPlainText()
    assert text.index("a.png") < text.index("b.png") < text.index("c.png")


# ── 3. attachment order preserved through the provider boundary ─────────────

def test_submit_with_three_images_sends_all_in_order_then_clears_once(chat, tmp_path, monkeypatch):
    monkeypatch.setattr(mw, "AgentWorker", _FakeAgentWorker)
    fake = _make_window(chat)
    paths = []
    for name in ("A.png", "B.png", "C.png"):
        p = tmp_path / name
        _write_valid_png(p)
        paths.append(str(p))
    LuminaWindow._on_files_dropped(fake, paths)
    # Snapshot before submit clears _pending_images, to assert positional
    # correspondence into the built `content` list below.
    expected = [(img["filename"], img["b64"], img["media_type"]) for img in fake._pending_images]
    assert [f for f, _, _ in expected] == ["A.png", "B.png", "C.png"]

    clear_calls = {"n": 0}
    orig_clear = chat.clear_image_previews
    def _spy():
        clear_calls["n"] += 1
        orig_clear()
    chat.clear_image_previews = _spy

    LuminaWindow._on_user_message(fake, "Describe each attached image separately.")

    assert len(_FakeAgentWorker.instances) == 1
    content = _FakeAgentWorker.instances[0].content
    image_blocks = [b for b in content if b["type"] == "image_url"]
    assert len(content) == 4  # 3 image blocks + exactly 1 trailing text block
    assert len(image_blocks) == 3
    for (fname, b64, media_type), block in zip(expected, image_blocks):
        assert block["image_url"]["url"] == f"data:{media_type};base64,{b64}"
    assert content[-1] == {"type": "text", "text": "Describe each attached image separately."}

    # "Submitting clears the pending attachment set exactly once."
    assert fake._pending_images == []
    assert clear_calls["n"] == 1
    assert len(chat._image_preview_rows) == 0


# ── 4. individual attachment removal ─────────────────────────────────────────

def test_individual_image_removed_leaves_others_pending(chat, tmp_path):
    fake = _make_window(chat)
    paths = []
    for name in ("a.png", "b.png"):
        p = tmp_path / name
        _write_valid_png(p)
        paths.append(str(p))
    LuminaWindow._on_files_dropped(fake, paths)
    assert len(fake._pending_images) == 2
    removed_id = fake._pending_images[0]["id"]

    # Real order (see ChatWidget._on_image_preview_row_cleared): the widget
    # removes its own row, THEN emits image_preview_removed, which is what
    # drives LuminaWindow._on_image_preview_removed(). Mirrored explicitly
    # here to unit-test the window-side list logic on its own; the full
    # signal-wired path is covered separately below.
    chat.remove_image_preview(removed_id)
    LuminaWindow._on_image_preview_removed(fake, removed_id)

    assert [img["filename"] for img in fake._pending_images] == ["b.png"]
    assert len(chat._image_preview_rows) == 1
    text = chat.input.toPlainText()
    assert "a.png" not in text
    assert "b.png" in text


def test_clicking_a_thumbnail_x_button_removes_only_that_image(chat, tmp_path):
    """End-to-end through the real ChatWidget signal, not just the handler
    called directly -- confirms ChatWidget.image_preview_removed actually
    carries the right id and MainWindow is wired to it."""
    fake = _make_window(chat)
    paths = []
    for name in ("a.png", "b.png"):
        p = tmp_path / name
        _write_valid_png(p)
        paths.append(str(p))
    LuminaWindow._on_files_dropped(fake, paths)
    first_id = fake._pending_images[0]["id"]

    received = []
    chat.image_preview_removed.connect(lambda iid: (
        received.append(iid), LuminaWindow._on_image_preview_removed(fake, iid)
    ))
    chat._on_image_preview_row_cleared(first_id)

    assert received == [first_id]
    assert [img["filename"] for img in fake._pending_images] == ["b.png"]
    assert len(chat._image_preview_rows) == 1


# ── 5. repeated selection appends rather than replaces ───────────────────────

def test_repeated_drops_append_not_replace(chat, tmp_path):
    fake = _make_window(chat)
    p1 = tmp_path / "first.png"
    _write_valid_png(p1)
    p2 = tmp_path / "second.png"
    _write_valid_png(p2)

    LuminaWindow._on_files_dropped(fake, [str(p1)])
    LuminaWindow._on_files_dropped(fake, [str(p2)])

    assert [img["filename"] for img in fake._pending_images] == ["first.png", "second.png"]
    assert len(chat._image_preview_rows) == 2


# ── 6. multi-file picker ──────────────────────────────────────────────────────

def test_attach_button_opens_multi_select_dialog_and_admits_all_in_order(chat, tmp_path, monkeypatch):
    fake = _make_window(chat)
    p1 = tmp_path / "x.png"
    _write_valid_png(p1)
    p2 = tmp_path / "y.png"
    _write_valid_png(p2)

    monkeypatch.setattr(
        mw.QFileDialog, "getOpenFileNames",
        lambda *a, **kw: ([str(p1), str(p2)], "Images (*.png *.jpg *.jpeg *.webp *.gif)"),
    )

    LuminaWindow._on_attach_files_requested(fake)

    assert [img["filename"] for img in fake._pending_images] == ["x.png", "y.png"]
    assert len(chat._image_preview_rows) == 2


def test_attach_button_dialog_cancelled_admits_nothing(chat, monkeypatch):
    fake = _make_window(chat)
    monkeypatch.setattr(mw.QFileDialog, "getOpenFileNames", lambda *a, **kw: ([], ""))

    LuminaWindow._on_attach_files_requested(fake)

    assert fake._pending_images == []
    assert len(chat._image_preview_rows) == 0


# ── 8. invalid member causes atomic batch rejection ──────────────────────────

def test_one_corrupt_image_rejects_the_whole_batch_atomically(chat, tmp_path):
    fake = _make_window(chat)
    good = tmp_path / "good.png"
    _write_valid_png(good)
    bad = tmp_path / "bad.png"
    _write_corrupt_png(bad)

    LuminaWindow._on_files_dropped(fake, [str(good), str(bad)])

    assert fake._pending_images == []
    assert len(chat._image_preview_rows) == 0
    text = chat.input.toPlainText()
    assert "bad.png" in text and "failed to load" in text
    assert "good.png" in text  # named too — the good file must be identifiable as skipped


def test_batch_rejection_does_not_touch_images_already_pending_from_an_earlier_drop(chat, tmp_path):
    """Atomicity is scoped to the incoming batch, not the whole pending
    queue -- a later bad drop must not undo an earlier good one (this is
    what makes "repeated selection appends" and "atomic batch rejection"
    compatible requirements)."""
    fake = _make_window(chat)
    already_good = tmp_path / "already.png"
    _write_valid_png(already_good)
    LuminaWindow._on_files_dropped(fake, [str(already_good)])
    assert len(fake._pending_images) == 1

    bad = tmp_path / "bad2.png"
    _write_corrupt_png(bad)
    LuminaWindow._on_files_dropped(fake, [str(bad)])

    assert [img["filename"] for img in fake._pending_images] == ["already.png"]
    assert len(chat._image_preview_rows) == 1


# ── 9. unsupported backends must fail clearly, not degrade to partial input ──

def test_openrouter_chat_forwards_all_image_blocks_in_order_unmodified(monkeypatch):
    """OpenRouterBackend inherits chat() unmodified from LMStudioBackend --
    this is the real GLM route (z-ai/glm-5.3-flash). Confirms it forwards
    every image_url block in a multipart user turn, in order, with nothing
    dropped/truncated/reordered: the backend layer never needed a
    multi-image-specific fix, because it already forwards `messages`
    straight through to the wire. This is also the positive half of "fail
    clearly rather than degrade to partial input" -- there is no code path
    here that silently keeps only some images."""
    backend = OpenRouterBackend.__new__(OpenRouterBackend)
    backend.base_url = "https://openrouter.ai/api/v1"
    backend.headers = {"Content-Type": "application/json", "Authorization": "Bearer test"}
    backend._model = "z-ai/glm-5.3-flash"
    backend._reasoning_cache = {}
    backend._reasoning_cache_ready = False
    backend._vision_tool_cache = {}

    captured = {}

    class _FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["payload"] = json
        return _FakeResp()

    monkeypatch.setattr(requests, "post", _fake_post)

    content = [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,BBBB"}},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,CCCC"}},
        {"type": "text", "text": "Describe each attached image separately."},
    ]
    messages = [{"role": "user", "content": content}]

    backend.chat(messages)

    sent = captured["payload"]["messages"][0]["content"]
    assert sent == content  # byte-identical, order preserved, nothing dropped
    assert sum(1 for b in sent if b["type"] == "image_url") == 3


def test_anthropic_backend_now_translates_image_blocks_ANTHROPIC_VISION_CAPABILITY_01():
    """Formerly a documented-but-not-fixed gap (see git history on this
    test name): AnthropicBackend._translate_messages() used to pass a
    plain user turn's multipart `content` straight through with no OpenAI
    image_url -> Anthropic image-block translation at all -- predated
    VISION-MULTI-IMAGE-01, deliberately ruled out of that ticket's scope,
    and explicitly pointed at ANTHROPIC-VISION-CAPABILITY-01 to fix. Full
    regression coverage for the fix itself lives in
    tests/test_anthropic_vision_capability_01.py; this is just the
    direct proof the specific gap this test used to document is closed."""
    content = [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        {"type": "text", "text": "what is this?"},
    ]
    messages = [{"role": "user", "content": content}]

    translated = AnthropicBackend._translate_messages(messages)

    # Anthropic's real API expects {"type": "image", "source": {...}}, not
    # {"type": "image_url", ...} -- now correctly translated instead of
    # passed through unconverted.
    assert translated == [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAA"}},
        {"type": "text", "text": "what is this?"},
    ]}]


# ── 10/11. cancellation/error cleanup, no duplicate delivery on resubmit ─────

def test_blocked_while_worker_running_preserves_pending_images(chat):
    running_worker = types.SimpleNamespace(isRunning=lambda: True)
    fake = _make_window(chat, worker=running_worker, _pending_images=[
        {"id": 1, "path": "p.png", "filename": "p.png", "b64": "AAA", "media_type": "image/png"},
    ])

    LuminaWindow._on_user_message(fake, "another message while busy")

    assert fake._pending_images == [
        {"id": 1, "path": "p.png", "filename": "p.png", "b64": "AAA", "media_type": "image/png"},
    ]


def test_second_submit_after_first_does_not_resend_already_cleared_images(chat, tmp_path, monkeypatch):
    monkeypatch.setattr(mw, "AgentWorker", _FakeAgentWorker)
    fake = _make_window(chat)
    p = tmp_path / "once.png"
    _write_valid_png(p)
    LuminaWindow._on_files_dropped(fake, [str(p)])

    LuminaWindow._on_user_message(fake, "first turn")
    LuminaWindow._on_user_message(fake, "second turn, no new image")

    assert len(_FakeAgentWorker.instances) == 2
    first_content = _FakeAgentWorker.instances[0].content
    second_content = _FakeAgentWorker.instances[1].content
    assert any(isinstance(b, dict) and b.get("type") == "image_url" for b in first_content)
    assert second_content == "second turn, no new image"  # plain string: no leftover image


# ── 12. no regressions to ordinary files or text-only turns ─────────────────

def test_text_only_turn_has_no_pending_images_and_plain_string_content(chat, monkeypatch):
    monkeypatch.setattr(mw, "AgentWorker", _FakeAgentWorker)
    fake = _make_window(chat)

    LuminaWindow._on_user_message(fake, "just a normal message, no attachments")

    content = _FakeAgentWorker.instances[0].content
    assert content == "just a normal message, no attachments"
    assert fake._pending_images == []


def test_dropping_a_text_file_does_not_touch_pending_images(chat, tmp_path):
    fake = _make_window(chat)
    p = tmp_path / "notes.txt"
    p.write_text("hello world")

    LuminaWindow._on_files_dropped(fake, [str(p)])

    assert fake._pending_images == []
    assert "notes.txt" in chat.input.toPlainText()
    assert len(chat._image_preview_rows) == 0


def test_mixed_batch_image_and_text_file_admits_image_and_inlines_text_independently(chat, tmp_path):
    fake = _make_window(chat)
    img = tmp_path / "pic.png"
    _write_valid_png(img)
    txt = tmp_path / "notes.txt"
    txt.write_text("hello world")

    LuminaWindow._on_files_dropped(fake, [str(img), str(txt)])

    assert [i["filename"] for i in fake._pending_images] == ["pic.png"]
    text = chat.input.toPlainText()
    assert "notes.txt" in text
    assert "hello world" in text
