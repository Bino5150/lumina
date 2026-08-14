"""eval/run_eval.py -- regression coverage for the per-task persona path
added alongside the T22/S60 self-disclosure fix (LUMINA_HANDOFF_S60.md
Section 6: "run_eval.py's run_task() never passed a persona to any of the
existing 21 tasks -- every eval task ran against bare config.SYSTEM_PROMPT,
no persona layered on"). That gap was fixed in run_eval.py, but no test
ever exercised the fix itself -- this file closes that.

Subprocess, not direct import, for the same reason
tests/test_eval_scratch_dir.py already uses subprocess for run_eval.py:
importing eval/run_eval.py mutates os.environ["LUMINA_DATA_DIR"] and wipes
eval/_scratch_data/ at module level (by design, per its own CRITICAL
comment) -- doing that inside this test process would leak into every
other test module that runs in the same pytest session.
"""
import os
import subprocess
import sys
import textwrap

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Driver script run in an isolated subprocess: imports the real run_eval.py
# (so its actual module-level side effects and run_task() logic both run
# for real, in a throwaway process), stubs out only the two calls that
# would otherwise need a live LLM backend / real persona files, then calls
# run_task() and reports what it observed via stdout markers the test
# process parses.
_DRIVER = textwrap.dedent("""
    import sys, json
    sys.path.insert(0, {repo_root!r})
    sys.path.insert(0, {eval_dir!r})
    import run_eval
    import core.personas as personas_mod

    captured = {{}}

    def fake_run_headless_turn(**kwargs):
        captured['persona'] = kwargs.get('persona')
        captured['task'] = kwargs.get('task')
        captured['owner'] = kwargs.get('owner')
        return {{"success": True, "response": "stub", "tool_calls": [],
                 "available_tools": [], "trace": True}}

    def fake_reset_headless_agent(channel_id):
        captured['reset_channel'] = channel_id

    run_eval.run_headless_turn = fake_run_headless_turn
    run_eval.reset_headless_agent = fake_reset_headless_agent

    {persona_stub}

    task = {task!r}
    result = run_eval.run_task(task)
    captured['result_task_id'] = result['task_id']
    print("CAPTURED_JSON:" + json.dumps({{
        k: v for k, v in captured.items() if k != 'persona'
    }}))
    print("PERSONA_NAME:" + str(captured['persona'].get('name') if captured['persona'] else None))
""")


def _run_driver(persona_stub: str, task: dict) -> dict:
    script = _DRIVER.format(
        repo_root=REPO_ROOT,
        eval_dir=os.path.join(REPO_ROOT, "eval"),
        persona_stub=persona_stub,
        task=task,
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        f"driver subprocess failed\\nstdout: {result.stdout}\\nstderr: {result.stderr}"
    )
    lines = {}
    for line in result.stdout.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            lines[key] = val
    return {"stdout": result.stdout, "stderr": result.stderr, "lines": lines}


def test_run_task_resolves_and_threads_named_persona():
    """task['persona'] = 'Lumina' -> run_task() must resolve it via
    find_persona_by_name() and pass the resolved persona dict (not just the
    name) into run_headless_turn()."""
    persona_stub = textwrap.dedent("""
        FAKE_PERSONA = {"name": "Lumina", "system_prompt": "stub persona prompt"}
        def fake_find_persona_by_name(name):
            assert name == "Lumina", f"unexpected persona name: {name!r}"
            return FAKE_PERSONA
        personas_mod.find_persona_by_name = fake_find_persona_by_name
    """)
    task = {"id": "T22", "prompt": "hello", "persona": "Lumina"}

    out = _run_driver(persona_stub, task)
    assert out["lines"].get("PERSONA_NAME") == "Lumina", out["stdout"] + out["stderr"]
    assert out["lines"].get("CAPTURED_JSON")
    import json
    captured = json.loads(out["lines"]["CAPTURED_JSON"])
    assert captured["task"] == "hello"
    assert captured["result_task_id"] == "T22"
    assert captured["reset_channel"] == "eval_T22"


def test_run_task_without_persona_key_passes_none_unchanged():
    """Regression guard: tasks that omit 'persona' entirely (all 21 prior
    tasks) must keep running with persona=None -- the exact 'prior behavior
    unaffected' backward-compatibility claim from the S60 handoff."""
    persona_stub = textwrap.dedent("""
        def fake_find_persona_by_name(name):
            raise AssertionError("find_persona_by_name should not be called when no persona key is set")
        personas_mod.find_persona_by_name = fake_find_persona_by_name
    """)
    task = {"id": "T01", "prompt": "What's the capital of France?"}

    out = _run_driver(persona_stub, task)
    assert out["lines"].get("PERSONA_NAME") == "None", out["stdout"] + out["stderr"]


def test_run_task_unknown_persona_name_falls_back_gracefully():
    """Persona name not found (typo, deleted persona file, etc.) -> must not
    raise. Falls back to persona=None (bare config.SYSTEM_PROMPT), same as
    the no-persona-key case, rather than crashing the whole eval run."""
    persona_stub = textwrap.dedent("""
        def fake_find_persona_by_name(name):
            return None
        personas_mod.find_persona_by_name = fake_find_persona_by_name
    """)
    task = {"id": "T22", "prompt": "hello", "persona": "DoesNotExist"}

    out = _run_driver(persona_stub, task)
    assert out["lines"].get("PERSONA_NAME") == "None", out["stdout"] + out["stderr"]
