"""CONTEXT-LIFECYCLE-A1 -- core/context_inventory.py unit tests.

Every DB-backed test isolates config.DB_PATH to tmp_path (same convention
tests/test_compaction_functional.py already uses) so nothing here ever
touches real app data. FlightRecorder tests construct their own isolated
instance (tests/test_flight_recorder.py's own convention) rather than the
production singleton.
"""
import sqlite3

import config
import tools.memory as memory
from core.context import ContextManager, estimate_message_tokens
from core.flight_recorder import FlightRecorder
from core.context_inventory import (
    MESSAGE_CLASSES,
    classify_message,
    inventory_active_history,
    eligible_durable_rows,
    durable_spine_fingerprint,
    observe_reconstruction_boundary,
    record_inventory_event,
)


# ── classify_message ──────────────────────────────────────────────────────

def test_classify_user():
    assert classify_message({"role": "user", "content": "hi"}) == "user"


def test_classify_assistant_final():
    assert classify_message({"role": "assistant", "content": "hi"}) == "assistant_final"


def test_classify_assistant_tool_call_carrier():
    msg = {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]}
    assert classify_message(msg) == "assistant_tool_call"


def test_classify_tool_result():
    msg = {"role": "tool", "tool_call_id": "c1", "name": "x", "content": "result"}
    assert classify_message(msg) == "tool_result"


def test_classify_cancelled_tool_result_is_still_tool_result_class():
    """add_cancelled_tool_result() produces the same role="tool" shape as a
    real tool result -- structurally indistinguishable, and this module
    does not invent a sixth class to tell them apart (mission: don't add a
    competing taxonomy)."""
    msg = {"role": "tool", "tool_call_id": "c1", "name": "x",
           "content": "[Cancelled by operator before execution.]"}
    assert classify_message(msg) == "tool_result"


def test_classify_unrecognized_role_is_other():
    assert classify_message({"role": "system", "content": "x"}) == "other"


# ── inventory_active_history ──────────────────────────────────────────────

def _tool_heavy_history():
    cm = ContextManager(owner=False)
    cm.add_user("do the thing")
    cm.add_tool_call({"role": "assistant", "content": "",
                       "tool_calls": [{"id": "c1", "type": "function",
                                       "function": {"name": "search", "arguments": "{}"}}]})
    cm.add_tool_result("c1", "search", "some result payload")
    cm.add_assistant("done, here's the answer")
    return cm


def test_tool_call_and_tool_result_counted_separately_from_durable_rows():
    """Required proof #1."""
    cm = _tool_heavy_history()
    inv = inventory_active_history(cm.history)
    assert inv["by_class"]["user"]["count"] == 1
    assert inv["by_class"]["assistant_final"]["count"] == 1
    assert inv["by_class"]["assistant_tool_call"]["count"] == 1
    assert inv["by_class"]["tool_result"]["count"] == 1
    assert inv["durable_class_entries"] == 2   # user + assistant_final
    assert inv["live_only_class_entries"] == 2  # tool_call + tool_result


def test_think_and_commentary_never_falsely_represented_as_history_entries():
    """Required proof #2. Think/Commentary are never ContextManager
    entries (A0 finding #3/#4) -- this must report that as an explicit
    fact, not a fake zero-token row indistinguishable from "measured and
    empty"."""
    cm = _tool_heavy_history()
    inv = inventory_active_history(cm.history)
    assert inv["think_resident"] is False
    assert inv["commentary_resident"] is False
    assert set(inv["by_class"].keys()) == set(MESSAGE_CLASSES)
    assert "think" not in inv["by_class"]
    assert "commentary" not in inv["by_class"]


def test_token_totals_reconcile_with_existing_estimator():
    """Required proof #3 -- reuse core.context.estimate_message_tokens(),
    don't reinvent it."""
    cm = _tool_heavy_history()
    inv = inventory_active_history(cm.history)
    expected = sum(estimate_message_tokens(m) for m in cm.history)
    assert inv["total_tokens"] == expected


def test_empty_history_reports_all_zero():
    inv = inventory_active_history([])
    assert inv["total_entries"] == 0
    assert inv["total_tokens"] == 0
    assert all(v["count"] == 0 for v in inv["by_class"].values())


# ── eligible_durable_rows -- parity with ui/main_window.py::_load_chat() ──

def _reference_load_chat_eligibility(rows, context_skip):
    """Independent re-implementation of _load_chat()'s exact restore logic
    (ui/main_window.py:890-930), read directly from live source during
    CONTEXT-LIFECYCLE-A0/A1 rather than copy-pasted from
    eligible_durable_rows() itself -- this is the parity oracle, not a
    duplicate of the thing under test."""
    out = []
    conversation_index = 0
    for m in rows:
        role = m.get("role")
        is_conversation = role in ("user", "assistant")
        restore_to_context = (not is_conversation or conversation_index >= context_skip)
        if is_conversation:
            conversation_index += 1
        content = m.get("content") or ""
        if not content:
            continue
        if role in ("user", "assistant") and restore_to_context:
            out.append(m)
    return out


def _rows(*pairs, metadata=None):
    """pairs of (role, content) -> row dicts with fake ids, in order."""
    meta = metadata or {}
    return [{"id": i, "role": r, "content": c, "metadata": meta.get(i, "")}
            for i, (r, c) in enumerate(pairs, start=1)]


def test_eligible_rows_matches_load_chat_reference_across_skip_values():
    """Required proof #7."""
    rows = _rows(
        ("user", "u1"), ("assistant", "a1"),
        ("user", "u2"), ("assistant", ""),   # empty-content row -- still consumes an index
        ("user", "u3"), ("assistant", "a3"),
    )
    for skip in range(0, 5):
        got = [r["content"] for r in eligible_durable_rows(rows, skip)]
        want = [m["content"] for m in _reference_load_chat_eligibility(rows, skip)]
        assert got == want, f"mismatch at context_skip={skip}"


def test_empty_content_row_still_consumes_conversation_index():
    rows = _rows(("user", "u1"), ("assistant", ""), ("user", "u2"))
    # skip=1 should skip u1 only; the empty assistant row consumed index 1
    # regardless of its own content, so u2 (index 2) is the only survivor.
    got = [r["content"] for r in eligible_durable_rows(rows, context_skip=1)]
    assert got == ["u2"]


def test_non_conversation_role_never_restored_regardless_of_skip():
    rows = _rows(("user", "u1"), ("tool", "some tool text"))
    got = eligible_durable_rows(rows, context_skip=0)
    assert [r["role"] for r in got] == ["user"]


# ── durable_spine_fingerprint -- DB-backed, isolated ────────────────────────

def _seed_chat(monkeypatch, tmp_path, name, messages):
    """messages: list of (role, content, metadata_dict_or_None). Returns
    chat_id. Uses the real tools.memory functions (init_chat_db/create_chat/
    save_chat_message) so fixture rows are shaped exactly like production,
    not hand-approximated."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / name))
    memory.init_chat_db()
    chat_id = memory.create_chat("test chat")
    for role, content, meta in messages:
        memory.save_chat_message(chat_id, role, content, metadata=meta)
    return chat_id


def test_identical_eligible_rows_reproduce_same_fingerprint(tmp_path, monkeypatch):
    """Required proof #6."""
    msgs = [("user", "hello", None), ("assistant", "hi there", None)]
    chat_id = _seed_chat(monkeypatch, tmp_path, "a.db", msgs)
    fp1 = durable_spine_fingerprint(chat_id, context_skip=0)
    fp2 = durable_spine_fingerprint(chat_id, context_skip=0)
    assert fp1["fingerprint_sha256"] == fp2["fingerprint_sha256"]
    assert fp1["eligible_row_count"] == fp2["eligible_row_count"] == 2


def test_content_change_alters_fingerprint(tmp_path, monkeypatch):
    """Required proof #5 / mutation proof."""
    chat_id = _seed_chat(monkeypatch, tmp_path, "b.db",
                          [("user", "hello", None), ("assistant", "hi there", None)])
    before = durable_spine_fingerprint(chat_id, context_skip=0)

    conn = sqlite3.connect(str(tmp_path / "b.db"))
    conn.execute("UPDATE chat_messages SET content='hi therE' WHERE role='assistant'")
    conn.commit()
    conn.close()

    after = durable_spine_fingerprint(chat_id, context_skip=0)
    assert before["fingerprint_sha256"] != after["fingerprint_sha256"]


def test_row_order_change_alters_fingerprint(tmp_path, monkeypatch):
    """Required proof #4."""
    chat_id = _seed_chat(monkeypatch, tmp_path, "c.db",
                          [("user", "first", None), ("user", "second", None)])
    before = durable_spine_fingerprint(chat_id, context_skip=0)

    # Swap created_at ordering of the two rows without changing content.
    conn = sqlite3.connect(str(tmp_path / "c.db"))
    rows = conn.execute("SELECT id, created_at FROM chat_messages ORDER BY id").fetchall()
    (id1, ts1), (id2, ts2) = rows
    conn.execute("UPDATE chat_messages SET created_at=? WHERE id=?", (ts2, id1))
    conn.execute("UPDATE chat_messages SET created_at=? WHERE id=?", (ts1, id2))
    conn.commit()
    conn.close()

    after = durable_spine_fingerprint(chat_id, context_skip=0)
    assert before["fingerprint_sha256"] != after["fingerprint_sha256"]


def test_cancelled_metadata_change_alters_fingerprint(tmp_path, monkeypatch):
    """Mutation proof: cancelled-message metadata must remain load-bearing
    for the fingerprint, not silently ignored."""
    chat_id = _seed_chat(monkeypatch, tmp_path, "d.db",
                          [("user", "hello", None), ("assistant", "partial", None)])
    before = durable_spine_fingerprint(chat_id, context_skip=0)

    conn = sqlite3.connect(str(tmp_path / "d.db"))
    conn.execute('UPDATE chat_messages SET metadata=? WHERE role="assistant"',
                 ('{"cancelled": true}',))
    conn.commit()
    conn.close()

    after = durable_spine_fingerprint(chat_id, context_skip=0)
    assert before["fingerprint_sha256"] != after["fingerprint_sha256"]


def test_context_skip_changes_eligible_rows_and_fingerprint(tmp_path, monkeypatch):
    """Required proof #7 (DB-backed variant) -- an explicit context_skip
    excludes the skipped prefix from both the count and the hash."""
    msgs = [("user", "u1", None), ("assistant", "a1", None),
            ("user", "u2", None), ("assistant", "a2", None)]
    chat_id = _seed_chat(monkeypatch, tmp_path, "e.db", msgs)

    skip0 = durable_spine_fingerprint(chat_id, context_skip=0)
    skip2 = durable_spine_fingerprint(chat_id, context_skip=2)

    assert skip0["eligible_row_count"] == 4
    assert skip2["eligible_row_count"] == 2
    assert skip0["fingerprint_sha256"] != skip2["fingerprint_sha256"]
    assert skip0["durable_row_count"] == skip2["durable_row_count"] == 4


def test_fingerprint_defaults_to_live_manual_compaction_skip(tmp_path, monkeypatch):
    """context_skip=None must resolve via
    core.manual_compaction.latest_manual_compaction_skip(), not silently
    default to 0 -- that would misreport what _load_chat() actually
    restores whenever a manual compaction has already happened for this
    chat."""
    chat_id = _seed_chat(monkeypatch, tmp_path, "f.db",
                          [("user", "u1", None), ("assistant", "a1", None)])
    monkeypatch.setattr(
        "core.manual_compaction.latest_manual_compaction_skip",
        lambda cid: 2,
    )
    result = durable_spine_fingerprint(chat_id)
    assert result["context_skip"] == 2
    assert result["eligible_row_count"] == 0


# ── observe_reconstruction_boundary ────────────────────────────────────────

def test_observe_reconstruction_boundary_bundles_inventory_and_spine(tmp_path, monkeypatch):
    chat_id = _seed_chat(monkeypatch, tmp_path, "g.db",
                          [("user", "u1", None), ("assistant", "a1", None)])
    cm = ContextManager(owner=False)
    cm.add_user("u1")
    cm.add_assistant("a1")

    result = observe_reconstruction_boundary(cm.history, chat_id, context_skip=0)
    assert result["inventory"]["total_entries"] == 2
    assert result["spine"]["eligible_row_count"] == 2


# ── record_inventory_event -- Flight Recorder integration ─────────────────

def _fr_rows(fr):
    conn = sqlite3.connect(fr.db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def test_record_inventory_event_writes_machine_provenance_row(tmp_path, monkeypatch):
    chat_id = _seed_chat(monkeypatch, tmp_path, "h.db",
                          [("user", "u1", None), ("assistant", "a1", None)])
    cm = ContextManager(owner=False)
    cm.add_user("u1")
    cm.add_assistant("a1")
    bundle = observe_reconstruction_boundary(cm.history, chat_id, context_skip=0)

    fr = FlightRecorder(db_path=str(tmp_path / "fr.db"))
    record_inventory_event(fr, chat_id=chat_id, inventory=bundle["inventory"],
                            spine=bundle["spine"], boundary_reason="test_observation")

    rows = _fr_rows(fr)
    assert len(rows) == 1
    assert rows[0]["event_type"] == "context.inventory.snapshot"
    assert rows[0]["provenance"] == "machine"
    assert rows[0]["chat_id"] == chat_id


def test_record_inventory_event_never_carries_raw_message_content():
    """Required proof #8 -- the actual conversation text must not appear
    anywhere in the recorded fields blob."""
    import tempfile
    secret_text = "the secret durable content nobody should see in telemetry"
    cm = ContextManager(owner=False)
    cm.add_user(secret_text)
    cm.add_assistant("a reply")
    inv = inventory_active_history(cm.history)
    spine = {"chat_id": 1, "context_skip": 0, "durable_row_count": 2,
             "eligible_row_count": 2, "fingerprint_sha256": "deadbeef"}

    with tempfile.TemporaryDirectory() as d:
        fr = FlightRecorder(db_path=d + "/fr.db")
        record_inventory_event(fr, chat_id=1, inventory=inv, spine=spine,
                                boundary_reason="test")
        rows = _fr_rows(fr)
        assert secret_text not in rows[0]["fields_json"]
        assert "a reply" not in rows[0]["fields_json"]


def test_flight_recorder_own_sanitizer_would_still_reject_a_raw_history_dump():
    """Defense in depth: even if a future bug tried to pass ctx.history
    directly into fields, core/flight_recorder.py's own
    _looks_like_conversation() sanitizer independently refuses it. This
    module never attempts that (see the test above), but this proves the
    safety net under it is real, not assumed."""
    import tempfile
    cm = ContextManager(owner=False)
    cm.add_user("hello")
    cm.add_assistant("hi")

    with tempfile.TemporaryDirectory() as d:
        fr = FlightRecorder(db_path=d + "/fr.db")
        fr.record_machine_event("context.inventory.snapshot",
                                 fields={"accidental_dump": cm.history})
        rows = _fr_rows(fr)
        assert "rejected" in rows[0]["fields_json"]
        assert "hello" not in rows[0]["fields_json"]


# ── controlled live reconstruction experiment ──────────────────────────────

def test_controlled_reconstruction_experiment_reports_observed_reduction(
        tmp_path, monkeypatch, capsys):
    """Simulates a realistic multi-tool-call turn, then a reconstruction
    boundary (the exact _load_chat() restore logic via
    eligible_durable_rows()), and reports the observed before/after
    class/token inventory -- required by the A1 mission ("report the
    observed class/token inventory from at least one controlled live
    reconstruction experiment"). Not dependent on manual UI observation:
    this runs headless as part of the normal test suite."""
    msgs = [("user", "please research and summarize topic X", None),
            ("assistant", "final summary of topic X", None)]
    chat_id = _seed_chat(monkeypatch, tmp_path, "experiment.db", msgs)

    # BEFORE: a live in-memory turn with several tool round-trips still resident.
    cm = ContextManager(owner=False)
    cm.add_user("please research and summarize topic X")
    for i in range(3):
        cm.add_tool_call({"role": "assistant", "content": "",
                           "tool_calls": [{"id": f"c{i}", "type": "function",
                                           "function": {"name": "web_search",
                                                         "arguments": "{\"q\": \"topic X\"}"}}]})
        cm.add_tool_result(f"c{i}", "web_search", "a fairly long search result " * 20)
    cm.add_assistant("final summary of topic X")

    before = inventory_active_history(cm.history)

    # AFTER: reconstruct the way _load_chat() actually would -- only the
    # durable user/assistant rows, via eligible_durable_rows().
    rows = [{"id": i, "role": r, "content": c, "metadata": ""}
            for i, (r, c) in enumerate([("user", "please research and summarize topic X"),
                                         ("assistant", "final summary of topic X")], start=1)]
    eligible = eligible_durable_rows(rows, context_skip=0)
    cm_after = ContextManager(owner=False)
    for row in eligible:
        if row["role"] == "user":
            cm_after.add_user(row["content"])
        else:
            cm_after.add_assistant(row["content"])
    after = inventory_active_history(cm_after.history)

    print(f"\n[A1 EXPERIMENT] before: entries={before['total_entries']} "
          f"tokens={before['total_tokens']} live_only={before['live_only_class_entries']}")
    print(f"[A1 EXPERIMENT] after:  entries={after['total_entries']} "
          f"tokens={after['total_tokens']} live_only={after['live_only_class_entries']}")

    assert before["live_only_class_entries"] == 6   # 3x tool_call + 3x tool_result
    assert after["live_only_class_entries"] == 0
    assert after["total_entries"] == 2
    assert after["total_tokens"] < before["total_tokens"]
