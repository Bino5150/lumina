"""core/backends/lmstudio.py — FE-15: a mid-stream disconnect
(requests.exceptions.ChunkedEncodingError, ConnectionError, etc.) raises
from *inside* resp.iter_lines(), outside chat_stream()'s connect-time
try/except. It's also not a subclass of the builtin ConnectionError /
TimeoutError that core/agent.py's _stream_final() catches, so it used to
escape both layers -- the turn's partial content got dropped from history
and a raw exception surfaced instead of the graceful "[Stream error: ...]"
path. _iter_lines_safe() converts it at the source.
"""
import json
import types
import pytest
import requests
from core.backends.lmstudio import (
    _iter_lines_safe,
    validate_base_url,
    join_endpoint,
    classify_provider_error,
    format_provider_error,
    extract_openai_compatible_reasoning,
    LMStudioBackend,
)
from core.backends.loader import CustomBackend


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


# ══════════════════════════════════════════════════════════════════════
# Bug C — LUMINA_HANDOFF_OMNIROUTE_TO_PROVIDER_FIXES_2026-08-02.md §8.3
# Regression tests per LUMINA_PROVIDER_BRIDGE_ADDENDUM_2026-08-01.md §13
# ══════════════════════════════════════════════════════════════════════

# ── URL validation & joining (addendum Tests 1-5) ────────────────────────

def test_url_joining_appends_chat_completions():
    base = validate_base_url("https://provider.example/v1", "Test")
    assert join_endpoint(base, "chat/completions") == "https://provider.example/v1/chat/completions"


def test_url_joining_strips_trailing_slash_first():
    base = validate_base_url("https://provider.example/v1/", "Test")
    assert join_endpoint(base, "chat/completions") == "https://provider.example/v1/chat/completions"


def test_url_joining_does_not_duplicate_suffix():
    base = validate_base_url("https://provider.example/v1/chat/completions", "Test")
    assert join_endpoint(base, "chat/completions") == "https://provider.example/v1/chat/completions"


def test_empty_base_url_raises_before_any_network_call():
    with pytest.raises(ValueError, match="no endpoint URL configured"):
        validate_base_url("", "Custom (OpenAI-compatible)")


def test_schemeless_base_url_raises_clear_missing_scheme_error():
    with pytest.raises(ValueError, match="missing a scheme"):
        validate_base_url("provider.example/v1", "Custom (OpenAI-compatible)")


def test_validation_error_names_the_actual_provider_not_lm_studio():
    """The whole point of Bug C item 2 -- an unconfigured custom endpoint
    must not claim to be an LM Studio problem."""
    with pytest.raises(ValueError) as exc_info:
        validate_base_url("", "Custom (OpenAI-compatible)")
    assert "LM Studio" not in str(exc_info.value)
    assert "Custom (OpenAI-compatible)" in str(exc_info.value)


# ── Error classification & attribution (addendum Tests 6-14) ────────────

def test_opencode_credits_error_classified_as_billing_required():
    body = json.dumps({"type": "error", "error": {
        "type": "CreditsError",
        "message": "No payment method. Add a payment method here: https://opencode.ai/workspace/x/billing",
    }})
    result = classify_provider_error(401, body)
    assert result["kind"] == "billing_required"
    assert "payment method" in result["message"].lower()


def test_quota_error_classified_as_insufficient_quota():
    """LongCat's quota fixture (addendum Test 7): the message-level 'quota'
    signal must win even though the HTTP status alone (402) would
    otherwise default to billing_required."""
    body = json.dumps({"error": {"type": "rate_limit_error", "message": "Token quota insufficient for this account"}})
    result = classify_provider_error(402, body)
    assert result["kind"] == "insufficient_quota"


def test_plain_401_without_recognizable_body_classified_as_authentication():
    result = classify_provider_error(401, "")
    assert result["kind"] == "authentication"


def test_429_classified_as_rate_limited():
    result = classify_provider_error(429, "")
    assert result["kind"] == "rate_limited"


def test_malformed_json_body_does_not_crash_classifier():
    result = classify_provider_error(500, "not json at all {{{")
    assert result["kind"] == "generic"
    assert result["message"] == ""


def test_format_provider_error_never_says_lm_studio_for_custom_provider():
    msg = format_provider_error("OpenCode Zen (Custom)", 401, json.dumps(
        {"error": {"type": "CreditsError", "message": "No payment method."}}
    ), "401 Client Error")
    assert "LM Studio" not in msg
    assert "OpenCode Zen (Custom)" in msg
    assert "billing_required" in msg
    assert "No payment method." in msg


def test_format_provider_error_falls_back_to_bounded_raw_body_preview():
    """If the body doesn't parse as {"error": {...}}, the raw body (bounded)
    should still be preserved rather than silently dropped -- addendum
    Test 10 / Test 24."""
    raw = "Internal Server Error: something broke upstream"
    msg = format_provider_error("Custom (OpenAI-compatible)", 500, raw, "500 Server Error")
    assert "something broke upstream" in msg


def test_custom_backend_chat_error_is_attributed_to_custom_not_lm_studio(monkeypatch):
    """End-to-end: CustomBackend inherits chat() from LMStudioBackend
    unchanged -- this is the actual mislabeling bug as it manifested in
    production (every custom-provider failure surfaced as 'LM Studio HTTP
    error')."""
    backend = CustomBackend.__new__(CustomBackend)
    backend.base_url = "https://opencode.ai/zen/v1"
    backend.headers = {"Content-Type": "application/json", "Authorization": "Bearer test"}
    backend._model = "big-pickle"

    class _FakeHTTPResp:
        status_code = 401
        text = json.dumps({"type": "error", "error": {
            "type": "CreditsError", "message": "No payment method."}})

        def raise_for_status(self):
            raise requests.exceptions.HTTPError("401 Client Error: Unauthorized")

        def json(self):
            return json.loads(self.text)

    monkeypatch.setattr(requests, "post", lambda *a, **kw: _FakeHTTPResp())

    with pytest.raises(RuntimeError) as exc_info:
        backend.chat(messages=[{"role": "user", "content": "hi"}])

    msg = str(exc_info.value)
    assert "LM Studio" not in msg
    assert "Custom (OpenAI-compatible)" in msg
    assert "billing_required" in msg
    assert "No payment method." in msg


def test_chat_http_error_body_print_is_bounded_not_unbounded(monkeypatch, capsys):
    """Patch-review gap: the console print of the raw provider body on an
    HTTP error was unbounded/unredacted, unlike the bounded raw-body
    preview format_provider_error() already uses for the raised exception
    message. Console-only, but an arbitrarily large or sensitive provider
    body still shouldn't dump in full."""
    backend = CustomBackend.__new__(CustomBackend)
    backend.base_url = "https://opencode.ai/zen/v1"
    backend.headers = {"Content-Type": "application/json", "Authorization": "Bearer test"}
    backend._model = "big-pickle"

    huge_body = json.dumps({"error": {"message": "x" * 5000}})

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
    assert body_lines, "expected an [HTTP ERROR BODY] log line"
    assert len(body_lines[0]) < 600


# ── SSE parser hardening against OpenCode's actual stream shape ─────────

def _sse_lines(*frames):
    """Build raw SSE 'data: ...' lines the way chat_stream() consumes them."""
    lines = []
    for f in frames:
        lines.append(f"data: {json.dumps(f)}".encode("utf-8"))
    lines.append(b"data: [DONE]")
    return lines


class _FakeStreamResp:
    def __init__(self, lines):
        self._lines = lines

    def raise_for_status(self):
        pass

    def iter_lines(self):
        yield from self._lines


def _make_backend():
    backend = LMStudioBackend.__new__(LMStudioBackend)
    backend.base_url = "https://opencode.ai/zen/v1"
    backend.headers = {"Content-Type": "application/json", "Authorization": "Bearer test"}
    backend._model = "big-pickle"
    return backend


def _make_custom_backend():
    """Uses CustomBackend, not LMStudioBackend, for attribution tests --
    LMStudioBackend legitimately IS named 'LM Studio'; the bug only shows
    up in subclasses (CustomBackend, OpenAIBackend, etc.) that inherit
    chat()/chat_stream() unchanged but have their own display_name."""
    backend = CustomBackend.__new__(CustomBackend)
    backend.base_url = "https://opencode.ai/zen/v1"
    backend.headers = {"Content-Type": "application/json", "Authorization": "Bearer test"}
    backend._model = "big-pickle"
    return backend


def test_stream_skips_empty_choices_metadata_frame_without_crashing(monkeypatch):
    """OpenCode's trailing frame: empty choices, top-level cost/usage --
    used to rely on IndexError-as-control-flow; now an explicit, cheap
    check with no exception involved."""
    backend = _make_backend()
    frames = [
        {"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]},
        {"choices": [], "cost": "0", "usage": {"total_tokens": 12}},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    monkeypatch.setattr(requests, "post", lambda *a, **kw: _FakeStreamResp(_sse_lines(*frames)))

    out = list(backend.chat_stream(messages=[{"role": "user", "content": "hi"}]))
    assert out == ["hi"]


def test_stream_reasoning_first_then_content_matches_opencode_shape(monkeypatch):
    """Reasoning-first chunks, then visible content, then a normal stop --
    the exact shape described for OpenCode/DeepSeek-V4-Flash.

    Asserts on joined content rather than exact chunk boundaries: the
    pre-existing <think>-tag lookahead buffer intentionally holds back the
    last few characters of a chunk until more content or finish_reason
    confirms no split tag is coming, so a short trailing chunk can
    legitimately arrive as two yields. That's correct, unrelated behavior --
    what Bug C cares about is that reasoning-first content is captured
    correctly and nothing is lost or garbled.
    """
    backend = _make_backend()
    frames = [
        {"choices": [{"delta": {"reasoning_content": "thinking..."}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "The answer is 42."}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    monkeypatch.setattr(requests, "post", lambda *a, **kw: _FakeStreamResp(_sse_lines(*frames)))

    out = list(backend.chat_stream(messages=[{"role": "user", "content": "hi"}]))
    assert out[0] == "__THINK_START__"
    assert out[1] == "thinking..."
    assert out[2] == "__THINK_END__"
    assert "".join(out[3:]) == "The answer is 42."


def test_stream_null_content_frames_are_skipped_not_crashed(monkeypatch):
    backend = _make_backend()
    frames = [
        {"choices": [{"delta": {"content": None}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": None}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "hello"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    monkeypatch.setattr(requests, "post", lambda *a, **kw: _FakeStreamResp(_sse_lines(*frames)))

    out = list(backend.chat_stream(messages=[{"role": "user", "content": "hi"}]))
    assert out == ["hello"]


def test_stream_non_string_content_shape_does_not_crash(monkeypatch):
    """Guards the exact 'Failure B' class from the debug handoff: a
    provider sending a structured (non-string) content value must be
    skipped, not concatenated into buffer and crash with a TypeError."""
    backend = _make_backend()
    frames = [
        {"choices": [{"delta": {"content": [{"type": "text", "text": "weird"}]}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "fine"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    monkeypatch.setattr(requests, "post", lambda *a, **kw: _FakeStreamResp(_sse_lines(*frames)))

    out = list(backend.chat_stream(messages=[{"role": "user", "content": "hi"}]))
    assert out == ["fine"]


def test_stream_malformed_frame_is_logged_and_skipped_not_crashed(monkeypatch, capsys):
    backend = _make_backend()
    lines = [
        b"data: {not valid json",
        f"data: {json.dumps({'choices': [{'delta': {'content': 'ok'}, 'finish_reason': None}]})}".encode(),
        f"data: {json.dumps({'choices': [{'delta': {}, 'finish_reason': 'stop'}]})}".encode(),
        b"data: [DONE]",
    ]
    monkeypatch.setattr(requests, "post", lambda *a, **kw: _FakeStreamResp(lines))

    out = list(backend.chat_stream(messages=[{"role": "user", "content": "hi"}]))
    assert out == ["ok"]

    captured = capsys.readouterr()
    assert "skipped malformed frame" in captured.out
    # Structure only -- never log the frame's own text content.
    assert "not valid json" not in captured.out


def test_stream_chat_http_error_is_attributed_to_actual_provider(monkeypatch):
    backend = _make_custom_backend()

    class _FakeHTTPResp:
        status_code = 402
        text = json.dumps({"error": {"message": "Token quota insufficient"}})

        def raise_for_status(self):
            raise requests.exceptions.HTTPError("402 Payment Required")

    monkeypatch.setattr(requests, "post", lambda *a, **kw: _FakeHTTPResp())

    with pytest.raises(RuntimeError) as exc_info:
        list(backend.chat_stream(messages=[{"role": "user", "content": "hi"}]))

    msg = str(exc_info.value)
    assert "LM Studio" not in msg
    assert "insufficient_quota" in msg


def test_empty_base_url_raises_before_stream_request_is_attempted(monkeypatch):
    """Addendum Test 16: empty utility/custom base URL must fail validation
    before any network call -- this is the exact 'Cannot reach LM Studio at
    : Invalid URL /models' symptom from the original bug report."""
    backend = _make_backend()
    backend.base_url = ""

    called = {"post": False}
    def _fail_if_called(*a, **kw):
        called["post"] = True
        raise AssertionError("requests.post should never be reached")
    monkeypatch.setattr(requests, "post", _fail_if_called)

    with pytest.raises(ValueError, match="no endpoint URL configured"):
        list(backend.chat_stream(messages=[{"role": "user", "content": "hi"}]))
    assert called["post"] is False


# ── extract_openai_compatible_reasoning() -- AGENT-TOOL-THINK-TELEMETRY-01A1 ─
#
# Field priority (reasoning_content, then reasoning, then thinking) and the
# "reasoning" field name specifically are anchored to the live OpenRouter/
# z-ai/glm-5.3-flash probe run for this ticket's Phase 0 gate -- a real
# non-streaming tool_calls-bearing response returned:
#   {"role": "assistant", "content": null, "refusal": null,
#    "reasoning": "<plain text>", "tool_calls": [...], "reasoning_details": [...]}
# and a second (control-primitive-shaped) probe on the same route returned
# "reasoning": null on an otherwise valid tool_calls-bearing turn -- absence
# on some valid turns is expected provider behavior, not a bug.

def _oai_response(message: dict, finish_reason: str = "tool_calls") -> dict:
    return {"choices": [{"message": message, "finish_reason": finish_reason}]}


def test_reasoning_extraction_prefers_reasoning_content_field():
    resp = _oai_response({"role": "assistant", "content": None,
                           "reasoning_content": "local-server style reasoning",
                           "reasoning": "should not be reached",
                           "tool_calls": []})
    assert extract_openai_compatible_reasoning(resp) == "local-server style reasoning"


def test_reasoning_extraction_falls_back_to_openrouter_reasoning_field():
    """Exact shape observed live against openrouter / z-ai/glm-5.3-flash --
    see this module's own comment above and the LMStudioBackend.
    extract_reasoning() docstring for the citation."""
    resp = _oai_response({
        "role": "assistant", "content": None, "refusal": None,
        "reasoning": "The user wants to know the weather in Tokyo. "
                      "I have a get_weather tool available.",
        "tool_calls": [{"type": "function", "function": {"name": "get_weather", "arguments": "{}"}}],
        "reasoning_details": [{"type": "reasoning.text", "text": "..."}],
    })
    assert extract_openai_compatible_reasoning(resp) == (
        "The user wants to know the weather in Tokyo. "
        "I have a get_weather tool available."
    )


def test_reasoning_extraction_falls_back_to_thinking_field():
    resp = _oai_response({"role": "assistant", "content": "", "thinking": "qwen-style thinking"})
    assert extract_openai_compatible_reasoning(resp) == "qwen-style thinking"


def test_reasoning_extraction_absent_on_valid_tool_call_turn_returns_none():
    """LIVE-VERIFIED shape: a genuine tool_calls-bearing response with no
    reasoning field present at all (openrouter / z-ai/glm-5.3-flash,
    control-gate-shaped probe, 2026-08-28)."""
    resp = _oai_response({
        "role": "assistant", "content": None, "refusal": None, "reasoning": None,
        "tool_calls": [{"type": "function", "function": {"name": "finish_tool_work", "arguments": "{}"}}],
    })
    assert extract_openai_compatible_reasoning(resp) is None


def test_reasoning_extraction_malformed_type_is_skipped_not_coerced():
    resp = _oai_response({"role": "assistant", "content": "",
                           "reasoning_content": {"nested": "object, not a string"}})
    assert extract_openai_compatible_reasoning(resp) is None


def test_reasoning_extraction_empty_string_treated_as_absent():
    resp = _oai_response({"role": "assistant", "content": "", "reasoning": "   "})
    assert extract_openai_compatible_reasoning(resp) is None


def test_reasoning_extraction_malformed_response_shape_returns_none_not_raise():
    assert extract_openai_compatible_reasoning({}) is None
    assert extract_openai_compatible_reasoning({"choices": []}) is None
    assert extract_openai_compatible_reasoning(None) is None


def test_lmstudio_backend_extract_reasoning_delegates_to_shared_helper():
    backend = _make_backend()
    resp = _oai_response({"role": "assistant", "content": "", "reasoning": "delegated"})
    assert backend.extract_reasoning(resp) == "delegated"


def test_lmstudio_backend_default_no_reasoning_is_none_not_empty_string():
    backend = _make_backend()
    resp = _oai_response({"role": "assistant", "content": "", "tool_calls": []})
    assert backend.extract_reasoning(resp) is None
