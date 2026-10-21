"""Verified-export candidate, format 2.1; no Restore or live C1 snapshot API.

The old v1 builder/verifier remain unchanged by default. The small private v2
hooks in agent_backup reuse its collectors, immutable capture, ZIP grammar,
resource limits, publication and rollback. This module owns only the new policy.

Production full export fails closed until C1 supplies a reviewed snapshot
contract. Explicitly acknowledged Incomplete State Export uses major 2, a new
destination, and separate coverage/compatibility/acceptance evidence. Neither
this format nor integrity PASS authorizes Restore or runtime grants.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import re
import stat
import tempfile
from datetime import datetime, timezone
import zipfile

from core import agent_backup as ab
from core import suspension_backup as c1
from core.test_isolation import refuse_if_production_path

VERSION = {"major": 2, "minor": 1}
READ_VERSIONS = ({"major": 2, "minor": 0}, VERSION)
HISTORICAL_CLAIMS = {
    "version": 1,
    "export_observations": "builder_observed_at_capture_then_archive_declared",
    "installation_records": "archive_declared_history_not_official_authentication",
    "independent_provenance": "not_established_by_archive_hashes",
    "destination_corroboration": "not_required_for_owner_designated_history",
    "extensions": "opaque_history_never_current_authority",
}
_NAMESPACE = re.compile(r"[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+")
_MEMBER_FIELDS = frozenset({"logical_id", "archive_path", "state_class", "content_kind",
                            "required", "portable", "restore_policy", "rebind_policy", "size", "sha256"})
MARKER_ID = "metadata.knowledge_pack_installation"
MARKER_PATH = "metadata/knowledge_pack_installation.json"
MARKER_POLICY = {
    "archive_path": MARKER_PATH, "state_class": "REQUIRED_AGENT_STATE",
    "content_kind": "json", "required": True, "portable": "PORTABLE_WITH_REBIND",
    "restore_policy": "preserve_installation_history_never_infer_first_use",
    "rebind_policy": "reconcile_destination_installation_marker_before_startup",
}
RECOVERY_CONTRACT = {
    "version": 1,
    "authority": "historical_inactive",
    "prefs": "preserve_destination_security_rebind_locally",
    "safety": "destination_supremacy_owner_local_reconciliation_required",
    "restore_implemented": False,
}
_PREFS_POLICY = "restore_content_preserve_destination_security_rebind_locally"

# Reviewed portable-content keys only. Unknown keys never cross an authority
# boundary by default. No endpoint/owner/PIN credential values are archived here.
_TEXT_PREFS = frozenset({
    "human_bio", "human_profile_curated", "human_bio_public", "last_persona",
    "tts_backend", "tts_voice", "tts_device", "stt_backend", "stt_model",
    "stt_device", "reasoning_effort", "theme", "language", "user_name",
})
_BOOL_PREFS = frozenset({"tts_enabled", "stt_enabled", "spellcheck_enabled"})
_NUMBER_PREFS = frozenset({"window_width", "window_height", "tts_speed",
                           "tts_pitch", "tts_volume", "last_chat_id"})
_DROP_PREFS = frozenset({
    "pin_hash", "pin_salt", "telegram_remote_access",
    "telegram_remote_access_on_since", "telegram_remote_access_off_reason",
    "telegram_owner_chat_id", "telegram_chat_id", "owner_chat_id",
})


class ExportUnavailable(ab.AgentBackupError):
    """Coverage is unavailable; no production artifact is published."""


def _json_bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")


def _same_contract(actual, expected):
    # JSON booleans and integers are different policy values even though Python
    # equality treats False == 0 and True == 1 as equal.
    return _json_bytes(actual) == _json_bytes(expected)


def _bounded_history(value, *, max_bytes=256 * 1024):
    """Finite JSON with explicit storage/shape bounds, never executable policy."""
    remaining = 10000
    def walk(item, depth=0):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 16:
            raise ValueError("history shape limit")
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str or not key or len(key) > 256:
                    raise ValueError("history key")
                walk(child, depth + 1)
        elif type(item) is list:
            for child in item:
                walk(child, depth + 1)
        elif item is not None and type(item) not in (str, bool, int, float):
            raise ValueError("history value")
    walk(value)
    if len(_json_bytes(value)) > max_bytes:
        raise ValueError("history byte limit")


def _valid_extensions(value):
    try:
        _bounded_history(value)
        if (type(value) is not dict or set(value) != {"version", "records"}
                or type(value["version"]) is not int or value["version"] != 1
                or type(value["records"]) is not dict or len(value["records"]) > 64):
            return False
        for namespace, record in value["records"].items():
            if (len(namespace) > 128 or not _NAMESPACE.fullmatch(namespace)
                    or type(record) is not dict or set(record) != {"version", "data"}
                    or type(record["version"]) is not int or record["version"] < 1):
                return False
        return True
    except (TypeError, ValueError, OverflowError, RecursionError):
        return False


def _member_fields(logical_id):
    fields = set(_MEMBER_FIELDS)
    policy = ab._CANONICAL_MEMBER_POLICY.get(logical_id, {})
    for prefix, suffix, fixed, _ in ab._DYNAMIC_MEMBER_POLICY_FAMILIES:
        if logical_id.startswith(prefix) and logical_id.endswith(suffix):
            policy = ab._CUSTOM_TOOL_FIXED_POLICY if logical_id.startswith("state.custom_tools.") else fixed
            break
    if "rebind_policy" not in policy:
        fields.discard("rebind_policy")
    if logical_id in {"state.databases.lumina_db", "state.telemetry.flight_recorder_db"}:
        fields.add("database_metadata")
    if (any(logical_id.startswith("overlay." + family + ".")
            for family in ("personas", "avatars", "voices", "skills", "tool_profiles"))
            or logical_id == "overlay.projects.projectlist"):
        fields.add("provenance")
    if logical_id == "metadata.identity_trailers":
        fields.update({"provenance", "non_runtime_authority"})
    if logical_id == "metadata.shipped_baseline_hashes":
        fields.add("note")
    return fields


class C1CaptureBoundary(dict):
    """Carry the one pre-capture exclusion through every collector and publication."""

    def __init__(self, forbidden, data_dir, exclusion):
        super().__init__(forbidden)
        self.data_dir = os.path.abspath(data_dir)
        self.data_real = os.path.realpath(data_dir)
        self.exclusion = exclusion
        self.update({f"C1 protected {i}": identity
                     for i, identity in enumerate(sorted(exclusion.denied_identities()))})

    def reject_protected_source(self, path):
        # Check both named and resolved paths: a collector may be rooted inside
        # data_dir, or reach it through a directory alias. Never infer inventory.
        for candidate, root in ((os.path.abspath(path), self.data_dir),
                                (os.path.realpath(path), self.data_real)):
            if candidate != root and os.path.commonpath([candidate, root]) == root:
                relative = os.path.relpath(candidate, root)
                if c1.is_protected_relpath(relative):
                    raise ExportUnavailable("C1_PROTECTED_PATH")
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return
        if (st.st_dev, st.st_ino) in self.exclusion.denied_identities():
            raise ExportUnavailable("C1_PROTECTED_IDENTITY")

    def reject_sqlite_sources(self, source_path):
        # SQLite opens its sibling files internally, outside Python os.open.
        # These are SQLite suffixes, not a second C1 protected inventory.
        for suffix in ("", "-wal", "-shm", "-journal"):
            self.reject_protected_source(source_path + suffix)

    def verify_before_publication(self):
        try:
            c1.verify_exclusion_unchanged(self.data_dir, self.exclusion)
        except c1.ExclusionUnavailable as exc:
            raise ExportUnavailable("C1_EXCLUSION_" + exc.code) from exc


def _valid_c1_record(record):
    # Archive declarations carry no runtime authority. Pin the owned record's
    # exact shape and constants; do not re-observe the destination while reading.
    if type(record) is not dict:
        return False
    expected = {"schema": c1.SCHEMA, "version": c1.VERSION,
                "snapshot_captured": False, "enforcement_compatibility": "unproven",
                "c1_schema_version": "unknown", "suspended_at_capture": "unknown",
                "restore_fence": "c1_trace_and_owner_recover_required"}
    if set(record) != set(expected) | {"status", "observed_at", "warnings"}:
        return False
    if any(not _same_contract(record[k], v) for k, v in expected.items()):
        return False
    if (type(record["status"]) is not str or record["status"] not in {"absent_unknown", "present_excluded"}
            or type(record["observed_at"]) is not str
            or type(record["warnings"]) is not list
            or any(type(w) is not str or w not in c1.WARNINGS for w in record["warnings"])
            or len(record["warnings"]) != len(set(record["warnings"]))):
        return False
    try:
        observed = datetime.fromisoformat(record["observed_at"])
        return observed.utcoffset() == timezone.utc.utcoffset(observed)
    except ValueError:
        return False


def _scrub_preferences(raw):
    try:
        prefs = ab._strict_json_loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ab.AgentBackupError("EXPORT_PREFS_INVALID") from exc
    if type(prefs) is not dict:
        raise ab.AgentBackupError("EXPORT_PREFS_INVALID")
    result, omitted, unknown = {}, 0, 0
    for key, value in prefs.items():
        lower = key.lower()
        if key in _DROP_PREFS or any(t in lower for t in ("api_key", "token", "secret", "password")):
            omitted += 1
            continue
        if key in _TEXT_PREFS:
            valid = type(value) is str or value is None
        elif key in _BOOL_PREFS:
            valid = type(value) is bool
        elif key in _NUMBER_PREFS:
            valid = type(value) in (int, float) or value is None
        elif key == "disabled_tools":
            valid = type(value) is list and all(type(x) is str for x in value)
        else:
            omitted += 1
            unknown += 1
            continue
        if not valid:
            raise ab.AgentBackupError("EXPORT_PREFS_TYPE_INVALID")
        result[key] = value
    # A recovered file cannot re-enable communications. PIN/owner setup is a
    # destination-local requirement, not an archived owner credential.
    result["telegram_remote_access"] = False
    return _json_bytes(result), omitted, unknown


def _marker_records(raw):
    try:
        records = ab._strict_json_loads(raw)
        if type(records) is not dict:
            raise ValueError()
        # The marker is portable history, not an official-package registry.
        # Future pack IDs, deletion records and extension fields survive intact.
        _bounded_history(records, max_bytes=1024 * 1024)
        for category, item in records.items():
            if type(item) is not dict:
                raise ValueError()
            if "version" in item and (type(item["version"]) is not int or item["version"] < 1):
                raise ValueError()
            for field in ("installed_at", "deleted_at"):
                if field in item and (type(item[field]) is not str or not item[field]):
                    raise ValueError()
        return records
    except (ValueError, UnicodeError, TypeError) as exc:
        raise ab.AgentBackupError("EXPORT_INSTALLATION_MARKER_INVALID") from exc


def prepare_export_members(members, data_dir, staging_root, forbidden, warnings, budget):
    """Transform already-captured bytes, then add one bound control member."""
    members = [dict(m) for m in members]
    omitted = unknown = 0
    for member in members:
        if member["logical_id"] == "state.preferences.prefs_json":
            member["data"], omitted, unknown = _scrub_preferences(member["data"])
            member["restore_policy"] = _PREFS_POLICY
    marker_source = os.path.join(data_dir, "knowledge_packs_installed.json")
    raw, _staged = ab._stage_physical_state_file(
        marker_source, staging_root, MARKER_PATH, os.path.realpath(data_dir),
        forbidden, "knowledge-pack installation marker", warnings, budget=budget)
    # A missing marker is represented as unknown absence, not fabricated data.
    payload = {"schema": "lumina.backup.knowledge_pack_installation", "version": 1,
               "source_status": "missing_unknown" if raw is None else "present",
               "records": None if raw is None else _marker_records(raw)}
    marker_bytes = _json_bytes(payload)
    budget.charge(len(marker_bytes), "installation-control record")
    members.append({"logical_id": MARKER_ID, **MARKER_POLICY, "data": marker_bytes,
                    "staged_path": None})
    return members, {"suspension": ("unavailable_present" if forbidden.exclusion.status == "present_excluded"
                                      else "unavailable_no_trace"),
                     "c1_exclusion": forbidden.exclusion.manifest_record(),
                     "installation_marker": payload["source_status"],
                     "prefs_omitted_count": omitted, "prefs_unclassified_count": unknown}


def apply_export_contract(manifest, observations):
    manifest["format_version"] = dict(VERSION)
    manifest["compatibility"]["min_supported_format_version"] = dict(VERSION)
    manifest["compatibility"]["max_known_format_version"] = dict(VERSION)
    manifest["recovery_contract"] = copy.deepcopy(RECOVERY_CONTRACT)
    manifest["historical_claims"] = copy.deepcopy(HISTORICAL_CLAIMS)
    manifest["extensions"] = {"version": 1, "records": {}}
    manifest["required_features"] = []
    manifest["export_observations"] = dict(observations)
    manifest["c1_exclusion"] = manifest["export_observations"].pop("c1_exclusion")
    # Explicitly content-only until the C1 owner supplies a snapshot contract.
    manifest["backup_mode"] = "incomplete_state_export"
    manifest["coverage"] = _coverage(manifest)


def _validate_v21_control_records(manifest):
    """Pin sibling control fields; extensions remain bounded opaque history.

    Existing canonical value/duplicate checks still belong to A2. This applies
    only to 2.1, leaving v1 and supported 2.0 historical behavior unchanged.
    """
    errors = []
    for array, required in (
            ("regenerate", {"logical_id", "state_class", "restore_policy"}),
            ("exclusions", {"logical_id", "reason"})):
        records = manifest.get(array)
        if type(records) is not list:
            continue  # A2 validates container and non-object entry types.
        for record in records:
            if type(record) is not dict:
                continue
            if set(record) - (required | {"note", "extensions"}):
                errors.append("EXPORT_CONTROL_RECORD_FIELDS_UNSUPPORTED")
            if (any(type(record.get(field)) is not str for field in required)
                    or ("note" in record and type(record["note"]) is not str)):
                errors.append("EXPORT_CONTROL_RECORD_FIELD_INVALID")
            if "extensions" in record and not _valid_extensions(record["extensions"]):
                errors.append("EXPORT_CONTROL_RECORD_EXTENSIONS_INVALID")
    return errors


def validate_export_schema(manifest):
    """Validate new mandatory policy first, then reuse v1's base-member rules.

    The projection below is validation-only, never an accepted old archive or
    reconstructed provenance. Output remains the original exact v2 manifest.
    The public v1 verifier continues to refuse major 2 and unknown members.
    """
    if type(manifest) is not dict:
        return ["EXPORT_MANIFEST_INVALID"]
    errors = []
    expected_roots = {"format", "format_version", "lumina_version", "created_at", "source_platform",
                      "source_data_root", "backup_mode", "hash_algorithm", "compatibility", "members",
                      "regenerate", "exclusions", "warnings", "recovery_contract", "export_observations", "coverage"}
    version = manifest.get("format_version")
    modern = _same_contract(version, VERSION)
    if modern:
        errors.extend(_validate_v21_control_records(manifest))
        expected_roots.update({"extensions", "required_features", "historical_claims"})
        if not _same_contract(manifest.get("historical_claims"), HISTORICAL_CLAIMS):
            errors.append("EXPORT_HISTORICAL_CLAIMS_POLICY_MISMATCH")
        if not _valid_extensions(manifest.get("extensions")):
            errors.append("EXPORT_EXTENSIONS_INVALID")
        features = manifest.get("required_features")
        if (type(features) is not list or len(features) > 64
                or any(type(f) is not str or len(f) > 128 or not _NAMESPACE.fullmatch(f) for f in features)
                or len(features) != len(set(features))):
            errors.append("EXPORT_REQUIRED_FEATURES_INVALID")
    # Earlier 2.0/2.1 historical exports predate the exclusion interface.
    # Keep them readable; every newly built export includes the exact record.
    if "c1_exclusion" in manifest:
        expected_roots.add("c1_exclusion")
        if not _valid_c1_record(manifest["c1_exclusion"]):
            errors.append("EXPORT_C1_EXCLUSION_INVALID")
        else:
            expected_observation = ("unavailable_present" if manifest["c1_exclusion"]["status"] == "present_excluded"
                                    else "unavailable_no_trace")
            declared_observations = manifest.get("export_observations")
            if (type(declared_observations) is not dict
                    or declared_observations.get("suspension") != expected_observation):
                errors.append("EXPORT_C1_OBSERVATION_MISMATCH")
    if set(manifest) != expected_roots:
        errors.append("EXPORT_ROOT_CONTRACT_MISMATCH")
    try:
        if not _same_contract(manifest.get("coverage"), _coverage(manifest)):
            errors.append("EXPORT_COVERAGE_CONTRACT_MISMATCH")
    except (KeyError, TypeError):
        errors.append("EXPORT_COVERAGE_CONTRACT_INVALID")
    if not any(_same_contract(version, v) for v in READ_VERSIONS) or manifest.get("backup_mode") != "incomplete_state_export":
        errors.append("EXPORT_FORMAT_OR_MODE_UNSUPPORTED")
    if not _same_contract(manifest.get("recovery_contract"), RECOVERY_CONTRACT):
        errors.append("EXPORT_RECOVERY_POLICY_MISMATCH")
    observations = manifest.get("export_observations")
    expected = {"suspension", "installation_marker", "prefs_omitted_count", "prefs_unclassified_count"}
    if type(observations) is not dict or set(observations) != expected:
        errors.append("EXPORT_COVERAGE_OBSERVATIONS_INVALID")
    elif (observations["suspension"] not in {"unavailable_present", "unavailable_no_trace"}
          or observations["installation_marker"] not in {"present", "missing_unknown"}
          or any(type(observations[k]) is not int or observations[k] < 0
                 for k in ("prefs_omitted_count", "prefs_unclassified_count"))):
        errors.append("EXPORT_COVERAGE_OBSERVATIONS_INVALID")
    compat = manifest.get("compatibility")
    if type(compat) is not dict or any(not _same_contract(compat.get(k), version) for k in
                                      ("min_supported_format_version", "max_known_format_version")):
        errors.append("EXPORT_COMPATIBILITY_INVALID")
    markers = [m for m in manifest.get("members", [])
               if type(m) is dict and m.get("logical_id") == MARKER_ID]
    if len(markers) != 1 or any(not _same_contract(markers[0].get(k), v) for k, v in MARKER_POLICY.items()):
        errors.append("EXPORT_INSTALLATION_MEMBER_MISSING_OR_INVALID")
    base = copy.deepcopy(manifest)
    base["format_version"] = {"major": 1, "minor": 0}
    base["backup_mode"] = "full"
    base["members"] = [m for m in base.get("members", [])
                       if not (type(m) is dict and m.get("logical_id") == MARKER_ID)]
    for member in base["members"]:
        if type(member) is not dict:
            continue
        logical_id = member.get("logical_id", "")
        if type(logical_id) is not str:
            continue
        allowed = _member_fields(logical_id)
        if modern:
            allowed.add("extensions")
        if set(member) - allowed:
            errors.append("EXPORT_MEMBER_FIELDS_UNSUPPORTED")
        if logical_id == "metadata.identity_trailers" and member.get("non_runtime_authority") is not True:
            errors.append("EXPORT_IDENTITY_METADATA_AUTHORITY_INVALID")
        if "extensions" in member:
            if not modern or not _valid_extensions(member["extensions"]):
                errors.append("EXPORT_MEMBER_EXTENSIONS_INVALID")
            # Validation projection only; v1's closed authority schema is intact.
            member.pop("extensions")
        if type(member) is dict and member.get("logical_id") == "state.preferences.prefs_json":
            if member.get("restore_policy") != _PREFS_POLICY:
                errors.append("EXPORT_PREFS_POLICY_MISMATCH")
            member["restore_policy"] = ab._CANONICAL_MEMBER_POLICY[member["logical_id"]]["restore_policy"]
    errors.extend(ab._validate_manifest_schema(base))
    # New member still gets all ordinary required-field/hash/type checks.
    if markers:
        m = markers[0]
        marker_fields = {"logical_id", "sha256", "size", *MARKER_POLICY}
        if modern and "extensions" in m:
            marker_fields.add("extensions")
            if not _valid_extensions(m["extensions"]):
                errors.append("EXPORT_MEMBER_EXTENSIONS_INVALID")
        if (set(m) != marker_fields
                or type(m.get("size")) is not int or m["size"] < 0
                or type(m.get("sha256")) is not str or not ab._SHA256_RE.fullmatch(m["sha256"])):
            errors.append("EXPORT_INSTALLATION_MEMBER_INVALID")
    return errors


def historical_recovery_contract(manifest, *, owner_designated=False):
    """Pure acceptance specification, not Restore or an archive verifier.

    Callers must first verify immutable archive bytes. No destination records,
    files, DBs, approvals or credentials are consulted or changed here. Unknown
    required runtime features retain history but block activation. There is no
    activation implementation in this campaign.
    """
    errors = validate_export_schema(manifest)
    return {"history_eligible": not errors and owner_designated is True,
            "destination_history_required": False,
            "current_authority": "inactive", "c1_recovery_fence_required": True,
            "unsupported_required_features": list(manifest.get("required_features", [])),
            "activation_eligible": False, "recovery_implemented": False,
            "schema_errors": errors}


def validate_export_payload(member, raw, manifest):
    try:
        if member.get("logical_id") == MARKER_ID:
            payload = ab._strict_json_loads(raw)
            if (type(payload) is not dict
                    or set(payload) != {"schema", "version", "source_status", "records"}
                    or payload["schema"] != "lumina.backup.knowledge_pack_installation"
                    or type(payload["version"]) is not int or payload["version"] != 1):
                return ["EXPORT_INSTALLATION_PAYLOAD_INVALID"]
            if payload["source_status"] != manifest["export_observations"]["installation_marker"]:
                return ["EXPORT_INSTALLATION_OBSERVATION_MISMATCH"]
            if payload["source_status"] == "present":
                _marker_records(_json_bytes(payload["records"]))
            elif payload["source_status"] != "missing_unknown" or payload["records"] is not None:
                return ["EXPORT_INSTALLATION_PAYLOAD_INVALID"]
        elif member.get("logical_id") == "state.preferences.prefs_json":
            cleaned, _, unknown = _scrub_preferences(raw)
            if ab._strict_json_loads(cleaned) != ab._strict_json_loads(raw) or unknown:
                return ["EXPORT_PREFS_SECURITY_POLICY_VIOLATION"]
    except (ValueError, UnicodeError, ab.AgentBackupError):
        return ["EXPORT_PAYLOAD_POLICY_INVALID"]
    return []


def _bounded_manifest_info(z, resident_bytes_hint):
    """Use A2's host/configured verification budget before decompression."""
    entries = z.infolist()
    if len(entries) > ab._MAX_PHYSICAL_ZIP_ENTRIES:
        raise ab.AgentBackupError("EXPORT_INPUT_RESOURCE_LIMIT")
    markers = [i for i in entries if i.filename == "manifest.json"]
    budget = ab._determine_verification_payload_budget(
        resident_bytes_hint=resident_bytes_hint, member_count=len(entries))
    if (len(markers) != 1 or markers[0].file_size > budget
            or sum(i.file_size for i in entries) > budget):
        raise ab.AgentBackupError("EXPORT_MANIFEST_RESOURCE_LIMIT")
    return markers[0]


def classify_input(path):
    """Untrusted format hint only; never verification or recovery acceptance.

    Uses the verifier's shared resource policy, with conservative reservation
    for two resident archive copies, rather than an unrelated manifest cap.
    Legacy payloads are never read.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        st = os.fstat(source.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size > ab._determine_aggregate_payload_budget(0):
            raise ab.AgentBackupError("EXPORT_INPUT_RESOURCE_LIMIT")
        with zipfile.ZipFile(source) as z:
            entries = z.infolist()
            if len(entries) > ab._MAX_PHYSICAL_ZIP_ENTRIES:
                raise ab.AgentBackupError("EXPORT_INPUT_RESOURCE_LIMIT")
            marker = [i for i in entries if i.filename == "manifest.json"]
            if not marker:
                return {"kind": "legacy_memory_backup", "verification": "not_performed", "full_agent_coverage": False,
                        "coverage_blockers": ["legacy_format", "installation_marker", "suspension"]}
            info = _bounded_manifest_info(z, 2 * st.st_size)
            manifest = ab._strict_json_loads(z.read(info))
    if type(manifest) is dict and manifest.get("format") == ab.MANIFEST_FORMAT:
        if manifest.get("format_version") == {"major": 1, "minor": 0}:
            return {"kind": "agent_backup_v1", "verification": "not_performed", "full_agent_coverage": False,
                    "coverage_blockers": ["installation_marker", "suspension", "prefs_policy"]}
        if any(_same_contract(manifest.get("format_version"), v) for v in READ_VERSIONS):
            return {"kind": "agent_export_v2", "verification": "not_performed", "full_agent_coverage": False,
                    "coverage_blockers": ["suspension"]}
    return {"kind": "unsupported", "verification": "not_performed", "full_agent_coverage": False,
            "coverage_blockers": ["unknown_format"]}


def verify_export(source, resident_bytes_hint=0):
    """Strict v2 verifier, with no permissive legacy/v1 acceptance fallback."""
    try:
        # The core verifier testzip()s all members. Refuse manifestless input
        # first so personal legacy specimens need no payload decompression.
        if isinstance(source, (str, os.PathLike)):
            raw, kind, _, _ = ab._open_and_capture_stable_bytes(
                os.fspath(source), os.path.realpath(os.path.dirname(os.path.abspath(source))), {},
                max_capture_bytes=ab._determine_aggregate_payload_budget(0))
            if kind != "ok":
                raise ab.AgentBackupError("EXPORT_CAPTURE_UNAVAILABLE")
            source = io.BytesIO(raw)
            resident_bytes_hint += len(raw)
        source.seek(0)
        with zipfile.ZipFile(source) as z:
            if "manifest.json" not in z.namelist():
                return {"valid": False, "errors": ["EXPORT_MANIFEST_REQUIRED"], "manifest": None}
            source.seek(0, os.SEEK_END)
            _bounded_manifest_info(z, resident_bytes_hint + source.tell())
        source.seek(0)
        return ab._verify_agent_backup_inner(source, resident_bytes_hint, _export_v2=True)
    except Exception:
        return {"valid": False, "errors": ["EXPORT_INPUT_INVALID"], "manifest": None}


def _coverage(manifest):
    observations = manifest["export_observations"]
    families = sorted({m["logical_id"] for m in manifest["members"]})
    return {
        "semantic_coverage": {"status": "incomplete", "captured_members": families,
                              "knowledge_pack_installation_marker": observations["installation_marker"],
                              "unclassified_preferences_omitted": observations["prefs_unclassified_count"]},
        "c1_safety_coverage": {"status": "unavailable", "source_observation": observations["suspension"],
                               "omitted": ["C1 store", "soul identity", "evidence", "incidents", "epochs", "replay state"],
                               "enforcement_compatibility": "unproven",
                               **({"exclusion": copy.deepcopy(manifest["c1_exclusion"])}
                                  if "c1_exclusion" in manifest else {})},
        "recovery_acceptance": {"status": "not_proven", "restore_input_accepted": False},
    }


def _receipt(result):
    report = verify_export(io.BytesIO(result.built_data))
    if not report["valid"]:
        raise ab.AgentBackupError("EXPORT_BUILT_BYTES_INVALID")
    observations = report["manifest"]["export_observations"]
    blockers = ["suspension"]
    if observations["installation_marker"] != "present":
        blockers.append("installation_marker")
    if observations["prefs_unclassified_count"]:
        blockers.append("unclassified_preferences")
    return {**_coverage(report["manifest"]),
            "historical_claims": copy.deepcopy(report["manifest"].get("historical_claims")),
            "archive_integrity": {"status": "PASS", "scope": "structure_hashes_sqlite"},
            "export_kind": "incomplete_state_export",
            "verification": "PASS", "destination": result.dest_path,
            "archive_sha256": hashlib.sha256(result.built_data).hexdigest(),
            "archive_size": len(result.built_data), "format_version": dict(VERSION),
            "member_count": report["validated_member_count"],
            "database_snapshots_verified": len(report["verified_sqlite_members"]),
            "full_agent_coverage": False, "restorable_verified": False,
            "coverage_blockers": blockers, "prefs_omitted_count": observations["prefs_omitted_count"],
            "publication": result.publication_observation,
            "warnings": report["manifest"]["warnings"]}


def build_review_export_with_receipt(data_dir, base_dir, destination, **kwargs):
    """Synthetic acceptance seam only; unavailable in a running installation."""
    if os.environ.get("LUMINA_TESTING") != "1":
        raise ExportUnavailable("Review export is restricted to isolated synthetic tests.")
    for root in (data_dir, base_dir, destination):
        refuse_if_production_path(root)
        real = os.path.realpath(root)
        # The historic generic guard misses the release data root.
        if real == os.path.expanduser("~/.local/share/lumina-release") or real.startswith(
                os.path.expanduser("~/.local/share/lumina-release") + os.sep):
            raise ExportUnavailable("Review export refuses the live release data root.")
    kwargs["_export_v2"] = True
    result = ab._build_agent_backup_core(data_dir, base_dir, destination, **kwargs)
    return result.manifest, _receipt(result)


def export_verified_agent_backup(data_dir, base_dir, destination):
    """Owner UI entry point; never publish incomplete coverage as full."""
    raise ExportUnavailable(
        "Verified Agent Backup is unavailable: suspension snapshot coverage has no "
        "approved C1 contract. Missing state is not proof of "
        "a first installation. No archive was written. Backup Memory (Legacy) remains "
        "available with its partial-coverage limitations. The separate Incomplete State Export "
        "requires deliberate owner acknowledgement and a new destination.")


def confirm_export_result(destination, manifest, receipt):
    """Reobserve one stable capture; claimed PASS/size alone cannot succeed."""
    raw, kind, _reason, _attempts = ab._open_and_capture_stable_bytes(
        destination, os.path.realpath(os.path.dirname(os.path.abspath(destination))), {},
        max_capture_bytes=ab._determine_aggregate_payload_budget(0))
    if kind != "ok":
        raise ab.AgentBackupError("EXPORT_PUBLICATION_NOT_OBSERVED")
    report = verify_export(io.BytesIO(raw))
    if (not report["valid"] or not _same_contract(report["manifest"], manifest)
            or receipt.get("destination") != destination
            or not _same_contract(receipt.get("format_version"), VERSION)
            or receipt.get("verification") != "PASS"
            or receipt.get("archive_sha256") != hashlib.sha256(raw).hexdigest()
            or receipt.get("archive_size") != len(raw)):
        raise ab.AgentBackupError("EXPORT_RECEIPT_NOT_BOUND_TO_ARTIFACT")
    # Derive coverage; a caller cannot launder a full-coverage claim in a dict.
    if receipt.get("full_agent_coverage") is not False or receipt.get("restorable_verified") is not False:
        raise ab.AgentBackupError("EXPORT_UNSUPPORTED_SUCCESS_CLAIM")
    expected = _coverage(report["manifest"])
    if (any(not _same_contract(receipt.get(k), v) for k, v in expected.items())
            or not _same_contract(receipt.get("historical_claims"), report["manifest"].get("historical_claims"))
            or receipt.get("member_count") != report["validated_member_count"]
            or receipt.get("database_snapshots_verified") != len(report["verified_sqlite_members"])
            or receipt.get("archive_integrity") != {"status": "PASS", "scope": "structure_hashes_sqlite"}
            or receipt.get("export_kind") != "incomplete_state_export"):
        raise ab.AgentBackupError("EXPORT_RECEIPT_COVERAGE_MISMATCH")
    confirmed = dict(receipt)
    publication = receipt.get("publication", {})
    if (publication.get("observed_sha256") != hashlib.sha256(raw).hexdigest()
            or publication.get("matched_built_artifact") is not True
            or publication.get("overwrite") is not False
            or publication.get("destination") != destination):
        raise ab.AgentBackupError("EXPORT_PUBLICATION_RECEIPT_MISMATCH")
    confirmed["publication"] = {**publication,
        "directory_fsynced": ab._best_effort_fsync_dir(os.path.dirname(os.path.abspath(destination))),
        "checked_at": datetime.now(timezone.utc).isoformat()}
    return confirmed


def preflight_incomplete(data_dir, destination):
    refuse_if_production_path(data_dir)
    if os.environ.get("LUMINA_TESTING") == "1":
        live_release = os.path.expanduser("~/.local/share/lumina-release")
        for candidate in (data_dir, destination):
            real = os.path.realpath(candidate)
            if real == live_release or real.startswith(live_release + os.sep):
                raise ExportUnavailable("Synthetic tests refuse the live release data root.")
    if os.path.lexists(destination):
        raise ExportUnavailable("Incomplete State Export needs a new destination. Existing files are preserved.")
    try:
        return c1.describe_exclusions(data_dir)
    except c1.ExclusionUnavailable as exc:
        raise ExportUnavailable("C1_EXCLUSION_" + exc.code) from exc


def export_incomplete_state(data_dir, base_dir, destination, *, owner_acknowledged=False,
                            identity_trailers_path=None, credentials_path=None):
    """Owner UI only. Explicit new-format partial export; never a full backup.

    A caller's acknowledgement authorizes only this partial archive, not Restore,
    security-state mutation, overwrite, store initialization or runtime grants.
    """
    if owner_acknowledged is not True:
        raise ExportUnavailable("Incomplete State Export requires explicit owner acknowledgement.")
    result = ab._build_agent_backup_core(
        data_dir, base_dir, destination, identity_trailers_path=identity_trailers_path,
        credentials_path=credentials_path, _export_v2=True)
    return result.manifest, _receipt(result)


def publish_new_export(data, expected_sha256, destination, parent_real, forbidden):
    """Atomic link-if-absent, never replace/rollback an existing destination.

    File data is fsynced before publication. The directory descriptor pins the
    target while link() provides an atomic EEXIST barrier against target races.
    A mismatch after publication leaves evidence for owner review; it cannot
    authorize deleting or replacing a concurrently supplied file.
    """
    parent = os.path.dirname(os.path.abspath(destination))
    leaf = os.path.basename(destination)
    directory_fd = os.open(parent_real, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = None
    identity = None
    try:
        def check_parent():
            held = os.fstat(directory_fd)
            named = os.stat(parent)
            if ((held.st_dev, held.st_ino) != (named.st_dev, named.st_ino)
                    or os.path.realpath(parent) != parent_real):
                raise ab.AgentBackupError("EXPORT_DESTINATION_PARENT_CHANGED")
        check_parent()
        fd, path = tempfile.mkstemp(prefix=".incomplete_export_", dir=parent_real)
        temporary = os.path.basename(path)
        # Own the scratch inode before any write/fsync can fail so cleanup
        # covers failures as well as boundary refusals.
        st = os.fstat(fd)
        identity = (st.st_dev, st.st_ino)
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
            st = os.fstat(output.fileno())
            identity = (st.st_dev, st.st_ino)
        check_parent()
        if isinstance(forbidden, C1CaptureBoundary):
            forbidden.verify_before_publication()
        os.link(temporary, leaf, src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                follow_symlinks=False)
        # Remove only the scratch name still bound to our inode. nlink must be
        # one before the existing strict stable-capture boundary can accept it.
        current = os.stat(temporary, dir_fd=directory_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != identity:
            raise ab.AgentBackupError("EXPORT_PUBLICATION_TEMP_CHANGED")
        os.unlink(temporary, dir_fd=directory_fd)
        temporary = None
        try:
            os.fsync(directory_fd)
            directory_fsynced = True
        except OSError:
            directory_fsynced = False
        check_parent()
        observed, kind, _, _ = ab._open_and_capture_stable_bytes(destination, parent_real, forbidden,
                                                               expected_identity=identity)
        if kind != "ok" or hashlib.sha256(observed).hexdigest() != expected_sha256:
            raise ab.AgentBackupError("EXPORT_PUBLICATION_CONTENT_MISMATCH")
        return {"destination": destination, "observed_sha256": expected_sha256,
                "matched_built_artifact": True, "published_identity": identity,
                "directory_fsynced": directory_fsynced,
                "checked_at": datetime.now(timezone.utc).isoformat(), "overwrite": False}
    finally:
        if temporary is not None and identity is not None:
            try:
                st = os.stat(temporary, dir_fd=directory_fd, follow_symlinks=False)
                if (st.st_dev, st.st_ino) == identity:
                    os.unlink(temporary, dir_fd=directory_fd)
            except OSError:
                pass
        os.close(directory_fd)
