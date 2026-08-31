"""CONTEXT-LIFECYCLE-A4I -- core/redaction.py: neutral secret-shape redaction.

Confirms the extraction from core/flight_recorder.py is behavior-preserving
(same patterns, same output) and that Flight Recorder now consumes this
module's public primitives rather than a private copy -- the two required
guarantees from A4I section 3: byte-for-byte-preserved FR behavior, and a
single shared redaction primitive for both consumers.
"""
import re

import core.flight_recorder as flight_recorder
import core.redaction as redaction


def test_flight_recorder_aliases_are_identical_objects():
    """Not just equal output -- literally the same objects, so the two
    modules cannot silently drift apart in a future edit to one only."""
    assert flight_recorder._SECRET_KEY_MARKERS is redaction.SECRET_KEY_MARKERS
    assert flight_recorder._SECRET_VALUE_RE is redaction.SECRET_VALUE_RE
    assert flight_recorder._redact_value_shapes is redaction.redact_secret_shapes


def test_redact_secret_shapes_openai_key():
    text = "here is my key sk-abcdefghijklmnopqrstuvwx1234"
    out = redaction.redact_secret_shapes(text)
    assert "sk-abcdefghijklmnopqrstuvwx1234" not in out
    assert "[REDACTED]" in out


def test_redact_secret_shapes_bearer_token():
    text = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456"
    out = redaction.redact_secret_shapes(text)
    assert "abcdefghijklmnopqrstuvwxyz123456" not in out


def test_redact_secret_shapes_aws_key():
    text = "AKIA" + "A" * 16
    out = redaction.redact_secret_shapes(text)
    assert out == "[REDACTED]"


def test_redact_secret_shapes_github_token():
    text = "ghp_" + "a" * 25
    out = redaction.redact_secret_shapes(text)
    assert "[REDACTED]" in out
    assert "ghp_" not in out


def test_redact_secret_shapes_slack_token():
    text = "xoxb-" + "1" * 12
    out = redaction.redact_secret_shapes(text)
    assert "[REDACTED]" in out


def test_ordinary_text_preserved():
    text = "the quick brown fox jumps over the lazy dog, revision 42"
    assert redaction.redact_secret_shapes(text) == text


def test_is_secret_key_matches_known_markers():
    for key in ("api_key", "API_KEY", "access_token", "Authorization",
                "password", "client_secret", "PRIVATE_KEY"):
        assert redaction.is_secret_key(key), key


def test_is_secret_key_does_not_match_ordinary_field_names():
    for key in ("summary", "title", "path", "status", "count"):
        assert not redaction.is_secret_key(key), key


def test_secret_value_re_is_compiled_pattern():
    assert isinstance(redaction.SECRET_VALUE_RE, re.Pattern)
