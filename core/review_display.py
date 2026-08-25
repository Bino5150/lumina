"""Neutral, read-only hostile-path display escaping (CODING-08A3/A4).

Extracted from ``tools/review.py`` (CODING-08A3) so the Qt owner-facing
review cockpit (CODING-08A4) can reuse the exact same, already-reviewed
escaping logic rather than creating a second, potentially-diverging
sanitizer for the same class of hostile filename. A3 and A4 are sibling
consumers of A1/A2 structured facts; this module is the one place either
may render a raw repository path as inert display text.

This module is deliberately Qt-free (importable and testable without a
PySide6 dependency), matching the project's established core/ convention
of keeping pure-string logic out of ui/ so it can be unit-tested in CI
jobs that never install PySide6 (see core/chat_render.py's own docstring
for the same rationale).

Escaping is always a literal textual substitution (``\\uHHHH``/``\\xHH``),
never reliance on downstream escaping (e.g. ``json.dumps(ensure_ascii=True)``
or Qt's own text rendering) -- the hostile codepoint must never survive as
a real character in the returned string, so it stays inert even after a
JSON round-trip or when inserted into any Qt text widget.
"""

from __future__ import annotations

import re

_BIDI_CONTROL_CHARS = "‎‏‪‫‬‭‮⁦⁧⁨⁩"
# Every C0 control character (including tab/newline/CR) is escaped, none
# preserved -- a tab or newline embedded in a PATH is exactly the
# metadata-spoofing vector this function exists to neutralize. This is
# deliberately stricter than a free-text sanitizer (e.g.
# tools/worktrees.py's _CONTROL_CHAR_RE), which preserves tab/newline as
# legitimate formatting for prose; a path is never prose.
_DISPLAY_ESCAPE_RE = re.compile(
    "[\x00-\x1f\x7f" + _BIDI_CONTROL_CHARS + "]"
)

MAX_DISPLAY_PATH_CHARS = 1024


def escape_display_path(path: str) -> str:
    """Inert, safe rendering of a path for display to a model or a human.

    Surrogateescape artifacts from a raw-byte decode of an invalid non-UTF-8
    path byte render as ``\\xHH``; C0/DEL/bidi-override codepoints render as
    ``\\uHHHH``; everything else passes through unchanged. Truncated with a
    trailing marker beyond ``MAX_DISPLAY_PATH_CHARS``.

    Never the retrieval selector -- callers must key content lookups by an
    opaque identity (A2's ``change_id``), never by this rendered string, and
    must never feed this escaped text back into Git as a real path.
    """
    out = []
    for ch in path:
        code = ord(ch)
        if 0xDC80 <= code <= 0xDCFF:
            out.append(f"\\x{code & 0xff:02x}")
        elif _DISPLAY_ESCAPE_RE.match(ch):
            out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    text = "".join(out)
    if len(text) > MAX_DISPLAY_PATH_CHARS:
        text = text[:MAX_DISPLAY_PATH_CHARS] + "...[truncated]"
    return text
