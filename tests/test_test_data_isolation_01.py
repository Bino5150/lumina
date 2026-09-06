"""TEST-DATA-ISOLATION-01 -- proves and locks in the fix for a real
contamination incident: the owner's live prefs.json was found holding
"- Did a thing" under human_profile_curated -- the canned fixture text used
by tests/test_dreaming.py, tests/test_emergency_stop_maintenance.py and
tests/test_settings_human_profile_curation_toggle.py's fake dream-sweep
backends. Root cause (confirmed by direct inspection, not narration):

    config.DATA_DIR = os.environ.get("LUMINA_DATA_DIR") or user_data_dir(...)

is computed ONCE, at config.py's own import time, and every downstream
*_PATH/*_DIR constant in the codebase (core/persistence.py's PREFS_PATH,
core/db.py's DB_PATH behind chat/Palace/Knowledge Base/Skills/checkpoints,
core/flight_recorder.py, core/idempotency.py, tools/projects.py,
tools/toolmaker.py, tools/pending_actions.py) derives from it the same way.
tests/conftest.py had no fixture setting LUMINA_DATA_DIR at all -- only
per-test-file ad hoc `monkeypatch.setattr(persistence, "PREFS_PATH", ...)`,
present in some test files and absent from others -- so any test exercising
the real persistence/dreaming code path without remembering that monkeypatch
wrote straight into the owner's real ~/.local/share/lumina/memory/prefs.json.
This is the same bug class the eval harness already hit once and fixed
structurally (eval/run_eval.py, eval/scratch_dir.py, LUMINA_HANDOFF_S46.md)
-- pytest had no equivalent guard until now.

These tests prove both layers of the fix: the structural one
(tests/conftest.py setting LUMINA_DATA_DIR/LUMINA_SECRETS_PATH before any
project import) and the fail-closed backstop (core/test_isolation.py, wired
into persistence.py/secrets.py/db.py).
"""
import json
import os
import subprocess
import sys

import pytest

import config
import core.db as db
import core.dreaming as dreaming
import core.persistence as persistence
import core.secrets as secrets_module


def _real_prefs_path() -> str:
    from platformdirs import user_data_dir

    return os.path.join(user_data_dir("lumina", appauthor=False), "memory", "prefs.json")


def _real_secrets_path() -> str:
    return os.path.expanduser("~/.config/lumina/credentials.json")


def _real_data_dir() -> str:
    from platformdirs import user_data_dir

    return user_data_dir("lumina", appauthor=False)


# ── R1/R3-R7/R14: structural proof -- every shared path resolver lands ──
# ── outside the owner's real data directories during a normal test run ──


def test_data_dir_resolves_outside_real_owner_data_dir():
    real = os.path.realpath(_real_data_dir())
    current = os.path.realpath(config.DATA_DIR)
    assert current != real
    assert not current.startswith(real + os.sep)


def test_prefs_path_resolves_outside_real_owner_data_dir():
    real = os.path.realpath(_real_prefs_path())
    assert os.path.realpath(persistence.PREFS_PATH) != real


def test_secrets_path_resolves_outside_real_owner_config_dir():
    real = os.path.realpath(_real_secrets_path())
    assert os.path.realpath(secrets_module.SECRETS_PATH) != real


def test_db_path_resolves_outside_real_owner_data_dir():
    """config.DB_PATH is the single shared sqlite file behind chat history,
    Memory Palace, Knowledge Base, Skills, and coding/context checkpoints
    (core/db.py's own docstring: all six former hand-rolled connect() call
    sites now go through this one factory) -- one check here covers all of
    them at once."""
    real = os.path.realpath(os.path.join(_real_data_dir(), "memory", "lumina.db"))
    assert os.path.realpath(config.DB_PATH) != real


# ── R2/R10: the literal incident, reproduced end-to-end ──────────────────


class _FakeDreamBackend:
    def complete_utility(self, prompt, prefill="", max_tokens=500, temperature=0.3):
        return "- Did a thing"


def test_dream_sweep_fake_backend_contamination_stays_in_temp_root(monkeypatch):
    """The exact live incident, deliberately reproduced: dream-sweep curation
    enabled, a fake backend returning the canned fixture text, run through
    the real on_session_idle() -> curate_human_profile() -> persistence.save()
    path -- with NO manual PREFS_PATH monkeypatch in this test at all, relying
    solely on tests/conftest.py's global isolation. Proves both halves:
    the isolated prefs.json changes, and the owner's real prefs.json does not."""
    monkeypatch.setattr(dreaming.config, "DREAM_SWEEP_ENABLED", True)
    monkeypatch.setattr(dreaming.config, "DREAM_MIN_TOKENS", 1)
    monkeypatch.setattr(dreaming.config, "HUMAN_PROFILE_CURATION_ENABLED", True)
    monkeypatch.setattr(
        dreaming, "load_chat_messages",
        lambda cid: [{"role": "user", "content": "x" * 50, "created_at": "2026-07-09T00:00:00"}],
    )
    monkeypatch.setattr(dreaming, "get_llm_backend", lambda: _FakeDreamBackend())
    monkeypatch.setattr(dreaming, "palace_store", lambda **kw: None)
    dreaming._last_dream_sweep.pop(424242, None)

    real_prefs_path = _real_prefs_path()
    real_before = None
    if os.path.exists(real_prefs_path):
        with open(real_prefs_path) as f:
            real_before = f.read()

    dreaming.on_session_idle(chat_id=424242)

    # Isolated (session temp-root) prefs.json got the fake content.
    assert persistence.load()["human_profile_curated"] == "- Did a thing"
    # The owner's real prefs.json -- a completely different path than
    # persistence.PREFS_PATH by this point -- is byte-for-byte untouched.
    # Deliberately NOT asserting "- Did a thing" is absent from the real
    # file: on this machine the original incident's contamination is still
    # live in human_profile_curated (never actually repaired, despite the
    # ticket's claim otherwise -- see the completion report). The only
    # thing this test can safely prove is that running it changes nothing
    # about that real file, not what its pre-existing content is.
    if real_before is None:
        assert not os.path.exists(real_prefs_path)
    else:
        with open(real_prefs_path) as f:
            real_after = f.read()
        assert real_after == real_before


# ── M2/M3/M6-equivalent: fail-closed backstop when a path is forced back ──
# ── toward the real owner root, regardless of how it got there ──────────


def test_forced_production_prefs_save_fails_closed(monkeypatch):
    monkeypatch.setattr(persistence, "PREFS_PATH", _real_prefs_path())
    with pytest.raises(RuntimeError, match="TEST-ISOLATION"):
        persistence.save({"evil": "value"})


def test_forced_production_prefs_load_fails_closed(monkeypatch):
    monkeypatch.setattr(persistence, "PREFS_PATH", _real_prefs_path())
    with pytest.raises(RuntimeError, match="TEST-ISOLATION"):
        persistence.load()


def test_forced_production_secrets_save_fails_closed(monkeypatch):
    monkeypatch.setattr(secrets_module, "SECRETS_PATH", _real_secrets_path())
    with pytest.raises(RuntimeError, match="TEST-ISOLATION"):
        secrets_module.set_secret("evil", "value")


def test_forced_production_db_connect_fails_closed():
    real_db = os.path.join(_real_data_dir(), "memory", "lumina.db")
    with pytest.raises(RuntimeError, match="TEST-ISOLATION"):
        db.connect(path=real_db)


def test_symlinked_root_toward_production_fails_closed(tmp_path, monkeypatch):
    """M6: a test root that itself symlinks into the real owner data dir
    must not slip past a plain string-prefix check -- the guard resolves
    realpath() before comparing."""
    real_data_dir = _real_data_dir()
    if not os.path.isdir(real_data_dir):
        pytest.skip("real owner data dir does not exist on this machine")
    fake_root = tmp_path / "escape-via-symlink"
    os.symlink(real_data_dir, fake_root)
    monkeypatch.setattr(persistence, "PREFS_PATH", str(fake_root / "memory" / "prefs.json"))
    with pytest.raises(RuntimeError, match="TEST-ISOLATION"):
        persistence.save({"evil": "via-symlink"})


# ── Guard scoping: never active outside of tests ─────────────────────────


def test_guard_is_inactive_when_not_under_test(monkeypatch):
    """The real desktop app must never trip this guard -- it only fires
    while tests/conftest.py's LUMINA_TESTING=1 sentinel is set."""
    from core.test_isolation import refuse_if_production_path

    monkeypatch.delenv("LUMINA_TESTING", raising=False)
    real = _real_prefs_path()
    refuse_if_production_path(real)  # must not raise


# ── M1: prove the ORIGINAL vulnerability is real, not hypothetical ───────


def test_original_vulnerability_reproduced_without_isolation_env():
    """Runs a fresh subprocess with LUMINA_DATA_DIR/LUMINA_SECRETS_PATH/
    LUMINA_TESTING all stripped -- i.e. tests/conftest.py's fix removed --
    and confirms config.DATA_DIR really does resolve to the owner's real
    data directory in that state. This is the guard-RED half of the M1
    mutation proof: without the isolation this suite now provides, any
    persistence write during a test run would land exactly here."""
    env = dict(os.environ)
    for key in ("LUMINA_DATA_DIR", "LUMINA_SECRETS_PATH", "LUMINA_TESTING"):
        env.pop(key, None)
    result = subprocess.run(
        [sys.executable, "-c",
         "import config; print(config.DATA_DIR)"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    from platformdirs import user_data_dir
    assert result.stdout.strip() == user_data_dir("lumina", appauthor=False)


def test_migrate_legacy_state_skipped_under_lumina_testing():
    """config.py's own migrate_legacy_state() call (a shutil.move, not a
    copy, of any legacy in-repo memory/lumina.db-style file into DATA_DIR)
    must never run against a throwaway test data dir -- a fresh test root
    always looks "unmigrated," which would relocate (and delete from the
    real repo tree) any legacy file that happened to exist. Verified via a
    fresh subprocess with a stub migrate_state_dir module so this doesn't
    depend on any legacy file actually existing on this machine."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    probe = (
        "import sys, types, os, json\n"
        "calls = []\n"
        "stub = types.ModuleType('migrate_state_dir')\n"
        "def fake_migrate(base_dir, data_dir):\n"
        "    calls.append((base_dir, data_dir))\n"
        "    return []\n"
        "stub.migrate_legacy_state = fake_migrate\n"
        "sys.modules['migrate_state_dir'] = stub\n"
        "import config\n"
        "print(json.dumps(len(calls)))\n"
    )

    def _run(env_extra):
        env = dict(os.environ)
        env.pop("LUMINA_TESTING", None)
        env.update(env_extra)
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=repo_root, env=env, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout.strip())

    assert _run({"LUMINA_TESTING": "1", "LUMINA_DATA_DIR": "/tmp/does-not-matter"}) == 0
    assert _run({"LUMINA_DATA_DIR": "/tmp/does-not-matter"}) == 1
