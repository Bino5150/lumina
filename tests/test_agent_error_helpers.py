"""Bug C fix (LUMINA_HANDOFF_OMNIROUTE_TO_PROVIDER_FIXES_2026-08-02.md
section 8.3, item 5): a failed turn returns its error as plain response
text instead of raising (chat()'s tool-loop except / _stream_final()'s
except), by design -- the "never raises" contract core/headless.py
documents. ui/main_window.py's _on_finished() used to have no way to tell
that apart from a real assistant reply, so every failed turn also fired
_auto_name_chat(), which made a second doomed complete_utility() call
against the same broken provider/endpoint -- duplicate noise, and
potentially duplicate paid traffic against a metered provider.

is_error_response() centralizes the sentinel-string convention in one
place instead of leaving the UI layer to duplicate/guess at the exact
wording. These tests cover that helper plus the companion fix: adding
ValueError to _stream_final()'s caught exception tuple, since Bug C's new
validate_base_url() raises ValueError for an unconfigured/schemeless
endpoint -- a real, expected failure mode that deserves the same graceful
"[Stream error: ...]" + persisted-history treatment as the other backend
failure types, not an escape to AgentWorker's cruder top-level handler.
"""
import types
import pytest
from core.agent import LuminaAgent, is_error_response


# ── is_error_response() ──────────────────────────────────────────────────

def test_empty_and_none_are_not_errors():
    assert is_error_response("") is False
    assert is_error_response(None) is False


def test_normal_assistant_reply_is_not_an_error():
    assert is_error_response("Sure, here's the summary you asked for.") is False


def test_lumina_error_prefix_is_an_error():
    assert is_error_response("[Lumina error: Custom (OpenAI-compatible): no endpoint URL configured.]") is True


def test_stream_error_prefix_is_an_error():
    assert is_error_response("[Stream error: OpenCode Zen error (billing_required, HTTP 401): No payment method.]") is True


def test_a_reply_that_merely_mentions_error_is_not_flagged():
    """Only the exact sentinel prefixes should match -- a real assistant
    reply that happens to discuss errors (e.g. debugging help) must not be
    mistaken for a failed turn and silently skip auto-naming."""
    assert is_error_response("Here's how to fix that Lumina error you saw earlier.") is False


def test_tool_continuation_failure_sentinel_is_flagged():
    """The gap found in patch review: a tool succeeds, then the *next*
    provider call in the same turn fails during continuation (Bug B /
    section 8.2.1's observability fix). That message's exact bracket
    formatting isn't verifiable from this repo (it's local-only, not
    pushed), so this anchors on the one substring independently confirmed
    stable across both the original handoff and the patch-review report."""
    msg = "Tool `search_memory` completed, but Gemini rejected the continuation: 400 Bad Request"
    assert is_error_response(msg) is True


def test_bracketed_continuation_failure_sentinel_is_also_flagged():
    """If the real local wording does wrap in brackets like the other two
    sentinels, the substring match still catches it regardless."""
    msg = "[Tool `search_memory` completed, but Gemini rejected the continuation: 400 Bad Request]"
    assert is_error_response(msg) is True


# ── _stream_final()'s new ValueError handling ────────────────────────────

def test_stream_final_converts_valueerror_to_stream_error_string():
    class FakeLLM:
        def chat_stream(self, messages, max_tokens, reasoning_effort=None):
            raise ValueError("Custom (OpenAI-compatible): no endpoint URL configured.")
            yield  # pragma: no cover -- makes this a generator function

    tokens = []
    fake_self = types.SimpleNamespace(
        llm=FakeLLM(),
        on_think_start=lambda step: None,
        on_think_end=lambda: None,
        on_think_token=lambda tok: None,
        on_response_token=lambda tok: tokens.append(tok),
        ctx=types.SimpleNamespace(add_assistant=lambda content: None),
        tts=None,
    )

    result = LuminaAgent._stream_final(fake_self, messages=[], think_step=[0])

    assert result.startswith("[Stream error:")
    assert "no endpoint URL configured" in result
    assert is_error_response(result) is True
    # on_response_token must have fired too -- this is what makes the
    # failure visible in the live chat bubble instead of silence.
    assert tokens and tokens[0] == result


def test_stream_final_still_handles_the_original_three_exception_types():
    """Regression guard: adding ValueError to the except tuple must not
    have replaced or narrowed the original three."""
    for exc in (ConnectionError("down"), TimeoutError("slow"), RuntimeError("boom")):
        class FakeLLM:
            def __init__(self, e):
                self.e = e

            def chat_stream(self, messages, max_tokens, reasoning_effort=None):
                raise self.e
                yield  # pragma: no cover

        fake_self = types.SimpleNamespace(
            llm=FakeLLM(exc),
            on_think_start=lambda step: None,
            on_think_end=lambda: None,
            on_think_token=lambda tok: None,
            on_response_token=lambda tok: None,
            ctx=types.SimpleNamespace(add_assistant=lambda content: None),
            tts=None,
        )
        result = LuminaAgent._stream_final(fake_self, messages=[], think_step=[0])
        assert result.startswith("[Stream error:")
