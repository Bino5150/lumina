"""BROWSER-COMPANION-01A-R2 -- hub-side repairs from Goblin's AR2 review.

    N1  WELCOME BEFORE WORK: a connection is request-addressable only after
        its welcome is on the wire, and only if nothing newer was published
        and the hub that accepted it is still running.
    N2  REJECT STATE BEFORE THE REJECTION IS OBSERVABLE.
    P1  A REQUEST ID GETS ONE LIFE: requests are numbered per connection, in
        wire order, under the connection's send lock.
    P2  WHEN TWO URL PARSERS DISAGREE, THE PARANOID ONE WINS: the Python
        defense-in-depth layer refuses what WHATWG would split differently.

Real Unix sockets and the real _serve/_read_loop/request paths throughout;
barriers wrap only the exact seam under test."""
from __future__ import annotations

import itertools
import json
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.chrome_companion_hub as hub_module
from chrome_companion import policy, protocol, state
from chrome_companion_testkit import (CANARY, FakeHost, RecordingRecorder, connect_ready, host_hello,
                                      make_hub, observed, ok_response, require_node, setup_companion,
                                      short_tmpdir, tab, wait_until)
from core.chrome_companion_hub import CompanionError

HOLD_S = 10.0
POLICY_JS = Path(__file__).resolve().parents[1] / "chrome_companion" / "extension" / "policy.js"


@pytest.fixture
def env(tmp_path):
    with short_tmpdir() as sdir:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        socket_path = setup_companion(data_dir, sdir)
        recorder = RecordingRecorder()
        hub = make_hub(data_dir, recorder)
        hub.start()
        hosts, releases = [], []

        def connect(serve=None):
            host, welcome = connect_ready(hub, socket_path)
            hosts.append(host)
            if serve is not None:
                host.serve(serve)
            return host, welcome

        def raw_host():
            host = FakeHost(socket_path)
            hosts.append(host)
            return host

        yield SimpleNamespace(hub=hub, data_dir=data_dir, socket_path=socket_path, recorder=recorder,
                              connect=connect, raw_host=raw_host, hosts=hosts, releases=releases)
        for release in releases:
            release.set()
        hub.stop()
        for host in hosts:
            host.close()


def _ping_answer(req):
    return ok_response(req, {"extension_version": "0.1.2"})


def _in_background(fn):
    box = {}

    def run():
        try:
            box["result"] = fn()
        except CompanionError as exc:
            box["error"] = exc
        except Exception as exc:  # surfaced by the caller as a test failure
            box["crash"] = exc
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


def _hold_welcome(env, monkeypatch, *, count=1, fail=False):
    """Hold (or fail) the next ``count`` welcome writes, BEFORE they reach the
    socket. Returns (reached, release)."""
    reached, release = threading.Event(), threading.Event()
    env.releases.append(release)
    remaining = [count]
    real_send = hub_module._Connection.send

    def send(self, message):
        if message.get("type") == "welcome" and remaining[0] > 0:
            remaining[0] -= 1
            reached.set()
            release.wait(HOLD_S)
            if fail:
                raise BrokenPipeError("synthetic welcome write failure")
        return real_send(self, message)
    monkeypatch.setattr(hub_module._Connection, "send", send)
    return reached, release


class _TrackingLock:
    """Stands in for the hub lock and notes, at every release, which
    connection is current -- i.e. every connection ever published."""

    def __init__(self, inner):
        self._inner = inner
        self.hub = None
        self.published = []

    def acquire(self, *args, **kwargs):
        return self._inner.acquire(*args, **kwargs)

    def release(self):
        current = self.hub._current
        if current is not None and all(current is not c for c in self.published):
            self.published.append(current)
        self._inner.release()

    def locked(self):
        return self._inner.locked()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


def _published(env):
    lock = _TrackingLock(env.hub._lock)
    lock.hub = env.hub
    env.hub._lock = lock
    return lock.published


# ── N1: welcome before work ──────────────────────────────────────────────

def test_n1_a_request_cannot_overtake_the_welcome(env, monkeypatch):
    """Goblin's N1 barrier: the welcome write is held after authentication.
    A concurrent request must not reach the new connection first."""
    reached, release = _hold_welcome(env, monkeypatch)
    host = env.raw_host()
    host.send(host_hello())
    assert reached.wait(5)
    assert env.hub.current_connection_id() is None, "published before its welcome was written"
    thread, box = _in_background(lambda: env.hub.request("ping", timeout_s=2))
    thread.join(5)
    assert box["error"].code == "not_connected"
    release.set()
    first = host.recv()
    assert first["type"] == "welcome", first
    assert wait_until(lambda: env.hub.current_connection_id() == first["connection_id"])
    host.serve(_ping_answer)
    assert env.hub.request("ping", timeout_s=3)["ok"] is True
    assert host.requests[0]["type"] == "request"


def test_n1_previous_connection_stays_current_and_valid_until_the_new_welcome_is_sent(env, monkeypatch):
    old_host, old_welcome = env.connect(serve=_ping_answer)
    reached, release = _hold_welcome(env, monkeypatch)
    new_host = env.raw_host()
    new_host.send(host_hello())
    assert reached.wait(5)
    assert env.hub.current_connection_id() == old_welcome["connection_id"]
    assert env.hub.request("ping", timeout_s=3)["ok"] is True, "the old connection still serves meanwhile"
    release.set()
    welcome = new_host.recv()
    assert welcome["type"] == "welcome"
    assert wait_until(lambda: env.hub.current_connection_id() == welcome["connection_id"])
    assert wait_until(old_host.closed.is_set), "the old connection is retired by the publish"


def test_n1_a_failed_welcome_is_never_published(env, monkeypatch):
    seen = _published(env)
    reached, release = _hold_welcome(env, monkeypatch, fail=True)
    release.set()
    host = env.raw_host()
    host.send(host_hello())
    assert reached.wait(5)
    assert host.recv() is None, "socket closed without a welcome"
    assert wait_until(lambda: env.recorder.of("chrome_companion.connection")
                      and env.recorder.of("chrome_companion.connection")[-1].get("state") == "not_published")
    assert seen == [] and env.hub.current_connection_id() is None
    assert env.hub.status()["last_disconnect"] is None, "a never-published connection is not 'the' connection"
    assert not [e for e in env.recorder.of("chrome_companion.connection") if e.get("state") == "ready"]


def test_n1_a_newer_connection_published_during_the_welcome_wins(env, monkeypatch):
    reached, release = _hold_welcome(env, monkeypatch)
    older = env.raw_host()
    older.send(host_hello())
    assert reached.wait(5)
    newer, newer_welcome = env.connect(serve=_ping_answer)  # its welcome is not held
    release.set()
    first = older.recv()
    assert first["type"] == "welcome"
    assert older.recv() is None, "the older connection is closed, never published"
    assert env.hub.current_connection_id() == newer_welcome["connection_id"]
    assert env.hub.request("ping", timeout_s=3)["ok"] is True
    assert any(e.get("state") == "not_published" and e.get("reason") == "superseded"
               for e in env.recorder.of("chrome_companion.connection"))


def test_n1_hub_stop_during_the_welcome_publishes_nothing(env, monkeypatch):
    reached, release = _hold_welcome(env, monkeypatch)
    host = env.raw_host()
    host.send(host_hello())
    assert reached.wait(5)
    stopper, _ = _in_background(env.hub.stop)
    stopper.join(5)
    assert not stopper.is_alive(), "stop() blocked on a connection mid-welcome"
    release.set()
    host.sock.settimeout(5)
    frames = [host.recv(), host.recv()]
    assert frames[-1] is None
    assert wait_until(lambda: any(e.get("state") == "not_published" and e.get("reason") == "hub_stopped"
                                  for e in env.recorder.of("chrome_companion.connection")))
    assert env.hub._current is None and not env.hub.listening


def test_n1_chrome_disconnect_or_pause_during_the_welcome_fails_closed(env, monkeypatch):
    for how in ("disconnect", "pause_bye"):
        reached, release = _hold_welcome(env, monkeypatch)
        host = env.raw_host()
        host.send(host_hello())
        assert reached.wait(5)
        if how == "disconnect":
            host.close()
        release.set()
        if how == "pause_bye":
            welcome = host.recv()
            host.send({"v": 1, "type": "bye", "connection_id": welcome["connection_id"], "reason": "paused"})
        assert wait_until(lambda: env.hub._current is None or env.hub._current.closed, timeout=5), how
        thread, box = _in_background(lambda: env.hub.request("ping", timeout_s=1))
        thread.join(5)
        assert "result" not in box and box["error"].code in {"not_connected", "paused", "host_closed"}, (how, box)
        monkeypatch.undo()


def test_n1_stress_every_connection_sees_its_welcome_first(env):
    """Requesters hammer the hub while connections churn. On every accepted
    connection the first hub frame is the welcome -- never a request."""
    stop = threading.Event()

    def hammer():
        while not stop.is_set():
            try:
                env.hub.request("ping", timeout_s=0.3)
            except CompanionError:
                time.sleep(0.0005)
    threads = [threading.Thread(target=hammer, daemon=True) for _ in range(6)]
    for t in threads:
        t.start()
    firsts = []
    try:
        for _ in range(120):
            host = env.raw_host()
            host.send(host_hello())
            frame = host.recv()
            firsts.append(frame and frame["type"])
            if frame is not None and frame["type"] == "welcome":
                host.serve(_ping_answer)
    finally:
        stop.set()
        for t in threads:
            t.join(5)
    assert firsts and all(kind == "welcome" for kind in firsts), set(firsts)


# ── N2: reject state before the rejection is observable ──────────────────

class _SocketProxy:
    """Delegates to the accepted socket; can hold close() -- the old code's
    gap between putting the reject on the wire and recording its reason."""

    def __init__(self, sock, hold_close):
        self._sock = sock
        self._hold_close = hold_close

    def close(self):
        self._hold_close()
        self._sock.close()

    def __getattr__(self, name):
        return getattr(self._sock, name)


def _unpaired_hello(env):
    state.clear_pairing(env.data_dir)
    host = env.raw_host()
    host.send(host_hello())
    return host


def test_n2_reason_is_recorded_before_the_reject_is_sent(env, monkeypatch):
    """Barrier exactly between recording the reason and sending the reject."""
    reached, release = threading.Event(), threading.Event()
    env.releases.append(release)
    real_encode = protocol.encode_frame

    def encode(message, max_bytes):
        if message.get("type") == "reject":
            reached.set()
            release.wait(HOLD_S)
        return real_encode(message, max_bytes)
    monkeypatch.setattr(hub_module.protocol, "encode_frame", encode)
    host = _unpaired_hello(env)
    assert reached.wait(5)
    with pytest.raises(CompanionError) as exc:
        env.hub.request("ping", timeout_s=1)
    assert exc.value.code == "not_paired"
    release.set()
    assert host.recv()["reason"] == "unpaired"


def test_n2_once_the_peer_has_the_reject_the_reason_is_already_observable(env, monkeypatch):
    """Barrier at the OLD ordering's gap: the reject is on the wire, the hub
    has not finished with the socket. The pre-R2 code recorded the reason
    only after this point, so a request here reported not_connected."""
    reached, release = threading.Event(), threading.Event()
    env.releases.append(release)
    real_serve = hub_module.ChromeCompanionHub._serve

    def hold():
        reached.set()
        release.wait(HOLD_S)

    def serve(self, sock, stop_event):
        return real_serve(self, _SocketProxy(sock, hold), stop_event)
    monkeypatch.setattr(hub_module.ChromeCompanionHub, "_serve", serve)
    host = _unpaired_hello(env)
    assert host.recv()["reason"] == "unpaired"
    assert reached.wait(5)
    with pytest.raises(CompanionError) as exc:
        env.hub.request("ping", timeout_s=1)
    assert exc.value.code == "not_paired"
    release.set()


def test_n2_stress_rejection_is_never_misreported(tmp_path):
    """The historical flake (~1 in 10 in a tight loop): a FRESH hub rejects an
    unpaired profile, and the request made the moment the peer sees the
    reject must already report not_paired. (Each round needs its own hub: a
    hub that has rejected once already remembers the reason.)"""
    wrong = []
    for i in range(60):
        with short_tmpdir() as sdir:
            data_dir = tmp_path / f"d{i}"
            data_dir.mkdir()
            socket_path = setup_companion(data_dir, sdir, paired=False)
            hub = make_hub(data_dir)
            hub.start()
            host = FakeHost(socket_path)
            try:
                host.send(host_hello())
                assert host.recv()["reason"] == "unpaired"
                try:
                    hub.request("ping", timeout_s=0.5)
                except CompanionError as exc:
                    if exc.code != "not_paired":
                        wrong.append((i, exc.code))
            finally:
                hub.stop()
                host.close()
    assert wrong == [], f"{len(wrong)}/60 misreported"


# ── P1: one life per request id ──────────────────────────────────────────

def test_p1_requests_are_numbered_per_connection_in_wire_order(env):
    host, _ = env.connect(serve=_ping_answer)
    errors = []

    def many():
        for _ in range(40):
            try:
                env.hub.request("ping", timeout_s=3)
            except CompanionError as exc:
                errors.append(exc.code)
    threads = [threading.Thread(target=many, daemon=True) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert errors == []
    seqs = [protocol.request_sequence(r["request_id"]) for r in host.requests]
    assert seqs == list(range(1, 321)), "strictly increasing from 1, in wire order, no reuse"
    host2, _ = env.connect(serve=_ping_answer)
    env.hub.request("ping", timeout_s=3)
    assert protocol.request_sequence(host2.requests[0]["request_id"]) == 1, "a new connection starts afresh"


def test_p1_the_number_is_taken_under_the_send_lock(env, monkeypatch):
    """If requester A stalls right after drawing its number, requester B must
    not overtake it on the wire -- the extension would refuse A forever."""
    host, _ = env.connect(serve=_ping_answer)
    real = protocol.sequenced_request_id
    first = threading.Event()

    def slow(seq):
        if not first.is_set():
            first.set()
            time.sleep(0.4)  # B is started meanwhile
        return real(seq)
    monkeypatch.setattr(hub_module.protocol, "sequenced_request_id", slow)
    a, box_a = _in_background(lambda: env.hub.request("ping", timeout_s=3))
    assert first.wait(5)
    b, box_b = _in_background(lambda: env.hub.request("ping", timeout_s=3))
    a.join(5)
    b.join(5)
    assert box_a.get("result") and box_b.get("result"), (box_a, box_b)
    seqs = [protocol.request_sequence(r["request_id"]) for r in host.requests]
    assert seqs == sorted(seqs) == [1, 2]


def test_p1_a_request_that_cannot_be_built_takes_no_number_and_leaves_nothing_behind(env):
    host, _ = env.connect(serve=lambda req: ok_response(req, {"text": "x", "total_chars": 1, "title": "T"},
                                                          tab_id=7, observed=observed()))
    with pytest.raises(protocol.ProtocolError):
        env.hub.request("extract_text", tab_id=7, args={"max_chars": 0}, timeout_s=1)
    conn = env.hub._current
    assert conn._in_flight == {} and conn._request_seq == 0
    env.hub.request("extract_text", tab_id=7, args={}, timeout_s=3)
    assert protocol.request_sequence(host.requests[0]["request_id"]) == 1


def test_p1_request_id_format_and_bounds():
    rid = protocol.sequenced_request_id(1)
    assert protocol.is_hex_id(rid) and protocol.request_sequence(rid) == 1
    assert rid != protocol.sequenced_request_id(1), "the tail is random"
    assert protocol.request_sequence(protocol.sequenced_request_id(protocol.MAX_REQUEST_SEQ)) == protocol.MAX_REQUEST_SEQ
    for bad in (0, -1, protocol.MAX_REQUEST_SEQ + 1, True, 1.0):
        with pytest.raises(protocol.ProtocolError):
            protocol.sequenced_request_id(bad)
    req = {"v": 1, "type": "request", "connection_id": "a" * 32, "request_id": "0" * 32, "op": "ping",
           "tab_id": None, "deadline_ms": 1, "args": {}}
    with pytest.raises(protocol.ProtocolError):
        protocol.validate_request(req)  # sequence 0 is never valid
    # 12 hex digits stay inside JavaScript's exact-integer range.
    assert protocol.MAX_REQUEST_SEQ < 2 ** 53


# ── P2: parser disagreement fails closed ─────────────────────────────────

R, B = "accounts.google.com", "evil.test"
BACKSLASH_CORPUS = [
    f"https://{R}\\", f"https:\\\\{R}", f"https://example.com\\@{R}/", f"https://{R}\\evil",
    f"https://{R}\\@{B}/", f"https://{B}\\@{R}/", f"https:/\\{R}/", f"https:\\/{R}/",
    f"https://user:pw@{B}\\@{R}/", f"https://u:p@{R}\\@{B}/", f"https://{R}:443\\@{B}/", f"https://{B}:8443\\@{R}/",
    f"https://{R}\\\\\\@{B}/", f"https://{B}\\\\\\\\{R}/", f"https:\\\\{R}\\x", f"http://{R}\\@{B}:80/",
    f"HTTPS://{R.upper()}\\@{B}/", f"https://{R}.\\@{B}/", "https://example.com/a\\b", f"https://{B}/\\@{R}",
    "https://benign.test\\@example.test/", "https://example.test:8080\\x",
]
# Where WHATWG does not treat the character as a separator either: both sides agree.
AGREEING = [f"https://{R}%5C@{B}/", f"https://{B}%5C@{R}/", "https://example.com/?q=a\\b",
            "https://example.com/#a\\b", f"https://{B}?\\@{R}/", f"https://{B}#\\@{R}/"]


def _generated_corpus():
    seps = ["//", "\\\\", "/\\", "\\/", "/", "\\", "///", ""]
    mids = ["", "\\", "\\\\", "/", "%5C", "\\/", "/\\"]
    out = []
    for sep, (h1, h2), mid, port, ui in itertools.product(seps, [(R, B), (B, R)], mids,
                                                         ["", ":443", ":8443"], ["", "u:p@"]):
        out.append(f"https:{sep}{ui}{h1}{port}{mid}@{h2}/p")
        out.append(f"https:{sep}{ui}{h1}{port}{mid}{h2}/p")
    return out


def _js(urls):
    script = ("const vm=require('node:vm'),fs=require('node:fs');const ctx=vm.createContext({URL});"
              "vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),ctx);const P=ctx.LuminaPolicy;"
              "const urls=JSON.parse(fs.readFileSync(0,'utf8'));process.stdout.write(JSON.stringify(urls.map(u=>{"
              "const v=P.classifyUrl(u);let h=null;try{h=new URL(u).hostname}catch{};"
              "return [v.readable,v.reason,v.origin,h]})))")
    result = subprocess.run([require_node(), "-e", script, str(POLICY_JS)], input=json.dumps(urls),
                            capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_p2_python_refuses_every_backslash_before_query_or_fragment():
    for url in BACKSLASH_CORPUS:
        verdict = policy.classify_url(url)
        assert not verdict.readable and verdict.reason == "invalid_url", url
    for url in AGREEING:
        assert policy.classify_url(url).reason != "invalid_url" or "\\" not in url.split("?")[0], url


def test_p2_python_is_never_more_permissive_than_whatwg():
    """Differential proof over the named corpus plus every combination of
    slash/backslash spelling, userinfo, port, and host order: whenever the
    Python layer calls a URL readable, WHATWG agrees on readability AND
    origin; whenever WHATWG sees a restricted host, Python refuses."""
    corpus = list(dict.fromkeys(BACKSLASH_CORPUS + AGREEING + _generated_corpus()))
    assert len(corpus) > 1000
    for url, (js_readable, js_reason, js_origin, js_host) in zip(corpus, _js(corpus)):
        py = policy.classify_url(url)
        if py.readable:
            assert js_readable and py.origin == js_origin, (url, py, js_reason, js_origin)
        if js_reason == "restricted_host" or policy.canonical_host(js_host) in policy.RESTRICTED_HOSTS:
            assert not py.readable, url


def test_p2_residual_invalid_punycode_is_readable_in_python_but_never_a_restricted_host():
    """Documented residual (not a restricted-surface path): WHATWG rejects an
    invalid punycode label such as xn--accounts, Python's canonical-ASCII
    check does not decode IDNA. The host is still compared exactly against
    the restricted list, so no spelling like this can alias a restricted
    host; the extension never reports it, and the hub treats it as the
    ordinary third-party host it names."""
    url = "https://xn--accounts.google.com/"
    (js_readable, js_reason, _, _), = _js([url])
    assert not js_readable and js_reason == "invalid_url"
    verdict = policy.classify_url(url)
    assert verdict.origin == "https://xn--accounts.google.com"
    assert policy.canonical_host("xn--accounts.google.com") not in policy.RESTRICTED_HOSTS


@pytest.mark.parametrize("url, origin", [
    (f"https://{R}\\@{B}/", f"https://{B}"),           # Python's old split: evil.test
    (f"https://{R}:443\\@{B}/", f"https://{B}"),
    (f"https://u:p@{R}\\\\@{B}/p", f"https://{B}"),
    (f"http://{R}\\@{B}:80/", f"http://{B}"),
])
def test_p2_hub_discards_content_whose_observed_url_is_backslash_spelled(env, url, origin):
    host, _ = env.connect(serve=lambda req: ok_response(req, {"text": CANARY, "total_chars": 5, "title": "T"},
                                                          tab_id=7, observed=observed(url=url, origin=origin)))
    with pytest.raises(CompanionError) as exc:
        env.hub.request("extract_text", tab_id=7, args={}, timeout_s=3)
    assert exc.value.code == "restricted_surface"
    assert CANARY not in exc.value.message


def test_p2_hub_withholds_url_and_title_of_backslash_spelled_tabs(env):
    tabs = [tab(1), tab(2, url=f"https://{R}\\@{B}/", title="Sign in"), tab(3, url=f"https:\\\\{R}/", title="x")]
    env.connect(serve=lambda req: ok_response(req, {"tabs": tabs, "total": 3}))
    by_id = {t["tab_id"]: t for t in env.hub.request("list_tabs", timeout_s=3)["result"]["tabs"]}
    for tab_id in (2, 3):
        assert by_id[tab_id]["restricted"] is True and by_id[tab_id]["url"] is None
        assert by_id[tab_id]["title"] is None and by_id[tab_id]["site_access"] == "restricted"
    assert by_id[1]["url"].startswith("https://www.reddit.com/")
