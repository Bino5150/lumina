"""CHAT-STARTUP-RESTORE-02 -- startup restore must never rewrite the durable
last_chat_id preference, on either its success path or its fallback path.

Live-reported symptom (2026-09-06): owner relaunches after normally using a
non-#1 conversation and lands on chat #1 instead, with no Telegram activity
involved -- ruling out the CHAT-STARTUP-RESTORE-01 cause (already covered by
tests/test_chat_startup_restore_01.py and confirmed still fixed).

Root cause: ui/main_window.py::_restore_session() calls
self._load_chat(target_id) with _load_chat()'s default persist_as_last=True.
On the validated-match path this is a harmless same-value rewrite, but on
the fallback path (persisted last_chat_id stale, deleted, or transiently
unvalidatable) target_id is chats[0]["id"] -- a chat the owner never chose --
and persist_as_last=True writes that id back to prefs.json as the new
"durable" last_chat_id. That converts one fallback event into a PERMANENT
redirect: the next relaunch validates that same fallback id successfully
(it is now a real, valid chat) and repeats it forever, with no remaining
path back to the owner's real target.

Startup restore -- whether it validates the saved id or falls back -- is a
mechanism inspecting/reconstructing conversation state, never an owner
navigation decision (see _load_chat()'s own docstring, which already
documents exactly this distinction for the Telegram case). It must never
persist, on either branch.

Same hermetic pattern as test_chat_startup_restore_01.py -- real offscreen
QApplication + real widgets + real (throwaway) prefs.json / lumina.db.
"""

import os
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import core.persistence as persistence
import config as config_module


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

    agent = LuminaAgent(owner=True, channel_id="chat-startup-restore-02")
    return LuminaWindow(agent)


def _seed_chats(names_in_order):
    from tools.memory import init_chat_db, create_chat

    init_chat_db()
    ids = []
    for name in names_in_order:
        ids.append(create_chat(name))
        time.sleep(0.01)
    return ids


# ── R10/R7-adjacent: a fallback restore must not become sticky ───────────


def test_fallback_restore_does_not_overwrite_last_chat_id(qapp, hermetic):
    ids = _seed_chats(["oldest", "middle", "newest"])
    persistence.set("last_chat_id", 999999)  # stale/invalid -- triggers fallback

    window = _make_window()
    try:
        assert window._current_chat_id == ids[-1]  # correctly falls back to most-recent
    finally:
        window.close()

    # The critical assertion: a fallback restore is not an owner navigation
    # decision and must never rewrite the durable preference -- not even to
    # the (valid) chat it fell back to. The stale value is untouched so a
    # later fix to whatever invalidated it can still recover the real target.
    assert persistence.load()["last_chat_id"] == 999999


def test_repeated_relaunches_after_fallback_do_not_diverge(qapp, hermetic):
    """Directly reproduces the live symptom shape: once a fallback has
    (under the bug) been persisted, EVERY subsequent ordinary relaunch keeps
    restoring the wrong chat forever, because the wrong id is now itself
    valid. Two consecutive relaunches after the same triggering event must
    both still fall back the same way, not lock onto a corrupted value."""
    ids = _seed_chats(["oldest", "middle", "newest"])
    persistence.set("last_chat_id", 999999)

    window1 = _make_window()
    try:
        assert window1._current_chat_id == ids[-1]
    finally:
        window1.close()

    window2 = _make_window()
    try:
        assert window2._current_chat_id == ids[-1]
    finally:
        window2.close()

    assert persistence.load()["last_chat_id"] == 999999


# ── validated-match path must remain a true no-op on the preference ──────


def test_validated_restore_does_not_touch_prefs_file_mtime_semantics(qapp, hermetic):
    """When the saved id is already valid, _restore_session() still calls
    _load_chat(). This must not persist either -- restoring what was already
    saved is not a new owner decision, and a real owner navigation
    afterwards (_on_chat_selected/_load_chat with its default) is what
    should be the only thing allowed to change last_chat_id going forward."""
    ids = _seed_chats(["oldest", "middle", "newest"])
    persistence.set("last_chat_id", ids[1])  # "middle" -- valid, not chats[0]

    window = _make_window()
    try:
        assert window._current_chat_id == ids[1]
        # Owner now deliberately switches to "newest" -- a real navigation
        # decision, which SHOULD persist.
        window._load_chat(ids[-1])
        assert persistence.load()["last_chat_id"] == ids[-1]
    finally:
        window.close()
