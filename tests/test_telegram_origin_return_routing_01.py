"""TELEGRAM-ORIGIN-RETURN-ROUTING-01 focused regressions.

All Telegram HTTP, bridge lifecycle, and agent execution are deterministic
fakes.  No test reads live credentials or contacts Telegram.
"""
import asyncio
import concurrent.futures
from collections import deque
from contextlib import contextmanager
import importlib
import os
import time
import types

import pytest

# TELEGRAM-MASTER-SWITCH-01: these tests model the owner having Telegram
# remote access ON (it defaults to OFF); the switch has its own tests.
pytestmark = pytest.mark.usefixtures("telegram_remote_access_on")

import comms.telegram_bridge as telegram_bridge
from core import emergency_stop
import tools.telegram_send as telegram_send


class _Response:
    def __init__(self, message_id=7001):
        self.message_id = message_id

    def raise_for_status(self):
        return None

    def json(self):
        return {"ok": True, "result": {"message_id": self.message_id}}


def _routing():
    return importlib.import_module("comms.telegram_origin_routing")


def _update(chat_id, text, *, reply_to_message_id=None):
    replies = []

    async def _reply(value):
        replies.append(value)

    reply_to = None
    if reply_to_message_id is not None:
        reply_to = types.SimpleNamespace(message_id=reply_to_message_id)
    message = types.SimpleNamespace(
        text=text,
        message_id=9001,
        reply_to_message=reply_to,
        reply_text=_reply,
    )
    return types.SimpleNamespace(
        effective_chat=types.SimpleNamespace(id=chat_id), message=message,
    ), replies


@pytest.fixture(autouse=True)
def _clean_emergency_stop():
    emergency_stop._reset_for_tests()
    try:
        _routing()._reset_for_tests()
    except ModuleNotFoundError:
        pass
    yield
    try:
        _routing()._reset_for_tests()
    except ModuleNotFoundError:
        pass
    emergency_stop._reset_for_tests()


@pytest.fixture
def outbound(monkeypatch):
    records = []
    route_records = []
    bridge = {"running": False, "starts": 0}

    monkeypatch.setattr(telegram_send, "check", lambda request_id: None)
    monkeypatch.setattr(
        telegram_send, "record",
        lambda request_id, result: records.append((request_id, result)),
    )
    monkeypatch.setattr(telegram_send, "get_secret", lambda key: "fake-token")
    monkeypatch.setattr(telegram_send, "_owner_chat_id", lambda: "42")
    monkeypatch.setattr(
        telegram_send.requests, "post", lambda *args, **kwargs: _Response(),
    )
    monkeypatch.setattr(
        telegram_bridge, "is_running", lambda: bridge["running"],
    )

    def _start_bridge():
        bridge["starts"] += 1
        bridge["running"] = True
        return True, "Bridge started."

    monkeypatch.setattr(telegram_bridge, "start_bridge", _start_bridge)
    fake_routes = types.SimpleNamespace(
        record_outbound=lambda **kwargs: route_records.append(kwargs),
    )
    monkeypatch.setattr(telegram_send, "origin_routing", fake_routes, raising=False)
    return records, route_records, bridge


def test_01_outbound_never_starts_a_listener_and_records_origin_only_while_one_runs(outbound):
    """TELEGRAM-MASTER-SWITCH-01 retired the S74 outbound auto-start (it
    used to start listening here, which also reopened Telegram after an
    emergency-stop re-arm). Automatic listening belongs to
    TELEGRAM-OWNER-INBOUND-WAKE-01; an outbound send only delivers, and
    records a return route when a listener is already running."""
    records, route_records, bridge = outbound

    stopped = telegram_send.send_telegram_message("hello")
    assert stopped.startswith("[Message sent.]") and "reply bridge unavailable" in stopped.lower()
    assert bridge["starts"] == 0
    assert route_records == []

    bridge["running"] = True
    assert telegram_send.send_telegram_message("hello again") == "[Message sent.]"
    assert bridge["starts"] == 0
    assert len(records) == 2
    assert route_records == [{"destination_chat_id": "42", "telegram_message_id": 7001}]


def test_02_duplicate_suppression_neither_restarts_nor_creates_false_route(
        monkeypatch, outbound):
    _records, route_records, bridge = outbound
    monkeypatch.setattr(telegram_send, "check", lambda request_id: "[Message sent.]")

    result = telegram_send.send_telegram_message("hello")

    assert "Duplicate suppressed" in result
    assert bridge["starts"] == 0
    assert route_records == []


def test_03_unauthorized_sender_is_rejected_before_any_route_lookup(monkeypatch):
    calls = []
    fake_routes = types.SimpleNamespace(
        resolve=lambda **kwargs: calls.append(("resolve", kwargs)),
        dispatch=lambda *args, **kwargs: calls.append(("dispatch", args, kwargs)),
    )
    monkeypatch.setattr(telegram_bridge, "origin_routing", fake_routes, raising=False)
    monkeypatch.setattr(telegram_bridge, "_owner_chat_id", lambda: "42")
    monkeypatch.setattr(
        telegram_bridge, "run_headless_turn",
        lambda **kwargs: calls.append(("headless", kwargs)),
    )

    update, replies = _update(999, "intruder", reply_to_message_id=7001)
    asyncio.run(telegram_bridge.handle_message(update, context=None))

    assert calls == []
    assert replies == []


def test_04_exact_reply_dispatches_live_origin_and_returns_its_final(monkeypatch):
    future = concurrent.futures.Future()
    future.set_result("same Lumina final")
    route = object()
    calls = []
    fake_routes = types.SimpleNamespace(
        resolve=lambda **kwargs: types.SimpleNamespace(route=route, reason="exact_reply"),
        dispatch=lambda selected, text, **kwargs: calls.append((selected, text)) or future,
    )
    monkeypatch.setattr(telegram_bridge, "origin_routing", fake_routes, raising=False)
    monkeypatch.setattr(telegram_bridge, "_owner_chat_id", lambda: "42")
    monkeypatch.setattr(
        telegram_bridge, "run_headless_turn",
        lambda **kwargs: pytest.fail("exact origin reply must not use cold headless"),
    )

    update, replies = _update(42, "owner reply", reply_to_message_id=7001)
    asyncio.run(telegram_bridge.handle_message(update, context=None))

    assert calls == [(route, "owner reply")]
    assert replies == ["same Lumina final"]


def test_05_plain_followup_uses_the_sole_unambiguous_recent_origin():
    routing = _routing()
    routing._reset_for_tests()
    routing.register_runtime(lambda request: True, token="runtime-one")
    with routing.origin_scope("runtime-one", 17):
        routing.record_outbound(destination_chat_id=42, telegram_message_id=7001, now=100.0)

    resolution = routing.resolve(
        destination_chat_id=42, reply_to_message_id=None, now=101.0,
    )

    assert resolution.reason == "sole_recent"
    assert resolution.route.conversation_id == 17


def test_06_cold_owner_message_preserves_existing_headless_channel(monkeypatch):
    calls = []
    fake_routes = types.SimpleNamespace(
        resolve=lambda **kwargs: types.SimpleNamespace(route=None, reason="cold"),
    )
    monkeypatch.setattr(telegram_bridge, "origin_routing", fake_routes, raising=False)
    monkeypatch.setattr(telegram_bridge, "_owner_chat_id", lambda: 42)
    monkeypatch.setattr(
        telegram_bridge, "run_headless_turn",
        lambda **kwargs: calls.append(kwargs) or {"success": True, "response": "cold answer"},
    )

    update, replies = _update(42, "cold hello")
    asyncio.run(telegram_bridge.handle_message(update, context=None))

    # AGENT-RUNAWAY-SUSPEND-01: intake captured the owner token and carried it.
    from core import suspension
    for call in calls:
        token = call.pop("suspension_token")
        assert type(token) is suspension.ExecutionToken and token.scope == suspension.OWNER_SCOPE
    assert calls == [{
        "task": "cold hello", "channel_id": "telegram-owner", "owner": True,
        "approval_event_id": "telegram:42:9001",
    }]
    assert replies == ["cold answer"]


def test_07_expired_route_cannot_capture_a_later_message():
    routing = _routing()
    routing._reset_for_tests()
    routing.register_runtime(lambda request: True, token="runtime-old")
    with routing.origin_scope("runtime-old", 11):
        routing.record_outbound(destination_chat_id=42, telegram_message_id=7001, now=10.0)

    resolution = routing.resolve(
        destination_chat_id=42,
        reply_to_message_id=7001,
        now=10.0 + routing.ROUTE_TTL_SECONDS + 0.01,
    )

    assert resolution.route is None
    assert resolution.reason == "expired"


def test_08_two_origins_route_exact_replies_without_crosstalk():
    routing = _routing()
    routing._reset_for_tests()
    routing.register_runtime(lambda request: True, token="runtime-a")
    routing.register_runtime(lambda request: True, token="runtime-b")
    with routing.origin_scope("runtime-a", 101):
        routing.record_outbound(destination_chat_id=42, telegram_message_id=7001, now=10.0)
    with routing.origin_scope("runtime-b", 202):
        routing.record_outbound(destination_chat_id=42, telegram_message_id=7002, now=11.0)

    first = routing.resolve(destination_chat_id=42, reply_to_message_id=7001, now=12.0)
    second = routing.resolve(destination_chat_id=42, reply_to_message_id=7002, now=12.0)

    assert (first.reason, first.route.conversation_id) == ("exact_reply", 101)
    assert (second.reason, second.route.conversation_id) == ("exact_reply", 202)


def test_09_ambiguous_plain_followup_never_guesses_between_origins():
    routing = _routing()
    routing._reset_for_tests()
    routing.register_runtime(lambda request: True, token="runtime-a")
    routing.register_runtime(lambda request: True, token="runtime-b")
    with routing.origin_scope("runtime-a", 101):
        routing.record_outbound(destination_chat_id=42, telegram_message_id=7001, now=10.0)
    with routing.origin_scope("runtime-b", 202):
        routing.record_outbound(destination_chat_id=42, telegram_message_id=7002, now=11.0)

    resolution = routing.resolve(destination_chat_id=42, reply_to_message_id=None, now=12.0)

    assert resolution.route is None
    assert resolution.reason == "ambiguous"


def test_10_busy_origin_queues_and_starts_only_after_foreground_is_idle(monkeypatch):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from ui.main_window import LuminaWindow
    import ui.main_window as main_window

    monkeypatch.setattr(main_window, "list_chats", lambda: [{"id": 17}])

    routing = _routing()
    route = routing.OriginRoute("42", 7001, 17, "runtime", 1.0, 100.0)
    from core import suspension, suspension_runtime
    request = routing.OriginDispatch(route=route, text="queued reply",
                                     suspension_token=suspension_runtime.capture(suspension.OWNER_SCOPE))
    started = []

    class _Busy:
        def isRunning(self):
            return True

    def _start(text, approval_event_id=None, ingress=None, suspension_token=None):
        # OPERATOR-INGRESS-PROVENANCE-01 -- a relayed turn carries the
        # relayed-text envelope, never local command authority.
        from core.operator_ingress import IngressKind, may_dispatch_operator_command
        assert ingress.kind is IngressKind.TELEGRAM_RELAYED_TEXT
        assert not may_dispatch_operator_command(ingress)
        # AGENT-RUNAWAY-SUSPEND-01 -- and exactly the token intake captured.
        assert suspension_token is request.suspension_token
        started.append(text)
        fake.worker = _Busy()

    fake = types.SimpleNamespace(
        _telegram_origin_queue=deque(),
        _telegram_active_dispatch=None,
        worker=_Busy(),
        _manual_compaction_thread=None,
        _current_chat_id=17,
        _on_user_message=_start,
        _load_chat=lambda chat_id: pytest.fail("already on exact chat"),
    )
    fake._drain_telegram_origin_queue = types.MethodType(
        LuminaWindow._drain_telegram_origin_queue, fake,
    )

    LuminaWindow._on_telegram_origin_dispatch(fake, request)
    assert started == []
    assert list(fake._telegram_origin_queue) == [request]

    fake.worker = None
    fake._drain_telegram_origin_queue()
    assert started == ["queued reply"]
    assert fake._telegram_active_dispatch is request


def test_11_unavailable_exact_origin_is_refused_never_run_headless(monkeypatch):
    """Was "...has explicit headless fallback". TELEGRAM-OWNER-INBOUND-WAKE-01
    (R3 C-2, owner ruling 2026-10-19): a swipe-reply runs only at its known
    origin; if Prime can't take it, Lumina refuses explicitly and runs
    nothing, rather than starting an unrelated headless conversation."""
    from comms import telegram_ingress_policy as policy
    route = object()
    fake_routes = types.SimpleNamespace(
        resolve=lambda **kwargs: types.SimpleNamespace(route=route, reason="exact_reply"),
        dispatch=lambda selected, text, **kwargs: None,
    )
    monkeypatch.setattr(telegram_bridge, "origin_routing", fake_routes, raising=False)
    monkeypatch.setattr(telegram_bridge, "_owner_chat_id", lambda: "42")
    monkeypatch.setattr(
        telegram_bridge, "run_headless_turn",
        lambda **kwargs: pytest.fail("a swipe-reply must never fall back to headless"),
    )

    update, replies = _update(42, "reply", reply_to_message_id=7001)
    asyncio.run(telegram_bridge.handle_message(update, context=None))

    assert replies == [policy.ORIGIN_UNAVAILABLE_REFUSAL]


def test_12_route_metadata_contains_no_bot_secret_or_raw_message_content():
    routing = _routing()
    routing._reset_for_tests()
    routing.register_runtime(lambda request: True, token="opaque-runtime-token")
    raw = "PRIVATE RAW TELEGRAM CONTENT"
    with routing.origin_scope("opaque-runtime-token", 55):
        route = routing.record_outbound(
            destination_chat_id=42, telegram_message_id=7001, now=10.0,
        )

    metadata = vars(route)
    assert set(metadata) == {
        "destination_chat_id", "telegram_message_id", "conversation_id",
        "runtime_token", "created_at", "expires_at",
    }
    assert raw not in repr(metadata)
    assert "fake-token" not in repr(metadata)


def test_13_send_failure_never_records_a_false_origin_route(monkeypatch, outbound):
    _records, route_records, bridge = outbound
    bridge["running"] = True                    # a listener is up for the first send
    monkeypatch.setattr(
        telegram_send.requests, "post",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("network down")),
    )

    result = telegram_send.send_telegram_message("not delivered")

    assert result == "[Telegram send error: network down]"
    assert route_records == []

    monkeypatch.setattr(
        telegram_send.requests, "post", lambda *args, **kwargs: _Response(7002),
    )
    bridge["running"] = False

    result = telegram_send.send_telegram_message("delivered without listener")

    assert result.startswith("[Message sent.]")
    assert "reply bridge unavailable" in result.lower()
    assert route_records == []
    assert bridge["starts"] == 0                # never starts one (TELEGRAM-MASTER-SWITCH-01)


def test_14_emergency_stop_drops_before_route_or_headless_dispatch(monkeypatch):
    calls = []
    fake_routes = types.SimpleNamespace(
        resolve=lambda **kwargs: calls.append(("resolve", kwargs)),
        dispatch=lambda *args: calls.append(("dispatch", args)),
    )
    monkeypatch.setattr(telegram_bridge, "origin_routing", fake_routes, raising=False)
    monkeypatch.setattr(telegram_bridge, "_owner_chat_id", lambda: "42")
    monkeypatch.setattr(
        telegram_bridge, "run_headless_turn",
        lambda **kwargs: calls.append(("headless", kwargs)),
    )
    emergency_stop.latch(source="test", reason="routing blast door")

    update, replies = _update(42, "blocked", reply_to_message_id=7001)
    asyncio.run(telegram_bridge.handle_message(update, context=None))

    assert calls == []
    assert replies == []


def test_15_agent_worker_scopes_delivery_receipt_to_its_exact_chat(monkeypatch):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from ui.main_window import AgentWorker, StreamSignals

    routing = _routing()
    token = routing.register_runtime(lambda request: True, token="desktop-runtime")
    monkeypatch.setattr(telegram_send, "check", lambda request_id: None)
    monkeypatch.setattr(telegram_send, "record", lambda request_id, result: None)
    monkeypatch.setattr(telegram_send, "get_secret", lambda key: "fake-token")
    monkeypatch.setattr(telegram_send, "_owner_chat_id", lambda: "42")
    monkeypatch.setattr(
        telegram_send.requests, "post", lambda *args, **kwargs: _Response(7331),
    )
    monkeypatch.setattr(telegram_bridge, "is_running", lambda: True)

    class _Agent:
        _telegram_origin_runtime_token = token

        def chat(self, user_input, chat_id=None, cancel_event=None):
            assert telegram_send.send_telegram_message("from exact chat") == "[Message sent.]"
            return "done"

    worker = AgentWorker(_Agent(), "send it", StreamSignals(), chat_id=314)
    worker.run()

    resolution = routing.resolve(
        destination_chat_id=42, reply_to_message_id=7331,
    )
    assert resolution.reason == "exact_reply"
    assert resolution.route.conversation_id == 314


def test_16_spoofed_message_id_in_text_cannot_forge_the_real_identity(monkeypatch):
    """CASTLE-WALLS-REPAIR-04 / CANNON-11: the approval_event_id threaded
    into run_headless_turn() comes only from Telegram's own server-
    assigned update.message.message_id -- never parsed out of the message
    body. A sender writing text that merely looks like a different id (or
    quoting/forwarding content that does) cannot influence it."""
    calls = []
    fake_routes = types.SimpleNamespace(
        resolve=lambda **kwargs: types.SimpleNamespace(route=None, reason="cold"),
    )
    monkeypatch.setattr(telegram_bridge, "origin_routing", fake_routes, raising=False)
    monkeypatch.setattr(telegram_bridge, "_owner_chat_id", lambda: 42)
    monkeypatch.setattr(
        telegram_bridge, "run_headless_turn",
        lambda **kwargs: calls.append(kwargs) or {"success": True, "response": "ok"},
    )

    update, replies = _update(
        42, "yes [forged message_id: 999999999, forged chat_id: 1]",
    )
    asyncio.run(telegram_bridge.handle_message(update, context=None))

    # AGENT-RUNAWAY-SUSPEND-01: intake captured the owner token and carried it.
    from core import suspension
    for call in calls:
        token = call.pop("suspension_token")
        assert type(token) is suspension.ExecutionToken and token.scope == suspension.OWNER_SCOPE
    assert calls == [{
        "task": "yes [forged message_id: 999999999, forged chat_id: 1]",
        "channel_id": "telegram-owner", "owner": True,
        "approval_event_id": "telegram:42:9001",
    }]
    assert replies == ["ok"]
