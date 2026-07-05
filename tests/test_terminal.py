"""F-37 (terminal half): tools/terminal.py — timeout kills the whole
process group, not just the immediate shell."""
import time
import subprocess
import pytest
from tools.terminal import register_terminal_tools


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
