"""BROWSER-COMPANION-01A -- end-to-end: the REAL extension worker (Node VM)
spawns the REAL native host (Python subprocess, Chrome framing on stdio),
which connects to the REAL hub on a private Unix socket. Covers the full
read path plus pause/resume, Chrome restart, Lumina restart, and pairing --
the offline stand-in for the owner-supervised live acceptance.

Must block in CI (see chrome_companion_testkit.require_node)."""
from __future__ import annotations

import json
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from chrome_companion import native_host, state
from chrome_companion_testkit import (CANARY, EXT_ID, INSTANCE, ORIGIN, RecordingRecorder, make_hub,
                                      require_node, setup_companion, short_tmpdir, wait_until)
from core.chrome_companion_hub import CompanionError

DRIVER = Path(__file__).resolve().parent / "chrome_companion_js" / "e2e_driver.js"
REDDIT = "https://www.reddit.com/r/AgentsInteractive/"
TABS = [
    {"id": 7, "windowId": 1, "active": True, "incognito": False, "status": "complete", "url": REDDIT,
     "title": "AgentsInteractive"},
    {"id": 8, "windowId": 1, "active": False, "incognito": False, "status": "complete",
     "url": "chrome://settings/passwords", "title": "Settings - Passwords"},
    {"id": 9, "windowId": 2, "active": False, "incognito": True, "status": "complete",
     "url": "https://private.test/", "title": "Private"},
    {"id": 10, "windowId": 1, "active": False, "incognito": False, "status": "complete",
     "url": "https://github.com/", "title": "GitHub"},
]
PAGES = {
    "7": {"href": REDDIT, "title": "AgentsInteractive",
          "text": f"Welcome to AgentsInteractive\n\n{CANARY}",
          "anchors": [{"href": "/r/AgentsInteractive/comments/abc", "text": "A post"},
                      {"href": "javascript:void(0)", "text": "js"}]},
    "10": {"href": "https://github.com/", "title": "GitHub", "text": "GitHub", "anchors": []},
}


class Chrome:
    """One 'Chrome session' = one driver process running the worker."""

    def __init__(self, cfg):
        self.proc = subprocess.Popen([require_node(), str(DRIVER), json.dumps(cfg)], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)

    def command(self, **cmd):
        self.send(**cmd)
        if cmd["cmd"] == "exit":
            return None
        return self.read()

    def send(self, **cmd):
        self.proc.stdin.write(json.dumps(cmd) + "\n")
        self.proc.stdin.flush()

    def read(self):
        return json.loads(self.proc.stdout.readline())

    def crash(self):
        """Chrome dies abruptly: the worker's native port pipes close with it."""
        self.proc.send_signal(signal.SIGKILL)
        self.proc.wait(5)

    def close(self):
        if self.proc.poll() is None:
            try:
                self.command(cmd="exit")
                self.proc.wait(5)
            except (OSError, subprocess.TimeoutExpired, BrokenPipeError):
                self.proc.kill()


@pytest.fixture
def world(tmp_path):
    with short_tmpdir() as sdir:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        socket_path = setup_companion(data_dir, sdir)
        host_config = tmp_path / "host" / "host_config.json"
        state.write_private_json(host_config, {"version": 1, "extension_origin": ORIGIN,
                                               "socket_path": socket_path})
        storage_file = tmp_path / "chrome_storage.json"
        storage_file.write_text(json.dumps({"companionInstanceId": INSTANCE}))
        cfg = {"python": sys.executable, "hostScript": native_host.__file__, "hostConfig": str(host_config),
               "extensionId": EXT_ID, "storageFile": str(storage_file), "tabs": TABS, "pages": PAGES,
               "granted": [f"{REDDIT.split('/r/')[0]}/*"]}
        recorder = RecordingRecorder()
        hub = make_hub(data_dir, recorder)
        hub.start()
        chromes = []

        def launch(**overrides):
            chrome = Chrome(dict(cfg, **overrides))
            chromes.append(chrome)
            return chrome

        class World:
            pass

        w = World()
        w.hub, w.launch, w.data_dir, w.recorder = hub, launch, data_dir, recorder
        yield w
        for chrome in chromes:
            chrome.close()
        hub.stop()


def _ready(hub, previous=None, timeout=12.0):
    assert wait_until(lambda: hub.current_connection_id() not in (None, previous), timeout=timeout), \
        hub.status()
    return hub.current_connection_id()


def test_full_read_path_through_real_worker_host_and_hub(world):
    chrome = world.launch()
    cid = _ready(world.hub)
    assert chrome.command(cmd="status")["state"] == "READY"

    tabs = world.hub.request("list_tabs", timeout_s=5)["result"]["tabs"]
    by_id = {t["tab_id"]: t for t in tabs}
    assert set(by_id) == {7, 8, 10}
    assert by_id[7]["url"] == REDDIT and by_id[7]["site_access"] == "granted"
    assert by_id[8]["restricted"] and by_id[8]["url"] is None
    assert by_id[10]["site_access"] == "not_granted"

    active = world.hub.request("get_active_tab", timeout_s=5)["result"]
    assert active["tab_id"] == 7 and active["title"] == "AgentsInteractive"

    text = world.hub.request("extract_text", tab_id=7, args={"max_chars": 2000}, timeout_s=5)
    assert CANARY in text["result"]["text"]
    assert text["observed"] == {"url": REDDIT, "origin": "https://www.reddit.com", "document_id": "doc-7"}

    links = world.hub.request("get_links", tab_id=7, args={}, timeout_s=5)["result"]["links"]
    assert links == [{"text": "A post", "href": "https://www.reddit.com/r/AgentsInteractive/comments/abc",
                      "same_origin": True}]

    with pytest.raises(CompanionError) as exc:
        world.hub.request("extract_text", tab_id=10, args={}, timeout_s=5)
    assert exc.value.code == "site_access_required"
    chrome.command(cmd="grant", pattern="https://github.com/*")
    assert world.hub.request("extract_text", tab_id=10, args={}, timeout_s=5)["result"]["text"] == "GitHub"

    for restricted in (8, 9):
        with pytest.raises(CompanionError) as exc:
            world.hub.request("extract_text", tab_id=restricted, args={}, timeout_s=5)
        assert exc.value.code in {"restricted_surface", "tab_not_found"}
    assert world.hub.current_connection_id() == cid
    assert CANARY not in repr(world.recorder.events)


def test_owner_granted_navigation_through_worker_host_and_hub(world):
    chrome = world.launch()
    _ready(world.hub)
    url = "https://github.com/Bino5150/lumina"
    with pytest.raises(CompanionError) as denied:
        world.hub.request("open_owner_url", args={"url": url}, timeout_s=5)
    assert denied.value.code == "navigation_not_allowed" and denied.value.executed is False
    assert chrome.command(cmd="navigation", allowed=True)["result"] == {
        "ok": True, "navigation_allowed": True}
    opened = world.hub.request("open_owner_url", args={"url": url}, timeout_s=5)["result"]
    assert opened["status"] == "browser_local_effect_observed"
    assert opened["load_confirmed"] is True and opened["observed_url"] == url
    assert opened["tab_id"] > 10 and opened["window_id"] == 1
    selected = world.hub.request("switch_tab", tab_id=7,
                                 args={"window_id": 1, "expected_url": REDDIT}, timeout_s=5)["result"]
    assert selected["status"] == "browser_local_effect_observed" and selected["tab_id"] == 7
    grant = "https://github.com/*"
    assert chrome.command(cmd="popup_revoke", pattern=grant)["result"]["invalidated"] is True
    hidden = {t["tab_id"]: t for t in world.hub.request("list_tabs", timeout_s=5)["result"]["tabs"]}
    assert hidden[opened["tab_id"]]["url"] is None and hidden[opened["tab_id"]]["title"] is None
    with pytest.raises(CompanionError) as revoked:
        world.hub.request("open_owner_url", args={"url": url}, timeout_s=5)
    assert revoked.value.code == "companion_revoked" and revoked.value.executed is False


def test_pause_and_resume_through_the_real_chain(world):
    chrome = world.launch()
    cid = _ready(world.hub)
    assert chrome.command(cmd="pause")["result"] == {"ok": True, "paused": True}
    assert wait_until(lambda: world.hub.current_connection_id() is None)
    assert wait_until(lambda: (world.hub.status()["last_disconnect"] or {}).get("reason") == "paused")
    with pytest.raises(CompanionError) as exc:
        world.hub.request("list_tabs", timeout_s=2)
    assert exc.value.code == "paused"
    chrome.command(cmd="alarm")
    assert not wait_until(lambda: world.hub.current_connection_id() is not None, timeout=1.5), \
        "paused worker must not reconnect"

    assert chrome.command(cmd="resume")["result"] == {"ok": True, "paused": False}
    new_cid = _ready(world.hub, previous=cid)
    assert new_cid != cid
    assert world.hub.request("list_tabs", timeout_s=5)["ok"] is True


def test_chrome_restart_reconnects_fresh_and_pause_survives_it(world):
    chrome = world.launch()
    cid = _ready(world.hub)
    chrome.crash()
    assert wait_until(lambda: world.hub.current_connection_id() is None, timeout=8)
    with pytest.raises(CompanionError):
        world.hub.request("list_tabs", timeout_s=2)

    chrome2 = world.launch()  # Chrome comes back with the same profile storage
    cid2 = _ready(world.hub, previous=cid)
    assert cid2 != cid
    assert world.hub.request("extract_text", tab_id=7, args={}, timeout_s=5)["ok"] is True

    chrome2.command(cmd="pause")
    assert wait_until(lambda: world.hub.current_connection_id() is None)
    chrome2.crash()
    chrome3 = world.launch()
    assert not wait_until(lambda: world.hub.current_connection_id() is not None, timeout=2.5), \
        "PAUSE must survive a Chrome restart"
    assert chrome3.command(cmd="status")["state"] == "PAUSED"


def test_lumina_restart_reconnects_with_fresh_state(world):
    world.launch()
    cid = _ready(world.hub)
    world.hub.stop()  # Lumina quits: host exits, worker sees the port close
    with pytest.raises(CompanionError) as exc:
        world.hub.request("list_tabs", timeout_s=2)
    assert exc.value.code == "hub_unavailable"
    world.hub.start()  # Lumina starts again
    cid2 = _ready(world.hub, previous=cid, timeout=15)
    assert cid2 != cid
    assert world.hub.request("get_links", tab_id=7, args={}, timeout_s=5)["ok"] is True


def test_unpaired_profile_is_refused_until_the_owner_pairs_it(world):
    state.clear_pairing(world.data_dir)
    chrome = world.launch()
    assert wait_until(lambda: chrome.command(cmd="status").get("lastError") == "unpaired", timeout=10)
    assert world.hub.current_connection_id() is None
    with pytest.raises(CompanionError) as exc:
        world.hub.request("list_tabs", timeout_s=2)
    assert exc.value.code == "not_paired"
    state.save_pairing(world.data_dir, instance_id=INSTANCE, extension_origin=ORIGIN)
    chrome.command(cmd="alarm")  # the worker's periodic reconnect alarm
    _ready(world.hub)
    assert world.hub.request("list_tabs", timeout_s=5)["ok"] is True


# ── BROWSER-COMPANION-01A-R2 through the real chain ──────────────────────

def test_r2_overlapping_pause_resume_pause_ends_paused_with_nothing_running(world):
    """B1: three owner commands in flight at once (the driver runs each
    popup message concurrently, like two popup windows). The last one --
    PAUSE -- is what persists, and nothing can run afterwards."""
    chrome = world.launch()
    cid = _ready(world.hub)
    for cmd in ("pause", "resume", "pause"):
        chrome.send(cmd=cmd)
    acks = [chrome.read() for _ in range(3)]
    assert all(a["event"] == "ack" and a["result"]["ok"] for a in acks), acks
    assert chrome.command(cmd="status")["state"] == "PAUSED"
    assert wait_until(lambda: world.hub.current_connection_id() is None)
    chrome.command(cmd="alarm")
    assert not wait_until(lambda: world.hub.current_connection_id() is not None, timeout=1.5)
    with pytest.raises(CompanionError) as exc:
        world.hub.request("extract_text", tab_id=7, args={}, timeout_s=2)
    assert exc.value.code in {"paused", "not_connected"}
    assert chrome.command(cmd="resume")["result"] == {"ok": True, "paused": False}
    assert _ready(world.hub, previous=cid) != cid
    assert CANARY in world.hub.request("extract_text", tab_id=7, args={}, timeout_s=5)["result"]["text"]


def test_r2_revoking_site_access_while_a_read_is_pending_returns_nothing(world):
    """B2: the page is held mid-read (a busy main thread), the owner revokes
    the site, the page is released: explicit permission failure, no content.
    A deliberate re-grant then allows a NEW read."""
    chrome = world.launch()
    _ready(world.hub)
    assert chrome.command(cmd="hold", tab=7)["event"] == "ack"
    box = {}

    def read():
        try:
            box["result"] = world.hub.request("extract_text", tab_id=7, args={}, timeout_s=10)
        except CompanionError as exc:
            box["error"] = exc
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    assert chrome.read() == {"event": "held", "tab": 7}
    assert chrome.command(cmd="revoke", pattern=f"{REDDIT.split('/r/')[0]}/*")["event"] == "ack"
    assert chrome.command(cmd="release")["event"] == "ack"
    reader.join(10)
    assert "result" not in box, "page content crossed after the grant was withdrawn"
    assert box["error"].code == "site_access_required"
    assert CANARY not in box["error"].message and CANARY not in repr(world.recorder.events)
    chrome.command(cmd="grant", pattern=f"{REDDIT.split('/r/')[0]}/*")
    assert CANARY in world.hub.request("extract_text", tab_id=7, args={}, timeout_s=5)["result"]["text"]


def test_r3_popup_revoke_then_regrant_with_chromes_event_still_held_returns_nothing(world):
    """R3 / B2-R3 through the real worker, host and hub: a read is held in the
    page; the owner revokes from the POPUP and re-grants before it ends; and
    Chrome's onRemoved is held back until after the answer (as when Chrome's
    network service is slow -- the event waits on it, contains() does not).
    Nothing crosses; the error and every telemetry record are content-free.
    The re-grant alone does not restore reads (R5.1); after the popup's
    Allow, a new read works."""
    chrome = world.launch()
    _ready(world.hub)
    grant = f"{REDDIT.split('/r/')[0]}/*"
    assert chrome.command(cmd="hold_events")["event"] == "ack"
    assert chrome.command(cmd="hold", tab=7)["event"] == "ack"
    box = {}

    def read():
        try:
            box["result"] = world.hub.request("extract_text", tab_id=7, args={}, timeout_s=10)
        except CompanionError as exc:
            box["error"] = exc
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    assert chrome.read() == {"event": "held", "tab": 7}
    assert chrome.command(cmd="popup_revoke", pattern=grant)["result"] == {
        "ok": True, "invalidated": True, "blocked": True, "chrome_access": False, "remove_result": True}
    chrome.command(cmd="grant", pattern=grant)  # re-granted before the read ends
    assert chrome.command(cmd="release")["event"] == "ack"
    reader.join(10)
    assert "result" not in box, "page content crossed after the owner's popup revoke"
    assert box["error"].code == "site_access_required"
    assert CANARY not in box["error"].message and CANARY not in repr(world.recorder.events)
    assert chrome.command(cmd="release_events")["released"] == 1, "Chrome's onRemoved was still held"
    # R5.1: Chrome granting it again is not the owner allowing it; the popup's Allow is.
    with pytest.raises(CompanionError) as exc:
        world.hub.request("extract_text", tab_id=7, args={}, timeout_s=5)
    assert exc.value.code == "site_access_required"
    assert chrome.command(cmd="popup_allow", pattern=grant)["result"] == {"ok": True}
    assert CANARY in world.hub.request("extract_text", tab_id=7, args={}, timeout_s=5)["result"]["text"]


def test_r5_popup_revoke_chrome_refuses_the_site_stays_blocked_through_restart_until_the_owner_allows(world):
    """R5 / AR5 through the real worker, host and hub: Chrome resolves
    permissions.remove() false and keeps its grant. No success answer; every
    later read is refused, content-free; a grant Chrome holds -- or grants
    again -- is not authorization; the block survives Chrome restarting (the
    extension's own storage); only the popup's Allow lifts it."""
    chrome = world.launch()
    cid = _ready(world.hub)
    grant = f"{REDDIT.split('/r/')[0]}/*"
    assert chrome.command(cmd="chrome_refuses_removal", value=True)["event"] == "ack"
    assert chrome.command(cmd="popup_revoke", pattern=grant)["result"] == {
        "ok": False, "invalidated": True, "blocked": True, "chrome_access": True, "remove_result": False,
        "error": "site_access_remove_failed"}

    def refused():
        with pytest.raises(CompanionError) as exc:
            world.hub.request("extract_text", tab_id=7, args={}, timeout_s=5)
        assert exc.value.code == "site_access_required"
        assert CANARY not in exc.value.message
    refused()
    tab = {t["tab_id"]: t for t in world.hub.request("list_tabs", timeout_s=5)["result"]["tabs"]}[7]
    assert tab["site_access"] == "restricted"
    assert tab["restriction"] == "companion_revoked"
    assert tab["url"] is None and tab["title"] is None
    chrome.command(cmd="grant", pattern=grant)  # Chrome (its settings) grants it: not the owner's Allow
    refused()
    chrome.crash()
    assert wait_until(lambda: world.hub.current_connection_id() is None, timeout=8)
    chrome2 = world.launch()  # same profile storage; Chrome still grants the site
    _ready(world.hub, previous=cid)
    refused()
    assert chrome2.command(cmd="popup_allow", pattern=grant)["result"] == {"ok": True}
    assert CANARY in world.hub.request("extract_text", tab_id=7, args={}, timeout_s=5)["result"]["text"]
    assert CANARY not in repr(world.recorder.events)


def test_r2_welcome_precedes_every_request_so_restarts_cause_no_churn(world):
    """N1: requesters hammer the hub while Chrome starts and restarts. Each
    launch yields exactly ONE ready connection: a request never overtakes a
    welcome, so the worker never drops a session for a pre-READY request."""
    stop = threading.Event()

    def hammer():
        while not stop.is_set():
            try:
                world.hub.request("ping", timeout_s=1)
            except CompanionError:
                time.sleep(0.001)
    threads = [threading.Thread(target=hammer, daemon=True) for _ in range(4)]
    for t in threads:
        t.start()
    try:
        cid = None
        for _ in range(3):
            chrome = world.launch()
            cid = _ready(world.hub, previous=cid)
            time.sleep(0.3)  # let any churn show itself
            assert world.hub.current_connection_id() == cid
            chrome.crash()
            assert wait_until(lambda: world.hub.current_connection_id() is None, timeout=8)
    finally:
        stop.set()
        for t in threads:
            t.join(5)
    ready = [e for e in world.recorder.of("chrome_companion.connection") if e.get("state") == "ready"]
    assert len(ready) == 3, f"{len(ready)} ready connections for 3 launches (churn)"


def test_r2_every_new_failure_path_is_content_free(world, tmp_path, caplog):
    """Canaries in a page's URL, title, text and links. A successful read,
    a read voided by revocation, and a read cut off by PAUSE: the content
    reaches the caller only on success -- and never the flight recorder,
    Python logging, the native host's log, or the worker's console."""
    url = f"{REDDIT}?q=CANARY-URL-9d1"
    tabs = [dict(TABS[0], url=url, title="CANARY-TITLE-9d1")]
    pages = {"7": {"href": url, "title": "CANARY-TITLE-9d1", "text": f"CANARY-TEXT-9d1 {CANARY}",
                   "anchors": [{"href": "https://canary.test/CANARY-LINK-9d1", "text": "CANARY-LINKTEXT-9d1"}]}}
    host_log = tmp_path / "host.log"
    caplog.set_level("DEBUG")
    chrome = world.launch(tabs=tabs, pages=pages, hostLog=str(host_log))
    _ready(world.hub)
    grant = f"{REDDIT.split('/r/')[0]}/*"
    ok = world.hub.request("extract_text", tab_id=7, args={}, timeout_s=5)
    assert "CANARY-TEXT-9d1" in ok["result"]["text"]
    assert world.hub.request("get_links", tab_id=7, args={}, timeout_s=5)["result"]["links"]

    def held_read(action):
        assert chrome.command(cmd="hold", tab=7)["event"] == "ack"
        box = {}

        def read():
            try:
                box["result"] = world.hub.request("extract_text", tab_id=7, args={}, timeout_s=6)
            except CompanionError as exc:
                box["error"] = exc
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        assert chrome.read() == {"event": "held", "tab": 7}
        action()
        assert chrome.command(cmd="release")["event"] == "ack"
        reader.join(8)
        assert "result" not in box
        return box["error"]

    revoked = held_read(lambda: chrome.command(cmd="revoke", pattern=grant))
    assert revoked.code == "site_access_required"
    chrome.command(cmd="grant", pattern=grant)
    paused = held_read(lambda: chrome.command(cmd="pause"))
    assert paused.code in {"paused", "host_closed"}
    status = chrome.command(cmd="status")
    chrome.close()
    console = chrome.proc.stderr.read()
    haystacks = {"flight recorder": repr(world.recorder.events), "python logging": caplog.text,
                 "host log": host_log.read_text(errors="replace") if host_log.exists() else "",
                 "worker console": console, "worker status": json.dumps(status),
                 "errors": revoked.message + paused.message}
    assert "[chrome-companion-host]" in haystacks["host log"], "the host log was captured (not vacuous)"
    assert "chrome_companion.request" in haystacks["flight recorder"]
    assert {"site_access_required", "ok"} <= {f.get("result") for f in world.recorder.of("chrome_companion.request")}
    for where, text in haystacks.items():
        assert "CANARY" not in text and "9d1" not in text, where
