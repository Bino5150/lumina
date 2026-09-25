#!/usr/bin/env python3
"""Owner CLI for Lumina's Chrome Companion (BROWSER-COMPANION-01A).

    python scripts/chrome_companion_setup.py --data-dir DIR install --extension-id ID
    python scripts/chrome_companion_setup.py --data-dir DIR pair INSTANCE_ID
    python scripts/chrome_companion_setup.py --data-dir DIR status
    python scripts/chrome_companion_setup.py --data-dir DIR unpair
    python scripts/chrome_companion_setup.py --data-dir DIR uninstall

--data-dir must be the SAME data dir the Lumina build you run uses (the
release launcher uses ~/.local/share/lumina-release; a default install uses
~/.local/share/lumina). It is required -- falling back to LUMINA_DATA_DIR
only when that is explicitly exported -- so a companion can never be
silently registered against the wrong build's state. This script never
imports Lumina's config (no import-time data migration side effects).

Steps (owner, once):
  1. chrome://extensions -> Developer mode -> Load unpacked ->
     <repo>/chrome_companion/extension   (in Lumina's Chrome profile)
  2. install --extension-id <the ID Chrome shows for it>
  3. open the extension popup, copy its Pairing ID, run: pair <that ID>
  4. (re)start Lumina
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chrome_companion import installer, state  # noqa: E402


def _data_dir(args) -> Path:
    raw = args.data_dir or os.environ.get("LUMINA_DATA_DIR")
    if not raw:
        raise SystemExit("error: --data-dir is required (the LUMINA_DATA_DIR of the Lumina build you run)")
    # Never resolved (R4 / AR4): resolving would hand every command the link's
    # target of the moment, and the walk would never see the link itself or
    # who can replace it. The path is checked as given, and used as given.
    path = state.absolute_path(Path(raw).expanduser())
    if not state.check_data_dir(path):  # a hostile chain raises StateError -> exit 1
        raise SystemExit(f"error: data dir does not exist: {path}")
    return path


def cmd_install(args) -> int:
    data_dir = _data_dir(args)
    result = installer.install(data_dir, extension_id=args.extension_id.strip())
    print(f"Data dir:        {data_dir}")
    print(f"Extension:       {result['extension_origin']}")
    print(f"Host manifest:   {result['host_manifest']}")
    print(f"Host launcher:   {result['launcher']}")
    print(f"Hub socket:      {result['socket_path']}")
    print("Next: open the Lumina Companion popup in Chrome, copy its Pairing ID, then run")
    print(f"  python scripts/{Path(__file__).name} --data-dir {data_dir} pair <PAIRING-ID>")
    return 0


def cmd_pair(args) -> int:
    data_dir = _data_dir(args)
    install = state.load_install(data_dir)
    if install is None:
        raise SystemExit("error: not installed for this data dir -- run install first")
    record = state.save_pairing(data_dir, instance_id=args.instance_id,
                                extension_origin=install.extension_origin)
    print(f"Paired Chrome instance {state.format_instance_id(record.instance_id)}")
    print(f"  fingerprint {state.instance_fingerprint(record.instance_id)} for {record.extension_origin}")
    print("Any other Chrome profile/instance of this extension will be refused.")
    return 0


def cmd_unpair(args) -> int:
    removed = state.clear_pairing(_data_dir(args))
    print("Pairing removed." if removed else "No pairing existed.")
    return 0


def cmd_status(args) -> int:
    data_dir = _data_dir(args)
    install = state.load_install(data_dir)
    pairing = state.load_pairing(data_dir)
    manifest_path = installer.host_manifest_path()
    print(json.dumps({
        "data_dir": str(data_dir),
        "installed": install is not None,
        "extension_origin": install.extension_origin if install else None,
        "socket_path": install.socket_path if install else None,
        "host_manifest": str(manifest_path) if manifest_path.exists() else None,
        "paired_instance_fingerprint": state.instance_fingerprint(pairing.instance_id) if pairing else None,
    }, indent=2))
    return 0


def cmd_uninstall(args) -> int:
    for path in installer.uninstall(_data_dir(args)):
        print(f"removed {path}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Lumina Chrome Companion installer / pairing")
    parser.add_argument("--data-dir", help="Lumina data dir (LUMINA_DATA_DIR of the build you run)")
    sub = parser.add_subparsers(dest="command", required=True)
    p_install = sub.add_parser("install", help="register the native host for an extension ID")
    p_install.add_argument("--extension-id", required=True)
    p_install.set_defaults(fn=cmd_install)
    p_pair = sub.add_parser("pair", help="enroll the Chrome extension instance shown in the popup")
    p_pair.add_argument("instance_id")
    p_pair.set_defaults(fn=cmd_pair)
    sub.add_parser("unpair", help="remove the enrolled instance").set_defaults(fn=cmd_unpair)
    sub.add_parser("status", help="show install/pairing state").set_defaults(fn=cmd_status)
    sub.add_parser("uninstall", help="remove this data dir's companion registration").set_defaults(fn=cmd_uninstall)
    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except state.StateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
