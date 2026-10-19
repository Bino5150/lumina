"""TELEGRAM-OUTBOUND-AUTOSTART-01, retired by TELEGRAM-MASTER-SWITCH-01.

TELEGRAM-OUTBOUND-AUTOSTART-01 made an outbound send start the inbound
bridge, so Bino could answer an alert Lumina sent while the bridge toggle
was off. That same path silently reopened Telegram after an emergency-stop
re-arm, despite the re-arm promise. The owner's design (2026-10-19) moved
automatic listening to TELEGRAM-OWNER-INBOUND-WAKE-01 (start at launch while
the persistent remote-access switch is ON) and made outbound sends delivery
only. These tests keep every original scenario and now pin that contract:
an outbound send never starts, restarts or enables a listener.

Deterministic bridge and HTTP fakes; no test contacts Telegram.
"""
import concurrent.futures
import threading

import pytest

import comms.telegram_bridge as telegram_bridge
import tools.telegram_send as telegram_send

pytestmark = pytest.mark.usefixtures("telegram_remote_access_on")


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


def _fake_bridge(monkeypatch, *, running=False):
    state = {"running": running, "starts": 0}
    lock = threading.Lock()

    def is_running():
        with lock:
            return state["running"]

    def start_bridge(*a, **k):
        with lock:
            state["starts"] += 1
        return True, "Bridge started."

    monkeypatch.setattr(telegram_bridge, "is_running", is_running)
    monkeypatch.setattr(telegram_bridge, "start_bridge", start_bridge)
    return state


def test_stopped_bridge_is_not_started_and_the_message_still_sends(monkeypatch, outbound_fakes):
    posts, records = outbound_fakes
    bridge = _fake_bridge(monkeypatch)

    result = telegram_send.send_telegram_message("wake up")

    assert result.startswith("[Message sent.]")
    assert "replies can't come back yet" in result
    assert bridge["starts"] == 0
    assert len(posts) == 1
    assert records[0][1] == "[Message sent.]"          # the ledger keeps the stable result


def test_running_bridge_sends_without_any_start(monkeypatch, outbound_fakes):
    posts, _records = outbound_fakes
    bridge = _fake_bridge(monkeypatch, running=True)

    result = telegram_send.send_telegram_message("already awake")

    assert result == "[Message sent.]"
    assert bridge["starts"] == 0
    assert len(posts) == 1


def test_repeated_sends_never_start_a_bridge(monkeypatch, outbound_fakes):
    posts, _records = outbound_fakes
    bridge = _fake_bridge(monkeypatch)

    telegram_send.send_telegram_message("first")
    telegram_send.send_telegram_message("second")

    assert bridge["starts"] == 0
    assert len(posts) == 2


def test_concurrent_sends_never_start_a_bridge(monkeypatch, outbound_fakes):
    posts, _records = outbound_fakes
    bridge = _fake_bridge(monkeypatch)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(telegram_send.send_telegram_message, [f"message-{i}" for i in range(8)]))

    assert all(r.startswith("[Message sent.]") for r in results)
    assert bridge["starts"] == 0
    assert len(posts) == 8


def test_send_failure_keeps_existing_telegram_error(monkeypatch, outbound_fakes):
    _posts, records = outbound_fakes
    bridge = _fake_bridge(monkeypatch, running=True)

    def fail_post(url, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(telegram_send.requests, "post", fail_post)

    result = telegram_send.send_telegram_message("will fail")

    assert result == "[Telegram send error: network down]"
    assert bridge["starts"] == 0
    assert records == []


def test_file_tool_never_starts_a_bridge_either(monkeypatch, outbound_fakes, tmp_path):
    posts, _records = outbound_fakes
    bridge = _fake_bridge(monkeypatch, running=True)
    payload = tmp_path / "report.txt"
    payload.write_text("safe fixture", encoding="utf-8")

    result = telegram_send.send_telegram_file(str(payload), "report")

    assert result == f"[Sent '{payload}' to Telegram.]"
    assert bridge["starts"] == 0
    assert len(posts) == 1


def test_embedded_bridge_shares_the_active_runtime_configuration():
    # The send tools read the same configuration as the embedded bridge in
    # this process; there is no subprocess/cwd/env reconstruction that could
    # silently bind them to the other checkout's data root.
    assert telegram_send.config is telegram_bridge.config
    assert telegram_send.load_prefs is telegram_bridge.load_prefs
