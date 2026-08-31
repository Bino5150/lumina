"""
tests/test_context_checkpoints.py -- CONTEXT-LIFECYCLE-A3I storage/lifecycle
suite for core/context_checkpoints.py.

Every test isolates config.DB_PATH to tmp_path (same convention
tests/test_context_reconstruction.py and tests/test_coding_checkpoint_store.py
already use) and seeds chat/message rows through the real tools.memory
functions so fixture rows are shaped exactly like production. Never touches
the real ~/lumina / ~/lumina-release app database.
"""
import hashlib
import json
import sqlite3
import threading

import pytest

import config
import core.db as db_module
import core.context_checkpoints as cc
import tools.memory as memory
import tools.palace as palace
from core.context_reconstruction import reconstruct_chat_context


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "lumina.db"))
    memory.init_chat_db()
    palace.init_palace_db()
    return tmp_path


def _seed_chat(messages=()):
    chat_id = memory.create_chat("test chat")
    for role, content in messages:
        memory.save_chat_message(chat_id, role, content)
    return chat_id


def _spine(chat_id, context_skip=0):
    return reconstruct_chat_context(chat_id, context_skip=context_skip).durable_spine_fingerprint


def _begin(chat_id, context_skip=0):
    fp = _spine(chat_id, context_skip)
    return cc.begin_checkpoint(chat_id, fp, context_skip), fp


def _payload(**overrides):
    base = {"v": 1}
    base.update(overrides)
    return base


def _write_compaction_skip(chat_id, skip):
    """Write a real manual-compaction Drawer -- same shape
    core/manual_compaction.py::run_manual_compaction() writes -- so
    resolve_context_skip()/latest_manual_compaction_skip() picks it up for
    real, rather than faking the resolved value."""
    palace.palace_store(
        content="summary",
        wing="nightstand",
        room=str(chat_id),
        layer=2,
        tags=["manual-compaction", f"session:{chat_id}", f"context-skip:{skip}"],
        compress=False,
    )


def _raw_row(checkpoint_id):
    """Fresh, unrelated connection -- simulates reading after a process
    restart/reopen rather than through any module-level state."""
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM context_checkpoints WHERE id=?", (checkpoint_id,)
        ).fetchone()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_init_creates_table_with_expected_columns():
    cc.init_context_checkpoint_db()
    conn = db_module.connect()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(context_checkpoints)").fetchall()}
    finally:
        conn.close()
    assert cols == {
        "id", "chat_id", "state", "durable_spine_fingerprint", "context_skip",
        "payload_version", "payload_json", "payload_hash", "failure_reason",
        "created_at", "ready_at", "failed_at", "superseded_at",
    }


def test_init_idempotent():
    cc.init_context_checkpoint_db()
    cc.init_context_checkpoint_db()  # must not raise


def test_init_does_not_disturb_existing_chat_data():
    chat_id = _seed_chat([("user", "hello")])
    cc.init_context_checkpoint_db()
    cc.init_context_checkpoint_db()
    assert memory.load_chat_messages(chat_id) == [
        {"role": "user", "content": "hello", "metadata": None}
    ]


def test_indexes_exist():
    cc.init_context_checkpoint_db()
    conn = db_module.connect()
    try:
        names = {r["name"] for r in conn.execute("PRAGMA index_list(context_checkpoints)").fetchall()}
    finally:
        conn.close()
    assert "idx_context_checkpoints_chat_state" in names
    assert "idx_context_checkpoints_chat_fingerprint" in names


def test_does_not_capture_db_path_at_import_time(tmp_path, monkeypatch):
    first_db = tmp_path / "first.db"
    monkeypatch.setattr(config, "DB_PATH", str(first_db))
    cc.init_context_checkpoint_db()
    assert first_db.exists()

    second_db = tmp_path / "second.db"
    monkeypatch.setattr(config, "DB_PATH", str(second_db))
    cc.init_context_checkpoint_db()
    assert second_db.exists()


# ---------------------------------------------------------------------------
# Begin
# ---------------------------------------------------------------------------

def test_begin_creates_building():
    chat_id = _seed_chat([("user", "hi")])
    rec, fp = _begin(chat_id)
    assert rec.state == cc.STATE_BUILDING
    assert isinstance(rec.id, int)


def test_begin_stores_correct_identity():
    chat_id = _seed_chat([("user", "hi")])
    rec, fp = _begin(chat_id, context_skip=0)
    assert rec.chat_id == chat_id
    assert rec.durable_spine_fingerprint == fp
    assert rec.context_skip == 0


def test_begin_contains_no_fabricated_ready_payload():
    chat_id = _seed_chat([("user", "hi")])
    rec, fp = _begin(chat_id)
    assert rec.payload is None
    assert rec.payload_version is None
    assert rec.payload_hash is None
    assert rec.ready_at is None


def test_begin_multiple_building_attempts_coexist():
    chat_id = _seed_chat([("user", "hi")])
    rec1, _ = _begin(chat_id)
    rec2, _ = _begin(chat_id)
    assert rec1.id != rec2.id
    assert cc.get_checkpoint(rec1.id).state == cc.STATE_BUILDING
    assert cc.get_checkpoint(rec2.id).state == cc.STATE_BUILDING


def test_begin_unknown_chat_raises():
    with pytest.raises(cc.UnknownChat):
        cc.begin_checkpoint(999999, "a" * 64, 0)


def test_begin_invalid_fingerprint_raises():
    chat_id = _seed_chat([("user", "hi")])
    with pytest.raises(cc.ContextCheckpointError):
        cc.begin_checkpoint(chat_id, "not-a-hash", 0)


def test_begin_negative_context_skip_raises():
    chat_id = _seed_chat([("user", "hi")])
    fp = _spine(chat_id)
    with pytest.raises(cc.ContextCheckpointError):
        cc.begin_checkpoint(chat_id, fp, -1)


def test_restart_preserves_building():
    chat_id = _seed_chat([("user", "hi")])
    rec, fp = _begin(chat_id)
    row = _raw_row(rec.id)
    assert row["state"] == cc.STATE_BUILDING
    assert cc.get_checkpoint(rec.id).state == cc.STATE_BUILDING


# ---------------------------------------------------------------------------
# Consumability
# ---------------------------------------------------------------------------

def test_building_not_usable():
    chat_id = _seed_chat([("user", "hi")])
    _begin(chat_id)
    assert cc.get_latest_usable_checkpoint(chat_id) is None


def test_building_not_usable_even_with_a_forged_payload():
    """A BUILDING row can never legitimately carry payload data (only
    finalize_checkpoint() writes those columns, atomically with the READY
    transition) -- but prove the *state* gate itself is load-bearing, not
    just the payload-presence check, by forging a structurally valid
    canonical payload directly onto a still-BUILDING row and confirming
    lookup still excludes it."""
    chat_id = _seed_chat([("user", "hi")])
    rec, fp = _begin(chat_id)

    payload = {"forged": True}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute(
        "UPDATE context_checkpoints SET payload_version=1, payload_json=?, payload_hash=? WHERE id=?",
        (canonical, payload_hash, rec.id),
    )
    conn.commit()
    conn.close()

    assert cc.get_checkpoint(rec.id).state == cc.STATE_BUILDING
    assert cc.get_latest_usable_checkpoint(chat_id) is None


def test_failed_not_usable():
    chat_id = _seed_chat([("user", "hi")])
    rec, fp = _begin(chat_id)
    cc.fail_checkpoint(rec.id, chat_id, reason="test")
    assert cc.get_latest_usable_checkpoint(chat_id) is None


def test_superseded_not_usable_even_if_newest():
    chat_id = _seed_chat([("user", "hi")])
    rec, fp = _begin(chat_id)
    cc.finalize_checkpoint(rec.id, chat_id, fp, 1, _payload())
    cc.supersede_checkpoint(rec.id, chat_id)
    assert cc.get_latest_usable_checkpoint(chat_id) is None


def test_ready_with_invalid_payload_not_usable():
    chat_id = _seed_chat([("user", "hi")])
    rec, fp = _begin(chat_id)
    cc.finalize_checkpoint(rec.id, chat_id, fp, 1, _payload())

    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("UPDATE context_checkpoints SET payload_json='not json' WHERE id=?", (rec.id,))
    conn.commit()
    conn.close()

    assert cc.get_latest_usable_checkpoint(chat_id) is None
    reloaded = cc.get_checkpoint(rec.id)
    assert reloaded.state == cc.STATE_READY
    assert reloaded.payload is None


# ---------------------------------------------------------------------------
# Finalize
# ---------------------------------------------------------------------------

def test_valid_building_finalizes_to_ready():
    chat_id = _seed_chat([("user", "hi")])
    rec, fp = _begin(chat_id)
    ready = cc.finalize_checkpoint(rec.id, chat_id, fp, 1, _payload(note="x"))
    assert ready.state == cc.STATE_READY
    assert ready.payload == _payload(note="x")
    assert ready.payload_version == 1
    assert ready.ready_at is not None


def test_ready_stores_canonical_payload_and_hash():
    chat_id = _seed_chat([("user", "hi")])
    rec, fp = _begin(chat_id)
    payload = {"b": 2, "a": 1}
    cc.finalize_checkpoint(rec.id, chat_id, fp, 1, payload)
    row = _raw_row(rec.id)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    assert row["payload_json"] == canonical
    assert row["payload_hash"] == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_ready_transition_sets_state_and_timestamp_together():
    chat_id = _seed_chat([("user", "hi")])
    rec, fp = _begin(chat_id)
    before = cc.get_checkpoint(rec.id)
    assert before.state == cc.STATE_BUILDING and before.ready_at is None
    ready = cc.finalize_checkpoint(rec.id, chat_id, fp, 1, _payload())
    assert ready.state == cc.STATE_READY and ready.ready_at is not None


def test_second_finalize_conflicts():
    chat_id = _seed_chat([("user", "hi")])
    rec, fp = _begin(chat_id)
    cc.finalize_checkpoint(rec.id, chat_id, fp, 1, _payload())
    with pytest.raises(cc.StateConflict):
        cc.finalize_checkpoint(rec.id, chat_id, fp, 1, _payload())


def test_failed_checkpoint_cannot_finalize():
    chat_id = _seed_chat([("user", "hi")])
    rec, fp = _begin(chat_id)
    cc.fail_checkpoint(rec.id, chat_id)
    with pytest.raises(cc.StateConflict):
        cc.finalize_checkpoint(rec.id, chat_id, fp, 1, _payload())


def test_superseded_checkpoint_cannot_finalize():
    chat_id = _seed_chat([("user", "hi")])
    rec, fp = _begin(chat_id)
    cc.finalize_checkpoint(rec.id, chat_id, fp, 1, _payload())
    cc.supersede_checkpoint(rec.id, chat_id)
    with pytest.raises(cc.StateConflict):
        cc.finalize_checkpoint(rec.id, chat_id, fp, 1, _payload())


# ---------------------------------------------------------------------------
# Spine movement
# ---------------------------------------------------------------------------

def test_append_user_row_between_begin_and_finalize_rejects():
    chat_id = _seed_chat([("user", "hi")])
    rec, fp = _begin(chat_id)
    memory.save_chat_message(chat_id, "user", "second message")
    with pytest.raises(cc.StaleSpine):
        cc.finalize_checkpoint(rec.id, chat_id, fp, 1, _payload())
    assert cc.get_checkpoint(rec.id).state == cc.STATE_FAILED


def test_append_assistant_row_between_begin_and_finalize_rejects():
    chat_id = _seed_chat([("user", "hi")])
    rec, fp = _begin(chat_id)
    memory.save_chat_message(chat_id, "assistant", "reply")
    with pytest.raises(cc.StaleSpine):
        cc.finalize_checkpoint(rec.id, chat_id, fp, 1, _payload())


def test_edit_durable_content_between_begin_and_finalize_rejects():
    chat_id = _seed_chat([("user", "hi")])
    rec, fp = _begin(chat_id)
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("UPDATE chat_messages SET content='edited' WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()
    with pytest.raises(cc.StaleSpine):
        cc.finalize_checkpoint(rec.id, chat_id, fp, 1, _payload())


def test_metadata_change_between_begin_and_finalize_rejects():
    chat_id = _seed_chat([("user", "hi")])
    rec, fp = _begin(chat_id)
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute('UPDATE chat_messages SET metadata=? WHERE chat_id=?', ('{"cancelled": true}', chat_id))
    conn.commit()
    conn.close()
    with pytest.raises(cc.StaleSpine):
        cc.finalize_checkpoint(rec.id, chat_id, fp, 1, _payload())


def test_context_skip_movement_between_begin_and_finalize_rejects():
    chat_id = _seed_chat([("user", "hi"), ("assistant", "hello"), ("user", "bye")])
    rec, fp = _begin(chat_id, context_skip=0)
    _write_compaction_skip(chat_id, 2)
    with pytest.raises(cc.StaleSpine):
        cc.finalize_checkpoint(rec.id, chat_id, fp, 1, _payload())
    assert cc.get_checkpoint(rec.id).state == cc.STATE_FAILED


def test_unchanged_durable_spine_finalizes_successfully():
    chat_id = _seed_chat([("user", "hi"), ("assistant", "hello")])
    rec, fp = _begin(chat_id)
    ready = cc.finalize_checkpoint(rec.id, chat_id, fp, 1, _payload())
    assert ready.state == cc.STATE_READY


# ---------------------------------------------------------------------------
# Chat isolation
# ---------------------------------------------------------------------------

def test_wrong_chat_id_cannot_finalize_another_chats_checkpoint():
    chat_a = _seed_chat([("user", "a")])
    chat_b = _seed_chat([("user", "b")])
    rec, fp = _begin(chat_a)
    with pytest.raises(cc.IdentityMismatch):
        cc.finalize_checkpoint(rec.id, chat_b, fp, 1, _payload())
    assert cc.get_checkpoint(rec.id).state == cc.STATE_BUILDING


def test_wrong_chat_cannot_retrieve_it_as_usable():
    chat_a = _seed_chat([("user", "a")])
    chat_b = _seed_chat([("user", "unrelated")])
    rec, fp = _begin(chat_a)
    cc.finalize_checkpoint(rec.id, chat_a, fp, 1, _payload())
    assert cc.get_latest_usable_checkpoint(chat_b) is None
    assert cc.get_latest_usable_checkpoint(chat_a) is not None


def test_empty_spine_chats_still_isolate_by_chat_id():
    """A chat with zero eligible durable rows hashes to the fixed
    SHA-256-of-nothing digest, identical for every empty chat -- the
    fingerprint alone cannot distinguish two such chats. Lookup must still
    isolate by chat_id at the SQL layer, not rely on fingerprint
    uniqueness (which the hash construction never actually guarantees
    across chats) to prevent cross-chat leakage."""
    chat_a = _seed_chat([])
    chat_b = _seed_chat([])
    assert _spine(chat_a) == _spine(chat_b)

    rec_a, fp_a = _begin(chat_a)
    cc.finalize_checkpoint(rec_a.id, chat_a, fp_a, 1, _payload(who="a"))

    assert cc.get_latest_usable_checkpoint(chat_b) is None
    usable_a = cc.get_latest_usable_checkpoint(chat_a)
    assert usable_a is not None and usable_a.payload["who"] == "a"


def test_same_payload_across_two_chats_never_crosses_identity():
    chat_a = _seed_chat([("user", "same text")])
    chat_b = _seed_chat([("user", "same text")])
    rec_a, fp_a = _begin(chat_a)
    rec_b, fp_b = _begin(chat_b)
    cc.finalize_checkpoint(rec_a.id, chat_a, fp_a, 1, _payload(who="a"))
    cc.finalize_checkpoint(rec_b.id, chat_b, fp_b, 1, _payload(who="b"))

    usable_a = cc.get_latest_usable_checkpoint(chat_a)
    usable_b = cc.get_latest_usable_checkpoint(chat_b)
    assert usable_a.payload["who"] == "a"
    assert usable_b.payload["who"] == "b"
    assert usable_a.id != usable_b.id


# ---------------------------------------------------------------------------
# Payload validation
# ---------------------------------------------------------------------------

def test_malformed_envelope_rejected_when_not_a_dict():
    chat_id = _seed_chat([("user", "hi")])
    rec, fp = _begin(chat_id)
    with pytest.raises(cc.PayloadValidationError):
        cc.finalize_checkpoint(rec.id, chat_id, fp, 1, "not a dict")
    assert cc.get_checkpoint(rec.id).state == cc.STATE_BUILDING


def test_unsupported_payload_version_rejected():
    chat_id = _seed_chat([("user", "hi")])
    rec, fp = _begin(chat_id)
    with pytest.raises(cc.PayloadValidationError):
        cc.finalize_checkpoint(rec.id, chat_id, fp, 999, _payload())
    assert cc.get_checkpoint(rec.id).state == cc.STATE_BUILDING


def test_unserializable_payload_rejected_before_ready():
    chat_id = _seed_chat([("user", "hi")])
    rec, fp = _begin(chat_id)
    with pytest.raises(cc.PayloadValidationError):
        cc.finalize_checkpoint(rec.id, chat_id, fp, 1, {"bad": {1, 2, 3}})
    assert cc.get_checkpoint(rec.id).state == cc.STATE_BUILDING


def test_oversized_payload_rejected():
    chat_id = _seed_chat([("user", "hi")])
    rec, fp = _begin(chat_id)
    huge = {"blob": "x" * (cc.MAX_PAYLOAD_BYTES + 1)}
    with pytest.raises(cc.PayloadValidationError):
        cc.finalize_checkpoint(rec.id, chat_id, fp, 1, huge)
    assert cc.get_checkpoint(rec.id).state == cc.STATE_BUILDING


def test_payload_validation_failure_leaves_row_retriable():
    """Unlike StaleSpine, a payload-shape error is a caller-input problem,
    not a race -- the frozen spine binding is still perfectly valid, so a
    corrected payload can finalize the SAME checkpoint id afterward."""
    chat_id = _seed_chat([("user", "hi")])
    rec, fp = _begin(chat_id)
    with pytest.raises(cc.PayloadValidationError):
        cc.finalize_checkpoint(rec.id, chat_id, fp, 1, "not a dict")
    ready = cc.finalize_checkpoint(rec.id, chat_id, fp, 1, _payload(retried=True))
    assert ready.state == cc.STATE_READY
    assert ready.payload["retried"] is True


# ---------------------------------------------------------------------------
# Failure
# ---------------------------------------------------------------------------

def test_building_to_failed_succeeds():
    chat_id = _seed_chat([("user", "hi")])
    rec, fp = _begin(chat_id)
    failed = cc.fail_checkpoint(rec.id, chat_id, reason="manual")
    assert failed.state == cc.STATE_FAILED
    assert failed.failure_reason == "manual"
    assert failed.failed_at is not None


def test_fail_is_idempotent_once_already_failed():
    chat_id = _seed_chat([("user", "hi")])
    rec, fp = _begin(chat_id)
    first = cc.fail_checkpoint(rec.id, chat_id, reason="first")
    second = cc.fail_checkpoint(rec.id, chat_id, reason="second")
    assert second.state == cc.STATE_FAILED
    assert second.failure_reason == "first"  # not overwritten


def test_failed_stays_non_consumable_across_restart():
    chat_id = _seed_chat([("user", "hi")])
    rec, fp = _begin(chat_id)
    cc.fail_checkpoint(rec.id, chat_id, reason="x")
    row = _raw_row(rec.id)
    assert row["state"] == cc.STATE_FAILED
    assert cc.get_latest_usable_checkpoint(chat_id) is None


def test_fail_on_ready_does_not_demote_it():
    chat_id = _seed_chat([("user", "hi")])
    rec, fp = _begin(chat_id)
    cc.finalize_checkpoint(rec.id, chat_id, fp, 1, _payload())
    with pytest.raises(cc.StateConflict):
        cc.fail_checkpoint(rec.id, chat_id, reason="oops")
    assert cc.get_checkpoint(rec.id).state == cc.STATE_READY


def test_fail_on_superseded_raises():
    chat_id = _seed_chat([("user", "hi")])
    rec, fp = _begin(chat_id)
    cc.finalize_checkpoint(rec.id, chat_id, fp, 1, _payload())
    cc.supersede_checkpoint(rec.id, chat_id)
    with pytest.raises(cc.StateConflict):
        cc.fail_checkpoint(rec.id, chat_id)


def test_fail_wrong_chat_raises_identity_mismatch():
    chat_a = _seed_chat([("user", "a")])
    chat_b = _seed_chat([("user", "b")])
    rec, fp = _begin(chat_a)
    with pytest.raises(cc.IdentityMismatch):
        cc.fail_checkpoint(rec.id, chat_b)
    assert cc.get_checkpoint(rec.id).state == cc.STATE_BUILDING


def test_failure_reason_is_bounded_length():
    chat_id = _seed_chat([("user", "hi")])
    rec, fp = _begin(chat_id)
    failed = cc.fail_checkpoint(rec.id, chat_id, reason="x" * 10000)
    assert len(failed.failure_reason) == cc.MAX_FAILURE_REASON_LEN


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------

def test_one_valid_ready_returns():
    chat_id = _seed_chat([("user", "hi")])
    rec, fp = _begin(chat_id)
    cc.finalize_checkpoint(rec.id, chat_id, fp, 1, _payload())
    usable = cc.get_latest_usable_checkpoint(chat_id)
    assert usable.id == rec.id


def test_multiple_ready_checkpoints_choose_deterministically():
    chat_id = _seed_chat([("user", "hi")])
    rec1, fp1 = _begin(chat_id)
    cc.finalize_checkpoint(rec1.id, chat_id, fp1, 1, _payload(n=1))
    rec2, fp2 = _begin(chat_id)
    assert fp2 == fp1  # spine unchanged between the two builds
    cc.finalize_checkpoint(rec2.id, chat_id, fp2, 1, _payload(n=2))

    usable = cc.get_latest_usable_checkpoint(chat_id)
    assert usable.id == rec2.id


def test_newer_ready_wins_even_if_older_never_marked_superseded():
    chat_id = _seed_chat([("user", "hi")])
    rec1, fp1 = _begin(chat_id)
    cc.finalize_checkpoint(rec1.id, chat_id, fp1, 1, _payload(n=1))
    rec2, fp2 = _begin(chat_id)
    cc.finalize_checkpoint(rec2.id, chat_id, fp2, 1, _payload(n=2))

    usable = cc.get_latest_usable_checkpoint(chat_id)
    assert usable.id == rec2.id
    assert cc.get_checkpoint(rec1.id).state == cc.STATE_READY  # never superseded


def test_explicit_supersession_does_not_change_which_checkpoint_wins():
    chat_id = _seed_chat([("user", "hi")])
    rec1, fp1 = _begin(chat_id)
    cc.finalize_checkpoint(rec1.id, chat_id, fp1, 1, _payload(n=1))
    rec2, fp2 = _begin(chat_id)
    cc.finalize_checkpoint(rec2.id, chat_id, fp2, 1, _payload(n=2))

    before = cc.get_latest_usable_checkpoint(chat_id)
    cc.supersede_checkpoint(rec1.id, chat_id)
    after = cc.get_latest_usable_checkpoint(chat_id)
    assert before.id == after.id == rec2.id


def test_stale_newest_ready_is_skipped_for_older_still_valid_ready():
    """Newest-by-id READY row is stale against the queried spine; an OLDER
    READY row still matches. Lookup must search backward, not just take the
    newest row unconditionally."""
    chat_id = _seed_chat([("user", "a"), ("assistant", "b")])
    fp_skip0 = _spine(chat_id, context_skip=0)
    fp_skip1 = _spine(chat_id, context_skip=1)
    assert fp_skip0 != fp_skip1

    rec_a = cc.begin_checkpoint(chat_id, fp_skip0, 0)
    ready_a = cc.finalize_checkpoint(rec_a.id, chat_id, fp_skip0, 1, _payload(n="a"))

    _write_compaction_skip(chat_id, 1)
    rec_b = cc.begin_checkpoint(chat_id, fp_skip1, 1)
    ready_b = cc.finalize_checkpoint(rec_b.id, chat_id, fp_skip1, 1, _payload(n="b"))
    assert ready_b.id > ready_a.id

    usable = cc.get_latest_usable_checkpoint(chat_id, context_skip=0)
    assert usable.id == ready_a.id
    assert usable.payload["n"] == "a"


def test_fully_stale_lookup_returns_none():
    chat_id = _seed_chat([("user", "a")])
    rec, fp = _begin(chat_id)
    cc.finalize_checkpoint(rec.id, chat_id, fp, 1, _payload())
    memory.save_chat_message(chat_id, "assistant", "b")
    assert cc.get_latest_usable_checkpoint(chat_id) is None


def test_lookup_for_chat_with_no_checkpoints_returns_none():
    chat_id = _seed_chat([("user", "hi")])
    assert cc.get_latest_usable_checkpoint(chat_id) is None


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------

def test_chat_deletion_cascades_its_checkpoints():
    chat_id = _seed_chat([("user", "hi")])
    rec, fp = _begin(chat_id)
    cc.finalize_checkpoint(rec.id, chat_id, fp, 1, _payload())

    memory.delete_chat(chat_id)

    assert cc.list_checkpoints(chat_id) == []
    with pytest.raises(cc.CheckpointNotFound):
        cc.get_checkpoint(rec.id)


def test_chat_deletion_does_not_affect_other_chats_checkpoints():
    chat_a = _seed_chat([("user", "a")])
    chat_b = _seed_chat([("user", "b")])
    rec_b, fp_b = _begin(chat_b)
    cc.finalize_checkpoint(rec_b.id, chat_b, fp_b, 1, _payload())

    memory.delete_chat(chat_a)

    usable = cc.get_latest_usable_checkpoint(chat_b)
    assert usable is not None and usable.id == rec_b.id


# ---------------------------------------------------------------------------
# Restart
# ---------------------------------------------------------------------------

def test_ready_survives_restart():
    chat_id = _seed_chat([("user", "hi")])
    rec, fp = _begin(chat_id)
    cc.finalize_checkpoint(rec.id, chat_id, fp, 1, _payload())
    row = _raw_row(rec.id)
    assert row["state"] == cc.STATE_READY


def test_lookup_is_deterministic_across_repeated_calls():
    chat_id = _seed_chat([("user", "hi")])
    rec, fp = _begin(chat_id)
    cc.finalize_checkpoint(rec.id, chat_id, fp, 1, _payload())
    first = cc.get_latest_usable_checkpoint(chat_id)
    second = cc.get_latest_usable_checkpoint(chat_id)
    assert first.id == second.id == rec.id


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

def test_concurrent_begin_calls_all_well_formed():
    chat_id = _seed_chat([("user", "hi")])
    fp = _spine(chat_id)
    barrier = threading.Barrier(4)
    results = []
    lock = threading.Lock()

    def attempt():
        barrier.wait()
        rec = cc.begin_checkpoint(chat_id, fp, 0)
        with lock:
            results.append(rec.id)

    threads = [threading.Thread(target=attempt) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert all(not t.is_alive() for t in threads)
    assert len(results) == len(set(results)) == 4
    for checkpoint_id in results:
        assert cc.get_checkpoint(checkpoint_id).state == cc.STATE_BUILDING


def test_concurrent_finalize_calls_produce_exactly_one_winner():
    """Two independent connections race finalize_checkpoint() on the SAME
    checkpoint id. No in-process lock exists in this module -- SQLite's
    BEGIN IMMEDIATE + busy_timeout is the sole correctness boundary (same
    convention as
    test_coding_checkpoint_store.py::test_concurrent_writers_exactly_one_wins)."""
    chat_id = _seed_chat([("user", "hi")])
    rec, fp = _begin(chat_id)
    barrier = threading.Barrier(2)
    results = {}

    def attempt(name):
        barrier.wait()
        try:
            r = cc.finalize_checkpoint(rec.id, chat_id, fp, 1, _payload(who=name))
            results[name] = ("ok", r.state)
        except cc.StateConflict:
            results[name] = ("conflict", None)

    t1 = threading.Thread(target=attempt, args=("A",))
    t2 = threading.Thread(target=attempt, args=("B",))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert not t1.is_alive() and not t2.is_alive()
    outcomes = sorted(r[0] for r in results.values())
    assert outcomes == ["conflict", "ok"]
    assert cc.get_checkpoint(rec.id).state == cc.STATE_READY


def test_concurrent_finalize_and_fail_race_exactly_one_wins():
    chat_id = _seed_chat([("user", "hi")])
    rec, fp = _begin(chat_id)
    barrier = threading.Barrier(2)
    results = {}

    def do_finalize():
        barrier.wait()
        try:
            r = cc.finalize_checkpoint(rec.id, chat_id, fp, 1, _payload())
            results["finalize"] = ("ok", r.state)
        except cc.StateConflict:
            results["finalize"] = ("conflict", None)

    def do_fail():
        barrier.wait()
        try:
            r = cc.fail_checkpoint(rec.id, chat_id, reason="race")
            results["fail"] = ("ok", r.state)
        except cc.StateConflict:
            results["fail"] = ("conflict", None)

    t1 = threading.Thread(target=do_finalize)
    t2 = threading.Thread(target=do_fail)
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert not t1.is_alive() and not t2.is_alive()
    final = cc.get_checkpoint(rec.id)
    assert final.state in (cc.STATE_READY, cc.STATE_FAILED)
    ok_count = sum(1 for outcome in results.values() if outcome[0] == "ok")
    assert ok_count == 1


def test_lookup_during_finalization_never_sees_a_partial_ready():
    """Pins a finalize_checkpoint() call mid-transaction, right after it
    acquires BEGIN IMMEDIATE's write lock, while a second thread repeatedly
    looks the checkpoint up. Deterministic via an explicit release Event,
    not sleep-based timing (sqlite3.Connection is a C extension type and
    cannot be monkeypatched directly -- core.db.connect is wrapped instead,
    which finalize_checkpoint()'s own `from core.db import connect` picks
    up fresh on each call)."""
    chat_id = _seed_chat([("user", "hi")])
    rec, fp = _begin(chat_id)

    reached_lock = threading.Event()
    release = threading.Event()
    real_connect = db_module.connect

    class _BlockingConnWrapper:
        def __init__(self, real_conn):
            self._real = real_conn

        def execute(self, sql, *a, **kw):
            result = self._real.execute(sql, *a, **kw)
            if isinstance(sql, str) and sql.strip() == "BEGIN IMMEDIATE":
                reached_lock.set()
                release.wait(timeout=15)
            return result

        def __getattr__(self, name):
            return getattr(self._real, name)

    def wrapped_connect(*a, **kw):
        return _BlockingConnWrapper(real_connect(*a, **kw))

    outcome = {}

    def finalize_thread():
        outcome["result"] = cc.finalize_checkpoint(rec.id, chat_id, fp, 1, _payload())

    db_module.connect = wrapped_connect
    t = threading.Thread(target=finalize_thread)
    try:
        t.start()
        assert reached_lock.wait(timeout=15)
        seen_states = {cc.get_checkpoint(rec.id).state for _ in range(20)}
    finally:
        release.set()
        db_module.connect = real_connect
        t.join(timeout=15)

    assert not t.is_alive()
    assert seen_states == {cc.STATE_BUILDING}
    assert outcome["result"].state == cc.STATE_READY


# ---------------------------------------------------------------------------
# Cross-subsystem lock domain (A3R corrective item 1/2)
# ---------------------------------------------------------------------------

def test_palace_get_db_is_core_db_connect():
    """Source-vet, not assumption: tools.palace.get_db() must be exactly
    core.db.connect() -- same factory, same config.DB_PATH, same WAL/
    busy_timeout pragmas -- for the module docstring's atomicity claim
    (a Palace context-skip:N write cannot interleave with finalize's spine
    recheck) to mean anything. Proven directly: writing through
    tools.palace.get_db() and reading back through core.db.connect() (and
    vice versa) must observe the same row in the same database file."""
    conn = palace.get_db()
    conn.execute(
        "INSERT INTO palace_wings (name) VALUES (?)", ("lock_domain_probe",)
    )
    conn.commit()
    conn.close()

    conn2 = db_module.connect()
    try:
        row = conn2.execute(
            "SELECT name FROM palace_wings WHERE name=?", ("lock_domain_probe",)
        ).fetchone()
    finally:
        conn2.close()
    assert row is not None and row["name"] == "lock_domain_probe"

    import inspect
    src = inspect.getsource(palace.get_db)
    assert "core.db" in src and "connect" in src


def test_palace_write_cannot_land_between_finalize_spine_observation_and_ready_commit():
    """Deterministic proof (not inference from "same DB file") that a
    concurrent Palace write -- the exact mechanism through which a
    context-skip:N Drawer is written, i.e. what
    core.manual_compaction.latest_manual_compaction_skip() reads -- cannot
    commit while finalize_checkpoint() holds its BEGIN IMMEDIATE lock.

    Uses an explicit shared, lock-protected event log rather than sleep-
    based timing: the wrapped connection records "finalize_committed" the
    instant finalize's COMMIT executes; the racing writer thread records
    "write_attempting" immediately before its own single INSERT statement
    and "write_completed" immediately after its own commit. The writer is
    given a bounded (1s) join-timeout window to complete BEFORE finalize is
    ever released -- if Palace and context_checkpoints did NOT share a lock
    domain, the write would complete inside that window while finalize
    stays pinned, and t2 would not still be alive; the final log-ordering
    assertion is the load-bearing, timing-independent check."""
    chat_id = _seed_chat([("user", "hi")])
    seeded = palace.palace_store(
        content="seed", wing="nightstand", room=str(chat_id),
        layer=2, tags=["seed"], compress=False,
    )
    conn = db_module.connect()
    try:
        room_id = conn.execute(
            "SELECT room_id FROM palace_drawers WHERE id=?", (seeded["drawer_id"],)
        ).fetchone()["room_id"]
    finally:
        conn.close()

    rec, fp = _begin(chat_id)

    reached_lock = threading.Event()
    release = threading.Event()
    write_may_attempt = threading.Event()
    log = []
    log_lock = threading.Lock()

    def record(tag):
        with log_lock:
            log.append(tag)

    real_connect = db_module.connect

    class _BlockingConnWrapper:
        def __init__(self, real_conn):
            self._real = real_conn

        def execute(self, sql, *a, **kw):
            stripped = sql.strip() if isinstance(sql, str) else None
            result = self._real.execute(sql, *a, **kw)
            if stripped == "BEGIN IMMEDIATE":
                reached_lock.set()
                release.wait(timeout=15)
            elif stripped == "COMMIT":
                record("finalize_committed")
            return result

        def __getattr__(self, name):
            return getattr(self._real, name)

    def wrapped_connect(*a, **kw):
        return _BlockingConnWrapper(real_connect(*a, **kw))

    outcome = {}

    def finalize_thread():
        outcome["result"] = cc.finalize_checkpoint(rec.id, chat_id, fp, 1, _payload())

    def palace_writer_thread():
        assert write_may_attempt.wait(timeout=15)
        # A raw, single-statement INSERT -- bypasses palace_store()'s own
        # wing/room lookup SELECTs so "about to write" is unambiguous.
        conn = real_connect()
        try:
            record("write_attempting")
            conn.execute(
                "INSERT INTO palace_drawers (room_id, content, tags, created_at) "
                "VALUES (?,?,?,?)",
                (room_id, "forced",
                 json.dumps(["manual-compaction", f"session:{chat_id}", "context-skip:1"]),
                 "2026-01-01T00:00:00"),
            )
            conn.commit()
            record("write_completed")
        finally:
            conn.close()

    db_module.connect = wrapped_connect
    t1 = threading.Thread(target=finalize_thread)
    t2 = threading.Thread(target=palace_writer_thread)
    try:
        t1.start()
        assert reached_lock.wait(timeout=15)  # finalize now holds the write lock
        write_may_attempt.set()
        t2.start()
        # Bounded negative check: t2 must NOT be able to finish while
        # finalize is pinned holding the lock. If Palace didn't share the
        # lock domain, this INSERT would complete near-instantly and t2
        # would already be dead here.
        t2.join(timeout=1.0)
        assert t2.is_alive(), (
            "Palace write completed while finalize_checkpoint() held its "
            "BEGIN IMMEDIATE lock -- Palace and context_checkpoints do NOT "
            "share a lock domain; the atomicity claim is false"
        )
    finally:
        release.set()
        db_module.connect = real_connect
        t1.join(timeout=15)
        t2.join(timeout=15)

    assert not t1.is_alive() and not t2.is_alive()
    assert "finalize_committed" in log and "write_completed" in log
    assert log.index("write_completed") > log.index("finalize_committed"), (
        f"Palace write completed before finalize's commit: {log}"
    )
    assert outcome["result"].state == cc.STATE_READY


def test_finalize_acquires_lock_before_reading_the_spine():
    """The BEGIN IMMEDIATE write lock must be acquired strictly BEFORE
    finalize_checkpoint() reads the durable spine (chat_messages, via
    reconstruct_chat_context()) -- reordering these two operations would
    reopen exactly the race the module docstring's atomicity claim rules
    out, even though a single non-racing call would still visibly succeed
    and the Palace-write-blocking test above would not catch it (that test
    pins on the lock, which a reordering bug would simply acquire later,
    after the now-unprotected read). No concurrency needed to prove
    ordering -- just record which statement executes first.

    Patches BOTH core.db.connect (what core/context_checkpoints.py's own
    local `from core.db import connect` picks up fresh each call) AND
    core.context_reconstruction.connect (bound once at THAT module's
    import time via its own module-level `from core.db import connect` --
    patching core.db.connect alone does not affect an already-bound name
    in another module's namespace)."""
    chat_id = _seed_chat([("user", "hi")])
    rec, fp = _begin(chat_id)

    order = []
    real_connect = db_module.connect

    class _OrderRecordingWrapper:
        def __init__(self, real_conn):
            self._real = real_conn

        def execute(self, sql, *a, **kw):
            stripped = sql.strip() if isinstance(sql, str) else ""
            if stripped == "BEGIN IMMEDIATE":
                order.append("lock_acquired")
            elif stripped.startswith("SELECT id, role, content, metadata FROM chat_messages"):
                order.append("spine_read")
            return self._real.execute(sql, *a, **kw)

        def __getattr__(self, name):
            return getattr(self._real, name)

    def wrapped_connect(*a, **kw):
        return _OrderRecordingWrapper(real_connect(*a, **kw))

    import core.context_reconstruction as reconstruction_module
    real_reconstruction_connect = reconstruction_module.connect

    db_module.connect = wrapped_connect
    reconstruction_module.connect = wrapped_connect
    try:
        ready = cc.finalize_checkpoint(rec.id, chat_id, fp, 1, _payload())
    finally:
        db_module.connect = real_connect
        reconstruction_module.connect = real_reconstruction_connect

    assert ready.state == cc.STATE_READY
    assert "lock_acquired" in order and "spine_read" in order
    assert order.index("lock_acquired") < order.index("spine_read"), (
        f"spine was read before the write lock was acquired: {order}"
    )


# ---------------------------------------------------------------------------
# Fail-closed skip resolution (A3R corrective item 4/5)
# ---------------------------------------------------------------------------

def _boom(chat_id):
    raise RuntimeError("palace db unreachable")


def test_finalize_raises_and_leaves_building_on_skip_resolution_failure(monkeypatch):
    """A compaction-checkpoint read failure during finalize's spine
    recheck must propagate as an exception, never silently substitute
    skip=0 and let the checkpoint become READY on a guess."""
    chat_id = _seed_chat([("user", "hi")])
    rec, fp = _begin(chat_id)

    monkeypatch.setattr("core.manual_compaction.latest_manual_compaction_skip", _boom)
    with pytest.raises(RuntimeError):
        cc.finalize_checkpoint(rec.id, chat_id, fp, 1, _payload())

    # Never promoted, never silently failed on a guess -- exactly BUILDING.
    assert cc.get_checkpoint(rec.id).state == cc.STATE_BUILDING


def test_lookup_raises_on_skip_resolution_failure(monkeypatch):
    """get_latest_usable_checkpoint() must not silently fall back to
    skip=0 and return a wrong verdict (a false usable OR a false None) when
    the current skip cannot be determined."""
    chat_id = _seed_chat([("user", "hi")])
    rec, fp = _begin(chat_id)
    cc.finalize_checkpoint(rec.id, chat_id, fp, 1, _payload())

    monkeypatch.setattr("core.manual_compaction.latest_manual_compaction_skip", _boom)
    with pytest.raises(RuntimeError):
        cc.get_latest_usable_checkpoint(chat_id)


def test_stale_checkpoint_never_becomes_usable_via_skip_resolution_failure(monkeypatch):
    """The regression this corrective exists for: a checkpoint built and
    finalized against context_skip=0 becomes genuinely stale once the real
    skip advances to 2 -- normal lookup correctly returns None for it. If
    skip resolution then fails and the code silently fell back to skip=0
    (the pre-correction behavior), that stale checkpoint would wrongly
    recompute as matching and come back as "usable" again. Fail-closed
    resolution must raise instead, never resurrect it."""
    chat_id = _seed_chat([("user", "a"), ("assistant", "b"), ("user", "c")])
    rec, fp = _begin(chat_id, context_skip=0)
    cc.finalize_checkpoint(rec.id, chat_id, fp, 1, _payload())

    _write_compaction_skip(chat_id, 2)

    # Skip resolution working correctly: genuinely stale, correctly absent.
    assert cc.get_latest_usable_checkpoint(chat_id) is None

    monkeypatch.setattr("core.manual_compaction.latest_manual_compaction_skip", _boom)
    with pytest.raises(RuntimeError):
        cc.get_latest_usable_checkpoint(chat_id)
