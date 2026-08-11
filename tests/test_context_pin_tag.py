"""core/context.py — MB-11 Step 4: chat_id threads through
build_messages() -> _build_system_prompt() -> build_context_block()'s
pin_tag param, as f"session:{chat_id}" when chat_id is truthy, None
otherwise. This is the read-side half of session-pinning: the compaction
write-side tags its nightstand closet with the same session:{chat_id} tag
so it always resurfaces on reopen (see tests/test_palace_pinning.py for
the write/query-side behavior itself).
"""
import core.context as context
import core.persistence as persistence
import tools.palace as palace


def _fake_prefs():
    return {"human_bio": "", "human_bio_public": "", "human_profile_curated": ""}


def test_chat_id_threads_into_pin_tag(monkeypatch):
    monkeypatch.setattr(persistence, "load", lambda: _fake_prefs())
    captured = {}
    monkeypatch.setattr(palace, "build_context_block", lambda **kw: captured.update(kw) or "")

    cm = context.ContextManager(owner=True)
    cm.build_messages(chat_id=4242)

    assert captured["pin_tag"] == "session:4242"


def test_chat_id_none_yields_pin_tag_none(monkeypatch):
    monkeypatch.setattr(persistence, "load", lambda: _fake_prefs())
    captured = {}
    monkeypatch.setattr(palace, "build_context_block", lambda **kw: captured.update(kw) or "")

    cm = context.ContextManager(owner=True)
    cm.build_messages()  # chat_id omitted -- default

    assert captured["pin_tag"] is None


def test_chat_id_omitted_matches_explicit_none(monkeypatch):
    """Headless/no-session contract: not passing chat_id must produce
    byte-identical output to passing chat_id=None explicitly."""
    monkeypatch.setattr(persistence, "load", lambda: _fake_prefs())
    monkeypatch.setattr(palace, "build_context_block", lambda **kw: "MEMORY_BLOCK")

    cm1 = context.ContextManager(owner=True)
    cm2 = context.ContextManager(owner=True)

    msgs_default = cm1.build_messages()
    msgs_explicit_none = cm2.build_messages(chat_id=None)

    assert msgs_default == msgs_explicit_none


def test_owner_false_never_calls_build_context_block(monkeypatch):
    """Non-owner sessions skip palace injection entirely (pre-existing
    gate) -- chat_id must not change that."""
    calls = []
    monkeypatch.setattr(palace, "build_context_block", lambda **kw: calls.append(kw) or "")

    cm = context.ContextManager(owner=False)
    cm.build_messages(chat_id=4242)

    assert calls == []
