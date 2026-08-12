"""core/backends/ollama.py -- chat_stream() was missing the HTTPError catch
chat() already has, plus chat()'s existing error-body print was unbounded.

Same class of bug as gemini_backend.py and anthropic_backend.py (see
tests/test_gemini_error_classification.py for the full writeup) -- Ollama
is the third backend that inherits BaseLLMBackend directly instead of
LMStudioBackend. Unlike Gemini/Anthropic, Ollama is a local backend with no
quota/billing concept, so this fix stays unclassified (matching chat()'s
own pre-existing style) -- only the missing catch and the unbounded print
needed fixing here, not a new classifier.
"""
import pytest
import requests

from core.backends.ollama import OllamaBackend


def _make_backend():
    backend = OllamaBackend.__new__(OllamaBackend)
    backend.base_url = "http://localhost:11434/v1"
    backend.headers = {"Content-Type": "application/json"}
    backend._model = "llama3"
    return backend


def test_stream_http_error_no_longer_leaks_as_raw_requests_exception(monkeypatch):
    """The actual bug: before this fix, an HTTP error mid-connect on the
    streaming path (e.g. model not pulled -> 404, OOM -> 500) raised
    requests.exceptions.HTTPError directly -- uncatchable by core/agent.py's
    _stream_final(), which only listens for (ConnectionError, TimeoutError,
    RuntimeError, ValueError). Must now raise RuntimeError like chat() does
    for the identical failure."""
    backend = _make_backend()

    class _FakeHTTPResp:
        status_code = 404
        text = "model 'llama3' not found, try pulling it first"

        def raise_for_status(self):
            raise requests.exceptions.HTTPError("404 Client Error")

    monkeypatch.setattr(requests, "post", lambda *a, **kw: _FakeHTTPResp())

    with pytest.raises(RuntimeError) as exc_info:
        list(backend.chat_stream(messages=[{"role": "user", "content": "hi"}]))

    assert not isinstance(exc_info.value, requests.exceptions.HTTPError)
    assert "Ollama HTTP error" in str(exc_info.value)


def test_chat_http_error_body_print_is_bounded(monkeypatch, capsys):
    backend = _make_backend()
    huge_body = "x" * 5000

    class _FakeHTTPResp:
        status_code = 500
        text = huge_body

        def raise_for_status(self):
            raise requests.exceptions.HTTPError("500 Server Error")

    monkeypatch.setattr(requests, "post", lambda *a, **kw: _FakeHTTPResp())

    with pytest.raises(RuntimeError):
        backend.chat(messages=[{"role": "user", "content": "hi"}])

    captured = capsys.readouterr()
    body_lines = [l for l in captured.out.splitlines() if "HTTP ERROR BODY" in l]
    assert body_lines
    assert len(body_lines[0]) < 700
