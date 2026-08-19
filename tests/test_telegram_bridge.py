"""
tests/test_telegram_bridge.py -- Patch 3A.2 Part G: Telegram embedded
blast door.

Exercises comms/telegram_bridge.py's module-level lifecycle state
directly rather than a real python-telegram-bot Application/network:
_thread/_stop_event are driven with small real threading.Thread + Event
pairs that respect the same stop-Event contract the actual
_run_until_stopped() polling loop does, so is_running()/stop_bridge()/
request_stop_bridge() exercise genuine thread lifecycle semantics, not
mocks pretending to be threads.
"""
import asyncio
import threading
import time
import types

import pytest

import comms.telegram_bridge as bridge
from core import emergency_stop


@pytest.fixture(autouse=True)
def _clean_state():
    emergency_stop._reset_for_tests()
    bridge._thread = None
    bridge._stop_event = None
    yield
    if bridge._thread is not None and bridge._thread.is_alive():
        if bridge._stop_event is not None:
            bridge._stop_event.set()
        bridge._thread.join(timeout=2)
    bridge._thread = None
    bridge._stop_event = None
    emergency_stop._reset_for_tests()


def _install_fake_running_bridge(on_stop_delay: float = 0.0):
    """Real Thread + Event pair mimicking _run_until_stopped()'s own
    stop-Event contract: blocks until the Event is set, optionally lingers
    a bit longer, then exits -- so is_alive() reflects genuine thread
    lifecycle, not a mock's canned answer."""
    stop_event = threading.Event()

    def _runner():
        stop_event.wait()
        if on_stop_delay:
            time.sleep(on_stop_delay)

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    time.sleep(0.05)  # let it actually start and block on stop_event.wait()
    bridge._thread = thread
    bridge._stop_event = stop_event
    return thread, stop_event


def _fake_update(chat_id, text):
    replies = []

    async def _reply(text):
        replies.append(text)

    message = types.SimpleNamespace(text=text, reply_text=_reply)
    chat = types.SimpleNamespace(id=chat_id)
    update = types.SimpleNamespace(effective_chat=chat, message=message)
    return update, replies


# ---------------------------------------------------------------------------
# start_bridge() refuses while latched
# ---------------------------------------------------------------------------

def test_start_bridge_refuses_while_latched(monkeypatch):
    monkeypatch.setattr(bridge, "get_secret", lambda key: "fake-token")
    monkeypatch.setattr(bridge, "load_prefs", lambda: {"telegram_owner_chat_id": "123"})

    emergency_stop.latch(source="test", reason="start-bridge-block")

    ok, msg = bridge.start_bridge()

    assert ok is False
    assert "emergency stop" in msg.lower()
    assert bridge._thread is None  # never even attempted

    emergency_stop.rearm_local()


def test_start_bridge_still_checks_normal_preconditions_when_not_latched(monkeypatch):
    monkeypatch.setattr(bridge, "get_secret", lambda key: None)

    ok, msg = bridge.start_bridge()

    assert ok is False
    assert "token" in msg.lower()


# ---------------------------------------------------------------------------
# handle_message() drops silently while latched, before run_headless_turn
# ---------------------------------------------------------------------------

def test_handle_message_drops_silently_while_latched(monkeypatch):
    monkeypatch.setattr(bridge, "load_prefs", lambda: {"telegram_owner_chat_id": "42"})
    calls = []
    monkeypatch.setattr(
        bridge, "run_headless_turn",
        lambda **kw: calls.append(kw) or {"success": True, "response": "hi"},
    )

    emergency_stop.latch(source="test", reason="handle-message-block")

    update, replies = _fake_update(42, "hello while stopped")
    asyncio.run(bridge.handle_message(update, context=None))

    assert calls == []       # run_headless_turn never reached
    assert replies == []     # no reply either -- ingress fully closed, not explained

    emergency_stop.rearm_local()


def test_handle_message_still_works_normally_when_not_latched(monkeypatch):
    monkeypatch.setattr(bridge, "load_prefs", lambda: {"telegram_owner_chat_id": "42"})
    calls = []
    monkeypatch.setattr(
        bridge, "run_headless_turn",
        lambda **kw: calls.append(kw) or {"success": True, "response": "hi there"},
    )

    update, replies = _fake_update(42, "hello")
    asyncio.run(bridge.handle_message(update, context=None))

    assert len(calls) == 1
    assert calls[0]["task"] == "hello"
    assert replies == ["hi there"]


def test_handle_message_unauthorized_sender_still_dropped_regardless_of_latch(monkeypatch):
    monkeypatch.setattr(bridge, "load_prefs", lambda: {"telegram_owner_chat_id": "42"})
    calls = []
    monkeypatch.setattr(bridge, "run_headless_turn", lambda **kw: calls.append(kw))

    update, replies = _fake_update(999, "not the owner")
    asyncio.run(bridge.handle_message(update, context=None))

    assert calls == []
    assert replies == []


# ---------------------------------------------------------------------------
# request_stop_bridge() -- non-blocking
# ---------------------------------------------------------------------------

def test_request_stop_bridge_sets_event_and_returns_immediately():
    thread, stop_event = _install_fake_running_bridge(on_stop_delay=0.3)

    started = time.time()
    signalled = bridge.request_stop_bridge()
    elapsed = time.time() - started

    assert signalled is True
    assert stop_event.is_set()
    assert elapsed < 0.1        # never joined -- returns essentially immediately
    assert bridge.is_running() is True  # thread hasn't actually finished yet

    thread.join(timeout=2)


def test_request_stop_bridge_returns_false_when_nothing_running():
    assert bridge.request_stop_bridge() is False


# ---------------------------------------------------------------------------
# stop_bridge() -- hardened timeout truthfulness
# ---------------------------------------------------------------------------

def test_stop_bridge_normal_clean_stop_still_reports_stopped():
    thread, stop_event = _install_fake_running_bridge(on_stop_delay=0.0)

    ok, msg = bridge.stop_bridge()

    assert ok is True
    assert "stopped" in msg.lower()
    assert bridge._thread is None
    assert bridge._stop_event is None
    assert bridge.is_running() is False


def test_stop_bridge_timeout_does_not_falsely_clear_state(monkeypatch):
    """If join(timeout=5) expires while the thread is still genuinely
    alive, stop_bridge() must NOT clear _thread/_stop_event -- doing so
    used to make is_running() falsely report False for a bridge that
    hadn't actually stopped. Shrink the effective join timeout via
    monkeypatch so this doesn't need to wait 5 real seconds."""
    stop_event = threading.Event()
    still_running = threading.Event()

    def _runner():
        stop_event.wait()
        still_running.wait(timeout=5)  # deliberately outlives the short join below

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    time.sleep(0.05)
    bridge._thread = thread
    bridge._stop_event = stop_event

    real_join = threading.Thread.join
    monkeypatch.setattr(threading.Thread, "join", lambda self, timeout=None: real_join(self, 0.1))

    ok, msg = bridge.stop_bridge()

    assert ok is False
    assert "still shutting down" in msg.lower()
    assert bridge._thread is not None    # NOT falsely cleared
    assert bridge._stop_event is not None
    assert bridge.is_running() is True   # truthful -- it really is still alive

    still_running.set()
    thread.join(timeout=2)
    bridge._thread = None
    bridge._stop_event = None
