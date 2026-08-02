"""MB-23 (LUMINA_HANDOFF_S46.md) -- eval/_scratch_data/ used to be assumed
clean rather than made clean: a stale prefs.json left over from an old S30
debugging session silently contaminated multiple eval runs, including two
that were already being cited as real results, until someone noticed
"Bino" appearing unprompted in a T03 response. A one-time `rm -rf` was
applied as a stopgap that session; this is the structural fix.

Two layers of test here:
  1. Pure unit tests against eval/scratch_dir.reset_scratch_dir() directly
     -- fast, no subprocess, no project imports.
  2. A subprocess-based integration test against the *real* run_eval.py --
     proves the actual wiring works, not just the helper in isolation.
     Subprocess, not direct import, because run_eval.py mutates
     os.environ["LUMINA_DATA_DIR"] at module level (by design, per its own
     CRITICAL comment) -- importing it directly inside this test process
     would leak that into the rest of the test session.
"""
import os
import subprocess
import sys

from eval.scratch_dir import reset_scratch_dir


# ── Unit tests: reset_scratch_dir() in isolation ─────────────────────────

def test_wipes_existing_contaminated_directory(tmp_path):
    scratch = tmp_path / "_scratch_data"
    memory_dir = scratch / "memory"
    memory_dir.mkdir(parents=True)
    stale_prefs = memory_dir / "prefs.json"
    stale_prefs.write_text('{"human_bio": "leftover from an old debugging session"}')

    reset_scratch_dir(str(scratch))

    assert scratch.exists()
    assert not stale_prefs.exists()
    assert list(scratch.iterdir()) == []


def test_does_not_crash_when_directory_does_not_exist_yet(tmp_path):
    """First-ever run on a fresh machine -- _scratch_data/ doesn't exist at
    all yet. Must not raise."""
    scratch = tmp_path / "_scratch_data"
    assert not scratch.exists()

    reset_scratch_dir(str(scratch))

    assert scratch.exists()
    assert list(scratch.iterdir()) == []


def test_leaves_directory_empty_and_ready_for_downstream_creation(tmp_path):
    """Downstream code (config.py / persistence init) is responsible for
    creating its own subdirectories (memory/, etc.) inside this -- this
    function's only job is guaranteeing the top-level dir starts empty."""
    scratch = tmp_path / "_scratch_data"
    reset_scratch_dir(str(scratch))
    assert os.path.isdir(scratch)
    assert len(os.listdir(scratch)) == 0


# ── Integration test: the real run_eval.py, via subprocess ──────────────

def test_run_eval_wipes_stale_scratch_dir_before_any_project_import():
    """Runs the actual eval/run_eval.py (not a copy, not a mock) with
    --help. argparse's --help exits inside main(), but the scratch-dir wipe
    happens at module level, before main() is even defined -- so by the
    time this process exits, the wipe has already run regardless of
    whether main() itself ever gets called. No real LLM backend needed:
    the module-level import chain (through core.headless) is the same one
    the rest of this test suite already exercises successfully."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    scratch = os.path.join(repo_root, "eval", "_scratch_data")
    memory_dir = os.path.join(scratch, "memory")
    os.makedirs(memory_dir, exist_ok=True)
    stale_prefs = os.path.join(memory_dir, "prefs.json")
    with open(stale_prefs, "w") as f:
        f.write('{"human_bio": "leftover from an old debugging session"}')

    try:
        result = subprocess.run(
            [sys.executable, os.path.join(repo_root, "eval", "run_eval.py"), "--help"],
            cwd=repo_root, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert not os.path.exists(stale_prefs), (
            "stale prefs.json survived -- the wipe either didn't run or "
            "ran after something already read the contaminated directory"
        )
    finally:
        # Leave the real repo's eval/_scratch_data/ in the same wiped-but-
        # present state a real run would -- nothing left to clean up here,
        # since the wipe itself already did that.
        pass
