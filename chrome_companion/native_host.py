#!/usr/bin/env python3
"""Chrome Companion native messaging host (BROWSER-COMPANION-01A).

Chrome launches one process of this per chrome.runtime.connectNative() port,
passing the calling extension's origin as its origin argument, and owns the
stdio pipes for the port's whole life. The generated launcher (see
chrome_companion/installer.py) runs:

    native_host.py --config <data_dir>/chrome_companion/native_host/host_config.json <origin>

This process is a thin, validating relay between Chrome (stdio) and Lumina's
hub (owner-only Unix socket). It holds no policy of its own beyond:

  * the Chrome-supplied origin must equal the installed extension origin
    exactly -- checked BEFORE a single byte of stdin is read;
  * the extension's hello must be well-formed and name that same extension;
  * the hub socket's peer must be this same Unix user (SO_PEERCRED);
  * every relayed frame must be size-bounded, valid UTF-8 JSON, the current
    protocol version, and a message type legal for its direction.

Any violation ends the process; Chrome then disconnects the port and the
extension starts over with fresh lifecycle state. stdout carries framed
JSON only. stderr (which Chrome forwards to its own log) carries content-free
metadata only -- never page text, URLs, titles or instance IDs.
"""
from __future__ import annotations

import os
import socket
import struct
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chrome_companion import protocol, state  # noqa: E402

HUB_CONNECT_TIMEOUT_S = 3.0


def log(message: str) -> None:
    # Chrome owns our stderr. When Chrome dies it closes that pipe too, and a
    # write would raise BrokenPipeError -- which must never decide whether
    # the relay tears down (it once killed the teardown path mid-finally and
    # left a zombie host holding the hub connection open).
    try:
        print(f"[chrome-companion-host] {message}", file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def parse_args(argv: list[str]) -> tuple[str, Path]:
    """Accept ``--config PATH ORIGIN``. Chrome on Windows appends
    ``--parent-window=<handle>``; tolerate only that, reject anything else."""
    args = [a for a in argv[1:] if not a.startswith("--parent-window=")]
    if len(args) != 3 or args[0] != "--config":
        raise ValueError("usage: native_host.py --config PATH ORIGIN")
    return args[2], Path(args[1])


def load_config(path: Path) -> dict:
    value = state.read_private_json(path)
    if value is None:
        raise state.StateError("host config missing")
    if protocol.extension_id_from_origin(value.get("extension_origin")) is None:
        raise state.StateError("host config has an invalid extension origin")
    if not isinstance(value.get("socket_path"), str) or not os.path.isabs(value["socket_path"]):
        raise state.StateError("host config has an invalid socket path")
    return value


def connect_hub(socket_path: str) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(HUB_CONNECT_TIMEOUT_S)
        sock.connect(socket_path)
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", raw)
        if uid != os.getuid():
            raise OSError("hub socket peer is a different user")
        sock.settimeout(None)
        return sock
    except BaseException:
        sock.close()
        raise


def _write(stdout, message: dict) -> None:
    stdout.write(protocol.encode_frame(message, protocol.MAX_TO_EXTENSION_BYTES))
    stdout.flush()


def _extension_to_hub(stdin, sock: socket.socket) -> None:
    """Daemon thread: Chrome -> hub. Any EOF or violation tears the hub
    connection down, which in turn ends the main relay loop."""
    reason = "extension closed"
    try:
        while True:
            message = protocol.read_frame(stdin.read, protocol.MAX_FROM_EXTENSION_BYTES)
            if message is None:
                break
            protocol.validate_relay_type(message, protocol.EXTENSION_TO_HUB_TYPES)
            sock.sendall(protocol.encode_frame(message, protocol.MAX_FROM_EXTENSION_BYTES))
    except protocol.ProtocolError as exc:
        reason = f"extension frame rejected ({exc.code})"
    except OSError:
        reason = "hub write failed"
    finally:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        log(f"relay ending: {reason}")


def run(argv: list[str], stdin, stdout, *, connect=connect_hub) -> int:
    try:
        origin, config_path = parse_args(argv)
        config = load_config(config_path)
    except (ValueError, state.StateError) as exc:
        log(f"startup refused: {exc}")
        return 2
    expected_origin = config["extension_origin"]
    if origin != expected_origin:
        log("rejected: calling extension origin is not the installed companion")
        return 2
    extension_id = protocol.extension_id_from_origin(expected_origin)

    try:
        hello = protocol.read_frame(stdin.read, protocol.MAX_FROM_EXTENSION_BYTES)
        if hello is None:
            return 0
        protocol.validate_extension_hello(hello, expected_extension_id=extension_id)
    except protocol.ProtocolError as exc:
        log(f"rejected extension hello ({exc.code})")
        return 3

    try:
        sock = connect(config["socket_path"])
    except OSError:
        log("hub unavailable (Lumina not running or companion hub not started)")
        _write(stdout, {"v": protocol.PROTOCOL_VERSION, "type": "host_status",
                        "state": "hub_unavailable"})
        return 0

    try:
        sock.sendall(protocol.encode_frame({
            "v": protocol.PROTOCOL_VERSION, "type": "hello", "origin": origin,
            "extension_id": extension_id, "instance_id": hello["instance_id"],
            "extension_version": hello["extension_version"],
            "host_version": protocol.HOST_VERSION,
        }, protocol.MAX_FROM_EXTENSION_BYTES))
        _write(stdout, {"v": protocol.PROTOCOL_VERSION, "type": "host_status",
                        "state": "hub_connected"})
        threading.Thread(target=_extension_to_hub, args=(stdin, sock), daemon=True,
                         name="chrome-companion-ext-to-hub").start()
        while True:
            message = protocol.read_frame(sock.recv, protocol.MAX_TO_EXTENSION_BYTES)
            if message is None:
                log("hub closed the connection")
                return 0
            protocol.validate_relay_type(message, protocol.HUB_TO_EXTENSION_TYPES)
            _write(stdout, message)
    except protocol.ProtocolError as exc:
        log(f"hub frame rejected ({exc.code})")
        return 4
    except OSError:
        log("relay I/O ended")
        return 0
    finally:
        sock.close()


def main() -> None:
    code = run(sys.argv, sys.stdin.buffer, sys.stdout.buffer)
    # The Chrome->hub reader is a daemon thread that may still be blocked in
    # stdin.read() holding the BufferedReader lock (e.g. Lumina quit first).
    # A normal interpreter shutdown would then abort with "could not acquire
    # lock for <stdin> at interpreter shutdown" (SIGABRT). Leave immediately
    # instead: everything we wrote has already been flushed.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError):
            pass
    os._exit(code)


if __name__ == "__main__":
    main()
