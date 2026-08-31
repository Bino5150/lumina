"""
core/redaction.py -- CONTEXT-LIFECYCLE-A4I: neutral, supported secret-shape
redaction primitives shared by core/flight_recorder.py and the continuity
compiler (core/continuity_compiler.py).

Extracted verbatim from core/flight_recorder.py's own pre-A4I private
`_SECRET_KEY_MARKERS`/`_SECRET_VALUE_RE`/`_redact_value_shapes` -- same
patterns, same behavior, now a public, independently-importable module so a
second consumer (the continuity compiler) does not have to reach into
flight_recorder's private names to get the same protection. Flight Recorder
itself is updated to import from here rather than defining its own copies,
so both consumers can never silently drift apart.

This is best-effort, known-shape secret filtering -- NOT a claim that any
text passed through it is guaranteed secret-free. It catches recognizable
key-name markers and known API-token/credential value shapes; it does not
and cannot detect every possible secret.
"""
import re

# Structural key-name redaction: ANY field whose key contains one of these
# (case-insensitive substring match, deliberately broad) should have its
# value replaced outright, regardless of type -- catches api_key/apikey/
# API_KEY/access_token/authorization/Authorization/password/client_secret/
# etc. without depending on the value itself looking like a known secret
# shape.
SECRET_KEY_MARKERS = (
    "api_key", "apikey", "access_key", "access_token", "secret_key",
    "secret", "token", "auth", "password", "passwd", "credential",
    "private_key",
)

# Regex layer for known secret VALUE shapes that could appear under an
# innocuous key name (e.g. a tool arg literally named "query" containing a
# pasted API key). Deliberately narrow, known-prefix patterns -- same
# posture as gemini_backend.py's own _redact_thought_signatures(): look for
# recognizable shapes, redact, never assume absence of a marker means safe.
SECRET_VALUE_RE = re.compile(
    r"sk-[A-Za-z0-9_-]{16,}"
    r"|Bearer\s+[A-Za-z0-9._-]{16,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|ghp_[A-Za-z0-9]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
)


def redact_secret_shapes(text: str) -> str:
    """Replace every known secret-value shape in `text` with "[REDACTED]".
    Best-effort only -- see module docstring."""
    return SECRET_VALUE_RE.sub("[REDACTED]", text)


def is_secret_key(key: str) -> bool:
    """Case-insensitive substring match of `key` against SECRET_KEY_MARKERS.
    A caller iterating a dict's keys uses this to decide whether to redact
    the whole value outright rather than pass it through redact_secret_shapes()."""
    key_lower = str(key).lower()
    return any(marker in key_lower for marker in SECRET_KEY_MARKERS)
