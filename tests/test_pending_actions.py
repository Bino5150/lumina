"""MB-33 Tier 2: staging + approval gate for edit_prompt, reset_chat,
delete_knowledge, delete_memory -- tools/pending_actions.py.

Two test groups:
  1. stage_action/list_pending_actions/reject_pending_action unit tests --
     pure queue/audit-log mechanics, no agent, no live state.
  2. Per-kind integration tests, one class per gated tool, each verifying:
       (a) calling the tool as the model would only stages -- real state
           (system prompt / chat history / DB row) must NOT change.
       (b) calling _apply_action directly (simulating the Settings > Tools >
           Pending Actions approve click) DOES correctly mutate real state.
       (c) reject removes the queue entry without touching live state.
"""
import json
import pytest

import config
import tools.pending_actions as pending_actions
import tools.meta as meta
import tools.knowledge as knowledge
import tools.memory as memory
from core.context import ContextManager


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def pending_env(tmp_path, monkeypatch):
    """Isolate the pending-actions queue + audit log to a throwaway dir."""
    monkeypatch.setattr(pending_actions, "QUEUE_PATH", str(tmp_path / "pending_actions.json"))
    monkeypatch.setattr(pending_actions, "AUDIT_LOG_PATH", str(tmp_path / "pending_actions_audit.log"))
    return tmp_path


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    """Isolate config.DB_PATH to a throwaway sqlite file and init the two
    tables delete_knowledge/delete_memory touch."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    knowledge.init_knowledge_db()
    memory.init_memory_db()
    return tmp_path


class _FakeAgent:
    """Only needs .ctx -- _apply_action never touches .registry for any of
    the four gated kinds."""
    def __init__(self):
        self.ctx = ContextManager()


@pytest.fixture
def agent():
    return _FakeAgent()


def _staged_id():
    """The single staged action's id, for tests that only ever stage one."""
    return next(iter(pending_actions._load_queue().keys()))


def _insert_memory(content: str = "hello", label: str = "general") -> int:
    conn = memory.get_db()
    cur = conn.execute(
        "INSERT INTO memories (label, content, created_at) VALUES (?, ?, ?)",
        (label, content, "2026-08-09T00:00:00"),
    )
    memory_id = cur.lastrowid
    conn.commit()
    conn.close()
    return memory_id


def _memory_exists(memory_id: int) -> bool:
    conn = memory.get_db()
    row = conn.execute("SELECT id FROM memories WHERE id=?", (memory_id,)).fetchone()
    conn.close()
    return row is not None


def _insert_knowledge(category: str = "projects", content: str = "content") -> int:
    conn = knowledge.get_db()
    cur = conn.execute(
        "INSERT INTO knowledge (category, title, content, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (category, None, content, "2026-08-09T00:00:00", "2026-08-09T00:00:00"),
    )
    entry_id = cur.lastrowid
    conn.commit()
    conn.close()
    return entry_id


def _knowledge_exists(entry_id: int) -> bool:
    conn = knowledge.get_db()
    row = conn.execute("SELECT id FROM knowledge WHERE id=?", (entry_id,)).fetchone()
    conn.close()
    return row is not None


# ── Part 1: stage_action / list_pending_actions / reject_pending_action ───

class TestStageAction:
    def test_returns_confirmation_naming_id_and_kind(self, pending_env):
        result = pending_actions.stage_action("delete_memory", {"memory_id": 5})
        assert "Staged as action #" in result
        assert "delete_memory" in result
        assert "Not applied" in result

    def test_persists_kind_payload_and_reason(self, pending_env):
        pending_actions.stage_action("reset_chat", {}, reason="testing")
        queue = pending_actions._load_queue()
        assert len(queue) == 1
        entry = next(iter(queue.values()))
        assert entry["kind"] == "reset_chat"
        assert entry["payload"] == {}
        assert entry["reason"] == "testing"
        assert "staged_at" in entry

    def test_each_call_gets_a_distinct_id(self, pending_env):
        pending_actions.stage_action("reset_chat", {})
        pending_actions.stage_action("reset_chat", {})
        assert len(pending_actions._load_queue()) == 2

    def test_writes_audit_log_entry(self, pending_env):
        pending_actions.stage_action("edit_prompt", {"new_prompt": "x"})
        with open(pending_actions.AUDIT_LOG_PATH) as f:
            lines = f.readlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["event"] == "staged"
        assert entry["kind"] == "edit_prompt"


class TestListPendingActions:
    def test_empty_queue(self, pending_env):
        assert pending_actions.list_pending_actions() == "No pending actions."

    def test_shows_staged_entries(self, pending_env):
        pending_actions.stage_action("delete_knowledge", {"entry_id": 3})
        result = pending_actions.list_pending_actions()
        assert "delete_knowledge" in result
        assert '"entry_id": 3' in result


class TestRejectPendingAction:
    def test_unknown_id_reports_not_found(self, pending_env):
        result = pending_actions.reject_pending_action("doesnotexist")
        assert "No pending action" in result

    def test_removes_entry_from_queue(self, pending_env):
        pending_actions.stage_action("reset_chat", {})
        aid = _staged_id()
        result = pending_actions.reject_pending_action(aid)
        assert f"Rejected action #{aid}" in result
        assert pending_actions._load_queue() == {}

    def test_writes_rejected_audit_entry(self, pending_env):
        pending_actions.stage_action("reset_chat", {})
        aid = _staged_id()
        pending_actions.reject_pending_action(aid)
        with open(pending_actions.AUDIT_LOG_PATH) as f:
            events = [json.loads(l)["event"] for l in f.readlines()]
        assert events == ["staged", "rejected"]


# ── Part 2a: edit_prompt ────────────────────────────────────────────────

class TestEditPromptGate:
    def test_tool_call_only_stages(self, pending_env, agent):
        original = agent.ctx.system_prompt
        result = meta.edit_prompt(agent.ctx, "NEW PROMPT")
        assert "Staged as action #" in result
        assert agent.ctx.system_prompt == original

    def test_apply_action_mutates_real_state(self, pending_env, agent):
        meta.edit_prompt(agent.ctx, "NEW PROMPT")
        aid = _staged_id()
        result = pending_actions._apply_action(aid, agent)
        assert result == "System prompt updated."
        assert agent.ctx.system_prompt == "NEW PROMPT"
        assert pending_actions._load_queue() == {}

    def test_reject_does_not_mutate(self, pending_env, agent):
        original = agent.ctx.system_prompt
        meta.edit_prompt(agent.ctx, "NEW PROMPT")
        aid = _staged_id()
        pending_actions.reject_pending_action(aid)
        assert agent.ctx.system_prompt == original


# ── Part 2b: reset_chat ─────────────────────────────────────────────────

class TestResetChatGate:
    def test_tool_call_only_stages(self, pending_env, agent):
        agent.ctx.add_user("hello")
        result = meta.reset_chat(agent.ctx)
        assert "Staged as action #" in result
        assert agent.ctx.history != []

    def test_apply_action_mutates_real_state(self, pending_env, agent):
        agent.ctx.add_user("hello")
        meta.reset_chat(agent.ctx)
        aid = _staged_id()
        result = pending_actions._apply_action(aid, agent)
        assert result == "Chat history cleared."
        assert agent.ctx.history == []
        assert pending_actions._load_queue() == {}

    def test_reject_does_not_mutate(self, pending_env, agent):
        agent.ctx.add_user("hello")
        meta.reset_chat(agent.ctx)
        aid = _staged_id()
        pending_actions.reject_pending_action(aid)
        assert agent.ctx.history != []


# ── Part 2c: delete_knowledge ───────────────────────────────────────────

class TestDeleteKnowledgeGate:
    def test_tool_call_only_stages(self, pending_env, db_env, agent):
        entry_id = _insert_knowledge()
        result = knowledge.delete_knowledge(entry_id=entry_id)
        assert "Staged as action #" in result
        assert _knowledge_exists(entry_id)

    def test_apply_action_mutates_real_state(self, pending_env, db_env, agent):
        entry_id = _insert_knowledge()
        knowledge.delete_knowledge(entry_id=entry_id)
        aid = _staged_id()
        result = pending_actions._apply_action(aid, agent)
        assert result == f"Entry {entry_id} deleted."
        assert not _knowledge_exists(entry_id)
        assert pending_actions._load_queue() == {}

    def test_apply_action_by_category(self, pending_env, db_env, agent):
        id1 = _insert_knowledge(category="scratch")
        id2 = _insert_knowledge(category="scratch")
        knowledge.delete_knowledge(category="scratch")
        aid = _staged_id()
        result = pending_actions._apply_action(aid, agent)
        assert "Deleted 2 entries" in result
        assert not _knowledge_exists(id1)
        assert not _knowledge_exists(id2)

    def test_reject_does_not_mutate(self, pending_env, db_env, agent):
        entry_id = _insert_knowledge()
        knowledge.delete_knowledge(entry_id=entry_id)
        aid = _staged_id()
        pending_actions.reject_pending_action(aid)
        assert _knowledge_exists(entry_id)


# ── Part 2d: delete_memory ──────────────────────────────────────────────

class TestDeleteMemoryGate:
    def test_tool_call_only_stages(self, pending_env, db_env, agent):
        memory_id = _insert_memory()
        result = memory.delete_memory(memory_id)
        assert "Staged as action #" in result
        assert _memory_exists(memory_id)

    def test_apply_action_mutates_real_state(self, pending_env, db_env, agent):
        memory_id = _insert_memory()
        memory.delete_memory(memory_id)
        aid = _staged_id()
        result = pending_actions._apply_action(aid, agent)
        assert result == f"Memory {memory_id} deleted."
        assert not _memory_exists(memory_id)
        assert pending_actions._load_queue() == {}

    def test_reject_does_not_mutate(self, pending_env, db_env, agent):
        memory_id = _insert_memory()
        memory.delete_memory(memory_id)
        aid = _staged_id()
        pending_actions.reject_pending_action(aid)
        assert _memory_exists(memory_id)


# ── Part 2e: unknown kind / missing action id (defensive) ──────────────

class TestApplyActionErrors:
    def test_apply_unknown_action_id(self, pending_env, agent):
        result = pending_actions._apply_action("nosuchid", agent)
        assert result.startswith("[Error: no pending action")

    def test_apply_unknown_kind(self, pending_env, agent):
        pending_actions.stage_action("frobnicate", {})
        aid = _staged_id()
        result = pending_actions._apply_action(aid, agent)
        assert result == "[Error: unknown action kind 'frobnicate'.]"
        # Unknown-kind entries are left staged, not silently dropped.
        assert aid in pending_actions._load_queue()
