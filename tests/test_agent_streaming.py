"""core/agent.py — FE-20: a print(f"[STREAM] {repr(chunk)}") fired on every
single streamed token, regardless of length -- console spam plus real
overhead on long responses. This slipped through the S43 "FE-20 through
FE-29 low-tier sweep" commit despite the handoff claiming it was removed:
git blame traced the line straight back to the initial commit, unchanged.
Confirmed still live on both the OG and release builds before this fix.
A behavioral test this time, not just a source grep, so a future refactor
that reintroduces equivalent spam under a different line gets caught too.
"""
import types
from core.agent import LuminaAgent


def test_stream_final_does_not_print_debug_spam(capsys):
    class FakeLLM:
        def chat_stream(self, messages, max_tokens):
            yield "hello "
            yield "world"

    fake_self = types.SimpleNamespace(
        llm=FakeLLM(),
        on_think_start=lambda step: None,
        on_think_end=lambda: None,
        on_think_token=lambda tok: None,
        on_response_token=lambda tok: None,
        ctx=types.SimpleNamespace(add_assistant=lambda content: None),
        tts=None,
    )

    result = LuminaAgent._stream_final(fake_self, messages=[], think_step=[0])

    captured = capsys.readouterr()
    assert "[STREAM]" not in captured.out
    assert result == "hello world"
