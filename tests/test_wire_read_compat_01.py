"""WIRE-READ-COMPAT-01: bounded reads, transport edges and cursor safety.

Deterministic transport/protocol guards run without a sibling Wire install.
Integration cases spawn isolated relays with disposable lumina test seats;
override AIVENGERS_WIRE_MERGED_SRC / AIVENGERS_WIRE_OLD_SRC for pinned sources.
No test connects to the production socket or reads production credentials.
"""

from collections import deque
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import pytest

from tools import aivengers_wire as wire


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def canonical(receipt):
    # Post receipts add a duplicate flag; stored messages and events do not.
    return {key: value for key, value in receipt.items() if key != "duplicate"}


class FragmentSocket:
    """Deterministic stream fragmentation, including split UTF-8 characters."""

    def __init__(self, *chunks):
        self.chunks = deque(chunks)
        self.sent = []
        self.closed = False

    def recv(self, count):
        if not self.chunks:
            return b""
        chunk = self.chunks.popleft()
        if len(chunk) > count:
            self.chunks.appendleft(chunk[count:])
        return chunk[:count]

    def sendall(self, data):
        self.sent.append(json.loads(data))

    def connect(self, path):
        pass

    def settimeout(self, value):
        pass

    def close(self):
        self.closed = True


def split_bytes(raw, size):
    return [raw[i:i + size] for i in range(0, len(raw), size)]


@pytest.mark.parametrize("size", [1, 4093, 65536])
def test_response_accepts_exact_incoming_ceiling(size, tmp_path):
    raw = encoded({"ok": True, "body": ""})
    value = {"ok": True, "body": "x" * (131072 - len(raw))}
    raw = encoded(value)
    assert len(raw) == 131072
    assert wire.AivengersWireClient(state_dir=tmp_path)._recv_frame(
        FragmentSocket(*split_bytes(raw, size))
    ) == value


@pytest.mark.parametrize("raw", [
    encoded({"body": "x" * 131072}),
    b"x" * 131072,
    b'{"ok":true',
    b"not-json\n",
    b"[]\n",
    b'{"body":"\xff"}\n',
    b'{"body":NaN}\n',
    b'{"body":Infinity}\n',
    b'{"body":' + b"1" * 5000 + b"}\n",
    b"[" * 2000 + b"0" + b"]" * 2000 + b"\n",
    b"",
])
def test_response_rejects_oversize_malformed_and_incomplete(raw, tmp_path):
    with pytest.raises(wire.WireFrameError):
        wire.AivengersWireClient(state_dir=tmp_path)._recv_frame(FragmentSocket(raw))


def test_outbound_ceiling_remains_exactly_64_kib():
    overhead = len(encoded({"body": ""}))
    assert len(wire.AivengersWireClient._encode_frame({"body": "x" * (65536 - overhead)})) == 65536
    with pytest.raises(wire.WireFrameError):
        wire.AivengersWireClient._encode_frame({"body": "x" * (65537 - overhead)})


@pytest.mark.parametrize("oversized", [False, True])
def test_watch_fragmented_max_frame_and_coalesced_following_event(monkeypatch, tmp_path, oversized):
    event = {"ok": True, "event": "message", "body": ""}
    event["body"] = "x" * (131072 - len(encoded(event)) + int(oversized))
    raw = encoded(event)
    assert len(raw) == 131072 + int(oversized)
    # Auth is a separate exchange. Subscription, event and timeout may be
    # coalesced; the maximum event also crosses multiple recv boundaries.
    stream = encoded({"ok": True, "event": "subscribed"}) + raw + encoded({"ok": True, "event": "timeout"})
    sock = FragmentSocket(encoded({"ok": True}), *split_bytes(stream, 4093))
    monkeypatch.setattr(wire.socket, "socket", lambda *a: sock)
    client = wire.AivengersWireClient(state_dir=tmp_path)
    monkeypatch.setattr(client, "_load_token", lambda: "disposable-test-token")
    events = client.watch(timeout_ms=1000)
    assert next(events)["event"] == "subscribed"
    if oversized:
        with pytest.raises(wire.WireFrameError):
            next(events)
    else:
        assert next(events) == event
        assert next(events)["event"] == "timeout"
        with pytest.raises(StopIteration):
            next(events)
    assert sock.closed
    assert client._watch_socket is None
    assert client._live_sockets == set()


def test_stream_checks_terminated_frame_size_before_yield():
    # Small custom cap forces the newline and oversize frame into one recv,
    # covering the old streaming reader's terminator-before-size-check hole.
    stream = wire.AivengersWireClient._iter_frames(
        FragmentSocket(b"x" * 32 + b"\n"), max_frame=32, idle_timeout=1,
    )
    with pytest.raises(wire.WireFrameError):
        next(stream)


def test_stream_coalesced_frames_are_bounded_individually():
    frames = wire.AivengersWireClient._iter_frames(
        FragmentSocket(b"abc\ndef\nghi\n"), max_frame=4, idle_timeout=1,
    )
    assert list(frames) == [b"abc", b"def", b"ghi"]


def page(ids, more=False, cursor=None):
    return {"ok": True, "messages": [{"id": i} for i in ids], "has_more": more, "next_after": cursor}


def scripted_client(monkeypatch, tmp_path, responses):
    client = wire.AivengersWireClient(state_dir=tmp_path)
    responses = iter(responses)
    requests = []

    @contextmanager
    def session():
        yield None

    def exchange(sock, payload):
        requests.append(payload)
        value = next(responses)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(client, "_session", session)
    monkeypatch.setattr(client, "_exchange", exchange)
    return client, requests


@pytest.mark.parametrize("response", [
    page([3], True, None), page([3], True, 2), page([3], True, 4),
    page([3], True, "3"), page([3], True, True), page([], True, 3),
    page([3], False, 3), page([3], "true", 3),
    {"messages": [{"id": 3}], "has_more": True},
    {"messages": [{"id": 3}], "next_after": 3},
    {"messages": None}, {"messages": [None]},
    page([3, 3]), page([4, 3]), page([True]), page([0]), page(["3"]),
])
def test_malformed_message_pages_never_advance(monkeypatch, tmp_path, response):
    client, requests = scripted_client(monkeypatch, tmp_path, [
        {"last_message_id": 0}, response,
    ])
    with pytest.raises(wire.WireFrameError):
        client.read_unseen()
    assert not any(r["op"] == "advance" for r in requests)


@pytest.mark.parametrize("second", [
    page([3], True, 3),  # repeated row/cursor
    page([2], True, 2),  # backwards loop
    {"messages": [{"id": 8}]},  # metadata disappears mid-pagination
    wire.WireUnavailableError("interrupted read"),
])
def test_failed_continuation_never_advance(monkeypatch, tmp_path, second):
    client, requests = scripted_client(monkeypatch, tmp_path, [
        {"last_message_id": 0}, page([3], True, 3), second,
    ])
    with pytest.raises(wire.WireError):
        client.read_unseen()
    assert [r["op"] for r in requests] == ["cursor", "read", "read"]


def test_limit_termination_and_gaps_preserve_contract(monkeypatch, tmp_path):
    client, requests = scripted_client(monkeypatch, tmp_path, [
        {"last_message_id": 1}, page([3, 7], True, 7), page([12], True, 12),
        {"last_message_id": 12},
    ])
    result = client.read_unseen(limit=3)
    assert result == {"messages": [{"id": 3}, {"id": 7}, {"id": 12}], "cursor": 12, "advanced": True}
    assert [r["limit"] for r in requests if r["op"] == "read"] == [3, 1]
    assert requests[-1] == {"op": "advance", "channel": "general", "last_message_id": 12}


def test_legacy_reads_and_nonmutating_peek(monkeypatch, tmp_path):
    client, requests = scripted_client(monkeypatch, tmp_path, [
        {"last_message_id": 2}, {"messages": [{"id": 5}, {"id": 9}]},
    ])
    assert client.read_unseen(advance=False) == {
        "messages": [{"id": 5}, {"id": 9}], "cursor": 2, "advanced": False,
    }
    assert [r["op"] for r in requests] == ["cursor", "read"]


def test_overfull_page_is_failure_not_silent_slice(monkeypatch, tmp_path):
    client, _ = scripted_client(monkeypatch, tmp_path, [page([1, 2])])
    with pytest.raises(wire.WireFrameError):
        client.read_since(0, limit=1)


@pytest.mark.parametrize("cursor", [None, -1, True, "0"])
def test_invalid_durable_cursor_refused_before_read(monkeypatch, tmp_path, cursor):
    client, requests = scripted_client(monkeypatch, tmp_path, [{"last_message_id": cursor}])
    with pytest.raises(wire.WireFrameError):
        client.read_unseen()
    assert [r["op"] for r in requests] == ["cursor"]


def channel_page(names, more=False, cursor=None):
    return {"channels": [{"channel": n} for n in names], "has_more": more, "next_after": cursor}


def test_channel_enumeration_follows_pages(monkeypatch, tmp_path):
    client, requests = scripted_client(monkeypatch, tmp_path, [
        channel_page(["a", "b"], True, "b"), channel_page(["z"]),
    ])
    assert client.channels() == [{"channel": n} for n in ["a", "b", "z"]]
    assert requests == [{"op": "channels", "limit": 500}, {"op": "channels", "limit": 500, "after": "b"}]


@pytest.mark.parametrize("second", [
    channel_page(["b"], True, "b"), channel_page(["a"], True, "a"),
    channel_page(["z"], True, "zz"), channel_page([], True, "z"),
    {"channels": [{"channel": "z"}]}, channel_page(["bad channel"]),
    wire.WireProtocolError("invalid_request", "unknown fields: limit"),
])
def test_invalid_channel_continuation_does_not_fallback(monkeypatch, tmp_path, second):
    client, requests = scripted_client(monkeypatch, tmp_path, [
        channel_page(["b"], True, "b"), second,
    ])
    with pytest.raises(wire.WireError):
        client.channels()
    assert len(requests) == 2


def test_channel_enumeration_bound_fails_loudly(monkeypatch, tmp_path):
    monkeypatch.setattr(wire, "MAX_CHANNEL_PAGES", 2)
    client, requests = scripted_client(monkeypatch, tmp_path, [
        channel_page(["a"], True, "a"), channel_page(["b"], True, "b"),
    ])
    with pytest.raises(wire.WireFrameError, match="page bound"):
        client.channels()
    assert len(requests) == 2


def test_old_channel_protocol_fallback_uses_new_connection(monkeypatch, tmp_path):
    client, requests = scripted_client(monkeypatch, tmp_path, [
        wire.WireProtocolError("invalid_request", "unknown fields: limit"),
        {"channels": [{"channel": "general"}]},
    ])
    sessions = []

    @contextmanager
    def session():
        sessions.append(object())
        yield sessions[-1]

    monkeypatch.setattr(client, "_session", session)
    assert client.channels() == [{"channel": "general"}]
    assert len(sessions) == 2
    assert requests == [{"op": "channels", "limit": 500}, {"op": "channels"}]


@pytest.mark.parametrize("error", [
    wire.WireProtocolError("authentication_failed", "bad token"),
    wire.WireProtocolError("operation_failed", "inventory too large"),
    wire.WireProtocolError("invalid_request", "other refusal"),
])
def test_channel_fallback_does_not_mask_other_errors(monkeypatch, tmp_path, error):
    client, requests = scripted_client(monkeypatch, tmp_path, [error])
    with pytest.raises(wire.WireProtocolError) as info:
        client.channels()
    assert info.value is error
    assert len(requests) == 1


class DisposableRelay:
    def __init__(self, source, state):
        self.state = state
        self.env = dict(os.environ, PYTHONPATH=str(source),
                        AIVENGERS_WIRE_STATE_DIR=str(state), PYTHONDONTWRITEBYTECODE="1")
        subprocess.run([sys.executable, "-m", "aivengers_wire", "init", "--seat", "lumina"],
                       env=self.env, check=True, capture_output=True, timeout=30)
        self.server = subprocess.Popen([sys.executable, "-m", "aivengers_wire", "serve"],
                                       env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop(self):
        self.server.terminate()
        try:
            self.server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.server.kill()
            self.server.wait(timeout=10)


@pytest.fixture(params=["merged", "old"])
def relay_client(request, tmp_path):
    kind = request.param
    default = Path.home() / ("aivengers-release" if kind == "merged" else "aivengers") / "src"
    source = Path(os.environ.get(f"AIVENGERS_WIRE_{kind.upper()}_SRC", str(default)))
    if not (source / "aivengers_wire" / "__main__.py").is_file():
        pytest.skip(f"{kind} Wire source absent; set AIVENGERS_WIRE_{kind.upper()}_SRC")
    relay = DisposableRelay(source, tmp_path / "state")
    try:
        client = wire.AivengersWireClient(state_dir=relay.state, timeout=2)
        deadline = time.monotonic() + 10
        while True:
            try:
                client.ping()
                break
            except wire.WireUnavailableError:
                assert relay.server.poll() is None, "disposable relay exited"
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
        with client._session() as sock:
            probe = client._exchange(sock, {"op": "read", "channel": "general", "after": 0, "limit": 1})
        if ("has_more" in probe) != (kind == "merged"):
            pytest.skip(f"{source} is not the requested {kind} protocol generation")
        yield kind, client
    finally:
        relay.stop()


def test_real_old_and_merged_public_contracts(relay_client):
    kind, client = relay_client
    messages = [canonical(client.post(f"message {i}")) for i in range(6)]
    assert client.read_since(0, limit=3) == messages[:3]
    assert client.cursor() == 0
    assert client.read_unseen(limit=4, advance=False) == {
        "messages": messages[:4], "cursor": 0, "advanced": False,
    }
    assert client.cursor() == 0
    assert client.read_unseen(limit=4) == {
        "messages": messages[:4], "cursor": messages[3]["id"], "advanced": True,
    }
    assert client.read_unseen()["messages"] == messages[4:]
    assert client.read_unseen() == {"messages": [], "cursor": messages[-1]["id"], "advanced": False}
    assert client.channels() == [{"channel": "general", "message_count": 6, "latest_message_id": messages[-1]["id"]}]
    assert all(m["owner"] is False and m["authority"] == "peer" for m in messages)


@pytest.mark.parametrize("method", ["read_since", "peek", "read_unseen"])
def test_real_twenty_10k_messages_across_byte_trimmed_pages(relay_client, monkeypatch, method):
    kind, client = relay_client
    if kind != "merged":
        pytest.skip("byte-trimmed pagination is a merged-relay feature")
    messages = []
    for i in range(20):
        messages.append(canonical(client.post(f"{i}:" + "x" * 10240)))
        client.post("global id gap", channel="noise")
    pages = []
    original = client._exchange

    def exchange(sock, payload):
        result = original(sock, payload)
        if payload["op"] == "read":
            pages.append((dict(payload), result))
        return result

    monkeypatch.setattr(client, "_exchange", exchange)
    if method == "read_since":
        result = client.read_since(0, limit=20)
    else:
        result = client.read_unseen(limit=20, advance=method != "peek")["messages"]
    assert result == messages
    assert len(pages) >= 4
    assert all(len(encoded(response)) <= 65536 for _, response in pages)
    assert all(response["next_after"] == response["messages"][-1]["id"]
               for _, response in pages if response["has_more"])
    assert client.cursor() == (messages[-1]["id"] if method == "read_unseen" else 0)
    if method == "read_unseen":
        client.post("late arrival")
        assert [m["body"] for m in client.read_unseen()["messages"]] == ["late arrival"]


def test_real_large_legal_message_response_read_and_watch(relay_client):
    kind, client = relay_client
    if kind != "merged":
        pytest.skip("expanded response ceiling is a merged-relay feature")
    events = client.watch(channels=["general"], timeout_ms=10000)
    try:
        assert next(events)["event"] == "subscribed"
        body = "é" * 32700
        assert len(encoded({"op": "post", "channel": "general", "body": body})) <= 65536
        message = canonical(client.post(body))
        assert len(encoded({"ok": True, "message": message})) > 65536
        assert client.read_since(0) == [message]
        event = next(events)
        assert event["message"] == message
        assert 65536 < len(encoded(event)) <= 131072
        assert client.cursor() == 0
    finally:
        events.close()


@pytest.mark.parametrize("failure", ["disconnect", "malformed", "loop"])
def test_real_pagination_failure_preserves_durable_cursor(relay_client, monkeypatch, failure):
    kind, client = relay_client
    if kind != "merged":
        pytest.skip("byte-trimmed pagination is a merged-relay feature")
    for _ in range(20):
        client.post("x" * 10240)
    original = client._exchange
    reads = 0
    advances = []

    def exchange(sock, payload):
        nonlocal reads
        if payload["op"] == "advance":
            advances.append(payload)
        if payload["op"] == "read":
            reads += 1
            if reads == 2:
                if failure == "disconnect":
                    sock.shutdown(socket.SHUT_RDWR)
                    return client._recv_frame(sock)
                elif failure == "malformed":
                    return {"messages": "not a page"}
                else:
                    return page([payload["after"]], True, payload["after"])
        return original(sock, payload)

    monkeypatch.setattr(client, "_exchange", exchange)
    with pytest.raises(wire.WireError):
        client.read_unseen(limit=20)
    assert not advances
    assert client.cursor() == 0
    monkeypatch.setattr(client, "_exchange", original)
    assert len(client.read_unseen(limit=20)["messages"]) == 20


def test_real_legacy_response_over_64k(relay_client):
    kind, client = relay_client
    if kind != "old":
        pytest.skip("merged relay byte-trims multi-message responses to 64 KiB")
    expected = [canonical(client.post("x" * 10240)) for _ in range(8)]
    assert 65536 < len(encoded({"ok": True, "messages": expected})) <= 131072
    assert client.read_since(0, limit=8) == expected


@pytest.mark.parametrize("limit", [0, True, 201, "2"])
def test_real_invalid_limits_keep_relay_refusal_contract(relay_client, limit):
    _, client = relay_client
    with pytest.raises(wire.WireProtocolError) as info:
        client.read_unseen(limit=limit)
    assert info.value.error_code == "invalid_request"
    assert client.cursor() == 0


def test_real_channel_inventory_multiple_pages(relay_client):
    kind, client = relay_client
    # Old protocol must still return the same list contract via fallback.
    count = 505 if kind == "merged" else 3
    for i in range(count):
        client.post("channel seed", channel=f"c{i:04d}")
    rows = client.channels()
    assert [row["channel"] for row in rows] == [f"c{i:04d}" for i in range(count)]
    assert all(row["message_count"] == 1 for row in rows)
