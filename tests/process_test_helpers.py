"""Portable command and process-topology helpers for A2 process tests."""

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path


def command_from_argv(argv, platform_name=None):
    """Quote an argv sequence for the manager's host-native command string."""
    platform_name = os.name if platform_name is None else platform_name
    argv = [os.fspath(arg) for arg in argv]
    if platform_name == "nt":
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


def python_command(source, *args, platform_name=None):
    """Build a safely quoted command using the running test interpreter."""
    return command_from_argv(
        [sys.executable, "-u", "-c", source, *args],
        platform_name=platform_name,
    )


def sleeping_python_command(seconds=30):
    return python_command(
        "import sys, time; time.sleep(float(sys.argv[1]))", str(seconds)
    )


def stdin_waiting_python_command():
    return python_command("import sys; sys.stdin.read()")


def descendant_python_command(sentinel, seconds=30):
    """Build a leader that exits after spawning one observable descendant."""
    child_source = (
        "import os, pathlib, sys, time; "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8'); "
        "time.sleep(float(sys.argv[2]))"
    )
    leader_source = (
        "import subprocess, sys; "
        f"subprocess.Popen([sys.executable, '-u', '-c', {child_source!r}, "
        "sys.argv[1], sys.argv[2]], stdin=subprocess.DEVNULL)"
    )
    return python_command(leader_source, sentinel, str(seconds))


def wait_for_path(path, timeout=5.0):
    deadline = time.monotonic() + timeout
    path = Path(path)
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return path.exists()
