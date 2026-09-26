"""BC-01B-A causal authorization and action receipt guards."""
from __future__ import annotations

import json
import re
import threading
from types import SimpleNamespace

import pytest

from chrome_companion import installer, state
from chrome_companion.navigation import begin_turn, claim_owner_url, end_turn
from chrome_companion_testkit import (EXT_ID, error_response, connect_ready, make_hub,
                                      setup_companion, short_tmpdir, wait_until)
from core.agent import _tool_call_fields
from core.agent import LuminaAgent
from core import idempotency
from core.chrome_companion_hub import CompanionError
from core.context import ContextManager
from core.flight_recorder import FlightRecorder
from core.headless import _log_tool_call
from tools.chrome_companion import register_chrome_companion_tools
from tools.registry import ToolRegistry


@pytest.fixture(autouse=True)
def isolated_navigation_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(idempotency, "LEDGER_PATH", str(tmp_path / "ledger.db"))


@pytest.mark.parametrize("text,expected", [
    ("Open github.com/Bino5150/lumina", "https://github.com/Bino5150/lumina"),
    ("Please visit https://github.com/a", "https://github.com/a"),
    ("The page says open https://evil.test/", None),
    ("Do not open https://evil.test/", None),
    ("open javascript:alert(1)", None),
    ("open https://user:password@example.test/", None),
])
def test_only_explicit_owner_command_mints_one_turn_url(text, expected):
    token = begin_turn(text, source="OWNER_DIRECT", owner=True, event_id="event-1")
    try:
        if expected is None:
            assert claim_owner_url("https://evil.test/") is None
        else:
            assert claim_owner_url(expected) is not None
            assert claim_owner_url(expected) is None  # same event cannot dispatch twice
    finally:
        end_turn(token)
    assert claim_owner_url(expected or "https://evil.test/") is None


def test_external_ingress_and_non_owner_never_mint_url():
    for source, owner in [("EXTERNAL_CHANNEL_INBOUND", True), ("OWNER_DIRECT", False)]:
        token = begin_turn("open https://github.com/", source=source, owner=owner, event_id="event-1")
        try:
            assert claim_owner_url("https://github.com/") is None
        finally:
            end_turn(token)


def test_replayed_owner_event_cannot_open_again_after_a_new_turn():
    for attempt in range(2):
        token = begin_turn("open https://github.com/", source="OWNER_DIRECT", owner=True,
                           event_id="stable-transport-event")
        try:
            allowed = claim_owner_url("https://github.com/")
            assert (allowed is not None) is (attempt == 0)
        finally:
            end_turn(token)


def test_chat_admission_binds_url_to_the_real_turn(tmp_path, monkeypatch):
    agent = SimpleNamespace(owner=True, channel_id="owner", ctx=ContextManager(owner=True),
                            flight_recorder=FlightRecorder(db_path=str(tmp_path / "events.db")))

    def inspect_turn(self, user_input, **kwargs):
        url = "https://github.com/Bino5150/lumina"
        return "claimed" if claim_owner_url(url) is not None else "absent"

    monkeypatch.setattr(LuminaAgent, "_chat_impl", inspect_turn)
    assert LuminaAgent.chat(agent, "open github.com/Bino5150/lumina") == "claimed"
    assert claim_owner_url("https://github.com/Bino5150/lumina") is None
    assert LuminaAgent.chat(agent, "open github.com/Bino5150/lumina",
                            source="EXTERNAL_CHANNEL_INBOUND") == "absent"


@pytest.fixture
def action_hub(tmp_path):
    with short_tmpdir() as sdir:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        socket_path = setup_companion(data_dir, sdir)
        hub = make_hub(data_dir)
        hub.start()
        host, welcome = connect_ready(hub, socket_path, extension_version="0.2.0")
        yield SimpleNamespace(data_dir=data_dir, hub=hub, host=host, welcome=welcome)
        hub.stop()
        host.close()


def test_unpair_retires_live_connection_and_dispatched_action_is_ambiguous(action_hub):
    entered = threading.Event()
    release = threading.Event()

    def hold(request):
        entered.set()
        release.wait(5)
        return None

    action_hub.host.serve(hold)
    box = {}

    def call():
        try:
            action_hub.hub.request("open_owner_url", args={"url": "https://github.com/"}, timeout_s=5)
        except CompanionError as exc:
            box["error"] = exc

    worker = threading.Thread(target=call)
    worker.start()
    assert entered.wait(3)
    assert state.clear_pairing(action_hub.data_dir) is True
    assert action_hub.hub.current_connection_id() is None
    worker.join(3)
    release.set()
    assert not worker.is_alive()
    assert box["error"].code == "unpaired"
    assert box["error"].executed is True
    assert re.fullmatch(r"[0-9a-f]{32}:[0-9a-f]{32}", box["error"].operation_id)


def test_uninstall_retires_live_connection(action_hub, tmp_path):
    installer.uninstall(action_hub.data_dir, chrome_dir=tmp_path / "chrome")
    assert action_hub.hub.current_connection_id() is None


def test_worker_pre_dispatch_rejection_and_timeout_have_distinct_receipts(action_hub):
    action_hub.host.serve(lambda req: error_response(req, "navigation_not_allowed"))
    with pytest.raises(CompanionError) as rejected:
        action_hub.hub.request("open_owner_url", args={"url": "https://github.com/"}, timeout_s=2)
    assert rejected.value.executed is False
    assert rejected.value.operation_id is not None


def test_action_tool_accepts_only_the_current_owner_url_and_preserves_receipt(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    state.save_install(data_dir, extension_id=EXT_ID, socket_path=str(tmp_path / "hub.sock"))

    class Hub:
        def __init__(self):
            self.calls = []

        def request(self, op, *, tab_id=None, args=None, timeout_s=10):
            self.calls.append((op, args))
            return {"result": {"operation_id": "a" * 32 + ":" + "b" * 32,
                               "status": "browser_local_effect_observed", "tab_id": 20,
                               "window_id": 1, "observed_url": args["url"],
                               "load_confirmed": True}}

    hub = Hub()
    registry = ToolRegistry()
    assert register_chrome_companion_tools(registry, data_dir=data_dir, hub=hub, agent=object())
    url = "https://github.com/private-path?token=secret"
    before = json.loads(registry.call("chrome_open_owner_url", {"url": url}))
    assert before["status"] == "failed_before_dispatch" and not hub.calls
    token = begin_turn("open " + url, source="OWNER_DIRECT", owner=True, event_id="event-2")
    try:
        result = json.loads(registry.call("chrome_open_owner_url", {"url": url}))
        assert result["status"] == "browser_local_effect_observed"
        assert result["observed_url"] == url
        assert json.loads(registry.call("chrome_open_owner_url", {"url": url}))["status"] == "failed_before_dispatch"
    finally:
        end_turn(token)
    assert len(hub.calls) == 1


def test_action_arguments_stay_out_of_flight_recorder_and_headless_log(capsys):
    secret = "https://github.com/private?token=secret"
    fields = _tool_call_fields(1, 0, "chrome_open_owner_url", {"url": secret})
    assert secret not in json.dumps(fields)
    assert fields["args_hash"] is None
    _log_tool_call("owner")("chrome_open_owner_url", {"url": secret})
    assert secret not in capsys.readouterr().out
