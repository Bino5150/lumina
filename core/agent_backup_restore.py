"""core/agent_backup_restore.py -- isolated staging Restore
(AGENT-BACKUP-RESTORE-02, first slice).

Turns an intake (core.agent_backup_restore_intake) into a restored data
folder, but only into a NEW one: the destination data folder must be absent
or an empty directory, and it is never a production data folder. This is an
engineering recovery path for an isolated test root, not production
disaster-recovery activation; nothing here touches a live Lumina.

Lifecycle:

  plan_restore()      intake -> integrity -> coverage class -> preview ->
                      owner selection -> plan with a plan_digest
  restore_isolated()  re-derives the plan and refuses unless it reproduces the
                      digest the owner approved; stages every selected unit in
                      a private sibling directory, validates it, writes the C1
                      restore trace, then publishes the whole data folder with
                      one no-replace rename. Before that rename nothing exists
                      at the destination; after it, everything does.
  verify_restored_root()  read-only acceptance of a published root.
  recover_interrupted_restore()  rolls back what a killed restore left.

History is not authority. Restored chats, memories, Palace, Skills text,
preferences and projects are history. Custom tools, the tool audit log (the
custom-tool approval record), pending actions and project root bindings are
copied only into restore_history/<restore_id>/, where nothing loads them.
Preferences pass the export scrubber (credentials, PIN and owner/channel
bindings dropped, Telegram remote access off). Skills rows are rebound to the
destination install. No restored Skill is official: that status comes only
from the destination checkout's own bootstrap of its tracked packages, and the
skills table must have exactly the shipped shape. Archived triggers and views
are dropped before any write; only the shipped search-sync triggers survive,
by exact definition. Archived C1 state is never
restored; the published root carries a C1 restore trace, so C1 probes
ABSENT_WITH_TRACE and the owner must Recover (owner suspended, fresh identity)
before anything is admitted.

No archive format captures a C1 snapshot, so no restore here is a Verified
Agent Backup: a verified A2/export archive restores as an Incomplete State
Export, a damaged one only as owner-selected Selective Recovery, a
manifestless ZIP as Legacy Recovery.

Linux only (the publishing rename is renameat2 RENAME_NOREPLACE).
"""
from __future__ import annotations

import contextlib
import ctypes
import errno
import hashlib
import io
import itertools
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import urllib.parse
import zipfile
from datetime import datetime, timezone

from core import agent_backup as ab
from core import agent_backup_export as abx
from core import agent_backup_restore_intake as ri
from core import suspension_backup as c1

PLAN_SCHEMA = "lumina.restore_plan"
RECEIPT_SCHEMA = "lumina.restore_receipt"
JOURNAL_SCHEMA = "lumina.restore_journal"
VERSION = 1

HISTORY_DIRNAME = "restore_history"
RECEIPT_NAME = "receipt.json"
JOURNAL_NAME = "journal.json"
SPACE_MARGIN_BYTES = 32 * 1024 * 1024
# The journal is rewritten (and fsynced) about three times per install file, so
# its cost grows with the square of the count: 1,000 files is roughly a 420 KB
# journal and a few hundred MB of journal writes. Real installs hold tens.
MAX_INSTALL_FILES = 1000
JOURNAL_READ_LIMIT = 16 * 1024 * 1024

INCOMPLETE = "incomplete_state_export"
SELECTIVE = "selective_recovery"
LEGACY = "legacy_recovery"
VERIFIED_AGENT_BACKUP = {"available": False, "reason": "c1_snapshot_contract_absent"}

CHOICES = ("recover", "recover_keep_destination", "skip")

REFUSALS = frozenset({
    "PRODUCTION_ROOT",                  # destination or install folder is live owner state
    "DESTINATION_NOT_EMPTY",            # exists and is not an empty real directory
    "DESTINATION_PARENT_UNSAFE",
    "DESTINATION_CHANGED",              # appeared, or its identity changed, before publishing
    "INSTALL_FOLDER_UNSAFE",
    "INTERRUPTED_RESTORE_PENDING",      # a killed restore left staging behind
    "ARCHIVE_REJECTED",
    "ARCHIVE_CHANGED",                  # bytes differ from the planned archive
    "UNSUPPORTED_REQUIRED_FEATURES",
    "SELECTION_REQUIRED",               # selective recovery needs an explicit owner choice
    "SELECTION_INVALID",
    "OWNER_CONFLICT_CHOICE_REQUIRED",
    "PLAN_NOT_APPROVED",                # approved digest missing or not this plan
    "PLAN_CHANGED",                     # a fresh plan no longer reproduces the digest
    "INSUFFICIENT_SPACE",
    "MEMBER_INVALID",
    "SQLITE_INVALID",
    "SKILL_AUTHORITY_UNVERIFIED",       # a Skill row still claims official after the fence
    "C1_TRACE_FAILED",
    "STAGED_VALIDATION_FAILED",
    "INSTALL_TARGET_CHANGED",
    "INSTALL_FILE_LIMIT",               # more install files than the journal is built for
    "COMMIT_PRIMITIVE_UNAVAILABLE",
    "WRITE_FAILED",
})

# Live filenames whose presence would make restored history act as authority.
_FORBIDDEN_LIVE = ("memory/suspension.db", "memory/suspension.db-wal", "memory/suspension.db-shm",
                   "memory/suspension.db-journal", "memory/soul_identity.json", "memory/ledger.db",
                   "memory/tool_audit.log", "memory/pending_actions.json",
                   "memory/pending_actions_audit.log", "custom_tools", "credentials.json")

_INSTALL_DIRS = {"skills": "skills", "personas": "personas", "avatars": "assets/avatars",
                 "voices": "assets/voices", "tool_profiles": "tool_profiles"}


class RestoreRefused(Exception):
    def __init__(self, code, detail="", rollback=None):
        assert code in REFUSALS, code
        super().__init__(code + (f": {detail}" if detail else ""))
        self.code, self.detail, self.rollback = code, detail, rollback


def _checkpoint(stage):
    """No-op stage marker; tests replace it to simulate a crash at `stage`."""


def _digest(value):
    return hashlib.sha256(abx._json_bytes(value)).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _free_bytes(path):
    return shutil.disk_usage(path).free


# --------------------------------------------------------------------------
# Folder safety
# --------------------------------------------------------------------------

def _production_roots():
    from platformdirs import user_data_dir
    home = os.path.expanduser("~")
    roots = {os.path.join(home, ".local", "share", "lumina"),
             os.path.join(home, ".local", "share", "lumina-release"),
             os.path.join(home, ".config", "lumina"), user_data_dir("lumina", appauthor=False)}
    return sorted(os.path.realpath(r) for r in roots)


def _within(path, root):
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _refuse_production(path, label):
    """Unconditional in this slice: no restore into live owner state or the
    running installation, with or without the test-isolation guard."""
    real = os.path.realpath(path)
    for root in _production_roots():
        if _within(real, root) or _within(root, real):
            raise RestoreRefused("PRODUCTION_ROOT", label)
    live = os.environ.get("LUMINA_DATA_DIR")
    if live and _within(real, os.path.realpath(live)):
        raise RestoreRefused("PRODUCTION_ROOT", f"{label} is this process's data folder")
    install = os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if _within(real, install):
        raise RestoreRefused("PRODUCTION_ROOT", f"{label} is inside the running installation")


def _destination_state(data_dir):
    try:
        st = os.lstat(data_dir)
    except FileNotFoundError:
        return {"state": "absent"}
    except OSError as exc:
        raise RestoreRefused("DESTINATION_NOT_EMPTY", type(exc).__name__) from None
    if not stat.S_ISDIR(st.st_mode):
        raise RestoreRefused("DESTINATION_NOT_EMPTY", "not a real directory")
    try:
        with os.scandir(data_dir) as it:
            if next(it, None) is not None:
                raise RestoreRefused("DESTINATION_NOT_EMPTY", "a restore never merges into existing state")
    except OSError as exc:
        raise RestoreRefused("DESTINATION_NOT_EMPTY", type(exc).__name__) from None
    return {"state": "empty_directory", "identity": [st.st_dev, st.st_ino]}


def _real_dir(path, code, label):
    try:
        st = os.lstat(path)
    except OSError:
        raise RestoreRefused(code, f"{label} missing") from None
    if not stat.S_ISDIR(st.st_mode):
        raise RestoreRefused(code, f"{label} is not a real directory")
    return [st.st_dev, st.st_ino]


def _staging_prefix(data_dir):
    return "." + os.path.basename(data_dir) + ".restore-"


def _leftover_staging(data_dir):
    parent, prefix = os.path.dirname(data_dir), _staging_prefix(data_dir)
    try:
        with os.scandir(parent) as it:
            return sorted(e.path for e in it if e.name.startswith(prefix) and e.name.endswith(".staging"))
    except OSError:
        raise RestoreRefused("DESTINATION_PARENT_UNSAFE") from None


def _lstat_chain(root, rel):
    """('absent'|'file'|'unsafe', st) for root/rel, never following a link."""
    path, parts = root, rel.split("/")
    for i, part in enumerate(parts):
        path = os.path.join(path, part)
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return "absent", None
        except OSError:
            return "unsafe", None
        last = i == len(parts) - 1
        if stat.S_ISLNK(st.st_mode) or (not last and not stat.S_ISDIR(st.st_mode)):
            return "unsafe", None
        if last:
            return ("file", st) if stat.S_ISREG(st.st_mode) else ("unsafe", None)


def _sha256_file(path):
    h = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------
# Placement: where each recoverable archive member goes
# --------------------------------------------------------------------------

def _placement(archive_path, restore_id):
    """(root, rel, transform) for one member; root is "data", "install" or
    None (recorded, never written). Raises on a path no unit should hold."""
    history = f"{HISTORY_DIRNAME}/{restore_id}"
    fixed = {
        "state/databases/lumina.db": ("data", "memory/lumina.db", "primary_database"),
        "memory/lumina.db": ("data", "memory/lumina.db", "primary_database"),
        "state/telemetry/flight_recorder.db": ("data", "telemetry/flight_recorder.db", "sqlite"),
        "telemetry/flight_recorder.db": ("data", "telemetry/flight_recorder.db", "sqlite"),
        "state/preferences/prefs.json": ("data", "memory/prefs.json", "preferences"),
        "memory/prefs.json": ("data", "memory/prefs.json", "preferences"),
        abx.MARKER_PATH: ("data", "knowledge_packs_installed.json", "marker_v2"),
        "knowledge_packs_installed.json": ("data", "knowledge_packs_installed.json", "marker_legacy"),
        "overlay/projects/projectlist.md": ("install", "projects/projectlist.md", "verbatim"),
    }
    if archive_path in fixed:
        return fixed[archive_path]
    parts = archive_path.split("/")
    if archive_path.startswith("metadata/"):
        return None, None, "informational"
    if parts[0] == "state" and parts[1] == "custom_tools" or parts[0] == "custom_tools":
        rest = "/".join(parts[2:] if parts[0] == "state" else parts[1:])
        return "data", f"{history}/custom_tools/{rest}", "verbatim"
    if parts[:2] == ["state", "audit"] or (parts[0] == "memory" and len(parts) == 2):
        return "data", f"{history}/audit/{parts[-1]}", "verbatim"
    if parts[0] == "overlay" and parts[1] in _INSTALL_DIRS:
        return "install", f"{_INSTALL_DIRS[parts[1]]}/{'/'.join(parts[2:])}", "verbatim"
    project = parts[2:] if parts[:2] == ["overlay", "projects"] else parts[1:] if parts[0] == "projects" else None
    if project and len(project) == 2:
        name, leaf = project
        if leaf == "binding.json":
            return "data", f"{history}/projects/{name}/binding.json", "verbatim"
        if leaf == "chats.json":
            return "data", f"projects/{name}/chats.json", "verbatim"
        if leaf in ("project.md", "codebase.md"):
            return "install", f"projects/{name}/{leaf}", "verbatim"
    raise RestoreRefused("MEMBER_INVALID", f"no placement for {archive_path}")


def _capture_archive(archive_path, expected_sha256):
    try:
        raw = ri._capture(archive_path)
    except ri._Refused as exc:
        raise RestoreRefused("ARCHIVE_CHANGED", exc.code) from None
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise RestoreRefused("ARCHIVE_CHANGED", "archive bytes differ from the planned archive")
    return raw


def _member_bytes(z, name):
    data = ri._read(z, name)
    if data is None:
        raise RestoreRefused("MEMBER_INVALID", name)
    return data


# --------------------------------------------------------------------------
# Plan
# --------------------------------------------------------------------------

def _restore_class(kind):
    return {ri.VERIFIED: INCOMPLETE, ri.SELECTIVE: SELECTIVE, ri.LEGACY: LEGACY}[kind]


def _resolve_selection(intake, units, selection):
    candidates = {u["unit"] for u in units if u["availability"] != "unavailable"
                  and u["disposition"] != "informational"}
    if selection is None:
        # A default restore takes everything or nothing: a damaged or
        # dependency-blocked unit is never dropped without an owner choice.
        # Installation history recorded as unknown is honest absence, not damage.
        damaged = sorted(u["unit"] for u in units if u["disposition"] != "informational" and any(
            s["reason"] != "history_missing_unknown" for s in u["skipped"]))
        if intake["kind"] == ri.SELECTIVE or damaged:
            raise RestoreRefused("SELECTION_REQUIRED", "damaged units need an owner choice: "
                                 + ", ".join(damaged or ["archive verification failed"]))
        return {name: "recover" for name in sorted(candidates)}
    if type(selection) is not dict or any(type(k) is not str or v not in CHOICES
                                          for k, v in selection.items()):
        raise RestoreRefused("SELECTION_INVALID")
    unknown = set(selection) - candidates
    if unknown:
        raise RestoreRefused("SELECTION_INVALID", f"not recoverable here: {sorted(unknown)}")
    return {name: selection.get(name, "skip") for name in sorted(candidates)}


def plan_restore(archive_path, data_dir, base_dir, *, selection=None, restore_id=None):
    """Describe exactly what restore_isolated() would write. Writes nothing.

    `selection` maps recoverable unit names to "recover", "skip" or
    "recover_keep_destination" (recover, but leave install files that differ
    from the archive alone and disclose them). A plan is "ready" only when no
    selected unit has an undecided conflict; its plan_digest is what the owner
    approves.
    """
    data_dir = os.path.abspath(os.fspath(data_dir))
    base_dir = os.path.abspath(os.fspath(base_dir))
    restore_id = restore_id or secrets.token_hex(16)
    _refuse_production(data_dir, "destination data folder")
    _refuse_production(base_dir, "install folder")
    parent_identity = _real_dir(os.path.dirname(data_dir), "DESTINATION_PARENT_UNSAFE", "parent folder")
    base_identity = _real_dir(base_dir, "INSTALL_FOLDER_UNSAFE", "install folder")
    blocking = _leftover_staging(data_dir)
    if blocking:
        raise RestoreRefused("INTERRUPTED_RESTORE_PENDING",
                             f"run recover_interrupted_restore() first; blocking: {', '.join(blocking)} "
                             "(one it cannot recognize needs quarantine_unrecognized=True)")
    destination = _destination_state(data_dir)

    intake = ri.intake_archive(archive_path)
    if intake["kind"] == ri.REJECTED:
        raise RestoreRefused("ARCHIVE_REJECTED", intake["refusal"]["code"])
    if intake["coverage"].get("unsupported_required_features"):
        raise RestoreRefused("UNSUPPORTED_REQUIRED_FEATURES",
                             ", ".join(intake["coverage"]["unsupported_required_features"]))
    preview = ri.preview_recovery(intake, data_dir, base_dir)
    if preview["status"] != "preview":
        raise RestoreRefused("ARCHIVE_REJECTED", preview["refusal"]["code"])
    choices = _resolve_selection(intake, preview["units"], selection)

    raw = _capture_archive(archive_path, intake["archive_sha256"])
    planned, required, conflicts_open = [], 0, False
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        for unit in preview["units"]:
            choice = choices.get(unit["unit"])
            entry = {"unit": unit["unit"], "disposition": unit["disposition"],
                     "availability": unit["availability"], "choice": choice or "not_recoverable",
                     "files": [], "conflicts": [], "skipped": unit["skipped"]}
            for key in ("security_fields_dropped", "unclassified_keys_omitted", "telegram_remote_access",
                        "binding", "integrity"):
                if key in unit:
                    entry[key] = unit[key]
            if choice in ("recover", "recover_keep_destination"):
                for path in unit["recoverable"]:
                    root, rel, transform = _placement(path, restore_id)
                    if root is None:
                        entry["files"].append({"archive_path": path, "action": "recorded_not_restored"})
                        continue
                    data = _member_bytes(z, path)
                    item = {"archive_path": path, "root": root, "rel": rel, "transform": transform,
                            "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                    if root == "install":
                        state, st = _lstat_chain(base_dir, rel)
                        if state == "unsafe":
                            raise RestoreRefused("INSTALL_FOLDER_UNSAFE", rel)
                        if state == "absent":
                            item["action"] = "create"
                        elif _sha256_file(os.path.join(base_dir, rel)) == item["sha256"]:
                            item["action"] = "already_present_identical"
                            item["observed"] = [st.st_dev, st.st_ino, st.st_size]
                        else:
                            item["action"] = "keep_destination"
                            item["observed"] = [st.st_dev, st.st_ino, st.st_size]
                            entry["conflicts"].append(rel)
                    else:
                        item["action"] = "create"
                    if item["action"] == "create":
                        required += len(data) * (3 if transform == "primary_database" else 1)
                    entry["files"].append(item)
                if entry["conflicts"] and choice == "recover":
                    conflicts_open = True
            planned.append(entry)

    install_files = sum(1 for u in planned for f in u["files"] if f.get("root") == "install" and f.get("action") == "create")
    if install_files > MAX_INSTALL_FILES:
        raise RestoreRefused("INSTALL_FILE_LIMIT", f"{install_files} install files; this engine restores at most {MAX_INSTALL_FILES}")
    plan = {
        "schema": PLAN_SCHEMA, "version": VERSION, "restore_id": restore_id,
        "status": "needs_owner_conflict_choice" if conflicts_open else "ready",
        "archive": {k: intake[k] for k in ("archive_sha256", "archive_size", "kind", "format",
                                            "verification", "intake_digest")},
        "restore_class": _restore_class(intake["kind"]),
        "verified_agent_backup": dict(VERIFIED_AGENT_BACKUP),
        "coverage": intake["coverage"],
        "destination": {"data_dir": data_dir, "parent_identity": parent_identity, **destination,
                        "install_dir": base_dir, "install_identity": base_identity},
        "selection": choices,
        "units": planned,
        "units_not_in_archive": preview.get("units_not_in_archive", []),
        "preview_digest": preview["plan_digest"],
        "required_bytes": required + SPACE_MARGIN_BYTES,
        "safety": {"c1": "restore_trace_then_owner_local_recover",
                   "archived_c1_state": "never_restored",
                   "historical_inactive_location": f"{HISTORY_DIRNAME}/{restore_id}",
                   "always_excluded": list(ri._ALWAYS_EXCLUDED),
                   "telegram_remote_access": "forced_off",
                   "official_skill_trust": "destination_bootstrap_only"},
    }
    plan["plan_digest"] = _digest(plan)
    return plan


# --------------------------------------------------------------------------
# Staging transforms
# --------------------------------------------------------------------------

def _prefs_bytes(raw, base_dir, personas_after):
    try:
        clean, omitted, unknown = abx._scrub_preferences(raw)
    except ab.AgentBackupError as exc:
        raise RestoreRefused("MEMBER_INVALID", str(exc)) from None
    prefs = json.loads(clean)
    note = {"security_fields_dropped": omitted - unknown, "unclassified_keys_omitted": unknown}
    persona = prefs.get("last_persona")
    if type(persona) is str and persona:
        name = persona.replace("\\", "/").rsplit("/", 1)[-1]
        if name in personas_after:
            prefs["last_persona"] = os.path.join(base_dir, "personas", name)
            note["last_persona"] = "rebound"
        else:
            del prefs["last_persona"]
            note["last_persona"] = "dropped_not_in_destination"
    prefs["telegram_remote_access"] = False
    return json.dumps(prefs, sort_keys=True, indent=2).encode("utf-8"), note


def _marker_bytes(raw, transform):
    try:
        if transform == "marker_v2":
            wrapper = ab._strict_json_loads(raw)
            if type(wrapper) is not dict or wrapper.get("source_status") != "present":
                raise RestoreRefused("MEMBER_INVALID", "installation history is not present")
            records = abx._marker_records(abx._json_bytes(wrapper["records"]))
        else:
            records = abx._marker_records(raw)
    except ab.AgentBackupError as exc:
        raise RestoreRefused("MEMBER_INVALID", str(exc)) from None
    return json.dumps(records, sort_keys=True, indent=2).encode("utf-8")


def _sqlite_uri(path, mode):
    return "file:" + urllib.parse.quote(path) + f"?mode={mode}"


def _integrity(path):
    conn = sqlite3.connect(_sqlite_uri(path, "ro"), uri=True)
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows] == ["ok"]


def _norm_sql(sql):
    # SQLite's tokenizer treats only these as whitespace; str.split() would
    # also collapse NBSP and friends, which SQLite reads as identifier bytes.
    return re.sub(r"[ \t\n\f\r]+", " ", str(sql or "")).strip()


# The only executable schema a restored database keeps: the FTS sync triggers
# the shipped core.skills / core.chat_history create, compared by exact
# (whitespace-normalized) stored text. SQLite stores the CREATE text minus
# "IF NOT EXISTS", so a same-named archived trigger with another body would
# survive the runtime's own CREATE ... IF NOT EXISTS; it is dropped here.
# tests/test_agent_backup_restore_02.py pins these to the shipped definitions.
_SHIPPED_TRIGGERS = {name: _norm_sql(sql) for name, sql in {
    "skills_ai": "CREATE TRIGGER skills_ai AFTER INSERT ON skills BEGIN "
                 "INSERT INTO skills_fts(rowid, name, description) VALUES (new.id, new.name, new.description); END",
    "skills_au": "CREATE TRIGGER skills_au AFTER UPDATE ON skills BEGIN "
                 "INSERT INTO skills_fts(skills_fts, rowid, name, description) "
                 "VALUES ('delete', old.id, old.name, old.description); "
                 "INSERT INTO skills_fts(rowid, name, description) VALUES (new.id, new.name, new.description); END",
    "skills_ad": "CREATE TRIGGER skills_ad AFTER DELETE ON skills BEGIN "
                 "INSERT INTO skills_fts(skills_fts, rowid, name, description) "
                 "VALUES ('delete', old.id, old.name, old.description); END",
    "chat_messages_ai": "CREATE TRIGGER chat_messages_ai AFTER INSERT ON chat_messages BEGIN "
                        "INSERT INTO chat_messages_fts(rowid, role, content) "
                        "VALUES (new.id, new.role, new.content); END",
    "chat_messages_ad": "CREATE TRIGGER chat_messages_ad AFTER DELETE ON chat_messages BEGIN "
                        "INSERT INTO chat_messages_fts(chat_messages_fts, rowid, role, content) "
                        "VALUES ('delete', old.id, old.role, old.content); END",
}.items()}


def _untrusted_schema(conn, primary):
    """Archived triggers and views that are not shipped schema. A trigger is
    code: one kept from an archive would run on every later write."""
    allowed = _SHIPPED_TRIGGERS if primary else {}
    return sorted(name for kind, name, sql in conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE type IN ('trigger', 'view')")
        if kind == "view" or allowed.get(name) != _norm_sql(sql))


# The skills table's stored CREATE text, exactly as every released core.skills
# leaves it (whitespace-normalized): created with six columns, then origin and
# content_sha256 added by ALTER TABLE across versions, or created with
# content_sha256 inline and origin added (current fresh installs). Derived by
# replaying each version's init, fresh and as an upgrade chain; there are no
# others. A constraint, index or collation the shipped table never had (one
# that could make the runtime's own official-Skill INSERT fail at every start,
# for example) is refused by this one rule.
_SKILLS_BASE = ("CREATE TABLE skills ( id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, "
                "description TEXT NOT NULL, path TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL")
_SHIPPED_SKILLS_TABLE = frozenset({
    _SKILLS_BASE + " )",
    _SKILLS_BASE + " , origin TEXT NOT NULL DEFAULT 'user')",
    _SKILLS_BASE + " , origin TEXT NOT NULL DEFAULT 'user', content_sha256 TEXT)",
    _SKILLS_BASE + ", content_sha256 TEXT , origin TEXT NOT NULL DEFAULT 'user')",
})


def _skills_shape(conn):
    """None when there is no skills table, else whether it has the origin
    column. Anything but a shipped definition with only its shipped index is
    refused before any authority decision. The exact text fixes every column
    name, type, key, default and collation, so no lookalike column (another
    spelling, a generated one) and no constraint the runtime never wrote can
    get past it."""
    tables = conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND lower(name)='skills'"
                          ).fetchall()
    if not tables:
        return None
    (name, sql), = tables
    if name != "skills" or _norm_sql(sql) not in _SHIPPED_SKILLS_TABLE:
        raise RestoreRefused("SQLITE_INVALID", "unsupported schema: skills is not a shipped table definition")
    if [(idx, unique, origin) for _seq, idx, unique, origin, _partial in conn.execute("PRAGMA index_list(skills)")] \
            != [("sqlite_autoindex_skills_1", 1, "u")]:
        raise RestoreRefused("SQLITE_INVALID", "unsupported schema: skills indexes are not the shipped one")
    return "origin" in {r[1] for r in conn.execute("PRAGMA table_info(skills)")}


def _claims_official(origin):
    return isinstance(origin, str) and origin.strip().casefold() == "official"


def _official_rows(conn):
    """Rows still claiming official, read the way the runtime reads them
    (its own comparison, deliberately not the fence's predicate)."""
    if not _skills_shape(conn):
        return []
    return [name for name, origin in conn.execute("SELECT name, origin FROM skills")
            if "official" in str(origin or "").casefold()]


# The search tables the shipped triggers and first start write to, by exact
# (whitespace-normalized) stored text. Their creators never changed this DDL;
# absent is fine (first start creates them).
_SHIPPED_FTS = {name: _norm_sql(sql) for name, sql in {
    "skills_fts": "CREATE VIRTUAL TABLE skills_fts USING fts5(name, description, content='skills', content_rowid='id')",
    "chat_messages_fts": "CREATE VIRTUAL TABLE chat_messages_fts USING fts5(role, content, "
                         "content='chat_messages', content_rowid='id')",
}.items()}


def _fts_shape(conn):
    for name, sql in conn.execute("SELECT name, sql FROM sqlite_master "
                                  "WHERE lower(name) IN ('skills_fts', 'chat_messages_fts')"):
        if _SHIPPED_FTS.get(name) != _norm_sql(sql):
            raise RestoreRefused("SQLITE_INVALID", f"unsupported schema: {name} is not the shipped search table")


def _startup_replay(conn):
    """Replay, then roll back, what first start and ordinary use do to this
    database: create missing search tables and triggers, write, update and
    delete a Skill and a chat message, check the Skills search index. It also
    proves Skill names are unique under BINARY comparison, as the official
    bootstrap assumes (a NOCASE column or index would make the bootstrap's own
    INSERT fail at first start), and that every row the runtime writes keeps
    its unset provenance columns NULL. A shape that passes every static check
    but that the runtime could not survive, or that would stamp provenance on
    its writes, is refused here. Only ever run on a staged or in-memory copy."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    has_origin = _skills_shape(conn)
    conn.execute("SAVEPOINT restore_startup_replay")
    try:
        if has_origin is not None:
            _replay_tables(conn, "skills_fts", ("skills_ai", "skills_au", "skills_ad"))
            # First start migrates an older table before anything writes to it.
            columns = {r[1] for r in conn.execute("PRAGMA table_info(skills)")}
            if "origin" not in columns:
                conn.execute("ALTER TABLE skills ADD COLUMN origin TEXT NOT NULL DEFAULT 'user'")
            if "content_sha256" not in columns:
                conn.execute("ALTER TABLE skills ADD COLUMN content_sha256 TEXT")
            # The official bootstrap's own INSERT, shape for shape.
            conn.execute("INSERT INTO skills (name, description, path, created_at, updated_at, origin, content_sha256) "
                         "VALUES (?, 'probe', '/probe', 't', 't', 'official', ?)",
                         (f"restore-probe-official-{secrets.token_hex(4)}", "0" * 64))
            probe = f"restore-probe-{secrets.token_hex(4)}"
            # One name per difference a built-in collation can ignore (NOCASE:
            # case; RTRIM: trailing spaces): each must be its own row, found
            # only by itself.
            names = (probe, probe.upper(), probe + " ")
            cols = "name, description, path, created_at, updated_at, origin"
            vals = "?, 'probe', '/probe', 't', 't', 'user'"
            for name in names:
                try:
                    conn.execute(f"INSERT INTO skills ({cols}) VALUES ({vals})", (name,))
                except sqlite3.IntegrityError:
                    raise RestoreRefused("SQLITE_INVALID", "unsupported schema: skills names not unique under BINARY") from None
            if any(conn.execute("SELECT count(*) FROM skills WHERE name=?", (n,)).fetchone()[0] != 1 for n in names):
                raise RestoreRefused("SQLITE_INVALID", "unsupported schema: skills names not compared as BINARY")
            conn.execute("UPDATE skills SET description='probe again' WHERE name=?", (probe,))
            conn.execute(f"DELETE FROM skills WHERE name IN ({', '.join('?' * len(names))})", names)
            try:
                conn.execute("INSERT INTO skills_fts(skills_fts, rank) VALUES ('integrity-check', 1)")
            except sqlite3.DatabaseError:
                conn.execute("INSERT INTO skills_fts(skills_fts) VALUES ('rebuild')")
        if "chat_messages" in tables:
            _replay_tables(conn, "chat_messages_fts", ("chat_messages_ai", "chat_messages_ad"))
            rid = conn.execute("INSERT INTO chat_messages (chat_id, role, content, created_at) "
                               "VALUES (0, 'user', 'probe', 't')").lastrowid
            conn.execute("DELETE FROM chat_messages WHERE id=?", (rid,))
            conn.execute("INSERT INTO chat_messages_fts(rowid, role, content) SELECT m.id, m.role, m.content "
                         "FROM chat_messages m WHERE m.id NOT IN (SELECT rowid FROM chat_messages_fts)")
        _replay_runtime_writes(conn, tables)
    except sqlite3.Error as exc:
        raise RestoreRefused("SQLITE_INVALID", f"restored database fails the first-start replay: {exc}") from None
    finally:
        conn.execute("ROLLBACK TO restore_startup_replay")
        conn.execute("RELEASE restore_startup_replay")


# Every row the runtime writes into these tables, by its exact column list
# (tools/palace.py, tools/memory.py). A column it leaves unset takes the
# table's DEFAULT, and in every shipped definition that is NULL -- including
# provenance: palace_drawers.origin is PALACE-GUARD-01B-2's trusted synthesis
# stamp (a manual-compaction checkpoint makes the runtime omit history), and
# source_memory_id / admission / withheld_reason are provenance too. An
# archived default could stamp them onto model-written rows, so each write is
# replayed and every unset column must read back NULL.
_RUNTIME_WRITES = {
    "palace_wings": ("name",),
    "palace_rooms": ("wing_id", "name"),
    "palace_drawers": ("room_id", "content", "tags", "untrusted", "created_at"),
    "palace_closets": ("room_id", "layer", "compressed", "token_est", "created_at", "updated_at",
                       "ever_had_untrusted_merge", "admission"),
    "palace_halls": ("hall", "compressed", "layer", "created_at", "untrusted"),
    "memories": ("label", "content", "created_at", "untrusted"),
    "chats": ("name", "created_at", "updated_at"),
    "chat_messages": ("chat_id", "role", "content", "metadata", "created_at"),
}
_PROBE_VALUES = {"wing_id": 1, "room_id": 1, "chat_id": 0, "layer": 2, "token_est": 0, "untrusted": 1,
                 "ever_had_untrusted_merge": 0, "admission": None, "metadata": None, "role": "user", "tags": "[]"}


def _generated_columns(conn):
    """Generated columns in any table: never shipped, and one would hand every
    row a value no writer chose (hidden 2/3; 1 is a virtual table's own)."""
    return sorted(f"{table}.{col}" for (table,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")
        for _cid, col, _t, _nn, _d, _pk, hidden in conn.execute(
            f'PRAGMA table_xinfo("{table.replace(chr(34), chr(34) * 2)}")') if hidden in (2, 3))


# A column default may only be a literal: NULL, a number or a quoted string,
# as in every shipped table. An expression (a clock, random(), CURRENT_*) is
# evaluated afresh on every insert, so the one-time write rehearsal could see
# NULL at restore and a forged provenance stamp later.
_LITERAL_DEFAULT = re.compile(r"(NULL|[-+]?[0-9]+(\.[0-9]+)?|'(?:[^']|'')*')", re.IGNORECASE)


# Every foreign key the shipped runtime declares, with its referential actions
# (PRAGMA foreign_key_list: table, from, to-table, to, on update, on delete).
# Each has had exactly this form in every released version. The runtime runs
# with foreign keys on, so any other key -- ON DELETE SET DEFAULT above all --
# is an action the archive would run on the runtime's own deletes, after
# acceptance: rewriting a Palace layer or a trust flag the runtime wrote.
_SHIPPED_FOREIGN_KEYS = frozenset({
    ("chat_messages", "chat_id", "chats", "id", "NO ACTION", "CASCADE"),
    ("chatgpt_plan_chat_bindings", "chat_id", "chats", "id", "NO ACTION", "CASCADE"),
    ("context_checkpoints", "chat_id", "chats", "id", "NO ACTION", "CASCADE"),
    ("generation_artifacts", "lumina_job_id", "generation_jobs", "lumina_job_id", "NO ACTION", "NO ACTION"),
    ("palace_closets", "room_id", "palace_rooms", "id", "NO ACTION", "CASCADE"),
    ("palace_drawers", "closet_id", "palace_closets", "id", "NO ACTION", "SET NULL"),
    ("palace_drawers", "room_id", "palace_rooms", "id", "NO ACTION", "CASCADE"),
    ("palace_drawers", "source_memory_id", "memories", "id", "NO ACTION", "NO ACTION"),
    ("palace_halls", "source_memory_id", "memories", "id", "NO ACTION", "NO ACTION"),
    ("palace_rooms", "wing_id", "palace_wings", "id", "NO ACTION", "CASCADE"),
})


def _unshipped_foreign_keys(conn):
    found = []
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        keys = conn.execute(f'PRAGMA foreign_key_list("{table.replace(chr(34), chr(34) * 2)}")').fetchall()
        for _id, seq, to_table, frm, to, on_update, on_delete, match in keys:
            if seq != 0 or match != "NONE" or (table, frm, to_table, to, on_update, on_delete) not in _SHIPPED_FOREIGN_KEYS:
                found.append(f"{table}.{frm}")
    return sorted(found)


def _non_literal_defaults(conn):
    return sorted(f"{table}.{col}" for (table,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")
        for _cid, col, _t, _nn, dflt, _pk, _hidden in conn.execute(
            f'PRAGMA table_xinfo("{table.replace(chr(34), chr(34) * 2)}")')
        if dflt is not None and not _LITERAL_DEFAULT.fullmatch(str(dflt)))


def _replay_runtime_writes(conn, tables):
    for table, written in _RUNTIME_WRITES.items():
        if table not in tables:
            continue                      # first start creates it with the shipped definition
        columns = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        cols = [c for c in written if c in columns]
        rowid = conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                             [_PROBE_VALUES.get(c, "restore-probe") for c in cols]).lastrowid
        row = conn.execute(f"SELECT * FROM {table} WHERE rowid=?", (rowid,)).fetchone()
        stamped = [c for c, v in zip(columns, row) if c not in written and c != "id" and v is not None]
        if stamped:
            raise RestoreRefused("SQLITE_INVALID",
                                 f"unsupported schema: archived default sets {table}.{stamped[0]} on runtime writes")


def _replay_tables(conn, fts, triggers):
    """What first start runs: CREATE ... IF NOT EXISTS of the shipped search
    table and its triggers."""
    conn.execute(_SHIPPED_FTS[fts].replace("CREATE VIRTUAL TABLE ", "CREATE VIRTUAL TABLE IF NOT EXISTS ", 1))
    for name in triggers:
        conn.execute(_SHIPPED_TRIGGERS[name].replace("CREATE TRIGGER ", "CREATE TRIGGER IF NOT EXISTS ", 1))


def _ensure_search_tables(conn):
    """Create a missing shipped search table, and index what its content table
    already holds, as first start would, before the fence writes rows whose
    shipped triggers write to it."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    try:
        if "skills" in tables and "skills_fts" not in tables:
            conn.execute(_SHIPPED_FTS["skills_fts"])
            conn.execute("INSERT INTO skills_fts(skills_fts) VALUES ('rebuild')")
        if "chat_messages" in tables and "chat_messages_fts" not in tables:
            conn.execute(_SHIPPED_FTS["chat_messages_fts"])
            conn.execute("INSERT INTO chat_messages_fts(chat_messages_fts) VALUES ('rebuild')")
    except sqlite3.Error as exc:
        raise RestoreRefused("SQLITE_INVALID", f"restored database fails the first-start replay: {exc}") from None


def _free_skill_name(conn, name, tracked):
    for i in itertools.count(1):
        candidate = f"{name} (restored)" if i == 1 else f"{name} (restored {i})"
        if candidate not in tracked and not conn.execute("SELECT 1 FROM skills WHERE name=?", (candidate,)).fetchone():
            return candidate


def _tracked_official_names(base_dir):
    """Names of the OFFICIAL Skill packages the destination checkout ships
    (skill_packages/official/<entry>/manifest.json). The runtime bootstrap
    installs these at first start; their names decide only whether an
    archived row is set aside for that bootstrap, never any trust."""
    names, root = set(), os.path.join(base_dir, "skill_packages", "official")
    if _dir_chain(base_dir, "skill_packages/official"):
        for entry in sorted(os.listdir(root)):
            state, st = _lstat_chain(base_dir, f"skill_packages/official/{entry}/manifest.json")
            if state != "file" or st.st_size > 64 * 1024:
                continue
            try:
                with open(os.path.join(root, entry, "manifest.json"), "rb") as fh:
                    manifest = ab._strict_json_loads(fh.read(64 * 1024))
            except (OSError, ValueError, UnicodeError):
                continue
            if type(manifest) is dict and type(manifest.get("name")) is str:
                names.add(manifest["name"])
    return names


def _finalize_sqlite(path, base_dir, tracked, primary):
    """Rollback-journal mode (no sidecars left behind), archived executable
    schema dropped, the skills table held to the shipped shape, Skills rebind
    and authority fence on the primary database, the invariant re-read from
    the result, then a full integrity check. Only staged copies are ever
    opened read-write.

    A restore never leaves a Skill official: official status comes only from
    the destination checkout's own bootstrap of its tracked packages. An
    archived official row named like one of those packages is set aside for
    that bootstrap (which would otherwise refuse the name collision); every
    other one becomes a user Skill, and a user Skill whose name is exactly a
    shipped package's is renamed so that bootstrap can install every package.
    Archive hashes and paths vouch for nothing. The search tables are held to
    the shipped definitions and first start is replayed (and rolled back)
    before the database is accepted."""
    note = {}
    conn = sqlite3.connect(_sqlite_uri(path, "rw"), uri=True, isolation_level=None)
    try:
        conn.execute("PRAGMA trusted_schema=OFF")
        # Outside any transaction (inside one it is a no-op): the replay's probe
        # rows reference no real chat, whatever this build's default is.
        conn.execute("PRAGMA foreign_keys=OFF")
        if conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0].lower() != "delete":
            raise RestoreRefused("SQLITE_INVALID", "could not leave WAL mode")
        conn.execute("BEGIN IMMEDIATE")
        generated = _generated_columns(conn)
        if generated:
            raise RestoreRefused("SQLITE_INVALID", f"unsupported schema: generated column {generated[0]}")
        computed = _non_literal_defaults(conn)
        if computed:
            raise RestoreRefused("SQLITE_INVALID", f"unsupported schema: non-literal default on {computed[0]}")
        foreign = _unshipped_foreign_keys(conn)
        if foreign:
            raise RestoreRefused("SQLITE_INVALID", f"unsupported schema: foreign key not shipped on {foreign[0]}")
        # Before any write: an archived trigger would otherwise run inside the
        # authority scrub itself (e.g. re-promote a Skill the scrub demotes).
        dropped = _untrusted_schema(conn, primary)
        kinds = dict(conn.execute("SELECT name, type FROM sqlite_master WHERE type IN ('trigger', 'view')"))
        for name in dropped:
            # IF EXISTS: dropping a view also drops its INSTEAD OF triggers.
            conn.execute(f'DROP {kinds[name].upper()} IF EXISTS "{name.replace(chr(34), chr(34) * 2)}"')
        note["untrusted_schema_dropped"] = len(dropped)
        if primary:
            has_origin = _skills_shape(conn)
            _fts_shape(conn)
            _ensure_search_tables(conn)
            rebound = demoted = 0
            deferred, conflicts = [], []
            if has_origin is not None:
                query = "SELECT id, name, path, origin FROM skills" if has_origin \
                    else "SELECT id, name, path, NULL FROM skills"
                for rid, name, old, origin in conn.execute(query).fetchall():
                    if _claims_official(origin) and name in tracked:
                        conn.execute("DELETE FROM skills WHERE id=?", (rid,))
                        deferred.append(name)
                        continue
                    leaf = str(old or "").replace("\\", "/").rsplit("/", 1)[-1]
                    conn.execute("UPDATE skills SET path=? WHERE id=?", (os.path.join(base_dir, "skills", leaf), rid))
                    rebound += 1
                    if _claims_official(origin):
                        conn.execute("UPDATE skills SET origin='user' WHERE id=?", (rid,))
                        demoted += 1
                    elif name in tracked:
                        # The bootstrap refuses an existing row with a shipped
                        # package's name, and stops there: that package and
                        # every later one would never install. Keep the user's
                        # Skill under a disclosed new name instead.
                        new = _free_skill_name(conn, name, tracked)
                        conn.execute("UPDATE skills SET name=? WHERE id=?", (new, rid))
                        conflicts.append({"from": name, "to": new})
            note.update(skills_rows_rebound=rebound, official_skills_demoted=demoted,
                        official_skills_deferred_to_bootstrap=sorted(deferred),
                        user_skills_named_like_tracked_official=sorted(c["from"] for c in conflicts),
                        skills_renamed_for_bootstrap=conflicts)
            # Measured, not assumed: the result itself must hold the invariant.
            if _official_rows(conn) or _untrusted_schema(conn, primary):
                raise RestoreRefused("SKILL_AUTHORITY_UNVERIFIED", "a Skill still claims official after the fence")
            _startup_replay(conn)
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        raise RestoreRefused("SQLITE_INVALID", type(exc).__name__) from None
    finally:
        conn.close()
    for suffix in ("-wal", "-shm", "-journal"):
        if os.path.lexists(path + suffix):
            raise RestoreRefused("SQLITE_INVALID", "sidecar left behind")
    try:
        ok = _integrity(path)
    except sqlite3.Error as exc:
        raise RestoreRefused("SQLITE_INVALID", type(exc).__name__) from None
    if not ok:
        raise RestoreRefused("SQLITE_INVALID", "integrity_check failed")
    note["integrity_check"] = "ok"
    return note


# --------------------------------------------------------------------------
# Writing, journal, rollback
# --------------------------------------------------------------------------

_CREATE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


def _fsync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_new(path, data, mode):
    fd = os.open(path, _CREATE, mode)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_staged(staging, rel, data):
    path = os.path.join(staging, rel)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    _write_new(path, data, 0o600)
    return path


def _write_journal(staging, restore_id, journal):
    path = os.path.join(staging, HISTORY_DIRNAME, restore_id, JOURNAL_NAME)
    # Each level private (makedirs would apply the mode to the last one only):
    # recovery trusts only folders no one else can write.
    for folder in (os.path.join(staging, HISTORY_DIRNAME), os.path.dirname(path)):
        try:
            os.mkdir(folder, 0o700)
        except FileExistsError:
            pass
    tmp = f"{path}.{secrets.token_hex(4)}.tmp"
    _write_new(tmp, abx._json_bytes(journal), 0o600)
    os.replace(tmp, path)
    _fsync_dir(os.path.dirname(path))


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _open_private_dir(name, dir_fd=None):
    """fd of a real directory (never a link) owned by this user and writable
    by no one else -- what this engine itself creates -- else None."""
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
    except OSError:
        return None
    st = os.fstat(fd)
    if st.st_uid != os.geteuid() or st.st_mode & 0o022:
        os.close(fd)
        return None
    return fd


def _staging_restore_id(staging):
    restore_id = os.path.basename(staging).rsplit(".restore-", 1)[-1][:-len(".staging")]
    return restore_id if _is_hex(restore_id, 32) else None


def _read_journal(staging):
    """The journal of a leftover staging directory, or None if it isn't ours.
    Read through directory descriptors: neither a linked staging folder nor a
    link inside it is ever followed, and only private folders count."""
    restore_id = _staging_restore_id(staging)
    if restore_id is None:
        return None
    fds = []
    try:
        for name in (staging, HISTORY_DIRNAME, restore_id):
            fd = _open_private_dir(name, dir_fd=fds[-1] if fds else None)
            if fd is None:
                return None
            fds.append(fd)
        jfd = os.open(JOURNAL_NAME, _FILE_FLAGS, dir_fd=fds[-1])
        with os.fdopen(jfd, "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                return None
            journal = ab._strict_json_loads(fh.read(JOURNAL_READ_LIMIT))
    except (OSError, ValueError, UnicodeError):
        return None
    finally:
        for fd in fds:
            os.close(fd)
    if type(journal) is not dict or journal.get("schema") != JOURNAL_SCHEMA \
            or journal.get("restore_id") != restore_id:
        return None
    return journal


def _never_journaled(staging):
    """A private staging folder killed before its first journal write holds at
    most journal temp files. Install files are journaled before they are
    created, so such a restore wrote nothing anywhere else."""
    restore_id = _staging_restore_id(staging)
    fd = _open_private_dir(staging) if restore_id else None
    if fd is None:
        return False
    os.close(fd)
    history = f"{HISTORY_DIRNAME}/{restore_id}"

    def unreadable(exc):
        raise exc

    try:
        walked = list(os.walk(staging, onerror=unreadable))
    except OSError:
        return False
    for root, dirs, files in walked:
        rel = os.path.relpath(root, staging).replace(os.sep, "/")
        if rel not in (".", HISTORY_DIRNAME, history):
            return False
        for name in dirs + files:
            if os.path.islink(os.path.join(root, name)):
                return False
        if files and (rel != history or not all(re.fullmatch(r"journal\.json\.[0-9a-f]{8}\.tmp", f) for f in files)):
            return False
    return True


def _make_install_dirs(base_dir, rel, journal, record):
    """Create the missing folders on the way to rel, each journaled before it
    exists: a recorded folder that was never made is harmless to recovery, an
    unrecorded one would be litter it cannot prove it made."""
    missing, path = [], base_dir
    for part in rel.split("/")[:-1]:
        path = os.path.join(path, part)
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            missing.append(path)
            continue
        if not stat.S_ISDIR(st.st_mode):
            raise RestoreRefused("INSTALL_TARGET_CHANGED", rel)
    if missing:
        journal["install_dirs_created"].extend(missing)
        record()
        _checkpoint("install_dirs_journaled")
        for i, path in enumerate(missing):
            try:
                os.mkdir(path, 0o755)
            except FileExistsError:
                # Someone else made it (and so everything after it is not
                # ours either): unrecord before refusing, so rollback leaves it.
                del journal["install_dirs_created"][-(len(missing) - i):]
                record()
                raise RestoreRefused("INSTALL_TARGET_CHANGED", rel) from None


def _install_tmp(target, restore_id):
    return os.path.join(os.path.dirname(target), f".{os.path.basename(target)}.{restore_id}.restore-tmp")


# Transaction-bound ownership: every install file this restore creates carries
# its restore id in this attribute from the moment its temp file exists. An
# inode number, bytes or timestamps alone can be reproduced by another writer
# (or by inode reuse); the mark is written only by this restore, before the
# journal records the file. It stays on installed files as provenance.
_OWNER_XATTR = "user.lumina.restore_id"


def _open_install_tmp(tmp, restore_id):
    """Create and mark the private temp file; returns (fd, identity)."""
    try:
        fd = os.open(tmp, _CREATE, 0o644)
    except FileExistsError:
        raise RestoreRefused("INSTALL_TARGET_CHANGED", tmp) from None
    try:
        os.setxattr(fd, _OWNER_XATTR, restore_id.encode())
    except OSError:
        if os.stat(tmp, follow_symlinks=False).st_ino == os.fstat(fd).st_ino:
            os.unlink(tmp)
        os.close(fd)
        raise RestoreRefused("INSTALL_FOLDER_UNSAFE", "install folder cannot carry restore ownership marks") from None
    st = os.fstat(fd)
    return fd, [st.st_dev, st.st_ino]


def _fill_install_tmp(fd, data):
    """Write and fsync the temp file; returns its seal [size, mtime_ns]."""
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
        st = os.fstat(fd)
    except BaseException:
        os.close(fd)
        raise
    return [st.st_size, st.st_mtime_ns]


def _link_install_file(fd, tmp, target):
    """Link the sealed temp file into place without ever replacing a file."""
    os.close(fd)
    try:
        os.link(tmp, target, follow_symlinks=False)
    except FileExistsError:
        raise RestoreRefused("INSTALL_TARGET_CHANGED", target) from None
    finally:
        os.unlink(tmp)


def _is_hex(value, n):
    return type(value) is str and len(value) == n and all(c in "0123456789abcdef" for c in value)


def _install_rel(install_dir, path):
    """path relative to install_dir if it is strictly inside it, else None."""
    if type(path) is not str or not os.path.isabs(path) or os.path.normpath(path) != path \
            or path == install_dir or not _within(path, install_dir):
        return None
    return os.path.relpath(path, install_dir).replace(os.sep, "/")


def _install_namespace(rel, *, directory=False):
    """True only for install-folder paths _placement() can produce (or, with
    directory=True, folders on the way to them). Recovery never deletes
    anything else, whatever a journal says."""
    parts = rel.split("/") if rel else []
    if not parts or any(p in ("", ".", "..") for p in parts):
        return False
    for prefix in set(_INSTALL_DIRS.values()):
        pp = prefix.split("/")
        if parts[:len(pp)] == pp and (len(parts) > len(pp) or directory):
            return True
        if directory and pp[:len(parts)] == parts:
            return True
    if parts[0] == "projects":
        if directory:
            return len(parts) <= 2
        return parts == ["projects", "projectlist.md"] or (len(parts) == 3 and parts[2] in ("project.md", "codebase.md"))
    return False


def _int_pair(value):
    return type(value) is list and len(value) == 2 and all(type(x) is int for x in value)


def _recognized_journal(journal, data_dir):
    """Recovery deletes only what this engine could itself have created: a
    known state, the same real non-production install folder, targets inside
    the restore's own install namespace, the deterministic temp names. A
    schema string is not deletion authority; anything else is left alone.
    The folder is matched by inode, not device number, which a remount can
    renumber; files are proven by their ownership mark, not by this."""
    restore_id, install_dir = journal.get("restore_id"), journal.get("install_dir")
    dirs, created = journal.get("install_dirs_created"), journal.get("install_created")
    if (journal.get("version") != VERSION or journal.get("data_dir") != data_dir
            or journal.get("state") not in ("staging", "install_writing", "publishing")
            or not _is_hex(restore_id, 32) or type(install_dir) is not str
            or not os.path.isabs(install_dir) or os.path.normpath(install_dir) != install_dir
            or not _int_pair(journal.get("install_identity"))
            or type(dirs) is not list or type(created) is not list
            or (journal["state"] == "staging" and (dirs or created))):
        return False
    try:
        _refuse_production(install_dir, "install folder")
        if _real_dir(install_dir, "INSTALL_FOLDER_UNSAFE", "install folder")[1] != journal["install_identity"][1]:
            return False
    except RestoreRefused:
        return False
    for path in dirs:
        rel = _install_rel(install_dir, path)
        if rel is None or not _install_namespace(rel, directory=True):
            return False
    for item in created:
        if type(item) is not dict or set(item) != {"path", "tmp", "sha256", "identity", "seal"}:
            return False
        rel = _install_rel(install_dir, item["path"])
        ident, seal = item["identity"], item["seal"]
        if (rel is None or not _install_namespace(rel)
                or item["tmp"] != _install_tmp(item["path"], restore_id) or not _is_hex(item["sha256"], 64)
                or (ident is not None and not _int_pair(ident)) or (seal is not None and not _int_pair(seal))
                or (seal is not None and ident is None)):
            return False
    return True


def _walk_dirs(root_fd, parts):
    """(fd, "ok") for root/parts opened without following any link, else
    (None, "absent" | "unsafe"). The caller closes the fd."""
    fd = os.dup(root_fd)
    for part in parts:
        try:
            nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
        except FileNotFoundError:
            os.close(fd)
            return None, "absent"
        except OSError:
            os.close(fd)
            return None, "unsafe"
        os.close(fd)
        fd = nxt
    return fd, "ok"


def _sha256_fd(fd):
    h = hashlib.sha256()
    offset = 0
    while chunk := os.pread(fd, 1 << 16, offset):
        h.update(chunk)
        offset += len(chunk)
    return h.hexdigest()


def _owner_mark(fd):
    try:
        return os.getxattr(fd, _OWNER_XATTR)
    except OSError:
        return None


def _remove_owned(root_fd, rel, item, restore_id, is_tmp):
    """absent | removed | left (ours, changed since) | preserved (not provably
    ours). Every step is relative to directory descriptors, so a folder
    swapped for a link after a check cannot redirect the unlink."""
    *parents, leaf = rel.split("/")
    parent, state = _walk_dirs(root_fd, parents)
    if parent is None:
        return "absent" if state == "absent" else "preserved"
    try:
        try:
            fd = os.open(leaf, _FILE_FLAGS, dir_fd=parent)
        except FileNotFoundError:
            return "absent"
        except OSError:
            return "preserved"
        try:
            st = os.fstat(fd)
            ident = item["identity"]
            if not stat.S_ISREG(st.st_mode) or ident is None or st.st_ino != ident[1] \
                    or _owner_mark(fd) != restore_id.encode():
                return "preserved"
            if not is_tmp and ([st.st_size, st.st_mtime_ns] != item["seal"] or _sha256_fd(fd) != item["sha256"]):
                return "left"
            if os.stat(leaf, dir_fd=parent, follow_symlinks=False).st_ino != st.st_ino:
                return "preserved"
            os.unlink(leaf, dir_fd=parent)  # a temp of ours may be partly written; it is still ours
            return "removed"
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def _rollback_install(journal):
    """Remove install files this restore created, and only those: the inode
    the journal recorded, still carrying this restore's ownership mark (and,
    for a linked file, its sealed size, mtime and bytes). Returns (left,
    preserved): its own files it could not remove (changed since), and present
    paths it cannot prove it created, which stay untouched."""
    install_dir, restore_id = journal["install_dir"], journal["restore_id"]
    created = journal.get("install_created", [])
    left, preserved = [], []
    try:
        root = os.open(install_dir, _DIR_FLAGS)
    except OSError:
        return left, [item[k] for item in created for k in ("tmp", "path") if os.path.lexists(item[k])]
    try:
        for item in reversed(created):
            for path, is_tmp in ((item["tmp"], True), (item["path"], False)):
                verdict = _remove_owned(root, _install_rel(install_dir, path), item, restore_id, is_tmp)
                if verdict == "left":
                    left.append(path)
                elif verdict == "preserved":
                    preserved.append(path)
        for path in reversed(journal.get("install_dirs_created", [])):
            *parents, leaf = _install_rel(install_dir, path).split("/")
            parent, _state = _walk_dirs(root, parents)
            if parent is not None:
                try:
                    os.rmdir(leaf, dir_fd=parent)  # only ever an empty real directory
                except OSError:
                    pass
                finally:
                    os.close(parent)
    finally:
        os.close(root)
    return left, preserved


def _dir_chain(root, rel):
    """True if every component of root/rel is a real directory (no links)."""
    path = root
    for part in rel.split("/"):
        path = os.path.join(path, part)
        try:
            if not stat.S_ISDIR(os.lstat(path).st_mode):
                return False
        except OSError:
            return False
    return True


def _rename_noreplace(src, dst):
    libc = ctypes.CDLL(None, use_errno=True)
    fn = getattr(libc, "renameat2", None)
    if fn is None:
        raise RestoreRefused("COMMIT_PRIMITIVE_UNAVAILABLE", "renameat2")
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    at_fdcwd, rename_noreplace = -100, 1
    if fn(at_fdcwd, os.fsencode(src), at_fdcwd, os.fsencode(dst), rename_noreplace) != 0:
        err = ctypes.get_errno()
        if err in (errno.EEXIST, errno.ENOTEMPTY):
            raise RestoreRefused("DESTINATION_CHANGED", "destination appeared before publishing")
        if err in (errno.ENOSYS, errno.EINVAL):
            raise RestoreRefused("COMMIT_PRIMITIVE_UNAVAILABLE", errno.errorcode.get(err, str(err)))
        raise OSError(err, os.strerror(err), dst)


# --------------------------------------------------------------------------
# Restore
# --------------------------------------------------------------------------

def _validate_staged(staging, expected):
    """Exactly the expected files, as regular unlinked files with the
    recorded bytes; no live-authority names; no links of any kind."""
    found = {}
    for root, dirs, files in os.walk(staging):
        for name in dirs + files:
            path = os.path.join(root, name)
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode) or not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
                raise RestoreRefused("STAGED_VALIDATION_FAILED", "link or special file")
            if stat.S_ISREG(st.st_mode):
                if st.st_nlink != 1:
                    raise RestoreRefused("STAGED_VALIDATION_FAILED", "aliased file")
                found[os.path.relpath(path, staging).replace(os.sep, "/")] = path
    for rel in _FORBIDDEN_LIVE:
        if os.path.lexists(os.path.join(staging, rel)):
            raise RestoreRefused("STAGED_VALIDATION_FAILED", f"live authority name {rel}")
    for rel in found:
        parts = rel.split("/")
        if parts[0] == "projects" and parts[-1] == "binding.json":
            raise RestoreRefused("STAGED_VALIDATION_FAILED", "live project binding")
    if set(found) != set(expected):
        raise RestoreRefused("STAGED_VALIDATION_FAILED",
                             f"unexpected={sorted(set(found) - set(expected))} "
                             f"missing={sorted(set(expected) - set(found))}")
    for rel, sha in expected.items():
        if sha is not None and _sha256_file(found[rel]) != sha:
            raise RestoreRefused("STAGED_VALIDATION_FAILED", f"bytes changed: {rel}")


def restore_isolated(plan, archive_path, *, approved_plan_digest):
    """Publish a new data folder from an approved plan, or change nothing.

    The owner approves plan["plan_digest"]; anything that makes a fresh plan
    differ (archive bytes, classification, destination, install folder
    observations, selection) refuses with PLAN_CHANGED. Failures before the
    publishing rename roll back: the staging directory is removed and every
    install file this restore created is removed again. Returns the receipt.
    """
    if type(plan) is not dict or plan.get("schema") != PLAN_SCHEMA:
        raise TypeError("plan must come from plan_restore()")
    if (type(approved_plan_digest) is not str or approved_plan_digest != plan.get("plan_digest")
            or _digest({k: v for k, v in plan.items() if k != "plan_digest"}) != approved_plan_digest):
        raise RestoreRefused("PLAN_NOT_APPROVED")
    dest = plan["destination"]
    data_dir, base_dir, restore_id = dest["data_dir"], dest["install_dir"], plan["restore_id"]
    selection = {u: c for u, c in plan["selection"].items()}
    fresh = plan_restore(archive_path, data_dir, base_dir, selection=selection, restore_id=restore_id)
    if fresh["plan_digest"] != approved_plan_digest:
        raise RestoreRefused("PLAN_CHANGED")
    if plan["status"] != "ready":
        raise RestoreRefused("OWNER_CONFLICT_CHOICE_REQUIRED")
    parent = os.path.dirname(data_dir)
    if _free_bytes(parent) < plan["required_bytes"]:
        raise RestoreRefused("INSUFFICIENT_SPACE", f"need {plan['required_bytes']} bytes free in {parent}")
    install_bytes = sum(f["size"] for u in plan["units"] for f in u["files"]
                        if f.get("root") == "install" and f.get("action") == "create")
    if install_bytes and _free_bytes(base_dir) < install_bytes + SPACE_MARGIN_BYTES:
        raise RestoreRefused("INSUFFICIENT_SPACE", f"need {install_bytes} bytes free in {base_dir}")

    raw = _capture_archive(archive_path, plan["archive"]["archive_sha256"])
    install_identity = _real_dir(base_dir, "INSTALL_FOLDER_UNSAFE", "install folder")
    staging = os.path.join(parent, f"{_staging_prefix(data_dir)}{restore_id}.staging")
    os.mkdir(staging, 0o700)
    journal = {"schema": JOURNAL_SCHEMA, "version": VERSION, "restore_id": restore_id,
               "state": "staging", "data_dir": data_dir, "install_dir": base_dir,
               "install_identity": install_identity,
               "plan_digest": approved_plan_digest, "install_dirs_created": [], "install_created": []}
    try:
        _write_journal(staging, restore_id, journal)
        receipt = _stage_and_publish(plan, raw, staging, journal)
    except BaseException as exc:
        if journal["state"] == "published":
            raise
        left, preserved = _rollback_install(journal)
        shutil.rmtree(staging, ignore_errors=True)
        if dest["state"] == "absent":
            untouched = not os.path.lexists(data_dir)
        else:
            untouched = os.path.isdir(data_dir) and not os.path.islink(data_dir) and not os.listdir(data_dir)
        rollback = {"staging_removed": not os.path.lexists(staging), "install_files_left": left,
                    "install_paths_preserved": preserved, "destination_untouched": untouched}
        if isinstance(exc, RestoreRefused):
            raise RestoreRefused(exc.code, exc.detail, rollback) from None
        if isinstance(exc, OSError):
            raise RestoreRefused("WRITE_FAILED", f"{type(exc).__name__}: {exc.strerror}", rollback) from None
        raise
    return receipt


def _stage_and_publish(plan, raw, staging, journal):
    dest = plan["destination"]
    data_dir, base_dir, restore_id = dest["data_dir"], dest["install_dir"], plan["restore_id"]
    files = [f for u in plan["units"] for f in u["files"] if "root" in f]
    personas_after = {f["rel"].rsplit("/", 1)[-1] for f in files
                      if f["root"] == "install" and f["rel"].startswith("personas/")
                      and f["action"] in ("create", "already_present_identical")}
    personas_dir = os.path.join(base_dir, "personas")
    if os.path.isdir(personas_dir) and not os.path.islink(personas_dir):
        personas_after |= {n for n in os.listdir(personas_dir)
                           if _lstat_chain(base_dir, f"personas/{n}")[0] == "file"}
    tracked = _tracked_official_names(base_dir)

    expected, written, notes, sqlite_checks = {}, [], {}, {}
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        payload = {f["archive_path"]: _member_bytes(z, f["archive_path"]) for f in files}
    for f in files:
        data = payload[f["archive_path"]]
        if len(data) != f["size"] or hashlib.sha256(data).hexdigest() != f["sha256"]:
            raise RestoreRefused("MEMBER_INVALID", f["archive_path"])
    _checkpoint("members_verified")

    for f in files:
        if f["root"] != "data":
            continue
        data, transform = payload[f["archive_path"]], f["transform"]
        if transform == "preferences":
            data, notes["preferences"] = _prefs_bytes(data, base_dir, personas_after)
        elif transform in ("marker_v2", "marker_legacy"):
            data = _marker_bytes(data, transform)
        path = _write_staged(staging, f["rel"], data)
        if transform in ("primary_database", "sqlite"):
            sqlite_checks[f["rel"]] = _finalize_sqlite(path, base_dir, tracked, transform == "primary_database")
            expected[f["rel"]] = _sha256_file(path)
        else:
            expected[f["rel"]] = hashlib.sha256(data).hexdigest()
        written.append({"root": "data", "rel": f["rel"], "archive_path": f["archive_path"],
                        "sha256": expected[f["rel"]], "size": os.lstat(path).st_size})
    _checkpoint("data_staged")

    try:
        trace = c1.write_restore_trace(staging, restore_id=restore_id,
                                       archive_sha256=plan["archive"]["archive_sha256"],
                                       restore_class=plan["restore_class"])
    except (c1.RestoreTraceRefused, ValueError) as exc:
        raise RestoreRefused("C1_TRACE_FAILED", str(exc)) from None
    expected[trace] = None
    journal_rel = f"{HISTORY_DIRNAME}/{restore_id}/{JOURNAL_NAME}"
    receipt_rel = f"{HISTORY_DIRNAME}/{restore_id}/{RECEIPT_NAME}"
    expected[journal_rel] = None

    install_written = []
    for f in files:
        if f["root"] == "install":
            install_written.append({"rel": f["rel"], "archive_path": f["archive_path"],
                                    "action": f["action"], "sha256": f["sha256"], "size": f["size"]})
    receipt = {
        "schema": RECEIPT_SCHEMA, "version": VERSION, "restore_id": restore_id,
        "state": "published_execution_fenced", "created_at": _now(),
        "archive": plan["archive"], "restore_class": plan["restore_class"],
        "verified_agent_backup": plan["verified_agent_backup"], "coverage": plan["coverage"],
        "plan_digest": plan["plan_digest"],
        "destination": {"data_dir": data_dir, "install_dir": base_dir},
        "units": [{k: u[k] for k in ("unit", "disposition", "availability", "choice", "conflicts", "skipped")}
                  for u in plan["units"]],
        "units_not_in_archive": plan["units_not_in_archive"],
        "data_files": written, "install_files": install_written,
        "rebind": {"preferences": notes.get("preferences", {}),
                   "primary_database": sqlite_checks.get("memory/lumina.db", {}),
                   "project_roots": "historical_inactive_owner_must_repick"},
        "sqlite": {rel: note.get("integrity_check") for rel, note in sqlite_checks.items()},
        "c1": {"trace": trace, "archived_c1_state": "never_restored",
               "required_next_step": "owner_local_recover",
               "first_run_initialize": "refused_trace_present"},
        "fence": {"telegram_remote_access": False, "custom_tools_loaded": False,
                  "custom_tool_approvals_active": False, "pending_actions_replayed": False,
                  "credentials_imported": False, "project_roots_bound": False,
                  "historical_inactive_location": f"{HISTORY_DIRNAME}/{restore_id}"},
    }
    receipt["receipt_digest"] = _digest(receipt)
    _write_staged(staging, receipt_rel, abx._json_bytes(receipt))
    expected[receipt_rel] = hashlib.sha256(abx._json_bytes(receipt)).hexdigest()
    _validate_staged(staging, expected)
    _checkpoint("staged_validated")

    journal["state"] = "install_writing"
    _write_journal(staging, restore_id, journal)
    for f in files:
        if f["root"] != "install":
            continue
        target = os.path.join(base_dir, f["rel"])
        state, st = _lstat_chain(base_dir, f["rel"])
        if f["action"] in ("already_present_identical", "keep_destination"):
            if state != "file" or [st.st_dev, st.st_ino, st.st_size] != f["observed"]:
                raise RestoreRefused("INSTALL_TARGET_CHANGED", f["rel"])
            continue
        if state != "absent":
            raise RestoreRefused("INSTALL_TARGET_CHANGED", f["rel"])
        _make_install_dirs(base_dir, f["rel"], journal, lambda: _write_journal(staging, restore_id, journal))
        entry = {"path": target, "tmp": _install_tmp(target, restore_id), "sha256": f["sha256"],
                 "identity": None, "seal": None}
        journal["install_created"].append(entry)
        _write_journal(staging, restore_id, journal)
        # The marked inode is journaled before any bytes, and its seal before
        # the link names it; a crash before the first leaves an unprovable
        # temp, which recovery preserves and reports.
        fd, entry["identity"] = _open_install_tmp(entry["tmp"], restore_id)
        try:
            _checkpoint("install_tmp_created")
            _write_journal(staging, restore_id, journal)
            _checkpoint("install_tmp_journaled")
        except BaseException:
            os.close(fd)
            raise
        entry["seal"] = _fill_install_tmp(fd, payload[f["archive_path"]])
        try:
            _write_journal(staging, restore_id, journal)
        except BaseException:
            os.close(fd)
            raise
        _link_install_file(fd, entry["tmp"], target)
        _checkpoint("install_file_created")

    journal["state"] = "publishing"
    _write_journal(staging, restore_id, journal)
    for root, dirs, _files in os.walk(staging):
        for name in dirs:
            _fsync_dir(os.path.join(root, name))
    _fsync_dir(staging)
    _checkpoint("before_publish")
    if dest["state"] == "empty_directory":
        if _real_dir(data_dir, "DESTINATION_CHANGED", "destination") != dest["identity"]:
            raise RestoreRefused("DESTINATION_CHANGED")
        try:
            os.rmdir(data_dir)
        except OSError:
            raise RestoreRefused("DESTINATION_CHANGED", "destination is no longer empty") from None
    try:
        _rename_noreplace(staging, data_dir)
    except BaseException:
        if dest["state"] == "empty_directory" and not os.path.lexists(data_dir):
            os.mkdir(data_dir, 0o700)
        raise
    # Published: from here on the root is complete and nothing rolls back.
    journal["state"] = "published"
    _fsync_dir(os.path.dirname(data_dir))
    _checkpoint("published")
    st = os.lstat(data_dir)
    journal["published_identity"] = [st.st_dev, st.st_ino]
    _write_journal(data_dir, restore_id, journal)
    return receipt


# --------------------------------------------------------------------------
# Interrupted restore and acceptance
# --------------------------------------------------------------------------

def _remove_staging(staging, report):
    """Remove a staging folder (fd-based: refuses a link, never leaves the
    tree). One that cannot be removed is reported, and recovery carries on."""
    try:
        shutil.rmtree(staging)
        return True
    except OSError:
        report["left_alone"].append(staging)
        return False


def recover_interrupted_restore(data_dir, *, quarantine_unrecognized=False):
    """Roll back every restore that was killed before publishing to `data_dir`.

    A staging directory still exists only if the publishing rename never
    happened (it is atomic), so the destination was never touched: remove the
    install files the journal proves this restore created, then the staging
    directory. A private staging directory killed before its first journal
    write holds nothing else and is removed. Any other one -- unreadable,
    foreign, linked, or naming anything this engine could not have written --
    is left alone, intact, and reported; it keeps new restores blocked until
    the owner passes quarantine_unrecognized=True, which renames it aside to
    `<name>.quarantined-<hex>` (never deletes it, nor any install file it
    names) for the owner to inspect. Returns a report.
    """
    data_dir = os.path.abspath(os.fspath(data_dir))
    _refuse_production(data_dir, "destination data folder")
    report = {"rolled_back": [], "cleared_never_journaled": [], "left_alone": [], "quarantined": [],
              "install_files_left": [], "install_paths_preserved": []}
    for staging in _leftover_staging(data_dir):
        journal = _read_journal(staging)
        if journal is not None and _recognized_journal(journal, data_dir):
            left, preserved = _rollback_install(journal)
            report["install_files_left"].extend(left)
            report["install_paths_preserved"].extend(preserved)
            if _remove_staging(staging, report):
                report["rolled_back"].append(journal["restore_id"])
        elif journal is None and _never_journaled(staging):
            if _remove_staging(staging, report):
                report["cleared_never_journaled"].append(staging)
        elif quarantine_unrecognized:
            aside = f"{staging[:-len('.staging')]}.quarantined-{secrets.token_hex(4)}"
            _rename_noreplace(staging, aside)  # renames a link itself, never its target
            report["quarantined"].append({"from": staging, "to": aside})
        else:
            report["left_alone"].append(staging)
    return report


def verify_restored_root(data_dir, receipt):
    """Read-only acceptance of a published root against its receipt."""
    data_dir = os.path.abspath(os.fspath(data_dir))
    base_dir = receipt["destination"]["install_dir"]
    checks = {}
    receipt_path = os.path.join(data_dir, HISTORY_DIRNAME, receipt["restore_id"], RECEIPT_NAME)
    with open(receipt_path, "rb") as fh:
        stored = ab._strict_json_loads(fh.read())
    checks["receipt_matches"] = stored == receipt and stored["receipt_digest"] == _digest(
        {k: v for k, v in stored.items() if k != "receipt_digest"})
    checks["data_files_match"] = all(
        _lstat_chain(data_dir, f["rel"])[0] == "file" and _sha256_file(os.path.join(data_dir, f["rel"])) == f["sha256"]
        for f in receipt["data_files"])
    checks["install_files_match"] = all(
        f["action"] == "keep_destination" or
        (_lstat_chain(base_dir, f["rel"])[0] == "file"
         and _sha256_file(os.path.join(base_dir, f["rel"])) == f["sha256"])
        for f in receipt["install_files"])
    checks["sqlite_integrity"] = all(_integrity(os.path.join(data_dir, rel)) for rel in receipt["sqlite"])
    # Authority re-derived from the published bytes, not from the receipt's
    # own counters: no archived executable schema, the shipped skills shape,
    # and no Skill claiming official (that comes only from bootstrap).
    schema_ok = authority_ok = True
    for rel in receipt["sqlite"]:
        conn = sqlite3.connect(_sqlite_uri(os.path.join(data_dir, rel), "ro"), uri=True)
        try:
            conn.execute("PRAGMA trusted_schema=OFF")
            primary = rel == "memory/lumina.db"
            schema_ok = schema_ok and not _untrusted_schema(conn, primary) and not _generated_columns(conn) \
                and not _non_literal_defaults(conn) and not _unshipped_foreign_keys(conn)
            if primary:
                authority_ok = authority_ok and not _official_rows(conn)
                # The same search-table pin and first-start replay, on an
                # in-memory copy (this check never writes the published root).
                _fts_shape(conn)
                with contextlib.closing(sqlite3.connect(":memory:", isolation_level=None)) as copy:
                    conn.backup(copy)
                    copy.execute("PRAGMA trusted_schema=OFF")
                    copy.execute("PRAGMA foreign_keys=OFF")
                    _startup_replay(copy)
        except (sqlite3.Error, RestoreRefused):
            schema_ok = authority_ok = False
        finally:
            conn.close()
    checks["sqlite_schema_vetted"] = schema_ok
    checks["skill_authority_fenced"] = authority_ok
    checks["no_live_authority_names"] = not any(os.path.lexists(os.path.join(data_dir, rel))
                                                for rel in _FORBIDDEN_LIVE)
    projects = os.path.join(data_dir, "projects")
    checks["no_live_project_bindings"] = not (os.path.isdir(projects) and any(
        os.path.lexists(os.path.join(projects, p, "binding.json")) for p in os.listdir(projects)))
    prefs = os.path.join(data_dir, "memory", "prefs.json")
    if os.path.exists(prefs):
        with open(prefs, "rb") as fh:
            values = json.loads(fh.read())
        checks["prefs_fenced"] = values.get("telegram_remote_access") is False and not any(
            t in k.lower() for k in values for t in ("api_key", "token", "secret", "password", "pin_"))
    checks["c1_restore_trace"] = c1.restore_trace_state(data_dir) == "absent_with_trace"
    return {"checks": checks, "passed": all(checks.values())}
