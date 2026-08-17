"""core/backends/anthropic_backend.py -- chat_stream() UTF-8 mojibake bug.

Same root cause as gemini_backend.py's chat_stream() (see
test_gemini_stream_encoding.py for the full requests.Response.encoding
trace): Anthropic's streaming endpoint also sends
Content-Type: text/event-stream with no charset param, so
resp.iter_lines(decode_unicode=True) let requests fall back to decoding
the UTF-8 response bytes as ISO-8859-1, mangling any multi-byte character.
Fixed by reading raw bytes and decoding as UTF-8 explicitly.
"""
import json

import requests

from core.backends.anthropic_backend import AnthropicBackend


def _make_backend():
    backend = AnthropicBackend.__new__(AnthropicBackend)
    backend.api_key = "test-key"
    backend.default_model = "claude-sonnet-4-6"
    backend.headers = {"Content-Type": "application/json", "x-api-key": "test-key"}
    backend.timeout = 30
    return backend


class _FakeStreamResp:
    """Reproduces the bug: decode_unicode=True decodes with ISO-8859-1,
    matching what requests.Response.encoding actually resolves to for a
    charset-less text/event-stream response."""

    def __init__(self, byte_lines):
        self._byte_lines = byte_lines

    def raise_for_status(self):
        pass

    def iter_lines(self, decode_unicode=False):
        if decode_unicode:
            for line in self._byte_lines:
                yield line.decode("ISO-8859-1")
        else:
            yield from self._byte_lines


def _sse_lines(*events):
    lines = []
    for event in events:
        # ensure_ascii=False so non-ASCII text lands as real multi-byte
        # UTF-8 bytes on the wire, same as Anthropic's actual SSE stream.
        lines.append(f"data: {json.dumps(event, ensure_ascii=False)}".encode("utf-8"))
    lines.append(b"data: [DONE]")
    return lines


def test_stream_preserves_em_dash_and_curly_quotes_not_mangled_to_mojibake(monkeypatch):
    text = "high-stakes chicken—right around the time “we” started."
    events = [
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}},
        {"type": "message_stop"},
    ]
    monkeypatch.setattr(requests, "post", lambda *a, **kw: _FakeStreamResp(_sse_lines(*events)))

    backend = _make_backend()
    out = "".join(backend.chat_stream(messages=[{"role": "user", "content": "hi"}]))

    assert out == text
    assert "â" not in out  # tell-tale first byte of ISO-8859-1-mangled UTF-8


def test_stream_preserves_emoji_across_multibyte_boundary(monkeypatch):
    text = "shipping it \U0001f680 today"
    events = [
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}},
        {"type": "message_stop"},
    ]
    monkeypatch.setattr(requests, "post", lambda *a, **kw: _FakeStreamResp(_sse_lines(*events)))

    backend = _make_backend()
    out = "".join(backend.chat_stream(messages=[{"role": "user", "content": "hi"}]))

    assert out == text
