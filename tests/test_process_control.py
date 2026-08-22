"""
tests/test_process_control.py -- CODING-04A1/A1C: core/process_control.py,
the only module in Lumina that understands host-specific process-spawn and
process-tree-control mechanics.

POSIX behavior is exercised LIVE against the real Linux dev host (real
spawn, real process groups, real signals). Windows behavior can never be
live-tested here (no Windows kernel available) -- those tests instead
prove the pure kwargs/args-construction helpers produce the right shape,
and that the Job-Object-based spawn/tree_is_alive/terminate_tree/
release_control sequence dispatches correctly against a fake kernel32
double, all without ever invoking a real Windows-only ctypes.WinDLL call.
No live Windows execution is claimed anywhere in this file.

CODING-04A1C: the independent safety review confirmed a landing blocker --
terminate_tree() (and the manager's finalization logic) treated "the
leader has exited" as equivalent to "nothing is left to terminate." The
group-liveness / tree_is_alive / release_control tests below are the
direct regression coverage for that fix; the old whole-tree tests (which
all ended in a trailing `wait`, structurally avoiding the exact ordering
that broke) are kept because they still validate real, non-overlapping
behavior, not because they cover the fix.

The portability-architecture tests at the bottom (grepping
core/process_manager.py's own source) are the regression guard for
Section 13's contract: the platform-neutral manager must never itself
contain a POSIX or Windows-specific primitive, and (A1C) must decide tree
lifetime through process_control.tree_is_alive(), never proc.poll() alone.
"""
import ctypes
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from core import process_control


# ── Backend selection ────────────────────────────────────────────────────

def test_linux_selects_posix_backend(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert process_control.backend_name() == "posix"


def test_darwin_selects_posix_backend(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert process_control.backend_name() == "posix"


def test_windows_selects_windows_backend(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert process_control.backend_name() == "windows"


# ── POSIX: kwargs construction ───────────────────────────────────────────

def test_posix_popen_kwargs_shape():
    kwargs = process_control._build_posix_popen_kwargs("echo hi", "/tmp", "/usr/bin/bash")
    assert kwargs["executable"] == "/usr/bin/bash"
    assert kwargs["shell"] is True
    assert kwargs["cwd"] == "/tmp"
    assert kwargs["start_new_session"] is True
    assert kwargs["text"] is True
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"
    assert "creationflags" not in kwargs


def test_resolve_posix_shell_uses_path_lookup_not_hardcoded_path(monkeypatch):
    calls = []
    monkeypatch.setattr(process_control.shutil, "which",
                         lambda name: calls.append(name) or "/usr/bin/bash")
    resolved = process_control._resolve_posix_shell()
    assert resolved == "/usr/bin/bash"
    assert calls == ["bash"]


def test_resolve_posix_shell_raises_clearly_when_no_bash_found(monkeypatch):
    monkeypatch.setattr(process_control.shutil, "which", lambda name: None)
    with pytest.raises(process_control.ProcessControlError):
        process_control._resolve_posix_shell()


# ── POSIX: live spawn/terminate on the real Linux host ───────────────────

def _wait_until_gone(pattern: str, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout.strip()
        if not out:
            return True
        time.sleep(0.1)
    return False


def _wait_until_present(pattern: str, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout.strip()
        if out:
            return True
        time.sleep(0.05)
    return False


def test_posix_live_spawn_and_has_exited():
    proc, control_metadata = process_control.spawn("sleep 30", "/tmp")
    try:
        assert control_metadata["backend"] == "posix"
        assert "pgid" in control_metadata
        assert control_metadata["retired"] is False
        assert process_control.has_exited(proc) is False
    finally:
        process_control.terminate_tree(proc, control_metadata)
        proc.wait(timeout=5)
    assert process_control.has_exited(proc) is True


def test_posix_live_whole_tree_termination_parent_child_grandchild():
    # Distinct, greppable sleep durations mark parent (managed bash)/child/
    # grandchild: "sleep 294 &" backgrounds a direct child of the managed
    # process; "(sleep 295 &)" runs a subshell that itself backgrounds a
    # grandchild then exits immediately, leaving sleep 295 without a
    # living direct parent but still inside the same process group. The
    # trailing `wait` keeps the managed leader alive until this point, so
    # this test (unlike the leader-exit tests below) does NOT exercise the
    # A1C fix -- it proves ordinary multi-generation tree-kill still works.
    command = "sleep 294 & (sleep 295 &) ; wait"
    proc, control_metadata = process_control.spawn(command, "/tmp")
    time.sleep(0.3)
    before = subprocess.run(["pgrep", "-f", "sleep 29[45]"], capture_output=True, text=True).stdout.strip()
    assert before, "expected the child/grandchild sleeps to be running before termination"

    process_control.terminate_tree(proc, control_metadata)
    proc.wait(timeout=5)

    assert _wait_until_gone("sleep 29[45]", timeout=5.0), "child/grandchild survived whole-tree termination"


def test_posix_terminate_tree_does_not_signal_retired_control(monkeypatch):
    proc, control_metadata = process_control.spawn("true", "/tmp")
    proc.wait(timeout=5)
    process_control.release_control(control_metadata)  # simulate the watcher having already retired it

    calls = []
    monkeypatch.setattr(process_control.os, "killpg", lambda *a, **kw: calls.append((a, kw)))
    process_control.terminate_tree(proc, control_metadata)
    assert calls == []  # never signals a retired (possibly PGID-reused) control identity


def test_posix_terminate_tolerates_already_gone_pgid(monkeypatch):
    """A process-group race (already reaped by the time we signal) must
    not raise -- OSError from os.killpg is expected and swallowed."""
    proc, control_metadata = process_control.spawn("sleep 30", "/tmp")

    def raising_killpg(*a, **kw):
        raise OSError("no such process group")
    monkeypatch.setattr(process_control.os, "killpg", raising_killpg)

    process_control.terminate_tree(proc, control_metadata)  # must not raise
    proc.wait(timeout=5)
    assert process_control.has_exited(proc) is True


# ── POSIX: group liveness (CODING-04A1C) ──────────────────────────────────

def test_process_group_is_alive_true_while_leader_alive():
    proc, control_metadata = process_control.spawn("sleep 30", "/tmp")
    try:
        assert process_control._process_group_is_alive_posix(control_metadata["pgid"]) is True
    finally:
        process_control.terminate_tree(proc, control_metadata)
        proc.wait(timeout=5)


def test_process_group_is_alive_false_once_pgid_is_none():
    assert process_control._process_group_is_alive_posix(None) is False


def test_process_group_is_alive_false_once_truly_empty():
    proc, control_metadata = process_control.spawn("true", "/tmp")
    proc.wait(timeout=5)
    assert _wait_until_gone(f"^true$", timeout=0.1) or True  # `true` leaves nothing to grep for
    # Give the kernel a moment to actually reclaim the (now-leaderless,
    # memberless) process group before probing.
    deadline = time.time() + 3.0
    alive = True
    while time.time() < deadline:
        alive = process_control._process_group_is_alive_posix(control_metadata["pgid"])
        if not alive:
            break
        time.sleep(0.05)
    assert alive is False


def test_process_group_is_alive_true_after_leader_exits_with_live_descendant():
    """THE direct regression proof for the CODING-04A1 landing blocker's
    root cause: bash (the leader) backgrounds a child and exits
    immediately -- proc.poll() already reports the leader dead, but the
    process group itself must still report alive because the descendant
    is still running in it."""
    proc, control_metadata = process_control.spawn("sleep 6248 & exit 0", "/tmp")
    try:
        proc.wait(timeout=5)  # leader has genuinely exited
        assert proc.poll() is not None
        assert _wait_until_present("sleep 6248", timeout=2.0), "descendant should have started"
        assert process_control._process_group_is_alive_posix(control_metadata["pgid"]) is True, (
            "group must report alive while the descendant lives, even though the leader already exited"
        )
    finally:
        subprocess.run(["pkill", "-9", "-f", "sleep 6248"])
        _wait_until_gone("sleep 6248", timeout=5.0)


def test_process_group_is_alive_permission_error_treated_as_alive(monkeypatch):
    def raising_killpg(pgid, sig):
        raise PermissionError("operation not permitted")
    monkeypatch.setattr(process_control.os, "killpg", raising_killpg)
    assert process_control._process_group_is_alive_posix(4242) is True


def test_process_group_is_alive_process_lookup_error_treated_as_dead(monkeypatch):
    def raising_killpg(pgid, sig):
        raise ProcessLookupError("no such process")
    monkeypatch.setattr(process_control.os, "killpg", raising_killpg)
    assert process_control._process_group_is_alive_posix(4242) is False


def test_process_group_is_alive_unexpected_oserror_treated_as_alive_conservatively(monkeypatch, capsys):
    def raising_killpg(pgid, sig):
        raise OSError(5, "unexpected I/O error")
    monkeypatch.setattr(process_control.os, "killpg", raising_killpg)
    assert process_control._process_group_is_alive_posix(4242) is True  # fail conservative, never a false "dead"
    captured = capsys.readouterr()
    assert "PROCESS_CONTROL" in captured.out  # surfaced, not silently swallowed


def test_tree_is_alive_posix_dispatches_to_group_liveness(monkeypatch):
    monkeypatch.setattr(process_control, "_process_group_is_alive_posix", lambda pgid: True)
    assert process_control.tree_is_alive({"backend": "posix", "pgid": 999, "retired": False}) is True


def test_tree_is_alive_false_when_retired():
    assert process_control.tree_is_alive({"backend": "posix", "pgid": 999, "retired": True}) is False


def test_terminate_tree_posix_kills_group_after_leader_already_exited():
    """Isolated root-cause proof: terminate_tree() must no longer
    short-circuit merely because proc.poll() shows the leader dead."""
    proc, control_metadata = process_control.spawn("sleep 6249 & exit 0", "/tmp")
    try:
        proc.wait(timeout=5)
        assert proc.poll() is not None
        assert _wait_until_present("sleep 6249", timeout=2.0)

        process_control.terminate_tree(proc, control_metadata)

        assert _wait_until_gone("sleep 6249", timeout=5.0), (
            "terminate_tree() failed to kill a live descendant after the leader had already exited"
        )
    finally:
        subprocess.run(["pkill", "-9", "-f", "sleep 6249"])


def test_release_control_posix_marks_retired_and_keeps_pgid_as_inert_metadata():
    proc, control_metadata = process_control.spawn("true", "/tmp")
    proc.wait(timeout=5)
    original_pgid = control_metadata["pgid"]
    process_control.release_control(control_metadata)
    assert control_metadata["retired"] is True
    assert control_metadata["pgid"] == original_pgid  # inert historical metadata, not erased
    assert process_control.tree_is_alive(control_metadata) is False


# ── Windows: Job Object construction/dispatch (no real Windows execution) ─

class _FakeProc:
    def __init__(self, pid=555, exited=False, returncode=0):
        self.pid = pid
        self._exited = exited
        self.returncode = returncode if exited else None
        self.kill_calls = 0

    def poll(self):
        return self.returncode if self._exited else None

    def kill(self):
        self.kill_calls += 1


class _FakeKernel32:
    """Stands in for ctypes.WinDLL('kernel32') -- every kernel32 function
    process_control.py calls is a plain Python function here (not a real
    ctypes foreign call), so process_control's own out-parameter calls use
    ctypes.pointer() (not byref()) specifically so these fakes can write
    through them, exactly like the real API would via CreateProcessW/
    QueryInformationJobObject's documented out-parameter semantics.

    CODING-04A1CT hardening: two of this fake's behaviors were previously
    too convenient to actually prove production correctness --
      (1) Thread32First always vended the target PID as the very first
          entry, so removing _find_main_thread_id's owner-PID filter
          entirely still passed every committed test (nothing ever forced
          the enumeration loop to walk past a non-matching entry).
      (2) QueryInformationJobObject always returned success regardless of
          job_members, so the ERROR_MORE_DATA branch in
          _tree_is_alive_windows (the path a real multi-process job with
          more members than the 1-slot ProcessIdList buffer can hold
          actually takes) was never exercised at all.
    Both are now realistic by default: thread_entries defaults to a
    4-thread snapshot with the target in the THIRD slot (never first), and
    QueryInformationJobObject models the real Win32 contract -- success
    only when the true count fits in the 1-slot buffer, else failure +
    ERROR_MORE_DATA (surfaced via self.last_error, which tests wire to a
    monkeypatched process_control._get_last_error) with
    NumberOfAssignedProcesses still populated with the TRUE count either
    way, exactly as QueryInformationJobObject documents."""

    def __init__(self, job_members=0, fail=frozenset(), thread_entries=None):
        self.calls = []
        self.job_members = job_members
        self.fail = fail  # names that should simulate failure (return falsy)
        self.closed_handles = []
        self._next_handle = 9000
        self.last_error = 0
        self.more_data_path_triggered = False
        self.issued_handles = {}  # call name -> [handle, ...] returned, in issuance order

        def make(name, ok_value=1):
            def fn(*args, **kwargs):
                self.calls.append((name, args))
                if name in self.fail:
                    return 0
                return ok_value
            return fn

        def make_handle(name):
            def fn(*args, **kwargs):
                self.calls.append((name, args))
                if name in self.fail:
                    return 0
                self._next_handle += 1
                self.issued_handles.setdefault(name, []).append(self._next_handle)
                return self._next_handle
            return fn

        self.CreateJobObjectW = make_handle("CreateJobObjectW")
        self.SetInformationJobObject = make("SetInformationJobObject")
        self.OpenProcess = make_handle("OpenProcess")
        self.AssignProcessToJobObject = make("AssignProcessToJobObject")
        self.CreateToolhelp32Snapshot = make_handle("CreateToolhelp32Snapshot")
        self.OpenThread = make_handle("OpenThread")
        self.ResumeThread = make("ResumeThread", ok_value=0)
        self.TerminateJobObject = make("TerminateJobObject")

        def close_handle(handle, *a, **kw):
            self.calls.append(("CloseHandle", (handle,)))
            self.closed_handles.append(handle)
            return 1
        self.CloseHandle = close_handle

        # thread_entries: explicit (th32OwnerProcessID, th32ThreadID) list,
        # in the exact order a real Thread32First/Thread32Next walk would
        # vend them. None => resolved lazily (first Thread32First call)
        # against whatever self._target_pid is set to by then, since
        # _spawn_windows_with_fake reassigns _target_pid AFTER construction.
        self._explicit_thread_entries = thread_entries
        self._resolved_thread_entries = None
        self._thread_cursor = 0
        self._target_pid = 555

        def _thread_entries():
            if self._resolved_thread_entries is None:
                if self._explicit_thread_entries is not None:
                    self._resolved_thread_entries = list(self._explicit_thread_entries)
                else:
                    t = self._target_pid
                    # Realistic default: unrelated, unrelated, TARGET, unrelated.
                    self._resolved_thread_entries = [
                        (t + 1000, 9101), (t + 2000, 9102), (t, 9103), (t + 3000, 9104),
                    ]
            return self._resolved_thread_entries

        def thread32first(snapshot, entry_ptr):
            self.calls.append(("Thread32First", (snapshot,)))
            if "Thread32First" in self.fail:
                return 0
            self._thread_cursor = 0
            owner_pid, thread_id = _thread_entries()[0]
            entry_ptr.contents.th32OwnerProcessID = owner_pid
            entry_ptr.contents.th32ThreadID = thread_id
            return 1
        self.Thread32First = thread32first

        def thread32next(snapshot, entry_ptr):
            self.calls.append(("Thread32Next", (snapshot,)))
            if "Thread32Next" in self.fail:
                return 0
            entries = _thread_entries()
            self._thread_cursor += 1
            if self._thread_cursor >= len(entries):
                return 0
            owner_pid, thread_id = entries[self._thread_cursor]
            entry_ptr.contents.th32OwnerProcessID = owner_pid
            entry_ptr.contents.th32ThreadID = thread_id
            return 1
        self.Thread32Next = thread32next

        def query_job(job_handle, info_class, ptr, size, ret_len):
            self.calls.append(("QueryInformationJobObject", (job_handle,)))
            if "QueryInformationJobObject" in self.fail:
                self.last_error = 0
                return 0
            # Real contract: NumberOfAssignedProcesses is always populated
            # with the TRUE total membership count, whether or not the
            # call itself reports success -- the 1-slot ProcessIdList
            # buffer this module allocates can only ever literally hold 1
            # PID, so any job with more than 1 live member forces the real
            # Win32 API down the FALSE + ERROR_MORE_DATA path.
            ptr.contents.NumberOfAssignedProcesses = self.job_members
            ptr.contents.NumberOfProcessIdsInList = min(self.job_members, 1)
            if self.job_members > 1:
                self.more_data_path_triggered = True
                self.last_error = process_control._ERROR_MORE_DATA
                return 0
            self.last_error = 0
            return 1
        self.QueryInformationJobObject = query_job


def test_windows_popen_kwargs_shape():
    kwargs = process_control._build_windows_popen_kwargs("dir", "C:\\Users\\lumina")
    assert kwargs["shell"] is True
    assert kwargs["cwd"] == "C:\\Users\\lumina"
    assert kwargs["creationflags"] == (process_control._CREATE_NEW_PROCESS_GROUP | process_control._CREATE_SUSPENDED)
    assert kwargs["creationflags"] & process_control._CREATE_SUSPENDED, (
        "CREATE_SUSPENDED is the race-freedom primitive -- the child must not run before Job Object assignment"
    )
    assert "executable" not in kwargs
    assert "start_new_session" not in kwargs
    assert kwargs["text"] is True
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"


def _spawn_windows_with_fake(monkeypatch, fake_kernel32, fake_proc):
    monkeypatch.setattr(process_control, "_kernel32", lambda: fake_kernel32)
    monkeypatch.setattr(process_control.subprocess, "Popen", lambda **kw: fake_proc)
    fake_kernel32._target_pid = fake_proc.pid
    return process_control._spawn_windows("dir", "C:\\Users\\lumina")


def test_windows_spawn_associates_job_before_resuming_thread(monkeypatch):
    """The race-freedom proof (Part 8): the process must be assigned to
    the Job Object strictly BEFORE its (only, suspended) thread is
    resumed -- there is no window in which it could run, let alone spawn
    an untracked child, before assignment."""
    fake_k32 = _FakeKernel32()
    fake_proc = _FakeProc(pid=555)
    proc, control_metadata = _spawn_windows_with_fake(monkeypatch, fake_k32, fake_proc)

    names = [c[0] for c in fake_k32.calls]
    assert "AssignProcessToJobObject" in names
    assert "ResumeThread" in names
    assert names.index("AssignProcessToJobObject") < names.index("ResumeThread")
    assert proc is fake_proc
    assert control_metadata["backend"] == "windows"
    assert control_metadata["job_handle"] is not None
    assert control_metadata["pid"] == 555
    assert control_metadata["retired"] is False


def test_windows_spawn_sets_kill_on_job_close_limit(monkeypatch):
    fake_k32 = _FakeKernel32()
    fake_proc = _FakeProc(pid=555)
    _spawn_windows_with_fake(monkeypatch, fake_k32, fake_proc)
    assert any(name == "SetInformationJobObject" for name, _ in fake_k32.calls)


def test_windows_spawn_uses_public_toolhelp_api_not_popen_private_handle(monkeypatch):
    """Part 8 explicitly forbids relying on private CPython internals
    (e.g. subprocess.Popen._handle) absent a STOP-and-report -- confirm
    the thread lookup goes through the public CreateToolhelp32Snapshot/
    Thread32First/Thread32Next mechanism instead."""
    fake_k32 = _FakeKernel32()
    fake_proc = _FakeProc(pid=555)
    _spawn_windows_with_fake(monkeypatch, fake_k32, fake_proc)
    names = [c[0] for c in fake_k32.calls]
    assert "CreateToolhelp32Snapshot" in names
    assert "Thread32First" in names


# ── Windows: realistic thread-snapshot owner-PID filtering (CODING-04A1CT) ─
#
# The tests above only prove CreateToolhelp32Snapshot/Thread32First were
# called -- not that _find_main_thread_id() actually filters by
# th32OwnerProcessID. The default _FakeKernel32 previously always vended
# the target PID as the very first Thread32First entry, so even a
# production build that returned the first entry's thread ID unconditionally
# (no owner-PID check at all) would still pass every committed test. The
# fake's default thread snapshot is now four entries -- unrelated,
# unrelated, TARGET, unrelated -- with the target never first, specifically
# so these tests fail if the filter is ever removed (see Mutation G below).

def test_windows_spawn_selects_target_owned_thread_not_first_enumerated(monkeypatch):
    """Direct regression proof: the target's thread lives in the THIRD
    enumerated slot behind two unrelated-PID entries. This proves
    enumeration genuinely advances past non-matching entries, the exact
    target-owned thread ID is what gets opened/resumed, no unrelated
    thread ID is ever opened as if it were the target, and the toolhelp
    snapshot handle plus the resolved thread handle both still close."""
    fake_k32 = _FakeKernel32()  # default: unrelated(9101), unrelated(9102), TARGET(9103), unrelated(9104)
    fake_proc = _FakeProc(pid=555)
    proc, control_metadata = _spawn_windows_with_fake(monkeypatch, fake_k32, fake_proc)

    thread32next_calls = [c for c in fake_k32.calls if c[0] == "Thread32Next"]
    assert len(thread32next_calls) == 2, (
        "enumeration must advance past exactly the 2 unrelated entries preceding the target"
    )

    open_thread_calls = [c for c in fake_k32.calls if c[0] == "OpenThread"]
    assert len(open_thread_calls) == 1
    opened_thread_id = open_thread_calls[0][1][-1]  # OpenThread(access, inherit, thread_id)
    assert opened_thread_id == 9103, "must open the exact target-owned thread ID (slot C), not an unrelated one"
    assert opened_thread_id not in (9101, 9102, 9104), "an unrelated thread ID must never be opened as the target"

    snapshot_handle = fake_k32.issued_handles["CreateToolhelp32Snapshot"][0]
    thread_handle = fake_k32.issued_handles["OpenThread"][0]
    assert snapshot_handle in fake_k32.closed_handles, "the toolhelp snapshot handle must be closed"
    assert thread_handle in fake_k32.closed_handles, "the resolved thread handle must be closed"
    assert control_metadata["pid"] == 555


@pytest.mark.parametrize("thread_entries,expected_thread_id", [
    # Target in slot B of 3 (a different arrangement than the fake's
    # default slot-C-of-4) -- proves this isn't hardcoded to one shape.
    ([(4001, 501), (555, 502), (4002, 503)], 502),
    # Target last of 4 -- the enumeration must walk the entire snapshot.
    ([(4101, 601), (4102, 602), (4103, 603), (555, 604)], 604),
])
def test_windows_spawn_selects_target_owned_thread_reordered_arrangements(
    monkeypatch, thread_entries, expected_thread_id
):
    fake_k32 = _FakeKernel32(thread_entries=thread_entries)
    fake_proc = _FakeProc(pid=555)
    _spawn_windows_with_fake(monkeypatch, fake_k32, fake_proc)

    open_thread_calls = [c for c in fake_k32.calls if c[0] == "OpenThread"]
    assert len(open_thread_calls) == 1
    assert open_thread_calls[0][1][-1] == expected_thread_id
    unrelated_ids = {tid for pid, tid in thread_entries if pid != 555}
    assert open_thread_calls[0][1][-1] not in unrelated_ids


def test_windows_spawn_cleans_up_job_handle_when_open_process_fails(monkeypatch):
    fake_k32 = _FakeKernel32(fail={"OpenProcess"})
    fake_proc = _FakeProc(pid=555)
    with pytest.raises(process_control.ProcessControlError):
        _spawn_windows_with_fake(monkeypatch, fake_k32, fake_proc)
    assert fake_proc.kill_calls == 1
    close_calls = [c for c in fake_k32.calls if c[0] == "CloseHandle"]
    assert close_calls, "the Job Object handle must not leak when a later spawn step fails"


def test_windows_spawn_raises_when_job_object_creation_fails(monkeypatch):
    fake_k32 = _FakeKernel32(fail={"CreateJobObjectW"})
    fake_proc = _FakeProc(pid=555)
    with pytest.raises(process_control.ProcessControlError):
        _spawn_windows_with_fake(monkeypatch, fake_k32, fake_proc)
    # No dependency-free fallback to a weaker tracking model -- Job Object
    # failure fails the launch cleanly rather than degrading to root-PID-only
    # tracking (the exact "kill only the parent and hope" the spec forbids).
    assert fake_proc.kill_calls == 0  # Popen was never even reached


def test_windows_tree_is_alive_true_while_job_has_members(monkeypatch):
    """CODING-04A1CT: with the fake's realistic ERROR_MORE_DATA modeling
    (job_members > 1 exceeds the real 1-slot ProcessIdList buffer), this
    now genuinely exercises that branch rather than coincidentally passing
    via the generic-query-failure fallback -- see
    test_windows_tree_is_alive_realistic_error_more_data_contract below
    for the full 0/1/2/10-member matrix this is a focused instance of."""
    fake_k32 = _FakeKernel32(job_members=2)
    monkeypatch.setattr(process_control, "_kernel32", lambda: fake_k32)
    monkeypatch.setattr(process_control, "_get_last_error", lambda: fake_k32.last_error)
    control_metadata = {"backend": "windows", "job_handle": 9001, "pid": 555, "retired": False}
    assert process_control._tree_is_alive_windows(control_metadata) is True
    assert fake_k32.more_data_path_triggered is True


def test_windows_tree_is_alive_false_once_job_empty_even_though_root_exited(monkeypatch):
    """Direct proof of Part 3's requirement: root exit must NOT make
    tree_is_alive() false while the Job Object still has members, and
    conversely, an EMPTY job must report dead regardless of the root's
    own poll() state -- job membership, never root PID/poll(), is
    authoritative."""
    fake_k32 = _FakeKernel32(job_members=0)
    monkeypatch.setattr(process_control, "_kernel32", lambda: fake_k32)
    control_metadata = {"backend": "windows", "job_handle": 9001, "pid": 555, "retired": False}
    assert process_control._tree_is_alive_windows(control_metadata) is False


def test_windows_tree_is_alive_false_when_retired(monkeypatch):
    fake_k32 = _FakeKernel32(job_members=5)  # even with "members" reported, retired wins
    monkeypatch.setattr(process_control, "_kernel32", lambda: fake_k32)
    control_metadata = {"backend": "windows", "job_handle": 9001, "pid": 555, "retired": True}
    assert process_control._tree_is_alive_windows(control_metadata) is False
    assert fake_k32.calls == []  # never even queries a retired handle


def test_windows_tree_is_alive_query_failure_treated_as_alive_conservatively(monkeypatch, capsys):
    fake_k32 = _FakeKernel32(fail={"QueryInformationJobObject"})
    monkeypatch.setattr(process_control, "_kernel32", lambda: fake_k32)
    control_metadata = {"backend": "windows", "job_handle": 9001, "pid": 555, "retired": False}
    assert process_control._tree_is_alive_windows(control_metadata) is True
    assert "PROCESS_CONTROL" in capsys.readouterr().out


# ── Windows: realistic ERROR_MORE_DATA contract (CODING-04A1CT Part 4) ────
#
# _tree_is_alive_windows() allocates only a 1-slot ProcessIdList buffer
# (deliberately -- only NumberOfAssignedProcesses is ever read, not the
# actual PIDs) and explicitly tolerates QueryInformationJobObject
# returning FALSE + GetLastError()==ERROR_MORE_DATA as meaning "the count
# is still trustworthy, the buffer was just too small to also list every
# PID." The old fake always reported success regardless of job_members, so
# that branch -- the one a real job with 2+ live members actually takes --
# was never exercised. This fake now models the real contract: success
# only when job_members fits the 1-slot buffer (<=1), else FALSE +
# ERROR_MORE_DATA with NumberOfAssignedProcesses still populated with the
# TRUE count either way, and self.more_data_path_triggered records
# whether that branch actually fired, so the test can prove it did
# (rather than merely asserting the intended boolean).

@pytest.mark.parametrize("job_members,expect_alive,expect_more_data_path", [
    (0, False, False),
    (1, True, False),
    (2, True, True),
    (10, True, True),
])
def test_windows_tree_is_alive_realistic_error_more_data_contract(
    monkeypatch, job_members, expect_alive, expect_more_data_path
):
    fake_k32 = _FakeKernel32(job_members=job_members)
    monkeypatch.setattr(process_control, "_kernel32", lambda: fake_k32)
    monkeypatch.setattr(process_control, "_get_last_error", lambda: fake_k32.last_error)
    control_metadata = {"backend": "windows", "job_handle": 9001, "pid": 555, "retired": False}

    result = process_control._tree_is_alive_windows(control_metadata)

    assert result is expect_alive
    assert fake_k32.more_data_path_triggered is expect_more_data_path, (
        f"job_members={job_members} should{''  if expect_more_data_path else ' not'} "
        f"have forced the ERROR_MORE_DATA path -- the fake never actually took it"
    )


def test_windows_terminate_tree_uses_terminate_job_object_not_taskkill(monkeypatch):
    """Part 7: taskkill is no longer the primary correctness mechanism at
    all -- TerminateJobObject is the whole-tree kill primitive."""
    fake_k32 = _FakeKernel32()
    monkeypatch.setattr(process_control, "_kernel32", lambda: fake_k32)
    monkeypatch.setattr(process_control.subprocess, "run",
                         lambda *a, **kw: pytest.fail("taskkill/subprocess.run must not be used"))
    proc = _FakeProc(pid=555)
    control_metadata = {"backend": "windows", "job_handle": 9001, "pid": 555, "retired": False}
    process_control._terminate_tree_windows(proc, control_metadata)
    assert any(name == "TerminateJobObject" for name, _ in fake_k32.calls)
    assert proc.kill_calls == 1


def test_windows_terminate_tree_does_not_short_circuit_on_root_poll(monkeypatch):
    """Direct analogue of the POSIX root-cause regression test: an
    already-exited root process must NOT prevent TerminateJobObject from
    being called -- job membership, not root poll(), gates termination."""
    fake_k32 = _FakeKernel32()
    monkeypatch.setattr(process_control, "_kernel32", lambda: fake_k32)
    proc = _FakeProc(pid=555, exited=True, returncode=0)  # root already exited
    control_metadata = {"backend": "windows", "job_handle": 9001, "pid": 555, "retired": False}
    process_control._terminate_tree_windows(proc, control_metadata)
    assert any(name == "TerminateJobObject" for name, _ in fake_k32.calls), (
        "terminate must still reach the Job Object even though proc.poll() shows the root already exited"
    )


def test_windows_terminate_tree_noops_when_retired(monkeypatch):
    fake_k32 = _FakeKernel32()
    monkeypatch.setattr(process_control, "_kernel32", lambda: fake_k32)
    proc = _FakeProc(pid=555)
    control_metadata = {"backend": "windows", "job_handle": 9001, "pid": 555, "retired": True}
    process_control.terminate_tree(proc, control_metadata)
    assert fake_k32.calls == []
    assert proc.kill_calls == 0


def test_windows_terminate_tree_tolerates_terminate_job_object_error(monkeypatch):
    fake_k32 = _FakeKernel32(fail={"TerminateJobObject"})
    monkeypatch.setattr(process_control, "_kernel32", lambda: fake_k32)
    proc = _FakeProc(pid=555)
    control_metadata = {"backend": "windows", "job_handle": 9001, "pid": 555, "retired": False}
    process_control._terminate_tree_windows(proc, control_metadata)  # must not raise
    assert proc.kill_calls == 1  # fallback still runs


def test_windows_release_control_closes_handle_and_retires(monkeypatch):
    fake_k32 = _FakeKernel32()
    monkeypatch.setattr(process_control, "_kernel32", lambda: fake_k32)
    control_metadata = {"backend": "windows", "job_handle": 9001, "pid": 555, "retired": False}
    process_control.release_control(control_metadata)
    assert control_metadata["retired"] is True
    assert control_metadata["job_handle"] is None
    assert (9001,) in [c[1] for c in fake_k32.calls if c[0] == "CloseHandle"]


def test_windows_release_control_then_terminate_never_touches_closed_handle(monkeypatch):
    """The exact handle-reuse-safety property Part 9 requires: closing a
    handle for a genuinely finished job must not leave any code path that
    could later act on that (possibly-reused-by-Windows) handle number."""
    fake_k32 = _FakeKernel32()
    monkeypatch.setattr(process_control, "_kernel32", lambda: fake_k32)
    proc = _FakeProc(pid=555)
    control_metadata = {"backend": "windows", "job_handle": 9001, "pid": 555, "retired": False}
    process_control.release_control(control_metadata)
    fake_k32.calls.clear()
    process_control.terminate_tree(proc, control_metadata)
    assert fake_k32.calls == []
    assert proc.kill_calls == 0


def test_terminate_tree_dispatches_posix_backend_without_windows_calls(monkeypatch):
    recorded_killpg = []
    monkeypatch.setattr(process_control.os, "killpg", lambda *a, **kw: recorded_killpg.append(a))

    def fail_if_called(*a, **kw):
        pytest.fail("posix dispatch must never touch kernel32")
    monkeypatch.setattr(process_control, "_kernel32", fail_if_called)

    proc = _FakeProc(exited=False)
    process_control.terminate_tree(proc, {"backend": "posix", "pgid": 555, "retired": False})
    assert recorded_killpg == [(555, signal.SIGKILL)]


# ── Portability architecture regression (Section 13 / A1C Part 25) ───────

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _process_manager_source() -> str:
    return (_REPO_ROOT / "core" / "process_manager.py").read_text(encoding="utf-8")


def test_process_manager_source_contains_no_posix_specific_primitives():
    src = _process_manager_source()
    for forbidden in ("os.killpg", "os.getpgid", "signal.SIGKILL", "/bin/bash"):
        assert forbidden not in src, f"{forbidden!r} leaked into platform-neutral process_manager.py"


def test_process_manager_source_contains_no_windows_specific_primitives():
    src = _process_manager_source()
    for forbidden in ("CREATE_NEW_PROCESS_GROUP", "taskkill", "JobObject", "AssignProcessToJobObject"):
        assert forbidden not in src, f"{forbidden!r} leaked into platform-neutral process_manager.py"


def test_process_manager_source_never_imports_subprocess_or_ctypes_directly():
    src = _process_manager_source()
    assert "process_control" in src  # it does dispatch through the portability layer...
    assert "import subprocess" not in src  # ...and never reaches for subprocess itself
    assert "import ctypes" not in src


def test_process_manager_source_uses_tree_is_alive_for_lifetime_not_poll_alone():
    """CODING-04A1C Part 25: the manager may ask process_control whether
    the managed tree is alive; it must not decide job lifetime from
    proc.poll() alone. This does not forbid job.proc.wait()/.returncode
    (reading the LEADER's own return code is still legitimate, and
    unavoidable -- see Section 13 of the corrective spec), only asserts
    that the tree-lifetime primitive is actually wired in."""
    src = _process_manager_source()
    assert "process_control.tree_is_alive(" in src
    assert "process_control.release_control(" in src
