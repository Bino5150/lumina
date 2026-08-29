"""
AGENT-FLIGHT-RECORDER-01A1 -- core recorder unit tests.

Every test constructs its own isolated FlightRecorder(db_path=tmp_path/...)
-- never core.flight_recorder.get_recorder() (the real production
singleton bound to DATA_DIR/telemetry/) -- so nothing here ever touches
real on-disk state. This mirrors the exact isolation convention
tests/test_db.py and tests/test_idempotency.py already use for their own
sqlite-backed modules (monkeypatch/explicit path override, never a shared
global).
"""
import json
import sqlite3
import threading
import time

import pytest

from core.flight_recorder import (
    FlightRecorder,
    MODEL_EVENT_TYPES,
    hash_args,
    bounded_repr,
    FULL_RETENTION_SECONDS,
    ERROR_RETENTION_SECONDS,
    MAX_FIELD_STRING_CHARS,
    MAX_FIELDS_JSON_BYTES,
)


def _fr(tmp_path, name="fr.db", **kwargs):
    return FlightRecorder(db_path=str(tmp_path / name), **kwargs)


def _rows(fr):
    conn = sqlite3.connect(fr.db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── runtime_id / turn_id (mission section 2, tests 1-3) ─────────────────

def test_runtime_id_stable_across_one_recorder_lifetime(tmp_path):
    fr = _fr(tmp_path)
    fr.record_machine_event("tool.call", fields={"n": 1})
    fr.record_machine_event("tool.call", fields={"n": 2})
    rows = _rows(fr)
    assert len(rows) == 2
    assert rows[0]["runtime_id"] == rows[1]["runtime_id"] == fr.runtime_id


def test_separate_runtime_gets_different_id(tmp_path):
    fr_a = _fr(tmp_path, "a.db")
    fr_b = _fr(tmp_path, "b.db")
    assert fr_a.runtime_id != fr_b.runtime_id


def test_turn_ids_are_distinct():
    from core.flight_recorder import new_turn_id
    ids = {new_turn_id() for _ in range(50)}
    assert len(ids) == 50


# ── seq ordering across concurrent writers (test 4) ──────────────────────

def test_seq_total_ordering_across_concurrent_writers(tmp_path):
    fr = _fr(tmp_path)
    n_threads, n_per_thread = 8, 25

    def worker(tid):
        for i in range(n_per_thread):
            fr.record_machine_event("tool.call", fields={"thread": tid, "i": i})

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rows = _rows(fr)
    assert len(rows) == n_threads * n_per_thread
    seqs = [r["seq"] for r in rows]
    assert len(set(seqs)) == len(seqs), "seq values must be unique"
    assert seqs == sorted(seqs), "seq must be monotonically increasing"

    # Within one thread's own writes, program order is preserved -- i.e.
    # each thread's own (thread, i) pairs appear in ascending i order when
    # read back in seq order (the guarantee the shared lock actually
    # provides: no reordering of one writer's own sequence of calls).
    per_thread_i = {}
    for r in rows:
        f = json.loads(r["fields_json"])
        per_thread_i.setdefault(f["thread"], []).append(f["i"])
    for tid, seen in per_thread_i.items():
        assert seen == list(range(n_per_thread)), f"thread {tid} events out of program order"


# ── retention: 7-day full, 90-day warning/error (tests 5-7) ──────────────

def test_normal_event_expires_at_7_days(tmp_path):
    fr = _fr(tmp_path)
    before = time.time()
    fr.record_machine_event("tool.call", severity="info", fields={})
    after = time.time()
    row = _rows(fr)[0]
    assert before + FULL_RETENTION_SECONDS <= row["expires_at"] <= after + FULL_RETENTION_SECONDS
    assert row["severity"] == "info"


def test_debug_event_also_uses_7_day_retention(tmp_path):
    fr = _fr(tmp_path)
    fr.record_machine_event("tool.call", severity="debug", fields={})
    row = _rows(fr)[0]
    assert abs(row["expires_at"] - (row["ts"] + FULL_RETENTION_SECONDS)) < 1.0


def test_warning_and_error_events_expire_at_90_days(tmp_path):
    fr = _fr(tmp_path)
    fr.record_machine_event("tool.call", severity="warning", fields={})
    fr.record_machine_event("tool.call", severity="error", fields={})
    rows = _rows(fr)
    for row in rows:
        assert abs(row["expires_at"] - (row["ts"] + ERROR_RETENTION_SECONDS)) < 1.0


def test_pruning_preserves_younger_errors_deletes_old_normal_events(tmp_path):
    fr = _fr(tmp_path)
    now = time.time()

    # An "info" row whose expires_at is already in the past (as if written
    # 8 days ago) -- must be pruned.
    fr._conn.execute(
        "INSERT INTO events (ts, runtime_id, event_type, severity, provenance, "
        "fields_json, expires_at) VALUES (?,?,?,?,?,?,?)",
        (now - 8 * 86400, fr.runtime_id, "tool.call", "info", "machine", "{}", now - 86400),
    )
    # A "warning" row from 8 days ago too, but its 90-day window means
    # expires_at is still comfortably in the future -- must survive.
    fr._conn.execute(
        "INSERT INTO events (ts, runtime_id, event_type, severity, provenance, "
        "fields_json, expires_at) VALUES (?,?,?,?,?,?,?)",
        (now - 8 * 86400, fr.runtime_id, "tool.call", "warning", "machine", "{}",
         now - 8 * 86400 + ERROR_RETENTION_SECONDS),
    )
    fr._conn.commit()
    assert len(_rows(fr)) == 2

    fr._prune()

    remaining = _rows(fr)
    assert len(remaining) == 1
    assert remaining[0]["severity"] == "warning"


def test_emergency_size_pruning_is_oldest_first(tmp_path):
    fr = _fr(tmp_path, max_db_bytes=1)  # absurdly low -- always "over" once any row exists
    for i in range(20):
        fr.record_machine_event("tool.call", fields={"i": i, "pad": "x" * 200})

    rows_before = _rows(fr)
    assert len(rows_before) == 20

    fr._enforce_size_ceiling()

    rows_after = _rows(fr)
    assert 0 < len(rows_after) < 20, "should have deleted some but not necessarily all rows"
    remaining_is = [json.loads(r["fields_json"])["i"] for r in rows_after]
    # Oldest-first: whatever survived must be a contiguous SUFFIX of the
    # original 0..19 sequence -- i.e. the smallest surviving `i` is greater
    # than every deleted one, never an arbitrary/newest-first pattern.
    deleted_count = 20 - len(remaining_is)
    assert remaining_is == list(range(deleted_count, 20))


def test_seq_never_reused_after_pruning(tmp_path):
    fr = _fr(tmp_path)
    for i in range(10):
        fr.record_machine_event("tool.call", fields={"i": i})
    max_seq_before = max(r["seq"] for r in _rows(fr))

    # Expire everything, prune it all away.
    fr._conn.execute("UPDATE events SET expires_at = 0")
    fr._conn.commit()
    fr._prune()
    assert _rows(fr) == []

    for i in range(10, 15):
        fr.record_machine_event("tool.call", fields={"i": i})

    new_seqs = [r["seq"] for r in _rows(fr)]
    assert all(s > max_seq_before for s in new_seqs), (
        "seq values must never be reused, even for rows inserted after "
        "every prior row was pruned away -- AUTOINCREMENT's whole job"
    )


# ── provenance API separation (mission section 3, tests 9-13) ───────────

def test_machine_api_rejects_model_event_types(tmp_path):
    fr = _fr(tmp_path)
    for et in MODEL_EVENT_TYPES:
        with pytest.raises(ValueError):
            fr.record_machine_event(et, fields={})


def test_model_api_rejects_non_model_event_types(tmp_path):
    fr = _fr(tmp_path)
    for et in ("tool.call", "tool.result", "runtime.startup", "turn.started"):
        with pytest.raises(ValueError):
            fr.record_model_expression(et, text="x")


def test_think_is_model_provenance(tmp_path):
    fr = _fr(tmp_path)
    fr.record_model_expression("turn.think", text="reasoning")
    row = _rows(fr)[0]
    assert row["event_type"] == "turn.think"
    assert row["provenance"] == "model"


def test_commentary_is_model_provenance(tmp_path):
    fr = _fr(tmp_path)
    fr.record_model_expression("turn.commentary", text="checking memory")
    row = _rows(fr)[0]
    assert row["provenance"] == "model"


def test_final_response_text_is_model_provenance(tmp_path):
    fr = _fr(tmp_path)
    fr.record_model_expression("turn.final", text="here's your answer")
    row = _rows(fr)[0]
    assert row["event_type"] == "turn.final"
    assert row["provenance"] == "model"
    assert json.loads(row["fields_json"])["text"] == "here's your answer"


def test_tool_execution_and_result_are_machine_provenance(tmp_path):
    fr = _fr(tmp_path)
    fr.record_machine_event("tool.call", fields={"tool_name": "search_memory"})
    fr.record_machine_event("tool.result", fields={"tool_name": "search_memory", "success": True})
    rows = _rows(fr)
    assert all(r["provenance"] == "machine" for r in rows)


# ── payload policy: redaction / bounding (mission section 7, tests 14-18) ─

def test_secret_key_names_structurally_redacted(tmp_path):
    fr = _fr(tmp_path)
    fr.record_machine_event("tool.call", fields={
        "api_key": "whatever-this-value-is",
        "Authorization": "Bearer abc123",
        "password": "hunter2",
        "client_secret": "xyz",
        "normal_field": "stays visible",
    })
    stored = json.loads(_rows(fr)[0]["fields_json"])
    assert stored["api_key"] == "[REDACTED]"
    assert stored["Authorization"] == "[REDACTED]"
    assert stored["password"] == "[REDACTED]"
    assert stored["client_secret"] == "[REDACTED]"
    assert stored["normal_field"] == "stays visible"


def test_token_count_field_names_are_a_real_redaction_collision(tmp_path):
    """Documents a real gotcha found while wiring core/agent.py's effective-
    budget telemetry: "token" is both a legitimate LLM-context-size unit
    AND one of this module's own structural secret markers (mission
    section 7's literal example list includes it). A field genuinely
    named e.g. "context_used_tokens" gets redacted exactly like a secret
    would -- this is the redaction layer doing its job correctly, NOT a
    bug to fix here; core/agent.py's _effective_tool_budgets() was
    renamed (tool_schema_budget/tool_schema_footprint/context_limit/
    context_used) specifically to avoid this collision rather than
    weakening the marker. This test locks in that the marker itself stays
    broad/safe regardless of what any future caller happens to name a
    field."""
    fr = _fr(tmp_path)
    fr.record_machine_event("tool.call", fields={
        "context_used_tokens": 123,       # collides -- would be redacted
        "context_used": 123,              # does not collide -- safe name
    })
    stored = json.loads(_rows(fr)[0]["fields_json"])
    assert stored["context_used_tokens"] == "[REDACTED]"
    assert stored["context_used"] == 123


def test_known_secret_value_shapes_redacted_under_innocuous_key(tmp_path):
    fr = _fr(tmp_path)
    fr.record_machine_event("tool.call", fields={
        "query": "here is sk-abcdefghijklmnopqrstuvwx1234 embedded in text",
        "header": "Authorization: Bearer aVeryLongTokenValueHere123456",
        "note": "AKIAABCDEFGHIJKLMNOP is an AWS-shaped key",
    })
    stored = json.loads(_rows(fr)[0]["fields_json"])
    assert "sk-abcdefghijklmnopqrstuvwx1234" not in stored["query"]
    assert "[REDACTED]" in stored["query"]
    assert "aVeryLongTokenValueHere123456" not in stored["header"]
    assert "AKIAABCDEFGHIJKLMNOP" not in stored["note"]


def test_bounded_truncated_fields_remain_valid_json(tmp_path):
    fr = _fr(tmp_path)
    huge = "x" * (MAX_FIELD_STRING_CHARS * 5)
    fr.record_machine_event("tool.call", fields={"result": huge})
    raw = _rows(fr)[0]["fields_json"]
    parsed = json.loads(raw)  # must not raise -- always valid JSON
    assert len(parsed["result"]) <= MAX_FIELD_STRING_CHARS + 60  # bounded text + marker suffix
    assert "truncated" in parsed["result"]


def test_oversized_fields_blob_collapses_to_valid_bounded_json(tmp_path):
    fr = _fr(tmp_path)
    # Many distinct large-ish strings so the WHOLE fields blob (not any
    # single string) exceeds MAX_FIELDS_JSON_BYTES even after per-string
    # bounding.
    fields = {f"k{i}": "y" * 1900 for i in range(10)}
    fr.record_machine_event("tool.call", fields=fields)
    raw = _rows(fr)[0]["fields_json"]
    parsed = json.loads(raw)
    assert len(raw.encode("utf-8")) <= MAX_FIELDS_JSON_BYTES + 200
    assert parsed.get("_truncated") is True


def test_binary_payload_stubbed_not_persisted_raw(tmp_path):
    fr = _fr(tmp_path)
    fr.record_machine_event("tool.result", fields={"payload": b"\x89PNG\r\n\x1a\n" + b"\x00" * 500})
    stored = json.loads(_rows(fr)[0]["fields_json"])
    assert stored["payload"]["_type"] == "binary"
    assert stored["payload"]["size_bytes"] == 508


def test_image_content_block_stubbed_not_persisted_raw(tmp_path):
    fr = _fr(tmp_path)
    fr.record_machine_event("tool.result", fields={
        "content": {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA...."}},
    })
    stored = json.loads(_rows(fr)[0]["fields_json"])
    assert stored["content"]["_type"] == "binary_stub"
    assert "base64" not in json.dumps(stored)


def test_context_history_shaped_payload_rejected_not_persisted(tmp_path):
    fr = _fr(tmp_path)
    fake_history = [
        {"role": "system", "content": "you are Lumina..."},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]
    fr.record_machine_event("tool.call", fields={"messages": fake_history})
    stored = json.loads(_rows(fr)[0]["fields_json"])
    assert stored["messages"] != fake_history
    assert "rejected" in stored["messages"]
    assert "hello" not in json.dumps(stored)


# ── tool args hashing / duplicate detection (mission section 6, tests 19-20) ─

def test_tool_args_hash_stable_and_order_independent():
    h1 = hash_args({"city": "Tokyo", "unit": "celsius"})
    h2 = hash_args({"unit": "celsius", "city": "Tokyo"})
    assert h1 == h2
    assert len(h1) == 64  # sha256 hex digest


def test_same_args_hash_enables_duplicate_detection(tmp_path):
    fr = _fr(tmp_path)
    h = hash_args({"query": "weather"})
    fr.record_machine_event("tool.call", fields={"tool_name": "get_weather", "args_hash": h})
    fr.record_machine_event("tool.call", fields={"tool_name": "get_weather", "args_hash": h})
    fr.record_machine_event("tool.call", fields={"tool_name": "get_weather",
                                                   "args_hash": hash_args({"query": "news"})})
    rows = _rows(fr)
    hashes = [json.loads(r["fields_json"])["args_hash"] for r in rows]
    assert hashes.count(h) == 2  # the duplicate is detectable via equality
    assert len(set(hashes)) == 2


def test_bounded_repr_redacts_and_truncates():
    text = bounded_repr({"api_key": "sk-abcdefghijklmnopqrstuvwx1234567890"})
    assert "sk-abcdefghijklmnopqrstuvwx1234567890" not in text
    long_text = bounded_repr("z" * 5000)
    assert len(long_text) <= MAX_FIELD_STRING_CHARS + 60


# ── recorder failure / disabled-safety (mission section 8, "no Qt") ──────

def test_recorder_write_failure_is_silently_absorbed(tmp_path):
    fr = _fr(tmp_path)

    class _BoomConn:
        def execute(self, *a, **kw):
            raise sqlite3.OperationalError("disk I/O error")
        def commit(self):
            pass

    fr._conn = _BoomConn()  # sqlite3.Connection.execute is read-only on a
    # real instance (can't be monkeypatched directly) -- swapping the whole
    # connection object for a minimal fake exercises the exact same
    # exception-during-write path _write()'s try/except is there for.
    fr.record_machine_event("tool.call", fields={"x": 1})  # must not raise


def test_recorder_init_failure_disables_recorder_not_raises(tmp_path):
    # A db_path pointing at a location that can never become a directory
    # (a FILE standing where a parent directory would need to be created).
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("i am a file, not a directory")
    bad_path = str(blocker / "sub" / "flight_recorder.db")

    fr = FlightRecorder(db_path=bad_path)  # must not raise

    assert fr.enabled is False
    fr.record_machine_event("tool.call", fields={"x": 1})  # must not raise either
    with pytest.raises(ValueError):
        fr.record_machine_event("turn.think", fields={})  # API-misuse still raises even when disabled


def test_flight_recorder_module_has_no_qt_dependency():
    import inspect
    import core.flight_recorder as mod
    source = inspect.getsource(mod)
    for marker in ("PySide", "PyQt", "QtCore", "QtWidgets", "QtGui"):
        assert marker not in source, f"unexpected Qt reference: {marker}"
