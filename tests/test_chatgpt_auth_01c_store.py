"""
SUBSCRIPTION-PLAN-BACKENDS-01C -- OS-local session custody: protection,
atomicity, crash consistency, corruption handling and canary-leak audits.

Canary secrets come from tests/chatgpt_auth_fakes (every minted token/code
starts with "CANARY-"). After each lifecycle the relevant persistence and
output surfaces are scanned for them.
"""
import json
import os
import sqlite3
import stat

import pytest

from core.chatgpt_auth import oauth
from core.chatgpt_auth import store as store_mod
from core.chatgpt_auth.session import (
    ChatGPTSessionManager, SessionState, SessionStorageError,
    sanitize_telemetry_fields,
)
from core.chatgpt_auth.store import (
    LockDiscipline, SessionStore, StoreCorrupt, StoreUnsafe,
)
from core.redaction import is_secret_key
from chatgpt_auth_fakes import FakeClock, FakeOpenAIAuth, provider_session


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def fake(clock):
    f = FakeOpenAIAuth(clock=clock)
    yield f
    f.close()


def make_manager(directory, fake, clock, recorder=None, **kw):
    store = SessionStore(str(directory), lock_timeout=10)
    provider = oauth.ProviderClient(fake.issuer, session=provider_session(), allow_loopback_http=True)
    events = []
    m = ChatGPTSessionManager(store, provider, clock=clock, open_browser=kw.pop("browser", fake.browser()),
                              recorder=recorder or (lambda e, f: events.append((e, f))), **kw)
    m.events = events
    return m


def sign_in(m, **kw):
    return m.complete_sign_in(m.begin_sign_in(**kw), timeout=10)


def full_lifecycle(m, clock):
    """sign-in -> token -> refresh -> disconnect, plus a failed refresh."""
    sign_in(m)
    m.get_valid_access_token()
    clock.advance(3600)
    m.get_valid_access_token()
    m.disconnect()


def scan_tree_for(root, needles):
    hits = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(dirpath, name)
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            for needle in needles:
                if needle in data:
                    hits.append((path, needle))
    return hits


# ---------------------------------------------------------------------------
# Protection
# ---------------------------------------------------------------------------

def test_store_files_are_owner_only(tmp_path, fake, clock):
    d = tmp_path / "chatgpt"
    m = make_manager(d, fake, clock)
    sign_in(m)
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
    for name in os.listdir(d):
        st = os.stat(d / name)
        assert stat.S_IMODE(st.st_mode) == 0o600, name
        assert st.st_uid == os.getuid()
        assert st.st_nlink == 1
    assert sorted(n for n in os.listdir(d) if not n.startswith(".")) == ["host.json", "sessions.json"]


def test_document_declares_honest_protection_and_schema(tmp_path, fake, clock):
    d = tmp_path / "chatgpt"
    sign_in(make_manager(d, fake, clock))
    raw = json.loads((d / "sessions.json").read_text())
    assert raw["schema"] == "lumina.chatgpt.sessions/1"
    assert raw["protection"] == "owner_only_file"      # not a claim of encryption
    host = json.loads((d / "host.json").read_text())
    assert host["schema"] == "lumina.chatgpt.host/1"
    assert host["ext_agent_host_id"].startswith("urn:uuid:")


def test_loose_directory_is_tightened(tmp_path, fake, clock):
    d = tmp_path / "chatgpt"
    d.mkdir(mode=0o755)
    os.chmod(d, 0o755)
    sign_in(make_manager(d, fake, clock))
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700


def test_loose_file_mode_fails_closed_and_is_not_silently_fixed(tmp_path, fake, clock):
    d = tmp_path / "chatgpt"
    m = make_manager(d, fake, clock)
    sign_in(m)
    os.chmod(d / "sessions.json", 0o644)
    status = m.get_session_state()
    assert status.state is SessionState.CORRUPT_SESSION
    with pytest.raises(SessionStorageError):
        m.get_valid_access_token()
    assert stat.S_IMODE(os.stat(d / "sessions.json").st_mode) == 0o644


def test_wrong_owner_fails_closed(tmp_path, fake, clock, monkeypatch):
    d = tmp_path / "chatgpt"
    m = make_manager(d, fake, clock)
    sign_in(m)
    real = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real + 1)
    with pytest.raises(StoreUnsafe):
        with m.store.locked():
            pass


def test_symlinked_session_file_is_refused(tmp_path, fake, clock):
    d = tmp_path / "chatgpt"
    m = make_manager(d, fake, clock)
    sign_in(m)
    target = tmp_path / "elsewhere.json"
    os.replace(d / "sessions.json", target)
    os.symlink(target, d / "sessions.json")
    assert m.get_session_state().state is SessionState.CORRUPT_SESSION


def test_symlinked_store_directory_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "chatgpt"
    os.symlink(real, link)
    store = SessionStore(str(link))
    with pytest.raises(StoreUnsafe):
        with store.locked():
            pass


def test_hardlinked_session_file_is_refused_for_read_and_write(tmp_path, fake, clock):
    d = tmp_path / "chatgpt"
    m = make_manager(d, fake, clock)
    sign_in(m)
    os.link(d / "sessions.json", tmp_path / "alias.json")
    assert m.get_session_state().state is SessionState.CORRUPT_SESSION
    with pytest.raises(StoreUnsafe):
        with m.store.locked():
            m.store._write_private_json(m.store.sessions_path, {"x": 1})


# ---------------------------------------------------------------------------
# Atomicity / crash consistency / corruption
# ---------------------------------------------------------------------------

def test_failed_replace_leaves_previous_document_intact(tmp_path, fake, clock, monkeypatch):
    d = tmp_path / "chatgpt"
    m = make_manager(d, fake, clock)
    sign_in(m)
    before = (d / "sessions.json").read_bytes()

    def boom(src, dst):
        raise OSError("simulated crash at replace")
    monkeypatch.setattr(store_mod.os, "replace", boom)
    clock.advance(3600)
    with pytest.raises(Exception):
        m.get_valid_access_token()
    monkeypatch.undo()
    assert (d / "sessions.json").read_bytes() == before
    assert [n for n in os.listdir(d) if n.startswith(".tmp-")] == []


def test_crash_leftover_temp_files_are_removed_on_next_write(tmp_path, fake, clock):
    d = tmp_path / "chatgpt"
    m = make_manager(d, fake, clock)
    sign_in(m)
    leftover = d / ".tmp-deadbeef"
    leftover.write_bytes(b"CANARY-RT-leftover")
    os.chmod(leftover, 0o600)
    m.select_profile(m.get_active_profile().profile_id)      # any write
    assert not leftover.exists()


def test_restart_recovers_ready_session(tmp_path, fake, clock):
    d = tmp_path / "chatgpt"
    m = make_manager(d, fake, clock)
    sign_in(m)
    grant = m.get_valid_access_token()
    restarted = make_manager(d, fake, clock)
    assert restarted.get_session_state().state is SessionState.READY
    assert restarted.get_valid_access_token().access_token == grant.access_token


@pytest.mark.parametrize("damage", ["garbage", "truncated", "checksum", "schema", "protection", "empty"])
def test_corrupt_store_fails_closed_and_is_preserved(tmp_path, fake, clock, damage):
    d = tmp_path / "chatgpt"
    m = make_manager(d, fake, clock)
    sign_in(m)
    path = d / "sessions.json"
    data = path.read_bytes()
    raw = json.loads(data)
    if damage == "garbage":
        data = b"\x00\xffnot json"
    elif damage == "truncated":
        data = data[: len(data) // 2]
    elif damage == "checksum":
        raw["body"]["profiles"][0]["generation"] += 7
        data = json.dumps(raw).encode()
    elif damage == "schema":
        raw["schema"] = "lumina.chatgpt.sessions/999"
        data = json.dumps(raw).encode()
    elif damage == "protection":
        raw["protection"] = "encrypted_maybe"
        data = json.dumps(raw).encode()
    elif damage == "empty":
        data = b""
    path.write_bytes(data)
    os.chmod(path, 0o600)

    assert m.get_session_state().state is SessionState.CORRUPT_SESSION
    with pytest.raises(SessionStorageError):
        m.get_valid_access_token()
    with pytest.raises(SessionStorageError):
        m.begin_sign_in()                 # never "repairs" by starting over
    with pytest.raises(SessionStorageError):
        m.disconnect()
    assert path.read_bytes() == data      # preserved, never auto-deleted


def test_structural_invariant_no_tokens_in_signed_out_profile(tmp_path, fake, clock):
    d = tmp_path / "chatgpt"
    m = make_manager(d, fake, clock)
    sign_in(m)
    with m.store.locked():
        doc = m.store.read()
        doc.profiles[0].status = store_mod.STATUS_DISCONNECTED     # but credentials still set
        with pytest.raises(StoreCorrupt):
            m.store.write(doc)


def test_store_operations_require_the_lock(tmp_path):
    store = SessionStore(str(tmp_path / "chatgpt"))
    with pytest.raises(LockDiscipline):
        store.read()
    with pytest.raises(LockDiscipline):
        store.get_or_create_host_id()


def test_locks_are_not_reentrant_and_ordered(tmp_path):
    store = SessionStore(str(tmp_path / "chatgpt"), lock_timeout=1)
    with store.locked():
        with pytest.raises(LockDiscipline):
            with store.locked():
                pass
        with pytest.raises(LockDiscipline):
            with store.refresh_locked():
                pass


def test_damaged_host_id_is_reported_never_regenerated(tmp_path, fake, clock):
    d = tmp_path / "chatgpt"
    m = make_manager(d, fake, clock)
    sign_in(m)
    (d / "host.json").write_text(json.dumps({"schema": "lumina.chatgpt.host/1", "ext_agent_host_id": "x"}))
    os.chmod(d / "host.json", 0o600)
    with pytest.raises(SessionStorageError):
        m.begin_sign_in(new_account=True)
    assert json.loads((d / "host.json").read_text())["ext_agent_host_id"] == "x"


def test_two_registrations_cannot_share_a_client_id(tmp_path, fake, clock):
    d = tmp_path / "chatgpt"
    m = make_manager(d, fake, clock)
    sign_in(m)
    m._open_browser = fake.browser(subject="sub-B")
    sign_in(m, new_account=True)
    with m.store.locked():
        doc = m.store.read()
        doc.profiles[1].client_id = doc.profiles[0].client_id
        with pytest.raises(StoreCorrupt):
            m.store.write(doc)


# ---------------------------------------------------------------------------
# Test isolation
# ---------------------------------------------------------------------------

def test_default_store_dir_is_os_local_and_guarded_in_tests(monkeypatch):
    monkeypatch.delenv("LUMINA_CHATGPT_AUTH_DIR", raising=False)
    assert store_mod.default_store_dir() == os.path.expanduser("~/.config/lumina/chatgpt")
    store = SessionStore()
    with pytest.raises(RuntimeError, match="TEST-ISOLATION"):
        with store.locked():
            pass


def test_conftest_isolates_the_store_before_import():
    assert os.environ.get("LUMINA_CHATGPT_AUTH_DIR")
    assert os.environ["LUMINA_CHATGPT_AUTH_DIR"].startswith(os.environ["LUMINA_DATA_DIR"])
    assert store_mod.default_store_dir() == os.environ["LUMINA_CHATGPT_AUTH_DIR"]


def test_store_lives_outside_the_data_dir_by_default(monkeypatch):
    import config
    monkeypatch.delenv("LUMINA_CHATGPT_AUTH_DIR", raising=False)
    default = os.path.realpath(store_mod.default_store_dir())
    data_dir = os.path.realpath(os.path.expanduser("~/.local/share/lumina"))
    assert not default.startswith(data_dir + os.sep)
    assert not default.startswith(os.path.realpath(config.DATA_DIR) + os.sep)


# ---------------------------------------------------------------------------
# Canary audits: logs, prefs, Flight Recorder
# ---------------------------------------------------------------------------

def test_no_token_reaches_stdout_or_stderr(tmp_path, fake, clock, capfd):
    m = make_manager(tmp_path / "chatgpt", fake, clock)
    full_lifecycle(m, clock)
    fake.refresh_faults.append((400, {"error": "invalid_grant", "error_description": "CANARY-RT-body"}))
    sign_in(m, profile_id=m.get_active_profile().profile_id)
    clock.advance(3600)
    with pytest.raises(Exception):
        m.get_valid_access_token()
    out, err = capfd.readouterr()
    assert "CANARY" not in out and "CANARY" not in err
    assert "code=" not in out + err and "state=" not in out + err


def test_no_token_reaches_prefs_or_data_dir(tmp_path, fake, clock, monkeypatch):
    import config
    from core import persistence
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    prefs = data_dir / "prefs.json"
    monkeypatch.setattr(config, "DATA_DIR", str(data_dir), raising=False)
    monkeypatch.setattr(config, "PREFS_PATH", str(prefs), raising=False)
    persistence.save({"llm_backend": "openai"}) if hasattr(persistence, "save") else None
    m = make_manager(tmp_path / "chatgpt", fake, clock)
    full_lifecycle(m, clock)
    assert scan_tree_for(data_dir, [b"CANARY", b"user-sub-A", b"oaiapp_"]) == []
    if prefs.exists():
        doc = json.loads(prefs.read_text())
        assert "chatgpt" not in json.dumps(doc).lower()


def test_flight_recorder_receives_only_allowlisted_categorical_fields(tmp_path, fake, clock):
    from core.flight_recorder import FlightRecorder
    db = tmp_path / "fr.db"
    fr = FlightRecorder(db_path=str(db))

    def record(event, fields):
        fr.record_machine_event(event, fields=fields, backend="openai_chatgpt_plan")
    m = make_manager(tmp_path / "chatgpt", fake, clock, recorder=record)
    full_lifecycle(m, clock)
    # failure paths too
    fake.refresh_faults.append((400, {"error": "refresh_token_reused"}))
    sign_in(m, profile_id=m.get_active_profile().profile_id)
    clock.advance(3600)
    with pytest.raises(Exception):
        m.get_valid_access_token()
    fr.close()

    raw = db.read_bytes()
    for needle in (b"CANARY", b"user-sub-A", b"owner@example.test", b"oaiapp_", b"127.0.0.1",
                   b"urn:uuid:", b"[REDACTED]"):
        assert needle not in raw, needle
    conn = sqlite3.connect(str(db))
    rows = conn.execute("SELECT event_type, backend, fields_json FROM events ORDER BY seq").fetchall()
    conn.close()
    types = [r[0] for r in rows]
    for expected in ("chatgpt.auth.started", "chatgpt.auth.completed", "chatgpt.refresh.started",
                     "chatgpt.refresh.completed", "chatgpt.disconnect.started",
                     "chatgpt.disconnect.completed", "chatgpt.refresh.failed"):
        assert expected in types, expected
    for _t, backend, fields_json in rows:
        assert backend == "openai_chatgpt_plan"
        fields = json.loads(fields_json)
        assert set(fields) <= {"backend_lane", "operation", "mode", "from_state", "to_state",
                               "error_class", "plan_permission", "remote_revocation"}


def test_telemetry_allowlist_drops_free_text_and_survives_redaction():
    clean = sanitize_telemetry_fields({
        "backend_lane": "openai_chatgpt_plan", "operation": "refresh",
        "error_class": "CANARY-RT-abc", "access_token": "CANARY-AT-x", "email": "a@b.c",
        "to_state": "https://auth.example/?id_token_hint=x", "mode": "new_registration",
    })
    assert clean == {"backend_lane": "openai_chatgpt_plan", "operation": "refresh",
                     "mode": "new_registration"}
    from core.chatgpt_auth import session as session_mod
    for key in session_mod._TELEMETRY_KEYS:
        assert not is_secret_key(key), key     # would be stored as [REDACTED]
