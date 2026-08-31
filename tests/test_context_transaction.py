"""
tests/test_context_transaction.py -- CONTEXT-LIFECYCLE-A5I suite for
core/context_transaction.py.

Isolates config.DB_PATH to tmp_path (same convention as
tests/test_context_checkpoints.py and tests/test_continuity_compiler.py)
and seeds chat/message rows through the real tools.memory functions. Never
touches the real ~/lumina / ~/lumina-release app database. Uses the real
core.context.ContextManager and the real core.context_checkpoints /
core.context_reconstruction modules throughout -- A2/A3's own correctness
is never mocked, only the generation-owner protocol A5I itself defines
(_FakeGenerationOwner, mirroring ui.main_window.LuminaWindow's four
methods, with no Qt import anywhere in this file).

Object-identity contract pinned for every failure-path test (A5D open
question #4): on any rejection, `ctx.history is old_history_object` holds
-- the transaction never reassigns `.history` until every check has
already passed, so failure-path identity preservation is not merely
content-equal, it is the same list object, every time.
"""
import threading
import time

import pytest

import config
import core.context_checkpoints as cc
import core.context_transaction as ct
import tools.memory as memory
import tools.palace as palace
from core import emergency_stop
from core.context import ContextManager
from core.context_reconstruction import reconstruct_chat_context
from core.flight_recorder import FlightRecorder


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "lumina.db"))
    memory.init_chat_db()
    palace.init_palace_db()
    return tmp_path


@pytest.fixture(autouse=True)
def isolated_emergency_stop():
    emergency_stop._reset_for_tests()
    yield
    emergency_stop._reset_for_tests()


def _seed_chat(messages=()):
    chat_id = memory.create_chat("test chat")
    for role, content in messages:
        memory.save_chat_message(chat_id, role, content)
    return chat_id


def _spine(chat_id, context_skip=0):
    return reconstruct_chat_context(chat_id, context_skip=context_skip).durable_spine_fingerprint


def _write_compaction_skip(chat_id, skip):
    palace.palace_store(
        content="summary", wing="nightstand", room=str(chat_id), layer=2,
        tags=["manual-compaction", f"session:{chat_id}", f"context-skip:{skip}"],
        compress=False,
    )


def _valid_payload(statement="finish part 1", machine_facts=(), reported=None, inferred=None):
    return {
        "schema_version": 1,
        "machine_facts": list(machine_facts),
        "reported": reported if reported is not None else [
            {"id": "r-1", "category": "objective", "statement": statement,
             "evidence_refs": [], "status": "unresolved"},
        ],
        "inferred": inferred if inferred is not None else [],
    }


def _ready_checkpoint(chat_id, context_skip=0, payload=None):
    fp = _spine(chat_id, context_skip)
    checkpoint = cc.begin_checkpoint(chat_id, fp, context_skip)
    payload = _valid_payload() if payload is None else payload
    return cc.finalize_checkpoint(checkpoint.id, chat_id, fp, payload_version=1, payload=payload)


def _corrupt_checkpoint_context_skip(checkpoint_id, wrong_skip):
    """Direct DB tamper, bypassing the begin_checkpoint()/finalize_checkpoint()
    API entirely -- the only way to produce a row whose durable_spine_
    fingerprint and context_skip columns disagree with each other, since
    the API always writes them together as one matched pair from a single
    ReconstructionResult (core/context_checkpoints.py's own begin_
    checkpoint() docstring). Mirrors tests/test_context_checkpoints.py's
    own raw-UPDATE tampering convention (e.g. its payload_json corruption
    tests)."""
    import sqlite3
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute(
        "UPDATE context_checkpoints SET context_skip=? WHERE id=?",
        (wrong_skip, checkpoint_id),
    )
    conn.commit()
    conn.close()


def _fr(tmp_path, name="flight.db"):
    return FlightRecorder(db_path=str(tmp_path / name))


def _events(fr):
    import sqlite3
    conn = sqlite3.connect(fr.db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
    conn.close()
    return [dict(r) for r in rows]


class _FakeGenerationOwner:
    """Minimal stand-in for ui.main_window.LuminaWindow's A5I protocol
    (current_chat_id/current_generation/bump/live_ctx) -- no Qt, matching
    A2/A3/A4's own neutral-kernel test convention. `ctx` is a real
    core.context.ContextManager, never a mock."""

    def __init__(self, chat_id, ctx=None):
        self.chat_id = chat_id
        self.ctx = ctx if ctx is not None else ContextManager()
        self.generation = ct.ContextGeneration()

    def current_chat_id(self):
        return self.chat_id

    def current_generation(self):
        return self.generation.current()

    def bump(self):
        return self.generation.bump()

    def live_ctx(self):
        return self.ctx


# ── ContextGeneration ────────────────────────────────────────────────────

def test_context_generation_starts_at_zero_and_bumps_monotonically():
    gen = ct.ContextGeneration()
    assert gen.current() == 0
    assert gen.bump() == 1
    assert gen.bump() == 2
    assert gen.current() == 2


# ── render_continuity_message ───────────────────────────────────────────

def test_render_continuity_message_includes_preamble_and_sections():
    text = ct.render_continuity_message(_valid_payload(statement="do the thing"))
    assert "not an instruction" in text
    assert "## Reported continuity" in text
    assert "do the thing" in text
    assert "## Machine-observed continuity" not in text
    assert "## Inferred continuity" not in text


def test_render_continuity_message_all_empty_sections_uses_neutral_fallback():
    text = ct.render_continuity_message(_valid_payload(reported=[], inferred=[]))
    assert text == ct._EMPTY_CONTINUITY_TEXT
    assert "No additional continuity notes" in text


def test_render_continuity_message_missing_field_raises_invalid_payload():
    with pytest.raises(ct.InvalidContinuityPayload):
        ct.render_continuity_message({"schema_version": 1, "reported": [], "inferred": []})


def test_render_continuity_message_never_renders_imperative_next_action():
    payload = _valid_payload(reported=[
        {"id": "r-1", "category": "next_action", "statement": "run the deploy script",
         "evidence_refs": [], "status": "unresolved"},
    ])
    text = ct.render_continuity_message(payload)
    assert "Reported (next_action, unresolved): run the deploy script" in text
    # Never appears as a bare imperative line (no leading "- run the deploy
    # script" without the "Reported (...)" framing prefix).
    assert "\n- run the deploy script" not in text


# ── Happy path ───────────────────────────────────────────────────────────

def test_happy_path_swaps_history_and_advances_generation(tmp_path):
    chat_id = _seed_chat([("user", "u1"), ("assistant", "a1")])
    checkpoint = _ready_checkpoint(chat_id)
    owner = _FakeGenerationOwner(chat_id)
    recorder = _fr(tmp_path)

    result = ct.deliberate_reconstruct(chat_id, owner, recorder=recorder)

    expected_reconstruction = reconstruct_chat_context(chat_id)
    assert owner.ctx.history == list(expected_reconstruction.messages) + [
        {"role": "assistant", "content": ct.render_continuity_message(checkpoint.payload)}
    ]
    assert owner.ctx._last_usage_snapshot is None
    assert owner.ctx._pending_compaction == []
    assert owner.current_generation() == 1
    assert result.generation_before == 0
    assert result.generation_after == 1
    assert result.checkpoint_id == checkpoint.id
    assert result.durable_row_count == expected_reconstruction.restored_row_count

    events = _events(recorder)
    types_seen = [e["event_type"] for e in events]
    assert types_seen == ["context.reconstruction.requested", "context.reconstruction.swapped"]
    assert events[-1]["severity"] == "info"


def test_happy_path_continuity_message_appears_exactly_once(tmp_path):
    chat_id = _seed_chat([("user", "u1")])
    _ready_checkpoint(chat_id)
    owner = _FakeGenerationOwner(chat_id)

    ct.deliberate_reconstruct(chat_id, owner, recorder=_fr(tmp_path))

    assistant_msgs = [m for m in owner.ctx.history if m["role"] == "assistant"]
    continuity_msgs = [m for m in assistant_msgs if "continuity summary" in m["content"]]
    assert len(continuity_msgs) == 1
    assert owner.ctx.history[-1] is continuity_msgs[0]


def test_happy_path_usage_accounting_reflects_new_history_immediately(tmp_path):
    chat_id = _seed_chat([("user", "u1"), ("assistant", "a1")])
    _ready_checkpoint(chat_id)
    owner = _FakeGenerationOwner(chat_id)
    owner.ctx.context_usage_snapshot()  # populate a stale cache first
    assert owner.ctx._last_usage_snapshot is not None

    ct.deliberate_reconstruct(chat_id, owner, recorder=_fr(tmp_path))

    assert owner.ctx._last_usage_snapshot is None
    fresh = owner.ctx.context_usage_snapshot(refresh=True)
    assert owner.ctx._last_usage_snapshot == fresh


# ── Failure atomicity ────────────────────────────────────────────────────

def _assert_untouched(owner, old_history, recorder):
    assert owner.ctx.history is old_history
    events = _events(recorder)
    swapped = [e for e in events if e["event_type"] == "context.reconstruction.swapped"]
    assert swapped == []


def test_no_usable_checkpoint_raises(tmp_path):
    chat_id = _seed_chat([("user", "u1")])
    owner = _FakeGenerationOwner(chat_id)
    old_history = owner.ctx.history
    recorder = _fr(tmp_path)

    with pytest.raises(ct.NoUsableCheckpoint):
        ct.deliberate_reconstruct(chat_id, owner, recorder=recorder)

    _assert_untouched(owner, old_history, recorder)
    events = _events(recorder)
    assert events[-1]["event_type"] == "context.reconstruction.rejected"
    assert events[-1]["severity"] == "warning"


def test_durable_spine_changes_during_preparation_raises_stale(tmp_path, monkeypatch):
    chat_id = _seed_chat([("user", "u1")])
    _ready_checkpoint(chat_id)
    owner = _FakeGenerationOwner(chat_id)
    old_history = owner.ctx.history
    recorder = _fr(tmp_path)

    real_reconstruct = ct.reconstruct_chat_context
    call_count = {"n": 0}

    def _reconstruct_then_mutate(cid, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            memory.save_chat_message(chat_id, "user", "a late concurrent message")
        return real_reconstruct(cid, **kwargs)

    monkeypatch.setattr(ct, "reconstruct_chat_context", _reconstruct_then_mutate)

    with pytest.raises(ct.CheckpointStale):
        ct.deliberate_reconstruct(chat_id, owner, recorder=recorder)

    _assert_untouched(owner, old_history, recorder)


def test_context_skip_changes_during_preparation_raises_stale_via_fingerprint(tmp_path, monkeypatch):
    chat_id = _seed_chat([("user", "u1"), ("assistant", "a1"), ("user", "u2")])
    _ready_checkpoint(chat_id, context_skip=0)
    owner = _FakeGenerationOwner(chat_id)
    old_history = owner.ctx.history
    recorder = _fr(tmp_path)

    real_reconstruct = ct.reconstruct_chat_context
    call_count = {"n": 0}

    def _reconstruct_then_compact(cid, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            _write_compaction_skip(chat_id, 1)
        return real_reconstruct(cid, **kwargs)

    monkeypatch.setattr(ct, "reconstruct_chat_context", _reconstruct_then_compact)

    with pytest.raises(ct.CheckpointStale):
        ct.deliberate_reconstruct(chat_id, owner, recorder=recorder)

    _assert_untouched(owner, old_history, recorder)


def test_active_turn_conflict_raises(tmp_path):
    chat_id = _seed_chat([("user", "u1")])
    _ready_checkpoint(chat_id)
    owner = _FakeGenerationOwner(chat_id)
    old_history = owner.ctx.history
    recorder = _fr(tmp_path)

    with emergency_stop.execution_scope(kind="foreground_turn", metadata={"chat_id": chat_id}):
        with pytest.raises(ct.ActiveTurnConflict):
            ct.deliberate_reconstruct(chat_id, owner, recorder=recorder)

    _assert_untouched(owner, old_history, recorder)


def test_active_turn_for_a_different_chat_does_not_block(tmp_path):
    chat_id = _seed_chat([("user", "u1")])
    other_chat_id = _seed_chat([("user", "other")])
    _ready_checkpoint(chat_id)
    owner = _FakeGenerationOwner(chat_id)

    with emergency_stop.execution_scope(kind="foreground_turn", metadata={"chat_id": other_chat_id}):
        result = ct.deliberate_reconstruct(chat_id, owner, recorder=_fr(tmp_path))
    assert result.chat_id == chat_id


def test_generation_changed_during_preparation_raises(tmp_path, monkeypatch):
    chat_id = _seed_chat([("user", "u1")])
    _ready_checkpoint(chat_id)
    owner = _FakeGenerationOwner(chat_id)
    old_history = owner.ctx.history
    recorder = _fr(tmp_path)

    real_reconstruct = ct.reconstruct_chat_context

    def _reconstruct_then_bump(cid, **kwargs):
        owner.bump()  # simulates a chat switch/new turn during off-side prep
        monkeypatch.setattr(ct, "reconstruct_chat_context", real_reconstruct)
        return real_reconstruct(cid, **kwargs)

    monkeypatch.setattr(ct, "reconstruct_chat_context", _reconstruct_then_bump)

    with pytest.raises(ct.ContextGenerationChanged):
        ct.deliberate_reconstruct(chat_id, owner, recorder=recorder)

    _assert_untouched(owner, old_history, recorder)


def test_active_chat_changed_during_preparation_raises(tmp_path, monkeypatch):
    chat_id = _seed_chat([("user", "u1")])
    other_chat_id = _seed_chat([("user", "u2")])
    _ready_checkpoint(chat_id)
    owner = _FakeGenerationOwner(chat_id)
    old_history = owner.ctx.history
    recorder = _fr(tmp_path)

    real_reconstruct = ct.reconstruct_chat_context

    def _reconstruct_then_switch(cid, **kwargs):
        owner.chat_id = other_chat_id  # simulates a mid-prep chat switch
        monkeypatch.setattr(ct, "reconstruct_chat_context", real_reconstruct)
        return real_reconstruct(cid, **kwargs)

    monkeypatch.setattr(ct, "reconstruct_chat_context", _reconstruct_then_switch)

    with pytest.raises(ct.ActiveChatChanged):
        ct.deliberate_reconstruct(chat_id, owner, recorder=recorder)

    _assert_untouched(owner, old_history, recorder)


def test_active_chat_already_changed_before_call_raises_immediately(tmp_path):
    chat_id = _seed_chat([("user", "u1")])
    _ready_checkpoint(chat_id)
    owner = _FakeGenerationOwner(chat_id)
    owner.chat_id = 999999  # never call reconstruct_chat_context in this shape
    old_history = owner.ctx.history
    recorder = _fr(tmp_path)

    with pytest.raises(ct.ActiveChatChanged):
        ct.deliberate_reconstruct(chat_id, owner, recorder=recorder)

    _assert_untouched(owner, old_history, recorder)


def test_emergency_stop_fires_after_preparation_raises_cancelled(tmp_path, monkeypatch):
    chat_id = _seed_chat([("user", "u1")])
    _ready_checkpoint(chat_id)
    owner = _FakeGenerationOwner(chat_id)
    old_history = owner.ctx.history
    recorder = _fr(tmp_path)

    real_reconstruct = ct.reconstruct_chat_context

    def _reconstruct_then_latch(cid, **kwargs):
        emergency_stop.latch(reason="test")
        monkeypatch.setattr(ct, "reconstruct_chat_context", real_reconstruct)
        return real_reconstruct(cid, **kwargs)

    monkeypatch.setattr(ct, "reconstruct_chat_context", _reconstruct_then_latch)

    with pytest.raises(ct.ReconstructionCancelled):
        ct.deliberate_reconstruct(chat_id, owner, recorder=recorder)

    _assert_untouched(owner, old_history, recorder)


def test_active_turn_starts_during_preparation_raises_conflict_at_swap(tmp_path, monkeypatch):
    """M17 (mandatory): proves the admission check is a real lifecycle gate
    re-checked AT the swap boundary, not a stale snapshot read once at
    entry. A foreground_turn lease opens for this chat AFTER
    deliberate_reconstruct() has already captured its epoch/generation and
    started off-side preparation (i.e. after the exact moment a
    snapshot-at-entry design would have already decided 'idle') -- and is
    still correctly caught."""
    chat_id = _seed_chat([("user", "u1")])
    _ready_checkpoint(chat_id)
    owner = _FakeGenerationOwner(chat_id)
    old_history = owner.ctx.history
    recorder = _fr(tmp_path)

    real_reconstruct = ct.reconstruct_chat_context
    lease_cm = {"cm": None}

    def _reconstruct_then_start_turn(cid, **kwargs):
        cm = emergency_stop.execution_scope(kind="foreground_turn", metadata={"chat_id": chat_id})
        cm.__enter__()
        lease_cm["cm"] = cm
        monkeypatch.setattr(ct, "reconstruct_chat_context", real_reconstruct)
        return real_reconstruct(cid, **kwargs)

    monkeypatch.setattr(ct, "reconstruct_chat_context", _reconstruct_then_start_turn)

    try:
        with pytest.raises(ct.ActiveTurnConflict):
            ct.deliberate_reconstruct(chat_id, owner, recorder=recorder)
    finally:
        if lease_cm["cm"] is not None:
            lease_cm["cm"].__exit__(None, None, None)

    _assert_untouched(owner, old_history, recorder)


def test_swap_lock_serializes_concurrent_swaps(tmp_path, monkeypatch):
    """Real-thread proof (not inference from 'it's a lock'): a second
    caller cannot acquire core.context_transaction._swap_lock while a
    deliberate_reconstruct() call is inside its own critical section.
    Thread A is paused *inside* the lock via a monkeypatched get_checkpoint
    that blocks on an Event; Thread B then attempts to acquire the same
    lock object directly. The load-bearing assertion is that B has NOT
    acquired it during a bounded window while A still holds it -- mirrors
    tests/test_context_checkpoints.py::
    test_palace_write_cannot_land_between_finalize_spine_observation_and_ready_commit's
    event-log-ordering style, not sleep-based timing."""
    chat_id = _seed_chat([("user", "u1")])
    _ready_checkpoint(chat_id)
    owner = _FakeGenerationOwner(chat_id)
    recorder = _fr(tmp_path)

    a_entered_lock = threading.Event()
    a_may_proceed = threading.Event()
    log = []
    log_lock = threading.Lock()

    def _record(tag):
        with log_lock:
            log.append(tag)

    real_get_checkpoint = ct.get_checkpoint

    def _blocking_get_checkpoint(cpid, **kwargs):
        _record("a_entered_lock")
        a_entered_lock.set()
        a_may_proceed.wait(timeout=5)
        return real_get_checkpoint(cpid, **kwargs)

    monkeypatch.setattr(ct, "get_checkpoint", _blocking_get_checkpoint)

    def _run_a():
        ct.deliberate_reconstruct(chat_id, owner, recorder=recorder)
        _record("a_committed")

    thread_a = threading.Thread(target=_run_a)
    thread_a.start()
    assert a_entered_lock.wait(timeout=5)

    b_attempting = threading.Event()

    def _run_b():
        b_attempting.set()
        with ct._swap_lock:
            _record("b_acquired_lock")

    thread_b = threading.Thread(target=_run_b)
    thread_b.start()
    assert b_attempting.wait(timeout=5)
    time.sleep(0.05)  # bounded window for B to (wrongly) acquire early
    with log_lock:
        assert "b_acquired_lock" not in log  # still blocked behind A

    a_may_proceed.set()
    thread_a.join(timeout=5)
    thread_b.join(timeout=5)

    assert "a_committed" in log
    assert "b_acquired_lock" in log


def test_durable_row_appended_between_get_checkpoint_and_fingerprint_recheck_raises_stale(tmp_path, monkeypatch):
    """M9: proves the IN-LOCK fingerprint recheck specifically (step 4's
    own reconstruct_chat_context() call), not merely step 2's off-side
    redundant check. Off-side preparation (step 2) completes cleanly
    against the spine as it existed then; the durable spine changes only
    after get_checkpoint()'s in-lock re-fetch (step 4a) but before the
    in-lock fingerprint recheck (step 4b) runs -- a window step 2's own
    check can never see, by construction."""
    chat_id = _seed_chat([("user", "u1")])
    checkpoint = _ready_checkpoint(chat_id)
    owner = _FakeGenerationOwner(chat_id)
    old_history = owner.ctx.history
    recorder = _fr(tmp_path)

    real_get_checkpoint = ct.get_checkpoint

    def _get_checkpoint_then_mutate_spine(cpid, **kwargs):
        record = real_get_checkpoint(cpid, **kwargs)
        memory.save_chat_message(chat_id, "user", "a late concurrent message")
        return record

    monkeypatch.setattr(ct, "get_checkpoint", _get_checkpoint_then_mutate_spine)

    with pytest.raises(ct.CheckpointStale):
        ct.deliberate_reconstruct(chat_id, owner, recorder=recorder)

    _assert_untouched(owner, old_history, recorder)


def test_corrupted_context_skip_column_rejected_even_with_valid_fingerprint(tmp_path):
    """A5I-R1: a checkpoint row whose context_skip column was tampered
    with directly (bypassing begin_checkpoint()/finalize_checkpoint()'s
    own contract of always writing durable_spine_fingerprint and
    context_skip together as one matched pair) must be rejected at the
    swap boundary -- even though its durable_spine_fingerprint column is
    genuinely valid and still matches the current live spine, and even
    though get_latest_usable_checkpoint() itself still selects this row as
    'usable' (that lookup only ever compares fingerprints too). Confirmed
    empirically: skip=0 and skip=1 produce different fingerprints for this
    fixture (fp0 != fp1 asserted below), so this is a real, detectable
    mismatch, not a case where the two skip values happen to select the
    same row set."""
    chat_id = _seed_chat([("user", "u1"), ("assistant", "a1"), ("user", "u2")])
    fp0 = _spine(chat_id, context_skip=0)
    fp1 = _spine(chat_id, context_skip=1)
    assert fp0 != fp1  # sanity: skip genuinely matters for this fixture

    checkpoint = _ready_checkpoint(chat_id, context_skip=0)
    assert checkpoint.durable_spine_fingerprint == fp0
    _corrupt_checkpoint_context_skip(checkpoint.id, wrong_skip=1)

    # The corruption is invisible to get_latest_usable_checkpoint() itself
    # -- it only ever compares fingerprints, never context_skip (source-
    # vetted: core/context_checkpoints.py has zero context_skip comparison
    # anywhere in its lookup/re-fetch paths). This checkpoint is still
    # selected as "usable" going into the transaction.
    assert cc.get_latest_usable_checkpoint(chat_id).id == checkpoint.id

    owner = _FakeGenerationOwner(chat_id)
    old_history = owner.ctx.history
    recorder = _fr(tmp_path)

    with pytest.raises(ct.CheckpointStale, match="context_skip"):
        ct.deliberate_reconstruct(chat_id, owner, recorder=recorder)

    _assert_untouched(owner, old_history, recorder)


def test_stale_checkpoint_never_silently_retried_even_when_a_fresh_one_exists(tmp_path, monkeypatch):
    """M15: this call captured checkpoint A. While it prepares, the durable
    spine moves (a manual compaction lands) AND a brand-new, perfectly
    valid checkpoint B is built for the NEW spine -- a scenario where an
    internal auto-retry would 'succeed' by silently switching checkpoints
    out from under the caller. §7.D is explicit that this must never
    happen inside one transaction call: retrying is the CALLER's decision,
    a wholly new invocation, never internal fallback logic. This is a
    stronger proof than a scenario where no replacement checkpoint exists
    at all (there, a broken auto-retry and a correct raise are
    indistinguishable by outcome)."""
    chat_id = _seed_chat([("user", "u1"), ("assistant", "a1"), ("user", "u2")])
    checkpoint_a = _ready_checkpoint(chat_id, context_skip=0)
    owner = _FakeGenerationOwner(chat_id)
    old_history = owner.ctx.history
    recorder = _fr(tmp_path)

    real_reconstruct = ct.reconstruct_chat_context
    checkpoint_b_id = {"id": None}

    def _reconstruct_then_build_fresh_checkpoint(cid, **kwargs):
        _write_compaction_skip(chat_id, 1)
        checkpoint_b = _ready_checkpoint(chat_id, context_skip=1)
        checkpoint_b_id["id"] = checkpoint_b.id
        monkeypatch.setattr(ct, "reconstruct_chat_context", real_reconstruct)
        return real_reconstruct(cid, **kwargs)

    monkeypatch.setattr(ct, "reconstruct_chat_context", _reconstruct_then_build_fresh_checkpoint)

    # Sanity: a fresh call to get_latest_usable_checkpoint() really would
    # find checkpoint B now -- proving this isn't a "nothing to retry to"
    # scenario like the CheckpointStale tests above.
    with pytest.raises(ct.CheckpointStale):
        ct.deliberate_reconstruct(chat_id, owner, recorder=recorder)

    assert checkpoint_b_id["id"] is not None
    assert cc.get_latest_usable_checkpoint(chat_id).id == checkpoint_b_id["id"]
    _assert_untouched(owner, old_history, recorder)


def test_checkpoint_superseded_between_preparation_and_swap_raises_stale(tmp_path, monkeypatch):
    chat_id = _seed_chat([("user", "u1")])
    checkpoint = _ready_checkpoint(chat_id)
    # A second, newer READY checkpoint against the same spine, then manually
    # supersede the first -- exercises the fresh re-fetch inside the lock.
    owner = _FakeGenerationOwner(chat_id)
    old_history = owner.ctx.history
    recorder = _fr(tmp_path)

    real_get_checkpoint = ct.get_checkpoint

    def _get_checkpoint_then_ready(cpid, **kwargs):
        cc.supersede_checkpoint(checkpoint.id, chat_id)
        return real_get_checkpoint(cpid, **kwargs)

    monkeypatch.setattr(ct, "get_checkpoint", _get_checkpoint_then_ready)

    with pytest.raises(ct.CheckpointStale):
        ct.deliberate_reconstruct(chat_id, owner, recorder=recorder)

    _assert_untouched(owner, old_history, recorder)


def test_candidate_render_failure_propagates_uncaught_and_leaves_history_untouched(tmp_path, monkeypatch):
    chat_id = _seed_chat([("user", "u1")])
    _ready_checkpoint(chat_id)
    owner = _FakeGenerationOwner(chat_id)
    old_history = owner.ctx.history
    recorder = _fr(tmp_path)

    def _boom(payload):
        raise RuntimeError("renderer bug")

    monkeypatch.setattr(ct, "render_continuity_message", _boom)

    with pytest.raises(RuntimeError, match="renderer bug"):
        ct.deliberate_reconstruct(chat_id, owner, recorder=recorder)

    assert owner.ctx.history is old_history
    events = _events(recorder)
    # A bare programming-bug exception is not a typed ReconstructionError --
    # it is never caught, so no .rejected event is recorded for it either.
    assert [e["event_type"] for e in events] == ["context.reconstruction.requested"]


def test_successful_swap_rebinds_to_a_new_list_object_not_in_place_mutation(tmp_path):
    """M7: the install must be a single rebind (ctx.history = candidate),
    never ctx.history.clear() + .extend(candidate) on the SAME list object
    -- an in-place mutation would let a concurrent read (e.g. build_
    messages()'s list(self.history) snapshot, or context_usage_snapshot's
    background 1s timer) observe a transient, partially-rebuilt list.
    A rebind is atomic from any other thread's perspective; an in-place
    clear+extend is not."""
    chat_id = _seed_chat([("user", "u1")])
    _ready_checkpoint(chat_id)
    owner = _FakeGenerationOwner(chat_id)
    old_history = owner.ctx.history

    ct.deliberate_reconstruct(chat_id, owner, recorder=_fr(tmp_path))

    assert owner.ctx.history is not old_history


def test_invalid_continuity_payload_raises_not_silent_fallback(tmp_path):
    """M14: a checkpoint whose payload passed A3's own hash/version/shape
    validation (so it reads back as READY with payload is not None) but is
    missing an A4-schema-required key must raise InvalidContinuityPayload,
    never silently fall back to a durable-only reconstruction. core.
    context_checkpoints.py's own _load_payload() only validates JSON
    shape/hash/version -- it has no knowledge of A4's schema -- so this is
    a real, reachable state, not a hypothetical one."""
    chat_id = _seed_chat([("user", "u1")])
    malformed_payload = {"schema_version": 1, "reported": [], "inferred": []}  # missing machine_facts
    checkpoint = _ready_checkpoint(chat_id, payload=malformed_payload)
    assert checkpoint.payload is not None  # A3 accepted it -- the gap this test targets
    owner = _FakeGenerationOwner(chat_id)
    old_history = owner.ctx.history
    recorder = _fr(tmp_path)

    with pytest.raises(ct.InvalidContinuityPayload):
        ct.deliberate_reconstruct(chat_id, owner, recorder=recorder)

    _assert_untouched(owner, old_history, recorder)
    events = _events(recorder)
    rejected = [e for e in events if e["event_type"] == "context.reconstruction.rejected"][0]
    assert rejected["severity"] == "error"  # M14/A5D §13: the one failure class that should be unreachable


# ── Idempotence ──────────────────────────────────────────────────────────

def test_reusing_the_same_checkpoint_twice_revalidates_independently(tmp_path):
    chat_id = _seed_chat([("user", "u1")])
    _ready_checkpoint(chat_id)
    owner = _FakeGenerationOwner(chat_id)
    recorder = _fr(tmp_path)

    first = ct.deliberate_reconstruct(chat_id, owner, recorder=recorder)
    # After the first swap, the live history now ends with a continuity
    # message rather than a durable row -- but the durable spine (SQLite)
    # is untouched, so the same checkpoint's binding is still valid.
    second = ct.deliberate_reconstruct(chat_id, owner, recorder=recorder)

    assert second.checkpoint_id == first.checkpoint_id
    assert second.generation_after == first.generation_after + 1
    # No duplicated/accumulated continuity artifacts -- each swap rebuilds
    # the candidate from the durable spine and installs exactly one.
    continuity_msgs = [m for m in owner.ctx.history if m["role"] == "assistant"
                        and "continuity summary" in m["content"]]
    assert len(continuity_msgs) == 1


# ── Provider shape ───────────────────────────────────────────────────────

def test_installed_continuity_message_has_only_role_and_content_keys(tmp_path):
    chat_id = _seed_chat([("user", "u1")])
    _ready_checkpoint(chat_id)
    owner = _FakeGenerationOwner(chat_id)

    ct.deliberate_reconstruct(chat_id, owner, recorder=_fr(tmp_path))

    installed = owner.ctx.history[-1]
    assert set(installed.keys()) == {"role", "content"}
    assert installed["role"] == "assistant"


# ── Telemetry field/content discipline ──────────────────────────────────

def test_swapped_telemetry_never_contains_raw_conversation_content(tmp_path):
    chat_id = _seed_chat([("user", "a very secret message body")])
    _ready_checkpoint(chat_id, payload=_valid_payload(statement="a secret statement"))
    owner = _FakeGenerationOwner(chat_id)
    recorder = _fr(tmp_path)

    ct.deliberate_reconstruct(chat_id, owner, recorder=recorder)

    events = _events(recorder)
    swapped = [e for e in events if e["event_type"] == "context.reconstruction.swapped"][0]
    assert "a very secret message body" not in swapped["fields_json"]
    assert "a secret statement" not in swapped["fields_json"]


def test_rejected_telemetry_carries_failure_class(tmp_path):
    chat_id = _seed_chat([("user", "u1")])
    owner = _FakeGenerationOwner(chat_id)
    recorder = _fr(tmp_path)

    with pytest.raises(ct.NoUsableCheckpoint):
        ct.deliberate_reconstruct(chat_id, owner, recorder=recorder)

    import json as _json
    events = _events(recorder)
    rejected = [e for e in events if e["event_type"] == "context.reconstruction.rejected"][0]
    fields = _json.loads(rejected["fields_json"])
    assert fields["failure_class"] == "NoUsableCheckpoint"
