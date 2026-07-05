#!/usr/bin/env python3
"""
Approve (or reject) a tool Lumina staged for review via create_tool.

Run this yourself, from a real terminal, after actually reading the source
with show_pending_tool_source (or just `cat tools/_pending/<name>.py`).
This script is deliberately NOT reachable from inside a chat session —
that's the entire point of the review gate. Nothing the model says, no
matter how it's phrased or what convinced it to say it, can run this.

Usage:
    python scripts/approve_tool.py <name>            # approve + hot-load
    python scripts/approve_tool.py <name> --reject    # discard, never loads
    python scripts/approve_tool.py --list             # show what's pending

Note on scope: this moves the file into tools/ and smoke-tests that it
imports and registers cleanly (against a throwaway registry, in this
script's own process — not the running app). That catches syntax/registration
bugs immediately. To actually use the tool in a live Lumina session, either
restart Lumina (same as any newly-added built-in tool picks up on restart)
or wire this into a Settings-tab "Approve" button in a future session so it
runs against the live agent.registry directly, no restart needed.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.toolmaker import approve_pending_tool, PENDING_DIR


class _ThrowawayRegistry:
    """Minimal stand-in just so approve_pending_tool() has something to
    call register_{name}_tool(registry) against, for the smoke test."""
    def __init__(self):
        self._tools = {}

    def register(self, name, fn, description, parameters):
        self._tools[name] = fn
        print(f"  -> registered '{name}' cleanly (smoke test passed)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", nargs="?", help="Pending tool name to approve/reject")
    parser.add_argument("--reject", action="store_true", help="Discard instead of approving")
    parser.add_argument("--list", action="store_true", help="List pending tools and exit")
    args = parser.parse_args()

    if args.list or not args.name:
        os.makedirs(PENDING_DIR, exist_ok=True)
        pending = sorted(f[:-3] for f in os.listdir(PENDING_DIR) if f.endswith(".py"))
        if not pending:
            print("No tools pending review.")
        else:
            print("Pending review:")
            for p in pending:
                print(f"  - {p}  (tools/_pending/{p}.py)")
        return

    if args.reject:
        path = os.path.join(PENDING_DIR, f"{args.name}.py")
        if not os.path.exists(path):
            print(f"No pending tool named '{args.name}'.")
            return
        confirm = input(f"Discard pending tool '{args.name}' without loading it? [y/N] ")
        if confirm.strip().lower() != "y":
            print("Cancelled.")
            return
        os.remove(path)
        print(f"Discarded '{args.name}'.")
        return

    path = os.path.join(PENDING_DIR, f"{args.name}.py")
    if not os.path.exists(path):
        print(f"No pending tool named '{args.name}'. Run --list to see what's staged.")
        return

    print(f"--- Source of '{args.name}' ---")
    with open(path, "r", encoding="utf-8") as f:
        print(f.read())
    print(f"--- end of '{args.name}' ---\n")

    confirm = input(f"Approve and hot-load '{args.name}'? [y/N] ")
    if confirm.strip().lower() != "y":
        print("Cancelled — still pending.")
        return

    result = approve_pending_tool(args.name, _ThrowawayRegistry())
    print(result)


if __name__ == "__main__":
    main()
