"""Synthetic 01D-B transport and tool-free foreground contract."""

import json
import types

import pytest

from core import backend_identity as bi
from core.backends.base import BaseLLMBackend, ModelDiscoveryOutcome
from core.backends.openai_chatgpt_plan import ChatGPTPlanBackend


class FakeResponse:
    def __init__(self, body=None, events=(), status=200):
        self.body = body
        self.events = events
        self.status_code = status
        self.ok = status == 200
        self.headers = {"x-request-id": "req-synthetic"}
        self.closed = False

    def json(self):
        return self.body

    def iter_lines(self):
        for event in self.events:
            if isinstance(event, Exception):
                raise event
            yield b"data: " + (event if isinstance(event, bytes) else json.dumps(event).encode())

    def close(self):
        self.closed = True


class FakeManager:
    def __init__(self):
        self.profile = "account-A"
        self.calls = []

    def get_valid_access_token(self):
        self.calls.append("grant")
        return types.SimpleNamespace(profile_id=self.profile, access_token="synthetic-oauth-token")

    def is_grant_current(self, grant):
        self.calls.append("current")
        return grant.profile_id == self.profile


@pytest.fixture
def transport(monkeypatch, tmp_path):
    from core import persistence
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    manager = FakeManager()
    backend = ChatGPTPlanBackend(manager=manager)
    backend._model = "model-A"
    calls = []
    catalog = {"models": [
        {"slug": "hidden", "display_name": "Hidden", "visibility": "hidden"},
        {"slug": "model-B", "display_name": "Second", "visibility": "list"},
        {"slug": "model-A", "display_name": "First", "visibility": "list"},
    ]}
    response_events = [
        {"type": "response.output_text.delta", "delta": "Hello"},
        {"type": "response.completed", "response": {
            "status": "completed", "output": [{"type": "message", "content": [
                {"type": "output_text", "text": "Hello"}]}],
            "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3}}},
    ]

    def get(url, **kw):
        calls.append(("get", url, kw))
        return FakeResponse(catalog)

    def post(url, **kw):
        calls.append(("post", url, kw))
        return FakeResponse(events=response_events)

    monkeypatch.setattr("core.backends.openai_chatgpt_plan.requests.get", get)
    monkeypatch.setattr("core.backends.openai_chatgpt_plan.requests.post", post)
    return backend, manager, calls, catalog, response_events


def test_plan_lane_constructible_only_for_foreground(monkeypatch, tmp_path):
    import inspect
    from core import persistence
    from core.backends.loader import get_llm_backend
    from core.backends.openai_backend import OpenAIBackend
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(OpenAIBackend, "__init__",
                        lambda *a, **kw: pytest.fail("paid API-key constructor called"))
    backend = get_llm_backend("openai_chatgpt_plan")
    assert isinstance(backend, ChatGPTPlanBackend)
    assert not hasattr(backend, "api_key") and not hasattr(backend, "headers")
    assert "OPENAI_API_KEY" not in inspect.getsource(ChatGPTPlanBackend)
    assert backend.supports_tool_work() is False
    assert BaseLLMBackend.supports_tool_work(backend) is True
    assert bi.operation_refusal(backend.backend_identity(), bi.OperationKind.FOREGROUND_CHAT) is None
    for kind in bi.OperationKind:
        if kind is not bi.OperationKind.FOREGROUND_CHAT:
            assert bi.operation_refusal(backend.backend_identity(), kind)


def test_catalog_filters_preserves_order_and_display_labels(transport):
    backend, _manager, calls, catalog, _events = transport
    result = backend.discover_models()
    assert result.outcome is ModelDiscoveryOutcome.SUCCESS
    assert result.models == ("model-B", "model-A")
    assert backend.model_labels == {"model-B": "Second", "model-A": "First"}
    assert backend.catalog_profile_id == "account-A"
    assert calls[0][1] == "https://api.openai.com/v1/models"
    assert calls[0][2]["headers"]["Authorization"] == "Bearer synthetic-oauth-token"
    catalog["models"] = [catalog["models"][0]]
    assert backend.discover_models().outcome is ModelDiscoveryOutcome.EMPTY
    assert backend.list_models() == []


def test_exact_wire_and_completed_stream(transport):
    backend, _manager, calls, _catalog, _events = transport
    messages = [{"role": "system", "content": "Trusted"},
                {"role": "user", "content": "Hello"}]
    chunks = list(backend.chat_stream(messages, reasoning_effort="max"))
    assert chunks[0] == "Hello"
    assert chunks[-1].fields["stream_completion_state"] == "completed"
    posts = [call for call in calls if call[0] == "post"]
    assert len(posts) == 1
    _, url, kwargs = posts[0]
    assert url == "https://api.openai.com/v1/responses"
    assert kwargs["headers"]["Authorization"] == "Bearer synthetic-oauth-token"
    assert kwargs["json"] == {"model": "model-A", "instructions": "Trusted",
                              "input": [{"role": "user", "content": "Hello"}],
                              "store": False, "stream": True}
    assert "synthetic-oauth-token" not in json.dumps(kwargs["json"])


def test_no_tools_vision_or_missing_model_substitution(transport):
    backend, _manager, calls, catalog, _events = transport
    with pytest.raises(ValueError, match="tool work"):
        backend.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function"}])
    with pytest.raises(ValueError, match="text-only"):
        list(backend.chat_stream([{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}}]}]))
    catalog["models"] = [{"slug": "other", "display_name": "Other", "visibility": "list"}]
    with pytest.raises(ValueError, match="not available"):
        list(backend.chat_stream([{"role": "user", "content": "hi"}]))
    assert not [c for c in calls if c[0] == "post"]


@pytest.mark.parametrize("terminal", [
    {"type": "response.failed", "response": {"status": "failed", "error": {"code": "usage_limit"}}},
    {"type": "response.incomplete", "response": {"status": "incomplete"}},
    {"type": "error", "message": "synthetic error"},
    b"[DONE]",
])
def test_noncompleted_stream_never_returns_final(transport, monkeypatch, terminal):
    backend, _manager, calls, _catalog, _events = transport
    def post(url, **kw):
        calls.append(("post", url, kw))
        return FakeResponse(events=[{"type": "response.output_text.delta", "delta": "partial"}, terminal])
    monkeypatch.setattr("core.backends.openai_chatgpt_plan.requests.post", post)
    with pytest.raises(RuntimeError):
        list(backend.chat_stream([{"role": "user", "content": "hi"}]))
    with pytest.raises(RuntimeError):
        backend.chat([{"role": "user", "content": "hi"}])


def test_tool_free_agent_branch_avoids_registry_and_gate(monkeypatch):
    import core.agent as agent_module
    from core.agent import LuminaAgent
    calls = []
    class NoTools:
        name = "openai_chatgpt_plan"
        def supports_tool_work(self):
            return False
        def configured_model(self):
            return "model-A"
    ctx = types.SimpleNamespace(
        history=[], turn_seq=0,
        add_user=lambda text, source="OWNER_DIRECT": calls.append(("user", text)),
        build_messages=lambda **kw: [{"role": "user", "content": "hi"}],
    )
    fake = types.SimpleNamespace(
        llm=NoTools(), ctx=ctx, owner=False, channel_id="synthetic",
        registry=types.SimpleNamespace(
            schema_token_estimate=lambda: pytest.fail("schema estimate called"),
            get_schemas=lambda: pytest.fail("schemas dispatched")),
        _stream_final=lambda messages, think_step, **kw: calls.append(("final", messages)) or "ok",
    )
    monkeypatch.setattr(agent_module, "build_skills_block", lambda text: ("", ""))
    monkeypatch.setattr(agent_module, "_maybe_approve_pending_draft", lambda *a, **kw: None)
    monkeypatch.setattr(agent_module, "_run_tool_work_control_gate",
                        lambda *a, **kw: pytest.fail("control gate called"))
    result = LuminaAgent._chat_impl(fake, "hi", source="OWNER_DIRECT")
    assert result == "ok"
    assert calls == [("user", "hi"), ("final", [{"role": "user", "content": "hi"}])]


def _real_plan_agent(backend, monkeypatch, tmp_path):
    """Use the real agent branch and final streamer with synthetic transport."""
    import core.agent as agent_module
    from core.agent import LuminaAgent
    from core.flight_recorder import FlightRecorder
    history, visible = [], []
    ctx = types.SimpleNamespace(
        history=history, turn_seq=0,
        add_user=lambda text, source="OWNER_DIRECT": history.append(
            {"role": "user", "content": text}),
        add_assistant=lambda text: history.append(
            {"role": "assistant", "content": text}),
        build_messages=lambda **kw: [{"role": "user", "content": "hi"}],
    )
    agent = types.SimpleNamespace(
        llm=backend, ctx=ctx, owner=False, channel_id="synthetic",
        registry=types.SimpleNamespace(
            schema_token_estimate=lambda: pytest.fail("schema estimate called"),
            get_schemas=lambda: pytest.fail("schemas dispatched")),
        on_response_token=visible.append,
        on_think_start=lambda step: None,
        on_think_token=lambda token: None,
        on_think_end=lambda: None,
        tts=None,
        flight_recorder=FlightRecorder(db_path=str(tmp_path / "fr.db")),
    )
    agent._stream_final = types.MethodType(LuminaAgent._stream_final, agent)
    monkeypatch.setattr(agent_module, "build_skills_block", lambda text: ("", ""))
    monkeypatch.setattr(agent_module, "_maybe_approve_pending_draft", lambda *a, **kw: None)
    monkeypatch.setattr(agent_module, "_run_tool_work_control_gate",
                        lambda *a, **kw: pytest.fail("control gate called"))
    return agent, history, visible


def _recorded_terminal_states(agent):
    import sqlite3
    with sqlite3.connect(agent.flight_recorder.db_path) as conn:
        rows = conn.execute("SELECT fields_json FROM events WHERE event_type=? ORDER BY seq",
                            ("provider.stream_terminal",)).fetchall()
    return [json.loads(row[0])["stream_completion_state"] for row in rows]


def _recorded_fields(agent, event_type):
    import sqlite3
    with sqlite3.connect(agent.flight_recorder.db_path) as conn:
        rows = conn.execute("SELECT fields_json FROM events WHERE event_type=? ORDER BY seq",
                            (event_type,)).fetchall()
    return [json.loads(row[0]) for row in rows]


def test_tool_free_completed_stream_persists_only_proven_final(transport, monkeypatch, tmp_path):
    from core.agent import LuminaAgent
    backend, _manager, calls, _catalog, _events = transport
    agent, history, visible = _real_plan_agent(backend, monkeypatch, tmp_path)
    result = LuminaAgent._chat_impl(agent, "hi")
    assert result == "Hello"
    assert history == [{"role": "user", "content": "hi"},
                       {"role": "assistant", "content": "Hello"}]
    assert visible == ["Hello"]
    assert [kind for kind, _url, _kw in calls] == ["get", "post"]
    assert _recorded_terminal_states(agent) == ["completed"]
    dispatched = _recorded_fields(agent, "provider.dispatch")
    assert len(dispatched) == 1
    assert dispatched[0]["backend_lane"] == "openai_chatgpt_plan"
    assert dispatched[0]["access_source"] == "oauth_subscription"
    terminal = _recorded_fields(agent, "provider.stream_terminal")[0]
    assert terminal["http_status"] == 200
    assert terminal["provider_request_id"] == "req-synthetic"


@pytest.mark.parametrize("terminal", [
    {"type": "response.failed", "response": {"status": "failed",
                                            "error": {"code": "usage_limit"}}},
    {"type": "response.incomplete", "response": {"status": "incomplete"}},
    b"[DONE]",
    ConnectionError("synthetic interruption"),
])
def test_tool_free_failed_stream_never_persists_final(transport, monkeypatch, tmp_path, terminal):
    from core.agent import LuminaAgent
    backend, _manager, calls, _catalog, _events = transport
    def post(url, **kw):
        calls.append(("post", url, kw))
        return FakeResponse(events=[{"type": "response.output_text.delta", "delta": "partial"}, terminal])
    monkeypatch.setattr("core.backends.openai_chatgpt_plan.requests.post", post)
    agent, history, visible = _real_plan_agent(backend, monkeypatch, tmp_path)
    result = LuminaAgent._chat_impl(agent, "hi")
    assert result.startswith("[Stream error:")
    assert history == [{"role": "user", "content": "hi"}]
    assert visible[0] == "partial"
    assert "partial" not in result
    expected = {"response.failed": "failed", "response.incomplete": "incomplete"}
    assert _recorded_terminal_states(agent) == [expected.get(terminal.get("type"), "none")
                                                if isinstance(terminal, dict) else "none"]


def test_tool_free_eof_mutant_cannot_promote_partial_to_final(transport, monkeypatch, tmp_path):
    from core.agent import LuminaAgent
    backend, _manager, _calls, _catalog, _events = transport
    backend.chat_stream = lambda **kwargs: iter(("partial",))
    agent, history, _visible = _real_plan_agent(backend, monkeypatch, tmp_path)
    result = LuminaAgent._chat_impl(agent, "hi")
    assert result.startswith("[Stream error:")
    assert history == [{"role": "user", "content": "hi"}]
    assert _recorded_terminal_states(agent) == ["none"]


def test_tool_free_cancellation_before_dispatch_preserves_user_without_request(transport, monkeypatch, tmp_path):
    import threading
    from core.agent import LuminaAgent, TurnCancelled
    backend, _manager, calls, _catalog, _events = transport
    agent, history, visible = _real_plan_agent(backend, monkeypatch, tmp_path)
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(TurnCancelled):
        LuminaAgent._chat_impl(agent, "hi", cancel_event=cancelled)
    assert history == [{"role": "user", "content": "hi"}]
    assert visible == []
    assert calls == []


def test_tool_free_cancellation_during_stream_closes_transport(transport, monkeypatch, tmp_path):
    import threading
    from core.agent import LuminaAgent, TurnCancelled
    backend, _manager, calls, _catalog, _events = transport
    response = FakeResponse(events=[
        {"type": "response.output_text.delta", "delta": "partial"},
        {"type": "response.completed", "response": {"status": "completed"}},
    ])
    monkeypatch.setattr("core.backends.openai_chatgpt_plan.requests.post",
                        lambda url, **kw: calls.append(("post", url, kw)) or response)
    agent, history, visible = _real_plan_agent(backend, monkeypatch, tmp_path)
    cancelled = threading.Event()
    agent.on_response_token = lambda token: (visible.append(token), cancelled.set())
    with pytest.raises(TurnCancelled):
        LuminaAgent._chat_impl(agent, "hi", cancel_event=cancelled)
    assert response.closed
    assert history[0] == {"role": "user", "content": "hi"}
    assert not _recorded_terminal_states(agent)
