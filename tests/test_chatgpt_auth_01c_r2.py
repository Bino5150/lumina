"""R2 review regressions: credential copies, pre-send refresh, and real Settings."""
import json
import threading
import zipfile
from pathlib import Path

import pytest
import requests
from urllib3.exceptions import ConnectTimeoutError, MaxRetryError, NameResolutionError, NewConnectionError

from core import agent_backup as ab, memory_backup as mb
from core.backup_credential_recognizer import is_session_document
from core.chatgpt_auth.session import AuthorizationCancelled, SessionState, TemporaryAuthError
from chatgpt_auth_fakes import FakeClock, FakeOpenAIAuth
from test_chatgpt_auth_01c_boundaries import _agent_backup, _backup_env
from test_chatgpt_auth_01c_concurrency import active, make_manager, sign_in


class Db:
    def execute(self, *_):
        pass

    def close(self):
        pass


DOC = {"schema": "lumina.chatgpt.sessions/99", "profiles": [{"refresh_token": "SYNTH-RT-R2"}]}
CANARY = b"SYNTH-RT-R2"


def _memory(tmp_path, monkeypatch, body):
    monkeypatch.setattr(mb, "_db", lambda: Db())
    data = tmp_path / "data"
    data.mkdir()
    (data / "copy.txt").write_bytes(body)
    dest = tmp_path / "memory.zip"
    result = mb.build_memory_backup(str(data), str(dest))
    with zipfile.ZipFile(dest) as zf:
        archived = "copy.txt" in zf.namelist()
        payload = zf.read("copy.txt") if archived else None
    return result, payload


@pytest.mark.parametrize("body", [
    json.dumps(DOC).encode(),
    json.dumps(DOC, indent=2).encode(),
    json.dumps({"x": 1, **DOC}).encode(),
    b'{"\\u0073chema":"lumina.chatgpt.\\u0073essions/99","refresh_token":"SYNTH-RT-R2"}',
    b"\xef\xbb\xbf" + json.dumps(DOC).encode(),
    json.dumps(DOC).encode("utf-16-le"),
    json.dumps(DOC).encode("utf-16-be"),
    json.dumps(DOC).encode("utf-16"),
    ("# note\n```json\n" + json.dumps(DOC) + "\n```").encode(),
    json.dumps([DOC]).encode(),
    json.dumps({"backup": DOC}).encode(),
    ("leading text\n" + json.dumps(DOC)).encode(),
    b"{" + b" " * (1024 * 1024 + 16) + b'"schema":"lumina.chatgpt.sessions/99","refresh_token":"SYNTH-RT-R2"}',
], ids=["canonical", "pretty", "reordered", "escaped", "bom", "utf16le", "utf16be",
        "utf16bom", "markdown", "array", "nested", "leading", "late-large"])
def test_credential_copies_excluded_from_both_backups(tmp_path, monkeypatch, body):
    assert is_session_document(body)
    exclusions, archived = _memory(tmp_path, monkeypatch, body)
    assert archived is None
    assert exclusions.count == 1 and exclusions.paths == ("copy.txt",)
    data_dir, base_dir = _backup_env(tmp_path)
    (Path(base_dir) / "skills" / "copy.md").write_bytes(body)
    with pytest.raises(ab.AgentBackupError, match="ChatGPT sign-in session"):
        _agent_backup(data_dir, base_dir, str(tmp_path / "agent.zip"))


@pytest.mark.parametrize("body", [
    b'{"ts":1,"note":"caf\\u00e9"}\n{"ts":2,"note":"ok"}\n',
    b'{"ts":1,"note":"ChatGPT"}\n{"ts":2,"note":"ok"}\n',
    b'{"ts":1,"note":"schema"}\n{"ts":2,"note":"ok"}\n',
    b'{"schema":"https://json-schema.org/draft-07","note":"ChatGPT"}',
    b'The schema lumina.chatgpt.sessions/1 is documented.',
    json.dumps({"notes": ["x" * 100] * 12000}).encode(),
], ids=["unicode-jsonl", "chatgpt-jsonl", "schema-jsonl", "settings", "prose", "large"])
def test_benign_files_archived_by_both_backups(tmp_path, monkeypatch, body):
    assert not is_session_document(body)
    exclusions, archived = _memory(tmp_path, monkeypatch, body)
    assert archived == body and exclusions.count == 0
    data_dir, base_dir = _backup_env(tmp_path)
    (Path(data_dir) / "memory" / "tool_audit.log").write_bytes(body)
    dest = tmp_path / "agent.zip"
    _agent_backup(data_dir, base_dir, str(dest))
    with zipfile.ZipFile(dest) as zf:
        assert any(zf.read(name) == body for name in zf.namelist())


def test_deep_json_fails_closed_with_visible_omission(tmp_path, monkeypatch):
    body = b"[" * 50000 + b"]" * 50000
    exclusions, archived = _memory(tmp_path, monkeypatch, body)
    assert archived is None and exclusions.paths == ("copy.txt",)
    data_dir, base_dir = _backup_env(tmp_path)
    (Path(base_dir) / "skills" / "deep.md").write_bytes(body)
    with pytest.raises(ab.AgentBackupError, match="ChatGPT sign-in session"):
        _agent_backup(data_dir, base_dir, str(tmp_path / "agent.zip"))


@pytest.mark.parametrize("where", ["discovery", "dns", "refused", "connect_timeout"])
def test_proven_unsent_refresh_can_retry(tmp_path, where):
    clock = FakeClock()
    fake = FakeOpenAIAuth(clock=clock)
    try:
        m = make_manager(tmp_path / "chatgpt", fake, clock)
        sign_in(m)
        clock.advance(3600)
        if where != "discovery":
            m.provider.endpoints()
        else:
            m = make_manager(tmp_path / "chatgpt", fake, clock)
        original = m.provider._http.request

        def fail(method, url, **kw):
            if where == "discovery" and "openid-configuration" not in url:
                return original(method, url, **kw)
            if where != "discovery" and method != "POST":
                return original(method, url, **kw)
            if where == "connect_timeout":
                raise requests.ConnectTimeout("connect timeout")
            if where in ("dns", "discovery"):
                cause = NameResolutionError("host", None, "dns")
            else:
                cause = NewConnectionError(None, "refused")
            raise requests.ConnectionError(MaxRetryError(None, url, reason=cause))

        m.provider._http.request = fail
        before = fake.refresh_count()
        token = active(m).credentials.refresh_token
        with pytest.raises(TemporaryAuthError):
            m.get_valid_access_token()
        assert fake.refresh_count() == before
        assert active(m).credentials.refresh_token == token
        assert m.get_session_state().state is SessionState.READY
        m.provider._http.request = original
        m.get_valid_access_token()
        assert fake.refresh_count() == before + 1
    finally:
        fake.close()


def test_plain_connection_error_remains_ambiguous(tmp_path):
    clock = FakeClock()
    fake = FakeOpenAIAuth(clock=clock)
    try:
        m = make_manager(tmp_path / "chatgpt", fake, clock)
        sign_in(m)
        clock.advance(3600)
        m.provider.endpoints()
        m.provider._http.request = lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("unknown"))
        with pytest.raises(TemporaryAuthError):
            m.get_valid_access_token()
        assert m.get_session_state().state is SessionState.REAUTH_REQUIRED
    finally:
        fake.close()


def test_second_confirmation_refuses_while_first_commits(tmp_path):
    clock = FakeClock()
    fake = FakeOpenAIAuth(clock=clock)
    try:
        m = make_manager(tmp_path / "chatgpt", fake, clock)
        pending = m.begin_sign_in()
        assert m.complete_sign_in(pending, timeout=10).state is SessionState.AUTHORIZING
        entered = threading.Event()
        release = threading.Event()
        original = m._commit_sign_in

        def held(*args, **kwargs):
            entered.set()
            assert release.wait(5)
            return original(*args, **kwargs)

        m._commit_sign_in = held
        worker = threading.Thread(target=lambda: m.confirm_sign_in(pending))
        worker.start()
        assert entered.wait(5)
        with pytest.raises(AuthorizationCancelled):
            m.confirm_sign_in(pending)
        release.set()
        worker.join(5)
        assert not worker.is_alive()
        assert sum(1 for event, _ in m.events if event == "chatgpt.auth.completed") == 1
    finally:
        fake.close()
