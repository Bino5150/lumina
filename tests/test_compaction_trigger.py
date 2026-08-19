"""ui/main_window.py — MB-11 Step 5: LuminaWindow._maybe_compact(), the
trigger that fires context-trim compaction from _on_finished(). Same shape
as _auto_name_chat(): fire-and-forget, background daemon thread.

Uses the unbound-method + SimpleNamespace fake pattern already established
in tests/test_agent_error_helpers.py for LuminaAgent._stream_final() --
_maybe_compact()'s body touches no Qt state directly, so a real QMainWindow
is unnecessary. Genuinely PySide6-dependent only because importing
ui.main_window at all requires it (module-level PySide6 imports) -- skipped
in CI same as the other ui/main_window.py tests.
"""
import os
import types

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import config
import core.dreaming as dreaming
import tools.palace as palace
from ui.main_window import LuminaWindow


class ImmediateThread:
    """Runs target() synchronously on .start() instead of spawning a real
    OS thread -- makes the fire-and-forget background job deterministic
    and assertable within a single test."""
    def __init__(self, target=None, daemon=None, **kw):
        self._target = target

    def start(self):
        self._target()


def _fake_window(pending_tokens, compacting=False):
    ctx = types.SimpleNamespace(
        _compacting=compacting,
        pending_compaction_tokens=lambda: pending_tokens,
        take_pending_compaction=lambda: [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ],
        restored=[],  # 3A.2 R9-corrective -- restore_pending_compaction() calls land here
    )
    ctx.restore_pending_compaction = lambda batch: ctx.restored.append(batch)
    win = types.SimpleNamespace(
        agent=types.SimpleNamespace(ctx=ctx),
    )
    return win, ctx


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    import ui.main_window as main_window
    monkeypatch.setattr(main_window, "threading", types.SimpleNamespace(Thread=ImmediateThread))
    monkeypatch.setattr(config, "CONTEXT_COMPACTION_ENABLED", True)
    monkeypatch.setattr(config, "CONTEXT_COMPACTION_BATCH_TOKENS", 1400)


def test_noop_when_flag_disabled(monkeypatch):
    monkeypatch.setattr(config, "CONTEXT_COMPACTION_ENABLED", False)
    win, ctx = _fake_window(pending_tokens=2000)
    calls = []
    monkeypatch.setattr(dreaming, "run_summarization_call", lambda *a, **k: calls.append(1) or "x")

    LuminaWindow._maybe_compact(win, chat_id=42)

    assert calls == []
    assert ctx._compacting is False


def test_noop_when_chat_id_is_none():
    """The guard requested to prevent room=str(None) writes."""
    win, ctx = _fake_window(pending_tokens=2000)

    LuminaWindow._maybe_compact(win, chat_id=None)

    assert ctx._compacting is False


def test_noop_when_chat_id_is_falsy_zero():
    win, ctx = _fake_window(pending_tokens=2000)

    LuminaWindow._maybe_compact(win, chat_id=0)

    assert ctx._compacting is False


def test_noop_when_already_compacting():
    win, ctx = _fake_window(pending_tokens=2000, compacting=True)

    LuminaWindow._maybe_compact(win, chat_id=42)

    # Still True -- untouched, not reset by a run that never happened.
    assert ctx._compacting is True


def test_noop_when_below_batch_threshold(monkeypatch):
    win, ctx = _fake_window(pending_tokens=100)
    calls = []
    monkeypatch.setattr(dreaming, "run_summarization_call", lambda *a, **k: calls.append(1) or "x")

    LuminaWindow._maybe_compact(win, chat_id=42)

    assert calls == []
    assert ctx._compacting is False


def test_fires_and_writes_to_nightstand_wing_tagged_correctly(monkeypatch):
    win, ctx = _fake_window(pending_tokens=2000)
    summarize_calls = []
    store_calls = []

    def fake_summarize(raw_text, prompt=None, max_tokens=500):
        summarize_calls.append({"raw_text": raw_text, "prompt": prompt, "max_tokens": max_tokens})
        return "- did a thing"

    def fake_store(**kw):
        store_calls.append(kw)
        return {"closet_id": 1, "drawer_id": 1, "compressed": "x", "tokens_saved": 0}

    monkeypatch.setattr(dreaming, "run_summarization_call", fake_summarize)
    monkeypatch.setattr(palace, "palace_store", fake_store)

    LuminaWindow._maybe_compact(win, chat_id=42)

    assert len(summarize_calls) == 1
    assert summarize_calls[0]["prompt"] is dreaming.COMPACTION_PROMPT
    assert summarize_calls[0]["max_tokens"] == 300
    assert "hello" in summarize_calls[0]["raw_text"]
    assert "hi there" in summarize_calls[0]["raw_text"]

    assert len(store_calls) == 1
    call = store_calls[0]
    assert call["wing"] == "nightstand"  # S57 correction -- NOT "sessions"
    assert call["room"] == "42"
    assert call["layer"] == 2
    assert call["content"] == "- did a thing"
    assert "auto-compaction" in call["tags"]
    assert "session:42" in call["tags"]

    # Reset after the run, so a later turn can trigger again.
    assert ctx._compacting is False
    assert ctx.restored == []  # committed successfully -- nothing to restore


def test_compacting_flag_resets_even_when_palace_store_raises(monkeypatch):
    """try/finally around `_compacting = False` must survive a failure
    downstream of the summarizer too (DB lock, disk full, whatever) -- not
    just run_summarization_call()'s own already-covered None-return path.
    A stuck _compacting=True is a silent deadlock: compaction would just
    quietly stop firing for the rest of the session, no error, no crash,
    nothing to notice.

    3A.2 R9-corrective: this is also the Palace-write-failure scenario --
    the drained batch must come back via restore_pending_compaction()
    rather than being silently lost along with the failed write."""
    win, ctx = _fake_window(pending_tokens=2000)
    monkeypatch.setattr(dreaming, "run_summarization_call", lambda *a, **k: "- did a thing")

    def raising_store(**kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(palace, "palace_store", raising_store)

    with pytest.raises(RuntimeError, match="database is locked"):
        LuminaWindow._maybe_compact(win, chat_id=42)

    assert ctx._compacting is False
    assert len(ctx.restored) == 1
    assert [m["content"] for m in ctx.restored[0]] == ["hello", "hi there"]


def test_does_not_store_when_summarization_returns_none(monkeypatch):
    win, ctx = _fake_window(pending_tokens=2000)
    store_calls = []
    monkeypatch.setattr(dreaming, "run_summarization_call", lambda *a, **k: None)
    monkeypatch.setattr(palace, "palace_store", lambda **kw: store_calls.append(kw))

    LuminaWindow._maybe_compact(win, chat_id=42)

    assert store_calls == []
    assert ctx._compacting is False  # finally still resets it
    # 3A.2 R9-corrective: an empty summary must not lose the batch either.
    assert len(ctx.restored) == 1
    assert [m["content"] for m in ctx.restored[0]] == ["hello", "hi there"]


def test_pending_buffer_drained_before_summarization_call(monkeypatch):
    """take_pending_compaction() must actually be called (draining the
    buffer) -- not just pending_compaction_tokens() re-read repeatedly."""
    win, ctx = _fake_window(pending_tokens=2000)
    monkeypatch.setattr(dreaming, "run_summarization_call", lambda *a, **k: None)
    drained = []
    ctx.take_pending_compaction = lambda: (drained.append(True), [{"role": "user", "content": "x"}])[1]

    LuminaWindow._maybe_compact(win, chat_id=42)

    assert drained == [True]
