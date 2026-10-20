"""TELEGRAM-OWNER-INBOUND-WAKE-01 -- one Telegram poller per bot, per OS.

Two pollers on one bot token make Telegram answer 409 Conflict and the two
then take turns receiving updates. The release and dev builds share the
token (it lives in the per-OS-user credential store) but not a data dir, so
the lock lives in the per-user runtime directory, keyed by the token's
public numeric bot id -- never the secret half.

An exclusive non-blocking flock: held for the listener thread's lifetime,
released explicitly on every exit path, and by the kernel if the process
dies. The holder's pid is written into the file so a refusal can name it.
Across Mint and Kali (separate OS instances) this lock can't see the other
side; Telegram's 409 and the bridge's Conflict yield cover that case.
"""
from __future__ import annotations

import fcntl
import os
import pathlib
import tempfile


def lock_dir() -> pathlib.Path:
    override = os.environ.get("LUMINA_RUNTIME_DIR")
    if override:
        return pathlib.Path(override)
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return pathlib.Path(runtime) / "lumina"
    return pathlib.Path(tempfile.gettempdir()) / f"lumina-{os.getuid()}"


def lock_path(bot_id: str) -> pathlib.Path:
    return lock_dir() / f"telegram-poller-{bot_id}.lock"


class PollerLock:
    def __init__(self, fd: int, path: pathlib.Path):
        self._fd = fd
        self.path = path

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


def acquire(bot_id: str) -> tuple[PollerLock | None, str | None]:
    """(lock, None) on success; (None, holder pid text or "") when another
    poller holds it. OSError (unwritable runtime dir) propagates."""
    directory = lock_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    path = lock_path(bot_id)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            holder = os.pread(fd, 32, 0).decode("ascii", "ignore").strip()
        except OSError:
            holder = ""
        os.close(fd)
        return None, holder
    except BaseException:
        os.close(fd)
        raise
    os.ftruncate(fd, 0)
    os.pwrite(fd, str(os.getpid()).encode("ascii"), 0)
    return PollerLock(fd, path), None
