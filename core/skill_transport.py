"""
Skill transport — deterministic import/export/bootstrap for Lumina skills.

SKILLS-IMPORT-EXPORT-PORTABILITY-01. Moves an approved skill's exact bytes
into a skills registry (filesystem + SQLite + FTS, all coherent) without an
LLM, without manual SQL, and without ever inferring its target from cwd or
ambient config -- every operation here takes an explicit `skills_dir` and
`db_path`. This is deliberate: core/skills.py's write_skill()/list_skills()/
etc. legitimately default to ambient config.BASE_DIR/config.DB_PATH for
interactive, single-machine use; this module never does, because its whole
job is moving skills *between* runtimes (release, dev, Prime, a fresh test
root) where silently reusing the caller's ambient runtime would be the bug.

Package shape (the portable transport unit, also what a fresh release ships
its OFFICIAL skills as under skill_packages/official/<name>/):

    <package_dir>/
        manifest.json   -- schema_version, name, description, origin,
                            payload_filename, payload_bytes, payload_sha256
        <payload_filename>   -- the exact skill Markdown, byte-for-byte

manifest.json is package/registry metadata only (T12) -- it never touches
or rewrites the payload Markdown. validate_package() proves the declared
byte count and SHA-256 match the actual payload BEFORE anything is ever
installed.

Collision/idempotency identity is (name, sha256, origin) -- the same triple
write_skill()'s UNIQUE(name) constraint already uses for identity, extended
with content+ownership so a same-name-different-payload package (or a
same-name-different-origin package) fails closed instead of silently
replacing what's installed (T7/T8/T9).

ensure_official_skills_bootstrapped() is the real-process-startup wrapper
(wired into main.py's run_cli()/run_gui() and core/headless.py's
get_headless_agent(), never into LuminaAgent.__init__ itself -- see that
function's own docstring for why). Its callers must pass data_dir and db_path
explicitly. A standalone harness therefore cannot silently fall back to the
owner's platformdirs runtime merely because LUMINA_DATA_DIR was omitted.
"""

import contextlib
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from datetime import datetime

from core.db import connect as db_connect
from core.skills import _safe_filename, init_skills_db


# ── Errors ─────────────────────────────────────────────────────────────────────

class SkillTransportError(Exception):
    """Base for all skill-transport failures."""
    code = "transport_error"


class PackageValidationError(SkillTransportError):
    """A package failed validation before any target mutation was attempted."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class SkillCollisionError(SkillTransportError):
    """Target already has a same-name skill with a different identity
    (different content hash and/or different origin). Fails closed --
    T8: same name + different payload is never an automatic overwrite."""
    code = "collision"

    def __init__(self, message: str, *, existing: dict, incoming: dict):
        self.existing = existing
        self.incoming = incoming
        super().__init__(message)


REQUIRED_MANIFEST_FIELDS = {
    "schema_version", "name", "description", "origin",
    "payload_filename", "payload_bytes", "payload_sha256",
}
ALLOWED_ORIGINS = {"official", "user"}
_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9 _.,:()/-]{0,199}$')
_SHA256_RE = re.compile(r'^[0-9a-f]{64}$')
MAX_SKILL_PAYLOAD_BYTES = 262144
MAX_PACKAGE_MANIFEST_BYTES = 65536
_BOUNDED_READ_CHUNK_BYTES = 65536


def _strict_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PackageValidationError(
                "duplicate_metadata", f"manifest.json contains duplicate field {key!r}"
            )
        result[key] = value
    return result


def _read_bounded_regular_file(path: str, *, max_bytes: int, label: str,
                               missing_code: str, too_large_code: str) -> bytes:
    """Read at most max_bytes + 1 from one descriptor-bound regular file.

    lstat + O_NOFOLLOW + inode comparison closes pathname/symlink swaps before
    the read. fstat rejects an already-oversized object without reading it;
    the bounded descriptor read remains authoritative if the same inode grows
    or shrinks after that check.
    """
    try:
        path_stat = os.lstat(path)
    except FileNotFoundError:
        raise PackageValidationError(missing_code, f"{label} not found: {path}")
    except OSError as e:
        raise PackageValidationError(missing_code, f"Cannot inspect {label} at {path}: {e}")

    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        raise PackageValidationError(
            "path_unsafe", f"{label} must be a regular, non-symlink file: {path}"
        )
    if path_stat.st_size > max_bytes:
        raise PackageValidationError(
            too_large_code,
            f"{label} is {path_stat.st_size} bytes; maximum is {max_bytes}",
        )

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as e:
        raise PackageValidationError(
            "path_unsafe", f"Cannot safely open {label} at {path}: {e}"
        )

    try:
        opened_stat = os.fstat(fd)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise PackageValidationError(
                "path_unsafe", f"{label} changed to a non-regular file: {path}"
            )
        if ((opened_stat.st_dev, opened_stat.st_ino)
                != (path_stat.st_dev, path_stat.st_ino)):
            raise PackageValidationError(
                "source_changed", f"{label} changed between lstat and open: {path}"
            )
        if opened_stat.st_size > max_bytes:
            raise PackageValidationError(
                too_large_code,
                f"{label} is {opened_stat.st_size} bytes; maximum is {max_bytes}",
            )

        chunks = []
        admitted = 0
        while admitted <= max_bytes:
            request = min(_BOUNDED_READ_CHUNK_BYTES, max_bytes + 1 - admitted)
            chunk = os.read(fd, request)
            if not chunk:
                break
            chunks.append(chunk)
            admitted += len(chunk)

        if admitted > max_bytes:
            raise PackageValidationError(
                too_large_code,
                f"{label} grew beyond the {max_bytes}-byte maximum while being read",
            )
        return b"".join(chunks)
    finally:
        os.close(fd)


# ── Validation (pure, read-only, no target touched) ─────────────────────────────

def validate_package(package_dir: str) -> dict:
    """Validate a package's structure, metadata, and payload identity.

    Raises PackageValidationError on any defect. Never touches an install
    target -- safe to call purely to inspect/report a package's identity.
    Returns a dict with the verified metadata (byte count and sha256 are the
    ACTUAL measured values, re-derived from the payload on disk, not merely
    echoed from the manifest) plus an internal '_raw_bytes' key so callers
    that go on to install don't have to re-read (and re-risk a TOCTOU gap
    against) the payload file a second time.
    """
    if not os.path.isdir(package_dir):
        raise PackageValidationError("not_a_directory", f"{package_dir} is not a directory")

    manifest_path = os.path.join(package_dir, "manifest.json")
    try:
        manifest_bytes = _read_bounded_regular_file(
            manifest_path,
            max_bytes=MAX_PACKAGE_MANIFEST_BYTES,
            label="manifest.json",
            missing_code="missing_manifest",
            too_large_code="manifest_too_large",
        )
        manifest = json.loads(
            manifest_bytes.decode("utf-8"), object_pairs_hook=_strict_json_object
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise PackageValidationError("malformed_manifest", f"manifest.json is not valid JSON: {e}")

    if not isinstance(manifest, dict):
        raise PackageValidationError("malformed_manifest", "manifest.json must be a JSON object")

    missing = REQUIRED_MANIFEST_FIELDS - manifest.keys()
    if missing:
        raise PackageValidationError(
            "missing_metadata", f"manifest.json missing required fields: {sorted(missing)}"
        )
    extra = manifest.keys() - REQUIRED_MANIFEST_FIELDS
    if extra:
        raise PackageValidationError(
            "extra_metadata", f"manifest.json contains unknown fields: {sorted(extra)}"
        )

    name = manifest["name"]
    description = manifest["description"]
    origin = manifest["origin"]
    payload_filename = manifest["payload_filename"]
    payload_bytes = manifest["payload_bytes"]
    payload_sha256 = manifest["payload_sha256"]

    if (not isinstance(manifest["schema_version"], int)
            or isinstance(manifest["schema_version"], bool)
            or manifest["schema_version"] != 1):
        raise PackageValidationError(
            "unsupported_schema", "schema_version must be the integer 1"
        )

    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise PackageValidationError("invalid_name", f"Invalid skill name: {name!r}")
    if not isinstance(description, str) or not description.strip():
        raise PackageValidationError("missing_metadata", "description must be a non-empty string")
    if origin not in ALLOWED_ORIGINS:
        raise PackageValidationError(
            "invalid_origin", f"origin must be one of {sorted(ALLOWED_ORIGINS)}, got {origin!r}"
        )
    if (not isinstance(payload_filename, str) or not payload_filename
            or "\x00" in payload_filename or "/" in payload_filename
            or "\\" in payload_filename
            or os.path.basename(payload_filename) != payload_filename
            or payload_filename in (".", "..")):
        # Bare filename only -- no separators, no traversal, no absolute path.
        raise PackageValidationError(
            "path_unsafe", f"payload_filename must be a bare filename: {payload_filename!r}"
        )
    if not isinstance(payload_bytes, int) or isinstance(payload_bytes, bool) or payload_bytes < 0:
        raise PackageValidationError("malformed_manifest", "payload_bytes must be a non-negative integer")
    if payload_bytes > MAX_SKILL_PAYLOAD_BYTES:
        raise PackageValidationError(
            "declared_payload_too_large",
            f"manifest declares {payload_bytes} payload bytes; maximum is "
            f"{MAX_SKILL_PAYLOAD_BYTES}",
        )
    if not isinstance(payload_sha256, str) or not _SHA256_RE.match(payload_sha256.lower()):
        raise PackageValidationError("malformed_manifest", "payload_sha256 must be a 64-char hex string")

    if payload_filename == "manifest.json":
        raise PackageValidationError(
            "path_unsafe", "payload_filename must be distinct from manifest.json"
        )
    payload_path = os.path.join(package_dir, payload_filename)
    try:
        package_entries = set(os.listdir(package_dir))
    except OSError as e:
        raise PackageValidationError(
            "malformed_package", f"Cannot enumerate package directory: {e}"
        )
    expected_entries = {"manifest.json", payload_filename}
    if payload_filename not in package_entries:
        raise PackageValidationError(
            "missing_payload", f"Payload file not found: {payload_path}"
        )
    if package_entries != expected_entries:
        raise PackageValidationError(
            "package_shape",
            f"Package must contain exactly {sorted(expected_entries)}, found "
            f"{sorted(package_entries)}",
        )

    real_pkg_dir = os.path.realpath(package_dir)
    real_payload_path = os.path.realpath(payload_path)
    if os.path.commonpath([real_pkg_dir, real_payload_path]) != real_pkg_dir:
        raise PackageValidationError("path_unsafe", "payload path escapes package directory")
    actual_bytes = _read_bounded_regular_file(
        payload_path,
        max_bytes=MAX_SKILL_PAYLOAD_BYTES,
        label="skill payload",
        missing_code="missing_payload",
        too_large_code="payload_too_large",
    )

    if len(actual_bytes) != payload_bytes:
        raise PackageValidationError(
            "byte_count_mismatch",
            f"manifest declares {payload_bytes} bytes, payload is actually {len(actual_bytes)}",
        )

    actual_hash = hashlib.sha256(actual_bytes).hexdigest()
    if actual_hash != payload_sha256.lower():
        raise PackageValidationError(
            "hash_mismatch",
            f"manifest declares sha256 {payload_sha256}, payload actually hashes to {actual_hash}",
        )

    return {
        "schema_version": manifest["schema_version"],
        "name": name,
        "description": description,
        "origin": origin,
        "payload_filename": payload_filename,
        "payload_bytes": len(actual_bytes),
        "payload_sha256": actual_hash,
        "_raw_bytes": actual_bytes,
    }


def inspect_package(package_dir: str) -> dict:
    """Validate a package and report its identity, without installing it."""
    info = dict(validate_package(package_dir))
    info.pop("_raw_bytes", None)
    return info


# ── Internal helpers ─────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now().isoformat()


def _atomic_write(path: str, data: bytes) -> None:
    """Write `data` to `path` with no partial-file window at the final path:
    write to a same-directory temp file, fsync, then os.replace (atomic
    rename on the same filesystem), then fsync the parent directory so the
    name publication itself is durable."""
    d = os.path.dirname(path)
    fd, tmp_path = tempfile.mkstemp(dir=d, prefix=".skill-import-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        _fsync_dir(d)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        raise


def _fsync_dir(path: str) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _remove_and_sync(path: str) -> None:
    os.remove(path)
    _fsync_dir(os.path.dirname(path))


def _cleanup_stale_import_temps(skills_dir: str) -> None:
    """Remove only transporter-owned temp residue while the DB write lock is
    held. Live importers use the same lock, so their temp files cannot be
    mistaken for crash residue."""
    removed = False
    with os.scandir(skills_dir) as entries:
        for entry in entries:
            if (entry.name.startswith(".skill-import-")
                    and entry.name.endswith(".tmp")
                    and entry.is_file(follow_symlinks=False)):
                os.remove(entry.path)
                removed = True
    if removed:
        _fsync_dir(skills_dir)


def _prepare_explicit_targets(*, skills_dir: str, db_path: str) -> tuple[str, str]:
    """Create and then pin explicit targets to non-symlinked absolute paths."""
    skills_dir = os.path.abspath(skills_dir)
    db_path = os.path.abspath(db_path)
    db_parent = os.path.dirname(db_path)
    # TEST-DATA-ISOLATION-01: fail before even directory preparation if a
    # test/harness explicitly resolves either target back into owner state.
    # This guard is a no-op in ordinary application processes.
    from core.test_isolation import refuse_if_production_path
    refuse_if_production_path(skills_dir)
    refuse_if_production_path(db_path)
    os.makedirs(skills_dir, exist_ok=True)
    os.makedirs(db_parent, exist_ok=True)

    if os.path.realpath(skills_dir) != skills_dir:
        raise PackageValidationError(
            "target_unsafe", f"skills_dir resolves through a symlink: {skills_dir}"
        )
    if os.path.realpath(db_parent) != db_parent or os.path.islink(db_path):
        raise PackageValidationError(
            "target_unsafe", f"db_path resolves through a symlink: {db_path}"
        )
    return skills_dir, db_path


def _hash_file(path: str) -> str:
    content = _read_bounded_regular_file(
        path,
        max_bytes=MAX_SKILL_PAYLOAD_BYTES,
        label="installed skill payload",
        missing_code="missing_payload",
        too_large_code="payload_too_large",
    )
    return hashlib.sha256(content).hexdigest()


def _verify_installed(*, name: str, db_path: str, expected_sha256: str) -> dict:
    """Re-open a FRESH connection and re-read the file from disk -- proves
    file + DB + content coherence (T5), not merely that a write call
    returned without raising."""
    conn = db_connect(path=db_path)
    try:
        row = conn.execute(
            "SELECT name, description, path, origin, content_sha256 "
            "FROM skills WHERE name=?", (name,)
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        raise SkillTransportError(f"Post-install verification failed: no DB row for '{name}'.")
    if os.path.islink(row["path"]):
        raise SkillTransportError(
            f"Post-install verification failed: '{name}' points to a symlink."
        )
    if not os.path.exists(row["path"]):
        raise SkillTransportError(
            f"Post-install verification failed: file missing at {row['path']}."
        )
    actual_hash = _hash_file(row["path"])
    if row["content_sha256"] != expected_sha256:
        raise SkillTransportError(
            f"Post-install verification failed: registry hash mismatch for '{name}' "
            f"(expected {expected_sha256}, stored {row['content_sha256']})."
        )
    if actual_hash != expected_sha256:
        raise SkillTransportError(
            f"Post-install verification failed: hash mismatch for '{name}' "
            f"(expected {expected_sha256}, found {actual_hash})."
        )

    return {
        "status": "installed",
        "name": row["name"],
        "description": row["description"],
        "origin": row["origin"],
        "path": row["path"],
        "sha256": actual_hash,
        "bytes": os.path.getsize(row["path"]),
    }


# ── Import ───────────────────────────────────────────────────────────────────

def import_skill_package(package_dir: str, *, skills_dir: str, db_path: str) -> dict:
    """Deterministically install a validated package into an explicit target
    (skills_dir + db_path). Never LLM, never manual SQL, never ambient config.

    Fail-closed semantics:
      - Package fails validation                       -> raises before any mutation.
      - No existing skill with this name                -> installs (file + DB row), verifies.
      - Existing skill, same (sha256, origin)            -> idempotent no-op, returns it (T7).
      - Existing skill, different sha256 and/or origin   -> raises SkillCollisionError (T8/T9).
      - File write succeeds, DB insert fails             -> file is removed, target left as it was (T6).
    """
    info = validate_package(package_dir)
    raw_bytes = info["_raw_bytes"]

    skills_dir, db_path = _prepare_explicit_targets(
        skills_dir=skills_dir, db_path=db_path
    )
    init_skills_db(db_path=db_path)
    target_path = os.path.join(skills_dir, _safe_filename(info["name"]))

    conn = db_connect(path=db_path)
    wrote_file = False
    try:
        # Serialize the identity check, deterministic path claim, filesystem
        # publication, and registry commit across processes. A losing importer
        # observes the winner instead of writing and then deleting its file.
        conn.execute("BEGIN IMMEDIATE")
        _cleanup_stale_import_temps(skills_dir)
        existing = conn.execute(
            "SELECT id, description, path, origin, content_sha256 "
            "FROM skills WHERE name=?", (info["name"],)
        ).fetchone()

        if existing:
            if os.path.abspath(existing["path"]) != target_path:
                raise SkillTransportError(
                    f"Existing DB row for '{info['name']}' points outside its explicit "
                    f"deterministic target (stored {existing['path']}, expected {target_path})."
                )
            if os.path.islink(existing["path"]):
                raise SkillTransportError(
                    f"Existing DB row for '{info['name']}' points to a symlink -- refusing it."
                )
            if not os.path.exists(existing["path"]):
                raise SkillTransportError(
                    f"Existing DB row for '{info['name']}' has no backing file at "
                    f"{existing['path']} -- refusing to import over an incoherent "
                    f"existing state."
                )
            existing_hash = _hash_file(existing["path"])
            existing_identity = {
                "sha256": existing_hash, "origin": existing["origin"], "path": existing["path"],
            }
            incoming_identity = {"sha256": info["payload_sha256"], "origin": info["origin"]}

            if (existing["content_sha256"] is not None
                    and existing_hash != existing["content_sha256"]):
                raise SkillTransportError(
                    f"Existing '{info['name']}' content disagrees with its persisted identity "
                    f"(expected {existing['content_sha256']}, found {existing_hash})."
                )

            if existing_hash == info["payload_sha256"] and existing["origin"] == info["origin"]:
                if existing["content_sha256"] is None:
                    conn.execute(
                        "UPDATE skills SET content_sha256=? WHERE id=?",
                        (info["payload_sha256"], existing["id"]),
                    )
                conn.commit()
                return {
                    "status": "already_installed",
                    "name": info["name"],
                    "description": existing["description"],
                    "origin": existing["origin"],
                    "path": existing["path"],
                    "sha256": existing_hash,
                }

            raise SkillCollisionError(
                f"'{info['name']}' is already installed with a different identity "
                f"(existing origin={existing['origin']!r} sha256={existing_hash}, "
                f"incoming origin={info['origin']!r} sha256={info['payload_sha256']}).",
                existing=existing_identity, incoming=incoming_identity,
            )

        for claimed in conn.execute(
            "SELECT name, path, origin, content_sha256 FROM skills WHERE name<>?",
            (info["name"],),
        ).fetchall():
            if os.path.abspath(claimed["path"]) == target_path:
                raise SkillCollisionError(
                    f"'{info['name']}' maps to {target_path}, already claimed by distinct "
                    f"skill {claimed['name']!r}; refusing a shared installed path.",
                    existing={
                        "name": claimed["name"], "origin": claimed["origin"],
                        "sha256": claimed["content_sha256"], "path": claimed["path"],
                    },
                    incoming={
                        "name": info["name"], "origin": info["origin"],
                        "sha256": info["payload_sha256"], "path": target_path,
                    },
                )

        if os.path.lexists(target_path):
            if os.path.islink(target_path):
                raise PackageValidationError(
                    "target_unsafe", f"Refusing symlink at installed target {target_path}"
                )
            # No DB row yet, but a file already sits at the target path --
            # e.g. bootstrap re-run, or a tracked in-repo copy. Only proceed
            # if it's byte-identical to what we're about to register;
            # otherwise this would silently present unrelated content as
            # the installed skill.
            if _hash_file(target_path) != info["payload_sha256"]:
                raise PackageValidationError(
                    "target_conflict",
                    f"A different, unregistered file already exists at {target_path}",
                )
        else:
            _atomic_write(target_path, raw_bytes)
            wrote_file = True

        conn.execute(
            "INSERT INTO skills "
            "(name, description, path, created_at, updated_at, origin, content_sha256) "
            "VALUES (?,?,?,?,?,?,?)",
            (info["name"], info["description"], target_path, _now(), _now(),
             info["origin"], info["payload_sha256"]),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        if wrote_file:
            with contextlib.suppress(OSError):
                _remove_and_sync(target_path)
        raise
    finally:
        conn.close()

    return _verify_installed(name=info["name"], db_path=db_path, expected_sha256=info["payload_sha256"])


# ── Export ───────────────────────────────────────────────────────────────────

def export_skill(name: str, dest_dir: str, *, skills_dir: str, db_path: str) -> dict:
    """Export an installed skill as a portable package (manifest.json +
    the exact payload bytes) into dest_dir. `skills_dir` is accepted for
    interface symmetry with import/bootstrap and as a future-proofing
    explicit-target hook; the row's own recorded path is what's actually
    read, since that's the skill's real installed location."""
    skills_dir, db_path = _prepare_explicit_targets(
        skills_dir=skills_dir, db_path=db_path
    )
    expected_path = os.path.join(skills_dir, _safe_filename(name))

    conn = db_connect(path=db_path)
    try:
        row = conn.execute(
            "SELECT name, description, path, origin, content_sha256 "
            "FROM skills WHERE name=?", (name,)
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        raise SkillTransportError(f"No installed skill named '{name}' in target.")
    if os.path.abspath(row["path"]) != expected_path:
        raise SkillTransportError(
            f"Installed skill '{name}' points outside its explicit deterministic target "
            f"(stored {row['path']}, expected {expected_path})."
        )
    if os.path.islink(row["path"]):
        raise SkillTransportError(f"Installed skill '{name}' points to a symlink.")
    if not os.path.exists(row["path"]):
        raise SkillTransportError(f"Installed skill '{name}' has no backing file at {row['path']}.")

    content = _read_bounded_regular_file(
        row["path"],
        max_bytes=MAX_SKILL_PAYLOAD_BYTES,
        label=f"installed skill {name!r}",
        missing_code="missing_payload",
        too_large_code="payload_too_large",
    )

    payload_filename = os.path.basename(row["path"])
    sha256 = hashlib.sha256(content).hexdigest()
    if row["content_sha256"] and sha256 != row["content_sha256"]:
        raise SkillTransportError(
            f"Installed skill '{name}' content disagrees with its persisted identity "
            f"(expected {row['content_sha256']}, found {sha256})."
        )
    if row["origin"] == "official" and not row["content_sha256"]:
        raise SkillTransportError(
            f"OFFICIAL skill '{name}' has no persisted expected content hash."
        )

    dest_dir = os.path.abspath(dest_dir)
    if os.path.exists(dest_dir) and os.listdir(dest_dir):
        raise SkillTransportError(f"Export destination {dest_dir} already exists and is not empty.")
    parent = os.path.dirname(dest_dir)
    os.makedirs(parent, exist_ok=True)

    manifest = {
        "schema_version": 1,
        "name": row["name"],
        "description": row["description"],
        "origin": row["origin"],
        "payload_filename": payload_filename,
        "payload_bytes": len(content),
        "payload_sha256": sha256,
    }

    staging = tempfile.mkdtemp(dir=parent, prefix=".skill-export-")
    try:
        _atomic_write(os.path.join(staging, payload_filename), content)
        manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
        _atomic_write(os.path.join(staging, "manifest.json"), manifest_bytes)
        if os.path.exists(dest_dir):
            os.rmdir(dest_dir)
            _fsync_dir(parent)
        os.replace(staging, dest_dir)
        _fsync_dir(parent)
    except BaseException:
        with contextlib.suppress(OSError):
            for entry in os.listdir(staging):
                os.remove(os.path.join(staging, entry))
            os.rmdir(staging)
        raise

    return manifest


# ── Bootstrap (fresh-release OFFICIAL skill availability) ──────────────────────

def _official_packages_root() -> str:
    """Repo-relative, never config/cwd-relative -- resolved from this
    module's own location so it's correct from any checkout path and
    immune to config.BASE_DIR being monkeypatched for test isolation."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(repo_root, "skill_packages", "official")


def bootstrap_official_skills(*, skills_dir: str, db_path: str) -> list:
    """Idempotently install every tracked OFFICIAL package shipped with this
    checkout under skill_packages/official/<name>/ into an explicit target.
    Deterministic, offline, no ambient config -- a fresh clone + a fresh
    empty db_path reproduces the same OFFICIAL skill availability every
    time. See ensure_official_skills_bootstrapped() for the real-startup
    entry point that calls this against the ambient runtime.
    """
    packages_root = _official_packages_root()
    results = []
    if not os.path.isdir(packages_root):
        return results
    for entry in sorted(os.listdir(packages_root)):
        pkg_dir = os.path.join(packages_root, entry)
        if os.path.isdir(pkg_dir):
            results.append(import_skill_package(pkg_dir, skills_dir=skills_dir, db_path=db_path))
    return results


def ensure_official_skills_bootstrapped(*, data_dir: str, db_path: str) -> list:
    """The real-process-startup entry point. Wired into main.py's
    run_cli()/run_gui() and core/headless.py's get_headless_agent() -- each
    an explicit, auditable call site, never LuminaAgent.__init__ itself,
    which every test that constructs an Agent (including tests with their
    own freshly isolated config.DB_PATH and strict skill-count assertions,
    e.g. tests/test_settings_skills_tab.py's env fixture reaching
    core.skills.init_skills_db() directly) also goes through. Keeping this
    out of that shared constructor path means no test's Agent construction
    can ever pick up two unexpected OFFICIAL skill rows as a side effect.

    Installs into explicit data_dir-relative storage
    (<data_dir>/skills/official/), NOT config.BASE_DIR's repo-tracked
    skills/ directory -- so ordinary startup never mutates the source
    checkout, and (same as the DB already does) this target is already
    isolated by LUMINA_DATA_DIR for every test in the suite, individually
    isolated or not, since conftest.py resolves DATA_DIR once, session-wide,
    before any test or fixture runs. user-authored skills (save_skill())
    are unaffected and keep living under BASE_DIR/skills as before -- this
    only changes where OFFICIAL bootstrap installs, nothing about the
    general skill-storage architecture.

    Never blocks startup on an ordinary/expected failure (missing tracked
    packages, a filesystem error) -- mirrors main.py's own fail-safe
    posture for non-critical startup side effects (_record_runtime_startup).
    Deliberately does NOT catch the test-isolation guard's RuntimeError
    (core/test_isolation.py's refuse_if_production_path, reached via
    core.db.connect()) -- that guard exists specifically to be loud and
    unmissable, and swallowing it here would quietly defeat its own purpose;
    it is a no-op in real (non-LUMINA_TESTING) use in any case.
    """
    skills_dir = os.path.join(data_dir, "skills", "official")
    try:
        return bootstrap_official_skills(skills_dir=skills_dir, db_path=db_path)
    except SkillTransportError as e:
        print(f"[skills] OFFICIAL skill bootstrap failed, continuing without it: {e}",
              file=sys.stderr)
        return []
    except OSError as e:
        print(f"[skills] OFFICIAL skill bootstrap failed (filesystem), continuing without it: {e}",
              file=sys.stderr)
        return []
