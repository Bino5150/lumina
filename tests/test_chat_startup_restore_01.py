"""CHAT-STARTUP-RESTORE-01 -- startup chat selection must restore the
persisted last-active chat, and must gracefully fall back to the most
recently updated real chat -- never the oldest, and never a silently
mismatched restore -- when that persisted id is missing OR stale.

Live-reproduced defect: ui/main_window.py::_restore_session() read
last_chat_id from prefs and, if truthy, passed it straight to _load_chat()
with no check that the id still names an existing chat. Root cause
established live (2026-09-01) via a running, unrelated ~/lumina (dev
build) process sharing this machine's real DATA_DIR/database with
~/lumina-release: last_chat_id is shared, cross-process, unlocked state --
whichever process's chat-selection action fires last wins the shared
value, and a stale value can point at a chat a different session already
deleted or never created. When that happens, the old code's behavior was:
_load_chat(stale_id) reconstructs zero rows (a real chat_id just doesn't
exist), leaving the message pane blank, while _refresh_chat_list()'s combo
-- finding no item matching self._current_chat_id -- falls back to Qt's
own default (index 0, i.e. chats[0], the same "most recent" chat the
existing fallback already intends for the missing-id case) -- a visibly
mismatched restore: the combo shows one chat's name while the pane shows
none of it.

Repair: validate last_chat_id against the actual chat list already
fetched in _restore_session() before treating it as the restore target;
an id that doesn't validate takes the exact same chats[0] (most recently
updated) fallback the missing-id case already used, so there is exactly
one fallback path and it is always "most recent," never "lowest id."

Real offscreen QApplication + real widgets + real (throwaway) prefs.json /
lumina.db, same hermetic pattern as tests/test_prefs_stale_write_01.py and
tests/test_review_panel.py -- read directly before writing this file.
"""

import os
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import core.persistence as persistence
import config as config_module

# ── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def hermetic(tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(config_module, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config_module, "DB_PATH", str(tmp_path / "data" / "memory" / "lumina.db"))
    monkeypatch.setattr(config_module, "LLM_BACKEND", "llamacpp")
    monkeypatch.setattr(config_module, "LLM_BACKEND_URL", "http://127.0.0.1:8080/v1")
    import core.secrets as secrets_module
    monkeypatch.setattr(secrets_module, "SECRETS_PATH", str(tmp_path / "credentials.json"))
    import ui.main_window as main_window_module
    monkeypatch.setattr(main_window_module.browser_manager, "close", lambda: None)
    return tmp_path


def _make_window():
    from core.agent import LuminaAgent
    from ui.main_window import LuminaWindow

    agent = LuminaAgent(owner=True, channel_id="chat-startup-restore-01")
    return LuminaWindow(agent)


def _seed_chats(names_in_order):
    """Create chats via the real tools/memory.py helpers (same DB the
    window under test will open), each strictly later than the last so
    list_chats()'s ORDER BY updated_at DESC is unambiguous. Returns the
    ordered list of created ids."""
    from tools.memory import init_chat_db, create_chat

    init_chat_db()
    ids = []
    for name in names_in_order:
        ids.append(create_chat(name))
        time.sleep(0.01)
    return ids


# ── B1/B4: a valid, non-most-recent persisted id is restored exactly ─────


def test_valid_last_chat_id_is_restored_even_when_not_most_recent(qapp, hermetic):
    ids = _seed_chats(["oldest", "middle", "newest"])
    persistence.set("last_chat_id", ids[1])  # "middle" -- deliberately not chats[0]

    window = _make_window()
    try:
        assert window._current_chat_id == ids[1]
    finally:
        window.close()


# ── B3: an invalid/deleted persisted id falls back to most-recent, and the
# combo's visible selection matches what actually got loaded ─────────────


def test_stale_last_chat_id_falls_back_to_most_recent_not_oldest(qapp, hermetic):
    ids = _seed_chats(["oldest", "middle", "newest"])
    persistence.set("last_chat_id", 999999)  # never existed

    window = _make_window()
    try:
        assert window._current_chat_id == ids[-1]  # "newest" -- chats[0], never "oldest"
        assert window._current_chat_id != ids[0]
        # No mismatch: the combo's selection agrees with what was actually
        # loaded, instead of Qt's unvalidated index-0 default landing on a
        # chat different from self._current_chat_id.
        idx = window.chat_combo.currentIndex()
        assert window.chat_combo.itemData(idx) == window._current_chat_id
    finally:
        window.close()


def test_deleted_chat_id_falls_back_the_same_way_as_never_existed(qapp, hermetic):
    """A chat that legitimately existed once (last_chat_id correctly
    pointed at it) but was since deleted by a different session sharing
    this database must degrade exactly like an id that never existed --
    not crash, not silently restore a blank/mismatched pane."""
    from tools.memory import delete_chat

    ids = _seed_chats(["oldest", "middle", "newest"])
    persistence.set("last_chat_id", ids[1])
    delete_chat(ids[1])

    window = _make_window()
    try:
        assert window._current_chat_id == ids[-1]
        idx = window.chat_combo.currentIndex()
        assert window.chat_combo.itemData(idx) == window._current_chat_id
    finally:
        window.close()


# ── missing id (pre-existing behavior) must stay exactly as it was ───────


def test_missing_last_chat_id_falls_back_to_most_recent(qapp, hermetic):
    ids = _seed_chats(["oldest", "middle", "newest"])
    persistence.set("last_chat_id", None)

    window = _make_window()
    try:
        assert window._current_chat_id == ids[-1]
    finally:
        window.close()


# ── B8: first-run empty DB is unaffected by this repair ──────────────────


def test_empty_database_creates_a_fresh_chat(qapp, hermetic):
    from tools.memory import init_chat_db
    init_chat_db()
    persistence.set("last_chat_id", None)

    window = _make_window()
    try:
        assert window._current_chat_id is not None
    finally:
        window.close()
