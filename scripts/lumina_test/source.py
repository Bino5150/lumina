"""Project only source files and private Git metadata into the test namespace."""
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import tarfile

RELAY_REVISIONS = {
    "merged": ("9dff48fc4916f6764fa3ce664e58a0257031c212", "6278d5e2489bd20dbbe353e31129db2521c26905"),
    "old": ("1ac89dfd990053c8f60f9489dd88df88df921345", "27b0520a258dc884ce2235fc86eeaf94f5ee2701"),
    # WIRE-AUTONOMY-01: the relay that creates attention events (the "second ping").
    # Lumina's autonomous-conversation tests run against it. See the exception below.
    "attention": ("08b06feedf2cf8968837cfa70d3a986ca65096a3", "830c77850c5eaa4f8268e6c015f37c8a28955e82"),
}
#: Pins that are honest but not yet reproducible elsewhere. Every entry is printed
#: in each Wire-profile receipt until it is deleted.
TEMPORARY_DEPENDENCY_EXCEPTIONS = {
    "attention": (
        "pinned to an AIvengers Wire commit that exists only in the local Wire checkout; it is "
        "not on GitHub until the owner publishes it, so CI and other machines cannot stage this "
        "generation. Resolve by publishing the commit (a merge commit keeps its SHA; if it is "
        "squashed, repin to the new commit, whose src tree must equal the pinned tree), then "
        "delete this entry."
    ),
}
SCREENSHOTS = "memory/browser_screenshots"
NEW_SOURCE_EXTENSIONS = {".py", ".ini", ".cfg", ".toml", ".js", ".mjs", ".cjs", ".ts", ".css", ".html", ".ui"}
PRIVATE_ROOTS = {"memory", "projects", "reports", "project-evidence", "personas", "skills"}
GIT_ENV = {
    "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_OPTIONAL_LOCKS": "0",
    "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "file",
}


def git(repo, *args, binary=False):
    output = subprocess.check_output(
        ["/usr/bin/git", "-c", "core.fsmonitor=false", "-C", str(repo), *args], env=GIT_ENV, timeout=45,
        stderr=subprocess.PIPE,
    )
    return output if binary else output.decode().strip()


def snapshot(repo, dest=None):
    """Include current tracked bytes and new visible source, even before staging."""
    head = git(repo, "rev-parse", "HEAD")
    tree = git(repo, "rev-parse", head + "^{tree}")
    tracked = git(repo, "ls-files", "-z", "--cached", binary=True)
    untracked = git(repo, "ls-files", "-z", "--others", "--exclude-standard", binary=True)
    additions = set()
    for name in untracked.split(b"\0"):
        if not name:
            continue
        path = Path(os.fsdecode(name))
        if path.parts[0] in PRIVATE_ROOTS or any(part.startswith(".") for part in path.parts):
            continue
        if (path.suffix in NEW_SOURCE_EXTENSIONS or path == Path("scripts/lumina-test")
                or (path.parent == Path("scripts/lumina_test") and path.name in {"inventory.tsv", "profiles.json"})):
            additions.add(name)
    files = sorted((set(tracked.split(b"\0")) | additions) - {b""})
    digest = hashlib.sha256()
    count = 0
    for name in files:
        relative = Path(os.fsdecode(name))
        if (relative.is_absolute() or ".." in relative.parts
                or relative.parts[0] in {".git", "memory", "reports"}):
            raise ValueError(f"unsafe source path: {relative}")
        path = repo / relative
        if path.resolve() != path:
            raise ValueError(f"symlink in source projection: {relative}")
        try:
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            continue
        limit = 64 * 1024**2
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ValueError(f"source projection needs a regular file within 64 MiB: {relative}")
            data = stream.read(limit + 1)
            if len(data) > limit:
                raise ValueError(f"source projection file grew beyond 64 MiB: {relative}")
        executable = bool(info.st_mode & stat.S_IXUSR)
        digest.update(name + b"\0" + bytes([executable]) + hashlib.sha256(data).digest())
        if dest is not None:
            target = dest / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(data)
            target.chmod(0o755 if executable else 0o644)
        count += 1
    return {"head": head, "head_tree": tree, "source_sha256": digest.hexdigest(), "files": count}


def project(repo, dest):
    dest.mkdir(mode=0o700)
    identity = snapshot(repo, dest)
    git(dest, "init", "-q", "--template=")
    git(dest, "-c", "pack.threads=1", "fetch", "-q", "--no-tags", "--no-write-fetch-head",
        "--no-recurse-submodules", "--", str(repo), identity["head"])
    git(dest, "update-ref", "--no-deref", "HEAD", identity["head"])
    git(dest, "read-tree", identity["head"])
    (dest / SCREENSHOTS).mkdir(parents=True, mode=0o700)
    return identity


def stage_relays(repo, dest):
    for generation, (commit, tree) in RELAY_REVISIONS.items():
        spec = commit + ":src/aivengers_wire"
        if git(repo, "rev-parse", spec) != tree:
            raise ValueError(f"{generation} Wire source does not match pinned tree {tree}")
        root = dest / generation
        root.mkdir(parents=True, mode=0o700)
        archive = root / "source.tar"
        with archive.open("xb") as stream:
            subprocess.run(
                ["/usr/bin/git", "-C", str(repo), "archive", "--prefix=aivengers_wire/", tree],
                env=GIT_ENV, stdout=stream, timeout=15, check=True,
            )
        with tarfile.open(archive) as stream:
            members = stream.getmembers()
            if any(not (member.isfile() or member.isdir()) for member in members):
                raise ValueError(f"{generation} Wire archive contains a non-source entry")
            stream.extractall(root, members=members, filter="data")
        archive.unlink()
    return RELAY_REVISIONS
