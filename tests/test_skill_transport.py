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

import hashlib
import json
import os
import subprocess
import sys
import textwrap

import pytest

import config
from core.skill_transport import (
    PackageValidationError,
    SkillCollisionError,
    SkillTransportError,
    bootstrap_official_skills,
    export_skill,
    import_skill_package,
    inspect_package,
    validate_package,
)
from core.skills import OfficialSkillOverwriteError, write_skill


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
