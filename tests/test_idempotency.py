"""core/idempotency.py — deterministic request IDs, check/record roundtrip,
now routed through the WAL-enabled db factory (F-08)."""
import pytest


@pytest.fixture
def isolated_ledger(tmp_path, monkeypatch):
    import core.idempotency as idem
    monkeypatch.setattr(idem, "LEDGER_PATH", str(tmp_path / "ledger.db"))
    return idem


def test_make_request_id_deterministic(isolated_ledger):
    id1 = isolated_ledger.make_request_id("send_telegram_message", "hello")
    id2 = isolated_ledger.make_request_id("send_telegram_message", "hello")
    assert id1 == id2


def test_make_request_id_differs_for_different_args(isolated_ledger):
    id1 = isolated_ledger.make_request_id("send_telegram_message", "hello")
    id2 = isolated_ledger.make_request_id("send_telegram_message", "goodbye")
    assert id1 != id2


def test_check_returns_none_when_not_recorded(isolated_ledger):
    request_id = isolated_ledger.make_request_id("op", "args")
    assert isolated_ledger.check(request_id) is None


def test_record_then_check_roundtrip(isolated_ledger):
    request_id = isolated_ledger.make_request_id("op", "args")
    isolated_ledger.record(request_id, "[Sent successfully]")
    assert isolated_ledger.check(request_id) == "[Sent successfully]"


def test_record_is_idempotent_on_retry(isolated_ledger):
    """The whole point of this module: a genuine retry with the same args
    produces the same key and short-circuits instead of double-sending."""
    request_id_1 = isolated_ledger.make_request_id("send_telegram_message", "hi")
    isolated_ledger.record(request_id_1, "[Sent]")

    # simulate a retry of the exact same call
    request_id_2 = isolated_ledger.make_request_id("send_telegram_message", "hi")
    assert request_id_1 == request_id_2
    assert isolated_ledger.check(request_id_2) == "[Sent]"
