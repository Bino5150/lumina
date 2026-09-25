"""BROWSER-COMPANION-01A-R1 / F1 -- superseded-request atomicity.

Goblin's adversarial review found that a response could be popped from the
old connection's in-flight map, then -- before its requester was woken -- the
connection was superseded; close() could no longer see the waiter, and the
old request returned SUCCESS although a different connection was current.

Law under test: for every waiter exactly ONE terminal outcome wins --
delivered success, or cancellation by supersede / PAUSE / host loss / hub
stop -- never both, and success is committed only while its connection is
still valid.

The deterministic seams below hold a request at the two pre-commit points of
the real hub.request() path, through real Unix sockets and the real _serve
handshake:

    "staged": the reader has accepted the response, held BEFORE it signals
              the requester (Goblin's exact barrier);
    "woken":  the requester has been signalled, held BEFORE it consumes the
              response.

Both wrap only the waiter's threading.Event, so they apply to any
implementation of the hub, including the frozen pre-R1 candidate that
Goblin broke."""
from __future__ import annotations

import random
import socket
import threading
import time
from types import SimpleNamespace

import pytest

import core.chrome_companion_hub as hub_module
from chrome_companion import protocol
from chrome_companion_testkit import (FakeHost, RecordingRecorder, connect_ready, make_hub, ok_response,
                                      setup_companion, short_tmpdir, tab, wait_until)
from core.chrome_companion_hub import ChromeCompanionHub, CompanionError, _Connection

HOLD_S = 10.0


def _answer(req):
    return ok_response(req, {"tabs": [tab(1)], "total": 1})


class HookRecorder(RecordingRecorder):
    """Records like RecordingRecorder, then calls ``hook`` (outside every hub
    lock, exactly where production telemetry runs)."""

    def __init__(self):
        super().__init__()
        self.hook = None

    def record_machine_event(self, event_type, **kwargs):
        super().record_machine_event(event_type, **kwargs)
        hook = self.hook
        if hook is not None:
            hook(event_type, kwargs.get("fields", {}))


class Gate:
    def __init__(self, seam):
        self.seam = seam
        self.reached = threading.Event()
        self.release = threading.Event()


class GatedEvent(threading.Event):
    """A waiter's event that pauses at one pre-commit seam."""

    def __init__(self, gate):
        super().__init__()
        self._gate = gate
        self._set_calls = 0

    def set(self):
        self._set_calls += 1
        if self._gate.seam == "staged" and self._set_calls == 1:
            # The FIRST signal is the reader's, right after it accepted the
            # response. A cancellation's later signal passes straight through.
            self._gate.reached.set()
            self._gate.release.wait(HOLD_S)
        super().set()

    def wait(self, timeout=None):
        result = super().wait(timeout)
        if self._gate.seam == "woken":
            self._gate.reached.set()
            self._gate.release.wait(HOLD_S)
        return result


@pytest.fixture
def seams(monkeypatch):
    """arm(seam) -> Gate for the NEXT request's waiter."""
    armed = []
    real_init = hub_module._Waiter.__init__

    def init(self, op, tab_id):
        real_init(self, op, tab_id)
        if armed:
            self.event = GatedEvent(armed.pop(0))

    monkeypatch.setattr(hub_module._Waiter, "__init__", init)

    def arm(seam):
        gate = Gate(seam)
        armed.append(gate)
        return gate
    return arm


@pytest.fixture
def env(tmp_path):
    with short_tmpdir() as sdir:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        socket_path = setup_companion(data_dir, sdir)
        recorder = HookRecorder()
        hub = make_hub(data_dir, recorder)
        hub.start()
        hosts = []
        gates = []

        def connect(serve=True):
            host, welcome = connect_ready(hub, socket_path)
            send, send_lock = host.send, threading.Lock()

            def locked_send(message, *a, **k):  # serve thread + test thread share the socket
                with send_lock:
                    send(message, *a, **k)
            host.send = locked_send
            hosts.append(host)
            if serve:
                host.serve(_answer)
            return host, welcome

        yield SimpleNamespace(hub=hub, recorder=recorder, connect=connect, hosts=hosts, gates=gates,
                              socket_path=socket_path)
        recorder.hook = None
        for gate in gates:
            gate.release.set()
        hub.stop()
        for host in hosts:
            host.close()


def _request_in_background(hub, op="list_tabs", timeout_s=8):
    box = {}

    def run():
        try:
            box["result"] = hub.request(op, timeout_s=timeout_s)
        except CompanionError as exc:
            box["error"] = exc
        except Exception as exc:  # anything else is a test failure, surfaced below
            box["crash"] = exc
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


def _hold(env, seams, seam):
    """Issue a request on the current connection; its host answers; hold it
    at ``seam`` before its success can be committed."""
    gate = seams(seam)
    env.gates.append(gate)
    conn = env.hub._current
    thread, box = _request_in_background(env.hub)
    assert gate.reached.wait(5), f"request never reached the {seam!r} seam"
    return SimpleNamespace(thread=thread, box=box, gate=gate, conn=conn, seam=seam)


def _finish(held, code):
    held.gate.release.set()
    held.thread.join(5)
    assert not held.thread.is_alive(), "request hung"
    assert "crash" not in held.box, held.box.get("crash")
    assert "result" not in held.box, (
        f"{held.seam}: old connection's request returned SUCCESS after {code}")
    assert held.box["error"].code == code


# ── Interruptions ────────────────────────────────────────────────────────

def _supersede(env, held):
    env.connect()
    assert held.conn.closed and env.hub._current is not held.conn


def _pause(env, held):
    host = env.hosts[-1]
    host.send({"v": 1, "type": "bye", "connection_id": held.conn.connection_id, "reason": "paused"})
    assert wait_until(lambda: held.conn.closed)


def _host_lost_then_reconnect(env, held):
    env.hosts[-1].close()
    assert wait_until(lambda: held.conn.closed)
    env.connect()


def _hub_stop(env, held):
    env.hub.stop()
    assert held.conn.closed


INTERRUPTIONS = {
    "supersede": (_supersede, "superseded"),
    "pause": (_pause, "paused"),
    "reconnect": (_host_lost_then_reconnect, "host_closed"),
    "hub_stop": (_hub_stop, "hub_stopped"),
}
# At the "staged" seam the old connection's reader thread is itself held
# inside resolve(), so it cannot also be reading a PAUSE bye or an EOF from
# that same socket: those two interleavings are structurally impossible there
# and are exercised at the "woken" seam instead.
MATRIX = [("staged", "supersede"), ("staged", "hub_stop"),
          ("woken", "supersede"), ("woken", "pause"), ("woken", "reconnect"), ("woken", "hub_stop")]


def _recover(env, interruption):
    if interruption == "hub_stop":
        env.hub.start()
        env.connect()
    elif interruption == "pause":
        env.connect()  # owner RESUME = a brand-new connection
    assert env.hub.request("list_tabs", timeout_s=3)["ok"] is True


@pytest.mark.parametrize("seam, interruption", MATRIX)
def test_pre_commit_request_fails_when_its_connection_is_retired(env, seams, seam, interruption):
    env.connect()
    held = _hold(env, seams, seam)
    interrupt, code = INTERRUPTIONS[interruption]
    interrupt(env, held)
    _finish(held, code)
    assert held.conn._in_flight == {}
    _recover(env, interruption)


def test_goblin_f1_exact_interleaving_fails_the_old_request():
    """Goblin's reproduction from the adversarial review, as a regression:
    resolve stages the response -> hold before the completion signal ->
    install connection B -> supersede A -> release -> MUST FAIL. (Only the
    reader's signal is held, so the cancellation's signal is not delayed.)"""
    hub = ChromeCompanionHub("/nonexistent-companion-data", recorder_fn=lambda: RecordingRecorder())
    old_sock, old_peer = socket.socketpair()
    new_sock, new_peer = socket.socketpair()
    old = _Connection(old_sock, connection_id="a" * 32, seq=1, instance_fp="synthetic", extension_version="0.1")
    new = _Connection(new_sock, connection_id="b" * 32, seq=2, instance_fp="synthetic", extension_version="0.1")
    try:
        hub._current = old
        box = {}

        def caller():
            try:
                box["result"] = hub.request("ping", timeout_s=5)
            except CompanionError as exc:
                box["error"] = exc.code
        t = threading.Thread(target=caller, daemon=True)
        t.start()
        req = protocol.read_frame(old_peer.recv, protocol.MAX_TO_EXTENSION_BYTES)
        waiter = old._in_flight[req["request_id"]]
        entered, release, real_set, calls = threading.Event(), threading.Event(), waiter.event.set, []

        def held_set():
            calls.append(1)
            if len(calls) == 1:
                entered.set()
                release.wait(HOLD_S)
            real_set()
        waiter.event.set = held_set
        response = {"v": 1, "type": "response", "connection_id": old.connection_id,
                    "request_id": req["request_id"], "ok": True, "result": {"extension_version": "0.1"},
                    "tab_id": None, "observed": None, "truncated": False}
        resolver = threading.Thread(target=lambda: old.resolve(response, 128), daemon=True)
        resolver.start()
        assert entered.wait(5), "resolver did not reach the held signal"
        hub._current = new
        old.close("superseded")
        release.set()
        resolver.join(5)
        t.join(5)
        assert not t.is_alive()
        assert hub._current is new and old.closed
        assert "result" not in box, "old connection's request returned SUCCESS after supersede"
        assert box["error"] == "superseded"
    finally:
        new.close("hub_stopped")
        old.close("hub_stopped")
        old_peer.close()
        new_peer.close()


# ── Exactly one terminal outcome (unit level) ────────────────────────────

def _bare_connection():
    sock, peer = socket.socketpair()
    return _Connection(sock, connection_id="e" * 32, seq=1, instance_fp="x", extension_version="0.1"), peer


def _staged(conn, request_id="1" * 32):
    waiter = conn.register(request_id, "ping", None)
    response = {"v": 1, "type": "response", "connection_id": conn.connection_id, "request_id": request_id,
                "ok": True, "result": {}, "tab_id": None, "observed": None, "truncated": False}
    assert conn.resolve(response, 10) == "ok"
    return waiter, response


def test_retirement_cancels_a_staged_but_unclaimed_response():
    conn, peer = _bare_connection()
    try:
        waiter, _ = _staged(conn)
        assert conn.retire("superseded") == [waiter]
        with pytest.raises(CompanionError) as exc:
            conn.claim("1" * 32, waiter, answered=True, timeout_s=1)
        assert exc.value.code == "superseded" and waiter.outcome == "cancelled"
    finally:
        conn.close("hub_stopped")
        peer.close()


def test_a_claimed_success_is_final_and_retirement_cannot_revoke_it():
    conn, peer = _bare_connection()
    try:
        waiter, response = _staged(conn)
        assert conn.claim("1" * 32, waiter, answered=True, timeout_s=1) is response
        assert conn.retire("superseded") == [] and waiter.outcome == "delivered"
    finally:
        conn.close("hub_stopped")
        peer.close()


def test_claim_and_retire_are_mutually_exclusive_on_the_connection_lock():
    """The terminal transition is a critical section: while the connection
    lock is held, neither the requester's claim nor a retirement can decide
    anything; once released, exactly one of them wins, and the requester's
    view agrees with the waiter's single outcome."""
    for _ in range(25):
        conn, peer = _bare_connection()
        try:
            waiter, response = _staged(conn)
            box = {}

            def claimer():
                try:
                    box["result"] = conn.claim("1" * 32, waiter, answered=True, timeout_s=1)
                except CompanionError as exc:
                    box["error"] = exc.code

            def retirer():
                box["cancelled"] = conn.retire("superseded")
            threads = [threading.Thread(target=claimer, daemon=True), threading.Thread(target=retirer, daemon=True)]
            with conn._lock:
                for t in threads:
                    t.start()
                time.sleep(0.02)
                assert all(t.is_alive() for t in threads), "a terminal transition ran outside the connection lock"
                assert waiter.outcome is None
            for t in threads:
                t.join(5)
            if "result" in box:
                assert box["result"] is response and waiter.outcome == "delivered" and box["cancelled"] == []
            else:
                assert box["error"] == "superseded" and waiter.outcome == "cancelled"
                assert box["cancelled"] == [waiter]
        finally:
            conn.close("hub_stopped")
            peer.close()


def test_responses_after_retirement_or_claim_are_dropped():
    conn, peer = _bare_connection()
    try:
        waiter, response = _staged(conn)
        assert conn.resolve(dict(response), 10) == "unsolicited"  # already staged
        conn.claim("1" * 32, waiter, answered=True, timeout_s=1)
        assert conn.resolve(dict(response), 10) == "unsolicited"  # already delivered
        conn.retire("paused")
        assert conn.resolve(dict(response, request_id="2" * 32), 10) == "connection_closed"
    finally:
        conn.close("hub_stopped")
        peer.close()


def test_a_timed_out_waiter_never_later_becomes_a_success():
    conn, peer = _bare_connection()
    try:
        waiter = conn.register("1" * 32, "ping", None)
        with pytest.raises(CompanionError) as exc:
            conn.claim("1" * 32, waiter, answered=False, timeout_s=0.1)
        assert exc.value.code == "timeout" and waiter.outcome == "timeout"
        assert conn.retire("superseded") == []
    finally:
        conn.close("hub_stopped")
        peer.close()
    conn, peer = _bare_connection()
    try:
        waiter, _ = _staged(conn)  # answer staged just as the wait gave up
        with pytest.raises(CompanionError) as exc:
            conn.claim("1" * 32, waiter, answered=False, timeout_s=0.1)
        assert exc.value.code == "timeout" and waiter.outcome == "timeout"
    finally:
        conn.close("hub_stopped")
        peer.close()


# ── The linearization point ──────────────────────────────────────────────

def test_success_committed_before_supersede_stands(env, monkeypatch):
    """The claim IS the commit. A request that claimed its response while its
    connection was current keeps that success even if a supersede lands
    before request() returns -- exactly as if the supersede came a moment
    after the return. (Guards against 'fixing' F1 by failing everything.)"""
    env.connect()
    old_conn = env.hub._current
    after_claim, release = threading.Event(), threading.Event()
    real_apply = env.hub._apply_policy

    def held_apply(op, response):
        after_claim.set()
        release.wait(HOLD_S)
        real_apply(op, response)
    monkeypatch.setattr(env.hub, "_apply_policy", held_apply)
    thread, box = _request_in_background(env.hub)
    assert after_claim.wait(5)
    env.connect()
    assert old_conn.closed
    release.set()
    thread.join(5)
    assert box["result"]["ok"] is True


# ── Atomic retirement: nobody learns A is gone before A's waiters are dead ─

@pytest.mark.parametrize("interruption", ["supersede", "pause", "reconnect", "hub_stop"])
def test_connection_is_retired_before_anyone_can_observe_it_gone(env, seams, interruption):
    """At the moment the hub announces A's disconnect (telemetry -- by then
    B may already be current, the status says A is gone), A must already be
    retired: a requester released exactly then must fail, not claim."""
    env.connect()
    held = _hold(env, seams, "woken")
    seen = {}

    def hook(event_type, fields):
        if (event_type == "chrome_companion.connection" and fields.get("state") == "disconnected"
                and fields.get("connection_seq") == held.conn.seq and "box" not in seen):
            seen["current_is_old"] = env.hub._current is held.conn
            held.gate.release.set()
            held.thread.join(5)
            seen["box"] = dict(held.box)
    env.recorder.hook = hook
    interrupt, code = INTERRUPTIONS[interruption]
    interrupt(env, held)
    # The announcement runs outside every lock, after the retirement; since
    # R2 / N1 a replacing connection is published (and the old one retired
    # and announced) only AFTER its welcome is written, so the caller may see
    # the new connection first -- wait for the announcement, don't assume it.
    assert wait_until(lambda: "box" in seen, timeout=10), "no disconnect telemetry for the retired connection"
    env.recorder.hook = None
    assert seen["current_is_old"] is False
    assert "result" not in seen["box"], f"claimed SUCCESS while {interruption} was being announced"
    assert seen["box"]["error"].code == code


class InvariantLock:
    """Stands in for the hub lock and checks, at EVERY release (while still
    held, so the state is consistent), that no connection which has ever been
    current is both no-longer-current and still alive. Two back-to-back
    critical sections (replace, then retire) would violate this at the first
    release, however small the gap between them."""

    def __init__(self, hub):
        self._inner = threading.Lock()
        self._hub = hub
        self.seen = []
        self.violations = []

    def acquire(self, *args, **kwargs):
        return self._inner.acquire(*args, **kwargs)

    def release(self):
        current = self._hub._current
        if current is not None and all(current is not c for c in self.seen):
            self.seen.append(current)
        for conn in self.seen:
            if conn is not current and not conn.closed:
                self.violations.append((conn.seq, getattr(current, "seq", None)))
        if current is not None and current.closed:
            self.violations.append(("current_is_closed", current.seq))
        self._inner.release()

    def locked(self):
        return self._inner.locked()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


def _drive_every_retirement_path(env):
    env.connect()
    env.connect()                                    # supersede
    host, welcome = env.connect()
    host.send({"v": 1, "type": "bye", "connection_id": welcome["connection_id"], "reason": "paused"})
    assert wait_until(lambda: env.hub._current is None)
    env.connect()
    env.hosts[-1].close()                            # host lost
    assert wait_until(lambda: env.hub._current is None)
    env.connect()
    env.hub.stop()


def test_no_hub_critical_section_leaves_a_replaced_connection_alive(env):
    lock = InvariantLock(env.hub)
    env.hub._lock = lock
    _drive_every_retirement_path(env)
    assert len(lock.seen) == 5
    assert lock.violations == []


def test_connections_are_only_retired_inside_the_hub_critical_section(env, monkeypatch):
    """Structural guard for the atomic swap: every retirement the hub performs
    happens while the hub lock is held -- the same critical section that
    changes which connection is current."""
    observed = []
    real_retire = hub_module._Connection.retire

    def spy(self, reason):
        observed.append((reason, env.hub._lock.locked()))
        return real_retire(self, reason)
    monkeypatch.setattr(hub_module._Connection, "retire", spy)
    _drive_every_retirement_path(env)
    reasons = {reason for reason, _ in observed}
    assert {"superseded", "paused", "host_closed", "hub_stopped"} <= reasons, observed
    assert all(locked for _, locked in observed), observed


# ── Rapid repeated supersede ─────────────────────────────────────────────

def test_rapid_repeated_supersede_fails_every_held_request(env, seams):
    held = []
    for i in range(4):
        env.connect()
        held.append(_hold(env, seams, ("staged", "woken")[i % 2]))
    env.connect()
    for h in held:
        _finish(h, "superseded")
        assert h.conn.closed and h.conn._in_flight == {}
    assert env.hub.request("list_tabs", timeout_s=3)["ok"] is True


def test_concurrent_burst_of_connections_leaves_one_current_and_fails_old_requests(env, seams):
    env.connect()
    held = _hold(env, seams, "woken")
    burst = []

    def raw_connect():
        # No "am I current?" check: in a burst each may be superseded at once.
        host = FakeHost(env.socket_path)
        burst.append(host)
        frame = host.handshake()
        assert frame is None or frame["type"] == "welcome", frame
        if frame is not None:
            host.serve(_answer)
    threads = [threading.Thread(target=raw_connect, daemon=True) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    env.hosts.extend(burst)
    _finish(held, "superseded")
    current = env.hub._current
    assert current is not None and not current.closed
    assert env.hub.request("list_tabs", timeout_s=3)["ok"] is True


# ── Stress ───────────────────────────────────────────────────────────────

def test_seam_stress_randomised_interleavings(env, seams):
    """Many rounds of random seam x interruption x concurrency: every request
    held before commit fails with its connection's retirement reason; no
    request hangs; the next connection always works."""
    rng = random.Random(0x01A_F1)
    for round_no in range(40):
        seam, interruption = rng.choice(MATRIX)
        env.connect()
        held = [_hold(env, seams, seam)]
        if seam == "woken":  # the reader is free, so more requests can be held too
            held += [_hold(env, seams, "woken") for _ in range(rng.randint(0, 2))]
        interrupt, code = INTERRUPTIONS[interruption]
        interrupt(env, held[0])
        for h in held:
            _finish(h, code)
        _recover(env, interruption)


def test_lifecycle_soak_every_waiter_reaches_exactly_one_terminal_outcome(env, monkeypatch):
    """Unseamed soak: concurrent requesters against random supersede / PAUSE
    / host loss / hub restart. Liveness (nothing hangs, no lost wake-up
    ever surfaces as a timeout) and exactly-once: success <=> delivered;
    every waiter terminal; a cancelled waiter is never a success."""
    records = []
    records_lock = threading.Lock()
    local = threading.local()
    real_init = hub_module._Waiter.__init__

    def init(self, op, tab_id):
        real_init(self, op, tab_id)
        local.record["waiter"] = self
    monkeypatch.setattr(hub_module._Waiter, "__init__", init)

    rng = random.Random(0x50AC)
    stop = threading.Event()
    live = {}
    dropped_sessions = []

    def worker():
        while not stop.is_set():
            local.record = record = {"waiter": None}
            try:
                record["ok"] = env.hub.request("list_tabs", timeout_s=3)["ok"]
            except CompanionError as exc:
                record["error"] = exc.code
                time.sleep(0.001)  # no hot spin while Chrome is away
            with records_lock:
                records.append(record)

    def connect():
        """Like the real extension: a session whose first hub frame is not the
        welcome is a protocol violation and is dropped. Since R2 / N1 a
        request can no longer overtake the welcome, so the only drops left are
        connections that never got a welcome (e.g. mid hub restart); the soak
        asserts that no session ever saw a request first."""
        while True:
            host = FakeHost(env.socket_path)
            env.hosts.append(host)
            frame = host.handshake()
            if frame is not None and frame["type"] == "welcome":
                send, send_lock = host.send, threading.Lock()

                def locked_send(message, *a, _send=send, _lock=send_lock, **k):
                    with _lock:
                        _send(message, *a, **k)
                host.send = locked_send
                host.serve(_answer)
                live["host"], live["cid"] = host, frame["connection_id"]
                return
            dropped_sessions.append(frame and frame["type"])
            host.close()

    connect()
    workers = [threading.Thread(target=worker, daemon=True) for _ in range(6)]
    for w in workers:
        w.start()
    try:
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            time.sleep(rng.uniform(0.002, 0.012))
            action = rng.choices(["supersede", "pause", "lost", "restart"], weights=[6, 2, 2, 1])[0]
            if action == "supersede":
                connect()
            elif action == "pause":
                live["host"].send({"v": 1, "type": "bye", "connection_id": live["cid"], "reason": "paused"})
                wait_until(lambda: env.hub.current_connection_id() != live["cid"], timeout=2)
                connect()
            elif action == "lost":
                live["host"].close()
                wait_until(lambda: env.hub.current_connection_id() != live["cid"], timeout=2)
                connect()
            elif action == "restart":
                env.hub.stop()
                env.hub.start()
                connect()
    finally:
        stop.set()
        for w in workers:
            w.join(10)
    assert not any(w.is_alive() for w in workers), "a requester hung (deadlock or lost wake-up)"
    assert "request" not in dropped_sessions, "a request overtook a welcome (R2 / N1)"

    allowed = {"superseded", "paused", "host_closed", "hub_stopped", "not_connected", "hub_unavailable",
               "disconnected"}
    successes = [r for r in records if "ok" in r]
    assert successes and len(successes) < len(records), "soak did not exercise both outcomes"
    for r in records:
        waiter = r["waiter"]
        if "ok" in r:
            assert r["ok"] is True and waiter is not None and waiter.outcome == "delivered", r
            continue
        assert r["error"] in allowed, r
        if waiter is not None:
            assert waiter.outcome in {"cancelled", "abandoned"}, (r, waiter.outcome)
            if waiter.outcome == "cancelled":
                # The retirement's reason -- or, if retirement closed the socket
                # under an in-progress send, that send's "disconnected".
                assert r["error"] in {waiter.error.code, "disconnected"}, (r, waiter.error.code)
