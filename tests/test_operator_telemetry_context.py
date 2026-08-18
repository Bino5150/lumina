"""Focused regression coverage for operator context telemetry."""
import config
from core.context import ContextManager, estimate_message_tokens


def _fill(cm, n=20):
    for i in range(n):
        cm.add_user(f"message number {i} padding padding padding padding")


def test_usage_counts_tool_budget_and_dynamic_prompt():
    cm = ContextManager(owner=False)
    cm.system_prompt = "base prompt"
    cm.max_tokens = 1000
    cm.reserve = 100
    cm.add_user("hello " * 40)
    cm.push_ephemeral("temporary operator-relevant block " * 20)

    usage = cm.context_usage_snapshot(tool_budget=77, refresh=True)

    assert usage["tool_tokens"] == 77
    assert usage["used_tokens"] == (
        usage["system_tokens"] + usage["history_tokens"] + usage["tool_tokens"]
    )
    assert usage["system_tokens"] > 4 + len(cm.system_prompt) // 4
    assert usage["max_tokens"] == 1000
    assert usage["reserve_tokens"] == 100


def test_usage_matches_trim_budget_without_mutating_compaction(monkeypatch):
    monkeypatch.setattr(config, "CONTEXT_COMPACTION_ENABLED", True)
    cm = ContextManager(owner=False)
    cm.system_prompt = "base prompt"
    cm.max_tokens = 90
    cm.reserve = 10
    _fill(cm)
    original_history = list(cm.history)

    usage = cm.context_usage_snapshot(tool_budget=10, refresh=True)

    assert usage["used_tokens"] <= cm.max_tokens - cm.reserve
    assert usage["prompt_headroom_tokens"] >= 0
    assert cm.history == original_history
    assert cm._pending_compaction == []


def test_build_messages_records_same_trimmed_history_usage(monkeypatch):
    monkeypatch.setattr(config, "CONTEXT_COMPACTION_ENABLED", False)
    cm = ContextManager(owner=False)
    cm.system_prompt = "base prompt"
    cm.max_tokens = 100
    cm.reserve = 10
    _fill(cm)

    messages = cm.build_messages(tool_budget=12, chat_id=42)
    usage = cm.context_usage_snapshot(tool_budget=12, chat_id=42, refresh=False)

    expected_history_tokens = sum(estimate_message_tokens(m) for m in messages[1:])
    assert usage["history_tokens"] == expected_history_tokens
    assert usage["chat_id"] == 42
    assert usage["used_tokens"] <= cm.max_tokens - cm.reserve


def test_denominator_tracks_live_backend_limit_change():
    cm = ContextManager(owner=False)
    cm.system_prompt = "small prompt"
    cm.reserve = 1000
    cm.max_tokens = 19_000
    cm.add_user("hello there")

    local = cm.context_usage_snapshot(tool_budget=50, refresh=True)
    cm.max_tokens = 1_000_000
    cloud = cm.context_usage_snapshot(tool_budget=50, refresh=False)

    assert local["max_tokens"] == 19_000
    assert cloud["max_tokens"] == 1_000_000
    assert cloud["used_tokens"] == local["used_tokens"]
    assert cloud["percent"] < local["percent"]


def test_cache_invalidated_by_new_message():
    cm = ContextManager(owner=False)
    cm.max_tokens = 10_000
    cm.reserve = 1000
    first = cm.context_usage_snapshot(tool_budget=50, refresh=True)
    cm.add_user("new material " * 40)
    second = cm.context_usage_snapshot(tool_budget=50, refresh=False)
    assert second["used_tokens"] > first["used_tokens"]
