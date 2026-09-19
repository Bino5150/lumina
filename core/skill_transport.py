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
"""

import contextlib
import hashlib
import json
import os
import re
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
    if not os.path.isfile(manifest_path):
        raise PackageValidationError("missing_manifest", f"No manifest.json in {package_dir}")

    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise PackageValidationError("malformed_manifest", f"manifest.json is not valid JSON: {e}")

    if not isinstance(manifest, dict):
        raise PackageValidationError("malformed_manifest", "manifest.json must be a JSON object")

    missing = REQUIRED_MANIFEST_FIELDS - manifest.keys()
    if missing:
        raise PackageValidationError(
            "missing_metadata", f"manifest.json missing required fields: {sorted(missing)}"
        )

    name = manifest["name"]
    description = manifest["description"]
    origin = manifest["origin"]
    payload_filename = manifest["payload_filename"]
    payload_bytes = manifest["payload_bytes"]
    payload_sha256 = manifest["payload_sha256"]

    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise PackageValidationError("invalid_name", f"Invalid skill name: {name!r}")
    if not isinstance(description, str) or not description.strip():
        raise PackageValidationError("missing_metadata", "description must be a non-empty string")
    if origin not in ALLOWED_ORIGINS:
        raise PackageValidationError(
            "invalid_origin", f"origin must be one of {sorted(ALLOWED_ORIGINS)}, got {origin!r}"
        )
    if (not isinstance(payload_filename, str) or not payload_filename
            or os.path.basename(payload_filename) != payload_filename
            or payload_filename in (".", "..")):
        # Bare filename only -- no separators, no traversal, no absolute path.
        raise PackageValidationError(
            "path_unsafe", f"payload_filename must be a bare filename: {payload_filename!r}"
        )
    if not isinstance(payload_bytes, int) or isinstance(payload_bytes, bool) or payload_bytes < 0:
        raise PackageValidationError("malformed_manifest", "payload_bytes must be a non-negative integer")
    if not isinstance(payload_sha256, str) or not _SHA256_RE.match(payload_sha256.lower()):
        raise PackageValidationError("malformed_manifest", "payload_sha256 must be a 64-char hex string")

    payload_path = os.path.join(package_dir, payload_filename)
    real_pkg_dir = os.path.realpath(package_dir)
    real_payload_path = os.path.realpath(payload_path)
    if os.path.commonpath([real_pkg_dir, real_payload_path]) != real_pkg_dir:
        raise PackageValidationError("path_unsafe", "payload path escapes package directory")
    if os.path.islink(payload_path):
        raise PackageValidationError("path_unsafe", "payload_filename must not be a symlink")
    if not os.path.isfile(payload_path):
        raise PackageValidationError("missing_payload", f"Payload file not found: {payload_path}")

    with open(payload_path, "rb") as f:
        actual_bytes = f.read()

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
    rename on the same filesystem)."""
    d = os.path.dirname(path)
    fd, tmp_path = tempfile.mkstemp(dir=d, prefix=".skill-import-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        raise


def _hash_file(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _verify_installed(*, name: str, db_path: str, expected_sha256: str) -> dict:
    """Re-open a FRESH connection and re-read the file from disk -- proves
    file + DB + content coherence (T5), not merely that a write call
    returned without raising."""
    conn = db_connect(path=db_path)
    try:
        row = conn.execute(
            "SELECT name, description, path, origin FROM skills WHERE name=?", (name,)
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        raise SkillTransportError(f"Post-install verification failed: no DB row for '{name}'.")
    if not os.path.exists(row["path"]):
        raise SkillTransportError(
            f"Post-install verification failed: file missing at {row['path']}."
        )
    actual_hash = _hash_file(row["path"])
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

    init_skills_db(db_path=db_path)
    os.makedirs(skills_dir, exist_ok=True)

    conn = db_connect(path=db_path)
    try:
        existing = conn.execute(
            "SELECT id, description, path, origin FROM skills WHERE name=?", (info["name"],)
        ).fetchone()

        if existing:
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

            if existing_hash == info["payload_sha256"] and existing["origin"] == info["origin"]:
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

        target_filename = _safe_filename(info["name"])
        target_path = os.path.join(skills_dir, target_filename)

        wrote_file = False
        if os.path.exists(target_path):
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

        try:
            conn.execute(
                "INSERT INTO skills (name, description, path, created_at, updated_at, origin) "
                "VALUES (?,?,?,?,?,?)",
                (info["name"], info["description"], target_path, _now(), _now(), info["origin"]),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            if wrote_file:
                with contextlib.suppress(OSError):
                    os.remove(target_path)
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
    conn = db_connect(path=db_path)
    try:
        row = conn.execute(
            "SELECT name, description, path, origin FROM skills WHERE name=?", (name,)
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        raise SkillTransportError(f"No installed skill named '{name}' in target.")
    if not os.path.exists(row["path"]):
        raise SkillTransportError(f"Installed skill '{name}' has no backing file at {row['path']}.")

    with open(row["path"], "rb") as f:
        content = f.read()

    payload_filename = os.path.basename(row["path"])
    sha256 = hashlib.sha256(content).hexdigest()

    if os.path.exists(dest_dir) and os.listdir(dest_dir):
        raise SkillTransportError(f"Export destination {dest_dir} already exists and is not empty.")
    os.makedirs(dest_dir, exist_ok=True)

    manifest = {
        "schema_version": 1,
        "name": row["name"],
        "description": row["description"],
        "origin": row["origin"],
        "payload_filename": payload_filename,
        "payload_bytes": len(content),
        "payload_sha256": sha256,
    }

    _atomic_write(os.path.join(dest_dir, payload_filename), content)
    with open(os.path.join(dest_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
        f.write("\n")

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
    checkout under skill_packages/official/<name>/. Deterministic, offline,
    no ambient config -- a fresh clone + a fresh empty db_path reproduces
    the same OFFICIAL skill availability every time. Not wired into any
    live startup path in this campaign (see SKILLS-IMPORT-EXPORT-PORTABILITY-01
    deliverable notes) -- this is the primitive a startup sequence would call.
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
