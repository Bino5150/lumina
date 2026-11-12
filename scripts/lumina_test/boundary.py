"""Run one fresh Linux test process in an admitted, verified systemd service."""
from contextlib import contextmanager
import fcntl
import json
import math
import os
import re
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import uuid

from boundary_child import atomic_json

MEMORY = 2500 * 1024**2
MIN_AVAILABLE_KIB = 4 * 1024**2
MAX_PSI = 60
MAX_SWAP_RATE = 200
UNIT = "lumina-test-workload.service"
CGROUP_ROOT = Path("/sys/fs/cgroup")


class BoundaryError(RuntimeError):
    pass


def swap_totals():
    fields = dict(line.split() for line in Path("/proc/vmstat").read_text().splitlines())
    return tuple(int(fields[name]) for name in ("pswpin", "pswpout"))


def host_sample(previous, *, before_launch=False, memory=MEMORY):
    now = time.monotonic()
    swap = swap_totals()
    counters = [end - start for end, start in zip(swap, previous[1])]
    swap_rate = sum(counters) / (now - previous[0])
    available = next(int(line.split()[1]) for line in Path("/proc/meminfo").read_text().splitlines()
                     if line.startswith("MemAvailable:"))
    psi_row = next(line for line in Path("/proc/pressure/memory").read_text().splitlines()
                   if line.startswith("some "))
    psi = float(dict(field.split("=") for field in psi_row.split()[1:])["avg10"])
    floor = MIN_AVAILABLE_KIB + (memory // 1024 + 256 * 1024 if before_launch else 0)
    sample = {"available_kib": available, "psi_some_avg10": psi, "swap_pages_per_second": swap_rate}
    if (available < floor or not math.isfinite(psi) or not 0 <= psi <= MAX_PSI
            or min(counters) < 0 or swap_rate > MAX_SWAP_RATE):
        raise BoundaryError(f"host resource admission refused: {sample}, memory floor {floor} KiB")
    return (now, swap), sample


def manager_env():
    runtime = Path("/run/user") / str(os.getuid())
    if sys.platform != "linux" or os.getuid() == 0:
        raise BoundaryError("containment requires Linux with a non-root user systemd manager")
    if not (runtime / "bus").is_socket() or runtime.stat().st_uid != os.getuid():
        raise BoundaryError(f"user systemd bus is unavailable at {runtime / 'bus'}")
    for name in ("systemd-run", "systemctl", "busctl", "bwrap"):
        if not os.access("/usr/bin/" + name, os.X_OK):
            raise BoundaryError(f"containment requires /usr/bin/{name}")
    return {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "XDG_RUNTIME_DIR": str(runtime),
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime / 'bus'}"}


@contextmanager
def admission_lock():
    env = manager_env()
    path = Path(env["XDG_RUNTIME_DIR"]) / "lumina-test.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BoundaryError("another lumina-test command holds workload admission") from exc
        yield env
    finally:
        os.close(fd)


def properties(env, unit, *keys):
    query = subprocess.run(
        ["/usr/bin/systemctl", "--user", "show", *(f"-p{key}" for key in keys), unit],
        env=env, capture_output=True, text=True, timeout=3,
    )
    values = dict(line.split("=", 1) for line in query.stdout.splitlines())
    if set(values) != set(keys) or (query.returncode and values.get("LoadState") != "not-found"):
        raise BoundaryError(f"systemd state unavailable for {unit}: {query.stderr.strip()}")
    return values


def service_runtime(env, name):
    service = "org.freedesktop.systemd1"
    base = ["/usr/bin/busctl", "--user", "--json=short"]

    def query(*args):
        raw = subprocess.check_output([*base, *args], env=env, timeout=3)
        return json.loads(raw)["data"]

    [path] = query("call", service, "/org/freedesktop/systemd1", service + ".Manager", "GetUnit", "s", name)
    return query("get-property", service, path, service + ".Service", "RuntimeMaxUSec")


def kernel_limits(path, memory):
    expected = {"memory.max": str(memory), "memory.swap.max": "0", "memory.oom.group": "1",
                "pids.max": "64"}
    actual = {name: (path / name).read_text().strip() for name in expected}
    cpu = (path / "cpu.max").read_text().split()
    if actual != expected or len(cpu) != 2 or 2 * int(cpu[0]) != int(cpu[1]):
        raise BoundaryError(f"kernel limits differ: {actual}, cpu.max={cpu}")
    return {**actual, "cpu.max": " ".join(cpu)}


def kernel_sample(path):
    counters = dict(line.split() for line in (path / "memory.events").read_text().splitlines())
    peak = int((path / "memory.peak").read_text())
    swap = int((path / "memory.swap.current").read_text())
    members = [int(pid) for pid in (path / "cgroup.procs").read_text().splitlines()]
    return {"peak_bytes": peak, "oom": int(counters["oom"]), "oom_kill": int(counters["oom_kill"]),
            "swap_bytes": swap, "member_pids": members}


def runtime_settings():
    mounts = set()
    for prefix in {Path(sys.prefix), Path(sys.base_prefix), Path(sys.executable).resolve().parent.parent}:
        if prefix != Path("/usr"):
            mounts.update(str(prefix / name) for name in ("bin", "lib", "lib64", "pyvenv.cfg")
                          if (prefix / name).exists())
    node = shutil.which("node")
    node_bin = Path(node).resolve() if node else None
    if node_bin and not node_bin.is_relative_to("/usr"):
        mounts.add(str(node_bin))
    paths = [str(Path(sys.executable).parent), "/usr/bin", "/bin"]
    if node_bin:
        paths.insert(0, str(node_bin.parent))
    tz = os.environ.get("TZ", "")
    if tz:
        zoneinfo = Path("/usr/share/zoneinfo")
        zone = zoneinfo / tz
        if not zone.resolve().is_relative_to(zoneinfo) or not zone.is_file():
            raise BoundaryError("TZ must name an installed IANA time zone")
    return {"runtime_mounts": sorted(mounts), "python": sys.executable,
            "path": ":".join(dict.fromkeys(paths)), "tz": tz or ":/etc/localtime"}


@contextmanager
def defer_interrupts():
    handlers = {number: signal.signal(number, signal.SIG_IGN) for number in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for number, handler in handlers.items():
            signal.signal(number, handler)


def cleanup(env, token, cgroup, process):
    if process.stdin is not None and not process.stdin.closed:
        process.stdin.close()
    keys = ("LoadState", "Description", "ControlGroup", "ActiveState", "Result", "ExecMainCode", "ExecMainStatus")
    state = properties(env, UNIT, *keys)
    if state["Description"] == token and state["ActiveState"] not in ("inactive", "failed"):
        subprocess.run(["/usr/bin/systemctl", "--user", "stop", "--no-block", UNIT],
                       env=env, capture_output=True, timeout=3)
    end = time.monotonic() + 7
    while time.monotonic() < end:
        state = properties(env, UNIT, *keys)
        if (state["ActiveState"] in ("inactive", "failed") and not cgroup.exists()
                and process.poll() is not None):
            if state["Description"] == token and state["ActiveState"] == "failed":
                subprocess.run(["/usr/bin/systemctl", "--user", "reset-failed", UNIT],
                               env=env, capture_output=True, timeout=3, check=True)
            return {"verified_empty": True, "manager": state, "systemd_run_exit": process.returncode}
        time.sleep(0.05)
    process.kill()
    process.wait(timeout=2)
    return {"verified_empty": False, "manager": state, "cgroup_exists": cgroup.exists()}


def run(env, scratch, settings, log, receipt, *, timeout=180, memory=MEMORY):
    if not 1 <= timeout <= 600 or not 64 * 1024**2 <= memory <= MEMORY:
        raise BoundaryError("workload limits exceed the supported range")
    previous = (time.monotonic(), swap_totals())
    time.sleep(3)
    previous, receipt["admission"] = host_sample(previous, before_launch=True, memory=memory)
    supervisor = scratch / "supervisor"
    supervisor.mkdir()
    atomic_json(supervisor / "settings.json", settings)
    app_slice = properties(env, "app.slice", "ControlGroup")["ControlGroup"]
    if not app_slice.startswith("/") or Path(app_slice).name != "app.slice":
        raise BoundaryError("cannot locate the user application slice")
    group_name = app_slice + "/" + UNIT
    group = CGROUP_ROOT / group_name.lstrip("/")
    state = properties(env, UNIT, "LoadState", "Description", "ActiveState", "Transient")
    if group.exists() or state["ActiveState"] not in ("inactive", "failed"):
        raise BoundaryError("another service already owns the workload boundary")
    if state["ActiveState"] == "failed":
        if state["Transient"] != "yes" or not re.fullmatch(r"lumina-test [0-9a-f]{32}", state["Description"]):
            raise BoundaryError("failed workload unit could not be attributed to lumina-test")
        subprocess.run(["/usr/bin/systemctl", "--user", "reset-failed", UNIT],
                       env=env, capture_output=True, timeout=3, check=True)
        receipt["prior_failed_unit"] = state["Description"]
    token = "lumina-test " + uuid.uuid4().hex
    limits = {"MemoryMax": memory, "MemorySwapMax": 0, "OOMPolicy": "kill",
              "TasksMax": 64, "CPUQuota": "50%", "KillMode": "control-group",
              "RuntimeMaxSec": timeout, "TimeoutStopSec": 3, "SendSIGKILL": "yes",
              "NoNewPrivileges": "yes", "UMask": "0077", "LimitCORE": 0, "LimitFSIZE": "64M"}
    command = ["/usr/bin/systemd-run", "--user", "--wait", "--pipe", "--quiet",
               "--unit", UNIT, "--slice", "app.slice", "--description", token]
    for name, value in limits.items():
        command.extend(("-p", f"{name}={value}"))
    command.extend(("--", "/usr/bin/env", "-i", sys.executable, "-I", "-S", "-B",
                    str(Path(settings["controls"]) / "boundary_child.py"), str(scratch)))
    receipt.update(unit=UNIT, token=token, cgroup=group_name, timeout_seconds=timeout)
    started = time.monotonic()
    process = None
    try:
        with log.open("xb") as stream, defer_interrupts():
            process = subprocess.Popen(command, env=env, stdin=subprocess.PIPE, stdout=stream,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        ready = supervisor / "ready.json"
        end = started + min(timeout, 15)
        while not ready.exists() and process.poll() is None and time.monotonic() < end:
            time.sleep(0.025)
        if not ready.exists():
            raise BoundaryError("service did not start its inert bootstrap; see native log")
        pid = json.loads(ready.read_text())["pid"]
        state = properties(env, UNIT, "Description", "ControlGroup", "ActiveState", "MainPID",
                           "KillMode", "OOMPolicy", "SendSIGKILL", "NoNewPrivileges", "InvocationID")
        expected = {"Description": token, "ControlGroup": group_name, "ActiveState": "active",
                    "MainPID": str(pid), "KillMode": "control-group", "OOMPolicy": "kill",
                    "SendSIGKILL": "yes", "NoNewPrivileges": "yes"}
        if (any(state[key] != value for key, value in expected.items()) or not state["InvocationID"]
                or Path(f"/proc/{pid}/cgroup").read_text().strip() != "0::" + group_name
                or service_runtime(env, UNIT) != timeout * 1_000_000):
            raise BoundaryError("service identity or manager limits differ")
        receipt["manager"] = state
        receipt["kernel_limits"] = kernel_limits(group, memory)
        receipt["kernel"] = kernel_sample(group)
        process.stdin.write(b"R")
        process.stdin.flush()
        exit_file = supervisor / "exit.json"
        while not exit_file.exists():
            if process.poll() is not None:
                raise BoundaryError(f"service ended before native pytest status (manager exit {process.returncode})")
            now = time.monotonic()
            if now >= started + timeout:
                raise BoundaryError("workload wall timeout")
            if now - previous[0] >= 1:
                previous, sample = host_sample(previous)
                receipt.setdefault("in_flight", []).append(sample)
                receipt["kernel"] = kernel_sample(group)
                if receipt["kernel"]["oom"] or receipt["kernel"]["oom_kill"]:
                    raise BoundaryError("workload hit cgroup memory limit")
            time.sleep(0.04)
        native = json.loads(exit_file.read_text())["native_exit"]
        receipt["native_exit"] = native
        sample = kernel_sample(group)
        receipt["kernel"] = sample
        if (sample["oom"] or sample["oom_kill"] or sample["swap_bytes"]
                or sample["peak_bytes"] > memory or sample["member_pids"] != [pid]):
            raise BoundaryError("workload limits or descendant cleanup failed")
        process.stdin.write(b"A")
        process.stdin.flush()
        manager_exit = process.wait(timeout=5)
        if type(native) is not int or native < 0 or manager_exit != native:
            raise BoundaryError(f"native/manager status mismatch: {native}, {manager_exit}")
    finally:
        with defer_interrupts():
            if process is not None:
                if "native_exit" not in receipt:
                    receipt["manager_exit_before_cleanup"] = process.poll()
                    try:
                        receipt["kernel"] = kernel_sample(group)
                    except OSError:
                        pass
                receipt["cleanup"] = cleanup(env, token, group, process)
                if not receipt["cleanup"]["verified_empty"]:
                    raise BoundaryError("service or its cgroup remains after teardown")
                manager_result = receipt["cleanup"]["manager"]["Result"]
                if receipt.get("native_exit") == 0 and manager_result not in ("", "success"):
                    raise BoundaryError(f"manager reported {manager_result} after a native-zero result")
            receipt["duration_seconds"] = round(time.monotonic() - started, 3)
    return native
