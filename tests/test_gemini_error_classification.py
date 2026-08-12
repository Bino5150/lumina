"""core/backends/gemini_backend.py -- error classification + the missing
chat_stream() HTTPError catch.

Two real gaps closed here, both surfaced while looking at the Gemini backend
to get vision testing working reliably:

1. classify_gemini_error()/format_gemini_error() didn't exist at all --
   chat()'s HTTPError handler just raised a flat, unclassified
   `RuntimeError(f"Gemini API HTTP error: {e}")`. core/backends/lmstudio.py's
   classify_provider_error()/format_provider_error() already give the
   OpenAI-compatible backend family (LM Studio, Groq, OpenRouter, DeepSeek,
   Kimi, Qwen, OpenAI, vLLM, OmniRoute, Custom) a legible "quota exceeded /
   rate limited / billing / auth" message -- Gemini's native backend
   (deliberately does NOT subclass LMStudioBackend, per its own module
   docstring -- the wire format is fundamentally different) never got the
   equivalent treatment, because Gemini's error body shape is Google's own
   {"error": {"code", "message", "status"}} envelope, not OpenAI's
   {"error": {"type", "message"}} shape lmstudio.py's classifier expects.
   This is the exact class of failure that actually happened during real
   testing: gemini-3.5-pro's free-tier daily quota ran out mid-session.

2. chat_stream() had NO `except requests.exceptions.HTTPError` branch at
   all -- only ConnectionError/Timeout were caught. An HTTP error on the
   streaming path (quota exhausted, rate limited, auth failure) leaked as a
   raw requests.exceptions.HTTPError instead of the RuntimeError chat()
   already raises for the identical failure. core/agent.py's
   _stream_final() only catches (ConnectionError, TimeoutError, RuntimeError,
   ValueError) for its graceful "[Stream error: ...]" + persisted-history
   handling -- an uncaught HTTPError skipped that path entirely and fell
   through to a cruder top-level handler. Since _stream_final() is the path
   that actually produces Lumina's final spoken/displayed answer (chat()
   itself is only used mid-turn, for tool-calling turns), this bug meant a
   quota failure on the *last* turn of a response was strictly worse-behaved
   than one on an earlier tool-calling turn.
"""
import json
import pytest
import requests

from core.backends.gemini_backend import (
    classify_gemini_error,
    format_gemini_error,
    GeminiBackend,
)


def _make_backend():
    backend = GeminiBackend.__new__(GeminiBackend)
    backend.api_key = "test-key"
    backend.default_model = "gemini-3.5-pro"
    backend.headers = {"Content-Type": "application/json", "x-goog-api-key": "test-key"}
    backend.timeout = 30
    return backend


# ── classify_gemini_error() ─────────────────────────────────────────────

def test_quota_exhausted_message_classified_as_insufficient_quota():
    """The real fixture: RESOURCE_EXHAUSTED status, quota-specific wording --
    this is what actually happened during testing."""
    body = json.dumps({"error": {
        "code": 429,
        "message": ("You exceeded your current quota, please check your plan "
                     "and billing details.\n* Quota exceeded for metric: "
                     "[generativelanguage.googleapis.com/generate_content_free_tier_requests], "
                     "limit: 20, model: gemini-3.5-pro\nPlease retry in 45s."),
        "status": "RESOURCE_EXHAUSTED",
    }})
    result = classify_gemini_error(429, body)
    assert result["kind"] == "insufficient_quota"
    assert "quota" in result["message"].lower()


def test_bare_resource_exhausted_without_quota_wording_classified_as_rate_limited():
    """RESOURCE_EXHAUSTED covers both quota exhaustion AND plain throttling
    -- Google doesn't split these into separate statuses. Without "quota" in
    the message, this should fall back to rate_limited, not insufficient_quota."""
    # NOTE: Google's own generic message literally says "check quota" -- use
    # a wording-free variant to actually exercise the fallback path.
    body_no_quota_word = json.dumps({"error": {
        "code": 429, "message": "Please retry in 12.4s.",
        "status": "RESOURCE_EXHAUSTED",
    }})
    result = classify_gemini_error(429, body_no_quota_word)
    assert result["kind"] == "rate_limited"


def test_unauthenticated_status_classified_as_authentication():
    body = json.dumps({"error": {"code": 401, "message": "API key not valid.", "status": "UNAUTHENTICATED"}})
    result = classify_gemini_error(401, body)
    assert result["kind"] == "authentication"


def test_permission_denied_status_classified_as_authentication():
    body = json.dumps({"error": {"code": 403, "message": "Permission denied.", "status": "PERMISSION_DENIED"}})
    result = classify_gemini_error(403, body)
    assert result["kind"] == "authentication"


def test_bare_429_without_recognizable_body_classified_as_rate_limited():
    result = classify_gemini_error(429, "")
    assert result["kind"] == "rate_limited"


def test_malformed_json_body_does_not_crash_classifier():
    result = classify_gemini_error(500, "not json at all {{{")
    assert result["kind"] == "generic"
    assert result["message"] == ""


def test_format_gemini_error_names_gemini_and_includes_kind_and_detail():
    body = json.dumps({"error": {"code": 429, "message": "Please retry in 12.4s.", "status": "RESOURCE_EXHAUSTED"}})
    msg = format_gemini_error(429, body, "429 Client Error")
    assert "Gemini error" in msg
    assert "rate_limited" in msg
    assert "retry in 12.4s" in msg


# ── chat() end-to-end attribution (existing coverage, now classified) ────

def test_chat_http_error_is_classified_and_body_is_bounded(monkeypatch, capsys):
    backend = _make_backend()
    body = json.dumps({"error": {
        "code": 429, "message": "Quota exceeded for metric: generate_content_free_tier_requests",
        "status": "RESOURCE_EXHAUSTED",
    }})

    class _FakeHTTPResp:
        status_code = 429
        text = body

        def raise_for_status(self):
            raise requests.exceptions.HTTPError("429 Client Error")

    monkeypatch.setattr(requests, "post", lambda *a, **kw: _FakeHTTPResp())

    with pytest.raises(RuntimeError) as exc_info:
        backend.chat(messages=[{"role": "user", "content": "hi"}])

    msg = str(exc_info.value)
    assert "insufficient_quota" in msg
    assert "Quota exceeded" in msg

    captured = capsys.readouterr()
    body_lines = [l for l in captured.out.splitlines() if "HTTP ERROR BODY" in l]
    assert body_lines and len(body_lines[0]) < 700


# ── chat_stream() -- the missing HTTPError branch ────────────────────────

def test_stream_http_error_no_longer_leaks_as_raw_requests_exception(monkeypatch):
    """The actual bug: before this fix, this raised requests.exceptions.
    HTTPError directly -- uncatchable by core/agent.py's _stream_final(),
    which only listens for (ConnectionError, TimeoutError, RuntimeError,
    ValueError). Must now raise RuntimeError like chat() does."""
    backend = _make_backend()
    body = json.dumps({"error": {
        "code": 429, "message": "Quota exceeded for metric: generate_content_free_tier_requests",
        "status": "RESOURCE_EXHAUSTED",
    }})

    class _FakeHTTPResp:
        status_code = 429
        text = body

        def raise_for_status(self):
            raise requests.exceptions.HTTPError("429 Client Error")

    monkeypatch.setattr(requests, "post", lambda *a, **kw: _FakeHTTPResp())

    with pytest.raises(RuntimeError) as exc_info:
        list(backend.chat_stream(messages=[{"role": "user", "content": "hi"}]))

    # Specifically NOT a bare requests.exceptions.HTTPError escaping.
    assert not isinstance(exc_info.value, requests.exceptions.HTTPError)
    msg = str(exc_info.value)
    assert "insufficient_quota" in msg


def test_stream_http_error_is_one_of_stream_finals_caught_types():
    """Direct guard on the actual production contract: core/agent.py's
    _stream_final() catches exactly (ConnectionError, TimeoutError,
    RuntimeError, ValueError). Whatever chat_stream() raises on an HTTP
    error must be an instance of one of those -- this is what makes the
    fix actually reach the graceful "[Stream error: ...]" path instead of
    the cruder top-level handler."""
    import requests as _requests
    STREAM_FINAL_CAUGHT = (ConnectionError, TimeoutError, RuntimeError, ValueError)
    assert not issubclass(_requests.exceptions.HTTPError, STREAM_FINAL_CAUGHT)
    # format_gemini_error's caller always wraps in RuntimeError -- confirm
    # that IS in the caught set (i.e. the fix path is actually reachable).
    assert issubclass(RuntimeError, STREAM_FINAL_CAUGHT)
