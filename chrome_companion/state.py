"""Chrome Companion local state: install record, profile pairing, socket path.

Stdlib-only; every function takes ``data_dir`` explicitly (Lumina passes
config.DATA_DIR; the installer CLI passes its required --data-dir), so this
module never imports config.

Layout (all owner-only; directories 0700, files 0600):

    <data_dir>/chrome_companion/install.json   extension origin + socket path
    <data_dir>/chrome_companion/pairing.json   the ONE enrolled extension instance
    <data_dir>/chrome_companion/native_host/   generated host config + launcher

Pairing exists because Native Messaging identifies an extension ID, not a
Chrome profile: the same unpacked extension loaded into a second profile has
the same ID. Each installed extension instance therefore generates a random
128-bit instance_id on first run (kept in chrome.storage.local), shows it in
its popup, and the owner enrolls exactly that instance with
``scripts/chrome_companion_setup.py pair <id>``. Pairing is never inferred from a
Google/Reddit/GitHub account, a tab title, a profile folder name, or an
email address. No credentials, cookies, or site tokens are stored here.

THE INSTALLER DOES NOT TRUST PATHNAMES (BROWSER-COMPANION-01A-R3 / B3-R3).
It trusts the directory objects it actually opened and verified, and it keeps
them open: every directory it writes into, reads from, or removes from is
reached by open_trusted_dir() -- a walk from "/" that opens ONE component at a
time, relative to the descriptor of the directory before it, with
O_DIRECTORY|O_NOFOLLOW, and checks each object it opened with fstat() -- and
every write then goes through that same descriptor (write_file_at): a fresh,
randomly named temp created with O_EXCL|O_NOFOLLOW inside it, chmod-ed and
fsync-ed through its own descriptor, renamed over the target within the same
directory descriptor, and re-verified. Nothing is validated as a path string
and reopened later, so replacing any component of the path afterwards --
first ancestor, middle, or the final directory itself -- cannot move a write.

Accepted chain (_check_entry): every directory, and every symlink, the walk
passes through must be owned by root or this user; the directory it sits in
must be writable by nobody else -- group write only through a proven
user-private group (_private_group) -- unless that directory is sticky and
owned by root or this user (like /tmp), where nobody else can replace an
entry root or this user owns. A symlink is followed only under that rule
(the walk reads it with readlinkat and continues from the objects it opens,
up to 40 links); the final component of a directory written into is never a
link (a relocated data dir may be; the directories inside it may not).
The final directory must be owned by this user and writable by nobody else,
or, if private, accessible by nobody else. A hostile chain is refused, never
repaired.

THE WALK SEES THE PATH AS GIVEN (BROWSER-COMPANION-01A-R4 / AR4). Nothing
canonicalizes a data dir before the walk: resolve()/realpath() would replace
a link, and the directory holding it, with the target they pointed at in that
instant -- the walk would then vet only the target's ancestry and never see
the replaceable link. A relative path is only made absolute against the
working directory (absolute_path), with ".." left for the walk to take
physically, exactly as the kernel will when the recorded paths are used.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from chrome_companion import protocol

COMPANION_DIRNAME = "chrome_companion"
INSTALL_FILENAME = "install.json"
PAIRING_FILENAME = "pairing.json"
STATE_VERSION = 1
# Linux sun_path is 108 bytes including the terminating NUL.
MAX_SOCKET_PATH_BYTES = 107
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_OPEN_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _CLOEXEC
_NEW_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _CLOEXEC
_TEMP_ATTEMPTS = 16


class StateError(Exception):
    """Local companion state is missing, malformed, or insecurely stored."""


@dataclass(frozen=True)
class InstallRecord:
    extension_origin: str
    socket_path: str
    installed_at: float


@dataclass(frozen=True)
class PairingRecord:
    instance_id: str
    extension_origin: str
    paired_at: float


def companion_dir(data_dir) -> Path:
    return Path(data_dir) / COMPANION_DIRNAME


def absolute_path(path) -> Path:
    """``path`` made absolute against the working directory -- and nothing
    else: no link is resolved and no ".." is folded (module docstring, R4).
    The walk takes the result exactly as the kernel would."""
    text = os.fspath(path)
    if not os.path.isabs(text):
        text = os.path.join(os.getcwd(), text)
    return Path(text)


def ensure_private_dir(path) -> Path:
    """Create (or verify) an owner-only directory. Refuses a directory owned
    by another user or reachable by group/world -- fail closed rather than
    silently tightening something we did not create."""
    path = Path(path)
    # Create every missing level ourselves at 0700: Path.mkdir(parents=True)
    # would create intermediate parents with the umask default (0775 under a
    # common 002 umask), which the check below then rightly refuses.
    missing = []
    probe = path
    while not probe.exists():
        missing.append(probe)
        probe = probe.parent
    for level in reversed(missing):
        try:
            os.mkdir(level, 0o700)
        except FileExistsError:
            continue
        os.chmod(level, 0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise StateError(f"not a directory: {path}")
    if info.st_uid != os.getuid():
        raise StateError(f"directory not owned by current user: {path}")
    if info.st_mode & 0o077:
        raise StateError(f"directory is group/world accessible: {path}")
    return path


def _private_group(gid: int) -> bool:
    """True only if this user is PROVABLY the one and only member of group
    ``gid`` -- a user-private group (Debian/Ubuntu USERGROUPS_ENAB, where the
    default umask is 002 and a user's directories are routinely 0775; Bino's
    own data dir is 0775). Group write is then write by this user alone. The
    same rule as Debian OpenSSH's user-group-modes, including its refusal of a
    group with NO members: such groups exist for setgid programs, whose
    processes -- run by anyone -- hold the group (R3 review of the R2 rule,
    which accepted them). Members are counted from both the passwd primary
    gids and the group's member list; every one must be this user."""
    import grp
    import pwd
    uid = os.getuid()
    try:
        group = grp.getgrgid(gid)
        me = pwd.getpwuid(uid).pw_name
    except KeyError:
        return False
    members = 0
    for entry in pwd.getpwall():
        if entry.pw_gid == gid:
            if entry.pw_uid != uid:
                return False
            members += 1
    for name in group.gr_mem:
        if name != me:
            return False
        members += 1
    return members > 0


def _others_can_write(info: os.stat_result) -> bool:
    return bool(info.st_mode & 0o002) or bool(info.st_mode & 0o020 and not _private_group(info.st_gid))


class _Missing(StateError):
    """A component of the directory does not exist (and was not to be created)."""


def _check_parent(parent: os.stat_result, path) -> None:
    """Nobody but root or this user may replace an entry of ``parent`` owned by
    root or this user: it is writable by nobody else, or it is sticky and
    itself owned by root or this user."""
    if _others_can_write(parent) and not (parent.st_mode & stat.S_ISVTX and parent.st_uid in (0, os.getuid())):
        raise StateError(f"refusing {path}: a directory on its path is writable by other users")


def _check_entry(parent: os.stat_result, entry: os.stat_result, path) -> None:
    """The accepted-chain rule for one step of the walk: ``entry`` (a directory
    or symlink the walk passes through, as fstat/lstat saw the object itself)
    sits in ``parent``; nobody but root or this user may be able to replace it."""
    if entry.st_uid not in (0, os.getuid()):
        raise StateError(f"refusing {path}: a directory on its path is owned by another user")
    _check_parent(parent, path)


_MAX_LINKS = 40


def _walk(path, *, create: bool, follow_final: bool = False) -> tuple[int, os.stat_result]:
    """Open directory ``path`` by walking it from "/" (see the module
    docstring). Returns the descriptor of the final directory and its fstat.
    Missing levels are created at 0700 only if ``create``. The final
    component may itself be a (trusted) link only if ``follow_final``."""
    text = os.fspath(path)
    if not os.path.isabs(text):
        text = os.path.join(os.getcwd(), text)
    pending = [part for part in text.split("/") if part not in ("", ".")]
    pending.reverse()  # a stack: the next component is pending[-1]
    root = os.open("/", _OPEN_DIR_FLAGS)
    chain = [(root, os.fstat(root))]  # the physical directories from "/" down
    links = 0
    try:
        _check_entry(chain[0][1], chain[0][1], path)
        while pending:
            name = pending.pop()
            if name == "..":
                if len(chain) > 1:
                    os.close(chain.pop()[0])
                continue
            parent_fd, parent = chain[-1]
            final = not pending
            try:
                fd = os.open(name, _OPEN_DIR_FLAGS, dir_fd=parent_fd)
            except FileNotFoundError:
                if not create:
                    raise _Missing(f"{path} does not exist") from None
                _check_parent(parent, path)  # never create inside a directory others control
                try:
                    os.mkdir(name, 0o700, dir_fd=parent_fd)
                except FileExistsError:
                    pass
                else:  # ours: nobody else can replace it (see _check_entry), so exact mode by name is safe
                    os.chmod(name, 0o700, dir_fd=parent_fd)
                pending.append(name)  # open (and check) what is there now
                continue
            except OSError as exc:
                try:
                    link = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except OSError:
                    raise StateError(f"refusing {path}: cannot open it ({exc.strerror})") from exc
                if stat.S_ISDIR(link.st_mode):
                    raise StateError(f"refusing {path}: cannot open it ({exc.strerror})") from exc
                if not stat.S_ISLNK(link.st_mode):
                    raise StateError(f"refusing {path}: not a directory") from exc
                if final and not follow_final:
                    raise StateError(f"refusing to use {path}: not a directory reachable without "
                                     "following a link") from exc
                _check_entry(parent, link, path)
                links += 1
                if links > _MAX_LINKS:
                    raise StateError(f"refusing {path}: too many symbolic links") from exc
                target = os.readlink(name, dir_fd=parent_fd)
                if target.startswith("/"):
                    while len(chain) > 1:
                        os.close(chain.pop()[0])
                parts = [part for part in target.split("/") if part not in ("", ".")]
                pending.extend(reversed(parts))
                continue
            info = os.fstat(fd)
            chain.append((fd, info))
            if not stat.S_ISDIR(info.st_mode):
                raise StateError(f"refusing {path}: not a directory")
            _check_entry(parent, info, path)
        fd, info = chain.pop()
        return fd, info
    except StateError:
        raise
    except OSError as exc:
        raise StateError(f"refusing {path}: {exc.strerror}") from exc
    finally:
        for fd, _ in chain:
            os.close(fd)


def _check_final(info: os.stat_result, path, private: bool) -> None:
    if info.st_uid != os.getuid():
        raise StateError(f"refusing {path}: directory not owned by current user")
    if private and info.st_mode & 0o077:
        raise StateError(f"refusing {path}: directory is group/world accessible")
    if not private and _others_can_write(info):
        raise StateError(f"refusing {path}: directory writable by other users")


def open_trusted_dir(path, *, create: bool = False, private: bool = False) -> int:
    """The ONE way this module reaches a directory: walk ``path`` from "/"
    (module docstring), then require the final directory to be owned by this
    user and writable by nobody else -- or, if ``private``, accessible by
    nobody else. Returns its descriptor; the caller owns it and must do every
    operation inside that directory THROUGH it, never by path again."""
    fd, info = _walk(path, create=create)
    try:
        _check_final(info, path, private)
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_private_subdir(parent_fd: int, name: str, *, create: bool, path) -> int:
    """One owner-only (0700) level inside an already-opened directory: never a
    link, created at 0700 if missing and ``create``. ``path`` is for messages."""
    parent = os.fstat(parent_fd)
    try:
        fd = os.open(name, _OPEN_DIR_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            raise _Missing(f"{path} does not exist") from None
        _check_parent(parent, path)
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        else:
            os.chmod(name, 0o700, dir_fd=parent_fd)
        return open_private_subdir(parent_fd, name, create=False, path=path)
    except OSError as exc:
        raise StateError(f"refusing to use {path}: not a directory reachable without "
                         f"following a link ({exc.strerror})") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise StateError(f"refusing {path}: not a directory")
        _check_entry(parent, info, path)
        _check_final(info, path, private=True)
    except BaseException:
        os.close(fd)
        raise
    return fd


def check_data_dir(data_dir) -> bool:
    """False if the data dir does not exist; StateError if its chain AS GIVEN
    -- every link on it and every directory holding one -- fails the walk's
    rule. The owner CLI's up-front check; every operation walks again."""
    try:
        fd = _walk(data_dir, create=False, follow_final=True)[0]
    except _Missing:
        return False
    os.close(fd)
    return True


def open_companion_dir(data_dir, *, create: bool) -> int:
    """<data_dir>/chrome_companion, reached by the walk and kept open. (The
    data dir itself need not be ours -- only nobody else may replace what is
    in it, which open_private_subdir checks on the data dir's own fstat.)
    ``data_dir`` must be the path as supplied, never a resolved one (R4)."""
    base = _walk(data_dir, create=create, follow_final=True)[0]  # a relocated data dir is fine
    try:
        return open_private_subdir(base, COMPANION_DIRNAME, create=create, path=companion_dir(data_dir))
    finally:
        os.close(base)


def _temp_token() -> str:
    return secrets.token_hex(8)


def write_file_at(dir_fd: int, name: str, data: bytes, *, mode: int, path) -> None:
    """Replace ``name`` inside the ALREADY-OPENED directory ``dir_fd`` with
    ``data`` at exactly ``mode``, such that the write can affect nothing but
    that one name (module docstring). An existing target must be a regular
    file this user owns -- anything else (a symlink, a directory, a FIFO,
    another user's file) is refused, never followed or overwritten. ``path``
    only names the target in messages."""
    try:
        existing = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        existing = None
    if existing is not None and (not stat.S_ISREG(existing.st_mode) or existing.st_uid != os.getuid()):
        raise StateError(f"refusing to replace {path}: not a regular file owned by the current user")
    for _ in range(_TEMP_ATTEMPTS):
        tmp = f".{name}.{_temp_token()}.tmp"
        try:
            fd = os.open(tmp, _NEW_FILE_FLAGS, 0o600, dir_fd=dir_fd)
            break
        except FileExistsError:
            continue
    else:
        raise StateError(f"cannot create a private temporary file next to {path}")
    try:
        try:
            created = os.fstat(fd)
            if not stat.S_ISREG(created.st_mode) or created.st_uid != os.getuid() or created.st_nlink != 1:
                raise StateError(f"temporary file next to {path} is not a fresh private file")
            os.fchmod(fd, mode)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except OSError:
            pass
        raise
    # Re-verify what the name now holds. (Another install by this same user
    # may have won the rename race -- only this user or root can write here --
    # so the file need not be ours, but it must be sound.)
    final = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    if not stat.S_ISREG(final.st_mode) or final.st_uid != os.getuid() or stat.S_IMODE(final.st_mode) != mode:
        raise StateError(f"{path} changed while it was being written")
    try:
        os.fsync(dir_fd)  # best effort: publish the rename durably
    except OSError:
        pass


def write_private_json_at(dir_fd: int, name: str, value: dict, *, path) -> None:
    """Atomic 0600 JSON write inside an already-opened owner-only directory."""
    text = json.dumps(value, indent=2, sort_keys=True) + "\n"
    write_file_at(dir_fd, name, text.encode("utf-8"), mode=0o600, path=path)


_MAX_STATE_BYTES = 1 << 20


def write_private_json(path, value: dict) -> None:
    """Atomic 0600 JSON write to ``path`` in an owner-only directory (created
    at 0700 if missing), anchored by the walk."""
    path = Path(path)
    dir_fd = open_trusted_dir(path.parent, create=True, private=True)
    try:
        write_private_json_at(dir_fd, path.name, value, path=path)
    finally:
        os.close(dir_fd)


def read_private_json_at(dir_fd: int, name: str, *, path) -> dict | None:
    """Read an owner-only state file inside an already-opened directory:
    opened without following a link or blocking on a FIFO, checked on its
    own descriptor, read through it."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | _CLOEXEC, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StateError(f"not a regular file: {path}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise StateError(f"not a regular file: {path}")
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise StateError(f"insecure ownership/permissions: {path}")
        chunks, size = [], 0
        while size <= _MAX_STATE_BYTES:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    try:
        if size > _MAX_STATE_BYTES:
            raise ValueError("state file too large")
        value = json.loads(b"".join(chunks).decode("utf-8"))
    except ValueError as exc:
        raise StateError(f"unreadable state file {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("version") != STATE_VERSION:
        raise StateError(f"unsupported state file {path}")
    return value


def read_private_json(path) -> dict | None:
    """Anchored read of ``path``: its directory is reached by the walk and must
    be owner-only. None if the file (or its directory) does not exist."""
    path = Path(path)
    try:
        dir_fd = open_trusted_dir(path.parent, private=True)
    except _Missing:
        return None
    try:
        return read_private_json_at(dir_fd, path.name, path=path)
    finally:
        os.close(dir_fd)


def remove_regular_at(dir_fd: int, name: str) -> bool:
    """Remove ``name`` from an already-opened directory only if it is a
    regular file this user owns; a link or anything else is left alone."""
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        return False
    try:
        os.unlink(name, dir_fd=dir_fd)
    except FileNotFoundError:
        return False
    return True


def default_socket_path(companion_fd: int, data_dir) -> str:
    """Private runtime location for the hub socket: $XDG_RUNTIME_DIR/lumina/
    (the per-user 0700 tmpfs), falling back to <data_dir>/chrome_companion/run.
    The file name is scoped by the identity of the companion directory the
    installer opened (its device and inode -- the object, not a resolved
    pathname, R4), so the release and dev builds (different LUMINA_DATA_DIRs)
    never collide. The hub and host read the result from install.json and
    host_config.json; it is never recomputed."""
    info = os.fstat(companion_fd)
    scope = hashlib.sha256(f"{info.st_dev}:{info.st_ino}".encode("utf-8")).hexdigest()[:12]
    name = f"chrome-companion-{scope}.sock"
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and Path(runtime).is_dir():
        candidate = Path(runtime) / "lumina" / name
    else:
        candidate = companion_dir(data_dir) / "run" / name
    if len(os.fsencode(str(candidate))) > MAX_SOCKET_PATH_BYTES:
        raise StateError(f"socket path too long for AF_UNIX: {candidate}")
    return str(candidate)


def normalize_instance_id(text) -> str:
    if not isinstance(text, str):
        raise StateError("instance ID must be text")
    cleaned = "".join(ch for ch in text.strip().lower() if ch not in "- ")
    if not protocol.is_hex_id(cleaned):
        raise StateError("instance ID must be 32 hex characters (dashes optional)")
    return cleaned


def format_instance_id(instance_id: str) -> str:
    return "-".join(instance_id[i:i + 4] for i in range(0, len(instance_id), 4))


def instance_fingerprint(instance_id: str) -> str:
    """Short, non-reversible label for telemetry/status -- never the raw ID."""
    return hashlib.sha256(instance_id.encode("utf-8")).hexdigest()[:12]


def _open_existing_companion_dir(data_dir) -> int | None:
    try:
        return open_companion_dir(data_dir, create=False)
    except _Missing:
        return None


def load_install(data_dir) -> InstallRecord | None:
    dir_fd = _open_existing_companion_dir(data_dir)
    if dir_fd is None:
        return None
    try:
        value = read_private_json_at(dir_fd, INSTALL_FILENAME, path=companion_dir(data_dir) / INSTALL_FILENAME)
    finally:
        os.close(dir_fd)
    if value is None:
        return None
    origin = value.get("extension_origin")
    socket_path = value.get("socket_path")
    if protocol.extension_id_from_origin(origin) is None:
        raise StateError("install.json has an invalid extension origin")
    if not isinstance(socket_path, str) or not os.path.isabs(socket_path):
        raise StateError("install.json has an invalid socket path")
    return InstallRecord(origin, socket_path, float(value.get("installed_at", 0)))


def save_install_at(dir_fd: int, data_dir, *, extension_id: str, socket_path: str) -> InstallRecord:
    """Write install.json through the already-opened companion directory."""
    origin = protocol.extension_origin(extension_id)
    if not os.path.isabs(socket_path):
        raise StateError("socket path must be absolute")
    record = InstallRecord(origin, socket_path, time.time())
    write_private_json_at(dir_fd, INSTALL_FILENAME, {
        "version": STATE_VERSION, "extension_origin": record.extension_origin,
        "socket_path": record.socket_path, "installed_at": record.installed_at,
    }, path=companion_dir(data_dir) / INSTALL_FILENAME)
    return record


def save_install(data_dir, *, extension_id: str, socket_path: str) -> InstallRecord:
    dir_fd = open_companion_dir(data_dir, create=True)
    try:
        return save_install_at(dir_fd, data_dir, extension_id=extension_id, socket_path=socket_path)
    finally:
        os.close(dir_fd)


def load_pairing(data_dir) -> PairingRecord | None:
    dir_fd = _open_existing_companion_dir(data_dir)
    if dir_fd is None:
        return None
    try:
        value = read_private_json_at(dir_fd, PAIRING_FILENAME, path=companion_dir(data_dir) / PAIRING_FILENAME)
    finally:
        os.close(dir_fd)
    if value is None:
        return None
    instance_id = value.get("instance_id")
    origin = value.get("extension_origin")
    if not protocol.is_hex_id(instance_id) or protocol.extension_id_from_origin(origin) is None:
        raise StateError("pairing.json is malformed")
    return PairingRecord(instance_id, origin, float(value.get("paired_at", 0)))


def save_pairing(data_dir, *, instance_id: str, extension_origin: str) -> PairingRecord:
    instance_id = normalize_instance_id(instance_id)
    if protocol.extension_id_from_origin(extension_origin) is None:
        raise StateError("invalid extension origin")
    record = PairingRecord(instance_id, extension_origin, time.time())
    dir_fd = open_companion_dir(data_dir, create=True)
    try:
        write_private_json_at(dir_fd, PAIRING_FILENAME, {
            "version": STATE_VERSION, "instance_id": record.instance_id,
            "extension_origin": record.extension_origin, "paired_at": record.paired_at,
        }, path=companion_dir(data_dir) / PAIRING_FILENAME)
    finally:
        os.close(dir_fd)
    return record


def clear_pairing(data_dir) -> bool:
    """Remove the enrolment. The name is unlinked inside the anchored companion
    directory; a link there is removed itself, never followed."""
    dir_fd = _open_existing_companion_dir(data_dir)
    if dir_fd is None:
        return False
    try:
        os.unlink(PAIRING_FILENAME, dir_fd=dir_fd)
        return True
    except FileNotFoundError:
        return False
    finally:
        os.close(dir_fd)
