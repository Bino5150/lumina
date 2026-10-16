"""BROWSER-COMPANION-HARDENING-01: truthful ambiguous receipts at the hub.

Live evidence (2026-10-15, Lumina's flight recorder): a real follow_link whose
page loaded slower than the extension's confirmation window came back from the
worker as an honest `ambiguous_after_dispatch` receipt with `tab_id: null` --
and the hub relabelled it `tab_mismatch` ("Chrome answered for a different
tab"), which it was not. The live installation is the PINNED v0.3.0 extension,
which will keep sending that shape, so the hub itself must carry an ambiguous
receipt through unchanged, while still refusing any answer that names a
DIFFERENT tab, and any claimed success it cannot tie to the requested tab.

Worker-side halves (bounded confirmation, ambiguous receipts naming their tab)
are in tests/chrome_companion_js/worker_test.js ("HARDEN ..."); the readiness
race is in tests/test_chrome_companion_e2e.py.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from chrome_companion_testkit import connect_ready, make_hub, ok_response, setup_companion, short_tmpdir
from core.chrome_companion_hub import CompanionError
from tools.chrome_companion import register_chrome_companion_tools
from tools.registry import ToolRegistry

DOC = "doc-7"
SOURCE = "https://www.reddit.com/r/AgentsInteractive/"
HREF = "https://www.reddit.com/r/AgentsInteractive/comments/abc"


@pytest.fixture
def hub_at(tmp_path):
    made = []

    def make(version):
        cm = short_tmpdir()
        sdir = cm.__enter__()
        data_dir = tmp_path / f"data-{len(made)}"
        data_dir.mkdir()
        socket_path = setup_companion(data_dir, sdir)
        hub = make_hub(data_dir)
        hub.start()
        host, _ = connect_ready(hub, socket_path, extension_version=version)
        made.append((hub, host, cm))
        return SimpleNamespace(hub=hub, host=host, data_dir=data_dir)

    yield make
    for hub, host, cm in made:
        hub.stop()
        host.close()
        cm.__exit__(None, None, None)


def receipt(req, status, *, tab_id=None, window_id=None):
    return {"operation_id": f'{req["connection_id"]}:{req["request_id"]}', "status": status,
            "tab_id": tab_id, "window_id": window_id, "observed_url": None, "load_confirmed": False}


def links_answer(req):
    return ok_response(req, {"links": [{"text": "A post", "href": HREF, "same_origin": True}], "total_links": 1},
                       tab_id=7, observed={"url": SOURCE, "origin": "https://www.reddit.com", "document_id": DOC})


def answering(action_answer):
    def handler(req):
        if req["op"] == "get_links":
            return links_answer(req)
        return action_answer(req)
    return handler


FOLLOW = {"window_id": 1, "document_id": DOC, "text": "A post", "href": HREF}


@pytest.mark.parametrize("version", ["0.3.0", "0.4.0", "0.4.1"])
def test_ambiguous_follow_link_without_a_tab_is_carried_through_not_relabelled(hub_at, version):
    side = hub_at(version)
    side.host.serve(answering(lambda req: ok_response(req, receipt(req, "ambiguous_after_dispatch"), tab_id=None)))
    side.hub.request("get_links", tab_id=7, args={}, timeout_s=3)
    response = side.hub.request("follow_link", tab_id=7, args=FOLLOW, timeout_s=3)
    assert response["result"]["status"] == "ambiguous_after_dispatch"
    assert response["result"]["observed_url"] is None


def test_ambiguous_switch_tab_without_a_tab_is_carried_through(hub_at):
    side = hub_at("0.3.0")
    side.host.serve(lambda req: ok_response(req, receipt(req, "ambiguous_after_dispatch"), tab_id=None))
    response = side.hub.request("switch_tab", tab_id=7, args={"window_id": 1, "expected_url": SOURCE}, timeout_s=3)
    assert response["result"]["status"] == "ambiguous_after_dispatch"


def test_ambiguous_navigate_without_a_tab_is_carried_through(hub_at):
    side = hub_at("0.4.1")
    side.host.serve(lambda req: ok_response(req, receipt(req, "ambiguous_after_dispatch"), tab_id=None))
    response = side.hub.request("navigate", tab_id=7, args={"kind": "back", "window_id": 1, "document_id": DOC},
                                timeout_s=3)
    assert response["result"]["status"] == "ambiguous_after_dispatch"


@pytest.mark.parametrize("status", ["ambiguous_after_dispatch", "browser_local_effect_observed"])
def test_an_answer_naming_a_DIFFERENT_tab_is_still_tab_mismatch(hub_at, status):
    side = hub_at("0.3.0")
    side.host.serve(answering(lambda req: ok_response(req, receipt(req, status, tab_id=9, window_id=1), tab_id=9)))
    side.hub.request("get_links", tab_id=7, args={}, timeout_s=3)
    with pytest.raises(CompanionError) as mismatch:
        side.hub.request("follow_link", tab_id=7, args=FOLLOW, timeout_s=3)
    assert mismatch.value.code == "tab_mismatch"
    assert mismatch.value.executed is True, "it may have reached Chrome: ambiguous, never retried"


def test_a_claimed_success_without_a_tab_is_still_refused(hub_at):
    side = hub_at("0.3.0")
    side.host.serve(lambda req: ok_response(req, dict(receipt(req, "browser_local_effect_observed"),
                                                      observed_url=SOURCE, load_confirmed=True), tab_id=None))
    with pytest.raises(CompanionError) as refused:
        side.hub.request("switch_tab", tab_id=7, args={"window_id": 1, "expected_url": SOURCE}, timeout_s=3)
    assert refused.value.code == "tab_mismatch"


def test_a_claimed_follow_link_success_without_a_tab_is_still_refused(hub_at):
    """follow_link's success policy checks the document, not the tab -- so the
    tab check is the only thing standing between a tab-less 'success' and a
    verified receipt for a navigation that cannot be tied to the tab asked for."""
    side = hub_at("0.3.0")
    landed = "https://www.reddit.com/r/AgentsInteractive/comments/abc"

    def success_without_tab(req):
        return ok_response(req, dict(receipt(req, "browser_local_effect_observed", tab_id=7, window_id=1),
                                     observed_url=landed, load_confirmed=True), tab_id=None,
                           observed={"url": landed, "origin": "https://www.reddit.com", "document_id": "doc-new"})
    side.host.serve(answering(success_without_tab))
    side.hub.request("get_links", tab_id=7, args={}, timeout_s=3)
    with pytest.raises(CompanionError) as refused:
        side.hub.request("follow_link", tab_id=7, args=FOLLOW, timeout_s=3)
    assert refused.value.code == "tab_mismatch"


def test_reads_keep_their_strict_tab_check(hub_at):
    side = hub_at("0.3.0")
    side.host.serve(lambda req: ok_response(req, {"text": "x", "total_chars": 1, "title": "t"}, tab_id=None,
                                            observed={"url": SOURCE, "origin": "https://www.reddit.com",
                                                      "document_id": DOC}))
    with pytest.raises(CompanionError) as mismatch:
        side.hub.request("extract_text", tab_id=7, args={}, timeout_s=3)
    assert mismatch.value.code == "tab_mismatch"


def test_chrome_follow_link_reports_the_honest_ambiguity_to_lumina(hub_at):
    """The live specimen, end to end through the tool: no 'tab_mismatch'."""
    side = hub_at("0.3.0")
    side.host.serve(answering(lambda req: ok_response(req, receipt(req, "ambiguous_after_dispatch"), tab_id=None)))
    registry = ToolRegistry()
    from chrome_companion import state
    assert state.load_install(side.data_dir) is not None
    register_chrome_companion_tools(registry, data_dir=side.data_dir, hub=side.hub, agent=object())
    json.loads(registry.call("chrome_get_links", {"tab_id": 7}))
    out = json.loads(registry.call("chrome_follow_link", dict(FOLLOW, tab_id=7)))
    assert out["ok"] is False
    assert out["status"] == "ambiguous_after_dispatch"
    assert "failure_class" not in out, out
    assert "tab_mismatch" not in json.dumps(out)
