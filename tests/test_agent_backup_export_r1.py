"""R1 policy regressions. All source history and archives are synthetic."""
import io
import json
import zipfile

import pytest

from core import agent_backup as ab
from core import agent_backup_export as ex
from test_agent_backup_verified_export_01 import source, build_review, read_zip


def rewritten(source, change, *, padding=0):
    payloads = read_zip(source[2])
    manifest = json.loads(payloads["manifest.json"])
    change(manifest)
    payloads["manifest.json"] = json.dumps(manifest).encode() + b" " * padding
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as z:
        for name, raw in payloads.items():
            z.writestr(ab._make_v1_zipinfo(name), raw)
    source[2].write_bytes(output.getvalue())
    return manifest


@pytest.mark.parametrize("logical_id,field", [
    ("state.databases.lumina_db", "owner_authorized"),
    ("state.preferences.prefs_json", "auto_execute"),
    ("state.telemetry.flight_recorder_db", "destination_authority"),
])
def test_unversioned_member_authority_fields_are_rejected(source, logical_id, field):
    manifest, _ = build_review(source)
    member = next(m for m in manifest["members"] if m["logical_id"] == logical_id)
    member[field] = True
    assert ex.validate_export_schema(manifest)


def extension(data=None):
    return {"version": 1, "records": {"example.future": {
        "version": 3, "data": data or {"deleted": True, "owner_authorized": True}}}}


def test_versioned_unknown_history_is_retained_without_current_authority(source):
    build_review(source)
    def change(m):
        m["extensions"] = extension()
        for member in m["members"]:
            member["extensions"] = extension()
    manifest = rewritten(source, change)
    result = ex.verify_export(str(source[2]))
    assert result["valid"], result["errors"]
    assert result["manifest"] == manifest
    assert result["manifest"]["recovery_contract"]["authority"] == "historical_inactive"


@pytest.mark.parametrize("bad", [
    {"version": True, "records": {}},
    {"version": 1, "records": {"unscoped": {"version": 1, "data": {}}}},
    extension("x" * (256 * 1024 + 1)),
    extension([[[[[[[[[[[[[[[[[0]]]]]]]]]]]]]]]]]),
])
def test_extension_limits_and_grammar_are_enforced(source, bad):
    manifest, _ = build_review(source)
    manifest["extensions"] = bad
    assert ex.validate_export_schema(manifest)


def test_future_pack_and_tombstone_survive_without_official_promotion(source):
    records = {"lumina-self-knowledge": {"version": 1, "installed_at": "2026-10-20"},
               "owner-created-pack": {"version": 7, "installed_at": "2028-01-01",
                    "deleted_at": "2028-02-01", "future": {"origin": "owner"}},
               "unfamiliar-official-name": {"schema_version": 3, "deleted": True}}
    (source[0] / "knowledge_packs_installed.json").write_text(json.dumps(records))
    manifest, receipt = build_review(source)
    marker = json.loads(read_zip(source[2])[ex.MARKER_PATH])
    assert marker["records"] == records
    assert receipt["full_agent_coverage"] is False
    assert manifest["recovery_contract"]["authority"] == "historical_inactive"


def test_classifier_and_verifier_accept_same_legal_large_manifest(source):
    build_review(source)
    rewritten(source, lambda _: None, padding=5 * 1024 * 1024)
    assert ex.verify_export(str(source[2]))["valid"]
    assert ex.classify_input(str(source[2]))["kind"] == "agent_export_v2"


def test_shared_resource_limit_refuses_before_any_decompression(source, monkeypatch):
    build_review(source)
    monkeypatch.setattr(ab, "_determine_verification_payload_budget", lambda **_: 1)
    monkeypatch.setattr(zipfile.ZipFile, "read", lambda *a, **kw: pytest.fail("payload decompressed"))
    with pytest.raises(ab.AgentBackupError, match="RESOURCE_LIMIT"):
        ex.classify_input(str(source[2]))
    assert not ex.verify_export(str(source[2]))["valid"]


def test_classification_is_explicitly_unverified_even_for_spoofed_manifest(source):
    build_review(source)
    rewritten(source, lambda m: m["members"].clear())
    assert ex.classify_input(str(source[2]))["verification"] == "not_performed"
    assert not ex.verify_export(str(source[2]))["valid"]


def test_core_rebind_policy_cannot_be_added_to_telemetry(source):
    manifest, _ = build_review(source)
    member = next(m for m in manifest["members"] if m["logical_id"] == "state.telemetry.flight_recorder_db")
    member["rebind_policy"] = "owner_authorized"
    assert ex.validate_export_schema(manifest)


def test_receipt_cannot_claim_independent_authentication_from_self_hashes(source):
    manifest, receipt = build_review(source)
    assert receipt["historical_claims"]["independent_provenance"] == "not_established_by_archive_hashes"
    receipt["historical_claims"]["independent_provenance"] = "authenticated"
    with pytest.raises(ab.AgentBackupError, match="COVERAGE_MISMATCH"):
        ex.confirm_export_result(str(source[2]), manifest, receipt)


def test_fresh_install_needs_owner_designation_not_destination_history(source):
    manifest, _ = build_review(source)
    plan = ex.historical_recovery_contract(manifest, owner_designated=True)
    assert plan["history_eligible"] is True
    assert plan["destination_history_required"] is False
    assert plan["current_authority"] == "inactive"
    assert plan["c1_recovery_fence_required"] is True
    assert plan["recovery_implemented"] is False
    assert ex.historical_recovery_contract(manifest, owner_designated=False)["history_eligible"] is False


def test_unknown_required_feature_preserves_history_but_blocks_activation(source):
    manifest, _ = build_review(source)
    manifest["required_features"] = ["example.future-runtime"]
    assert not ex.validate_export_schema(manifest)
    plan = ex.historical_recovery_contract(manifest, owner_designated=True)
    assert plan["history_eligible"] is True
    assert plan["unsupported_required_features"] == ["example.future-runtime"]
    assert plan["activation_eligible"] is False


def test_v20_remains_structurally_readable_without_extensions(source):
    build_review(source)
    def downgrade(m):
        m["format_version"] = {"major": 2, "minor": 0}
        for key in ("min_supported_format_version", "max_known_format_version"):
            m["compatibility"][key] = dict(m["format_version"])
        for key in ("extensions", "required_features", "historical_claims"):
            m.pop(key, None)
    rewritten(source, downgrade)
    assert ex.verify_export(str(source[2]))["valid"]

