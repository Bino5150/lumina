"""core/backends/anthropic_backend.py -- error classification, bounded
error-body logging, and the missing chat_stream() HTTPError catch.

Same three gaps as gemini_backend.py (see tests/test_gemini_error_classification.py
for the full rationale) found while sweeping every backend that inherits
BaseLLMBackend directly instead of LMStudioBackend -- Anthropic is the other
one, alongside Gemini and Ollama:

1. No classify_anthropic_error()/format_anthropic_error() existed --
   chat()'s HTTPError handler raised a flat `RuntimeError(f"Anthropic API
   HTTP error: {e}")`. Anthropic's error body is actually the easiest of the
   three native backends to classify: a documented, stable
   {"type": "error", "error": {"type", "message"}} envelope with a fixed,
   published error.type enum (docs.claude.com/en/api/errors) -- no
   message-text guessing needed, just a direct lookup.
2. chat()'s HTTPError handler printed the raw, unbounded, unredacted
   resp.text straight to the console -- the same hygiene gap
   gemini_backend.py's equivalent had already fixed (bounded to 500 chars)
   before this pass.
3. chat_stream() had NO `except requests.exceptions.HTTPError` branch --
   identical bug to Gemini's: an HTTP error on the streaming path leaked as
   a raw requests.exceptions.HTTPError, uncatchable by core/agent.py's
   _stream_final() (which only catches ConnectionError/TimeoutError/
   RuntimeError/ValueError), skipping the graceful "[Stream error: ...]"
   handling entirely.
"""
import json
import pytest
import requests

from core.backends.anthropic_backend import (
    classify_anthropic_error,
    format_anthropic_error,
    AnthropicBackend,
)


def _make_backend():
    backend = AnthropicBackend.__new__(AnthropicBackend)
    backend.api_key = "test-key"
    backend.default_model = "claude-test"
    backend.headers = {
        "Content-Type": "application/json",
        "x-api-key": "test-key",
        "anthropic-version": "2023-06-01",
    }
    backend.timeout = 30
    return backend


# ── classify_anthropic_error() -- direct error.type lookup, no guessing ──

def test_rate_limit_error_classified_as_rate_limited():
    body = json.dumps({"type": "error", "error": {
        "type": "rate_limit_error", "message": "Number of requests has exceeded your rate limit."}})
    result = classify_anthropic_error(429, body)
    assert result["kind"] == "rate_limited"


def test_billing_error_classified_as_billing_required():
    body = json.dumps({"type": "error", "error": {
        "type": "billing_error", "message": "Your account has insufficient credit balance."}})
    result = classify_anthropic_error(402, body)
    assert result["kind"] == "billing_required"


def test_authentication_error_classified_as_authentication():
    body = json.dumps({"type": "error", "error": {
        "type": "authentication_error", "message": "invalid x-api-key"}})
    result = classify_anthropic_error(401, body)
    assert result["kind"] == "authentication"


def test_permission_error_classified_as_authentication():
    """Anthropic keeps billing_error and permission_error distinct (payment
    problem vs. this key can't use this resource), but both map to the same
    normalized "authentication" bucket here -- same treatment classify_
    provider_error() gives an invalid key, since both mean "this credential
    can't do this," just for different underlying reasons."""
    body = json.dumps({"type": "error", "error": {
        "type": "permission_error", "message": "Your API key does not have permission to use this resource."}})
    result = classify_anthropic_error(403, body)
    assert result["kind"] == "authentication"


def test_overloaded_error_falls_back_to_generic():
    """overloaded_error (529) isn't a credential/quota/rate-limit problem --
    it's Anthropic's own capacity, unrelated to this account. Correctly NOT
    classified as any of the actionable kinds."""
    body = json.dumps({"type": "error", "error": {
        "type": "overloaded_error", "message": "Overloaded"}})
    result = classify_anthropic_error(529, body)
    assert result["kind"] == "generic"


def test_malformed_json_body_does_not_crash_classifier():
    result = classify_anthropic_error(500, "not json at all {{{")
    assert result["kind"] == "generic"
    assert result["message"] == ""


def test_format_anthropic_error_names_anthropic_and_includes_kind_and_detail():
    body = json.dumps({"type": "error", "error": {
        "type": "rate_limit_error", "message": "Number of requests has exceeded your rate limit."}})
    msg = format_anthropic_error(429, body, "429 Client Error")
    assert "Anthropic error" in msg
    assert "rate_limited" in msg
    assert "exceeded your rate limit" in msg


# ── chat(): classification + bounded log line ─────────────────────────────

def test_chat_http_error_is_classified_and_log_line_is_bounded(monkeypatch, capsys):
    backend = _make_backend()
    huge_message = "x" * 5000
    body = json.dumps({"type": "error", "error": {"type": "rate_limit_error", "message": huge_message}})

    class _FakeHTTPResp:
        status_code = 429
        text = body

        def raise_for_status(self):
            raise requests.exceptions.HTTPError("429 Client Error")

    monkeypatch.setattr(requests, "post", lambda *a, **kw: _FakeHTTPResp())

    with pytest.raises(RuntimeError) as exc_info:
        backend.chat(messages=[{"role": "user", "content": "hi"}])

    assert "rate_limited" in str(exc_info.value)

    captured = capsys.readouterr()
    body_lines = [l for l in captured.out.splitlines() if "HTTP ERROR BODY" in l]
    assert body_lines, "expected an [HTTP ERROR BODY] log line"
    assert len(body_lines[0]) < 700, "log line must be bounded, not the full 5000-char body"


# ── chat_stream(): the missing HTTPError branch ───────────────────────────

def test_stream_http_error_no_longer_leaks_as_raw_requests_exception(monkeypatch):
    backend = _make_backend()
    body = json.dumps({"type": "error", "error": {
        "type": "rate_limit_error", "message": "Number of requests has exceeded your rate limit."}})

    class _FakeHTTPResp:
        status_code = 429
        text = body

        def raise_for_status(self):
            raise requests.exceptions.HTTPError("429 Client Error")

    monkeypatch.setattr(requests, "post", lambda *a, **kw: _FakeHTTPResp())

    with pytest.raises(RuntimeError) as exc_info:
        list(backend.chat_stream(messages=[{"role": "user", "content": "hi"}]))

    assert not isinstance(exc_info.value, requests.exceptions.HTTPError)
    assert "rate_limited" in str(exc_info.value)
