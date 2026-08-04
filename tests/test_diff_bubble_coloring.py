import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from ui.chat_widget import md_to_html
_COLORS = {"accent": "#00ffcc"}  # md_to_html only reads colors["accent"] outside code fences
_DIFF_SAMPLE = """```diff
--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,3 @@
 unchanged line
-old line
+new line
```"""
def test_diff_block_colors_added_line_green():
    html = md_to_html(_DIFF_SAMPLE, _COLORS)
    assert '#3fb950' in html
    assert '+new line' in html
def test_diff_block_colors_removed_line_red():
    html = md_to_html(_DIFF_SAMPLE, _COLORS)
    assert '#f85149' in html
    assert '-old line' in html
def test_diff_block_file_headers_neutral():
    html = md_to_html(_DIFF_SAMPLE, _COLORS)
    assert '#8b949e' in html
    assert '+++ b/foo.py' in html
def test_diff_block_hunk_header_accent():
    html = md_to_html(_DIFF_SAMPLE, _COLORS)
    assert '#79c0ff' in html
    assert '@@ -1,3 +1,3 @@' in html
def test_non_diff_code_block_unaffected():
    """Regression guard: plain (non-diff) fenced code still renders as the
    original flat <pre> block, unchanged."""
    text = "```python\nprint('hi')\n```"
    html = md_to_html(text, _COLORS)
    assert '<span style="color:#3fb950' not in html
    assert "print('hi')" in html
