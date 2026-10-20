"""TELEGRAM-OWNER-INBOUND-WAKE-01 -- Lumina listens whenever she runs and
Telegram remote access is ON.

Design: ~/lumina/reports/TELEGRAM-OWNER-INBOUND-WAKE-01_DESIGN_R2_2026-10-19.md
plus its R3 contract addendum. The listener runs exactly when the desktop is
running, the master switch (TELEGRAM-MASTER-SWITCH-01) is ON, the owner
configuration is valid, the emergency stop is not latched, and this process
holds the single-consumer lock.

Covered here (no Qt, so these block in the main CI gate):
  * the pure ingress policy (token / chat id validation, staleness, refusal cap);
  * the inbound gate order: update type, previous tenant, transport
    staleness, durable duplicate claim -- before routing, any turn, any model;
  * C-1 remote-access authority revalidated at final admission;
  * C-2 swipe-replies run only at their known origin, else an explicit refusal;
  * C-3 startup validation, and /resume's gates without WAKE's duplicate claim;
  * C-4 bounded shutdown, single-consumer lock lifecycle (real separate
    processes), no headless turn while closing;
  * the polling supervisor: bootstrap retry, InvalidToken, Conflict yield;
  * run_gui()'s launch start, and no other entry point starting a listener.
The real LuminaWindow paths are in test_telegram_owner_inbound_wake_01_qt.py.
"""
import asyncio
import datetime
import fcntl
import inspect
import json
import os
import pathlib
import signal
import subprocess
import sys
import threading
import time
import types

import pytest

import comms.telegram_access as access
import comms.telegram_bridge as telegram_bridge
import comms.telegram_recovery as recovery
from comms import telegram_origin_routing as routing
from core import emergency_stop, idempotency, persistence

OWNER = "42"
REPO = pathlib.Path(__file__).resolve().parents[1]


def _fake_token(bot_id="12345678"):
    # Assembled at runtime: a literal token-shaped string in the repository
    # trips GitHub secret scanning.
    return bot_id + ":" + ("Q" * 35)


@pytest.fixture(autouse=True)
def hermetic(tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(idempotency, "LEDGER_PATH", str(tmp_path / "ledger.db"))
    monkeypatch.setenv("LUMINA_RUNTIME_DIR", str(tmp_path / "runtime"))
    access._reset_for_tests()
    emergency_stop._reset_for_tests()
    routing._reset_for_tests()
    getattr(telegram_bridge, "_reset_for_tests", lambda: None)()
    yield tmp_path
    _stop_any_listener()
    access._reset_for_tests()
    emergency_stop._reset_for_tests()
    routing._reset_for_tests()
    getattr(telegram_bridge, "_reset_for_tests", lambda: None)()


def _stop_any_listener():
    thread = telegram_bridge._thread
    if thread is not None and thread.is_alive():
        telegram_bridge.request_stop_bridge()
        thread.join(timeout=5)


def _on(*, since_seconds_ago=3600):
    """The owner turned remote access ON, by default an hour ago, so the
    previous-tenant rule (TELEGRAM-MASTER-SWITCH-01) can't mask the
    transport-staleness rule under test."""
    assert access.turn_on_by_owner() is True
    if since_seconds_ago:
        prefs = json.load(open(persistence.PREFS_PATH))
        prefs[access.ON_SINCE_KEY] = time.time() - since_seconds_ago
        json.dump(prefs, open(persistence.PREFS_PATH, "w"))


def _date(seconds_ago=0):
    return datetime.datetime.fromtimestamp(int(time.time()) - seconds_ago, datetime.timezone.utc)


def _update(text, *, message_id=9001, reply_to=None, date="now", chat_id=OWNER, sent_ids=None):
    """A minimal inbound update; reply_text returns a sent Message stand-in
    with a fresh message id, as python-telegram-bot does."""
    replies = []
    counter = iter(sent_ids or range(5001, 6000))

    async def _reply(value):
        replies.append(value)
        return types.SimpleNamespace(message_id=next(counter))

    message = types.SimpleNamespace(
        text=text, message_id=message_id, reply_text=_reply,
        date=_date() if date == "now" else date,
        reply_to_message=types.SimpleNamespace(message_id=reply_to) if reply_to is not None else None,
    )
    update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=int(chat_id)),
                                   message=message, edited_message=None)
    return update, replies


def _deliver(update):
    asyncio.run(telegram_bridge.handle_message(update, context=None))


@pytest.fixture
def headless(monkeypatch):
    calls = []

    def _turn(**kwargs):
        calls.append(kwargs)
        return {"success": True, "response": f"headless answer {len(calls)}"}

    monkeypatch.setattr(telegram_bridge, "run_headless_turn", _turn)
    monkeypatch.setattr(telegram_bridge, "_owner_chat_id", lambda: OWNER)
    return calls


@pytest.fixture
def no_turns(monkeypatch):
    def _fail(*a, **k):
        pytest.fail("this update must not reach routing, a turn or a model")

    monkeypatch.setattr(telegram_bridge, "run_headless_turn", _fail)
    monkeypatch.setattr(telegram_bridge, "origin_routing",
                        types.SimpleNamespace(resolve=_fail, dispatch=_fail), raising=False)
    monkeypatch.setattr(telegram_bridge, "looks_like_operator_command", _fail)
    monkeypatch.setattr(telegram_bridge, "_owner_chat_id", lambda: OWNER)


# ══ The pure ingress policy ═══════════════════════════════════════════════

def test_a_well_formed_token_yields_only_its_public_bot_id():
    from comms import telegram_ingress_policy as policy
    assert policy.parse_bot_id(_fake_token("987654321")) == "987654321"


@pytest.mark.parametrize("token", [
    None, "", "fake-token", "fake", "12345678", "12345678:", ":" + "Q" * 35,
    "abc:" + "Q" * 35, "12345678:" + "Q" * 29, "12345678:" + "Q" * 34 + "!",
    " 12345678:" + "Q" * 35, "12345678:" + "Q" * 35 + "\n",
])
def test_a_malformed_token_has_no_bot_id(token):
    from comms import telegram_ingress_policy as policy
    assert policy.parse_bot_id(token) is None


@pytest.mark.parametrize("value, ok", [
    ("42", True), (42, True), (" 42 ", True), ("0", False), (0, False), ("-1001", False),
    (-5, False), ("abc", False), ("", False), (None, False), (True, False), (False, False), ("4.2", False),
])
def test_owner_chat_id_must_be_a_positive_integer(value, ok):
    from comms import telegram_ingress_policy as policy
    assert policy.valid_owner_chat_id(value) is ok


def _at(epoch_seconds):
    return datetime.datetime.fromtimestamp(epoch_seconds, datetime.timezone.utc)


@pytest.mark.parametrize("date_s, now, verdict", [
    (820, 1000.0, "fresh"),          # exactly 180 s: still runs
    (820, 1000.99, "fresh"),         # whole-second floor: still 180
    (819, 1000.0, "stale"),          # 181 s
    (820, 1001.0, "stale"),          # 181 s
    (1120, 1000.0, "fresh"),         # 120 s in the future: tolerated
    (1121, 1000.0, "clock_suspect"),  # 121 s in the future
])
def test_staleness_boundaries(date_s, now, verdict):
    from comms import telegram_ingress_policy as policy
    assert policy.message_age_verdict(_at(date_s), now=now) == verdict


def test_staleness_is_utc_based_not_local_time():
    from comms import telegram_ingress_policy as policy
    plus_ten = datetime.timezone(datetime.timedelta(hours=10))
    same_instant = datetime.datetime.fromtimestamp(820, plus_ten)
    assert policy.message_age_verdict(same_instant, now=1000.0) == "fresh"


def test_a_message_without_a_date_is_not_judged():
    from comms import telegram_ingress_policy as policy
    assert policy.message_age_verdict(None, now=1000.0) == "fresh"


def test_the_stale_reply_names_the_time_and_never_carries_message_text():
    from comms import telegram_ingress_policy as policy
    text = policy.stale_reply(_at(3600 * 5 + 60 * 7), now=3600 * 5 + 60 * 7 + 600)
    assert "05:07 UTC" in text and "10 min" in text and "not run" in text


def test_refusal_replies_are_capped_with_one_summary():
    from comms import telegram_ingress_policy as policy
    budget = policy.RefusalBudget()
    out = [budget.next_reply(f"r{i}") for i in range(8)]
    assert out[:5] == ["r0", "r1", "r2", "r3", "r4"]
    assert out[5] == policy.BACKLOG_SUMMARY_REPLY
    assert out[6:] == [None, None]
    budget.reset()
    assert budget.next_reply("again") == "again"


# ══ Inbound gate: stale, clock, previous tenant, edits ═══════════════════

def test_a_stale_message_never_reaches_routing_a_turn_or_a_model(no_turns):
    _on()
    secret_text = "stop the print and delete job 7"
    update, replies = _update(secret_text, date=_date(seconds_ago=181))
    _deliver(update)
    assert len(replies) == 1
    assert "not run" in replies[0] and secret_text not in replies[0]


def test_a_message_dated_in_the_future_fails_closed(no_turns):
    from comms import telegram_ingress_policy as policy
    _on()
    future = datetime.datetime.fromtimestamp(int(time.time()) + 300, datetime.timezone.utc)
    update, replies = _update("hello", date=future)
    _deliver(update)
    assert replies == [policy.CLOCK_SUSPECT_REPLY]


def test_a_fresh_message_runs(headless):
    _on()
    update, replies = _update("status?", date=_date(seconds_ago=60))
    _deliver(update)
    assert len(headless) == 1 and replies == ["headless answer 1"]


def test_backlog_refusals_are_capped_per_listener_start(no_turns):
    from comms import telegram_ingress_policy as policy
    _on()
    since = access.on_since()
    sent = []
    for i in range(4):   # previous tenant's mail
        update, replies = _update(f"old {i}", message_id=100 + i,
                                  date=datetime.datetime.fromtimestamp(int(since) - 60, datetime.timezone.utc))
        _deliver(update)
        sent += replies
    for i in range(4):   # stale after a restart while ON
        update, replies = _update(f"late {i}", message_id=200 + i, date=_date(seconds_ago=900))
        _deliver(update)
        sent += replies
    assert len(sent) == 6
    assert sent[:4] == [access.STALE_BACKLOG_REPLY] * 4
    assert sent[5] == policy.BACKLOG_SUMMARY_REPLY


def test_an_edited_message_is_refused_deliberately_without_an_exception(no_turns):
    _on()
    edited = types.SimpleNamespace(text="edited text", message_id=9001, date=_date(),
                                   reply_to_message=None)
    update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=int(OWNER)),
                                   message=None, edited_message=edited)
    _deliver(update)                      # no AttributeError, no reply, no turn


def test_the_text_handler_never_matches_an_edited_message():
    from telegram import Chat, Message, Update
    from telegram.ext import MessageHandler
    added = []
    telegram_bridge._register_handlers(types.SimpleNamespace(add_handler=added.append))
    text_handler = next(h for h in added if isinstance(h, MessageHandler))
    msg = Message(message_id=1, date=datetime.datetime.now(datetime.timezone.utc),
                  chat=Chat(id=42, type=Chat.PRIVATE), text="hello")
    assert text_handler.check_update(Update(update_id=1, message=msg))
    assert not text_handler.check_update(Update(update_id=2, edited_message=msg))


# ══ Durable duplicate claim (at most once) ═══════════════════════════════

def test_the_same_message_id_runs_once_even_across_a_restart(headless):
    _on()
    update, _ = _update("turn the lights off", message_id=777)
    _deliver(update)
    _deliver(update)                                   # transport redelivery
    telegram_bridge._reset_for_tests()                 # simulated restart:
    routing._reset_for_tests()                         # memory gone, ledger.db kept
    _deliver(update)
    assert len(headless) == 1


def test_the_same_text_with_a_new_message_id_runs_again(headless):
    _on()
    _deliver(_update("again", message_id=1)[0])
    _deliver(_update("again", message_id=2)[0])
    assert len(headless) == 2


def test_a_routed_message_that_falls_back_runs_once(headless, monkeypatch):
    _on()
    routing.register_runtime(lambda request: False, token="prime")   # Prime refuses admission
    with routing.origin_scope("prime", 17):
        routing.record_outbound(destination_chat_id=OWNER, telegram_message_id=4001)
    update, replies = _update("plain follow-up", message_id=31)
    _deliver(update)
    _deliver(update)
    assert len(headless) == 1
    assert "telegram-owner fallback" in replies[0]


def test_the_claim_namespace_is_distinct_from_the_raw_event_id(headless):
    from comms import telegram_ingress_policy as policy
    _on()
    _deliver(_update("hello", message_id=55)[0])
    raw = f"telegram:{OWNER}:55"
    assert idempotency.claim(raw) is True                          # the approval hook's key is untouched
    assert idempotency.claim(policy.inbound_claim_id(raw)) is False  # WAKE's key is spent


def test_a_claim_store_failure_fails_closed(headless, monkeypatch):
    _on()

    def _broken(*a, **k):
        raise OSError("ledger unavailable")

    monkeypatch.setattr(idempotency, "claim", _broken)
    update, replies = _update("hello")
    _deliver(update)
    assert headless == [] and replies == []


# ══ C-1: authority revalidated at final admission ═══════════════════════

def _revoke(how):
    if how == "off":
        access.turn_off("owner")
    elif how == "estop_latched":
        emergency_stop.latch(source="test", reason="test")
    elif how == "estop_latched_then_rearmed":
        emergency_stop.latch(source="test", reason="test")
        assert emergency_stop.rearm_local() is True
    elif how == "off_then_on":
        access.turn_off("owner")
        time.sleep(0.01)
        _on()
    elif how == "off_in_another_process":
        prefs = json.load(open(persistence.PREFS_PATH))
        prefs[access.PREF_KEY] = False
        json.dump(prefs, open(persistence.PREFS_PATH, "w"))


REVOCATIONS = ["off", "estop_latched", "estop_latched_then_rearmed", "off_then_on", "off_in_another_process"]


@pytest.mark.parametrize("how", REVOCATIONS)
def test_authority_snapshot_is_invalidated(how):
    _on()
    snapshot = access.capture_ingress_authority()
    assert snapshot is not None and access.ingress_authority_valid(snapshot)
    _revoke(how)
    assert access.ingress_authority_valid(snapshot) is False


def test_no_snapshot_while_off_or_latched():
    assert access.capture_ingress_authority() is None
    _on()
    emergency_stop.latch(source="test", reason="test")
    assert access.capture_ingress_authority() is None


@pytest.mark.parametrize("how", REVOCATIONS)
def test_a_revocation_after_intake_stops_the_headless_turn_at_final_admission(headless, monkeypatch, how):
    _on()
    real = telegram_bridge.looks_like_operator_command

    def _revoke_after_intake(text):          # runs after the snapshot and the claim
        _revoke(how)
        return real(text)

    monkeypatch.setattr(telegram_bridge, "looks_like_operator_command", _revoke_after_intake)
    update, replies = _update("delete the old backups")
    _deliver(update)
    assert headless == [] and replies == []


# The turn runs on the headless worker thread, and rearm_local() is main-
# thread only, so the re-armed variant is covered by the snapshot,
# final-admission and real-Qt tests instead.
@pytest.mark.parametrize("how", ["off", "estop_latched", "off_then_on", "off_in_another_process"])
def test_a_revocation_during_the_turn_suppresses_the_reply(monkeypatch, how):
    _on()
    monkeypatch.setattr(telegram_bridge, "_owner_chat_id", lambda: OWNER)

    def _turn(**kwargs):
        _revoke(how)
        return {"success": True, "response": "done"}

    monkeypatch.setattr(telegram_bridge, "run_headless_turn", _turn)
    update, replies = _update("long job")
    _deliver(update)
    assert replies == []


@pytest.mark.parametrize("how", REVOCATIONS)
def test_a_routed_dispatch_carries_a_live_admission_check(monkeypatch, how):
    _on()
    monkeypatch.setattr(telegram_bridge, "_owner_chat_id", lambda: OWNER)
    received = []

    def _prime(request):
        received.append(request)
        request.future.set_result("prime answer")
        return True

    routing.register_runtime(_prime, token="prime")
    with routing.origin_scope("prime", 17):
        routing.record_outbound(destination_chat_id=OWNER, telegram_message_id=4001)
    update, replies = _update("yes do it", reply_to=4001)
    _deliver(update)
    assert replies == ["prime answer"]
    check = received[0].admission_check
    assert callable(check) and check() is True
    _revoke(how)
    assert check() is False


def test_a_routed_request_refused_for_revoked_authority_is_silent(monkeypatch):
    _on()
    monkeypatch.setattr(telegram_bridge, "_owner_chat_id", lambda: OWNER)
    monkeypatch.setattr(telegram_bridge, "run_headless_turn",
                        lambda **k: pytest.fail("never headless"))

    def _prime(request):
        access.turn_off("owner")
        request.future.set_exception(routing.OriginUnavailable("remote access authority changed"))
        return True

    routing.register_runtime(_prime, token="prime")
    with routing.origin_scope("prime", 17):
        routing.record_outbound(destination_chat_id=OWNER, telegram_message_id=4001)
    update, replies = _update("yes do it", reply_to=4001)
    _deliver(update)
    assert replies == []


# ══ C-2: a swipe-reply never silently becomes headless ══════════════════

def test_resolve_names_an_unknown_reply():
    assert routing.resolve(destination_chat_id=OWNER, reply_to_message_id=7001).reason == "unknown_reply"


def test_resolve_recognises_a_reply_to_a_headless_answer_until_cleared():
    routing.record_headless_reply(destination_chat_id=OWNER, telegram_message_id=5001, now=10.0)
    assert routing.resolve(destination_chat_id=OWNER, reply_to_message_id=5001, now=11.0).reason == "headless_reply"
    expired = routing.resolve(destination_chat_id=OWNER, reply_to_message_id=5001,
                              now=10.0 + routing.ROUTE_TTL_SECONDS + 1)
    assert expired.reason == "expired"
    routing.record_headless_reply(destination_chat_id=OWNER, telegram_message_id=5002)
    routing.clear_routes()
    assert routing.resolve(destination_chat_id=OWNER, reply_to_message_id=5002).reason == "unknown_reply"


def test_headless_answers_never_count_as_prime_origins():
    routing.record_headless_reply(destination_chat_id=OWNER, telegram_message_id=5001)
    assert routing.resolve(destination_chat_id=OWNER).reason == "cold"


def test_a_reply_to_an_unknown_message_is_refused_explicitly(no_turns, monkeypatch):
    from comms import telegram_ingress_policy as policy
    _on()
    monkeypatch.setattr(telegram_bridge, "origin_routing", routing)
    monkeypatch.setattr(telegram_bridge, "looks_like_operator_command", lambda text: False)
    update, replies = _update("yes, go ahead", reply_to=7001)
    _deliver(update)
    assert replies == [policy.UNKNOWN_REPLY_REFUSAL]


def test_a_reply_to_an_expired_route_is_refused_explicitly(no_turns, monkeypatch):
    from comms import telegram_ingress_policy as policy
    _on()
    monkeypatch.setattr(telegram_bridge, "origin_routing", routing)
    monkeypatch.setattr(telegram_bridge, "looks_like_operator_command", lambda text: False)
    routing.register_runtime(lambda request: True, token="prime")
    with routing.origin_scope("prime", 17):
        routing.record_outbound(destination_chat_id=OWNER, telegram_message_id=4001,
                                now=time.monotonic() - routing.ROUTE_TTL_SECONDS - 5)
    update, replies = _update("yes, go ahead", reply_to=4001)
    _deliver(update)
    assert replies == [policy.EXPIRED_REPLY_REFUSAL]


def test_a_reply_whose_prime_origin_is_unavailable_is_refused_not_run_headless(no_turns, monkeypatch):
    from comms import telegram_ingress_policy as policy
    _on()
    monkeypatch.setattr(telegram_bridge, "origin_routing", routing)
    monkeypatch.setattr(telegram_bridge, "looks_like_operator_command", lambda text: False)

    def _prime(request):
        request.future.set_exception(routing.OriginUnavailable("origin chat unavailable"))
        return True

    routing.register_runtime(_prime, token="prime")
    with routing.origin_scope("prime", 17):
        routing.record_outbound(destination_chat_id=OWNER, telegram_message_id=4001)
    update, replies = _update("yes, go ahead", reply_to=4001)
    _deliver(update)
    assert replies == [policy.ORIGIN_UNAVAILABLE_REFUSAL]


def test_a_reply_whose_prime_runtime_is_gone_is_refused_not_run_headless(no_turns, monkeypatch):
    from comms import telegram_ingress_policy as policy
    _on()
    monkeypatch.setattr(telegram_bridge, "origin_routing", routing)
    monkeypatch.setattr(telegram_bridge, "looks_like_operator_command", lambda text: False)
    routing.register_runtime(lambda request: False, token="prime")   # refuses admission
    with routing.origin_scope("prime", 17):
        routing.record_outbound(destination_chat_id=OWNER, telegram_message_id=4001)
    update, replies = _update("yes, go ahead", reply_to=4001)
    _deliver(update)
    assert replies == [policy.ORIGIN_UNAVAILABLE_REFUSAL]


def test_a_reply_to_a_headless_answer_continues_with_the_headless_agent(headless):
    _on()
    first, replies = _update("what's the printer status?", message_id=1, sent_ids=[5001])
    _deliver(first)
    assert replies == ["headless answer 1"]
    follow_up, replies = _update("and the queue?", message_id=2, reply_to=5001)
    _deliver(follow_up)
    assert len(headless) == 2 and replies == ["headless answer 2"]


def test_plain_cold_and_ambiguous_messages_keep_the_s74_headless_contract(headless):
    _on()
    update, replies = _update("cold hello", message_id=1)
    _deliver(update)
    assert replies == ["headless answer 1"]
    routing.register_runtime(lambda request: True, token="a")
    routing.register_runtime(lambda request: True, token="b")
    with routing.origin_scope("a", 1):
        routing.record_outbound(destination_chat_id=OWNER, telegram_message_id=4001)
    with routing.origin_scope("b", 2):
        routing.record_outbound(destination_chat_id=OWNER, telegram_message_id=4002)
    update, replies = _update("which one?", message_id=2)
    _deliver(update)
    assert len(headless) == 2
    assert replies[0].startswith("[No unique Telegram origin; telegram-owner fallback.]")


# ══ C-3: startup configuration validation ═══════════════════════════════

@pytest.fixture
def configured(monkeypatch):
    token = _fake_token()
    monkeypatch.setattr(telegram_bridge, "get_secret", lambda key: token)
    monkeypatch.setattr(telegram_bridge, "_owner_chat_id", lambda: OWNER)
    return token


@pytest.fixture
def parked_runner(monkeypatch):
    """_run_bridge replaced by a stand-in that parks until stopped."""
    runs = []

    def _park(stop_event, token):
        runs.append(token)
        stop_event.wait(10)
        return True

    monkeypatch.setattr(telegram_bridge, "_run_bridge", _park)
    return runs


@pytest.mark.parametrize("token", ["fake-token", "12345678:short", "x" * 46])
def test_a_malformed_token_never_starts_and_is_never_echoed(monkeypatch, parked_runner, token):
    _on()
    monkeypatch.setattr(telegram_bridge, "get_secret", lambda key: token)
    monkeypatch.setattr(telegram_bridge, "_owner_chat_id", lambda: OWNER)
    ok, message = telegram_bridge.start_bridge(reason="startup")
    assert ok is False and token not in message and "malformed" in message.lower()
    assert telegram_bridge.health()["state"] == "token_malformed"
    assert parked_runner == [] and not telegram_bridge.is_running()


@pytest.mark.parametrize("chat_id", [None, "", "abc", "0", "-1001", True])
def test_an_invalid_owner_chat_id_means_not_configured(monkeypatch, parked_runner, chat_id):
    _on()
    monkeypatch.setattr(telegram_bridge, "get_secret", lambda key: _fake_token())
    monkeypatch.setattr(telegram_bridge, "_owner_chat_id", lambda: chat_id)
    ok, _ = telegram_bridge.start_bridge(reason="startup")
    assert ok is False and telegram_bridge.health()["state"] == "not_configured"
    assert parked_runner == []


def test_a_missing_token_means_not_configured(monkeypatch, parked_runner):
    _on()
    monkeypatch.setattr(telegram_bridge, "get_secret", lambda key: None)
    monkeypatch.setattr(telegram_bridge, "_owner_chat_id", lambda: OWNER)
    assert telegram_bridge.start_bridge(reason="startup")[0] is False
    assert telegram_bridge.health()["state"] == "not_configured"


def test_off_and_latched_still_refuse_every_reason(configured, parked_runner):
    for reason in ("startup", "switch_on", "config_saved", "manual"):
        assert telegram_bridge.start_bridge(reason=reason)[0] is False
    _on()
    emergency_stop.latch(source="test", reason="test")
    for reason in ("startup", "switch_on", "config_saved", "manual"):
        assert telegram_bridge.start_bridge(reason=reason)[0] is False
    assert parked_runner == []


def test_concurrent_starts_produce_exactly_one_listener(configured, parked_runner):
    _on()
    results = []
    barrier = threading.Barrier(8)

    def _start():
        barrier.wait()
        results.append(telegram_bridge.start_bridge(reason="switch_on")[0])

    threads = [threading.Thread(target=_start) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert results.count(True) == 1
    time.sleep(0.1)
    assert parked_runner == [configured]


# ══ C-4: the single-consumer lock, across real processes ════════════════

_HOLDER = r"""
import fcntl, os, sys, time
path = sys.argv[1]
fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
os.ftruncate(fd, 0); os.write(fd, str(os.getpid()).encode())
print("held", flush=True)
time.sleep(60)
"""

_PROBE = r"""
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print("busy")
else:
    print("free")
"""


def _lock_path(bot_id="12345678"):
    return pathlib.Path(os.environ["LUMINA_RUNTIME_DIR"]) / f"telegram-poller-{bot_id}.lock"


def _probe():
    return subprocess.run([sys.executable, "-c", _PROBE, str(_lock_path())],
                          capture_output=True, text=True, timeout=20).stdout.strip()


def test_another_process_holding_the_lock_blocks_this_start_and_is_named(configured, parked_runner):
    _on()
    _lock_path().parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen([sys.executable, "-c", _HOLDER, str(_lock_path())],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        ok, message = telegram_bridge.start_bridge(reason="startup")
        assert ok is False and str(holder.pid) in message
        assert telegram_bridge.health()["state"] == "lock_held"
        assert parked_runner == []
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_a_killed_holder_releases_the_lock_for_the_next_start(configured, parked_runner):
    _on()
    _lock_path().parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen([sys.executable, "-c", _HOLDER, str(_lock_path())],
                              stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == "held"
    os.kill(holder.pid, signal.SIGKILL)
    holder.wait(timeout=10)
    assert telegram_bridge.start_bridge(reason="startup")[0] is True


def test_a_running_listener_holds_the_lock_and_a_clean_stop_releases_it(configured, parked_runner):
    _on()
    assert telegram_bridge.start_bridge(reason="startup")[0] is True
    assert _probe() == "busy"
    assert telegram_bridge.stop_bridge()[0] is True
    assert _probe() == "free"


def test_the_lock_file_names_the_holder_pid_and_lives_in_a_private_dir(configured, parked_runner):
    _on()
    assert telegram_bridge.start_bridge(reason="startup")[0] is True
    assert _lock_path().read_text().strip() == str(os.getpid())
    assert (_lock_path().parent.stat().st_mode & 0o777) == 0o700
    assert "Q" * 35 not in str(_lock_path())


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_runner_that_raises_still_releases_the_lock(configured, monkeypatch):
    _on()

    def _boom(stop_event, token):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(telegram_bridge, "_run_bridge", _boom)
    assert telegram_bridge.start_bridge(reason="startup")[0] is True
    telegram_bridge._thread.join(timeout=5)
    assert _probe() == "free"


def test_the_standalone_bridge_honours_the_lock_and_releases_it(configured, monkeypatch):
    _on()
    runs = []
    monkeypatch.setattr(telegram_bridge, "_run_bridge",
                        lambda stop_event, token: runs.append(_probe()) or True)
    _lock_path().parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen([sys.executable, "-c", _HOLDER, str(_lock_path())],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        telegram_bridge.main()                     # refused: another poller
        assert runs == []
    finally:
        holder.kill()
        holder.wait(timeout=10)
    telegram_bridge.main()
    assert runs == ["busy"]                        # held while it ran
    assert _probe() == "free"                      # released afterwards


def test_the_standalone_bridge_refuses_a_malformed_token(monkeypatch):
    _on()
    monkeypatch.setattr(telegram_bridge, "get_secret", lambda key: "fake-token")
    monkeypatch.setattr(telegram_bridge, "_owner_chat_id", lambda: OWNER)
    monkeypatch.setattr(telegram_bridge, "_run_bridge", lambda *a: pytest.fail("must not run"))
    telegram_bridge.main()


# ══ The polling supervisor (fake PTB at the network seam) ═══════════════

class _FakeUpdater:
    def __init__(self, app):
        self.app = app
        self.kwargs = None
        self.stop_hangs = False

    async def start_polling(self, **kwargs):
        self.kwargs = kwargs
        self.app.calls.append("start_polling")
        if self.app.polling_raises_later is not None:
            exc = self.app.polling_raises_later

            async def _poll():
                await asyncio.sleep(0.2)
                raise exc

            asyncio.get_running_loop().create_task(_poll(), name="Updater:start_polling:polling_task")

    async def stop(self):
        if self.stop_hangs:
            await asyncio.sleep(30)


class _FakeApp:
    def __init__(self, script):
        self.script = script
        self.calls = []
        self.polling_raises_later = None
        self.updater = _FakeUpdater(self)
        self.shutdowns = 0

    def add_handler(self, *a, **k):
        pass

    async def initialize(self):
        self.calls.append("initialize")
        step = self.script.pop(0) if self.script else None
        if isinstance(step, BaseException):
            raise step

    async def start(self):
        pass

    async def stop(self):
        pass

    async def shutdown(self):
        self.shutdowns += 1


@pytest.fixture
def fake_ptb(monkeypatch):
    """Each Application build returns a fresh _FakeApp sharing one script."""
    state = types.SimpleNamespace(script=[], apps=[], polling_raises_later=None, stop_hangs=False)

    class _Builder:
        def token(self, value):
            return self

        def build(self):
            app = _FakeApp(state.script)
            app.polling_raises_later = state.polling_raises_later
            app.updater.stop_hangs = state.stop_hangs
            state.apps.append(app)
            return app

    monkeypatch.setattr(telegram_bridge, "Application", types.SimpleNamespace(builder=_Builder))
    monkeypatch.setattr(telegram_bridge, "BOOTSTRAP_BACKOFF_S", (0.01, 0.01, 0.01, 0.01))
    monkeypatch.setattr(telegram_bridge, "BACKOFF_CAP_S", 0.01)
    return state


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_polling_starts_with_the_message_only_filter_and_a_sync_error_callback(configured, fake_ptb):
    _on()
    assert telegram_bridge.start_bridge(reason="startup")[0] is True
    assert _wait_for(lambda: telegram_bridge.health()["state"] == "listening")
    kwargs = fake_ptb.apps[-1].updater.kwargs
    assert kwargs["allowed_updates"] == ["message"]
    assert kwargs["timeout"] == 10
    assert kwargs["drop_pending_updates"] is False
    assert callable(kwargs["error_callback"])
    assert not asyncio.iscoroutinefunction(kwargs["error_callback"])


def test_an_offline_launch_retries_until_telegram_is_reachable(configured, fake_ptb):
    from telegram.error import NetworkError, TimedOut
    _on()
    fake_ptb.script.extend([NetworkError("no route"), TimedOut("slow"), None])
    assert telegram_bridge.start_bridge(reason="startup")[0] is True
    assert _wait_for(lambda: telegram_bridge.health()["state"] == "listening")
    assert len(fake_ptb.apps) == 3 and telegram_bridge.is_running()


@pytest.mark.parametrize("how", ["stop", "off", "estop"])
def test_a_stop_during_bootstrap_backoff_is_prompt(configured, fake_ptb, monkeypatch, how):
    from telegram.error import NetworkError
    _on()
    monkeypatch.setattr(telegram_bridge, "BOOTSTRAP_BACKOFF_S", (30, 30, 30, 30))
    fake_ptb.script.extend([NetworkError("no route")] * 5)
    assert telegram_bridge.start_bridge(reason="startup")[0] is True
    assert _wait_for(lambda: telegram_bridge.health()["state"] == "retrying")
    started = time.monotonic()
    if how == "stop":
        telegram_bridge.request_stop_bridge()
    elif how == "off":
        access.turn_off("owner")
    else:
        emergency_stop.latch(source="test", reason="test")
    assert _wait_for(lambda: not telegram_bridge.is_running(), timeout=2.0)
    assert time.monotonic() - started < 1.5
    assert _probe() == "free"


def test_a_rejected_token_at_startup_stops_without_retrying(configured, fake_ptb):
    from telegram.error import InvalidToken
    _on()
    fake_ptb.script.append(InvalidToken())
    assert telegram_bridge.start_bridge(reason="startup")[0] is True
    assert _wait_for(lambda: not telegram_bridge.is_running())
    assert telegram_bridge.health()["state"] == "token_rejected"
    assert len(fake_ptb.apps) == 1
    assert _probe() == "free"


def test_a_token_revoked_while_running_is_reported_truthfully(configured, fake_ptb):
    from telegram.error import InvalidToken
    _on()
    fake_ptb.polling_raises_later = InvalidToken()
    assert telegram_bridge.start_bridge(reason="startup")[0] is True
    assert _wait_for(lambda: not telegram_bridge.is_running(), timeout=3.0)
    assert telegram_bridge.health()["state"] == "token_rejected"
    assert access.remote_access_enabled() is True          # a fault never flips the switch
    assert _probe() == "free"


def test_three_conflicts_in_the_window_make_the_listener_yield(configured, fake_ptb):
    from telegram.error import Conflict
    _on()
    assert telegram_bridge.start_bridge(reason="startup")[0] is True
    assert _wait_for(lambda: telegram_bridge.health()["state"] == "listening")
    callback = fake_ptb.apps[-1].updater.kwargs["error_callback"]
    for _ in range(3):
        callback(Conflict("terminated by other getUpdates request"))
    assert _wait_for(lambda: not telegram_bridge.is_running(), timeout=3.0)
    assert telegram_bridge.health()["state"] == "conflict"
    assert access.remote_access_enabled() is True          # D-B: the switch stays ON
    assert _probe() == "free"


def test_two_conflicts_do_not_make_the_listener_yield(configured, fake_ptb):
    from telegram.error import Conflict
    _on()
    assert telegram_bridge.start_bridge(reason="startup")[0] is True
    assert _wait_for(lambda: telegram_bridge.health()["state"] == "listening")
    callback = fake_ptb.apps[-1].updater.kwargs["error_callback"]
    callback(Conflict("x"))
    callback(Conflict("x"))
    time.sleep(1.2)
    assert telegram_bridge.is_running() and telegram_bridge.health()["state"] == "listening"


def test_conflicts_outside_the_window_do_not_add_up(configured, fake_ptb, monkeypatch):
    from telegram.error import Conflict
    _on()
    monkeypatch.setattr(telegram_bridge, "CONFLICT_WINDOW_S", 0.3)
    assert telegram_bridge.start_bridge(reason="startup")[0] is True
    assert _wait_for(lambda: telegram_bridge.health()["state"] == "listening")
    callback = fake_ptb.apps[-1].updater.kwargs["error_callback"]
    for _ in range(3):
        callback(Conflict("x"))
        time.sleep(0.2)
    time.sleep(1.0)
    assert telegram_bridge.is_running()


# ══ C-4: bounded shutdown and no headless turn while closing ════════════

def test_shutdown_for_exit_is_bounded_even_when_ptb_hangs(configured, fake_ptb):
    _on()
    fake_ptb.stop_hangs = True
    assert telegram_bridge.start_bridge(reason="startup")[0] is True
    assert _wait_for(lambda: telegram_bridge.health()["state"] == "listening")
    started = time.monotonic()
    stopped = telegram_bridge.shutdown_for_exit(timeout=0.5)
    assert stopped is False and time.monotonic() - started < 1.5


def test_shutdown_for_exit_with_no_listener_returns_at_once():
    started = time.monotonic()
    assert telegram_bridge.shutdown_for_exit(timeout=3.0) is True
    assert time.monotonic() - started < 0.5
    assert telegram_bridge.is_closing() is False


def test_closing_only_applies_while_a_listener_thread_is_alive(monkeypatch):
    monkeypatch.setattr(telegram_bridge, "is_running", lambda: True)   # a stale test double
    telegram_bridge.begin_shutdown()
    assert telegram_bridge.is_closing() is False


@pytest.fixture
def live_listener(configured, parked_runner):
    _on()
    assert telegram_bridge.start_bridge(reason="startup")[0] is True
    return parked_runner


def test_a_message_arriving_while_closing_is_refused_and_never_runs_headless(live_listener, monkeypatch):
    from comms import telegram_ingress_policy as policy
    monkeypatch.setattr(telegram_bridge, "run_headless_turn", lambda **k: pytest.fail("never headless"))
    telegram_bridge.begin_shutdown()
    assert telegram_bridge.is_closing() is True
    update, replies = _update("are you there?")
    _deliver(update)
    assert replies == [policy.CLOSING_REFUSAL]


def test_a_routed_request_failed_by_window_close_gets_the_closing_reply(live_listener, monkeypatch):
    from comms import telegram_ingress_policy as policy
    monkeypatch.setattr(telegram_bridge, "run_headless_turn", lambda **k: pytest.fail("never headless"))

    def _prime(request):
        telegram_bridge.begin_shutdown()
        request.future.set_exception(routing.OriginUnavailable("origin window closed"))
        return True

    routing.register_runtime(_prime, token="prime")
    with routing.origin_scope("prime", 17):
        routing.record_outbound(destination_chat_id=OWNER, telegram_message_id=4001)
    for reply_to in (4001, None):            # a swipe-reply and a plain sole-recent follow-up
        update, replies = _update("go", message_id=1000 + (reply_to or 0), reply_to=reply_to)
        _deliver(update)
        assert replies == [policy.CLOSING_REFUSAL]


def test_closing_at_final_headless_admission_never_starts_the_turn(live_listener, monkeypatch):
    monkeypatch.setattr(telegram_bridge, "run_headless_turn", lambda **k: pytest.fail("never headless"))
    real = telegram_bridge.looks_like_operator_command

    def _close_after_intake(text):
        telegram_bridge.begin_shutdown()
        return real(text)

    monkeypatch.setattr(telegram_bridge, "looks_like_operator_command", _close_after_intake)
    update, replies = _update("hello")
    _deliver(update)
    from comms import telegram_ingress_policy as policy
    assert replies == [policy.CLOSING_REFUSAL]


def test_a_new_start_clears_closing(configured, parked_runner):
    _on()
    assert telegram_bridge.start_bridge(reason="startup")[0] is True
    telegram_bridge.begin_shutdown()
    telegram_bridge._thread.join(timeout=5)
    assert telegram_bridge.start_bridge(reason="switch_on")[0] is True
    assert telegram_bridge.is_closing() is False


# ══ /resume: same gates, no WAKE duplicate claim (C-3) ══════════════════

def _resume_update(*, date, message_id=7):
    from telegram import Chat, Message, MessageEntity, Update, User
    msg = Message(message_id=message_id, date=date, chat=Chat(id=42, type=Chat.PRIVATE),
                  from_user=User(id=42, first_name="o", is_bot=False), text="/resume ABCD2345",
                  entities=[MessageEntity(MessageEntity.BOT_COMMAND, 0, 7)])
    return Update(update_id=message_id, message=msg)


@pytest.fixture
def resume_replies(monkeypatch):
    from telegram import Message
    sent = []

    async def _reply(self, text, *a, **k):
        sent.append(text)

    monkeypatch.setattr(Message, "reply_text", _reply)
    monkeypatch.setattr(recovery, "_owner_chat_id", lambda: 42)
    return sent


def _resume(update):
    context = types.SimpleNamespace(bot=types.SimpleNamespace(username="LuminaBot"))
    asyncio.run(recovery.handle_recovery_command(update, context))


def test_a_resume_sent_while_access_was_off_is_refused(resume_replies):
    _on()
    before = datetime.datetime.fromtimestamp(int(access.on_since()) - 60, datetime.timezone.utc)
    _resume(_resume_update(date=before))
    assert resume_replies == [access.STALE_BACKLOG_REPLY]


def test_a_stale_resume_is_refused(resume_replies):
    _on()
    old = datetime.datetime.fromtimestamp(int(time.time()) - 600, datetime.timezone.utc)
    access_on_since_patch = old - datetime.timedelta(seconds=60)
    prefs = json.load(open(persistence.PREFS_PATH))
    prefs[access.ON_SINCE_KEY] = access_on_since_patch.timestamp()
    json.dump(prefs, open(persistence.PREFS_PATH, "w"))
    _resume(_resume_update(date=old))
    assert len(resume_replies) == 1 and "not run" in resume_replies[0]
    assert resume_replies[0] != recovery.RECOVERY_NOT_ACTIVE_REPLY


def test_a_fresh_resume_is_not_claimed_by_wake(resume_replies):
    _on()
    now = datetime.datetime.now(datetime.timezone.utc)
    _resume(_resume_update(date=now))
    _resume(_resume_update(date=now))                 # same message id twice
    assert resume_replies == [recovery.RECOVERY_NOT_ACTIVE_REPLY] * 2


# ══ Launch start and entry points ════════════════════════════════════════

def _install_fake_gui(monkeypatch, order, exec_result=0):
    class FakeApplication:
        def __init__(self, argv):
            pass

        def setApplicationName(self, name):
            pass

        def setApplicationVersion(self, version):
            pass

        def setFont(self, font):
            pass

        def exec(self):
            order.append("exec")
            return exec_result

    class FakeFont:
        def __init__(self, name, size):
            pass

        def exactMatch(self):
            return True

    class FakeWindow:
        def __init__(self, agent, stt=None):
            order.append("window")
            self.chat_widget = types.SimpleNamespace(add_tool_indicator=lambda *args: None)

        def show(self):
            order.append("show")

    class FakeAgent:
        def __init__(self, *a, **k):
            self.tts = None

    widgets = types.ModuleType("PySide6.QtWidgets")
    widgets.QApplication = FakeApplication
    gui = types.ModuleType("PySide6.QtGui")
    gui.QFont = FakeFont
    agent_module = types.ModuleType("core.agent")
    agent_module.LuminaAgent = FakeAgent
    window_module = types.ModuleType("ui.main_window")
    window_module.LuminaWindow = FakeWindow
    tts_module = types.ModuleType("tts.loader")
    tts_module.get_tts_backend = lambda: None
    tts_module.unload_tts_backend = lambda backend=None: None
    stt_module = types.ModuleType("stt.whisper_bridge")
    stt_module.WhisperBridge = lambda **kwargs: object()
    for name, module in [("PySide6.QtWidgets", widgets), ("PySide6.QtGui", gui), ("core.agent", agent_module),
                         ("ui.main_window", window_module), ("tts.loader", tts_module),
                         ("stt.whisper_bridge", stt_module)]:
        monkeypatch.setitem(sys.modules, name, module)
    import main
    import core.skill_transport as skill_transport
    import core.knowledge_bootstrap as knowledge_bootstrap
    from core import process_manager
    monkeypatch.setattr(skill_transport, "ensure_official_skills_bootstrapped", lambda **k: None)
    monkeypatch.setattr(knowledge_bootstrap, "ensure_official_knowledge_bootstrapped", lambda **k: None)
    monkeypatch.setattr(process_manager, "shutdown_all", lambda: order.append("process_shutdown"))
    return main


def test_run_gui_starts_the_listener_once_after_the_window_exists_and_stops_it_on_exit(monkeypatch):
    _on()
    order = []
    main = _install_fake_gui(monkeypatch, order)
    monkeypatch.setattr(telegram_bridge, "start_bridge",
                        lambda reason="manual": order.append(f"start:{reason}") or (False, "off"))
    monkeypatch.setattr(telegram_bridge, "shutdown_for_exit",
                        lambda timeout=3.0: order.append(f"shutdown_for_exit:{timeout}") or True)
    with pytest.raises(SystemExit):
        main.run_gui()
    assert order == ["window", "show", "start:startup", "exec", "shutdown_for_exit:3.0", "process_shutdown"]


def test_run_gui_with_remote_access_off_starts_and_stops_nothing(monkeypatch):
    order = []
    main = _install_fake_gui(monkeypatch, order)
    monkeypatch.setattr(telegram_bridge, "start_bridge",
                        lambda reason="manual": order.append(f"start:{reason}") or (False, "off"))
    monkeypatch.setattr(telegram_bridge, "shutdown_for_exit",
                        lambda timeout=3.0: order.append("shutdown_for_exit") or True)
    with pytest.raises(SystemExit):
        main.run_gui()
    assert order == ["window", "show", "exec", "process_shutdown"]


def test_run_gui_launch_survives_a_listener_start_failure(monkeypatch):
    _on()
    order = []
    main = _install_fake_gui(monkeypatch, order)

    def _explode(reason="manual"):
        raise RuntimeError("bridge exploded")

    monkeypatch.setattr(telegram_bridge, "start_bridge", _explode)
    monkeypatch.setattr(telegram_bridge, "shutdown_for_exit", lambda timeout=3.0: True)
    with pytest.raises(SystemExit):
        main.run_gui()
    assert "exec" in order


def test_only_run_gui_starts_a_listener():
    """CLI mode, headless agents and merely constructing the window (as
    hundreds of Qt tests do) never start a poller; only the GUI launch does."""
    import main
    main_src = (REPO / "main.py").read_text()
    assert "start_bridge" in inspect.getsource(main.run_gui)
    assert "start_bridge" not in inspect.getsource(main.run_cli)
    assert main_src.count("start_bridge") == inspect.getsource(main.run_gui).count("start_bridge")
    assert "start_bridge" not in (REPO / "core" / "headless.py").read_text()
    window_src = (REPO / "ui" / "main_window.py").read_text()
    window_class = window_src.split("class LuminaWindow", 1)[1]
    init_src = window_class.split("def __init__", 1)[1].split("\n    def ", 1)[0]
    assert "start_bridge" not in init_src and "start_at_launch" not in init_src
