"""
Toolmaker — Lumina can write her own tools, but they no longer go live
on her own say-so.

F-34 fix: create_tool used to write straight into tools/ and hot-load
immediately — no review gate, and OWNER_ONLY_TOOLS only walls it off from
Discord, not from a prompt-injected owner session. A single crafted string
reaching this tool used to mean live, callable, arbitrary code with zero
human awareness.

New flow:
  1. create_tool()      -> writes to tools/_pending/, does NOT hot-load,
                            appends a full-source entry to the audit log.
                            Only a syntax check (compile(), never exec()) —
                            no code from an unapproved tool ever runs.
  2. list_pending_tools() / show_pending_tool_source() -> read-only,
                            lets the owner review what's waiting, in-chat.
  3. reject_pending_tool() -> deletes a staged file. Still just file
                            deletion — never touches the live registry.
  4. approve_pending_tool() -> the actual gate. Moves the file into tools/
                            and hot-loads it. Deliberately NOT registered
                            with the agent's tool registry, so nothing in
                            a chat turn — injected or not — can call it.
                            Run it from a real terminal: see
                            scripts/approve_tool.py.

delete_tool (removing an already-approved, live tool) is unchanged — it's
the existing undo half of this, same as it always was.
"""

import os
import json
import importlib.util
import sys
import traceback
from datetime import datetime

import config

PROTECTED = {"registry", "meta", "memory", "knowledge", "web",
             "filesystem", "sandbox", "terminal", "toolmaker",
             # temporal_decay graduated from "custom tool" to a genuine,
             # statically-imported dependency: tools/palace.py does
             # `from tools.temporal_decay import decay_engine` at module
             # load time. delete_tool("temporal_decay") would currently
             # succeed and break MemPalace boot on next launch -- found
             # while scoping the FE-13 custom-tool migration, which would
             # have hit the identical problem by relocating the file.
             "temporal_decay"}

# FE-02: this hand list drifted 9 modules stale (missing palace, browser, pin,
# projects, diff, telegram_send, get_weather, temporal_decay,
# temporal_decay_engine) — delete_tool("palace") could delete the entire
# MemPalace layer from disk, callable from any owner chat turn. A hand list
# only ever drifts one direction: more wrong over time as new modules ship.
#
# Fix: gate deletion on "was this tool ever actually approved through the
# toolmaker review pipeline" instead of maintaining a name list at all.
# Fail-closed, same as the rest of the security spine's default-deny
# philosophy — a name can only become deletable by going through
# create_tool() -> approve_pending_tool(), which is the one path that
# produces an "approved" audit log entry. Every core framework module and
# every shipped file that was added straight to the repo (get_weather.py,
# temporal_decay.py, any future addition) is protected by construction,
# with no list to keep in sync.
def _deletable_tool_names() -> set:
    approved = set()
    if not os.path.exists(AUDIT_LOG_PATH):
        return approved
    try:
        with open(AUDIT_LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                name = entry.get("name")
                event = entry.get("event")
                if not name:
                    continue
                if event == "approved":
                    approved.add(name)
                elif event == "deleted":
                    approved.discard(name)
    except Exception:
        return set()  # any read failure -> nothing deletable, fail closed
    return approved

CUSTOM_TOOLS_DIR = os.path.join(config.DATA_DIR, "custom_tools")
PENDING_DIR = os.path.join(CUSTOM_TOOLS_DIR, "_pending")
AUDIT_LOG_PATH = os.path.join(config.DATA_DIR, "memory", "tool_audit.log")


def _append_audit_log(event: str, name: str, description: str = "", code: str = ""):
    """Append-only forensic trail — every stage of every tool's life,
    full source included, regardless of what happens to it later."""
    os.makedirs(os.path.dirname(AUDIT_LOG_PATH), exist_ok=True)
    entry = {
        "ts": datetime.now().isoformat(),
        "event": event,          # "staged" | "approved" | "rejected" | "deleted"
        "name": name,
        "description": description,
        "code": code,
    }
    try:
        with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"[TOOLMAKER] audit log write failed: {e}", flush=True)


def approve_pending_tool(name: str, registry) -> str:
    """
    THE review gate. Not registered as an agent tool on purpose — call this
    from scripts/approve_tool.py, run by a human in a real terminal, after
    actually reading the source (list_pending_tools /
    show_pending_tool_source surface it in-chat for exactly that purpose).

    Moves the staged file from tools/_pending/ into tools/ and hot-loads it.
    This is the only place in the whole toolmaker pipeline where untrusted
    code actually executes.
    """
    if name in PROTECTED:
        return f"[Error: '{name}' is a protected tool name.]"
    live_path_check = os.path.join(CUSTOM_TOOLS_DIR, f"{name}.py")
    if os.path.exists(live_path_check):
        return f"[Error: '{name}' already exists as a live tool — cannot overwrite via approval.]"

    pending_path = os.path.join(PENDING_DIR, f"{name}.py")
    if not os.path.exists(pending_path):
        return f"[Error: no pending tool named '{name}'.]"

    live_path = os.path.join(CUSTOM_TOOLS_DIR, f"{name}.py")
    with open(pending_path, "r", encoding="utf-8") as f:
        code = f.read()

    try:
        os.replace(pending_path, live_path)
    except Exception as e:
        return f"[Error moving tool into place: {e}]"

    try:
        spec = importlib.util.spec_from_file_location(name, live_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    except Exception:
        return f"[Error loading tool module: {traceback.format_exc()}]"

    register_fn_name = f"register_{name}_tool"
    if not hasattr(module, register_fn_name):
        return f"[Error: module missing '{register_fn_name}' function.]"

    try:
        getattr(module, register_fn_name)(registry)
    except Exception:
        return f"[Error registering tool: {traceback.format_exc()}]"

    _append_audit_log("approved", name, code=code)
    return f"[Tool '{name}' approved, loaded, and live.]"


def load_approved_custom_tools(registry) -> list:
    """
    FE-11 startup loader. approve_pending_tool() moves a staged file into
    tools/ and hot-loads it into *that session's* live registry — nothing
    about that call persists the load across a restart. Every custom tool
    Lumina ever wrote and had approved (get_weather, temporal_decay, or
    whatever comes next) silently vanished the next time the app launched,
    with no error and no log line, until someone noticed it missing from
    the tool list.

    Walks the exact same audit-log-derived "approved" set that
    _deletable_tool_names() already uses for the delete_tool gate — so this
    only ever re-loads a tool that genuinely went through
    create_tool() -> approve_pending_tool() in the past. A bare .py file
    someone drops into tools/ by hand, without a matching "approved" audit
    log entry, is never picked up here, same fail-closed posture as the
    deletion gate.

    Each tool loads inside its own try/except. One broken or half-written
    custom tool (see: temporal_decay_engine.py's mismatched register-call
    arity) must never be able to block the rest of boot — it gets logged
    and skipped, not raised.

    Returns the list of tool names actually loaded, for anyone who wants to
    log/print it at startup.
    """
    loaded = []
    approved = _deletable_tool_names()
    for name in sorted(approved):
        live_path = os.path.join(CUSTOM_TOOLS_DIR, f"{name}.py")
        if not os.path.exists(live_path):
            # Approved once, but the file is gone now (manually removed
            # outside the delete_tool path) — nothing to load.
            continue

        register_fn_name = f"register_{name}_tool"
        try:
            spec = importlib.util.spec_from_file_location(name, live_path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        except Exception:
            print(f"[TOOLMAKER] startup load FAILED (import) for '{name}':\n"
                  f"{traceback.format_exc()}", flush=True)
            continue

        if not hasattr(module, register_fn_name):
            print(f"[TOOLMAKER] startup load skipped '{name}': "
                  f"module has no {register_fn_name}(registry).", flush=True)
            continue

        try:
            getattr(module, register_fn_name)(registry)
        except Exception:
            print(f"[TOOLMAKER] startup load FAILED (register) for '{name}':\n"
                  f"{traceback.format_exc()}", flush=True)
            continue

        loaded.append(name)

    return loaded


def register_toolmaker_tools(registry, agent):

    def create_tool(name: str, description: str, code: str) -> str:
        """
        Stage a new tool for human review. Does NOT go live and does NOT
        execute — only checked for valid Python syntax. An owner (or
        anyone with the Settings terminal) must explicitly run
        scripts/approve_tool.py before this becomes a callable tool.
        """
        # FE-02: block on PROTECTED (hand list, kept only as a name-clash
        # belt-and-suspenders) OR a real collision with any live file in
        # tools/ — the latter can't drift, since it checks what's actually
        # on disk rather than a maintained set.
        if name in PROTECTED or os.path.exists(os.path.join(CUSTOM_TOOLS_DIR, f"{name}.py")):
            return f"[Error: '{name}' is protected or already exists as a live tool.]"

        try:
            compile(code, f"<pending:{name}>", "exec")
        except SyntaxError as e:
            return f"[Error: code has a syntax error and was not staged: {e}]"

        os.makedirs(PENDING_DIR, exist_ok=True)
        pending_path = os.path.join(PENDING_DIR, f"{name}.py")
        try:
            with open(pending_path, "w", encoding="utf-8") as f:
                f.write(code)
        except Exception as e:
            return f"[Error writing pending tool file: {e}]"

        _append_audit_log("staged", name, description=description, code=code)
        return (f"[Tool '{name}' staged for review — NOT loaded yet. "
                f"Ask the owner to review it (list_pending_tools / "
                f"show_pending_tool_source) and run scripts/approve_tool.py "
                f"{name} from a terminal to activate it.]")

    def list_pending_tools() -> str:
        """List tools staged for review but not yet approved/loaded."""
        os.makedirs(PENDING_DIR, exist_ok=True)
        pending = sorted(f[:-3] for f in os.listdir(PENDING_DIR) if f.endswith(".py"))
        if not pending:
            return "[No tools pending review.]"
        return f"[Pending review: {', '.join(pending)}]"

    def show_pending_tool_source(name: str) -> str:
        """Show the full source of a staged (not-yet-approved) tool, for review."""
        pending_path = os.path.join(PENDING_DIR, f"{name}.py")
        if not os.path.exists(pending_path):
            return f"[Error: no pending tool named '{name}'.]"
        with open(pending_path, "r", encoding="utf-8") as f:
            return f.read()

    def reject_pending_tool(name: str) -> str:
        """Discard a staged tool without ever loading it."""
        pending_path = os.path.join(PENDING_DIR, f"{name}.py")
        if not os.path.exists(pending_path):
            return f"[Error: no pending tool named '{name}'.]"
        try:
            os.remove(pending_path)
            _append_audit_log("rejected", name)
            return f"[Pending tool '{name}' discarded — never loaded.]"
        except Exception as e:
            return f"[Error rejecting tool: {e}]"

    def list_custom_tools() -> str:
        """List all live custom tools written by Lumina — i.e. tools that
        actually went through create_tool() -> approve_pending_tool(), not
        just any .py file sitting in tools/ that isn't hand-listed as core."""
        deletable = _deletable_tool_names()
        custom = [f[:-3] for f in os.listdir(CUSTOM_TOOLS_DIR)
                  if f.endswith(".py") and f[:-3] in deletable]
        if not custom:
            return "[No custom tools yet.]"
        return f"[Custom tools: {', '.join(sorted(custom))}]"

    def delete_tool(name: str) -> str:
        """Delete a live custom tool file and unregister it."""
        if name not in _deletable_tool_names():
            return (f"[Error: '{name}' was not created and approved through "
                     f"the toolmaker pipeline, so it cannot be deleted.]")

        filepath = os.path.join(CUSTOM_TOOLS_DIR, f"{name}.py")

        if not os.path.exists(filepath):
            return f"[Error: tool file '{name}.py' not found.]"

        try:
            os.remove(filepath)
            if name in agent.registry._tools:
                del agent.registry._tools[name]
            if name in sys.modules:
                del sys.modules[name]
            _append_audit_log("deleted", name)
            return f"[Tool '{name}' deleted and unregistered.]"
        except Exception as e:
            return f"[Error deleting tool: {e}]"

    registry.register(
        name="create_tool",
        fn=create_tool,
        description="Stage a new Python tool for human review. Does not go live until approved.",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Tool name — also the filename (no .py)."},
                "description": {"type": "string", "description": "What the tool does."},
                "code": {"type": "string", "description": "Full Python source. Must define the tool function and a register_{name}_tool(registry) function."}
            },
            "required": ["name", "description", "code"]
        }
    )

    registry.register(
        name="list_pending_tools",
        fn=list_pending_tools,
        description="List tools staged for review but not yet approved.",
        parameters={"type": "object", "properties": {}, "required": []}
    )

    registry.register(
        name="show_pending_tool_source",
        fn=show_pending_tool_source,
        description="Show the full source of a tool pending review.",
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Pending tool name."}},
            "required": ["name"]
        }
    )

    registry.register(
        name="reject_pending_tool",
        fn=reject_pending_tool,
        description="Discard a pending tool without ever loading it.",
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Pending tool name to discard."}},
            "required": ["name"]
        }
    )

    registry.register(
        name="list_custom_tools",
        fn=list_custom_tools,
        description="List all live custom tools written by Lumina.",
        parameters={
            "type": "object",
            "properties": {},
            "required": []
        }
    )

    registry.register(
        name="delete_tool",
        fn=delete_tool,
        description="Delete a live custom tool and unregister it from the registry.",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name of the tool to delete."}
            },
            "required": ["name"]
        }
    )
