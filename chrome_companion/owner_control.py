"""Same-UID owner CLI notice that retires a live Companion connection."""
from __future__ import annotations

import errno
import socket

from chrome_companion import protocol


def retire_live_connection(socket_path: str | None, reason: str) -> None:
    """Wait for the hub's retirement acknowledgement if a hub is listening.

    Missing/stale sockets mean there is no live hub to retire. A connected hub
    that does not acknowledge is an error; the CLI must not claim success.
    """
    if not socket_path or reason not in {"unpaired", "uninstalled"}:
        return
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(2.0)
        try:
            sock.connect(socket_path)
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ECONNREFUSED}:
                return
            raise
        sock.sendall(protocol.encode_frame({"v": protocol.PROTOCOL_VERSION,
            "type": "owner_revoke", "reason": reason}, protocol.MAX_FROM_EXTENSION_BYTES))
        try:
            reply = protocol.read_frame(sock.recv, protocol.MAX_TO_EXTENSION_BYTES)
        except protocol.ProtocolError as exc:
            raise OSError("Chrome Companion hub sent an invalid retirement acknowledgement") from exc
        if reply != {"v": protocol.PROTOCOL_VERSION, "type": "owner_revoke_ack", "ok": True}:
            raise OSError("Chrome Companion hub did not acknowledge connection retirement")
    finally:
        sock.close()
