"""Neutral bounded Git-read subprocess boundary.

Extracted from core.coding_checkpoint_observation (CODING-05A2) so that
CODING-08's review kernel (core.git_review) can share exactly one hardened
execution primitive for read-only Git invocations instead of forking a
second, subtly different subprocess implementation. Behavior is unchanged
from the original private ``_run_git``: argv-only, no shell, no terminal
prompts, no optional locks, a hard wall-clock timeout, and hard byte caps
on both stdout and stderr enforced by a background reader thread so a
runaway or malicious process can never grow this process's memory
unbounded while a caller is still deciding whether to keep reading.

This module is deliberately domain-blind: it never raises a domain
exception (ObservationError, ReviewProbeError, ...) and never interprets
a nonzero exit code -- both are a caller's job. It also never decides
what argv to run; callers own their own safety flags (``--no-ext-diff``,
``--no-textconv``, ``-c color.ui=never``, and similar) entirely.
"""

from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

DEFAULT_TIMEOUT_SECONDS = 5
DEFAULT_MAX_STDOUT_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_STDERR_BYTES = 16 * 1024

_READER_CHUNK_BYTES = 64 * 1024


class GitReadError(Exception):
    """Transport-level failure: exec unavailable, timeout, or output overflow.

    Never raised for a nonzero Git exit code -- that is Git reporting a
    normal (if sometimes unwelcome) outcome, which callers interpret
    themselves via the returned GitCommandResult.
    """


@dataclass(frozen=True)
class GitCommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


def _bounded_reader(stream, limit: int, sink: list, overflow: threading.Event):
    total = 0
    while True:
        chunk = stream.read(_READER_CHUNK_BYTES)
        if not chunk:
            break
        remaining = limit - total
        if remaining > 0:
            sink.append(chunk[:remaining])
        total += len(chunk)
        if total > limit:
            overflow.set()


def run_bounded_git(
    root: str,
    args: Sequence[str],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_stdout_bytes: int = DEFAULT_MAX_STDOUT_BYTES,
    max_stderr_bytes: int = DEFAULT_MAX_STDERR_BYTES,
    extra_env: Optional[Mapping[str, str]] = None,
) -> GitCommandResult:
    """Run ``git -C root <args>`` with hard time/output bounds, no prompts.

    ``extra_env`` is applied on top of the always-set safety baseline
    (GIT_TERMINAL_PROMPT=0, GIT_OPTIONAL_LOCKS=0, LC_ALL=C), never in
    place of it -- a caller cannot accidentally re-enable a prompt or a
    lock by omission.
    """
    env = os.environ.copy()
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "LC_ALL": "C",
    })
    if extra_env:
        env.update(extra_env)
    try:
        process = subprocess.Popen(
            ["git", "-C", root, *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            env=env,
        )
    except OSError as error:
        raise GitReadError("Git executable is unavailable") from error

    stdout_parts: list = []
    stderr_parts: list = []
    stdout_overflow = threading.Event()
    stderr_overflow = threading.Event()
    stdout_thread = threading.Thread(
        target=_bounded_reader,
        args=(process.stdout, max_stdout_bytes, stdout_parts, stdout_overflow),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_bounded_reader,
        args=(process.stderr, max_stderr_bytes, stderr_parts, stderr_overflow),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        process.kill()
        process.wait()
        stdout_thread.join()
        stderr_thread.join()
        raise GitReadError("Git probe timed out") from error

    stdout_thread.join()
    stderr_thread.join()
    if stdout_overflow.is_set() or stderr_overflow.is_set():
        raise GitReadError("Git probe output exceeded its bounded capture limit")
    return GitCommandResult(
        returncode=process.returncode,
        stdout=b"".join(stdout_parts),
        stderr=b"".join(stderr_parts),
    )
