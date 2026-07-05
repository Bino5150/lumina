"""tools/telegram_send.py — chat ID resolution must check prefs.json before
falling back to config.py, matching comms/telegram_bridge.py's convention.
This was the dead-feature bug: anyone who set their chat ID via the
Communications tab (not by hand-editing config.py) got silently ignored."""
import pytest
import tools.telegram_send as telegram_send


def test_prefs_value_takes_priority_over_config(monkeypatch):
    monkeypatch.setattr(telegram_send, "load_prefs", lambda: {"telegram_owner_chat_id": "12345"})
    monkeypatch.setattr(telegram_send.config, "TELEGRAM_OWNER_CHAT_ID", "99999")
    assert telegram_send._owner_chat_id() == "12345"


def test_falls_back_to_config_when_prefs_empty(monkeypatch):
    monkeypatch.setattr(telegram_send, "load_prefs", lambda: {})
    monkeypatch.setattr(telegram_send.config, "TELEGRAM_OWNER_CHAT_ID", "99999")
    assert telegram_send._owner_chat_id() == "99999"


def test_returns_none_when_neither_set(monkeypatch):
    monkeypatch.setattr(telegram_send, "load_prefs", lambda: {})
    monkeypatch.setattr(telegram_send.config, "TELEGRAM_OWNER_CHAT_ID", None)
    assert telegram_send._owner_chat_id() is None


def test_send_message_reports_not_configured_when_no_chat_id(monkeypatch):
    monkeypatch.setattr(telegram_send, "load_prefs", lambda: {})
    monkeypatch.setattr(telegram_send.config, "TELEGRAM_OWNER_CHAT_ID", None)
    monkeypatch.setattr(telegram_send, "get_secret", lambda k: "fake-token")
    # idempotency check() would hit a real DB — short-circuit it for this test
    monkeypatch.setattr(telegram_send, "check", lambda rid: None)
    result = telegram_send.send_telegram_message("hello")
    assert "not configured" in result.lower()
