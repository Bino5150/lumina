"""Account-bound stateless Plan continuation, using only fake OAuth and HTTP."""

import json
from types import SimpleNamespace

import pytest

from core.backends.openai_chatgpt_plan import ChatGPTPlanBackend


class FakeManager:
    def __init__(self):
        self.profile = "profile-a"
        self.identities = {"profile-a": ("issuer", "subject-a"),
                           "profile-b": ("issuer", "subject-b")}
        self.generation = 1
        self.token = "oauth-a-1"

    def get_valid_access_token(self):
        return SimpleNamespace(profile_id=self.profile,
                               account_identity=self.identities[self.profile],
                               generation=self.generation, access_token=self.token)

    def get_active_profile(self):
        return SimpleNamespace(profile_id=self.profile)

    def is_grant_current(self, grant):
        return (grant.profile_id == self.profile
                and grant.generation == self.generation)


class Response:
    ok = True
    status_code = 200
    headers = {"x-request-id": "synthetic"}

    def __init__(self, body):
        self.body = body

    def iter_lines(self):
        yield b"data: " + json.dumps({"type": "response.completed", "response": self.body}).encode()

    def close(self):
        pass


def completed(*items):
    return {"status": "completed", "output": list(items)}


def tool_schema():
    return [{"type": "function", "function": {"name": "get_time",
            "description": "Clock", "parameters": {"type": "object", "properties": {}}}}]


@pytest.fixture
def turn(monkeypatch, tmp_path):
    import config
    from core import persistence
    from tools.memory import create_chat, init_chat_db, save_chat_message
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "lumina.db"))
    init_chat_db()
    chat_id = create_chat("01L synthetic")
    save_chat_message(chat_id, "user", "Get time")
    manager = FakeManager()
    backend = ChatGPTPlanBackend(manager=manager)
    backend._model = "model-a"
    backend.set_chat_context(chat_id)
    backend._test_chat_id = chat_id
    seen = []
    bodies = [completed(
        {"type": "reasoning", "encrypted_content": "opaque-account-a"},
        {"type": "function_call", "namespace": "lumina", "name": "get_time",
         "call_id": "call-a", "arguments": "{}"})]

    def get(url, **kwargs):
        seen.append(("GET", kwargs))
        return SimpleNamespace(ok=True, json=lambda: {"models": [
            {"slug": model, "display_name": model, "visibility": "list"}
            for model in ("model-a", "model-b")]})

    def post(url, **kwargs):
        seen.append(("POST", kwargs))
        return Response(bodies.pop(0))

    monkeypatch.setattr("core.backends.openai_chatgpt_plan.requests.get", get)
    monkeypatch.setattr("core.backends.openai_chatgpt_plan.requests.post", post)
    user = {"role": "user", "content": "Get time"}
    first = backend.chat([user], tools=tool_schema())
    call = backend.extract_message(first)
    history = [user, call, {"role": "tool", "tool_call_id": "call-a",
                            "name": "get_time", "content": "synthetic-time"}]
    assert len([x for x in seen if x[0] == "POST"]) == 1
    return backend, manager, history, bodies, seen


def post_count(seen):
    return sum(method == "POST" for method, _ in seen)


def test_replaced_backend_turn_exit_retires_original_replay(monkeypatch, tmp_path):
    import config
    from core import persistence
    from core.agent import LuminaAgent

    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "lumina.db"))
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    agent = LuminaAgent(owner=False, backend="openai_chatgpt_plan")
    original = agent.llm
    original._response_replay["private-call"] = {"output": ["opaque-reasoning"]}
    original._turn_binding = ("profile", ("issuer", "subject"),
                              "openai_chatgpt_plan", "model")
    replacement = SimpleNamespace(clear_tool_turn_state=lambda: None)

    def swap_during_turn(self, *_args, **_kwargs):
        self.llm = replacement
        return "synthetic completion"

    monkeypatch.setattr(LuminaAgent, "_chat_impl", swap_during_turn)
    assert agent.chat("hello") == "synthetic completion"
    assert original._response_replay == {}
    assert original._turn_binding is None


def test_same_authenticated_account_token_renewal_continues(turn):
    backend, manager, history, bodies, seen = turn
    manager.generation += 1
    manager.token = "oauth-a-2"
    bodies.append(completed({"type": "message", "content": [
        {"type": "output_text", "text": "Done"}]}))
    backend.chat(history)
    assert post_count(seen) == 2
    assert seen[-1][1]["headers"]["Authorization"] == "Bearer oauth-a-2"
    assert seen[-1][1]["json"]["input"][1]["encrypted_content"] == "opaque-account-a"


def test_account_switch_refuses_before_authenticated_request(turn):
    backend, manager, history, _bodies, seen = turn
    manager.profile = "profile-b"
    manager.token = "oauth-b"
    with pytest.raises(RuntimeError, match="account changed"):
        backend.chat(history)
    assert post_count(seen) == 1
    assert sum(method == "GET" for method, _ in seen) == 1
    assert backend._response_replay == {}


def test_same_display_profile_with_changed_verified_subject_refuses(turn):
    backend, manager, history, _bodies, seen = turn
    manager.identities["profile-a"] = ("issuer", "subject-b")
    with pytest.raises(RuntimeError, match="account changed"):
        backend.chat(history)
    assert post_count(seen) == 1


def test_model_switch_refuses_before_replay(turn):
    backend, _manager, history, _bodies, seen = turn
    backend._model = "model-b"
    with pytest.raises(RuntimeError, match="model changed"):
        backend.chat(history)
    assert post_count(seen) == 1
    assert backend._response_replay == {}


def test_account_switch_refuses_final_completion_gate(turn):
    backend, manager, history, _bodies, seen = turn
    manager.profile = "profile-b"
    with pytest.raises(RuntimeError, match="account changed"):
        list(backend.chat_stream(history))
    assert post_count(seen) == 1


def test_model_switch_refuses_final_completion_gate(turn):
    backend, _manager, history, _bodies, seen = turn
    backend._model = "model-b"
    with pytest.raises(RuntimeError, match="model changed"):
        list(backend.chat_stream(history))
    assert post_count(seen) == 1


def test_switch_during_catalog_refuses_before_post(turn, monkeypatch):
    backend, manager, history, _bodies, seen = turn

    def switch_after_catalog(url, **kwargs):
        seen.append(("GET", kwargs))
        manager.profile = "profile-b"
        return SimpleNamespace(ok=True, json=lambda: {"models": [
            {"slug": "model-a", "display_name": "A", "visibility": "list"}]})

    monkeypatch.setattr("core.backends.openai_chatgpt_plan.requests.get", switch_after_catalog)
    with pytest.raises(RuntimeError, match="account changed"):
        backend.chat(history)
    assert post_count(seen) == 1
    assert backend._response_replay == {}


def test_detected_switch_cannot_resurrect_replay(turn):
    backend, manager, history, _bodies, seen = turn
    manager.profile = "profile-b"
    with pytest.raises(RuntimeError, match="account changed"):
        backend.chat(history)
    manager.profile = "profile-a"
    with pytest.raises(RuntimeError, match="account changed"):
        backend.chat(history)
    assert post_count(seen) == 1
    assert backend._response_replay == {}
    assert sum(method == "GET" for method, _ in seen) == 1


def test_lane_switch_refuses_before_replay(turn):
    backend, _manager, history, _bodies, seen = turn
    backend.name = "openai"
    with pytest.raises(RuntimeError, match="lane changed"):
        backend.chat(history)
    assert post_count(seen) == 1


def test_completed_turn_account_switch_cannot_send_old_history(turn):
    backend, manager, history, _bodies, seen = turn
    backend.clear_tool_turn_state()
    backend.set_chat_context(backend._test_chat_id)
    manager.profile = "profile-b"
    manager.token = "oauth-b"
    history.append({"role": "assistant", "content": "Earlier answer"})
    history.append({"role": "user", "content": "Follow up"})
    with pytest.raises(RuntimeError, match="account changed"):
        backend.chat(history)
    assert post_count(seen) == 1


def test_backend_recreation_still_refuses_bound_chat(turn):
    backend, manager, history, _bodies, seen = turn
    manager.profile = "profile-b"
    manager.token = "oauth-b"
    replacement = ChatGPTPlanBackend(manager=manager)
    replacement._model = "model-a"
    replacement.set_chat_context(backend._test_chat_id)
    with pytest.raises(RuntimeError, match="account changed"):
        replacement.chat(history)
    assert post_count(seen) == 1


def test_fresh_chat_can_bind_new_account(turn):
    from tools.memory import create_chat, save_chat_message
    backend, manager, _history, bodies, seen = turn
    backend.clear_tool_turn_state()
    manager.profile = "profile-b"
    manager.token = "oauth-b"
    chat_id = create_chat("01L new account")
    save_chat_message(chat_id, "user", "Hello")
    backend.set_chat_context(chat_id)
    bodies.append(completed({"type": "message", "content": [
        {"type": "output_text", "text": "Hello"}]}))
    backend.chat([{"role": "user", "content": "Hello"}])
    assert post_count(seen) == 2
    assert seen[-1][1]["headers"]["Authorization"] == "Bearer oauth-b"


def test_unbound_old_chat_refuses_before_catalog(turn):
    from tools.memory import create_chat, save_chat_message
    backend, _manager, _history, _bodies, seen = turn
    backend.clear_tool_turn_state()
    chat_id = create_chat("01L legacy")
    save_chat_message(chat_id, "user", "Earlier")
    save_chat_message(chat_id, "assistant", "Earlier reply")
    save_chat_message(chat_id, "user", "Follow up")
    backend.set_chat_context(chat_id)
    with pytest.raises(RuntimeError, match="unbound previous history"):
        backend.chat([{"role": "user", "content": "Earlier"},
                      {"role": "assistant", "content": "Earlier reply"},
                      {"role": "user", "content": "Follow up"}])
    assert post_count(seen) == 1
    assert sum(method == "GET" for method, _ in seen) == 1


def test_plan_bound_chat_refuses_other_backend_lane(turn):
    from core.agent import _dispatch_refusal
    from core.backend_identity import OperationKind, identity_of_backend
    backend, _manager, _history, _bodies, _seen = turn
    other = SimpleNamespace(name="openai", configured_model=lambda: "model-a")
    agent = SimpleNamespace(llm=other, _current_chat_id=backend._test_chat_id,
                            _turn_dispatch_identity=identity_of_backend(other))
    assert "Start a new chat" in _dispatch_refusal(agent, OperationKind.FOREGROUND_CHAT)


def test_no_id_backend_recreation_refuses_old_history(turn):
    backend, manager, history, _bodies, seen = turn
    backend.clear_tool_turn_state()
    manager.profile = "profile-b"
    replacement = ChatGPTPlanBackend(manager=manager)
    replacement._model = "model-a"
    with pytest.raises(RuntimeError, match="unbound previous history"):
        replacement.chat(history + [{"role": "user", "content": "Follow up"}])
    assert post_count(seen) == 1


def test_no_id_context_refuses_plan_to_other_lane():
    from core.agent import _dispatch_refusal
    from core.backend_identity import OperationKind, identity_of_backend
    ctx = SimpleNamespace(_plan_bound_without_chat=False)
    plan_backend = SimpleNamespace(name="openai_chatgpt_plan", configured_model=lambda: "model-a")
    agent = SimpleNamespace(llm=plan_backend, ctx=ctx, _current_chat_id=None,
                            _turn_dispatch_identity=identity_of_backend(plan_backend))
    assert _dispatch_refusal(agent, OperationKind.FOREGROUND_CHAT) is None
    assert ctx._plan_bound_without_chat is True
    other = SimpleNamespace(name="openai", configured_model=lambda: "model-a")
    agent.llm = other
    agent._turn_dispatch_identity = identity_of_backend(other)
    assert "Start a new chat" in _dispatch_refusal(agent, OperationKind.FOREGROUND_CHAT)
