"""R2.2: prose-wrapped serialized credential fragments inside string values.

F1' from the R2.1 independent review: a parseable <=1 MiB outer JSON document
may carry a string value like "some prose {\"schema\": ...} more prose". The
bounded walk must scan every string value for the exact schema field,
regardless of whether the whole string parses as JSON. Both real backup
paths must agree: credential copies excluded, benign content archived.
"""
import json
import zipfile
from pathlib import Path

import pytest

from core import agent_backup as ab
from core.backup_credential_recognizer import is_session_document
from test_chatgpt_auth_01c_boundaries import _agent_backup, _backup_env
from test_chatgpt_auth_01c_r2 import DOC, _memory


HOST = {"schema": "lumina.chatgpt.host/99", "host_id": "SYNTH-HOST-R2-2"}


def _prose_wrapped(value):
    return {"note": "see the export below: " + json.dumps(value) + " -- end of note."}


@pytest.mark.parametrize("document, wrapper", [
    (DOC, _prose_wrapped),
    (HOST, _prose_wrapped),
    (DOC, lambda value: {"outer": {"note": "export: " + json.dumps(value)}}),
    (DOC, lambda value: ["ordinary", "export: " + json.dumps(value)]),
    (DOC, lambda value: {"note": json.dumps(value)[1:-1]}),
], ids=["prose-session", "prose-host", "nested-string", "array-string", "json-fragment"])
def test_prose_wrapped_serialized_copies_excluded_from_both_backups(
    tmp_path, monkeypatch, document, wrapper,
):
    body = json.dumps(wrapper(document)).encode()
    assert is_session_document(body)
    exclusions, archived = _memory(tmp_path, monkeypatch, body)
    assert archived is None and exclusions.paths == ("copy.txt",)
    data_dir, base_dir = _backup_env(tmp_path)
    (Path(base_dir) / "skills" / "copy.md").write_bytes(body)
    with pytest.raises(ab.AgentBackupError, match="ChatGPT sign-in session"):
        _agent_backup(data_dir, base_dir, str(tmp_path / "agent.zip"))


@pytest.mark.parametrize("body", [
    json.dumps({"note": "ChatGPT is a topic in this discussion."}).encode(),
    json.dumps({"note": "The schema field is described in the docs."}).encode(),
    json.dumps({"note": json.dumps({"schema": "ordinary.document/1", "items": [1, 2]})}).encode(),
    (b'{"ts":"2026-10-01","tool":"web_search","args":{"query":"chatgpt schema docs"}}\n'
     b'{"ts":"2026-10-01","tool":"palace_search","args":{"query":"host identifier"}}\n'),
], ids=["prose-chatgpt", "prose-schema", "stringified-benign", "audit-log-jsonl"])
def test_benign_string_values_remain_archivable_by_both_backups(tmp_path, monkeypatch, body):
    assert not is_session_document(body)
    exclusions, archived = _memory(tmp_path, monkeypatch, body)
    assert archived == body and exclusions.count == 0
    data_dir, base_dir = _backup_env(tmp_path)
    (Path(data_dir) / "memory" / "tool_audit.log").write_bytes(body)
    dest = tmp_path / "agent.zip"
    _agent_backup(data_dir, base_dir, str(dest))
    with zipfile.ZipFile(dest) as zf:
        assert any(zf.read(name) == body for name in zf.namelist())
