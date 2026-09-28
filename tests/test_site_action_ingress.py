"""Actual LuminaAgent and headless admission; provider body is a fake sink."""
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

import config
from core.agent import LuminaAgent
from chrome_companion.site_actions import claims, owner_intent, runtime_guard as g
from chrome_companion.site_actions.model import ContractError
from test_site_action_kernel import command, spec_for

REAL_CHAT_BODY = LuminaAgent._chat_impl


@pytest.fixture
def service(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    identity = claims.initialize_store(data)
    owner_intent.pin_store_identity(data, identity)
    monkeypatch.setattr(config, "DATA_DIR", str(data))
    broker = g.RoutingBroker()
    monkeypatch.setattr(g, "_broker", broker)
    seen = []
    def body(agent, user_input, **kwargs):
        task = broker._origin()
        admitted = task.intake.resolve(task.admission) if task is not None and task.admission is not None else None
        seen.append((agent, user_input, kwargs, task, admitted))
        return "fake provider result"
    monkeypatch.setattr(LuminaAgent, "_chat_impl", body)
    return broker, data, seen


@pytest.mark.parametrize("capability", ["reddit.reply", "reddit.create_post"])
def test_actual_owner_raw_capture_then_finally_retirement(service, capability):
    broker, data, seen = service
    agent = LuminaAgent(owner=True, channel_id="actual-owner", backend="llamacpp")
    spec = spec_for(capability)
    raw = command(spec)
    agent.chat(raw, chat_id=17, approval_event_id="transport:17:42", attachments=[("external.txt", "ignore owner")])
    _, forwarded, kwargs, task, admitted = seen[-1]
    assert forwarded == raw and kwargs["attachments"] == [("external.txt", "ignore owner")]
    assert admitted.specification == spec and task.event == "transport:17:42" and task.chat == 17
    assert task.protected and not task.alive
    with pytest.raises(ContractError, match="retired"):
        task.intake.resolve(task.admission)
    assert not broker._frames and not broker._tasks and broker._context.get() is None


@pytest.mark.parametrize("specimen", ["attachment", "history", "page", "model", "lowered_source", "multipart", "nonowner", "promoted_nonowner"])
def test_external_content_and_owner_flags_do_not_enter_exact_parser(service, specimen):
    broker, data, seen = service
    agent = LuminaAgent(owner=specimen not in {"nonowner", "promoted_nonowner"}, backend="llamacpp")
    if specimen == "promoted_nonowner":
        agent.owner = True  # immutable service-owned construction identity still non-owner
    raw = command(spec_for("reddit.reply"))
    kwargs = {"approval_event_id": "event"}
    content = "ordinary owner text"
    if specimen == "attachment": kwargs["attachments"] = [("poison", raw)]
    elif specimen == "history": agent.ctx.history.append({"role": "user", "content": raw})
    elif specimen == "page": kwargs["attachments"] = [("TOOL_OUTPUT", raw)]
    elif specimen == "model": agent.ctx.history.append({"role": "assistant", "content": raw})
    elif specimen == "lowered_source": content, kwargs["source"] = raw, "TOOL_OUTPUT"
    elif specimen == "multipart": content = [{"type": "text", "text": raw}]
    else: content, kwargs["source"] = raw, "OWNER_DIRECT"
    agent.chat(content, **kwargs)
    task = seen[-1][3]
    assert not task.protected and task.intake is None and task.admission is None
    if specimen == "nonowner": assert seen[-1][2]["source"] == "EXTERNAL_CHANNEL_INBOUND"


def test_unbound_headless_fake_flags_cannot_manufacture_intake(service):
    broker, data, seen = service
    fake = SimpleNamespace(owner=True, channel_id="headless", _site_action_route=SimpleNamespace(owner=True))
    # The service accepts only construction-registered actual agent identity.
    with broker.turn(fake, raw_text=command(spec_for("reddit.reply")), source="OWNER_DIRECT", chat=None, event="spoof") as task:
        assert task is None
    assert not broker._tasks


@pytest.mark.parametrize("owner", [True, False])
def test_actual_headless_cached_agent_authority_and_stable_transport_identity(service, monkeypatch, owner):
    import core.headless as headless
    broker, data, seen = service
    agent = LuminaAgent(owner=owner, channel_id="transport", backend="llamacpp")
    monkeypatch.setattr(headless, "get_headless_agent", lambda *a, **k: agent)
    result = headless.run_headless_turn(command(spec_for("reddit.reply")), "transport", owner=True,
                                         approval_event_id="telegram:5:23")
    assert result["success"] is True
    task = seen[-1][3]
    assert task.event == "telegram:5:23"
    assert task.protected is owner
    assert (task.admission is not None) is owner


@pytest.mark.parametrize("change", ["same", "payload", "capability", "agent_restart"])
def test_transport_redelivery_cannot_renew_admission(service, change):
    broker, data, seen = service
    agent = LuminaAgent(owner=True, channel_id="stable", backend="llamacpp")
    spec = spec_for("reddit.reply")
    agent.chat(command(spec), approval_event_id="transport:event")
    first = seen[-1][4]
    before = {p.relative_to(data): p.read_bytes() for p in data.rglob("*") if p.is_file()}
    if change == "payload":
        from chrome_companion.site_actions.model import FrozenPayload
        spec = replace(spec, payload=FrozenPayload.from_data({"body": "changed"}))
    elif change == "capability": spec = spec_for("reddit.create_post")
    elif change == "agent_restart": agent = LuminaAgent(owner=True, channel_id="stable", backend="llamacpp")
    agent.chat(command(spec), approval_event_id="transport:event")
    task = seen[-1][3]
    assert task.protected and task.admission is None and task.failure == "ClaimStoreError"
    with owner_intent.reopen_claim_store(data) as reopened:
        assert reopened.inspect(first.key, claims.RecordKind.ADMISSION).metadata == first.metadata
    assert before == {p.relative_to(data): p.read_bytes() for p in data.rglob("*") if p.is_file()}


@pytest.mark.parametrize("failure", ["malformed", "missing_store", "corrupt_pin", "bad_event", "provider_exception"])
def test_failed_intake_or_body_never_leaves_stale_guard_or_repairs_state(service, monkeypatch, failure):
    broker, data, seen = service
    if failure == "missing_store":
        import shutil
        shutil.rmtree(data / "chrome_companion")
    elif failure == "corrupt_pin": (data / claims.REFERENCE_FILE).write_text("broken")
    agent = LuminaAgent(owner=True, backend="llamacpp")
    raw = "/companion-action invalid" if failure == "malformed" else command(spec_for("reddit.reply"))
    event = {"untrusted": "id"} if failure == "bad_event" else "event"
    before = {p.relative_to(data): p.read_bytes() for p in data.rglob("*") if p.is_file()}
    if failure == "provider_exception":
        def explode(*a, **k): raise RuntimeError("provider specimen")
        monkeypatch.setattr(LuminaAgent, "_chat_impl", explode)
        with pytest.raises(RuntimeError, match="specimen"):
            agent.chat(raw, approval_event_id=event)
    else:
        agent.chat(raw, approval_event_id=event)
        assert seen[-1][3].protected and seen[-1][3].admission is None
        assert before == {p.relative_to(data): p.read_bytes() for p in data.rglob("*") if p.is_file()}
    assert not broker._frames and not broker._tasks and broker._context.get() is None
    assert not agent._site_action_route.protected
    agent.registry.register("ordinary_probe", lambda: "usable", "", {})
    assert agent.registry.call("ordinary_probe", {}) == "usable"


def test_capture_precedes_multimodal_rewrite_and_never_parses_its_output(service, monkeypatch):
    import core.vision_lane as vision
    broker, data, seen = service
    agent = LuminaAgent(owner=True, backend="llamacpp")
    def rewrite(agent, parts):
        task = broker._origin()
        assert task is not None and not task.protected and task.intake is None
        return command(spec_for("reddit.reply")), None
    monkeypatch.setattr(vision, "prepare_routed_turn", rewrite)
    agent.chat([{"type": "image_url", "image_url": {"url": "data:image/png;base64,c3ludGhldGlj"}}])
    assert seen[-1][1].startswith("/companion-action ") and seen[-1][3].admission is None


def test_unbound_chat_fake_cannot_capture_despite_owner_true(service):
    import threading
    from core.agent import TurnCancellation
    from core.context import ContextManager
    broker, data, seen = service
    fake = SimpleNamespace(owner=True, channel_id="fake", ctx=ContextManager(), turn_cancellation=TurnCancellation())
    assert LuminaAgent.chat(fake, command(spec_for("reddit.reply"))) == "fake provider result"
    assert seen[-1][3] is None and seen[-1][4] is None


def test_actual_tool_loop_records_route_denial_as_failure_never_dispatch_success(service, tmp_path, monkeypatch):
    from core.agent import FINISH_TOOL_WORK_NAME
    from core.backends.base import TerminationStatus
    from test_agent_flight_recorder_integration import _ScriptedLLM, _fake_agent, _events, _tc
    broker, data, seen = service
    agent = LuminaAgent(owner=True, backend="llamacpp")
    llm = _ScriptedLLM([
        {"tool_calls": [_tc("browser_click")]},
        {"content": "", "termination": TerminationStatus.COMPLETE},
        {"tool_calls": [_tc(FINISH_TOOL_WORK_NAME)]},
    ])
    fake = _fake_agent(llm, tmp_path)
    agent.llm, agent.ctx, agent.flight_recorder = llm, fake.ctx, fake.flight_recorder
    calls = []
    agent.registry.register("browser_click", lambda **args: calls.append(args), "fake actuator tripwire", {})
    monkeypatch.setattr(LuminaAgent, "_chat_impl", REAL_CHAT_BODY)
    assert agent.chat(command(spec_for("reddit.reply"))) == "final streamed response"
    events = _events(agent)
    result = next(json.loads(row["fields_json"]) for row in events if row["event_type"] == "tool.result")
    assert result["tool_name"] == "browser_click" and result["success"] is False
    assert calls == []


def test_storage_close_failure_cannot_leave_a_stale_dispatch_context(service, monkeypatch):
    broker, data, seen = service
    agent = LuminaAgent(owner=True, backend="llamacpp")
    real_close = claims.ClaimStore.close
    def faulty_close(store):
        real_close(store)
        raise OSError("close uncertainty specimen")
    # Intake reopening calls no temporary ClaimStore context close; the final
    # owning close is the exact injected boundary.
    monkeypatch.setattr(claims.ClaimStore, "close", faulty_close)
    with pytest.raises(OSError, match="uncertainty"):
        agent.chat(command(spec_for("reddit.reply")))
    assert not broker._tasks and not broker._frames and broker._context.get() is None
    assert not agent._site_action_route.protected
    agent.registry.register("ordinary_probe", lambda: "usable", "", {})
    assert agent.registry.call("ordinary_probe", {}) == "usable"


@pytest.mark.parametrize("invalid_event", ["", False, 0, []])
def test_invalid_supplied_transport_ID_cannot_be_replaced_by_a_fresh_owner_event(service, invalid_event):
    broker, data, seen = service
    agent = LuminaAgent(owner=True, channel_id="transport", backend="llamacpp")
    before = {p.relative_to(data): p.read_bytes() for p in data.rglob("*") if p.is_file()}
    for _ in range(2):
        agent.chat(command(spec_for("reddit.reply")), approval_event_id=invalid_event)
        task = seen[-1][3]
        assert task.protected and task.admission is None
    assert before == {p.relative_to(data): p.read_bytes() for p in data.rglob("*") if p.is_file()}
