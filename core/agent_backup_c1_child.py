"""core/agent_backup_c1_child.py -- the Verified Agent Backup's C1 capture,
run in a short-lived child process (AGENT-BACKUP-RESTORE C1-S3, gates G-1
and G-2; C1S3-AMENDMENT-A1 A-2, A-3).

The capture's CPU work competes for Python's interpreter lock, so it never
runs in the agent's own process: a trip there is never delayed by it. The
parent (core.agent_backup_export) starts this module as

    python -E -s -B -m core.agent_backup_c1_child <mode> <data_dir>

from the code root, with a minimal environment, no stdin and no stderr, in
its own session. -E ignores PYTHON* variables, -s ignores user
site-packages and -B writes no bytecode, so the child creates no file of
its own; SQLite may leave the store's -wal/-shm sidecars (C1-S1 S1-08).

Protocol: the exit code is the status and stdout the only channel.
    0  capture: the canonical record bytes. incarnation: "<soul> <incarnation>\\n".
    3  one line: a core.suspension snapshot refusal code.
    4  one line: a core.suspension_backup exclusion refusal code.
    5  one line: a LIMIT_CODES word.
Anything else (another exit, a signal, a traceback on the discarded stderr)
is a failed capture. The parent never trusts these bytes: it re-parses and
re-validates them, and they are only ever data.

Limits go on before anything from core.* is imported: a self-alarm, a lower
CPU priority, a CPU-time limit and, on Linux, first place in the OOM
killer's queue. A child that can't apply them refuses rather than running
unbounded. The store and its WAL are measured before C1 copies them into
memory, because C1's own caps bound the record, not the copy (TS1-01).
"""
import os
import signal
import sys

MODES = ("capture", "incarnation")
EXIT_OK, EXIT_REFUSED, EXIT_BOUNDARY, EXIT_LIMIT = 0, 3, 4, 5
CAPTURE_TIMEOUT_S = 120
INCARNATION_TIMEOUT_S = 60
SELF_LIMIT_S = CAPTURE_TIMEOUT_S + 30
CPU_LIMIT_S = 60
STORE_MAX_BYTES = 512 * 1024 * 1024
LIMIT_CODES = frozenset({"CHILD_LIMITS_UNAVAILABLE", "STORE_TOO_LARGE", "USAGE"})
CODE_LINE_MAX_BYTES = 64
INCARNATION_LINE_MAX_BYTES = 128


def _emit(data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(1, view):]


def _code(exit_code: int, code: str) -> int:
    _emit((code + "\n").encode("ascii"))
    return exit_code


def _apply_limits() -> None:
    if not hasattr(signal, "alarm") or not hasattr(os, "nice"):
        raise OSError("no alarm or nice on this platform")
    signal.alarm(SELF_LIMIT_S)
    os.nice(10)
    import resource
    _soft, hard = resource.getrlimit(resource.RLIMIT_CPU)
    soft = CPU_LIMIT_S if hard == resource.RLIM_INFINITY else min(CPU_LIMIT_S, hard)
    resource.setrlimit(resource.RLIMIT_CPU, (soft, hard))
    if sys.platform.startswith("linux"):
        with open("/proc/self/oom_score_adj", "w") as handle:
            handle.write("1000")


def _store_bytes(memory_dir: str, store_name: str) -> int:
    import stat
    total = 0
    for name in (store_name, store_name + "-wal"):
        try:
            st = os.lstat(os.path.join(memory_dir, name))
        except OSError:
            continue  # missing or unobservable: C1's own capture decides
        if stat.S_ISREG(st.st_mode):
            total += st.st_size
    return total


def main(argv) -> int:
    try:
        _apply_limits()
    except (OSError, ValueError, ImportError):
        return _code(EXIT_LIMIT, "CHILD_LIMITS_UNAVAILABLE")
    if len(argv) != 2 or argv[0] not in MODES or not argv[1]:
        return _code(EXIT_LIMIT, "USAGE")
    mode, data_dir = argv
    from core import suspension_backup as c1
    try:
        if mode == "capture":
            memory_dir = os.path.join(data_dir, c1.MEMORY_DIRNAME)
            if _store_bytes(memory_dir, c1.STORE_NAME) > STORE_MAX_BYTES:
                return _code(EXIT_LIMIT, "STORE_TOO_LARGE")
            _emit(c1.capture_recovery_state(data_dir).payload)
            return EXIT_OK
        soul_id, incarnation = c1.observe_store_incarnation(data_dir)
        _emit(f"{soul_id} {incarnation}\n".encode("ascii"))
        return EXIT_OK
    except c1.RecoverySnapshotRefused as exc:
        return _code(EXIT_REFUSED, exc.code)
    except c1.ExclusionUnavailable as exc:
        return _code(EXIT_BOUNDARY, exc.code)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
