"""SUBSCRIPTION-PLAN-BACKENDS-01C-R1 adversarial custody regressions."""
import json
import os
import zipfile
from pathlib import Path

import pytest

from core.chatgpt_auth import oauth
from core.chatgpt_auth.session import (
    AuthorizationCancelled, IdentityNotVerified, ReauthRequired, SessionState, TemporaryAuthError,
)
from core.memory_backup import build_memory_backup
from core import memory_backup as mb
from core import agent_backup as ab
from chatgpt_auth_fakes import DROP, FakeClock, FakeOpenAIAuth
from test_chatgpt_auth_01c_concurrency import active, doc_of, make_manager, sign_in
from test_chatgpt_auth_01c_boundaries import _backup_env, _agent_backup


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def fake(clock):
    provider = FakeOpenAIAuth(clock=clock)
    yield provider
    provider.close()


def test_failed_first_registration_never_persists_attacker_client(tmp_path, fake, clock):
    browser = fake.browser(subject="attacker-sub", mutate_callback=lambda cb: {**cb, "code": "junk"})
    m = make_manager(tmp_path / "chatgpt", fake, clock, browser=browser)
    with pytest.raises(AuthorizationCancelled):
        sign_in(m)
    assert doc_of(m).pending_registrations == []
    restarted = make_manager(tmp_path / "chatgpt", fake, clock)
    seen = []
    restarted._open_browser = fake.browser(record=seen)
    sign_in(restarted)
    assert seen[0]["client_id"] == oauth.BOOTSTRAP_CLIENT_ID


def test_first_hit_verified_attacker_requires_owner_confirmation(tmp_path, fake, clock):
    m = make_manager(tmp_path / "chatgpt", fake, clock,
                     browser=fake.browser(subject="attacker-sub", email="attacker@example.test"))
    pending = m.begin_sign_in()
    status = m.complete_sign_in(pending, timeout=10)
    assert status.state is not SessionState.READY
    assert doc_of(m).profiles == []
    m.cancel_sign_in()
    assert doc_of(m).pending_registrations == []


def test_second_start_hit_after_exchange_prevents_publication(tmp_path, fake, clock):
    import requests
    m = make_manager(tmp_path / "chatgpt", fake, clock)
    pending = m.begin_sign_in()
    assert m.complete_sign_in(pending, timeout=10).state is SessionState.AUTHORIZING
    s = requests.Session()
    s.trust_env = False
    assert s.get(fake.launched_urls[0], timeout=5).status_code == 404
    with pytest.raises(IdentityNotVerified):
        m.confirm_sign_in(pending)
    assert doc_of(m).profiles == [] and doc_of(m).pending_registrations == []


def test_second_start_hit_after_owner_commit_cannot_change_account(tmp_path, fake, clock):
    import requests
    m = make_manager(tmp_path / "chatgpt", fake, clock)
    pending = m.begin_sign_in()
    assert m.complete_sign_in(pending, timeout=10).state is SessionState.AUTHORIZING
    assert m.confirm_sign_in(pending).state is SessionState.READY
    with pytest.raises(requests.ConnectionError):
        requests.get(fake.launched_urls[0], timeout=1)
    assert active(m).subject == "user-sub-A" and active(m).client_id == "oaiapp_test1"


def test_url_only_first_hit_cannot_start_registration(tmp_path, fake, clock):
    launched = []
    m = make_manager(tmp_path / "chatgpt", fake, clock,
                     browser=lambda url: launched.append(url) or True)
    pending = m.begin_sign_in()
    assert pending.start_code not in launched[0] and pending.start_code not in repr(pending)
    assert all(pending.start_code.encode() not in p.read_bytes()
               for p in (tmp_path / "chatgpt").iterdir() if p.is_file())
    import requests
    s = requests.Session()
    s.trust_env = False
    first = s.get(launched[0], allow_redirects=False, timeout=5)
    assert first.status_code == 200 and "Location" not in first.headers
    assert fake.issuer not in first.text
    for _ in range(3):
        assert s.post(launched[0], data={"code": "WRONG"}, timeout=5).status_code == 403
    assert s.post(launched[0], data={"code": pending.start_code},
                  allow_redirects=False, timeout=5).status_code == 404
    assert m._attempt.listener.compromised
    m.cancel_sign_in()
    assert doc_of(m).profiles == [] and doc_of(m).pending_registrations == []


@pytest.mark.parametrize("failure", [500, 502, 503, 504, "timeout", "reset"])
def test_lost_refresh_response_never_reuses_old_token(tmp_path, fake, clock, failure):
    m = make_manager(tmp_path / "chatgpt", fake, clock)
    sign_in(m)
    clock.advance(3600)
    original = m.provider.refresh

    def lost(**kw):
        original(**kw)
        raise oauth.ProviderUnavailable(str(failure), failure if isinstance(failure, int) else None)

    m.provider.refresh = lost
    with pytest.raises(TemporaryAuthError):
        m.get_valid_access_token()
    count = fake.refresh_count()
    assert m.get_session_state().state is not SessionState.READY
    with pytest.raises(Exception):
        m.get_valid_access_token()
    restarted = make_manager(tmp_path / "chatgpt", fake, clock)
    with pytest.raises(Exception):
        restarted.get_valid_access_token()
    assert fake.refresh_count() == count
    assert active(m).refresh_outcome_unknown


def test_unknown_refresh_disconnect_and_reconnect_are_truthful(tmp_path, fake, clock):
    m = make_manager(tmp_path / "chatgpt", fake, clock)
    sign_in(m)
    pid = active(m).profile_id
    clock.advance(3600)
    original = m.provider.refresh

    def lost(**kw):
        original(**kw)
        raise oauth.ProviderUnavailable("http_504", 504)

    m.provider.refresh = lost
    with pytest.raises(TemporaryAuthError):
        m.get_valid_access_token()
    assert m.disconnect().remote_revocation_confirmed is False
    m.provider.refresh = original
    assert sign_in(m, profile_id=pid).state is SessionState.READY
    assert not active(m).refresh_outcome_unknown
    assert fake.refresh_count() == 1


def test_identity_without_offline_access_is_connected(tmp_path, fake, clock):
    fake.code_scope_override = "openid profile email"
    fake.code_response_overrides["refresh_token"] = DROP
    m = make_manager(tmp_path / "chatgpt", fake, clock)
    status = sign_in(m)
    assert status.state is SessionState.CONNECTED_NO_PLAN_PERMISSION
    assert doc_of(m).profiles[0].subject == "user-sub-A"
    assert doc_of(m).profiles[0].credentials.refresh_token is None
    with pytest.raises(Exception):
        m.get_valid_access_token()
    pid = active(m).profile_id
    outcome = m.disconnect()
    assert outcome.state is SessionState.DISCONNECTED
    assert outcome.remote_revocation_confirmed is None
    fake.code_scope_override = None
    fake.code_response_overrides.clear()
    assert sign_in(m, profile_id=pid).state is SessionState.READY


@pytest.mark.parametrize("body", [
    b'{"\\u0073chema":"lumina.chatgpt.sessions/1","refresh_token":"CANARY-RT"}',
    b'{"schema":"lumina.chatgpt.\\u0073essions/1","refresh_token":"CANARY-RT"}',
], ids=["escaped-key", "escaped-value"])
def test_semantic_backup_exclusion(tmp_path, body):
    source = tmp_path / "renamed.json"
    source.write_bytes(body)
    assert mb.is_session_document(source.read_bytes())
    assert ab._contains_chatgpt_session_document(body)


@pytest.mark.parametrize("body", [
    b'{"\\u0073chema":"lumina.chatgpt.sessions/1","refresh_token":"CANARY-RT"}',
    b'{"schema":"lumina.chatgpt.\\u0068ost/1","ext_agent_host_id":"CANARY"}',
    b'{"schema":"lumina.chatgpt.sessions/1",',
    json.dumps({"schema": "lumina.chatgpt.sessions/1", "refresh_token": "CANARY-RT"}).encode("utf-16")[:-2],
    json.dumps({"schema": "lumina.chatgpt.sessions/1", "refresh_token": "CANARY-RT"}).encode("utf-16-be")[:-1],
], ids=["escaped-session", "escaped-host", "malformed-session", "malformed-utf16", "malformed-utf16be"])
def test_escaped_or_malformed_documents_never_enter_archives(tmp_path, monkeypatch, body):
    class Db:
        def execute(self, *_):
            pass

        def close(self):
            pass

    monkeypatch.setattr(mb, "_db", lambda: Db())
    data = tmp_path / "data"
    data.mkdir()
    (data / "secret.json").write_bytes(body)
    mem = tmp_path / "memory.zip"
    build_memory_backup(str(data), str(mem))
    with zipfile.ZipFile(mem) as zf:
        assert "secret.json" not in zf.namelist()
    data_dir, base_dir = _backup_env(tmp_path)
    (Path(base_dir) / "skills" / "secret.md").write_bytes(body)
    with pytest.raises(ab.AgentBackupError):
        _agent_backup(data_dir, base_dir, str(tmp_path / "agent.zip"))


def test_document_size_boundary_is_fail_closed(tmp_path):
    body = b"{" + b" " * (1024 * 1024) + b'"schema":"lumina.chatgpt.sessions/1"}'
    assert mb.is_session_document(body[:1024 * 1024 + 1])
    assert ab._contains_chatgpt_session_document(body)


@pytest.mark.parametrize("replacement_kind", ["rename", "symlink"])
def test_memory_backup_archives_only_classified_bytes(tmp_path, monkeypatch, replacement_kind):
    class Db:
        def execute(self, *_):
            pass

        def close(self):
            pass

    monkeypatch.setattr(mb, "_db", lambda: Db())
    data = tmp_path / "data"
    data.mkdir()
    source = data / "note.txt"
    source.write_bytes(b"safe before replacement")
    original = mb.is_session_document

    def swap(snapshot):
        result = original(snapshot)
        replacement = data / "replacement"
        replacement.write_bytes(b'{"schema":"lumina.chatgpt.sessions/1","refresh_token":"CANARY-RT"}')
        if replacement_kind == "rename":
            os.replace(replacement, source)
        else:
            source.unlink()
            source.symlink_to(replacement)
        return result

    monkeypatch.setattr(mb, "is_session_document", swap)
    archive = tmp_path / "backup.zip"
    build_memory_backup(str(data), str(archive))
    with zipfile.ZipFile(archive) as zf:
        assert zf.read("note.txt") == b"safe before replacement"
