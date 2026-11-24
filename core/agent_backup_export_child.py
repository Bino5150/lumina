"""core/agent_backup_export_child.py -- one owner export, in its own short-lived
process (F-DR-5).

Why a process: every SQLite member of an export is bound to the file it
opened by diffing this process's /proc/self/fd before and after connect()
(core.agent_backup._connect_sqlite_with_bound_identity). Inside a running
Lumina, another connection (the flight recorder) holds a lock on the same
file; when an export closes its read-only connection, SQLite's unix VFS
parks that descriptor instead of closing it and hands it to the next
read-only open. The next export then sees no new descriptor and fails
closed, so only the first export of a session could succeed. A fresh
process holds no other connection and no parked descriptor, so the identity
proof runs exactly as designed, unweakened.

The parent (core.agent_backup_export.run_owner_export) starts this module as

    python -B -m core.agent_backup_export_child <mode> <data_dir> <base_dir>
        <destination> <credentials_path> <identity_trailers_path>

from the code root, with no stdin and stderr discarded, the same interpreter
and site configuration as the parent, and absolute paths. The credentials and
identity-trailer paths are the ones the in-process call would have used,
resolved by the parent. It runs
the same export and the same confirm_export_result as before.

Protocol: the exit code is the status and stdout the only channel.
    0  {"receipt": <the confirmed receipt>}
    3  {"refused": <owner words>, "code": <refusal code or null>}
Anything else is a failed export. Nothing here grants anything: the archive
is published (or not) by the unchanged builder, and the parent treats these
bytes as data only.
"""
import json
import sys

MODES = ("verified", "incomplete")
EXIT_OK, EXIT_REFUSED, EXIT_USAGE = 0, 3, 2


def main(argv) -> int:
    if len(argv) != 6 or argv[0] not in MODES:
        return EXIT_USAGE
    mode, data_dir, base_dir, destination, credentials_path, identity_trailers_path = argv
    from core import agent_backup_export as ex
    try:
        receipt = ex._owner_export_in_process(
            mode, data_dir, base_dir, destination,
            credentials_path=credentials_path, identity_trailers_path=identity_trailers_path)
    except ex.ExportUnavailable as exc:
        sys.stdout.write(json.dumps({"refused": ex.owner_message(exc), "code": exc.code}))
        return EXIT_REFUSED
    sys.stdout.write(json.dumps({"receipt": receipt}, default=str))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
