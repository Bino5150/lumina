"""
SKILLS-IMPORT-EXPORT-PORTABILITY-01 — focused + adversarial + acceptance tests
for core/skill_transport.py.

Every target in this file is an explicit tmp_path-derived (skills_dir, db_path)
pair, never ambient config.BASE_DIR/config.DB_PATH -- proving the transporter's
own T2 (target explicitness) invariant, not just asserting it. conftest.py's
whole-session LUMINA_DATA_DIR/LUMINA_TESTING isolation is the backstop underneath
that; one test in this file (test_release_target_cannot_resolve_prime_db_by_accident)
exercises that backstop directly.
"""

import contextlib
import hashlib
import json
import os
import stat
import subprocess
import sys
import textwrap
import tempfile
import threading

import pytest

import config
from core.skill_transport import (
    MAX_PACKAGE_MANIFEST_BYTES,
    MAX_SKILL_PAYLOAD_BYTES,
    PackageValidationError,
    SkillCollisionError,
    SkillTransportError,
    bootstrap_official_skills,
    export_skill,
    import_skill_package,
    inspect_package,
    validate_package,
)
from core.skills import (
    OfficialSkillIntegrityError,
    OfficialSkillOverwriteError,
    SkillPayloadPolicyError,
    write_skill,
)


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OFFICIAL_PACKAGES_ROOT = os.path.join(REPO_ROOT, "skill_packages", "official")

FROZEN = {
    "media-generation": {
        "bytes": 10188,
        "sha256": "4eac5c81840715c3bf2a631734733afdf6105557195ac53c20320c7ae12cb9b9",
    },
    "generated-artifact-manifest": {
        "bytes": 8130,
        "sha256": "cf35a207d5c75c66f8672d26b37724c42381ea7d76efef13e618e235d4e7cbd2",
    },
}


# ── Fixtures / helpers ───────────────────────────────────────────────────────

@pytest.fixture
def target_a(tmp_path):
    return {"skills_dir": str(tmp_path / "a_skills"), "db_path": str(tmp_path / "a" / "lumina.db")}


@pytest.fixture
def target_b(tmp_path):
    return {"skills_dir": str(tmp_path / "b_skills"), "db_path": str(tmp_path / "b" / "lumina.db")}


def _make_package(pkg_dir, *, name="demo-skill", description="A demo skill for tests.",
                   origin="user", payload_filename="demo-skill.md",
                   content=b"# Skill: demo-skill\n\nDo the thing.\n",
                   manifest_overrides=None, omit_fields=None, write_payload=True):
    os.makedirs(pkg_dir, exist_ok=True)
    if write_payload:
        with open(os.path.join(pkg_dir, payload_filename), "wb") as f:
            f.write(content)
    manifest = {
        "schema_version": 1,
        "name": name,
        "description": description,
        "origin": origin,
        "payload_filename": payload_filename,
        "payload_bytes": len(content),
        "payload_sha256": hashlib.sha256(content).hexdigest(),
    }
    if manifest_overrides:
        manifest.update(manifest_overrides)
    if omit_fields:
        for k in omit_fields:
            manifest.pop(k, None)
    with open(os.path.join(pkg_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f)
    return pkg_dir


def _pad_manifest_to_exact_size(pkg_dir: str, exact_bytes: int) -> None:
    manifest_path = os.path.join(pkg_dir, "manifest.json")
    manifest = json.loads(open(manifest_path, "r", encoding="utf-8").read())
    encoded = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
    assert len(encoded) <= exact_bytes
    with open(manifest_path, "wb") as handle:
        handle.write(encoded)
        handle.write(b" " * (exact_bytes - len(encoded)))
    assert os.path.getsize(manifest_path) == exact_bytes


# ── Validation: happy path ───────────────────────────────────────────────────

def test_validate_package_valid(tmp_path):
    pkg = _make_package(str(tmp_path / "pkg"))
    info = inspect_package(pkg)
    assert info["name"] == "demo-skill"
    assert info["origin"] == "user"
    assert info["payload_bytes"] == len(b"# Skill: demo-skill\n\nDo the thing.\n")
    assert len(info["payload_sha256"]) == 64
    assert "_raw_bytes" not in info  # internal key stripped from the public report


# ── Validation: adversarial ──────────────────────────────────────────────────

def test_hash_mismatch_rejected(tmp_path):
    pkg = _make_package(str(tmp_path / "pkg"),
                         manifest_overrides={"payload_sha256": "0" * 64})
    with pytest.raises(PackageValidationError) as ei:
        validate_package(pkg)
    assert ei.value.code == "hash_mismatch"


def test_byte_count_mismatch_rejected(tmp_path):
    pkg = _make_package(str(tmp_path / "pkg"), manifest_overrides={"payload_bytes": 99999})
    with pytest.raises(PackageValidationError) as ei:
        validate_package(pkg)
    assert ei.value.code == "byte_count_mismatch"


def test_malformed_manifest_json_rejected(tmp_path):
    pkg_dir = str(tmp_path / "pkg")
    os.makedirs(pkg_dir)
    with open(os.path.join(pkg_dir, "manifest.json"), "w") as f:
        f.write("{not valid json")
    with open(os.path.join(pkg_dir, "demo.md"), "w") as f:
        f.write("content")
    with pytest.raises(PackageValidationError) as ei:
        validate_package(pkg_dir)
    assert ei.value.code == "malformed_manifest"


@pytest.mark.parametrize("field", [
    "schema_version", "name", "description", "origin",
    "payload_filename", "payload_bytes", "payload_sha256",
])
def test_missing_required_metadata_rejected(tmp_path, field):
    pkg = _make_package(str(tmp_path / "pkg"), omit_fields=[field])
    with pytest.raises(PackageValidationError) as ei:
        validate_package(pkg)
    assert ei.value.code == "missing_metadata"


@pytest.mark.parametrize("bad_name", ["", "   ", "../evil", "/etc/passwd", "a" * 500])
def test_invalid_name_rejected(tmp_path, bad_name):
    pkg = _make_package(str(tmp_path / "pkg"), manifest_overrides={"name": bad_name})
    with pytest.raises(PackageValidationError) as ei:
        validate_package(pkg)
    assert ei.value.code == "invalid_name"


def test_invalid_origin_rejected(tmp_path):
    pkg = _make_package(str(tmp_path / "pkg"), manifest_overrides={"origin": "prime"})
    with pytest.raises(PackageValidationError) as ei:
        validate_package(pkg)
    assert ei.value.code == "invalid_origin"


def test_missing_manifest_rejected(tmp_path):
    pkg_dir = str(tmp_path / "pkg")
    os.makedirs(pkg_dir)
    with open(os.path.join(pkg_dir, "demo.md"), "w") as f:
        f.write("content, no manifest.json alongside it")
    with pytest.raises(PackageValidationError) as ei:
        validate_package(pkg_dir)
    assert ei.value.code == "missing_manifest"


def test_missing_payload_file_rejected(tmp_path):
    pkg = _make_package(str(tmp_path / "pkg"), write_payload=False)
    with pytest.raises(PackageValidationError) as ei:
        validate_package(pkg)
    assert ei.value.code == "missing_payload"


@pytest.mark.parametrize("traversal_name", ["../escaped.md", "sub/escaped.md", "/etc/passwd"])
def test_payload_path_traversal_rejected(tmp_path, traversal_name):
    pkg = _make_package(str(tmp_path / "pkg"),
                         manifest_overrides={"payload_filename": traversal_name},
                         write_payload=False)
    with pytest.raises(PackageValidationError) as ei:
        validate_package(pkg)
    assert ei.value.code == "path_unsafe"


def test_payload_symlink_escape_rejected(tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"not the real payload")
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    link = pkg_dir / "demo-skill.md"
    os.symlink(outside, link)
    manifest = {
        "schema_version": 1, "name": "demo-skill", "description": "x", "origin": "user",
        "payload_filename": "demo-skill.md", "payload_bytes": outside.stat().st_size,
        "payload_sha256": hashlib.sha256(outside.read_bytes()).hexdigest(),
    }
    (pkg_dir / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(PackageValidationError) as ei:
        validate_package(str(pkg_dir))
    assert ei.value.code == "path_unsafe"


# ── Import: install, coherence, idempotency, collisions ─────────────────────

def test_import_installs_file_db_and_verifies_coherence(tmp_path, target_a):
    pkg = _make_package(str(tmp_path / "pkg"))
    result = import_skill_package(pkg, **target_a)
    assert result["status"] == "installed"
    assert result["name"] == "demo-skill"
    assert os.path.exists(result["path"])
    with open(result["path"], "rb") as f:
        assert hashlib.sha256(f.read()).hexdigest() == result["sha256"]


def test_source_swap_after_validation_cannot_change_published_bytes(
        tmp_path, target_a, monkeypatch):
    import core.skill_transport as st
    original = b"# Skill: demo-skill\n\nValidated bytes.\n"
    replacement = b"# Skill: demo-skill\n\nAttacker! bytes.\n"
    assert len(original) == len(replacement)
    pkg = _make_package(str(tmp_path / "pkg"), content=original)
    real_validate = st.validate_package

    def validate_then_swap(package_dir):
        info = real_validate(package_dir)
        with open(os.path.join(package_dir, "demo-skill.md"), "wb") as handle:
            handle.write(replacement)
        return info

    monkeypatch.setattr(st, "validate_package", validate_then_swap)
    installed = st.import_skill_package(pkg, **target_a)
    with open(installed["path"], "rb") as handle:
        published = handle.read()
    assert published == original
    assert hashlib.sha256(published).hexdigest() == installed["sha256"]


def test_import_is_idempotent_on_identical_reimport(tmp_path, target_a):
    pkg = _make_package(str(tmp_path / "pkg"))
    first = import_skill_package(pkg, **target_a)
    second = import_skill_package(pkg, **target_a)
    assert second["status"] == "already_installed"
    assert second["sha256"] == first["sha256"]

    import core.db as db_mod
    conn = db_mod.connect(path=target_a["db_path"])
    n = conn.execute("SELECT COUNT(*) c FROM skills WHERE name=?", ("demo-skill",)).fetchone()["c"]
    n_fts = conn.execute(
        "SELECT COUNT(*) c FROM skills_fts WHERE skills_fts MATCH 'demo'"
    ).fetchone()["c"]
    conn.close()
    assert n == 1          # no duplicate row
    assert n_fts == 1       # no duplicate FTS entry


def test_import_same_name_different_hash_collision_refused(tmp_path, target_a):
    pkg1 = _make_package(str(tmp_path / "pkg1"), content=b"# Skill: demo-skill\n\nVersion 1.\n")
    pkg2 = _make_package(str(tmp_path / "pkg2"), content=b"# Skill: demo-skill\n\nVersion 2 (different).\n")
    import_skill_package(pkg1, **target_a)
    with pytest.raises(SkillCollisionError) as ei:
        import_skill_package(pkg2, **target_a)
    assert ei.value.existing["sha256"] != ei.value.incoming["sha256"]

    # Refusal must leave the original content untouched.
    import core.db as db_mod
    conn = db_mod.connect(path=target_a["db_path"])
    row = conn.execute("SELECT path FROM skills WHERE name=?", ("demo-skill",)).fetchone()
    conn.close()
    with open(row["path"], "rb") as f:
        assert f.read() == b"# Skill: demo-skill\n\nVersion 1.\n"


def test_import_same_identity_different_origin_collision_refused(tmp_path, target_a):
    content = b"# Skill: demo-skill\n\nSame bytes.\n"
    pkg_user = _make_package(str(tmp_path / "pkg_user"), origin="user", content=content)
    pkg_official = _make_package(str(tmp_path / "pkg_official"), origin="official", content=content)
    import_skill_package(pkg_user, **target_a)
    with pytest.raises(SkillCollisionError):
        import_skill_package(pkg_official, **target_a)


def test_user_import_cannot_overwrite_official_skill(tmp_path, target_a):
    official_pkg = _make_package(str(tmp_path / "official"), name="shared-name", origin="official",
                                  content=b"# Skill: shared-name\n\nOFFICIAL content.\n")
    import_skill_package(official_pkg, **target_a)

    user_pkg = _make_package(str(tmp_path / "user"), name="shared-name", origin="user",
                              content=b"# Skill: shared-name\n\nImpostor content.\n")
    with pytest.raises(SkillCollisionError) as ei:
        import_skill_package(user_pkg, **target_a)
    assert ei.value.existing["origin"] == "official"


def test_official_collision_with_different_hash_refused(tmp_path, target_a):
    pkg1 = _make_package(str(tmp_path / "pkg1"), name="off-skill", origin="official",
                          content=b"# Skill: off-skill\n\nV1.\n")
    pkg2 = _make_package(str(tmp_path / "pkg2"), name="off-skill", origin="official",
                          content=b"# Skill: off-skill\n\nV2 different.\n")
    import_skill_package(pkg1, **target_a)
    with pytest.raises(SkillCollisionError):
        import_skill_package(pkg2, **target_a)


def test_write_skill_refuses_to_overwrite_official_skill(tmp_path, target_a, monkeypatch):
    """write_skill()'s OFFICIAL guard fires on the DB-identity check alone,
    before it ever touches the filesystem -- so this only needs config.DB_PATH
    pointed at the target; config.BASE_DIR just needs to be a writable tmp
    dir (write_skill()'s _skills_dir() helper unconditionally mkdirs it,
    but the guard raises before anything is written into it)."""
    official_pkg = _make_package(str(tmp_path / "official"), name="reserved-name", origin="official",
                                  content=b"# Skill: reserved-name\n\nOFFICIAL, do not touch.\n")
    import_skill_package(official_pkg, **target_a)

    monkeypatch.setattr(config, "DB_PATH", target_a["db_path"])
    monkeypatch.setattr(config, "BASE_DIR", str(tmp_path / "unrelated_base"))

    with pytest.raises(OfficialSkillOverwriteError):
        write_skill("reserved-name", "an attempted user overwrite", "malicious content")


# ── Fail-closed / rollback ───────────────────────────────────────────────────

def test_filesystem_write_failure_leaves_no_db_row(tmp_path, target_a):
    pkg = _make_package(str(tmp_path / "pkg"))
    skills_dir = target_a["skills_dir"]
    os.makedirs(skills_dir)
    os.chmod(skills_dir, 0o555)  # read+execute only -- write must fail
    try:
        with pytest.raises(OSError):
            import_skill_package(pkg, **target_a)
    finally:
        os.chmod(skills_dir, 0o755)

    import core.db as db_mod
    conn = db_mod.connect(path=target_a["db_path"])
    n = conn.execute("SELECT COUNT(*) c FROM skills WHERE name=?", ("demo-skill",)).fetchone()["c"]
    conn.close()
    assert n == 0
    assert os.listdir(skills_dir) == []  # no orphan temp file left behind


class _SimulatedRegistrationFailure(Exception):
    pass


class _FailingConnProxy:
    """Wraps a real sqlite3.Connection and injects a failure on the specific
    INSERT that registers a skill, to prove import_skill_package rolls the
    just-written file back rather than leaving an orphan (T6)."""

    def __init__(self, real_conn):
        self._real = real_conn

    def execute(self, sql, *a, **kw):
        if "INSERT INTO skills" in sql:
            raise _SimulatedRegistrationFailure("simulated registration failure")
        return self._real.execute(sql, *a, **kw)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_db_registration_failure_removes_orphan_file(tmp_path, target_a, monkeypatch):
    import core.skill_transport as st

    pkg = _make_package(str(tmp_path / "pkg"))
    real_connect = st.db_connect

    def fake_connect(*, path):
        return _FailingConnProxy(real_connect(path=path))

    monkeypatch.setattr(st, "db_connect", fake_connect)

    with pytest.raises(_SimulatedRegistrationFailure):
        import_skill_package(pkg, **target_a)

    # No orphan file: skills_dir must be empty (the just-written payload
    # was rolled back along with the failed DB transaction).
    assert os.listdir(target_a["skills_dir"]) == []


# ── Target isolation ─────────────────────────────────────────────────────────

def test_target_isolation_import_into_a_does_not_appear_in_b(tmp_path, target_a, target_b):
    pkg = _make_package(str(tmp_path / "pkg"))
    import_skill_package(pkg, **target_a)

    import core.db as db_mod
    os.makedirs(os.path.dirname(target_b["db_path"]), exist_ok=True)
    from core.skills import init_skills_db
    init_skills_db(db_path=target_b["db_path"])
    conn = db_mod.connect(path=target_b["db_path"])
    n = conn.execute("SELECT COUNT(*) c FROM skills").fetchone()["c"]
    conn.close()
    assert n == 0
    assert not os.path.exists(target_b["skills_dir"])


def test_release_target_cannot_resolve_prime_db_by_accident(tmp_path, target_a):
    """T14: proves core.skill_transport inherits core/test_isolation.py's
    refuse_if_production_path backstop -- it never touches Prime's real
    data directory even if a caller mistakenly points db_path there, as
    long as the suite-wide LUMINA_TESTING guard (set by conftest.py) is
    active, which it always is for this whole pytest process."""
    from platformdirs import user_data_dir
    pkg = _make_package(str(tmp_path / "pkg"))
    real_prime_db = os.path.join(user_data_dir("lumina", appauthor=False), "memory", "lumina.db")
    with pytest.raises(RuntimeError, match="TEST-ISOLATION"):
        import_skill_package(pkg, skills_dir=target_a["skills_dir"], db_path=real_prime_db)


# ── Export round trip ─────────────────────────────────────────────────────────

def test_export_then_import_into_fresh_target_preserves_hash(tmp_path, target_a, target_b):
    pkg = _make_package(str(tmp_path / "pkg"), name="portable-skill",
                         content=b"# Skill: portable-skill\n\nRoams freely.\n")
    installed_a = import_skill_package(pkg, **target_a)

    export_dir = str(tmp_path / "exported")
    manifest = export_skill("portable-skill", export_dir, **target_a)
    assert manifest["payload_sha256"] == installed_a["sha256"]

    installed_b = import_skill_package(export_dir, **target_b)
    assert installed_b["sha256"] == installed_a["sha256"] == manifest["payload_sha256"]

    with open(installed_b["path"], "rb") as f:
        assert f.read() == b"# Skill: portable-skill\n\nRoams freely.\n"


def test_export_nonexistent_skill_raises(tmp_path, target_a):
    from core.skills import init_skills_db
    os.makedirs(os.path.dirname(target_a["db_path"]), exist_ok=True)
    init_skills_db(db_path=target_a["db_path"])
    with pytest.raises(SkillTransportError):
        export_skill("does-not-exist", str(tmp_path / "out"), **target_a)


# ── FTS / recall through the real native API ─────────────────────────────────

def test_fts_discovery_and_recall_through_native_api(tmp_path, target_a, monkeypatch):
    """search_skills()/load_skill() read the DB (FTS + the row's own stored
    absolute path) and never touch config.BASE_DIR, so only config.DB_PATH
    needs to be pointed at the target for this to exercise the real native
    read path end to end."""
    pkg = _make_package(str(tmp_path / "pkg"), name="native-lookup-skill",
                         description="A very particular searchable description about zebras",
                         content=b"# Skill: native-lookup-skill\n\nZebra procedure.\n")
    import_skill_package(pkg, **target_a)

    monkeypatch.setattr(config, "DB_PATH", target_a["db_path"])

    import core.skills as skills_mod
    hits = skills_mod.search_skills("zebras")
    assert any(h["name"] == "native-lookup-skill" for h in hits)

    content = skills_mod.load_skill("native-lookup-skill")
    assert content == "# Skill: native-lookup-skill\n\nZebra procedure.\n"


def test_init_repairs_db_fts_split_brain(tmp_path, target_a, monkeypatch):
    bootstrap_official_skills(**target_a)
    import core.db as db_mod
    conn = db_mod.connect(path=target_a["db_path"])
    row = conn.execute(
        "SELECT id, name, description FROM skills WHERE name='media-generation'"
    ).fetchone()
    conn.execute(
        "INSERT INTO skills_fts(skills_fts, rowid, name, description) "
        "VALUES ('delete', ?, ?, ?)",
        (row["id"], row["name"], row["description"]),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(config, "DB_PATH", target_a["db_path"])
    import core.skills as skills_mod
    assert "media-generation" not in {
        item["name"] for item in skills_mod.search_skills("image generation", limit=10)
    }

    skills_mod.init_skills_db(db_path=target_a["db_path"])
    assert "media-generation" in {
        item["name"] for item in skills_mod.search_skills("image generation", limit=10)
    }


# ── Restart / new-process discovery ───────────────────────────────────────────

def test_discovery_survives_a_fresh_process(tmp_path, target_a):
    """Proves durability across a genuine process boundary: a brand new
    Python interpreter, with no in-memory state from the writer (no cached
    rows, no open WAL connection), independently opens config.DB_PATH and
    finds the skill via the real search_skills()/load_skill() API."""
    pkg = _make_package(str(tmp_path / "pkg"), name="durable-skill",
                         content=b"# Skill: durable-skill\n\nSurvives a restart.\n")
    installed = import_skill_package(pkg, **target_a)
    with open(installed["path"], "r", encoding="utf-8") as f:
        expected_content = f.read()

    script = tmp_path / "fresh_process_check.py"
    script.write_text(textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {REPO_ROOT!r})
        import config
        config.DB_PATH = {target_a["db_path"]!r}
        import core.skills as skills_mod
        hits = skills_mod.search_skills("durable-skill")
        content = skills_mod.load_skill("durable-skill")
        assert any(h["name"] == "durable-skill" for h in hits), hits
        assert content == {expected_content!r}
        print("FRESH_PROCESS_OK")
    """))

    env = dict(os.environ)
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, env=env)
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "FRESH_PROCESS_OK" in result.stdout


# ── Bootstrap / fresh-release proof ───────────────────────────────────────────

def test_official_packages_root_contains_only_the_two_official_skills():
    entries = sorted(os.listdir(OFFICIAL_PACKAGES_ROOT))
    assert entries == ["generated-artifact-manifest", "media-generation"]
    for name in entries:
        pkg_dir = os.path.join(OFFICIAL_PACKAGES_ROOT, name)
        info = inspect_package(pkg_dir)
        assert info["origin"] == "official"
        assert info["name"] == name
        assert info["payload_bytes"] == FROZEN[name]["bytes"]
        assert info["payload_sha256"] == FROZEN[name]["sha256"]
    # promotion-manifest.md is evidence only -- never shipped as a package.
    assert not any("promotion" in e for e in entries)


def test_bootstrap_official_skills_fresh_release_proof(tmp_path, target_a):
    """Fresh/isolated release code + a fresh empty data root -> both OFFICIAL
    skills available with exact frozen bytes, no Prime access (structurally:
    only tmp_path and the tracked skill_packages/official/ tree are touched)."""
    results = bootstrap_official_skills(**target_a)
    by_name = {r["name"]: r for r in results}
    assert set(by_name) == {"media-generation", "generated-artifact-manifest"}
    for name, frozen in FROZEN.items():
        r = by_name[name]
        assert r["status"] == "installed"
        assert r["origin"] == "official"
        assert r["sha256"] == frozen["sha256"]
        with open(r["path"], "rb") as f:
            data = f.read()
        assert len(data) == frozen["bytes"]
        assert hashlib.sha256(data).hexdigest() == frozen["sha256"]


def test_bootstrap_official_skills_is_idempotent(tmp_path, target_a):
    first = bootstrap_official_skills(**target_a)
    second = bootstrap_official_skills(**target_a)
    assert {r["name"]: r["status"] for r in first} == {
        "media-generation": "installed", "generated-artifact-manifest": "installed",
    }
    assert {r["name"]: r["status"] for r in second} == {
        "media-generation": "already_installed", "generated-artifact-manifest": "already_installed",
    }


# ── War Room acceptance specimen (the two real frozen payloads) ─────────────

@pytest.mark.parametrize("skill_name", ["media-generation", "generated-artifact-manifest"])
def test_war_room_specimen_import_acceptance(tmp_path, target_a, skill_name):
    frozen = FROZEN[skill_name]
    pkg_dir = os.path.join(OFFICIAL_PACKAGES_ROOT, skill_name)

    # Recompute identity before use, per the campaign's own acceptance law.
    info = inspect_package(pkg_dir)
    assert info["payload_bytes"] == frozen["bytes"]
    assert info["payload_sha256"] == frozen["sha256"]

    installed = import_skill_package(pkg_dir, **target_a)
    assert installed["sha256"] == frozen["sha256"]
    assert installed["origin"] == "official"

    # Idempotent second import.
    again = import_skill_package(pkg_dir, **target_a)
    assert again["status"] == "already_installed"

    # DB row + FTS + recall through the real native API. list_skills()/
    # load_skill() read the row's own stored absolute path and never touch
    # config.BASE_DIR, so only config.DB_PATH needs to be pointed at target_a.
    import config as cfg
    old_db = cfg.DB_PATH
    try:
        cfg.DB_PATH = target_a["db_path"]
        import core.skills as skills_mod
        all_skills = skills_mod.list_skills()
        assert any(s["name"] == skill_name and s["origin"] == "official" for s in all_skills)
        recalled = skills_mod.load_skill(skill_name)
        with open(os.path.join(pkg_dir, f"{skill_name}.md"), "rb") as f:
            expected = f.read().decode("utf-8")
        assert recalled == expected
    finally:
        cfg.DB_PATH = old_db


def test_war_room_specimen_prime_only_skills_absent(tmp_path, target_a):
    """A skill that only exists on Prime (never packaged/imported here) must
    never appear in an isolated release target."""
    bootstrap_official_skills(**target_a)
    import core.db as db_mod
    conn = db_mod.connect(path=target_a["db_path"])
    names = {r["name"] for r in conn.execute("SELECT name FROM skills").fetchall()}
    conn.close()
    assert names == {"media-generation", "generated-artifact-manifest"}


def test_war_room_specimen_export_round_trip(tmp_path, target_a, target_b):
    """target A (isolated release) -> export both officials -> fresh target B
    -> hash chain A == exported == B for both frozen skills."""
    bootstrap_official_skills(**target_a)
    for name, frozen in FROZEN.items():
        export_dir = str(tmp_path / f"exported_{name}")
        manifest = export_skill(name, export_dir, **target_a)
        assert manifest["payload_sha256"] == frozen["sha256"]

        installed_b = import_skill_package(export_dir, **target_b)
        assert installed_b["sha256"] == frozen["sha256"]
        assert installed_b["origin"] == "official"

    import core.db as db_mod
    conn = db_mod.connect(path=target_b["db_path"])
    names = {r["name"] for r in conn.execute("SELECT name FROM skills").fetchall()}
    conn.close()
    assert names == {"media-generation", "generated-artifact-manifest"}


# ── Final combined-tree adversarial regressions ─────────────────────────────

@pytest.mark.parametrize("bad_schema", [0, 2, 999, True, "1"])
def test_manifest_rejects_unsupported_or_ambiguous_schema(tmp_path, bad_schema):
    pkg = _make_package(
        str(tmp_path / "pkg"), manifest_overrides={"schema_version": bad_schema}
    )
    with pytest.raises(PackageValidationError) as ei:
        validate_package(pkg)
    assert ei.value.code == "unsupported_schema"


def test_manifest_rejects_unknown_fields(tmp_path):
    pkg = _make_package(
        str(tmp_path / "pkg"), manifest_overrides={"shadow_identity": "ignored"}
    )
    with pytest.raises(PackageValidationError) as ei:
        validate_package(pkg)
    assert ei.value.code == "extra_metadata"


def test_manifest_rejects_duplicate_json_keys(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    payload = b"# Skill: duplicate\n"
    (pkg / "payload.md").write_bytes(payload)
    (pkg / "manifest.json").write_text(
        '{"schema_version":1,"name":"first","name":"second",'
        '"description":"duplicate","origin":"user",'
        '"payload_filename":"payload.md",'
        f'"payload_bytes":{len(payload)},'
        f'"payload_sha256":"{hashlib.sha256(payload).hexdigest()}"}}'
    )
    with pytest.raises(PackageValidationError) as ei:
        validate_package(str(pkg))
    assert ei.value.code == "duplicate_metadata"


def test_manifest_rejects_nul_payload_filename(tmp_path):
    pkg = _make_package(
        str(tmp_path / "pkg"),
        manifest_overrides={"payload_filename": "payload.md\x00shadow"},
    )
    with pytest.raises(PackageValidationError) as ei:
        validate_package(pkg)
    assert ei.value.code == "path_unsafe"


def test_runtime_official_drift_is_rejected_by_recall_and_export(
        tmp_path, target_a, monkeypatch):
    results = bootstrap_official_skills(**target_a)
    installed = {item["name"]: item for item in results}["media-generation"]
    original = open(installed["path"], "rb").read()
    tampered = original.replace(b"# Skill:", b"# SkilL:", 1)
    assert len(tampered) == len(original)
    assert hashlib.sha256(tampered).hexdigest() != FROZEN["media-generation"]["sha256"]
    with open(installed["path"], "wb") as handle:
        handle.write(tampered)

    monkeypatch.setattr(config, "DB_PATH", target_a["db_path"])
    import core.skills as skills_mod
    with pytest.raises(OfficialSkillIntegrityError, match="runtime integrity"):
        skills_mod.load_skill("media-generation")
    with pytest.raises(SkillTransportError, match="persisted identity"):
        export_skill(
            "media-generation", str(tmp_path / "exported"), **target_a
        )


@pytest.mark.parametrize("mutation", ["newline", "truncate", "replace", "delete", "symlink"])
def test_runtime_official_mutation_variants_fail_loud(
        tmp_path, target_a, monkeypatch, mutation):
    results = bootstrap_official_skills(**target_a)
    installed = {item["name"]: item for item in results}["media-generation"]
    path = installed["path"]
    original = open(path, "rb").read()

    if mutation == "newline":
        with open(path, "ab") as handle:
            handle.write(b"\n")
    elif mutation == "truncate":
        with open(path, "wb") as handle:
            handle.write(original[:100])
    elif mutation == "replace":
        replacement = tmp_path / "replacement.md"
        replacement.write_bytes(original.replace(b"# Skill:", b"# SkilL:", 1))
        os.replace(replacement, path)
    elif mutation == "delete":
        os.remove(path)
    else:
        outside = tmp_path / "outside.md"
        outside.write_bytes(original)
        os.remove(path)
        os.symlink(outside, path)

    monkeypatch.setattr(config, "DB_PATH", target_a["db_path"])
    import core.skills as skills_mod
    with pytest.raises(OfficialSkillIntegrityError):
        skills_mod.load_skill("media-generation")


def test_official_import_persists_expected_content_hash(tmp_path, target_a):
    bootstrap_official_skills(**target_a)
    import core.db as db_mod
    conn = db_mod.connect(path=target_a["db_path"])
    rows = {
        row["name"]: row["content_sha256"]
        for row in conn.execute(
            "SELECT name, content_sha256 FROM skills WHERE origin='official'"
        ).fetchall()
    }
    conn.close()
    assert rows == {name: frozen["sha256"] for name, frozen in FROZEN.items()}


def test_legacy_unbound_official_row_is_bound_only_after_byte_match(tmp_path, target_a):
    pkg = os.path.join(OFFICIAL_PACKAGES_ROOT, "media-generation")
    os.makedirs(target_a["skills_dir"])
    target_path = os.path.join(target_a["skills_dir"], "media-generation.md")
    with open(os.path.join(pkg, "media-generation.md"), "rb") as source:
        with open(target_path, "wb") as target:
            target.write(source.read())

    from core.skills import init_skills_db
    init_skills_db(db_path=target_a["db_path"])
    import core.db as db_mod
    conn = db_mod.connect(path=target_a["db_path"])
    conn.execute(
        "INSERT INTO skills "
        "(name, description, path, created_at, updated_at, origin, content_sha256) "
        "VALUES (?,?,?,?,?,'official',NULL)",
        ("media-generation", "legacy row", target_path, "now", "now"),
    )
    conn.commit()
    conn.close()

    result = import_skill_package(pkg, **target_a)
    assert result["status"] == "already_installed"
    conn = db_mod.connect(path=target_a["db_path"])
    stored = conn.execute(
        "SELECT content_sha256 FROM skills WHERE name='media-generation'"
    ).fetchone()["content_sha256"]
    conn.close()
    assert stored == FROZEN["media-generation"]["sha256"]


def test_existing_registry_path_outside_explicit_target_is_rejected(tmp_path, target_a):
    pkg = os.path.join(OFFICIAL_PACKAGES_ROOT, "media-generation")
    outside = tmp_path / "prime-like" / "media-generation.md"
    outside.parent.mkdir()
    with open(os.path.join(pkg, "media-generation.md"), "rb") as source:
        outside.write_bytes(source.read())

    from core.skills import init_skills_db
    init_skills_db(db_path=target_a["db_path"])
    import core.db as db_mod
    conn = db_mod.connect(path=target_a["db_path"])
    conn.execute(
        "INSERT INTO skills "
        "(name, description, path, created_at, updated_at, origin, content_sha256) "
        "VALUES (?,?,?,?,?,'official',?)",
        ("media-generation", "outside", str(outside), "now", "now",
         FROZEN["media-generation"]["sha256"]),
    )
    conn.commit()
    conn.close()

    with pytest.raises(SkillTransportError, match="outside its explicit"):
        import_skill_package(pkg, **target_a)
    with pytest.raises(SkillTransportError, match="outside its explicit"):
        export_skill("media-generation", str(tmp_path / "exported"), **target_a)


def test_symlink_at_installed_target_is_rejected(tmp_path, target_a):
    pkg = os.path.join(OFFICIAL_PACKAGES_ROOT, "media-generation")
    outside = tmp_path / "outside.md"
    with open(os.path.join(pkg, "media-generation.md"), "rb") as source:
        outside.write_bytes(source.read())
    os.makedirs(target_a["skills_dir"])
    os.symlink(outside, os.path.join(target_a["skills_dir"], "media-generation.md"))
    with pytest.raises(PackageValidationError) as ei:
        import_skill_package(pkg, **target_a)
    assert ei.value.code == "target_unsafe"


def test_distinct_names_with_same_slug_fail_closed(tmp_path, target_a):
    content = b"# Skill: slug collision\n\nSame bytes.\n"
    first = _make_package(
        str(tmp_path / "first"), name="Case Skill", origin="official", content=content
    )
    second = _make_package(
        str(tmp_path / "second"), name="case-skill", origin="official", content=content
    )
    import_skill_package(first, **target_a)
    with pytest.raises(SkillCollisionError) as ei:
        import_skill_package(second, **target_a)
    assert ei.value.existing["name"] == "Case Skill"
    assert ei.value.incoming["name"] == "case-skill"


def test_atomic_write_fsyncs_file_then_publishes_then_fsyncs_directory(
        tmp_path, monkeypatch):
    import core.skill_transport as st
    events = []
    real_fsync = st.os.fsync
    real_replace = st.os.replace

    def traced_fsync(fd):
        mode = os.fstat(fd).st_mode
        events.append("fsync_dir" if stat.S_ISDIR(mode) else "fsync_file")
        return real_fsync(fd)

    def traced_replace(source, target):
        events.append("replace")
        return real_replace(source, target)

    monkeypatch.setattr(st.os, "fsync", traced_fsync)
    monkeypatch.setattr(st.os, "replace", traced_replace)
    st._atomic_write(str(tmp_path / "published.md"), b"durable bytes")
    assert events == ["fsync_file", "replace", "fsync_dir"]


def test_next_import_removes_only_stale_transporter_temp_residue(tmp_path, target_a):
    os.makedirs(target_a["skills_dir"])
    stale = os.path.join(target_a["skills_dir"], ".skill-import-crashed.tmp")
    unrelated = os.path.join(target_a["skills_dir"], ".unrelated.tmp")
    with open(stale, "wb") as handle:
        handle.write(b"partial untrusted bytes")
    with open(unrelated, "wb") as handle:
        handle.write(b"preserve me")

    pkg = _make_package(str(tmp_path / "pkg"), name="cleanup-specimen")
    import_skill_package(pkg, **target_a)

    assert not os.path.exists(stale)
    assert os.path.exists(unrelated)


def test_export_failure_before_manifest_publication_leaves_no_partial_package(
        tmp_path, target_a, monkeypatch):
    import core.skill_transport as st
    pkg = _make_package(str(tmp_path / "pkg"), name="export-crash")
    import_skill_package(pkg, **target_a)
    destination = tmp_path / "published-package"
    real_atomic_write = st._atomic_write
    calls = 0

    def fail_second_staged_write(path, data):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated death before manifest publication")
        return real_atomic_write(path, data)

    monkeypatch.setattr(st, "_atomic_write", fail_second_staged_write)
    with pytest.raises(OSError, match="before manifest"):
        export_skill("export-crash", str(destination), **target_a)

    assert not destination.exists()
    assert not list(tmp_path.glob(".skill-export-*"))


def test_two_simultaneous_imports_leave_one_coherent_identity(
        tmp_path, target_a, monkeypatch):
    import core.skill_transport as st
    pkg = _make_package(
        str(tmp_path / "pkg"), name="race-skill", origin="official",
        content=b"# Skill: race-skill\n\nApproved bytes.\n",
    )
    from core.skills import init_skills_db
    init_skills_db(db_path=target_a["db_path"])
    os.makedirs(target_a["skills_dir"])

    gate = threading.Barrier(2)

    def gated_atomic_write(path, data):
        directory = os.path.dirname(path)
        fd, temporary = tempfile.mkstemp(dir=directory, prefix=".race-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                gate.wait(timeout=0.25)
            except threading.BrokenBarrierError:
                pass
            os.replace(temporary, path)
            dir_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except BaseException:
            with contextlib.suppress(OSError):
                os.remove(temporary)
            raise

    monkeypatch.setattr(st, "_atomic_write", gated_atomic_write)
    outcomes = []
    outcome_lock = threading.Lock()

    def worker():
        try:
            result = import_skill_package(pkg, **target_a)
            outcome = ("ok", result["status"])
        except BaseException as exc:
            outcome = ("error", type(exc).__name__)
        with outcome_lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert sorted(outcomes) == [("ok", "already_installed"), ("ok", "installed")]
    target_path = os.path.join(target_a["skills_dir"], "race-skill.md")
    assert os.path.exists(target_path)

    import core.db as db_mod
    conn = db_mod.connect(path=target_a["db_path"])
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM skills WHERE name='race-skill'"
    ).fetchone()["c"]
    fts_count = conn.execute(
        "SELECT COUNT(*) AS c FROM skills_fts WHERE skills_fts MATCH 'race'"
    ).fetchone()["c"]
    conn.close()
    assert count == 1
    assert fts_count == 1


def test_official_import_racing_write_skill_fails_before_user_file_write(
        tmp_path, target_a, monkeypatch):
    import core.skill_transport as st
    package = os.path.join(OFFICIAL_PACKAGES_ROOT, "media-generation")
    monkeypatch.setattr(config, "DB_PATH", target_a["db_path"])
    monkeypatch.setattr(config, "BASE_DIR", str(tmp_path / "user-runtime"))
    from core.skills import init_skills_db
    init_skills_db(db_path=target_a["db_path"])

    official_written = threading.Event()
    release_import = threading.Event()
    real_atomic_write = st._atomic_write

    def paused_atomic_write(path, content):
        real_atomic_write(path, content)
        official_written.set()
        assert release_import.wait(timeout=10)

    monkeypatch.setattr(st, "_atomic_write", paused_atomic_write)
    outcomes = {}

    def importer():
        try:
            outcomes["import"] = import_skill_package(package, **target_a)["status"]
        except BaseException as exc:
            outcomes["import"] = type(exc).__name__

    def saver():
        try:
            write_skill("media-generation", "racing user", "attacker content")
            outcomes["save"] = "saved"
        except BaseException as exc:
            outcomes["save"] = type(exc).__name__

    import_thread = threading.Thread(target=importer)
    save_thread = threading.Thread(target=saver)
    import_thread.start()
    assert official_written.wait(timeout=10)
    save_thread.start()

    user_path = tmp_path / "user-runtime" / "skills" / "media-generation.md"
    assert not user_path.exists()
    release_import.set()
    import_thread.join(timeout=10)
    save_thread.join(timeout=10)

    assert not import_thread.is_alive()
    assert not save_thread.is_alive()
    assert outcomes == {
        "import": "installed",
        "save": "OfficialSkillOverwriteError",
    }
    assert not user_path.exists()


# ── ST-09 bounded payload / manifest law ────────────────────────────────────

@pytest.mark.parametrize("manifest_size", [65535, 65536])
def test_manifest_boundary_sizes_are_accepted(tmp_path, manifest_size):
    pkg = _make_package(str(tmp_path / "pkg"), content=b"bounded payload")
    _pad_manifest_to_exact_size(pkg, manifest_size)
    info = inspect_package(pkg)
    assert info["payload_bytes"] == len(b"bounded payload")


def test_manifest_one_byte_over_limit_is_rejected_before_read_or_parse(
        tmp_path, monkeypatch):
    import core.skill_transport as st
    pkg = _make_package(str(tmp_path / "pkg"), content=b"bounded payload")
    _pad_manifest_to_exact_size(pkg, MAX_PACKAGE_MANIFEST_BYTES + 1)
    read_calls = []
    real_read = st.os.read

    def traced_read(fd, size):
        read_calls.append((fd, size))
        return real_read(fd, size)

    monkeypatch.setattr(st.os, "read", traced_read)
    monkeypatch.setattr(
        st.json, "loads",
        lambda *args, **kwargs: pytest.fail("oversized manifest reached JSON parsing"),
    )
    with pytest.raises(PackageValidationError) as ei:
        validate_package(pkg)
    assert ei.value.code == "manifest_too_large"
    assert read_calls == []


@pytest.mark.parametrize("payload_size", [262143, 262144])
def test_payload_boundary_sizes_are_accepted_and_published(
        tmp_path, target_a, payload_size):
    content = b"P" * payload_size
    pkg = _make_package(
        str(tmp_path / "pkg"), name=f"boundary-{payload_size}", content=content
    )
    installed = import_skill_package(pkg, **target_a)
    assert installed["bytes"] == payload_size
    assert os.path.getsize(installed["path"]) == payload_size


def test_actual_payload_one_byte_over_limit_refuses_before_read_hash_or_mutation(
        tmp_path, target_a, monkeypatch):
    import core.skill_transport as st
    content = b"X" * (MAX_SKILL_PAYLOAD_BYTES + 1)
    pkg = _make_package(
        str(tmp_path / "pkg"), origin="official", content=content,
        manifest_overrides={"payload_bytes": 1},
    )
    payload_path = os.path.realpath(os.path.join(pkg, "demo-skill.md"))
    payload_reads = []
    real_read = st.os.read

    def traced_read(fd, size):
        if os.path.realpath(f"/proc/self/fd/{fd}") == payload_path:
            payload_reads.append(size)
        return real_read(fd, size)

    monkeypatch.setattr(st.os, "read", traced_read)
    monkeypatch.setattr(
        st.hashlib, "sha256",
        lambda *args, **kwargs: pytest.fail("oversized payload reached hashing"),
    )
    with pytest.raises(PackageValidationError) as ei:
        import_skill_package(pkg, **target_a)
    assert ei.value.code == "payload_too_large"
    assert payload_reads == []
    assert not os.path.exists(target_a["skills_dir"])
    assert not os.path.exists(target_a["db_path"])


def test_declared_oversize_actual_small_refuses_before_payload_read(
        tmp_path, target_a, monkeypatch):
    import core.skill_transport as st
    pkg = _make_package(
        str(tmp_path / "pkg"), content=b"small",
        manifest_overrides={"payload_bytes": MAX_SKILL_PAYLOAD_BYTES + 1},
    )
    payload_path = os.path.realpath(os.path.join(pkg, "demo-skill.md"))
    payload_reads = []
    real_read = st.os.read

    def traced_read(fd, size):
        if os.path.realpath(f"/proc/self/fd/{fd}") == payload_path:
            payload_reads.append(size)
        return real_read(fd, size)

    monkeypatch.setattr(st.os, "read", traced_read)
    with pytest.raises(PackageValidationError) as ei:
        import_skill_package(pkg, **target_a)
    assert ei.value.code == "declared_payload_too_large"
    assert payload_reads == []
    assert not os.path.exists(target_a["skills_dir"])
    assert not os.path.exists(target_a["db_path"])


def test_payload_growth_after_fstat_is_caught_by_bounded_read(
        tmp_path, target_a, monkeypatch):
    import core.skill_transport as st
    content = b"G" * MAX_SKILL_PAYLOAD_BYTES
    pkg = _make_package(str(tmp_path / "pkg"), content=content)
    payload_path = os.path.realpath(os.path.join(pkg, "demo-skill.md"))
    real_fstat = st.os.fstat
    grew = False

    def grow_after_fstat(fd):
        nonlocal grew
        observed = real_fstat(fd)
        if not grew and os.path.realpath(f"/proc/self/fd/{fd}") == payload_path:
            with open(payload_path, "ab") as handle:
                handle.write(b"!")
            grew = True
        return observed

    monkeypatch.setattr(st.os, "fstat", grow_after_fstat)
    with pytest.raises(PackageValidationError) as ei:
        import_skill_package(pkg, **target_a)
    assert ei.value.code == "payload_too_large"
    assert grew
    assert not os.path.exists(target_a["skills_dir"])
    assert not os.path.exists(target_a["db_path"])


def test_payload_shrink_after_fstat_fails_byte_count_without_mutation(
        tmp_path, target_a, monkeypatch):
    import core.skill_transport as st
    content = b"S" * 100
    pkg = _make_package(str(tmp_path / "pkg"), content=content)
    payload_path = os.path.realpath(os.path.join(pkg, "demo-skill.md"))
    real_fstat = st.os.fstat
    shrank = False

    def shrink_after_fstat(fd):
        nonlocal shrank
        observed = real_fstat(fd)
        if not shrank and os.path.realpath(f"/proc/self/fd/{fd}") == payload_path:
            os.truncate(payload_path, 99)
            shrank = True
        return observed

    monkeypatch.setattr(st.os, "fstat", shrink_after_fstat)
    with pytest.raises(PackageValidationError) as ei:
        import_skill_package(pkg, **target_a)
    assert ei.value.code == "byte_count_mismatch"
    assert shrank
    assert not os.path.exists(target_a["skills_dir"])
    assert not os.path.exists(target_a["db_path"])


def test_oversize_pre_policy_user_export_refuses_without_partial_package(
        tmp_path, target_a):
    from core.skills import init_skills_db
    import core.db as db_mod

    os.makedirs(target_a["skills_dir"])
    path = os.path.join(target_a["skills_dir"], "legacy-large.md")
    with open(path, "wb") as handle:
        handle.truncate(MAX_SKILL_PAYLOAD_BYTES + 1)
    init_skills_db(db_path=target_a["db_path"])
    conn = db_mod.connect(path=target_a["db_path"])
    conn.execute(
        "INSERT INTO skills "
        "(name, description, path, created_at, updated_at, origin, content_sha256) "
        "VALUES (?,?,?,?,?,'user',NULL)",
        ("legacy-large", "pre-policy oversized user skill", path, "now", "now"),
    )
    conn.commit()
    conn.close()

    destination = tmp_path / "exported"
    with pytest.raises(PackageValidationError) as ei:
        export_skill("legacy-large", str(destination), **target_a)
    assert ei.value.code == "payload_too_large"
    assert not destination.exists()


def test_64_mib_payload_attack_performs_zero_payload_reads_and_zero_hashes(
        tmp_path, target_a, monkeypatch):
    import core.skill_transport as st
    pkg_dir = tmp_path / "huge-package"
    pkg_dir.mkdir()
    payload_path = pkg_dir / "huge.md"
    with open(payload_path, "wb") as handle:
        handle.truncate(64 * 1024 * 1024)
    manifest = {
        "schema_version": 1,
        "name": "huge-skill",
        "description": "64 MiB bounded-read attack",
        "origin": "official",
        "payload_filename": "huge.md",
        "payload_bytes": 1,
        "payload_sha256": "0" * 64,
    }
    (pkg_dir / "manifest.json").write_text(json.dumps(manifest))

    payload_realpath = os.path.realpath(payload_path)
    payload_reads = []
    real_read = st.os.read

    def traced_read(fd, size):
        if os.path.realpath(f"/proc/self/fd/{fd}") == payload_realpath:
            payload_reads.append(size)
        return real_read(fd, size)

    monkeypatch.setattr(st.os, "read", traced_read)
    monkeypatch.setattr(
        st.hashlib, "sha256",
        lambda *args, **kwargs: pytest.fail("64 MiB payload reached hashing"),
    )

    with pytest.raises(PackageValidationError) as ei:
        import_skill_package(str(pkg_dir), **target_a)
    assert ei.value.code == "payload_too_large"
    assert payload_reads == []
    assert not os.path.exists(target_a["skills_dir"])
    assert not os.path.exists(target_a["db_path"])


def test_package_rejects_hidden_or_duplicate_payload_entries(tmp_path):
    pkg = _make_package(str(tmp_path / "pkg"), content=b"one admitted payload")
    with open(os.path.join(pkg, ".hidden-replacement.md"), "wb") as handle:
        handle.write(b"second payload")
    with pytest.raises(PackageValidationError) as ei:
        validate_package(pkg)
    assert ei.value.code == "package_shape"


def test_write_skill_rejects_oversize_before_filesystem_or_db_mutation(
        tmp_path, monkeypatch):
    runtime_root = tmp_path / "runtime"
    db_path = tmp_path / "state" / "lumina.db"
    monkeypatch.setattr(config, "BASE_DIR", str(runtime_root))
    monkeypatch.setattr(config, "DB_PATH", str(db_path))
    content = "# Skill: too-large\n" + ("W" * MAX_SKILL_PAYLOAD_BYTES)

    with pytest.raises(SkillPayloadPolicyError, match="maximum"):
        write_skill("too-large", "must not publish", content)

    assert not runtime_root.exists()
    assert not db_path.exists()


def test_runtime_user_recall_refuses_64_mib_before_payload_read(
        tmp_path, target_a, monkeypatch):
    import core.db as db_mod
    import core.skill_transport as st
    import core.skills as skills_mod
    from core.skills import init_skills_db

    os.makedirs(target_a["skills_dir"])
    path = os.path.join(target_a["skills_dir"], "legacy-huge.md")
    with open(path, "wb") as handle:
        handle.truncate(64 * 1024 * 1024)
    init_skills_db(db_path=target_a["db_path"])
    conn = db_mod.connect(path=target_a["db_path"])
    conn.execute(
        "INSERT INTO skills "
        "(name, description, path, created_at, updated_at, origin, content_sha256) "
        "VALUES (?,?,?,?,?,'user',NULL)",
        ("legacy-huge", "pre-policy huge user skill", path, "now", "now"),
    )
    conn.commit()
    conn.close()

    payload_reads = []
    real_read = st.os.read

    def traced_read(fd, size):
        if os.path.realpath(f"/proc/self/fd/{fd}") == os.path.realpath(path):
            payload_reads.append(size)
        return real_read(fd, size)

    monkeypatch.setattr(config, "DB_PATH", target_a["db_path"])
    monkeypatch.setattr(st.os, "read", traced_read)
    with pytest.raises(SkillPayloadPolicyError, match="maximum is 262144"):
        skills_mod.load_skill("legacy-huge")
    assert payload_reads == []


def test_runtime_official_recall_refuses_64_mib_before_read_or_hash(
        tmp_path, target_a, monkeypatch):
    import core.skill_transport as st
    import core.skills as skills_mod

    installed = {
        item["name"]: item for item in bootstrap_official_skills(**target_a)
    }["media-generation"]
    with open(installed["path"], "wb") as handle:
        handle.truncate(64 * 1024 * 1024)

    payload_reads = []
    real_read = st.os.read

    def traced_read(fd, size):
        if os.path.realpath(f"/proc/self/fd/{fd}") == os.path.realpath(installed["path"]):
            payload_reads.append(size)
        return real_read(fd, size)

    monkeypatch.setattr(config, "DB_PATH", target_a["db_path"])
    monkeypatch.setattr(st.os, "read", traced_read)
    monkeypatch.setattr(
        skills_mod.hashlib, "sha256",
        lambda *args, **kwargs: pytest.fail("oversized runtime payload reached hashing"),
    )
    with pytest.raises(OfficialSkillIntegrityError, match="maximum is 262144"):
        skills_mod.load_skill("media-generation")
    assert payload_reads == []


def test_idempotent_restart_refuses_64_mib_installed_drift_before_read(
        tmp_path, target_a, monkeypatch):
    import core.skill_transport as st

    package = os.path.join(OFFICIAL_PACKAGES_ROOT, "media-generation")
    installed = import_skill_package(package, **target_a)
    with open(installed["path"], "wb") as handle:
        handle.truncate(64 * 1024 * 1024)

    payload_reads = []
    real_read = st.os.read

    def traced_read(fd, size):
        if os.path.realpath(f"/proc/self/fd/{fd}") == os.path.realpath(installed["path"]):
            payload_reads.append(size)
        return real_read(fd, size)

    monkeypatch.setattr(st.os, "read", traced_read)
    with pytest.raises(PackageValidationError) as ei:
        import_skill_package(package, **target_a)
    assert ei.value.code == "payload_too_large"
    assert payload_reads == []
