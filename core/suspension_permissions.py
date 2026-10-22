"""Explicit desktop repair of a single memory directory, never its contents.

No C1 admission rules live here. A preview is only a filesystem snapshot;
repair requires local owner ingress and revalidates it through directory FDs.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import stat

from core import suspension as S
from core.test_isolation import refuse_if_production_path


class PermissionRepairRefused(S.SuspensionError):
    pass


@dataclass(frozen=True)
class Preview:
    path: str
    chain: tuple
    uid: int
    mode: int

    @property
    def proposed_mode(self):
        return self.mode & ~0o022


@contextmanager
def _directory(path):
    # Do not resolve links or normalize '..' before walking. Each component
    # is opened relative to its already-open parent without following links.
    if (os.name != "posix" or not hasattr(os, "O_NOFOLLOW")
            or not hasattr(os, "O_DIRECTORY") or not hasattr(os, "fchmod")
            or os.open not in os.supports_dir_fd):
        raise PermissionRepairRefused("Safe folder repair is unavailable on this system. Ask for help.")
    path = os.fspath(path)
    if not os.path.isabs(path):
        path = os.path.join(os.getcwd(), path)
    refuse_if_production_path(path)
    parts = Path(path).parts
    if ".." in parts:
        raise PermissionRepairRefused("The folder path contains '..'; stop and review the launch path.")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open("/", flags)
    try:
        st = os.fstat(fd)
        chain = [(st.st_dev, st.st_ino)]
        for part in parts[1:]:
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            st = os.fstat(fd)
            chain.append((st.st_dev, st.st_ino))
        yield fd, str(Path(path)), tuple(chain)
    finally:
        os.close(fd)


def _snapshot(fd, path, chain):
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise PermissionRepairRefused("This must be an ordinary folder owned by your OS user. Stop and ask its owner for help.")
    return Preview(path, chain, st.st_uid, stat.S_IMODE(st.st_mode))


def inspect(path) -> Preview:
    with _directory(path) as (fd, path, chain):
        return _snapshot(fd, path, chain)


def repair(preview: Preview, *, ingress) -> None:
    S._require_local_owner(ingress)
    with _directory(preview.path) as (fd, path, chain):
        if _snapshot(fd, path, chain) != preview or not preview.mode & 0o022:
            raise PermissionRepairRefused("The folder identity, ownership or permissions changed. Stop and review it again.")
        # Rewalk the current name too: a parent may have moved after open.
        if inspect(path) != preview or _snapshot(fd, path, chain) != preview:
            raise PermissionRepairRefused("The folder changed before repair. Stop and review it again.")
        os.fchmod(fd, preview.proposed_mode)
        expected = Preview(path, chain, preview.uid, preview.proposed_mode)
        if _snapshot(fd, path, chain) != expected or inspect(path) != expected:
            raise PermissionRepairRefused("Could not verify the repaired folder. Permissions may have changed; stop and ask for help.")
