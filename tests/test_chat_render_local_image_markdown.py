"""
tests/test_chat_render_local_image_markdown.py -- MEDIA-GENERATION-
CONVERSATIONAL-RUNTIME-01.

Before this campaign, core/chat_render.py's md_to_html() had NO handling
for `![alt](path)` at all -- only `[text](url)` -- so a generated image's
local path had no way to actually render in a chat bubble; QTextBrowser
natively renders a local `<img src="file://...">`, so this is the smallest
correct addition. Pure-string logic, no PySide6 dependency, matching this
module's existing test convention (tests/test_diff_bubble_coloring.py).
"""
import os

from core.chat_render import md_to_html

_COLORS = {"accent": "#00ffcc"}


def test_local_image_with_real_file_renders_as_img_tag(tmp_path):
    img_path = tmp_path / "generated.png"
    img_path.write_bytes(b"\x89PNG\r\n\x1a\n")  # minimal PNG magic bytes, content irrelevant here

    html = md_to_html(f"Here you go:\n\n![Generated image](file://{img_path})", _COLORS)

    assert "<img " in html
    assert f'src="file://{img_path}"' in html or "src=\"file://" in html
    assert "<a href=" not in html  # must not ALSO fall through to the link renderer


def test_nonexistent_local_path_is_left_untouched():
    """A path that doesn't resolve to a real file must never be claimed as
    an image -- this must never assert an image exists when it doesn't."""
    text = "![Generated image](file:///nonexistent/path/does-not-exist.png)"
    html = md_to_html(text, _COLORS)

    assert "<img " not in html


def test_http_url_is_never_rendered_as_an_image():
    """Security boundary: file:// is the only accepted scheme. Text a model
    might echo from an untrusted source (fetched web content, provider
    text) must never turn into a live network image request from this
    renderer -- see core/image_generation_service.py's "remote URL is
    never durable" law, mirrored here as "remote URL is never rendered."""
    text = "![tracker](http://example.com/pixel.png)"
    html = md_to_html(text, _COLORS)

    assert "<img " not in html
    assert 'src="http://example.com/pixel.png"' not in html


def test_https_url_is_never_rendered_as_an_image():
    text = "![tracker](https://example.com/pixel.png)"
    html = md_to_html(text, _COLORS)

    assert "<img " not in html


def test_image_markdown_inside_code_fence_stays_literal():
    """Image syntax appearing inside a fenced code block is sample text,
    not a real reference -- it must not be converted."""
    text = "```\n![alt](file:///tmp/whatever.png)\n```"
    html = md_to_html(text, _COLORS)

    assert "<img " not in html
    assert "![alt](file:///tmp/whatever.png)" in html


def test_ordinary_link_markdown_still_renders_as_anchor(tmp_path):
    """Regression guard: the pre-existing [text](url) -> <a> behavior for
    non-image links must be completely unaffected by this addition."""
    html = md_to_html("See [the docs](https://example.com/docs) for more.", _COLORS)

    assert '<a href="https://example.com/docs"' in html
    assert "<img " not in html


def test_multiple_local_images_all_render(tmp_path):
    img1 = tmp_path / "one.png"
    img2 = tmp_path / "two.png"
    img1.write_bytes(b"one")
    img2.write_bytes(b"two")

    html = md_to_html(
        f"![Generated image](file://{img1})\n![Generated image](file://{img2})",
        _COLORS,
    )

    assert html.count("<img ") == 2
