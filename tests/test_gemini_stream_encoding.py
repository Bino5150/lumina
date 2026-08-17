"""core/backends/gemini_backend.py -- chat_stream() UTF-8 mojibake bug.

Gemini's streamGenerateContent endpoint (?alt=sse) sends
Content-Type: text/event-stream with no charset param. requests.Response
.encoding is populated by requests.utils.get_encoding_from_headers(), which
falls back to ISO-8859-1 for any "text/*" content-type lacking an explicit
charset (confirmed by reading requests/utils.py directly: the "text" in
content_type branch returns "ISO-8859-1" before the "application/json"
branch is even reached). chat_stream() used to call
resp.iter_lines(decode_unicode=True), which decodes the raw UTF-8 response
bytes with whatever codec resp.encoding names -- so every multi-byte UTF-8
character (em-dashes, curly quotes, accented letters, emoji) came out as
mojibake in Lumina's chat UI. The fix reads iter_lines() as raw bytes and
decodes each line as UTF-8 explicitly, bypassing the guess entirely.
"""
import json

import requests

from core.backends.gemini_backend import GeminiBackend


def _make_backend():
    backend = GeminiBackend.__new__(GeminiBackend)
    backend.api_key = "test-key"
    backend.default_model = "gemini-3.5-pro"
    backend.headers = {"Content-Type": "application/json", "x-goog-api-key": "test-key"}
    backend.timeout = 30
    return backend


class _FakeStreamResp:
    """Mimics real requests.Response behavior for a charset-less
    text/event-stream response closely enough to reproduce the bug: when
    decode_unicode=True is requested, lines are decoded with ISO-8859-1 --
    exactly what requests.Response.encoding resolves to for this
    Content-Type -- rather than the UTF-8 the bytes are actually encoded
    in."""

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


def _sse_line(text):
    frame = {"candidates": [{"content": {"parts": [{"text": text}]}}]}
    # ensure_ascii=False so non-ASCII characters land in the line as real
    # multi-byte UTF-8 bytes, not as \uXXXX escapes -- that's what Gemini's
    # actual wire format does, and it's the only way to exercise the
    # encoding bug at all.
    return f"data: {json.dumps(frame, ensure_ascii=False)}".encode("utf-8")


def test_stream_preserves_em_dash_and_curly_quotes_not_mangled_to_mojibake(monkeypatch):
    text = "high-stakes chicken—right around the time “we” started."
    lines = [_sse_line(text), b"data: [DONE]"]
    monkeypatch.setattr(requests, "post", lambda *a, **kw: _FakeStreamResp(lines))

    backend = _make_backend()
    out = "".join(backend.chat_stream(messages=[{"role": "user", "content": "hi"}]))

    assert out == text
    assert "â" not in out  # tell-tale first byte of ISO-8859-1-mangled UTF-8


def test_stream_preserves_emoji_across_multibyte_boundary(monkeypatch):
    text = "shipping it \U0001f680 today"
    lines = [_sse_line(text), b"data: [DONE]"]
    monkeypatch.setattr(requests, "post", lambda *a, **kw: _FakeStreamResp(lines))

    backend = _make_backend()
    out = "".join(backend.chat_stream(messages=[{"role": "user", "content": "hi"}]))

    assert out == text
