"""Lumina R2 N1 exact archive reproductions and historical compatibility."""
import copy

import pytest

from core import agent_backup as ab
from core import agent_backup_export as ex
from test_agent_backup_export_r1 import rewritten, extension
from test_agent_backup_verified_export_01 import source, build_review, NO_CREDENTIALS, NO_IDENTITY_TRAILERS


@pytest.mark.parametrize("array,field", [
    ("regenerate", "owner_authorized"), ("exclusions", "auto_approve"),
])
def test_lumina_exact_n1_reproductions_refused(source, array, field):
    build_review(source)
    def mutate(manifest):
        # Exact N1 mutations in Lumina's independent R2 probes.
        manifest[array][0][field] = True
    rewritten(source, mutate)
    result = ex.verify_export(str(source[2]))
    assert not result["valid"], result
    assert "EXPORT_CONTROL_RECORD_FIELDS_UNSUPPORTED" in result["errors"]


@pytest.mark.parametrize("array", ["regenerate", "exclusions"])
def test_control_extensions_retain_unknown_history_without_authority(source, array):
    build_review(source)
    def mutate(manifest):
        manifest[array][0]["extensions"] = extension({"auto_approve": True, "future": "tombstone"})
    expected = rewritten(source, mutate)
    result = ex.verify_export(str(source[2]))
    assert result["valid"], result["errors"]
    assert result["manifest"][array] == expected[array]
    plan = ex.historical_recovery_contract(result["manifest"], owner_designated=True)
    assert plan["current_authority"] == "inactive"
    assert plan["activation_eligible"] is False


@pytest.mark.parametrize("array", ["regenerate", "exclusions"])
def test_control_extensions_use_existing_bounds(source, array):
    build_review(source)
    rewritten(source, lambda m: m[array][0].update(extensions={"version": True, "records": {}}))
    result = ex.verify_export(str(source[2]))
    assert not result["valid"]
    assert "EXPORT_CONTROL_RECORD_EXTENSIONS_INVALID" in result["errors"]


@pytest.mark.parametrize("array,field", [
    ("regenerate", "logical_id"), ("regenerate", "state_class"),
    ("regenerate", "restore_policy"), ("regenerate", "note"),
    ("exclusions", "logical_id"), ("exclusions", "reason"), ("exclusions", "note"),
])
def test_control_core_field_types_are_checked(source, array, field):
    build_review(source)
    def mutate(manifest):
        # Keep required canonical records intact; test a future historical record.
        record = copy.deepcopy(manifest[array][0])
        record["logical_id"] = "future.historical_record"
        record[field] = True
        manifest[array].append(record)
    rewritten(source, mutate)
    result = ex.verify_export(str(source[2]))
    assert not result["valid"]
    assert "EXPORT_CONTROL_RECORD_FIELD_INVALID" in result["errors"]


def test_v21_optional_notes_and_unfamiliar_historical_ids_remain_valid(source):
    build_review(source)
    def mutate(m):
        for array in ("regenerate", "exclusions"):
            record = copy.deepcopy(m[array][0])
            record["logical_id"] = "future.historical_record"
            record.pop("note", None)
            m[array].append(record)
    rewritten(source, mutate)
    assert ex.verify_export(str(source[2]))["valid"]


def test_v20_preserves_preexisting_historical_control_compatibility(source):
    build_review(source)
    def mutate(m):
        m["format_version"] = {"major": 2, "minor": 0}
        for field in ("min_supported_format_version", "max_known_format_version"):
            m["compatibility"][field] = dict(m["format_version"])
        for field in ("historical_claims", "extensions", "required_features"):
            m.pop(field)
        m["regenerate"][0]["owner_authorized"] = True
        m["exclusions"][0]["auto_approve"] = True
    rewritten(source, mutate)
    result = ex.verify_export(str(source[2]))
    assert result["valid"], result["errors"]
    plan = ex.historical_recovery_contract(result["manifest"], owner_designated=True)
    assert plan["history_eligible"] is True
    assert plan["current_authority"] == "inactive" and plan["activation_eligible"] is False


def test_v1_control_behavior_is_unchanged(source):
    data, base, destination = source
    ab.build_agent_backup(str(data), str(base), str(destination),
        credentials_path=NO_CREDENTIALS, identity_trailers_path=NO_IDENTITY_TRAILERS)
    def mutate(m):
        m["regenerate"][0]["owner_authorized"] = True
        m["exclusions"][0]["auto_approve"] = True
    rewritten(source, mutate)
    assert ab.verify_agent_backup(str(destination))["valid"]
    assert not ex.verify_export(str(destination))["valid"]
