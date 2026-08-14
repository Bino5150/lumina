"""core/context.py — MB-11 Step 2: build_messages()'s trim loop now captures
dropped history into ContextManager._pending_compaction instead of silently
discarding it, gated by config.CONTEXT_COMPACTION_ENABLED (default False).
"""
import config
from core.context import ContextManager


def _fill(cm, n=20):
    for i in range(n):
        cm.add_user(f"message number {i} padding padding padding padding")


def test_flag_off_pending_compaction_stays_empty(monkeypatch):
    monkeypatch.setattr(config, "CONTEXT_COMPACTION_ENABLED", False)
    cm = ContextManager(owner=False)
    cm.max_tokens = 50
    cm.reserve = 0
    _fill(cm)

    msgs = cm.build_messages(tool_budget=0)

    assert len(msgs) < 21, "sanity: trimming should actually have happened"
    assert cm._pending_compaction == []
    assert cm.pending_compaction_tokens() == 0


def test_flag_on_captures_dropped_messages(monkeypatch):
    monkeypatch.setattr(config, "CONTEXT_COMPACTION_ENABLED", True)
    cm = ContextManager(owner=False)
    cm.max_tokens = 50
    cm.reserve = 0
    _fill(cm)

    cm.build_messages(tool_budget=0)

    assert len(cm._pending_compaction) > 0
    assert cm.pending_compaction_tokens() > 0


def test_take_pending_compaction_drains_buffer(monkeypatch):
    monkeypatch.setattr(config, "CONTEXT_COMPACTION_ENABLED", True)
    cm = ContextManager(owner=False)
    cm.max_tokens = 50
    cm.reserve = 0
    _fill(cm)
    cm.build_messages(tool_budget=0)

    batch = cm.take_pending_compaction()

    assert len(batch) > 0
    assert cm._pending_compaction == []
    assert cm.pending_compaction_tokens() == 0


def test_compacting_flag_defaults_false():
    cm = ContextManager(owner=False)
    assert cm._compacting is False


def test_provenance_reminder_absent_before_untrusted_content():
    cm = ContextManager(owner=False)
    cm.add_user("hello")
    prompt = cm._build_system_prompt()
    assert "## Provenance reminder" not in prompt


def test_provenance_reminder_present_after_tool_result():
    cm = ContextManager(owner=False)
    cm.add_tool_result("call_1", "get_website", "some fetched content")
    prompt = cm._build_system_prompt()
    assert "## Provenance reminder" in prompt
    assert "directive addressed at you" in prompt


def test_tool_result_tag_names_directives_explicitly():
    cm = ContextManager(owner=False)
    cm.add_tool_result("call_1", "get_website", "some fetched content")
    tagged_content = cm.history[-1]["content"]
    assert "directives addressed at you" in tagged_content
    assert "declining silently" in tagged_content
