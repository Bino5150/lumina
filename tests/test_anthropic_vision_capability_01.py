"""
ANTHROPIC-VISION-CAPABILITY-01 -- honest Anthropic vision contract.

Root cause (source-vetted, confirmed via the pre-existing documented-not-
fixed test this ticket closes -- see test_vision_multi_image_01.py's
former test_anthropic_backend_does_not_translate_image_blocks_pre_existing_gap,
now updated): AnthropicBackend._translate_messages()'s plain user/assistant
branch passed `content` straight through unconverted --
`{"role": role, "content": m.get("content", "")}` -- so an OpenAI-shaped
image_url block reached Anthropic's /v1/messages endpoint verbatim instead
of Anthropic's own {"type": "image", "source": {...}} shape, failing late
with a generic provider HTTP error. Predates multi-image support entirely
(already true for a single image); VISION-MULTI-IMAGE-01 deliberately left
it out of scope and pointed at this ticket to fix.

Authoritative provider contract verified via platform.claude.com/docs
(fetched live for this ticket, not assumed from memory):
  - Image content block: {"type": "image", "source": {"type": "base64",
    "media_type": "image/jpeg"|"image/png"|"image/gif"|"image/webp",
    "data": "<base64>"}}
  - Supported formats: JPEG, PNG, GIF, WebP only.
  - Vision shipped with the Claude 3 family (2024-03); every model
    released since supports it. Only the pre-Claude-3 legacy family
    (claude-1, claude-2, claude-2.1, claude-instant-1, claude-instant-1.2)
    predates it.

Fix: BaseLLMBackend gains supports_vision(model) (default False, same
fail-safe posture as every other capability method in this family).
AnthropicBackend overrides it with a denylist of the known pre-vision
legacy id prefixes (not an allowlist -- a brand-new, not-yet-catalogued
Claude model defaults to the historically accurate "supported" answer
instead of being incorrectly rejected merely for being unrecognized).
_translate_content_blocks() correctly translates image_url blocks,
validating and raising ValueError on a malformed data URL or unsupported
media type rather than silently dropping or sending a doomed request (a
deliberately stricter contract than GeminiBackend._parts_from_content()'s
established silent-skip precedent for the same shape -- justified here
because this ticket explicitly requires catching bad attachments before
dispatch). _reject_if_vision_unsupported() fails closed BEFORE any network
call or message translation when vision content is present but the
configured model doesn't support it, with a specific, honest,
user-facing message; core/agent.py's existing _provider_chat_or_error()
already surfaces any chat()-raised exception as "[Lumina error: ...]"
with zero new agent-side plumbing needed.
"""
import copy

import pytest

from core.backends.anthropic_backend import AnthropicBackend
from core.backends.base import BaseLLMBackend
from core.context import ContextManager


def _tiny_backend(model="claude-sonnet-4-6"):
    backend = AnthropicBackend.__new__(AnthropicBackend)
    backend.api_key = "test-key"
    backend.default_model = model
    backend.headers = {
        "Content-Type": "application/json",
        "x-api-key": "test-key",
        "anthropic-version": "2023-06-01",
    }
    backend.timeout = 60
    return backend


_PNG_DATA = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
_IMAGE = {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_PNG_DATA}"}}
_IMAGE_2 = {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{_PNG_DATA}"}}


# ── supports_vision() -- base default + AnthropicBackend override ──────────

def test_base_backend_default_is_false_for_any_model():
    class _Bare(BaseLLMBackend):
        def get_model(self): return "m"
        def list_models(self): return ["m"]
        def health_check(self): return True, "ok"
        def chat(self, *a, **k): return {}
        def chat_stream(self, *a, **k): yield ""
    b = _Bare()
    assert b.supports_vision("any-model") is False
    assert b.supports_vision(None) is False


@pytest.mark.parametrize("model", [
    "claude-opus-4-7", "claude-sonnet-4-6", "claude-sonnet-5",
    "claude-haiku-4-5-20251001",
])
def test_known_configured_models_all_support_vision(model):
    backend = _tiny_backend(model)
    assert backend.supports_vision(model) is True


@pytest.mark.parametrize("model", [
    "claude-2", "claude-2.1", "claude-1", "claude-instant-1", "claude-instant-1.2",
])
def test_legacy_pre_claude_3_models_do_not_support_vision(model):
    backend = _tiny_backend(model)
    assert backend.supports_vision(model) is False


def test_unrecognized_future_model_defaults_to_supported_not_rejected():
    """Denylist, not allowlist: a brand-new Claude id this codebase has
    never catalogued must default to the historically accurate answer
    (vision supported) rather than being incorrectly rejected merely for
    being unrecognized."""
    backend = _tiny_backend("claude-opus-6")
    assert backend.supports_vision("claude-opus-6") is True


def test_none_model_returns_false():
    backend = _tiny_backend()
    assert backend.supports_vision(None) is False


# ── _translate_content_blocks() -- pure translation + validation ───────────

def test_plain_string_content_passes_through_unchanged():
    assert AnthropicBackend._translate_content_blocks("hello") == "hello"


def test_single_image_translates_to_anthropic_shape():
    content = [_IMAGE]
    result = AnthropicBackend._translate_content_blocks(content)
    assert result == [{
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": _PNG_DATA},
    }]


def test_multi_image_translation_and_ordering():
    content = [_IMAGE, _IMAGE_2, {"type": "text", "text": "compare these"}]
    result = AnthropicBackend._translate_content_blocks(content)
    assert result == [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _PNG_DATA}},
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": _PNG_DATA}},
        {"type": "text", "text": "compare these"},
    ]


def test_text_image_interleaving_preserves_order():
    content = [
        {"type": "text", "text": "first, look at this:"},
        _IMAGE,
        {"type": "text", "text": "now this one:"},
        _IMAGE_2,
    ]
    result = AnthropicBackend._translate_content_blocks(content)
    assert [b["type"] for b in result] == ["text", "image", "text", "image"]
    assert result[0] == {"type": "text", "text": "first, look at this:"}
    assert result[2] == {"type": "text", "text": "now this one:"}


def test_empty_caption_text_block_is_skipped_not_sent_as_empty_block():
    content = [{"type": "text", "text": ""}, _IMAGE]
    result = AnthropicBackend._translate_content_blocks(content)
    assert result == [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _PNG_DATA}}]


def test_invalid_data_url_no_data_prefix_raises():
    content = [{"type": "image_url", "image_url": {"url": "not-a-data-url"}}]
    with pytest.raises(ValueError, match="not a valid data URL"):
        AnthropicBackend._translate_content_blocks(content)


def test_invalid_data_url_no_comma_separator_raises():
    content = [{"type": "image_url", "image_url": {"url": "data:image/png;base64"}}]
    with pytest.raises(ValueError, match="not a valid data URL"):
        AnthropicBackend._translate_content_blocks(content)


def test_unsupported_mime_type_raises():
    content = [{"type": "image_url", "image_url": {"url": "data:image/bmp;base64,AAAA"}}]
    with pytest.raises(ValueError, match="does not support the image type"):
        AnthropicBackend._translate_content_blocks(content)


def test_svg_mime_type_raises():
    """Not in Anthropic's documented allowlist (JPEG/PNG/GIF/WebP only)."""
    content = [{"type": "image_url", "image_url": {"url": "data:image/svg+xml;base64,AAAA"}}]
    with pytest.raises(ValueError, match="does not support the image type"):
        AnthropicBackend._translate_content_blocks(content)


def test_empty_base64_payload_raises():
    content = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,"}}]
    with pytest.raises(ValueError, match="no base64 payload"):
        AnthropicBackend._translate_content_blocks(content)


def test_all_four_supported_formats_translate_cleanly():
    for mime in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        content = [{"type": "image_url", "image_url": {"url": f"data:{mime};base64,AAAA"}}]
        result = AnthropicBackend._translate_content_blocks(content)
        assert result == [{"type": "image", "source": {"type": "base64", "media_type": mime, "data": "AAAA"}}]


def test_does_not_mutate_the_original_content_list_or_blocks():
    block = dict(_IMAGE)
    content = [block]
    AnthropicBackend._translate_content_blocks(content)
    assert content == [block]
    assert content[0] is block
    assert block["image_url"]["url"] == _IMAGE["image_url"]["url"]


def test_unrecognized_block_type_silently_skipped_unchanged_from_before():
    """Audio/other non-text/non-image blocks were never handled here
    either way -- no new validation added for a shape this method never
    claimed to support."""
    content = [{"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}},
               {"type": "text", "text": "what did I say?"}]
    result = AnthropicBackend._translate_content_blocks(content)
    assert result == [{"type": "text", "text": "what did I say?"}]


# ── _translate_messages() end-to-end (tool/plain turns unaffected) ─────────

def test_translate_messages_translates_a_plain_vision_user_turn():
    messages = [{"role": "user", "content": [_IMAGE, {"type": "text", "text": "what is this?"}]}]
    translated = AnthropicBackend._translate_messages(messages)
    assert translated == [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _PNG_DATA}},
        {"type": "text", "text": "what is this?"},
    ]}]


def test_translate_messages_text_only_turn_unaffected():
    messages = [{"role": "user", "content": "plain text"}]
    assert AnthropicBackend._translate_messages(messages) == [{"role": "user", "content": "plain text"}]


def test_translate_messages_tool_call_turn_unaffected():
    messages = [{"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]}]
    translated = AnthropicBackend._translate_messages(messages)
    assert translated[0]["content"] == [{"type": "tool_use", "id": "call_1", "name": "read_file", "input": {}}]


def test_translate_messages_tool_result_turn_unaffected():
    messages = [{"role": "tool", "tool_call_id": "call_1", "name": "read_file", "content": "file contents"}]
    translated = AnthropicBackend._translate_messages(messages)
    assert translated[0]["content"][0]["type"] == "tool_result"


# ── _reject_if_vision_unsupported() -- fail-closed capability gate ─────────

def test_reject_raises_for_unsupported_model_with_vision_content():
    backend = _tiny_backend("claude-2.1")
    messages = [{"role": "user", "content": [_IMAGE, {"type": "text", "text": "hi"}]}]
    with pytest.raises(ValueError, match="does not support image input"):
        backend._reject_if_vision_unsupported(messages)


def test_reject_does_not_raise_for_supported_model_with_vision_content():
    backend = _tiny_backend("claude-sonnet-4-6")
    messages = [{"role": "user", "content": [_IMAGE, {"type": "text", "text": "hi"}]}]
    backend._reject_if_vision_unsupported(messages)  # must not raise


def test_reject_does_not_raise_for_unsupported_model_without_vision_content():
    backend = _tiny_backend("claude-2.1")
    messages = [{"role": "user", "content": "plain text, no images"}]
    backend._reject_if_vision_unsupported(messages)  # must not raise


def test_reject_error_message_names_the_model_and_backend():
    backend = _tiny_backend("claude-2.1")
    messages = [{"role": "user", "content": [_IMAGE]}]
    with pytest.raises(ValueError) as exc_info:
        backend._reject_if_vision_unsupported(messages)
    assert "claude-2.1" in str(exc_info.value)
    assert "Anthropic" in str(exc_info.value)


def test_reject_error_message_never_contains_raw_base64_payload():
    """No raw image payload in logs or diagnostics -- bait the payload
    with a distinctive, large fake base64 string and confirm it never
    surfaces in the raised message."""
    bait = "X" * 5000
    backend = _tiny_backend("claude-2.1")
    messages = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{bait}"}}]}]
    with pytest.raises(ValueError) as exc_info:
        backend._reject_if_vision_unsupported(messages)
    assert bait not in str(exc_info.value)


# ── Wire-level: chat()/chat_stream() reject BEFORE any network dispatch ────

def test_chat_raises_before_any_network_call_for_unsupported_model(monkeypatch):
    import requests
    backend = _tiny_backend("claude-2.1")
    post_called = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: post_called.append(1) or (_ for _ in ()).throw(AssertionError("must not reach network")))

    messages = [{"role": "user", "content": [_IMAGE, {"type": "text", "text": "what is this?"}]}]
    with pytest.raises(ValueError, match="does not support image input"):
        backend.chat(messages)
    assert post_called == []


def test_chat_stream_raises_before_any_network_call_for_unsupported_model(monkeypatch):
    import requests
    backend = _tiny_backend("claude-2.1")
    post_called = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: post_called.append(1) or (_ for _ in ()).throw(AssertionError("must not reach network")))

    messages = [{"role": "user", "content": [_IMAGE, {"type": "text", "text": "what is this?"}]}]
    with pytest.raises(ValueError, match="does not support image input"):
        list(backend.chat_stream(messages))
    assert post_called == []


def test_chat_sends_correctly_translated_image_blocks_for_a_supported_model(monkeypatch):
    """Wire-level proof through the real chat() (only requests.post is
    mocked, no network) -- proves the fix reaches the actual payload."""
    import requests

    backend = _tiny_backend("claude-sonnet-4-6")
    captured = {}

    class _FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"content": [{"type": "text", "text": "It's a red square."}], "stop_reason": "end_turn"}

    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["payload"] = json
        return _FakeResp()

    monkeypatch.setattr(requests, "post", _fake_post)

    messages = [{"role": "user", "content": [_IMAGE, {"type": "text", "text": "what is this?"}]}]
    backend.chat(messages)

    sent_content = captured["payload"]["messages"][0]["content"]
    assert sent_content == [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _PNG_DATA}},
        {"type": "text", "text": "what is this?"},
    ]
    assert _PNG_DATA not in str({k: v for k, v in captured["payload"].items() if k != "messages"})


def test_chat_raises_for_a_malformed_image_before_dispatch(monkeypatch):
    import requests
    backend = _tiny_backend("claude-sonnet-4-6")
    post_called = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: post_called.append(1) or (_ for _ in ()).throw(AssertionError("must not reach network")))

    messages = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/bmp;base64,AAAA"}},
        {"type": "text", "text": "what is this?"},
    ]}]
    with pytest.raises(ValueError, match="does not support the image type"):
        backend.chat(messages)
    assert post_called == []


# ── No mutation of stored context during translation ───────────────────────

def test_translation_never_mutates_a_real_contextmanagers_history():
    ctx = ContextManager(owner=False)
    vision_content = [_IMAGE, {"type": "text", "text": "what is this?"}]
    ctx.add_user(list(vision_content))
    ctx.add_assistant("A red square.")

    history_before_ids = [id(m) for m in ctx.history]
    deep_snapshot = copy.deepcopy(ctx.history)

    messages = ctx.build_messages(tool_budget=0)
    translated = AnthropicBackend._translate_messages(messages)

    assert [id(m) for m in ctx.history] == history_before_ids
    assert ctx.history == deep_snapshot
    user_entry = next(m for m in ctx.history if m.get("role") == "user")
    assert user_entry["content"] == vision_content

    translated_user = next(m for m in translated if m.get("role") == "user")
    assert translated_user["content"][0]["type"] == "image"
