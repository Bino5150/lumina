import os
import types

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import ui.main_window as main_window
from ui.main_window import LuminaWindow


class _Bubble:
    def __init__(self, rendered):
        self._response_text = ""
        self._rendered = rendered

    def finalize(self):
        self._rendered.append(("assistant", self._response_text))


class _ChatWidget:
    def __init__(self):
        self.rendered = []
        self.notices = []

    def clear_messages(self):
        self.rendered.clear()

    def add_user_message(self, content):
        self.rendered.append(("user", content))

    def create_live_bubble(self):
        return _Bubble(self.rendered)

    def add_operator_message(self, text):
        self.notices.append(text)


class _Ctx:
    def __init__(self, history=None):
        self.history = list(history or [])
        self._last_usage_snapshot = {"stale": True}

    def clear(self):
        self.history = []

    def add_user(self, content):
        self.history.append({"role": "user", "content": content})

    def add_assistant(self, content):
        self.history.append({"role": "assistant", "content": content})


def test_load_chat_renders_full_transcript_but_restores_only_checkpoint_tail(monkeypatch):
    messages = [
        {"role": "user", "content": "u1", "metadata": None},
        {"role": "assistant", "content": "a1", "metadata": None},
        {"role": "user", "content": "u2", "metadata": None},
        {"role": "assistant", "content": "a2", "metadata": None},
        {"role": "user", "content": "u3", "metadata": None},
        {"role": "assistant", "content": "a3", "metadata": None},
    ]
    monkeypatch.setattr(main_window.persistence, "save", lambda prefs: None)
    monkeypatch.setattr(main_window, "load_chat_messages", lambda chat_id: messages)
    monkeypatch.setattr(main_window, "latest_manual_compaction_skip", lambda chat_id: 2)

    fake = types.SimpleNamespace(
        _current_chat_id=None,
        _prefs={},
        agent=types.SimpleNamespace(ctx=_Ctx()),
        chat_widget=_ChatWidget(),
        _refresh_chat_list=lambda: None,
    )
    LuminaWindow._load_chat(fake, 77)

    assert fake.chat_widget.rendered == [
        ("user", "u1"), ("assistant", "a1"),
        ("user", "u2"), ("assistant", "a2"),
        ("user", "u3"), ("assistant", "a3"),
    ]
    assert fake.agent.ctx.history == [
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
        {"role": "assistant", "content": "a3"},
    ]


def _completion_fake(history, chat_id=7):
    ctx = _Ctx(history)
    widget = _ChatWidget()
    agent = types.SimpleNamespace(
        ctx=ctx,
        get_context_usage=lambda chat_id=None, refresh=False: {
            "used_tokens": 1200, "max_tokens": 19000,
        },
    )
    return types.SimpleNamespace(
        _current_chat_id=chat_id,
        agent=agent,
        chat_widget=widget,
        _manual_compaction_thread=object(),
        _manual_compaction_cancel=object(),
        _manual_compaction_started_at=123.0,
        _refresh_operator_telemetry=lambda refresh_context=False: None,
    )


def test_completion_prunes_only_when_exact_snapshot_is_still_live():
    snapshot = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
    ]
    retained = snapshot[2:]
    fake = _completion_fake(snapshot)
    result = {
        "status": "success", "chat_id": 7,
        "history_snapshot": snapshot,
        "retained_history": retained,
        "compacted_messages": 2,
        "compacted_tokens": 500,
    }

    LuminaWindow._on_manual_compaction_finished(fake, result)

    assert fake.agent.ctx.history == retained
    assert fake.agent.ctx._last_usage_snapshot is None
    assert fake._manual_compaction_thread is None
    assert "full transcript unchanged" in fake.chat_widget.notices[-1]


def test_completion_never_prunes_a_different_live_context():
    snapshot = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "keep1"},
        {"role": "user", "content": "keep2"},
    ]
    current = [{"role": "user", "content": "different chat"}]
    fake = _completion_fake(current, chat_id=99)
    result = {
        "status": "success", "chat_id": 7,
        "history_snapshot": snapshot,
        "retained_history": snapshot[2:],
        "compacted_messages": 2,
        "compacted_tokens": 200,
    }

    LuminaWindow._on_manual_compaction_finished(fake, result)

    assert fake.agent.ctx.history == current
    assert "take effect when that chat is reopened" in fake.chat_widget.notices[-1]
