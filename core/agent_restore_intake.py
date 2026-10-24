"""core/agent_restore_intake.py -- Restore intake and recovery preview
(AGENT-BACKUP-RESTORE-01, first slice).

Reads one owner-chosen archive once, decides what kind of input it is, and
describes what a later Restore could recover into a chosen data folder.

It never writes. There is no staging directory, no destination file, no
database open on the destination, and no C1 call that opens SQLite (the
destination's safety state is observed with core.suspension_backup, which
only lstats). Nothing here activates, imports, executes or grants anything:
`activation.available` is always False until a separately authorized slice
supplies the writer fence, rollback journal and C1 restore reconciliation.

Four input classes:

- verified_agent_backup: the strict A2 v1 verifier or the v2 export verifier
  passed the exact captured bytes.
- selective_recovery_candidates: the manifest, ZIP grammar and namespace are
  sound, but one or more declared members are damaged or missing. Only
  members that independently validate are offered, with dependency closure;
  a full Restore of this archive is refused.
- legacy_backup_limited: a manifestless Backup Memory (Legacy) ZIP of the
  data folder. No hashes, no provenance and no source overlays exist, so
  every family it offers is labeled limited.
- rejected: unsafe, unsupported or unexplained input. Nothing is offered.
"""
from __future__ import annotations

import copy
import hashlib
import io
import os
import stat
import struct
import unicodedata
import zipfile

from core import agent_backup as ab
from core import agent_backup_export as abx
from core import suspension_backup as c1
from core.test_isolation import refuse_if_production_path

INTAKE_SCHEMA = "lumina.restore_intake"
PREVIEW_SCHEMA = "lumina.restore_preview"
VERSION = 1

VERIFIED = "verified_agent_backup"
SELECTIVE = "selective_recovery_candidates"
LEGACY = "legacy_backup_limited"
REJECTED = "rejected"

REFUSALS = frozenset({
    "INPUT_UNAVAILABLE",           # missing or unreadable
    "INPUT_NOT_REGULAR",           # symlink, directory, FIFO, device
    "INPUT_ALIASED",               # another hard link names the same file
    "INPUT_UNSTABLE",              # changed while it was being read
    "INPUT_TOO_LARGE",
    "NOT_A_ZIP",
    "ZIP_STRUCTURE_UNSAFE",        # prefix, concatenation, trailing bytes, header disagreement
    "ZIP_ENCRYPTED",
    "ZIP_METHOD_UNSUPPORTED",
    "ZIP_ENTRY_UNSAFE",            # traversal, absolute, symlink/directory entries
    "ZIP_NAME_COLLISION",          # duplicate, NFC or case-fold collision
    "ZIP_RESOURCE_LIMIT",
    "MANIFEST_INVALID",
    "FORMAT_UNSUPPORTED",          # unknown format, newer or unknown version
    "MANIFEST_SCHEMA_INVALID",
    "ARCHIVE_NAMESPACE_INVALID",
    "ARCHIVE_UNDECLARED_MEMBER",
    "ARCHIVE_INTEGRITY_FAILED",    # verifier failed and no member damage explains it
    "LEGACY_LAYOUT_UNRECOGNIZED",
})

# Prerequisites a future activation slice must satisfy; listed in every
# preview so no caller can read a preview as permission to proceed.
ACTIVATION_GATE = (
    "owner_local_consent_bound_to_plan_digest",
    "writer_fence_and_quiescence_proof",
    "final_rollback_snapshot_after_quiescence",
    "write_ahead_recovery_journal",
    "c1_restore_trace_and_owner_recover",
    "post_activation_acceptance_with_execution_blocked",
)

_V1 = {"major": 1, "minor": 0}
_A2_PREFIXES = ("state/", "overlay/", "metadata/")
_FILE_SETS = {"skills", "personas", "avatars", "voices", "tool_profiles", "custom_tools",
              "audit_and_pending_actions"}

# unit -> (disposition, description). "recover" units are recovery candidates;
# historical_inactive units are kept as history and never activated.
_UNIT_POLICY = {
    "primary_database": ("recover", "Chats, messages, memories, Palace, Knowledge Base and the Skills "
                                    "index, as one coherent database"),
    "flight_recorder": ("recover", "Flight Recorder telemetry history"),
    "preferences": ("recover", "Portable preference content only; security and credential fields are dropped"),
    "skills": ("recover", "Skill Markdown files with their index rows"),
    "personas": ("recover", "Persona files"),
    "avatars": ("recover", "Avatar images"),
    "voices": ("recover", "Voice files"),
    "tool_profiles": ("recover", "Tool profiles (selections, never permission grants)"),
    "project_list": ("recover", "Project list"),
    "project": ("recover", "Project documents and chat links; the root binding stays inactive until "
                           "the owner re-picks it"),
    "knowledge_pack_installation": ("recover", "Knowledge Pack installation and deletion history, adopted "
                                               "before bootstrap; never official-package trust"),
    "custom_tools": ("historical_inactive", "Custom tool source kept as quarantined history; never imported "
                                            "or executed; reapproval required"),
    "audit_and_pending_actions": ("historical_inactive", "Tool audit and pending actions kept as history; "
                                                         "pending actions are never replayed"),
    "provenance_metadata": ("informational", "Identity trailers and baseline hashes; never credentials or "
                                             "authority"),
}

# Members that need the primary database to mean anything once restored.
_NEEDS_PRIMARY_DB = ("overlay.skills.",)

# Destination targets, relative to the data folder ("data") or install
# folder ("base"). Only lstat is ever applied to them.
_DATA_TARGETS = {
    "primary_database": ["memory/lumina.db"],
    "flight_recorder": ["telemetry/flight_recorder.db"],
    "preferences": ["memory/prefs.json"],
    "knowledge_pack_installation": ["knowledge_packs_installed.json"],
    "audit_and_pending_actions": ["memory/tool_audit.log", "memory/pending_actions.json",
                                  "memory/pending_actions_audit.log"],
}
_BASE_DIRS = {"skills": "skills", "personas": "personas", "avatars": "assets/avatars",
              "voices": "assets/voices", "tool_profiles": "tool_profiles"}

_ALWAYS_EXCLUDED = ("credentials", "auth_sessions", "pin_hash_and_salt", "owner_and_channel_bindings",
                    "idempotency_ledger", "worktrees_and_runtime_state")

# Legacy (DATA_DIR-relative) layout written by core.memory_backup.
_LEGACY_FILES = {
    "memory/lumina.db": ("primary_database", "lumina_db"),
    "telemetry/flight_recorder.db": ("flight_recorder", "flight_recorder_db"),
    "memory/prefs.json": ("preferences", None),
    "memory/tool_audit.log": ("audit_and_pending_actions", None),
    "memory/pending_actions.json": ("audit_and_pending_actions", None),
    "memory/pending_actions_audit.log": ("audit_and_pending_actions", None),
    "knowledge_packs_installed.json": ("knowledge_pack_installation", None),
}
_LEGACY_ANCHORS = ("memory/lumina.db", "memory/prefs.json")
_SQLITE_SIDECARS = ("-wal", "-shm", "-journal")


class _Refused(Exception):
    def __init__(self, code, detail=""):
        assert code in REFUSALS, code
        super().__init__(code)
        self.code, self.detail = code, detail


def _digest(value):
    return hashlib.sha256(abx._json_bytes(value)).hexdigest()


# --------------------------------------------------------------------------
# Capture and ZIP grammar shared by every input class
# --------------------------------------------------------------------------

def _capture(path):
    """One bounded, stable read of a regular, singly linked file."""
    path = os.fspath(path)
    try:
        st = os.lstat(path)
    except OSError:
        raise _Refused("INPUT_UNAVAILABLE") from None
    if not stat.S_ISREG(st.st_mode):
        raise _Refused("INPUT_NOT_REGULAR")
    limit = ab._determine_aggregate_payload_budget(0)
    if st.st_size > limit:
        raise _Refused("INPUT_TOO_LARGE")
    parent = os.path.realpath(os.path.dirname(os.path.abspath(path)))
    raw, kind, _reason, _ = ab._open_and_capture_stable_bytes(path, parent, {}, max_capture_bytes=limit)
    if kind == "ok":
        return raw
    raise _Refused({"hardlink": "INPUT_ALIASED", "special": "INPUT_NOT_REGULAR",
                    "unstable": "INPUT_UNSTABLE", "absent": "INPUT_UNAVAILABLE"}.get(kind, "INPUT_UNAVAILABLE"))


def _check_names(names):
    seen, folded = set(), set()
    for name in names:
        if (not name or name.startswith("/") or "\\" in name or "\x00" in name
                or name.endswith("/") or any(seg in ("", ".", "..") for seg in name.split("/"))
                or any(ord(ch) < 0x20 for ch in name)):
            raise _Refused("ZIP_ENTRY_UNSAFE", name)
        nfc = unicodedata.normalize("NFC", name)
        if name in seen or nfc.casefold() in folded:
            raise _Refused("ZIP_NAME_COLLISION", name)
        seen.add(name)
        folded.add(nfc.casefold())


def _check_grammar(raw, infos):
    """Format-neutral ZIP safety, from headers only, before any decompression.

    The A2 verifiers apply their stricter canonical layout on top of this;
    a legacy archive gets this and nothing weaker.
    """
    if ab._check_no_trailing_zip_bytes(raw):
        raise _Refused("ZIP_STRUCTURE_UNSAFE", "trailing bytes or archive comment")
    idx = raw.rfind(ab._EOCD_SIGNATURE)
    disk, cd_disk, here, total, cd_size, cd_offset = struct.unpack(ab._EOCD_STRUCT, raw[idx:idx + 20])[1:]
    if (disk or cd_disk or here != total or total != len(infos) or cd_offset + cd_size != idx
            or ab._ZIP64_SENTINEL_32 in (cd_size, cd_offset) or total == ab._ZIP64_SENTINEL_16):
        raise _Refused("ZIP_STRUCTURE_UNSAFE", "end-of-central-directory layout")
    if not infos:
        raise _Refused("ZIP_STRUCTURE_UNSAFE", "empty archive")
    if len(infos) > ab._MAX_PHYSICAL_ZIP_ENTRIES:
        raise _Refused("ZIP_RESOURCE_LIMIT", "entry count")
    _check_names([i.filename for i in infos])
    budget = ab._determine_verification_payload_budget(resident_bytes_hint=2 * len(raw),
                                                       member_count=len(infos))
    if any(i.file_size > budget for i in infos) or sum(i.file_size for i in infos) > budget:
        raise _Refused("ZIP_RESOURCE_LIMIT", "declared uncompressed size")
    cursor = 0
    for info in sorted(infos, key=lambda i: i.header_offset):
        if info.flag_bits & 0x1:
            raise _Refused("ZIP_ENCRYPTED", info.filename)
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise _Refused("ZIP_METHOD_UNSUPPORTED", info.filename)
        mode = info.external_attr >> 16
        if (stat.S_IFMT(mode) not in (0, stat.S_IFREG) or info.external_attr & 0x10
                or info.flag_bits not in (0, 0x800)):
            raise _Refused("ZIP_ENTRY_UNSAFE", info.filename)
        local = ab._parse_local_header(raw, info.header_offset)
        expected_name = (info.filename.encode("utf-8") if info.flag_bits & 0x800
                         else info.filename.encode("cp437", "replace"))
        y, mo, d, h, mi, s = info.date_time
        dos = struct.pack("<HH", h << 11 | mi << 5 | s // 2, (y - 1980) << 9 | mo << 5 | d)
        if (info.header_offset != cursor or local is None or local["fname"] != expected_name
                or raw[info.header_offset + 10:info.header_offset + 14] != dos
                or local["flags"] != info.flag_bits or local["compression"] != info.compress_type
                or local["crc32"] != info.CRC or local["comp_size"] != info.compress_size
                or local["uncomp_size"] != info.file_size):
            raise _Refused("ZIP_STRUCTURE_UNSAFE", info.filename)
        cursor = info.header_offset + local["header_size"] + local["comp_size"]
    if cursor != cd_offset:
        raise _Refused("ZIP_STRUCTURE_UNSAFE", "unaccounted bytes before the central directory")


def _read(z, name):
    """Member bytes, or None when the stored data fails its CRC or inflate."""
    try:
        return z.read(name)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, OSError, EOFError):
        return None
    except Exception:  # zlib.error and friends: damage, never a crash
        return None


# --------------------------------------------------------------------------
# Units: grouping members into recoverable families
# --------------------------------------------------------------------------

def _unit_of(logical_id):
    fixed = {"state.databases.lumina_db": "primary_database",
             "state.telemetry.flight_recorder_db": "flight_recorder",
             "state.preferences.prefs_json": "preferences",
             "overlay.projects.projectlist": "project_list",
             abx.MARKER_ID: "knowledge_pack_installation",
             "metadata.identity_trailers": "provenance_metadata",
             "metadata.shipped_baseline_hashes": "provenance_metadata"}
    if logical_id in fixed:
        return fixed[logical_id]
    if logical_id.startswith("state.custom_tools."):
        return "custom_tools"
    if logical_id.startswith("state.audit."):
        return "audit_and_pending_actions"
    for family in ("skills", "personas", "avatars", "voices", "tool_profiles"):
        if logical_id.startswith(f"overlay.{family}."):
            return family
    if logical_id.startswith("overlay.projects."):
        for suffix in (".binding_json", ".chats_json", ".project_md", ".codebase_md"):
            if logical_id.endswith(suffix):
                return "project:" + logical_id[len("overlay.projects."):-len(suffix)]
    return None


def _assemble_units(members, provenance, notes=None):
    """Group validated/damaged members into units with dependency closure."""
    by_unit = {}
    for m in members:
        if m["unit"] is not None:
            by_unit.setdefault(m["unit"], []).append(m)
    primary = by_unit.get("primary_database", [])
    primary_ok = bool(primary) and all(m["state"] == "valid" for m in primary)
    units = []
    for name in sorted(by_unit):
        kind = name.split(":", 1)[0]
        disposition, description = _UNIT_POLICY[kind]
        recoverable, skipped = [], []
        for m in by_unit[name]:
            needs_db = (m["logical_id"].startswith(_NEEDS_PRIMARY_DB)
                        or m["logical_id"].endswith(".chats_json"))
            if m["state"] != "valid":
                skipped.append({"archive_path": m["archive_path"], "reason": m["damage"]})
            elif needs_db and not primary_ok:
                skipped.append({"archive_path": m["archive_path"],
                                "reason": "dependency_unavailable:primary_database"})
            else:
                recoverable.append(m["archive_path"])
        availability = ("unavailable" if not recoverable else "partial" if skipped else "complete")
        unit = {"unit": name, "disposition": disposition, "description": description,
                "availability": availability, "recoverable": sorted(recoverable),
                "skipped": sorted(skipped, key=lambda s: s["archive_path"]),
                "provenance": provenance}
        unit.update((notes or {}).get(kind, {}))
        if kind == "project":
            unit["binding"] = "historical_inactive_owner_must_repick_root"
        units.append(unit)
    return units


def _prefs_note(raw):
    if raw is None:
        return {}
    try:
        _clean, omitted, unknown = abx._scrub_preferences(raw)
    except ab.AgentBackupError:
        return {"availability_override": "unavailable", "prefs_policy": "types_invalid"}
    return {"security_fields_dropped": omitted - unknown, "unclassified_keys_omitted": unknown,
            "telegram_remote_access": "forced_off"}


def _apply_overrides(units):
    for unit in units:
        if unit.pop("availability_override", None) == "unavailable":
            unit["skipped"] = sorted(unit["skipped"] + [{"archive_path": p, "reason": "payload_policy"}
                                                        for p in unit["recoverable"]],
                                     key=lambda s: s["archive_path"])
            unit["recoverable"] = []
            unit["availability"] = "unavailable"
    return units


# --------------------------------------------------------------------------
# Manifest archives (A2 v1 and v2 export)
# --------------------------------------------------------------------------

def _format_of(manifest):
    if type(manifest) is not dict or manifest.get("format") != ab.MANIFEST_FORMAT:
        raise _Refused("FORMAT_UNSUPPORTED", "not a Lumina Agent Backup manifest")
    version = manifest.get("format_version")
    if abx._same_contract(version, _V1):
        return "agent_backup_v1"
    for known in abx.READ_VERSIONS:
        if abx._same_contract(version, known):
            return f"agent_export_v{known['major']}.{known['minor']}"
    raise _Refused("FORMAT_UNSUPPORTED", "unknown or newer format version")


def _member_state(z, names, m, manifest, v1):
    """(damage or None, bytes or None) for one declared member."""
    path = m.get("archive_path")
    if path not in names:
        return "missing", None
    data = _read(z, path)
    if data is None:
        return "crc_or_inflate_failed", None
    if len(data) != m.get("size") or hashlib.sha256(data).hexdigest() != m.get("sha256"):
        return "hash_mismatch", None
    if m.get("content_kind") == "sqlite_database":
        ok, _ = ab._verify_sqlite_payload(data, role=ab._SQLITE_ROLE_BY_LOGICAL_ID.get(m.get("logical_id")))
        if not ok:
            return "sqlite_invalid", None
    elif m.get("logical_id") == "state.preferences.prefs_json" and not ab._verify_prefs_payload(data)[0]:
        return "prefs_invalid", None
    if not v1 and abx.validate_export_payload(m, data, manifest):
        return "payload_policy", None
    return None, data


def _intake_manifest_archive(raw, z, infos):
    try:
        manifest = ab._strict_json_loads(z.read("manifest.json"))
    except Exception:
        raise _Refused("MANIFEST_INVALID") from None
    fmt = _format_of(manifest)
    v1 = fmt == "agent_backup_v1"
    report = (ab.verify_agent_backup(io.BytesIO(raw), resident_bytes_hint=len(raw)) if v1
              else abx.verify_export(io.BytesIO(raw), resident_bytes_hint=len(raw)))
    names = {i.filename for i in infos} - {"manifest.json"}
    if not report["valid"]:
        # Selective recovery needs an intact manifest, the canonical physical
        # layout and namespace; any doubt there rejects the whole archive.
        schema = ab._validate_manifest_schema(manifest) if v1 else abx.validate_export_schema(manifest)
        if schema:
            raise _Refused("MANIFEST_SCHEMA_INVALID", schema[0])
        if ab._validate_zip_physical_structure(raw, infos):
            raise _Refused("ZIP_STRUCTURE_UNSAFE", "not the canonical Agent Backup layout")
        if ab._check_path_and_identity_rules([{"archive_path": n, "logical_id": f"<physical:{n}>"}
                                              for n in names]):
            raise _Refused("ARCHIVE_NAMESPACE_INVALID")
        if names - {m["archive_path"] for m in manifest["members"]}:
            raise _Refused("ARCHIVE_UNDECLARED_MEMBER")
    members, payloads = [], {}
    for m in manifest["members"]:
        damage, data = _member_state(z, names, m, manifest, v1)
        if damage is None and m["logical_id"] in ("state.preferences.prefs_json", abx.MARKER_ID):
            payloads[m["logical_id"]] = data
        members.append({"logical_id": m["logical_id"], "archive_path": m["archive_path"],
                        "unit": _unit_of(m["logical_id"]), "state": "damaged" if damage else "valid",
                        "damage": damage})
    damaged = [m for m in members if m["state"] != "valid"]
    if report["valid"] and damaged:
        raise _Refused("ARCHIVE_INTEGRITY_FAILED", "verifier and member checks disagree")
    if not report["valid"] and not damaged:
        raise _Refused("ARCHIVE_INTEGRITY_FAILED", report["errors"][0] if report["errors"] else "")

    notes = {"preferences": _prefs_note(payloads.get("state.preferences.prefs_json"))}
    marker = "not_captured_by_format" if v1 else "unknown"
    if abx.MARKER_ID in payloads:
        marker = ab._strict_json_loads(payloads[abx.MARKER_ID])["source_status"]
    observations = manifest.get("export_observations", {})
    coverage = {
        "format_coverage": "older_full_mode" if v1 else "incomplete_state_export",
        "installation_history": marker,
        "c1_safety_state": "not_captured",
        "c1_at_source": (manifest.get("c1_exclusion", {}).get("status", "not_recorded") if not v1
                         else "not_recorded"),
        "unsupported_required_features": list(manifest.get("required_features", [])),
        "unclassified_preferences_omitted_at_export": observations.get("prefs_unclassified_count"),
        "source_overlays": "captured",
    }
    provenance = "archive_declared_hash_consistent"
    units = _apply_overrides(_assemble_units(members, provenance, notes))
    if marker != "present":
        # A valid record of "missing_unknown" is honest absence, not history
        # to adopt; it must never become first-run proof.
        for unit in units:
            if unit["unit"] == "knowledge_pack_installation" and unit["recoverable"]:
                unit["skipped"] = [{"archive_path": p, "reason": "history_missing_unknown"}
                                   for p in unit["recoverable"]] + unit["skipped"]
                unit["recoverable"], unit["availability"] = [], "unavailable"
    return {
        "kind": VERIFIED if report["valid"] else SELECTIVE,
        "format": fmt,
        "verification": {"status": "PASS" if report["valid"] else "FAILED",
                         "scope": "structure_hashes_sqlite_policy",
                         "error_count": len(report["errors"])},
        "members": members, "units": units, "coverage": coverage,
        "full_restore_eligible": False,
    }


# --------------------------------------------------------------------------
# Legacy (manifestless DATA_DIR) archives
# --------------------------------------------------------------------------

def _legacy_family(name):
    """(unit, sqlite_role) for an allowlisted legacy path, or a class label."""
    if c1.is_protected_relpath(name):
        return "c1_protected", None
    lower = name.casefold()
    parts = name.split("/")
    if ("credential" in lower or "ledger" in parts[-1].casefold()
            or any(p.casefold() in ab._FORBIDDEN_NAME_SEGMENTS for p in parts)):
        return "excluded", None
    if name in _LEGACY_FILES:
        return _LEGACY_FILES[name]
    for db in ("memory/lumina.db", "telemetry/flight_recorder.db"):
        if name in (db + s for s in _SQLITE_SIDECARS):
            return "sidecar", None
    if parts[0] == "custom_tools" and len(parts) >= 2 and name.endswith(".py"):
        return "custom_tools", None
    if parts[0] == "projects" and len(parts) == 3 and parts[2] in ("binding.json", "chats.json"):
        return "project:" + parts[1], None
    return "unclassified", None


def _intake_legacy(z, infos):
    names = [i.filename for i in infos]
    if any(n.startswith(_A2_PREFIXES) for n in names):
        raise _Refused("FORMAT_UNSUPPORTED", "structured Agent Backup layout without a manifest")
    if not any(n in names for n in _LEGACY_ANCHORS):
        raise _Refused("LEGACY_LAYOUT_UNRECOGNIZED")
    sizes = {i.filename: i.file_size for i in infos}
    counts = {"c1_protected": 0, "excluded": 0, "unclassified": 0, "sidecar": 0}
    members, payloads = [], {}
    for name in sorted(names):
        unit, role = _legacy_family(name)
        if unit in counts:
            counts[unit] += 1
            continue
        data = _read(z, name)
        damage = None
        if data is None:
            damage = "crc_or_inflate_failed"
        elif role is not None:
            # A non-empty WAL or hot journal means the database file alone is
            # not the generation the source last saw.
            if any(sizes.get(name + s, 0) for s in ("-wal", "-journal")):
                damage = "ambiguous_database_generation"
            elif not ab._verify_sqlite_payload(data, role=role)[0]:
                damage = "sqlite_invalid"
        elif name == "memory/prefs.json" and not ab._verify_prefs_payload(data)[0]:
            damage = "prefs_invalid"
        elif name.endswith(".json"):
            try:
                ab._strict_json_loads(data)
            except Exception:
                damage = "json_invalid"
        if damage is None and name in ("memory/prefs.json", "knowledge_packs_installed.json"):
            payloads[name] = data
        logical = ("overlay.projects." + name.split("/")[1] + ("." + name.split("/")[2].replace(".", "_"))
                   if unit.startswith("project:") else "legacy:" + name)
        members.append({"logical_id": logical, "archive_path": name, "unit": unit,
                        "state": "damaged" if damage else "valid", "damage": damage})
    marker = "missing_unknown"
    if "knowledge_packs_installed.json" in payloads:
        try:
            abx._marker_records(payloads["knowledge_packs_installed.json"])
            marker = "present"
        except ab.AgentBackupError:
            marker = "invalid"
            for m in members:
                if m["archive_path"] == "knowledge_packs_installed.json":
                    m["state"], m["damage"] = "damaged", "installation_marker_invalid"
    notes = {"preferences": _prefs_note(payloads.get("memory/prefs.json"))}
    units = _apply_overrides(_assemble_units(members, "legacy_unverified", notes))
    for unit in units:
        unit["integrity"] = "crc_and_payload_shape_only"
    absent = [{"unit": u, "availability": "unavailable", "reason": "not_in_legacy_format"}
              for u in ("skills", "personas", "avatars", "voices", "tool_profiles", "project_list")]
    return {
        "kind": LEGACY, "format": "legacy_memory_backup",
        "verification": {"status": "not_applicable", "scope": "no_manifest_no_hashes",
                         "error_count": 0},
        "members": members, "units": units, "units_not_in_archive": absent,
        "coverage": {
            "format_coverage": "legacy_data_folder_only",
            "installation_history": marker,
            "c1_safety_state": "evidence_only" if counts["c1_protected"] else "absent_unknown",
            "excluded_entries_present": counts["excluded"] > 0,
            "unclassified_entries": counts["unclassified"],
            "source_overlays": "not_in_legacy_format",
            "provenance": "unknown",
        },
        "full_restore_eligible": False,
    }


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def intake_archive(path):
    """Capture and classify one archive. Archive-only: no destination access.

    Returns a JSON-safe description; payload bytes are never retained or
    returned. Unsafe or unexplained input comes back as `rejected` with a
    refusal code, never as an exception and never with recovery units.
    """
    result = {"schema": INTAKE_SCHEMA, "version": VERSION, "source_path": os.path.abspath(os.fspath(path)),
              "archive_sha256": None, "archive_size": None, "refusal": None}
    try:
        raw = _capture(path)
        result.update(archive_sha256=hashlib.sha256(raw).hexdigest(), archive_size=len(raw))
        try:
            z = zipfile.ZipFile(io.BytesIO(raw))
        except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, OSError):
            raise _Refused("NOT_A_ZIP") from None
        with z:
            infos = z.infolist()
            _check_grammar(raw, infos)
            if "manifest.json" in {i.filename for i in infos}:
                result.update(_intake_manifest_archive(raw, z, infos))
            else:
                result.update(_intake_legacy(z, infos))
    except _Refused as refusal:
        result.update(kind=REJECTED, format=None, refusal={"code": refusal.code, "detail": refusal.detail},
                      members=[], units=[], full_restore_eligible=False)
    except Exception as exc:  # hostile input must never crash the caller
        result.update(kind=REJECTED, format=None,
                      refusal={"code": "ARCHIVE_INTEGRITY_FAILED", "detail": type(exc).__name__},
                      members=[], units=[], full_restore_eligible=False)
    result["intake_digest"] = _digest({k: v for k, v in result.items() if k != "source_path"})
    return result


def _lstat_under(root, rel):
    """'absent' | 'file' | 'dir' | 'other' | 'unsafe_alias', never following links."""
    path = root
    parts = rel.split("/")
    for i, part in enumerate(parts):
        path = os.path.join(path, part)
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return "absent", None
        except OSError:
            return "unobservable", None
        if stat.S_ISLNK(st.st_mode):
            return "unsafe_alias", None
        if i < len(parts) - 1 and not stat.S_ISDIR(st.st_mode):
            return "unsafe_alias", None
    kind = "file" if stat.S_ISREG(st.st_mode) else "dir" if stat.S_ISDIR(st.st_mode) else "other"
    return kind, (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def _root_identity(path, label):
    if path is None:
        return {"state": "not_surveyed"}
    path = os.path.abspath(os.fspath(path))
    refuse_if_production_path(path)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return {"state": "absent", "path": path}
    except OSError:
        raise _Refused("INPUT_UNAVAILABLE", label) from None
    if not stat.S_ISDIR(st.st_mode):
        raise _Refused("INPUT_NOT_REGULAR", label + " is not a real directory")
    return {"state": "present", "path": path, "identity": [st.st_dev, st.st_ino]}


def _targets(unit, recoverable):
    kind, _, name = unit.partition(":")
    if kind in _DATA_TARGETS:
        return [("data", rel) for rel in _DATA_TARGETS[kind]]
    if kind == "custom_tools":
        return [("data", "custom_tools")]
    if kind == "project":
        return [("data", f"projects/{name}"), ("base", f"projects/{name}")]
    if kind == "project_list":
        return [("base", "projects/projectlist.md")]
    if kind in _BASE_DIRS:
        return [("base", f"{_BASE_DIRS[kind]}/{p.rsplit('/', 1)[-1]}") for p in recoverable]
    return []


def _destination_c1(data_root):
    if data_root["state"] != "present":
        return {"status": "absent_unknown", "plan": "restore_trace_required_then_owner_recover"}
    try:
        exclusion = c1.describe_exclusions(data_root["path"])
    except c1.ExclusionUnavailable as exc:
        raise _Refused("INPUT_UNAVAILABLE", "destination C1 state unobservable: " + exc.code) from None
    if exclusion.status == "present_excluded":
        return {"status": "present", "plan": "destination_authoritative_unchanged",
                "identity_pin": ("present" if any(e.relpath == "memory/" + c1.IDENTITY_NAME
                                                  for e in exclusion.entries) else "absent")}
    return {"status": "absent_unknown", "plan": "restore_trace_required_then_owner_recover"}


def preview_recovery(intake, data_dir, base_dir=None):
    """Non-destructive recovery preview for one intake and one destination.

    Destination access is lstat/scandir only. The preview binds the archive
    hash, the intake and the destination observation into `plan_digest`; a
    future activation must recompute it (revalidate_preview) and stop on any
    difference. A preview is never consent and never permission to write.
    """
    if type(intake) is not dict or intake.get("schema") != INTAKE_SCHEMA:
        raise TypeError("intake must come from intake_archive()")
    preview = {"schema": PREVIEW_SCHEMA, "version": VERSION,
               "archive": {k: copy.deepcopy(intake.get(k)) for k in
                           ("archive_sha256", "archive_size", "kind", "format", "verification",
                            "refusal", "coverage", "intake_digest")},
               "writes_performed": False, "full_restore_eligible": False,
               "activation": {"available": False, "gate": list(ACTIVATION_GATE), "blockers": []}}
    try:
        if intake["kind"] == REJECTED:
            raise _Refused(intake["refusal"]["code"], intake["refusal"]["detail"])
        data_root = _root_identity(data_dir, "data folder")
        base_root = _root_identity(base_dir, "install folder")
        roots = {"data": data_root, "base": base_root}
        units, observed = [], {}
        for source_unit in intake["units"]:
            unit = copy.deepcopy(source_unit)
            states = []
            for root_label, rel in _targets(unit["unit"], unit["recoverable"]):
                root = roots[root_label]
                if root["state"] == "not_surveyed":
                    states.append("not_surveyed")
                    continue
                if root["state"] == "absent":
                    states.append("absent")
                    continue
                kind, ident = _lstat_under(root["path"], rel)
                observed[f"{root_label}:{rel}"] = [kind, ident]
                states.append(kind)
            if unit["availability"] == "unavailable":
                effect = "nothing_to_recover"
            elif any(s in ("unsafe_alias", "unobservable", "other") for s in states):
                effect = "blocked_unsafe_destination_path"
            elif "not_surveyed" in states:
                effect = "not_surveyed"
            elif any(s in ("file", "dir") for s in states):
                effect = "conflict_owner_choice_and_rollback_snapshot_required"
            else:
                effect = "would_create"
            if unit["disposition"] != "recover" and effect != "nothing_to_recover":
                effect = "preserve_as_inactive_history"
            unit["destination_effect"] = effect
            units.append(unit)
        destination_c1 = _destination_c1(data_root)
        coverage = intake.get("coverage", {})
        blockers = ["restore_engine_not_implemented"]
        if intake["kind"] == SELECTIVE:
            blockers.append("full_restore_refused_damaged_members")
        if intake["kind"] == LEGACY:
            blockers.append("legacy_provenance_unknown")
        if coverage.get("unsupported_required_features"):
            blockers.append("unsupported_required_features")
        if coverage.get("installation_history") != "present":
            blockers.append("bootstrap_hold_installation_history_unknown")
        if any(u["destination_effect"] == "blocked_unsafe_destination_path" for u in units):
            blockers.append("unsafe_destination_path")
        preview.update(status="preview", units=units,
                       units_not_in_archive=copy.deepcopy(intake.get("units_not_in_archive", [])),
                       destination={"data": data_root, "base": base_root, "observed": observed},
                       safety={"c1": destination_c1,
                               "archived_c1_state": "never_restored_as_live_files",
                               "always_excluded": list(_ALWAYS_EXCLUDED),
                               "executable_authority": "historical_inactive_reapproval_required",
                               "telegram_remote_access": "forced_off",
                               "quiescence": "not_evaluated_in_preview"})
        preview["activation"]["blockers"] = blockers
    except _Refused as refusal:
        preview.update(status="refused", units=[], units_not_in_archive=[],
                       refusal={"code": refusal.code, "detail": refusal.detail})
        preview["activation"]["blockers"] = ["input_or_destination_refused"]
    preview["plan_digest"] = _plan_digest(preview)
    return preview


def _plan_digest(preview):
    bound = {k: v for k, v in preview.items() if k != "plan_digest"}
    return _digest(bound)


def revalidate_preview(preview, archive_path, data_dir, base_dir=None):
    """True only if a fresh intake and preview reproduce `plan_digest` exactly.

    This is the entry check a future activation slice runs after quiescence;
    any change to the archive bytes, its classification, or the destination
    observation since the owner saw the preview returns False.
    """
    if type(preview) is not dict or preview.get("schema") != PREVIEW_SCHEMA:
        return False
    if preview.get("plan_digest") != _plan_digest(preview):
        return False
    fresh = preview_recovery(intake_archive(archive_path), data_dir, base_dir)
    return fresh["status"] == "preview" and fresh["plan_digest"] == preview["plan_digest"]
