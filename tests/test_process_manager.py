"""
tests/test_process_manager.py -- CODING-04A1: core/process_manager.py, the
platform-neutral persistent-process kernel (job registry, emergency
leases, reader/watcher threads, bounded buffers, lifecycle, TTL).

No model-facing tool exists yet -- every call here goes straight through
the internal API (launch/request_stop/get_job_snapshot/etc.) the future
A2 tool layer will wrap. Real subprocesses are spawned on the live Linux
host throughout; the emergency-latch races use monkeypatch hooks to place
the latch deterministically at each of Section 17's required timings,
per the spec's own guidance, rather than relying on real-thread timing
luck.
"""
import json
import os
import threading
import time

import pytest

from core import emergency_stop, process_control, process_manager


@pytest.fixture(autouse=True)
def _clean_state():
    process_manager._reset_for_tests()
    emergency_stop._reset_for_tests()
    yield
    process_manager._reset_for_tests()
    emergency_stop._reset_for_tests()


def _wait_for_status(process_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = process_manager.get_job_snapshot(process_id)
        if snap is not None and snap["status"] != "running":
            return snap
        time.sleep(0.05)
    return process_manager.get_job_snapshot(process_id)


# ── Launch: identity, cwd, metadata ───────────────────────────────────────

def test_launch_successful_long_lived_child():
    pid = process_manager.launch("sleep 30", cwd="/tmp")
    try:
        snap = process_manager.get_job_snapshot(pid)
        assert snap["status"] == "running"
        assert snap["pid"] > 0
    finally:
        process_manager.request_stop(pid)
        _wait_for_status(pid)


def test_process_id_is_opaque_not_raw_pid():
    pid = process_manager.launch("true", cwd="/tmp")
    try:
        assert pid.startswith("p-")
        assert len(pid) == 14  # "p-" + 12 hex chars
        int(pid[2:], 16)  # must actually be hex
        snap = process_manager.get_job_snapshot(pid)
        assert pid != str(snap["pid"])
    finally:
        _wait_for_status(pid)


def test_process_ids_are_unique_across_launches():
    ids = set()
    procs = []
    for _ in range(5):
        p = process_manager.launch("true", cwd="/tmp")
        ids.add(p)
        procs.append(p)
    assert len(ids) == 5
    for p in procs:
        _wait_for_status(p)


def test_real_os_pid_and_control_metadata_retained():
    pid = process_manager.launch("sleep 30", cwd="/tmp")
    try:
        snap = process_manager.get_job_snapshot(pid)
        assert isinstance(snap["pid"], int) and snap["pid"] > 0
        assert snap["control_metadata"]["backend"] == "posix"
        assert "pgid" in snap["control_metadata"]
    finally:
        process_manager.request_stop(pid)
        _wait_for_status(pid)


def test_absolute_cwd_required_and_stored(tmp_path):
    pid = process_manager.launch("true", cwd=str(tmp_path))
    snap = process_manager.get_job_snapshot(pid)
    assert snap["cwd"] == str(tmp_path)
    _wait_for_status(pid)


def test_relative_cwd_rejected():
    with pytest.raises(process_manager.InvalidLaunchRequest):
        process_manager.launch("true", cwd="relative/path")


def test_nonexistent_cwd_rejected(tmp_path):
    missing = str(tmp_path / "does_not_exist")
    with pytest.raises(process_manager.InvalidLaunchRequest):
        process_manager.launch("true", cwd=missing)


def test_non_directory_cwd_rejected(tmp_path):
    f = tmp_path / "a_file.txt"
    f.write_text("not a directory")
    with pytest.raises(process_manager.InvalidLaunchRequest):
        process_manager.launch("true", cwd=str(f))


# ── Launch: guardrail placement ───────────────────────────────────────────

def test_blocked_catastrophic_command_never_reaches_spawn(monkeypatch):
    def sentinel_spawn(command, cwd):
        pytest.fail("process_control.spawn() was reached for a blocked command")
    monkeypatch.setattr(process_control, "spawn", sentinel_spawn)

    with pytest.raises(process_manager.CommandBlocked):
        process_manager.launch("rm -rf ~", cwd="/tmp")

    assert process_manager.list_job_ids() == []
    assert emergency_stop.snapshot()["active_execution_count"] == 0
    assert process_manager._live_reserved == 0


def test_blocked_command_never_consumes_a_lease_or_slot():
    for _ in range(process_manager.MAX_LIVE_PROCESSES):
        with pytest.raises(process_manager.CommandBlocked):
            process_manager.launch("sudo rm -rf /", cwd="/tmp")
    # If a blocked command had consumed a slot, this would raise
    # LiveProcessLimitReached instead of succeeding.
    pid = process_manager.launch("true", cwd="/tmp")
    _wait_for_status(pid)


# ── Launch: spawn failure cleanup ─────────────────────────────────────────

def test_spawn_failure_closes_lease_and_releases_slot(monkeypatch):
    monkeypatch.setattr(process_control, "spawn",
                         lambda command, cwd: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(RuntimeError):
        process_manager.launch("true", cwd="/tmp")

    assert emergency_stop.snapshot()["active_execution_count"] == 0
    assert process_manager._live_reserved == 0


def test_spawn_failure_leaves_no_registry_residue(monkeypatch):
    monkeypatch.setattr(process_control, "spawn",
                         lambda command, cwd: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        process_manager.launch("true", cwd="/tmp")
    assert process_manager.list_job_ids() == []


# ── Launch: live-process limit ────────────────────────────────────────────

def test_live_process_limit_enforced():
    ids = []
    try:
        for _ in range(process_manager.MAX_LIVE_PROCESSES):
            ids.append(process_manager.launch("sleep 30", cwd="/tmp"))
        with pytest.raises(process_manager.LiveProcessLimitReached):
            process_manager.launch("sleep 30", cwd="/tmp")
    finally:
        for pid in ids:
            process_manager.request_stop(pid)
        for pid in ids:
            _wait_for_status(pid)


def test_simultaneous_launches_cannot_exceed_limit():
    results = []
    errors = []
    results_lock = threading.Lock()

    def _try_launch():
        try:
            pid = process_manager.launch("sleep 30", cwd="/tmp")
            with results_lock:
                results.append(pid)
        except process_manager.LiveProcessLimitReached:
            pass
        except Exception as e:
            with results_lock:
                errors.append(e)

    threads = [threading.Thread(target=_try_launch) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    try:
        assert errors == []
        assert len(results) == process_manager.MAX_LIVE_PROCESSES
    finally:
        for pid in results:
            process_manager.request_stop(pid)
        for pid in results:
            _wait_for_status(pid)


# ── Tree termination ───────────────────────────────────────────────────────

def _pgrep(pattern: str) -> str:
    import subprocess
    return subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout.strip()


def test_whole_tree_termination_parent_child_grandchild():
    command = "sleep 292 & (sleep 293 &) ; wait"
    pid = process_manager.launch(command, cwd="/tmp")
    time.sleep(0.3)
    assert _pgrep("sleep 29[23]"), "expected tree to be running before stop"

    assert process_manager.request_stop(pid) is True
    snap = _wait_for_status(pid)
    assert snap["status"] == "stopped"

    deadline = time.time() + 5.0
    while time.time() < deadline and _pgrep("sleep 29[23]"):
        time.sleep(0.1)
    assert not _pgrep("sleep 29[23]"), "child/grandchild survived request_stop()"


def test_repeated_stop_is_safe():
    pid = process_manager.launch("sleep 30", cwd="/tmp")
    process_manager.request_stop(pid)
    process_manager.request_stop(pid)  # must not raise
    snap = _wait_for_status(pid)
    assert snap["status"] == "stopped"


def test_naturally_exited_job_never_incorrectly_resignaled(monkeypatch):
    pid = process_manager.launch("true", cwd="/tmp")
    snap = _wait_for_status(pid)
    assert snap["status"] == "exited"

    calls = []
    monkeypatch.setattr(process_control, "terminate_tree", lambda *a, **kw: calls.append(1))
    result = process_manager.request_stop(pid)
    assert result is False
    assert calls == []  # never signals a job that already exited on its own


# ── Pipe draining (no deadlock, no unbounded readline) ────────────────────

def test_pipe_draining_large_stdout_no_deadlock():
    command = "python3 -c \"import sys; sys.stdout.write('A' * 3000000)\""
    pid = process_manager.launch(command, cwd="/tmp")
    snap = _wait_for_status(pid, timeout=30.0)
    assert snap["status"] == "exited"
    assert snap["returncode"] == 0
    assert snap["stdout"]["end_offset"] == 3_000_000
    assert len(snap["stdout"]["text"]) <= process_manager.STREAM_BUFFER_MAX_CHARS


def test_pipe_draining_large_stderr_no_deadlock():
    command = "python3 -c \"import sys; sys.stderr.write('B' * 3000000)\""
    pid = process_manager.launch(command, cwd="/tmp")
    snap = _wait_for_status(pid, timeout=30.0)
    assert snap["status"] == "exited"
    assert snap["returncode"] == 0
    assert snap["stderr"]["end_offset"] == 3_000_000
    assert len(snap["stderr"]["text"]) <= process_manager.STREAM_BUFFER_MAX_CHARS


def test_no_newline_output_drains_in_bounded_chunks():
    command = "python3 -c \"import sys; sys.stdout.write('B' * 500000)\""
    pid = process_manager.launch(command, cwd="/tmp")
    snap = _wait_for_status(pid, timeout=15.0)
    assert snap["status"] == "exited"
    assert snap["stdout"]["end_offset"] == 500000
    assert "\n" not in snap["stdout"]["text"]


# ── Buffer contract ────────────────────────────────────────────────────────

def test_stdout_and_stderr_buffers_stay_separate():
    command = "python3 -c \"import sys; sys.stdout.write('OUT_MARK'); sys.stderr.write('ERR_MARK')\""
    pid = process_manager.launch(command, cwd="/tmp")
    snap = _wait_for_status(pid)
    assert "OUT_MARK" in snap["stdout"]["text"]
    assert "ERR_MARK" not in snap["stdout"]["text"]
    assert "ERR_MARK" in snap["stderr"]["text"]
    assert "OUT_MARK" not in snap["stderr"]["text"]


def test_buffer_caps_retained_size_and_discards_oldest():
    total = process_manager.STREAM_BUFFER_MAX_CHARS + 20000
    command = f"python3 -c \"import sys; sys.stdout.write('C' * {total})\""
    pid = process_manager.launch(command, cwd="/tmp")
    snap = _wait_for_status(pid, timeout=15.0)
    assert snap["status"] == "exited"
    stdout = snap["stdout"]
    assert len(stdout["text"]) <= process_manager.STREAM_BUFFER_MAX_CHARS
    assert stdout["end_offset"] == total
    assert stdout["start_offset"] == total - len(stdout["text"])
    assert stdout["end_offset"] - stdout["start_offset"] == len(stdout["text"])


def test_buffer_offsets_monotonic_under_direct_append():
    buf = process_manager._StreamBuffer(max_chars=10)
    seen_starts = []
    seen_ends = []
    for chunk in ["abcde", "fghij", "klmno", "pqrst"]:
        buf.append(chunk)
        snap = buf.snapshot()
        seen_starts.append(snap["start_offset"])
        seen_ends.append(snap["end_offset"])
    assert seen_starts == sorted(seen_starts)
    assert seen_ends == sorted(seen_ends)
    assert seen_ends[-1] == len("abcde" + "fghij" + "klmno" + "pqrst")
    final = buf.snapshot()
    assert len(final["text"]) <= 10


def test_unicode_buffer_offsets_are_character_based_not_byte_based():
    content = "héllo wôrld 🎉 " * 200  # < STREAM_BUFFER_MAX_CHARS in character count
    assert len(content.encode("utf-8")) > len(content)  # sanity: genuinely multi-byte

    escaped = content.replace("'", "\\'")
    command = f"python3 -c \"import sys; sys.stdout.write('{escaped}')\""
    pid = process_manager.launch(command, cwd="/tmp")
    snap = _wait_for_status(pid, timeout=15.0)
    assert snap["status"] == "exited"
    assert snap["stdout"]["end_offset"] == len(content)
    assert snap["stdout"]["text"] == content


def test_invalid_utf8_becomes_replacement_characters_reader_survives():
    command = "python3 -c \"import sys,os; os.write(1, b'before-' + bytes([0xff, 0xfe, 0x80]) + b'-after')\""
    pid = process_manager.launch(command, cwd="/tmp")
    snap = _wait_for_status(pid, timeout=10.0)
    assert snap["status"] == "exited"
    assert snap["returncode"] == 0
    assert "�" in snap["stdout"]["text"]
    assert "before-" in snap["stdout"]["text"]
    assert "-after" in snap["stdout"]["text"]


def test_direct_buffer_survives_concurrent_append_and_snapshot():
    buf = process_manager._StreamBuffer(max_chars=1000)
    errors = []

    def _writer():
        for i in range(500):
            buf.append(f"x{i};")

    def _reader():
        for _ in range(500):
            snap = buf.snapshot()
            if len(snap["text"]) > 1000 or snap["start_offset"] > snap["end_offset"]:
                errors.append(snap)

    w = threading.Thread(target=_writer)
    r = threading.Thread(target=_reader)
    w.start()
    r.start()
    w.join(timeout=10)
    r.join(timeout=10)
    assert errors == []


# ── Watcher / automatic reaping ────────────────────────────────────────────

def test_natural_exit_automatically_finalized_no_further_call_needed():
    pid = process_manager.launch("true", cwd="/tmp")
    snap = _wait_for_status(pid)
    assert snap["status"] == "exited"
    assert snap["returncode"] == 0
    assert snap["finish_at"] is not None

    with process_manager._registry_lock:
        job = process_manager._registry[pid]
    assert job.stdout_thread.is_alive() is False
    assert job.stderr_thread.is_alive() is False
    assert job.watcher_thread.is_alive() is False


def test_natural_exit_nonzero_returncode_preserved():
    pid = process_manager.launch("exit 7", cwd="/tmp")
    snap = _wait_for_status(pid)
    assert snap["status"] == "exited"
    assert snap["returncode"] == 7


def test_signal_killed_returncode_is_truthful_not_fabricated_zero():
    pid = process_manager.launch("sleep 30", cwd="/tmp")
    process_manager.request_stop(pid)
    snap = _wait_for_status(pid)
    assert snap["status"] == "stopped"
    assert snap["returncode"] != 0
    assert snap["returncode"] < 0  # negative == killed by signal (-SIGKILL)


# ── TTL retention ──────────────────────────────────────────────────────────

def test_completed_job_retained_before_ttl():
    pid = process_manager.launch("true", cwd="/tmp")
    _wait_for_status(pid)
    assert process_manager.get_job_snapshot(pid) is not None
    assert pid in process_manager.list_job_ids()


def test_completed_job_swept_after_ttl():
    pid = process_manager.launch("true", cwd="/tmp")
    _wait_for_status(pid)
    with process_manager._registry_lock:
        job = process_manager._registry[pid]
        job.finish_at = time.time() - process_manager.COMPLETED_PROCESS_TTL_SECONDS - 1

    assert process_manager.get_job_snapshot(pid) is None
    assert pid not in process_manager.list_job_ids()


def test_live_job_never_swept_regardless_of_age():
    pid = process_manager.launch("sleep 30", cwd="/tmp")
    try:
        with process_manager._registry_lock:
            job = process_manager._registry[pid]
            assert job.status == "running"
            assert job.finish_at is None
        # A running job has no finish_at, so it can never satisfy the TTL
        # sweep's age check regardless of COMPLETED_PROCESS_TTL_SECONDS.
        assert process_manager.get_job_snapshot(pid) is not None
        assert pid in process_manager.list_job_ids()
    finally:
        process_manager.request_stop(pid)
        _wait_for_status(pid)


# ── Stop races / concurrency ────────────────────────────────────────────────

def test_stop_races_natural_exit_no_exception():
    errors = []

    def _run():
        try:
            pid = process_manager.launch("sleep 0.1", cwd="/tmp")
            process_manager.request_stop(pid)
            _wait_for_status(pid, timeout=5.0)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=_run) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert errors == []


def test_duplicate_simultaneous_stop_no_exception_single_consistent_reason():
    pid = process_manager.launch("sleep 30", cwd="/tmp")
    errors = []

    def _stop():
        try:
            process_manager.request_stop(pid)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=_stop) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    snap = _wait_for_status(pid)
    assert snap["status"] == "stopped"


def test_kill_all_vs_manual_stop_no_exception():
    pid = process_manager.launch("sleep 30", cwd="/tmp")
    errors = []

    def _manual_stop():
        try:
            process_manager.request_stop(pid)
        except Exception as e:
            errors.append(e)

    def _kill_all():
        try:
            process_manager.emergency_kill_all()
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=_manual_stop)
    t2 = threading.Thread(target=_kill_all)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert errors == []
    snap = _wait_for_status(pid)
    assert snap["status"] in ("stopped", "emergency_killed")


# ── shutdown_all() ─────────────────────────────────────────────────────────

def test_shutdown_all_terminates_every_running_job():
    ids = [process_manager.launch("sleep 30", cwd="/tmp") for _ in range(3)]
    n = process_manager.shutdown_all(wait_seconds=5.0)
    assert n == 3
    for pid in ids:
        snap = _wait_for_status(pid)
        assert snap["status"] == "shutdown_killed"


def test_shutdown_all_idempotent_with_no_jobs():
    assert process_manager.shutdown_all(wait_seconds=0.1) == 0


# ── Emergency-lease persistence (Section 16/31) ────────────────────────────

def test_live_managed_process_makes_can_rearm_false_after_latch():
    pid = process_manager.launch("sleep 30", cwd="/tmp")
    assert emergency_stop.can_rearm() is False  # not even latched yet
    emergency_stop.latch(source="test", reason="lease persistence proof")
    assert emergency_stop.can_rearm() is False  # latched AND a live lease open

    process_manager.request_stop(pid)
    _wait_for_status(pid)
    assert emergency_stop.can_rearm() is True  # watcher observed death, lease closed
    emergency_stop.rearm_local()


def test_lease_stays_open_until_death_is_actually_observed(monkeypatch):
    """Deterministic proof that terminate_tree() being *requested* is not
    sufficient -- the lease must survive until the watcher's proc.wait()
    genuinely returns. Neuters terminate_tree so the real child keeps
    running, proving can_rearm() stays False the whole time it's alive,
    then restores real termination for cleanup."""
    real_terminate = process_control.terminate_tree
    monkeypatch.setattr(process_control, "terminate_tree", lambda *a, **kw: None)

    pid = process_manager.launch("sleep 30", cwd="/tmp")
    emergency_stop.latch(source="test", reason="dying-but-not-dead")
    n = process_manager.emergency_kill_all()
    assert n == 1

    time.sleep(0.3)
    snap = process_manager.get_job_snapshot(pid)
    assert snap["status"] == "running"  # never finalized -- death was never observed
    assert emergency_stop.can_rearm() is False

    monkeypatch.setattr(process_control, "terminate_tree", real_terminate)
    with process_manager._registry_lock:
        job = process_manager._registry[pid]
    real_terminate(job.proc, job.control_metadata)
    _wait_for_status(pid)

    assert emergency_stop.can_rearm() is True
    emergency_stop.rearm_local()


def test_incident_snapshot_is_json_safe_with_managed_process_active():
    pid = process_manager.launch("sleep 30", cwd="/tmp")
    try:
        incident = emergency_stop.latch(source="test", reason="json safety")
        snap = emergency_stop.snapshot()
        json.dumps(incident)
        json.dumps(snap)
    finally:
        process_manager.request_stop(pid)
        _wait_for_status(pid)
        emergency_stop.rearm_local()


def test_process_command_and_output_not_leaked_into_incident_snapshot():
    secret_marker = "SUPER_SECRET_COMMAND_MARKER_84213"
    pid = process_manager.launch(f"echo {secret_marker}", cwd="/tmp")
    try:
        incident = emergency_stop.latch(source="test", reason="no leakage")
        snap = emergency_stop.snapshot()
        blob = json.dumps(incident) + json.dumps(snap)
        assert secret_marker not in blob
    finally:
        _wait_for_status(pid)
        emergency_stop.rearm_local()


# ── Spawn/latch race ordering (Section 17/23) ──────────────────────────────

def test_race_latch_before_lease_refuses_launch_no_process(monkeypatch):
    def sentinel_spawn(command, cwd):
        pytest.fail("spawn reached the OS despite the latch winning before lease acquisition")
    monkeypatch.setattr(process_control, "spawn", sentinel_spawn)
    try:
        emergency_stop.latch(source="test", reason="race: before lease")
        with pytest.raises(process_manager.LaunchRefused):
            process_manager.launch("sleep 30", cwd="/tmp")
    finally:
        emergency_stop.rearm_local()

    assert process_manager.list_job_ids() == []
    assert emergency_stop.snapshot()["active_execution_count"] == 0
    assert process_manager._live_reserved == 0


def test_race_latch_after_lease_before_spawn_no_surviving_process_no_lease_leak(monkeypatch):
    real_begin = emergency_stop.begin_persistent_execution

    def latching_begin(*args, **kwargs):
        lease_id = real_begin(*args, **kwargs)
        emergency_stop.latch(source="test", reason="race: after lease, before spawn")
        return lease_id

    monkeypatch.setattr(emergency_stop, "begin_persistent_execution", latching_begin)

    pid = process_manager.launch("sleep 30", cwd="/tmp")
    snap = _wait_for_status(pid, timeout=5.0)
    assert snap["status"] == "emergency_killed"
    assert emergency_stop.snapshot()["active_execution_count"] == 0  # lease closed, no leak

    emergency_stop.rearm_local()


def test_race_latch_after_spawn_before_registration_immediately_terminated(monkeypatch):
    real_spawn = process_control.spawn

    def latching_spawn(command, cwd):
        result = real_spawn(command, cwd)
        emergency_stop.latch(source="test", reason="race: after spawn, before registration")
        return result

    monkeypatch.setattr(process_control, "spawn", latching_spawn)

    pid = process_manager.launch("sleep 30", cwd="/tmp")
    snap = _wait_for_status(pid, timeout=5.0)
    assert snap["status"] == "emergency_killed"
    assert emergency_stop.snapshot()["active_execution_count"] == 0

    emergency_stop.rearm_local()


def test_race_latch_after_full_registration_reached_by_kill_all():
    pid = process_manager.launch("sleep 30", cwd="/tmp")
    snap = process_manager.get_job_snapshot(pid)
    assert snap["status"] == "running"

    emergency_stop.latch(source="test", reason="race: after full registration")
    n = process_manager.emergency_kill_all()
    assert n == 1

    snap = _wait_for_status(pid)
    assert snap["status"] == "emergency_killed"
    emergency_stop.rearm_local()


def test_rearm_after_emergency_kill_old_process_stays_terminal():
    pid = process_manager.launch("sleep 30", cwd="/tmp")
    emergency_stop.latch(source="test", reason="rearm-after-kill")
    process_manager.emergency_kill_all()
    _wait_for_status(pid)
    emergency_stop.rearm_local()

    snap = process_manager.get_job_snapshot(pid)
    assert snap["status"] == "emergency_killed"  # no call resurrects or relegitimizes it
    assert process_manager.request_stop(pid) is False


# ── CODING-04A1C: managed-tree lifetime (leader exit != job finished) ──────
#
# The independent CODING-04A1 safety review reproduced a landing blocker:
# `sleep 120 & exit 0` backgrounds a descendant and lets the shell leader
# exit immediately. proc.poll() then reports the leader dead while the
# descendant is still alive in the same process group -- the old watcher
# treated proc.wait() returning as "the managed work is finished," closed
# the persistent emergency lease, and emergency_kill_all() subsequently
# reported nothing to kill while the descendant kept running, fully
# invisible to Lumina. Every fixture below uses a distinct sleep duration
# so pgrep -f can never cross-match another test's descendant.

def _leader_exit_descendant_command(duration: int) -> str:
    return f"sleep {duration} & exit 0"


def _leader_exit_descendant_command_redirected(duration) -> str:
    """Like _leader_exit_descendant_command, except the descendant's own
    stdout/stderr are redirected to /dev/null rather than inheriting
    bash's pipe fds. This deliberately removes a confound discovered
    during this corrective's own mutation testing: an UN-redirected
    descendant keeps bash's stdout/stderr pipe write-end open for as long
    as it lives, so the watcher's reader-thread join(timeout=10) calls
    (two of them, sequential -> up to ~20s total) incidentally keep a job
    looking 'running' for up to ~20s after leader exit REGARDLESS of
    whether tree-lifetime tracking is even wired up correctly -- a leader-
    only watcher (CODING-04A1's original bug, deliberately reintroduced as
    Mutation A below) was still observed passing a same-second status
    check purely because of this side effect. Redirected output lets the
    reader threads reach real EOF the moment the leader exits, regardless
    of the descendant's own lifetime, which is what makes status checks
    taken shortly after leader-exit an actual proof of correct tree
    tracking rather than an accidental proof of the join timeout."""
    return f"sleep {duration} >/dev/null 2>&1 & exit 0"


def test_a_leader_exits_descendant_alive_job_still_running():
    """Uses the UN-redirected fixture deliberately (see
    _leader_exit_descendant_command_redirected's docstring): this is
    Part 12's exact scenario -- a descendant that inherits and holds open
    bash's stdout/stderr pipe fds after the leader exits. The reader-join
    timeout (up to ~20s total across both streams, sequential) means a
    same-second status check cannot distinguish correct tree tracking
    from that incidental delay -- checking again well past 20s is what
    actually proves the job stays 'running' because the descendant is
    genuinely still alive, not merely because the reader joins haven't
    timed out yet."""
    pid = process_manager.launch(_leader_exit_descendant_command(7101), cwd="/tmp")
    try:
        deadline = time.time() + 5.0
        proc = None
        with process_manager._registry_lock:
            proc = process_manager._registry[pid].proc
        while time.time() < deadline and proc.poll() is None:
            time.sleep(0.02)
        assert proc.poll() is not None, "leader should have exited almost immediately"

        assert _pgrep("sleep 7101"), "descendant should still be alive"
        snap = process_manager.get_job_snapshot(pid)
        assert snap["status"] == "running", (
            "job must stay 'running' while the descendant is alive, even though the leader already exited"
        )
        assert pid in process_manager.list_job_ids()

        # The real discriminator: still "running" at t=21s, past BOTH
        # sequential 10s reader-join timeouts, while the descendant (a
        # 7101-second sleep) is still very much alive.
        time.sleep(21.0)
        assert _pgrep("sleep 7101"), "descendant should still be alive at t=21s"
        snap2 = process_manager.get_job_snapshot(pid)
        assert snap2["status"] == "running", (
            "job finalized past both reader-join windows even though the descendant "
            "never died -- tree lifetime, not the reader-join timeout, must govern this"
        )
    finally:
        # Hard fallback, not just request_stop(): under a mutated watcher
        # that auto-finalizes the job "exited" purely from the reader-join
        # timeout (see docstring above), request_stop() would see
        # status != "running" and skip terminate_tree() entirely, leaking
        # a real OS process during mutation testing. pkill here is
        # unconditional and independent of the manager's own state.
        process_manager.request_stop(pid)
        _wait_for_status(pid, timeout=10.0)
        import subprocess
        subprocess.run(["pkill", "-9", "-f", "sleep 7101"])
        assert not _pgrep("sleep 7101")


def test_b_manual_stop_after_leader_exit_kills_descendant():
    pid = process_manager.launch(_leader_exit_descendant_command(7102), cwd="/tmp")
    try:
        assert _wait_until_present_helper("sleep 7102")

        assert process_manager.request_stop(pid) is True
        snap = _wait_for_status(pid, timeout=10.0)
        assert snap["status"] == "stopped"
        assert not _pgrep("sleep 7102"), "descendant survived request_stop() after leader had already exited"
    finally:
        # Hard fallback: if a mutation breaks tree-lifecycle termination,
        # request_stop() above can request a kill that never actually
        # reaches a live descendant -- this cleanup is unconditional and
        # independent of the manager's own (possibly mutation-corrupted)
        # state, so it always removes the real OS process regardless of
        # which assertion above failed.
        process_manager.request_stop(pid)
        _wait_for_status(pid, timeout=10.0)
        import subprocess
        subprocess.run(["pkill", "-9", "-f", "sleep 7102"])


def test_c_emergency_kill_after_leader_exit_kills_descendant_and_gates_rearm():
    pid = process_manager.launch(_leader_exit_descendant_command(7103), cwd="/tmp")
    try:
        assert _wait_until_present_helper("sleep 7103")

        emergency_stop.latch(source="test", reason="A1C leader-exit emergency")
        assert emergency_stop.can_rearm() is False  # lease still open -- tree not yet observed dead

        n = process_manager.emergency_kill_all()
        assert n == 1

        snap = _wait_for_status(pid, timeout=10.0)
        assert snap["status"] == "emergency_killed"
        assert not _pgrep("sleep 7103"), "descendant survived emergency_kill_all() after leader had already exited"
        assert emergency_stop.can_rearm() is True
        emergency_stop.rearm_local()
    finally:
        # Hard fallback: the independent review found this exact test can
        # leak a real descendant when a mutation breaks tree-lifecycle
        # termination (emergency_kill_all() requests death but it never
        # actually reaches the descendant, so the watcher never finalizes
        # and the lease never closes). This cleanup is unconditional and
        # independent of the manager's own (possibly mutation-corrupted)
        # state, and runs even when an assertion above failed.
        process_manager.request_stop(pid)
        _wait_for_status(pid, timeout=10.0)
        import subprocess
        subprocess.run(["pkill", "-9", "-f", "sleep 7103"])
        # The watcher thread finalizes (and closes the lease) asynchronously
        # after observing tree-death -- give it a moment to actually catch
        # up to the hard pkill above before attempting rearm, otherwise
        # rearm_local() can race a lease that is about to close but hasn't
        # yet, and raise RearmBlocked instead of cleanly succeeding.
        _wait_for_status(pid, timeout=10.0)
        if emergency_stop.is_latched():
            emergency_stop.rearm_local()


def test_d_shutdown_after_leader_exit_kills_descendant():
    pid = process_manager.launch(_leader_exit_descendant_command(7104), cwd="/tmp")
    try:
        assert _wait_until_present_helper("sleep 7104")

        n = process_manager.shutdown_all(wait_seconds=10.0)
        assert n == 1

        snap = _wait_for_status(pid, timeout=10.0)
        assert snap["status"] == "shutdown_killed"
        assert not _pgrep("sleep 7104"), "descendant survived shutdown_all() after leader had already exited"
    finally:
        process_manager.request_stop(pid)
        _wait_for_status(pid, timeout=10.0)
        import subprocess
        subprocess.run(["pkill", "-9", "-f", "sleep 7104"])


def test_e_natural_background_completion_finalizes_only_after_descendant_exits():
    # Fractional duration -- short real wait, but a grep pattern that can
    # never collide with any other (integer-duration) fixture in this file.
    pid = process_manager.launch("sleep 2.7111 & exit 0", cwd="/tmp")
    try:
        assert _wait_until_present_helper("sleep 2.7111", timeout=3.0)

        # No stop/emergency call -- purely natural completion. Confirm it does
        # NOT finalize merely because the leader already exited.
        snap = process_manager.get_job_snapshot(pid)
        assert snap["status"] == "running"

        snap = _wait_for_status(pid, timeout=8.0)
        assert snap["status"] == "exited"
        assert snap["returncode"] == 0  # leader's own returncode, truthfully retained
        assert not _pgrep("sleep 2.7111")
    finally:
        # Unconditional fallback for consistency with the other fixtures in
        # this section -- low-risk here (nothing in this test signals the
        # descendant, it is only ever observed exiting on its own), but a
        # mutation to finalization logic could in principle leave the
        # manager's own bookkeeping out of sync with a since-self-exited OS
        # process; this never touches anything but this fixture's own
        # distinctive sleep duration.
        import subprocess
        subprocess.run(["pkill", "-9", "-f", "sleep 2.7111"])


def test_f_multiple_background_children_all_must_die_before_finalization():
    pid = process_manager.launch("sleep 7105 & sleep 7106 & exit 0", cwd="/tmp")
    try:
        assert _wait_until_present_helper("sleep 7105")
        assert _wait_until_present_helper("sleep 7106")

        assert process_manager.get_job_snapshot(pid)["status"] == "running"

        # Kill only one member out-of-band -- job must stay "running" while
        # the other sibling is still alive in the same managed group.
        import subprocess
        subprocess.run(["pkill", "-9", "-f", "sleep 7105"])
        deadline = time.time() + 3.0
        while time.time() < deadline and _pgrep("sleep 7105"):
            time.sleep(0.05)
        assert not _pgrep("sleep 7105")
        assert process_manager.get_job_snapshot(pid)["status"] == "running", (
            "job must remain running while any member of the managed group is still alive"
        )
        assert _pgrep("sleep 7106")

        n = process_manager.emergency_kill_all()
        assert n == 1
        snap = _wait_for_status(pid, timeout=10.0)
        assert snap["status"] == "emergency_killed"
        assert not _pgrep("sleep 7106")
    finally:
        process_manager.request_stop(pid)
        _wait_for_status(pid, timeout=10.0)
        import subprocess
        subprocess.run(["pkill", "-9", "-f", "sleep 7105"])
        subprocess.run(["pkill", "-9", "-f", "sleep 7106"])
        # See test_c's identical comment: give the watcher thread a moment
        # to observe tree-death and close the lease before rearm.
        _wait_for_status(pid, timeout=10.0)
        if emergency_stop.is_latched():
            emergency_stop.rearm_local()


def test_g_leader_nonzero_exit_descendant_survives_returncode_retained_truthfully():
    pid = process_manager.launch(f"sleep 7107 & exit 17", cwd="/tmp")
    try:
        assert _wait_until_present_helper("sleep 7107")
        assert process_manager.get_job_snapshot(pid)["status"] == "running"

        n = process_manager.emergency_kill_all()
        assert n == 1
        snap = _wait_for_status(pid, timeout=10.0)
        assert snap["status"] == "emergency_killed"  # termination reason outranks the leader's earlier exit
        assert snap["returncode"] == 17  # leader's own returncode is still truthfully retained
        assert not _pgrep("sleep 7107")
    finally:
        process_manager.request_stop(pid)
        _wait_for_status(pid, timeout=10.0)
        import subprocess
        subprocess.run(["pkill", "-9", "-f", "sleep 7107"])
        # See test_c's identical comment: give the watcher thread a moment
        # to observe tree-death and close the lease before rearm.
        _wait_for_status(pid, timeout=10.0)
        if emergency_stop.is_latched():
            emergency_stop.rearm_local()


def _wait_until_present_helper(pattern: str, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _pgrep(pattern):
            return True
        time.sleep(0.02)
    return False


def test_ttl_never_starts_while_descendant_alive_despite_leader_exit():
    """Part 18: even an aggressively-monkeypatched-into-the-past finish_at
    must not cause a sweep while the managed tree still has a live member
    -- because with the corrected watcher, finish_at is never even SET
    until the tree is actually empty (status stays 'running' the whole
    time), so there is nothing for a hostile finish_at value to act on."""
    pid = process_manager.launch(_leader_exit_descendant_command_redirected(7108), cwd="/tmp")
    try:
        assert _wait_until_present_helper("sleep 7108")
        with process_manager._registry_lock:
            job = process_manager._registry[pid]
            assert job.status == "running"
            assert job.finish_at is None
            # Even if something were to force finish_at into the deep past
            # on a running job, _sweep_expired() only ever considers jobs
            # whose status != "running" -- so this can't matter, but prove
            # sweeping still leaves the live job alone regardless.
            job.finish_at = time.time() - process_manager.COMPLETED_PROCESS_TTL_SECONDS - 1000

        assert process_manager.get_job_snapshot(pid) is not None
        assert pid in process_manager.list_job_ids()
        assert _pgrep("sleep 7108"), "descendant must still be alive -- TTL must never have touched this job"
    finally:
        process_manager.request_stop(pid)
        _wait_for_status(pid, timeout=10.0)
        import subprocess
        subprocess.run(["pkill", "-9", "-f", "sleep 7108"])


def test_finish_at_reflects_true_tree_death_not_leader_exit_time():
    """Part 18's other half: finish_at itself (the TTL clock's reference
    point) must be stamped at TRUE completion (tree empty), not at the
    earlier moment the leader happened to exit -- a subtler variant of
    'TTL starts from leader exit' than the running-job-is-immune test
    above: even once the job DOES reach a terminal status, its finish_at
    must reflect when the descendant actually died, not when the leader
    did, so a TTL sweep can't fire early for a job that only just
    genuinely finished."""
    launched_at = time.time()
    pid = process_manager.launch("sleep 2.7211 >/dev/null 2>&1 & exit 0", cwd="/tmp")
    try:
        assert _wait_until_present_helper("sleep 2.7211", timeout=3.0)
        leader_exit_deadline = time.time() + 2.0
        with process_manager._registry_lock:
            proc = process_manager._registry[pid].proc
        while time.time() < leader_exit_deadline and proc.poll() is None:
            time.sleep(0.02)
        leader_exited_at = time.time()
        assert proc.poll() is not None

        snap = _wait_for_status(pid, timeout=8.0)
        assert snap["status"] == "exited"
        # The descendant lives ~2.7s total -- true completion must be well
        # after the leader's own near-instant exit, and finish_at must
        # reflect that true completion time, not the earlier leader-exit
        # moment (which a "TTL starts from leader exit" bug would stamp
        # instead).
        assert snap["finish_at"] >= leader_exited_at, (
            "finish_at was stamped at (or before) leader-exit time, not true tree-death time"
        )
        assert snap["finish_at"] - launched_at >= 2.0, (
            "finish_at is implausibly close to launch time -- TTL clock started too early"
        )
    finally:
        process_manager.request_stop(pid)
        _wait_for_status(pid, timeout=10.0)
        import subprocess
        subprocess.run(["pkill", "-9", "-f", "sleep 2.7211"])


def test_live_job_limit_leader_exited_descendants_still_consume_slots():
    """Part 19: 10 jobs shaped like `long_child & exit 0` -- once every
    leader has exited but before any descendant has been reaped, an 11th
    launch must still be refused. Only once one entire managed tree
    becomes empty does a slot free up."""
    pids = []
    try:
        for i in range(process_manager.MAX_LIVE_PROCESSES):
            duration = 7200 + i
            pid = process_manager.launch(_leader_exit_descendant_command_redirected(duration), cwd="/tmp")
            pids.append((pid, duration))

        for _, duration in pids:
            assert _wait_until_present_helper(f"sleep {duration}"), (
                f"descendant for duration {duration} should be alive"
            )
        for pid, _ in pids:
            assert process_manager.get_job_snapshot(pid)["status"] == "running"

        with pytest.raises(process_manager.LiveProcessLimitReached):
            process_manager.launch("true", cwd="/tmp")

        # Kill exactly one managed tree -- only then must a slot free up.
        first_pid, first_duration = pids[0]
        assert process_manager.request_stop(first_pid) is True
        _wait_for_status(first_pid, timeout=10.0)
        assert not _pgrep(f"sleep {first_duration}")

        new_pid = process_manager.launch("true", cwd="/tmp")
        _wait_for_status(new_pid)
    finally:
        for pid, duration in pids:
            try:
                process_manager.request_stop(pid)
            except Exception:
                pass
        for pid, duration in pids:
            _wait_for_status(pid, timeout=10.0)
        import subprocess
        for _, duration in pids:
            subprocess.run(["pkill", "-9", "-f", f"sleep {duration}"])


def test_persistent_lease_stays_open_through_leader_exit_until_tree_empty():
    """Part 20: the lease primitives themselves need no redesign -- this
    proves the existing lease correctly tracks TREE lifetime now that the
    watcher's finalization premise changed, not leader lifetime."""
    pid = process_manager.launch(_leader_exit_descendant_command_redirected(7109), cwd="/tmp")
    try:
        assert _wait_until_present_helper("sleep 7109")

        with process_manager._registry_lock:
            job = process_manager._registry[pid]
            lease_id = job.lease_id

        assert lease_id in emergency_stop._executions
        snap = emergency_stop.snapshot()
        assert snap["active_execution_count"] >= 1
        assert any(e["id"] == lease_id for e in snap["active_executions"])

        emergency_stop.latch(source="test", reason="A1C lease-through-leader-exit")
        assert emergency_stop.can_rearm() is False

        process_manager.emergency_kill_all()
        _wait_for_status(pid, timeout=10.0)

        assert lease_id not in emergency_stop._executions
        assert emergency_stop.can_rearm() is True
        emergency_stop.rearm_local()
    finally:
        if emergency_stop.is_latched():
            emergency_stop.rearm_local()
        # Hard fallback: if an earlier assertion in the try block raised
        # before emergency_kill_all() ever ran (e.g. under a mutation that
        # closes the lease early), the descendant must still not leak.
        import subprocess
        subprocess.run(["pkill", "-9", "-f", "sleep 7109"])


def test_manager_contract_never_decides_finalization_from_leader_poll_alone():
    """Part 25 behavioral half (the source-grep architecture assertion
    lives in tests/test_process_control.py): directly proves the watcher
    does not finalize merely because the leader Popen object reports
    exited -- it must keep asking process_control.tree_is_alive()."""
    pid = process_manager.launch(_leader_exit_descendant_command_redirected(7110), cwd="/tmp")
    try:
        assert _wait_until_present_helper("sleep 7110")
        with process_manager._registry_lock:
            job = process_manager._registry[pid]
        assert job.proc.poll() is not None, "leader must have already exited"
        assert job.status == "running", (
            "the manager must not treat proc.poll() != None as proof the job is finished"
        )
    finally:
        process_manager.request_stop(pid)
        _wait_for_status(pid, timeout=10.0)
        import subprocess
        subprocess.run(["pkill", "-9", "-f", "sleep 7110"])
