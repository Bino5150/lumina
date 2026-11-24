"""Agent Backup export formats 2.2 (Incomplete State Export) and 2.3
(Verified Agent Backup); no Restore and no live C1 state.

Format 2.2 (AGENT-BACKUP-RESTORE-PHASE-B S3) adds, for new exports only:
generated-image blobs and their manifest sidecars as hash-bound members,
checked row by row against the archived database; the data-root inventory
and its owner-declared exclusions (D1-D4) in export_observations; and the
installation-local preference count. 2.0 and 2.1 stay readable unchanged.

Format 2.3 (C1-S3a, C1S3-AMENDMENT-A1) is the Verified Agent Backup. It is
2.2 plus one hash-bound member, C1's sanitized recovery record, captured in
a child process (core/agent_backup_c1_child.py) so the agent's own process
never does that work. It runs every 2.2 check (A-1) and then the C1 ones.
The verifier alone derives its coverage from the archived bytes, and an
archive with any coverage blocker can never verify as 2.3, so a verified 2.3
archive is a Verified backup by construction. The record is derived,
sanitized history, never live safety state. Restore (C1-S3b) reads 2.3 into
an isolated root and keeps the record only as inactive history; "Verified"
never means "restorable".

The old v1 builder/verifier remain unchanged by default. The small private v2
hooks in agent_backup reuse its collectors, immutable capture, ZIP grammar,
resource limits, publication and rollback. This module owns only the new policy.

Explicitly acknowledged Incomplete State Export uses major 2, a new
destination, and separate coverage/compatibility/acceptance evidence. Neither
format nor integrity PASS authorizes Restore or runtime grants.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import re
import selectors
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime, timezone
import zipfile

from core import agent_backup as ab
from core import agent_backup_c1_child as c1_child
from core import agent_backup_inventory as inv
from core import agent_backup_references as refs
from core import suspension_backup as c1
from core.test_isolation import refuse_if_production_path

V21 = {"major": 2, "minor": 1}
VERSION = {"major": 2, "minor": 2}             # Incomplete State Export
VERIFIED_VERSION = {"major": 2, "minor": 3}    # Verified Agent Backup
# Formats Restore may read. C1-S3b adds 2.3, with its own per-format plan
# and receipt values (TS3-05); the C1 record restores as history only.
READ_VERSIONS = ({"major": 2, "minor": 0}, V21, VERSION, VERIFIED_VERSION)
# Formats this verifier reads: the same set.
VERIFY_VERSIONS = READ_VERSIONS
C1_ARCHIVE_PREFIX = "state/c1/"
# Namespace the 2.x verifier accepts beyond v1's (v1 itself never does).
EXTRA_ARCHIVE_PREFIXES = (inv.GENERATION_ARCHIVE_PREFIX, C1_ARCHIVE_PREFIX)
C1_MEMBER_ID = "state.c1.recovery_state"
C1_MEMBER_POLICY = {
    "archive_path": C1_ARCHIVE_PREFIX + "recovery_state.json", "state_class": "REQUIRED_AGENT_STATE",
    "content_kind": "json", "required": True, "portable": "PORTABLE",
    "restore_policy": "history_only_never_live_c1_state",
}
C1_OBSERVATION_FIELDS = frozenset({
    "status", "record_sha256", "record_schema", "record_version", "claimed_source_incarnation",
    "audit_truncated", "incidents_truncated", "runtimes_truncated", "redaction_total"})
C1_EXCLUSION_V2_CONSTANTS = {
    "schema": c1.SCHEMA, "version": 2, "status": "present_excluded", "raw_store": "excluded",
    "sanitized_record": "see_c1_recovery", "enforcement_compatibility": "unproven",
    "restore_fence": "c1_trace_and_owner_recover_required",
}
C1_LIMITATIONS = (
    "snapshot_predates_later_trips_recovery_or_replacement",
    "process_local_unconfirmed_trips_not_included",
    "captured_at_is_a_claimed_lower_bound",
    "unknown_evidence_keys_dropped_and_counted",
    "restore_starts_every_scope_suspended",
    "members_captured_at_different_times",
)
_HEX32 = re.compile(r"[0-9a-f]{32}")
_C1_OBSERVATION_CACHE_MAX = 4
_c1_observation_cache: dict = {}
_c1_observation_lock = threading.Lock()
GENERATION_BLOB_POLICY = {
    "state_class": "REQUIRED_AGENT_STATE", "content_kind": "binary", "required": True,
    "portable": "PORTABLE_WITH_REBIND",
    "restore_policy": "restore_into_isolated_root_hash_verified_against_row",
    "rebind_policy": "rebind_local_path_to_destination_root_by_artifact_id",
}
GENERATION_MANIFEST_POLICY = {
    "state_class": "REQUIRED_AGENT_STATE", "content_kind": "json", "required": True,
    "portable": "PORTABLE", "restore_policy": "preserve_verbatim_provenance_never_rewritten",
}
_GEN_ID = re.compile(r"state\.artifacts\.generation\.([0-9a-f]{32})(\.manifest_json)?")
GENERATION_STATUSES = ("complete", "blocked", "unavailable")
_GEN_FINDING_CODES = frozenset({refs.MISSING_BLOB, refs.NOT_A_FILE, refs.SIZE_MISMATCH,
                                refs.HASH_MISMATCH, refs.UNSAFE_ID, refs.UNEXPECTED_ENTRY,
                                refs.FOREIGN_LOCAL_PATH, refs.ORPHAN_FILE, refs.TMP_LEFTOVER,
                                "changed_during_capture", "manifest_invalid"})
_GEN_BLOCKING = refs.BLOCKING | {"changed_during_capture", "manifest_invalid"}
MAX_GENERATION_MANIFEST_BYTES = 1 << 20
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

# Ordinary preferences (F-DR-4, owner direction 2026-11-24): a Verified backup
# preserves every ordinary setting this version knows, so a recovered copy keeps
# its voice, persona, avatar, model, limit and display choices. Backing a
# setting up never activates it: a recovered copy starts with every scope
# suspended until the owner recovers C1 and resumes it (C1-S2), Telegram is
# forced off, Wire settings stay installation-local, and credentials, PINs and
# owner bindings are never archived. Settings naming things the new machine may
# lack (a voice engine, an avatar file, a model endpoint) are kept as values and
# degrade at use. tests/test_f_dr_4_prefs_classification.py keeps every key the
# product reads or writes classified here.
_TEXT_PREFS = frozenset({
    "human_bio", "human_profile_curated", "human_bio_public", "last_persona",
    "tts_backend", "tts_voice", "tts_device", "stt_backend", "stt_model",
    "stt_device", "reasoning_effort", "theme", "language", "user_name",
    "agent_name", "system_prompt", "avatar_path", "user_avatar_path",
    "voicebox_profile", "voicebox_instruct", "chatterbox_voice", "chatterbox_ref_dir",
    "chatterbox_device", "supertonic_voice", "elevenlabs_voice_id", "elevenlabs_model",
    "elevenlabs_output_format", "llm_backend", "custom_default_model", "omniroute_default_model",
})
_BOOL_PREFS = frozenset({
    "tts_enabled", "stt_enabled", "spellcheck_enabled", "show_think_blocks",
    "subagents_enabled", "background_tasks_enabled", "dream_sweep_enabled",
    "human_profile_curation_enabled", "context_compaction_enabled", "backend_endpoints_migrated",
})
# Checked before the secret-name rule: these four are numeric limits whose
# names contain "token", not credentials.
_NUMBER_PREFS = frozenset({
    "window_width", "window_height", "tts_speed", "tts_pitch", "tts_volume", "last_chat_id",
    "max_tool_iterations", "max_tool_calls_per_response", "max_tool_calls_per_turn",
    "max_turn_minutes", "max_subagent_depth", "no_progress_read_only_warn",
    "no_progress_read_only_trip", "no_progress_write_warn", "no_progress_write_trip",
    "dream_idle_minutes", "dream_min_tokens", "tool_budget_tokens", "response_reserve_tokens",
    "context_compaction_batch_tokens",
})
# Endpoints are kept, except one that carries a credential in its URL (user
# info, or a key/token-like query parameter): that whole setting is dropped as
# a security field.
_ENDPOINT_PREFS = frozenset({"tts_host", "voicebox_host", "chatterbox_host", "supertonic_host",
                             "llm_backend_url"})
# Structured settings: plain JSON (objects, lists, strings, numbers, booleans,
# null) within the bounds below. A value outside them is left out and counted,
# never archived.
_STRUCTURED_PREFS = {
    "disabled_tools": list, "multimodal_disabled_providers": list, "multimodal_routes": dict,
    "backend_endpoints": dict, "backend_models": dict, "backend_provider_routing": dict,
    "backend_reasoning": dict, "backend_context": dict,
    # FE-09 moved provider keys out of this block; what remains is each
    # provider's model choice. A block still holding a key-like field is
    # dropped whole as a security field.
    "cloud_credentials": dict,
}
_PREF_VALUE_MAX_BYTES = 256 * 1024
_PREF_JSON_MAX_DEPTH = 6
_SECRET_NAME_PARTS = ("api_key", "token", "secret", "password")
# Nested field names and values that may hold a credential (review L1). Names
# are compared as lower-case words (camelCase, hyphens and underscores split)
# and as one joined string, so accessToken, x-api-key and client_secret all
# match; plural "tokens" (context and budget sizes) is not a credential word.
_SECRET_WORDS = frozenset({"key", "token", "auth", "authorization", "pass", "passwd", "password",
                           "passphrase", "pin", "secret", "sig", "signature", "bearer", "cookie",
                           "session", "credential", "credentials"})
_SECRET_JOINED = ("apikey", "accesstoken", "refreshtoken", "authtoken", "idtoken", "privatekey",
                  "clientsecret", "secretkey", "password", "passwd")
_SECRET_VALUE = re.compile(r"(?i)^(?:bearer\s+\S|sk-|sk_|rk_|pk_live_|xox[abpr]-|ghp_|gho_|github_pat_"
                           r"|aiza|glpat-|hf_|eyj[a-z0-9_-]{8,}\.)")
_DROP_PREFS = frozenset({
    "pin_hash", "pin_salt", "telegram_remote_access",
    "telegram_remote_access_on_since", "telegram_remote_access_off_reason",
    "telegram_owner_chat_id", "telegram_chat_id", "owner_chat_id",
})
# Installation-local Wire state (owner decision W-A, 2026-11-19). These keys
# belong to the installation that holds the Wire seat, so they are never
# archived and never restored. Absent means OFF (comms/wire_autonomy.py
# PREF_ENABLED; ui/main_window.py _wire_wake_enabled), so a recovered
# installation starts with Wire autonomy and the wake listener off, and
# enabling either later captures a fresh activation watermark. Any other
# wire_* key stays unclassified: this list is exact, not a prefix rule.
_DESTINATION_LOCAL_PREFS = frozenset({
    "wire_autonomy", "wire_autonomy_watermark", "wire_autonomy_spend",
    "wire_autonomy_tokens_per_hour", "wire_autonomy_tokens_per_day", "wire_wake_listener",
})


_CODE_SHAPE = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+")


class ExportUnavailable(ab.AgentBackupError):
    """Coverage is unavailable; no production artifact is published.

    `code` is for tests and diagnostics. Verified-export refusals carry owner
    words as their message; owner_message() turns any older code-only
    message into owner words too (TS3-10)."""

    def __init__(self, message, *, code=None, detail=None):
        super().__init__(message)
        self.code = code if code is not None else (message if type(message) is str
                                                   and _CODE_SHAPE.fullmatch(message) else None)
        self.detail = detail


# Owner words for every refusal code (TS3-10, amendment A-7). Never echoes a
# raw code, a path or an archive payload.
_NOT_WRITTEN = " No archive was written. Existing files are preserved."
_INCOMPLETE_HINT = (" Incomplete State Export remains available and says exactly what it leaves out.")
OWNER_MESSAGES = {
    "C1_STATE_ABSENT": (
        "Verified Agent Backup was not written: this data folder has no safety-suspension state "
        "to capture. Missing state is never proof of a first installation." + _NOT_WRITTEN + _INCOMPLETE_HINT),
    "C1_SNAPSHOT_CHILD_FAILED": (
        "Verified Agent Backup was not written: the separate process that captures the "
        "safety-suspension state did not finish cleanly." + _NOT_WRITTEN),
    "C1_SNAPSHOT_REFUSED": (
        "Verified Agent Backup was not written: the safety-suspension state could not be captured "
        "consistently. It may be damaged, written by a newer version, changing, or larger than a "
        "backup may carry." + _NOT_WRITTEN + _INCOMPLETE_HINT),
    "C1_SNAPSHOT_BOUNDARY": (
        "Verified Agent Backup was not written: the safety-suspension files changed or could not be "
        "observed while they were being captured. Try again." + _NOT_WRITTEN),
    "C1_SNAPSHOT_STORE_TOO_LARGE": (
        "Verified Agent Backup was not written: the safety-suspension store is larger than a backup "
        "may copy." + _NOT_WRITTEN + _INCOMPLETE_HINT),
    "C1_SNAPSHOT_CHILD_LIMITS_UNAVAILABLE": (
        "Verified Agent Backup was not written: this system could not give the safety-suspension "
        "capture its time and memory limits, so it was not run." + _NOT_WRITTEN),
    "C1_SNAPSHOT_STORE_REPLACED": (
        "Verified Agent Backup was not written: the safety-suspension store was replaced while the "
        "backup was being made. Try again." + _NOT_WRITTEN),
    "C1_SNAPSHOT_RECORD_INVALID": (
        "Verified Agent Backup was not written: the captured safety-suspension record failed its "
        "strict check." + _NOT_WRITTEN),
    "C1_EXCLUSION": (
        "Agent Backup was not written: the protected safety-suspension files changed or could not be "
        "observed while the backup was being made. Try again." + _NOT_WRITTEN),
    "C1_PROTECTED": (
        "Agent Backup was not written: a source pointed into the protected safety-suspension files, "
        "which a backup never copies." + _NOT_WRITTEN),
    "EXPORT_FAILED": (
        "Agent Backup failed validation or publication. No verified success was recorded. Keep any "
        "prior backup and recovery evidence."),
}
BLOCKER_WORDS = {
    "suspension": "the safety-suspension state was not captured",
    "installation_marker": "the Knowledge Pack installation record is missing",
    "generation_artifacts": "some generated images or their records are missing or changed",
    "inventory": "some files in the data folder still need a backup decision, or the folder could not be listed",
}


def _owner_words(code):
    if code in OWNER_MESSAGES:
        return OWNER_MESSAGES[code]
    for prefix, key in (("C1_SNAPSHOT_EXCLUSION_", "C1_SNAPSHOT_BOUNDARY"),
                        ("C1_SNAPSHOT_", "C1_SNAPSHOT_REFUSED"),
                        ("C1_EXCLUSION_", "C1_EXCLUSION"), ("C1_PROTECTED_", "C1_PROTECTED")):
        if code.startswith(prefix):
            return OWNER_MESSAGES[key]
    return OWNER_MESSAGES["EXPORT_FAILED"]


def owner_message(exc):
    """What the owner sees for a refused export: owner words, never a code."""
    text = str(exc)
    head = text.split(":", 1)[0]
    if _CODE_SHAPE.fullmatch(head):
        return _owner_words(head)
    return text


def _blocked_message(blockers):
    absence = (" Missing state is never proof of a first installation."
               if {"suspension", "installation_marker"} & set(blockers) else "")
    return ("Verified Agent Backup was not written because "
            + "; ".join(BLOCKER_WORDS[b] for b in blockers) + "." + absence + _NOT_WRITTEN + _INCOMPLETE_HINT)


def _json_bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")


def _same_contract(actual, expected):
    # JSON booleans and integers are different policy values even though Python
    # equality treats False == 0 and True == 1 as equal.
    return _json_bytes(actual) == _json_bytes(expected)


def _v22_family(version):
    """2.2 and 2.3 share every 2.2 rule; 2.3 adds the C1 ones (TS3-01, A-1).
    The only place a verifier rule names either version."""
    return _same_contract(version, VERSION) or _same_contract(version, VERIFIED_VERSION)


def _is_verified_format(version):
    return _same_contract(version, VERIFIED_VERSION)


# -- C1 recovery record: one path from bytes to an observation (A-5, TS3-11) --

class C1RecordInvalid(ValueError):
    """The bytes are not one strict, canonical, valid C1 recovery record."""


def _c1_record_facts(raw):
    """(observation json, soul_id, incarnation) for exact record bytes, from
    the full strict check: strict parse, validate_recovery_state and exact
    canonical equality. Results are kept under the bytes' full sha256 only,
    so a repeat can only answer for bytes that already passed (A-4)."""
    if type(raw) is not bytes or not raw or len(raw) > c1.RECOVERY_STATE_MAX_BYTES:
        raise C1RecordInvalid("record size")
    digest = hashlib.sha256(raw).hexdigest()
    with _c1_observation_lock:
        cached = _c1_observation_cache.get(digest)
    if cached is not None:
        return cached
    try:
        record = ab._strict_json_loads(raw)
        c1.validate_recovery_state(record)
        canonical = c1.canonical_recovery_payload(record)
    except (c1.RecoverySnapshotRefused, ValueError, TypeError, KeyError, RecursionError,
            MemoryError, UnicodeError) as exc:
        raise C1RecordInvalid(type(exc).__name__) from None
    if canonical != raw:
        raise C1RecordInvalid("not the canonical form")
    prov = record["provenance"]
    observation = {
        "status": "captured_sanitized", "record_sha256": digest,
        "record_schema": record["schema"], "record_version": record["version"],
        "claimed_source_incarnation": prov["claimed_source_incarnation"],
        "audit_truncated": record["audit_carried"]["truncated"],
        "incidents_truncated": record["incidents_carried"]["truncated"],
        "runtimes_truncated": record["runtimes_carried"]["truncated"],
        "redaction_total": sum(record["redactions"].values()),
    }
    facts = (_json_bytes(observation), prov["claimed_source_soul_id"], prov["claimed_source_incarnation"])
    with _c1_observation_lock:
        while len(_c1_observation_cache) >= _C1_OBSERVATION_CACHE_MAX:
            _c1_observation_cache.pop(next(iter(_c1_observation_cache)))
        _c1_observation_cache[digest] = facts
    return facts


def c1_observation_from_bytes(raw):
    """The c1_recovery observation derived from exact record bytes. Builder
    and verifier both use this; neither writes observation fields by hand."""
    return json.loads(_c1_record_facts(raw)[0])


def _valid_c1_observation(value):
    return (type(value) is dict and set(value) == C1_OBSERVATION_FIELDS
            and value["status"] == "captured_sanitized"
            and type(value["record_sha256"]) is str and ab._SHA256_RE.fullmatch(value["record_sha256"]) is not None
            and value["record_schema"] == c1.RECOVERY_STATE_SCHEMA
            and _same_contract(value["record_version"], c1.RECOVERY_STATE_VERSION)
            and type(value["claimed_source_incarnation"]) is str
            and _HEX32.fullmatch(value["claimed_source_incarnation"]) is not None
            and all(type(value[k]) is bool for k in ("audit_truncated", "incidents_truncated",
                                                     "runtimes_truncated"))
            and _nonneg_int(value["redaction_total"]))


# -- the C1 child process (G-1, G-2; A-2) -------------------------------------

_CODE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _child_command(mode, data_dir):
    exe = sys.executable
    if type(exe) is not str or not exe or not os.path.isabs(exe):
        raise OSError("no usable interpreter")
    return [exe, "-E", "-s", "-B", "-m", "core.agent_backup_c1_child", mode, os.fspath(data_dir)]


def _child_env():
    env = {"PATH": os.defpath, "LANG": "C.UTF-8"}
    if os.environ.get("LUMINA_TESTING") == "1":
        env["LUMINA_TESTING"] = "1"   # keeps the test-isolation refusals live in the child
    return env


def _child_failed(detail):
    return ExportUnavailable(OWNER_MESSAGES["C1_SNAPSHOT_CHILD_FAILED"],
                             code="C1_SNAPSHOT_CHILD_FAILED", detail=detail)


def _kill_child(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def _await_exit_unreaped(proc, deadline):
    """Wait for the child to exit without reaping it, so its pid (and with it
    its process group id) stays reserved until the group has been killed
    (Sonnet S3A-01). False on timeout."""
    if not hasattr(os, "waitid"):
        try:
            proc.wait(timeout=max(0.05, deadline - time.monotonic()))
            return True
        except subprocess.TimeoutExpired:
            return False
    while True:
        if os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.005)


def _run_c1_child(mode, data_dir, *, timeout, cap):
    """(exit code, stdout bytes) of one bounded child run. Raises
    ExportUnavailable on a spawn failure, timeout, overflow or signal. On
    every path the child's whole process group is killed while the child is
    still unreaped, then the child is reaped, before this returns or raises."""
    try:
        proc = subprocess.Popen(
            _child_command(mode, data_dir), cwd=_CODE_ROOT, env=_child_env(),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            close_fds=True, start_new_session=True)
    except (OSError, ValueError, subprocess.SubprocessError):
        raise _child_failed("spawn") from None
    failure = None
    out = bytearray()
    try:
        deadline = time.monotonic() + timeout
        fd = proc.stdout.fileno()
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failure = "timeout"
                    break
                if not selector.select(remaining):
                    continue
                chunk = os.read(fd, 1 << 16)
                if not chunk:
                    break
                out += chunk
                if len(out) > cap:
                    failure = "overflow"
                    break
        if failure is None and not _await_exit_unreaped(proc, deadline):
            failure = "timeout"
    finally:
        _kill_child(proc)          # the whole group: no grandchild outlives the call
        proc.wait()
        proc.stdout.close()
    if failure is not None:
        raise _child_failed(failure)
    if proc.returncode < 0:
        raise _child_failed("signal")
    return proc.returncode, bytes(out)


def _child_code_line(out, allowed):
    if len(out) > c1_child.CODE_LINE_MAX_BYTES or not out.endswith(b"\n") or out.count(b"\n") != 1:
        return None
    try:
        code = out[:-1].decode("ascii")
    except UnicodeDecodeError:
        return None
    return code if code in allowed else None


def _child_result(mode, returncode, out):
    """Map one child result to its bytes or a refusal (closed sets only)."""
    if returncode == c1_child.EXIT_OK:
        if mode == "capture" and out:
            return out
        if mode == "incarnation" and len(out) <= c1_child.INCARNATION_LINE_MAX_BYTES:
            parts = out[:-1].split(b" ") if out.endswith(b"\n") else []
            if len(parts) == 2 and all(_HEX32.fullmatch(p.decode("ascii", "replace")) for p in parts):
                return tuple(p.decode("ascii") for p in parts)
        raise _child_failed("garbage")
    for exit_code, allowed, prefix in (
            (c1_child.EXIT_REFUSED, c1.SNAPSHOT_REFUSALS, "C1_SNAPSHOT_"),
            (c1_child.EXIT_BOUNDARY, c1.REFUSALS, "C1_SNAPSHOT_EXCLUSION_"),
            (c1_child.EXIT_LIMIT, c1_child.LIMIT_CODES - {"USAGE"}, "C1_SNAPSHOT_")):
        if returncode == exit_code:
            code = _child_code_line(out, allowed)
            if code is None:
                raise _child_failed("garbage")
            raise ExportUnavailable(_owner_words(prefix + code), code=prefix + code)
    raise _child_failed("exit")


def _capture_c1_record(data_dir):
    """C1's canonical record bytes, captured by the child process."""
    returncode, out = _run_c1_child("capture", data_dir, timeout=c1_child.CAPTURE_TIMEOUT_S,
                                    cap=c1.RECOVERY_STATE_MAX_BYTES)
    return _child_result("capture", returncode, out)


def _observe_c1_incarnation(data_dir):
    returncode, out = _run_c1_child("incarnation", data_dir, timeout=c1_child.INCARNATION_TIMEOUT_S,
                                    cap=c1_child.INCARNATION_LINE_MAX_BYTES)
    return _child_result("incarnation", returncode, out)


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


def c1_exclusion_record_v2(record_v1):
    """The 2.3 exclusion record (TS3-07, A-9): the raw store is excluded and
    the sanitized record is described by c1_recovery. It never says
    `snapshot_captured`, which in v1 means the raw store."""
    return {**C1_EXCLUSION_V2_CONSTANTS, "observed_at": record_v1["observed_at"],
            "warnings": list(record_v1["warnings"])}


def _valid_c1_record(record, *, v2=False):
    # Archive declarations carry no runtime authority. Pin the owned record's
    # exact shape and constants; do not re-observe the destination while reading.
    # v1 pairs with 2.0-2.2 and v2 with 2.3 only (A-9).
    if type(record) is not dict:
        return False
    if v2:
        expected = dict(C1_EXCLUSION_V2_CONSTANTS)
        expected.pop("status")
    else:
        expected = {"schema": c1.SCHEMA, "version": c1.VERSION,
                    "snapshot_captured": False, "enforcement_compatibility": "unproven",
                    "c1_schema_version": "unknown", "suspended_at_capture": "unknown",
                    "restore_fence": "c1_trace_and_owner_recover_required"}
    if set(record) != set(expected) | {"status", "observed_at", "warnings"}:
        return False
    if any(not _same_contract(record[k], v) for k, v in expected.items()):
        return False
    if v2 and record["status"] != "present_excluded":
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
    """(scrubbed bytes, omitted, unclassified). See _scrub_preferences_counts."""
    data, counts = _scrub_preferences_counts(raw)
    return data, counts["omitted"], counts["unclassified"]


def _name_words(name):
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    words = [w for w in re.split(r"[^a-z0-9]+", spaced.lower()) if w]
    return words, "".join(words)


def _url_carries_secret(value) -> bool:
    """User info, or a query parameter named like a credential (review L3:
    parameter names, not substrings of the whole query)."""
    try:
        parts = urllib.parse.urlsplit(value)
        params = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    except ValueError:
        return True
    return "@" in parts.netloc or any(_secret_like_name(k) for k, _v in params)


def _endpoint_ok(value) -> bool:
    """An endpoint a recovered copy may keep: http(s) with a host (review L2)."""
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def _secret_like_name(name) -> bool:
    """A field name that may hold a credential."""
    words, joined = _name_words(name)
    return bool(_SECRET_WORDS.intersection(words)) or any(j in joined for j in _SECRET_JOINED)


def _nested_secret(value) -> bool:
    """A key-like field name, or a string shaped like a known credential
    (sk-…, a bearer header, a JWT…), anywhere inside a structured setting."""
    if type(value) is dict:
        return any(_secret_like_name(k) or _nested_secret(v) for k, v in value.items())
    if type(value) is list:
        return any(_nested_secret(v) for v in value)
    return type(value) is str and _SECRET_VALUE.match(value.strip()) is not None


def _inert_json(value, depth=0) -> bool:
    if depth > _PREF_JSON_MAX_DEPTH:
        return False
    if value is None or type(value) in (str, bool, int):
        return True
    if type(value) is float:
        return value == value and value not in (float("inf"), float("-inf"))
    if type(value) is list:
        return all(_inert_json(v, depth + 1) for v in value)
    if type(value) is dict:
        return all(type(k) is str and _inert_json(v, depth + 1) for k, v in value.items())
    return False


def _pref_disposition(key, value):
    """keep, security (left out: a credential or owner authority), local
    (installation-local Wire state), or unclassified (left out and counted).
    Raises EXPORT_PREFS_TYPE_INVALID for a known scalar of the wrong type."""
    if key in _DESTINATION_LOCAL_PREFS:
        return "local"
    if key in _DROP_PREFS:
        return "security"
    if key in _ENDPOINT_PREFS:
        if not (type(value) is str or value is None):
            raise ab.AgentBackupError("EXPORT_PREFS_TYPE_INVALID")
        if value and _url_carries_secret(value):
            return "security"
        return "keep" if not value or _endpoint_ok(value) else "unclassified"
    if key in _TEXT_PREFS:
        valid = type(value) is str or value is None
    elif key in _BOOL_PREFS:
        valid = type(value) is bool
    elif key in _NUMBER_PREFS:
        valid = type(value) in (int, float) or value is None
    elif key in _STRUCTURED_PREFS:
        if type(value) is not _STRUCTURED_PREFS[key] or not _inert_json(value):
            return "unclassified"
        if _nested_secret(value):
            return "security"
        if key == "backend_endpoints":
            if any(type(v) is not str for v in value.values()):
                return "unclassified"
            if any(v and _url_carries_secret(v) for v in value.values()):
                return "security"
            if not all(_endpoint_ok(v) for v in value.values() if v):
                return "unclassified"
        return "keep"
    elif any(part in key.lower() for part in _SECRET_NAME_PARTS):
        return "security"
    else:
        return "unclassified"
    if not valid:
        raise ab.AgentBackupError("EXPORT_PREFS_TYPE_INVALID")
    return "keep"


def _scrub_preferences_counts(raw):
    """Scrubbed bytes and counts. `omitted` counts every key left out;
    `unclassified` and `destination_local` are the parts of it that were
    unrecognized (or outside the structured bounds) and installation-local
    respectively. An unrecognized key is left out and counted, never archived
    and never a reason to refuse the backup (F-DR-4)."""
    try:
        prefs = ab._strict_json_loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ab.AgentBackupError("EXPORT_PREFS_INVALID") from exc
    if type(prefs) is not dict:
        raise ab.AgentBackupError("EXPORT_PREFS_INVALID")
    result, omitted, unknown, local = {}, 0, 0, 0
    for key, value in prefs.items():
        disposition = _pref_disposition(key, value)
        if disposition == "keep" and len(_json_bytes(value)) > _PREF_VALUE_MAX_BYTES:
            disposition = "unclassified"
        if disposition == "keep":
            result[key] = value
            continue
        omitted += 1
        unknown += disposition == "unclassified"
        local += disposition == "local"
    # A recovered file cannot re-enable communications. PIN/owner setup is a
    # destination-local requirement, not an archived owner credential.
    result["telegram_remote_access"] = False
    return _json_bytes(result), {"omitted": omitted, "unclassified": unknown,
                                 "destination_local": local}


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


def generation_member_kind(logical_id):
    """("blob" | "manifest", artifact_id, archive_path) for a 2.2 generation
    member id, else None. The archive path is derived, never taken as given."""
    match = _GEN_ID.fullmatch(logical_id) if type(logical_id) is str else None
    if match is None:
        return None
    artifact_id = match.group(1)
    path = inv.GENERATION_ARCHIVE_PREFIX + f"{artifact_id[:2]}/{artifact_id}"
    if match.group(2):
        return "manifest", artifact_id, path + refs.MANIFEST_SUFFIX
    return "blob", artifact_id, path


def _manifest_names(raw, artifact_id):
    """A sidecar is a JSON object that, if it names an artifact, names its
    own one (S3 review S3-05). Other content is provenance, kept verbatim."""
    doc = ab._strict_json_loads(raw)
    return type(doc) is dict and doc.get("artifact_id", artifact_id) == artifact_id


def _empty_generation_observation(status):
    return {"status": status, "rows": 0, "captured_blobs": 0, "captured_manifests": 0,
            "hashes_verified": False, "findings": {}}


def _capture_generation(members, data_dir, staging_root, forbidden, warnings, budget):
    """Blob and manifest members for every row whose blob verifies, plus the
    observation that says what was and was not captured. The reference check
    reads a private copy of the exact database bytes being archived, so rows
    and blobs describe the same generation. Each file is then captured once
    through A2's capture boundary and re-hashed: bytes that changed since the
    check are never archived under the row's hash."""
    db = [m for m in members if m["logical_id"] == "state.databases.lumina_db"]
    if len(db) != 1:
        return [], _empty_generation_observation("unavailable")
    check_dir = tempfile.mkdtemp(prefix=".generation_check_", dir=staging_root)
    snapshot = os.path.join(check_dir, "lumina.db")
    with open(snapshot, "xb") as fh:
        fh.write(db[0]["data"])
    try:
        report = refs.generation_references(snapshot, data_dir, verify_hashes=True)
    except refs.ReferenceUnavailable:
        return [], _empty_generation_observation("unavailable")
    findings = {}
    for f in report.findings:
        findings[f.code] = findings.get(f.code, 0) + 1
    out, blobs_ok, manifests = [], set(), 0
    canonical_root = os.path.realpath(data_dir)
    for item in report.capture:
        artifact_id = item.relpath.rsplit("/", 1)[-1].split(".", 1)[0]
        if item.role == "manifest" and artifact_id not in blobs_ok:
            continue
        data, staged = ab._stage_physical_state_file(
            os.path.join(data_dir, item.relpath), staging_root, item.archive_path, canonical_root,
            forbidden, "generated image" if item.role == "blob" else "generated image manifest",
            warnings, budget=budget)
        if data is None:
            findings[refs.MISSING_BLOB if item.role == "blob" else "changed_during_capture"] = \
                findings.get(refs.MISSING_BLOB if item.role == "blob" else "changed_during_capture", 0) + 1
            continue
        if len(data) != item.size or (item.role == "blob" and hashlib.sha256(data).hexdigest() != item.sha256):
            findings["changed_during_capture"] = findings.get("changed_during_capture", 0) + 1
            continue
        if item.role == "manifest":
            try:
                if len(data) > MAX_GENERATION_MANIFEST_BYTES or not _manifest_names(data, artifact_id):
                    raise ValueError()
            except (ValueError, UnicodeError):
                findings["manifest_invalid"] = findings.get("manifest_invalid", 0) + 1
                continue
            manifests += 1
            logical_id = f"state.artifacts.generation.{artifact_id}.manifest_json"
            policy = GENERATION_MANIFEST_POLICY
        else:
            blobs_ok.add(artifact_id)
            logical_id = f"state.artifacts.generation.{artifact_id}"
            policy = GENERATION_BLOB_POLICY
        out.append({"logical_id": logical_id, "archive_path": item.archive_path, **policy,
                    "data": data, "staged_path": staged})
    # A manifest whose blob failed after it was captured is dropped with it.
    out = [m for m in out if generation_member_kind(m["logical_id"])[1] in blobs_ok]
    manifests = sum(1 for m in out if m["logical_id"].endswith(".manifest_json"))
    blocked = any(code in _GEN_BLOCKING for code in findings) or len(blobs_ok) != report.rows
    return out, {"status": "blocked" if blocked else "complete", "rows": report.rows,
                 "captured_blobs": len(blobs_ok), "captured_manifests": manifests,
                 "hashes_verified": report.hashes_verified, "findings": dict(sorted(findings.items()))}


def _observe_inventory(data_dir):
    """In-line inventory at export time: the D1-D4 declarations as the
    receipt block, and how many entries still block. Unobservable is said
    so, never read as clean."""
    try:
        report = inv.inventory(data_dir)
    except inv.InventoryUnavailable:
        return {"status": "unavailable", "blocking_entries": 0}, None
    return ({"status": "observed", "blocking_entries": len(report.blockers)},
            report.exclusion_receipt())


def prepare_export_members(members, data_dir, staging_root, forbidden, warnings, budget, *, verified=False):
    """Transform already-captured bytes, then add one bound control member.
    A Verified export then captures C1's record as the last collection
    step (A-3), in the child process, and adds it as one bound member."""
    members = [dict(m) for m in members]
    counts = {"omitted": 0, "unclassified": 0, "destination_local": 0}
    for member in members:
        if member["logical_id"] == "state.preferences.prefs_json":
            member["data"], counts = _scrub_preferences_counts(member["data"])
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
    generation, generation_observation = _capture_generation(
        members, data_dir, staging_root, forbidden, warnings, budget)
    members.extend(generation)
    inventory_observation, exclusions = _observe_inventory(data_dir)
    observations = {"suspension": ("unavailable_present" if forbidden.exclusion.status == "present_excluded"
                                   else "unavailable_no_trace"),
                    "c1_exclusion": forbidden.exclusion.manifest_record(),
                    "installation_marker": payload["source_status"],
                    "prefs_omitted_count": counts["omitted"],
                    "prefs_unclassified_count": counts["unclassified"],
                    "prefs_installation_local_count": counts["destination_local"],
                    "generation_artifacts": generation_observation,
                    "inventory": inventory_observation,
                    "declared_exclusions": exclusions}
    if verified:
        if forbidden.exclusion.status != "present_excluded":
            raise ExportUnavailable(OWNER_MESSAGES["C1_STATE_ABSENT"], code="C1_STATE_ABSENT")
        record = _capture_c1_record(data_dir)
        budget.charge(len(record), "C1 recovery record")
        try:
            observation = c1_observation_from_bytes(record)
        except C1RecordInvalid:
            raise ExportUnavailable(OWNER_MESSAGES["C1_SNAPSHOT_RECORD_INVALID"],
                                    code="C1_SNAPSHOT_RECORD_INVALID") from None
        members.append({"logical_id": C1_MEMBER_ID, **C1_MEMBER_POLICY, "data": record,
                        "staged_path": None})
        observations["suspension"] = "captured_sanitized"
        observations["c1_exclusion"] = c1_exclusion_record_v2(observations["c1_exclusion"])
        observations["c1_recovery"] = observation
    return members, observations


def apply_export_contract(manifest, observations, *, verified=False):
    version = VERIFIED_VERSION if verified else VERSION
    manifest["format_version"] = dict(version)
    manifest["compatibility"]["min_supported_format_version"] = dict(version)
    manifest["compatibility"]["max_known_format_version"] = dict(version)
    manifest["recovery_contract"] = copy.deepcopy(RECOVERY_CONTRACT)
    manifest["historical_claims"] = copy.deepcopy(HISTORICAL_CLAIMS)
    manifest["extensions"] = {"version": 1, "records": {}}
    manifest["required_features"] = []
    manifest["export_observations"] = dict(observations)
    manifest["c1_exclusion"] = manifest["export_observations"].pop("c1_exclusion")
    # 2.2 stays content-only; 2.3 says Verified, which its verifier must prove.
    manifest["backup_mode"] = "verified_agent_backup" if verified else "incomplete_state_export"
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


_V22_OBSERVATIONS = frozenset({"prefs_installation_local_count", "generation_artifacts",
                               "inventory", "declared_exclusions"})
_GEN_OBS_FIELDS = {"status", "rows", "captured_blobs", "captured_manifests", "hashes_verified", "findings"}


def _nonneg_int(value):
    return type(value) is int and value >= 0


def _valid_exclusion_receipt(value):
    """The inventory's declared-exclusion block: constants and counts only."""
    if (type(value) is not dict or set(value) != {"schema", "version", "paths_listed",
                                                  "originals_modified", "decisions"}
            or value["schema"] != inv.RECEIPT_SCHEMA or not _same_contract(value["version"], 1)
            or value["paths_listed"] is not False or value["originals_modified"] is not False
            or type(value["decisions"]) is not list
            or [d.get("decision") if type(d) is dict else None for d in value["decisions"]] != list(inv.DECISIONS)):
        return False
    for record in value["decisions"]:
        fixed = inv.DECISIONS[record["decision"]]
        keys = {"decision", "status", "entries", "items", "bytes", *fixed}
        if record["decision"] == "D3" and "uninspectable_processes" in record:
            keys.add("uninspectable_processes")
            if not _nonneg_int(record["uninspectable_processes"]):
                return False
        if (set(record) != keys or any(not _same_contract(record[k], v) for k, v in fixed.items())
                or record["status"] not in ("excluded", "absent", "blocked")
                or not all(_nonneg_int(record[k]) for k in ("entries", "items", "bytes"))):
            return False
    return True


def _valid_v22_observations(observations):
    gen, inventory = observations["generation_artifacts"], observations["inventory"]
    if not _nonneg_int(observations["prefs_installation_local_count"]):
        return False
    if (type(gen) is not dict or set(gen) != _GEN_OBS_FIELDS or gen["status"] not in GENERATION_STATUSES
            or not all(_nonneg_int(gen[k]) for k in ("rows", "captured_blobs", "captured_manifests"))
            or type(gen["hashes_verified"]) is not bool or type(gen["findings"]) is not dict
            or any(k not in _GEN_FINDING_CODES or not _nonneg_int(v) or v == 0 for k, v in gen["findings"].items())
            or gen["captured_manifests"] > gen["captured_blobs"] or gen["captured_blobs"] > gen["rows"]):
        return False
    if gen["status"] == "complete" and (gen["captured_blobs"] != gen["rows"] or not gen["hashes_verified"]
                                        or any(k in _GEN_BLOCKING for k in gen["findings"])):
        return False
    if gen["status"] == "unavailable" and gen != _empty_generation_observation("unavailable"):
        return False
    if (type(inventory) is not dict or set(inventory) != {"status", "blocking_entries"}
            or inventory["status"] not in ("observed", "unavailable")
            or not _nonneg_int(inventory["blocking_entries"])):
        return False
    if inventory["status"] == "unavailable":
        return inventory["blocking_entries"] == 0 and observations["declared_exclusions"] is None
    return _valid_exclusion_receipt(observations["declared_exclusions"])


def _generation_member_errors(members):
    """Exact policy, derived path and a blob for every manifest (2.2 only)."""
    if type(members) is not list:
        return []
    errors, blobs, manifests = [], set(), set()
    for m in members:
        if type(m) is not dict:
            continue
        kind = generation_member_kind(m.get("logical_id"))
        if kind is None:
            continue
        role, artifact_id, path = kind
        policy = GENERATION_BLOB_POLICY if role == "blob" else GENERATION_MANIFEST_POLICY
        fields = {"logical_id", "archive_path", "sha256", "size", *policy}
        if "extensions" in m:
            fields.add("extensions")
            if not _valid_extensions(m["extensions"]):
                errors.append("EXPORT_MEMBER_EXTENSIONS_INVALID")
        if (set(m) != fields or m["archive_path"] != path
                or any(not _same_contract(m.get(k), v) for k, v in policy.items())
                or not _nonneg_int(m.get("size")) or (role == "blob" and m["size"] == 0)
                or type(m.get("sha256")) is not str or not ab._SHA256_RE.fullmatch(m["sha256"])):
            errors.append("EXPORT_GENERATION_MEMBER_INVALID")
        (blobs if role == "blob" else manifests).add(artifact_id)
    if manifests - blobs:
        errors.append("EXPORT_GENERATION_MANIFEST_WITHOUT_BLOB")
    return errors


def _generation_rows(db_bytes):
    """{artifact_id: (sha256, size_bytes)} from archived database bytes, read
    in memory with the untrusted schema disabled; {} when the table is
    absent. Raises on anything that is not a plain table."""
    data = bytes(db_bytes)
    if len(data) >= 20 and data[18] == 2 and data[19] == 2:
        data = data[:18] + b"\x01\x01" + data[20:]   # in-memory copy cannot open WAL mode
    conn = sqlite3.connect(":memory:")
    try:
        conn.deserialize(data)
        conn.execute("PRAGMA trusted_schema=OFF")
        kinds = {k for (k,) in conn.execute(
            "SELECT type FROM sqlite_master WHERE name = 'generation_artifacts'")}
        if not kinds:
            return {}
        if kinds != {"table"}:
            raise ValueError("generation_artifacts is not a table")
        rows = conn.execute("SELECT artifact_id, sha256, size_bytes FROM generation_artifacts "
                            "LIMIT ?", (refs.MAX_ROWS + 1,)).fetchall()
    finally:
        conn.close()
    if len(rows) > refs.MAX_ROWS or len({r[0] for r in rows}) != len(rows):
        raise ValueError("generation_artifacts rows out of bounds or duplicated")
    return {r[0]: (r[1], r[2]) for r in rows}


def validate_export_archive(manifest, zf):
    """2.2-family cross-member rules, from the archived bytes themselves:
    every captured blob matches its row's sha256 and size in the archived
    database, the observation counts match the members, and "complete"
    means every row's blob is in the archive. 2.3 runs all of that (A-1)
    and then derives the C1 observation from the archived record."""
    if type(manifest) is not dict or not _v22_family(manifest.get("format_version")):
        return []
    errors = _generation_crosscheck(manifest, zf)
    if _is_verified_format(manifest.get("format_version")):
        errors.extend(_c1_crosscheck(manifest, zf))
    return errors


def _c1_crosscheck(manifest, zf):
    try:
        found = [m for m in manifest["members"] if type(m) is dict and m.get("logical_id") == C1_MEMBER_ID]
        if len(found) != 1:
            return ["EXPORT_C1_MEMBER_MISSING_OR_DUPLICATE"]
        derived = c1_observation_from_bytes(zf.read(found[0]["archive_path"]))
    except C1RecordInvalid:
        return ["EXPORT_C1_RECORD_INVALID"]
    except Exception:
        return ["EXPORT_C1_CROSSCHECK_UNAVAILABLE"]
    if not _same_contract(derived, manifest["export_observations"].get("c1_recovery")):
        return ["EXPORT_C1_OBSERVATION_MISMATCH"]
    return []


def _generation_crosscheck(manifest, zf):
    try:
        members = [m for m in manifest["members"] if type(m) is dict]
        blobs = {}
        manifests = 0
        for m in members:
            kind = generation_member_kind(m.get("logical_id"))
            if kind and kind[0] == "blob":
                blobs[kind[1]] = (m["sha256"], m["size"])
            elif kind:
                manifests += 1
        gen = manifest["export_observations"]["generation_artifacts"]
        db = [m for m in members if m.get("logical_id") == "state.databases.lumina_db"]
        rows = _generation_rows(zf.read(db[0]["archive_path"])) if len(db) == 1 else None
        if rows is None:
            return ["EXPORT_GENERATION_CROSSCHECK_UNAVAILABLE"]
        errors = []
        if gen["captured_blobs"] != len(blobs) or gen["captured_manifests"] != manifests:
            errors.append("EXPORT_GENERATION_OBSERVATION_MISMATCH")
        if gen["status"] != "unavailable" and gen["rows"] != len(rows):
            errors.append("EXPORT_GENERATION_OBSERVATION_MISMATCH")
        if any(rows.get(i) != v for i, v in blobs.items()):
            errors.append("EXPORT_GENERATION_ROW_MISMATCH")
        if gen["status"] == "complete" and set(blobs) != set(rows):
            errors.append("EXPORT_GENERATION_INCOMPLETE_CLAIM")
        return errors
    except Exception:
        return ["EXPORT_GENERATION_CROSSCHECK_UNAVAILABLE"]


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
    v22 = _v22_family(version)          # 2.2 and 2.3 (A-1)
    v23 = _is_verified_format(version)
    modern = v22 or _same_contract(version, V21)
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
    if v23 and "c1_exclusion" not in manifest:
        errors.append("EXPORT_C1_EXCLUSION_INVALID")
    if "c1_exclusion" in manifest:
        expected_roots.add("c1_exclusion")
        if not _valid_c1_record(manifest["c1_exclusion"], v2=v23):
            errors.append("EXPORT_C1_EXCLUSION_INVALID")
        else:
            expected_observation = ("captured_sanitized" if v23 else
                                    "unavailable_present" if manifest["c1_exclusion"]["status"] == "present_excluded"
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
    except (KeyError, TypeError, AttributeError):
        errors.append("EXPORT_COVERAGE_CONTRACT_INVALID")
    if (not any(_same_contract(version, v) for v in VERIFY_VERSIONS)
            or manifest.get("backup_mode") != ("verified_agent_backup" if v23 else "incomplete_state_export")):
        errors.append("EXPORT_FORMAT_OR_MODE_UNSUPPORTED")
    if not _same_contract(manifest.get("recovery_contract"), RECOVERY_CONTRACT):
        errors.append("EXPORT_RECOVERY_POLICY_MISMATCH")
    observations = manifest.get("export_observations")
    expected = {"suspension", "installation_marker", "prefs_omitted_count", "prefs_unclassified_count"}
    if v22:
        expected |= _V22_OBSERVATIONS
    if v23:
        expected |= {"c1_recovery"}
    if type(observations) is not dict or set(observations) != expected:
        errors.append("EXPORT_COVERAGE_OBSERVATIONS_INVALID")
    elif (observations["suspension"] not in ({"captured_sanitized"} if v23
                                             else {"unavailable_present", "unavailable_no_trace"})
          or observations["installation_marker"] not in {"present", "missing_unknown"}
          or any(type(observations[k]) is not int or observations[k] < 0
                 for k in ("prefs_omitted_count", "prefs_unclassified_count"))
          or (v22 and not _valid_v22_observations(observations))
          or (v23 and not _valid_c1_observation(observations["c1_recovery"]))):
        errors.append("EXPORT_COVERAGE_OBSERVATIONS_INVALID")
    elif v23:
        # A 2.3 archive is Verified by construction: any blocker refuses it.
        if coverage_blockers(manifest):
            errors.append("EXPORT_VERIFIED_MODE_WITH_BLOCKERS")
    listed = manifest.get("members") if type(manifest.get("members")) is list else []
    c1_members = [m for m in listed if type(m) is dict
                  and (m.get("logical_id") == C1_MEMBER_ID
                       or (type(m.get("archive_path")) is str and m["archive_path"].startswith(C1_ARCHIVE_PREFIX)))]
    if not v23 and c1_members:
        errors.append("EXPORT_C1_MEMBER_UNEXPECTED")
    if v23:
        errors.extend(_c1_member_errors(c1_members, observations))
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
    if v22:
        errors.extend(_generation_member_errors(manifest.get("members")))
        base["members"] = [m for m in base["members"]
                           if not (type(m) is dict and generation_member_kind(m.get("logical_id")))]
    if v23:
        base["members"] = [m for m in base["members"]
                           if not (type(m) is dict and m.get("logical_id") == C1_MEMBER_ID)]
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


def _c1_member_errors(c1_members, observations):
    """Exactly one C1 member with its exact policy, bound to the observation."""
    if len(c1_members) != 1:
        return ["EXPORT_C1_MEMBER_MISSING_OR_DUPLICATE"]
    m = c1_members[0]
    observation = observations.get("c1_recovery") if type(observations) is dict else None
    if (set(m) != {"logical_id", "sha256", "size", *C1_MEMBER_POLICY}
            or m.get("logical_id") != C1_MEMBER_ID
            or any(not _same_contract(m.get(k), v) for k, v in C1_MEMBER_POLICY.items())
            or not _nonneg_int(m.get("size")) or m["size"] == 0
            or type(m.get("sha256")) is not str or not ab._SHA256_RE.fullmatch(m["sha256"])
            or type(observation) is not dict or m["sha256"] != observation.get("record_sha256")):
        return ["EXPORT_C1_MEMBER_INVALID"]
    return []


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
        elif generation_member_kind(member.get("logical_id")):
            kind = generation_member_kind(member.get("logical_id"))[0]
            if kind == "blob" and not raw:
                return ["EXPORT_GENERATION_PAYLOAD_INVALID"]
            if kind == "manifest" and (len(raw) > MAX_GENERATION_MANIFEST_BYTES
                                       or not _manifest_names(raw, generation_member_kind(
                                           member.get("logical_id"))[1])):
                return ["EXPORT_GENERATION_PAYLOAD_INVALID"]
        elif member.get("logical_id") == C1_MEMBER_ID:
            if not _is_verified_format(manifest.get("format_version")):
                return ["EXPORT_C1_MEMBER_UNEXPECTED"]
            try:
                derived = c1_observation_from_bytes(raw)
            except C1RecordInvalid:
                return ["EXPORT_C1_RECORD_INVALID"]
            if not _same_contract(derived, manifest["export_observations"].get("c1_recovery")):
                return ["EXPORT_C1_OBSERVATION_MISMATCH"]
        elif member.get("logical_id") == "state.preferences.prefs_json":
            cleaned, _, unknown = _scrub_preferences(raw)
            if ab._strict_json_loads(cleaned) != ab._strict_json_loads(raw) or unknown:
                return ["EXPORT_PREFS_SECURITY_POLICY_VIOLATION"]
    except (ValueError, UnicodeError, KeyError, TypeError, AttributeError, ab.AgentBackupError):
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
        if _is_verified_format(manifest.get("format_version")):
            # First, before the READ_VERSIONS family that now includes it
            # (TS3-05): a hint only. Nothing here was verified, so no coverage
            # and no blocker is claimed either way; only the verifier derives them.
            return {"kind": "agent_export_v2_verified", "verification": "not_performed",
                    "full_agent_coverage": False, "coverage_blockers": ["verification_not_performed"]}
        if any(_same_contract(manifest.get("format_version"), v) for v in READ_VERSIONS):
            # 2.0-2.2 (2.3 returned above): these formats cannot carry C1
            # state at all, so "suspension" is a fact of the format.
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


def _c1_safety_coverage(manifest, observations):
    if _is_verified_format(manifest.get("format_version")):
        record = observations["c1_recovery"]
        return {"status": "captured_sanitized" if record["status"] == "captured_sanitized" else "unavailable",
                "source_observation": observations["suspension"],
                "record": copy.deepcopy(record), "raw_store": "excluded",
                "label": "derived_sanitized_producer_claims", "live_state": "never_restored_as_live",
                "enforcement_compatibility": "unproven", "limitations": list(C1_LIMITATIONS),
                "exclusion": copy.deepcopy(manifest["c1_exclusion"])}
    return {"status": "unavailable", "source_observation": observations["suspension"],
            "omitted": ["C1 store", "soul identity", "evidence", "incidents", "epochs", "replay state"],
            "enforcement_compatibility": "unproven",
            **({"exclusion": copy.deepcopy(manifest["c1_exclusion"])}
               if "c1_exclusion" in manifest else {})}


def _coverage(manifest):
    observations = manifest["export_observations"]
    families = sorted({m["logical_id"] for m in manifest["members"]})
    return {
        "semantic_coverage": {"status": ("required_state_captured" if _is_verified_format(manifest.get("format_version"))
                                         else "incomplete"),
                              "captured_members": families,
                              "knowledge_pack_installation_marker": observations["installation_marker"],
                              "unclassified_preferences_omitted": observations["prefs_unclassified_count"]},
        "c1_safety_coverage": _c1_safety_coverage(manifest, observations),
        "recovery_acceptance": {"status": "not_proven", "restore_input_accepted": False},
        **({"generation_artifacts": copy.deepcopy(observations["generation_artifacts"]),
            "inventory": copy.deepcopy(observations["inventory"]),
            "declared_exclusions": copy.deepcopy(observations["declared_exclusions"]),
            "installation_local_preferences_omitted": observations["prefs_installation_local_count"]}
           if "generation_artifacts" in observations else {}),
    }


def coverage_blockers(manifest):
    """The one blockers function: the publication gate, _receipt and
    confirm_export_result all call it (TS3-11, A-6). It reads a manifest the
    verifier already accepted (or, at the gate's first call, the builder's
    own pre-serialization manifest). 2.2 never covers C1, so "suspension"
    always blocks it; 2.3 clears it only through its c1_recovery observation."""
    observations = manifest["export_observations"]
    blockers = []
    record = observations.get("c1_recovery")
    if not (_is_verified_format(manifest.get("format_version")) and type(record) is dict
            and record.get("status") == "captured_sanitized"
            and observations["suspension"] == "captured_sanitized"):
        blockers.append("suspension")
    if observations["installation_marker"] != "present":
        blockers.append("installation_marker")
    if "generation_artifacts" in observations:
        if observations["generation_artifacts"]["status"] != "complete":
            blockers.append("generation_artifacts")
        if (observations["inventory"]["status"] != "observed"
                or observations["inventory"]["blocking_entries"]):
            blockers.append("inventory")
    return blockers


def _derived_claims(manifest):
    """(export_kind, full_agent_coverage, blockers) for a verified manifest."""
    blockers = coverage_blockers(manifest)
    verified = _is_verified_format(manifest.get("format_version"))
    return ("verified_agent_backup" if verified else "incomplete_state_export",
            verified and not blockers, blockers)


def _receipt(result):
    report = verify_export(io.BytesIO(result.built_data))
    if not report["valid"]:
        raise ab.AgentBackupError("EXPORT_BUILT_BYTES_INVALID")
    manifest = report["manifest"]
    observations = manifest["export_observations"]
    export_kind, full, blockers = _derived_claims(manifest)
    return {**_coverage(manifest),
            "historical_claims": copy.deepcopy(manifest.get("historical_claims")),
            "archive_integrity": {"status": "PASS", "scope": "structure_hashes_sqlite"},
            "export_kind": export_kind,
            "verification": "PASS", "destination": result.dest_path,
            "archive_sha256": hashlib.sha256(result.built_data).hexdigest(),
            "archive_size": len(result.built_data), "format_version": dict(manifest["format_version"]),
            "member_count": report["validated_member_count"],
            "database_snapshots_verified": len(report["verified_sqlite_members"]),
            "full_agent_coverage": full, "restorable_verified": False,
            "coverage_blockers": blockers, "prefs_omitted_count": observations["prefs_omitted_count"],
            "publication": result.publication_observation,
            "warnings": manifest["warnings"]}


def verified_publication_gate(manifest):
    """Refuse a Verified export with any coverage blocker, before anything is
    published. Runs on the builder's manifest (to stop early, in owner words)
    and again on the verified built manifest (the binding check)."""
    if not _is_verified_format(manifest.get("format_version")):
        raise ExportUnavailable(OWNER_MESSAGES["EXPORT_FAILED"], code="VERIFIED_FORMAT_MISSING")
    blockers = coverage_blockers(manifest)
    if blockers:
        raise ExportUnavailable(_blocked_message(blockers), code="VERIFIED_BLOCKED", detail=tuple(blockers))


def recheck_c1_incarnation(data_dir, built_manifest, built_data):
    """Re-read the live store's identity in the child just before
    publication and refuse unless it is the one the archived record claims
    (TS3-03 option a, A-3). The capture is still one point in time; this
    only proves the store at publication is the same incarnation."""
    with zipfile.ZipFile(io.BytesIO(built_data)) as zf:
        found = [m for m in built_manifest["members"] if m.get("logical_id") == C1_MEMBER_ID]
        if len(found) != 1:
            raise ExportUnavailable(OWNER_MESSAGES["C1_SNAPSHOT_RECORD_INVALID"],
                                    code="C1_SNAPSHOT_RECORD_INVALID")
        raw = zf.read(found[0]["archive_path"])
    try:
        _observation, soul_id, incarnation = _c1_record_facts(raw)
    except C1RecordInvalid:
        raise ExportUnavailable(OWNER_MESSAGES["C1_SNAPSHOT_RECORD_INVALID"],
                                code="C1_SNAPSHOT_RECORD_INVALID") from None
    if _observe_c1_incarnation(data_dir) != (soul_id, incarnation):
        raise ExportUnavailable(OWNER_MESSAGES["C1_SNAPSHOT_STORE_REPLACED"], code="C1_SNAPSHOT_STORE_REPLACED")


def owner_receipt_text(receipt):
    """(title, text) the owner sees for a confirmed export receipt (Qt-free,
    so its wording lint runs everywhere). Every line is derived
    from the receipt; nothing here claims more than the verifier derived
    (C1-S3 A-7: never "restorable", members captured at different times)."""
    durability = ("confirmed" if receipt["publication"].get("directory_fsynced")
                  else "not confirmed; best effort only")
    if receipt.get("export_kind") == "verified_agent_backup" and receipt.get("full_agent_coverage") is True:
        record = receipt["c1_safety_coverage"]["record"]
        truncated = [name for name, key in (("audit history", "audit_truncated"),
                                            ("resumed incidents", "incidents_truncated"),
                                            ("runtime records", "runtimes_truncated")) if record[key]]
        return "Verified Agent Backup", (
            f"Verified Agent Backup written to:\n{receipt['destination']}\n\n"
            "Archive integrity and structure: PASS\n"
            f"SHA-256: {receipt['archive_sha256']}\n"
            f"Verified members: {receipt['member_count']}\n"
            f"Verified SQLite snapshots: {receipt['database_snapshots_verified']}\n"
            "Coverage checks: no blockers (safety-suspension state, installation record, preferences, "
            "generated images, data-folder inventory)\n"
            "C1 safety state: sanitized history record included (raw store excluded); "
            "not restored as live safety state\n"
            f"Safety record: {record['redaction_total']} identifying values removed; "
            + ("oldest " + ", ".join(truncated) + " not carried (declared)" if truncated
               else "nothing truncated") + "\n"
            "C1 enforcement compatibility: UNPROVEN\n"
            "Members were captured at different times; this is not one point-in-time snapshot. "
            "Later safety trips are not included.\n"
            "Full recovery: NOT PROVEN until a restore is verified. An isolated restore keeps the "
            "safety record only as inactive history; owner-local Recover then starts every scope suspended.\n"
            f"Directory durability: {durability}\n"
            "Not encrypted. Ordinary content may contain secrets. Preserve prior backups.")
    marker = receipt["semantic_coverage"]["knowledge_pack_installation_marker"]
    return "Incomplete State Export", (
        f"Incomplete State Export written to:\n{receipt['destination']}\n\n"
        "Archive integrity and structure: PASS\n"
        f"SHA-256: {receipt['archive_sha256']}\n"
        f"Verified members: {receipt['member_count']}\n"
        f"Verified SQLite snapshots: {receipt['database_snapshots_verified']}\n"
        "Semantic state coverage: INCOMPLETE\n"
        f"Knowledge Pack installation marker: {marker}\n"
        "C1 store / soul identity / safety evidence: OMITTED\n"
        "C1 enforcement compatibility: UNPROVEN\n"
        "Full recovery acceptance: NOT PROVEN; not a complete Restore input\n"
        f"Directory durability: {durability}\n"
        "Not encrypted. Ordinary content may contain secrets. Preserve prior verified backups.")


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


def export_verified_agent_backup(data_dir, base_dir, destination, *,
                                identity_trailers_path=None, credentials_path=None):
    """Owner UI entry point (OD-1). Publishes a format-2.3 archive only when
    every coverage condition is derived true from its own verified bytes;
    any blocker refuses and nothing is written. Verified never means
    restorable: full recovery stays not proven until a restore is."""
    refuse_if_production_path(data_dir)
    try:
        exclusion = c1.describe_exclusions(data_dir)
    except c1.ExclusionUnavailable as exc:
        raise ExportUnavailable(_owner_words("C1_EXCLUSION_" + exc.code), code="C1_EXCLUSION_" + exc.code) from exc
    if exclusion.status != "present_excluded":
        raise ExportUnavailable(OWNER_MESSAGES["C1_STATE_ABSENT"], code="C1_STATE_ABSENT")
    result = ab._build_agent_backup_core(
        data_dir, base_dir, destination, identity_trailers_path=identity_trailers_path,
        credentials_path=credentials_path, _export_v2=True, _export_verified=True)
    return result.manifest, _receipt(result)


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
            or not _v22_family(report["manifest"].get("format_version"))
            or not _same_contract(receipt.get("format_version"), report["manifest"].get("format_version"))
            or receipt.get("verification") != "PASS"
            or receipt.get("archive_sha256") != hashlib.sha256(raw).hexdigest()
            or receipt.get("archive_size") != len(raw)):
        raise ab.AgentBackupError("EXPORT_RECEIPT_NOT_BOUND_TO_ARTIFACT")
    # Derive coverage; a caller cannot launder a full-coverage claim in a dict.
    export_kind, full, blockers = _derived_claims(report["manifest"])
    if receipt.get("full_agent_coverage") is not full or receipt.get("restorable_verified") is not False:
        raise ab.AgentBackupError("EXPORT_UNSUPPORTED_SUCCESS_CLAIM")
    expected = _coverage(report["manifest"])
    if (any(not _same_contract(receipt.get(k), v) for k, v in expected.items())
            or not _same_contract(receipt.get("historical_claims"), report["manifest"].get("historical_claims"))
            or receipt.get("member_count") != report["validated_member_count"]
            or receipt.get("database_snapshots_verified") != len(report["verified_sqlite_members"])
            or receipt.get("archive_integrity") != {"status": "PASS", "scope": "structure_hashes_sqlite"}
            or not _same_contract(receipt.get("coverage_blockers"), blockers)
            or receipt.get("export_kind") != export_kind):
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


def preflight_incomplete(data_dir, destination, *, verified=False):
    label = "Verified Agent Backup" if verified else "Incomplete State Export"
    refuse_if_production_path(data_dir)
    if os.environ.get("LUMINA_TESTING") == "1":
        live_release = os.path.expanduser("~/.local/share/lumina-release")
        for candidate in (data_dir, destination):
            real = os.path.realpath(candidate)
            if real == live_release or real.startswith(live_release + os.sep):
                raise ExportUnavailable("Synthetic tests refuse the live release data root.")
    if os.path.lexists(destination):
        raise ExportUnavailable(label + " needs a new destination. Existing files are preserved.")
    try:
        exclusion = c1.describe_exclusions(data_dir)
    except c1.ExclusionUnavailable as exc:
        raise ExportUnavailable("C1_EXCLUSION_" + exc.code) from exc
    if verified and exclusion.status != "present_excluded":
        raise ExportUnavailable(OWNER_MESSAGES["C1_STATE_ABSENT"], code="C1_STATE_ABSENT")
    return exclusion


def _owner_export_in_process(mode, data_dir, base_dir, destination, *,
                             credentials_path=None, identity_trailers_path=None):
    """The Settings export and its re-observation, unchanged: what the owner's
    button has always run. Returns the confirmed receipt."""
    paths = {k: v for k, v in (("credentials_path", credentials_path),
                               ("identity_trailers_path", identity_trailers_path)) if v is not None}
    if mode == "incomplete":
        manifest, receipt = export_incomplete_state(
            data_dir, base_dir, destination, owner_acknowledged=True, **paths)
    elif mode == "verified":
        manifest, receipt = export_verified_agent_backup(data_dir, base_dir, destination, **paths)
    else:
        raise ValueError(f"unknown export mode {mode!r}")
    return confirm_export_result(destination, manifest, receipt)


OWNER_EXPORT_CHILD = "core.agent_backup_export_child"


def run_owner_export(mode, data_dir, base_dir, destination):
    """F-DR-5: run one owner export in a fresh child process (see
    core/agent_backup_export_child.py for why), so the SQLite connection
    identity proof never meets a descriptor parked by an earlier export in
    the running application. The credentials and identity-trailer paths are
    resolved here exactly as the in-process call resolves them and passed on
    explicitly, so the child excludes the same credentials store.

    Returns the confirmed receipt. Raises ExportUnavailable (owner words)
    for a refusal and AgentBackupError for any other failure; the builder's
    own all-or-nothing publication is unchanged, so a failure publishes
    nothing."""
    if mode not in ("verified", "incomplete"):
        raise ValueError(f"unknown export mode {mode!r}")
    credentials_path = (os.environ.get("LUMINA_SECRETS_PATH")
                        or os.path.expanduser("~/.config/lumina/credentials.json"))
    identity_trailers_path = os.path.expanduser("~/.config/lumina/identity_trailers.json")
    code_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # The child runs from the code root: hand it absolute paths (review C1).
    # Same interpreter and site configuration as this process (review C2).
    paths = [os.path.abspath(str(p)) for p in (data_dir, base_dir, destination)]
    proc = subprocess.run(
        [sys.executable, "-B", "-m", OWNER_EXPORT_CHILD, mode, *paths,
         credentials_path, identity_trailers_path],
        cwd=code_root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        reply = json.loads(proc.stdout.decode("utf-8")) if proc.stdout else None
    except (ValueError, UnicodeError):
        reply = None
    if proc.returncode == 0 and type(reply) is dict and type(reply.get("receipt")) is dict:
        return reply["receipt"]
    if (proc.returncode == 3 and type(reply) is dict and type(reply.get("refused")) is str):
        code = reply.get("code")
        raise ExportUnavailable(reply["refused"], code=code if type(code) is str else None)
    raise ab.AgentBackupError("EXPORT_CHILD_FAILED")


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
