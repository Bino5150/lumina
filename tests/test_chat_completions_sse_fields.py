"""Offline wire fixtures for both shared Chat Completions stream readers.

Use requests.Response.iter_lines itself so network fragmentation (including
splits inside field names and UTF-8 text) is exercised before backend parsing.
"""
import json

import pytest
import requests

from core.backends.base import BackendStreamTelemetry
from core.backends.loader import CustomBackend


USAGE = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}


def _frame(delta=None, finish_reason=None, **extra):
    return {"choices": [{"delta": delta or {}, "finish_reason": finish_reason}], **extra}


def _response(monkeypatch, frames, space, chunk_size, newline="\n", failure=None):
    data = newline.join([
        ": keepalive", "event: message", "database: ignored", "",
        *["data:" + space + (json.dumps(f, ensure_ascii=False) if isinstance(f, dict) else f)
          + newline for f in frames], "",
    ]).encode("utf-8")
    response = requests.Response()
    response.status_code = 200
    response.url = "https://provider.example/v1/chat/completions"

    def iter_content(chunk_size=None, decode_unicode=False):
        for start in range(0, len(data), fragment_size):
            yield data[start:start + fragment_size]
        if failure:
            raise failure

    fragment_size = chunk_size
    response.iter_content = iter_content
    monkeypatch.setattr("core.backends.lmstudio.requests.post", lambda *a, **kw: response)


def _backend():
    backend = CustomBackend.__new__(CustomBackend)
    backend.base_url = "https://provider.example/v1"
    backend.headers = {"Authorization": "Bearer placeholder"}
    backend._model = "fixture-model"
    return backend


@pytest.mark.parametrize("space", ["", " "])
@pytest.mark.parametrize("chunk_size", [1, 7, 65536])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_regular_stream_keeps_text_terminal_and_trailing_usage(monkeypatch, space, chunk_size, newline):
    _response(monkeypatch, [
        "{malformed", _frame({"content": "Hello 世界"}),
        _frame(finish_reason="stop"),
        _frame({"content": "must not leak after terminal"}),
        {"choices": [], "usage": USAGE}, "[DONE]",
        _frame({"content": "must not leak after DONE"}),
    ], space, chunk_size, newline)
    events = list(_backend().chat_stream([{"role": "user", "content": "fixture"}]))
    assert "".join(e for e in events if isinstance(e, str)) == "Hello 世界"
    telemetry = [e for e in events if isinstance(e, BackendStreamTelemetry)]
    assert len(telemetry) == 1
    assert telemetry[0].fields["usage"] == USAGE


@pytest.mark.parametrize("space", ["", " "])
@pytest.mark.parametrize("chunk_size", [1, 7, 65536])
def test_telemetry_assembly_keeps_fragmented_tools_reasoning_and_usage(monkeypatch, space, chunk_size):
    _response(monkeypatch, [
        "{malformed", _frame({"reasoning_content": "Checking"}),
        _frame({"content": "Hello 世界"}),
        _frame({"tool_calls": [{"index": 0, "id": "call_fixture", "type": "function",
                                "function": {"name": "lookup", "arguments": '{"q":'}}]}),
        _frame({"tool_calls": [{"index": 0, "function": {"arguments": '"test"}'}}]}),
        _frame(finish_reason="tool_calls"),
        _frame({"content": "must not leak", "tool_calls": [{"index": 1}]},
               finish_reason="tool_calls", usage=USAGE),
        "[DONE]", _frame({"content": "after DONE"}),
    ], space, chunk_size)
    body = _backend().chat([{"role": "user", "content": "fixture"}], capture_telemetry=True)
    message = body["choices"][0]["message"]
    assert message["content"] == "Hello 世界"
    assert message["reasoning_content"] == "Checking"
    assert message["tool_calls"] == [{"id": "call_fixture", "type": "function",
        "function": {"name": "lookup", "arguments": '{"q":"test"}'}}]
    assert body["choices"][0]["finish_reason"] == "tool_calls"
    assert body["usage"] == USAGE
    assert body["_lumina_telemetry"]["usage"] == USAGE


@pytest.mark.parametrize("space", ["", " "])
@pytest.mark.parametrize("telemetry", [False, True])
def test_transport_interruption_still_surfaces(monkeypatch, space, telemetry):
    _response(monkeypatch, [_frame({"content": "partial content before disconnect"})],
              space, 1, failure=requests.exceptions.ChunkedEncodingError("fixture disconnect"))
    with pytest.raises(ConnectionError, match="stream interrupted"):
        if telemetry:
            _backend().chat([{"role": "user", "content": "fixture"}], capture_telemetry=True)
        else:
            list(_backend().chat_stream([{"role": "user", "content": "fixture"}]))


@pytest.mark.parametrize("space", ["", " "])
def test_closing_consumer_does_not_read_more_frames(monkeypatch, space):
    """The consumer can still cancel by closing the existing generator."""
    reads = []

    class Response:
        def raise_for_status(self):
            pass

        def iter_lines(self):
            reads.append("first")
            yield ("data:" + space + json.dumps(_frame({"content": "first answer long enough to yield"}))).encode()
            reads.append("second")
            yield ("data:" + space + "[DONE]").encode()

    monkeypatch.setattr("core.backends.lmstudio.requests.post", lambda *a, **kw: Response())
    stream = _backend().chat_stream([{"role": "user", "content": "fixture"}])
    assert next(stream)
    stream.close()
    assert reads == ["first"]
