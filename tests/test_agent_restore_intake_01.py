"""AGENT-BACKUP-RESTORE-01 slice 1: Restore intake, strict verification and
non-destructive recovery preview. Synthetic disposable fixtures only."""
import ast
import hashlib
import io
import json
import os
import sqlite3
import struct
import zipfile
from pathlib import Path

import pytest

import core.agent_backup as ab
import core.agent_backup_export as abx
import core.agent_restore_intake as ri
from core import memory_backup
from test_agent_backup import _make_env, NO_IDENTITY_TRAILERS, NO_CREDENTIALS

SENTINELS = ("PIN-SENTINEL", "SALT-SENTINEL", "KEY-SENTINEL", "OWNER-SENTINEL")
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def source(tmp_path):
    data, base = map(Path, _make_env(tmp_path / "src"))
    (data / "knowledge_packs_installed.json").write_text(json.dumps({
        "lumina-self-knowledge": {"version": 1, "installed_at": "2026-10-20"},
        "future-pack": {"version": 3, "deleted_at": "2026-10-21", "extra": {"x": 1}}}))
    (data / "memory/prefs.json").write_text(json.dumps({
        "human_bio": "SYNTHETIC HUMAN", "last_persona": "/old/personas/lumina.json",
        "pin_hash": "PIN-SENTINEL", "pin_salt": "SALT-SENTINEL", "openai_api_key": "KEY-SENTINEL",
        "telegram_remote_access": True, "telegram_owner_chat_id": "OWNER-SENTINEL",
        "future_unknown_key": 1}))
    with sqlite3.connect(data / "memory/lumina.db") as conn:
        conn.execute("INSERT INTO skills VALUES(1, 'Duck', 'synthetic', ?, 't', 't')",
                     (str(base / "skills/rubber-duck-debugging.md"),))
    return data, base


@pytest.fixture
def v1(source, tmp_path):
    data, base = source
    dest = tmp_path / "v1.zip"
    ab.build_agent_backup(str(data), str(base), str(dest),
                          identity_trailers_path=NO_IDENTITY_TRAILERS, credentials_path=NO_CREDENTIALS)
    return dest


@pytest.fixture
def v2(source, tmp_path):
    data, base = source
    dest = tmp_path / "v2.zip"
    abx.build_review_export_with_receipt(str(data), str(base), str(dest),
                                         identity_trailers_path=NO_IDENTITY_TRAILERS,
                                         credentials_path=NO_CREDENTIALS)
    return dest


class _FakeConn:
    def execute(self, *_a):
        return self

    def close(self):
        pass


@pytest.fixture
def legacy(source, tmp_path, monkeypatch):
    """A real Backup Memory (Legacy) ZIP from core.memory_backup's own builder."""
    data, _base = source
    (data / "memory/suspension.db").write_bytes(b"SYNTHETIC C1 STORE")
    (data / "memory/soul_identity.json").write_text('{"soul_id": "SOUL-SENTINEL"}')
    monkeypatch.setattr(memory_backup, "_db", lambda: _FakeConn())
    dest = tmp_path / "legacy.zip"
    memory_backup.build_memory_backup(str(data), str(dest))
    return dest


def entries(path):
    with zipfile.ZipFile(path) as z:
        return [(i.filename, z.read(i.filename)) for i in z.infolist()]


def repack(path, items, out=None, canonical=True):
    """Write (name, bytes) items in order, with A2's canonical ZipInfo by default."""
    out = out or path
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in items:
            z.writestr(ab._make_v1_zipinfo(name) if canonical else name, data)
    return out


def manifest_of(path):
    return json.loads(dict(entries(path))["manifest.json"])


def with_manifest(path, mutate):
    items = entries(path)
    manifest = json.loads(dict(items)["manifest.json"])
    mutate(manifest)
    return repack(path, [(n, json.dumps(manifest).encode() if n == "manifest.json" else d) for n, d in items])


def units(intake):
    return {u["unit"]: u for u in intake["units"]}


def tree_state(root):
    root = Path(root)
    if not root.exists():
        return None
    state = {}
    for p in sorted(root.rglob("*")):
        st = os.lstat(p)
        state[str(p.relative_to(root))] = (st.st_mode, st.st_ino, st.st_size, st.st_mtime_ns,
                                           p.read_bytes() if p.is_file() and not p.is_symlink() else None)
    return state


# ---------------------------------------------------------------- classes

def test_v1_agent_backup_is_verified_input_with_older_coverage(v1):
    intake = ri.intake_archive(v1)
    assert intake["kind"] == ri.VERIFIED and intake["format"] == "agent_backup_v1"
    assert intake["verification"]["status"] == "PASS"
    assert intake["archive_sha256"] == hashlib.sha256(v1.read_bytes()).hexdigest()
    u = units(intake)
    assert u["primary_database"]["availability"] == "complete"
    assert u["custom_tools"]["disposition"] == "historical_inactive"
    assert u["audit_and_pending_actions"]["disposition"] == "historical_inactive"
    assert u["project:demo"]["binding"] == "historical_inactive_owner_must_repick_root"
    # v1 copied prefs verbatim: the plan drops security fields, by count only.
    assert u["preferences"]["security_fields_dropped"] >= 4
    assert u["preferences"]["telegram_remote_access"] == "forced_off"
    assert intake["coverage"]["installation_history"] == "not_captured_by_format"
    assert intake["coverage"]["c1_safety_state"] == "not_captured"
    assert intake["full_restore_eligible"] is False
    blob = json.dumps(intake)
    assert not any(s in blob for s in SENTINELS)


def test_v2_export_is_verified_input_and_adopts_installation_history(v2):
    intake = ri.intake_archive(v2)
    assert intake["kind"] == ri.VERIFIED and intake["format"] == "agent_export_v2.1"
    assert intake["coverage"]["installation_history"] == "present"
    assert intake["coverage"]["c1_at_source"] == "absent_unknown"
    assert units(intake)["knowledge_pack_installation"]["availability"] == "complete"
    assert intake["full_restore_eligible"] is False


def test_damaged_noncritical_member_gives_selective_candidates_only(v2):
    items = [(n, b'{"name": "TAMPERED"}' if n == "overlay/personas/lumina.json" else d)
             for n, d in entries(v2)]
    repack(v2, items)
    intake = ri.intake_archive(v2)
    assert intake["kind"] == ri.SELECTIVE
    assert intake["verification"]["status"] == "FAILED"
    u = units(intake)
    assert u["personas"]["availability"] == "unavailable"
    assert u["personas"]["skipped"] == [{"archive_path": "overlay/personas/lumina.json",
                                         "reason": "hash_mismatch"}]
    assert u["primary_database"]["availability"] == "complete"
    assert u["skills"]["availability"] == "complete"
    assert intake["full_restore_eligible"] is False


def test_damaged_primary_database_closes_its_dependents(v2):
    items = [(n, d[:-1] + bytes([d[-1] ^ 1]) if n == "state/databases/lumina.db" else d)
             for n, d in entries(v2)]
    repack(v2, items)
    intake = ri.intake_archive(v2)
    assert intake["kind"] == ri.SELECTIVE
    u = units(intake)
    assert u["primary_database"]["availability"] == "unavailable"
    assert u["skills"]["availability"] == "unavailable"
    assert u["skills"]["skipped"][0]["reason"] == "dependency_unavailable:primary_database"
    project = u["project:demo"]
    assert project["availability"] == "partial"
    assert "overlay/projects/demo/project.md" in project["recoverable"]
    assert {"archive_path": "overlay/projects/demo/chats.json",
            "reason": "dependency_unavailable:primary_database"} in project["skipped"]
    assert u["flight_recorder"]["availability"] == "complete"


def test_crc_damage_in_stored_bytes_is_a_damaged_member(v1):
    raw = bytearray(v1.read_bytes())
    with zipfile.ZipFile(io.BytesIO(bytes(raw))) as z:
        info = z.getinfo("overlay/skills/rubber-duck-debugging.md")
    local = ab._parse_local_header(bytes(raw), info.header_offset)
    raw[info.header_offset + local["header_size"]] ^= 0xFF
    v1.write_bytes(bytes(raw))
    intake = ri.intake_archive(v1)
    assert intake["kind"] == ri.SELECTIVE
    assert units(intake)["skills"]["skipped"][0]["reason"] == "crc_or_inflate_failed"


def test_missing_declared_member_gives_selective_candidates(v2):
    repack(v2, [(n, d) for n, d in entries(v2) if n != "overlay/voices/lumina.wav"])
    intake = ri.intake_archive(v2)
    assert intake["kind"] == ri.SELECTIVE
    assert units(intake)["voices"]["skipped"] == [{"archive_path": "overlay/voices/lumina.wav",
                                                   "reason": "missing"}]


def test_undeclared_member_is_rejected_not_salvaged(v2):
    items = entries(v2)
    items.insert(0, ("overlay/personas/smuggled.json", b"{}"))
    repack(v2, items)
    intake = ri.intake_archive(v2)
    assert intake["kind"] == ri.REJECTED
    assert intake["refusal"]["code"] == "ARCHIVE_UNDECLARED_MEMBER"
    assert intake["units"] == []


@pytest.mark.parametrize("mutate", [
    lambda m: m["members"][0].update(owner_authorized=True),
    lambda m: m["members"][0].update(restore_policy="activate_immediately"),
    lambda m: m.update(backup_mode="full"),
    lambda m: m.pop("historical_claims"),
])
def test_manifest_policy_tampering_rejects_whole_archive(v2, mutate):
    with_manifest(v2, mutate)
    intake = ri.intake_archive(v2)
    assert intake["kind"] == ri.REJECTED
    assert intake["refusal"]["code"] == "MANIFEST_SCHEMA_INVALID"


@pytest.mark.parametrize("version", [{"major": 3, "minor": 0}, {"major": 2, "minor": 9},
                                     {"major": 1, "minor": 1}, "2.1"])
def test_unknown_or_newer_format_fails_closed(v2, version):
    with_manifest(v2, lambda m: m.update(format_version=version))
    intake = ri.intake_archive(v2)
    assert intake["kind"] == ri.REJECTED and intake["refusal"]["code"] == "FORMAT_UNSUPPORTED"


def test_export_without_installation_history_never_offers_it(source, tmp_path):
    data, base = source
    (data / "knowledge_packs_installed.json").unlink()
    dest = tmp_path / "nomarker.zip"
    abx.build_review_export_with_receipt(str(data), str(base), str(dest),
                                         identity_trailers_path=NO_IDENTITY_TRAILERS,
                                         credentials_path=NO_CREDENTIALS)
    intake = ri.intake_archive(dest)
    assert intake["kind"] == ri.VERIFIED
    assert intake["coverage"]["installation_history"] == "missing_unknown"
    unit = units(intake)["knowledge_pack_installation"]
    assert unit["availability"] == "unavailable"
    assert unit["skipped"] == [{"archive_path": abx.MARKER_PATH, "reason": "history_missing_unknown"}]


def test_damaged_installation_marker_is_listed_once(v2):
    repack(v2, [(n, d.replace(b"future-pack", b"futuRe-pack") if n == abx.MARKER_PATH else d)
                for n, d in entries(v2)])
    intake = ri.intake_archive(v2)
    assert intake["kind"] == ri.SELECTIVE
    assert units(intake)["knowledge_pack_installation"]["skipped"] == [
        {"archive_path": abx.MARKER_PATH, "reason": "hash_mismatch"}]


def test_export_from_initialized_c1_source_records_exclusion_only(source, tmp_path):
    data, base = source
    (data / "memory/suspension.db").write_bytes(b"SYNTHETIC STORE")
    (data / "memory/soul_identity.json").write_text('{"soul_id": "SOUL-SENTINEL"}')
    dest = tmp_path / "c1.zip"
    abx.build_review_export_with_receipt(str(data), str(base), str(dest),
                                         identity_trailers_path=NO_IDENTITY_TRAILERS,
                                         credentials_path=NO_CREDENTIALS)
    intake = ri.intake_archive(dest)
    assert intake["kind"] == ri.VERIFIED
    assert intake["coverage"]["c1_at_source"] == "present_excluded"
    assert intake["coverage"]["c1_safety_state"] == "not_captured"
    assert not any("suspension" in m["archive_path"] or "soul" in m["archive_path"]
                   for m in intake["members"])


def test_type_invalid_preferences_are_not_offered(source, tmp_path):
    data, base = source
    (data / "memory/prefs.json").write_text(json.dumps({"human_bio": "x", "tts_enabled": "yes"}))
    dest = tmp_path / "v1-badprefs.zip"
    ab.build_agent_backup(str(data), str(base), str(dest),
                          identity_trailers_path=NO_IDENTITY_TRAILERS, credentials_path=NO_CREDENTIALS)
    unit = units(ri.intake_archive(dest))["preferences"]
    assert unit["availability"] == "unavailable"
    assert unit["skipped"] == [{"archive_path": "state/preferences/prefs.json", "reason": "payload_policy"}]


def test_unparseable_manifest_fails_closed(v2):
    repack(v2, [(n, b'{"a": 1, "a": 2}' if n == "manifest.json" else d) for n, d in entries(v2)])
    assert ri.intake_archive(v2)["refusal"]["code"] == "MANIFEST_INVALID"


def test_unknown_required_feature_keeps_history_but_blocks_activation(v2, tmp_path):
    with_manifest(v2, lambda m: m.update(required_features=["future.runtime-feature"]))
    intake = ri.intake_archive(v2)
    assert intake["kind"] == ri.VERIFIED
    assert intake["coverage"]["unsupported_required_features"] == ["future.runtime-feature"]
    preview = ri.preview_recovery(intake, tmp_path / "fresh")
    assert "unsupported_required_features" in preview["activation"]["blockers"]
    assert preview["activation"]["available"] is False


# ---------------------------------------------------------------- legacy

def test_real_legacy_backup_is_limited_and_never_offers_c1_or_secrets(legacy):
    intake = ri.intake_archive(legacy)
    assert intake["kind"] == ri.LEGACY and intake["format"] == "legacy_memory_backup"
    assert intake["verification"]["status"] == "not_applicable"
    u = units(intake)
    assert u["primary_database"]["availability"] == "complete"
    assert u["primary_database"]["provenance"] == "legacy_unverified"
    assert u["primary_database"]["integrity"] == "crc_and_payload_shape_only"
    assert u["custom_tools"]["disposition"] == "historical_inactive"
    assert set(u["project:demo"]["recoverable"]) == {"projects/demo/binding.json", "projects/demo/chats.json"}
    assert {x["unit"] for x in intake["units_not_in_archive"]} >= {"skills", "personas", "project_list"}
    assert intake["coverage"]["c1_safety_state"] == "evidence_only"
    assert intake["coverage"]["excluded_entries_present"] is True  # memory/ledger.db
    offered = {p for unit in intake["units"] for p in unit["recoverable"]}
    assert not any("suspension" in p or "soul_identity" in p or "ledger" in p for p in offered)
    blob = json.dumps(intake)
    assert not any(s in blob for s in SENTINELS + ("SOUL-SENTINEL",))
    assert u["preferences"]["security_fields_dropped"] >= 4


@pytest.mark.parametrize("sidecar", ["memory/lumina.db-wal", "memory/lumina.db-journal"])
def test_legacy_nonempty_wal_or_journal_is_an_ambiguous_database_generation(legacy, sidecar):
    items = entries(legacy) + [(sidecar, b"\x00" * 64)]
    repack(legacy, items, canonical=False)
    u = units(ri.intake_archive(legacy))
    assert u["primary_database"]["availability"] == "unavailable"
    assert u["primary_database"]["skipped"][0]["reason"] == "ambiguous_database_generation"


def test_legacy_checkpointed_empty_wal_is_not_ambiguous(legacy):
    repack(legacy, entries(legacy) + [("memory/lumina.db-wal", b""), ("memory/lumina.db-shm", b"\x00" * 32)],
           canonical=False)
    assert units(ri.intake_archive(legacy))["primary_database"]["availability"] == "complete"


def test_legacy_corrupt_primary_database_is_unavailable(legacy):
    repack(legacy, [(n, b"SQLite format 3\x00garbage" if n == "memory/lumina.db" else d)
                    for n, d in entries(legacy)], canonical=False)
    u = units(ri.intake_archive(legacy))
    assert u["primary_database"]["skipped"][0]["reason"] == "sqlite_invalid"


def test_legacy_invalid_installation_marker_is_not_adopted(legacy):
    repack(legacy, [(n, b'{"pack": []}' if n == "knowledge_packs_installed.json" else d)
                    for n, d in entries(legacy)], canonical=False)
    intake = ri.intake_archive(legacy)
    assert intake["coverage"]["installation_history"] == "invalid"
    assert units(intake)["knowledge_pack_installation"]["availability"] == "unavailable"


def test_legacy_zip_with_stored_members_is_still_legacy(legacy, tmp_path):
    stored = tmp_path / "stored.zip"
    with zipfile.ZipFile(stored, "w", zipfile.ZIP_STORED) as z:
        for name, data in entries(legacy):
            z.writestr(name, data)
    intake = ri.intake_archive(stored)
    assert intake["kind"] == ri.LEGACY
    assert units(intake)["primary_database"]["availability"] == "complete"


def test_manifestless_zip_without_legacy_anchor_is_rejected(tmp_path):
    p = repack(tmp_path / "x.zip", [("notes/readme.txt", b"hello")], canonical=False)
    assert ri.intake_archive(p)["refusal"]["code"] == "LEGACY_LAYOUT_UNRECOGNIZED"


def test_structured_layout_without_manifest_is_not_treated_as_legacy(v2):
    repack(v2, [(n, d) for n, d in entries(v2) if n != "manifest.json"] + [("memory/prefs.json", b"{}")])
    assert ri.intake_archive(v2)["refusal"]["code"] == "FORMAT_UNSUPPORTED"


# ---------------------------------------------------------------- dangerous input

def test_input_file_rules(tmp_path, v1):
    assert ri.intake_archive(tmp_path / "nope.zip")["refusal"]["code"] == "INPUT_UNAVAILABLE"
    assert ri.intake_archive(tmp_path)["refusal"]["code"] == "INPUT_NOT_REGULAR"
    link = tmp_path / "link.zip"
    link.symlink_to(v1)
    assert ri.intake_archive(link)["refusal"]["code"] == "INPUT_NOT_REGULAR"
    os.link(v1, tmp_path / "hard.zip")
    assert ri.intake_archive(v1)["refusal"]["code"] == "INPUT_ALIASED"
    junk = tmp_path / "junk.zip"
    junk.write_bytes(b"not a zip at all")
    assert ri.intake_archive(junk)["refusal"]["code"] == "NOT_A_ZIP"


def test_input_over_budget_is_refused_before_reading(v1, monkeypatch):
    monkeypatch.setattr(ab, "_determine_aggregate_payload_budget", lambda *_a, **_k: 16)
    monkeypatch.setattr(ab, "_open_and_capture_stable_bytes",
                        lambda *a, **k: pytest.fail("read an over-budget input"))
    assert ri.intake_archive(v1)["refusal"]["code"] == "INPUT_TOO_LARGE"


def test_declared_expansion_over_budget_is_refused_before_decompression(v1, monkeypatch):
    monkeypatch.setattr(ab, "_determine_verification_payload_budget", lambda **_k: 1024)
    monkeypatch.setattr(zipfile.ZipFile, "read", lambda *a, **k: pytest.fail("decompressed"))
    assert ri.intake_archive(v1)["refusal"]["code"] == "ZIP_RESOURCE_LIMIT"


@pytest.mark.parametrize("names,code", [
    (["../escape.txt"], "ZIP_ENTRY_UNSAFE"),
    (["/etc/passwd"], "ZIP_ENTRY_UNSAFE"),
    (["memory\\prefs.json"], "ZIP_ENTRY_UNSAFE"),
    (["memory//prefs.json"], "ZIP_ENTRY_UNSAFE"),
    (["memory/"], "ZIP_ENTRY_UNSAFE"),
    (["memory/prefs.json", "memory/prefs.json"], "ZIP_NAME_COLLISION"),
    (["memory/prefs.json", "MEMORY/PREFS.JSON"], "ZIP_NAME_COLLISION"),
])
def test_unsafe_names_fail_closed(tmp_path, names, code):
    p = tmp_path / "bad.zip"
    with pytest.warns(UserWarning) if len(set(names)) < len(names) else _null():
        repack(p, [(n, b"{}") for n in names], canonical=False)
    assert ri.intake_archive(p)["refusal"]["code"] == code


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_symlink_entry_fails_closed(tmp_path):
    p = tmp_path / "link.zip"
    info = zipfile.ZipInfo("memory/prefs.json")
    info.external_attr = (0o120777) << 16
    with zipfile.ZipFile(p, "w") as z:
        z.writestr(info, "/home/someone/.ssh/id_rsa")
    assert ri.intake_archive(p)["refusal"]["code"] == "ZIP_ENTRY_UNSAFE"


def _set_flag(raw, info, bit):
    """Set a general-purpose flag bit in both headers of a one-entry archive."""
    raw = bytearray(raw)
    raw[info.header_offset + 6:info.header_offset + 8] = struct.pack("<H", info.flag_bits | bit)
    cd = raw.find(b"PK\x01\x02")
    raw[cd + 8:cd + 10] = struct.pack("<H", info.flag_bits | bit)
    return bytes(raw)


def test_encrypted_entry_fails_closed(tmp_path):
    p = repack(tmp_path / "e.zip", [("memory/prefs.json", b"{}")], canonical=False)
    with zipfile.ZipFile(p) as z:
        info = z.infolist()[0]
    p.write_bytes(_set_flag(p.read_bytes(), info, 0x1))
    assert ri.intake_archive(p)["refusal"]["code"] == "ZIP_ENCRYPTED"


@pytest.mark.parametrize("kind", ["v1", "legacy"])
@pytest.mark.parametrize("offset", [12, 14])  # local-header DOS date; local CRC-32
def test_local_and_central_header_disagreement_fails_closed(request, kind, offset):
    # Legacy archives get no A2 canonical layout check, so intake's own
    # grammar is the only thing standing between them and this ambiguity.
    archive = request.getfixturevalue(kind)
    raw = bytearray(archive.read_bytes())
    raw[offset] ^= 0x01  # first member's local header only; the central copy is unchanged
    archive.write_bytes(bytes(raw))
    assert ri.intake_archive(archive)["refusal"]["code"] == "ZIP_STRUCTURE_UNSAFE"


def test_dos_directory_attribute_entry_fails_closed(tmp_path):
    p = tmp_path / "dosdir.zip"
    info = zipfile.ZipInfo("memory/prefs.json")
    info.external_attr = 0x10
    with zipfile.ZipFile(p, "w") as z:
        z.writestr(info, "{}")
    assert ri.intake_archive(p)["refusal"]["code"] == "ZIP_ENTRY_UNSAFE"


@pytest.mark.parametrize("damage", ["prefix", "trailing", "concatenated"])
def test_physical_ambiguity_fails_closed(v1, damage):
    raw = v1.read_bytes()
    v1.write_bytes({"prefix": b"MZ-stub" + raw, "trailing": raw + b"extra",
                    "concatenated": raw + raw}[damage])
    intake = ri.intake_archive(v1)
    assert intake["kind"] == ri.REJECTED
    assert intake["refusal"]["code"] in {"ZIP_STRUCTURE_UNSAFE", "NOT_A_ZIP"}


_ZIP_FIELDS = ("filename", "date_time", "compress_type", "comment", "extra", "create_system",
               "create_version", "extract_version", "flag_bits", "volume", "internal_attr",
               "external_attr", "header_offset", "CRC", "compress_size", "file_size")


def _zip_view(raw):
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        return {i.filename: ({f: getattr(i, f) for f in _ZIP_FIELDS}, z.read(i.filename))
                for i in z.infolist()}


@pytest.mark.parametrize("damage", ["prefix", "concatenated", "gap"])
def test_legacy_physical_ambiguity_fails_closed(legacy, damage):
    raw = legacy.read_bytes()
    if damage == "gap":
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            second = sorted(z.infolist(), key=lambda i: i.header_offset)[1].header_offset
        # Unreferenced bytes before member two: the central directory no longer
        # describes the physical layout.
        raw = raw[:second] + b"HIDDEN-LOCAL-RECORD" + raw[second:]
    legacy.write_bytes({"prefix": b"MZ-stub" + raw, "concatenated": raw + raw, "gap": raw}[damage])
    intake = ri.intake_archive(legacy)
    assert intake["kind"] == ri.REJECTED
    assert intake["refusal"]["code"] in {"ZIP_STRUCTURE_UNSAFE", "NOT_A_ZIP"}


def test_hidden_unreferenced_local_record_fails_closed(legacy):
    """A local record the central directory omits, with a self-consistent EOCD:
    another reader could see a member this one never lists."""
    items = entries(legacy)
    items.insert(1, ("memory/hidden.json", b'{"smuggled": true}'))
    raw = repack(legacy, items, canonical=False).read_bytes()
    eocd = raw.rfind(b"PK\x05\x06")
    sig, disk, cd_disk, here, total, cd_size, cd_offset, comment = struct.unpack("<4sHHHHIIH", raw[eocd:eocd + 22])
    records, pos = [], cd_offset
    while pos < cd_offset + cd_size:
        n, e, c = struct.unpack("<HHH", raw[pos + 28:pos + 34])
        size = 46 + n + e + c
        if raw[pos + 46:pos + 46 + n] != b"memory/hidden.json":
            records.append(raw[pos:pos + size])
        pos += size
    central = b"".join(records)
    tail = struct.pack("<4sHHHHIIH", sig, 0, 0, len(records), len(records), len(central), cd_offset, 0)
    legacy.write_bytes(raw[:cd_offset] + central + tail)
    with zipfile.ZipFile(legacy) as z:
        assert "memory/hidden.json" not in z.namelist()  # invisible to an ordinary reader
    assert ri.intake_archive(legacy)["refusal"]["code"] == "ZIP_STRUCTURE_UNSAFE"


def test_unattributed_verifier_refusal_rejects_whole_archive(v2, monkeypatch):
    # Every check intake can attribute mirrors the verifier today; anything the
    # strict verifier adds later must still fail closed, never become salvage.
    monkeypatch.setattr(abx, "verify_export", lambda *a, **k: {"valid": False, "errors": ["future check"]})
    intake = ri.intake_archive(v2)
    assert intake["kind"] == ri.REJECTED
    assert intake["refusal"]["code"] == "ARCHIVE_INTEGRITY_FAILED"


def test_verifier_pass_contradicted_by_member_damage_rejects(v2, monkeypatch):
    repack(v2, [(n, b"{}" if n == "overlay/tool_profiles/chat.json" else d) for n, d in entries(v2)])
    monkeypatch.setattr(abx, "verify_export", lambda *a, **k: {"valid": True, "errors": []})
    intake = ri.intake_archive(v2)
    assert intake["kind"] == ri.REJECTED
    assert intake["refusal"]["code"] == "ARCHIVE_INTEGRITY_FAILED"


def test_every_byte_flip_is_rejected_damaged_or_content_identical(v2):
    """No single-byte change can verify with different content or header
    meaning. Survivors only touch bits no reader uses (the central 'reserved'
    byte, deflate padding); the archive hash still changes and is bound into
    the plan digest."""
    raw = v2.read_bytes()
    original = _zip_view(raw)
    for offset in range(len(raw)):
        flipped = bytearray(raw)
        flipped[offset] ^= 0x5A
        v2.write_bytes(bytes(flipped))
        intake = ri.intake_archive(v2)
        if intake["kind"] == ri.REJECTED:
            assert intake["units"] == []
            continue
        assert intake["archive_sha256"] != hashlib.sha256(raw).hexdigest()
        if intake["kind"] == ri.VERIFIED:
            assert _zip_view(bytes(flipped)) == original, offset
        else:
            assert intake["kind"] == ri.SELECTIVE, offset


# ---------------------------------------------------------------- preview

def test_preview_into_fresh_destination_writes_nothing(v2, tmp_path):
    dest = tmp_path / "fresh-data"
    preview = ri.preview_recovery(ri.intake_archive(v2), dest, tmp_path / "fresh-base")
    assert not dest.exists() and not (tmp_path / "fresh-base").exists()
    assert preview["status"] == "preview" and preview["writes_performed"] is False
    u = units(preview)
    assert u["primary_database"]["destination_effect"] == "would_create"
    assert u["custom_tools"]["destination_effect"] == "preserve_as_inactive_history"
    assert preview["safety"]["c1"]["plan"] == "restore_trace_required_then_owner_recover"
    assert preview["safety"]["archived_c1_state"] == "never_restored_as_live_files"
    assert "credentials" in preview["safety"]["always_excluded"]
    assert preview["safety"]["telegram_remote_access"] == "forced_off"
    assert preview["activation"]["available"] is False
    assert preview["activation"]["gate"] == list(ri.ACTIVATION_GATE)


def test_preview_preserves_healthy_destination_and_its_c1_authority(v2, tmp_path, monkeypatch):
    data, base = map(Path, _make_env(tmp_path / "dest"))
    (data / "memory/suspension.db").write_bytes(b"DESTINATION STORE")
    (data / "memory/soul_identity.json").write_text('{"soul_id": "DEST"}')
    (data / "memory/suspension_evidence").mkdir()
    before = (tree_state(data), tree_state(base))
    intake = ri.intake_archive(v2)
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: pytest.fail("preview opened SQLite"))
    preview = ri.preview_recovery(intake, data, base)
    assert (tree_state(data), tree_state(base)) == before
    assert preview["safety"]["c1"] == {"status": "present", "plan": "destination_authoritative_unchanged",
                                       "identity_pin": "present"}
    u = units(preview)
    effect = "conflict_owner_choice_and_rollback_snapshot_required"
    assert u["primary_database"]["destination_effect"] == effect
    assert u["skills"]["destination_effect"] == effect
    assert u["preferences"]["destination_effect"] == effect


def test_preview_refuses_unobservable_or_aliased_destination(v2, tmp_path):
    real = tmp_path / "real"
    (real / "memory").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    intake = ri.intake_archive(v2)
    assert ri.preview_recovery(intake, alias)["status"] == "refused"
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / "memory").symlink_to(real / "memory", target_is_directory=True)
    refused = ri.preview_recovery(intake, linked)
    assert refused["status"] == "refused" and refused["units"] == []


def test_preview_blocks_symlinked_destination_family_path(v2, tmp_path):
    data, base = map(Path, _make_env(tmp_path / "dest"))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (base / "skills" / "rubber-duck-debugging.md").unlink()
    (base / "skills").rmdir()
    (base / "skills").symlink_to(elsewhere, target_is_directory=True)
    preview = ri.preview_recovery(ri.intake_archive(v2), data, base)
    assert units(preview)["skills"]["destination_effect"] == "blocked_unsafe_destination_path"
    assert "unsafe_destination_path" in preview["activation"]["blockers"]


def test_preview_labels_a_symlinked_destination_file_as_an_alias(v2, tmp_path):
    data, base = map(Path, _make_env(tmp_path / "dest"))
    persona = base / "personas" / "lumina.json"
    persona.unlink()
    persona.symlink_to(tmp_path / "outside.json")
    preview = ri.preview_recovery(ri.intake_archive(v2), data, base)
    assert preview["destination"]["observed"]["base:personas/lumina.json"] == ["unsafe_alias", None]
    assert units(preview)["personas"]["destination_effect"] == "blocked_unsafe_destination_path"


def test_rejected_input_previews_as_refused(tmp_path):
    junk = tmp_path / "junk.zip"
    junk.write_bytes(b"\x00" * 10)
    preview = ri.preview_recovery(ri.intake_archive(junk), tmp_path / "dest")
    assert preview["status"] == "refused" and preview["units"] == []
    assert preview["activation"]["available"] is False


def test_legacy_preview_discloses_limits(legacy, tmp_path):
    preview = ri.preview_recovery(ri.intake_archive(legacy), tmp_path / "dest")
    assert "legacy_provenance_unknown" in preview["activation"]["blockers"]
    assert {u["unit"] for u in preview["units_not_in_archive"]} >= {"skills", "personas"}


def test_v1_preview_holds_bootstrap_without_installation_history(v1, tmp_path):
    preview = ri.preview_recovery(ri.intake_archive(v1), tmp_path / "dest")
    assert "bootstrap_hold_installation_history_unknown" in preview["activation"]["blockers"]


def test_plan_digest_binds_archive_and_destination(v2, tmp_path):
    data, base = map(Path, _make_env(tmp_path / "dest"))
    (data / "memory" / "prefs.json").unlink()
    preview = ri.preview_recovery(ri.intake_archive(v2), data, base)
    assert ri.revalidate_preview(preview, v2, data, base) is True
    forged = json.loads(json.dumps(preview))
    forged["activation"]["available"] = True
    assert ri.revalidate_preview(forged, v2, data, base) is False
    (data / "memory" / "prefs.json").write_text("{}")
    assert ri.revalidate_preview(preview, v2, data, base) is False
    (data / "memory" / "prefs.json").unlink()
    assert ri.revalidate_preview(preview, v2, data, base) is True
    before = v2.read_bytes()
    repack(v2, entries(v2)[::-1])  # same members, different archive bytes
    assert v2.read_bytes() != before
    assert ri.intake_archive(v2)["kind"] == ri.VERIFIED
    assert ri.revalidate_preview(preview, v2, data, base) is False


def test_preview_requires_an_intake_object(tmp_path):
    with pytest.raises(TypeError):
        ri.preview_recovery({"kind": ri.VERIFIED, "units": []}, tmp_path)


# ---------------------------------------------------------------- source boundaries

def test_intake_module_has_no_write_or_execute_paths():
    tree = ast.parse((REPO / "core" / "agent_restore_intake.py").read_text(encoding="utf-8"))
    os_verbs = {"replace", "rename", "unlink", "remove", "rmdir", "mkdir", "makedirs", "link", "symlink",
                "chmod", "chown", "open", "fdopen", "truncate", "system", "popen", "utime"}
    any_receiver = {"write", "write_bytes", "write_text", "writestr", "extractall", "extract", "connect",
                    "exec_module", "rmtree", "copyfile"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Attribute):
                assert fn.attr not in any_receiver, fn.attr
                if isinstance(fn.value, ast.Name) and fn.value.id == "os":
                    assert fn.attr not in os_verbs, fn.attr
            elif isinstance(fn, ast.Name):
                assert fn.id not in {"open", "exec", "eval", "compile", "__import__"}, fn.id
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module]
            assert not any(m in {"shutil", "subprocess", "tempfile", "core.suspension"} for m in mods), mods
