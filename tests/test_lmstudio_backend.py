"""core/backends/lmstudio.py — FE-15: a mid-stream disconnect
(requests.exceptions.ChunkedEncodingError, ConnectionError, etc.) raises
from *inside* resp.iter_lines(), outside chat_stream()'s connect-time
try/except. It's also not a subclass of the builtin ConnectionError /
TimeoutError that core/agent.py's _stream_final() catches, so it used to
escape both layers -- the turn's partial content got dropped from history
and a raw exception surfaced instead of the graceful "[Stream error: ...]"
path. _iter_lines_safe() converts it at the source.
"""
import pytest
import requests
from core.backends.lmstudio import _iter_lines_safe


class _FakeResp:
    """Minimal stand-in for a requests.Response in streaming mode."""
    def __init__(self, lines, fail_after=None, fail_with=None):
        self._lines = lines
        self._fail_after = fail_after
        self._fail_with = fail_with

    def iter_lines(self):
        for i, line in enumerate(self._lines):
            if self._fail_after is not None and i == self._fail_after:
                raise self._fail_with
            yield line


def test_normal_iteration_passes_through_unchanged():
    resp = _FakeResp([b"a", b"b", b"c"])
    assert list(_iter_lines_safe(resp)) == [b"a", b"b", b"c"]


def test_mid_stream_disconnect_converts_to_builtin_connection_error():
    resp = _FakeResp(
        [b"a", b"b"], fail_after=1,
        fail_with=requests.exceptions.ChunkedEncodingError("connection broken"),
    )
    with pytest.raises(ConnectionError):
        list(_iter_lines_safe(resp))


def test_lines_yielded_before_the_disconnect_are_not_lost():
    resp = _FakeResp(
        [b"a", b"b", b"c"], fail_after=2,
        fail_with=requests.exceptions.ConnectionError("reset by peer"),
    )
    seen = []
    with pytest.raises(ConnectionError):
        for line in _iter_lines_safe(resp):
            seen.append(line)
    assert seen == [b"a", b"b"]


def test_unrelated_exceptions_are_not_mislabeled():
    """Only requests.exceptions.RequestException gets converted -- an
    unrelated bug in the caller shouldn't get relabeled as a stream/
    connection issue and silently caught by _stream_final()'s handler."""
    resp = _FakeResp([b"a"], fail_after=0, fail_with=ValueError("unrelated bug"))
    with pytest.raises(ValueError):
        list(_iter_lines_safe(resp))
