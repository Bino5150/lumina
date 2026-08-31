"""
tests/test_context_transaction_ui.py -- CONTEXT-LIFECYCLE-A5I: ui/main_window.py
wiring proof for core/context_transaction.py.

Companion to tests/test_context_transaction.py (which covers the
transaction itself against a neutral _FakeGenerationOwner, no Qt at all).
This file proves the two things that live in ui/main_window.py:

1. The D4 fail-closed admission guard (_chat_switch_admitted()) actually
   rejects _new_chat()/_clear_chat()/_load_chat() while a foreground turn
   is running -- the exact pre-A5I gap CONTEXT-LIFECYCLE-A5D's source-vet
   documented (no worker.isRunning() check existed on any of the three).
2. LuminaWindow's four-method generation-owner protocol (current_chat_id/
   current_generation/bump/live_ctx) is wired correctly and lets a real
   caller run core.context_transaction.deliberate_reconstruct(chat_id,
   window) end to end.

Same lightweight-fake convention as tests/test_manual_compaction_ui.py:
unbound LuminaWindow methods called directly against a types.SimpleNamespace
shaped like enough of a real window -- no QApplication/real LuminaWindow
construction needed, since nothing here instantiates a QWidget.
"""
import os
import types

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import config
import core.context_checkpoints as cc
import core.context_transaction as ct
import tools.memory as memory
import tools.palace as palace
from core.context import ContextManager
from core.context_reconstruction import reconstruct_chat_context
from core.flight_recorder import FlightRecorder
from ui.main_window import LuminaWindow


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "lumina.db"))
    memory.init_chat_db()
    palace.init_palace_db()
    return tmp_path


class _ChatWidget:
    def __init__(self):
        self.rendered = []
        self.notices = []

    def clear_messages(self):
        self.rendered.clear()

    def add_user_message(self, content, mode="passive"):
        self.rendered.append(("user", content))

    def add_system_message(self, text):
        self.notices.append(text)

    def add_operator_message(self, text):
        self.notices.append(text)

    def scroll_to_bottom_now(self):
        pass

    def create_live_bubble(self):
        class _Bubble:
            def __init__(self, rendered):
                self._response_text = ""
                self._rendered = rendered

            def finalize(self):
                self._rendered.append(("assistant", self._response_text))
        return _Bubble(self.rendered)


class _RunningWorker:
    def isRunning(self):
        return True


def _fake_window(chat_id=None, ctx=None):
    """types.SimpleNamespace stores instance attributes directly -- a plain
    function assigned that way is NOT auto-bound as a method (the
    descriptor protocol only fires for attributes found via the class,
    not the instance __dict__). types.MethodType binds each of
    LuminaWindow's real, unmodified methods to this fake explicitly, so
    self.method() calls inside _load_chat()/_new_chat()/_clear_chat() and
    the generation-owner protocol behave exactly as they would on a real
    LuminaWindow instance."""
    fake = types.SimpleNamespace(
        _current_chat_id=chat_id,
        _prefs={},
        agent=types.SimpleNamespace(ctx=ctx if ctx is not None else ContextManager()),
        chat_widget=_ChatWidget(),
        _refresh_chat_list=lambda: None,
        persona_combo=types.SimpleNamespace(currentData=lambda: None),
        worker=None,
        _context_generation=ct.ContextGeneration(),
    )
    fake._chat_switch_admitted = types.MethodType(LuminaWindow._chat_switch_admitted, fake)
    fake.current_chat_id = types.MethodType(LuminaWindow.current_chat_id, fake)
    fake.current_generation = types.MethodType(LuminaWindow.current_generation, fake)
    fake.bump = types.MethodType(LuminaWindow.bump, fake)
    fake.live_ctx = types.MethodType(LuminaWindow.live_ctx, fake)
    return fake


def _seed_chat(messages=()):
    chat_id = memory.create_chat("test chat")
    for role, content in messages:
        memory.save_chat_message(chat_id, role, content)
    return chat_id


# ── D4 fail-closed admission guard ──────────────────────────────────────

def test_load_chat_rejected_while_worker_running_leaves_ctx_untouched():
    chat_id = _seed_chat([("user", "u1"), ("assistant", "a1")])
    other_chat_id = _seed_chat([("user", "u2")])
    fake = _fake_window(chat_id=other_chat_id)
    fake.worker = _RunningWorker()
    old_history = fake.agent.ctx.history

    LuminaWindow._load_chat(fake, chat_id)

    assert fake._current_chat_id == other_chat_id  # never switched
    assert fake.agent.ctx.history is old_history    # never cleared/reassigned
    assert fake.chat_widget.notices  # operator was told why
    assert fake._context_generation.current() == 0  # never bumped


def test_new_chat_rejected_while_worker_running():
    fake = _fake_window(chat_id=42)
    fake.worker = _RunningWorker()
    old_chat_id = fake._current_chat_id

    LuminaWindow._new_chat(fake)

    assert fake._current_chat_id == old_chat_id
    assert fake._context_generation.current() == 0


def test_clear_chat_rejected_while_worker_running():
    fake = _fake_window(chat_id=42)
    fake.agent.ctx.add_user("hello")
    fake.worker = _RunningWorker()
    history_before = list(fake.agent.ctx.history)

    LuminaWindow._clear_chat(fake)

    assert fake.agent.ctx.history == history_before
    assert fake._context_generation.current() == 0


def test_load_chat_admitted_when_worker_idle_bumps_generation():
    chat_id = _seed_chat([("user", "u1"), ("assistant", "a1")])
    fake = _fake_window(chat_id=None)
    assert fake.worker is None  # idle

    LuminaWindow._load_chat(fake, chat_id)

    assert fake._current_chat_id == chat_id
    assert fake._context_generation.current() == 1
    assert fake.agent.ctx.history == [
        {"role": "user", "content": "u1"}, {"role": "assistant", "content": "a1"},
    ]


def test_new_clear_chat_admitted_when_worker_idle_bump_generation():
    fake = _fake_window(chat_id=None)

    LuminaWindow._new_chat(fake)
    assert fake._context_generation.current() == 1

    LuminaWindow._clear_chat(fake)
    assert fake._context_generation.current() == 2


# ── generation-owner protocol wired on LuminaWindow ─────────────────────

def test_luminawindow_protocol_methods_proxy_correctly():
    ctx = ContextManager()
    fake = _fake_window(chat_id=7, ctx=ctx)

    assert LuminaWindow.current_chat_id(fake) == 7
    assert LuminaWindow.current_generation(fake) == 0
    assert LuminaWindow.bump(fake) == 1
    assert LuminaWindow.current_generation(fake) == 1
    assert LuminaWindow.live_ctx(fake) is ctx


def test_deliberate_reconstruct_against_luminawindow_shaped_owner(tmp_path):
    """End-to-end proof that core.context_transaction.deliberate_reconstruct()
    runs against a real ui.main_window.LuminaWindow-shaped caller -- not
    just the neutral _FakeGenerationOwner in test_context_transaction.py --
    satisfying the mission's 'some caller must exist for A5I to be
    testable end-to-end' requirement without a user-facing button (A6)."""
    chat_id = _seed_chat([("user", "u1"), ("assistant", "a1")])
    fp = reconstruct_chat_context(chat_id).durable_spine_fingerprint
    checkpoint = cc.begin_checkpoint(chat_id, fp, 0)
    payload = {
        "schema_version": 1, "machine_facts": [],
        "reported": [{"id": "r-1", "category": "objective", "statement": "keep going",
                       "evidence_refs": [], "status": "unresolved"}],
        "inferred": [],
    }
    cc.finalize_checkpoint(checkpoint.id, chat_id, fp, payload_version=1, payload=payload)

    fake = _fake_window(chat_id=chat_id)
    recorder = FlightRecorder(db_path=str(tmp_path / "flight.db"))

    result = ct.deliberate_reconstruct(chat_id, fake, recorder=recorder)

    assert result.chat_id == chat_id
    assert fake.agent.ctx.history[-1]["role"] == "assistant"
    assert "keep going" in fake.agent.ctx.history[-1]["content"]
    assert fake._context_generation.current() == 1
