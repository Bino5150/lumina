"""BROWSER-COMPANION-01A -- Lumina-side hub: identity/pairing, socket hygiene,
request semantics, lifecycle (fresh state on every reconnect), policy defense
in depth, and metadata-only telemetry. Real Unix sockets throughout."""
from __future__ import annotations

import hashlib
import logging
import os
import socket
import stat
import struct
import threading
import time
from types import SimpleNamespace

import pytest

from chrome_companion import protocol, state
from chrome_companion_testkit import (CANARY, EXT_ID, INSTANCE, ORIGIN, OTHER_EXT_ID,
                                      OTHER_INSTANCE, FakeHost, RecordingRecorder, connect_ready,
                                      error_response, make_hub, observed, ok_response,
                                      setup_companion, short_tmpdir, tab, wait_until)
from core.chrome_companion_hub import CompanionError


@pytest.fixture
def env(tmp_path):
    with short_tmpdir() as sdir:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        socket_path = setup_companion(data_dir, sdir)
        recorder = RecordingRecorder()
        hub = make_hub(data_dir, recorder)
        hub.start()
        hosts = []

        def connect(**hello):
            host, welcome = connect_ready(hub, socket_path, **hello)
            hosts.append(host)
            return host, welcome

        yield SimpleNamespace(hub=hub, data_dir=data_dir, socket_path=socket_path, recorder=recorder,
                              sdir=sdir, connect=connect, hosts=hosts)
        hub.stop()
        for host in hosts:
            host.close()


def _reject_reason(host):
    frame = host.recv()
    assert frame is not None and frame["type"] == "reject", frame
    return frame["reason"]


# ── Identity & pairing ───────────────────────────────────────────────────

def test_paired_instance_gets_fresh_welcome(env):
    host, welcome = env.connect()
    assert protocol.is_hex_id(welcome["connection_id"])
    assert welcome["limits"]["max_text_chars"] == protocol.MAX_TEXT_CHARS
    status = env.hub.status()
    assert status["connection"]["state"] == "ready"
    assert status["connection"]["instance"] == state.instance_fingerprint(INSTANCE)
    assert INSTANCE not in repr(status)


def test_wrong_extension_origin_rejected(env):
    host = FakeHost(env.socket_path)
    env.hosts.append(host)
    host.send({"v": 1, "type": "hello", "origin": f"chrome-extension://{OTHER_EXT_ID}/",
               "extension_id": OTHER_EXT_ID, "instance_id": INSTANCE,
               "extension_version": "0.1.0", "host_version": "1"})
    assert _reject_reason(host) == "wrong_origin"
    assert env.hub.current_connection_id() is None


def test_unpaired_profile_rejected(env):
    state.clear_pairing(env.data_dir)
    host = FakeHost(env.socket_path)
    env.hosts.append(host)
    host.send({"v": 1, "type": "hello", "origin": ORIGIN, "extension_id": EXT_ID,
               "instance_id": INSTANCE, "extension_version": "0.1.0", "host_version": "1"})
    assert _reject_reason(host) == "unpaired"
    assert env.hub.current_connection_id() is None
    with pytest.raises(CompanionError) as exc:
        env.hub.request("list_tabs", timeout_s=1)
    assert exc.value.code == "not_paired"


def test_wrong_paired_instance_rejected(env):
    host = FakeHost(env.socket_path)
    env.hosts.append(host)
    assert host.handshake(instance=OTHER_INSTANCE)["reason"] == "wrong_instance"
    assert env.hub.current_connection_id() is None


def test_pairing_is_bound_to_the_extension_origin(env):
    state.save_pairing(env.data_dir, instance_id=INSTANCE,
                       extension_origin=f"chrome-extension://{OTHER_EXT_ID}/")
    host = FakeHost(env.socket_path)
    env.hosts.append(host)
    assert host.handshake()["reason"] == "wrong_instance"


def test_wrong_local_peer_dropped_without_reply(tmp_path):
    with short_tmpdir() as sdir:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        socket_path = setup_companion(data_dir, sdir)
        hub = make_hub(data_dir, peer_uid_fn=lambda sock: os.getuid() + 1)
        hub.start()
        try:
            host = FakeHost(socket_path)
            host.send({"v": 1, "type": "hello", "origin": ORIGIN, "extension_id": EXT_ID,
                       "instance_id": INSTANCE, "extension_version": "0.1.0", "host_version": "1"})
            try:
                assert host.recv() is None
            except OSError:
                pass  # reset is equally a refusal
            assert hub.current_connection_id() is None
            host.close()
        finally:
            hub.stop()


@pytest.mark.parametrize("hello, reason", [
    ({"v": 1, "type": "hello"}, "bad_hello"),
    ({"v": 2, "type": "hello", "origin": ORIGIN, "extension_id": EXT_ID, "instance_id": INSTANCE,
      "extension_version": "0.1.0", "host_version": "1"}, "unsupported_version"),
    ({"v": 1, "type": "response", "origin": ORIGIN}, "bad_hello"),
    ({"v": 1, "type": "hello", "origin": ORIGIN, "extension_id": OTHER_EXT_ID, "instance_id": INSTANCE,
      "extension_version": "0.1.0", "host_version": "1"}, "bad_hello"),
])
def test_bad_and_future_hellos_rejected(env, hello, reason):
    host = FakeHost(env.socket_path)
    env.hosts.append(host)
    host.send(hello)
    assert _reject_reason(host) == reason


def test_install_record_removed_after_start_rejects(env):
    (state.companion_dir(env.data_dir) / state.INSTALL_FILENAME).unlink()
    host = FakeHost(env.socket_path)
    env.hosts.append(host)
    assert host.handshake()["reason"] == "not_installed"


# ── Socket hygiene ───────────────────────────────────────────────────────

def test_socket_is_owner_only_unix_socket(env):
    info = os.lstat(env.socket_path)
    assert stat.S_ISSOCK(info.st_mode)
    assert stat.S_IMODE(info.st_mode) == 0o600
    assert stat.S_IMODE(os.lstat(os.path.dirname(env.socket_path)).st_mode) == 0o700
    assert env.hub._listener.family == socket.AF_UNIX


def test_refuses_group_accessible_runtime_dir(tmp_path):
    with short_tmpdir() as sdir:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        socket_path = setup_companion(data_dir, sdir)
        os.makedirs(os.path.dirname(socket_path), mode=0o750)
        os.chmod(os.path.dirname(socket_path), 0o750)
        with pytest.raises(state.StateError):
            make_hub(data_dir).start()


def test_refuses_to_replace_non_socket_path(tmp_path):
    with short_tmpdir() as sdir:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        socket_path = setup_companion(data_dir, sdir)
        state.ensure_private_dir(os.path.dirname(socket_path))
        with open(socket_path, "w") as handle:
            handle.write("not a socket")
        with pytest.raises(CompanionError):
            make_hub(data_dir).start()
        assert open(socket_path).read() == "not a socket"


def test_second_hub_refused_while_first_listens_and_stale_socket_replaced(env, tmp_path):
    with pytest.raises(CompanionError) as exc:
        make_hub(env.data_dir).start()
    assert "already owns" in exc.value.message
    env.hub.stop()
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(env.socket_path)
    stale.close()  # leaves a dead socket file behind, like a crashed Lumina
    env.hub.start()
    env.connect()


# ── Requests ─────────────────────────────────────────────────────────────

def test_list_tabs_applies_policy_in_depth(env):
    host, _ = env.connect()
    tabs = [
        tab(1),
        tab(2, url="https://private.test/", title="Private", incognito=True),
        tab(3, url="chrome://settings/passwords", title="Passwords"),          # unmarked by extension
        tab(4, url="https://passwords.google.com/", title="Google Passwords"),  # unmarked by extension
        tab(5, url="https://github.com/", title="GitHub", site_access="not_granted"),
        # R1/F2: trailing-dot / case aliases of restricted hosts, unmarked by the extension.
        tab(6, url="https://accounts.google.com./", title="Sign in"),
        tab(8, url="https://PASSWORDS.GOOGLE.COM./", title="Google Passwords"),
    ]
    host.serve(lambda req: ok_response(req, {"tabs": tabs, "total": len(tabs)}))
    result = env.hub.request("list_tabs", timeout_s=3)["result"]
    by_id = {t["tab_id"]: t for t in result["tabs"]}
    assert set(by_id) == {1, 3, 4, 5, 6, 8}
    for restricted_id in (3, 4, 6, 8):
        assert by_id[restricted_id]["restricted"] is True
        assert by_id[restricted_id]["url"] is None and by_id[restricted_id]["title"] is None
        assert by_id[restricted_id]["site_access"] == "restricted"
    assert by_id[1]["url"].startswith("https://www.reddit.com/")


def test_extract_text_round_trip(env):
    host, _ = env.connect()
    host.serve(lambda req: ok_response(req, {"text": "hello world", "total_chars": 11, "title": "T"},
                                       tab_id=7, observed=observed()))
    response = env.hub.request("extract_text", tab_id=7, args={"max_chars": 500}, timeout_s=3)
    assert response["result"]["text"] == "hello world"
    assert response["observed"]["document_id"] == "doc-1"
    request = host.requests[0]
    assert request["op"] == "extract_text" and request["tab_id"] == 7
    assert request["deadline_ms"] > time.time() * 1000


@pytest.mark.parametrize("obs", [
    observed(url="chrome://settings/", origin="chrome://settings"),
    observed(url="https://accounts.google.com/", origin="https://accounts.google.com"),
    # R1/F2: aliases carrying exactly the origin a pre-R1 classifier derived for
    # them, so only canonical restricted-host comparison can refuse them.
    observed(url="https://accounts.google.com./", origin="https://accounts.google.com."),
    observed(url="https://PASSWORDS.GOOGLE.COM./", origin="https://passwords.google.com."),
    observed(url="https://accounts.google%2Ecom/", origin="https://accounts.google%2ecom"),
    observed(origin="https://evil.test"),    # origin disagrees with the URL
    None,
])
def test_content_from_restricted_or_unverifiable_surface_is_discarded(env, obs):
    host, _ = env.connect()
    host.serve(lambda req: ok_response(req, {"text": CANARY, "total_chars": 5, "title": "T"},
                                       tab_id=7, observed=obs))
    with pytest.raises(CompanionError) as exc:
        env.hub.request("extract_text", tab_id=7, args={}, timeout_s=3)
    assert exc.value.code == "restricted_surface"
    assert CANARY not in exc.value.message


def test_get_tab_for_incognito_tab_is_not_found(env):
    host, _ = env.connect()
    host.serve(lambda req: ok_response(req, tab(9, incognito=True), tab_id=9))
    with pytest.raises(CompanionError) as exc:
        env.hub.request("get_tab", tab_id=9, timeout_s=3)
    assert exc.value.code == "tab_not_found"


def test_answer_for_a_different_tab_is_rejected(env):
    host, _ = env.connect()
    host.serve(lambda req: ok_response(req, tab(8), tab_id=8))
    with pytest.raises(CompanionError) as exc:
        env.hub.request("get_tab", tab_id=7, timeout_s=3)
    assert exc.value.code == "tab_mismatch"


def test_extension_error_is_an_explicit_failure(env):
    host, _ = env.connect()
    host.serve(lambda req: error_response(req, "site_access_required", "grant it", tab_id=7))
    with pytest.raises(CompanionError) as exc:
        env.hub.request("extract_text", tab_id=7, args={}, timeout_s=3)
    assert exc.value.code == "site_access_required" and exc.value.executed is False


def test_malformed_result_rejected(env):
    host, _ = env.connect()
    host.serve(lambda req: ok_response(req, {"text": "x"}, tab_id=7, observed=observed()))
    with pytest.raises(CompanionError) as exc:
        env.hub.request("extract_text", tab_id=7, args={}, timeout_s=3)
    assert exc.value.code == "malformed_response"


def test_response_carrying_an_old_connection_id_is_dropped(env):
    host, _ = env.connect()
    stale_cid = "c" * 32

    def answer(req):
        return dict(ok_response(req, {"extension_version": "0.1.0"}), connection_id=stale_cid)

    host.serve(answer)
    with pytest.raises(CompanionError) as exc:
        env.hub.request("ping", timeout_s=0.6)
    assert exc.value.code == "timeout"
    assert {"reason": "wrong_connection"}.items() <= env.recorder.of("chrome_companion.response_dropped")[0].items()


def test_unsolicited_response_dropped_and_connection_survives(env):
    host, welcome = env.connect()
    host.send({"v": 1, "type": "response", "connection_id": welcome["connection_id"],
               "request_id": "d" * 32, "ok": True, "result": {"extension_version": "x"},
               "tab_id": None, "observed": None, "truncated": False})
    host.serve(lambda req: ok_response(req, {"extension_version": "0.1.0"}))
    assert env.hub.request("ping", timeout_s=3)["result"]["extension_version"] == "0.1.0"
    assert env.hub.current_connection_id() == welcome["connection_id"]


def test_duplicate_response_only_first_is_delivered(env):
    host, _ = env.connect()
    host.serve(lambda req: [ok_response(req, {"extension_version": "first"}),
                            ok_response(req, {"extension_version": "second"})])
    assert env.hub.request("ping", timeout_s=3)["result"]["extension_version"] == "first"
    assert wait_until(lambda: any(e["reason"] == "unsolicited"
                                  for e in env.recorder.of("chrome_companion.response_dropped")))


def test_duplicate_request_registration_is_refused(env):
    env.connect()
    conn = env.hub._current
    conn.register("e" * 32, "ping", None)
    with pytest.raises(CompanionError) as exc:
        conn.register("e" * 32, "ping", None)
    assert exc.value.code == "duplicate_request"


def test_timeout_then_late_answer_is_dropped(env):
    host, _ = env.connect()
    held = []
    host.serve(lambda req: held.append(req))
    with pytest.raises(CompanionError) as exc:
        env.hub.request("ping", timeout_s=0.3)
    assert exc.value.code == "timeout"
    host.send(ok_response(held[0], {"extension_version": "late"}))
    assert wait_until(lambda: any(e["reason"] == "unsolicited"
                                  for e in env.recorder.of("chrome_companion.response_dropped")))


def test_in_flight_requests_are_bounded(env):
    env.connect()
    conn = env.hub._current
    for i in range(protocol.MAX_IN_FLIGHT):
        conn.register(f"{i:032x}", "ping", None)
    with pytest.raises(CompanionError) as exc:
        conn.register("f" * 32, "ping", None)
    assert exc.value.code == "busy"


# ── Lifecycle ────────────────────────────────────────────────────────────

def _request_in_background(hub, *args, **kwargs):
    box = {}

    def run():
        try:
            box["result"] = hub.request(*args, **kwargs)
        except CompanionError as exc:
            box["error"] = exc
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


def test_host_exit_mid_request_fails_fast_and_explicitly(env):
    host, _ = env.connect()
    host.serve(lambda req: host.close())
    started = time.monotonic()
    with pytest.raises(CompanionError) as exc:
        env.hub.request("list_tabs", timeout_s=8)
    assert exc.value.code == "host_closed"
    assert time.monotonic() - started < 3
    assert wait_until(lambda: env.hub.current_connection_id() is None)
    with pytest.raises(CompanionError) as exc:
        env.hub.request("list_tabs", timeout_s=1)
    assert exc.value.code == "not_connected"


def test_pause_during_request_fails_it_and_later_requests_report_paused(env):
    host, welcome = env.connect()
    received = threading.Event()
    host.serve(lambda req: received.set())
    thread, box = _request_in_background(env.hub, "extract_text", tab_id=7, args={}, timeout_s=8)
    assert received.wait(3)
    host.send({"v": 1, "type": "bye", "connection_id": welcome["connection_id"], "reason": "paused"})
    thread.join(3)
    assert box["error"].code == "paused"
    with pytest.raises(CompanionError) as exc:
        env.hub.request("list_tabs", timeout_s=1)
    assert exc.value.code == "paused"
    assert "PAUSE" in exc.value.message
    assert env.hub.status()["last_disconnect"]["reason"] == "paused"


def test_reconnect_creates_fresh_lifecycle_state(env):
    host1, welcome1 = env.connect()
    received = threading.Event()
    host1.serve(lambda req: received.set())
    thread, box = _request_in_background(env.hub, "list_tabs", timeout_s=8)
    assert received.wait(3)
    old_request = host1.requests[0]
    old_conn = env.hub._current
    host1.close()
    thread.join(3)
    assert box["error"].code == "host_closed"
    assert old_conn.closed and old_conn._in_flight == {}

    host2, welcome2 = env.connect()
    assert welcome2["connection_id"] != welcome1["connection_id"]
    new_conn = env.hub._current
    assert new_conn is not old_conn
    assert new_conn._in_flight == {}
    assert new_conn.seq == old_conn.seq + 1
    # A stale answer to the dead connection's request, replayed on the new one, is dropped.
    host2.send(ok_response(old_request, {"tabs": [], "total": 0}))
    host2.serve(lambda req: ok_response(req, {"tabs": [tab(1)], "total": 1}))
    assert [t["tab_id"] for t in env.hub.request("list_tabs", timeout_s=3)["result"]["tabs"]] == [1]
    assert wait_until(lambda: any(e["reason"] == "wrong_connection"
                                  for e in env.recorder.of("chrome_companion.response_dropped")))


def test_newer_connection_supersedes_older(env):
    host1, welcome1 = env.connect()
    old_conn = env.hub._current
    received = threading.Event()
    host1.serve(lambda req: received.set())
    thread, box = _request_in_background(env.hub, "list_tabs", timeout_s=8)
    assert received.wait(3)
    old_request = host1.requests[0]
    host2, welcome2 = env.connect()
    thread.join(3)
    assert box["error"].code == "superseded"
    assert host1.closed.wait(3)
    assert env.hub.current_connection_id() == welcome2["connection_id"]
    # Superseding is a reconnect too: fresh identity and fresh in-flight state.
    new_conn = env.hub._current
    assert welcome2["connection_id"] != welcome1["connection_id"]
    assert new_conn is not old_conn and new_conn._in_flight is not old_conn._in_flight
    # The superseded connection's answer, replayed on the new one, is dropped.
    host2.send(ok_response(old_request, {"tabs": [], "total": 0}))
    assert wait_until(lambda: any(e["reason"] == "wrong_connection"
                                  for e in env.recorder.of("chrome_companion.response_dropped")))


def test_hub_stop_then_start_builds_new_lifecycle(env):
    host, _ = env.connect()
    received = threading.Event()
    host.serve(lambda req: received.set())
    thread, box = _request_in_background(env.hub, "list_tabs", timeout_s=8)
    assert received.wait(3)
    first_thread = env.hub._accept_thread
    env.hub.stop()
    thread.join(3)
    assert box["error"].code == "hub_stopped"
    assert not os.path.exists(env.socket_path)
    with pytest.raises(CompanionError) as exc:
        env.hub.request("list_tabs", timeout_s=1)
    assert exc.value.code == "hub_unavailable"
    assert not first_thread.is_alive()

    env.hub.start()
    assert env.hub._accept_thread is not first_thread
    host2, _ = env.connect()
    host2.serve(lambda req: ok_response(req, {"extension_version": "0.1.0"}))
    assert env.hub.request("ping", timeout_s=3)["ok"] is True


def test_hub_absent_is_an_explicit_failure(tmp_path):
    hub = make_hub(tmp_path)
    with pytest.raises(CompanionError) as exc:
        hub.request("list_tabs", timeout_s=1)
    assert exc.value.code == "hub_unavailable"


@pytest.mark.parametrize("raw", [
    struct.pack("=I", 4) + b"\xff\xfe\xfd\xfc",                        # invalid UTF-8
    struct.pack("=I", protocol.MAX_FROM_EXTENSION_BYTES + 1),          # oversized
    struct.pack("=I", 2) + b"[]",                                      # not an object
    struct.pack("=I", 23) + b'{"v":1,"type":"request"}',               # wrong direction
])
def test_malformed_frame_drops_the_connection(env, raw):
    host, _ = env.connect()
    received = threading.Event()
    host.serve(lambda req: (received.set(), host.send_raw(raw))[1])
    with pytest.raises(CompanionError) as exc:
        env.hub.request("list_tabs", timeout_s=8)
    assert exc.value.code == "protocol_violation"
    assert wait_until(lambda: env.hub.current_connection_id() is None)


# ── Telemetry ────────────────────────────────────────────────────────────

def test_telemetry_is_metadata_only(env, caplog):
    caplog.set_level(logging.DEBUG)
    secret_url = "https://mail.google.com/mail/u/0/?token=SECRET123#inbox/FMfcgz"
    host, _ = env.connect()
    host.serve(lambda req: ok_response(
        req, {"text": CANARY, "total_chars": len(CANARY), "title": "Inbox - lumina@example.test"},
        tab_id=7, observed=observed(url=secret_url, origin="https://mail.google.com")))
    env.hub.request("extract_text", tab_id=7, args={}, timeout_s=3)
    blob = repr(env.recorder.events) + caplog.text
    for needle in (CANARY, "SECRET123", secret_url, "lumina@example.test", INSTANCE):
        assert needle not in blob
    event = env.recorder.of("chrome_companion.request")[-1]
    assert event["origin_hash"] == hashlib.sha256(b"https://mail.google.com").hexdigest()[:12]
    assert event["result"] == "ok" and event["op"] == "extract_text"
    assert event["response_bytes"] > 0


def test_recorder_failure_never_breaks_a_request(tmp_path):
    with short_tmpdir() as sdir:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        socket_path = setup_companion(data_dir, sdir)
        hub = make_hub(data_dir, RecordingRecorder(fail=True))
        hub.start()
        try:
            host, _ = connect_ready(hub, socket_path)
            host.serve(lambda req: ok_response(req, {"extension_version": "0.1.0"}))
            assert hub.request("ping", timeout_s=3)["ok"] is True
            host.close()
        finally:
            hub.stop()
