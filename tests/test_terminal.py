"""F-37 (terminal half): tools/terminal.py — timeout kills the whole
process group, not just the immediate shell."""
import os
import time
import subprocess
import pytest
from tools.terminal import register_terminal_tools
from core.project_context import ProjectContext, ProjectContextState


class _CapturingRegistry:
    def __init__(self):
        self.fns = {}

    def register(self, name, fn, description, parameters):
        self.fns[name] = fn


@pytest.fixture
def run_command():
    reg = _CapturingRegistry()
    register_terminal_tools(reg)
    return reg.fns["run_command"]


def test_normal_command(run_command):
    result = run_command("echo hello")
    assert "hello" in result


def test_stderr_captured(run_command):
    result = run_command("echo oops 1>&2")
    assert "oops" in result


def test_exit_code_reported(run_command):
    result = run_command("exit 3")
    assert "exit code: 3" in result


def test_timeout_enforced(run_command):
    start = time.time()
    result = run_command("sleep 10", timeout=1)
    elapsed = time.time() - start
    assert "timed out" in result
    assert elapsed < 5


def test_timeout_kills_backgrounded_children(run_command):
    """The specific bug this hardening fixes: the old subprocess.run(timeout=)
    only killed the direct shell, leaving a backgrounded child running."""
    run_command("sleep 8 & echo parent-done", timeout=1)
    time.sleep(0.5)
    leftover = subprocess.run(["pgrep", "-f", "sleep 8"], capture_output=True, text=True).stdout
    assert leftover.strip() == ""


# ── CODING-02B-A: project-aware default cwd ──────────────────────────────

def _run_command_with_state(project_state=None):
    reg = _CapturingRegistry()
    register_terminal_tools(reg, project_state=project_state)
    return reg.fns["run_command"]


def _active_state(root):
    state = ProjectContextState()
    state.set(ProjectContext(name="p", root=str(root)))
    return state


def test_no_cwd_with_active_project_defaults_to_project_root(tmp_path):
    run_command = _run_command_with_state(_active_state(tmp_path))
    result = run_command("pwd")
    assert str(tmp_path) in result


def test_explicit_cwd_wins_over_active_project(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    run_command = _run_command_with_state(_active_state(tmp_path))
    result = run_command("pwd", cwd=str(other))
    assert str(other) in result
    assert str(other) != str(tmp_path)


def test_explicit_relative_cwd_stays_relative_even_with_active_project(tmp_path):
    """Section 17: an explicit relative cwd is NOT reinterpreted as
    project-relative merely because a project is active -- it's expanded
    exactly as before (resolved by the OS/Popen against the real process
    cwd), never joined against project.root."""
    (tmp_path / "subdir").mkdir()
    run_command = _run_command_with_state(_active_state(tmp_path / "unrelated_root_never_used"))
    old_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        result = run_command("pwd", cwd="subdir")
        assert str(tmp_path / "subdir") in result
    finally:
        os.chdir(old_cwd)


def test_no_cwd_no_active_project_defaults_to_home():
    run_command = _run_command_with_state(project_state=None)
    result = run_command("pwd")
    assert os.path.expanduser("~") in result


def test_no_cwd_with_empty_state_holder_defaults_to_home():
    empty_state = ProjectContextState()  # holder exists, nothing activated
    run_command = _run_command_with_state(empty_state)
    result = run_command("pwd")
    assert os.path.expanduser("~") in result


def test_two_context_states_produce_independent_cwd(tmp_path):
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    run_command_a = _run_command_with_state(_active_state(root_a))
    run_command_b = _run_command_with_state(_active_state(root_b))

    result_a = run_command_a("pwd")
    result_b = run_command_b("pwd")
    assert str(root_a) in result_a
    assert str(root_b) in result_b
    assert str(root_b) not in result_a
    assert str(root_a) not in result_b


def test_guardrails_still_block_before_execution_with_active_project(tmp_path):
    run_command = _run_command_with_state(_active_state(tmp_path))
    result = run_command("rm -rf /")
    assert result.startswith("[BLOCKED:")


def test_no_os_chdir_leaks_into_process(tmp_path):
    """Each run_command call must set cwd only on its own Popen — never
    mutate the real process cwd (os.chdir), which would leak across
    concurrent/subsequent calls regardless of which project_state issued
    them."""
    before = os.getcwd()
    run_command = _run_command_with_state(_active_state(tmp_path))
    run_command("pwd")
    assert os.getcwd() == before
