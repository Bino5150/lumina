"""Synthetic ChatGPT Plan namespace tools and existing agent dispatch path."""

import json
import threading
import types

import pytest

from core.backends.openai_chatgpt_plan import ChatGPTPlanBackend, _plan_tools
from tools.registry import ToolRegistry


class Response:
    status_code = 200
    ok = True
    headers = {"x-request-id": "req-synthetic"}

    def __init__(self, body):
        self.body = body
        self.closed = False

    def iter_lines(self):
        yield b"data: " + json.dumps({"type": "response.completed", "response": self.body}).encode()

    def close(self):
        self.closed = True


@pytest.fixture
def plan(monkeypatch, tmp_path):
    from core import persistence
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    manager = types.SimpleNamespace(
        get_valid_access_token=lambda: types.SimpleNamespace(
            profile_id="owner", generation=1,
            account_identity=("issuer", "owner"), access_token="synthetic-oauth-token"),
        get_active_profile=lambda: types.SimpleNamespace(profile_id="owner"),
        is_grant_current=lambda grant: grant.profile_id == "owner",
    )
    backend = ChatGPTPlanBackend(manager=manager)
    backend._model = "synthetic-model"
    requests_seen = []
    bodies = []

    def get(url, **kwargs):
        requests_seen.append(("get", url, kwargs))
        return types.SimpleNamespace(ok=True, json=lambda: {"models": [
            {"slug": "synthetic-model", "display_name": "Synthetic", "visibility": "list"}]})

    def post(url, **kwargs):
        requests_seen.append(("post", url, kwargs))
        return Response(bodies.pop(0))

    monkeypatch.setattr("core.backends.openai_chatgpt_plan.requests.get", get)
    monkeypatch.setattr("core.backends.openai_chatgpt_plan.requests.post", post)
    return backend, requests_seen, bodies


def schema(name):
    return {"type": "function", "function": {"name": name,
            "description": "Synthetic local action", "parameters": {"type": "object", "properties": {}}}}


def call(name="alpha", call_id="call-one", args="{}", **extra):
    item = {"type": "function_call", "namespace": "lumina", "name": name,
            "call_id": call_id, "arguments": args}
    item.update(extra)
    return item


def completed(*output):
    return {"status": "completed", "output": list(output)}


class StreamResponse(Response):
    def __init__(self, events):
        super().__init__({})
        self.events = events

    def iter_lines(self):
        for event in self.events:
            yield b"data: " + json.dumps(event).encode()


def streamed_item(index, item, *, added=False):
    return {"type": "response.output_item.added" if added else "response.output_item.done",
            "output_index": index, "item": item}


def test_01h_empty_terminal_output_reconstructs_text_reply(plan, monkeypatch):
    backend, _seen, _bodies = plan
    text = "I can see one offered native tool."
    fragments = ["I ", "can ", "see ", "one ", "offered ", "native ", "tool", "."]
    assert "".join(fragments) == text
    added = {"type": "message", "id": "msg-01h", "status": "in_progress",
             "role": "assistant", "content": []}
    done = {"type": "message", "id": "msg-01h", "status": "completed",
            "role": "assistant", "content": [{"type": "output_text", "text": text}]}
    events = [streamed_item(0, added, added=True)]
    events += [{"type": "response.output_text.delta", "item_id": "msg-01h",
                "output_index": 0, "content_index": 0, "delta": part} for part in fragments]
    events += [streamed_item(0, done), {"type": "response.completed", "response": completed()}]
    monkeypatch.setattr("core.backends.openai_chatgpt_plan.requests.post",
                        lambda *a, **kw: StreamResponse(events))
    response = backend.chat([{"role": "user", "content": "what tools?"}], tools=[schema("alpha")])
    assert backend.extract_message(response)["content"] == text
    assert backend.get_tool_calls(backend.extract_message(response)) == []


def test_01f_empty_terminal_output_reconstructs_native_call(plan, monkeypatch):
    backend, _seen, _bodies = plan
    pending = call(call_id="id-stream", id="fc-stream", args="", status="in_progress")
    finished = call(call_id="id-stream", id="fc-stream", args="{}", status="completed")
    events = [streamed_item(0, pending, added=True),
              {"type": "response.function_call_arguments.delta", "item_id": "fc-stream",
               "output_index": 0, "delta": "{}"},
              {"type": "response.function_call_arguments.done", "item_id": "fc-stream",
               "output_index": 0, "arguments": "{}"},
              streamed_item(0, finished),
              {"type": "response.completed", "response": completed()}]
    monkeypatch.setattr("core.backends.openai_chatgpt_plan.requests.post",
                        lambda *a, **kw: StreamResponse(events))
    response = backend.chat([{"role": "user", "content": "work"}], tools=[schema("alpha")])
    assert backend.get_tool_calls(backend.extract_message(response)) == [
        {"id": "id-stream", "type": "function",
         "function": {"name": "alpha", "arguments": "{}"}}]


def test_01h_empty_terminal_output_preserves_streamed_final(plan, monkeypatch):
    backend, _seen, _bodies = plan
    fragments = ["This", " ", "turn", " ", "offers", " ", "one", " ",
                 "native", " ", "tool", ":", " alpha", "."]
    text = "".join(fragments)
    added = {"type": "message", "id": "msg-01h", "status": "in_progress",
             "role": "assistant", "content": []}
    done = {"type": "message", "id": "msg-01h", "status": "completed",
            "role": "assistant", "content": [{"type": "output_text", "text": text}]}
    events = [{"type": "response.created"}, {"type": "response.in_progress"},
              streamed_item(0, added, added=True),
              {"type": "response.content_part.added", "item_id": "msg-01h",
               "output_index": 0, "content_index": 0,
               "part": {"type": "output_text", "text": ""}}]
    events.extend({"type": "response.output_text.delta", "item_id": "msg-01h",
                   "output_index": 0, "content_index": 0, "delta": part} for part in fragments)
    events.extend([{"type": "response.output_text.done", "item_id": "msg-01h",
                    "output_index": 0, "content_index": 0, "text": text},
                   {"type": "response.content_part.done", "item_id": "msg-01h",
                    "output_index": 0, "content_index": 0,
                    "part": {"type": "output_text", "text": text}},
                   streamed_item(0, done),
                   {"type": "response.completed", "response": completed()}])
    monkeypatch.setattr("core.backends.openai_chatgpt_plan.requests.post",
                        lambda *a, **kw: StreamResponse(events))
    chunks = list(backend.chat_stream([{"role": "user", "content": "what tools?"}]))
    assert "".join(chunk for chunk in chunks if isinstance(chunk, str)) == text
    assert chunks[-1].fields["stream_completion_state"] == "completed"


def test_streamed_calls_and_interleaved_encrypted_reasoning_replay_in_order(plan, monkeypatch):
    backend, _seen, _bodies = plan
    first = call(call_id="id-A", id="fc-A", status="completed")
    reasoning = {"type": "reasoning", "id": "rs-R", "status": "completed",
                 "encrypted_content": "opaque-R"}
    second = call(call_id="id-B", id="fc-B", status="completed")
    items = [first, reasoning, second]
    first_events = []
    for index, item in enumerate(items):
        pending = dict(item, status="in_progress")
        if item["type"] == "reasoning":
            pending.pop("encrypted_content")
        else:
            pending["arguments"] = ""
        first_events.extend([streamed_item(index, pending, added=True),
                             streamed_item(index, item)])
    first_events.append({"type": "response.completed", "response": completed()})
    second_events = [{"type": "response.completed", "response": completed(
        {"type": "message", "content": [{"type": "output_text", "text": "Done"}]})}]
    queued = [StreamResponse(first_events), StreamResponse(second_events)]
    posts = []
    def post(_url, **kwargs):
        posts.append(kwargs["json"])
        return queued.pop(0)
    monkeypatch.setattr("core.backends.openai_chatgpt_plan.requests.post", post)
    user = {"role": "user", "content": "work"}
    response = backend.chat([user], tools=[schema("alpha")])
    message = backend.extract_message(response)
    assert [tool["id"] for tool in message["tool_calls"]] == ["id-A", "id-B"]
    assert "opaque-R" not in json.dumps(response)
    history = [user, message,
               {"role": "tool", "tool_call_id": "id-A", "content": "result A"},
               {"role": "tool", "tool_call_id": "id-B", "content": "result B"}]
    backend.chat(history, tools=[schema("alpha")])
    assert posts[1]["input"] == [user, first, reasoning, second,
                                 {"type": "function_call_output", "call_id": "id-A", "output": "result A"},
                                 {"type": "function_call_output", "call_id": "id-B", "output": "result B"}]


def test_stream_items_replay_by_output_index_even_if_done_events_arrive_out_of_order(plan, monkeypatch):
    backend, _seen, _bodies = plan
    first = call(call_id="id-A", id="fc-A", status="completed")
    second = call(call_id="id-B", id="fc-B", status="completed")
    events = [streamed_item(0, dict(first, status="in_progress"), added=True),
              streamed_item(1, dict(second, status="in_progress"), added=True),
              streamed_item(1, second), streamed_item(0, first),
              {"type": "response.completed", "response": completed(first, second)}]
    monkeypatch.setattr("core.backends.openai_chatgpt_plan.requests.post",
                        lambda *a, **kw: StreamResponse(events))
    response = backend.chat([{"role": "user", "content": "work"}], tools=[schema("alpha")])
    assert [tool["id"] for tool in backend.get_tool_calls(backend.extract_message(response))] == ["id-A", "id-B"]


@pytest.mark.parametrize("damage", ["wrong_index", "changed_id", "changed_call_id",
                                     "duplicate_added", "duplicate_done", "wrong_argument_item_id",
                                     "arguments_mismatch", "arguments_without_done",
                                     "terminal_mismatch", "unfinished_item"])
def test_stream_item_integrity_refuses_malformed_or_duplicate_items(plan, monkeypatch, damage):
    backend, _seen, _bodies = plan
    pending = call(call_id="id-one", id="fc-one", args="", status="in_progress")
    finished = call(call_id="id-one", id="fc-one", args="{}", status="completed")
    events = [streamed_item(0, pending, added=True)]
    if damage == "wrong_index":
        events.append(streamed_item(1, finished))
    elif damage == "changed_id":
        events.append(streamed_item(0, dict(finished, id="fc-other")))
    elif damage == "changed_call_id":
        events.append(streamed_item(0, dict(finished, call_id="different")))
    elif damage == "duplicate_added":
        events.append(streamed_item(0, pending, added=True))
    elif damage == "duplicate_done":
        events.extend([streamed_item(0, finished), streamed_item(0, finished)])
    elif damage == "wrong_argument_item_id":
        events.extend([{"type": "response.function_call_arguments.delta", "item_id": "fc-other",
                        "output_index": 0, "delta": "{}"}, streamed_item(0, finished)])
    elif damage == "arguments_mismatch":
        events.extend([{"type": "response.function_call_arguments.delta", "item_id": "fc-one",
                        "output_index": 0, "delta": '{"x":1}'},
                       {"type": "response.function_call_arguments.done", "item_id": "fc-one",
                        "output_index": 0, "arguments": '{"x":1}'},
                       streamed_item(0, finished)])
    elif damage == "arguments_without_done":
        events.extend([{"type": "response.function_call_arguments.delta", "item_id": "fc-one",
                        "output_index": 0, "delta": "{}"}, streamed_item(0, finished)])
    elif damage == "terminal_mismatch":
        events.extend([streamed_item(0, finished),
                       {"type": "response.completed", "response": completed(dict(finished, name="other"))}])
    elif damage == "unfinished_item":
        pass
    if not events[-1]["type"] == "response.completed":
        events.append({"type": "response.completed", "response": completed()})
    monkeypatch.setattr("core.backends.openai_chatgpt_plan.requests.post",
                        lambda *a, **kw: StreamResponse(events))
    with pytest.raises(ValueError):
        backend.chat([{"role": "user", "content": "work"}], tools=[schema("alpha")])


def test_streamed_message_role_change_and_non_assistant_body_are_refused(plan, monkeypatch):
    backend, _seen, _bodies = plan
    pending = {"type": "message", "id": "msg-one", "role": "assistant",
               "status": "in_progress", "content": []}
    changed = dict(pending, role="user", status="completed")
    events = [streamed_item(0, pending, added=True), streamed_item(0, changed),
              {"type": "response.completed", "response": completed()}]
    responses = [StreamResponse(events), Response(completed(
        {"type": "message", "role": "user", "content": []}))]
    monkeypatch.setattr("core.backends.openai_chatgpt_plan.requests.post",
                        lambda *a, **kw: responses.pop(0))
    with pytest.raises(ValueError):
        backend.chat([{"role": "user", "content": "hi"}])
    with pytest.raises(ValueError):
        backend.chat([{"role": "user", "content": "hi"}])


@pytest.mark.parametrize("terminal", ["response.failed", "response.incomplete"])
def test_streamed_items_cannot_escape_failed_or_incomplete_terminal(plan, monkeypatch, terminal):
    backend, _seen, _bodies = plan
    item = call(call_id="id-one", id="fc-one", status="completed")
    events = [streamed_item(0, dict(item, status="in_progress"), added=True),
              streamed_item(0, item),
              {"type": terminal, "response": {"status": terminal.removeprefix("response.")}}]
    monkeypatch.setattr("core.backends.openai_chatgpt_plan.requests.post",
                        lambda *a, **kw: StreamResponse(events))
    with pytest.raises(RuntimeError):
        backend.chat([{"role": "user", "content": "work"}], tools=[schema("alpha")])
    assert backend._response_replay == {}


@pytest.mark.parametrize("terminal", ["response.failed", "response.incomplete"])
def test_text_deltas_without_successful_terminal_never_complete_final(plan, monkeypatch, terminal):
    backend, _seen, _bodies = plan
    pending = {"type": "message", "id": "msg-one", "status": "in_progress",
               "role": "assistant", "content": []}
    done = {"type": "message", "id": "msg-one", "status": "completed",
            "role": "assistant", "content": [{"type": "output_text", "text": "Partial"}]}
    events = [streamed_item(0, pending, added=True),
              {"type": "response.output_text.delta", "item_id": "msg-one", "output_index": 0,
               "content_index": 0, "delta": "Partial"},
              streamed_item(0, done),
              {"type": terminal, "response": {"status": terminal.removeprefix("response.")}}]
    monkeypatch.setattr("core.backends.openai_chatgpt_plan.requests.post",
                        lambda *a, **kw: StreamResponse(events))
    with pytest.raises(RuntimeError):
        list(backend.chat_stream([{"role": "user", "content": "hello"}]))


def test_enabled_registry_namespace_and_oauth_payload(plan):
    backend, requests_seen, bodies = plan
    registry = ToolRegistry()
    registry.register("alpha", lambda: "ok", "Synthetic local action", {"type": "object", "properties": {}})
    registry.register("disabled", lambda: "bad", "Disabled", {"type": "object", "properties": {}})
    registry.disable("disabled")
    bodies.append(completed(call()))
    response = backend.chat([{"role": "system", "content": "Trusted"},
                             {"role": "user", "content": "Do the work"}], tools=registry.get_schemas())
    assert response["choices"][0]["message"]["tool_calls"] == [
        {"id": "call-one", "type": "function", "function": {"name": "alpha", "arguments": "{}"}}]
    post = [entry for entry in requests_seen if entry[0] == "post"][0]
    assert post[1] == "https://api.openai.com/v1/responses"
    assert post[2]["headers"] == {"Content-Type": "application/json",
                                  "Authorization": "Bearer synthetic-oauth-token"}
    assert post[2]["json"] == {"model": "synthetic-model", "instructions": "Trusted",
        "input": [{"role": "user", "content": "Do the work"}], "store": False, "stream": True,
        "tools": [{"type": "namespace", "name": "lumina",
                   "description": "Lumina's enabled local tools", "tools": [
                       {"type": "function", "name": "alpha", "description": "Synthetic local action",
                        "parameters": {"type": "object", "properties": {}}}]}]}
    assert "disabled" not in json.dumps(post[2]["json"])


@pytest.mark.parametrize("schemas", [
    [schema("alpha"), schema("alpha")], [schema("bad.name")],
    [{"type": "function", "function": {"name": "alpha", "description": "d", "parameters": []}}],
    [{"type": "custom", "name": "alpha"}],
])
def test_schema_collision_and_malformed_refused_before_dispatch(plan, schemas):
    backend, seen, _ = plan
    with pytest.raises(ValueError):
        backend.chat([{"role": "user", "content": "hi"}], tools=schemas)
    assert not [entry for entry in seen if entry[0] == "post"]


@pytest.mark.parametrize("item", [
    call(name="unknown"), call(call_id=""), call(args="[1]"), call(args="{bad"),
    call(args='{"x":1,"x":2}'), call(args='{"x":NaN}'), call(args='{"x":1e999}'),
    call(namespace="other"), call(status="incomplete"), call(**{"async": True}),
])
def test_unknown_or_malformed_call_refused(plan, item):
    backend, _seen, bodies = plan
    bodies.append(completed(item))
    with pytest.raises(ValueError):
        backend.chat([{"role": "user", "content": "hi"}], tools=[schema("alpha")])


def test_duplicate_call_ids_and_prose_are_not_dispatchable(plan):
    backend, _seen, bodies = plan
    bodies.append(completed(call(), call(call_id="call-one")))
    with pytest.raises(ValueError):
        backend.chat([{"role": "user", "content": "hi"}], tools=[schema("alpha")])
    bodies.append(completed({"type": "message", "content": [
        {"type": "output_text", "text": 'Call alpha with {"x":1}'}]}))
    response = backend.chat([{"role": "user", "content": "hi"}], tools=[schema("alpha")])
    assert backend.get_tool_calls(backend.extract_message(response)) == []


def test_oversized_plan_call_batch_refused_before_any_dispatch(plan):
    backend, _seen, bodies = plan
    bodies.append(completed(*(call(call_id=f"id-{index}") for index in range(33))))
    with pytest.raises(ValueError, match="safe limit"):
        backend.chat([{"role": "user", "content": "hi"}], tools=[schema("alpha")])


def test_multiple_calls_and_opaque_stateless_continuation(plan):
    backend, seen, bodies = plan
    reasoning = {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque-ciphertext",
                 "summary": [{"type": "summary_text", "text": "brief summary"}]}
    bodies.append(completed(reasoning, call(call_id="id-A"), call("beta", "id-B", '{"x":2}')))
    user = {"role": "user", "content": "work"}
    first = backend.chat([user], tools=[schema("alpha"), schema("beta")])
    message = backend.extract_message(first)
    assert [tc["id"] for tc in message["tool_calls"]] == ["id-A", "id-B"]
    assert "opaque-ciphertext" not in json.dumps(first)
    history = [user, message,
               {"role": "tool", "tool_call_id": "id-A", "content": "result A"},
               {"role": "tool", "tool_call_id": "id-B", "content": "result B"}]
    bodies.append(completed({"type": "message", "content": [
        {"type": "output_text", "text": "Done"}]}))
    second = backend.chat(history, tools=[schema("alpha"), schema("beta")])
    assert backend.extract_message(second)["content"] == "Done"
    payload = [entry for entry in seen if entry[0] == "post"][1][2]["json"]
    assert payload["input"] == [
        {"role": "user", "content": "work"}, reasoning,
        {"type": "function_call", "call_id": "id-A", "name": "alpha", "arguments": "{}", "namespace": "lumina"},
        {"type": "function_call", "call_id": "id-B", "name": "beta", "arguments": '{"x":2}', "namespace": "lumina"},
        {"type": "function_call_output", "call_id": "id-A", "output": "result A"},
        {"type": "function_call_output", "call_id": "id-B", "output": "result B"}]


def test_interleaved_output_items_keep_provider_order_and_identity(plan):
    backend, seen, bodies = plan
    first_call = call(call_id="id-A", id="fc-A")
    reasoning = {"type": "reasoning", "id": "rs-between", "encrypted_content": "opaque-between"}
    second_call = call(call_id="id-B", id="fc-B")
    commentary = {"type": "message", "id": "msg-after", "content": [
        {"type": "output_text", "text": "Working"}]}
    bodies.append(completed(first_call, reasoning, second_call, commentary))
    user = {"role": "user", "content": "work"}
    first = backend.chat([user], tools=[schema("alpha")])
    message = backend.extract_message(first)
    assert [tc["id"] for tc in message["tool_calls"]] == ["id-A", "id-B"]
    assert "opaque-between" not in json.dumps(first)
    history = [user, message,
               {"role": "tool", "tool_call_id": "id-A", "content": "result A"},
               {"role": "tool", "tool_call_id": "id-B", "content": "result B"}]
    bodies.append(completed({"type": "message", "content": [
        {"type": "output_text", "text": "Done"}]}))
    backend.chat(history, tools=[schema("alpha")])
    payload = [entry[2]["json"] for entry in seen if entry[0] == "post"][1]
    assert payload["input"] == [user, first_call, reasoning, second_call, commentary,
                                {"type": "function_call_output", "call_id": "id-A", "output": "result A"},
                                {"type": "function_call_output", "call_id": "id-B", "output": "result B"}]
    assert payload["input"][2]["id"] == "rs-between"
    assert payload["input"][1]["id"] == "fc-A"
    assert payload["input"][3]["id"] == "fc-B"


def test_single_call_replay_and_changed_call_history_refused(plan):
    backend, seen, bodies = plan
    reasoning = {"type": "reasoning", "id": "rs-one", "encrypted_content": "opaque-one"}
    original_call = call(call_id="id-one", id="fc-one")
    bodies.append(completed(reasoning, original_call))
    user = {"role": "user", "content": "work"}
    first = backend.chat([user], tools=[schema("alpha")])
    message = backend.extract_message(first)
    history = [user, message, {"role": "tool", "tool_call_id": "id-one", "content": "result"}]
    bodies.append(completed({"type": "message", "content": [
        {"type": "output_text", "text": "Done"}]}))
    backend.chat(history, tools=[schema("alpha")])
    posts = [entry[2]["json"] for entry in seen if entry[0] == "post"]
    assert posts[1]["input"] == [user, reasoning, original_call,
                                 {"type": "function_call_output", "call_id": "id-one", "output": "result"}]
    message["tool_calls"][0]["function"]["arguments"] = '{"changed":true}'
    with pytest.raises(ValueError, match="cached function-call history changed"):
        backend.chat(history, tools=[schema("alpha")])
    assert len([entry for entry in seen if entry[0] == "post"]) == 2


def test_previous_call_id_refused_and_no_unauthorized_credential_fallback(plan):
    backend, seen, bodies = plan
    bodies.append(completed({"type": "message", "content": [
        {"type": "output_text", "text": "Earlier"}]}))
    backend.chat([{"role": "user", "content": "Earlier"}])
    bodies.append(completed(call(call_id="old")))
    messages = [{"role": "user", "content": "hi"},
                {"role": "assistant", "tool_calls": [{"id": "old", "function": {
                    "name": "alpha", "arguments": "{}"}}]}]
    with pytest.raises(ValueError):
        backend.chat(messages, tools=[schema("alpha")])
    assert [entry for entry in seen if entry[0] == "post"][0][2]["headers"]["Authorization"] == "Bearer synthetic-oauth-token"


@pytest.mark.parametrize("terminal", ["response.failed", "response.incomplete"])
def test_failed_or_incomplete_stream_never_returns_calls(plan, monkeypatch, terminal):
    backend, _seen, _bodies = plan
    class BadResponse(Response):
        def iter_lines(self):
            yield b"data: " + json.dumps({"type": terminal, "response": {"status": "failed"}}).encode()
    monkeypatch.setattr("core.backends.openai_chatgpt_plan.requests.post",
                        lambda *a, **kw: BadResponse({}))
    with pytest.raises(RuntimeError):
        backend.chat([{"role": "user", "content": "work"}], tools=[schema("alpha")])


def test_second_terminal_event_is_ambiguous_and_refused(plan, monkeypatch):
    backend, _seen, _bodies = plan
    class AmbiguousResponse(Response):
        def iter_lines(self):
            for body in (completed(call()), completed(call(call_id="id-two"))):
                yield b"data: " + json.dumps({"type": "response.completed", "response": body}).encode()
    monkeypatch.setattr("core.backends.openai_chatgpt_plan.requests.post",
                        lambda *a, **kw: AmbiguousResponse({}))
    with pytest.raises(RuntimeError, match="after completion"):
        backend.chat([{"role": "user", "content": "work"}], tools=[schema("alpha")])


def test_final_stream_requires_text_confirmed_by_completed_body(plan, monkeypatch):
    backend, _seen, _bodies = plan
    class MismatchResponse(Response):
        def iter_lines(self):
            yield b'data: {"type":"response.output_text.delta","delta":"partial"}'
            yield b"data: " + json.dumps({"type": "response.completed", "response": completed(
                {"type": "message", "content": [{"type": "output_text", "text": "different"}]})}).encode()
    monkeypatch.setattr("core.backends.openai_chatgpt_plan.requests.post",
                        lambda *a, **kw: MismatchResponse({}))
    with pytest.raises(RuntimeError, match="did not match"):
        list(backend.chat_stream([{"role": "user", "content": "hi"}]))


def test_real_agent_work_loop_dispatches_sequentially_and_uses_registry_gates(plan, monkeypatch):
    from core.agent import LuminaAgent
    import core.agent as agent_module

    backend, seen, bodies = plan
    executed = []
    registry = ToolRegistry()
    registry.register("alpha", lambda: executed.append("alpha") or "A", "Synthetic local action",
                      {"type": "object", "properties": {}})
    registry.register("beta", lambda: executed.append("beta") or "B", "Synthetic local action",
                      {"type": "object", "properties": {}})
    registry.register("owner_only", lambda: executed.append("owner_only") or "bad", "Owner only",
                      {"type": "object", "properties": {}})
    registry.disable("owner_only")  # non-owner profile, applied before schema selection
    registry.set_gate(lambda name: (name != "beta", "PIN required"))
    history = []
    outputs = []
    ctx = types.SimpleNamespace(
        history=history, turn_seq=0,
        add_user=lambda content, source="OWNER_DIRECT": history.append({"role": "user", "content": content}),
        add_assistant=lambda content: history.append({"role": "assistant", "content": content}),
        add_tool_call=lambda message: history.append(message),
        add_tool_result=lambda call_id, name, result: history.append(
            {"role": "tool", "tool_call_id": call_id, "name": name, "content": result}),
        add_cancelled_tool_result=lambda call_id, name: history.append(
            {"role": "tool", "tool_call_id": call_id, "name": name, "content": "cancelled"}),
        push_ephemeral=lambda block: None,
        build_messages=lambda **kwargs: [{"role": "system", "content": "Trusted"}] + list(history),
    )
    agent = types.SimpleNamespace(
        llm=backend, ctx=ctx, registry=registry, owner=False, channel_id="synthetic",
        on_tool_call=lambda name, args: None, on_tool_result=lambda name, result: None,
        on_think_start=lambda step: None, on_think_token=lambda text: None,
        on_think_end=lambda: None, on_response_token=outputs.append, tts=None,
        _session_tool_calls=0, _skill_nudge_sent=False,
    )
    agent._stream_final = types.MethodType(LuminaAgent._stream_final, agent)
    agent._finalize_completion_candidate = types.MethodType(LuminaAgent._finalize_completion_candidate, agent)
    agent._finalize_with_reconciliation = types.MethodType(LuminaAgent._finalize_with_reconciliation, agent)
    monkeypatch.setattr(agent_module, "build_skills_block", lambda text: ("", ""))
    monkeypatch.setattr(agent_module, "_maybe_approve_pending_draft", lambda *a, **kw: None)
    bodies.extend([
        completed(call(call_id="id-A"), call("beta", "id-B")),
        completed({"type": "message", "content": [{"type": "output_text", "text": "Complete"}]}),
        completed(call("finish_tool_work", "gate-id")),
    ])
    result = LuminaAgent.chat(agent, "work")
    assert result == "Complete"
    assert executed == ["alpha"]
    assert [m["tool_call_id"] for m in history if m["role"] == "tool"] == ["id-A", "id-B"]
    assert "PIN required" in history[3]["content"]
    payloads = [entry[2]["json"] for entry in seen if entry[0] == "post"]
    assert "owner_only" not in json.dumps(payloads[0])
    assert [t["name"] for t in payloads[2]["tools"][0]["tools"]] == [
        "continue_tool_work", "finish_tool_work"]
    assert backend._response_replay == {}
    assert history[-1] == {"role": "assistant", "content": "Complete"}
    assert len([m for m in history if m["role"] == "assistant" and m.get("content")]) == 1


def test_cancellation_between_plan_calls_closes_remaining_id_without_dispatch(plan, monkeypatch):
    from core.agent import LuminaAgent, TurnCancelled
    import core.agent as agent_module

    backend, seen, bodies = plan
    cancelled = threading.Event()
    executed = []
    registry = ToolRegistry()
    registry.register("alpha", lambda: executed.append("alpha") or "A", "Synthetic local action",
                      {"type": "object", "properties": {}})
    registry.register("beta", lambda: executed.append("beta") or "B", "Synthetic local action",
                      {"type": "object", "properties": {}})
    history = []
    ctx = types.SimpleNamespace(
        history=history, turn_seq=0,
        add_user=lambda content, source="OWNER_DIRECT": history.append({"role": "user", "content": content}),
        add_assistant=lambda content: history.append({"role": "assistant", "content": content}),
        add_tool_call=lambda message: history.append(message),
        add_tool_result=lambda call_id, name, result: history.append(
            {"role": "tool", "tool_call_id": call_id, "name": name, "content": result}),
        add_cancelled_tool_result=lambda call_id, name: history.append(
            {"role": "tool", "tool_call_id": call_id, "name": name, "content": "cancelled"}),
        push_ephemeral=lambda block: None,
        build_messages=lambda **kwargs: [{"role": "system", "content": "Trusted"}] + list(history),
    )
    agent = types.SimpleNamespace(
        llm=backend, ctx=ctx, registry=registry, owner=True, channel_id="synthetic",
        on_tool_call=lambda name, args: None,
        on_tool_result=lambda name, result: cancelled.set(),
        on_think_start=lambda step: None, on_think_token=lambda text: None,
        on_think_end=lambda: None, on_response_token=lambda text: None, tts=None,
        _session_tool_calls=0, _skill_nudge_sent=False,
    )
    monkeypatch.setattr(agent_module, "build_skills_block", lambda text: ("", ""))
    monkeypatch.setattr(agent_module, "_maybe_approve_pending_draft", lambda *a, **kw: None)
    bodies.append(completed(call(call_id="id-A"), call("beta", "id-B")))

    with pytest.raises(TurnCancelled):
        LuminaAgent.chat(agent, "work", cancel_event=cancelled)

    assert executed == ["alpha"]
    assert [m["tool_call_id"] for m in history if m["role"] == "tool"] == ["id-A", "id-B"]
    assert history[-1]["content"] == "cancelled"
    assert not any(m["role"] == "assistant" and m.get("content") for m in history)
    assert len([entry for entry in seen if entry[0] == "post"]) == 1
    assert backend._response_replay == {}
