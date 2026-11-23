"""C1-S3b: Restore reads format 2.3 (the Verified Agent Backup) into an
isolated root and keeps C1's sanitized record only as inactive history.

Synthetic roots only (C1S3-AMENDMENT-A1 A-11). Pins TS3-05 (per-format plan,
receipt, coverage and class values), TS3-06 (the record under
restore_history/<restore_id>/c1/, its sha256 in the receipt, deviation
D-S3b-1), that nothing at runtime reads that record as state, and that every
malformed, mismatched, tampered or interrupted case fails closed without
touching the source archive or the destination. C1-S2 is not built here.
"""
import ast
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import core.agent_backup_export as ex
import core.agent_backup_restore as ar
import core.agent_backup_restore_cockpit as cp
import core.agent_backup_restore_intake as ri
from core import operator_ingress as oi
from core import suspension as S
from core import suspension_backup as c1
from core import wire_seat_gate as wg
from test_agent_backup_verified_export_01 import source, NO_CREDENTIALS, NO_IDENTITY_TRAILERS  # noqa: F401
from test_c1_verified_backup_s3a import (  # noqa: F401
    _fresh_cache, c1_member, entries, incomplete, lint, rebuild, verified, vsource)

REPO = Path(__file__).resolve().parent.parent
RECORD_PATH = ex.C1_MEMBER_POLICY["archive_path"]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def v23(vsource):
    verified(vsource)
    return vsource[2]


@pytest.fixture
def target(tmp_path):
    base = tmp_path / "fresh_install"
    (base / "skills").mkdir(parents=True)
    (base / "personas").mkdir()
    (tmp_path / "restore_parent").mkdir()
    return tmp_path / "restore_parent" / "data", base


def restore(archive, data_dir, base_dir, **kw):
    plan = ar.plan_restore(archive, data_dir, base_dir, **kw)
    return plan, ar.restore_isolated(plan, archive, approved_plan_digest=plan["plan_digest"])


def archive_bytes(archive):
    return dict(entries(archive))


def manifest_of(archive):
    return json.loads(archive_bytes(archive)["manifest.json"])


def sha(data):
    return hashlib.sha256(data).hexdigest()


def snapshot(path):
    """Every name under path with its type and bytes; None if absent."""
    path = Path(path)
    if not os.path.lexists(path):
        return None
    if path.is_file():
        return path.read_bytes()
    return {str(p.relative_to(path)): (p.read_bytes() if p.is_file() and not p.is_symlink() else
                                       ("link" if p.is_symlink() else "dir"))
            for p in sorted(path.rglob("*"))}


def leftovers(data_dir):
    return sorted(p.name for p in Path(data_dir).parent.iterdir() if ".restore-" in p.name)


def text_lint(text):
    """S3a's owner-words lint (forbidden claims and raw code shapes)."""
    lint(text)


def c1_unit(units):
    return next(u for u in units if u["unit"] == ri.C1_UNIT)


# ---------------------------------------------------------------------------
# Intake, plan and receipt: per-format values (TS3-05)
# ---------------------------------------------------------------------------

def test_version_sets_and_the_format_hint(v23, tmp_path, vsource):
    assert ex.READ_VERSIONS == ({"major": 2, "minor": 0}, {"major": 2, "minor": 1},
                                {"major": 2, "minor": 2}, {"major": 2, "minor": 3})
    assert ex.VERIFY_VERSIONS == ex.READ_VERSIONS
    # The hint claims no blocker it did not compute: 2.3 is checked first.
    assert ex.classify_input(str(v23)) == {
        "kind": "agent_export_v2_verified", "verification": "not_performed",
        "full_agent_coverage": False, "coverage_blockers": ["verification_not_performed"]}
    incomplete(vsource, tmp_path / "incomplete.zip")
    assert ex.classify_input(str(tmp_path / "incomplete.zip"))["coverage_blockers"] == ["suspension"]


def test_a_verified_2_3_intake_derives_its_own_coverage(v23):
    manifest = manifest_of(v23)
    observation = manifest["export_observations"]["c1_recovery"]
    intake = ri.intake_archive(str(v23))
    assert intake["kind"] == ri.VERIFIED and intake["format"] == ri.FORMAT_V23 == "agent_export_v2.3"
    assert intake["verification"]["status"] == "PASS"
    cov = intake["coverage"]
    assert cov["format_coverage"] == "verified_agent_backup"
    assert cov["c1_safety_state"] == "captured_sanitized_history_only"
    assert cov["coverage_blockers"] == []
    assert cov["c1_at_source"] == "present_excluded"
    assert cov["c1_record"] == {
        "archive_path": RECORD_PATH, "sha256": observation["record_sha256"],
        "record_schema": c1.RECOVERY_STATE_SCHEMA, "record_version": c1.RECOVERY_STATE_VERSION,
        "claimed_source_incarnation": observation["claimed_source_incarnation"],
        "provenance": "archive_declared_producer_claims",
        "restore_policy": "inactive_history_never_live_c1_state"}
    assert cov["c1_record"]["sha256"] == sha(archive_bytes(v23)[RECORD_PATH])
    unit = c1_unit(intake["units"])
    assert unit["disposition"] == "historical_inactive" and unit["availability"] == "complete"
    assert unit["recoverable"] == [RECORD_PATH]
    assert intake["full_restore_eligible"] is False
    preview = ri.preview_recovery(intake, None)
    assert preview["safety"]["archived_c1_record"] == "inactive_history_only_never_live_c1_state"
    assert preview["safety"]["archived_c1_state"] == "never_restored_as_live_files"
    assert c1_unit(preview["units"])["destination_effect"] == "preserve_as_inactive_history"
    assert preview["activation"]["available"] is False


def test_intake_recomputes_the_blockers_and_fails_closed(v23, monkeypatch):
    """A layer under the verifier: were a blocker ever derived for a verified
    2.3 archive, intake rejects it instead of calling it Verified."""
    real_verify = ex.verify_export

    def verify_then_inject(*args, **kwargs):
        report = real_verify(*args, **kwargs)
        assert report["valid"]
        monkeypatch.setattr(ex, "coverage_blockers", lambda manifest: ["installation_marker"])
        return report

    monkeypatch.setattr(ex, "verify_export", verify_then_inject)
    intake = ri.intake_archive(str(v23))
    assert intake["kind"] == ri.REJECTED and intake["refusal"]["code"] == "ARCHIVE_INTEGRITY_FAILED"


def test_a_verified_2_3_plan_states_what_is_true(v23, target):
    data_dir, base = target
    plan = ar.plan_restore(v23, data_dir, base)
    assert plan["status"] == "ready"
    assert plan["restore_class"] == ar.VERIFIED == "verified_agent_backup"
    assert plan["verified_agent_backup"] == ar.VERIFIED_AGENT_BACKUP_V23 == {
        "available": True, "archive_verified": True, "c1_record": "captured_sanitized_history_only",
        "live_c1_state_restored": False, "recover_from_archive": "absent", "full_recovery": "not_proven"}
    assert plan["coverage"]["c1_safety_state"] == "captured_sanitized_history_only"
    assert plan["safety"]["archived_c1_record"] == "inactive_history_only_never_live_c1_state"
    assert plan["safety"]["archived_c1_state"] == "never_restored"
    unit = c1_unit(plan["units"])
    assert unit["choice"] == "recover" and unit["disposition"] == "historical_inactive"
    (f,) = unit["files"]
    assert f["root"] == "data" and f["transform"] == "c1_history" and f["action"] == "create"
    assert f["rel"] == f"restore_history/{plan['restore_id']}/c1/recovery_state.json"
    # The class is pinned by the plan digest: a fresh plan reproduces it.
    again = ar.plan_restore(v23, data_dir, base, restore_id=plan["restore_id"],
                            selection=dict(plan["selection"]))
    assert again["plan_digest"] == plan["plan_digest"]
    tampered = dict(plan, restore_class=ar.INCOMPLETE)
    with pytest.raises(ar.RestoreRefused) as info:
        ar.restore_isolated(tampered, v23, approved_plan_digest=plan["plan_digest"])
    assert info.value.code == "PLAN_NOT_APPROVED"


def test_the_record_is_kept_only_as_inactive_history_with_its_hash_in_the_receipt(v23, target):
    data_dir, base = target
    source_before = v23.read_bytes()
    plan, receipt = restore(v23, data_dir, base)
    record = archive_bytes(v23)[RECORD_PATH]
    rel = f"restore_history/{plan['restore_id']}/c1/recovery_state.json"
    placed = data_dir / rel
    assert placed.read_bytes() == record and oct(placed.stat().st_mode & 0o777) == "0o600"
    kept = receipt["c1"]["archived_c1_record"]
    assert kept == {
        "status": "kept_as_inactive_history", "archive_verified": True, "path": rel, "sha256": sha(record),
        "size": len(record),
        "record_schema": c1.RECOVERY_STATE_SCHEMA, "record_version": c1.RECOVERY_STATE_VERSION,
        "claimed_source_incarnation": plan["coverage"]["c1_record"]["claimed_source_incarnation"],
        "archive_member": RECORD_PATH, "archive_sha256": sha(source_before),
        "provenance": "archive_declared_producer_claims_revalidated_at_restore",
        "authority": "none_history_only", "consulted_by_runtime": False,
        "deviation": "D-S3b-1_sha256_in_receipt_not_trace"}
    assert kept["sha256"] == manifest_of(v23)["export_observations"]["c1_recovery"]["record_sha256"]
    assert receipt["c1"]["archived_c1_state"] == "never_restored"
    assert receipt["restore_class"] == ar.VERIFIED
    assert receipt["verified_agent_backup"] == ar.VERIFIED_AGENT_BACKUP_V23
    # Live C1 state is never written; C1 still needs the owner's Recover.
    for name in ar._FORBIDDEN_LIVE:
        assert not os.path.lexists(data_dir / name)
    assert S.probe(data_dir).status is S.StoreStatus.ABSENT_WITH_TRACE
    result = ar.verify_restored_root(data_dir, receipt)
    assert result["passed"] is True and result["checks"]["c1_history_record"] is True
    assert v23.read_bytes() == source_before


def test_the_restore_trace_format_is_unchanged_and_the_wire_gate_reads_it(v23, target, vsource, tmp_path):
    data_dir, base = target
    _plan, receipt = restore(v23, data_dir, base)
    raw = (data_dir / receipt["c1"]["trace"]).read_bytes()
    trace = json.loads(raw)
    assert set(trace) == wg.TRACE_KEYS == {"schema", "version", "at", "restore_id", "archive_sha256",
                                           "restore_class", "archived_c1_state", "required_next_step"}
    assert trace["restore_class"] == "verified_agent_backup" and trace["archived_c1_state"] == "not_restored"
    assert wg._trace_identity(raw) is not None
    assert "c1_record" not in raw.decode() and "recovery_state" not in raw.decode()
    # Same Wire verdict as an Incomplete State Export restore (W-B1 quarantine kept).
    incomplete(vsource, tmp_path / "incomplete.zip")
    other = tmp_path / "restore_parent" / "data22"
    restore(tmp_path / "incomplete.zip", other, base)
    assert wg.seat_verdict(str(data_dir)).reason == wg.seat_verdict(str(other)).reason
    assert wg.seat_verdict(str(data_dir)).allowed is False


def test_the_owner_may_skip_the_history_record(v23, target):
    data_dir, base = target
    intake = ri.intake_archive(str(v23))
    selection = {u["unit"]: "recover" for u in intake["units"]
                 if u["availability"] != "unavailable" and u["disposition"] != "informational"}
    selection[ri.C1_UNIT] = "skip"
    plan, receipt = restore(v23, data_dir, base, selection=selection)
    assert receipt["c1"]["archived_c1_record"] == {"status": "not_restored_owner_skipped"}
    assert not (data_dir / "restore_history" / plan["restore_id"] / "c1").exists()
    assert receipt["restore_class"] == ar.VERIFIED
    assert ar.verify_restored_root(data_dir, receipt)["passed"] is True


def test_pre_2_3_outputs_keep_their_values(vsource, target, tmp_path):
    incomplete(vsource, tmp_path / "incomplete.zip")
    archive = tmp_path / "incomplete.zip"
    intake = ri.intake_archive(str(archive))
    assert intake["format"] == "agent_export_v2.2"
    assert set(intake["coverage"]) == {
        "format_coverage", "installation_history", "c1_safety_state", "c1_at_source",
        "unsupported_required_features", "unclassified_preferences_omitted_at_export", "generated_images",
        "source_overlays"}
    assert intake["coverage"]["format_coverage"] == "incomplete_state_export"
    assert intake["coverage"]["c1_safety_state"] == "not_captured"
    assert not any(u["unit"] == ri.C1_UNIT for u in intake["units"])
    assert "archived_c1_record" not in ri.preview_recovery(intake, None)["safety"]
    data_dir, base = target
    plan, receipt = restore(archive, data_dir, base)
    assert plan["restore_class"] == ar.INCOMPLETE
    assert plan["verified_agent_backup"] == {"available": False, "reason": "c1_snapshot_contract_absent"}
    assert "archived_c1_record" not in plan["safety"]
    assert set(receipt["c1"]) == {"trace", "archived_c1_state", "required_next_step", "first_run_initialize"}
    checks = ar.verify_restored_root(data_dir, receipt)["checks"]
    assert "c1_history_record" not in checks and all(checks.values())


# ---------------------------------------------------------------------------
# Nothing reads restore_history/**/c1/ as state (TS3-06, A-11, directive 7)
# ---------------------------------------------------------------------------

def _runtime_view(data_dir):
    paths = S._paths(str(data_dir))
    return {"probe": S.probe(str(data_dir)).status.value, "traces": S._traces(paths),
            "trace_state": c1.restore_trace_state(str(data_dir)),
            "seat": wg.seat_verdict(str(data_dir)).reason,
            "exclusion": c1.describe_exclusions(str(data_dir)).status}


def _hostile_record():
    """A record that, if anything read it as state, would say the owner is
    not suspended and carry a foreign identity."""
    return json.dumps({"schema": c1.RECOVERY_STATE_SCHEMA, "version": 1, "owner_suspended": False,
                       "provenance": {"claimed_source_soul_id": "f" * 32,
                                      "claimed_source_incarnation": "e" * 32}}).encode()


def test_runtime_safety_and_wire_never_consult_the_history_record(v23, target):
    data_dir, base = target
    plan, _receipt = restore(v23, data_dir, base)
    record = data_dir / "restore_history" / plan["restore_id"] / "c1" / "recovery_state.json"
    baseline = _runtime_view(data_dir)
    assert baseline["probe"] == "absent_with_trace" and baseline["traces"]
    for content in (_hostile_record(), b"\x00garbage", None):
        if content is None:
            record.unlink()
        else:
            record.chmod(0o600)
            record.write_bytes(content)
        assert _runtime_view(data_dir) == baseline


def _recovered_store_dump(data_dir):
    outcome = S.recover_destructively(str(data_dir), ingress=oi.local_suspension_control("recover"),
                                      acknowledge_unreadable_runtimes=True)
    with S.open_store(str(data_dir)) as store:
        suspended = store.scope_state(S.OWNER_SCOPE).suspended
    conn = sqlite3.connect(f"file:{data_dir}/memory/{S.STORE_NAME}?mode=ro", uri=True)
    try:
        dump = "\n".join(conn.iterdump())
    finally:
        conn.close()
    return outcome, suspended, dump


def test_ordinary_c1_recovery_ignores_the_history_record(v23, target, tmp_path):
    data_dir, base = target
    plan, receipt = restore(v23, data_dir, base)
    record = json.loads((data_dir / receipt["c1"]["archived_c1_record"]["path"]).read_bytes())
    claimed_soul = record["provenance"]["claimed_source_soul_id"]
    claimed_incarnation = record["provenance"]["claimed_source_incarnation"]
    # A second restore of the same archive without the record, for comparison.
    other = tmp_path / "restore_parent" / "data_without"
    selection = dict(plan["selection"], **{ri.C1_UNIT: "skip"})
    restore(v23, other, base, selection=selection)
    with_record = _recovered_store_dump(data_dir)
    without_record = _recovered_store_dump(other)
    for outcome, suspended, dump in (with_record, without_record):
        assert outcome.suspended_scopes == (S.OWNER_SCOPE,) and outcome.unknown_scope_policy == "suspended"
        assert suspended is True
        assert claimed_soul not in dump and claimed_incarnation not in dump
    # The history record is still there, untouched, after Recover.
    assert sha((data_dir / receipt["c1"]["archived_c1_record"]["path"]).read_bytes()) \
        == receipt["c1"]["archived_c1_record"]["sha256"]
    assert with_record[0].suspended_scopes == without_record[0].suspended_scopes


# The folders that hold shipped Python (every non-test .py in the release
# tree lives in one of them or at the top level). Only these are scanned, so a
# checkout's own reports, handoffs, patches or frozen review copies (the dev
# mirror carries thousands) never count as product code (S3B-13).
PRODUCT_ROOTS = ("chrome_companion", "comms", "core", "eval", "scripts", "stt", "tools", "tts", "ui")


def _product_py():
    """Every Python file under the product roots, tracked or not (an
    untracked module would ship once committed), plus top-level modules.
    Walks the filesystem, so it needs no git (S3B-11)."""
    out = sorted(p for p in REPO.glob("*.py") if p.is_file())
    for top in PRODUCT_ROOTS:
        for root, dirs, files in os.walk(REPO / top):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__" and not d.startswith("."))
            out += [Path(root) / f for f in sorted(files) if f.endswith(".py")]
    return out


# Who may touch the restore engine or name its history (S3B-01): an allowlist.
_ENGINE = {"core/agent_backup_restore.py", "core/agent_backup_restore_intake.py",
           "core/agent_backup_restore_cockpit.py"}
_MAY_IMPORT_ENGINE = _ENGINE | {"ui/settings/restore_cockpit.py"}
_NAME_ALLOWLIST = {
    "agent_backup_restore": _MAY_IMPORT_ENGINE,
    "HISTORY_DIRNAME": {"core/agent_backup_restore.py", "core/agent_backup_restore_cockpit.py"},
    "C1_HISTORY_NAME": {"core/agent_backup_restore.py"},
    # The inventory names the folder only to classify it as known and never
    # captured by an export (pre-existing; it reads no content).
    "restore_history": _ENGINE | {"core/agent_backup_inventory.py"},
    "archived_c1_record": {"core/agent_backup_restore.py", "core/agent_backup_restore_intake.py"},
    # The export names the archive member path; nothing else names the file.
    "recovery_state.json": {"core/agent_backup_export.py", "core/agent_backup_restore.py"},
}


def _names_in(tree):
    """Every module name imported (absolute, relative or by importlib/__import__
    string), every identifier and attribute, and every string constant."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(f"{node.module or ''}.{a.name}" for a in node.names)
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names.add(node.value)
    return names


def _name_users(root, files):
    found = {token: set() for token in _NAME_ALLOWLIST}
    for path in files:
        rel = path.relative_to(root).as_posix()
        names = _names_in(ast.parse(path.read_text(encoding="utf-8")))
        for token in found:
            if any(token in name for name in names):
                found[token].add(rel)
    return found


def _violations(found):
    return {token: sorted(found[token] - allowed) for token, allowed in _NAME_ALLOWLIST.items()
            if found[token] - allowed}


def test_only_the_restore_engine_names_the_history_record():
    """No runtime, safety-enforcement, Wire or C1-recovery module imports the
    restore engine or names the history folder, the record or its receipt
    entry, however indirectly. C1-S2 (the reader that would bind receipt and
    trace) is not built."""
    files = _product_py()
    rels = {p.relative_to(REPO).as_posix() for p in files}
    assert {"core/suspension.py", "core/wire_seat_gate.py", "main.py"} <= rels and len(rels) > 100
    found = _name_users(REPO, files)
    assert _violations(found) == {}
    assert found["C1_HISTORY_NAME"] == {"core/agent_backup_restore.py"}
    assert '"restore_history": (UNCAPTURED_KNOWN' in (REPO / "core/agent_backup_inventory.py").read_text()


PROBES = [
    ("from core import agent_backup_restore as _ar\n"
     "def read(d):\n    return (d / _ar.HISTORY_DIRNAME).glob('*/c1/' + _ar.C1_HISTORY_NAME)\n",
     "C1_HISTORY_NAME"),
    ("import importlib\nm = importlib.import_module('core.agent_backup_restore')\n", "agent_backup_restore"),
    ("from .agent_backup_restore import _c1_history_accepted\n", "agent_backup_restore"),
    ("def f(r):\n    return r['c1']['archived_c1_record']\n", "archived_c1_record"),
    ("P = 'restore_history' + '/x'\n", "restore_history"),
]


@pytest.mark.parametrize("source,token", PROBES)
def test_the_pin_catches_an_indirect_reader(tmp_path, source, token):
    """The allowlist pin is not a literal grep over a few modules: any other
    module reaching the record, the engine or its constants is caught
    (S3B-01's own probe and its variants)."""
    probe = tmp_path / "core" / "suspension.py"   # a module that must never name them
    probe.parent.mkdir()
    probe.write_text(source)
    assert "core/suspension.py" in _violations(_name_users(tmp_path, [probe])).get(token, [])


def test_the_c1_s2_hand_off_is_carried_in_the_engine():
    """A-11's five C1-S2 requirements travel with the code that keeps the
    record (S3B-10); nothing implements them here."""
    text = (REPO / "core/agent_backup_restore.py").read_text(encoding="utf-8")
    for item in ("1. bind receipt and trace by (restore_id, archive_sha256), both ways;",
                 "2. fall back to the R-6 baseline if the receipt is missing, invalid or",
                 "3. when the trace and its reader change together, move the record's",
                 "4. re-run validate_recovery_state at Recover;",
                 "5. never copy archived strings into a live incident, audit detail or"):
        assert item in text, item


# ---------------------------------------------------------------------------
# Malformed, missing, mismatched, tampered (fail closed, nothing damaged)
# ---------------------------------------------------------------------------

def _alter_record_keep_observation(manifest, files):
    files[RECORD_PATH] = files[RECORD_PATH].replace(b'"version":1', b'"version":2', 1)


def _invalid_record_rebound(manifest, files):
    """Record bytes no longer a valid record, with the member hash and the
    observation's sha256 both updated: the schema binding holds, so only the
    payload check can catch it."""
    _alter_record_keep_observation(manifest, files)
    manifest["export_observations"]["c1_recovery"]["record_sha256"] = sha(files[RECORD_PATH])


def _drop_record_blob(manifest, files):
    del files[RECORD_PATH]


def _drop_record_entirely(manifest, files):
    del files[RECORD_PATH]
    manifest["members"] = [m for m in manifest["members"] if m["logical_id"] != ex.C1_MEMBER_ID]


def _relabel_2_2(manifest, files):
    manifest["format_version"] = {"major": 2, "minor": 2}


def _observation_incarnation(manifest, files):
    manifest["export_observations"]["c1_recovery"]["claimed_source_incarnation"] = "0" * 32


def _marker_missing(manifest, files):
    manifest["export_observations"]["installation_marker"] = "missing_unknown"


def _undeclared_c1_file(manifest, files):
    files["state/c1/extra.json"] = b"{}"


def _record_path_moved(manifest, files):
    files["state/c1/elsewhere.json"] = files.pop(RECORD_PATH)
    c1_member(manifest)["archive_path"] = "state/c1/elsewhere.json"


SELECTIVE_CASES = [
    # (name, mutate, rehash): the record member is damaged; other units are fine.
    ("record_invalid_rebound", _invalid_record_rebound, True),
    ("record_altered_hash_stale", _alter_record_keep_observation, False),
    ("record_blob_missing", _drop_record_blob, True),
    ("observation_mismatch", _observation_incarnation, True),
]
REJECTED_CASES = [
    ("record_member_absent", _drop_record_entirely, "MANIFEST_SCHEMA_INVALID"),
    # Member hash updated but not the observation's: the schema binds them.
    ("record_altered_hash_updated", _alter_record_keep_observation, "MANIFEST_SCHEMA_INVALID"),
    ("relabelled_2_2", _relabel_2_2, "MANIFEST_SCHEMA_INVALID"),
    ("verified_with_blocker", _marker_missing, "MANIFEST_SCHEMA_INVALID"),
    ("undeclared_c1_file", _undeclared_c1_file, "ARCHIVE_UNDECLARED_MEMBER"),
    ("record_path_moved", _record_path_moved, "MANIFEST_SCHEMA_INVALID"),
]


@pytest.mark.parametrize("name,mutate,rehash", SELECTIVE_CASES)
def test_a_damaged_record_is_never_verified_and_never_placed(v23, target, tmp_path, name, mutate, rehash):
    data_dir, base = target
    hostile = rebuild(v23, tmp_path / f"{name}.zip", mutate, rehash=rehash)
    before = hostile.read_bytes()
    intake = ri.intake_archive(str(hostile))
    assert intake["kind"] == ri.SELECTIVE and intake["format"] == ri.FORMAT_V23
    assert intake["coverage"]["format_coverage"] == "verified_agent_backup_failed_verification"
    assert intake["coverage"]["c1_safety_state"] == "record_unavailable_damaged"
    assert intake["coverage"]["coverage_blockers"] == ["archive_failed_verification"]
    assert "c1_record" not in intake["coverage"]
    unit = c1_unit(intake["units"])
    assert unit["availability"] == "unavailable" and unit["recoverable"] == []
    with pytest.raises(ar.RestoreRefused) as info:
        ar.plan_restore(hostile, data_dir, base)
    assert info.value.code == "SELECTION_REQUIRED"
    with pytest.raises(ar.RestoreRefused) as info:
        ar.plan_restore(hostile, data_dir, base, selection={ri.C1_UNIT: "recover"})
    assert info.value.code == "SELECTION_INVALID"
    plan, receipt = restore(hostile, data_dir, base, selection={"primary_database": "recover"})
    assert plan["restore_class"] == ar.SELECTIVE
    assert plan["verified_agent_backup"] == {"available": False, "reason": "archive_failed_verification"}
    assert receipt["c1"]["archived_c1_record"] == {"status": "not_restored_record_unavailable"}
    assert not (data_dir / "restore_history" / plan["restore_id"] / "c1").exists()
    assert ar.verify_restored_root(data_dir, receipt)["passed"] is True
    assert hostile.read_bytes() == before


@pytest.mark.parametrize("name,mutate,code", REJECTED_CASES)
def test_a_malformed_2_3_archive_is_rejected_before_any_write(v23, target, tmp_path, name, mutate, code):
    data_dir, base = target
    hostile = rebuild(v23, tmp_path / f"{name}.zip", mutate)
    before, base_before = hostile.read_bytes(), snapshot(base)
    intake = ri.intake_archive(str(hostile))
    assert intake["kind"] == ri.REJECTED
    assert intake["refusal"]["code"] == code
    with pytest.raises(ar.RestoreRefused) as info:
        ar.plan_restore(hostile, data_dir, base)
    assert info.value.code == "ARCHIVE_REJECTED"
    assert not data_dir.exists() and leftovers(data_dir) == []
    assert hostile.read_bytes() == before and snapshot(base) == base_before


def test_identical_non_c1_damage_gives_the_same_intake_on_2_2_and_2_3(vsource, tmp_path):
    """The intake-level twin of S3a's differential test: damage that is not
    about C1 classifies the same way in both formats."""
    verified(vsource, tmp_path / "v23.zip")
    incomplete(vsource, tmp_path / "v22.zip")

    def damage_db(manifest, files):
        files["state/databases/lumina.db"] = files["state/databases/lumina.db"][:-1] + b"\x01"

    out = {}
    for label in ("v22", "v23"):
        hostile = rebuild(tmp_path / f"{label}.zip", tmp_path / f"{label}-bad.zip", damage_db, rehash=False)
        intake = ri.intake_archive(str(hostile))
        out[label] = (intake["kind"], sorted((m["logical_id"], m["damage"]) for m in intake["members"]
                                             if m["state"] != "valid"))
    assert out["v22"] == out["v23"] == (ri.SELECTIVE, [("state.databases.lumina_db", "hash_mismatch")])


@pytest.mark.parametrize("swap,code", [("malformed", "ARCHIVE_REJECTED"), ("another_valid_2_3", "PLAN_CHANGED")])
def test_an_archive_changed_after_the_plan_is_refused(v23, vsource, target, tmp_path, swap, code):
    data_dir, base = target
    plan = ar.plan_restore(v23, data_dir, base)
    if swap == "malformed":
        rebuild(v23, tmp_path / "swap.zip", _alter_record_keep_observation)
    else:
        verified(vsource, tmp_path / "swap.zip")          # same source, new bytes
        assert ri.intake_archive(str(tmp_path / "swap.zip"))["kind"] == ri.VERIFIED
    assert (tmp_path / "swap.zip").read_bytes() != v23.read_bytes()
    shutil.copyfile(tmp_path / "swap.zip", v23)
    base_before = snapshot(base)
    with pytest.raises(ar.RestoreRefused) as info:
        ar.restore_isolated(plan, v23, approved_plan_digest=plan["plan_digest"])
    assert info.value.code == code
    assert not data_dir.exists() and leftovers(data_dir) == [] and snapshot(base) == base_before


def test_a_staged_record_changed_before_validation_rolls_back(v23, target, monkeypatch):
    data_dir, base = target
    base_before, source_before = snapshot(base), v23.read_bytes()
    plan = ar.plan_restore(v23, data_dir, base)

    def tamper(stage):
        if stage == "data_staged":
            (staging,) = [p for p in data_dir.parent.iterdir() if p.name.endswith(".staging")]
            path = staging / "restore_history" / plan["restore_id"] / "c1" / "recovery_state.json"
            path.write_bytes(path.read_bytes() + b" ")

    monkeypatch.setattr(ar, "_checkpoint", tamper)
    with pytest.raises(ar.RestoreRefused) as info:
        ar.restore_isolated(plan, v23, approved_plan_digest=plan["plan_digest"])
    assert info.value.code == "STAGED_VALIDATION_FAILED"
    assert info.value.rollback["destination_untouched"] is True and info.value.rollback["staging_removed"]
    assert not data_dir.exists() and leftovers(data_dir) == []
    assert snapshot(base) == base_before and v23.read_bytes() == source_before


@pytest.mark.parametrize("fault", ["invalid", "other_record"])
def test_the_record_is_revalidated_at_staging(v23, target, monkeypatch, fault):
    data_dir, base = target
    plan = ar.plan_restore(v23, data_dir, base)
    real = ex.c1_observation_from_bytes

    def lying(raw):
        if fault == "invalid":
            raise ex.C1RecordInvalid("test")
        return dict(real(raw), record_sha256="0" * 64)

    def inject(stage):
        # After the plan is re-derived and the members re-verified, so only
        # the staging re-validation stands between the record and the root.
        if stage == "members_verified":
            monkeypatch.setattr(ex, "c1_observation_from_bytes", lying)

    monkeypatch.setattr(ar, "_checkpoint", inject)
    with pytest.raises(ar.RestoreRefused) as info:
        ar.restore_isolated(plan, v23, approved_plan_digest=plan["plan_digest"])
    assert info.value.code == "MEMBER_INVALID"
    assert info.value.rollback["destination_untouched"] is True
    assert not data_dir.exists() and leftovers(data_dir) == []


def test_the_staging_check_is_what_refuses_a_mismatched_record(v23, target):
    """Direct: the staging re-validation itself (independent of the plan
    re-derivation) refuses a record that is not the declared one."""
    data_dir, base = target
    plan = ar.plan_restore(v23, data_dir, base)
    record = archive_bytes(v23)[RECORD_PATH]
    assert ar._c1_history_note(record, plan)["record_sha256"] == sha(record)
    bad_plan = json.loads(json.dumps(plan))
    bad_plan["coverage"]["c1_record"]["sha256"] = "0" * 64
    with pytest.raises(ar.RestoreRefused) as info:
        ar._c1_history_note(record, bad_plan)
    assert info.value.code == "MEMBER_INVALID"
    with pytest.raises(ar.RestoreRefused) as info:
        ar._c1_history_note(record + b" ", plan)
    assert info.value.code == "MEMBER_INVALID"


def test_acceptance_fails_when_the_kept_record_changes(v23, target):
    data_dir, base = target
    _plan, receipt = restore(v23, data_dir, base)
    path = data_dir / receipt["c1"]["archived_c1_record"]["path"]
    path.write_bytes(path.read_bytes() + b" ")
    checks = ar.verify_restored_root(data_dir, receipt)["checks"]
    assert checks["c1_history_record"] is False and checks["data_files_match"] is False
    path.unlink()
    assert ar.verify_restored_root(data_dir, receipt)["checks"]["c1_history_record"] is False
    moved = json.loads(json.dumps(receipt))
    moved["c1"]["archived_c1_record"]["path"] = "memory/recovery_state.json"
    assert ar._c1_history_accepted(data_dir, moved) is False


def test_acceptance_needs_the_exact_record_at_the_exact_place(v23, target):
    data_dir, base = target
    _plan, receipt = restore(v23, data_dir, base)
    kept = data_dir / receipt["c1"]["archived_c1_record"]["path"]
    original = kept.read_bytes()
    # The same valid bytes somewhere else in the root are not the kept record.
    elsewhere = data_dir / "restore_history" / receipt["restore_id"] / "recovery_state.json"
    elsewhere.write_bytes(original)
    moved = json.loads(json.dumps(receipt))
    moved["c1"]["archived_c1_record"]["path"] = str(elsewhere.relative_to(data_dir))
    assert ar._c1_history_accepted(data_dir, moved) is False
    # Another valid record at the right place is not the recorded one.
    golden = (REPO / "tests/fixtures/c1_recovery_state_v1_golden.json").read_bytes()
    assert ex.c1_observation_from_bytes(golden)["record_sha256"] != sha(original)
    kept.write_bytes(golden)
    assert ar._c1_history_accepted(data_dir, receipt) is False
    kept.write_bytes(original)
    assert ar._c1_history_accepted(data_dir, receipt) is True


def test_every_2_3_receipt_is_checked_for_its_record(v23, target):
    """S3B-03: the format decides that the history check runs, not the
    receipt's own key; and a record said not to be placed must be absent."""
    data_dir, base = target
    plan, receipt = restore(v23, data_dir, base)
    keyless = json.loads(json.dumps(receipt))
    del keyless["c1"]["archived_c1_record"]
    assert ar.verify_restored_root(data_dir, keyless)["checks"]["c1_history_record"] is False
    claims_skipped = json.loads(json.dumps(receipt))
    claims_skipped["c1"]["archived_c1_record"] = {"status": "not_restored_owner_skipped"}
    assert ar._c1_history_accepted(data_dir, claims_skipped) is False   # but the record is there
    unknown = json.loads(json.dumps(receipt))
    unknown["c1"]["archived_c1_record"] = {"status": "something_else"}
    assert ar._c1_history_accepted(data_dir, unknown) is False


def test_a_skipped_record_leaves_the_history_path_empty(v23, target):
    data_dir, base = target
    intake = ri.intake_archive(str(v23))
    selection = {u["unit"]: "recover" for u in intake["units"]
                 if u["availability"] != "unavailable" and u["disposition"] != "informational"}
    selection[ri.C1_UNIT] = "skip"
    plan, receipt = restore(v23, data_dir, base, selection=selection)
    assert ar._c1_history_accepted(data_dir, receipt) is True
    stray = data_dir / "restore_history" / plan["restore_id"] / "c1"
    stray.mkdir()                     # even an empty c1/ folder contradicts "not placed" (S3B-14)
    # R5 (TS3B-01): a receipt that says the owner skipped the record is checked under its own name.
    assert ar.verify_restored_root(data_dir, receipt)["checks"]["c1_history_skipped_by_owner"] is False
    (stray / "recovery_state.json").write_bytes(archive_bytes(v23)[RECORD_PATH])
    assert ar.verify_restored_root(data_dir, receipt)["checks"]["c1_history_skipped_by_owner"] is False


def _damage_db(manifest, files):
    files["state/databases/lumina.db"] = files["state/databases/lumina.db"][:-1] + b"\x01"


def test_an_intact_record_in_a_failed_archive_is_history_and_says_so(v23, target, tmp_path):
    """S3B-06, decided: the record validated on its own (strict parse,
    validator, canonical bytes, equal to the declared observation), so the
    owner may keep it as inactive history; every label says the archive
    failed verification."""
    data_dir, base = target
    hostile = rebuild(v23, tmp_path / "db_damaged.zip", _damage_db, rehash=False)
    intake = ri.intake_archive(str(hostile))
    assert intake["kind"] == ri.SELECTIVE
    cov = intake["coverage"]
    assert cov["format_coverage"] == "verified_agent_backup_failed_verification"
    assert cov["c1_safety_state"] == "record_intact_in_unverified_archive_history_only"
    assert cov["coverage_blockers"] == ["archive_failed_verification"]
    assert c1_unit(intake["units"])["availability"] == "complete"
    text = cp.render_intake_text(cp.summarize_intake(intake))
    text_lint(text)
    assert "checks out on its own, but the archive FAILED verification" in text
    assert "only the sanitized record was captured" not in text
    plan, receipt = restore(hostile, data_dir, base, selection={ri.C1_UNIT: "recover"})
    assert plan["restore_class"] == ar.SELECTIVE
    assert plan["verified_agent_backup"] == ar.VERIFIED_AGENT_BACKUP_V23_FAILED
    kept = receipt["c1"]["archived_c1_record"]
    assert kept["status"] == "kept_as_inactive_history" and kept["archive_verified"] is False
    assert kept["sha256"] == sha(archive_bytes(hostile)[RECORD_PATH])
    assert ar.verify_restored_root(data_dir, receipt)["passed"] is True
    assert S.probe(data_dir).status is S.StoreStatus.ABSENT_WITH_TRACE


@pytest.fixture
def pre23_archives(vsource, tmp_path, monkeypatch):
    """v1, 2.2 and Legacy archives of the same synthetic source."""
    import core.agent_backup as ab
    from core import memory_backup
    data, base, _ = vsource
    out = tmp_path / "pre23"
    out.mkdir()
    incomplete(vsource, out / "v22.zip")
    ab.build_agent_backup(str(data), str(base), str(out / "v1.zip"),
                          identity_trailers_path=NO_IDENTITY_TRAILERS, credentials_path=NO_CREDENTIALS)

    class _Conn:
        def execute(self, *_a):
            return self

        def close(self):
            pass

    with sqlite3.connect(data / "memory/lumina.db") as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    monkeypatch.setattr(memory_backup, "_db", lambda: _Conn())
    memory_backup.build_memory_backup(str(data), str(out / "legacy.zip"))
    monkeypatch.undo()
    return out


# S3B-08: what each older format says, pinned value for value.
PRE23 = {
    "v1.zip": {"format": "agent_backup_v1", "kind": ri.VERIFIED, "class": ar.INCOMPLETE,
               "coverage": {"format_coverage": "older_full_mode", "c1_safety_state": "not_captured",
                            "c1_at_source": "not_recorded"}},
    "v22.zip": {"format": "agent_export_v2.2", "kind": ri.VERIFIED, "class": ar.INCOMPLETE,
                "coverage": {"format_coverage": "incomplete_state_export", "c1_safety_state": "not_captured",
                             "c1_at_source": "present_excluded"}},
    "legacy.zip": {"format": "legacy_memory_backup", "kind": ri.LEGACY, "class": ar.LEGACY,
                   "coverage": {"format_coverage": "legacy_data_folder_only",
                                "c1_safety_state": "evidence_only"}},
}
NEW_23_KEYS = {"coverage_blockers", "c1_record"}


@pytest.mark.parametrize("name", sorted(PRE23))
def test_older_formats_keep_their_values(pre23_archives, target, name):
    want = PRE23[name]
    archive = pre23_archives / name
    intake = ri.intake_archive(str(archive))
    assert (intake["format"], intake["kind"]) == (want["format"], want["kind"])
    assert {k: intake["coverage"].get(k) for k in want["coverage"]} == want["coverage"]
    assert not NEW_23_KEYS & set(intake["coverage"])
    assert not any(u["unit"] == ri.C1_UNIT for u in intake["units"])
    preview = ri.preview_recovery(intake, None)
    assert set(preview["safety"]) == {"c1", "archived_c1_state", "always_excluded", "executable_authority",
                                      "telegram_remote_access", "quiescence"}
    data_dir, base = target
    plan, receipt = restore(archive, data_dir, base)
    assert plan["restore_class"] == want["class"]
    assert plan["verified_agent_backup"] == {"available": False, "reason": "c1_snapshot_contract_absent"}
    assert set(plan["safety"]) == {"c1", "archived_c1_state", "historical_inactive_location", "always_excluded",
                                   "telegram_remote_access", "official_skill_trust"}
    assert set(receipt["c1"]) == {"trace", "archived_c1_state", "required_next_step", "first_run_initialize"}
    # S3B-09: the intake kind is not the class. A verified 2.2 archive's kind
    # reads "verified_agent_backup" (pre-existing, digest-bound); its class
    # and its verified_agent_backup value say what it is.
    if want["kind"] == ri.VERIFIED:
        assert plan["archive"]["kind"] == "verified_agent_backup" != plan["restore_class"]
    checks = ar.verify_restored_root(data_dir, receipt)["checks"]
    assert "c1_history_record" not in checks and all(checks.values())


# ---------------------------------------------------------------------------
# Interrupted restores
# ---------------------------------------------------------------------------

def _killed_restore(archive, data_dir, base, stage, tmp_path):
    script = tmp_path / f"kill_{stage}.py"
    script.write_text(textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {str(REPO)!r})
        import core.agent_backup_restore as ar
        def die(stage):
            if stage == {stage!r}:
                os._exit(77)
        ar._checkpoint = die
        plan = ar.plan_restore({str(archive)!r}, {str(data_dir)!r}, {str(base)!r})
        ar.restore_isolated(plan, {str(archive)!r}, approved_plan_digest=plan["plan_digest"])
        os._exit(0)
    """))
    env = dict(os.environ, LUMINA_TESTING="1")
    run = subprocess.run([sys.executable, str(script)], cwd=REPO, env=env, capture_output=True, text=True,
                         timeout=120)
    assert run.returncode == 77, run.stderr[-2000:]


@pytest.mark.parametrize("stage", ["members_verified", "data_staged", "generation_verified",
                                   "staged_validated", "before_publish"])
def test_an_interrupted_2_3_restore_leaves_nothing_published(stage, v23, target, tmp_path):
    data_dir, base = target
    base_before, source_before = snapshot(base), v23.read_bytes()
    _killed_restore(v23, data_dir, base, stage, tmp_path)
    assert not data_dir.exists() and leftovers(data_dir)
    report = ar.recover_interrupted_restore(data_dir)
    assert len(report["rolled_back"]) == 1 and report["left_alone"] == []
    assert report["install_files_left"] == [] and report["install_paths_preserved"] == []
    assert leftovers(data_dir) == [] and snapshot(base) == base_before and v23.read_bytes() == source_before
    _plan, receipt = restore(v23, data_dir, base)
    assert ar.verify_restored_root(data_dir, receipt)["passed"] is True


def test_a_2_3_restore_killed_after_publishing_is_complete(v23, target, tmp_path):
    data_dir, base = target
    _killed_restore(v23, data_dir, base, "published", tmp_path)
    restore_id = next((data_dir / "restore_history").iterdir()).name
    receipt = json.loads((data_dir / "restore_history" / restore_id / "receipt.json").read_text())
    assert receipt["c1"]["archived_c1_record"]["status"] == "kept_as_inactive_history"
    assert ar.verify_restored_root(data_dir, receipt)["passed"] is True


# ---------------------------------------------------------------------------
# Owner words (cockpit) for 2.3
# ---------------------------------------------------------------------------

def test_cockpit_words_for_a_verified_2_3_archive(v23, target):
    data_dir, base = target
    intake = ri.intake_archive(str(v23))
    view = cp.summarize_intake(intake)
    assert view["restore_class"] == ar.VERIFIED and view["class_label"] == "Verified Agent Backup recovery"
    text = cp.render_intake_text(view)
    text_lint(text)
    assert "never restored as live safety state" in text and "NOT PROVEN" in text
    assert "Verified Agent Backup is unavailable" not in text
    assert "C1 safety state is not captured" not in text
    plan = ar.plan_restore(v23, data_dir, base)
    plan_text = cp.render_plan_text(plan)
    text_lint(plan_text)
    assert "Verified Agent Backup: verified archive, not a proven recovery." in plan_text
    assert cp.BANNER.endswith("never proof of a full recovery.")
    assert "unavailable (C1 snapshot contract absent)" not in plan_text
    receipt = ar.restore_isolated(plan, v23, approved_plan_digest=plan["plan_digest"])
    checks = cp.describe_checks(ar.verify_restored_root(data_dir, receipt))
    assert "[PASS] The C1 safety record is kept only as inactive history, unchanged and still valid" in checks


def test_cockpit_words_for_a_failed_2_3_archive(v23, tmp_path):
    hostile = rebuild(v23, tmp_path / "bad.zip", _invalid_record_rebound)
    intake = ri.intake_archive(str(hostile))
    assert intake["kind"] == ri.SELECTIVE
    text = cp.render_intake_text(cp.summarize_intake(intake))
    text_lint(text)
    assert "FAILED verification, so it is not a Verified Agent Backup" in text
    assert "record is damaged and is not recovered" in text
    assert "only the sanitized record was captured" not in text           # S3B-04
    assert cp._verified_text(ar.VERIFIED_AGENT_BACKUP_V23_FAILED).startswith("Verified Agent Backup: no")


# ---------------------------------------------------------------------------
# R5 (Tech review TS3B-01..03): the owner's words when no record was kept,
# receipt shapes the acceptance check must tolerate, and no stale claim that a
# recovery "cannot produce" a Verified Agent Backup.
# ---------------------------------------------------------------------------

KEPT_LABEL = "The C1 safety record is kept only as inactive history, unchanged and still valid"
SKIPPED_LABEL = "No C1 history record was kept, as you chose; none is present"
DAMAGED_LABEL = "No C1 history record was kept (the archive's record was damaged); none is present"
NOT_KEPT_CHECKS = ("c1_history_skipped_by_owner", "c1_history_unavailable_as_recorded")


def _recover_through_the_cockpit(archive, data_dir, base, selection=None):
    plan = ar.plan_restore(str(archive), data_dir, base, selection=selection)
    result = cp.execute_recovery(plan, str(archive), plan["plan_digest"])
    assert result["ok"] is True, result
    return plan, result


def test_the_verification_list_says_no_record_was_kept_when_the_owner_skipped_it(v23, target):
    """TS3B-01(a): the check passes (absence is as recorded), and the line says what happened."""
    data_dir, base = target
    plan, result = _recover_through_the_cockpit(v23, data_dir, base, selection={ri.C1_UNIT: "skip"})
    assert result["receipt"]["c1"]["archived_c1_record"] == {"status": "not_restored_owner_skipped"}
    checks = result["verification"]["checks"]
    assert checks["c1_history_skipped_by_owner"] is True and "c1_history_record" not in checks
    text = cp.describe_success(result)
    text_lint(text)
    assert f"[PASS] {SKIPPED_LABEL}" in text
    assert KEPT_LABEL not in text


@pytest.mark.parametrize("name,mutate,rehash", SELECTIVE_CASES)
def test_the_verification_list_says_no_record_was_kept_when_the_archives_record_was_damaged(
        v23, target, tmp_path, name, mutate, rehash):
    """TS3B-01(b): a damaged record is never placed, so the list must not say it was kept."""
    data_dir, base = target
    hostile = rebuild(v23, tmp_path / f"{name}.zip", mutate, rehash=rehash)
    plan, result = _recover_through_the_cockpit(hostile, data_dir, base, selection={"primary_database": "recover"})
    assert result["receipt"]["c1"]["archived_c1_record"] == {"status": "not_restored_record_unavailable"}
    checks = result["verification"]["checks"]
    assert checks["c1_history_unavailable_as_recorded"] is True and "c1_history_record" not in checks
    text = cp.describe_success(result)
    text_lint(text)
    assert f"[PASS] {DAMAGED_LABEL}" in text
    assert KEPT_LABEL not in text


def test_a_kept_record_keeps_its_verification_words(v23, target):
    data_dir, base = target
    plan, result = _recover_through_the_cockpit(v23, data_dir, base)
    assert result["receipt"]["c1"]["archived_c1_record"]["status"] == "kept_as_inactive_history"
    checks = result["verification"]["checks"]
    assert checks["c1_history_record"] is True and not any(k in checks for k in NOT_KEPT_CHECKS)
    text = cp.describe_success(result)
    assert f"[PASS] {KEPT_LABEL}" in text
    assert SKIPPED_LABEL not in text and DAMAGED_LABEL not in text


def test_an_intact_record_kept_from_a_failed_archive_keeps_its_verification_words(v23, target, tmp_path):
    data_dir, base = target
    hostile = rebuild(v23, tmp_path / "db_damaged.zip", _damage_db, rehash=False)
    plan, result = _recover_through_the_cockpit(hostile, data_dir, base, selection={ri.C1_UNIT: "recover"})
    assert result["receipt"]["c1"]["archived_c1_record"]["status"] == "kept_as_inactive_history"
    assert result["verification"]["checks"]["c1_history_record"] is True
    assert f"[PASS] {KEPT_LABEL}" in cp.describe_success(result)


@pytest.mark.parametrize("claim,check", [
    ("not_restored_owner_skipped", "c1_history_skipped_by_owner"),
    ("not_restored_record_unavailable", "c1_history_unavailable_as_recorded"),
])
def test_a_receipt_claiming_no_record_while_one_is_present_reads_as_a_failure(v23, target, claim, check):
    """The not-kept names are only as good as the absence they check."""
    data_dir, base = target
    plan, receipt = restore(v23, data_dir, base)
    claims_none = json.loads(json.dumps(receipt))
    claims_none["c1"]["archived_c1_record"] = {"status": claim}
    verification = ar.verify_restored_root(data_dir, claims_none)
    assert verification["checks"][check] is False and "c1_history_record" not in verification["checks"]
    assert verification["passed"] is False
    label = SKIPPED_LABEL if claim == "not_restored_owner_skipped" else DAMAGED_LABEL
    assert f"[FAIL] {label}" in cp.describe_checks(verification)


def test_every_check_name_the_engine_can_emit_for_the_record_has_owner_words():
    names = set(ar._C1_NOT_PLACED_CHECK.values()) | {"c1_history_record"}
    lines = cp.describe_checks({"checks": {name: True for name in names}})
    assert len(lines) == 3 and all(line.startswith("[PASS] ") for line in lines)
    assert not any("unrecognised" in line.lower() or name in line for line in lines for name in names)


@pytest.mark.parametrize("label,mutate,check_must_be_false", [
    ("no_c1_key", lambda r: r.pop("c1"), True),
    ("c1_none", lambda r: r.__setitem__("c1", None), True),
    ("c1_string", lambda r: r.__setitem__("c1", "kept"), True),
    ("c1_list", lambda r: r.__setitem__("c1", []), True),
    ("record_none", lambda r: r["c1"].__setitem__("archived_c1_record", None), True),
    ("record_string", lambda r: r["c1"].__setitem__("archived_c1_record", "kept"), True),
    ("status_not_a_string", lambda r: r["c1"]["archived_c1_record"].__setitem__("status", ["kept"]), True),
    ("archive_string", lambda r: r.__setitem__("archive", "agent_export_v2.3"), False),
])
def test_a_malformed_2_3_receipt_fails_acceptance_without_raising(v23, target, label, mutate, check_must_be_false):
    """TS3B-03: the history check returns False for a receipt it cannot read; it never raises."""
    data_dir, base = target
    plan, receipt = restore(v23, data_dir, base)
    mangled = json.loads(json.dumps(receipt))
    mutate(mangled)
    result = ar.verify_restored_root(data_dir, mangled)
    assert result["passed"] is False
    if check_must_be_false:
        assert result["checks"]["c1_history_record"] is False


@pytest.mark.parametrize("label,mutate", [
    ("no_c1_key", lambda r: r.pop("c1")),
    ("c1_none", lambda r: r.__setitem__("c1", None)),
    ("c1_string", lambda r: r.__setitem__("c1", "kept")),
])
def test_a_malformed_pre_2_3_receipt_does_not_gain_a_history_check_or_raise(vsource, target, tmp_path, label, mutate):
    """The 2.3 history check must not change how a 2.2 receipt behaves: base returns a failed
    check for a receipt with a missing or null c1 block, and so must this."""
    data_dir, base = target
    incomplete(vsource, tmp_path / "incomplete.zip")
    plan, receipt = restore(tmp_path / "incomplete.zip", data_dir, base)
    assert "c1_history_record" not in ar.verify_restored_root(data_dir, receipt)["checks"]
    mangled = json.loads(json.dumps(receipt))
    mutate(mangled)
    result = ar.verify_restored_root(data_dir, mangled)
    assert result["passed"] is False
    assert not any(k.startswith("c1_history") for k in result["checks"])


# A recovery of a verified format 2.3 archive IS a Verified Agent Backup class (C1-S3b), so no
# shipped text may say a recovery cannot produce one. Conditional texts about older formats or a
# failed archive ("this format cannot carry", "FAILED verification, so it is not one") do not match.
STALE_RESTORE_CLAIM = re.compile(
    r"(?i)\b(?:cannot|can not|never|does not|doesn't)\s+(?:produce|produces|yield|yields)\s+(?:a|an)\s+verified")


def _stale_restore_claims(root, files):
    hits = []
    for path in files:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and STALE_RESTORE_CLAIM.search(node.value):
                hits.append((path.relative_to(root).as_posix(), node.lineno))
    return hits


def test_no_shipped_text_says_a_recovery_cannot_produce_a_verified_agent_backup():
    """TS3B-02: the Settings tooltip said so after S3b made it false. Every string constant
    in the product roots is scanned (an implicit concatenation is one constant)."""
    files = _product_py()
    assert {"ui/settings/memory_tab.py", "core/agent_backup_restore_cockpit.py"} <= {
        p.relative_to(REPO).as_posix() for p in files}
    assert _stale_restore_claims(REPO, files) == []


def test_the_stale_claim_scan_catches_a_planted_string_and_a_split_literal(tmp_path):
    probe = tmp_path / "ui" / "x.py"
    probe.parent.mkdir()
    probe.write_text('A = "Recovery never produces a Verified Agent Backup."\n'
                     'B = ("it does not "\n     "produce a Verified "\n     "Agent Backup.")\n'
                     'C = "Verified Agent Backup is unavailable: this archive format cannot carry a snapshot."\n')
    assert [line for _rel, line in _stale_restore_claims(tmp_path, [probe])] == [1, 2]
