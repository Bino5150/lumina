"""core/context_reconstruction.py -- CONTEXT-LIFECYCLE-A2 unit tests.

Every DB-backed test isolates config.DB_PATH to tmp_path (same convention
tests/test_context_inventory.py and tests/test_compaction_functional.py
already use). ui/main_window.py-level integration coverage (the neutral
kernel actually being consumed by _load_chat(), and visible-transcript
equivalence) lives in tests/test_manual_compaction_ui.py -- this file is
the kernel in isolation, with no Qt import anywhere.
"""
import ast
import sqlite3

import config
import tools.memory as memory
from core.context_reconstruction import (
    CONVERSATION_ROLES,
    ReconstructionResult,
    eligible_durable_rows,
    load_durable_rows,
    reconstruct_chat_context,
    resolve_context_skip,
)


def _seed_chat(monkeypatch, tmp_path, name, messages):
    """messages: list of (role, content, metadata_dict_or_None). Returns
    chat_id. Uses the real tools.memory functions so fixture rows are
    shaped exactly like production, not hand-approximated (same
    convention as tests/test_context_inventory.py::_seed_chat)."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / name))
    memory.init_chat_db()
    chat_id = memory.create_chat("test chat")
    for role, content, meta in messages:
        memory.save_chat_message(chat_id, role, content, metadata=meta)
    return chat_id


# ── kernel basics ────────────────────────────────────────────────────────

def test_empty_chat_reconstructs_empty_durable_history(tmp_path, monkeypatch):
    chat_id = _seed_chat(monkeypatch, tmp_path, "empty.db", [])
    result = reconstruct_chat_context(chat_id, context_skip=0)
    assert result.messages == []
    assert result.rows == []
    assert result.eligible_rows == []
    assert result.restored_row_count == 0
    assert result.skipped_row_count == 0


def test_one_user_row_reconstructs_identically(tmp_path, monkeypatch):
    chat_id = _seed_chat(monkeypatch, tmp_path, "u.db", [("user", "hello", None)])
    result = reconstruct_chat_context(chat_id, context_skip=0)
    assert result.messages == [{"role": "user", "content": "hello"}]


def test_one_assistant_row_reconstructs_identically(tmp_path, monkeypatch):
    chat_id = _seed_chat(monkeypatch, tmp_path, "a.db", [("assistant", "hi there", None)])
    result = reconstruct_chat_context(chat_id, context_skip=0)
    assert result.messages == [{"role": "assistant", "content": "hi there"}]


def test_alternating_rows_preserve_exact_order(tmp_path, monkeypatch):
    msgs = [("user", "u1", None), ("assistant", "a1", None),
            ("user", "u2", None), ("assistant", "a2", None)]
    chat_id = _seed_chat(monkeypatch, tmp_path, "alt.db", msgs)
    result = reconstruct_chat_context(chat_id, context_skip=0)
    assert result.messages == [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
    ]


def test_metadata_survives_as_a_load_bearing_fingerprint_input(tmp_path, monkeypatch):
    """Required proof #5. Ordinary reconstruction never reads metadata
    back into ctx.history (the {"cancelled": True} tag is write-only from
    _load_chat()'s perspective) -- but the fingerprint must still change
    if metadata changes underneath a chat, exactly like pre-A2 A1."""
    chat_id = _seed_chat(monkeypatch, tmp_path, "meta.db",
                          [("user", "hello", None), ("assistant", "partial", None)])
    before = reconstruct_chat_context(chat_id, context_skip=0)

    conn = sqlite3.connect(str(tmp_path / "meta.db"))
    conn.execute('UPDATE chat_messages SET metadata=? WHERE role="assistant"',
                 ('{"cancelled": true}',))
    conn.commit()
    conn.close()

    after = reconstruct_chat_context(chat_id, context_skip=0)
    assert before.durable_spine_fingerprint != after.durable_spine_fingerprint
    # but the restored message content itself is unaffected -- metadata
    # is fingerprint-load-bearing, not restoration-load-bearing.
    assert before.messages == after.messages


def test_unsupported_role_does_not_become_active_durable_history(tmp_path, monkeypatch):
    chat_id = _seed_chat(monkeypatch, tmp_path, "sys.db",
                          [("system", "should never restore", None),
                           ("user", "u1", None)])
    result = reconstruct_chat_context(chat_id, context_skip=0)
    assert result.messages == [{"role": "user", "content": "u1"}]


# ── execution baggage exclusion ─────────────────────────────────────────

def test_tool_role_row_cannot_enter_reconstructed_active_history(tmp_path, monkeypatch):
    """Required proof #7. A role="tool" row is not part of ordinary chat
    persistence in production (tools/memory.py::save_chat_message() is
    only ever called with role="user"/"assistant"), but if one ever
    landed in the table -- corrupt data, a future bug -- it must still
    never be promoted into active context."""
    chat_id = _seed_chat(monkeypatch, tmp_path, "tool.db",
                          [("user", "u1", None),
                           ("tool", "[TOOL_OUTPUT] some result", None),
                           ("assistant", "a1", None)])
    result = reconstruct_chat_context(chat_id, context_skip=0)
    assert result.messages == [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
    ]
    assert all(r["role"] != "tool" for r in result.eligible_rows)


def test_assistant_row_never_gains_tool_calls_shape_from_persistence(tmp_path, monkeypatch):
    """Required proof #8. chat_messages has no tool_calls column -- even
    content that looks like tool-call JSON is restored as ordinary final
    assistant text, never promoted into an assistant_tool_call-shaped
    message (ContextManager.add_assistant() has no tool_calls parameter
    at all -- there is no code path here that could invent one)."""
    tool_call_shaped = '{"tool_calls": [{"id": "c1", "function": {"name": "x"}}]}'
    chat_id = _seed_chat(monkeypatch, tmp_path, "shape.db",
                          [("assistant", tool_call_shaped, None)])
    result = reconstruct_chat_context(chat_id, context_skip=0)
    assert result.messages == [{"role": "assistant", "content": tool_call_shaped}]
    assert "tool_calls" not in result.messages[0]


def test_think_role_row_never_appears_in_reconstructed_history(tmp_path, monkeypatch):
    """Required proof #9. Think is never a ContextManager entry in live
    traffic (CONTEXT-LIFECYCLE-A0 finding); a synthetic role="think" row
    is exercised here as the negative-space proof."""
    chat_id = _seed_chat(monkeypatch, tmp_path, "think.db",
                          [("think", "internal reasoning", None), ("user", "u1", None)])
    result = reconstruct_chat_context(chat_id, context_skip=0)
    assert result.messages == [{"role": "user", "content": "u1"}]


def test_commentary_role_row_never_appears_in_reconstructed_history(tmp_path, monkeypatch):
    """Required proof #10, same reasoning as Think above."""
    chat_id = _seed_chat(monkeypatch, tmp_path, "commentary.db",
                          [("commentary", "narrated aside", None), ("user", "u1", None)])
    result = reconstruct_chat_context(chat_id, context_skip=0)
    assert result.messages == [{"role": "user", "content": "u1"}]


# ── manual compaction ────────────────────────────────────────────────────

def test_context_skip_n_is_preserved_across_the_kernel(tmp_path, monkeypatch):
    """Required proof #11/#13."""
    msgs = [("user", "u1", None), ("assistant", "a1", None),
            ("user", "u2", None), ("assistant", "a2", None),
            ("user", "u3", None), ("assistant", "a3", None)]
    chat_id = _seed_chat(monkeypatch, tmp_path, "skip.db", msgs)

    for skip in range(0, 7):
        result = reconstruct_chat_context(chat_id, context_skip=skip)
        rows = load_durable_rows(chat_id)
        expected = eligible_durable_rows(rows, skip)
        assert [(m["role"], m["content"]) for m in result.messages] == \
               [(r["role"], r["content"]) for r in expected]


def test_empty_content_rows_consume_the_same_indexing_semantics(tmp_path, monkeypatch):
    """Required proof #12. Matches tests/test_context_inventory.py::
    test_empty_content_row_still_consumes_conversation_index, exercised
    here at the DB-backed kernel entrypoint instead of the pure function."""
    chat_id = _seed_chat(monkeypatch, tmp_path, "emptybound.db",
                          [("user", "u1", None), ("assistant", "", None), ("user", "u2", None)])
    result = reconstruct_chat_context(chat_id, context_skip=1)
    # skip=1 skips u1 (index 0); the empty assistant row consumes index 1
    # regardless of its own content; u2 (index 2) is the only survivor.
    assert result.messages == [{"role": "user", "content": "u2"}]
    assert result.skipped_row_count == 2  # u1 (skipped by context_skip) + empty assistant


def test_fingerprint_identical_for_identical_durable_state(tmp_path, monkeypatch):
    """Required proof #14."""
    msgs = [("user", "hello", None), ("assistant", "hi there", None)]
    chat_id = _seed_chat(monkeypatch, tmp_path, "fp.db", msgs)
    fp1 = reconstruct_chat_context(chat_id, context_skip=0).durable_spine_fingerprint
    fp2 = reconstruct_chat_context(chat_id, context_skip=0).durable_spine_fingerprint
    assert fp1 == fp2
    assert fp1 != ""


def test_no_compaction_marker_defaults_to_zero_skip(tmp_path, monkeypatch):
    chat_id = _seed_chat(monkeypatch, tmp_path, "nomarker.db",
                          [("user", "u1", None), ("assistant", "a1", None)])
    # No manual_compaction Drawer exists for this chat_id -- resolve_context_skip
    # must fall through to latest_manual_compaction_skip()'s own "no rows -> 0".
    assert resolve_context_skip(chat_id) == 0
    result = reconstruct_chat_context(chat_id, context_skip=resolve_context_skip(chat_id))
    assert result.restored_row_count == 2


def test_skip_beyond_eligible_content_restores_nothing(tmp_path, monkeypatch):
    chat_id = _seed_chat(monkeypatch, tmp_path, "beyond.db",
                          [("user", "u1", None), ("assistant", "a1", None)])
    result = reconstruct_chat_context(chat_id, context_skip=99)
    assert result.messages == []
    assert result.restored_row_count == 0


def test_resolve_context_skip_falls_back_to_zero_on_read_failure(monkeypatch):
    """Reproduces _load_chat()'s exact pre-A2 graceful-degradation
    contract on a checkpoint read failure."""
    def _boom(chat_id):
        raise RuntimeError("palace db unreachable")
    monkeypatch.setattr("core.manual_compaction.latest_manual_compaction_skip", _boom)
    assert resolve_context_skip(123) == 0


def test_cancelled_message_metadata_does_not_block_restoration(tmp_path, monkeypatch):
    """A cancelled assistant row (metadata={"cancelled": True}) is restored
    like any ordinary assistant row -- _load_chat() has never read that
    metadata back for restoration purposes, only written it."""
    chat_id = _seed_chat(monkeypatch, tmp_path, "cancelled.db",
                          [("user", "u1", None),
                           ("assistant", "partial reply", {"cancelled": True})])
    result = reconstruct_chat_context(chat_id, context_skip=0)
    assert result.messages == [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "partial reply"},
    ]


def test_normal_assistant_user_ordering_preserved(tmp_path, monkeypatch):
    msgs = [("user", "u1", None), ("assistant", "a1", None), ("user", "u2", None)]
    chat_id = _seed_chat(monkeypatch, tmp_path, "order.db", msgs)
    result = reconstruct_chat_context(chat_id, context_skip=0)
    assert [m["role"] for m in result.messages] == ["user", "assistant", "user"]


# ── isolation ────────────────────────────────────────────────────────────

def test_kernel_callable_without_qt_or_mainwindow(tmp_path, monkeypatch):
    """Required proof #20. No PySide6 import appears anywhere in this
    file or in core/context_reconstruction.py -- calling the kernel here
    at all is the proof."""
    chat_id = _seed_chat(monkeypatch, tmp_path, "noqt.db", [("user", "u1", None)])
    result = reconstruct_chat_context(chat_id, context_skip=0)
    assert isinstance(result, ReconstructionResult)


def test_kernel_performs_no_writes(tmp_path, monkeypatch):
    """Required proof #21. chat_messages content is byte-identical before
    and after repeated reconstruction calls."""
    msgs = [("user", "u1", None), ("assistant", "a1", None)]
    chat_id = _seed_chat(monkeypatch, tmp_path, "nowrite.db", msgs)
    db_path = str(tmp_path / "nowrite.db")

    def _snapshot():
        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT id, role, content, metadata, created_at FROM chat_messages "
                             "ORDER BY id").fetchall()
        conn.close()
        return rows

    before = _snapshot()
    for _ in range(3):
        reconstruct_chat_context(chat_id, context_skip=0)
    after = _snapshot()
    assert before == after


def test_kernel_source_has_no_model_or_tool_dispatch_imports():
    """Required proof #22/#23. Static architecture-boundary check: the
    kernel must never import a backend/model-call or tool-dispatch
    surface -- if it ever does, that is new authority this neutral
    kernel was never supposed to gain."""
    with open("core/context_reconstruction.py") as f:
        tree = ast.parse(f.read(), filename="core/context_reconstruction.py")

    banned_prefixes = ("core.backends", "core.agent", "core.dreaming",
                        "tools.registry", "core.operator_commands")
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    for mod in imported:
        assert not mod.startswith(banned_prefixes), (
            f"core/context_reconstruction.py imports {mod!r} -- a neutral "
            "reconstruction kernel must not gain model-call or tool-dispatch reach"
        )


def test_kernel_module_has_no_qt_import():
    """PySide6/PyQt must never appear in the kernel's own import list."""
    with open("core/context_reconstruction.py") as f:
        tree = ast.parse(f.read(), filename="core/context_reconstruction.py")

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert not any("PySide" in m or "PyQt" in m for m in imported)


# ── regression fixture matrix (legacy _load_chat() behavior) ─────────────

def _reference_load_chat_eligibility(rows, context_skip):
    """Independent re-implementation of pre-A2 _load_chat()'s exact
    restore logic (ui/main_window.py, historical lines 890-930) -- the
    parity oracle, read directly from the pre-refactor source, not
    copy-pasted from eligible_durable_rows() itself. Mirrors tests/
    test_context_inventory.py's own oracle for the same reason."""
    out = []
    conversation_index = 0
    for m in rows:
        role = m.get("role")
        is_conversation = role in CONVERSATION_ROLES
        restore_to_context = (not is_conversation or conversation_index >= context_skip)
        if is_conversation:
            conversation_index += 1
        content = m.get("content") or ""
        if not content:
            continue
        if role in CONVERSATION_ROLES and restore_to_context:
            out.append(m)
    return out


def test_regression_matrix_matches_legacy_load_chat_oracle(tmp_path, monkeypatch):
    """Required section 16: fixture matrix comparing the new kernel
    against the documented pre-refactor _load_chat() semantics, across
    every case section 15 calls out (skip=0, mid-skip, over-skip, empty
    content at the boundary, alternating roles)."""
    msgs = [("user", "u1", None), ("assistant", "a1", None),
            ("user", "u2", None), ("assistant", "", None),  # empty content
            ("user", "u3", None), ("assistant", "a3", None)]
    chat_id = _seed_chat(monkeypatch, tmp_path, "matrix.db", msgs)
    rows = load_durable_rows(chat_id)

    for skip in range(0, 6):
        result = reconstruct_chat_context(chat_id, context_skip=skip)
        want = _reference_load_chat_eligibility(rows, skip)
        assert [m["content"] for m in result.messages] == [r["content"] for r in want], \
            f"mismatch at context_skip={skip}"
