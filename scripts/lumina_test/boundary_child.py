"""Trusted service bootstrap and in-namespace entry point."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

NAMESPACES = ("user", "pid", "mnt", "net", "ipc", "uts")
SCREENSHOTS = "/work/memory/browser_screenshots"
PASSWD = "/etc/passwd"
EVAL_ROOT = "/work/eval"
EVAL_FILES = ("run_eval.py", "score_eval.py", "scratch_dir.py", "tasks.json")
EVAL_TMPFS_BYTES = 16 * 1024**2


def private_passwd():
    return f"lumina-test:x:{os.getuid()}:{os.getgid()}:Lumina test user:/h:/bin/bash\n"


def namespace_ids():
    return {name: os.readlink("/proc/self/ns/" + name) for name in NAMESPACES}


def atomic_json(path, value):
    temp = path.with_suffix(".tmp")
    with temp.open("x", encoding="utf-8") as stream:
        json.dump(value, stream)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def verify_namespace(expected):
    namespaces = namespace_ids()
    mounts = {}
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        left, right = line.split(" - ", 1)
        fields = left.split()
        mounts[fields[4]] = (fields[5].split(","), right.split()[0])
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines())
    capabilities = [int(status[name].strip(), 16) for name in
                    ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")]
    unchanged = [name for name in namespaces if namespaces[name] == expected["namespaces"][name]]
    if unchanged or os.readlink("/proc/1/ns/pid") != namespaces["pid"]:
        raise RuntimeError(f"namespaces or scoped procfs differ: unchanged={unchanged}")
    if Path("/proc/self/cgroup").read_text().strip() != expected["cgroup"]:
        raise RuntimeError("workload cgroup differs inside the namespace")
    if os.geteuid() == 0 or any(capabilities) or status["NoNewPrivs"].strip() != "1":
        raise RuntimeError("namespace retained privileges")
    if Path.cwd() != Path("/work") or Path.home() != Path("/h"):
        raise RuntimeError("private workspace or HOME differs")
    for path in ("/", "/work", "/controls", "/sys/fs/cgroup", PASSWD):
        if "ro" not in mounts[path][0]:
            raise RuntimeError(f"required mount is writable: {path}")
    if Path(PASSWD).read_text(encoding="ascii") != private_passwd():
        raise RuntimeError("private workload identity differs")
    private_mounts = ("/h", "/tmp", "/dev/shm", SCREENSHOTS)
    eval_mounts = ()
    if expected.get("eval_scratch"):
        private_mounts += (EVAL_ROOT,)
        eval_mounts = (EVAL_ROOT, *(f"{EVAL_ROOT}/{name}" for name in EVAL_FILES))
        stats = os.statvfs(EVAL_ROOT)
        if (stats.f_blocks * stats.f_frsize != EVAL_TMPFS_BYTES
                or (Path(EVAL_ROOT).stat().st_mode & 0o777) != 0o700
                or {p.name for p in Path(EVAL_ROOT).iterdir()} != set(EVAL_FILES)
                or any("ro" not in mounts[path][0] for path in eval_mounts[1:])):
            raise RuntimeError("private eval scratch or read-only eval files differ")
    for path in private_mounts:
        flags, kind = mounts[path]
        if kind != "tmpfs" or not {"rw", "nodev", "nosuid"} <= set(flags):
            raise RuntimeError(f"missing private scratch mount: {path}")
    stats = os.statvfs(SCREENSHOTS)
    if (stats.f_blocks * stats.f_frsize != 4 * 1024 * 1024
            or mounts["/proc"][1] != "proc"
            or mounts["/sys/fs/cgroup"][1] != "cgroup2"
            or "rw" not in mounts["/out"][0]
            or Path(f"/run/user/{os.getuid()}/bus").exists()):
        raise RuntimeError("mount size, type or manager isolation differs")
    return {"namespaces": namespaces, "cgroup": expected["cgroup"], "mounts": {
        key: mounts[key] for key in ("/", "/work", "/controls", "/sys/fs/cgroup",
                                    "/tmp", "/h", "/proc", "/out", SCREENSHOTS, PASSWD, *eval_mounts)
    }}


def inside(config):
    proof = verify_namespace(config)
    Path("/h/runtime").mkdir(mode=0o700)
    atomic_json(Path("/out/namespace.json"), proof)
    os.execv(sys.executable, [sys.executable, "-I", "-B", "/controls/pytest_entry.py"])


def sandbox(settings):
    cgroup = Path("/proc/self/cgroup").read_text().strip()
    if not cgroup.startswith("0::/") or "\n" in cgroup:
        raise RuntimeError("unified cgroup is unavailable")
    host_cgroup = "/sys/fs/cgroup" + cgroup[3:]
    args = ["/usr/bin/bwrap", "--unshare-user", "--unshare-pid", "--unshare-net",
            "--unshare-ipc", "--unshare-uts", "--disable-userns", "--die-with-parent", "--cap-drop", "ALL", "--clearenv"]
    for path in ("/usr", "/etc/ld.so.cache", "/etc/localtime", "/etc/alternatives", "/etc/fonts",
                 *settings["runtime_mounts"]):
        if Path(path).exists():
            args.extend(("--ro-bind", path, path))
    for target in ("bin", "sbin", "lib", "lib64"):
        args.extend(("--symlink", f"usr/{target}", "/" + target))
    args.extend(("--proc", "/proc", "--dev", "/dev", "--tmpfs", "/dev/shm",
                 "--perms", "1777", "--size", str(512 * 1024**2), "--tmpfs", "/tmp",
                 "--perms", "0700", "--tmpfs", "/h",
                 "--ro-bind", settings["project"], "/work",
                 "--perms", "0700", "--size", str(4 * 1024**2), "--tmpfs", SCREENSHOTS,
                 "--ro-bind", settings["controls"], "/controls",
                 "--ro-bind", str(Path(settings["controls"]) / "passwd"), PASSWD,
                 "--ro-bind", host_cgroup, "/sys/fs/cgroup",
                 "--bind", settings["output"], "/out"))
    if settings.get("eval_scratch"):
        eval_source = Path(settings["project"]) / "eval"
        if {p.name for p in eval_source.iterdir()} != set(EVAL_FILES):
            raise RuntimeError("projected eval layout differs from the read-only file list")
        # The real reset deletes and recreates _scratch_data beneath this parent.
        args.extend(("--perms", "0700", "--size", str(EVAL_TMPFS_BYTES), "--tmpfs", EVAL_ROOT))
        for name in EVAL_FILES:
            file = eval_source / name
            if file.is_symlink() or not file.is_file():
                raise RuntimeError(f"invalid projected eval file: {name}")
            args.extend(("--ro-bind", str(file), f"{EVAL_ROOT}/{name}"))
    if settings["relays"]:
        args.extend(("--ro-bind", settings["relays"], "/relays"))
    environment = {
        "HOME": "/h", "TMPDIR": "/tmp", "XDG_RUNTIME_DIR": "/h/runtime",
        "PATH": settings["path"], "LANG": "C.UTF-8", "TZ": settings["tz"],
        "QT_QPA_PLATFORM": "offscreen", "QT_ACCESSIBILITY": "0",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_OPTIONAL_LOCKS": "0", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null", "CI": "true",
    }
    if settings["require_qt"]:
        environment["LUMINA_REQUIRE_QT"] = "1"
    if settings["relays"]:
        environment.update(AIVENGERS_WIRE_SRC="/relays/merged",
                           AIVENGERS_WIRE_MERGED_SRC="/relays/merged",
                           AIVENGERS_WIRE_OLD_SRC="/relays/old",
                           AIVENGERS_WIRE_ATTENTION_SRC="/relays/attention")
    for name, value in environment.items():
        args.extend(("--setenv", name, value))
    proof = {"namespaces": namespace_ids(), "cgroup": cgroup,
             "eval_scratch": settings.get("eval_scratch", False)}
    return [*args, "--chdir", "/work", "--remount-ro", "/", "--",
            settings["python"], "-I", "-S", "-B", "/controls/boundary_child.py",
            "inside", json.dumps(proof)]


def service(root):
    control = root / "supervisor"
    settings = json.loads((control / "settings.json").read_text())
    atomic_json(control / "ready.json", {"pid": os.getpid()})
    if os.read(0, 1) != b"R":
        return 125
    acknowledged = threading.Event()

    def watch_controller():
        if os.read(0, 1) != b"A":
            # The service exits and systemd stops every remaining cgroup member.
            os._exit(125)
        acknowledged.set()

    threading.Thread(target=watch_controller, daemon=True).start()
    child = subprocess.run(sandbox(settings), stdin=subprocess.DEVNULL, close_fds=True)
    atomic_json(control / "exit.json", {"native_exit": child.returncode})
    if not acknowledged.wait(15):
        return 125
    return child.returncode if child.returncode >= 0 else 128 - child.returncode


if __name__ == "__main__":
    if sys.argv[1] == "inside":
        inside(json.loads(sys.argv[2]))
    else:
        sys.exit(service(Path(sys.argv[1])))
