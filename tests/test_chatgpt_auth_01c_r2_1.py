"""R2.1: serialized credential documents inside JSON string values."""
import json
import zipfile
from pathlib import Path

import pytest

from core import agent_backup as ab
from core.backup_credential_recognizer import MAX_DOCUMENT_BYTES, is_session_document
from test_chatgpt_auth_01c_boundaries import _agent_backup, _backup_env
from test_chatgpt_auth_01c_r2 import DOC, _memory


HOST = {"schema": "lumina.chatgpt.host/99", "host_id": "SYNTH-HOST-R2-1"}


@pytest.mark.parametrize("document, wrapper", [
    (DOC, lambda value: {"wrapper": value}),
    (HOST, lambda value: {"wrapper": value}),
    (DOC, lambda value: {"outer": {"inner": value}}),
    (DOC, lambda value: ["ordinary", value]),
    ({"schema": "lumina.chatgpt.sessions/123", "profiles": []},
     lambda value: {"wrapper": value}),
    (DOC, lambda value: {"wrapper": json.dumps(value)}),
], ids=["session", "host", "nested", "array", "future-version", "reserialized-twice"])
def test_serialized_credential_strings_excluded_from_both_backups(
    tmp_path, monkeypatch, document, wrapper,
):
    body = json.dumps(wrapper(json.dumps(document))).encode()
    assert is_session_document(body)
    exclusions, archived = _memory(tmp_path, monkeypatch, body)
    assert archived is None and exclusions.paths == ("copy.txt",)
    data_dir, base_dir = _backup_env(tmp_path)
    (Path(base_dir) / "skills" / "copy.md").write_bytes(body)
    with pytest.raises(ab.AgentBackupError, match="ChatGPT sign-in session"):
        _agent_backup(data_dir, base_dir, str(tmp_path / "agent.zip"))


def test_escaped_key_and_value_inside_serialized_document(tmp_path, monkeypatch):
    inner = '{"\\u0073chema":"lumina.chatgpt.\\u0073essions/99"}'
    body = json.dumps({"wrapper": inner}).encode()
    assert is_session_document(body)
    exclusions, archived = _memory(tmp_path, monkeypatch, body)
    assert archived is None and exclusions.paths == ("copy.txt",)
    data_dir, base_dir = _backup_env(tmp_path)
    (Path(base_dir) / "skills" / "copy.md").write_bytes(body)
    with pytest.raises(ab.AgentBackupError, match="ChatGPT sign-in session"):
        _agent_backup(data_dir, base_dir, str(tmp_path / "agent.zip"))


def test_large_file_with_late_serialized_document(tmp_path, monkeypatch):
    body = (b'{"filler":"' + b"x" * (MAX_DOCUMENT_BYTES + 16)
            + b'","wrapper":' + json.dumps(json.dumps(DOC)).encode() + b"}")
    assert is_session_document(body)
    exclusions, archived = _memory(tmp_path, monkeypatch, body)
    assert archived is None and exclusions.paths == ("copy.txt",)
    data_dir, base_dir = _backup_env(tmp_path)
    (Path(base_dir) / "skills" / "copy.md").write_bytes(body)
    with pytest.raises(ab.AgentBackupError, match="ChatGPT sign-in session"):
        _agent_backup(data_dir, base_dir, str(tmp_path / "agent.zip"))


@pytest.mark.parametrize("body", [
    json.dumps({"note": json.dumps({"schema": "ordinary.document/1"})}).encode(),
    json.dumps({"note": "ChatGPT and schema are topics in this discussion."}).encode(),
    json.dumps({"note": "Example: {schema: ordinary}"}).encode(),
    json.dumps({"note": '{"schema":"ordinary.document/1"}'}).encode(),
    (b"SQLite format 3\x00" + b"\x00" * MAX_DOCUMENT_BYTES
     + json.dumps({"note": json.dumps({"schema": "lumina.chatgpt.sessions/99",
                                       "example": "schema-only, no credentials"})}).encode()
     + b"\x00" * 8192),
], ids=["stringified-noncredential", "discussion", "json-looking-prose",
        "ordinary-schema", "binary-database-example"])
def test_benign_serialized_strings_archived_by_both_backups(tmp_path, monkeypatch, body):
    assert not is_session_document(body)
    exclusions, archived = _memory(tmp_path, monkeypatch, body)
    assert archived == body and exclusions.count == 0
    data_dir, base_dir = _backup_env(tmp_path)
    (Path(data_dir) / "memory" / "tool_audit.log").write_bytes(body)
    dest = tmp_path / "agent.zip"
    _agent_backup(data_dir, base_dir, str(dest))
    with zipfile.ZipFile(dest) as zf:
        assert any(zf.read(name) == body for name in zf.namelist())


def test_parseable_deep_benign_json_remains_archivable():
    document = {}
    for _ in range(300):
        document = {"outer": document}
    assert not is_session_document(json.dumps(document).encode())
