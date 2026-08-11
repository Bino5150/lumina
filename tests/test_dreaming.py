"""S41 refactor test: dreaming.run_summarization_call() must route through
get_llm_backend().complete_utility() — same model resolution, auth headers,
and timeout config as every real chat turn — instead of the old bespoke
requests.post() that bypassed the backend abstraction entirely (the bug
these tests used to guard against, in the code complete_utility() replaced).
"""
import pytest
import core.dreaming as dreaming


class FakeBackend:
    """Stands in for whatever get_llm_backend() returns. Records the
    prompt/prefill/temperature/max_tokens it was called with so tests can
    assert on the contract, and can be told to raise or return empty."""

    def __init__(self, response_text="- User asked about X\n- Decided on Y", raises=False):
        self.response_text = response_text
        self.raises = raises
        self.calls = []

    def complete_utility(self, prompt, prefill="", max_tokens=500, temperature=0.3):
        self.calls.append({
            "prompt": prompt,
            "prefill": prefill,
            "max_tokens": max_tokens,
            "temperature": temperature,
        })
        if self.raises:
            return None
        return self.response_text


@pytest.fixture(autouse=True)
def reset_sweep_state():
    dreaming._last_dream_sweep.clear()
    yield
    dreaming._last_dream_sweep.clear()


def test_run_summarization_call_uses_active_backend(monkeypatch):
    """Must call get_llm_backend() rather than hardcoding a URL/model —
    this is the entire point of the S41 refactor."""
    fake = FakeBackend()
    monkeypatch.setattr(dreaming, "get_llm_backend", lambda: fake)

    result = dreaming.run_summarization_call("user: hello\nassistant: hi there")

    assert result == fake.response_text
    assert len(fake.calls) == 1


def test_run_summarization_call_sends_prefill_and_dream_prompt(monkeypatch):
    """prefill='SUMMARY:' is the established thinking-bleed fix (reused from
    S23) — losing it silently reintroduces the bug complete_utility() exists
    to prevent."""
    fake = FakeBackend()
    monkeypatch.setattr(dreaming, "get_llm_backend", lambda: fake)

    dreaming.run_summarization_call("user: test message")

    call = fake.calls[0]
    assert call["prefill"] == "SUMMARY:"
    assert dreaming.DREAM_PROMPT in call["prompt"]
    assert "user: test message" in call["prompt"]


def test_run_summarization_call_truncates_to_6000_chars(monkeypatch):
    fake = FakeBackend()
    monkeypatch.setattr(dreaming, "get_llm_backend", lambda: fake)

    long_text = "x" * 10_000
    dreaming.run_summarization_call(long_text)

    call = fake.calls[0]
    # prompt = DREAM_PROMPT + "\n\n" + raw_text[:6000]
    assert call["prompt"].count("x") == 6000


def test_run_summarization_call_returns_none_on_backend_exception(monkeypatch):
    """Never raises — callers (on_session_idle) treat None as 'skip this
    sweep,' matching the pre-existing contract."""
    class ExplodingBackend:
        def complete_utility(self, *a, **k):
            raise RuntimeError("backend unreachable")

    monkeypatch.setattr(dreaming, "get_llm_backend", lambda: ExplodingBackend())

    result = dreaming.run_summarization_call("some text")
    assert result is None


def test_run_summarization_call_returns_none_when_backend_yields_empty(monkeypatch):
    """complete_utility()'s own contract returns None on empty/failed
    completions — run_summarization_call must propagate that, not paper
    over it with an empty string."""
    fake = FakeBackend(raises=True)  # FakeBackend.raises=True -> returns None
    monkeypatch.setattr(dreaming, "get_llm_backend", lambda: fake)

    result = dreaming.run_summarization_call("some text")
    assert result is None


def test_on_session_idle_skips_when_dream_sweep_disabled(monkeypatch):
    monkeypatch.setattr(dreaming.config, "DREAM_SWEEP_ENABLED", False)
    calls = []
    monkeypatch.setattr(dreaming, "load_chat_messages", lambda cid: calls.append(cid))

    dreaming.on_session_idle(chat_id=123)

    assert calls == []  # never even loaded messages


def test_on_session_idle_skips_below_min_token_threshold(monkeypatch):
    monkeypatch.setattr(dreaming.config, "DREAM_SWEEP_ENABLED", True)
    monkeypatch.setattr(dreaming.config, "DREAM_MIN_TOKENS", 800)
    monkeypatch.setattr(
        dreaming, "load_chat_messages",
        lambda cid: [{"role": "user", "content": "short", "created_at": "2026-07-09T00:00:00"}],
    )
    fake = FakeBackend()
    monkeypatch.setattr(dreaming, "get_llm_backend", lambda: fake)

    dreaming.on_session_idle(chat_id=123)

    assert fake.calls == []  # never called the backend — below threshold


def test_on_session_idle_writes_to_nightstand_wing_on_success(monkeypatch):
    monkeypatch.setattr(dreaming.config, "DREAM_SWEEP_ENABLED", True)
    monkeypatch.setattr(dreaming.config, "DREAM_MIN_TOKENS", 1)  # force past threshold
    monkeypatch.setattr(
        dreaming, "load_chat_messages",
        lambda cid: [{"role": "user", "content": "x" * 50, "created_at": "2026-07-09T00:00:00"}],
    )
    fake = FakeBackend(response_text="- Did a thing")
    monkeypatch.setattr(dreaming, "get_llm_backend", lambda: fake)

    stored = {}

    def fake_palace_store(content, wing, room, layer, tags):
        stored.update(content=content, wing=wing, room=room, layer=layer, tags=tags)

    monkeypatch.setattr(dreaming, "palace_store", fake_palace_store)

    dreaming.on_session_idle(chat_id=42)

    assert stored["wing"] == "nightstand"
    assert stored["room"] == "42"
    assert stored["layer"] == 2
    assert stored["content"] == "- Did a thing"
    assert "dream-sweep" in stored["tags"]


# ── MB-11: run_summarization_call() parameterized for compaction reuse ──

def test_run_summarization_call_defaults_match_original_dream_behavior(monkeypatch):
    """No prompt/max_tokens passed -> identical contract to before
    parameterization (DREAM_PROMPT, max_tokens=500)."""
    fake = FakeBackend()
    monkeypatch.setattr(dreaming, "get_llm_backend", lambda: fake)

    dreaming.run_summarization_call("user: test message")

    call = fake.calls[0]
    assert dreaming.DREAM_PROMPT in call["prompt"]
    assert call["max_tokens"] == 500
    assert call["prefill"] == "SUMMARY:"
    assert call["temperature"] == 0.3


def test_run_summarization_call_accepts_custom_prompt_and_max_tokens(monkeypatch):
    """Compaction's call shape: prompt=COMPACTION_PROMPT, max_tokens=300."""
    fake = FakeBackend()
    monkeypatch.setattr(dreaming, "get_llm_backend", lambda: fake)

    dreaming.run_summarization_call(
        "user: hi\nassistant: hello",
        prompt=dreaming.COMPACTION_PROMPT,
        max_tokens=300,
    )

    call = fake.calls[0]
    assert dreaming.COMPACTION_PROMPT in call["prompt"]
    assert dreaming.DREAM_PROMPT not in call["prompt"]
    assert call["max_tokens"] == 300
    assert call["prefill"] == "SUMMARY:"  # unchanged even for compaction path


def test_compaction_prompt_is_distinct_from_dream_prompt():
    assert dreaming.COMPACTION_PROMPT != dreaming.DREAM_PROMPT


# ── MB-08 Part 1: human profile curation ────────────────────────────────

def test_curate_human_profile_calls_backend_with_expected_prompt_shape(monkeypatch):
    fake = FakeBackend(response_text="- Likes tea\n- Working on MB-08")
    monkeypatch.setattr(dreaming, "get_llm_backend", lambda: fake)
    monkeypatch.setattr(dreaming.config, "USER_NAME", "Bino")

    result = dreaming.curate_human_profile(
        raw_text="user: I've been drinking a lot of tea lately",
        bio="Bino is a software engineer.",
        existing_profile="- Likes coffee",
    )

    assert result == fake.response_text
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["prefill"] == "NOTES:"
    assert call["max_tokens"] == 400
    assert call["temperature"] == 0.3
    assert "Bino is a software engineer." in call["prompt"]
    assert "- Likes coffee" in call["prompt"]
    assert "I've been drinking a lot of tea lately" in call["prompt"]
    assert "Bino" in call["prompt"]


def test_curate_human_profile_fills_placeholders_when_bio_and_existing_empty(monkeypatch):
    fake = FakeBackend()
    monkeypatch.setattr(dreaming, "get_llm_backend", lambda: fake)

    dreaming.curate_human_profile(raw_text="user: hi", bio="", existing_profile="")

    call = fake.calls[0]
    assert "(none written yet)" in call["prompt"]
    assert "(none yet)" in call["prompt"]


def test_curate_human_profile_returns_none_on_backend_exception(monkeypatch):
    class ExplodingBackend:
        def complete_utility(self, *a, **k):
            raise RuntimeError("backend unreachable")

    monkeypatch.setattr(dreaming, "get_llm_backend", lambda: ExplodingBackend())

    result = dreaming.curate_human_profile("some text", "bio", "existing")
    assert result is None


def test_on_session_idle_does_not_curate_profile_when_flag_disabled(monkeypatch):
    """Regression guard: HUMAN_PROFILE_CURATION_ENABLED defaults False --
    curate_human_profile() must not fire (and therefore never touches
    prefs.json) unless the flag is explicitly on."""
    monkeypatch.setattr(dreaming.config, "DREAM_SWEEP_ENABLED", True)
    monkeypatch.setattr(dreaming.config, "DREAM_MIN_TOKENS", 1)
    monkeypatch.setattr(dreaming.config, "HUMAN_PROFILE_CURATION_ENABLED", False)
    monkeypatch.setattr(
        dreaming, "load_chat_messages",
        lambda cid: [{"role": "user", "content": "x" * 50, "created_at": "2026-07-09T00:00:00"}],
    )
    fake = FakeBackend(response_text="- Did a thing")
    monkeypatch.setattr(dreaming, "get_llm_backend", lambda: fake)
    monkeypatch.setattr(dreaming, "palace_store", lambda **kw: None)

    curate_calls = []
    monkeypatch.setattr(dreaming, "curate_human_profile", lambda *a, **k: curate_calls.append(a))

    dreaming.on_session_idle(chat_id=42)

    assert curate_calls == []
