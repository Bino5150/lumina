"""Verified export: synthetic-only contract and failure-first workflow tests."""
import builtins
import hashlib
import io
import json
import os
import sqlite3
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import core.agent_backup as ab
from test_agent_backup import _make_env, NO_IDENTITY_TRAILERS, NO_CREDENTIALS


def api():
    from core import agent_backup_export
    return agent_backup_export


@pytest.fixture
def source(tmp_path):
    data, base = map(Path, _make_env(tmp_path))
    (data / "knowledge_packs_installed.json").write_text(json.dumps({
        "lumina-self-knowledge": {"version": 1, "installed_at": "2026-10-20"}}))
    prefs = {"human_bio": "SYNTHETIC HUMAN", "human_profile_curated": "SYNTHETIC PROFILE",
             "last_persona": "/old/personas/lumina.json", "pin_hash": "PIN-SENTINEL",
             "pin_salt": "SALT-SENTINEL", "openai_api_key": "KEY-SENTINEL",
             "telegram_remote_access": True, "telegram_remote_access_on_since": 123,
             "telegram_owner_chat_id": "OWNER-SENTINEL", "disabled_tools": ["danger"]}
    (data / "memory/prefs.json").write_text(json.dumps(prefs))
    with sqlite3.connect(data / "memory/lumina.db") as conn:
        conn.execute("CREATE TABLE palace_wings(id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO palace_wings VALUES(1, 'synthetic')")
        conn.execute("INSERT INTO skills VALUES(1, 'Duck', 'synthetic', ?, 't', 't')",
                     (str(base / "skills/rubber-duck-debugging.md"),))
    return data, base, tmp_path / "verified.zip"


def build_review(source, **kwargs):
    data, base, dest = source
    kwargs.setdefault("identity_trailers_path", NO_IDENTITY_TRAILERS)
    kwargs.setdefault("credentials_path", NO_CREDENTIALS)
    return api().build_review_export_with_receipt(str(data), str(base), str(dest), **kwargs)


def read_zip(path):
    with zipfile.ZipFile(path) as z:
        return {n: z.read(n) for n in z.namelist()}


def test_v2_actual_builder_verifier_receipt_and_semantic_coverage(source):
    manifest, receipt = build_review(source)
    payloads = read_zip(source[2])
    assert manifest["format_version"] == api().VERSION
    verdict = api().verify_export(io.BytesIO(source[2].read_bytes()))
    assert verdict["valid"], verdict["errors"]
    assert receipt["verification"] == "PASS"
    assert receipt["archive_sha256"] == hashlib.sha256(source[2].read_bytes()).hexdigest()
    for family in ["overlay/skills/", "overlay/personas/", "overlay/avatars/", "overlay/voices/",
                   "overlay/projects/", "state/databases/", "state/telemetry/"]:
        assert any(n.startswith(family) for n in payloads)
    assert "metadata/knowledge_pack_installation.json" in payloads
    assert not any("worktrees/" in n or n.endswith("ledger.db") for n in payloads)
    # An integrity PASS does not manufacture missing safety coverage or a Restore acceptance.
    assert receipt["full_agent_coverage"] is False
    assert receipt["restorable_verified"] is False
    assert "suspension" in receipt["coverage_blockers"]


def test_v2_prefs_exclude_credentials_pin_owner_and_remote_authority(source):
    manifest, receipt = build_review(source)
    payloads = read_zip(source[2])
    prefs = json.loads(payloads["state/preferences/prefs.json"])
    assert prefs["human_bio"] == "SYNTHETIC HUMAN"
    assert prefs["human_profile_curated"] == "SYNTHETIC PROFILE"
    assert prefs["disabled_tools"] == ["danger"]
    assert prefs["telegram_remote_access"] is False
    all_bytes = b"".join(payloads.values())
    for sentinel in [b"PIN-SENTINEL", b"SALT-SENTINEL", b"KEY-SENTINEL", b"OWNER-SENTINEL"]:
        assert sentinel not in all_bytes
    assert manifest["recovery_contract"]["authority"] == "historical_inactive"
    assert manifest["recovery_contract"]["prefs"] == "preserve_destination_security_rebind_locally"


def test_old_reader_refuses_v2_and_new_reader_classifies_v1_as_older_coverage(source):
    data, base, dest = source
    v1 = dest.with_name("old-v1.zip")
    ab.build_agent_backup(str(data), str(base), str(v1),
                          identity_trailers_path=NO_IDENTITY_TRAILERS, credentials_path=NO_CREDENTIALS)
    classification = api().classify_input(str(v1))
    assert classification["kind"] == "agent_backup_v1"
    assert classification["full_agent_coverage"] is False
    assert "installation_marker" in classification["coverage_blockers"]
    build_review(source)
    assert not ab.verify_agent_backup(str(dest))["valid"]


@pytest.mark.parametrize("name,content", [
    ("suspension.db", b"CORRUPT"), ("soul_identity.json", b'{"version":999}'),
    ("suspension.db-wal", b"UNKNOWN"), ("suspension_evidence", None)])
def test_safety_trace_blocks_production_export_without_creating_destination(source, name, content):
    data, base, dest = source
    p = data / "memory" / name
    p.mkdir() if content is None else p.write_bytes(content)
    with pytest.raises(api().ExportUnavailable, match="suspension"):
        api().export_verified_agent_backup(str(data), str(base), str(dest))
    assert not dest.exists()
    assert (data / "memory" / name).exists()


def test_missing_safety_is_unknown_and_production_export_writes_nothing(source):
    data, base, dest = source
    with pytest.raises(api().ExportUnavailable, match="suspension"):
        api().export_verified_agent_backup(str(data), str(base), str(dest))
    assert not dest.exists()


@pytest.mark.parametrize("value", [b"not-json", b'{"bad": NaN}', b'[]'])
def test_corrupt_installation_marker_fails_before_publication(source, value):
    (source[0] / "knowledge_packs_installed.json").write_bytes(value)
    with pytest.raises(ab.AgentBackupError):
        build_review(source)
    assert not source[2].exists()


def test_missing_marker_is_explicit_unknown_not_fabricated_first_install(source):
    (source[0] / "knowledge_packs_installed.json").unlink()
    manifest, receipt = build_review(source)
    marker = json.loads(read_zip(source[2])["metadata/knowledge_pack_installation.json"])
    assert marker["source_status"] == "missing_unknown"
    assert marker["records"] is None
    assert "installation_marker" in receipt["coverage_blockers"]


def test_marker_is_captured_once_and_verifier_rejects_omission(source, monkeypatch):
    build_review(source)
    payloads = read_zip(source[2])
    payloads.pop("metadata/knowledge_pack_installation.json")
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in payloads.items():
            z.writestr(ab._make_v1_zipinfo(name), data)
    assert not api().verify_export(io.BytesIO(raw.getvalue()))["valid"]


def test_credentials_store_is_never_opened(source, monkeypatch):
    secret = source[2].with_name("credentials.json")
    secret.write_text("NEVER-READ-CREDENTIALS")
    original = os.open
    def guarded(path, *args, **kwargs):
        if os.fspath(path) == str(secret):
            pytest.fail("credential store read")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, "open", guarded)
    build_review(source, credentials_path=str(secret))


def test_bad_existing_destination_is_preserved(source):
    source[2].write_bytes(b"OWNER-EXISTING-OUTPUT")
    with pytest.raises(ab.AgentBackupError):
        build_review(source)
    assert source[2].read_bytes() == b"OWNER-EXISTING-OUTPUT"


def test_damaged_required_db_preserves_prior_verified_output(source):
    build_review(source)
    before = source[2].read_bytes()
    (source[0] / "memory/lumina.db").write_bytes(b"BAD SQLITE")
    with pytest.raises(ab.AgentBackupError):
        build_review(source)
    assert source[2].read_bytes() == before


def test_destination_symlink_rejected_without_touching_target(source):
    target = source[2].with_name("owner-target")
    target.write_bytes(b"KEEP")
    source[2].symlink_to(target)
    with pytest.raises(ab.AgentBackupError):
        build_review(source)
    assert target.read_bytes() == b"KEEP"


def test_synthetic_legacy_rejected_before_any_payload_read(source, monkeypatch):
    with zipfile.ZipFile(source[2], "w") as z:
        z.writestr("memory/lumina.db", b"PRIVATE-DO-NOT-READ")
    monkeypatch.setattr(zipfile.ZipFile, "read", lambda *a, **kw: pytest.fail("legacy payload read"))
    assert api().classify_input(str(source[2]))["kind"] == "legacy_memory_backup"
    assert not api().verify_export(str(source[2]))["valid"]


def test_no_arbitrary_prefs_authority_or_secret_can_pass_unknown_key(source):
    p = source[0] / "memory/prefs.json"
    prefs = json.loads(p.read_text());prefs["unknown_future_authority"] = "UNKNOWN-SENTINEL"
    p.write_text(json.dumps(prefs))
    _, receipt = build_review(source)
    assert b"UNKNOWN-SENTINEL" not in b"".join(read_zip(source[2]).values())
    assert "unclassified_preferences" in receipt["coverage_blockers"]


def incomplete(source, **kwargs):
    return api().export_incomplete_state(*map(str, source), owner_acknowledged=True,
        identity_trailers_path=NO_IDENTITY_TRAILERS, credentials_path=NO_CREDENTIALS, **kwargs)


def rewrite(source, mutate):
    payloads = read_zip(source[2])
    manifest = json.loads(payloads['manifest.json'])
    mutate(manifest, payloads)
    payloads['manifest.json'] = json.dumps(manifest).encode()
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, data in payloads.items():
            z.writestr(ab._make_v1_zipinfo(name), data)
    return out.getvalue()


def test_explicit_incomplete_acknowledgement_required_before_collection(source, monkeypatch):
    monkeypatch.setattr(ab, '_collect_members', lambda *a, **k: pytest.fail('unacknowledged capture'))
    with pytest.raises(api().ExportUnavailable, match='acknowledgement'):
        api().export_incomplete_state(*map(str, source))
    assert not source[2].exists()


def test_real_owner_incomplete_entry_bound_receipt_and_archive_contract(source):
    manifest, receipt = incomplete(source)
    confirmed = api().confirm_export_result(str(source[2]), manifest, receipt)
    assert manifest['backup_mode'] == 'incomplete_state_export'
    assert manifest['coverage'] == {k: confirmed[k] for k in manifest['coverage']}
    assert confirmed['archive_integrity']['status'] == 'PASS'
    assert confirmed['semantic_coverage']['status'] == 'incomplete'
    assert confirmed['c1_safety_coverage']['enforcement_compatibility'] == 'unproven'
    assert 'soul identity' in confirmed['c1_safety_coverage']['omitted']
    assert confirmed['recovery_acceptance']['restore_input_accepted'] is False
    assert source[2].stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('name', ['suspension.db', 'soul_identity.json', 'suspension.db-wal', 'suspension_evidence'])
def test_incomplete_excludes_present_protected_state_without_open_or_mutation(source, monkeypatch, name):
    protected = source[0] / 'memory' / name
    protected.write_bytes(b'PROTECTED-NEWER-OR-CORRUPT')
    old_open = os.open
    def guarded(path, *args, **kwargs):
        if os.fspath(path) == str(protected):
            pytest.fail('protected state opened')
        return old_open(path, *args, **kwargs)
    monkeypatch.setattr(os, 'open', guarded)
    manifest, receipt = incomplete(source)
    assert manifest['c1_exclusion']['status'] == 'present_excluded'
    assert receipt['full_agent_coverage'] is False
    assert b'PROTECTED-NEWER-OR-CORRUPT' not in b''.join(read_zip(source[2]).values())
    assert protected.read_bytes() == b'PROTECTED-NEWER-OR-CORRUPT'
    assert source[2].exists()


def test_no_automatic_overwrite_even_when_previous_archive_is_verified(source):
    incomplete(source)
    before = source[2].read_bytes()
    with pytest.raises(api().ExportUnavailable, match='new destination'):
        incomplete(source)
    assert source[2].read_bytes() == before


def test_target_substitution_at_atomic_publication_preserves_competing_owner_file(source, monkeypatch):
    original = os.link
    def race(src, dst, *args, **kwargs):
        source[2].write_bytes(b'CONCURRENT-OWNER-OUTPUT')
        return original(src, dst, *args, **kwargs)
    monkeypatch.setattr(os, 'link', race)
    with pytest.raises(FileExistsError):
        incomplete(source)
    assert source[2].read_bytes() == b'CONCURRENT-OWNER-OUTPUT'


def test_corruption_after_publication_cannot_receive_receipt(source, monkeypatch):
    original = api().publish_new_export
    def corrupt(*args, **kwargs):
        result = original(*args, **kwargs)
        source[2].write_bytes(b'ATTACKER')
        return result
    monkeypatch.setattr(api(), 'publish_new_export', corrupt)
    manifest, receipt = incomplete(source)
    with pytest.raises(ab.AgentBackupError):
        api().confirm_export_result(str(source[2]), manifest, receipt)
    assert source[2].read_bytes() == b'ATTACKER'  # no unowned deletion


def test_disk_error_preserves_other_good_artifacts(source, monkeypatch):
    good = source[2].with_name('previous-good.zip')
    good.write_bytes(b'PREVIOUS-GOOD')
    original = os.fsync
    def fail(fd):
        raise OSError('synthetic ENOSPC')
    monkeypatch.setattr(os, 'fsync', fail)
    with pytest.raises(OSError):
        incomplete(source)
    assert not source[2].exists()
    assert good.read_bytes() == b'PREVIOUS-GOOD'


def test_changed_source_marker_is_not_silently_omitted(source):
    marker = source[0] / 'knowledge_packs_installed.json'
    external = source[2].with_name('outside-marker')
    external.write_text('{}')
    marker.unlink();marker.symlink_to(external)
    with pytest.raises(ab.AgentBackupError):
        incomplete(source)
    assert not source[2].exists()


@pytest.mark.parametrize('attack', ['full_claim', 'active_authority', 'marker_mismatch', 'major_downgrade', 'prefs_pin'])
def test_incompatible_policy_or_false_claim_cannot_pass_strict_v2(source, attack):
    incomplete(source)
    def mutate(manifest, payloads):
        if attack == 'full_claim':
            manifest['coverage']['recovery_acceptance']['restore_input_accepted'] = True
        elif attack == 'active_authority':
            manifest['recovery_contract']['authority'] = 'active'
        elif attack == 'major_downgrade':
            manifest['format_version']['major'] = 1
        else:
            path = ('metadata/knowledge_pack_installation.json' if attack == 'marker_mismatch'
                    else 'state/preferences/prefs.json')
            body = json.loads(payloads[path])
            if attack == 'marker_mismatch':
                body.update(source_status='missing_unknown', records=None)
            else:
                body['pin_hash'] = 'REINTRODUCED-SECRET'
            raw = json.dumps(body).encode();payloads[path] = raw
            member = next(m for m in manifest['members'] if m['archive_path'] == path)
            member.update(sha256=hashlib.sha256(raw).hexdigest(), size=len(raw))
    verdict = api().verify_export(io.BytesIO(rewrite(source, mutate)))
    assert not verdict['valid'], verdict


def test_forged_count_and_full_claim_rejected_even_with_matching_archive_hash(source):
    manifest, receipt = incomplete(source)
    for update in ({'member_count': 999999}, {'full_agent_coverage': True},
                   {'c1_safety_coverage': {'status': 'PASS'}}):
        with pytest.raises(ab.AgentBackupError):
            api().confirm_export_result(str(source[2]), manifest, {**receipt, **update})


def test_v2_preserves_db_rows_and_corresponding_skill_source(source, tmp_path):
    incomplete(source)
    payloads = read_zip(source[2])
    snapshot = tmp_path / 'isolated-readonly-snapshot.db'
    snapshot.write_bytes(payloads['state/databases/lumina.db'])
    with sqlite3.connect(f'file:{snapshot}?mode=ro', uri=True) as conn:
        assert conn.execute('SELECT name FROM chats').fetchone() == ('Test Chat',)
        assert conn.execute('SELECT name FROM palace_wings').fetchone() == ('synthetic',)
        assert conn.execute('SELECT name FROM skills').fetchone() == ('Duck',)
    assert b'Rubber Duck Debugging' in payloads['overlay/skills/rubber-duck-debugging.md']


def test_payload_resource_limit_blocks_export(source, monkeypatch):
    monkeypatch.setattr(ab, '_determine_aggregate_payload_budget', lambda *a, **k: 1)
    with pytest.raises(ab.AgentBackupError):
        incomplete(source)
    assert not source[2].exists()


@pytest.mark.parametrize('field', ['format_minor', 'marker_required', 'restore_implemented'])
def test_bool_integer_contract_substitution_is_rejected(source, field):
    incomplete(source)
    def mutate(manifest, payloads):
        if field == 'format_minor':
            manifest['format_version']['minor'] = False
        elif field == 'marker_required':
            next(m for m in manifest['members'] if m['logical_id'] == api().MARKER_ID)['required'] = 1
        else:
            manifest['recovery_contract']['restore_implemented'] = 0
    assert not api().verify_export(io.BytesIO(rewrite(source, mutate)))['valid']


def test_protected_trace_appearing_during_capture_blocks_publication(source, monkeypatch):
    original = ab._collect_members
    def race(*args, **kwargs):
        result = original(*args, **kwargs)
        (source[0] / 'memory/soul_identity.json').write_bytes(b'PROTECTED')
        return result
    monkeypatch.setattr(ab, '_collect_members', race)
    with pytest.raises(api().ExportUnavailable, match='BOUNDARY_CHANGED'):
        incomplete(source)
    assert not source[2].exists()


def test_existing_archive_input_capture_is_bounded_before_read(source, monkeypatch):
    incomplete(source)
    monkeypatch.setattr(ab, '_determine_aggregate_payload_budget', lambda *a, **k: 1)
    monkeypatch.setattr(os, 'read', lambda *a, **k: pytest.fail('oversize archive read'))
    assert not api().verify_export(str(source[2]))['valid']


def test_synthetic_test_guard_refuses_live_release_root_before_stat(monkeypatch):
    monkeypatch.setattr(os.path, 'lexists', lambda *a: pytest.fail('live root probed'))
    with pytest.raises(api().ExportUnavailable, match='live release'):
        api().preflight_incomplete(os.path.expanduser('~/.local/share/lumina-release'), '/unused/output')


def test_target_created_after_final_preflight_is_never_overwritten(source, monkeypatch):
    original = api().publish_new_export
    def race(*args, **kwargs):
        source[2].write_bytes(b'CONCURRENT-PREVIOUS-GOOD')
        return original(*args, **kwargs)
    monkeypatch.setattr(api(), 'publish_new_export', race)
    with pytest.raises(FileExistsError):
        incomplete(source)
    assert source[2].read_bytes() == b'CONCURRENT-PREVIOUS-GOOD'


def test_receipt_cannot_claim_a_different_destination_or_format(source):
    manifest, receipt = incomplete(source)
    for change in ({'destination': '/somewhere/else.zip'}, {'format_version': {'major': 1, 'minor': 0}}):
        with pytest.raises(ab.AgentBackupError):
            api().confirm_export_result(str(source[2]), manifest, {**receipt, **change})
