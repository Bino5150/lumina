"""TELEGRAM-MASTER-SWITCH-01 -- one persistent Telegram master switch.

Owner design (Bino, 2026-10-19): the Settings Telegram toggle becomes a
persistent master switch. OFF wins against startup, outbound sends,
reconnection, standalone processes and automatic listening; the emergency
stop forces it OFF and re-arm never turns it back ON; only the owner's
explicit local action restores it. OFF also blocks outbound (a lost phone
must not receive reports). The outbound auto-start of listening (the S74
"previous tenant's mail" path) is removed; TELEGRAM-OWNER-INBOUND-WAKE-01
will start the listener at launch while ON. Until then, ON does not start
anything: an accepted, temporary Telegram blackout.

Covered here (no Qt): the switch module, every send/start/listen gate,
the other-process (Kali / standalone) OFF watch, late-backlog refusal, the
route-ledger reset, and the no-model-path rule. The Settings switch, the
emergency-stop write and re-arm are in test_telegram_master_switch_01_qt.py.
"""
import asyncio
import datetime
import json
import os
import pathlib
import threading
import time
import types

import pytest

import comms.telegram_access as access
import comms.telegram_bridge as telegram_bridge
import comms.telegram_recovery as recovery
import tools.telegram_send as telegram_send
from comms import telegram_origin_routing as routing
from core import emergency_stop, persistence

OWNER = "42"


@pytest.fixture(autouse=True)
def hermetic(tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    access._reset_for_tests()
    emergency_stop._reset_for_tests()
    routing._reset_for_tests()
    yield tmp_path
    access._reset_for_tests()
    emergency_stop._reset_for_tests()
    routing._reset_for_tests()


def _prefs():
    try:
        return json.load(open(persistence.PREFS_PATH))
    except FileNotFoundError:
        return {}


def _other_process_writes(**values):
    """A different process (Kali, the standalone bridge, a second Lumina)
    flipping the shared prefs file -- bypasses this process's module state."""
    prefs = _prefs()
    prefs.update(values)
    with open(persistence.PREFS_PATH, "w") as f:
        json.dump(prefs, f)


# ── The switch itself ────────────────────────────────────────────────────

def test_default_is_off():
    assert access.remote_access_enabled() is False


def test_owner_turn_on_persists_with_its_time_and_clears_the_off_reason():
    access.turn_off("emergency_stop")
    before = time.time()
    assert access.turn_on_by_owner() is True
    prefs = _prefs()
    assert prefs[access.PREF_KEY] is True
    assert prefs[access.REASON_KEY] is None
    assert prefs[access.ON_SINCE_KEY] >= before - 1
    assert access.remote_access_enabled() is True


@pytest.mark.parametrize("reason", ["owner", "emergency_stop"])
def test_turn_off_persists_its_reason(reason):
    access.turn_on_by_owner()
    assert access.turn_off(reason) is True
    assert _prefs()[access.PREF_KEY] is False
    assert access.off_reason() == reason
    assert access.remote_access_enabled() is False


def test_a_failed_off_write_still_holds_off_for_this_process(monkeypatch):
    access.turn_on_by_owner()
    monkeypatch.setattr(persistence, "update", lambda mapping: False)
    assert access.turn_off("emergency_stop") is False
    assert _prefs()[access.PREF_KEY] is True                 # disk still says ON
    assert access.remote_access_enabled() is False           # ...but this process is OFF


def test_a_failed_on_write_leaves_it_off(monkeypatch):
    monkeypatch.setattr(persistence, "update", lambda mapping: False)
    assert access.turn_on_by_owner() is False
    assert access.remote_access_enabled() is False


def test_restart_keeps_the_persisted_state():
    access.turn_on_by_owner()
    access.turn_off("emergency_stop")
    access._reset_for_tests()                                # fresh process
    assert access.remote_access_enabled() is False
    assert access.off_reason() == "emergency_stop"


def test_another_process_turning_it_off_is_seen_immediately():
    access.turn_on_by_owner()
    _other_process_writes(**{access.PREF_KEY: False})
    assert access.remote_access_enabled() is False


@pytest.mark.parametrize("value", ["true", 1, "on", [True], {"on": True}, None])
def test_only_a_real_true_counts_as_on(value):
    _other_process_writes(**{access.PREF_KEY: value})
    assert access.remote_access_enabled() is False


def test_off_clears_the_return_route_ledger():
    access.turn_on_by_owner()
    token = routing.register_runtime(lambda request: True)
    with routing.origin_scope(token, 17):
        routing.record_outbound(destination_chat_id=OWNER, telegram_message_id=7001)
    assert routing.resolve(destination_chat_id=OWNER, reply_to_message_id=7001).reason == "exact_reply"

    access.turn_off("owner")

    assert routing.resolve(destination_chat_id=OWNER, reply_to_message_id=7001).reason == "cold"


# ── Outbound: never starts listening; OFF sends nothing ──────────────────

class _Resp:
    def __init__(self, message_id=8001):
        self.message_id = message_id

    def raise_for_status(self):
        return None

    def json(self):
        return {"ok": True, "result": {"message_id": self.message_id}}


@pytest.fixture
def outbound(monkeypatch):
    posts = []

    def post(url, data=None, files=None, timeout=None):
        posts.append({"method": url.rsplit("/", 1)[-1], "data": dict(data or {}), "files": files})
        return _Resp()

    monkeypatch.setattr(telegram_send.requests, "post", post)
    monkeypatch.setattr(telegram_send, "get_secret", lambda key: "fake-token")
    monkeypatch.setattr(telegram_send, "_owner_chat_id", lambda: OWNER)
    monkeypatch.setattr(telegram_send, "check", lambda request_id: None)
    monkeypatch.setattr(telegram_send, "record", lambda request_id, result: None)
    monkeypatch.setattr(telegram_bridge, "start_bridge",
                        lambda *a, **k: pytest.fail("an outbound send must never start listening"))
    return posts


def _sends(tmp_path):
    report = tmp_path / "report.md"
    report.write_text("findings")
    return [lambda: telegram_send.send_telegram_message("printer alert"),
            lambda: telegram_send.send_telegram_file(str(report), caption="report")]


@pytest.mark.parametrize("switch_on", [True, False])
def test_an_outbound_send_never_starts_or_enables_listening(outbound, hermetic, monkeypatch, switch_on):
    if switch_on:
        access.turn_on_by_owner()
    monkeypatch.setattr(telegram_bridge, "is_running", lambda: False)
    for send in _sends(hermetic):
        send()
    assert access.remote_access_enabled() is switch_on


def test_off_blocks_every_send_with_no_request_and_no_route(outbound, hermetic):
    token = routing.register_runtime(lambda request: True)
    with routing.origin_scope(token, 17):
        results = [send() for send in _sends(hermetic)]
    assert outbound == []
    assert results == [telegram_send.ACCESS_OFF_RESULT] * 2
    assert routing.resolve(destination_chat_id=OWNER).reason == "cold"


def test_on_with_a_running_listener_delivers_files_and_messages_and_records_routes_as_before(
        outbound, hermetic, monkeypatch):
    access.turn_on_by_owner()
    monkeypatch.setattr(telegram_bridge, "is_running", lambda: True)
    token = routing.register_runtime(lambda request: True)
    with routing.origin_scope(token, 17):
        message, document = (send() for send in _sends(hermetic))
    assert message == "[Message sent.]"
    assert document.startswith("[Sent '") and document.endswith("' to Telegram.]")
    assert [(p["method"], p["data"]["chat_id"]) for p in outbound] == [("sendMessage", OWNER),
                                                                          ("sendDocument", OWNER)]
    assert routing.resolve(destination_chat_id=OWNER, reply_to_message_id=8001).reason == "exact_reply"


def test_on_without_a_listener_still_delivers_and_says_replies_cannot_come_back(outbound, hermetic,
                                                                                 monkeypatch):
    access.turn_on_by_owner()
    monkeypatch.setattr(telegram_bridge, "is_running", lambda: False)
    token = routing.register_runtime(lambda request: True)
    with routing.origin_scope(token, 17):
        result = telegram_send.send_telegram_message("printer alert")
    assert len(outbound) == 1
    assert result.startswith("[Message sent.]") and "no Telegram listener is running" in result
    assert routing.resolve(destination_chat_id=OWNER).reason == "cold"   # no route to nowhere


def test_the_outbound_auto_start_helper_is_gone():
    assert not hasattr(telegram_send, "_ensure_reply_bridge")


# ── Every start path refuses while OFF ───────────────────────────────────

@pytest.fixture
def startable(monkeypatch):
    monkeypatch.setattr(telegram_bridge, "get_secret", lambda key: "fake-token")
    monkeypatch.setattr(telegram_bridge, "_owner_chat_id", lambda: OWNER)
    started = []
    monkeypatch.setattr(telegram_bridge, "_run_bridge", lambda event, token: started.append(token) or True)
    monkeypatch.setattr(telegram_bridge, "_thread", None)
    return started


def test_start_bridge_refuses_while_off_persisted(startable):
    ok, message = telegram_bridge.start_bridge()
    assert (ok, message) == (False, access.ACCESS_OFF_MESSAGE)
    assert telegram_bridge._thread is None and startable == []


def test_start_bridge_refuses_while_forced_off_even_if_disk_says_on(startable, monkeypatch):
    access.turn_on_by_owner()
    monkeypatch.setattr(persistence, "update", lambda mapping: False)
    access.turn_off("emergency_stop")                        # write fails -> forced off
    assert telegram_bridge.start_bridge() == (False, access.ACCESS_OFF_MESSAGE)


def test_start_bridge_still_starts_when_on(startable):
    access.turn_on_by_owner()
    ok, _ = telegram_bridge.start_bridge()
    telegram_bridge._thread.join(timeout=2)
    assert ok is True and startable == ["fake-token"]


def test_the_standalone_process_refuses_while_off(startable):
    telegram_bridge.main()
    assert startable == []


# ── A running listener stops when the switch goes OFF ────────────────────

class _FakeApp:
    def __init__(self):
        self.updater = types.SimpleNamespace(start_polling=self._noop, stop=self._noop)
        self.stopped = False

    async def _noop(self, *a, **k):
        return None

    def add_handler(self, *a, **k):
        pass

    async def initialize(self):
        pass

    async def start(self):
        pass

    async def stop(self):
        self.stopped = True

    async def shutdown(self):
        pass


@pytest.fixture
def fake_application(monkeypatch):
    app = _FakeApp()

    class _Builder:
        def token(self, value):
            return self

        def build(self):
            return app

    monkeypatch.setattr(telegram_bridge, "Application", types.SimpleNamespace(builder=_Builder))
    return app


def _run_listener():
    stop = threading.Event()
    thread = threading.Thread(target=lambda: asyncio.run(telegram_bridge._run_until_stopped(stop, "fake")))
    thread.start()
    return thread, stop


@pytest.mark.parametrize("how", ["settings_in_this_process", "another_process_or_kali"])
def test_a_running_listener_shuts_itself_down_when_the_switch_goes_off(fake_application, how):
    access.turn_on_by_owner()
    thread, stop = _run_listener()
    time.sleep(0.2)
    assert thread.is_alive()

    if how == "settings_in_this_process":
        access.turn_off("owner")
    else:
        _other_process_writes(**{access.PREF_KEY: False})
    thread.join(timeout=3)

    try:
        assert not thread.is_alive() and fake_application.stopped
    finally:
        stop.set()
        thread.join(timeout=3)


# ── Inbound while OFF, and the previous tenant's mail ────────────────────

def _update(text, *, date=None, chat_id=OWNER):
    replies = []

    async def _reply(value):
        replies.append(value)

    message = types.SimpleNamespace(text=text, message_id=9001, reply_to_message=None, reply_text=_reply,
                                    date=date)
    return types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=int(chat_id)), message=message), replies


@pytest.fixture
def no_turns(monkeypatch):
    def _fail(*a, **k):
        pytest.fail("this update must not reach routing or any turn")

    monkeypatch.setattr(telegram_bridge, "origin_routing",
                        types.SimpleNamespace(resolve=_fail, dispatch=_fail), raising=False)
    monkeypatch.setattr(telegram_bridge, "run_headless_turn", _fail)
    monkeypatch.setattr(telegram_bridge, "_owner_chat_id", lambda: OWNER)


def test_an_update_arriving_while_off_is_dropped_silently(no_turns):
    update, replies = _update("stop the print")
    asyncio.run(telegram_bridge.handle_message(update, context=None))
    assert replies == []


def test_a_recovery_command_arriving_while_off_is_dropped_silently(monkeypatch):
    from telegram import Chat, Message, MessageEntity, Update, User
    sent = []

    async def _reply(self, text, *a, **k):
        sent.append(text)

    monkeypatch.setattr(Message, "reply_text", _reply)
    monkeypatch.setattr(recovery, "_owner_chat_id", lambda: 42)
    when = datetime.datetime.now(datetime.timezone.utc)
    msg = Message(message_id=7, date=when, chat=Chat(id=42, type=Chat.PRIVATE),
                  from_user=User(id=42, first_name="o", is_bot=False), text="/resume ABCD2345",
                  entities=[MessageEntity(MessageEntity.BOT_COMMAND, 0, 7)])
    context = types.SimpleNamespace(bot=types.SimpleNamespace(username="LuminaBot"))
    asyncio.run(recovery.handle_recovery_command(Update(update_id=1, message=msg), context))
    assert sent == []


def test_a_message_sent_while_access_was_off_never_runs_after_it_comes_back_on(no_turns):
    access.turn_on_by_owner()
    on_since = access.on_since()
    sent_while_off = datetime.datetime.fromtimestamp(int(on_since) - 120, datetime.timezone.utc)
    update, replies = _update("stop the print and delete the job", date=sent_while_off)

    asyncio.run(telegram_bridge.handle_message(update, context=None))

    assert replies == [access.STALE_BACKLOG_REPLY]


def test_a_message_sent_after_access_came_back_on_proceeds(monkeypatch):
    access.turn_on_by_owner()
    calls = []
    monkeypatch.setattr(telegram_bridge, "origin_routing", types.SimpleNamespace(
        resolve=lambda **kw: types.SimpleNamespace(route=None, reason="cold")), raising=False)
    monkeypatch.setattr(telegram_bridge, "_owner_chat_id", lambda: OWNER)
    monkeypatch.setattr(telegram_bridge, "run_headless_turn",
                        lambda **kw: calls.append(kw) or {"success": True, "response": "ok"})
    same_second = datetime.datetime.fromtimestamp(int(access.on_since()), datetime.timezone.utc)
    update, replies = _update("status?", date=same_second)

    asyncio.run(telegram_bridge.handle_message(update, context=None))

    assert replies == ["ok"] and len(calls) == 1


# ── No model-facing path can turn it on ──────────────────────────────────

def _turn_on_callers(root: pathlib.Path) -> list:
    """Non-test, git-tracked (shipped) source files under `root` that mention
    turn_on_by_owner. Untracked local files -- dev keeps reports and evidence
    scripts inside its checkout -- are not code and never count. Fails
    loudly rather than passing vacuously if git cannot list the files."""
    import subprocess
    listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--", "*.py"],
                            check=True, capture_output=True).stdout.decode()
    tracked = [rel for rel in listed.split("\0") if rel]
    assert tracked, "git listed no tracked .py files"
    callers = []
    for rel in tracked:
        if rel.startswith("tests/"):
            continue
        path = root / rel
        if path.is_file() and "turn_on_by_owner" in path.read_text(errors="ignore"):
            callers.append(rel)
    return sorted(callers)


def test_only_the_desktop_settings_switch_can_turn_remote_access_on():
    root = pathlib.Path(__file__).resolve().parent.parent
    assert _turn_on_callers(root) == ["comms/telegram_access.py", "ui/settings/communications_tab.py"]


def test_the_caller_scan_reads_shipped_code_only_not_untracked_local_files(tmp_path):
    """Dev's ~/lumina keeps untracked reports and evidence scripts inside the
    checkout (excluded via .git/info/exclude). Found on the dev mirror on
    2026-10-19: an evidence script mentioning turn_on_by_owner was counted
    as a caller. Only git-tracked files are shipped code."""
    import subprocess
    run = lambda *args: subprocess.run(["git", "-C", str(tmp_path), *args], check=True,
                                       capture_output=True)
    run("init", "-q")
    (tmp_path / "ui").mkdir()
    (tmp_path / "ui" / "switch.py").write_text("telegram_access.turn_on_by_owner()\n")
    (tmp_path / "reports" / "EVIDENCE").mkdir(parents=True)
    (tmp_path / "reports" / "EVIDENCE" / "mutation_sweep.py").write_text("x = 'turn_on_by_owner'\n")
    (tmp_path / ".git" / "info" / "exclude").write_text("/reports/\n")
    (tmp_path / "scratch.py").write_text("turn_on_by_owner  # untracked, not ignored\n")
    run("add", "ui/switch.py")

    assert _turn_on_callers(tmp_path) == ["ui/switch.py"]
