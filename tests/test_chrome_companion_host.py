"""BROWSER-COMPANION-01A -- the Chrome-launched native host: origin pinning
before any stdin read, hello validation, hub-unavailable semantics, direction-
checked relaying, real stdio framing in a subprocess, and content-free stderr."""
from __future__ import annotations

import io
import os
import socket
import struct
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from chrome_companion import native_host, protocol, state
from chrome_companion_testkit import (CANARY, EXT_ID, INSTANCE, ORIGIN, OTHER_EXT_ID, make_hub,
                                      ok_response, setup_companion, short_tmpdir, wait_until)

HOST_SCRIPT = Path(native_host.__file__).resolve()


def framed(message: dict) -> bytes:
    return protocol.encode_frame(message, protocol.MAX_FROM_EXTENSION_BYTES)


def ext_hello(**overrides) -> dict:
    hello = {"v": 1, "type": "hello", "extension_id": EXT_ID, "instance_id": INSTANCE,
             "extension_version": "0.1.0"}
    hello.update(overrides)
    return hello


def read_all(data: bytes) -> list[dict]:
    stream = io.BytesIO(data)
    out = []
    while True:
        message = protocol.read_frame(stream.read, protocol.MAX_TO_EXTENSION_BYTES)
        if message is None:
            return out
        out.append(message)


@pytest.fixture
def config(tmp_path):
    with short_tmpdir() as sdir:
        path = tmp_path / "host" / "host_config.json"
        state.write_private_json(path, {"version": 1, "extension_origin": ORIGIN,
                                        "socket_path": os.path.join(sdir, "run", "hub.sock")})
        yield path


class _ExplodingStdin:
    def read(self, n):
        raise AssertionError("stdin must not be read before the origin check")


def _argv(config, origin=ORIGIN, *extra):
    return ["native_host.py", "--config", str(config), origin, *extra]


# ── Origin pinning & argument handling ───────────────────────────────────

def test_wrong_origin_refused_before_reading_stdin(config):
    out = io.BytesIO()
    assert native_host.run(_argv(config, f"chrome-extension://{OTHER_EXT_ID}/"), _ExplodingStdin(), out) == 2
    assert out.getvalue() == b""


@pytest.mark.parametrize("argv_tail", [
    # Firefox launches hosts with (manifest path, extension id), never a chrome-extension origin.
    ["/home/user/.mozilla/native-messaging-hosts/org.lumina.chrome_companion.json", "lumina@example"],
    ["moz-extension://6f5b1c1e-0000-4000-8000-000000000000/"],
])
def test_firefox_style_invocations_are_refused(config, argv_tail):
    out = io.BytesIO()
    argv = ["native_host.py", "--config", str(config), *argv_tail]
    assert native_host.run(argv, _ExplodingStdin(), out) == 2
    assert out.getvalue() == b""


def test_windows_parent_window_flag_tolerated_but_nothing_else(config):
    out = io.BytesIO()
    code = native_host.run(_argv(config, ORIGIN, "--parent-window=1234"), io.BytesIO(b""), out)
    assert code == 0  # clean EOF before hello
    assert native_host.run(_argv(config, ORIGIN, "--evil"), _ExplodingStdin(), io.BytesIO()) == 2


def test_missing_or_insecure_config_refused(tmp_path, config):
    assert native_host.run(_argv(tmp_path / "nope.json"), _ExplodingStdin(), io.BytesIO()) == 2
    os.chmod(config, 0o644)
    assert native_host.run(_argv(config), _ExplodingStdin(), io.BytesIO()) == 2


@pytest.mark.parametrize("hello", [
    ext_hello(extension_id="p" * 32),           # spoofed id vs Chrome's origin
    ext_hello(instance_id="nothex"),
    ext_hello(v=2),
    {"v": 1, "type": "response"},
])
def test_bad_extension_hello_refused_without_output(config, hello):
    out = io.BytesIO()
    assert native_host.run(_argv(config), io.BytesIO(framed(hello)), out) == 3
    assert out.getvalue() == b""


def test_hub_unavailable_reports_status_and_exits(config):
    out = io.BytesIO()
    assert native_host.run(_argv(config), io.BytesIO(framed(ext_hello())), out) == 0
    assert read_all(out.getvalue()) == [{"v": 1, "type": "host_status", "state": "hub_unavailable"}]


# ── Relay (fake hub on a socketpair) ─────────────────────────────────────

class _Pipe:
    """A blocking stdin stand-in the test can feed incrementally."""

    def __init__(self):
        self._r, self._w = os.pipe()
        self._reader = os.fdopen(self._r, "rb", buffering=0)

    def read(self, n):
        return self._reader.read(n)

    def feed(self, data: bytes):
        os.write(self._w, data)

    def close(self):
        os.close(self._w)


def _run_host_with_fake_hub(config, script):
    """Run native_host.run() in a thread against a socketpair 'hub'."""
    hub_end, host_end = socket.socketpair()
    stdin, stdout = _Pipe(), io.BytesIO()
    box = {}
    thread = threading.Thread(target=lambda: box.setdefault(
        "code", native_host.run(_argv(config), stdin, stdout, connect=lambda path: host_end)), daemon=True)
    stdin.feed(framed(ext_hello()))
    thread.start()
    hub_end.settimeout(5)
    hello = protocol.read_frame(hub_end.recv, protocol.MAX_FROM_EXTENSION_BYTES)
    script(hub_end, stdin)
    thread.join(5)
    hub_end.close()
    return hello, read_all(stdout.getvalue()), box.get("code")


def test_relay_passes_legal_frames_both_ways(config, capfd):
    cid, rid = "a" * 32, "b" * 32
    request = {"v": 1, "type": "request", "connection_id": cid, "request_id": rid, "op": "extract_text",
               "tab_id": 7, "deadline_ms": 1_900_000_000_000, "args": {}}
    response = ok_response(request, {"text": CANARY, "total_chars": 5, "title": "t"}, tab_id=7,
                           observed={"url": "https://x.test/?token=SECRET123", "origin": "https://x.test",
                                     "document_id": None})
    relayed = {}

    def script(hub, stdin):
        hub.sendall(protocol.encode_frame({"v": 1, "type": "welcome", "connection_id": cid, "limits": {}},
                                          protocol.MAX_TO_EXTENSION_BYTES))
        hub.sendall(protocol.encode_frame(request, protocol.MAX_TO_EXTENSION_BYTES))
        stdin.feed(framed(response))
        relayed["response"] = protocol.read_frame(hub.recv, protocol.MAX_FROM_EXTENSION_BYTES)
        stdin.close()  # Chrome disconnects the port
        assert protocol.read_frame(hub.recv, protocol.MAX_FROM_EXTENSION_BYTES) is None

    hello, to_extension, code = _run_host_with_fake_hub(config, script)
    assert hello == {"v": 1, "type": "hello", "origin": ORIGIN, "extension_id": EXT_ID,
                     "instance_id": INSTANCE, "extension_version": "0.1.0", "host_version": "1"}
    assert [m["type"] for m in to_extension] == ["host_status", "welcome", "request"]
    assert to_extension[0]["state"] == "hub_connected"
    assert relayed["response"] == response
    assert code == 0
    err = capfd.readouterr().err
    for needle in (CANARY, "SECRET123", INSTANCE, "x.test"):
        assert needle not in err


def test_relay_refuses_hub_frames_of_the_wrong_direction(config):
    def script(hub, stdin):
        hub.sendall(protocol.encode_frame({"v": 1, "type": "response"}, protocol.MAX_TO_EXTENSION_BYTES))
        stdin.close()

    _, to_extension, code = _run_host_with_fake_hub(config, script)
    assert [m["type"] for m in to_extension] == ["host_status"]
    assert code == 4


def test_relay_refuses_extension_frames_of_the_wrong_direction(config):
    def script(hub, stdin):
        stdin.feed(framed({"v": 1, "type": "request", "op": "list_tabs"}))
        assert protocol.read_frame(hub.recv, protocol.MAX_FROM_EXTENSION_BYTES) is None
        stdin.close()

    _, _, code = _run_host_with_fake_hub(config, script)
    assert code == 0


def test_relay_refuses_oversized_extension_frame(config):
    def script(hub, stdin):
        stdin.feed(struct.pack("=I", protocol.MAX_FROM_EXTENSION_BYTES + 1))
        assert protocol.read_frame(hub.recv, protocol.MAX_FROM_EXTENSION_BYTES) is None
        stdin.close()

    _run_host_with_fake_hub(config, script)


# ── Real process, real stdio framing, real hub ───────────────────────────

def test_real_host_process_against_real_hub(tmp_path):
    with short_tmpdir() as sdir:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        socket_path = setup_companion(data_dir, sdir)
        config_path = tmp_path / "hc" / "host_config.json"
        state.write_private_json(config_path, {"version": 1, "extension_origin": ORIGIN,
                                               "socket_path": socket_path})
        hub = make_hub(data_dir)
        hub.start()
        proc = subprocess.Popen([sys.executable, str(HOST_SCRIPT), "--config", str(config_path), ORIGIN],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            proc.stdin.write(framed(ext_hello()))
            proc.stdin.flush()
            status = protocol.read_frame(proc.stdout.read, protocol.MAX_TO_EXTENSION_BYTES)
            welcome = protocol.read_frame(proc.stdout.read, protocol.MAX_TO_EXTENSION_BYTES)
            assert status == {"v": 1, "type": "host_status", "state": "hub_connected"}
            assert welcome["type"] == "welcome"
            assert wait_until(lambda: hub.current_connection_id() == welcome["connection_id"])

            box = {}
            thread = threading.Thread(target=lambda: box.setdefault(
                "response", hub.request("ping", timeout_s=5)), daemon=True)
            thread.start()
            request = protocol.read_frame(proc.stdout.read, protocol.MAX_TO_EXTENSION_BYTES)
            assert request["op"] == "ping" and request["connection_id"] == welcome["connection_id"]
            proc.stdin.write(framed(ok_response(request, {"extension_version": "0.1.0"})))
            proc.stdin.flush()
            thread.join(5)
            assert box["response"]["result"]["extension_version"] == "0.1.0"

            proc.stdin.close()  # Chrome closes the port
            assert proc.wait(5) == 0
            assert wait_until(lambda: hub.current_connection_id() is None)
        finally:
            if proc.poll() is None:
                proc.kill()
            hub.stop()
        assert INSTANCE not in proc.stderr.read().decode()


def test_real_host_exits_when_chrome_dies_even_with_stderr_gone(tmp_path):
    """Regression: when Chrome dies it closes the host's stdin AND stderr. The
    stdin reader's teardown logged to the dead stderr first; BrokenPipeError
    killed the thread before it shut the hub socket, leaving a zombie host and
    a hub that still reported Chrome as connected."""
    with short_tmpdir() as sdir:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        socket_path = setup_companion(data_dir, sdir)
        config_path = tmp_path / "hc" / "host_config.json"
        state.write_private_json(config_path, {"version": 1, "extension_origin": ORIGIN,
                                               "socket_path": socket_path})
        hub = make_hub(data_dir)
        hub.start()
        proc = subprocess.Popen([sys.executable, str(HOST_SCRIPT), "--config", str(config_path), ORIGIN],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            proc.stdin.write(framed(ext_hello()))
            proc.stdin.flush()
            protocol.read_frame(proc.stdout.read, protocol.MAX_TO_EXTENSION_BYTES)
            welcome = protocol.read_frame(proc.stdout.read, protocol.MAX_TO_EXTENSION_BYTES)
            assert wait_until(lambda: hub.current_connection_id() == welcome["connection_id"])
            proc.stderr.close()   # Chrome is gone: its end of our stderr closes...
            proc.stdout.close()
            proc.stdin.close()    # ...and so does our stdin.
            assert proc.wait(5) == 0
            assert wait_until(lambda: hub.current_connection_id() is None, timeout=3)
        finally:
            if proc.poll() is None:
                proc.kill()
            hub.stop()


def test_real_host_exits_when_lumina_hub_stops(tmp_path):
    with short_tmpdir() as sdir:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        socket_path = setup_companion(data_dir, sdir)
        config_path = tmp_path / "hc" / "host_config.json"
        state.write_private_json(config_path, {"version": 1, "extension_origin": ORIGIN,
                                               "socket_path": socket_path})
        hub = make_hub(data_dir)
        hub.start()
        proc = subprocess.Popen([sys.executable, str(HOST_SCRIPT), "--config", str(config_path), ORIGIN],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            proc.stdin.write(framed(ext_hello()))
            proc.stdin.flush()
            protocol.read_frame(proc.stdout.read, protocol.MAX_TO_EXTENSION_BYTES)
            protocol.read_frame(proc.stdout.read, protocol.MAX_TO_EXTENSION_BYTES)
            hub.stop()  # Lumina quits
            assert proc.wait(5) == 0  # host ends -> Chrome sees the port disconnect
        finally:
            if proc.poll() is None:
                proc.kill()
            hub.stop()
