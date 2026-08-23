"""
core/process_control.py -- CODING-04A1/A1C: the ONLY module in Lumina that
understands host-specific process-spawn and process-tree-control mechanics.
core/process_manager.py (the platform-neutral job registry/lifecycle owner)
calls exactly the functions in the "Platform-neutral public API" section
below and never branches on platform itself, never inspects a PGID or a
Job Object handle, and never calls a POSIX signal or a Win32 API directly
-- see that module's own docstring.

CODING-04A1C corrective: the CODING-04A1 independent safety review found a
confirmed landing blocker -- terminate_tree() (and, downstream, the
manager's finalization logic) treated "the shell leader has exited" as
equivalent to "the managed work is finished." It is not. `command &`
followed by the shell itself exiting is an ordinary pattern that leaves a
live descendant in the same POSIX process group (or, on Windows, the same
job) after Popen.poll() already reports the leader dead. The reviewer
reproduced this live: terminate_tree() no-opped, the manager's watcher
marked the job "exited" and closed its persistent emergency lease, and
emergency_kill_all() subsequently reported nothing to kill while the
descendant was still confirmed running.

The fix is architectural, not a patch to the leader-exit check: tree
lifetime is now a first-class concept, separate from leader lifetime.
    tree_is_alive(control_metadata) -> bool
        Is any process still alive in this job's managed control group
        (POSIX process group / Windows Job Object)? NEVER answered by
        Popen.poll() on the leader.
    terminate_tree(proc, control_metadata)
        Best-effort whole-tree hard termination. No longer short-circuits
        on the leader's poll() -- only a control identity that has already
        been *retired* (see release_control below) is skipped, to avoid
        signalling a since-reused PGID/handle.
    release_control(control_metadata)
        Called exactly once, only after the manager's watcher has itself
        observed tree_is_alive() go False -- retires the control identity
        (POSIX: marks the PGID inert; Windows: closes the Job Object
        handle) so no later stray call can ever act on a reused PGID or a
        closed handle.

Backend selection reads sys.platform live (never cached at import time),
so tests can monkeypatch sys.platform and exercise branch selection
deterministically. Linux and macOS (Darwin) both select the POSIX
backend -- process groups and POSIX signal-0 liveness probing are
genuinely shared mechanics between them, not something this module fakes.

Windows tree control is Job-Object-based, not root-PID-based (Part 7 of
the corrective): a Job Object's lifetime is independent of whether the
original root process is still running, exactly like a POSIX process
group survives its leader's exit. The root process is assigned to the
Job Object via CREATE_SUSPENDED + AssignProcessToJobObject + ResumeThread
(all public, documented Win32 APIs, reached only through ctypes -- no
pywin32, no private CPython attributes) rather than
PROC_THREAD_ATTRIBUTE_JOB_LIST's raw CreateProcessW/STARTUPINFOEX path,
specifically so subprocess.Popen still owns pipe/stdin/stdout/stderr/
wait/poll plumbing -- reimplementing that plumbing by hand via raw
CreateProcessW would be far riskier to get right with zero live Windows
testing available. CREATE_SUSPENDED gives the identical race-freedom
guarantee Part 8 requires: the child executes literally zero instructions
-- including spawning any child of its own -- until this module has
already assigned it to the Job Object and explicitly resumed it.

No live Windows testing is claimed anywhere in this file or its tests --
see tests/test_process_control.py's Windows section for what is proven
via ctypes-structure-construction and monkeypatched-dispatch unit tests
instead.
"""

import ctypes
import ctypes.wintypes as wintypes  # importable on any OS -- verified; only
                                     # ctypes.WinDLL/ctypes.windll (resolved
                                     # lazily inside functions below) require
                                     # an actual Windows host.
import os
import shutil
import signal
import subprocess
import sys


class ProcessControlError(Exception):
    """Raised when this platform's process-control layer cannot safely
    satisfy a request -- e.g. no usable shell found on a POSIX host, or a
    Win32 Job Object API call failed."""


# subprocess only defines CREATE_NEW_PROCESS_GROUP as a module attribute
# when Python itself is running on Windows -- constructing Windows Popen
# kwargs on a non-Windows host (Section 11's required mock/unit testing
# from the Linux dev host, since no Windows kernel is available) would
# otherwise raise AttributeError before a single test could run. The
# fallback is the exact, stable Win32 API value (winbase.h:
# CREATE_NEW_PROCESS_GROUP == 0x00000200) -- on a real Windows host,
# getattr finds the real stdlib attribute and this is a no-op.
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
_CREATE_SUSPENDED = 0x00000004  # stable Win32 constant (winbase.h)


def _get_last_error() -> int:
    """ctypes.get_last_error() -- like ctypes.WinDLL itself -- only exists
    on Windows Python builds (confirmed: AttributeError on this dev host).
    Guarded the same way and for the same reason: so the error-reporting
    paths below can be exercised via fakes in unit tests on the Linux dev
    host without raising before the test's actual assertion is reached.
    On a real Windows host the real ctypes.get_last_error is always used."""
    return ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else -1


def backend_name() -> str:
    """'windows' or 'posix', read live from sys.platform every call."""
    return "windows" if sys.platform.startswith("win") else "posix"


# ── POSIX (Linux + macOS) ───────────────────────────────────────────────

def _resolve_posix_shell() -> str:
    """Resolve a real bash executable via PATH lookup rather than
    hardcoding /bin/bash -- a Linux-only pathname assumption that would
    silently misbehave (or simply not exist) on a differently-configured
    POSIX host. Raises ProcessControlError if none is found; callers must
    fail clearly rather than silently falling back to a shell whose
    quoting/builtin semantics differ from what the caller's command text
    assumes."""
    bash = shutil.which("bash")
    if not bash:
        raise ProcessControlError(
            "no bash executable found on PATH -- cannot spawn a managed shell process"
        )
    return bash


def _build_posix_popen_kwargs(command: str, cwd: str, bash_path: str) -> dict:
    return dict(
        args=command,
        shell=True,
        executable=bash_path,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,  # own process group -> killpg reaches children too
    )


def _spawn_posix(command: str, cwd: str):
    bash_path = _resolve_posix_shell()
    kwargs = _build_posix_popen_kwargs(command, cwd, bash_path)
    proc = subprocess.Popen(**kwargs)
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = proc.pid  # process already gone; keep metadata internally consistent
    return proc, {"backend": "posix", "pgid": pgid, "retired": False}


def _build_posix_argv_popen_kwargs(argv, cwd: str, env=None) -> dict:
    return dict(
        args=list(argv),
        shell=False,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    )


def _spawn_posix_argv(argv, cwd: str, env=None):
    kwargs = _build_posix_argv_popen_kwargs(argv, cwd, env)
    proc = subprocess.Popen(**kwargs)
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = proc.pid
    return proc, {"backend": "posix", "pgid": pgid, "retired": False}


def _process_group_is_alive_posix(pgid) -> bool:
    """Signal-0 liveness probe -- os.killpg(pgid, 0) never actually signals
    anything, it only asks the kernel whether the target exists and
    whether we have permission to signal it (POSIX kill(2) semantics).
        ESRCH  (ProcessLookupError) -> the group has no members: dead.
        EPERM  (PermissionError)    -> the group exists, we just can't
                                        presently signal it: still alive.
        success                     -> the group exists: alive.
        anything else               -> unexpected; fail conservative (
                                        "still alive", never a false
                                        "dead" that would prematurely
                                        release emergency authority) and
                                        surface it rather than silently
                                        guessing.
    """
    if pgid is None:
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as e:
        print(f"[PROCESS_CONTROL] unexpected error probing process group {pgid} "
              f"liveness ({e!r}) -- treating as still alive", flush=True)
        return True


def _tree_is_alive_posix(control_metadata: dict) -> bool:
    if control_metadata.get("retired"):
        return False
    return _process_group_is_alive_posix(control_metadata.get("pgid"))


def _terminate_tree_posix(proc, control_metadata: dict):
    if control_metadata.get("retired"):
        # Section 6: a retired control identity's PGID is inert historical
        # metadata only -- never signal it again, it may since have been
        # reused by an unrelated process.
        return
    pgid = control_metadata.get("pgid")
    # Hard kill only, no SIGTERM grace period -- matches tools/terminal.py's
    # already-proven run_command timeout semantics (F-37); this is the
    # same owner-trust tier. No longer gated on the leader's poll() --
    # that was the confirmed A1C landing blocker.
    try:
        if pgid is not None:
            os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass  # group already empty/gone -- tolerate the ordinary "already dead" race
    try:
        proc.kill()  # fallback: guarantees the direct child dies even if
    except OSError:  # the group-kill couldn't reach it for any reason
        pass


def _release_control_posix(control_metadata: dict):
    """Called exactly once by the manager's watcher, only after it has
    itself observed tree_is_alive() go False. Retires the control
    identity so no later stray terminate_tree()/tree_is_alive() call can
    ever act on this PGID again -- the OS is free to reuse that PGID
    number for an unrelated process group the moment this one is truly
    empty, and this module must never mistake that unrelated group for
    "ours" again. The PGID value itself is left in place as inert
    historical metadata (visible in completed-job snapshots), it is just
    never operated on again once retired."""
    control_metadata["retired"] = True


# ── Windows (Job Object) ────────────────────────────────────────────────
#
# Win32 constants and structure layouts below are stable across all
# Windows versions Job Objects have existed on and are safe to define at
# module import time (they are pure data -- no DLL is touched to define
# them). Only the functions that actually call into kernel32 are gated to
# run solely on a real Windows host (see _kernel32() below); nothing here
# raises on import on a non-Windows Python build -- verified: ctypes and
# ctypes.wintypes both import cleanly on Linux, only ctypes.WinDLL/
# ctypes.windll do not exist there.

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JobObjectExtendedLimitInformation = 9
_JobObjectBasicProcessIdList = 3
_ERROR_MORE_DATA = 234
_TH32CS_SNAPTHREAD = 0x00000004
_THREAD_SUSPEND_RESUME = 0x00000002
_PROCESS_SET_QUOTA = 0x00000100
_PROCESS_TERMINATE = 0x00000001


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_void_p),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _JOBOBJECT_BASIC_PROCESS_ID_LIST(ctypes.Structure):
    # A 1-element ProcessIdList is deliberately enough: QueryInformationJobObject
    # documents that NumberOfAssignedProcesses always reports the TRUE total
    # membership count even when the buffer is too small to hold every PID
    # (ERROR_MORE_DATA) -- we only need "is it zero or not", never the actual
    # PIDs, so there is no reason to size this for an unbounded list.
    _fields_ = [
        ("NumberOfAssignedProcesses", wintypes.DWORD),
        ("NumberOfProcessIdsInList", wintypes.DWORD),
        ("ProcessIdList", ctypes.c_size_t * 1),
    ]


class _THREADENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", ctypes.c_long),
        ("tpDeltaPri", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
    ]


def _kernel32():
    """Lazily resolves kernel32 -- only ever actually invoked on a real
    Windows host. Referencing ctypes.WinDLL at module import time would
    raise on non-Windows Python builds (ctypes.WinDLL doesn't exist there
    at all -- confirmed on this dev host), exactly the same class of bug
    A1's CREATE_NEW_PROCESS_GROUP fallback exists to avoid."""
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _build_windows_popen_kwargs(command: str, cwd: str) -> dict:
    """Host-native string-command execution -- Windows' own subprocess
    shell=True runs the command through cmd.exe, no bash/POSIX shell
    involved. CREATE_NEW_PROCESS_GROUP isolates the tree from console
    signals meant for the parent Lumina process (independent of, and
    orthogonal to, Job-Object-based tree-kill semantics below).
    CREATE_SUSPENDED is the race-freedom primitive for Job Object
    assignment (Part 8): the child cannot execute a single instruction --
    including spawning a child of its own -- until _spawn_windows() below
    has assigned it to the Job Object and explicitly resumed it."""
    return dict(
        args=command,
        shell=True,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_CREATE_NEW_PROCESS_GROUP | _CREATE_SUSPENDED,
    )


def _build_windows_argv_popen_kwargs(argv, cwd: str, env=None) -> dict:
    """Direct argv execution with the same suspended Job assignment.

    ``args`` remains a list and ``shell`` remains false.  Python's Windows
    subprocess implementation necessarily serializes that list for the native
    CreateProcessW API, but this module never creates shell command text and
    never routes it through cmd.exe.
    """
    return dict(
        args=list(argv),
        shell=False,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_CREATE_NEW_PROCESS_GROUP | _CREATE_SUSPENDED,
    )


def _create_job_object():
    """Creates a new, unnamed Job Object and configures
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE on it: a crash-safety net only --
    if this handle is ever closed (including by Lumina's own process
    exiting unexpectedly) while the job still has live members, Windows
    itself guarantees the whole tree dies. This is NOT the primary
    termination mechanism (release_control() below only ever closes this
    handle once the tree is already confirmed empty via tree_is_alive());
    it exists purely so an uncatchable Lumina crash doesn't orphan a
    managed tree on Windows any worse than the already-documented POSIX
    crash limitation (a Python process cannot guarantee cleanup after an
    uncatchable kill, on any platform)."""
    k32 = _kernel32()
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    handle = k32.CreateJobObjectW(None, None)
    if not handle:
        raise ProcessControlError(f"CreateJobObjectW failed: error {_get_last_error()}")

    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    ok = k32.SetInformationJobObject(
        handle, _JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
    )
    if not ok:
        err = _get_last_error()
        k32.CloseHandle(handle)
        raise ProcessControlError(f"SetInformationJobObject failed: error {err}")
    return handle


def _find_main_thread_id(k32, pid: int) -> int:
    """CREATE_SUSPENDED guarantees exactly one thread exists in the child
    at this point (its not-yet-resumed primary thread). There is no
    public 'get the main thread handle from just a PID' API, so this
    enumerates via a toolhelp snapshot -- a fully public, documented
    Win32 mechanism -- rather than reaching into subprocess.Popen's
    private _handle attribute, which the corrective spec explicitly
    disallows absent a STOP-and-report."""
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = k32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
    if not snapshot or snapshot == -1:
        raise ProcessControlError(f"CreateToolhelp32Snapshot failed: error {_get_last_error()}")
    try:
        entry = _THREADENTRY32()
        entry.dwSize = ctypes.sizeof(_THREADENTRY32)
        # ctypes.pointer() (not byref()) deliberately -- byref() produces a
        # lightweight reference only meant for immediate use as a real FFI
        # call argument; it can't be dereferenced afterward. Tests here
        # stand in for kernel32 with plain Python fakes (no real FFI call
        # occurs), so the out-parameter needs a pointer whose .contents a
        # fake can actually write to.
        entry_ptr = ctypes.pointer(entry)
        if not k32.Thread32First(snapshot, entry_ptr):
            raise ProcessControlError(f"Thread32First failed: error {_get_last_error()}")
        while True:
            if entry.th32OwnerProcessID == pid:
                return entry.th32ThreadID
            if not k32.Thread32Next(snapshot, entry_ptr):
                break
        raise ProcessControlError(f"could not locate primary thread for pid {pid}")
    finally:
        k32.CloseHandle(snapshot)


def _spawn_windows_from_kwargs(kwargs: dict):
    """Race-free Job Object assignment via CREATE_SUSPENDED +
    AssignProcessToJobObject + ResumeThread -- see module docstring for
    why this was chosen over PROC_THREAD_ATTRIBUTE_JOB_LIST's raw
    CreateProcessW path. subprocess.Popen still owns all pipe/stdin/
    stdout/stderr/wait/poll/returncode plumbing; only tree lifetime is
    Job-Object-based."""
    k32 = _kernel32()
    job_handle = _create_job_object()
    try:
        proc = subprocess.Popen(**kwargs)

        k32.OpenProcess.restype = wintypes.HANDLE
        proc_handle = k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, proc.pid)
        if not proc_handle:
            err = _get_last_error()
            proc.kill()
            raise ProcessControlError(f"OpenProcess failed: error {err}")
        try:
            k32.AssignProcessToJobObject.restype = wintypes.BOOL
            if not k32.AssignProcessToJobObject(job_handle, proc_handle):
                err = _get_last_error()
                proc.kill()
                raise ProcessControlError(f"AssignProcessToJobObject failed: error {err}")
        finally:
            k32.CloseHandle(proc_handle)

        thread_id = _find_main_thread_id(k32, proc.pid)
        k32.OpenThread.restype = wintypes.HANDLE
        thread_handle = k32.OpenThread(_THREAD_SUSPEND_RESUME, False, thread_id)
        if not thread_handle:
            err = _get_last_error()
            proc.kill()
            raise ProcessControlError(f"OpenThread failed: error {err}")
        try:
            if k32.ResumeThread(thread_handle) == 0xFFFFFFFF:
                err = _get_last_error()
                proc.kill()
                raise ProcessControlError(f"ResumeThread failed: error {err}")
        finally:
            k32.CloseHandle(thread_handle)
    except Exception:
        k32.CloseHandle(job_handle)
        raise

    return proc, {"backend": "windows", "job_handle": job_handle, "pid": proc.pid, "retired": False}


def _spawn_windows(command: str, cwd: str):
    return _spawn_windows_from_kwargs(_build_windows_popen_kwargs(command, cwd))


def _spawn_windows_argv(argv, cwd: str, env=None):
    return _spawn_windows_from_kwargs(
        _build_windows_argv_popen_kwargs(argv, cwd, env)
    )


def _tree_is_alive_windows(control_metadata: dict) -> bool:
    if control_metadata.get("retired"):
        return False
    job_handle = control_metadata.get("job_handle")
    if not job_handle:
        return False
    k32 = _kernel32()
    id_list = _JOBOBJECT_BASIC_PROCESS_ID_LIST()
    id_list_ptr = ctypes.pointer(id_list)  # pointer(), not byref() -- see
                                            # _find_main_thread_id's comment;
                                            # fakes need a dereferenceable
                                            # out-parameter to write through.
    k32.QueryInformationJobObject.restype = wintypes.BOOL
    k32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.POINTER(_JOBOBJECT_BASIC_PROCESS_ID_LIST),
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ]
    ok = k32.QueryInformationJobObject(
        job_handle, _JobObjectBasicProcessIdList, id_list_ptr, ctypes.sizeof(id_list), None
    )
    if not ok:
        err = _get_last_error()
        if err != _ERROR_MORE_DATA:
            print(f"[PROCESS_CONTROL] unexpected error querying Job Object "
                  f"membership ({err}) -- treating job as still alive", flush=True)
            return True
        # ERROR_MORE_DATA just means our 1-slot ProcessIdList couldn't hold
        # every PID -- NumberOfAssignedProcesses is still the true count.
    return id_list.NumberOfAssignedProcesses > 0


def _terminate_tree_windows(proc, control_metadata: dict):
    if control_metadata.get("retired"):
        return
    job_handle = control_metadata.get("job_handle")
    if job_handle:
        k32 = _kernel32()
        k32.TerminateJobObject.restype = wintypes.BOOL
        try:
            k32.TerminateJobObject(job_handle, 1)
        except OSError:
            pass
    try:
        proc.kill()  # fallback: guarantees the direct child dies even if
    except OSError:  # TerminateJobObject couldn't reach it for any reason
        pass


def _release_control_windows(control_metadata: dict):
    """Called exactly once, only after the manager's watcher has itself
    observed tree_is_alive() go False. Retires the control identity so no
    later stray call can act on this handle again, then closes it."""
    control_metadata["retired"] = True
    job_handle = control_metadata.get("job_handle")
    if job_handle:
        k32 = _kernel32()
        k32.CloseHandle(job_handle)
    control_metadata["job_handle"] = None


# ── Platform-neutral public API ─────────────────────────────────────────

def spawn(command: str, cwd: str):
    """Spawn `command` as a managed shell subprocess in `cwd`. Returns
    (Popen, control_metadata: dict). control_metadata is opaque outside
    this module -- only tree_is_alive()/terminate_tree()/release_control()
    below interpret it. Raises ProcessControlError if this host has no
    usable mechanism to run the command, or if Job Object setup fails on
    Windows."""
    if backend_name() == "windows":
        return _spawn_windows(command, cwd)
    return _spawn_posix(command, cwd)


def spawn_argv(argv, cwd: str, env=None):
    """Spawn direct argv inside the same managed process tree as ``spawn``.

    No shell is involved and argv is passed as a sequence without joining,
    quoting, or reparsing.  The returned control metadata has the identical
    lifecycle contract consumed by tree_is_alive(), terminate_tree(), and
    release_control().
    """
    if backend_name() == "windows":
        return _spawn_windows_argv(argv, cwd, env)
    return _spawn_posix_argv(argv, cwd, env)


def has_exited(proc) -> bool:
    """Leader-only fact -- proc.poll() is not None. Retained as a simple
    diagnostic/return-code-availability helper; the manager MUST NOT use
    this to decide whether managed work has finished (see tree_is_alive()
    below) -- that was the confirmed CODING-04A1 landing blocker."""
    return proc.poll() is not None


def tree_is_alive(control_metadata: dict) -> bool:
    """Is any process still alive in this job's managed control group
    (POSIX process group / Windows Job Object)? This -- never the
    leader's proc.poll() -- is the authoritative answer to "is the
    managed job still running." A retired control identity (see
    release_control()) always answers False."""
    if control_metadata.get("backend") == "windows":
        return _tree_is_alive_windows(control_metadata)
    return _tree_is_alive_posix(control_metadata)


def terminate_tree(proc, control_metadata: dict):
    """Best-effort whole-tree hard termination. Deliberately does NOT
    short-circuit on the leader's proc.poll() -- a leader that has
    already exited while descendants remain alive is exactly the
    scenario this corrective exists to fix. The only thing that skips
    this call is an already-retired control identity (control_metadata
    marked "retired" by release_control()), which protects against ever
    signalling a PGID/handle that may since have been reused for an
    unrelated process/job."""
    if control_metadata.get("retired"):
        return
    if control_metadata.get("backend") == "windows":
        _terminate_tree_windows(proc, control_metadata)
    else:
        _terminate_tree_posix(proc, control_metadata)


def release_control(control_metadata: dict):
    """Retires this control identity and releases any platform resource
    it holds (POSIX: nothing to release, just marks the PGID inert;
    Windows: closes the Job Object handle). MUST be called exactly once,
    and only after the caller has itself observed tree_is_alive() return
    False -- calling this while the tree may still be alive would let a
    later reused PGID/handle be silently ignored by terminate_tree()."""
    if control_metadata.get("backend") == "windows":
        _release_control_windows(control_metadata)
    else:
        _release_control_posix(control_metadata)
