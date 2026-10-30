"""core/agent_backup_restore_cockpit.py -- Qt-free logic behind Settings'
isolated-recovery cockpit (AGENT-BACKUP-RESTORE-UI-01).

The cockpit is a thin, honest front end for the shipped isolated recovery
engine (core.agent_backup_restore) and its intake (core.agent_backup_restore_
intake). It adds no recovery capability and weakens no engine rule:

  * every operation here calls the engine's public functions and returns a
    plain dict; nothing raises, and no exception text is passed on (archive
    content can reach exception text, so only fixed wording and error-class
    names are shown);
  * RestoreSession is the owner-consent state machine: a plan is approved by
    its plan_digest, any change to an input drops the plan and the approval,
    and one execution attempt consumes the approval whatever its outcome, so
    a retry always needs a fresh preview and a fresh approval;
  * every string that came from an archive (unit names, paths, coverage
    values, damage reasons) goes through safe_text() before display.

Nothing here restores into a live or production folder, and nothing here
weakens the engine's production-root refusals: those stay in the engine and
this module only relays them. Not production disaster recovery.
"""
from __future__ import annotations

import copy
import errno
import json
import os
import re
import stat
import unicodedata

from core import agent_backup as ab
from core import agent_backup_restore as ar
from core import agent_backup_restore_intake as ri

RECEIPT_READ_LIMIT = 16 * 1024 * 1024

# What the engine's restore class means, in words that claim no more than it
# guarantees.
CLASS_FOR_KIND = {ri.VERIFIED: ar.INCOMPLETE, ri.SELECTIVE: ar.SELECTIVE, ri.LEGACY: ar.LEGACY}
CLASS_TEXT = {
    ar.INCOMPLETE: (
        "Incomplete State Export recovery",
        "A structurally verified Incomplete State Export. Its coverage is incomplete (C1 safety state "
        "is not captured), so it is not a Verified Agent Backup."),
    ar.SELECTIVE: (
        "Selective Recovery",
        "The archive FAILED whole-archive verification. Only units that verify independently can be "
        "recovered, and only the ones you choose. The archive as a whole is NOT verified."),
    ar.LEGACY: (
        "Legacy Recovery",
        "A Backup Memory (Legacy) ZIP with no manifest or hashes. Only the data-folder content it holds "
        "can be recovered, with unknown provenance; Skills, personas and other source overlays are not "
        "in it. Integrity is ZIP CRC and payload shape only."),
}

BANNER = (
    "Isolated recovery writes a NEW folder you choose. It does not restore into, replace or modify the "
    "running Lumina, its data folder or any known Lumina production folder (those are refused). It is "
    "not production disaster recovery and cannot produce a Verified Agent Backup.")

TRUST_NOTE = (
    "A valid checksum shows an archive is intact, not where it came from. Recover only a backup you made "
    "or whose source you trust. Recovered chats, memories and Palace rows keep the layer, trust and "
    "provenance marks the archive carries.")

ALWAYS_TRUE = (
    "Credentials, PIN, owner and channel bindings, tool approvals and C1 safety state are never restored.",
    "Telegram remote access is forced off; nothing reconnects and no background work is replayed.",
    "Custom tools, the tool audit log and pending actions are kept only as inactive history under "
    "restore_history/; nothing loads them.",
    "Project root bindings stay inactive until you re-pick them. No recovered Skill is official.",
    "The running Lumina is not changed and is not switched to the recovered folder.",
)

NEXT_STEPS = (
    "This is an isolated recovery root. It has not been activated.",
    "Starting Lumina on it is not yet a supported or verified path: C1 owner-local Recover, Knowledge "
    "Pack reconciliation, project rebind and a verified launch are still pending.",
)

# Categories a human can tell apart.
UNSAFE_DESTINATION = "unsafe_destination"
INVALID_ARCHIVE = "invalid_archive"
INCOMPLETE_COVERAGE = "incomplete_coverage"
INTERRUPTED_PENDING = "interrupted_restore_pending"
PLAN_STALE = "plan_stale"
INSUFFICIENT_RESOURCES = "insufficient_resources"
RESTORE_FAILED = "restore_failed"
PUBLISHED_UNVERIFIED = "published_unverified"
UNEXPECTED_ERROR = "unexpected_error"

_NOTHING = "Nothing was changed."

# Refusals that, after the owner chose from an inspected table, can only mean
# the archive is not the one inspected (the table offers nothing else). The
# cockpit confirms that against the SHA-256 before saying so.
REPLAN_CODES = frozenset({"SELECTION_INVALID", "SELECTION_REQUIRED", "ARCHIVE_REJECTED",
                          "UNSUPPORTED_REQUIRED_FEATURES"})

# code -> (category, title, guidance). Every engine and intake refusal code
# has an entry (a test pins that, so a new code cannot ship without wording).
REFUSAL_GUIDE = {
    # -- engine ------------------------------------------------------------
    "PRODUCTION_ROOT": (UNSAFE_DESTINATION, "That folder is live or protected Lumina state",
                        "Isolated recovery never writes into a Lumina data folder, a known production "
                        "location, this process's data folder or the running installation. Choose a new "
                        "folder outside them."),
    "DESTINATION_NOT_EMPTY": (UNSAFE_DESTINATION, "The destination is not a new or empty folder",
                              "Recovery never merges into existing data. Choose a path that does not exist "
                              "yet, or an empty folder."),
    "DESTINATION_PARENT_UNSAFE": (UNSAFE_DESTINATION, "The destination's parent folder is missing or unsafe",
                                  "The parent must be an existing real folder (not a link) that you can "
                                  "write to."),
    "DESTINATION_CHANGED": (UNSAFE_DESTINATION, "The destination changed before it could be published",
                            "It appeared, filled or was replaced after the preview. Nothing was published. "
                            "Preview again."),
    "INSTALL_FOLDER_UNSAFE": (UNSAFE_DESTINATION, "The install folder is missing or unsafe",
                              "Choose an existing real folder (not a link) for the recovered Skills, "
                              "personas, avatars, voices, tool profiles and project documents."),
    "INSTALL_TARGET_CHANGED": (PLAN_STALE, "An install-folder file changed after the preview",
                               "A file the plan relied on was created, replaced or removed. Preview again."),
    "INTERRUPTED_RESTORE_PENDING": (INTERRUPTED_PENDING, "An earlier recovery was interrupted here",
                                    "A killed recovery left staging behind and it blocks new plans. Use "
                                    "'Check for interrupted recovery' for this destination."),
    "ARCHIVE_REJECTED": (INVALID_ARCHIVE, "The archive was rejected", "The archive cannot be recovered."),
    "ARCHIVE_CHANGED": (PLAN_STALE, "The archive changed after you previewed it",
                        "Its bytes differ from the archive you inspected. Inspect it again, then preview."),
    "UNSUPPORTED_REQUIRED_FEATURES": (INCOMPLETE_COVERAGE, "The archive needs features this version cannot handle",
                                      "It declares required features Lumina cannot activate safely. Update "
                                      "Lumina; nothing can be recovered from it meanwhile."),
    "SELECTION_REQUIRED": (INCOMPLETE_COVERAGE, "Some units are damaged: choose what to recover",
                           "Damaged or dependency-blocked units are never taken silently. Pick the units "
                           "you want from the table."),
    "SELECTION_INVALID": (INCOMPLETE_COVERAGE, "The selection includes units that cannot be recovered",
                          "Only units that verify independently can be chosen."),
    "OWNER_CONFLICT_CHOICE_REQUIRED": (INCOMPLETE_COVERAGE, "Install-folder conflicts need your decision",
                                       "A file already in the install folder differs from the archive. "
                                       "Choose 'keep my file' or 'skip' for that unit, then preview again."),
    "PLAN_NOT_APPROVED": (PLAN_STALE, "The plan was not approved as shown",
                          "The approved digest does not match this plan. Preview and approve again."),
    "PLAN_CHANGED": (PLAN_STALE, "The plan no longer matches the archive or folders",
                     "The archive, its classification, the destination, an install-folder file or the "
                     "selection changed since the preview. Nothing was written. Preview again."),
    "INSUFFICIENT_SPACE": (INSUFFICIENT_RESOURCES, "Not enough free space",
                           "Free space on the destination (or install) volume, or choose another folder."),
    "MEMBER_INVALID": (INVALID_ARCHIVE, "A member failed validation while staging",
                       "The archive changed or a member is not what was planned. Nothing was published."),
    "SQLITE_INVALID": (INVALID_ARCHIVE, "A database failed the safety checks",
                       "An archived database is not a compatible, intact Lumina database. Nothing was "
                       "published."),
    "SKILL_AUTHORITY_UNVERIFIED": (INVALID_ARCHIVE, "A Skill still claimed official status",
                                   "The engine refuses to publish a Skill that claims official trust."),
    "C1_TRACE_FAILED": (RESTORE_FAILED, "The C1 restore trace could not be written",
                        "Without the trace the folder could be first-run initialized, so nothing was "
                        "published."),
    "STAGED_VALIDATION_FAILED": (RESTORE_FAILED, "The staged folder failed validation",
                                 "The engine refused to publish what it had staged."),
    "INSTALL_FILE_LIMIT": (INSUFFICIENT_RESOURCES, "Too many install files",
                           "This engine restores at most 1,000 install files."),
    "COMMIT_PRIMITIVE_UNAVAILABLE": (RESTORE_FAILED, "Atomic publishing is not available here",
                                     "The no-replace rename this engine needs is unavailable (Linux only)."),
    "WRITE_FAILED": (RESTORE_FAILED, "Writing failed",
                     "The disk or permissions refused a write. Check space, permissions and the parent folder."),
    # -- intake (archive and survey) ----------------------------------------
    "INPUT_UNAVAILABLE": (INVALID_ARCHIVE, "A file or folder could not be read",
                          "The archive, or a folder being surveyed, is missing or unreadable."),
    "INPUT_NOT_REGULAR": (INVALID_ARCHIVE, "That path is not a plain file or folder",
                          "Links, devices and special files are refused."),
    "INPUT_ALIASED": (INVALID_ARCHIVE, "The archive file has another hard link",
                      "Copy it to a new file and choose the copy."),
    "INPUT_UNSTABLE": (INVALID_ARCHIVE, "The archive changed while it was being read",
                       "Wait for any copy or sync to finish, then inspect again."),
    "INPUT_TOO_LARGE": (INVALID_ARCHIVE, "The archive is too large", "It exceeds the intake size limit."),
    "NOT_A_ZIP": (INVALID_ARCHIVE, "Not a ZIP archive", "The file is not a readable ZIP."),
    "ZIP_STRUCTURE_UNSAFE": (INVALID_ARCHIVE, "Unsafe ZIP structure",
                             "Prefix or trailing bytes, concatenation or header disagreement."),
    "ZIP_ENCRYPTED": (INVALID_ARCHIVE, "The ZIP is encrypted",
                      "Encrypted archives are not supported here; nothing was read."),
    "ZIP_METHOD_UNSUPPORTED": (INVALID_ARCHIVE, "Unsupported ZIP compression", "Only stored and deflate."),
    "ZIP_ENTRY_UNSAFE": (INVALID_ARCHIVE, "Unsafe ZIP entry",
                         "A path escapes the archive, is absolute, or is a link or directory entry."),
    "ZIP_NAME_COLLISION": (INVALID_ARCHIVE, "Colliding ZIP entry names", "Duplicate or case/Unicode-colliding names."),
    "ZIP_RESOURCE_LIMIT": (INVALID_ARCHIVE, "The archive exceeds a resource limit",
                           "Too many or too large members for safe handling."),
    "MANIFEST_INVALID": (INVALID_ARCHIVE, "The manifest is unreadable", "manifest.json is missing or malformed."),
    "FORMAT_UNSUPPORTED": (INVALID_ARCHIVE, "Unsupported archive format",
                           "Not a Lumina Agent Backup, or a newer or unknown version."),
    "MANIFEST_SCHEMA_INVALID": (INVALID_ARCHIVE, "The manifest failed validation",
                                "Its structure is not a valid Agent Backup manifest."),
    "ARCHIVE_NAMESPACE_INVALID": (INVALID_ARCHIVE, "Archive paths are outside the allowed layout",
                                  "Members sit where Agent Backups never place them."),
    "ARCHIVE_UNDECLARED_MEMBER": (INVALID_ARCHIVE, "Undeclared member in the archive",
                                  "The archive holds a member its manifest does not declare."),
    "ARCHIVE_INTEGRITY_FAILED": (INVALID_ARCHIVE, "The archive failed its integrity check",
                                 "Damage that cannot be isolated to individual units."),
    "LEGACY_LAYOUT_UNRECOGNIZED": (INVALID_ARCHIVE, "Not a recognised legacy backup",
                                   "It has no manifest and none of the files a Backup Memory ZIP holds."),
}

DAMAGE_TEXT = {
    "missing": "declared in the manifest but absent from the archive",
    "crc_or_inflate_failed": "failed ZIP integrity (CRC or decompression)",
    "hash_mismatch": "content does not match its recorded size or SHA-256",
    "sqlite_invalid": "SQLite database failed validation",
    "prefs_invalid": "preferences failed validation",
    "payload_policy": "content violates the export policy",
    "dependency_unavailable:primary_database": "needs the primary database, which is damaged or absent",
    "history_missing_unknown": "installation history is recorded as unknown (not damage; nothing to recover)",
    "ambiguous_database_generation": "pending WAL or journal frames: the file alone is not the generation the source saw",
    "json_invalid": "not valid JSON",
    "installation_marker_invalid": "installation marker is invalid",
}

ACTION_TEXT = {
    "create": "create",
    "already_present_identical": "already present and identical (left alone)",
    "keep_destination": "differs from the archive: your existing file is kept",
    "recorded_not_restored": "recorded in the receipt only; not restored",
}

CHOICE_TEXT = {
    "recover": "Recover",
    "recover_keep_destination": "Recover, keep my differing install files",
    "skip": "Skip",
}

_BAD_CHARS = re.compile("[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff]")
# Beyond the ASCII/C1 controls and the explicit bidi/zero-width ranges above,
# anything Unicode files under control (Cc), format (Cf: U+061C ARABIC LETTER
# MARK, the soft hyphen, U+180E, the tag characters ...) or a line/paragraph
# separator (Zl, Zp) can reorder or hide text, and the Hangul fillers draw as
# blanks. Display only: the archive's bytes and anything recovered are never
# altered by this.
_UNSAFE_CATEGORIES = frozenset(("Cc", "Cf", "Zl", "Zp"))
_BLANK_FILLERS = frozenset("\u115f\u1160\u3164\uffa0")
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def safe_text(value, limit=240):
    """Display-safe text for anything an archive, journal or exception could
    have shaped: control, bidi, zero-width and other invisible format
    characters become '?' (one for one, so the owner can see something was
    there), and the length is bounded."""
    if type(value) is str:
        text = value
    elif type(value) in (int, float, bool):
        text = str(value)
    elif value is None:
        text = ""
    else:
        text = f"<{type(value).__name__}>"
    if len(text) > limit:
        text = text[:limit - 1] + "…"
    text = _BAD_CHARS.sub("?", text)
    if not text.isascii():
        text = "".join("?" if (ch in _BLANK_FILLERS or unicodedata.category(ch) in _UNSAFE_CATEGORIES) else ch
                       for ch in text)
    return text


def normalize_folder(text):
    """A typed folder path as the engine will receive it: surrounding
    whitespace dropped and a leading ~ expanded. Nothing else is changed,
    and the preview shows the resulting absolute path."""
    return os.path.expanduser(text.strip()) if type(text) is str else ""


def human_bytes(n):
    if type(n) is not int or n < 0:
        return safe_text(n, 40)
    size = float(n)
    for unit in ("bytes", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{n} bytes" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024


def damage_text(reason):
    return DAMAGE_TEXT.get(reason) or "reported problem: " + safe_text(reason, 80)


def is_selectable(unit):
    return unit["availability"] != "unavailable" and unit["disposition"] != "informational"


def selectable_units(intake):
    return [u for u in intake.get("units", []) if is_selectable(u)]


def default_selection(intake):
    """What the table starts on: Recover for every recoverable unit of an
    archive with no damage (the engine's own default), Skip for all of them
    when the archive is selective or any unit is damaged, so that recovering
    from a damaged archive is always a deliberate owner choice. A starting
    point only: the owner sees and can change it, and the engine still
    decides what is valid."""
    damaged = any(u["disposition"] != "informational"
                  and any(s["reason"] != "history_missing_unknown" for s in u["skipped"])
                  for u in intake.get("units", []))
    clean = intake.get("kind") in (ri.VERIFIED, ri.LEGACY) and not damaged
    return {u["unit"]: ("recover" if clean else "skip") for u in selectable_units(intake)}


# --------------------------------------------------------------------------
# Archive identity, coverage and warnings
# --------------------------------------------------------------------------

_INSTALL_HISTORY = {
    "present": "Knowledge Pack installation history is present. It is recovered as historical installation "
               "state in the place Lumina's Knowledge Pack startup step reads, so it can influence whether "
               "official packs are installed again if Lumina is later started on that folder. It grants no "
               "authority and no official-package trust.",
    "missing_unknown": "Knowledge Pack installation history is UNKNOWN. Unknown is never proof of a first "
                       "run; it is not recovered.",
    "unknown": "Knowledge Pack installation history is UNKNOWN. Unknown is never proof of a first run; "
               "it is not recovered.",
    "not_captured_by_format": "This older format never captured Knowledge Pack installation history.",
    "invalid": "The Knowledge Pack installation marker is invalid and is not recovered.",
}
_C1_SOURCE = {
    "absent_unknown": "C1 state at the source: absent or unknown when exported.",
    "present_excluded": "C1 state at the source: present, and deliberately excluded from the export.",
    "not_recorded": "C1 state at the source: not recorded by this archive format.",
}


def intake_warnings(intake):
    """Coverage, exclusions, unknown history, damage and compatibility, as
    plain sentences. Nothing here is reassurance."""
    out = []
    kind = intake.get("kind")
    restore_class = CLASS_FOR_KIND.get(kind)
    if restore_class:
        out.append(f"Recovery class: {CLASS_TEXT[restore_class][0]}. {CLASS_TEXT[restore_class][1]}")
    out.append("Verified Agent Backup is unavailable: no archive can carry a C1 safety-state snapshot yet, "
               "so no recovery from it is labelled one.")
    verification = intake.get("verification") or {}
    status = verification.get("status")
    if status == "PASS":
        out.append("Verification PASS covers ZIP structure, member hashes and SQLite validity only. It is "
                   "not proof of origin, complete coverage or a successful full recovery.")
    elif status == "FAILED":
        out.append(f"Archive verification FAILED ({safe_text(verification.get('error_count'), 12)} error(s)). "
                   "Only units that verify independently can be recovered.")
    elif status == "not_applicable":
        out.append("Legacy ZIP: no manifest or hashes exist. Integrity is ZIP CRC and payload shape only; "
                   "origin and provenance are unknown.")
    cov = intake.get("coverage") or {}
    if cov.get("format_coverage") == "older_full_mode":
        out.append("Older v1 format: it did not capture the Knowledge Pack installation marker or the C1 "
                   "contract, and copied preferences under an older policy.")
    if "installation_history" in cov:
        out.append(_INSTALL_HISTORY.get(cov["installation_history"],
                                        "Installation history: unrecognised value "
                                        + safe_text(cov["installation_history"], 60)))
    out.append("C1 safety state is not captured and is never restored. After recovery C1 must be "
               "established by owner-local Recover (the owner starts suspended).")
    if "c1_at_source" in cov:
        out.append(_C1_SOURCE.get(cov["c1_at_source"],
                                  "C1 state at the source: unrecognised value " + safe_text(cov["c1_at_source"], 60)))
    if cov.get("c1_safety_state") == "evidence_only":
        out.append("The legacy ZIP holds archived C1 files; they are evidence only and are not restored.")
    if cov.get("unsupported_required_features"):
        out.append("REQUIRED FEATURES THIS VERSION CANNOT HANDLE: "
                   + ", ".join(safe_text(f, 60) for f in cov["unsupported_required_features"][:16])
                   + ". Recovery is refused.")
    count = cov.get("unclassified_preferences_omitted_at_export")
    if type(count) is int and count:
        out.append(f"{count} unclassified preference key(s) were omitted at export and cannot be recovered.")
    if cov.get("source_overlays") == "not_in_legacy_format":
        out.append("Skills, personas, avatars, voices, tool profiles and the project list are not in a "
                   "legacy ZIP.")
    if cov.get("excluded_entries_present"):
        out.append("The legacy ZIP held credential-like or excluded entries; they are never recovered.")
    if type(cov.get("unclassified_entries")) is int and cov["unclassified_entries"]:
        out.append(f"{cov['unclassified_entries']} unclassified legacy entr(ies) are ignored.")
    if cov.get("provenance") == "unknown":
        out.append("Provenance is unknown: nothing in a legacy ZIP vouches for where it came from.")
    for unit in intake.get("units", []):
        for skipped in unit["skipped"]:
            if skipped["reason"] == "history_missing_unknown":
                continue
            out.append(f"Cannot recover {safe_text(unit['unit'], 80)}: {safe_text(skipped['archive_path'], 120)} "
                       f"- {damage_text(skipped['reason'])}.")
    for absent in intake.get("units_not_in_archive", []):
        out.append(f"Not in this archive: {safe_text(absent.get('unit'), 60)} "
                   f"({safe_text(absent.get('reason'), 60)}).")
    out.append("Never recovered, whatever the archive holds: "
               + ", ".join(x.replace("_", " ") for x in ri._ALWAYS_EXCLUDED) + ".")
    out.append(TRUST_NOTE)
    return out


def summarize_intake(intake):
    """JSON-safe view of one intake for the dialog."""
    kind = intake.get("kind")
    restore_class = CLASS_FOR_KIND.get(kind)
    units = []
    for unit in intake.get("units", []):
        reasons = [f"{safe_text(s['archive_path'], 100)}: {damage_text(s['reason'])}" for s in unit["skipped"]]
        units.append({
            "unit": unit["unit"], "label": safe_text(unit["unit"], 80),
            "description": safe_text(unit.get("description"), 200),
            "disposition": unit["disposition"], "availability": unit["availability"],
            "selectable": is_selectable(unit), "reasons": reasons,
            "inactive_history": unit["disposition"] == "historical_inactive",
        })
    return {
        "name": safe_text(os.path.basename(intake.get("source_path") or ""), 120),
        "path": safe_text(intake.get("source_path"), 400),
        "sha256": intake.get("archive_sha256"), "size": intake.get("archive_size"),
        "size_text": human_bytes(intake.get("archive_size")),
        "kind": kind, "restore_class": restore_class,
        "class_label": CLASS_TEXT[restore_class][0] if restore_class else "Rejected",
        "format": safe_text(intake.get("format"), 60),
        "verification": safe_text((intake.get("verification") or {}).get("status"), 40),
        "units": units, "warnings": intake_warnings(intake),
    }


def render_intake_text(view):
    lines = [f"Archive: {view['name']}", f"Path: {view['path']}", f"SHA-256: {view['sha256']}",
             f"Size: {view['size_text']}", f"Format: {view['format'] or 'unrecognised'}",
             f"Verification: {view['verification'] or 'not applicable'}",
             f"Recovery class: {view['class_label']}", "", "WARNINGS AND LIMITS"]
    lines += [f"- {w}" for w in view["warnings"]]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Plan rendering
# --------------------------------------------------------------------------

def plan_problem(plan, session_selection, intake):
    """None if `plan` is what the owner chose: same archive as inspected,
    same selection as the table. A mismatch means the plan must not be shown
    as approvable."""
    if type(plan) is not dict or plan.get("schema") != ar.PLAN_SCHEMA:
        return "not an engine plan"
    if intake is not None and plan["archive"]["archive_sha256"] != intake.get("archive_sha256"):
        return "the archive changed since it was inspected"
    wanted = {u["unit"]: session_selection.get(u["unit"], "skip") for u in selectable_units(intake or {})}
    if intake is not None and plan["selection"] != wanted:
        return "the planned selection differs from the table"
    return None


def render_plan_text(plan, warnings=()):
    cls = CLASS_TEXT.get(plan["restore_class"], (plan["restore_class"], ""))
    dest = plan["destination"]
    archive = plan["archive"]
    ready = plan["status"] == "ready"
    lines = [
        "RECOVERY PLAN - nothing has been written",
        f"Plan digest: {plan['plan_digest']}",
        f"Status: {'READY for your approval' if ready else 'NEEDS YOUR DECISION on install-folder conflicts'}",
        f"Recovery class: {cls[0]}",
        "Verified Agent Backup: unavailable (C1 snapshot contract absent); this recovery is not one",
        f"Archive SHA-256: {archive['archive_sha256']}  ({human_bytes(archive['archive_size'])}, "
        f"{safe_text(archive['format'], 60)}, verification {safe_text((archive.get('verification') or {}).get('status'), 30)})",
        f"New data folder: {safe_text(dest['data_dir'], 400)}  "
        f"({'will be created' if dest['state'] == 'absent' else 'existing empty folder'})",
        f"Install folder: {safe_text(dest['install_dir'], 400)}",
        f"Free space needed (with margin): {human_bytes(plan['required_bytes'])}",
        f"Recovery id: {plan['restore_id']}  (inactive history is written under restore_history/{plan['restore_id']}/)",
    ]
    if warnings:
        lines += ["", "HEADS-UP"] + [f"- {safe_text(w, 300)}" for w in warnings]
    lines += ["", "UNITS"]
    for unit in plan["units"]:
        name = safe_text(unit["unit"], 80)
        choice = unit["choice"]
        lines.append(f"* {name}  [{CHOICE_TEXT.get(choice, 'Not recoverable' if choice == 'not_recoverable' else safe_text(choice, 40))}]"
                     f"  ({safe_text(unit['availability'], 20)}, {safe_text(unit['disposition'], 30)})")
        for f in unit["files"]:
            action = ACTION_TEXT.get(f.get("action"), safe_text(f.get("action"), 40))
            if "root" not in f:
                lines.append(f"    - {safe_text(f['archive_path'], 140)}: {action}")
                continue
            where = "install" if f["root"] == "install" else "data"
            lines.append(f"    - [{where}] {safe_text(f['rel'], 160)}: {action}; {human_bytes(f['size'])}; "
                         f"sha256 {f['sha256'][:16]}...")
        for skipped in unit["skipped"]:
            if skipped["reason"] != "history_missing_unknown":
                lines.append(f"    ! not recoverable: {safe_text(skipped['archive_path'], 140)} - "
                             f"{damage_text(skipped['reason'])}")
    conflicts = [(u["unit"], rel) for u in plan["units"] for rel in u["conflicts"]]
    if conflicts:
        lines += ["", "INSTALL-FILE CONFLICTS (this install folder already has a different file)"]
        for unit, rel in conflicts:
            choice = next(u["choice"] for u in plan["units"] if u["unit"] == unit)
            note = ("your file is kept and this is disclosed in the receipt" if choice == "recover_keep_destination"
                    else "choose 'Recover, keep my differing install files' or 'Skip' for this unit")
            lines.append(f"- {safe_text(rel, 160)} ({safe_text(unit, 60)}): {note}")
    if plan.get("units_not_in_archive"):
        lines += ["", "NOT IN THIS ARCHIVE"]
        lines += [f"- {safe_text(x.get('unit'), 60)}: {safe_text(x.get('reason'), 60)}"
                  for x in plan["units_not_in_archive"]]
    lines += ["", "THESE ALWAYS HOLD"] + [f"- {t}" for t in ALWAYS_TRUE]
    lines += ["", TRUST_NOTE]
    return "\n".join(lines)


def recovery_conflicts(plan):
    return [rel for u in plan["units"] for rel in u["conflicts"]]


def count_recovering(selection):
    return sum(1 for v in selection.values() if v in ("recover", "recover_keep_destination"))


# --------------------------------------------------------------------------
# Result construction (nothing here raises)
# --------------------------------------------------------------------------

def _failure(op, code, category, title, guidance, detail="", rollback=None, extra=None):
    result = {"ok": False, "op": op, "code": code, "category": category, "title": title,
              "guidance": guidance, "detail": safe_text(detail, 300), "rollback": rollback}
    if rollback is None:
        result["rollback_lines"] = []
    else:
        result["rollback_lines"] = rollback_lines(rollback)
    result.update(extra or {})
    return result


def rollback_lines(rollback):
    lines = [f"Staging folder removed: {'yes' if rollback.get('staging_removed') else 'NO - a hidden .restore-*.staging folder is still next to the destination'}",
             f"Destination untouched: {'yes' if rollback.get('destination_untouched') else 'NO - inspect it before use'}"]
    left = rollback.get("install_files_left") or []
    kept = rollback.get("install_paths_preserved") or []
    if left:
        lines.append(f"Install-folder files this attempt created and could not remove ({len(left)}): "
                     + "; ".join(safe_text(p, 160) for p in left[:8]))
    if kept:
        lines.append(f"Install-folder paths left in place (not provably this attempt's) ({len(kept)}): "
                     + "; ".join(safe_text(p, 160) for p in kept[:8]))
    return lines


def refusal_failure(op, exc):
    """A RestoreRefused as a result dict."""
    code = exc.code
    category, title, guidance = REFUSAL_GUIDE[code]
    detail = exc.detail
    if code == "ARCHIVE_REJECTED" and detail in REFUSAL_GUIDE:
        category, title, guidance = REFUSAL_GUIDE[detail]
    rollback = getattr(exc, "rollback", None)
    if rollback is None and op == "execute":
        guidance += " " + _NOTHING
    return _failure(op, code, category, title, guidance, detail, rollback)


def _current_sha(archive_path):
    try:
        return ri.intake_archive(archive_path).get("archive_sha256")
    except Exception:
        return None


def archive_changed_failure(op, expected, now):
    seen = (now or "unreadable")
    return _failure(op, "ARCHIVE_CHANGED", PLAN_STALE, REFUSAL_GUIDE["ARCHIVE_CHANGED"][1],
                    "Its bytes differ from the archive you inspected "
                    f"(SHA-256 {safe_text(expected, 80)[:16]}... then, {safe_text(seen, 80)[:16]}... now). "
                    "Nothing was written. Inspect it again, then preview.",
                    f"inspected {safe_text(expected, 80)}; now {safe_text(seen, 80)}")


def unexpected_failure(op, exc):
    """Only the error class (and, for OS errors, the OS's own wording) is
    shown: other exception text can carry archive-derived content."""
    name = type(exc).__name__
    detail = name
    if isinstance(exc, OSError) and exc.errno:
        detail = f"{name}: {errno.errorcode.get(exc.errno, exc.errno)} ({safe_text(exc.strerror, 80)})"
    return _failure(op, "UNEXPECTED_ERROR", UNEXPECTED_ERROR, "Something unexpected stopped this step",
                    "The reason is not shown because it may contain archive-derived text. No success was "
                    "recorded. If this was a recovery, inspect the destination before using it and use "
                    "'Check for interrupted recovery'.", detail)


# --------------------------------------------------------------------------
# Operations (worker threads call these; none raises)
# --------------------------------------------------------------------------

def inspect_archive(path):
    """Read-only, archive only. No destination is consulted."""
    try:
        intake = ri.intake_archive(path)
        if intake["kind"] == ri.REJECTED:
            code = intake["refusal"]["code"]
            category, title, guidance = REFUSAL_GUIDE[code]
            result = _failure("inspect", code, category, title, guidance, intake["refusal"].get("detail", ""))
            result["intake"], result["sha256"] = intake, intake.get("archive_sha256")
            return result
        return {"ok": True, "op": "inspect", "intake": intake, "view": summarize_intake(intake)}
    except Exception as exc:
        return unexpected_failure("inspect", exc)


def _writable_hint(parent, install_dir, plan):
    notes = []
    if not os.access(parent, os.W_OK | os.X_OK):
        notes.append("The destination's parent folder does not look writable to this account; writing may fail.")
    creates = any(f.get("root") == "install" and f.get("action") == "create"
                  for u in plan["units"] for f in u["files"])
    if creates and not os.access(install_dir, os.W_OK | os.X_OK):
        notes.append("The install folder does not look writable to this account; writing may fail.")
    return notes


def build_plan(archive_path, data_dir, base_dir, selection, inspected_sha256=None):
    try:
        plan = ar.plan_restore(archive_path, data_dir, base_dir, selection=dict(selection))
    except ar.RestoreRefused as exc:
        if inspected_sha256 and exc.code in REPLAN_CODES:
            now = _current_sha(archive_path)
            if now != inspected_sha256:
                return archive_changed_failure("preview", inspected_sha256, now)
        return refusal_failure("preview", exc)
    except Exception as exc:
        return unexpected_failure("preview", exc)
    try:
        warnings = _writable_hint(os.path.dirname(plan["destination"]["data_dir"]),
                                  plan["destination"]["install_dir"], plan)
    except Exception:
        warnings = []
    return {"ok": True, "op": "preview", "plan": plan, "warnings": warnings}


def receipt_problem(receipt):
    """None if a receipt loaded from disk has the shape verify_restored_root
    relies on; otherwise why it must not be handed to it. A loaded receipt is
    untrusted input (its paths are joined onto folders)."""
    def safe_rel(rel):
        return (type(rel) is str and rel and not rel.startswith("/") and "\x00" not in rel
                and all(p not in ("", ".", "..") for p in rel.split("/")))
    try:
        if type(receipt) is not dict or receipt.get("schema") != ar.RECEIPT_SCHEMA:
            return "not a recovery receipt"
        if type(receipt.get("restore_id")) is not str or not _HEX32.match(receipt["restore_id"]):
            return "bad recovery id"
        install = receipt["destination"]["install_dir"]
        if type(install) is not str or not os.path.isabs(install) or "\x00" in install:
            return "bad install folder"
        if type(receipt.get("receipt_digest")) is not str:
            return "no receipt digest"
        for key in ("data_files", "install_files"):
            if type(receipt[key]) is not list:
                return f"bad {key}"
            for f in receipt[key]:
                if type(f) is not dict or not safe_rel(f.get("rel")) or not _HEX64.match(str(f.get("sha256"))):
                    return f"bad entry in {key}"
                if key == "install_files" and type(f.get("action")) is not str:
                    return f"bad entry in {key}"
        if type(receipt["sqlite"]) is not dict or not all(safe_rel(k) for k in receipt["sqlite"]):
            return "bad sqlite list"
    except (KeyError, TypeError):
        return "incomplete receipt"
    return None


def read_stored_receipt_bytes(data_dir, restore_id):
    """The exact bytes of the receipt the engine left in a published root, or
    None. Never follows a link (in the path or at the file), never reads
    more than RECEIPT_READ_LIMIT."""
    if type(restore_id) is not str or not _HEX32.match(restore_id):
        return None
    try:
        data_dir = os.path.abspath(os.fspath(data_dir))
        path = os.path.join(data_dir, ar.HISTORY_DIRNAME, restore_id, ar.RECEIPT_NAME)
        if os.path.realpath(path) != os.path.join(os.path.realpath(data_dir), ar.HISTORY_DIRNAME,
                                                  restore_id, ar.RECEIPT_NAME):
            return None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_size > RECEIPT_READ_LIMIT:
                return None
            return fh.read(RECEIPT_READ_LIMIT + 1)
    except OSError:
        return None


def load_stored_receipt(data_dir, restore_id):
    """The parsed, shape-checked stored receipt, or None."""
    raw = read_stored_receipt_bytes(data_dir, restore_id)
    if raw is None:
        return None
    try:
        receipt = ab._strict_json_loads(raw)
    except (ValueError, ab.AgentBackupError):
        return None
    return receipt if receipt_problem(receipt) is None and receipt["restore_id"] == restore_id else None


def receipt_display_text(raw):
    """Pretty, escape-only (ASCII) rendering of stored receipt bytes: control
    and bidi characters in archive-derived paths cannot reach a text widget."""
    try:
        return json.dumps(ab._strict_json_loads(raw), indent=2, sort_keys=True, ensure_ascii=True)
    except (ValueError, ab.AgentBackupError, TypeError):
        return None


def _verify(data_dir, receipt):
    if receipt_problem(receipt):
        return {"passed": False, "checks": {}, "error": "the receipt failed its shape check"}
    try:
        return ar.verify_restored_root(data_dir, receipt)
    except Exception as exc:
        return {"passed": False, "checks": {}, "error": f"verification could not complete ({type(exc).__name__})"}


def _published_result(op, receipt, data_dir, verification, warnings=()):
    receipt_path = os.path.join(data_dir, ar.HISTORY_DIRNAME, receipt["restore_id"], ar.RECEIPT_NAME)
    base = {"op": op, "receipt": receipt, "verification": verification, "receipt_path": receipt_path,
            "data_dir": data_dir, "warnings": list(warnings)}
    if verification["passed"]:
        return {"ok": True, "outcome": "published_verified", **base}
    return {"ok": False, "outcome": "published_unverified", "category": PUBLISHED_UNVERIFIED,
            "title": "Published, but verification FAILED - do not use this folder",
            "guidance": "The folder was created but did not pass post-publication verification. Nothing "
                        "was activated. Inspect the failing checks; do not point Lumina at it.",
            "code": "VERIFICATION_FAILED", "detail": "", "rollback": None, "rollback_lines": [], **base}


def execute_recovery(plan, archive_path, approved_plan_digest):
    """restore_isolated() then verify_restored_root(), as one worker job."""
    try:
        data_dir = plan["destination"]["data_dir"]
        restore_id = plan["restore_id"]
    except Exception as exc:
        return unexpected_failure("execute", exc)
    try:
        receipt = ar.restore_isolated(plan, archive_path, approved_plan_digest=approved_plan_digest)
    except ar.RestoreRefused as exc:
        if exc.code in REPLAN_CODES:
            expected = plan["archive"]["archive_sha256"]
            now = _current_sha(archive_path)
            if now != expected:
                return archive_changed_failure("execute", expected, now)
        return refusal_failure("execute", exc)
    except Exception as exc:
        # Past the publishing rename the engine re-raises whatever it hit
        # (a late fsync, say) with the folder complete. Say what is true.
        stored = load_stored_receipt(data_dir, restore_id)
        if stored is not None and stored.get("plan_digest") == approved_plan_digest:
            verification = _verify(data_dir, stored)
            return _published_result("execute", stored, data_dir, verification, [
                f"The engine reported an error after publishing ({type(exc).__name__}); the folder is "
                "complete. Directory durability may be unconfirmed."])
        result = unexpected_failure("execute", exc)
        result["destination_exists"] = os.path.lexists(data_dir)
        result["guidance"] += (" The destination now exists and was not verified." if result["destination_exists"]
                               else " The destination does not exist.")
        return result
    return _published_result("execute", receipt, data_dir, _verify(data_dir, receipt))


def describe_recovery_report(report):
    lines = []
    for rid in report.get("rolled_back", []):
        lines.append(f"Rolled back an interrupted recovery ({safe_text(rid, 40)}): its staging folder and the "
                     "install files it created were removed.")
    for staging in report.get("cleared_never_journaled", []):
        lines.append(f"Removed an empty staging folder that never wrote its journal: {safe_text(staging, 300)}")
    for staging in report.get("left_alone", []):
        lines.append(f"LEFT ALONE (not recognised as this engine's): {safe_text(staging, 300)}")
    for moved in report.get("quarantined", []):
        lines.append(f"Moved aside, never deleted: {safe_text(moved.get('from'), 240)} -> {safe_text(moved.get('to'), 240)}")
    for path in report.get("install_files_left", []):
        lines.append(f"Could not remove an install file this attempt created: {safe_text(path, 300)}")
    for path in report.get("install_paths_preserved", []):
        lines.append(f"Left in place (not provably this attempt's): {safe_text(path, 300)}")
    return lines or ["No interrupted recovery was found for this destination."]


def recover_interrupted(data_dir, quarantine_unrecognized=False):
    op = "quarantine" if quarantine_unrecognized else "interrupted"
    try:
        report = ar.recover_interrupted_restore(data_dir, quarantine_unrecognized=quarantine_unrecognized)
    except ar.RestoreRefused as exc:
        return refusal_failure(op, exc)
    except Exception as exc:
        return unexpected_failure(op, exc)
    return {"ok": True, "op": op, "report": report, "lines": describe_recovery_report(report),
            "needs_quarantine_decision": bool(report.get("left_alone"))}


def verify_recovered_folder(data_dir):
    """Read-only re-check of a previously recovered folder against the
    receipt stored inside it. It detects changes made after recovery; it
    cannot prove that stored receipt is the original."""
    try:
        data_dir = os.path.abspath(os.fspath(data_dir))
        history = os.path.join(data_dir, ar.HISTORY_DIRNAME)
        if os.path.islink(history) or not os.path.isdir(history):
            return _failure("verify_folder", "NO_RECEIPT", INVALID_ARCHIVE, "No recovery receipt found",
                            "This folder has no restore_history/ recorded by an isolated recovery.")
        with os.scandir(history) as it:
            ids = sorted(e.name for e in it if _HEX32.match(e.name) and e.is_dir(follow_symlinks=False))
        if len(ids) != 1:
            return _failure("verify_folder", "NO_RECEIPT", INVALID_ARCHIVE, "Expected exactly one recovery receipt",
                            f"Found {len(ids)}. A recovered folder holds one.")
        receipt = load_stored_receipt(data_dir, ids[0])
        if receipt is None:
            return _failure("verify_folder", "RECEIPT_UNREADABLE", INVALID_ARCHIVE, "The stored receipt is unreadable",
                            "It is missing, too large, not strict JSON, or not shaped like a receipt.")
        result = _published_result("verify_folder", receipt, data_dir, _verify(data_dir, receipt))
        result["caveat"] = ("Checked against the receipt stored inside this folder. This detects changes made "
                            "after recovery; it cannot prove the receipt itself is the original.")
        return result
    except Exception as exc:
        return unexpected_failure("verify_folder", exc)


def describe_checks(verification):
    names = {
        "receipt_matches": "Stored receipt matches and its digest is intact",
        "data_files_match": "Every recovered data file matches its recorded SHA-256",
        "install_files_match": "Every recovered install file matches (kept destination files excepted)",
        "sqlite_integrity": "SQLite integrity_check passes on every database",
        "sqlite_schema_vetted": "No archived triggers/views/generated columns; shipped schema rules hold",
        "skill_authority_fenced": "No Skill claims official status",
        "no_live_authority_names": "No live authority or C1 filenames are present",
        "no_live_project_bindings": "No live project root bindings are present",
        "prefs_fenced": "Preferences hold no credentials and Telegram remote access is off",
        "c1_restore_trace": "C1 restore trace present (first-run Initialize refused)",
    }
    lines = [f"[{'PASS' if ok else 'FAIL'}] {names.get(key, safe_text(key, 60))}"
             for key, ok in (verification.get("checks") or {}).items()]
    if verification.get("error"):
        lines.append("[FAIL] " + safe_text(verification["error"], 200))
    return lines


def describe_success(result):
    receipt = result["receipt"]
    cls = CLASS_TEXT.get(receipt.get("restore_class"), (safe_text(receipt.get("restore_class"), 40), ""))[0]
    lines = [
        "ISOLATED RECOVERY PUBLISHED AND VERIFIED" if result["ok"] else "PUBLISHED - VERIFICATION FAILED",
        f"Recovery class: {cls}",
        "Verified Agent Backup: not available (C1 snapshot contract absent)",
        f"New data folder: {safe_text(result['data_dir'], 400)}",
        f"Receipt: {safe_text(result['receipt_path'], 400)}",
        f"Receipt state: {safe_text(receipt.get('state'), 60)} (nothing resumed, replayed or activated)",
        f"Plan digest: {safe_text(receipt.get('plan_digest'), 70)}",
        "", "VERIFICATION (read-only re-check of the published folder)",
    ] + describe_checks(result["verification"])
    for w in result.get("warnings", []):
        lines += ["", "NOTE: " + safe_text(w, 300)]
    if result.get("caveat"):
        lines += ["", result["caveat"]]
    kept = [rel for u in receipt.get("units", []) for rel in u.get("conflicts", [])]
    if kept:
        lines += ["", "Install files kept as they were (they differ from the archive): "
                  + "; ".join(safe_text(r, 120) for r in kept[:12])]
    lines += ["", "WHAT THIS IS NOT"] + [f"- {t}" for t in NEXT_STEPS] + [f"- {t}" for t in ALWAYS_TRUE]
    return "\n".join(lines)


def describe_failure(result):
    lines = [safe_text(result["title"], 200), "", "Kind: " + result["category"].replace("_", " "),
             safe_text(result["guidance"], 600)]
    if result.get("detail"):
        lines.append("Detail: " + result["detail"])
    for line in result.get("rollback_lines", []):
        lines.append(line)
    if result.get("verification"):
        lines += ["", "VERIFICATION"] + describe_checks(result["verification"])
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Owner-consent state machine
# --------------------------------------------------------------------------

class SessionError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class RestoreSession:
    """What the owner has chosen, and what they have approved.

    A plan exists only for exactly the inputs it was made from; approving it
    binds the owner's consent to its plan_digest; and one execution attempt
    consumes the approval whatever its outcome. Results that arrive for an
    older generation are dropped, never applied.
    """

    def __init__(self):
        self.generation = 0
        self.archive_path = None
        self.intake = None
        self.data_dir = ""
        self.base_dir = ""
        self.selection = {}
        self.plan = None
        self._plan_inputs = None
        self.approved_digest = None
        self.executing = False
        self.invalidated_because = None

    # -- invalidation ------------------------------------------------------
    def _drop_plan(self, reason):
        self.generation += 1
        if self.plan is not None or self.approved_digest is not None:
            self.invalidated_because = reason
        self.plan = self._plan_inputs = self.approved_digest = None

    def _inputs(self):
        return (self.archive_path, self.data_dir, self.base_dir, tuple(sorted(self.selection.items())))

    # -- inputs ------------------------------------------------------------
    def set_archive(self, path):
        if self.executing:
            raise SessionError("EXECUTING")
        self._drop_plan("the archive changed")
        self.archive_path, self.intake, self.selection = path, None, {}
        return self.generation

    def require_reinspection(self, because):
        """The archive no longer matches what was inspected: forget the
        inspection and everything built on it."""
        if self.executing:
            raise SessionError("EXECUTING")
        self._drop_plan(because)
        self.intake, self.selection = None, {}
        self.invalidated_because = because
        return self.generation

    def accept_intake(self, generation, result):
        if generation != self.generation or self.executing or not result.get("ok"):
            return False
        self.intake = result["intake"]
        self.selection = default_selection(self.intake)
        return True

    def set_folders(self, data_dir, base_dir):
        if (data_dir, base_dir) != (self.data_dir, self.base_dir):
            if self.executing:
                raise SessionError("EXECUTING")
            self._drop_plan("a folder changed")
            self.data_dir, self.base_dir = data_dir, base_dir

    def set_choice(self, unit, choice):
        if self.intake is None or unit not in self.selection or choice not in ar.CHOICES:
            raise SessionError("SELECTION_INVALID")
        if self.selection[unit] != choice:
            if self.executing:
                raise SessionError("EXECUTING")
            self._drop_plan("the selection changed")
            self.selection[unit] = choice

    # -- preview -----------------------------------------------------------
    def preview_blocker(self):
        if self.intake is None:
            return "Inspect an archive first."
        if not self.data_dir.strip():
            return "Choose the new data folder."
        if not self.base_dir.strip():
            return "Choose the install folder."
        if count_recovering(self.selection) == 0:
            return "Nothing is selected to recover."
        return None

    def request_preview(self):
        blocker = self.preview_blocker()
        if blocker or self.executing:
            raise SessionError("PREVIEW_BLOCKED")
        self._drop_plan("a new preview was requested")
        return {"generation": self.generation, "archive_path": self.archive_path,
                "data_dir": normalize_folder(self.data_dir), "base_dir": normalize_folder(self.base_dir),
                "selection": dict(self.selection), "inspected_sha256": self.intake["archive_sha256"]}

    def accept_plan(self, generation, request, result):
        """Keep the plan only if it is for the current inputs. Returns None
        when accepted, else the reason it was dropped."""
        if generation != self.generation or self.executing:
            return "stale"
        if not result.get("ok"):
            return "refused"
        plan = copy.deepcopy(result["plan"])
        problem = plan_problem(plan, self.selection, self.intake)
        if problem:
            self.invalidated_because = problem
            return problem
        self.plan, self._plan_inputs = plan, self._inputs()
        return None

    # -- consent -----------------------------------------------------------
    def approve(self, digest):
        """Bind consent to the plan the owner is looking at."""
        if (self.plan is None or self.plan["status"] != "ready" or self._plan_inputs != self._inputs()
                or type(digest) is not str or digest != self.plan["plan_digest"]):
            self.approved_digest = None
            return False
        self.approved_digest = digest
        return True

    def revoke(self):
        self.approved_digest = None

    @property
    def can_execute(self):
        return (not self.executing and self.plan is not None and self.plan["status"] == "ready"
                and self._plan_inputs == self._inputs() and self.approved_digest == self.plan["plan_digest"])

    def begin_execution(self):
        if not self.can_execute:
            raise SessionError("NOT_APPROVED")
        self.executing = True
        return {"generation": self.generation, "plan": copy.deepcopy(self.plan),
                "archive_path": self.archive_path, "digest": self.approved_digest}

    def finish_execution(self, because):
        """One attempt consumes the plan and the approval, success or not."""
        self.executing = False
        self.invalidated_because = because
        self.generation += 1
        self.plan = self._plan_inputs = self.approved_digest = None
