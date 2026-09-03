"""TELEGRAM-OUTBOUND-AUTOSTART-01 focused regressions.

The outbound tools use deterministic bridge and HTTP fakes here.  No test in
this file contacts Telegram or touches the configured owner identity/token.
"""
import concurrent.futures
import threading
import time

import pytest

import comms.telegram_bridge as telegram_bridge
import tools.telegram_send as telegram_send


class _Response:
    def raise_for_status(self):
        return None


@pytest.fixture
def outbound_fakes(monkeypatch):
    posts = []
    records = []

    monkeypatch.setattr(telegram_send, "check", lambda request_id: None)
    monkeypatch.setattr(
        telegram_send,
        "record",
        lambda request_id, result: records.append((request_id, result)),
    )
    monkeypatch.setattr(telegram_send, "get_secret", lambda key: "fake-token")
    monkeypatch.setattr(telegram_send, "_owner_chat_id", lambda: "12345")

    def post(url, **kwargs):
        posts.append((url, kwargs))
        return _Response()

    monkeypatch.setattr(telegram_send.requests, "post", post)
    return posts, records


def _fake_bridge(monkeypatch, *, initially_running=False, start_result=(True, "Bridge started.")):
    state = {"running": initially_running, "starts": 0, "checks": 0}

    def is_running():
        state["checks"] += 1
        return state["running"]

    def start_bridge():
        state["starts"] += 1
        if start_result[0]:
            state["running"] = True
        return start_result

    monkeypatch.setattr(telegram_bridge, "is_running", is_running)
    monkeypatch.setattr(telegram_bridge, "start_bridge", start_bridge)
    return state


def test_stopped_bridge_starts_once_then_message_sends(monkeypatch, outbound_fakes):
    posts, records = outbound_fakes
    bridge = _fake_bridge(monkeypatch)

    result = telegram_send.send_telegram_message("wake up")

    assert result == "[Message sent.]"
    assert bridge["starts"] == 1
    assert len(posts) == 1
    assert len(records) == 1


def test_running_bridge_sends_without_second_listener(monkeypatch, outbound_fakes):
    posts, _records = outbound_fakes
    bridge = _fake_bridge(monkeypatch, initially_running=True)

    result = telegram_send.send_telegram_message("already awake")

    assert result == "[Message sent.]"
    assert bridge["starts"] == 0
    assert len(posts) == 1


def test_repeated_sends_reuse_one_bridge(monkeypatch, outbound_fakes):
    posts, _records = outbound_fakes
    bridge = _fake_bridge(monkeypatch)

    first = telegram_send.send_telegram_message("first")
    second = telegram_send.send_telegram_message("second")

    assert first == second == "[Message sent.]"
    assert bridge["starts"] == 1
    assert len(posts) == 2


def test_concurrent_sends_cannot_create_duplicate_bridge(monkeypatch, outbound_fakes):
    posts, _records = outbound_fakes
    state = {"running": False, "starts": 0}
    state_lock = threading.Lock()

    def is_running():
        with state_lock:
            return state["running"]

    def start_bridge():
        with state_lock:
            state["starts"] += 1
        # Widen the pre-fix check/start race deterministically.  The relay's
        # lock and second is_running() check must still permit only one start.
        time.sleep(0.05)
        with state_lock:
            state["running"] = True
        return True, "Bridge started."

    monkeypatch.setattr(telegram_bridge, "is_running", is_running)
    monkeypatch.setattr(telegram_bridge, "start_bridge", start_bridge)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(telegram_send.send_telegram_message, [f"message-{i}" for i in range(8)]))

    assert results == ["[Message sent.]"] * 8
    assert state["starts"] == 1
    assert len(posts) == 8


def test_start_failure_is_reported_without_blocking_existing_send(monkeypatch, outbound_fakes):
    posts, records = outbound_fakes
    bridge = _fake_bridge(
        monkeypatch,
        start_result=(False, "Emergency stop is active — the bridge cannot start until it's re-armed."),
    )

    result = telegram_send.send_telegram_message("outbound still allowed")

    assert result.startswith("[Message sent.]")
    assert "reply bridge" in result.lower()
    assert "did not start" in result.lower()
    assert bridge["starts"] == 1
    assert len(posts) == 1
    # Only the stable send result belongs in the idempotency ledger; a later
    # distinct send may start the bridge and must not replay a stale warning.
    assert records[0][1] == "[Message sent.]"


def test_send_failure_keeps_existing_telegram_error(monkeypatch, outbound_fakes):
    _posts, records = outbound_fakes
    bridge = _fake_bridge(monkeypatch)

    def fail_post(url, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(telegram_send.requests, "post", fail_post)

    result = telegram_send.send_telegram_message("will fail")

    assert result == "[Telegram send error: network down]"
    assert bridge["starts"] == 1
    assert records == []


def test_file_tool_uses_same_bridge_tripwire(monkeypatch, outbound_fakes, tmp_path):
    posts, _records = outbound_fakes
    bridge = _fake_bridge(monkeypatch)
    payload = tmp_path / "report.txt"
    payload.write_text("safe fixture", encoding="utf-8")

    result = telegram_send.send_telegram_file(str(payload), "report")

    assert result == f"[Sent '{payload}' to Telegram.]"
    assert bridge["starts"] == 1
    assert len(posts) == 1


def test_embedded_bridge_shares_the_active_runtime_configuration():
    # The starter imports the established embedded bridge in this process;
    # there is no subprocess/cwd/env reconstruction that could silently bind
    # it to the other checkout's data root.
    assert telegram_send.config is telegram_bridge.config
    assert telegram_send.load_prefs is telegram_bridge.load_prefs
