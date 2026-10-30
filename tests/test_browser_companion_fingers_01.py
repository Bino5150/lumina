"""F0 interaction authority, strict transport, tool and privacy boundaries."""
import json

import pytest
from chrome_companion_testkit import connect_ready, make_hub, ok_response, setup_companion, short_tmpdir

from chrome_companion import interaction, mission, protocol
from core.chrome_companion_hub import ACTION_OPS, CompanionError, extension_version_at_least, OP_MIN_EXTENSION_VERSION
from core.tool_profiles import OWNER_ONLY_TOOLS, TOOL_TIERS
from tools.chrome_companion import CHROME_ACTION_TOOL_NAMES, register_chrome_companion_tools
from tools.registry import ToolRegistry

URL = "http://127.0.0.1:8765/inert.html"
CID, RID, DOC = "c" * 32, "000000000001" + "a" * 20, "fixture-doc"
ARGS = dict(kind="set_checked", value=True, window_id=1, document_id=DOC, ref="e1", gen=1, fixture_url=URL)


def request(args=None, tab_id=7):
    return dict(v=1, type="request", connection_id=CID, request_id=RID, op="interact", tab_id=tab_id,
                deadline_ms=10000, args=dict(ARGS if args is None else args))


@pytest.mark.parametrize("change", [dict(kind="submit"), dict(kind="click"), dict(kind="send"), dict(kind="delete"),
    dict(kind="purchase"), dict(kind="upload"), dict(kind="grant_permission"), dict(kind="fill"),
    dict(kind="select"), dict(script="alert(1)"),
    dict(selector="button"), dict(ref="#submit"), dict(gen=True), dict(gen=0), dict(window_id=True),
    dict(document_id=""), dict(value="true"), dict(value=1), dict(fixture_url="https://github.com/"),
    dict(fixture_url="http://localhost:8765/inert.html"), dict(fixture_url="http://127.0.0.2:8765/inert.html")])
def test_request_refuses_outside_fixed_contract(change):
    with pytest.raises(protocol.ProtocolError):
        protocol.validate_request(request(dict(ARGS, **change)))


@pytest.mark.parametrize("kind", sorted(interaction.KINDS))
def test_fixed_kinds_have_a_bounded_native_host_frame(kind):
    args = dict(ARGS, kind=kind)
    if kind == "focus":
        del args["value"]
    req = request(args)
    protocol.validate_request(req)
    assert protocol.decode_payload(protocol.encode_frame(req, protocol.MAX_TO_EXTENSION_BYTES)[4:]) == req


@pytest.mark.parametrize("tab_id", [None, True, -1, "7"])
def test_exact_tab_is_required(tab_id):
    with pytest.raises(protocol.ProtocolError):
        protocol.validate_request(request(tab_id=tab_id))


@pytest.mark.parametrize("owner,source,text,allowed", [
    (True, "OWNER_DIRECT", "test browser fixture " + URL, True),
    (True, "OWNER_DIRECT", "TEST BROWSER FIXTURE " + URL + " focus Notes", True),
    (True, "OWNER_DIRECT", "browse " + URL, False),
    (True, "OWNER_DIRECT", "test browser fixture https://github.com/", False),
    (False, "OWNER_DIRECT", "test browser fixture " + URL, False),
    (True, "TOOL_OUTPUT", "test browser fixture " + URL, False),
    (True, "EXTERNAL_CHANNEL_INBOUND", "test browser fixture " + URL, False),
])
def test_only_explicit_authenticated_fixture_mission_grants_class(owner, source, text, allowed):
    token = mission.begin_turn(text, source=source, owner=owner, event_id="fixture-event")
    try:
        current = mission.current()
        assert bool(current and interaction.MISSION_CLASS in current.classes) is allowed
        if allowed:
            assert current.fixture_url == URL
    finally:
        mission.end_turn(token)


def test_budget_and_reconnect_never_refund_fixture_authority():
    token = mission.begin_turn("test browser fixture " + URL, source="OWNER_DIRECT", owner=True, event_id="budget")
    try:
        current = mission.current()
        for _ in range(interaction.MAX_INTERACTIONS):
            current.check(interaction.MISSION_CLASS, CID)
        with pytest.raises(mission.MissionRefused, match="mission_budget_exhausted"):
            current.check(interaction.MISSION_CLASS, CID)
        with pytest.raises(mission.MissionRefused, match="mission_session_changed"):
            current.check(interaction.MISSION_CLASS, "d" * 32)
        with pytest.raises(mission.MissionRefused, match="outside_mission_class"):
            current.check("submit", CID)
    finally:
        mission.end_turn(token)


def test_new_vocabulary_never_reaches_daily_pinned_extension():
    assert OP_MIN_EXTENSION_VERSION["interact"] == (0, 5, 0)
    assert not extension_version_at_least("0.4.1", OP_MIN_EXTENSION_VERSION["interact"])
    assert extension_version_at_least("0.5.0", OP_MIN_EXTENSION_VERSION["interact"])
    assert "interact" in ACTION_OPS
    assert "chrome_interact" in OWNER_ONLY_TOOLS & CHROME_ACTION_TOOL_NAMES
    assert TOOL_TIERS["chrome_interact"] == "execute"


class Hub:
    def __init__(self, error=None):
        self.calls, self.error = [], error

    def current_connection_id(self):
        return CID

    def request(self, op, **kwargs):
        self.calls.append((op, kwargs))
        if self.error:
            raise self.error
        return {"result": dict(operation_id=f"{CID}:{RID}", status="browser_local_effect_observed", tab_id=7,
            window_id=1, observed_url=URL, load_confirmed=False, kind="set_checked", ref="e1", gen=1,
            before=False, after=True), "observed": {"url": URL, "origin": "http://127.0.0.1:8765", "document_id": DOC}}


def registry(tmp_path, monkeypatch, hub, agent=object()):
    from chrome_companion import state
    monkeypatch.setattr(state, "load_install", lambda _: object())
    registry = ToolRegistry()
    assert register_chrome_companion_tools(registry, data_dir=tmp_path, hub=hub, agent=agent)
    return registry


TOOL_ARGS = dict(action="set_checked", value=True, tab_id=7, window_id=1, document_id=DOC, ref="e1", gen=1)


def test_no_action_registry_no_actuator(tmp_path, monkeypatch):
    assert "chrome_interact" not in registry(tmp_path, monkeypatch, Hub(), agent=None).list_tools()


@pytest.mark.parametrize("text", ["browse " + URL, "interact with the checkbox", ""])
def test_tool_cannot_promote_an_ordinary_mission(tmp_path, monkeypatch, text):
    hub = Hub()
    reg = registry(tmp_path, monkeypatch, hub)
    token = mission.begin_turn(text, source="OWNER_DIRECT", owner=True, event_id="no-authority")
    try:
        out = json.loads(reg.call("chrome_interact", TOOL_ARGS))
        assert out["status"] == "failed_before_dispatch"
        assert out["failure_class"] == "no_fixture_mission"
        assert hub.calls == []
    finally:
        mission.end_turn(token)


def test_tool_injects_only_owner_scope_and_preserves_external_provenance(tmp_path, monkeypatch):
    hub = Hub()
    reg = registry(tmp_path, monkeypatch, hub)
    token = mission.begin_turn("test browser fixture " + URL, source="OWNER_DIRECT", owner=True, event_id="fixture")
    try:
        out = json.loads(reg.call("chrome_interact", TOOL_ARGS))
        assert out["ok"] and out["after"] is True
        assert out["provenance"]["owner"] is False
        assert out["provenance"]["trust"] == "external_untrusted"
        assert len(hub.calls) == 1
        assert hub.calls[0][1]["args"] == ARGS
    finally:
        mission.end_turn(token)


def test_interrupted_tool_answer_never_retries(tmp_path, monkeypatch):
    hub = Hub(CompanionError("timeout", "answer lost", executed=True, operation_id=f"{CID}:{RID}"))
    reg = registry(tmp_path, monkeypatch, hub)
    token = mission.begin_turn("test browser fixture " + URL, source="OWNER_DIRECT", owner=True, event_id="lost")
    try:
        out = json.loads(reg.call("chrome_interact", TOOL_ARGS))
        assert out["status"] == "ambiguous_after_dispatch" and out["ok"] is False
        assert len(hub.calls) == 1
    finally:
        mission.end_turn(token)


@pytest.mark.parametrize("code,reason", [("navigation_pending", "navigation is pending"),
    ("page_loading", "loading is incomplete"), ("stale_document", "document identity"),
    ("stale_observation", "observation is stale")])
def test_loading_refusal_gives_bounded_recovery_guidance(tmp_path, monkeypatch, code, reason):
    hub = Hub(CompanionError(code, "CANARY PRIVATE RAW MESSAGE", executed=False))
    reg = registry(tmp_path, monkeypatch, hub)
    token = mission.begin_turn("test browser fixture " + URL, source="OWNER_DIRECT", owner=True, event_id="guidance")
    try:
        out = json.loads(reg.call("chrome_interact", TOOL_ARGS))
        assert out["status"] == "failed_before_dispatch" and out["ok"] is False
        assert reason in out["note"]
        assert "Wait for navigation/loading to settle" in out["note"]
        assert "fresh observation" in out["note"] and "may not resolve" in out["note"]
        assert "never retried automatically" in out["note"]
        assert "CANARY" not in str(out)
        assert len(hub.calls) == 1
    finally:
        mission.end_turn(token)


@pytest.mark.parametrize("code", ["navigation_pending", "page_loading", "stale_document", "stale_observation"])
def test_post_admission_loading_failure_remains_uncertain_without_retry(tmp_path, monkeypatch, code):
    hub = Hub(CompanionError(code, "private", executed=True))
    reg = registry(tmp_path, monkeypatch, hub)
    token = mission.begin_turn("test browser fixture " + URL, source="OWNER_DIRECT", owner=True, event_id="uncertain")
    try:
        out = json.loads(reg.call("chrome_interact", TOOL_ARGS))
        assert out["status"] == "ambiguous_after_dispatch" and out["ok"] is False
        assert "Execution or outcome is uncertain" in out["note"]
        assert "Do not retry this action" in out["note"]
        assert "before another action" not in out["note"]
        assert len(hub.calls) == 1
    finally:
        mission.end_turn(token)


def test_private_arguments_and_observations_stay_out_of_telemetry():
    from core.agent import _tool_call_fields, _tool_result_summary, _chrome_action_receipt_success
    args = dict(TOOL_ARGS, private="CANARY-PRIVATE")
    fields = _tool_call_fields(1, 0, "chrome_interact", args)
    assert fields["args_hash"] is None
    assert "CANARY" not in str(fields)
    assert "CANARY" not in _tool_result_summary("chrome_interact", "CANARY-PRIVATE")
    answer = dict(Hub().request("interact")["result"], ok=True,
                  observed={"url": URL, "document_id": DOC})
    assert _chrome_action_receipt_success("chrome_interact", json.dumps(answer))
    assert not _chrome_action_receipt_success("chrome_interact", json.dumps(dict(answer, after=None)))


@pytest.mark.parametrize("change", [dict(after="true"), dict(before="private value"), dict(ref="#x"),
                                    dict(status="consequential_effect_confirmed"), dict(after=None)])
def test_false_completion_receipts_fail_closed(change):
    answer = dict(Hub().request("interact")["result"], **change)
    with pytest.raises(protocol.ProtocolError):
        protocol.validate_result("interact", answer)


@pytest.mark.parametrize("change,observed_change", [
    ({"kind": "focus"}, {}), ({"ref": "e2"}, {}), ({"gen": 2}, {}), ({"window_id": 9}, {}),
    ({"observed_url": "http://127.0.0.1:8765/other"}, {}), ({}, {"document_id": "other-doc"}),
    ({"after": False}, {}), ({"before": None}, {}), ({"load_confirmed": True}, {}),
])
def test_hub_never_accepts_a_forged_success_tuple(tmp_path, change, observed_change):
    with short_tmpdir() as sdir:
        data = tmp_path / "data"
        data.mkdir()
        socket_path = setup_companion(data, sdir)
        hub = make_hub(data)
        hub.start()
        host = None
        try:
            host, _ = connect_ready(hub, socket_path, extension_version="0.5.0")
            hub._current.record_delivered_refs(7, DOC, [{"ref": "e1"}])
            def answer(req):
                result = dict(Hub().request("interact")["result"],
                              operation_id=f'{req["connection_id"]}:{req["request_id"]}')
                result.update(change)
                observed = dict(url=URL, origin="http://127.0.0.1:8765", document_id=DOC)
                observed.update(observed_change)
                return ok_response(req, result, tab_id=7, observed=observed)
            host.serve(answer)
            with pytest.raises(CompanionError) as refused:
                hub.request("interact", tab_id=7, args=ARGS, timeout_s=3)
            assert refused.value.code == "malformed_response"
            assert refused.value.executed is True, "unverifiable post-dispatch evidence remains ambiguous"
        finally:
            hub.stop()
            if host is not None:
                host.close()
