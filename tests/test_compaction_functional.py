"""MB-11 end-to-end functional test — the pre-commit checklist from the
task block, run for real rather than asserted piecewise:

  (a) a closet lands in nightstand/{chat_id} L2, tagged auto-compaction +
      session:{chat_id} (S57 correction: nightstand, NOT sessions -- same
      wing on_session_idle already uses, so palace_store()'s room-scoped
      rolling merge can never accidentally combine with curated memory)
  (b) closing and reopening that exact chat resurfaces it via pin_tag,
      regardless of what else is in the Palace
  (c) a second compaction in the same session appends to the same rolling
      closet rather than duplicating

Exercises the real production path: ContextManager.build_messages()'s
trim loop actually capturing dropped history, LuminaWindow._maybe_compact()
actually firing, tools.palace.palace_store()/build_context_block() against
a real isolated SQLite DB. Only the outbound LLM call itself is stubbed
(core.dreaming.run_summarization_call's own contract -- prompt shape,
prefill, exception handling -- is already covered by tests/test_dreaming.py;
duplicating a live-backend dependency here would make this test flaky for
no added coverage) and core.persistence.load (human bio, irrelevant to
this feature).
"""
import os
import types

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import config
import core.context as context
import core.dreaming as dreaming
import core.persistence as persistence
import tools.palace as palace
from ui.main_window import LuminaWindow


class ImmediateThread:
    def __init__(self, target=None, daemon=None, **kw):
        self._target = target

    def start(self):
        self._target()


@pytest.fixture
def functional_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "functional.db"))
    palace.init_palace_db()
    monkeypatch.setattr(
        persistence, "load",
        lambda: {"human_bio": "", "human_bio_public": "", "human_profile_curated": ""},
    )
    monkeypatch.setattr(config, "CONTEXT_COMPACTION_ENABLED", True)
    monkeypatch.setattr(config, "CONTEXT_COMPACTION_BATCH_TOKENS", 50)

    import ui.main_window as main_window
    monkeypatch.setattr(main_window, "threading", types.SimpleNamespace(Thread=ImmediateThread))
    return tmp_path


def _run_session_until_trim(chat_id, n_turns=25):
    """A real ContextManager, small enough max_tokens that build_messages()'s
    trim loop actually fires and captures dropped history -- 'a session long
    enough to force a real trim,' not a synthetic pre-seeded buffer."""
    cm = context.ContextManager(owner=True)
    cm.max_tokens = 80
    cm.reserve = 0
    for i in range(n_turns):
        cm.add_user(f"turn {i} user message padding padding padding padding padding")
        cm.add_assistant(f"turn {i} assistant reply padding padding padding padding")
        cm.build_messages(chat_id=chat_id)
    return cm


def _fake_window(cm):
    return types.SimpleNamespace(agent=types.SimpleNamespace(ctx=cm))


def test_full_compaction_and_pin_resurfacing_cycle(functional_env, monkeypatch):
    chat_id = 777

    # ── Force a real trim, drive one compaction cycle ──
    cm = _run_session_until_trim(chat_id)
    assert cm.pending_compaction_tokens() >= config.CONTEXT_COMPACTION_BATCH_TOKENS, \
        "sanity: the session above must actually have forced real trimming"

    monkeypatch.setattr(
        dreaming, "run_summarization_call",
        lambda raw_text, prompt=None, max_tokens=500: "- FIRSTCOMPACTIONMARKER did stuff",
    )
    win = _fake_window(cm)
    LuminaWindow._maybe_compact(win, chat_id=chat_id)

    # ── (a) closet lands in nightstand/{chat_id}/L2, correctly tagged ──
    writes = palace.list_flagged_writes(tag="auto-compaction")
    assert len(writes) == 1
    write = writes[0]
    assert write["wing"] == "nightstand"
    assert write["room"] == str(chat_id)
    tags = write["tags"]
    assert "auto-compaction" in tags and f"session:{chat_id}" in tags

    closets_after_first = _closets_for_room(chat_id)
    assert len(closets_after_first) == 1
    assert closets_after_first[0]["layer"] == 2
    first_closet_id = closets_after_first[0]["id"]

    # ── (b) reopening resurfaces it via pin_tag, regardless of Palace noise ──
    # 10 unrelated, newer-looking L2 closets competing for the same slots.
    for i in range(10):
        palace.palace_store(
            content=f"ZZDISTRACTORZZ{i} unrelated content",
            wing="nightstand", room=f"other-{i}", layer=2,
            tags=["dream-sweep", f"session:{9000 + i}"],
        )

    pin_tag = f"session:{chat_id}"
    block = palace.build_context_block(max_tokens=5000, inject_limit=3, pin_tag=pin_tag)
    assert "FIRSTCOMPACTIONMARKER" in block, \
        "pinned session closet must resurface even though inject_limit=3 would otherwise exclude it"

    # ── (c) a second compaction in the same session appends, not duplicates ──
    cm2 = _run_session_until_trim(chat_id, n_turns=25)
    assert cm2.pending_compaction_tokens() >= config.CONTEXT_COMPACTION_BATCH_TOKENS

    monkeypatch.setattr(
        dreaming, "run_summarization_call",
        lambda raw_text, prompt=None, max_tokens=500: "- SECONDCOMPACTIONMARKER did more stuff",
    )
    win2 = _fake_window(cm2)
    LuminaWindow._maybe_compact(win2, chat_id=chat_id)

    writes_after_second = palace.list_flagged_writes(tag="auto-compaction")
    assert len(writes_after_second) == 2  # two drawers -- two distinct writes recorded

    closets_after_second = _closets_for_room(chat_id)
    assert len(closets_after_second) == 1, "must still be ONE rolling closet, not two"
    assert closets_after_second[0]["id"] == first_closet_id, "same closet, updated in place"
    assert "FIRSTCOMPACTIONMARKER" in closets_after_second[0]["compressed"]
    assert "SECONDCOMPACTIONMARKER" in closets_after_second[0]["compressed"]

    # And the pin still resolves to that one merged closet after the second write.
    block2 = palace.build_context_block(max_tokens=5000, inject_limit=3, pin_tag=pin_tag)
    assert "FIRSTCOMPACTIONMARKER" in block2
    assert "SECONDCOMPACTIONMARKER" in block2


def _closets_for_room(chat_id):
    conn = palace.get_db()
    rows = conn.execute("""
        SELECT c.id, c.layer, c.compressed
        FROM palace_closets c
        JOIN palace_rooms r ON c.room_id = r.id
        JOIN palace_wings w ON r.wing_id = w.id
        WHERE w.name = 'nightstand' AND r.name = ?
    """, (str(chat_id),)).fetchall()
    conn.close()
    return [dict(r) for r in rows]
