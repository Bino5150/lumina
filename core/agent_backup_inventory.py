"""core/agent_backup_inventory.py -- data-root inventory completeness
(AGENT-BACKUP-RESTORE-PHASE-B, R0: the "missing-state rejection" slice of B1).

A Verified Agent Backup has to prove that it captured everything the data
root holds, not only that the archive it wrote is intact. The exporter
collects from a fixed allowlist, so a state family it was never taught about
is left out silently: no member, no exclusion, no warning. This module is
the other half of that proof. It walks the data root and puts every entry in
exactly one class, and every class says what the Agent Backup contract does
with it:

  captured            an export collector archives it (the logical id is named;
                      prefs.json is archived through a reviewed-key allowlist)
  regenerated         rebuilt at the destination, never carried
  excluded_by_design  a security boundary the exporter enforces
  ephemeral           session-local or a write leftover, declared or harmless
  c1_protected        owned by C1; observed by core.suspension_backup, never opened
  excluded_declared   left out by a recorded owner decision (D1-D4, 2026-11-18),
                      never archived, never deleted or changed; each entry
                      names its decision and is counted in exclusion_receipt()
  uncaptured_known    real state that source names but no collector archives
  undeclared          not named by the contract at all
  unsafe              a link or special file where state is expected

uncaptured_known, undeclared and unsafe block, and so does a required source
that is missing or empty. `no_known_blockers` means only that: no known
family is left out and nothing could not be classified. It says nothing about
the content of captured files and is not a Verified Agent Backup claim by
itself. Unknown is never reported as absent. An excluded_declared entry does
not block, and it is not captured either: the receipt lists it, so a reader
never takes "no blockers" to mean "everything was carried".

The owner decisions, each matched narrowly so anything else still blocks:

  D1  chrome_companion/ (a real directory): machine-local pairing; recovery
      requires an explicit re-pair.
  D2  memory/lumina.db.bak_<tag> and memory/prefs.json.bak_<tag> (regular
      files): owner side copies, never parsed or carried, so a side copy can't
      get around the preferences filter. A bare "*.bak" or any other name
      still blocks.
  D3  the stray root lumina.db, only while it is a regular 0-byte file that
      is unchanged across the check, with no SQLite sidecar beside it, no
      write access for group or other, a single link, owned by this user, and
      no inspectable process holding it open ("unused" means exactly that: a
      process this user may not inspect is counted in the receipt, not
      ruled out). Otherwise it blocks as undeclared with decision D3. The configured
      database is always memory/lumina.db (config.DB_PATH), never this file.
  D4  telemetry/flight-recorder-corrupt-<YYYYMMDD>-<HHMMSS>/ (a real
      directory): forensic quarantine, held until the owner decides its
      disposition. It is never active recovered state, and the 90-day
      error-history retention of flight_recorder.db does not apply to it.

It only looks. Directories are walked through descriptors opened with
O_NOFOLLOW and identity-checked against the lstat that classified them, so a
directory swapped for a link mid-walk is refused, never followed. The D1 and
D4 directories are walked the same way only to count their entries. No
regular file is ever opened, so it can't create SQLite sidecars on a running
installation. The D3 check reads /proc/<pid>/fd links with stat(), which
never opens the file they name; a process it may not inspect is counted in
the receipt, not hidden. It is NOT wired into any export, button or Restore path; doing
that, and deciding what to do about each blocking family, are separate
reviewed steps. The BASE_DIR overlays (personas, avatars, voices, skills,
tool profiles, project documents) are outside its scope.
"""
from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass

from core import suspension_backup as c1

CAPTURED = "captured"
REGENERATED = "regenerated"
EXCLUDED = "excluded_by_design"
EPHEMERAL = "ephemeral"
C1_PROTECTED = "c1_protected"
EXCLUDED_DECLARED = "excluded_declared"
UNCAPTURED_KNOWN = "uncaptured_known"
UNDECLARED = "undeclared"
UNSAFE = "unsafe"
CLASSES = (CAPTURED, REGENERATED, EXCLUDED, EPHEMERAL, C1_PROTECTED,
           EXCLUDED_DECLARED, UNCAPTURED_KNOWN, UNDECLARED, UNSAFE)
BLOCKING = frozenset({UNCAPTURED_KNOWN, UNDECLARED, UNSAFE})

# Owner decisions of 2026-11-18. The receipt fields are constants: they never
# carry a path, a name, a value or file content.
DECISIONS = {
    "D1": {"family": "chrome_companion_pairing", "repair_required": "chrome_companion_pairing"},
    "D2": {"family": "owner_side_copies", "restore": "refused_in_verified_archives"},
    "D3": {"family": "stray_root_database", "condition": "verified_empty_no_inspectable_holder"},
    "D4": {"family": "flight_recorder_forensic_quarantine",
           "hold": "until_owner_disposition", "active_recovered_state": False},
}
RECEIPT_SCHEMA = "lumina.backup.declared_exclusions"
_SIDE_COPY = re.compile(r"(?:lumina\.db|prefs\.json)\.bak_[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_FORENSIC_QUARANTINE = re.compile(r"flight-recorder-corrupt-[0-9]{8}-[0-9]{6}")
_STRAY_DB = "lumina.db"
_PROC = "/proc"
MAX_PROC_FDS = 200000

MAX_ENTRIES = 20000
MAX_DEPTH = 8

_SIDECARS = ("-wal", "-shm", "-journal")
_PREFS_TMP = re.compile(r"prefs\.json\.[0-9]+\.[0-9]+\.tmp")
# agent_backup._SKIP_DIR_NAMES: the custom-tool collector never descends these.
_CUSTOM_TOOLS_SKIPPED = {"__pycache__": (REGENERATED, "Python bytecode cache; never collected"),
                         ".git": (UNCAPTURED_KNOWN, "custom_tools/.git is skipped by the collector")}
# The export always writes this record, "missing_unknown" when the source is absent.
MARKER_ARCHIVE_PATH = "metadata/knowledge_pack_installation.json"

# Data-root files a collector archives: relpath -> (logical id, archive path).
_CAPTURED_FILES = {
    "memory/lumina.db": ("state.databases.lumina_db", "state/databases/lumina.db"),
    "telemetry/flight_recorder.db": ("state.telemetry.flight_recorder_db",
                                     "state/telemetry/flight_recorder.db"),
    "memory/prefs.json": ("state.preferences.prefs_json", "state/preferences/prefs.json"),
    "memory/tool_audit.log": ("state.audit.tool_audit_log", "state/audit/tool_audit.log"),
    "memory/pending_actions.json": ("state.audit.pending_actions.json",
                                    "state/audit/pending_actions.json"),
    "memory/pending_actions_audit.log": ("state.audit.pending_actions_audit.log",
                                         "state/audit/pending_actions_audit.log"),
    "knowledge_packs_installed.json": ("metadata.knowledge_pack_installation", MARKER_ARCHIVE_PATH),
}
# The three sources a full archive always contains (agent_backup's floor).
REQUIRED = ("memory/lumina.db", "telemetry/flight_recorder.db", "memory/prefs.json")

# Whole subtrees that are not walked; the class covers everything below.
# Each must be a real directory: a link or special file here is unsafe.
_SUBTREES = {
    "worktrees": (EPHEMERAL, "declared exclusion 'worktrees' (coding-agent working copies, "
                             "which may hold unmerged work)"),
    "skills/official": (REGENERATED, "official Skills are bootstrapped at the destination from "
                                     "skill_packages/official; Restore never trusts archived official status"),
    # S3: export 2.2 captures each referenced blob and manifest, hash-verified
    # against its row; per-file completeness is core.agent_backup_references'
    # job, and a missing or mismatched blob blocks there, not here.
    "artifacts/generation": (CAPTURED, "export 2.2 captures every referenced blob and manifest, "
                                       "hash-verified per row (agent_backup_references)"),
    "restore_history": (UNCAPTURED_KNOWN, "inactive history from earlier isolated recoveries; "
                                          "neither captured nor declared"),
}
# Declared subtrees: walked only to count entries, never classified inside.
_DECLARED_SUBTREE_REASONS = {
    "D1": "owner decision D1: machine-local Chrome Companion pairing; re-pair after recovery",
    "D4": "owner decision D4: forensic quarantine, preserved in place until the owner decides",
}
_CONTAINERS = frozenset({"memory", "telemetry", "custom_tools", "projects", "artifacts", "skills"})

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


class InventoryUnavailable(Exception):
    """The data root can't be observed completely; nothing may be claimed."""


@dataclass(frozen=True)
class Entry:
    relpath: str
    kind: str        # "file" | "dir" | "link" | "other"
    cls: str
    reason: str
    logical_id: str | None = None
    archive_path: str | None = None
    size: int | None = None
    decision: str | None = None   # owner decision id (D1-D4), excluded or blocked
    items: int | None = None      # entries counted below a declared directory
    item_bytes: int | None = None  # regular-file bytes below a declared directory


@dataclass(frozen=True)
class InventoryReport:
    entries: tuple
    missing_required: tuple
    empty_required: tuple = ()
    d3_uninspectable_processes: int = 0

    def exclusion_receipt(self) -> dict:
        """The declared-exclusion block a backup receipt carries: every owner
        decision is listed, present or not, with counts and constants only.
        status is "excluded", "absent" or, for D3 when its conditions fail,
        "blocked" (that entry is also in `blockers`)."""
        decisions = []
        for decision, fields in DECISIONS.items():
            mine = [e for e in self.entries if e.decision == decision]
            excluded = [e for e in mine if e.cls == EXCLUDED_DECLARED]
            blocked = [e for e in mine if e.cls in BLOCKING]
            status = "blocked" if blocked else "excluded" if excluded else "absent"
            items = sum(1 if e.items is None else e.items for e in excluded)
            size = sum((e.size or 0) if e.items is None else e.item_bytes for e in excluded)
            record = {"decision": decision, **fields, "status": status,
                      "entries": len(excluded), "items": items, "bytes": size}
            if decision == "D3" and mine:
                record["uninspectable_processes"] = self.d3_uninspectable_processes
            decisions.append(record)
        return {"schema": RECEIPT_SCHEMA, "version": 1, "paths_listed": False,
                "originals_modified": False, "decisions": decisions}

    @property
    def blockers(self) -> tuple:
        found = tuple(e.relpath for e in self.entries if e.cls in BLOCKING)
        return (found + tuple("missing_required:" + r for r in self.missing_required)
                + tuple("empty_required:" + r for r in self.empty_required))

    @property
    def no_known_blockers(self) -> bool:
        return not self.blockers

    def expected_archive_paths(self) -> frozenset:
        expected = {e.archive_path for e in self.entries if e.archive_path}
        expected.add(MARKER_ARCHIVE_PATH)
        return frozenset(expected)


def _kind(st) -> str:
    if stat.S_ISLNK(st.st_mode):
        return "link"
    if stat.S_ISREG(st.st_mode):
        return "file"
    if stat.S_ISDIR(st.st_mode):
        return "dir"
    return "other"


def _classify_file(rel: str) -> tuple:
    """(class, reason, logical id, archive path) for a regular file."""
    parts = rel.split("/")
    if rel in _CAPTURED_FILES:
        logical, archive = _CAPTURED_FILES[rel]
        reason = ("export collector (reviewed-key allowlist; other keys omitted)"
                  if rel == "memory/prefs.json" else "export collector")
        return CAPTURED, reason, logical, archive
    for db in ("memory/lumina.db", "telemetry/flight_recorder.db"):
        if rel in (db + s for s in _SIDECARS):
            return CAPTURED, f"folded into the {db} SQLite snapshot", None, None
    if rel == "memory/ledger.db" or rel in ("memory/ledger.db" + s for s in _SIDECARS):
        return REGENERATED, "ledger is regenerated empty; content never read", None, None
    if len(parts) == 2 and parts[0] == "memory" and _PREFS_TMP.fullmatch(parts[1]):
        return EPHEMERAL, "atomic prefs write leftover", None, None
    if len(parts) == 2 and parts[0] == "memory" and _SIDE_COPY.fullmatch(parts[1]):
        return (EXCLUDED_DECLARED, "owner decision D2: owner side copy; never parsed or carried",
                None, None)
    if rel == "credentials.json":
        return EXCLUDED, "credentials are never archived (exporter identity-excludes the secrets file)", None, None
    if parts[0] == "custom_tools" and len(parts) >= 2 and rel.endswith(".py"):
        return (CAPTURED, "export collector (quarantined pending re-approval on Restore)",
                "state.custom_tools." + "/".join(parts[1:]), "state/" + rel)
    if parts[0] == "projects" and len(parts) == 3 and parts[2] in ("binding.json", "chats.json"):
        return (CAPTURED, "export collector",
                f"overlay.projects.{parts[1]}.{parts[2].replace('.', '_')}", "overlay/" + rel)
    return UNDECLARED, "not named by the Agent Backup contract", None, None


def _unavailable(what: str, exc: OSError):
    return InventoryUnavailable(f"cannot observe {what}: {type(exc).__name__}")


def _open_dir(name, dir_fd, expected, rel):
    """Open one directory without following a link, bound to the identity
    its lstat saw. Anything else is a change mid-walk and is refused.

    The (st_dev, st_ino) binding has one known window: if the directory is
    removed and a new one created on the same inode between the lstat and
    the open, the replacement is walked. It is still a real directory inside
    the root, opened without following a link, so the walk cannot leave the
    root; at worst it classifies the replacement's entries."""
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
    except OSError as exc:
        raise _unavailable(rel or "data root", exc) from None
    try:
        st = os.fstat(fd)
    except OSError as exc:
        os.close(fd)
        raise _unavailable(rel or "data root", exc) from None
    if (st.st_dev, st.st_ino) != (expected.st_dev, expected.st_ino):
        os.close(fd)
        raise InventoryUnavailable(f"{rel or 'data root'} changed during the inventory")
    return fd


def _open_root(data_dir: str) -> int:
    """Open the data root itself by descending from "/" one component at a
    time, each with O_NOFOLLOW, so neither the root nor any ancestor can be
    a link (a trailing slash or a linked parent would otherwise make the
    kernel follow one). The path must already be exact: absolute, with no
    trailing slash, "." , ".." or doubled separator, so the folder that is
    walked is the folder that was named."""
    if "\0" in data_dir:
        raise InventoryUnavailable("data root must not contain a NUL byte")
    if (not os.path.isabs(data_dir) or os.path.normpath(data_dir) != data_dir
            or data_dir.startswith("//")):
        raise InventoryUnavailable("data root must be an exact absolute path "
                                   "(no trailing slash, '.', '..' or '//')")
    fd = os.open("/", _DIR_FLAGS)
    try:
        for part in [p for p in data_dir.split("/") if p]:
            child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
    except (OSError, ValueError) as exc:
        os.close(fd)
        raise InventoryUnavailable(
            f"data root is missing, a link, under a link or not a directory: {type(exc).__name__}") from None
    return fd


def _scan(fd, rel) -> list:
    try:
        return sorted(entry.name for entry in os.scandir(fd))
    except OSError as exc:
        raise _unavailable(rel or "data root", exc) from None


def _open_holders(st):
    """(held, uninspectable) for the file whose lstat is `st`.

    Each /proc/<pid>/fd/<n> link is stat()ed, which resolves the descriptor
    without opening the file it names. A process this user may not inspect
    is counted, not assumed clean. If /proc itself can't be listed, or the
    scan passes MAX_PROC_FDS, the answer is unknown and comes back as held."""
    seen = uninspectable = 0
    try:
        pids = [e.name for e in os.scandir(_PROC) if e.name.isdigit()]
    except OSError:
        return True, 0
    for pid in pids:
        fd_dir = os.path.join(_PROC, pid, "fd")
        try:
            names = [e.name for e in os.scandir(fd_dir)]
        except (FileNotFoundError, ProcessLookupError):
            continue  # exited since the listing
        except OSError:
            uninspectable += 1
            continue
        for name in names:
            seen += 1
            if seen > MAX_PROC_FDS:
                return True, uninspectable
            try:
                target = os.stat(os.path.join(fd_dir, name))
            except (FileNotFoundError, ProcessLookupError):
                continue
            except OSError:
                uninspectable += 1
                break
            if (target.st_dev, target.st_ino) == (st.st_dev, st.st_ino):
                return True, uninspectable
    return False, uninspectable


def _stray_db_failures(fd, names, st):
    """Why the D3 exclusion does not hold for the root lumina.db (empty
    tuple when it holds), and how many processes could not be inspected."""
    failed = []
    if st.st_size != 0:
        failed.append("not_empty")
    if any(_STRAY_DB + s in names for s in _SIDECARS):
        failed.append("sidecar_present")
    if st.st_mode & 0o022:
        failed.append("writable_by_others")
    if st.st_nlink != 1:
        failed.append("linked_elsewhere")
    if st.st_uid != os.getuid():
        failed.append("foreign_owner")
    held, uninspectable = _open_holders(st)
    if held:
        failed.append("open_or_unknown")
    try:
        again = os.lstat(_STRAY_DB, dir_fd=fd)
    except OSError:
        again = None
    if (again is None or (again.st_dev, again.st_ino, again.st_size, again.st_mtime_ns, again.st_ctime_ns)
            != (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)):
        failed.append("changed_during_check")
    return tuple(failed), uninspectable


def inventory(data_dir) -> InventoryReport:
    """Classify every entry under `data_dir` without opening any file."""
    data_dir = os.fspath(data_dir)
    if not isinstance(data_dir, str):
        raise InventoryUnavailable("data root must be a text path")
    entries = []
    count = 0
    uninspectable = 0

    def count_tree(fd, rel_dir, depth):
        """(entries, regular-file bytes) below a declared directory. Nothing
        is classified, opened or followed; the walk shares the bounds."""
        nonlocal count
        items = size = 0
        for name in _scan(fd, rel_dir):
            count += 1
            if count > MAX_ENTRIES or depth + 1 > MAX_DEPTH:
                raise InventoryUnavailable("data root inventory exceeds its bounds")
            rel = f"{rel_dir}/{name}"
            try:
                st = os.lstat(name, dir_fd=fd)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise _unavailable(rel, exc) from None
            items += 1
            if stat.S_ISDIR(st.st_mode):
                child = _open_dir(name, fd, st, rel)
                try:
                    below, below_size = count_tree(child, rel, depth + 1)
                finally:
                    os.close(child)
                items += below
                size += below_size
            elif stat.S_ISREG(st.st_mode):
                size += st.st_size
        return items, size

    def walk(fd, rel_dir, depth):
        nonlocal count, uninspectable
        names = _scan(fd, rel_dir)
        for name in names:
            count += 1
            if count > MAX_ENTRIES or depth + 1 > MAX_DEPTH:
                raise InventoryUnavailable("data root inventory exceeds its bounds")
            rel = f"{rel_dir}/{name}" if rel_dir else name
            try:
                st = os.lstat(name, dir_fd=fd)
            except FileNotFoundError:
                continue  # removed since the scan; it can't be captured either
            except OSError as exc:
                raise _unavailable(rel, exc) from None
            kind = _kind(st)
            if c1.is_protected_relpath(rel):
                entries.append(Entry(rel, kind, C1_PROTECTED, "C1 safety state (never opened here)"))
                continue
            declared = ("D1" if rel == "chrome_companion"
                        else "D4" if rel_dir == "telemetry" and _FORENSIC_QUARANTINE.fullmatch(name)
                        else None)
            if declared:
                if kind != "dir":
                    cls = UNDECLARED if declared == "D4" and kind == "file" else UNSAFE
                    entries.append(Entry(rel, kind, cls, f"owner decision {declared} names a real "
                                         "directory here; this is not one", decision=declared))
                    continue
                child = _open_dir(name, fd, st, rel)
                try:
                    items, size = count_tree(child, rel, depth + 1)
                finally:
                    os.close(child)
                entries.append(Entry(rel, kind, EXCLUDED_DECLARED, _DECLARED_SUBTREE_REASONS[declared],
                                     decision=declared, items=items, item_bytes=size))
                continue
            if rel == _STRAY_DB and kind == "file":
                failed, uninspectable = _stray_db_failures(fd, names, st)
                if failed:
                    entries.append(Entry(rel, kind, UNDECLARED, "D3_condition_failed: " + ",".join(failed),
                                         size=st.st_size, decision="D3"))
                else:
                    entries.append(Entry(rel, kind, EXCLUDED_DECLARED,
                                         "owner decision D3: stray empty database, verified empty, no inspectable holder",
                                         size=0, decision="D3"))
                continue
            if rel == _STRAY_DB:
                entries.append(Entry(rel, kind, UNSAFE, "D3_condition_failed: not_a_regular_file",
                                     decision="D3"))
                continue
            if rel in _SUBTREES:
                cls, reason = _SUBTREES[rel]
                if kind != "dir":
                    cls, reason = UNSAFE, "expected a real directory; " + reason
                entries.append(Entry(rel, kind, cls, reason))
                continue
            if kind == "dir":
                if rel_dir == "custom_tools" or rel_dir.startswith("custom_tools/"):
                    if name in _CUSTOM_TOOLS_SKIPPED:
                        cls, reason = _CUSTOM_TOOLS_SKIPPED[name]
                        entries.append(Entry(rel, kind, cls, reason))
                        continue
                if ((rel_dir == "" and name not in _CONTAINERS)
                        or rel_dir in ("memory", "telemetry")
                        or rel.count("/") >= 2 and rel.startswith("projects/")):
                    entries.append(Entry(rel, kind, UNDECLARED, "directory not named by the contract"))
                    continue
                child = _open_dir(name, fd, st, rel)
                try:
                    walk(child, rel, depth + 1)
                finally:
                    os.close(child)
                continue
            if kind != "file":
                entries.append(Entry(rel, kind, UNSAFE, "link or special file; never followed"))
                continue
            cls, reason, logical, archive = _classify_file(rel)
            entries.append(Entry(rel, kind, cls, reason, logical, archive, st.st_size,
                                 decision="D2" if cls == EXCLUDED_DECLARED else None))

    root_fd = _open_root(data_dir)
    try:
        walk(root_fd, "", 0)
    finally:
        os.close(root_fd)
    entries.sort(key=lambda e: e.relpath)
    present = {e.relpath: e for e in entries if e.cls == CAPTURED and e.kind == "file"}
    missing = tuple(r for r in REQUIRED if r not in present)
    empty = tuple(r for r in REQUIRED if r in present and not present[r].size)
    return InventoryReport(tuple(entries), missing, empty, uninspectable)


GENERATION_ARCHIVE_PREFIX = "state/artifacts/generation/"


def data_root_archive_paths(manifest) -> frozenset:
    """Archive paths in a v1/v2 manifest that come from the data root.
    Generation blobs are left out: their per-file parity is checked row by
    row against the archived database (agent_backup_export), not here."""
    out = set()
    for member in manifest.get("members", []):
        path = member.get("archive_path", "")
        if path.startswith(GENERATION_ARCHIVE_PREFIX):
            continue
        if (path.startswith("state/") or path == MARKER_ARCHIVE_PATH
                or re.fullmatch(r"overlay/projects/[^/]+/(binding|chats)\.json", path)):
            out.add(path)
    return frozenset(out)


def parity(report: InventoryReport, manifest) -> dict:
    """Compare what the inventory says should be archived with what the
    manifest says was archived. Both directions must be empty for parity.
    The installation-marker record is always expected: the export writes it
    as "missing_unknown" when the source file is absent."""
    expected = report.expected_archive_paths()
    archived = data_root_archive_paths(manifest)
    return {"missing_from_archive": sorted(expected - archived),
            "unexpected_in_archive": sorted(archived - expected),
            "parity": expected == archived}
