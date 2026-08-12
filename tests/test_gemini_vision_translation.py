"""core/backends/gemini_backend.py -- GeminiBackend._parts_from_content().

Regression coverage for the vision/multimodal translator. Commit `2cae99d`
("fix: Gemini vision + tool-continuation, custom-provider errors") already
fixed real mishandling of image content blocks and noted it was "self-
patched and live-verified against the real API" -- but that verification was
manual, and no unit test ever locked the fix in. Adding coverage now,
directly triggered by planning to actually exercise Gemini vision again.

Covers the OpenAI-shaped multipart content list -> Gemini Part translation:
plain string, multipart text-only, text + image data URL, image-only (no
text block), an empty-caption text block alongside an image, a malformed
data URL, and a non-data (remote http/https) image URL -- the one case
_parts_from_content()'s own comment says is deliberately unhandled.
"""
from core.backends.gemini_backend import GeminiBackend


def test_plain_string_becomes_single_text_part():
    parts = GeminiBackend._parts_from_content("hello there")
    assert parts == [{"text": "hello there"}]


def test_none_content_becomes_empty_text_part():
    parts = GeminiBackend._parts_from_content(None)
    assert parts == [{"text": ""}]


def test_multipart_text_only_list():
    content = [{"type": "text", "text": "describe this"}]
    parts = GeminiBackend._parts_from_content(content)
    assert parts == [{"text": "describe this"}]


def test_text_plus_png_data_url_produces_text_and_inline_data_parts():
    content = [
        {"type": "text", "text": "what's in this image?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
    ]
    parts = GeminiBackend._parts_from_content(content)
    assert parts == [
        {"text": "what's in this image?"},
        {"inline_data": {"mime_type": "image/png", "data": "QUJD"}},
    ]


def test_image_only_no_text_block_produces_single_inline_data_part():
    """No fake empty text Part should be inserted just because there's no
    caption -- the "if not parts: append empty text" fallback only exists
    for the genuinely-empty case, not "image with no caption."""
    content = [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,Zg=="}}]
    parts = GeminiBackend._parts_from_content(content)
    assert parts == [{"inline_data": {"mime_type": "image/jpeg", "data": "Zg=="}}]


def test_empty_caption_text_block_alongside_image_is_skipped_not_sent_empty():
    content = [
        {"type": "text", "text": ""},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
    ]
    parts = GeminiBackend._parts_from_content(content)
    # The empty text block must not survive as {"text": ""} alongside the image.
    assert parts == [{"inline_data": {"mime_type": "image/png", "data": "QUJD"}}]


def test_mime_type_defaults_to_png_when_missing_from_data_url():
    content = [{"type": "image_url", "image_url": {"url": "data:;base64,QUJD"}}]
    parts = GeminiBackend._parts_from_content(content)
    assert parts == [{"inline_data": {"mime_type": "image/png", "data": "QUJD"}}]


def test_comma_less_data_url_does_not_crash_translation():
    """str.partition() never raises on a missing separator (returns an empty
    sep/tail instead of erroring), so a data: URL with no comma doesn't hit
    the surrounding try/except at all -- it still produces an inline_data
    part, just with an empty data string. Documenting actual behavior here
    (not inventing a "should be dropped" rule this code doesn't implement):
    the caller must not crash on this shape, whatever the exact output."""
    content = [
        {"type": "text", "text": "here's a broken image"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64"}},  # no comma separator
    ]
    parts = GeminiBackend._parts_from_content(content)  # must not raise
    assert {"text": "here's a broken image"} in parts
    assert {"inline_data": {"mime_type": "image/png", "data": ""}} in parts


def test_remote_http_image_url_is_skipped_not_fetched():
    """_parts_from_content()'s own comment: only data: URLs are handled --
    remote http(s) URLs are deliberately skipped, not fetched."""
    content = [
        {"type": "text", "text": "check this out"},
        {"type": "image_url", "image_url": {"url": "https://example.com/cat.png"}},
    ]
    parts = GeminiBackend._parts_from_content(content)
    assert parts == [{"text": "check this out"}]


def test_all_parts_dropped_falls_back_to_single_empty_text_part():
    """If every block is unusable (e.g. only a remote URL, no caption), the
    result must not be an empty parts list -- Gemini's Content requires at
    least one Part."""
    content = [{"type": "image_url", "image_url": {"url": "https://example.com/cat.png"}}]
    parts = GeminiBackend._parts_from_content(content)
    assert parts == [{"text": ""}]


def test_unknown_block_type_is_ignored():
    content = [{"type": "audio_url", "audio_url": {"url": "data:audio/mp3;base64,ZZZ"}}]
    parts = GeminiBackend._parts_from_content(content)
    assert parts == [{"text": ""}]
