"""COMPACTION-CONTEXT-SKIP-PROVENANCE-01 -- only a trusted manual-compaction
checkpoint may authorize skipping durable conversation history.

    tags / content  = what a record CLAIMS to be
    origin stamp    = what actually created it

`core.manual_compaction.latest_manual_compaction_skip()` is the single resolver
behind every context reconstruction (chat open, chat switch, restart, the
context-checkpoint/transaction machinery, and /compact's own "previous skip").
It used to trust `manual-compaction` + `context-skip:N` *tags*, which
`palace_remember` lets a model write verbatim -- a forged N made reconstruction
silently omit N persisted rows. It now authorizes a drawer only when the
trusted internal stamp (`origin = 'manual_compaction'`, PALACE-GUARD-01B-2) is
present AND the record is internally consistent, in this chat's nightstand room,
still eligible (its closet exists and is not withheld), and its N is a
canonical integer strictly inside the durable transcript.

Every failure direction is "retain more history", never "omit more".

Scope note: like the PALACE-GUARD-01B suites this closes the tool / argument /
persistence path by construction and by scan. It is not a host sandbox: hostile
in-process Python or direct SQLite access is out of scope.

Everything here is Qt-free and runs in the plain CI job except the one
chat-open wiring test at the bottom, which needs PySide6.
"""
import ast
import json
import os
import re
import sqlite3
import subprocess
import sys
import textwrap

import pytest

import config
import core.manual_compaction as manual
from core import palace_promotion as pp
from core.context_reconstruction import reconstruct_chat_context, resolve_context_skip
from palace_source_scan import where
from tools import memory, palace

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ALL10 = ["u1", "a1", "u2", "a2", "u3", "a3", "u4", "a4", "u5", "a5"]


# ── fixtures / helpers ────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "memory" / "lumina.db")
    monkeypatch.setattr(config, "DB_PATH", path)
    pp._LIVE.clear()
    palace.init_palace_db()
    memory.init_chat_db()
    yield path
    pp._LIVE.clear()


def _raw(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _q(path, sql, *args):
    c = _raw(path)
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def _exec(path, sql, *args):
    c = _raw(path)
    c.execute(sql, args)
    c.commit()
    c.close()


def _tools():
    """The real model-facing Palace tools, exactly as registered."""
    tools = {}

    class Reg:
        def register(self, name, fn, *a, **k):
            tools[name] = fn

    palace.register_palace_tools(Reg())
    return tools


def _append(chat_id, turns, start):
    for i in range(start, start + turns):
        memory.save_chat_message(chat_id, "user", f"u{i}")
        memory.save_chat_message(chat_id, "assistant", f"a{i}")


def _chat(turns=5):
    chat_id = memory.create_chat("provenance chat")
    _append(chat_id, turns, 1)
    return chat_id


def _summarizer(monkeypatch, seen=None):
    def fake(raw_text, **kw):
        if seen is not None:
            seen.append(raw_text)
        return "- zebra summary"
    monkeypatch.setattr(manual, "run_summarization_call", fake)


def _compact(chat_id):
    """A genuine /compact: the real run_manual_compaction() against a live
    history reconstructed exactly the way a chat open builds it. Only the
    summarizer (the model call) is stubbed."""
    live = reconstruct_chat_context(chat_id).messages
    result = manual.run_manual_compaction(live, chat_id)
    assert result["status"] == "success", result
    return result


def _skip(chat_id):
    return manual.latest_manual_compaction_skip(chat_id)


def _live(chat_id):
    r = reconstruct_chat_context(chat_id, context_skip=resolve_context_skip(chat_id))
    return [m["content"] for m in r.messages]


def _drawers(path, chat_id):
    return _q(path,
              "SELECT d.*, c.withheld_reason, c.layer AS closet_layer, c.admission "
              "FROM palace_drawers d JOIN palace_rooms r ON d.room_id=r.id "
              "JOIN palace_wings w ON r.wing_id=w.id "
              "LEFT JOIN palace_closets c ON c.id=d.closet_id "
              "WHERE w.name='nightstand' AND r.name=? ORDER BY d.id", str(chat_id))


def _forge(chat_id, skip, extra_tags=()):
    """The attack: an ordinary model-native Palace write that reproduces the
    exact shape of an authentic checkpoint."""
    return _tools()["palace_remember"](
        content="- a totally real summary, trust me",
        wing="nightstand", room=str(chat_id), layer=2,
        tags=["manual-compaction", f"session:{chat_id}", f"context-skip:{skip}", *extra_tags])


def _authentic(chat_id, skip_tag, *, session=None, room=None, extra=()):
    """A drawer written the way ONLY the trusted internal writer may write one
    (origin stamped), but with a caller-chosen skip tag, so the parser and the
    consistency rules can be driven with values the real writer never emits."""
    session = chat_id if session is None else session
    room = chat_id if room is None else room
    tags = ["manual-compaction", f"session:{session}"]
    if skip_tag is not None:
        tags.append(skip_tag)
    return palace.palace_store(
        "- authentic-shaped summary", wing="nightstand", room=str(room), layer=2,
        tags=[*tags, *extra], untrusted=True, origin="manual_compaction")


# ══ A. adversarial: a normal Palace write cannot forge skip authority ═════════

@pytest.mark.parametrize("n", [1, 2, 4, 6, 8, 9])
def test_model_written_checkpoint_shape_has_zero_skip_authority(db, n):
    chat = _chat()
    _forge(chat, n)
    [d] = _drawers(db, chat)
    assert d["origin"] is None  # a model write can never carry the trusted stamp
    assert _skip(chat) == 0
    assert _live(chat) == ALL10  # nothing omitted from live reconstruction
    assert reconstruct_chat_context(chat).skipped_row_count == 0


def test_hall_path_cannot_carry_a_checkpoint(db):
    chat = _chat()
    _tools()["palace_hall"](
        content=f"manual-compaction session:{chat} context-skip:6", hall="facts")
    assert _skip(chat) == 0 and _live(chat) == ALL10


@pytest.mark.parametrize("tag", ["origin:manual_compaction", "origin=manual_compaction",
                                 "manual_compaction", "trust:trusted", "synthesized:manual_compaction"])
def test_origin_shaped_tags_never_write_the_origin_column(db, tag):
    chat = _chat()
    _forge(chat, 6, extra_tags=[tag])
    assert [d["origin"] for d in _drawers(db, chat)] == [None]
    assert _skip(chat) == 0


@pytest.mark.parametrize("tool", ["palace_remember", "palace_hall"])
def test_origin_argument_is_impossible_through_the_model_tool_path(db, tool):
    chat = _chat()
    args = {"content": "x", "origin": "manual_compaction"}
    if tool == "palace_remember":
        args.update(wing="nightstand", room=str(chat), tags=["manual-compaction", f"session:{chat}",
                                                            "context-skip:6"])
    before = _q(db, "SELECT COUNT(*) AS n FROM palace_drawers")[0]["n"]
    with pytest.raises(TypeError):
        _tools()[tool](**args)
    assert _q(db, "SELECT COUNT(*) AS n FROM palace_drawers")[0]["n"] == before
    assert _skip(chat) == 0


def test_origin_argument_through_the_real_registry_dispatch_writes_nothing(db):
    from tools.registry import ToolRegistry
    reg = ToolRegistry()
    palace.register_palace_tools(reg)
    chat = _chat()
    out = reg.call("palace_remember", {
        "content": "x", "wing": "nightstand", "room": str(chat), "origin": "manual_compaction",
        "tags": ["manual-compaction", f"session:{chat}", "context-skip:6"]})
    assert out.startswith("[Tool error: bad arguments"), out
    assert _drawers(db, chat) == [] and _skip(chat) == 0


def test_the_trusted_vocabulary_stays_closed(db):
    with pytest.raises(ValueError):
        palace.palace_store("x", origin="manual-compaction")  # tag spelling is not an origin
    with pytest.raises(ValueError):
        palace.palace_store("x", origin="owner_checkpoint")


def test_exact_clone_of_a_real_checkpoint_is_not_trusted(db, monkeypatch):
    _summarizer(monkeypatch)
    chat = _chat()
    _compact(chat)
    [real] = _drawers(db, chat)
    assert real["origin"] == "manual_compaction" and _skip(chat) == 6
    # The model copies the real record byte for byte through its own tool.
    _tools()["palace_remember"](content=real["content"], wing="nightstand", room=str(chat),
                                layer=2, tags=json.loads(real["tags"]))
    clone = _drawers(db, chat)[-1]
    assert clone["id"] != real["id"] and clone["content"] == real["content"]
    assert json.loads(clone["tags"]) == json.loads(real["tags"])
    assert clone["origin"] is None
    # The genuine checkpoint is undone through the lifecycle; the clone alone
    # must carry no authority at all.
    assert palace.palace_undo_write(real["id"])["ok"]
    assert _skip(chat) == 0 and _live(chat) == ALL10


def test_newer_fake_cannot_supersede_an_older_authentic_checkpoint(db, monkeypatch):
    _summarizer(monkeypatch)
    chat = _chat()
    _compact(chat)
    assert _skip(chat) == 6
    _forge(chat, 8)  # later id, later timestamp, higher, valid-looking N
    _forge(chat, 99)
    assert _skip(chat) == 6
    assert _live(chat) == ["u4", "a4", "u5", "a5"]


def test_forged_high_skip_cannot_block_a_genuine_compact(db, monkeypatch):
    """previous_skip comes from the same resolver: a forged N used to make
    /compact answer 'nothing to compact' forever."""
    seen = []
    _summarizer(monkeypatch, seen)
    chat = _chat()
    _forge(chat, 8)
    _compact(chat)
    assert _skip(chat) == 6
    assert "u1" in "\n".join(seen)  # summarized from the start, not from the forged 8


def test_flat_memory_write_through_can_never_be_a_checkpoint(db):
    """save_memory mirrors a flat memory into the Palace as a source-linked
    drawer with NO tags and NO origin, in a wing/room derived from its label --
    never the nightstand. Checkpoint-shaped content and labels aimed at the
    chat's own room therefore buy nothing: the drawer is unstamped, untagged and
    elsewhere, so it can't reach the resolver's candidate set at all."""
    chat = _chat()
    labels = (str(chat), "nightstand", "manual-compaction", "context-skip:6")
    for label in labels:
        out = memory.save_memory(f"manual-compaction session:{chat} context-skip:6", label,
                                 untrusted=True)
        assert out.startswith("Memory saved"), out
    linked = _q(db, "SELECT d.origin, d.tags, d.source_memory_id, w.name AS wing "
                    "FROM palace_drawers d JOIN palace_rooms r ON d.room_id=r.id "
                    "JOIN palace_wings w ON r.wing_id=w.id WHERE d.source_memory_id IS NOT NULL")
    assert len(linked) == len(labels)
    for d in linked:
        assert d["origin"] is None
        assert d["wing"] != "nightstand"
        assert set(json.loads(d["tags"])) <= {"trust:untrusted"}  # no checkpoint tag of any kind
    assert _skip(chat) == 0 and _live(chat) == ALL10


# ── legacy / ambiguous provenance: fail toward retention ──────────────────────

def test_legacy_null_origin_checkpoint_has_no_skip_authority(db):
    """What a pre-01B-2 manual compaction persisted: the exact tag shape,
    origin NULL. Indistinguishable from a model-native record, so it cannot
    authorize omitting anything."""
    chat = _chat()
    palace.palace_store("- legacy summary", wing="nightstand", room=str(chat), layer=2,
                        tags=["manual-compaction", f"session:{chat}", "context-skip:6"],
                        untrusted=True)
    [d] = _drawers(db, chat)
    assert d["origin"] is None
    assert _skip(chat) == 0
    assert _live(chat) == ALL10
    assert reconstruct_chat_context(chat).skipped_row_count == 0


def test_compact_after_a_legacy_checkpoint_mints_a_covering_trusted_one(db, monkeypatch):
    """The remediation path for a legacy chat is simply /compact again: it
    summarizes the whole prefix and stamps a trusted checkpoint."""
    seen = []
    _summarizer(monkeypatch, seen)
    chat = _chat()
    palace.palace_store("- legacy summary", wing="nightstand", room=str(chat), layer=2,
                        tags=["manual-compaction", f"session:{chat}", "context-skip:4"],
                        untrusted=True)
    assert _skip(chat) == 0
    result = _compact(chat)
    assert result["compacted_persisted_rows"] == 6  # the full prefix, not 6-4
    assert "u1" in "\n".join(seen)
    assert _skip(chat) == 6
    assert _live(chat) == ["u4", "a4", "u5", "a5"]


def test_pre_migration_database_is_all_legacy_and_migration_classifies_nothing(db):
    chat = _chat()
    palace.palace_store("- legacy summary", wing="nightstand", room=str(chat), layer=2,
                        tags=["manual-compaction", f"session:{chat}", "context-skip:6"],
                        untrusted=True)
    # Reduce the schema to what a database from before PALACE-GUARD-01B-2 has.
    assert sqlite3.sqlite_version_info >= (3, 35), "DROP COLUMN needs SQLite 3.35+"
    _exec(db, "ALTER TABLE palace_drawers DROP COLUMN origin")
    _exec(db, "ALTER TABLE palace_closets DROP COLUMN withheld_reason")
    cols = [r["name"] for r in _q(db, "PRAGMA table_info(palace_drawers)")]
    assert "origin" not in cols
    assert _skip(chat) == 0  # no trusted stamp can exist yet; must not raise
    palace.init_palace_db()  # the real startup migration
    [d] = _drawers(db, chat)
    assert d["origin"] is None  # migration invented no provenance
    assert "context-skip:6" in json.loads(d["tags"])  # and destroyed nothing
    assert _skip(chat) == 0
    assert _live(chat) == ALL10


# ── wrong chat / session ──────────────────────────────────────────────────────

def test_checkpoint_of_another_chat_is_never_used(db, monkeypatch):
    _summarizer(monkeypatch)
    a, b = _chat(), _chat()
    _compact(a)
    assert _skip(a) == 6
    assert _skip(b) == 0 and _live(b) == ALL10
    # Trusted stamp, but parked in b's room while claiming session a ...
    _authentic(b, "context-skip:6", session=a)
    assert _skip(b) == 0
    # ... and trusted stamp in a's room while claiming session b: a is unmoved.
    _authentic(a, "context-skip:8", session=b)
    assert _skip(a) == 6


def test_trusted_stamp_outside_the_nightstand_wing_is_ignored(db):
    chat = _chat()
    palace.palace_store("- elsewhere", wing="sessions", room=str(chat), layer=2,
                        tags=["manual-compaction", f"session:{chat}", "context-skip:6"],
                        untrusted=True, origin="manual_compaction")
    assert _skip(chat) == 0


# ── context-skip value semantics (driven with a trusted stamp, so only the
#    parser / bounds rules decide the outcome) ────────────────────────────────

@pytest.mark.parametrize("tag,expected", [
    ("context-skip:6", 6),
    ("context-skip:9", 9),                      # one durable row left live: the maximum
    ("context-skip:1", 1),
    ("context-skip:0", 0),
    ("context-skip:10", 0),                     # == durable rows: would restore nothing
    ("context-skip:11", 0),                     # > transcript length
    ("context-skip:" + "9" * 40, 0),            # huge
    ("context-skip:" + "9" * 5000, 0),          # beyond int<->str digit limit
    ("context-skip:-3", 0),                     # negative
    ("context-skip:-0", 0),
    ("context-skip:+6", 0),                     # non-canonical spellings of a valid 6
    ("context-skip: 6", 0),
    ("context-skip:6 ", 0),
    ("context-skip:06", 0),
    ("context-skip:6_0", 0),
    ("context-skip:٦", 0),                      # ARABIC-INDIC DIGIT SIX
    ("context-skip:６", 0),                      # FULLWIDTH DIGIT SIX
    ("context-skip:6.0", 0),                    # float
    ("context-skip:6.5", 0),
    ("context-skip:6e0", 0),
    ("context-skip:0x6", 0),
    ("context-skip:True", 0),                   # bool / weird scalar
    ("context-skip:None", 0),
    ("context-skip:nan", 0),
    ("context-skip:abc", 0),
    ("context-skip:", 0),
    ("context-skip", 0),                        # prefix without a value
    ("Context-Skip:6", 0),                      # wrong case is a different tag
    ("xcontext-skip:6", 0),
])
def test_context_skip_value_semantics(db, tag, expected):
    chat = _chat()
    _authentic(chat, tag)
    assert _skip(chat) == expected
    r = reconstruct_chat_context(chat, context_skip=resolve_context_skip(chat))
    assert r.context_skip == expected
    assert r.restored_row_count == 10 - expected  # never fewer rows than the skip allows


def test_the_row_bound_is_this_chats_rows_not_the_database_total(db):
    big, small = _chat(5), _chat(2)  # 10 and 4 durable conversation rows
    _authentic(small, "context-skip:6")  # >= small's 4 rows, yet < the 14 in the database
    assert _skip(small) == 0
    assert _live(small) == ["u1", "a1", "u2", "a2"]
    assert _skip(big) == 0
    _authentic(small, "context-skip:3")  # a real in-bounds value still counts
    assert _skip(small) == 3


def test_only_conversation_rows_count_toward_the_row_bound(db):
    """Roles that are never restored (think, tool, ...) cannot pad the
    transcript so that an out-of-range N looks in range."""
    chat = _chat(2)  # 4 conversation rows
    for i in range(6):
        memory.save_chat_message(chat, "think", f"t{i}")
    _authentic(chat, "context-skip:6")  # < the 10 stored rows, >= the 4 conversation rows
    assert _skip(chat) == 0
    assert _live(chat) == ["u1", "a1", "u2", "a2"]


def test_no_context_skip_tag_at_all_is_no_checkpoint(db):
    chat = _chat()
    _authentic(chat, None)
    assert _skip(chat) == 0


def test_duplicate_markers_in_one_drawer_are_ambiguous_and_refused(db):
    chat = _chat()
    _authentic(chat, "context-skip:4", extra=["context-skip:6"])
    assert _skip(chat) == 0
    chat2 = _chat()
    _authentic(chat2, "context-skip:4", extra=["context-skip:4"])
    assert _skip(chat2) == 0


def test_duplicate_markers_across_drawers_resolve_deterministically(db):
    chat = _chat()
    for tag in ("context-skip:4", "context-skip:6", "context-skip:6", "context-skip:2"):
        _authentic(chat, tag)  # insertion order deliberately not ascending
    assert _skip(chat) == 6


def test_a_malformed_drawer_does_not_poison_a_valid_one(db):
    chat = _chat()
    _authentic(chat, "context-skip:4")
    _authentic(chat, "context-skip:banana")
    _authentic(chat, "context-skip:99")
    assert _skip(chat) == 4


@pytest.mark.parametrize("raw", [
    "not-json", '{"a": 1}', '"manual-compaction"', "null", "[1, 2]", "[]",
    '["manual-compaction", "session:{c}", "context-skip:6", 7]',   # non-string element
    '["manual-compaction", "session:{c}", "context-skip:6", null]',
    '["session:{c}", "context-skip:6"]',                           # no manual-compaction tag
    '["manual-compaction", "context-skip:6"]',                     # no session tag
    '["manual-compaction", "session:{c}x", "context-skip:6"]',     # session tag not exact
    '["manual-compaction ", "session:{c}", "context-skip:6"]',
])
def test_malformed_or_inconsistent_tags_never_authorize(db, raw):
    chat = _chat()
    d = _authentic(chat, "context-skip:6")["drawer_id"]
    _exec(db, "UPDATE palace_drawers SET tags=? WHERE id=?", raw.replace("{c}", str(chat)), d)
    assert _skip(chat) == 0


def test_null_tags_column_never_authorizes(db):
    chat = _chat()
    d = _authentic(chat, "context-skip:6")["drawer_id"]
    _exec(db, "UPDATE palace_drawers SET tags=NULL WHERE id=?", d)
    assert _skip(chat) == 0


# ══ B. multiple checkpoints, undo, quarantine, moved ═════════════════════════

def test_two_real_compactions_and_deterministic_fallback_on_undo(db, monkeypatch):
    _summarizer(monkeypatch)
    chat = _chat()
    _compact(chat)
    assert _skip(chat) == 6
    _append(chat, 2, 6)  # u6..a7 -> 14 durable rows
    second = _compact(chat)
    assert second["compacted_persisted_rows"] == 4 and second["skip_conversation_messages"] == 10
    assert _skip(chat) == 10
    first_d, second_d = _drawers(db, chat)
    assert first_d["origin"] == second_d["origin"] == "manual_compaction"

    _forge(chat, 12)  # a later fake must not move the authentic checkpoint
    assert _skip(chat) == 10
    assert _live(chat) == ["u6", "a6", "u7", "a7"]

    # Undo the newest genuine checkpoint: its skip rolls back with it.
    assert palace.palace_undo_write(second_d["id"])["ok"]
    assert _skip(chat) == 6
    assert _live(chat)[0] == "u4"
    # Undo the older one too: only the fake is left, and it carries nothing.
    assert palace.palace_undo_write(first_d["id"])["ok"]
    assert _skip(chat) == 0
    assert len(_live(chat)) == 14


def test_quarantined_checkpoint_loses_skip_authority(db, monkeypatch):
    """Undoing a sibling from a drifted closet withholds the whole closet
    (01B-2 law): its summaries stop being injected, so the rows they replaced
    must come back too -- otherwise history would vanish from BOTH places."""
    _summarizer(monkeypatch)
    chat = _chat()
    _compact(chat)
    _append(chat, 2, 6)
    _compact(chat)
    first_d, second_d = _drawers(db, chat)
    assert first_d["closet_id"] == second_d["closet_id"]
    closet = first_d["closet_id"]
    _exec(db, "UPDATE palace_closets SET compressed = compressed || ' | drifted' WHERE id=?", closet)
    out = palace.palace_undo_write(second_d["id"])
    assert out["ok"] and out["closet_outcome"] == "closet_withheld"
    [survivor] = _drawers(db, chat)
    assert survivor["origin"] == "manual_compaction" and survivor["withheld_reason"] is not None
    assert _skip(chat) == 0
    assert len(_live(chat)) == 14


def test_checkpoint_moved_out_of_its_room_loses_authority(db, monkeypatch):
    _summarizer(monkeypatch)
    chat = _chat()
    _compact(chat)
    [d] = _drawers(db, chat)
    palace.palace_store("- elsewhere", wing="nightstand", room="9999", layer=2,
                        tags=["x"], untrusted=True)
    other_room = _q(db, "SELECT r.id FROM palace_rooms r JOIN palace_wings w ON w.id=r.wing_id "
                        "WHERE w.name='nightstand' AND r.name='9999'")[0]["id"]
    _exec(db, "UPDATE palace_drawers SET room_id=? WHERE id=?", other_room, d["id"])
    assert _skip(chat) == 0


def test_checkpoint_detached_from_its_closet_loses_authority(db, monkeypatch):
    """No closet = no injectable summary. Deleting the closet detaches the
    drawer (ON DELETE SET NULL); the drawer alone must not keep the skip."""
    _summarizer(monkeypatch)
    chat = _chat()
    _compact(chat)
    [d] = _drawers(db, chat)
    _exec(db, "DELETE FROM palace_closets WHERE id=?", d["closet_id"])
    [d] = _drawers(db, chat)
    assert d["closet_id"] is None and d["origin"] == "manual_compaction"
    assert _skip(chat) == 0
    assert _live(chat) == ALL10


def test_owner_promoted_checkpoint_keeps_its_authority(db, monkeypatch):
    """Promotion changes layer only (01B-4): the record is still the genuine
    checkpoint, its summary is injected even more strongly, and origin/room/
    tags are untouched -- so the skip must survive being moved to L1."""
    _summarizer(monkeypatch)
    chat = _chat()
    _compact(chat)
    [d] = _drawers(db, chat)
    snap = pp.capture_promotion_snapshot("drawer", d["id"], 1, operation_id="a1b2c3d4")
    approval = pp.mint_owner_promotion_approval(snap, owner_event="test:click")
    pp.promote_with_owner_approval(approval)
    [d] = _drawers(db, chat)
    assert d["closet_layer"] == 1 and d["admission"] == "explicit_owner_promotion"
    assert d["origin"] == "manual_compaction"
    assert _skip(chat) == 6
    assert _live(chat) == ["u4", "a4", "u5", "a5"]


# ══ C. positive: a fresh /compact still works end to end ═════════════════════

def test_fresh_compact_stamps_trusted_provenance_and_reconstructs_correctly(db, monkeypatch):
    _summarizer(monkeypatch)
    chat = _chat()
    from core.context_reconstruction import load_durable_rows
    before = load_durable_rows(chat)

    result = _compact(chat)
    assert result["skip_conversation_messages"] == 6

    [d] = _drawers(db, chat)
    assert d["origin"] == "manual_compaction"
    assert d["untrusted"] == 1 and d["source_memory_id"] is None
    assert d["closet_id"] is not None and d["withheld_reason"] is None
    assert set(json.loads(d["tags"])) >= {"manual-compaction", f"session:{chat}", "context-skip:6"}

    assert _skip(chat) == 6
    r = reconstruct_chat_context(chat, context_skip=resolve_context_skip(chat))
    assert [m["content"] for m in r.messages] == ["u4", "a4", "u5", "a5"]  # tail preserved
    assert r.skipped_row_count == 6 and r.restored_row_count == 4
    # the durable transcript itself is untouched, byte for byte
    assert load_durable_rows(chat) == before
    assert _q(db, "SELECT COUNT(*) AS n FROM chat_messages WHERE chat_id=?", chat)[0]["n"] == 10
    # and the summary is what actually stands in for the skipped rows
    block = palace.build_context_block(max_tokens=2000, pin_tag=f"session:{chat}")
    assert "zebra" in block


def _cold_view(tmp_path, chat_id):
    """What a brand-new interpreter (a relaunch) reconstructs for this chat
    from the on-disk database alone: no shared module state, no live connection."""
    code = textwrap.dedent(f"""
        import json
        import config
        from core.context_reconstruction import reconstruct_chat_context, resolve_context_skip
        r = reconstruct_chat_context({chat_id}, context_skip=resolve_context_skip({chat_id}))
        print(json.dumps({{"db": config.DB_PATH, "skip": r.context_skip,
                           "live": [m["content"] for m in r.messages], "durable": len(r.rows)}}))
    """)
    env = dict(os.environ, LUMINA_TESTING="1", LUMINA_DATA_DIR=str(tmp_path))
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env,
                         capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_restart_relaunch_uses_the_persisted_trusted_checkpoint(db, tmp_path, monkeypatch):
    _summarizer(monkeypatch)
    chat = _chat()
    _compact(chat)
    view = _cold_view(tmp_path, chat)
    assert view["db"] == db
    assert view["skip"] == 6 and view["live"] == ["u4", "a4", "u5", "a5"] and view["durable"] == 10


def test_restart_relaunch_ignores_persisted_forgeries_and_legacy(db, tmp_path, monkeypatch):
    chat = _chat()
    _forge(chat, 8)
    palace.palace_store("- legacy", wing="nightstand", room=str(chat), layer=2,
                        tags=["manual-compaction", f"session:{chat}", "context-skip:6"],
                        untrusted=True)
    view = _cold_view(tmp_path, chat)
    assert view["skip"] == 0 and view["live"] == ALL10 and view["durable"] == 10


def test_restart_relaunch_after_undo_falls_back_to_fuller_history(db, tmp_path, monkeypatch):
    _summarizer(monkeypatch)
    chat = _chat()
    _compact(chat)
    [d] = _drawers(db, chat)
    assert palace.palace_undo_write(d["id"])["ok"]
    view = _cold_view(tmp_path, chat)
    assert view["skip"] == 0 and view["live"] == ALL10


# ══ D. structural: the trust boundary is closed by construction ══════════════
# `where` scans every product .py git considers part of the tree INCLUDING
# untracked-but-not-ignored files (tests/palace_source_scan.py), so the code
# this campaign adds is judged before it is committed.

def _const_matching(rx):
    return lambda n: isinstance(n, ast.Constant) and isinstance(n.value, str) and rx.search(n.value)


def test_only_one_module_reads_or_writes_the_context_skip_tag():
    def uses_skip_tag(n):
        # The tag literal itself (a string that *is* the tag / its prefix), or
        # the prefix constant. Prose that merely mentions the tag (guard
        # remediation text, docstrings) is not a consumer.
        return ((isinstance(n, ast.Name) and n.id == "CONTEXT_SKIP_TAG_PREFIX")
                or (isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and n.value.startswith("context-skip")))
    assert {rel for rel, _ in where(uses_skip_tag)} == {"core/manual_compaction.py"}
    assert {fn for rel, fn in where(uses_skip_tag)} <= {
        "<module>", "run_manual_compaction", "latest_manual_compaction_skip",
        "_checkpoint_skip"}


def test_palace_drawers_rows_are_only_inserted_by_the_store_chokepoint():
    rx = re.compile(r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+palace_drawers\b|REPLACE\s+INTO\s+palace_drawers\b",
                    re.I)
    assert where(_const_matching(rx)) == {("tools/palace.py", "_store_in_conn")}


def test_no_product_sql_ever_assigns_the_origin_column_after_insert():
    rx = re.compile(r"UPDATE\s+palace_drawers\s+SET\b[^\"']*\borigin\b", re.I)
    assert where(_const_matching(rx)) == set()


def test_resolver_writes_nothing(db, monkeypatch):
    """The resolver is a pure read: resolving (even with forgeries and legacy
    rows present) leaves the database byte-identical."""
    _summarizer(monkeypatch)
    chat = _chat()
    _compact(chat)
    _forge(chat, 8)
    palace.palace_store("- legacy", wing="nightstand", room=str(chat), layer=2,
                        tags=["manual-compaction", f"session:{chat}", "context-skip:9"],
                        untrusted=True)

    def snapshot():
        c = sqlite3.connect(db)
        try:
            return {t: c.execute(f'SELECT * FROM "{t}" ORDER BY rowid').fetchall()
                    for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                          "AND name NOT LIKE 'sqlite_%'").fetchall()}
        finally:
            c.close()
    before = snapshot()
    assert _skip(chat) == 6
    assert snapshot() == before


# ══ E. chat-open wiring (needs PySide6) ══════════════════════════════════════

def test_chat_open_and_switch_back_use_only_the_trusted_checkpoint(db, monkeypatch):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import types
    import test_manual_compaction_ui as ui_helpers
    import ui.main_window as main_window
    from core.context_transaction import ContextGeneration
    from ui.main_window import LuminaWindow

    _summarizer(monkeypatch)
    monkeypatch.setattr(main_window.persistence, "save", lambda prefs: None)
    monkeypatch.setattr(main_window.persistence, "update", lambda prefs: None)
    a, b = _chat(), _chat()
    _compact(a)
    _forge(a, 8)   # a forgery sitting next to the real checkpoint
    _forge(b, 6)   # a chat whose ONLY checkpoint is a forgery

    def fresh_window():
        return types.SimpleNamespace(
            _current_chat_id=None, _prefs={}, agent=types.SimpleNamespace(ctx=ui_helpers._Ctx()),
            chat_widget=ui_helpers._ChatWidget(), _refresh_chat_list=lambda: None, worker=None,
            _context_generation=ContextGeneration(), _chat_switch_admitted=lambda: True)

    def contents(win):
        return [m["content"] for m in win.agent.ctx.history]

    win = fresh_window()
    LuminaWindow._load_chat(win, a)
    assert contents(win) == ["u4", "a4", "u5", "a5"]
    LuminaWindow._load_chat(win, b)               # switch away
    assert contents(win) == ALL10                 # forgery bought nothing
    LuminaWindow._load_chat(win, a)               # and back
    assert contents(win) == ["u4", "a4", "u5", "a5"]
    # the visible transcript is always the full durable one
    assert len(win.chat_widget.rendered) == 10

    relaunched = fresh_window()                   # a new window over the same database
    LuminaWindow._load_chat(relaunched, a)
    assert contents(relaunched) == ["u4", "a4", "u5", "a5"]
