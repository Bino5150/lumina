"""C1-S3a: the Verified Agent Backup (format 2.3) -- C1's sanitized recovery
record captured in a child process, verifier-derived coverage, the
publication gate and owner words. Synthetic roots only (C1S3-AMENDMENT-A1).
"""
import ast
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

import core.agent_backup as ab
import core.agent_backup_export as ex
import core.agent_backup_inventory as inv
import core.agent_backup_restore_intake as ri
from core import agent_backup_c1_child as c1_child
from core import operator_ingress as oi
from core import suspension as S
from core import suspension_backup as c1
from test_agent_backup_verified_export_01 import source, read_zip, NO_CREDENTIALS, NO_IDENTITY_TRAILERS  # noqa: F401
from test_agent_backup_phase_b_s3_capture_rebind import _ingest, _blob

REPO = Path(__file__).resolve().parent.parent
GOLDEN = REPO / "tests" / "fixtures" / "c1_recovery_state_v1_golden.json"
GOLDEN_SHA256 = "2674340dec5cb7d75b39b030483fe7ad56d122830e617e76b9445abaf8eeaf8b"
SIDECARS = (S.STORE_NAME + "-wal", S.STORE_NAME + "-shm", S.STORE_NAME + "-journal")
FORBIDDEN_WORDS = ("restorable", "fully recoverable", "complete backup", "guaranteed", "safe to restore")
RAW_CODE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def local(action="test"):
    return oi.local_suspension_control(action)


def c1_store(data, *, evidence=None):
    (data / "memory").chmod(0o700)
    store = S.initialize(data, ingress=local("init"))
    store.trip(store.capture(S.OWNER_SCOPE), trigger=S.Trigger.BREAKER_TRIPPED,
               evidence=evidence or {"trip_kind": "stale_streak", "executed_calls": 7})
    store.close()


@pytest.fixture
def vsource(source):
    c1_store(source[0])
    return source


@pytest.fixture(autouse=True)
def _fresh_cache():
    ex._c1_observation_cache.clear()
    yield
    ex._c1_observation_cache.clear()


def verified(source, dest=None):
    data, base, default = source
    return ex.export_verified_agent_backup(str(data), str(base), str(dest or default),
                                           credentials_path=NO_CREDENTIALS,
                                           identity_trailers_path=NO_IDENTITY_TRAILERS)


def incomplete(source, dest):
    data, base, _ = source
    return ex.export_incomplete_state(str(data), str(base), str(dest), owner_acknowledged=True,
                                      credentials_path=NO_CREDENTIALS, identity_trailers_path=NO_IDENTITY_TRAILERS)


def refused(call, *args, **kwargs):
    with pytest.raises(ex.ExportUnavailable) as info:
        call(*args, **kwargs)
    return info.value


def entries(path):
    with zipfile.ZipFile(path) as z:
        return [(i.filename, z.read(i.filename)) for i in z.infolist()]


def rebuild(src, dst, mutate, *, rehash=True, coverage=True):
    """Rewrite an archive: mutate(manifest, files) edits both; members whose
    bytes changed are re-hashed unless rehash=False; coverage is recomputed
    so only the targeted rule can fail."""
    items = entries(src)
    files = {n: d for n, d in items if n != "manifest.json"}
    manifest = json.loads(dict(items)["manifest.json"])
    mutate(manifest, files)
    if rehash:
        for m in manifest["members"]:
            if type(m) is dict and m.get("archive_path") in files:
                m["sha256"] = hashlib.sha256(files[m["archive_path"]]).hexdigest()
                m["size"] = len(files[m["archive_path"]])
    if coverage:
        try:
            manifest["coverage"] = ex._coverage(manifest)
        except (KeyError, TypeError, AttributeError):
            pass
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(ab._make_v1_zipinfo(name), data)
        z.writestr(ab._make_v1_zipinfo("manifest.json"), json.dumps(manifest, indent=2))
    return dst


def errors_of(path):
    report = ex.verify_export(io.BytesIO(Path(path).read_bytes()))
    return [] if report["valid"] else report["errors"]


def c1_member(manifest):
    return next(m for m in manifest["members"] if m["logical_id"] == ex.C1_MEMBER_ID)


def tree(root):
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            st = os.lstat(path)
            digest = hashlib.sha256(Path(path).read_bytes()).hexdigest() if stat.S_ISREG(st.st_mode) else None
            out[os.path.relpath(path, root)] = (stat.S_IFMT(st.st_mode), st.st_ino, digest)
    return out


def lint(text):
    lowered = text.lower()
    assert not [w for w in FORBIDDEN_WORDS if w in lowered], text
    assert not RAW_CODE.search(text), text


# ---------------------------------------------------------------------------
# G-3/G-4/G-5: the happy path, format 2.3, receipt
# ---------------------------------------------------------------------------

def test_verified_export_is_2_3_with_derived_full_coverage_and_confirms(vsource):
    manifest, receipt = verified(vsource)
    dest = vsource[2]
    assert manifest["format_version"] == ex.VERIFIED_VERSION == {"major": 2, "minor": 3}
    assert manifest["backup_mode"] == "verified_agent_backup"
    assert manifest["compatibility"]["min_supported_format_version"] == ex.VERIFIED_VERSION
    assert errors_of(dest) == []
    assert receipt["export_kind"] == "verified_agent_backup"
    assert receipt["format_version"] == ex.VERIFIED_VERSION
    assert receipt["full_agent_coverage"] is True and receipt["coverage_blockers"] == []
    assert receipt["restorable_verified"] is False
    assert receipt["recovery_acceptance"] == {"status": "not_proven", "restore_input_accepted": False}
    coverage = receipt["c1_safety_coverage"]
    assert coverage["status"] == "captured_sanitized" and coverage["raw_store"] == "excluded"
    assert coverage["label"] == "derived_sanitized_producer_claims"
    assert coverage["live_state"] == "never_restored_as_live"
    assert coverage["enforcement_compatibility"] == "unproven"
    assert coverage["limitations"] == list(ex.C1_LIMITATIONS)
    assert ex.C1_LIMITATIONS[0] == "snapshot_predates_later_trips_recovery_or_replacement"
    assert ex.C1_LIMITATIONS[-1] == "members_captured_at_different_times"
    member = c1_member(manifest)
    assert {k: member[k] for k in ex.C1_MEMBER_POLICY} == ex.C1_MEMBER_POLICY
    assert member["portable"] == "PORTABLE"
    raw = read_zip(dest)[member["archive_path"]]
    assert raw == c1.canonical_recovery_payload(json.loads(raw))
    c1.validate_recovery_state(json.loads(raw))
    assert manifest["export_observations"]["c1_recovery"] == ex.c1_observation_from_bytes(raw)
    assert manifest["export_observations"]["c1_recovery"]["record_sha256"] == member["sha256"]
    exclusion = manifest["c1_exclusion"]
    assert exclusion["version"] == 2 and "snapshot_captured" not in exclusion
    assert exclusion["raw_store"] == "excluded" and exclusion["sanitized_record"] == "see_c1_recovery"
    confirmed = ex.confirm_export_result(str(dest), manifest, receipt)
    assert confirmed["full_agent_coverage"] is True and confirmed["restorable_verified"] is False
    names = set(read_zip(dest))
    assert not any(n.startswith("memory/") or "soul_identity" in n or n.endswith(".db-wal") for n in names)


def test_incomplete_export_stays_2_2_without_any_c1_member(vsource, tmp_path):
    manifest, receipt = incomplete(vsource, tmp_path / "incomplete.zip")
    assert manifest["format_version"] == ex.VERSION == {"major": 2, "minor": 2}
    assert manifest["backup_mode"] == "incomplete_state_export"
    assert not any(m["logical_id"] == ex.C1_MEMBER_ID or m["archive_path"].startswith("state/c1/")
                   for m in manifest["members"])
    assert "c1_recovery" not in manifest["export_observations"]
    assert manifest["export_observations"]["suspension"] == "unavailable_present"
    assert manifest["c1_exclusion"]["version"] == 1 and manifest["c1_exclusion"]["snapshot_captured"] is False
    assert receipt["export_kind"] == "incomplete_state_export" and receipt["full_agent_coverage"] is False
    assert receipt["coverage_blockers"][0] == "suspension"
    assert receipt["c1_safety_coverage"]["status"] == "unavailable"
    assert ex.confirm_export_result(str(tmp_path / "incomplete.zip"), manifest, receipt)["verification"] == "PASS"
    assert ex.owner_receipt_text(receipt)[0] == "Incomplete State Export"


def test_versions_record_version_and_the_restore_boundary_are_pinned(vsource):
    assert ex.READ_VERSIONS == ({"major": 2, "minor": 0}, {"major": 2, "minor": 1}, {"major": 2, "minor": 2})
    assert ex.VERIFY_VERSIONS == ex.READ_VERSIONS + ({"major": 2, "minor": 3},)
    assert c1.RECOVERY_STATE_SCHEMA == "lumina.c1_recovery_state" and c1.RECOVERY_STATE_VERSION == 1
    verified(vsource)
    # S3a is an older restore engine for 2.3: intake refuses it (A-1, S3b).
    intake = ri.intake_archive(str(vsource[2]))
    assert intake["kind"] == ri.REJECTED and intake["refusal"]["code"] == "FORMAT_UNSUPPORTED"
    hint = ex.classify_input(str(vsource[2]))
    assert hint == {"kind": "agent_export_v2_verified", "verification": "not_performed",
                    "full_agent_coverage": False, "coverage_blockers": ["verification_not_performed"]}


def test_existing_destination_is_refused_and_preserved(vsource):
    vsource[2].write_bytes(b"PRIOR OWNER FILE")
    exc = refused(verified, vsource)
    assert "Verified Agent Backup needs a new destination" in str(exc)
    assert vsource[2].read_bytes() == b"PRIOR OWNER FILE"


def test_absent_c1_state_is_named_first_even_when_the_destination_exists(source):
    # The entry point checks C1 before the shared preflight's destination rule.
    source[2].write_bytes(b"PRIOR OWNER FILE")
    exc = refused(verified, source)
    assert exc.code == "C1_STATE_ABSENT" and "safety-suspension" in str(exc)
    assert source[2].read_bytes() == b"PRIOR OWNER FILE"


def test_absent_c1_state_refuses_before_any_collection(source, monkeypatch):
    monkeypatch.setattr(ab, "_collect_members", lambda *a, **k: pytest.fail("collected before refusing"))
    exc = refused(verified, source)
    assert exc.code == "C1_STATE_ABSENT" and "safety-suspension" in str(exc)
    assert "never proof of a first installation" in str(exc)
    assert not source[2].exists()


# ---------------------------------------------------------------------------
# G-1: out of process, trips never wait
# ---------------------------------------------------------------------------

def test_capture_and_recheck_run_in_a_child_never_in_this_process(vsource, monkeypatch):
    monkeypatch.setattr(c1, "capture_recovery_state", lambda *a, **k: pytest.fail("in-process capture"))
    monkeypatch.setattr(c1, "observe_store_incarnation", lambda *a, **k: pytest.fail("in-process recheck"))
    monkeypatch.setattr(S.SuspensionStore, "recovery_snapshot", lambda *a, **k: pytest.fail("in-process snapshot"))
    spawned = []
    real = subprocess.Popen

    def spy(cmd, **kwargs):
        proc = real(cmd, **kwargs)
        spawned.append((cmd, kwargs, proc))
        return proc
    monkeypatch.setattr(subprocess, "Popen", spy)
    _manifest, receipt = verified(vsource)
    assert receipt["full_agent_coverage"] is True
    assert [cmd[6] for cmd, _, _ in spawned] == ["capture", "incarnation"]
    for cmd, kwargs, proc in spawned:
        assert cmd[:6] == [sys.executable, "-E", "-s", "-B", "-m", "core.agent_backup_c1_child"]
        assert cmd[7] == str(vsource[0])
        assert kwargs["cwd"] == ex._CODE_ROOT == str(REPO)
        assert kwargs["stdin"] is subprocess.DEVNULL and kwargs["stderr"] is subprocess.DEVNULL
        assert kwargs["stdout"] is subprocess.PIPE
        assert kwargs["start_new_session"] is True and kwargs["close_fds"] is True
        assert kwargs["env"] == {"PATH": os.defpath, "LANG": "C.UTF-8", "LUMINA_TESTING": "1"}
        assert proc.returncode == 0 and proc.pid != os.getpid()


def test_child_env_carries_test_mode_only_when_exactly_on(monkeypatch):
    monkeypatch.setenv("LUMINA_TESTING", "yes")
    monkeypatch.setenv("PYTHONPATH", "/somewhere")
    assert ex._child_env() == {"PATH": os.defpath, "LANG": "C.UTF-8"}
    monkeypatch.setenv("LUMINA_TESTING", "1")
    assert ex._child_env() == {"PATH": os.defpath, "LANG": "C.UTF-8", "LUMINA_TESTING": "1"}


def test_trips_commit_while_a_verified_export_runs(vsource):
    data = vsource[0]
    store = S.open_store(data)
    stop, results, errors = threading.Event(), [], []

    def loop():
        try:
            while not stop.is_set():
                started = time.perf_counter()
                outcome = store.trip(store.capture("channel:wire:g1"), trigger=S.Trigger.BREAKER_TRIPPED,
                                     evidence={"trip_kind": "stale_streak"})
                results.append((outcome.result, time.perf_counter() - started))
                view = next(v for v in store.operator_view() if v.scope == "channel:wire:g1")
                exp = view.expectation()
                store.resume(S.desktop_recovery_event(local("resume"), exp, ack_unknown=False), exp)
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)
    worker = threading.Thread(target=loop)
    worker.start()
    try:
        while not results:
            time.sleep(0.005)
        _manifest, receipt = verified(vsource)
    finally:
        stop.set()
        worker.join(30)
        store.close()
    assert errors == []
    assert results and all(r is S.TripResult.TRIPPED for r, _ in results)
    assert receipt["full_agent_coverage"] is True


# ---------------------------------------------------------------------------
# G-2: the bounded child protocol (fake children; nothing is published)
# ---------------------------------------------------------------------------

def fake_child(monkeypatch, script, *, modes=("capture",)):
    real = ex._child_command

    def command(mode, data_dir):
        if mode in modes:
            return [sys.executable, "-c", script]
        return real(mode, data_dir)
    monkeypatch.setattr(ex, "_child_command", command)


@pytest.mark.parametrize("script,code,detail", [
    ("import sys; sys.stderr.write('x' * 500000); sys.exit(1)", "C1_SNAPSHOT_CHILD_FAILED", "exit"),
    ("import os, signal; os.kill(os.getpid(), signal.SIGKILL)", "C1_SNAPSHOT_CHILD_FAILED", "signal"),
    ("pass", "C1_SNAPSHOT_CHILD_FAILED", "garbage"),
    ("import sys; sys.stdout.write('STORE_FAULT\\n'); sys.exit(3)", "C1_SNAPSHOT_STORE_FAULT", None),
    ("import sys; sys.stdout.write('NOT_A_CODE\\n'); sys.exit(3)", "C1_SNAPSHOT_CHILD_FAILED", "garbage"),
    ("import sys; sys.stdout.write('STORE_FAULT'); sys.exit(3)", "C1_SNAPSHOT_CHILD_FAILED", "garbage"),
    ("import sys; sys.stdout.write('STORE_FAULT\\nSTORE_FAULT\\n'); sys.exit(3)", "C1_SNAPSHOT_CHILD_FAILED", "garbage"),
    ("import sys; sys.stdout.write('BOUNDARY_CHANGED\\n'); sys.exit(4)", "C1_SNAPSHOT_EXCLUSION_BOUNDARY_CHANGED", None),
    ("import sys; sys.stdout.write('STORE_FAULT\\n'); sys.exit(4)", "C1_SNAPSHOT_CHILD_FAILED", "garbage"),
    ("import sys; sys.stdout.write('STORE_TOO_LARGE\\n'); sys.exit(5)", "C1_SNAPSHOT_STORE_TOO_LARGE", None),
    ("import sys; sys.stdout.write('USAGE\\n'); sys.exit(5)", "C1_SNAPSHOT_CHILD_FAILED", "garbage"),
    ("import sys; sys.exit(7)", "C1_SNAPSHOT_CHILD_FAILED", "exit"),
])
def test_every_child_outcome_maps_to_a_closed_refusal(vsource, monkeypatch, script, code, detail):
    fake_child(monkeypatch, script)
    exc = refused(verified, vsource)
    assert exc.code == code
    assert exc.detail == detail
    lint(ex.owner_message(exc))
    assert "safety-suspension" in str(exc)
    assert not vsource[2].exists()
    assert not [p for p in vsource[2].parent.iterdir() if p.name.startswith((".incomplete_export_", ".agent_backup_"))]


def test_a_record_that_is_not_canonical_json_is_refused(vsource, monkeypatch):
    fake_child(monkeypatch, "import sys; sys.stdout.write('{\"not\": \"a record\"}')")
    exc = refused(verified, vsource)
    assert exc.code == "C1_SNAPSHOT_RECORD_INVALID"
    assert not vsource[2].exists()


def _gone(pid, within=5.0):
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.02)
    return False


def test_a_hung_child_and_its_process_group_are_killed_and_reaped(vsource, monkeypatch, tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    fake_child(monkeypatch, "import subprocess, sys, time\n"
                            f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
                            f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
                            "time.sleep(120)\n")
    monkeypatch.setattr(c1_child, "CAPTURE_TIMEOUT_S", 2)
    procs = []
    real = subprocess.Popen

    def spy(cmd, **kwargs):
        procs.append(real(cmd, **kwargs))
        return procs[-1]
    monkeypatch.setattr(subprocess, "Popen", spy)
    started = time.monotonic()
    exc = refused(verified, vsource)
    assert exc.code == "C1_SNAPSHOT_CHILD_FAILED" and exc.detail == "timeout"
    assert time.monotonic() - started < 30
    assert procs and procs[0].returncode is not None          # reaped, no zombie
    assert _gone(procs[0].pid)
    assert _gone(int(pidfile.read_text()))                    # killpg reached the grandchild
    assert not vsource[2].exists()


@pytest.mark.parametrize("exit_code", [0, 7])
def test_a_grandchild_never_outlives_a_child_that_already_exited(vsource, monkeypatch, tmp_path, exit_code):
    # Sonnet S3A-01: the group is killed while the exited child is still unreaped.
    pidfile = tmp_path / "grandchild.pid"
    fake_child(monkeypatch, "import subprocess, sys\n"
                            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],\n"
                            "                     stdout=subprocess.DEVNULL)\n"
                            f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
                            f"sys.stdout.write('x'); sys.exit({exit_code})\n")
    if exit_code:
        exc = refused(ex._capture_c1_record, str(vsource[0]))
        assert exc.code == "C1_SNAPSHOT_CHILD_FAILED" and exc.detail == "exit"
    else:
        assert ex._capture_c1_record(str(vsource[0])) == b"x"
    assert _gone(int(pidfile.read_text()))


def test_repeated_child_runs_leak_no_descriptor(vsource):
    def open_fds():
        return len(os.listdir("/proc/self/fd"))
    if not os.path.isdir("/proc/self/fd"):
        pytest.skip("no /proc on this platform")
    ex._capture_c1_record(str(vsource[0]))
    before = open_fds()
    for _ in range(5):
        ex._capture_c1_record(str(vsource[0]))
        ex._observe_c1_incarnation(str(vsource[0]))
    assert open_fds() == before


def test_the_child_limits_really_apply(tmp_path):
    # Sonnet S3A-02: apply them in a throwaway interpreter and read them back.
    probe = ("import os, resource, signal, sys\n"
             "import core.agent_backup_c1_child as c\n"
             "base = os.nice(0)\n"
             "c._apply_limits()\n"
             "adj = open('/proc/self/oom_score_adj').read().strip() if sys.platform.startswith('linux') else '1000'\n"
             "print(resource.getrlimit(resource.RLIMIT_CPU)[0], base, os.nice(0), adj, signal.alarm(0))\n")
    done = subprocess.run([sys.executable, "-E", "-s", "-B", "-c", probe], cwd=str(REPO),
                          env={"PATH": os.defpath, "LANG": "C.UTF-8"}, capture_output=True, timeout=60)
    cpu, base, nice, adj, alarm = done.stdout.decode().split()
    assert int(cpu) == c1_child.CPU_LIMIT_S
    assert int(nice) == min(19, int(base) + 10)
    assert adj == "1000"
    assert c1_child.SELF_LIMIT_S - 5 <= int(alarm) <= c1_child.SELF_LIMIT_S


def test_a_flooding_child_is_cut_off_at_the_cap(vsource, monkeypatch):
    fake_child(monkeypatch, "import os\nwhile True:\n    os.write(1, b'x' * 65536)\n")
    monkeypatch.setattr(c1, "RECOVERY_STATE_MAX_BYTES", 1 << 20)
    read = []
    real_read = os.read

    def counting(fd, n):
        chunk = real_read(fd, n)
        if n == 1 << 16:              # the exporter's own pipe reads only
            read.append(len(chunk))
        return chunk
    monkeypatch.setattr(os, "read", counting)
    exc = refused(verified, vsource)
    monkeypatch.setattr(os, "read", real_read)
    assert exc.code == "C1_SNAPSHOT_CHILD_FAILED" and exc.detail == "overflow"
    assert sum(read) <= (1 << 20) + (1 << 16)
    assert not vsource[2].exists()


@pytest.mark.parametrize("executable", ["", "python3", None])
def test_an_unusable_interpreter_is_a_spawn_refusal(vsource, monkeypatch, executable):
    monkeypatch.setattr(sys, "executable", executable)
    exc = refused(ex._capture_c1_record, str(vsource[0]))
    assert exc.code == "C1_SNAPSHOT_CHILD_FAILED" and exc.detail == "spawn"


def test_a_missing_interpreter_is_a_spawn_refusal(vsource, monkeypatch):
    monkeypatch.setattr(ex, "_child_command", lambda mode, d: ["/nonexistent/python-for-s3a"])
    exc = refused(ex._capture_c1_record, str(vsource[0]))
    assert exc.code == "C1_SNAPSHOT_CHILD_FAILED" and exc.detail == "spawn"


@pytest.mark.parametrize("line", ["zz zz\n", "a b c\n", "", "0" * 32 + " " + "0" * 32])
def test_a_garbled_incarnation_answer_is_refused(vsource, monkeypatch, line):
    fake_child(monkeypatch, f"import sys; sys.stdout.write({line!r})", modes=("incarnation",))
    exc = refused(verified, vsource)
    assert exc.code == "C1_SNAPSHOT_CHILD_FAILED" and exc.detail == "garbage"
    assert not vsource[2].exists()


def test_the_real_child_refuses_an_oversized_store_before_copying_it(vsource):
    wal = vsource[0] / "memory" / (S.STORE_NAME + "-wal")
    with open(wal, "wb") as handle:
        handle.truncate(c1_child.STORE_MAX_BYTES + 1)   # sparse: no real disk use
    try:
        exc = refused(ex._capture_c1_record, str(vsource[0]))
        assert exc.code == "C1_SNAPSHOT_STORE_TOO_LARGE"
    finally:
        wal.unlink()


def test_the_real_child_writes_nothing_but_sqlite_sidecars(vsource, monkeypatch, tmp_path):
    # TS3-02: a fresh code root with no __pycache__, a pristine data root.
    code = tmp_path / "code"
    shutil.copytree(REPO / "core", code / "core", ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setattr(ex, "_CODE_ROOT", str(code))
    data = vsource[0]
    for name in SIDECARS:
        (data / "memory" / name).unlink(missing_ok=True)
    code_before, data_before = tree(code), tree(data)
    record = ex._capture_c1_record(str(data))
    identity = ex._observe_c1_incarnation(str(data))
    assert ex.c1_observation_from_bytes(record)["claimed_source_incarnation"] == identity[1]
    assert tree(code) == code_before
    assert not list(code.rglob("__pycache__")) and not list(code.rglob("*.pyc"))
    data_after = tree(data)
    assert set(data_after) - set(data_before) <= {os.path.join("memory", n) for n in SIDECARS}
    assert {k: v for k, v in data_after.items() if k in data_before} == data_before


def test_the_child_import_closure_is_stdlib_and_core_only(vsource, tmp_path):
    # TS3-09: run the real child module, production-mode (no LUMINA_TESTING),
    # under the real flags, and list what it imported.
    probe = ("import sys, runpy\n"
             f"sys.argv = ['c1child', 'capture', {str(vsource[0])!r}]\n"
             "try:\n    runpy.run_module('core.agent_backup_c1_child', run_name='__main__')\n"
             "except SystemExit as exc:\n    code = exc.code\n"
             "sys.stderr.write(repr(code) + '\\n' + '\\n'.join(sorted(sys.modules)))\n")
    done = subprocess.run([sys.executable, "-E", "-s", "-B", "-c", probe], cwd=str(REPO),
                          env={"PATH": os.defpath, "LANG": "C.UTF-8"}, capture_output=True, timeout=120)
    lines = done.stderr.decode().splitlines()
    assert lines[0] == "0", done.stderr[-2000:]
    assert ex.c1_observation_from_bytes(done.stdout)["status"] == "captured_sanitized"
    # Interpreter startup (`site` and the base install's .pth hooks) is the
    # same for the parent Lumina process; -s removes user site-packages. The
    # child's own imports, beyond that startup set, are stdlib and core only.
    bare = subprocess.run([sys.executable, "-E", "-s", "-B", "-c",
                           "import sys; sys.stderr.write('\\n'.join(sorted(sys.modules)))"],
                          cwd=str(tmp_path), env={"PATH": os.defpath, "LANG": "C.UTF-8"},
                          capture_output=True, timeout=60)
    startup = {name.split(".")[0] for name in bare.stderr.decode().splitlines()}
    tops = {name.split(".")[0] for name in lines[1:]}
    assert tops - startup - set(sys.stdlib_module_names) - {"core", "__main__"} == set()
    assert not [n for n in startup if n.startswith(("core", "PySide6", "config", "platformdirs"))]
    assert "PySide6" not in tops and "config" not in tops and "platformdirs" not in tops
    # cwd is first on sys.path: nothing at the code root may shadow those stdlib names.
    root_names = {p.stem if p.suffix == ".py" else p.name for p in REPO.iterdir()}
    assert root_names & (tops & set(sys.stdlib_module_names)) == set()


def test_the_child_applies_its_limits_before_importing_core():
    tree_ = ast.parse((REPO / "core" / "agent_backup_c1_child.py").read_text(encoding="utf-8"))
    top_imports = {a.name for n in tree_.body if isinstance(n, ast.Import) for a in n.names}
    assert top_imports == {"os", "signal", "sys"}
    assert not [n for n in tree_.body if isinstance(n, ast.ImportFrom)]
    main = next(n for n in tree_.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    limits = min(n.lineno for n in ast.walk(main) if isinstance(n, ast.Call)
                 and getattr(n.func, "id", None) == "_apply_limits")
    core_import = min(n.lineno for n in ast.walk(main) if isinstance(n, ast.ImportFrom) and n.module == "core")
    assert limits < core_import
    assert c1_child.SELF_LIMIT_S == c1_child.CAPTURE_TIMEOUT_S + 30 == 150
    assert c1_child.CPU_LIMIT_S == 60 and c1_child.STORE_MAX_BYTES == 512 * 1024 * 1024


def test_the_record_is_charged_to_the_export_budget(vsource, monkeypatch):
    charged = []
    real = ab._AggregateBudget.charge

    def spy(self, nbytes, label):
        charged.append((label, nbytes))
        return real(self, nbytes, label)
    monkeypatch.setattr(ab._AggregateBudget, "charge", spy)
    manifest, _ = verified(vsource)
    assert ("C1 recovery record", c1_member(manifest)["size"]) in charged


# ---------------------------------------------------------------------------
# TS3-03 (A-3): a store replaced after the capture is never published
# ---------------------------------------------------------------------------

def _replace_store(data):
    memory = Path(data) / "memory"
    for name in (S.STORE_NAME, *SIDECARS, S.IDENTITY_NAME):
        (memory / name).unlink(missing_ok=True)
    shutil.rmtree(memory / S.EVIDENCE_DIRNAME, ignore_errors=True)
    S.initialize(data, ingress=local("init")).close()


def _replace_after_capture(monkeypatch):
    real = ex._capture_c1_record

    def capture(data_dir):
        record = real(data_dir)
        _replace_store(data_dir)          # unlink and recreate, possibly at a reused inode
        return record
    monkeypatch.setattr(ex, "_capture_c1_record", capture)


def test_unlink_and_recreate_after_the_capture_is_refused(vsource, monkeypatch):
    _replace_after_capture(monkeypatch)
    exc = refused(verified, vsource)
    assert exc.code in {"C1_SNAPSHOT_STORE_REPLACED", "C1_EXCLUSION_BOUNDARY_CHANGED"}
    assert not vsource[2].exists()


def test_the_incarnation_recheck_alone_catches_a_replacement_the_inode_check_misses(vsource, monkeypatch):
    # Lab L2: ext4 reuses the inode number; simulate that by blinding the
    # parent's (dev, ino) boundary check.
    monkeypatch.setattr(c1, "verify_exclusion_unchanged", lambda *a, **k: None)
    _replace_after_capture(monkeypatch)
    exc = refused(verified, vsource)
    assert exc.code == "C1_SNAPSHOT_STORE_REPLACED"
    assert "replaced while the backup was being made" in str(exc)
    assert not vsource[2].exists()


def test_the_recheck_runs_after_the_gate_and_just_before_publication():
    source_text = (REPO / "core" / "agent_backup.py").read_text(encoding="utf-8")
    body = source_text[source_text.index("def _build_agent_backup_core("):]
    early = body.index("verified_publication_gate(manifest)")
    zip_work = body.index("zipfile.ZipFile(tmp_path, \"w\"")
    gate = body.index("verified_publication_gate(built_manifest)")
    recheck = body.index("recheck_c1_incarnation(data_dir, built_manifest, built_data)")
    publish = body.index("publication_observation = publish_new_export(")
    assert early < zip_work < gate < recheck < publish


# ---------------------------------------------------------------------------
# A-6 / TS3-11: the publication gate and the one blockers function
# ---------------------------------------------------------------------------

def _drop_marker(data):
    (data / "knowledge_packs_installed.json").unlink()


def _unknown_pref(data):
    prefs = json.loads((data / "memory" / "prefs.json").read_text())
    prefs["mystery_pref_from_the_future"] = 1
    (data / "memory" / "prefs.json").write_text(json.dumps(prefs))


@pytest.mark.parametrize("blocker,setup", [
    ("installation_marker", _drop_marker),
    ("unclassified_preferences", _unknown_pref),
    ("inventory", None),
])
def test_each_blocker_alone_refuses_in_owner_words_before_any_zip_work(vsource, monkeypatch, blocker, setup):
    if setup:
        setup(vsource[0])
    else:
        def unavailable(*a, **k):
            raise inv.InventoryUnavailable("synthetic")
        monkeypatch.setattr(inv, "inventory", unavailable)
    monkeypatch.setattr(ab, "_capture_and_verify_archive", lambda *a, **k: pytest.fail("zip work after a blocker"))
    exc = refused(verified, vsource)
    assert exc.code == "VERIFIED_BLOCKED" and exc.detail == (blocker,)
    assert ex.BLOCKER_WORDS[blocker] in str(exc)
    lint(str(exc))
    assert not vsource[2].exists()
    assert not [p for p in vsource[2].parent.iterdir() if p.name.startswith((".incomplete_export_", ".agent_backup_"))]


@pytest.fixture
def gen_source(tmp_path):
    from test_agent_backup import _make_env
    data, base = map(Path, _make_env(tmp_path))
    (data / "memory").chmod(0o700)
    (data / "knowledge_packs_installed.json").write_text(json.dumps(
        {"lumina-self-knowledge": {"version": 1, "installed_at": "2026-10-20"}}))
    arts = _ingest(data)
    c1_store(data)
    return data, base, tmp_path / "out" / "verified.zip", arts


def test_a_blocked_image_set_refuses_the_verified_export(gen_source):
    data, base, dest, arts = gen_source
    _blob(data, arts[0]).unlink()
    dest.parent.mkdir(parents=True)
    exc = refused(verified, (data, base, dest))
    assert exc.code == "VERIFIED_BLOCKED" and exc.detail == ("generation_artifacts",)
    assert not dest.exists()


def test_the_gate_and_the_receipts_share_one_blockers_function():
    module = ast.parse((REPO / "core" / "agent_backup_export.py").read_text(encoding="utf-8"))
    funcs = {n.name: n for n in module.body if isinstance(n, ast.FunctionDef)}

    def calls(name):
        return {getattr(c.func, "id", None) for c in ast.walk(funcs[name]) if isinstance(c, ast.Call)}
    assert "coverage_blockers" in calls("verified_publication_gate")
    assert "coverage_blockers" in calls("_derived_claims")
    assert "_derived_claims" in calls("_receipt") and "_derived_claims" in calls("confirm_export_result")
    appenders = {name for name, f in funcs.items() for c in ast.walk(f)
                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == "append"
                 and getattr(c.func.value, "id", None) == "blockers"}
    assert appenders == {"coverage_blockers"}
    # The builder writes the observation only from c1_observation_from_bytes.
    prep = funcs["prepare_export_members"]
    sources = [a.value for a in ast.walk(prep) if isinstance(a, ast.Assign)
               and any(getattr(t, "id", None) == "observation" for t in a.targets)]
    assert len(sources) == 1 and sources[0].func.id == "c1_observation_from_bytes"


def test_a_builder_that_writes_a_different_observation_never_publishes(vsource, monkeypatch):
    real = ex.prepare_export_members

    def lying(*a, **k):
        members, observations = real(*a, **k)
        observations["c1_recovery"] = dict(observations["c1_recovery"],
                                           redaction_total=observations["c1_recovery"]["redaction_total"] + 1)
        return members, observations
    monkeypatch.setattr(ex, "prepare_export_members", lying)
    with pytest.raises(ab.AgentBackupError):
        verified(vsource)
    assert not vsource[2].exists()


def test_coverage_blockers_by_format(vsource, tmp_path):
    manifest, _ = verified(vsource)
    assert ex.coverage_blockers(manifest) == []
    m22, _ = incomplete(vsource, tmp_path / "i.zip")
    assert ex.coverage_blockers(m22) == ["suspension"]
    relabelled = json.loads(json.dumps(m22))
    relabelled["format_version"] = dict(ex.VERIFIED_VERSION)
    assert ex.coverage_blockers(relabelled) == ["suspension"]      # no c1_recovery, no coverage
    stripped = json.loads(json.dumps(manifest))
    stripped["export_observations"]["c1_recovery"]["status"] = "unavailable"
    assert ex.coverage_blockers(stripped) == ["suspension"]


# ---------------------------------------------------------------------------
# G-3: hostile 2.3 archives fail verification
# ---------------------------------------------------------------------------

def _obs(manifest):
    return manifest["export_observations"]["c1_recovery"]


def _record_path(manifest):
    return c1_member(manifest)["archive_path"]


def _other_valid_record(manifest, files):
    record = json.loads(files[_record_path(manifest)])
    record["owner_scope"]["state_revision"] += 1
    files[_record_path(manifest)] = c1.canonical_recovery_payload(record)


def _noncanonical(manifest, files):
    record = json.loads(files[_record_path(manifest)])
    files[_record_path(manifest)] = json.dumps(record, indent=1).encode()
    _obs(manifest)["record_sha256"] = hashlib.sha256(files[_record_path(manifest)]).hexdigest()


def _duplicate_key(manifest, files):
    raw = files[_record_path(manifest)]
    files[_record_path(manifest)] = b'{"audit":0,' + raw[1:]
    _obs(manifest)["record_sha256"] = hashlib.sha256(files[_record_path(manifest)]).hexdigest()


def _deep(manifest, files):
    files[_record_path(manifest)] = b"[" * 200000 + b"]" * 200000
    _obs(manifest)["record_sha256"] = hashlib.sha256(files[_record_path(manifest)]).hexdigest()


def _set_obs(key, value):
    def mutate(manifest, files):
        _obs(manifest)[key] = value
    return mutate


def _drop_member(manifest, files):
    path = _record_path(manifest)
    manifest["members"] = [m for m in manifest["members"] if m["logical_id"] != ex.C1_MEMBER_ID]
    del files[path]


def _two_members(manifest, files):
    extra = dict(c1_member(manifest), archive_path="state/c1/copy.json")
    manifest["members"].append(extra)
    files["state/c1/copy.json"] = files[_record_path(manifest)]


def _exclusion_v1(manifest, files):
    manifest["c1_exclusion"] = {"schema": c1.SCHEMA, "version": 1, "status": "present_excluded",
                                "observed_at": manifest["c1_exclusion"]["observed_at"], "warnings": [],
                                "snapshot_captured": False, "enforcement_compatibility": "unproven",
                                "c1_schema_version": "unknown", "suspended_at_capture": "unknown",
                                "restore_fence": "c1_trace_and_owner_recover_required"}


def _snapshot_key(manifest, files):
    manifest["c1_exclusion"]["snapshot_captured"] = True


def _mode(manifest, files):
    manifest["backup_mode"] = "incomplete_state_export"


def _suspension_obs(manifest, files):
    manifest["export_observations"]["suspension"] = "unavailable_present"


def _drop_obs(manifest, files):
    del manifest["export_observations"]["c1_recovery"]


def _unclassified(manifest, files):
    manifest["export_observations"]["prefs_unclassified_count"] = 1


def _inventory_blocking(manifest, files):
    manifest["export_observations"]["inventory"]["blocking_entries"] = 1


def _extra_member_field(manifest, files):
    c1_member(manifest)["extensions"] = {"version": 1, "records": {}}


def _member_policy(manifest, files):
    c1_member(manifest)["portable"] = "HISTORICAL_EVIDENCE"


@pytest.mark.parametrize("name,mutate,expected", [
    ("record_replaced", _other_valid_record, "EXPORT_C1_OBSERVATION_MISMATCH"),
    ("observation_sha", _set_obs("record_sha256", "0" * 64), "EXPORT_C1_OBSERVATION_MISMATCH"),
    ("audit_truncated", _set_obs("audit_truncated", True), "EXPORT_C1_OBSERVATION_MISMATCH"),
    ("incidents_truncated", _set_obs("incidents_truncated", True), "EXPORT_C1_OBSERVATION_MISMATCH"),
    ("runtimes_truncated", _set_obs("runtimes_truncated", True), "EXPORT_C1_OBSERVATION_MISMATCH"),
    ("redaction_total", _set_obs("redaction_total", 0), "EXPORT_C1_OBSERVATION_MISMATCH"),
    ("incarnation", _set_obs("claimed_source_incarnation", "0" * 32), "EXPORT_C1_OBSERVATION_MISMATCH"),
    ("status", _set_obs("status", "unavailable"), "EXPORT_COVERAGE_OBSERVATIONS_INVALID"),
    ("noncanonical", _noncanonical, "EXPORT_C1_RECORD_INVALID"),
    ("duplicate_key", _duplicate_key, "EXPORT_C1_RECORD_INVALID"),
    ("deep_json", _deep, "EXPORT_C1_RECORD_INVALID"),
    ("member_missing", _drop_member, "EXPORT_C1_MEMBER_MISSING_OR_DUPLICATE"),
    ("two_members", _two_members, "EXPORT_C1_MEMBER_MISSING_OR_DUPLICATE"),
    ("member_extra_field", _extra_member_field, "EXPORT_C1_MEMBER_INVALID"),
    ("member_policy", _member_policy, "EXPORT_C1_MEMBER_INVALID"),
    ("exclusion_v1_on_2_3", _exclusion_v1, "EXPORT_C1_EXCLUSION_INVALID"),
    ("snapshot_captured_key", _snapshot_key, "EXPORT_C1_EXCLUSION_INVALID"),
    ("mode_incomplete", _mode, "EXPORT_FORMAT_OR_MODE_UNSUPPORTED"),
    ("suspension_observation", _suspension_obs, "EXPORT_C1_OBSERVATION_MISMATCH"),
    ("observation_missing", _drop_obs, "EXPORT_COVERAGE_OBSERVATIONS_INVALID"),
    ("blocker_prefs", _unclassified, "EXPORT_VERIFIED_MODE_WITH_BLOCKERS"),
    ("blocker_inventory", _inventory_blocking, "EXPORT_VERIFIED_MODE_WITH_BLOCKERS"),
])
def test_hostile_2_3_archives_never_verify(vsource, tmp_path, name, mutate, expected):
    verified(vsource)
    hostile = rebuild(vsource[2], tmp_path / f"{name}.zip", mutate)
    errors = errors_of(hostile)
    assert expected in errors, errors
    intake = ri.intake_archive(str(hostile))
    assert intake["kind"] == ri.REJECTED


def _manifest_and_files(path):
    items = entries(path)
    return json.loads(dict(items)["manifest.json"]), {n: d for n, d in items if n != "manifest.json"}


@pytest.mark.parametrize("name,mutate,schema_code", [
    ("member_missing", _drop_member, "EXPORT_C1_MEMBER_MISSING_OR_DUPLICATE"),
    ("two_members", _two_members, "EXPORT_C1_MEMBER_MISSING_OR_DUPLICATE"),
    ("member_policy", _member_policy, "EXPORT_C1_MEMBER_INVALID"),
    ("observation_sha", _set_obs("record_sha256", "0" * 64), "EXPORT_C1_MEMBER_INVALID"),
    ("blocker_prefs", _unclassified, "EXPORT_VERIFIED_MODE_WITH_BLOCKERS"),
])
def test_each_c1_rule_holds_in_the_schema_hook_alone(vsource, tmp_path, name, mutate, schema_code):
    verified(vsource)
    manifest, _files = _manifest_and_files(rebuild(vsource[2], tmp_path / f"{name}.zip", mutate))
    assert schema_code in ex.validate_export_schema(manifest)


@pytest.mark.parametrize("mutate", [_other_valid_record, _set_obs("audit_truncated", True), _noncanonical])
def test_the_record_crosscheck_holds_in_each_hook_alone(vsource, tmp_path, mutate):
    verified(vsource)
    path = rebuild(vsource[2], tmp_path / "h.zip", mutate)
    manifest, files = _manifest_and_files(path)
    member = c1_member(manifest)
    payload = ex.validate_export_payload(member, files[member["archive_path"]], manifest)
    with zipfile.ZipFile(path) as zf:
        archive = ex.validate_export_archive(manifest, zf)
    for errors in (payload, archive):
        assert errors and set(errors) <= {"EXPORT_C1_OBSERVATION_MISMATCH", "EXPORT_C1_RECORD_INVALID"}, errors


def test_a_2_2_manifest_carrying_the_c1_member_fails_in_each_hook(vsource, tmp_path):
    vmanifest, _ = verified(vsource)
    record = read_zip(vsource[2])[_record_path(vmanifest)]
    incomplete(vsource, tmp_path / "i.zip")

    def add_member(manifest, files):
        manifest["members"].append(dict(c1_member(vmanifest)))
        files[_record_path(vmanifest)] = record
    manifest, files = _manifest_and_files(rebuild(tmp_path / "i.zip", tmp_path / "h.zip", add_member))
    assert "EXPORT_C1_MEMBER_UNEXPECTED" in ex.validate_export_schema(manifest)
    assert ex.validate_export_payload(c1_member(manifest), record, manifest) == ["EXPORT_C1_MEMBER_UNEXPECTED"]


def test_each_absent_state_layer_refuses_on_its_own(source, monkeypatch):
    data, base, dest = source
    # Layer 1: the export preflight.
    with pytest.raises(ex.ExportUnavailable) as info:
        ex.preflight_incomplete(str(data), str(dest), verified=True)
    assert info.value.code == "C1_STATE_ABSENT"
    assert ex.preflight_incomplete(str(data), str(dest)).status == "absent_unknown"
    # Layer 2: the member preparation, with the preflight check bypassed.
    monkeypatch.setattr(ex, "preflight_incomplete", lambda d, p, verified=False: c1.describe_exclusions(d))
    monkeypatch.setattr(ex, "_capture_c1_record", lambda *a: pytest.fail("captured an absent store"))
    with pytest.raises(ex.ExportUnavailable) as info:
        ab._build_agent_backup_core(str(data), str(base), str(dest), identity_trailers_path=NO_IDENTITY_TRAILERS,
                                    credentials_path=NO_CREDENTIALS, _export_v2=True, _export_verified=True)
    assert info.value.code == "C1_STATE_ABSENT" and not dest.exists()


def test_the_incarnation_read_is_bracketed_by_the_boundary(vsource, monkeypatch):
    # Sonnet S3A-03 (M15): an evidence tree appearing while the store is open.
    data = vsource[0]
    real_open = S._open_store

    def opening(*a, **k):
        store = real_open(*a, **k)
        (data / "memory" / S.EVIDENCE_DIRNAME).mkdir(mode=0o700, exist_ok=True)
        return store
    monkeypatch.setattr(S, "_open_store", opening)
    assert not (data / "memory" / S.EVIDENCE_DIRNAME).exists()
    with pytest.raises(c1.ExclusionUnavailable) as info:
        c1.observe_store_incarnation(data)
    assert info.value.code == "BOUNDARY_CHANGED"


def test_the_binding_gate_sees_the_verified_built_manifest(vsource, monkeypatch):
    seen = []
    real = ex.verified_publication_gate

    def gate(manifest):
        seen.append(json.loads(json.dumps(manifest)))
        return real(manifest)
    monkeypatch.setattr(ex, "verified_publication_gate", gate)
    manifest, _ = verified(vsource)
    assert len(seen) == 2
    assert seen[1] == manifest == json.loads(dict(entries(vsource[2]))["manifest.json"])


def test_the_core_builder_itself_refuses_a_root_without_c1_state(source):
    data, base, dest = source
    with pytest.raises(ex.ExportUnavailable) as info:
        ab._build_agent_backup_core(str(data), str(base), str(dest), identity_trailers_path=NO_IDENTITY_TRAILERS,
                                    credentials_path=NO_CREDENTIALS, _export_v2=True, _export_verified=True)
    assert info.value.code == "C1_STATE_ABSENT" and not dest.exists()
    with pytest.raises(ValueError):
        ab._build_agent_backup_core(str(data), str(base), str(dest), _export_verified=True)


def test_the_settings_ui_shows_owner_words_and_the_derived_receipt_only():
    module = ast.parse((REPO / "ui" / "settings" / "memory_tab.py").read_text(encoding="utf-8"))
    worker = next(n for n in ast.walk(module) if isinstance(n, ast.ClassDef) and n.name == "_AgentBackupWorker")
    handler = next(h for h in ast.walk(worker) if isinstance(h, ast.ExceptHandler)
                   and getattr(h.type, "attr", None) == "ExportUnavailable")
    emitted = [c.args[0] for c in ast.walk(handler) if isinstance(c, ast.Call)
               and getattr(c.func, "attr", None) == "emit"]
    assert len(emitted) == 1 and getattr(emitted[0].func, "attr", None) == "owner_message"
    done = next(n for n in ast.walk(module) if isinstance(n, ast.FunctionDef) and n.name == "_agent_backup_completed")
    assert "owner_receipt_text" in {getattr(c.func, "attr", None) for c in ast.walk(done) if isinstance(c, ast.Call)}


def test_a_2_2_archive_can_never_carry_c1_coverage(vsource, tmp_path):
    vmanifest, _ = verified(vsource)
    record = read_zip(vsource[2])[_record_path(vmanifest)]
    incomplete(vsource, tmp_path / "i.zip")

    def add_member(manifest, files):
        manifest["members"].append(dict(c1_member(vmanifest)))
        files[_record_path(vmanifest)] = record

    def add_obs(manifest, files):
        manifest["export_observations"]["c1_recovery"] = dict(_obs(vmanifest))

    def v2_exclusion(manifest, files):
        manifest["c1_exclusion"] = dict(vmanifest["c1_exclusion"])

    def relabel(manifest, files):
        manifest["format_version"] = dict(ex.VERIFIED_VERSION)
        manifest["compatibility"]["min_supported_format_version"] = dict(ex.VERIFIED_VERSION)
        manifest["compatibility"]["max_known_format_version"] = dict(ex.VERIFIED_VERSION)
        manifest["backup_mode"] = "verified_agent_backup"
    for mutate, expected in ((add_member, "EXPORT_C1_MEMBER_UNEXPECTED"),
                             (add_obs, "EXPORT_COVERAGE_OBSERVATIONS_INVALID"),
                             (v2_exclusion, "EXPORT_C1_EXCLUSION_INVALID"),
                             (relabel, "EXPORT_C1_MEMBER_MISSING_OR_DUPLICATE")):
        errors = errors_of(rebuild(tmp_path / "i.zip", tmp_path / f"h-{mutate.__name__}.zip", mutate))
        assert expected in errors, (mutate.__name__, errors)


def test_confirm_refuses_every_laundered_claim(vsource, tmp_path):
    manifest, receipt = verified(vsource)
    dest = str(vsource[2])
    for key, value, code in (("full_agent_coverage", False, "EXPORT_UNSUPPORTED_SUCCESS_CLAIM"),
                             ("restorable_verified", True, "EXPORT_UNSUPPORTED_SUCCESS_CLAIM"),
                             ("coverage_blockers", ["suspension"], "EXPORT_RECEIPT_COVERAGE_MISMATCH"),
                             ("export_kind", "incomplete_state_export", "EXPORT_RECEIPT_COVERAGE_MISMATCH"),
                             ("format_version", dict(ex.VERSION), "EXPORT_RECEIPT_NOT_BOUND_TO_ARTIFACT")):
        with pytest.raises(ab.AgentBackupError, match=code):
            ex.confirm_export_result(dest, manifest, dict(receipt, **{key: value}))
    m22, r22 = incomplete(vsource, tmp_path / "i.zip")
    with pytest.raises(ab.AgentBackupError, match="EXPORT_UNSUPPORTED_SUCCESS_CLAIM"):
        ex.confirm_export_result(str(tmp_path / "i.zip"), m22, dict(r22, full_agent_coverage=True))
    with pytest.raises(ab.AgentBackupError, match="EXPORT_RECEIPT_COVERAGE_MISMATCH"):
        ex.confirm_export_result(str(tmp_path / "i.zip"), m22, dict(r22, coverage_blockers=[]))


# ---------------------------------------------------------------------------
# A-1 / TS3-01: 2.3 runs every 2.2 check (differential)
# ---------------------------------------------------------------------------

def _gen(manifest):
    return [m for m in manifest["members"] if m["archive_path"].startswith("state/artifacts/generation/")]


def _blob_member(manifest):
    return next(m for m in _gen(manifest) if not m["logical_id"].endswith(".manifest_json"))


def _alter_blob(manifest, files):
    files[_blob_member(manifest)["archive_path"]] = b"\x89PNG tampered"


def _drop_blob_and_sidecar(manifest, files):
    blob = _blob_member(manifest)
    gone = {blob["archive_path"], blob["archive_path"] + ".manifest.json"}
    manifest["members"] = [m for m in manifest["members"] if m["archive_path"] not in gone]
    for path in gone:
        files.pop(path, None)
    gen = manifest["export_observations"]["generation_artifacts"]
    gen["captured_blobs"] -= 1
    gen["captured_manifests"] -= 1


def _drop_blob_keep_sidecar(manifest, files):
    blob = _blob_member(manifest)
    manifest["members"] = [m for m in manifest["members"] if m is not blob]
    del files[blob["archive_path"]]
    manifest["export_observations"]["generation_artifacts"]["captured_blobs"] -= 1


def _count_mismatch(manifest, files):
    manifest["export_observations"]["generation_artifacts"]["captured_manifests"] -= 1


def _rows_mismatch(manifest, files):
    gen = manifest["export_observations"]["generation_artifacts"]
    gen["rows"] += 1


@pytest.mark.parametrize("mutate,rehash,must_contain", [
    (_alter_blob, True, "EXPORT_GENERATION_ROW_MISMATCH"),
    (_alter_blob, False, "hash mismatch for"),
    (_drop_blob_and_sidecar, True, "EXPORT_GENERATION_INCOMPLETE_CLAIM"),
    (_drop_blob_keep_sidecar, True, "EXPORT_GENERATION_MANIFEST_WITHOUT_BLOB"),
    (_count_mismatch, True, "EXPORT_GENERATION_OBSERVATION_MISMATCH"),
    (_rows_mismatch, True, None),
])
def test_every_2_2_hostile_case_fails_the_same_way_on_2_3(gen_source, tmp_path, mutate, rehash, must_contain):
    data, base, dest, _arts = gen_source
    dest.parent.mkdir(parents=True)
    v22, _ = incomplete((data, base, None), tmp_path / "out" / "i.zip")
    v23, _ = verified((data, base, dest))
    assert len(_gen(v22)) == len(_gen(v23)) == 4
    outcomes = {}
    for label, src in (("2.2", tmp_path / "out" / "i.zip"), ("2.3", dest)):
        errors = errors_of(rebuild(src, tmp_path / f"{label}-{mutate.__name__}.zip", mutate, rehash=rehash))
        assert errors, label
        outcomes[label] = sorted(e for e in errors if not e.startswith("EXPORT_C1")
                                 and e != "EXPORT_VERIFIED_MODE_WITH_BLOCKERS")
        if must_contain:
            assert any(e.startswith(must_contain) for e in errors), (label, errors)
    assert outcomes["2.2"] == outcomes["2.3"] and outcomes["2.2"]


def test_no_verifier_rule_names_a_version_outside_the_family_helpers():
    module = ast.parse((REPO / "core" / "agent_backup_export.py").read_text(encoding="utf-8"))
    owners = []
    for func in [n for n in ast.walk(module) if isinstance(n, ast.FunctionDef)]:
        for call in ast.walk(func):
            if (isinstance(call, ast.Call) and getattr(call.func, "id", None) == "_same_contract"
                    and any(getattr(a, "id", None) in ("VERSION", "VERIFIED_VERSION") for a in call.args)):
                owners.append(func.name)
    assert sorted(owners) == ["_is_verified_format", "_v22_family", "_v22_family"]
    text = (REPO / "core" / "agent_backup_export.py").read_text(encoding="utf-8")
    assert not re.search(r"==\s*(VERSION|VERIFIED_VERSION)\b", text)


# ---------------------------------------------------------------------------
# A-5 / TS3-08: one strict path from bytes to an observation
# ---------------------------------------------------------------------------

def golden():
    return GOLDEN.read_bytes()


@pytest.mark.parametrize("raw", [
    "a str, not bytes", b"", b"\xff\xfe", b"not json", b'{"x": NaN}',
    b"[" * 200000 + b"]" * 200000, None])
def test_bytes_that_are_not_one_strict_record_are_refused(raw):
    with pytest.raises(ex.C1RecordInvalid):
        ex.c1_observation_from_bytes(raw)


def test_canonical_bytes_of_an_invalid_record_are_refused():
    record = json.loads(golden())
    for change in ({"label": "original evidence"}, {"version": 2}, {"redactions": {"x": 0}}):
        with pytest.raises(ex.C1RecordInvalid):
            ex.c1_observation_from_bytes(c1.canonical_recovery_payload(dict(record, **change)))


def test_noncanonical_or_duplicated_golden_bytes_are_refused():
    raw = golden()
    for variant in (json.dumps(json.loads(raw), indent=1).encode(), raw + b"\n", b" " + raw,
                    b'{"audit":0,' + raw[1:], raw.replace(b'"version":1', b'"version":1.0', 1)):
        with pytest.raises(ex.C1RecordInvalid):
            ex.c1_observation_from_bytes(variant)


def test_an_oversized_record_is_refused_before_parsing(monkeypatch):
    monkeypatch.setattr(c1, "RECOVERY_STATE_MAX_BYTES", len(golden()) - 1)
    monkeypatch.setattr(ab, "_strict_json_loads", lambda *a: pytest.fail("parsed an oversized record"))
    with pytest.raises(ex.C1RecordInvalid):
        ex.c1_observation_from_bytes(golden())


def test_the_cache_answers_only_for_bytes_that_already_passed(monkeypatch):
    raw = golden()
    first = ex.c1_observation_from_bytes(raw)
    calls = []
    monkeypatch.setattr(c1, "validate_recovery_state", lambda r: calls.append(1) or (_ for _ in ()).throw(
        c1.RecoverySnapshotRefused("RECORD_INVALID")))
    assert ex.c1_observation_from_bytes(bytes(raw)) == first and calls == []
    record = json.loads(raw)
    record["owner_scope"]["state_revision"] += 1
    with pytest.raises(ex.C1RecordInvalid):
        ex.c1_observation_from_bytes(c1.canonical_recovery_payload(record))
    assert calls == [1]
    first["redaction_total"] = -5          # callers get a copy, never the cached value
    assert ex.c1_observation_from_bytes(raw)["redaction_total"] != -5
    monkeypatch.undo()
    for i in range(ex._C1_OBSERVATION_CACHE_MAX + 3):
        record["owner_scope"]["state_revision"] += 1
        ex.c1_observation_from_bytes(c1.canonical_recovery_payload(record))
        assert len(ex._c1_observation_cache) <= ex._C1_OBSERVATION_CACHE_MAX


# ---------------------------------------------------------------------------
# A-8 / TS3-12: golden record v1, append-only vocabularies
# ---------------------------------------------------------------------------

def test_the_golden_v1_record_is_pinned_and_still_accepted():
    raw = golden()
    assert hashlib.sha256(raw).hexdigest() == GOLDEN_SHA256
    record = json.loads(raw)
    assert record["schema"] == "lumina.c1_recovery_state" and record["version"] == 1
    c1.validate_recovery_state(record)
    observation = ex.c1_observation_from_bytes(raw)
    assert observation["record_sha256"] == GOLDEN_SHA256 and observation["status"] == "captured_sanitized"
    assert {i["trigger"] for i in record["incidents"]} == {"breaker_tripped", "second_batch_refused"}
    assert {i["status"] for i in record["incidents"]} == {"active", "resumed"}


def test_c1_vocabularies_are_append_only():
    assert S._AUDIT_ACTIONS >= {
        "initialize", "runtime_registered", "trip_foreign_incarnation", "trip_invalid_token",
        "late_trip_evidence", "trip_while_suspended", "trip", "scope_created_suspended", "resume",
        "resume_refused", "unknown_scopes_acknowledged", "identity_repaired", "no_trace_recovery",
        "destructive_recovery"}
    assert {t.value for t in S.Trigger} >= {"breaker_tripped", "second_batch_refused"}
    assert {k.value for k in S.LeaseKind} >= {"tool", "provider", "write", "background"}
    assert {r.value for r in S.ResumeResult} >= {
        "resumed", "no_such_incident", "incident_not_active", "expectation_mismatch", "event_before_incident",
        "event_not_bound_to_this_incident", "stale_local_authority", "drain_pending",
        "unknown_outcome_needs_acknowledgement"}
    assert S._FREE_TEXT_KEYS >= {
        "trip_kind", "stale_streak", "write_stale_streak", "executed_calls", "distinct_fingerprints",
        "refused_batches", "batch_size", "limit", "distinct_calls", "evidence_dropped", "reason",
        "unknown_outcome_leases", "trigger", "in_flight", "channel", "result", "orphans_acknowledged",
        "evidence", "receipt", "prior_state", "suspended_scopes", "unknown_scope_policy", "salvage_complete"}


# ---------------------------------------------------------------------------
# A-7 / TS3-10: owner words
# ---------------------------------------------------------------------------

def test_every_owner_message_is_plain_words():
    for code, text in ex.OWNER_MESSAGES.items():
        lint(text)
        if code.startswith(("C1_SNAPSHOT_", "C1_STATE_", "C1_EXCLUSION", "C1_PROTECTED")):
            assert "safety-suspension" in text, code
    for blocker in ex.BLOCKER_WORDS:
        lint(ex._blocked_message([blocker]))
    lint(ex._blocked_message(list(ex.BLOCKER_WORDS)))
    for code in sorted(c1.SNAPSHOT_REFUSALS):
        lint(ex.owner_message(ex.ExportUnavailable("C1_SNAPSHOT_" + code)))
    for code in sorted(c1.REFUSALS):
        lint(ex.owner_message(ex.ExportUnavailable("C1_SNAPSHOT_EXCLUSION_" + code)))
        lint(ex.owner_message(ex.ExportUnavailable("C1_EXCLUSION_" + code)))
    for code in ("C1_PROTECTED_PATH", "C1_PROTECTED_IDENTITY", "SOMETHING_UNKNOWN"):
        lint(ex.owner_message(ex.ExportUnavailable(code)))
    plain = "Incomplete State Export needs a new destination. Existing files are preserved."
    assert ex.owner_message(ex.ExportUnavailable(plain)) == plain


def test_the_verified_receipt_text_says_only_what_was_derived(vsource):
    manifest, receipt = verified(vsource)
    confirmed = ex.confirm_export_result(str(vsource[2]), manifest, receipt)
    title, text = ex.owner_receipt_text(confirmed)
    assert title == "Verified Agent Backup"
    lint(text)
    for line in ("Archive integrity and structure: PASS",
                 "C1 safety state: sanitized history record included (raw store excluded); "
                 "not restored as live safety state",
                 "C1 enforcement compatibility: UNPROVEN",
                 "Members were captured at different times; this is not one point-in-time snapshot.",
                 "Full recovery: NOT PROVEN until a restore is verified; restoring this format is not "
                 "yet supported by this build.",
                 "Not encrypted.", "Preserve prior backups."):
        assert line in text
    assert receipt["archive_sha256"] in text
    downgraded = dict(confirmed, full_agent_coverage=False)
    assert ex.owner_receipt_text(downgraded)[0] == "Incomplete State Export"
