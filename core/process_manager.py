"""
core/process_manager.py -- CODING-04A1: platform-neutral persistent
process kernel.

Owns the job registry, per-job metadata, emergency execution leases,
reader/watcher threads, bounded output buffers, lifecycle, cleanup, and
TTL retention for OS processes that outlive the call that launched them.
core/process_control.py is the ONLY thing this module calls for anything
host-specific (spawn / has_exited / terminate_tree) -- this file must
never itself branch on platform or touch a POSIX/Windows-only API
directly (tests/test_process_control.py's portability regression tests
assert exactly that by grepping this file's own source).

No model-facing tool is registered here or anywhere else in this slice --
this is engine + safety-boundary only. CODING-04A2 will build
start_process/read_process/send_process_input/stop_process/list_processes
on top of the internal functions here (launch/request_stop/
get_job_snapshot/etc.), after independent safety review.

Module-global runtime state, same idiom as core/task_queue.py and
core/emergency_stop.py -- deliberately not a class-based singleton, so
tests can reach in via _reset_for_tests() the same way those modules
already allow.

No disk persistence, no restart reconnection, no external PID adoption --
a process surviving a hard crash is unmanaged external OS state; this
module (and its future A2 model-facing layer) never adopts a PID it did
not itself spawn.
"""

import os
import secrets
import threading
import time

from core import emergency_stop, process_control
from tools.guardrails import check_command

MAX_LIVE_PROCESSES = 10
STREAM_BUFFER_MAX_CHARS = 65536
COMPLETED_PROCESS_TTL_SECONDS = 1800

_CHUNK_SIZE = 4096  # bounded read() size -- see _drain_stream's docstring

# CODING-04A1C: how often the watcher re-asks process_control.tree_is_alive()
# after the leader has exited but the managed control tree/group has not
# yet been confirmed empty. A manager-side polling-cadence choice, not a
# platform mechanic -- lives here, not in process_control.py. Internal
# only, never model-facing.
_TREE_POLL_INTERVAL_SECONDS = 0.2

_STATUSES = frozenset({"running", "exited", "stopped", "emergency_killed", "shutdown_killed"})


class ProcessManagerError(Exception):
    """Base class for launch() rejections."""


class InvalidLaunchRequest(ProcessManagerError):
    """Empty command, non-absolute cwd, or a cwd that isn't a real directory."""


class CommandBlocked(ProcessManagerError):
    """tools.guardrails.check_command() denied this command before it
    ever reached the OS."""


class LiveProcessLimitReached(ProcessManagerError):
    """MAX_LIVE_PROCESSES managed jobs are already live."""


class LaunchRefused(ProcessManagerError):
    """Emergency stop is latched, or the calling execution's own epoch is
    already stale -- the process-manager-boundary equivalent of
    emergency_stop.EmergencyStopError."""


class _StreamBuffer:
    """Bounded ring-ish text buffer for one job's stdout or stderr.
    start_offset/end_offset are absolute character offsets into the
    *total* decoded stream this job has ever produced -- end_offset never
    resets on wrap, and start_offset only ever advances by exactly the
    number of characters actually discarded, so a future incremental
    cursor read (A2) has a truthful coordinate system regardless of how
    much has been trimmed. Retained text itself is always <= max_chars."""

    def __init__(self, max_chars: int):
        self._max_chars = max_chars
        self._text = ""
        self._start_offset = 0
        self._end_offset = 0
        self._lock = threading.Lock()

    def append(self, chunk: str):
        if not chunk:
            return
        with self._lock:
            self._text += chunk
            self._end_offset += len(chunk)
            overflow = len(self._text) - self._max_chars
            if overflow > 0:
                self._text = self._text[overflow:]
                self._start_offset += overflow

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "start_offset": self._start_offset,
                "end_offset": self._end_offset,
                "text": self._text,
            }


class _Job:
    """Deliberately does NOT retain core.project_context.ProjectContextState
    (Section 15) -- cwd is captured as a concrete absolute string at
    spawn time and never follows a later Project change."""

    def __init__(self, process_id, command, cwd, channel_id, epoch, lease_id, proc, control_metadata):
        self.process_id = process_id
        self.command = command
        self.cwd = cwd
        self.channel_id = channel_id
        self.epoch = epoch
        self.lease_id = lease_id
        self.proc = proc
        self.pid = proc.pid
        self.control_metadata = control_metadata
        self.start_at = time.time()
        self.finish_at = None
        self.status = "running"
        self.returncode = None
        # First-reason-wins marker consumed exactly once by the watcher at
        # finalize time -- set by _terminate_job(), never overwritten by a
        # later, redundant stop request racing against the first.
        self.stop_reason = None
        self.stdout_buffer = _StreamBuffer(STREAM_BUFFER_MAX_CHARS)
        self.stderr_buffer = _StreamBuffer(STREAM_BUFFER_MAX_CHARS)
        self.stdout_thread = None
        self.stderr_thread = None
        self.watcher_thread = None
        # Guards status/returncode/finish_at/stop_reason read-modify-write.
        # Never held while spawning, waiting on the process, reading
        # output, joining threads, or calling into process_control --
        # see module docstring / Section 26.
        self.lifecycle_lock = threading.RLock()
        self.stdin_lock = threading.Lock()


_registry: dict = {}
_registry_lock = threading.RLock()
_live_reserved = 0  # slots currently occupied: reserved-but-not-yet-a-job, or a running job


def _reserve_slot():
    global _live_reserved
    with _registry_lock:
        if _live_reserved >= MAX_LIVE_PROCESSES:
            raise LiveProcessLimitReached(
                f"at most {MAX_LIVE_PROCESSES} managed processes may run at once"
            )
        _live_reserved += 1


def _release_slot():
    global _live_reserved
    with _registry_lock:
        _live_reserved = max(0, _live_reserved - 1)


def _generate_process_id() -> str:
    with _registry_lock:
        while True:
            candidate = "p-" + secrets.token_hex(6)  # 12 lowercase hex chars
            if candidate not in _registry:
                return candidate


def _drain_stream(job: "_Job", stream_name: str):
    """One daemon reader thread per stream. Bounded chunk reads, not
    readline() -- an unbounded readline() call risks accumulating an
    arbitrarily large single line in reader-thread memory if the child
    writes continuously without a newline; a fixed-size chunk read bounds
    per-call memory and keeps partial no-newline output visible instead
    of invisible until a newline (or EOF) finally shows up. text=True +
    errors="replace" at spawn time means an invalid byte sequence decodes
    to U+FFFD rather than raising -- this thread never dies from bad
    input, only from the stream actually closing."""
    stream = job.proc.stdout if stream_name == "stdout" else job.proc.stderr
    buf = job.stdout_buffer if stream_name == "stdout" else job.stderr_buffer
    try:
        while True:
            chunk = stream.read(_CHUNK_SIZE)
            if not chunk:
                break
            buf.append(chunk)
    except (ValueError, OSError):
        # Stream closed out from under us during teardown -- proc.wait()
        # in the watcher plus this thread's own return is the real
        # completion signal; nothing further to drain.
        pass


def _safe_close_stdin(job: "_Job"):
    with job.stdin_lock:
        try:
            if job.proc.stdin is not None:
                job.proc.stdin.close()
        except (OSError, ValueError):
            pass


def _watch_job(job: "_Job"):
    """One daemon watcher thread per job -- the sole path that finalizes
    a job and ends its persistent emergency lease. Runs exactly once per
    job.

    CODING-04A1C: proc.wait() returning only means the LEADER has exited
    -- it is not the finalization signal. The independent safety review
    confirmed a landing blocker built on exactly that conflation: a
    backgrounded descendant left alive after `command & exit` survives
    the leader, and the old code finalized (and released the emergency
    lease) the instant proc.wait() returned regardless. The managed
    CONTROL TREE (POSIX process group / Windows Job Object) -- not the
    leader -- now defines job lifetime: finalization waits for
    process_control.tree_is_alive() to go False, however that happens
    (natural exit of every member, or a terminate_tree() request from
    request_stop()/emergency_kill_all()/shutdown_all() racing in at any
    point). Death must be OBSERVED before any authority (the lease, the
    live-process slot) is released -- terminate_tree() merely requests
    death, it does not itself finalize anything."""
    job.proc.wait()  # leader's own return code is now available; the
                      # managed tree may still have live members below.

    while process_control.tree_is_alive(job.control_metadata):
        time.sleep(_TREE_POLL_INTERVAL_SECONDS)

    # The manager never re-uses a control identity once the underlying
    # tree/group is confirmed empty -- retiring it here (exactly once,
    # only now) is what makes it safe for a PGID/Job-Object-handle number
    # to be reused by an unrelated process later without this module ever
    # mistaking that unrelated process for "ours" again.
    process_control.release_control(job.control_metadata)

    # Reader threads may still be draining pipes an inherited-fd-holding
    # descendant kept open after the leader exited -- by the time the
    # tree is confirmed empty, no writer can remain, so EOF should already
    # be at hand. These joins are now a bounded defensive backstop, not
    # the definition of "done" (that was the other half of the A1 bug).
    if job.stdout_thread is not None:
        job.stdout_thread.join(timeout=10)
    if job.stderr_thread is not None:
        job.stderr_thread.join(timeout=10)

    with job.lifecycle_lock:
        job.returncode = job.proc.returncode
        job.finish_at = time.time()
        job.status = job.stop_reason if job.stop_reason is not None else "exited"

    _safe_close_stdin(job)
    emergency_stop.end_persistent_execution(job.lease_id)
    _release_slot()


def _terminate_job(job: "_Job", reason: str):
    """Idempotent core stop primitive shared by request_stop(),
    emergency_kill_all(), shutdown_all(), and launch()'s post-spawn
    epoch re-check. First reason wins; does not itself finalize the job
    or release the lease/slot -- the watcher owns that, once, when it
    actually observes death (see _watch_job)."""
    with job.lifecycle_lock:
        if job.status != "running":
            return
        if job.stop_reason is None:
            job.stop_reason = reason
    # terminate_tree() is called outside the lock (Section 26) and itself
    # re-checks proc.poll() first, so redundant concurrent calls here
    # (manual stop racing emergency kill-all, etc.) are safe.
    process_control.terminate_tree(job.proc, job.control_metadata)


def launch(command: str, cwd: str, channel_id: str = "") -> str:
    """Launch `command` as a managed, persistent background process
    rooted at concrete absolute `cwd`. Returns an opaque Lumina process_id
    ("p-<12 hex chars>") -- never the raw OS PID (Section 6). Raises a
    ProcessManagerError subclass on rejection; a blocked/rejected launch
    never reaches the OS, never consumes a lease, never consumes a live
    slot, and leaves no registry entry.

    channel_id is stored as provenance only -- this slice does not treat
    it as an authorization principal (Section 27; that's A2's job).

    Implements the exact ordering Section 17 requires: guardrail check
    and cwd validation happen before the live-job slot is even reserved,
    so a rejected launch never touches capacity meant for a real job; the
    epoch is captured and a persistent emergency lease is opened BEFORE
    spawning, so a latch that wins that race is caught by
    capture_epoch_for_new_work()/begin_persistent_execution() raising
    before any OS process exists; and a latch that wins the race anywhere
    between lease acquisition and manager registration is caught by the
    immediate post-registration epoch re-check at the end of this
    function, which promptly terminates the newly spawned tree while
    leaving the lease open until the watcher actually observes death.
    """
    if not isinstance(command, str) or not command.strip():
        raise InvalidLaunchRequest("command must not be empty or whitespace-only")

    block_reason = check_command(command)
    if block_reason:
        raise CommandBlocked(block_reason)

    if not os.path.isabs(cwd):
        raise InvalidLaunchRequest(f"cwd must be an absolute path: {cwd!r}")
    if not os.path.isdir(cwd):
        raise InvalidLaunchRequest(f"cwd does not exist or is not a directory: {cwd!r}")

    _reserve_slot()

    job = None
    lease_id = None
    try:
        try:
            epoch = emergency_stop.capture_epoch_for_new_work()
        except emergency_stop.EmergencyStopError as e:
            raise LaunchRefused(str(e)) from e

        process_id = _generate_process_id()

        try:
            lease_id = emergency_stop.begin_persistent_execution(
                kind="managed_process", label=process_id, expected_epoch=epoch,
            )
        except emergency_stop.EmergencyStopError as e:
            raise LaunchRefused(str(e)) from e

        proc, control_metadata = process_control.spawn(command, cwd)

        job = _Job(
            process_id=process_id, command=command, cwd=cwd, channel_id=channel_id,
            epoch=epoch, lease_id=lease_id, proc=proc, control_metadata=control_metadata,
        )
        with _registry_lock:
            _registry[process_id] = job

        job.stdout_thread = threading.Thread(target=_drain_stream, args=(job, "stdout"), daemon=True)
        job.stderr_thread = threading.Thread(target=_drain_stream, args=(job, "stderr"), daemon=True)
        job.watcher_thread = threading.Thread(target=_watch_job, args=(job,), daemon=True)
        job.stdout_thread.start()
        job.stderr_thread.start()
        job.watcher_thread.start()

        # Step 13: a latch that won the race any time between epoch
        # capture and here must not leave a live, unmanaged-looking child
        # merely because this call is about to return. The lease stays
        # open regardless -- only the watcher observing real death closes
        # it -- but the tree is asked to die right now.
        if not emergency_stop.execution_permitted(epoch):
            _terminate_job(job, reason="emergency_killed")

        return process_id

    except Exception:
        if job is None:
            # Never reached job creation/registration -- release whatever
            # this attempt already acquired and leave zero residue.
            if lease_id is not None:
                emergency_stop.end_persistent_execution(lease_id)
            _release_slot()
        raise


def request_stop(process_id: str) -> bool:
    """Internal stop core (Section 21) -- no model-facing tool calls this
    in A1; A2 will wrap it. Idempotent: returns False for an unknown
    process_id or one that's already terminal (including one that exited
    naturally on its own, which is never re-signaled based on stale
    metadata)."""
    with _registry_lock:
        job = _registry.get(process_id)
    if job is None:
        return False
    with job.lifecycle_lock:
        if job.status != "running":
            return False
    _terminate_job(job, reason="stopped")
    return True


def emergency_kill_all() -> int:
    """Called by the OH SHIT cascade ONLY after emergency_stop.latch()
    has already flipped -- this function does not latch anything itself.
    Requests whole-tree termination for every currently-running managed
    job and returns immediately (does not block the caller waiting for
    watcher finalization -- Section 22). can_rearm() stays False for each
    job until its own watcher actually observes death and closes that
    job's persistent lease."""
    with _registry_lock:
        jobs = [j for j in _registry.values() if j.status == "running"]
    for job in jobs:
        _terminate_job(job, reason="emergency_killed")
    return len(jobs)


def shutdown_all(wait_seconds: float = 5.0) -> int:
    """Normal (non-emergency) application-shutdown core (Section 24) --
    not wired to any GUI closeEvent or CLI exit path in this slice.
    Requests termination for every running job, classified "shutdown_killed",
    then optionally waits up to wait_seconds total for their watchers to
    finalize. Idempotent; a process that dies via an uncatchable
    interpreter/process kill before this runs is a documented limitation
    Python cannot work around, not something this function attempts to
    detect or recover from."""
    with _registry_lock:
        jobs = [j for j in _registry.values() if j.status == "running"]
    for job in jobs:
        _terminate_job(job, reason="shutdown_killed")
    if wait_seconds and wait_seconds > 0:
        deadline = time.time() + wait_seconds
        for job in jobs:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            if job.watcher_thread is not None:
                job.watcher_thread.join(timeout=remaining)
    return len(jobs)


def _sweep_expired():
    now = time.time()
    with _registry_lock:
        expired = [
            pid for pid, job in _registry.items()
            if job.status != "running" and job.finish_at is not None
            and (now - job.finish_at) > COMPLETED_PROCESS_TTL_SECONDS
        ]
        for pid in expired:
            del _registry[pid]


def _snapshot_job(job: "_Job") -> dict:
    with job.lifecycle_lock:
        return {
            "process_id": job.process_id,
            "command": job.command,
            "cwd": job.cwd,
            "channel_id": job.channel_id,
            "epoch": job.epoch,
            "pid": job.pid,
            "control_metadata": dict(job.control_metadata),
            "status": job.status,
            "returncode": job.returncode,
            "termination_reason": job.stop_reason,
            "start_at": job.start_at,
            "finish_at": job.finish_at,
            "stdout": job.stdout_buffer.snapshot(),
            "stderr": job.stderr_buffer.snapshot(),
        }


def get_job_snapshot(process_id: str):
    """Internal/test read API -- not model-facing. Returns None if
    process_id is unknown or has already aged out past
    COMPLETED_PROCESS_TTL_SECONDS."""
    _sweep_expired()
    with _registry_lock:
        job = _registry.get(process_id)
    if job is None:
        return None
    return _snapshot_job(job)


def list_job_ids() -> list:
    """Internal/test read API -- not model-facing."""
    _sweep_expired()
    with _registry_lock:
        return list(_registry.keys())


def _reset_for_tests():
    """Test-only, mirrors core.emergency_stop._reset_for_tests()'s
    convention. Never call from product code. Best-effort hard-terminates
    and reaps any job left running by a previous test, then clears all
    module-global state including the live-slot counter."""
    global _live_reserved
    with _registry_lock:
        jobs = list(_registry.values())
    for job in jobs:
        try:
            _terminate_job(job, reason="shutdown_killed")
        except Exception:
            pass
    for job in jobs:
        if job.watcher_thread is not None:
            job.watcher_thread.join(timeout=5)
        try:
            emergency_stop.end_persistent_execution(job.lease_id)
        except Exception:
            pass
    with _registry_lock:
        _registry.clear()
        _live_reserved = 0
