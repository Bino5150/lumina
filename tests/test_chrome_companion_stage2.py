"""BROWSER COMPANION STAGE 2 (FINGER) -- the model-facing and protocol seam.

Stage 2 gives Lumina one bounded capability: inside an origin the owner has
already authorized, open ONE link she has already read on that exact
document. This file covers the Python half:

  * the protocol accepts a follow_link claim and refuses everything else;
  * the tool never turns a claim into a destination of its own;
  * the receipt distinguishes attempted / refused / dispatched / verified;
  * the operation vocabulary cannot drift between the three implementations.

The load-bearing boundary -- that the destination is read from the live
document and never taken from the model -- is enforced in the extension and
proven end-to-end in tests/chrome_companion_js/worker_test.js. Nothing here
is trusted to carry it on its own.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import tools.chrome_companion as chrome_tools
from chrome_companion import protocol, state
from chrome_companion_testkit import EXT_ID
from core.chrome_companion_hub import ACTION_OPS, CompanionError
from core.agent import _chrome_action_receipt_success, _tool_call_fields
from core.headless import _log_tool_call
from core.tool_profiles import OWNER_ONLY_TOOLS, TOOL_TIERS, resolve_enabled_set
from tools.registry import ToolRegistry

CID = "c" * 32
RID = "000000000001" + "a" * 20  # first 12 hex digits are the hub's per-connection sequence
WORKER = Path(__file__).resolve().parents[1] / "chrome_companion" / "extension" / "worker.js"
POPUP = WORKER.with_name("popup.html")
HREF = "https://www.reddit.com/r/AgentsInteractive/comments/1wmuwbo/post/"
DOC = "doc-finger-1"
TEXT = "A stranger credited me with my work"


def claim(**overrides):
    args = {"window_id": 1, "document_id": DOC, "text": TEXT, "href": HREF}
    args.update(overrides)
    return args


def request_message(**overrides):
    message = {"v": protocol.PROTOCOL_VERSION, "type": "request", "connection_id": CID,
               "request_id": RID, "op": "follow_link", "tab_id": 7,
               "deadline_ms": 4102444800000, "args": claim()}
    message.update(overrides)
    return message


class FakeHub:
    """Stands in at the tool boundary with already-validated responses, and
    records every call so a test can prove the hub was never reached."""

    def __init__(self, error=None, observed_document=None, status="browser_local_effect_observed"):
        self.calls = []
        self.error = error
        self.observed_document = observed_document
        self.status = status

    def request(self, op, *, tab_id=None, args=None, timeout_s=10.0):
        self.calls.append({"op": op, "tab_id": tab_id, "args": args})
        if self.error is not None:
            raise self.error
        return {"ok": True, "tab_id": tab_id, "observed": None, "truncated": False,
                "result": {"operation_id": f"{CID}:{RID}", "status": self.status,
                           "tab_id": tab_id, "window_id": 1,
                           "observed_url": HREF if self.status ==
                           "browser_local_effect_observed" else None,
                           "load_confirmed": self.status == "browser_local_effect_observed"}}

    def verified_hub(self, document_id="doc-finger-2"):
        hub = FakeHub()
        original = hub.request

        def request(op, *, tab_id=None, args=None, timeout_s=10.0):
            response = original(op, tab_id=tab_id, args=args, timeout_s=timeout_s)
            response["observed"] = {"url": HREF, "origin": "https://www.reddit.com",
                                    "document_id": document_id}
            return response

        hub.request = request
        return hub

    def status(self):
        return {"installed": True, "paired_instance": "x", "hub": "listening", "hub_error": None,
                "state_error": None, "connection": None, "last_disconnect": None,
                "last_rejected_connection": None}


@pytest.fixture
def installed(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    state.save_install(data_dir, extension_id=EXT_ID, socket_path=str(tmp_path / "hub.sock"))
    return data_dir


def owner_registry(installed, hub):
    registry = ToolRegistry()
    assert chrome_tools.register_chrome_companion_tools(registry, data_dir=installed, hub=hub,
                                                        agent=object())
    return registry


# ── the protocol claim ─────────────────────────────────────────────────

def test_protocol_accepts_exactly_one_claim_shape():
    assert protocol.validate_request(request_message())["op"] == "follow_link"
    # An empty label is real: an image-only link reports text "".
    assert protocol.validate_request(request_message(args=claim(text="")))["op"] == "follow_link"


def test_one_navigation_allow_discloses_stage2_scope():
    popup = " ".join(POPUP.read_text().split()).lower()
    for phrase in ("open urls you directly requested", "switch among observed tabs",
                   "follow one verified link", "typing", "filling", "submitting forms",
                   "posting", "voting", "purchasing", "deleting", "changing account settings",
                   "generic site actions"):
        assert phrase in popup
    assert popup.count('id="navigation"') == 1
    readme = (WORKER.parent.parent / "README.md").read_text().lower()
    assert "follows one verified same-origin link" in readme
    assert "cannot click page elements" in readme
    assert "cannot click page elements, type, submit, follow observed links" not in readme


@pytest.mark.parametrize("args, why", [
    (claim(href=""), "an empty destination"),
    (claim(href="javascript:alert(1)"), "a non-HTTP scheme"),
    (claim(href="data:text/html,hi"), "a data URL"),
    (claim(document_id=""), "no document binding at all"),
    (claim(window_id=-1), "a negative window"),
    (claim(window_id="1"), "a window that is not an integer"),
    ({"window_id": 1, "document_id": DOC, "text": TEXT}, "a missing href"),
    (claim(selector="a"), "a smuggled CSS selector"),
    (claim(script="location='evil'"), "smuggled script"),
    (claim(submit=True), "smuggled submit"),
    (claim(method="POST"), "smuggled method"),
    (claim(target="_blank"), "smuggled target"),
    (claim(url=HREF), "a bare destination field"),
])
def test_protocol_refuses_every_other_claim(args, why):
    with pytest.raises(protocol.ProtocolError):
        protocol.validate_request(request_message(args=args))


def test_protocol_requires_a_tab_for_follow_link():
    with pytest.raises(protocol.ProtocolError):
        protocol.validate_request(request_message(tab_id=None))
    with pytest.raises(protocol.ProtocolError):
        protocol.validate_request(request_message(tab_id=-3))


def test_protocol_never_accepts_a_bare_destination_argument_set():
    """follow_link has no field the model can use as a destination on its
    own: href is only ever a CLAIM, and the document supplies the truth."""
    fields = set(claim())
    assert fields == {"window_id", "document_id", "text", "href"}
    assert not fields & {"url", "dest", "destination", "target", "to", "navigate_to"}


# ── the receipt ────────────────────────────────────────────────────────

def test_result_is_validated_as_an_action_receipt():
    receipt = {"operation_id": f"{CID}:{RID}", "status": "browser_local_effect_observed",
               "tab_id": 7, "window_id": 1, "observed_url": HREF, "load_confirmed": True}
    assert protocol.validate_result("follow_link", dict(receipt))["status"] == \
        "browser_local_effect_observed"
    for status in ("dispatched", "ambiguous_after_dispatch"):
        protocol.validate_result("follow_link", dict(receipt, status=status,
                                                     observed_url=None, load_confirmed=False))


def test_result_refuses_a_receipt_that_claims_more_than_it_proved():
    base = {"operation_id": f"{CID}:{RID}", "status": "browser_local_effect_observed",
            "tab_id": 7, "window_id": 1, "observed_url": HREF, "load_confirmed": True}
    for broken in ({**base, "load_confirmed": "yes"},
                   {**base, "status": "clicked"},
                   {**base, "operation_id": "not-an-operation-id"},
                   {k: v for k, v in base.items() if k != "observed_url"}):
        with pytest.raises(protocol.ProtocolError):
            protocol.validate_result("follow_link", broken)


# ── the tool ───────────────────────────────────────────────────────────

def test_tool_is_registered_only_on_an_owner_registry(installed):
    registry = owner_registry(installed, FakeHub())
    assert "chrome_follow_link" in set(registry.list_tools())
    assert "chrome_follow_link" in chrome_tools.CHROME_ACTION_TOOL_NAMES
    # Tier alone does not keep a browsing action away from a subagent: this
    # second axis is what actually strips it, whatever the parent asks for.
    assert TOOL_TIERS["chrome_follow_link"] == "execute"
    assert "chrome_follow_link" in OWNER_ONLY_TOOLS
    all_tools = list(registry.list_tools())
    guest = resolve_enabled_set(None, all_tools, owner=False, all_tools=all_tools)
    owner = resolve_enabled_set(None, all_tools, owner=True, all_tools=all_tools)
    assert "chrome_follow_link" not in guest, "stripped from every non-owner profile"
    assert "chrome_follow_link" in owner


def test_tool_is_absent_without_the_action_registry(installed):
    registry = ToolRegistry()
    assert chrome_tools.register_chrome_companion_tools(registry, data_dir=installed,
                                                        hub=FakeHub())
    assert "chrome_follow_link" not in set(registry.list_tools())


def test_tool_sends_the_claim_verbatim_and_nothing_else(installed):
    hub = FakeHub()
    registry = owner_registry(installed, hub)
    registry.call("chrome_follow_link", {"tab_id": 7, "window_id": 1,
                                         "document_id": DOC, "text": TEXT, "href": HREF})
    assert len(hub.calls) == 1
    call = hub.calls[0]
    assert call["op"] == "follow_link"
    assert call["tab_id"] == 7
    # Exactly the four claim fields -- no selector, no script, no free-form
    # action the model could fill in later.
    assert set(call["args"]) == {"window_id", "document_id", "text", "href"}
    assert call["args"]["href"] == HREF


@pytest.mark.parametrize("field,value", [("selector", "a"), ("script", "alert(1)"),
                                          ("submit", True), ("method", "POST"),
                                          ("target", "_blank"), ("url", HREF),
                                          ("click", True)])
def test_tool_rejects_unknown_actuator_args(installed, field, value):
    hub = FakeHub()
    registry = owner_registry(installed, hub)
    payload = json.loads(registry.call("chrome_follow_link", {
        "tab_id": 7, **claim(), field: value}))
    assert payload["ok"] is False
    assert payload["failure_class"] == "invalid_args"
    assert hub.calls == []


def test_follow_link_arguments_are_withheld_from_telemetry(installed, capsys):
    args = {"tab_id": 7, **claim(href=HREF + "?private=canary")}
    fields = _tool_call_fields(1, 0, "chrome_follow_link", args)
    assert fields["args"] == "[Chrome action arguments withheld]"
    assert fields["args_hash"] is None
    _log_tool_call("owner")("chrome_follow_link", args)
    assert "canary" not in capsys.readouterr().out


def test_follow_link_telemetry_requires_a_verified_receipt():
    receipt = {"ok": True, "status": "browser_local_effect_observed",
               "operation_id": f"{CID}:{RID}", "tab_id": 7, "observed_url": HREF,
               "load_confirmed": True, "verified_document_id": "doc-new"}
    assert _chrome_action_receipt_success("chrome_follow_link", json.dumps(receipt))
    for broken in ("garbage", "{}", json.dumps({**receipt, "ok": False}),
                   json.dumps({**receipt, "status": "ambiguous_after_dispatch"}),
                   json.dumps({**receipt, "verified_document_id": None}),
                   json.dumps({**receipt, "operation_id": "bogus"})):
        assert not _chrome_action_receipt_success("chrome_follow_link", broken)


@pytest.mark.parametrize("args", [
    {"tab_id": 7, "window_id": 1, "document_id": DOC, "text": TEXT, "href": ""},
    {"tab_id": 7, "window_id": 1, "document_id": DOC, "text": TEXT, "href": "javascript:1"},
    {"tab_id": 7, "window_id": 1, "document_id": "", "text": TEXT, "href": HREF},
    {"tab_id": 7, "window_id": -1, "document_id": DOC, "text": TEXT, "href": HREF},
    {"tab_id": 7, "window_id": 1, "document_id": DOC, "text": "x" * 5000, "href": HREF},
])
def test_tool_refuses_a_malformed_claim_without_reaching_chrome(installed, args):
    hub = FakeHub()
    registry = owner_registry(installed, hub)
    payload = json.loads(registry.call("chrome_follow_link", args))
    assert payload["ok"] is False
    assert payload["status"] == "failed_before_dispatch"
    assert payload["failure_class"] == "invalid_args"
    assert hub.calls == [], "a malformed claim never reaches the browser"


def test_tool_reports_a_refusal_as_a_refusal(installed):
    hub = FakeHub(error=CompanionError("target_ambiguous", "more than one link"))
    registry = owner_registry(installed, hub)
    payload = json.loads(registry.call("chrome_follow_link", {"tab_id": 7, "window_id": 1,
                                                             "document_id": DOC, "text": TEXT,
                                                             "href": HREF}))
    assert payload["ok"] is False
    assert payload["status"] == "failed_before_dispatch"
    assert payload["failure_class"] == "target_ambiguous"
    assert payload["provenance"] == {"owner": False, "trust": "external_untrusted",
                                     "source": "chrome_companion"}


def test_tool_never_reports_a_dispatch_it_could_not_verify(installed):
    hub = FakeHub(status="ambiguous_after_dispatch")
    registry = owner_registry(installed, hub)
    payload = json.loads(registry.call("chrome_follow_link", {"tab_id": 7, "window_id": 1,
                                                             "document_id": DOC, "text": TEXT,
                                                             "href": HREF}))
    assert payload["ok"] is False, "an unverified navigation is never reported as success"
    assert payload["status"] == "ambiguous_after_dispatch"
    assert payload["observed_url"] is None
    assert payload["load_confirmed"] is False


def test_tool_receipt_names_the_resulting_document(installed):
    hub = FakeHub().verified_hub("doc-finger-2")
    registry = owner_registry(installed, hub)
    payload = json.loads(registry.call("chrome_follow_link", {"tab_id": 7, "window_id": 1,
                                                             "document_id": DOC, "text": TEXT,
                                                             "href": HREF}))
    assert payload["ok"] is True
    assert payload["status"] == "browser_local_effect_observed"
    assert payload["observed_url"] == HREF
    assert payload["load_confirmed"] is True
    assert payload["verified_document_id"] == "doc-finger-2"
    assert payload["verified_origin"] == "https://www.reddit.com"


def test_tool_result_is_external_untrusted_provenance(installed):
    registry = owner_registry(installed, FakeHub().verified_hub())
    payload = json.loads(registry.call("chrome_follow_link", {"tab_id": 7, "window_id": 1,
                                                             "document_id": DOC, "text": TEXT,
                                                             "href": HREF}))
    assert payload["provenance"]["owner"] is False
    assert payload["provenance"]["trust"] == "external_untrusted"
    assert "Bino approved" in payload["notice"] or "EXTERNAL" in payload["notice"]


# ── vocabulary drift ───────────────────────────────────────────────────

def _worker_op_rule():
    source = WORKER.read_text(encoding="utf-8")
    block = re.search(r"const OP_TAB_RULE = \{(.*?)\};", source, re.S).group(1)
    return dict(re.findall(r'([a-z_]+): "(none|required|optional)"', block))


def _worker_action_ops():
    source = WORKER.read_text(encoding="utf-8")
    block = re.search(r"const ACTION_OPS = new Set\(\[(.*?)\]\);", source, re.S).group(1)
    return set(re.findall(r'"([a-z_]+)"', block))


def test_the_three_implementations_agree_on_the_operation_vocabulary():
    """A browser-moving operation missing from any one of these sets would
    silently skip the owner's navigation-allow and revoke overrides. The
    extension, the hub, and the protocol must name exactly the same ops."""
    worker_rules = _worker_op_rule()
    assert set(worker_rules) == set(protocol.OPS), "worker OP_TAB_RULE drifted from protocol.OPS"
    assert _worker_action_ops() == set(ACTION_OPS), "worker ACTION_OPS drifted from hub.ACTION_OPS"
    assert set(ACTION_OPS) <= set(protocol.OPS)
    assert {op for op, rule in worker_rules.items() if rule == "required"} == \
        set(protocol.OPS_TAB_REQUIRED)
    assert {op for op, rule in worker_rules.items() if rule == "optional"} == \
        set(protocol.OPS_TAB_OPTIONAL)
    assert {op for op, rule in worker_rules.items() if rule == "none"} == set(protocol.OPS_TAB_NONE)
    assert "follow_link" in protocol.OPS_TAB_REQUIRED
    assert "follow_link" in ACTION_OPS


def test_stage_two_added_no_typing_or_submission_capability():
    """Stage 3 (HAND) is NOT this campaign. If a future change quietly adds a
    typing, filling, or submitting operation to the companion vocabulary,
    this fails."""
    worker = WORKER.read_text(encoding="utf-8")
    for forbidden in ("type_text", "fill_form", "set_value", "press_key", "submit_form",
                      "post_comment", "click_at", "evaluate_js"):
        assert forbidden not in worker
        assert forbidden not in protocol.OPS
    assert set(protocol.OPS) == {"ping", "list_tabs", "get_active_tab", "get_tab",
                                 "extract_text", "get_links", "open_owner_url",
                                 "switch_tab", "follow_link"}


def test_the_extension_still_never_executes_page_code_for_a_navigation():
    """The 01B-A law Stage 2 must not erode: no action injects a function
    that clicks, types, or dispatches. follow_link injects a read-only
    resolver and navigates at the browser level."""
    worker = WORKER.read_text(encoding="utf-8")
    resolver = worker[worker.index("function resolveLinkTarget"):]
    resolver = resolver[:resolver.index("\n}\n")]
    assert ".click(" not in resolver
    assert "dispatchEvent" not in resolver
    assert ".value" not in resolver
    assert "querySelectorAll" in resolver, "it reads the DOM, read-only"
