"""
SKILLS-IMPORT-EXPORT-PORTABILITY-01 FINAL CLOSURE -- startup-level proof for
core.skill_transport.ensure_official_skills_bootstrapped(), the real-process
entry point wired into main.py's run_cli()/run_gui() and
core/headless.py's get_headless_agent() (never LuminaAgent.__init__ itself
-- see that function's own docstring for why).

Every test here monkeypatches config.DATA_DIR/config.DB_PATH to a brand-new
tmp_path tree -- simulating "fresh release code + a fresh empty release-local
LUMINA_DATA_DIR" exactly, the campaign's own fresh-release acceptance
invariant, at the actual function ordinary startup calls.
"""

import hashlib
import os
import subprocess
import sys
import textwrap

import pytest

import config
from core.skill_transport import ensure_official_skills_bootstrapped


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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


@pytest.fixture
def fresh_release_root(tmp_path, monkeypatch):
    """'Fresh release code + a fresh empty release-local LUMINA_DATA_DIR':
    a brand-new, never-before-used tmp_path tree, exactly what a first-ever
    run against an empty data root looks like. config.DATA_DIR/config.DB_PATH
    are read fresh at call time (never captured at import time -- same
    convention tests/test_settings_skills_tab.py already documents for
    BASE_DIR/DB_PATH), so overriding them here is sufficient."""
    data_dir = str(tmp_path / "release_data_root")
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DB_PATH", os.path.join(data_dir, "memory", "lumina.db"))
    return data_dir


# ── Ordinary startup -> both OFFICIAL skills available ──────────────────────

def test_ordinary_startup_bootstraps_both_official_skills(fresh_release_root):
    results = ensure_official_skills_bootstrapped()
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


def test_bootstrap_never_writes_into_the_source_checkout(fresh_release_root):
    """Explicit proof the chosen architecture (installed OFFICIAL skills
    live under config.DATA_DIR, not config.BASE_DIR's tracked skills/)
    means ordinary startup never mutates the source checkout."""
    tracked_skills_dir = os.path.join(REPO_ROOT, "skills")
    before = set(os.listdir(tracked_skills_dir))

    ensure_official_skills_bootstrapped()

    after = set(os.listdir(tracked_skills_dir))
    assert before == after, "ordinary startup must never write into the tracked skills/ directory"

    installed_dir = os.path.join(fresh_release_root, "skills", "official")
    installed_files = set(os.listdir(installed_dir))
    assert "media-generation.md" in installed_files
    assert "generated-artifact-manifest.md" in installed_files


def test_startup_bootstrap_is_idempotent(fresh_release_root):
    first = ensure_official_skills_bootstrapped()
    second = ensure_official_skills_bootstrapped()
    assert {r["name"]: r["status"] for r in first} == {
        "media-generation": "installed", "generated-artifact-manifest": "installed",
    }
    assert {r["name"]: r["status"] for r in second} == {
        "media-generation": "already_installed", "generated-artifact-manifest": "already_installed",
    }

    import core.db as db_mod
    conn = db_mod.connect(path=config.DB_PATH)
    counts = {
        r["name"]: r["c"] for r in conn.execute(
            "SELECT name, COUNT(*) c FROM skills GROUP BY name"
        ).fetchall()
    }
    conn.close()
    assert counts == {"media-generation": 1, "generated-artifact-manifest": 1}


def test_fresh_release_discovery_through_real_native_api(fresh_release_root):
    ensure_official_skills_bootstrapped()

    import core.skills as skills_mod
    hits = skills_mod.search_skills("image generation")
    assert any(h["name"] == "media-generation" for h in hits)

    content = skills_mod.load_skill("generated-artifact-manifest")
    assert content is not None
    assert hashlib.sha256(content.encode("utf-8")).hexdigest() == \
        FROZEN["generated-artifact-manifest"]["sha256"]

    all_skills = skills_mod.list_skills()
    officials = {s["name"]: s["origin"] for s in all_skills}
    assert officials == {"media-generation": "official", "generated-artifact-manifest": "official"}


# ── Restart in a NEW PROCESS against the same isolated target ───────────────

def test_new_process_restart_no_duplicate_no_rewrite_discovery_survives(fresh_release_root):
    first = ensure_official_skills_bootstrapped()
    by_name = {r["name"]: r for r in first}
    with open(by_name["media-generation"]["path"], "rb") as f:
        expected_media_gen = f.read()

    script = os.path.join(fresh_release_root, "..", "restart_check.py")
    script = os.path.abspath(script)
    with open(script, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(f"""
            import sys, hashlib
            sys.path.insert(0, {REPO_ROOT!r})
            import config
            config.DATA_DIR = {fresh_release_root!r}
            config.DB_PATH = {config.DB_PATH!r}

            from core.skill_transport import ensure_official_skills_bootstrapped
            import core.skills as skills_mod

            results = ensure_official_skills_bootstrapped()
            by_name = {{r["name"]: r for r in results}}
            assert by_name["media-generation"]["status"] == "already_installed", by_name
            assert by_name["generated-artifact-manifest"]["status"] == "already_installed", by_name

            with open(by_name["media-generation"]["path"], "rb") as fh:
                content = fh.read()
            assert content == {expected_media_gen!r}
            assert hashlib.sha256(content).hexdigest() == {FROZEN["media-generation"]["sha256"]!r}

            hits = skills_mod.search_skills("image generation")
            assert any(h["name"] == "media-generation" for h in hits), hits

            import core.db as db_mod
            conn = db_mod.connect(path=config.DB_PATH)
            n = conn.execute("SELECT COUNT(*) c FROM skills").fetchone()["c"]
            conn.close()
            assert n == 2, n  # no duplicate registration across the restart

            print("STARTUP_RESTART_OK")
        """))

    env = dict(os.environ)
    result = subprocess.run([sys.executable, script], capture_output=True, text=True, env=env)
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "STARTUP_RESTART_OK" in result.stdout


# ── Production-path refusal under LUMINA_TESTING ─────────────────────────────

def test_startup_under_lumina_testing_cannot_target_production_paths(monkeypatch):
    """The test-isolation guard (core/test_isolation.py) is reached through
    core.db.connect() -> deliberately NOT swallowed by
    ensure_official_skills_bootstrapped()'s fail-safe except clauses (it
    only catches SkillTransportError/OSError) -- a real safety-guard
    violation must stay loud, not be quietly absorbed into "continuing
    without it"."""
    from platformdirs import user_data_dir
    real_data_dir = user_data_dir("lumina", appauthor=False)
    monkeypatch.setattr(config, "DATA_DIR", real_data_dir)
    monkeypatch.setattr(config, "DB_PATH", os.path.join(real_data_dir, "memory", "lumina.db"))

    with pytest.raises(RuntimeError, match="TEST-ISOLATION"):
        ensure_official_skills_bootstrapped()


def test_agent_construction_never_triggers_official_bootstrap(fresh_release_root, monkeypatch):
    """Confirms the deliberate design choice: register_skills_tools() --
    the exact skills-registration seam every LuminaAgent.__init__ call
    goes through, including in tests with their own strict skill-count
    assertions -- never installs the two OFFICIAL skills as a side effect.
    Only the three explicit real-startup call sites
    (ensure_official_skills_bootstrapped(), wired into main.py's
    run_cli()/run_gui() and core/headless.py's get_headless_agent()) do."""
    monkeypatch.setattr(config, "BASE_DIR", fresh_release_root)  # isolate write_skill()'s target too

    from core.skills import register_skills_tools
    from tools.registry import ToolRegistry
    registry = ToolRegistry()
    register_skills_tools(registry)

    import core.skills as skills_mod
    assert skills_mod.list_skills() == []
